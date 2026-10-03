"""
Search engine over the Store's in-memory indexes.

Query syntax (everything can be combined):
  word1 word2                all words (AND); the last one also matches as a prefix (search-as-you-type)
  "exact phrase"             consecutive words, in that order
  a OR b   /   a | b         alternatives
  -word   -"phrase"          exclude
  ext:mkv,mp4   -ext:txt     extension of some file
  cat:video                  category (video, audio, images, docs, software, archives, data, other)
  size>1gb  size<700mb  size:1gb..5gb        total size
  seeders>10  peers>=5  files>100  files:10..50
  age<7d   indexed:24h       indexed less than … ago  (s, min, h, d, w, mo, y)   age>30d = older
  in:name | in:files         where to search the words (name and files by default)
  name:word  file:word       a word only in the name / only in the files
  hash:6f3a…                 infohash (or a prefix of ≥6 hex digits)
  alive:yes | health:dead | health:verified
(Spanish aliases from older versions — tam, semillas, ficheros, edad, nombre, categoria — are still accepted.)
"""
import bisect
import difflib
import heapq
import math
import re
import time
from collections import Counter

from records import STATES, files_of, health_state
from textutil import (AGE_BUCKETS, SIZE_BUCKETS, ALL_CATEGORIES, ext_of, health_brief, index_tokens, magnet_of, norm_map, resolve_cat,
                      size_bucket, tokenize)

_SIZE_UNITS = {"": 1, "b": 1, "k": 1 << 10, "kb": 1 << 10, "kib": 1 << 10, "m": 1 << 20, "mb": 1 << 20, "mib": 1 << 20,
               "g": 1 << 30, "gb": 1 << 30, "gib": 1 << 30, "t": 1 << 40, "tb": 1 << 40, "tib": 1 << 40}
_AGE_UNITS = {"s": 1, "min": 60, "h": 3600, "d": 86400, "w": 7 * 86400, "mo": 30 * 86400, "y": 365 * 86400}
_NUM_RE = re.compile(r"^(\d+(?:[.,]\d+)?)\s*([a-z]*)$", re.I)
_OP_RE = re.compile(r"^(size|tam|tamano|seeders|seeds|semillas|peers|files|ficheros|age|edad)(>=|<=|>|<|=|:)(.+)$", re.I)
_KEY_RE = re.compile(r"^(ext|cat|category|categoria|in|scope|hash|alive|health|indexed|name|file)\:(.*)$", re.I)
_NUM_KEYS = {"size": "size", "tam": "size", "tamano": "size", "seeders": "seeds", "seeds": "seeds", "semillas": "seeds",
             "peers": "peers", "files": "files", "ficheros": "files", "age": "age", "edad": "age"}
INF = float("inf")


# ------------------------------------------------------------------ parser
def _scan(q):
    items, i, n = [], 0, len(q)
    while i < n:
        c = q[i]
        if c.isspace():
            i += 1
            continue
        neg = False
        if c == "-" and i + 1 < n and not q[i + 1].isspace():
            neg, i = True, i + 1
            c = q[i]
        if c == '"':
            j = q.find('"', i + 1)
            j = n if j == -1 else j
            items.append(("phrase", q[i + 1:j], neg))
            i = j + 1
            continue
        j, inq = i, False
        while j < n and (inq or not q[j].isspace()):   # key:"value with spaces"
            if q[j] == '"':
                inq = not inq
            j += 1
        items.append(("word", q[i:j], neg))
        i = j
    return items


def _num(v, units):
    m = _NUM_RE.match(v.strip().strip('"'))
    if not m:
        return None
    x = float(m.group(1).replace(",", "."))
    u = m.group(2).lower()
    if u not in units:
        return None
    return x * units[u]


def _new_filters():
    return {"exts": set(), "not_exts": set(), "cats": set(), "not_cats": set(), "size": [0, INF], "seeds": [0, INF],
            "peers": [0, INF], "files": [0, INF], "age": [0, INF], "scope": "all", "hash": None, "health": None}


