// Home: a greeting, the projects on this computer as preview cards, what is waiting on the director across all of them (answerable
// right here), and what happened recently.

import { h, button } from "../dom.js";
import { STAGES, stageVar, avatar, ago, waitingContent } from "./common.js";

export function createHome(ctx) {
  const { S, rows, act } = ctx;
  const el = h("div", { class: "home", id: "main", tabindex: "-1" });

  function greeting() {
    const hr = new Date().getHours();
    const part = hr < 12 ? "Good morning" : hr < 18 ? "Good afternoon" : "Good evening";
    const me = ctx.me();
    return me ? `${part}, ${me[0].toUpperCase()}${me.slice(1)}` : part;
  }
  function preview(p, list) {
    const shown = list.slice(0, 6);
    return h("div", { class: "proj-prev", "aria-hidden": "true" }, h("span", { class: "proj-prev-title" }),
      shown.map((r) => h("div", { class: "proj-prev-row" }, h("span"), h("span"), h("span", { class: "seg", style: { "--seg": stageVar(r.stage) } }),
        h("span", { class: "seg", style: { "--seg": r.waiting === "you" ? "var(--waiting)" : "var(--surface)" } }))));
  }
  function segBar(list) {
    const n = {};
    for (const r of list) n[r.stage] = (n[r.stage] || 0) + 1;
    return h("div", { class: "seg-bar seg-bar-thin", role: "img", "aria-label": STAGES.filter((s) => n[s]).map((s) => `${s}: ${n[s]}`).join(", ") },
      STAGES.filter((s) => n[s]).map((s) => h("span", { style: { "--n": String(n[s]), "--seg": stageVar(s) } })));
  }
  function render() {
    if (!S.data) {
      el.replaceChildren(h("div", { class: "home-hello" }, h("div", {}, h("span", { class: "skeleton skeleton-title", style: { width: "320px", height: "32px" } }))),
        h("div", { class: "proj-cards" }, Array.from({ length: 3 }, () => h("div", { class: "card", style: { height: "200px", padding: "16px" } },
          h("span", { class: "skeleton", style: { height: "110px" } }), h("span", { class: "skeleton skeleton-text", style: { width: "60%", marginTop: "12px" } })))));
      if (S.error) el.replaceChildren(h("div", { class: "ws-error", role: "alert" }, h("h2", {}, "The Workspace could not load"),
        h("p", { class: "secondary-text" }, "The app is not answering. Check that AVE Studio is still open, then try again."),
        button("Try again", { variant: "primary", onclick: () => location.reload() })));
      return;
    }
    const all = [...rows.values()].filter((r) => !r.archived);
    const ps = [...ctx.projects.values()].sort((a, b) => last(b, all) - last(a, all));
    const waiting = all.filter((r) => r.waiting === "you").sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0));
    const nWait = waiting.reduce((n, r) => n + (r.relayed?.length || 0) + (r.decisions?.length || 0) + (r.conflict ? 1 : 0), 0);
    const cards = ps.map((p) => {
      const list = all.filter((r) => r.project === p.id);
      const w = list.filter((r) => r.waiting === "you").length;
      return h("a", { class: "card proj-card", href: `#/workspace/p/${encodeURIComponent(p.id)}`, style: { "--group": p.colour }, "aria-label": `${p.title}: ${list.length} artifacts${w ? `, ${w} waiting on you` : ""}` },
        preview(p, list),
        h("div", { class: "proj-meta" }, h("strong", {}, p.title),
          h("span", { class: "secondary-text" }, `${list.length} ${list.length === 1 ? "artifact" : "artifacts"}${w ? ` · ${w} waiting on you` : ""}`),
          list.length ? segBar(list) : null));
    });
    const waitEls = waiting.map((r) => h("div", { class: "pop-item" },
      h("div", { class: "home-wait-name" },
        h("button", { type: "button", class: "link", onclick: () => ctx.openPanel(r.key, "decisions") }, r.name),
        ctx.projectOf(r)?.title !== r.name ? h("span", { class: "secondary-text" }, ctx.projectOf(r)?.title || "") : null),
      waitingContent(r, act, {})));
    const feed = (S.data.activity || []).slice(0, 20).map((a) => h("div", { class: "feed-item" }, avatar(a.who),
      h("p", { class: "feed-text" }, h("strong", {}, a.who), ` ${a.text}`,
        a.key && rows.has(a.key) ? [" ", h("button", { type: "button", class: "link", onclick: () => ctx.openPanel(a.key, "activity") }, "Open")] : null),
      h("span", { class: "feed-when" }, ago(a.at, S.data.now))));
    el.replaceChildren(
      h("div", { class: "home-hello" }, h("div", {}, h("h1", {}, greeting()),
        h("p", {}, nWait ? `${nWait} ${nWait === 1 ? "thing is" : "things are"} waiting on you. Everything else is moving.` : "Nothing is waiting on you. Claude keeps the board current as it works.")),
        button("Open the board", { variant: "primary", iconName: "grid", onclick: () => ctx.go("#/workspace") })),
      h("div", { class: "home-cols" },
        h("div", {},
          h("section", { class: "home-sec", "aria-labelledby": "h-projects" }, h("div", { class: "home-sec-h" }, h("h2", { id: "h-projects" }, "Recent projects")),
            cards.length ? h("div", { class: "proj-cards" }, cards) : h("p", { class: "home-empty" }, "No projects yet. Point Claude at a folder in Claude Code and say what you are making.")),
          h("section", { class: "home-sec home-activity", "aria-labelledby": "h-activity" }, h("div", { class: "home-sec-h" }, h("h2", { id: "h-activity" }, "Recent activity")),
            feed.length ? h("div", { class: "feed" }, feed) : h("p", { class: "home-empty" }, "Nothing has happened yet."))),
        h("section", { class: "home-sec home-waiting", "aria-labelledby": "h-waiting" }, h("div", { class: "home-sec-h" }, h("h2", { id: "h-waiting" }, "Waiting on you"),
          waiting.length ? h("a", { class: "btn btn-tertiary btn-sm", href: "#/workspace/waiting" }, "See all") : null),
          waitEls.length ? h("div", { class: "pop-list", style: { width: "auto", maxHeight: "none" } }, waitEls) : h("p", { class: "home-empty" }, "Nothing. When Claude decides something alone or reports what you said, it waits here for a word from you."))));
  }
  const last = (p, all) => Math.max(0, ...all.filter((r) => r.project === p.id).map((r) => r.updated_at || 0));
  return { el, render };
}
