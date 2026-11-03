// Read-only phone-sized profile and post previews. No platform login or invented analytics.
import { h, clear, thumbImg, icon } from "./dom.js";
import { effectiveMedia, findVariant, localUrl } from "./panels.js";
import {editProfile} from "./authoring.js";
import { openManualExport } from "./manual.js";
import { feedMedia, onSurface } from "./feed-media.js";

export async function mount(root, ctx, route) {
  if (!document.querySelector('link[data-feed-planner]')) document.head.append(h("link", { rel: "stylesheet", href: "/static/planner.css", "data-feed-planner": "" }));
  let alive = true, serial = 0, data = null, selected = null, detail = null, slide = 0;
  let accountId = route.args[0] === "local" ? "" : (route.args[0] || "");
  let surface = "profile";
  const contentSurface = route.query.get("surface") || "feed";
  const host = h("section", { class: "phone-workspace" });
  const body = h("div", { class: "phone-layout" });
  const error = h("p", { class: "phone-error", role: "alert", hidden: true });
  const account = h("select", { "aria-label": "Preview account", onchange: () => { accountId = account.value; selected = detail = null; load(); } },
    h("option", { value: "" }, "All drafts · local plan"), ctx.accounts.map((a) => h("option", { value: a.id, selected: a.id === accountId }, a.label || a.id)));
  const profileButton = h("button", { type: "button", "aria-pressed": "true", onclick: () => { surface = "profile"; draw(); } }, "Profile");
  const postButton = h("button", { type: "button", "aria-pressed": "false", onclick: () => { surface = "post"; draw(); } }, "Post");
  host.append(h("header", { class: "workspace-heading" }, h("div", {}, h("h1", {}, "Phone preview"),
      h("p", {}, "See the feed in context, then take the original files with you.")), h("a", { class: "btn", href: `#/grid/${encodeURIComponent(accountId || "local")}?surface=${contentSurface}` }, "Back to planning")),
    h("div", { class: "phone-toolbar" }, account, h("div", { class: "view-switch", role: "group", "aria-label": "Phone surface" }, profileButton, postButton)), error, body);
  root.replaceChildren(host);

  async function load() {
    const n = ++serial;
    try {
      const result = await ctx.api.get("feed-plan", { account: accountId });
      result.cells = result.cells.filter((c) => onSurface(c, contentSurface));
      if (!alive || serial !== n) return;
      data = result;
      error.hidden = true;
      const cell = selected && data.cells.find((c) => c.post === selected.post && c.variant === selected.variant);
      if (cell || data.cells.length) await choose(cell || data.cells[0], false);
      else { selected = detail = null; }
      draw();
    } catch (e) { if (alive) { error.textContent = e.message; error.hidden = false; } }
  }

  async function choose(cell, showPost = true) {
    if(cell.external){selected=cell;detail=null;draw();return;}
    const n = ++serial;
    selected = cell; slide = 0;
    try {
      const result = await ctx.api.get(`posts/${encodeURIComponent(cell.post)}`);
      const p = result.post;
      const v = findVariant(p, cell.variant) || p.variants[0] || null;
      const media = [...effectiveMedia(p, v)].filter((m) => m.role !== "cover").sort((a, b) => (a.slot || 0) - (b.slot || 0));
      const textPreview = await ctx.api.get(v ? `posts/${encodeURIComponent(p.id)}/preview/${encodeURIComponent(v.id)}` : `posts/${encodeURIComponent(p.id)}/export`, v ? undefined : { draft: "1" });
      const assets = await Promise.all(media.map(async (m) => {
        try { const a = await ctx.api.get(`library/${encodeURIComponent(m.asset)}`); return { ...m, ...(a.asset || {}), url: localUrl(a.url || (a.asset && a.asset.file ? "/files/" + a.asset.file : "")) }; }
        catch { return { ...m, missing: true }; }
      }));
      if (!alive || serial !== n) return;
      detail = { post: p, variant: v, media: assets, caption: v ? (textPreview.caption || {}).text : (textPreview.manifest || {}).caption, firstComment: v ? textPreview.first_comment : (textPreview.manifest || {}).first_comment };
      if (showPost) surface = "post";
      draw();
    } catch (e) { if (alive && serial === n) { detail = null; error.textContent = e.message; error.hidden = false; draw(); } }
  }

  function draw() {
    if (!alive || !data) return;
    profileButton.setAttribute("aria-pressed", String(surface === "profile"));
    postButton.setAttribute("aria-pressed", String(surface === "post"));
    const label = (data.account || {}).label || "Your preview";
    const screen = h("div", { class: "device-screen" }, h("div", { class: "device-status", "aria-hidden": "true" }, h("span", {}, "9:41"), h("span", {}, "● ▰")),
      h("header", { class: "ig-profile-name" }, h("strong", {}, surface === "profile" ? label.replace(/^@/, "") : "Posts"), h("span", { "aria-hidden": "true" }, "☰")));
    if (surface === "profile") {
      screen.append(h("div", { class: "ig-profile-info" }, h("div", { class: "ig-profile-avatar", "aria-hidden": "true" }, data.account.profile?.avatar_url?h("img",{src:data.account.profile.avatar_url,alt:""}):label.replace(/^@/, "").slice(0, 1).toUpperCase()),
        h("div", {}, h("strong", {}, data.account.profile?.display_name||ctx.summary.title || "Your studio"), h("p", {}, `${data.cells.filter((c) => !c.locked).length} planned · ${data.cells.filter((c) => c.locked).length} published or publishing`))),
        h("p", { class: "ig-profile-bio" }, data.account.profile?.bio||"Your local visual plan"),
        h("div", { class: "ig-profile-actions" }, h("button",{type:"button",onclick:()=>editProfile(ctx,accountId||null,load)},"Edit local profile")),
        h("div", { class: "ig-profile-tabs", "aria-hidden": "true" }, h("span", {}, icon("grid")), h("span", {}, icon("reels")), h("span", {}, icon("profile"))));
      const grid = h("div", { class: "ig-profile-grid", "aria-label": "Planned Instagram profile" });
      for (const c of data.cells) grid.append(h("button", { type: "button", class: "ig-profile-tile", "aria-label": `Preview ${c.title}`, onclick: () => choose(c) }, feedMedia(c, c.title)));
      if (!data.cells.length) grid.append(h("p", { class: "ig-empty" }, "Your images will appear here. Add media to a draft to start planning."));
      screen.append(grid);
    } else if (!detail) screen.append(h("p", { class: "ig-empty" }, "Choose a post from the profile to preview it."));
    else {
      const d = detail, v = d.variant;
      const m = d.media[slide];
      const cap = d.caption || "";
      screen.append(h("div", { class: "ig-post-account" }, h("span", { class: "ig-mini-avatar", "aria-hidden": "true" }, label.slice(0, 1).toUpperCase()), h("strong", {}, label.replace(/^@/, "")), h("span", { "aria-hidden": "true" }, "•••")));
      const media = m && m.url ? (m.kind === "video" || m.role === "video" ? h("video", { src: m.url, controls: true, playsinline: true }) : h("img", { src: m.preview_url||m.url, alt: m.alt || "", decoding: "async" })) : h("p", { class: "ig-empty" }, "No media in this draft yet");
      screen.append(h("div", { class: "ig-post-media" }, media));
      if (d.media.length > 1) screen.append(h("div", { class: "ig-carousel-controls" },
        h("button", { type: "button", disabled: slide === 0, "aria-label": "Previous slide", onclick: () => { slide--; draw(); } }, "‹"),
        h("span", {}, `${slide + 1} / ${d.media.length}`), h("button", { type: "button", disabled: slide === d.media.length - 1, "aria-label": "Next slide", onclick: () => { slide++; draw(); } }, "›")));
      screen.append(h("div", { class: "ig-post-glyphs", "aria-hidden": "true" }, h("span", {}, icon("heart"), icon("comment"), icon("send")), h("span", {}, icon("bookmark"))),
        h("p", { class: "ig-post-caption" }, h("strong", {}, label.replace(/^@/, "") + " "), cap));
    }
    screen.append(h("div", { class: "device-home", "aria-hidden": "true" }));
    const aside = h("aside", { class: "phone-notes" }, h("h2", {}, "A visual check before you publish"),
      h("p", {}, data.read_only ? "This is a read-only sample. Profile crop and app controls are approximate." : "This is the visual order saved in your feed planner. Publishing dates stay unchanged. Profile crop and app controls are approximate."),
      h("p", {}, "You can finish in Instagram yourself, or use the existing approval and scheduling workflow."));
    if (detail) {
      aside.append(...[h("h3", {}, (detail.post.idea || {}).text || "Selected post"),
        h("p", {}, `${detail.media.length} media ${detail.media.length === 1 ? "file" : "files"}`),
        detail.firstComment ? h("details", {}, h("summary", {}, "First comment"), h("p", {}, detail.firstComment)) : null,
        h("button", { type: "button", class: "primary", onclick: () => openManualExport(ctx, detail.post, detail.variant) }, "Export files and caption"),
        h("a", { class: "btn", href: `#/post/${encodeURIComponent(detail.post.id)}${detail.variant ? "?variant=" + encodeURIComponent(detail.variant.id) : ""}` }, "Edit this post")].filter(Boolean));
    }
    clear(body).append(h("div", { class: "device-shell", "aria-label": "Phone preview frame" }, screen), aside);
  }
  await load();
  return { refresh: load, unmount() { alive = false; serial++; } };
}
