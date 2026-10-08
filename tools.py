"""
Maintenance tools for the data directory (stop the service first for anything that writes).

  python tools.py info       [--data DIR]                 what is on disk and how big (read only)
  python tools.py export-v3  [--data DIR] [--apply]       ROLL BACK to 3.x: writes torrents.jsonl (3.x format) and moves the
                                                          4.x files to DIR/v4-moved-<date>/ (nothing is deleted)
  python tools.py export-v3 --legacy-categories --apply   the same for 2.x (Spanish category names)

Starting 4.x again on the rolled-back directory converts torrents.jsonl again (whatever 3.x indexed meanwhile is kept).
"""
import argparse
import json
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

V4_FILES = ("meta.log", "names.dat", "state.bin", "state.bin.prev", "health.bin", "health.strings", "hashes.jsonl", "hidden.json", "index")


def holders_of(paths):
    """PIDs of other processes that have any of these files open (Linux /proc). Best effort."""
    targets = {os.path.realpath(p) for p in paths if os.path.exists(p)}
    pids = set()
    if not targets or not os.path.isdir("/proc"):
        return pids
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        fd_dir = f"/proc/{pid}/fd"
        try:
            for fd in os.listdir(fd_dir):
                try:
                    if os.readlink(os.path.join(fd_dir, fd)) in targets:
                        pids.add(int(pid))
                        break
                except OSError:
                    pass
        except OSError:                       # other users' processes when not root
            pass
    return pids


def data_in_use(dst):
    return holders_of([os.path.join(dst, n) for n in ("meta.log", "names.dat", "torrents.jsonl", "peers.jsonl", "hashes.jsonl")])


def refuse_if_running(dst):
    pids = data_in_use(dst)
    if pids:
        raise SystemExit(f"{dst} is in use by PID {', '.join(map(str, sorted(pids)))}: stop the service first "
                         f"(systemctl stop torrent-search).")


def v4_paths(dst):
    import glob
    out = [os.path.join(dst, n) for n in V4_FILES if os.path.exists(os.path.join(dst, n))]
    return out + sorted(glob.glob(os.path.join(dst, "health-*.wal")))


def du(path):
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for base, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(base, f))
            except OSError:
                pass
    return total


def info(dst):
    out = {"dir": dst, "format": "4.x" if os.path.exists(os.path.join(dst, "meta.log")) else
           "3.x" if os.path.exists(os.path.join(dst, "torrents.jsonl")) else "2.x" if os.path.exists(os.path.join(dst, "torrents.json"))
           else "empty", "files_mb": {}}
    for name in sorted(os.listdir(dst)) if os.path.isdir(dst) else ():
        out["files_mb"][name] = round(du(os.path.join(dst, name)) / 1e6, 1)
    sp = os.path.join(dst, "state.bin")
    if os.path.exists(sp):
        with open(sp, "rb") as f:
            f.read(10)
            hl = int.from_bytes(f.read(8), "little")
            hdr = json.loads(f.read(hl))
        out["checkpoint"] = {"documents": hdr["n"], "saved_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(hdr.get("saved_at", 0)))}
    return out


def export_v3(dst, apply, legacy_categories=False):
    """Rollback: 3.x journal from the 4.x data, then the 4.x files are moved aside (so a later 4.x start converts the
    3.x journal again instead of ignoring it)."""
    if not os.path.exists(os.path.join(dst, "meta.log")):
        raise SystemExit(f"{dst} has no 4.x data (meta.log)")
    rep = {"dir": dst, "applied": apply, "legacy_categories": legacy_categories, "move": [os.path.basename(p) for p in v4_paths(dst)]}
    jl = os.path.join(dst, "torrents.jsonl")
    if os.path.exists(jl):
        rep["existing_torrents_jsonl"] = "renamed to torrents.jsonl.before-export"
    if not apply:
        return rep
    from store import BLOCKLIST_HEADER, LEGACY_BLOCKLIST_HEADER, Store
    if os.path.exists(jl):
        os.replace(jl, jl + ".before-export")
    v3 = jl + ".v3"                                         # left by the 3.x -> 4.x conversion: not needed any more
    if os.path.exists(v3):
        os.replace(v3, v3 + ".old")
    st = Store(dst, index_background=False, indexing=False)
    try:
        rep["torrents"] = st.export_v3(jl, legacy_categories=legacy_categories)
    finally:
        st.close()                                          # hashes.json is written complete (pending ones too)
    bdir = os.path.join(dst, "v4-moved-" + time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(bdir)
    for p in v4_paths(dst):
        shutil.move(p, os.path.join(bdir, os.path.basename(p)))
    rep["moved_to"] = bdir
    bl = os.path.join(dst, "blocklist.txt")
    if legacy_categories and os.path.exists(bl):
        txt = open(bl, encoding="utf-8").read()
        if BLOCKLIST_HEADER in txt:
            with open(bl + ".tmp", "w", encoding="utf-8") as f:
                f.write(txt.replace(BLOCKLIST_HEADER, LEGACY_BLOCKLIST_HEADER))
            os.replace(bl + ".tmp", bl)
    match_owner([jl, bl, bdir], dst)
    return rep


def match_owner(paths, ref_dir):
    """Run as root, new files would belong to root and the service (its own user) could not rewrite them."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    st = os.stat(ref_dir)
    for p in paths:
        if not os.path.exists(p):
            continue
        for base, dirs, files in os.walk(p) if os.path.isdir(p) else [(os.path.dirname(p), [], [os.path.basename(p)])]:
            for x in [base] * os.path.isdir(p) + [os.path.join(base, n) for n in dirs + files]:
                try:
                    os.chown(x, st.st_uid, st.st_gid)
                except OSError:
                    pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="DHT Search data tools", formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__)
    ap.add_argument("command", choices=["info", "export-v3"])
    ap.add_argument("--data", default=os.path.join(ROOT, "data"))
    ap.add_argument("--apply", action="store_true", help="write (without it: dry run)")
    ap.add_argument("--legacy-categories", action="store_true", help="Spanish category names (for 2.x)")
    ap.add_argument("--force", action="store_true", help="skip the 'service is running' check")
    a = ap.parse_args(argv)
    dst = os.path.abspath(a.data)
    if a.command == "info":
        rep = info(dst)
    else:
        if a.apply and not a.force:
            refuse_if_running(dst)
        rep = export_v3(dst, a.apply, a.legacy_categories)
        if not a.apply:
            print("DRY RUN: nothing was written. Stop the service and run again with --apply.")
    print(json.dumps(rep, indent=2, ensure_ascii=False))
    return rep


if __name__ == "__main__":
    main()
