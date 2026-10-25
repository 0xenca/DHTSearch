"""
Memory diagnostics of the running service (/api/debug/*), to find what makes RSS grow on a live server.

Access: an admin session, or the header  X-Debug-Token: <token>  whose SHA-256 matches TS_DEBUG_TOKEN_SHA256 (or the
built-in hash below when that variable is not set). Only the hash lives in the code; TS_DEBUG_TOKEN_SHA256=off disables
token access. Without access every /api/debug/* URL answers 404, as if it did not exist. Nothing here changes data:
it only reads, apart from starting/stopping tracemalloc (which costs some RAM and CPU while it runs).

  GET /api/debug/memory                    process, glibc heap, Python objects, size of every in-memory structure, libtorrent
  GET /api/debug/memory?types=1            + the 40 most numerous Python object types (walks every object: ~1-2 s)
  GET /api/debug/tracemalloc?action=start&frames=8   starts tracing Python allocations (numpy arrays included)
  GET /api/debug/tracemalloc?action=mark             new baseline for "diff"
  GET /api/debug/tracemalloc?action=diff&top=40      what grew since the baseline (by line, or &by=traceback)
  GET /api/debug/tracemalloc?action=top&top=40       what is allocated now
  GET /api/debug/tracemalloc?action=stop
  GET /api/debug/malloc_info               glibc's per-arena report (XML)

If glibc's in-use heap (mallinfo2 "in_use_mb") grows while Python's traced memory does not, the growth is in C/C++
(libtorrent); if both grow, tracemalloc's diff says which line allocates it.
"""
import ctypes
import gc
import hashlib
import hmac
import os
import sys
import tempfile
import threading
import time
import tracemalloc
from collections import Counter

from flask import Blueprint, Response, abort, jsonify, request

import admin
from memstat import process_memory

bp = Blueprint("debug", __name__, url_prefix="/api/debug")
_TOKEN_SHA256 = "e31f0f47a50352479f63450fc215a4c4fb0c787e63e1157cd741463ca1b59420"
_CFG = {"state": None}
_TM = {"base": None, "base_at": 0, "lock": threading.Lock()}
MB = 1 << 20


def init(app, state):
    """state: the app's STATE dict (store, crawler, ai)."""
    _CFG["state"] = state
    app.register_blueprint(bp)


def _token_ok():
    want = os.environ.get("TS_DEBUG_TOKEN_SHA256", _TOKEN_SHA256).strip().lower()
    got = request.headers.get("X-Debug-Token", "")
    if not got or want in ("", "off", "0", "no"):
        return False
    return hmac.compare_digest(hashlib.sha256(got.encode()).hexdigest(), want)


@bp.before_request
def _guard():
    if not (admin.is_admin() or _token_ok()):
        abort(404)


# ------------------------------------------------------------------ helpers
class _Mallinfo2(ctypes.Structure):
    _fields_ = [(n, ctypes.c_size_t) for n in ("arena", "ordblks", "smblks", "hblks", "hblkhd", "usmblks", "fsmblks",
                                               "uordblks", "fordblks", "keepcost")]


def _libc():
    try:
        return ctypes.CDLL("libc.so.6")
    except OSError:
        return None


def mallinfo():
    """glibc heap: arena = sbrk heap + arenas, in_use = allocated blocks, free = free blocks kept, mmapped = big blocks."""
    libc = _libc()
    if libc is None or not hasattr(libc, "mallinfo2"):
        return None
    libc.mallinfo2.restype = _Mallinfo2
    m = libc.mallinfo2()
    return {"heap_mb": round(m.arena / MB, 1), "in_use_mb": round(m.uordblks / MB, 1), "free_kept_mb": round(m.fordblks / MB, 1),
            "mmapped_mb": round(m.hblkhd / MB, 1), "mmapped_blocks": m.hblks, "free_chunks": m.ordblks,
            "top_releasable_mb": round(m.keepcost / MB, 1)}


def _proc(path, keys=None):
    out = {}
    try:
        with open(path) as f:
            for line in f:
                p = line.split()
                if len(p) >= 2 and (keys is None or p[0].rstrip(":") in keys):
                    out[p[0].rstrip(":")] = int(p[1]) if p[1].isdigit() else p[1]
    except OSError:
        pass
    return out


