"""
Peers seen per torrent (for the admin panel).

Where they come from (all while the crawler probes a torrent: when indexing it or refreshing its health):
  c  connected: libtorrent connects to the swarm and get_peer_info() gives IP, port, client and whether it is a SEEDER
     (full bitfield / have_all). The only source that tells seeders from leechers.
  d  DHT get_peers replies to our own lookups (IP:port registered in the DHT; role unknown)
  a  announce_peer received from another node for an already indexed torrent (role unknown)

Role: 1 = seeder (seen complete at least once), 0 = leecher (connected and incomplete), -1 = unknown.

Only what is seen FROM NOW ON is stored: there is no earlier history. It is a few-seconds snapshot per probe;
a live torrent is probed again every 6 h (or when "Probe peers now" is clicked in the panel).

Persistence: data/peers.jsonl (append-only journal, compacts itself). Retention: --peer-ttl-days (30 by default).
Memory cap: --peer-max entries (≈ 250 B each). NOTE: an IP address is personal data (GDPR).
"""
import ipaddress
import json
import os
import sys
import threading
import time

import struct

from jsonio import Journal, read_jsonl
from packing import PostingIndex, _Table

ROLE_UNKNOWN, ROLE_LEECH, ROLE_SEED = -1, 0, 1
# Each entry is packed into 20 bytes (previously a list of 7 objects: ~250 B with its ints):
#   port H · role b · first seen I · last seen I · last written I · client H (table) · sources B (bits c/d/a)
_E = struct.Struct("<HbIIIHB")
PORT, ROLE, FIRST, LAST, WLAST, CLIENT, SRC = range(7)
_SRC_BITS = {"c": 1, "d": 2, "a": 4}
CLIENTS = _Table(20_000, "")


def _src_str(mask):
    return "".join(k for k, b in _SRC_BITS.items() if mask & b)


def _src_mask(s):
    m = 0
    for ch in s or "":
        m |= _SRC_BITS.get(ch, 0)
    return m


def _pack(port, role, first, last, wlast, client, src):
    return bytearray(_E.pack(int(port or 0) & 0xFFFF, max(min(int(role), 1), -1), int(first) & 0xFFFFFFFF,
                             int(last) & 0xFFFFFFFF, int(wlast) & 0xFFFFFFFF, CLIENTS.id(client or ""), _src_mask(src)))


