"""
DHT crawler based on libtorrent (2.x).

How it discovers torrents:
  1. Passive: every DHT node that queries us (get_peers) or announces to us (announce_peer)
     reveals an infohash with a live swarm  -> dht_get_peers_alert / dht_announce_alert.
  2. Active (BEP 51): asks other nodes for samples of the infohashes they store
     -> session.dht_sample_infohashes() / dht_sample_infohashes_alert.
     Replies also carry more nodes, which is how the DHT gets traversed.

How it gets the information:
  For each infohash it adds a "metadata only" torrent (upload_mode, no content
  download), waits for metadata_received_alert (BEP 9), stores name, size and file
  list, measures seeders/peers (connected peers + tracker scrape) and removes it.
  While the probe runs it records the swarm's peers (IP, port, client, seeder/leecher) -> peers.py.
"""
import ipaddress
import heapq
import logging
import os
import sys
import random
import tempfile
import threading
import time
from collections import Counter, deque

try:
    import libtorrent as lt
except ImportError:  # --demo mode works without libtorrent
    lt = None

log = logging.getLogger("crawler")

from trackers import DEFAULT_TRACKERS  # noqa: E402  (list in trackers.py)
BOOTSTRAP = "router.bittorrent.com:6881,router.utorrent.com:6881,dht.transmissionbt.com:6881,dht.libtorrent.org:25401"


def _val(obj, name, default=None):
    """Attribute or zero-argument method (bindings differ between versions)."""
    try:
        v = getattr(obj, name)
    except AttributeError:
        return default
    if callable(v):
        try:
            return v()
        except TypeError:
            return default
    return v


def _cat(*names):
    """Value of an alert category, trying several names; 0 if this build does not have it."""
    if lt is None:
        return 0
    for holder in ("alert.category_t", "alert_category"):
        try:
            obj = lt
            for part in holder.split("."):
                obj = getattr(obj, part)
        except AttributeError:
            continue
        for name in names:
            try:
                return int(getattr(obj, name))
            except Exception:
                continue
    return 0


def _is_global(ip):
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


def _ep_str(ep):
    try:
        if isinstance(ep, (tuple, list)):
            return f"{ep[0]}:{ep[1]}"
        return f"{ep.address()}:{ep.port()}"
    except Exception:
        return str(ep)


def _ep_tuple(ep):
    try:
        if isinstance(ep, (tuple, list)):
            return (str(ep[0]), int(ep[1]))
        return (str(ep.address()), int(ep.port()))
    except Exception:
        return None


PEER_POLL = 4                   # s between get_peer_info() reads per probe
NODES_MAX = 50_000              # DHT nodes used for BEP 51 sampling
NODE_MAX_FAILS = 3              # no answer 3 times in a row (2, 4 and 8 min) -> forgotten, making room
DHT_CONNECT_MAX = 60            # DHT peers handed to each probe (the same libtorrent would use)
META_T_BUCKETS = (10, 20, 30, 45, 60, 90)   # histogram of "seconds until metadata arrived"


class Job:
    __slots__ = ("ih", "handle", "kind", "started", "deadline", "finish_at",
                 "saved", "seeds", "peers", "scrape_asked", "scrape_ok", "scrape_n", "peers_at",
                 "tr", "conn_seeds", "conn_peers", "seed_ips", "peer_ips", "dht_ips", "md", "dht_conn")

    def __init__(self, ih, handle, kind, timeout):
        self.ih = ih
        self.handle = handle
        self.kind = kind              # "fetch" | "refresh"
        self.started = time.time()
        self.deadline = self.started + timeout
        self.finish_at = None
        self.saved = False
        self.seeds = 0
        self.peers = 0
        self.scrape_asked = 0           # no. of trackers asked for a scrape
        self.scrape_ok = False
        self.scrape_n = 0               # no. of trackers that answered
        self.peers_at = 0.0             # last read of connected peers
        self.tr = {}                    # url -> {"s": seeders, "l": leechers, "r": peers returned on announce, "e"/"ae": error}
        self.conn_seeds = 0             # seeders CONNECTED at once (status.num_seeds): confirmed, not "reported"
        self.conn_peers = 0             # leechers connected at once
        self.seed_ips = set()           # IPs seen as seeders in this probe (full bitfield / have_all)
        self.peer_ips = set()           # IPs connected in this probe (any role)
        self.dht_ips = set()            # IPs the DHT returned for this torrent
        self.md = False                 # metadata was received in THIS probe (someone answered with the torrent)
        self.dht_conn = 0               # DHT peers handed to libtorrent to connect to (cap DHT_CONNECT_MAX)

    def detail(self, now):
        """Breakdown of the measurement: who reported what. Goes to the journal and to the web."""
        return {"at": int(now),
                "tr": [{"u": u, **v} for u, v in self.tr.items()],
                "cs": max(self.conn_seeds, len(self.seed_ips)), "cp": self.conn_peers, "ci": len(self.peer_ips),
                "dht": len(self.dht_ips), "md": int(self.md)}


