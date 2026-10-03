"""
Demo mode: generates SYNTHETIC activity (random infohashes and made-up names based on
free software / public-domain content) to try the web without a network.
It uses the same real Store, so search, JSON and analytics are genuinely exercised.
"""
import hashlib
import math
import os
import random
import threading
import time

from records import apply_health

BASES = [
    ("Debian {v} amd64 netinst", ["debian-{v}-amd64-netinst.iso"], 650),
    ("Ubuntu {v} LTS Desktop", ["ubuntu-{v}-desktop-amd64.iso"], 5800),
    ("Fedora Workstation Live {v}", ["Fedora-Workstation-Live-{v}.iso"], 2200),
    ("Arch Linux {y}.{m}.01 x86_64", ["archlinux-{y}.{m}.01-x86_64.iso"], 1100),
    ("Big Buck Bunny 1080p", ["Big.Buck.Bunny.1080p.mkv", "subs/en.srt", "subs/es.srt"], 700),
    ("Sintel 4K Open Movie", ["Sintel.4K.mkv", "poster.jpg"], 3400),
    ("Tears of Steel {q}", ["Tears.of.Steel.{q}.mp4"], 900),
    ("Wikipedia dump {y}-{m}", ["enwiki-{y}{m}01-pages-articles.xml.bz2"], 21000),
    ("LibreOffice {v} Win x64", ["LibreOffice_{v}_Win_x86-64.msi"], 340),
    ("Blender {v} Linux x64", ["blender-{v}-linux-x64.tar.xz"], 280),
    ("OpenStreetMap planet extract {y}", ["planet-{y}.osm.pbf"], 68000),
    ("Linux Kernel Source {v}", ["linux-{v}.tar.xz"], 140),
]
# synthetic peers: ONLY documentation ranges (RFC 5737), never real IPs. A small group of "regulars"
# shows up in many torrents so that the panel's peer search has matches to show.
PEER_POOL = [f"{net}.{i}" for net in ("192.0.2", "198.51.100", "203.0.113") for i in range(1, 255)]
PEER_REGULARS = PEER_POOL[:25]
CLIENTS = ["qBittorrent 4.6.5", "Transmission 4.0.6", "Deluge 2.1.1", "libtorrent 2.0.10", "µTorrent 3.6", "BiglyBT 3.6"]
SHOWS = ["Open Source Stories", "Public Domain Theatre", "The Linux Chronicles", "Creative Commons Weekly", "Blender Foundation Shorts"]
ARTISTS = ["Free Music Archive Jazz Collection", "Public Domain Classical Piano", "Creative Commons Ambient Sessions"]


def _series(rng):
    show = rng.choice(SHOWS)
    s, eps = rng.randint(1, 8), rng.randint(6, 24)
    q = rng.choice(["720p", "1080p", "2160p"])
    name = f"{show} S{s:02d} {q} Complete"
    root = f"{show} S{s:02d}"
    files = []
    for e in range(1, eps + 1):
        files.append((f"{root}/Season {s}/{show.replace(' ', '.')}.S{s:02d}E{e:02d}.{q}.mkv", int(rng.uniform(0.6, 1.6) * {"720p": 700, "1080p": 1800, "2160p": 5200}[q] * 2 ** 20)))
        files.append((f"{root}/Subs/S{s:02d}E{e:02d}.en.srt", rng.randint(30_000, 90_000)))
    files.append((f"{root}/Extras/making-of.mkv", rng.randint(200, 900) * 2 ** 20))
    files.append((f"{root}/README.txt", 2_000))
    return name, files


def _album(rng):
    artist = rng.choice(ARTISTS)
    y = rng.randint(1998, 2025)
    name = f"{artist} {y} FLAC"
    files = []
    for cd in (1, 2) if rng.random() < 0.4 else (1,):
        for t in range(1, rng.randint(8, 16) + 1):
            files.append((f"{name}/CD{cd}/{t:02d} - Track {t}.flac", rng.randint(20, 45) * 2 ** 20))
    files += [(f"{name}/cover.jpg", 400_000), (f"{name}/{name}.cue", 3_000)]
    return name, files


def _ebooks(rng):
    n = rng.choice([50, 100, 250, 500])
    name = f"Project Gutenberg Top {n} ebooks"
    files = [(f"{name}/{['Fiction', 'Science', 'History', 'Poetry'][i % 4]}/ebook_{i + 1:04d}.epub", rng.randint(200_000, 3_000_000)) for i in range(n)]
    return name, files


def _photos(rng):
    n = rng.choice([40, 120, 300])
    name = f"Creative Commons Photo Pack {n}"
    files = [(f"{name}/{['Nature', 'Cities', 'People'][i % 3]}/img_{i + 1:04d}.jpg", rng.randint(500_000, 6_000_000)) for i in range(n)]
    return name, files


