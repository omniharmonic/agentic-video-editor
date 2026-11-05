// More tools: the shell of the four older tools the app keeps unchanged (Production, Media tools, Feed planner, Post editor), opened
// from the Workspace's "More tools" menu in their own window. Boot, hash router, status strip, change polling, theme, keyboard help.

import {leaveSafely} from "./navigation-guard.js";
import { api } from "./api.js";
import { announce, bindLive, clear, h, modeOf, $, $$, store } from "/app/shared/dom.js";
import { createDnd } from "./dnd.js";
import { makeCtx as makePanelCtx } from "/app/shared/panels.js";
import { runUndo } from "./bulk.js";

// The four tools the new app does not rebuild, kept unchanged and opened from the Workspace's "More tools" menu in their own window.
// Everything else moved to the app (/app/): the Workspace, the Room, Settings and the approval sheets.
const ROUTES = {
  workbench: "./workbench.js",
  pipeline: "./pipeline.js",
  grid: "./grid.js",
  post: "./editor.js",
};
const TITLES = { workbench: "Media tools", pipeline: "Production", grid: "Feed planner", post: "Post editor" };

const q = new URLSearchParams(location.search);
const POLL_MS = Math.max(200, Number(q.get("poll")) || 3000);   // ?poll=300 lets tests watch agent edits arrive quickly

export const ctx = {
  api,
  dnd: createDnd(),
  summary: null,
  accounts: [],
  tz: "UTC",
  today: "",
  announce,
  view: null,
  busy: () => ctx.dnd.active || document.body.classList.contains("kb-moving"),
  async navigate(hash) { if(location.hash===hash){if(await leaveSafely())await route();}else location.hash = hash; },
  remember(hash, replace=false) { history[replace?'replaceState':'pushState'](null,'',hash); acceptedHash=hash; },
  async refreshChrome() {
    const [summary, accounts] = await Promise.all([api.summary(), api.accounts()]);
    ctx.summary = summary;
    ctx.accounts = accounts.accounts || [];
    ctx.tz = ctx.summary.tz || "UTC";
    ctx.today = localDate(ctx.summary.now, ctx.tz);
    paintCounts();
    paintModes();
  },
};

let acceptedHash=location.hash;
let current = null;          // {name, mod, api}
let cursor = null;
let pendingRefresh = false;
let routeToken = 0;

// ---------- boot
async function boot() {
  bindLive($("#live"), $("#note"));
  initTheme();
  initHelp();
  initNavigation();
  try {
    const [summary, accounts] = await Promise.all([api.summary(), api.accounts()]);
    ctx.summary = summary;
    ctx.accounts = accounts.accounts || [];
    ctx.tz = summary.tz || "UTC";
    ctx.today = localDate(summary.now, ctx.tz);
    cursor = summary.cursor;
  } catch (e) {
    clear($("#main")).append(h("div", { class: "empty empty-error", role: "alert" }, h("h1", {}, "The studio is not reachable"), h("p", {}, e.message),
      h("p", {}, "The page loads again as soon as the server answers."), h("button", { type: "button", class: "primary", onclick: () => location.reload() }, "Try again")));
    return;
  }
  $("#studio-title").textContent = ctx.summary.title || "Studio";
  document.title = `${ctx.summary.title || "Studio"}`;
  paintCounts();
  paintModes();
  window.addEventListener("hashchange", route);
  ctx.dnd.onChange((active) => { if (!active) flushPending(); });
  setInterval(poll, POLL_MS);
  await route();
}

function localDate(iso, tz) {
  try { return new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date(iso)); }
  catch { return String(iso).slice(0, 10); }
}

// ---------- router
function parseHash() {
  const raw = location.hash.replace(/^#\/?/, "") || "pipeline";
  const [path, query = ""] = raw.split("?");
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  return { name: parts[0] || "pipeline", args: parts.slice(1), query: new URLSearchParams(query) };
}

async function route() {
  const requested=location.hash;
  if(current && requested!==acceptedHash && !await leaveSafely()){history.replaceState(null,"",acceptedHash);return;}
  acceptedHash=requested;
  const token = ++routeToken;
  const r = parseHash();
  document.body.classList.remove("nav-open");
  $("#nav-toggle").setAttribute("aria-expanded", "false");
  $("#nav-backdrop").hidden = true;
  $("#page-title").textContent = TITLES[r.name] || "Studio";
  if (current && current.api && current.api.unmount) { try { current.api.unmount(); } catch (e) { console.error(e); } }
  current = null;
  const main = clear($("#main"));
  main.dataset.page = r.name;
  for (const a of $$("#nav a")) { if (a.dataset.p === r.name) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); }
  const file = ROUTES[r.name];
  document.title = `${TITLES[r.name] || "Studio"} · ${ctx.summary.title || "Studio"}`;
  if (!file) return notice(main, "That page moved to the app", "The Workspace, the Room and Settings have everything else. Use Back to the Workspace above.");
  let mod;
  try { mod = await import(file); }
  catch (e) {
    if (token !== routeToken) return;
    return notice(main, `${TITLES[r.name]} could not be loaded`, "Close this window and open it again from More tools.");
  }
  if (token !== routeToken) return;
  try {
    const api2 = (await mountPage(mod, r, main)) || {};
    if (token !== routeToken) { api2.unmount && api2.unmount(); return; }
    current = { name: r.name, api: api2 };
  } catch (e) {
    console.error(e);
    notice(main, "This page could not be drawn", e.message);
  }
}

