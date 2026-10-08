"""DHT Search version. Bumped on EVERY change that gets deployed (MAJOR.MINOR.PATCH):
   MINOR = feature or behaviour change · PATCH = fixes · MAJOR = incompatible data format.
The web shows it in the header and in Analytics; /api/version and /api/stats return it."""

__version__ = "4.3.0"

# (version, date, summary) — newest first
CHANGELOG = [
    ("4.3.0", "2026-10-04", "AI moderation speaks Ollama's native API (/api/generate, raw prompt, logprobs): real confidence "
                            "percentages with Ollama (0.12+) instead of 0/50/100 %, and no double prompt template. Auto-detected "
                            "(GET /api/version) or chosen in \"Server API\"."),
    ("4.2.4", "2026-10-04", "AI moderation: the wait for each answer is editable in the panel (\"Timeout per request\", 600 s by "
                            "default; it was a hidden 60 s), a timeout says so clearly and is retried after 30 s instead of the "
                            "long network backoff."),
    ("4.2.3", "2026-10-04", "AI moderation: requests (and \"Try it\") went to the SAVED endpoint instead of the one in the form, and "
                            "an old error stayed on screen after changing it. Clear errors (HTTP code, wrong API key, missing "
                            "/v1/completions -> use the 'chat' type), saving the settings wakes the worker at once, and an "
                            "endpoint ending in /v1 is accepted."),
    ("4.2.2", "2026-10-04", "install.sh: says WHY llama.cpp could not be downloaded (HTTP code / error from GitHub) and accepts a "
                            "package downloaded by hand: TS_AI_LLAMA=/path/llama-bNNNN-bin-ubuntu-x64.tar.gz."),
    ("4.2.1", "2026-10-04", "install.sh: llama.cpp is now published as .tar.gz (it was .zip), so the local AI model server was "
                            "not installed; both formats are accepted, with a fallback when the GitHub API is rate-limited."),
    ("4.2.0", "2026-10-04", "AI moderation (Admin): a safety model (Qwen3Guard-Gen-0.6B or Llama Guard 3 through llama.cpp, or any "
                            "OpenAI-compatible endpoint) rates torrent names and first file names as NSFW/harmful and hides those "
                            "above a confidence threshold. Admin list with filters (state, category, confidence, name), show "
                            "again / hide / re-analyse, threshold applied instantly, switch off = nothing hidden by it. "
                            "install.sh can set up the local model server (optional, ~0.7 GB RAM in its own process)."),
    ("4.1.0", "2026-10-04", "install.sh: one-command installer/updater. Asks the settings with defaults (install and data "
                            "directories, listen address — every interface by default —, web port, DHT UDP port, simultaneous "
                            "downloads, admin password), installs packages, virtualenv and libtorrent, writes the systemd unit, "
                            "opens the firewall, runs the self-test and starts the service. --yes for unattended, --uninstall."),
    ("4.0.0", "2026-10-03", "New storage engine for millions of torrents on small servers (data format change: converted once on the "
                            "first start; `tools.py export-v3` rolls back). Immutable compressed metadata log, hot fields as columns "
                            "(~100 B per torrent in RAM), fixed slots for health history, a daily checkpoint + health WAL instead of "
                            "a journal that is re-read and compacted, and a log-structured on-disk index (sequential segments, "
                            "tiered merges) replacing the SQLite file index, which wrote ~2 MB per indexed torrent. Startup in "
                            "seconds, vectorized search, filters, facets, analytics and refresh scheduling."),
    ("3.1.2", "2026-10-03", "Fix: a full pass over an uncompacted journal (hide rules with substring/regex at startup, compaction, "
                            "big phrase searches) held the parsed record of almost every torrent in RAM until it reached its "
                            "latest breakdown line: GBs of peak memory that stayed allocated. Now memory stays flat."),
    ("3.1.1", "2026-10-03", "Memory shown in Analytics and /api/stats, split into program memory and reclaimable file cache; the page "
                            "cache of whole-file passes (startup, compaction, index build, big scans) is released afterwards, so "
                            "systemd/Proxmox no longer report the journal as used RAM."),
    ("3.1.0", "2026-10-03", "Low memory: ~1.5 KB per torrent instead of ~6.5 KB (624 -> 195 MB with 70,000 torrents). File lists, "
                            "per-tracker breakdown and older history are read from the journal on demand; the file-name index lives "
                            "on disk (files.idx) and now covers ALL files; packed peers and hash states. Indexing is as fast or "
                            "faster; searches are 1.3-2.5x slower."),
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
