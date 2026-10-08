"""Offline tests of the Crawler (simulated libtorrent) and the Store. Run: python tests/test_crawler_alerts.py"""
import os, sys, tempfile, time, types
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import crawler as C
from collections import Counter
import store as S
from packing import unpack_hd, unpack_hh
from records import refresh_due_at
from store import Store


_init = Store.__init__
def _fixed_init(self, *a, **k):              # FIXED queue order in these tests (adaptive scheduling has its own test)
    _init(self, *a, **k); self.sched = "fixed"
Store.__init__ = _fixed_init


class H:  # sha1_hash falso
    def __init__(s, x): s.x = x
    def __str__(s): return s.x


def mk(name, **kw):
    return type(name, (), kw)()


def new_crawler():
    st = Store(tempfile.mkdtemp())
    cr = C.Crawler(st, {"state_file": os.path.join(st.dir, "x.bin")})
    return st, cr


# ---------------------------------------------------------------- alerts
st, cr = new_crawler()
alerts = [
    mk("dht_announce_alert", info_hash=H("ab" * 20), ip="1.2.3.4", port=6881),
    mk("dht_get_peers_alert", info_hash=H("cd" * 20)),
    mk("dht_sample_infohashes_alert", samples=staticmethod(lambda: [H("ef" * 20)]),
       nodes=staticmethod(lambda: [(H("00" * 20), ("5.6.7.8", 6881))]), endpoint=("9.9.9.9", 6881), interval=60),
    mk("dht_stats_alert", routing_table=[{"num_nodes": 40, "num_replacements": 3}], active_requests=[1, 2]),
    mk("session_stats_alert", values={"net.recv_bytes": 1000, "net.sent_bytes": 500, "dht.dht_bytes_in": 200,
                                       "dht.dht_bytes_out": 100, "peer.num_peers_connected": 7, "peer.num_peers_half_open": 2}),
    mk("dht_live_nodes_alert", nodes=[(H("11" * 20), ("8.8.4.4", 6881))]),
    mk("dht_outgoing_get_peers_alert", info_hash=H("22" * 20), endpoint=("4.4.4.4", 51413)),
]
for a in alerts:
    cr._on_alert(a)
assert len(st.hashes) == 3, st.hashes
assert st.counters["bep51_replies"] == 1 and st.counters["bep51_hashes"] == 1
assert cr.live["dht_nodes"] == 40 and cr.live["rx_total"] == 1200
# nodos: 5.6.7.8 (respuesta BEP51), 8.8.4.4 (live_nodes), 4.4.4.4 (outgoing_get_peers)
assert {("5.6.7.8", 6881), ("8.8.4.4", 6881), ("4.4.4.4", 51413)} <= set(cr.known_nodes), cr.known_nodes
print("alerts and node sources: OK")

# ------------------------------------------------- filters for own / non-global nodes
class FakeSes:
    def __init__(s): s.sent = []; s.rules = []
    def dht_sample_infohashes(s, ep, target): s.sent.append(ep)
    def get_ip_filter(s):
        outer = s
        return types.SimpleNamespace(add_rule=lambda a, b, f: outer.rules.append((a, b, f)))
    def set_ip_filter(s, f): pass

st, cr = new_crawler()
cr.ses = FakeSes()
cr._on_alert(mk("external_ip_alert", external_address="203.0.113.5"))
assert "203.0.113.5" in cr.ext_ips and ("203.0.113.5", "203.0.113.5", 1) in cr.ses.rules
for ip in ("203.0.113.5", "192.168.1.10", "10.0.0.2", "127.0.0.1"):
    cr._add_node((ip, 6881))
assert not cr.known_nodes, f"should not accept {cr.known_nodes}"
cr._add_node(("8.8.8.8", 6881))
assert ("8.8.8.8", 6881) in cr.known_nodes
print("own/private IP filter + ip_filter: OK")

# ------------------------------------------------- limitador BEP 51 (fichas)
C.lt = types.SimpleNamespace(sha1_hash=lambda b: b)
st, cr = new_crawler()
cr.ses = FakeSes(); cr.sample_supported = True
for i in range(200):
    cr._add_node((f"8.8.{i // 250}.{i % 250 + 1}", 6881))
cr._tok_t = time.time() - 1.0          # 1 s of accumulated tokens
for _ in range(50):                    # fast loop: previously every call sent sample_qps queries
    cr._sample_nodes()
