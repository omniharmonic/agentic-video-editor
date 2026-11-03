import {openMedia} from './media-workshop.js';
// Library page (spec 7.2): grid + filters, upload, asset detail, "Find B-roll" modal; plus the asset picker the editor uses for swaps.
//
//   mountLibrary(root, ctx) -> {destroy}
//   openAssetPicker(ctx, {title?, kind?, currentId?, onPick(asset)}) -> {close}      // library tab + B-roll tab
//   openBrollModal(ctx, {post?, onAdded(asset)}) -> {close}
import {
  clear, debounce, empty, errorBox, field, fmtBytes, h, localUrl, openModal, say, shortSha, storeGet, storeSet,
} from "./panels.js";

const KINDS = [["", "Any kind"], ["image", "Images"], ["video", "Video"], ["audio", "Audio"], ["document", "Documents"]];
const SOURCES = [["", "Any source"], ["own", "Own"], ["stock", "Stock"], ["render", "Rendered"], ["generated", "Generated"], ["import", "Imported"]];
const PROVIDERS = [["pexels", "Pexels"], ["pixabay", "Pixabay"], ["unsplash", "Unsplash"], ["coverr", "Coverr"], ["local", "My library"]];

function licenseBadge(a) {
  const summary = (a.license && a.license.summary) || "no licence";
  const missing = a.license_ok === false || (a.source && a.source.type === "stock" && !(a.license && a.license.entry));
  return h(`span.st-chip.lic.${missing ? "bad" : "ok"}`, { title: missing ? "No licence entry covers this file" : summary }, h("span", { "aria-hidden": "true" }, missing ? "✕" : "✓"), missing ? "No licence" : summary);
}

function thumb(a, big = false) {
  const url = localUrl(a.preview_url || a.url || (a.file ? "/files/" + a.file : ""));
  if (a.kind === "video" && url && !a.preview_url) return h("video.lib-thumb", { src: url, muted: true, preload: "metadata", "aria-label": a.alt || a.id });
  if (a.kind === "audio") return h("div.lib-thumb.lib-ph", "audio");
  if (!url) return h("div.lib-thumb.lib-ph", a.kind || "file");
  return h("img.lib-thumb", { src: url, alt: a.alt || "", loading: "lazy", decoding: "async" });
}

function assetCard(a, { onOpen, selectable = false, onPick = null }) {
  const label = `${a.alt || a.id}, ${a.kind}${a.w ? `, ${a.w} by ${a.h}` : ""}`;
  const card = h("li.lib-card", { dataset: { asset: a.id } },
    h("button.lib-open", { type: "button", "aria-label": label, onclick: () => (onPick ? onPick(a) : onOpen(a)) }, thumb(a)),
    h("div.lib-meta", licenseBadge(a),
      h("span.st-hint", [a.source && a.source.type, a.w ? `${a.w}×${a.h}` : null, (a.usage || []).length ? `used ${(a.usage || []).length}×` : "unused"].filter(Boolean).join(" · "))));
  return card;
}

async function fetchLibrary(ctx, f) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(f)) if (v) p.set(k, v === true ? "1" : v);
  const res = await ctx.api.get(`library${p.toString() ? "?" + p : ""}`);
  const assets = res.assets || [];
  assets.total = Number.isInteger(res.total) ? res.total : assets.length;      // how many match, which can be more than came back
  return assets;
}
const countText = (assets) => (assets.total > assets.length ? `Showing ${assets.length} of ${assets.total} assets. Narrow the search to see the rest.` : `${assets.length} asset${assets.length === 1 ? "" : "s"}`);

