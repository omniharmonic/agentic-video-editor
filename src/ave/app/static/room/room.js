// The Room: one stage that follows the conversation with Claude. Claude puts a version on stage from Claude Code; the director
// watches it here and answers with Keep this or Go back, which work at once without Claude. Everything the Room shows comes from
// the stream (GET /api/room/stream), so a click anywhere (or Claude) is reflected by the same events.

import { h, button, busy, toast, popover, icon, fmtTime, typing, versionLabel, shortVersion } from "../dom.js";
import { createCut } from "./cut.js";
import { createScript } from "./script.js";
import { createSelects } from "./selects.js";
import { createLayout } from "./layout.js";
import { renderStrip } from "./strip.js";
import { avatar, boardWrite } from "../workspace/common.js";

const RETRIES = 3;

class AppError extends Error {
  constructor(message, { status = 0, network = false } = {}) { super(message); this.status = status; this.network = network; }
}

// A project a newer AVE saved is read-only here: every Room answer says so (`update_needed`), and the Room shows the banner.
let sawUpdateNeeded = null;
// The controls that change the project: flags and notes, Keep this / Go back, trims, keep/maybe/skip and their reasons, select
// ends, and the Layout composer's editing. Watching, reading, searching and switching views stay.
const EDIT_CONTROLS = '[data-edit], [aria-label^="Flag"], .sc-selflag, .strip-trim, .sel-keep, .sel-maybe, .sel-skip, .sel-reason, .sel-note, '
  + '[data-act="save-why"], [data-nudge], .lay-input, .lay-alt, .lay-rail [draggable], #flag-note';

async function call(method, path, body) {
  const viaApp = await boardWrite(method, path, body, AppError);     // the director's own board writes: through the app's bridge
  if (viaApp) return viaApp.data;
  let r;
  try {
    r = await fetch(path, { method, credentials: "same-origin", headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined });
  } catch (e) { throw new AppError(String(e), { network: true }); }
  let data = {};
  try { data = await r.json(); } catch { /* an empty or non-JSON reply */ }
  if (!r.ok) throw new AppError(data.error || `HTTP ${r.status}`, { status: r.status });
  if (data && Object.prototype.hasOwnProperty.call(data, "update_needed")) sawUpdateNeeded?.(data.update_needed);
  return data;
}

// The app's own words for a failed request; the server's text goes in a title attribute, never in the sentence.
function plain(e, doing) {
  if (e.network) return `Could not ${doing}. The app is not answering: check that AVE Studio is still open.`;
  if (e.status === 404) return `Could not ${doing}. That version is no longer in this project.`;
  if (e.status === 409 || e.status === 400) return `Could not ${doing}. The stage changed a moment ago: look again and choose.`;
  if (e.status === 429 || e.status === 503) return `Could not ${doing}. The app is busy: try again in a moment.`;
  return `Could not ${doing}. Try again.`;
}

const SHORTCUTS = [["1, 2, 3", "Cut, Script or Selects (1 is Layout for a post)"], ["Space or K", "Play or pause"], ["J and L", "Back or forward 5 seconds"],
  ["F", "Flag this moment, line or select for Claude"], ["/", "Search the script or find a select"], ["N and Shift+N", "Next or previous match"],
  ["Y", "Keep this version"], ["B", "Go back to the version before"], ["?", "Show these shortcuts"]];
const VIEWS = [["cut", "Cut"], ["script", "Script"], ["selects", "Selects"], ["layout", "Layout"]];
const viewOf = (v) => (VIEWS.some(([k]) => k === v) ? v : "cut");
// The view set depends on the artifact: video and audio have Cut, Script and Selects; a post, thumbnail or other design artifact
// has Layout. The board's kind decides, and Claude showing the layout view says so before the board has answered.
const DESIGN_KINDS = ["post", "thumbnail", "newsletter"];
const VIDEO_VIEWS = ["cut", "script", "selects"];

