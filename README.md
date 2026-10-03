# DHT Search

BitTorrent DHT crawler + web search engine + persistent analytics. Data is stored as JSON (JSON Lines).

## Getting started

```bash
pip install -r requirements.txt        # flask + libtorrent (>= 2.0.9)
python app.py --selftest               # checks the libtorrent API and alert handling on your Python version
python app.py                          # http://127.0.0.1:8080
python app.py --demo                   # synthetic data, no network and no libtorrent (to preview the web UI)
```

Options: `--dht-port 6881` (open UDP on the router; TCP is optional), `--max-probes 150` (simultaneous metadata downloads: the bottleneck),
`--probe-timeout 90`, `--sample-qps 5` (BEP 51 queries/s), `--queue-max 20000`, `--refresh-share 0.15` (share of probes reserved for
refreshing seeders/peers of what is already indexed), `--no-trackers`, `--no-refresh`, `--host 0.0.0.0` (local only by default), `--data-dir PATH`.
`python app.py --help` lists them all.

The running version is shown in the web header, in the analytics tab and at `/api/version` (with the changelog, see `version.py`).

## Upgrading

```bash
systemctl stop torrent-search
cp -a /opt/torrentcrawler/data /opt/torrentcrawler/data.bak        # backup (recommended)
# copy the new files over the project, then:
systemctl start torrent-search && journalctl -u torrent-search -f
```

**Since 3.0 the data files are in English too.** Versions up to 2.9 stored category names in Spanish (`Vídeo`, `Imágenes`,
`Documentos`, `Comprimidos`, `Datos`, `Otros`). On the first start of 3.x the journal is rewritten once with the English names
(the log says `N torrents had Spanish category names (<= 2.9); journal rewritten in English`), and the old Spanish comment header of
`blocklist.txt` is replaced (your patterns are kept). The rewrite needs free disk space for a second copy of `torrents.jsonl` while it runs.
Old links such as `?cat=Vídeo` and Spanish query aliases (`cat:imagenes`, `cat:comprimidos`…) keep working.

**Rolling back to 2.x** after that: stop the service and run `python migrate_es.py --to-legacy --apply` (category names back to Spanish).
Starting 3.x again converts them back automatically.

Very old installs with a single `torrents.json` are migrated automatically on first start to `torrents.jsonl` (≈ 25 s and ≈ 800 MB peak
RAM with 38,000 torrents / 2 M files); `stats.json` (counters, 24 h history) is kept. The original is left as `torrents.json.migrated`
(delete it whenever you like; it lets you go back).

## Importing Spanish-format data (`migrate_es.py`)

To bring in data from another or older installation (≤ 2.9, Spanish format) at any time, put the files in `data/migrate_esp/`
(any subset of `torrents.jsonl` or the very old `torrents.json`, `hashes.json`, `peers.jsonl`, `hidden_rules.json`, `blocklist.txt`,
`nodes.json`) and run:

```bash
python migrate_es.py                     # dry run: shows what would be imported, writes nothing
systemctl stop torrent-search
python migrate_es.py --apply             # import
systemctl start torrent-search
```

| What | How |
|---|---|
| torrents | only those missing from `data/` are appended, with their health history and the categories translated; torrents already present are left as they are (the destination wins); those matching the blocklist are skipped |
| hashes | pending/failed hashes not known yet |
| peers | only for the torrents imported in that run, skipping those older than `--peer-ttl-days` (30); `--no-peers` skips them |
| hide rules, blocklist, nodes | merged without duplicates |
| not imported | `stats.json` / `history_*` (they would double count) and `trackers.txt` (it replaces the default trackers, and each extra tracker adds one scrape per probe: more network load) |

Safety: dry run by default; with `--apply` it refuses to run while a process has `data/torrents.jsonl` open; `data/migrate_esp/` is only
read; journals are only appended to and every other file is backed up to `data/migrate-backup-<date>/`, with an `undo.sh` that is valid
until the service is started again. Running it twice imports nothing twice. Run as root, it keeps the owner of `data/` on the files it writes.
Other directories: `--src DIR --dst DIR`.

## What is in `data/`