assert 8 <= len(cr.ses.sent) <= 22, len(cr.ses.sent)   # ~10 qps * 1 s (+bucket margin)
assert st.counters["bep51_queries"] == len(cr.ses.sent)
print(f"BEP 51 limiter: {len(cr.ses.sent)} queries for 1 s of tokens: OK")

# backpressure: with the sample-hash buffer full NOTHING more is requested (and no tokens pile up for a burst)
st, cr = new_crawler()
cr.ses = FakeSes(); cr.sample_supported = True; cr.cfg["bep51_buffer"] = 50
for i in range(100):
    cr._add_node((f"8.8.7.{i + 1}", 6881))
for i in range(60):
    st.add_hash("%040x" % (0x777000 + i), "bep51")
cr._tok_t = time.time() - 5
cr._sample_nodes()
assert cr.ses.sent == [] and cr.live["bep51_paused"] and cr._tokens == 0, "buffer full: paused"
while st.queues["other"]:
    st.next_pending()
cr._tok_t = time.time() - 1; cr._sample_nodes()
assert 1 <= len(cr.ses.sent) <= 22 and not cr.live["bep51_paused"], len(cr.ses.sent)
print("BEP 51 on demand (paused while the buffer is full, no burst on resume): OK")

# silent nodes: retried at 2, 4 and 8 min, then forgotten; a responsive node is revisited after its interval (with jitter)
st, cr = new_crawler()
cr.ses = FakeSes(); cr.sample_supported = True
cr._add_node(("9.9.9.1", 6881)); cr._add_node(("9.9.9.2", 6881))
real_time = time.time
t0 = real_time(); clock = [t0]
C.time.time = lambda: clock[0]
try:
    for step in range(5):
        n0 = len(cr.ses.sent)
        cr._tokens = 5; cr._tok_t = clock[0]
        cr._sample_nodes()
        if ("9.9.9.2", 6881) in cr.ses.sent[n0:]:          # the good one always answers (asks for a 10 min wait)
            cr._on_alert(mk("dht_sample_infohashes_alert", samples=[], nodes=[], endpoint=("9.9.9.2", 6881), interval=600))
        clock[0] += 8 * 60 * 1.31
    assert ("9.9.9.1", 6881) not in cr.known_nodes and st.counters["bep51_nodes_evicted"] == 1, cr.known_nodes
    assert cr.known_nodes[("9.9.9.2", 6881)][1] == 0, "the responsive node stays, with no failures"
    assert cr.ses.sent.count(("9.9.9.1", 6881)) == 3, cr.ses.sent
    assert cr.ses.sent.count(("9.9.9.2", 6881)) < 5, "its interval is respected: not queried on every pass"
finally:
    C.time.time = real_time
print("silent nodes forgotten after 3 attempts; responsive node interval respected: OK")

# repeated failures disable sampling and are logged
class BadSes(FakeSes):
    def dht_sample_infohashes(s, ep, target): raise TypeError("firma incorrecta")
st, cr = new_crawler()
cr.ses = BadSes(); cr.sample_supported = True
for i in range(100):
    cr._add_node((f"8.8.9.{i + 1}", 6881))
cr._tok_t = time.time() - 60; cr._tokens = 0
cr._sample_nodes()
assert cr.sample_supported is False
print("disabled after 20 failures: OK")

# ------------------------------------------------- Store: purge and retry of failed hashes
st = Store(tempfile.mkdtemp())
ih_old, ih_new, ih_back = "aa" * 20, "bb" * 20, "cc" * 20
now = int(time.time())
st.hashes[ih_old] = {"status": "failed", "failed_at": now - 5 * 86400, "last_seen": now - 5 * 86400, "attempts": 3}
st.hashes[ih_new] = {"status": "failed", "failed_at": now - 3600, "last_seen": now - 3600, "attempts": 3}
st.hashes[ih_back] = {"status": "failed", "failed_at": now - 7 * 3600, "last_seen": now - 100, "attempts": 3}
assert st.maintenance()["failed_purged"] == 1 and ih_old not in st.hashes and ih_new in st.hashes
st.add_hash(ih_new, "announce", prio=True)          # failed 1 h ago: NOT retried yet
assert st.hashes[ih_new]["status"] == "failed"
st.add_hash(ih_back, "announce", prio=True)         # failed 7 h ago and reappears: retried
assert st.hashes[ih_back]["status"] == "pending" and st.hashes[ih_back]["attempts"] == 0
assert st.next_pending() == ih_back
st.flush(force=True)
print("purge/retry of failed hashes and flush: OK")

