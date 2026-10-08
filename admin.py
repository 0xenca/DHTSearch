"""
Admin panel API (/api/admin/*).

Access: password checked ON THE SERVER (--admin-password or the TS_ADMIN_PASSWORD variable; "admin" by default)
-> signed session cookie (HttpOnly, SameSite=Strict, 12 h). The signing key is generated once in data/admin_secret.key.
Changing the password invalidates open sessions. 5 failed attempts per IP every 5 min; after that, 429.
Requests that change anything require JSON (a form on another site cannot send it without a CORS preflight).
"""
import hashlib
import hmac
import ipaddress
import os
import threading
import time
from collections import deque
from datetime import timedelta

from flask import Blueprint, jsonify, request, session

from hiderules import MODES, SCOPES, Matcher, RuleError, validate
from peers import parse_ip_specs
from records import health_state
from textutil import magnet_of

bp = Blueprint("admin", __name__, url_prefix="/api/admin")
_CFG = {"store": None, "pwd": "admin", "pwd_tag": ""}
_FAILS = {}
_FAILS_LOCK = threading.Lock()
MAX_FAILS, FAIL_WINDOW = 5, 300


def init(app, get_store, password, data_dir, get_crawler=None, get_ai=None):
    """Registers the blueprint. get_store / get_crawler / get_ai: callables returning the Store, the crawler and the
    AI moderator."""
    key_path = os.path.join(data_dir, "admin_secret.key")
    try:
        with open(key_path, "rb") as f:
            key = f.read()
        if len(key) < 32:
            raise FileNotFoundError
    except FileNotFoundError:
        key = os.urandom(48)
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
    app.secret_key = key
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
                      SESSION_COOKIE_NAME="ts_admin", PERMANENT_SESSION_LIFETIME=timedelta(hours=12))
    _CFG["get_store"] = get_store
    _CFG["get_crawler"] = get_crawler or (lambda: None)
    _CFG["get_ai"] = get_ai or (lambda: None)
    _CFG["pwd"] = password
    _CFG["pwd_tag"] = hashlib.sha256(b"ts-admin:" + password.encode()).hexdigest()[:16]
    app.register_blueprint(bp)


def is_admin():
    return session.get("admin") is True and session.get("tag") == _CFG["pwd_tag"]


def _store():
    return _CFG["get_store"]()


@bp.before_request
def _guard():
    if request.endpoint in ("admin.login", "admin.me"):
        return None
    if not is_admin():
        return jsonify({"error": "unauthorized"}), 401
    if request.method in ("POST", "PATCH", "DELETE") and not request.is_json:
        return jsonify({"error": "JSON expected"}), 415
    return None


# ------------------------------------------------------------------ session
@bp.post("/login")
def login():
    ip = request.remote_addr or "?"
    now = time.time()
    with _FAILS_LOCK:
        dq = _FAILS.setdefault(ip, deque())
        while dq and dq[0] < now - FAIL_WINDOW:
            dq.popleft()
        if len(dq) >= MAX_FAILS:
            return jsonify({"error": f"Too many attempts. Wait {int(dq[0] + FAIL_WINDOW - now) // 60 + 1} min."}), 429
    if not request.is_json:
        return jsonify({"error": "JSON expected"}), 415
    pwd = str((request.get_json(silent=True) or {}).get("password", ""))
    if not hmac.compare_digest(pwd.encode(), _CFG["pwd"].encode()):
        with _FAILS_LOCK:
            _FAILS[ip].append(now)
        time.sleep(0.4)                                     # slows down brute force
        return jsonify({"error": "Wrong password"}), 403
    with _FAILS_LOCK:
        _FAILS.pop(ip, None)
    session.clear()
    session.permanent = True
    session["admin"] = True
    session["tag"] = _CFG["pwd_tag"]
    return jsonify({"ok": True})


