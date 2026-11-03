// LinkedIn: ~3 lines before "see more" on mobile, 1.91:1 / 1:1 / 4:5 images, PDF documents for carousels.
import { h } from "../panels.js";
import { aspectOf, captionEl, captionStats, cardShell, ghostActions, head, mediaEl, platformCfg } from "./common.js";

export const label = "LinkedIn";

export function render(model, cfg) {
  const p = platformCfg(cfg, "linkedin");
  const stats = captionStats(model, p);
  const media = model.media || [];
  let mediaBlock = null;
  if ((model.postType || "") === "document") {
    mediaBlock = h("div.pv-doc", h("div.pv-doc-page", media[0] ? mediaEl(media[0]) : h("div.pv-media-ph", "page 1")),
      h("p.pv-hint", `Document, ${media.length || 1} page${media.length === 1 ? "" : "s"}`));
  } else if (media.length) {
    const a = Math.min(1.91, Math.max(0.8, aspectOf(media[0])));
    mediaBlock = h("div.pv-feedmedia.wide", { style: { aspectRatio: String(a) } }, mediaEl(media[0]));
  }
  const body = h("div.pv-body", head(model, { platformLabel: label }), captionEl(stats, model, { moreLabel: "see more" }), mediaBlock, ghostActions(4));
  return cardShell(model, cfg, "linkedin", label, body, stats);
}
