"""Migration from the OLD format (torrents.json + stats.json) and performance with a large catalogue."""
import json, os, random, resource, sys, tempfile, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from store import Store

now = int(time.time())
# ---------------- 1) old format exactly as the previous version wrote it
d = tempfile.mkdtemp()
old = {}
for i in range(300):
    ih = "%040x" % (0xfeed00 + i)
    files = [[f"Album {i}/{j:02d} Track.flac", 30 * 1024 * 1024] for j in range(12)]
    old[ih] = {"ih": ih, "name": f"Album {i} Remastered", "size": sum(f[1] for f in files), "file_count": 12, "files": files,
               "piece_length": 262144, "created": 1_500_000_000, "comment": "", "created_by": "", "private": False,
               "trackers": [], "seeders": i % 50, "peers": i % 7, "health_at": now - 86400, "first_seen": now - 90000,
               "last_seen": now - 90000, "indexed_at": now - 90000, "src": "bep51", "category": "Audio"}       # no exts/hh/health_src
old["%040x" % 0xbad] = {"ih": "%040x" % 0xbad, "name": "FORBIDDEN test content", "size": 1, "file_count": 1, "files": [["a.mkv", 1]],
                        "piece_length": 1, "created": 0, "comment": "", "created_by": "", "private": False, "trackers": [], "seeders": 1,
                        "peers": 1, "health_at": now, "first_seen": now, "last_seen": now, "indexed_at": now, "src": "x", "category": "Vídeo"}
json.dump(old, open(d + "/torrents.json", "w"))
json.dump({"aa" * 20: {"first_seen": now, "last_seen": now, "hits": 3, "src": "announce", "attempts": 1, "status": "pending"},
           "bb" * 20: {"first_seen": now, "last_seen": now, "hits": 1, "src": "bep51", "attempts": 1, "status": "failed", "failed_at": now}},
          open(d + "/hashes.json", "w"))
json.dump({"history": [{"t": now - 3000 + i * 10, "rx_bps": 5, "torrents": i, "hashes": i * 2} for i in range(300)],
           "counters": {"hashes_new": 3260524, "stale_dropped": 3199415, "metadata_ok": 38055, "probe_timeouts": 378758,
                        "bep51_queries": 528068, "bep51_replies": 46261},
           "sources": {"bep51": 100}, "first_start": now - 86400 * 2}, open(d + "/stats.json", "w"))
json.dump([f"1.2.3.{i}:6881" for i in range(50)], open(d + "/nodes.json", "w"))
open(d + "/blocklist.txt", "w").write("# test\nFORBIDDEN\n")

st = Store(d)
fs = sorted(os.listdir(d))
assert "meta.log" in fs and "state.bin" in fs and "torrents.json.v3" in fs and "torrents.json" not in fs, fs
assert len(st.torrents) == 300, len(st.torrents)                               # 301 - 1 purged by the blocklist
assert st.counters["blocked_purged"] == 1 and st.search({"q": "forbidden"})["total"] == 0
r = st.get("%040x" % 0xfeed05)
assert r["health_src"] == "legacy" and r["exts"] == ["flac"] and r["hh"] == [] and r["category"] == "Audio", r
a = st.analytics()
assert a["health_verified"] == 0 and a["health_unverified"] == 300 and a["torrents_no_seeds"] == 0     # unverified does NOT count as "dead"
assert st.counters["hashes_new"] == 3260524 and a["hashes_discovered"] == 3260524                     # inherited counters
assert st.stats.life["first_start"] == now - 86400 * 2 and st.stats.life["sessions"] == 1
assert len(st.stats.tiers["raw"]) == 300 and len(st.stats.tiers["m5"]) >= 8, (len(st.stats.tiers["raw"]), len(st.stats.tiers["m5"]))
assert a["nodes_seen"] == 0 and len(st.nodes) == 50
assert st.search({"q": "album remastered ext:flac"})["total"] == 300
assert st.hashes["aa" * 20]["status"] == "pending" and len(st.queue_prio) == 1 and len(st.queue) == 0   # hits=3: back to the priority queue on restart
st.close()
st2 = Store(d); assert len(st2.torrents) == 300 and st2.stats.life["sessions"] == 2 and st2.stats.life["first_start"] == now - 86400 * 2
st2.close()
print("migration from the old format (torrents.json/stats.json/hashes/nodes/blocklist): OK")

# ---------------- 2) performance with a large catalogue
random.seed(7)
words = [f"w{i}" for i in range(4000)] + ["movie", "season", "complete", "linux", "ubuntu", "album", "flac", "x264", "1080p", "collection"]
weights = [1 / (1 + i) ** 0.9 for i in range(len(words))]
N = int(os.environ.get("N", 100_000))
import shutil
cache = f"/tmp/corpus3_{N}.jsonl"
d2 = tempfile.mkdtemp()
t0 = time.time()
if os.path.exists(cache):
    shutil.copy(cache, d2 + "/torrents.jsonl")