# ------------------------------------------------- bounded queue, LIFO, attempts and promotion
st = Store(tempfile.mkdtemp())
st.queue_max = 1000
for i in range(5000):                                   # flood of sample hashes
    st.add_hash("%040x" % i, "bep51")
assert len(st.queue) <= 1000 and len(st.hashes) <= 1000, (len(st.queue), len(st.hashes))
assert st.counters["hashes_new"] == 5000 and st.counters["stale_dropped"] >= 4000
first = st.next_pending()
assert first == "%040x" % 4999, first                   # newest first
st.mark_failed(first)                                   # a single attempt for a sample hash seen once
assert st.hashes[first]["status"] == "failed"
print("bounded queue / LIFO / 1 attempt for samples: OK")

# --- DEFERRED RETRIES: with the queue FULL the retry is no longer lost (bug in the previous version)
st2 = Store(tempfile.mkdtemp()); st2.queue_max = 5; st2.sched = "fixed"     # fixed queue order (no randomness)
for i in range(5): st2.add_hash("%040x" % i, "bep51")                      # cola normal llena (5/5)
st2.add_hash("dd" * 20, "announce", prio=True)
x = st2.next_pending(); assert x == "dd" * 20
st2.mark_failed(x)
assert x in st2.hashes and st2.hashes[x]["status"] == "retry", st2.hashes.get(x)      # previously: dropped immediately
assert st2.counters["stale_dropped"] == 0 and st2.counters["retries_scheduled"] == 1
assert st2.next_pending() != x                                                       # not due yet (RETRY_DELAY)
st2.retry[0] = (time.time() - 1, x)                                                  # "pasan 5 minutos"
assert st2.next_pending() == x and st2.hashes[x]["status"] == "probing"
st2.mark_failed(x); st2.retry[0] = (time.time() - 1, x); assert st2.next_pending() == x
st2.mark_failed(x); assert st2.hashes[x]["status"] == "failed"                       # 3.er fallo: definitivo
# a hash waiting for retry that is seen again is retried right away
st2.add_hash("ee" * 20, "announce", prio=True); y = st2.next_pending(); st2.mark_failed(y)
assert st2.hashes[y]["status"] == "retry"; st2.add_hash(y, "get_peers", prio=True)
assert st2.hashes[y]["status"] == "pending" and st2.next_pending() == y
print("deferred retries with a full queue: OK")

# promotion on repeat sightings, no duplicates in the priority queue
st3 = Store(tempfile.mkdtemp())
st3.add_hash("01" * 20, "bep51"); st3.add_hash("02" * 20, "bep51"); st3.add_hash("03" * 20, "bep51")
for _ in range(5):
    st3.add_hash("01" * 20, "bep51")                    # visto repetidamente => prioritario
assert list(st3.queue_prio).count("01" * 20) == 1, list(st3.queue_prio)
assert st3.next_pending() == "01" * 20                  # jumps ahead of "03" (newer)
print("promotion without duplicates: OK")

# --- stress: 300k hashes overflow neither memory nor queue
st4 = Store(tempfile.mkdtemp())
t0 = time.time()
for i in range(300_000):
    st4.add_hash("%040x" % i, "bep51")
assert len(st4.hashes) <= st4.queue_max + 5 and len(st4.queue) <= st4.queue_max
print(f"300k hashes in {time.time() - t0:.1f}s; in memory: {len(st4.hashes)}; dropped: {st4.counters['stale_dropped']}: OK")

# --- refresh: heap, staleness and postponement
st5 = Store(tempfile.mkdtemp()); base = int(time.time())
for i in range(3):
    st5.save_torrent("%040x" % (i + 1), {"name": f"t{i}", "size": 10, "files": [("a.mkv", 10)]})
    st5.torrents["%040x" % (i + 1)]["health_at"] = base - (10 + i) * 3600           # t2 is the oldest
st5._rebuild_heap = None
import heapq
from records import refresh_due_at
st5._refresh_heap = [(refresh_due_at(r), ih) for ih, r in st5.torrents.items()]; heapq.heapify(st5._refresh_heap)
assert st5.refresh_due() and st5.next_refresh() == "%040x" % 3
st5.update_health("%040x" % 2, 5, 1, "scrape")                                        # refreshed: leaves the pending queue
assert st5.next_refresh() == "%040x" % 1 and st5.next_refresh() is None
st5.defer_refresh("%040x" % 3); assert not st5.refresh_due()
print("refresh queue (heap): OK")

