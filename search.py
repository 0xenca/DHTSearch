"""
Search engine over the Store: an inverted index on disk (segindex.py) for the words, and the hot columns
(colstore.py, numpy) for filters, sorting and facets.

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
import difflib
import math
import re
import time
from collections import Counter

import numpy as np

from colstore import CATEGORY_LIST, EXT_BITS, SRC_LIST, mask_exts
from records import STATES
from textutil import (AGE_BUCKETS, SIZE_BUCKETS, ALL_CATEGORIES, ext_of, health_brief, index_tokens, magnet_of, norm, norm_map, resolve_cat,
                      size_bucket, tokenize)

_SIZE_EDGES = np.array([lim for _, lim in SIZE_BUCKETS[:-1]], np.float64)

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


# --------------------------------------------------------------- index lookups (numpy arrays of document ids)
PHRASE_FILE_CHECKS = 5000        # phrase searches: candidates whose FILES are read from disk to confirm the phrase
RANK_MAX = 5000                  # relevance ranking reads the names of at most this many candidates (the most seeded)
NAME_SORT_MAX = 300_000          # sorting by name reads at most this many names
_E = np.zeros(0, np.int64)


def _i64(a):
    return a.astype(np.int64, copy=False)


def _inter(a, b):
    return np.intersect1d(a, b, assume_unique=True)


class _Lookups:
    """Index lookups of one search, memoized."""

    def __init__(self, store):
        self.ix, self.c, self.n = store.index, {}, {}

    def keys(self, field, t, prefix=False):
        k = (field, t, prefix)
        v = self.c.get(k)
        if v is None:
            key = field + t.encode()
            v = self.c[k] = _i64(self.ix.prefix_postings(key) if prefix else self.ix.postings(key))
        return v

    def count(self, field, t):
        k = (field, t)
        v = self.n.get(k)
        if v is None:
            v = self.n[k] = self.ix.count(field + t.encode())
        return v

    def postings(self, t, scope, prefix=False):
        prefix = prefix and len(t) >= 3
        parts = []
        if scope in ("all", "name"):
            parts.append(self.keys(b"n", t, prefix))
        if scope in ("all", "files"):
            parts.append(self.keys(b"f", t, prefix))
        if not parts:
            return _E
        return parts[0] if len(parts) == 1 else np.union1d(parts[0], parts[1])

    def all_words(self, field, words):
        acc = None
        for w in words:
            p = self.keys(field, w)
            acc = p if acc is None else _inter(acc, p)
            if not len(acc):
                break
        return acc if acc is not None else _E


def _norm_text(s):
    return " " + " ".join(tokenize(s)) + " "


def _group_set(store, g, scope, last_prefix_token, lk, live):
    """Documents matching a group (AND words + phrases – exclusions) -> (sure, maybe). None = no text restriction.
    maybe = {doc: [(phrase needle, must_be_present)]} still to be checked against the FILES (read from disk)."""
    sets = []
    for t, sc in g["pos"]:
        sets.append(lk.postings(t, sc or scope, t == last_prefix_token and sc is None))
    for ph in g["phrases"]:
        for t in ph:
            sets.append(lk.postings(t, scope))
    if not sets:
        if not g["neg"] and not g["negphrases"]:
            return None, {}
        acc = live
    else:
        sets.sort(key=len)
        acc = sets[0]
        for s in sets[1:]:
            acc = _inter(acc, s)
            if not len(acc):
                break
    for t, sc in g["neg"]:
        acc = np.setdiff1d(acc, lk.postings(t, sc or scope), assume_unique=True)
    checks = [(ph, True) for ph in g["phrases"]] + [(ph, False) for ph in g["negphrases"]]
    if not checks or not len(acc):
        return acc, {}
    in_name, in_files = scope in ("all", "name"), scope in ("all", "files")
    maybe = {}
    for words, positive in checks:
        needle = " " + " ".join(words) + " "
        name_hit = _E
        if in_name:                       # only documents whose NAME has every word can have the phrase in the name
            nc = _inter(acc, lk.all_words(b"n", words))
            if len(nc):
                name_hit = np.array([d for d in nc.tolist() if needle in _norm_text(store.name_of(d))], np.int64)
        fc = np.setdiff1d(_inter(acc, lk.all_words(b"f", words)), name_hit, assume_unique=True) if in_files else _E
        for d in fc.tolist():
            maybe.setdefault(d, []).append((needle, positive))
        if positive:
            acc = np.union1d(name_hit, fc)
        else:
            acc = np.setdiff1d(acc, name_hit, assume_unique=True)
    maybe = {d: v for d, v in maybe.items() if v}
    if maybe:
        m = np.fromiter(maybe, np.int64, len(maybe))
        keep = np.isin(m, acc)
        maybe = {d: maybe[d] for d in m[keep].tolist()}
        acc = np.setdiff1d(acc, m, assume_unique=False)
    return acc, maybe


def _check_files(store, maybe, warnings):
    """Confirms phrase checks against the files read from disk. Returns the documents that pass."""
    if not maybe:
        return set()
    ok = set()
    docs = np.fromiter(maybe, np.int64, len(maybe))
    if len(docs) > PHRASE_FILE_CHECKS:                       # the most seeded first
        sd = store.cols.c["seeders"][docs]
        chosen = docs[np.argpartition(-sd.astype(np.int64), PHRASE_FILE_CHECKS)[:PHRASE_FILE_CHECKS]]
        cs = set(chosen.tolist())
        rest = [d for d in maybe if d not in cs]
        # beyond the cap: a document that only had EXCLUSIONS to check is kept (not checked), one that needed a phrase
        # to be present is left out (not confirmed); the warning says which one applies
        ok.update(d for d in rest if all(not p for _, p in maybe[d]))
        if any(p for d in rest for _, p in maybe[d]):
            warnings.append(f"Phrase searched inside the files of the {PHRASE_FILE_CHECKS:,} most seeded candidates only; "
                            "narrow the search to check them all")
        if any(not p for d in rest for _, p in maybe[d]):
            warnings.append(f"Excluded phrase checked inside the files of the {PHRASE_FILE_CHECKS:,} most seeded "
                            "candidates only; less seeded results may still contain it in a file name")
        docs = chosen
    for d, r in store.iter_cold(docs):
        checks = maybe[d]
        found = [False] * len(checks)
        words = [needle.split() for needle, _ in checks]
        for p, _ in r.get("files") or []:
            n = norm(p)
            x = None
            for i, ws in enumerate(words):
                if found[i] or not all(w in n for w in ws):          # cheap substring test before tokenizing
                    continue
                if x is None:
                    x = _norm_text(p)
                if checks[i][0] in x:
                    found[i] = True
            if all(found):
                break
        if all(f == positive for f, (_, positive) in zip(found, checks)):
            ok.add(d)
    return ok


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


def _matched_files(files, terms, limit=3):
    scored = []
    for path, size in files[:500]:
        n = " " + " ".join(tokenize(path))
        hits = sum(1 for t in terms if (" " + t) in n)
        if hits:
            scored.append((-hits, len(path), path, size))
    scored.sort()
    return [{"path": p, "hl": highlight(p, terms), "size": s} for _, _, p, s in scored[:limit]]


# --------------------------------------------------------------- search
def _is_trivial(q, f):
    return (not q and not f["cats"] and not f["not_cats"] and not f["exts"] and not f["not_exts"] and not f["health"]
            and not f["hash"] and f["scope"] == "all"
            and all(f[k][0] == 0 and f[k][1] == INF for k in ("size", "seeds", "peers", "files", "age")))


def _score(name, seeders, verified, terms, idf, phrases_in_name, plain):
    ntoks = tokenize(name)
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
    if verified:
        s += 0.6 * math.log10(1 + seeders)
    return s


def _hash_docs(store, h, live):
    if len(h) == 40:
        d = store.doc(h)
        return np.array([d], np.int64) if d >= 0 else _E
    ih = store.cols.ih[: store.cols.n]
    k = len(h) // 2
    m = np.all(ih[:, :k] == np.frombuffer(bytes.fromhex(h[: 2 * k]), np.uint8), axis=1) if k else np.ones(len(ih), bool)
    if len(h) % 2:
        m &= (ih[:, k] >> 4) == int(h[-1], 16)
    return _inter(np.flatnonzero(m).astype(np.int64), live)


def _top(keys, k, desc):
    """Indexes of the k first rows by keys (list of arrays, most significant LAST, like np.lexsort)."""
    n = len(keys[0])
    if n == 0:
        return np.zeros(0, np.int64)
    order = np.lexsort([(-x.astype(np.float64) if desc else x) for x in keys])
    return order[:k]


def run(store, p):
    t0 = time.perf_counter()
    q = (p.get("q") or "").strip()[:500]
    query = parse_query(q)
    applied_in_query = describe(query)            # only what was typed in the query itself (operators)
    apply_params(query, p)
    f, groups = query["f"], query["groups"]
    warnings = query["warnings"]
    try:
        page = max(1, int(p.get("page", 1)))
        per_page = min(100, max(5, int(p.get("per_page", 20))))
    except (TypeError, ValueError):
        page, per_page = 1, 20
    sort = p.get("sort") or "relevance"
    order = p.get("order") or ("asc" if sort == "name" else "desc")
    desc = order != "asc"
    want_facets = str(p.get("facets", "1")) != "0"
    trivial = _is_trivial(q, f)
    ckey = (sort, order, page, per_page, want_facets)
    if trivial:                                   # home / browsing without filters: short cache
        c = store.__dict__.setdefault("_qcache", {}).get(ckey)
        if c and time.time() - c[0] < 20:
            out = dict(c[1])
            out["took_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            out["cached"] = True
            return out
    terms = _hl_terms(query)
    has_text = any(g["pos"] or g["phrases"] for g in groups)
    now = time.time()
    last_tok = None
    for g in groups[-1:]:
        plain = [t for t, sc in g["pos"] if sc is None]
        last_tok = plain[-1] if plain else None
    cols = store.cols
    C = cols.c
    N = cols.n
    live = cols.live(N)
    if (has_text or f["exts"] or f["not_exts"] or any(g["neg"] or g["negphrases"] for g in groups)) and not store.index_sync():
        warnings.append("The search index is still being built: some matches may be missing")

    # --- 1) candidates by text (index on disk; phrases inside file names are confirmed reading the files)
    lk = _Lookups(store)
    if f["hash"]:
        cands = _hash_docs(store, f["hash"], live)
    else:
        res = [_group_set(store, g, f["scope"], last_tok, lk, live) for g in groups]
        if any(s is None for s, _ in res):
            cands = None
        else:
            cands = res[0][0] if len(res) == 1 else np.unique(np.concatenate([s for s, _ in res]))
            maybe = {}
            have = set()
            for _, m in res:
                for d, chk in m.items():
                    maybe.setdefault(d, []).append(chk)
            if maybe:                                 # OR of groups: a document passes if ANY of its groups passes
                passed = set()
                for k in range(max(len(a) for a in maybe.values())):
                    sub = {d: alts[k] for d, alts in maybe.items() if k < len(alts) and d not in passed}
                    passed |= _check_files(store, sub, warnings)
                if passed:
                    cands = np.union1d(cands, np.fromiter(passed, np.int64, len(passed)))
    if cands is None:
        cands = live
    else:
        cands = _inter(_i64(cands), live)
    hidden = store.hidden_docs()
    if len(hidden):                                  # admin panel rules: out of public search
        cands = np.setdiff1d(cands, hidden, assume_unique=True)
    # --- extension restriction (index)
    if f["exts"]:
        u = np.unique(np.concatenate([lk.keys(b"e", e) for e in f["exts"]])) if f["exts"] else _E
        cands = _inter(cands, u)
    for e in f["not_exts"]:
        cands = np.setdiff1d(cands, lk.keys(b"e", e), assume_unique=True)

    # --- 2) field filters, vectorized
    m = np.ones(len(cands), bool)
    for col, (lo, hi) in (("size", f["size"]), ("seeders", f["seeds"]), ("peers", f["peers"]), ("file_count", f["files"])):
        if lo > 0 or hi < INF:
            v = C[col][cands].astype(np.float64)
            m &= (v >= lo) & (v <= hi)
    alo, ahi = f["age"]
    if alo > 0 or ahi < INF:
        a = now - C["indexed_at"][cands].astype(np.float64)
        m &= (a >= alo) & (a <= ahi)
    h = f["health"]
    if h == "verified":
        m &= C["src"][cands] == SRC_LIST.index("scrape")
    elif h == "unverified":
        m &= C["src"][cands] != SRC_LIST.index("scrape")
    elif h:
        m &= store.states(cands) == STATES.index(h)
    if f["not_cats"]:
        m &= ~np.isin(C["cat"][cands], [CATEGORY_LIST.index(c) for c in f["not_cats"] if c in CATEGORY_LIST])
    recs_nocat = cands[m]
    recs = recs_nocat
    if f["cats"]:
        recs = recs_nocat[np.isin(C["cat"][recs_nocat], [CATEGORY_LIST.index(c) for c in f["cats"] if c in CATEGORY_LIST])]
    total = len(recs)

    # --- 3) sorting: only what the page needs
    start = (page - 1) * per_page
    need = start + per_page
    rel = sort == "relevance" and has_text
    if rel:
        pool = recs
        if len(pool) > RANK_MAX:
            pool = pool[np.argpartition(-C["seeders"][pool].astype(np.int64), RANK_MAX)[:RANK_MAX]]
        idf = {t: math.log(1 + max(len(live), 1) / (1 + max(lk.count(b"n", t), lk.count(b"f", t)))) for t in terms}
        plain_q = " ".join(t for t, sc in groups[0]["pos"] if sc is None) if len(groups) == 1 else ""
        allph = [" " + " ".join(ph) + " " for g in groups for ph in g["phrases"]]
        sc_src = SRC_LIST.index("scrape")
        scored = []
        for d in pool.tolist():
            name = store.name_of(d)
            ph_in = bool(allph) and any(x in _norm_text(name) for x in allph)
            sd = int(C["seeders"][d])
            scored.append((_score(name, sd, C["src"][d] == sc_src, terms, idf, ph_in, plain_q), sd, int(C["indexed_at"][d]), d))
        scored.sort(key=lambda x: x[:3], reverse=desc)
        page_docs = [x[3] for x in scored[start:need]]
    elif sort == "name":
        pool = recs
        if len(pool) > NAME_SORT_MAX:
            pool = pool[np.argpartition(-C["seeders"][pool].astype(np.int64), NAME_SORT_MAX)[:NAME_SORT_MAX]]
            warnings.append(f"Sorted by name among the {NAME_SORT_MAX:,} most seeded results")
        keyed = sorted(((" ".join(tokenize(store.name_of(d))), d) for d in pool.tolist()), reverse=desc)
        page_docs = [d for _, d in keyed[start:need]]
    else:
        keys = {"seeders": ("peers", "seeders"), "peers": ("seeders", "peers"), "size": ("size",), "files": ("file_count",),
                "date": ("indexed_at",), "created": ("created",)}.get(sort, ("peers", "seeders"))
        if len(recs) > 4 * need and need <= 2000:          # only the start is needed: partial selection first
            main = C[keys[-1]][recs].astype(np.float64)
            kth = need - 1
            v = (-np.partition(-main, kth)[kth]) if desc else np.partition(main, kth)[kth]
            pool = recs[main >= v] if desc else recs[main <= v]     # every tie of the cut stays in (exact order)
        else:
            pool = recs
        o = _top([C[k][pool] for k in keys], need, desc)
        page_docs = pool[o][start:need].tolist()

    # --- 4) results page (hot fields from the columns, cold ones from disk)
    st = store.states(np.array(page_docs, np.int64)) if page_docs else []
    results = []
    sc_src = SRC_LIST.index("scrape")
    for i, d in enumerate(page_docs):
        name = store.name_of(d)
        cold = store.cold(d)
        ih = cols.ih_hex(d)
        view = {"ih": ih, "name": name, "hd": cold.get("hd"), "trackers": cold.get("trackers")}
        item = {
            "ih": ih, "name": name, "name_hl": highlight(name, terms), "size": int(C["size"][d]),
            "file_count": int(C["file_count"][d]), "seeders": int(C["seeders"][d]), "peers": int(C["peers"][d]),
            "verified": bool(C["src"][d] == sc_src), "health_at": int(C["health_at"][d]), "state": STATES[int(st[i])],
            "checked_at": int(C["checked_at"][d]), "seed_ok_at": int(C["seed_ok_at"][d]), "hd": health_brief(view),
            "category": CATEGORY_LIST[int(C["cat"][d])], "exts": (cold.get("exts") if isinstance(cold.get("exts"), list) else mask_exts(int(C["exts"][d])))[:5],
            "indexed_at": int(C["indexed_at"][d]), "created": int(C["created"][d]), "magnet": magnet_of(view),
            "private": bool(C["flags"][d] & 1),
        }
        files = [tuple(x) for x in cold.get("files") or []]
        if terms and f["scope"] != "name":
            nset = set(tokenize(name))
            if not all(t in nset or any(n.startswith(t) for n in nset) for t in terms):
                item["matched_files"] = _matched_files(files, terms)
        if not terms:
            item["top_files"] = [x[0] for x in files[:3]]
        results.append(item)

    # --- 5) facets (optional: the web only asks for them on the first page)
    facets = None
    if want_facets:
        cats_c = np.bincount(C["cat"][recs_nocat], minlength=len(CATEGORY_LIST))
        sizes_c = np.bincount(np.searchsorted(_SIZE_EDGES, C["size"][recs].astype(np.float64), side="right"),
                              minlength=len(SIZE_BUCKETS))
        states_c = np.bincount(store.states(recs), minlength=len(STATES))
        em = C["exts"][recs]
        exts_c = Counter({e: int(np.count_nonzero(em & np.uint32(1 << i))) for i, e in enumerate(EXT_BITS)})
        exts_c = Counter({e: c for e, c in exts_c.items() if c})
        facets = {
            "categories": sorted(({"name": CATEGORY_LIST[i], "count": int(cats_c[i])} for i in range(len(CATEGORY_LIST))
                                  if cats_c[i]), key=lambda x: -x["count"]),
            "extensions": [{"name": k, "count": v} for k, v in exts_c.most_common(14)],
            "sizes": [{"name": l, "count": int(sizes_c[i])} for i, (l, _) in enumerate(SIZE_BUCKETS)],
            "states": [{"name": k, "count": int(states_c[i])} for i, k in enumerate(STATES)],
        }
    suggestion = _did_you_mean(store, q, groups, lk) if total == 0 and has_text else None

    out = {"total": int(total), "page": page, "per_page": per_page, "pages": max(1, math.ceil(total / per_page)),
           "results": results, "facets": facets, "terms": terms, "applied": applied_in_query, "warnings": list(dict.fromkeys(warnings)),
           "suggestion": suggestion, "sort": sort if (sort != "relevance" or has_text) else "seeders",
           "took_ms": round((time.perf_counter() - t0) * 1000, 1)}
    if trivial:
        qc = store.__dict__.setdefault("_qcache", {})
        if len(qc) > 64:
            qc.clear()
        qc[ckey] = (time.time(), out)
    return out


def _visible_count(store, key):
    """Documents with this key that are not hidden."""
    hid = store.hidden_docs()
    p = store.index.postings(key)
    return int(len(p) - np.isin(p, hid).sum()) if len(hid) else int(len(p))


def _did_you_mean(store, q, groups, lk):
    fixes = {}
    hid = store.hidden_docs()
    for g in groups:
        for t, sc in g["pos"]:
            if sc or t.isdigit() or len(t) < 4 or len(lk.postings(t, "all", True)):
                continue
            keys = store.index.keys_with_prefix(b"n" + t[0].encode(), 20000)
            words = {k[1:].decode(): c for k, c in keys.items() if abs(len(k) - 1 - len(t)) <= 2}
            best = difflib.get_close_matches(t, list(words), n=3, cutoff=0.75)
            if len(hid):
                best = [k for k in best if _visible_count(store, b"n" + k.encode())]
            if best:
                fixes[t] = max(best, key=lambda k: words[k])
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
    keys = store.index.keys_with_prefix(b"n" + tk[0].encode(), 3000)
    ranked = sorted(keys.items(), key=lambda kv: -kv[1])
    if len(store.hidden_docs()):                 # do not suggest (or count) hidden torrents: it would reveal the term
        vis = [(_visible_count(store, k), k) for k, _ in ranked[: n * 6]]
        ranked = [(k, c) for c, k in sorted(vis, key=lambda x: -x[0]) if c]
    return [{"text": (head + " " if head else "") + k[1:].decode(), "count": c} for k, c in ranked[:n]]


def related(store, ih, n=8):
    d0 = store.doc(ih)
    if d0 < 0:
        return []
    C = store.cols.c
    toks = [t for t in index_tokens(store.name_of(d0)) if not t.isdigit() or len(t) == 4]
    N = max(store.live_count, 1)
    info = []
    for t in toks:
        df = store.index.count(b"n" + t.encode())
        if 2 <= df <= max(50, N // 20):
            info.append((math.log(1 + N / df), t))
    info.sort(reverse=True)
    info = info[:8]
    if not info:
        return []
    parts = [_i64(store.index.postings(b"n" + t.encode())) for _, t in info]
    weights = np.concatenate([np.full(len(p), w) for (w, _), p in zip(info, parts)])
    docs = np.concatenate(parts)
    uniq, inv = np.unique(docs, return_inverse=True)
    score = np.bincount(inv, weights=weights)
    keep = (uniq != d0) & ((C["flags"][uniq] & 2) == 0)
    hid = store.hidden_docs()
    if len(hid):
        keep &= ~np.isin(uniq, hid)
    uniq, score = uniq[keep], score[keep]
    order = np.lexsort((-C["seeders"][uniq].astype(np.int64), -score))[:n]
    sc = SRC_LIST.index("scrape")
    return [{"ih": store.cols.ih_hex(d), "name": store.name_of(d), "size": int(C["size"][d]), "seeders": int(C["seeders"][d]),
             "verified": bool(C["src"][d] == sc), "category": CATEGORY_LIST[int(C["cat"][d])], "score": round(float(score[i]), 2)}
            for i, d in zip(order.tolist(), uniq[order].tolist())]


def health_state_of(store, d):
    return STATES[int(store.states(np.array([d], np.int64))[0])]
