"use strict";
/* app.js — search and analytics (shared helpers live in common.js) */

/* ---------- tabs ---------- */
window.INITIAL_HASH = location.hash;              // setTab rewrites it; admin.js needs it to reopen #admin
let currentTab = "search";
window.adminUnlocked = false;                     // set by admin.js after checking the session on the server
function setTab(tab) {
  currentTab = tab === "stats" ? "stats" : tab === "admin" && window.adminUnlocked ? "admin" : "search";
  for (const b of document.querySelectorAll(".tabs button")) b.setAttribute("aria-selected", b.dataset.tab === currentTab);
  $("#view-search").hidden = currentTab !== "search";
  $("#view-stats").hidden = currentTab !== "stats";
  $("#view-admin").hidden = currentTab !== "admin";
  if (location.hash.slice(1) !== currentTab && !(currentTab === "search" && !location.hash)) history.replaceState(null, "", location.pathname + location.search + "#" + currentTab);
  onTabChange();
}
for (const b of document.querySelectorAll(".tabs button")) b.onclick = () => setTab(b.dataset.tab);
addEventListener("hashchange", () => setTab(location.hash.slice(1)));

/* =====================================================================
   SEARCH
   ===================================================================== */
const DEFAULT_S = { q: "", page: 1, sort: "relevance", order: "", per_page: 20, cat: "", ext: "", min_size: 0, max_size: 0,
                    min_seeds: 0, min_files: 0, max_files: 0, age: "", scope: "", health: "" };
const S = { ...DEFAULT_S };
const SORTS = [["relevance", "Relevance"], ["seeders", "Seeders"], ["peers", "Peers"], ["size", "Size"], ["files", "No. of files"],
               ["date", "Newest (indexed)"], ["name", "Name (A-Z)"]];
const MB = 2 ** 20, GB = 2 ** 30;
const SIZE_FACETS = { "< 1 MB": [0, MB - 1], "1–100 MB": [MB, 100 * MB - 1], "100 MB–1 GB": [100 * MB, GB - 1],
                      "1–10 GB": [GB, 10 * GB - 1], "10–50 GB": [10 * GB, 50 * GB - 1], "> 50 GB": [50 * GB, 0] };
const AGES = [["", "Any date"], ["24h", "Last 24 h"], ["7d", "Last 7 days"], ["30d", "Last 30 days"], ["90d", "Last 90 days"]];
const HEALTHS = [["", "Any"], ["alive", "Alive (has seeders)"], ["weak", "Weak (peers only)"], ["quiet", "No activity (unconfirmed)"],
                 ["dead", "Dead (confirmed)"], ["unknown", "Not measured"], ["verified", "Verified with a tracker"]];
const SCOPES = [["", "Name and files"], ["name", "Name only"], ["files", "Files only"]];
const HELP = [
  ["ubuntu desktop", "all words"], ['"the matrix"', "exact phrase"], ["debian OR ubuntu", "alternatives"], ["linux -server", "exclude a word"],
  ["ext:mkv,mp4", "with files of those extensions (-ext:srt excludes)"], ["cat:video", "category: video, audio, images, docs, software, archives, data"],
  ["size>1gb", "size (size<700mb · size:1gb..4gb)"], ["seeders>=10", "seeders (peers>5 too)"], ["files>100", "number of files (files:10..50)"],
  ["age<7d", "indexed less than … ago (h, d, w, mo, y) · age>30d = older"], ["in:name", "search the name only (in:files: files only)"],
  ["name:matrix file:sunset", "a word only in the name / only in the files"], ["health:alive", "state: alive · weak · quiet · dead (confirmed) · unknown · verified"], ["hash:6f3a9c", "by infohash (≥ 6 characters)"],
];
let searchSeq = 0, lastFacets = null, lastSearch = null;

function isHome() { return !S.q.trim() && !hasFilters(); }
// category names used in links before 2.9 (Spanish) -> current names
const LEGACY_CAT = { "Vídeo": "Video", "Imágenes": "Images", "Documentos": "Documents", "Comprimidos": "Archives", "Datos": "Data", "Otros": "Other" };
function hasFilters() { return !!(S.cat || S.ext || S.min_size || S.max_size || S.min_seeds || S.min_files || S.max_files || S.age || S.scope || S.health); }

function toParams(forApi) {
  const p = new URLSearchParams();
  const put = (k, v) => { if (v !== "" && v != null && v !== 0 && v !== false) p.set(k, v); };
  put("q", S.q.trim()); put("cat", S.cat); put("ext", S.ext); put("min_size", S.min_size); put("max_size", S.max_size);
  put("min_seeds", S.min_seeds); put("min_files", S.min_files); put("max_files", S.max_files); put("age", S.age);
  put("scope", S.scope); put("health", S.health);
  if (S.sort !== "relevance") p.set("sort", S.sort);
  put("order", S.order);
  if (forApi) { p.set("per_page", S.per_page); p.set("page", S.page); if (S.page > 1) p.set("facets", "0"); }
  else { if (S.per_page !== DEFAULT_S.per_page) p.set("per_page", S.per_page); if (S.page > 1) p.set("page", S.page); }
  return p;
}
function fromParams(p) {
  Object.assign(S, DEFAULT_S);
  S.q = p.get("q") || ""; S.cat = LEGACY_CAT[p.get("cat")] || p.get("cat") || ""; S.ext = p.get("ext") || ""; S.age = p.get("age") || "";
  S.scope = p.get("scope") || ""; S.health = p.get("health") || ""; S.sort = p.get("sort") || "relevance"; S.order = p.get("order") || "";
  for (const k of ["min_size", "max_size", "min_seeds", "min_files", "max_files"]) S[k] = Math.max(0, +p.get(k) || 0);
  S.page = Math.max(1, +p.get("page") || 1); S.per_page = [10, 20, 30, 50].includes(+p.get("per_page")) ? +p.get("per_page") : DEFAULT_S.per_page;
}
function pushUrl(replace) {
  const qs = toParams(false).toString();
  const url = location.pathname + (qs ? "?" + qs : "") + (location.hash || "");
  if (url !== location.pathname + location.search + location.hash) history[replace ? "replaceState" : "pushState"](null, "", url);
}
function newSearch(opts = {}) {                 // any query/filter change goes back to page 1
  if (!opts.keepPage) S.page = 1;
  syncForm(); pushUrl(!!opts.replace); doSearch();
}

