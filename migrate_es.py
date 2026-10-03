"""
Import data saved in the Spanish format (DHT Search <= 2.9) into the current (English) data directory.

Put whatever you want to import in data/migrate_esp/ (any subset of: torrents.jsonl or the very old torrents.json,
hashes.json, peers.jsonl, hidden_rules.json, blocklist.txt, nodes.json) and run:

  python migrate_es.py                          # DRY RUN: reports what would be imported, writes nothing
  python migrate_es.py --apply                  # imports (stop the service first)
  python migrate_es.py --src DIR --dst DIR      # other directories (default: data/migrate_esp -> data)
  python migrate_es.py --to-legacy --apply      # converts DST back to the <= 2.9 format (only to roll back to 2.x)

What it does
  torrents     Torrents that are not in the destination are appended to torrents.jsonl, with their health history and
               category names translated to English. Torrents already in the destination are left untouched (the
               destination wins). Torrents matching the blocklist (destination + imported) are skipped.
  hashes       Pending/failed hashes that the destination does not know yet.
  peers        Peers of the torrents imported in THIS run only (so running it twice imports nothing twice), skipping
               those older than --peer-ttl-days. --no-peers skips them (an IP address is personal data).
  rules        Admin hide rules not already present (same term + scope + mode).
  blocklist    Patterns not already present, appended under a dated comment.
  nodes        Union of DHT nodes (capped).
  NOT imported stats.json / history_* (counters of another instance would double count) and trackers.txt (it REPLACES
               the default trackers and every extra tracker means one more scrape per probe: more network load).

Safety
  * Dry run by default. With --apply it refuses to run while a process has the destination journal open.
  * The source directory is only read (plus a small IMPORTED-<date>.txt note).
  * Journals are only APPENDED to; the other files are backed up first. Everything goes to
    <dst>/migrate-backup-<date>/ together with undo.sh, valid as long as the service has not been started since.
"""
import argparse
import json
import os
import re
import shutil
import sys
import time
import uuid

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from hiderules import RuleError, validate                          # noqa: E402
from jsonio import atomic_write, load_json                          # noqa: E402
from store import BLOCKLIST_HEADER, LEGACY_BLOCKLIST_HEADER, MAX_NODES_PERSISTED  # noqa: E402
from textutil import LEGACY_CATEGORIES                              # noqa: E402

TO_LEGACY = {v: k for k, v in LEGACY_CATEGORIES.items()}
IH_RE = re.compile(r"^[0-9a-f]{40}$")
DUMP = dict(separators=(",", ":"), ensure_ascii=False)


# ------------------------------------------------------------------ helpers
def jline(obj):
    return json.dumps(obj, **DUMP) + "\n"


def iter_jsonl(path):
    """(line number, object) of each readable line; corrupted lines are skipped."""
    try:
        with open(path, encoding="utf-8") as f:
            for n, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield n, json.loads(line)
                except ValueError:
                    continue
    except FileNotFoundError:
        return


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


def live_torrents(dirpath):
    """Infohashes present in a data dir (journal replay: 'n' adds, 'd' removes), without loading the records."""
    jl, old = os.path.join(dirpath, "torrents.jsonl"), os.path.join(dirpath, "torrents.json")
    live = set()
    if os.path.exists(jl):
        for _, o in iter_jsonl(jl):
            t = o.get("t")
            if t == "n" and isinstance(o.get("r"), dict):
                live.add(o["r"].get("ih"))
            elif t == "d":
                live.discard(o.get("ih"))
    elif os.path.exists(old):
        live.update(load_json(old, {}).keys())
    live.discard(None)
    return live