def _len(obj, *path):
    try:
        for p in path:
            obj = obj[p] if isinstance(p, int) or (isinstance(obj, dict) and p in obj) else getattr(obj, p)
        return len(obj)
    except Exception:
        return None


def structures(st, cr, ai):
    """Size of every container of the service that could grow."""
    out = {}
    if st is not None:
        ix = st.index
        out["store"] = {
            "torrents_cols_n": st.cols.n, "cols_capacity": st.cols.cap, "cols_mb": round(sum(a.nbytes for a in st.cols.c.values()) / MB + st.cols.ih.nbytes / MB, 1),
            "ih_table_mb": round(st.cols.table.nbytes / MB, 1),
            "hashes": len(st.hashes), "hash_status": dict(Counter(h.get("status") for h in list(st.hashes.values()))),
            "queues": {k: len(q) for k, q in st.queues.items()}, "queue_sets": {k: len(s) for k, s in st._inq.items()},
            "retry": len(st.retry), "forced": len(st._forced), "due_cache": len(st._due_cache),
            "hidden": len(st.hidden), "nodes": len(st.nodes), "ext_counter": len(st.ext_counter),
            "qcache": _len(st.__dict__.get("_qcache") or {}), "health_urls": len(st.health.urls.items),
            "health_errs": len(st.health.errs.items), "wal_mb": round((st.wal.size if st.wal else 0) / MB, 1),
            "hashes_log_lines": st._hlog_lines,
        }
        with ix.lock:
            fr = ix.frozen
            out["index"] = {"segments": len(ix.segs), "delta_postings": ix.delta_n, "delta_keys": len(ix.delta),
                            "frozen_keys": len(fr[0]) if fr else 0, "fence_entries": sum(len(s.fence) for s in ix.segs)}
        ps = st.peers
        out["peers"] = {"entries": ps.n, "torrents": len(ps.by_ih), "dirty": len(ps._dirty),
                        "bytes_mb": round(sum(len(b) for b in list(ps.by_ih.values())) / MB, 1), "journal_lines": ps.journal.lines}
        s = st.stats
        out["stats"] = {k: len(v) for k, v in s.tiers.items()}
    if cr is not None:
        jobs = list(getattr(cr, "jobs", {}).values())
        c = {"jobs": len(jobs), "known_nodes": _len(cr, "known_nodes"), "node_heap": _len(cr, "node_heap"),
             "tracker_stats": _len(cr, "tracker_stats"), "alert_types_seen": _len(cr, "_seen_types"),
             "job_dht_ips": sum(len(j.dht_ips) for j in jobs), "job_peer_ips": sum(len(j.peer_ips) for j in jobs)}
        ses = getattr(cr, "ses", None)
        if ses is not None:
            try:
                c["lt_torrents_in_session"] = len(ses.get_torrents())     # > jobs = torrents never removed
            except Exception as e:
                c["lt_torrents_in_session"] = f"error: {e}"
        lv = getattr(cr, "lt_values", None) or {}
        keys = ("ses.num_", "peer.num_", "disk.", "dht.dht_", "net.has_", "sock_bufs.", "picker.")
        c["lt_counters"] = {k: v for k, v in sorted(lv.items()) if v and k.startswith(keys)}
        out["crawler"] = c
    if ai is not None:
        out["ai"] = {"state": ai.st.get("state"), "processed": ai.st.get("processed")}
    return out


