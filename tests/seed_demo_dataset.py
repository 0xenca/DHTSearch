"""Generates a COHERENT demo dataset in a directory: N synthetic torrents + 40 days of history in all 3 tiers."""
import hashlib, math, os, random, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import stats as S
from demo import _fake, PEER_POOL, PEER_REGULARS, CLIENTS
from store import Store

def seed(d, n=3000, days=40, seed_=11):
    rng = random.Random(seed_)
    st = Store(d)
    now = int(time.time())
    t0 = now - days * 86400
    # torrents spread over time, with health history
    times = sorted(rng.uniform(t0, now - 60) for _ in range(n))
    for i, ts in enumerate(times):
        ih = hashlib.sha1(f"seed{i}".encode()).hexdigest()
        name, files = _fake(rng)
        st.hashes[ih] = {"first_seen": int(ts), "last_seen": int(ts), "hits": 1, "src": rng.choice(["announce", "get_peers", "bep51"]), "attempts": 0, "status": "pending"}
        st.save_torrent(ih, {"name": name, "size": sum(s for _, s in files), "files": files, "piece_length": 262144 * rng.choice([1, 2, 4, 8]),
                             "created": int(ts - rng.randint(0, 86400 * 900)), "comment": "synthetic torrent (demo mode)", "created_by": "demo",
                             "trackers": ["udp://tracker.opentrackr.org:1337/announce"]})
        rec = st.torrents[ih]
        rec["indexed_at"] = int(ts)                      # first_seen (on disk) comes from st.hashes above
        dead = rng.random() < 0.08
        seeds = 0 if dead else int(rng.lognormvariate(2.5, 1.5)); k = 6 if dead else rng.choice([0, 1, 3, 6, 10]); nrep = 3 if dead else rng.choice([1, 2, 3, 4])
        for j in range(k):
            at = int(now - (k - j) * 21600 - rng.randint(0, 900))
            sj = 0 if dead else max(0, int(seeds * rng.uniform(0.5, 1.5))); st.update_health(ih, sj, int(sj * 0.6), "scrape", nrep, at=at)
        src = "scrape" if (dead or rng.random() < 0.8) else "swarm"
        st.update_health(ih, seeds, int(seeds * .6), src, nrep if src == "scrape" else 0, at=now - rng.randint(0, 20000))
        if not dead:                                   # synthetic peers (documentation IPs, RFC 5737)
            ips = rng.sample(PEER_POOL, min(40, max(1, (seeds + int(seeds * .6)) // 3)))
            if rng.random() < 0.35:
                ips += rng.sample(PEER_REGULARS, rng.randint(1, 4))
            for ip in set(ips):
                role = rng.choice([1, 1, 0, -1]) if seeds else rng.choice([0, -1])
                st.peers.note(ih, ip, rng.randint(1025, 65000), role, rng.choice(CLIENTS) if role != -1 else "",
                              "c" if role != -1 else rng.choice("da"), now=now - rng.randint(0, 86400 * 5))
    st.checkpoint()
    # history: cumulative counters consistent with the N torrents and their indexing dates
    cum = lambda t: sum(1 for x in times if x <= t)
    files_total = st.analytics()["files"]
    st.stats.counters.update({"hashes_new": n * 90, "metadata_ok": n, "probe_timeouts": n * 9, "stale_dropped": n * 80, "bep51_queries": n * 40, "bep51_replies": n * 4})
    idx_times = times
    step_raw, agg = 10, {name: S._Agg(step) for name, step, _ in S.TIERS[1:]}
    j = 0
    life_rx = life_tx = 0
    for t in range(t0, now - 5, 10):
        while j < len(idx_times) and idx_times[j] <= t: j += 1
        day = (t - t0) / 86400
        wave = 0.55 + 0.45 * math.sin(t / 5400.0) + 0.1 * math.sin(t / 700.0)
        rx = 60000 * wave + rng.uniform(0, 8000); tx = 30000 * wave + rng.uniform(0, 4000)
        life_rx += rx * 10; life_tx += tx * 10
        frac = j / n
        p = {"t": t, "rx_bps": round(rx), "tx_bps": round(tx), "dht_nodes": int(360 + 25 * math.sin(t / 9000.0) + rng.randint(-8, 8)),
             "peers": int(20 + 15 * wave + rng.randint(0, 6)), "connections": int(60 + 40 * wave + rng.randint(0, 20)), "probes": 150,
             "pending": int(min(20000, 300 + day * 700)), "torrents": j, "files": int(files_total * frac), "hashes": int(n * 90 * (t - t0) / (now - t0)),
             "ok": int(n * frac), "fail": int(n * 9 * frac), "drop": int(n * 80 * (t - t0) / (now - t0)), "bq": int(n * 40 * (t - t0) / (now - t0)),
             "br": int(n * 4 * (t - t0) / (now - t0)), "verified": int(j * 0.8), "rxb": int(life_rx), "txb": int(life_tx)}
        if now - t <= 86400: st.stats.tiers["raw"].append(p)
        for name, step, keep in S.TIERS[1:]:
            out = agg[name].add(p)
            if out and out["t"] >= now - keep:
                st.stats.tiers[name].append(out); st.stats._append_file(name, out)
    st.stats.life.update({"first_start": t0, "uptime_s": (now - t0) * 0.97, "sessions": 14, "rx_bytes": int(life_rx), "tx_bytes": int(life_tx)})
    for i in range(20000): st.note_node(f"11.{i >> 8 & 255}.{i & 255}.{i % 250 + 1}:6881")
    for i in range(6000): st.note_peer(f"12.{i >> 8 & 255}.{i & 255}.{i % 250 + 1}")
    st._raw_dirty = True
    st.close()
    return st

if __name__ == "__main__":
    d = sys.argv[1]; n = int(sys.argv[2]) if len(sys.argv) > 2 else 3000
    t = time.time(); seed(d, n); print(f"demo dataset in {d}: {n} torrents, generated in {time.time() - t:.0f}s"); print(sorted(os.listdir(d)))
