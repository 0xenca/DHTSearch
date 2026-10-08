"""v4 storage: cold data from meta.log, fixed history/breakdown slots, checkpoint concurrent with writes, crash recovery,
deletions, LSM index (flushes, tiered merges, reopen, damage), bounded memory, write volume, peers and hash states.
Run: python tests/test_memory.py"""
import hashlib, ipaddress, json, os, shutil, sys, tempfile, threading, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import numpy as np
import segindex
import store as S
from colstore import COLUMNS
from records import HH_MAX

MB = 1 << 20
def H(i): return hashlib.sha1(str(i).encode()).hexdigest()
def det(s, cs=1, at=None): return {"at": at or int(time.time()), "tr": [{"u": "udp://t:1/announce", "s": s, "l": 1, "r": 3}], "cs": cs, "cp": 2,
                                   "ci": 0, "dht": 5, "md": 1}
def wchar(): return int(dict(l.split(": ") for l in open("/proc/self/io").read().splitlines())["wchar"])

# ---------------------------------------------------------------- cold data comes back from meta.log
d = tempfile.mkdtemp()
st = S.Store(d)
files = [(f"Big Set/disc {j // 100}/track {j:04d} {'needle' if j == 2500 else 'x'}.flac", 1000 + j) for j in range(3000)]
assert st.save_torrent(H(1), {"name": "Big Set", "size": sum(s for _, s in files), "files": files, "piece_length": 65536,
                              "comment": "a comment", "created_by": "mktorrent", "trackers": ["udp://own:1/announce"], "created": 1600000000})
st.save_torrent(H(2), {"name": "Small one", "size": 5, "files": [("only file.mkv", 5)]})
assert "files" not in st.torrents[H(1)].keys(), "cold fields are not in memory"
g = st.get(H(1))
assert len(g["files"]) == 3000 and g["files"][2500][0].endswith("needle.flac") and g["comment"] == "a comment"
assert g["piece_length"] == 65536 and g["trackers"] == ["udp://own:1/announce"] and g["num_pieces"] > 0 and g["created"] == 1600000000
print("cold fields read back from meta.log: OK")

# ---------------------------------------------------------------- history (40 in a fixed slot) and breakdown
base = int(time.time()) - 100 * 3600
for k in range(50):
    st.update_health(H(1), k, 1, "scrape", 3, det(k, at=base + k * 3600), at=base + k * 3600)
st.update_health(H(1), 99, 7, "swarm", 0, at=base + 60 * 3600)            # no breakdown: hd stays the last one
g = st.get(H(1))
assert len(g["hh"]) == HH_MAX and g["hh"][-1][1] == 99 and g["hh"][0][1] == 11 and g["hd"]["tr"][0]["s"] == 49 and g["seed_ok_at"]
hist_size = os.path.getsize(os.path.join(d, "health.bin"))
for k in range(20):
    st.update_health(H(2), k, 1, "scrape", at=base + k)
assert os.path.getsize(os.path.join(d, "health.bin")) == hist_size, "updated in place: the file does not grow with measurements"
print("history (40 per torrent, fixed slot updated in place) and breakdown: OK")

# ---------------------------------------------------------------- checkpoint while the crawler keeps writing, then a crash
stop, born = False, []
def writer():
    i = 0
    while not stop:
        st.save_torrent(H(f"w{i}"), {"name": f"born during checkpoint {i}", "size": 7, "files": [(f"born{i}.txt", 7)]})
        st.update_health(H(1), 500 + i, 5, "scrape", at=base + 70 * 3600 + i)
        born.append(i); i += 1
t = threading.Thread(target=writer); t.start()
for _ in range(5):
    st.checkpoint()
stop = True; t.join()
last = st.get(H(1))
st.flush()                                                    # WAL written, but NO close: simulated crash
st2 = S.Store(d)
assert len(st2.torrents) == 2 + len(born), (len(st2.torrents), len(born))
assert st2.get(H(1))["seeders"] == last["seeders"] and st2.get(H(1))["hh"] == last["hh"]
assert st2.get(H(1))["hd"] == last["hd"] and st2.get(H(1))["hd"]["tr"][0]["u"] == "udp://t:1/announce", "tracker ids persisted"
assert st2.get(H(f"w{born[-1]}"))["name"] == f"born during checkpoint {born[-1]}"
print(f"checkpoint concurrent with {len(born)} writes + crash: nothing lost: OK")

