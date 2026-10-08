"""Binary packing of health history / breakdowns and the fixed-slot files: exact round trip.
Run: python tests/test_packing.py"""
import json, os, subprocess, sys, tempfile
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
from packing import hh_append, hh_last, hh_len, hh_reversed, pack_hd, pack_hh, unpack_hd, unpack_hh, hd_tracker_urls
from slots import HD_LEN, HIST_LEN, SLOT, Breakdowns, HealthFile, History

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

d = tempfile.mkdtemp()
hf = HealthFile(os.path.join(d, "health.bin"))
h, x = History(hf), Breakdowns(hf)
h.put(5, hh); x.put(5, hd); x.put(2, None)
assert h.get(5) == hh and h.get(0) == [] and h.get(99) == [] and x.get(5) == hd and x.get(2) is None and x.get(99) is None
assert 5 * SLOT < os.path.getsize(os.path.join(d, "health.bin")) <= 6 * SLOT, "fixed slots: document d at d * 1 KiB"
assert SLOT == 1024 and HIST_LEN + HD_LEN == SLOT and 4096 % SLOT == 0, "a measurement dirties ONE page"
h.put(5, unpack_hh(b3)); assert h.get(5) == unpack_hh(b3) and x.get(5) == hd     # a full history next to a breakdown
big = {**hd, "tr": [{"u": f"udp://tracker-{i}.example.org:6969/announce", "s": i, "l": i, "r": 0} for i in range(200)]}
x.put(7, big); got = x.get(7)
assert got["cs"] == 2 and len(got["tr"]) == 28 and got["tr"][0] == big["tr"][0], "too many trackers: trimmed to fit the slot"
hf.close()
# a NEW process (empty in-memory tables) must read the same tracker URLs and errors back
code = (f"import sys; sys.path.insert(0, {ROOT!r}); from slots import *; hf = HealthFile({os.path.join(d, 'health.bin')!r}); "
        f"import json; print(json.dumps([Breakdowns(hf).get(5), Breakdowns(hf).get(7)['tr'][27], History(hf).get(5)]))")
out = json.loads(subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout)
assert out[0] == hd and out[1] == big["tr"][27] and out[2] == unpack_hh(b3), out
print("health slots (1 KiB: history + breakdown), trimming, string tables persisted across processes: OK")
print("ALL OK (packing)")