/* ---------- advanced options panel ---------- */
const F = {};                                    // references to the controls
function field(label, control, wide) { return h("label", { class: "fld" + (wide ? " wide" : "") }, h("span", null, label), control); }
function select(id, opts, onchange) {
  const s = h("select", { id }, opts.map(([v, l]) => h("option", { value: v }, l)));
  s.onchange = onchange; F[id] = s; return s;
}
function numInput(id, ph, onchange) {
  const i = h("input", { id, type: "number", min: "0", placeholder: ph, inputmode: "numeric" }); i.onchange = onchange; F[id] = i; return i;
}
function buildAdvanced() {
  const cats = h("select", { id: "f-cat" }, h("option", { value: "" }, "All")); F["f-cat"] = cats;
  cats.onchange = () => { S.cat = cats.value; newSearch(); };
  api("/api/categories").then(l => { for (const c of l) cats.append(h("option", { value: c }, c)); syncForm(); }).catch(() => {});
  const ext = h("input", { id: "f-ext", placeholder: "mkv, mp4, flac…" }); F["f-ext"] = ext;
  ext.onchange = () => { S.ext = ext.value.trim().replace(/\s+/g, ","); newSearch(); };
  const unit = id => { const u = h("select", { id, class: "unit" }, [["1048576", "MB"], ["1073741824", "GB"], ["1099511627776", "TB"]].map(([v, l]) => h("option", { value: v }, l))); u.value = "1073741824"; F[id] = u; return u; };
  const size = (which) => {
    const n = numInput("f-" + which + "size", which === "min" ? "min" : "max", () => { S[which + "_size"] = Math.round((+F["f-" + which + "size"].value || 0) * +F["u-" + which].value); newSearch(); });
    const u = unit("u-" + which); u.onchange = n.onchange;
    return h("span", { class: "pair" }, n, u);
  };
  const files = h("span", { class: "pair" },
    numInput("f-minfiles", "min", () => { S.min_files = +F["f-minfiles"].value || 0; newSearch(); }),
    numInput("f-maxfiles", "max", () => { S.max_files = +F["f-maxfiles"].value || 0; newSearch(); }));
  const order = h("button", { type: "button", class: "btn-s", id: "f-order", title: "Reverse the order" }, "↓");
  order.onclick = () => { S.order = orderIsAsc() ? "desc" : "asc"; newSearch(); }; F["f-order"] = order;
  const reset = h("button", { type: "button", class: "btn-s" }, "Reset filters");
  reset.onclick = () => { const q = S.q, sort = S.sort, pp = S.per_page; Object.assign(S, DEFAULT_S, { q, sort, per_page: pp }); newSearch(); };
  $("#adv").replaceChildren(
    field("Category", cats), field("Extension", ext), field("Search in", select("f-scope", SCOPES, () => { S.scope = F["f-scope"].value; newSearch(); })),
    field("Min size", size("min")), field("Max size", size("max")),
    field("Min seeders", numInput("f-seeds", "0", () => { S.min_seeds = +F["f-seeds"].value || 0; newSearch(); })),
    field("No. of files", files),
    field("Indexed", select("f-age", AGES, () => { S.age = F["f-age"].value; newSearch(); })),
    field("Health", select("f-health", HEALTHS, () => { S.health = F["f-health"].value; newSearch(); })),
    field("Sort by", h("span", { class: "pair" }, select("f-sort", SORTS, () => { S.sort = F["f-sort"].value; S.order = ""; newSearch(); }), order)),
    field("Per page", select("f-pp", [[10, "10"], [20, "20"], [30, "30"], [50, "50"]], () => { S.per_page = +F["f-pp"].value; newSearch(); })),
    h("div", { class: "fld actions-end" }, reset));
  const help = h("div", { class: "helpbox", id: "help", hidden: true },
    h("p", null, "Type operators right in the search box (click an example to try it):"),
    h("div", { class: "helpgrid" }, HELP.map(([ex, txt]) => h("div", { class: "hrow" },
      h("button", { type: "button", class: "code", onclick: () => { $("#q").value = (($("#q").value.trim() + " " + ex).trim()); S.q = $("#q").value; newSearch(); } }, ex), h("span", null, txt)))));
  $("#helpwrap").append(help);
}
function orderIsAsc() { return S.order ? S.order === "asc" : S.sort === "name"; }
function syncForm() {
  $("#q").value = S.q;
  const set = (id, v) => { if (F[id] && document.activeElement !== F[id]) F[id].value = v; };
  set("f-cat", S.cat); set("f-ext", S.ext.replace(/,/g, ", ")); set("f-scope", S.scope); set("f-age", S.age); set("f-health", S.health);
  set("f-sort", S.sort); set("f-pp", S.per_page); set("f-seeds", S.min_seeds || ""); set("f-minfiles", S.min_files || ""); set("f-maxfiles", S.max_files || "");
  for (const w of ["min", "max"]) {
    const v = S[w + "_size"], u = v >= 2 ** 40 ? 2 ** 40 : v >= GB ? GB : v >= MB ? MB : GB;
    if (F["u-" + w] && document.activeElement !== F["f-" + w + "size"]) { F["u-" + w].value = String(u); F["f-" + w + "size"].value = v ? +(v / u).toFixed(2) : ""; }
  }
  if (F["f-order"]) F["f-order"].textContent = orderIsAsc() ? "↑" : "↓";
  const n = activeChips().length;
  $("#adv-toggle").textContent = "Advanced options" + (n ? ` (${n})` : "") + ($("#advwrap").hidden ? " ▾" : " ▴");
}
$("#adv-toggle").onclick = () => { $("#advwrap").hidden = !$("#advwrap").hidden; store.set("advOpen", $("#advwrap").hidden ? "0" : "1"); syncForm(); };
$("#help-toggle").onclick = () => { $("#help").hidden = !$("#help").hidden; };

/* ---------- active filter chips ---------- */
function sizeLabel(v) { return fmtBytes(v); }
function activeChips() {
  const c = [], add = (label, clear) => c.push({ label, clear });
  if (S.cat) add("Category: " + S.cat, () => S.cat = "");
  if (S.ext) add("Extension: " + S.ext.split(",").map(e => "." + e).join(", "), () => S.ext = "");
  if (S.min_size || S.max_size) add("Size: " + (S.min_size ? sizeLabel(S.min_size) : "0") + " – " + (S.max_size ? sizeLabel(S.max_size) : "∞"), () => { S.min_size = 0; S.max_size = 0; });
  if (S.min_seeds) add("Seeders ≥ " + fmtNum(S.min_seeds), () => S.min_seeds = 0);
  if (S.min_files || S.max_files) add("Files: " + fmtNum(S.min_files) + " – " + (S.max_files ? fmtNum(S.max_files) : "∞"), () => { S.min_files = 0; S.max_files = 0; });
  if (S.age) add("Indexed: " + (AGES.find(a => a[0] === S.age) || [, S.age])[1], () => S.age = "");
  if (S.scope) add("Only " + (S.scope === "name" ? "name" : "files"), () => S.scope = "");
  if (S.health) add("Health: " + (HEALTHS.find(a => a[0] === S.health) || [, S.health])[1], () => S.health = "");
  return c;
}