# --- probe split: refresh share guaranteed even with 500 pending
class FP:  # fake add_torrent params
    def __init__(s): s.flags = 0; s.save_path = ""; s.trackers = []
class FH:
    def __init__(s, ih): s.ih = ih; s.scraped = 0
    def scrape_tracker(s, idx=-1): s.scraped += 1
    def status(s): return types.SimpleNamespace(num_seeds=0, list_seeds=0, num_peers=0, list_peers=0)
class Ses2(FakeSes):
    def __init__(s): super().__init__(); s.added = []
    def add_torrent(s, params): h = FH(params.ih); s.added.append(h); return h
    def remove_torrent(s, h): pass
def parse_magnet(uri): p = FP(); p.ih = uri.split("btih:")[1]; return p
C.lt = types.SimpleNamespace(sha1_hash=lambda b: b, parse_magnet_uri=parse_magnet,
                             torrent_flags=types.SimpleNamespace(upload_mode=1, auto_managed=2, paused=4))
st6 = Store(tempfile.mkdtemp())
for i in range(30):
    st6.save_torrent("%040x" % (i + 1000), {"name": f"idx{i}", "size": 10, "files": [("a.mkv", 10)]}); st6.torrents["%040x" % (i + 1000)]["health_at"] = base - 86400
st6._refresh_heap = [(refresh_due_at(r), ih) for ih, r in st6.torrents.items()]; heapq.heapify(st6._refresh_heap)
for i in range(500): st6.add_hash("%040x" % (i + 5000), "bep51")
cr6 = C.Crawler(st6, {"max_probes": 20, "refresh_share": 0.25, "state_file": os.path.join(st6.dir, "x.bin")})
cr6.ses = Ses2(); cr6._fill_probes()
kinds = [j.kind for j in cr6.jobs.values()]
assert len(kinds) == 20 and kinds.count("refresh") == 5 and kinds.count("fetch") == 15, (kinds.count("refresh"), kinds.count("fetch"))
assert all(h.scraped == len(C.DEFAULT_TRACKERS) for h in cr6.ses.added), "the scrape is requested as soon as the torrent is added, from EVERY tracker"
print("refresh share 5/20 with 500 pending + immediate scrape: OK")

# --- health: a scrape cuts the wait short; without one the full window is waited and it is marked "swarm"
job = next(j for j in cr6.jobs.values() if j.kind == "fetch")
class HB: ...
cr6._on_alert(mk("scrape_reply_alert", handle=types.SimpleNamespace(info_hashes=lambda: types.SimpleNamespace(v1=job.ih)), complete=77, incomplete=12))
assert job.scrape_ok and job.seeds == 77 and job.peers == 12
rfj = next(j for j in cr6.jobs.values() if j.kind == "refresh")
cr6._on_alert(mk("scrape_reply_alert", handle=types.SimpleNamespace(info_hashes=lambda: types.SimpleNamespace(v1=rfj.ih)), complete=9, incomplete=4))
rfj.finish_at = time.time() - 1; cr6._service_jobs(time.time())
rec = st6.torrents[rfj.ih]; assert rec["health_src"] == "scrape" and rec["seeders"] == 9 and unpack_hh(rec["hh"])[-1][3] == "s" and rfj.ih not in cr6.jobs
rf2 = next(j for j in cr6.jobs.values() if j.kind == "refresh"); rf2.finish_at = time.time() - 1; cr6._service_jobs(time.time())
assert st6.torrents[rf2.ih]["health_src"] == "swarm"                                       # no tracker response
print("health measurement source (scrape/swarm): OK")

