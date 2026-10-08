"""
Hiding rules (admin panel).

A torrent matching an active rule is NOT deleted: it stays indexed and its health and peers keep being refreshed,
but it does not show up in public search, autocomplete, "related", or the analytics top list,
and its public page returns 404 (the administrator can still see it).

Match modes (always case- and accent-insensitive):
  word       whole word(s): "foo" matches "Foo.Bar" or "foo-2024", not "foobar".
             Several words = that exact sequence ("foo bar" matches "Foo.Bar.mkv").
  substring  the text appears anywhere ("foo" also matches "foobar").
  regex      Python regular expression (tested on the original and on the normalized text).
Scope: torrent name, its file paths, or both.

Stored in data/hidden_rules.json.
"""
import os
import re
import threading
import time
import uuid

import regex

from jsonio import atomic_write, load_json
from textutil import norm, tokenize

SCOPES = ("all", "name", "files")
MODES = ("word", "substring", "regex")
MAX_TERM = 200
MAX_RULES = 500
# Admin regexes run against every indexed name/path: bound each search so a catastrophic pattern
# (ReDoS, e.g. "(a+)+$") cannot stall the server. regex is re-compatible but supports a timeout.
RX_TIMEOUT = 0.05
RX_PROBE_TIMEOUT = 0.25
_RX_PROBES = tuple(c * 5000 + "!" for c in ("a", "1", " ", ".", "-")) + ("ab" * 2500 + "!",)


class RuleError(ValueError):
    pass


def validate(term, scope, mode):
    term = (term or "").strip()
    if not term:
        raise RuleError("Enter a word or some text")
    if len(term) > MAX_TERM:
        raise RuleError(f"At most {MAX_TERM} characters")
    if scope not in SCOPES:
        raise RuleError("Invalid scope")
    if mode not in MODES:
        raise RuleError("Invalid mode")
    if mode == "word" and not tokenize(term):
        raise RuleError("The term has no letters or digits: use the \"contains\" mode or a regular expression")
    if mode == "substring" and not norm(term).strip():
        raise RuleError("Empty term")
    if mode == "regex":
        try:
            rx = regex.compile(term, regex.I)
        except regex.error as e:
            raise RuleError(f"Invalid regular expression: {e}")
        if rx.search(""):
            raise RuleError("The regular expression matches the empty text (it would hide everything)")
        try:
            for probe in _RX_PROBES:
                rx.search(probe, timeout=RX_PROBE_TIMEOUT)
        except TimeoutError:
            raise RuleError("The regular expression is too slow (catastrophic backtracking): simplify it")
    return term


class Matcher:
    """Compiled version of a rule. test(original_text, normalized_text, normalized_tokens) -> bool"""
    __slots__ = ("id", "scope", "mode", "needle", "rx", "tokens", "slow")

    def __init__(self, rule):
        self.id, self.scope, self.mode = rule["id"], rule["scope"], rule["mode"]
        self.rx = None
        self.tokens = []
        self.slow = False
        if self.mode == "word":
            self.tokens = tokenize(rule["term"])
            self.needle = " " + " ".join(self.tokens) + " "
        elif self.mode == "substring":
            self.needle = norm(rule["term"]).strip()
        else:
            self.needle = None
            self.rx = regex.compile(rule["term"], regex.I)

    def test(self, raw, n, toks):
        if self.mode == "word":
            return self.needle in toks
        if self.mode == "substring":
            return self.needle in n
        if self.slow:                                       # timed out once: stop running it
            return False
        try:
            return bool(self.rx.search(raw, timeout=RX_TIMEOUT) or self.rx.search(n, timeout=RX_TIMEOUT))
        except TimeoutError:
            self.slow = True
            print(f"[hiderules] rule {self.id}: regex timed out, disabled until it is edited")
            return False

    def spans(self, raw):
        """Spans [a, b) of the ORIGINAL text that match (for highlighting in the panel)."""
        from textutil import norm_map
        if self.mode == "regex":
            if self.slow:
                return []
            try:
                return [(m.start(), m.end()) for m in self.rx.finditer(raw, timeout=RX_TIMEOUT) if m.end() > m.start()][:20]
            except TimeoutError:
                return []
        n, mp = norm_map(raw)
        out = []
        if self.mode == "substring":
            i = n.find(self.needle)
            while i >= 0 and len(out) < 20:
                out.append((mp[i], mp[i + len(self.needle) - 1] + 1))
                i = n.find(self.needle, i + 1)
            return out
        # word(s): the token sequence separated by non-alphanumerics
        pat = r"(?<![a-z0-9])" + r"[^a-z0-9]+".join(re.escape(t) for t in self.tokens) + r"(?![a-z0-9])"
        for m in re.finditer(pat, n):
            out.append((mp[m.start()], mp[m.end() - 1] + 1))
            if len(out) >= 20:
                break
        return out