# ---------------------------------------------------------------- deletions: O(1), the same infohash can come back
n0 = len(st2.torrents)
assert st2.delete(H("w0")) and H("w0") not in st2.torrents and len(st2.torrents) == n0 - 1
assert st2.wait_index(10) and st2.search({"q": '"born during checkpoint 0"'})["total"] == 0
assert st2.save_torrent(H("w0"), {"name": "came back", "size": 1, "files": [("again.txt", 1)]})
assert st2.get(H("w0"))["name"] == "came back" and st2.wait_index(10) and st2.search({"q": "came back"})["total"] == 1
st2.close()
st2 = S.Store(d)
assert st2.get(H("w0"))["name"] == "came back" and len(st2.torrents) == n0
st2.close()
print("delete + re-add the same infohash, across restarts: OK")

# ---------------------------------------------------------------- LSM index: flushes, tiered merges, results = brute force
old_flush = segindex.FLUSH_POSTINGS
segindex.FLUSH_POSTINGS = 400                                  # tiny delta: many segments and several merge tiers
d3 = tempfile.mkdtemp()
s3 = S.Store(d3)
words = ["alpha", "beta", "gamma", "delta", "omega", "kappa", "sigma", "theta"]
truth = {w: set() for w in words}
for i in range(3000):
    ws = [words[(i * 7 + j) % 8] for j in range(1 + i % 3)]
    for w in ws:
        truth[w].add(H(f"x{i}"))
    s3.save_torrent(H(f"x{i}"), {"name": " ".join(ws) + f" item{i}", "size": i + 1, "files": [(f"f{i}/{ws[0]}.mkv", i + 1)]})
assert s3.wait_index(60)
s3.index.merge_all_pending()
ixs = s3.index.stats()
assert 1 < ixs["segments"] < 40, ixs                           # flushed many times, then merged by tiers
for w in words:
    got = {x["ih"] for x in s3.search({"q": w, "per_page": 5000})["results"]}
    tot = s3.search({"q": w})["total"]
    assert tot == len(truth[w]) and got <= truth[w], (w, tot, len(truth[w]))
assert s3.search({"q": "alpha -beta"})["total"] == len(truth["alpha"] - truth["beta"])
assert s3.search({"q": "item299"})["total"] == 11 and s3.search({"q": "item29"})["total"] == 111   # last word = prefix
s3.close()
segindex.FLUSH_POSTINGS = old_flush
s3 = S.Store(d3)
assert s3.index.indexed_upto() == s3.cols.n, "persisted: nothing to re-index at startup"
assert s3.search({"q": "omega"})["total"] == len(truth["omega"])
s3.close()
print(f"LSM index: {ixs['segments']} segments after flushes + tiered merges; same results as brute force; persisted: OK")

