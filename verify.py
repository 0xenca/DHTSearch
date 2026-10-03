"""
INDEPENDENT checker of a torrent's seeders/peers (pure Python: uses neither libtorrent nor the crawler).

Answers "is anybody really seeding this?" by cross-checking sources and testing every peer by hand:
  1. scrape    every tracker (UDP BEP 15 / HTTP): what the tracker SAYS (seeders, leechers, completed)
  2. announce  to every tracker: the IP:port the tracker RETURNS (announces as a leecher and sends "stopped" right away)
  3. DHT       iterative get_peers lookup: the IP:port registered in the DHT
  4. connect   TCP + BitTorrent handshake to those addresses: does it answer?, has the torrent?, full bitfield (seeder)?

Usage:
  python verify.py <infohash | magnet> [--tracker URL ...] [--pieces N] [--connect 150] [--no-dht] [--json]

Honest limits:
  - Unencrypted TCP only: a peer that only accepts uTP or requires encryption (MSE) shows as "does not connect" even if it exists.
  - A peer behind NAT without an open port is not reachable either (nor will it be for your client, unless your client has an open port).
  - It is a ~20 s snapshot.
"""
import argparse
import hashlib
import ipaddress
import json
import os
import random
import re
import select
import socket
import struct
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from trackers import DEFAULT_TRACKERS  # noqa: E402  (same list as the crawler)
DHT_BOOTSTRAP = [("router.bittorrent.com", 6881), ("dht.transmissionbt.com", 6881),
                 ("router.utorrent.com", 6881), ("dht.libtorrent.org", 25401)]
PEER_ID = b"-VF0001-" + os.urandom(6).hex().encode()[:12]


# ------------------------------------------------------------------ bencode
def bdecode(data):
    def dec(i):
        c = data[i:i + 1]
        if c == b"i":
            j = data.index(b"e", i)
            return int(data[i + 1:j]), j + 1
        if c == b"l":
            out, i = [], i + 1
            while data[i:i + 1] != b"e":
                v, i = dec(i)
                out.append(v)
            return out, i + 1
        if c == b"d":
            out, i = {}, i + 1
            while data[i:i + 1] != b"e":
                k, i = dec(i)
                v, i = dec(i)
                out[k] = v
            return out, i + 1
        if c.isdigit():
            j = data.index(b":", i)
            n = int(data[i:j])
            return data[j + 1:j + 1 + n], j + 1 + n
        raise ValueError("invalid bencode")
    return dec(0)[0]


def bencode(v):
    if isinstance(v, int):
        return b"i%de" % v
    if isinstance(v, str):
        v = v.encode()
    if isinstance(v, bytes):
        return b"%d:%s" % (len(v), v)
    if isinstance(v, list):
        return b"l" + b"".join(bencode(x) for x in v) + b"e"
    if isinstance(v, dict):
        return b"d" + b"".join(bencode(k) + bencode(v[k]) for k in sorted(v, key=lambda k: k if isinstance(k, bytes) else k.encode())) + b"e"
    raise TypeError(type(v))


def _compact_peers(blob, v6=False):
    step = 18 if v6 else 6
    out = []
    for i in range(0, len(blob) - step + 1, step):
        if v6:
            ip = str(ipaddress.IPv6Address(blob[i:i + 16]))
        else:
            ip = socket.inet_ntoa(blob[i:i + 4])
        port = struct.unpack(">H", blob[i + step - 2:i + step])[0]
        if port:
            out.append((ip, port))
    return out


ALLOW_PRIVATE = False                           # tests only (peers on 127.0.0.1)


def _global(ip):
    if ALLOW_PRIVATE:
        return True
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


def host_of(url):
    try:
        return urllib.parse.urlsplit(url).hostname or url
    except ValueError:
        return url


# ------------------------------------------------------------------ trackers
class TrackerError(Exception):
    pass


