"""
Binary packing of the health data: per-tracker breakdown (hd) and health history (hh) as struct-packed bytes, with
interned tables for tracker URLs / error messages. Used by the fixed slots of health.bin (slots.py) and by
the packed peer store. The API and exports always unpack them back to the same JSON as before.
"""
import struct
import sys

# ------------------------------------------------------------------ interned tables (small ids)
class _Table:
    """Interned strings -> small ids. `on_new(id, s)` is called for each new one (to persist the table)."""
    """str <-> small id. Bounded: whatever does not fit is stored as `overflow`."""

    def __init__(self, cap, overflow):
        self.items, self.ids, self.cap = [], {}, cap
        self.on_new = None
        self.overflow = self.id(overflow)

    def id(self, s):
        i = self.ids.get(s)
        if i is None:
            if len(self.items) >= self.cap:
                return self.overflow
            i = len(self.items)
            self.items.append(sys.intern(s))
            self.ids[self.items[i]] = i
            if self.on_new is not None:
                self.on_new(i, s)
        return i

    def get(self, i):
        return self.items[i] if 0 <= i < len(self.items) else ""


URLS = _Table(60_000, "?")            # trackers (the configured ones + those from the .torrent itself)
ERRS = _Table(4_000, "error")         # tracker error messages (few, and repetitive)

NONE_I = -2_147_483_648               # "field absent" in the int32s
NONE_H = 0xFFFF                       # "no error" in the uint16s

# ------------------------------------------------------------------ breakdown of a measurement (hd)
_HD_HEAD = struct.Struct("<IiiiiB")                 # at, cs, cp, ci, dht, md                    21 B
_HD_TR = struct.Struct("<Hiii" "HH")                # url, s, l, r, e, ae                         18 B


def _i(v):
    return NONE_I if v is None else max(min(int(v), 2_147_483_647), -2_147_483_647)


def pack_hd(d, urls=None, errs=None):
    """dict {"at","tr":[{"u","s","l","r","e","ae"}],"cs","cp","ci","dht","md"} -> bytes. URLs and error messages are
    stored as ids of `urls` / `errs` (tables persisted next to the file that stores the bytes: see slots.py)."""
    if d is None or isinstance(d, (bytes, bytearray)):
        return d
    urls, errs = urls or URLS, errs or ERRS
    try:
        return _pack_hd_fast(d, urls, errs)
    except (struct.error, TypeError, ValueError, OverflowError):
        return _pack_hd_safe(d, urls, errs)


def _pack_hd_fast(d, urls, errs):
    g = d.get
    parts = [_HD_HEAD.pack(g("at", 0), g("cs", 0), g("cp", 0), g("ci", 0), g("dht", 0), 1 if g("md") else 0)]
    uids, eids, pk, N = urls.ids, errs.ids, _HD_TR.pack, NONE_I
    for t in g("tr") or ():
        tg = t.get
        u = uids.get(tg("u"))
        if u is None:
            u = urls.id(tg("u") or "?")
        s, l, r, e, ae = tg("s"), tg("l"), tg("r"), tg("e"), tg("ae")
        if e is not None:
            e = eids.get(e)
            if e is None:
                e = errs.id(str(tg("e")))
        if ae is not None:
            ae = eids.get(ae)
            if ae is None:
                ae = errs.id(str(tg("ae")))
        parts.append(pk(u, N if s is None else s, N if l is None else l, N if r is None else r,
                        NONE_H if e is None else e, NONE_H if ae is None else ae))
    return b"".join(parts)


def _pack_hd_safe(d, urls, errs):
    parts = [_HD_HEAD.pack(int(d.get("at", 0)) & 0xFFFFFFFF, _i(d.get("cs", 0)), _i(d.get("cp", 0)), _i(d.get("ci", 0)),
                           _i(d.get("dht", 0)), 1 if d.get("md") else 0)]
    for t in d.get("tr") or []:
        e, ae = t.get("e"), t.get("ae")
        parts.append(_HD_TR.pack(urls.id(t.get("u") or "?"), _i(t.get("s")), _i(t.get("l")), _i(t.get("r")),
                                 NONE_H if e is None else errs.id(str(e)), NONE_H if ae is None else errs.id(str(ae))))
    return b"".join(parts)


def unpack_hd(b, urls=None, errs=None):
    """bytes -> the same dict that was written (only with the keys that existed)."""
    if not b or isinstance(b, dict):
        return b or None
    urls, errs = urls or URLS, errs or ERRS
    at, cs, cp, ci, dht, md = _HD_HEAD.unpack_from(b, 0)
    tr = []
    for u, s, l, r, e, ae in _HD_TR.iter_unpack(b[_HD_HEAD.size:]):
        t = {"u": urls.get(u)}
        if s != NONE_I:
            t["s"] = s
        if l != NONE_I:
            t["l"] = l
        if r != NONE_I:
            t["r"] = r
        if e != NONE_H:
            t["e"] = errs.get(e)
        if ae != NONE_H:
            t["ae"] = errs.get(ae)
        tr.append(t)
    return {"at": at, "tr": tr, "cs": cs, "cp": cp, "ci": ci, "dht": dht, "md": md}


