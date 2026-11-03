// The production desk connects source material, editorial work and release review.
import { h, clear, chip } from "./dom.js";

export async function mount(root, ctx) {
  let alive = true;
  const host = h("section", { class: "desk" });
  const body = h("div", { class: "desk-body" });
  const form = intakeForm();
  const error = h("p", { class: "desk-error", role: "alert", hidden: true });
  const retry = h("button", { type: "button", hidden: true, onclick: load }, "Try again");
  host.append(h("header", { class: "desk-head" }, h("div", {}, h("h1", {}, "Studio desk"),
    h("p", {}, "Bring in your source material, produce a draft, and review what goes out.")),
    h("a", { class: "btn", href: "#/workbench" }, "Open production workbench"),
    h("a", { href: "#/calendar" }, "Open calendar")), error, retry, form, body);
  root.replaceChildren(host);

  async function load() {
    try {
      const data = await ctx.api.get("desk");
      if (!alive) return;
      error.hidden = retry.hidden = true;
      draw(data);
    } catch (e) {
      if (!alive) return;
      error.hidden = retry.hidden = false;
      error.textContent = `The desk could not refresh. ${e.message} Your text stays here.`;
    }
  }

  function intakeForm() {
    const kind = h("select", { name: "kind", "aria-label": "Source type" },
      h("option", { value: "idea" }, "An idea"), h("option", { value: "writing" }, "An essay or passage"));
    const title = h("input", { name: "title", maxlength: 200, placeholder: "Title (optional)", "aria-label": "Source title", hidden: true });
    const text = h("textarea", { name: "text", rows: 4, required: true, maxlength: 200000,
      placeholder: "A thought, a passage, something you want to make…", "aria-label": "Source text" });
    const save = h("button", { class: "primary", type: "submit" }, "Add to ideas");
    const message = h("p", { class: "desk-intake-result", role: "status" });
    kind.addEventListener("change", () => { title.hidden = kind.value !== "writing"; save.textContent = kind.value === "idea" ? "Add to ideas" : "Save source text"; });
    const f = h("form", { class: "desk-intake" }, h("h2", {}, "Start with your material"),
      h("p", {}, "Paste an idea or your own writing. Recordings enter through your agent; the original files stay in place."),
      h("div", { class: "desk-intake-fields" }, kind, title), text,
      h("div", { class: "desk-intake-foot" }, save, h("a", { href: "#/library" }, "Browse media library")), message);
    f.addEventListener("submit", async (e) => {
      e.preventDefault();
      save.disabled = true; message.textContent = "Saving…";
      try {
        const res = await ctx.api.post("intake", { kind: kind.value, title: title.value, text: text.value });
        if (!alive) return;
        if (res.fixtures) message.textContent = res.note;
        else {
          text.value = ""; title.value = "";
          message.replaceChildren(h("span", {}, res.kind === "idea" ? "Idea saved. " : "Source saved with passages your agent can quote. "),
            res.post ? h("a", { href: `#/post/${encodeURIComponent(res.post.id)}` }, "Open idea") : h("span", {}, "Ask your agent to make a content plan."));
        }
        await load(); ctx.refreshChrome().catch(() => {});
      } catch (e) { message.textContent = `Could not save. ${e.message} Your text is still here.`; }
      finally { if (alive) save.disabled = false; }
    });
    return f;
  }

  function draw(data) {
    clear(body);
    if (data.fixtures) body.append(h("p", { class: "desk-notice" }, "Sample studio. Content and accounts are fictional; source intake saves nothing."));
    for (const w of data.warnings || []) body.append(h("p", { class: "desk-notice" }, w));
    const groups = data.groups;
    const lanes = [
      ["ideas", "Ideas", "#/ideas", "Thoughts to develop"],
      ["drafts", "In the making", "#/ideas", "Copy and media taking shape"],
      ["review", "Your review", "#/review", "Ready for your eyes and ears"],
      ["rework", "Needs attention", "#/feedback", "Feedback, retries and missed slots"],
      ["scheduled", "Ready and scheduled", "#/calendar", "Approved pool and timed posts"],
      ["published", "Published", "#/grid", "Completed releases"],
    ];
    body.append(h("section", { class: "desk-flow", "aria-label": "Content pipeline" }, lanes.map(([key, title, href, hint]) => {
      const g = groups[key];
      return h("article", { class: "desk-lane" }, h("header", {}, h("a", { href }, title), h("span", { class: "desk-total" }, g.count)),
        h("p", { class: "desk-hint" }, hint),
        g.items.length ? h("ul", {}, g.items.slice(0, 5).map((p) => h("li", {}, h("a", { href: `#/post/${encodeURIComponent(p.id)}` }, p.title),
          (p.variants || []).map((v) => h("div", { class: "desk-variant" }, chip(v.status), h("span", {}, v.account)))))) : h("p", { class: "desk-quiet" }, "Nothing here yet"));
    })));
    const sourceList = h("section", { class: "desk-sources" }, h("h2", {}, "Source material"));
    if (!(data.sources || []).length) sourceList.append(h("p", {}, "Paste some writing above, or ask your agent to ingest an essay, audio or video file."));
    else sourceList.append(h("ul", {}, data.sources.map((s) => h("li", {}, h("strong", {}, s.title), h("span", {}, `${s.passages} passages`), h("code", {}, s.name)))));
    const episodes = h("section", { class: "desk-episodes" }, h("h2", {}, "Episode workbench"));
    if (!(data.episodes || []).length) episodes.append(h("p", {}, "Episodes alongside this studio will appear here, with their next step and review checkpoint."));
    for (const ep of data.episodes || []) {
      const art = h("article", { class: "desk-episode" }, h("h3", {}, ep.title));
      if (ep.error) art.append(h("p", { role: "alert" }, ep.error));
      else art.append(...[h("ol", { class: "desk-stages" }, ep.stages.map((s) => h("li", { class: s.done ? "done" : s.skipped ? "skipped" : "pending", title: s.human_gate ? "Human review checkpoint" : "" },
        h("span", { "aria-hidden": "true" }, s.done ? "✓ " : s.skipped ? "– " : "○ "), s.title))),
        ep.review ? h("p", { class: "desk-notice" }, "Waiting on your review: ", ep.review) : null,
        ep.next ? h("p", {}, "Next: ", ep.next.title) : null,
        h("details", {}, h("summary", {}, "Continue with your agent"), h("p", {}, "Ask your agent to resume this episode and read its PROGRESS.md."), h("code", {}, ep.resume))].filter(Boolean));
      episodes.append(art);
    }
    body.append(h("div", { class: "desk-workbench" }, sourceList, episodes));
    const integrations = h("section", { class: "desk-integrations" }, h("h2", {}, "Connected tools and publishing"),
      h("p", {}, "Your agent uses the tools you connect. Live account checks and editorial approval stay with you."),
      h("div", { class: "desk-integration-list" }, data.integrations.map((i) => h("article", {}, h("h3", {}, i.title), h("p", { class: "desk-state" }, i.state), h("p", {}, i.detail)))));
    if (data.accounts.length) integrations.append(h("ul", { class: "desk-account-list" }, data.accounts.map((a) => h("li", {}, h("a", { href: "#/accounts" }, a.label || a.id),
      h("span", {}, `${a.platform}: ${a.paused ? "paused" : a.live ? "live enabled" : "dry run"}`),
      h("span", {}, a.credentials_ready ? "Credentials present; live verification pending" : "Account setup incomplete")))));
    else integrations.append(h("p", {}, h("a", { href: "#/accounts" }, "Set up your publishing accounts")));
    body.append(integrations);
  }
  await load();
  return { refresh: load, unmount() { alive = false; } };
}