/* ---------- search ---------- */
async function doSearch() {
  const seq = ++searchSeq;
  document.body.classList.toggle("searching", !isHome());
  let data;
  try { data = await api("/api/search?" + toParams(true)); }
  catch (e) { $("#results").replaceChildren(h("div", { class: "empty" }, "Could not reach the server.")); return; }
  if (seq !== searchSeq) return;                  // a newer search arrived
  lastSearch = data;
  if (data.facets) lastFacets = data.facets;
  renderSummary(data);
  renderFacets();
  const box = $("#results");
  box.replaceChildren();
  if (!data.results.length) {
    box.append(h("div", { class: "empty" }, data.suggestion ? null : (isHome()
      ? "No torrents indexed yet. The crawler is discovering infohashes and downloading their metadata; come back in a few minutes."
      : "No results. Try fewer words or remove a filter."), data.suggestion ? "No results." : null));
    $("#pager").replaceChildren();
    return;
  }
  for (const r of data.results) box.append(resultCard(r));
  renderPager(data);
}

function renderSummary(d) {
  const box = $("#summary");
  const sortSel = h("select", { "aria-label": "Sort" }, SORTS.filter(([v]) => v !== "relevance" || d.terms.length).map(([v, l]) => h("option", { value: v }, l)));
  sortSel.value = d.sort;
  sortSel.onchange = () => { S.sort = sortSel.value; S.order = ""; newSearch(); };
  const dir = h("button", { type: "button", class: "btn-s", title: "Reverse the order" }, orderIsAsc() ? "↑" : "↓");
  dir.onclick = () => { S.order = orderIsAsc() ? "desc" : "asc"; newSearch(); };
  const chips = activeChips().map(c => h("button", { type: "button", class: "chip x", title: "Remove this filter", onclick: () => { c.clear(); newSearch(); } }, c.label, " ✕"));
  const inq = (d.applied || []).map(a => h("span", { class: "chip q", title: "Typed in the search itself" }, a));
  const parts = [
    h("div", { class: "sumrow" },
      h("span", { class: "meta" }, d.total ? [h("b", null, fmtNum(d.total)), ` result${d.total === 1 ? "" : "s"} · ${d.took_ms} ms${d.cached ? " (cached)" : ""}${isHome() ? " · most popular" : ""}`] : ""),
      h("span", { class: "grow" }), sortSel, dir)];
  if (chips.length || inq.length) parts.push(h("div", { class: "chips" }, chips, inq));
  if (d.suggestion) parts.push(h("div", { class: "dym" }, "Did you mean: ", h("a", { href: "#", onclick: e => { e.preventDefault(); S.q = d.suggestion; newSearch(); } }, d.suggestion), "?"));
  for (const w of d.warnings || []) parts.push(h("div", { class: "warn" }, "⚠ " + w));
  box.replaceChildren(...parts);
  if (F["f-sort"] && document.activeElement !== F["f-sort"]) F["f-sort"].value = d.sort;
}

function facetGroup(title, items, active, onclick, label) {
  const shown = items.filter(i => i.count > 0 || active(i.name));
  if (!shown.length) return null;
  return h("div", { class: "fgroup" }, h("span", { class: "flabel" }, title),
    shown.map(i => h("button", { type: "button", class: "chip f" + (active(i.name) ? " on" : ""), title: STATE_HELP[i.name] || null, onclick: () => onclick(i.name) }, label ? label(i.name) : i.name, " ", h("i", null, fmtNum(i.count)))));
}
function renderFacets() {
  const f = lastFacets, box = $("#facets");
  if (!f || (lastSearch && !lastSearch.total && !hasFilters())) { box.replaceChildren(); return; }
  const sizeActive = n => { const r = SIZE_FACETS[n]; return r && S.min_size === r[0] && S.max_size === r[1]; };
  box.replaceChildren(
    facetGroup("Category", f.categories, n => S.cat === n, n => { S.cat = S.cat === n ? "" : n; newSearch(); }),
    facetGroup("Extension", f.extensions, n => S.ext === n, n => { S.ext = S.ext === n ? "" : n; newSearch(); }),
    f.states ? facetGroup("Health", f.states.map(x => ({ name: x.name, count: x.count })), n => S.health === n, n => { S.health = S.health === n ? "" : n; newSearch(); }, n => STATE_LABEL[n] || n) : null,
    facetGroup("Size", f.sizes, sizeActive, n => { const r = SIZE_FACETS[n]; if (sizeActive(n)) { S.min_size = 0; S.max_size = 0; } else { S.min_size = r[0]; S.max_size = r[1]; } newSearch(); }));
}

function seedsNode(r) {
  const cs = r.hd ? r.hd.cs : 0;
  const old = r.checked_at && r.checked_at - r.health_at > 3600 ? ` · figure from ${fmtRel(r.health_at)}; the last check (${fmtRel(r.checked_at)}) got no answer from any tracker` : "";
  if (r.verified) return h("span", { class: "seeds ver", title: `Highest figure reported by a tracker (${fmtRel(r.health_at)})` +
      (cs ? ` · ${cs} seeder(s) confirmed by connecting` : " · no seeder confirmed by connecting in the last probe") + old },
    "Seeders ", h("b", null, fmtNum(r.seeders)), cs ? h("span", { class: "vok" }, " ✓" + fmtNum(cs)) : h("span", { class: "vbad" }, " ?"));
  const none = !r.seeders;
  return h("span", { class: "seeds est", title: none ? "Not measured: no tracker has answered yet" : "Estimate (no tracker answer): may be lower than reality" },
    "Seeders ", h("b", null, none ? "?" : "≥ " + fmtNum(r.seeders)));
}
function detailUrl(r) { const q = S.q.trim(); return "/torrent/" + r.ih + (q ? "?q=" + encodeURIComponent(q) : ""); }
function resultCard(r) {
  const copy = h("button", { class: "btn-s", type: "button" }, "Copy magnet");
  copy.onclick = () => copyText(r.magnet, copy);
  return h("article", { class: "card" },
    h("h3", null, h("a", { href: detailUrl(r), class: "tlink" }, hlNodes(r.name_hl))),
    h("div", { class: "row" },
      seedsNode(r),
      h("span", null, "Peers ", h("b", null, fmtNum(r.peers))),
      h("span", null, "Size ", h("b", null, fmtBytes(r.size))),
      h("span", null, "Files ", h("b", null, fmtNum(r.file_count))),
      h("span", { class: "chip" }, r.category),
      r.exts.slice(0, 4).map(e => h("span", { class: "chip ext" }, "." + e)),
      r.private ? h("span", { class: "chip warn", title: "Private torrent: DHT/PEX are not used" }, "private") : null,
      ["dead", "quiet", "weak"].includes(r.state) ? h("span", { class: "chip st-" + r.state, title: STATE_HELP[r.state] }, STATE_LABEL[r.state]) : null,
      h("span", { title: fmtDateTime(r.indexed_at) }, "Indexed ", fmtRel(r.indexed_at))),
    sourcesLine(r.hd),
    (r.matched_files || []).length ? h("div", { class: "mfiles" }, h("span", null, "Matches in files:"),
      r.matched_files.map(m => h("div", { class: "mf", title: m.path }, hlNodes(m.hl), h("i", null, fmtBytes(m.size))))) : null,
    r.top_files ? h("p", { class: "top-files" }, r.top_files.join("  ·  ") + (r.file_count > 3 ? "  …" : "")) : null,
    h("div", { class: "actions" },
      h("a", { class: "btn-s primary", href: r.magnet, title: "Open in your BitTorrent client" }, "🧲 Magnet"), copy,
      h("a", { class: "btn-s", href: detailUrl(r) }, "View details →")));
}

