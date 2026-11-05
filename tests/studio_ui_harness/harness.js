// Minimal hash router that mounts the post editor (kept under More tools) and the preview lab. WS5's shell does the same with its own api.js/dom.js; the call shapes are the contract.
import { makeCtx, clear, h } from "/app/shared/panels.js";
import { mountEditorFromHash } from "/static/editor.js";
import { buildPreview, loadPreviewConfig, previewModel, PREVIEW_PLATFORMS } from "/app/shared/previews/index.js";

const main = document.getElementById("main");
const ctx = makeCtx({ by: () => "maren" });
window.__ctx = ctx;
let current = null;

async function previews() {
  const cfg = await loadPreviewConfig();
  const res = await ctx.api.get("posts/p_h_social");
  const post = res.post;
  const wrap = h("section.st-page", h("h1", "Preview lab"), h("div.lab"));
  main.append(wrap);
  for (const v of post.variants) {
    const lib = {};
    for (const m of (v.media || post.media)) { try { const r = await ctx.api.get(`library/${m.asset}`); lib[m.asset] = { ...r.asset, url: r.url }; } catch { /* ignore */ } }
    wrap.querySelector(".lab").append(h("div", { style: { display: "inline-block", boxSizing: "border-box", verticalAlign: "top", margin: "0 0 14px 0", width: "min(100%, 400px)", maxWidth: "100%" } }, h("h2", `${v.platform} ${v.post_type}`), buildPreview(previewModel(post, v, { assets: lib }), cfg)));
  }
}

async function route() {
  if (current && current.destroy) current.destroy();
  current = null;
  clear(main);
  main.classList.add("st");
  const hash = location.hash || "#/post/p_20261102_k3f9";
  const seg = hash.split("?")[0].split("/")[1];
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("on", a.getAttribute("href").split("/")[1] === seg));
  if (seg === "post") current = mountEditorFromHash(main, ctx, hash);
  else if (seg === "previews") await previews();
  main.dataset.route = seg;
}
window.addEventListener("hashchange", route);
route();
window.__platforms = PREVIEW_PLATFORMS;