export function mountRoom(root, { artifact } = {}) {
  const s = { artifact: artifact || null, stage: null, offer: null, preview: null, cut: null, cutError: null, mediaError: null, full: {}, part: {},
    rendering: new Set(), mode: "change", flags: 0, live: "connecting", seq: 0, loading: null, view: "cut", info: null, myFlags: new Map(),
    ready: null,                                         // readiness of the version on stage when it cannot be opened yet
    together: null };                                    // the board's word on this artifact: a stage chosen twice, who else is here
  const probe = window.__AVE_TEST__ ? (window.__room = { events: 0, last: null, state: s }) : null;

  // ---- layout
  const title = h("h1", { class: "room-title" }, "The Room");
  const chip = h("span", { class: "version-chip", hidden: true });
  const replacing = h("span", { class: "room-replacing", hidden: true });
  const arrivePill = h("span", { class: "pill room-arrive", hidden: true });
  const flagsChip = h("span", { class: "pill pill-waiting room-flags", hidden: true });
  const liveDot = h("span", { class: "room-live", role: "status" });
  const retry = button("Try again", { variant: "secondary", size: "sm", hidden: true, onclick: () => { attempts = 0; open(); } });
  const keysBtn = button("Keyboard shortcuts", { variant: "tertiary", size: "sm", iconName: "keyboard", iconOnly: true, "aria-haspopup": "dialog",
    "aria-expanded": "false", onclick: () => showKeys() });
  const claude = h("span", { class: "avatar avatar-sm avatar-claude tip", "data-tip": "Claude works from Claude Code and drives this stage", "aria-label": "Claude" }, "C");
  const back = h("a", { class: "btn btn-tertiary btn-sm btn-icon tip room-back", href: "#/workspace", "aria-label": "Workspace", "data-tip": "Back to the Workspace" }, icon("grid"));
  const settingsLink = h("a", { class: "btn btn-tertiary btn-sm btn-icon tip room-settings", href: "#/settings", "aria-label": "Settings", "data-tip": "Settings: accounts, alerts, people and collaboration" }, icon("gear"));
  const tabs = VIEWS.map(([key, label]) => {
    const t = h("button", { type: "button", role: "tab", class: "tab room-tab", id: `tab-${key}`, "aria-selected": "false", "aria-controls": "main", tabindex: "-1",
      dataset: { view: key, tip: `${label} (${key === "layout" ? 1 : VIEWS.findIndex(([k]) => k === key) + 1})` } }, label);
    t.addEventListener("click", () => chooseView(key));
    return t;
  });
  const tabList = h("div", { class: "tabs room-tabs", role: "tablist", "aria-label": "Views", hidden: true }, tabs);
  tabList.addEventListener("keydown", (e) => {
    const shown = tabs.filter((t) => !t.hidden);
    const i = shown.indexOf(document.activeElement);
    const j = e.key === "ArrowRight" ? (i + 1) % shown.length : e.key === "ArrowLeft" ? (i - 1 + shown.length) % shown.length : e.key === "Home" ? 0 : e.key === "End" ? shown.length - 1 : null;
    if (j == null || i < 0) return;
    e.preventDefault(); shown[j].focus(); chooseView(shown[j].dataset.view);
  });
  const herePeople = h("span", { class: "room-here", hidden: true });
  const head = h("header", { class: "room-head" },
    h("div", { class: "room-id" }, back, title, chip, arrivePill, replacing, tabList),
    h("div", { class: "room-presence" }, herePeople, flagsChip, liveDot, retry, keysBtn, settingsLink, claude));
  const conflictBar = h("section", { class: "room-conflict", "aria-label": "Changed on two computers", hidden: true });
  const updateBar = h("div", { class: "notice notice-negative notice-update room-update", role: "alert", hidden: true });
  const strip = h("div", { class: "room-strip" });
  const cut = createCut({ onFlag: openFlag, onMediaError: mediaFailed });
  const empty = h("div", { class: "room-empty" },
    h("h2", {}, "Nothing on stage yet"),
    h("p", { class: "secondary-text" }, "Ask Claude in Claude Code to show a cut. It appears here the moment Claude puts it on stage."));
  const emptyKids = [...empty.childNodes];
  const offerBar = h("section", { class: "offer", "aria-label": "New version", tabindex: "0", hidden: true });
  const onInfo = (info) => { if (info?.title) { s.info = info; paint(); } };
  const script = createScript({ call, plain, sendFlag, onInfo, ensureSource });
  const selects = createSelects({ call, plain, sendFlag, onInfo, ensureSource });
  const layout = createLayout({ call, onInfo });
  const noVersion = h("div", { class: "room-empty room-noversion", hidden: true },
    h("h2", {}, "No version of this on stage yet"),
    h("p", { class: "secondary-text" }, "Read the script or look through the selects while Claude builds the cut. It appears here when Claude puts a version on stage."));
  const main = h("main", { class: "room-main", id: "main", tabindex: "-1", role: "tabpanel" }, empty, noVersion, cut.el, script.el, selects.el, layout.el);
  const roomEl = h("div", { class: "room" }, head, updateBar, conflictBar, strip, main, offerBar);
  root.replaceChildren(roomEl);

  // ---- read-only: a project a newer AVE saved. The banner says why; every edit control is marked disabled and does nothing.
  s.readOnly = null;
  const markEdits = () => {
    for (const el of roomEl.querySelectorAll(EDIT_CONTROLS)) {
      if (s.readOnly) { el.setAttribute("aria-disabled", "true"); if (el.matches("textarea, input")) el.readOnly = true; }
      else if (el.getAttribute("aria-disabled") === "true") { el.removeAttribute("aria-disabled"); if (el.matches("textarea, input")) el.readOnly = false; }
    }
  };
  new MutationObserver(() => { if (s.readOnly) markEdits(); }).observe(roomEl, { childList: true, subtree: true });
  sawUpdateNeeded = (why) => {
    why = why || null;
    if (why === s.readOnly) return;
    s.readOnly = why;
    roomEl.classList.toggle("is-readonly", !!why);
    updateBar.hidden = !why;
    updateBar.replaceChildren(...(why ? [icon("alert"), h("div", { class: "notice-body" },
      h("p", { class: "notice-title" }, "Update needed: this project is read-only"),
      h("p", { class: "notice-text" }, why),
      h("p", { class: "notice-text" }, "Choose AVE Studio \u203a Check for Updates\u2026, then open it again to edit."))] : []));
    markEdits();
  };
  const refuseEdit = (e) => {
    if (!s.readOnly || !e.target.closest?.(EDIT_CONTROLS)) return;
    if (e.type === "keydown" && ["Tab", "Escape"].includes(e.key)) return;
    e.preventDefault(); e.stopImmediatePropagation();
    if (e.type === "click") toast("This project is read-only until you update AVE Studio.", { key: "read-only" });
  };
  for (const ev of ["click", "pointerdown", "keydown", "beforeinput", "dragstart"]) roomEl.addEventListener(ev, refuseEdit, { capture: true });
  cut.onOutside = (t) => toast(`${fmtTime(t)} is outside the part on screen. Watch the full cut to go there.`, {
    action: "Watch the full cut", onAction: () => watchFull() });

  // ---- readiness: a version whose files have not all arrived (or arrived damaged) is never opened or played
  const notReady = () => (s.ready && s.stage && s.ready.version === s.stage.version && s.ready.artifact === s.stage.artifact
    && s.ready.status !== "ready") ? s.ready : null;
  function readyMessage(r) {
    const who = r.from || "another computer";
    if (r.status === "damaged") {
      return msg(h("span", { class: "stage-icon is-damaged", "aria-hidden": "true" }, icon("alert")),
        h("h2", {}, "This version arrived damaged"),
        h("p", { class: "secondary-text" }, `A file in it does not match what ${who} saved, so it will not open. Ask ${who} to resend it.`));
    }
    if (r.status === "stuck") {
      const resend = r.reason === "resend";
      return msg(h("span", { class: "stage-icon is-stuck", "aria-hidden": "true" }, icon("alert")),
        h("h2", {}, `Still waiting for ${who}'s files`),
        h("p", { class: "secondary-text" }, resend
          ? `A file in it has been the wrong size for ten minutes and nothing is copying it. Ask ${who} to resend it.`
          : `Nothing more has arrived for ten minutes. ${who}'s computer may be off or not syncing.`),
        resend ? null : h("a", { class: "btn btn-secondary", href: "#/workspace" }, icon("sync"), h("span", {}, "Check sync status")));
    }
    return msg(h("span", { class: "stage-icon", "aria-hidden": "true" }, icon("clock")),
      h("h2", {}, `Arriving from ${who}…`),
      h("p", { class: "secondary-text" }, r.reason === "checking"
        ? "Everything is here. The app is checking the files are whole, and opens it in a moment."
        : "Its files are still copying to this computer. It opens here by itself once everything has arrived."));
  }

  // ---- painting
  function paint() {
    const staged = !!(s.stage && s.stage.artifact);
    const set = viewSet();
    if (staged && !set.includes(s.view)) {                        // a post has no Cut, an episode no Layout: say so to the app,
      s.view = set[0];                                            // so room_read tells Claude the view the director really sees
      s.stage = { ...s.stage, view: s.view };
      call("POST", "/api/room/view", { view: s.view }).catch(() => { /* the stage moved meanwhile: its event repaints */ });
    }
    const on = !!(s.stage && s.stage.version) && s.view !== "layout";
    const view = staged ? s.view : null;
    if (!empty.dataset.failed || staged) empty.hidden = staged;
    cut.el.hidden = !(view === "cut" && on);
    noVersion.hidden = !(view === "cut" && !on);
    script.show(view === "script");
    selects.show(view === "selects");
    layout.show(view === "layout");
    const side = view === "script" || view === "layout" ? "left" : view === "selects" ? "right" : null;
    if (side) document.body.dataset.toasts = side; else delete document.body.dataset.toasts;
    if (view === "script") script.open(s.stage.artifact);
    if (view === "selects") selects.open(s.stage.artifact);
    if (view === "layout") layout.open(s.stage.artifact);
    if (view !== "cut") cut.video.pause();
    tabList.hidden = !staged;
    for (const t of tabs) t.hidden = !set.includes(t.dataset.view);
    for (const t of tabs) {
      const sel = t.dataset.view === view;
      t.setAttribute("aria-selected", String(sel));
      t.tabIndex = sel ? 0 : -1;
    }
    main.setAttribute("aria-labelledby", view ? `tab-${view}` : "");
    const v = s.stage?.version;
    const name = (s.cut?.title || (s.info?.artifact === s.stage?.artifact ? s.info?.title : null) || s.together?.name) || "This artifact";
    title.textContent = staged ? name : "The Room";
    title.title = "";
    document.title = staged ? (on ? `${name}, ${versionLabel(v)}` : name) : "The Room";
    chip.hidden = !on;
    chip.textContent = on ? versionLabel(v) : "";
    chip.title = v || "";
    const nr = on ? notReady() : null;
    arrivePill.hidden = !nr;
    arrivePill.className = `pill room-arrive is-${nr?.status || "arriving"}`;
    arrivePill.textContent = nr ? ({ damaged: "Damaged", stuck: "Stuck" }[nr.status] || "Arriving") : "";
    cut.flagButton.disabled = !!nr;                      // nothing is open to flag
    const cmp = s.offer && s.offer.version === v && s.offer.compare_to;
    replacing.hidden = !cmp;
    replacing.textContent = cmp ? `replacing ${shortVersion(cmp)}` : "";
    replacing.title = cmp || "";
    flagsChip.hidden = !s.flags;
    flagsChip.textContent = s.flags === 1 ? "1 flag waiting for your next message" : `${s.flags} flags waiting for your next message`;
    liveDot.textContent = { live: "Live", reconnecting: "Reconnecting", connecting: "Connecting", offline: "Lost the connection to the app" }[s.live];
    liveDot.dataset.state = s.live;
    retry.hidden = s.live !== "offline";
    paintTogether();
    strip.hidden = !(on && s.cut && view === "cut");
    strip.replaceChildren(renderStrip(s.cut, { trim: trimChip }));
    paintOffer();
  }

  // The trim recorded for this session, and clearing it: a person's act, like `ave trim set`, so only the app's sheet does it
  // (aveNative.clearTrim checks that it is you). A browser shows the terminal line instead.
  function trimChip(t) {
    const when = (x) => (x == null ? "the recording's own" : fmtTime(x));
    const label = `Trim ${when(t.in)} to ${when(t.out)}`;
    return h("button", { type: "button", class: `chip strip-trim ${t.mine ? "" : "chip-claude"}`, "aria-haspopup": "dialog", title: t.mine ? "Your trim" : "Claude's trim for the draft",
      onclick: (e) => {
        const anchor = e.currentTarget, app = window.aveNative?.clearTrim ? window.aveNative : null;
        const err = h("p", { class: "field-error", role: "alert" });
        const clear = button("Clear the trim", { variant: "secondary", size: "sm", iconName: "lock", disabled: !app, onclick: (ev) => busy(ev.currentTarget, async () => {
          err.textContent = "";
          try { await app.clearTrim(t.project, t.session); pop.close(); toast("Trim cleared. Ask Claude to make the assembly again from the whole recording."); if (s.cut) { s.cut.trim = null; paint(); } }
          catch (er) { err.textContent = /cancel/i.test(er.message) ? "Nothing was cleared." : er.message; }
        }) });
        const pop = popover(anchor, h("div", { class: "trim-pop" }, h("p", { class: "popover-title" }, label),
          h("p", { class: "secondary-text" }, t.mine ? "You set this trim. Everything before the start and after the end is left out of the cut."
            : "Claude's trim for the draft. Confirm it in Approvals before the final cut, or clear it here."),
          h("div", { class: "popover-actions" }, clear), err,
          app ? null : h("p", { class: "secondary-text" }, "Clearing needs the AVE Studio app. In your own terminal instead: ", h("code", {}, `ave trim clear <project folder> ${t.session}`))),
        { label: "Trim", align: "center" });
      } }, icon("film"), h("span", {}, label), t.mine ? null : h("span", { class: "sr-only" }, " (Claude's draft)"));
  }

  function mediaNow() {
    if (notReady()) return null;
    const v = s.stage?.version;
    const p = s.preview;
    if (s.offer && p && p.status === "ready" && p.version === v && s.mode === "change") return { url: p.url, start: p.start, end: p.end, kind: "change" };
    const f = s.full[v];
    if (f) return { url: f.url, start: f.start, end: f.end, kind: "full" };
    const q = s.part[v];                      // a kept or undone version keeps playing the part already rendered for it
    return q ? { url: q.url, start: q.start, end: q.end, kind: "part" } : null;
  }

  const msg = (...kids) => h("div", { class: "stage-msg" }, ...kids);
  const skel = () => h("span", { class: "skeleton stage-skel" });
  function stageMessage(m) {
    const v = s.stage?.version;
    const nr = notReady();
    if (nr && s.loading !== v) return readyMessage(nr);
    if (s.cutError) return msg(h("h2", {}, "This version could not be opened"), h("p", { class: "secondary-text" }, s.cutError));
    if (s.loading === v) return msg(skel(), h("p", { class: "secondary-text" }, `Opening ${versionLabel(v)}`));
    if (m && s.mediaError === m.url) {
      return msg(h("h2", {}, "This version could not play"),
        h("p", { class: "secondary-text" }, m.kind !== "full" ? "The preview of the change would not play. The full cut may." : "The render would not play here."),
        m.kind !== "full" ? button("Open the full cut", { variant: "primary", iconName: "film", onclick: (e) => watchFull(e.currentTarget) })
          : button("Try again", { variant: "primary", onclick: () => { s.mediaError = null; refreshMedia(); cut.reload(); } }));
    }
    if (m) return null;
    const p = s.preview;
    if (s.offer && s.offer.version === v && s.mode === "change" && (!p || p.status === "rendering"))
      return msg(skel(), h("p", {}, "Rendering the changed part"), h("p", { class: "secondary-text" }, "It plays here in a few seconds. Keep this and Go back already work."));
    if (s.rendering.has(v)) return msg(skel(), h("p", {}, "Rendering the full cut"),
      h("p", { class: "secondary-text" }, "A draft-quality render of the whole version. It plays here when it is done."));
    if (!s.full[v]) return msg(h("h2", {}, "No render of this version yet"),
      h("p", { class: "secondary-text" }, "Render a draft of the whole cut to watch it here."),
      button("Render the full cut", { variant: "primary", onclick: (e) => renderFull(e.currentTarget) }));
    return null;
  }

  function refreshMedia({ at, play = false } = {}) {
    const m = mediaNow();
    const p = s.preview;
    if (at == null && m && m.kind === "change" && cut.media?.url !== m.url) at = p.start + p.seek;
    if (s.mediaError === m?.url) { cut.setMedia(null, { at }); cut.setOverlay(stageMessage(m)); }
    else { cut.setMedia(m, { at: at ?? s.stage?.position_s ?? 0, play }); cut.setOverlay(stageMessage(m)); }
    const span = s.offer && s.offer.version === s.stage?.version ? (p?.span || s.offer.span) : null;
    cut.setChanged(span || null);
  }

  function mediaFailed(m) { s.mediaError = m.url; refreshMedia(); }

  // ---- the offer bar. Built once per offer; a preview or mode change only updates its status line and the one extra button,
  // so the director's focus stays where it is.
  const ob = {
    title: h("p", { class: "offer-title" }),
    status: h("p", { class: "offer-status" }),
    hint: h("p", { class: "offer-hint" }),
    alt: button("Watch the full cut", { variant: "tertiary", size: "sm", iconName: "film", hidden: true }),
    back: button("Go back", { "data-edit": "1", variant: "secondary", iconName: "undo", "data-tip": "Go back (B)", class: "tip", onclick: (e) => goBack(e.currentTarget) }),
    keep: button("Keep this", { "data-edit": "1", variant: "primary", iconName: "check", "data-tip": "Keep this (Y)", class: "tip", onclick: (e) => keepThis(e.currentTarget) }),
  };
  ob.alt.addEventListener("click", (e) => ob.altAction?.(e.currentTarget));
  offerBar.append(
    h("div", { class: "offer-text" }, ob.title, ob.status, ob.hint),
    h("div", { class: "offer-actions" }, h("div", { class: "offer-alt" }, ob.alt), h("span", { class: "offer-sep", "aria-hidden": "true" }), ob.back, ob.keep));
  offerBar.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target === offerBar) { e.preventDefault(); keepThis(ob.keep); } });
  let offerKey = "";

  function paintOffer() {
    const o = s.offer;
    const show = !!(o && s.stage && o.version === s.stage.version);
    offerBar.hidden = !show;
    if (!show) { offerKey = ""; return; }
    const key = JSON.stringify([o.artifact, o.version, o.compare_to, o.summary]);
    if (key !== offerKey) {
      offerKey = key;
      ob.title.textContent = o.summary || `A new version is ready: ${versionLabel(o.version)}`;
      ob.title.title = o.version;
      ob.hint.textContent = o.compare_to ? `Go back returns to ${shortVersion(o.compare_to)}.` : "Go back returns to the version before.";
    }
    const p = s.preview;
    const st = [];
    let alt = null;
    if (!p || p.status === "rendering") st.push(h("span", { class: "offer-pulse", "aria-hidden": "true" }), "Rendering the changed part");
    else if (p.status === "ready") {
      st.push(s.mode === "change" ? `Playing the change, ${fmtTime(p.span[0])} to ${fmtTime(p.span[1])}` : "Playing the full cut");
      if (p.level_approximate) st.push(h("span", { class: "offer-note", title: "The loudness of the whole cut is still being measured, so this preview's level is close but not exact" }, "Level approximate"));
      alt = s.mode === "change" ? ["Watch the full cut", (b) => watchFull(b)] : ["Watch the change", () => watchChange()];
    } else if (p.status === "too_large") {
      st.push("Change too large to preview here, open the full cut");
      alt = ["Open the full cut", (b) => watchFull(b)];
    } else if (p.status === "unchanged") st.push("Nothing a viewer would notice changed");
    else {
      st.push(h("span", {}, "The preview of this change could not be rendered"));
      alt = ["Open the full cut", (b) => watchFull(b)];
    }
    const nr = notReady();
    if (nr) { st.length = 0; alt = null; st.push(nr.status === "damaged" ? `This version arrived damaged. Ask ${nr.from || "the sender"} to resend it.`
      : nr.status === "stuck" ? `Still waiting for ${nr.from || "another computer"}'s files. Keep this works once they are here.`
      : `Arriving from ${nr.from || "another computer"}. Keep this works once it is here.`); }
    ob.keep.disabled = !!nr;
    ob.status.replaceChildren(...st);
    ob.alt.hidden = !alt;
    if (alt) { ob.alt.querySelector("span").textContent = alt[0]; ob.altAction = alt[1]; }
  }

  // ---- working together: a stage two people changed at once, and who else has this artifact open. Read from the board
  // (GET /api/workspace/room/<artifact>) when the artifact changes and on every `board` event. While the Room is open and
  // visible it says so once a minute (POST /api/workspace/presence); the app writes presence to the log at most once a minute.
  let togetherKey = "", togetherSeq = 0, togetherTimer = 0;
  async function loadTogether() {
    const a = s.stage?.artifact;
    const mine = ++togetherSeq;
    if (!a) { s.together = null; paint(); return; }
    try {
      const got = await call("GET", `/api/workspace/room/${encodeURIComponent(a)}`);
      if (mine !== togetherSeq || a !== s.stage?.artifact) return;
      s.together = { ...got, skew: (got.now || Date.now()) - Date.now() };
    } catch { if (mine === togetherSeq) s.together = null; }
    paint();
  }
  const soon = () => { clearTimeout(togetherTimer); togetherTimer = setTimeout(loadTogether, 150); };
  // Presence follows the director's work here: input (keys, clicks, scrubbing) and playback. An open Room nobody touches
  // stops saying so, and its presence lapses after ten minutes.
  const WORK_MS = 60000;
  let lastInput = Date.now();                              // opening the Room is work
  const worked = () => { lastInput = Date.now(); };
  function here() {
    const t = s.together;
    if (!t || t.key == null) return [];
    const now = Date.now() + (t.skew || 0);
    return (t.presence || []).filter((p) => p.until > now);
  }
  function paintTogether() {
    const people = here();
    herePeople.hidden = !people.length;
    const words = people.map((p) => p.text).join(", ");
    if (herePeople.dataset.words !== words) {
      herePeople.dataset.words = words;
      herePeople.replaceChildren(...(people.length ? [h("span", { class: "here tip", "data-tip": words, role: "img", "aria-label": words },
        avatar(people[0].person, { live: true }), h("span", { class: "here-text" }, people[0].text),
        people.length > 1 ? h("span", { class: "here-more" }, `+${people.length - 1}`) : null)] : []));
    }
    const c = s.together?.conflict;
    const key = c ? JSON.stringify([c.id, c.text, c.detail]) : "";
    conflictBar.hidden = !c;
    if (key === togetherKey) return;
    togetherKey = key;
    if (!c) { conflictBar.replaceChildren(); return; }
    const choices = (c.choices || []).filter((ch) => ch.stage !== "Out");
    conflictBar.replaceChildren(h("div", { class: "notice", role: "status" }, h("span", { class: "notice-icon", "aria-hidden": "true" }, icon("people")),
      h("div", { class: "notice-body" },
        h("p", { class: "notice-title" }, c.text),
        h("p", { class: "notice-text" }, c.detail),
        h("div", { class: "notice-actions" }, choices.map((ch, i) => button(`Keep ${ch.stage}`, { variant: "secondary", size: "sm",
          title: ch.label, onclick: (e) => settleStage(ch, e.currentTarget) }))))));
  }
  async function settleStage(ch, btn) {
    const t = s.together;
    if (!t?.key) return;
    try {
      await busy(btn, () => call("PATCH", `/api/workspace/artifact/${encodeURIComponent(t.key)}`, { stage: ch.stage, from: t.stage }));
      toast(`Settled on ${ch.stage}, as ${ch.who} set it`);
      loadTogether();
    } catch (e) {
      toast(e.status === 409 ? "The stage changed a moment ago. Look again and choose." : plain(e, "settle the stage"), { tone: "negative" });
      loadTogether();
    }
  }
  let lastTouch = "";
  function touch(force = false) {
    const a = s.stage?.artifact;
    if (!a || document.visibilityState === "hidden") return;
    const where = `${a}\u0000${s.view}`;
    if (!force && where === lastTouch) return;
    lastTouch = where;
    call("POST", "/api/workspace/presence", { artifact: a, view: s.view })
      .then((got) => { if (a === s.stage?.artifact) { s.together = { ...got, skew: (got.now || Date.now()) - Date.now() }; paint(); } })
      .catch(() => { /* presence is a courtesy: never an error for the director */ });
  }
  const beat = setInterval(() => {
    const playing = [...root.querySelectorAll("video")].some((v) => !v.paused && !v.ended);
    if (playing || Date.now() - lastInput < WORK_MS) touch(true);
    paintTogether();
  }, WORK_MS);
  const togetherLife = new AbortController();
  for (const ev of ["pointerdown", "keydown", "wheel"]) root.addEventListener(ev, worked, { signal: togetherLife.signal, passive: true });

  // ---- actions
  const focusPlay = () => requestAnimationFrame(() => (cut.playButton.disabled ? main.querySelector(".tl-ruler") : cut.playButton)?.focus());

  async function keepThis(btn) {
    if (s.readOnly) return;                                       // a newer AVE saved this project
    const v = s.stage?.version;
    if (!v || offerBar.hidden || notReady()) return;
    try {
      await busy(btn, () => call("POST", "/api/room/keep", { version: v }));
      toast(`Kept ${shortVersion(v)}. Claude hears about it with your next message.`);
      focusPlay();
    } catch (e) { toast(plain(e, "keep this version"), { tone: "negative" }); }
  }

  async function goBack(btn) {
    if (s.readOnly) return;                                       // a newer AVE saved this project
    const v = s.stage?.version;
    if (!v || offerBar.hidden) return;
    try {
      const r = await busy(btn, () => call("POST", "/api/room/back", { version: v }));
      const { version: to, from, artifact: a } = r.click;
      toast(`Back on ${shortVersion(to)}`, { action: "Undo", onAction: () => call("POST", "/api/room/offer", { artifact: a, version: from, compare_to: to })
        .catch((e) => toast(plain(e, "undo"), { tone: "negative" })) });
      focusPlay();
    } catch (e) {
      toast(e.status === 409 ? "There is no earlier version to go back to." : plain(e, "go back"), { tone: "negative" });
    }
  }

  async function renderFull(btn) {
    const v = s.stage.version;
    if (notReady()) return;
    const go = () => call("POST", "/api/room/render", { artifact: s.stage.artifact, version: v });
    try {
      await (btn ? busy(btn, go) : go());
      s.rendering.add(v);
      refreshMedia();
    } catch (e) { toast(plain(e, "render the full cut"), { tone: "negative" }); }
  }

  function watchFull(btn) {
    s.mode = "full";
    const v = s.stage?.version;
    if (!s.full[v] && !s.rendering.has(v)) renderFull(btn);
    refreshMedia({ at: cut.cutTime() });
    paintOffer();
  }
  function watchChange() { s.mode = "change"; refreshMedia({ play: true }); paintOffer(); }

  // ---- review copies of the recordings: made once, on first need; `review` events report progress and the result
  const reviews = new Map();
  // A copy that says nothing for reviewQuietMs() (no progress, no waiting turn) is given up on, with Try again; a reconnect
  // asks the server again for every copy still pending, and the state read carries each job's last word.
  const reviewQuietMs = () => window.__aveReviewQuietMs || 90000;        // read each time: a test can shorten it, then restore it
  function ensureSource(artifact, key, onProgress) {
    const id = `${artifact}\u0000${key}`;
    let r = reviews.get(id);
    if (r?.status === "ready") return Promise.resolve(r.url);
    if (!r || r.status === "failed") {
      r = { artifact, key, status: "rendering", waiters: [], listeners: [], timer: 0 };
      reviews.set(id, r);
      ask(id);
    }
    if (onProgress) r.listeners.push(onProgress);
    return new Promise((ok, no) => r.waiters.push([ok, no]));
  }
  function ask(id) {
    const r = reviews.get(id); if (!r) return;
    hush(id);
    call("POST", "/api/room/review", { artifact: r.artifact, key: r.key }).then((got) => settle(id, got))
      .catch((e) => settle(id, { status: "failed", message: e.network ? "The app is not answering." : "The copy could not be started." }));
  }
  function hush(id) {
    const r = reviews.get(id); if (!r) return;
    clearTimeout(r.timer);
    r.timer = setTimeout(() => settle(id, { status: "failed", message: "The copy stopped making progress." }), reviewQuietMs());
  }
  function settle(id, d) {
    const r = reviews.get(id); if (!r || r.status !== "rendering") return;
    if (d.status === "waiting") {                      // in line behind another copy: not a stall, so no timer while it waits
      clearTimeout(r.timer);
      for (const fn of r.listeners) fn(-Math.max(1, d.ahead || 1));
      return;
    }
    if (d.status === "rendering" || d.status === "already rendering") {
      hush(id);
      for (const fn of r.listeners) fn(d.progress || 0);
      return;
    }
    clearTimeout(r.timer);
    r.status = d.status === "ready" ? "ready" : "failed"; r.url = d.url;
    for (const [ok, no] of r.waiters.splice(0)) r.status === "ready" ? ok(d.url) : no(new Error((d.message || "The copy could not be made").replace(/\.?$/, ".")));
    r.listeners = [];
  }
  function resumeReviews(jobs) {
    const known = new Map((jobs || []).map((j) => [`${j.artifact}\u0000${j.key}`, j]));
    for (const [id, r] of reviews) {
      if (r.status !== "rendering") continue;
      const j = known.get(id);
      if (j && j.status !== "rendering" && j.status !== "waiting") settle(id, j);
      else ask(id);                                        // the server restarted, or the event was missed: ask again
    }
  }

  // ---- flags: every view posts the same flag; each shows its own until Claude reads it
  async function sendFlag(flag) {
    try {
      const got = await call("POST", "/api/room/flag", flag);
      addFlag({ ...flag, id: got.id, status: "waiting" });
      return got;
    } catch (e) { throw e; }
  }
  function addFlag(f) {
    if (f.id == null || s.myFlags.has(f.id)) return;
    s.myFlags.set(f.id, { ...f, status: f.status || "waiting" });
    shareFlags();
  }
  function shareFlags() { const list = [...s.myFlags.values()]; script.setFlags(list); selects.setFlags(list); }

  function viewSet() {
    const design = s.stage?.view === "layout" || DESIGN_KINDS.includes(s.together?.kind);
    return design ? ["layout"] : VIDEO_VIEWS;
  }

  function chooseView(view) {
    view = viewOf(view);
    if (!s.stage || view === s.view || !viewSet().includes(view)) return;
    s.view = view;
    s.stage = { ...s.stage, view };
    worked();
    touch();
    paint();
    if (view === "cut") refreshMedia();
    call("POST", "/api/room/view", { view }).catch((e) => toast(plain(e, "tell Claude which view you are on"), { tone: "negative" }));
  }

  function openFlag(anchor, t) {
    if (s.readOnly) return;                                       // a newer AVE saved this project
    if (!s.stage) return;
    const start = Math.max(0, t - 3), end = t;
    const note = h("input", { class: "input", id: "flag-note", maxlength: "500", "aria-label": "Note for Claude", placeholder: "Too long here, try the wide shot" });
    const send = button("Flag for Claude", { variant: "primary", size: "sm" });
    const cancel = button("Cancel", { variant: "tertiary", size: "sm" });
    const form = h("form", { class: "flag-form" },
      h("p", { class: "flag-head" }, h("span", { class: "popover-title" }, `Flag ${fmtTime(start)} to ${fmtTime(end)}`),
        h("span", { class: "flag-hint secondary-text" }, "Claude reads it with your next message.")),
      h("div", { class: "flag-row" }, note, cancel, send));
    // below the button, right-aligned to it: over the timeline, never over the picture
    const pop = popover(anchor, form, { place: "below", align: "end", modal: true, wide: true, label: "Flag this moment for Claude" });
    cancel.addEventListener("click", () => pop.close());
    form.addEventListener("submit", (e) => e.preventDefault());
    note.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); send.click(); } });
    send.addEventListener("click", async () => {
      try {
        await busy(send, () => call("POST", "/api/room/flag", { artifact: s.stage.artifact, start, end, note: note.value.trim() }));
        pop.close();
        toast(`Flagged ${fmtTime(start)} to ${fmtTime(end)} for Claude`);
      } catch (e) { toast(plain(e, "flag this moment"), { tone: "negative" }); }
    });
    note.focus();
  }

  function showKeys() {
    const list = h("dl", { class: "keys" }, SHORTCUTS.map(([k, what]) => [h("dt", {}, h("kbd", {}, k)), h("dd", {}, what)]));
    const close = button("Close", { variant: "secondary", size: "sm" });
    const pop = popover(keysBtn, h("div", { class: "keys-pop" }, h("p", { class: "popover-title" }, "Keyboard shortcuts"), list,
      h("p", { class: "secondary-text keys-note" }, "They work when you are not typing in a field."), h("div", { class: "popover-actions" }, close)),
      { place: "below", align: "end", modal: true, label: "Keyboard shortcuts" });
    close.addEventListener("click", () => pop.close());
    close.focus();
  }

  // ---- loading a version
  let loadSeq = 0;
  async function loadCut(at, play) {
    const v = s.stage.version, a = s.stage.artifact || s.artifact;
    const mine = ++loadSeq;
    s.loading = v; s.cutError = null; s.mediaError = null;
    refreshMedia({ at });
    try {
      const info = await call("GET", `/api/room/cut?artifact=${encodeURIComponent(a)}&version=${encodeURIComponent(v)}`);
      if (mine !== loadSeq) return;
      const r = info.readiness;
      s.ready = r && r.status !== "ready" ? { ...r, artifact: a, version: v } : null;
      if (s.ready) { s.cut = null; if (info.title) s.info = { artifact: a, title: info.title }; }
      else {
        s.cut = info;
        if (info.media) s.full[v] = info.media;
      }
    } catch (e) {
      if (mine !== loadSeq) return;
      s.cut = null;
      s.cutError = e.network ? "The app is not answering: check that AVE Studio is still open."
        : e.status === 404 ? "Claude put a version on stage that is not in this project. Ask Claude which version it meant."
        : "The app could not read this version.";
    }
    s.loading = null;
    cut.setCut(s.cut);
    refreshMedia({ at, play });
    paint();
  }

  function applyStage(data) {
    const prev = s.stage;
    s.stage = { ...(prev || {}), ...data, artifact: data.artifact || prev?.artifact || s.artifact };
    s.artifact = s.stage.artifact;
    if (data.view) s.view = viewOf(data.view);
    if (prev?.artifact !== s.stage.artifact) { s.cut = null; s.info = null; s.together = null; loadTogether(); }
    if (Date.now() - lastInput < WORK_MS) touch();
    if ("highlights" in data || prev?.artifact !== s.stage.artifact) script.setHighlights(s.stage.highlights || []);
    if (data.cause === "show" && (s.view === "script" || s.view === "selects")) (s.view === "script" ? script : selects).open(s.stage.artifact, { force: true });
    if (s.view === "script" && data.position_s != null && s.stage.time_base === "transcript"   // never cue cut seconds in the script
        && (data.position_s !== prev?.position_s || prev?.view !== "script" || prev?.artifact !== s.stage.artifact || data.cause === "show")) {
      script.open(s.stage.artifact);
      script.cue(data.position_s);
    }
    if (s.offer && (s.offer.version !== s.stage.version || s.offer.artifact !== s.stage.artifact)) { s.offer = null; s.preview = null; }
    if (!s.stage.version) { paint(); return; }
    if (!prev || prev.version !== s.stage.version || prev.artifact !== s.stage.artifact) {
      s.mode = "change";
      loadCut(s.stage.position_s ?? 0);
    } else if (data.position_s != null && data.position_s !== prev.position_s && s.view === "cut") {
      cut.seekOrSay(data.position_s);
    }
    paint();
  }

  function applySnapshot(snap) {
    s.seq = snap.seq || 0;
    s.offer = snap.offer; s.preview = snap.preview; s.flags = snap.flags_waiting || 0;
    for (const f of snap.flags_read || []) s.myFlags.set(f.id, { ...f, status: "read" });
    for (const f of snap.flags || []) s.myFlags.set(f.id, { ...f, status: "waiting" });
    shareFlags();
    if (snap.preview?.status === "ready") s.part[snap.preview.version] = snap.preview;
    resumeReviews(snap.reviews);
    s.stage = null;
    if (snap.stage) applyStage(snap.stage); else paint();
  }

  // ---- the stream
  const handlers = {
    stage: (d) => applyStage(d),
    offer: (d) => { s.offer = d; s.preview = null; s.mode = "change"; refreshMedia(); paint(); },
    preview: (d) => {
      if (!s.offer || d.version !== s.offer.version) return;
      s.preview = d;
      if (d.status === "ready") s.part[d.version] = d;
      refreshMedia({ play: d.status === "ready" });
      paint();
    },
    click: (d) => {
      if (d.action === "keep" || d.action === "back") { s.offer = null; s.preview = null; }
      if (d.via === "chat" && d.action === "keep") toast(`Kept in chat: ${shortVersion(d.version)}`);
      else if (d.via === "chat" && d.action === "back") toast(`Back in chat: ${shortVersion(d.version)}`);
      paint(); refreshMedia();
    },
    flag: (d) => { s.flags += 1; addFlag({ ...d, status: "waiting" }); paint(); },
    read: (d) => {
      const before = s.flags; s.flags = d.flags_waiting || 0;
      for (const f of s.myFlags.values()) if ((d.ids || []).includes(f.id) || (d.through != null && f.id <= d.through)) f.status = "read";
      shareFlags();
      paint();
      if (before && !s.flags) toast("Claude read your flags");
    },
    readiness: (d) => {
      const v = s.stage?.version, a = s.stage?.artifact;
      if (!v) return;
      const it = (d.items || []).find((x) => x.version === v && (x.room === a || x.artifact === a));
      if (!it) return;
      const was = notReady();
      if (it.status === "ready") {
        if (was) { s.ready = null; loadCut(s.stage.position_s ?? 0); toast(`${versionLabel(v)} has arrived`); }
        return;
      }
      s.ready = { status: it.status, reason: it.reason, from: it.from, text: it.text, artifact: a, version: v };
      if (!was) { s.cut = null; cut.setCut(null); }
      refreshMedia(); paint();
    },
    selects: (d) => selects.applyDecision(d),
    board: () => soon(),
    files: (d) => { if (d.artifact === s.stage?.artifact) (d.view === "script" ? script : selects).open(d.artifact, { force: true, keep: true }); },
    post: (d) => { if (d.artifact === s.stage?.artifact) layout.onPost(d); },
    review: (d) => settle(`${d.artifact}\u0000${d.key}`, d),
    media: (d) => {
      if (d.status === "rendering") s.rendering.add(d.version);
      else {
        s.rendering.delete(d.version);
        if (d.status === "ready") { s.full[d.version] = d; if (s.mediaError) s.mediaError = null; }
        else toast("The full cut could not be rendered. Ask Claude to look at the render.", { tone: "negative" });
      }
      if (d.version === s.stage?.version) refreshMedia({ play: d.status === "ready" && s.mode === "full" });
    },
  };

  let es = null, attempts = 0, timer = 0;
  function connect() {
    es?.close();
    es = new EventSource(`/api/room/stream?room=1&last_event_id=${s.seq}`);
    es.onopen = () => { attempts = 0; s.live = "live"; paint(); };
    es.onerror = () => {
      if (es.readyState !== EventSource.CLOSED) { s.live = "reconnecting"; paint(); return; }    // the browser is retrying itself
      es.close();
      attempts += 1;
      if (attempts > RETRIES) { s.live = "offline"; paint(); return; }
      s.live = "reconnecting"; paint();
      clearTimeout(timer);
      timer = setTimeout(open, 1000 * 2 ** (attempts - 1));          // 1 s, 2 s, 4 s, then a Try again button
    };
    es.onmessage = (m) => {
      let e;
      try { e = JSON.parse(m.data); } catch { return; }
      if (e.type === "resync") { open(); return; }                  // the app restarted or we fell behind: start from its state
      if (e.seq && e.seq <= s.seq) return;                          // already reflected in the state we loaded (a replay)
      if (e.seq) s.seq = e.seq;
      if (probe) { probe.events += 1; probe.last = e; }
      handlers[e.type]?.(e.data || {});
    };
  }

  // read the state, then follow the stream from it; the same path serves a first start, a resync and Try again
  function open(btn) {
    const go = call("GET", "/api/room/state")
      .then((snap) => { if (empty.dataset.failed) { empty.replaceChildren(...emptyKids); delete empty.dataset.failed; } applySnapshot(snap); connect(); })
      .catch((e) => {
        es?.close();
        attempts += 1;
        if (attempts <= RETRIES && s.live !== "offline") { s.live = "reconnecting"; paint(); clearTimeout(timer); timer = setTimeout(open, 1000 * 2 ** (attempts - 1)); return; }
        s.live = "offline"; paint();
        if (!s.stage) {
          empty.dataset.failed = "1";
          empty.replaceChildren(h("h2", {}, "The Room could not reach the app"),
            h("p", { class: "secondary-text" }, plain(e, "open the Room")),
            button("Try again", { variant: "primary", onclick: (ev) => { attempts = 0; open(ev.currentTarget); } }));
          empty.hidden = false; cut.el.hidden = true;
        }
      });
    return btn ? busy(btn, () => go) : go;
  }

  // ---- keyboard (never while typing or with a dialog open)
  const life = new AbortController();
  document.addEventListener("keydown", (e) => {
    if (typing(e) || e.metaKey || e.ctrlKey || e.altKey || document.querySelector(".popover")) return;
    const onControl = e.target.closest?.("button, [role=slider]");
    const k = e.key;
    const view = s.stage ? s.view : null;
    const player = view === "script" ? script.viewer : view === "selects" ? selects.viewer : view === "layout" ? null : cut;
    const set = viewSet();
    if (s.stage && ["1", "2", "3"].includes(k)) { e.preventDefault(); if (set[Number(k) - 1]) chooseView(set[Number(k) - 1]); }
    else if (view === "layout" && k !== "?") return;               // nothing plays here; the composer's own keys are its controls
    else if ((k === " " && !onControl) || k === "k") { e.preventDefault(); player.toggle(); }
    else if (k === "j") player.nudge(-5);
    else if (k === "l") player.nudge(5);
    else if (k === "/" && (view === "script" || view === "selects")) { e.preventDefault(); (view === "script" ? script : selects).focusSearch(); }
    else if (k === "n" && view === "script") { e.preventDefault(); script.next(); }
    else if (k === "N" && view === "script") { e.preventDefault(); script.prev(); }
    else if (s.readOnly && ["f", "y", "b"].includes(k)) { e.preventDefault(); toast("This project is read-only until you update AVE Studio.", { key: "read-only" }); }
    else if (k === "f" && view === "script") { e.preventDefault(); script.flagKey(); }
    else if (k === "f" && view === "selects") { e.preventDefault(); selects.flagKey(); }
    else if (k === "f" && s.stage && s.stage.version && !notReady()) { e.preventDefault(); openFlag(cut.flagButton, cut.cutTime()); }
    else if (k === "y" && !offerBar.hidden) { e.preventDefault(); keepThis(ob.keep); }
    else if (k === "b" && !offerBar.hidden) { e.preventDefault(); goBack(ob.back); }
    else if (k === "?") { e.preventDefault(); showKeys(); }
  }, { signal: life.signal });

  paint();
  open();
  return { state: s, close: () => { es?.close(); clearTimeout(timer); clearInterval(beat); clearTimeout(togetherTimer); togetherLife.abort(); life.abort(); script.close(); selects.close(); layout.close(); } };
}
