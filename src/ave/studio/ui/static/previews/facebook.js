// Facebook Page post: timeline card, text cut after ~3 lines, full-width media.
import { h } from "../panels.js";
import { captionEl, captionStats, cardShell, ghostActions, head, imageGrid, mediaEl, platformCfg, safeFrame } from "./common.js";

export const label = "Facebook";

export function render(model, cfg) {
  const p = platformCfg(cfg, "facebook");
  const stats = captionStats(model, p);
  const media = model.media || [];
  const type = model.postType || "feed";
  let mediaBlock = null;
  if (type === "reel") mediaBlock = safeFrame(media[0], ((p.surfaces || {}).reel || {}).safe, null, "caption and buttons");
  else if (media.length > 1) mediaBlock = imageGrid(media, { max: 4, ratio: "1 / 1" });
  else if (media.length === 1) mediaBlock = h("div.pv-feedmedia.wide", mediaEl(media[0]));
  const body = h("div.pv-body", head(model, { platformLabel: label }), captionEl(stats, model, { moreLabel: "See more" }), mediaBlock, ghostActions(3));
  return cardShell(model, cfg, "facebook", label, body, stats);
}
