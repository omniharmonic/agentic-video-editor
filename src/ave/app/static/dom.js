// Small DOM helpers for the app. No framework: elements are built with h(), text always goes in as text nodes (never innerHTML).

const SVG_TAGS = new Set(["svg", "path", "text", "circle", "g"]);

export function h(tag, props = {}, ...kids) {
  const el = SVG_TAGS.has(tag) ? document.createElementNS("http://www.w3.org/2000/svg", tag) : document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === false || v == null) continue;
    if (k === "class") el.setAttribute("class", v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k === "style" && typeof v === "object") for (const [p, val] of Object.entries(v)) el.style.setProperty(p, val);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, String(v));
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
export const clear = (el) => { el.replaceChildren(); return el; };

// 16 px line icons on a 16 grid, tinted by currentColor.
const ICONS = {
  play: ["M5 3.2v9.6L12.8 8z", true],
  pause: ["M4.5 3h2.5v10H4.5zM9 3h2.5v10H9z", true],
  rewind: ["M7.5 3.5v9L1.5 8zM14.5 3.5v9L8.5 8z", true],
  fastforward: ["M1.5 3.5v9l6-4.5zM8.5 3.5v9l6-4.5z", true],
  volume: ["M2.5 6h2.5l3.5-3v10L5 10H2.5zM11 5.5a3.5 3.5 0 0 1 0 5M12.8 3.8a6 6 0 0 1 0 8.4"],
  mute: ["M2.5 6h2.5l3.5-3v10L5 10H2.5zM11 6l3.5 4M14.5 6L11 10"],
  flag: ["M4 14V2.5M4 3h7.5l-1.5 2.5 1.5 2.5H4"],
  check: ["M3 8.5l3.2 3.2L13 4.8"],
  undo: ["M5.5 3.5L2.5 6.5l3 3M2.5 6.5H10a3.5 3.5 0 0 1 0 7H7"],
  chevron: ["M4 6l4 4 4-4"],
  x: ["M4 4l8 8M12 4l-8 8"],
  film: ["M2 3h12v10H2zM5 3v10M11 3v10M2 6h3M2 10h3M11 6h3M11 10h3"],
  info: ["M8 1.8a6.2 6.2 0 1 0 0 12.4A6.2 6.2 0 0 0 8 1.8zM8 7.2v4M8 5v.1"],
  star: ["M8 2l1.8 3.8 4.2.5-3.1 2.9.8 4.1L8 11.3l-3.7 2 .8-4.1L2 6.3l4.2-.5z"],
  plus: ["M8 3v10M3 8h10"],
  search: ["M7 2.5a4.5 4.5 0 1 0 0 9 4.5 4.5 0 0 0 0-9zM10.3 10.3L13.5 13.5"],
  arrive: ["M2 8h7M6 4.5L9.5 8 6 11.5M12.5 3v10"],
  send: ["M3.5 8h7M7 4.5L10.5 8 7 11.5M13 3v10"],
  keyboard: ["M1.5 4h13v8h-13zM4 6.5h.01M6.5 6.5h.01M9 6.5h.01M11.5 6.5h.01M5 9.5h6"],
  sun: ["M8 5a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM8 1v1.5M8 13.5V15M1 8h1.5M13.5 8H15M3 3l1 1M12 12l1 1M3 13l1-1M12 4l1-1"],
  moon: ["M13 9.5A5.5 5.5 0 0 1 6.5 3a5.5 5.5 0 1 0 6.5 6.5z"],
  home: ["M2.5 7.5L8 3l5.5 4.5M4 6.5V13h3v-3.5h2V13h3V6.5"],
  invite: ["M6.5 2.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5zM2 13.5c0-2.5 2-4.2 4.5-4.2s4.5 1.7 4.5 4.2M12.5 4.5v4M10.5 6.5h4"],
  person: ["M8 2.5a2.75 2.75 0 1 0 0 5.5 2.75 2.75 0 0 0 0-5.5zM2.5 13.5c.6-2.6 2.8-4 5.5-4s4.9 1.4 5.5 4"],
  filter: ["M2 3.5h12L9.5 9v4l-3 1.5V9z"],
  sort: ["M5 2.5v11M2.5 11L5 13.5 7.5 11M11 13.5v-11M8.5 5L11 2.5 13.5 5"],
  hide: ["M2 8s2.2-4.5 6-4.5S14 8 14 8s-2.2 4.5-6 4.5S2 8 2 8zM8 6a2 2 0 1 0 0 4 2 2 0 0 0 0-4zM2.5 2.5l11 11"],
  group: ["M2.5 3h11v3.5h-11zM2.5 9.5h11V13h-11z"],
  dots: ["M2.5 7h2v2h-2zM7 7h2v2H7zM11.5 7h2v2h-2z", true],
  grip: ["M5 3h2v2H5zM9 3h2v2H9zM5 7h2v2H5zM9 7h2v2H9zM5 11h2v2H5zM9 11h2v2H9z", true],
  open: ["M9.5 2.5h4v4M13.5 2.5L8.5 7.5M6.5 3.5h-3v9h9v-3"],
  bubble: ["M2.5 3.5h11v7.5H7l-3 2.5v-2.5H2.5z"],
  "bubble-plus": ["M2.5 3.5h11v7.5H7l-3 2.5v-2.5H2.5zM8 5.3v4M6 7.3h4"],
  calendar: ["M2.5 4h11v9.5h-11zM2.5 7h11M5.5 2.5v3M10.5 2.5v3"],
  kanban: ["M2.5 2.5h3v11h-3zM6.5 2.5h3v7h-3zM10.5 2.5h3v9h-3z"],
  table: ["M2 3h12v10H2zM2 6.5h12M2 10h12M6 6.5V13"],
  folder: ["M2 4h4l1.5 1.5H14v7.5H2z"],
  archive: ["M2 3h12v3H2zM3 6v7h10V6M6.5 8.5h3"],
  right: ["M6 4l4 4-4 4"],
  lock: ["M4 7.5h8v6H4zM5.5 7.5V5.5a2.5 2.5 0 0 1 5 0v2"],
  sidebar: ["M2 3h12v10H2zM6 3v10"],
  inbox: ["M3 9.5h3l1 2h2l1-2h3M3 9.5L4.5 3.5h7L13 9.5v3.5H3z"],
  grid: ["M2.5 2.5h4.5v4.5H2.5zM9 2.5h4.5v4.5H9zM2.5 9h4.5v4.5H2.5zM9 9h4.5v4.5H9z"],
  "star-fill": ["M8 2l1.8 3.8 4.2.5-3.1 2.9.8 4.1L8 11.3l-3.7 2 .8-4.1L2 6.3l4.2-.5z", true],
  sync: ["M12.8 6.2A5 5 0 0 0 3.5 5.2M3.2 9.8a5 5 0 0 0 9.3 1M3.2 2.6v2.8H6M12.8 13.4v-2.8H10"],
  spark: ["M8 1.8l1.5 4.7 4.7 1.5-4.7 1.5L8 14.2 6.5 9.5 1.8 8l4.7-1.5z"],
  clock: ["M8 2.2a5.8 5.8 0 1 0 0 11.6A5.8 5.8 0 0 0 8 2.2zM8 4.8V8l2.3 1.5"],
  alert: ["M8 2.3l6 10.7H2zM8 6.5v3.2M8 11.4v.01"],
  gear: ["M8 5.6a2.4 2.4 0 1 0 0 4.8 2.4 2.4 0 0 0 0-4.8zM6.9 1.8h2.2l.4 1.7 1.3.7 1.6-.6 1.1 1.9-1.3 1.2v1.6l1.3 1.2-1.1 1.9-1.6-.6-1.3.7-.4 1.7H6.9l-.4-1.7-1.3-.7-1.6.6-1.1-1.9 1.3-1.2V7.1L2.5 5.9l1.1-1.9 1.6.6 1.3-.7z"],
  people: ["M5.5 3a2.2 2.2 0 1 0 0 4.4A2.2 2.2 0 0 0 5.5 3zM1.5 12.8c.4-2.2 2-3.5 4-3.5s3.6 1.3 4 3.5M10.6 3.2a2.1 2.1 0 1 1 0 4.2M11.6 9.4c1.5.3 2.6 1.5 2.9 3.4"],
};

