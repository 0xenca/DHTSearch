"""
Hot fields of every torrent as COLUMNS (numpy arrays indexed by document id), ~100 B per torrent instead of a Python
object per torrent (~900 B). Search, filters, sorting, facets, analytics and the refresh schedule work on whole columns
(vectorized), which is also much faster than looping over objects.

  ih            20 bytes (binary infohash); lookup by an open-addressing hash table (uint32 slots, ~8 B per torrent)
  numbers       size, file_count, seeders, peers, dates, metadata-log offset…
  small codes   category, health source, flags (private, deleted, "dead confirmed"), backoff counter
  name          in data/names.dat (append-only, read with pread / mmap; not in RAM)

Persistence: a CHECKPOINT (data/state.bin) with every column, written atomically (tmp + fsync + rename) once a day and
on shutdown; whatever happened after it is replayed from the metadata log and the health WAL at startup.
"""
import json
import mmap
import os
import threading

import numpy as np

from memstat import drop_cache

# name, dtype
COLUMNS = (
    ("size", np.uint64), ("on", np.int64), ("name_off", np.uint64),
    ("file_count", np.uint32), ("seeders", np.uint32), ("peers", np.uint32), ("health_at", np.uint32),
    ("checked_at", np.uint32), ("seed_ok_at", np.uint32), ("last_seen", np.uint32), ("indexed_at", np.uint32),
    ("exts", np.uint32), ("created", np.int64),
    ("name_len", np.uint16), ("cat", np.uint8), ("src", np.uint8), ("flags", np.uint8), ("unk", np.uint8),
    ("ai_score", np.uint8), ("ai_cats", np.uint8), ("ai_flags", np.uint8),     # AI moderation (aimod.py): 3 B
)
F_PRIVATE, F_DELETED, F_DEAD = 1, 2, 4
AI_DONE, AI_ALLOW, AI_HIDE, AI_ERR = 1, 2, 4, 8        # analysed · shown by the admin · hidden by the admin · model error

# Extensions as a BITMASK (the "exts" column): one bit per common extension, so the extension facet costs 4 B per torrent
# and never grows (a table of extension combinations reached hundreds of bytes per torrent with millions of torrents).
# Search by ANY extension uses the index (e-keys); the exact list of a torrent is in its metadata record.
EXT_BITS = ("mkv", "mp4", "avi", "mov", "wmv", "m4v", "ts", "webm", "srt", "sub", "ass", "mp3", "flac", "m4a", "wav", "ogg",
            "aac", "jpg", "png", "gif", "pdf", "epub", "mobi", "cbz", "cbr", "txt", "nfo", "iso", "exe", "zip", "rar", "7z")
EXT_BIT = {e: 1 << i for i, e in enumerate(EXT_BITS)}


def ext_mask(exts):
    m = 0
    for e in exts:
        m |= EXT_BIT.get(e, 0)
    return m


def mask_exts(m):
    return [e for i, e in enumerate(EXT_BITS) if m >> i & 1]


CATEGORY_LIST = ["Video", "Audio", "Images", "Documents", "Software", "Archives", "Data", "Other"]
SRC_LIST = ["none", "scrape", "swarm", "legacy"]
STATE_MAGIC = b"DHTSTATE1\n"
# infohash -> table slot: two 8-byte words of the infohash mixed with Fibonacci hashing (real infohashes are random,
# but sequential/synthetic ones must not pile up in one cluster)
_GOLD_I = 0x9E3779B97F4A7C15
_GOLD = np.uint64(_GOLD_I)


class Names:
    """Torrent names, appended to data/names.dat (UTF-8). Read with pread (one) or through an mmap (many)."""

    def __init__(self, path):
        self.path = path
        self.f = open(path, "ab")
        self.size = os.fstat(self.f.fileno()).st_size
        self.fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        self._mm, self._mm_size = None, 0
        self.lock = threading.Lock()

    def append(self, name):
        b = name.encode("utf-8", "replace")[:65535]
        with self.lock:
            off = self.size
            self.f.write(b)
            self.f.flush()
            self.size += len(b)
            return off, len(b)

    def get(self, off, n):
        return os.pread(self.fd, n, off).decode("utf-8", "replace") if n else ""

    def view(self):
        """mmap covering the whole file as it is now (re-created when the file has grown)."""
        with self.lock:
            if self._mm is None or self._mm_size < self.size:
                if self.size == 0:
                    return b""
                self._mm = mmap.mmap(self.fd, 0, access=mmap.ACCESS_READ)
                self._mm_size = len(self._mm)
            return self._mm

    def truncate(self, size):
        with self.lock:
            self.f.flush()
            os.truncate(self.path, size)
            self.size = size
            self._mm, self._mm_size = None, 0

    def sync(self):
        with self.lock:
            self.f.flush()
            os.fsync(self.f.fileno())

    def close(self):
        self.sync()
        self.f.close()