def _range(f, key, op, value, units):
    """Applies a numeric operator to the range f[key] = [lo, hi] (inclusive)."""
    rng = f[key]
    if ".." in value and op in (":", "="):
        a, b = value.split("..", 1)
        lo, hi = (_num(a, units) if a else 0), (_num(b, units) if b else INF)
        if lo is None or hi is None:
            return False
        rng[0], rng[1] = max(rng[0], lo), min(rng[1], hi)
        return True
    x = _num(value, units)
    if x is None:
        return False
    step = 1
    if op == ">":
        rng[0] = max(rng[0], x + step)
    elif op == ">=":
        rng[0] = max(rng[0], x)
    elif op == "<":
        rng[1] = min(rng[1], x - step)
    elif op == "<=":
        rng[1] = min(rng[1], x)
    else:                                              # ':' or '='
        rng[0], rng[1] = max(rng[0], x), min(rng[1], x)
    return True


def parse_query(q):
    f = _new_filters()
    groups = [{"pos": [], "phrases": [], "neg": [], "negphrases": []}]
    warnings = []
    for kind, text, neg in _scan(q):
        g = groups[-1]
        if kind == "phrase":
            toks = tokenize(text)
            if len(toks) == 1:
                (g["neg"] if neg else g["pos"]).append((toks[0], None))
            elif toks:
                (g["negphrases"] if neg else g["phrases"]).append(toks)
            continue
        if not neg and text in ("OR", "|"):
            groups.append({"pos": [], "phrases": [], "neg": [], "negphrases": []})
            continue
        m = _OP_RE.match(text)
        if m and not neg:
            key, op, val = _NUM_KEYS[m.group(1).lower()], m.group(2), m.group(3)
            units = _SIZE_UNITS if key == "size" else _AGE_UNITS if key == "age" else {"": 1}
            if key == "age":
                # age<7d = indexed LESS than 7 d ago ; age>7d = MORE than 7 d ago
                if not _range(f, "age", op, val, units):
                    warnings.append(f"Not understood: “{text}”")
            elif not _range(f, key, op, val, units):
                warnings.append(f"Not understood: “{text}”")
            continue
        m = _KEY_RE.match(text)
        if m:
            key, val = m.group(1).lower(), m.group(2).strip().strip('"')
            if key == "ext":
                exts = {e.lower().lstrip(".") for e in re.split(r"[,|]", val) if e}
                f["not_exts" if neg else "exts"] |= exts
            elif key in ("cat", "category", "categoria"):
                for c in re.split(r"[,|]", val):
                    r = resolve_cat(c)
                    if r:
                        f["not_cats" if neg else "cats"].add(r)
                    elif c:
                        warnings.append(f"Unknown category: {c}")
            elif key in ("in", "scope"):
                v = val.lower()
                f["scope"] = "name" if v in ("name", "nombre") else "files" if v in ("files", "file", "ficheros") else "all"
            elif key == "hash":
                h = val.lower()
                if re.fullmatch(r"[0-9a-f]{6,40}", h):
                    f["hash"] = h
                else:
                    warnings.append("hash: needs at least 6 hex characters")
            elif key in ("alive", "health"):
                v = val.lower()
                if key == "alive":
                    f["health"] = "alive" if v in ("yes", "si", "sí", "1", "true") else "dead"
                elif v in ("alive", "weak", "quiet", "dead", "unknown", "verified", "unverified"):
                    f["health"] = v
                else:
                    warnings.append("health: accepts alive, weak, quiet, dead, unknown, verified, unverified")
            elif key == "indexed":
                x = _num(val, _AGE_UNITS)
                if x is not None:
                    f["age"][1] = min(f["age"][1], x)
            elif key in ("name", "file"):
                for t in tokenize(val):
                    (g["neg"] if neg else g["pos"]).append((t, "name" if key == "name" else "files"))
            continue
        for t in tokenize(text):
            if len(t) == 1 and not t.isdigit():
                continue                                # single letters: noise
            (g["neg"] if neg else g["pos"]).append((t, None))
    groups = [g for g in groups if g["pos"] or g["phrases"] or g["neg"] or g["negphrases"]] or [groups[0]]
    return {"groups": groups, "f": f, "warnings": warnings, "q": q}


