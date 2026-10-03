"""
Compact IN-MEMORY representations (the on-disk format, JSON Lines, does not change: data is packed when loaded and
unpacked when written or served by the API).

Why: with ~70,000 torrents the process went above 2 GB. What took the most (measured on a synthetic catalogue shaped
like the production one):
  file index           ~620 MB  -> 1.1 M tokens with a SINGLE torrent, each one with its own set (216 B empty)
  per-tracker breakdown ~460 MB -> 20 dicts per torrent, with the tracker URL repeated as a str in each one
  health history       ~170 MB  -> lists of lists of ints (growing to 40 measurements per torrent: ~570 MB)
  dict keys            ~100 MB  -> json does not share keys across lines: 27 strs per torrent
Here: struct-packed bytes (history and breakdown), interned tables for URLs/errors/clients and sets that are only
created when really needed.
"""
import struct
import sys

# ------------------------------------------------------------------ interned tables (small ids)
class _Table:
    """str <-> small id. Bounded: whatever does not fit is stored as `overflow`."""

    def __init__(self, cap, overflow):
        self.items, self.ids, self.cap = [], {}, cap
        self.overflow = self.id(overflow)

    def id(self, s):
        i = self.ids.get(s)
        if i is None:
            if len(self.items) >= self.cap:
                return self.overflow
            i = len(self.items)
            self.items.append(sys.intern(s))
            self.ids[self.items[i]] = i
        return i

    def get(self, i):
        return self.items[i] if 0 <= i < len(self.items) else ""


URLS = _Table(60_000, "?")            # trackers (the configured ones + those from the .torrent itself)
ERRS = _Table(4_000, "error")         # tracker error messages (few, and repetitive)

NONE_I = -2_147_483_648               # "field absent" in the int32s
NONE_H = 0xFFFF                       # "no error" in the uint16s

# ------------------------------------------------------------------ breakdown of a measurement (hd)
_HD_HEAD = struct.Struct("<IiiiiB")                 # at, cs, cp, ci, dht, md                    21 B
_HD_TR = struct.Struct("<Hiiiii" "HH")              # url, s, l, r, (reserved ×2), e, ae         26 B


def _i(v):
    return NONE_I if v is None else max(min(int(v), 2_147_483_647), -2_147_483_647)


def pack_hd(d):
    """dict {"at","tr":[{"u","s","l","r","e","ae"}],"cs","cp","ci","dht","md"} -> bytes
    (Fast path without per-field calls: at startup hundreds of thousands of journal measurements get packed.)"""
    if d is None or isinstance(d, (bytes, bytearray)):
        return d
    try:
        return _pack_hd_fast(d)
    except (struct.error, TypeError, ValueError, OverflowError):
        return _pack_hd_safe(d)


def _pack_hd_fast(d):
    g = d.get
    parts = [_HD_HEAD.pack(g("at", 0), g("cs", 0), g("cp", 0), g("ci", 0), g("dht", 0), 1 if g("md") else 0)]
    uids, eids, pk, N = URLS.ids, ERRS.ids, _HD_TR.pack, NONE_I
    for t in g("tr") or ():
        tg = t.get
        u = uids.get(tg("u"))
        if u is None:
            u = URLS.id(tg("u") or "?")
        s, l, r, e, ae = tg("s"), tg("l"), tg("r"), tg("e"), tg("ae")
        if e is not None:
            e = eids.get(e)
            if e is None:
                e = ERRS.id(str(tg("e")))
        if ae is not None:
            ae = eids.get(ae)
            if ae is None:
                ae = ERRS.id(str(tg("ae")))
        parts.append(pk(u, N if s is None else s, N if l is None else l, N if r is None else r, N, N,
                        NONE_H if e is None else e, NONE_H if ae is None else ae))
    return b"".join(parts)


def _pack_hd_safe(d):
    parts = [_HD_HEAD.pack(int(d.get("at", 0)) & 0xFFFFFFFF, _i(d.get("cs", 0)), _i(d.get("cp", 0)), _i(d.get("ci", 0)),
                           _i(d.get("dht", 0)), 1 if d.get("md") else 0)]
    for t in d.get("tr") or []:
        e, ae = t.get("e"), t.get("ae")
        parts.append(_HD_TR.pack(URLS.id(t.get("u") or "?"), _i(t.get("s")), _i(t.get("l")), _i(t.get("r")), NONE_I, NONE_I,
                                 NONE_H if e is None else ERRS.id(str(e)), NONE_H if ae is None else ERRS.id(str(ae))))
    return b"".join(parts)


