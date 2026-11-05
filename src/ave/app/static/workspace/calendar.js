// Calendar: the release dates, like Monday's calendar view. A month or a week of days; each day lists the studio's posts at their
// times (a solid status colour, the channel and the time) and the artifacts due that day (their stage colour). It keeps what the
// old studio calendar does: an Unscheduled tray, filters by brand, account and campaign, Claude's suggested moves as ghost cards
// with Accept, several posts selected and moved at once from a batch bar, a post opens its sheet (preview, why it has its status,
// Change time, Next free slot), and dragging a post or an artifact to another day moves it. Every move is undoable from the toast.
// Times come from the server: a drop sends a wish and the calendar reads the truth back. Claude's changes arrive as `posts` events
// on the Workspace stream (ctx.onPosts); nothing here polls.
//
// The periods, what can move and the result sentences are the old calendar's own logic (static/plan.js), shared, not copied.

import { h, button, icon, toast, busy, popover, typing } from "../dom.js";
import { menu, plain, stageVar, fmtDate, parseDay } from "./common.js";
import { statusTone, statusWord, platformMark, platformWord, asItem, retime, changeTime } from "./posts.js";
import { rangeFor, eachDay, stepAnchor, itemLocked, lockWhy, oneItem, humanWhen, bulkItems, proposalVisible, acceptProposal, acceptedText,
  slotLabel } from "/app/shared/plan.js";
import { fmtDayNum, fmtDow, fmtLong, fmtMonth, fmtShort, fmtMonthName, dateKey, timeOf, daysBetween } from "/app/shared/dom.js";
const MONTH_ITEMS = 4;                  // a month cell shows this many, then "+N more"

