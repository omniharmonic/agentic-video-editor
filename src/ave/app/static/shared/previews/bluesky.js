// Bluesky: 300 graphemes (emoji sequences count once), up to 4 images, alt text expected.
import { h } from "../panels.js";
import { captionEl, captionStats, cardShell, ghostActions, head, imageGrid, platformCfg } from "./common.js";

export const label = "Bluesky";

export function render(model, cfg) {
  const p = platformCfg(cfg, "bluesky");
  const stats = captionStats(model, p);
  const media = model.media || [];
  const noAlt = media.filter((m) => !m.alt || !String(m.alt).trim()).length;
  const body = h("div.pv-body", head(model, { platformLabel: label }), captionEl(stats, model),
    media.length ? imageGrid(media, { max: 4, ratio: "1 / 1" }) : null,
    noAlt ? h("p.pv-warnchip", { role: "note" }, `! ${noAlt} image${noAlt > 1 ? "s" : ""} without alt text (required for Bluesky posts here)`) : null,
    ghostActions(4));
  return cardShell(model, cfg, "bluesky", label, body, stats);
}
