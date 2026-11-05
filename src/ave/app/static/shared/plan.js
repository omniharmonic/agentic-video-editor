// Planning logic shared by the kept studio screens (studio/ui/static: bulk.js, reschedule.js, manual.js) and the Workspace's views
// (app/static/workspace: calendar.js, previews.js, posts.js, ideas.js, campaigns.js, approvals.js, workspace.js). No page chrome
// here: the periods a calendar shows, what a post's status means in words, which posts can move and why not, one sentence for a
// retiming result and its undo, the words, tags and alt text around a post, its history in words, and the delivery package
// requests. Every screen calls these, so a fix lands in all of them.
import { addDays, addMonths, dateKey, fmtShort, humanWhen, startOfMonth, startOfWeek, timeOf } from "./dom.js";
import { effectiveCaption, effectiveFirstComment, effectiveHashtags, effectiveMedia } from "./panels.js";

// ---------------------------------------------------------------- periods
export function rangeFor(view, anchor) {
  if (view === "day") return [anchor, anchor];
  if (view === "week") { const s = startOfWeek(anchor); return [s, addDays(s, 6)]; }
  const first = startOfMonth(anchor);
  const last = addDays(addMonths(first, 1), -1);
  return [startOfWeek(first), addDays(startOfWeek(last), 6)];
}
export function eachDay(from, to) { const out = []; for (let d = from; d <= to; d = addDays(d, 1)) out.push(d); return out; }
export const stepAnchor = (view, anchor, n) => (view === "month" ? addMonths(anchor, n) : addDays(anchor, n * (view === "week" ? 7 : 1)));

export { humanWhen };                         // "Wednesday 21 October, 09:00" from a local ISO time
export const slotLabel = (local) => `${fmtShort(dateKey(local))}, ${timeOf(local)}`;

// ---------------------------------------------------------------- what can move
export const LOCKED = new Set(["published", "publishing"]);
export const itemLocked = (it) => !!it.locked || LOCKED.has(it.status);
export const lockWhy = (it) => (it.status === "published" ? "Published posts cannot be moved." : it.status === "publishing" ? "This post is being sent right now." : "This post is locked.");

// ---------------------------------------------------------------- what a status means
const REASON = {
  draft: () => "Not in review yet. It becomes ready to approve once the queue has it.",
  in_review: () => "Waiting for your approval, which you give in the review sheet on the publishing computer.",
  approved: () => "Approved, with no time yet. Give it a time and it is ready to go.",
  scheduled: (v, live) => `Approved and scheduled${v.schedule ? ` for ${fmtShort(dateKey(v.schedule.local))}, ${timeOf(v.schedule.local)}` : ""}. ${live ? "It posts at that time." : "This account is in dry run: nothing is sent."}`,
  publishing: () => "Being sent right now.",
  published: (v) => `Published${v.result && v.result.url ? ` at ${v.result.url}` : ""}.`,
  failed: (v) => `The send failed${v.result && v.result.error ? `: ${v.result.error}` : ""}. Nothing was posted.`,
  missed: () => "The time passed without a send, and it is never posted late. Give it a new time.",
  needs_rework: () => "Sent back to Claude, who has not changed it yet.",
  dropped: () => "Dropped. It will not be posted.",
};
export const statusReason = (v, live) => (REASON[v.status] ? REASON[v.status](v, live) : v.status_reason || "");

/** The notes that sent a post back and are still open, for this channel. */
export const openNotes = (detail, v) => (detail.feedback || []).filter((f) => !f.resolved && (f.variant == null || (v && f.variant === v.id))
  && ["rework_copy", "rework_visual", "retime", "note", "drop"].includes(f.kind));
export const noteHead = (n) => `${n.by || "You"} sent it back${n.kind === "rework_copy" ? " (words)" : n.kind === "rework_visual" ? " (picture)" : ""}:`;

// ---------------------------------------------------------------- the words around a post
/** What the calendar calls a post: the first sentence of the caption it will post (as the server's calendar does), else its idea. */
export function postName(post, v) {
  const cap = String(effectiveCaption(post, v) || "").trim().split(/\n/)[0].trim();
  const first = (cap.match(/^.*?[.!?](?=\s|$)/) || [cap])[0].replace(/[.!?]$/, "").trim();
  const text = first || String((post.idea && post.idea.text) || "").trim();
  return text.length > 90 ? text.slice(0, 89).trimEnd() + "…" : text || "Untitled post";
}
/** Hashtags, first comment, link and alt text of one channel ({missing} marks an image with no alt text). */
export function postFacts(post, v) {
  const media = v ? effectiveMedia(post, v) : post.media || [];
  return {
    tags: v ? effectiveHashtags(post, v) : (post.copy && post.copy.hashtags) || [],
    firstComment: v ? effectiveFirstComment(post, v) : (post.copy && post.copy.first_comment) || "",
    link: (post.copy && post.copy.link) || "",
    alts: media.map((m, i) => ({ n: i + 1, alt: String(m.alt || "").trim(), missing: !String(m.alt || "").trim() })),
  };
}
export const MISSING_ALT = "Missing: screen readers get nothing here.";

