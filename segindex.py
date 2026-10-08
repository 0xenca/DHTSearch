"""
Inverted index on disk, built like a search engine (log-structured): token -> sorted document ids.

  * New documents go to a small in-memory DELTA (bounded: ~1 M postings).
  * When the delta is full it is written as an immutable SEGMENT: a few files written ONCE, sequentially.
  * Segments are merged in the background by tiers (8 similar-sized segments -> 1): every posting is rewritten only
    ~log8(total / delta) times in its whole life, always sequentially. No random in-place updates (that is what made
    SQLite write ~2 MB per indexed torrent: SSD wear).
  * Reads use mmap: a sparse "fence" of every 256th token is in RAM, so a lookup touches 1-2 pages per segment.

Keys are bytes: one field letter + the token (b"n" name, b"f" file names, b"e" extension), so every field shares the
same machinery. Documents are added strictly in increasing id order, so concatenating the postings of the segments
(oldest first) and the delta gives an already sorted list.

data/index/: manifest.json (segments in order + "docs": every document below it is in a segment) and, per segment,
<id>.blob (tokens), <id>.toff (u32 end offsets), <id>.poff (u32 end positions), <id>.post (u32 doc ids), <id>.json.
"""
import bisect
import heapq
import json
import logging
import mmap
import os
import threading
import time
from array import array

import numpy as np

from memstat import drop_cache

log = logging.getLogger("index")

FENCE = 256
MERGE_FACTOR = 8
FLUSH_POSTINGS = 1_000_000
_EMPTY = np.zeros(0, np.uint32)
_EXTS = ("blob", "toff", "poff", "post", "json")


class _Aborted(Exception):
    pass


def _upper(prefix):
    return prefix[:-1] + bytes([prefix[-1] + 1]) if prefix and prefix[-1] < 255 else prefix + b"\xff"


class Segment:
    def __init__(self, d, sid):
        self.sid = sid
        base = os.path.join(d, f"{sid:08d}")
        with open(base + ".json") as f:
            m = json.load(f)
        self.ntok, self.npost, self.lo, self.hi = m["ntok"], m["npost"], m["lo"], m["hi"]
        self.fence = [x.encode("latin-1") for x in m["fence"]]
        self.paths = [base + "." + e for e in _EXTS]
        self._mm = []

        def mm(ext):
            with open(base + "." + ext, "rb") as f:
                if os.fstat(f.fileno()).st_size == 0:
                    return b""
                m_ = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
                self._mm.append(m_)
                return m_
        self.blob = mm("blob")
        self.toff = np.frombuffer(mm("toff"), np.uint32) if self.ntok else _EMPTY
        self.poff = np.frombuffer(mm("poff"), np.uint32) if self.ntok else _EMPTY
        self.post = np.frombuffer(mm("post"), np.uint32) if self.npost else _EMPTY

    def tier(self):
        n, t = max(self.npost, 1), 0
        while n >= FLUSH_POSTINGS * (MERGE_FACTOR ** (t + 1)):
            t += 1
        return t

    def tok(self, i):
        a = int(self.toff[i - 1]) if i else 0
        return self.blob[a: int(self.toff[i])]

    def _lower(self, key):
        """First token index >= key."""
        if not self.ntok:
            return 0
        k = bisect.bisect_right(self.fence, key) - 1
        if k < 0:
            return 0
        lo, hi = k * FENCE, min((k + 1) * FENCE, self.ntok)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.tok(mid) < key:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def find(self, key):
        i = self._lower(key)
        return i if i < self.ntok and self.tok(i) == key else -1

    def postings_at(self, i):
        a = int(self.poff[i - 1]) if i else 0
        return self.post[a: int(self.poff[i])]

    def count_at(self, i):
        return int(self.poff[i]) - (int(self.poff[i - 1]) if i else 0)

    def range(self, prefix):
        """(first, last+1) token indexes starting with prefix."""
        return self._lower(prefix), self._lower(_upper(prefix))

    def items(self):
        """(token, postings) in order: for merging."""
        blob, toff, poff, post = self.blob, self.toff, self.poff, self.post
        a = p = 0
        for i in range(self.ntok):
            b, q = int(toff[i]), int(poff[i])
            yield blob[a:b], post[p:q]
            a, p = b, q

    def close(self):
        self.toff = self.poff = self.post = _EMPTY
        self.blob = b""
        for m in self._mm:
            try:
                m.close()
            except BufferError:              # a reader still holds a view: the GC closes it later
                pass
        self._mm = []


