// Shared pieces of the Workspace: requests, people, dates, and the popovers every view opens from a cell (stage, person, due date,
// what is waiting on the director). Board, Kanban, the item panel and Home all use these, so a stage picked anywhere looks and
// behaves the same.

import { h, button, icon, popover } from "../dom.js";

export const STAGES = ["Plan", "Draft", "Refine", "Finish", "Out"];
export const stageClass = (s) => `st-${String(s || "plan").toLowerCase()}`;
export const stageVar = (s) => `var(--stage-${String(s || "plan").toLowerCase()})`;
export const GROUP_COLOURS = 8;

export class AppError extends Error {
  constructor(message, { status = 0, network = false, code = "" } = {}) { super(message); this.status = status; this.network = network; this.code = code; }
}

// Attribution follows the channel: a board write over HTTP carries only the room token, which Claude's MCP holds too, so the
// server records it as Claude's (relayed, for a sign-off or an answer). In the app the person's own board writes go through the
// app's native bridge instead (aveNative.boardWrite), which records them as the person's, with the device-owner check for an
// answer that changes a shared settings file.
export const BOARD_WRITE = /^\/api\/workspace\/artifact(\/[^/?#]+)?$|^\/api\/room\/selects\/(decide|nudge)$/;
export async function boardWrite(method, path, body, Err = AppError) {
  if (method === "GET" || !BOARD_WRITE.test(path) || typeof window.aveNative?.boardWrite !== "function") return undefined;
  try { return { data: await window.aveNative.boardWrite(method, path, body || {}) }; }
  catch (e) {
    const msg = String(e?.message || e || "");
    throw new Err(/cancel/i.test(msg) ? "Cancelled. Nothing was changed." : msg, { status: Number(e?.status) || 409 });
  }
}

export async function api(method, path, body) {
  const viaApp = await boardWrite(method, path, body);
  if (viaApp) return viaApp.data;
  let r;
  try {
    r = await fetch(path, { method, credentials: "same-origin", headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined });
  } catch (e) { throw new AppError(String(e), { network: true }); }
  let data = {};
  try { data = await r.json(); } catch { /* an empty or non-JSON reply */ }
  if (!r.ok) throw new AppError(data.error || `HTTP ${r.status}`, { status: r.status, code: data.code || "" });
  return data;
}

// The workspace's own words for a failed write. Its 400 and 409 answers are written for the person, so they are shown as they are.
export function plain(e, doing) {
  if (e.network) return `Could not ${doing}. The app is not answering: check that AVE Studio is still open.`;
  if (e.status === 409 || e.status === 400) return e.message;
  if (e.status === 404) return `Could not ${doing}. It is no longer on the board.`;
  return `Could not ${doing}. Try again.`;
}

// ---- people
export const isClaude = (name) => String(name || "").toLowerCase() === "claude";
export function initials(name) {
  const parts = String(name || "?").trim().split(/[\s._-]+/).filter(Boolean);
  return ((parts[0]?.[0] || "?") + (parts.length > 1 ? parts.at(-1)[0] : "")).toUpperCase();
}
function hue(name) { let n = 0; for (const c of String(name)) n = (n * 31 + c.charCodeAt(0)) >>> 0; return n % GROUP_COLOURS + 1; }
export function avatar(name, { size = "sm", live = false, cls = "" } = {}) {
  if (!name) return h("span", { class: `avatar avatar-${size} avatar-empty ${cls}`, "aria-label": "Nobody" }, icon("person"));
  const claude = isClaude(name);
  return h("span", { class: `avatar avatar-${size} ${claude ? "avatar-claude" : ""} ${live ? "avatar-live" : ""} ${cls}`.trim(), role: "img",
    "aria-label": claude ? "Claude" : name, title: claude ? "Claude" : name,
    style: claude ? null : { background: `var(--group-${hue(name)})` } }, claude ? "C" : initials(name));
}

// ---- dates (due dates are plain YYYY-MM-DD; stamps are ms)
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
export function fmtDate(d) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(d || "");
  if (!m) return "";
  const year = +m[1] !== new Date().getFullYear() ? `, ${m[1]}` : "";
  return `${MONTHS[+m[2] - 1]} ${+m[3]}${year}`;
}
export const isoDay = (date) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
export function isLate(row) { return !!row.due && row.stage !== "Out" && row.due < isoDay(new Date()); }
export function ago(ms, now = Date.now()) {
  if (!ms) return "";
  const s = Math.max(0, (now - ms) / 1000);
  if (s < 50) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  if (s < 172800) return "yesterday";
  const d = new Date(ms);
  return `${MONTHS[d.getMonth()]} ${d.getDate()}`;
}
// A date the person types: 2026-11-04, "nov 4", "4 nov", "today", "tomorrow"; null when it cannot be read.
export function parseDay(text) {
  const t = String(text || "").trim().toLowerCase();
  if (!t) return "";
  const today = new Date();
  if (t === "today") return isoDay(today);
  if (t === "tomorrow") return isoDay(new Date(today.getTime() + 86400000));
  if (/^\d{4}-\d{2}-\d{2}$/.test(t)) return t;
  const m = /^([a-z]{3})[a-z]*\.?\s+(\d{1,2})(?:,?\s+(\d{4}))?$/.exec(t) || /^(\d{1,2})\s+([a-z]{3})[a-z]*\.?(?:,?\s+(\d{4}))?$/.exec(t);
  if (!m) return null;
  const [mon, day] = /^\d/.test(m[1]) ? [m[2], m[1]] : [m[1], m[2]];
  const mi = MONTHS.findIndex((x) => x.toLowerCase() === mon);
  if (mi < 0) return null;
  const d = new Date(m[3] ? +m[3] : today.getFullYear(), mi, +day);
  return d.getDate() === +day ? isoDay(d) : null;
}

// ---- menus: a list of items in a popover, arrow keys move, Enter picks, Esc closes and returns focus
export function menu(anchor, items, { label, align = "start", place = "below", heading } = {}) {
  const list = h("div", { class: "menu-body", role: "menu", "aria-label": label });
  if (heading) list.append(h("p", { class: "menu-heading" }, heading));
  for (const it of items) {
    if (it === "-") { list.append(h("div", { class: "menu-sep", role: "separator" })); continue; }
    const role = it.checked == null ? "menuitem" : "menuitemradio";
    const b = h("button", { type: "button", class: "menu-item", role, "aria-checked": it.checked == null ? null : String(!!it.checked), disabled: !!it.disabled,
      title: it.title || null, onclick: () => { pop.close(); it.onSelect?.(); } },
      it.icon ? icon(it.icon) : (it.checked != null ? (it.checked ? icon("check") : h("span", { style: { width: "16px" } })) : null),
      it.lead || null, h("span", {}, it.label), it.hint ? h("span", { class: "menu-hint" }, it.hint) : null);
    list.append(b);
    if (it.note) list.append(h("p", { class: "menu-note" }, it.note));
  }
  const pop = popover(anchor, list, { label, align, place });
  pop.el.classList.add("popover-menu");
  navigate(list, ".menu-item");
  (list.querySelector(".menu-item[aria-checked='true']:not(:disabled)") || list.querySelector(".menu-item:not(:disabled)"))?.focus();
  return pop;
}

export function navigate(list, sel) {
  list.addEventListener("keydown", (e) => {
    const all = [...list.querySelectorAll(sel)].filter((b) => !b.disabled);
    const i = all.indexOf(document.activeElement);
    if (e.key === "ArrowDown") { e.preventDefault(); all[(i + 1) % all.length]?.focus(); }
    if (e.key === "ArrowUp") { e.preventDefault(); all[(i - 1 + all.length) % all.length]?.focus(); }
    if (e.key === "Home") { e.preventDefault(); all[0]?.focus(); }
    if (e.key === "End") { e.preventDefault(); all.at(-1)?.focus(); }
  });
}

// ---- stage: solid colour choices; Out is locked, with the reason right under it
export function stagePopover(anchor, current, onPick, { outReason } = {}) {
  const list = h("div", { class: "stage-list", role: "menu", "aria-label": "Stage" });
  for (const s of STAGES) {
    const out = s === "Out";
    list.append(h("button", { type: "button", class: `stage-opt ${stageClass(s)}`, role: "menuitemradio", "aria-checked": String(s === current),
      disabled: out && current !== "Out", onclick: () => { pop.close(); if (s !== current) onPick(s); } }, s, out && current !== "Out" ? icon("lock") : null));
  }
  const why = h("p", { class: "stage-why" }, outReason || "Out needs the publish approval, given in the approval sheet. Moving the stage by hand cannot publish anything.");
  const wrap = h("div", {}, h("p", { class: "popover-title" }, "Stage"), list, why);
  const pop = popover(anchor, wrap, { label: "Stage" });
  navigate(list, ".stage-opt");
  (list.querySelector("[aria-checked='true']:not(:disabled)") || list.querySelector(".stage-opt:not(:disabled)"))?.focus();
  return pop;
}

// ---- who: the project's members and Claude
export function peoplePopover(anchor, people, current, onPick) {
  const names = [...new Set([...(people || []).filter(Boolean), "Claude"])];
  return menu(anchor, [
    ...names.map((n) => ({ label: isClaude(n) ? "Claude" : n, lead: avatar(n), checked: (current || "") === n, onSelect: () => onPick(n) })),
    "-",
    { label: "Nobody", lead: avatar(null), checked: !current, onSelect: () => onPick(null) },
  ], { label: "Who", heading: "Who is on it" });
}

// ---- due date: type it (Enter saves, Esc cancels) or pick a quick one
export function datePopover(anchor, current, onPick) {
  const inp = h("input", { class: "input", value: current || "", placeholder: "2026-11-04 or Nov 4", "aria-label": "Due date" });
  const err = h("p", { class: "field-label", hidden: true, style: { color: "var(--negative)" } }, "Write a date like 2026-11-04 or Nov 4");
  const save = () => {
    const v = parseDay(inp.value);
    if (v === null) { inp.setAttribute("aria-invalid", "true"); err.hidden = false; return; }
    pop.close();
    if ((v || null) !== (current || null)) onPick(v || null);
  };
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); save(); } });
  inp.addEventListener("input", () => { inp.removeAttribute("aria-invalid"); err.hidden = true; });
  const d = new Date();
  const quick = [["Today", 0], ["Tomorrow", 1], ["In a week", 7]].map(([l, n]) =>
    button(l, { variant: "secondary", size: "sm", onclick: () => { inp.value = isoDay(new Date(d.getTime() + n * 86400000)); save(); } }));
  const body = h("div", { class: "date-pop" }, h("p", { class: "popover-title" }, "Due date"),
    h("label", { class: "field" }, inp), err, h("div", { class: "date-quick" }, quick),
    h("div", { class: "popover-actions" }, current ? button("Clear date", { variant: "tertiary", size: "sm", onclick: () => { pop.close(); onPick(null); } }) : null,
      button("Save", { variant: "primary", size: "sm", onclick: save })));
  const pop = popover(anchor, body, { label: "Due date" });
  inp.focus(); inp.select();
  return pop;
}