# --- multi-tracker scrape: the MAXIMUM across trackers is taken, responders are counted, and it ends as soon as all have answered
job = next(j for j in cr6.jobs.values() if j.kind == "fetch" and not j.scrape_ok)
def reply(j, c, i): cr6._on_alert(mk("scrape_reply_alert", handle=types.SimpleNamespace(info_hashes=lambda: types.SimpleNamespace(v1=j.ih)), complete=c, incomplete=i))
NT = len(C.DEFAULT_TRACKERS); assert job.scrape_asked == NT
job.saved = True; job.finish_at = time.time() + 18                  # metadata already in; waiting for the maximum window
reply(job, 5, 1); reply(job, 30, 9)
assert job.scrape_n == 2 and job.seeds == 30 and job.peers == 9, (job.scrape_n, job.seeds, job.peers)
assert job.finish_at - time.time() <= 4.5, "after the 1st response, only wait briefly for the rest"
reply(job, 0, 0); reply(job, 12, 2)
for _ in range(NT - 4): reply(job, 1, 1)
assert job.scrape_n == NT and job.finish_at - time.time() <= 1.5, "all answered: closes now"
st6.save_torrent(job.ih, {"name": "x", "size": 1, "files": [("a.mkv", 1)]}) if job.ih not in st6.torrents else None
job.finish_at = time.time() - 1; cr6._service_jobs(time.time())
e = unpack_hh(st6.torrents[job.ih]["hh"])[-1]; assert e[3] == "s" and e[1] == 30 and e[4] == NT, e
print("multi-tracker scrape (maximum, number of responses, early close): OK")


# --- per-tracker breakdown: who says what, errors, peers returned, only CONNECTED seeders, persistence and magnet
TR = C.DEFAULT_TRACKERS
job = next(j for j in cr6.jobs.values() if j.kind == "refresh")
hd_ = lambda: types.SimpleNamespace(info_hashes=lambda: types.SimpleNamespace(v1=job.ih))
cr6._on_alert(mk("scrape_reply_alert", handle=hd_(), complete=120, incomplete=30, tracker_url=TR[0]))
cr6._on_alert(mk("scrape_reply_alert", handle=hd_(), complete=3, incomplete=1, tracker_url=TR[1]))
cr6._on_alert(mk("scrape_failed_alert", handle=hd_(), error_message="timed out", tracker_url=TR[2]))
cr6._on_alert(mk("tracker_reply_alert", handle=hd_(), num_peers=0, tracker_url=TR[0]))
cr6._on_alert(mk("tracker_error_alert", handle=hd_(), error_message="host not found", tracker_url=TR[3]))
assert job.tr[TR[0]] == {"s": 120, "l": 30, "r": 0} and job.tr[TR[1]] == {"s": 3, "l": 1}
assert job.tr[TR[2]] == {"e": "timed out"} and job.tr[TR[3]] == {"ae": "host not found"}
# status: connected num_seeds count; list_seeds (PEX/DHT candidates) NO LONGER do
job.handle.status = lambda: types.SimpleNamespace(num_seeds=2, list_seeds=900, num_peers=5, list_peers=4000)
job.finish_at = time.time() - 1; ih_d = job.ih; cr6._service_jobs(time.time())
rec = st6.get(ih_d); d = unpack_hd(rec["hd"])                         # full record: the breakdown is read from the journal
assert rec["seeders"] == 120 and d["cs"] == 2 and d["cp"] == 3 and rec.get("seed_ok_at"), d
assert unpack_hh(rec["hh"])[-1][5] == 2, "the history also stores confirmed seeders"
m = C.__dict__.get("magnet_of") or __import__("textutil").magnet_of
mg = m(rec)
from urllib.parse import quote
assert mg.index(quote(TR[0], safe="")) < mg.index(quote(TR[1], safe="")) < mg.index(quote(TR[2], safe="")), "the magnet carries the trackers, the one reporting most seeders first"
st6.close(); st6b = Store(st6.dir); rr = st6b.get(ih_d)
assert unpack_hd(rr["hd"])["tr"][0]["s"] == 120 and unpack_hd(rr["hd"])["cs"] == 2, "the breakdown survives a restart (journal)"
rep_ = cr6.tracker_report(); byu = {x["url"]: x for x in rep_}
assert byu[TR[2]]["scrape_fail"] >= 1 and byu[TR[2]]["last_error"] == "timed out" and byu[TR[0]]["scrape_ok"] >= 1
assert C.Crawler._tr_url(mk("x", url="udp://a:1/announce")) == "udp://a:1/announce", "libtorrent 1.2: url attribute"
print("per-tracker breakdown, connected seeders only, persistence, magnet with trackers, tracker health: OK")

# --- a worse non-scrape measurement does NOT refresh an old value (previously health_at became "now")
from records import apply_health
r0 = {"hh": [], "health_src": "scrape", "seeders": 50, "peers": 5, "health_at": 1000}
apply_health(r0, 0, 0, 1000 + 86400, "swarm", 0, {"at": 1000 + 86400, "tr": [], "cs": 0, "cp": 0, "ci": 0, "dht": 0, "md": 0})
assert r0["seeders"] == 50 and r0["health_at"] == 1000 and r0["checked_at"] == 1000 + 86400
assert refresh_due_at(r0) > 1000 + 86400, "the refresh is postponed using checked_at"
print("figures kept with their REAL date: OK")