def _prep(raw):
    n = norm(raw)
    return raw, n, " " + " ".join(tokenize(n)) + " "


def evaluate(matchers, name, paths, want_files=False, max_files=50):
    """Which rules a torrent matches.
    Returns {rule_id: {"name": bool, "files": [file indexes]}}. Without want_files, stops at the first match of each rule."""
    hits = {}
    if not matchers:
        return hits
    pending = list(matchers)
    name_ms = [m for m in pending if m.scope in ("all", "name")]
    if name_ms:
        pn = _prep(name)
        for m in name_ms:
            if m.test(*pn):
                hits[m.id] = {"name": True, "files": []}
    file_ms = [m for m in pending if m.scope in ("all", "files") and (want_files or m.id not in hits)]
    if file_ms and paths:
        for i, p in enumerate(paths):
            if not file_ms:
                break
            pp = _prep(p)
            keep = []
            for m in file_ms:
                if m.test(*pp):
                    h = hits.setdefault(m.id, {"name": False, "files": []})
                    h["files"].append(i)
                    if want_files and len(h["files"]) < max_files:
                        keep.append(m)
                else:
                    keep.append(m)
            file_ms = keep
    return hits


class RuleBook:
    """Persistent rules + their compiled versions. Thread-safe."""

    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "hidden_rules.json")
        self.lock = threading.Lock()
        raw = load_json(self.path, [])
        self.rules = []
        for r in raw if isinstance(raw, list) else []:
            try:
                r["term"] = validate(r.get("term"), r.get("scope", "all"), r.get("mode", "word"))
                r.setdefault("id", uuid.uuid4().hex[:10])
                r.setdefault("enabled", True)
                r.setdefault("created", int(time.time()))
                self.rules.append(r)
            except RuleError as e:
                print(f"[rules] rule ignored {r!r}: {e}")
        self.matchers = self._compile()

    def _compile(self):
        return [Matcher(r) for r in self.rules if r.get("enabled", True)]

    def _save(self):
        atomic_write(self.path, self.rules)

    def active(self):
        return self.matchers

    def list(self):
        with self.lock:
            return [dict(r) for r in self.rules]

    def get(self, rid):
        with self.lock:
            for r in self.rules:
                if r["id"] == rid:
                    return dict(r)
        return None

    def add(self, term, scope="all", mode="word", note=""):
        term = validate(term, scope, mode)
        with self.lock:
            if len(self.rules) >= MAX_RULES:
                raise RuleError(f"At most {MAX_RULES} rules")
            for r in self.rules:
                if r["term"] == term and r["scope"] == scope and r["mode"] == mode:
                    raise RuleError("An identical rule already exists")
            rule = {"id": uuid.uuid4().hex[:10], "term": term, "scope": scope, "mode": mode, "enabled": True,
                    "created": int(time.time()), "note": (note or "")[:200]}
            self.rules.append(rule)
            self.matchers = self._compile()
            self._save()
            return dict(rule)

    def update(self, rid, **changes):
        with self.lock:
            for r in self.rules:
                if r["id"] == rid:
                    if "enabled" in changes:
                        r["enabled"] = bool(changes["enabled"])
                    if "note" in changes:
                        r["note"] = str(changes["note"] or "")[:200]
                    self.matchers = self._compile()
                    self._save()
                    return dict(r)
        return None

    def delete(self, rid):
        with self.lock:
            n = len(self.rules)
            self.rules = [r for r in self.rules if r["id"] != rid]
            if len(self.rules) == n:
                return False
            self.matchers = self._compile()
            self._save()
            return True
