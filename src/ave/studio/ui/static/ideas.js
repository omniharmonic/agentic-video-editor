// Ideas (#/ideas): the posts that are only a thought so far. Claude files an idea with its reason and how sure it is; nothing here has words or
// pictures yet, so nothing else in the studio shows it. A person turns the good ones into drafts (the idea becomes the first words) or sets the rest
// aside. Both can be undone. Brand and campaign filters are the same ones the calendar uses.

import { announce, h } from "./dom.js";
import { filterBar, postBrand, readFilters, writeFilters } from "./filters.js";
import { ago } from "./panels.js";

const sure = (c) => (c == null ? null : Math.round(c * 100));
const doubt = (c) => (c == null ? "" : c >= 0.7 ? "Likely to work" : c >= 0.45 ? "Worth a try" : "A long shot");

export async function mount(root, ctx) {
  const accounts = ctx.accounts;
  const st = { posts: [], campaigns: [], filters: readFilters(accounts), loaded: false, alive: true, error: null };
  const bar = h("div", { class: "fbar-host" });
  const host = h("div", { class: "ideas-host", "aria-live": "polite" });
  root.replaceChildren(h("section", { class: "ideas" },
    h("header", { class: "ideas-head" }, h("h1", {}, "Ideas"),
      h("p", { class: "ideas-sub" }, "Thoughts Claude has filed, with why and how sure it is. Turn the good ones into drafts; the idea becomes the first words.")),
    bar, host));

  async function load() {
    try {
      const [p, c] = await Promise.all([ctx.api.get("posts", { stage: "idea" }), ctx.api.get("campaigns").catch(() => ({ campaigns: [] }))]);
      if (!st.alive) return;
      st.posts = p.posts || [];
      st.campaigns = (c.campaigns || []).map((x) => ({ id: x.campaign.id, name: x.campaign.name, accounts: x.campaign.accounts || [] }));
      st.loaded = true; st.error = null;
    } catch (e) { if (!st.alive) return; st.error = e; }
    draw();
  }
  function setFilters(next) { st.filters = next; writeFilters(next); draw(); }

  function visible() {
    const f = st.filters;
    return st.posts.filter((p) => (!f.campaign || p.campaign === f.campaign) && (f.brand === null || postBrand(p, accounts, st.campaigns) === f.brand));
  }

  function draw() {
    bar.replaceChildren(filterBar({ accounts, campaigns: st.campaigns, filters: st.filters, onChange: setFilters, showAccounts: false, label: "Brand and campaign filters" }));
    bar.hidden = !bar.firstChild || !bar.firstChild.childNodes.length;
    if (st.error) {
      host.replaceChildren(h("div", { class: "empty empty-error", role: "alert" }, h("h2", {}, "The ideas could not load"), h("p", {}, st.error.message),
        h("button", { type: "button", class: "primary", onclick: () => { st.error = null; load(); } }, "Try again")));
      return;
    }
    if (!st.loaded) { host.replaceChildren(h("p", { class: "loading" }, "Loading the ideas")); return; }
    const list = visible();
    if (!st.posts.length) {
      host.replaceChildren(h("div", { class: "empty" }, h("h2", {}, "No ideas waiting"),
        h("p", {}, "Ask Claude for ideas from an episode, a blog post or a single line. They land here with the reason Claude gives and how sure it is."),
        h("p", { class: "empty-cmd" }, "ave studio ingest idea \"Five questions to ask at your own long table\"")));
      return;
    }
    if (!list.length) {
      host.replaceChildren(h("div", { class: "empty empty-filter" }, h("h2", {}, "No ideas match these filters"),
        h("p", {}, `${st.posts.length} ${st.posts.length === 1 ? "idea is" : "ideas are"} waiting under other brands or campaigns.`),
        h("button", { type: "button", class: "primary", onclick: () => setFilters({ brand: null, campaign: null, off: [] }) }, "Clear filters")));
      return;
    }
    host.replaceChildren(h("p", { class: "ideas-count" }, `${list.length} ${list.length === 1 ? "idea" : "ideas"}${list.length !== st.posts.length ? ` of ${st.posts.length}` : ""}`),
      h("ul", { class: "idea-list" }, list.map(card)));
  }

  function card(p) {
    const idea = p.idea || {};
    const pct = sure(idea.confidence);
    const brand = postBrand(p, accounts, st.campaigns);
    const camp = st.campaigns.find((c) => c.id === p.campaign);
    const origin = idea.origin && (idea.origin.ref || idea.origin.type) ? `From ${[idea.origin.type, idea.origin.ref].filter(Boolean).join(" ")}` : null;
    const li = h("li", { class: "idea", dataset: { post: p.id } });
    li.append(
      h("p", { class: "idea-text" }, idea.text || "(no text)"),
      idea.reason ? h("p", { class: "idea-why" }, h("span", { class: "idea-whyl" }, "Why"), idea.reason) : null,
      pct != null ? h("div", { class: "idea-sure" }, h("span", { class: "idea-bar", "aria-hidden": "true" }, h("span", { style: { width: pct + "%" } })), h("span", {}, `${doubt(idea.confidence)}, ${pct}% sure`)) : null,
      h("p", { class: "idea-meta" }, [brand ? h("span", { class: "idea-brand" }, brand) : null, camp ? h("span", {}, camp.name) : null, origin ? h("span", {}, origin) : null, h("span", {}, `Filed ${ago(p.created)}${p.by ? " by " + (p.by === "claude" ? "Claude" : p.by) : ""}`)].filter(Boolean)),
      h("div", { class: "idea-actions" },
        h("button", { type: "button", class: "primary", dataset: { act: "draft" }, onclick: () => toDraft(p, idea) }, "Turn into a draft"),
        h("a", { class: "btn", href: `#/post/${encodeURIComponent(p.id)}` }, "Open"),
        h("button", { type: "button", class: "quiet", dataset: { act: "dismiss" }, onclick: () => dismiss(p) }, "Not now"),
        !brand && accounts.some((a) => a.brand) ? brandPicker(p) : null));
    return li;
  }

  function brandPicker(p) {
    const names = [...new Set(accounts.map((a) => a.brand).filter(Boolean))];
    const sel = h("select", { "aria-label": "Assign a brand", onchange: async (e) => { if (e.target.value) await edit(p, { brand: e.target.value }, `Assigned to ${e.target.value}.`, { brand: null }); } },
      h("option", { value: "" }, "Assign a brand"), names.map((n) => h("option", { value: n }, n)));
    return h("label", { class: "idea-brandpick" }, sel);
  }

  async function edit(p, patch, text, back) {
    try {
      const res = await ctx.api.post(`posts/${encodeURIComponent(p.id)}/edit`, { expected_rev: p.rev, patch });
      const rev = res.post.rev;
      announce(text, { action: { label: "Undo", run: async () => {
        try { await ctx.api.post(`posts/${encodeURIComponent(p.id)}/edit`, { expected_rev: rev, patch: back }); announce("Undone."); } catch (e) { announce(`Could not undo: ${e.message}`, { tone: "error" }); }
        await load(); ctx.refreshChrome().catch(() => {});
      } } });
      await load(); ctx.refreshChrome().catch(() => {});
    } catch (e) {
      announce(e.status === 409 ? "Claude changed this idea first. The list is refreshed: try again." : `Could not save: ${e.message}`, { tone: "error" });
      load();
    }
  }
  const toDraft = (p, idea) => edit(p, { "copy.base": idea.text || "" }, "Turned into a draft. Open it to add the pictures and an account.", { "copy.base": "" });
  const dismiss = (p) => edit(p, { stage: "dropped" }, "Set aside. It will not come back.", { stage: "idea" });

  await load();
  return {
    refresh: load,
    unmount() { st.alive = false; },
  };
}