# --- announce_peer with IP:port: top-priority queue and direct connection to the peer that has it
st8 = Store(tempfile.mkdtemp()); st8.sched = "fixed"
A, B, G, G2 = "a1" * 20, "b2" * 20, "c3" * 20, "d4" * 20
st8.add_hash(G, "get_peers")                                     # seen once in a query: normal queue
assert G in st8.queue_getpeers and G not in st8.queue_prio
st8.add_hash(G2, "get_peers"); st8.add_hash(G2, "get_peers")     # visto dos veces: prioritaria
assert G2 in st8.queue_prio
st8.add_hash(A, "announce", prio=True, peer=("8.8.8.8", 51413))
st8.add_hash(B, "bep51")
st8.add_hash(B, "announce", prio=True, peer=("9.9.9.9", 6881))  # already known: now with a peer -> moves up
st8.add_hash(B, "announce", prio=True, peer=("9.9.9.9", 6881))  # repeated: not duplicated
assert st8.hash_peers(B) == [("9.9.9.9", 6881)] and st8.hash_peers(A) == [("8.8.8.8", 51413)]
order = [st8.next_pending() for _ in range(4)]
assert order[:2] == [B, A] and order[2] == G2 and order[3] == G, order     # with peer (LIFO) > repeated > rest
assert st8.counters["probes_peer"] == 2 and st8.counters["probes_prio"] == 1 and st8.counters["probes_getpeers"] == 1
st8.save_torrent(A, {"name": "a", "size": 1, "files": [("a.mkv", 1)]})
assert st8.counters["metadata_ok_peer"] == 1
# a full normal queue does NOT drop a hash that is also waiting in the "with peer" queue
st8.queue_max = 3
X = "e5" * 20
st8.add_hash(X, "bep51"); st8.add_hash(X, "announce", prio=True, peer=("1.1.1.1", 1))
for i in range(5):
    st8.add_hash("%040x" % (0xabc0 + i), "bep51")
assert X in st8.hashes and st8.next_pending() == X, "still alive and comes out first"
# a failed hash re-announced by someone who has it: retried
F = "f6" * 20
st8.add_hash(F, "bep51"); st8.hashes[F].update(status="failed", failed_at=int(time.time()) - 7200, attempts=1)
st8.add_hash(F, "announce", prio=True, peer=("2.2.2.2", 2))
assert st8.hashes[F]["status"] == "pending" and F in st8.queue_peer
print("known-peer queue > repeated > rest, no hashes lost when full, retry of announced failures: OK")

class FH2(FH):
    def __init__(s, ih): super().__init__(ih); s.connected = []
    def connect_peer(s, ep, *a): s.connected.append(ep)
class Ses3(Ses2):
    def add_torrent(s, params): h = FH2(params.ih); s.added.append(h); return h
cr8 = C.Crawler(st8, {"state_file": os.path.join(st8.dir, "x.bin"), "max_probes": 1})
cr8.ses = Ses3()
st8.queue_max = 20000
st8.add_hash("a7" * 20, "announce", prio=True, peer=("8.8.4.4", 6000))
cr8._fill_probes()
h8 = cr8.ses.added[-1]
assert h8.connected == [("8.8.4.4", 6000)], h8.connected
cr8._on_alert(mk("dht_announce_alert", info_hash=H("a7" * 20), ip="1.0.0.1", port=7000))    # another arrives while probing
assert h8.connected[-1] == ("1.0.0.1", 7000) and st8.counters["direct_connects"] == 2
cr8._on_alert(mk("dht_announce_alert", info_hash=H("a8" * 20), ip="192.168.1.5", port=7000))
assert st8.hash_peers("a8" * 20) == [], "private IP: not stored as a peer"
print("direct connection to the announcing peer (at probe start and during it): OK")


# --- probe with NO peers at all: abandoned after 20 s (frees the slot); with peers it waits for the timeout
st9 = Store(tempfile.mkdtemp())
cr9 = C.Crawler(st9, {"state_file": os.path.join(st9.dir, "x.bin"), "max_probes": 2, "probe_timeout": 45, "nopeer_giveup": 20})
cr9.ses = Ses3()
for ih in ("11" * 20, "22" * 20):
    st9.add_hash(ih, "bep51")
