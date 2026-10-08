"""Health rules shared by the store (vectorized over columns) and by code that handles one torrent as a dict
(admin views, tests): states, refresh frequency, and how a measurement is applied."""
from packing import hh_append, hh_reversed

HH_MAX = 40                      # health-history entries kept per torrent (health.bin, API, chart)
SRC_CODE = {"scrape": "s", "swarm": "w", "legacy": "l", "none": "n"}      # "scrape" and "swarm" share a first letter: never use src[:1]!


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
      - the breakdown (stored by the caller: its journal offset) IS updated: it is the most recent evidence, and the web shows both."""
    seeders, peers = max(int(seeders), 0), max(int(peers), 0)
    keep = (src != "scrape" and rec.get("health_src") == "scrape" and at - rec.get("health_at", 0) < 14 * 86400
            and seeders < rec.get("seeders", 0))   # keep the verified value (with its real date)
    if detail and detail.get("cs"):
        rec["seed_ok_at"] = at                       # last time we actually CONNECTED to a real seeder
    rec["checked_at"] = at
    if keep:
        return
    rec["seeders"], rec["peers"], rec["health_at"], rec["health_src"] = seeders, peers, at, src
    entry = [at, seeders, peers, SRC_CODE.get(src, "w"), int(nrep)]
    if detail:
        entry.append(int(detail.get("cs", 0)))      # 6th field: seeders confirmed by connecting
    # same timestamp (e.g. when replaying the journal): the last one wins; the last HH_MAX are kept
    rec["hh"] = hh_append(rec.get("hh"), entry, HH_MAX)