// ---------------------------------------------------------------- history
export const eventWho = (e) => (e.actor && e.actor.kind === "agent" ? "Claude" : (e.actor && e.actor.name) || "someone");
function stamp(utc) { try { return new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }).format(new Date(utc)); } catch { return String(utc); } }
export function eventLine(e) {
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
/** The last few things that happened to a post, newest first, without views. */
export const recentEvents = (detail, n = 8) => (detail.events || []).filter((e) => e.op !== "viewed").slice().reverse().slice(0, n);

// ---------------------------------------------------------------- retiming results (POST /api/studio/bulk)
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
const when = (s) => (s && s.local ? humanWhen(s.local) : "Unscheduled");

/** One short sentence per bulk result. `titles` maps variant id -> what the person calls the post. */
export function summarize(op, res, { titles = {}, days = null, account = null } = {}) {
  const n = res.done.length;
  const bad = res.refused.length;
  const tail = bad ? ` ${plural(bad, "post was", "posts were")} left where ${bad === 1 ? "it is" : "they are"}: ${res.refused[0].reason}${bad > 1 ? ` (and ${bad - 1} more)` : ""}.` : "";
  const approved = res.done.filter((d) => (d.flags || []).includes("moved_after_approval")).length;
  const keep = approved ? ` ${approved === 1 ? "It was" : `${approved} were`} approved: the approval still holds, and the move is recorded.` : "";
  if (!n) return `Nothing moved.${tail || ""}`;
  if (n === 1 && op !== "shift") {
    const d = res.done[0];
    const title = titles[d.variant] || "the post";
    if (op === "unschedule" || (op === "set" && !d.after)) return `Moved "${title}" to Unscheduled.${keep}${tail}`;
    if (op === "account") return `Moved "${title}" to ${account}.${tail}`;
    return `Moved "${title}" to ${when(d.after)}.${keep}${tail}`;
  }
  const what = op === "shift" ? `Shifted ${plural(n, "post", "posts")} by ${days > 0 ? "+" : "−"}${Math.abs(days)} ${Math.abs(days) === 1 ? "day" : "days"}`
    : op === "slots" ? `Moved ${plural(n, "post", "posts")} to the next free slots`
      : op === "unschedule" ? `Moved ${plural(n, "post", "posts")} to Unscheduled`
        : op === "account" ? `Moved ${plural(n, "post", "posts")} to ${account}`
          : `Rescheduled ${plural(n, "post", "posts")}`;
  return `${what}.${keep}${tail}`;
}

/** The "set" items that put a bulk result back, each guarded by the time it moved to (a later move wins over the undo). */
export const undoItems = (done) => done.map((d) => ({ post: d.post, variant: d.variant, to: d.before ? { local: d.before.local, tz: d.before.tz } : null, if_utc: d.after ? d.after.utc : null }));
export const undoSummary = (r) => (r.refused.length ? `Put ${plural(r.done.length, "post", "posts")} back. ${r.refused.length} could not be restored: ${r.refused[0].reason}.` : `Undone: put ${plural(r.done.length, "post", "posts")} back.`);

/** One bulk request for one post: `op` is "set" (with `to`, or null for Unscheduled), "slots" or "unschedule". */
export const oneItem = (it, op, to) => ({ op, items: [{ post: it.post, variant: it.variant, expected_rev: it.rev, ...(op === "set" ? { to } : {}) }] });

/** A post's next free slots (GET slots), without the slot it already has. `{slots, has_rules}`. */
export async function freeSlots(api, it, count = 6) {
  const r = await api.get(`slots?post=${encodeURIComponent(it.post)}&variant=${encodeURIComponent(it.variant)}&count=${count}`);
  return { ...r, slots: (r.slots || []).filter((s) => !(it.at && s.local === String(it.at).slice(0, 16))) };
}
export const noSlotsText = (r, it) => (r.has_rules ? "No free slot in the next few months." : `${it.account || "This account"} has no schedule rules, so there are no slots to suggest. Pick a time above.`);

// ---------------------------------------------------------------- delivery packages (exact files for posting by hand)
export const buildPackage = (api, post, variant, draft) => api.post(`posts/${post.id}/export`, { expected_rev: post.rev, variant: variant?.id || null, draft });
export const packageHistory = (api, post, variant) => api.get(`posts/${post.id}/export-history?variant=${encodeURIComponent(variant?.id || "")}`);

/** The requests that put a bulk result back. A change of account goes back account by account (`was`: variant -> old account);
 *  everything else is one "set" guarded by the time each post moved to. */
export function undoRequests(body, res, was = {}) {
  if (body.op !== "account") return [{ op: "set", items: undoItems(res.done) }];
  const byOld = new Map();
  for (const d of res.done) { const old = was[d.variant]; if (old) byOld.set(old, [...(byOld.get(old) || []), d]); }
  return [...byOld].map(([account, rows]) => ({ op: "account", account, items: rows.map((d) => ({ post: d.post, variant: d.variant })) }));
}
/** Several posts as bulk items. */
export const bulkItems = (items) => items.map((i) => ({ post: i.post, variant: i.variant, expected_rev: i.rev }));

// ---------------------------------------------------------------- Claude's suggested moves (schedule proposals)
/** A suggestion is shown only when every post it would move is visible: "Accept" applies all of its moves. */
export const proposalVisible = (pr, find) => pr.moves.every((mv) => !!find(mv.variant));
// In the app accepting Claude's moves is a protected act, checked by the app for exactly this suggestion (aveNative.acceptProposal).
export const acceptProposal = (api, id) => (window.aveNative?.acceptProposal ? window.aveNative.acceptProposal(id)
  : api.post(`schedule/proposals/${encodeURIComponent(id)}/accept`, {}));
export const acceptedText = (res) => { const n = (res.applied || []).length, bad = (res.conflicts || []).length;
  return `Applied ${n} suggested ${n === 1 ? "move" : "moves"}${bad ? `; ${bad} could not be applied` : ""}.`; };

// ---------------------------------------------------------------- ideas (posts at stage "idea")
export const ideaSure = (c) => (c == null ? null : Math.round(c * 100));
export const ideaDoubt = (c) => (c == null ? "" : c >= 0.7 ? "Likely to work" : c >= 0.45 ? "Worth a try" : "A long shot");
/** The edit, its undo and the sentence for each thing a person can do to an idea. Promoting gives it its first words. */
export const IDEA_ACTS = {
  promote: (idea) => ({ patch: { "copy.base": (idea && idea.text) || "" }, back: { "copy.base": "" }, said: "Turned into a draft. Open it to add the pictures and an account." }),
  dismiss: () => ({ patch: { stage: "dropped" }, back: { stage: "idea" }, said: "Set aside. It will not come back." }),
  brand: (_, brand) => ({ patch: { brand }, back: { brand: null }, said: `Assigned to ${brand}.` }),
};
export const editPost = (api, post, patch) => api.post(`posts/${encodeURIComponent(post.id)}/edit`, { expected_rev: post.rev, patch });
export const NO_IDEAS = "Ask Claude for ideas from an episode, a blog post or a single line. They land here with the reason Claude gives and how sure it is.";
export const IDEA_COMMAND = "ave studio ingest idea \"Five questions to ask at your own long table\"";

// ---------------------------------------------------------------- delivery packages: what each one is, and the human report
export function packageState(pack, variant) {
  const publication = pack.publication || (variant?.result?.package_id === pack.package_id ? variant.result : null);
  const published = publication?.state === "published";
  return { publication, published, label: published ? "Published · human reported" : pack.reviewed ? "Exported · ready to post" : "Exported · unreviewed draft",
    canReport: !published && !!pack.reviewed };
}
/** The protected "I posted this" record (aveNative.recordManualPublication): only for a reviewed package, only through the app. */
export const publicationRequest = ({ post, pack, url, at, by, changed = "", mismatch = "", requestId }) => ({
  post_id: post.id, variant_id: pack.variant, package_id: pack.package_id, reviewed_payload_hash: pack.reviewed_payload_hash,
  posted_url: url, posted_at: new Date(at).toISOString(), expected_rev: post.rev, request_id: requestId, by, changed_in_native: changed,
  url_mismatch_explanation: mismatch });
export const newRequestId = () => crypto.randomUUID().replaceAll("-", "");
