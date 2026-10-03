"use strict";
/* Helpers shared by the search page (index.html) and the torrent page (torrent.html). */
const $ = (s, r = document) => r.querySelector(s);
const SVGNS = "http://www.w3.org/2000/svg";
// Torrent names are UNTRUSTED text: everything is built with DOM/textContent, never innerHTML.
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat(Infinity)) if (k != null && k !== false) el.append(k.nodeType ? k : document.createTextNode(String(k)));
  return el;
}
function svg(tag, attrs, ...kids) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
  for (const k of kids.flat(Infinity)) if (k != null) el.append(k.nodeType ? k : document.createTextNode(String(k)));
  return el;
}

/* ---------- formats ---------- */
const LOCALE = "en-GB";
const nf = new Intl.NumberFormat(LOCALE);
const fmtNum = n => nf.format(Math.round(n || 0));
function fmtBytes(b) {
  b = b || 0;
  const u = ["B", "KB", "MB", "GB", "TB", "PB"];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return (i === 0 ? Math.round(b) : b.toFixed(b < 10 ? 2 : 1)) + " " + u[i];
}
const fmtRate = b => fmtBytes(b) + "/s";
function fmtDate(t) { return t ? new Date(t * 1000).toLocaleDateString(LOCALE, { year: "numeric", month: "short", day: "numeric" }) : "—"; }
function fmtDateTime(t) { return t ? new Date(t * 1000).toLocaleString(LOCALE, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—"; }
function fmtTime(t, secs) { return new Date(t * 1000).toLocaleTimeString(LOCALE, secs ? { hour: "2-digit", minute: "2-digit", second: "2-digit" } : { hour: "2-digit", minute: "2-digit" }); }
// X-axis label depending on the span covered by the chart
function fmtAxis(t, span) {
  const d = new Date(t * 1000);
  if (span < 900) return fmtTime(t, true);
  if (span <= 2 * 86400) return fmtTime(t, false);
  if (span <= 14 * 86400) return d.toLocaleDateString(LOCALE, { day: "2-digit", month: "short" }) + " " + d.toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" });
  return d.toLocaleDateString(LOCALE, span > 400 * 86400 ? { month: "short", year: "2-digit" } : { day: "2-digit", month: "short" });
}
function fmtDur(s) {
  const d = Math.floor(s / 86400), hh = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return d ? `${d} d ${hh} h` : hh ? `${hh} h ${m} min` : `${m} min`;
}
function fmtRel(t) {                       // "5 min ago"
  if (!t) return "—";
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  if (s < 86400 * 45) return `${Math.round(s / 86400)} d ago`;
  return fmtDate(t);
}

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) { const e = new Error(path + " → " + r.status); e.status = r.status; throw e; }
  return r.json();
}
const store = { get(k) { try { return localStorage.getItem(k); } catch { return null; } },
                set(k, v) { try { localStorage.setItem(k, v); } catch {} } };

/* ---------- theme ---------- */
(function theme() {
  const t = store.get("theme");
  if (t) document.documentElement.dataset.theme = t;
  const btn = $("#theme-btn");
  if (btn) btn.onclick = () => {
    const cur = document.documentElement.dataset.theme ||
      (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const next = cur === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next; store.set("theme", next);
  };
})();

/* ---------- UI helpers ---------- */
function copyText(text, btn) {
  const done = () => { const o = btn.dataset.label || btn.textContent; btn.dataset.label = o; btn.textContent = "Copied!"; setTimeout(() => btn.textContent = o, 1200); };
  const fallback = () => {
    const ta = h("textarea", { style: "position:fixed;opacity:0" }); ta.value = text;
    document.body.append(ta); ta.select();
    try { document.execCommand("copy"); done(); } catch {}
    ta.remove();
  };
  if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, fallback);
  else fallback();
}
// [[text, highlighted], …] (returned by the server) -> nodes with <mark>
function hlNodes(segs) { return (segs || []).map(([t, on]) => on ? h("mark", null, t) : t); }
const SRC_LONG = { announce: "announce_peer (a node announced it to us)", get_peers: "get_peers (a node looked for it)", bep51: "BEP 51 sampling" };

// Health states (see records.health_state on the server)
const STATE_LABEL = { alive: "Alive", weak: "Weak (peers only)", quiet: "No activity", dead: "Dead (confirmed)", unknown: "Not measured" };
const STATE_HELP = {
  alive: "There are seeders: a tracker or our own connections have seen them.",
  weak: "No seeders, but peers are visible.",
  quiet: "Trackers report 0 seeders and 0 peers, but it is not confirmed yet: a tracker that does not know the torrent also answers 0.",
  dead: "0 seeders and 0 peers in 3 or more measurements verified by 2 or more trackers, spread over at least 12 h.",
  unknown: "Not measured yet, or no tracker has answered.",
};