def apply_params(query, p):
    """Merges the explicit API parameters (options form) with what was typed in the query."""
    f = query["f"]

    def i(name):
        try:
            return int(float(p.get(name)))
        except (TypeError, ValueError):
            return None
    for name, key in (("min_seeds", ("seeds", 0)), ("max_seeds", ("seeds", 1)), ("min_peers", ("peers", 0)),
                      ("max_peers", ("peers", 1)), ("min_size", ("size", 0)), ("max_size", ("size", 1)),
                      ("min_files", ("files", 0)), ("max_files", ("files", 1))):
        v = i(name)
        if v is not None and v >= 0:
            rng = f[key[0]]
            rng[key[1]] = max(rng[0], v) if key[1] == 0 else min(rng[1], v)
    if p.get("cat"):
        for c in re.split(r"[,|]", str(p["cat"])):
            r = resolve_cat(c)
            if r:
                f["cats"].add(r)
    if p.get("ext"):
        f["exts"] |= {e.lower().lstrip(".") for e in re.split(r"[,|\s]", str(p["ext"])) if e}
    if p.get("age"):
        a = str(p["age"])
        x = _num(a, _AGE_UNITS) if not a.isdigit() else float(a)
        if x:
            f["age"][1] = min(f["age"][1], x)
    if p.get("scope") in ("name", "files"):
        f["scope"] = p["scope"]
    if p.get("health") in ("alive", "weak", "quiet", "dead", "unknown", "verified", "unverified"):
        f["health"] = p["health"]
    return query