def _udp_tx(sock, addr, payload, action, txid, timeout):
    sock.settimeout(timeout)
    sock.sendto(payload, addr)
    t_end = time.time() + timeout
    while time.time() < t_end:
        try:
            data, _ = sock.recvfrom(65536)
        except socket.timeout:
            break
        if len(data) < 8:
            continue
        act, tid = struct.unpack(">II", data[:8])
        if tid != txid:
            continue
        if act == 3:
            raise TrackerError("the tracker answers with an error: " + data[8:].decode("utf-8", "replace")[:200])
        if act == action:
            return data
    raise TrackerError("no answer (timeout)")


def _udp_session(url, timeout):
    u = urllib.parse.urlsplit(url)
    if not u.hostname or not u.port:
        raise TrackerError("UDP tracker URL without host/port")
    try:
        infos = socket.getaddrinfo(u.hostname, u.port, 0, socket.SOCK_DGRAM)
    except socket.gaierror as e:
        raise TrackerError(f"DNS: {e}")
    fam, _, _, _, addr = infos[0]
    sock = socket.socket(fam, socket.SOCK_DGRAM)
    for attempt in range(2):
        txid = random.getrandbits(32)
        try:
            data = _udp_tx(sock, addr, struct.pack(">QII", 0x41727101980, 0, txid), 0, txid, timeout)
            return sock, addr, struct.unpack(">Q", data[8:16])[0]
        except TrackerError:
            if attempt:
                sock.close()
                raise
    raise TrackerError("no answer")


def udp_scrape(url, ih, timeout=4):
    sock, addr, cid = _udp_session(url, timeout)
    try:
        txid = random.getrandbits(32)
        data = _udp_tx(sock, addr, struct.pack(">QII", cid, 2, txid) + ih, 2, txid, timeout)
        if len(data) < 20:
            raise TrackerError("short scrape reply")
        s, c, l = struct.unpack(">III", data[8:20])
        return {"seeders": s, "completed": c, "leechers": l}
    finally:
        sock.close()


def udp_announce(url, ih, size=1, port=6881, timeout=4, numwant=200):
    sock, addr, cid = _udp_session(url, timeout)
    try:
        key = random.getrandbits(32)

        def ann(event, want):
            txid = random.getrandbits(32)
            pkt = struct.pack(">QII20s20sQQQIIIiH", cid, 1, txid, ih, PEER_ID, 0, max(size, 1), 0, event, 0, key, want, port)
            return _udp_tx(sock, addr, pkt, 1, txid, timeout)
        data = ann(2, numwant)                       # started (as a leecher: left > 0, we do not inflate the seeders)
        interval, leech, seeds = struct.unpack(">III", data[8:20])
        peers = _compact_peers(data[20:], v6=(addr[0].count(":") > 1))
        try:
            sock.settimeout(0.5)
            txid = random.getrandbits(32)          # stopped: so it does not count us as a peer (without waiting for the answer)
            sock.sendto(struct.pack(">QII20s20sQQQIIIiH", cid, 1, txid, ih, PEER_ID, 0, max(size, 1), 0, 3, 0, key, 0, port), addr)
        except OSError:
            pass
        return {"seeders": seeds, "leechers": leech, "peers": peers}
    finally:
        sock.close()


