"use strict";
// Vanilla JS, no framework, no network beyond this origin. All text goes in through textContent (h()), never innerHTML.

const main = document.getElementById("main");
let keyHandler = null;       // per-page keyboard handler
let timer = null;            // per-page poller
let onLeave = [];            // per-page cleanup callbacks

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

function fill(el, ...kids) { el.replaceChildren(...kids.flat(2).filter((k) => k !== null && k !== undefined && k !== false)); }

async function api(path, body) {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch("/api/" + path, opt);
  let data = {};
  try { data = await r.json(); } catch (e) { /* non-JSON error page */ }
  if (!r.ok) throw new Error(data.error || (r.status + " " + r.statusText));
  return data;
}

function toast(msg, bad) {
  const t = document.getElementById("toast");
  t.textContent = msg; t.className = "show" + (bad ? " error" : "");
  clearTimeout(toast.t); toast.t = setTimeout(() => { t.className = ""; }, 3600);
}

// per-viewer conveniences only; the page works with storage blocked
const store = {
  get(k, d) { try { const v = localStorage.getItem("ave." + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem("ave." + k, JSON.stringify(v)); } catch (e) { /* private window */ } },
};

const clock = (s) => (s === null || s === undefined || isNaN(s)) ? "" : Math.floor(Math.abs(s) / 60) + ":" + (Math.abs(s) % 60).toFixed(1).padStart(4, "0");
const relClock = (s) => (s < -0.05 ? "−" : "") + clock(s);
const secs = (s) => (s === null || s === undefined) ? "" : (s >= 100 ? Math.round(s) : s.toFixed(1)) + " s";
const stClass = (s) => "st-" + String(s || "").replace(/\W/g, "");
const reduceMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
const plural = (n, a, b) => n + " " + (n === 1 ? a : (b || a + "s"));

// ---------------------------------------------------------------- theme
const THEMES = ["auto", "light", "dark"];
function applyTheme(mode) {
  const root = document.documentElement;
  if (mode === "auto") root.removeAttribute("data-theme"); else root.setAttribute("data-theme", mode);
  const b = document.getElementById("theme");
  b.textContent = mode === "auto" ? "Theme: system" : mode === "light" ? "Theme: light" : "Theme: dark";
  b.dataset.mode = mode;
  b.dispatchEvent(new Event("themechange"));
}
document.querySelector(".skiplink").addEventListener("click", (e) => { e.preventDefault(); main.focus(); });
document.getElementById("theme").addEventListener("click", () => {
  const next = THEMES[(THEMES.indexOf(store.get("theme", "auto")) + 1) % 3];
  store.set("theme", next); applyTheme(next);
});
applyTheme(store.get("theme", "auto"));

// ---------------------------------------------------------------- shared bits
function setBadge(page, n) {
  const a = document.querySelector('#nav a[data-p="' + page + '"]'); if (!a) return;
  const old = a.querySelector(".badge"); if (old) old.remove();
  if (n > 0) a.append(h("span", { class: "badge", "aria-label": n + " waiting" }, n));
}
async function refreshBadges(d) {
  try {
    d = d || await api("dashboard");
    const open = Object.values(d.clip_counts || {}).reduce((a, r) => a + r.proposed + r.maybe, 0);
    setBadge("clips", open);
    const pub = d.waiting.find((w) => w.kind === "publish");
    setBadge("publish", pub ? (parseInt(pub.text, 10) || 1) : 0);
    document.getElementById("ep").textContent = d.title || d.path;
    document.getElementById("ep").title = d.path;
  } catch (e) { /* the page itself shows errors */ }
}

async function runJob(cmd, args, quiet) {
  try {
    const j = await api("jobs", { cmd, args: args || {} });
    if (!quiet) toast("Started ave " + cmd);
    return j;
  } catch (e) { toast(e.message, true); return null; }
}

// ---------------------------------------------------------------- dashboard
const STAGE_NOTE = {
  assembly: (d) => editNote(d, "assembly"), rough: (d) => editNote(d, "rough"), final: (d) => editNote(d, "final"),
  selects: (d) => Object.entries(d.clip_counts || {}).map(([s, r]) => r.total + " in " + s).join(", "),
};
function editNote(d, stage) {
  return Object.entries(d.edits || {}).filter(([k]) => k === stage || k.startsWith(stage + ".")).map(([k, v]) => (k.includes(".") ? k.split(".")[1] + " " : "") + "v" + v[v.length - 1]).join(", ");
}

async function dashboard() {
  const d = await api("dashboard");
  refreshBadges(d);
  const titles = Object.fromEntries(d.stages.map((s) => [s.stage, s.title]));
  const next = d.next && d.next.stage;
  const tile = (s) => {
    const cls = s.done ? "done" : s.skipped ? "skipped" : next === s.stage ? "next" : s.blocked_by.length ? "blocked" : "";
    const state = s.done ? ["✓", "Done"] : s.skipped ? ["–", "Not needed here"] : next === s.stage ? ["▶", "Next up"]
      : s.blocked_by.length ? ["…", "Waits on " + s.blocked_by.map((b) => (titles[b] || b).toLowerCase()).join(", ")] : ["○", s.optional ? "Optional, ready" : "Ready"];
    const note = (STAGE_NOTE[s.stage] || (() => ""))(d);
    return h("li", { class: "stg " + cls + (s.human_gate ? " gate" : "") },
      h("span", { class: "name" }, s.title),
      h("span", { class: "state" }, h("span", { class: "glyph", "aria-hidden": "true" }, state[0]), state[1]),
      note ? h("span", { class: "note" }, note) : null,
      s.human_gate && s.done ? h("span", { class: "note" }, "You sign off on this one") : null);
  };
  const clipRows = Object.entries(d.clip_counts || {});
  const seg = (n, tot, color) => h("i", { style: "width:" + (tot ? (100 * n / tot) : 0) + "%;background:var(" + color + ")" });
  const doneJob = async (a) => { const j = await runJob(a.cmd, a.args); if (j) location.hash = "#/jobs/" + j.id; };
  return h("div", {},
    h("div", { class: "page-head" }, h("h1", {}, d.title || "Episode"),
      h("p", {}, Object.entries(d.speakers || {}).map(([, n]) => n).join(" and "))),
    h("div", { class: "dash" },
      h("div", { class: "col" },
        h("section", { class: "panel" }, h("h2", {}, "Waiting on you"),
          d.waiting.length ? h("ul", { class: "todo" }, d.waiting.slice().sort((x, y) => (y.page ? 1 : 0) - (x.page ? 1 : 0)).map((w) => h("li", {}, h("span", {}, w.text),
            w.page ? h("a", { href: "#/" + w.page }, h("button", { class: "primary", tabindex: "-1" }, w.page === "clips" ? "Open the review desk" : "Open " + w.page)) : null)))
            : h("p", { class: "ok", style: "margin-top:8px" }, "Nothing is waiting on you.")),
        h("section", { class: "panel" }, h("h2", {}, "Pipeline"),
          h("p", { class: "muted small", style: "margin-top:4px" }, "Read from the files on disk, so it is always current. A yellow underline marks a step where you sign off."),
          h("ol", { class: "tape", style: "list-style:none;padding:0" }, d.stages.map(tile)),
          d.next ? h("div", { class: "row", style: "margin-top:14px" },
            h("span", {}, h("strong", {}, "Next: " + d.next.title + ". "), d.next.human_gate ? "It stops for your OK afterwards. " : ""),
            h("code", {}, d.next.command),
            h("button", { onclick: async () => { try { await navigator.clipboard.writeText(d.next.command); toast("Command copied"); } catch (e) { toast("Select the command and copy it", true); } } }, "Copy command")) : null),
        h("section", { class: "panel" }, h("h2", {}, "Last log lines"),
          h("pre", { class: "logbox", style: "margin-top:8px" }, d.recent_log.join("\n") || "(nothing logged yet)"),
          h("div", { class: "faint small", style: "margin-top:6px" }, "From " + d.progress_file))),
      h("div", { class: "col" },
        h("section", { class: "panel" }, h("h2", {}, "Clips"),
          clipRows.length ? h("div", { class: "sessions" }, clipRows.map(([sid, r]) => h("div", { class: "sess" },
            h("div", { class: "row", style: "margin:0" }, h("strong", {}, sid), h("span", { class: "muted small" }, plural(r.total, "clip"))),
            h("div", { class: "stack-bar", role: "img", "aria-label": r.approved + " kept, " + r.maybe + " maybe, " + r.rejected + " skipped, " + r.proposed + " not looked at" },
              seg(r.approved, r.total, "--spruce"), seg(r.maybe, r.total, "--slate"), seg(r.rejected, r.total, "--pencil")),
            h("div", { class: "legend" }, h("span", {}, r.approved + " kept"), h("span", {}, r.maybe + " maybe"), h("span", {}, r.rejected + " skipped"), h("span", {}, r.proposed + " to look at"),
              h("span", {}, r.previews + " of " + r.total + " have a preview"))))) : h("p", { class: "muted" }, "No clip candidates yet."),
          h("div", { class: "row" }, h("a", { href: "#/clips" }, h("button", { class: "primary", tabindex: "-1" }, "Review clips")))),
        h("section", { class: "panel" }, h("h2", {}, "One-click, safe"),
          h("p", { class: "muted small", style: "margin:4px 0 6px" }, "These start an allowlisted ave command and open its live log. Nothing here publishes, approves or spends money."),
          d.actions.map((a) => h("div", { class: "act" }, h("strong", {}, a.label), h("span", { class: "why" }, a.why), h("button", { onclick: () => doneJob(a) }, "Run"))),
          h("div", { class: "row" }, h("a", { href: "#/jobs" }, "All jobs and the custom runner"))),
        Object.keys(d.edits).length ? h("section", { class: "panel" }, h("h2", {}, "Edits on disk"),
          h("div", { class: "row" }, Object.entries(d.edits).map(([k, v]) => h("span", { class: "chip" }, k + " v" + v[v.length - 1])))) : null)));
}

// ---------------------------------------------------------------- clips: the review desk
const peakCache = new Map();
async function peaksFor(url, n) {
  if (peakCache.has(url)) return peakCache.get(url);
  const AC = window.OfflineAudioContext || window.webkitOfflineAudioContext;
  if (!AC) return null;
  try {
    const buf = await (await fetch(url)).arrayBuffer();
    const ab = await new AC(1, 44100, 44100).decodeAudioData(buf);
    const ch = ab.getChannelData(0), step = Math.max(1, Math.floor(ch.length / n)), out = [];
    for (let i = 0; i < n; i++) { let m = 0; for (let j = i * step, e = Math.min(ch.length, j + step); j < e; j += 4) m = Math.max(m, Math.abs(ch[j])); out.push(m); }
    const top = Math.max(...out, 0.05);
    const res = { dur: ab.duration, p: out.map((v) => v / top) };
    peakCache.set(url, res); return res;
  } catch (e) { peakCache.set(url, null); return null; }
}

async function clipsPage() {
  const F = Object.assign({ session: "", top: 0, min: 0, tag: "", sort: "score", unrev: false, autoplay: true, loop: true, speed: 1 }, store.get("clipfilters", {}));
  const S = { list: null, sel: null, detail: null, undo: [], pending: null, pend: null, rows: new Map(), jobWatch: null, tightPlay: false, cuts: new Set(), reasonsFor: null };
  const root = h("div", {});
  let listSeq = 0, detSeq = 0, rafId = 0;
  const saveF = () => store.set("clipfilters", F);

  // ---- tools
  const sessSeg = h("div", { class: "seg", role: "group", "aria-label": "Session" });
  const topIn = h("input", { type: "number", min: "0", step: "1", "aria-label": "Best how many clips (0 means all)", value: F.top || "" , placeholder: "All" });
  const topMinus = h("button", { type: "button", "aria-label": "Fewer clips" }, "−"), topPlus = h("button", { type: "button", "aria-label": "More clips" }, "+");
  const minR = h("input", { type: "range", min: "0", max: "100", step: "1", value: F.min, "aria-label": "Minimum score" });
  const minOut = h("output", {}, String(F.min));
  const tagSel = h("select", { "aria-label": "Tag" }); const sortSel = h("select", { "aria-label": "Sort" }, [["score", "Best score first"], ["time", "In episode order"], ["duration", "Longest first"], ["rank", "Rank within session"]].map(([v, l]) => h("option", { value: v }, l)));
  sortSel.value = F.sort;
  const unrev = h("input", { type: "checkbox", checked: F.unrev || undefined }); unrev.checked = F.unrev;
  const count = h("div", { class: "count", "aria-live": "polite" });
  const meterTrack = h("div", { class: "bar-track", role: "img", "aria-label": "Progress" }), meterTxt = h("span", { class: "txt" });
  const apBtn = h("button", { class: "tog", type: "button", onclick: () => { F.autoplay = !F.autoplay; saveF(); paintToggles(); } }, "Play when I move");
  const undoBtn = h("button", { type: "button", onclick: () => undo(), disabled: true }, "Undo ", h("span", { class: "kbd" }, "z"));
  const tools = h("div", { class: "desk-tools" },
    sessSeg,
    h("div", { class: "tool" }, h("span", { class: "lab" }, "Best"), h("div", { class: "stepper" }, topMinus, topIn, topPlus)),
    h("label", { class: "slider" }, h("span", { class: "lab" }, "Score at least"), minR, minOut),
    h("label", { class: "tool" }, h("span", { class: "lab" }, "Tag"), tagSel),
    h("label", { class: "tool" }, h("span", { class: "lab" }, "Order"), sortSel),
    h("label", { class: "toggle" }, unrev, "Not decided yet"), apBtn,
    count,
    h("div", { class: "meter" }, meterTrack, meterTxt), undoBtn);

  // ---- queue, stage, paper
  const queueOl = h("ol", {}), queue = h("nav", { class: "queue", "aria-label": "Clips to review" }, queueOl);
  const video = h("video", { playsinline: "", preload: "metadata", "aria-label": "Clip in context" });
  const flag = h("span", { class: "flag" }, ""), srcTag = h("span", { class: "src" }, "");
  const noprev = h("div", { class: "noprev", hidden: "" });
  const screen = h("div", { class: "screen" }, video, flag, srcTag, noprev);
  const canvas = h("canvas", { "aria-hidden": "true" });
  const zLead = h("div", { class: "zone lead" }), zTail = h("div", { class: "zone tail" });
  const tIn = h("div", { class: "tick", "data-l": "in" }), tOut = h("div", { class: "tick", "data-l": "out" });
  const head = h("div", { class: "head" });
  const cutLayer = h("div", {});
  const strip = h("div", { class: "strip", role: "slider", tabindex: "0", "aria-label": "Position in the clip", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": "0" },
    canvas, zLead, zTail, cutLayer, tIn, tOut, head);
  const stripL = h("span", {}), stripR = h("span", {}), stripM = h("span", {});
  const playBtn = h("button", { class: "play", type: "button", "aria-label": "Play", onclick: () => togglePlay() }, "▶");
  const clockEl = h("span", { class: "clock" }, "0:00.0");
  const speedSeg = h("div", { class: "seg small", role: "group", "aria-label": "Speed" }, [1, 1.25, 1.5].map((v) => h("button", { type: "button", "data-v": v, onclick: () => setSpeed(v) }, v + "×")));
  const loopBtn = h("button", { class: "tog", type: "button", onclick: () => setLoop(!F.loop) }, "Loop clip ", h("span", { class: "kbd" }, "l"));
  const modeSeg = h("div", { class: "seg small", role: "group", "aria-label": "Which cut to play", hidden: "" },
    h("button", { type: "button", "data-m": "orig", onclick: () => setTightPlay(false) }, "Original"), h("button", { type: "button", "data-m": "tight", onclick: () => setTightPlay(true) }, "Tightened"));
  const transport = h("div", { class: "transport" }, playBtn, clockEl,
    h("div", { class: "seg small" }, h("button", { type: "button", "aria-label": "Back 5 seconds", onclick: () => jump(-5) }, "−5 s"), h("button", { type: "button", "aria-label": "Forward 5 seconds", onclick: () => jump(5) }, "+5 s")),
    speedSeg, loopBtn, modeSeg);
  const clipHead = h("div", { class: "clip-head" });
  const verdict = h("div", { class: "verdict" });
  const player = h("div", { class: "player" }, clipHead, screen, strip, h("div", { class: "strip-meta" }, stripL, stripM, stripR), transport, verdict);
  const info = h("div", { class: "info" });
  const stageCol = h("div", { class: "stage-col" }, player, h("div", { class: "player" }, info));
  const reasonBox = h("section", { class: "why-box", hidden: "" }), paperBody = h("div", {});
  const paper = h("aside", { class: "paper", "aria-label": "Transcript and context" }, reasonBox, paperBody);
  const keys = h("details", { style: "margin-top:12px" }, h("summary", {}, "Keyboard"),
    h("p", { class: "muted small", style: "margin-top:6px" }, [["k", "keep"], ["m", "maybe"], ["s", "skip"], ["z", "undo"], ["↑ ↓", "previous, next"], ["space", "play or pause"], ["← →", "back or forward 5 s"],
      ["l", "loop"], [", .", "slower, faster"], ["1–8", "toggle a reason"], ["[ ]", "start earlier, end later by a sentence"], ["{ }", "start later, end earlier"], ["c", "cut all or none"], ["enter", "next"]]
      .map(([k, l]) => h("span", { style: "display:inline-block;margin:0 14px 6px 0" }, h("span", { class: "kbd" }, k), " " + l))));
  root.append(tools, h("div", { class: "desk" }, queue, stageCol, paper), keys);

  // ---- data
  function params() {
    const q = new URLSearchParams();
    if (F.session) q.set("session", F.session);
    if (F.top > 0) q.set("top", F.top);
    if (F.min > 0) q.set("min_score", F.min);
    if (F.tag) q.set("tag", F.tag);
    q.set("sort", F.sort);
    if (F.unrev) q.set("unreviewed", "1");
    return q.toString();
  }
  async function loadList(keepSel) {
    const my = ++listSeq;
    const d = await api("clips?" + params());
    if (my !== listSeq) return;
    S.list = d;
    d.clips.forEach((r) => S.rows.set(r.id, r));
    // chrome
    const max = Math.max(10, Math.ceil(d.score_range.max / 5) * 5);
    minR.max = String(max); if (F.min > max) { F.min = max; } minR.value = String(F.min); minOut.textContent = String(F.min);
    fill(sessSeg, ...[["", "All sessions", d.clips_total]].concat(d.sessions.map((s) => [s.id, s.id, s.total])).map(([id, l, n]) =>
      h("button", { type: "button", "aria-pressed": String(F.session === id), onclick: () => { F.session = id; saveF(); S.sel = null; loadList(); } }, l + " (" + n + ")")));
    fill(tagSel, h("option", { value: "" }, "Any tag"), d.tags.map((t) => h("option", { value: t.tag }, t.tag + " (" + t.n + ")")));
    tagSel.value = F.tag;
    fill(count, "Showing ", h("b", {}, d.shown), " of ", h("b", {}, d.of));
    const p = d.progress, tot = Math.max(1, p.total);
    fill(meterTrack, h("i", { class: "k", style: "width:" + 100 * p.keep / tot + "%" }), h("i", { class: "m", style: "width:" + 100 * p.maybe / tot + "%" }), h("i", { class: "s", style: "width:" + 100 * p.skip / tot + "%" }));
    meterTrack.setAttribute("aria-label", p.done + " of " + p.total + " decided: " + p.keep + " kept, " + p.maybe + " maybe, " + p.skip + " skipped");
    meterTxt.textContent = p.done + " of " + p.total + " decided";
    topIn.value = F.top > 0 ? String(F.top) : "";
    paintQueue();
    if (!keepSel) {
      const want = S.sel && d.clips.find((c) => c.id === S.sel) ? S.sel : ((location.hash.split("/")[2] && d.clips.find((c) => c.id === location.hash.split("/")[2])) || d.clips[0] || {}).id;
      if (want) select(want, true); else showNothing(d);
    }
  }
  const visibleRows = () => {
    const rows = S.list ? S.list.clips.slice() : [];
    if (S.sel && !rows.find((r) => r.id === S.sel) && S.rows.get(S.sel)) { const i = Math.min(S.pinAt ?? rows.length, rows.length); rows.splice(i, 0, S.rows.get(S.sel)); }
    return rows;
  };
  function paintQueue() {
    const rows = visibleRows();
    fill(queueOl, ...rows.map((r) => {
      const mk = r.status === "approved" ? ["keep", "Kept"] : r.status === "rejected" ? ["skip", "Skipped"] : r.status === "maybe" ? ["maybe", "Maybe"] : null;
      return h("li", {}, h("button", { class: "q" + (mk ? " done" : ""), type: "button", "aria-current": String(r.id === S.sel), "data-id": r.id, onclick: () => select(r.id) },
        h("span", { class: "score" }, r.score === null ? "" : Math.round(r.score)),
        h("span", { class: "t" }, r.title || r.id),
        mk ? h("span", { class: "mk " + mk[0] }, mk[1]) : h("span", {}),
        h("span", { class: "m2" }, h("span", {}, (r.session || "") + " " + clock(r.start)), h("span", {}, Math.round(r.duration) + " s"),
          r.start_complete === false || r.end_complete === false ? h("span", { style: "color:var(--amber);font-weight:650" }, "check ends") : null,
          h("span", { class: r.has_preview ? "has-pv" : "no-pv", title: r.has_preview ? "Preview rendered" : "No preview yet", role: "img", "aria-label": r.has_preview ? "preview rendered" : "no preview yet" }))));
    }));
    if (!rows.length) queueOl.append(h("li", { class: "empty" }, "No clips match. Lower the score or raise the count."));
    const cur = queueOl.querySelector('[aria-current="true"]');
    if (cur) {      // scroll the queue only, never the page
      const q = queue.getBoundingClientRect(), c = cur.getBoundingClientRect();
      if (c.top < q.top) queue.scrollTop -= q.top - c.top + 8; else if (c.bottom > q.bottom) queue.scrollTop += c.bottom - q.bottom + 8;
      if (c.left < q.left) queue.scrollLeft -= q.left - c.left + 8; else if (c.right > q.right) queue.scrollLeft += c.right - q.right + 8;
    }
  }
  function showNothing(d) {
    S.sel = null; S.detail = null; video.removeAttribute("src"); video.load();
    fill(info, h("div", { class: "empty" }, d.clips_total ? "No clips match these filters." : "No clip candidates yet. Run `ave clips prepare` from the Jobs page."));
    fill(verdict, ); fill(paperBody, ); reasonBox.hidden = true; noprev.hidden = true;
  }

  // ---- selection and detail
  async function select(id, first) {
    await flush();
    if (S.sel !== id) { const rows = visibleRows(); S.pinAt = rows.findIndex((r) => r.id === id) + 1; }
    S.sel = id; S.cuts = new Set(); S.tightPlay = false; S.reasonsFor = null;
    paintQueue();
    history.replaceState(null, "", "#/clips/" + id);
    const my = ++detSeq;
    let d;
    try { d = await api("clips/" + encodeURIComponent(id)); } catch (e) { return toast(e.message, true); }
    if (my !== detSeq) return;
    S.detail = d;
    paintDetail(); loadMedia(true);
  }
  const idx = () => visibleRows().findIndex((r) => r.id === S.sel);
  function step(n) { const rows = visibleRows(); const i = idx() + n; if (i >= 0 && i < rows.length) select(rows[i].id); else if (n > 0) { flush().then(() => loadList(true).then(() => { const r2 = visibleRows(); const j = r2.findIndex((r) => r.id === S.sel); if (r2[j + 1]) select(r2[j + 1].id); else toast("That was the last clip in this selection"); })); } }

  // ---- the player model
  const P = { a: 0, b: 0, lo: 0, hi: 0, mode: "none", src: "" };
  function setWindow() {
    const d = S.detail; if (!d) return;
    const pv = d.media.preview, fb = d.media.fallback, dur = video.duration && isFinite(video.duration) ? video.duration : null;
    if (pv && S.tightPlay && pv.tight_url) { P.mode = "tight"; P.a = 0; P.lo = 0; P.hi = dur || (d.tight_duration || d.duration); P.b = P.hi; }
    else if (pv) { P.mode = "preview"; P.a = pv.clip_in_s; P.b = pv.clip_out_s != null ? pv.clip_out_s : pv.clip_in_s + d.duration; P.lo = 0; P.hi = dur || P.b + pv.pad_after_s; }
    else if (fb && fb.kind === "draft") { P.mode = "draft"; P.a = 0; P.lo = 0; P.hi = dur || d.duration; P.b = P.hi; }
    else if (fb) { P.mode = "proxy"; P.a = fb.t0; P.b = fb.t1; P.lo = Math.max(0, P.a - 8); P.hi = dur ? Math.min(dur, P.b + 8) : P.b + 8; }
    else P.mode = "none";
  }
  function loadMedia(fresh) {
    const d = S.detail, pv = d.media.preview, fb = d.media.fallback;
    const src = pv ? (S.tightPlay && pv.tight_url ? pv.tight_url : pv.url) : fb ? fb.url : "";
    modeSeg.hidden = !(pv && pv.tight_url);
    paintToggles();
    srcTag.textContent = pv ? (S.tightPlay ? "Tightened file" : "Preview") : fb ? (fb.kind === "draft" ? "Draft clip" : "Edit proxy, ranged") : "";
    srcTag.hidden = !src;
    if (!src) {
      P.mode = "none"; video.removeAttribute("src"); video.load(); video.hidden = true; noprev.hidden = false; renderNoPreview(); drawStrip(); return;
    }
    video.hidden = false; noprev.hidden = true;
    if (P.src !== src) {
      P.src = src; P.pendingSeek = "start";
      video.src = src; video.defaultPlaybackRate = F.speed; video.playbackRate = F.speed; video.load();
      setWindow();
    } else { setWindow(); if (fresh) seekToStart(); }
    drawStrip(); peaksDraw();
  }
  function seekToStart() {
    const lead = P.mode === "tight" || P.mode === "draft" ? 0 : 3;
    video.currentTime = Math.max(P.lo, P.a - lead);
    if (F.autoplay) video.play().catch(() => {});
  }
  video.addEventListener("loadedmetadata", () => {
    setWindow(); drawStrip();
    if (P.pendingSeek === "start") seekToStart(); else if (typeof P.pendingSeek === "number") video.currentTime = Math.min(P.pendingSeek, video.duration);
    P.pendingSeek = undefined; video.playbackRate = F.speed; peaksDraw();
  });
  video.addEventListener("play", () => { playBtn.textContent = "❚❚"; playBtn.setAttribute("aria-label", "Pause"); tick(); });
  video.addEventListener("pause", () => { playBtn.textContent = "▶"; playBtn.setAttribute("aria-label", "Play"); cancelAnimationFrame(rafId); paintTime(); });
  video.addEventListener("seeked", paintTime);
  video.addEventListener("error", () => { if (video.getAttribute("src")) { noprev.hidden = false; renderNoPreview("The browser could not play this file."); } });
  function tick() {
    cancelAnimationFrame(rafId);
    let t = video.currentTime;
    if (P.mode !== "none" && P.mode !== "tight" && P.mode !== "draft") {
      const hit = activeCuts().find(([s, e]) => t >= s && t < e - 0.02);
      if (hit && !video.paused) { video.currentTime = hit[1]; t = hit[1]; }
    }
    if (F.loop && P.b > P.a && t >= P.b - 0.03 && !video.paused) { video.currentTime = P.a; t = P.a; }
    paintTime();
    if (!video.paused) rafId = requestAnimationFrame(tick);
  }
  const activeCuts = () => (S.detail && P.mode !== "tight" ? S.detail.tighten.map((t, i) => (S.cuts.has(i) && t.rel ? [P.a + t.rel[0], P.a + t.rel[1]] : null)).filter(Boolean) : []);
  function paintTime() {
    const t = video.currentTime || 0, span = Math.max(0.1, P.hi - P.lo);
    head.style.left = (100 * (t - P.lo) / span) + "%";
    strip.setAttribute("aria-valuenow", String(Math.round(100 * (t - P.lo) / span)));
    strip.setAttribute("aria-valuetext", relClock(t - P.a) + " into the clip");
    clockEl.textContent = relClock(t - P.a) + " / " + clock(P.b - P.a);
    const out = P.mode === "preview" || P.mode === "proxy" ? (t < P.a - 0.05 ? "lead" : t > P.b + 0.05 ? "tail" : "") : "";
    screen.classList.toggle("outside", !!out);
    flag.textContent = out === "lead" ? "Lead-in, before the clip" : out === "tail" ? "After the clip" : "";
    if (S.detail) paintNow(t - P.a + S.detail.start);
  }
  function drawStrip() {
    const d = S.detail; if (!d) return;
    const span = Math.max(0.1, P.hi - P.lo), pc = (t) => (100 * (t - P.lo) / span) + "%";
    const dim = P.mode === "preview" || P.mode === "proxy";
    zLead.style.width = dim ? pc(P.a) : "0"; zTail.style.width = dim ? (100 - 100 * (P.b - P.lo) / span) + "%" : "0";
    tIn.style.left = pc(P.a); tOut.style.left = pc(P.b); tIn.hidden = tOut.hidden = !dim;
    fill(cutLayer, ...(P.mode === "tight" ? [] : d.tighten.map((t, i) => t.rel ? h("div", { class: "cut" + (S.cuts.has(i) ? " on" : ""), style: "left:" + pc(P.a + t.rel[0]) + ";width:" + (100 * (t.rel[1] - t.rel[0]) / span) + "%" }) : null)));
    stripL.textContent = dim ? "lead-in " + secs(Math.max(0, P.a - P.lo)) : "";
    stripM.textContent = "clip " + secs(P.b - P.a) + (activeCuts().length ? ", cuts skip " + secs(activeCuts().reduce((s, [x, y]) => s + y - x, 0)) : "");
    stripR.textContent = dim ? "tail " + secs(Math.max(0, P.hi - P.b)) : "";
    paintTime();
  }
  async function peaksDraw() {
    const ctx = canvas.getContext("2d"), r = canvas.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
    canvas.width = Math.max(10, r.width * dpr); canvas.height = Math.max(10, r.height * dpr);
    const css = getComputedStyle(document.documentElement);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const mid = canvas.height / 2;
    ctx.fillStyle = css.getPropertyValue("--line").trim(); ctx.fillRect(0, mid - dpr / 2, canvas.width, dpr);
    if (P.mode !== "preview" && P.mode !== "tight" && P.mode !== "draft") return;
    const url = P.src, pk = await peaksFor(url, Math.floor(canvas.width / (3 * dpr)));
    if (!pk || url !== P.src) return;
    ctx.fillStyle = css.getPropertyValue("--ink-2").trim();
    const n = pk.p.length, w = canvas.width / n;
    for (let i = 0; i < n; i++) { const hh = Math.max(dpr, pk.p[i] * (canvas.height - 12 * dpr) / 2); ctx.fillRect(i * w + w * 0.15, mid - hh, w * 0.7, hh * 2); }
  }
  new ResizeObserver(() => { drawStrip(); peaksDraw(); }).observe(strip);
  const themeBtn = document.getElementById("theme"), dm = matchMedia("(prefers-color-scheme: dark)");
  themeBtn.addEventListener("themechange", peaksDraw); dm.addEventListener("change", peaksDraw);
  onLeave.push(() => { themeBtn.removeEventListener("themechange", peaksDraw); dm.removeEventListener("change", peaksDraw); });
  function seekFromPointer(e) {
    const r = strip.getBoundingClientRect(), f = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
    if (P.mode !== "none") { video.currentTime = P.lo + f * (P.hi - P.lo); paintTime(); }
  }
  strip.addEventListener("pointerdown", (e) => { strip.setPointerCapture(e.pointerId); seekFromPointer(e); strip.onpointermove = seekFromPointer; });
  strip.addEventListener("pointerup", () => { strip.onpointermove = null; });
  const togglePlay = () => { if (P.mode === "none") return; video.paused ? video.play().catch(() => {}) : video.pause(); };
  const jump = (n) => { if (P.mode !== "none") { video.currentTime = Math.max(P.lo, Math.min(P.hi, video.currentTime + n)); paintTime(); } };
  function setSpeed(v) { F.speed = v; saveF(); video.playbackRate = v; video.defaultPlaybackRate = v; paintToggles(); }
  function setLoop(v) { F.loop = v; saveF(); paintToggles(); }
  function setTightPlay(v) { S.tightPlay = v; setWindow(); loadMedia(false); }
  function paintToggles() {
    speedSeg.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(+b.dataset.v === F.speed)));
    loopBtn.setAttribute("aria-pressed", String(F.loop)); apBtn.setAttribute("aria-pressed", String(F.autoplay));
    modeSeg.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String((b.dataset.m === "tight") === S.tightPlay)));
  }
  function renderNoPreview(msg) {
    const d = S.detail, sess = d && d.session;
    const watching = S.jobWatch;
    fill(noprev, h("h3", {}, msg || "Preview not rendered yet"),
      h("p", {}, msg ? "Try rendering it again." : "A preview plays the clip with a few seconds either side, so you can hear what leads in and what follows. The transcript is still here."),
      h("div", { class: "row" },
        h("button", { type: "button", disabled: !!watching, onclick: () => queuePreviews({ name: sess, ids: d.id, force: !!msg }) }, "Render this clip"),
        h("button", { type: "button", disabled: !!watching, onclick: () => queuePreviews({ name: sess, top: 20 }) }, "Render the best 20 of " + (sess || "this session"))),
      watching ? h("p", { class: "small" }, "Rendering in the background. ", h("a", { href: "#/jobs/" + watching, style: "color:#bfe3d8" }, "Watch the log"), ".") : null);
  }
  async function queuePreviews(args) {
    const a = {}; for (const [k, v] of Object.entries(args)) if (v !== undefined && v !== null && v !== false) a[k] = v;
    const j = await runJob("clips previews", a); if (!j) return;
    S.jobWatch = j.id; if (S.detail) renderNoPreview(); watchJob(j.id);
  }
  function watchJob(id) {
    clearTimeout(S.jt);
    const poll = async () => {
      let j; try { j = await api("jobs/" + id); } catch (e) { S.jobWatch = null; return; }
      if (j.status === "running") { S.jt = setTimeout(poll, 2000); return; }
      S.jobWatch = null;
      toast(j.status === "done" ? "Previews rendered" : "The preview job failed. Open Jobs for the log.", j.status !== "done");
      if (S.sel) { loadList(true); const my = S.sel; const d = await api("clips/" + encodeURIComponent(my)); if (my === S.sel) { S.detail = d; paintDetail(); loadMedia(true); } }
    };
    S.jt = setTimeout(poll, 1500);
  }
  onLeave.push(() => { clearTimeout(S.jt); cancelAnimationFrame(rafId); video.pause(); });

  // ---- detail panels
  function paintDetail() {
    const d = S.detail;
    const warn = [];
    if (d.start_complete === false) warn.push("Starts mid-sentence");
    if (d.end_complete === false) warn.push("Ends unfinished");
    const kind = (k) => ({ filler: "filler", tangent: "tangent", repeat: "repeat", restart: "restart", aside: "aside" }[k] || k || "cut");
    fill(clipHead, 
      h("h2", {}, d.title || d.id),
      h("div", { class: "badges" },
        warn.map((w) => h("span", { class: "badge-w" }, h("span", { "aria-hidden": "true" }, "\u26A0"), w)),
        !warn.length && d.start_complete !== null && d.end_complete !== null ? h("span", { class: "badge-ok" }, h("span", { "aria-hidden": "true" }, "\u2713"), "Begins and ends on a full thought") : null,
        d.warnings.map((w) => h("span", { class: "badge-w" }, h("span", { "aria-hidden": "true" }, "\u26A0"), w)),
        d.stale ? h("span", { class: "badge-w" }, "Stale: the edit changed since this was picked") : null,
        d.media.preview && d.media.preview.stale ? h("span", { class: "badge-n" }, "Range changed since the preview was rendered") : null));
    fill(info, 
      d.completeness_note ? h("div", { class: "muted small" }, d.completeness_note) : null,
      d.hook ? h("div", { class: "hookline" }, "\u201C" + d.hook + "\u201D") : null,
      d.rationale ? h("div", { class: "why" }, d.rationale) : null,
      h("div", { class: "row", style: "margin:0" }, h("span", { class: "chip" }, "Rank " + d.rank + " in " + (d.session || "episode")), h("span", { class: "chip" }, "Score " + (d.score === null ? "n/a" : d.score)),
        h("span", { class: "chip" }, d.session + " " + clock(d.start) + " to " + clock(d.end)), h("span", { class: "chip" }, secs(d.duration)),
        d.tags.map((t) => h("span", { class: "chip" }, t)), d.speakers.map((s) => h("span", { class: "chip" }, (S.names && S.names[s]) || s))),
      Object.keys(d.scores).length ? h("details", {}, h("summary", {}, "How it scored"),
        h("div", { class: "scores" }, Object.entries(d.scores).map(([k, v]) => h("div", {}, h("span", {}, k.replace(/_/g, " ")), h("b", { class: "num" }, v), h("span", { class: "b" }, h("i", { style: "width:" + Math.min(100, v * 10) + "%" })))))) : null,
      h("div", { class: "nudge" },
        h("span", { class: "grp" }, "Start", h("button", { type: "button", onclick: () => nudge({ start: -1 }) }, "\u2039 a sentence earlier"), h("button", { type: "button", onclick: () => nudge({ start: 1 }) }, "later \u203A")),
        h("span", { class: "grp" }, "End", h("button", { type: "button", onclick: () => nudge({ end: -1 }) }, "\u2039 shorter"), h("button", { type: "button", onclick: () => nudge({ end: 1 }) }, "a sentence longer \u203A"))));
    paintVerdict();
    // paper
    const tx = d.transcript, rel = (s) => s.start - d.start;
    const li = (s, cls) => h("li", { class: cls + (s.cutIdx ? " cut" : ""), "data-id": s.id, "data-s": s.start, "data-e": s.end, onclick: () => { if (P.mode !== "none") { video.currentTime = Math.max(P.lo, P.a + rel(s)); paintTime(); } } },
      h("span", { class: "who" }, h("b", {}, s.who || s.speaker || ""), relClock(rel(s))), h("span", { class: "txt" }, h("span", { class: "hl" }, s.text)));
    const txList = h("ol", { class: "tx", "aria-label": "Transcript", tabindex: "0" },
      tx.before.map((s) => li(s, "before")), h("li", { class: "edge" }, "Clip starts here"),
      tx.inside.map((s) => li(s, "in")), h("li", { class: "edge" }, "Clip ends here"), tx.after.map((s) => li(s, "after")));
    const cutBox = h("div", {});
    fill(paperBody,
      d.tighten.length ? h("section", {}, h("h3", {}, "Tighter cut", h("span", {}, "suggestions only, nothing is applied")), cutBox) : null,
      h("section", { class: "grow" }, h("h3", {}, "Transcript", h("span", {}, tx.available ? "click a line to play from it" : "")),
        tx.available ? txList : h("div", { class: "ctx" }, h("p", { class: "muted" }, "No sentence table yet (run `ave clips prepare`). Showing the context text instead."),
          d.context_before ? h("p", {}, h("b", {}, "Just before"), d.context_before) : null, h("p", {}, h("b", {}, "The clip"), d.text), d.context_after ? h("p", {}, h("b", {}, "Just after"), d.context_after) : null)));
    S.txList = txList; S.cutBox = cutBox; paintCuts();
    const edge = txList.querySelector("li.edge"); lastNow = null;
    if (edge) txList.scrollTop = Math.max(0, edge.offsetTop - txList.offsetTop - 70);
  }
  function paintCuts() {
    const d = S.detail, box = S.cutBox; if (!box) return;
    const on = [...S.cuts].filter((i) => d.tighten[i]), saved = on.reduce((s, i) => s + d.tighten[i].saves_s, 0);
    fill(box, d.tighten.length ? h("div", {}, h("div", { class: "chips-cut" }, d.tighten.map((t, i) => h("button", { type: "button", class: "cutchip", "aria-pressed": String(S.cuts.has(i)), title: t.reason || "", onclick: () => toggleCut(i) },
      h("span", { class: "k" }, t.kind || "cut"), h("span", { class: "q" }, t.text ? "“" + (t.text.length > 46 ? t.text.slice(0, 44) + "…" : t.text) + "”" : "(no text)"), h("span", { class: "sv" }, "−" + t.saves_s.toFixed(1) + " s")))),
      h("div", { class: "saved" }, on.length ? "With " + plural(on.length, "cut") + ": saves " + saved.toFixed(1) + " s, " + secs(d.duration) + " becomes " + secs(d.duration - saved) + ". Playback skips them."
        : "Turn a suggestion on to strike it from the transcript and have playback skip it. Together they would save " + d.tighten.reduce((s, t) => s + t.saves_s, 0).toFixed(1) + " s."),
      h("div", { class: "row" }, h("button", { type: "button", onclick: () => toggleAllCuts() }, S.cuts.size === d.tighten.length ? "Put all back" : "Cut all")))
      : h("p", { class: "muted" }, "Nothing to tighten in this clip."));
    if (S.txList) S.txList.querySelectorAll("li[data-id]").forEach((li) => {
      const id = li.dataset.id; li.classList.toggle("cut", on.some((i) => d.tighten[i].sentence_ids.includes(id)));
    });
    drawStrip();
  }
  function toggleCut(i) { S.cuts.has(i) ? S.cuts.delete(i) : S.cuts.add(i); paintCuts(); }
  function toggleAllCuts() { const d = S.detail; if (!d.tighten.length) return; if (S.cuts.size === d.tighten.length) S.cuts.clear(); else d.tighten.forEach((t, i) => S.cuts.add(i)); paintCuts(); }
  let lastNow = null;
  function paintNow(editT) {
    if (!S.txList) return;
    let hit = null; for (const li of S.txList.querySelectorAll("li[data-id]")) { if (+li.dataset.s <= editT + 0.01) hit = li; else break; }
    if (hit === lastNow) return;
    if (lastNow) lastNow.classList.remove("now");
    lastNow = hit; if (hit) { hit.classList.add("now"); if (!video.paused) { const box = S.txList; const top = hit.offsetTop - box.offsetTop; if (top < box.scrollTop + 20 || top > box.scrollTop + box.clientHeight - 60) box.scrollTo({ top: Math.max(0, top - 40), behavior: reduceMotion() ? "auto" : "smooth" }); } }
  }

  // ---- verdicts
  const rowOf = () => S.rows.get(S.sel);
  function paintVerdict() {
    const r = rowOf(), d = S.detail; if (!d) return;
    const v = r && r.status === "approved" ? "keep" : r && r.status === "rejected" ? "skip" : r && r.status === "maybe" ? "maybe" : null;
    const rs = S.pend && S.pend.id === S.sel ? S.pend.reasons : (r && r.verdict && r.verdict.reasons) || [];
    const labels = S.list.reason_labels || {};
    const vocab = S.list.reasons.filter((id) => { const l = labels[id]; return !v || v === "keep" || !l || l.verdicts.includes(v); });
    const chooser = v && v !== "keep";
    fill(verdict,
      h("div", { class: "btns" },
        [["keep", "Keep", "k"], ["maybe", "Maybe", "m"], ["skip", "Skip", "s"]].map(([id, l, k]) => h("button", { type: "button", class: "vbtn " + id, "aria-pressed": String(v === id), onclick: () => decide(id) }, l, h("small", {}, "press " + k)))),
      v === "keep" ? h("div", { class: "small ok" }, "Kept, and written to the ledger.") : v ? h("div", { class: "small muted" }, "Recorded. Say why on the right if you like, then press Enter for the next clip.") : h("div", { class: "small muted" }, "Not decided yet."));
    reasonBox.hidden = !chooser;
    fill(reasonBox, chooser ? [
      h("h3", {}, v === "skip" ? "Why skip it?" : "What would change your mind?", h("span", {}, "optional, teaches the finder")),
      h("div", { class: "reasons" }, vocab.map((id) => h("button", { type: "button", "aria-pressed": String(rs.includes(id)), onclick: () => toggleReason(id) }, h("span", { class: "kbd" }, S.list.reasons.indexOf(id) + 1), (labels[id] || {}).label || id.replace(/_/g, " ")))),
      h("div", { class: "row" }, h("input", { class: "wide", id: "note", placeholder: "A note, if it helps", value: (S.pend && S.pend.id === S.sel ? S.pend.note : (r.verdict && r.verdict.note)) || "", "aria-label": "Note",
        oninput: (e) => { queueNote(e.target.value); }, onkeydown: (e) => { if (e.key === "Enter") { flush().then(() => step(1)); } } }), h("button", { type: "button", class: "primary", onclick: () => flush().then(() => step(1)) }, "Next clip ", h("span", { class: "kbd" }, "\u21B5")))] : null);
  }
  const NEWSTATUS = { keep: "approved", skip: "rejected", maybe: "maybe", pending: "proposed" };
  async function post(id, v, reasons, note) {
    const r = await api("clips/" + encodeURIComponent(id) + "/decide", { verdict: v, reasons: reasons || [], note: note || undefined });
    const row = S.rows.get(id); if (row) { row.status = NEWSTATUS[v]; row.verdict = v === "pending" ? null : { verdict: v, reasons: reasons || [], note: note || null }; }
    return r;
  }
  async function decide(v) {
    const id = S.sel; if (!id) return; await flush();
    const row = S.rows.get(id), prev = { status: row.status, verdict: row.verdict ? { ...row.verdict } : null };
    if (prev.status === NEWSTATUS[v] || (v === "maybe" && prev.status === "maybe")) { if (v === "keep") return step(1); return; }
    try { await post(id, v, [], ""); } catch (e) { return toast(e.message, true); }
    S.undo.push({ id, prev }); undoBtn.disabled = false;
    toast({ keep: "Kept", maybe: "Marked maybe", skip: "Skipped" }[v] + ": " + (row.title || id));
    paintQueue(); paintVerdict(); loadList(true);
    if (v === "keep") step(1);
  }
  function queueNote(text) { const r = rowOf(); S.pend = { id: S.sel, verdict: r.status === "approved" ? "keep" : r.status === "rejected" ? "skip" : "maybe", reasons: (S.pend && S.pend.id === S.sel ? S.pend.reasons : (r.verdict && r.verdict.reasons) || []), note: text }; clearTimeout(S.pt); S.pt = setTimeout(flush, 900); }
  function toggleReason(id) {
    const r = rowOf(); if (!r || r.status === "proposed") return toast("Press s or m first, then say why");
    const cur = S.pend && S.pend.id === S.sel ? S.pend : { id: S.sel, verdict: r.status === "approved" ? "keep" : r.status === "rejected" ? "skip" : "maybe", reasons: (r.verdict && r.verdict.reasons) || [], note: (r.verdict && r.verdict.note) || "" };
    cur.reasons = cur.reasons.includes(id) ? cur.reasons.filter((x) => x !== id) : [...cur.reasons, id];
    S.pend = cur; paintVerdict(); clearTimeout(S.pt); S.pt = setTimeout(flush, 700);
  }
  async function flush() {
    clearTimeout(S.pt); const p = S.pend; if (!p) return; S.pend = null;
    try { await post(p.id, p.verdict, p.reasons, p.note); } catch (e) { toast(e.message, true); }
    if (S.sel === p.id) paintVerdict();
  }
  async function undo() {
    await flush(); const u = S.undo.pop(); undoBtn.disabled = !S.undo.length; if (!u) return toast("Nothing to undo");
    const v = { approved: "keep", rejected: "skip", maybe: "maybe", proposed: "pending" }[u.prev.status];
    try { await post(u.id, v, (u.prev.verdict && u.prev.verdict.reasons) || [], (u.prev.verdict && u.prev.verdict.note) || ""); } catch (e) { return toast(e.message, true); }
    toast("Undone"); await loadList(true); if (S.sel !== u.id) { S.pinAt = undefined; select(u.id); } else { paintQueue(); paintVerdict(); }
  }
  async function nudge(n) {
    await flush(); const d = S.detail;
    try {
      const r = await api("clips/" + encodeURIComponent(d.id) + "/decide", { verdict: "nudge", nudge: n });
      toast("Moved to " + clock(r.start) + " – " + clock(r.end) + " (" + secs(r.duration) + "), score " + r.score);
      const id = d.id; const row = S.rows.get(id); if (row) row.status = "proposed";
      const my = ++detSeq; S.detail = await api("clips/" + encodeURIComponent(id)); if (my === detSeq) { paintDetail(); loadMedia(false); } loadList(true);
    } catch (e) { toast(e.message, true); }
  }

  // ---- filter wiring
  const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
  const reload = debounce(() => { saveF(); S.sel = S.sel; loadList(); }, 140);
  const setTop = (v) => { F.top = Math.max(0, v | 0); topIn.value = F.top > 0 ? String(F.top) : ""; reload(); };
  const nowTop = () => F.top > 0 ? F.top : (S.list ? S.list.of : 0);
  topMinus.onclick = () => setTop(Math.max(1, nowTop() - (nowTop() > 30 ? 10 : 5)));
  topPlus.onclick = () => setTop(nowTop() + (nowTop() >= 30 ? 10 : 5) >= (S.list ? S.list.of : 0) && F.top > 0 && F.top + 5 >= S.list.of ? 0 : nowTop() + (nowTop() >= 30 ? 10 : 5));
  topIn.addEventListener("input", () => { F.top = Math.max(0, parseInt(topIn.value || "0", 10) || 0); reload(); });
  minR.addEventListener("input", () => { F.min = +minR.value; minOut.textContent = minR.value; reload(); });
  tagSel.addEventListener("change", () => { F.tag = tagSel.value; reload(); });
  sortSel.addEventListener("change", () => { F.sort = sortSel.value; reload(); });
  unrev.addEventListener("change", () => { F.unrev = unrev.checked; reload(); });

  keyHandler = (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const tag = document.activeElement.tagName;
    if (["TEXTAREA", "INPUT", "SELECT"].includes(tag)) { if (e.key === "Escape") document.activeElement.blur(); return; }
    const k = e.key;
    if (k === "k") decide("keep"); else if (k === "m") decide("maybe"); else if (k === "s") decide("skip");
    else if (k === "z" || k === "u") undo();
    else if (k === " ") togglePlay();
    else if (k === "ArrowDown" || k === "j") step(1); else if (k === "ArrowUp") step(-1);
    else if (k === "Enter" && tag !== "BUTTON" && tag !== "A") { flush().then(() => step(1)); }
    else if (k === "ArrowRight") jump(5); else if (k === "ArrowLeft") jump(-5);
    else if (k === "l") setLoop(!F.loop);
    else if (k === ",") setSpeed([1, 1.25, 1.5][Math.max(0, [1, 1.25, 1.5].indexOf(F.speed) - 1)]);
    else if (k === ".") setSpeed([1, 1.25, 1.5][Math.min(2, [1, 1.25, 1.5].indexOf(F.speed) + 1)]);
    else if (k === "c") toggleAllCuts();
    else if (k === "0" || k === "Home") { if (P.mode !== "none") seekToStart(); }
    else if (k === "[") nudge({ start: -1 }); else if (k === "{") nudge({ start: 1 });
    else if (k === "]") nudge({ end: 1 }); else if (k === "}") nudge({ end: -1 });
    else if (/^[1-8]$/.test(k) && S.list && S.list.reasons[+k - 1]) toggleReason(S.list.reasons[+k - 1]);
    else return;
    e.preventDefault();
  };
  try { const dd = await api("dashboard"); S.names = dd.speakers; } catch (e) { S.names = {}; }
  paintToggles();
  await loadList();
  return root;
}

