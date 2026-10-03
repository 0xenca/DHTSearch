"""
Data store (JSON) + in-memory search indexes.

Files in data_dir:
  torrents.jsonl   append-only JSON Lines journal (one event per line: "n" new torrent, "h" health, "d" deleted).
                   Writing is O(1); it is compacted in the background. (Previously: a single torrents.json rewritten whole.)
  hashes.json      discovered infohashes: pending / failed
  nodes.json       DHT nodes seen (for a faster start)
  stats.json, history_*  -> see stats.py
  blocklist.txt    regular expressions; applied when indexing AND at startup to what is already indexed (they DELETE)
  hidden_rules.json hiding rules from the admin panel (they do NOT delete: they hide from search) -> hiderules.py
  peers.jsonl      peers seen per torrent (IP, port, seeder/leecher) -> peers.py
"""
import heapq
import json
import math
import os
import random
import re
import threading
import time
import zlib
from collections import Counter, deque

import search as _search
from hiderules import RuleBook, evaluate
from jsonio import Journal, atomic_write, load_json, read_jsonl
from packing import PostingIndex
from peers import PeerStore
from records import (STATES, apply_health, dump_rec, files_of, health_state, normalize_record, pack_files, refresh_due_at)
from stats import Stats
from textutil import (LEGACY_CATEGORIES, AGE_BUCKETS, SEED_BUCKETS, SIZE_BUCKETS, category_of, ext_of, index_tokens, magnet_of,
                      seed_bucket, size_bucket, top_exts, _LETTERS)

MAX_FILES_STORED = 5000          # files stored per torrent (file_count keeps the real number)
QUEUE_MAX = 20_000               # cap of pending hashes PER QUEUE; when full, the OLDEST are dropped
MAX_ATTEMPTS = 3                 # metadata download attempts for hashes with signs of life
RETRY_DELAY = 300                # s before retrying a hash that failed (retries no longer compete with the queue cap)
RETRY_MAX = 20_000
RETRY_FAILED_AFTER = 6 * 3600    # a failed hash that shows up again is retried after this long
FAILED_TTL = 3 * 86400           # failed hashes are purged after this long without being seen
FAILED_MAX = 50_000              # cap of retained failed hashes
MAX_NODES_PERSISTED = 100_000
FLUSH_MIN_INTERVAL = {"hashes": 300, "nodes": 60}
COMPACT_FACTOR = 3               # the journal is compacted when it has > 3 lines per torrent (+50,000)
FORCED_MAX = 1000                # refreshes requested from the admin panel, pending at most
QUEUES = ("peer", "prio", "getpeers", "other")
BANDIT_WINDOW = 4000             # recent probes that weigh in the split between queues


BLOCKLIST_HEADER = ("# One regular expression per line. Torrents whose name or files\n"
                    "# match are NOT indexed (and are deleted at startup if already present). Example:\n"
                    "# \\bmy_forbidden_word\\b\n")
LEGACY_BLOCKLIST_HEADER = ("# Una expresión regular por línea. Los torrents cuyo nombre o ficheros\n"
                           "# coincidan NO se indexan (y se eliminan al arrancar si ya estaban). Ejemplo:\n"
                           "# \\bmi_palabra_prohibida\\b\n")


