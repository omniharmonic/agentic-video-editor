// The Script view: the whole transcript beside the viewer. Claude's pulled sections are marked and listed under the picture;
// clicking one plays it, clicking a line plays from there, and the line being heard follows playback. Search marks every match
// (Enter or n for the next, Shift+Enter or N for the one before). Words or a line can be flagged for Claude with a note.
//
// Long conversations (two hours, 20,000 words) stay smooth: lines are built a hundred at a time between frames, each hundred
// sits in a block the browser skips laying out while it is off screen, and search marks are redrawn in slices.
// Times are transcript (episode) seconds; the viewer's camera has its own clock, mapped with its offset and drift.

import { h, button, icon, fmtTime, toast, popover } from "../dom.js";
import { createViewer } from "./viewer.js";
import { flagPopover } from "./flag.js";
// a copy in line behind another camera's (x is minus the number of copies ahead)
const waitText = (x) => `Waiting for another camera to finish: ${-x === 1 ? "1 copy" : `${-x} copies`} ahead of this one.`;
const lowerFirst = (t) => (t ? t[0].toLowerCase() + t.slice(1) : "") ;

const CHUNK = 100;              // lines per block
const SLICE = 250;              // lines re-marked per task while searching
const FOLLOW_PAUSE_MS = 4000;   // after the director scrolls, playback stops pulling the script along for this long

const clock = (t) => fmtTime(t).replace(/\.\d$/, "");
// case-folded text the same length as the original, so a match's offsets fit the line as shown ("İ" folds to two characters)
const fold = (s) => s.replace(/./gsu, (ch) => { const l = ch.toLowerCase(); return l.length === ch.length ? l : ch; });
// a recording's clock, as editdoc.src_time: offset and drift, or the piecewise sync map with the drift line beyond its ends
function interp(x, xs, ys, slope) {
  if (x <= xs[0]) return ys[0] + (x - xs[0]) * slope;
  if (x >= xs[xs.length - 1]) return ys[ys.length - 1] + (x - xs[xs.length - 1]) * slope;
  let lo = 0, hi = xs.length - 1;
  while (hi - lo > 1) { const m = (lo + hi) >> 1; if (xs[m] <= x) lo = m; else hi = m; }
  return ys[lo] + (x - xs[lo]) * (ys[hi] - ys[lo]) / ((xs[hi] - xs[lo]) || 1);
}
export function toFileTime(c, e) { return c?.ft ? interp(e, c.et, c.ft, 1 / c.k) : (e - (c?.start_offset || 0)) / (c?.k || 1); }
export function toEpisodeTime(c, m) { return c?.ft ? interp(m, c.ft, c.et, c.k) : m * (c?.k || 1) + (c?.start_offset || 0); }
const span = (a, b) => (clock(a) === clock(b) ? clock(a) : `${clock(a)} to ${clock(b)}`);
const later = (fn) => setTimeout(fn, 0);