# ------------------------------------------------------------------ endpoints
@bp.get("/memory")
def memory():
    S = _CFG["state"]
    t0 = time.time()
    out = {"at": int(t0), "uptime_s": int(t0 - S.get("started", t0)), "pid": os.getpid(),
           "process": process_memory(),
           "status": _proc("/proc/self/status", {"VmRSS", "VmHWM", "VmData", "VmSize", "RssAnon", "RssFile", "RssShmem", "Threads"}),
           "smaps_rollup": _proc("/proc/self/smaps_rollup", {"Rss", "Pss", "Anonymous", "Private_Dirty", "Swap"}),
           "malloc": mallinfo(),
           "malloc_env": {k: os.environ.get(k) for k in ("MALLOC_ARENA_MAX", "MALLOC_MMAP_THRESHOLD_", "MALLOC_TRIM_THRESHOLD_",
                                                          "OPENBLAS_NUM_THREADS")},
           "python": {"allocated_blocks": sys.getallocatedblocks(), "gc_counts": gc.get_count(), "gc_objects": len(gc.get_objects()),
                      "threads": [t.name for t in threading.enumerate()]},
           "tracemalloc": {"tracing": tracemalloc.is_tracing()}}
    if tracemalloc.is_tracing():
        cur, peak = tracemalloc.get_traced_memory()
        out["tracemalloc"].update(traced_mb=round(cur / MB, 1), peak_mb=round(peak / MB, 1),
                                  overhead_mb=round(tracemalloc.get_tracemalloc_memory() / MB, 1),
                                  baseline_age_s=int(t0 - _TM["base_at"]) if _TM["base"] else None)
    try:
        out["structures"] = structures(S.get("store"), S.get("crawler"), S.get("ai"))
    except Exception as e:                                  # never let a diagnostic break on a race
        out["structures_error"] = f"{type(e).__name__}: {e}"
    if request.args.get("types") == "1":
        cnt = Counter(type(o).__name__ for o in gc.get_objects())
        out["python"]["top_types"] = cnt.most_common(40)
    out["took_ms"] = round((time.time() - t0) * 1000)
    return jsonify(out)


def _stats(snap_or_diff, top):
    rows = []
    for s in snap_or_diff[:top]:
        tb = [f"{f.filename.rsplit('/', 1)[-1]}:{f.lineno}" for f in s.traceback]
        r = {"where": " <- ".join(reversed(tb[-6:])) if len(tb) > 1 else tb[0], "size_mb": round(s.size / MB, 2), "count": s.count}
        if hasattr(s, "size_diff"):
            r.update(diff_mb=round(s.size_diff / MB, 2), count_diff=s.count_diff)
        rows.append(r)
    return rows


@bp.get("/tracemalloc")
def tm():
    a = request.args.get("action", "status")
    top = min(max(int(request.args.get("top", 40)), 1), 200)
    by = "traceback" if request.args.get("by") == "traceback" else "lineno"
    with _TM["lock"]:
        if a == "start":
            if not tracemalloc.is_tracing():
                tracemalloc.start(min(max(int(request.args.get("frames", 8)), 1), 30))
            _TM["base"], _TM["base_at"] = tracemalloc.take_snapshot(), time.time()
            return jsonify({"tracing": True, "frames": tracemalloc.get_traceback_limit()})
        if a == "stop":
            tracemalloc.stop()
            _TM["base"] = None
            return jsonify({"tracing": False})
        if not tracemalloc.is_tracing():
            return jsonify({"tracing": False, "hint": "action=start first"}), 409
        snap = tracemalloc.take_snapshot().filter_traces((tracemalloc.Filter(False, tracemalloc.__file__),))
        if a == "mark":
            _TM["base"], _TM["base_at"] = snap, time.time()
            return jsonify({"tracing": True, "marked": True})
        if a == "diff" and _TM["base"] is not None:
            diff = snap.compare_to(_TM["base"], by)
            return jsonify({"since_s": int(time.time() - _TM["base_at"]), "top": _stats(diff, top)})
        return jsonify({"top": _stats(snap.statistics(by), top)})


@bp.get("/malloc_info")
def malloc_info():
    libc = _libc()
    if libc is None:
        abort(404)
    libc.fopen.restype = ctypes.c_void_p
    libc.fopen.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
    libc.malloc_info.argtypes = [ctypes.c_int, ctypes.c_void_p]
    libc.fclose.argtypes = [ctypes.c_void_p]
    fd, path = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    try:
        fp = libc.fopen(path.encode(), b"w")
        libc.malloc_info(0, fp)
        libc.fclose(fp)
        with open(path) as f:
            return Response(f.read(), mimetype="application/xml")
    finally:
        os.remove(path)
