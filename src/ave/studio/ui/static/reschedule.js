// "Change time": the way to reschedule without dragging. One post gets a date and time picker plus its next free slots; several posts get
// shift-by-days, start-on-a-date or next-free-slots. Everything goes through runBulk (bulk.js), so a refusal is explained in words and an
// approved post keeps its approval.

import { announce, append, clear, dateKey, daysBetween, fmtShort, h, humanWhen, icon, timeOf } from "/app/shared/dom.js";
import { runBulk } from "./bulk.js";
import { openModal } from "/app/shared/panels.js";
import { freeSlots, noSlotsText, slotLabel } from "/app/shared/plan.js";

const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

/** items: [{post, variant, rev, title, at (local ISO or null), tz, account, status, flags}] */
export function openReschedule(ctx, items, { onDone } = {}) {
  return items.length === 1 ? single(ctx, items[0], onDone) : several(ctx, items, onDone);
}

function single(ctx, it, onDone) {
  const tz = it.tz || ctx.tz || "UTC";
  const approved = ["approved", "scheduled"].includes(it.status);
  const today = ctx.today || new Date().toISOString().slice(0, 10);
  const date = h("input", { type: "date", id: "rs-date", name: "date", min: today, required: true, value: it.at ? dateKey(it.at) : "" });
  const time = h("input", { type: "time", id: "rs-time", name: "time", step: "300", required: true, value: it.at ? timeOf(it.at) : "09:00" });
  const err = h("p", { class: "rs-err", role: "alert" });
  const chips = h("div", { class: "rs-slots", role: "group", "aria-label": "Next free slots" }, h("span", { class: "rs-hint" }, "Looking for free slots"));
  let first = null;
  const save = h("button", { type: "submit", class: "primary", form: "rs-form" }, "Move");
  const clearBtn = h("button", { type: "button", class: "quiet", disabled: !it.at, onclick: () => apply({ to: null }) }, icon("tray"), "Move to Unscheduled");
  const m = openModal({
    title: "Change time",
    body: h("form", { id: "rs-form", class: "rs", onsubmit: (e) => { e.preventDefault(); submit(); } },
      h("p", { class: "rs-post" }, h("strong", {}, it.title), it.account ? h("span", { class: "rs-acct" }, it.account) : null),
      h("p", { class: "rs-now" }, it.at ? `Now: ${humanWhen(it.at)}` : "Not scheduled yet.", " ", h("span", { class: "rs-tz" }, `${tz} time`)),
      approved ? h("p", { class: "rs-note" }, icon("info"), "This post is approved. The approval stays: it covers the words and pictures, not the time. The move is recorded and the post is marked Moved after approval.") : null,
      h("div", { class: "rs-field" }, h("label", { for: "rs-date" }, "Date"), date),
      h("div", { class: "rs-field" }, h("label", { for: "rs-time" }, "Time"), time),
      h("div", { class: "rs-slotwrap" }, h("p", { class: "rs-label" }, "Next free slots"), chips),
      err),
    actions: [clearBtn, h("button", { type: "button", onclick: () => m.close("cancel") }, "Cancel"), save],
    wide: false,
  });
  date.focus();

  async function load() {
    try {
      const r = await freeSlots(ctx.api, it);                                                  // its own slot is not a change
      chips.replaceChildren();
      if (!r.slots.length) {
        chips.append(h("span", { class: "rs-hint" }, noSlotsText(r, it)));
        return;
      }
      first = r.slots[0];
      r.slots.forEach((s, i) => chips.append(h("button", {
        type: "button", class: "rs-chip", "aria-pressed": "false", dataset: { slot: s.slot },
        onclick: () => pick(s, i),
      }, slotLabel(s.local))));
    } catch (e) { chips.replaceChildren(h("span", { class: "rs-hint" }, `Could not load slots: ${e.message}`)); }
  }
  function pick(s, i) {
    date.value = dateKey(s.local); time.value = timeOf(s.local);
    [...chips.children].forEach((c, j) => c.setAttribute("aria-pressed", String(j === i)));
    err.textContent = "";
    save.focus();
  }
  async function submit() {
    if (!date.value || !time.value) { err.textContent = "Choose a date and a time."; return; }
    const local = `${date.value}T${time.value}`;
    if (it.at && it.at.slice(0, 16) === local) { err.textContent = "That is the time it already has."; return; }
    await apply({ to: { local, tz } });
  }
  async function apply({ to }) {
    err.textContent = "";
    save.disabled = clearBtn.disabled = true;
    try {
      const res = await runBulk(ctx, { op: "set", items: [{ post: it.post, variant: it.variant, expected_rev: it.rev, to }] }, { titles: { [it.variant]: it.title }, onChange: null });
      if (res.refused.length) { err.textContent = res.refused[0].reason + (res.refused[0].code === "conflict.rev" ? " Close this and open it again." : ""); save.disabled = false; clearBtn.disabled = !it.at; return; }
      m.close("done");
      if (onDone) await onDone(res);
    } catch (e) { err.textContent = e.message; save.disabled = false; clearBtn.disabled = !it.at; }
  }
  load();
  return m;
}

