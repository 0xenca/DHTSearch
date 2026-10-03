#!/usr/bin/env python3
"""Torrent search engine on top of the DHT + analytics. See README.md."""
import argparse
import atexit
import logging
import os
import re
import signal
import sys
import threading
import time

from flask import Flask, abort, jsonify, request, send_from_directory

import admin
from version import VERSION_DATE, __version__, info as version_info
from store import Store

ROOT = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(ROOT, "static"), static_url_path="/static")
STATE = {"store": None, "crawler": None, "mode": "live", "started": time.time()}
_HASH_RE = re.compile(r"^[0-9a-fA-F]{40}$")


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/torrent/<ih>")
def torrent_page(ih):
    """Dedicated page for a torrent (the page itself fetches its data from /api/torrent/<ih>)."""
    if not _HASH_RE.match(ih):
        abort(404)
    return send_from_directory(app.static_folder, "torrent.html")


@app.get("/api/search")
def api_search():
    return jsonify(STATE["store"].search(request.args.to_dict()))


@app.get("/api/suggest")
def api_suggest():
    return jsonify(STATE["store"].suggest(request.args.get("q", "")))


@app.get("/api/torrent/<ih>")
def api_torrent(ih):
    if not _HASH_RE.match(ih):
        return jsonify({"error": "invalid infohash"}), 400
    rec = STATE["store"].get(ih)
    if not rec:
        return jsonify({"error": "not found"}), 404
    hidden_by = rec.pop("hidden_by", [])
    if admin.is_admin():                            # the administrator also sees why it is hidden, and its peers
        rec["hidden_by"] = hidden_by
        rec["peers_known"] = STATE["store"].peers.peers_of(rec["ih"], limit=300)
    elif hidden_by:                                 # hidden by the panel: for the public it does not exist
        return jsonify({"error": "not found"}), 404
    return jsonify(rec)


@app.get("/api/torrent/<ih>/related")
def api_related(ih):
    if not _HASH_RE.match(ih):
        return jsonify([]), 400
    if ih.lower() in STATE["store"].hidden and not admin.is_admin():
        return jsonify([]), 404
    return jsonify(STATE["store"].related(ih))


@app.get("/api/stats")
def api_stats():
    c = STATE["crawler"]
    return jsonify({
        "version": __version__, "version_date": VERSION_DATE,
        "mode": STATE["mode"],
        "uptime": int(time.time() - STATE["started"]),
        "live": c.snapshot() if c else {},
        "analytics": STATE["store"].analytics(),
    })


@app.get("/api/version")
def api_version():
    return jsonify(version_info())


@app.get("/api/history")
def api_history():
    try:
        secs = int(request.args.get("seconds", 3600))
    except ValueError:
        secs = 3600
    return jsonify(STATE["store"].get_history(secs))


@app.get("/api/categories")
def api_categories():
    from textutil import ALL_CATEGORIES
    return jsonify(ALL_CATEGORIES)


@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "not found"}), 404
    return e


def _rss_mb():
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 0


def trim_memory():
    """Returns free heap memory to the system (glibc does not do it by itself after peaks such as loading the journal
    or a compaction). Does not touch disk or data."""
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


