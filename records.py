"""Format of a torrent record in memory / in the journal.

In MEMORY the file list is compressed (`_fz`: paths joined with \\n, zlib; `_fs`: sizes in a 64-bit array) because it is,
by far, what takes the most space (millions of paths). It is only decompressed when needed (torrent detail, one page of
results, phrase checks). In the JOURNAL it is stored as a plain list `files: [[path, size], …]`.
Keys starting with "_" are ephemeral: they are never written to the journal.
"""
import sys
import zlib
from array import array

from packing import hh_append, hh_reversed, pack_hd, pack_hh, unpack_hd, unpack_hh
from textutil import LEGACY_CATEGORIES, category_of, size_bucket, top_exts

HH_MAX = 40                      # health-history entries kept per torrent
SRC_CODE = {"scrape": "s", "swarm": "w", "legacy": "l", "none": "n"}      # "scrape" and "swarm" share a first letter: never use src[:1]!


def pack_files(files):
    if not files:
        return b"", array("Q")
    paths = "\n".join(p.replace("\n", " ") for p, _ in files)
    return zlib.compress(paths.encode("utf-8", "replace"), 1), array("Q", (max(int(s), 0) for _, s in files))


def files_of(rec):
    """List of (path, size) of a torrent."""
    fz = rec.get("_fz")
    if not fz:
        return []
    paths = zlib.decompress(fz).decode("utf-8", "replace").split("\n")
    return list(zip(paths, rec["_fs"]))


def dump_rec(rec, files=True):
    """Serializable record (no ephemeral keys; file list, history and breakdown unpacked). Same format in the journal and the API."""
    out = {k: v for k, v in rec.items() if not k.startswith("_")}
    out["hh"] = unpack_hh(rec.get("hh"))
    if rec.get("hd") is not None:
        out["hd"] = unpack_hd(rec["hd"])
    for k in ("trackers", "exts"):
        if isinstance(out.get(k), tuple):
            out[k] = list(out[k])
    if files:
        out["files"] = [[p, s] for p, s in files_of(rec)]
    return out


# Text values repeated across all records: a single copy in memory
_INTERN_VALUES = ("category", "health_src", "src", "created_by")
_EMPTY = ()


def compact_record(rec):
    """Same information, less memory: keys and repeated strings interned (json creates a new str per line),
    history and breakdown packed into bytes, empty lists shared."""
    out = {}
    for k, v in rec.items():
        out[sys.intern(k)] = v
    if out.get("ih").__class__ is str:
        out["ih"] = sys.intern(out["ih"])          # the same object in torrents, indexes and peers
    for k in _INTERN_VALUES:
        v = out.get(k)
        if v.__class__ is str and len(v) < 64:
            out[k] = sys.intern(v)
    out["hh"] = pack_hh(out.get("hh"))
    if out.get("hd") is not None:
        out["hd"] = pack_hd(out["hd"])
    for k in ("trackers", "exts"):
        v = out.get(k)
        if v is not None and not isinstance(v, tuple):
            out[k] = tuple(sys.intern(x) if k == "exts" and x.__class__ is str else x for x in v) if v else _EMPTY
    return out


def normalize_record(rec):
    """Fills in fields that did not exist in older versions and compresses the file list."""
    files = rec.pop("files", None)
    if files is not None:
        rec["_fz"], rec["_fs"] = pack_files(files)
        rec.setdefault("file_count", len(files))
        if "category" not in rec:
            rec["category"] = category_of(files, rec.get("name", ""))
        if "exts" not in rec:
            rec["exts"] = top_exts(files)
    else:
        rec.setdefault("_fz", b"")
        rec.setdefault("_fs", array("Q"))
        rec.setdefault("file_count", 0)
        rec.setdefault("category", "Other")
        rec.setdefault("exts", [])
    rec["category"] = LEGACY_CATEGORIES.get(rec["category"], rec["category"])  # Spanish name (<= 2.9) -> English
    rec.setdefault("hh", [])
    rec.setdefault("health_src", "legacy")          # old measurements: unverified estimate
    rec.setdefault("health_at", rec.get("indexed_at", 0))
    rec.setdefault("seeders", 0)
    rec.setdefault("peers", 0)
    rec.setdefault("created", 0)
    rec.setdefault("src", "?")
    rec.setdefault("indexed_at", rec.get("first_seen", 0))
    rec = compact_record(rec)
    rec["_sb"] = size_bucket(rec.get("size", 0))
    return rec