function renderPager(d) {
  const pages = d.pages, cur = d.page, box = $("#pager");
  box.replaceChildren();
  if (pages <= 1) return;
  const go = p => () => { S.page = p; syncForm(); pushUrl(false); doSearch(); scrollTo({ top: 0, behavior: "smooth" }); };
  box.append(h("button", { disabled: cur === 1, onclick: go(cur - 1) }, "‹"));
  const from = Math.max(1, cur - 3), to = Math.min(pages, cur + 3);
  if (from > 1) box.append(h("button", { onclick: go(1) }, "1"), from > 2 ? h("span", { class: "gap" }, "…") : null);
  for (let p = from; p <= to; p++) box.append(h("button", { class: p === cur ? "on" : "", onclick: go(p) }, p));
  if (to < pages) box.append(to < pages - 1 ? h("span", { class: "gap" }, "…") : null, h("button", { onclick: go(pages) }, fmtNum(pages)));
  box.append(h("button", { disabled: cur === pages, onclick: go(cur + 1) }, "›"));
}

/* ---------- search box + autocomplete ---------- */
let acTimer, acSeq = 0, acItems = [], acIdx = -1;
function closeAc() { $("#ac").hidden = true; acItems = []; acIdx = -1; }
function renderAc() {
  const ac = $("#ac");
  ac.replaceChildren(...acItems.map((it, i) => h("div", { class: "aci" + (i === acIdx ? " on" : ""), onmousedown: e => { e.preventDefault(); pick(i); } }, it.text, h("i", null, fmtNum(it.count)))));
  ac.hidden = !acItems.length;
}
function pick(i) { $("#q").value = acItems[i].text + " "; S.q = $("#q").value.trim(); closeAc(); newSearch(); }
$("#q").addEventListener("input", () => {
  clearTimeout(acTimer);
  acTimer = setTimeout(async () => {
    const v = $("#q").value, my = ++acSeq;
    if (v.trim().length < 2 || v.endsWith(" ")) return closeAc();
    try { const r = await api("/api/suggest?q=" + encodeURIComponent(v)); if (my === acSeq) { acItems = r; acIdx = -1; renderAc(); } } catch { closeAc(); }
  }, 130);
  clearTimeout(window.__st); window.__st = setTimeout(() => { if (S.q !== $("#q").value.trim()) { S.q = $("#q").value.trim(); newSearch({ replace: true }); } }, 450);
});
$("#q").addEventListener("keydown", e => {
  if (!acItems.length) return;
  if (e.key === "ArrowDown") { acIdx = (acIdx + 1) % acItems.length; renderAc(); e.preventDefault(); }
  else if (e.key === "ArrowUp") { acIdx = (acIdx - 1 + acItems.length) % acItems.length; renderAc(); e.preventDefault(); }
  else if (e.key === "Enter" && acIdx >= 0) { e.preventDefault(); pick(acIdx); }
  else if (e.key === "Escape") closeAc();
});
$("#q").addEventListener("blur", () => setTimeout(closeAc, 120));
$("#search-form").onsubmit = e => { e.preventDefault(); clearTimeout(window.__st); closeAc(); S.q = $("#q").value.trim(); newSearch(); };
addEventListener("popstate", () => { fromParams(new URLSearchParams(location.search)); syncForm(); doSearch(); });


/* =====================================================================
   ANALYTICS
   Cards are built ONCE and afterwards only their content is updated, in the same instant
   (without destroying the page or repainting in another frame): so scrolling does not jump on refresh.
   ===================================================================== */
const prefs = Object.assign({ auto: true, every: 10, range: 3600, view: {}, axis: {}, table: {} },
  (() => { try { return JSON.parse(store.get("prefs") || "{}"); } catch { return {}; } })());
const savePrefs = () => store.set("prefs", JSON.stringify(prefs));
let statsTimer = null, inflight = false, lastData = null, built = false;
const cards = [];
const C = { s1: "var(--s1)", s2: "var(--s2)", s3: "var(--s3)", s4: "var(--s4)" };
const UNIT = { d: 86400, h: 3600, m: 60 }, UNIT_LABEL = { d: "day", h: "h", m: "min" };
function unitOf(def) { return def.unit === "h" && prefs.range >= 7 * 86400 ? "d" : def.unit; }
const fmtDec = n => n == null ? "—" : n === 0 ? "0" : (n >= 100 ? fmtNum(n) : n.toFixed(n >= 10 ? 1 : 2));

/* ---------- controls ---------- */
$("#auto-on").checked = !!prefs.auto;
$("#auto-every").value = String(prefs.every);
document.querySelectorAll(".range button").forEach(b => b.classList.toggle("on", +b.dataset.r === prefs.range));
for (const b of document.querySelectorAll(".range button")) b.onclick = () => {
  prefs.range = +b.dataset.r; savePrefs();
  document.querySelectorAll(".range button").forEach(x => x.classList.toggle("on", x === b));
  loadStats({ force: true });
};
$("#auto-on").onchange = e => { prefs.auto = e.target.checked; savePrefs(); schedule(); showUpdated(); };
$("#auto-every").onchange = e => { prefs.every = +e.target.value; savePrefs(); schedule(); };
$("#refresh-now").onclick = () => loadStats({ force: true });
document.addEventListener("visibilitychange", () => { if (!document.hidden && prefs.auto && currentTab === "stats") loadStats(); });

function schedule() {
  clearInterval(statsTimer); statsTimer = null;
  if (prefs.auto && currentTab === "stats")
    statsTimer = setInterval(() => { if (!document.hidden) loadStats(); }, prefs.every * 1000);
}
function onTabChange() {
  if (currentTab === "stats") loadStats({ force: true });
  if (currentTab === "admin" && window.onAdminTab) window.onAdminTab();
  schedule();
}
let lastUpdate = 0;
function showUpdated() {
  const t = lastUpdate ? new Date(lastUpdate).toLocaleTimeString(LOCALE) : "";
  $("#updated").textContent = !lastUpdate ? "" : prefs.auto ? `updated ${t}` : `paused · data from ${t}`;
}

