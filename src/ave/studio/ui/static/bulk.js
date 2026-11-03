// Retiming from the UI, in one place: the calendar's selection bar, the side preview, the editor and the keyboard all call runBulk().
// Every change goes through POST /api/studio/bulk, which applies the same checks as a drag, never approves anything, and reports each post as
// done or refused. The last change is kept as ONE undo (the toast button, the header button and the z key all run it).

import { announce, humanWhen } from "./dom.js";

let last = null;
const subs = new Set();
export const getUndo = () => last;
export const onUndoChange = (fn) => { subs.add(fn); return () => subs.delete(fn); };
export function setUndo(u) { last = u; for (const fn of subs) fn(last); }
export async function runUndo() {
  const u = last;
  if (!u) return announce("Nothing to undo.");
  setUndo(null);
  try { await u.run(); } catch (e) { announce(`Could not undo: ${e.message}`, { tone: "error" }); }
}

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

function undoItems(done) {
  return done.map((d) => ({ post: d.post, variant: d.variant, to: d.before ? { local: d.before.local, tz: d.before.tz } : null, if_utc: d.after ? d.after.utc : null }));
}

/** Run a bulk op. `body` = {op, items:[{post, variant, expected_rev?, to?}], days?, account?}. Returns the server's {done, refused}; announces
 *  the outcome with an Undo; never throws for a refusal (that is in `refused`), only for a transport or request error. */
export async function runBulk(ctx, body, { titles = {}, quiet = false, onChange = null, was = {} } = {}) {
  const res = await ctx.api.post("bulk", body);
  const text = summarize(body.op, res, { titles, days: body.days, account: body.account });
  if (res.done.length) {
    let undo;
    if (body.op === "account") {
      const byOld = new Map();
      for (const d of res.done) { const old = was[d.variant]; if (old) byOld.set(old, [...(byOld.get(old) || []), d]); }
      undo = async () => {
        let back = 0;
        for (const [acct, rows] of byOld) {
          const r = await ctx.api.post("bulk", { op: "account", account: acct, items: rows.map((d) => ({ post: d.post, variant: d.variant })) });
          back += r.done.length;
        }
        announce(`Put ${plural(back, "post", "posts")} back on ${plural(byOld.size, "account", "accounts")}.`);
        if (onChange) await onChange();
      };
    } else {
      const items = undoItems(res.done);
      undo = async () => {
        const r = await ctx.api.post("bulk", { op: "set", items });
        announce(r.refused.length ? `Put ${plural(r.done.length, "post", "posts")} back. ${r.refused.length} could not be restored: ${r.refused[0].reason}.` : `Undone: put ${plural(r.done.length, "post", "posts")} back.`,
          { tone: r.refused.length ? "error" : "info" });
        if (onChange) await onChange();
      };
    }
    const entry = { label: "Undo", text, run: undo };
    setUndo(entry);
    if (!quiet) announce(text, { tone: res.refused.length && !res.done.length ? "error" : "info", action: entry });
  } else if (!quiet) {
    announce(text, { tone: "error" });
  }
  if (onChange) await onChange(res);
  return res;
}

/** The post's next free slot, in one click. Uses the same checks as a drag; says why when the account has no free slot. */
export function nextFreeSlot(ctx, item, opts = {}) {
  return runBulk(ctx, { op: "slots", items: [{ post: item.post, variant: item.variant, expected_rev: item.rev }] }, { titles: { [item.variant]: item.title }, ...opts });
}