@bp.post("/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@bp.get("/me")
def me():
    return jsonify({"admin": is_admin()})


# ------------------------------------------------------------------ status
def _rule_counts(st):
    c = {}
    for rids in list(st.hidden.values()):
        for r in rids:
            c[r] = c.get(r, 0) + 1
    return c


@bp.get("/status")
def status():
    st = _store()
    return jsonify({"hidden": len(st.hidden), "torrents": len(st.torrents), "rules": len(st.rules.list()),
                    "recompute": st.hidden_info, "peers": st.peers.summary(), "forced_pending": st.forced_pending(),
                    "default_password": _CFG["pwd"] == "admin", "trackers": _tracker_report()})


def _tracker_report():
    cr = _CFG["get_crawler"]()
    fn = getattr(cr, "tracker_report", None)
    try:
        return fn() if fn else []
    except Exception:
        return []


# ------------------------------------------------------------ live verification
_VERIFY = {}                     # id -> {"ih", "state": running|done|error, "log": [...], "result": {...}}
_VERIFY_LOCK = threading.Lock()
VERIFY_MAX_RUNNING, VERIFY_KEEP = 2, 30


@bp.post("/verify")
def verify_start():
    """INDEPENDENT check (verify.py, no libtorrent): scrape + announce to every tracker, DHT, and a handshake with every peer."""
    import verify as V
    st = _store()
    ih = str((request.get_json(silent=True) or {}).get("ih", "")).lower()
    rec = st.get(ih) if len(ih) == 40 else None
    if not rec:
        return jsonify({"error": "not found"}), 404
    cr = _CFG["get_crawler"]()
    cfg = getattr(cr, "cfg", {}) or {}
    trackers = list(cfg.get("trackers") or V.DEFAULT_TRACKERS)
    trackers += [t["u"] for t in (rec.get("hd") or {}).get("tr") or [] if t.get("u")]
    trackers += list(rec.get("trackers") or [])
    trackers = [t for t in dict.fromkeys(trackers) if t.startswith(("udp://", "http://", "https://"))][:30]
    with _VERIFY_LOCK:
        if sum(1 for v in _VERIFY.values() if v["state"] == "running") >= VERIFY_MAX_RUNNING:
            return jsonify({"error": "verifications already running; wait for them to finish"}), 429
        vid = os.urandom(6).hex()
        job = _VERIFY[vid] = {"id": vid, "ih": ih, "state": "running", "log": [], "started": int(time.time()), "result": None}
        for old in sorted(_VERIFY, key=lambda k: _VERIFY[k]["started"])[:-VERIFY_KEEP]:
            if _VERIFY[old]["state"] != "running":
                del _VERIFY[old]

    def run():
        try:
            rep = V.verify(ih, trackers, num_pieces=rec.get("num_pieces") or None, size=rec.get("size") or 1,
                           port=int(cfg.get("port", 6881)), progress=lambda t: job["log"].append(t))
            rep["verdict"] = V.verdict(rep)
            rep["peers"] = rep["peers"][:300]
            job["result"], job["state"] = rep, "done"
        except Exception as e:                          # must never bring the server down
            job["log"].append(f"error: {type(e).__name__}: {e}")
            job["state"] = "error"
    threading.Thread(target=run, daemon=True, name="verify-" + vid).start()
    return jsonify({"id": vid, "trackers": trackers})


@bp.get("/verify/<vid>")
def verify_get(vid):
    job = _VERIFY.get(vid)
    if not job:
        return jsonify({"error": "does not exist"}), 404
    return jsonify(job)


# ------------------------------------------------------------------ rules
@bp.get("/rules")
def rules_list():
    st = _store()
    counts = _rule_counts(st)
    rules = st.rules.list()
    for r in rules:
        r["count"] = counts.get(r["id"], 0)
    return jsonify({"rules": rules, "hidden": len(st.hidden), "recompute": st.hidden_info})


def _rule_input():
    b = request.get_json(silent=True) or {}
    scope = b.get("scope") or "all"
    mode = b.get("mode") or "word"
    term = validate(b.get("term"), scope, mode)
    return term, scope, mode, str(b.get("note") or "")


@bp.post("/rules/preview")
def rules_preview():
    try:
        term, scope, mode, _ = _rule_input()
    except RuleError as e:
        return jsonify({"error": str(e)}), 400
    m = Matcher({"id": "preview", "term": term, "scope": scope, "mode": mode})
    return jsonify(_store().preview_rule(m))


@bp.post("/rules")
def rules_add():
    st = _store()
    try:
        term, scope, mode, note = _rule_input()
        rule = st.rules.add(term, scope, mode, note)
    except RuleError as e:
        return jsonify({"error": str(e)}), 400
    res = st.recompute_hidden()
    rule["count"] = _rule_counts(st).get(rule["id"], 0)
    return jsonify({"rule": rule, **res})


@bp.patch("/rules/<rid>")
def rules_update(rid):
    st = _store()
    b = request.get_json(silent=True) or {}
    changes = {k: b[k] for k in ("enabled", "note") if k in b}
    rule = st.rules.update(rid, **changes)
    if not rule:
        return jsonify({"error": "does not exist"}), 404
    res = st.recompute_hidden() if "enabled" in changes else {}
    return jsonify({"rule": rule, **res})


@bp.delete("/rules/<rid>")
def rules_delete(rid):
    st = _store()
    if not st.rules.delete(rid):
        return jsonify({"error": "does not exist"}), 404
    return jsonify({"ok": True, **st.recompute_hidden()})


# ------------------------------------------------------------ hidden content
def _brief(st, r):
    return {"ih": r["ih"], "name": r["name"], "size": r["size"], "file_count": r["file_count"], "seeders": r["seeders"],
            "peers": r["peers"], "verified": r.get("health_src") == "scrape", "state": health_state(r),
            "category": r["category"], "indexed_at": r.get("indexed_at", 0), "health_at": r.get("health_at", 0),
            "known_peers": st.peers.count(r["ih"]), "hidden_by": list(st.hidden.get(r["ih"], ()))}


def _hidden_ihs(st, rule=None):
    return [ih for ih, rids in list(st.hidden.items()) if not rule or rule in rids]


@bp.get("/hidden")
def hidden_list():
    st = _store()
    rule = request.args.get("rule") or None
    q = (request.args.get("q") or "").strip().lower()
    sort = request.args.get("sort") or "seeders"
    try:
        page = max(1, int(request.args.get("page", 1)))
        per = min(100, max(5, int(request.args.get("per_page", 20))))
    except ValueError:
        page, per = 1, 20
    with st.lock:
        recs = [st.torrents[ih] for ih in _hidden_ihs(st, rule) if ih in st.torrents]
        if q:
            recs = [r for r in recs if q in r["name"].lower() or q == r["ih"][: len(q)]]
        key = {"seeders": lambda r: (r["seeders"], r["peers"]), "date": lambda r: r.get("indexed_at", 0),
               "size": lambda r: r["size"], "name": lambda r: r["name"].lower(),
               "peers_known": lambda r: st.peers.count(r["ih"])}.get(sort, lambda r: r["seeders"])
        recs.sort(key=key, reverse=sort != "name")
        total = len(recs)
        items = [_brief(st, r) for r in recs[(page - 1) * per: page * per]]
    for it in items:                                        # where it matches (outside the lock: decompresses files)
        it["matches"] = st.match_details(it["ih"], max_files=5)
    return jsonify({"total": total, "page": page, "per_page": per, "pages": max(1, -(-total // per)), "results": items})


@bp.get("/torrent/<ih>")
def torrent(ih):
    st = _store()
    ih = ih.lower()
    rec = st.get(ih)
    if not rec:
        return jsonify({"error": "not found"}), 404
    return jsonify({"torrent": {k: rec[k] for k in ("ih", "name", "size", "file_count", "seeders", "peers", "state", "category")},
                    "hidden_by": rec.get("hidden_by", []), "matches": st.match_details(ih, max_files=50),
                    "peers": st.peers.peers_of(ih)})


@bp.post("/refresh")
def refresh():
    """Asks the crawler to probe NOW (health + peers) the given torrents, or those hidden by a rule."""
    st = _store()
    b = request.get_json(silent=True) or {}
    ihs = [str(x).lower() for x in (b.get("ihs") or [])][:1000]
    if not ihs:
        ihs = _hidden_ihs(st, b.get("rule") or None)
        with st.lock:                                       # those most likely to answer first
            ihs.sort(key=lambda ih: st.torrents[ih]["seeders"] if ih in st.torrents else -1, reverse=True)
    n = st.request_refresh(ihs)
    return jsonify({"queued": n, "pending": st.forced_pending()})


# ------------------------------------------------------------------ peers
@bp.get("/peers")
def peers_of_hidden():
    """Peers/seeders seen in hidden torrents (all, or those of one rule), aggregated by IP."""
    st = _store()
    rule = request.args.get("rule") or None
    only_seeds = request.args.get("seeds") == "1"
    only_else = request.args.get("elsewhere") == "1"
    ihs = _hidden_ihs(st, rule)
    rows = st.peers.aggregate(ihs, hidden=st.hidden)
    if only_seeds:
        rows = [r for r in rows if r["role"] == 1]
    if only_else:
        rows = [r for r in rows if r["elsewhere"] > 0]
    rows.sort(key=lambda r: (-r["in_set"], -r["seed_in"], -r["elsewhere"], -r["last"]))
    with_peers = sum(1 for ih in ihs if st.peers.count(ih))
    return jsonify({"torrents": len(ihs), "torrents_with_peers": with_peers, "total": len(rows), "results": rows[:2000],
                    "truncated": len(rows) > 2000, "peers_enabled": st.peers.enabled})


@bp.route("/peer-search", methods=["GET", "POST"])
def peer_search():
    """In which other torrents do these IPs appear? Hidden ones are excluded by default (already covered by the block)."""
    st = _store()
    b = request.get_json(silent=True) or {} if request.method == "POST" else request.args
    only_seeds = str(b.get("seeds", "")) in ("1", "true", "True")
    include_hidden = str(b.get("include_hidden", "")) in ("1", "true", "True")
    from_rule = b.get("from_rule")
    from_seeds = str(b.get("from_seeds", "")) in ("1", "true", "True")
    bad = []
    if from_rule is not None:                              # "all peers of the hidden content" (of one rule or all)
        rows = st.peers.aggregate(_hidden_ihs(st, from_rule or None), hidden=st.hidden)
        specs = [ipaddress.ip_address(r["ip"]) for r in rows if not from_seeds or r["role"] == 1]
    else:
        specs, bad = parse_ip_specs(str(b.get("ips", "")))
    if not specs:
        return jsonify({"error": "Enter at least one valid IP or network (e.g. 203.0.113.7 or 203.0.113.0/24)", "invalid": bad}), 400
    found = st.peers.find(specs, only_seeds=only_seeds)
    hid = st.hidden
    out, ips_found, recs = [], set(), []
    with st.lock:
        for ih, hits in found.items():
            r = st.torrents.get(ih)
            if r is None or (ih in hid and not include_hidden):
                continue
            it = _brief(st, r)
            recs.append(r)
            ips_found.update(x[0] for x in hits)
            hits.sort(key=lambda x: (-x[1], -x[2]))
            it["match_count"] = len(hits)
            it["match_seeds"] = sum(1 for x in hits if x[1] == 1)
            it["matched"] = [{"ip": ip, "role": role, "last": last} for ip, role, last in hits[:30]]
            out.append(it)
    out.sort(key=lambda x: (-x["match_count"], -x["match_seeds"], -x["seeders"]))
    total, out = len(out), out[:501]
    keep = {x["ih"] for x in out}
    cold = st.cold_views([x for x in keep])                             # breakdown + trackers: from disk, no lock
    for it in out:
        c = cold.get(it["ih"]) or {}
        it["magnet"] = magnet_of({"ih": it["ih"], "name": it["name"], "hd": c.get("hd"), "trackers": c.get("trackers")})
    return jsonify({"searched": len(specs), "invalid": bad, "total": total, "results": out[:500],
                    "truncated": total > 500, "ips_with_hits": len(ips_found)})


# ------------------------------------------------------------ AI moderation (aimod.py)
class _NoAI(Exception):
    pass


def _ai():
    ai = _CFG["get_ai"]()
    if ai is None:
        raise _NoAI()
    return ai


@bp.errorhandler(_NoAI)
def _no_ai(_e):
    return jsonify({"error": "AI moderation is not available in this mode"}), 404


@bp.get("/ai")
def ai_status():
    return jsonify(_ai().status())


@bp.post("/ai/config")
def ai_config():
    try:
        cfg = _ai().update(request.get_json(silent=True) or {})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "config": cfg, **_store().ai_counts()})


@bp.post("/ai/test")
def ai_test():
    """Classifies a text or an indexed torrent NOW with the settings in the form (without saving them)."""
    ai, st = _ai(), _store()
    b = request.get_json(silent=True) or {}
    try:
        cfg = ai.validated(b.get("config") or {})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    text = str(b.get("text") or "").strip()[:900]
    ih = str(b.get("ih") or "").strip().lower()
    if ih:
        d = st.doc(ih)
        text = st.ai_text(d, cfg["files"]) if d >= 0 else None
    elif text:
        from aimod import torrent_text
        text = torrent_text(text, [], 0)
    if not text:
        return jsonify({"error": "write a torrent name (or give an indexed infohash)"}), 400
    t0 = time.time()
    from aimod import ModelError, ModelUnreachable
    try:
        r = ai.classify(text, cfg)
    except (ModelUnreachable, ModelError) as e:
        return jsonify({"error": str(e)}), 502
    except (ValueError, KeyError, TypeError) as e:
        return jsonify({"error": f"the model at {cfg['endpoint']} answered something unexpected: {e}"}), 502
    score = int(round(r["score"] * 100))
    hides = score >= cfg["threshold"] and bool(set(r["cats"]) & set(cfg["act_on"]))
    return jsonify({"text": text, "score": score, "cats": r["cats"], "label": r["label"], "logprobs": r["logprobs"], "api": r.get("api"),
                    "raw": r["raw"], "would_hide": hides, "ms": int((time.time() - t0) * 1000)})


@bp.get("/ai/items")
def ai_items():
    st = _store()
    a = request.args
    try:
        page = max(1, int(a.get("page", 1)))
        per = min(100, max(5, int(a.get("per_page", 20))))
        smin, smax = int(a.get("min", 0)), int(a.get("max", 100))
    except ValueError:
        return jsonify({"error": "invalid number"}), 400
    return jsonify(st.ai_list(state=a.get("state", "hidden"), smin=smin, smax=smax, cat=a.get("cat") or None,
                              q=(a.get("q") or "").strip()[:200], sort=a.get("sort", "score"), page=page, per=per))


@bp.post("/ai/items")
def ai_items_action():
    st = _store()
    b = request.get_json(silent=True) or {}
    ihs = [str(x) for x in (b.get("ihs") or [])][:5000]
    action = b.get("action")
    if action not in ("allow", "hide", "reset", "reanalyse"):
        return jsonify({"error": "action must be allow, hide, reset or reanalyse"}), 400
    n = st.ai_override(ihs, action)
    if action == "reanalyse":
        _ai().notify()
    return jsonify({"ok": True, "changed": n, **st.ai_counts()})


@bp.post("/ai/reanalyse-all")
def ai_reanalyse_all():
    _store().ai_reanalyse_all()
    _ai().notify()
    return jsonify({"ok": True, **_store().ai_counts()})
