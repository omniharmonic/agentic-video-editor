// Threads: 500 characters, media shown as a swipe row.
import { h } from "../panels.js";
import { captionEl, captionStats, cardShell, ghostActions, head, mediaEl, platformCfg } from "./common.js";

export const label = "Threads";

export function render(model, cfg) {
  const p = platformCfg(cfg, "threads");
  const stats = captionStats(model, p);
  const media = model.media || [];
  const row = media.length
    ? h("div.pv-row", { tabindex: "0", role: "group", "aria-label": "Attached images, scroll sideways" }, media.map((m) => h("div.pv-rowcell", mediaEl(m))))
    : null;
  const body = h("div.pv-body", head(model, { platformLabel: label }), captionEl(stats, model), row, ghostActions(4));
  return cardShell(model, cfg, "threads", label, body, stats);
}
