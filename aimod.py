"""
AI moderation: a safety classifier reads the NAME (and the first file names) of every torrent and gives a probability
that it is NSFW or harmful. Above the threshold set in the admin panel the torrent is HIDDEN from public search,
exactly like an admin hide rule (never deleted: it stays indexed and measured). The admin panel lists what the AI
hid, with filters, and can show any torrent again ("allow": the AI never hides it again), hide one by hand, re-analyse,
change the threshold (applied instantly to the stored scores) or switch the AI off (nothing stays hidden by it).

The model does NOT run inside this process: it is called over HTTP, so this service's RAM does not change. Recommended:
llama.cpp's `llama-server` on the same machine (127.0.0.1) with Qwen3Guard-Gen-0.6B (Q4_K_M GGUF, ~0.7 GB RAM in the
llama-server process; install.sh can set it up). Any OpenAI-compatible endpoint works (another machine in the LAN, a
bigger model, a hosted API with the "chat" profile).

Profiles
  qwen3guard   Qwen3Guard-Gen (0.6B / 4B / 8B), 119 languages. /v1/completions with its own prompt format.
  llamaguard3  Llama Guard 3 (1B / 8B), 8 languages; has a separate "child sexual exploitation" category.
  chat         any instruction model through /v1/chat/completions (asks for a JSON answer).

Confidence = probability of the model's FIRST answer token ("Unsafe" / "Controversial" / "Safe") from the server's
logprobs: P(unsafe) + controversial_weight * P(controversial). Without logprobs (some servers) it falls back to the
label: unsafe = 100 %, controversial = controversial_weight, safe = 0 %. Copyright is never a category (every film would
be "unsafe"): only the categories selected in the panel are put in the prompt.

Cost: ~0.3-1 s of CPU per torrent with the 0.6B model on a small server (the long fixed part of the prompt is cached
by llama-server). New torrents are analysed first; the backlog, newest first, when there is spare time.
"""
import json
import logging
import math
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request

from jsonio import atomic_write, load_json

log = logging.getLogger("ai")

# category key -> label (bit = position)
CATS = (("sexual", "Sexual / pornographic"), ("child", "Minors (sexual exploitation)"), ("violent", "Violence / weapons"),
        ("illegal", "Illegal acts (drugs, crime)"), ("self_harm", "Suicide / self-harm"), ("hate", "Hate / discrimination"),
        ("other", "Other unsafe"))
CAT_KEYS = [k for k, _ in CATS]
CAT_BIT = {k: 1 << i for i, k in enumerate(CAT_KEYS)}
PROFILES = ("qwen3guard", "llamaguard3", "chat")
APIS = ("auto", "openai", "ollama")

DEFAULTS = {
    "enabled": False,               # analyse AND apply (off: nothing stays hidden by the AI; scores are kept)
    "endpoint": "http://127.0.0.1:8091",
    "api_key": "",
    "profile": "qwen3guard",
    "model": "",                    # model name for servers that need it (Ollama, hosted APIs); llama-server ignores it
    "api": "auto",                  # auto | openai (llama.cpp, vLLM…: /v1/completions) | ollama (/api/generate, raw prompt)
    "threshold": 85,                # % confidence from which a torrent is hidden
    "act_on": ["sexual", "child", "violent", "illegal", "self_harm", "hate", "other"],
    "files": 5,                     # file names sent along with the torrent name (0 = name only)
    "max_rate": 2.0,                # requests per second at most (CPU of the model server)
    "timeout": 600,                 # s to wait for ONE answer (slow models / busy shared servers)
    "backlog": True,                # also analyse what was indexed before enabling it (newest first)
    "controversial_weight": 0.5,
    "start_doc": 0,                 # with backlog off: first document to analyse (set when enabled)
}
_SCORE_MIN_FOR_CATS = 0.15          # below this, the categories are not asked (saves a request)

# ------------------------------------------------------------------ prompts
_QWEN_CATS = {"violent": "Violent.", "illegal": "Non-violent Illegal Acts.", "sexual": "Sexual Content or Sexual Acts.",
              "child": "Sexual Content or Sexual Acts.", "self_harm": "Suicide & Self-Harm.", "hate": "Unethical Acts."}
