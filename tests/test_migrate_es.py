"""migrate_es.py: importing Spanish-format (<= 2.9) data into the English data dir, undo, and --to-legacy.
Run: python tests/test_migrate_es.py"""
import glob, hashlib, json, os, shutil, subprocess, sys, tempfile, time
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
mk(s, 6, "Docs pack", [("a.pdf", MB)])
T0 = int(time.time())
for k in range(3): s.update_health(H(2), 10 + k, 2, "scrape", at=T0 + 1 + k)  # health history to carry over
s.peers.note(H(2), "203.0.113.20", 6881, 1, "qBittorrent", "c")
s.peers.note(H(1), "203.0.113.21", 6881, 1, "qBittorrent", "c")             # torrent already in dst: peer not imported
s.rules.add("photos", "name", "word")
s.rules.add("secret", "all", "substring")
s.add_hash("ee" * 20, "announce", prio=True); s.add_hash(H(1), "announce")
s.nodes.update({"1.2.3.4:6881", "5.6.7.8:6881"}); s._dirty["nodes"] = True
s.close()
with open(os.path.join(src, "blocklist.txt"), "a", encoding="utf-8") as f: f.write("forbiddenword\n(\n")   # one valid, one invalid
quiet(M.main, ["--to-legacy", "--apply", "--dst", src, "--force"])          # -> exactly what <= 2.9 wrote on disk
shutil.rmtree(glob.glob(os.path.join(src, "v4-moved-*"))[0])
with open(os.path.join(src, "torrents.jsonl"), "a", encoding="utf-8") as f:  # 2.x journal events after the "n" lines
    f.write(json.dumps({"t": "n", "r": {"ih": H(5), "name": "Deleted later", "size": 1, "files": [["y.mkv", 1]]}}) + "\n")
    f.write(json.dumps({"t": "d", "ih": H(5)}) + "\n")                    # deleted in the source: not imported
    f.write(json.dumps({"t": "h", "ih": H(2), "s": 12, "p": 2, "a": T0 + 10, "g": "scrape", "n": 1}) + "\n")
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
assert (t["new"], t["already_present"], t["blocked"], t["health_events"]) == (3, 1, 1, 1), t
assert t["categories_translated"] == 2 and rep["peers"]["new"] == 1 and rep["rules"]["new"] == 1, rep
assert rep["blocklist"]["new_patterns"] == 1 and rep["nodes"]["new"] == 2 and rep["hashes"]["new"] == 1, rep
assert "stats.json" in rep["not_imported"] and rep["backup"] is None
assert snapshot(dst) == dst_before and snapshot(src) == src_before
print("dry run: correct plan, nothing written: OK")

# ---------------------------------------------------------------- apply
dst2 = tempfile.mkdtemp(); shutil.rmtree(dst2); shutil.copytree(dst, dst2)  # pristine copy for the undo test
rep = quiet(M.plan_and_import, src, dst, apply=True)
assert rep["torrents"]["new"] == 3 and rep["torrents"]["imported"] == 3 and rep["torrents"]["health_events_applied"] == 1
assert os.path.exists(os.path.join(rep["backup"], "imported.txt"))
assert {k: v for k, v in snapshot(src).items() if not k.startswith("IMPORTED-")} == src_before   # source only read
st = quiet(Store, dst)
assert len(st.torrents) == 5 and {H(2), H(3), H(6)} <= set(st.torrents) and H(4) not in st.torrents and H(5) not in st.torrents
assert st.get(H(1))["seeders"] == 99                                         # destination wins
g2 = st.get(H(2))
assert g2["category"] == "Images" and st.get(H(6))["category"] == "Documents" and g2["seeders"] == 12
assert len(g2["hh"]) == 5, g2["hh"]                                          # 4 carried in the record + 1 "h" event
assert st.legacy_converted == 0                                              # imported records are already English
assert st.wait_index(20)
assert st.search({"q": "album"})["total"] == 1                               # indexed by the service in the background
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

# ---------------------------------------------------------------- --undo removes what was imported (also after the service ran)
rep = quiet(M.plan_and_import, src, dst2, apply=True)
st = quiet(Store, dst2); mk(st, 11, "Indexed after the import", [("n.mkv", MB)]); st.close()     # the service ran meanwhile
u = quiet(M.main, ["--undo", rep["backup"], "--dst", dst2, "--force"])
assert u["torrents_removed"] == 3 and u["hashes_removed"] == 1 and u["peers_removed"] == 1, u
assert sorted(u["restored"]) == ["blocklist.txt", "hidden_rules.json", "nodes.json"], u
st = quiet(Store, dst2)
assert set(st.torrents) == {H(1), H(10), H(11)} and "ee" * 20 not in st.hashes and not st.peers.by_ih
assert st.wait_index(20) and st.search({"q": "album"})["total"] == 0 and st.search({"q": "indexed"})["total"] == 1
st.close()
assert open(os.path.join(dst2, "blocklist.txt"), encoding="utf-8").read() == dst_before["blocklist.txt"].decode()
print("--undo: imported torrents, hashes and peers removed; newer torrents kept; small files restored: OK")

# ---------------------------------------------------------------- the service must be stopped
holder = subprocess.Popen([sys.executable, "-c", f"f=open({os.path.join(dst, 'meta.log')!r}); import time; time.sleep(30)"])
time.sleep(0.5)
try:
    assert holder.pid in M.holders_of([os.path.join(dst, "meta.log")])
    try:
        quiet(M.main, ["--src", src, "--dst", dst, "--apply"]); raise AssertionError("should refuse")
    except SystemExit as e:
        assert "stop the service" in str(e)
finally:
    holder.kill()
print("refuses to write while another process has the data open: OK")

# ---------------------------------------------------------------- rollback to 2.x and back
rep = quiet(M.main, ["--to-legacy", "--apply", "--dst", dst, "--force"])
assert rep["torrents"] == 5 and '"category":"Vídeo"' in open(os.path.join(dst, "torrents.jsonl"), encoding="utf-8").read()
assert not os.path.exists(os.path.join(dst, "meta.log")) and os.path.isdir(rep["moved_to"])
st = quiet(Store, dst); assert st.legacy_converted >= 2 and st.get(H(1))["category"] == "Video" and len(st.torrents) == 5; st.close()
assert os.path.exists(os.path.join(dst, "meta.log")) and os.path.exists(os.path.join(dst, "torrents.jsonl.v3"))
print("--to-legacy for rolling back to 2.x, and 4.x converts it back: OK")

# ---------------------------------------------------------------- very old source format (torrents.json)
src3, dst3 = tempfile.mkdtemp(), tempfile.mkdtemp()
json.dump({H(30): {"ih": H(30), "name": "Old Movie", "size": 1, "file_count": 1, "files": [["a.mkv", 1]], "category": "Vídeo",
                   "seeders": 2, "peers": 1}, "zz": {"name": "bad"}}, open(os.path.join(src3, "torrents.json"), "w"))
rep = quiet(M.plan_and_import, src3, dst3, apply=True)
assert rep["torrents"]["new"] == 1 and rep["torrents"]["invalid"] == 1
st = quiet(Store, dst3); assert st.get(H(30))["category"] == "Video"; st.close()
print("source in the very old torrents.json format: OK")
print("ALL OK (migrate_es)")