def source_events(src):
    """Final state of the source torrents as events: the LAST 'n' of each live infohash and the 'h' events after it."""
    jl, old = os.path.join(src, "torrents.jsonl"), os.path.join(src, "torrents.json")
    if os.path.exists(jl):
        last_n = {}
        for n, o in iter_jsonl(jl):                                    # pass 1: which 'n' line is the current one
            t = o.get("t")
            if t == "n" and isinstance(o.get("r"), dict) and o["r"].get("ih"):
                last_n[o["r"]["ih"]] = n
            elif t == "d":
                last_n.pop(o.get("ih"), None)
        for n, o in iter_jsonl(jl):                                    # pass 2: emit in file order
            t = o.get("t")
            if t == "n" and isinstance(o.get("r"), dict) and last_n.get(o["r"].get("ih")) == n:
                yield o
            elif t == "h" and o.get("ih") in last_n and n > last_n[o["ih"]]:
                yield o
    elif os.path.exists(old):
        for ih, rec in load_json(old, {}).items():
            if isinstance(rec, dict):
                rec.setdefault("ih", ih)
                yield {"t": "n", "r": rec}


def read_patterns(path):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    out.append(line)
    except FileNotFoundError:
        pass
    return out


def compile_patterns(lines):
    pats = []
    for p in lines:
        try:
            pats.append(re.compile(p, re.I))
        except re.error:
            pass
    return pats


def blocked(rec, pats):
    if not pats:
        return False
    files = rec.get("files") or []
    hay = str(rec.get("name", "")) + "\n" + "\n".join(str(f[0]) for f in files[:200] if isinstance(f, (list, tuple)) and f)
    return any(p.search(hay) for p in pats)