_QWEN_ORDER = ["Violent.", "Non-violent Illegal Acts.", "Sexual Content or Sexual Acts.", "Suicide & Self-Harm.", "Unethical Acts."]
_QWEN_BACK = {"violent": "violent", "non-violent illegal acts": "illegal", "sexual content or sexual acts": "sexual",
              "suicide & self-harm": "self_harm", "unethical acts": "hate"}
_QWEN = ("<|im_start|>user\n# Task:\n1) Evaluate whether THE LAST USER's query belongs to safe or unsafe content based on our "
         "safety policy.\n2) Identify the category of unsafe content found.\n\n<BEGIN SAFETY POLICY>\n**Safety: Safe**: The "
         "USER's query does not contain any prohibited content or content that could lead to prohibited content.\n**Safety: "
         "Unsafe**: The USER's query contains prohibited content.\n**Safety: Controversial**: The USER's query does not "
         "explicitly contain prohibited content, but its intent, context, or potential responses could be misinterpreted, "
         "misused, or exploited to generate unsafe content under certain conditions.\n<END SAFETY POLICY>\n\n<BEGIN UNSAFE "
         "CONTENT CATEGORIES>\n{cats}\n<END UNSAFE CONTENT CATEGORIES>\n\n<BEGIN CONVERSATION>\n\nUSER: {text}\n\n<END "
         "CONVERSATION>\n\nProvide your safety assessment for ONLY THE LAST **USER's query** in the above conversation:\n - "
         "The first line must be one of: 'Safety: Safe', 'Safety: Unsafe', 'Safety: Controversial'.\n - The second line "
         "should start with 'Categories:' followed by a list of any unsafe content categories, separated by commas. If the "
         "content is safe, use 'Categories: None'.<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\nSafety:")

_LG_CATS = {"violent": ["S1: Violent Crimes.", "S9: Indiscriminate Weapons."], "illegal": ["S2: Non-Violent Crimes."],
            "sexual": ["S3: Sex-Related Crimes.", "S12: Sexual Content."], "child": ["S4: Child Sexual Exploitation."],
            "hate": ["S10: Hate."], "self_harm": ["S11: Suicide & Self-Harm."]}
_LG_BACK = {"S1": "violent", "S9": "violent", "S2": "illegal", "S3": "sexual", "S12": "sexual", "S4": "child",
            "S10": "hate", "S11": "self_harm"}
_LG = ("<|start_header_id|>user<|end_header_id|>\n\nTask: Check if there is unsafe content in 'User' messages in "
       "conversations according our safety policy with the below categories.\n\n<BEGIN UNSAFE CONTENT CATEGORIES>\n{cats}\n"
       "<END UNSAFE CONTENT CATEGORIES>\n\n<BEGIN CONVERSATION>\n\nUser: {text}\n\n<END CONVERSATION>\n\nProvide your "
       "safety assessment for ONLY THE LAST User message in the above conversation:\n - First line must read 'safe' or "
       "'unsafe'.\n - If unsafe, a second line must include a comma-separated list of violated categories.<|eot_id|>"
       "<|start_header_id|>assistant<|end_header_id|>\n\n")

_CHAT_SYS = ("You are a content-safety classifier for a BitTorrent search engine. You get a torrent name and some of its "
             "file names. Estimate the probability (0-100) that the torrent contains content of these categories: {cats}. "
             "Ordinary films, series, music, software, games and books are SAFE even if they are pirated: copyright is not "
             "a category. Answer ONLY with JSON, no other text: {{\"p\": <0-100>, \"categories\": [<keys from: {keys}>]}}")
_CAT_HELP = {"sexual": "sexual: pornography or explicit sexual content", "child": "child: any sexual content involving minors",
             "violent": "violent: gore, real violence, weapons manufacturing", "illegal": "illegal: drugs, crime, fraud, hacking tools for crime",
             "self_harm": "self_harm: suicide or self-harm", "hate": "hate: hate speech, extremism, discrimination",
             "other": "other: anything else clearly harmful"}


def _clean(s, n):
    s = re.sub(r"[\x00-\x1f\x7f<>|]+", " ", str(s))
    return re.sub(r"\s+", " ", s).strip()[:n]


def torrent_text(name, files, nfiles):
    """What the model sees: the name plus the first file names (truncated)."""
    out = "Torrent name: " + _clean(name, 300)
    paths = [_clean(f[0] if isinstance(f, (list, tuple)) else f, 120) for f in (files or [])[:max(0, nfiles)]]
    if paths:
        out += "\nFiles: " + " ; ".join(p for p in paths if p)
    return out[:900]