/* ---------- peers (admin panel and torrent page seen as admin) ---------- */
const ROLE_LABEL = { "1": "Seeder", "0": "Leecher", "-1": "Unknown role" };
const ROLE_HELP = {
  "1": "Seen with the complete torrent (full bitfield / have_all) when connecting.",
  "0": "Connected, without the complete torrent.",
  "-1": "We only know it is in the swarm (DHT or announce), not whether it has the complete torrent.",
};
const PEER_SRC = { c: "connected", d: "DHT get_peers", a: "announce_peer" };
function roleChip(role) {
  return h("span", { class: "chip role r" + role, title: ROLE_HELP[role] }, role === 1 ? "Seeder" : role === 0 ? "Leecher" : "?");
}
function srcText(src) { return (src || "").split("").map(c => PEER_SRC[c] || c).join(" + "); }
// Peer table of a torrent. onIp(ip): click on an IP (e.g. search it in other torrents).
function peerTable(rows, onIp) {
  if (!rows || !rows.length) return h("div", { class: "muted small" }, "No peers seen for this torrent yet. They are recorded every time the crawler probes it (when indexing it and when refreshing its health).");
  const seeds = rows.filter(r => r.role === 1).length, leech = rows.filter(r => r.role === 0).length;
  return h("div", null,
    h("p", { class: "muted small" }, `${fmtNum(rows.length)} peers · ${fmtNum(seeds)} seeders · ${fmtNum(leech)} leechers · ${fmtNum(rows.length - seeds - leech)} with unknown role`),
    h("div", { class: "tscroll" }, h("table", { class: "data peers" },
      h("thead", null, h("tr", null, ["IP", "Port", "Role", "Client", "Source", "First seen", "Last seen"].map(x => h("th", null, x)))),
      h("tbody", null, rows.map(r => h("tr", null,
        h("td", null, onIp ? h("button", { type: "button", class: "iplink", title: "Search this IP in other torrents", onclick: () => onIp(r.ip) }, r.ip) : h("span", { class: "mono" }, r.ip)),
        h("td", null, r.port || "—"), h("td", null, roleChip(r.role)), h("td", null, r.client || "—"),
        h("td", { class: "muted" }, srcText(r.src)), h("td", { title: fmtDateTime(r.first) }, fmtRel(r.first)), h("td", { title: fmtDateTime(r.last) }, fmtRel(r.last))))))));
}
// Text with highlighted spans [[a, b], …] (indexes into the original text, computed on the server)
function spanNodes(text, spans) {
  if (!spans || !spans.length) return [text];
  const out = []; let pos = 0;
  for (const [a, b] of [...spans].sort((x, y) => x[0] - y[0])) {
    if (a < pos) continue;
    if (a > pos) out.push(text.slice(pos, a));
    out.push(h("mark", null, text.slice(a, b))); pos = b;
  }
  if (pos < text.length) out.push(text.slice(pos));
  return out;
}

/* ---------- who reports what (breakdown of the latest health measurement) ---------- */
const trShort = host => (host || "?").replace(/^tracker\./, "");
// Compact line for result lists: "opentrackr.org 120/30 · open.tracker.cl 3/1 · torrent.eu.org ✗ · connected 2 S"
function sourcesLine(hd) {
  if (!hd) return null;
  const parts = hd.tr.map(t => t.s != null
    ? h("span", { class: "src ok", title: `${t.h}: reports ${t.s} seeders / ${t.l} leechers` + (t.r != null ? `; returned ${t.r} addresses on announce` : "") },
        trShort(t.h), " ", h("b", null, fmtNum(t.s)), "/", fmtNum(t.l))
    : h("span", { class: "src bad", title: `${t.h}: ${t.e || "no response"}` }, trShort(t.h), " ✗"));
  parts.push(h("span", { class: "src conn" + (hd.cs ? " ok" : ""), title: "What the crawler saw by CONNECTING in that probe: confirmed seeders (full bitfield) and leechers. It is the only verified figure; tracker figures are what they claim." },
    "connected ", h("b", null, fmtNum(hd.cs)), " S · ", fmtNum(hd.cp), " L"));
  if (hd.dht) parts.push(h("span", { class: "src", title: "Addresses returned by the DHT (unverified)" }, "DHT ", fmtNum(hd.dht)));
  return h("div", { class: "srcline", title: "Measured " + fmtRel(hd.at) }, h("span", { class: "muted" }, "Sources:"), parts);
}

/* ---------- running version (header) ---------- */
let APP_VERSION = null;
(async function showVersion() {
  const el = $("#app-version"); if (!el) return;
  try {
    APP_VERSION = await api("/api/version");
    el.textContent = "v" + APP_VERSION.version;
    el.title = `Version ${APP_VERSION.version} · ${fmtDate(Date.parse(APP_VERSION.date) / 1000)}\n${APP_VERSION.changelog[0].summary}`;
  } catch {}
})();
