// The side preview: click a post in the calendar and it opens beside the calendar (a bottom sheet on a phone) instead of taking you to the
// editor. A proof of what will go out (the platform mock with its "more" fold), the words around it (hashtags, first comment, link, alt text),
// WHY it has the status it has, and what happened to it. Time changes live here too. Nothing in this panel edits words or approves anything.

import { announce, chip, dateKey, flagInfo, fmtShort, h, icon, timeOf, store } from "./dom.js";
import { nextFreeSlot } from "./bulk.js";
import { openReschedule } from "./reschedule.js";
import { ago, effectiveFirstComment, effectiveHashtags, effectiveMedia, findVariant, localUrl, PLATFORM_NAMES, postTitle } from "./panels.js";
import { buildPreview, loadPreviewConfig, previewModel } from "./previews/index.js";

import { openManualExport } from "./manual.js";
import { deliveryReceipt, receiptReason } from "./delivery-receipt.js";

let el = null, token = 0, current = null, returnFocus = null, onCloseCb = null;

export const isPeekOpen = () => !!(el && el.classList.contains("on"));
export const peekTarget = () => (current ? { post: current.post, variant: current.variant } : null);

function shell() {
  if (el) return el;
  el = h("aside", { id: "peek", class: "peek", role: "dialog", "aria-modal": "false", "aria-labelledby": "peek-title", tabindex: "-1" });
  el.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); closePeek(); } });
  document.body.append(el);
  return el;
}

export function closePeek({ restore = true } = {}) {
  if (!el || !el.classList.contains("on")) return;
  token++;
  el.classList.remove("on");
  document.body.classList.remove("peek-open");
  const back = returnFocus;
  current = null; returnFocus = null;
  if (restore && back && document.contains(back)) back.focus();
  if (onCloseCb) { const f = onCloseCb; onCloseCb = null; f(); }
}

/** item = a calendar card {post, variant, rev, title, account, status, at?, tz?, thumb?}. hooks.onChanged() runs after the panel moved or re-timed the post. */
export async function openPeek(ctx, item, hooks = {}) {
  const root = shell();
  const my = ++token;
  returnFocus = document.activeElement && document.activeElement !== document.body ? document.activeElement : returnFocus;
  onCloseCb = hooks.onClose || null;
  current = item;
  const bar = document.querySelector(".top");                      // on a desk the panel starts under the sticky top bar, so the navigation stays whole
  document.documentElement.style.setProperty("--peek-top", `${bar && bar.getBoundingClientRect().width > innerWidth * .7 ? Math.round(bar.getBoundingClientRect().height) : 0}px`);
  root.classList.add("on");
  document.body.classList.add("peek-open");
  root.replaceChildren(head(item, null), h("div", { class: "peek-body" }, h("p", { class: "peek-loading" }, "Loading the post"), h("div", { class: "peek-skel", "aria-hidden": "true" })));
  root.focus({ preventScroll: true });
  try {
    const [detail, cfg] = await Promise.all([ctx.api.get(`posts/${encodeURIComponent(item.post)}`), loadPreviewConfig().catch(() => ({ platforms: {} }))]);
    if (my !== token) return;
    const v = findVariant(detail.post, item.variant) || detail.post.variants[0] || null;
    const assets = {};
    await Promise.all(effectiveMedia(detail.post, v).map((m) => m.asset).filter(Boolean).map(async (id) => {
      try { const r = await ctx.api.get(`library/${id}`); assets[id] = { ...r.asset, url: r.url }; } catch { assets[id] = { id, missing: true }; }
    }));
    let server = null;
    if (v) { try { server = await ctx.api.get(`posts/${encodeURIComponent(item.post)}/preview/${encodeURIComponent(v.id)}`); } catch { /* the mock still draws without crop hints */ } }
    if (my !== token) return;
    draw(ctx, root, item, detail, v, cfg, assets, server, hooks);
  } catch (e) {
    if (my !== token) return;
    root.replaceChildren(head(item, null), h("div", { class: "peek-body" },
      h("div", { class: "peek-error", role: "alert" }, h("p", {}, e.status === 404 ? "This post was not found. It may have been dropped." : `The post could not be loaded: ${e.message}`),
        e.status === 404 ? null : h("button", { type: "button", onclick: () => openPeek(ctx, item, hooks) }, "Try again"))));
  }
}

function head(item, v) {
  return h("header", { class: "peek-head" },
    h("div", { class: "peek-headtext" },
      h("h2", { id: "peek-title", class: "peek-title" }, item.title || "Post"),
      h("p", { class: "peek-sub" }, [item.account, v ? `${PLATFORM_NAMES[v.platform] || v.platform} ${v.post_type}` : null].filter(Boolean).join(" · "))),
    h("button", { type: "button", class: "icon-b peek-x", "aria-label": "Close the preview", onclick: () => closePeek() }, icon("x")));
}