cr9._fill_probes()
j_dead, j_live = cr9.jobs["22" * 20], cr9.jobs["11" * 20]
j_dead.handle.status = lambda: types.SimpleNamespace(num_seeds=0, num_peers=0, list_peers=0, list_seeds=0)
j_live.handle.status = lambda: types.SimpleNamespace(num_seeds=0, num_peers=1, list_peers=6, list_seeds=0)
cr9._service_jobs(time.time())
assert len(cr9.jobs) == 2, "nothing is abandoned before 20 s"
for j in (j_dead, j_live):
    j.started -= 25
cr9._service_jobs(time.time())
assert "22" * 20 not in cr9.jobs and "11" * 20 in cr9.jobs and st9.counters["probe_nopeers"] == 1
assert st9.hashes["22" * 20]["status"] == "failed", "BEP 51 seen once: a single attempt"
print("early abandonment of probes without any peer: OK")

# --- libtorrent settings for fetching metadata: only those present in the installed version
captured = {}
C.lt = types.SimpleNamespace(**{**C.lt.__dict__, "__version__": "2.0.9",
                                "default_settings": lambda: {"connection_speed": 30, "smooth_connects": True, "peer_connect_timeout": 15,
                                                             "max_metadata_size": 3 << 20, "torrent_connect_boost": 30},
                                "session_params": lambda d: captured.update(d) or types.SimpleNamespace(),
                                "session": lambda sp: "ses"})
cr10 = C.Crawler(Store(tempfile.mkdtemp()), {"state_file": "/nonexistent/x.bin", "connection_speed": 250})
assert cr10._make_session() == "ses"
assert captured["connection_speed"] == 250 and captured["peer_connect_timeout"] == 7 and captured["max_metadata_size"] == 32 << 20
assert "smooth_connects" not in captured and "torrent_connect_boost" not in captured, "nothing that raises the load by default"
cr11 = C.Crawler(Store(tempfile.mkdtemp()), {"state_file": "/nonexistent/x.bin"}); cr11._make_session()
assert captured["connection_speed"] == 30, "by default, the same connection budget as libtorrent"
cr12 = C.Crawler(Store(tempfile.mkdtemp()), {"state_file": "/nonexistent/x.bin", "max_probes": 1}); cr12.ses = Ses3()
cr12.store.add_hash("33" * 20, "bep51"); cr12._fill_probes(); jj = cr12.jobs["33" * 20]; jj.started -= 60
jj.handle.status = lambda: types.SimpleNamespace(num_seeds=0, num_peers=0, list_peers=0, list_seeds=0)
cr12._service_jobs(time.time()); assert "33" * 20 in cr12.jobs, "early abandonment disabled by default"
print("connection settings (connection_speed, smooth_connects, max_metadata_size…) filtered by version: OK")


# --- ADAPTIVE split across queues: the most successful one gets most probes, while still exploring
import random as _r
_r.seed(3)
stb = Store(tempfile.mkdtemp()); stb.sched = "adaptive"; stb.queue_max = 100000
rate = {"peer": 0.03, "prio": 0.04, "getpeers": 0.15, "other": 0.01}     # "real" success rate of each source (simulated)
src_of = {"peer": ("announce", True, ("8.8.8.8", 1)), "prio": None, "getpeers": ("get_peers", False, None), "other": ("bep51", False, None)}
n = 0
def feed(k):
    global n
    n += 1; ih = "%040x" % (0x5000000 + n)
    if k == "prio":
        stb.add_hash(ih, "bep51"); stb.add_hash(ih, "bep51")
    else:
        src, pr, pe = src_of[k]; stb.add_hash(ih, src, prio=pr, peer=pe)
for _ in range(3000):
    for k in rate: feed(k)
picked = Counter()
for _ in range(6000):
    ih = stb.next_pending(); k = stb.hashes[ih]["qk"]; picked[k] += 1
    if _r.random() < rate[k]:
        stb.save_torrent(ih, {"name": ih, "size": 1, "files": [("a.mkv", 1)]})
    else:
        stb.mark_failed(ih)
    if _ % 4 == 0:
        for k in rate: feed(k)
assert picked["getpeers"] > 0.6 * 6000 and min(picked.values()) > 30, picked
print("adaptive split across queues (the best gets most, the others keep being explored):", dict(picked), "OK")