def _label_of(tok):
    t = (tok or "").strip().lower()
    if not t:
        return None
    if t.startswith("unsafe") or t in ("un", "uns", "unsa", "unsaf"):
        return "unsafe"
    if t.startswith("contro") or t in ("con", "cont", "contr"):
        return "controversial"
    if t == "safe" or t in ("sa", "saf"):
        return "safe"
    return None


def _first_alternatives(resp):
    """[(token, logprob)] for the FIRST generated token, from any of the usual response formats (or None)."""
    ch = (resp.get("choices") or [{}])[0]
    lp = ch.get("logprobs")
    try:
        olp = resp.get("logprobs")
        if isinstance(olp, list) and olp:                                      # Ollama native /api/generate
            first = olp[0]
            alts = [(a.get("token", ""), a.get("logprob")) for a in first.get("top_logprobs") or []]
            return alts or [(first.get("token", ""), first.get("logprob"))]
        if isinstance(lp, dict) and lp.get("content"):                         # OpenAI chat / new llama.cpp
            first = lp["content"][0]
            alts = [(a.get("token", ""), a.get("logprob")) for a in first.get("top_logprobs") or []]
            return alts or [(first.get("token", ""), first.get("logprob"))]
        if isinstance(lp, dict) and lp.get("top_logprobs"):                    # OpenAI legacy completions
            return list(lp["top_logprobs"][0].items())
        cp = resp.get("completion_probabilities") or ch.get("completion_probabilities")
        if cp:                                                                 # llama.cpp native
            first = cp[0]
            out = []
            for a in first.get("top_logprobs") or first.get("probs") or []:
                tok = a.get("token", a.get("tok_str", ""))
                lpv = a["logprob"] if "logprob" in a else math.log(max(float(a.get("prob", 0)), 1e-12))
                out.append((tok, lpv))
            return out
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return None


def _text_of(resp):
    ch = (resp.get("choices") or [{}])[0]
    if "text" in ch:
        return ch.get("text") or ""
    if "response" in resp:                                                     # Ollama native
        return resp.get("response") or ""
    return ((ch.get("message") or {}).get("content")) or resp.get("content") or ""


class ModelUnreachable(Exception):
    """Network problem (refused, timeout, DNS): retried later with backoff, the torrent is not marked."""


class ModelError(Exception):
    """The server answered, but not what was expected (HTTP error, wrong format)."""