async function loadStats(opts = {}) {
  if (inflight) return;
  inflight = true;
  try {
    const [st, hist] = await Promise.all([api("/api/stats"), api("/api/history?seconds=" + prefs.range)]);
    updatePill(st);
    if (currentTab !== "stats") return;
    lastData = { st, hist };
    render(!!opts.force);
    lastUpdate = Date.now(); showUpdated();
  } catch (e) { setPill("bad", "no connection to the server"); }
  finally { inflight = false; }
}
async function refreshPill() { try { updatePill(await api("/api/stats")); } catch { setPill("bad", "no connection to the server"); } }

function render(force) {
  const y = scrollY;
  buildOnce();
  renderKpis(lastData.st, lastData.hist);
  for (const c of cards) c.paint(force);
  if (Math.abs(scrollY - y) > 1) scrollTo(0, y);   // safety net: never move the user's scroll
}

/* ---------- status pill and KPIs ---------- */
function updatePill(st) {
  $("#demo-banner").hidden = st.mode !== "demo";
  const lv = st.live || {};
  if (lv.error) return setPill("bad", lv.error);
  const n = lv.dht_nodes;
  setPill("ok", `${fmtNum(st.analytics.torrents)} torrents · ${n != null ? fmtNum(n) + " DHT nodes" : "starting…"}`);
  $("#hero-sub").textContent = st.analytics.torrents
    ? `${fmtNum(st.analytics.torrents)} torrents and ${fmtNum(st.analytics.files)} files indexed.`
    : "Torrents discovered by the crawler, with magnet links, seeders, peers and files.";
}
function setPill(cls, text) { const p = $("#pill-live"); p.className = "pill " + cls; $("#pill-text").textContent = text; }

function kpi(label, value, sub) {
  return h("div", { class: "kpi" }, h("div", { class: "l" }, label), h("div", { class: "v" }, value), h("div", { class: "s" }, sub || " "));
}
const st_of = (a, k) => ((a.health_states || []).find(x => x.name === k) || {}).count || 0;
function renderKpis(st, hist) {
  const a = st.analytics, lv = st.live || {}, cn = a.counters || {}, life = a.life || {};
  const days = Math.max((Date.now() / 1000 - (life.first_start || Date.now() / 1000)) / 86400, 1 / 24);
  const okN = cn.metadata_ok || 0, failN = cn.probe_timeouts || 0;
  const success = okN + failN ? fmtDec(okN / (okN + failN) * 100) + " % success (cumulative)" : "—";
  const vpct = a.torrents ? Math.round(a.health_verified / a.torrents * 100) : 0;
  $("#kpis").replaceChildren(
    kpi("Torrents indexed", fmtNum(a.torrents), `${fmtNum(a.torrents / days)} per day on average since the start`),
    kpi("Files found", fmtNum(a.files)),
    kpi("Size indexed", fmtBytes(a.bytes_indexed), "sum of all torrents"),
    kpi("Infohashes discovered", fmtNum(a.hashes_discovered), `${fmtNum(a.hashes_pending)} queued · ${fmtNum(a.hashes_retry)} to retry · ${fmtNum(a.hashes_dropped)} dropped as stale · ${fmtNum(a.hashes_failed)} without metadata`),
    kpi("DHT nodes (routing table)", fmtNum(lv.dht_nodes), `≈ ${fmtNum(a.nodes_seen)} unique nodes seen in total (all-time)`),
    kpi("Connected peers", fmtNum(lv.peers_connected), `${fmtNum((lv.peers_connected || 0) + (lv.peers_half_open || 0))} connections (${fmtNum(lv.peers_half_open)} half-open) · ≈ ${fmtNum(a.peers_seen)} unique peers (all-time)`),
    kpi("Metadata downloads", fmtNum(lv.active_probes), `${fmtNum(lv.refresh_probes || 0)} refreshing health · ${success}`),
    kpi("Total traffic ↓", fmtBytes(life.rx_bytes), `now ${fmtRate(lv.rx_bps)} · cumulative over all sessions`),
    kpi("Total traffic ↑", fmtBytes(life.tx_bytes), `now ${fmtRate(lv.tx_bps)} · cumulative over all sessions`),
    kpi("BEP 51 (DHT sampling)", lv.sampling === false ? "off" : fmtNum(cn.bep51_replies),
      lv.sampling === false ? "disabled (passive only)" : `replies to ${fmtNum(cn.bep51_queries)} queries (cumulative)`),
    kpi("Alive torrents", fmtNum(st_of(a, "alive")), `${fmtNum(st_of(a, "weak"))} weak · ${fmtNum(st_of(a, "quiet"))} no activity · ${fmtNum(st_of(a, "dead"))} dead · ${fmtNum(st_of(a, "unknown"))} not measured · ${vpct} % verified`),
    kpi("Total uptime", fmtDur(life.uptime_total || 0), `${fmtNum(life.sessions)} session${life.sessions === 1 ? "" : "s"} · since ${fmtDate(life.first_start)} · this session: ${fmtDur(life.session_uptime || 0)}`));
}

