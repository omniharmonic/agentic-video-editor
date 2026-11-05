// Composition logic shared by the post editor (editor.js, the old studio screen) and the Room's Layout view (app/static/room/layout.js).
// No page chrome here: dotted-path edits, the three-way merge rule, what a field shows, the assets and preview of a post, and the
// native media import. Both screens call these, so a fix lands in both.
import { effectiveCaption, effectiveFirstComment, effectiveHashtags, effectiveMedia, findVariant, getPath } from "./panels.js";
import { previewModel } from "./previews/index.js";
import { importToLibrary } from "./library-core.js";

export const AUTOSAVE_MS = (typeof window !== "undefined" && Number(window.__aveAutosaveMs)) || 800;   // tests may hold the autosave

export function setPath(obj, path, value) {
  const parts = path.split(".");
  let cur = obj;
  for (let i = 0; i < parts.length - 1; i++) {
    const p = parts[i];
    let next = Array.isArray(cur) ? (cur.find((x) => x && x.id === p) ?? cur[Number(p)]) : cur[p];
    if (next === undefined || next === null) { next = {}; if (!Array.isArray(cur)) cur[p] = next; }
    cur = next;
  }
  const last = parts[parts.length - 1];
  if (Array.isArray(cur)) { const i = cur.findIndex((x) => x && x.id === last); cur[i >= 0 ? i : Number(last)] = value; } else cur[last] = value;
}

/** Three-way classification of one field. "same": both sides landed on the same value. "rebase": only mine changed. "conflict": both changed differently. */
export function classify(base, theirs, mine) {
  const eq = (a, b) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
  if (eq(mine, theirs)) return "same";
  if (eq(base, theirs)) return "rebase";
  return "conflict";
}

export const pathInfo = (path) => {
  let m = /^variants\.([^.]+)\.copy\.(caption|first_comment|hashtags)$/.exec(path);
  if (m) return { kind: "variant-copy", variant: m[1], key: m[2] };
  m = /^(?:variants\.([^.]+)\.)?media\.(\d+)\.alt$/.exec(path);
  if (m) return { kind: "alt", variant: m[1] || null, index: Number(m[2]) };
  if (path === "copy.base") return { kind: "base" };
  if (path === "copy.link") return { kind: "link" };
  return { kind: "other" };
};

/** What the human sees for a stored value (inherited copy shows through when a variant's own value is null). */
export function displayOf(post, path) {
  const info = pathInfo(path);
  const v = info.variant ? findVariant(post, info.variant) : null;
  if (info.kind === "variant-copy") {
    if (info.key === "caption") return effectiveCaption(post, v);
    if (info.key === "first_comment") return effectiveFirstComment(post, v);
    return effectiveHashtags(post, v).join(" ");
  }
  const raw = getPath(post, path);
  return raw === null || raw === undefined ? "" : Array.isArray(raw) ? raw.join(" ") : String(raw);
}

/** The stored form of what was typed: hashtags become a list without the #, everything else stays text. */
export const toStored = (path, text) => (/(^|\.)copy\.hashtags$/.test(path) ? text.split(/[\s,]+/).filter(Boolean).map((t) => t.replace(/^#/, "")) : text);

/** The dirty fields' display text (a hashtag list joins with spaces). */
export const dirtyText = (entry) => (Array.isArray(entry.value) ? entry.value.join(" ") : entry.value);

/** Sort my unsaved fields against a newer post: `same` landed on the same value, `rebase` only I changed, `conflict` both changed. */
export function sortAgainst(latest, dirty) {
  const out = { same: [], rebase: [], conflict: [] };
  for (const [path, e] of dirty) out[classify(e.base, getPath(latest, path), e.value)].push(path);
  return out;
}

/** Every asset id a post's media uses, shared and per channel. */
export function assetIds(post) {
  const ids = new Set();
  (post.media || []).forEach((m) => ids.add(m.asset));
  (post.variants || []).forEach((v) => (v.media || []).forEach((m) => ids.add(m.asset)));
  return [...ids].filter(Boolean);
}

/** Read the library record of every asset the post uses that `cache` does not have yet; a missing one is marked, never thrown. */
export async function loadAssets(api, post, cache) {
  await Promise.all(assetIds(post).filter((id) => !cache[id]).map(async (id) => {
    try { const r = await api.get(`library/${id}`); cache[id] = { ...r.asset, url: r.url, preview_url: r.preview_url, coverage: r.license, usage: r.usage }; }
    catch { cache[id] = { id, missing: true }; }
  }));
  return cache;
}

/** The preview model of one channel with the unsaved edits applied, so the preview follows typing before anything is saved. */
export function previewOf(post, dirty, variant, { assets = {}, accounts = {}, server = null, showHidden = true } = {}) {
  const view = JSON.parse(JSON.stringify(post));
  for (const [p, e] of dirty) setPath(view, p, e.value);
  const vv = findVariant(view, variant.id) || variant;
  const acct = accounts[variant.account];
  return previewModel(view, vv, { assets, account: acct ? { id: variant.account, label: acct.label } : null, server, showHidden });
}

/** Content slides of a channel (the cover is separate), each with a stable id. */
export const slidesOf = (post, variant) => effectiveMedia(post, variant || {}).map((m, i) => ({ ...m, id: m.id || `legacy_${i}` }));

/** Media import through the app: the web view cannot show a file picker, so files always come through aveNative.importLibrary.
 *  `add(assetId, row)` is called for each file the person chose, in order. Returns the rows; null when there is no app to ask. */
export async function importNative(add) {
  const rows = await importToLibrary();
  if (rows === null) return null;
  for (const row of rows) await add(row.asset.id, row);
  return rows;
}