class AIModerator:
    def __init__(self, store, data_dir):
        self.store = store
        self.path = os.path.join(data_dir, "ai.json")
        saved = load_json(self.path, {})
        self.cfg = {**DEFAULTS, **{k: v for k, v in (saved if isinstance(saved, dict) else {}).items() if k in DEFAULTS}}
        if isinstance(saved, dict) and saved.get("timeout") == 60:
            self.cfg["timeout"] = DEFAULTS["timeout"]     # 4.2.x default (not editable then): too short for slow models
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stop_ev = threading.Event()
        self.thread = None
        self._api_cache = {}                              # endpoint -> detected API ("openai" / "ollama")
        self.st = {"processed": 0, "hidden_now": 0, "errors": 0, "last_error": "", "last_error_at": 0, "last_ok_at": 0,
                   "avg_ms": 0.0, "logprobs": None, "backoff_until": 0, "state": "off", "saved_at": 0, "error_endpoint": "",
                   "api": None}
        self._apply_policy()

    # ------------------------------------------------------------ config
    def public_cfg(self):
        c = dict(self.cfg)
        c["api_key"] = ("•" * 8 + c["api_key"][-4:]) if c["api_key"] else ""
        return c

    def validated(self, changes):
        """The current config with these changes applied and checked (not saved). Raises ValueError."""
        c, problem = self.check(changes)
        if problem:
            raise ValueError(problem)
        return c

    def check(self, changes):
        """Like validated(), without raising: (config, None) or (None, message for the admin)."""
        c = dict(self.cfg)
        for k, v in changes.items():
            if k not in DEFAULTS or k == "start_doc":
                continue
            if k == "api_key" and isinstance(v, str) and v.startswith("•"):
                continue                                        # masked value sent back unchanged
            c[k] = v
        c["enabled"] = bool(c["enabled"])
        c["backlog"] = bool(c["backlog"])
        c["profile"] = c["profile"] if c["profile"] in PROFILES else "qwen3guard"
        c["api"] = c.get("api") if c.get("api") in APIS else "auto"
        c["endpoint"] = re.sub(r"/v1(/chat)?(/completions)?/?$", "", str(c["endpoint"]).strip().rstrip("/"))
        if not re.match(r"^https?://[^\s/]+", c["endpoint"]):
            return None, "the endpoint must be an http(s):// URL"
        for k, lo, hi, typ in (("threshold", 1, 100, int), ("files", 0, 20, int), ("max_rate", 0.05, 50, float),
                               ("timeout", 10, 3600, int), ("controversial_weight", 0, 1, float)):
            try:
                c[k] = min(max(typ(c[k]), lo), hi)
            except (TypeError, ValueError):
                return None, f"invalid {k}"
        c["act_on"] = [k for k in CAT_KEYS if k in set(c.get("act_on") or [])]
        if not c["act_on"]:
            return None, "select at least one category"
        c["model"] = str(c["model"])[:200]
        c["api_key"] = str(c["api_key"])[:500]
        return c, None

    def update(self, changes):
        """Validates and saves config changes. Returns the new public config. Raises ValueError."""
        c = self.validated(changes)
        if c["enabled"] and not self.cfg["enabled"] and not c["backlog"]:
            c["start_doc"] = int(self.store.cols.n)            # from now on only
        with self.lock:
            self.cfg = c
            atomic_write(self.path, c)
            try:
                os.chmod(self.path, 0o600)                       # may hold an API key
            except OSError:
                pass
        self._apply_policy()
        self.st.update(last_error="", last_error_at=0, backoff_until=0, saved_at=int(time.time()), logprobs=None)
        self._api_cache.clear()
        self.wake.set()
        return self.public_cfg()

    def _apply_policy(self):
        c = self.cfg
        mask = 0
        for k in c["act_on"]:
            mask |= CAT_BIT.get(k, 0)
        self.store.set_ai_policy((int(c["threshold"]), mask) if c["enabled"] else None)

    # ------------------------------------------------------------ model calls
    @staticmethod
    def _post(c, path, body):
        """POST to the endpoint of config `c`. Network problems -> ModelUnreachable; an HTTP error or a non-JSON
        answer -> ModelError with what the server said."""
        url = c["endpoint"] + path
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        if c["api_key"]:
            req.add_header("Authorization", "Bearer " + c["api_key"])
        try:
            with urllib.request.urlopen(req, timeout=c["timeout"]) as r:
                raw = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "replace")[:200]
            except OSError:
                detail = ""
            hint = {401: " (wrong or missing API key)", 403: " (wrong API key, or blocked by the proxy)",
                    404: " (this server has no " + path + ": with Ollama, IARemote or hosted APIs use the 'chat' model type)",
                    405: " (method not allowed: check the endpoint URL)"}.get(e.code, "")
            raise ModelError(f"{url} answered HTTP {e.code}{hint} {detail}".strip())
        except (TimeoutError, socket.timeout) as e:
            raise ModelUnreachable(f"no answer from {c['endpoint']} within {c['timeout']} s (model slow or busy): raise the "
                                   f"timeout, or send fewer file names") from e
        except urllib.error.URLError as e:
            if isinstance(e.reason, (TimeoutError, socket.timeout)):
                raise ModelUnreachable(f"no answer from {c['endpoint']} within {c['timeout']} s (model slow or busy): raise "
                                       f"the timeout, or send fewer file names") from e
            raise ModelUnreachable(f"cannot reach the model at {c['endpoint']}: {e.reason}") from e
        except OSError as e:
            raise ModelUnreachable(f"cannot reach the model at {c['endpoint']}: {e}") from e
        try:
            return json.loads(raw)
        except ValueError:
            raise ModelError(f"{url} did not answer JSON: {raw[:150]!r}")

    def api_of(self, c):
        """Which API the server speaks. Ollama (also behind a proxy that forwards /api/*) answers GET /api/version."""
        if c.get("api") in ("openai", "ollama"):
            return c["api"]
        ep = c["endpoint"]
        if ep not in self._api_cache:
            api = "openai"
            req = urllib.request.Request(ep + "/api/version")
            if c["api_key"]:
                req.add_header("Authorization", "Bearer " + c["api_key"])
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    if "version" in json.loads(r.read().decode("utf-8", "replace") or "{}"):
                        api = "ollama"
            except (urllib.error.URLError, OSError, ValueError, TimeoutError):
                pass
            self._api_cache[ep] = api
        return self._api_cache[ep]

    def _complete(self, c, prompt, max_tokens, stops, want_logprobs):
        """Raw-prompt completion on either API -> (text, response). The prompt already has the model's own format."""
        if self.api_of(c) == "ollama":
            if not c["model"]:
                raise ModelError("Ollama needs the model name (Model name field), e.g. llama-guard3:1b")
            body = {"model": c["model"], "prompt": prompt, "raw": True, "stream": False, "keep_alive": "30m",
                    "options": {"temperature": 0, "num_predict": max_tokens, "stop": stops}}
            if want_logprobs:
                body.update(logprobs=True, top_logprobs=10)
            resp = self._post(c, "/api/generate", body)
            return resp.get("response") or "", resp
        body = {"prompt": prompt, "max_tokens": max_tokens, "temperature": 0, "stop": stops, "cache_prompt": True}
        if want_logprobs:
            body.update(logprobs=10, n_probs=10)
        if c["model"]:
            body["model"] = c["model"]
        resp = self._post(c, "/v1/completions", body)
        return _text_of(resp), resp

    def classify(self, text, cfg=None):
        """-> {"score": 0..1, "cats": [keys], "label": str, "logprobs": bool, "raw": str}. Raises on network errors."""
        c = cfg or self.cfg
        prof, w = c["profile"], float(c["controversial_weight"])
        act = [k for k in CAT_KEYS if k in c["act_on"]]
        if prof == "chat":
            cats = ", ".join(_CAT_HELP[k] for k in act)
            body = {"model": c["model"] or "default", "temperature": 0, "max_tokens": 80,
                    "messages": [{"role": "system", "content": _CHAT_SYS.format(cats=cats, keys=", ".join(act))},
                                 {"role": "user", "content": text}]}
            resp = self._post(c, "/v1/chat/completions", body)
            raw = _text_of(resp)
            m = re.search(r"\{.*\}", raw, re.S)
            try:
                j = json.loads(m.group(0)) if m else {}
            except ValueError:
                j = {}
            p = j.get("p", j.get("probability"))
            if not isinstance(p, (int, float)):
                raise ValueError("unexpected answer: " + raw[:200])
            got = [k for k in (j.get("categories") or []) if k in CAT_BIT]
            score = min(max(float(p) / 100.0, 0.0), 1.0)
            return {"score": score, "cats": got or (["other"] if score >= 0.5 else []), "label": "json", "logprobs": False, "raw": raw[:300]}

        if prof == "qwen3guard":
            lines = []
            for k in act:
                x = _QWEN_CATS.get(k)
                if x and x not in lines:
                    lines.append(x)
            lines.sort(key=_QWEN_ORDER.index)
            prompt = _QWEN.format(cats="\n".join(lines) or "Sexual Content or Sexual Acts.", text=text)
        else:
            lines = sorted({x for k in act for x in _LG_CATS.get(k, [])}, key=lambda s: int(s[1:s.index(":")]))
            prompt = _LG.format(cats="\n".join(lines) or "S12: Sexual Content.", text=text)

        stops = ["\n", "<|im_end|>", "<|eot_id|>"]
        first, resp = self._complete(c, prompt, 4, stops, True)
        label = _label_of(first.split()[0] if first.split() else "")
        alts = _first_alternatives(resp)
        has_lp = False
        if alts:
            p = {"unsafe": 0.0, "controversial": 0.0, "safe": 0.0}
            for tok, lp in alts:
                lab = _label_of(tok)
                if lab and lp is not None:
                    p[lab] += math.exp(float(lp))
            tot = sum(p.values())
            if tot >= 0.3:
                has_lp = True
                score = (p["unsafe"] + w * p["controversial"]) / tot
        if not has_lp:
            if label is None:
                raise ValueError("unexpected answer: " + first[:200])
            score = {"unsafe": 1.0, "controversial": w, "safe": 0.0}[label]
        cats = []
        raw = first.strip()
        if score >= _SCORE_MIN_FOR_CATS:                 # which categories (prefix cached by the server: cheap)
            if prof == "qwen3guard":
                cont = prompt + " Unsafe\nCategories:"
            else:
                cont = prompt + "unsafe\n"
            try:
                t2, _ = self._complete(c, cont, 24, stops, False)
            except (ModelError, ModelUnreachable):
                t2 = ""
            raw += " | " + t2.strip()
            if prof == "qwen3guard":
                for part in t2.split(","):
                    k = _QWEN_BACK.get(part.strip().rstrip(".").lower())
                    if k:
                        cats.append(k)
            else:
                for code in re.findall(r"S\d{1,2}", t2):
                    k = _LG_BACK.get(code)
                    if k:
                        cats.append(k)
            cats = list(dict.fromkeys(cats)) or ["other"]
        self.st["api"] = self.api_of(c)
        return {"score": score, "cats": cats, "label": label or "?", "logprobs": has_lp, "raw": raw[:300],
                "api": self.st["api"]}

    # ------------------------------------------------------------ worker
    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="ai-moderation", daemon=True)
            self.thread.start()

    def stop(self):
        self.stop_ev.set()
        self.wake.set()

    def notify(self):
        self.wake.set()

    def _run(self):
        backoff = 5
        while not self.stop_ev.is_set():
            c = self.cfg
            if not c["enabled"]:
                self.st["state"] = "off"
                self.wake.wait(30)
                self.wake.clear()
                continue
            docs = self.store.ai_pending(16, 0 if c["backlog"] else int(c["start_doc"]))
            if not len(docs):
                self.st["state"] = "idle"
                self.wake.wait(15)
                self.wake.clear()
                continue
            self.st["state"] = "working"
            for d in docs:
                if self.stop_ev.is_set() or not self.cfg["enabled"]:
                    break
                t0 = time.time()
                if self.cfg is not c:                       # settings saved meanwhile: start again with them
                    backoff = 5
                    break
                try:
                    text = self.store.ai_text(int(d), int(c["files"]))
                    if text is None:
                        continue
                    r = self.classify(text, c)
                except ModelUnreachable as e:
                    self._error(str(e), c)
                    wait = 30 if "no answer from" in str(e) else backoff     # slow (it answers) vs unreachable
                    self.st["backoff_until"] = time.time() + wait
                    self._pause(wait, c)
                    if wait == backoff:
                        backoff = min(backoff * 2, 300)
                    break
                except ModelError as e:                     # the server answers, but wrongly: same for every torrent
                    self._error(str(e), c)
                    self.st["backoff_until"] = time.time() + 60
                    self._pause(60, c)
                    break
                except (ValueError, KeyError, TypeError) as e:   # an odd answer for THIS torrent: mark it and go on
                    self._error(str(e), c)
                    self.store.ai_set(int(d), score=0, cats=0, done=True, error=True)
                    continue
                backoff = 5
                cats = 0
                for k in r["cats"]:
                    cats |= CAT_BIT.get(k, 0)
                hid = self.store.ai_set(int(d), score=int(round(r["score"] * 100)), cats=cats, done=True)
                ms = (time.time() - t0) * 1000
                st = self.st
                st["processed"] += 1
                st["hidden_now"] += 1 if hid else 0
                st["avg_ms"] = ms if not st["avg_ms"] else st["avg_ms"] * 0.95 + ms * 0.05
                st["last_ok_at"] = int(time.time())
                st["logprobs"] = r["logprobs"]
                gap = 1.0 / max(float(c["max_rate"]), 0.05) - (time.time() - t0)
                if gap > 0:
                    self._pause(gap, c)

    def _pause(self, seconds, c):
        """Sleeps, but wakes up at once on shutdown or when the settings are saved."""
        end = time.time() + seconds
        while not self.stop_ev.is_set() and self.cfg is c and time.time() < end:
            self.wake.wait(min(1.0, end - time.time()))
        self.wake.clear()

    def _error(self, msg, c):
        if self.cfg is not c:                             # an answer to settings that were replaced meanwhile
            return
        self.st["errors"] += 1
        self.st["last_error"] = msg[:300]
        self.st["last_error_at"] = int(time.time())
        self.st["error_endpoint"] = c["endpoint"]
        log.warning("AI moderation: %s", msg)

    def status(self):
        return {**self.st, **self.store.ai_counts(), "config": self.public_cfg(),
                "categories": [{"key": k, "label": l} for k, l in CATS], "profiles": list(PROFILES)}
