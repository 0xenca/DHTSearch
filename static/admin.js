"use strict";
/* admin.js — admin panel.
   Shortcut: Ctrl+Alt+A (on Mac ⌃⌥A). Opens the password dialog; the password is checked ON THE SERVER,
   which returns a session cookie. This file only renders: without a session, the /api/admin/* API answers 401. */
(function () {
  const HAS_PANEL = !!document.getElementById("view-admin");

  async function req(method, path, body) {
    const opts = { method, credentials: "same-origin", headers: {} };
    if (method !== "GET") { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body || {}); }
    const r = await fetch(path, opts);
    let data = null;
    try { data = await r.json(); } catch {}
    if (!r.ok) {
      if (r.status === 401 && path !== "/api/admin/login") lock();
      const e = new Error((data && data.error) || ("Error " + r.status)); e.status = r.status; e.data = data; throw e;
    }
    return data;
  }

  /* ---------------------------------------------------------------- access */
  function unlock() {
    window.adminUnlocked = true;
    const t = document.getElementById("tab-admin"); if (t) t.hidden = false;
  }
  function lock() {
    window.adminUnlocked = false;
    const t = document.getElementById("tab-admin"); if (t) t.hidden = true;
    if (HAS_PANEL && typeof currentTab !== "undefined" && currentTab === "admin") setTab("search");
  }
  function goAdmin() {
    if (HAS_PANEL) { location.hash = "admin"; setTab("admin"); }
    else location.href = "/#admin";
  }

  let dlg, pwd, msg, okBtn;
  function openLogin() {
    if (window.adminUnlocked) return goAdmin();
    if (!dlg) {
      pwd = h("input", { type: "password", autocomplete: "current-password", placeholder: "Password", "aria-label": "Admin password", required: true });
      msg = h("div", { class: "adm-msg", role: "alert" });
      okBtn = h("button", { type: "submit", class: "btn-s primary" }, "Sign in");
      const cancel = h("button", { type: "button", class: "btn-s", onclick: () => dlg.close() }, "Cancel");
      const form = h("form", { class: "adm-login" },
        h("h2", null, "Admin access"),
        h("p", { class: "muted small" }, "Enter the password to unlock the panel."),
        pwd, msg, h("div", { class: "adm-btns" }, cancel, okBtn));
      form.onsubmit = async e => {
        e.preventDefault();
        okBtn.disabled = true; msg.textContent = "";
        try {
          await req("POST", "/api/admin/login", { password: pwd.value });
          pwd.value = ""; dlg.close(); unlock(); goAdmin();
        } catch (err) {
          msg.textContent = err.message; pwd.select();
          form.classList.remove("shake"); void form.offsetWidth; form.classList.add("shake");
        } finally { okBtn.disabled = false; }
      };
      dlg = h("dialog", { class: "adm-dlg", id: "adm-login" }, form);
      dlg.addEventListener("click", e => { if (e.target === dlg) dlg.close(); });   // click outside: close
      document.body.append(dlg);
    }
    msg.textContent = ""; pwd.value = "";
    dlg.showModal(); pwd.focus();
  }

  document.addEventListener("keydown", e => {
    if (e.ctrlKey && e.altKey && !e.shiftKey && !e.metaKey && e.code === "KeyA") { e.preventDefault(); openLogin(); }
  });

  req("GET", "/api/admin/me").then(r => {
    if (!r.admin) return;
    unlock();
    if (HAS_PANEL && (window.INITIAL_HASH === "#admin" || location.hash === "#admin")) setTab("admin");
  }).catch(() => {});

  if (!HAS_PANEL) return;

  /* ============================================================ PANEL ============================================================ */
  const A = { rules: [], byId: {}, rule: "", hq: "", hsort: "seeders", hpage: 1, pSeeds: false, pElse: false, built: false, statusTimer: null };
  const SCOPES = [["all", "Name and files"], ["name", "Name only"], ["files", "Files only"]];
  const MODES = [["word", "Whole word"], ["substring", "Contains the text"], ["regex", "Regular expression"]];
  const lbl = (list, v) => (list.find(x => x[0] === v) || [, v])[1];
  const E = {};                                         // node references

  function ruleName(id) { if (id === "ai") return "AI moderation"; const r = A.byId[id]; return r ? r.term : "(deleted rule)"; }
  function sel(opts, value, onchange, aria) {
    const s = h("select", { "aria-label": aria }, opts.map(([v, l]) => h("option", { value: v }, l)));
    s.value = value; s.onchange = () => onchange(s.value); return s;
  }
  function check(label, value, onchange) {
    const i = h("input", { type: "checkbox" }); i.checked = value; i.onchange = () => onchange(i.checked);
    return h("label", { class: "switch" }, i, h("span", null, label));
  }
  function busy(btn, on, text) {
    if (on) { btn.dataset.l = btn.textContent; btn.textContent = text; btn.disabled = true; }
    else { btn.textContent = btn.dataset.l || btn.textContent; btn.disabled = false; }
  }
  function toast(text, bad) {
    const t = h("div", { class: "adm-toast" + (bad ? " bad" : "") }, text);
    document.body.append(t); setTimeout(() => t.remove(), 3500);
  }

  function build() {
    if (A.built) return;
    A.built = true;
    E.status = h("div", { class: "muted small" });
    E.pwdWarn = h("div", { class: "adm-warn", hidden: true },
      "⚠ You are using the default password (\"admin\"). Anyone reaching this site can sign in: start with --admin-password or the TS_ADMIN_PASSWORD variable.");
    const logout = h("button", { type: "button", class: "btn-s" }, "Sign out");
    logout.onclick = async () => { try { await req("POST", "/api/admin/logout"); } catch {} lock(); };

    /* --- rules --- */
    E.term = h("input", { type: "text", placeholder: "Word, text or expression to hide…", "aria-label": "Term", maxlength: "200" });
    E.scope = sel(SCOPES, "all", () => {}, "Where to search");
    E.mode = sel(MODES, "word", () => {}, "Match type");
    E.prevBtn = h("button", { type: "button", class: "btn-s" }, "Preview");
    E.addBtn = h("button", { type: "submit", class: "btn-s primary" }, "Hide");
    E.preview = h("div", { class: "adm-preview", hidden: true });
    E.ruleForm = h("form", { class: "adm-form" }, E.term, E.scope, E.mode, E.prevBtn, E.addBtn);
    E.ruleForm.onsubmit = e => { e.preventDefault(); addRule(); };
    E.prevBtn.onclick = previewRule;
    E.rulesBox = h("div");

    /* --- hidden --- */
    E.ruleSel1 = h("select", { "aria-label": "Filter by rule" });
    E.ruleSel1.onchange = () => setRule(E.ruleSel1.value);
    E.hq = h("input", { type: "search", placeholder: "Filter by name or infohash…", "aria-label": "Filter hidden" });
    let hqT; E.hq.oninput = () => { clearTimeout(hqT); hqT = setTimeout(() => { A.hq = E.hq.value.trim(); A.hpage = 1; loadHidden(); }, 300); };
    const hsort = sel([["seeders", "Most seeders"], ["peers_known", "Most known peers"], ["date", "Recently indexed"], ["size", "Size"], ["name", "Name"]],
      A.hsort, v => { A.hsort = v; A.hpage = 1; loadHidden(); }, "Order");
    E.refreshBtn = h("button", { type: "button", class: "btn-s", title: "The crawler reconnects to the swarm of these torrents as soon as it has a free slot and records their peers" }, "Probe peers now");
    E.refreshBtn.onclick = async () => {
      busy(E.refreshBtn, true, "Queuing…");
      try { const r = await req("POST", "/api/admin/refresh", { rule: A.rule }); toast(`${fmtNum(r.queued)} torrents queued for probing (${fmtNum(r.pending)} pending). Peers will appear within seconds or minutes.`); loadStatus(); }
      catch (e) { toast(e.message, true); } finally { busy(E.refreshBtn, false); }
    };
    E.hsum = h("div", { class: "muted small" });
    E.hlist = h("div", { class: "adm-list" });
    E.hpager = h("div", { class: "pager" });

    /* --- peers of hidden content --- */
    E.ruleSel2 = h("select", { "aria-label": "Filter by rule" });
    E.ruleSel2.onchange = () => setRule(E.ruleSel2.value);
    const allBtn = h("button", { type: "button", class: "btn-s primary", title: "Searches all these IPs in torrents that are NOT hidden" }, "Search these peers in other torrents");
    allBtn.onclick = () => searchPeers({ from_rule: A.rule, from_seeds: A.pSeeds }, A.pSeeds ? "seeders of hidden content" : "all peers of hidden content");
    E.psum = h("div", { class: "muted small" });
    E.pbox = h("div");

    /* --- peer search --- */
    E.ips = h("textarea", { rows: "3", placeholder: "IPs or networks separated by spaces, commas or line breaks.  E.g.: 203.0.113.7  198.51.100.0/24  2001:db8::1", "aria-label": "IPs to search" });
    E.sSeeds = false; E.sHidden = false;
    const sBtn = h("button", { type: "submit", class: "btn-s primary" }, "Search");
    const sForm = h("form", { class: "adm-sform" }, E.ips,
      h("div", { class: "adm-row" },
        check("Only where they act as seeder", false, v => E.sSeeds = v),
        check("Also include hidden torrents", false, v => E.sHidden = v), h("span", { class: "grow" }), sBtn));
    sForm.onsubmit = e => { e.preventDefault(); searchPeers({ ips: E.ips.value }, null); };
    E.ssum = h("div", { class: "muted small" });
    E.sres = h("div", { class: "adm-list" });
    E.searchCard = h("section", { class: "tcard wide", id: "adm-search" },
      h("h2", null, "Peer search in other torrents"),
      h("p", { class: "muted small" }, "Are these peers or seeders in torrents NOT covered by any rule? Already hidden content is excluded by default. Click any IP in the tables to search it here."),
      sForm, E.ssum, E.sres);

    $("#view-admin").replaceChildren(
      h("div", { class: "adm-head" }, h("div", null, h("h2", { class: "stats-title" }, "Admin panel"), E.status), h("span", { class: "grow" }), logout),
      E.pwdWarn,
      h("div", { class: "adm-grid" },
        h("section", { class: "tcard wide", id: "adm-trackers" },
          h("h2", null, "Trackers: do they really answer?"),
          h("p", { class: "muted small" }, "Answers the crawler has received since it started. If a tracker never answers, the \"verified\" figures depend on the others; ",
            "if NONE answers, check that the server can get out over UDP (firewall/router). To check a specific torrent, open its page and click \"Verify live now\"."),
          E.trBox = h("div", { class: "muted small" }, "Loading…")),
        h("section", { class: "tcard wide" },
          h("h2", null, "Hide from search"),
          h("p", { class: "muted small" }, "Torrents whose name or files match disappear from search, autocomplete, \"similar\" and their public page. ",
            h("b", null, "They are not deleted"), ": they stay indexed, keep being measured, and you can show them again by pausing or deleting the rule. Case and accents do not matter."),
          E.ruleForm,
          h("p", { class: "muted small adm-modes" },
            h("b", null, "Whole word"), ": \"foo\" hides \"Foo.Bar.mkv\" but not \"foobar\"; several words = that sequence. ",
            h("b", null, "Contains"), ": also inside other words. ",
            h("b", null, "Regex"), ": Python regular expression."),
          E.preview, E.rulesBox),
        buildAI(),
        h("section", { class: "tcard wide", id: "adm-hidden" },
          h("h2", null, "Hidden content and why"),
          h("div", { class: "adm-row" }, E.ruleSel1, E.hq, hsort, h("span", { class: "grow" }), E.refreshBtn),
          E.hsum, E.hlist, E.hpager),
        h("section", { class: "tcard wide", id: "adm-peers" },
          h("h2", null, "Peers and seeders of hidden content"),
          h("p", { class: "muted small" }, "IPs seen in the swarm of hidden torrents. \"Elsewhere\" = in how many NON-hidden torrents the same IP appears. ",
            "An IP is not a person: CGNAT, VPNs, seedboxes and fake DHT peers share or invent addresses."),
          h("div", { class: "adm-row" }, E.ruleSel2,
            check("Seeders only", A.pSeeds, v => { A.pSeeds = v; loadPeers(); }),
            check("Only those in other torrents", A.pElse, v => { A.pElse = v; loadPeers(); }),
            h("span", { class: "grow" }), allBtn),
          E.psum, E.pbox),
        E.searchCard));
  }

  /* ---------------------------------------------------------------- AI moderation */
  const AI = { cfg: null, cats: [], state: "hidden", cat: "", min: 0, max: 100, q: "", sort: "score", page: 1, sel: new Set(), seq: 0 };
  const AI_STATES = [["hidden", "Hidden by the AI"], ["allowed", "Shown again by you"], ["manual", "Hidden by you"],
                     ["visible", "Analysed, visible"], ["all", "Everything analysed"]];
  const STATE_CHIP = { hidden: ["warn", "hidden by AI"], allowed: ["ok", "shown (your decision)"], manual: ["warn", "hidden by you"], visible: ["ok", "visible"] };
  const catLabel = k => (AI.cats.find(c => c.key === k) || { label: k }).label;
  function scoreChip(s) { return h("span", { class: "chip ai-score " + (s >= 85 ? "hi" : s >= 50 ? "mid" : "lo"), title: "Confidence that it is NSFW / harmful" }, s + " %"); }

  function buildAI() {
    const f = E.ai = {};
    f.status = h("div", { class: "muted small" }, "Loading…");
    f.warn = h("div", { class: "adm-warn", hidden: true });
    f.enabled = h("input", { type: "checkbox" });
    f.endpoint = h("input", { type: "url", placeholder: "http://127.0.0.1:8091", "aria-label": "Model endpoint" });
    f.profile = h("select", { "aria-label": "Model type" },
      h("option", { value: "qwen3guard" }, "Qwen3Guard-Gen (recommended)"), h("option", { value: "llamaguard3" }, "Llama Guard 3"),
      h("option", { value: "chat" }, "Any chat model (JSON answer)"));
    f.model = h("input", { type: "text", placeholder: "Ollama: e.g. llama-guard3:1b · llama-server: empty", "aria-label": "Model name" });
    f.api = h("select", { "aria-label": "Server API" },
      h("option", { value: "auto" }, "Auto-detect"), h("option", { value: "ollama" }, "Ollama (native API, with probabilities)"),
      h("option", { value: "openai" }, "OpenAI-compatible (llama.cpp, vLLM…)"));
    f.key = h("input", { type: "password", placeholder: "(none)", autocomplete: "off", "aria-label": "API key" });
    f.thr = h("input", { type: "range", min: "50", max: "100", step: "1", "aria-label": "Threshold" });
    f.thrNum = h("b", { class: "ai-thr" });
    f.thr.oninput = () => f.thrNum.textContent = f.thr.value + " %";
    f.catsBox = h("div", { class: "ai-cats" });
    f.files = h("input", { type: "number", min: "0", max: "20", "aria-label": "File names sent" });
    f.rate = h("input", { type: "number", min: "0.05", max: "50", step: "0.05", "aria-label": "Requests per second" });
    f.cw = h("input", { type: "number", min: "0", max: "1", step: "0.05", "aria-label": "Weight of controversial" });
    f.timeout = h("input", { type: "number", min: "10", max: "3600", step: "10", "aria-label": "Timeout per request" });
    f.backlog = h("input", { type: "checkbox" });
    f.save = h("button", { type: "submit", class: "btn-s primary" }, "Save");
    const field = (label, el, hint) => h("label", { class: "ai-field" }, h("span", null, label), el, hint ? h("small", { class: "muted" }, hint) : null);
    const form = h("form", { class: "ai-form" },
      h("label", { class: "switch ai-on" }, f.enabled, h("span", null, h("b", null, "AI moderation on"), " — analyses new torrents and hides those above the threshold. Off: nothing stays hidden by the AI (scores are kept).")),
      field("Model endpoint", f.endpoint, "OpenAI-compatible server (llama.cpp llama-server, Ollama, LAN machine or hosted API)"),
      field("Model type", f.profile), field("Model name", f.model), field("API key", f.key),
      field("Server API", f.api, "Ollama gives probabilities only through its own API (version 0.12 or newer)"),
      h("div", { class: "ai-field wide" }, h("span", null, "Hide from ", f.thrNum, " confidence"), f.thr,
        h("small", { class: "muted" }, "Lower = hides more (more false positives). The change applies at once to everything already analysed.")),
      h("div", { class: "ai-field wide" }, h("span", null, "Act on these categories"), f.catsBox,
        h("small", { class: "muted" }, "Only these are asked to the model. Qwen3Guard has no separate “minors” category (it comes as sexual / illegal); Llama Guard 3 does. Changing them only affects new analyses: use “Re-analyse everything”.")),
      field("File names sent", f.files, "besides the name (0 = name only)"),
      field("Max requests / s", f.rate, "limits the CPU of the model server"),
      field("“Controversial” counts as", f.cw, "0-1 of an “unsafe” answer"),
      field("Timeout per request (s)", f.timeout, "how long to wait for one answer before retrying (slow model: 600)"),
      h("label", { class: "switch ai-field" }, f.backlog, h("span", null, "Also analyse what was indexed before (newest first)")),
      h("div", { class: "adm-row wide" }, h("span", { class: "grow" }), f.save));
    form.onsubmit = e => { e.preventDefault(); saveAI(); };

    f.testIn = h("input", { type: "text", placeholder: "A torrent name, or an infohash of an indexed torrent…", "aria-label": "Text to test" });
    f.testBtn = h("button", { type: "submit", class: "btn-s" }, "Test now");
    f.testOut = h("div", { class: "ai-test", hidden: true });
    const tform = h("form", { class: "adm-form" }, f.testIn, f.testBtn);
    tform.onsubmit = e => { e.preventDefault(); testAI(); };

    f.hist = h("div", { class: "ai-hist" });
    f.reBtn = h("button", { type: "button", class: "btn-s" }, "Re-analyse everything");
    f.reBtn.onclick = async () => {
      if (!confirm("Forget every AI score and analyse everything again?\nYour show/hide decisions are kept. With many torrents this takes hours of model CPU.")) return;
      busy(f.reBtn, true, "…");
      try { await req("POST", "/api/admin/ai/reanalyse-all"); toast("Everything will be analysed again (newest first)."); loadAI(); }
      catch (e) { toast(e.message, true); } finally { busy(f.reBtn, false); }
    };

    /* list */
    f.state = sel(AI_STATES, AI.state, v => { AI.state = v; AI.page = 1; loadAIList(); }, "State");
    f.cat = h("select", { "aria-label": "Category" }); f.cat.onchange = () => { AI.cat = f.cat.value; AI.page = 1; loadAIList(); };
    f.min = h("input", { type: "number", min: "0", max: "100", value: "0", class: "ai-num", "aria-label": "Minimum confidence" });
    f.max = h("input", { type: "number", min: "0", max: "100", value: "100", class: "ai-num", "aria-label": "Maximum confidence" });
    const rng = () => { AI.min = +f.min.value || 0; AI.max = f.max.value === "" ? 100 : +f.max.value; AI.page = 1; loadAIList(); };
    f.min.onchange = rng; f.max.onchange = rng;
    f.q = h("input", { type: "search", placeholder: "Filter by name or infohash…", "aria-label": "Filter" });
    let qT; f.q.oninput = () => { clearTimeout(qT); qT = setTimeout(() => { AI.q = f.q.value.trim(); AI.page = 1; loadAIList(); }, 350); };
    const sort = sel([["score", "Highest confidence"], ["seeders", "Most seeders"], ["date", "Recently indexed"], ["size", "Size"], ["name", "Name"]],
      AI.sort, v => { AI.sort = v; AI.page = 1; loadAIList(); }, "Order");
    f.sum = h("div", { class: "muted small" });
    f.all = h("input", { type: "checkbox", "aria-label": "Select the whole page" });
    f.all.onchange = () => { f.list.querySelectorAll("input.ai-pick").forEach(i => { i.checked = f.all.checked; i.onchange(); }); };
    const bulk = (action, label, cls) => h("button", { type: "button", class: "btn-s " + (cls || ""), onclick: () => aiAction([...AI.sel], action) }, label);
    f.bulk = h("div", { class: "adm-row ai-bulk" }, h("label", { class: "switch" }, f.all, h("span", null, "Page")),
      f.selN = h("span", { class: "muted small" }, ""), h("span", { class: "grow" }),
      bulk("allow", "Show again", "primary"), bulk("hide", "Hide"), bulk("reset", "Back to AI verdict"), bulk("reanalyse", "Re-analyse"));
    f.list = h("div", { class: "adm-list" });
    f.pager = h("div", { class: "pager" });

    return h("section", { class: "tcard wide", id: "adm-ai" },
      h("h2", null, "AI moderation (NSFW / harmful)"),
      h("p", { class: "muted small" }, "A safety model reads the name and first file names of each torrent and gives a confidence that it is NSFW or harmful. ",
        "Above the threshold the torrent is ", h("b", null, "hidden"), " like with a rule: not deleted, you can show it again at any time. ",
        "The model runs in its own server (llama.cpp), so this service uses no extra RAM."),
      f.warn, f.status, form,
      h("h3", { class: "ai-sub" }, "Try it"), tform, f.testOut,
      h("h3", { class: "ai-sub" }, "Analysed torrents"),
      h("div", { class: "adm-row" }, f.hist, h("span", { class: "grow" }), f.reBtn),
      h("div", { class: "adm-row" }, f.state, f.cat, h("span", { class: "small muted" }, "confidence"), f.min, h("span", { class: "muted" }, "–"), f.max, f.q, sort),
      f.sum, f.bulk, f.list, f.pager);
  }

  function aiForm() {
    const f = E.ai;
    return { enabled: f.enabled.checked, endpoint: f.endpoint.value.trim(), profile: f.profile.value, model: f.model.value.trim(),
             api_key: f.key.value, api: f.api.value, threshold: +f.thr.value, files: +f.files.value, max_rate: +f.rate.value,
             controversial_weight: +f.cw.value, timeout: +f.timeout.value, backlog: f.backlog.checked,
             act_on: [...f.catsBox.querySelectorAll("input:checked")].map(i => i.value) };
  }
  function fillAIForm(c) {
    const f = E.ai;
    f.enabled.checked = c.enabled; f.endpoint.value = c.endpoint; f.profile.value = c.profile; f.model.value = c.model;
    f.key.value = c.api_key; f.api.value = c.api || "auto"; f.thr.value = c.threshold; f.thrNum.textContent = c.threshold + " %"; f.files.value = c.files;
    f.rate.value = c.max_rate; f.cw.value = c.controversial_weight; f.timeout.value = c.timeout; f.backlog.checked = c.backlog;
    f.catsBox.replaceChildren(...AI.cats.map(k => {
      const i = h("input", { type: "checkbox", value: k.key }); i.checked = c.act_on.includes(k.key);
      return h("label", { class: "switch" }, i, h("span", null, k.label));
    }));
    f.cat.replaceChildren(h("option", { value: "" }, "All categories"), ...AI.cats.map(k => h("option", { value: k.key }, k.label)));
    f.cat.value = AI.cat;
  }

  async function loadAI(refillForm) {
    if (!E.ai) return;
    let s;
    try { s = await req("GET", "/api/admin/ai"); }
    catch (e) { E.ai.status.textContent = e.status === 404 ? "Not available in this mode." : "⚠ " + e.message; return; }
    AI.cats = s.categories;
    if (refillForm || !AI.cfg) fillAIForm(s.config);
    AI.cfg = s.config;
    const f = E.ai;
    const st = { off: ["", "off"], idle: ["ok", "up to date"], working: ["ok", "analysing"] }[s.state] || ["", s.state];
    const err = s.last_error && s.last_error_at > (s.last_ok_at || 0) && s.last_error_at >= (s.saved_at || 0) && s.error_endpoint === s.config.endpoint;
    f.status.replaceChildren(
      h("span", { class: "chip " + (err ? "warn" : st[0]) }, err ? "error" : st[1]), " ",
      `${fmtNum(s.analysed)} analysed · ${fmtNum(s.pending)} pending · `, h("b", null, fmtNum(s.hidden)), " hidden by the AI · ",
      `${fmtNum(s.allowed)} shown again by you · ${fmtNum(s.manual)} hidden by you` +
      (s.avg_ms ? ` · ${Math.round(s.avg_ms)} ms per torrent` : "") + (s.errors ? ` · ${fmtNum(s.errors)} errors` : ""));
    const warns = [];
    if (err) warns.push("⚠ " + s.last_error + " (" + fmtRel(s.last_error_at) + ")" +
      (s.backoff_until > Date.now() / 1000 ? ` — retrying in ${Math.ceil(s.backoff_until - Date.now() / 1000)} s` : ""));
    if (s.logprobs === false && s.config.profile !== "chat") warns.push(s.api === "ollama"
      ? "Ollama did not return probabilities (logprobs): it needs Ollama 0.12 or newer, and a proxy in between (IARemote…) must forward the logprobs / top_logprobs fields of /api/generate and its answer. Meanwhile the confidence is only 0 %, the “controversial” weight or 100 %."
      : "The model server does not return probabilities (logprobs): the confidence is only 0 %, the “controversial” weight or 100 %. llama.cpp's llama-server returns them; for Ollama choose “Server API: Ollama” (a proxy in between must forward /api/generate).");
    f.warn.hidden = !warns.length; f.warn.replaceChildren(...warns.map(w => h("div", null, w)));
    const hist = s.histogram || [], max = Math.max(1, ...hist);
    f.hist.replaceChildren(h("span", { class: "small muted" }, "Confidence of what was analysed: "),
      ...hist.map((n, i) => h("span", { class: "ai-bar", title: `${i * 10}–${i * 10 + 9 + (i === 9 ? 1 : 0)} %: ${fmtNum(n)}`, onclick: () => { f.min.value = i * 10; f.max.value = i === 9 ? 100 : i * 10 + 9; f.state.value = AI.state = "all"; f.min.onchange(); } },
        h("i", { style: `height:${Math.max(2, Math.round(28 * n / max))}px` }))));
  }

  async function saveAI() {
    const f = E.ai;
    busy(f.save, true, "Saving…");
    try { const r = await req("POST", "/api/admin/ai/config", aiForm()); fillAIForm(r.config); AI.cfg = r.config; toast("AI settings saved."); loadAI(); loadAIList(); loadStatus(); }
    catch (e) { toast(e.message, true); } finally { busy(f.save, false); }
  }

  async function testAI() {
    const f = E.ai, v = f.testIn.value.trim();
    if (!v) { f.testIn.focus(); return; }
    busy(f.testBtn, true, `Asking the model… (up to ${f.timeout.value} s)`);
    const body = /^[0-9a-fA-F]{40}$/.test(v) ? { ih: v } : { text: v };
    try {
      const r = await req("POST", "/api/admin/ai/test", { ...body, config: aiForm() });
      f.testOut.hidden = false;
      f.testOut.replaceChildren(
        h("div", { class: "adm-row" }, scoreChip(r.score),
          r.would_hide ? h("span", { class: "chip warn" }, "would be hidden") : h("span", { class: "chip ok" }, "would stay visible"),
          ...r.cats.map(c => h("span", { class: "chip" }, catLabel(c))),
          h("span", { class: "muted small" }, `${r.ms} ms · ${r.api === "ollama" ? "Ollama API" : r.api === "openai" ? "OpenAI-compatible API" : ""}` + (r.logprobs ? " · with probabilities" : " · NO probabilities (logprobs)"))),
        h("div", { class: "mono small muted" }, "Model answer: " + r.raw),
        h("details", null, h("summary", { class: "small muted" }, "Text sent"), h("pre", { class: "mono small" }, r.text)));
    } catch (e) { f.testOut.hidden = false; f.testOut.replaceChildren(h("div", { class: "warn" }, "⚠ " + e.message)); }
    finally { busy(f.testBtn, false); }
  }

  async function loadAIList() {
    if (!E.ai) return;
    const f = E.ai, my = ++AI.seq;
    const p = new URLSearchParams({ state: AI.state, min: AI.min, max: AI.max, sort: AI.sort, page: AI.page, per_page: 20 });
    if (AI.cat) p.set("cat", AI.cat);
    if (AI.q) p.set("q", AI.q);
    let d;
    try { d = await req("GET", "/api/admin/ai/items?" + p); } catch (e) { if (e.status !== 404) f.list.replaceChildren(h("div", { class: "warn" }, "⚠ " + e.message)); return; }
    if (my !== AI.seq) return;
    AI.sel.clear(); f.all.checked = false; f.selN.textContent = "";
    f.sum.textContent = `${fmtNum(d.total)} torrents` + (d.truncated ? " (name filter applied to the 200,000 highest scores)" : "");
    f.bulk.hidden = !d.results.length;
    f.list.replaceChildren(...(d.results.length ? d.results.map(aiCard) : [h("div", { class: "muted adm-empty" },
      AI.cfg && !AI.cfg.enabled && AI.state === "hidden" ? "AI moderation is off: nothing is hidden by it." : "Nothing with these filters.")]));
    pager(f.pager, d, pg => { AI.page = pg; loadAIList(); $("#adm-ai").scrollIntoView({ block: "start" }); });
  }

  function aiCard(t) {
    const pick = h("input", { type: "checkbox", class: "ai-pick", "aria-label": "Select" });
    pick.onchange = () => { pick.checked ? AI.sel.add(t.ih) : AI.sel.delete(t.ih); E.ai.selN.textContent = AI.sel.size ? `${AI.sel.size} selected` : ""; };
    const act = (action, label, cls) => h("button", { type: "button", class: "btn-s " + (cls || ""), onclick: () => aiAction([t.ih], action) }, label);
    const chip = STATE_CHIP[t.state] || ["", t.state];
    return h("article", { class: "card adm-card ai-card" },
      h("h3", null, pick, " ", h("a", { class: "tlink", href: "/torrent/" + t.ih, target: "_blank", rel: "noopener", title: "Torrent page (hidden ones: visible only to you)" }, t.name)),
      h("div", { class: "row" },
        t.analysed ? scoreChip(t.score) : h("span", { class: "chip" }, "not analysed"),
        h("span", { class: "chip " + chip[0] }, chip[1]),
        ...t.cats.map(c => h("span", { class: "chip" }, catLabel(c))),
        t.error ? h("span", { class: "chip warn", title: "The model gave an unexpected answer for this one" }, "model error") : null,
        h("span", { class: "seeds " + (t.verified ? "ver" : "est") }, "Seeders ", h("b", null, (t.verified ? "" : "≥ ") + fmtNum(t.seeders))),
        h("span", null, "Size ", h("b", null, fmtBytes(t.size))), h("span", { class: "chip" }, t.category),
        h("span", { title: fmtDateTime(t.indexed_at) }, "Indexed ", fmtRel(t.indexed_at))),
      h("div", { class: "actions" },
        t.state === "allowed" ? act("reset", "Back to AI verdict") : act("allow", "Show again", t.state === "hidden" || t.state === "manual" ? "primary" : ""),
        t.state === "manual" ? act("reset", "Back to AI verdict") : t.state !== "hidden" ? act("hide", "Hide") : null,
        act("reanalyse", "Re-analyse"),
        h("button", { type: "button", class: "btn-s", onclick: e => copyText(t.ih, e.currentTarget) }, "Copy infohash")));
  }

  async function aiAction(ihs, action) {
    if (!ihs.length) { toast("Select some torrents first.", true); return; }
    try {
      const r = await req("POST", "/api/admin/ai/items", { ihs, action });
      toast({ allow: "Shown again", hide: "Hidden", reset: "Back to the AI's verdict", reanalyse: "Will be analysed again" }[action] + `: ${fmtNum(r.changed)} torrent${r.changed === 1 ? "" : "s"}.`);
      loadAI(); loadAIList(); loadStatus();
    } catch (e) { toast(e.message, true); }
  }

  function setRule(id) {
    A.rule = id; A.hpage = 1;
    E.ruleSel1.value = id; E.ruleSel2.value = id;
    loadHidden(); loadPeers();
  }
  function fillRuleSelects() {
    for (const s of [E.ruleSel1, E.ruleSel2]) {
      s.replaceChildren(h("option", { value: "" }, "All rules"),
        ...A.rules.map(r => h("option", { value: r.id }, `${r.term} (${fmtNum(r.count)})${r.enabled ? "" : " — paused"}`)));
      s.value = A.byId[A.rule] ? A.rule : "";
    }
    if (!A.byId[A.rule]) A.rule = "";
  }

  /* ---------------------------------------------------------------- status */
  async function loadStatus() {
    try {
      const s = await req("GET", "/api/admin/status");
      const p = s.peers || {};
      E.pwdWarn.hidden = !s.default_password;
      renderTrackers(s.trackers || []);
      E.status.textContent = `${fmtNum(s.hidden)} of ${fmtNum(s.torrents)} torrents hidden · ` +
        (p.enabled ? `${fmtNum(p.entries)} peers stored (${fmtNum(p.ips)} distinct IPs in ${fmtNum(p.torrents)} torrents, retention ${p.ttl_days} d)` : "peer logging DISABLED (--no-peers)") +
        (s.forced_pending ? ` · ${fmtNum(s.forced_pending)} requested probes queued` : "") +
        (s.recompute && s.recompute.at ? ` · rules applied ${fmtRel(s.recompute.at)} (${s.recompute.took_s} s)` : "");
    } catch {}
  }

  function renderTrackers(list) {
    if (!list.length) { E.trBox.replaceChildren("No tracker answer has arrived yet (or the crawler is not running). If this lasts more than a few minutes, something is wrong."); return; }
    const pct = x => x == null ? "—" : Math.round(x * 100) + " %";
    E.trBox.replaceChildren(h("div", { class: "tscroll" }, h("table", { class: "data" },
      h("thead", null, h("tr", null, ["Tracker", "Scrape OK", "Scrape failed", "Rate", "Announce OK / failed", "Last answer", "Last error"].map(x => h("th", null, x)))),
      h("tbody", null, list.slice(0, 30).map(t => h("tr", null,
        h("td", { class: "mono", title: t.url }, t.url + (t.configured ? "" : " (from the .torrent)")),
        h("td", null, fmtNum(t.scrape_ok)), h("td", { class: t.scrape_fail > t.scrape_ok ? "bad" : "" }, fmtNum(t.scrape_fail)),
        h("td", { class: t.scrape_rate != null && t.scrape_rate < 0.5 ? "bad" : "ok" }, pct(t.scrape_rate)),
        h("td", null, `${fmtNum(t.announce_ok)} / ${fmtNum(t.announce_fail)}`),
        h("td", null, fmtRel(t.last_ok)), h("td", { class: "bad", title: t.last_error }, t.last_error ? `${t.last_error.slice(0, 50)} (${fmtRel(t.last_fail)})` : "—")))))));
  }

  /* ---------------------------------------------------------------- rules */
  async function loadRules() {
    const r = await req("GET", "/api/admin/rules");
    A.rules = r.rules; A.byId = Object.fromEntries(r.rules.map(x => [x.id, x]));
    fillRuleSelects();
    if (!r.rules.length) { E.rulesBox.replaceChildren(h("div", { class: "muted small adm-empty" }, "No rules yet: search shows everything indexed.")); return; }
    E.rulesBox.replaceChildren(h("div", { class: "tscroll" }, h("table", { class: "data adm-rules" },
      h("thead", null, h("tr", null, ["Term", "Where", "Match", "Hidden", "Created", "Status", ""].map(x => h("th", null, x)))),
      h("tbody", null, r.rules.map(rule => {
        const toggle = h("button", { type: "button", class: "btn-s" }, rule.enabled ? "Pause" : "Enable");
        toggle.onclick = async () => {
          busy(toggle, true, "Applying…");
          try { await req("PATCH", "/api/admin/rules/" + rule.id, { enabled: !rule.enabled }); await refreshAll(); }
          catch (e) { toast(e.message, true); busy(toggle, false); }
        };
        const del = h("button", { type: "button", class: "btn-s danger" }, "Delete");
        del.onclick = async () => {
          if (!confirm(`Delete the rule “${rule.term}”?\nThe ${rule.count} torrents it hides will show up in search again (unless another rule covers them).`)) return;
          busy(del, true, "…");
          try { await req("DELETE", "/api/admin/rules/" + rule.id); if (A.rule === rule.id) A.rule = ""; await refreshAll(); }
          catch (e) { toast(e.message, true); busy(del, false); }
        };
        const see = h("button", { type: "button", class: "btn-s" }, "Show hidden");
        see.onclick = () => { setRule(rule.id); $("#adm-hidden").scrollIntoView({ behavior: "smooth", block: "start" }); };
        return h("tr", { class: rule.enabled ? "" : "off" },
          h("td", null, h("span", { class: "mono" }, rule.term)), h("td", null, lbl(SCOPES, rule.scope)), h("td", null, lbl(MODES, rule.mode)),
          h("td", null, h("b", null, fmtNum(rule.count))), h("td", { title: fmtDateTime(rule.created) }, fmtRel(rule.created)),
          h("td", null, rule.enabled ? h("span", { class: "chip ok" }, "active") : h("span", { class: "chip warn" }, "paused")),
          h("td", { class: "adm-acts" }, see, toggle, del));
      })))));
  }
  function ruleBody() { return { term: E.term.value.trim(), scope: E.scope.value, mode: E.mode.value }; }
  async function previewRule() {
    const b = ruleBody();
    if (!b.term) { E.term.focus(); return; }
    busy(E.prevBtn, true, "Computing…");
    try {
      const r = await req("POST", "/api/admin/rules/preview", b);
      E.preview.hidden = false;
      E.preview.replaceChildren(
        h("div", null, h("b", null, fmtNum(r.count)), ` torrents match` + (r.already_hidden ? ` (${fmtNum(r.already_hidden)} were already hidden)` : "") + (r.count ? ". Those with most seeders:" : ".")),
        r.sample.length ? h("ul", { class: "adm-sample" }, r.sample.map(x => h("li", null, h("a", { class: "tlink", href: "/torrent/" + x.ih, target: "_blank", rel: "noopener" }, x.name), h("span", { class: "muted" }, ` · ${fmtNum(x.seeders)} seeders`)))) : null);
    } catch (e) { E.preview.hidden = false; E.preview.replaceChildren(h("div", { class: "warn" }, "⚠ " + e.message)); }
    finally { busy(E.prevBtn, false); }
  }
  async function addRule() {
    const b = ruleBody();
    if (!b.term) { E.term.focus(); return; }
    busy(E.addBtn, true, "Applying…");
    try {
      const r = await req("POST", "/api/admin/rules", b);
      E.term.value = ""; E.preview.hidden = true;
      toast(`Rule “${r.rule.term}” created: ${fmtNum(r.rule.count)} torrents hidden (${r.took_s} s).`);
      A.rule = r.rule.id; A.hpage = 1;
      await refreshAll();
    } catch (e) { toast(e.message, true); }
    finally { busy(E.addBtn, false); }
  }

  /* ---------------------------------------------------------------- hidden */
  let hSeq = 0;
  async function loadHidden() {
    const my = ++hSeq;
    const p = new URLSearchParams({ page: A.hpage, per_page: 15, sort: A.hsort });
    if (A.rule) p.set("rule", A.rule);
    if (A.hq) p.set("q", A.hq);
    let d;
    try { d = await req("GET", "/api/admin/hidden?" + p); } catch (e) { E.hlist.replaceChildren(h("div", { class: "warn" }, "⚠ " + e.message)); return; }
    if (my !== hSeq) return;
    E.hsum.textContent = d.total ? `${fmtNum(d.total)} hidden torrents${A.rule ? " by “" + ruleName(A.rule) + "”" : ""}${A.hq ? " matching the filter" : ""}` : "";
    E.refreshBtn.hidden = !d.total;
    E.hlist.replaceChildren(...(d.results.length ? d.results.map(hiddenCard) : [h("div", { class: "muted adm-empty" }, A.rules.length ? "Nothing hidden with this filter." : "Create a rule to hide content.")]));
    pager(E.hpager, d, pg => { A.hpage = pg; loadHidden(); $("#adm-hidden").scrollIntoView({ block: "start" }); });
  }
  function hiddenCard(t) {
    const peersSlot = h("div", { class: "adm-peers-slot", hidden: true });
    const peersBtn = h("button", { type: "button", class: "btn-s" }, `Show peers (${fmtNum(t.known_peers)})`);
    peersBtn.onclick = async () => {
      if (!peersSlot.hidden) { peersSlot.hidden = true; peersBtn.textContent = `Show peers (${fmtNum(t.known_peers)})`; return; }
      peersSlot.hidden = false; peersBtn.textContent = "Close peer list";
      peersSlot.replaceChildren(h("div", { class: "muted small" }, "Loading…"));
      try {
        const r = await req("GET", "/api/admin/torrent/" + t.ih);
        const ips = r.peers.map(p => p.ip);
        peersSlot.replaceChildren(peerTable(r.peers, searchIp),
          ips.length ? h("div", { class: "adm-row" }, h("button", { type: "button", class: "btn-s primary", onclick: () => searchPeers({ ips: ips.join(" ") }, "peers of “" + t.name + "”") }, "Search these peers in other torrents")) : null);
      } catch (e) { peersSlot.replaceChildren(h("div", { class: "warn" }, "⚠ " + e.message)); }
    };
    const probe = h("button", { type: "button", class: "btn-s", title: "Reconnect to its swarm now to update seeders/peers" }, "Probe peers now");
    probe.onclick = async () => {
      busy(probe, true, "…");
      try { const r = await req("POST", "/api/admin/refresh", { ihs: [t.ih] }); toast(r.queued ? "Queued: the crawler will probe it as soon as it has a free slot." : "It was already queued."); loadStatus(); }
      catch (e) { toast(e.message, true); } finally { busy(probe, false); }
    };
    const why = (t.matches || []).map(m => h("div", { class: "adm-why" },
      h("span", { class: "chip rule" }, "“" + ruleName(m.rule) + "”"),
      m.in_name ? h("span", { class: "small" }, "in the name") : null,
      m.files.length ? h("div", { class: "adm-files" },
        m.files.map(f => h("div", { class: "mf", title: f.path }, h("span", null, spanNodes(f.path, f.spans)), h("i", null, fmtBytes(f.size)))),
        m.files_more ? h("div", { class: "muted small" }, "… and more files") : null) : null));
    const nameSpans = (t.matches || []).flatMap(m => m.name_spans || []);
    return h("article", { class: "card adm-card" },
      h("h3", null, h("a", { class: "tlink", href: "/torrent/" + t.ih, target: "_blank", rel: "noopener", title: "Torrent page (visible only to you)" }, spanNodes(t.name, nameSpans))),
      h("div", { class: "row" },
        h("span", { class: "seeds " + (t.verified ? "ver" : "est") }, "Seeders ", h("b", null, (t.verified ? "" : "≥ ") + fmtNum(t.seeders))),
        h("span", null, "Peers ", h("b", null, fmtNum(t.peers))),
        h("span", null, "Peers with known IP ", h("b", null, fmtNum(t.known_peers))),
        h("span", null, "Size ", h("b", null, fmtBytes(t.size))),
        h("span", { class: "chip" }, t.category),
        ["dead", "quiet", "weak"].includes(t.state) ? h("span", { class: "chip st-" + t.state }, STATE_LABEL[t.state]) : null,
        h("span", { title: fmtDateTime(t.indexed_at) }, "Indexed ", fmtRel(t.indexed_at))),
      why,
      h("div", { class: "actions" }, peersBtn, probe, h("button", { type: "button", class: "btn-s", onclick: e => copyText(t.ih, e.currentTarget) }, "Copy infohash")),
      peersSlot);
  }

  /* ---------------------------------------------------------------- peers of hidden content */
  let pSeq = 0;
  async function loadPeers() {
    const my = ++pSeq;
    const p = new URLSearchParams();
    if (A.rule) p.set("rule", A.rule);
    if (A.pSeeds) p.set("seeds", "1");
    if (A.pElse) p.set("elsewhere", "1");
    let d;
    try { d = await req("GET", "/api/admin/peers?" + p); } catch (e) { E.pbox.replaceChildren(h("div", { class: "warn" }, "⚠ " + e.message)); return; }
    if (my !== pSeq) return;
    if (!d.peers_enabled) { E.psum.textContent = ""; E.pbox.replaceChildren(h("div", { class: "warn" }, "Peer logging is disabled (--no-peers).")); return; }
    E.psum.textContent = d.torrents ? `${fmtNum(d.total)} IPs in ${fmtNum(d.torrents_with_peers)} of ${fmtNum(d.torrents)} hidden torrents with recorded peers` + (d.truncated ? " · showing 2,000" : "") : "";
    if (!d.results.length) {
      E.pbox.replaceChildren(h("div", { class: "muted small adm-empty" }, !d.torrents ? "Nothing is hidden." :
        d.torrents_with_peers ? "No peer matches these filters." :
        "No peers seen for these torrents yet. They are recorded every time the crawler probes them (alive ones every 6 h). Click \"Probe peers now\" in the section above to probe them right away."));
      return;
    }
    E.pbox.replaceChildren(h("div", { class: "tscroll tall" }, h("table", { class: "data peers" },
      h("thead", null, h("tr", null, ["IP", "Ports", "Role", "In hidden", "Elsewhere", "Client", "Source", "Last seen"].map(x => h("th", null, x)))),
      h("tbody", null, d.results.map(r => h("tr", null,
        h("td", null, h("button", { type: "button", class: "iplink", title: "Search this IP in other torrents", onclick: () => searchIp(r.ip) }, r.ip)),
        h("td", { class: "mono" }, r.ports.join(", ") || "—"),
        h("td", null, roleChip(r.role)),
        h("td", { title: `seeder in ${r.seed_in}` }, fmtNum(r.in_set), r.seed_in ? h("span", { class: "muted" }, ` (${fmtNum(r.seed_in)} as seeder)`) : null),
        h("td", null, r.elsewhere ? h("button", { type: "button", class: "iplink strong", title: "See which other torrents it is in", onclick: () => searchIp(r.ip) }, fmtNum(r.elsewhere) + " →") : h("span", { class: "muted" }, "0")),
        h("td", null, r.clients.join(", ") || "—"), h("td", { class: "muted" }, srcText(r.src)),
        h("td", { title: fmtDateTime(r.last) }, fmtRel(r.last))))))));
  }

  /* ---------------------------------------------------------------- peer search */
  function searchIp(ip) { E.ips.value = ip; searchPeers({ ips: ip }, null); }
  let sSeq = 0;
  async function searchPeers(body, label) {
    const my = ++sSeq;
    if (body.ips !== undefined && label) E.ips.value = body.ips.split(/\s+/).length > 50 ? "" : body.ips;
    E.searchCard.scrollIntoView({ behavior: "smooth", block: "start" });
    E.ssum.textContent = "Searching…"; E.sres.replaceChildren();
    let d;
    try { d = await req("POST", "/api/admin/peer-search", { ...body, seeds: E.sSeeds, include_hidden: E.sHidden }); }
    catch (e) { E.ssum.textContent = ""; E.sres.replaceChildren(h("div", { class: "warn" }, "⚠ " + e.message + (e.data && e.data.invalid && e.data.invalid.length ? " — invalid: " + e.data.invalid.join(", ") : ""))); return; }
    if (my !== sSeq) return;
    E.ssum.textContent = `${label ? label[0].toUpperCase() + label.slice(1) + ": " : ""}${fmtNum(d.searched)} IPs/networks searched · ${fmtNum(d.ips_with_hits)} appear in ${fmtNum(d.total)} ${E.sHidden ? "" : "non-hidden "}torrents` +
      (d.invalid && d.invalid.length ? ` · invalid: ${d.invalid.join(", ")}` : "") + (d.truncated ? " · showing 500" : "");
    if (!d.results.length) {
      E.sres.replaceChildren(h("div", { class: "muted adm-empty" }, E.sHidden ? "Those IPs do not appear in any torrent." : "Those IPs do not appear in any torrent outside already hidden content."));
      return;
    }
    const card = t => h("article", { class: "card adm-card" },
      h("h3", null, h("a", { class: "tlink", href: "/torrent/" + t.ih, target: "_blank", rel: "noopener" }, t.name)),
      h("div", { class: "row" },
        h("span", null, h("b", null, fmtNum(t.match_count)), ` matching peer${t.match_count === 1 ? "" : "s"}`, t.match_seeds ? ` (${fmtNum(t.match_seeds)} as seeder)` : ""),
        t.hidden_by.length ? h("span", { class: "chip warn", title: "Already hidden by: " + t.hidden_by.map(ruleName).join(", ") }, "hidden") : h("span", { class: "chip ok" }, "visible in search"),
        h("span", null, "Seeders ", h("b", null, (t.verified ? "" : "≥ ") + fmtNum(t.seeders))), h("span", null, "Peers ", h("b", null, fmtNum(t.peers))),
        h("span", null, "Size ", h("b", null, fmtBytes(t.size))), h("span", { class: "chip" }, t.category)),
      h("div", { class: "adm-ips" }, t.matched.map(m => h("button", { type: "button", class: "chip ipchip r" + m.role, title: `${ROLE_LABEL[m.role]} · seen ${fmtRel(m.last)} · click: search this IP`, onclick: () => searchIp(m.ip) }, m.ip, m.role === 1 ? " ⬆" : "")),
        t.match_count > t.matched.length ? h("span", { class: "muted small" }, ` … and ${t.match_count - t.matched.length} more`) : null),
      h("div", { class: "actions" },
        h("button", { type: "button", class: "btn-s", title: "Prepares a rule with this torrent's name", onclick: () => { E.term.value = t.name; E.mode.value = "substring"; E.scope.value = "name"; $("#view-admin").scrollIntoView({ behavior: "smooth" }); E.term.focus(); } }, "Hide this…"),
        h("button", { type: "button", class: "btn-s", onclick: e => copyText(t.ih, e.currentTarget) }, "Copy infohash")));
    const PAGE = 30;
    let shown = 0;
    const more = h("button", { type: "button", class: "btn-s adm-more" });
    const showMore = () => {
      const next = d.results.slice(shown, shown + PAGE);
      shown += next.length;
      more.before(...next.map(card));
      more.hidden = shown >= d.results.length;
      more.textContent = `Show ${Math.min(PAGE, d.results.length - shown)} more (${fmtNum(d.results.length - shown)} left)`;
    };
    more.onclick = showMore;
    E.sres.replaceChildren(more);
    showMore();
  }

  function pager(box, d, go) {
    box.replaceChildren();
    if (d.pages <= 1) return;
    const cur = d.page;
    box.append(h("button", { disabled: cur === 1, onclick: () => go(cur - 1) }, "‹"));
    for (let p = Math.max(1, cur - 3); p <= Math.min(d.pages, cur + 3); p++) box.append(h("button", { class: p === cur ? "on" : "", onclick: () => go(p) }, p));
    box.append(h("button", { disabled: cur === d.pages, onclick: () => go(cur + 1) }, "›"));
  }

  async function refreshAll() {
    try { await loadRules(); } catch (e) { return; }
    loadStatus(); loadHidden(); loadPeers(); loadAI(true); loadAIList();
  }

  window.onAdminTab = () => {
    build(); refreshAll();
    let ip = null;
    try { ip = sessionStorage.getItem("admSearchIp"); sessionStorage.removeItem("admSearchIp"); } catch {}
    if (ip) setTimeout(() => searchIp(ip), 300);
    clearInterval(A.statusTimer);
    A.statusTimer = setInterval(() => { if (!document.hidden && currentTab === "admin") { loadStatus(); loadAI(); } else if (currentTab !== "admin") clearInterval(A.statusTimer); }, 10000);
  };
})();