def _http_get(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": "verify.py/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(2_000_000)
    except Exception as e:
        raise TrackerError(f"{type(e).__name__}: {e}")


def http_scrape(url, ih, timeout=6):
    u = urllib.parse.urlsplit(url)
    path, _, last = u.path.rpartition("/")
    if not last.startswith("announce"):
        raise TrackerError("this tracker does not support scrape (its URL does not end in /announce)")
    surl = urllib.parse.urlunsplit((u.scheme, u.netloc, path + "/" + last.replace("announce", "scrape", 1), u.query, ""))
    sep = "&" if "?" in surl else "?"
    d = bdecode(_http_get(surl + sep + "info_hash=" + urllib.parse.quote_from_bytes(ih), timeout))
    if b"failure reason" in d:
        raise TrackerError(d[b"failure reason"].decode("utf-8", "replace")[:200])
    f = (d.get(b"files") or {}).get(ih)
    if f is None:
        return {"seeders": 0, "completed": 0, "leechers": 0}
    return {"seeders": f.get(b"complete", 0), "completed": f.get(b"downloaded", 0), "leechers": f.get(b"incomplete", 0)}


def http_announce(url, ih, size=1, port=6881, timeout=6, numwant=200):
    q = {"peer_id": PEER_ID, "port": port, "uploaded": 0, "downloaded": 0, "left": max(size, 1), "compact": 1, "numwant": numwant}
    base = url + ("&" if "?" in url else "?") + "info_hash=" + urllib.parse.quote_from_bytes(ih) + "&"
    d = bdecode(_http_get(base + urllib.parse.urlencode(q) + "&event=started", timeout))
    if b"failure reason" in d:
        raise TrackerError(d[b"failure reason"].decode("utf-8", "replace")[:200])
    peers = []
    p = d.get(b"peers", b"")
    if isinstance(p, bytes):
        peers = _compact_peers(p)
    else:
        peers = [(x[b"ip"].decode(), x[b"port"]) for x in p if isinstance(x, dict) and b"ip" in x]
    peers += _compact_peers(d.get(b"peers6", b""), v6=True)
    try:
        _http_get(base + urllib.parse.urlencode({**q, "numwant": 0}) + "&event=stopped", 3)
    except TrackerError:
        pass
    return {"seeders": d.get(b"complete"), "leechers": d.get(b"incomplete"), "peers": peers}


def check_tracker(url, ih, size=1, port=6881, timeout=4):
    """Scrape + announce to ONE tracker. Never raises: errors go into the result."""
    r = {"url": url, "host": host_of(url), "scrape": None, "scrape_error": "", "announce": None, "announce_error": "", "peers": []}
    t0 = time.time()
    udp = url.startswith("udp://")
    if not (udp or url.startswith(("http://", "https://"))):
        r["scrape_error"] = r["announce_error"] = "esquema no soportado"
        return r
    try:
        r["scrape"] = (udp_scrape if udp else http_scrape)(url, ih, timeout=timeout)
    except (TrackerError, OSError, ValueError) as e:
        r["scrape_error"] = str(e)[:200]
    try:
        a = (udp_announce if udp else http_announce)(url, ih, size=size, port=port, timeout=timeout)
        r["announce"] = {"seeders": a["seeders"], "leechers": a["leechers"]}
        r["peers"] = [p for p in a["peers"] if _global(p[0])]
    except (TrackerError, OSError, ValueError) as e:
        r["announce_error"] = str(e)[:200]
    r["ms"] = int((time.time() - t0) * 1000)
    return r


# ------------------------------------------------------------------ DHT
def dht_get_peers(ih, duration=12, max_queries=300):
    """Iterative get_peers lookup (BEP 5). Returns (peers, number of nodes that answered)."""
    nid = os.urandom(20)
    target = int.from_bytes(ih, "big")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 0))
    cand = {}                                   # (ip, port) -> distancia
    asked, peers, responded = set(), set(), 0
    for host, port in DHT_BOOTSTRAP:
        try:
            for info in socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM)[:2]:
                cand[info[4]] = 1 << 160
        except socket.gaierror:
            pass
    pending = {}                                # tid -> addr
    t_end = time.time() + duration
    try:
        while time.time() < t_end and len(asked) < max_queries:
            fresh = sorted((d, a) for a, d in cand.items() if a not in asked)
            while len(pending) < 16 and fresh:
                _, addr = fresh.pop(0)
                asked.add(addr)
                tid = os.urandom(2)
                msg = bencode({b"t": tid, b"y": b"q", b"q": b"get_peers", b"a": {b"id": nid, b"info_hash": ih}})
                try:
                    sock.sendto(msg, addr)
                    pending[tid] = (addr, time.time())
                except OSError:
                    pass
            now = time.time()
            for tid in [t for t, (_, ts) in pending.items() if now - ts > 3]:
                del pending[tid]
            if not pending and not fresh:
                break
            rd, _, _ = select.select([sock], [], [], 0.3)
            if not rd:
                continue
            try:
                data, src = sock.recvfrom(65536)
                m = bdecode(data)
            except Exception:
                continue
            if not isinstance(m, dict) or m.get(b"t") not in pending or m.get(b"y") != b"r":
                continue
            del pending[m[b"t"]]
            responded += 1
            r = m.get(b"r") or {}
            for v in r.get(b"values") or []:
                if isinstance(v, bytes) and len(v) == 6:
                    for p in _compact_peers(v):
                        if _global(p[0]):
                            peers.add(p)
            nodes = r.get(b"nodes") or b""
            for i in range(0, len(nodes) - 25, 26):
                ip = socket.inet_ntoa(nodes[i + 20:i + 24])
                port = struct.unpack(">H", nodes[i + 24:i + 26])[0]
                if _global(ip) and port:
                    cand.setdefault((ip, port), int.from_bytes(nodes[i:i + 20], "big") ^ target)
    finally:
        sock.close()
    return sorted(peers), responded