class Columns:
    def __init__(self, cap=1024):
        self.n = 0
        self.cap = 0
        self.c = {}
        self.ih = np.zeros((0, 20), np.uint8)
        self._grow(cap)
        self.tbits = 0
        self.table = None
        self.c.setdefault("flags", np.zeros(cap, np.uint8))
        self._rebuild_table(max(cap * 2, 1024))
        self.lock = threading.RLock()

    # ------------------------------------------------------------ storage
    def _grow(self, cap):
        cap = max(cap, 16)
        for name, dt in COLUMNS:
            old = self.c.get(name)
            a = np.zeros(cap, dt)
            if old is not None:
                a[: self.n] = old[: self.n]
            self.c[name] = a
        ih = np.zeros((cap, 20), np.uint8)
        ih[: self.n] = self.ih[: self.n]
        self.ih = ih
        self.cap = cap

    def __getattr__(self, name):              # cols.seeders -> the array (only for column names)
        c = self.__dict__.get("c")
        if c is not None and name in c:
            return c[name]
        raise AttributeError(name)

    # ------------------------------------------------------------ infohash -> document id
    def _rebuild_table(self, size=None):
        """(Re)builds the infohash table from the live documents, vectorized (linear probing resolved in rounds)."""
        size = size or max(self.n * 4, 1024)
        bits = max(10, (size - 1).bit_length())
        mask = np.uint64((1 << bits) - 1)
        t = np.zeros(1 << bits, np.uint32)
        if self.n:
            docs = np.flatnonzero((self.c["flags"][: self.n] & F_DELETED) == 0)
            ih = self.ih[docs]
            keys = ih[:, :8].copy().view("<u8").ravel() ^ ih[:, 12:20].copy().view("<u8").ravel()
            slots = (keys * _GOLD) >> np.uint64(64 - bits)
            pending = np.arange(len(docs))
            while pending.size:
                s = slots[pending]
                free = t[s] == 0
                cand, cs = pending[free], s[free]
                uniq, first = np.unique(cs, return_index=True)
                win = cand[first]
                t[uniq] = (docs[win] + 1).astype(np.uint32)
                lose = np.setdiff1d(pending, win, assume_unique=True)
                slots[lose] = (slots[lose] + np.uint64(1)) & mask
                pending = lose
        self.tbits, self.table = bits, t

    def _slot(self, ihb):
        k = int.from_bytes(ihb[:8], "little") ^ int.from_bytes(ihb[12:20], "little")
        return ((k * _GOLD_I) & 0xFFFFFFFFFFFFFFFF) >> (64 - self.tbits)

    def find(self, ihb):
        """Document id of a 20-byte infohash, or -1."""
        mask = (1 << self.tbits) - 1
        i = self._slot(ihb)
        t, ih = self.table, self.ih
        while True:
            d = int(t[i])
            if d == 0:
                return -1
            d -= 1
            if ih[d].tobytes() == ihb:                   # one slot per infohash: a deleted one is simply absent
                return -1 if self.c["flags"][d] & F_DELETED else d
            i = (i + 1) & mask

    def _table_put(self, ihb, d):
        mask = (1 << self.tbits) - 1
        i = self._slot(ihb)
        t, ih = self.table, self.ih
        while True:
            cur = int(t[i])
            if cur == 0 or ih[cur - 1].tobytes() == ihb:     # empty, or the same infohash re-added after a deletion
                t[i] = d + 1
                return
            i = (i + 1) & mask

    def add(self, ihb):
        """New document id for an infohash (columns zeroed)."""
        d = self.n
        if d >= self.cap:
            self._grow(self.cap * 2)
        self.ih[d] = np.frombuffer(ihb, np.uint8)
        self.n = d + 1
        if self.n * 2 > (1 << self.tbits):
            self._rebuild_table(self.n * 4)
        else:
            self._table_put(ihb, d)
        return d

    def ih_hex(self, d):
        return self.ih[d].tobytes().hex()

    def live(self, n=None):
        """Document ids that are not deleted (sorted)."""
        n = self.n if n is None else n
        return np.flatnonzero((self.c["flags"][:n] & F_DELETED) == 0).astype(np.int64)

    # ------------------------------------------------------------ checkpoint
    def save(self, path, extra, n=None):
        """Writes every column (rows < n) to `path` atomically (tmp + fsync + rename; the previous one is kept as .prev).
        Runs WITHOUT the store lock: rows changed meanwhile are fixed by replaying the WAL started before it."""
        n = self.n if n is None else n
        c, ih = self.c, self.ih                      # the arrays as they are now (a resize creates new ones)
        hdr = {"version": 2, "n": n, "cols": [], **extra}
        parts, off = [], 0
        for name, dt in COLUMNS:
            b = memoryview(np.ascontiguousarray(c[name][:n]).reshape(-1)).cast("B")
            hdr["cols"].append([name, np.dtype(dt).str, off, len(b)])
            parts.append(b)
            off += len(b)
        b = memoryview(np.ascontiguousarray(ih[:n]).reshape(-1)).cast("B")
        hdr["cols"].append(["ih", "|u1", off, len(b)])
        parts.append(b)
        head = json.dumps(hdr, separators=(",", ":")).encode()
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(STATE_MAGIC)
            f.write(len(head).to_bytes(8, "little"))
            f.write(head)
            for p in parts:
                f.write(p)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(path):
            os.replace(path, path + ".prev")
        os.replace(tmp, path)
        drop_cache(path)
        return 8 + len(head) + off + len(STATE_MAGIC)

    @classmethod
    def load(cls, path):
        """Columns from a checkpoint -> (Columns, extra header fields). Raises ValueError if the file is not valid."""
        with open(path, "rb") as f:
            if f.read(len(STATE_MAGIC)) != STATE_MAGIC:
                raise ValueError("not a state file")
            hl = int.from_bytes(f.read(8), "little")
            hdr = json.loads(f.read(hl))
            base = f.tell()
            n = hdr["n"]
            self = cls(cap=max(n + n // 4, 1024))
            for name, dt, off, ln in hdr["cols"]:
                f.seek(base + off)
                raw = f.read(ln)
                if len(raw) != ln:
                    raise ValueError("truncated state file")
                a = np.frombuffer(raw, np.dtype(dt))
                if name == "ih":
                    self.ih[:n] = a.reshape(n, 20)
                elif name in self.c:
                    self.c[name][:n] = a
            self.n = n
        drop_cache(path)
        if hdr.get("version") != 2:
            raise ValueError("checkpoint of an older 4.0 pre-release: rebuilt from meta.log")
        self._rebuild_table(max(n * 4, 1024))
        return self, hdr