def match_owner(paths, ref_dir):
    """Run as root, new files would belong to root and the service (its own user) could not rewrite them."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    st = os.stat(ref_dir)
    for p in paths:
        try:
            os.chown(p, st.st_uid, st.st_gid)
        except OSError:
            pass


def ensure_newline(path):
    """A journal cut by a power failure may not end in '\\n': appending would glue two lines together."""
    if os.path.exists(path) and os.path.getsize(path):
        with open(path, "rb") as f:
            f.seek(-1, os.SEEK_END)
            last = f.read(1)
        if last != b"\n":
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n")


# ------------------------------------------------------------------ import
def plan_and_import(src, dst, apply, peers=True, peer_ttl_days=30):
    now = time.time()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    rep = {"src": src, "dst": dst, "applied": apply}

    dst_old = os.path.join(dst, "torrents.json")
    if os.path.exists(dst_old) and not os.path.exists(os.path.join(dst, "torrents.jsonl")):
        raise SystemExit(f"{dst} still has the very old torrents.json: start the service once so it migrates it, then import.")

    # ---- what the destination already has
    existing = live_torrents(dst)
    dst_bl_path = os.path.join(dst, "blocklist.txt")
    dst_patterns = read_patterns(dst_bl_path)
    src_patterns = [p for p in read_patterns(os.path.join(src, "blocklist.txt")) if compile_patterns([p])]
    new_patterns = [p for p in dict.fromkeys(src_patterns) if p not in dst_patterns]
    pats = compile_patterns(dst_patterns + new_patterns)

    # ---- torrents
    t = {"new": 0, "already_present": 0, "blocked": 0, "invalid": 0, "health_events": 0, "categories_translated": 0}
    out_lines, imported = [], set()
    seen_src = set()
    for o in source_events(src):
        if o["t"] == "n":
            rec = dict(o["r"])
            ih = str(rec.get("ih", "")).lower()
            rec["ih"] = ih
            if not IH_RE.match(ih) or not isinstance(rec.get("name"), str):
                t["invalid"] += 1
                continue
            if ih in seen_src:                       # duplicated key in an old torrents.json: keep the first
                continue
            seen_src.add(ih)
            if ih in existing:
                t["already_present"] += 1
                continue
            if blocked(rec, pats):
                t["blocked"] += 1
                continue
            cat = rec.get("category")
            if cat in LEGACY_CATEGORIES:
                rec["category"] = LEGACY_CATEGORIES[cat]
                t["categories_translated"] += 1
            imported.add(ih)
            t["new"] += 1
            out_lines.append(jline({"t": "n", "r": rec}))
        elif o["t"] == "h" and o.get("ih") in imported:
            t["health_events"] += 1
            out_lines.append(jline(o))
    rep["torrents"] = t
    known = existing | imported

    # ---- hashes
    dst_hashes_path = os.path.join(dst, "hashes.json")
    src_hashes = load_json(os.path.join(src, "hashes.json"), {})
    dst_hashes = load_json(dst_hashes_path, {}) if os.path.exists(dst_hashes_path) else {}
    h_new = {ih: v for ih, v in (src_hashes.items() if isinstance(src_hashes, dict) else ())
             if IH_RE.match(str(ih)) and isinstance(v, dict) and ih not in dst_hashes and ih not in known}
    by_status = {}
    for v in h_new.values():
        by_status[v.get("status", "?")] = by_status.get(v.get("status", "?"), 0) + 1
    rep["hashes"] = {"new": len(h_new), "by_status": by_status}

    # ---- peers (only for torrents imported in this run)
    peer_lines, p_old = [], 0
    if peers and imported:
        cutoff = now - peer_ttl_days * 86400
        for _, o in iter_jsonl(os.path.join(src, "peers.jsonl")):
            if o.get("i") in imported and o.get("a"):
                if o.get("l", 0) < cutoff:
                    p_old += 1
                    continue
                peer_lines.append(jline(o))
    rep["peers"] = {"new": len(peer_lines), "expired_skipped": p_old, "enabled": peers}

    # ---- hide rules
    dst_rules_path = os.path.join(dst, "hidden_rules.json")
    dst_rules = load_json(dst_rules_path, []) if os.path.exists(dst_rules_path) else []
    dst_rules = dst_rules if isinstance(dst_rules, list) else []
    keys = {(r.get("term"), r.get("scope", "all"), r.get("mode", "word")) for r in dst_rules}
    ids = {r.get("id") for r in dst_rules}
    new_rules, bad_rules = [], 0
    src_rules = load_json(os.path.join(src, "hidden_rules.json"), [])
    for r in src_rules if isinstance(src_rules, list) else []:
        try:
            r = dict(r)
            r["scope"], r["mode"] = r.get("scope", "all"), r.get("mode", "word")
            r["term"] = validate(r.get("term"), r["scope"], r["mode"])
        except (RuleError, TypeError, AttributeError):
            bad_rules += 1
            continue
        k = (r["term"], r["scope"], r["mode"])
        if k in keys:
            continue
        keys.add(k)
        if not r.get("id") or r["id"] in ids:
            r["id"] = uuid.uuid4().hex[:10]
        ids.add(r["id"])
        r.setdefault("enabled", True)
        r.setdefault("created", int(now))
        new_rules.append(r)
    rep["rules"] = {"new": len(new_rules), "invalid_skipped": bad_rules}
    rep["blocklist"] = {"new_patterns": len(new_patterns)}

    # ---- nodes
    dst_nodes_path = os.path.join(dst, "nodes.json")
    dst_nodes = load_json(dst_nodes_path, []) if os.path.exists(dst_nodes_path) else []
    src_nodes = load_json(os.path.join(src, "nodes.json"), [])
    have = set(dst_nodes)
    n_new = [n for n in dict.fromkeys(src_nodes if isinstance(src_nodes, list) else []) if isinstance(n, str) and n not in have]
    n_new = n_new[:max(0, MAX_NODES_PERSISTED - len(dst_nodes))]
    rep["nodes"] = {"new": len(n_new)}

    rep["not_imported"] = [f for f in ("stats.json", "history_raw.json", "history_m5.jsonl", "history_h1.jsonl", "trackers.txt")
                           if os.path.exists(os.path.join(src, f))]

    changes = bool(out_lines or h_new or peer_lines or new_rules or new_patterns or n_new)
    if not apply or not changes:
        rep["backup"] = None
        return rep

    # ---- write (backup first)
    bdir, k = os.path.join(dst, f"migrate-backup-{stamp}"), 1
    while os.path.exists(bdir):                    # two runs within the same second
        k += 1
        bdir = os.path.join(dst, f"migrate-backup-{stamp}-{k}")
    os.makedirs(bdir)
    undo = ["#!/bin/sh",
            f"# Undoes the import of {stamp} from {src}.",
            "# ONLY valid if the service has NOT been started since the import (it may have compacted the journals).",
            "# Stop the service, then run:  sh " + os.path.join(bdir, "undo.sh"),
            "set -e", f'cd "{os.path.abspath(dst)}"']
    restore = []                                   # run only after every check passed
    jsonl_sizes = {}

    def backup(name):
        p = os.path.join(dst, name)
        if os.path.exists(p):
            shutil.copy2(p, os.path.join(bdir, name))
            restore.append(f'cp -p "{os.path.join(os.path.abspath(bdir), name)}" "{name}"')
        else:
            restore.append(f'rm -f "{name}"')

    def append_journal(name, lines):
        p = os.path.join(dst, name)
        ensure_newline(p)
        jsonl_sizes[name] = os.path.getsize(p) if os.path.exists(p) else None
        with open(p, "a", encoding="utf-8") as f:
            f.writelines(lines)
            f.flush()
            os.fsync(f.fileno())

    if out_lines:
        append_journal("torrents.jsonl", out_lines)
    if peer_lines:
        append_journal("peers.jsonl", peer_lines)
    for name, before in jsonl_sizes.items():
        after = os.path.getsize(os.path.join(dst, name))
        undo.append(f'[ "$(stat -c %s {name})" = "{after}" ] || {{ echo "{name} changed since the import: not undoing"; exit 1; }}')
        restore.append(f'rm -f "{name}"' if before is None else f'truncate -s {before} "{name}"')
    if h_new:
        backup("hashes.json")
        dst_hashes.update(h_new)
        atomic_write(dst_hashes_path, dst_hashes)
    if new_rules:
        backup("hidden_rules.json")
        atomic_write(dst_rules_path, dst_rules + new_rules)
    if new_patterns:
        backup("blocklist.txt")
        if not os.path.exists(dst_bl_path):
            with open(dst_bl_path, "w", encoding="utf-8") as f:
                f.write(BLOCKLIST_HEADER)
        ensure_newline(dst_bl_path)
        with open(dst_bl_path, "a", encoding="utf-8") as f:
            f.write(f"# imported from {os.path.basename(os.path.abspath(src))} on {time.strftime('%Y-%m-%d %H:%M')}\n")
            f.write("".join(p + "\n" for p in new_patterns))
    if n_new:
        backup("nodes.json")
        atomic_write(dst_nodes_path, list(dst_nodes) + n_new)

    with open(os.path.join(bdir, "undo.sh"), "w", encoding="utf-8") as f:
        f.write("\n".join(undo + restore) + '\necho "import undone"\n')
    touched = [os.path.join(dst, n) for n in ("torrents.jsonl", "peers.jsonl", "hashes.json", "hidden_rules.json",
                                               "blocklist.txt", "nodes.json") if os.path.exists(os.path.join(dst, n))]
    match_owner(touched + [bdir] + [os.path.join(bdir, n) for n in os.listdir(bdir)], dst)
    rep["backup"] = bdir
    atomic_write(os.path.join(bdir, "report.json"), rep)
    try:
        with open(os.path.join(src, f"IMPORTED-{stamp}.txt"), "w", encoding="utf-8") as f:
            f.write(json.dumps(rep, indent=2, ensure_ascii=False) + "\n")
    except OSError:
        pass                                   # read-only source: not important
    return rep


# ------------------------------------------------------------------ rollback to 2.x
def to_legacy(dst, apply):
    """Category names back to Spanish in DST/torrents.jsonl (the only on-disk difference with 2.x)."""
    path = os.path.join(dst, "torrents.jsonl")
    if not os.path.exists(path):
        raise SystemExit(f"{path} does not exist")
    changed = 0
    tmp = path + ".legacy"
    out = open(tmp, "w", encoding="utf-8") if apply else None
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                new = line
                if '"category"' in line:
                    try:
                        o = json.loads(line)
                    except ValueError:
                        o = None
                    if o and o.get("t") == "n" and isinstance(o.get("r"), dict) and o["r"].get("category") in TO_LEGACY:
                        o["r"]["category"] = TO_LEGACY[o["r"]["category"]]
                        new = jline(o)
                        changed += 1
                if out:
                    out.write(new)
        if out:
            out.flush()
            os.fsync(out.fileno())
            out.close()
            out = None
            os.replace(tmp, path)
            match_owner([path], dst)
    finally:
        if out:
            out.close()
            os.remove(tmp)
    bl = os.path.join(dst, "blocklist.txt")
    if apply and os.path.exists(bl):
        txt = open(bl, encoding="utf-8").read()
        if BLOCKLIST_HEADER in txt:
            atomic_text = txt.replace(BLOCKLIST_HEADER, LEGACY_BLOCKLIST_HEADER)
            with open(bl + ".tmp", "w", encoding="utf-8") as f:
                f.write(atomic_text)
            os.replace(bl + ".tmp", bl)
            match_owner([bl], dst)
    return {"dst": dst, "applied": apply, "records_converted_to_spanish": changed}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Import Spanish-format (<= 2.9) DHT Search data into the current English data dir.",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("What it does")[0])
    ap.add_argument("--src", default=os.path.join(ROOT, "data", "migrate_esp"), help="directory with the data to import")
    ap.add_argument("--dst", default=os.path.join(ROOT, "data"), help="data directory of the running installation")
    ap.add_argument("--apply", action="store_true", help="write the changes (without it: dry run)")
    ap.add_argument("--no-peers", action="store_true", help="do not import peers (IP addresses)")
    ap.add_argument("--peer-ttl-days", type=int, default=30, help="skip peers not seen for this many days (same as the service)")
    ap.add_argument("--to-legacy", action="store_true", help="convert DST back to the <= 2.9 format (to roll back to 2.x)")
    ap.add_argument("--force", action="store_true", help="skip the 'service is running' check")
    a = ap.parse_args(argv)
    src, dst = os.path.abspath(a.src), os.path.abspath(a.dst)

    if a.apply and not a.force:
        pids = holders_of([os.path.join(dst, "torrents.jsonl"), os.path.join(dst, "peers.jsonl")])
        if pids:
            raise SystemExit(f"The destination is in use by PID {', '.join(map(str, sorted(pids)))}: stop the service first "
                             f"(systemctl stop torrent-search).")

    if a.to_legacy:
        rep = to_legacy(dst, a.apply)
    else:
        if not os.path.isdir(src):
            raise SystemExit(f"{src} does not exist: put the data to import there (or use --src).")
        if os.path.realpath(src) == os.path.realpath(dst):
            raise SystemExit("--src and --dst are the same directory")
        rep = plan_and_import(src, dst, a.apply, peers=not a.no_peers, peer_ttl_days=a.peer_ttl_days)

    print(json.dumps(rep, indent=2, ensure_ascii=False))
    if not a.apply:
        print("\nDRY RUN: nothing was written. Stop the service and run again with --apply.")
    elif rep.get("backup"):
        print(f"\nDone. Backup and undo script: {rep['backup']}/undo.sh  (valid until the service is started)."
              "\nStart the service: the imported torrents are indexed and the hide rules/blocklist applied on load.")
    elif a.to_legacy:
        print("\nDone: the data can now be used by a 2.x version. Starting 3.x again converts it back to English automatically.")
    else:
        print("\nNothing new to import.")
    return rep


if __name__ == "__main__":
    main()