# ------------------------------------------------------------------ peers
def _recv_exact(sock, n, t_end):
    buf = b""
    while len(buf) < n:
        left = t_end - time.time()
        if left <= 0:
            raise socket.timeout()
        sock.settimeout(left)
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed the connection")
        buf += chunk
    return buf


def probe_peer(ip, port, ih, num_pieces=None, timeout=5):
    """BitTorrent handshake. status: seed | leecher | connected (no bitfield) | no_torrent | refused | timeout | error"""
    r = {"ip": ip, "port": port, "status": "error", "progress": None, "client": ""}
    t0 = time.time()
    t_end = t0 + timeout
    try:
        s = socket.create_connection((ip, port), timeout=min(timeout, 4))
    except socket.timeout:
        r["status"] = "timeout"
        return r
    except OSError as e:
        r["status"] = "refused" if isinstance(e, ConnectionRefusedError) else "timeout" if "timed out" in str(e) else "unreachable"
        return r
    try:
        reserved = bytearray(8)
        reserved[5] |= 0x10                          # extension protocol (BEP 10)
        reserved[7] |= 0x04                          # fast extension (have_all / have_none)
        s.sendall(b"\x13BitTorrent protocol" + bytes(reserved) + ih + PEER_ID)
        hs = _recv_exact(s, 68, t_end)
        if hs[28:48] != ih:
            r["status"] = "no_torrent"
            return r
        r["rtt_ms"] = int((time.time() - t0) * 1000)
        if hs[25] & 0x10:
            s.sendall(_ext_msg())
        have = None
        pieces_have = 0
        while time.time() < t_end:
            try:
                ln = struct.unpack(">I", _recv_exact(s, 4, min(t_end, time.time() + 3)))[0]
            except (socket.timeout, ConnectionError):
                break
            if ln == 0:
                continue
            if ln > 2_000_000:
                break
            body = _recv_exact(s, ln, t_end)
            mid = body[0]
            if mid == 5:                               # bitfield
                bf = body[1:]
                pieces_have = sum(bin(b).count("1") for b in bf)
                have = ("bits", bf)
            elif mid == 14:                            # have_all
                have = ("all", None)
                break
            elif mid == 15:                            # have_none
                have = ("none", None)
            elif mid == 4:                             # have
                pieces_have += 1
            elif mid == 20 and len(body) > 1 and body[1] == 0:
                try:
                    d = bdecode(body[2:])
                    r["client"] = (d.get(b"v") or b"").decode("utf-8", "replace")[:40]
                except Exception:
                    pass
            if have and have[0] == "bits" and r["client"]:
                break
        if have is None and pieces_have == 0:
            r["status"] = "connected"
            return r
        if have and have[0] == "all":
            r["status"], r["progress"] = "seed", 1.0
            return r
        if num_pieces:
            prog = min(pieces_have / num_pieces, 1.0)
        elif have and have[0] == "bits":
            bf = have[1]
            full = all(b == 0xFF for b in bf[:-1]) and bf and re.fullmatch("1*0*", format(bf[-1], "08b"))
            prog = 1.0 if full else pieces_have / max(len(bf) * 8, 1)
        else:
            prog = 0.0
        r["progress"] = round(prog, 4)
        r["status"] = "seed" if prog >= 1.0 else "leecher"
        return r
    except socket.timeout:
        r["status"] = "timeout"
        return r
    except (ConnectionError, OSError):
        r["status"] = "closed"                        # connected and hung up (e.g. requires encryption or lacks the torrent)
        return r
    finally:
        s.close()


