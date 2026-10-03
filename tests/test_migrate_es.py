"""migrate_es.py: importing Spanish-format (<= 2.9) data into the English data dir, undo, and --to-legacy.
Run: python tests/test_migrate_es.py"""
import hashlib, json, os, shutil, subprocess, sys, tempfile, time
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
import migrate_es as M
from store import Store

MB = 1 << 20
def H(i): return hashlib.sha1(str(i).encode()).hexdigest()
def mk(st, i, name, files, seeds=10):
    assert st.save_torrent(H(i), {"name": name, "size": sum(s for _, s in files), "files": files, "piece_length": 262144},
                           seeders=seeds, peers=1, src="scrape")
def snapshot(d):
    return {f: open(os.path.join(d, f), "rb").read() for f in sorted(os.listdir(d)) if os.path.isfile(os.path.join(d, f))}
def quiet(f, *a, **k):
    import contextlib, io
    with contextlib.redirect_stdout(io.StringIO()):
        return f(*a, **k)

# ---------------------------------------------------------------- a 2.9-style (Spanish) source
src = tempfile.mkdtemp()
s = Store(src)
mk(s, 1, "Shared Movie 1080p", [("movie.mkv", 700 * MB)])                   # also in the destination
mk(s, 2, "Holiday Photos", [(f"img{j}.jpg", MB) for j in range(5)], seeds=3)
mk(s, 3, "Some Album FLAC", [("01.flac", 30 * MB)])
mk(s, 4, "Blocked thing", [("x.mkv", MB)])                                  # matches the destination blocklist
mk(s, 5, "Deleted later", [("y.mkv", MB)])
mk(s, 6, "Docs pack", [("a.pdf", MB)])
for k in range(3): s.update_health(H(2), 10 + k, 2, "scrape")              # health history to carry over
s.journal.append({"t": "d", "ih": H(5)})                                    # deleted in the source: not imported
s.peers.note(H(2), "203.0.113.20", 6881, 1, "qBittorrent", "c")
s.peers.note(H(1), "203.0.113.21", 6881, 1, "qBittorrent", "c")             # torrent already in dst: peer not imported
s.rules.add("photos", "name", "word")
s.rules.add("secret", "all", "substring")
s.add_hash("ee" * 20, "announce", prio=True); s.add_hash(H(1), "announce")
s.nodes.update({"1.2.3.4:6881", "5.6.7.8:6881"}); s._dirty["nodes"] = True
s.close()
with open(os.path.join(src, "blocklist.txt"), "a", encoding="utf-8") as f: f.write("forbiddenword\n(\n")   # one valid, one invalid
quiet(M.to_legacy, src, True)                                               # -> exactly what <= 2.9 wrote on disk
raw = open(os.path.join(src, "torrents.jsonl"), encoding="utf-8").read()
assert '"category":"Vídeo"' in raw and '"category":"Imágenes"' in raw and '"category":"Video"' not in raw
open(os.path.join(src, "stats.json"), "a").close()
src_before = snapshot(src)

# ---------------------------------------------------------------- the English destination
dst = tempfile.mkdtemp()
d = Store(dst)
mk(d, 1, "Shared Movie 1080p", [("movie.mkv", 700 * MB)], seeds=99)
mk(d, 10, "Local only", [("z.iso", MB)])
d.rules.add("secret", "all", "substring")                                   # same rule already present
d.close()
with open(os.path.join(dst, "blocklist.txt"), "a", encoding="utf-8") as f: f.write("blocked thing\n")
dst_before = snapshot(dst)

# ---------------------------------------------------------------- dry run writes nothing
rep = quiet(M.plan_and_import, src, dst, apply=False)
t = rep["torrents"]
assert (t["new"], t["already_present"], t["blocked"], t["health_events"]) == (3, 1, 1, 3), t
assert t["categories_translated"] == 2 and rep["peers"]["new"] == 1 and rep["rules"]["new"] == 1, rep
assert rep["blocklist"]["new_patterns"] == 1 and rep["nodes"]["new"] == 2 and rep["hashes"]["new"] == 1, rep
assert "stats.json" in rep["not_imported"] and rep["backup"] is None
assert snapshot(dst) == dst_before and snapshot(src) == src_before
print("dry run: correct plan, nothing written: OK")