def hd_tracker_urls(b, urls=None):
    """Only URLs and seeders (for the magnet link), without building dicts: [(url, seeders|None, answered)]"""
    if not b:
        return []
    if isinstance(b, dict):
        return [(t.get("u"), t.get("s"), "s" in t or "r" in t) for t in b.get("tr") or []]
    urls = urls or URLS
    return [(urls.get(u), None if s == NONE_I else s, s != NONE_I or r != NONE_I)
            for u, s, _l, r, _e, _ae in _HD_TR.iter_unpack(b[_HD_HEAD.size:])]


# ------------------------------------------------------------------ health history (hh)
# Each measurement: [at, seeders, peers, source("s"/"w"/"l"/"n"), no. of trackers, (confirmed seeders)]
# 12 B per measurement: at uint32 · seeders and peers 24-bit (low 16 + high 8) · source (2 bits) + trackers (6 bits) ·
# confirmed seeders uint8 (255 = absent). Limits: 16.7 M seeders/peers, 63 trackers, 254 confirmed seeders per probe.
_HH = struct.Struct("<IHHBBBB")
HH_SIZE = _HH.size
_SRC_CHARS = "swln"
_SRC_IX = {c: i for i, c in enumerate(_SRC_CHARS)}
_M24 = 0xFFFFFF


def _hh_pack(at, s, p, src, n, cs):
    s = min(max(int(s), 0), _M24)
    p = min(max(int(p), 0), _M24)
    return _HH.pack(int(at) & 0xFFFFFFFF, s & 0xFFFF, p & 0xFFFF, s >> 16, p >> 16,
                    _SRC_IX.get((src or "w")[0], 1) | (min(max(int(n), 0), 63) << 2),
                    255 if cs is None else min(max(int(cs), 0), 254))


def pack_hh(entries):
    if isinstance(entries, (bytes, bytearray)):
        return bytes(entries)
    return b"".join(_hh_pack(e[0], e[1], e[2], e[3] if len(e) > 3 else "w", e[4] if len(e) > 4 else 1, e[5] if len(e) > 5 else None)
                    for e in entries or ())


def _hh_entry(t):
    at, slo, plo, shi, phi, sn, cs = t
    e = [at, slo | (shi << 16), plo | (phi << 16), _SRC_CHARS[sn & 3], sn >> 2]
    if cs != 255:
        e.append(cs)
    return e


def unpack_hh(b):
    """bytes -> list of measurements (journal and API format)."""
    if not b:
        return []
    if isinstance(b, list):
        return b
    return [_hh_entry(t) for t in _HH.iter_unpack(b)]


def hh_len(b):
    return len(b) // HH_SIZE if isinstance(b, (bytes, bytearray)) else len(b or ())


def hh_last(b):
    if not b:
        return None
    if isinstance(b, list):
        return b[-1]
    return _hh_entry(_HH.unpack_from(b, len(b) - HH_SIZE))


def hh_reversed(b):
    """Measurements from newest to oldest, without unpacking everything if iteration stops early."""
    if not b:
        return
    if isinstance(b, list):
        yield from reversed(b)
        return
    for off in range(len(b) - HH_SIZE, -1, -HH_SIZE):
        yield _hh_entry(_HH.unpack_from(b, off))


def hh_append(b, entry, max_len):
    """Appends a measurement (replacing the last one if the timestamp matches) and trims to max_len."""
    b = b if isinstance(b, (bytes, bytearray)) else pack_hh(b)
    at = int(entry[0]) & 0xFFFFFFFF
    if b and _HH.unpack_from(b, len(b) - HH_SIZE)[0] > at:      # older than the newest (journal replayed twice): merge in order
        ents = {e[0]: e for e in unpack_hh(b)}
        ents[at] = list(entry)
        return pack_hh([ents[k] for k in sorted(ents)][-max_len:])
    new = _hh_pack(entry[0], entry[1], entry[2], entry[3] if len(entry) > 3 else "w", entry[4] if len(entry) > 4 else 1,
                   entry[5] if len(entry) > 5 else None)
    if b and _HH.unpack_from(b, len(b) - HH_SIZE)[0] == (int(entry[0]) & 0xFFFFFFFF):
        b = b[:-HH_SIZE]
    b = b + new
    if len(b) > max_len * HH_SIZE:
        b = b[len(b) - max_len * HH_SIZE:]
    return bytes(b)


# ------------------------------------------------------------------ compact inverted index
