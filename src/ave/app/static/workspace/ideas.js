// Ideas: the studio's posts that are only a thought so far, as artifacts of kind "idea" in their own Ideas group at the top of
// the Main table. Claude files each idea with its reason and how sure it is. A person turns the good ones into drafts (the idea
// becomes the first words), sets the rest aside, gives one a brand, or adds one by hand. Every act is the old Ideas page's own
// edit (static/plan.js IDEA_ACTS, through POST /api/studio/posts/<id>/edit or POST posts), recorded as the person, and each one is
// undoable from the toast. Changes from Claude arrive as `posts` events on the Workspace stream.

import { h, button, icon, toast, busy } from "../dom.js";
import { menu, plain, avatar } from "./common.js";
import { editPost, IDEA_ACTS, IDEA_COMMAND, ideaDoubt, ideaSure, NO_IDEAS } from "/app/shared/plan.js";
import { postBrand } from "/app/shared/filters.js";
import { ago } from "/app/shared/panels.js";

const COLS = "46px minmax(220px, 2.2fr) minmax(160px, 1.6fr) 148px 124px minmax(300px, 1.8fr)";

export function createIdeas(ctx, studio) {
  const { S } = ctx;
  let posts = null, campaigns = [], error = null, seq = 0, alive = false;
  const el = h("section", { class: "ideas-grp", "data-group": "ideas", "aria-label": "Ideas", style: { "--group": "var(--claude)" }, hidden: true });

  const here = () => S.route.page === "board" && !S.route.waiting && (!S.route.project || ctx.projects.get(S.route.project)?.kind === "studio");
  const collapsedKey = "ideas|ideas";
  const passQ = (t) => S.q.split(/\s+/).filter(Boolean).every((w) => String(t || "").toLowerCase().includes(w));

  async function load({ quiet = false } = {}) {
    const my = ++seq;
    try {
      await studio.ready();
      const [p, c] = await Promise.all([studio.api.get("posts?stage=idea"), studio.api.get("campaigns").catch(() => ({ campaigns: [] }))]);
      if (my !== seq) return;
      posts = p.posts || [];
      campaigns = (c.campaigns || []).map((x) => ({ id: x.campaign.id, name: x.campaign.name, accounts: x.campaign.accounts || [] }));
      error = null;
    } catch (e) {
      if (my !== seq) return;
      error = e;
      if (quiet && posts) toast(plain(e, "read the ideas"), { tone: "negative" });
    }
    paint();
  }

  function paint() {
    el.hidden = !alive || !here() || (!posts && !error) || (error && !posts && studio.st.error);
    if (el.hidden) return;
    const list = (posts || []).filter((p) => passQ((p.idea || {}).text));
    if (S.q && !list.length) { el.hidden = true; return; }
    const collapsed = S.collapsed.has(collapsedKey);
    el.classList.toggle("is-collapsed", collapsed);
    const n = list.length;
    const countText = `${n} ${n === 1 ? "idea" : "ideas"}`;
    const toggle = h("button", { type: "button", class: "grp-toggle", "aria-expanded": String(!collapsed), onclick: () => {
      if (S.collapsed.has(collapsedKey)) S.collapsed.delete(collapsedKey); else S.collapsed.add(collapsedKey);
      ctx.savePrefs(); paint(); } }, icon("chevron"), h("span", { class: "grp-title" }, "Ideas"));
    const head = h("header", { class: "grp-head" }, h("span", { class: "grp-handle-space", "aria-hidden": "true" }), toggle, h("span", { class: "grp-count" }, countText),
      h("span", { class: "chip chip-claude" }, avatar("Claude"), "Filed by Claude"));
    if (collapsed) { el.replaceChildren(head, h("div", { class: "grp-collapsed" }, h("span", {}, countText))); return; }
    const hcell = (t) => h("div", { class: "ws-cell c-head", role: "columnheader" }, h("span", {}, t));
    const headRow = h("div", { class: "ws-row ws-hrow idea-row", role: "row", style: { "grid-template-columns": COLS } },
      h("div", { class: "ws-cell c-check", role: "columnheader" }), h("div", { class: "ws-cell c-name", role: "columnheader" }, h("span", {}, "Idea")),
      hcell("Why"), hcell("How sure"), hcell("Brand"), h("div", { class: "ws-cell c-head", role: "columnheader" }, h("span", {}, "")));
    let rowsEl;
    if (error && !posts) {
      rowsEl = h("div", { class: "idea-empty", role: "alert" }, h("p", {}, `The ideas could not be read: ${error.message}`),
        button("Try again", { variant: "secondary", size: "sm", onclick: (e) => busy(e.currentTarget, () => load()) }));
    } else if (!posts.length) {
      rowsEl = h("div", { class: "idea-empty", role: "status" }, h("p", { class: "idea-empty-title" }, "No ideas waiting"),
        h("p", { class: "secondary-text" }, NO_IDEAS), h("code", { class: "say" }, IDEA_COMMAND));
    } else rowsEl = h("div", { class: "ws-rows", role: "rowgroup" }, list.map(row));
    const add = h("input", { class: "ws-add-input", placeholder: "+ Add idea", "aria-label": "Add an idea", autocomplete: "off" });
    add.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { add.value = ""; add.blur(); }
      if (e.key === "Enter" && add.value.trim()) { e.preventDefault(); create(add.value.trim(), add); }
    });
    const addRow = h("div", { class: "ws-row ws-addrow idea-row", role: "row", style: { "grid-template-columns": COLS } }, h("div", { class: "ws-cell c-check", role: "presentation" }),
      h("div", { class: "ws-cell c-addname", role: "gridcell", style: { "grid-column": "2 / -1" } }, add));
    const was = el.querySelector(".ws-add-input");           // a redraw must not take what the director is typing, or the cursor
    const typing = was && { value: was.value, focused: document.activeElement === was, from: was.selectionStart, to: was.selectionEnd };
    el.replaceChildren(head, h("div", { class: "grp-table", role: "grid", "aria-label": "Ideas" }, headRow, rowsEl, addRow));
    if (typing) {
      add.value = typing.value;
      if (typing.focused) { add.focus(); add.setSelectionRange(typing.from, typing.to); }
    }
  }

  function row(p) {
    const idea = p.idea || {};
    const pct = ideaSure(idea.confidence);
    const brand = postBrand(p, studio.st.accounts, campaigns);
    const brands = [...new Set(studio.st.accounts.map((a) => a.brand).filter(Boolean))];
    const cell = (cls, kids, extra = {}) => h("div", { class: `ws-cell ${cls}`, role: "gridcell", ...extra }, kids);
    return h("div", { class: "ws-row idea-row idea", role: "row", dataset: { post: p.id, kind: "idea" }, style: { "grid-template-columns": COLS } },
      cell("c-check", null),
      cell("c-name idea-name", [h("span", { class: "chip idea-kind" }, "Idea"), h("span", { class: "idea-text", title: idea.text || "" }, idea.text || "(no text)")]),
      cell("idea-why", idea.reason ? h("span", { title: idea.reason }, idea.reason) : h("span", { class: "empty" }, "–")),
      cell("idea-sure", pct == null ? h("span", { class: "empty" }, "–") : [h("span", { class: "idea-bar", "aria-hidden": "true" }, h("span", { style: { width: `${pct}%` } })),
        h("span", { title: ideaDoubt(idea.confidence) }, `${pct}% sure`)]),
      cell("idea-brandcell", brand ? h("span", { class: "idea-brand" }, brand)
        : brands.length ? button("Give it a brand", { variant: "tertiary", size: "sm", class: "idea-brandpick", "aria-haspopup": "menu",
          onclick: (e) => menu(e.currentTarget, brands.map((b) => ({ label: b, onSelect: () => act(p, IDEA_ACTS.brand(idea, b)) })), { label: "Brand", heading: "Whose idea is it" }) })
          : h("span", { class: "empty" }, "–")),
      cell("idea-acts", [
        button("Turn into a draft", { variant: "primary", size: "sm", "data-act": "promote", onclick: (e) => act(p, IDEA_ACTS.promote(idea), e.currentTarget) }),
        button("Not now", { variant: "tertiary", size: "sm", "data-act": "dismiss", onclick: (e) => act(p, IDEA_ACTS.dismiss(idea), e.currentTarget) }),
        h("span", { class: "secondary-text idea-filed" }, `Filed ${ago(p.created)}${p.by ? ` by ${p.by === "claude" ? "Claude" : p.by}` : ""}`)]));
  }

  async function act(p, a, btn) {
    const run = async () => {
      try {
        const res = await editPost(studio.api, p, a.patch);
        const rev = res.post.rev;
        toast(a.said, { key: "idea", timeout: 10000, action: "Undo", onAction: async () => {
          try { await editPost(studio.api, { id: p.id, rev }, a.back); toast("Undone", { key: "idea" }); }
          catch (e) { toast(e.status === 409 ? "Claude changed this idea since, so it was left as it is." : plain(e, "undo that"), { tone: "negative" }); }
          await load({ quiet: true });
        } });
      } catch (e) { toast(e.status === 409 ? "Claude changed this idea first. The list is read again: try again." : plain(e, "save that"), { tone: "negative" }); }
      await load({ quiet: true });
    };
    return btn ? busy(btn, run) : run();
  }
  async function create(text, input) {
    input.disabled = true;
    try {
      const res = await studio.api.post("posts", { idea: text });
      input.value = "";
      const made = res.post;
      toast("Added the idea", { key: "idea", action: "Undo", onAction: async () => {
        try { await editPost(studio.api, made, IDEA_ACTS.dismiss().patch); } catch (e) { toast(plain(e, "undo that"), { tone: "negative" }); }
        await load({ quiet: true });
      } });
      await load({ quiet: true });
    } catch (e) { toast(plain(e, "add the idea"), { tone: "negative" }); }
    input.disabled = false;
    el.querySelector(".ideas-grp .ws-add-input, .ws-add-input")?.focus();
  }

  ctx.onPosts(() => { if (alive) load({ quiet: true }); });
  return {
    el,
    render() { if (!alive) { alive = true; load(); } else paint(); },
    leave() { alive = false; seq++; el.hidden = true; },
  };
}
