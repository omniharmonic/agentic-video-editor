// Preview entry point. The shell and the editor import only this file.
//
//   import { loadPreviewConfig, buildPreview, previewModel } from "./previews/index.js";
//   const cfg = await loadPreviewConfig();
//   container.replaceChildren(buildPreview(model, cfg));
//
// `model` = {platform, postType, account:{id,label}, caption, hashtags[], link, media:[{url,w,h,alt,crop,crop_overlay,text_cut}],
//            scheduledLabel, showHidden, warnings[], hardLimit}. `previewModel()` builds one from a post + variant (+ the server preview).
import { effectiveCaption, effectiveHashtags, effectiveMedia, fmtWhen, localUrl } from "../panels.js";
import { loadPreviewConfig, captionStats, platformCfg, composeText, resetPreviewConfig } from "./common.js";
import * as instagram from "./instagram.js";
import * as facebook from "./facebook.js";
import * as x from "./x.js";
import * as linkedin from "./linkedin.js";
import * as bluesky from "./bluesky.js";
import * as threads from "./threads.js";
import * as tiktok from "./tiktok.js";
import * as generic from "./generic.js";

export { loadPreviewConfig, captionStats, platformCfg, composeText, resetPreviewConfig };

const RENDERERS = {
  instagram: instagram.render, facebook: facebook.render, x: x.render, linkedin: linkedin.render,
  bluesky: bluesky.render, threads: threads.render, tiktok: tiktok.render,
};
export const PREVIEW_PLATFORMS = Object.keys(RENDERERS);

export function buildPreview(model, cfg) {
  let fn = RENDERERS[model.platform];
  if (model.platform === "youtube" && (model.postType === "short" || model.postType === "reel")) fn = tiktok.renderShorts;
  return (fn || generic.render)(model, cfg);
}

/** Build a preview model from a post + variant. `assets` maps asset id -> library record (w, h, url); `server` is GET posts/<id>/preview/<variant>. */
export function previewModel(post, variant, { assets = {}, account = null, server = null, caption = null, showHidden = true } = {}) {
  const srvMedia = (server && server.media) || [];
  const media = effectiveMedia(post, variant).filter(m=>m.role!=="cover").map((m, i) => {
    const a = assets[m.asset] || {};
    const s = srvMedia.find((x) => x.asset === m.asset) || {};
    return {
      asset: m.asset, preview_url:localUrl(s.preview_url||a.preview_url||""), url: localUrl(s.url || a.url || ""), w: s.w || a.w, h: s.h || a.h, kind: a.kind === "video" ? "video" : "image",
      alt: m.alt || "", crop: m.crop, crop_overlay: s.crop_overlay, text_cut: s.text_cut, slot: m.slot ?? i,
    };
  });
  return {
    platform: variant.platform, postType: variant.post_type, account: account || { id: variant.account, label: variant.account },
    caption: caption === null ? effectiveCaption(post, variant) : caption, hashtags: effectiveHashtags(post, variant), link: post.copy && post.copy.link,
    media, showHidden, warnings: (server && server.warnings) || [],
    hardLimit: server && server.caption && server.caption.counter && server.caption.counter.limit,
    scheduledLabel: variant.schedule ? fmtWhen(variant.schedule.local, variant.schedule.tz) : "not scheduled",
  };
}
