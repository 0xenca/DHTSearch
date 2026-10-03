"""In-memory compact representations: exact round trip. Run: python tests/test_packing.py"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from packing import PostingIndex, hh_append, hh_last, hh_len, hh_reversed, pack_hd, pack_hh, unpack_hd, unpack_hh, hd_tracker_urls
from records import dump_rec, normalize_record

hd = {"at": 1790626402, "tr": [{"u": "udp://a:1/announce", "s": 120, "l": 30, "r": 0}, {"u": "udp://b:2/announce", "e": "timed out"},
                               {"u": "http://c/announce", "ae": "host not found"}, {"u": "udp://d:4/announce"}],
      "cs": 2, "cp": 3, "ci": 5, "dht": 40, "md": 1}
assert unpack_hd(pack_hd(hd)) == hd
assert unpack_hd(pack_hd({**hd, "tr": [{"u": "x", "s": 10 ** 12}]}))["tr"][0]["s"] == 2_147_483_647, "out of range: clamped, does not fail"
assert hd_tracker_urls(pack_hd(hd))[:2] == [("udp://a:1/announce", 120, True), ("udp://b:2/announce", None, False)]
hh = [[1790412212, 6354, 6692, "s", 1], [1790433889, 6691, 7999, "s", 3, 2], [1790455583, 0, 0, "w", 0]]
b = pack_hh(hh)
assert unpack_hh(b) == hh and hh_len(b) == 3 and hh_last(b) == hh[-1] and list(hh_reversed(b)) == hh[::-1]
b2 = hh_append(b, [1790455583, 5, 1, "s", 2], 40)
assert hh_len(b2) == 3 and hh_last(b2) == [1790455583, 5, 1, "s", 2], "same timestamp: replaces"
b3 = b
for i in range(50):
    b3 = hh_append(b3, [1790500000 + i, i, 0, "s", 1], 40)
assert hh_len(b3) == 40 and unpack_hh(b3)[-1][0] == 1790500049
print("packed breakdown and history: exact round trip: OK")

rec = {"ih": "ab" * 20, "name": "x", "size": 1, "files": [["a.mkv", 1]], "hh": hh, "hd": hd, "trackers": [], "exts": ["mkv"],
       "category": "Vídeo", "health_src": "scrape"}
r = normalize_record(dict(rec))
assert isinstance(r["hh"], bytes) and isinstance(r["hd"], bytes)
d = dump_rec(r)
assert d["hh"] == hh and d["hd"] == hd and d["trackers"] == [] and d["exts"] == ["mkv"] and d["files"] == [["a.mkv", 1]]
assert r["category"] == "Video" and d["category"] == "Video"          # Spanish name (<= 2.9) read, English written
print("record: packed on load and identical when dumped (journal/API): OK")

ix = PostingIndex()
for i in range(40):
    ix.add("big", f"h{i}")
ix.add("one", "h1"); ix.add("two", "h1"); ix.add("two", "h2"); ix.add("two", "h2")
assert dict.get(ix, "one") == "h1" and dict.get(ix, "two") == ("h1", "h2") and isinstance(dict.get(ix, "big"), list)
assert ix.get("one") == {"h1"} and ix["two"] == {"h1", "h2"} and ix.count("big") == 40 and ix.get("nope") is None
assert set(ix.iter("big")) == {f"h{i}" for i in range(40)} and list(ix.iter("nope")) == []
ix.discard("two", "h1"); assert dict.get(ix, "two") == "h2"
ix.discard("one", "h1"); assert "one" not in ix
for i in range(30):
    ix.discard("big", f"h{i}")
assert ix["big"] == {f"h{i}" for i in range(30, 40)} and isinstance(dict.get(ix, "big"), tuple)
print("compact inverted index (str / tuple / list): OK")
print("ALL OK (packing)")
