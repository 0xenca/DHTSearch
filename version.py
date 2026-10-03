"""DHT Search version. Bumped on EVERY change that gets deployed (MAJOR.MINOR.PATCH):
   MINOR = feature or behaviour change · PATCH = fixes · MAJOR = incompatible data format.
The web shows it in the header and in Analytics; /api/version and /api/stats return it."""

__version__ = "3.0.0"

# (version, date, summary) — newest first
CHANGELOG = [
    ("3.0.0", "2026-10-03", "Data files in English too: category names are written in English, and a journal from <= 2.9 is "
                            "rewritten once at startup. New migrate_es.py imports Spanish-format data from data/migrate_esp. "
                            "Rolling back to 2.x requires converting the data back (migrate_es.py --to-legacy)."),
    ("2.9.0", "2026-10-03", "Web, API, logs, CLI help, code and docs in English. Data files unchanged (byte-identical journal; "
                            "the historical on-disk category names are kept and translated on load/save)."),
    ("2.8.0", "2026-10-03", "On-demand BEP 51: stops asking for samples while 2,000 are waiting (--bep51-buffer); nodes that never "
                            "answer are dropped after 3 attempts; randomized rescheduling (no more long silences followed by floods)."),
    ("2.7.0", "2026-10-01", "Version shown in the web and in /api/stats. hashes.json saved every 5 min without pending hashes (full on shutdown)."),
    ("2.6.0", "2026-10-01", "Adaptive split of probes across queues by measured success; probes no longer announce us in the DHT "
                            "(end of the ghost incoming connections); time-to-metadata histogram."),
    ("2.5.0", "2026-10-01", "Load-neutral libtorrent tuning: peer_connect_timeout 7 s and max_metadata_size 32 MB."),
    ("2.4.0", "2026-10-01", "Per-source queues and direct connection to whoever announces the torrent (announce_peer)."),
    ("2.3.0", "2026-09-30", "Less RAM (~2 GB → ~0.65 GB with 70,000 torrents) with the same files on disk."),
    ("2.2.0", "2026-09-29", "20 default trackers (trackers.py) and data/trackers.txt."),
    ("2.1.0", "2026-09-29", "Per-tracker breakdown, seeders confirmed by connecting, magnet links with trackers, and verify.py."),
    ("2.0.0", "2026-09-28", "Starting point: search engine, analytics, admin panel, peers and multi-tracker health."),
]
VERSION_DATE = CHANGELOG[0][1]
assert CHANGELOG[0][0] == __version__, "the first CHANGELOG entry must be the current version"


def info():
    return {"version": __version__, "date": VERSION_DATE, "changelog": [{"version": v, "date": d, "summary": s} for v, d, s in CHANGELOG]}
