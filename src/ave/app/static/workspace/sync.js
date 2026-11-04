// Each project's sync state as a status pill, in the words people use: "Up to date", "Sending 3 files to Imani",
// "Waiting for Imani (offline)", "Paused: the drive is low on space", "Drive not connected", "Tailscale is off".
// The words come from the app (ave.syncthing.words); this file only shows them. The first read is GET /api/workspace/sync,
// then `sync` events on the Room's stream carry every change, so a pill never needs a reload.
//
// A pill opens a popover with what it means, each member's state, and the one next step when there is one. A shared project
// whose drive is unplugged is not on the board (its folder is gone), so it gets its own quiet group saying so.

import { h, icon, popover } from "../dom.js";
import { api, avatar } from "./common.js";

const TONE_ICON = { ok: "check", busy: "sync", wait: "clock", warn: "alert", bad: "alert", quiet: null };

export function createSync(ctx) {
  let data = { available: false, projects: {}, missing: [] };
  let missingSig = "[]";

  const of = (pid) => data.projects?.[pid] || null;

  function paintPill(el, w) {
    el.hidden = !w || w.state === "not_shared" && el.dataset.where === "group";
    if (!w) return;
    el.dataset.tone = w.tone;
    el.dataset.state = w.state;
    el.className = `pill sync-pill sync-${w.tone}`;
    const ic = TONE_ICON[w.tone];
    el.replaceChildren(...(ic ? [icon(ic)] : []), h("span", { class: "sync-text" }, w.text));
    el.setAttribute("aria-label", `Sync: ${w.text}. Show details`);
    el.classList.add("tip");
    el.dataset.tip = w.text;                               // the whole text when the pill is cut short
  }

  // `where` is "group" (a group header: hidden while not shared, which is most projects) or "head" (the board header).
  function pill(pid, where = "group", title = "") {
    const el = h("button", { type: "button", class: "pill sync-pill", "data-sync": pid, "data-where": where, "aria-haspopup": "dialog",
      "aria-expanded": "false", hidden: true });
    el.addEventListener("click", (e) => { e.stopPropagation(); details(el, pid, title); });
    el.addEventListener("keydown", (e) => e.stopPropagation());         // the board's arrow keys never steal the pill's focus
    paintPill(el, of(pid));
    return el;
  }

  function details(anchor, pid, title) {
    const w = of(pid) || data.missing.find((m) => m.id === pid);
    if (!w) return;
    const p = ctx.projects.get(pid);
    popover(anchor, h("div", { class: "sync-pop" },
      h("p", { class: "popover-title" }, title || p?.title || w.title || "This project"),
      h("p", { class: `sync-pop-state sync-${w.tone}` }, h("span", { class: "dot" }), h("span", {}, w.text)),
      w.detail ? h("p", { class: "secondary-text" }, w.detail) : null,
      w.members?.length && !(w.members.length === 1 && w.members[0].text === w.text) ? h("ul", { class: "sync-members", "aria-label": "Members" },
        w.members.map((m) => h("li", { class: "pop-who", "data-member-state": m.state }, avatar(m.name), h("span", {}, m.text)))) : null,
      w.next ? h("p", { class: "sync-next" }, icon("info"), h("span", {}, w.next)) : null),
    { label: `Sync for ${title || p?.title || w.title || "this project"}` });
  }

  function missingGroups() {
    if (!data.missing?.length || ctx.S.route.project || ctx.S.group !== "project" || ctx.S.q) return [];
    // the same columns as every other group: a (hidden) handle, the toggle's icon slot, the title, then the pill
    return data.missing.map((m) => h("section", { class: "grp grp-missing", "data-missing": m.id, "aria-label": m.title },
      h("header", { class: "grp-head" }, h("span", { class: "btn btn-tertiary btn-icon btn-sm grp-handle", "aria-hidden": "true" }, icon("grip")),
        h("span", { class: "grp-toggle", "aria-disabled": "true" }, icon("folder"), h("span", { class: "grp-title" }, m.title)),
        (() => { const el = pill(m.id, "missing", m.title); paintPill(el, m); return el; })()),
      h("div", { class: "grp-missing-body" }, h("p", {}, m.detail), h("p", { class: "secondary-text" }, m.next),
        m.path ? h("p", { class: "sync-path" }, `It was at ${m.path}`) : null)));
  }

  function apply(next) {
    if (!next || typeof next !== "object") return;
    data = { available: !!next.available, projects: next.projects || {}, missing: next.missing || [] };
    for (const el of document.querySelectorAll(".sync-pill[data-sync]")) {
      if (el.dataset.where === "missing") continue;
      paintPill(el, of(el.dataset.sync));
    }
    const sig = JSON.stringify(data.missing);
    if (sig !== missingSig) { missingSig = sig; ctx.rerender(); }
    if (ctx.probe) ctx.probe.sync = data;
  }

  // Before the app's first look the answer is `pending`: ask again shortly (the look also arrives as a `sync` event).
  let retry = 0;
  const ask = (tries) => api("GET", "/api/workspace/sync").then((d) => {
    apply(d);
    clearTimeout(retry);
    if (d?.pending && tries < 10) retry = setTimeout(() => ask(tries + 1), 500);
  }).catch(() => { /* no sync to show; the pills stay hidden */ });
  const load = () => ask(0);                // every load (first open, a resync) gets its own ten tries

  return { pill, apply, load, missingGroups, of };
}