| File | Contents |
|---|---|
| `torrents.jsonl` | Append-only JSON Lines journal: one event per line (`n` new torrent, `h` health measurement, `d` deletion). Writes are O(1); it compacts itself in the background. |
| `hashes.json` | Pending / retrying / failed infohashes |
| `stats.json` | Cumulative counters, lifetime totals (uptime, sessions, traffic) and cardinalities (HyperLogLog) |
| `history_raw.json`, `history_m5.jsonl`, `history_h1.jsonl` | History at 3 resolutions: 10 s · 24 h, 5 min · 30 days, 1 h · 2 years |
| `nodes.json` | DHT nodes seen (they seed sampling on startup) |
| `blocklist.txt` | Regular expressions (one per line) matched against name and paths; applied when indexing **and on startup to what is already indexed** |
| `hidden_rules.json` | Admin hide rules |
| `peers.jsonl` | Peers (IP:port) seen per torrent — personal data, see below |
| `admin_secret.key` | Session signing key — never share or commit it |
| `migrate_esp/`, `migrate-backup-*/` | Data to import and import backups (see above) |

**Do not commit `data/` to a repository.**

## Search

Front page with the most popular torrents, advanced options (category, extension, size, seeders, number of files, indexing date, health,
where to search, sort order, results per page), facets with counts, active filters as chips, autocomplete, "Did you mean", highlighted
matches (case- and accent-insensitive) and the matching **files** of each result. Everything lives in the URL: it can be shared and the
Back button works.

Syntax (also under the "Search syntax" button on the site):

| Example | Meaning |
|---|---|
| `ubuntu desktop` | all words (the last one also as a prefix) |
| `"the matrix"` · `-"the matrix"` | exact phrase / exclude it |
| `debian OR ubuntu` · `debian \| ubuntu` | alternatives |
| `linux -server` | exclude a word |
| `ext:mkv,mp4` · `-ext:srt` | files with those extensions |
| `cat:video` | video, audio, images, docs, software, archives, data, other |
| `size>1gb` · `size<700mb` · `size:1gb..4gb` | total size |
| `seeders>=10` · `peers>5` · `files>100` · `files:10..50` | |
| `age<7d` · `age>30d` · `indexed:24h` | indexed less / more than … ago (`h d w mo y`) |
| `in:name` · `in:files` · `name:x` · `file:x` | where to search |
| `health:verified` · `alive:yes` · `health:dead` | verified health / with seeders / verified without seeders |
| `hash:6f3a9c` | by infohash (≥ 6 characters) |

Ranking weighs name > files, rarity of each word (IDF), density, exact phrase and, slightly, **verified** seeders.

## Admin panel (Ctrl+Alt+A)

On any page press **Ctrl+Alt+A** (Mac: ⌃⌥A) to open a password prompt. With the right password the **Admin** tab appears.
The password is checked **on the server** and yields a signed session cookie (HttpOnly, SameSite=Strict, 12 h); without it `/api/admin/*` returns 401.
5 failed attempts per IP within 5 min → temporary lockout. Changing the password closes open sessions.

```bash
TS_ADMIN_PASSWORD='something-long' python app.py     # preferred: not visible in `ps` or in the unit file
python app.py --admin-password 'something-long'      # works, but visible to any local user
```
**The default is `admin`** and the log warns about it on every start. Behind a reverse proxy, serve the site over HTTPS (otherwise the
password travels in clear text), and bind the app to `--host 127.0.0.1`.

**Hiding searches.** A rule = term + scope (name / files / both) + mode (whole word · substring · regular expression), case- and accent-insensitive.
Use "Preview" before applying it. Matches are **not deleted** (unlike `blocklist.txt`): they stay indexed and measured, but disappear from
search, the front page, autocomplete, "Did you mean", "Similar torrents", the analytics top list and their public page (404).
Rules also apply to what is indexed later. They can be paused or removed and the content comes back. The panel lists hidden torrents with
**why** (which rule, and whether it matched the name or which files, highlighted).