def _ext_msg():
    payload = bencode({b"m": {b"ut_metadata": 1}, b"v": b"verify.py"})
    return struct.pack(">IBB", len(payload) + 2, 20, 0) + payload


# ------------------------------------------------------------------ orchestration
def verify(ih_hex, trackers=None, num_pieces=None, size=1, connect=150, use_dht=True, port=6881, progress=None):
    """Returns a report dict with the per-source breakdown. progress(text) optional, for progress updates."""
    ih = bytes.fromhex(ih_hex)
    trackers = list(dict.fromkeys(trackers or DEFAULT_TRACKERS))
    say = progress or (lambda _t: None)
    t0 = time.time()
    rep = {"ih": ih_hex, "started": int(t0), "trackers": [], "dht": None, "peers": [], "summary": {}}
    say(f"querying {len(trackers)} trackers" + (" and the DHT" if use_dht else ""))
    dht_res = {}

    def run_dht():
        try:
            p, n = dht_get_peers(ih)
            dht_res.update(peers=p, nodes=n)
        except Exception as e:
            dht_res.update(peers=[], nodes=0, error=f"{type(e).__name__}: {e}")
    th = threading.Thread(target=run_dht, daemon=True)
    if use_dht:
        th.start()
    with ThreadPoolExecutor(max_workers=max(len(trackers), 1)) as ex:
        rep["trackers"] = list(ex.map(lambda u: check_tracker(u, ih, size=size, port=port), trackers))
    if use_dht:
        th.join(15)
        rep["dht"] = {"peers": len(dht_res.get("peers", [])), "nodes_replied": dht_res.get("nodes", 0), "error": dht_res.get("error", "")}

    # addresses to test, and which sources they come from
    sources = {}
    for t in rep["trackers"]:
        for p in t["peers"]:
            sources.setdefault(tuple(p), set()).add(t["url"])
    for p in dht_res.get("peers", []):
        sources.setdefault(tuple(p), set()).add("DHT")
    addrs = list(sources)
    random.shuffle(addrs)
    addrs = addrs[:connect]
    say(f"{len(sources)} distinct addresses; testing connections to {len(addrs)}")
    results = []
    if addrs:
        with ThreadPoolExecutor(max_workers=64) as ex:
            results = list(ex.map(lambda a: probe_peer(a[0], a[1], ih, num_pieces), addrs))
    for r in results:
        r["sources"] = sorted(sources[(r["ip"], r["port"])])
    rep["peers"] = sorted(results, key=lambda r: ({"seed": 0, "leecher": 1, "connected": 2}.get(r["status"], 3), -(r["progress"] or 0)))

    # summary per source: how many addresses it gave, how many answered, how many were real seeders
    def agg(name):
        rs = [r for r in results if name in r["sources"]]
        return {"tested": len(rs), "reachable": sum(r["status"] in ("seed", "leecher", "connected") for r in rs),
                "seeds": sum(r["status"] == "seed" for r in rs), "leechers": sum(r["status"] == "leecher" for r in rs)}
    for t in rep["trackers"]:
        t["returned"] = len(t["peers"])
        t["check"] = agg(t["url"])
        t["peers"] = []                               # the IPs go in rep["peers"]; only the count here
    if rep["dht"] is not None:
        rep["dht"]["check"] = agg("DHT")
    reach = [r for r in results if r["status"] in ("seed", "leecher", "connected")]
    rep["summary"] = {
        "addresses": len(sources), "tested": len(results), "reachable": len(reach),
        "seeds_confirmed": sum(r["status"] == "seed" for r in results),
        "leechers_confirmed": sum(r["status"] == "leecher" for r in results),
        "max_reported_seeders": max([(t["scrape"] or {}).get("seeders", 0) for t in rep["trackers"]] + [0]),
        "trackers_ok": sum(1 for t in rep["trackers"] if t["scrape"] is not None),
        "took_s": round(time.time() - t0, 1),
    }
    say("terminado")
    return rep


