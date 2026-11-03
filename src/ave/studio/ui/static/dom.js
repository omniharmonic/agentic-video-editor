// Small DOM helpers shared by every studio view. No framework: elements are built with h(), text goes in as text nodes
// (never innerHTML), so copy from posts can never inject markup.

export function h(tag, props = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === false || v == null) continue;
    if (k === "class") el.className = v;
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k === "style" && typeof v === "object") for (const [p, val] of Object.entries(v)) el.style.setProperty(p, val);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, String(v));
  }
  append(el, kids);
  return el;
}

export function append(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k == null || k === false) continue;
    el.append(k.nodeType ? k : document.createTextNode(String(k)));
  }
  return el;
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
export const clear = (el) => { while (el.firstChild) el.removeChild(el.firstChild); return el; };

// ---- icons: 16px line icons, drawn once, tinted by currentColor. Status never relies on colour alone: icon + word.
const ICONS = {
  heart: "M8 13.5 2.5 8A3.3 3.3 0 0 1 8 3.5 3.3 3.3 0 0 1 13.5 8z",
  comment: "M13.5 7.5a5.5 5.5 0 0 1-8 4.9L2 13.5l1.1-3.2a5.5 5.5 0 1 1 10.4-2.8z",
  send: "M14 2 8.5 14l-2-4.5L2 7.5 14 2zM6.5 9.5 14 2",
  bookmark: "M4 2h8v12L8 11.3 4 14z",
  grid: "M2 2h12v12H2zM6 2v12M10 2v12M2 6h12M2 10h12",
  reels: "M2 3h12v11H2zM2 6h12M5 3l2 3M10 3l2 3M6.5 8l4 2-4 2z",
  profile: "M2 3h12v11H2zM6 7a2 2 0 1 0 4 0 2 2 0 0 0-4 0M4.5 13a3.5 3.5 0 0 1 7 0",
  draft: "M3 13l1-3.5L11 2.5l2.5 2.5-7 7zM9.5 4l2.5 2.5",
  in_review: "M1.5 8S4 3.5 8 3.5 14.5 8 14.5 8 12 12.5 8 12.5 1.5 8 1.5 8zM8 6.2a1.8 1.8 0 100 3.6 1.8 1.8 0 000-3.6z",
  approved: "M3 8.5l3.2 3.2L13 4.8",
  scheduled: "M8 2.5a5.5 5.5 0 100 11 5.5 5.5 0 000-11zM8 5v3.2l2.2 1.4",
  publishing: "M3 8h8M8 4.5L11.5 8 8 11.5M13.5 3v10",
  published: "M2.5 8.5l3.4 3.4 7.6-8",
  failed: "M8 2.5l6 10.5H2zM8 6.5v3M8 11.3v.2",
  missed: "M8 2.5a5.5 5.5 0 100 11 5.5 5.5 0 000-11zM5.6 5.6l4.8 4.8",
  needs_rework: "M3 8a5 5 0 018.6-3.4L13 6M13 2.8V6h-3.2M13 8a5 5 0 01-8.6 3.4L3 10M3 13.2V10h3.2",
  dropped: "M3.5 4.5h9M6 4.5V3h4v1.5M5 4.5l.5 8h5l.5-8",
  lock: "M4.5 7.5h7v5.5h-7zM6 7.5V5.5a2 2 0 014 0v2",
  grip: "M6 4v.01M10 4v.01M6 8v.01M10 8v.01M6 12v.01M10 12v.01",
  flag: "M4 14V2.5M4 3h7.5l-1.5 2.5 1.5 2.5H4",
  plus: "M8 3v10M3 8h10",
  chev_l: "M10 3.5L5.5 8l4.5 4.5",
  chev_r: "M6 3.5L10.5 8 6 12.5",
  spark: "M8 2l1.4 3.6L13 7l-3.6 1.4L8 12l-1.4-3.6L3 7l3.6-1.4z",
  crop: "M4.5 1.5v10h10M1.5 4.5h10v10",
  tray: "M2 9l2-5.5h8L14 9v4H2zM2 9h3.5l.5 1.5h4l.5-1.5H14",
  warn: "M8 2.5l6 10.5H2zM8 6.5v3M8 11.3v.2",
  x: "M4 4l8 8M12 4l-8 8",
  check: "M3 8.5l3.2 3.2L13 4.8",
  undo: "M5.5 3.5L2.5 6.5l3 3M2.8 6.5h6.2a4 4 0 010 8H6",
  calendar: "M2.5 4.5h11v9h-11zM2.5 7.5h11M5.5 2.5v3M10.5 2.5v3",
  select: "M2.5 2.5h11v11h-11zM5 8.3l2.2 2.2L11.2 5.8",
  bulb: "M6 12.5h4M6.6 14h2.8M8 1.8a4.2 4.2 0 00-2.4 7.6c.5.4.8.9.9 1.6h3c.1-.7.4-1.2.9-1.6A4.2 4.2 0 008 1.8z",
  info: "M8 2.5a5.5 5.5 0 100 11 5.5 5.5 0 000-11zM8 7.2v3.6M8 5.2v.2",
  more: "M3.5 8h.01M8 8h.01M12.5 8h.01",
};
export function icon(name, cls = "") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("class", "ico " + cls);
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
  p.setAttribute("d", ICONS[name] || ICONS.draft);
  svg.append(p);
  return svg;
}