export function createScript({ call, plain, sendFlag, onInfo, ensureSource }) {
  let info = null, artifact = null, loading = null, lines = [], lower = [], lineEls = [], src = null;
  let highlights = [], pulled = [], flags = [], hits = [], hitAt = -1, query = "", playingIdx = -1, current = 0, userScrollAt = 0;
  let renderGen = 0, markGen = 0, marked = new Set(), selection = null, pendingCue = null, queued = null;

  // ---- layout
  const camBtn = button("Camera", { variant: "tertiary", size: "sm", class: "viewer-cam", hidden: true, "aria-haspopup": "menu", "aria-expanded": "false",
    onclick: () => cameraMenu() });
  const viewer = createViewer({ label: "The viewer", extra: camBtn, onTime: (t, playing) => follow(t, playing) });
  const pullsList = h("div", { class: "sc-pulls-list" });
  const pullsHead = h("h2", { class: "sc-pulls-head" });
  const pulls = h("section", { class: "sc-pulls", "aria-label": "Sections Claude pulled", hidden: true }, pullsHead, pullsList);
  const search = h("input", { class: "input", type: "search", "aria-label": "Search the transcript", placeholder: "Search", autocomplete: "off", spellcheck: "false" });
  const count = h("span", { class: "sc-count", "aria-live": "polite" });
  const prevBtn = button("Previous match", { variant: "tertiary", size: "sm", iconName: "chevron", iconOnly: true, class: "sc-prev", disabled: true, "data-tip": "Previous match (Shift+Enter or N)", onclick: () => step(-1) });
  const nextBtn = button("Next match", { variant: "tertiary", size: "sm", iconName: "chevron", iconOnly: true, class: "sc-next", disabled: true, "data-tip": "Next match (Enter or n)", onclick: () => step(1) });
  const linesEl = h("div", { class: "sc-lines", role: "list", "aria-label": "Transcript" });
  const msg = h("div", { class: "sc-msg", hidden: true });
  const scroll = h("div", { class: "sc-scroll", tabindex: "-1" }, linesEl, msg);
  const selBtn = button("Flag the selection for Claude", { variant: "primary", size: "sm", iconName: "flag", text: "Flag", class: "sc-selflag", hidden: true,
    onmousedown: (e) => e.preventDefault(), onclick: () => flagSelection() });
  const speakersEl = h("p", { class: "sc-speakers secondary-text" });
  const toolbar = h("div", { class: "sc-toolbar" },
    h("label", { class: "search-field sc-search" }, icon("search"), search), count, prevBtn, nextBtn, h("span", { class: "sc-spacer" }), speakersEl);
  const main = h("section", { class: "sc-main", "aria-label": "Script" }, toolbar, scroll, selBtn);
  const el = h("div", { class: "script", hidden: true }, h("aside", { class: "sc-side" }, viewer.el, pulls), main);

  // ---- time on the viewer's camera
  const toMedia = (e) => Math.max(0, toFileTime(src?.clock, e));
  const toEpisode = (m) => toEpisodeTime(src?.clock, m);
  const media = () => (src ? { url: src.url, label: src.label } : null);

  // ---- loading
  // `force` reads it again (Claude showed it, or the transcript changed); `keep` keeps the place and what is playing
  async function open(a, { force = false, keep = false } = {}) {
    if (!a || (!force && a === artifact && (info || loading))) return;
    keep = keep && a === artifact && !!info;
    const resume = keep && src && viewer.media ? toEpisode(viewer.time()) : null;
    const top = keep ? scroll.scrollTop : 0;
    artifact = a;
    if (!keep) info = null;
    const mine = loading = call("GET", `/api/room/script?artifact=${encodeURIComponent(a)}`);
    if (!keep) showMsg(h("div", { class: "sc-skel", "aria-label": "Loading the script" }, [80, 64, 90, 52, 74, 60].map((w) => h("span", { class: "skeleton skeleton-text", style: { width: `${w}%` } }))));
    try {
      const got = await mine;
      if (mine !== loading) return;
      info = got;
    } catch (e) {
      if (mine !== loading) return;
      loading = null;
      showMsg(h("div", { class: "stage-msg" }, h("h2", {}, "The script could not be read"), h("p", { class: "secondary-text" }, plain(e, "read the script")),
        button("Try again", { variant: "primary", onclick: () => open(a, { force: true }) })));
      return;
    }
    loading = null;
    onInfo?.(info);
    if (resume != null) pendingCue = resume;
    setup({ keep });
    if (keep) { scroll.scrollTop = top; userScrollAt = Date.now(); }
  }

  function showMsg(node) {
    linesEl.replaceChildren(); lineEls = [];
    msg.hidden = !node; msg.replaceChildren(...(node ? [node] : []));
  }

  function setup({ keep = false } = {}) {
    lines = info.lines || [];
    lower = lines.map((l) => fold(l.text));
    const was = src?.key;
    src = (info.sources || []).find((s) => s.key === (keep && was ? was : info.source)) || (info.sources || [])[0] || null;
    camBtn.hidden = !src || (info.sources || []).length < 2;
    paintCam();
    speakersEl.textContent = info.speakers?.length ? info.speakers.join(" and ") : "";
    hits = []; hitAt = -1; marked = new Set(); playingIdx = -1; current = 0;
    if (!info.has_transcript || !lines.length) {
      const why = info.transcript === "unreadable"
        ? ["The transcript could not be read", "The file is there but damaged. Ask Claude to transcribe this episode again."]
        : info.transcript === "ok" ? ["The transcript has no words", "Nobody speaks in what was transcribed. Ask Claude to check the recording it used."]
        : ["No transcript yet", "Ask Claude to transcribe this episode. The script appears here the moment it is ready."];
      showMsg(h("div", { class: "stage-msg" }, h("h2", {}, why[0]), h("p", { class: "secondary-text" }, why[1])));
      viewer.load(null);
      paintPulls();
      return;
    }
    msg.hidden = true;
    render();
    if (src && !src.ready) prepare(src);
    if (!src) viewer.setMessage(h("div", { class: "stage-msg" }, h("h2", {}, "No recording to play"), h("p", { class: "secondary-text" }, "The script is here; the cameras are not in this project folder.")));
    const at = pendingCue ?? lines[0].t0;
    pendingCue = null;
    cue(at);
    paintPulls();
    if (query) runSearch();
  }

  // ---- lines, built in blocks between frames so nothing blocks the page
  function lineEl(l) {
    const time = h("button", { type: "button", class: "sc-time num", tabindex: "-1", "aria-label": `Play from ${clock(l.t0)}` }, clock(l.t0));
    const flagBtn = button("Flag this line for Claude", { variant: "tertiary", size: "sm", iconName: "flag", iconOnly: true, class: "sc-flag-btn", tabindex: "-1" });
    const prev = lines[l.i - 1];
    return h("div", { class: `sc-line sp-${l.s % 4}${prev && prev.s === l.s ? " is-cont" : ""}`, role: "listitem", dataset: { i: l.i } },
      time, h("div", { class: "sc-body" }, h("span", { class: "sc-sp" }, l.sp), h("p", { class: "sc-text" }, l.text)), flagBtn);
  }

  function render() {
    const gen = ++renderGen;
    linesEl.replaceChildren();
    lineEls = new Array(lines.length);
    let n = 0;
    const block = () => {
      if (gen !== renderGen) return;
      const box = h("div", { class: "sc-chunk" });
      const end = Math.min(lines.length, n + CHUNK);
      for (; n < end; n++) box.append(lineEls[n] = lineEl(lines[n]));
      linesEl.append(box);
      paintLines(n - CHUNK, n);
      if (n < lines.length) later(block);
      else {
        rove(current, false);
        // a cue or a search that arrived while lines were still being built: put them on screen now that they exist
        if (hitAt >= 0) showCurrent();
        else if (playingIdx >= 0 && Date.now() - userScrollAt > FOLLOW_PAUSE_MS) setPlaying(playingIdx, { scrollTo: true, center: true });
        el.dispatchEvent(new CustomEvent("rendered"));
      }
    };
    block();
  }

  // the classes a line carries: pulled by Claude, playing, flagged, the roving tab stop
  function paintLines(from = 0, to = lines.length) {
    for (let i = Math.max(0, from); i < to; i++) {
      const e = lineEls[i]; if (!e) continue;
      const p = pulled[i];
      e.classList.toggle("is-pulled", !!p);
      e.classList.toggle("is-pull-start", !!p && p.first);
      e.classList.toggle("is-playing", i === playingIdx);
      if (marked.has(i)) markLine(i);
      paintFlags(i);
    }
  }

  // ---- the line being heard
  function lineAt(t) {
    let lo = 0, hi = lines.length - 1, ans = 0;
    while (lo <= hi) { const mid = (lo + hi) >> 1; if (lines[mid].t0 <= t + 0.05) { ans = mid; lo = mid + 1; } else hi = mid - 1; }
    return ans;
  }
  function setPlaying(i, { scrollTo = false, center = false } = {}) {
    if (i !== playingIdx) {
      lineEls[playingIdx]?.classList.remove("is-playing");
      playingIdx = i;
      lineEls[i]?.classList.add("is-playing");
    }
    if (scrollTo && lineEls[i]) lineEls[i].scrollIntoView({ block: center ? "center" : "nearest" });
  }
  function follow(m, playing) {
    if (!lines.length || !src) return;
    const i = lineAt(toEpisode(m));
    const moved = i !== playingIdx;
    setPlaying(i, { scrollTo: moved && playing && Date.now() - userScrollAt > FOLLOW_PAUSE_MS });
  }
  for (const ev of ["wheel", "touchmove"]) scroll.addEventListener(ev, () => { userScrollAt = Date.now(); }, { passive: true });

  // the viewer plays a review copy of the camera; the first time, it is made while the script is already readable
  function ready(fn) {
    if (!src) return;
    if (src.ready) { fn(); return; }
    queued = fn;
    prepare(src);
  }
  let preparing = null;
  function prepare(s) {
    if (preparing === s) return;
    preparing = s;
    const bar = h("span", { class: "progress-fill" });
    const note = h("p", { class: "secondary-text" }, "The original stays untouched. Read, search and flag the script meanwhile.");
    viewer.setMessage(h("div", { class: "stage-msg" }, h("span", { class: "progress", role: "progressbar", "aria-label": `Making a copy of the ${s.label} to play here` }, bar),
      h("p", {}, `Making a copy of the ${s.label} to play here`), note));
    viewer.setLabel(s.label);
    const meanwhile = note.textContent;
    ensureSource(artifact, s.key, (x) => {
      if (x < 0) { note.textContent = waitText(x); return; }
      note.textContent = meanwhile; bar.style.width = `${Math.round(x * 100)}%`;
    }).then(() => {
      if (preparing === s) preparing = null;
      for (const o of info?.sources || []) if (o.key === s.key) o.ready = true;
      if (s !== src) return;
      viewer.setMessage(null);
      const fn = queued; queued = null;
      if (fn) fn(); else viewer.cue(media(), toMedia(lines[Math.max(0, playingIdx)]?.t0 || 0), { text: `${src.label}` });
    }).catch((e) => {
      if (preparing === s) preparing = null;
      if (s !== src) return;
      viewer.setMessage(h("div", { class: "stage-msg" }, h("h2", {}, "This recording could not be prepared"),
        h("p", { class: "secondary-text" }, `Its copy for the viewer could not be made: ${lowerFirst(e.message)} The script still works.`),
        button("Try again", { variant: "primary", onclick: () => prepare(s) })));
    });
  }
  function cue(e) {
    if (!lines.length) { pendingCue = e; return; }
    setPlaying(lineAt(e), { scrollTo: true, center: true });
    if (src && src.ready) viewer.cue(media(), toMedia(e), { text: `${src.label}, from ${clock(e)}` });
    else if (src) queued = () => viewer.cue(media(), toMedia(e), { text: `${src.label}, from ${clock(e)}` });
  }
  function playFrom(i) {
    const l = lines[i]; if (!l || !src) return;
    current = i; rove(i, false);
    setPlaying(i);
    ready(() => {
      viewer.cue(media(), toMedia(l.t0), { text: `${src.label}, from ${clock(l.t0)}` });
      viewer.play();
    });
  }

  // ---- what Claude pulled
  function ranges() {
    return highlights.map((x) => {
      const c = x.clip && info?.clips?.[x.clip];
      const start = Number.isFinite(x.start) ? x.start : c?.e0, end = Number.isFinite(x.end) ? x.end : c?.e1;
      return Number.isFinite(start) && Number.isFinite(end) && end >= start ? { start, end, note: x.note || "", title: c?.title || "" } : null;
    }).filter(Boolean).sort((a, b) => a.start - b.start);
  }
  function paintPulls() {
    pulled = [];
    const rs = info && lines.length ? ranges() : [];
    const firstOf = (r) => { let i = lineAt(r.start); while (i < lines.length - 1 && lines[i].t1 <= r.start) i++; return i; };
    for (const r of rs) {
      let first = true;
      for (let i = lineAt(r.start); i < lines.length && lines[i].t0 < r.end; i++) {
        if (lines[i].t1 <= r.start) continue;
        pulled[i] = { first, note: r.note };
        first = false;
      }
    }
    pulls.hidden = !rs.length;
    pullsHead.textContent = rs.length === 1 ? "Claude pulled 1 section" : `Claude pulled ${rs.length} sections`;
    pullsList.replaceChildren(...rs.map((r) => {
      const i = firstOf(r), first = lines[i];
      const words = first ? first.text.split(" ").slice(0, 18).join(" ") + (first.text.split(" ").length > 18 ? "…" : "") : "";
      const b = h("button", { type: "button", class: "sc-pull" },
        h("span", { class: "sc-pull-time num" }, icon("play"), span(r.start, r.end)),
        r.title ? h("span", { class: "sc-pull-title" }, r.title) : null,
        r.note ? h("span", { class: "sc-pull-note" }, r.note) : null,
        words ? h("span", { class: "sc-pull-words" }, `${first.sp}: ${words}`) : null);
      b.addEventListener("click", () => playPull(r, b));
      return b;
    }));
    paintLines();
  }
  function playPull(r, b) {
    for (const x of pullsList.children) x.classList.toggle("is-current", x === b);
    if (!src) return;
    const what = r.title || (r.note.length > 48 ? r.note.slice(0, 47) + "…" : r.note) || "Claude's section";
    ready(() => viewer.playRange(media(), toMedia(r.start), toMedia(r.end), { text: `${what}, ${span(r.start, r.end)}` }));
    const i = lineAt(r.start);
    userScrollAt = 0;
    setPlaying(i, { scrollTo: true, center: true });
  }

  // ---- search
  let searchTimer = 0;
  search.addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(runSearch, 120); });
  search.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault(); clearTimeout(searchTimer);
      if (fold(search.value.trim()) !== query) runSearch({ first: true });     // typed and pressed Enter at once: the first match
      else step(e.shiftKey ? -1 : 1);
    }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); if (search.value && !query) search.value = ""; search.blur(); scroll.focus({ preventScroll: true }); }
  });
  let byLine = new Map();                       // line -> the indexes of its matches in `hits`
  function runSearch({ first = false } = {}) {
    query = fold(search.value.trim());
    const was = marked;
    hits = [];
    byLine = new Map();
    marked = new Set();
    if (query) {
      for (let i = 0; i < lower.length; i++) {
        let at = lower[i].indexOf(query);
        if (at < 0) continue;
        marked.add(i);
        const mine = [];
        while (at >= 0) { mine.push(hits.length); hits.push({ i, from: at, to: at + query.length }); at = lower[i].indexOf(query, at + query.length); }
        byLine.set(i, mine);
      }
    }
    hitAt = !hits.length ? -1 : first ? 0 : Math.max(0, hits.findIndex((x) => x.i >= Math.max(0, playingIdx)));
    const todo = [...new Set([...was, ...marked])];
    const gen = ++markGen;
    const slice = (k) => {
      if (gen !== markGen) return;
      for (const i of todo.slice(k, k + SLICE)) markLine(i);
      if (k + SLICE < todo.length) later(() => slice(k + SLICE));
    };
    if (hitAt >= 0) markLine(hits[hitAt].i);         // the current match first, so it is on screen at once
    slice(0);
    paintCount();
    showCurrent();
  }
  function markLine(i) {
    const e = lineEls[i]?.querySelector(".sc-text"); if (!e) return;
    const text = lines[i].text;
    if (!marked.has(i)) { if (e.firstElementChild) e.textContent = text; return; }
    const kids = []; let at = 0;
    for (const j of byLine.get(i) || []) {
      const x = hits[j];
      if (x.from > at) kids.push(text.slice(at, x.from));
      kids.push(h("mark", { class: `sc-hit${j === hitAt ? " is-current" : ""}`, dataset: { h: j } }, text.slice(x.from, x.to)));
      at = x.to;
    }
    kids.push(text.slice(at));
    e.replaceChildren(...kids);
  }
  function paintCount() {
    const has = hits.length > 0;
    count.textContent = !query ? "" : has ? `${hitAt + 1} of ${hits.length}` : "No line says that";
    count.classList.toggle("is-none", !!query && !has);
    prevBtn.disabled = nextBtn.disabled = !has;
  }
  function showCurrent() {
    for (const m of linesEl.querySelectorAll("mark.is-current")) m.classList.remove("is-current");
    if (hitAt < 0) return;
    const x = hits[hitAt];
    if (!lineEls[x.i]) return;
    if (!lineEls[x.i].querySelector("mark")) markLine(x.i);
    const m = lineEls[x.i].querySelector(`mark[data-h="${hitAt}"]`);
    m?.classList.add("is-current");
    userScrollAt = Date.now();
    (m || lineEls[x.i]).scrollIntoView({ block: "center" });
  }
  function step(d) {
    if (!hits.length) return;
    hitAt = (hitAt + d + hits.length) % hits.length;
    paintCount(); showCurrent();
  }

  // ---- flags: a line, or the words selected in the script
  function flaggedLine(f) { return f.view === "script" && f.artifact === artifact && lines.length ? lineAt(f.start) : -1; }
  function paintFlags(i) {
    const body = lineEls[i]?.querySelector(".sc-body"); if (!body) return;
    const mine = flags.filter((f) => flaggedLine(f) === i);
    let box = body.querySelector(".sc-flags");
    if (!mine.length) { box?.remove(); lineEls[i].classList.remove("is-flagged"); return; }
    if (!box) body.append(box = h("div", { class: "sc-flags" }));
    lineEls[i].classList.add("is-flagged");
    box.replaceChildren(...mine.map((f) => h("div", { class: "sc-flag", dataset: { state: f.status } }, icon("flag"),
      h("span", { class: "sc-flag-note" }, f.note ? `“${f.note}”` : "Flagged for Claude"),
      h("span", { class: "sc-flag-state" }, f.status === "read" ? "Claude has it" : "Waiting for your next message"))));
  }
  function setFlags(list) {
    const before = new Set(flags.map((f) => flaggedLine(f)));
    flags = list;
    for (const i of new Set([...before, ...flags.map((f) => flaggedLine(f))])) if (i >= 0) paintFlags(i);
  }

  async function send(flag, where) {
    try {
      await sendFlag({ artifact, view: "script", ...flag });
      toast(`Flagged ${where} for Claude`, { key: "flag" });
    } catch (e) { toast(plain(e, "flag this for Claude"), { tone: "negative" }); throw e; }
  }
  function flagLine(i, anchor) {
    const l = lines[i]; if (!l) return;
    flagPopover(anchor || lineEls[i]?.querySelector(".sc-flag-btn") || search, { title: `Flag ${span(l.t0, l.t1)}`,
      send: (note) => send({ start: l.t0, end: l.t1, note, text: l.text.slice(0, 2000) }, span(l.t0, l.t1)) });
  }

  function textOffset(textEl, node, offset) {
    const r = document.createRange();
    r.setStart(textEl, 0);
    try { r.setEnd(node, offset); } catch { return 0; }
    return r.toString().length;
  }
  function wordAt(l, c, end) {
    let at = 0, k = 0;
    const ws = l.text.split(" ");
    for (; k < ws.length; k++) { const next = at + ws[k].length + 1; if ((end ? c <= next - 1 : c < next)) break; at = next; }
    return Math.min(k, ws.length - 1);
  }
  function point(node, offset, isEnd) {
    const e = node.nodeType === 1 ? node : node.parentElement;
    const lineE = e?.closest?.(".sc-line"); if (!lineE || !linesEl.contains(lineE)) return null;
    const i = Number(lineE.dataset.i), textEl = lineE.querySelector(".sc-text");
    if (!textEl.contains(node)) return { i, c: isEnd ? lines[i].text.length : 0 };
    return { i, c: textOffset(textEl, node, offset) };
  }
  function readSelection() {
    const sel = getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount || el.hidden) return null;
    const r = sel.getRangeAt(0);
    if (!linesEl.contains(r.commonAncestorContainer)) return null;
    const a = point(r.startContainer, r.startOffset, false), b = point(r.endContainer, r.endOffset, true);
    if (!a || !b || (a.i === b.i && b.c <= a.c)) return null;
    const parts = [];
    for (let i = a.i; i <= b.i; i++) parts.push(lines[i].text.slice(i === a.i ? a.c : 0, i === b.i ? b.c : undefined));
    const text = parts.join(" ").replace(/\s+/g, " ").trim();
    if (!text) return null;
    const wa = wordAt(lines[a.i], a.c, false), wb = wordAt(lines[b.i], Math.max(0, b.c - 1), true);
    return { start: lines[a.i].w[2 * wa], end: lines[b.i].w[2 * wb + 1], text: text.slice(0, 2000), rect: r.getBoundingClientRect() };
  }
  const life = new AbortController();
  document.addEventListener("selectionchange", () => {
    selection = readSelection();
    selBtn.hidden = !selection;
    if (selection) {
      const box = main.getBoundingClientRect();
      const x = Math.min(box.width - 96, Math.max(8, selection.rect.right - box.left - 40));
      const y = Math.max(48, selection.rect.top - box.top - 40);
      Object.assign(selBtn.style, { left: `${x}px`, top: `${y}px` });
    }
  }, { signal: life.signal });
  function flagSelection() {
    const s = selection; if (!s) return;
    flagPopover(selBtn, { title: `Flag “${s.text.length > 40 ? s.text.slice(0, 39) + "…" : s.text}”`,
      send: (note) => send({ start: s.start, end: s.end, note, text: s.text }, span(s.start, s.end)).then(() => getSelection().removeAllRanges()) });
  }

  // ---- lines are one tab stop: Up and Down move between them, Enter plays, the flag button flags
  function rove(i, focus = true) {
    const e = lineEls[i]; if (!e) return;
    for (const b of linesEl.querySelectorAll(".sc-time[tabindex='0'], .sc-flag-btn[tabindex='0']")) b.tabIndex = -1;
    e.querySelector(".sc-time").tabIndex = 0;
    e.querySelector(".sc-flag-btn").tabIndex = 0;
    current = i;
    if (focus) { e.querySelector(".sc-time").focus(); }
  }
  linesEl.addEventListener("click", (e) => {
    const lineE = e.target.closest(".sc-line"); if (!lineE) return;
    const i = Number(lineE.dataset.i);
    if (e.target.closest(".sc-flag-btn")) { rove(i, false); flagLine(i, e.target.closest(".sc-flag-btn")); return; }
    if (e.target.closest(".sc-time") || getSelection().isCollapsed) playFrom(i);
  });
  linesEl.addEventListener("keydown", (e) => {
    const lineE = e.target.closest(".sc-line"); if (!lineE) return;
    const i = Number(lineE.dataset.i);
    let j = null;
    if (e.key === "ArrowDown") j = Math.min(lines.length - 1, i + 1);
    else if (e.key === "ArrowUp") j = Math.max(0, i - 1);
    else if (e.key === "PageDown") j = Math.min(lines.length - 1, i + 10);
    else if (e.key === "PageUp") j = Math.max(0, i - 10);
    else if (e.key === "Home" && e.ctrlKey) j = 0;
    else if (e.key === "End" && e.ctrlKey) j = lines.length - 1;
    if (j == null) return;
    e.preventDefault();
    const wasFlag = e.target.classList.contains("sc-flag-btn");
    rove(j);
    if (wasFlag) lineEls[j].querySelector(".sc-flag-btn").focus();
    lineEls[j].scrollIntoView({ block: "nearest" });
  });

  // ---- the camera the viewer shows
  function paintCam() {
    camBtn.replaceChildren(h("span", {}, src ? src.label : "Camera"), icon("chevron"));
    camBtn.setAttribute("aria-label", src ? `Camera: ${src.label}` : "Camera");
  }
  function cameraMenu() {
    const items = (info?.sources || []).map((s) => {
      const b = h("button", { type: "button", class: "menu-item", role: "menuitemradio", "aria-checked": String(s === src) }, s.label);
      b.addEventListener("click", () => { pop.close(); pick(s); });
      return b;
    });
    const menuEl = h("div", { class: "menu", role: "menu", "aria-label": "Camera" }, items);
    menuEl.addEventListener("keydown", (e) => {
      const i = items.indexOf(document.activeElement);
      if (e.key === "ArrowDown") { e.preventDefault(); items[(i + 1) % items.length].focus(); }
      if (e.key === "ArrowUp") { e.preventDefault(); items[(i - 1 + items.length) % items.length].focus(); }
    });
    const pop = popoverMenu(camBtn, menuEl);
    (items.find((b) => b.getAttribute("aria-checked") === "true") || items[0])?.focus();
  }
  function pick(s) {
    if (s === src) return;
    const e = toEpisode(viewer.time()), was = viewer.playing;
    src = s; paintCam();
    if (!src.ready) viewer.load(null);
    ready(() => {
      viewer.cue(media(), toMedia(e), { text: `${src.label}, from ${clock(e)}` });
      if (was) viewer.play();
    });
  }

  return {
    el, open, cue, viewer,
    get info() { return info; },
    setHighlights(list) { highlights = Array.isArray(list) ? list : []; if (info) paintPulls(); },
    setFlags,
    show(on) { el.hidden = !on; if (!on) { viewer.pause(); selBtn.hidden = true; } },
    focusSearch() { search.focus(); search.select(); },
    next() { step(1); }, prev() { step(-1); },
    flagKey() {
      if (selection) return flagSelection();
      const focused = document.activeElement?.closest?.(".sc-line");
      flagLine(focused ? Number(focused.dataset.i) : playingIdx >= 0 ? playingIdx : current);
    },
    close() { life.abort(); renderGen++; markGen++; },
  };
}

// a menu in the shared popover, closed by Esc or an outside click
function popoverMenu(anchor, menuEl) { return popover(anchor, menuEl, { place: "below", align: "end", label: "Camera" }); }
