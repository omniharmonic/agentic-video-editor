// The planner and phone view use the same media and surface rules.
import { h, icon, thumbImg } from "/app/shared/dom.js";

export function onSurface(cell, surface = "feed") {
  const type = cell.post_type || "feed";
  if (surface === "all") return true;
  if (surface === "reel") return type === "reel" || type === "reels";
  if (surface === "story") return type === "story" || type === "stories";
  return !["story", "stories"].includes(type) && (!["reel","reels"].includes(type)||cell.feed_presence===true);
}

export function feedMedia(cell, alt = "") {
  const missing = () => h("span", { class: "planner-placeholder" }, icon(cell.media_kind === "video" ? "reels" : "plus"),
    h("span", {}, cell.title), h("small", {}, cell.media_url ? "Preview unavailable · open post" : "Add media"));
  const media = cell.thumb ? thumbImg(cell.thumb, alt) : cell.media_kind === "video" && cell.media_url ? h("video", {
    src: cell.media_url + "#t=0.1", muted: true, playsinline: true, preload: "metadata", draggable: "false", "aria-label": alt || "Video preview",
  }) : missing();
  if(cell.crop)media.style.objectPosition=`${cell.crop.x*100}% ${cell.crop.y*100}%`;
  media.addEventListener("error", () => media.replaceWith(missing()), { once: true });
  return media;
}