# damaged segment: dropped with everything after it and re-indexed from meta.log
segs = sorted(f for f in os.listdir(os.path.join(d3, "index")) if f.endswith(".json") and f[0].isdigit())
open(os.path.join(d3, "index", segs[len(segs) // 2]), "w").write("{broken")
s3 = S.Store(d3)
assert s3.index.indexed_upto() < s3.cols.n and s3.wait_index(60)
assert s3.search({"q": "omega"})["total"] == len(truth["omega"])
s3.close()
shutil.rmtree(os.path.join(d3, "index"))                       # index lost completely
s3 = S.Store(d3); assert s3.wait_index(60) and s3.search({"q": "gamma"})["total"] == len(truth["gamma"]); s3.close()
print("index segment damaged / index deleted: rebuilt from meta.log: OK")

# ---------------------------------------------------------------- write volume per indexed torrent (SSD wear regression)
d4 = tempfile.mkdtemp()
s4 = S.Store(d4)
w0 = wchar()
for i in range(2000):
    fl = [(f"Show {i}/Season {j // 10}/Episode {j:02d} {['720p', '1080p'][j % 2]} x264.mkv", 300 * MB) for j in range(20)]
    s4.save_torrent(H(f"v{i}"), {"name": f"Show {i} complete series", "size": 6000 * MB, "files": fl, "piece_length": 1 << 20})
    s4.update_health(H(f"v{i}"), 5, 2, "scrape", 2, det(5))
s4.close()
per = (wchar() - w0) / 2000
assert per < 12_000, per                                       # 3.1 (SQLite file index): ~2 MB per torrent
print(f"bytes written per indexed torrent (20 files, metadata + index + health + checkpoint): {per / 1024:.1f} KB: OK")

# ---------------------------------------------------------------- RAM per torrent
s4 = S.Store(d4)
col_bytes = sum(np.dtype(dt).itemsize for _, dt in COLUMNS) + 20 + 8   # columns + infohash + hash-table slots
assert col_bytes < 110, col_bytes
assert s4.index.stats()["delta_postings"] == 0 and s4.load_info["how"].startswith("checkpoint")
s4.close()
print(f"RAM per torrent in columns: {col_bytes} B (+ names on disk, index on disk): OK")

# ---------------------------------------------------------------- hide rules: the hidden list is saved, a restart does not rescan
d5 = tempfile.mkdtemp(); s5 = S.Store(d5)
for i in range(50):
    s5.save_torrent(H(f"h{i}"), {"name": f"thing {i} {'secret' if i % 5 == 0 else ''}", "size": 1, "files": [(f"f{i}.mkv", 1)]})
s5.rules.add("secret", "name", "substring"); assert s5.recompute_hidden()["hidden"] == 10; s5.close()
s5 = S.Store(d5); assert len(s5.hidden) == 10 and s5.hidden_info["scanned"] == 0, s5.hidden_info
s5.save_torrent(H("h99"), {"name": "new secret", "size": 1, "files": [("a", 1)]}); s5.flush()
s5 = S.Store(d5); assert len(s5.hidden) == 11 and s5.hidden_info["scanned"] == 1, "crash: only the torrents after the save are evaluated"
s5.rules.add("thing 1", "name", "substring"); s5.close()          # rule added but never applied (crash before recompute)
s5 = S.Store(d5); assert len(s5.hidden) == 20, "the saved list belongs to other rules: recomputed"; s5.close()
print("hidden list saved with the checkpoint and tied to its rules: OK")

# ---------------------------------------------------------------- packed peers
st = S.Store(d)
ps = st.peers
ps.note(H(1), "203.0.113.5", 51413, 1, "qBittorrent", "c")
ps.note(H(1), "2001:db8::7", 6881, 0, "Transmission", "c")
ps.note(H(2), "2001:db8::7", 6881, -1, "", "d")
assert ps.count(H(1)) == 2 and set(ps.ips_of(H(1))) == {"203.0.113.5", "2001:db8::7"}
assert set(ps.find([ipaddress.ip_network("2001:db8::/32")])) == {H(1), H(2)}
assert set(ps.find([ipaddress.ip_network("203.0.113.0/29")])) == {H(1)} and not ps.find([ipaddress.ip_network("203.0.113.8/29")])
assert {ip for hits in ps.find([ipaddress.ip_network("::/0")]).values() for ip, _, _ in hits} == {"2001:db8::7"}, "IPv6 range: no IPv4 peers"
agg = {a["ip"]: a for a in ps.aggregate([H(1)])}
assert agg["2001:db8::7"]["elsewhere"] == 2 and agg["203.0.113.5"]["role"] == 1
ps.flush()
lines = [json.loads(l) for l in open(os.path.join(d, "peers.jsonl"))]
assert {"i", "a", "p", "s", "f", "l", "c", "o"} <= set(lines[-1]), "same journal format as always"
assert all(l["i"] != H(2) for l in lines), "DHT-only peers with unknown role are not written to disk (default)"
ps.compact()
ps2 = type(ps)(d)
assert ps2.entry(H(1), "2001:db8::7")["client"] == "Transmission" and ps2.n == 2
assert ps.forget([H(2)]) == 1 and ps.count(H(2)) == 0
ps.max_entries = 1
assert ps.maintenance()["peers_capped"] == 1 and ps.n == 1
print("packed peers (IPv4/IPv6, CIDR search, aggregate, journal, DHT-only not persisted, compaction, cap, forget): OK")

# ---------------------------------------------------------------- hash states: snapshot + change log
st.add_hash("ab" * 20, "announce", peer=("1.2.3.4", 5))
h = st.hashes["ab" * 20]
assert type(h).__name__ == "HashState" and h["src"] == "announce" and h.get("pe") == [["1.2.3.4", 5]] and h.get("failed_at") is None
st.flush(force=True)
log_lines = [json.loads(l) for l in open(os.path.join(d, "hashes.jsonl")) if l.strip()]
assert all(l["i"] != "ab" * 20 for l in log_lines), "pending hashes are not written on every change"
st.close()
saved = json.load(open(os.path.join(d, "hashes.json")))
assert saved["ab" * 20]["status"] == "pending" and saved["ab" * 20]["hits"] == 1, "complete snapshot on shutdown"
assert os.path.getsize(os.path.join(d, "hashes.jsonl")) == 0
st = S.Store(d)
assert st.hashes["ab" * 20]["pe"] == [["1.2.3.4", 5]] and "ab" * 20 in st.queue_peer
st.close()
print("hash states (change log while running, snapshot on shutdown): OK")
print("ALL OK (memory)")