# ---------------------------------------------------------------- apply
dst2 = tempfile.mkdtemp(); shutil.rmtree(dst2); shutil.copytree(dst, dst2)  # pristine copy for the undo test
rep = quiet(M.plan_and_import, src, dst, apply=True)
assert rep["torrents"]["new"] == 3 and os.path.exists(os.path.join(rep["backup"], "undo.sh"))
assert {k: v for k, v in snapshot(src).items() if not k.startswith("IMPORTED-")} == src_before   # source only read
assert '"category":"Vídeo"' not in open(os.path.join(dst, "torrents.jsonl"), encoding="utf-8").read()
st = quiet(Store, dst)
assert len(st.torrents) == 5 and {H(2), H(3), H(6)} <= set(st.torrents) and H(4) not in st.torrents and H(5) not in st.torrents
assert st.get(H(1))["seeders"] == 99                                         # destination wins
assert st.get(H(2))["category"] == "Images" and st.get(H(6))["category"] == "Documents" and st.get(H(2))["seeders"] == 12
assert st.legacy_converted == 0                                                 # imported lines are already English
assert st.search({"q": "cat:images"})["total"] == 0                          # "photos" rule imported: hidden, not deleted
assert H(2) in st.hidden and H(2) in st.torrents
assert set(st.peers.by_ih) == {H(2)}
assert "ee" * 20 in st.hashes and st.hashes.get(H(1), {}).get("status") != "pending"
assert "1.2.3.4:6881" in st.nodes
st.close()
bl = open(os.path.join(dst, "blocklist.txt"), encoding="utf-8").read()
assert "forbiddenword" in bl and "\n(\n" not in bl and bl.count("blocked thing") == 1
rules = json.load(open(os.path.join(dst, "hidden_rules.json")))
assert sorted(r["term"] for r in rules) == ["photos", "secret"]
print("apply: new torrents with history, English categories, destination wins, blocklist, rules, peers, hashes, nodes: OK")

# ---------------------------------------------------------------- idempotent
rep = quiet(M.plan_and_import, src, dst, apply=True)
assert rep["torrents"]["new"] == 0 and rep["peers"]["new"] == 0 and rep["rules"]["new"] == 0 and rep["backup"] is None, rep
print("running it again imports nothing: OK")

# ---------------------------------------------------------------- undo.sh restores the destination exactly
before2 = snapshot(dst2)
rep = quiet(M.plan_and_import, src, dst2, apply=True)
subprocess.run(["sh", os.path.join(rep["backup"], "undo.sh")], check=True, capture_output=True)
assert snapshot(dst2) == before2
rep = quiet(M.plan_and_import, src, dst2, apply=True)                        # undo refuses after the journal changed
with open(os.path.join(dst2, "torrents.jsonl"), "a") as f: f.write('{"t":"d","ih":"' + H(10) + '"}\n')
r = subprocess.run(["sh", os.path.join(rep["backup"], "undo.sh")], capture_output=True, text=True)
assert r.returncode != 0 and "changed since the import" in r.stdout
print("undo.sh: exact restore, and refuses once the journal changed: OK")

# ---------------------------------------------------------------- the service must be stopped
holder = subprocess.Popen([sys.executable, "-c", f"f=open({os.path.join(dst, 'torrents.jsonl')!r}); import time; time.sleep(30)"])
time.sleep(0.5)
try:
    assert holder.pid in M.holders_of([os.path.join(dst, "torrents.jsonl")])
    try:
        quiet(M.main, ["--src", src, "--dst", dst, "--apply"]); raise AssertionError("should refuse")
    except SystemExit as e:
        assert "stop the service" in str(e)
finally:
    holder.kill()
print("refuses to write while another process has the journal open: OK")

# ---------------------------------------------------------------- rollback to 2.x and back
rep = quiet(M.to_legacy, dst, True)
assert rep["records_converted_to_spanish"] >= 2 and '"category":"Vídeo"' in open(os.path.join(dst, "torrents.jsonl"), encoding="utf-8").read()
st = quiet(Store, dst); assert st.legacy_converted >= 2 and st.get(H(1))["category"] == "Video"; st.close()
assert '"category":"Vídeo"' not in open(os.path.join(dst, "torrents.jsonl"), encoding="utf-8").read()
print("--to-legacy for rolling back to 2.x, and 3.x converts it back: OK")

# ---------------------------------------------------------------- very old source format (torrents.json)
src3, dst3 = tempfile.mkdtemp(), tempfile.mkdtemp()
json.dump({H(30): {"ih": H(30), "name": "Old Movie", "size": 1, "file_count": 1, "files": [["a.mkv", 1]], "category": "Vídeo",
                   "seeders": 2, "peers": 1}, "zz": {"name": "bad"}}, open(os.path.join(src3, "torrents.json"), "w"))
rep = quiet(M.plan_and_import, src3, dst3, apply=True)
assert rep["torrents"]["new"] == 1 and rep["torrents"]["invalid"] == 1
st = quiet(Store, dst3); assert st.get(H(30))["category"] == "Video"; st.close()
print("source in the very old torrents.json format: OK")
print("ALL OK (migrate_es)")