class PeerStore:
    def __init__(self, data_dir, enabled=True, ttl_days=30, max_entries=500_000, per_torrent=300):
        self.enabled = enabled
        self.ttl = ttl_days * 86400
        self.max_entries = max_entries
        self.per_torrent = per_torrent
        self.lock = threading.Lock()
        self.by_ih = {}                 # ih -> {ip: packed entry (20-byte bytearray)}
        self.by_ip = PostingIndex()     # ip -> infohashes (no set for IPs seen in a single torrent)
        self.n = 0
        self._dirty = set()             # (ih, ip) waiting to be written
        self.journal = Journal(os.path.join(data_dir, "peers.jsonl"))
        if not enabled:
            return
        t0 = time.time()
        lines = 0
        cutoff = time.time() - self.ttl
        for o in read_jsonl(self.journal.path):
            lines += 1
            try:
                ih, ip = o["i"], o["a"]
                if o.get("l", 0) < cutoff:
                    continue
                self._put(sys.intern(ih), ip, _pack(o.get("p", 0), o.get("s", -1), o.get("f", 0), o.get("l", 0),
                                                    o.get("l", 0), o.get("c", ""), o.get("o", "")))
            except (KeyError, TypeError):
                pass
        self.journal.lines = lines
        self.journal.open()
        if lines:
            print(f"[peers] {self.n} peers of {len(self.by_ih)} torrents loaded in {time.time() - t0:.1f}s")

    # ------------------------------------------------------------ writing
    def _put(self, ih, ip, entry):
        d = self.by_ih.get(ih)
        if d is None:
            d = self.by_ih[ih] = {}
        if ip not in d:
            self.n += 1
            self.by_ip.add(ip, ih)
        d[ip] = entry

    def _drop(self, ih, ip):
        d = self.by_ih.get(ih)
        if not d or ip not in d:
            return
        del d[ip]
        self.n -= 1
        if not d:
            del self.by_ih[ih]
        self.by_ip.discard(ip, ih)

    def note(self, ih, ip, port=0, role=ROLE_UNKNOWN, client="", src="c", now=None):
        """Records (or updates) that `ip` is in the swarm of `ih`."""
        if not self.enabled or not ip:
            return
        now = int(now or time.time())
        client = (client or "")[:40]
        with self.lock:
            d = self.by_ih.get(ih)
            e = d.get(ip) if d else None
            if e is None:
                if d is not None and len(d) >= self.per_torrent:            # per-torrent cap: drop the oldest
                    old = min(d, key=lambda k: _E.unpack(d[k])[LAST])
                    self._drop(ih, old)
                    self._dirty.discard((ih, old))
                if d is None:
                    ih = sys.intern(ih)
                self._put(ih, ip, _pack(port, role, now, now, 0, client, src))
                self._dirty.add((ih, ip))
                return
            v = list(_E.unpack(e))
            changed = False
            if role > v[ROLE]:                                              # desconocido < leecher < seeder
                v[ROLE], changed = role, True
            if port and v[PORT] != port and src == "c":                      # the port of a real connection wins
                v[PORT], changed = int(port) & 0xFFFF, True
            if client:
                cid = CLIENTS.id(client)
                if v[CLIENT] != cid:
                    v[CLIENT], changed = cid, True
            m = _src_mask(src)
            if m & ~v[SRC]:
                v[SRC], changed = v[SRC] | m, True
            v[LAST] = max(v[LAST], now)
            _E.pack_into(e, 0, *v)
            if changed or v[LAST] - v[WLAST] > 3600:                        # "last seen" is persisted at most once an hour
                self._dirty.add((ih, ip))

    def flush(self):
        if not self.enabled:
            return
        with self.lock:
            out = []
            for ih, ip in self._dirty:
                e = self.by_ih.get(ih, {}).get(ip)
                if e is None:
                    continue
                v = _E.unpack(e)
                _E.pack_into(e, 0, *v[:WLAST], v[LAST], *v[WLAST + 1:])      # last written = last seen
                out.append(self._json(ih, ip, v))
            self._dirty.clear()
        self.journal.append_many(out)                       # outside the peers lock (the journal has its own)

    def maintenance(self):
        """Expiry, global cap and journal compaction. Returns a summary."""
        res = {"peers_expired": 0, "peers_capped": 0, "peers_compacted": False}
        if not self.enabled:
            return res
        cutoff = time.time() - self.ttl
        with self.lock:
            old = [(ih, ip) for ih, d in self.by_ih.items() for ip, e in d.items() if _E.unpack(e)[LAST] < cutoff]
            for ih, ip in old:
                self._drop(ih, ip)
            res["peers_expired"] = len(old)
            if self.n > self.max_entries:
                allp = sorted(((_E.unpack(e)[LAST], ih, ip) for ih, d in self.by_ih.items() for ip, e in d.items()))
                extra = allp[: self.n - self.max_entries]
                for _, ih, ip in extra:
                    self._drop(ih, ip)
                res["peers_capped"] = len(extra)
            need = self.journal.lines > 2 * max(self.n, 1) + 200_000
        if need:
            self.compact()
            res["peers_compacted"] = True
        return res

    def compact(self):
        j = self.journal
        j.begin_compaction()
        try:
            with self.lock:
                snap = [(ih, ip, _E.unpack(e)) for ih, d in self.by_ih.items() for ip, e in d.items()]
            j.finish_compaction(json.dumps(self._json(ih, ip, v), separators=(",", ":"), ensure_ascii=False) + "\n"
                                for ih, ip, v in snap)
        except Exception:
            j.abort_compaction()
            raise

    def close(self):
        self.flush()
        self.journal.close()

    # ------------------------------------------------------------- queries
    @staticmethod
    def _json(ih, ip, v):
        """Journal line: the SAME format as always."""
        return {"i": ih, "a": ip, "p": v[PORT], "s": v[ROLE], "f": v[FIRST], "l": v[LAST], "c": CLIENTS.get(v[CLIENT]),
                "o": _src_str(v[SRC])}

    @staticmethod
    def _row(ip, e):
        v = _E.unpack(e)
        return {"ip": ip, "port": v[PORT], "role": v[ROLE], "first": v[FIRST], "last": v[LAST], "client": CLIENTS.get(v[CLIENT]),
                "src": _src_str(v[SRC])}

    def count(self, ih):
        d = self.by_ih.get(ih)
        return len(d) if d else 0

    def peers_of(self, ih, limit=500):
        with self.lock:
            d = self.by_ih.get(ih) or {}
            rows = [self._row(ip, e) for ip, e in d.items()]
        rows.sort(key=lambda r: (-r["role"], -r["last"]))
        return rows[:limit]

    def aggregate(self, ihs, hidden=frozenset()):
        """Peers of a set of torrents: per IP, in how many of THEM it appears and in how many OTHER (not hidden) torrents."""
        agg = {}
        with self.lock:
            for ih in ihs:
                for ip, pe in (self.by_ih.get(ih) or {}).items():
                    e = _E.unpack(pe)
                    a = agg.get(ip)
                    if a is None:
                        a = agg[ip] = {"ip": ip, "ports": set(), "role": -1, "in_set": 0, "seed_in": 0, "last": 0,
                                       "clients": set(), "src": set()}
                    a["in_set"] += 1
                    if e[ROLE] == ROLE_SEED:
                        a["seed_in"] += 1
                    a["role"] = max(a["role"], e[ROLE])
                    if e[PORT]:
                        a["ports"].add(e[PORT])
                    if e[CLIENT]:
                        a["clients"].add(CLIENTS.get(e[CLIENT]))
                    a["src"].update(_src_str(e[SRC]))
                    a["last"] = max(a["last"], e[LAST])
            for ip, a in agg.items():
                a["elsewhere"] = sum(1 for x in self.by_ip.iter(ip) if x not in hidden)
                a["ports"] = sorted(a["ports"])[:6]
                a["clients"] = sorted(a["clients"])[:3]
                a["src"] = "".join(sorted(a["src"]))
        return list(agg.values())

    def find(self, specs, only_seeds=False):
        """Torrents in which these IPs / networks appear. specs: list of ip_address / ip_network.
        Devuelve {ih: [(ip, role, last), …]}"""
        exact = {str(s) for s in specs if not isinstance(s, (ipaddress.IPv4Network, ipaddress.IPv6Network))}
        nets = [s for s in specs if isinstance(s, (ipaddress.IPv4Network, ipaddress.IPv6Network))]
        out = {}
        with self.lock:
            ips = set(ip for ip in exact if ip in self.by_ip)
            if nets:
                for ip in self.by_ip:
                    try:
                        a = ipaddress.ip_address(ip)
                    except ValueError:
                        continue
                    if any(a.version == n.version and a in n for n in nets):
                        ips.add(ip)
            for ip in ips:
                for ih in self.by_ip.iter(ip):
                    e = _E.unpack(self.by_ih[ih][ip])
                    if only_seeds and e[ROLE] != ROLE_SEED:
                        continue
                    out.setdefault(ih, []).append((ip, e[ROLE], e[LAST]))
        return out

    def summary(self):
        with self.lock:
            return {"enabled": self.enabled, "entries": self.n, "torrents": len(self.by_ih), "ips": len(self.by_ip),
                    "max": self.max_entries, "ttl_days": self.ttl // 86400, "journal_lines": self.journal.lines}


def parse_ip_specs(text, limit=5000):
    """"1.2.3.4, 5.6.7.0/24 2001:db8::1" -> [ip_address | ip_network]. Returns (specs, errors)."""
    specs, bad = [], []
    for tok in (text or "").replace(",", " ").replace(";", " ").split():
        tok = tok.strip().strip("[]")
        if not tok:
            continue
        if tok.count(":") == 1 and "." in tok:             # 1.2.3.4:6881 -> without port
            tok = tok.split(":")[0]
        try:
            specs.append(ipaddress.ip_network(tok, strict=False) if "/" in tok else ipaddress.ip_address(tok))
        except ValueError:
            bad.append(tok)
        if len(specs) >= limit:
            break
    return specs, bad
