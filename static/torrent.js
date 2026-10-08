"use strict";
/* Dedicated torrent page: /torrent/<infohash>  (shared helpers in common.js) */
const IH = location.pathname.split("/").filter(Boolean).pop().toLowerCase();
const FROM_Q = new URLSearchParams(location.search).get("q") || "";
const main = $("#tmain");

(async function init() {
  let t;
  try { t = await api("/api/torrent/" + IH); }
  catch (e) { return renderMissing(e.status === 404); }
  document.title = t.name + " · DHT Search";
  render(t);
  api(`/api/torrent/${IH}/related`).then(r => renderRelated(r)).catch(() => renderRelated([]));
})();

function renderMissing(notFound) {
  main.replaceChildren(
    backLink(),
    h("div", { class: "tcard center" },
      h("h1", { class: "ttitle" }, notFound ? "This torrent is not in the index" : "Could not load the torrent"),
      h("p", { class: "muted" }, notFound ? "The crawler may not have fetched its metadata yet, or it was removed by the blocklist." : "Check that the server is running."),
      h("p", null, h("code", null, IH)),
      h("p", null, h("a", { class: "btn-s primary", href: "/" }, "Back to search"))));
}

function backLink() {
  const a = h("a", { class: "back", href: "/" + (FROM_Q ? "?q=" + encodeURIComponent(FROM_Q) : "") }, FROM_Q ? "← Back to results for “" + FROM_Q + "”" : "← Back to search");
  a.onclick = e => {                                    // coming from the search page, going back keeps filters and page
    if (document.referrer && new URL(document.referrer).origin === location.origin && history.length > 1) { e.preventDefault(); history.back(); }
  };
  return h("div", { class: "tback" }, a);
}

/* ---------- local helpers ---------- */
const extOf = p => { const b = p.slice(p.lastIndexOf("/") + 1), i = b.lastIndexOf("."); return i > 0 ? b.slice(i + 1).toLowerCase().slice(0, 8) : ""; };
const plain = s => s.toLowerCase().normalize("NFD").replace(/\p{M}/gu, "");
function card(title, body, cls) { return h("section", { class: "tcard " + (cls || "") }, title ? h("h2", null, title) : null, body); }
function dl(rows) {
  return h("dl", { class: "tdl" }, rows.filter(Boolean).map(([k, v]) => [h("dt", null, k), h("dd", null, v)]));
}