// ---------------------------------------------------------------- trim
async function trimPage() {
  const d = await api("trim");
  const keys = d.sessions.length ? d.sessions : [d.main];
  const root = h("div", {}, h("div", { class: "page-head" }, h("h1", {}, "Trim"), h("p", {}, "The real start and end of each session. Suggestions are proposals; nothing changes until you set a trim.")));
  d.pending_questions.forEach((q) => root.append(h("div", { class: "warn small", style: "margin-bottom:10px" }, q)));
  const cards = h("div", { class: "cards" }); root.append(cards);
  for (const key of keys) {
    const sug = d.suggestions[key]; const rec = d.trim[key];
    const inI = h("input", { type: "number", step: "0.1", "aria-label": key + " in, seconds", value: rec && rec.in !== undefined ? rec.in : (sug && sug.head.proposed_in !== null ? sug.head.proposed_in : "") });
    const outI = h("input", { type: "number", step: "0.1", "aria-label": key + " out, seconds", value: rec && rec.out !== undefined ? rec.out : (sug && sug.tail.proposed_out !== null ? sug.tail.proposed_out : "") });
    const num = (el) => el.value === "" ? null : Number(el.value);
    const ev = (side) => side ? (side.evidence || []).map((x) => h("li", {}, typeof x === "string" ? x : JSON.stringify(x))) : [];
    cards.append(h("section", { class: "panel" }, h("h2", {}, key),
      rec ? h("p", { class: "ok small" }, "Recorded: in " + (rec.in ?? "none") + " s, out " + (rec.out ?? "none") + " s, by " + rec.by + " at " + rec.at) : h("p", { class: "muted small" }, "No trim recorded."),
      sug ? h("div", { class: "sug" }, h("div", {}, "Suggested start " + (sug.head.proposed_in ?? "none") + " s: " + (sug.head.reason || "")), h("div", {}, "Suggested end " + (sug.tail.proposed_out ?? "none") + " s: " + (sug.tail.reason || "")),
        h("ul", {}, ev(sug.head), ev(sug.tail))) : h("p", { class: "muted small" }, "No suggestion yet. Run “Suggest where each session really starts and ends” from the dashboard."),
      h("div", { class: "row" }, h("label", {}, "Start (s) ", inI), h("label", {}, "End (s) ", outI),
        h("button", { class: "primary", onclick: async () => { try { await api("trim/set", { session: key, in: num(inI), out: num(outI) }); toast("Trim set for " + key); render(); } catch (e) { toast(e.message, true); } } }, "Set this trim"),
        rec ? h("button", { class: "danger", onclick: async () => { try { await api("trim/clear", { session: key }); toast("Cleared " + key); render(); } catch (e) { toast(e.message, true); } } }, "Clear") : null)));
  }
  return root;
}

