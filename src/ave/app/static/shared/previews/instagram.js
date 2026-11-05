// Instagram: feed/carousel card with the "more" cut, the 3:4 profile-grid crop, and 9:16 safe zones for stories and reels.
import { h } from "../panels.js";
import { aspectOf, captionEl, captionStats, cardShell, ghostActions, gridCrop, head, mediaEl, platformCfg, safeFrame } from "./common.js";

export const label = "Instagram";

export function render(model, cfg) {
  const p = platformCfg(cfg, "instagram");
  const stats = captionStats(model, p);
  const type = model.postType || "feed";
  const media = model.media || [];
  const surface = (p.surfaces || {})[type] || {};
  let body;
  if (type === "story" || type === "reel") {
    body = h("div.pv-body", head(model, { platformLabel: label }),
      safeFrame(media[0], surface.safe, surface.right_rail, type === "reel" ? "caption, audio, buttons" : "reply bar"),
      type === "reel" ? captionEl(stats, model) : null);
  } else {
    const first = media[0];
    const a = first ? Math.min(1.91, Math.max(0.8, aspectOf(first))) : 0.8;
    body = h("div.pv-body", head(model, { platformLabel: label }),
      h("div.pv-feedmedia", { style: { aspectRatio: String(a) } }, first ? mediaEl(first) : h("div.pv-media-ph", "no image"),
        type === "carousel" && media.length > 1 ? h("span.pv-pager", `1 / ${media.length}`) : null),
      ghostActions(4),
      captionEl(stats, model),
      first ? h("section.pv-grid-section", h("h4", "On the profile grid"), gridCrop(first, p)) : null);
  }
  return cardShell(model, cfg, "instagram", label, body, stats);
}
