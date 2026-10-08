"""
Data store (v4: built for millions of torrents on a small server).

Files in data_dir:
  meta.log         IMMUTABLE part of every torrent (name, files, comment, trackers…), zlib-compressed, written ONCE and
                   never rewritten -> metalog.py
  names.dat        torrent names (append-only), read with pread/mmap
  state.bin        CHECKPOINT of the hot fields of every torrent as columns (colstore.py): written atomically once a day
                   and on shutdown; startup loads it instead of re-reading everything
  health-N.wal     health measurements since the last checkpoint (JSON lines); deleted after the next checkpoint
  health.bin       last 40 health measurements + latest per-tracker breakdown of each torrent: one 1 KiB slot per
                   torrent updated in place (one 4 KiB page written per measurement) -> slots.py; health.strings
  index/           inverted index of names, file names and extensions (immutable segments) -> segindex.py
  hashes.json      discovered infohashes (snapshot on shutdown) + hashes.jsonl (changes since then)
  nodes.json, stats.json, history_*, blocklist.txt, hidden_rules.json, peers.jsonl -> as before

Everything except meta.log, the WAL and health.bin can be rebuilt from them. Upgrading from 3.x converts
torrents.jsonl once (kept as torrents.jsonl.v3; `python tools.py export-v3` writes a 3.x journal for rolling back).
"""
import glob
import hashlib
import json
import logging
import math
import os
import random
import re
import sys
import threading
import time
from collections import Counter, deque

import numpy as np

import search as _search
from colstore import (AI_ALLOW, AI_DONE, AI_ERR, AI_HIDE, CATEGORY_LIST, F_DEAD, F_DELETED, F_PRIVATE, SRC_LIST, Columns, Names,
                      ext_mask, mask_exts)
from hiderules import RuleBook, evaluate
from jsonio import atomic_write, iter_jsonl_offsets, load_json, read_jsonl
from metalog import MetaLog
from packing import hh_append, unpack_hh
from peers import PeerStore
from records import (DEAD_MIN_MEASURES, DEAD_MIN_SPAN, DEAD_MIN_TRACKERS, HH_MAX, REFRESH_INTERVAL, SRC_CODE, STATES,
                     UNKNOWN_BASE, UNKNOWN_MAX)
from segindex import SegIndex
from slots import Breakdowns, HealthFile, History
from stats import Stats
from textutil import (LEGACY_CATEGORIES, AGE_BUCKETS, SEED_BUCKETS, SIZE_BUCKETS, category_of, ext_of, index_tokens, magnet_of,
                      size_bucket, top_exts, _LETTERS)

log = logging.getLogger("store")

MAX_FILES_STORED = 5000          # files stored per torrent (file_count keeps the real number); all of them are indexed
QUEUE_MAX = 20_000               # cap of pending hashes PER QUEUE; when full, the OLDEST are dropped
MAX_ATTEMPTS = 3                 # metadata download attempts for hashes with signs of life
RETRY_DELAY = 300                # s before retrying a hash that failed
RETRY_MAX = 20_000
RETRY_FAILED_AFTER = 6 * 3600    # a failed hash that shows up again is retried after this long
FAILED_TTL = 3 * 86400           # failed hashes are purged after this long without being seen
FAILED_MAX = 50_000              # cap of retained failed hashes
MAX_NODES_PERSISTED = 100_000
FLUSH_MIN_INTERVAL = {"nodes": 600}
CHECKPOINT_EVERY = 24 * 3600     # columns written to state.bin at most once a day (and on shutdown)…
CHECKPOINT_WAL_BYTES = 64 << 20  # …or when the health WAL reaches this size
FORCED_MAX = 1000                # refreshes requested from the admin panel, pending at most
QUEUES = ("peer", "prio", "getpeers", "other")
BANDIT_WINDOW = 4000             # recent probes that weigh in the split between queues
_PERSISTED = ("failed", "retry")  # hash states written to hashes.jsonl as they change

STATE_CODES = {s: i for i, s in enumerate(STATES)}        # alive weak quiet dead unknown
SRC_IX = {s: i for i, s in enumerate(SRC_LIST)}            # none scrape swarm legacy
CAT_IX = {c: i for i, c in enumerate(CATEGORY_LIST)}
_SCRAPE, _NONE = SRC_IX["scrape"], SRC_IX["none"]
_SIZE_EDGES = np.array([lim for _, lim in SIZE_BUCKETS[:-1]], np.float64)
_SEED_EDGES = np.array([lim for _, lim in SEED_BUCKETS[:-1]], np.float64)

BLOCKLIST_HEADER = ("# One regular expression per line. Torrents whose name or files\n"
                    "# match are NOT indexed (and are deleted at startup if already present). Example:\n"
                    "# \\bmy_forbidden_word\\b\n")
LEGACY_BLOCKLIST_HEADER = ("# Una expresión regular por línea. Los torrents cuyo nombre o ficheros\n"
                           "# coincidan NO se indexan (y se eliminan al arrancar si ya estaban). Ejemplo:\n"
                           "# \\bmi_palabra_prohibida\\b\n")


def doc_keys(name, files):
    """Index keys of a torrent: b"n"+token (name), b"f"+token (any stored file name), b"e"+extension."""
    keys = {b"n" + t.encode() for t in index_tokens(name)}
    ftoks, exts = set(), set()
    for p in files[:MAX_FILES_STORED]:
        path = p[0] if isinstance(p, (list, tuple)) else p
        ftoks |= index_tokens(path)
        e = ext_of(path)
        if e and e.isascii():
            exts.add(e)
    keys.update(b"f" + t.encode() for t in ftoks)
    keys.update(b"e" + e.encode() for e in exts)
    return keys


def history_flags(hh):
    """(dead confirmed?, trailing measurements without a tracker answer) from a history list (oldest first)."""
    streak = []
    for e in reversed(hh):
        if e[3] == "s" and e[1] == 0 and e[2] == 0 and (e[4] if len(e) > 4 else 1) >= DEAD_MIN_TRACKERS:
            streak.append(e)
        else:
            break
    dead = len(streak) >= DEAD_MIN_MEASURES and streak[0][0] - streak[-1][0] >= DEAD_MIN_SPAN
    k = 0
    for e in reversed(hh):
        if e[3] == "s":
            break
        k += 1
    return dead, min(k, 255)


class HashState:
    """State of a discovered infohash (pending / probing / retry / failed). Behaves like the dict it replaces
    (h["status"], h.get("hits", 1), h.setdefault("pe", [])) but takes ~90 B instead of ~400 B."""
    __slots__ = ("first_seen", "last_seen", "hits", "src", "attempts", "status", "failed_at", "pe", "qk")
    _KEYS = frozenset(__slots__)

    def __init__(self, d=None):
        for k, v in (d or {}).items():
            if k in self._KEYS:
                setattr(self, k, sys.intern(v) if k in ("src", "status", "qk") and isinstance(v, str) else v)

    def get(self, k, default=None):
        return getattr(self, k, default) if k in self._KEYS else default

    def __getitem__(self, k):
        try:
            return getattr(self, k)
        except AttributeError:
            raise KeyError(k) from None

    def __setitem__(self, k, v):
        setattr(self, k, v)

    def __contains__(self, k):
        return k in self._KEYS and hasattr(self, k)

    def setdefault(self, k, default=None):
        if not hasattr(self, k):
            setattr(self, k, default)
        return getattr(self, k)

    def update(self, d=(), **kw):
        for k, v in dict(d, **kw).items():
            setattr(self, k, v)

    def keys(self):
        return [k for k in self.__slots__ if hasattr(self, k)]

    def items(self):
        return [(k, getattr(self, k)) for k in self.keys()]

    def __repr__(self):
        return f"HashState({dict(self.items())})"