export function createCalendar(ctx, studio, sheet) {
  const { S } = ctx;
  const P = (S.cal ||= { mode: "month", anchor: null, filter: null, campaign: null, tray: true });
  let data = null, error = null, loading = false, seq = 0, alive = false, stale = false, campaigns = null;
  const sel = new Set();                    // variant ids of the selected posts
  let selecting = false, refused = [];

  const title = h("h2", { class: "cal-title", id: "cal-title", "aria-live": "polite" });
  const today = button("Today", { variant: "secondary", size: "sm", onclick: () => { P.anchor = studio.st.today; save(); load(); } });
  const prev = button("Previous", { variant: "tertiary", size: "sm", iconName: "right", iconOnly: true, class: "cal-prev", onclick: () => step(-1) });
  prev.querySelector(".icon").style.transform = "rotate(180deg)";
  const next = button("Next", { variant: "tertiary", size: "sm", iconName: "right", iconOnly: true, onclick: () => step(1) });
  const seg = h("div", { class: "seg-toggle", role: "group", "aria-label": "Calendar period" },
    ["month", "week"].map((m) => h("button", { type: "button", class: "seg-opt", dataset: { mode: m }, "aria-pressed": String(P.mode === m), onclick: () => setMode(m) }, m === "month" ? "Month" : "Week")));
  const filterBtn = h("button", { type: "button", class: "btn btn-tertiary btn-tool cal-filter", "aria-haspopup": "menu", "aria-expanded": "false",
    onclick: (e) => filterMenu(e.currentTarget) }, icon("filter"), h("span", { class: "tool-label" }, "All accounts"));
  const campBtn = h("button", { type: "button", class: "btn btn-tertiary btn-tool cal-camp", "aria-haspopup": "menu", "aria-expanded": "false",
    onclick: (e) => campaignMenu(e.currentTarget) }, icon("group"), h("span", { class: "tool-label" }, "All campaigns"));
  const selBtn = h("button", { type: "button", class: "btn btn-tertiary btn-tool cal-selbtn", "aria-pressed": "false", onclick: () => setSelecting(!selecting) },
    icon("check"), h("span", { class: "tool-label" }, "Select"));
  const trayBtn = h("button", { type: "button", class: "btn btn-tertiary btn-tool cal-traybtn", "aria-pressed": String(P.tray), onclick: () => { P.tray = !P.tray; save(); paint(); } },
    icon("inbox"), h("span", { class: "tool-label" }, "Unscheduled"), h("span", { class: "tool-count" }));
  const sub = h("p", { class: "cal-sub secondary-text" });
  const bar = h("div", { class: "cal-bar" }, h("div", { class: "cal-nav" }, today, prev, next, title), h("div", { class: "cal-tools" }, selBtn, filterBtn, campBtn, trayBtn, seg));
  const gridHost = h("div", { class: "cal-host" });
  const tray = h("aside", { class: "cal-tray", "aria-labelledby": "cal-tray-h" });
  const notice = h("div", { class: "cal-notice", hidden: true });
  const batch = h("div", { class: "batch cal-batch", role: "region", "aria-label": "Selected posts", hidden: true });
  const el = h("div", { class: "cal", "aria-busy": "false" }, bar, sub, notice, h("div", { class: "cal-layout" }, gridHost, tray), batch);

  const save = () => ctx.savePrefs();
  const anchor = () => P.anchor || studio.st.today;
  function step(n) { P.anchor = stepAnchor(P.mode, anchor(), n); save(); load(); }
  function setMode(m) { if (P.mode === m) return; P.mode = m; save(); for (const b of seg.children) b.setAttribute("aria-pressed", String(b.dataset.mode === m)); load(); }

  // ---------------------------------------------------------------- which posts: the brand or account filter, the search, the project
  const accounts = () => studio.st.accounts;
  const brands = () => [...new Set(accounts().map((a) => a.brand).filter(Boolean))];
  function activeIds() {
    const f = P.filter;
    if (!f) return accounts().map((a) => a.id);
    if (f.brand) return accounts().filter((a) => a.brand === f.brand).map((a) => a.id);
    return accounts().some((a) => a.id === f.account) ? [f.account] : accounts().map((a) => a.id);
  }
  const filterLabel = () => (P.filter?.brand ? P.filter.brand : P.filter?.account ? studio.accountLabel(P.filter.account) : "All accounts");
  function filterMenu(anchor) {
    const pick = (f) => { P.filter = f; save(); load(); };
    const items = [{ label: "All accounts", checked: !P.filter, onSelect: () => pick(null) }];
    if (brands().length) { items.push("-"); for (const b of brands()) items.push({ label: b, checked: P.filter?.brand === b, onSelect: () => pick({ brand: b }) }); }
    items.push("-");
    for (const a of accounts()) items.push({ label: a.label || a.id, lead: platformMark(a.platform), checked: P.filter?.account === a.id, onSelect: () => pick({ account: a.id }) });
    menu(anchor, items, { label: "Show posts for", heading: "Show posts for", align: "end" });
  }
  const postsHere = () => {
    if (!S.route.project) return true;
    return ctx.projects.get(S.route.project)?.kind === "studio";
  };
  const words = () => S.q.split(/\s+/).filter(Boolean);
  const passQ = (t) => words().every((w) => String(t || "").toLowerCase().includes(w));
  const filtersOn = () => !!P.filter || !!P.campaign || !!S.q;
  const passCampaign = (it) => !P.campaign || (!!it && it.campaign === P.campaign);
  const shows = (it) => passQ(it.title) && passCampaign(it);
  const campaignName = (id) => (campaigns || []).find((c) => c.id === id)?.name || "Campaign";
  function campaignMenu(anchor) {
    const pick = (id) => { P.campaign = id; save(); prune(); paint(); };
    menu(anchor, [{ label: "All campaigns", checked: !P.campaign, onSelect: () => pick(null) }, "-",
      ...(campaigns || []).map((c) => ({ label: c.name, checked: P.campaign === c.id, onSelect: () => pick(c.id) }))],
    { label: "Campaign", heading: "Show the posts of", align: "end" });
  }
  const isLate = (r) => !!r.due && r.stage !== "Out" && r.due < studio.st.today;      // late by the studio's today, as the calendar shows it

  // ---------------------------------------------------------------- reading
  async function load({ quiet = false } = {}) {
    const my = ++seq;
    loading = !data || !quiet;
    el.setAttribute("aria-busy", "true");
    if (!data) paint();
    try {
      await studio.ready();
      if (!campaigns) campaigns = await studio.api.get("campaigns").then((r) => (r.campaigns || []).map((c) => ({ id: c.campaign.id, name: c.campaign.name }))).catch(() => []);
      const [from, to] = rangeFor(P.mode, anchor());
      const got = await studio.api.get(`calendar?from=${from}&to=${to}&accounts=${encodeURIComponent(activeIds().join(","))}`);
      if (my !== seq) return;
      data = { ...got, from, to }; error = null;
      prune();
    } catch (e) {
      if (my !== seq) return;
      error = e;
      if (data && !quiet) toast(plain(e, "read the calendar"), { tone: "negative" });
    } finally {
      if (my === seq) { loading = false; el.setAttribute("aria-busy", "false"); if (alive) paintSoon(); }
    }
  }
  // ---------------------------------------------------------------- painting
  let held = false;                         // a live update never swaps a day out from under the pointer: it waits for the drop
  const paintSoon = () => { if (dragging) held = true; else paint(); };
  function paint() {
    if (!alive) return;
    const focused = el.contains(document.activeElement) ? document.activeElement.closest?.(".cal-item.is-post")?.dataset.variant : null;
    paintInner();
    if (focused) el.querySelector(`.cal-item.is-post[data-variant="${CSS.escape(focused)}"]`)?.focus({ preventScroll: true });   // a live repaint keeps the keyboard's place
  }
  function paintInner() {
    if (!studio.st.today) {                    // the studio's own today is not known yet (or the studio is not answering)
      title.textContent = "";
      if (!error) { gridHost.replaceChildren(skeleton()); tray.hidden = trayBtn.hidden = true; return; }
      gridHost.replaceChildren(h("div", { class: "ws-error", role: "alert" }, h("h2", {}, "The calendar could not load"),
        h("p", { class: "secondary-text" }, `${error.message || "The studio did not answer."} Your posts are safe on disk.`),
        button("Try again", { variant: "primary", onclick: (e) => busy(e.currentTarget, () => load()) })));
      tray.hidden = trayBtn.hidden = true;
      return;
    }
    const [from, to] = data ? [data.from, data.to] : rangeFor(P.mode, anchor());
    title.textContent = P.mode === "month" ? fmtMonth(anchor()) : `${fmtShort(from)} – ${fmtShort(to)}`;
    filterBtn.querySelector(".tool-label").textContent = filterLabel();
    filterBtn.classList.toggle("is-on", !!P.filter);
    filterBtn.hidden = !postsHere() || accounts().length < 2;
    campBtn.hidden = !postsHere() || !(campaigns || []).length;
    campBtn.querySelector(".tool-label").textContent = P.campaign ? campaignName(P.campaign) : "All campaigns";
    campBtn.classList.toggle("is-on", !!P.campaign);
    selBtn.hidden = !postsHere();
    for (const b of seg.children) b.setAttribute("aria-pressed", String(b.dataset.mode === P.mode));
    if (loading && !data) { gridHost.replaceChildren(skeleton()); tray.hidden = true; trayBtn.hidden = true; return; }
    const studioDown = !!error && postsHere();
    notice.hidden = !studioDown;
    if (studioDown) {
      notice.replaceChildren(h("div", { class: "notice notice-negative", role: "alert" }, icon("alert"), h("div", { class: "notice-body" },
        h("p", { class: "notice-title" }, data ? "The calendar could not read the latest posts" : "The posts could not be read"),
        h("p", { class: "notice-text" }, `${error.message || "The studio did not answer."} Due dates still show. Your posts are safe on disk.`),
        h("div", { class: "notice-actions" }, button("Try again", { variant: "primary", size: "sm", onclick: (e) => busy(e.currentTarget, () => load()) })))));
    }
    const days = eachDay(from, to);
    const posts = postsHere() && data ? data : { days: {}, unscheduled: [] };
    const byDay = {};
    for (const d of days) byDay[d] = [];
    for (const [d, list] of Object.entries(posts.days || {})) for (const it of list) if (byDay[d] && shows(it)) byDay[d].push({ type: "post", it });
    const proposals = postsHere() && data ? (data.proposals || []).filter((pr) => proposalVisible(pr, (v) => { const b = findItem(v); return b && shows(b); })) : [];
    const hiddenProposals = postsHere() && data ? (data.proposals || []).length - proposals.length : 0;
    for (const pr of proposals) for (const mv of pr.moves) { const d = mv.to.local.slice(0, 10); if (byDay[d]) byDay[d].push({ type: "ghost", pr, mv, it: { at: mv.to.local } }); }
    for (const r of ctx.visible()) if (r.due && byDay[r.due]) byDay[r.due].push({ type: "due", r });
    for (const d of days) byDay[d].sort((a, b) => (a.type !== "due" ? a.it.at : "99") < (b.type !== "due" ? b.it.at : "99") ? -1 : 1);
    const shown = Object.values(byDay).reduce((n, l) => n + l.length, 0);
    const unscheduled = (posts.unscheduled || []).filter(shows);
    sub.textContent = postsHere()
      ? `${studio.st.tz} time. ${count(shown, "item")} in this ${P.mode}${unscheduled.length ? `, ${unscheduled.length} waiting in Unscheduled` : ""}.`
      : `Due dates in ${ctx.projects.get(S.route.project)?.title || "this project"}. Posts are planned in the studio.`;

    const grid = h("div", { class: `cal-grid is-${P.mode}`, role: "grid", "aria-labelledby": "cal-title" });
    grid.append(h("div", { class: "cal-dow", role: "row" }, days.slice(0, 7).map((d) => h("div", { class: "cal-dowcell", role: "columnheader", "aria-label": fmtLong(d).split(" ")[0] }, fmtDow(d)))));
    for (let i = 0; i < days.length; i += 7) {
      grid.append(h("div", { class: "cal-week", role: "row" }, days.slice(i, i + 7).map((d) => dayCell(d, byDay[d]))));
    }
    const wrap = [grid];
    if (!shown && !unscheduled.length && data) {
      wrap.push(filtersOn()
        ? h("div", { class: "cal-empty", role: "status" }, h("h3", {}, "Nothing matches these filters"),
          h("p", { class: "secondary-text" }, "No post or due date in this period fits the search and accounts you chose. Other periods may still have some."),
          button("Clear filters", { variant: "primary", size: "sm", onclick: () => { P.filter = null; P.campaign = null; save(); ctx.clearSearch(); load(); } }))
        : h("p", { class: "cal-quiet secondary-text", role: "status" }, `Nothing is planned in ${P.mode === "month" ? fmtMonthName(anchor()) : "this week"} yet. Ask Claude to plan some posts, or drag one in from Unscheduled.`));
    }
    gridHost.replaceChildren(...wrap);
    paintTray(unscheduled, proposals, hiddenProposals);
    paintBatch();
  }
  const count = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;

  function dayCell(d, list) {
    const isToday = d === studio.st.today;
    const outside = P.mode === "month" && d.slice(0, 7) !== anchor().slice(0, 7);
    const cell = h("div", { class: `cal-day${isToday ? " is-today" : ""}${outside ? " is-outside" : ""}${d < studio.st.today ? " is-past" : ""}`, role: "gridcell",
      dataset: { date: d }, "aria-label": `${fmtLong(d)}${isToday ? ", today" : ""}. ${count(list.length, "item")}` },
      h("div", { class: "cal-dayhead" }, h("span", { class: "cal-num" }, fmtDayNum(d)), fmtDayNum(d) === "1" && P.mode === "month" ? h("span", { class: "cal-mon" }, fmtMonthName(d).slice(0, 3)) : null));
    const ul = h("ul", { class: "cal-items", "aria-label": fmtLong(d) });
    const cap = P.mode === "month" && list.length > MONTH_ITEMS + 1 ? MONTH_ITEMS : list.length;
    for (const x of list.slice(0, cap)) ul.append(h("li", {}, x.type === "post" ? postItem(x.it) : x.type === "ghost" ? ghostItem(x.pr, x.mv) : dueItem(x.r)));
    if (cap < list.length) {
      ul.append(h("li", {}, h("button", { type: "button", class: "cal-more", onclick: (e) => moreMenu(e.currentTarget, d, list) }, `+${list.length - cap} more`)));
    }
    cell.append(ul);
    dropTarget(cell, d);
    return cell;
  }
  function moreMenu(anchor, d, list) {
    menu(anchor, list.filter((x) => x.type !== "ghost").map((x) => x.type === "post"
      ? { label: `${timeOf(x.it.at)} ${x.it.title}`, lead: h("span", { class: "dot", style: { color: statusTone(x.it.status) } }), onSelect: () => openPost(x.it) }
      : { label: `Due: ${x.r.name}`, lead: h("span", { class: "dot", style: { color: stageVar(x.r.stage) } }), onSelect: () => ctx.openPanel(x.r.key) }),
    { label: fmtLong(d), heading: fmtLong(d) });
  }

  function postItem(it, { tray: inTray = false } = {}) {
    const locked = itemLocked(it);
    const acct = studio.account(it.account);
    const label = `${it.title}. ${statusWord(it.status)}. ${it.at ? humanWhen(it.at) : "Not scheduled"}${it.account ? `. ${studio.accountLabel(it.account)}` : ""}${locked ? `. ${lockWhy(it)}` : ""}`;
    const on = sel.has(it.variant);
    const b = h("button", { type: "button", class: `cal-item is-post${locked ? " is-locked" : ""}${inTray ? " is-tray" : ""}${on ? " is-selected" : ""}`,
      style: { "--tone": statusTone(it.status) }, dataset: { post: it.post, variant: it.variant || "", status: it.status },
      "aria-label": label, "aria-pressed": selecting || on ? String(on) : null, title: locked ? lockWhy(it) : label, "aria-keyshortcuts": locked ? null : "x r n",
      draggable: locked || !it.variant ? "false" : "true", onclick: (e) => (selecting || e.metaKey || e.ctrlKey || e.shiftKey ? toggle(it) : openPost(it)) },
      on ? h("span", { class: "cal-check", "aria-hidden": "true" }, icon("check")) : null,
      platformMark(acct?.platform),
      inTray ? null : h("span", { class: "cal-time" }, it.at ? timeOf(it.at) : ""),
      h("span", { class: "cal-name" }, it.title),
      locked ? icon("lock") : null,
      (it.flags || []).length ? h("span", { class: "cal-flag", "aria-hidden": "true" }, icon("flag")) : null);
    if (!locked && it.variant) dragSource(b, { type: "post", it });
    return b;
  }
  function ghostItem(pr, mv) {
    const base = findItem(mv.variant);
    const name = base ? base.title : "a post";
    return h("div", { class: "cal-item is-ghost", dataset: { proposal: pr.id, ghostVariant: mv.variant }, title: mv.why || "" },
      icon("spark"), h("span", { class: "cal-time" }, mv.to.local.slice(11, 16)), h("span", { class: "cal-name" }, name),
      h("button", { type: "button", class: "cal-accept", "aria-label": `Accept Claude's suggestion: move ${name} to ${fmtLong(mv.to.local.slice(0, 10))} at ${mv.to.local.slice(11, 16)}`,
        onclick: (e) => accept(pr, e.currentTarget) }, "Accept"));
  }
  async function accept(pr, btn) {
    await busy(btn, async () => {
      try {
        const res = await acceptProposal(studio.api, pr.id);
        toast(acceptedText(res), { tone: (res.conflicts || []).length ? "negative" : "positive" });
      } catch (e) { toast(`Could not apply the suggestion: ${e.message}`, { tone: "negative" }); }
      await load({ quiet: true });
    });
  }
  function findItem(variant) {
    if (!data) return null;
    for (const list of Object.values(data.days || {})) for (const it of list) if (it.variant === variant) return it;
    return (data.unscheduled || []).find((it) => it.variant === variant) || null;
  }
  function dueItem(r) {
    const p = ctx.projectOf(r);
    const late = isLate(r);
    const b = h("button", { type: "button", class: `cal-item is-due${late ? " is-late" : ""}`, style: { "--tone": stageVar(r.stage) }, dataset: { key: r.key },
      "aria-label": `${r.name}, due ${fmtDate(r.due)}${late ? ", late" : ""}. ${r.stage}. ${p?.title || ""}`, draggable: "true", onclick: () => ctx.openPanel(r.key) },
      h("span", { class: "cal-stage" }, r.stage), h("span", { class: "cal-name" }, r.name));
    dragSource(b, { type: "due", r });
    return b;
  }
  function paintTray(list, proposals = [], hiddenProposals = 0) {
    const show = postsHere() && !!data;
    trayBtn.hidden = !show;
    trayBtn.setAttribute("aria-pressed", String(P.tray));
    const c = trayBtn.querySelector(".tool-count"); c.hidden = !list.length; c.textContent = list.length ? String(list.length) : "";
    tray.hidden = !show || !P.tray;
    if (tray.hidden) return;
    const ul = h("ul", { class: "cal-traylist", "aria-label": "Unscheduled posts" }, list.map((it) => h("li", {}, postItem(it, { tray: true }))));
    const suggest = proposals.length ? h("section", { class: "cal-suggest", "aria-labelledby": "cal-sug-h" },
      h("div", { class: "cal-trayhead" }, icon("spark"), h("h3", { id: "cal-sug-h" }, "Claude suggests")),
      proposals.map((pr) => h("div", { class: "cal-sugitem", dataset: { proposal: pr.id } },
        h("ul", {}, pr.moves.map((mv) => { const base = findItem(mv.variant);
          return h("li", {}, h("strong", {}, base ? base.title : "A post"),
            h("span", { class: "secondary-text" }, `${base && base.at ? fmtShort(dateKey(base.at)) : "Unscheduled"} to ${slotLabel(mv.to.local)}`),
            mv.why ? h("span", { class: "cal-why" }, mv.why) : null); })),
        button(pr.moves.length > 1 ? `Accept ${pr.moves.length} moves` : "Accept move", { variant: "primary", size: "sm", onclick: (e) => accept(pr, e.currentTarget) }))),
      h("p", { class: "secondary-text cal-trayhelp" }, "Accepting moves the posts. Their approvals stay: they cover the words and pictures, not the time.")) : null;
    tray.replaceChildren(...[suggest,
      hiddenProposals ? h("p", { class: "secondary-text cal-trayhelp" }, `${hiddenProposals} ${hiddenProposals === 1 ? "suggestion is" : "suggestions are"} hidden: ${hiddenProposals === 1 ? "it moves" : "they move"} posts outside these filters.`) : null,
      h("div", { class: "cal-trayhead" }, h("h3", { id: "cal-tray-h" }, "Unscheduled"), h("span", { class: "chip chip-count" }, String(list.length))),
      list.length ? ul : h("p", { class: "cal-trayempty secondary-text" }, "Nothing waiting. Drag a post here to take it off the calendar."),
      h("p", { class: "cal-trayhelp secondary-text" }, "Drag a post onto a day, or open it and choose Change time.")].filter(Boolean));
  }
  function skeleton() {
    return h("div", { class: "cal-grid is-skel", "aria-hidden": "true" },
      h("div", { class: "cal-dow" }, Array.from({ length: 7 }, () => h("div", { class: "cal-dowcell" }, h("span", { class: "skeleton skeleton-text", style: { width: "28px" } })))),
      Array.from({ length: P.mode === "month" ? 5 : 1 }, () => h("div", { class: "cal-week" }, Array.from({ length: 7 }, () =>
        h("div", { class: "cal-day" }, h("span", { class: "skeleton skeleton-text", style: { width: "18px" } }), h("span", { class: "skeleton cal-skelitem" }))))));
  }

  // ---------------------------------------------------------------- opening and moving
  function openPost(it) {
    if (dragging) return;
    sheet.open({ post: it.post, variant: it.variant, title: it.title, account: it.account, status: it.status }, { changed: () => load({ quiet: true }) });
  }
  let dragging = null;
  function dragSource(elm, payload) {
    elm.addEventListener("dragstart", (e) => {
      dragging = payload;
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", payload.type === "post" ? payload.it.title : payload.r.name);
      el.classList.add("is-dragging");
      requestAnimationFrame(() => elm.classList.add("is-lifted"));
    });
    elm.addEventListener("dragend", () => {
      elm.classList.remove("is-lifted"); el.classList.remove("is-dragging");
      for (const c of el.querySelectorAll(".is-drop")) c.classList.remove("is-drop");
      setTimeout(() => { dragging = null; if (held) { held = false; paint(); } }, 0);
    });
  }
  function dropTarget(target, date) {
    const ok = () => dragging && (date !== null || (dragging.type === "post" && !!dragging.it.at));
    target.addEventListener("dragover", (e) => { if (!ok()) return; e.preventDefault(); e.dataTransfer.dropEffect = "move"; target.classList.add("is-drop"); });
    target.addEventListener("dragleave", (e) => { if (!target.contains(e.relatedTarget)) target.classList.remove("is-drop"); });
    target.addEventListener("drop", (e) => {
      if (!ok()) return;
      e.preventDefault();
      target.classList.remove("is-drop");
      const p = dragging; dragging = null;
      if (p.type === "due") return moveDue(p.r, date);
      return movePost(p.it, date);
    });
  }
  function freeTimeOn(it, d) {
    const s = ((data && data.slots && data.slots[d]) || []).find((x) => x.free && x.account === it.account);
    return s ? timeOf(s.at) : "09:00";
  }
  async function movePost(it, date) {
    const item = asItem(it, studio);
    if (date === null) {
      return retime(studio, oneItem(item, "set", null), { titles: { [it.variant]: it.title }, onChange: () => load({ quiet: true }) })
        .catch((e) => toast(plain(e, "move it"), { tone: "negative" }));
    }
    const local = `${date}T${it.at ? timeOf(it.at) : freeTimeOn(it, date)}`;
    if (it.at && String(it.at).slice(0, 16) === local) return;
    optimistic(it, date, local);
    return retime(studio, oneItem(item, "set", { local, tz: item.tz }), { titles: { [it.variant]: it.title }, onChange: () => load({ quiet: true }) })
      .catch((e) => { toast(plain(e, "move it"), { tone: "negative" }); load({ quiet: true }); });
  }
  function optimistic(it, date, local) {
    if (!data) return;
    for (const list of Object.values(data.days)) { const i = list.findIndex((x) => x.variant === it.variant); if (i >= 0) list.splice(i, 1); }
    data.unscheduled = (data.unscheduled || []).filter((x) => x.variant !== it.variant);
    (data.days[date] ||= []).push({ ...it, at: `${local}:00` });
    paint();
  }
  function moveDue(r, date) {
    if (!date || r.due === date) return;
    ctx.act.fields([r], { due: date }, { say: `${r.name} is due ${fmtDate(date)}` });
  }

  dropTarget(tray, null);                    // once: the tray outlives every paint

  // ---------------------------------------------------------------- several posts at once: select, then the batch bar
  const selectable = (it) => !!it && !!it.variant && !itemLocked(it);
  const selected = () => [...sel].map(findItem).filter(Boolean);
  function setSelecting(on) {
    selecting = on;
    if (!on) { sel.clear(); refused = []; }
    selBtn.setAttribute("aria-pressed", String(on));
    selBtn.classList.toggle("is-on", on);
    paint();
  }
  function toggle(it) {
    if (!selectable(it)) { toast(it && it.variant ? lockWhy(it) : `“${it.title}” has no account yet, so it cannot be selected.`, { tone: "negative" }); return; }
    if (sel.has(it.variant)) sel.delete(it.variant); else sel.add(it.variant);
    refused = [];
    if (sel.size && !selecting) { selecting = true; selBtn.setAttribute("aria-pressed", "true"); selBtn.classList.add("is-on"); }
    paint();
    el.querySelector(`.cal-item[data-variant="${CSS.escape(it.variant)}"]`)?.focus({ preventScroll: true });
  }
  function prune() { for (const v of [...sel]) { const it = findItem(v); if (!selectable(it) || !shows(it)) sel.delete(v); } }
  async function bulk(op, extra = {}, items = selected(), btn = null) {
    if (!items.length) return;
    const body = { op, ...extra, items: bulkItems(items) };
    const titles = Object.fromEntries(items.map((i) => [i.variant, i.title]));
    const was = Object.fromEntries(items.map((i) => [i.variant, i.account]));
    const run = () => retime(studio, body, { titles, was, onChange: () => load({ quiet: true }) });
    try {
      const res = btn ? await busy(btn, run) : await run();
      sel.clear();
      for (const r of res.refused) sel.add(r.variant);            // what moved leaves the selection; what was refused stays, with its reason
      refused = res.refused.map((r) => ({ ...r, title: titles[r.variant] || "A post" }));
      if (!sel.size) { selecting = false; selBtn.setAttribute("aria-pressed", "false"); selBtn.classList.remove("is-on"); }
      paint();
    } catch (e) { toast(plain(e, "move them"), { tone: "negative" }); }
  }
  function moveBy(anchor) {
    const items = selected();
    const earliest = items.filter((i) => i.at).map((i) => dateKey(i.at)).sort()[0] || studio.st.today;
    const days = h("input", { class: "input", id: "mb-days", value: "7", inputmode: "numeric", "aria-label": "Days to move (negative moves earlier)" });
    const start = h("input", { class: "input", id: "mb-start", placeholder: "2026-10-26 or Oct 26", "aria-label": "Start on" });
    const err = h("p", { class: "ct-err", role: "alert" });
    const apply = button("Move", { variant: "primary", size: "sm", onclick: () => {
      let n;
      if (start.value.trim()) {
        const d = parseDay(start.value);
        if (!d) { err.textContent = "Write a date like 2026-10-26 or Oct 26."; return; }
        n = daysBetween(earliest, d);
        if (!n) { err.textContent = "The earliest post is already on that date."; return; }
      } else {
        n = parseInt(days.value, 10);
        if (!n) { err.textContent = "Choose how many days, not zero."; return; }
      }
      pop.close(); bulk("shift", { days: n }, items);
    } });
    const pop = popover(anchor, h("form", { class: "ct", onsubmit: (e) => { e.preventDefault(); apply.click(); } }, h("p", { class: "popover-title" }, `Move ${items.length} ${items.length === 1 ? "post" : "posts"}`),
      h("p", { class: "secondary-text ct-now" }, "Every post keeps its time of day. Negative numbers move them earlier."),
      h("div", { class: "ct-fields" }, h("label", { class: "field" }, h("span", { class: "field-label" }, "By days"), days),
        h("label", { class: "field" }, h("span", { class: "field-label" }, "Or start on"), start)), err,
      h("div", { class: "popover-actions" }, button("Cancel", { variant: "tertiary", size: "sm", onclick: () => pop.close() }), apply),
      h("button", { type: "submit", hidden: true, tabindex: "-1" })), { label: "Move the selected posts", modal: true, place: "above" });
    days.focus(); days.select();
  }
  function accountMenu(anchor) {
    const items = selected();
    menu(anchor, accounts().map((a) => ({ label: `${a.label || a.id} (${platformWord(a.platform)})`, lead: platformMark(a.platform), onSelect: () => bulk("account", { account: a.id }, items) })),
      { label: "Move to account", heading: "Only posts not in review yet can change account, on the same platform", place: "above" });
  }
  function paintBatch() {
    const items = selected();
    document.body.classList.toggle("has-calbatch", !!items.length && alive);
    batch.hidden = !items.length || !alive;
    if (batch.hidden) { batch.replaceChildren(); return; }
    const scheduled = items.some((i) => i.at);
    const act = (label, iconName, onclick, extra = {}) => h("button", { type: "button", class: "batch-act", onclick, ...extra }, icon(iconName), h("span", {}, label));
    batch.replaceChildren(
      h("div", { class: "batch-count", "aria-hidden": "true" }, String(items.length)),
      h("div", { class: "batch-what" }, h("span", { class: "batch-title" }, items.length === 1 ? "Post selected" : "Posts selected", h("span", { class: "sr-only" }, `: ${items.length}`)),
        refused.length ? h("span", { class: "cal-refused", role: "status" }, `${refused.length} not moved: `, refused.slice(0, 2).map((r, i) => [i ? "; " : "", h("strong", {}, r.title), `: ${r.reason}`]))
          : h("span", { class: "batch-dots" }, items.slice(0, 14).map((i) => h("span", { class: "dot", style: { color: statusTone(i.status) } })))),
      h("div", { class: "batch-actions" },
        act("Move by…", "calendar", (e) => moveBy(e.currentTarget), { disabled: !scheduled, title: scheduled ? "" : "None of these has a time yet" }),
        act("−1 day", "undo", (e) => bulk("shift", { days: -1 }, items, e.currentTarget), { disabled: !scheduled }),
        act("+1 day", "right", (e) => bulk("shift", { days: 1 }, items, e.currentTarget), { disabled: !scheduled }),
        act("+1 week", "send", (e) => bulk("shift", { days: 7 }, items, e.currentTarget), { disabled: !scheduled }),
        act("Next free slots", "clock", (e) => bulk("slots", {}, items, e.currentTarget)),
        act("Move to account", "person", (e) => accountMenu(e.currentTarget)),
        act("Unscheduled", "inbox", (e) => bulk("unschedule", {}, items, e.currentTarget), { disabled: !scheduled })),
      button("Clear selection", { variant: "tertiary", iconName: "x", iconOnly: true, class: "batch-close", onclick: () => setSelecting(false) }));
  }
  // keys on a focused post: x selects, r changes time, n takes the next free slot; Esc clears the selection
  el.addEventListener("keydown", (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey || typing(e) || document.querySelector(".popover")) return;
    if (e.key === "Escape" && (sel.size || selecting)) { e.preventDefault(); e.stopPropagation(); setSelecting(false); return; }
    const b = e.target.closest?.(".cal-item.is-post");
    const it = b && findItem(b.dataset.variant);
    if (e.key === "x" && b) { e.preventDefault(); e.stopPropagation(); if (it) toggle(it); return; }
    if (e.key === "r" && (b || sel.size)) {
      e.preventDefault(); e.stopPropagation();
      if (sel.size > 1) return moveBy(batch.querySelector(".batch-act") || b);
      const one = sel.size ? selected()[0] : it;
      if (!selectable(one)) return toast(one ? lockWhy(one) : "Focus a post first, then press r to change its time.");
      return changeTime(b || batch, studio, asItem(one, studio), { onDone: () => load({ quiet: true }) });
    }
    if (e.key === "n" && (b || sel.size)) {
      e.preventDefault(); e.stopPropagation();
      if (sel.size) return bulk("slots", {});
      if (!selectable(it)) return toast(it ? lockWhy(it) : "Focus a post first.");
      return retime(studio, oneItem(asItem(it, studio), "slots"), { titles: { [it.variant]: it.title }, onChange: () => load({ quiet: true }) })
        .catch((er) => toast(plain(er, "move it"), { tone: "negative" }));
    }
  });
  ctx.onPosts(() => { if (alive) load({ quiet: true }); else stale = true; });

  // ---------------------------------------------------------------- the view's interface (workspace.js)
  return {
    el,
    render() {
      if (!alive) { alive = true; stale = false; load(); }
      else paint();
    },
    update() { paintSoon(); return true; },
    leave() { alive = false; seq++; sel.clear(); selecting = false; refused = []; paintBatch(); },
    reload: () => load({ quiet: true }),
  };
}