// ---- what waits on the director: Claude's reported words (Confirm / Undo), decisions with their options, a stage chosen on two computers
export function waitingContent(row, act, { onClose } = {}) {
  const items = [];
  for (const r of row.relayed || []) {
    items.push(h("div", { class: "pop-item" },
      h("p", { class: "pop-who" }, avatar("Claude"), r.quote ? h("span", {}, "Claude reports you said: ", h("strong", { class: "pop-quote" }, `“${r.quote}”`))
        : h("span", {}, "Claude did this without your words")),
      h("p", { class: "pop-reason" }, r.type === "sign_off" ? `So ${r.from} is signed off and it moved to ${r.to}.`
        : r.type === "settle" ? `So the stage changed on two computers is settled on ${r.to}.` : `So the answer is “${r.choice}”.`),
      h("div", { class: "pop-actions" },
        button("Confirm", { variant: "primary", size: "sm", iconName: "check", disabled: !window.aveNative?.confirmRelayed, class: "tip",
          "data-tip": window.aveNative?.confirmRelayed ? "This Mac checks that it is you" : "Confirming needs the AVE Studio app: it checks that it is you",
          onclick: (e) => act.confirm(row, r, e.currentTarget).then(onClose) }),
        button("Undo", { variant: "secondary", size: "sm", iconName: "undo", disabled: !r.can_undo,
          title: r.can_undo ? null : "It has moved on since. Set the stage by hand instead.",
          onclick: (e) => act.undo(row, r, e.currentTarget).then(onClose) }))));
  }
  for (const d of row.decisions || []) {
    if (d.kind === "setting") { items.push(settingItem(row, d, act, onClose)); continue; }
    items.push(h("div", { class: "pop-item" },
      h("p", { class: "pop-who" }, avatar("Claude"), "Claude decided alone"),
      h("p", { class: "popover-title", style: { margin: 0 } }, d.summary),
      d.reason ? h("p", { class: "pop-reason" }, d.reason) : null,
      h("div", { class: "pop-actions" }, d.options.map((o) => button(o, { variant: o === d.chosen ? "primary" : "secondary", size: "sm",
        onclick: (e) => act.resolve(row, d, o, e.currentTarget).then(onClose) }))),
      h("p", { class: "opt-chosen" }, `Claude chose “${d.chosen}”. Pick it to approve, or another to change it.`),
      d.approval === "trim" && act.openTrim ? h("p", { class: "pop-reason" }, "Answering here keeps the draft as it is. The final cut needs your own trim: ",
        button("Confirm it in the trim sheet", { variant: "tertiary", size: "sm", iconName: "lock", onclick: () => { onClose?.(); act.openTrim(row, d); } })) : null));
  }
  if (row.conflict) items.push(conflictItem(row, act, onClose));
  return h("div", { class: "pop-list" }, items);
}