function render(t) {
  const files = t.files;
  const verified = t.health_src === "scrape";
  const copyMagnet = h("button", { class: "btn-s", type: "button" }, "Copy magnet"); copyMagnet.onclick = () => copyText(t.magnet, copyMagnet);
  const copyHash = h("button", { class: "btn-s", type: "button" }, "Copy infohash"); copyHash.onclick = () => copyText(t.ih, copyHash);
  const simQ = t.name.replace(/[^\p{L}\p{N}]+/gu, " ").trim().split(/\s+/).filter(w => w.length > 2).slice(0, 4).join(" ");
  const isAdmin = Array.isArray(t.peers_known);          // the API only sends peers to an admin session
  const hiddenBanner = t.hidden_by && t.hidden_by.length ? h("div", { class: "adm-banner", id: "adm-banner" },
    h("b", null, "Hidden from public search"), " by " + [t.hidden_by.includes("ai") ? "AI moderation" : null,
      t.hidden_by.filter(x => x !== "ai").length ? t.hidden_by.filter(x => x !== "ai").length + " admin panel rule(s)" : null].filter(Boolean).join(" and ") +
    ". Only you (with an admin session) see this page; for everyone else it does not exist. Admin → AI moderation can show it again.") : null;
  main.replaceChildren(...[                              // replaceChildren(null) would insert the text "null"
    backLink(),
    hiddenBanner,
    h("h1", { class: "ttitle" }, t.name),
    h("div", { class: "tchips" },
      h("span", { class: "chip" }, t.category), h("span", { class: "chip" }, fmtBytes(t.size)), h("span", { class: "chip" }, fmtNum(t.file_count) + " files"),
      t.private ? h("span", { class: "chip warn", title: "Private torrent: DHT/PEX are not used" }, "private") : null,
      verified ? h("span", { class: "chip ok", title: "Health verified with a tracker " + fmtRel(t.health_at) }, "health verified ✓")
               : h("span", { class: "chip warn", title: "No tracker answer yet" }, "health estimated")),
    h("div", { class: "tactions" },
      h("a", { class: "btn-s primary", href: t.magnet }, "🧲 Open magnet"), copyMagnet, copyHash,
      simQ ? h("a", { class: "btn-s", href: "/?q=" + encodeURIComponent(simQ) }, "Find similar") : null),
    h("div", { class: "tgrid" },
      card("Health", healthBlock(t)),
      card("Information", infoBlock(t)),
      card("Who reports what", sourcesBlock(t, isAdmin), "wide"),
      card("File types", compositionBlock(t)),
      h("div", { id: "related-slot", class: "tcard" }, h("h2", null, "Similar torrents"), h("div", { class: "muted" }, "Searching…")),
      card("Files", filesBlock(files, t), "wide"),
      isAdmin ? card("Known peers (admin only)", peerTable(t.peers_known, ip => { location.href = "/#admin"; sessionStorage.setItem("admSearchIp", ip); }), "wide") : null)].filter(Boolean));
  if (hiddenBanner)                                      // rule names (the page does not know them)
    fetch("/api/admin/rules", { credentials: "same-origin" }).then(r => r.ok ? r.json() : null).then(r => {
      if (!r) return;
      const names = t.hidden_by.map(id => (r.rules.find(x => x.id === id) || {}).term).filter(Boolean);
      if (names.length) hiddenBanner.append(" Rules: ", ...names.map(n => h("span", { class: "chip rule" }, "“" + n + "”")));
    }).catch(() => {});
}

/* ---------- health ---------- */
function healthBlock(t) {
  const verified = t.health_src === "scrape";
  const big = (label, val, sub) => h("div", { class: "big" }, h("div", { class: "l" }, label), h("div", { class: "v" }, val), sub ? h("div", { class: "s" }, sub) : null);
  const none = t.health_src === "none";
  const seedsVal = none ? "?" : (verified ? "" : "≥ ") + fmtNum(t.seeders);
  const why = verified ? "Tracker answer (scrape), " + fmtRel(t.health_at)
    : none ? "Not measured yet" : t.health_src === "legacy" ? "Old unverified measurement" : "Only peers seen when connecting (lower bound); no tracker answered";
  const hh = t.hh || [];
  const nxt = t.next_check_at ? (t.next_check_at * 1000 <= Date.now() ? "as soon as there is a free slot" : "around " + fmtDateTime(t.next_check_at)) : "—";
  return h("div", null,
    h("div", { class: "statebox st-" + t.state }, h("b", null, STATE_LABEL[t.state] || t.state), h("span", null, STATE_HELP[t.state] || "")),
    h("div", { class: "bigrow" }, big("Seeders", seedsVal, verified ? "highest tracker figure" : "estimated"),
      big("Confirmed seeders", t.hd ? fmtNum(t.hd.cs) : "?", t.seed_ok_at ? "last connected " + fmtRel(t.seed_ok_at) : "never connected to one"),
      big("Peers", none ? "?" : fmtNum(t.peers), "leechers"), big("Figures measured", fmtRel(t.health_at), fmtDateTime(t.health_at))),
    t.checked_at && t.checked_at - t.health_at > 3600 ? h("p", { class: "warn" }, `Note: these figures are from ${fmtRel(t.health_at)}. The last check (${fmtRel(t.checked_at)}) got no answer from any tracker, so they were kept.`) : null,
    h("p", { class: "muted small" }, why + ". Next check: " + nxt + " (alive every 6 h, weak 12 h, dead every 3 days)."),
    hh.length >= 2 ? healthChart(hh) : h("div", { class: "muted small" }, hh.length ? "Only one measurement; the trend will appear after the next refresh." : "No measurement history yet."),
    hh.length >= 2 ? h("div", { class: "legend" }, h("span", null, h("i", { style: "background:var(--s1)" }), "Seeders"), h("span", null, h("i", { style: "background:var(--s2)" }), "Peers"),
      h("span", { class: "muted" }, "● verified with a tracker · ○ estimated")) : null);
}