const REASON = {
  draft: () => "Not in review yet. It becomes ready to approve once the queue has it.",
  in_review: () => "Waiting for your approval. Open Review to look at it and approve.",
  approved: () => "Approved, with no time yet. Give it a time and it is ready to go.",
  scheduled: (v, live) => `Approved and scheduled${v.schedule ? ` for ${fmtShort(dateKey(v.schedule.local))}, ${timeOf(v.schedule.local)}` : ""}. ${live ? "It posts at that time." : "This account is in dry run: nothing is sent."}`,
  publishing: () => "Being sent right now.",
  published: (v) => `Published${v.result && v.result.url ? ` at ${v.result.url}` : ""}.`,
  failed: (v) => `The send failed${v.result && v.result.error ? `: ${v.result.error}` : ""}. Nothing was posted.`,
  missed: () => "The time passed without a send, and it is never posted late. Give it a new time.",
  needs_rework: () => "Sent back to Claude, who has not changed it yet.",
  dropped: () => "Dropped. It will not be posted.",
};

function draw(ctx, root, item, detail, v, cfg, assets, server, hooks) {
  const post = detail.post;
  const acct = ctx.accounts.find((a) => a.id === (v && v.account));
  const q = (v && detail.queue && detail.queue[v.id]) || null;
  const flags = v ? v.flags || [] : [];
  const locked = !v || ["published", "publishing", "dropped"].includes(v.status);
  const cur = v ? {
    post: post.id, variant: v.id, rev: post.rev, title: item.title || postTitle(post, v), account: v.account, status: v.status, flags,
    at: v.schedule ? v.schedule.local : null, tz: v.schedule ? v.schedule.tz : (acct && acct.tz) || ctx.tz,
  } : null;
  const after = async () => { await openPeek(ctx, item, hooks); if (hooks.onChanged) await hooks.onChanged(); };

  const notes = (detail.feedback || []).filter((f) => !f.resolved && (f.variant == null || (v && f.variant === v.id)) && ["rework_copy", "rework_visual", "retime", "note", "drop"].includes(f.kind));
  const reasonText = v ? (receiptReason(v) || (REASON[v.status] ? REASON[v.status](v, acct && acct.live) : v.status_reason)) : "This post has no account yet. Add one from the editor to schedule it.";
  const why = h("section", { class: "peek-why", "aria-label": "Status" },
    h("div", { class: "peek-chips" }, v ? chip(v.status,"",v) : h("span", { class: "chip st-draft" }, "No account yet"),
      flags.map((f) => h("span", { class: `chip flag-chip f-${f}`, title: flagInfo(f)[1] }, icon("flag"), h("span", { class: "chip-t" }, flagInfo(f)[0])))),
    h("p", { class: "peek-reason" }, reasonText),
    notes.map((n) => h("blockquote", { class: "peek-note" }, h("span", { class: "peek-notehead" }, `${n.by || "You"} sent it back${n.kind === "rework_copy" ? " (words)" : n.kind === "rework_visual" ? " (picture)" : ""}:`), n.note || "(no note)")),
    flags.map((f) => h("p", { class: "peek-flag" }, h("strong", {}, `${flagInfo(f)[0]}. `), flagInfo(f)[1])),
    q && v.status === "in_review" && q.mismatch && q.mismatch.length ? h("p", { class: "peek-flag bad" }, h("strong", {}, "The review item differs from the post. "), `It differs in: ${q.mismatch.map((d) => d.field).join(", ")}. It cannot be approved until the queue is synced.`) : null,
    v && v.approval ? h("p", { class: "peek-approval" }, icon("check"), `Approved by ${v.approval.by} ${ago(v.approval.at)}${q && q.expires_at ? `, valid until ${fmtShort(dateKey(q.expires_at))}` : ""}.`) : null,
    q && q.preflight_problems && q.preflight_problems.length ? h("ul", { class: "peek-problems" }, q.preflight_problems.map((p) => h("li", {}, String(p)))) : null);

  const when = v && v.schedule ? `${fmtShort(dateKey(v.schedule.local))}, ${timeOf(v.schedule.local)} (${v.schedule.tz})` : "Not scheduled";
  const actions = h("div", { class: "peek-actions" },
    h("button", { type: "button", onclick: () => openManualExport(ctx, post, v) }, "Export files and caption"),
    cur && !locked ? h("button", { type: "button", class: "primary", dataset: { act: "change-time" }, onclick: () => openReschedule(ctx, [cur], { onDone: after }) }, icon("calendar"), "Change time") : null,
    cur && !locked ? h("button", { type: "button", dataset: { act: "next-slot" }, onclick: async () => { try { await nextFreeSlot(ctx, cur, { onChange: null }); await after(); } catch (e) { announce(e.message, { tone: "error" }); } } }, icon("spark"), "Next free slot") : null,
    h("a", { class: "btn", href: `#/post/${encodeURIComponent(post.id)}${v ? "?variant=" + encodeURIComponent(v.id) : ""}`, dataset: { act: "open-editor" } }, "Open editor"),
    v && ["in_review", "draft", "needs_rework"].includes(v.status) ? h("a", { class: "btn", href: `#/review?post=${encodeURIComponent(post.id)}&variant=${encodeURIComponent(v.id)}` }, "Review") : null);

  const mock = h("section", { class: "peek-mock", "aria-label": "How it will look" });
  const drawMock = () => {
    if (!v) return mock.replaceChildren();
    const model = previewModel(post, v, { assets, account: acct ? { id: v.account, label: acct.label } : null, server, showHidden: !!store("peek.fold") });
    mock.replaceChildren(
      h("div", { class: "peek-mockhead" }, h("h3", {}, `On ${PLATFORM_NAMES[v.platform] || v.platform}`),
        h("label", { class: "peek-fold" }, h("input", { type: "checkbox", checked: !!store("peek.fold"), onchange: (e) => { store("peek.fold", e.target.checked); drawMock(); } }), "Show the text hidden behind “more”")),
      buildPreview(model, cfg));
  };
  drawMock();

  const media = v ? effectiveMedia(post, v) : post.media || [];
  const tags = v ? effectiveHashtags(post, v) : (post.copy && post.copy.hashtags) || [];
  const fc = v ? effectiveFirstComment(post, v) : (post.copy && post.copy.first_comment) || "";
  const link = post.copy && post.copy.link;
  const details = h("section", { class: "peek-details", "aria-label": "Words and alt text" },
    h("dl", {},
      h("dt", {}, "When"), h("dd", {}, when),
      post.campaign ? [h("dt", {}, "Campaign"), h("dd", {}, post.campaign)] : null,
      h("dt", {}, "Hashtags"), h("dd", {}, tags.length ? h("span", { class: "peek-tags" }, tags.map((t) => h("span", { class: "peek-tag" }, "#" + t))) : h("span", { class: "peek-none" }, "None")),
      h("dt", {}, "First comment"), h("dd", {}, fc ? h("span", { class: "peek-pre" }, fc) : h("span", { class: "peek-none" }, "None")),
      link ? [h("dt", {}, "Link"), h("dd", {}, h("code", {}, link))] : null,
      h("dt", {}, "Alt text"),
      h("dd", {}, media.length ? h("ol", { class: "peek-alts" }, media.map((m, i) => h("li", { class: String(m.alt || "").trim() ? "" : "missing" },
        h("span", { class: "peek-altn" }, `Image ${i + 1}`), String(m.alt || "").trim() ? m.alt : "Missing: screen readers get nothing here."))) : h("span", { class: "peek-none" }, "No images"))));

  const events = (detail.events || []).filter((e) => e.op !== "viewed").slice().reverse().slice(0, 8);
  const hist = h("section", { class: "peek-history", "aria-label": "History" }, h("h3", {}, "History"), deliveryReceipt(v),
    events.length ? h("ol", {}, events.map((e) => h("li", {}, h("span", { class: "peek-ev" }, describe(e)), h("span", { class: "peek-evwho" }, `${who(e)} · ${ago(e.ts)}`)))) : h("p", { class: "peek-none" }, "Nothing has happened yet."));

  root.replaceChildren(head({ ...item, title: item.title || postTitle(post, v) }, v), h("div", { class: "peek-body" }, actions, why, mock, details, hist));
  root.scrollTop = 0;
}

const who = (e) => (e.actor && e.actor.kind === "agent" ? "Claude" : (e.actor && e.actor.name) || "someone");
function stamp(utc) { try { return new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }).format(new Date(utc)); } catch { return String(utc); } }
function describe(e) {
  const d = e.detail || {};
  switch (e.op) {
    case "create": return "Created";
    case "edit": return `Edited ${d.paths && d.paths.length ? d.paths.map((p) => p.split(".").slice(-2).join(" ").replace(/_/g, " ")).slice(0, 2).join(", ") : "the post"}`;
    case "move": return d.from ? `Moved from ${stamp(d.from)} to ${stamp(d.to)}` : `Given a time: ${stamp(d.to)}`;
    case "unschedule": return "Moved to Unscheduled";
    case "approved": return "Approved";
    case "rejected": return "Rejected";
    case "approval_invalidated": return `Approval removed${d.why ? ` (${d.why})` : ""}`;
    case "feedback": return `Sent back${d.kind ? ` (${String(d.kind).replace(/_/g, " ")})` : ""}`;
    case "variant_add": return "Added an account";
    default: return String(e.op).replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
  }
}
