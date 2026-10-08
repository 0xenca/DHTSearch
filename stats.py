"""
PERSISTENT statistics (survive restarts and deployments).

  · cumulative counters             -> stats.json
  · lifetime totals                 -> stats.json  (uptime, sessions, cumulative traffic)
  · unique items seen (peers, nodes)-> stats.json  (HyperLogLog: ~0.8 % error, fixed size, unbounded)
  · history in 3 resolutions        -> history_raw.jsonl (10 s · 24 h), history_m5.jsonl (5 min · 30 d), history_h1.jsonl (1 h · 2 years)

History points carry GAUGES (instantaneous values: averaged when aggregating) and cumulative COUNTERS
(the last value is taken). With cumulative counters the rate can be derived at any resolution.
"""
import base64
import hashlib
import json
import math
import os
import threading
import time
import zlib
from collections import Counter, deque

from jsonio import atomic_write, load_json, read_jsonl

GAUGES = ("rx_bps", "tx_bps", "dht_nodes", "peers", "connections", "probes", "pending")
CUMULATIVE = ("torrents", "files", "hashes", "ok", "fail", "drop", "bq", "br", "rxb", "txb", "verified")
# name, step (s), retention (s)
TIERS = (("raw", 10, 24 * 3600), ("m5", 300, 30 * 86400), ("h1", 3600, 730 * 86400))
_POW = [2.0 ** -i for i in range(80)]


class HLL:
    """HyperLogLog (p=14 -> 16 KiB of registers, typical error ~0.8 %)."""

    def __init__(self, p=14):
        self.p = p
        self.m = 1 << p
        self.reg = bytearray(self.m)
        self._cache = (0.0, 0)

    def add(self, item: str):
        h = int.from_bytes(hashlib.blake2b(item.encode("utf-8", "ignore"), digest_size=8).digest(), "big")
        idx = h >> (64 - self.p)
        w = h & ((1 << (64 - self.p)) - 1)
        rank = (64 - self.p) - w.bit_length() + 1
        if rank > self.reg[idx]:
            self.reg[idx] = rank

    def count(self) -> int:
        now = time.time()
        if now - self._cache[0] < 5:
            return self._cache[1]
        m = self.m
        alpha = 0.7213 / (1 + 1.079 / m)
        est = alpha * m * m / sum(_POW[r] for r in self.reg)
        zeros = self.reg.count(0)
        if est <= 2.5 * m and zeros:                  # small-range correction (linear counting)
            est = m * math.log(m / zeros)
        self._cache = (now, int(round(est)))
        return self._cache[1]

    def to_b64(self) -> str:
        return base64.b64encode(zlib.compress(bytes(self.reg), 6)).decode("ascii")

    @classmethod
    def from_b64(cls, s, p=14):
        h = cls(p)
        try:
            raw = zlib.decompress(base64.b64decode(s))
            if len(raw) == h.m:
                h.reg = bytearray(raw)
        except Exception:
            pass
        return h