/* ---------- chart definitions ---------- */
const SRC_NAMES = { announce: "announce_peer", get_peers: "get_peers", bep51: "BEP 51 (sampling)" };
const DEFS = [
  { sec: "Indexing", id: "torrents", kind: "line", title: "Torrents indexed", fmt: fmtNum, cumulative: true, unit: "h", int: true, axis: "fit",
    series: [{ key: "torrents", name: "Torrents", c: C.s1 }] },
  { sec: "Indexing", id: "files", kind: "line", title: "Files found", fmt: fmtNum, cumulative: true, unit: "h", int: true, axis: "fit",
    series: [{ key: "files", name: "Files", c: C.s1 }] },
  { sec: "Indexing", id: "flow", kind: "line", title: "Infohash flow per minute", rateOnly: true, unit: "m",
    series: [{ key: "hashes", name: "Discovered", c: C.s1 }, { key: "ok", name: "With metadata", c: C.s2 },
             { key: "fail", name: "Probes without metadata", c: C.s3 }, { key: "drop", name: "Dropped as stale", c: C.s4 }] },
  { sec: "Indexing", id: "success", kind: "line", title: "Metadata success rate", ratio: { num: "ok", plus: "fail" }, fixedMax: 100,
    fmt: v => fmtDec(v) + " %", series: [{ key: "ok", name: "Success", c: C.s1 }] },
  { sec: "Indexing", id: "verified", kind: "line", title: "Torrents with verified health (tracker)", fmt: fmtNum, int: true, axis: "fit",
    series: [{ key: "verified", name: "Verified", c: C.s3 }] },
  { sec: "Indexing", id: "queue", kind: "line", title: "Queued infohashes", fmt: fmtNum, int: true, axis: "fit",
    series: [{ key: "pending", name: "Queued", c: C.s1 }] },
  { sec: "Network and DHT", id: "bw", kind: "line", title: "Bandwidth", fmt: fmtRate, bytes: true,
    series: [{ key: "rx_bps", name: "In", c: C.s1 }, { key: "tx_bps", name: "Out", c: C.s2 }] },
  { sec: "Network and DHT", id: "peers", kind: "line", title: "Peers and connections", fmt: fmtNum, int: true,
    series: [{ key: "peers", name: "Connected peers", c: C.s1 }, { key: "connections", name: "Connections (incl. half-open)", c: C.s2 }] },
  { sec: "Network and DHT", id: "nodes", kind: "line", title: "DHT nodes in the routing table", fmt: fmtNum, int: true, axis: "fit",
    series: [{ key: "dht_nodes", name: "Nodes", c: C.s1 }] },
  { sec: "Network and DHT", id: "bep", kind: "line", title: "BEP 51: queries and replies per minute", rateOnly: true, unit: "m",
    series: [{ key: "bq", name: "Queries", c: C.s1 }, { key: "br", name: "Replies", c: C.s2 }] },
  { sec: "Content", id: "cats", kind: "bars", title: "Torrents by category",
    get: a => a.categories.map(x => ({ name: x.name, v: x.count, extra: fmtBytes(x.bytes) })) },
  { sec: "Content", id: "sizes", kind: "bars", title: "Size distribution", get: a => a.size_buckets.map(x => ({ name: x.name, v: x.count })) },
  { sec: "Content", id: "seeds", kind: "bars", title: "Seeder distribution", get: a => (a.seed_buckets || []).map(x => ({ name: x.name, v: x.count })) },
  { sec: "Content", id: "exts", kind: "bars", title: "Most frequent extensions", get: a => a.extensions.map(x => ({ name: "." + x.name, v: x.count })) },
  { sec: "Content", id: "src", kind: "bars", title: "How infohashes are discovered", get: a => a.sources.map(x => ({ name: SRC_NAMES[x.name] || x.name, v: x.count })) },
  { sec: "Content", id: "states", kind: "bars", title: "Torrent state (still alive?)",
    get: a => (a.health_states || []).map(x => ({ name: STATE_LABEL[x.name], v: x.count, extra: STATE_HELP[x.name] })) },
  { sec: "Content", id: "health", kind: "bars", title: "Health measurement quality",
    get: a => [{ name: "Verified (tracker)", v: a.health_verified }, { name: "Estimated / not measured", v: a.health_unverified }] },
  { sec: "Content", id: "ages", kind: "bars", title: "Age of indexed content", get: a => (a.age_buckets || []).map(x => ({ name: x.name, v: x.count })) },
  { sec: "Content", id: "top", kind: "top", title: "Most seeded torrents (verified)", get: a => a.top_seeded || [] },
  { sec: "System", id: "life", kind: "life", title: "All-time summary", get: a => a.life || {} },
];

function viewOf(def) { return def.rateOnly || def.ratio ? "rate" : (def.cumulative && prefs.view[def.id]) || "total"; }
function axisOf(def) { return prefs.axis[def.id] || (viewOf(def) === "total" && def.axis) || "zero"; }

function buildOnce() {
  if (built) return;
  built = true;
  const frag = document.createDocumentFragment();
  let sec = null;
  for (const def of DEFS) {
    if (def.sec !== sec) { sec = def.sec; frag.append(h("h3", { class: "sec" }, sec)); }
    const card = makeCard(def);
    cards.push(card);
    frag.append(card.el);
  }
  $("#charts").replaceChildren(frag);
}

function makeCard(def) {
  const card = { def, hover: false };
  const body = h("div", { class: "cbody" + (def.kind === "line" ? "" : " bars") });
  const badge = h("span", { class: "badge", hidden: true, title: "The Y axis does not start at 0: it magnifies small changes" }, "fitted axis");
  const tools = h("div", { class: "tools" }, badge);
  card.badge = badge;

  if (def.kind === "line" && def.cumulative) {           // Total | Rate
    const seg = h("div", { class: "seg", role: "group", "aria-label": "View" });
    card.segBtns = [["total", "Total"], ["rate", "Rate"]].map(([v, label]) => {
      const b = h("button", { type: "button" }, label);
      b.onclick = () => { prefs.view[def.id] = v; savePrefs(); card.paint(true); };
      seg.append(b); return [v, b];
    });
    tools.append(seg);
  }
  if (def.kind === "line" && def.fixedMax == null) {     // axis 0 | fitted
    card.axBtn = h("button", { type: "button", class: "tbl-btn", title: "Toggle Y axis: from 0 / fitted to the data" });
    card.axBtn.onclick = () => { prefs.axis[def.id] = axisOf(def) === "fit" ? "zero" : "fit"; savePrefs(); card.paint(true); };
    tools.append(card.axBtn);
  }
  if (def.kind !== "top" && def.kind !== "life") {
    card.tbBtn = h("button", { type: "button", class: "tbl-btn" });
    card.tbBtn.onclick = () => { prefs.table[def.id] = !prefs.table[def.id]; savePrefs(); card.paint(true); };
    tools.append(card.tbBtn);
  }
  const legend = def.kind === "line" && def.series.length > 1
    ? h("div", { class: "legend" }, def.series.map(s => h("span", null, h("i", { style: `background:${s.c}` }), s.name))) : null;
  card.el = h("section", { class: "chart" + (def.kind === "top" || def.kind === "life" ? " wide" : "") }, h("header", null, h("h3", null, def.title), tools), legend, body);
  // while hovered the card is not redrawn (the tooltip is not lost); it updates on mouse leave
  card.el.addEventListener("mouseenter", () => { card.hover = true; });
  card.el.addEventListener("mouseleave", () => { card.hover = false; });

  card.paint = force => {
    if (!lastData || (card.hover && !force)) return;
    const { st, hist } = lastData, asTable = !!prefs.table[def.id];
    let node;
    if (def.kind === "line") {
      const mdl = lineModel(def, hist), mode = axisOf(def);
      node = asTable ? lineTable(mdl, def) : lineChart(mdl, def, mode);
      card.badge.hidden = asTable || !node.__fitBadge;
      if (card.axBtn) card.axBtn.textContent = mode === "fit" ? "Axis: fitted" : "Axis: from 0";
      if (card.segBtns) for (const [v, b] of card.segBtns) { b.classList.toggle("on", v === viewOf(def)); if (v === "rate") b.textContent = "Rate /" + UNIT_LABEL[unitOf(def)]; }
    } else if (def.kind === "bars") {
      const rows = def.get(st.analytics);
      node = asTable ? barTable(rows) : hbars(rows);
    } else if (def.kind === "life") node = lifeTable(def.get(st.analytics), st.analytics, st);
    else node = topTable(def.get(st.analytics));
    if (card.tbBtn) card.tbBtn.textContent = asTable ? "Show chart" : "Show table";
    body.replaceChildren(node);                           // a single synchronous change: no gaps between frames
  };
  return card;
}