def _flusher(store, stop):
    ticks = 0
    while not stop.wait(15):
        ticks += 1
        try:
            store.flush()
            if ticks % 40 == 0:                     # every ~10 min
                res = store.maintenance()
                if res["failed_purged"] or res["compacted"] or res.get("peers_expired") or res.get("peers_capped"):
                    trim_memory()
                    logging.info("maintenance: %s (RSS %d MB)", res, _rss_mb())
            if ticks % 5760 == 0:                   # every ~24 h
                store.stats.compact_tiers()
        except Exception:
            logging.exception("error saving data")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="127.0.0.1", help="local only by default; 0.0.0.0 to expose it on the network")
    p.add_argument("--port", type=int, default=8080, help="web port")
    p.add_argument("--dht-port", type=int, default=6881, help="libtorrent listen port (TCP/UDP)")
    p.add_argument("--data-dir", default=None)
    p.add_argument("--max-probes", type=int, default=150, help="simultaneous metadata downloads (the bottleneck)")
    p.add_argument("--probe-timeout", type=int, default=90, help="seconds waiting for metadata before giving up")
    p.add_argument("--connection-speed", type=int, default=30, help="peer connection attempts per second, in total (30 = libtorrent; higher = more success and more connections)")
    p.add_argument("--nopeer-giveup", type=int, default=0, help="give up after N s on a probe with no known peer at all (0 = never; enabling it = more probes per hour = more load)")
    p.add_argument("--dht-announce", action="store_true", help="announce ourselves in the DHT when probing (old behaviour: causes hundreds of ghost incoming connections per second)")
    p.add_argument("--bep51-buffer", type=int, default=2000, help="stop requesting BEP 51 samples while at least N sampled hashes are waiting (0 = always request)")
    p.add_argument("--sample-qps", type=float, default=5, help="BEP 51 queries per second (discovery)")
    p.add_argument("--queue-max", type=int, default=20000, help="max pending hashes per queue (4 queues); when full the oldest are dropped")
    p.add_argument("--refresh-share", type=float, default=0.15, help="share of probes reserved for refreshing seeders/peers of what is already indexed")
    p.add_argument("--tracker", action="append", metavar="URL", help="tracker for the seeders/peers scrape (repeatable; replaces trackers.py and data/trackers.txt). More trackers = more reliable health verdict")
    p.add_argument("--no-trackers", action="store_true", help="do not contact trackers (DHT only)")
    p.add_argument("--no-refresh", action="store_true", help="do not refresh seeders/peers of already indexed torrents")
    p.add_argument("--admin-password", default=os.environ.get("TS_ADMIN_PASSWORD", "admin"),
                   help="admin panel password (Ctrl+Alt+A on the web). Also TS_ADMIN_PASSWORD. Default \"admin\": change it")
    p.add_argument("--no-peers", action="store_true", help="do not store the peers (IPs) of each torrent")
    p.add_argument("--peer-ttl-days", type=int, default=30, help="days a peer is kept without being seen again")
    p.add_argument("--peer-max", type=int, default=500_000, help="max torrent-peer entries in memory (≈ 250 B each)")
    p.add_argument("--demo", action="store_true", help="synthetic data, no network and no libtorrent")
    p.add_argument("--selftest", action="store_true", help="checks the libtorrent API and exits")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    if args.selftest:
        from crawler import selftest
        raise SystemExit(selftest())

    data_dir = args.data_dir or os.path.join(ROOT, "data-demo" if args.demo else "data")
    t0 = time.time()
    store = Store(data_dir, {"enabled": not args.no_peers, "ttl_days": args.peer_ttl_days, "max_entries": args.peer_max})
    store.queue_max = args.queue_max
    STATE["store"] = store
    admin.init(app, lambda: STATE["store"], args.admin_password, data_dir, lambda: STATE.get("crawler"))
    if args.admin_password == "admin":
        logging.warning("admin panel with the DEFAULT password (\"admin\"). Change it with --admin-password "
                        "or TS_ADMIN_PASSWORD%s", " — and you are listening on " + args.host + "!" if args.host not in ("127.0.0.1", "localhost", "::1") else "")
    trim_memory()
    logging.info("store loaded (RSS %d MB): %d torrents, %d pending/failed hashes in %.1fs (session #%d, total uptime %s)",
                 _rss_mb(), len(store.torrents), len(store.hashes), time.time() - t0, store.stats.life["sessions"],
                 time.strftime("%H:%M:%S", time.gmtime(store.stats.life["uptime_s"])) + f" (+{int(store.stats.life['uptime_s'] // 86400)} d)")

    if args.demo:
        from demo import DemoCrawler
        crawler = DemoCrawler(store)
        STATE["mode"] = "demo"
    else:
        from crawler import Crawler, lt
        from trackers import load_trackers_file
        if lt is None:
            raise SystemExit("libtorrent is missing:  pip install libtorrent   (or use --demo to try the web)")
        crawler = Crawler(store, {"port": args.dht_port, "max_probes": args.max_probes,
                                  "probe_timeout": args.probe_timeout,
                                  "connection_speed": args.connection_speed, "nopeer_giveup": args.nopeer_giveup, "dht_announce": args.dht_announce, "bep51_buffer": args.bep51_buffer, "sample_qps": args.sample_qps,
                                  "refresh_share": args.refresh_share, "trackers": args.tracker or load_trackers_file(os.path.join(data_dir, "trackers.txt")),
                                  "use_trackers": not args.no_trackers, "refresh": not args.no_refresh})
    STATE["crawler"] = crawler
    crawler.start()

    stop = threading.Event()
    threading.Thread(target=_flusher, args=(store, stop), daemon=True).start()

    def shutdown():
        stop.set()
        crawler.stop()
        time.sleep(0.5)
        store.close()
    atexit.register(shutdown)
    # systemctl stop sends SIGTERM: turn it into a normal exit so atexit flushes everything
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    logging.info("DHT Search %s (%s) · web on http://%s:%s   (data in %s, mode %s)", __version__, VERSION_DATE,
                 args.host, args.port, data_dir, STATE["mode"])
    app.run(host=args.host, port=args.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