// ---------------------------------------------------------------- content
function ratio(w, hgt) { const g = (a, b) => b ? g(b, a % b) : a; const k = g(w, hgt); const r = [w / k, hgt / k]; return r[0] > 30 ? (w / hgt).toFixed(2) + ":1" : r.join(":"); }
async function contentPage() {
  const d = await api("content");
  const root = h("div", {}, h("div", { class: "page-head" }, h("h1", {}, "Content"), h("p", {}, "Draft images and posts, shown at their real shape.")));
  if (!d.enabled) root.append(h("div", { class: "warn", style: "margin-bottom:12px" }, "`ave content` is switched off on this install, so decisions are disabled. You can still look."));
  if (!d.docs.length) root.append(h("div", { class: "panel empty" }, "No content drafts yet. They appear after `ave content propose` and `render`."));
  for (const doc of d.docs) {
    root.append(h("h2", { style: "margin:18px 0 10px" }, "Draft set " + doc.id + ", version " + doc.version));
    const cards = h("div", { class: "wall" }); root.append(cards);
    for (const it of doc.items) {
      const crit = it.critic;
      const note = h("input", { class: "wide", placeholder: "Note (an override needs one)", "aria-label": "Note for " + it.id });
      const act = async (decision, override) => {
        try { await api("content/decide", { doc: doc.id, id: it.id, decision, note: note.value, override: override || undefined }); toast(it.id + ": " + decision); render(); }
        catch (e) { toast(e.message, true); }
      };
      cards.append(h("section", { class: "panel itemcard" },
        h("div", { class: "item-head" }, h("h3", {}, it.id), h("span", { class: "chip" }, it.template), it.style ? h("span", { class: "chip" }, it.style) : null,
          h("span", { class: "pill " + stClass(it.status) }, it.status), it.by ? h("span", { class: "muted small" }, "by " + it.by) : null),
        h("div", { class: "gallery" }, it.outputs.map((o) => {
          if (!o.url) return h("span", { class: "muted small" }, o.file + " (not servable)");
          const cap = h("figcaption", {}, (o.alt_text || "No alt text yet"));
          const img = h("img", { src: o.url, alt: o.alt_text || it.id, onload: (e) => { cap.prepend(ratio(e.target.naturalWidth, e.target.naturalHeight) + ", " + e.target.naturalWidth + "×" + e.target.naturalHeight + ". "); } });
          return h("figure", { class: "shot" }, img, cap);
        })),
        crit ? h("div", { class: crit.ok ? "ok small" : "warn small" }, (crit.ok ? "Critic passed. " : "Critic flagged: ") + (crit.notes || []).join("; ")) : h("div", { class: "muted small" }, "Not rendered or checked yet."),
        h("div", { class: "row" }, note),
        h("div", { class: "row" }, h("button", { class: "primary", disabled: !d.enabled, onclick: () => act("approve") }, "Approve"),
          crit && !crit.ok ? h("button", { disabled: !d.enabled, onclick: () => act("approve", true) }, "Approve despite the critic") : null,
          h("button", { class: "danger", disabled: !d.enabled, onclick: () => act("reject") }, "Reject"))));
    }
  }
  return root;
}

