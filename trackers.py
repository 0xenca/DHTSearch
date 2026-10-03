"""Default public trackers (health scrape, magnet links and verify.py).

Replace them with --tracker URL (repeatable) or with data/trackers.txt (one URL per line, "#" = comment).
More trackers give a more reliable verdict, but each probe sends a scrape to EVERY one: with a high --max-probes some
public trackers rate-limit or block IPs that query a lot (see the "Trackers" table in the admin panel).
"""
import os

DEFAULT_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.skynetcloud.site:6969/announce",
    "udp://tracker.qu.ax:6969/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.gmi.gd:6969/announce",
    "udp://tracker.nyaa.vc:6969/announce",
    "udp://explodie.org:6969/announce",
    "udp://tracker.corpscorp.online:80/announce",
    "udp://tracker.bittor.pw:1337/announce",
    "udp://tracker-udp.gbitt.info:80/announce",
    "udp://tracker.ducks.party:1984/announce",
    "http://tracker2.dler.org:80/announce",
    "http://tracker.dler.org:6969/announce",
    "http://tracker.dler.com:6969/announce",
    "http://tracker.renfei.net:8080/announce",
    "udp://retracker01-msk-virt.corbina.net:80/announce",
    "udp://tracker.peerfect.org:6969/announce",
    "udp://tracker.opentrackr.com:6969/announce",
]


def load_trackers_file(path):
    """List from data/trackers.txt, or None if it does not exist or is empty."""
    try:
        with open(path, encoding="utf-8") as f:
            urls = [ln.split("#", 1)[0].strip() for ln in f]
    except FileNotFoundError:
        return None
    urls = [u for u in dict.fromkeys(urls) if u.startswith(("udp://", "http://", "https://"))]
    return urls or None


if __name__ == "__main__":
    print("\n".join(DEFAULT_TRACKERS))