/* ---------- who reports what ---------- */
function sourcesBlock(t, isAdmin) {
  const d = t.hd;
  const out = h("div");
  if (!d) out.append(h("p", { class: "muted" }, "No per-source breakdown yet: it will appear after the next measurement (old measurements only kept the maximum)."));
  else {
    const host = u => { try { return new URL(u.replace(/^udp:/, "http:")).hostname; } catch { return u; } };
    const rows = d.tr.map(x => h("tr", null,
      h("td", { title: x.u }, host(x.u)),
      h("td", { class: x.s != null ? "" : "bad" }, x.s != null ? fmtNum(x.s) : "—"),
      h("td", null, x.l != null ? fmtNum(x.l) : "—"),
      h("td", { title: "IP:port addresses the tracker returned when the crawler announced" }, x.r != null ? fmtNum(x.r) : "—"),
      h("td", { class: x.s != null ? "ok" : "bad" }, x.s != null ? "answered" : (x.e || x.ae || "no response"))));
    rows.push(h("tr", { class: "sep" }, h("td", null, h("b", null, "Direct connection (crawler)")),
      h("td", { class: d.cs ? "ok" : "bad" }, fmtNum(d.cs)), h("td", null, fmtNum(d.cp)), h("td", null, fmtNum(d.ci) + " connected"),
      h("td", null, d.md ? "metadata received" : "—")));
    rows.push(h("tr", null, h("td", null, "DHT (get_peers)"), h("td", null, "—"), h("td", null, "—"), h("td", null, fmtNum(d.dht)), h("td", null, "unverified")));
    out.append(
      h("div", { class: "tscroll" }, h("table", { class: "data" },
        h("thead", null, h("tr", null, ["Source", "Seeders", "Leechers", "Addresses", "Status"].map(x => h("th", null, x)))), h("tbody", null, rows))),
      h("p", { class: "muted small" }, `Probe ${fmtRel(d.at)}. Trackers report what THEY believe (peers that announced to them in the last ~hour, reachable or not). `,
        h("b", null, "Only “Direct connection” is verified"), ": peers that accepted the connection and showed the complete torrent. Many seeders according to trackers and 0 connected usually means ",
        "seeders behind NAT, uTP/encryption only, expired, or inflated figures. The “Seeders” figure above is the maximum across trackers."));
  }
  if (isAdmin) out.append(verifyBox(t));
  return out;
}

function verifyBox(t) {
  const btn = h("button", { type: "button", class: "btn-s primary" }, "Verify live now");
  const box = h("div");
  btn.onclick = async () => {
    btn.disabled = true; box.replaceChildren(h("p", { class: "muted small" }, "Asking the trackers and the DHT and connecting to every peer (≈ 20-30 s)…"));
    try {
      const r = await fetch("/api/admin/verify", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ih: t.ih }) });
      const j = await r.json(); if (!r.ok) throw new Error(j.error || r.status);
      for (let i = 0; i < 90; i++) {
        await new Promise(res => setTimeout(res, 1500));
        const v = await (await fetch("/api/admin/verify/" + j.id, { credentials: "same-origin" })).json();
        if (v.state === "running") { box.replaceChildren(h("div", { class: "vlog muted" }, v.log.map(l => h("div", null, "· " + l)))); continue; }
        if (v.state === "error" || !v.result) throw new Error(v.log.slice(-1)[0] || "error");
        box.replaceChildren(verifyReport(v.result)); return;
      }
      throw new Error("took too long");
    } catch (e) { box.replaceChildren(h("p", { class: "warn" }, "Could not verify: " + e.message)); }
    finally { btn.disabled = false; }
  };
  return h("div", { class: "vbox" }, h("h3", null, "Independent verification (admin only)"),
    h("p", { class: "muted small" }, "Without libtorrent or the crawler: scrape and announce to every tracker, a DHT lookup, and a BitTorrent handshake with every address to see whether it really has the complete torrent. Same thing from the console: ",
      h("code", null, "python verify.py " + t.ih)), btn, box);
}

