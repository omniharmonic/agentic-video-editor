// The studio's posts inside the Workspace: one client for the studio API (/api/studio/), the post's status in Monday's solid
// colours, and the post sheet that opens from the Calendar and the Previews views. The sheet slides in on the right like an item
// panel: a phone preview made by the studio's own preview cards, why the post has its status, its words and alt text, its
// history, and the time controls (Change time, Next free slot, Move to Unscheduled), each undoable from the toast.
//
// Nothing here approves, queues or sends a post: that stays in the approval sheet on the publishing computer. Every retiming goes
// through POST /api/studio/bulk, which applies the same checks as the old calendar's drag and keeps an approval in place.

import { h, button, icon, toast, busy, popover } from "../dom.js";
import { plain, parseDay, ago } from "./common.js";
import { makeApi, findVariant, PLATFORM_NAMES, localUrl } from "/app/shared/panels.js";
import { STATUS, flagInfo, dateKey, timeOf, fmtLong } from "/app/shared/dom.js";
import { loadAssets } from "/app/shared/compose.js";
import { deliveryReceipt } from "/app/shared/delivery-receipt.js";
import { buildPreview, loadPreviewConfig, previewModel } from "/app/shared/previews/index.js";
import {
  statusReason, openNotes, noteHead, postFacts, MISSING_ALT, recentEvents, eventLine, eventWho, summarize, undoRequests, undoSummary,
  oneItem, freeSlots, noSlotsText, slotLabel, humanWhen, itemLocked, lockWhy, buildPackage, packageHistory, postName, packageState,
  publicationRequest, newRequestId,
} from "/app/shared/plan.js";

// Monday's status colours: a solid fill with white text. The words are the studio's own (old dom.js STATUS).
const TONE = { draft: "var(--stage-plan)", in_review: "var(--group-6)", approved: "var(--stage-finish)", scheduled: "var(--positive)",
  publishing: "var(--sync-busy)", published: "var(--stage-out)", failed: "var(--negative)", missed: "var(--negative)",
  needs_rework: "var(--stage-draft)", dropped: "var(--sync-wait)" };
export const statusWord = (s) => (STATUS[s] || [String(s || "No account yet").replace(/_/g, " ")])[0];
export const statusTone = (s) => TONE[s] || "var(--sync-wait)";
export function statusPill(s, { cls = "" } = {}) {
  return h("span", { class: `pill post-pill ${cls}`.trim(), style: { background: statusTone(s) }, dataset: { status: s || "none" } }, statusWord(s));
}
export const platformWord = (p) => PLATFORM_NAMES[p] || (p ? p[0].toUpperCase() + p.slice(1) : "");
const PLATFORM_MARK = { instagram: "IG", facebook: "FB", x: "X", linkedin: "in", bluesky: "BS", threads: "TH", tiktok: "TT", youtube: "YT" };
export const platformMark = (p) => h("span", { class: `plat plat-${p || "none"}`, "aria-hidden": "true" }, PLATFORM_MARK[p] || "·");

// ---------------------------------------------------------------- the studio client
export function createStudio() {
  const api = makeApi();
  const st = { summary: null, accounts: [], tz: "UTC", today: "", error: null };
  let loading = null;
  const localDate = (iso, tz) => {
    try { return new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date(iso)); }
    catch { return String(iso).slice(0, 10); }
  };
  async function load() {
    const [summary, accounts] = await Promise.all([api.get("summary"), api.get("accounts")]);
    st.summary = summary; st.accounts = accounts.accounts || []; st.tz = summary.tz || "UTC"; st.today = localDate(summary.now, st.tz);
    st.error = null;
    return st;
  }
  return {
    api, st,
    ready: () => (loading ||= load().catch((e) => { loading = null; st.error = e; throw e; })),
    refresh: () => (loading = load()),
    account: (id) => st.accounts.find((a) => a.id === id) || null,
    accountLabel: (id) => { const a = st.accounts.find((x) => x.id === id); return a ? a.label || a.id : id || ""; },
  };
}

