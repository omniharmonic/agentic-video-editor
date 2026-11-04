// The viewer beside the Script and the Selects bin: one picture, a small transport, and a line that says what is playing.
// It plays a part of a recording (`playRange`, which stops at the end of the part) or plays on from a point (`cue` then play).
// Times here are the media's own seconds; the views map them to and from transcript time.

import { h, button, icon, fmtTime } from "../dom.js";

export function createViewer({ label = "The viewer", extra = null, onTime } = {}) {
  let media = null, until = null, raf = 0, pendingAt = null;

  const video = h("video", { class: "viewer-video", playsinline: true, preload: "auto", "aria-label": label });
  const overlay = h("div", { class: "viewer-overlay", hidden: true });
  const prompt = button("Play", { variant: "primary", iconName: "play", class: "cut-prompt", hidden: true, onclick: () => { prompt.hidden = true; play(); } });
  const frame = h("div", { class: "cut-frame viewer-frame" }, video, prompt, overlay);
  const now = h("span", { class: "cut-now num" }, "0:00.0");
  const what = h("p", { class: "viewer-label" });
  const playBtn = button("Play", { variant: "secondary", iconName: "play", iconOnly: true, class: "cut-play", onclick: () => toggle() });
  const backBtn = button("Back 5 seconds", { variant: "tertiary", size: "sm", iconName: "rewind", text: "5s", class: "cut-skip", onclick: () => nudge(-5) });
  const fwdBtn = button("Forward 5 seconds", { variant: "tertiary", size: "sm", iconName: "fastforward", text: "5s", class: "cut-skip cut-skip-fwd", onclick: () => nudge(5) });
  const muteBtn = button("Mute", { variant: "tertiary", iconName: "volume", iconOnly: true, "aria-pressed": "false", onclick: () => { video.muted = !video.muted; paintMute(); } });
  const transport = h("div", { class: "viewer-transport" },
    h("div", { class: "cut-transport-left" }, backBtn, playBtn, fwdBtn, h("span", { class: "cut-clock" }, now)),
    h("div", { class: "cut-transport-right" }, extra, muteBtn));
  const el = h("section", { class: "viewer", "aria-label": label }, frame, transport, what);

  function time() { return video.currentTime || 0; }
  function play() {
    if (!media) return;
    const p = video.play();
    p?.then(() => { prompt.hidden = true; }).catch((e) => {
      if (e && e.name === "NotAllowedError") { prompt.hidden = false; }
    });
  }
  function pause() { video.pause(); }
  function toggle() { if (!media) return; if (video.paused) { if (until != null && time() >= until - 0.05) until = null; play(); } else pause(); }
  function nudge(d) { if (!media) return; until = null; seek(time() + d); }
  function seek(t) {
    t = Math.max(0, t);
    if (video.readyState >= 1) video.currentTime = Math.min(t, video.duration || t); else pendingAt = t;
    paintTime();
  }

  // load `src` ({url, label}) at media time `at`; the same url only seeks
  function load(src, at = 0) {
    setMessage(null);
    if (!src) { media = null; video.removeAttribute("src"); video.load(); paintPlay(); return; }
    if (media?.url !== src.url) {
      media = src;
      pendingAt = at;
      video.src = src.url;
      video.load();
    } else { media = src; seek(at); }
    paintPlay();
  }
  function cue(src, at, { text } = {}) { until = null; load(src, at); if (text != null) setLabel(text); }
  function playRange(src, start, end, { text } = {}) {
    load(src, start);
    until = end;
    if (text != null) setLabel(text);
    play();
  }
  function setLabel(text) { what.textContent = text || ""; what.hidden = !text; }
  function setMessage(node) { overlay.hidden = !node; overlay.replaceChildren(...(node ? [node] : [])); }

  video.addEventListener("loadedmetadata", () => {
    if (pendingAt != null) { const t = pendingAt; pendingAt = null; video.currentTime = Math.min(t, video.duration || t); }
    paintTime();
  });
  video.addEventListener("error", () => {
    if (!media || !video.getAttribute("src")) return;
    setMessage(h("div", { class: "stage-msg" }, h("h2", {}, "This recording could not play here"),
      h("p", { class: "secondary-text" }, "The file may have moved, or this computer cannot decode it. Ask Claude to make a draft render."),
      button("Try again", { variant: "primary", onclick: () => { const m = media, t = time(); media = null; load(m, t); } })));
  });
  const loop = () => {
    paintTime();
    if (until != null && time() >= until) { until = null; video.pause(); return; }
    if (!video.paused) raf = requestAnimationFrame(loop);
  };
  video.addEventListener("play", () => { prompt.hidden = true; cancelAnimationFrame(raf); raf = requestAnimationFrame(loop); });
  video.addEventListener("pause", () => { cancelAnimationFrame(raf); paintTime(); });
  for (const ev of ["seeked", "timeupdate"]) video.addEventListener(ev, paintTime);
  for (const ev of ["play", "pause", "ended", "emptied", "loadedmetadata"]) video.addEventListener(ev, paintPlay);

  let lastT = -1;
  function paintTime() {
    const t = time();
    now.textContent = fmtTime(t);
    if (Math.abs(t - lastT) > 0.01) { lastT = t; onTime?.(t, !video.paused); }
  }
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
  paintPlay(); setLabel("");

  return { el, video, time, play, pause, toggle, nudge, seek, load, cue, playRange, setLabel, setMessage,
    get media() { return media; }, get playing() { return !video.paused; }, playButton: playBtn };
}