/* ---------- derived data ---------- */
function medStep(T) {
  const d = [];
  for (let i = 1; i < T.length; i++) d.push(T[i] - T[i - 1]);
  d.sort((a, b) => a - b);
  return d.length ? d[Math.floor(d.length / 2)] : 10;
}
// Rate of a cumulative counter over a sliding window; broken at gaps (restarts) and missing values.
function rateArr(T, X, perSec, win) {
  const n = T.length, out = new Array(n).fill(null);
  if (n < 2) return out;
  const med = medStep(T), gap = Math.max(60, med * 4), w = Math.max(win, med * 3);
  let j = 0, segStart = 0;
  for (let i = 0; i < n; i++) {
    if (i > 0 && T[i] - T[i - 1] > gap) segStart = i;
    if (X[i] == null) continue;
    j = Math.max(j, segStart);
    while (j + 1 < i && T[i] - T[j + 1] >= w) j++;
    if (X[j] != null && T[i] - T[j] >= w * 0.8) out[i] = Math.max(0, (X[i] - X[j]) / (T[i] - T[j]) * perSec);   // max(0): counter reset
  }
  return out;
}
function lineModel(def, hist) {
  const T = hist.map(p => p.t), get = k => hist.map(p => (p[k] == null ? null : p[k]));
  const view = viewOf(def);
  if (def.ratio) {
    const a = rateArr(T, get(def.ratio.num), 1, 120), b = rateArr(T, get(def.ratio.plus), 1, 120);
    return { T, view, fmt: def.fmt, arrs: [a.map((v, i) => (v == null || b[i] == null || v + b[i] <= 0 ? null : v / (v + b[i]) * 100))] };
  }
  if (view === "rate") {
    const un = unitOf(def), u = UNIT[un], win = un === "d" ? 6 * 3600 : un === "h" ? 180 : 60, lab = " /" + UNIT_LABEL[un];
    return { T, view, fmt: v => fmtDec(v) + lab, arrs: def.series.map(s => rateArr(T, get(s.key), u, win)) };
  }
  return { T, view, fmt: def.fmt, arrs: def.series.map(s => get(s.key)) };
}

/* ---------- Y-axis scale ---------- */
function niceStep(raw) {
  const e = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / e;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * e;
}
function yScale(arrs, mode, def, integer) {
  if (def.fixedMax != null) return { min: 0, max: def.fixedMax, ticks: [0, 25, 50, 75, 100], fitted: false };
  let mn = Infinity, mx = -Infinity;
  for (const a of arrs) for (const v of a) if (v != null) { if (v < mn) mn = v; if (v > mx) mx = v; }
  let lo, hi;
  if (mode === "fit") {
    let span = mx - mn; if (span <= 0) span = Math.max(Math.abs(mx) * 0.02, 1);
    lo = mn - span * 0.12; hi = mx + span * 0.12;
    if (mn >= 0 && lo < 0) lo = 0;
  } else { lo = 0; hi = mx > 0 ? mx : 1; }
  const u = def.bytes ? Math.pow(1024, Math.max(0, Math.floor(Math.log(Math.max(hi, 1)) / Math.log(1024)))) : 1;   // KB, MB…
  lo /= u; hi /= u;
  let step = niceStep((hi - lo) / 4);
  if (integer && u === 1) step = Math.max(step, 1);
  const min = Math.floor(lo / step + 1e-9) * step; let max = Math.ceil(hi / step - 1e-9) * step;
  if (max <= min) max = min + step;
  const ticks = [];
  for (let v = min; v <= max + step * 1e-6; v += step) ticks.push(Number((v * u).toPrecision(12)));
  return { min: min * u, max: max * u, ticks, fitted: mode === "fit" && min > 0 };
}

/* ---------- line chart ---------- */
function lineChart(mdl, def, mode) {
  const { T, arrs, fmt } = mdl;
  if (T.length < 2 || !arrs.some(a => a.some(v => v != null)))
    return h("div", { class: "empty" }, def.rateOnly || mdl.view === "rate" ? "Collecting data… (the rate needs a few minutes of history)" : "Collecting data… (one point every 10 s)");
  const sc = yScale(arrs, mode, def, !!def.int && mdl.view === "total");
  const W = 520, H = 200, mg = { r: 10, t: 8, b: 22 };
  mg.l = Math.max(52, Math.max(...sc.ticks.map(t => fmt(t).length)) * 6.4 + 12);
  const iw = W - mg.l - mg.r, ih = H - mg.t - mg.b, t0 = T[0], t1 = T[T.length - 1], gap = Math.max(60, medStep(T) * 4);
  const X = t => mg.l + (t - t0) / Math.max(t1 - t0, 1) * iw;
  const Y = v => mg.t + ih - (v - sc.min) / (sc.max - sc.min) * ih;
  const el = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": def.title });
  for (const v of sc.ticks) {
    el.append(svg("line", { class: "tick-line", x1: mg.l, x2: W - mg.r, y1: Y(v), y2: Y(v) }),
      svg("text", { x: mg.l - 6, y: Y(v) + 3.5, "text-anchor": "end" }, fmt(v)));
  }
  for (let i = 0; i <= 4; i++) {
    const t = t0 + (t1 - t0) * i / 4;
    el.append(svg("text", { x: X(t), y: H - 5, "text-anchor": i === 0 ? "start" : i === 4 ? "end" : "middle" }, fmtAxis(t, t1 - t0)));
  }
  def.series.forEach((s, k) => {
    let d = "", prev = -1;                                   // broken at gaps and at missing values
    for (let i = 0; i < T.length; i++) {
      const v = arrs[k][i];
      if (v == null) { prev = -1; continue; }
      const brk = prev < 0 || T[i] - T[prev] > gap;
      d += (brk ? "M" : "L") + X(T[i]).toFixed(1) + " " + Y(v).toFixed(1) + (brk ? "l0 0" : "");
      prev = i;
    }
    el.append(svg("path", { class: "line", stroke: s.c, d }));
  });
  // hover: crosshair + markers + tooltip
  const cross = svg("line", { y1: mg.t, y2: mg.t + ih, stroke: "var(--ink-3)", "stroke-width": 1, "stroke-dasharray": "3 3", visibility: "hidden" });
  const dots = def.series.map(s => svg("circle", { r: 4, fill: s.c, stroke: "var(--surface)", "stroke-width": 2, visibility: "hidden" }));
  const hit = svg("rect", { x: mg.l, y: mg.t, width: iw, height: ih, fill: "transparent" });
  el.append(cross, ...dots, hit);
  const tip = $("#tip");
  hit.addEventListener("mousemove", ev => {
    const box = el.getBoundingClientRect(), px = (ev.clientX - box.left) / box.width * W;
    let best = -1, bd = Infinity;
    T.forEach((t, i) => { if (arrs.some(a => a[i] != null)) { const d = Math.abs(X(t) - px); if (d < bd) { bd = d; best = i; } } });
    if (best < 0) return;
    const x = X(T[best]);
    cross.setAttribute("x1", x); cross.setAttribute("x2", x); cross.setAttribute("visibility", "visible");
    def.series.forEach((s, k) => {
      const v = arrs[k][best];
      dots[k].setAttribute("visibility", v == null ? "hidden" : "visible");
      if (v != null) { dots[k].setAttribute("cx", x); dots[k].setAttribute("cy", Y(v)); }
    });
    tip.replaceChildren(h("div", { class: "t" }, new Date(T[best] * 1000).toLocaleString(LOCALE)),
      ...def.series.map((s, k) => h("div", { class: "r" }, h("span", null, h("i", { style: `background:${s.c}` }), s.name), h("b", null, arrs[k][best] == null ? "—" : fmt(arrs[k][best])))));
    tip.hidden = false;
    tip.style.left = Math.min(ev.clientX + 14, innerWidth - tip.offsetWidth - 8) + "px";
    tip.style.top = ev.clientY + 14 + "px";
  });
  hit.addEventListener("mouseleave", () => { tip.hidden = true; cross.setAttribute("visibility", "hidden"); dots.forEach(d => d.setAttribute("visibility", "hidden")); });
  el.__fitBadge = sc.fitted;
  return el;
}