function filterBar(state, onChange, { withUpload = null } = {}) {
  const q = h("input", { type: "search", name: "q", placeholder: "Search tags, alt text, source", value: state.q || "", "aria-label": "Search the library" });
  const kind = h("select", { name: "kind", "aria-label": "Kind" }, KINDS.map(([k, l]) => h("option", { value: k, selected: state.kind === k }, l)));
  const source = h("select", { name: "source", "aria-label": "Source" }, SOURCES.map(([k, l]) => h("option", { value: k, selected: state.source === k }, l)));
  const lic = h("select", { name: "license", "aria-label": "Licence" }, [["", "Any licence state"], ["ok", "Licence covered"], ["missing", "Licence missing"]].map(([k, l]) => h("option", { value: k, selected: state.license === k }, l)));
  const tag = h("input", { type: "text", name: "tag", placeholder: "Tag", value: state.tag || "", "aria-label": "Tag", size: 10 });
  const unused = h("input", { type: "checkbox", name: "unused", checked: !!state.unused });
  const fire = debounce(() => { Object.assign(state, { q: q.value.trim(), kind: kind.value, source: source.value, license: lic.value, tag: tag.value.trim(), unused: unused.checked }); onChange(); }, 250);
  [q, tag].forEach((e) => e.addEventListener("input", fire));
  [kind, source, lic, unused].forEach((e) => e.addEventListener("change", () => fire.flush()));
  return h("form.lib-filters", { role: "search", onsubmit: (e) => { e.preventDefault(); fire.flush(); } }, q, kind, source, lic, tag, h("label.st-check", unused, "Unused only"), withUpload);
}

// ---- upload ----------------------------------------------------------------------------------------------------------------------------
async function uploadFiles(ctx, files, status, after) {
  for (const file of files) {
    status.textContent = `Uploading ${file.name}…`;
    try {
      const res = await ctx.api.put(`library/upload?name=${encodeURIComponent(file.name)}`, file, { headers: { "Content-Type": file.type || "application/octet-stream" } });
      const a = res.asset;
      say(ctx, res.duplicate ? `${file.name} is already in the library as ${a.id}.` : `Added ${file.name} (${a.id}). Add alt text and a licence note before using it.`);
      if (after) after(a, res);
    } catch (ex) {
      status.textContent = "";
      say(ctx, `Could not add ${file.name}: ${ex.message}`, { error: true });
    }
  }
  status.textContent = "";
}

function uploader(ctx, after) {
  const status = h("span.st-hint", { role: "status" });
  const input = h("input.st-sr", { type: "file", multiple: true, id: "lib-file", "aria-label": "Add files to the library", accept: "image/*,video/*,audio/*,application/pdf" });
  input.addEventListener("change", () => { if (input.files.length) uploadFiles(ctx, Array.from(input.files), status, after).then(() => { input.value = ""; }); });
  if (window.aveNative?.importLibrary) {       // the app's web view cannot show a file picker
    const add = h("button.st-btn.primary", { type: "button", onclick: async () => {
      try {
        status.textContent = "Adding files…";
        const res = await window.aveNative.importLibrary();
        for (const row of res.added) {
          say(ctx, row.duplicate ? `${row.name} is already in the library as ${row.asset.id}.` : `Added ${row.name} (${row.asset.id}). Add alt text and a licence note before using it.`);
          if (after) after(row.asset, row);
        }
      } catch (ex) { say(ctx, `Could not add files: ${ex.message}`, { error: true }); }
      status.textContent = "";
    } }, "Add files");
    return { el: h("span.lib-upload", add, status), input, status };
  }
  const btn = h("label.st-btn.primary", { for: "lib-file", tabindex: "0", role: "button", onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } } }, "Add files");
  return { el: h("span.lib-upload", btn, input, status), input, status };
}