function several(ctx, items, onDone) {
  const scheduled = items.filter((i) => i.at);
  const earliest = scheduled.map((i) => dateKey(i.at)).sort()[0] || ctx.today;
  let mode = "shift", days = 7;
  const today = ctx.today || earliest;
  const out = h("div", { class: "rs-result", role: "status", "aria-live": "polite" });
  const panel = h("div", { class: "rs-mode" });
  const apply = h("button", { type: "button", class: "primary", onclick: () => run() }, "Apply");
  const modes = [["shift", "Shift by days"], ["start", "Start on a date"], ["slots", "Next free slots"]];
  const seg = h("div", { class: "seg rs-seg", role: "radiogroup", "aria-label": "How to reschedule" }, modes.map(([k, label]) =>
    h("button", { type: "button", role: "radio", class: "seg-b", "aria-checked": String(k === mode), dataset: { mode: k }, onclick: () => setMode(k) }, label)));
  let startInput = null, daysInput = null;
  const m = openModal({
    title: `Reschedule ${plural(items.length, "post", "posts")}`,
    body: h("div", { class: "rs rs-multi" },
      h("ul", { class: "rs-list", "aria-label": "Selected posts" }, items.slice(0, 4).map((i) => h("li", {}, h("span", {}, i.title), h("span", { class: "rs-when" }, i.at ? humanWhen(i.at) : "Unscheduled"))),
        items.length > 4 ? h("li", { class: "rs-more" }, `and ${items.length - 4} more`) : null),
      seg, panel, out),
    actions: [h("button", { type: "button", onclick: () => m.close("cancel") }, "Close"), apply],
  });
  setMode("shift");

  function setMode(k) {
    mode = k;
    seg.querySelectorAll("button").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.mode === k)));
    out.textContent = "";
    panel.replaceChildren();
    if (k === "shift") {
      daysInput = h("input", { type: "number", id: "rs-days", name: "days", value: String(days), min: "-366", max: "366", step: "1", "aria-label": "Days to shift (negative moves earlier)", oninput: () => { days = parseInt(daysInput.value, 10) || 0; } });
      const preset = (n, label) => h("button", { type: "button", class: "rs-chip", onclick: () => { days = n; daysInput.value = String(n); } }, label);
      panel.append(
        h("p", {}, "Every post keeps its time of day. Negative numbers move them earlier."),
        h("div", { class: "rs-step" }, h("button", { type: "button", class: "icon-b", "aria-label": "One day earlier", onclick: () => { days -= 1; daysInput.value = String(days); } }, "−"), daysInput,
          h("button", { type: "button", class: "icon-b", "aria-label": "One day later", onclick: () => { days += 1; daysInput.value = String(days); } }, "+"), h("span", { class: "rs-unit" }, "days")),
        h("div", { class: "rs-presets" }, preset(-7, "A week earlier"), preset(-1, "A day earlier"), preset(1, "A day later"), preset(7, "A week later")),
        items.length - scheduled.length ? h("p", { class: "rs-hint" }, `${plural(items.length - scheduled.length, "selected post is", "selected posts are")} in Unscheduled and will be skipped. Use Next free slots to place them.`) : null);
    } else if (k === "start") {
      startInput = h("input", { type: "date", id: "rs-start", name: "start", min: today, value: earliest });
      panel.append(h("p", {}, "The earliest post lands on this date. The others keep their spacing and times of day."),
        h("div", { class: "rs-field" }, h("label", { for: "rs-start" }, "Start on"), startInput));
    } else {
      panel.append(h("p", {}, "Each post takes its own account's next free slot after the time it has now (a post with no time starts from today). Nothing lands on a taken slot or inside quiet hours."));
    }
  }
  async function run() {
    out.textContent = "";
    let body;
    if (mode === "shift") {
      if (!days) { out.textContent = "Choose how many days (not zero)."; return; }
      body = { op: "shift", days, items: items.map((i) => ({ post: i.post, variant: i.variant, expected_rev: i.rev })) };
    } else if (mode === "start") {
      const d = daysBetween(earliest, startInput.value);
      if (!startInput.value || !d) { out.textContent = d === 0 ? "The earliest post is already on that date." : "Choose a date."; return; }
      body = { op: "shift", days: d, items: items.map((i) => ({ post: i.post, variant: i.variant, expected_rev: i.rev })) };
    } else {
      body = { op: "slots", items: items.map((i) => ({ post: i.post, variant: i.variant, expected_rev: i.rev })) };
    }
    apply.disabled = true;
    try {
      const titles = Object.fromEntries(items.map((i) => [i.variant, i.title]));
      const res = await runBulk(ctx, body, { titles, quiet: true, onChange: null });
      showResult(res, body, titles);
      if (onDone) await onDone(res);
    } catch (e) { out.textContent = e.message; }
    apply.disabled = false;
  }
  function showResult(res, body, titles) {
    clear(out);
    append(out, [
      h("p", { class: "rs-done" }, icon(res.done.length ? "check" : "warn"), res.done.length ? `Moved ${plural(res.done.length, "post", "posts")}.` : "Nothing moved."),
      res.refused.length ? h("div", { class: "rs-refused" }, h("p", {}, `${plural(res.refused.length, "post was", "posts were")} left where ${res.refused.length === 1 ? "it is" : "they are"}:`),
        h("ul", {}, res.refused.map((r) => h("li", {}, h("strong", {}, titles[r.variant] || r.post), ": ", r.reason)))) : null,
      res.done.length ? h("p", { class: "rs-hint" }, "The toast on the page has Undo, and so does the z key.") : null]);
    if (res.done.length) announce(`${plural(res.done.length, "post", "posts")} moved.`, { action: { label: "Undo", run: () => import("./bulk.js").then((b) => b.runUndo()) } });
  }
  return m;
}
