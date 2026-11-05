// The Cut view: the picture, the transport and a review timeline (shots, the angles that could replace them, the changed part).
// It is for watching and pointing, not trimming: Resolve stays the editor for hand work.
//
// Keyboard model of the timeline: the ruler is a slider (arrows move 1 s, Shift 5 s, Home and End); the shots are one composite
// widget with a roving tab stop (one Tab stop however many shots), arrows move between shots in time order, Enter or Space
// selects one and moves the playhead there.

import { h, icon, button, fmtTime, clear } from "../dom.js";

const LANES = ["var(--lane-1)", "var(--lane-2)", "var(--lane-3)", "var(--lane-4)"];

export function createCut({ onFlag, onMediaError } = {}) {
  let cut = null, media = null, changed = null, selected = null, pendingSeek = null, current = null;

  const video = h("video", { class: "cut-video", playsinline: true, preload: "auto", "aria-label": "The cut" });
  const overlay = h("div", { class: "cut-overlay", hidden: true });
  const prompt = button("Play", { variant: "primary", iconName: "play", class: "cut-prompt", hidden: true, onclick: () => { prompt.hidden = true; play(); } });
  const frame = h("div", { class: "cut-frame" }, video, prompt, overlay);
  const stage = h("div", { class: "cut-stage" }, frame);

  // ---- transport
  const playBtn = button("Play", { variant: "secondary", iconName: "play", iconOnly: true, class: "cut-play", onclick: () => toggle() });
  const backBtn = button("Back 5 seconds", { variant: "tertiary", size: "sm", iconName: "rewind", text: "5s", class: "cut-skip", onclick: () => nudge(-5) });
  const fwdBtn = button("Forward 5 seconds", { variant: "tertiary", size: "sm", iconName: "fastforward", text: "5s", class: "cut-skip cut-skip-fwd", onclick: () => nudge(5) });
  const now = h("span", { class: "cut-now num" }, "0:00.0");
  const total = h("span", { class: "cut-total num" }, "0:00.0");
  const flagBtn = button("Flag this moment", { "data-edit": "1", variant: "secondary", size: "sm", iconName: "flag", "aria-haspopup": "dialog", "aria-expanded": "false",
    onclick: () => onFlag?.(flagBtn, cutTime()) });
  const muteBtn = button("Mute", { variant: "tertiary", iconName: "volume", iconOnly: true, "aria-pressed": "false", onclick: () => { video.muted = !video.muted; paintMute(); } });
  const transport = h("div", { class: "cut-transport" },
    h("div", { class: "cut-transport-left" }, backBtn, playBtn, fwdBtn,
      h("span", { class: "cut-clock" }, now, h("span", { class: "cut-of", "aria-hidden": "true" }, " / "), total)),
    h("div", { class: "cut-transport-right" }, flagBtn, muteBtn));

  // ---- timeline
  const changedLabel = h("span", { class: "tl-changed-label", hidden: true }, "Changed");
  const marks = h("div", { class: "tl-marks", "aria-hidden": "true" }, changedLabel);
  const ticks = h("div", { class: "tl-ticks", "aria-hidden": "true" });
  const changedLine = h("span", { class: "tl-changed-line", hidden: true, "aria-hidden": "true" });
  const winStart = h("span", { class: "tl-bracket tl-bracket-start", hidden: true, "aria-hidden": "true" });
  const winEnd = h("span", { class: "tl-bracket tl-bracket-end", hidden: true, "aria-hidden": "true" });
  const ruler = h("div", { class: "tl-ruler", role: "slider", tabindex: "0", "aria-label": "Position in the cut",
    "aria-valuemin": "0", "aria-valuemax": "0", "aria-valuenow": "0", "aria-valuetext": "0:00.0" }, ticks, changedLine, winStart, winEnd);
  const band = h("div", { class: "tl-changed", hidden: true, "aria-hidden": "true" });
  const lanes = h("div", { class: "tl-lanes", role: "toolbar", "aria-label": "Shots", "aria-orientation": "horizontal" }, band);
  const head = h("div", { class: "tl-head", "aria-hidden": "true" });
  const area = h("div", { class: "tl-area" }, marks, ruler, lanes, head);
  const gutter = h("div", { class: "tl-gutter" });
  const detail = h("p", { class: "tl-detail", "aria-live": "polite", hidden: true });
  const timeline = h("section", { class: "timeline", "aria-label": "Timeline" }, h("div", { class: "tl-body" }, gutter, area), detail);

  const el = h("div", { class: "cut" }, stage, transport, timeline);

  // ---- time: the player may hold the full cut or just a preview of part of it, so cut time = media start + player time
  function cutTime() { return media ? media.start + (video.currentTime || 0) : (pendingSeek ?? 0); }
  function duration() { return cut ? cut.duration : 0; }
  function pct(t) { const d = duration(); return d ? `${Math.max(0, Math.min(100, (t / d) * 100))}%` : "0%"; }

  function seek(t) {
    t = Math.max(0, Math.min(duration() || t, t));
    if (!media) { pendingSeek = t; paintTime(); return true; }
    if (t < media.start - 0.05 || t > media.end + 0.05) return false;
    if (video.readyState >= 1) video.currentTime = Math.max(0, t - media.start); else pendingSeek = t;
    paintTime();
    return true;
  }
  let outside = null;
  function seekOrSay(t) { if (seek(t)) return true; outside?.(t); return false; }
  function nudge(d) { const t = cutTime() + d; if (!seek(t) && media) seek(d < 0 ? media.start : media.end); }
  function play() {
    if (!media) return;
    const p = video.play();
    p?.then(() => { prompt.hidden = true; }).catch((e) => {
      if (e && e.name === "NotAllowedError") { prompt.hidden = false; prompt.querySelector("span").textContent = media.kind === "change" ? "Play the change" : "Play"; }
    });
  }
  function toggle() { if (!media) return; if (video.paused) play(); else video.pause(); }

  video.addEventListener("loadedmetadata", () => {
    if (pendingSeek != null && media) { const t = pendingSeek; pendingSeek = null; video.currentTime = Math.max(0, Math.min(t - media.start, video.duration || t)); }
    paintTime();
  });
  video.addEventListener("error", () => { if (media && video.getAttribute("src")) onMediaError?.(media, video.error?.code); });
  for (const ev of ["timeupdate", "seeked", "seeking"]) video.addEventListener(ev, paintTime);
  for (const ev of ["play", "pause", "ended"]) video.addEventListener(ev, paintPlay);
  let raf = 0;
  video.addEventListener("play", () => { prompt.hidden = true; const loop = () => { paintTime(); if (!video.paused) raf = requestAnimationFrame(loop); }; raf = requestAnimationFrame(loop); });
  video.addEventListener("pause", () => cancelAnimationFrame(raf));

  function paintPlay() {
    const playing = !video.paused && !video.ended;
    playBtn.replaceChildren(icon(playing ? "pause" : "play"));
    playBtn.setAttribute("aria-label", playing ? "Pause" : "Play");
    playBtn.dataset.tip = playing ? "Pause" : "Play";
    for (const b of [playBtn, backBtn, fwdBtn]) b.disabled = !media;
  }
  function paintMute() {
    muteBtn.replaceChildren(icon(video.muted ? "mute" : "volume"));
    muteBtn.setAttribute("aria-label", video.muted ? "Unmute" : "Mute");
    muteBtn.dataset.tip = video.muted ? "Unmute" : "Mute";
    muteBtn.setAttribute("aria-pressed", String(video.muted));
  }
  function paintTime() {
    const t = cutTime();
    now.textContent = fmtTime(t);
    head.style.left = pct(t);
    ruler.setAttribute("aria-valuenow", t.toFixed(1));
    ruler.setAttribute("aria-valuetext", fmtTime(t));
  }

  // ---- pointing: click or drag on the ruler or between shots moves the playhead
  function timeAt(x) { const r = ruler.getBoundingClientRect(); return ((x - r.left) / r.width) * duration(); }
  for (const surface of [ruler, lanes]) {
    surface.addEventListener("pointerdown", (e) => {
      if (e.target.closest(".tl-seg")) return;
      surface.setPointerCapture?.(e.pointerId);
      const move = (ev) => seekOrSay(timeAt(ev.clientX));
      move(e);
      surface.addEventListener("pointermove", move);
      surface.addEventListener("pointerup", () => surface.removeEventListener("pointermove", move), { once: true });
    });
  }
  ruler.addEventListener("keydown", (e) => {
    const step = e.shiftKey ? 5 : 1;
    if (e.key === "ArrowLeft" || e.key === "ArrowDown") { e.preventDefault(); nudge(-step); }
    else if (e.key === "ArrowRight" || e.key === "ArrowUp") { e.preventDefault(); nudge(step); }
    else if (e.key === "Home") { e.preventDefault(); seekOrSay(media ? media.start : 0); }
    else if (e.key === "End") { e.preventDefault(); seekOrSay(media ? media.end : duration()); }
  });

  // ---- shots: one roving tab stop
  const segButtons = () => [...lanes.querySelectorAll(".tl-seg")];
  function rove(btn, focus = true) {
    for (const b of segButtons()) b.tabIndex = b === btn ? 0 : -1;
    current = btn?.dataset.id ?? null;
    if (focus) btn?.focus();
  }
  lanes.addEventListener("keydown", (e) => {
    const all = segButtons(); if (!all.length) return;
    const i = Math.max(0, all.indexOf(document.activeElement));
    let j = null;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") j = Math.min(all.length - 1, i + 1);
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") j = Math.max(0, i - 1);
    else if (e.key === "Home") j = 0;
    else if (e.key === "End") j = all.length - 1;
    else if (e.key === "PageDown") j = Math.min(all.length - 1, i + 10);
    else if (e.key === "PageUp") j = Math.max(0, i - 10);
    if (j == null) return;
    e.preventDefault();
    rove(all[j]);
  });

  function select(seg, btn) {
    selected = seg.id;
    for (const b of segButtons()) b.setAttribute("aria-pressed", String(b === btn));
    rove(btn, false);
    const alts = seg.alternates.map((k) => cut.lanes.find((l) => l.src === k)?.label || "another camera");
    detail.hidden = false;
    detail.textContent = `${seg.label}, ${fmtTime(seg.o0)} to ${fmtTime(seg.o1)}. ` +
      (alts.length ? `The ${alts.join(" and the ")} also ${alts.length > 1 ? "cover" : "covers"} this shot.` : "No other camera covers this shot.");
    seekOrSay(seg.o0);
  }

  function paintTimeline() {
    clear(ticks); clear(gutter);
    for (const l of [...lanes.querySelectorAll(".tl-lane")]) l.remove();
    if (!cut) return;
    const d = cut.duration || 1;
    total.textContent = fmtTime(d);
    ruler.setAttribute("aria-valuemax", d.toFixed(1));
    const stepS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800].find((s) => d / s <= 12) || 3600;
    for (let t = 0; t <= d + 1e-6; t += stepS) {
      ticks.append(h("span", { class: "tl-tick", style: { left: pct(t) } }, h("span", { class: "tl-tick-label num" }, fmtTime(t).replace(/\.0$/, ""))));
    }
    const laneIndex = new Map(cut.lanes.map((l, i) => [l.src, i]));
    const laneEls = cut.lanes.map((l, i) => {
      gutter.append(h("div", { class: "tl-lane-label", title: l.label, style: { "--lane": LANES[i % 4] } }, h("span", { class: "dot", "aria-hidden": "true" }), l.label));
      const lane = h("div", { class: "tl-lane", dataset: { src: l.src } });
      lanes.append(lane);
      return lane;
    });
    const ordered = [...cut.segments].sort((a, b) => a.o0 - b.o0);
    let first = null;
    for (const seg of ordered) {
      const i = laneIndex.get(seg.src) ?? 0;
      const w = ((seg.o1 - seg.o0) / d) * 100;
      for (const alt of seg.alternates) {
        const j = laneIndex.get(alt);
        if (j == null) continue;
        laneEls[j].append(h("span", { class: "tl-alt", style: { left: pct(seg.o0), width: `${w}%`, "--lane": LANES[j % 4] }, "aria-hidden": "true" }));
      }
      const n = seg.alternates.length;
      const label = `${seg.label}, ${fmtTime(seg.o0)} to ${fmtTime(seg.o1)}${n ? `, ${n} other camera${n > 1 ? "s" : ""}` : ""}`;
      const b = h("button", { type: "button", class: "tl-seg", tabindex: "-1", "aria-label": label, "aria-pressed": String(selected === seg.id),
        dataset: { id: seg.id ?? "" }, style: { left: pct(seg.o0), width: `${w}%`, "--lane": LANES[i % 4] } },
        h("span", { class: "tl-seg-label" }, seg.label));
      b.addEventListener("click", () => select(seg, b));
      laneEls[i].append(b);
      first ??= b;
    }
    // the timeline's tab stop: the selected shot, else the one last focused, else the first
    const stop = segButtons().find((b) => b.dataset.id === String(selected)) || segButtons().find((b) => b.dataset.id === String(current)) || first;
    if (stop) stop.tabIndex = 0;
    paintChanged();
    paintTime();
  }

  function paintChanged() {
    const on = !!(changed && cut);
    band.hidden = changedLine.hidden = changedLabel.hidden = !on;
    if (on) {
      const left = pct(changed[0]), width = `calc(${pct(changed[1])} - ${pct(changed[0])})`;
      for (const x of [band, changedLine]) { x.style.left = left; x.style.width = width; }
      changedLabel.style.left = left;
      changedLabel.classList.toggle("is-end", changed[0] / (duration() || 1) > 0.85);       // near the end it hangs left instead
    }
    const win = !!(media && cut && media.kind !== "full");
    winStart.hidden = winEnd.hidden = !win;
    if (win) { winStart.style.left = pct(media.start); winEnd.style.left = pct(media.end); }
    frame.classList.toggle("is-change", !!(media && media.kind === "change"));
  }

  return {
    el, video, cutTime, seek, seekOrSay, play,
    get media() { return media; },
    set onOutside(fn) { outside = fn; },
    playButton: playBtn, flagButton: flagBtn,
    setCut(next) {
      const same = cut && next && cut.version === next.version && cut.artifact === next.artifact;
      cut = next;
      if (!same) { selected = null; detail.hidden = true; detail.textContent = ""; }
      paintTimeline();
    },
    setMedia(next, { at, play: autoplay = false } = {}) {
      const changedSrc = (next?.url || null) !== (media?.url || null);
      const keepAt = at ?? cutTime();
      media = next;
      prompt.hidden = true;
      if (changedSrc) {
        pendingSeek = keepAt;
        if (next) { video.src = next.url; video.load(); } else { video.removeAttribute("src"); video.load(); }
      } else if (next) {
        seek(keepAt);
      }
      if (next && autoplay) play();
      paintPlay(); paintChanged(); paintTime();
    },
    reload() { if (media) { const at = cutTime(); const m = media; media = null; this.setMedia(m, { at }); } },
    setChanged(span) { changed = span; paintChanged(); },
    setOverlay(node) { overlay.hidden = !node; overlay.replaceChildren(...(node ? [node] : [])); },
    toggle, nudge,
  };
}
