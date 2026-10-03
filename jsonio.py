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


class Journal:
    """Append-only JSON Lines file. Writing an event costs O(1), not O(database size).
    Compaction rewrites the file from a snapshot without blocking writes: while it runs, new lines are also kept
    in a buffer and appended to the end of the compacted file."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.f = None
        self.lines = 0
        self._buf = None

    def open(self):
        if self.f is None:
            self.f = open(self.path, "a", encoding="utf-8")

    def append(self, obj):
        line = json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n"
        with self.lock:
            self.open()
            self.f.write(line)
            self.f.flush()
            self.lines += 1
            if self._buf is not None:
                self._buf.append(line)

    def append_many(self, objs):
        """Several lines with a single write (for large batches: peers)."""
        lines = [json.dumps(o, separators=(",", ":"), ensure_ascii=False) + "\n" for o in objs]
        if not lines:
            return
        with self.lock:
            self.open()
            self.f.write("".join(lines))
            self.f.flush()
            self.lines += len(lines)
            if self._buf is not None:
                self._buf.extend(lines)

    def flush(self):
        with self.lock:
            if self.f:
                self.f.flush()

    def close(self):
        with self.lock:
            if self.f:
                self.f.close()
                self.f = None

    def begin_compaction(self):
        with self.lock:
            self._buf = []

    def finish_compaction(self, snapshot_lines):
        """snapshot_lines: iterable of lines (str with \\n) of the already-compacted state."""
        tmp = self.path + ".new"
        n = 0
        with open(tmp, "w", encoding="utf-8") as out:
            for line in snapshot_lines:
                out.write(line)
                n += 1
            with self.lock:                       # atomic finish: append what was written during compaction
                for line in self._buf or []:
                    out.write(line)
                    n += 1
                out.flush()
                if self.f:
                    self.f.close()
                    self.f = None
                os.replace(tmp, self.path)
                self._buf = None
                self.lines = n
                self.open()
        return n

    def abort_compaction(self):
        with self.lock:
            self._buf = None