function verifyReport(r) {
  const s = r.summary, good = s.seeds_confirmed > 0;
  const host = u => { try { return new URL(u.replace(/^udp:/, "http:")).hostname; } catch { return u; } };
  const row = (name, said, ret, c, err) => h("tr", null, h("td", null, name), h("td", null, said), h("td", null, ret),
    h("td", null, fmtNum(c.reachable) + " / " + fmtNum(c.tested)), h("td", { class: c.seeds ? "ok" : "" }, fmtNum(c.seeds)), h("td", null, fmtNum(c.leechers)), h("td", { class: err ? "bad" : "" }, err || "ok"));
  const rows = r.trackers.map(x => row(host(x.url), x.scrape ? `${fmtNum(x.scrape.seeders)} / ${fmtNum(x.scrape.leechers)}` : "—", fmtNum(x.returned), x.check, x.scrape_error || x.announce_error));
  if (r.dht) rows.push(row(`DHT (${fmtNum(r.dht.nodes_replied)} nodes)`, "—", fmtNum(r.dht.peers), r.dht.check, r.dht.error || (r.dht.nodes_replied ? "" : "no node answered")));
  const peers = r.peers.filter(p => p.status === "seed" || p.status === "leecher").slice(0, 40);
  return h("div", null,
    h("div", { class: "verdict" + (good ? " good" : "") }, h("b", null, r.verdict)),
    h("div", { class: "tscroll" }, h("table", { class: "data" },
      h("thead", null, h("tr", null, ["Source", "Says S / L", "Addresses", "Answer", "Seeders OK", "Leechers OK", "Error"].map(x => h("th", null, x)))), h("tbody", null, rows))),
    h("p", { class: "muted small" }, `${fmtNum(s.addresses)} distinct addresses, ${fmtNum(s.tested)} tested, ${fmtNum(s.reachable)} answered → `,
      h("b", null, `${fmtNum(s.seeds_confirmed)} seeders and ${fmtNum(s.leechers_confirmed)} leechers confirmed`), ` (${s.took_s} s). `,
      "Unencrypted TCP only: a peer that only accepts uTP or encryption counts as “no answer” even if it exists."),
    peers.length ? h("div", { class: "tscroll" }, h("table", { class: "data" },
      h("thead", null, h("tr", null, ["Peer", "Role", "Completed", "Client", "Via"].map(x => h("th", null, x)))),
      h("tbody", null, peers.map(p => h("tr", null, h("td", { class: "mono" }, `${p.ip}:${p.port}`), h("td", { class: p.status === "seed" ? "ok" : "" }, p.status === "seed" ? "seeder" : "leecher"),
        h("td", null, Math.round((p.progress || 0) * 100) + " %"), h("td", null, p.client || "—"), h("td", null, p.sources.map(x => x === "DHT" ? x : host(x)).join(", "))))))) : null);
}