def verdict(rep):
    s = rep["summary"]
    if s["seeds_confirmed"]:
        return f"REAL SEEDERS: {s['seeds_confirmed']} answered with the complete torrent."
    if s["max_reported_seeders"] and not s["reachable"]:
        return ("Trackers say there are seeders but NO address answered: expired seeders, behind NAT, "
                "uTP/encryption only, or inflated/fake figures.")
    if s["max_reported_seeders"]:
        return "Trackers say there are seeders, but only leechers answered in this test."
    if s["reachable"]:
        return "No seeders: there are peers, but none has the complete torrent."
    return "Nobody was found."


def _parse_target(s):
    s = s.strip()
    m = re.search(r"btih:([0-9a-fA-F]{40})", s) or re.fullmatch(r"([0-9a-fA-F]{40})", s)
    if not m:
        raise SystemExit("a 40-character hex infohash or a magnet link containing it is required")
    trs = [urllib.parse.unquote(x) for x in re.findall(r"[?&]tr=([^&]+)", s)]
    return m.group(1).lower(), trs


def main(argv=None):
    ap = argparse.ArgumentParser(description="Really verifies a torrent's seeders/peers")
    ap.add_argument("target", help="infohash or magnet link")
    ap.add_argument("--tracker", action="append", help="tracker to query (repeatable). Default: those in trackers.py + those in the magnet")
    ap.add_argument("--pieces", type=int, help="number of pieces (to compute each peer's %%; otherwise inferred from the bitfield)")
    ap.add_argument("--connect", type=int, default=150, help="max addresses to connect to")
    ap.add_argument("--no-dht", action="store_true")
    ap.add_argument("--json", action="store_true", help="full JSON output")
    a = ap.parse_args(argv)
    ih, trs = _parse_target(a.target)
    rep = verify(ih, (a.tracker or DEFAULT_TRACKERS) + trs, num_pieces=a.pieces, connect=a.connect, use_dht=not a.no_dht,
                 progress=lambda t: print("·", t, file=sys.stderr))
    if a.json:
        print(json.dumps(rep, indent=1))
        return 0
    print(f"\nTorrent {ih}\n")
    print(f"{'Source':34} {'says S/L':>12} {'returns':>9} {'answer':>9} {'seeds OK':>9}  error")
    for t in rep["trackers"]:
        sc = t["scrape"]
        said = f"{sc['seeders']}/{sc['leechers']}" if sc else "—"
        c = t["check"]
        print(f"{t['host'][:34]:34} {said:>12} {t['returned']:>9} {c['reachable']:>9} {c['seeds']:>9}  {(t['scrape_error'] or t['announce_error'])[:60]}")
    if rep["dht"] is not None:
        c = rep["dht"]["check"]
        print(f"{'DHT (' + str(rep['dht']['nodes_replied']) + ' nodes)':34} {'—':>12} {rep['dht']['peers']:>9} {c['reachable']:>9} {c['seeds']:>9}  {rep['dht']['error']}")
    s = rep["summary"]
    print(f"\n{s['addresses']} distinct addresses, {s['tested']} tested, {s['reachable']} answered "
          f"→ confirmed seeders {s['seeds_confirmed']}, leechers {s['leechers_confirmed']}  ({s['took_s']} s)")
    for r in rep["peers"][:15]:
        if r["status"] in ("seed", "leecher"):
            pct = f"{(r['progress'] or 0) * 100:.0f}%"
            print(f"   {r['status']:8} {pct:>5}  {r['ip']}:{r['port']}  {r['client']}  via {', '.join(host_of(x) for x in r['sources'])}")
    print("\nVERDICT:", verdict(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