class Crawler(threading.Thread):
    def __init__(self, store, cfg=None):
        super().__init__(daemon=True, name="crawler")
        c = {
            "port": 6881,
            "max_probes": 60,           # simultaneous metadata downloads
            "probe_timeout": 90,        # s before giving up without metadata
            "health_window": 18,        # MAX s to wait for health after getting metadata (ends sooner if the scrape answers)
            "refresh_window": 20,
            "refresh_share": 0.15,      # share of probes reserved for refreshing the health of what is already indexed
            "sample_qps": 10,           # BEP51 queries per second
            "connection_speed": 30,     # outgoing connection attempts per second, TOTAL (30 = libtorrent's: same load)
            "nopeer_giveup": 0,         # s: give up a probe with NO peer at this age (0 = no; >0 = more probes/hour = more load)
            "bep51_buffer": 2000,       # sampled hashes waiting above which no more samples are requested (0 = no limit)
            "dht_announce": False,      # announce ourselves in the DHT when probing (True = old behaviour: ghost connections)
            "use_trackers": True,
            "refresh": True,
            "state_file": os.path.join(store.dir, "dht_state.bin"),
        }
        c.update(cfg or {})
        c["trackers"] = list(c.get("trackers") or DEFAULT_TRACKERS)
        self.cfg = c
        self.store = store
        self.stop_evt = threading.Event()
        self.jobs = {}                       # ih -> Job
        self.known_nodes = {}                # (ip, port) -> [when it may be asked again, consecutive failures]
        self.node_heap = []                  # (when, (ip, port)): only nodes that are ALREADY due are looked at (O(log n))
        self.live = {}                       # live metrics for the web
        self.sample_supported = None
        self.tmpdir = tempfile.mkdtemp(prefix="tsearch-")
        self._prev = None
        self.ses = None
        self.error = None
        self.ext_ips = set()                 # our external IPs (do not query ourselves)
        self.alert_counts = Counter()        # alerts by type in the current window
        self._seen_types = set()
        self._tokens = 0.0                   # token bucket limiting BEP 51 queries
        self._tok_t = time.time()
        self._sample_errors = 0
        self.tracker_stats = {}              # url -> global scrape/announce counters (admin panel)
        self._noann = None                   # probes without announcing in the DHT? (decided at the first probe)

    # ------------------------------------------------------------------ setup
    def _make_session(self):
        cats = {
            "dht": _cat("dht_notification"),
            "dht_operation": _cat("dht_operation_notification", "dht_operation"),
            "error": _cat("error_notification"),
            "status": _cat("status_notification"),
            "stats": _cat("stats_notification"),
            # Without this category libtorrent 2.0 does NOT emit scrape_reply_alert or tracker_reply_alert!
            # (it used to be missing; added as a safeguard)
            "tracker": _cat("tracker_notification"),
        }
        mask = 0
        for v in cats.values():
            mask |= v
        log.info("alert mask 0x%x  categories: %s", mask, {k: bool(v) for k, v in cats.items()})
        if not cats["dht_operation"]:
            log.warning("this build has no dht_operation category; BEP 51 replies might not arrive")
        settings = {
            "user_agent": f"libtorrent/{lt.__version__}",
            "listen_interfaces": f"0.0.0.0:{self.cfg['port']},[::]:{self.cfg['port']}",
            "enable_dht": True,
            "enable_lsd": False,
            "enable_upnp": True,
            "enable_natpmp": True,
            "alert_mask": mask or 0x7FFFFFFF,
            "dht_bootstrap_nodes": BOOTSTRAP,
            "active_downloads": -1, "active_seeds": -1, "active_checking": -1,
            "active_dht_limit": -1, "active_tracker_limit": -1, "active_lsd_limit": -1,
            "active_limit": max(500, self.cfg["max_probes"] * 4),
            "connections_limit": 3000,
            "dht_upload_rate_limit": 200_000,
            "dht_announce_interval": 900,
        }
        # Tuning to GET METADATA without increasing network load:
        tuning = {
            # GLOBAL budget of connection attempts per second (30 = libtorrent's). With 150 probes that is ~9 peers tried
            # per torrent in 45 s: raising it (--connection-speed) gives the most success, but also more connections.
            "connection_speed": int(self.cfg["connection_speed"]),
            # 15 s by default: a peer that does not answer holds the attempt (and a half-open entry) for half as long.
            # Same attempts per second, fewer connections open at once.
            "peer_connect_timeout": 7,
            # 3 MB by default: torrents with tens of thousands of files could NEVER be fetched (and the attempt was
            # repeated up to 3 times). Does not change the load: it just stops throwing away already-downloaded metadata.
            "max_metadata_size": 32 * 1024 * 1024,
        }
        try:
            known = set(lt.default_settings())
        except Exception:
            known = set()
        applied = {k: v for k, v in tuning.items() if k in known}
        settings.update(applied)
        if set(tuning) - set(applied):
            log.warning("libtorrent settings not available in this version (ignored): %s", sorted(set(tuning) - set(applied)))
        log.info("connection settings: %s", applied)
        sp = lt.session_params(settings)
        try:
            with open(self.cfg["state_file"], "rb") as f:
                old = lt.read_session_params(f.read())
            sp.dht_state = old.dht_state           # previous routing table: fast start
        except Exception:
            pass
        return lt.session(sp)

    def _save_state(self):
        try:
            buf = lt.write_session_params_buf(self.ses.session_state())
            with open(self.cfg["state_file"], "wb") as f:
                f.write(buf)
        except Exception as e:
            log.debug("could not save the DHT state: %s", e)

    # ---------------------------------------------------------------- loop
    def run(self):
        if lt is None:
            self.error = "libtorrent is not installed (pip install libtorrent)"
            log.error(self.error)
            return
        try:
            self.ses = self._make_session()
        except Exception as e:
            self.error = f"could not create the session: {e}"
            log.exception(self.error)
            return
        log.info("libtorrent %s, DHT starting on port %s", lt.__version__, self.cfg["port"])
        seeded = 0
        for addr in list(self.store.nodes)[:20000]:               # nodes from previous sessions: sampling starts right away
            try:
                ip, _, port = addr.rpartition(":")
                self._add_node((ip, int(port)))
                seeded += 1
            except ValueError:
                pass
        if seeded:
            log.info("seeded %d DHT nodes from previous sessions (%d accepted)", seeded, len(self.known_nodes))
        self.sample_supported = hasattr(self.ses, "dht_sample_infohashes")
        if not self.sample_supported:
            log.warning("this libtorrent build does not expose dht_sample_infohashes: passive discovery only")

        t_stats = t_pt = t_live = t_state = t_sum = 0.0
        while not self.stop_evt.is_set():
            try:
                self.ses.wait_for_alert(500)
                for a in self.ses.pop_alerts():
                    try:
                        self._on_alert(a)
                    except Exception:
                        log.exception("error processing %s", type(a).__name__)
                now = time.time()
                self._fill_probes()
                self._service_jobs(now)
                self._sample_nodes()
                if now - t_stats >= 2:
                    t_stats = now
                    self.ses.post_session_stats()
                    self.ses.post_dht_stats()
                if now - t_live >= 20:
                    t_live = now
                    self.ses.dht_live_nodes(lt.sha1_hash(os.urandom(20)))
                if now - t_pt >= 10:
                    t_pt = now
                    self._record_point(now)
                if now - t_state >= 300:
                    t_state = now
                    self._save_state()
                if now - t_sum >= 60:
                    t_sum = now
                    self._log_summary()
            except Exception:
                log.exception("error in the crawler loop")
                time.sleep(1)
        self._save_state()
        try:
            self.ses.pause()
        except Exception:
            pass

    def stop(self):
        self.stop_evt.set()

    # -------------------------------------------------------------- alerts
    def _log_summary(self):
        top = dict(self.alert_counts.most_common(12))
        c = self.store.counters
        log.info("60s summary | alerts: %s | known nodes: %d (%d scheduled) | BEP51: %d queries, %d replies, %d hashes | "
                 "active probes: %d", top, len(self.known_nodes), len(self.node_heap),
                 c.get("bep51_queries", 0), c.get("bep51_replies", 0), c.get("bep51_hashes", 0), len(self.jobs))
        self.alert_counts.clear()

    def _on_alert(self, a):
        n = type(a).__name__
        st = self.store
        self.alert_counts[n] += 1
        if n not in self._seen_types:
            self._seen_types.add(n)
            attrs = [x for x in dir(a) if not x.startswith("_")]
            log.info("first alert %s; attributes: %s", n, attrs[:40])
        if n == "dht_announce_alert":
            ih = str(a.info_hash)
            ip = str(_val(a, "ip", "") or "")
            port = int(_val(a, "port", 0) or 0)
            # Whoever announces HAS the torrent and gives us its IP:port: stored so we can connect to it directly
            # (without waiting for libtorrent to find peers in the DHT, which is where the timeouts went)
            peer = (ip, port) if port and self._peer_ok(ip) else None
            st.add_hash(ih, "announce", prio=True, peer=peer)
            job = self.jobs.get(ih.lower())
            if peer and job is not None and not job.saved:          # already being probed: connect now
                self._connect_peers(job.handle, [peer])
            st.note_peer(ip)
            if ih in st.torrents and self._peer_ok(ip):             # someone announces being in the swarm of something indexed
                st.peers.note(ih, ip, int(_val(a, "port", 0) or 0), -1, "", "a")
        elif n == "dht_get_peers_alert":
            # Whoever ASKS for a torrent is looking for it, not necessarily holding it: normal priority
            # (promoted if seen several times). It is ~90 % of the events.
            st.add_hash(str(a.info_hash), "get_peers", prio=False)
        elif n == "dht_sample_infohashes_alert":
            self._on_samples(a)
        elif n == "dht_get_peers_reply_alert":
            self._on_get_peers_reply(a)
        elif n == "dht_live_nodes_alert":
            for item in (_val(a, "nodes", []) or []):
                try:
                    self._add_node(item[1])
                except Exception:
                    pass
        elif n == "dht_outgoing_get_peers_alert":
            # nodes our own peer lookups are querying: an abundant and reliable source
            self._add_node(_val(a, "endpoint") or _val(a, "ip"))
        elif n == "external_ip_alert":
            ip = str(_val(a, "external_address", "") or "")
            if ip and ip not in self.ext_ips:
                self.ext_ips.add(ip)
                log.info("external IP detected: %s (excluded from queries and connections)", ip)
                self._block_ip(ip)
        elif n == "dht_stats_alert":
            rt = _val(a, "routing_table", []) or []
            self.live["dht_nodes"] = sum(b.get("num_nodes", 0) for b in rt)
            self.live["dht_replacements"] = sum(b.get("num_replacements", 0) for b in rt)
            self.live["dht_active_requests"] = len(_val(a, "active_requests", []) or [])
        elif n == "session_stats_alert":
            self._on_session_stats(_val(a, "values", {}) or {})
        elif n == "metadata_received_alert":
            self._on_metadata(a)
        elif n == "scrape_reply_alert":
            job = self._job_of(a)
            url = self._tr_url(a, job)
            seeds, leech = int(_val(a, "complete", 0) or 0), int(_val(a, "incomplete", 0) or 0)
            self._tr_stat(url, "scrape_ok")
            if job:
                job.scrape_ok = True
                job.scrape_n += 1
                job.tr.setdefault(url, {}).update(s=seeds, l=leech)
                job.tr[url].pop("e", None)
                job.seeds = max(job.seeds, seeds)      # each tracker only sees ITS part of the swarm: keep the highest
                job.peers = max(job.peers, leech)      # figure (the per-tracker breakdown stays in job.tr)
                if job.finish_at:                                  # metadata already here: wait only briefly for the rest
                    allin = job.scrape_asked and job.scrape_n >= job.scrape_asked
                    job.finish_at = min(job.finish_at, time.time() + (1 if allin else 4))
        elif n == "scrape_failed_alert":
            job = self._job_of(a)
            url = self._tr_url(a, job)
            err = str(_val(a, "error_message", "") or _val(a, "msg", "") or _val(a, "message", "") or "error")[:160]
            self._tr_stat(url, "scrape_fail", err)
            if job and "s" not in job.tr.get(url, {}):
                job.tr.setdefault(url, {})["e"] = err
        elif n == "tracker_reply_alert":
            job = self._job_of(a)
            url = self._tr_url(a, job)
            self._tr_stat(url, "announce_ok")
            if job:
                d = job.tr.setdefault(url, {})
                d["r"] = max(d.get("r", 0), int(_val(a, "num_peers", 0) or 0))
                d.pop("ae", None)
        elif n == "tracker_error_alert":
            job = self._job_of(a)
            url = self._tr_url(a, job)
            err = str(_val(a, "error_message", "") or _val(a, "msg", "") or _val(a, "message", "") or "error")[:160]
            self._tr_stat(url, "announce_fail", err)
            if job and "r" not in job.tr.get(url, {}):
                job.tr.setdefault(url, {})["ae"] = err
        elif n in ("torrent_error_alert", "dht_error_alert"):
            log.debug("%s: %s", n, _val(a, "message", ""))

    @staticmethod
    def _tr_url(a, job=None):
        url = _val(a, "tracker_url") or _val(a, "url") or ""
        if isinstance(url, bytes):
            url = url.decode("utf-8", "replace")
        url = str(url)
        if not url and job is not None and len(job.tr) == 1:
            url = next(iter(job.tr))
        return url or "?"

    def _tr_stat(self, url, key, err=""):
        s = self.tracker_stats.get(url)
        if s is None:
            if len(self.tracker_stats) > 200:           # trackers from the .torrent itself: do not grow without bound
                return
            s = self.tracker_stats[url] = {"scrape_ok": 0, "scrape_fail": 0, "announce_ok": 0, "announce_fail": 0,
                                           "last_ok": 0, "last_fail": 0, "last_error": ""}
        s[key] += 1
        if key.endswith("_ok"):
            s["last_ok"] = int(time.time())
        else:
            s["last_fail"], s["last_error"] = int(time.time()), err

    def tracker_report(self):
        """Health of each tracker (for the panel): does it really answer?"""
        out = []
        for url, s in list(self.tracker_stats.items()):
            n = s["scrape_ok"] + s["scrape_fail"]
            out.append({"url": url, **s, "scrape_rate": round(s["scrape_ok"] / n, 3) if n else None,
                        "configured": url in self.cfg["trackers"]})
        out.sort(key=lambda x: (not x["configured"], -(x["scrape_ok"] + x["scrape_fail"])))
        return out

    def _no_dht_announce(self):
        if self._noann is None:
            self._noann = bool(self.cfg.get("dht_announce") is False and lt is not None
                               and getattr(getattr(lt, "torrent_flags", None), "disable_dht", None) is not None
                               and self.ses is not None and hasattr(self.ses, "dht_get_peers"))
            log.info("probes %s in the DHT", "WITHOUT announcing (lookup with dht_get_peers)" if self._noann else "announcing")
        return self._noann

    def _connect_peers(self, handle, peers, counter="direct_connects"):
        n = 0
        for ip, port in peers:
            try:
                handle.connect_peer((ip, int(port)))
                n += 1
            except Exception as e:                      # old bindings / invalid endpoint: not serious
                log.debug("connect_peer %s:%s: %s", ip, port, e)
        if n:
            self.store.counters[counter] += n
        return n

    def _peer_ok(self, ip):
        return bool(ip) and ip not in self.ext_ips and _is_global(ip)

    def _on_get_peers_reply(self, a):
        """Peers the DHT returns for OUR lookups (started by libtorrent or by us when probing a torrent)."""
        ps = self.store.peers
        ih = str(_val(a, "info_hash", "") or "")
        job = self.jobs.get(ih)
        if job is None and ih not in self.store.torrents:
            return
        for ep in (_val(a, "peers", []) or [])[:200]:
            t = _ep_tuple(ep)
            if t and self._peer_ok(t[0]):
                if job is not None and len(job.dht_ips) < 2000:
                    if (self._noann and not job.saved and job.dht_conn < DHT_CONNECT_MAX and t[0] not in job.dht_ips):
                        # with the torrent's DHT disabled, libtorrent does not know these peers: hand them over (nothing is announced)
                        job.dht_conn += self._connect_peers(job.handle, [t], "dht_connects")
                    job.dht_ips.add(t[0])
                if ps.enabled:
                    ps.note(ih, t[0], t[1], -1, "", "d")

    def _collect_peers(self, job):
        """Peers connected to a probe: IP, port, client and whether it is a seeder."""
        ps = self.store.peers
        try:
            infos = job.handle.get_peer_info()
        except Exception:
            return
        pi_cls = getattr(lt, "peer_info", None)
        seed_flag = int(getattr(pi_cls, "seed", 0) or 0)
        known = job.saved                          # without metadata the bitfield cannot be interpreted: role unknown
        for pi in infos:
            t = _ep_tuple(_val(pi, "ip"))
            if not t or not self._peer_ok(t[0]):
                continue
            flags = int(_val(pi, "flags", 0) or 0)
            role = 1 if (seed_flag and flags & seed_flag) else (0 if known else -1)
            if role != 1 and known:
                try:                                # without the flag, a full bitfield is also a seeder
                    if float(_val(pi, "progress", 0) or 0) >= 1.0:
                        role = 1
                except (TypeError, ValueError):
                    pass
            client = _val(pi, "client", "") or ""
            if isinstance(client, bytes):
                client = client.decode("utf-8", "replace")
            job.peer_ips.add(t[0])
            if role == 1:
                job.seed_ips.add(t[0])
            if ps.enabled:
                ps.note(job.ih, t[0], t[1], role, str(client), "c")

    def _job_of(self, a):
        try:
            return self.jobs.get(str(a.handle.info_hashes().v1))
        except Exception:
            return None

    # ---------------------------------------------------------- DHT sampling
    def _block_ip(self, ip):
        """Prevents libtorrent from connecting to our own public IP (peers returned by trackers/DHT)."""
        try:
            f = self.ses.get_ip_filter()
            f.add_rule(ip, ip, 1)
            self.ses.set_ip_filter(f)
        except Exception as e:
            log.debug("could not add %s to the ip_filter: %s", ip, e)

    def _add_node(self, ep):
        t = _ep_tuple(ep)
        if not t or t in self.known_nodes:
            return
        if t[0] in self.ext_ips or not _is_global(t[0]) or not (0 < t[1] < 65536):
            return
        if len(self.known_nodes) >= NODES_MAX:
            return
        self.known_nodes[t] = [0.0, 0]
        heapq.heappush(self.node_heap, (0.0, t))
        self.store.note_node(f"{t[0]}:{t[1]}")

    def _on_samples(self, a):
        self.store.counters["bep51_replies"] += 1
        for h in (_val(a, "samples", []) or []):
            self.store.counters["bep51_hashes"] += 1
            self.store.add_hash(str(h), "bep51")
        for item in (_val(a, "nodes", []) or []):
            try:
                self._add_node(item[1])
            except Exception:
                pass
        t = _ep_tuple(_val(a, "endpoint"))
        interval = _val(a, "interval", 60)
        try:
            interval = interval.total_seconds()
        except AttributeError:
            interval = float(interval or 60)
        if t and t in self.known_nodes:
            # It answered: ask again when it says (interval, up to 6 h), with some randomness. Without it, nodes queried at
            # the same time become available at the same time: long silences followed by a flood.
            nxt = time.time() + max(interval, 60) * random.uniform(1.0, 1.3)
            self.known_nodes[t] = [nxt, 0]
            heapq.heappush(self.node_heap, (nxt, t))

    def _sample_nodes(self):
        if not self.sample_supported or not self.node_heap:
            return
        now = time.time()
        qps = float(self.cfg["sample_qps"])
        # Backpressure: if enough sampled hashes are already waiting, do NOT ask for more. The most recent are probed
        # (LIFO) and whatever does not fit is dropped: asking for more only spent UDP to throw it away.
        buf = int(self.cfg.get("bep51_buffer") or 0)
        q = getattr(self.store, "queues", {}).get("other")
        paused = bool(buf and q is not None and len(q) >= buf)
        self.live["bep51_paused"] = paused
        if paused:
            self._tokens, self._tok_t = 0.0, now          # no token build-up: no burst on resume
            self.store.counters["bep51_paused_ticks"] += 1
            return
        self._tokens = min(self._tokens + (now - self._tok_t) * qps, qps * 2)
        self._tok_t = now
        hp, known = self.node_heap, self.known_nodes
        while self._tokens >= 1 and hp and hp[0][0] <= now:
            due, t = heapq.heappop(hp)
            e = known.get(t)
            if e is None or e[0] != due:
                continue                                  # stale entry (already rescheduled)
            if e[1] >= NODE_MAX_FAILS:                    # never answers: out, making room for new nodes
                del known[t]
                self.store.counters["bep51_nodes_evicted"] += 1
                continue
            e[1] += 1                                     # provisional: reset to 0 if it answers
            e[0] = now + 120 * (2 ** (e[1] - 1)) * random.uniform(1.0, 1.3)
            heapq.heappush(hp, (e[0], t))
            try:
                self.ses.dht_sample_infohashes(t, lt.sha1_hash(os.urandom(20)))
                self._tokens -= 1
                self._sample_errors = 0
                self.store.counters["bep51_queries"] += 1
            except Exception as ex:
                self._sample_errors += 1
                if self._sample_errors <= 3:
                    log.warning("dht_sample_infohashes(%r) failed: %s: %s", t, type(ex).__name__, ex)
                if self._sample_errors >= 20:
                    log.error("20 failures in a row in dht_sample_infohashes; disabling active sampling")
                    self.sample_supported = False
                    return

    # ------------------------------------------------------ metadata download
    def _fill_probes(self):
        maxp = self.cfg["max_probes"]
        target = int(maxp * self.cfg["refresh_share"]) if self.cfg["refresh"] else 0
        n_ref = sum(1 for j in self.jobs.values() if j.kind == "refresh")
        while len(self.jobs) < maxp:
            ih, kind = self.store.next_forced(), "refresh"            # requested from the admin panel: first
            if ih is not None:
                if ih not in self.jobs:
                    self._start_job(ih, kind)
                    n_ref += 1
                continue
            ih, kind = None, "fetch"
            if n_ref < target and self.store.refresh_due():       # reserved quota: with the pending queue always full,
                ih, kind = self.store.next_refresh(), "refresh"    # without it what is indexed would NEVER be refreshed
            if ih is None:
                ih, kind = self.store.next_pending(), "fetch"
            if ih is None and self.cfg["refresh"]:
                ih, kind = self.store.next_refresh(), "refresh"
            if ih is None or ih in self.jobs:
                return
            self._start_job(ih, kind)
            if kind == "refresh":
                n_ref += 1

    def _start_job(self, ih, kind):
        try:
            uri = f"magnet:?xt=urn:btih:{ih}"
            params = lt.parse_magnet_uri(uri)
            params.save_path = self.tmpdir
            params.flags |= lt.torrent_flags.upload_mode
            params.flags &= ~lt.torrent_flags.auto_managed
            params.flags &= ~lt.torrent_flags.paused
            if self.cfg["use_trackers"]:
                params.trackers = list(self.cfg["trackers"])
            no_announce = self._no_dht_announce()
            if no_announce:
                # Do NOT announce ourselves in the DHT: the probe lasts 45 s but the DHT keeps our IP ~30 min, and meanwhile the
                # swarm's clients keep connecting to a torrent we no longer have (in production: ~364 INCOMING connections
                # per second, almost all useless). Peers are looked up with dht_get_peers, which does NOT announce.
                params.flags |= lt.torrent_flags.disable_dht
            handle = self.ses.add_torrent(params)
            if no_announce:
                try:
                    self.ses.dht_get_peers(lt.sha1_hash(bytes.fromhex(ih)))
                    self.store.counters["dht_lookups"] += 1
                except Exception as e:
                    log.debug("dht_get_peers %s: %s", ih, e)
        except Exception as e:
            log.debug("add_torrent %s: %s", ih, e)
            if kind == "fetch":
                self.store.mark_failed(ih)
            else:
                self.store.defer_refresh(ih)
            return
        timeout = self.cfg["probe_timeout"] if kind == "fetch" else self.cfg["refresh_window"]
        job = Job(ih, handle, kind, timeout)
        if self.cfg["use_trackers"]:               # the scrape does not need metadata: ask every tracker right away
            job.tr = {u: {} for u in self.cfg["trackers"]}      # {} = asked and has not answered (yet)
            for i in range(len(self.cfg["trackers"])):
                try:
                    handle.scrape_tracker(i)
                    job.scrape_asked += 1
                except Exception:
                    pass
        if kind == "refresh":
            job.finish_at = job.started + self.cfg["refresh_window"]
        if kind == "fetch":                            # peers that announced having it: direct connection, without waiting for the DHT
            self._connect_peers(handle, [p for p in self.store.hash_peers(ih) if self._peer_ok(p[0])])
        self.jobs[ih] = job

    def _on_metadata(self, a):
        job = self._job_of(a)
        if not job or job.saved:
            return
        job.saved = True
        job.md = True
        if job.kind == "fetch":
            dt = time.time() - job.started
            b = next((x for x in META_T_BUCKETS if dt < x), None)
            self.store.counters[f"meta_t_lt{b}" if b else f"meta_t_ge{META_T_BUCKETS[-1]}"] += 1
            ti = a.handle.torrent_file()
            info = self._extract(ti)
            self.store.save_torrent(job.ih, info)
            self.store.counters["metadata_ok"] += 1
        allin = job.scrape_asked and job.scrape_n >= job.scrape_asked
        job.finish_at = time.time() + (1 if allin else 4 if job.scrape_ok else self.cfg["health_window"])

    @staticmethod
    def _extract(ti):
        fs = ti.files()
        files = [(fs.file_path(i), fs.file_size(i)) for i in range(fs.num_files())]
        created = 0
        try:
            cd = ti.creation_date()
            created = int(cd.timestamp()) if hasattr(cd, "timestamp") else int(cd or 0)
        except Exception:
            pass
        trackers = []
        try:
            trackers = [t.url for t in ti.trackers()]
        except Exception:
            pass
        return {
            "name": ti.name(), "size": ti.total_size(), "files": files,
            "piece_length": ti.piece_length(), "created": created,
            "comment": ti.comment(), "created_by": ti.creator(),
            "private": ti.priv(), "trackers": trackers,
        }

    def _service_jobs(self, now):
        for ih, job in list(self.jobs.items()):
            st = None
            try:
                st = job.handle.status()
                # Only what is CONNECTED. list_seeds/list_peers are candidates (PEX, DHT, trackers) that may never have been
                # connected to: counting them as seeders inflated the "estimated" figures.
                job.conn_seeds = max(job.conn_seeds, int(st.num_seeds))
                job.conn_peers = max(job.conn_peers, int(st.num_peers) - int(st.num_seeds))
            except Exception:
                pass
            if now - job.peers_at >= PEER_POLL:
                job.peers_at = now
                self._collect_peers(job)
            done = False
            if job.finish_at and now >= job.finish_at:
                done = True
                self._collect_peers(job)                         # last snapshot of the swarm (with metadata by now)
                # "scrape" = a real tracker answer; "swarm" = only what was seen by connecting (lower bound)
                d = job.detail(now)
                seeds, peers = max(job.seeds, d["cs"]), max(job.peers, job.conn_peers)
                self.store.update_health(ih, seeds, peers, "scrape" if job.scrape_ok else "swarm", job.scrape_n, detail=d)
            elif not job.saved and now >= job.deadline:
                done = True
                if job.kind == "fetch":
                    self.store.mark_failed(ih)
            elif (self.cfg["nopeer_giveup"] and job.kind == "fetch" and not job.saved and st is not None
                  and now - job.started >= self.cfg["nopeer_giveup"]
                  and int(getattr(st, "list_peers", 1)) == 0 and int(getattr(st, "num_peers", 1)) == 0):
                # Neither the DHT, nor the trackers, nor an announce gave us a SINGLE peer: nobody to fetch metadata from.
                # Waiting for the full timeout only held the slot (counts as a failed attempt: retried if it shows up again).
                done = True
                self.store.counters["probe_nopeers"] += 1
                self.store.mark_failed(ih)
            if done:
                try:
                    self.ses.remove_torrent(job.handle)
                except Exception:
                    pass
                del self.jobs[ih]

    # ------------------------------------------------------------- metrics
    def _on_session_stats(self, v):
        def g(*names):
            return sum(int(v.get(n, 0)) for n in names)
        rx = g("net.recv_bytes", "dht.dht_bytes_in")
        tx = g("net.sent_bytes", "dht.dht_bytes_out")
        now = time.time()
        rx_bps = tx_bps = 0.0
        if self._prev:
            dt = max(now - self._prev[0], 0.001)
            rx_bps = max(rx - self._prev[1], 0) / dt
            tx_bps = max(tx - self._prev[2], 0) / dt
            # PERSISTENT cumulative traffic (libtorrent counters start from 0 at every start)
            self.store.stats.add_traffic(max(rx - self._prev[1], 0), max(tx - self._prev[2], 0))
        self._prev = (now, rx, tx)
        self.live.update({
            "rx_total": rx, "tx_total": tx, "rx_bps": rx_bps, "tx_bps": tx_bps,
            "dht_rx_total": g("dht.dht_bytes_in"), "dht_tx_total": g("dht.dht_bytes_out"),
            "peers_connected": g("peer.num_peers_connected"),
            "peers_half_open": g("peer.num_peers_half_open"),
            "dht_torrents": g("dht.dht_torrents"),
            # are INCOMING connections arriving? (0 = the port is not open on the router: half the network is lost)
            "incoming_connections": g("peer.incoming_connections"),
            "connection_attempts": g("peer.connection_attempts"),
            "connect_timeouts": g("peer.connect_timeouts"),
            "dht_nodes_metric": g("dht.dht_nodes"),
        })

    def _record_point(self, now):
        a = self.store.analytics()
        lv = self.live
        self.store.record_point({
            "t": int(now),
            "rx_bps": round(lv.get("rx_bps", 0)), "tx_bps": round(lv.get("tx_bps", 0)),
            "dht_nodes": lv.get("dht_nodes", lv.get("dht_nodes_metric", 0)),
            "peers": lv.get("peers_connected", 0),
            "connections": lv.get("peers_connected", 0) + lv.get("peers_half_open", 0),
            "probes": len(self.jobs),
            "torrents": a["torrents"], "files": a["files"],
            "hashes": a["hashes_discovered"], "pending": a["hashes_pending"],
            # cumulative counters: allow deriving rates (per min/h) and success rates on the web
            "ok": a["counters"].get("metadata_ok", 0), "fail": a["counters"].get("probe_timeouts", 0),
            "drop": a["counters"].get("stale_dropped", 0),
            "bq": a["counters"].get("bep51_queries", 0), "br": a["counters"].get("bep51_replies", 0),
            "verified": a["health_verified"],
        })

    def snapshot(self):
        lv = dict(self.live)
        lv["active_probes"] = len(self.jobs)
        lv["refresh_probes"] = sum(1 for j in self.jobs.values() if j.kind == "refresh")
        lv["nodes_known_to_crawl"] = len(self.known_nodes)
        lv["bep51_nodes_due"] = sum(1 for e in self.known_nodes.values() if e[0] <= time.time()) if len(self.known_nodes) < 200_000 else -1
        q = getattr(self.store, "queues", {}).get("other")
        lv["bep51_buffer"] = [len(q) if q is not None else 0, int(self.cfg.get("bep51_buffer") or 0)]
        lv["sampling"] = bool(self.sample_supported)
        lv["bep51_queries"] = self.store.counters.get("bep51_queries", 0)
        lv["bep51_replies"] = self.store.counters.get("bep51_replies", 0)
        lv["error"] = self.error
        return lv