def describe(query):
    """Applied filters as readable text (shown as chips)."""
    f, out = query["f"], []

    def size(x):
        for u, m in (("TB", 1 << 40), ("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
            if x >= m:
                return f"{x / m:g} {u}"
        return f"{int(x)} B"

    def rng(key, label, fmt=lambda x: f"{int(x):,}"):
        lo, hi = f[key]
        if lo > 0 and hi < INF:
            out.append(f"{label}: {fmt(lo)}–{fmt(hi)}")
        elif lo > 0:
            out.append(f"{label} ≥ {fmt(lo)}")
        elif hi < INF:
            out.append(f"{label} ≤ {fmt(hi)}")
    rng("size", "Size", size)
    rng("seeds", "Seeders")
    rng("peers", "Peers")
    rng("files", "Files")
    lo, hi = f["age"]
    if hi < INF:
        out.append("Indexed less than " + (f"{hi / 86400:g} d" if hi >= 86400 else f"{hi / 3600:g} h") + " ago")
    if lo > 0:
        out.append("Indexed more than " + f"{lo / 86400:g} d ago")
    if f["cats"]:
        out.append("Category: " + ", ".join(sorted(f["cats"])))
    if f["not_cats"]:
        out.append("Not category: " + ", ".join(sorted(f["not_cats"])))
    if f["exts"]:
        out.append("Extension: " + ", ".join("." + e for e in sorted(f["exts"])))
    if f["not_exts"]:
        out.append("Not extension: " + ", ".join("." + e for e in sorted(f["not_exts"])))
    if f["scope"] != "all":
        out.append("Search only in " + ("the name" if f["scope"] == "name" else "the files"))
    if f["health"]:
        out.append("Health: " + {"alive": "alive (has seeders)", "weak": "weak (peers only)", "quiet": "no activity (unconfirmed)",
                                "dead": "dead (confirmed)", "unknown": "not measured", "verified": "verified with a tracker",
                                "unverified": "unverified"}[f["health"]])
    if f["hash"]:
        out.append("Infohash " + f["hash"])
    return out


# --------------------------------------------------------------- indexes
_sorted_cache = {}


def _sorted_keys(d):
    """Sorted list of an index's tokens (for prefix lookups with bisect). Refreshed at most every 30 s."""
    key = id(d)
    now = time.time()
    c = _sorted_cache.get(key)
    if c is None or (c[1] != len(d) and now - c[0] > 30):
        c = (now, len(d), sorted(d))
        _sorted_cache[key] = c
    return c[2]


def _prefix_tokens(d, t, limit=4000):
    keys = _sorted_keys(d)
    i = bisect.bisect_left(keys, t)
    out = []
    while i < len(keys) and keys[i].startswith(t) and len(out) < limit:
        out.append(keys[i])
        i += 1
    return out


def _indexes(store, scope):
    if scope == "name":
        return (store.name_index,)
    if scope == "files":
        return (store.file_index,)
    return (store.name_index, store.file_index)


def _postings(store, t, scope, prefix=False):
    out = set()
    for d in _indexes(store, scope):
        out.update(d.iter(t))
        if prefix and len(t) >= 3:
            for k in _prefix_tokens(d, t):
                if k != t:
                    out.update(d.iter(k))
    return out


def _df(store, t):
    return max(store.name_index.count(t), store.file_index.count(t))


def _norm_text(s):
    return " " + " ".join(tokenize(s)) + " "


def _phrase_hit(rec, phrase, scope):
    needle = " " + " ".join(phrase) + " "
    if scope in ("all", "name") and needle in _norm_text(rec["name"]):
        return True
    if scope in ("all", "files"):
        for path, _ in files_of(rec)[:200]:
            if needle in _norm_text(path):
                return True
    return False


def _group_set(store, g, scope, last_prefix_token):
    """Torrents matching a group (AND words + phrases – exclusions). None = no text restriction."""
    sets = []
    for t, sc in g["pos"]:
        sets.append(_postings(store, t, sc or scope, prefix=(t == last_prefix_token and sc is None)))
    for ph in g["phrases"]:
        for t in ph:
            sets.append(_postings(store, t, scope))
    if not sets:
        if not g["neg"] and not g["negphrases"]:
            return None
        acc = set(store.torrents)
    else:
        sets.sort(key=len)
        acc = set(sets[0])
        for s in sets[1:]:
            acc &= s
            if not acc:
                break
    if g["phrases"] and acc:
        acc = {ih for ih in acc if all(_phrase_hit(store.torrents[ih], ph, scope) for ph in g["phrases"])}
    for t, sc in g["neg"]:
        acc -= _postings(store, t, sc or scope)
    for ph in g["negphrases"]:
        acc = {ih for ih in acc if not _phrase_hit(store.torrents[ih], ph, scope)}
    return acc


def _hl_terms(query):
    terms, seen = [], set()
    for g in query["groups"]:
        for t, _ in g["pos"]:
            if t not in seen:
                seen.add(t)
                terms.append(t)
        for ph in g["phrases"]:
            for t in ph:
                if t not in seen:
                    seen.add(t)
                    terms.append(t)
    return terms


# ------------------------------------------------------------- highlighting
def highlight(text, terms):
    """[[chunk, highlighted(0/1)], …] over the ORIGINAL text (matches ignore case and accents)."""
    if not terms or not text:
        return [[text, 0]]
    n, mp = norm_map(text)
    spans = []
    for t in terms:
        pat = r"(?<![a-z0-9])" + re.escape(t) + ("" if len(t) >= 3 else r"(?![a-z0-9])")
        for m in re.finditer(pat, n):
            a, b = m.start(), m.end()
            while b < len(n) and n[b].isalnum() and len(t) >= 3:
                b += 1
            spans.append((a, b))
    if not spans:
        return [[text, 0]]
    spans.sort()
    merged = [list(spans[0])]
    for a, b in spans[1:]:
        if a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    out, pos = [], 0
    for a, b in merged:
        oa, ob = mp[a], mp[b - 1] + 1
        if oa > pos:
            out.append([text[pos:oa], 0])
        if ob > oa:
            out.append([text[oa:ob], 1])
        pos = max(pos, ob)
    if pos < len(text):
        out.append([text[pos:], 0])
    return out


def _matched_files(rec, terms, limit=3):
    scored = []
    for path, size in files_of(rec)[:500]:
        n = " " + " ".join(tokenize(path))
        hits = sum(1 for t in terms if (" " + t) in n)
        if hits:
            scored.append((-hits, len(path), path, size))
    scored.sort()
    return [{"path": p, "hl": highlight(p, terms), "size": s} for _, _, p, s in scored[:limit]]


# --------------------------------------------------------------- search
def _health_ok(r, h):
    if h == "verified":
        return r.get("health_src") == "scrape"
    if h == "unverified":
        return r.get("health_src") != "scrape"
    return health_state(r) == h


def _is_trivial(q, f):
    return (not q and not f["cats"] and not f["not_cats"] and not f["exts"] and not f["not_exts"] and not f["health"]
            and not f["hash"] and f["scope"] == "all"
            and all(f[k][0] == 0 and f[k][1] == INF for k in ("size", "seeds", "peers", "files", "age")))


def _score(r, terms, idf, phrases_in_name, plain):
    ntoks = tokenize(r["name"])
    nset = set(ntoks)
    s, in_name = 0.0, 0
    for t in terms:
        w = idf[t]
        if t in nset:
            s += 3.0 * w
            in_name += 1
        elif len(t) >= 3 and any(n.startswith(t) for n in nset):
            s += 2.0 * w
            in_name += 1
        else:
            s += 1.0 * w                       # matches only in files
    s += 2.0 * (len(terms) / max(len(ntoks), 1)) + 1.5 * (in_name / max(len(terms), 1))
    if phrases_in_name:
        s += 3.0
    if plain and " ".join(ntoks) == plain:
        s += 6.0
    if r.get("health_src") == "scrape":
        s += 0.6 * math.log10(1 + r["seeders"])
    return s


def _sort_key(name):
    return {
        "seeders": lambda r: (r["seeders"], r["peers"]),
        "peers": lambda r: (r["peers"], r["seeders"]),
        "size": lambda r: r["size"],
        "files": lambda r: r["file_count"],
        "date": lambda r: r.get("indexed_at", 0),
        "created": lambda r: r.get("created", 0),
        "name": lambda r: " ".join(tokenize(r["name"])),
    }.get(name)


def run(store, p):
    t0 = time.perf_counter()
    q = (p.get("q") or "").strip()[:500]
    query = parse_query(q)
    applied_in_query = describe(query)            # only what was typed in the query itself (operators)
    apply_params(query, p)
    f, groups = query["f"], query["groups"]
    try:
        page = max(1, int(p.get("page", 1)))
        per_page = min(100, max(5, int(p.get("per_page", 20))))
    except (TypeError, ValueError):
        page, per_page = 1, 20
    sort = p.get("sort") or "relevance"
    order = p.get("order") or ("asc" if sort == "name" else "desc")
    want_facets = str(p.get("facets", "1")) != "0"
    trivial = _is_trivial(q, f)
    ckey = (sort, order, page, per_page, want_facets)
    if trivial:                                   # home / browsing without filters: short cache (would scan the whole catalogue)
        c = store.__dict__.setdefault("_qcache", {}).get(ckey)
        if c and time.time() - c[0] < 20:
            out = dict(c[1])
            out["took_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            out["cached"] = True
            return out
    terms = _hl_terms(query)
    has_text = any(g["pos"] or g["phrases"] for g in groups)
    now = time.time()

    with store.lock:
        # --- candidates by text
        last_tok = None
        for g in groups[-1:]:
            plain = [t for t, sc in g["pos"] if sc is None]
            last_tok = plain[-1] if plain else None
        if f["hash"]:
            h = f["hash"]
            cands = {h} if len(h) == 40 else {ih for ih in store.torrents if ih.startswith(h)}
            cands &= set(store.torrents)
        else:
            sets = [_group_set(store, g, f["scope"], last_tok) for g in groups]
            cands = None if any(s is None for s in sets) else set().union(*sets)
        if cands is None:
            cands = set(store.torrents)
        hidden = store.__dict__.get("hidden")
        if hidden:                                # admin panel rules: out of public search
            cands.difference_update(hidden)
        # --- extension restriction (index)
        if f["exts"]:
            u = set()
            for e in f["exts"]:
                u.update(store.ext_index.iter(e))
            cands &= u
        for e in f["not_exts"]:
            cands.difference_update(store.ext_index.iter(e))
        # --- field filters (only the active ones are checked)
        recs_nocat, recs = [], []
        T = store.torrents
        checks = [(fld, lo, hi) for fld, (lo, hi) in (("size", f["size"]), ("seeders", f["seeds"]), ("peers", f["peers"]),
                                                       ("file_count", f["files"])) if lo > 0 or hi < INF]
        alo, ahi = f["age"]
        age_on = alo > 0 or ahi < INF
        health, cats, notcats = f["health"], f["cats"], f["not_cats"]
        for ih in cands:
            r = T.get(ih)
            if r is None:
                continue
            ok = True
            for fld, lo, hi in checks:
                v = r[fld]
                if v < lo or v > hi:
                    ok = False
                    break
            if not ok:
                continue
            if age_on:
                a = now - r.get("indexed_at", 0)
                if a < alo or a > ahi:
                    continue
            if health and not _health_ok(r, health):
                continue
            if notcats and r["category"] in notcats:
                continue
            recs_nocat.append(r)
            if not cats or r["category"] in cats:
                recs.append(r)
        total = len(recs)

        # --- orden
        rel = sort == "relevance" and has_text
        if rel:
            if len(recs) > 5000:
                recs.sort(key=lambda r: r["seeders"], reverse=True)
                recs = recs[:5000]
            N = max(len(T), 1)
            idf = {t: math.log(1 + N / (1 + _df(store, t))) for t in terms}
            plain_q = " ".join(t for t, sc in groups[0]["pos"] if sc is None) if len(groups) == 1 else ""
            allph = [ph for g in groups for ph in g["phrases"]]
            scored = []
            for r in recs:
                ph_in_name = bool(allph) and any((" " + " ".join(ph) + " ") in _norm_text(r["name"]) for ph in allph)
                scored.append((_score(r, terms, idf, ph_in_name, plain_q), r["seeders"], r.get("indexed_at", 0), r))
            scored.sort(key=lambda x: x[:3], reverse=(order != "asc"))
            recs = [x[3] for x in scored]
        else:
            key = _sort_key(sort) or _sort_key("seeders")             # orden desconocido -> seeders
            desc = order != "asc"
            k = (page - 1) * per_page + per_page
            if len(recs) > 3000 and k <= 500:                        # only the start is needed: O(n log k)
                recs = heapq.nlargest(k, recs, key=key) if desc else heapq.nsmallest(k, recs, key=key)
            else:
                recs.sort(key=key, reverse=desc)

        # --- results page
        start = (page - 1) * per_page
        results = []
        for r in recs[start:start + per_page]:
            item = {
                "ih": r["ih"], "name": r["name"], "name_hl": highlight(r["name"], terms), "size": r["size"],
                "file_count": r["file_count"], "seeders": r["seeders"], "peers": r["peers"],
                "verified": r.get("health_src") == "scrape", "health_at": r.get("health_at", 0), "state": health_state(r),
                "checked_at": r.get("checked_at", 0), "seed_ok_at": r.get("seed_ok_at", 0), "hd": health_brief(r),
                "category": r["category"], "exts": r.get("exts", [])[:5], "indexed_at": r.get("indexed_at", 0),
                "created": r.get("created", 0), "magnet": magnet_of(r), "private": bool(r.get("private")),
            }
            if terms and f["scope"] != "name":
                nset = set(tokenize(r["name"]))
                if not all(t in nset or any(n.startswith(t) for n in nset) for t in terms):
                    item["matched_files"] = _matched_files(r, terms)
            if not terms:
                item["top_files"] = [x[0] for x in files_of(r)[:3]]
            results.append(item)

        # --- facets (optional: the web only asks for them on the first page)
        facets = None
        if want_facets:
            if trivial and not hidden:            # with hidden torrents, the global counters would include them
                facets = {
                    "categories": [{"name": k, "count": v} for k, v in store.cat_counter.most_common()],
                    "extensions": [{"name": k, "count": v} for k, v in store.ext_counter.most_common(14)],
                    "sizes": [{"name": l, "count": store.bucket_counter.get(l, 0)} for l, _ in SIZE_BUCKETS],
                    "states": store.analytics()["health_states"],
                }
            else:
                cats_c = Counter(r["category"] for r in recs_nocat)
                exts_c, sizes_c, states_c = Counter(), Counter(), Counter()
                for r in recs:
                    sizes_c[r["_sb"]] += 1
                    states_c[health_state(r)] += 1
                    for e in r.get("exts", [])[:6]:
                        exts_c[e] += 1
                facets = {
                    "categories": [{"name": k, "count": v} for k, v in cats_c.most_common()],
                    "extensions": [{"name": k, "count": v} for k, v in exts_c.most_common(14)],
                    "sizes": [{"name": l, "count": sizes_c.get(l, 0)} for l, _ in SIZE_BUCKETS],
                    "states": [{"name": k, "count": states_c.get(k, 0)} for k in STATES],
                }
        suggestion = _did_you_mean(store, q, groups) if total == 0 and has_text else None

    out = {"total": total, "page": page, "per_page": per_page, "pages": max(1, math.ceil(total / per_page)),
           "results": results, "facets": facets, "terms": terms, "applied": applied_in_query, "warnings": query["warnings"],
           "suggestion": suggestion, "sort": sort if (sort != "relevance" or has_text) else "seeders",
           "took_ms": round((time.perf_counter() - t0) * 1000, 1)}
    if trivial:
        qc = store.__dict__.setdefault("_qcache", {})
        if len(qc) > 64:
            qc.clear()
        qc[ckey] = (time.time(), out)
    return out


def _did_you_mean(store, q, groups):
    fixes = {}
    for g in groups:
        for t, sc in g["pos"]:
            if sc or t.isdigit() or len(t) < 4 or _postings(store, t, "all", prefix=True):
                continue
            keys = [k for k in _prefix_tokens(store.name_index, t[0], 20000) if abs(len(k) - len(t)) <= 2]
            best = difflib.get_close_matches(t, keys, n=3, cutoff=0.75)
            hid = store.__dict__.get("hidden") or {}
            best = [k for k in best if not hid or any(ih not in hid for ih in store.name_index.iter(k))]
            if best:
                fixes[t] = max(best, key=lambda k: store.name_index.count(k))
    if not fixes:
        return None
    words = []
    for w in q.split():
        tk = tokenize(w)
        words.append(fixes[tk[0]] if len(tk) == 1 and tk[0] in fixes and not w.startswith("-") else w)
    s = " ".join(words)
    return s if s != q else None


def suggest(store, text, n=8):
    """Autocomplete of the last term: completes the most frequent word starting the same way."""
    text = text.lstrip()
    if not text or text.endswith(" "):
        return []
    head, _, last = text.rpartition(" ")
    tk = tokenize(last)
    if not tk or len(tk[0]) < 2:
        return []
    t = tk[0]
    with store.lock:
        cands = _prefix_tokens(store.name_index, t, 3000)
        ranked = sorted(cands, key=store.name_index.count, reverse=True)
        hid = store.__dict__.get("hidden")
        if hid:                                   # do not suggest (or count) hidden torrents: it would reveal the term
            vis = []
            for k in ranked[: n * 6]:
                c = sum(1 for ih in store.name_index.iter(k) if ih not in hid)
                if c:
                    vis.append((c, k))
            vis.sort(key=lambda x: -x[0])
            out = [{"text": (head + " " if head else "") + k, "count": c} for c, k in vis[:n]]
        else:
            out = [{"text": (head + " " if head else "") + k, "count": store.name_index.count(k)} for k in ranked[:n]]
    return out


def related(store, ih, n=8):
    with store.lock:
        r = store.torrents.get(ih.lower())
        if not r:
            return []
        toks = [t for t in index_tokens(r["name"]) if not t.isdigit() or len(t) == 4]
        N = max(len(store.torrents), 1)
        info = []
        for t in toks:
            df = store.name_index.count(t)
            if 2 <= df <= max(50, N // 20):
                info.append((math.log(1 + N / df), t))
        info.sort(reverse=True)
        info = info[:8]
        score = Counter()
        hid = store.__dict__.get("hidden") or {}
        for w, t in info:
            for other in store.name_index.iter(t):
                if other != r["ih"] and other not in hid:
                    score[other] += w
        top = sorted(score.items(), key=lambda kv: (kv[1], store.torrents[kv[0]]["seeders"]), reverse=True)[:n]
        return [{"ih": o, "name": store.torrents[o]["name"], "size": store.torrents[o]["size"],
                 "seeders": store.torrents[o]["seeders"], "verified": store.torrents[o].get("health_src") == "scrape",
                 "category": store.torrents[o]["category"], "score": round(s, 2)} for o, s in top]