**Peers and seeders.** While probing each torrent the crawler records the IPs of its swarm: connected ones (with port, client and whether it
is a **seeder** or leecher), those returned by the DHT and those announcing the torrent (unknown role). For hidden torrents the panel shows
each IP with how many hidden torrents it is in and **how many others (not hidden)**. The **peer search** (IPs or CIDR networks, "Only where
they act as seeder", "Also include hidden torrents") tells you which other torrents they appear in; clicking any IP searches for it.
"Probe peers now" makes the crawler probe those torrents right away.

Limits worth knowing:
- Each probe is a ~20 s snapshot of the swarm, not continuous monitoring; how many seeders are seen depends on how many accept the connection.
- **An IP is not a person**: CGNAT, VPNs, seedboxes, trackers returning fake peers and DHT poisoning.
- **An IP address is personal data (GDPR).** Retention `--peer-ttl-days 30`, memory cap `--peer-max 500000` (≈ 250 B each), `--no-peers` disables it.

## Torrent page (`/torrent/<infohash>`)

A standalone, shareable page: current health **and its evolution** over time, technical details (infohash, pieces, creator, comment,
trackers, how it was discovered), composition by file type, a **browsable folder tree** (text and extension filter, sorting, list mode,
expand/collapse) and similar torrents. "Back" keeps the search and its filters.

## Probe order: decided by measured success

The bottleneck is not discovering hashes (dozens arrive per second) but fetching their metadata. One LIFO queue per source:
`peer` (announce_peer with IP:port; the crawler connects to it directly), `prio` (seen ≥ 2 times, retries), `getpeers` (someone is
looking for it right now) and `other` (BEP 51, seen once). **Which one gets probed is decided by each queue's recent success** (Thompson
sampling with decay over ~4,000 probes): the best performer gets most probes, the others keep being explored and win probes back if they
improve. (An earlier version gave `peer` fixed priority assuming "whoever announces it has it"; in production it had a 2.9 % success rate
and starved the rest: −23 % torrents/day.)

`--queue-max` (20,000) caps EACH queue: up to 80,000 pending hashes in total. `hashes.json` is saved every 5 min with failed and retrying
hashes only (pending ones expire within minutes); a full save happens when the service stops.

**BEP 51 on demand.** Sampling (`dht_sample_infohashes`) only asks for more hashes while the `other` queue holds fewer than 2,000
(`--bep51-buffer`, 0 = always): anything that will not be probed soon would be dropped anyway. Each node is queried again when it says so
(`interval`, up to 6 h) with 0–30 % jitter so they do not all come back at once; a node that does not answer is retried after 2, 4 and
8 min and then forgotten, making room (cap 50,000) for new nodes. In `live`: `bep51_paused`, `bep51_buffer`, `bep51_nodes_due`;
in `counters`: `bep51_paused_ticks`, `bep51_nodes_evicted`.

**Not announcing in the DHT.** Probes used to be added with the DHT enabled, so libtorrent announced our IP: the DHT keeps it ~30 min while
the probe lasts 45 s, so clients in that swarm kept connecting to a torrent we no longer had (production: ~364 incoming connections/s).
Now the torrent is added with the DHT disabled, peers are looked up with `dht_get_peers` (which does not announce) and handed to the probe
(up to 60). `--dht-announce` restores the old behaviour.

To measure, in `/api/stats` → `counters`: `probes_X` / `metadata_ok_X` per queue, `meta_t_lt10 … meta_t_ge90` (seconds until metadata
arrived: tells you whether `--probe-timeout` cuts off successes), `dht_lookups`, `dht_connects`, `direct_connects`; in `live`, `incoming_connections`.

### libtorrent connection settings (for fetching metadata)

**By default the network load is unchanged.** Only two neutral settings are applied: `peer_connect_timeout` 7 s (was 15: same attempts per
second, fewer half-open connections at once) and `max_metadata_size` 32 MB (was 3 MB: torrents with tens of thousands of files could never
be fetched and were retried). The startup log says which ones your version supports.