export function icon(name, cls = "") {
  const [d, fill] = ICONS[name] || ICONS.info;
  return h("svg", { class: `icon ${fill ? "icon-fill" : ""} ${cls}`.replace(/\s+/g, " ").trim(), viewBox: "0 0 16 16", "aria-hidden": "true", focusable: "false" },
    h("path", { d }));
}

// A button: label text, optional leading icon; `iconOnly` keeps the label as the accessible name and shows it as a tooltip.
// `text` shows a short visible text instead of the label (the label stays the accessible name), e.g. "5s" for "Back 5 seconds".
export function button(label, { variant = "secondary", size, iconName, iconOnly = false, text, onclick, ...rest } = {}) {
  const cls = ["btn", `btn-${variant}`, size === "sm" ? "btn-sm" : "", iconOnly ? "btn-icon tip" : "", text ? "tip" : "", rest.class || ""].filter(Boolean).join(" ");
  delete rest.class;
  const named = iconOnly || text ? { "aria-label": label, "data-tip": rest["data-tip"] || label } : {};
  return h("button", { type: "button", class: cls, onclick, ...named, ...rest },
    iconName ? icon(iconName) : null, iconOnly ? null : h("span", {}, text || label));
}

// Run an async action with the button showing it is working; it never sits there silently.
export async function busy(btn, fn) {
  btn.setAttribute("aria-busy", "true");
  btn.disabled = true;
  try { return await fn(); }
  finally { btn.removeAttribute("aria-busy"); btn.disabled = false; }
}

