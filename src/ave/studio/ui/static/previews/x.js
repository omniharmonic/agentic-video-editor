// X: weighted 280 counter (URLs 23, emoji and CJK 2), 1-4 image grid.
import { h } from "../panels.js";
import { captionEl, captionStats, cardShell, ghostActions, head, imageGrid, platformCfg } from "./common.js";

export const label = "X";

export function render(model, cfg) {
  const p = platformCfg(cfg, "x");
  const stats = captionStats(model, p);
  const media = model.media || [];
  const body = h("div.pv-body", head(model, { platformLabel: label }), captionEl(stats, model, { moreLabel: "Show more" }),
    media.length ? imageGrid(media, { max: 4, ratio: "16 / 9" }) : null,
    media.length > 4 ? h("p.pv-hint", `Only the first 4 of ${media.length} images fit in one post.`) : null,
    ghostActions(4));
  return cardShell(model, cfg, "x", label, body, stats);
}