Knobs that DO increase the load — use them deliberately:
- `--connection-speed N` (default 30, libtorrent's): connection attempts per second for the whole session. With 150 probes that is ~9 peers
  tried per torrent in 45 s; it would give the most success, and adds the most connections (and conntrack entries).
- `--nopeer-giveup N` (0 = off): gives up after N s on a probe with no known peer; frees slots = more probes per hour = more DHT lookups
  and more scrapes.
- Connecting directly to whoever announces (`connect_peer`) adds at most ~1 connection per announce with IP:port (~2/s in production).

In `/api/stats` → `live`: `incoming_connections` (if it stays at 0, the port is not open on the router), `connection_attempts` and
`connect_timeouts`; in `counters`: `probe_nopeers`, `direct_connects`.

## Memory

Measured with a synthetic catalogue shaped like the production one (70,000 torrents, 3.6 M files, 12 health measurements with a 20-tracker
breakdown, 300,000 peers), Python store only, without libtorrent:

| | Before | Now |
|---|---|---|
| RSS after loading | ~2.0 GB | ~0.65 GB |
| File index | 620 MB | 150 MB |
| Per-tracker breakdown (`hd`) | 460 MB | 38 MB |
| Health history (`hh`, 12 measurements; grows to 40) | 170 MB (→ ~570 MB) | 17 MB (→ ~50 MB) |
| Records (repeated keys) | 165 MB | 81 MB |
| Peers | 230 MB | 130 MB |

What changed (all in memory, `packing.py`): history and breakdown stored as `bytes` with `struct`; tracker URLs, errors and clients in
interned tables; repeated keys and strings interned; inverted indexes store the infohash as-is when there is only one, a `tuple` when there
are few and a `list` when there are many (instead of one `set` per token). **Files on disk are byte-identical** and there are no new reads
or writes. Search is the same or slightly faster; startup ~10 % slower (packing while replaying the journal).
Also: `malloc_trim` after loading and after each compaction, and `MALLOC_ARENA_MAX=2` in the service. The startup log shows the RSS.

## Are the seeders real? Who reports what and how to verify it

**What a tracker says is not the same as what is out there.** A scrape returns the peers that *announced to it* in the last ~hour:
reachable or not (NAT without a forwarded port, uTP/encryption only), stale or even invented. That is why each measurement stores a
**per-source breakdown** (`hd` in the journal):

| Source | What it is | Verified? |
|---|---|---|
| each tracker | seeders / leechers from its scrape, addresses returned on announce, or the error | no: it is what the tracker claims |
| direct connection | seeders and leechers that **accepted the crawler's connection** and sent their bitfield | **yes** |
| DHT | addresses returned by `get_peers` | no |

It is shown in the result list ("Sources" line), on each torrent page ("Who reports what", with "Confirmed seeders" and when one was last
connected) and in the admin panel ("Trackers: do they really answer?", response rate and last error of each tracker).

**Independent verification** (uses neither libtorrent nor the crawler): scrape and announce to each tracker, DHT lookup and a BitTorrent
handshake with each address to see whether it has the complete torrent.
```bash
python verify.py <infohash|magnet>            # table: claims S/L · returns · answer · seeders OK, plus a verdict
python verify.py <infohash> --json            # full report
```
Also available on the torrent page with an admin session: **"Verify live now"**. Limitation: plain TCP only, so a peer that only accepts
uTP or encryption shows up as "no response" even if it exists. It announces as a leecher and sends `stopped` right away so it does not
inflate the swarm.

Fixes behind the "fake-looking" figures:
- The alert mask explicitly includes `tracker_notification` (needed for `scrape_reply_alert`); `python app.py --selftest` checks it.
- "Estimated" figures used to come from `list_seeds`/`list_peers` (PEX/DHT candidates, never connected). Now only **connected** peers count.
- **The magnet had no trackers** (the ones in the `.torrent` almost never arrive via DHT): your client relied on the DHT alone while seeders
  were counted on trackers. Now it carries the trackers that answered first (the one with most seeders at the front).
- A measurement with no tracker response kept the previous figure **but dated it "now"**. Now `health_at` = when the figures were measured
  and `checked_at` = last attempt.

## Health: what is measured and how "alive / dead" is decided

For each torrent the crawler requests a **scrape from every configured tracker** (`--tracker URL`, repeatable, or `data/trackers.txt` with
one URL per line; by default the 20 public ones in `trackers.py`) as soon as it is added, and also looks at how many peers it sees when
connecting (about 18 s at most; it ends earlier if every tracker answers). Each tracker only sees *its* part of the swarm, so the maximum is
taken. The source and the number of trackers that answered are stored. From that a **state** is decided:

| State | Criterion | Rechecked |
|---|---|---|
| **Alive** | there are seeders (seen by a tracker or by connecting) | every 6 h |
| **Weak** | no seeders but some peers | every 12 h |
| **Quiet** | 0 seeders and 0 peers according to trackers, **unconfirmed** | every 6 h |
| **Dead (confirmed)** | 0/0 in ≥ 3 measurements verified by ≥ 2 trackers each, spread ≥ 12 h apart | every 3 days (in case it revives) |
| **Unknown** | never measured, or no tracker answered | 30 min, with exponential backoff up to 3 days |

*A tracker that does not know the torrent answers "0" even if the swarm is alive on the DHT*, which is why "dead" requires repeated
confirmation. More trackers = a more reliable verdict. Filter: `health:alive|weak|quiet|dead|unknown|verified`. Each torrent keeps its last
40 measurements (chart on its page).

### Creation date, comment and original trackers
**These cannot be obtained over the DHT**: BEP 9 only transfers the `info` dictionary; `creation date`, `comment`, `created by` and
`announce` live in the full `.torrent`. The crawler reads them if they arrive, but they are usually empty, and the site does not show what
does not exist (the reliable date is when it was indexed). To check your own data:
```bash
python3 -c "import json;n=c=0
for l in open('/opt/torrentcrawler/data/torrents.jsonl'):
    o=json.loads(l)
    if o.get('t')=='n': n+=1; c+=bool(o['r'].get('created'))
print(n,'torrents;',c,'with a creation date')"
```

## Measurement accuracy

- **Verified seeders/peers** (`✓` = at least one tracker answered; `≥ n` = only peers seen when connecting (lower bound); `?` = not measured).
  A worse measurement without a scrape **does not overwrite** a recent verified one. Only verified measurements count as "no seeders".
- **Per-torrent health history** (last 40 measurements).
- **Guaranteed refresh**: with the pending queue always full, already indexed torrents were never refreshed; they now have a reserved share (`--refresh-share`).
- **Deferred retries** (5 min) that actually run; previously, with the queue full, the retry was dropped immediately.
- Zero is no longer confused with "not measured", and rates/success are computed from persistent cumulative counters.

## Persistent analytics

They survive restarts and deployments: cumulative counters, **total uptime and number of sessions**, **cumulative traffic** (↓/↑),
**unique nodes and peers** seen (HyperLogLog ±1 %) and a history with ranges from 15 min to **1 year**. Total/rate (per hour, per day on
long ranges), Y axis "from 0 / fitted", infohash flow, metadata success, BEP 51, catalogue health, seeder distribution, age, top torrents
and an "All-time summary". Configurable auto refresh (5 s–1 min), updated in place without scroll jumps.

## Pending queue

BEP 51 sampling discovers thousands of infohashes per minute, far more than can be resolved. The queue is bounded (`--queue-max`) and
processed **newest first**; when full, the oldest are dropped. Sample hashes seen once get 1 attempt; those received via `announce` /
`get_peers` or seen repeatedly move to the front and get up to 3 (spaced 5 min apart).

## Diagnostics

```bash
journalctl -u torrent-search -f | grep -E "store loaded|alert mask|first alert|summary|external IP|dht_sample|maintenance|migrated"
```
Every minute: alerts by type, known nodes and BEP 51 queries/replies. Private IPs and our own external IP are excluded from queries
(the latter is also added to libtorrent's `ip_filter`).

Since 2.9 log lines and API error messages are in English: update any alert or script that matched the old Spanish text.

## systemd service

See `deploy/torrent-search.service` (or the minimal unit of your install: `WorkingDirectory=/opt/torrentcrawler`, `ExecStart=… .venv/bin/python app.py`).
`SIGTERM` is handled as a normal exit: everything is flushed before stopping.

## Tests (no network)

```bash
python tests/test_stats.py             # HyperLogLog, history tiers, stats.json migration
python tests/test_search.py            # search engine, journal, compaction, health, on-disk category compatibility
python tests/test_crawler_alerts.py    # alerts, queues, retries, refresh, scrape (simulated libtorrent)
python tests/test_admin.py             # hide rules, peers, admin API (login, public 404, peer search)
python tests/test_packing.py           # compact in-memory representations
python tests/test_verify.py            # verify.py verdicts
python tests/test_migrate_es.py        # importing Spanish-format data, undo, --to-legacy
N=20000 python tests/test_migration_perf.py   # migration from the old format and performance
```

## Limits and notes

- It cannot index "all" torrents: the DHT is not enumerable; you see the part your queries reach, and it grows over the days.
- The whole catalogue lives in RAM (see Memory). Reference: 100,000 torrents → 30–40 s startup.
- A DHT crawler indexes whatever circulates, **including copyrighted or illegal material**. You are responsible for how it is used: fill in
  `data/blocklist.txt` before exposing it, and do not publish it on the Internet without authentication and a reverse proxy (it runs
  Flask's development server).
- Torrent names are untrusted text; the site always renders them with `textContent` (never `innerHTML`).