// ---- toasts: one region, newest at the bottom, each with an optional action and a close button. At most two show: a newer one
// pushes the oldest out, and one with the same `key` (the same kind of action) replaces the last of its kind.
// A toast waits while the pointer or focus is on it, so nobody loses an Undo mid-read.
const MAX_TOASTS = 2;
let toastRegion;
export function toast(text, { action, onAction, tone, timeout = 6000, title, key } = {}) {
  if (!toastRegion || !toastRegion.isConnected) {
    toastRegion = h("div", { class: "toasts", role: "status", "aria-live": "polite" });
    document.body.append(toastRegion);
  }
  let timer = 0;
  const close = () => { clearTimeout(timer); el.remove(); };
  const arm = () => { clearTimeout(timer); if (timeout) timer = setTimeout(close, timeout); };
  const el = h("div", { class: `toast ${tone ? "toast-" + tone : ""}`, title },
    h("span", { class: "toast-text" }, text),
    action ? button(action, { variant: "secondary", size: "sm", class: "toast-action", onclick: () => { close(); onAction?.(); } }) : null,
    button("Close", { variant: "tertiary", size: "sm", iconName: "x", iconOnly: true, onclick: close }));
  for (const ev of ["pointerenter", "focusin"]) el.addEventListener(ev, () => clearTimeout(timer));
  el.addEventListener("pointerleave", () => { if (!el.contains(document.activeElement)) arm(); });
  el.addEventListener("focusout", (e) => { if (!el.contains(e.relatedTarget) && !el.matches(":hover")) arm(); });
  if (key) { el.dataset.key = key; for (const old of toastRegion.querySelectorAll(".toast")) if (old.dataset.key === key) old.remove(); }
  toastRegion.append(el);
  const all = toastRegion.querySelectorAll(".toast");
  for (let i = 0; i < all.length - MAX_TOASTS; i++) all[i].remove();
  arm();
  el.close = close;
  return el;
}