def unpack_hd(b):
    """bytes -> the same dict that was written (only with the keys that existed)."""
    if not b or isinstance(b, dict):
        return b or None
    at, cs, cp, ci, dht, md = _HD_HEAD.unpack_from(b, 0)
    tr = []
    for u, s, l, r, _x, _y, e, ae in _HD_TR.iter_unpack(b[_HD_HEAD.size:]):
        t = {"u": URLS.get(u)}
        if s != NONE_I:
            t["s"] = s
        if l != NONE_I:
            t["l"] = l
        if r != NONE_I:
            t["r"] = r
        if e != NONE_H:
            t["e"] = ERRS.get(e)
        if ae != NONE_H:
            t["ae"] = ERRS.get(ae)
        tr.append(t)
    return {"at": at, "tr": tr, "cs": cs, "cp": cp, "ci": ci, "dht": dht, "md": md}


def hd_tracker_urls(b):
    """Only URLs and seeders (for the magnet link), without building dicts: [(url, seeders|None, answered)]"""
    if not b:
        return []
    if isinstance(b, dict):
        return [(t.get("u"), t.get("s"), "s" in t or "r" in t) for t in b.get("tr") or []]
    return [(URLS.get(u), None if s == NONE_I else s, s != NONE_I or r != NONE_I)
            for u, s, _l, r, _x, _y, _e, _ae in _HD_TR.iter_unpack(b[_HD_HEAD.size:])]


# ------------------------------------------------------------------ health history (hh)
# Each measurement: [at, seeders, peers, source("s"/"w"/"l"/"n"), no. of trackers, (confirmed seeders)]
_HH = struct.Struct("<IiiBBi")                      # 18 B per measurement (previously ~180 B)
HH_SIZE = _HH.size


def pack_hh(entries):
    if isinstance(entries, (bytes, bytearray)):
        return bytes(entries)
    out = bytearray()
    for e in entries or ():
        src = e[3] if len(e) > 3 else "w"
        out += _HH.pack(int(e[0]) & 0xFFFFFFFF, _i(e[1]), _i(e[2]), ord((src or "w")[0]) & 0x7F,
                        min(int(e[4]) if len(e) > 4 else 1, 255), _i(e[5]) if len(e) > 5 else NONE_I)
    return bytes(out)


def _hh_entry(t):
    at, s, p, src, n, cs = t
    e = [at, s, p, chr(src), n]
    if cs != NONE_I:
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
    new = pack_hh([entry])
    if b and _HH.unpack_from(b, len(b) - HH_SIZE)[0] == (int(entry[0]) & 0xFFFFFFFF):
        b = b[:-HH_SIZE]
    b = b + new
    if len(b) > max_len * HH_SIZE:
        b = b[len(b) - max_len * HH_SIZE:]
    return bytes(b)


# ------------------------------------------------------------------ compact inverted index
class PostingIndex(dict):
    """token -> infohashes, stored according to size:
         1 element    -> the infohash str itself (0 B extra: the same object as the store.torrents key)
         2..SMALL     -> tuple (8 B per element)
         more         -> list (8 B per element and O(1) append; a set cost ~30-60 B per element)
    An infohash is added only once per token (the index never receives duplicates), so no set is needed.
    iter(t) iterates without copying (what the search engine uses: set.update/intersection accept any iterable),
    get()/[] return a NEW set (compatibility) and count() returns the size without building anything."""
    SMALL = 16
    __slots__ = ()

    def add(self, t, ih):
        v = dict.get(self, t)
        if v is None:
            dict.__setitem__(self, t, ih)
        elif v.__class__ is str:
            if v != ih:
                dict.__setitem__(self, t, (v, ih))
        elif v.__class__ is tuple:
            if ih not in v:
                dict.__setitem__(self, t, v + (ih,) if len(v) < self.SMALL else list(v + (ih,)))
        else:
            v.append(ih)

    def discard(self, t, ih):
        v = dict.get(self, t)
        if v is None:
            return
        if v.__class__ is str:
            if v == ih:
                dict.__delitem__(self, t)
            return
        if ih not in v:
            return
        w = tuple(x for x in v if x != ih)
        if v.__class__ is list and len(w) > self.SMALL:
            v.remove(ih)
        else:
            dict.__setitem__(self, t, w[0] if len(w) == 1 else w)

    def iter(self, t):
        """The infohashes of a token, without copying them."""
        v = dict.get(self, t)
        if v is None:
            return ()
        return (v,) if v.__class__ is str else v

    def get(self, t, default=None):
        v = dict.get(self, t)
        if v is None:
            return default
        return {v} if v.__class__ is str else set(v)

    def __getitem__(self, t):
        v = dict.__getitem__(self, t)
        return {v} if v.__class__ is str else set(v)

    def count(self, t):
        v = dict.get(self, t)
        return 0 if v is None else 1 if v.__class__ is str else len(v)

    def compact(self):
        """Trims list over-allocation (after the initial load)."""
        for t, v in dict.items(self):
            if v.__class__ is list:
                dict.__setitem__(self, t, v[:])