class Store:
    def __init__(self, data_dir="data", peers_cfg=None):
        self.dir = data_dir
        os.makedirs(self.dir, exist_ok=True)
        self.lock = threading.RLock()
        self.stats = Stats(self.dir)
        self.counters = self.stats.counters              # same object: counters are persistent
        self.sources = self.stats.sources
        self.first_start = self.stats.life["first_start"]

        self.hashes = load_json(os.path.join(self.dir, "hashes.json"), {})
        self.nodes = set(load_json(os.path.join(self.dir, "nodes.json"), []))
        self._dirty = {"hashes": False, "nodes": False}
        self._last_flush = {"hashes": 0.0, "nodes": 0.0}
        self._english_blocklist_header()
        self.blocklist = self._load_blocklist()

        self.journal = Journal(os.path.join(self.dir, "torrents.jsonl"))
        self.torrents = {}
        self.legacy_converted = 0                          # records loaded with Spanish category names (<= 2.9)
        self._load_torrents()
        self.queue_max = QUEUE_MAX

        self._analytics_cache = (0, None)
        self._rebuild()

        # ---- admin panel: hiding and peers
        self.rules = RuleBook(self.dir)
        self.hidden = {}                                   # ih -> (rule id, …)   (excluded from public search)
        self._recompute_lock = threading.Lock()
        self.hidden_info = {"at": 0, "took_s": 0.0, "scanned": 0}
        self.peers = PeerStore(self.dir, **(peers_cfg or {}))
        self._forced, self._forced_set = deque(), set()   # refreshes requested from the panel (peers "now")
        if self.rules.active():
            r = self.recompute_hidden()
            print(f"[store] hiding rules: {r['hidden']} torrents hidden ({r['took_s']} s)")

    # ------------------------------------------------------------------ loading
    def _load_blocklist(self):
        path = os.path.join(self.dir, "blocklist.txt")
        pats = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        pats.append(re.compile(line, re.I))
        except FileNotFoundError:
            with open(path, "w", encoding="utf-8") as f:
                f.write(BLOCKLIST_HEADER)
        except re.error as e:
            print(f"[store] blocklist.txt has an invalid regex: {e}")
        return pats

    def _english_blocklist_header(self):
        """The comment header that versions <= 2.9 wrote in Spanish is replaced by the English one (patterns untouched)."""
        path = os.path.join(self.dir, "blocklist.txt")
        try:
            with open(path, encoding="utf-8") as f:
                txt = f.read()
        except FileNotFoundError:
            return
        if LEGACY_BLOCKLIST_HEADER in txt:
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                f.write(txt.replace(LEGACY_BLOCKLIST_HEADER, BLOCKLIST_HEADER))
            os.replace(path + ".tmp", path)

    def _blocked(self, name, files):
        if not self.blocklist:
            return False
        hay = name + "\n" + "\n".join(p for p, _ in files[:200])
        return any(p.search(hay) for p in self.blocklist)

    def _load_torrents(self):
        jl, old = self.journal.path, os.path.join(self.dir, "torrents.json")
        if os.path.exists(jl):
            n = 0
            for obj in read_jsonl(jl):
                n += 1
                self._apply_event(obj)
            self.journal.lines = n
        elif os.path.exists(old):                          # automatic migration from the old format
            t0 = time.time()
            data = load_json(old, {})
            for ih, rec in data.items():
                rec.setdefault("ih", ih)
                self.torrents[ih] = normalize_record(rec)
            self._write_full_journal()
            os.replace(old, old + ".migrated")
            print(f"[store] migrated {len(self.torrents)} torrents from torrents.json to torrents.jsonl in {time.time() - t0:.1f}s "
                  f"(copy kept in torrents.json.migrated; delete it whenever you like)")
        if self.legacy_converted:                          # journal written by <= 2.9: rewrite it once, all in English
            t0 = time.time()
            self._write_full_journal()
            print(f"[store] {self.legacy_converted} torrents had Spanish category names (<= 2.9); "
                  f"journal rewritten in English in {time.time() - t0:.1f}s")
        self.journal.open()
        removed = 0                                        # the blocklist also applies to what is already indexed
        for ih, rec in list(self.torrents.items() if self.blocklist else ()):
            if self._blocked(rec.get("name", ""), files_of(rec)):
                del self.torrents[ih]
                self.journal.append({"t": "d", "ih": ih})
                removed += 1
        if removed:
            self.counters["blocked_purged"] += removed
            print(f"[store] blocklist: removed {removed} already indexed torrents")

    def _write_full_journal(self):
        self.journal.begin_compaction()
        try:
            return self.journal.finish_compaction(
                json.dumps({"t": "n", "r": dump_rec(r)}, separators=(",", ":"), ensure_ascii=False) + "\n" for r in list(self.torrents.values()))
        except Exception:
            self.journal.abort_compaction()
            raise

    def _apply_event(self, obj):
        t = obj.get("t")
        if t == "n":
            if obj["r"].get("category") in LEGACY_CATEGORIES:
                self.legacy_converted += 1
            rec = normalize_record(obj["r"])
            self.torrents[rec["ih"]] = rec
        elif t == "h":
            rec = self.torrents.get(obj.get("ih"))
            if rec:
                apply_health(rec, obj["s"], obj["p"], obj["a"], obj["g"], obj.get("n", 1), obj.get("d"))
        elif t == "d":
            self.torrents.pop(obj.get("ih"), None)

    def _rebuild(self):
        """Derived indexes and aggregates; pending queue."""
        self.name_index, self.file_index, self.ext_index = PostingIndex(), PostingIndex(), PostingIndex()
        self.cat_counter, self.cat_bytes = Counter(), Counter()
        self.ext_counter, self.bucket_counter = Counter(), Counter()
        self.total_bytes = self.total_files = 0
        # One queue per SOURCE (LIFO each). Which one gets probed is decided by the MEASURED success of each (see next_pending):
        #   peer      announce_peer with IP:port (in production it turned out the WORST: 2.9 %; hence no fixed priority)
        #   prio      seen several times, retries, and failed hashes that show up again
        #   getpeers  someone is LOOKING for it right now (get_peers): active swarm
        #   other     BEP 51 sample seen once
        self.queues = {k: deque() for k in QUEUES}
        self._inq = {k: set() for k in QUEUES}
        self.queue_peer, self.queue_prio = self.queues["peer"], self.queues["prio"]
        self.queue_getpeers, self.queue = self.queues["getpeers"], self.queues["other"]
        self.retry = deque()
        self.bandit = {k: [0.0, 0.0] for k in QUEUES}      # recent [successes, probes] per queue (with decay)
        self.sched = "adaptive"                             # "fixed" = orden fijo (tests)
        for ih, rec in self.torrents.items():
            self._index_add(ih, rec)
        for ix in (self.name_index, self.file_index, self.ext_index):
            ix.compact()
        self._refresh_heap = [(refresh_due_at(r), ih) for ih, r in self.torrents.items()]
        heapq.heapify(self._refresh_heap)
        for ih, h in list(self.hashes.items()):
            if h.get("status") in ("pending", "probing", "retry"):
                h["status"] = "pending"
                if h.get("pe"):
                    self._enqueue(ih, peer=True)
                else:
                    self._enqueue(ih, prio=h.get("hits", 1) >= 2, src=h.get("src"))

    def _index_add(self, ih, rec):
        files = files_of(rec)
        for t in index_tokens(rec["name"]):
            self.name_index.add(t, ih)
        ftoks = set()
        for path, _ in files[:200]:
            ftoks |= index_tokens(path)
        for t in ftoks:
            self.file_index.add(t, ih)
        exts = [ext_of(p) for p, _ in files]
        for e in set(exts):
            if e:
                self.ext_index.add(e, ih)
        cat = rec.get("category", "Other")
        self.cat_counter[cat] += 1
        self.cat_bytes[cat] += rec.get("size", 0)
        self.bucket_counter[size_bucket(rec.get("size", 0))] += 1
        self.total_bytes += rec.get("size", 0)
        self.total_files += rec.get("file_count", 0)
        for e in exts:
            if e:
                self.ext_counter[e] += 1

    # ---------------------------------------------------------------- queue
    def _enqueue(self, ih, prio=False, peer=False, src=None):
        """Queues a hash in its source's queue. The most recent is processed first (LIFO): a hash that has been waiting a
        while almost certainly has no swarm any more. When a queue is full its oldest are dropped (unless the hash
        is also waiting in another queue)."""
        name = "peer" if peer else "prio" if prio else "getpeers" if src == "get_peers" else "other"
        inq = self._inq[name]
        if ih in inq:
            return
        inq.add(ih)
        q = self.queues[name]
        q.append(ih)
        # queue_max is the cap of EACH queue (4 queues: up to 4 × queue_max pending; 80,000 with the default)
        while len(q) > self.queue_max:
            old = q.popleft()
            inq.discard(old)
            oh = self.hashes.get(old)
            if oh and oh.get("status") == "pending" and not any(old in s for s in self._inq.values()):
                del self.hashes[old]
                self.counters["stale_dropped"] += 1
                self._dirty["hashes"] = True

    def _note_hash_peer(self, h, peer):
        """Stores (the last 4) IP:port that announced having this torrent."""
        pe = h.setdefault("pe", [])
        ep = [str(peer[0]), int(peer[1])]
        if ep in pe:
            return False
        pe.append(ep)
        if len(pe) > 4:
            del pe[0]
        return True

    def hash_peers(self, ih):
        """Known IP:port that have this torrent (from announce_peer). Used by the crawler to connect directly."""
        with self.lock:
            h = self.hashes.get(ih)
            return [tuple(x) for x in (h.get("pe") or ())] if h else []

    def add_hash(self, ih: str, src: str, prio: bool = False, peer=None) -> bool:
        """Records a seen infohash. Returns True if it is new.
        peer = (ip, port) of whoever announced it (announce_peer): that node HAS the torrent -> its own queue."""
        ih = ih.lower()
        now = int(time.time())
        with self.lock:
            self.counters["hash_events"] += 1
            self.sources[src] += 1
            if ih in self.torrents:
                self.torrents[ih]["last_seen"] = now
                return False
            h = self.hashes.get(ih)
            if h:
                h["last_seen"] = now
                h["hits"] = h.get("hits", 1) + 1
                st = h.get("status")
                if peer and self._note_hash_peer(h, peer):
                    self._dirty["hashes"] = True
                    if st in ("pending", "retry") or (st == "failed" and now - h.get("failed_at", 0) > RETRY_FAILED_AFTER / 6):
                        # now we know who has it: worth another attempt, and soon
                        if st != "pending":
                            h["status"], h["attempts"] = "pending", min(h.get("attempts", 0), MAX_ATTEMPTS - 1)
                        self._enqueue(ih, peer=True)
                        return False
                if st == "failed" and now - h.get("failed_at", 0) > RETRY_FAILED_AFTER:
                    # a "dead" hash that shows up again in the DHT may have come back to life: retry
                    h["status"], h["attempts"] = "pending", 0
                    self._enqueue(ih, True)
                    self._dirty["hashes"] = True
                elif st == "retry":                     # seen again while waiting: retry right away
                    h["status"] = "pending"
                    self._enqueue(ih, True)
                elif st == "pending" and (prio or h["hits"] >= 2):
                    self._enqueue(ih, True)             # announced or seen repeatedly => live swarm: to the front
                return False
            self.hashes[ih] = {"first_seen": now, "last_seen": now, "hits": 1,
                               "src": src, "attempts": 0, "status": "pending"}
            self.counters["hashes_new"] += 1
            if peer:
                self._note_hash_peer(self.hashes[ih], peer)
                self._enqueue(ih, peer=True)
            else:
                self._enqueue(ih, prio, src=src)
            self._dirty["hashes"] = True
            return True

    def _release_retries(self):
        now = time.time()
        while self.retry and self.retry[0][0] <= now:
            _, ih = self.retry.popleft()
            h = self.hashes.get(ih)
            if h and h.get("status") == "retry":
                h["status"] = "pending"
                self.counters["retries_released"] += 1
                self._enqueue(ih, True, peer=bool(h.get("pe")))

    def _pick_order(self):
        """Order of the queues for the next probe. Thompson sampling over each queue's recent success: the best one
        gets most probes, but none is left unexplored (if it improves, it shows)."""
        if self.sched == "fixed":
            return QUEUES
        draw = {k: random.betavariate(1 + ok, 1 + max(n - ok, 0)) for k, (ok, n) in self.bandit.items()}
        return sorted(QUEUES, key=lambda k: -draw[k])

    def _bandit_add(self, name, ok):
        b = self.bandit.get(name)
        if b is None:
            return
        b[0] += ok
        b[1] += 0 if ok else 1
        if not ok and sum(x[1] for x in self.bandit.values()) > BANDIT_WINDOW:     # decay: count what is recent
            for x in self.bandit.values():
                x[0] *= 0.5
                x[1] *= 0.5

    def next_pending(self):
        with self.lock:
            self._release_retries()
            for name in self._pick_order():
                q, inq = self.queues[name], self._inq[name]
                while q:
                    ih = q.pop()                # most recent first
                    inq.discard(ih)
                    h = self.hashes.get(ih)
                    if h and h.get("status") == "pending":
                        h["status"] = "probing"
                        h["qk"] = name
                        self.counters["probes_" + name] += 1            # success rate = metadata_ok_X / probes_X
                        self._bandit_add(name, 0)
                        return ih
            return None

    def _push_refresh(self, ih, rec):
        heapq.heappush(self._refresh_heap, (refresh_due_at(rec), ih))

    def _top_due(self):
        """Heap head after discarding stale entries (the due time is recomputed after every measurement)."""
        hp = self._refresh_heap
        while hp:
            due, ih = hp[0]
            rec = self.torrents.get(ih)
            if rec is None or refresh_due_at(rec) != due:
                heapq.heappop(hp)
                continue
            return due, ih
        return None

    def next_refresh(self):
        """Torrent whose due time has passed (heap: O(log n)). Each state has its own frequency: alive 6 h, weak 12 h,
        dead 3 days, not measured 30 min with exponential backoff."""
        with self.lock:
            top = self._top_due()
            if top is None or top[0] > time.time():
                return None
            heapq.heappop(self._refresh_heap)      # comes back in when update_health/defer_refresh update the measurement
            return top[1]

    def refresh_due(self):
        with self.lock:
            top = self._top_due()
            return top is not None and top[0] <= time.time()

    def defer_refresh(self, ih):
        """A refresh could not even start: postpone it so it is not retried in a loop."""
        with self.lock:
            rec = self.torrents.get(ih)
            if rec:
                rec["checked_at"] = int(time.time())
                self._push_refresh(ih, rec)

    def mark_failed(self, ih):
        with self.lock:
            h = self.hashes.get(ih)
            if not h:
                return
            now = time.time()
            h["attempts"] = h.get("attempts", 0) + 1
            self.counters["probe_timeouts"] += 1
            # seen only once via sampling or get_peers (someone was LOOKING for it): 1 attempt, nearly all are dead swarms;
            # announced (with or without IP:port) or seen several times: up to MAX_ATTEMPTS, spaced RETRY_DELAY apart
            limit = MAX_ATTEMPTS if (h.get("hits", 1) > 1 or h.get("src") == "announce" or h.get("pe")) else 1
            if h["attempts"] >= limit:
                h["status"] = "failed"
                h["failed_at"] = int(now)
            else:
                h["status"] = "retry"
                self.retry.append((now + RETRY_DELAY, ih))
                self.counters["retries_scheduled"] += 1
                if len(self.retry) > RETRY_MAX:
                    _, old = self.retry.popleft()
                    oh = self.hashes.get(old)
                    if oh and oh.get("status") == "retry":
                        oh["status"], oh["failed_at"] = "failed", int(now)
            self._dirty["hashes"] = True

    # ------------------------------------------------------------- torrents
    def save_torrent(self, ih, info: dict, seeders=0, peers=0, src="none") -> bool:
        """info: name,size,files[(path,size)],piece_length,created,comment,created_by,private,trackers"""
        ih = ih.lower()
        files = [(p, int(s)) for p, s in info["files"]]
        with self.lock:
            if ih in self.torrents:
                return False
            if self._blocked(info["name"], files):
                self.hashes.pop(ih, None)
                self.counters["blocked"] += 1
                self._dirty["hashes"] = True
                return False
            now = int(time.time())
            h = self.hashes.pop(ih, None) or {}
            self.counters["metadata_ok_" + h.get("qk", "other")] += 1      # success per queue (compare with probes_*)
            self._bandit_add(h.get("qk"), 1)
            stored = files[:MAX_FILES_STORED]
            rec = {
                "ih": ih, "name": info["name"], "size": int(info["size"]), "file_count": len(files),
                "files": stored,
                "piece_length": info.get("piece_length", 0), "created": info.get("created", 0),
                "comment": (info.get("comment") or "")[:2000], "created_by": (info.get("created_by") or "")[:100],
                "private": bool(info.get("private")), "trackers": (info.get("trackers") or [])[:20],
                "seeders": 0, "peers": 0, "health_at": now, "health_src": "none", "hh": [],
                "first_seen": h.get("first_seen", now), "last_seen": now, "indexed_at": now, "src": h.get("src", "?"),
                "category": category_of(files, info["name"]), "exts": top_exts(files),
            }
            rec = normalize_record(rec)                    # compresses files/history and adds derived fields
            ih = rec["ih"]                                 # interned str: the same object in torrents, indexes and peers
            if src != "none":
                apply_health(rec, seeders, peers, now, src)
            self.torrents[ih] = rec
            self._index_add(ih, rec)
            self._push_refresh(ih, rec)
            hits = evaluate(self.rules.active(), rec["name"], [p for p, _ in stored])
            if hits:
                self.hidden[ih] = tuple(hits)
            self.counters["torrents_indexed"] += 1
            self.journal.append({"t": "n", "r": dump_rec(rec)})
            self._dirty["hashes"] = True
            return True

    def update_health(self, ih, seeders, peers, src="swarm", nrep=1, detail=None):
        with self.lock:
            rec = self.torrents.get(ih)
            if not rec:
                return
            now = int(time.time())
            apply_health(rec, seeders, peers, now, src, nrep, detail)
            self._push_refresh(ih, rec)
            ev = {"t": "h", "ih": ih, "s": seeders, "p": peers, "a": now, "g": src, "n": nrep}
            if detail:
                ev["d"] = detail
            self.journal.append(ev)

    def get(self, ih):
        with self.lock:
            rec = self.torrents.get(ih.lower())
            if not rec:
                return None
            out = dump_rec(rec)
            out["magnet"] = magnet_of(rec)
            pl = rec.get("piece_length") or 0
            out["num_pieces"] = math.ceil(rec["size"] / pl) if pl else 0
            out["files_truncated"] = rec["file_count"] > len(out["files"])
            out["state"] = health_state(rec)
            out["next_check_at"] = int(refresh_due_at(rec))
            out["hidden_by"] = list(self.hidden.get(rec["ih"], ()))
            return out

    # ------------------------------------------------- hiding (admin panel)
    def _rule_candidates(self, m, big):
        """Superset of the torrents that CAN match a word rule, using the inverted indexes
        (avoids decompressing every file list). None = everything has to be checked."""
        if m.mode != "word":
            return None
        toks = [t for t in m.tokens if t not in _LETTERS]           # the index does not store single letters
        if not toks:
            return None

        def inter(index):
            acc = None
            for t in toks:
                s = index.iter(t)
                if not s:
                    return set()
                acc = set(s) if acc is None else acc.intersection(s)
                if not acc:
                    return set()
            return acc
        out = set()
        if m.scope in ("all", "name"):
            out |= inter(self.name_index)
        if m.scope in ("all", "files"):
            out |= inter(self.file_index) | big                     # the file index covers the first 200 files
        return out

    def recompute_hidden(self):
        """Recomputes which torrents the active rules hide. The scan runs OUTSIDE the lock (crawler and web keep going)."""
        with self._recompute_lock:
            t0 = time.time()
            matchers = self.rules.active()
            with self.lock:
                present = set(self.torrents)
                if not matchers:
                    snap = []
                else:
                    big = {ih for ih, r in self.torrents.items() if r["file_count"] > 200}
                    cands = set()
                    for m in matchers:
                        c = self._rule_candidates(m, big)
                        if c is None:
                            cands = present
                            break
                        cands |= c
                    snap = [(ih, self.torrents[ih]["name"], self.torrents[ih].get("_fz") or b"") for ih in cands if ih in self.torrents]
            new = {}
            for ih, name, fz in snap:
                paths = zlib.decompress(fz).decode("utf-8", "replace").split("\n") if fz else []
                hits = evaluate(matchers, name, paths)
                if hits:
                    new[ih] = tuple(hits)
            with self.lock:
                # whatever got indexed during the scan was already evaluated by save_torrent with these same rules
                for ih, v in self.hidden.items():
                    if ih not in present and ih in self.torrents:
                        new[ih] = v
                self.hidden = new
                self.__dict__.pop("_qcache", None)                   # the search cache might contain hidden torrents
                self._analytics_cache = (0, None)
            took = round(time.time() - t0, 2)
            self.hidden_info = {"at": int(time.time()), "took_s": took, "scanned": len(snap)}
            return {"hidden": len(new), "took_s": took, "scanned": len(snap)}

    def preview_rule(self, matcher, limit=20):
        """What a rule would hide (without saving it)."""
        with self.lock:
            big = {ih for ih, r in self.torrents.items() if r["file_count"] > 200}
            c = self._rule_candidates(matcher, big)
            ihs = list(self.torrents) if c is None else [ih for ih in c if ih in self.torrents]
            snap = [(ih, self.torrents[ih]["name"], self.torrents[ih].get("_fz") or b"", self.torrents[ih]["seeders"]) for ih in ihs]
        hits = []
        for ih, name, fz, sd in snap:
            paths = zlib.decompress(fz).decode("utf-8", "replace").split("\n") if fz else []
            if evaluate([matcher], name, paths):
                hits.append((sd, ih, name))
        hits.sort(reverse=True)
        return {"count": len(hits), "already_hidden": sum(1 for _, ih, _ in hits if ih in self.hidden),
                "sample": [{"ih": ih, "name": n, "seeders": sd} for sd, ih, n in hits[:limit]], "scanned": len(snap)}

    def match_details(self, ih, max_files=8):
        """Where each rule matches in a torrent (name / files), with spans to highlight."""
        with self.lock:
            rec = self.torrents.get(ih)
            if not rec:
                return []
            name, files = rec["name"], files_of(rec)
        ms = {m.id: m for m in self.rules.active()}
        hits = evaluate(list(ms.values()), name, [p for p, _ in files], want_files=True, max_files=max_files)
        out = []
        for rid, h in hits.items():
            m = ms[rid]
            out.append({"rule": rid, "in_name": h["name"],
                        "name_spans": m.spans(name) if h["name"] else [],
                        "files": [{"path": files[i][0], "size": files[i][1], "spans": m.spans(files[i][0])} for i in h["files"]],
                        "files_more": len(h["files"]) >= max_files})
        return out

    def request_refresh(self, ihs):
        """Priority refresh (health + peers) requested from the panel. Returns how many were queued."""
        n = 0
        with self.lock:
            for ih in ihs:
                if len(self._forced) >= FORCED_MAX:
                    break
                if ih in self.torrents and ih not in self._forced_set:
                    self._forced.append(ih)
                    self._forced_set.add(ih)
                    n += 1
        return n

    def next_forced(self):
        with self.lock:
            while self._forced:
                ih = self._forced.popleft()
                self._forced_set.discard(ih)
                if ih in self.torrents:
                    return ih
            return None

    def forced_pending(self):
        return len(self._forced)

    # -------------------------------------------------------------- nodes/peers
    def note_node(self, addr: str):
        self.stats.note_node(addr)
        with self.lock:
            if addr not in self.nodes and len(self.nodes) < MAX_NODES_PERSISTED:
                self.nodes.add(addr)
                self._dirty["nodes"] = True

    def note_peer(self, ip: str):
        self.stats.note_peer(ip)

    # ------------------------------------------------------------- search
    def search(self, params: dict):
        return _search.run(self, params)

    def suggest(self, prefix: str, n=8):
        return _search.suggest(self, prefix, n)

    def related(self, ih: str, n=8):
        return _search.related(self, ih, n)

    # ------------------------------------------------------------- analytics
    def analytics(self):
        now = time.time()
        cached_at, cached = self._analytics_cache
        if cached and now - cached_at < 8:
            return cached
        with self.lock:
            st_count = Counter(h.get("status") for h in self.hashes.values())
            seeds_v = peers_v = dead_v = verified = 0
            seed_b, unver = Counter(), 0
            age, states = Counter(), Counter()
            for r in self.torrents.values():           # a single pass
                if r.get("health_src") == "scrape":
                    verified += 1
                    sd = r["seeders"]
                    seeds_v += sd
                    peers_v += r["peers"]
                    if sd == 0:
                        dead_v += 1
                    seed_b[seed_bucket(sd)] += 1
                else:
                    unver += 1
                states[health_state(r)] += 1
                a = now - r.get("indexed_at", 0)
                for label, lim in AGE_BUCKETS:
                    if a < lim:
                        age[label] += 1
                        break
            hid = self.hidden
            top = heapq.nlargest(8, (r for r in self.torrents.values() if r.get("health_src") == "scrape" and r["ih"] not in hid),
                                 key=lambda r: r["seeders"])
            c = self.counters
            data = {
                "torrents": len(self.torrents), "files": self.total_files, "bytes_indexed": self.total_bytes,
                "hashes_known": len(self.torrents) + len(self.hashes),
                "hashes_discovered": c.get("hashes_new", 0), "hashes_dropped": c.get("stale_dropped", 0),
                "hashes_pending": st_count.get("pending", 0) + st_count.get("probing", 0),
                "hashes_retry": st_count.get("retry", 0), "hashes_failed": st_count.get("failed", 0),
                "seeders_sum": seeds_v, "peers_sum": peers_v, "torrents_no_seeds": dead_v,
                "health_verified": verified, "health_unverified": unver,
                "health_states": [{"name": k, "count": states.get(k, 0)} for k in STATES],
                "seed_buckets": [{"name": label, "count": seed_b.get(label, 0)} for label, _ in SEED_BUCKETS],
                "top_seeded": [{"ih": r["ih"], "name": r["name"], "seeders": r["seeders"], "peers": r["peers"],
                                "size": r["size"], "category": r["category"]} for r in top],
                "categories": [{"name": k, "count": v, "bytes": self.cat_bytes[k]} for k, v in self.cat_counter.most_common()],
                "extensions": [{"name": k, "count": v} for k, v in self.ext_counter.most_common(12)],
                "size_buckets": [{"name": label, "count": self.bucket_counter.get(label, 0)} for label, _ in SIZE_BUCKETS],
                "age_buckets": [{"name": label, "count": age.get(label, 0)} for label, _ in AGE_BUCKETS],
                "sources": [{"name": k, "count": v} for k, v in self.sources.most_common()],
                "counters": dict(c), "first_start": self.first_start, "journal_lines": self.journal.lines,
                "hidden": len(hid),
            }
        snap = self.stats.snapshot()
        data["life"] = snap
        data["nodes_seen"], data["peers_seen"] = snap["nodes_unique"], snap["peers_unique"]
        self._analytics_cache = (now, data)
        return data

    def record_point(self, point: dict):
        self.stats.record_point(point)

    def get_history(self, seconds=3600):
        return self.stats.get_history(seconds)

    # ------------------------------------------------------------ maintenance
    def maintenance(self):
        """Purges old failed hashes, compacts the journal if needed and trims the history. Returns a summary."""
        now = time.time()
        res = {"failed_purged": 0, "compacted": False}
        with self.lock:
            def last(h):
                return max(h.get("failed_at", 0), h.get("last_seen", 0))
            failed = [(last(h), ih) for ih, h in self.hashes.items() if h.get("status") == "failed"]
            drop = [ih for t, ih in failed if t < now - FAILED_TTL]
            dset = set(drop)
            keep = sorted(x for x in failed if x[1] not in dset)
            if len(keep) > FAILED_MAX:
                drop += [ih for _, ih in keep[: len(keep) - FAILED_MAX]]
            for ih in drop:
                del self.hashes[ih]
            if drop:
                self.counters["failed_purged"] += len(drop)
                self._dirty["hashes"] = True
            res["failed_purged"] = len(drop)
            need = self.journal.lines > COMPACT_FACTOR * max(len(self.torrents), 1) + 50_000
        if need:
            self.compact_journal()
            res["compacted"] = True
        res.update(self.peers.maintenance())
        return res

    def compact_journal(self):
        j = self.journal
        j.begin_compaction()
        try:
            with self.lock:
                snap = [dict(r) for r in self.torrents.values()]   # shallow copies (hh/hd are immutable bytes)
            return j.finish_compaction(       # serialization (decompressing files) happens OUTSIDE the lock
                json.dumps({"t": "n", "r": dump_rec(r)}, separators=(",", ":"), ensure_ascii=False) + "\n" for r in snap)
        except Exception:
            j.abort_compaction()
            raise

    # ------------------------------------------------------------ persistence
    def flush(self, force=False):
        now = time.time()
        snap = {}
        with self.lock:
            if force or (self._dirty["hashes"] and now - self._last_flush["hashes"] >= FLUSH_MIN_INTERVAL["hashes"]):
                # PENDING hashes expire within minutes (the most recent are probed): they are only saved on shutdown. What must be
                # kept across restarts are the failed ones (so they are not probed again) and the retries.
                snap["hashes"] = {k: dict(v) for k, v in self.hashes.items() if force or v.get("status") not in ("pending", "probing")}
                self._dirty["hashes"] = False
                self._last_flush["hashes"] = now
            if force or (self._dirty["nodes"] and now - self._last_flush["nodes"] >= FLUSH_MIN_INTERVAL["nodes"]):
                snap["nodes"] = sorted(self.nodes)
                self._dirty["nodes"] = False
                self._last_flush["nodes"] = now
        # disk write happens OUTSIDE the lock: the crawler and the web are not blocked
        if "hashes" in snap:
            atomic_write(os.path.join(self.dir, "hashes.json"), snap["hashes"])
        if "nodes" in snap:
            atomic_write(os.path.join(self.dir, "nodes.json"), snap["nodes"])
        self.journal.flush()
        self.peers.flush()
        self.stats.flush(force)

    def close(self):
        self.flush(force=True)
        self.stats.close()
        self.journal.close()
        self.peers.close()
