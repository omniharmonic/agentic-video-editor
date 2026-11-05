// Platforms without a bespoke card yet (Pinterest, YouTube long form): a plain card with the right counter and fold.
import { h } from "../panels.js";
import { captionEl, captionStats, cardShell, head, imageGrid, platformCfg } from "./common.js";

export function render(model, cfg) {
  const key = model.platform;
  const p = platformCfg(cfg, key) || { counter: { unit: "chars", soft_limit: null } };
  const stats = captionStats(model, p);
  const body = h("div.pv-body", head(model, { platformLabel: key }), captionEl(stats, model),
    (model.media || []).length ? imageGrid(model.media, { max: 1, ratio: "2 / 3" }) : null);
  return cardShell(model, cfg, key, key, body, stats);
}
