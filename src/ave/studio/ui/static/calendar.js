// The calendar (spec 7.2 #/calendar): month, week and day; per-account lanes; empty rule slots as drop targets; an unscheduled
// tray; agent proposals as ghost cards; status chips (icon + word). Drag with the pointer, or press m and use the arrow keys.
// Times are never computed here: a drop sends a wish to POST /posts/<id>/move and the server answers with the truth.

import { ApiError } from "./api.js";
import {
  $, $$, addDays, append, addMonths, announce, chip, clear, dateKey, flagInfo, fmtDayNum, fmtDow, fmtLong, fmtMonth, fmtMonthName, fmtShort, h, humanWhen, icon,
  LOCKED_STATUS, monogram, startOfMonth, startOfWeek, store, thumbImg, timeOf,
} from "./dom.js";
import { KeyboardMover } from "./dnd.js";
import { getUndo, nextFreeSlot, onUndoChange, runBulk, runUndo, setUndo } from "./bulk.js";
import { activeIds, anyFilter, filterBar, readFilters, writeFilters } from "./filters.js";
import { closePeek, isPeekOpen, openPeek, peekTarget } from "./peek.js";
import { openReschedule } from "./reschedule.js";
import { openModal } from "./panels.js";

const flagText = (f) => flagInfo(f)[0];
const isNarrow = () => matchMedia("(max-width: 899px)").matches;
const isPhone = () => matchMedia("(max-width: 639px)").matches;

export function rangeFor(view, anchor) {
  if (view === "day") return [anchor, anchor];
  if (view === "week") { const s = startOfWeek(anchor); return [s, addDays(s, 6)]; }
  const first = startOfMonth(anchor);
  const last = addDays(addMonths(first, 1), -1);
  return [startOfWeek(first), addDays(startOfWeek(last), 6)];
}
export function eachDay(from, to) { const out = []; for (let d = from; d <= to; d = addDays(d, 1)) out.push(d); return out; }