// ---- popover anchored to a control. `place`: "below" | "above"; `align`: "start" | "end" (edge of the control it lines up with).
// `modal`: a dialog that keeps Tab inside it. Esc and an outside click close it and return focus to the control.
export function popover(anchor, content, { place = "below", align = "start", modal = false, label, onClose, wide = false } = {}) {
  const pop = h("div", { class: `popover ${wide ? "popover-wide" : ""}`.trim(), role: "dialog", "aria-modal": modal ? "true" : null, "aria-label": label }, content);
  document.body.append(pop);
  const r = anchor.getBoundingClientRect();
  const w = pop.offsetWidth, ht = pop.offsetHeight;
  const x = align === "end" ? r.right - w : r.left;
  const left = Math.max(8, Math.min(window.innerWidth - w - 8, x));
  let top = place === "above" ? r.top - ht - 8 : r.bottom + 8;
  top = Math.max(8, Math.min(window.innerHeight - ht - 8, top));
  Object.assign(pop.style, { left: `${left + window.scrollX}px`, top: `${top + window.scrollY}px` });
  pop.style.setProperty("--origin", `${place === "above" ? "bottom" : "top"} ${align === "end" ? "right" : "left"}`);
  anchor.setAttribute("aria-expanded", "true");
  const close = (refocus = true) => {
    if (!pop.isConnected) return;
    pop.remove();
    anchor.setAttribute("aria-expanded", "false");
    document.removeEventListener("keydown", onKey, true);
    document.removeEventListener("pointerdown", onDown, true);
    onClose?.();
    if (refocus) anchor.focus();
  };
  const focusables = () => [...pop.querySelectorAll("button, textarea, input, select, [tabindex]:not([tabindex='-1'])")].filter((e) => !e.disabled);
  const onKey = (e) => {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(); return; }
    if (modal && e.key === "Tab") {
      const f = focusables(); if (!f.length) return;
      const i = f.indexOf(document.activeElement);
      if (e.shiftKey && (i <= 0)) { e.preventDefault(); f[f.length - 1].focus(); }
      else if (!e.shiftKey && (i === f.length - 1 || i === -1)) { e.preventDefault(); f[0].focus(); }
    }
  };
  const onDown = (e) => { if (!pop.contains(e.target) && !anchor.contains(e.target)) close(false); };
  document.addEventListener("keydown", onKey, true);
  setTimeout(() => document.addEventListener("pointerdown", onDown, true));
  return { el: pop, close };
}

export function fmtTime(s) {
  if (!Number.isFinite(s) || s < 0) s = 0;
  const m = Math.floor(s / 60), sec = s - m * 60;
  return `${m}:${sec.toFixed(1).padStart(4, "0")}`;
}

export function typing(e) {
  const t = e.target;
  return t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
}

// ---- words people read. Internal names (rough.s2.v8, camA.mp4) never reach the screen; they stay in title attributes.
const STAGE_WORDS = { assembly: "Assembly", rough: "Rough cut", final: "Final cut", select: "Selects", short: "Short" };
const VERSION = /^([a-z]+)(?:\.([A-Za-z0-9_-]+))?\.v(\d+)(?:~[a-z0-9]{4})?$/;

export function versionLabel(v) {
  const m = VERSION.exec(v || "");
  if (!m) return "this version";
  const kind = STAGE_WORDS[m[1]] || "Cut";
  const part = m[2] ? (/^s\d+$/.test(m[2]) ? `, session ${m[2].slice(1)}` : `, ${m[2].replace(/[-_]/g, " ")}`) : "";
  return `${kind}${part}, version ${m[3]}`;
}
export const versionNumber = (v) => (VERSION.exec(v || "") || [])[3] || "";
export const shortVersion = (v) => (versionNumber(v) ? `version ${versionNumber(v)}` : "this version");