// ---- status vocabulary (2.8). Plain words, the same everywhere.
export const STATUS = {
  draft: ["Draft", "draft"],
  in_review: ["In review", "in_review"],
  approved: ["Approved", "approved"],
  scheduled: ["Scheduled", "scheduled"],
  publishing: ["Publishing", "publishing"],
  published: ["Published", "published"],
  failed: ["Failed", "failed"],
  missed: ["Missed", "missed"],
  needs_rework: ["Needs rework", "needs_rework"],
  dropped: ["Dropped", "dropped"],
};
export const LOCKED_STATUS = new Set(["published", "publishing"]);

export function chip(status, extra = "", variant = null) {
  const [baseLabel, ic] = STATUS[status] || [String(status || "unknown"), "draft"];
  const phase=variant?.result?.receipt?.phase;
  const label=phase && phase!=="published"?phase.replaceAll("_"," "):variant?.result?.receipt?.source==="human_report"?"Published · human reported":baseLabel;
  return h("span", { class: `chip st-${status} ${extra}`, dataset: { status } }, icon(ic), h("span", { class: "chip-t" }, label));
}

// ---- why a post carries a flag: a short label for the card, a sentence for the places that have room. Never colour alone: the chip has an icon and words.
export const FLAGS = {
  delivery_processing: ["Processing", "The provider accepted this operation; publication is not yet confirmed."],
  delivery_scheduled: ["Scheduled at provider", "The provider has scheduled this post; it has not yet published."],
  delivery_awaiting_user_publish: ["Awaiting you", "Open the platform app to finish publication."],
  delivery_sandbox: ["Sandbox only", "This is a sandbox result, not a public post."],
  delivery_draft: ["Provider draft", "This remains a draft on the provider."],
  adjunct_uncertain: ["Comment uncertain", "Inspect the platform before adding the first comment manually; the primary post is published."],
  adjunct_failed: ["Comment failed", "The primary post is published. Inspect the platform to finish the first comment manually."],
  host_lease_expired: ["Media lease expired", "Refresh provider status or review the unresolved outcome before attempting another send."],
  moved_after_approval: ["Moved after approval", "The time changed after you approved this. The approval still holds, because it covers the words and pictures, not the time. It goes out at the new time, and the move is in the history."],
  approval_stale: ["Approval stale", "Something the approval covers (a file, the caption, the link or the account) is not what you approved any more. It is back in review and will not go out until you approve it again."],
  approval_expired: ["Approval expired", "The approval is older than this studio allows, so it will not go out. Review it again to approve it afresh."],
  license_gap: ["Licence missing", "A picture in this post has no attested licence entry, so it cannot go out. Attest the licence or swap the picture."],
  uncertain: ["Outcome unknown", "The send may or may not have reached the platform. Inspect the destination, then open the post’s Review publication outcome action in the desktop app."],
  bad_schedule: ["Bad time", "The stored time cannot be read. Move the post to a new time."],
};
export const flagInfo = (f) => FLAGS[f] || [String(f).replace(/_/g, " "), ""];

