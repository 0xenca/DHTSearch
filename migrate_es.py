"""
Import data saved in the Spanish format (DHT Search <= 2.9) into the current (English) data directory.

Put whatever you want to import in data/migrate_esp/ (any subset of: torrents.jsonl or the very old torrents.json,
hashes.json, peers.jsonl, hidden_rules.json, blocklist.txt, nodes.json) and run:

  python migrate_es.py                          # DRY RUN: reports what would be imported, writes nothing
  python migrate_es.py --apply                  # imports (stop the service first)
  python migrate_es.py --src DIR --dst DIR      # other directories (default: data/migrate_esp -> data)
  python migrate_es.py --to-legacy --apply      # converts DST back to the <= 2.9 format (only to roll back to 2.x)

What it does
  torrents     Torrents that are not in the destination are added to it (4.x storage: meta.log + columns), with their
               health history and category names translated to English. Torrents already in the destination are left untouched (the
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
  * Dry run by default. With --apply it refuses to run while a process has the destination data open.
  * The source directory is only read (plus a small IMPORTED-<date>.txt note).
  * Torrents and hashes are only ADDED; the other files are backed up first. Everything goes to
    <dst>/migrate-backup-<date>/ with the list of what was imported:
      python migrate_es.py --undo <dst>/migrate-backup-<date>     removes it again (also after the service has run)
  * The imported torrents are indexed for search by the service when it starts (in the background).
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
from store import BLOCKLIST_HEADER, MAX_NODES_PERSISTED             # noqa: E402
from textutil import LEGACY_CATEGORIES                              # noqa: E402
from tools import export_v3, holders_of, match_owner, refuse_if_running  # noqa: E402,F401

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


class Existing:
    """`ih in Existing(dir)`: is the torrent in that data dir? 4.x: checkpoint columns + meta.log tail (read only, without
    loading the records); 3.x: journal replay; 2.x: torrents.json."""

    def __init__(self, dirpath):
        self.cols, self.extra, self.gone = None, set(), set()
        if os.path.exists(os.path.join(dirpath, "meta.log")):
            from colstore import Columns
            from metalog import MetaLog
            start = None
            sp = os.path.join(dirpath, "state.bin")
            if os.path.exists(sp):
                try:
                    self.cols, hdr = Columns.load(sp)
                    start = hdr["meta_size"]
                except (OSError, ValueError, KeyError):
                    self.cols = None
            ml = MetaLog(os.path.join(dirpath, "meta.log"))
            try:
                for _, o in ml.iterate(start):
                    if o.get("t") == "n" and isinstance(o.get("r"), dict):
                        ih = str(o["r"].get("ih", "")).lower()
                        self.extra.add(ih)
                        self.gone.discard(ih)
                    elif o.get("t") == "d":
                        self.extra.discard(o.get("ih"))
                        self.gone.add(o.get("ih"))
            finally:
                ml.close()
        else:
            self.extra = live_torrents(dirpath)

    def __contains__(self, ih):
        if ih in self.extra:
            return True
        if ih in self.gone or self.cols is None:
            return False
        try:
            return self.cols.find(bytes.fromhex(ih)) >= 0
        except ValueError:
            return False

    def __len__(self):
        return len(self.extra) + (len(self.cols.live()) if self.cols is not None else 0)


def live_torrents(dirpath):
    """Infohashes present in a 3.x/2.x data dir (journal replay: 'n' adds, 'd' removes), without loading the records."""
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


def ensure_newline(path):
    """A journal cut by a power failure may not end in '\\n': appending would glue two lines together."""
    if os.path.exists(path) and os.path.getsize(path):
        with open(path, "rb") as f:
            f.seek(-1, os.SEEK_END)
            last = f.read(1)
        if last != b"\n":
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n")




def file_sha(path):
    import hashlib
    try:
        with open(path, "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()
    except FileNotFoundError:
        return None


# ------------------------------------------------------------------ import
def translate(rec):
    """Source record -> destination record (lower-case infohash, English category). None if unusable."""
    rec = dict(rec)
    ih = str(rec.get("ih", "")).lower()
    if not IH_RE.match(ih) or not isinstance(rec.get("name"), str):
        return None
    rec["ih"] = ih
    if rec.get("category") in LEGACY_CATEGORIES:
        rec["category"] = LEGACY_CATEGORIES[rec["category"]]
    return rec


def plan_and_import(src, dst, apply, peers=True, peer_ttl_days=30):
    now = time.time()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    rep = {"src": src, "dst": dst, "applied": apply}

    # ---- what the destination already has (read only)
    existing = Existing(dst)
    dst_bl_path = os.path.join(dst, "blocklist.txt")
    dst_patterns = read_patterns(dst_bl_path)
    src_patterns = [p for p in read_patterns(os.path.join(src, "blocklist.txt")) if compile_patterns([p])]
    new_patterns = [p for p in dict.fromkeys(src_patterns) if p not in dst_patterns]
    pats = compile_patterns(dst_patterns + new_patterns)

    # ---- torrents (plan: only infohashes are kept in memory)
    t = {"new": 0, "already_present": 0, "blocked": 0, "invalid": 0, "health_events": 0, "categories_translated": 0}
    to_import, seen_src = set(), set()
    for o in source_events(src):
        if o["t"] == "n":
            raw_cat = o["r"].get("category")
            rec = translate(o["r"])
            if rec is None:
                t["invalid"] += 1
                continue
            ih = rec["ih"]
            if ih in seen_src:                       # duplicated key in an old torrents.json: keep the first
                continue
            seen_src.add(ih)
            if ih in existing:
                t["already_present"] += 1
            elif blocked(rec, pats):
                t["blocked"] += 1
            else:
                to_import.add(ih)
                t["new"] += 1
                t["categories_translated"] += raw_cat in LEGACY_CATEGORIES
        elif o["t"] == "h" and o.get("ih") in to_import:
            t["health_events"] += 1
    rep["torrents"] = t
    del seen_src

    # ---- hashes
    src_hashes = load_json(os.path.join(src, "hashes.json"), {})
    dst_hashes = load_json(os.path.join(dst, "hashes.json"), {})
    dst_hashes = dst_hashes if isinstance(dst_hashes, dict) else {}
    for _, ev in iter_jsonl(os.path.join(dst, "hashes.jsonl")):          # 4.x change log since the last snapshot
        if ev.get("t") == "s" and ev.get("i"):
            dst_hashes[ev["i"]] = ev.get("h") or {}
        elif ev.get("t") == "x":
            dst_hashes.pop(ev.get("i"), None)
    h_new = {ih: v for ih, v in (src_hashes.items() if isinstance(src_hashes, dict) else ())
             if IH_RE.match(str(ih)) and isinstance(v, dict) and ih not in dst_hashes and ih not in existing and ih not in to_import}
    del dst_hashes
    by_status = {}
    for v in h_new.values():
        by_status[v.get("status", "?")] = by_status.get(v.get("status", "?"), 0) + 1
    rep["hashes"] = {"new": len(h_new), "by_status": by_status}

    # ---- peers (only for torrents imported in this run)
    peer_lines, p_old = [], 0
    if peers and to_import:
        cutoff = now - peer_ttl_days * 86400
        for _, o in iter_jsonl(os.path.join(src, "peers.jsonl")):
            if o.get("i") in to_import and o.get("a"):
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

    changes = bool(to_import or h_new or peer_lines or new_rules or new_patterns or n_new)
    if not apply or not changes:
        rep["backup"] = None
        return rep

    # ---- write. Backup dir first: it records what is added, so --undo can remove it later.
    bdir, k = os.path.join(dst, f"migrate-backup-{stamp}"), 1
    while os.path.exists(bdir):                    # two runs within the same second
        k += 1
        bdir = os.path.join(dst, f"migrate-backup-{stamp}-{k}")
    os.makedirs(bdir)
    small = {}                                     # file name -> sha1 after our write (undo restores only if unchanged)

    def backup(name):
        p = os.path.join(dst, name)
        if os.path.exists(p):
            shutil.copy2(p, os.path.join(bdir, name))

    if peer_lines:                                 # before opening the store: it loads them
        pp = os.path.join(dst, "peers.jsonl")
        ensure_newline(pp)
        with open(pp, "a", encoding="utf-8") as f:
            f.writelines(peer_lines)
            f.flush()
            os.fsync(f.fileno())

    from store import HashState, Store            # noqa: E402  (heavy import only when writing)
    done, events = [], 0
    with open(os.path.join(bdir, "imported.txt"), "w", encoding="utf-8") as imp:
        st = Store(dst, index_background=False, indexing=False)
        try:
            ok = set()
            for o in source_events(src):
                if o["t"] == "n":
                    rec = translate(o["r"])
                    if rec is None or rec["ih"] not in to_import or rec["ih"] in ok:
                        continue
                    hh, hd = rec.pop("hh", None), rec.pop("hd", None)
                    if st.import_record(rec, hh=hh, hd=hd):
                        ok.add(rec["ih"])
                        imp.write(rec["ih"] + "\n")
                elif o["t"] == "h" and o.get("ih") in ok:
                    st.update_health(o["ih"], o.get("s", 0), o.get("p", 0), src=o.get("g", "swarm"), nrep=o.get("n", 1),
                                     detail=o.get("d"), at=o.get("a"))
                    events += 1
            done = ok
            with st.lock:
                for ih, v in h_new.items():
                    if ih not in st.hashes and st.doc(ih) < 0:
                        st.hashes[ih] = HashState(v)
        finally:
            st.close()                             # checkpoint + full hashes.json
        imp.flush()
        os.fsync(imp.fileno())
    with open(os.path.join(bdir, "hashes.txt"), "w", encoding="utf-8") as f:
        f.write("".join(ih + "\n" for ih in h_new))
    rep["torrents"]["imported"] = len(done)
    rep["torrents"]["health_events_applied"] = events

    if new_rules:
        backup("hidden_rules.json")
        atomic_write(dst_rules_path, dst_rules + new_rules)
        small["hidden_rules.json"] = file_sha(dst_rules_path)
    if new_patterns:
        backup("blocklist.txt")
        if not os.path.exists(dst_bl_path):
            with open(dst_bl_path, "w", encoding="utf-8") as f:
                f.write(BLOCKLIST_HEADER)
        ensure_newline(dst_bl_path)
        with open(dst_bl_path, "a", encoding="utf-8") as f:
            f.write(f"# imported from {os.path.basename(os.path.abspath(src))} on {time.strftime('%Y-%m-%d %H:%M')}\n")
            f.write("".join(p + "\n" for p in new_patterns))
        small["blocklist.txt"] = file_sha(dst_bl_path)
    if n_new:
        backup("nodes.json")
        atomic_write(dst_nodes_path, list(dst_nodes) + n_new)
        small["nodes.json"] = file_sha(dst_nodes_path)

    rep["backup"] = bdir
    rep["small_files_after"] = small
    atomic_write(os.path.join(bdir, "report.json"), rep)
    touched = [os.path.join(dst, n) for n in ("peers.jsonl", "hashes.json", "hidden_rules.json", "blocklist.txt", "nodes.json")]
    match_owner(touched + [bdir] + [os.path.join(bdir, n) for n in os.listdir(bdir)], dst)
    match_owner([p for p in [os.path.join(dst, n) for n in os.listdir(dst)] if not os.path.isdir(p) or p.endswith("index")], dst)
    try:
        with open(os.path.join(src, f"IMPORTED-{stamp}.txt"), "w", encoding="utf-8") as f:
            f.write(json.dumps(rep, indent=2, ensure_ascii=False) + "\n")
    except OSError:
        pass                                   # read-only source: not important
    return rep


# ------------------------------------------------------------------ undo
def undo(bdir, dst):
    """Removes what an import added: its torrents (a deletion record each), its hashes not indexed since, the peers of
    its torrents; restores rules/blocklist/nodes from the backup if nobody changed them after the import."""
    rep = load_json(os.path.join(bdir, "report.json"), None)
    if not isinstance(rep, dict):
        raise SystemExit(f"{bdir} is not an import backup (no report.json)")

    def lines(name):
        try:
            with open(os.path.join(bdir, name), encoding="utf-8") as f:
                return [x.strip() for x in f if x.strip()]
        except FileNotFoundError:
            return []
    ihs, hashes = lines("imported.txt"), lines("hashes.txt")
    out = {"backup": bdir, "torrents_removed": 0, "hashes_removed": 0, "peers_removed": 0, "restored": [], "not_restored": []}
    from store import Store                       # noqa: E402
    st = Store(dst, index_background=False, indexing=False)
    try:
        for ih in ihs:
            out["torrents_removed"] += bool(st.delete(ih))
        with st.lock:
            for ih in hashes:
                h = st.hashes.get(ih)
                if h is not None and st.doc(ih) < 0:
                    del st.hashes[ih]
                    out["hashes_removed"] += 1
        out["peers_removed"] = st.peers.forget(ihs)
    finally:
        st.close()
    for name, sha in (rep.get("small_files_after") or {}).items():
        cur = os.path.join(dst, name)
        if file_sha(cur) != sha:
            out["not_restored"].append(f"{name} (changed after the import)")
            continue
        b = os.path.join(bdir, name)
        if os.path.exists(b):
            shutil.copy2(b, cur)
        else:
            os.remove(cur)
        out["restored"].append(name)
    match_owner([os.path.join(dst, n) for n in ("hashes.json", "peers.jsonl", "hidden_rules.json", "blocklist.txt", "nodes.json")], dst)
    os.replace(bdir, bdir + "-undone")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Import Spanish-format (<= 2.9) DHT Search data into the current English data dir.",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("What it does")[0])
    ap.add_argument("--src", default=os.path.join(ROOT, "data", "migrate_esp"), help="directory with the data to import")
    ap.add_argument("--dst", default=os.path.join(ROOT, "data"), help="data directory of the installation")
    ap.add_argument("--apply", action="store_true", help="write the changes (without it: dry run)")
    ap.add_argument("--no-peers", action="store_true", help="do not import peers (IP addresses)")
    ap.add_argument("--peer-ttl-days", type=int, default=30, help="skip peers not seen for this many days (same as the service)")
    ap.add_argument("--undo", metavar="BACKUP_DIR", help="remove what that import added")
    ap.add_argument("--to-legacy", action="store_true",
                    help="convert DST back to the <= 2.9 format (to roll back to 2.x; same as tools.py export-v3 --legacy-categories)")
    ap.add_argument("--force", action="store_true", help="skip the 'service is running' check")
    a = ap.parse_args(argv)
    src, dst = os.path.abspath(a.src), os.path.abspath(a.dst)

    if (a.apply or a.undo) and not a.force:
        refuse_if_running(dst)

    if a.undo:
        rep = undo(os.path.abspath(a.undo), dst)
        print(json.dumps(rep, indent=2, ensure_ascii=False))
        print("\nImport undone.")
        return rep
    if a.to_legacy:
        rep = export_v3(dst, a.apply, legacy_categories=True)
    else:
        if not os.path.isdir(src):
            raise SystemExit(f"{src} does not exist: put the data to import there (or use --src).")
        if os.path.realpath(src) == os.path.realpath(dst):
            raise SystemExit("--src and --dst are the same directory")
        rep = plan_and_import(src, dst, a.apply, peers=not a.no_peers, peer_ttl_days=a.peer_ttl_days)

    print(json.dumps(rep, indent=2, ensure_ascii=False))
    if not a.apply:
        print("\nDRY RUN: nothing was written. Stop the service and run again with --apply.")
    elif a.to_legacy:
        print(f"\nDone: {dst}/torrents.jsonl can be used by a 2.x version (the 4.x files were moved to {rep.get('moved_to')}).")
    elif rep.get("backup"):
        print(f"\nDone. To undo:  python migrate_es.py --undo {rep['backup']}"
              "\nStart the service: the imported torrents are indexed for search in the background.")
    else:
        print("\nNothing new to import.")
    return rep


if __name__ == "__main__":
    main()