# Health states (see health_state) and how often a torrent in each state is re-checked
STATES = ("alive", "weak", "quiet", "dead", "unknown")
REFRESH_INTERVAL = {"alive": 6 * 3600, "weak": 12 * 3600, "quiet": 6 * 3600, "dead": 3 * 86400}
UNKNOWN_BASE, UNKNOWN_MAX = 1800, 3 * 86400        # "not measured": 30 min, doubled on each failed attempt, max 3 days
DEAD_MIN_MEASURES = 3                              # consecutive verified measurements at zero…
DEAD_MIN_SPAN = 12 * 3600                          # …spread over at least this long
DEAD_MIN_TRACKERS = 2                              # …each confirmed by at least 2 different trackers


def health_state(rec):
    """Verdict from the available evidence:
      alive   there are seeders (reported by a tracker or seen when connecting)
      weak    no seeders but there are peers
      quiet   0 seeders and 0 peers according to trackers, but not confirmed enough yet
      dead    0 seeders and 0 peers in ≥ 3 measurements verified by ≥ 2 trackers each, spread over ≥ 12 h
      unknown never measured, or only "zero" with no tracker answering (that is NOT evidence of anything)
    A tracker that does not know the torrent answers 0 even if the swarm is alive via DHT: hence "dead" needs repeated confirmation."""
    src = rec.get("health_src")
    if src in (None, "none"):
        return "unknown"
    if rec.get("seeders", 0) > 0:
        return "alive"
    if rec.get("peers", 0) > 0:
        return "weak"
    if src != "scrape":
        return "unknown"
    streak = []
    for e in hh_reversed(rec.get("hh")):
        if e[3] == "s" and e[1] == 0 and e[2] == 0 and (e[4] if len(e) > 4 else 1) >= DEAD_MIN_TRACKERS:
            streak.append(e)
        else:
            break
    if len(streak) >= DEAD_MIN_MEASURES and streak[0][0] - streak[-1][0] >= DEAD_MIN_SPAN:
        return "dead"
    return "quiet"


def refresh_due_at(rec):
    """Time (epoch) from which this torrent should be measured again."""
    st = health_state(rec)
    if st == "unknown":
        k = 0
        for e in hh_reversed(rec.get("hh")):
            if e[3] == "s":
                break
            k += 1
        iv = min(UNKNOWN_BASE * (2 ** min(k, 8)), UNKNOWN_MAX)
    else:
        iv = REFRESH_INTERVAL[st]
    return max(rec.get("health_at", 0), rec.get("checked_at", 0)) + iv


def apply_health(rec, seeders, peers, at, src, nrep=1, detail=None):
    """Applies a health measurement (nrep = number of trackers that answered the scrape; detail = per-source breakdown).
    A measurement WITHOUT scrape that is worse than a recent verified one does not overwrite the figures, but:
      - health_at stays WHEN those figures were measured (it used to be set to "now", so days-old data looked fresh)
      - checked_at = last attempt (used by the refresh queue)
      - hd (breakdown) IS updated: it is the most recent evidence, and the web shows both."""
    seeders, peers = max(int(seeders), 0), max(int(peers), 0)
    if detail:
        rec["hd"] = pack_hd(detail)
        if detail.get("cs"):
            rec["seed_ok_at"] = at                  # last time we actually CONNECTED to a real seeder
    rec["checked_at"] = at
    if (src != "scrape" and rec.get("health_src") == "scrape" and at - rec.get("health_at", 0) < 14 * 86400
            and seeders < rec.get("seeders", 0)):
        return                                      # keep the verified value (with its real date)
    rec["seeders"], rec["peers"], rec["health_at"], rec["health_src"] = seeders, peers, at, src
    entry = [at, seeders, peers, SRC_CODE.get(src, "w"), int(nrep)]
    if detail:
        entry.append(int(detail.get("cs", 0)))      # 6th field: seeders confirmed by connecting
    # same timestamp (e.g. when replaying the journal): the last one wins; the last HH_MAX are kept
    rec["hh"] = hh_append(rec.get("hh"), entry, HH_MAX)