// ---- time. The API sends local ISO strings with an offset; the wall clock in them is already the account's, so we slice, not convert.
export const dateKey = (iso) => String(iso).slice(0, 10);
export const timeOf = (iso) => String(iso).slice(11, 16);
const utcDate = (key) => { const [y, m, d] = key.split("-").map(Number); return new Date(Date.UTC(y, m - 1, d)); };
export const keyOf = (dt) => dt.toISOString().slice(0, 10);
export const addDays = (key, n) => { const d = utcDate(key); d.setUTCDate(d.getUTCDate() + n); return keyOf(d); };
export const startOfMonth = (key) => key.slice(0, 8) + "01";
export const startOfWeek = (key) => { const d = utcDate(key); const wd = (d.getUTCDay() + 6) % 7; d.setUTCDate(d.getUTCDate() - wd); return keyOf(d); }; // Monday
export function daysBetween(a, b) { return Math.round((utcDate(b) - utcDate(a)) / 864e5); }
export function addMonths(key, n) { const d = utcDate(startOfMonth(key)); d.setUTCMonth(d.getUTCMonth() + n); return keyOf(d); }
const fmt = (opts) => new Intl.DateTimeFormat("en-GB", { timeZone: "UTC", ...opts });
export const fmtLong = (key) => fmt({ weekday: "long", day: "numeric", month: "long" }).format(utcDate(key));
export const fmtShort = (key) => fmt({ weekday: "short", day: "numeric", month: "short" }).format(utcDate(key));
export const fmtMonth = (key) => fmt({ month: "long", year: "numeric" }).format(utcDate(key));
export const fmtMonthName = (key) => fmt({ month: "long" }).format(utcDate(key));
export const fmtDow = (key) => fmt({ weekday: "short" }).format(utcDate(key));
export const fmtDayNum = (key) => String(utcDate(key).getUTCDate());
export const fmtWeekday = (key) => fmt({ weekday: "long" }).format(utcDate(key));
export const humanWhen = (iso) => `${fmtWeekday(dateKey(iso))} ${fmtDayNum(dateKey(iso))} ${fmtMonthName(dateKey(iso))}, ${timeOf(iso)}`;

// ---- announcements: one polite live region, mirrored by a visible, transient note.
let liveEl = null, noteEl = null, noteTimer = null;
export function bindLive(live, note) { liveEl = live; noteEl = note; }
// `action` = {label, run}: the note becomes a toast with a button (an Undo). It stays longer, is announced with its action, and pauses while the pointer or focus is on it.
export function announce(msg, { tone = "info", action = null } = {}) {
  if (liveEl) {
    liveEl.textContent = "";                                   // clear first so an identical message is announced again
    setTimeout(() => { liveEl.textContent = action ? `${msg} Press z to ${action.label.toLowerCase()}.` : msg; }, 30);
  }
  if (noteEl) {
    noteEl.replaceChildren(document.createTextNode(msg));
    noteEl.dataset.tone = tone;
    noteEl.classList.toggle("has-action", !!action);
    noteEl.setAttribute("aria-hidden", action ? "false" : "true");
    if (action) {
      noteEl.append(h("button", { type: "button", class: "note-act", onclick: async () => { hideNote(); await action.run(); } }, icon("undo"), action.label));
    }
    noteEl.classList.add("on");
    clearTimeout(noteTimer);
    const ms = tone === "error" ? 7000 : action ? 12000 : 4000;
    noteTimer = setTimeout(hideNote, ms);
    noteEl.onpointerenter = noteEl.onfocusin = () => clearTimeout(noteTimer);
    noteEl.onpointerleave = noteEl.onfocusout = () => { clearTimeout(noteTimer); noteTimer = setTimeout(hideNote, 4000); };
  }
}
export function hideNote() { clearTimeout(noteTimer); if (noteEl) noteEl.classList.remove("on"); }

export function store(key, value) {   // localStorage may throw (private window, blocked site data): the page must work without it
  try {
    if (value === undefined) return JSON.parse(localStorage.getItem("ave.studio." + key) || "null");
    localStorage.setItem("ave.studio." + key, JSON.stringify(value));
  } catch { /* view state only; losing it is fine */ }
  return null;
}

export const reducedMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

export function thumbImg(src, alt = "", cls = "") {
  return h("img", { src, alt, class: cls, loading: "lazy", decoding: "async", draggable: "false" });
}

// Posting mode in words (6.7): never colour alone. Returns [word, kind].
export function modeOf(account, summary) {
  if ((summary && summary.paused) || account.paused) return ["Paused", "paused"];
  if (account.live) return ["Live", "live"];
  return ["Dry run", "dry"];
}

export function monogram(platform) {
  const m = { instagram: "IG", facebook: "FB", threads: "TH", x: "X", linkedin: "in", youtube: "YT", tiktok: "TT", bluesky: "BS", pinterest: "Pi", manual: "··" };
  return m[platform] || String(platform || "?").slice(0, 2);
}
