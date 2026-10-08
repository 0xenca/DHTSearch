"""
Memory of the process, split the way it matters:

  rss_anon   memory the program itself holds (Python objects, libtorrent): what this project can reduce
  rss_file   pages of files mapped into the process (libraries…)
  cgroup     what systemd / Proxmox (LXC) / Docker show as the service's or container's "memory": it ALSO counts the
             kernel's page cache for the files the service read or wrote (journal, files.idx). That cache is reclaimed
             by the kernel whenever something needs the memory, but it makes the figure look much bigger.
Linux only; anything that cannot be read is left out.
"""
import os

MB = 1 << 20


def _kv(path):
    out = {}
    try:
        with open(path) as f:
            for line in f:
                p = line.split()
                if len(p) >= 2:
                    out[p[0].rstrip(":")] = p[1]
    except OSError:
        pass
    return out


def _read_int(path):
    try:
        with open(path) as f:
            v = f.read().strip()
        return None if v == "max" else int(v)
    except (OSError, ValueError):
        return None


def _cgroup():
    """(version, directory) of this process's memory cgroup, or None."""
    try:
        with open("/proc/self/cgroup") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    for line in lines:                                   # v2: "0::/system.slice/torrent-search.service"
        h, _, rest = line.partition(":")
        ctrl, _, path = rest.partition(":")
        if h == "0" and ctrl == "":
            d = "/sys/fs/cgroup" + path
            if os.path.exists(os.path.join(d, "memory.current")):
                return 2, d
    for line in lines:                                   # v1: "9:memory:/system.slice/torrent-search.service"
        h, _, rest = line.partition(":")
        ctrl, _, path = rest.partition(":")
        if "memory" in ctrl.split(","):
            for d in ("/sys/fs/cgroup/memory" + path, "/sys/fs/cgroup/memory"):
                if os.path.exists(os.path.join(d, "memory.usage_in_bytes")):
                    return 1, d
    return None


def process_memory():
    st = _kv("/proc/self/status")
    kb = lambda k: int(st[k]) // 1024 if k in st else None     # noqa: E731
    out = {"rss_mb": kb("VmRSS"), "rss_anon_mb": kb("RssAnon"), "rss_file_mb": kb("RssFile"), "peak_rss_mb": kb("VmHWM")}
    cg = _cgroup()
    if cg:
        v, d = cg
        if v == 2:
            ms = _kv(os.path.join(d, "memory.stat"))
            cur, lim = _read_int(os.path.join(d, "memory.current")), _read_int(os.path.join(d, "memory.max"))
            anon, file_ = ms.get("anon"), ms.get("file")
        else:
            ms = _kv(os.path.join(d, "memory.stat"))
            cur, lim = _read_int(os.path.join(d, "memory.usage_in_bytes")), _read_int(os.path.join(d, "memory.limit_in_bytes"))
            anon, file_ = ms.get("rss"), ms.get("cache")
        out["cgroup"] = {"total_mb": cur // MB if cur is not None else None,
                         "anon_mb": int(anon) // MB if anon else None,
                         "page_cache_mb": int(file_) // MB if file_ else None,
                         "limit_mb": lim // MB if lim and lim < (1 << 60) else None}
    return out


def drop_cache(path_or_fd):
    """Asks the kernel to drop the (clean) page cache of a file the service has just read or written in bulk (journal at
    startup, compaction, building the file-name index). Only a hint: the data stays on disk and is read again if needed."""
    if not hasattr(os, "posix_fadvise"):
        return False
    try:
        if isinstance(path_or_fd, int):
            os.posix_fadvise(path_or_fd, 0, 0, os.POSIX_FADV_DONTNEED)
            return True
        fd = os.open(path_or_fd, os.O_RDONLY)
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        finally:
            os.close(fd)
        return True
    except OSError:
        return False