function lineTable(mdl, def) {
  const idx = [];
  for (let i = mdl.T.length - 1; i >= 0 && idx.length < 30; i--) idx.push(i);
  return h("table", { class: "data" }, h("thead", null, h("tr", null, h("th", null, "Time"), def.series.map(s => h("th", null, s.name)))),
    h("tbody", null, idx.map(i => h("tr", null, h("td", null, mdl.T[mdl.T.length - 1] - mdl.T[0] > 2 * 86400 ? fmtDateTime(mdl.T[i]) : fmtTime(mdl.T[i], true)),
      mdl.arrs.map(a => h("td", null, a[i] == null ? "—" : mdl.fmt(a[i])))))));
}

/* ---------- bars and tables ---------- */
function hbars(rows) {
  if (!rows.length) return h("div", { class: "empty" }, "No data yet.");
  const max = Math.max(...rows.map(r => r.v), 1);
  return h("div", { class: "hbars" }, rows.map(r => h("div", { class: "hb", title: r.extra ? `${r.name}: ${fmtNum(r.v)} (${r.extra})` : `${r.name}: ${fmtNum(r.v)}` },
    h("span", { class: "n" }, r.name), h("span", { class: "bar" }, h("i", { style: `width:${(r.v / max * 100).toFixed(1)}%` })), h("span", { class: "val" }, fmtNum(r.v)))));
}
function barTable(rows) {
  return h("table", { class: "data" }, h("thead", null, h("tr", null, h("th", null, "Name"), h("th", null, "Count"), rows.some(r => r.extra) ? h("th", null, "Size") : null)),
    h("tbody", null, rows.map(r => h("tr", null, h("td", null, r.name), h("td", null, fmtNum(r.v)), rows.some(x => x.extra) ? h("td", null, r.extra || "") : null))));
}
function topTable(rows) {
  if (!rows.length) return h("div", { class: "empty" }, "No verified data yet.");
  return h("table", { class: "data toptbl" }, h("thead", null, h("tr", null, h("th", null, "Torrent"), h("th", null, "Seeders"), h("th", null, "Size"))),
    h("tbody", null, rows.map(r => h("tr", null,
      h("td", null, h("a", { href: "/torrent/" + r.ih, class: "tlink", title: "View details" }, r.name)),
      h("td", null, fmtNum(r.seeders)), h("td", null, fmtBytes(r.size))))));
}
const TIER_LABEL = { raw: "10 s · last 24 h", m5: "5 min · up to 30 days", h1: "1 h · up to 2 years" };
function lifeTable(life, a, st) {
  const days = Math.max((Date.now() / 1000 - life.first_start) / 86400, 1 / 24);
  const cov = life.coverage || {};
  const ver = st && st.version ? `${st.version} (${fmtDate(Date.parse(st.version_date) / 1000)})` + (APP_VERSION ? " — " + APP_VERSION.changelog[0].summary : "") : "—";
  const rows = [
    ["Running version", ver],
    ["First start", fmtDateTime(life.first_start) + ` (${fmtDur(Date.now() / 1000 - life.first_start)} ago)`],
    ["Cumulative uptime", `${fmtDur(life.uptime_total)} in ${fmtNum(life.sessions)} session${life.sessions === 1 ? "" : "s"} · availability ${fmtDec(Math.min(100, life.uptime_total / 864 / days))} %`],
    ["Cumulative traffic", `↓ ${fmtBytes(life.rx_bytes)} · ↑ ${fmtBytes(life.tx_bytes)} (average ${fmtRate((life.rx_bytes + life.tx_bytes) / Math.max(life.uptime_total, 1))})`],
    ["Torrents indexed", `${fmtNum(a.torrents)} (${fmtNum(a.torrents / days)}/day on average)`],
    ["Infohashes discovered", `${fmtNum(a.hashes_discovered)} (${fmtNum(a.hashes_discovered / days)}/day) · ${fmtDec(a.torrents / Math.max(a.hashes_discovered, 1) * 100)} % ended up indexed`],
    ["Unique seen (estimate ±1 %)", `≈ ${fmtNum(life.nodes_unique)} DHT nodes · ≈ ${fmtNum(life.peers_unique)} peers`],
    ["Verified health", `${fmtNum(a.health_verified)} of ${fmtNum(a.torrents)} torrents (${a.torrents ? fmtDec(a.health_verified / a.torrents * 100) : 0} %)`],
    ...Object.entries(cov).map(([k, v]) => ["History " + TIER_LABEL[k], v.points ? `${fmtNum(v.points)} points · since ${fmtDateTime(v.from)}` : "no data yet"]),
  ];
  const table = h("table", { class: "data life" }, h("tbody", null, rows.map(([k, v]) => h("tr", null, h("th", null, k), h("td", null, v)))));
  if (!APP_VERSION) return table;
  return h("div", null, table, h("details", { class: "small" }, h("summary", null, `Version history (${APP_VERSION.changelog.length})`),
    h("ul", { class: "changelog" }, APP_VERSION.changelog.map(c => h("li", null, h("b", null, "v" + c.version), ` · ${fmtDate(Date.parse(c.date) / 1000)} — ${c.summary}`)))));
}

/* ---------- startup ---------- */
(function init() {
  buildAdvanced();
  fromParams(new URLSearchParams(location.search));
  $("#advwrap").hidden = store.get("advOpen") !== "1" && !hasFilters();
  syncForm();
  setTab(location.hash.slice(1) || "search");
  doSearch();
  refreshPill();
  setInterval(() => { if (!document.hidden && currentTab !== "stats") refreshPill(); }, 15000);
})();
