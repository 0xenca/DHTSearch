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
Memory cap: --peer-max entries (≈ 40 B each). NOTE: an IP address is personal data (GDPR).
"""
import ipaddress
import json
import os
import socket
import sys
import threading
import time
from array import array

import struct

from jsonio import Journal, read_jsonl
from memstat import drop_cache
from packing import _Table

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
    return bytes(_E.pack(int(port or 0) & 0xFFFF, max(min(int(role), 1), -1), int(first) & 0xFFFFFFFF,
                             int(last) & 0xFFFFFFFF, int(wlast) & 0xFFFFFFFF, CLIENTS.id(client or ""), _src_mask(src)))


class PeerStore:
    """Peers per torrent, packed: by_ih[ih] = bytearray of 36-byte entries (IP as 16 bytes, IPv4 mapped into IPv6,
    + the 20-byte entry above). About 40 B per peer instead of ~440 B with a dict of dicts plus a reverse index.
    There is no IP -> torrents index any more: the admin searches by IP scan the packed entries (one pass, C-speed
    comparisons), which is slower but only the admin panel does it."""

    def __init__(self, data_dir, enabled=True, ttl_days=30, max_entries=500_000, per_torrent=300, persist_dht=False):
        self.enabled = enabled
        # peers known ONLY because the DHT returned their address (role unknown, never connected) are kept in memory but
        # not written to disk unless persist_dht: they are most of the churn (hundreds per second) and the weakest evidence
        self.persist_dht = persist_dht
        self.ttl = ttl_days * 86400
        self.max_entries = max_entries
        self.per_torrent = per_torrent
        self.lock = threading.Lock()
        self.by_ih = {}                 # ih -> bytearray(n * 36)
        self.n = 0
        self._dirty = set()             # (ih, ip) waiting to be written
        self._ips_cache = (0.0, 0)
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
                ipb = _ip16(ip)
                if ipb is None:
                    continue
                e = _pack(o.get("p", 0), o.get("s", -1), o.get("f", 0), o.get("l", 0), o.get("l", 0), o.get("c", ""), o.get("o", ""))
                self._put(ih, ipb, e)
            except (KeyError, TypeError, ValueError):
                pass
        self.journal.lines = lines
        self.journal.open()
        drop_cache(self.journal.path)
        if lines:
            print(f"[peers] {self.n} peers of {len(self.by_ih)} torrents loaded in {time.time() - t0:.1f}s")

    # ------------------------------------------------------------ writing
    def _put(self, ih, ipb, entry):
        """Inserts or replaces (journal replay: the last line wins)."""
        blob = self.by_ih.get(ih)
        if blob is None:
            self.by_ih[sys.intern(ih)] = bytearray(ipb + entry)
            self.n += 1
            return
        off = _find(blob, ipb)
        if off < 0:
            blob += ipb + entry
            self.n += 1
        else:
            blob[off + 16: off + REC] = entry

    def _drop_at(self, ih, blob, off):
        del blob[off: off + REC]
        self.n -= 1
        if not blob:
            del self.by_ih[ih]

    def note(self, ih, ip, port=0, role=ROLE_UNKNOWN, client="", src="c", now=None):
        """Records (or updates) that `ip` is in the swarm of `ih`."""
        if not self.enabled or not ip:
            return
        ipb = _ip16(ip)
        if ipb is None:
            return
        now = int(now or time.time())
        client = (client or "")[:40]
        with self.lock:
            blob = self.by_ih.get(ih)
            off = _find(blob, ipb) if blob is not None else -1
            if off < 0:
                if blob is not None and len(blob) >= self.per_torrent * REC:     # per-torrent cap: drop the oldest
                    oldest = min(range(0, len(blob), REC), key=lambda o: _E.unpack_from(blob, o + 16)[LAST])
                    self._dirty.discard((ih, _ip_str(bytes(blob[oldest: oldest + 16]))))
                    self._drop_at(ih, blob, oldest)
                self._put(ih, ipb, _pack(port, role, now, now, 0, client, src))
                self._dirty.add((ih, ip))
                return
            v = list(_E.unpack_from(blob, off + 16))
            changed = False
            if role > v[ROLE]:                                              # unknown < leecher < seeder
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
            _E.pack_into(blob, off + 16, *v)
            if changed or v[LAST] - v[WLAST] > 3600:                        # "last seen" is persisted at most once an hour
                self._dirty.add((ih, ip))

    def flush(self):
        if not self.enabled:
            return
        with self.lock:
            out = []
            for ih, ip in self._dirty:
                blob = self.by_ih.get(ih)
                ipb = _ip16(ip)
                off = _find(blob, ipb) if blob is not None and ipb is not None else -1
                if off < 0:
                    continue
                v = _E.unpack_from(blob, off + 16)
                if not self._persisted(v):
                    continue
                _E.pack_into(blob, off + 16, *v[:WLAST], v[LAST], *v[WLAST + 1:])      # last written = last seen
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
            res["peers_expired"] = self._drop_where(lambda last: last < cutoff)
            if self.n > self.max_entries:
                lasts = array("I")
                for blob in self.by_ih.values():
                    lasts.extend(_E.unpack_from(blob, o + 16)[LAST] for o in range(0, len(blob), REC))
                extra = self.n - self.max_entries
                limit = sorted(lasts)[extra - 1]
                del lasts
                res["peers_capped"] = self._drop_where(lambda last: last <= limit, extra)
            need = self.journal.lines > 2 * max(self.n, 1) + 200_000
        if need:
            self.compact()
            res["peers_compacted"] = True
        return res

    def _drop_where(self, cond, max_n=None):
        n = 0
        for ih in list(self.by_ih):
            blob = self.by_ih[ih]
            keep = bytearray()
            for o in range(0, len(blob), REC):
                if (max_n is None or n < max_n) and cond(_E.unpack_from(blob, o + 16)[LAST]):
                    n += 1
                else:
                    keep += blob[o: o + REC]
            if len(keep) != len(blob):
                self.n -= (len(blob) - len(keep)) // REC
                if keep:
                    self.by_ih[ih] = keep
                else:
                    del self.by_ih[ih]
        return n

    def forget(self, ihs):
        """Removes every peer of these torrents and rewrites the journal without them (import undo). Returns how many."""
        n = 0
        with self.lock:
            for ih in ihs:
                blob = self.by_ih.pop(ih, None)
                if blob:
                    n += len(blob) // REC
            self.n -= n
        if n:
            self.compact()
        return n

    def compact(self):
        j = self.journal
        j.begin_compaction()
        try:
            with self.lock:
                snap = [(ih, bytes(blob)) for ih, blob in self.by_ih.items()]      # ~36 B per peer, not a tuple per peer
            j.finish_compaction(json.dumps(self._json(ih, ip, v), separators=(",", ":"), ensure_ascii=False) + "\n"
                                for ih, blob in snap for ip, v in _entries(blob) if self._persisted(v))
        except Exception:
            j.abort_compaction()
            raise

    def _persisted(self, v):
        return self.persist_dht or v[SRC] != _SRC_BITS["d"] or v[ROLE] != ROLE_UNKNOWN

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
    def _row(ip, v):
        if isinstance(v, (bytes, bytearray)):
            v = _E.unpack(v)
        return {"ip": ip, "port": v[PORT], "role": v[ROLE], "first": v[FIRST], "last": v[LAST], "client": CLIENTS.get(v[CLIENT]),
                "src": _src_str(v[SRC])}

    def entry(self, ih, ip):
        """Unpacked entry of one peer in one torrent (None if absent)."""
        with self.lock:
            blob, ipb = self.by_ih.get(ih), _ip16(ip)
            off = _find(blob, ipb) if blob is not None and ipb is not None else -1
            return None if off < 0 else self._row(ip, _E.unpack_from(blob, off + 16))

    def ips_of(self, ih):
        with self.lock:
            blob = self.by_ih.get(ih)
            return [ip for ip, _ in _entries(bytes(blob))] if blob else []

    def count(self, ih):
        blob = self.by_ih.get(ih)
        return len(blob) // REC if blob else 0

    def peers_of(self, ih, limit=500):
        with self.lock:
            blob = self.by_ih.get(ih)
            rows = [self._row(ip, v) for ip, v in _entries(bytes(blob))] if blob else []
        rows.sort(key=lambda r: (-r["role"], -r["last"]))
        return rows[:limit]

    def aggregate(self, ihs, hidden=frozenset()):
        """Peers of a set of torrents: per IP, in how many of THEM it appears and in how many OTHER (not hidden) torrents."""
        agg = {}
        with self.lock:
            sel = set(ihs)
            for ih in sel:
                blob = self.by_ih.get(ih)
                if not blob:
                    continue
                for o in range(0, len(blob), REC):
                    ipb = bytes(blob[o: o + 16])
                    e = _E.unpack_from(blob, o + 16)
                    a = agg.get(ipb)
                    if a is None:
                        a = agg[ipb] = {"ip": _ip_str(ipb), "ports": set(), "role": -1, "in_set": 0, "seed_in": 0, "last": 0,
                                        "clients": set(), "src": set(), "elsewhere": 0}
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
            if agg:                                  # one pass over every torrent that is not hidden
                for ih, blob in self.by_ih.items():
                    if ih in hidden:
                        continue
                    for o in range(0, len(blob), REC):
                        a = agg.get(bytes(blob[o: o + 16]))
                        if a is not None:
                            a["elsewhere"] += 1
        for a in agg.values():
            a["ports"] = sorted(a["ports"])[:6]
            a["clients"] = sorted(a["clients"])[:3]
            a["src"] = "".join(sorted(a["src"]))
        return list(agg.values())

    def find(self, specs, only_seeds=False):
        """Torrents in which these IPs / networks appear. specs: list of ip_address / ip_network.
        Returns {ih: [(ip, role, last), …]}"""
        exact = {_ip16(str(s)) for s in specs if not isinstance(s, (ipaddress.IPv4Network, ipaddress.IPv6Network))}
        exact.discard(None)
        ranges = []
        for s in specs:
            if isinstance(s, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
                lo, hi = _ip16(str(s.network_address)), _ip16(str(s.broadcast_address))
                if lo and hi:
                    ranges.append((lo, hi, s.version == 4))
        out = {}
        with self.lock:
            for ih, blob in self.by_ih.items():
                for o in range(0, len(blob), REC):
                    ipb = bytes(blob[o: o + 16])
                    if ipb in exact or any(lo <= ipb <= hi and (ipb[:12] == _V4) == v4 for lo, hi, v4 in ranges):
                        # 16-byte big-endian: byte order = numeric order; the IP version must match the network's
                        e = _E.unpack_from(blob, o + 16)
                        if only_seeds and e[ROLE] != ROLE_SEED:
                            continue
                        out.setdefault(ih, []).append((_ip_str(ipb), e[ROLE], e[LAST]))
        return out

    def distinct_ips(self):
        now = time.time()
        at, n = self._ips_cache
        if now - at < 60:
            return n
        with self.lock:
            seen = set()
            for blob in self.by_ih.values():
                for o in range(0, len(blob), REC):
                    seen.add(bytes(blob[o: o + 16]))
            n = len(seen)
        self._ips_cache = (now, n)
        return n

    def summary(self):
        ips = self.distinct_ips() if self.enabled else 0
        with self.lock:
            return {"enabled": self.enabled, "entries": self.n, "torrents": len(self.by_ih), "ips": ips,
                    "max": self.max_entries, "ttl_days": self.ttl // 86400, "journal_lines": self.journal.lines}


REC = 16 + _E.size                                 # 36 B per peer
_V4 = b"\x00" * 10 + b"\xff\xff"


def _ip16(ip):
    """'1.2.3.4' / '2001:db8::1' -> 16 bytes (IPv4 mapped into IPv6). None if it is not an IP address."""
    try:
        if ":" in ip:
            return socket.inet_pton(socket.AF_INET6, ip)
        return _V4 + socket.inet_pton(socket.AF_INET, ip)
    except (OSError, TypeError, ValueError):
        return None


def _ip_str(b):
    if b[:12] == _V4:
        return socket.inet_ntop(socket.AF_INET, b[12:])
    return socket.inet_ntop(socket.AF_INET6, b)


def _find(blob, ipb):
    """Offset of the entry with this IP in a packed blob, or -1 (bytes.find in C, checking the alignment)."""
    i = blob.find(ipb)
    while i >= 0:
        if i % REC == 0:
            return i
        i = blob.find(ipb, i + 1)
    return -1


def _entries(blob):
    for o in range(0, len(blob), REC):
        yield _ip_str(bytes(blob[o: o + 16])), _E.unpack_from(blob, o + 16)


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