class _Writer:
    def __init__(self, d, sid):
        self.base = os.path.join(d, f"{sid:08d}")
        self.f = {e: open(self.base + "." + e + ".tmp", "wb", buffering=1 << 20) for e in _EXTS[:4]}
        self.ntok = self.npost = self.boff = 0
        self.fence = []
        self.toff, self.poff = array("I"), array("I")

    def add(self, key, docs):
        if self.ntok % FENCE == 0:
            self.fence.append(key.decode("latin-1"))
        self.f["blob"].write(key)
        self.boff += len(key)
        self.toff.append(self.boff)
        if isinstance(docs, np.ndarray):
            self.f["post"].write(docs.astype(np.uint32, copy=False).tobytes())
            n = len(docs)
        else:
            self.f["post"].write(docs.tobytes())
            n = len(docs)
        self.npost += n
        self.poff.append(self.npost)
        self.ntok += 1
        if len(self.toff) >= 1 << 18:                 # flush the offset buffers every 256 K tokens
            self.f["toff"].write(self.toff.tobytes())
            self.f["poff"].write(self.poff.tobytes())
            self.toff, self.poff = array("I"), array("I")

    def finish(self, lo, hi):
        self.f["toff"].write(self.toff.tobytes())
        self.f["poff"].write(self.poff.tobytes())
        for e, f in self.f.items():
            f.flush()
            os.fsync(f.fileno())
            f.close()
            os.replace(self.base + "." + e + ".tmp", self.base + "." + e)
            drop_cache(self.base + "." + e)          # just written: do not keep it as page cache
        meta = {"ntok": self.ntok, "npost": self.npost, "lo": lo, "hi": hi, "fence": self.fence}
        with open(self.base + ".json.tmp", "w") as f:
            json.dump(meta, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(self.base + ".json.tmp", self.base + ".json")

    def abort(self):
        for e, f in self.f.items():
            f.close()
            try:
                os.remove(self.base + "." + e + ".tmp")
            except OSError:
                pass


class SegIndex:
    def __init__(self, d, background=True):
        self.dir = d
        os.makedirs(d, exist_ok=True)
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        man = self._read_manifest()
        self.next_id = man.get("next_id", 1)
        self.segs = []
        for sid in man.get("segments", []):
            try:
                self.segs.append(Segment(d, sid))
            except (OSError, ValueError, KeyError) as e:  # damaged: drop it and everything after (re-indexed)
                log.warning("index segment %s unreadable (%s): re-indexing from there", sid, e)
                break
        self.docs = self.segs[-1].hi if self.segs else 0  # every document below this is in a segment
        if man.get("docs", 0) != self.docs or len(self.segs) != len(man.get("segments", [])):
            self._write_manifest()
        self._cleanup()
        self.delta, self.delta_lo, self.delta_hi, self.delta_n = {}, self.docs, self.docs, 0
        self.frozen = None                                  # (dict, lo, hi) being written
        self.bytes_written = 0
        self._stop = False
        self._busy = False
        self._thread = None
        if background:
            self._thread = threading.Thread(target=self._run, name="index-writer", daemon=True)
            self._thread.start()

    # ------------------------------------------------------------ manifest
    def _read_manifest(self):
        try:
            with open(os.path.join(self.dir, "manifest.json")) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _write_manifest(self):
        p = os.path.join(self.dir, "manifest.json")
        with open(p + ".tmp", "w") as f:
            json.dump({"version": 1, "segments": [s.sid for s in self.segs], "next_id": self.next_id,
                       "docs": self.segs[-1].hi if self.segs else 0}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(p + ".tmp", p)

    def _cleanup(self):
        keep = {s.sid for s in self.segs}
        for fn in os.listdir(self.dir):
            head = fn.split(".")[0]
            if head.isdigit() and int(head) not in keep:
                try:
                    os.remove(os.path.join(self.dir, fn))
                except OSError:
                    pass

    # ------------------------------------------------------------ writing
    def indexed_upto(self):
        """Documents below this id are searchable (segments + delta)."""
        return self.delta_hi

    def add_doc(self, d, keys):
        """Adds one document (ids strictly increasing). keys: iterable of bytes."""
        with self.lock:
            if d < self.delta_hi:
                return
            dl = self.delta
            n = 0
            for k in keys:
                a = dl.get(k)
                if a is None:
                    a = dl[k] = array("I")
                a.append(d)
                n += 1
            self.delta_hi = d + 1
            self.delta_n += n
            if self.delta_n >= FLUSH_POSTINGS and self.frozen is None:
                self.cond.notify_all()

    def skip_to(self, d):
        """No document below d needs indexing (e.g. deleted ones at the end)."""
        with self.lock:
            if d > self.delta_hi:
                self.delta_hi = d

    def _freeze(self):
        with self.lock:
            if self.frozen is not None or not self.delta and self.delta_hi == self.delta_lo:
                return None
            self.frozen = (self.delta, self.delta_lo, self.delta_hi)
            self.delta, self.delta_lo, self.delta_n = {}, self.delta_hi, 0
            return self.frozen

    def flush(self):
        """Writes the delta as a segment (synchronously)."""
        fr = self._freeze()
        if fr is None:
            return False
        dl, lo, hi = fr
        sid = self._new_id()
        w = _Writer(self.dir, sid)
        try:
            for k in sorted(dl):
                w.add(k, dl[k])
            w.finish(lo, hi)
        except Exception:
            w.abort()
            with self.lock:                          # put it back in front of the live delta
                for k, a in dl.items():
                    cur = self.delta.get(k)
                    self.delta[k] = a + cur if cur is not None else a
                self.delta_lo, self.frozen = lo, None
            raise
        self.bytes_written += w.boff + 8 * w.ntok + 4 * w.npost
        seg = Segment(self.dir, sid)
        with self.lock:
            self.segs.append(seg)
            self.frozen = None
            self._write_manifest()
        return True

    def _new_id(self):
        with self.lock:
            sid = self.next_id
            self.next_id += 1
            return sid

    def _merge_candidates(self):
        with self.lock:
            segs = list(self.segs)
        if len(segs) < MERGE_FACTOR:
            return None
        t = segs[-1].tier()
        j = len(segs)
        while j > 0 and segs[j - 1].tier() == t:
            j -= 1
        run = segs[j:]
        return run if len(run) >= MERGE_FACTOR else None

    def merge(self, run):
        """Merges consecutive segments into one (sequential read + write)."""
        sid = self._new_id()
        w = _Writer(self.dir, sid)
        try:
            def tagged(seg, i):
                for k, p in seg.items():
                    yield k, i, p
            streams = [tagged(s, i) for i, s in enumerate(run)]
            cur, parts, n = None, [], 0
            for k, i, p in heapq.merge(*streams):
                n += 1
                if n & 0xFFFF == 0 and self._stop:       # shutting down: give up (the inputs are untouched; redone later)
                    raise _Aborted()
                if k != cur:
                    if cur is not None:
                        w.add(bytes(cur), parts[0] if len(parts) == 1 else np.concatenate(parts))
                    cur, parts = k, []
                parts.append(p)
            if cur is not None:
                w.add(bytes(cur), parts[0] if len(parts) == 1 else np.concatenate(parts))
            w.finish(run[0].lo, run[-1].hi)
        except BaseException:
            w.abort()
            raise
        self.bytes_written += w.boff + 8 * w.ntok + 4 * w.npost
        seg = Segment(self.dir, sid)
        with self.lock:
            i = self.segs.index(run[0])
            assert self.segs[i: i + len(run)] == run
            self.segs[i: i + len(run)] = [seg]
            self._write_manifest()
        for s in run:
            for p in s.paths:
                try:
                    os.remove(p)
                except OSError:
                    pass
            s.close()
        return seg

    def _run(self):
        while True:
            with self.lock:
                while not self._stop and self.delta_n < FLUSH_POSTINGS and self._merge_candidates() is None:
                    self.cond.wait(30)
                if self._stop:
                    return
                self._busy = True
            try:
                if self.delta_n >= FLUSH_POSTINGS:
                    self.flush()
                run = self._merge_candidates()
                if run:
                    t0 = time.time()
                    seg = self.merge(run)
                    log.info("index: merged %d segments into one (%d tokens, %d postings) in %.1fs", len(run), seg.ntok,
                             seg.npost, time.time() - t0)
            except _Aborted:
                log.info("index: merge interrupted by shutdown (it is redone later)")
                return
            except Exception as e:
                log.warning("index writer: %s", e)
                time.sleep(10)
            finally:
                with self.lock:
                    self._busy = False
                    self.cond.notify_all()

    def merge_all_pending(self):
        """Runs the merges that are due, in this thread (tests, shutdown)."""
        while True:
            run = self._merge_candidates()
            if not run:
                return
            self.merge(run)

    def close(self, flush=True):
        with self.lock:
            self._stop = True
            self.cond.notify_all()
        if self._thread:
            self._thread.join(timeout=600)
        if flush:
            self.flush()

    # ------------------------------------------------------------ reading
    def _snapshot(self):
        with self.lock:
            fr = self.frozen
            return list(self.segs), (fr[0] if fr else None), self.delta

    def postings(self, key):
        """Sorted document ids that contain key (numpy uint32)."""
        segs, frozen, delta = self._snapshot()
        parts = []
        for s in segs:
            i = s.find(key)
            if i >= 0:
                parts.append(s.postings_at(i))
        for dl in (frozen, delta):
            if dl:
                a = dl.get(key)
                if a is not None:
                    parts.append(np.frombuffer(a.tobytes(), np.uint32))
        if not parts:
            return _EMPTY
        return parts[0].copy() if len(parts) == 1 else np.concatenate(parts)

    def count(self, key):
        segs, frozen, delta = self._snapshot()
        n = 0
        for s in segs:
            i = s.find(key)
            if i >= 0:
                n += s.count_at(i)
        for dl in (frozen, delta):
            if dl:
                a = dl.get(key)
                if a is not None:
                    n += len(a)
        return n

    def prefix_postings(self, prefix, max_tokens=4000, max_postings=2_000_000):
        """Union of the postings of every token starting with prefix (bounded)."""
        segs, frozen, delta = self._snapshot()
        parts, ntok, npost = [], 0, 0
        for s in segs:
            a, b = s.range(prefix)
            for i in range(a, min(b, a + max_tokens)):
                p = s.postings_at(i)
                parts.append(p)
                npost += len(p)
                if npost >= max_postings:
                    break
            ntok += b - a
        for dl in (frozen, delta):
            if dl:
                for k, a in dl.items():
                    if k.startswith(prefix):
                        parts.append(np.frombuffer(a.tobytes(), np.uint32))
        if not parts:
            return _EMPTY
        return np.unique(np.concatenate(parts))

    def keys_with_prefix(self, prefix, limit=3000):
        """{key: document count} for keys starting with prefix (suggestions, "did you mean")."""
        segs, frozen, delta = self._snapshot()
        out = {}
        for s in segs:
            a, b = s.range(prefix)
            for i in range(a, min(b, a + limit)):
                k = bytes(s.tok(i))
                out[k] = out.get(k, 0) + s.count_at(i)
        for dl in (frozen, delta):
            if dl:
                for k, a in dl.items():
                    if k.startswith(prefix):
                        out[k] = out.get(k, 0) + len(a)
        return out

    def stats(self):
        with self.lock:
            segs = list(self.segs)
            size = 0
            for s in segs:
                for p in s.paths:
                    try:
                        size += os.path.getsize(p)
                    except OSError:
                        pass
            return {"segments": len(segs), "docs": self.delta_hi, "in_segments": segs[-1].hi if segs else 0,
                    "delta_postings": self.delta_n, "bytes": size, "postings": sum(s.npost for s in segs) + self.delta_n,
                    "written_bytes": self.bytes_written}