// ---------------------------------------------------------------- publish
async function publishPage() {
  const d = await api("publish");
  const root = h("div", {}, h("div", { class: "page-head" }, h("h1", {}, "Publish")),
    h("p", { class: "safe", style: "margin-bottom:14px" }, "Approving records who approved which exact bytes. This page never sends or posts anything: sending stays a separate step in the terminal (ave publish run)."));
  if (!d.items.length) root.append(h("div", { class: "panel empty" }, "The publish queue is empty."));
  const box = h("div", {});
  const list = h("div", { class: "list" });
  root.append(h("div", { class: "split" }, list, box));
  const draw = (cur) => fill(list, ...d.items.map((i) => h("button", { class: "item", type: "button", "aria-current": String(i.id === cur), onclick: () => show(i.id) },
    h("div", { class: "t" }, h("span", {}, i.title || i.id), h("span", { class: "pill " + stClass(i.status) }, i.status)), h("div", { class: "small muted" }, i.id + ", " + (i.platforms || []).join(", ")))));
  draw(null);
  const hashView = (sha) => sha ? h("div", { class: "hash" }, h("b", {}, sha.slice(0, 8)), h("span", {}, sha.slice(8))) : h("div", { class: "hash" }, "n/a");
  async function show(id) {
    let it;
    try { it = await api("publish/" + encodeURIComponent(id)); } catch (e) { return toast(e.message, true); }
    draw(id);
    const sha = h("input", { class: "wide mono", placeholder: "Type the first 8 or more characters of the highlighted hash", autocomplete: "off", spellcheck: "false", "aria-label": "File hash prefix" });
    const by = h("input", { placeholder: "Approver name", value: d.by, autocomplete: "off", "aria-label": "Approver name" });
    const reason = h("input", { class: "wide", placeholder: "Reason (for a rejection)", "aria-label": "Rejection reason" });
    const can = ["awaiting_approval", "draft"].includes(it.status);
    fill(box, h("section", { class: "panel stack" },
      h("div", { class: "item-head" }, h("h2", {}, it.title || it.id), h("span", { class: "pill " + stClass(it.status) }, it.status)), h("div", { class: "small muted" }, it.id),
      h("h3", {}, "The exact file"),
      it.files.map((f) => h("div", { class: "stack" }, h("div", { class: "mono small" }, f.file + (f.exists ? "" : "  (MISSING)")), hashView(f.sha256),
        f.url && /\.(mp4|mov|webm|m4v)$/i.test(f.file) ? h("video", { controls: true, preload: "metadata", src: f.url, style: "max-width:100%;max-height:50vh;border-radius:9px;background:#000" })
          : (f.url ? h("img", { style: "max-height:46vh;max-width:100%;border-radius:9px;border:1px solid var(--line)", src: f.url, alt: "" }) : null))),
      h("h3", {}, "Details bound to the approval"), h("pre", {}, JSON.stringify(it.metadata, null, 2)),
      it.preflight_problems.length ? h("div", { class: "warn small" }, "Preflight: " + it.preflight_problems.join("; ")) : null,
      it.approvals.length ? h("div", { class: "small" }, "Last approval: " + it.approvals.at(-1).by + " at " + it.approvals.at(-1).at + " via " + (it.approvals.at(-1).source || "cli")) : null,
      it.send_blockers.length ? h("div", { class: "warn small" }, "Cannot send now: " + it.send_blockers.join("; ")) : null,
      can ? h("div", { class: "stack" }, h("h3", {}, "Approve"), h("div", { class: "row" }, sha), h("div", { class: "row" }, by,
        h("button", { class: "primary", onclick: async () => { try { const r = await api("publish/" + encodeURIComponent(id) + "/approve", { confirm_sha: sha.value.trim(), by: by.value.trim() }); toast(r.note); render(); } catch (e) { toast(e.message, true); } } }, "Approve these exact bytes"))) : null,
      !["published", "publishing"].includes(it.status) ? h("div", { class: "stack" }, h("h3", {}, "Reject"), h("div", { class: "row" }, reason, h("button", { class: "danger", onclick: async () => {
        try { await api("publish/" + encodeURIComponent(id) + "/reject", { by: by.value.trim() || d.by, reason: reason.value }); toast("Rejected"); render(); } catch (e) { toast(e.message, true); } } }, "Reject"))) : null,
      h("h3", {}, "History"), h("pre", {}, it.events.map((e) => e.at + "  " + e.event + "  " + (e.by || "") + " " + (e.source || "")).join("\n") || "(none)")));
  }
  return root;
}