function healthChart(hh) {
  const W = 560, H = 180, m = { l: 40, r: 10, t: 10, b: 22 }, iw = W - m.l - m.r, ih = H - m.t - m.b;
  const t0 = hh[0][0], t1 = hh[hh.length - 1][0], span = Math.max(t1 - t0, 1);
  const mx = Math.max(4, ...hh.map(p => Math.max(p[1], p[2])));      // minimum 4: avoids rounded fractional ticks (1,1,1,0)
  const e = Math.pow(10, Math.floor(Math.log10(mx))), f = mx / e, top = (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * e;
  const X = t => m.l + (t - t0) / span * iw, Y = v => m.t + ih - v / top * ih;
  const el = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Seeders and peers over time", class: "hchart" });
  for (let i = 0; i <= 4; i++) {
    const v = top * i / 4;
    el.append(svg("line", { class: "tick-line", x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v) }), svg("text", { x: m.l - 6, y: Y(v) + 3.5, "text-anchor": "end" }, fmtNum(v)));
  }
  for (let i = 0; i <= 3; i++) { const t = t0 + span * i / 3; el.append(svg("text", { x: X(t), y: H - 5, "text-anchor": i === 0 ? "start" : i === 3 ? "end" : "middle" }, fmtAxis(t, span))); }
  [[1, "var(--s1)"], [2, "var(--s2)"]].forEach(([k, c]) => {
    el.append(svg("path", { class: "line", stroke: c, d: hh.map((p, i) => (i ? "L" : "M") + X(p[0]).toFixed(1) + " " + Y(p[k]).toFixed(1)).join("") }));
    for (const p of hh) {
      const ok = p[3] === "s";
      el.append(svg("circle", { cx: X(p[0]), cy: Y(p[k]), r: 3.6, fill: ok ? c : "var(--surface)", stroke: c, "stroke-width": 2 },
        svg("title", null, `${fmtDateTime(p[0])} · ${k === 1 ? "seeders" : "peers"}: ${fmtNum(p[k])} · ${ok ? "verified with a tracker" : "estimated"}`)));
    }
  });
  return el;
}

/* ---------- information ---------- */
function infoBlock(t) {
  const hashCell = h("span", { class: "mono" }, t.ih);
  return h("div", null, dl([
    ["Infohash", hashCell],
    ["Size", `${fmtBytes(t.size)} (${fmtNum(t.size)} bytes)`],
    ["Files", fmtNum(t.file_count) + (t.files_truncated ? ` · the first ${fmtNum(t.files.length)} are stored` : "")],
    ["Pieces", t.piece_length ? `${fmtNum(t.num_pieces)} × ${fmtBytes(t.piece_length)}` : "—"],
    t.created ? ["Creation date", fmtDate(t.created) + (t.created_by ? " · " + t.created_by : "")] : (t.created_by ? ["Created with", t.created_by] : null),
    ["Indexed", `${fmtDateTime(t.indexed_at)} (${fmtRel(t.indexed_at)})`],
    ["Last seen", fmtRel(t.last_seen)],
    ["Discovered via", SRC_LONG[t.src] || t.src],
    ["Private", t.private ? "yes (DHT and PEX are not used)" : "no"],
  ]),
    !t.created ? h("p", { class: "muted small" }, "The creation date does not travel with metadata fetched via DHT (BEP 9 only transfers the “info” dictionary): the reliable date we have is when it was indexed.") : null,
    t.comment ? h("div", { class: "tcomment" }, h("div", { class: "l" }, "Comment"), h("div", { class: "pre" }, t.comment)) : null,
    t.trackers && t.trackers.length ? h("div", { class: "ttrackers" }, h("div", { class: "l" }, `Trackers (${t.trackers.length})`), h("ul", null, t.trackers.map(u => h("li", { class: "mono" }, u)))) : null);
}

/* ---------- composition ---------- */
function compositionBlock(t) {
  const by = new Map();
  for (const [p, s] of t.files) { const e = extOf(p) || "(no extension)"; const c = by.get(e) || { n: 0, b: 0 }; c.n++; c.b += s; by.set(e, c); }
  const rows = [...by.entries()].sort((a, b) => b[1].b - a[1].b).slice(0, 8);
  if (!rows.length) return h("div", { class: "muted" }, "No file list.");
  const max = Math.max(...rows.map(r => r[1].b), 1);
  return h("div", null, h("div", { class: "hbars" }, rows.map(([e, c]) => h("div", { class: "hb wide2", title: `${e}: ${fmtNum(c.n)} files, ${fmtBytes(c.b)}` },
    h("span", { class: "n" }, e === "(no extension)" ? e : "." + e), h("span", { class: "bar" }, h("i", { style: `width:${Math.max(c.b / max * 100, 1).toFixed(1)}%` })),
    h("span", { class: "val" }, fmtNum(c.n), " · ", fmtBytes(c.b))))),
    t.files_truncated ? h("p", { class: "muted small" }, `Computed over the first ${fmtNum(t.files.length)} of ${fmtNum(t.file_count)} files.`) : null);
}

