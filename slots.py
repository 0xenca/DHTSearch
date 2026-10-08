"""
Health data of every torrent in ONE file of fixed 1 KiB slots (data/health.bin), slot = document id:

  [0, 484)     history: u32 count + the last 40 measurements x 12 B (packing._HH)
  [484, 1024)  latest per-tracker breakdown: u16 length + packed breakdown (21 B + 18 B per tracker: up to 28 trackers)

A measurement updates both parts of the SAME slot, and slots are 1 KiB-aligned, so it dirties ONE 4 KiB page: one page
written per measurement, nothing ever rewritten in bulk. Unwritten slots are holes (no disk used).

Tracker URLs and error messages are stored as small ids; the id tables are appended to health.strings (one JSON line
per new string, fsync'ed: a few thousand lines in the life of the install) and loaded back on start.
"""
import json
import os
import struct
import threading

from packing import HH_SIZE, _Table, pack_hd, pack_hh, unpack_hd, unpack_hh

HH_SLOTS = 40                                   # measurements kept per torrent
SLOT = 1024
HIST_OFF, HIST_LEN = 0, 4 + HH_SLOTS * HH_SIZE  # 484
HD_OFF = HIST_LEN
HD_LEN = SLOT - HD_OFF                          # 540
HIST_SLOT, HD_SLOT = HIST_LEN, HD_LEN           # (names used by older tests/tools)

_CNT = struct.Struct("<I")
_HDLEN = struct.Struct("<H")


class HealthFile:
    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o644)
        self.lock = threading.Lock()
        self.closed = False
        self.urls, self.errs = _Table(60_000, "?"), _Table(4_000, "error")
        spath = path.rsplit(".", 1)[0] + ".strings"
        kinds = {"u": self.urls, "e": self.errs}
        try:
            with open(spath, encoding="utf-8") as f:
                for line in f:
                    try:
                        k, i, s = json.loads(line)
                    except ValueError:
                        break                                # torn last line (power cut): the rest was never used
                    t = kinds[k]
                    if i == len(t.items):
                        t.id(s)
        except FileNotFoundError:
            pass
        self._sf = open(spath, "a", encoding="utf-8")
        for k, t in kinds.items():
            t.on_new = (lambda k: lambda i, s: self._new_string(k, i, s))(k)

    def _new_string(self, kind, i, s):
        self._sf.write(json.dumps([kind, i, s], ensure_ascii=False) + "\n")
        self._sf.flush()
        os.fsync(self._sf.fileno())                     # before any slot can reference the id

    def read(self, i, off, n):
        b = os.pread(self.fd, n, i * SLOT + off)
        return b if len(b) == n else b.ljust(n, b"\0")

    def write(self, i, off, data):
        os.pwrite(self.fd, data, i * SLOT + off)

    def sync(self):
        if not self.closed:
            os.fsync(self.fd)

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            os.fsync(self.fd)
            os.close(self.fd)
            self._sf.close()
        except OSError:
            pass


class History:
    """The last 40 health measurements of each torrent."""

    def __init__(self, hf):
        self.hf = hf

    def get_bytes(self, i):
        b = self.hf.read(i, HIST_OFF, HIST_LEN)
        n = min(_CNT.unpack_from(b, 0)[0], HH_SLOTS)
        return b[4:4 + n * HH_SIZE]

    def get(self, i):
        return unpack_hh(self.get_bytes(i))

    def put_bytes(self, i, hh):
        hh = hh[-HH_SLOTS * HH_SIZE:]
        self.hf.write(i, HIST_OFF, _CNT.pack(len(hh) // HH_SIZE) + hh)

    def put(self, i, entries):
        self.put_bytes(i, pack_hh(entries))

    def sync(self):
        self.hf.sync()

    def close(self):
        self.hf.close()


class Breakdowns:
    """The latest per-tracker breakdown of each torrent."""

    def __init__(self, hf):
        self.hf = hf

    def get(self, i):
        b = self.hf.read(i, HD_OFF, HD_LEN)
        n = _HDLEN.unpack_from(b, 0)[0]
        return unpack_hd(b[2:2 + n], self.hf.urls, self.hf.errs) if 0 < n <= HD_LEN - 2 else None

    def put(self, i, hd):
        hf = self.hf
        b = pack_hd(hd, hf.urls, hf.errs) if hd is not None else b""
        while len(b) + 2 > HD_LEN:                    # more trackers than fit: keep the first ones (answered first)
            hd = dict(hd, tr=hd["tr"][:-1])
            b = pack_hd(hd, hf.urls, hf.errs)
        hf.write(i, HD_OFF, _HDLEN.pack(len(b)) + b)

    def sync(self):
        self.hf.sync()

    def close(self):
        self.hf.close()
