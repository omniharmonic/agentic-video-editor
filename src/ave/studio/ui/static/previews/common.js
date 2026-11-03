// Shared parts of the per-platform preview cards (spec 7.4). A preview never alters data and always says it is approximate.
import { h, localUrl } from "../panels.js";
import { counter, visibleFold } from "../text_metrics.js";

const LINK_IN_BODY = new Set(["x", "bluesky", "threads", "linkedin", "facebook"]);
const UNIT_LABEL = { chars: "characters", graphemes: "characters", x_weighted: "weighted characters", bytes: "bytes" };

let cfgPromise = null;
/** config/previews.yaml as JSON (static/previews/previews.json is its generated mirror; a test keeps them equal). */
export function loadPreviewConfig() {
  if (!cfgPromise) {
    cfgPromise = fetch(new URL("./previews.json", import.meta.url), { credentials: "same-origin" })
      .then((r) => { if (!r.ok) throw new Error(`previews.json ${r.status}`); return r.json(); })
      .catch((e) => { cfgPromise = null; throw e; });
  }
  return cfgPromise;
}
export const resetPreviewConfig = () => { cfgPromise = null; };

export function platformCfg(cfg, platform) {
  return (cfg.platforms || {})[platform] || null;
}

/** The text a reader will see as the post body: caption, hashtags not already in it, and the link where the platform shows it inline. */
export function composeText(model) {
  let text = model.caption || "";
  const tags = (model.hashtags || []).map((t) => "#" + String(t).replace(/^#/, "")).filter((t) => !text.includes(t));
  if (tags.length) text += (text ? "\n\n" : "") + tags.join(" ");
  if (model.link && LINK_IN_BODY.has(model.platform) && !text.includes(model.link)) text += (text ? "\n\n" : "") + model.link;
  return text;
}

export function captionStats(model, pcfg) {
  const text = composeText(model);
  const unit = (pcfg && pcfg.counter && pcfg.counter.unit) || "chars";
  const limit = (pcfg && pcfg.counter && pcfg.counter.soft_limit) || null;
  const used = counter(text, unit);
  const fold = pcfg && pcfg.fold ? visibleFold(text, pcfg.fold) : { visible: text, hidden: "", folded: false };
  const state = limit === null ? "ok" : used > limit ? "over" : used >= limit * 0.9 ? "near" : "ok";
  return { text, unit, limit, used, over: limit === null ? 0 : Math.max(0, used - limit), state, fold, hardLimit: model.hardLimit || null };
}

export function counterEl(stats) {
  const pct = stats.limit ? Math.min(100, Math.round((stats.used / stats.limit) * 100)) : 0;
  const words = stats.state === "over" ? `${stats.over} over` : stats.state === "near" ? "close to the limit" : "within the limit";
  const glyph = stats.state === "over" ? "✕" : stats.state === "near" ? "!" : "✓";
  return h(`div.pv-counter.is-${stats.state}`, { "data-state": stats.state },
    h("span.pv-meter", { "aria-hidden": "true" }, h("span", { style: { width: pct + "%" } })),
    h("output.pv-count", { "data-used": String(stats.used), "data-limit": String(stats.limit ?? "") },
      h("span.tnum", `${stats.used.toLocaleString()}${stats.limit ? " / " + stats.limit.toLocaleString() : ""}`), " ", UNIT_LABEL[stats.unit] || stats.unit),
    h("span.pv-count-state", h("span", { "aria-hidden": "true" }, glyph + " "), words));
}

/** Caption with the fold drawn: text before "more", then (optionally) a cut rule and the dimmed remainder. */
export function captionEl(stats, model, { moreLabel = "more" } = {}) {
  const f = stats.fold;
  const show = model.showHidden !== false;
  if (!f.folded) return h("p.pv-text", stats.text || h("span.pv-placeholder", "No text yet."));
  const kids = [f.visible, h("span.pv-more", { "aria-hidden": "true" }, "… " + moreLabel)];
  if (show) {
    kids.push(h("span.pv-cut", { role: "separator", "aria-label": "Readers see everything above this line before tapping more" }, h("span", "cut here")));
    kids.push(h("span.pv-hidden", f.hidden));
  }
  return h("p.pv-text.is-folded", { "data-folded": "true" }, kids);
}

export function mediaEl(m, { fit = "cover", cls = "" } = {}) {
  const url = localUrl(m && (m.kind==='image'?(m.preview_url||m.url):m.url));
  const alt = (m && m.alt) || "";
  if (!url) return h("div.pv-media-ph" + (cls ? "." + cls : ""), { role: "img", "aria-label": alt || "Media placeholder" }, "no image");
  if (m.kind === "video" || /\.(mp4|mov|webm)$/i.test(url)) return h("video.pv-media" + (cls ? "." + cls : ""), { src: url, controls: true, playsinline:true, preload: "metadata", "aria-label": alt, style: { objectFit: fit } });
  return h("img.pv-media" + (cls ? "." + cls : ""), { src: url, alt, loading: "lazy", decoding: "async", style: { objectFit: fit } });
}

export const aspectOf = (m) => (m && m.w && m.h ? m.w / m.h : 1);
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));

