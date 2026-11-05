// Campaigns on the board: a "Campaign" Group by, and the old Campaigns page's acts (new, rename, milestones, which posts are in it)
// from the group header and the item panel. A campaign gathers studio posts, so an artifact is in a campaign through the post it is
// composed in (its `post` field). Every write is the studio's own campaign request (`POST campaigns`, `campaigns/<id>/edit`,
// `campaigns/<id>/members`, each checked against the revision it read), written as the person.

import { h, button, icon, toast, popover, busy } from "../dom.js";
import { plain, menu, parseDay, fmtDate } from "./common.js";
import { postName } from "/app/shared/plan.js";

const NONE = "campaign:";

export function createCampaigns(ctx, studio) {
  let list = [], byPost = new Map(), loaded = false, loading = null;
  const changed = new Set();

  async function load() {
    if (loading) return loading;
    studio.ready().catch(() => {});                       // the accounts the edit form offers
    loading = studio.api.get("campaigns").then((res) => {
      list = (res.campaigns || []).map((r) => ({ ...r.campaign, posts: r.posts || [] }));
      byPost = new Map(list.flatMap((c) => c.posts.map((p) => [p, c])));
      loaded = true;
      for (const fn of changed) fn();
    }).catch(() => { loaded = true; }).finally(() => { loading = null; });
    return loading;
  }
  const find = (id) => list.find((c) => c.id === id) || null;
  const of = (row) => (row?.post ? byPost.get(row.post) || null : null);

  // ---- the board's groups
  function groupOf(row) {
    const c = of(row);
    if (!c) return { key: NONE, title: "No campaign", colour: "var(--stage-plan)", rank: 1e6 };
    return { key: `campaign:${c.id}`, title: c.name, colour: `var(--group-${(list.indexOf(c) % 8) + 1})`, rank: list.indexOf(c), campaign: c.id };
  }
  const allGroups = () => list.map((c, i) => ({ key: `campaign:${c.id}`, title: c.name, colour: `var(--group-${(i % 8) + 1})`, rank: i, campaign: c.id }));
  const idOfGroup = (key) => (key && key.startsWith("campaign:") ? key.slice(9) || null : null);

  // ---- writes
  async function edit(c, fields, say) {
    try {
      await studio.api.post(`campaigns/${encodeURIComponent(c.id)}/edit`, { expected_rev: c.rev, fields });
      await load();
      if (say) toast(say);
      return true;
    } catch (e) {
      toast(e.code === "rev_conflict" || e.status === 409 ? "Someone changed this campaign just now. Look again and try once more." : plain(e, "save the campaign"), { tone: "negative" });
      await load();
      return false;
    }
  }
  async function membership(c, add, remove) {
    const ids = [...add, ...remove];
    const revs = {};
    for (const id of ids) revs[id] = (await studio.api.get(`posts/${encodeURIComponent(id)}`)).post.rev;
    await studio.api.post(`campaigns/${encodeURIComponent(c.id)}/members`, { expected_rev: c.rev, post_revs: revs, add, remove });
    await load();
  }
  // Put one post in a campaign (or none). Leaving a campaign is a removal from the one it is in.
  async function place(row, id) {
    const was = of(row);
    if ((was?.id || null) === id) return;
    try {
      if (was) await membership(was, [], [row.post]);
      if (id) {
        try { await membership(find(id), [row.post], []); }
        catch (e) { if (was) await membership(find(was.id) || was, [row.post], []).catch(() => {}); throw e; }   // never left in no campaign
      }
      const now = id ? find(id) : null;
      toast(now ? `${row.name} is in ${now.name}` : `${row.name} is in no campaign`, {
        action: "Undo", onAction: () => place(row, was?.id || null) });
      ctx.rerender();
    } catch (e) { toast(plain(e, "change the campaign"), { tone: "negative" }); await load(); }
  }

  // ---- forms (kit popovers, typed fields only)
  function field(label, input) { return h("label", { class: "field" }, h("span", { class: "field-label" }, label), input); }
  // One form for a new campaign and for editing one: every field the old Campaigns page had (name, goal, brief, the dates, themes,
  // accounts and posts per week for each), saved with the studio's own `POST campaigns` or `campaigns/<id>/edit` at the revision read.
  function create(anchor, opts = {}) { return campaignForm(anchor, null, opts); }
  function campaignForm(anchor, c, { thenAdd } = {}) {
    const accts = studio.st.accounts || [];
    const name = h("input", { class: "input", maxlength: "200", "aria-label": "Campaign name", placeholder: "Spring launch", value: c?.name || "" });
    const goal = h("input", { class: "input", maxlength: "2000", "aria-label": "Goal", placeholder: "What it is for", value: c?.goal || "" });
    const brief = h("textarea", { class: "input", rows: "3", maxlength: "10000", "aria-label": "Brief", placeholder: "The story, the audience, what to avoid" });
    brief.value = c?.brief || "";
    const start = h("input", { class: "input", "aria-label": "Starts", placeholder: "2026-11-04", value: c?.window?.start || "" });
    const end = h("input", { class: "input", "aria-label": "Ends", placeholder: "2026-11-30", value: c?.window?.end || "" });
    const themes = h("input", { class: "input", maxlength: "1000", "aria-label": "Themes", placeholder: "bread, mornings, the street", value: (c?.themes || []).join(", ") });
    const picked = new Set(c?.accounts || []);
    const perWeek = new Map(accts.map((a) => [a.id, h("input", { class: "input cmp-perweek", inputmode: "numeric", "aria-label": `Posts per week on ${a.label || a.id}`,
      placeholder: "—", value: c?.cadence?.[a.id]?.per_week ?? "" })]));
    const acctRows = accts.map((a) => {
      const cb = h("button", { type: "button", class: "check", role: "checkbox", "aria-checked": String(picked.has(a.id)), "aria-label": `Use ${a.label || a.id}`,
        onclick: () => { picked.has(a.id) ? picked.delete(a.id) : picked.add(a.id); cb.setAttribute("aria-checked", String(picked.has(a.id))); perWeek.get(a.id).disabled = !picked.has(a.id); } }, icon("check"));
      perWeek.get(a.id).disabled = !picked.has(a.id);
      return h("div", { class: "cmp-acct" }, cb, h("span", { class: "grow" }, a.label || a.id), perWeek.get(a.id), h("span", { class: "secondary-text" }, "a week"));
    });
    const err = h("p", { class: "field-error", role: "alert", hidden: true });
    const save = button(c ? "Save campaign" : "Create campaign", { variant: "primary", size: "sm", onclick: (e) => busy(e.currentTarget, submit) });
    const fail = (t, el) => { err.textContent = t; err.hidden = false; el?.focus(); };
    async function submit() {
      const n = name.value.trim();
      const a = start.value.trim() ? parseDay(start.value) : "", b = end.value.trim() ? parseDay(end.value) : "";
      if (!n) return fail("Give the campaign a name.", name);
      if (a === null || b === null || (!!a !== !!b)) return fail("Write both dates like 2026-11-04, or leave both empty.", start);
      if (a && a > b) return fail("The campaign has to start before it ends.", start);
      const accounts = accts.map((x) => x.id).filter((id) => picked.has(id));
      const cadence = {};
      for (const id of accounts) {
        const v = perWeek.get(id).value.trim();
        if (!v) continue;
        if (!/^\d{1,3}$/.test(v) || +v > 100) return fail("Posts per week is a whole number from 0 to 100.", perWeek.get(id));
        cadence[id] = c ? { per_week: +v } : +v;
      }
      const fields = { name: n, goal: goal.value.trim(), brief: brief.value, window: a ? { start: a, end: b } : null,
        themes: themes.value.split(",").map((t) => t.trim()).filter(Boolean), accounts, cadence };
      try {
        let res;
        if (c) res = await studio.api.post(`campaigns/${encodeURIComponent(c.id)}/edit`, { expected_rev: c.rev, fields });
        else { const { window: w, ...rest } = fields; res = await studio.api.post("campaigns", { ...rest, ...(w ? { window: w } : {}) }); }
        pop.close();
        await load();
        if (thenAdd) await place(thenAdd, res.campaign.id); else toast(c ? `Saved ${n}` : `Created ${n}`);
        ctx.rerender();
      } catch (e) {
        fail(e.status === 409 ? "Someone changed this campaign just now. Close this and open it again." : plain(e, c ? "save the campaign" : "create the campaign"));
      }
    }
    name.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); save.click(); } });
    const pop = popover(anchor, h("div", { class: "cmp-form cmp-edit" }, h("p", { class: "popover-title" }, c ? `Edit ${c.name}` : "New campaign"),
      field("Name", name), field("Goal", goal), field("Brief", brief), h("div", { class: "cmp-dates" }, field("Starts", start), field("Ends", end)),
      field("Themes (separate with commas)", themes),
      accts.length ? h("div", { class: "field" }, h("span", { class: "field-label" }, "Accounts and posts per week"), h("div", { class: "cmp-accts" }, acctRows)) : null,
      err, h("div", { class: "popover-actions" }, button("Cancel", { variant: "tertiary", size: "sm", onclick: () => pop.close() }), save)),
    { label: c ? `Edit ${c.name}` : "New campaign", modal: true, wide: true });
    name.focus();
    return pop;
  }
  function rename(anchor, c) {
    const name = h("input", { class: "input", maxlength: "120", value: c.name, "aria-label": "Campaign name" });
    const goal = h("input", { class: "input", maxlength: "300", value: c.goal || "", "aria-label": "Goal" });
    const submit = async () => {
      const n = name.value.trim();
      if (!n) { name.setAttribute("aria-invalid", "true"); return; }
      pop.close();
      const fields = {};
      if (n !== c.name) fields.name = n;
      if (goal.value.trim() !== (c.goal || "")) fields.goal = goal.value.trim();
      if (Object.keys(fields).length && await edit(c, fields, fields.name ? `Renamed to ${n}` : "Saved")) ctx.rerender();
    };
    name.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); submit(); } });
    const pop = popover(anchor, h("div", { class: "cmp-form" }, h("p", { class: "popover-title" }, "Campaign"), field("Name", name), field("Goal", goal),
      h("div", { class: "popover-actions" }, button("Cancel", { variant: "tertiary", size: "sm", onclick: () => pop.close() }),
        button("Save", { variant: "primary", size: "sm", onclick: submit }))), { label: `Rename ${c.name}`, modal: true });
    name.focus(); name.select();
  }
  function milestones(anchor, c0) {
    const body = h("div", { class: "cmp-form cmp-ms" });
    const pop = popover(anchor, body, { label: `Milestones of ${c0.name}`, modal: true, wide: true });
    const paint = () => {
      const c = find(c0.id) || c0;
      const ms = c.milestones || [];
      const title = h("input", { class: "input", maxlength: "120", "aria-label": "New milestone", placeholder: "Launch day" });
      const date = h("input", { class: "input", "aria-label": "Milestone date", placeholder: "2026-11-20" });
      const add = async () => {
        const t = title.value.trim(), d = date.value.trim() ? parseDay(date.value) : null;
        if (!t || d === null && date.value.trim()) { title.setAttribute("aria-invalid", "true"); return; }
        const m = { id: `milestone_${crypto.randomUUID()}`, title: t, date: d || null, done: false };
        if (await edit(c, { milestones: [...ms, m] }, `Added ${t}`)) paint();
      };
      title.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); add(); } });
      // edit one milestone in place: its title and its date
      const editing = (m) => {
        const t = h("input", { class: "input", maxlength: "300", "aria-label": `Title of ${m.title}`, value: m.title });
        const d = h("input", { class: "input", "aria-label": `Date of ${m.title}`, placeholder: "2026-11-20", value: m.date || "" });
        const keep = async () => {
          const tt = t.value.trim(), dd = d.value.trim() ? parseDay(d.value) : null;
          if (!tt || (dd === null && d.value.trim())) { t.setAttribute("aria-invalid", "true"); return; }
          if (await edit(c, { milestones: ms.map((x) => (x.id === m.id ? { ...x, title: tt, date: dd || null } : x)) }, `Saved ${tt}`)) paint();
        };
        const li = body.querySelector(`li[data-ms="${CSS.escape(m.id)}"]`);
        li?.replaceChildren(t, d, button("Save", { variant: "primary", size: "sm", onclick: keep }), button("Cancel", { variant: "tertiary", size: "sm", onclick: paint }));
        t.focus();
      };
      body.replaceChildren(h("p", { class: "popover-title" }, `Milestones · ${c.name}`),
        ms.length ? h("ul", { class: "cmp-ms-list" }, ms.map((m) => h("li", { dataset: { ms: m.id } },
          h("button", { type: "button", class: "check", role: "checkbox", "aria-checked": String(!!m.done), "aria-label": `${m.title} is done`,
            onclick: async () => { if (await edit(c, { milestones: ms.map((x) => (x.id === m.id ? { ...x, done: !x.done } : x)) })) paint(); } }, icon("check")),
          h("span", { class: m.done ? "cmp-done" : "" }, m.title), h("span", { class: "secondary-text" }, m.date ? fmtDate(m.date) : "No date"),
          button(`Edit ${m.title}`, { variant: "tertiary", size: "sm", text: "Edit", onclick: () => editing(m) }),
          button(`Remove ${m.title}`, { variant: "tertiary", size: "sm", iconName: "x", iconOnly: true,
            onclick: async () => { if (await edit(c, { milestones: ms.filter((x) => x.id !== m.id) }, `Removed ${m.title}`)) paint(); } }))))
          : h("p", { class: "secondary-text" }, "No milestones yet. Add a launch, a handoff or an important decision."),
        h("div", { class: "cmp-ms-add" }, title, date, button("Add", { variant: "secondary", size: "sm", onclick: add })),
        h("div", { class: "popover-actions" }, button("Done", { variant: "primary", size: "sm", onclick: () => pop.close() })));
    };
    paint();
    body.querySelector("input")?.focus();
  }
  async function posts(anchor, c0) {
    const body = h("div", { class: "cmp-form cmp-posts" }, h("p", { class: "secondary-text" }, "Reading the posts…"));
    const pop = popover(anchor, body, { label: `Posts in ${c0.name}`, modal: true, wide: true });
    let all;
    try { all = (await studio.api.get("posts")).posts || []; }
    catch (e) { body.replaceChildren(h("p", {}, plain(e, "read the posts"))); return; }
    const c = find(c0.id) || c0;
    const chosen = new Set(c.posts);
    const label = (p) => postName(p);
    const save = button("Save", { variant: "primary", size: "sm", onclick: (e) => busy(e.currentTarget, async () => {
      const add = [...chosen].filter((id) => !c.posts.includes(id)), remove = c.posts.filter((id) => !chosen.has(id));
      if (!add.length && !remove.length) { pop.close(); return; }
      try { await membership(c, add, remove); pop.close(); toast(`${c.name}: ${chosen.size} ${chosen.size === 1 ? "post" : "posts"}`); ctx.rerender(); }
      catch (er) { toast(plain(er, "change the campaign"), { tone: "negative" }); }
    }) });
    body.replaceChildren(h("p", { class: "popover-title" }, `Posts in ${c.name}`),
      all.length ? h("div", { class: "cmp-post-list", role: "group", "aria-label": "Posts" }, all.map((p) => {
        const cb = h("button", { type: "button", class: "check", role: "checkbox", "aria-checked": String(chosen.has(p.id)), "aria-label": `Include ${label(p)}`,
          onclick: () => { chosen.has(p.id) ? chosen.delete(p.id) : chosen.add(p.id); cb.setAttribute("aria-checked", String(chosen.has(p.id))); } },
        icon("check"));
        const other = byPost.get(p.id);
        return h("div", { class: "cmp-post" }, cb, h("span", { class: "grow" }, label(p)), other && other.id !== c.id ? h("span", { class: "secondary-text" }, `In ${other.name}`) : null);
      })) : h("p", { class: "secondary-text" }, "There are no posts yet."),
      h("p", { class: "secondary-text" }, "A post is in one campaign at a time. Its times stay where they are."),
      h("div", { class: "popover-actions" }, button("Cancel", { variant: "tertiary", size: "sm", onclick: () => pop.close() }), save));
  }

  // ---- menus for the group header and the item panel
  function groupItems(anchor, key) {
    const c = find(idOfGroup(key));
    const items = [];
    if (c) items.push({ label: "Edit campaign…", icon: "open", onSelect: () => campaignForm(anchor, c) },
      { label: "Rename campaign", onSelect: () => rename(anchor, c) },
      { label: "Milestones…", icon: "flag", onSelect: () => milestones(anchor, c) },
      { label: "Add or remove posts…", icon: "plus", onSelect: () => posts(anchor, c) });
    items.push({ label: "New campaign…", icon: "plus", onSelect: () => create(anchor) });
    return items;
  }
  function panelControl(row) {
    if (!row.post) return null;
    const c = of(row);
    return h("button", { type: "button", class: "btn btn-tertiary btn-sm cmp-pick", "aria-haspopup": "menu", "aria-label": `Campaign: ${c ? c.name : "none"}`,
      onclick: (e) => {
        const a = e.currentTarget;
        menu(a, [
          ...list.map((x) => ({ label: x.name, checked: c?.id === x.id, onSelect: () => place(row, x.id) })),
          { label: "No campaign", checked: !c, onSelect: () => place(row, null) },
          "-",
          { label: "New campaign…", icon: "plus", onSelect: () => create(a, { thenAdd: row }) },
          ...(c ? [{ label: `Edit ${c.name}…`, icon: "open", onSelect: () => campaignForm(a, c) },
            { label: `Milestones of ${c.name}…`, icon: "flag", onSelect: () => milestones(a, c) }] : []),
        ], { label: "Campaign", heading: "Campaign" });
      } }, icon("group"), h("span", {}, c ? c.name : "No campaign"));
  }

  ctx.onPosts(() => load());
  return {
    load, of, groupOf, allGroups, groupItems, panelControl, create,
    loaded: () => loaded, list: () => list,
    onChange: (fn) => { changed.add(fn); return () => changed.delete(fn); },
  };
}