def _fake(rng):
    r = rng.random()
    if r < 0.18:
        return _series(rng)
    if r < 0.30:
        return _album(rng)
    if r < 0.38:
        return _ebooks(rng)
    if r < 0.44:
        return _photos(rng)
    tpl, files, mb = rng.choice(BASES)
    ctx = {"v": f"{rng.randint(1, 24)}.{rng.randint(0, 12)}", "y": rng.randint(2019, 2026), "m": f"{rng.randint(1, 12):02d}",
           "q": rng.choice(["720p", "1080p", "4K"])}
    name = tpl.format(**ctx)
    total = int(mb * 1024 * 1024 * rng.uniform(0.7, 1.3))
    out = []
    for j, f in enumerate(files):
        path = f.format(**ctx)
        out.append((path if len(files) == 1 else f"{name}/{path}", total if j == 0 else rng.randint(5_000, 400_000)))
    return name, out


class DemoCrawler(threading.Thread):
    def __init__(self, store, cfg=None):
        super().__init__(daemon=True, name="demo-crawler")
        self.store = store
        self.stop_evt = threading.Event()
        self.rng = random.Random()
        self.live = {}
        self.pending = []  # (ready_at, ih)
        self.rx_total = self.tx_total = 0
        self.error = None
        self.t0 = time.time()

    def stop(self):
        self.stop_evt.set()

    from trackers import DEFAULT_TRACKERS as DEMO_TRACKERS

    def _detail(self, seeds, peers, src, now):
        """Synthetic per-tracker breakdown: one usually sees almost everything, others a part, some fail; few are connected."""
        rng, tr = self.rng, []
        for i, u in enumerate(self.DEMO_TRACKERS):
            if src != "scrape" or rng.random() < (0.15 if i else 0.03):
                tr.append({"u": u, "e": rng.choice(["timed out", "host not found", "connection refused"])})
                continue
            f = 1.0 if i == 0 else rng.uniform(0, 0.8)
            tr.append({"u": u, "s": int(seeds * f), "l": int(peers * f), "r": min(int((seeds + peers) * f), 200)})
        if src == "scrape" and not any("s" in t and t["s"] == seeds for t in tr):
            tr[0] = {"u": tr[0]["u"], "s": seeds, "l": peers, "r": min(seeds + peers, 200)}
        cs = 0 if not seeds else min(seeds, int(rng.choice([0, 0, 1, 2, 3, 5]) * (1 + seeds / 200)))
        return {"at": int(now), "tr": tr, "cs": cs, "cp": min(peers, rng.randint(0, 6)), "ci": cs + rng.randint(0, 6),
                "dht": rng.randint(0, 80) if seeds + peers else rng.randint(0, 3), "md": int(rng.random() < 0.9)}

    def tracker_report(self):
        return [{"url": u, "configured": True, "scrape_ok": 900 - i * 150, "scrape_fail": 10 + i * 90,
                 "announce_ok": 880 - i * 150, "announce_fail": 15 + i * 80, "last_ok": int(time.time()) - 5,
                 "last_fail": int(time.time()) - 60, "last_error": "timed out" if i else "",
                 "scrape_rate": round((900 - i * 150) / (910 + i * -60), 3)} for i, u in enumerate(self.DEMO_TRACKERS)]

    def _add_torrent(self, ih, now):
        rng, st = self.rng, self.store
        name, files = _fake(rng)
        seeds = int(rng.lognormvariate(2.5, 1.5))
        peers = int(seeds * rng.uniform(0.3, 3))
        dead = rng.random() < 0.08                              # some of the torrents have had no activity for days
        if dead:
            seeds = peers = 0
        st.save_torrent(ih, {
            "name": name, "size": sum(s for _, s in files), "files": files,
            "piece_length": 262144 * rng.choice([1, 2, 4, 8]),
            "created": int(now - rng.randint(0, 86400 * 900)),
            "comment": "synthetic torrent (demo mode)", "created_by": "demo",
            "trackers": ["udp://tracker.opentrackr.org:1337/announce", "udp://open.tracker.cl:1337/announce"],
        })
        rec = st.torrents.get(ih)
        if rec:
            n = 6 if dead else rng.choice([0, 0, 1, 3, 6, 10])   # earlier measurements (for the health chart)
            nrep = 3 if dead else rng.choice([1, 2, 3, 4])
            for k in range(n):
                ts = int(now - (n - k) * 21600 - rng.randint(0, 900))
                s_k = 0 if dead else max(0, int(seeds * rng.uniform(0.5, 1.5)))
                apply_health(rec, s_k, int(s_k * 0.6), ts, "scrape", nrep)
                st.journal.append({"t": "h", "ih": ih, "s": s_k, "p": int(s_k * 0.6), "a": ts, "g": "scrape", "n": nrep})
            src = "scrape" if (dead or rng.random() < 0.8) else "swarm"
            d = self._detail(seeds, peers, src, now)
            if src != "scrape":                  # without a tracker, all there is is what was connected
                seeds, peers = d["cs"], d["cp"]
            st.update_health(ih, max(seeds, d["cs"]), peers, src, nrep if src == "scrape" else 0, detail=d)
            if not dead:
                self._add_peers(ih, now, seeds, peers)
        st.counters["metadata_ok"] += 1

    def _add_peers(self, ih, now, seeds, peers):
        rng = self.rng
        n = min(40, max(1, (seeds + peers) // 3))
        ips = rng.sample(PEER_POOL, n)
        if rng.random() < 0.35:
            ips += rng.sample(PEER_REGULARS, rng.randint(1, 4))
        for ip in set(ips):
            role = rng.choice([1, 1, 0, -1]) if seeds else rng.choice([0, -1])
            self.store.peers.note(ih, ip, rng.randint(1025, 65000) if rng.random() < 0.8 else 6881, role,
                                  rng.choice(CLIENTS) if role != -1 else "", "c" if role != -1 else rng.choice("da"),
                                  now=now - rng.randint(0, 3600))

    def run(self):
        last_pt = 0.0
        while not self.stop_evt.is_set():
            now = time.time()
            wave = 0.6 + 0.4 * math.sin((now - self.t0) / 40)
            for _ in range(self.rng.randint(0, int(4 * wave) + 1)):
                ih = hashlib.sha1(os.urandom(20)).hexdigest()
                src = self.rng.choice(["announce", "get_peers", "bep51", "bep51"])
                if self.store.add_hash(ih, src, prio=src != "bep51"):
                    self.pending.append((now + self.rng.uniform(2, 20), ih))
            for _ in range(20):                                     # "Probe peers now" from the panel
                fih = self.store.next_forced()
                if not fih:
                    break
                r = self.store.torrents.get(fih)
                if r:
                    self._add_peers(fih, now, max(r["seeders"], 1), r["peers"])
                    self.store.update_health(fih, r["seeders"], r["peers"], "scrape", 2,
                                             detail=self._detail(r["seeders"], r["peers"], "scrape", now))
            due = [p for p in self.pending if p[0] <= now]
            self.pending = [p for p in self.pending if p[0] > now]
            for _, ih in due:
                if self.rng.random() < 0.25:
                    self.store.mark_failed(ih)
                    continue
                self._add_torrent(ih, now)
            for _ in range(self.rng.randint(0, 6)):
                self.store.note_node(f"10.{self.rng.randint(0, 255)}.{self.rng.randint(0, 255)}.{self.rng.randint(1, 254)}:{self.rng.randint(1025, 65000)}")
                self.store.note_peer(f"192.0.2.{self.rng.randint(1, 254)}.{self.rng.randint(0, 99999)}")
            rx = 80_000 * wave * self.rng.uniform(0.6, 1.4)
            tx = 35_000 * wave * self.rng.uniform(0.6, 1.4)
            q = self.rng.randint(0, 8)
            self.store.counters["bep51_queries"] += q
            self.store.counters["bep51_replies"] += int(q * 0.6)
            self.store.stats.add_traffic(rx, tx)
            self.rx_total += rx
            self.tx_total += tx
            self.live.update({
                "rx_bps": rx, "tx_bps": tx, "rx_total": int(self.rx_total), "tx_total": int(self.tx_total),
                "dht_nodes": int(600 + 200 * wave + self.rng.randint(-20, 20)),
                "peers_connected": int(30 * wave + self.rng.randint(0, 10)),
                "peers_half_open": self.rng.randint(0, 8),
                "active_probes": len(self.pending), "sampling": True,
            })
            if now - last_pt >= 10 or not last_pt:
                last_pt = now
                a = self.store.analytics()
                lv = self.live
                self.store.record_point({
                    "t": int(now), "rx_bps": round(lv["rx_bps"]), "tx_bps": round(lv["tx_bps"]),
                    "dht_nodes": lv["dht_nodes"], "peers": lv["peers_connected"],
                    "connections": lv["peers_connected"] + lv["peers_half_open"],
                    "probes": lv["active_probes"], "torrents": a["torrents"], "files": a["files"],
                    "hashes": a["hashes_discovered"], "pending": a["hashes_pending"],
                    "ok": a["counters"].get("metadata_ok", 0), "fail": a["counters"].get("probe_timeouts", 0),
                    "drop": a["counters"].get("stale_dropped", 0),
                    "bq": a["counters"].get("bep51_queries", 0), "br": a["counters"].get("bep51_replies", 0),
                    "verified": a["health_verified"],
                })
            time.sleep(1)

    def snapshot(self):
        return dict(self.live)
