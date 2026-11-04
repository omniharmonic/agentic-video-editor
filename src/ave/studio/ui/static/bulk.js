// Retiming from the UI, in one place: the calendar's selection bar, the side preview, the editor and the keyboard all call runBulk().
// Every change goes through POST /api/studio/bulk, which applies the same checks as a drag, never approves anything, and reports each post as
// done or refused. The last change is kept as ONE undo (the toast button, the header button and the z key all run it).

import { announce } from "/app/shared/dom.js";
import { summarize, undoRequests, undoSummary } from "/app/shared/plan.js";

export { summarize };

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

/** Run a bulk op. `body` = {op, items:[{post, variant, expected_rev?, to?}], days?, account?}. Returns the server's {done, refused}; announces
 *  the outcome with an Undo; never throws for a refusal (that is in `refused`), only for a transport or request error. */
export async function runBulk(ctx, body, { titles = {}, quiet = false, onChange = null, was = {} } = {}) {
  const res = await ctx.api.post("bulk", body);
  const text = summarize(body.op, res, { titles, days: body.days, account: body.account });
  if (res.done.length) {
    const back = undoRequests(body, res, was);
    const undo = async () => {
      let r = { done: [], refused: [] };
      for (const req of back) { const x = await ctx.api.post("bulk", req); r = { done: [...r.done, ...x.done], refused: [...r.refused, ...x.refused] }; }
      announce(body.op === "account" ? `Put ${r.done.length} ${r.done.length === 1 ? "post" : "posts"} back on ${back.length} ${back.length === 1 ? "account" : "accounts"}.` : undoSummary(r),
        { tone: r.refused.length ? "error" : "info" });
      if (onChange) await onChange();
    };
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