// ---- page ----------------------------------------------------------------------------------------------------------------------------
export function mountLibrary(root, ctx) {
  root.classList.add("st");
  const state = { q: "", kind: storeGet("studio.library.kind", ""), source: "", license: "", tag: "", unused: false };
  const grid = h("ul.lib-grid", { "aria-label": "Library assets" });
  const count = h("p.st-hint", { role: "status" });
  const detail = h("aside.lib-detail", { "aria-label": "Asset detail", hidden: true });
  let alive = true;
  let reqSeq = 0;

  const up = uploader(ctx, () => load());
  const zone = h("div.lib-drop", { tabindex: "-1" }, "Drop files here to add them");
  const bar = filterBar(state, () => { storeSet("studio.library.kind", state.kind); load(); }, { withUpload: h("span.lib-actions", up.el, h("button", { type: "button", onclick: () => openBrollModal(ctx, { onAdded: () => load() }) }, "Find B-roll")) });
  clear(root).append(h("section.st-page.lib", h("header.st-pagehead", h("h1", "Library"), h("p.st-hint", "Everything that can go into a post. Each file has a hash, a source and a licence note.")), bar, count, h("div.lib-body", h("div.lib-main", grid, zone), detail)));

  ["dragenter", "dragover"].forEach((ev) => root.addEventListener(ev, (e) => { if (e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files")) { e.preventDefault(); root.classList.add("dropping"); } }));
  ["dragleave", "drop"].forEach((ev) => root.addEventListener(ev, (e) => { if (ev === "dragleave" && root.contains(e.relatedTarget)) return; root.classList.remove("dropping"); }));
  root.addEventListener("drop", (e) => { if (e.dataTransfer && e.dataTransfer.files.length) { e.preventDefault(); uploadFiles(ctx, Array.from(e.dataTransfer.files), up.status, () => load()); } });

  async function load() {
    const seq = ++reqSeq;
    try {
      const assets = await fetchLibrary(ctx, state);
      if (!alive || seq !== reqSeq) return;
      clear(grid);
      if (!assets.length) { grid.append(h("li.lib-none", empty("Nothing matches.", "Clear the filters, or add files with Add files."))); count.textContent = "0 assets"; return; }
      assets.forEach((a) => grid.append(assetCard(a, { onOpen: showDetail })));
      count.textContent = countText(assets);
    } catch (ex) { clear(grid).append(h("li", errorBox(ex, load))); }
  }

  async function showDetail(a) {
    detail.hidden = false;
    clear(detail).append(h("p.st-hint", "Loading…"));
    try {
      const res = await ctx.api.get(`library/${a.id}`);
      const asset = res.asset;
      const lic = res.license || {};
      clear(detail).append(
        h("header", h("h2", asset.id), h("button.ghost", { type: "button", "aria-label": "Close detail", onclick: () => { detail.hidden = true; } }, "×")),
        h("div.lib-big", thumb({ ...asset, url: res.url, preview_url:res.preview_url })),
        asset.kind==='image'?h("button",{type:"button",onclick:()=>openMedia(ctx,{asset:asset.id})},"Edit image / design"):null,
        h("dl.st-dl",
          h("dt", "Kind"), h("dd", `${asset.kind}${asset.w ? `, ${asset.w}×${asset.h}` : ""}, ${fmtBytes(asset.bytes)}`),
          h("dt", "Hash"), h("dd.mono", asset.sha256 ? shortSha(asset.sha256, 16) + "…" : ""),
          h("dt", "Source"), h("dd", [asset.source.type, asset.source.provider, asset.source.url ? h("a", { href: asset.source.url, rel: "noopener noreferrer", target: "_blank" }, "page") : null].filter(Boolean).map((x, i) => (i ? [" · ", x] : x))),
          h("dt", "Licence"), h("dd", lic.covered ? `Covered: ${(lic.entries || []).map((e) => e.summary).join(", ") || asset.license.summary}` : h("span.bad", `Not covered${lic.missing && lic.missing.length ? ": " + lic.missing.join(", ") : ""}`)),
          asset.license && asset.license.attribution && asset.license.attribution.required ? [h("dt", "Attribution"), h("dd", asset.license.attribution.text)] : null,
          h("dt", "Alt text"), h("dd", asset.alt || h("span.st-hint", "none yet")),
          h("dt", "Tags"), h("dd", (asset.tags || []).join(", ") || h("span.st-hint", "none")),
          asset.ai_generated ? [h("dt", "AI"), h("dd", "Generated: posts using it carry an AI disclosure")] : null,
          asset.people && asset.people.faces ? [h("dt", "People"), h("dd", `${asset.people.faces} face(s), release ${asset.people.release || "not on file"}`)] : null,
          asset.derived_from && asset.derived_from.length ? [h("dt", "Made from"), h("dd", asset.derived_from.join(", "))] : null,
          h("dt", "Used in"), h("dd", (res.usage || []).length ? (res.usage || []).map((u) => h("a.st-block", { href: `#/post/${u.post}?variant=${u.variant}` }, `${u.post} / ${u.variant}`)) : h("span.st-hint", "not used yet"))));
    } catch (ex) { clear(detail).append(errorBox(ex)); }
  }
  load();
  return { destroy() { alive = false; }, reload: load };
}

// ---- picker (swap) -------------------------------------------------------------------------------------------------------------------
export function openAssetPicker(ctx, { title = "Choose from the library", kind = "", currentId = null, allowDraft = false, onPick }) {
  const state = { q: "", kind, source: "", license: "", tag: "", unused: false };
  const grid = h("ul.lib-grid.pick", { "aria-label": "Library assets" });
  const live = h("p.st-hint", { role: "status" });
  const body = h("div.lib-picker");
  let seq = 0;
  const bar = filterBar(state, load, {});
  const brollBtn = h("button", { type: "button", onclick: () => { modal.close("broll"); openBrollModal(ctx, { onAdded: (a) => { onPick(a); } }); } }, "Find B-roll instead");
  body.append(bar, live, grid, h("p", brollBtn));
  const modal = openModal({ title, body, wide: true, actions: [h("button", { type: "button", onclick: () => modal.close("cancel") }, "Cancel")] });
  async function load() {
    const my = ++seq;
    try {
      const assets = await fetchLibrary(ctx, state);
      if (my !== seq) return;
      clear(grid);
      assets.forEach((a) => {
        const blocked = a.license_ok === false && !allowDraft;
        const card = assetCard(a, { onOpen: () => {}, onPick: blocked ? () => say(ctx, "This file has no licence entry, so it cannot go in a post. Log a licence first.", { error: true }) : (x) => { modal.close("picked"); onPick(x); } });
        if (a.id === currentId) card.classList.add("current");
        if (blocked) card.classList.add("blocked");
        grid.append(card);
      });
      live.textContent = countText(assets);
      if (!assets.length) grid.append(h("li.lib-none", empty("Nothing matches.")));
    } catch (ex) { clear(grid).append(h("li", errorBox(ex, load))); }
  }
  load();
  return modal;
}

// ---- B-roll ---------------------------------------------------------------------------------------------------------------------------
function candidateCard(c, { onPick, busy }) {
  const thumbUrl = localUrl(c.thumb || c.thumb_url || "");
  const critic = c.critic || {};
  const problems = (critic.problems || []).map((p) => (typeof p === "string" ? p : p.message || p.kind || JSON.stringify(p)));
  const flagged = critic.verdict && critic.verdict !== "ok";
  const key = `${c.provider}:${c.asset_id}`;
  return h("li.lib-card.cand", { dataset: { cand: key } },
    thumbUrl ? h("img.lib-thumb", { src: thumbUrl, alt: c.title || "", loading: "lazy" })
      : h("div.lib-thumb.lib-ph", { title: "The preview is fetched by the server; none is cached yet." }, c.kind === "video" ? "video" : "photo", h("span", `${c.w}×${c.h}`)),
    h("div.lib-meta",
      h("span.st-chip.lic.ok", h("span", { "aria-hidden": "true" }, "✓"), `${c.provider[0].toUpperCase() + c.provider.slice(1)} licence`),
      c.attribution_required ? h("span.st-chip.warn", h("span", { "aria-hidden": "true" }, "!"), "Attribution required") : h("span.st-chip", "No attribution needed"),
      h("p.cand-by", `${c.title || "Untitled"} by ${c.author || "unknown"}`),
      c.attribution_text ? h("p.st-hint", `Credit line: ${c.attribution_text}`) : null,
      flagged ? h("p.st-alert.warn", { role: "note" }, `Authenticity check: ${critic.verdict}${problems.length ? ", " + problems.join("; ") : ""}`) : h("p.st-hint", "Authenticity check: ok"),
      h("p.st-hint", [c.duration ? `${Math.round(c.duration)}s` : null, `${c.w}×${c.h}`].filter(Boolean).join(" · ")),
      c.page_url ? h("a.st-hint", { href: c.page_url, target: "_blank", rel: "noopener noreferrer" }, "View on the provider's site") : null,
      h("button.primary", { type: "button", disabled: busy, onclick: () => onPick(c, key) }, "Download and add")));
}

export function openBrollModal(ctx, { post = null, query = "", onAdded = null } = {}) {
  const qEl = h("input", { type: "search", name: "query", value: query, placeholder: "bread crust, morning light…", "aria-label": "What to look for" });
  const kind = h("select", { name: "kind", "aria-label": "Kind" }, [["photo", "Photos"], ["video", "Video"]].map(([k, l]) => h("option", { value: k }, l)));
  const providers = PROVIDERS.map(([k, l]) => h("label.st-check", h("input", { type: "checkbox", name: "prov", value: k, checked: k === "pexels" || k === "local" }), l));
  const byEl = h("input", { type: "text", name: "by", value: ctx.by ? ctx.by() : "", placeholder: "Your name", "aria-label": "Who is approving the download", size: 12, autocomplete: "name" });
  const results = h("ul.lib-grid.cands", { "aria-label": "Search results" });
  const status = h("p.st-hint", { role: "status" });
  const form = h("form.lib-filters", { role: "search" }, qEl, kind, h("fieldset.st-inline", h("legend.st-sr", "Where to search"), providers), h("button.primary", { type: "submit" }, "Search"));
  const body = h("div.lib-picker", form,
    h("p.st-hint", "Nothing is downloaded until you pick one. Your name is logged as the person who approved the download."),
    h("div.st-row", h("label", { for: "broll-by" }, "Approved by"), byEl), status, results);
  byEl.id = "broll-by";
  const modal = openModal({ title: "Find B-roll", body, wide: true, actions: [h("button", { type: "button", onclick: () => modal.close("cancel") }, "Close")] });
  let search = null;

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!qEl.value.trim()) { status.textContent = "Type what you are looking for."; qEl.focus(); return; }
    status.textContent = "Searching…";
    clear(results);
    try {
      const res = await ctx.api.post("broll/search", { post: post ? post.id || post : null, query: qEl.value.trim(), providers: providers.map((l) => l.querySelector("input")).filter((i) => i.checked).map((i) => i.value), kind: kind.value });
      search = res.search || res;
      const cands = search.candidates || [];
      status.textContent = cands.length ? `${cands.length} result${cands.length === 1 ? "" : "s"}. Pick one to download it.` : "No results. Try other words or another provider.";
      cands.forEach((c) => results.append(candidateCard(c, { onPick: pick })));
    } catch (ex) { status.textContent = ""; results.append(h("li", errorBox(ex))); }
  });

  async function pick(c, key) {
    const by = byEl.value.trim();
    if (!by) { status.textContent = "Add your name first: a named person approves every download."; byEl.focus(); return; }
    status.textContent = `Downloading ${key}…`;
    try {
      const res = await ctx.api.post("broll/pick", { search: search.id, candidate: key, by });
      say(ctx, `Added ${res.asset.id} to the library with licence entry ${shortSha(res.license_entry, 8)}.`);
      status.textContent = `Added ${res.asset.id}. Licence entry ${shortSha(res.license_entry, 8)} logged.`;
      if (onAdded) { modal.close("added"); onAdded(res.asset, res); }
    } catch (ex) {
      status.textContent = "";
      say(ctx, `Could not download: ${ex.message}`, { error: true });
    }
  }
  return modal;
}
