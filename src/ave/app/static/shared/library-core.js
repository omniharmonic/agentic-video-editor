// Library logic shared by the old Library screen (library.js) and the Workspace's Sources view (app/static/workspace/sources.js).
// No page chrome here: the filter choices, what a licence state means, the listing request and its count, the picture to show
// for an asset, and adding files through the app's own panel. Both screens call these, so a fix lands in both.
import { localUrl } from "./panels.js";

export const KINDS = [["", "Any kind"], ["image", "Images"], ["video", "Video"], ["audio", "Audio"], ["document", "Documents"]];
export const SOURCES = [["", "Any source"], ["own", "Own"], ["stock", "Stock"], ["render", "Rendered"], ["generated", "Generated"], ["import", "Imported"]];
export const LICENCES = [["", "Any licence state"], ["ok", "Licence covered"], ["missing", "Licence missing"]];
export const SOURCE_WORDS = { own: "Your own", stock: "Stock", render: "Rendered here", generated: "Generated", import: "Imported" };

/** {missing, text, title}: whether a licence entry covers the file, in the words both screens show. */
export function licenceState(a) {
  const summary = (a.license && a.license.summary) || "no licence";
  const missing = a.license_ok === false || (a.source && a.source.type === "stock" && !(a.license && a.license.entry));
  return { missing, text: missing ? "No licence" : summary, title: missing ? "No licence entry covers this file" : summary };
}

/** The listing request for a filter state {q, kind, source, license, tag, unused}. */
export function libraryPath(f) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(f || {})) if (v) p.set(k, v === true ? "1" : v);
  return `library${p.toString() ? "?" + p : ""}`;
}
/** The assets, with `.total` set to how many match (which can be more than came back). */
export async function fetchLibrary(api, f) {
  const res = await api.get(libraryPath(f));
  const assets = res.assets || [];
  assets.total = Number.isInteger(res.total) ? res.total : assets.length;
  return assets;
}
export const countText = (assets, total = assets.total) => (total > assets.length ? `Showing ${assets.length} of ${total} assets. Narrow the search to see the rest.` : `${assets.length} asset${assets.length === 1 ? "" : "s"}`);

/** The URL of a small picture of the asset ("" when there is none to show). */
export const thumbUrl = (a) => localUrl(a.preview_url || a.url || (a.file ? "/files/" + a.file : ""));

/** Used in N posts, in words. */
export const usageText = (a) => ((a.usage || []).length ? `Used ${(a.usage || []).length}×` : "Unused");

/** Add files through the app's own panel: the web view cannot show a file picker, so files always come through
 *  aveNative.importLibrary. Returns the added rows ({name, asset, duplicate}); null when there is no app to ask. */
export async function importToLibrary() {
  if (!window.aveNative?.importLibrary) return null;
  const r = await window.aveNative.importLibrary();
  return (r && r.added) || [];
}
export const NO_APP = "Adding files needs the AVE Studio app: a browser cannot hand files to it.";

// ---------------------------------------------------------------- B-roll search (POST broll/search, then a named pick)
export const PROVIDERS = [["pexels", "Pexels"], ["pixabay", "Pixabay"], ["unsplash", "Unsplash"], ["coverr", "Coverr"], ["local", "My library"]];
export const DEFAULT_PROVIDERS = ["pexels", "local"];
/** What a candidate says about itself: its licence, credit, who made it and the authenticity check. The provider's preview URL is
 *  never handed to the browser: only the server's cached thumbnail (`thumb`) is shown. */
export function candidateFacts(c) {
  const critic = c.critic || {};
  const problems = (critic.problems || []).map((p) => (typeof p === "string" ? p : p.message || p.kind || JSON.stringify(p)));
  const flagged = !!critic.verdict && critic.verdict !== "ok";
  return {
    key: `${c.provider}:${c.asset_id}`,
    licence: `${c.provider[0].toUpperCase() + c.provider.slice(1)} licence`,
    credit: c.attribution_required ? "Attribution required" : "No attribution needed",
    creditLine: c.attribution_text ? `Credit line: ${c.attribution_text}` : "",
    by: `${c.title || "Untitled"} by ${c.author || "unknown"}`,
    flagged, check: flagged ? `Authenticity check: ${critic.verdict}${problems.length ? ", " + problems.join("; ") : ""}` : "Authenticity check: ok",
    size: [c.duration ? `${Math.round(c.duration)}s` : null, `${c.w}×${c.h}`].filter(Boolean).join(" · "),
    thumb: localUrl(c.thumb || c.thumb_url || ""),
  };
}
export const brollSearch = (api, { query, providers, kind, post = null }) => api.post("broll/search", { post, query, providers, kind });
/** A named person approves every download: no name, no request. */
export const NEED_NAME = "Add your name first: a named person approves every download.";
// In the app a pick is a protected act: the app checks it is the device owner for exactly this result (aveNative.pickBroll).
// Outside the app (`ave studio ui`, started by a person at a terminal) the studio route takes it directly, as before.
export const brollPick = (api, search, key, by) => (window.aveNative?.pickBroll ? window.aveNative.pickBroll(search, key, by)
  : api.post("broll/pick", { search, candidate: key, by }));