/* ---------- files: tree / list / filters ---------- */
function filesBlock(files, t) {
  const st = { q: "", ext: "", sort: "name", mode: "tree" };
  const list = h("div", { class: "flist" });
  const note = h("div", { class: "muted small" });
  const exts = new Map();
  for (const [p] of files) { const e = extOf(p); if (e) exts.set(e, (exts.get(e) || 0) + 1); }
  const topExts = [...exts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8);
  let root = null;

  const input = h("input", { class: "file-filter", type: "search", placeholder: `Filter ${fmtNum(files.length)} files…`, "aria-label": "Filter files" });
  const sortSel = h("select", { "aria-label": "Sort files" }, h("option", { value: "name" }, "Sort: name"), h("option", { value: "size" }, "Sort: size (largest first)"));
  const modeBtn = h("button", { type: "button", class: "btn-s" }, "Show as list");
  const expand = h("button", { type: "button", class: "btn-s" }, "Expand all");
  const collapse = h("button", { type: "button", class: "btn-s" }, "Collapse all");
  const chips = h("div", { class: "chips" }, topExts.map(([e, n]) => h("button", { type: "button", class: "chip f", "data-e": e, onclick: () => { st.ext = st.ext === e ? "" : e; draw(); } }, "." + e, " ", h("i", null, fmtNum(n)))));

  const cmp = () => st.sort === "size" ? (a, b) => b.size - a.size : (a, b) => a.name.localeCompare(b.name, "en", { numeric: true });
  const sizeCell = n => h("span", { class: "fsz" }, fmtBytes(n));

  function fileRow(f, depth) {
    return h("div", { class: "frow", style: `--d:${depth}`, title: f.path }, h("span", { class: "fname" }, f.name), sizeCell(f.size));
  }
  function dirNode(node, depth, open) {
    const body = h("div", { class: "tbody", hidden: !open });
    let built = false;
    const build = () => { if (!built) { built = true; body.replaceChildren(...children(node, depth + 1)); } };
    const caret = h("span", { class: "caret" }, open ? "▾" : "▸");
    const btn = h("button", { type: "button", class: "frow dir", style: `--d:${depth}`, "aria-expanded": String(open), "data-dir": "1" },
      caret, h("span", { class: "fname" }, node.name + "/"), h("span", { class: "fcnt" }, fmtNum(node.count) + " files"), sizeCell(node.size));
    btn.onclick = () => { const o = body.hidden; body.hidden = !o; caret.textContent = o ? "▾" : "▸"; btn.setAttribute("aria-expanded", String(o)); if (o) build(); };
    if (open) build();
    return h("div", { class: "tnode" }, btn, body);
  }
  function children(node, depth) {
    const c = cmp();
    const dirs = [...node.dirs.values()].sort(st.sort === "size" ? (a, b) => b.size - a.size : (a, b) => a.name.localeCompare(b.name, "en", { numeric: true }));
    return [...dirs.map(d => dirNode(d, depth, false)), ...node.files.slice().sort(c).map(f => fileRow(f, depth))];
  }
  function buildTree() {
    const r = { name: "", dirs: new Map(), files: [], size: 0, count: 0 };
    for (const [p, s] of files) {
      const parts = p.split("/"); let n = r;
      for (let i = 0; i < parts.length - 1; i++) {
        let c = n.dirs.get(parts[i]);
        if (!c) { c = { name: parts[i], dirs: new Map(), files: [], size: 0, count: 0 }; n.dirs.set(parts[i], c); }
        n = c;
      }
      n.files.push({ name: parts[parts.length - 1], size: s, path: p });
    }
    (function agg(n) { n.size = 0; n.count = 0; for (const d of n.dirs.values()) { agg(d); n.size += d.size; n.count += d.count; } for (const f of n.files) { n.size += f.size; n.count++; } })(r);
    return r;
  }
  function highlight(path) {
    const q = st.q; if (!q) return path;
    const i = plain(path).indexOf(q);
    return i < 0 ? path : [path.slice(0, i), h("mark", null, path.slice(i, i + q.length)), path.slice(i + q.length)];
  }
  function draw() {
    for (const c of chips.children) c.classList.toggle("on", c.dataset.e === st.ext);
    const filtering = st.q || st.ext;
    const flat = filtering || st.mode === "list";
    expand.hidden = collapse.hidden = flat;
    modeBtn.textContent = st.mode === "tree" ? "Show as list" : "Show as tree";
    modeBtn.hidden = !!filtering;
    if (flat) {
      let rows = files.map(([p, s]) => ({ path: p, size: s })).filter(f => (!st.ext || extOf(f.path) === st.ext) && (!st.q || plain(f.path).includes(st.q)));
      rows.sort(st.sort === "size" ? (a, b) => b.size - a.size : (a, b) => a.path.localeCompare(b.path, "en", { numeric: true }));
      const total = rows.reduce((a, r) => a + r.size, 0), shown = rows.slice(0, 600);
      list.replaceChildren(...shown.map(f => h("div", { class: "frow flat", title: f.path }, h("span", { class: "fname" }, highlight(f.path)), sizeCell(f.size))));
      note.textContent = `${fmtNum(rows.length)} files · ${fmtBytes(total)}` + (rows.length > 600 ? ` · showing 600` : "") +
        (t.files_truncated ? ` · the torrent has ${fmtNum(t.file_count)} files and only the first ${fmtNum(files.length)} are stored` : "");
      if (!rows.length) list.replaceChildren(h("div", { class: "empty" }, "No file matches."));
      return;
    }
    root = buildTree();
    let node = root, depth = 0;
    const kids = [];
    // single root folder (the usual case): shown already open to save a pointless click
    if (!node.files.length && node.dirs.size === 1) kids.push(dirNode([...node.dirs.values()][0], 0, true));
    else kids.push(...children(node, 0));
    list.replaceChildren(...kids);
    note.textContent = `${fmtNum(files.length)} files · ${fmtBytes(root.size)}` +
      (t.files_truncated ? ` · the torrent has ${fmtNum(t.file_count)} files and only the first ${fmtNum(files.length)} are stored` : "");
  }
  input.oninput = () => { st.q = plain(input.value.trim()); draw(); };
  sortSel.onchange = () => { st.sort = sortSel.value; draw(); };
  modeBtn.onclick = () => { st.mode = st.mode === "tree" ? "list" : "tree"; draw(); };
  expand.onclick = () => { let n = 0; const open = () => { for (const b of list.querySelectorAll('button[data-dir][aria-expanded="false"]')) { if (n++ < 3000) b.click(); } };
    for (let i = 0; i < 12; i++) { const before = n; open(); if (n === before) break; } };
  collapse.onclick = () => draw();

  const el = h("div", null,
    h("div", { class: "ftools" }, input, sortSel, modeBtn, expand, collapse), topExts.length ? chips : null, note, list);
  draw();
  return el;
}

/* ---------- similar ---------- */
function renderRelated(items) {
  const slot = $("#related-slot"); if (!slot) return;
  slot.replaceChildren(h("h2", null, "Similar torrents"),
    items.length ? h("ul", { class: "rel" }, items.map(r => h("li", null,
      h("a", { class: "tlink", href: "/torrent/" + r.ih + (FROM_Q ? "?q=" + encodeURIComponent(FROM_Q) : "") }, r.name),
      h("span", { class: "muted small" }, `${fmtBytes(r.size)} · ${fmtNum(r.seeders)} seeders${r.verified ? " ✓" : ""} · ${r.category}`))))
      : h("div", { class: "muted" }, "No torrents with similar names in the index."));
}
