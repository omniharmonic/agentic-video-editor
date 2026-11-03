// Platform text metrics (spec 4.4): the browser twin of src/ave/studio/text_metrics.py.
// Both are tested against tests/data_studio/text_metrics_fixtures.json, so the counter you see while typing equals the server's number.
// Plain ES module, no DOM, runs in node too. graphemes() uses Intl.Segmenter when the browser has it, else the same approximation as Python.

const MARGIN = 0.02;
const X_URL_WEIGHT = 23;
const IG_FOLD_CHARS = 125;
const URL_RE = /(?:https?:\/\/|www\.)[^\s]+/gi;
const URL_TRAIL = ".,;:!?)]}'\"";
const MARK = /^\p{M}$/u;

const cpOf = (ch) => ch.codePointAt(0);
const isExtend = (cp) => {
  if (cp === 0x200d) return false;
  if ((cp >= 0xfe00 && cp <= 0xfe0f) || (cp >= 0x1f3fb && cp <= 0x1f3ff) || (cp >= 0xe0020 && cp <= 0xe007f) || cp === 0x20e3) return true;
  return MARK.test(String.fromCodePoint(cp));
};
const isRI = (cp) => cp >= 0x1f1e6 && cp <= 0x1f1ff;
const isPict = (cp) =>
  (cp >= 0x1f000 && cp <= 0x1faff) || (cp >= 0x2600 && cp <= 0x27bf) || (cp >= 0x2300 && cp <= 0x23ff) || (cp >= 0x2b00 && cp <= 0x2bff) ||
  [0x00a9, 0x00ae, 0x203c, 0x2049, 0x2122, 0x2139, 0x3030, 0x303d, 0x3297, 0x3299].includes(cp) ||
  (cp >= 0x2190 && cp <= 0x21ff) || (cp >= 0x25a0 && cp <= 0x25ff);

/** The approximation (identical in Python): an array of user-perceived characters. */
export function graphemesApprox(text) {
  const cps = Array.from(text);
  const out = [];
  let i = 0;
  while (i < cps.length) {
    const cp = cpOf(cps[i]);
    let j = i + 1;
    if (cp === 0x0d && cps[j] === "\n") j += 1;
    else if (isRI(cp) && j < cps.length && isRI(cpOf(cps[j]))) {
      j += 1;
      while (j < cps.length && isExtend(cpOf(cps[j]))) j += 1;
    } else if (cp !== 0x0a && cp !== 0x0d && cp !== 0x09) {
      while (j < cps.length) {
        const c = cpOf(cps[j]);
        if (isExtend(c)) j += 1;
        else if (c === 0x200d) {
          j += 1;
          if (j < cps.length && isPict(cpOf(cps[j]))) j += 1;
        } else break;
      }
    }
    out.push(cps.slice(i, j).join(""));
    i = j;
  }
  return out;
}

const SEG = typeof Intl !== "undefined" && Intl.Segmenter ? new Intl.Segmenter(undefined, { granularity: "grapheme" }) : null;

/** User-perceived characters. Bluesky and Threads count these. */
export function graphemes(text) {
  if (!SEG) return graphemesApprox(text);
  return Array.from(SEG.segment(text), (s) => s.segment);
}
export const graphemeLen = (text) => graphemes(text).length;

const isWide = (cp) => !(cp <= 4351 || (cp >= 8192 && cp <= 8205) || (cp >= 8208 && cp <= 8223) || (cp >= 8242 && cp <= 8247));

function urlSpans(text) {
  const spans = [];
  for (const m of text.matchAll(URL_RE)) {
    let end = m.index + m[0].length;
    while (end > m.index && URL_TRAIL.includes(text[end - 1])) end -= 1;
    if (end > m.index) spans.push([m.index, end]);
  }
  return spans;
}

function weigh(chunk) {
  let total = 0;
  for (const g of graphemes(chunk)) {
    const cps = Array.from(g, cpOf);
    if (cps.some((c) => isPict(c) || isRI(c)) && cps.some((c) => c > 0x2000)) total += 2;
    else for (const c of cps) total += isWide(c) ? 2 : 1;
  }
  return total;
}

/** X's weighted length: URLs 23, emoji clusters and wide scripts 2, the rest 1. */
export function xWeightedLen(text) {
  let total = 0;
  let pos = 0;
  for (const [a, b] of urlSpans(text)) {
    total += weigh(text.slice(pos, a)) + X_URL_WEIGHT;
    pos = b;
  }
  return total + weigh(text.slice(pos));
}

/** One number in the platform's unit: chars (code points) | graphemes | x_weighted | bytes. */
export function counter(text, unit = "chars") {
  if (unit === "graphemes") return graphemeLen(text);
  if (unit === "x_weighted") return xWeightedLen(text);
  if (unit === "bytes") return new TextEncoder().encode(text).length;
  return Array.from(text).length;
}

/** The effective limit after the 2 % safety margin the server applies. */
export const withMargin = (limit, margin = MARGIN) => Math.floor(limit * (1 - margin));

function wrapLines(text, perLine) {
  const lines = [];
  for (const para of text.split("\n")) {
    let cur = [];
    let word = [];
    const flush = () => {
      if (!word.length) return;
      if (cur.length + word.length > perLine && cur.length) { lines.push(cur); cur = []; }
      while (word.length > perLine) { lines.push(word.slice(0, perLine)); word = word.slice(perLine); }
      cur = cur.concat(word);
      word = [];
    };
    for (const ch of graphemes(para)) {
      word.push(ch);
      if (ch === " ") flush();
    }
    flush();
    lines.push(cur);
  }
  return lines;
}

/** Grapheme index where the feed cuts the caption behind "more", or null when it all shows.
 *  fold = {unit:"chars", at:125} | {unit:"lines", at:3, chars_per_line:52} (config/previews.yaml). */
export function foldIndex(text, fold) {
  const g = graphemes(text);
  const at = Number(fold.at || 0);
  if (fold.unit === "lines") {
    const lines = wrapLines(text, Number(fold.chars_per_line || 50));
    if (lines.length <= at) return null;
    let idx = 0;
    for (const ln of lines.slice(0, at)) {
      idx += ln.length;
      if (idx < g.length && g[idx] === "\n") idx += 1;
    }
    return idx < g.length ? idx : null;
  }
  return g.length > at ? at : null;
}

/** {visible, hidden, folded}: what shows before "more", and what sits behind it. */
export function visibleFold(text, fold) {
  const cut = foldIndex(text, fold);
  if (cut === null) return { visible: text, hidden: "", folded: false };
  const g = graphemes(text);
  return { visible: g.slice(0, cut).join("").trimEnd(), hidden: g.slice(cut).join(""), folded: true };
}
export const igVisibleFold = (text, at = IG_FOLD_CHARS) => visibleFold(text, { unit: "chars", at });
