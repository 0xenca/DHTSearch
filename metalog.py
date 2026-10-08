"""
Metadata log (data/meta.log): the IMMUTABLE part of every torrent (name, files, comment, trackers, piece length…),
written ONCE when the torrent is indexed and never rewritten.

Format: an 8-byte magic, then records of  [length u32][zlib(JSON)].  JSON = {"t": "n", "r": {...}} for a torrent,
{"t": "d", "ih": "..."} for a deletion. zlib (with its own checksum) cuts the size ~4x (file lists compress well): less
disk, fewer bytes written, faster reads. A record cut by a power failure at the end is detected and truncated.

Offsets never change (nothing is rewritten), so the in-memory index only needs one integer per torrent.
"""
import json
import os
import struct
import threading
import zlib

from memstat import drop_cache

MAGIC = b"DHTMETA1"
_LEN = struct.Struct("<I")
MAX_RECORD = 64 << 20                     # sanity limit when reading a length


class MetaLog:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        self.f = open(path, "ab")
        if new:
            self.f.write(MAGIC)
            self.f.flush()
        self.size = os.fstat(self.f.fileno()).st_size
        self.fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        with open(path, "rb") as f:
            if f.read(len(MAGIC)) != MAGIC:
                raise ValueError(f"{path} is not a metadata log")

    # ------------------------------------------------------------ writing
    @staticmethod
    def encode(obj, level=6):
        data = zlib.compress(json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8"), level)
        return _LEN.pack(len(data)) + data

    def append(self, obj):
        """Appends a record; returns its offset."""
        rec = self.encode(obj)
        with self.lock:
            off = self.size
            self.f.write(rec)
            self.f.flush()
            self.size += len(rec)
            return off

    def sync(self):
        with self.lock:
            self.f.flush()
            os.fsync(self.f.fileno())

    def truncate(self, size):
        """Cuts a partial record left by a power failure (or rolls back to a known size)."""
        with self.lock:
            self.f.flush()
            os.truncate(self.path, size)
            self.size = size

    def close(self):
        with self.lock:
            try:
                self.f.flush()
                os.fsync(self.f.fileno())
            except (OSError, ValueError):
                pass
            self.f.close()
        try:
            os.close(self.fd)
        except OSError:
            pass

    # ------------------------------------------------------------ reading
    def read(self, off):
        """The record at `off` (dict). One or two preads + decompression; safe from any thread."""
        head = os.pread(self.fd, 16384, off)
        if len(head) < 4:
            raise ValueError(f"meta.log: no record at {off}")
        n = _LEN.unpack_from(head, 0)[0]
        if n > MAX_RECORD:
            raise ValueError(f"meta.log: bad record length at {off}")
        data = head[4:4 + n]
        if len(data) < n:
            data += os.pread(self.fd, n - len(data), off + 4 + len(data))
        return json.loads(zlib.decompress(data))

    def iterate(self, start=None, end=None, chunk=4 << 20, decode=True):
        """(offset, record) from `start` (default: first record) to `end` (default: current end), sequentially in large
        reads. Stops at a damaged or incomplete record and sets self.bad_tail to its offset (None if the file is clean)."""
        pos = len(MAGIC) if start is None else start
        end = self.size if end is None else end
        self.bad_tail = None
        buf, bpos = b"", pos
        while pos < end:
            need = 4
            if len(buf) - (pos - bpos) < need:
                buf = buf[pos - bpos:] + os.pread(self.fd, chunk, bpos + len(buf))
                bpos = pos
            if len(buf) - (pos - bpos) < 4:
                self.bad_tail = pos
                break
            n = _LEN.unpack_from(buf, pos - bpos)[0]
            if n > MAX_RECORD or pos + 4 + n > end:
                self.bad_tail = pos
                break
            if len(buf) - (pos - bpos) < 4 + n:
                buf = buf[pos - bpos:] + os.pread(self.fd, max(chunk, 4 + n), bpos + len(buf))
                bpos = pos
            data = buf[pos - bpos + 4: pos - bpos + 4 + n]
            try:
                rec = json.loads(zlib.decompress(data)) if decode else data
            except (zlib.error, ValueError):
                self.bad_tail = pos
                break
            yield pos, rec
            pos += 4 + n
        drop_cache(self.fd)                      # a bulk pass: do not keep the whole file as page cache

    def check_tail(self, start):
        """Validates the records from `start` to the end; truncates a damaged/incomplete tail. Returns bytes cut."""
        for _ in self.iterate(start):
            pass
        cut = 0
        if self.bad_tail is not None:
            cut = self.size - self.bad_tail
            self.truncate(self.bad_tail)
        return cut