// Two people changed one setting at once: both values, who made each, one button per side. Nothing is merged until one is picked.
export function settingItem(row, d, act, onClose) {
  const people = d.people || [d.file_who, d.copy_who].filter(Boolean);
  return h("div", { class: "pop-item" },
    h("p", { class: "pop-who" }, h("span", { class: "avatars" }, people.map((n) => avatar(n))), h("span", {}, "Changed on two computers at once")),
    h("p", { class: "popover-title", style: { margin: 0 } }, d.summary),
    h("p", { class: "pop-reason" }, d.reason || "Both are kept until you choose."),
    settingChooser(d, {
      all: (option, btn) => act.resolve(row, d, option, btn).then(onClose),
      picks: (picks, btn) => act.resolvePicks(row, d, picks, btn).then(onClose) }));
}

// Two copies of a settings file, side by side: one row per setting that differs ("Maren's file: X / Tomas's file: Y", "(not
// set)" where a file has none), a choice per row, or all of one file. Nothing is written until the person applies.
export const PER_KEY = "Picked setting by setting";
// The side labels are the record's own words ("Maren's file", "This computer's copy"), fixed when the question was asked:
// never worked out again here. A record from before they were stored falls back to "<who>'s file".
export const sideLabels = (d) => [d.file_label || (d.file_who ? `${d.file_who}'s file` : "This computer's copy"),
  d.copy_label || (d.copy_who ? `${d.copy_who}'s file` : "The other computer's copy")];
