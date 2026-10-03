"""Search engine and store tests (indexes, journal, migration, retries)."""
import json, os, random, sys, tempfile, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from store import Store, normalize_record

MB, GB = 1 << 20, 1 << 30
import hashlib
def H(i): return hashlib.sha1(str(i).encode()).hexdigest()
def mk(st, i, name, files, seeds=None, src="scrape", size=None, category=None):
    fl = [(p, s) for p, s in files]
    ok = st.save_torrent(H(i), {"name": name, "size": size if size is not None else sum(s for _, s in fl), "files": fl,
                                "piece_length": 262144, "created": 1_600_000_000 + i, "trackers": ["udp://t/announce"]},
                         seeders=seeds or 0, peers=(seeds or 0) // 2, src=src if seeds is not None else "none")
    assert ok, name

d = tempfile.mkdtemp(); st = Store(d)
mk(st, 1, "Ubuntu 24.04 Desktop amd64", [("ubuntu-24.04-desktop-amd64.iso", 6 * GB)], seeds=500)
mk(st, 2, "Ubuntu 22.04 Server", [("ubuntu-22.04-live-server-amd64.iso", 2 * GB)], seeds=80)
mk(st, 3, "Debian 12 netinst", [("debian-12-netinst.iso", 600 * MB)], seeds=200)
mk(st, 4, "Big Buck Bunny 1080p", [("Big.Buck.Bunny.1080p.mkv", 700 * MB), ("subs/en.srt", 50_000)], seeds=40)
mk(st, 5, "Sintel 4K", [("Sintel.4K.mkv", 3 * GB), ("poster.jpg", 200_000)], seeds=120)
mk(st, 6, "The Matrix Reloaded 1080p x264", [("The.Matrix.Reloaded.2003.1080p.x264.mkv", 4 * GB)], seeds=60)
mk(st, 7, "Matrix Collection", [("Matrix.1.mkv", 2 * GB), ("Matrix.2.mkv", 2 * GB), ("extras/making of the matrix.mkv", GB)], seeds=10)
mk(st, 8, "Café del Mar Vol 3", [("01 - Cafe del Mar.flac", 40 * MB), ("02 - Sunset.flac", 38 * MB)], seeds=25)
mk(st, 9, "Cafe Tacvba discography", [("Re/01 Ingrata.mp3", 9 * MB)], seeds=3)
mk(st, 10, "Huge archive", [(f"data/part{j:03d}.bin", MB) for j in range(250)], seeds=None)       # not measured
mk(st, 11, "Ubuntu Wallpapers Pack", [(f"img{j}.png", MB) for j in range(30)], seeds=0)              # measured and dead
def q(text, **kw):
    p = {"q": text}; p.update(kw); return st.search(p)
def ids(r): return [x["ih"] for x in r["results"]]
def names(r): return [x["name"] for x in r["results"]]

# --- basics
assert ids(q("ubuntu desktop")) == [H(1)]
assert set(ids(q("ubun"))) == {H(1), H(2), H(11)}, "prefix of the last word"
assert ids(q("ubuntu -server")) and H(2) not in ids(q("ubuntu -server"))
assert set(ids(q("debian OR sintel"))) == {H(3), H(5)}
assert set(ids(q("debian | sintel"))) == {H(3), H(5)}
# frases
assert set(ids(q('"the matrix"'))) == {H(6), H(7)}                     # name of F and file of G
assert ids(q('"the matrix" in:name')) == [H(6)]
assert ids(q('"matrix reloaded"')) == [H(6)]
assert ids(q('"reloaded matrix"')) == []                                # order matters
assert H(6) not in ids(q('matrix -"the matrix"')) and H(7) not in ids(q('matrix -"the matrix"'))
# accents and numeric tokens
assert set(ids(q("cafe"))) == {H(8), H(9)} and set(ids(q("café"))) == {H(8), H(9)}
assert set(ids(q("1080"))) == {H(4), H(6)}, ids(q("1080"))                # 1080p -> 1080
assert ids(q("264")) == [H(6)]
# operadores
assert set(ids(q("ext:mkv"))) == {H(4), H(5), H(6), H(7)}
assert set(ids(q("ext:mkv -ext:srt"))) == {H(5), H(6), H(7)}
assert set(ids(q("ext:flac,mp3"))) == {H(8), H(9)}
assert set(ids(q("cat:video"))) == {H(4), H(5), H(6), H(7)} and set(ids(q("cat:musica"))) == {H(8), H(9)}
assert set(ids(q("size>3gb"))) == {H(1), H(5), H(6), H(7)}, ids(q("size>3gb"))       # Sintel: 3 GB + 200 KB
assert set(ids(q("size:1gb..4gb"))) >= {H(2), H(5)} and H(3) not in ids(q("size:1gb..4gb"))
assert set(ids(q("seeders>100"))) == {H(1), H(3), H(5)}
assert set(ids(q("seeders>=120 cat:software"))) == {H(1), H(3)} or set(ids(q("seeders>=120 cat:software"))) == {H(1), H(3)}
assert ids(q("files>100")) == [H(10)] and set(ids(q("files:20..40"))) == {H(11)}
assert len(ids(q("age<1h"))) == 11 and ids(q("age>1d")) == [] and len(ids(q("indexed:24h"))) == 11
assert ids(q(f"hash:{H(6)[:10]}")) == [H(6)] and ids(q(f"hash:{H(6)}")) == [H(6)]
assert set(ids(q("ubuntu alive:yes"))) == {H(1), H(2)}, ids(q("ubuntu alive:yes"))
# H(11): measured 0 by ONE tracker -> "quiet", NOT yet "dead" (a tracker that does not know the torrent also answers 0)
assert ids(q("health:dead")) == [] and ids(q("health:quiet")) == [H(11)], (ids(q("health:dead")), ids(q("health:quiet")))
assert H(10) in ids(q("health:unverified")) and H(10) in ids(q("health:unknown")) and H(10) not in ids(q("health:dead"))
assert set(ids(q("health:alive"))) >= {H(1), H(2), H(3)} and H(11) not in ids(q("health:alive"))
assert set(ids(q("file:sunset"))) == {H(8)} and ids(q("name:sunset")) == []
r = q("size>abc"); assert r["warnings"], "an invalid operator must warn"
# explicit (form) parameters == operators
assert set(ids(q("", cat="Vídeo"))) == set(ids(q("cat:video")))
assert set(ids(q("", min_seeds=100))) == set(ids(q("seeders>=100")))
assert set(ids(q("", ext="mkv"))) == set(ids(q("ext:mkv")))
assert set(ids(q("", min_size=3 * GB))) == set(ids(q("size>=3gb")))
assert set(ids(q("ubuntu", scope="name"))) == {H(1), H(2), H(11)}
# --- orden
assert names(q("", sort="size"))[0] == "Ubuntu 24.04 Desktop amd64"
assert names(q("", sort="size", order="asc"))[0] != "Ubuntu 24.04 Desktop amd64"
assert names(q("", sort="name", order="asc"))[0] == "Big Buck Bunny 1080p"
assert q("", sort="whatever")["total"] == 11                             # unknown sort: does not break
assert names(q("sintel 4k"))[0] == "Sintel 4K"                           # coincidencia exacta gana
assert names(q("matrix"))[0] == "Matrix Collection", names(q("matrix"))  # short direct name > loose files
# --- highlighting and matching files
hl = q("matrix")["results"][0]["name_hl"]; assert any(seg[1] for seg in hl) and "".join(s[0] for s in hl) == "Matrix Collection"
hl = q("cafe")["results"][0]["name_hl"]; assert "".join(s[0] for s in hl) in ("Café del Mar Vol 3", "Cafe Tacvba discography") and any(s[1] for s in hl)
assert "Caf" in "".join(s[0] for s in hl if s[1]) or "Caf" in "".join(s[0] for s in hl)
mf = q("making")["results"][0]; assert mf["ih"] == H(7) and mf["matched_files"] and "making" in mf["matched_files"][0]["path"]
# --- sugerencias, autocompletado, relacionados
assert q("ubunto")["total"] == 0 and q("ubunto")["suggestion"] == "ubuntu"
assert q("ubunto desktop")["suggestion"] == "ubuntu desktop"
assert st.suggest("ubu")[0]["text"] == "ubuntu" and st.suggest("debian ubu")[0]["text"] == "debian ubuntu" and st.suggest("ubuntu ") == []
rel = st.related(H(1)); assert {x["ih"] for x in rel} >= {H(2), H(11)} and H(1) not in {x["ih"] for x in rel}
# --- facetas
f = q("ubuntu")["facets"]; assert {c["name"]: c["count"] for c in f["categories"]}.get("Software") == 2
f = q("", cat="Vídeo")["facets"]; assert {c["name"] for c in f["categories"]} >= {"Video", "Software"}      # old Spanish URL still works; facets in English
assert dict((e["name"], e["count"]) for e in q("ext:mkv")["facets"]["extensions"])["mkv"] == 4
# --- pagination
r = q("", per_page=5, page=2); assert len(r["results"]) == 5 and r["pages"] == 3 and r["total"] == 11
print("search: operators, phrases, OR, filters, sorting, highlighting, suggestions, facets: OK")

# --- health: a worse non-scrape measurement does NOT overwrite a recent verified one
st.update_health(H(1), 0, 0, "swarm"); assert st.get(H(1))["seeders"] == 500 and st.get(H(1))["health_src"] == "scrape"
time.sleep(1.1); st.update_health(H(1), 900, 400, "scrape"); g = st.get(H(1)); assert g["seeders"] == 900 and len(g["hh"]) == 2 and g["hh"][-1][3] == "s", g["hh"]
st.update_health(H(10), 7, 2, "swarm"); assert st.get(H(10))["health_src"] == "swarm" and q("health:verified")["total"] >= 10 - 1
print("verified health / history: OK")

# --- states: "dead" requires repeated confirmation by several trackers
from records import apply_health, health_state, refresh_due_at, REFRESH_INTERVAL
import store as ST
rec = st.torrents[H(11)]; rec["hh"] = []; rec["health_src"] = "none"; rec["seeders"] = rec["peers"] = 0
nowt = int(time.time())
def meas(at, s_, p_, src, n): apply_health(rec, s_, p_, at, src, n)
meas(nowt - 30 * 3600, 0, 0, "scrape", 1); assert health_state(rec) == "quiet"            # 1 tracker: not enough
meas(nowt - 20 * 3600, 0, 0, "scrape", 3); meas(nowt - 10 * 3600, 0, 0, "scrape", 3)
assert health_state(rec) == "quiet", "the streak with 1 tracker does not count: only 2 valid measurements"
meas(nowt - 1 * 3600, 0, 0, "scrape", 2)
assert health_state(rec) == "dead" and refresh_due_at(rec) == rec["health_at"] + REFRESH_INTERVAL["dead"]
assert H(11) in ids(q("health:dead"))
meas(nowt, 0, 3, "scrape", 2); assert health_state(rec) == "weak"                         # aparecen peers: resucita
meas(nowt + 1, 4, 3, "scrape", 2); assert health_state(rec) == "alive"
rec2 = st.torrents[H(9)]; rec2["hh"] = []; rec2["health_src"] = "scrape"; rec2["seeders"] = rec2["peers"] = 0
for k in range(3): apply_health(rec2, 0, 0, nowt - k * 3600, "scrape", 4)               # 3 measurements but only within 2 h
assert health_state(rec2) == "quiet", "needs ≥ 12 h of spacing"
rec3 = st.torrents[H(8)]; rec3["hh"] = []; rec3["seeders"] = rec3["peers"] = 0; rec3["health_src"] = "swarm"
assert health_state(rec3) == "unknown", "a zero without a tracker response is not evidence"
# exponential backoff for what cannot be measured
rec3["health_at"] = nowt; ivs = []
for k in range(12): apply_health(rec3, 0, 0, nowt + k, "swarm", 0); rec3["health_at"] = rec3["checked_at"] = nowt; ivs.append(refresh_due_at(rec3) - nowt)
assert ivs[0] == 1800 * 2 and ivs[-1] == 3 * 86400 and all(b >= a for a, b in zip(ivs, ivs[1:])), ivs
print("states alive/weak/quiet/dead/unknown and exponential backoff: OK")

# --- persistencia: journal + reinicio
st.close()
files = sorted(os.listdir(d)); assert "torrents.jsonl" in files and "torrents.json" not in files, files
lines = open(d + "/torrents.jsonl").read().splitlines(); assert lines[0].startswith('{"t":"n"') and any('"t":"h"' in l for l in lines)
st2 = Store(d)
assert len(st2.torrents) == 11 and st2.get(H(1))["seeders"] == 900 and len(st2.get(H(1))["hh"]) == 2
assert st2.search({"q": "ubuntu desktop"})["results"][0]["ih"] == H(1)                      # indexes rebuilt
assert st2.stats.life["sessions"] == 2
print("journal persistence and index rebuild: OK")
# compaction keeping concurrent writes
for i in range(300): st2.update_health(H(3), i, 1, "scrape")
before = st2.journal.lines; n = st2.compact_journal(); assert n == 11 and st2.journal.lines == 11, (before, n)
st2.update_health(H(3), 4242, 1, "scrape"); st2.close(); st3 = Store(d); assert st3.get(H(3))["seeders"] == 4242 and len(st3.torrents) == 11
print(f"journal compaction ({before} -> {n} lines) and continuity after compacting: OK")
# truncated last line (power cut): ignored, the rest is not lost
with open(d + "/torrents.jsonl", "a") as f: f.write('{"t":"n","r":{"ih":"dead')
st3.close(); st4 = Store(d); assert len(st4.torrents) == 11; st4.close()
print("journal truncated by a power cut: OK")

# --- English on disk; a journal written by <= 2.9 (Spanish category names) is converted once at startup
import json as _json
dc = tempfile.mkdtemp(); stc = Store(dc)
stc.save_torrent("c0" * 20, {"name": "Movie", "size": 5, "files": [("a.mkv", 5)]})
stc.save_torrent("c1" * 20, {"name": "Pics", "size": 5, "files": [("a.jpg", 5)]})
assert stc.torrents["c0" * 20]["category"] == "Video" and stc.get("c1" * 20)["category"] == "Images"
stc.close()
def disk_cats(d): return {o["r"]["ih"]: o["r"]["category"] for o in map(_json.loads, open(d + "/torrents.jsonl")) if o.get("t") == "n"}
assert disk_cats(dc) == {"c0" * 20: "Video", "c1" * 20: "Images"}, disk_cats(dc)
# simulate a 2.9 data dir: Spanish categories in the journal + the old Spanish blocklist header
lines = open(dc + "/torrents.jsonl", encoding="utf-8").read().replace('"category":"Video"', '"category":"Vídeo"').replace('"category":"Images"', '"category":"Imágenes"')
open(dc + "/torrents.jsonl", "w", encoding="utf-8").write(lines + _json.dumps({"t": "h", "ih": "c0" * 20, "s": 7, "p": 1, "a": int(time.time()), "g": "scrape", "n": 2}) + "\n")
import store as _store
open(dc + "/blocklist.txt", "w", encoding="utf-8").write(_store.LEGACY_BLOCKLIST_HEADER + "badword\n")
stc2 = Store(dc)
assert stc2.legacy_converted == 2 and stc2.torrents["c0" * 20]["category"] == "Video" and stc2.get("c0" * 20)["seeders"] == 7
assert stc2.search({"q": "cat:video"})["total"] == 1 and stc2.search({"q": "", "cat": "Vídeo"})["total"] == 1   # old links still work
stc2.close()
assert disk_cats(dc) == {"c0" * 20: "Video", "c1" * 20: "Images"} and "Vídeo" not in open(dc + "/torrents.jsonl", encoding="utf-8").read()
assert open(dc + "/blocklist.txt", encoding="utf-8").read() == _store.BLOCKLIST_HEADER + "badword\n"
stc3 = Store(dc); assert stc3.legacy_converted == 0 and stc3.get("c0" * 20)["seeders"] == 7; stc3.close()      # converted once only
print("English on disk; 2.9 journal and blocklist header converted once at startup: OK")
print("ALL OK (search/store)")
