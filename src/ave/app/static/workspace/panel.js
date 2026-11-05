// The item panel: an artifact opened from the board slides in on the right with its updates, versions, decisions and activity. The
// title edits in place, the stage pill opens the same stage choices as the board, and "Open in the Room" goes straight to the stage.

import { h, button, icon, toast, busy } from "../dom.js";
import { api, plain, stageClass, avatar, ago, stagePopover, peoplePopover, waitingContent, isClaude, lowerFirst } from "./common.js";

const TABS = [["updates", "Updates"], ["versions", "Versions"], ["decisions", "Decisions"], ["activity", "Activity"]];

export function createPanel(ctx) {
  const { S, rows, act } = ctx;
  const el = h("aside", { class: "side-panel ws-panel", "aria-label": "Artifact", hidden: true, tabindex: "-1" });
  let key = null, tab = "updates", detail = null, opener = null, loading = false, failed = null, draft = "";

  const crumb = h("p", { class: "ws-panel-crumb" });
  const closeBtn = button("Close", { variant: "tertiary", size: "sm", iconName: "x", iconOnly: true, onclick: () => close() });
  const titleEl = h("h2", { tabindex: "0", title: "Click to rename" });
  const meta = h("div", { class: "ws-panel-meta" });
  const tabs = h("div", { class: "tabs ws-panel-tabs", role: "tablist", "aria-label": "Artifact details" });
  const body = h("div", { class: "ws-panel-body", role: "tabpanel" });
  const roomBtn = button("Open in the Room", { variant: "primary", size: "sm", iconName: "film", onclick: () => row() && ctx.openRoom(row()) });
  el.append(h("div", { class: "ws-panel-head" }, h("div", { class: "ws-panel-top" }, crumb, h("div", { class: "ws-panel-tools" }, roomBtn, closeBtn)), titleEl, meta), tabs, body);

  titleEl.addEventListener("click", () => editTitle());
  titleEl.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); editTitle(); } });
  el.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !document.querySelector(".popover")) {
      const t = e.target;
      if (t.tagName === "TEXTAREA" && t.value.trim()) return;               // a draft is never closed away by accident
      if (t.classList?.contains("ws-title-edit")) return;
      e.preventDefault(); e.stopPropagation(); close();
    }
  });
  tabs.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    e.preventDefault();
    const i = TABS.findIndex(([k]) => k === tab) + (e.key === "ArrowRight" ? 1 : -1);
    setTab(TABS[(i + TABS.length) % TABS.length][0]);
    tabs.querySelector("[aria-selected=true]")?.focus();
  });

  const row = () => rows.get(key);
  async function fetchDetail() {
    if (!key) return;
    loading = !detail;
    paint();
    try { detail = await api("GET", `/api/workspace/artifact/${encodeURIComponent(key)}`); failed = null; }
    catch (e) { failed = e; }
    loading = false;
    paint();
  }
  function open(k, t = "updates") {
    if (!rows.has(k)) return;
    if (el.hidden) opener = document.activeElement;
    if (key !== k) { detail = null; draft = ""; }
    key = k; tab = t;
    el.hidden = false;
    paint();
    fetchDetail();
    (tab === "updates" ? body.querySelector("textarea") : titleEl)?.focus({ preventScroll: true });
  }
  function close(refocus = true) {
    if (el.hidden) return;
    el.hidden = true;
    key = null; detail = null;
    if (refocus && opener?.isConnected) opener.focus({ preventScroll: true });
  }
  function setTab(t) { tab = t; paint(); }

  function paintRow() {
    if (el.hidden || !key) return;
    if (!row()) { close(); return; }
    paintHead();
  }
  function paintHead() {
    const r = row();
    const p = ctx.projectOf(r);
    el.style.setProperty("--group", p?.colour || "var(--group-1)");
    el.setAttribute("aria-label", r.name);
    crumb.replaceChildren(...[h("span", { class: "dot" }), h("span", {}, p?.title || ""), r.parent ? h("span", {}, `/ part of ${rows.get(r.parent)?.name || "an artifact"}`) : null].filter(Boolean));
    if (!titleEl.querySelector("input")) titleEl.textContent = r.name;
    const stage = h("button", { type: "button", class: `pill ${stageClass(r.stage)}`, "aria-haspopup": "menu", "aria-label": `Stage: ${r.stage}`,
      onclick: (e) => stagePopover(e.currentTarget, r.stage, (s) => act.stage([r], s), { outReason: S.data?.out_refused }) }, r.stage);
    const who = h("button", { type: "button", class: "btn btn-tertiary btn-sm", "aria-haspopup": "menu", "aria-label": `Who: ${r.owner || "nobody"}`,
      onclick: (e) => peoplePopover(e.currentTarget, p?.members, r.owner, (n) => act.fields([r], { owner: n }, { say: `Gave ${r.name} to ${n || "nobody"}` })) },
      avatar(r.owner), h("span", {}, r.owner ? (isClaude(r.owner) ? "Claude" : r.owner) : "Nobody"));
    const campaign = ctx.campaigns?.panelControl(r);
    meta.replaceChildren(...[stage, who, campaign, r.claude_is ? h("span", { class: "chip chip-claude" }, `Claude is ${lowerFirst(r.claude_is)}`) : null].filter(Boolean));
    const count = { updates: detail?.updates.length ?? r.updates, versions: detail?.versions.length ?? r.versions,
      decisions: (r.decisions?.length || 0) + (r.relayed?.length || 0) + (detail?.decisions_resolved.length || 0), activity: null };
    tabs.replaceChildren(...TABS.map(([k, l]) => h("button", { type: "button", class: "tab", role: "tab", "aria-selected": String(tab === k), tabindex: tab === k ? "0" : "-1",
      onclick: () => setTab(k) }, l, count[k] ? h("span", { class: "chip chip-count" }, String(count[k])) : null)));
  }
  function paint() {
    if (el.hidden || !key || !row()) return;
    paintHead();
    const r = row();
    const kids = [];
    if (r.needs_review) kids.push(h("p", { class: "flag-review" }, icon("flag"), `Needs review: ${r.review_reason}. Claude proposes what to redo.`));
    if (failed && !detail) kids.push(h("div", { class: "panel-empty" }, h("p", {}, plain(failed, "open it")), button("Try again", { variant: "secondary", onclick: (e) => busy(e.currentTarget, fetchDetail) })));
    else if (loading || !detail) kids.push(...Array.from({ length: 3 }, () => h("div", { class: "post" }, h("span", { class: "skeleton skeleton-text", style: { width: "40%" } }),
      h("span", { class: "skeleton skeleton-text", style: { width: "90%" } }))));
    else kids.push(...({ updates, versions, decisions, activity }[tab])(r, detail));
    const keepDraft = body.querySelector("textarea")?.value;
    if (keepDraft != null) draft = keepDraft;
    const focusedTA = document.activeElement?.tagName === "TEXTAREA" && body.contains(document.activeElement);
    body.replaceChildren(...kids);
    if (focusedTA) body.querySelector("textarea")?.focus();
  }

  function updates(r, d) {
    const ta = h("textarea", { class: "input", rows: "3", placeholder: "Write an update for the team, or a note for Claude to read", "aria-label": "Write an update" });
    ta.value = draft;
    const post = button("Post update", { variant: "primary", size: "sm", onclick: (e) => send(e.currentTarget) });
    async function send(btn) {
      const text = ta.value.trim();
      if (!text) { ta.setAttribute("aria-invalid", "true"); ta.focus(); return; }
      if (await act.note(r, text, btn)) { draft = ""; ta.value = ""; toast("Update posted"); fetchDetail(); }
    }
    ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); send(post); } });
    ta.addEventListener("input", () => { ta.removeAttribute("aria-invalid"); draft = ta.value; });
    return [
      h("div", { class: "composer" }, ta, h("div", { class: "composer-foot" }, h("span", { class: "secondary-text" }, "⌘ Enter posts it"), post)),
      ...(d.updates.length ? d.updates.map((u) => h("article", { class: "post" },
        h("p", { class: "post-head" }, avatar(u.who), h("strong", {}, u.who), h("span", {}, ago(u.at))), h("p", { class: "post-text" }, u.text)))
        : [h("p", { class: "panel-empty" }, "No updates yet. Anything written here stays with the artifact, on every computer on the project.")]),
    ];
  }
  function versions(r, d) {
    if (!d.versions.length) return [h("p", { class: "panel-empty" }, "No versions yet. Each cut or draft Claude makes shows up here.")];
    return [h("div", { class: "panel-list" }, d.versions.map((v, i) => h("div", { class: "panel-row" }, avatar(v.by || "Someone"),
      h("div", { class: "grow" }, h("strong", {}, v.name), h("span", { class: "secondary-text" }, `${v.by || "Someone"}, ${ago(v.at)}`)),
      v.ready ? h("span", { class: `chip chip-ready is-${v.ready.status}`, title: v.ready.text }, { arriving: "Arriving", stuck: "Stuck", damaged: "Damaged" }[v.ready.status] || "Arriving") : null,
      i === 0 ? h("span", { class: "chip" }, "Latest") : null, v.kept ? h("span", { class: "chip chip-claude" }, "Kept") : null))),
      h("div", { class: "popover-actions" }, button("Watch it in the Room", { variant: "secondary", size: "sm", iconName: "film", onclick: () => ctx.openRoom(r) }))];
  }
  function decisions(r, d) {
    const out = [];
    const open = waitingContent(r, act, { onClose: () => fetchDetail() });
    if (open.childElementCount) out.push(h("p", { class: "panel-h" }, "Waiting on you"), h("div", { class: "panel-card is-open" }, open));
    if (d.decisions_resolved.length) {
      out.push(h("p", { class: "panel-h" }, "Settled"));
      for (const x of d.decisions_resolved) out.push(h("div", { class: "panel-card" }, h("strong", {}, x.summary),
        h("p", { class: "secondary-text" }, x.relayed ? (x.quote ? `Claude reports you said “${x.quote}”: ${x.choice}` : `Claude answered without your words: ${x.choice}`) : `You chose ${x.choice}`)));
    }
    if (!out.length) out.push(h("p", { class: "panel-empty" }, "No decisions. When Claude chooses something alone, such as which take opens the cut, it is flagged here for you."));
    return out;
  }
  function activity(r, d) {
    if (!d.activity.length) return [h("p", { class: "panel-empty" }, "Nothing has happened yet.")];
    return [h("div", { class: "feed panel-card" }, d.activity.map((a) => h("div", { class: "feed-item" }, avatar(a.who),
      h("p", { class: "feed-text" }, h("strong", {}, a.who), ` ${a.text}`), h("span", { class: "feed-when" }, ago(a.at)))))];
  }

  function editTitle() {
    const r = row();
    if (!r || titleEl.querySelector("input")) return;
    const inp = h("input", { class: "input ws-title-edit", value: r.name, "aria-label": `Name of ${r.name}`, maxlength: "200" });
    titleEl.replaceChildren(inp);
    inp.focus(); inp.select();
    let done = false;
    const finish = (save) => {
      if (done) return;
      done = true;
      const v = inp.value.trim();
      titleEl.textContent = save && v ? v : r.name;
      if (save && v && v !== r.name) act.fields([r], { title: v }, { say: `Renamed to ${v}` });
      titleEl.focus();
    };
    inp.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); finish(true); }
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); finish(false); }
    });
    inp.addEventListener("blur", () => finish(true));
  }

  return {
    el, open, close, paintRow: () => { paintRow(); },
    isOpen: () => !el.hidden,
    refreshIf: (projects) => { const r = row(); if (r && projects.includes(r.project)) fetchDetail(); },
  };
}