export function settingChooser(d, { all, picks }) {
  const keys = d.keys || [];
  const [fileLabel, copyLabel] = sideLabels(d);
  const optionButtons = (d.options || []).map((o) => button(o, { variant: "secondary", size: "sm", onclick: (e) => all(o, e.currentTarget) }));
  if (d.whole || !keys.length) {
    // one side is not a settings object (or a text file): both whole files, side by side, to read before choosing
    const side = (label, text, form) => h("div", { class: "whole-side" },
      h("p", { class: "pick-who" }, label, form ? h("span", { class: "whole-form" }, form) : null),
      h("pre", { class: "whole-text", tabindex: "0", "aria-label": label }, text ?? ""));
    return h("div", { class: "chooser" },
      h("div", { class: "whole-pair" }, side(fileLabel, d.texts?.file, d.forms?.file), side(copyLabel, d.texts?.copy, d.forms?.copy)),
      h("div", { class: "pop-actions pop-actions-wrap" }, optionButtons));
  }
  const chosen = new Map();
  const apply = button("Apply my choices", { variant: "primary", size: "sm", disabled: true,
    onclick: (e) => picks(Object.fromEntries(chosen), e.currentTarget) });
  const rows = keys.map((k) => {
    const opt = (sideKey, label, value) => {
      const b = h("button", { type: "button", class: "pick", "aria-pressed": "false" },
        h("span", { class: "pick-who" }, label), h("span", { class: "pick-value" }, value));
      b.addEventListener("click", () => {
        chosen.set(k.key, sideKey);
        for (const x of b.parentElement.children) x.setAttribute("aria-pressed", String(x === b));
        apply.disabled = chosen.size !== keys.length;
      });
      return b;
    };
    return h("div", { class: "pick-row", role: "group", "aria-label": k.label },
      h("span", { class: "pick-key" }, k.label),
      h("div", { class: "pick-opts" }, opt("file", fileLabel, k.file), opt("copy", copyLabel, k.copy)));
  });
  return h("div", { class: "chooser" },
    h("div", { class: "pick-list" }, rows),
    h("div", { class: "pop-actions pop-actions-wrap" }, optionButtons, apply));
}