// ---------------------------------------------------------------- retiming, with one Undo on the toast
function undoToast(studio, body, res, text, onChange, was = {}) {
  const back = undoRequests(body, res, was);
  toast(text, { key: "retime", timeout: 10000, action: "Undo", onAction: async () => {
    try {
      let r = { done: [], refused: [] };
      for (const req of back) { const x = await studio.api.post("bulk", req); r = { done: [...r.done, ...x.done], refused: [...r.refused, ...x.refused] }; }
      toast(r.refused.length ? undoSummary(r) : "Undone", { key: "retime", tone: r.refused.length ? "negative" : undefined });
    } catch (e) { toast(plain(e, "undo that"), { tone: "negative" }); }
    await onChange?.();
  } });
}
/** One retiming request through POST bulk: the sentence and its Undo on a toast. `was` maps variant -> account for a change of account. */
export async function retime(studio, body, { titles = {}, onChange, was = {} } = {}) {
  const res = await studio.api.post("bulk", body);
  const text = summarize(body.op, res, { titles, days: body.days, account: body.account ? studio.accountLabel(body.account) : null });
  if (res.done.length) undoToast(studio, body, res, text, onChange, was);
  else toast(text, { tone: "negative", key: "retime", timeout: 9000 });
  await onChange?.(res);
  return res;
}

/** An item as the calendar API sends it -> what the time controls need. */
export const asItem = (it, studio) => ({ post: it.post, variant: it.variant, rev: it.rev, title: it.title, at: it.at || null,
  tz: it.tz || studio.st.tz, account: it.account, status: it.status, flags: it.flags || [] });

// ---------------------------------------------------------------- Change time: a date and a time, or one of the next free slots
export function changeTime(anchor, studio, it, { onDone } = {}) {
  const approved = ["approved", "scheduled"].includes(it.status);
  const date = h("input", { class: "input", id: "ct-date", value: it.at ? dateKey(it.at) : "", placeholder: "2026-10-22 or Oct 22", "aria-label": "Date", autocomplete: "off" });
  const time = h("input", { class: "input", id: "ct-time", value: it.at ? timeOf(it.at) : "09:00", placeholder: "09:00", "aria-label": "Time", autocomplete: "off", inputmode: "numeric" });
  const err = h("p", { class: "ct-err", role: "alert" });
  const chips = h("div", { class: "ct-slots", role: "group", "aria-label": "Next free slots" },
    [0, 1, 2].map(() => h("span", { class: "skeleton ct-skel" })));
  const save = button("Move", { variant: "primary", size: "sm", onclick: () => submit() });
  const body = h("form", { class: "ct", onsubmit: (e) => { e.preventDefault(); submit(); } },
    h("p", { class: "popover-title" }, "Change time"),
    h("p", { class: "ct-now secondary-text" }, it.at ? `Now ${humanWhen(it.at)}, ${it.tz} time` : `Not scheduled yet. Times are ${it.tz} time.`),
    approved ? h("p", { class: "ct-note" }, icon("info"), h("span", {}, "This post is approved, and the approval stays: it covers the words and pictures, not the time. The move is recorded and the post is marked Moved after approval.")) : null,
    h("div", { class: "ct-fields" }, h("label", { class: "field" }, h("span", { class: "field-label" }, "Date"), date),
      h("label", { class: "field" }, h("span", { class: "field-label" }, "Time"), time)),
    h("p", { class: "field-label" }, "Next free slots"), chips, err,
    h("div", { class: "popover-actions" }, button("Cancel", { variant: "tertiary", size: "sm", onclick: () => pop.close() }), save),
    h("button", { type: "submit", hidden: true, tabindex: "-1" }));
  const pop = popover(anchor, body, { label: "Change time", modal: true });
  pop.el.classList.add("ct-pop");
  date.focus(); date.select();
  for (const inp of [date, time]) inp.addEventListener("input", () => { inp.removeAttribute("aria-invalid"); err.textContent = ""; });
  freeSlots(studio.api, it).then((r) => {
    chips.replaceChildren();
    if (!r.slots.length) { chips.append(h("span", { class: "secondary-text" }, noSlotsText(r, it))); return; }
    r.slots.forEach((s) => chips.append(h("button", { type: "button", class: "ct-chip", "aria-pressed": "false", onclick: (e) => {
      date.value = dateKey(s.local); time.value = timeOf(s.local); err.textContent = "";
      for (const c of chips.children) c.setAttribute("aria-pressed", String(c === e.currentTarget));
      save.focus();
    } }, slotLabel(s.local))));
  }).catch((e) => chips.replaceChildren(h("span", { class: "secondary-text" }, `The free slots could not be read: ${e.message}`)));

  function submit() {
    const d = parseDay(date.value);
    const t = /^([01]?\d|2[0-3]):([0-5]\d)$/.exec(time.value.trim());
    if (!d) { date.setAttribute("aria-invalid", "true"); err.textContent = "Write a date like 2026-10-22 or Oct 22."; date.focus(); return; }
    if (!t) { time.setAttribute("aria-invalid", "true"); err.textContent = "Write a time like 09:00 or 14:30."; time.focus(); return; }
    if (studio.st.today && d < studio.st.today) { date.setAttribute("aria-invalid", "true"); err.textContent = "Choose today or a later day."; date.focus(); return; }
    const local = `${d}T${t[1].padStart(2, "0")}:${t[2]}`;
    if (it.at && String(it.at).slice(0, 16) === local) { err.textContent = "That is the time it already has."; return; }
    apply({ local, tz: it.tz });
  }
  async function apply(to) {
    err.textContent = "";
    await busy(save, async () => {
      try {
        const res = await studio.api.post("bulk", oneItem(it, "set", to));
        if (res.refused.length) { err.textContent = res.refused[0].reason + (res.refused[0].code === "conflict.rev" ? " Close this and open it again." : ""); return; }
        pop.close();
        const body = oneItem(it, "set", to);
        undoToast(studio, body, res, summarize("set", res, { titles: { [it.variant]: it.title } }), onDone);
        await onDone?.(res);
      } catch (e) { err.textContent = e.message; }
    });
  }
  return pop;
}

