// Shared kit for the WS6 panels (editor, review, library, feedback, alerts, accounts, previews).
// Self-contained on purpose: the shell (WS5) owns dom.js and api.js, and the panels must not depend on their exact exports.
// The shell hands every panel a `ctx` object; `makeCtx()` below builds a working one for tests and for a shell that has none.
//
//   ctx = {
//     api:      { get(path), post(path, body), put(path, body, {headers}) }   // paths are relative to /api/studio/, JSON in and out;
//                                                                              // a non-2xx response must reject with an ApiError (status, code, latest, message)
//     navigate: (hash) => void            // e.g. "#/post/p_1?variant=v_ig"
//     announce: (text, {assertive}) => void   // the shell's aria-live region; falls back to our own
//     toast:    (text, {error}) => void       // optional
//     by:       () => string               // who is acting (the reviewer's name), default from localStorage
//     now:      () => Date                  // test hook
//   }

export class ApiError extends Error {
  constructor(status, body) {
    super((body && (body.error || body.message)) || `request failed (${status})`);
    this.status = status;
    this.code = (body && body.code) || "";
    this.latest = body && body.latest;
    this.body = body || {};
  }
}

/** Minimal fetch wrapper with the contract above. The shell's api.js may replace it. */
export function makeApi(base = "/api/studio/") {
  const url = (p) => base + String(p).replace(/^\//, "");
  async function call(method, path, body, opts = {}) {
    const init = { method, headers: { Accept: "application/json", ...(opts.headers || {}) }, credentials: "same-origin" };
    if (body !== undefined && body !== null) {
      if (body instanceof Blob || body instanceof ArrayBuffer) init.body = body;
      else { init.body = JSON.stringify(body); init.headers["Content-Type"] = "application/json"; }
    }
    let res;
    try { res = await fetch(url(path), init); }
    catch { throw new ApiError(0, { error: window.aveNative ? 'The workspace is not reachable. Reopen it from the app’s File menu.' : 'The studio server is not reachable. Is `ave studio ui` still running?', code: "offline" }); }
    let data = null;
    try { data = await res.json(); } catch { data = null; }
    if (!res.ok) throw new ApiError(res.status, data);
    return data;
  }
  return {
    get: (p) => call("GET", p),
    post: (p, b) => call("POST", p, b === undefined ? {} : b),
    put: (p, b, o) => call("PUT", p, b, o),
  };
}

export function makeCtx(over = {}) {
  const live = ensureLive();
  return {
    api: makeApi(),
    navigate: (h) => { location.hash = h; },
    announce: (t, o) => announceIn(live, t, o),
    toast: (t, o) => toast(t, o),
    by: () => storeGet("studio.by", ""),
    now: () => new Date(),
    ...over,
  };
}

// ---- tiny DOM helper ---------------------------------------------------------------------------------------------------------------
/** h("div.cls#id", {attr, onclick, dataset, class}, ...children). Strings become text nodes; null/false are skipped; arrays flatten. */
export function h(tag, props, ...kids) {
  const m = /^([a-z0-9-]*)((?:[.#][\w-]+)*)$/i.exec(tag) || [];
  const el = document.createElement(m[1] || "div");
  for (const part of (m[2] || "").match(/[.#][\w-]+/g) || []) {
    if (part[0] === "#") el.id = part.slice(1); else el.classList.add(part.slice(1));
  }
  if (props && (typeof props !== "object" || props instanceof Node || Array.isArray(props))) { kids.unshift(props); props = null; }
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.classList.add(...String(v).split(/\s+/).filter(Boolean));
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "value" || k === "checked" || k === "disabled" || k === "selected" || k === "hidden") el[k] = v;
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  append(el, kids);
  return el;
}
function append(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k === null || k === undefined || k === false) continue;
    el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
}
export const clear = (el) => { while (el.firstChild) el.removeChild(el.firstChild); return el; };
export const q = (root, sel) => root.querySelector(sel);
export const qa = (root, sel) => Array.from(root.querySelectorAll(sel));

export function debounce(fn, ms) {
  let t = null;
  const d = (...a) => { clearTimeout(t); t = setTimeout(() => { t = null; fn(...a); }, ms); };
  d.flush = (...a) => { if (t) { clearTimeout(t); t = null; } return fn(...a); };
  d.cancel = () => { clearTimeout(t); t = null; };
  d.pending = () => t !== null;
  return d;
}

// ---- guarded storage (view state only; the files are the truth, spec 7.7) ----------------------------------------------------------
export function storeGet(k, dflt = "") { try { const v = localStorage.getItem(k); return v === null ? dflt : v; } catch { return dflt; } }
export function storeSet(k, v) { try { localStorage.setItem(k, v); } catch { /* private window: fine */ } }

// ---- announcements and toast -------------------------------------------------------------------------------------------------------
function ensureLive() {
  let el = document.getElementById("st-live");
  if (!el) {
    el = h("div#st-live.st-sr", { role: "status", "aria-live": "polite", "aria-atomic": "true" });
    document.body.append(el);
  }
  return el;
}
function announceIn(el, text, { assertive = false } = {}) {
  el.setAttribute("aria-live", assertive ? "assertive" : "polite");
  el.textContent = "";
  setTimeout(() => { el.textContent = text; }, 30);
}
export function announce(ctx, text, opts) {
  if (ctx && ctx.announce) ctx.announce(text, opts); else announceIn(ensureLive(), text, opts);
}
let toastTimer = null;
export function toast(text, { error = false } = {}) {
  let el = document.getElementById("st-toast");
  if (!el) { el = h("div#st-toast", { role: "status" }); document.body.append(el); }
  el.textContent = text;
  el.className = "st-toast show" + (error ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), error ? 6000 : 3200);
}
export function say(ctx, text, opts = {}) {
  announce(ctx, text, opts);
  if (ctx && ctx.toast) ctx.toast(text, opts); else toast(text, opts);
}

// ---- formatting --------------------------------------------------------------------------------------------------------------------
export const shortSha = (s, n = 8) => (s ? String(s).slice(0, n) : "none");
export function groupSha(s) { return String(s || "").replace(/(.{8})/g, "$1 ").trim(); }
export function fmtBytes(n) {
  if (n === null || n === undefined) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}
export function fmtWhen(iso, tz) {
  if (!iso) return "";
  const naive = !/(Z|[+-]\d{2}:?\d{2})$/.test(String(iso));                           // a wall-clock time in `tz`: already local there, so do not shift it by the browser's zone
  const d = new Date(naive ? (iso.length === 16 ? iso + ":00" : iso) + "Z" : isoFix(iso));
  if (isNaN(d)) return String(iso);
  try {
    return new Intl.DateTimeFormat(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZone: naive ? "UTC" : (tz || undefined) }).format(d);
  } catch { return d.toLocaleString(); }
}
const isoFix = (s) => String(s).replace(/([+-]\d{2})(\d{2})$/, "$1:$2");          // the studio writes -0600; Safari wants -06:00
export function ago(iso, now = new Date()) {
  const t = new Date(isoFix(iso));
  if (isNaN(t)) return "";
  const s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

export const STATUS = {
  idea: ["○", "Idea"], draft: ["○", "Draft"], in_review: ["◐", "In review"], needs_rework: ["↺", "Waiting for the editor"],
  approved: ["✓", "Approved"], scheduled: ["◷", "Scheduled"], publishing: ["…", "Publishing"], published: ["●", "Published"],
  failed: ["✕", "Failed"], missed: ["!", "Missed"], dropped: ["⌀", "Dropped"],
};
/** A status chip that never relies on colour alone: glyph + words. */
export function statusChip(status, variant = null) {
  const [g, baseLabel] = STATUS[status] || ["?", status || "Unknown"];
  const phase=variant?.result?.receipt?.phase;
  const label=phase && phase!=="published"?phase.replaceAll("_"," "):variant?.result?.receipt?.source==="human_report"?"Published · human reported":baseLabel;
  return h(`span.st-chip.s-${status || "unknown"}`, { title: label }, h("span.g", { "aria-hidden": "true" }, g), label);
}
export const PLATFORM_NAMES = {
  instagram: "Instagram", facebook: "Facebook", x: "X", linkedin: "LinkedIn", tiktok: "TikTok", youtube: "YouTube Shorts", bluesky: "Bluesky",
  threads: "Threads", pinterest: "Pinterest",
};

// ---- data helpers shared by the panels ---------------------------------------------------------------------------------------------
export function findVariant(post, variantId) {
  return (post.variants || []).find((v) => v.id === variantId) || null;
}
/** The caption this variant will post: its own, else the post's shared copy. */
export function effectiveCaption(post, variant) {
  const c = variant && variant.copy && variant.copy.caption;
  return c === null || c === undefined ? (post.copy && post.copy.base) || "" : c;
}
/** What a person calls this post: the idea, else the first line of its caption, else (only as a last resort) the id. */
export function postTitle(post, variant) {
  const text = (post.idea && post.idea.text) || effectiveCaption(post, variant) || (post.copy && post.copy.base) || "";
  const first = String(text).trim().split(/\n/)[0].trim();
  return first.length > 72 ? first.slice(0, 69).trimEnd() + "…" : first || post.id;
}
export function effectiveFirstComment(post, variant) {
  const c = variant && variant.copy && variant.copy.first_comment;
  return c === null || c === undefined ? (post.copy && post.copy.first_comment) || "" : c;
}
export function effectiveHashtags(post, variant) {
  const c = variant && variant.copy && variant.copy.hashtags;
  return c === null || c === undefined ? (post.copy && post.copy.hashtags) || [] : c;
}
export function effectiveMedia(post, variant) {
  return [...(variant && variant.media !== null && variant.media !== undefined ? variant.media : post.media || [])].sort((a,b)=>a.slot-b.slot);
}
/** Read a dotted path out of an object ("variants.v_ig.copy.caption", "media.0.alt"); array members may be addressed by index or by `id`. */
export function getPath(obj, path) {
  let cur = obj;
  for (const part of path.split(".")) {
    if (cur === null || cur === undefined) return undefined;
    if (Array.isArray(cur)) {
      const byId = cur.find((x) => x && x.id === part);
      cur = byId !== undefined ? byId : cur[Number(part)];
    } else cur = cur[part];
  }
  return cur;
}

/** Same-origin paths only: the page's CSP is 'self', and a preview must never cause a third-party request. */
export function localUrl(u) {
  if (!u || typeof u !== "string") return "";
  if (u.startsWith("/") && !u.startsWith("//")) return u;
  try {
    const x = new URL(u, location.href);
    return x.origin === location.origin ? x.pathname + x.search : "";
  } catch { return ""; }
}

// ---- modal (native <dialog>: focus trap, Esc, inert background for free) -------------------------------------------------------------
export function openModal({ title, body, actions = [], onClose, wide = false, labelledby }) {
  const prev = document.activeElement;
  const titleId = labelledby || "st-dlg-" + Math.random().toString(36).slice(2, 8);
  const dlg = h("dialog.st.st-dialog" + (wide ? ".wide" : ""), { "aria-labelledby": titleId });
  const head = h("header.st-dialog-head", h("h2", { id: titleId }, title), h("button.ghost.st-x", { type: "button", "aria-label": "Close", onclick: () => close("cancel") }, "×"));
  const foot = actions.length ? h("footer.st-dialog-foot", actions) : null;
  dlg.append(head, h("div.st-dialog-body", body), foot);
  document.body.append(dlg);
  let done = false;
  function close(reason = "cancel") {
    if (done) return;
    done = true;
    try { dlg.close(); } catch { /* already closed */ }
    dlg.remove();
    if (prev && prev.focus && document.contains(prev)) prev.focus();
    if (onClose) onClose(reason);
  }
  dlg.addEventListener("cancel", (e) => { e.preventDefault(); close("cancel"); });
  dlg.addEventListener("click", (e) => { if (e.target === dlg) close("cancel"); });
  if (dlg.showModal) dlg.showModal(); else dlg.setAttribute("open", "");
  const first = dlg.querySelector("[autofocus], textarea, input, select, button:not(.st-x)") || dlg.querySelector("button");
  if (first) first.focus();
  return { el: dlg, close, body: dlg.querySelector(".st-dialog-body") };
}

export function field(label, control, { hint, id, wide } = {}) {
  const cid = id || "f-" + Math.random().toString(36).slice(2, 8);
  if (control.setAttribute && !control.id) control.id = cid;
  return h("div.st-field" + (wide ? ".wide" : ""), h("label", { for: control.id }, label), control, hint ? h("p.st-hint", hint) : null);
}

export function empty(title, body) {
  return h("div.st-empty", h("p.st-empty-title", title), body ? h("p.st-hint", body) : null);
}

export function errorBox(err, onRetry = null) {
  const msg = err && err.message ? err.message : String(err);
  return h("div.st-alert.bad", { role: "alert" }, h("strong", "That did not work. "), msg,
    onRetry ? h("p", h("button", { type: "button", dataset: { act: "retry" }, onclick: onRetry }, "Try again")) : null);
}

/** Read the query part of a hash route: "#/post/p_1?variant=v_ig" -> {variant: "v_ig"}. */
export function hashQuery(hash = location.hash) {
  const i = hash.indexOf("?");
  return i < 0 ? {} : Object.fromEntries(new URLSearchParams(hash.slice(i + 1)));
}

export function reducedMotion() {
  try { return matchMedia("(prefers-reduced-motion: reduce)").matches; } catch { return false; }
}