def selftest():
    """Checks that this libtorrent build exposes the API the crawler needs."""
    if lt is None:
        print("libtorrent NOT installed -> pip install libtorrent")
        return 1
    from version import __version__ as _v
    print("DHT Search", _v, "· libtorrent", lt.__version__)
    ses = lt.session({"enable_dht": False, "listen_interfaces": "127.0.0.1:0"})
    checks = {
        "session.dht_sample_infohashes (BEP51)": hasattr(ses, "dht_sample_infohashes"),
        "session.dht_live_nodes": hasattr(ses, "dht_live_nodes"),
        "session.post_dht_stats": hasattr(ses, "post_dht_stats"),
        "session.post_session_stats": hasattr(ses, "post_session_stats"),
        "lt.parse_magnet_uri": hasattr(lt, "parse_magnet_uri"),
        "lt.session_params": hasattr(lt, "session_params"),
        "lt.torrent_flags.upload_mode": hasattr(getattr(lt, "torrent_flags", None), "upload_mode"),
        "lt.peer_info.seed (seeder/leecher role)": hasattr(getattr(lt, "peer_info", None), "seed"),
        "lt.torrent_flags.disable_dht + session.dht_get_peers (probe without announcing)":
            hasattr(getattr(lt, "torrent_flags", None), "disable_dht") and hasattr(ses, "dht_get_peers"),
        "torrent_handle.connect_peer": hasattr(lt, "torrent_handle") and hasattr(lt.torrent_handle, "connect_peer"),
        "alert category tracker_notification (needed for scrape replies)": bool(_cat("tracker_notification")),
    }
    for n in ("dht_announce_alert", "dht_get_peers_alert", "dht_sample_infohashes_alert",
              "dht_live_nodes_alert", "dht_stats_alert", "session_stats_alert",
              "metadata_received_alert", "scrape_reply_alert", "scrape_failed_alert", "tracker_reply_alert",
              "tracker_error_alert", "dht_get_peers_reply_alert"):
        checks[f"lt.{n}"] = hasattr(lt, n)
    ok = True
    for k, v in checks.items():
        print(f"  [{'OK' if v else '--'}] {k}")
        ok &= v
    try:
        names = sorted(m.name for m in lt.session_stats_metrics())
        print("  available bandwidth metrics:",
              [n for n in names if "bytes" in n and ("net.recv" in n or "net.sent" in n or "dht" in n)][:10])
        print("  peer metrics:", [n for n in names if n.startswith("peer.num_peers")][:6])
    except Exception as e:
        print("  session_stats_metrics not available:", e)
    # Also checks OUR code: instantiates the Crawler and processes simulated alerts.
    # (Detects, e.g., name clashes with threading.Thread depending on the Python version.)
    try:
        import tempfile
        from store import Store
        class _H:
            def __init__(s, x): s.x = x
            def __str__(s): return s.x
        st = Store(tempfile.mkdtemp())
        cr = Crawler(st, {"state_file": os.path.join(st.dir, "x.bin")})
        cr._on_alert(type("dht_announce_alert", (), {"info_hash": _H("ab" * 20), "ip": "1.2.3.4", "port": 1})())
        cr._on_alert(type("session_stats_alert", (), {"values": {"net.recv_bytes": 10}})())
        cr._on_alert(type("dht_stats_alert", (), {"routing_table": [{"num_nodes": 5}], "active_requests": []})())
        good = len(st.hashes) == 1 and cr.live.get("dht_nodes") == 5 and cr.live.get("rx_total") == 10
        print(f"  [{'OK' if good else '--'}] crawler alert handling (Python {sys.version.split()[0]})")
        ok &= good
    except Exception as e:
        print(f"  [--] crawler alert handling: {type(e).__name__}: {e}")
        ok = False
    print("RESULT:", "everything available" if ok else "missing pieces (the crawler degrades where it can)")
    return 0 if ok else 2
