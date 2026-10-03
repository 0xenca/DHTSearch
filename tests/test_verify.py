"""Network-free tests for verify.py: fake UDP and HTTP trackers + a fake seeder and leecher on 127.0.0.1.
Run: python tests/test_verify.py"""
import os, socket, struct, sys, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit, unquote_to_bytes
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import verify as V

V.ALLOW_PRIVATE = True
IH = bytes.fromhex("ab" * 20)
NUM_PIECES = 20


# ------------------------------------------------------------ fake peers
def fake_peer(kind):
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(8)

    def serve():
        while True:
            c, _ = srv.accept()
            try:
                hs = c.recv(68)
                c.sendall(b"\x13BitTorrent protocol" + bytes([0, 0, 0, 0, 0, 0x10, 0, 0x04]) + hs[28:48] + b"-XX0001-" + b"0" * 12)
                ext = V.bencode({b"m": {}, b"v": ("FakeSeed 1.0" if kind != "leech" else "FakeLeech 1.0").encode()})
                c.sendall(struct.pack(">IBB", len(ext) + 2, 20, 0) + ext)
                if kind == "seed_all":
                    c.sendall(struct.pack(">IB", 1, 14))                     # have_all
                else:
                    bf = bytearray(3)                                         # 20 piezas -> 3 bytes
                    n = NUM_PIECES if kind == "seed_bits" else 5
                    for i in range(n):
                        bf[i // 8] |= 0x80 >> (i % 8)
                    c.sendall(struct.pack(">IB", len(bf) + 1, 5) + bf)
                time.sleep(0.3)
            except OSError:
                pass
            finally:
                c.close()
    threading.Thread(target=serve, daemon=True).start()
    return srv.getsockname()


SEED1, SEED2, LEECH = fake_peer("seed_all"), fake_peer("seed_bits"), fake_peer("leech")
DEAD = ("127.0.0.1", 1)                                                       # nobody listening: "refused"


def compact(peers):
    return b"".join(socket.inet_aton(ip) + struct.pack(">H", p) for ip, p in peers)


# ------------------------------------------------------------ fake UDP tracker
def fake_udp_tracker(seeds, leech, peers):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(("127.0.0.1", 0))
    state = {"announces": [], "stopped": 0}

    def serve():
        while True:
            data, addr = s.recvfrom(2048)
            if len(data) < 16:
                continue
            cid, act, tid = struct.unpack(">QII", data[:16])
            if act == 0:
                s.sendto(struct.pack(">IIQ", 0, tid, 0x1234), addr)
            elif act == 2:
                s.sendto(struct.pack(">IIIII", 2, tid, seeds, 99, leech), addr)
            elif act == 1:
                event = struct.unpack(">I", data[80:84])[0]
                left = struct.unpack(">Q", data[64:72])[0]
                state["announces"].append((event, left))
                if event == 3:
                    state["stopped"] += 1
                    continue
                s.sendto(struct.pack(">IIIII", 1, tid, 1800, leech, seeds) + compact(peers), addr)
    threading.Thread(target=serve, daemon=True).start()
    return f"udp://127.0.0.1:{s.getsockname()[1]}/announce", state


# ------------------------------------------------------------ fake HTTP tracker
class HH(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        u = urlsplit(self.path)
        raw = dict(x.split("=", 1) for x in u.query.split("&"))
        ih = unquote_to_bytes(raw["info_hash"])
        if u.path.endswith("/scrape"):
            body = V.bencode({b"files": {ih: {b"complete": 500, b"incomplete": 40, b"downloaded": 7000}}})
        else:
            body = V.bencode({b"interval": 1800, b"complete": 500, b"incomplete": 40, b"peers": compact([DEAD])})
        self.send_response(200); self.end_headers(); self.wfile.write(body)


http = HTTPServer(("127.0.0.1", 0), HH)
threading.Thread(target=http.serve_forever, daemon=True).start()
HTTP_URL = f"http://127.0.0.1:{http.server_port}/announce"

# ------------------------------------------------------------ tests
UDP_GOOD, st_good = fake_udp_tracker(3, 2, [SEED1, SEED2, LEECH])
UDP_DEAD = "udp://127.0.0.1:9/announce"                                      # does not answer: timeout

r = V.udp_scrape(UDP_GOOD, IH)
assert r == {"seeders": 3, "completed": 99, "leechers": 2}, r
r = V.http_scrape(HTTP_URL, IH)
assert r == {"seeders": 500, "completed": 7000, "leechers": 40}, r
print("UDP (BEP 15) and HTTP scrape: OK")

a = V.udp_announce(UDP_GOOD, IH, size=1000)
assert a["seeders"] == 3 and len(a["peers"]) == 3
time.sleep(0.2)
assert st_good["announces"][0] == (2, 1000), "announces as a LEECHER (left > 0), never as a seeder"
assert st_good["stopped"] >= 1, "after the announce a \"stopped\" is sent so we are not counted in the swarm"
print("announce as leecher + stopped: OK")

assert V.probe_peer(*SEED1, IH, NUM_PIECES)["status"] == "seed"
p = V.probe_peer(*SEED2, IH, NUM_PIECES); assert p["status"] == "seed" and p["client"] == "FakeSeed 1.0", p
p = V.probe_peer(*LEECH, IH, NUM_PIECES); assert p["status"] == "leecher" and p["progress"] == 0.25, p
p = V.probe_peer(*SEED2, IH, None); assert p["status"] == "seed", "without number of pieces: full bitfield = seeder"
assert V.probe_peer(*DEAD, IH)["status"] == "refused"
print("handshake: have_all, bitfield completo, leecher 25 %, puerto cerrado: OK")

rep = V.verify(IH.hex(), [UDP_GOOD, HTTP_URL, UDP_DEAD], num_pieces=NUM_PIECES, use_dht=False)
by = {t["url"]: t for t in rep["trackers"]}
assert by[UDP_GOOD]["scrape"]["seeders"] == 3 and by[UDP_GOOD]["check"] == {"tested": 3, "reachable": 3, "seeds": 2, "leechers": 1}
assert by[HTTP_URL]["scrape"]["seeders"] == 500 and by[HTTP_URL]["check"]["reachable"] == 0, "the one claiming 500 provides not a single reachable peer"
assert by[UDP_DEAD]["scrape"] is None and "timeout" in by[UDP_DEAD]["scrape_error"]
s = rep["summary"]
assert s["seeds_confirmed"] == 2 and s["leechers_confirmed"] == 1 and s["max_reported_seeders"] == 500 and s["trackers_ok"] == 2, s
assert "REAL SEEDERS" in V.verdict(rep)
print("per-source report (says / returns / answer / confirmed seeders): OK")

UDP_LIAR, _ = fake_udp_tracker(250, 10, [DEAD])
rep = V.verify(IH.hex(), [UDP_LIAR], num_pieces=NUM_PIECES, use_dht=False)
assert rep["summary"]["seeds_confirmed"] == 0 and "NO address answered" in V.verdict(rep)
print("tracker claiming 250 seeders with nobody there: correct verdict: OK")

# ------------------------------------------------------------ fake DHT: one node that refers to another, which has the peers
def fake_dht(reply):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(("127.0.0.1", 0))
    def serve():
        while True:
            data, addr = s.recvfrom(2048)
            m = V.bdecode(data)
            s.sendto(V.bencode({b"t": m[b"t"], b"y": b"r", b"r": {b"id": b"x" * 20, **reply()}}), addr)
    threading.Thread(target=serve, daemon=True).start()
    return s.getsockname()
n2 = fake_dht(lambda: {b"values": [compact([SEED1]), compact([LEECH])]})
n1 = fake_dht(lambda: {b"nodes": b"y" * 20 + socket.inet_aton(n2[0]) + struct.pack(">H", n2[1])})
V.DHT_BOOTSTRAP = [n1]
peers, n = V.dht_get_peers(IH, duration=3)
assert set(peers) == {SEED1, LEECH} and n == 2, (peers, n)
rep = V.verify(IH.hex(), [UDP_DEAD], num_pieces=NUM_PIECES, use_dht=True)
assert rep["dht"]["check"]["seeds"] == 1 and rep["dht"]["peers"] == 2, rep["dht"]
print("iterative DHT get_peers (nodes -> values) and seeders confirmed via DHT: OK")
print("ALL OK (verify)")