/** X / Bluesky style 1-4 image grid. */
export function imageGrid(media, { max = 4, ratio = "16 / 9" } = {}) {
  const items = (media || []).slice(0, max);
  if (!items.length) return null;
  const n = items.length;
  const box = h(`div.pv-grid.n${n}`, { style: { aspectRatio: n === 1 ? String(clamp(aspectOf(items[0]), 0.8, 1.91)) : ratio } });
  items.forEach((m) => box.append(h("div.pv-cell", mediaEl(m))));
  return box;
}

/** IG profile-grid crop: 3:4 cell, centred cover crop, dashed full-frame outline, focal-point dot (spec 7.4). */
export function gridCrop(m, pcfg) {
  const [cw, ch] = (pcfg.grid && pcfg.grid.cell_aspect) || [3, 4];
  const cell = cw / ch;
  const a = aspectOf(m);
  // fraction of the frame kept, per axis
  const keepX = a > cell ? cell / a : 1;
  const keepY = a < cell ? a / cell : 1;
  const lostX = Math.round((1 - keepX) * 100);
  const lostY = Math.round((1 - keepY) * 100);
  const o = m && m.crop_overlay && m.crop_overlay.lost;
  const lost = o || { left_pct: Math.round(lostX / 2), right_pct: Math.round(lostX / 2), top_pct: Math.round(lostY / 2), bottom_pct: Math.round(lostY / 2) };
  const window_ = { left: (1 - keepX) / 2 * 100, top: (1 - keepY) / 2 * 100, width: keepX * 100, height: keepY * 100 };
  const focus = m && m.crop && (m.crop.grid_focus || (m.crop.v===1?[m.crop.x,m.crop.y]:null));
  const focusOut = focus && (focus[0] * 100 < window_.left || focus[0] * 100 > window_.left + window_.width || focus[1] * 100 < window_.top || focus[1] * 100 > window_.top + window_.height);
  const frame = h("div.pv-gridframe", { style: { aspectRatio: String(a) } },
    mediaEl(m, { fit: "fill" }),
    h("div.pv-gridwin", { "data-testid": "grid-crop", style: { left: window_.left + "%", top: window_.top + "%", width: window_.width + "%", height: window_.height + "%" } }),
    focus ? h("span.pv-focus", { title: "focal point", style: { left: focus[0] * 100 + "%", top: focus[1] * 100 + "%" } }) : null);
  const note = lost.left_pct + lost.right_pct + lost.top_pct + lost.bottom_pct === 0
    ? "The grid shows the whole image."
    : `The grid crops about ${[lost.left_pct + lost.right_pct ? `${lost.left_pct + lost.right_pct}% of the width` : "", lost.top_pct + lost.bottom_pct ? `${lost.top_pct + lost.bottom_pct}% of the height` : ""].filter(Boolean).join(" and ")}. Dashed box = what shows.`;
  return h("figure.pv-gridcrop", { "data-lost": JSON.stringify(lost) },
    frame,
    h("figcaption", note, focusOut ? h("span.pv-warnchip", { role: "note" }, " ! focal point is outside the crop") : null,
      m && m.text_cut ? h("span.pv-warnchip", { role: "note" }, " ! text cut in grid") : null,
      h("span.pv-hint", " The app sets the crop; adjust it there after publishing if needed.")));
}

/** 9:16 frame with the UI-overlay safe zones from config (top, bottom, optional right rail). */
export function safeFrame(m, safe, rail, label) {
  const s = safe || { top: 0.13, bottom: 0.2, x: 0.085 };
  const fr = h("div.pv-vframe", { "data-testid": "safe-frame" },
    mediaEl(m, { fit: "cover" }),
    h("div.pv-zone.top", { style: { height: s.top * 100 + "%" } }, h("span", "app header")),
    h("div.pv-zone.bottom", { style: { height: s.bottom * 100 + "%" } }, h("span", label || "caption and buttons")),
    rail ? h("div.pv-zone.rail", { title: "buttons", style: { width: rail * 100 + "%", top: s.top * 100 + "%", bottom: s.bottom * 100 + "%" } }) : null,
    h("div.pv-safe", { style: { left: s.x * 100 + "%", right: s.x * 100 + "%", top: s.top * 100 + "%", bottom: s.bottom * 100 + "%" } }));
  return fr;
}

export function head(model, { platformLabel, time }) {
  const name = (model.account && model.account.label) || (model.account && model.account.id) || "account";
  return h("header.pv-head",
    h("span.pv-avatar", { "aria-hidden": "true" }, name.replace(/^@/, "").slice(0, 1).toUpperCase()),
    h("span.pv-who", h("strong", name), h("span.pv-sub", time || model.scheduledLabel || "scheduled")),
    h("span.pv-tag", platformLabel));
}

export function cardShell(model, cfg, key, label, body, stats) {
  const pcfg = platformCfg(cfg, key) || {};
  return h(`article.pv.pv-${key}`, { "aria-label": `${label} preview, approximate`, "data-platform": key, "data-posttype": model.postType || "" },
    body,
    h("footer.pv-foot", counterEl(stats),
      h("p.pv-approx", { title: cfg.approximate_label || "Approximate" }, h("span", { "aria-hidden": "true" }, "≈ "), "Approximate",
        pcfg.verified === false ? " (rules not yet checked against a live post)" : "")),
    (model.warnings || []).length ? h("ul.pv-warnings", model.warnings.map((w) => h("li", w))) : null);
}

export const ghostActions = (n = 4) => h("div.pv-actions", { "aria-hidden": "true" }, Array.from({ length: n }, () => h("span")));
