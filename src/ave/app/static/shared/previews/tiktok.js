// TikTok and YouTube Shorts share one vertical frame: UI-overlay safe zones come from config/content.yaml via previews.yaml.
import { h } from "../panels.js";
import { captionEl, captionStats, cardShell, head, platformCfg, safeFrame } from "./common.js";

export const label = "TikTok";

function vertical(key, name, surfaceKey) {
  return (model, cfg) => {
    const p = platformCfg(cfg, key);
    const stats = captionStats(model, p);
    const surface = (p.surfaces || {})[surfaceKey] || (p.surfaces || {}).video || {};
    const body = h("div.pv-body", head(model, { platformLabel: name }),
      safeFrame((model.media || [])[0], surface.safe, surface.right_rail, "caption and buttons"), captionEl(stats, model));
    return cardShell(model, cfg, key, name, body, stats);
  };
}
export const render = vertical("tiktok", "TikTok", "video");
export const renderShorts = vertical("youtube", "YouTube Shorts", "short");