// A stage changed on two computers: "Maren and Tomas both changed Refine", and the stage each one chose.
export function conflictItem(row, act, onClose) {
  const c = row.conflict;
  const choices = (c.choices || c.options.map((s) => ({ stage: s, who: "", label: s }))).filter((ch) => ch.stage !== "Out");
  return h("div", { class: "pop-item" },
    h("p", { class: "pop-who" }, h("span", { class: "avatars" }, (c.choices || []).map((ch) => avatar(ch.who.split(" and ")[0]))), h("span", {}, "Changed on two computers at once")),
    h("p", { class: "popover-title", style: { margin: 0 } }, c.text || "The stage was changed on two computers"),
    h("p", { class: "pop-reason" }, c.detail || "Both changes are kept. Choose the stage it should be in."),
    h("div", { class: "pop-actions pop-actions-wrap" }, choices.map((ch) =>
      h("button", { type: "button", class: `pill pill-choice ${stageClass(ch.stage)}`, title: ch.label,
        onclick: (e) => { act.stage([row], ch.stage, { btn: e.currentTarget }); onClose?.(); } },
        h("span", {}, ch.stage), ch.who ? h("span", { class: "pill-who" }, ch.who) : null))));
}

export function waitingPopover(anchor, row, act) {
  let pop;
  const content = waitingContent(row, act, { onClose: () => pop?.close(false) });
  pop = popover(anchor, h("div", {}, h("p", { class: "popover-title" }, `Waiting on you: ${row.name}`), content), { label: "Waiting on you", wide: true });
  pop.el.querySelector("button:not(:disabled)")?.focus();
  return pop;
}

// ---- presence: the other people working on a row right now, until their presence runs out (`now` is the app's clock)
export const presentOn = (row, now) => (row.presence || []).filter((p) => p.until > now);

export function waitingCount(row) { return (row.relayed?.length || 0) + (row.decisions?.length || 0) + (row.conflict ? 1 : 0); }