// ---------------------------------------------------------------- the post sheet
export function createPostSheet(ctx, studio) {
  if (!document.querySelector("link[data-preview-css]"))             // the preview cards' own styles (.pv-*), shared with the old screens and Layout
    document.head.append(h("link", { rel: "stylesheet", href: "/app/shared/panels.css", "data-preview-css": "" }));
  const el = h("aside", { class: "side-panel post-sheet", "aria-label": "Post", hidden: true, tabindex: "-1" });
  const crumb = h("p", { class: "ws-panel-crumb" });
  const titleEl = h("h2", { id: "ps-title" });
  const meta = h("div", { class: "ws-panel-meta ps-meta" });
  const body = h("div", { class: "ws-panel-body ps-body" });
  const roomBtn = button("Open in the Room", { variant: "primary", size: "sm", iconName: "film", onclick: () => cur && (location.hash = `#/room/${encodeURIComponent(cur.post)}`) });
  const closeBtn = button("Close", { variant: "tertiary", size: "sm", iconName: "x", iconOnly: true, class: "ps-x", onclick: () => close() });
  el.append(h("div", { class: "ws-panel-head" }, h("div", { class: "ws-panel-top" }, crumb, h("div", { class: "ws-panel-tools" }, roomBtn, closeBtn)), titleEl, meta), body);
  el.addEventListener("keydown", (e) => { if (e.key === "Escape" && !document.querySelector(".popover")) { e.preventDefault(); e.stopPropagation(); close(); } });
  document.body.append(el);

  let cur = null, token = 0, opener = null, onChanged = null, showHidden = false, slide = 0;
  const assets = {};

  function open(item, { changed } = {}) {
    if (el.hidden) opener = document.activeElement;
    cur = item; onChanged = changed || null; slide = 0;
    el.hidden = false;
    paintHead(item, null);
    body.replaceChildren(h("div", { class: "ps-skel", "aria-hidden": "true" }, h("span", { class: "skeleton" }), h("span", { class: "skeleton" }),
      h("span", { class: "skeleton ps-skel-phone" })), h("p", { class: "sr-only", role: "status" }, "Loading the post"));
    el.focus({ preventScroll: true });
    return load();
  }
  function close(refocus = true) {
    if (el.hidden) return;
    token++;
    const was = cur;
    el.hidden = true; cur = null;
    if (refocus) {                           // the view may have been redrawn while the sheet was open: find the same post again
      const again = opener?.isConnected ? opener : was && document.querySelector(`.cal-item[data-variant="${CSS.escape(was.variant || "")}"], .pvw-card[data-variant="${CSS.escape(was.variant || "")}"], .pvw-tile[data-variant="${CSS.escape(was.variant || "")}"]`);
      again?.focus({ preventScroll: true });
    }
  }
  async function load() {
    const my = ++token, item = cur;
    try {
      const [detail, cfg] = await Promise.all([studio.api.get(`posts/${encodeURIComponent(item.post)}`), loadPreviewConfig().catch(() => ({ platforms: {} }))]);
      if (my !== token) return;
      const v = findVariant(detail.post, item.variant) || detail.post.variants[0] || null;
      await loadAssets(studio.api, detail.post, assets);
      let server = null;
      if (v) { try { server = await studio.api.get(`posts/${encodeURIComponent(item.post)}/preview/${encodeURIComponent(v.id)}`); } catch { /* the card still draws */ } }
      if (my !== token) return;
      draw(item, detail, v, cfg, server);
    } catch (e) {
      if (my !== token) return;
      body.replaceChildren(h("div", { class: "ws-error ps-error", role: "alert" },
        h("h2", {}, e.status === 404 ? "This post is not in the studio any more" : "The post could not be read"),
        h("p", { class: "secondary-text" }, e.status === 404 ? "It may have been dropped. The calendar reads again in a moment." : plain(e, "read the post")),
        e.status === 404 ? null : button("Try again", { variant: "primary", onclick: (ev) => busy(ev.currentTarget, load) })));
    }
  }
  function paintHead(item, v) {
    crumb.replaceChildren(h("span", { class: "dot", style: { color: statusTone(v ? v.status : item.status) } }),
      h("span", {}, [v ? studio.accountLabel(v.account) : item.account ? studio.accountLabel(item.account) : "No account yet",
        v ? `${platformWord(v.platform)} ${String(v.post_type || "").replace(/_/g, " ")}`.trim() : ""].filter(Boolean).join(" · ")));
    titleEl.textContent = item.title || "Post";
    el.setAttribute("aria-label", item.title || "Post");
  }

  function draw(item, detail, v, cfg, server) {
    const post = detail.post;
    const acct = v ? studio.account(v.account) : null;
    const title = item.title || postName(post, v);
    const it = v ? { post: post.id, variant: v.id, rev: post.rev, title, at: v.schedule ? v.schedule.local : null,
      tz: v.schedule ? v.schedule.tz : (acct && acct.tz) || studio.st.tz, account: v.account, status: v.status, flags: v.flags || [] } : null;
    cur = { ...item, title, rev: post.rev };
    paintHead(cur, v);
    const locked = !v || itemLocked({ status: v.status }) || v.status === "dropped";
    const after = async () => { await load(); await onChanged?.(); };

    meta.replaceChildren(statusPill(v ? v.status : null),
      ...(it?.flags || []).map((f) => h("span", { class: `chip flag-chip f-${f}`, title: flagInfo(f)[1] }, icon("flag"), h("span", {}, flagInfo(f)[0]))),
      h("span", { class: "ps-when" }, icon("calendar"), it && it.at ? humanWhen(it.at) : "Not scheduled"));

    const actions = h("div", { class: "ps-actions" });
    if (it && !locked) {
      actions.append(...[
        button("Change time", { variant: "secondary", size: "sm", iconName: "calendar", "data-act": "change-time", onclick: (e) => changeTime(e.currentTarget, studio, it, { onDone: after }) }),
        button("Next free slot", { variant: "secondary", size: "sm", iconName: "right", "data-act": "next-slot", onclick: (e) => busy(e.currentTarget, () =>
          retime(studio, oneItem(it, "slots"), { titles: { [it.variant]: title }, onChange: after }).catch((er) => toast(plain(er, "move it"), { tone: "negative" }))) }),
        it.at ? button("Move to Unscheduled", { variant: "tertiary", size: "sm", iconName: "inbox", "data-act": "unschedule", onclick: (e) => busy(e.currentTarget, () =>
          retime(studio, oneItem(it, "set", null), { titles: { [it.variant]: title }, onChange: after }).catch((er) => toast(plain(er, "move it"), { tone: "negative" }))) }) : null].filter(Boolean));
    }
    if (post && ctx.openTool) actions.append(button("Open in the post editor", { variant: "tertiary", size: "sm", iconName: "open", "data-act": "post-editor",
      title: "More tools: the library picker, cover, crop and the delivery check, in their own window", onclick: () => ctx.openTool(`#/post/${encodeURIComponent(post.id)}`) }));
    if (v && v.status === "in_review" && ctx.openApprovals)      // approving happens in the approval sheet, never here
      actions.prepend(button("Review for approval", { variant: "primary", size: "sm", iconName: "lock", "data-act": "review-approval",
        onclick: () => { close(false); ctx.openApprovals({ post: post.id, variant: v.id }); } }));
    if (!(it && !locked) && v) actions.append(h("p", { class: "ps-locked" }, icon("lock"), h("span", {}, v.status === "dropped" ? "Dropped posts cannot be moved." : lockWhy({ status: v.status }))));

    const notes = openNotes(detail, v);
    const waitingNotes = notes.filter((n) => !n.claim);
    const earlier = noteHistory(detail, v);
    const reason = h("section", { class: "ps-why", "aria-label": "Why it has this status" },
      h("p", { class: "ps-reason" }, v ? statusReason(v, acct && acct.live) : "This post has no account yet. Ask Claude to add one, or add it in the Room."),
      waitingNotes.map((n) => h("blockquote", { class: "ps-note" }, h("span", { class: "ps-notehead" }, noteHead(n)), n.note || "(no note)")),
      earlier.length ? h("details", { class: "ps-earlier" },
        h("summary", {}, `Notes being worked on or settled (${earlier.length})`),
        earlier.map((n) => h("blockquote", { class: `ps-note ${n.resolved ? "is-resolved" : "is-claimed"}` }, h("span", { class: "ps-notehead" }, noteHead(n)),
          n.note || "(no note)", h("span", { class: "ps-noteclaim" }, noteState(n))))) : null,
      (it?.flags || []).map((f) => h("p", { class: "ps-flag" }, h("strong", {}, `${flagInfo(f)[0]}. `), flagInfo(f)[1])),
      v && v.approval ? h("p", { class: "ps-approval" }, icon("check"), `Approved by ${v.approval.by}.`) : null);

    const phoneWrap = h("div", { class: "ps-phone-wrap" });
    const fold = h("button", { type: "button", class: "check", role: "checkbox", "aria-checked": String(showHidden), "aria-label": "Show the text after “more”",
      onclick: () => { showHidden = !showHidden; fold.setAttribute("aria-checked", String(showHidden)); drawPhone(); } }, icon("check"));
    const drawPhone = (refocus) => {
      if (!v) { phoneWrap.replaceChildren(h("p", { class: "secondary-text" }, "A preview needs an account to show the post on.")); return; }
      const model = previewModel(post, v, { assets, account: acct ? { id: v.account, label: acct.label } : null, server, showHidden });
      const n = model.media.length;
      slide = Math.min(slide, Math.max(0, n - 1));
      if (n > 1) model.media = [model.media[slide], ...model.media.filter((_, i) => i !== slide)];   // the card draws the first slide
      const card = buildPreview(model, cfg);
      const pager = card.querySelector(".pv-pager");
      if (pager) pager.textContent = `${slide + 1} / ${n}`;
      const steps = n > 1 ? h("div", { class: "ps-slides", role: "group", "aria-label": "Slides" },
        button("Previous slide", { variant: "secondary", size: "sm", iconName: "right", iconOnly: true, class: "ps-prev", disabled: slide === 0, onclick: () => { slide -= 1; drawPhone(slide === 0 ? ".ps-next" : ".ps-prev"); } }),
        h("span", { class: "ps-slidepos", "aria-live": "polite" }, `Slide ${slide + 1} of ${n}`),
        button("Next slide", { variant: "secondary", size: "sm", iconName: "right", iconOnly: true, class: "ps-next", disabled: slide === n - 1, onclick: () => { slide += 1; drawPhone(slide === n - 1 ? ".ps-prev" : ".ps-next"); } })) : null;
      phoneWrap.replaceChildren(...[h("div", { class: "ps-phone", role: "region", "aria-label": `${platformWord(v.platform)} preview` }, card), steps].filter(Boolean));
      phoneWrap.querySelector(".ps-prev .icon")?.style.setProperty("transform", "rotate(180deg)");
      if (refocus) phoneWrap.querySelector(refocus)?.focus();
    };
    drawPhone();
    const phone = h("section", { class: "ps-section ps-mock", "aria-label": "How it will look" },
      h("div", { class: "ps-sechead" }, h("h3", {}, v ? `On ${platformWord(v.platform)}` : "Preview"), h("span", { class: "chip" }, "Approximate"),
        h("label", { class: "ps-fold" }, fold, h("span", {}, "Show the text after “more”"))),
      phoneWrap);

    const f = postFacts(post, v);
    const dd = (x) => h("dd", {}, x);
    const none = (t = "None") => h("span", { class: "secondary-text" }, t);
    const details = h("section", { class: "ps-section ps-details", "aria-label": "Words and alt text" },
      h("h3", {}, "Words and alt text"),
      h("dl", { class: "ps-dl" },
        h("dt", {}, "When"), dd(it && it.at ? `${humanWhen(it.at)} (${it.tz})` : "Not scheduled"),
        post.campaign ? [h("dt", {}, "Campaign"), dd(post.campaign)] : null,
        h("dt", {}, "Hashtags"), dd(f.tags.length ? h("span", { class: "ps-tags" }, f.tags.map((t) => h("span", { class: "chip" }, "#" + t))) : none()),
        h("dt", {}, "First comment"), dd(f.firstComment ? h("span", { class: "ps-pre" }, f.firstComment) : none()),
        f.link ? [h("dt", {}, "Link"), dd(h("span", { class: "ps-pre" }, f.link))] : null,
        h("dt", {}, "Alt text"), dd(f.alts.length ? h("ol", { class: "ps-alts" }, f.alts.map((m) => h("li", { class: m.missing ? "missing" : "" },
          h("span", { class: "ps-altn" }, `Image ${m.n}`), h("span", {}, m.missing ? MISSING_ALT : m.alt)))) : none("No images"))));

    const events = recentEvents(detail);
    const history = h("section", { class: "ps-section ps-history", "aria-label": "History" }, h("h3", {}, "History"),
      events.length ? h("ol", {}, events.map((e) => h("li", {}, h("span", {}, eventLine(e)), h("span", { class: "secondary-text" }, `${eventWho(e)} · ${stamp(e.ts)}`))))
        : h("p", { class: "secondary-text" }, "Nothing has happened yet."));

    const files = deliverSection(post, v);
    body.replaceChildren(actions, reason, phone, details, files, history);
    body.scrollTop = 0;
  }

  // Exact files for posting by hand: a package of the original bytes, caption, first comment and alt text. Preparing or downloading
  // one publishes nothing. A reviewed package can then be reported as posted ("I posted this"), and that report is the protected,
  // human-only act: it goes through the app (aveNative.recordManualPublication), never a web request, exactly as the old dialog did.
  // What happened to a send: the provider's receipt (or a person's report) and, for a send that may or may not have gone out,
  // the two protected acts the old editor had. "Check with provider" reads the existing operation's status; "Review publication
  // outcome" records what the person saw on the platform. Both go through the app's own sheets (aveNative.connectionAction,
  // with only the post, variant, revision and operation), exactly as the editor did; without the app the status check uses the
  // studio's route and the outcome review is not offered.
  function deliveryOutcome(post, v) {
    if (!v || !(v.result?.receipt || v.result || v.delivery_operation?.recoverable)) return null;
    const note = h("p", { class: "ap-error", role: "alert" });
    const args = { post: post.id, variant: v.id, revision: post.rev, ...(v.delivery_operation ? { operation: v.delivery_operation.digest } : {}) };
    const after = async () => { await load(); await onChanged?.(); };
    const refresh = (v.result?.receipt || v.delivery_operation?.recoverable) ? button("Check with provider", { variant: "secondary", size: "sm", iconName: "sync", "data-act": "provider-refresh",
      onclick: (e) => busy(e.currentTarget, async () => {
        note.textContent = "";
        try {
          if (window.aveNative?.connectionAction) await window.aveNative.connectionAction("providerRefresh", args);
          else await studio.api.post(`posts/${encodeURIComponent(post.id)}/delivery-refresh`, { variant: v.id });
          await after();
        } catch (er) { note.textContent = er.message; }
      }) }) : null;
    const review = window.aveNative?.connectionAction ? button("Review publication outcome", { variant: "secondary", size: "sm", iconName: "lock", "data-act": "outcome-review",
      onclick: (e) => busy(e.currentTarget, async () => {
        note.textContent = "";
        try { await window.aveNative.connectionAction("reviewProviderOutcome", args); await after(); } catch (er) { note.textContent = er.message; }
      }) }) : null;
    return h("div", { class: "ps-outcome", "aria-label": "Delivery outcome" },
      h("h3", {}, "Delivery outcome"),
      v.result?.receipt ? deliveryReceipt(v) : h("p", { class: "secondary-text" }, "An earlier send has an unresolved queue record. Look at the platform before reviewing the outcome."),
      h("div", { class: "ps-delacts" }, refresh, review),
      window.aveNative?.connectionAction ? null : h("p", { class: "secondary-text" }, "Reviewing the outcome needs the AVE Studio app on the publishing computer."),
      note);
  }

  function deliverSection(post, v) {
    const status = h("div", { class: "ps-delstatus", role: "status", "aria-live": "polite" });
    const history = h("section", { class: "ps-packages", "aria-label": "Package history" });
    const reviewedOk = !!v && ["approved", "scheduled", "published"].includes(v.status);
    const reviewed = button("Export reviewed post", { variant: reviewedOk ? "primary" : "secondary", size: "sm", "data-act": "export-reviewed", onclick: (e) => build(e.currentTarget, false) });
    const draft = button("Download unreviewed draft", { variant: "secondary", size: "sm", iconName: "arrive", "data-act": "download", onclick: (e) => build(e.currentTarget, true) });
    const sec = h("section", { class: "ps-section ps-deliver", "aria-label": "Deliver by hand" },
      deliveryOutcome(post, v),
      h("h3", {}, "Deliver by hand"),
      h("p", { class: "secondary-text" }, `Original files, the exact caption, first comment, alt text and credits, ready for ${v ? platformWord(v.platform) : "the platform"}'s app. Downloading posts nothing.`),
      h("div", { class: "ps-delacts" }, reviewed, draft), status, history);
    async function build(btn, isDraft) {
      await busy(btn, async () => {
        status.textContent = "Preparing the original files and the caption…";
        try {
          const r = await buildPackage(studio.api, post, v, isDraft);
          if (!r.ok) { status.textContent = (r.warnings || ["The export is not available."]).join(" "); return; }
          status.replaceChildren(h("p", { class: "ps-delok" }, icon("check"), h("span", {}, "The package is ready. It is in the history below. Nothing has been posted.")));
          await loadHistory();
        } catch (e) { status.replaceChildren(h("p", { class: "ct-err", role: "alert" }, plain(e, "prepare the files"))); }
      });
    }
    async function loadHistory() {
      try {
        const r = await packageHistory(studio.api, post, v);
        const packs = r.packages || [];
        history.replaceChildren(...(packs.length ? [h("h4", {}, "Package history"), h("ul", {}, packs.map((x) => h("li", {}, packageCard(x))))] : []));
      } catch (e) { history.replaceChildren(h("p", { class: "secondary-text" }, `The package history could not be read: ${e.message}`)); }
    }
    function packageCard(pack) {
      const st = packageState(pack, v);
      const media = pack.media || [];
      const native = window.aveNative;
      const form = h("div", { class: "ps-report" });
      const copy = (label, text) => button(`Copy ${label.toLowerCase()}`, { variant: "tertiary", size: "sm", onclick: () =>
        navigator.clipboard?.writeText(text || "").then(() => toast(`${label} copied`), () => toast("Select the text and copy it.")) });
      return h("article", { class: "manual-package ps-pack", dataset: { package: pack.package_id } },
        h("div", { class: "ps-packhead" }, h("strong", {}, st.label), h("span", { class: "secondary-text" }, new Date(pack.created).toLocaleString())),
        h("p", { class: "secondary-text" }, `${media.length} original ${media.length === 1 ? "file" : "files"} · revision ${pack.rev} · ${studio.accountLabel(pack.account) || (v ? studio.accountLabel(v.account) : "")}`),
        h("div", { class: "ps-packacts" },
          h("a", { class: "btn btn-primary btn-sm", href: localUrl(pack.zip_url), download: `${pack.package_id}.zip` }, h("span", {}, "Download full-resolution package")),
          native?.sharePackage ? button("Share or AirDrop", { variant: "tertiary", size: "sm", onclick: () => native.sharePackage(pack.package_id).catch((e) => toast(e.message, { tone: "negative" })) }) : null,
          native?.revealPackage ? button("Show in Finder", { variant: "tertiary", size: "sm", onclick: () => native.revealPackage(pack.package_id).catch((e) => toast(e.message, { tone: "negative" })) }) : null,
          copy("Caption", pack.caption), copy("Alt text", media.map((m) => `${m.position}. ${m.alt}`).join("\n\n"))),
        st.published && st.publication?.url ? h("a", { class: "ps-pubLink", href: st.publication.url, target: "_blank", rel: "noopener" }, "Open the reported post")
          : st.canReport ? button("I posted this", { variant: "secondary", size: "sm", "data-act": "posted", onclick: () => reportForm(pack, form) }) : null,
        form);
    }
    function reportForm(pack, target) {
      const native = window.aveNative?.recordManualPublication;
      const now = new Date();
      const field = (label, input) => h("label", { class: "field" }, h("span", { class: "field-label" }, label), input);
      const url = h("input", { class: "input", placeholder: "https://…", "aria-label": "Published URL", autocomplete: "off" });
      const at = h("input", { class: "input", "aria-label": "Publication time", value: `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")} ${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}` });
      const by = h("input", { class: "input", "aria-label": "Reported by", placeholder: "Your name", value: ctx.me ? ctx.me() || "" : "", maxlength: "120" });
      const changed = h("textarea", { class: "input", "aria-label": "Changes made in the platform app", placeholder: "Music, tags, caption edits or other changes (optional)", maxlength: "2000" });
      const mismatch = h("input", { class: "input", "aria-label": "Alternate link explanation", placeholder: "Only for a link outside this platform", maxlength: "500" });
      const err = h("p", { class: "ct-err", role: "alert" });
      const submit = button("Record publication", { variant: "primary", size: "sm", disabled: !native, onclick: () => send() });
      const requestId = newRequestId();
      target.replaceChildren(h("form", { class: "ps-reportform", onsubmit: (e) => { e.preventDefault(); send(); } },
        h("p", { class: "popover-title" }, "I posted this package"),
        h("p", { class: "secondary-text" }, `Revision ${pack.rev}. This records your report; it does not check the platform itself.`),
        native ? null : h("p", { class: "ct-note" }, icon("lock"), h("span", {}, "Recording a publication needs the AVE Studio app: the report is signed on the publishing computer.")),
        field("Published URL", url), field("Publication time", at), field("Reported by", by), field("Alternate link explanation", mismatch), field("Changes made in the platform app", changed),
        err, h("div", { class: "popover-actions" }, button("Cancel", { variant: "tertiary", size: "sm", onclick: () => target.replaceChildren() }), submit)));
      url.focus();
      async function send() {
        if (!native) return;
        const when = new Date(at.value.trim().replace(" ", "T"));
        if (!/^https?:\/\//.test(url.value.trim())) { err.textContent = "Paste the link of the published post."; url.focus(); return; }
        if (Number.isNaN(when.getTime())) { err.textContent = "Write the time like 2026-10-21 09:30."; at.focus(); return; }
        if (!by.value.trim()) { err.textContent = "Add your name: a named person reports every publication."; by.focus(); return; }
        await busy(submit, async () => {
          try {
            const r = await native(publicationRequest({ post, pack, url: url.value.trim(), at: when, by: by.value.trim(), changed: changed.value, mismatch: mismatch.value, requestId }));
            target.replaceChildren();
            status.replaceChildren(h("p", { class: "ps-delok" }, icon("check"), h("span", {}, `Published · reported by ${r.by || by.value.trim()}. The package history keeps the exact files and your report.`)));
            await loadHistory();
          } catch (e) { err.textContent = e.message; }
        });
      }
    }
    loadHistory();
    return sec;
  }

  return { el, open, close, isOpen: () => !el.hidden, current: () => cur, reload: () => (cur ? load() : null) };
}

function stamp(iso) {
  try { return new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }).format(new Date(iso)); }
  catch { return String(iso || ""); }
}
export { fmtLong };

// What happened to the notes you sent back on this post: the ones Claude has taken and the ones that are settled, newest first. The
// old Feedback page's history, for one post (the open ones that nobody has taken yet show above, uncollapsed).
const KIND_NOTE = ["rework_copy", "rework_visual", "retime", "note", "drop"];
export function noteHistory(detail, v) {
  return (detail.feedback || []).filter((f) => (f.resolved || f.claim) && (f.variant == null || (v && f.variant === v.id)) && KIND_NOTE.includes(f.kind))
    .sort((a, b) => String((b.resolved || b.claim || {}).ts || b.ts || "").localeCompare(String((a.resolved || a.claim || {}).ts || a.ts || "")));
}
const who = (n) => (String(n || "").toLowerCase() === "claude" ? "Claude" : n || "the editor");
const when = (ts) => { const t = Date.parse(ts || ""); return Number.isFinite(t) ? ago(t) : ""; };
export function noteState(n) {
  if (n.resolved) return `Settled by ${who(n.resolved.by)} ${when(n.resolved.ts)}${n.resolved.note ? `: ${n.resolved.note}` : ""}`.trim();
  return `${who(n.claim.by)} took it ${when(n.claim.ts)} and is working on it`.replace("  ", " ");
}