# --- probing WITHOUT announcing in the DHT: torrent with DHT disabled + dht_get_peers + peers handed to the probe
class SesN(Ses3):
    def __init__(s): super().__init__(); s.lookups = []
    def dht_get_peers(s, h): s.lookups.append(h)
C.lt = types.SimpleNamespace(**{**C.lt.__dict__, "torrent_flags": types.SimpleNamespace(upload_mode=1, auto_managed=2, paused=4, disable_dht=64),
                                "sha1_hash": lambda b: b})
stn = Store(tempfile.mkdtemp()); crn = C.Crawler(stn, {"state_file": os.path.join(stn.dir, "x.bin"), "max_probes": 1})
crn.ses = SesN(); stn.add_hash("44" * 20, "get_peers"); crn._fill_probes()
jn = crn.jobs["44" * 20]
assert crn.ses.lookups == [bytes.fromhex("44" * 20)] and stn.counters["dht_lookups"] == 1
crn._on_alert(mk("dht_get_peers_reply_alert", info_hash=H("44" * 20), peers=[("5.5.5.5", 1), ("6.6.6.6", 2), ("10.0.0.1", 3), ("5.5.5.5", 1)]))
assert jn.handle.connected == [("5.5.5.5", 1), ("6.6.6.6", 2)] and stn.counters["dht_connects"] == 2, jn.handle.connected
crn._extract = lambda ti: {"name": "x", "size": 1, "files": [("a.mkv", 1)]}
jn.started -= 12; crn._on_alert(mk("metadata_received_alert", handle=types.SimpleNamespace(info_hashes=lambda: types.SimpleNamespace(v1="44" * 20),
                                   torrent_file=lambda: None)))
assert stn.counters["meta_t_lt20"] == 1, dict(stn.counters)
crd = C.Crawler(Store(tempfile.mkdtemp()), {"state_file": "/x/y", "dht_announce": True}); crd.ses = SesN()
assert crd._no_dht_announce() is False, "--dht-announce: comportamiento antiguo"
print("probing without announcing in the DHT (own lookup, peers to the probe, timing histogram): OK")


# --- hashes: failures/retries go to hashes.jsonl as they happen (no periodic rewrite); a crash keeps them, pending ones
# (they expire in minutes) are only saved on shutdown
import json as _json
dh = tempfile.mkdtemp()
sth = Store(dh)
sth.add_hash("aa" * 20, "bep51"); sth.add_hash("bb" * 20, "bep51"); sth.mark_failed(sth.next_pending())
sth.flush()
log_ = [_json.loads(l) for l in open(os.path.join(dh, "hashes.jsonl"))]
assert [e["t"] for e in log_] == ["s"] and log_[0]["i"] == "bb" * 20 and log_[0]["h"]["status"] == "failed", log_
crash = Store(dh)                                        # no close: crash
assert crash.hashes["bb" * 20]["status"] == "failed" and "aa" * 20 not in crash.hashes
sth.close(); saved = _json.load(open(os.path.join(dh, "hashes.json")))
assert set(saved) == {"aa" * 20, "bb" * 20} and os.path.getsize(os.path.join(dh, "hashes.jsonl")) == 0
st_b = Store(dh); assert st_b.hashes["bb" * 20]["status"] == "failed" and st_b.hashes["aa" * 20]["status"] == "pending"
st_b.add_hash("bb" * 20, "bep51"); st_b.hashes["bb" * 20]["failed_at"] = 0; st_b.add_hash("bb" * 20, "bep51")
assert st_b.hashes["bb" * 20]["status"] == "pending"     # revived: removed from the log
st_b.flush(); assert _json.loads(open(os.path.join(dh, "hashes.jsonl")).read().splitlines()[-1])["t"] == "x"
print("hashes: change log (crash-safe), full snapshot on shutdown, no periodic rewrite: OK")

# --- persistent cumulative traffic: only the DELTAS of each libtorrent session are added
st7 = Store(tempfile.mkdtemp()); cr7 = C.Crawler(st7, {"state_file": os.path.join(st7.dir, "x.bin")})
cr7._on_session_stats({"net.recv_bytes": 1000, "net.sent_bytes": 400})
cr7._on_session_stats({"net.recv_bytes": 5000, "net.sent_bytes": 900})
assert st7.stats.life["rx_bytes"] == 4000 and st7.stats.life["tx_bytes"] == 500, st7.stats.life
print("cumulative traffic by deltas: OK")
print("ALL OK", sys.version.split()[0])
