"""Tests for stats.py: HyperLogLog, multi-resolution aggregation, persistence and migration."""
import json, os, random, sys, tempfile, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import stats as S
from stats import HLL, Stats

# --- HyperLogLog: accuracy and persistence
h = HLL()
for i in range(200_000): h.add(f"10.{i >> 16}.{(i >> 8) & 255}.{i & 255}:{6881 + i % 7}")
est = h.count(); err = abs(est - 200_000) / 200_000
assert err < 0.03, (est, err)
before = h.count(); h._cache = (0, 0)
for i in range(1000): h.add(f"10.0.0.{i % 200}:{6881 + (i % 200) % 7}")   # the SAME strings: must not inflate
assert h.count() == before
est = h.count()
h2 = HLL.from_b64(h.to_b64()); h2._cache = (0, 0)
assert h2.count() == est or abs(h2.count() - est) <= 1
small = HLL()
for i in range(500): small.add(f"x{i}")
assert abs(small.count() - 500) <= 15, small.count()
print(f"HLL: 200,000 unique -> estimated {est} ({err:.2%} error) | 500 -> {small.count()} | serialized size {len(h.to_b64())//1024} KiB: OK")

# --- multi-resolution history + persistence across 'restarts'
d = tempfile.mkdtemp()
t0 = int(time.time()) - 3 * 86400 - 1500
st = Stats(d)
for i in range(3 * 8640):                                            # 3 days of points every 10 s
    t = t0 + i * 10
    st.tiers["raw"].append({"t": t})                                 # (trimmed to 24 h in _trim_raw)
    p = {"t": t, "rx_bps": 1000 + (i % 100), "tx_bps": 500, "torrents": i // 10, "hashes": i * 3, "ok": i // 20, "fail": i // 5}
    for name, step, keep in S.TIERS[1:]:
        out = st._agg[name].add(p)
        if out:
            st.tiers[name].append(out); st._append_file(name, out)
st.tiers["raw"].clear()
now = int(time.time())
for i in range(100):                                                 # and the last 1000 s in raw via the public API
    st.record_point({"t": now - 1000 + i * 10, "rx_bps": 2000, "torrents": 5000 + i, "peers": 3})
st.add_traffic(10_000_000, 4_000_000)
for i in range(1000): st.note_peer(f"1.2.{i // 250}.{i % 250}")
st.counters["hashes_new"] = 12345
st.close()
files = sorted(os.listdir(d)); print("files:", files)
assert {"stats.json", "history_raw.json", "history_m5.jsonl", "history_h1.jsonl"} <= set(files)

st2 = Stats(d)                                                       # 'restart'
snap = st2.snapshot()
assert st2.counters["hashes_new"] == 12345
assert snap["rx_bytes"] == 10_000_000 and snap["tx_bytes"] == 4_000_000, snap
assert snap["sessions"] == 2 and snap["first_start"] == st.life["first_start"]
assert abs(snap["peers_unique"] - 1000) <= 30, snap["peers_unique"]
assert snap["uptime_total"] >= 0
cov = snap["coverage"]; print("coverage after restart:", {k: v["points"] for k, v in cov.items()})
assert cov["raw"]["points"] >= 100 and cov["m5"]["points"] > 500 and cov["h1"]["points"] >= 60
# ranges: 1 h -> raw, 7 d -> m5, 90 d -> h1; never more than ~600 points
for secs, tier in ((3600, "raw"), (7 * 86400, "m5"), (90 * 86400, "h1")):
    pts = st2.get_history(secs)
    assert st2.tier_for(secs) == tier and 0 < len(pts) <= 620, (secs, len(pts))
    assert all(pts[i]["t"] <= pts[i + 1]["t"] for i in range(len(pts) - 1))
m5 = st2.get_history(3 * 86400)
assert all("ok" in p and "rx_bps" in p for p in m5[:50])
print("ranges and tiers: OK")

# --- aggregation: gauges = mean, cumulative = last value
a = S._Agg(300)
outs = [a.add({"t": 600 + i, "rx_bps": 100 * (k % 2), "torrents": i}) for k, i in enumerate(range(0, 300, 10))]
out = a.add({"t": 900, "rx_bps": 1, "torrents": 999})
assert out and out["rx_bps"] == 50.0 and out["torrents"] == 290 and out["t"] == 750, out
print("aggregation (mean/last): OK")

# --- clock going backwards: history stays monotonic
d3 = tempfile.mkdtemp(); c = Stats(d3); base = int(time.time())
c.record_point({"t": base, "peers": 1}); c.record_point({"t": base + 10, "peers": 1}); c.record_point({"t": base - 500, "peers": 1})
ts = [p["t"] for p in c.tiers["raw"]]
assert ts == sorted(ts), ts
print("clock going backwards: OK")

# --- downsample does not join points across a gap
pts = [{"t": 1000 + i * 10, "rx_bps": 1} for i in range(100)] + [{"t": 5000 + i * 10, "rx_bps": 9} for i in range(100)]
ds = S.downsample(pts, 20, 10)
assert len(ds) <= 30
assert all(p["rx_bps"] in (1.0, 9.0) for p in ds), [p["rx_bps"] for p in ds]      # no mixed 1↔9 average
print("downsample respects gaps: OK")

# --- migration: OLD stats.json (history + counters + first_start inside)
d2 = tempfile.mkdtemp()
old_hist = [{"t": now - 5000 + i * 10, "rx_bps": 7, "torrents": i, "hashes": i * 2} for i in range(400)]
json.dump({"history": old_hist, "counters": {"hashes_new": 999, "metadata_ok": 50}, "sources": {"bep51": 10}, "first_start": now - 86400}, open(d2 + "/stats.json", "w"))
m = Stats(d2)
assert m.counters["hashes_new"] == 999 and m.life["first_start"] == now - 86400
assert len(m.tiers["raw"]) == 400 and len(m.tiers["m5"]) >= 10, (len(m.tiers["raw"]), len(m.tiers["m5"]))   # backfill from raw
print(f"old stats.json migration: raw={len(m.tiers['raw'])} m5(backfill)={len(m.tiers['m5'])}: OK")
# --- long range right after upgrading (m5/h1 empty): completed with the finer tier, not empty
d4 = tempfile.mkdtemp(); f = Stats(d4)
b = int(time.time()) - 2000
for i in range(200): f.record_point({"t": b + i * 10, "torrents": i, "peers": 1})
f.tiers["m5"].clear(); f.tiers["h1"].clear()
assert len(f.get_history(7 * 86400)) > 100 and len(f.get_history(90 * 86400)) > 100
# tier mix: m5 up to 1000 s ago + recent raw -> no duplicate of the same instant
f.tiers["m5"].extend({"t": b - 5000 + i * 300, "peers": 2, "torrents": 0} for i in range(20))
h = f.get_history(7 * 86400, max_points=10_000); ts = [p["t"] for p in h]
assert ts == sorted(ts) and len(ts) == len(set(ts)) and h[0]["t"] < b, (ts[:3], len(ts))
print("long ranges with incomplete tiers: OK")
# --- repair: ALREADY migrated stats.json with only 8 min of life but a 10 s history of almost 18 h
d5 = tempfile.mkdtemp(); nowr = int(time.time())
json.dump({"counters": {}, "sources": {}, "first_start": nowr - 18 * 3600, "life": {"first_start": nowr - 18 * 3600, "uptime_s": 480.0, "sessions": 1,
           "rx_bytes": 284_000_000, "tx_bytes": 62_000_000}}, open(d5 + "/stats.json", "w"))
json.dump([{"t": nowr - 64000 + i * 10, "rx_bps": 20000, "tx_bps": 5000} for i in range(6400)], open(d5 + "/history_raw.json", "w"))
r = Stats(d5); r.close()
assert 60000 < r.life["uptime_s"] < 66000, r.life["uptime_s"]
assert 1.2e9 < r.life["rx_bytes"] < 1.3e9 and r.life["tx_bytes"] > 3e8, (r.life["rx_bytes"], r.life["tx_bytes"])
up1 = r.life["uptime_s"]; r2 = Stats(d5); assert r2.life["repaired"] and abs(r2.life["uptime_s"] - up1) < 5     # once only
# fresh install: untouched
d6 = tempfile.mkdtemp(); n = Stats(d6); assert n.life["uptime_s"] == 0
print("uptime/traffic repair after migration: OK")
print("ALL OK (stats)")
