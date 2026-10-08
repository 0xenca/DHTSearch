"""JSON I/O: atomic writes, tolerant loading and a JSON Lines journal with compaction."""
import json
import os
import threading
import time


def atomic_write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:  # corrupted file: moved aside instead of being lost
        bad = f"{path}.corrupt-{int(time.time())}"
        try:
            os.replace(path, bad)
        except OSError:
            pass
        print(f"[store] {path} unreadable ({e}); moved to {bad}")
        return default


def read_jsonl(path):
    """Iterates the objects of a .jsonl file. Skips corrupted lines (e.g. the last one after a power cut)."""
    bad = 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    bad += 1
    except FileNotFoundError:
        return
    if bad:
        print(f"[store] {path}: {bad} unreadable lines skipped")


def iter_jsonl_offsets(path):
    """(byte offset, object) of each line of a .jsonl file; corrupted lines are skipped (e.g. the last one after a power cut)."""
    bad = 0
    try:
        with open(path, "rb") as f:
            off = 0
            for line in f:
                n = len(line)
                s = line.strip()
                if s:
                    try:
                        yield off, json.loads(s)
                    except ValueError:
                        bad += 1
                off += n
    except FileNotFoundError:
        return
    if bad:
        print(f"[store] {path}: {bad} unreadable lines skipped")


class LineReader:
    """Read-only handle on ONE version of a journal file, to read single lines by byte offset (os.pread: no shared
    file position, so any thread can use it). Offsets are only valid for the version they came from: compaction swaps
    the file and the journal gets a new reader, while an old reader keeps reading the old (already replaced) file until
    nobody references it any more. So code that took offsets and a reader together always reads consistent data."""
    __slots__ = ("fd", "__weakref__")

    def __init__(self, path):
        self.fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))

    def line(self, off):
        chunks, pos = [], off
        size = 16384
        while True:
            b = os.pread(self.fd, size, pos)
            if not b:
                break
            i = b.find(b"\n")
            if i >= 0:
                chunks.append(b[:i])
                break
            chunks.append(b)
            pos += len(b)
            size = 262144                          # a torrent with thousands of files: long line
        return b"".join(chunks)

    def obj(self, off):
        return json.loads(self.line(off))

    def lines(self, chunk=1 << 20):
        """(offset, line) of every line, in order. Reads with pread and its OWN position, so any number of these scans
        (and single-line reads) can run at the same time on the same reader."""
        pos, start, buf = 0, 0, b""
        while True:
            b = os.pread(self.fd, chunk, pos)
            if not b:
                break
            pos += len(b)
            buf = buf + b if buf else b
            i = 0
            while True:
                j = buf.find(b"\n", i)
                if j < 0:
                    break
                yield start, buf[i:j + 1]
                start += j + 1 - i
                i = j + 1
            buf = buf[i:]
        if buf:
            yield start, buf

    def __del__(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


class Journal:
    """Append-only JSON Lines file. Writing an event costs O(1), not O(database size); append() returns the byte offset
    of the line, so the data that is not kept in memory can be read back later (see LineReader).
    Compaction rewrites the file from a snapshot without blocking writes: while it runs, new lines are also kept in a
    buffer and appended to the end of the compacted file; commit_compaction() returns where each buffered line ended up."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.f = None
        self.size = 0
        self.lines = 0
        self.reader = None
        self._buf = None
        self._snap = None

    def open(self):
        if self.f is None:
            self.f = open(self.path, "ab")
            self.size = os.fstat(self.f.fileno()).st_size
        if self.reader is None:
            self.reader = LineReader(self.path)

    @staticmethod
    def encode(obj):
        return (json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")

    def append(self, obj, tag=None):
        """Writes one event; returns its byte offset. `tag` is handed back by commit_compaction if this line was
        written while a compaction was running (so the caller can fix the offset it stored)."""
        line = self.encode(obj)
        with self.lock:
            self.open()
            off = self.size
            self.f.write(line)
            self.f.flush()
            self.size += len(line)
            self.lines += 1
            if self._buf is not None:
                self._buf.append((line, tag))
            return off

    def append_many(self, objs):
        """Several lines with a single write (for large batches: peers)."""
        data = b"".join(self.encode(o) for o in objs)
        if not data:
            return
        with self.lock:
            self.open()
            self.f.write(data)
            self.f.flush()
            self.size += len(data)
            n = data.count(b"\n")
            self.lines += n
            if self._buf is not None:
                self._buf.extend((ln + b"\n", None) for ln in data.split(b"\n")[:-1])

    def flush(self):
        with self.lock:
            if self.f:
                self.f.flush()

    def close(self):
        with self.lock:
            if self.f:
                self.f.close()
                self.f = None
            self.reader = None

    # ---------------------------------------------------------------- compaction
    def begin_compaction(self):
        with self.lock:
            self._buf = []

    def write_snapshot(self, items):
        """items: iterable of (key, line) with the compacted state (line: str or bytes ending in \\n). Written to a
        temporary file WITHOUT any lock. Returns {key: byte offset} (key None = not recorded)."""
        tmp = self.path + ".new"
        offs, pos, n = {}, 0, 0
        with open(tmp, "wb") as out:
            for key, line in items:
                if isinstance(line, str):
                    line = line.encode("utf-8")
                if key is not None:
                    offs[key] = pos
                out.write(line)
                pos += len(line)
                n += 1
            out.flush()
            os.fsync(out.fileno())                  # the bulk is synced here, WITHOUT any lock held
        self._snap = (tmp, pos, n)
        return offs

    def commit_compaction(self):
        """Appends the lines written during the compaction, swaps the files and opens a new reader.
        Returns [(tag, new offset)] for the buffered lines that had a tag, in write order."""
        tmp, pos, n = self._snap
        with self.lock:
            moved = []
            with open(tmp, "ab") as out:
                for line, tag in self._buf or []:
                    if tag is not None:
                        moved.append((tag, pos))
                    out.write(line)
                    pos += len(line)
                    n += 1
                out.flush()
                os.fsync(out.fileno())              # only the short tail written during the compaction is still dirty
            if self.f:
                self.f.close()
                self.f = None
            os.replace(tmp, self.path)
            self._buf, self._snap = None, None
            self.lines = n
            self.reader = None
            self.open()
            return moved

    def finish_compaction(self, snapshot_lines):
        """Compaction in one step for callers that do not keep offsets (peers): snapshot_lines = iterable of lines."""
        self.write_snapshot((None, line) for line in snapshot_lines)
        self.commit_compaction()
        return self.lines

    def abort_compaction(self):
        with self.lock:
            self._buf, self._snap = None, None
        try:
            os.remove(self.path + ".new")
        except OSError:
            pass