// ---------------------------------------------------------------- jobs
async function jobsPage(arg) {
  const d = await api("jobs");
  const dash = await api("dashboard").catch(() => ({ actions: [] }));
  const cmd = h("select", { "aria-label": "Command" }, Object.keys(d.allowed).map((c) => h("option", { value: c }, "ave " + c)));
  const argBox = h("div", { class: "row" });
  const log = h("pre", { class: "log", "aria-live": "off", tabindex: "0", "aria-label": "Job log" }, "Pick a job, or start one.");
  const head = h("div", { class: "jobhead" });
  const listBox = h("div", {});
  let watching = null;
  const fields = {};
  function drawArgs() {
    fill(argBox, ); for (const k of Object.keys(fields)) delete fields[k];
    for (const [name, kind] of Object.entries(d.allowed[cmd.value])) {
      const el = kind === "flag" ? h("input", { type: "checkbox" }) : Array.isArray(kind) ? h("select", {}, h("option", { value: "" }, "(default)"), kind.map((o) => h("option", { value: o }, o))) : h("input", { size: 8, placeholder: kind });
      fields[name] = { el, kind };
      argBox.append(h("label", { class: "small" }, name.replace(/_/g, " ") + " ", el));
    }
  }
  cmd.addEventListener("change", drawArgs); drawArgs();
  const start = async (c, args) => {
    const j = await runJob(c, args); if (j) { watch(j.id); refreshList(); }
  };
  const startCustom = () => {
    const args = {};
    for (const [name, { el, kind }] of Object.entries(fields)) {
      if (kind === "flag") { if (el.checked) args[name] = true; }
      else if (el.value !== "") args[name] = (kind === "int" || kind === "float") ? Number(el.value) : el.value;
    }
    start(cmd.value, args);
  };
  async function watch(id) {
    watching = id; history.replaceState(null, "", "#/jobs/" + id);
    const j = await api("jobs/" + id);
    fill(head, h("strong", {}, "ave " + j.cmd), h("span", { class: "pill " + stClass(j.status) }, j.status === "running" ? "running" : j.status === "done" ? "finished" : "failed (exit " + j.returncode + ")"), h("span", { class: "muted small" }, "started " + j.started));
    const stick = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
    log.textContent = j.tail || "(no output yet)";
    if (stick) log.scrollTop = log.scrollHeight;
    if (j.status === "running") { clearTimeout(timer); timer = setTimeout(() => { if (watching === id) watch(id); }, 1000); } else refreshList();
  }
  async function refreshList() {
    const j = await api("jobs");
    fill(listBox, j.jobs.length ? h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Started"), h("th", {}, "Command"), h("th", {}, "State"))),
      h("tbody", {}, j.jobs.map((x) => h("tr", { class: x.id === watching ? "sel" : "" }, h("td", {}, h("button", { class: "ghost", onclick: () => watch(x.id) }, x.started)), h("td", {}, "ave " + x.cmd), h("td", {}, h("span", { class: "pill " + stClass(x.status) }, x.status)))))) : h("p", { class: "muted" }, "No jobs yet in this session."));
  }
  refreshList();
  if (arg) watch(arg).catch(() => {});
  return h("div", {}, h("div", { class: "page-head" }, h("h1", {}, "Jobs"), h("p", {}, "Only allowlisted ave commands with checked arguments run here. Publishing, approving, sending and anything that skips a safety check are not on the list.")),
    h("div", { class: "dash" },
      h("div", { class: "col" }, h("section", { class: "panel" }, head, log)),
      h("div", { class: "col" },
        dash.actions.length ? h("section", { class: "panel" }, h("h2", {}, "Suggested now"), dash.actions.map((a) => h("div", { class: "act" }, h("strong", {}, a.label), h("span", { class: "why" }, a.why), h("button", { onclick: () => start(a.cmd, a.args) }, "Run")))) : null,
        h("section", { class: "panel" }, h("h2", {}, "Recent jobs"), listBox),
        h("details", { class: "panel" }, h("summary", {}, "Custom job"), h("div", { class: "row" }, cmd, h("button", { class: "primary", onclick: startCustom }, "Run")), argBox))));
}

// ---------------------------------------------------------------- router
const PAGES = { dashboard, clips: clipsPage, trim: trimPage, content: contentPage, publish: publishPage, jobs: jobsPage };
let seq = 0;
async function render() {
  const parts = (location.hash.replace(/^#\//, "") || "dashboard").split("/");
  const name = parts[0];
  const fn = PAGES[name] || dashboard;
  const my = ++seq; keyHandler = null; clearTimeout(timer);
  onLeave.forEach((f) => { try { f(); } catch (e) { /* page already gone */ } }); onLeave = [];
  document.querySelectorAll("#nav a").forEach((a) => { const on = a.dataset.p === name; a.classList.toggle("on", on); if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); });
  try { const el = await fn(parts[1]); if (my === seq) { fill(main, el); } }
  catch (e) { if (my === seq) fill(main, h("p", { class: "error" }, "Could not load: " + e.message)); }
}
document.addEventListener("keydown", (e) => { if (keyHandler) keyHandler(e); }, true);   // capture: a focused <video> must not swallow k/m/s/space
window.addEventListener("hashchange", render);
if (!location.hash) location.hash = "#/dashboard"; else render();
refreshBadges();