else:
  with open(d2 + "/torrents.jsonl", "w", encoding="utf-8") as f:
      for i in range(N):
          nm = " ".join(random.choices(words, weights, k=random.randint(2, 6)))
          files = [[f"{nm.replace(' ', '.')}/{' '.join(random.choices(words, weights, k=3))}.{random.choice(['mkv','mp4','flac','pdf','iso','srt'])}",
                    random.randint(10**5, 5 * 10**9)] for _ in range(random.randint(1, 40))]
          rec = {"ih": "%040x" % i, "name": nm, "size": sum(x[1] for x in files), "file_count": len(files), "files": files, "piece_length": 262144,
                 "created": now - random.randint(0, 10**8), "comment": "", "created_by": "", "private": False, "trackers": [],
                 "seeders": int(random.lognormvariate(1.5, 1.5)), "peers": 3, "health_at": now - random.randint(0, 10**5),
                 "health_src": "scrape", "hh": [], "first_seen": now, "last_seen": now, "indexed_at": now - random.randint(0, 10**6), "src": "bep51",
                 "category": "Video", "exts": ["mkv"]}
          f.write(json.dumps({"t": "n", "r": rec}, separators=(",", ":")) + "\n")
  sz = os.path.getsize(d2 + "/torrents.jsonl") / 1e6
  shutil.copy(d2 + "/torrents.jsonl", cache)
sz = os.path.getsize(d2 + "/torrents.jsonl") / 1e6
print(f"synthetic corpus: {N:,} torrents, {sz:.0f} MB journal, generated in {time.time() - t0:.0f}s")
t0 = time.time(); big = Store(d2); load = time.time() - t0
t1 = time.time(); big.wait_index(); idx_s = time.time() - t1
rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
print(f"first start (3.x journal -> 4.x storage): {load:.1f}s | index built in the background in {idx_s:.1f}s: {big.index.stats()}")
big.close(); t0 = time.time(); big = Store(d2); load2 = time.time() - t0
rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
print(f"normal start (checkpoint): {load2:.2f}s ({big.load_info['how']}) | peak RAM of the whole test ≈ {rss:.0f} MB")
def timeit(label, params, reps=5):
    ts = []
    for _ in range(reps):
        t = time.perf_counter(); r = big.search(params); ts.append((time.perf_counter() - t) * 1000)
    print(f"  {label:<44} {min(ts):7.1f} ms (best)  {sorted(ts)[len(ts) // 2]:7.1f} ms (median)  total={r['total']:,}")
    return sorted(ts)[len(ts) // 2]
worst = 0
for label, p in [("empty (popular) + facets", {"q": ""}), ("one common word: linux", {"q": "linux"}), ("two words: movie season", {"q": "movie season"}),
                 ("prefix: mov", {"q": "mov"}), ('phrase: "movie season"', {"q": '"movie season"'}), ("OR: linux OR ubuntu", {"q": "linux OR ubuntu"}),
                 ("ext:iso size>1gb seeders>5", {"q": "ext:iso size>1gb seeders>5"}), ("rare word + filters", {"q": "w3500 ext:mkv"}),
                 ("sort by size", {"q": "linux", "sort": "size"}), ("cat + age", {"q": "age<7d cat:video"})]:
    worst = max(worst, timeit(label, p))
t = time.perf_counter(); big.analytics(); print(f"  full analytics(){'':<28} {(time.perf_counter() - t) * 1000:7.1f} ms")
t = time.perf_counter(); big.suggest("mov"); print(f"  autocomplete 'mov'{'':<27} {(time.perf_counter() - t) * 1000:7.1f} ms")
t = time.perf_counter(); big.related("%040x" % 5); print(f"  related{'':<37} {(time.perf_counter() - t) * 1000:7.1f} ms")
# writing while compacting and the cost of persisting an event
t = time.perf_counter()
for i in range(2000): big.update_health("%040x" % i, 5, 2, "scrape")
print(f"  2000 health events (slot + WAL){'':<12} {(time.perf_counter() - t) * 1000:7.1f} ms  ({(time.perf_counter() - t) * 1000 / 2000:.3f} ms/event)")
t = time.perf_counter(); big.checkpoint(); print(f"  checkpoint (state.bin){'':<22} {(time.perf_counter() - t):7.2f} s  (without blocking writes)")
big.close()
assert worst < 900, f"search too slow: {worst:.0f} ms"
print("performance: OK")
