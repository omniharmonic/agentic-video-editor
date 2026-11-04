// Main table: groups with a coloured edge, rows with solid stage cells, parts folded under their artifact. Every text cell edits in
// place (Enter saves, Esc cancels), every status cell opens its choices, rows and groups drag to reorder, columns resize and reorder.
// Built for 500 rows: one delegated listener, rows rebuilt only when they change.

import { h, button, icon, toast } from "../dom.js";
import { trackDrag, STAGES, stageClass, stageVar, avatar, isClaude, fmtDate, isLate, menu, stagePopover, peoplePopover, datePopover, waitingPopover,
  waitingCount, presentOn } from "./common.js";

export const COLUMNS = [
  { id: "stage", label: "Stage", w: 120, min: 96 },
  { id: "who", label: "Who", w: 56, min: 56 },
  { id: "waiting", label: "Waiting on", w: 136, min: 112 },
  { id: "claude", label: "Claude is", w: 168, min: 120 },
  { id: "due", label: "Due", w: 88, min: 80 },
  { id: "version", label: "Latest version", w: 160, min: 120 },
];
const NAME_W = 372, NAME_MIN = 200, CHECK_W = 46;

export function createBoard(ctx) {
  const { S, rows, act } = ctx;
  const el = h("div", { class: "ws-board" });
  let sig = "";
  let rowEls = new Map();
  let focusKey = null, focusCol = "name";
  let addTarget = null;

  // ---------------------------------------------------------------- layout: groups, order, parts
  const cols = () => S.colOrder.filter((c) => !S.hidden.has(c)).map((id) => COLUMNS.find((x) => x.id === id));
  const width = (id) => S.widths[id] || (id === "name" ? NAME_W : COLUMNS.find((c) => c.id === id).w);
  const template = () => `${CHECK_W}px ${width("name")}px ${cols().map((c) => `${width(c.id)}px`).join(" ")} minmax(0, 1fr)`;
  const eff = (r) => r.order ?? r.created_at ?? 0;
  const byName = (a, b) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" });
  function comparator() {
    switch (S.sort) {
      case "name": return byName;
      case "stage": return (a, b) => STAGES.indexOf(a.stage) - STAGES.indexOf(b.stage) || byName(a, b);
      case "due": return (a, b) => (a.due || "9999").localeCompare(b.due || "9999") || byName(a, b);
      case "updated": return (a, b) => (b.updated_at || 0) - (a.updated_at || 0);
      default: return (a, b) => eff(a) - eff(b) || byName(a, b);
    }
  }
  const projIndex = () => new Map([...ctx.projects.keys()].map((k, i) => [k, i]));
  function groupOf(r, pi) {
    if (S.group === "stage") return { key: `stage:${r.stage}`, title: r.stage, colour: stageVar(r.stage), rank: STAGES.indexOf(r.stage) };
    if (S.group === "who") {
      const n = r.owner || "";
      return { key: `who:${n}`, title: n ? (isClaude(n) ? "Claude" : n) : "Nobody", colour: isClaude(n) ? "var(--claude)" : n ? "var(--group-6)" : "var(--stage-plan)",
        rank: n ? (isClaude(n) ? 1 : 0) : 2, sub: n };
    }
    if (S.group === "waiting") {
      const w = r.waiting || "none";
      return { key: `waiting:${w}`, title: { you: "Waiting on you", claude: "Waiting on Claude", none: "Nothing waiting" }[w],
        colour: { you: "var(--waiting)", claude: "var(--claude)", none: "var(--stage-plan)" }[w], rank: { you: 0, claude: 1, none: 2 }[w] };
    }
    if (S.group === "campaign" && ctx.campaigns) return ctx.campaigns.groupOf(r);
    const p = ctx.projects.get(r.project);
    return { key: r.project, title: p?.title || "Project", colour: ctx.colourOf(r.project), rank: pi.get(r.project) ?? 99, project: r.project };
  }
  function layout() {
    const vis = ctx.visible();
    const visKeys = new Set(vis.map((r) => r.key));
    const cmp = comparator();
    const kids = new Map();
    const top = [];
    for (const r of vis) {
      if (r.parent && visKeys.has(r.parent)) (kids.get(r.parent) || kids.set(r.parent, []).get(r.parent)).push(r);
      else top.push(r);
    }
    for (const list of kids.values()) list.sort(cmp);
    top.sort(cmp);
    const pi = projIndex();
    const groups = new Map();
    // in project grouping, every project in scope shows, even an empty one, so there is always somewhere to add
    if (S.group === "project" && !S.q && !anyFilter()) {
      for (const [pid, p] of ctx.projects) if (!S.route.project || S.route.project === pid)
        groups.set(pid, { key: pid, title: p.title, colour: p.colour, rank: pi.get(pid), project: pid, items: [] });
    }
    // in campaign grouping, every campaign shows, even one with nothing on the board yet, so its menu is always there
    if (S.group === "campaign" && ctx.campaigns && !S.q && !anyFilter()) for (const g of ctx.campaigns.allGroups()) groups.set(g.key, { ...g, items: [] });
    for (const r of top) {
      const g = groupOf(r, pi);
      if (!groups.has(g.key)) groups.set(g.key, { ...g, items: [] });
      const list = groups.get(g.key).items;
      list.push({ r, depth: 0, n: (kids.get(r.key) || []).length });
      const open = S.expanded.has(r.key) || (S.q && kids.has(r.key));
      if (open) (kids.get(r.key) || []).forEach((k, i, all) => list.push({ r: k, depth: 1, n: 0, last: i === all.length - 1 }));
    }
    const saved = S.groupOrder[S.group] || [];
    const out = [...groups.values()].sort((a, b) => {
      const ia = saved.indexOf(a.key), ib = saved.indexOf(b.key);
      if (ia >= 0 && ib >= 0) return ia - ib;
      if (ia >= 0 || ib >= 0) return ia >= 0 ? -1 : 1;
      return a.rank - b.rank || String(a.title).localeCompare(String(b.title));
    });
    return { groups: out, kids };
  }
  const anyFilter = () => S.stages.size || S.waitingOnly || S.reviewOnly || S.person !== undefined || S.route.waiting;
  const sigOf = (L) => JSON.stringify([template(), S.colOrder, [...S.hidden], S.group, [...S.collapsed],
    L.groups.map((g) => [g.key, g.title, g.items.map((x) => x.r.key + (x.n ? `+${x.n}${S.expanded.has(x.r.key) ? "o" : ""}` : ""))])]);

  // ---------------------------------------------------------------- cells
  const cell = (col, cls, kids, extra = {}) => h("div", { class: `ws-cell ${cls}`, role: "gridcell", "data-col": col, tabindex: "-1", ...extra }, kids);
  function nameCell(r, depth, n) {
    const open = S.expanded.has(r.key) || (!!S.q && n > 0);
    const kids = [];
    if (depth) kids.push(h("span", { class: "ws-connector", "aria-hidden": "true" }));
    if (n) kids.push(h("button", { type: "button", class: "ws-expand", "data-do": "expand", "aria-expanded": String(open),
      "aria-label": `${open ? "Hide" : "Show"} ${n} ${n === 1 ? "part" : "parts"} of ${r.name}` }, icon("right")));
    else if (!depth) kids.push(h("span", { class: "ws-indent" }));
    kids.push(h("span", { class: "ws-name-text", title: r.name }, r.name));
    if (n) kids.push(h("span", { class: "chip chip-count", title: `${n} ${n === 1 ? "part" : "parts"}` }, String(n)));
    if (r.needs_review) kids.push(h("span", { class: "flag-review tip", "data-tip": r.review_reason || "Its artifact was reopened" }, icon("flag"), "Needs review"));
    const here = presentOn(r, ctx.now());
    if (here.length) {
      const all = here.map((p) => p.text).join(", ");
      kids.push(h("span", { class: "here tip", "data-tip": all, role: "img", "aria-label": all },
        avatar(here[0].person, { live: true }), h("span", { class: "here-text" }, here[0].text),
        here.length > 1 ? h("span", { class: "here-more" }, `+${here.length - 1}`) : null));
    }
    const bubble = h("button", { type: "button", class: `bubble ${r.updates ? "has-updates" : ""}`, "data-do": "updates",
      "aria-label": r.updates ? `${r.updates} ${r.updates === 1 ? "update" : "updates"} on ${r.name}` : `Write an update on ${r.name}` },
      icon(r.updates ? "bubble" : "bubble-plus"), r.updates ? h("span", { class: "bubble-n" }, String(r.updates)) : null);
    kids.push(h("span", { class: "ws-row-acts" },
      h("button", { type: "button", class: "btn btn-tertiary btn-sm ws-open ws-hover-act", "data-do": "open", "aria-label": `Open ${r.name}` }, icon("open"), h("span", {}, "Open")),
      bubble));
    return cell("name", "c-name", kids, { "data-act": "name" });
  }
  function colCell(c, r) {
    switch (c) {
      case "stage": return cell("stage", `c-solid c-act ${stageClass(r.stage)}`, r.stage, { "data-act": "stage", "aria-haspopup": "menu", "aria-label": `Stage: ${r.stage}` });
      case "who": return cell("who", "c-who c-act", avatar(r.owner), { "data-act": "who", "aria-haspopup": "menu", "aria-label": `Who: ${r.owner || "nobody"}` });
      case "waiting": {
        if (r.waiting === "you") {
          const n = waitingCount(r);
          return cell("waiting", "c-solid c-act st-waiting", ["Waiting on you", n > 1 ? h("span", { class: "wait-n" }, String(n)) : null],
            { "data-act": "waiting", "aria-haspopup": "dialog" });
        }
        if (r.waiting === "claude") return cell("waiting", "c-claude-waiting", [avatar("Claude"), "Claude"]);
        return cell("waiting", "", null);
      }
      case "claude": return r.claude_is
        ? cell("claude", `c-claude ${r.claude_state === "blocked" ? "is-blocked" : ""}`, [h("span", { class: "claude-dot" }), h("span", { title: r.claude_is }, r.claude_is)])
        : cell("claude", "", null);
      case "due": {
        const late = isLate(r);
        return cell("due", `c-due c-act ${late ? "is-late" : ""}`, r.due ? [late ? icon("info") : null, fmtDate(r.due)] : null,
          { "data-act": "due", "aria-haspopup": "dialog", "aria-label": r.due ? `Due ${fmtDate(r.due)}${late ? ", late" : ""}` : "No due date" });
      }
      case "version": return r.version
        ? cell("version", `c-version c-act ${r.ready ? "is-" + r.ready : ""}`, [
          r.ready ? h("span", { class: "ready-mark tip", "data-tip": r.ready_text, "aria-label": r.ready_text }, icon(r.ready === "arriving" ? "clock" : "alert")) : null,
          h("span", { title: `${r.version} (${r.versions} ${r.versions === 1 ? "version" : "versions"})` }, r.version)], { "data-act": "version" })
        : cell("version", "", null);
    }
    return cell(c, "", null);
  }
  // A project a newer AVE saved is read-only here (the notice says why): its rows open and read, but nothing edits them.
  const readOnly = (r) => !!ctx.projectOf(r)?.read_only;
  function rowEl(x) {
    const { r, depth, n } = x;
    const sel = S.selected.has(r.key);
    return h("div", { class: `ws-row ${depth ? "is-part" : ""} ${x.last ? "is-last-part" : ""} ${readOnly(r) ? "is-readonly" : ""}`, role: "row", "data-key": r.key, "data-parent": depth ? r.parent : null, "aria-selected": String(sel),
      "aria-readonly": readOnly(r) ? "true" : null, title: readOnly(r) ? ctx.projectOf(r).update_needed : null },
      h("div", { class: "ws-cell c-check", role: "gridcell", "data-col": "check" },
        h("button", { type: "button", class: "row-handle", "data-do": "handle", "aria-haspopup": "menu", "aria-label": `Actions for ${r.name}` }, icon("dots")),
        h("button", { type: "button", class: "check", role: "checkbox", "data-do": "select", "aria-checked": String(sel), "aria-label": `Select ${r.name}` }, icon("check"))),
      nameCell(r, depth, n),
      cols().map((c) => colCell(c.id, r)),
      h("div", { class: "ws-cell c-fill", role: "presentation" }));
  }

  // ---------------------------------------------------------------- group parts
  function headRow(g) {
    const keys = g.items.map((x) => x.r.key);
    const on = keys.filter((k) => S.selected.has(k)).length;
    const all = h("button", { type: "button", class: "check", role: "checkbox", "data-do": "select-all", "aria-checked": on === 0 ? "false" : on === keys.length ? "true" : "mixed",
      "aria-label": `Select every artifact in ${g.title}` }, icon("check"));
    const head = (id, label) => h("div", { class: `ws-cell ${id === "name" ? "c-name" : "c-head"}`, role: "columnheader", "data-head": id },
      h("span", {}, label), h("span", { class: "col-resize", "data-resize": id, "aria-hidden": "true" }));
    return h("div", { class: "ws-row ws-hrow", role: "row" }, h("div", { class: "ws-cell c-check", role: "columnheader" }, all),
      head("name", "Artifact"), cols().map((c) => head(c.id, c.label)), h("div", { class: "ws-cell c-fill", role: "presentation" }));
  }
  function projectFor(g) { return g.project || S.route.project || ctx.projects.keys().next().value; }
  function addRow(g) {
    const pid = projectFor(g);
    if (ctx.projects.get(pid)?.read_only) return null;
    const ptitle = ctx.projects.get(pid)?.title || "the project";
    const inp = h("input", { class: "ws-add-input", "data-project": pid, placeholder: g.project ? "+ Add artifact" : `+ Add artifact to ${ptitle}`,
      "aria-label": `Add an artifact to ${ptitle}`, autocomplete: "off" });
    return h("div", { class: "ws-row ws-addrow", role: "row" }, h("div", { class: "ws-cell c-check", role: "presentation" }),
      h("div", { class: "ws-cell c-addname", role: "gridcell" }, inp), h("div", { class: "ws-cell c-addrest", role: "presentation" }));
  }
  function segBar(list, thin = false) {
    const n = {};
    for (const r of list) n[r.stage] = (n[r.stage] || 0) + 1;
    return h("div", { class: `seg-bar ${thin ? "seg-bar-thin" : ""}`, role: "img", "aria-label": STAGES.filter((s) => n[s]).map((s) => `${s}: ${n[s]}`).join(", ") },
      STAGES.filter((s) => n[s]).map((s) => h("span", { title: `${s}: ${n[s]}`, style: { "--n": String(n[s]), "--seg": stageVar(s) } })));
  }
  function sumRow(g) {
    const list = g.items.map((x) => x.r);
    const cells = cols().map((c, i) => {
      let kid = null;
      if (c.id === "stage") kid = segBar(list);
      else if (c.id === "who") { const who = [...new Set(list.map((r) => r.owner).filter(Boolean))]; kid = h("span", { class: "avatars" }, who.slice(0, 4).map((n) => avatar(n))); }
      else if (c.id === "waiting") { const n = list.filter((r) => r.waiting === "you").length; kid = n ? `${n} waiting on you` : ""; }
      else if (c.id === "claude") { const n = list.filter((r) => r.claude_is).length; kid = n ? `Claude is on ${n}` : ""; }
      else if (c.id === "due") {
        const d = list.map((r) => r.due).filter(Boolean).sort();
        const a = fmtDate(d[0]), b = fmtDate(d.at(-1));
        kid = !d.length ? "" : a === b ? a : a.split(" ")[0] === b.split(" ")[0] && !a.includes(",") ? `${a}–${b.split(" ")[1]}` : `${a}–${b}`;
      }
      else if (c.id === "version") { const n = list.reduce((a, r) => a + (r.versions || 0), 0); kid = n ? `${n} ${n === 1 ? "version" : "versions"}` : ""; }
      return h("div", { class: `ws-cell c-sum ${i === 0 ? "is-first" : ""}`, role: "gridcell" }, kid);
    });
    return h("div", { class: "ws-row ws-sum", role: "row", "aria-label": `Summary of ${g.title}` }, h("div", { class: "ws-cell" }), h("div", { class: "ws-cell" }), cells, h("div", { class: "ws-cell" }));
  }
  function groupEl(g) {
    const ck = `${S.group}|${g.key}`;
    const collapsed = S.collapsed.has(ck);
    const count = g.items.filter((x) => !x.depth).length;
    const nParts = g.items.filter((x) => !x.depth).reduce((n, x) => n + x.n, 0);
    const countText = `${count} ${count === 1 ? "artifact" : "artifacts"}${nParts ? ` \u00b7 ${nParts} ${nParts === 1 ? "part" : "parts"}` : ""}`;
    const sec = h("section", { class: `grp ${collapsed ? "is-collapsed" : ""}`, "data-group": g.key, style: { "--group": g.colour }, "aria-label": g.title });
    sec.append(
      h("header", { class: "grp-head" },
        h("button", { type: "button", class: "btn btn-tertiary btn-icon btn-sm grp-handle", "data-do": "group-handle", "aria-haspopup": "menu",
          "aria-label": `Group options for ${g.title}` }, icon("grip")),
        h("button", { type: "button", class: "grp-toggle", "data-do": "collapse", "aria-expanded": String(!collapsed) }, icon("chevron"), h("span", { class: "grp-title" }, g.title)),
        h("span", { class: "grp-count" }, countText),
        g.project && !S.route.project ? ctx.syncPill(g.project, "group", g.title) : null),
      h("div", { class: "grp-collapsed" }, h("span", {}, countText), segBar(g.items.map((x) => x.r)),
        h("span", {}, `${g.items.filter((x) => x.r.waiting === "you").length} waiting on you`)));
    const body = h("div", { class: "ws-rows", role: "rowgroup" });
    for (const x of g.items) { const e = rowEl(x); rowEls.set(x.r.key, e); body.append(e); }
    // a campaign gathers posts, so a new artifact cannot be added into one: its menu adds posts instead
    sec.append(h("div", { class: "grp-table", role: "grid", "aria-label": g.title }, headRow(g), body, S.group === "campaign" ? (g.items.length ? null
      : h("div", { class: "ws-row ws-addrow", role: "row" }, h("div", { class: "ws-cell c-check", role: "presentation" }),
        h("div", { class: "ws-cell c-addname c-addhint secondary-text", role: "gridcell" }, g.campaign ? "Nothing on the board is in this campaign yet. Its menu adds posts." : "Every artifact with a post is in a campaign."))) : addRow(g),
      S.group === "campaign" && !g.items.length ? null : sumRow(g)));
    return sec;
  }

  // ---------------------------------------------------------------- render and update
  function emptyState() {
    if (S.q) return h("div", { class: "ws-noresult" }, h("h2", {}, `No artifacts match “${S.q}”`),
      h("p", {}, "Search looks at names, stages, versions, people and what Claude is doing."),
      button("Clear the search", { variant: "secondary", onclick: () => { const s = document.querySelector("input[aria-label='Search artifacts']"); if (s) s.value = ""; S.q = ""; ctx.rerender(); } }));
    if (anyFilter()) return h("div", { class: "ws-noresult" }, h("h2", {}, S.route.waiting ? "Nothing is waiting on you" : "Nothing matches these filters"),
      h("p", {}, S.route.waiting ? "Claude has not asked you anything it is waiting for. When it does, it shows up here." : "Change the filters to see more."),
      S.route.waiting ? button("See every artifact", { variant: "secondary", onclick: () => ctx.go("#/workspace") })
        : button("Clear the filters", { variant: "secondary", onclick: () => { S.stages.clear(); S.waitingOnly = S.reviewOnly = false; S.person = undefined; ctx.rerender(); } }));
    return null;
  }
  function render() {
    const L = layout();
    sig = sigOf(L);
    rowEls = new Map();
    el.style.setProperty("--cols", template());
    const empty = !L.groups.length || L.groups.every((g) => !g.items.length) && (S.q || anyFilter());
    const kids = empty ? [emptyState() || boardEmpty()] : [...L.groups.map(groupEl), ...ctx.missingGroups()];
    if (!empty && L.groups.every((g) => !g.items.length)) kids.unshift(boardEmpty(true));
    const typing = [...el.querySelectorAll(".ws-add-input")].filter((i) => i.value || document.activeElement === i)      // a redraw must not
      .map((i) => ({ pid: i.dataset.project, value: i.value, parent: i.dataset.parent, focused: document.activeElement === i,  // take a name
        from: i.selectionStart, to: i.selectionEnd, adding: i.closest(".ws-addrow")?.classList.contains("is-adding"), ph: i.placeholder })); // being typed
    el.replaceChildren(...kids);
    for (const k of typing) {
      const inp = el.querySelector(`.ws-add-input[data-project="${CSS.escape(k.pid || "")}"]`);
      if (!inp) continue;
      inp.value = k.value;
      inp.dataset.parent = k.parent || "";
      if (k.adding) { inp.closest(".ws-addrow")?.classList.add("is-adding"); inp.placeholder = k.ph; }
      if (k.focused && !addTarget) { inp.focus(); inp.setSelectionRange(k.from, k.to); }
    }
    if (addTarget) { const t = addTarget; addTarget = null; focusAdd(t); }
    else if (!typing.some((k) => k.focused)) restoreFocus(false);
  }
  function boardEmpty(inline) {
    return h("div", { class: "ws-empty", style: inline ? { margin: "32px auto 0" } : null },
      h("h2", {}, "Nothing on the board yet"),
      h("p", { class: "secondary-text" }, "Ask Claude in Claude Code what you are making, and it adds each artifact here as it starts. Or add one yourself."),
      h("p", { class: "say" }, "Plan a short from the best moment in episode 12"),
      button("New artifact", { variant: "primary", iconName: "plus", onclick: () => ctx.startAdding() }));
  }
  let dragging = false, pending = false;
  function update(keys, flashes) {
    if (!el.isConnected) return false;
    if (dragging) { pending = true; return true; }              // wait for the drop; never move rows under the pointer
    const L = layout();
    if (sigOf(L) !== sig) { render(); applyFlashes(flashes); return true; }
    const items = new Map(L.groups.flatMap((g) => g.items.map((x) => [x.r.key, x])));
    const hadFocus = el.contains(document.activeElement) && document.activeElement.closest(".ws-row[data-key]");
    for (const k of new Set(keys)) {
      const old = rowEls.get(k), x = items.get(k);
      if (!old || !x || old.querySelector(".ws-edit")) continue;          // never pull a field out from under the person typing
      const nu = rowEl(x);
      old.replaceWith(nu);
      rowEls.set(k, nu);
    }
    for (const g of L.groups) {
      const sec = el.querySelector(`.grp[data-group="${CSS.escape(g.key)}"]`);
      sec?.querySelector(".ws-sum")?.replaceWith(sumRow(g));
      sec?.querySelector(".ws-hrow")?.replaceWith(headRow(g));
    }
    if (hadFocus) restoreFocus(true);
    applyFlashes(flashes);
    return true;
  }
  function applyFlashes(flashes) {
    for (const [k, f] of flashes || []) {
      const r = rowEls.get(k);
      if (!r) continue;
      for (const c of f.cols) {
        const ce = r.querySelector(`[data-col="${c}"]`);
        if (!ce) continue;
        ce.classList.add("is-changed");
        const a = avatar(f.by, { cls: "changed-by" });
        a.setAttribute("aria-label", `Changed by ${isClaude(f.by) ? "Claude" : f.by}`);
        ce.append(a);
        setTimeout(() => { ce.classList.remove("is-changed"); a.remove(); }, 2400);
      }
    }
  }
  function paintSelection() {
    for (const [k, e] of rowEls) {
      const on = S.selected.has(k);
      e.setAttribute("aria-selected", String(on));
      e.querySelector(".check")?.setAttribute("aria-checked", String(on));
    }
    for (const sec of el.querySelectorAll(".grp")) {
      const keys = [...sec.querySelectorAll(".ws-rows .ws-row[data-key]")].map((r) => r.dataset.key);
      const n = keys.filter((k) => S.selected.has(k)).length;
      sec.querySelector(".ws-hrow .check")?.setAttribute("aria-checked", n === 0 ? "false" : n === keys.length ? "true" : "mixed");
    }
  }

  // ---------------------------------------------------------------- focus: one cell is in the Tab order, arrows move it
  function focusCell(c) {
    if (!c) return;
    el.querySelectorAll('.ws-cell[tabindex="0"]').forEach((x) => { x.tabIndex = -1; });
    c.tabIndex = 0;
    focusKey = c.closest(".ws-row")?.dataset.key || null;
    focusCol = c.dataset.col;
    c.focus({ preventScroll: false });
  }
  function restoreFocus(take) {
    const r = focusKey && rowEls.get(focusKey);
    const c = r?.querySelector(`[data-col="${focusCol}"]`) || el.querySelector(".ws-rows .ws-cell.c-name");
    if (!c) return;
    el.querySelectorAll('.ws-cell[tabindex="0"]').forEach((x) => { x.tabIndex = -1; });
    c.tabIndex = 0;
    if (take) c.focus({ preventScroll: true });
  }
  const navCells = (rowE) => [...rowE.querySelectorAll(".ws-cell[data-col]")];

  // ---------------------------------------------------------------- acting on a cell
  const rowOf = (e) => rows.get(e.closest(".ws-row[data-key]")?.dataset.key);
  function activate(c, how = "click") {
    const r = rowOf(c);
    if (!r) return;
    const col = c.dataset.col;
    if (col === "check") { ctx.select(r.key, !S.selected.has(r.key)); return; }
    if (!c.dataset.act || readOnly(r)) return;
    if (col === "name") return editName(c, r);
    if (col === "stage") return stagePopover(c, r.stage, (s) => act.stage([r], s), { outReason: S.data?.out_refused });
    if (col === "who") return peoplePopover(c, ctx.projectOf(r)?.members, r.owner, (n) => act.fields([r], { owner: n }, { say: `Gave ${r.name} to ${n || "nobody"}` }));
    if (col === "due") return datePopover(c, r.due, (d) => act.fields([r], { due: d }, { say: d ? `${r.name} is due ${fmtDate(d)}` : "Due date cleared" }));
    if (col === "waiting") return waitingPopover(c, r, act);
    if (col === "version") return ctx.openPanel(r.key, "versions");
    void how;
  }
  function editName(c, r) {
    const text = c.querySelector(".ws-name-text");
    if (!text) return;
    const inp = h("input", { class: "input ws-edit", value: r.name, "aria-label": `Name of ${r.name}`, maxlength: "200" });
    text.replaceWith(inp);
    inp.focus(); inp.select();
    let done = false;
    const finish = (save) => {
      if (done) return;
      done = true;
      const v = inp.value.trim();
      inp.replaceWith(text);
      if (save && v && v !== r.name) { text.textContent = v; act.fields([r], { title: v }, { say: `Renamed to ${v}` }); }
      focusCell(c);
    };
    inp.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); finish(true); }
      else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); finish(false); }
      e.stopPropagation();
    });
    inp.addEventListener("blur", () => finish(true));
    inp.addEventListener("click", (e) => e.stopPropagation());
  }
  function rowMenu(anchor, r) {
    menu(anchor, [
      { label: "Open", icon: "open", hint: "O", onSelect: () => ctx.openPanel(r.key) },
      { label: "Open in the Room", icon: "film", onSelect: () => ctx.openRoom(r) },
      ...(r.parts?.length ? [{ label: S.expanded.has(r.key) ? "Hide its parts" : "Show its parts", icon: "right", hint: "Alt \u2192", onSelect: () => expandKey(r, !S.expanded.has(r.key)) }] : []),
      { label: "Add a part", icon: "plus", onSelect: () => startAdding({ parent: r.key }) },
      { label: S.selected.has(r.key) ? "Deselect" : "Select", icon: "check", hint: "X", onSelect: () => ctx.select(r.key, !S.selected.has(r.key)) },
      "-",
      { label: "Archive", icon: "archive", onSelect: () => act.archive([r]) },
    ], { label: `Actions for ${r.name}` });
  }
  function startAdding({ parent } = {}) {
    const p = parent ? rows.get(parent) : null;
    const pid = p?.project || S.route.project || ctx.projects.keys().next().value;
    addTarget = { pid, parent: parent || null, name: p?.name };
    if (S.group === "project" && S.collapsed.delete(`${S.group}|${pid}`)) ctx.savePrefs();
    if (!el.isConnected || el.querySelector(".ws-empty") || !focusAdd(addTarget)) ctx.rerender();
    else addTarget = null;
  }
  function focusAdd(t) {
    const inp = el.querySelector(`.ws-add-input[data-project="${CSS.escape(t.pid)}"]`) || el.querySelector(".ws-add-input");
    if (!inp) return false;
    inp.dataset.parent = t.parent || "";
    inp.placeholder = t.parent ? `Name the new part of ${t.name}` : "Name the new artifact, then Enter";
    inp.closest(".ws-addrow").classList.add("is-adding");
    inp.scrollIntoView({ block: "nearest" });
    inp.focus();
    return true;
  }

  // ---------------------------------------------------------------- one listener for every row
  el.addEventListener("click", (e) => {
    const b = e.target.closest("[data-do]");
    if (b && el.contains(b)) {
      const r = rowOf(b);
      const sec = b.closest(".grp");
      switch (b.dataset.do) {
        case "select": ctx.select(r.key, !S.selected.has(r.key)); return;
        case "select-all": {
          const keys = [...sec.querySelectorAll(".ws-rows .ws-row[data-key]")].map((x) => x.dataset.key);
          ctx.selectMany(keys, !keys.every((k) => S.selected.has(k))); return;
        }
        case "expand": S.expanded.has(r.key) ? S.expanded.delete(r.key) : S.expanded.add(r.key); focusKey = r.key; focusCol = "name"; render(); restoreFocus(true); return;
        case "open": ctx.openPanel(r.key); return;
        case "updates": ctx.openPanel(r.key, "updates"); return;
        case "room": ctx.openRoom(r); return;
        case "handle": if (!b.dataset.dragged && !readOnly(r)) rowMenu(b, r); delete b.dataset.dragged; return;
        case "group-handle": if (!b.dataset.dragged) groupMenu(b, sec); delete b.dataset.dragged; return;
        case "collapse": {
          const ck = `${S.group}|${sec.dataset.group}`;
          S.collapsed.has(ck) ? S.collapsed.delete(ck) : S.collapsed.add(ck);
          ctx.savePrefs(); render();
          el.querySelector(`.grp[data-group="${CSS.escape(sec.dataset.group)}"] .grp-toggle`)?.focus();
          return;
        }
      }
      return;
    }
    const c = e.target.closest(".ws-cell[data-col]");
    if (c && el.contains(c) && c.closest(".ws-rows") && !e.target.closest(".ws-edit, .flag-review")) { focusCell(c); activate(c); }
  });
  el.addEventListener("keydown", (e) => {
    if (e.target.classList?.contains("ws-add-input")) return addKey(e);
    if (e.target.dataset?.do === "group-handle" && (e.key === "ArrowUp" || e.key === "ArrowDown")) { e.preventDefault(); moveGroup(e.target.closest(".grp"), e.key === "ArrowUp" ? -1 : 1); return; }
    const c = e.target;
    const inRow = c.closest?.(".ws-rows .ws-row[data-key]");
    if (inRow && (e.key === "ContextMenu" || (e.key === "F10" && e.shiftKey))) { e.preventDefault(); rowMenu(inRow.querySelector(".row-handle"), rowOf(inRow)); return; }
    if (inRow && e.altKey && (e.key === "ArrowRight" || e.key === "ArrowLeft")) { e.preventDefault(); expandKey(rowOf(inRow), e.key === "ArrowRight"); return; }
    if (!c.classList?.contains("ws-cell") || !c.closest(".ws-rows")) return;
    const rowE = c.closest(".ws-row");
    const cells = navCells(rowE);
    const i = cells.indexOf(c);
    const r = rowOf(c);
    if (e.key === "ArrowRight") { e.preventDefault(); focusCell(cells[Math.min(cells.length - 1, i + 1)]); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); focusCell(cells[Math.max(0, i - 1)]); }
    else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const all = [...el.querySelectorAll(".ws-rows .ws-row[data-key]")];
      const j = all.indexOf(rowE) + (e.key === "ArrowDown" ? 1 : -1);
      if (all[j]) focusCell(all[j].querySelector(`[data-col="${c.dataset.col}"]`));
    }
    else if (e.key === "Home") { e.preventDefault(); focusCell(cells[0]); }
    else if (e.key === "End") { e.preventDefault(); focusCell(cells.at(-1)); }
    else if (e.key === "Enter") { e.preventDefault(); activate(c, "key"); }
    else if (e.key === " " || e.key === "x") { e.preventDefault(); ctx.select(r.key, !S.selected.has(r.key)); }
    else if (e.key === "o") { e.preventDefault(); ctx.openPanel(r.key); }
  });
  // Alt+Right opens an artifact's parts, Alt+Left folds them (or, on a part, goes back to its artifact)
  function expandKey(r, open) {
    focusCol = "name";
    if (!open && r.parent && rows.has(r.parent) && !S.expanded.has(r.key)) { focusKey = r.parent; S.expanded.delete(r.parent); render(); restoreFocus(true); return; }
    if (!r.parts?.length) return;
    open ? S.expanded.add(r.key) : S.expanded.delete(r.key);
    focusKey = r.key; render(); restoreFocus(true);
  }
  function addKey(e) {
    const inp = e.target;
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); inp.value = ""; inp.blur(); inp.closest(".ws-addrow")?.classList.remove("is-adding"); return; }
    if (e.key !== "Enter") return;
    e.preventDefault();
    const v = inp.value.trim();
    if (!v) return;
    inp.value = "";
    act.create(inp.dataset.project, v, { parent: inp.dataset.parent || undefined }).then((row) => {
      if (!row) { inp.value = v; return; }
      const again = el.querySelector(`.ws-add-input[data-project="${CSS.escape(inp.dataset.project)}"]`);
      again?.focus();                                                // ready for the next one
    });
  }
  el.addEventListener("focusout", (e) => {
    if (e.target.classList?.contains("ws-add-input") && !e.target.value) { e.target.closest(".ws-addrow")?.classList.remove("is-adding"); e.target.dataset.parent = ""; }
  });

  // ---------------------------------------------------------------- dragging: rows, groups, columns
  el.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    const handle = e.target.closest(".row-handle");
    if (handle) return dragRow(e, handle);
    const gh = e.target.closest(".grp-handle");
    if (gh) return dragGroup(e, gh);
    const rs = e.target.closest(".col-resize");
    if (rs) return resizeCol(e, rs);
    const ch = e.target.closest(".ws-hrow .c-head");
    if (ch) return dragCol(e, ch);
  });
  const drag = trackDrag;
  function dragRow(e, handle) {
    const rowE = handle.closest(".ws-row");
    const r = rowOf(rowE);
    const home = rowE.parentElement;
    let body = home;
    const across = S.group === "stage" && !rowE.dataset.parent;
    const sibs = () => [...body.querySelectorAll(".ws-row[data-key]")].filter((x) => x !== rowE && (x.dataset.parent || "") === (body === home ? rowE.dataset.parent || "" : ""));
    let ghost, line, before = null;
    drag(e, {
      start: () => {
        dragging = true;
        handle.dataset.dragged = "1";
        const b = rowE.getBoundingClientRect();
        ghost = rowE.cloneNode(true);
        ghost.classList.add("ws-drag-ghost");
        Object.assign(ghost.style, { width: `${Math.min(b.width, 640)}px`, left: `${b.left}px`, top: `${b.top}px` });
        document.body.append(ghost);
        rowE.classList.add("is-dragging");
        body.style.position = "relative";
        line = h("div", { class: "ws-drop-line" });
        body.append(line);
      },
      move: (ev) => {
        ghost.style.top = `${ev.clientY - 18}px`;
        if (across) {
          const g = [...el.querySelectorAll(".grp:not(.is-collapsed)")].find((x) => { const b = x.getBoundingClientRect(); return ev.clientY >= b.top && ev.clientY < b.bottom; });
          const nb = g?.querySelector(".ws-rows");
          if (nb && nb !== body) { body.style.position = ""; body = nb; body.style.position = "relative"; body.append(line); }
          el.querySelectorAll(".grp.is-refused").forEach((x) => x.classList.remove("is-refused"));
          if (g?.dataset.group === "stage:Out" && r.stage !== "Out") g.classList.add("is-refused");
        }
        const list = sibs();
        before = list.find((x) => { const b = x.getBoundingClientRect(); return ev.clientY < b.top + b.height / 2; }) || null;
        const ref = before || list.at(-1);
        if (!ref) return;
        const top = before ? before.offsetTop : ref.offsetTop + ref.offsetHeight;
        Object.assign(line.style, { top: `${top}px`, width: `${body.scrollWidth}px` });
      },
      cleanup: () => {
        dragging = false;
        ghost.remove(); line.remove(); rowE.classList.remove("is-dragging"); body.style.position = "";
        el.querySelectorAll(".grp.is-refused").forEach((x) => x.classList.remove("is-refused"));
        setTimeout(() => { delete handle.dataset.dragged; }, 0);
        if (pending) { pending = false; setTimeout(() => update([...rows.keys()], new Map()), 0); }
      },
      drop: () => {
        if (body !== home) {
          const to = body.closest(".grp").dataset.group.replace(/^stage:/, "");
          if (to === "Out") { toast(S.data?.out_refused || "Out needs the publish approval.", { tone: "negative", timeout: 8000 }); return; }
          focusKey = r.key; focusCol = "name";
          act.stage([r], to);
          return;
        }
        const list = sibs();
        const at = before ? list.indexOf(before) : list.length;
        const prev = list[at - 1] ? rows.get(list[at - 1].dataset.key) : null;
        const next = list[at] ? rows.get(list[at].dataset.key) : null;
        const order = prev && next ? (eff(prev) + eff(next)) / 2 : prev ? eff(prev) + 1000 : next ? eff(next) - 1000 : eff(r);
        if (order === eff(r)) return;
        if (S.sort !== "manual") { S.sort = "manual"; ctx.savePrefs(); }
        focusKey = r.key; focusCol = "name";
        act.fields([r], { order }, { say: `Moved ${r.name}` });
      },
    });
  }
  // the group's own menu (like the reference's group options): fold it, fold every group, or move it
  function groupMenu(anchor, sec) {
    const ck = `${S.group}|${sec.dataset.group}`;
    const keys = [...el.querySelectorAll(".grp")].map((g) => g.dataset.group);
    const i = keys.indexOf(sec.dataset.group);
    const title = sec.getAttribute("aria-label");
    menu(anchor, [
      { label: S.collapsed.has(ck) ? "Expand this group" : "Collapse this group", icon: "chevron",
        onSelect: () => { S.collapsed.has(ck) ? S.collapsed.delete(ck) : S.collapsed.add(ck); ctx.savePrefs(); render(); } },
      { label: "Collapse all groups", icon: "chevron", onSelect: () => { for (const k of keys) S.collapsed.add(`${S.group}|${k}`); ctx.savePrefs(); render(); } },
      "-",
      { label: "Move up", icon: "sort", hint: "\u2191", disabled: i === 0, onSelect: () => moveGroup(sec, -1) },
      { label: "Move down", icon: "sort", hint: "\u2193", disabled: i === keys.length - 1, onSelect: () => moveGroup(sec, 1) },
      ...(S.group === "campaign" && ctx.campaigns ? ["-", ...ctx.campaigns.groupItems(anchor, sec.dataset.group)] : []),
    ], { label: `Group options for ${title}` });
  }
  function moveGroup(sec, d) {
    const keys = [...el.querySelectorAll(".grp")].map((g) => g.dataset.group);
    const i = keys.indexOf(sec.dataset.group);
    const j = Math.max(0, Math.min(keys.length - 1, i + d));
    if (i === j) return;
    keys.splice(j, 0, keys.splice(i, 1)[0]);
    S.groupOrder[S.group] = keys; ctx.savePrefs(); render();
    el.querySelector(`.grp[data-group="${CSS.escape(sec.dataset.group)}"] .grp-handle`)?.focus();
  }
  function dragGroup(e, gh) {
    const sec = gh.closest(".grp");
    let line, before = null;
    drag(e, {
      start: () => { gh.dataset.dragged = "1"; sec.classList.add("is-dragging"); line = h("div", { class: "ws-drop-line" }); el.style.position = "relative"; el.append(line); },
      move: (ev) => {
        const list = [...el.querySelectorAll(".grp")].filter((g) => g !== sec);
        before = list.find((g) => { const b = g.getBoundingClientRect(); return ev.clientY < b.top + Math.min(b.height / 2, 60); }) || null;
        const ref = before || list.at(-1);
        if (ref) Object.assign(line.style, { top: `${before ? ref.offsetTop - 12 : ref.offsetTop + ref.offsetHeight + 12}px`, width: "100%" });
      },
      cleanup: () => { sec.classList.remove("is-dragging"); line.remove(); setTimeout(() => { delete gh.dataset.dragged; }, 0); },
      drop: () => {
        const keys = [...el.querySelectorAll(".grp")].map((g) => g.dataset.group).filter((k) => k !== sec.dataset.group);
        const at = before ? keys.indexOf(before.dataset.group) : keys.length;
        keys.splice(at, 0, sec.dataset.group);
        S.groupOrder[S.group] = keys; ctx.savePrefs(); render();
      },
    });
  }
  function resizeCol(e, rs) {
    e.preventDefault(); e.stopPropagation();
    const id = rs.dataset.resize;
    const w0 = width(id);
    const min = id === "name" ? NAME_MIN : COLUMNS.find((c) => c.id === id).min;
    let saved = false;
    drag(e, {
      threshold: 0,
      start: () => rs.classList.add("is-active"),
      move: (ev) => { S.widths[id] = Math.max(min, Math.round(w0 + ev.clientX - e.clientX)); el.style.setProperty("--cols", template()); },
      cleanup: () => {
        rs.classList.remove("is-active");
        setTimeout(() => { if (!saved) { S.widths[id] = w0; el.style.setProperty("--cols", template()); } }, 0);   // abandoned: back as it was
      },
      drop: () => { saved = true; ctx.savePrefs(); sig = sigOf(layout()); },
    });
  }
  function dragCol(e, ch) {
    const id = ch.dataset.head;
    let target = null;
    const heads = () => [...ch.parentElement.querySelectorAll(".c-head")];
    drag(e, {
      move: (ev) => {
        heads().forEach((x) => x.classList.remove("is-drop"));
        target = heads().find((x) => { const b = x.getBoundingClientRect(); return ev.clientX >= b.left && ev.clientX < b.right; }) || null;
        if (target && target !== ch) target.classList.add("is-drop");
      },
      cleanup: () => heads().forEach((x) => x.classList.remove("is-drop")),
      drop: () => {
        if (!target || target === ch) return;
        const order = S.colOrder.filter((c) => c !== id);
        order.splice(order.indexOf(target.dataset.head), 0, id);
        S.colOrder = order; ctx.savePrefs(); render();
      },
    });
  }

  function skeleton() {
    const row = (head) => h("div", { class: "ws-row" }, h("div", { class: "ws-cell c-check" }),
      h("div", { class: "ws-cell c-name" }, h("span", { class: "skeleton skeleton-text", style: { width: head ? "30%" : "60%" } })),
      COLUMNS.map(() => h("div", { class: "ws-cell" }, h("span", { class: "skeleton skeleton-text" }))), h("div", { class: "ws-cell c-fill" }));
    const grp = (n, c) => h("section", { class: "grp", style: { "--group": c } },
      h("header", { class: "grp-head", style: { marginLeft: "0" } }, h("span", { class: "skeleton skeleton-title", style: { width: "180px" } })),
      h("div", { class: "grp-table" }, Array.from({ length: n }, (_, i) => row(i === 0))));
    const s = h("div", { class: "ws-board ws-skel", "aria-label": "Loading the board", role: "status" }, grp(6, "var(--skeleton)"), grp(4, "var(--skeleton)"));
    s.style.setProperty("--cols", `${CHECK_W}px ${NAME_W}px ${COLUMNS.map((c) => `${c.w}px`).join(" ")} minmax(0, 1fr)`);
    return s;
  }

  return { el, render, update, paintSelection, skeleton, startAdding, groupKeys: () => [...el.querySelectorAll(".grp")].map((g) => `${S.group}|${g.dataset.group}`) };
}