class _Agg:
    """Aggregates 10 s points into `step`-second buckets (gauges: mean; cumulative: last value)."""

    def __init__(self, step):
        self.step = step
        self.start = None
        self.n = 0
        self.sums = Counter()
        self.last = {}

    def add(self, p):
        b = p["t"] // self.step * self.step
        if self.start is not None and b < self.start:      # clock going backwards: stay in the current bucket
            b = self.start
        out = None
        if self.start is not None and b != self.start:
            out = self.finish()
        if self.start is None or out is not None:
            self.start = b
        self.n += 1
        for g in GAUGES:
            if g in p and p[g] is not None:
                self.sums[g] += p[g]
        for c in CUMULATIVE:
            if c in p:
                self.last[c] = p[c]
        return out

    def finish(self):
        if not self.n:
            return None
        pt = {"t": self.start + self.step // 2}
        for g in GAUGES:
            if g in self.sums:
                pt[g] = round(self.sums[g] / self.n, 1)
        pt.update(self.last)
        self.n = 0
        self.sums = Counter()
        self.last = {}
        return pt


def downsample(pts, max_points, base_step):
    """Reduces to at most ~max_points by grouping consecutive points, without grouping across gaps
    (restarts), so that charts can keep breaking the line there."""
    if len(pts) <= max_points:
        return pts
    g = math.ceil(len(pts) / max_points)
    gap = max(base_step * 4, 60)
    out, grp = [], []

    def flush():
        if not grp:
            return
        agg = {"t": grp[-1]["t"]}
        for k in GAUGES:
            vals = [p[k] for p in grp if k in p and p[k] is not None]
            if vals:
                agg[k] = round(sum(vals) / len(vals), 1)
        for k in CUMULATIVE:
            for p in reversed(grp):
                if k in p:
                    agg[k] = p[k]
                    break
        out.append(agg)
        grp.clear()

    prev_t = None
    for p in pts:
        if grp and (len(grp) >= g or (prev_t is not None and p["t"] - prev_t > gap)):
            flush()
        grp.append(p)
        prev_t = p["t"]
    flush()
    return out


class Stats:
    def __init__(self, data_dir):
        self.dir = data_dir
        self.lock = threading.RLock()
        st = load_json(os.path.join(data_dir, "stats.json"), {})
        self.counters = Counter(st.get("counters", {}))
        self.sources = Counter(st.get("sources", {}))
        life = st.get("life", {})
        self.life = {"first_start": st.get("first_start") or int(time.time()), "uptime_s": 0.0, "sessions": 0,
                     "rx_bytes": 0, "tx_bytes": 0}
        self.life.update(life)
        self.life["sessions"] += 1
        self.session_start = time.time()
        self._last_tick = self.session_start
        self.hll_peers = HLL.from_b64(st["hll_peers"]) if st.get("hll_peers") else HLL()
        self.hll_nodes = HLL.from_b64(st["hll_nodes"]) if st.get("hll_nodes") else HLL()

        # history. raw (10 s, 24 h) is APPENDED to history_raw.jsonl (it used to be rewritten whole every minute:
        # ~3 GB/day of writes); older formats: history_raw.json, or stats.json["history"]
        self._paths = {"raw": os.path.join(data_dir, "history_raw.jsonl"), "m5": os.path.join(data_dir, "history_m5.jsonl"),
                       "h1": os.path.join(data_dir, "history_h1.jsonl")}
        if os.path.exists(self._paths["raw"]):
            raw = self._load_tier("raw", TIERS[0][2])
        else:
            raw = load_json(os.path.join(data_dir, "history_raw.json"), None)
            if raw is None:
                raw = st.get("history", [])
            with open(self._paths["raw"], "w", encoding="utf-8") as f:
                for pt in raw:
                    f.write(json.dumps(pt, separators=(",", ":")) + "\n")
        self.tiers = {"raw": deque(raw)}
        for name, step, keep in TIERS[1:]:
            self.tiers[name] = deque(self._load_tier(name, keep))
        self._agg = {name: _Agg(step) for name, step, _ in TIERS[1:]}
        self._files = {}
        self._raw_dirty = False
        self._last_flush = {"meta": 0.0, "raw": 0.0}
        self._trim_raw()
        self._repair_life()
        for name, step, keep in TIERS[1:]:                # first start after the upgrade: backfill from raw
            if not self.tiers[name] and self.tiers["raw"]:
                self._backfill(name)

    def _repair_life(self):
        """Once only: if the 10 s history covers MORE time than was recorded as uptime (typical when migrating from a
        version without lifetime totals), uptime and traffic are rebuilt from it. That way "availability" and the
        averages are not absurd right after upgrading. (Anything older than the last 24 h cannot be recovered.)"""
        if self.life.get("repaired"):
            return
        self.life["repaired"] = True
        pts = list(self.tiers["raw"])
        up = rx = tx = 0.0
        for a, b in zip(pts, pts[1:]):
            dt = b.get("t", 0) - a.get("t", 0)
            if 0 < dt <= 60:
                up += dt
                rx += (b.get("rx_bps") or 0) * dt
                tx += (b.get("tx_bps") or 0) * dt
        if up > self.life["uptime_s"] * 1.5:
            self.life["uptime_s"] = up
            self.life["rx_bytes"] = max(self.life["rx_bytes"], int(rx))
            self.life["tx_bytes"] = max(self.life["tx_bytes"], int(tx))

    # ------------------------------------------------------------- history
    def _load_tier(self, name, keep):
        cutoff = time.time() - keep
        pts = [p for p in read_jsonl(self._paths[name]) if isinstance(p, dict) and p.get("t", 0) >= cutoff]
        return pts

    def _backfill(self, name):
        agg = _Agg(dict((n, s) for n, s, _ in TIERS)[name])
        for p in list(self.tiers["raw"]):
            out = agg.add(p)
            if out:
                self.tiers[name].append(out)
                self._append_file(name, out)

    def _append_file(self, name, pt):
        f = self._files.get(name)
        if f is None:
            f = self._files[name] = open(self._paths[name], "a", encoding="utf-8")
        f.write(json.dumps(pt, separators=(",", ":")) + "\n")        # buffered: written in 8 KB blocks, flushed every 5 min

    def _trim_raw(self):
        cutoff = time.time() - TIERS[0][2]
        raw = self.tiers["raw"]
        while raw and raw[0].get("t", 0) < cutoff:
            raw.popleft()

    def record_point(self, p: dict):
        """Adds a 10 s point and aggregates it into the coarser resolutions."""
        with self.lock:
            self.tick()
            p = dict(p)
            raw = self.tiers["raw"]
            if raw and p["t"] < raw[-1]["t"]:              # history is always monotonic even if the clock goes back
                p["t"] = raw[-1]["t"]
            p["rxb"], p["txb"] = self.life["rx_bytes"], self.life["tx_bytes"]
            self.tiers["raw"].append(p)
            self._trim_raw()
            self._append_file("raw", p)
            for name, step, keep in TIERS[1:]:
                out = self._agg[name].add(p)
                if out:
                    self.tiers[name].append(out)
                    self._append_file(name, out)
                    cutoff = time.time() - keep
                    while self.tiers[name] and self.tiers[name][0].get("t", 0) < cutoff:
                        self.tiers[name].popleft()

    def tier_for(self, seconds):
        return "raw" if seconds <= 24 * 3600 else "m5" if seconds <= 30 * 86400 else "h1"

    def get_history(self, seconds=3600, max_points=600):
        """Points of the requested range. The tier suited to the range is used and, if the coarse tier does not reach the
        present yet (e.g. right after upgrading), it is completed with the finer tiers so the chart is not empty."""
        seconds = max(60, min(int(seconds), 730 * 86400))
        order = [n for n, _, _ in TIERS]                       # raw, m5, h1
        steps = {n: st for n, st, _ in TIERS}
        name = self.tier_for(seconds)
        cutoff = time.time() - seconds
        pts, last = [], cutoff
        with self.lock:
            for tier in reversed(order[: order.index(name) + 1]):     # from coarsest to finest
                add = [p for p in self.tiers[tier] if p.get("t", 0) >= cutoff and p["t"] > last]
                if add:
                    pts += add
                    last = add[-1]["t"] + steps[tier] / 2         # later points come from the finer tier
        return downsample(pts, max_points, steps[name])

    def coverage(self):
        """How much history there really is in each resolution (so the web knows which ranges to offer)."""
        with self.lock:
            out = {}
            for name, _, _ in TIERS:
                d = self.tiers[name]
                out[name] = {"points": len(d), "from": d[0]["t"] if d else None, "to": d[-1]["t"] if d else None}
            return out

    # ---------------------------------------------------------- lifetime totals
    def tick(self):
        now = time.time()
        dt = now - self._last_tick
        self._last_tick = now
        if 0 < dt < 120:                                  # if the process was stopped, that gap is not counted
            self.life["uptime_s"] += dt

    def add_traffic(self, rx, tx):
        if rx > 0 or tx > 0:
            self.life["rx_bytes"] += max(int(rx), 0)
            self.life["tx_bytes"] += max(int(tx), 0)

    def note_peer(self, ip: str):
        if ip:
            self.hll_peers.add(ip)

    def note_node(self, addr: str):
        self.hll_nodes.add(addr)

    def snapshot(self):
        with self.lock:
            self.tick()
            now = time.time()
            return {
                "first_start": self.life["first_start"],
                "uptime_total": int(self.life["uptime_s"]),
                "session_uptime": int(now - self.session_start),
                "sessions": self.life["sessions"],
                "rx_bytes": self.life["rx_bytes"],
                "tx_bytes": self.life["tx_bytes"],
                "peers_unique": self.hll_peers.count(),
                "nodes_unique": self.hll_nodes.count(),
                "coverage": self.coverage(),
            }

    # ------------------------------------------------------------ persistence
    def flush(self, force=False):
        now = time.time()
        with self.lock:
            self.tick()
            do_meta = force or now - self._last_flush["meta"] >= 300          # counters: every 5 min (and on shutdown)
            meta = None
            if do_meta:
                meta = {"counters": dict(self.counters), "sources": dict(self.sources), "life": dict(self.life),
                        "first_start": self.life["first_start"], "hll_peers": self.hll_peers.to_b64(),
                        "hll_nodes": self.hll_nodes.to_b64()}
                self._last_flush["meta"] = now
                for f in self._files.values():
                    f.flush()
        if meta is not None:                               # disk write happens OUTSIDE the lock
            atomic_write(os.path.join(self.dir, "stats.json"), meta)

    def compact_tiers(self):
        """Rewrites the history .jsonl files without already-expired points (maintenance, once a day)."""
        with self.lock:
            self._trim_raw()
            for name, _, keep in TIERS:
                f = self._files.pop(name, None)
                if f:
                    f.close()
                tmp = self._paths[name] + ".new"
                with open(tmp, "w", encoding="utf-8") as out:
                    for p in self.tiers[name]:
                        out.write(json.dumps(p, separators=(",", ":")) + "\n")
                os.replace(tmp, self._paths[name])

    def close(self):
        self.flush(force=True)
        with self.lock:
            for f in self._files.values():
                f.close()
            self._files.clear()