export async function mount(root, ctx, route) {
  const accounts = ctx.accounts;
  const saved = store("cal") || {};
  const legacyOn = (route.query.get("accounts") || (saved.accounts || []).join(",") || "").split(",").filter((id) => accounts.some((a) => a.id === id));
  const filters0 = readFilters(accounts, legacyOn.length && legacyOn.length < accounts.length ? accounts.filter((a) => !legacyOn.includes(a.id)).map((a) => a.id) : []);
  const st = {
    view: route.query.get("view") || saved.view || (isPhone() ? "week" : "month"),
    anchor: route.query.get("d") || ctx.today,
    filters: filters0,
    active: new Set(activeIds(accounts, filters0)),
    campaigns: [],
    foldOpen: null,
    sel: new Set(),                // variant ids of the selected cards
    selecting: false,
    data: null,
    focusDate: null,
    pendingFocus: null,
    pending: 0,
  };
  if (!["month", "week", "day"].includes(st.view)) st.view = "month";
  st.focusDate = st.anchor;
  const mover = new KeyboardMover();
  const acct = (id) => accounts.find((a) => a.id === id);
  let alive = true;
  let reqSeq = 0;

  // ---------- skeleton
  const title = h("h1", { class: "cal-title", id: "cal-title" });
  const rangeLabel = h("p", { class: "cal-sub", id: "cal-sub" });
  const seg = h("div", { class: "seg", role: "group", "aria-label": "Calendar view" },
    ["month", "week", "day"].map((v) => h("button", { type: "button", class: "seg-b", dataset: { view: v }, "aria-pressed": String(v === st.view), onclick: () => setView(v) }, v[0].toUpperCase() + v.slice(1))));
  const nav = h("div", { class: "pager", role: "group", "aria-label": "Change period" },
    h("button", { type: "button", class: "icon-b", "aria-label": "Previous", onclick: () => step(-1) }, icon("chev_l")),
    h("button", { type: "button", class: "today-b", onclick: () => jump(ctx.today) }, "Today"),
    h("button", { type: "button", class: "icon-b", "aria-label": "Next", onclick: () => step(1) }, icon("chev_r")));
  const acctBar = h("div", { class: "fbar-host" });
  const selToggle = h("button", { type: "button", class: "tool-b", id: "sel-toggle", "aria-pressed": "false", onclick: () => setSelecting(!st.selecting) }, icon("select"), "Select");
  const undoBtn = h("button", { type: "button", class: "tool-b", id: "undo-b", hidden: true, onclick: () => runUndo() }, icon("undo"), "Undo");
  const selBar = h("div", { class: "selbar", id: "selbar", role: "region", "aria-label": "Selected posts", hidden: true });
  const gridHost = h("div", { class: "cal-host" });
  const rail = h("aside", { class: "rail", "aria-label": "Unscheduled posts and suggestions" });
  const hint = h("p", { class: "kb-hint", id: "kb-hint", hidden: true }, "Press Enter to preview a post. m picks it up to move with the arrow keys (Enter drops, Escape cancels), r changes its time, n takes its next free slot, u moves it to Unscheduled, x selects it.");
  clear(root).append(
    h("div", { class: "cal-head" }, h("div", { class: "cal-titles" }, title, rangeLabel), h("div", { class: "cal-tools" }, undoBtn, selToggle, nav, seg)),
    acctBar,
    h("div", { class: "cal-layout" }, h("div", { class: "cal-main" }, gridHost), rail),
    hint, selBar);

  // ---------- data
  async function load({ quiet = false } = {}) {
    const [from, to] = rangeFor(st.view, st.anchor);
    const seq = ++reqSeq;
    st.pending++;
    root.setAttribute("aria-busy", "true");
    try {
      const data = await ctx.api.calendar(from, to, [...st.active]);
      if (!alive || seq !== reqSeq) return;
      st.data = data;
      render();
    } catch (e) {
      if (!alive) return;
      if (!quiet) announce(e.message, { tone: "error" });
      if (!st.data) gridHost.replaceChildren(h("div", { class: "empty empty-error", role: "alert" }, h("h2", {}, "The calendar could not load"), h("p", {}, e.message),
        h("button", { type: "button", class: "primary", onclick: () => { gridHost.replaceChildren(h("p", { class: "loading" }, "Loading the calendar")); load(); } }, "Try again")));
    } finally {
      st.pending--;
      if (!st.pending) root.removeAttribute("aria-busy");
    }
  }
  const persist = () => {
    store("cal", { view: st.view });
    const q = new URLSearchParams({ view: st.view, d: st.anchor });
    history.replaceState(null, "", `#/calendar?${q}`);
  };

  // ---------- navigation
  function setView(v) { st.view = v; persist(); seg.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.view === v))); load(); }
  function step(n) {
    st.anchor = st.view === "month" ? addMonths(st.anchor, n) : addDays(st.anchor, n * (st.view === "week" ? 7 : 1));
    st.focusDate = st.view === "month" ? st.anchor : st.anchor;
    persist(); load();
  }
  function jump(date) { st.anchor = date; st.focusDate = date; st.pendingFocus = { date }; persist(); load(); announce(`Showing ${fmtLong(date)}.`); }
  function setFilters(next) {
    st.filters = next;
    writeFilters(next);
    const ids = new Set(activeIds(accounts, next));
    const changed = ids.size !== st.active.size || [...ids].some((id) => !st.active.has(id));
    st.active = ids;
    pruneSelection();
    paintAccounts();
    if (changed) load(); else if (st.data) render();
  }
  function paintAccounts() {
    clear(acctBar);
    if (accounts.length < 2 && !st.campaigns.length) { acctBar.hidden = true; return; }
    acctBar.hidden = false;
    acctBar.append(filterBar({ accounts, campaigns: st.campaigns, filters: st.filters, onChange: setFilters, phone: isPhone(), label: "Brand, campaign and account filters",
      foldOpen: st.foldOpen, onFold: (o) => { st.foldOpen = o; } }));
  }
  ctx.api.get("campaigns").then((r) => { st.campaigns = (r.campaigns || []).map((c) => ({ id: c.campaign.id, name: c.campaign.name, accounts: c.campaign.accounts || Object.keys(c.cadence || {}) })); paintAccounts(); }).catch(() => {});

  // ---------- items
  const hueOf = (id) => Math.max(0, accounts.findIndex((a) => a.id === id)) % 4;
  const itemLocked = (it) => !!it.locked || LOCKED_STATUS.has(it.status);
  const lockWhy = (it) => it.status === "published" ? "Published posts cannot be moved." : it.status === "publishing" ? "This post is being sent right now." : "This post is locked.";

  function cardLabel(it) {
    const when = it.at ? `${humanWhen(it.at)}` : "not scheduled";
    return `${it.title}. ${(it.status || "").replace(/_/g, " ")}. ${when}${it.account ? `. ${it.account}` : ""}${it.flags && it.flags.length ? ". " + it.flags.map(flagText).join(", ") : ""}${st.sel.has(it.variant) ? ". Selected" : ""}`;
  }

  function card(it, mode, tabbable) {
    const locked = itemLocked(it);
    const li = h("li", {
      class: `card mode-${mode} st-${it.status} hue-${hueOf(it.account)}${locked ? " is-locked" : ""}`,
      dataset: { post: it.post, variant: it.variant || "", status: it.status, mode },
    });
    const link = h("a", {
      class: "card-link", draggable: "false", href: `#/post/${encodeURIComponent(it.post)}${it.variant ? "?variant=" + encodeURIComponent(it.variant) : ""}`,
      tabindex: tabbable ? "0" : "-1", "aria-label": cardLabel(it), "aria-describedby": locked ? null : "kb-hint", "aria-keyshortcuts": locked ? null : "m u r n x",
      title: locked ? lockWhy(it) : null,
    },
      h("span", { class: "thumb" }, it.thumb ? thumbImg(it.thumb, "") : h("span", { class: "thumb-empty" }, "No image"),
        it.account ? h("span", { class: "acct-tag", "aria-hidden": "true" }, monogram((acct(it.account) || {}).platform)) : null),
      h("span", { class: "cap" },
        h("span", { class: "time" }, it.at ? timeOf(it.at) : "No time"),
        chip(it.status)),
      h("span", { class: "ttl" }, it.title),
      it.flags && it.flags.length ? h("span", { class: "flags" }, it.flags.map((f) => h("span", { class: `flag f-${f}`, title: flagInfo(f)[1] || flagText(f) }, icon("flag"), h("span", { class: "flag-t" }, flagText(f))))) : null);
    li.append(link);
    if (it.variant && mode !== "ghost") {
      const on = st.sel.has(it.variant);
      if (on) li.classList.add("is-selected");
      li.append(h("label", { class: "sel", title: locked ? "Published posts cannot be changed" : "Select this post" },
        h("input", { type: "checkbox", class: "sel-box", checked: on, disabled: locked, "aria-label": `Select ${it.title}`, onchange: (e) => toggleSelect(it.variant, e.target.checked) }),
        h("span", { class: "sel-mark", "aria-hidden": "true" }, icon("check"))));
    }
    li.append(locked ? h("span", { class: "lock", title: lockWhy(it) }, icon("lock"), h("span", { class: "sr-only" }, lockWhy(it)))
      : h("span", { class: "grip", "aria-hidden": "true" }, icon("grip")));
    if (!locked && it.variant && mode !== "month") {                       // a tap-sized way to change the time without dragging (month cards are too small: open the preview)
      li.append(h("button", { type: "button", class: "card-act", title: "Change time", "aria-label": `Change time for ${it.title}`,
        onclick: (e) => { e.stopPropagation(); openReschedule(ctx, [asRs(it)], { onDone: afterBulk }); } }, icon("calendar")));
    }
    if (!locked) ctx.dnd.source(li, { payload: () => ({ ...it, from: it.at ? dateKey(it.at) : null }), handleSel: ".grip" });
    return li;
  }

  function ghostCard(pr, mv, base) {
    const li = h("li", { class: `card mode-ghost mode-${st.view === "month" ? "month" : "week"}`, dataset: { proposal: pr.id, ghostVariant: mv.variant } });
    li.append(
      h("div", { class: "ghost-body" },
        h("span", { class: "thumb" }, base && base.thumb ? thumbImg(base.thumb, "") : null, h("span", { class: "ghost-tag" }, icon("spark"), h("span", { class: "gt" }, "Suggested"))),
        h("span", { class: "cap" }, h("span", { class: "time" }, mv.to.local.slice(11, 16))),
        h("span", { class: "ttl" }, base ? base.title : mv.post),
        h("span", { class: "why" }, mv.why || "")),
      h("button", { type: "button", class: "ghost-accept", "aria-label": `Accept: move ${base ? base.title : mv.post} to ${fmtLong(mv.to.local.slice(0, 10))} at ${mv.to.local.slice(11, 16)}`, onclick: () => acceptProposal(pr) }, icon("check"), "Accept"));
    return li;
  }

  // ---------- render
  const findItem = (variant) => {
    if (!st.data) return null;
    for (const list of Object.values(st.data.days)) for (const it of list) if (it.variant === variant) return it;
    return (st.data.unscheduled || []).find((it) => it.variant === variant) || null;
  };

  const passCampaign = (it) => !st.filters.campaign || (!!it && it.campaign === st.filters.campaign);
  // A suggestion is shown only when every post it would move is visible under the filters: "Accept" applies all of its moves.
  const proposalVisible = (pr) => pr.moves.every((mv) => { const b = findItem(mv.variant); return !!b && passCampaign(b); });
  function viewData(d) {
    const proposals = (d.proposals || []).filter(proposalVisible);
    st.hiddenProposals = (d.proposals || []).length - proposals.length;
    if (!st.filters.campaign) return { ...d, proposals };
    return { ...d, days: Object.fromEntries(Object.entries(d.days).map(([k, l]) => [k, l.filter(passCampaign)]).filter(([, l]) => l.length)),
      unscheduled: (d.unscheduled || []).filter(passCampaign), proposals };
  }

  function render() {
    const focusMemo = rememberFocus();
    ctx.dnd.reset();
    const [from, to] = rangeFor(st.view, st.anchor);
    const days = eachDay(from, to);
    const data = viewData(st.data);
    title.textContent = st.view === "month" ? fmtMonth(st.anchor) : st.view === "week" ? `${fmtShort(from)} to ${fmtShort(to)}` : fmtLong(st.anchor);
    rangeLabel.textContent = `${data.tz || ctx.tz} time. ${countPosts(data)} on the calendar${(data.unscheduled || []).length ? `, ${(data.unscheduled || []).length} waiting in Unscheduled` : ""}.`;
    if (ctx.summary && ctx.summary.posts === 0) {       // a new studio: say what to do instead of showing seven empty days
      rangeLabel.textContent += " Nothing is planned yet. Ask Claude for posts (it files them as ideas and drafts), then drag them onto a day.";
    }
    if (!days.includes(st.focusDate)) st.focusDate = days.includes(ctx.today) ? ctx.today : days[0];

    const inMonth = (d) => st.view !== "month" || d.slice(0, 7) === st.anchor.slice(0, 7);
    const multi = st.active.size > 1;
    const grid = h("div", { class: `cal-grid view-${st.view}`, role: "grid", "aria-labelledby": "cal-title", "aria-describedby": "kb-hint" });
    const dowRow = h("div", { class: "dow-row", role: "row" });
    for (const d of days.slice(0, st.view === "day" ? 1 : 7)) dowRow.append(h("div", { class: "dow", role: "columnheader" }, h("span", { "aria-hidden": "true" }, fmtDow(d)), h("span", { class: "sr-only" }, fmtLong(d).split(" ")[0])));
    grid.append(dowRow);
    const perRow = st.view === "day" ? 1 : 7;
    for (let i = 0; i < days.length; i += perRow) {
      const row = h("div", { class: "week-row", role: "row" });
      for (const d of days.slice(i, i + perRow)) row.append(dayCell(d, data, inMonth(d), multi));
      grid.append(row);
    }
    gridHost.replaceChildren(grid);
    if (anyFilter(st.filters) && !countPosts(data) && !(data.unscheduled || []).length) {
      gridHost.append(h("div", { class: "empty empty-filter" }, h("h2", {}, "Nothing matches these filters"),
        h("p", {}, "No post in this period belongs to the brand, campaign and accounts you picked. Other periods and brands may still have posts."),
        h("button", { type: "button", class: "primary", onclick: () => setFilters({ brand: null, campaign: null, off: [] }) }, "Clear filters")));
    } else if (!countPosts(data) && !Object.keys(data.slots || {}).length) {
      gridHost.append(h("p", { class: "quiet-note" }, `Nothing is scheduled in ${st.view === "month" ? fmtMonthName(st.anchor) : "this period"} yet. Ask Claude to plan some posts, or drag one in from Unscheduled.`));
    }
    renderRail(data);
    paintSelection();
    restoreFocus(focusMemo);
    if (st.pendingFocus) { applyPendingFocus(); }
  }

  const countPosts = (data) => Object.values(data.days).reduce((n, l) => n + l.length, 0);

  function dayCell(d, data, inRange, multi) {
    const items = (data.days[d] || []).slice().sort((a, b) => a.at.localeCompare(b.at) || (a.account || "").localeCompare(b.account || ""));
    const slots = d < ctx.today ? [] : (data.slots[d] || []).filter((s) => s.free);
    const ghosts = [];
    for (const pr of data.proposals || []) for (const mv of pr.moves) if (mv.to.local.slice(0, 10) === d && st.active.has((findItem(mv.variant) || {}).account || [...st.active][0])) ghosts.push([pr, mv]);
    const isToday = d === ctx.today;
    const label = `${fmtLong(d)}${isToday ? ", today" : ""}. ${items.length} ${items.length === 1 ? "post" : "posts"}${ghosts.length ? `, ${ghosts.length} suggested` : ""}${slots.length ? `, ${slots.length} free ${slots.length === 1 ? "slot" : "slots"}` : ""}`;
    const cell = h("div", {
      class: `day${isToday ? " is-today" : ""}${inRange ? "" : " is-outside"}${d < ctx.today ? " is-past" : ""}`, role: "gridcell", tabindex: d === st.focusDate ? "0" : "-1",
      "aria-label": label, dataset: { date: d },
    });
    cell.append(h("div", { class: "day-head" }, h("span", { class: "num" }, fmtDayNum(d)), st.view !== "month" ? h("span", { class: "day-name" }, fmtDow(d)) : (fmtDayNum(d) === "1" ? h("span", { class: "mon" }, fmtMonthName(d)) : null), isToday ? h("span", { class: "today-mark" }, "Today") : null));
    const list = h("ul", { class: "day-list", "aria-label": fmtLong(d) });
    const tab = d === st.focusDate;
    let lastAcct = null;
    for (const it of items) {
      if (multi && st.view !== "month" && it.account !== lastAcct) { lastAcct = it.account; list.append(h("li", { class: `lane hue-${hueOf(it.account)}`, "aria-hidden": "true" }, h("span", { class: "mono" }, monogram((acct(it.account) || {}).platform)), it.account)); }
      list.append(card(it, st.view === "month" ? "month" : "week", tab));
    }
    for (const [pr, mv] of ghosts) list.append(ghostCard(pr, mv, findItem(mv.variant)));
    for (const s of slots) {
      const slotEl = h("li", { class: `slot hue-${hueOf(s.account)}`, "aria-hidden": "true", dataset: { slotAt: s.at, account: s.account } }, icon("plus"), timeOf(s.at));
      ctx.dnd.target(slotEl, {
        kind: "slot", label: `${fmtLong(d)} at ${timeOf(s.at)}, free slot`,
        accept: (p) => !!p.variant && p.account === s.account,
        drop: (p) => performMove(p, { kind: "slot", date: d, time: timeOf(s.at), label: `${fmtLong(d)} at ${timeOf(s.at)}` }),
      });
      list.append(slotEl);
    }
    cell.append(list);
    ctx.dnd.target(cell, {
      kind: "day", label: fmtLong(d),
      drop: (p) => performMove(p, { kind: "day", date: d, time: p.at ? timeOf(p.at) : defaultTime(p, d, data), label: fmtLong(d) }),
    });
    return cell;
  }
  function defaultTime(p, d, data) {
    const s = (data.slots[d] || []).find((x) => x.free && x.account === p.account);
    return s ? timeOf(s.at) : "09:00";
  }

  function renderRail(data) {
    clear(rail);
    const props = data.proposals || [];
    if (props.length) {
      const sec = h("section", { class: "suggest", "aria-labelledby": "sug-h" }, h("h2", { id: "sug-h" }, icon("spark"), "Claude suggests"));
      for (const pr of props) {
        const li = h("ul", { class: "sug-list" });
        for (const mv of pr.moves) {
          const base = findItem(mv.variant);
          li.append(h("li", {}, h("strong", {}, base ? base.title : mv.post), h("span", { class: "sug-move" }, `${base && base.at ? fmtShort(dateKey(base.at)) : "Unscheduled"} to ${fmtShort(mv.to.local.slice(0, 10))}, ${mv.to.local.slice(11, 16)}`), mv.why ? h("span", { class: "why" }, mv.why) : null));
        }
        sec.append(li, h("button", { type: "button", class: "primary small", onclick: () => acceptProposal(pr) }, props.length > 1 || pr.moves.length > 1 ? `Accept ${pr.moves.length} ${pr.moves.length === 1 ? "move" : "moves"}` : "Accept move"));
      }
      rail.append(sec);
    }
    if (st.hiddenProposals) rail.append(h("p", { class: "quiet-note hidden-sug" }, `${st.hiddenProposals} ${st.hiddenProposals === 1 ? "suggestion is" : "suggestions are"} hidden because they involve posts outside these filters.`));
    const un = data.unscheduled || [];
    const tray =h("section", { class: `tray${un.length ? "" : " is-empty"}`, "aria-labelledby": "tray-h", dataset: { tray: "1" } },
      h("h2", { id: "tray-h" }, icon("tray"), "Unscheduled", h("span", { class: "tray-n" }, String(un.length))));
    const strip = h("ul", { class: "strip", "aria-label": "Unscheduled posts" });
    un.forEach((it, i) => strip.append(card(it, "tray", i === 0)));
    if (!un.length) strip.append(h("li", { class: "strip-empty" }, "Nothing waiting. Drop a post here to take it off the calendar."));
    tray.append(strip, h("p", { class: "tray-help" }, "Drag a post here to unschedule it, or drag one out onto a day."));
    ctx.dnd.target(tray, {
      kind: "tray", label: "Unscheduled", accept: (p) => !!p.variant && !!p.at,
      drop: (p) => performUnschedule(p),
    });
    rail.append(tray);
  }

  // ---------- mutations (optimistic, server authoritative)
  async function performMove(p, target) {
    if (!p.variant) {
      return announce(`"${p.title}" has no account yet. Open it and add a variant before scheduling.`, { tone: "error" });
    }
    const local = `${target.date}T${target.time}`;
    if (p.at && p.at.slice(0, 16) === local) return announce(`"${p.title}" is already at ${target.label}.`);
    const before = structuredClone(st.data);
    optimistic(p, target.date, local);
    try {
      const res = await ctx.api.move(p.post, { variant: p.variant, expected_rev: p.rev, to: { local, tz: ctx.tz }, snap: "slot" });
      st.pendingFocus = { variant: p.variant };
      await load({ quiet: true });
      const now = findItem(p.variant);
      markLanded(p.variant);
      const approved = !!(now && (now.flags || []).includes("moved_after_approval"));
      const text = `Moved "${p.title}" to ${now && now.at ? humanWhen(now.at) : target.label}.${approved ? " It was approved: the approval still holds." : ""}`;
      announce(text, { action: moveUndo(p, res, text) });
      ctx.refreshChrome().catch(() => {});
    } catch (e) {
      st.data = before; render();
      st.pendingFocus = { variant: p.variant };
      applyPendingFocus();
      announce(failText("move", p, e), { tone: "error" });
      if (e instanceof ApiError && e.status === 409) load({ quiet: true });
    }
  }
  async function performUnschedule(p) {
    if (!p.variant) return;
    if (!p.at) return announce(`"${p.title}" is already unscheduled.`);
    const before = structuredClone(st.data);
    const un = st.data.unscheduled = st.data.unscheduled || [];
    const day = st.data.days[dateKey(p.at)] || [];
    const idx = day.findIndex((x) => x.variant === p.variant);
    if (idx >= 0) { const [it] = day.splice(idx, 1); un.push({ ...it, at: undefined }); }
    render();
    try {
      const res = await ctx.api.unschedule(p.post, { variant: p.variant, expected_rev: p.rev });
      st.pendingFocus = { variant: p.variant };
      await load({ quiet: true });
      markLanded(p.variant);
      const text = `Moved "${p.title}" to Unscheduled.`;
      announce(text, { action: moveUndo(p, res, text) });
      ctx.refreshChrome().catch(() => {});
    } catch (e) {
      st.data = before; render();
      announce(failText("unschedule", p, e), { tone: "error" });
      if (e instanceof ApiError && e.status === 409) load({ quiet: true });
    }
  }
  /** One undo for the last move or unschedule: put the post back where it was, unless something moved it again since. */
  function moveUndo(p, res, text) {
    const v = ((res && res.post && res.post.variants) || []).find((x) => x.id === p.variant);
    const afterUtc = v && v.schedule ? v.schedule.utc : null;
    const before = p.at ? { local: p.at.slice(0, 16), tz: p.tz || ctx.tz } : null;
    const entry = {
      label: "Undo", text,
      run: async () => {
        const r = await ctx.api.post("bulk", { op: "set", items: [{ post: p.post, variant: p.variant, to: before, if_utc: afterUtc }] });
        announce(r.refused.length ? `Could not undo: ${r.refused[0].reason}.` : `Undone: "${p.title}" is back ${before ? "on " + humanWhen(before.local) : "in Unscheduled"}.`, { tone: r.refused.length ? "error" : "info" });
        await load({ quiet: true });
        ctx.refreshChrome().catch(() => {});
      },
    };
    setUndo(entry);
    return entry;
  }
  function optimistic(p, date, local) {
    const d = st.data;
    for (const list of Object.values(d.days)) { const i = list.findIndex((x) => x.variant === p.variant); if (i >= 0) list.splice(i, 1); }
    d.unscheduled = (d.unscheduled || []).filter((x) => x.variant !== p.variant);
    (d.days[date] = d.days[date] || []).push({ ...p, at: `${local}:00`, from: undefined, flags: p.flags || [] });
    render();
  }
  function failText(op, p, e) {
    const where = p.at ? `It stays on ${fmtLong(dateKey(p.at))}.` : "It stays in Unscheduled.";
    if (e instanceof ApiError && e.status === 409) {
      const why = e.code === "conflict.slot" ? e.message.replace(/^slot taken/, "That slot is taken") : e.code === "conflict.rev" ? "Someone changed this post first" : e.message;
      return `Could not ${op === "move" ? "move" : "unschedule"} "${p.title}": ${why}. ${where}`;
    }
    return `Could not ${op === "move" ? "move" : "unschedule"} "${p.title}": ${e.message} ${where}`;
  }
  async function acceptProposal(pr) {
    try {
      const res = await ctx.api.acceptProposal(pr.id);
      const n = (res.applied || []).length, bad = (res.conflicts || []).length;
      announce(`Applied ${n} suggested ${n === 1 ? "move" : "moves"}${bad ? `; ${bad} could not be applied` : ""}.`, { tone: bad ? "error" : "info" });
      await load({ quiet: true });
      ctx.refreshChrome().catch(() => {});
    } catch (e) { announce(`Could not apply the suggestion: ${e.message}`, { tone: "error" }); }
  }
  function markLanded(variant) {
    const el = root.querySelector(`.card[data-variant="${CSS.escape(variant)}"]`);
    if (el) { el.classList.add("landed"); setTimeout(() => el.classList.remove("landed"), 700); }
  }

  // ---------- focus
  function rememberFocus() {
    const a = document.activeElement;
    if (!a || !root.contains(a)) return null;
    const c = a.closest(".card");
    if (c && c.dataset.variant) return { variant: c.dataset.variant, mode: c.dataset.mode };
    const d = a.closest(".day");
    return d ? { date: d.dataset.date } : null;
  }
  function restoreFocus(m) { if (m) { st.pendingFocus = m; applyPendingFocus(); } }
  function applyPendingFocus() {
    const m = st.pendingFocus;
    if (!m) return;
    st.pendingFocus = null;
    let el = null;
    if (m.variant) el = root.querySelector(`.card[data-variant="${CSS.escape(m.variant)}"][data-mode="${m.mode || ""}"] .card-link`) || root.querySelector(`.card[data-variant="${CSS.escape(m.variant)}"] .card-link`);
    if (!el && m.date) el = root.querySelector(`.day[data-date="${m.date}"]`);
    if (el) { const cell = el.closest(".day"); if (cell) setRoving(cell.dataset.date, true); el.focus({ preventScroll: false }); }
  }
  function setRoving(date, keepCards) {
    st.focusDate = date;
    for (const c of $$(".day", root)) {
      const on = c.dataset.date === date;
      c.tabIndex = on ? 0 : -1;
      for (const l of $$(".card-link", c)) l.tabIndex = on ? 0 : -1;
    }
  }
  function focusDay(date) {
    const [from, to] = rangeFor(st.view, st.anchor);
    if (date < from || date > to) {                    // walked off the visible range: bring the neighbouring period in
      st.anchor = st.view === "month" ? date : date;
      st.pendingFocus = { date };
      st.focusDate = date;
      persist();
      return load();
    }
    setRoving(date);
    const el = root.querySelector(`.day[data-date="${date}"]`);
    if (el) { el.focus(); }
  }

  // ---------- selection, bulk actions and the side preview
  const isSelectable = (it) => !!it && !!it.variant && !itemLocked(it);
  const itemOf = (li) => (li.dataset.variant ? findItem(li.dataset.variant) : ((st.data && st.data.unscheduled) || []).find((x) => x.post === li.dataset.post && !x.variant) || null);
  const selectedItems = () => [...st.sel].map(findItem).filter(Boolean);
  let refusedNote = null;

  function setSelecting(on) {
    st.selecting = on;
    if (!on) { st.sel.clear(); refusedNote = null; }
    syncSelection();
    announce(on ? "Selecting: click posts to add them." : "Selection cleared.");
  }
  function toggleSelect(variant, on) {
    const it = findItem(variant);
    if (on && !isSelectable(it)) { announce(it ? lockWhy(it) : "That post cannot be selected.", { tone: "error" }); return syncSelection(); }
    if (on) { st.sel.add(variant); st.selecting = true; } else st.sel.delete(variant);
    refusedNote = null;
    syncSelection();
  }
  function pruneSelection() {
    for (const v of [...st.sel]) { const it = findItem(v); if (!it || !isSelectable(it) || (it.account && !st.active.has(it.account)) || !passCampaign(it)) st.sel.delete(v); }
  }
  function syncSelection() {
    for (const li of $$(".card[data-variant]", root)) {
      const v = li.dataset.variant;
      if (!v) continue;
      const on = st.sel.has(v);
      li.classList.toggle("is-selected", on);
      const box = li.querySelector(".sel-box");
      if (box) box.checked = on;
      const a = li.querySelector(".card-link");
      if (a) { const lbl = a.getAttribute("aria-label") || ""; a.setAttribute("aria-label", lbl.replace(/\. Selected$/, "") + (on ? ". Selected" : "")); }
    }
    paintSelection();
  }
  function selectAll() {
    for (const li of $$(".card[data-variant]:not(.mode-ghost)", root)) { const it = li.dataset.variant && findItem(li.dataset.variant); if (isSelectable(it) && !li.closest(".tray")) st.sel.add(it.variant); }
    st.selecting = true;
    syncSelection();
    announce(`${st.sel.size} posts selected.`);
  }
  function paintSelection() {
    const items = selectedItems();
    selToggle.setAttribute("aria-pressed", String(st.selecting));
    root.classList.toggle("selecting", st.selecting);
    document.body.classList.toggle("has-selbar", !!items.length);
    if (!items.length) { selBar.hidden = true; selBar.replaceChildren(); return; }
    const scheduled = items.some((i) => i.at);
    const btn = (label, fn, extra = {}) => h("button", { type: "button", class: "sel-b", onclick: fn, ...extra }, label);
    selBar.hidden = false;
    clear(selBar);
    append(selBar, [
      h("p", { class: "sel-count" }, h("strong", {}, String(items.length)), " selected"),
      h("div", { class: "sel-actions", role: "group", "aria-label": "Do this to the selected posts" },
        btn("Change time…", () => openReschedule(ctx, items.map(asRs), { onDone: afterBulk })),
        btn("−1 day", () => bulk("shift", { days: -1 }), { disabled: !scheduled, title: "Move every scheduled one a day earlier, same time of day" }),
        btn("+1 day", () => bulk("shift", { days: 1 }), { disabled: !scheduled }),
        btn("+1 week", () => bulk("shift", { days: 7 }), { disabled: !scheduled }),
        btn("Next free slots", () => bulk("slots", {}), { title: "Each takes its account's next free slot, in order" }),
        btn("Move to account…", () => chooseAccount(items)),
        btn("Unscheduled", () => bulk("unschedule", {}), { disabled: !scheduled, title: "Take them off the calendar; approvals stay" })),
      h("div", { class: "sel-end" }, btn("Select all in view", selectAll), btn("Clear", () => setSelecting(false), { class: "sel-b sel-clear" })),
      refusedNote ? h("details", { class: "sel-refused", open: true }, h("summary", {}, `${refusedNote.length} not changed`),
        h("ul", {}, refusedNote.map((r) => h("li", {}, h("strong", {}, ((findItem(r.variant) || {}).title) || r.post), ": ", r.reason)))) : null]);
  }
  const asRs = (it) => ({ post: it.post, variant: it.variant, rev: it.rev, title: it.title, at: it.at, tz: it.tz || ctx.tz, account: it.account, status: it.status, flags: it.flags });
  async function afterBulk() { await load({ quiet: true }); ctx.refreshChrome().catch(() => {}); }
  async function bulk(op, extra, items = selectedItems()) {
    if (!items.length) return;
    const body = { op, ...extra, items: items.map((i) => ({ post: i.post, variant: i.variant, expected_rev: i.rev })) };
    try {
      const res = await runBulk(ctx, body, { titles: Object.fromEntries(items.map((i) => [i.variant, i.title])), was: Object.fromEntries(items.map((i) => [i.variant, i.account])), onChange: null });
      await afterBulk();
      st.sel = new Set(res.refused.map((r) => r.variant));        // what was done leaves the selection; what was refused stays, with the reason
      refusedNote = res.refused.length ? res.refused : null;
      if (!st.sel.size) st.selecting = false;
      syncSelection();
      return res;
    } catch (e) { announce(`Could not do that: ${e.message}`, { tone: "error" }); }
  }
  function chooseAccount(items) {
    const sel = h("select", { id: "mv-acct", name: "account" }, accounts.map((a) => h("option", { value: a.id }, `${a.label || a.id} (${a.platform})`)));
    const m = openModal({
      title: `Move ${items.length === 1 ? "this post" : items.length + " posts"} to an account`,
      body: h("div", { class: "rs" },
        h("p", {}, "Only posts that are not in the review queue yet can change account, and only to an account on the same platform. A post with a review item has an approval tied to its account, so it is left alone and you are told why."),
        h("div", { class: "rs-field" }, h("label", { for: "mv-acct" }, "Account"), sel)),
      actions: [h("button", { type: "button", onclick: () => m.close("cancel") }, "Cancel"),
        h("button", { type: "button", class: "primary", onclick: async () => { const a = sel.value; m.close("done"); await bulk("account", { account: a }, items); } }, "Move")],
    });
  }

  function peekItem(it) {
    if (st.selecting && isSelectable(it)) return toggleSelect(it.variant, !st.sel.has(it.variant));
    openPeek(ctx, it, { onChanged: () => load({ quiet: true }), onClose: markPeeked });
    markPeeked(it);
  }
  function markPeeked(it) {
    for (const el of $$(".card.is-peeked", root)) el.classList.remove("is-peeked");
    const tgt = it && it.post ? it : peekTarget();
    if (!tgt || !isPeekOpen()) return;
    for (const el of $$(`.card[data-post="${CSS.escape(tgt.post)}"]`, root)) if ((el.dataset.variant || "") === (tgt.variant || "")) el.classList.add("is-peeked");
  }
  const offs = [];                                          // `main` outlives this page: every listener on it (or the document) is removed on unmount
  const listen = (target, type, fn) => { target.addEventListener(type, fn); offs.push(() => target.removeEventListener(type, fn)); };
  listen(root, "click", (e) => {
    const a = e.target.closest && e.target.closest(".card-link");
    if (!a || e.defaultPrevented || e.button !== 0) return;
    const li = a.closest(".card");
    if (!li || li.classList.contains("mode-ghost")) return;
    const it = itemOf(li);
    if (!it) return;
    e.preventDefault();
    if (e.ctrlKey || e.metaKey || e.shiftKey) {
      if (!isSelectable(it)) return announce(it.variant ? lockWhy(it) : `"${it.title}" has no account yet, so it cannot be selected.`, { tone: "error" });
      return toggleSelect(it.variant, !st.sel.has(it.variant));
    }
    peekItem(it);
  });
  const onEsc = (e) => {                                  // Esc closes the preview first; with nothing open it clears the selection
    if (e.key !== "Escape" || e.defaultPrevented || mover.active || ctx.dnd.active || document.querySelector("dialog[open]")) return;
    if (isPeekOpen()) { e.preventDefault(); closePeek(); } else if (st.sel.size || st.selecting) { e.preventDefault(); setSelecting(false); }
  };
  listen(document, "keydown", onEsc);
  const unsubUndo = onUndoChange((u) => { undoBtn.hidden = !u; undoBtn.title = u ? u.text || "" : ""; undoBtn.setAttribute("aria-label", u ? `Undo: ${u.text || "the last change"}` : "Undo"); });
  { const u = getUndo(); undoBtn.hidden = !u; if (u) undoBtn.title = u.text || ""; }

  // ---------- keyboard
  const movables = () => $$(".card:not(.is-locked):not(.mode-ghost)", root);
  function focusedItem() {
    const a = document.activeElement;
    const c = a && a.closest && a.closest(".card");
    if (c && c.dataset.variant) return findItem(c.dataset.variant);
    const cell = a && a.closest && a.closest(".day");
    if (cell) { const first = cell.querySelector(".card:not(.mode-ghost) "); return first ? findItem(first.dataset.variant) : null; }
    return null;
  }

  function pickUp(it) {
    if (!it) return announce("Focus a post first, then press m.");
    if (itemLocked(it)) return announce(lockWhy(it));
    if (!it.variant) return announce(`"${it.title}" has no account yet, so it cannot be scheduled. Open it to add one.`, { tone: "error" });
    const [from, to] = rangeFor(st.view, st.anchor);
    const all = eachDay(from, to);
    const payload = { ...it };
    const startDate = it.at ? dateKey(it.at) : (all.includes(ctx.today) ? ctx.today : all[0]);
    mover.begin(payload, {
      describe: (p) => `"${p.title}"`,
      targets: () => buildTargets(payload, all),
      start: (p, ts) => Math.max(0, ts.findIndex((t) => t.kind === "day" && t.date === startDate)),
      nav: kbNav,
      drop: (p, t) => performMove(p, { kind: t.kind, date: t.date, time: t.time || (p.at ? timeOf(p.at) : defaultTime(p, t.date, st.data)), label: t.label }),
    });
  }
  function buildTargets(p, all) {
    const out = [];
    for (const d of all) {
      const cell = root.querySelector(`.day[data-date="${d}"]`);
      if (cell) out.push({ el: cell, kind: "day", date: d, label: `${fmtLong(d)}, keeps ${p.at ? timeOf(p.at) : "its time"}${(st.data.slots[d] || []).some((s) => s.free && s.account === p.account) ? "; the nearest free slot is used" : ""}` });
      for (const s of (d < ctx.today ? [] : st.data.slots[d] || [])) {
        if (!s.free || s.account !== p.account) continue;
        const el = root.querySelector(`.day[data-date="${d}"] .slot[data-slot-at="${CSS.escape(s.at)}"]`);
        if (el) out.push({ el, kind: "slot", date: d, time: timeOf(s.at), label: `${fmtLong(d)} at ${timeOf(s.at)}, free slot` });
      }
    }
    return out;
  }
  function kbNav(e, i, ts) {
    const dir = { ArrowLeft: -1, ArrowUp: -1, ArrowRight: 1, ArrowDown: 1 }[e.key];
    if (!dir) return null;
    if (e.shiftKey) {                     // one day (Left/Right) or one week (Up/Down) at a time
      const span = e.key === "ArrowUp" || e.key === "ArrowDown" ? 7 : 1;
      const date = addDays(ts[i].date, dir * span);
      const j = ts.findIndex((t) => t.kind === "day" && t.date === date);
      return j < 0 ? i : j;
    }
    const wantSlots = ts.some((t) => t.kind === "slot");
    for (let j = i + dir; j >= 0 && j < ts.length; j += dir) if (!wantSlots || ts[j].kind === "slot") return j;
    return i;
  }

  function onKey(e) {
    if (mover.active || e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey) return;
    const inCell = e.target.closest && e.target.closest(".day");
    const inTray = !inCell && e.target.closest && e.target.closest(".tray");         // the Unscheduled tray takes the per-post keys too (m, r, n, x, o)
    if (!inCell && !inTray) return;
    const onCard = !!e.target.closest(".card");
    const date = inCell ? inCell.dataset.date : null;
    const k = e.key;
    if (k === "m") { e.preventDefault(); return pickUp(focusedItem()); }
    if (k === "u") { e.preventDefault(); const it = focusedItem(); if (!it) return; if (itemLocked(it)) return announce(lockWhy(it)); return performUnschedule(it); }
    if (k === "t") { e.preventDefault(); return jump(ctx.today); }
    if (k === "x") { e.preventDefault(); const it = focusedItem(); if (!it || !it.variant) return announce("Focus a post first, then press x to select it."); if (!isSelectable(it)) return announce(lockWhy(it)); return toggleSelect(it.variant, !st.sel.has(it.variant)); }
    if (k === "r") {
      e.preventDefault();
      const it = focusedItem();
      const items = st.sel.size ? selectedItems() : it && isSelectable(it) ? [it] : [];
      if (!items.length) return announce(it && it.variant ? lockWhy(it) : "Focus a post first, then press r to change its time.");
      return openReschedule(ctx, items.map(asRs), { onDone: afterBulk });
    }
    if (k === "n") {
      e.preventDefault();
      const it = focusedItem();
      if (st.sel.size) return bulk("slots", {});
      if (!it || !isSelectable(it)) return announce(it && it.variant ? lockWhy(it) : "Focus a post first, then press n for its next free slot.");
      return nextFreeSlot(ctx, asRs(it), { onChange: afterBulk }).catch((err) => announce(err.message, { tone: "error" }));
    }
    if (k === "o") { const a = e.target.closest(".card-link"); if (a) { e.preventDefault(); location.hash = a.getAttribute("href"); } return; }
    if (!inCell) return;                                                               // everything below walks the day grid
    if (k === "PageDown") { e.preventDefault(); return step(1); }
    if (k === "PageUp") { e.preventDefault(); return step(-1); }
    if (k === "Enter" && !onCard) {
      const first = inCell.querySelector(".card-link");
      e.preventDefault();
      if (first) first.focus(); else announce(`${fmtLong(date)}: nothing scheduled.`);
      return;
    }
    const mv = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: st.view === "month" ? -7 : (isNarrow() ? -1 : 0), ArrowDown: st.view === "month" ? 7 : (isNarrow() ? 1 : 0) }[k];
    if (mv === undefined) return;
    e.preventDefault();
    if (mv === 0) {                          // wide week/day: Up/Down walk the cards inside one day column
      const links = $$(".card-link", inCell);
      const at = links.indexOf(e.target.closest(".card-link"));
      const next = links[k === "ArrowDown" ? at + 1 : at - 1];
      if (next) next.focus();
      return;
    }
    focusDay(addDays(date, mv));
  }
  listen(root, "keydown", onKey);
  listen(root, "focusin", (e) => { const c = e.target.closest && e.target.closest(".day"); if (c && c.dataset.date !== st.focusDate && !mover.active) setRoving(c.dataset.date); });
  const onResize = () => { if (st.data) render(); };
  let lastNarrow = isNarrow();
  const mq = matchMedia("(max-width: 899px)");
  listen(mq, "change", () => { if (lastNarrow !== isNarrow()) { lastNarrow = isNarrow(); onResize(); } });

  persist();
  paintAccounts();
  await load();
  return {
    refresh: () => load({ quiet: true }),
    unmount() { alive = false; mover.cancel(true); ctx.dnd.reset(); unsubUndo(); offs.forEach((off) => off()); closePeek({ restore: false }); document.body.classList.remove("has-selbar"); },
  };
}
