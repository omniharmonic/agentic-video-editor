import {openMedia} from './media-workshop.js';
// The studio library's asset picker and "Find B-roll" modal, for the tools kept under More tools (the post editor, the feed planner,
// Media tools). The library itself is listed in the Workspace's Sources view.
//
//   openAssetPicker(ctx, {title?, kind?, currentId?, onPick(asset)}) -> {close}      // library tab + B-roll tab
//   openBrollModal(ctx, {post?, onAdded(asset)}) -> {close}
import {
  clear, debounce, empty, errorBox, field, fmtBytes, h, localUrl, openModal, say, shortSha, storeGet, storeSet,
} from "/app/shared/panels.js";
import { brollPick, brollSearch, candidateFacts, countText, fetchLibrary as fetchAssets, importToLibrary, KINDS, LICENCES, licenceState, NEED_NAME, PROVIDERS, SOURCES, thumbUrl } from "/app/shared/library-core.js";


function licenseBadge(a) {
  const l = licenceState(a);
  return h(`span.st-chip.lic.${l.missing ? "bad" : "ok"}`, { title: l.title }, h("span", { "aria-hidden": "true" }, l.missing ? "✕" : "✓"), l.text);
}

function thumb(a, big = false) {
  const url = thumbUrl(a);
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

const fetchLibrary = (ctx, f) => fetchAssets(ctx.api, f);

function filterBar(state, onChange, { withUpload = null } = {}) {
  const q = h("input", { type: "search", name: "q", placeholder: "Search tags, alt text, source", value: state.q || "", "aria-label": "Search the library" });
  const kind = h("select", { name: "kind", "aria-label": "Kind" }, KINDS.map(([k, l]) => h("option", { value: k, selected: state.kind === k }, l)));
  const source = h("select", { name: "source", "aria-label": "Source" }, SOURCES.map(([k, l]) => h("option", { value: k, selected: state.source === k }, l)));
  const lic = h("select", { name: "license", "aria-label": "Licence" }, LICENCES.map(([k, l]) => h("option", { value: k, selected: state.license === k }, l)));
  const tag = h("input", { type: "text", name: "tag", placeholder: "Tag", value: state.tag || "", "aria-label": "Tag", size: 10 });
  const unused = h("input", { type: "checkbox", name: "unused", checked: !!state.unused });
  const fire = debounce(() => { Object.assign(state, { q: q.value.trim(), kind: kind.value, source: source.value, license: lic.value, tag: tag.value.trim(), unused: unused.checked }); onChange(); }, 250);
  [q, tag].forEach((e) => e.addEventListener("input", fire));
  [kind, source, lic, unused].forEach((e) => e.addEventListener("change", () => fire.flush()));
  return h("form.lib-filters", { role: "search", onsubmit: (e) => { e.preventDefault(); fire.flush(); } }, q, kind, source, lic, tag, h("label.st-check", unused, "Unused only"), withUpload);
}

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
  const f = candidateFacts(c);
  return h("li.lib-card.cand", { dataset: { cand: f.key } },
    f.thumb ? h("img.lib-thumb", { src: f.thumb, alt: c.title || "", loading: "lazy" })
      : h("div.lib-thumb.lib-ph", { title: "The preview is fetched by the server; none is cached yet." }, c.kind === "video" ? "video" : "photo", h("span", `${c.w}×${c.h}`)),
    h("div.lib-meta",
      h("span.st-chip.lic.ok", h("span", { "aria-hidden": "true" }, "✓"), f.licence),
      c.attribution_required ? h("span.st-chip.warn", h("span", { "aria-hidden": "true" }, "!"), f.credit) : h("span.st-chip", f.credit),
      h("p.cand-by", f.by),
      f.creditLine ? h("p.st-hint", f.creditLine) : null,
      f.flagged ? h("p.st-alert.warn", { role: "note" }, f.check) : h("p.st-hint", f.check),
      h("p.st-hint", f.size),
      c.page_url ? h("a.st-hint", { href: c.page_url, target: "_blank", rel: "noopener noreferrer" }, "View on the provider's site") : null,
      h("button.primary", { type: "button", disabled: busy, onclick: () => onPick(c, f.key) }, "Download and add")));
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
      const res = await brollSearch(ctx.api, { post: post ? post.id || post : null, query: qEl.value.trim(), providers: providers.map((l) => l.querySelector("input")).filter((i) => i.checked).map((i) => i.value), kind: kind.value });
      search = res.search || res;
      const cands = search.candidates || [];
      status.textContent = cands.length ? `${cands.length} result${cands.length === 1 ? "" : "s"}. Pick one to download it.` : "No results. Try other words or another provider.";
      cands.forEach((c) => results.append(candidateCard(c, { onPick: pick })));
    } catch (ex) { status.textContent = ""; results.append(h("li", errorBox(ex))); }
  });

  async function pick(c, key) {
    const by = byEl.value.trim();
    if (!by) { status.textContent = NEED_NAME; byEl.focus(); return; }
    status.textContent = `Downloading ${key}…`;
    try {
      const res = await brollPick(ctx.api, search.id, key, by);
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