// ---- names that share a long beginning ("The Long Table, episode 12", "... 13") stay apart: cut the middle, keep the end that differs
export function midTruncate(text, max = 24) {
  const s = String(text || "");
  if (s.length <= max) return s;
  let tail = s.slice(-Math.ceil(max * 0.5));
  const sp = tail.indexOf(" ");
  if (sp > 0 && sp < tail.length - 4 && /\d/.test(tail.slice(sp + 1)) === /\d/.test(tail)) tail = tail.slice(sp + 1);
  let head = s.slice(0, max - tail.length - 1);
  const cut = head.lastIndexOf(" ");
  if (/[\p{L}\p{N}]/u.test(s[head.length] || "") && cut >= 4) head = head.slice(0, cut);      // never stop mid-word
  return `${head.replace(/[\s,;:.\-]+$/, "")}…${tail.trimStart()}`;
}

// A project's tile: the part of its name that tells it from the others ("12" for "The Long Table, episode 12"), else its first letter.
export function tileOf(title, titles = []) {
  const t = String(title || "?");
  const others = titles.filter((x) => x && x !== t);
  let rest = t;
  if (others.length) {
    let n = 0;
    while (n < t.length && others.some((o) => o.startsWith(t.slice(0, n + 1)))) n += 1;
    const cut = t.slice(0, n).lastIndexOf(" ");
    rest = n ? t.slice(cut >= 0 ? cut + 1 : 0) : t;
  }
  const num = rest.match(/\d+/g);
  if (num && others.length) return num.at(-1).slice(-3);
  const word = rest.replace(/^[^\p{L}\p{N}]+/u, "");
  return (word[0] || t[0] || "?").toUpperCase();
}

export const lowerFirst = (s) => (s ? s[0].toLowerCase() + s.slice(1) : s);

// ---- one way to follow a drag. It ends on the drop (pointerup) and is abandoned, cleanly, on everything else that can end a drag
// without one: pointercancel, losing pointer capture, the window losing focus, Esc, a route change, or a minute with no movement.
// `cleanup` always runs (ghost, classes, held live updates); `drop` runs only for a real drop.
export const DRAG_SAFETY_MS = 60000;
export function trackDrag(e, { threshold = 4, start, move, drop, cleanup }) {
  const x0 = e.clientX, y0 = e.clientY, target = e.target;
  let on = false, done = false, timer = 0;
  try { target.setPointerCapture?.(e.pointerId); } catch { /* a synthetic event has no pointer to capture */ }
  const safety = Number(globalThis.__aveDragSafetyMs) || DRAG_SAFETY_MS;
  const arm = () => { clearTimeout(timer); timer = setTimeout(cancel, safety); };
  const mv = (ev) => {
    if (!on && Math.hypot(ev.clientX - x0, ev.clientY - y0) < threshold) return;
    if (!on) { on = true; start?.(ev); }
    arm();
    move?.(ev);
  };
  const finish = (ev, dropped) => {
    if (done) return;
    done = true;
    clearTimeout(timer);
    window.removeEventListener("pointermove", mv);
    window.removeEventListener("pointerup", up);
    window.removeEventListener("pointercancel", cancel);
    window.removeEventListener("blur", cancel);
    window.removeEventListener("hashchange", cancel);
    window.removeEventListener("keydown", key, true);
    target.removeEventListener("lostpointercapture", cancel);
    if (!on) return;
    try { cleanup?.(); } finally { if (dropped) drop?.(ev); }
  };
  const up = (ev) => finish(ev, true);
  function cancel() { finish(null, false); }
  const key = (ev) => { if (ev.key === "Escape") { ev.preventDefault(); ev.stopPropagation(); cancel(); } };
  window.addEventListener("pointermove", mv);
  window.addEventListener("pointerup", up);
  window.addEventListener("pointercancel", cancel);
  window.addEventListener("blur", cancel);
  window.addEventListener("hashchange", cancel);
  window.addEventListener("keydown", key, true);
  target.addEventListener("lostpointercapture", cancel);
  arm();
  return { cancel };
}
