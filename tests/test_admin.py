"""Admin panel tests: hide rules, peers and API. Run: python tests/test_admin.py"""
import hashlib, ipaddress, os, sys, tempfile, time, types
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from store import Store
from hiderules import Matcher, RuleError, validate

MB, GB = 1 << 20, 1 << 30
def H(i): return hashlib.sha1(str(i).encode()).hexdigest()
def mk(st, i, name, files, seeds=10):
    assert st.save_torrent(H(i), {"name": name, "size": sum(s for _, s in files), "files": files, "piece_length": 262144},
                           seeders=seeds, peers=seeds // 2, src="scrape")

d = tempfile.mkdtemp()
st = Store(d)
mk(st, 1, "Ubuntu 24.04 Desktop", [("ubuntu-24.04-desktop-amd64.iso", 6 * GB)], 500)
mk(st, 2, "Holiday Photos", [("Holiday/IMG_0001.jpg", MB), ("Holiday/SecretStuff/notes.txt", 1000)], 3)
mk(st, 3, "Foobar Collection", [("foobar.mkv", GB)], 40)
mk(st, 4, "Foo Bar Live", [("Foo.Bar.Live.2024.mkv", GB)], 30)
mk(st, 5, "Big pack", [(f"part{j:03d}.bin", MB) for j in range(250)] + [("zz/secretstuff-final.bin", MB)], 7)   # match beyond the first 200 files
mk(st, 6, "Café Olé", [("cafe.flac", 30 * MB)], 5)
def ids(q, **kw): p = {"q": q}; p.update(kw); return {x["ih"] for x in st.search(p)["results"]}

# ---------------------------------------------------------------- validation
for bad in [("", "all", "word"), ("x", "nope", "word"), ("x", "all", "nope"), ("(", "all", "regex"), (".*", "all", "regex"), ("!!", "all", "word")]:
    try:
        validate(*bad); raise AssertionError(f"should fail: {bad}")
    except RuleError:
        pass
print("rule validation: OK")

# ---------------------------------------------------------------- modes and scopes
def prev(term, scope="all", mode="word"):
    return {x["ih"] for x in st.preview_rule(Matcher({"id": "p", "term": term, "scope": scope, "mode": mode}), 50)["sample"]}
assert prev("foo") == {H(4)}, prev("foo")                                  # whole word: not "foobar"
assert prev("foo", mode="substring") == {H(3), H(4)}
assert prev("foo bar") == {H(4)}                                           # word sequence
assert prev("secretstuff") == {H(2), H(5)}, prev("secretstuff")           # in files, even > 200 files
assert prev("secretstuff", scope="name") == set()
assert prev("holiday", scope="files") == {H(2)} and prev("holiday", scope="name") == {H(2)}
assert prev("cafe ole") == {H(6)} and prev("CAFÉ", mode="substring") == {H(6)}   # accent- and case-insensitive
assert prev(r"part0\d\d\.bin", mode="regex") == {H(5)}
print("word / substring / regex modes and scopes: OK")

# ---------------------------------------------------------------- hide from search
assert H(2) in ids("holiday")
r = st.rules.add("secretstuff", "files", "word"); st.recompute_hidden()
assert set(st.hidden) == {H(2), H(5)}
assert H(2) not in ids("holiday") and not ids("secretstuff") and H(5) not in ids("pack")
assert H(2) not in ids("") and H(5) not in ids("")                         # not on the front page either
assert st.search({"q": ""})["total"] == 4
assert not any(s["text"].endswith("holiday") for s in st.suggest("holi")), st.suggest("holi")
assert all(x["ih"] not in st.hidden for x in st.related(H(3)))
assert st.analytics()["hidden"] == 2 and all(t["ih"] not in st.hidden for t in st.analytics()["top_seeded"])
assert H(2) in st.torrents                                                 # NOT deleted
# new torrent matching the rule: hidden when indexed
mk(st, 7, "Another", [("x/SecretStuff.doc", 100)])
assert H(7) in st.hidden and H(7) not in ids("another")
# pause the rule -> visible again; re-enable -> hidden
st.rules.update(r["id"], enabled=False); st.recompute_hidden()
assert not st.hidden and H(2) in ids("holiday")
st.rules.update(r["id"], enabled=True); st.recompute_hidden()
assert H(2) in st.hidden
det = st.match_details(H(2))
assert det and det[0]["in_name"] is False and det[0]["files"][0]["path"] == "Holiday/SecretStuff/notes.txt"
a, b = det[0]["files"][0]["spans"][0]
assert "Holiday/SecretStuff/notes.txt"[a:b] == "SecretStuff"
print("hide without deleting, front page, autocomplete, related, new torrents, pause: OK")

# ---------------------------------------------------------------- peers
ps = st.peers
ps.note(H(2), "203.0.113.5", 51413, 1, "qBittorrent 4.6", "c")
ps.note(H(2), "203.0.113.6", 6881, -1, "", "d")
ps.note(H(2), "203.0.113.6", 6882, 0, "Transmission", "c")             # a real connection sets port and role
ps.note(H(1), "203.0.113.5", 51413, 0, "qBittorrent 4.6", "c")          # the same peer in a NON-hidden torrent
ps.note(H(3), "198.51.100.9", 1, 1, "", "c")
ps.note(H(5), "198.51.100.9", 1, 1, "", "c")
R = lambda ps_, ih, ip: ps_.entry(ih, ip)                         # unpacked entry
e = R(ps, H(2), "203.0.113.6")
assert e["port"] == 6882 and e["role"] == 0 and e["src"] == "cd" and e["client"] == "Transmission", e
ps.note(H(2), "203.0.113.6", 6882, -1, "", "a")                          # unknown role does not downgrade
assert R(ps, H(2), "203.0.113.6")["role"] == 0
agg = {x["ip"]: x for x in ps.aggregate(list(st.hidden), hidden=st.hidden)}
assert agg["203.0.113.5"]["role"] == 1 and agg["203.0.113.5"]["elsewhere"] == 1
assert agg["198.51.100.9"]["elsewhere"] == 1 and agg["203.0.113.6"]["elsewhere"] == 0
found = ps.find([ipaddress.ip_network("203.0.113.0/24")])
assert set(found) == {H(1), H(2)}
assert set(ps.find([ipaddress.ip_address("198.51.100.9")], only_seeds=True)) == {H(3), H(5)}
# per-torrent cap
ps.per_torrent = 3
for i in range(10):
    ps.note(H(6), f"192.0.2.{i + 1}", 1000 + i, now=time.time() + i)
assert ps.count(H(6)) == 3 and "192.0.2.10" in ps.ips_of(H(6))
ps.per_torrent = 300
print("peers: role, ports, sources, aggregate, IP/CIDR search, cap: OK")

# ---------------------------------------------------------------- persistence
st.close()
st2 = Store(d)
assert set(st2.hidden) == {H(2), H(5), H(7)}, st2.hidden
assert R(st2.peers, H(2), "203.0.113.6")["port"] == 6882 and R(st2.peers, H(2), "203.0.113.6")["client"] == "Transmission" and st2.peers.n == ps.n
print("persistence of rules, hidden torrents and peers: OK")

# ---------------------------------------------------------------- crawler: connected peers and forced refresh
import crawler as C
class PI:
    def __init__(s, ip, flags=0, client=b"qBittorrent", progress=0.0): s.ip, s.flags, s.client, s.progress = ip, flags, client, progress
C.lt = types.SimpleNamespace(peer_info=types.SimpleNamespace(seed=0x400), sha1_hash=lambda b: b)
cr = C.Crawler(st2, {"state_file": os.path.join(d, "x.bin")})
job = C.Job(H(3), types.SimpleNamespace(get_peer_info=lambda: [PI(("8.8.8.8", 1)), PI(("9.9.9.9", 2), 0x400), PI(("10.0.0.1", 3)),
                                                              PI(("1.1.1.1", 4), 0, b"x", 1.0)]), "refresh", 20)
cr._collect_peers(job)                                   # no metadata: unknown role unless the seeder flag is set
got = st2.peers.ips_of(H(3))
assert R(st2.peers, H(3), "8.8.8.8")["role"] == -1 and R(st2.peers, H(3), "9.9.9.9")["role"] == 1 and "10.0.0.1" not in got, got   # private IPs left out
job.saved = True
cr._collect_peers(job)
assert R(st2.peers, H(3), "8.8.8.8")["role"] == 0 and R(st2.peers, H(3), "1.1.1.1")["role"] == 1
cr._on_alert(type("dht_get_peers_reply_alert", (), {"info_hash": H(3), "peers": staticmethod(lambda: [("4.4.4.4", 6881), ("192.168.1.2", 1)])})())
got = st2.peers.ips_of(H(3)); assert "4.4.4.4" in got and "192.168.1.2" not in got
cr._on_alert(type("dht_announce_alert", (), {"info_hash": H(3), "ip": "5.5.5.5", "port": 7000})())
assert R(st2.peers, H(3), "5.5.5.5")["src"] == "a"
started = []
cr._start_job = lambda ih, kind: (started.append((ih, kind)), cr.jobs.__setitem__(ih, object()))
assert st2.request_refresh([H(2), H(5), "0" * 40, H(2)]) == 2
cr.cfg["max_probes"] = 3
cr._fill_probes()
assert started[:2] == [(H(2), "refresh"), (H(5), "refresh")], started
print("crawler: connected/DHT/announce peers, private IP filter, forced refresh: OK")

# ---------------------------------------------------------------- API
import app as A
A.STATE["store"] = st2
A.admin.init(A.app, lambda: A.STATE["store"], "admin", d)
c = A.app.test_client()
from version import __version__, CHANGELOG
v = c.get("/api/version").json
assert v["version"] == __version__ == CHANGELOG[0][0] and v["changelog"][0]["version"] == __version__
assert c.get("/api/stats").json["version"] == __version__
assert len({x[0] for x in CHANGELOG}) == len(CHANGELOG), "duplicate versions in the CHANGELOG"
print("version in /api/version and /api/stats:", __version__, "OK")
assert c.get("/api/admin/me").json == {"admin": False}
assert c.get("/api/admin/rules").status_code == 401
assert c.get(f"/api/torrent/{H(2)}").status_code == 404                       # hidden: public 404
assert c.get(f"/api/torrent/{H(1)}").status_code == 200 and "hidden_by" not in c.get(f"/api/torrent/{H(1)}").json
assert c.post("/api/admin/login", json={"password": "nope"}).status_code == 403
assert c.post("/api/admin/login", data={"password": "admin"}).status_code == 415   # form post: rejected (CSRF)
assert c.post("/api/admin/login", json={"password": "admin"}).status_code == 200
assert c.get("/api/admin/me").json == {"admin": True}
t = c.get(f"/api/torrent/{H(2)}").json
assert t["hidden_by"] and t["peers_known"]
rl = c.get("/api/admin/rules").json
assert rl["rules"][0]["count"] == 3
pv = c.post("/api/admin/rules/preview", json={"term": "foo", "scope": "all", "mode": "substring"}).json
assert pv["count"] == 2
assert c.post("/api/admin/rules", json={"term": "(", "mode": "regex"}).status_code == 400
nr = c.post("/api/admin/rules", json={"term": "ubuntu", "scope": "name", "mode": "word"}).json
assert nr["rule"]["count"] == 1 and H(1) in st2.hidden
assert H(1) not in {x["ih"] for x in c.get("/api/search?q=ubuntu").json["results"]}
hl = c.get("/api/admin/hidden?rule=" + nr["rule"]["id"]).json
assert hl["total"] == 1 and hl["results"][0]["matches"][0]["in_name"]
pr = c.get("/api/admin/peers").json
ips = {x["ip"]: x for x in pr["results"]}
assert "203.0.113.5" in ips and ips["203.0.113.5"]["elsewhere"] == 0        # H(1) is now hidden too
s1 = c.post("/api/admin/peer-search", json={"ips": "198.51.100.9, garbage"}).json
assert {x["ih"] for x in s1["results"]} == {H(3)} and s1["invalid"] == ["garbage"]
s2 = c.post("/api/admin/peer-search", json={"ips": "198.51.100.9", "include_hidden": True}).json
assert {x["ih"] for x in s2["results"]} == {H(3), H(5)}
s3 = c.post("/api/admin/peer-search", json={"from_rule": ""}).json
assert H(3) in {x["ih"] for x in s3["results"]}                             # peers of hidden torrents that are in visible ones
assert c.post("/api/admin/refresh", json={"rule": ""}).json["queued"] >= 1
assert c.delete("/api/admin/rules/" + nr["rule"]["id"], json={}).json["ok"] and H(1) not in st2.hidden
assert c.post("/api/admin/logout", json={}).status_code == 200 and c.get("/api/admin/rules").status_code == 401
# changing the password invalidates the session
c.post("/api/admin/login", json={"password": "admin"})
A.admin._CFG["pwd"], A.admin._CFG["pwd_tag"] = "other", "new-tag"
assert c.get("/api/admin/rules").status_code == 401
# attempt limit
A.admin._FAILS.clear()
codes = [c.post("/api/admin/login", json={"password": "wrong"}).status_code for _ in range(6)]
assert codes[:5] == [403] * 5 and codes[5] == 429, codes
print("API: server-side login, JSON required, public 404 for hidden, rules, peers, peer search, attempt limit: OK")
st2.close()
print("ALL OK")