// Pages from the editor workstream export mountXxx(root, panelCtx) -> {destroy}; calendar and grid export mount(root, ctx, route).
// One adapter keeps both families behind the same router.
async function mountPage(mod, r, main) {
  if (typeof mod.mount === "function") return mod.mount(main, ctx, r);
  const pctx = makePanelCtx({ navigate: ctx.navigate, announce: (t) => ctx.announce(t),
    now: () => new Date(ctx.summary.now) });
  Object.defineProperties(pctx, { today: { get: () => ctx.today }, tz: { get: () => ctx.tz } });
  const hash = location.hash;
  const by = {
    post: () => mod.mountEditorFromHash(main, pctx, hash),
  }[r.name];
  if (!by) throw new Error(`${r.name} has no mount function`);
  const handle = (await by()) || {};
  return { unmount: handle.destroy || handle.unmount, refresh: handle.refresh };
}

function notice(main, title, body) {
  clear(main).append(h("div", { class: "empty" }, h("h1", {}, title), h("p", {}, body)));
}

// ---------- chrome
function paintCounts() {
  for (const el of $$("[data-count]")) {
    const n = (ctx.summary && el.dataset.count.split(".").reduce((o, k) => (o == null ? o : o[k]), ctx.summary)) || 0;
    el.hidden = !n;
    el.textContent = n ? String(n) : "";
    el.setAttribute("aria-label", n ? `${n} open` : "");
  }
}
// One line for every account instead of a dashed chip each: "7 accounts: all Dry run". It opens to the per-account list (never colour alone: each row says its mode in words).
function paintModes() {
  const box = $("#modes");
  const list = clear($(".mode-list", box));
  const counts = new Map();
  for (const a of ctx.accounts) {
    const [word, kind] = modeOf(a, ctx.summary);
    counts.set(word, (counts.get(word) || 0) + 1);
    list.append(h("li", { class: `mode m-${kind}`, title: kind === "dry" ? "Nothing is sent from this account until a human turns live posting on" : "" }, h("span", { class: "mode-acct" }, a.label || a.id), h("span", { class: "mode-word" }, word)));
  }
  const n = ctx.accounts.length;
  const sum = $("summary", box);
  const kinds = [...counts.entries()];
  sum.textContent = !n ? "No accounts yet" : `${n} ${n === 1 ? "account" : "accounts"}: ${kinds.length === 1 ? (n === 1 ? kinds[0][0] : "all " + kinds[0][0]) : kinds.map(([w, c]) => `${c} ${w}`).join(", ")}`;
  box.dataset.kind = kinds.some(([w]) => w === "Live") ? "live" : kinds.some(([w]) => w === "Paused") ? "paused" : "dry";
  box.hidden = !n;
}

function initNavigation() {
  const toggle = $("#nav-toggle"), backdrop = $("#nav-backdrop");
  const show = (open) => {document.body.classList.toggle("nav-open", open); toggle.setAttribute("aria-expanded", String(open)); backdrop.hidden = !open; if(open)$("#nav a")?.focus(); else toggle.focus();};
  toggle.addEventListener("click", () => show(!document.body.classList.contains("nav-open")));
  backdrop.addEventListener("click", () => show(false));
  $("#nav").addEventListener("click", e => {if(e.target.closest("a"))show(false);});
  document.addEventListener("keydown", e => {if(e.key === "Escape" && document.body.classList.contains("nav-open"))show(false);});
}

function initTheme() {
  const btn = $("#theme");
  const apply = (t) => {
    if (t === "light" || t === "dark") document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme;
    btn.textContent = t === "light" ? "Theme: light" : t === "dark" ? "Theme: dark" : "Theme: auto";
    btn.setAttribute("aria-label", `${btn.textContent}. Press to change.`);
  };
  let t = store("theme") || "light";
  apply(t);
  btn.addEventListener("click", () => { t = t === "auto" ? "light" : t === "light" ? "dark" : "auto"; store("theme", t); apply(t); });
}

function initHelp() {
  const dlg = $("#help");
  $("#help-btn").addEventListener("click", () => dlg.showModal());
  document.addEventListener("keydown", (e) => {                       // z undoes the last move or reschedule, from any page
    if (e.key !== "z" || e.metaKey || e.ctrlKey || e.altKey || e.target.closest("input, textarea, select, [contenteditable]") || document.querySelector("dialog[open]")) return;
    e.preventDefault();
    runUndo();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "?" || e.target.closest("input, textarea, select, [contenteditable]") || dlg.open) return;
    e.preventDefault();
    dlg.showModal();
  });
}

// ---------- live updates: a 3 s change-feed poll so edits an agent makes through the CLI appear without a reload
async function poll() {
  if (!cursor || document.hidden) return;
  let res;
  try {
    res = await api.changes(cursor);
    const connection = $("#connection");
    if (connection) { connection.hidden = true; connection.textContent = ""; }
  } catch (e) {
    const connection = $("#connection");
    if (connection) { connection.hidden = false; connection.textContent = "Connection lost. Showing the last loaded state. Your edits stay on this page; saving needs the studio server."; }
    return;
  }
  if (!res || !res.events || !res.events.length) { if (res && res.cursor) cursor = res.cursor; return; }
  cursor = res.cursor;
  const byAgent = res.events.filter((ev) => ev.actor && ev.actor.kind !== "human");
  if (byAgent.length) announce(`Updated: ${byAgent.length === 1 ? "an agent changed a post" : byAgent.length + " changes from an agent"}.`);
  ctx.refreshChrome().catch(() => {});
  if (ctx.busy()) { pendingRefresh = true; return; }       // never re-render under a drag
  refreshView();
}
function refreshView() { if (current && current.api && current.api.refresh) Promise.resolve(current.api.refresh()).catch(console.error); }
function flushPending() { if (pendingRefresh && !ctx.busy()) { pendingRefresh = false; refreshView(); } }
ctx.flushPending = flushPending;
new MutationObserver(flushPending).observe(document.body, { attributes: true, attributeFilter: ["class"] });

boot();