class _WAL:
    """Health measurements since the last checkpoint: data/health-<gen>.wal (JSON lines, the 3.x "h" event format)."""

    def __init__(self, d, gen):
        self.dir, self.gen = d, gen
        self.path = os.path.join(d, f"health-{gen}.wal")
        self.f = open(self.path, "ab")
        self.size = os.fstat(self.f.fileno()).st_size

    def append(self, ev):
        b = (json.dumps(ev, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
        self.f.write(b)
        self.size += len(b)

    def flush(self):
        self.f.flush()

    def close(self):
        self.f.flush()
        try:
            os.fsync(self.f.fileno())
        except OSError:
            pass
        self.f.close()

    @staticmethod
    def files(d):
        out = []
        for p in glob.glob(os.path.join(d, "health-*.wal")):
            try:
                out.append((int(os.path.basename(p)[7:-4]), p))
            except ValueError:
                pass
        return sorted(out)


class RecView:
    """One torrent seen as the dict of 3.x (rec["seeders"], rec.get("hh"), rec["health_src"] = …), backed by the
    columns and the slot files. For the code paths that handle ONE torrent (admin, tests, demo)."""
    __slots__ = ("_s", "d")
    _NUM = ("size", "file_count", "seeders", "peers", "health_at", "checked_at", "seed_ok_at", "last_seen", "indexed_at", "created")

    def __init__(self, store, d):
        self._s, self.d = store, d

    def get(self, k, default=None):
        try:
            v = self[k]
        except KeyError:
            return default
        return default if v is None else v

    def __getitem__(self, k):
        s, d = self._s, self.d
        C = s.cols.c
        if k in self._NUM:
            v = int(C[k][d])
            if k in ("checked_at", "seed_ok_at") and not v:
                raise KeyError(k)
            return v
        if k == "ih":
            return s.cols.ih_hex(d)
        if k == "name":
            return s.name_of(d)
        if k == "category":
            return CATEGORY_LIST[int(C["cat"][d])]
        if k == "health_src":
            return SRC_LIST[int(C["src"][d])]
        if k == "exts":
            return mask_exts(int(C["exts"][d]))
        if k == "private":
            return bool(C["flags"][d] & F_PRIVATE)
        if k == "hh":
            return s.hist.get(d)
        if k == "hd":
            return s.hdf.get(d)
        if k == "_sb":
            return size_bucket(int(C["size"][d]))
        raise KeyError(k)

    def __setitem__(self, k, v):
        s, d = self._s, self.d
        C = s.cols.c
        if k in self._NUM:
            C[k][d] = max(int(v), 0)
        elif k == "health_src":
            C["src"][d] = SRC_IX.get(v, SRC_IX["swarm"])
        elif k == "hh":
            s.hist.put(d, v or [])
        else:
            raise KeyError(f"{k} cannot be set")
        if k in ("hh", "health_src", "seeders", "peers"):
            s._refresh_flags(d)

    def __contains__(self, k):
        try:
            self[k]
            return True
        except KeyError:
            return False

    def keys(self):
        return [k for k in ("ih", "name", "size", "file_count", "seeders", "peers", "health_at", "health_src", "hh", "last_seen",
                            "indexed_at", "created", "category", "exts", "private", "checked_at", "seed_ok_at") if k in self]

    def __repr__(self):
        return f"RecView({self['ih']})"


class TorrentsView:
    """store.torrents for the code of 3.x: `ih in store.torrents`, store.torrents.get(ih) -> RecView, len(), iteration."""

    def __init__(self, store):
        self._s = store

    def _doc(self, ih):
        if not isinstance(ih, str) or len(ih) != 40:
            return -1
        try:
            b = bytes.fromhex(ih)
        except ValueError:
            return -1
        return self._s.cols.find(b)

    def __contains__(self, ih):
        return self._doc(ih) >= 0

    def get(self, ih, default=None):
        d = self._doc(ih)
        return RecView(self._s, d) if d >= 0 else default

    def __getitem__(self, ih):
        d = self._doc(ih)
        if d < 0:
            raise KeyError(ih)
        return RecView(self._s, d)

    def __len__(self):
        return self._s.live_count

    def __iter__(self):
        c = self._s.cols
        for d in c.live().tolist():
            yield c.ih_hex(d)

    def keys(self):
        return list(self)

    def values(self):
        return [RecView(self._s, d) for d in self._s.cols.live().tolist()]

    def items(self):
        c = self._s.cols
        return [(c.ih_hex(d), RecView(self._s, d)) for d in c.live().tolist()]


class Store:
    def __init__(self, data_dir="data", peers_cfg=None, index_background=True, indexing=True):
        """indexing=False (offline tools: import, export): documents are not indexed here; the service indexes them
        in the background when it starts."""
        self.dir = data_dir
        os.makedirs(self.dir, exist_ok=True)
        self.lock = threading.RLock()
        self.stats = Stats(self.dir)
        self.counters = self.stats.counters              # same object: counters are persistent
        self.sources = self.stats.sources
        self.first_start = self.stats.life["first_start"]

        self.nodes = set(load_json(os.path.join(self.dir, "nodes.json"), []))
        self._dirty = {"nodes": False}
        self._last_flush = {"nodes": 0.0}
        self._load_hashes()
        self._english_blocklist_header()
        self.blocklist = self._load_blocklist()

        self.meta = MetaLog(os.path.join(self.dir, "meta.log"))
        self.names = Names(os.path.join(self.dir, "names.dat"))
        self.health = HealthFile(os.path.join(self.dir, "health.bin"))
        self.hist, self.hdf = History(self.health), Breakdowns(self.health)
        self.torrents = TorrentsView(self)
        self.legacy_converted = 0
        self.n_ready = 0                                   # documents below this are complete (cols.n grows first)
        self.ext_counter = Counter()
        self.live_count = 0
        self._last_checkpoint = time.time()
        self.wal = None
        self.rules = RuleBook(self.dir)
        self.hidden = {}                                   # ih -> (rule id, …)   (excluded from public search)
        self._hidden_docs = (None, np.zeros(0, np.int64))
        self.ai_policy = None                              # (threshold %, category mask) while the AI is on (aimod.py)
        self._ai_ver = 0
        self._ai_cache = (-1, np.zeros(0, np.int64))
        self._union_cache = (None, -1, None)
        self._load_state()
        self.queue_max = QUEUE_MAX
        self._analytics_cache = (0, None)
        self._due_cache, self._due_at = [], 0.0
        self._rebuild_queues()

        # ---- index of names / file names / extensions (on disk); missing documents are indexed in the background
        self.index = SegIndex(os.path.join(self.dir, "index"), background=index_background)
        self._ix_lock = threading.Lock()
        self._ix_cond = threading.Condition()
        self._ix_stop = False
        self._ix_thread = threading.Thread(target=self._index_loop, name="indexer", daemon=True) if indexing else None
        if self._ix_thread:
            self._ix_thread.start()
        old = os.path.join(self.dir, "files.idx")              # 3.1 SQLite index: replaced by index/
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(old + ext):
                os.remove(old + ext)

        # ---- admin panel: hiding and peers
        self._recompute_lock = threading.Lock()
        self.hidden_info = {"at": 0, "took_s": 0.0, "scanned": 0}
        self.peers = PeerStore(self.dir, **(peers_cfg or {}))
        self._hidden_ready = not self.rules.active()        # no rules: "nothing hidden" is already correct
        self._hidden_sig = self._rules_sig()
        self._forced, self._forced_set = deque(), set()   # refreshes requested from the panel (peers "now")
        if self.rules.active():
            r = self._load_hidden() or self.recompute_hidden()
            self._hidden_ready = True
            print(f"[store] hiding rules: {r['hidden']} torrents hidden ({r['took_s']} s{', saved list' if r.get('cached') else ''})")
        if self._blocklist_changed:
            threading.Thread(target=self.purge_blocklist, name="blocklist", daemon=True).start()

    # ================================================================== loading
    def _load_blocklist(self):
        path = os.path.join(self.dir, "blocklist.txt")
        pats, src = [], []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        src.append(line)
                        pats.append(re.compile(line, re.I))
        except FileNotFoundError:
            with open(path, "w", encoding="utf-8") as f:
                f.write(BLOCKLIST_HEADER)
        except re.error as e:
            print(f"[store] blocklist.txt has an invalid regex: {e}")
        self.blocklist_sig = hashlib.sha1("\n".join(src).encode()).hexdigest() if src else ""
        return pats

    def _english_blocklist_header(self):
        path = os.path.join(self.dir, "blocklist.txt")
        try:
            with open(path, encoding="utf-8") as f:
                txt = f.read()
        except FileNotFoundError:
            return
        if LEGACY_BLOCKLIST_HEADER in txt:
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                f.write(txt.replace(LEGACY_BLOCKLIST_HEADER, BLOCKLIST_HEADER))
            os.replace(path + ".tmp", path)

    def _blocked(self, name, files):
        if not self.blocklist:
            return False
        hay = name + "\n" + "\n".join((p[0] if isinstance(p, (list, tuple)) else p) for p in files[:200])
        return any(p.search(hay) for p in self.blocklist)

    def _load_state(self):
        """Columns from the checkpoint + what happened after it (metadata log tail, health WAL). First start after a
        3.x version: converts torrents.jsonl."""
        t0 = time.time()
        sp = os.path.join(self.dir, "state.bin")
        cols = hdr = None
        for p in (sp, sp + ".prev"):
            if os.path.exists(p):
                try:
                    cols, hdr = Columns.load(p)
                    if hdr.get("meta_size", 0) > self.meta.size or hdr.get("names_size", 0) > self.names.size:
                        raise ValueError("checkpoint newer than the data files")
                    break
                except (OSError, ValueError, KeyError) as e:
                    log.warning("checkpoint %s unusable (%s)", p, e)
                    cols = hdr = None
        wal_files = _WAL.files(self.dir)
        v3 = os.path.join(self.dir, "torrents.jsonl")
        v2 = os.path.join(self.dir, "torrents.json")
        if cols is not None:
            self.cols = cols
            self.ext_counter = Counter(hdr.get("ext_counter") or {})
            self.names.truncate(hdr["names_size"])
            self.blocklist_seen = hdr.get("blocklist", "")
            n0 = cols.n
            for off, rec in self.meta.iterate(start=hdr["meta_size"]):           # torrents added after the checkpoint
                self._ingest(rec, off)
            if self.meta.bad_tail is not None:
                log.warning("meta.log: incomplete record at the end (power cut?): cut")
                self.meta.truncate(self.meta.bad_tail)
            replay = [p for g, p in wal_files if g >= hdr.get("wal_gen", 0)]
            how = f"checkpoint ({n0} torrents) + {cols.n - n0} from the log tail"
            fresh = False
        elif self.meta.size > 8:                                                 # checkpoint lost: rebuild from the log
            self.cols = Columns()
            self.names.truncate(0)
            self.blocklist_seen = None
            for off, rec in self.meta.iterate():
                self._ingest(rec, off)
            if self.meta.bad_tail is not None:
                self.meta.truncate(self.meta.bad_tail)
            self._restore_from_history()
            replay = [p for g, p in wal_files]
            how = "rebuilt from meta.log"
            fresh = True
        else:
            self.cols = Columns()
            self.names.truncate(0)
            self.blocklist_seen = None
            replay = []
            how = "empty"
            fresh = False
            if os.path.exists(v3) or os.path.exists(v2):
                self._migrate_v3(v3 if os.path.exists(v3) else v2)
                how = "converted from 3.x"
                fresh = True
        n_ev = 0
        for p in replay:
            for ev in read_jsonl(p):
                if ev.get("t") == "ar":                                           # AI: re-analyse everything
                    self.cols.c["ai_flags"][: self.cols.n] &= np.uint8(AI_ALLOW | AI_HIDE)
                    continue
                ih = ev.get("ih", "")
                d = self.cols.find(bytes.fromhex(ih)) if len(ih) == 40 else -1
                if d < 0:
                    continue
                if ev.get("t") == "h":
                    self._apply(d, ev["s"], ev["p"], ev["a"], ev["g"], ev.get("n", 1), ev.get("d"))
                    n_ev += 1
                elif ev.get("t") == "c":                                          # checked_at only (deferred refresh)
                    self.cols.c["checked_at"][d] = ev["a"]
                elif ev.get("t") == "a":                                          # AI moderation result
                    C = self.cols.c
                    C["ai_score"][d], C["ai_cats"][d] = ev.get("s", 0), ev.get("c", 0)
                    C["ai_flags"][d] = (C["ai_flags"][d] & (AI_ALLOW | AI_HIDE)) | (ev.get("f", AI_DONE) & (AI_DONE | AI_ERR))
        self.live_count = int(len(self.cols.live()))
        self.n_ready = self.cols.n
        self._load_ai_overrides()
        gen = (max(g for g, _ in wal_files) + 1) if wal_files else ((hdr or {}).get("wal_gen", 0) + 1)
        self.wal = _WAL(self.dir, gen)
        self._blocklist_changed = bool(self.blocklist) and self.blocklist_seen != self.blocklist_sig
        log.info("store: %d torrents (%s, %d health events replayed) in %.1fs", self.live_count, how, n_ev, time.time() - t0)
        self.load_info = {"how": how, "events": n_ev, "seconds": round(time.time() - t0, 2)}
        if fresh or replay:
            self.checkpoint()

    def _restore_from_history(self):
        """Without a checkpoint, the latest measurement of each torrent is taken from health.bin."""
        C = self.cols.c
        for d in range(self.cols.n):
            hh = self.hist.get(d)
            if hh:
                e = hh[-1]
                C["seeders"][d], C["peers"][d], C["health_at"][d] = max(e[1], 0), max(e[2], 0), e[0]
                C["src"][d] = {"s": _SCRAPE, "w": SRC_IX["swarm"], "l": SRC_IX["legacy"], "n": _NONE}.get(e[3], 2)
                dead, k = history_flags(hh)
                C["flags"][d] = (C["flags"][d] & (0xFF ^ F_DEAD)) | (F_DEAD if dead else 0)
                C["unk"][d] = k

    def _ingest(self, rec, off):
        """A metadata record ("n" new torrent / "d" deletion) -> columns. Returns the document id or -1."""
        t = rec.get("t")
        if t == "d":
            ih = rec.get("ih", "")
            d = self.cols.find(bytes.fromhex(ih)) if len(ih) == 40 else -1
            if d >= 0:
                self._delete_doc(d)
            return -1
        if t != "n":
            return -1
        raw = rec["r"]
        ih = str(raw.get("ih", "")).lower()
        if len(ih) != 40:
            return -1
        try:
            ihb = bytes.fromhex(ih)
        except ValueError:
            return -1
        old = self.cols.find(ihb)
        if old >= 0:
            self._delete_doc(old)                             # the same torrent written again: the newest record wins
        files = raw.get("files")
        d = self.cols.add(ihb)
        C = self.cols.c
        name = raw.get("name") or ""
        C["name_off"][d], C["name_len"][d] = self.names.append(name.replace("\n", " "))
        C["on"][d] = off
        C["size"][d] = max(int(raw.get("size") or 0), 0)
        C["file_count"][d] = int(raw["file_count"]) if raw.get("file_count") is not None else len(files or ())
        cat = raw.get("category")
        if cat is None:
            cat = category_of([tuple(x) for x in files or []], name) if files else "Other"
        if cat in LEGACY_CATEGORIES:
            self.legacy_converted += 1
        C["cat"][d] = CAT_IX.get(LEGACY_CATEGORIES.get(cat, cat), CAT_IX["Other"])
        ex = raw.get("exts")
        if ex is None:
            ex = top_exts([tuple(x) for x in files or []])
        C["exts"][d] = ext_mask(x for x in ex if isinstance(x, str))
        indexed = int(raw.get("indexed_at", raw.get("first_seen", 0)) or 0)
        C["indexed_at"][d] = indexed
        C["last_seen"][d] = int(raw.get("last_seen", indexed) or 0)
        C["created"][d] = max(min(int(raw.get("created", 0) or 0), 2 ** 62), -2 ** 62)
        C["seeders"][d] = max(int(raw.get("seeders", 0) or 0), 0)
        C["peers"][d] = max(int(raw.get("peers", 0) or 0), 0)
        C["health_at"][d] = int(raw.get("health_at", indexed) or 0)
        C["checked_at"][d] = int(raw.get("checked_at") or 0)
        C["seed_ok_at"][d] = int(raw.get("seed_ok_at") or 0)
        C["src"][d] = SRC_IX.get(raw.get("health_src") or "legacy", SRC_IX["swarm"])
        C["flags"][d] = F_PRIVATE if raw.get("private") else 0
        C["unk"][d] = 0
        for x in files or ():
            if isinstance(x, (list, tuple)) and x:
                e = ext_of(x[0])
                if e:
                    self.ext_counter[e] += 1
        self.n_ready = self.cols.n                            # every column of d is written: the indexer may read it
        return d

    def _delete_doc(self, d):
        C = self.cols.c
        C["flags"][d] |= F_DELETED            # its table slot stays (find() skips it) until the next table rebuild

    def _migrate_v3(self, path):
        """3.x journal (torrents.jsonl: "n"/"h"/"d" events) or 2.x torrents.json -> meta.log + columns + slots."""
        t0 = time.time()
        log.info("store: converting %s to the 4.x format (once)…", path)

        def events():
            if path.endswith(".jsonl"):
                for _, ev in iter_jsonl_offsets(path):
                    yield ev
            else:
                for ih, raw in load_json(path, {}).items():
                    if isinstance(raw, dict):
                        raw.setdefault("ih", ih)
                        yield {"t": "n", "r": raw}
        n = 0
        for ev in events():
            t = ev.get("t")
            if t == "n":
                raw = dict(ev["r"])
                hh, hd = raw.pop("hh", None), raw.pop("hd", None)
                if self._blocked(raw.get("name", ""), raw.get("files") or []):
                    self.counters["blocked_purged"] += 1
                    continue
                off = self.meta.append({"t": "n", "r": raw})
                d = self._ingest({"t": "n", "r": raw}, off)
                if d >= 0:
                    self.hist.put(d, [list(e) for e in hh or []][-HH_MAX:])
                    self.hdf.put(d, hd) if hd else self.hdf.put(d, None)
                    self._refresh_flags(d)
                    n += 1
            elif t == "h":
                ih = ev.get("ih", "")
                d = self.cols.find(bytes.fromhex(ih)) if len(ih) == 40 else -1
                if d >= 0:
                    self._apply(d, ev["s"], ev["p"], ev["a"], ev["g"], ev.get("n", 1), ev.get("d"))
            elif t == "d":
                ih = ev.get("ih", "")
                if len(ih) == 40 and self.cols.find(bytes.fromhex(ih)) >= 0:
                    self.meta.append({"t": "d", "ih": ih})
                    self._ingest(ev, -1)
        self.meta.sync()
        os.replace(path, path + ".v3")
        self.blocklist_seen = self.blocklist_sig
        log.info("store: %d torrents converted in %.0fs (the old file is kept as %s.v3)", n, time.time() - t0,
                 os.path.basename(path))

    def _rebuild_queues(self):
        # One queue per SOURCE (LIFO each). Which one gets probed is decided by the MEASURED success of each (see next_pending):
        #   peer      announce_peer with IP:port (in production it turned out the WORST: 2.9 %; hence no fixed priority)
        #   prio      seen several times, retries, and failed hashes that show up again
        #   getpeers  someone is LOOKING for it right now (get_peers): active swarm
        #   other     BEP 51 sample seen once
        self.queues = {k: deque() for k in QUEUES}
        self._inq = {k: set() for k in QUEUES}
        self.queue_peer, self.queue_prio = self.queues["peer"], self.queues["prio"]
        self.queue_getpeers, self.queue = self.queues["getpeers"], self.queues["other"]
        self.retry = deque()
        self.bandit = {k: [0.0, 0.0] for k in QUEUES}      # recent [successes, probes] per queue (with decay)
        self.sched = "adaptive"                             # "fixed" = fixed order (tests)
        for ih, h in list(self.hashes.items()):
            if h.get("status") in ("pending", "probing", "retry"):
                h["status"] = "pending"
                if h.get("pe"):
                    self._enqueue(ih, peer=True)
                else:
                    self._enqueue(ih, prio=h.get("hits", 1) >= 2, src=h.get("src"))

    # ================================================================== hashes: snapshot + change log
    def _load_hashes(self):
        self.hashes = {}
        for ih, h in load_json(os.path.join(self.dir, "hashes.json"), {}).items():
            if isinstance(h, dict):
                self.hashes[sys.intern(ih)] = HashState(h)
        hl = os.path.join(self.dir, "hashes.jsonl")
        n = 0
        for ev in read_jsonl(hl):
            n += 1
            ih = ev.get("i")
            if not ih:
                continue
            if ev.get("t") == "s":
                self.hashes[sys.intern(ih)] = HashState(ev.get("h") or {})
            elif ev.get("t") == "x":
                self.hashes.pop(ih, None)
        self._hlog = open(hl, "a", encoding="utf-8")
        self._hlog_lines = n

    def _hl(self, ev):
        self._hlog.write(json.dumps(ev, separators=(",", ":")) + "\n")
        self._hlog_lines += 1

    def _hlog_set(self, ih, h):
        self._hl({"t": "s", "i": ih, "h": dict(h.items())})

    def _hlog_del(self, ih):
        self._hl({"t": "x", "i": ih})

    def _hashes_snapshot(self, everything):
        """hashes.json with the persistent hashes (everything=True on shutdown: pending ones too) and an empty log."""
        with self.lock:
            snap = {k: dict(v.items()) for k, v in self.hashes.items() if everything or v.get("status") in _PERSISTED}
            self._hlog.close()
            atomic_write(os.path.join(self.dir, "hashes.json"), snap)
            self._hlog = open(os.path.join(self.dir, "hashes.jsonl"), "w", encoding="utf-8")
            self._hlog_lines = 0

    # ================================================================== queue
    def _enqueue(self, ih, prio=False, peer=False, src=None):
        """Queues a hash in its source's queue. The most recent is processed first (LIFO): a hash that has been waiting a
        while almost certainly has no swarm any more. When a queue is full its oldest are dropped (unless the hash
        is also waiting in another queue)."""
        name = "peer" if peer else "prio" if prio else "getpeers" if src == "get_peers" else "other"
        inq = self._inq[name]
        if ih in inq:
            return
        inq.add(ih)
        q = self.queues[name]
        q.append(ih)
        while len(q) > self.queue_max:
            old = q.popleft()
            inq.discard(old)
            oh = self.hashes.get(old)
            if oh and oh.get("status") == "pending" and not any(old in s for s in self._inq.values()):
                del self.hashes[old]
                self.counters["stale_dropped"] += 1

    def _note_hash_peer(self, h, peer):
        """Stores (the last 4) IP:port that announced having this torrent."""
        pe = h.setdefault("pe", [])
        ep = [str(peer[0]), int(peer[1])]
        if ep in pe:
            return False
        pe.append(ep)
        if len(pe) > 4:
            del pe[0]
        return True

    def hash_peers(self, ih):
        """Known IP:port that have this torrent (from announce_peer). Used by the crawler to connect directly."""
        with self.lock:
            h = self.hashes.get(ih)
            return [tuple(x) for x in (h.get("pe") or ())] if h else []

    def add_hash(self, ih: str, src: str, prio: bool = False, peer=None) -> bool:
        """Records a seen infohash. Returns True if it is new.
        peer = (ip, port) of whoever announced it (announce_peer): that node HAS the torrent -> its own queue."""
        ih = ih.lower()
        now = int(time.time())
        with self.lock:
            self.counters["hash_events"] += 1
            self.sources[src] += 1
            try:
                d = self.cols.find(bytes.fromhex(ih))
            except ValueError:
                return False
            if d >= 0:
                self.cols.c["last_seen"][d] = now
                return False
            h = self.hashes.get(ih)
            if h:
                h["last_seen"] = now
                h["hits"] = h.get("hits", 1) + 1
                st = h.get("status")
                if peer and self._note_hash_peer(h, peer):
                    if st in ("pending", "retry") or (st == "failed" and now - h.get("failed_at", 0) > RETRY_FAILED_AFTER / 6):
                        # now we know who has it: worth another attempt, and soon
                        if st != "pending":
                            h["status"], h["attempts"] = "pending", min(h.get("attempts", 0), MAX_ATTEMPTS - 1)
                            self._hlog_del(ih)
                        self._enqueue(ih, peer=True)
                        return False
                if st == "failed" and now - h.get("failed_at", 0) > RETRY_FAILED_AFTER:
                    # a "dead" hash that shows up again in the DHT may have come back to life: retry
                    h["status"], h["attempts"] = "pending", 0
                    self._hlog_del(ih)
                    self._enqueue(ih, True)
                elif st == "retry":                     # seen again while waiting: retry right away
                    h["status"] = "pending"
                    self._hlog_del(ih)
                    self._enqueue(ih, True)
                elif st == "pending" and (prio or h["hits"] >= 2):
                    self._enqueue(ih, True)             # announced or seen repeatedly => live swarm: to the front
                return False
            h = HashState()
            h.first_seen = h.last_seen = now
            h.hits, h.src, h.attempts, h.status = 1, sys.intern(src), 0, "pending"
            self.hashes[ih] = h
            self.counters["hashes_new"] += 1
            if peer:
                self._note_hash_peer(h, peer)
                self._enqueue(ih, peer=True)
            else:
                self._enqueue(ih, prio, src=src)
            return True

    def _release_retries(self):
        now = time.time()
        while self.retry and self.retry[0][0] <= now:
            _, ih = self.retry.popleft()
            h = self.hashes.get(ih)
            if h and h.get("status") == "retry":
                h["status"] = "pending"
                self._hlog_del(ih)
                self.counters["retries_released"] += 1
                self._enqueue(ih, True, peer=bool(h.get("pe")))

    def _pick_order(self):
        """Order of the queues for the next probe. Thompson sampling over each queue's recent success: the best one
        gets most probes, but none is left unexplored (if it improves, it shows)."""
        if self.sched == "fixed":
            return QUEUES
        draw = {k: random.betavariate(1 + ok, 1 + max(n - ok, 0)) for k, (ok, n) in self.bandit.items()}
        return sorted(QUEUES, key=lambda k: -draw[k])

    def _bandit_add(self, name, ok):
        b = self.bandit.get(name)
        if b is None:
            return
        b[0] += ok
        b[1] += 0 if ok else 1
        if not ok and sum(x[1] for x in self.bandit.values()) > BANDIT_WINDOW:     # decay: count what is recent
            for x in self.bandit.values():
                x[0] *= 0.5
                x[1] *= 0.5

    def next_pending(self):
        with self.lock:
            self._release_retries()
            for name in self._pick_order():
                q, inq = self.queues[name], self._inq[name]
                while q:
                    ih = q.pop()                # most recent first
                    inq.discard(ih)
                    h = self.hashes.get(ih)
                    if h and h.get("status") == "pending":
                        h["status"] = "probing"
                        h["qk"] = name
                        self.counters["probes_" + name] += 1            # success rate = metadata_ok_X / probes_X
                        self._bandit_add(name, 0)
                        return ih
            return None

    def mark_failed(self, ih):
        with self.lock:
            h = self.hashes.get(ih)
            if not h:
                return
            now = time.time()
            h["attempts"] = h.get("attempts", 0) + 1
            self.counters["probe_timeouts"] += 1
            # seen only once via sampling or get_peers (someone was LOOKING for it): 1 attempt, nearly all are dead swarms;
            # announced (with or without IP:port) or seen several times: up to MAX_ATTEMPTS, spaced RETRY_DELAY apart
            limit = MAX_ATTEMPTS if (h.get("hits", 1) > 1 or h.get("src") == "announce" or h.get("pe")) else 1
            if h["attempts"] >= limit:
                h["status"] = "failed"
                h["failed_at"] = int(now)
            else:
                h["status"] = "retry"
                self.retry.append((now + RETRY_DELAY, ih))
                self.counters["retries_scheduled"] += 1
                if len(self.retry) > RETRY_MAX:
                    _, old = self.retry.popleft()
                    oh = self.hashes.get(old)
                    if oh and oh.get("status") == "retry":
                        oh["status"], oh["failed_at"] = "failed", int(now)
                        self._hlog_set(old, oh)
            self._hlog_set(ih, h)

    # ================================================================== health (columns + slots)
    def _refresh_flags(self, d):
        dead, k = history_flags(self.hist.get(d))
        C = self.cols.c
        C["flags"][d] = (C["flags"][d] & (0xFF ^ F_DEAD)) | (F_DEAD if dead else 0)
        C["unk"][d] = k

    def _apply(self, d, seeders, peers, at, src, nrep=1, detail=None):
        """A health measurement (same rules as 3.x: a worse non-scrape value does not overwrite a recent verified one)."""
        C = self.cols.c
        seeders, peers, at = max(int(seeders), 0), max(int(peers), 0), int(at)
        keep = (src != "scrape" and C["src"][d] == _SCRAPE and at - int(C["health_at"][d]) < 14 * 86400
                and seeders < int(C["seeders"][d]))
        C["checked_at"][d] = max(int(C["checked_at"][d]), at)
        if detail:
            if detail.get("cs"):
                C["seed_ok_at"][d] = at                  # last time we actually CONNECTED to a real seeder
            self.hdf.put(d, detail)
        if keep:
            return
        C["seeders"][d], C["peers"][d], C["health_at"][d] = seeders, peers, at
        C["src"][d] = SRC_IX.get(src, SRC_IX["swarm"])
        entry = [at, seeders, peers, SRC_CODE.get(src, "w"), int(nrep)]
        if detail:
            entry.append(int(detail.get("cs", 0)))
        hh = hh_append(self.hist.get_bytes(d), entry, HH_MAX)
        self.hist.put_bytes(d, hh)
        dead, k = history_flags(unpack_hh(hh))
        C["flags"][d] = (C["flags"][d] & (0xFF ^ F_DEAD)) | (F_DEAD if dead else 0)
        C["unk"][d] = k

    def states(self, docs):
        """Health state code (index into STATES) of these documents, vectorized (= records.health_state)."""
        C = self.cols.c
        src, s, p, fl = C["src"][docs], C["seeders"][docs], C["peers"][docs], C["flags"][docs]
        st = np.full(len(docs), STATE_CODES["unknown"], np.uint8)
        known = src != _NONE
        st[known & (src == _SCRAPE)] = STATE_CODES["quiet"]
        st[known & (src == _SCRAPE) & ((fl & F_DEAD) != 0)] = STATE_CODES["dead"]
        st[known & (p > 0)] = STATE_CODES["weak"]
        st[known & (s > 0)] = STATE_CODES["alive"]
        return st

    def due(self, docs):
        """When each of these documents should be measured again (epoch), vectorized (= records.refresh_due_at)."""
        C = self.cols.c
        st = self.states(docs)
        base = np.maximum(C["health_at"][docs], C["checked_at"][docs]).astype(np.int64)
        iv = np.empty(len(docs), np.int64)
        for name, code in STATE_CODES.items():
            if name != "unknown":
                iv[st == code] = REFRESH_INTERVAL[name]
        unk = st == STATE_CODES["unknown"]
        k = np.minimum(C["unk"][docs].astype(np.int64), 8)
        iv[unk] = np.minimum(UNKNOWN_BASE * (2 ** k[unk]), UNKNOWN_MAX)
        return base + iv

    def _invalidate(self):
        self._analytics_cache = (0, None)
        self.__dict__.pop("_qcache", None)

    # ================================================================== torrents
    def name_of(self, d):
        C = self.cols.c
        return self.names.get(int(C["name_off"][d]), int(C["name_len"][d]))

    def doc(self, ih):
        try:
            return self.cols.find(bytes.fromhex(ih.lower()))
        except (ValueError, AttributeError):
            return -1

    def save_torrent(self, ih, info: dict, seeders=0, peers=0, src="none") -> bool:
        """info: name,size,files[(path,size)],piece_length,created,comment,created_by,private,trackers"""
        ih = ih.lower()
        files = [(p, int(s)) for p, s in info["files"]]
        with self.lock:
            if self.doc(ih) >= 0:
                return False
            if self._blocked(info["name"], files):
                h = self.hashes.pop(ih, None)
                if h is not None and h.get("status") in _PERSISTED:
                    self._hlog_del(ih)
                self.counters["blocked"] += 1
                return False
            now = int(time.time())
            h = self.hashes.pop(ih, None) or {}
            if h and h.get("status") in _PERSISTED:
                self._hlog_del(ih)
            self.counters["metadata_ok_" + h.get("qk", "other")] += 1      # success per queue (compare with probes_*)
            self._bandit_add(h.get("qk"), 1)
            stored = files[:MAX_FILES_STORED]
            raw = {
                "ih": ih, "name": info["name"], "size": int(info["size"]), "file_count": len(files),
                "files": [[p, s] for p, s in stored],
                "piece_length": info.get("piece_length", 0), "created": info.get("created", 0),
                "comment": (info.get("comment") or "")[:2000], "created_by": (info.get("created_by") or "")[:100],
                "private": bool(info.get("private")), "trackers": (info.get("trackers") or [])[:20],
                "seeders": 0, "peers": 0, "health_at": now, "health_src": "none",
                "first_seen": h.get("first_seen", now), "last_seen": now, "indexed_at": now, "src": h.get("src", "?"),
                "category": category_of(files, info["name"]), "exts": top_exts(files),
            }
            off = self.meta.append({"t": "n", "r": raw})
            d = self._ingest({"t": "n", "r": raw}, off)
            self.live_count += 1
            self.hist.put(d, [])
            self.hdf.put(d, None)
            if src != "none":
                self._apply(d, seeders, peers, now, src)
                self.wal.append({"t": "h", "ih": ih, "s": seeders, "p": peers, "a": now, "g": src, "n": 1})
            hits = evaluate(self.rules.active(), info["name"], [p for p, _ in stored])
            if hits:
                self.hidden[ih] = tuple(hits)
                self._hidden_docs = (None, self._hidden_docs[1])
            self.counters["torrents_indexed"] += 1
            self._invalidate()
        with self._ix_cond:
            self._ix_cond.notify()
        return True

    def import_record(self, raw, hh=None, hd=None):
        """Adds a complete torrent record (3.x format: files, health…) as it is (imports, conversions). Returns True if
        it was added (False: already present, invalid or blocked)."""
        ih = str(raw.get("ih", "")).lower()
        with self.lock:
            if len(ih) != 40 or self.doc(ih) >= 0 or self._blocked(raw.get("name", ""), raw.get("files") or []):
                return False
            raw = {k: v for k, v in raw.items() if k not in ("hh", "hd")}
            raw["ih"] = ih
            off = self.meta.append({"t": "n", "r": raw})
            d = self._ingest({"t": "n", "r": raw}, off)
            if d < 0:
                return False
            self.hist.put(d, [list(e) for e in hh or []][-HH_MAX:])
            self.hdf.put(d, hd or None)
            self._refresh_flags(d)
            self.live_count += 1
            hits = evaluate(self.rules.active(), raw.get("name", ""), [x[0] for x in raw.get("files") or []])
            if hits:
                self.hidden[ih] = tuple(hits)
                self._hidden_docs = (None, self._hidden_docs[1])
            self._invalidate()
        with self._ix_cond:
            self._ix_cond.notify()
        return True

    def delete(self, ih):
        """Removes a torrent (a "d" record in meta.log; its data stays on disk but it no longer exists for the store)."""
        with self.lock:
            d = self.doc(ih)
            if d < 0:
                return False
            self.meta.append({"t": "d", "ih": ih.lower()})
            self._delete_doc(d)
            self.live_count -= 1
            self.hidden.pop(ih.lower(), None)
            self._invalidate()
            return True

    def update_health(self, ih, seeders, peers, src="swarm", nrep=1, detail=None, at=None):
        with self.lock:
            d = self.doc(ih)
            if d < 0:
                return
            now = int(at or time.time())
            self._apply(d, seeders, peers, now, src, nrep, detail)
            ev = {"t": "h", "ih": ih, "s": seeders, "p": peers, "a": now, "g": src, "n": nrep}
            if detail:
                ev["d"] = detail
            self.wal.append(ev)

    def cold(self, d):
        """Cold data of a document: the metadata record (files, comment, trackers…) + the latest breakdown (hd)."""
        r = self.meta.read(int(self.cols.c["on"][d])).get("r") or {}
        hd = self.hdf.get(d)
        if hd is not None:
            r["hd"] = hd
        else:
            r.pop("hd", None)
        return r

    def full(self, d, cold=None):
        """The complete record in the 3.x format (API, export)."""
        C = self.cols.c
        r = cold if cold is not None else self.cold(d)
        out = {"ih": self.cols.ih_hex(d), "name": self.name_of(d), "size": int(C["size"][d]), "file_count": int(C["file_count"][d]),
               "files": r.get("files") or [], "piece_length": r.get("piece_length", 0), "created": int(C["created"][d]),
               "comment": r.get("comment", ""), "created_by": r.get("created_by", ""), "private": bool(C["flags"][d] & F_PRIVATE),
               "trackers": r.get("trackers") or [], "seeders": int(C["seeders"][d]), "peers": int(C["peers"][d]),
               "health_at": int(C["health_at"][d]), "health_src": SRC_LIST[int(C["src"][d])], "hh": self.hist.get(d),
               "first_seen": r.get("first_seen", int(C["indexed_at"][d])), "last_seen": int(C["last_seen"][d]),
               "indexed_at": int(C["indexed_at"][d]), "src": r.get("src", "?"), "category": CATEGORY_LIST[int(C["cat"][d])],
               "exts": r["exts"] if isinstance(r.get("exts"), list) else mask_exts(int(C["exts"][d]))}
        if C["checked_at"][d]:
            out["checked_at"] = int(C["checked_at"][d])
        if C["seed_ok_at"][d]:
            out["seed_ok_at"] = int(C["seed_ok_at"][d])
        hd = r.get("hd") if cold is None else self.hdf.get(d)
        if hd is not None:
            out["hd"] = hd
        return out

    def get(self, ih):
        """Full record for the API (hot fields from the columns + cold fields from disk)."""
        with self.lock:
            d = self.doc(ih)
            if d < 0:
                return None
            due = int(self.due(np.array([d]))[0])
            state = STATES[int(self.states(np.array([d]))[0])]
            hidden_by = list(self.hidden.get(ih.lower(), ())) + (["ai"] if self.ai_hides(d) else [])
        out = self.full(d)                                   # disk reads OUTSIDE the lock
        out["magnet"] = magnet_of(out)
        pl = out.get("piece_length") or 0
        out["num_pieces"] = math.ceil(out["size"] / pl) if pl else 0
        out["files_truncated"] = out["file_count"] > len(out.get("files") or ())
        out["state"] = state
        out["next_check_at"] = due
        out["hidden_by"] = hidden_by
        return out

    def cold_views(self, ihs_or_recs):
        """{ih: cold record (files, trackers, hd…)} for a few torrents (admin lists)."""
        out = {}
        for x in ihs_or_recs:
            ih = x if isinstance(x, str) else x["ih"]
            d = self.doc(ih)
            if d >= 0:
                out[ih] = self.cold(d)
        return out

    def iter_cold(self, docs):
        """(doc, metadata record) for many documents: ONE sequential pass over meta.log when there are many."""
        C = self.cols.c
        docs = sorted(int(x) for x in docs)
        if len(docs) < 3000:
            for d in docs:
                yield d, self.meta.read(int(C["on"][d])).get("r") or {}
            return
        want = {int(C["on"][d]): d for d in docs}
        for off, rec in self.meta.iterate(start=min(want)):
            d = want.pop(off, None)
            if d is not None:
                yield d, rec.get("r") or {}
                if not want:
                    break

    # ================================================================== index
    def _index_upto(self, target, max_docs=None):
        """Indexes documents from the index watermark up to `target` (reading meta.log). Returns how many."""
        with self._ix_lock:
            ix = self.index
            d0 = ix.indexed_upto()
            if d0 >= target:
                return 0
            if max_docs:
                target = min(target, d0 + max_docs)
            C = self.cols.c
            done = 0
            docs = [d for d in range(d0, target) if not C["flags"][d] & F_DELETED]
            for d, r in self.iter_cold(docs):
                ix.add_doc(d, doc_keys(r.get("name") or "", r.get("files") or []))
                done += 1
            ix.skip_to(target)
            return done

    def _index_loop(self):
        announced = False
        while not self._ix_stop:
            with self._ix_cond:
                if self.index.indexed_upto() >= self.n_ready and not self._ix_stop:
                    self._ix_cond.wait(5)
            if self._ix_stop:
                return
            backlog = self.n_ready - self.index.indexed_upto()
            if backlog <= 0:
                if announced:
                    log.info("index: up to date (%d torrents)", self.n_ready)
                    announced = False
                continue
            if backlog > 5000 and not announced:
                log.info("index: indexing %d torrents in the background (searches are partial until it finishes)", backlog)
                announced = True
            try:
                self._index_upto(self.n_ready, max_docs=20000)
            except Exception as e:
                log.warning("indexer: %s", e)
                time.sleep(5)

    def index_sync(self, limit=300):
        """Before a search: indexes the backlog itself if it is small. Returns True if the index is complete."""
        backlog = self.n_ready - self.index.indexed_upto()
        if backlog <= 0:
            return True
        if backlog > limit:
            return False
        self._index_upto(self.n_ready)
        return self.index.indexed_upto() >= self.n_ready

    def wait_index(self, timeout=None):
        end = None if timeout is None else time.time() + timeout
        while self.index.indexed_upto() < self.n_ready:
            if end is not None and time.time() > end:
                return False
            with self._ix_cond:
                self._ix_cond.notify()
            time.sleep(0.05)
        return True

    # ================================================================== hiding (admin panel)
    def hidden_docs(self):
        """Sorted document ids hidden from the public: admin rules + AI moderation (each cached until it changes)."""
        key, arr = self._hidden_docs
        h = self.hidden
        if key is not h:
            docs = sorted(d for d in (self.doc(ih) for ih in list(h)) if d >= 0)
            arr = np.array(docs, np.int64)
            self._hidden_docs = (h, arr)
        ai = self.ai_hidden_docs()
        if not len(ai):
            return arr
        if not len(arr):
            return ai
        uc = self._union_cache
        if uc[0] is arr and uc[1] == self._ai_ver:
            return uc[2]
        u = np.union1d(arr, ai)
        self._union_cache = (arr, self._ai_ver, u)
        return u

    def is_hidden(self, ih):
        d = self.doc(ih)
        return ih.lower() in self.hidden or (d >= 0 and self.ai_hides(d))

    def _rule_candidates(self, m):
        """Superset of the documents that CAN match a word rule (from the index). None = everything must be checked."""
        if m.mode != "word":
            return None
        toks = [t for t in m.tokens if t not in _LETTERS]           # the index does not store single letters
        if not toks:
            return None
        if self.n_ready > self.index.indexed_upto():
            return None                                              # index incomplete: check everything

        def inter(field):
            acc = None
            for t in toks:
                p = self.index.postings(field + t.encode())
                acc = p if acc is None else np.intersect1d(acc, p, assume_unique=True)
                if not len(acc):
                    break
            return acc if acc is not None else np.zeros(0, np.uint32)
        parts = []
        if m.scope in ("all", "name"):
            parts.append(inter(b"n"))
        if m.scope in ("all", "files"):
            parts.append(inter(b"f"))
        return np.unique(np.concatenate(parts)).astype(np.int64) if parts else np.zeros(0, np.int64)

    def _rules_sig(self):
        rules = sorted((m.id, m.scope, m.mode, getattr(m, "needle", None) or (m.rx.pattern if m.rx else "")) for m in self.rules.active())
        return hashlib.sha1(json.dumps(rules).encode()).hexdigest()

    def _save_hidden(self, n):
        """hidden.json: which torrents the rules hide, so a restart does not have to scan every file list again."""
        if not getattr(self, "_hidden_ready", False):           # not computed yet (startup): nothing valid to save
            return
        if not self.rules.active() and not os.path.exists(os.path.join(self.dir, "hidden.json")):
            return
        with self.lock:
            snap = {"sig": self._hidden_sig, "n": n, "hidden": {ih: list(v) for ih, v in self.hidden.items()}}
        atomic_write(os.path.join(self.dir, "hidden.json"), snap)

    def _load_hidden(self):
        """Saved list valid for the current rules: load it and evaluate only the torrents added after it."""
        t0 = time.time()
        h = load_json(os.path.join(self.dir, "hidden.json"), None)
        if not isinstance(h, dict) or h.get("sig") != self._rules_sig() or not 0 <= int(h.get("n", -1)) <= self.cols.n:
            return None
        new = {ih: tuple(v) for ih, v in (h.get("hidden") or {}).items() if self.doc(ih) >= 0}
        matchers = self.rules.active()
        tail = np.arange(int(h["n"]), self.cols.n, dtype=np.int64)
        tail = tail[(self.cols.c["flags"][tail] & F_DELETED) == 0]
        for d, r in self.iter_cold(tail):
            hits = evaluate(matchers, r.get("name") or self.name_of(d), [x[0] for x in r.get("files") or []])
            if hits:
                new[self.cols.ih_hex(d)] = tuple(hits)
        self.hidden = new
        self._hidden_sig = h["sig"]
        took = round(time.time() - t0, 2)
        self.hidden_info = {"at": int(time.time()), "took_s": took, "scanned": len(tail)}
        return {"hidden": len(new), "took_s": took, "scanned": len(tail), "cached": True}

    def _rule_scan(self, matchers):
        """(ih, name, paths, seeders) of the torrents to evaluate, reading the files from meta.log."""
        cands = []
        for m in matchers:
            c = self._rule_candidates(m)
            if c is None:
                cands = None
                break
            cands.append(c)
        live = self.cols.live()
        docs = live if cands is None else np.intersect1d(np.unique(np.concatenate(cands)) if cands else np.zeros(0, np.int64), live)
        C = self.cols.c
        for d, r in self.iter_cold(docs):
            yield self.cols.ih_hex(d), r.get("name") or self.name_of(d), [x[0] for x in r.get("files") or []], int(C["seeders"][d])

    def recompute_hidden(self):
        """Recomputes which torrents the active rules hide (outside the lock: the crawler and the web keep going)."""
        with self._recompute_lock:
            t0 = time.time()
            sig = self._rules_sig()
            matchers = self.rules.active()
            n0 = self.cols.n
            new, scanned = {}, 0
            if matchers:
                for ih, name, paths, _ in self._rule_scan(matchers):
                    scanned += 1
                    hits = evaluate(matchers, name, paths)
                    if hits:
                        new[ih] = tuple(hits)
            with self.lock:
                for ih, v in self.hidden.items():         # indexed during the scan: already evaluated by save_torrent
                    if self.doc(ih) >= n0:
                        new[ih] = v
                self.hidden = new
                self._hidden_sig = sig                    # the rules this list was computed with
                self._invalidate()
            took = round(time.time() - t0, 2)
            self.hidden_info = {"at": int(time.time()), "took_s": took, "scanned": scanned}
            self._hidden_ready = True
            return {"hidden": len(new), "took_s": took, "scanned": scanned}

    def preview_rule(self, matcher, limit=20):
        """What a rule would hide (without saving it)."""
        hits, scanned = [], 0
        for ih, name, paths, sd in self._rule_scan([matcher]):
            scanned += 1
            if evaluate([matcher], name, paths):
                hits.append((sd, ih, name))
        hits.sort(reverse=True)
        hid = self.hidden
        return {"count": len(hits), "already_hidden": sum(1 for _, ih, _ in hits if ih in hid),
                "sample": [{"ih": ih, "name": n, "seeders": sd} for sd, ih, n in hits[:limit]], "scanned": scanned}

    def match_details(self, ih, max_files=8):
        """Where each rule matches in a torrent (name / files), with spans to highlight."""
        d = self.doc(ih)
        if d < 0:
            return []
        name = self.name_of(d)
        files = [tuple(x) for x in (self.cold(d).get("files") or [])]
        ms = {m.id: m for m in self.rules.active()}
        hits = evaluate(list(ms.values()), name, [p for p, _ in files], want_files=True, max_files=max_files)
        out = []
        for rid, h in hits.items():
            m = ms[rid]
            out.append({"rule": rid, "in_name": h["name"],
                        "name_spans": m.spans(name) if h["name"] else [],
                        "files": [{"path": files[i][0], "size": files[i][1], "spans": m.spans(files[i][0])} for i in h["files"]],
                        "files_more": len(h["files"]) >= max_files})
        return out

    def purge_blocklist(self):
        """blocklist.txt changed: deletes the indexed torrents that now match it (one pass over meta.log, in background)."""
        removed = 0
        if self.blocklist:
            for off, rec in self.meta.iterate():
                if rec.get("t") != "n":
                    continue
                r = rec["r"]
                if self._blocked(r.get("name", ""), r.get("files") or []):
                    with self.lock:
                        d = self.doc(r.get("ih", ""))
                        if d >= 0 and int(self.cols.c["on"][d]) == off:
                            self.meta.append({"t": "d", "ih": r["ih"]})
                            self._delete_doc(d)
                            self.live_count -= 1
                            removed += 1
        with self.lock:
            self.blocklist_seen = self.blocklist_sig
            self.counters["blocked_purged"] += removed
            self._invalidate()
        if removed:
            print(f"[store] blocklist: removed {removed} already indexed torrents")
        return removed

    # ================================================================== AI moderation (aimod.py)
    def set_ai_policy(self, policy):
        """(threshold %, category bit mask) while the AI is on; None = off (only the admin's manual hides apply)."""
        with self.lock:
            self.ai_policy = policy
            self._ai_ver += 1
            self._invalidate()

    def _ai_mask(self, docs=None):
        C = self.cols.c
        if docs is None:
            n = self.cols.n
            fl, sc, ct = C["ai_flags"][:n], C["ai_score"][:n], C["ai_cats"][:n]
        else:
            fl, sc, ct = C["ai_flags"][docs], C["ai_score"][docs], C["ai_cats"][docs]
        m = (fl & AI_HIDE) != 0
        pol = self.ai_policy
        if pol is not None:
            thr, cmask = pol
            m |= ((fl & AI_DONE) != 0) & (sc >= thr) & ((ct & cmask) != 0)
        m &= (fl & AI_ALLOW) == 0
        if docs is None:
            m &= (C["flags"][: len(m)] & F_DELETED) == 0
        return m

    def ai_hides(self, d):
        return bool(self._ai_mask(np.array([d]))[0])

    def ai_hidden_docs(self):
        ver, arr = self._ai_cache
        if ver != self._ai_ver:
            arr = np.flatnonzero(self._ai_mask()).astype(np.int64)
            self._ai_cache = (self._ai_ver, arr)
        return arr

    def ai_pending(self, limit, min_doc=0):
        """Live documents not analysed yet, newest first."""
        C = self.cols.c
        n = self.n_ready
        lo = min(max(int(min_doc), 0), n)
        todo = ((C["ai_flags"][lo:n] & AI_DONE) == 0) & ((C["flags"][lo:n] & F_DELETED) == 0)
        idx = np.flatnonzero(todo)
        return (idx[-limit:][::-1] + lo).tolist()

    def ai_text(self, d, nfiles):
        """Text the model reads (name + first file names), or None if the torrent no longer exists."""
        from aimod import torrent_text
        if d >= self.cols.n or self.cols.c["flags"][d] & F_DELETED:
            return None
        files = (self.cold(d).get("files") or []) if nfiles else []
        return torrent_text(self.name_of(d), files, nfiles)

    def ai_set(self, d, score, cats, done=True, error=False):
        """Stores an analysis (WAL + columns). Returns True if the torrent is now hidden by the AI."""
        with self.lock:
            C = self.cols.c
            if d >= self.cols.n or C["flags"][d] & F_DELETED:
                return False
            before = self.ai_hides(d)
            f = (int(C["ai_flags"][d]) & (AI_ALLOW | AI_HIDE)) | (AI_DONE if done else 0) | (AI_ERR if error else 0)
            C["ai_score"][d], C["ai_cats"][d], C["ai_flags"][d] = min(max(int(score), 0), 100), int(cats) & 0xFF, f
            self.wal.append({"t": "a", "ih": self.cols.ih_hex(d), "s": int(C["ai_score"][d]), "c": int(C["ai_cats"][d]),
                             "f": f & (AI_DONE | AI_ERR)})
            after = self.ai_hides(d)
            if after != before:
                self._ai_ver += 1
                self._invalidate()
            return after

    def _load_ai_overrides(self):
        """The admin's decisions (show / hide) live in ai_overrides.json too: they survive even a lost checkpoint."""
        ov = load_json(os.path.join(self.dir, "ai_overrides.json"), {})
        C = self.cols.c
        for ih, a in (ov.items() if isinstance(ov, dict) else ()):
            d = self.doc(ih)
            if d >= 0:
                C["ai_flags"][d] = (C["ai_flags"][d] & ~np.uint8(AI_ALLOW | AI_HIDE)) | (AI_ALLOW if a == "allow" else AI_HIDE)
        self._ai_ver += 1

    def ai_override(self, ihs, action):
        """action: allow (show again; the AI never hides it) · hide (by hand) · reset (back to the AI's verdict) ·
        reanalyse (forget the score). Returns how many torrents changed."""
        path = os.path.join(self.dir, "ai_overrides.json")
        n = 0
        with self.lock:
            ov = load_json(path, {})
            ov = ov if isinstance(ov, dict) else {}
            C = self.cols.c
            for ih in ihs:
                ih = str(ih).lower()
                d = self.doc(ih)
                if d < 0:
                    continue
                f = int(C["ai_flags"][d])
                if action == "allow":
                    f, ov[ih] = (f & ~AI_HIDE) | AI_ALLOW, "allow"
                elif action == "hide":
                    f, ov[ih] = (f & ~AI_ALLOW) | AI_HIDE, "hide"
                elif action == "reset":
                    f &= ~(AI_ALLOW | AI_HIDE)
                    ov.pop(ih, None)
                elif action == "reanalyse":
                    f &= ~(AI_DONE | AI_ERR)
                    self.wal.append({"t": "a", "ih": ih, "s": 0, "c": 0, "f": 0})
                else:
                    raise ValueError("unknown action")
                C["ai_flags"][d] = f
                n += 1
            atomic_write(path, ov)
            self._ai_ver += 1
            self._invalidate()
        return n

    def ai_reanalyse_all(self):
        """Forgets every score (keeps the admin's show/hide decisions): everything is analysed again."""
        with self.lock:
            self.cols.c["ai_flags"][: self.cols.n] &= np.uint8(AI_ALLOW | AI_HIDE)
            self.wal.append({"t": "ar"})
            self._ai_ver += 1
            self._invalidate()

    def ai_counts(self):
        C = self.cols.c
        n = self.n_ready
        fl, sc = C["ai_flags"][:n], C["ai_score"][:n]
        live = (C["flags"][:n] & F_DELETED) == 0
        done = live & ((fl & AI_DONE) != 0)
        hid = self._ai_mask()[:n]
        cats = C["ai_cats"][:n][hid]
        from aimod import CAT_KEYS
        return {"analysed": int(done.sum()), "pending": int((live & ~done).sum()), "hidden": int(hid.sum()),
                "allowed": int((live & ((fl & AI_ALLOW) != 0)).sum()), "manual": int((live & ((fl & AI_HIDE) != 0)).sum()),
                "errors": int((done & ((fl & AI_ERR) != 0)).sum()),
                "histogram": np.bincount(np.minimum(sc[done] // 10, 9), minlength=10).tolist(),
                "by_category": {k: int(np.count_nonzero(cats & (1 << i))) for i, k in enumerate(CAT_KEYS)}}

    def ai_list(self, state="hidden", smin=0, smax=100, cat=None, q="", sort="score", page=1, per=20):
        """Analysed torrents for the admin panel. state: hidden · allowed · manual · visible (analysed, not hidden) · all."""
        from aimod import CAT_BIT, CAT_KEYS
        C = self.cols.c
        n = self.n_ready
        fl = C["ai_flags"][:n]
        live = (C["flags"][:n] & F_DELETED) == 0
        hid = self._ai_mask()[:n]
        m = live & (((fl & AI_DONE) != 0) | ((fl & (AI_ALLOW | AI_HIDE)) != 0))
        if state == "hidden":
            m &= hid
        elif state == "allowed":
            m &= (fl & AI_ALLOW) != 0
        elif state == "manual":
            m &= (fl & AI_HIDE) != 0
        elif state == "visible":
            m &= ~hid
        sc = C["ai_score"][:n]
        m &= (sc >= int(smin)) & (sc <= int(smax))
        if cat in CAT_BIT:
            m &= (C["ai_cats"][:n] & CAT_BIT[cat]) != 0
        docs = np.flatnonzero(m)
        truncated = False
        if q:
            ql = q.lower()
            if len(docs) > 200_000:                              # names are on disk: bounded scan, highest scores first
                docs = docs[np.argsort(-sc[docs].astype(np.int64), kind="stable")[:200_000]]
                truncated = True
            docs = np.array([d for d in docs.tolist() if ql in self.name_of(d).lower() or self.cols.ih_hex(d).startswith(ql)],
                            np.int64)
        keyed = {"score": sc, "seeders": C["seeders"], "date": C["indexed_at"], "size": C["size"]}
        if sort == "name":
            docs = np.array(sorted(docs.tolist(), key=lambda d: self.name_of(d).lower()), np.int64)
        else:
            k = keyed.get(sort, sc)[docs].astype(np.int64)
            docs = docs[np.lexsort((-C["seeders"][docs].astype(np.int64), -k))]
        total = len(docs)
        out = []
        for d in docs[(page - 1) * per: page * per].tolist():
            f = int(C["ai_flags"][d])
            out.append({"ih": self.cols.ih_hex(d), "name": self.name_of(d), "score": int(sc[d]),
                        "cats": [k for i, k in enumerate(CAT_KEYS) if C["ai_cats"][d] >> i & 1],
                        "state": "allowed" if f & AI_ALLOW else "manual" if f & AI_HIDE else "hidden" if hid[d] else "visible",
                        "error": bool(f & AI_ERR), "analysed": bool(f & AI_DONE),
                        "seeders": int(C["seeders"][d]), "peers": int(C["peers"][d]), "size": int(C["size"][d]),
                        "category": CATEGORY_LIST[int(C["cat"][d])], "indexed_at": int(C["indexed_at"][d]),
                        "verified": SRC_LIST[int(C["src"][d])] == "scrape"})
        return {"total": total, "page": page, "per_page": per, "pages": max(1, -(-total // per)), "results": out,
                "truncated": truncated}

    # ================================================================== refresh schedule
    def _due_refill(self, now):
        live = self.cols.live()
        if not len(live):
            self._due_cache, self._due_at = [], now
            return
        due = self.due(live)
        ok = np.flatnonzero(due <= now)
        if len(ok) > 512:
            ok = ok[np.argpartition(due[ok], 512)[:512]]
        order = ok[np.argsort(-due[ok], kind="stable")]               # pop() takes the most overdue first
        self._due_cache = live[order].tolist()
        self._due_at = now

    def next_refresh(self):
        """Torrent whose due time has passed (the most overdue first). Each state has its own frequency: alive 6 h,
        weak 12 h, dead 3 days, not measured 30 min with exponential backoff."""
        with self.lock:
            now = time.time()
            for _ in range(2):
                while self._due_cache:
                    d = self._due_cache.pop()
                    if not self.cols.c["flags"][d] & F_DELETED and self.due(np.array([d]))[0] <= now:
                        return self.cols.ih_hex(d)
                if now - self._due_at < 5:
                    return None
                self._due_refill(now)
            return None

    def refresh_due(self):
        with self.lock:
            now = time.time()
            if not self._due_cache and now - self._due_at >= 5:
                self._due_refill(now)
            return bool(self._due_cache)

    def defer_refresh(self, ih):
        """A refresh could not even start: postpone it so it is not retried in a loop."""
        with self.lock:
            d = self.doc(ih)
            if d >= 0:
                now = int(time.time())
                self.cols.c["checked_at"][d] = now
                self.wal.append({"t": "c", "ih": ih, "a": now})

    def request_refresh(self, ihs):
        """Priority refresh (health + peers) requested from the panel. Returns how many were queued."""
        n = 0
        with self.lock:
            for ih in ihs:
                if len(self._forced) >= FORCED_MAX:
                    break
                if ih in self.torrents and ih not in self._forced_set:
                    self._forced.append(ih)
                    self._forced_set.add(ih)
                    n += 1
        return n

    def next_forced(self):
        with self.lock:
            while self._forced:
                ih = self._forced.popleft()
                self._forced_set.discard(ih)
                if ih in self.torrents:
                    return ih
            return None

    def forced_pending(self):
        return len(self._forced)

    # ================================================================== nodes/peers
    def note_node(self, addr: str):
        self.stats.note_node(addr)
        with self.lock:
            if addr not in self.nodes and len(self.nodes) < MAX_NODES_PERSISTED:
                self.nodes.add(addr)
                self._dirty["nodes"] = True

    def note_peer(self, ip: str):
        self.stats.note_peer(ip)

    # ================================================================== search
    def search(self, params: dict):
        return _search.run(self, params)

    def suggest(self, prefix: str, n=8):
        return _search.suggest(self, prefix, n)

    def related(self, ih: str, n=8):
        return _search.related(self, ih, n)

    # ================================================================== analytics
    def analytics(self):
        now = time.time()
        cached_at, cached = self._analytics_cache
        if cached and now - cached_at < 8:
            return cached
        with self.lock:
            st_count = Counter(h.get("status") for h in self.hashes.values())
            n = self.cols.n
            hid = self.hidden
            hidden_docs = self.hidden_docs()
        C = self.cols.c
        live = self.cols.live(n)
        ver = C["src"][live] == _SCRAPE
        sd = C["seeders"][live]
        states = np.bincount(self.states(live), minlength=len(STATES))
        seed_b = np.bincount(np.searchsorted(_SEED_EDGES, sd[ver], side="left"), minlength=len(SEED_BUCKETS))
        age = now - C["indexed_at"][live].astype(np.float64)
        age_edges = np.array([lim for _, lim in AGE_BUCKETS[:-1]], np.float64)
        age_b = np.bincount(np.searchsorted(age_edges, age, side="right"), minlength=len(AGE_BUCKETS))
        size = C["size"][live]
        size_b = np.bincount(np.searchsorted(_SIZE_EDGES, size.astype(np.float64), side="right"), minlength=len(SIZE_BUCKETS))
        cats = np.bincount(C["cat"][live], minlength=len(CATEGORY_LIST))
        cat_bytes = np.bincount(C["cat"][live], weights=size.astype(np.float64), minlength=len(CATEGORY_LIST))
        cand = live[ver]
        if len(hidden_docs):
            cand = np.setdiff1d(cand, hidden_docs, assume_unique=True)
        top = cand[np.argsort(-C["seeders"][cand].astype(np.int64), kind="stable")[:8]] if len(cand) else []
        c = self.counters
        data = {
            "torrents": int(len(live)), "files": int(C["file_count"][live].sum()), "bytes_indexed": int(size.sum()),
            "hashes_known": int(len(live)) + len(self.hashes),
            "hashes_discovered": c.get("hashes_new", 0), "hashes_dropped": c.get("stale_dropped", 0),
            "hashes_pending": st_count.get("pending", 0) + st_count.get("probing", 0),
            "hashes_retry": st_count.get("retry", 0), "hashes_failed": st_count.get("failed", 0),
            "seeders_sum": int(sd[ver].sum()), "peers_sum": int(C["peers"][live][ver].sum()),
            "torrents_no_seeds": int((sd[ver] == 0).sum()),
            "health_verified": int(ver.sum()), "health_unverified": int(len(live) - ver.sum()),
            "health_states": [{"name": k, "count": int(states[i])} for i, k in enumerate(STATES)],
            "seed_buckets": [{"name": label, "count": int(seed_b[i])} for i, (label, _) in enumerate(SEED_BUCKETS)],
            "top_seeded": [{"ih": self.cols.ih_hex(d), "name": self.name_of(d), "seeders": int(C["seeders"][d]),
                            "peers": int(C["peers"][d]), "size": int(C["size"][d]), "category": CATEGORY_LIST[int(C["cat"][d])]}
                           for d in top],
            "categories": sorted(({"name": CATEGORY_LIST[i], "count": int(cats[i]), "bytes": int(cat_bytes[i])}
                                  for i in range(len(CATEGORY_LIST)) if cats[i]), key=lambda x: -x["count"]),
            "extensions": [{"name": k, "count": v} for k, v in self.ext_counter.most_common(12)],
            "size_buckets": [{"name": label, "count": int(size_b[i])} for i, (label, _) in enumerate(SIZE_BUCKETS)],
            "age_buckets": [{"name": label, "count": int(age_b[i])} for i, (label, _) in enumerate(AGE_BUCKETS)],
            "sources": [{"name": k, "count": v} for k, v in self.sources.most_common()],
            "counters": dict(c), "first_start": self.first_start, "journal_lines": self.wal.size,
            "hidden": len(hid),
            "storage": self.storage_stats(),
        }
        snap = self.stats.snapshot()
        data["life"] = snap
        data["nodes_seen"], data["peers_seen"] = snap["nodes_unique"], snap["peers_unique"]
        data["file_index"] = {"docs": self.index.indexed_upto(), "backlog": max(self.cols.n - self.index.indexed_upto(), 0),
                              "bytes": self.index.stats()["bytes"]}
        self._analytics_cache = (now, data)
        return data

    def storage_stats(self):
        def size(p):
            try:
                return os.path.getsize(os.path.join(self.dir, p))
            except OSError:
                return 0
        ix = self.index.stats() if hasattr(self, "index") else {"bytes": 0, "segments": 0}
        return {"meta_log": size("meta.log"), "names": size("names.dat"), "state": size("state.bin"),
                "health": size("health.bin"), "wal": self.wal.size if self.wal else 0,
                "index": ix["bytes"], "index_segments": ix["segments"]}

    def record_point(self, point: dict):
        self.stats.record_point(point)

    def get_history(self, seconds=3600):
        return self.stats.get_history(seconds)

    # ================================================================== maintenance / persistence
    def maintenance(self):
        """Purges old failed hashes, checkpoints when due, trims the history. Returns a summary."""
        now = time.time()
        res = {"failed_purged": 0, "checkpoint": False}
        with self.lock:
            def last(h):
                return max(h.get("failed_at", 0), h.get("last_seen", 0))
            failed = [(last(h), ih) for ih, h in self.hashes.items() if h.get("status") == "failed"]
            drop = [ih for t, ih in failed if t < now - FAILED_TTL]
            dset = set(drop)
            keep = sorted(x for x in failed if x[1] not in dset)
            if len(keep) > FAILED_MAX:
                drop += [ih for _, ih in keep[: len(keep) - FAILED_MAX]]
            for ih in drop:
                del self.hashes[ih]
                self._hlog_del(ih)
            if drop:
                self.counters["failed_purged"] += len(drop)
            res["failed_purged"] = len(drop)
            persisted = sum(1 for h in self.hashes.values() if h.get("status") in _PERSISTED)
            compact_hashes = self._hlog_lines > 3 * persisted + 20_000
            due = now - self._last_checkpoint >= CHECKPOINT_EVERY or self.wal.size >= CHECKPOINT_WAL_BYTES
        if compact_hashes:
            self._hashes_snapshot(everything=False)
        if due:
            self.checkpoint()
            res["checkpoint"] = True
        res.update(self.peers.maintenance())
        return res

    def checkpoint(self):
        """Writes the columns (state.bin) and starts a new health WAL; the older WAL files are deleted."""
        t0 = time.time()
        with self.lock:
            self.meta.sync()
            self.names.sync()
            self.health.sync()
            old_gen = self.wal.gen if self.wal else 0
            if self.wal:
                self.wal.close()
            self.wal = _WAL(self.dir, old_gen + 1)
            extra = {"meta_size": self.meta.size, "names_size": self.names.size, "wal_gen": self.wal.gen,
                     "ext_counter": dict(self.ext_counter), "blocklist": self.blocklist_seen or "",
                     "saved_at": int(time.time())}
            cols, n = self.cols, self.cols.n
        size = cols.save(os.path.join(self.dir, "state.bin"), extra, n=n)
        self._save_hidden(n)
        for g, p in _WAL.files(self.dir):
            if g < extra["wal_gen"]:
                try:
                    os.remove(p)
                except OSError:
                    pass
        self._last_checkpoint = time.time()
        log.info("checkpoint: %d torrents, %.1f MB in %.1fs", n, size / 1e6, time.time() - t0)
        return size

    def compact_journal(self):
        """3.x API: there is no journal to compact any more; a checkpoint is the closest thing."""
        self.checkpoint()
        return self.cols.n

    def flush(self, force=False):
        now = time.time()
        snap = None
        with self.lock:
            if self.wal:
                self.wal.flush()
            self._hlog.flush()
            if force or (self._dirty["nodes"] and now - self._last_flush["nodes"] >= FLUSH_MIN_INTERVAL["nodes"]):
                snap = sorted(self.nodes)
                self._dirty["nodes"] = False
                self._last_flush["nodes"] = now
        if snap is not None:                               # disk write happens OUTSIDE the lock
            atomic_write(os.path.join(self.dir, "nodes.json"), snap)
        self.peers.flush()
        self.stats.flush(force)

    def close(self):
        self._ix_stop = True
        with self._ix_cond:
            self._ix_cond.notify_all()
        if self._ix_thread:
            self._ix_thread.join(timeout=60)
            if 0 < self.n_ready - self.index.indexed_upto() <= 20_000:   # the last few: indexed now (a big backlog
                self._index_upto(self.n_ready)                             # continues on the next start)
        self.flush(force=True)
        self.index.close(flush=True)
        self._hashes_snapshot(everything=True)
        self._hlog.close()
        self.checkpoint()
        self.wal.close()
        self.stats.close()
        self.meta.close()
        self.names.close()
        self.health.close()
        self.peers.close()

    # ================================================================== export (rollback to 3.x)
    def export_v3(self, path, legacy_categories=False):
        """Writes a 3.x torrents.jsonl ("n" lines with the full record) from the current data."""
        to_es = {v: k for k, v in LEGACY_CATEGORIES.items()}
        n = 0
        with open(path + ".tmp", "w", encoding="utf-8") as out:
            for d, r in self.iter_cold(self.cols.live()):
                rec = self.full(d, r)
                if legacy_categories:
                    rec["category"] = to_es.get(rec["category"], rec["category"])
                out.write(json.dumps({"t": "n", "r": rec}, separators=(",", ":"), ensure_ascii=False) + "\n")
                n += 1
        os.replace(path + ".tmp", path)
        return n
