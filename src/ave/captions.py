"""Captions: transcript words -> output-time cues -> SRT (sidecar) and ASS (styled, burned in with ffmpeg).

Why we generate captions ourselves: Resolve's free scripting API has no subtitle styling and one Studio-gated call, so
captions are computed from the transcript and the EditDoc, written as plain files, and burned by ffmpeg's `ass` filter.

Pipeline
  transcript words (episode time) --cues_from_edit--> words in OUTPUT time (removed sections dropped)
  --cues_from_words--> cues (line/length/reading-speed rules from config/captions.styles.yaml)
  --write_srt / write_ass--> files;  burn() draws the ASS onto a video;  retranscribe() re-derives words from a render.

Everything visual is expressed as fractions of the canvas (see the YAML), so one style is correct at any resolution.
"""

from __future__ import annotations

import bisect
import copy
import math
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import config, editdoc, sessions, transcribe
from .episode import Episode, read_json, write_json

REPO_CONFIG = config.REPO_ROOT / "config" / "captions.styles.yaml"
FONT_EXTS = {".ttf", ".otf", ".ttc"}
DEFAULT_LINE_HEIGHT = 1.3     # conservative line box / font size, only used to keep blocks inside the safe zone
BOLD_WIDTH_FACTOR = 1.08      # width of a bold face vs the regular file we may have measured
WIDTH_SLACK = 0.96            # use at most this fraction of the safe width (libass hinting differs a little from PIL)


@dataclass
class Cue:
    start: float
    end: float
    text: str                                   # lines joined with "\n"
    speaker: str | None = None
    words: list[dict] = field(default_factory=list)   # [{"w","start","end"}] in the same clock as start/end

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def chars(self) -> int:
        """Characters a viewer must read (a line break counts as the space it replaces)."""
        return len(self.text.replace("\n", " "))


# ---- styles ------------------------------------------------------------------------------------

def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def load_styles(path: Path | str | None = None, ep: Episode | None = None) -> dict:
    """Packaged defaults, overridden (deep-merged) by ~/.config/ave, then <episode>/policy, then an explicit path."""
    data = yaml.safe_load(REPO_CONFIG.read_text())
    layers = [Path.home() / ".config" / "ave" / "captions.styles.yaml"]
    if ep is not None:
        layers.append(ep.root / "policy" / "captions.styles.yaml")
    if path:
        layers.append(Path(path))
    for p in layers:
        if p.exists():
            data = _deep_merge(data, yaml.safe_load(p.read_text()) or {})
    return data


def get_style(name: str, styles: dict | None = None, ep: Episode | None = None, path: Path | str | None = None) -> dict:
    """One style with its segmentation rules resolved into style["rules"]."""
    styles = styles or load_styles(path, ep)
    if name not in styles["styles"]:
        raise KeyError(f"unknown caption style {name!r}; choose one of {sorted(styles['styles'])}")
    st = copy.deepcopy(styles["styles"][name])
    st["name"] = name
    st["rules"] = copy.deepcopy(styles["rules"][st["rules"]])
    return st


def _rules(style_or_rules: dict | None) -> dict:
    if style_or_rules is None:
        return get_style("podcast")["rules"]
    return style_or_rules["rules"] if "rules" in style_or_rules and "max_chars_per_line" not in style_or_rules else style_or_rules


# ---- words -> output time ----------------------------------------------------------------------

def episode_glossary(ep: Episode) -> dict | list:
    return read_json(ep.meta_path).get("glossary") or {}


def map_words_to_output(words: list[dict], doc: dict) -> list[dict]:
    """Transcript words (episode time) -> words in output time through the doc's dialogue segments.

    A word belongs to the segment that contains its midpoint, so words in removed sections vanish and `fill` segments
    (room tone, no speech) never contribute. A word is clipped at a cut, but not where the next kept range continues
    straight on in the episode (an artificial split must not shave the word).
    """
    segs = [s for s in sorted(doc["tracks"]["dialogue"], key=lambda s: s["o0"]) if not s.get("fill") and "e0" in s]
    ws = sorted(words, key=lambda w: w["start"])
    mids = [(w["start"] + w["end"]) / 2 for w in ws]
    out: list[dict] = []
    for i, s in enumerate(segs):
        length = s["o1"] - s["o0"]
        e0, e1 = s["e0"], s["e0"] + length
        prev_joined = i > 0 and segs[i - 1]["src"] == s["src"] and abs(segs[i - 1]["e0"] + (segs[i - 1]["o1"] - segs[i - 1]["o0"]) - e0) < 1e-3
        next_joined = i + 1 < len(segs) and segs[i + 1]["src"] == s["src"] and abs(segs[i + 1]["e0"] - e1) < 1e-3
        for k in range(bisect.bisect_left(mids, e0), bisect.bisect_left(mids, e1)):
            w = ws[k]
            a, b = w["start"], w["end"]
            if not prev_joined:
                a = max(a, e0)
            if not next_joined:
                b = min(b, e1)
            m = dict(w)
            m["start"], m["end"] = round(s["o0"] + a - e0, 4), round(s["o0"] + b - e0, 4)
            out.append(m)
    out.sort(key=lambda w: (w["start"], w["end"]))
    return [w for w in out if w["end"] > w["start"] and w["start"] < doc["duration"]]


# ---- word cleanup: fillers + glossary ----------------------------------------------------------

_PUNCT = re.compile(r"^(\W*)(.*?)(\W*)$", re.S)


def _split_punct(tok: str) -> tuple[str, str, str]:
    m = _PUNCT.match(tok)
    return m.group(1), m.group(2), m.group(3)


def _bare(tok: str) -> str:
    return _split_punct(tok)[1].lower()


def _glossary_map(glossary) -> dict[tuple[str, ...], str]:
    """{wrong: right} or a list of correctly-spelled names -> {lowercase token tuple: replacement}."""
    if not glossary:
        return {}
    pairs = glossary.items() if isinstance(glossary, dict) else [(g, g) for g in glossary]
    return {tuple(k.lower().split()): v for k, v in pairs if k and v}


def _apply_glossary(words: list[dict], glossary) -> list[dict]:
    gmap = _glossary_map(glossary)
    if not gmap:
        return words
    longest = max(len(k) for k in gmap)
    out: list[dict] = []
    i = 0
    while i < len(words):
        for n in range(min(longest, len(words) - i), 0, -1):
            key = tuple(_bare(w["w"]) for w in words[i:i + n])
            if key in gmap:
                span = words[i:i + n]
                lead, tail = _split_punct(span[0]["w"])[0], _split_punct(span[-1]["w"])[2]
                new = gmap[key].split()
                t0, t1 = span[0]["start"], span[-1]["end"]
                step = (t1 - t0) / len(new)
                for j, tok in enumerate(new):
                    w = dict(span[0])
                    w["w"] = (lead if j == 0 else "") + tok + (tail if j == len(new) - 1 else "")
                    w["start"], w["end"] = round(t0 + j * step, 4), round(t0 + (j + 1) * step, 4)
                    out.append(w)
                i += n
                break
        else:
            out.append(words[i])
            i += 1
    return out


def _clean(words: list[dict], rules: dict, glossary) -> list[dict]:
    fillers = {f.lower() for f in rules.get("drop_fillers") or []}
    kept = [dict(w) for w in words if w.get("w", "").strip() and _bare(w["w"]) not in fillers]
    return _apply_glossary(kept, glossary)


# ---- segmentation ------------------------------------------------------------------------------

def _wrap(tokens: list[str], rules: dict) -> list[list[str]] | None:
    """Split tokens into at most max_lines lines of at most max_chars each, or None if it cannot be done.

    Two lines are balanced, prefer a break after punctuation, never end line one on a weak word, and never leave
    a one-word line (an orphan) while there are enough words to avoid it.
    """
    mx, lines = rules["max_chars_per_line"], rules["max_lines"]
    one = " ".join(tokens)
    if len(one) <= mx:
        return [tokens]
    if lines < 2 or len(tokens) < 2:
        return None
    if lines > 2:  # greedy fill; the editorial rules above are specified for two lines
        out, cur = [], []
        for t in tokens:
            if cur and len(" ".join(cur + [t])) > mx:
                out.append(cur)
                cur = []
            cur.append(t)
        out.append(cur)
        return out if len(out) <= lines and all(len(" ".join(l)) <= mx for l in out) else None
    weak = set(rules.get("avoid_ending_with") or [])
    best, best_score = None, None
    for k in range(1, len(tokens)):
        a, b = tokens[:k], tokens[k:]
        la, lb = len(" ".join(a)), len(" ".join(b))
        if la > mx or lb > mx:
            continue
        score = -abs(la - lb) / mx * 2.0 + (0.4 if lb >= la else 0.0)
        if a[-1][-1:] in ".?!":
            score += 3.0
        elif a[-1][-1:] in ",;:":
            score += 2.0
        if _bare(a[-1]) in weak:
            score -= 2.5
        if _bare(b[0]) in set(rules.get("clause_starters") or []):
            score += 0.8
        if len(a) == 1 or len(b) == 1:
            score -= 8.0                      # orphan line
        if best_score is None or score > best_score:
            best, best_score = [a, b], score
    return best


def _fits(chunk: list[dict], rules: dict) -> bool:
    if chunk[-1]["end"] + rules["tail_s"] - chunk[0]["start"] > rules["max_cue_s"]:
        return False
    return _wrap([w["w"] for w in chunk], rules) is not None


def _break_score(block: list[dict], i: int, k: int, rules: dict) -> float:
    """How good is ending a cue after block[k-1] (the cue being block[i:k])? Higher is better."""
    last = block[k - 1]["w"]
    score = 0.0
    if last[-1:] in ".?!":
        score += 4.0
    elif last[-1:] in ",;:":
        score += 2.5
    if k < len(block):
        score += min(block[k]["start"] - block[k - 1]["end"], 0.6) * 5.0       # a breath is a natural seam
        if _bare(block[k]["w"]) in set(rules.get("clause_starters") or []):
            score += 1.5
    cap = rules["max_chars_per_line"] * rules["max_lines"]
    score += 2.0 * len(" ".join(w["w"] for w in block[i:k])) / cap            # prefer fuller cues
    if _bare(last) in set(rules.get("avoid_ending_with") or []):
        score -= 2.5
    if k - i == 1:
        score -= 6.0
    if len(block) - k == 1:
        score -= 5.0                                                           # would strand a one-word cue
    if block[k - 1]["end"] - block[i]["start"] < rules["min_cue_s"] * 0.6:
        score -= 1.0
    return score


def _blocks(words: list[dict], rules: dict) -> list[list[dict]]:
    """Runs of words with no hard boundary: a speaker change or a pause of pause_break_s starts a new run."""
    blocks: list[list[dict]] = []
    for w in words:
        if blocks:
            p = blocks[-1][-1]
            same = w.get("speaker") == p.get("speaker") or w.get("speaker") is None or p.get("speaker") is None
            if same and w["start"] - p["end"] < rules["pause_break_s"]:
                blocks[-1].append(w)
                continue
        blocks.append([w])
    return blocks


def _split_block(block: list[dict], rules: dict) -> list[list[dict]]:
    out, i, n = [], 0, len(block)
    while i < n:
        j = i + 1
        while j < n and _fits(block[i:j + 1], rules):
            j += 1                                    # block[i:j] is the longest chunk that fits
        if j >= n:
            out.append(block[i:])
            break
        k = max(range(i + 1, j + 1), key=lambda c: _break_score(block, i, c, rules))
        out.append(block[i:k])
        i = k
    return out


def _timed(chunks: list[list[dict]], rules: dict) -> list[Cue]:
    """Chunks of words -> cues with start/end set by the timing rules (hold, min length, reading speed, no overlap)."""
    cues: list[Cue] = []
    for ch in chunks:
        lines = _wrap([w["w"] for w in ch], rules) or [[w["w"] for w in ch]]
        spk = next((w.get("speaker") for w in ch if w.get("speaker")), None)
        cues.append(Cue(ch[0]["start"], ch[-1]["end"] + rules["tail_s"], "\n".join(" ".join(l) for l in lines), spk,
                        [{"w": w["w"], "start": w["start"], "end": w["end"]} for w in ch]))
    for i, c in enumerate(cues):
        limit = cues[i + 1].start - rules["min_gap_s"] if i + 1 < len(cues) else float("inf")
        end = min(c.end, limit)
        need = max(rules["min_cue_s"], c.chars / rules["max_cps"])
        end = max(end, min(c.start + need, limit))          # lengthen into the silence after the cue, never past it
        c.end = round(min(math.ceil(end * 1000) / 1000, limit, c.start + rules["max_cue_s"]), 4)
        if c.end <= c.start:                                 # words so crowded there is no room: keep a sliver
            c.end = round(c.start + 0.2, 4)
    return cues


def cues_from_words(words: list[dict], rules: dict | None = None, glossary=None) -> list[Cue]:
    """Words [{"w","start","end","speaker"?}] in any one clock -> cues obeying the caption rules.

    `rules` is a rules dict (or a whole style). Fillers named in rules are dropped; `glossary` ({wrong: right} or a
    list of correctly capitalised names) is applied before segmentation so line lengths are measured on final text.
    """
    rules = _rules(rules)
    ws = _clean(sorted(words, key=lambda w: (w["start"], w["end"])), rules, glossary)
    chunks = [c for b in _blocks(ws, rules) for c in _split_block(b, rules)]
    return _timed(chunks, rules)


def cues_from_transcript(transcript: dict | list, rules: dict | None = None, glossary=None) -> list[Cue]:
    """Cues in the transcript's own clock (no edit applied). Accepts a transcript dict or a bare word list."""
    words = transcript["words"] if isinstance(transcript, dict) else transcript
    return cues_from_words(words, rules, glossary)


def cues_from_edit(ep: Episode, doc: dict, style: dict | str | None = None) -> list[Cue]:
    """Cues in the doc's OUTPUT time from work/transcript.json, honouring cuts, fills and the episode glossary."""
    st = get_style(style, ep=ep) if isinstance(style, str) else (style or get_style("podcast", ep=ep))
    tr = read_json(ep.artifact("transcript.json"))
    return cues_from_words(map_words_to_output(tr["words"], doc), st["rules"], episode_glossary(ep))


# ---- check -------------------------------------------------------------------------------------

def check(cues: list[Cue], style: dict) -> list[dict]:
    """Rule violations as [{"cue": 1-based index, "kind", "detail"}]. Empty means the captions meet the policy.

    A cue shorter than the minimum is not reported when the next cue follows immediately (a one-word interjection
    between two long cues cannot be lengthened without delaying the next caption; sync wins over minimum time).
    """
    r = _rules(style)
    problems: list[dict] = []

    def add(i, kind, detail):
        problems.append({"cue": i, "kind": kind, "detail": detail})

    for i, c in enumerate(cues, 1):
        lines = c.text.split("\n")
        if not c.text.strip():
            add(i, "empty", "cue has no text")
        if len(lines) > r["max_lines"]:
            add(i, "too_many_lines", f"{len(lines)} lines, max {r['max_lines']}")
        for l in lines:
            if len(l) > r["max_chars_per_line"]:
                add(i, "line_too_long", f"{len(l)} chars (max {r['max_chars_per_line']}): {l!r}")
        if len(lines) > 1 and any(len(l.split()) == 1 for l in lines):
            add(i, "orphan_line", f"one-word line in {c.text!r}")
        nxt = cues[i].start if i < len(cues) else None
        boxed_in = nxt is not None and nxt - c.end < r["min_gap_s"] + 0.002   # nowhere to extend: a quick interjection
        if c.duration < r["min_cue_s"] - 0.002 and not boxed_in:
            add(i, "too_short", f"{c.duration:.2f}s on screen (min {r['min_cue_s']}s)")
        if c.duration > r["max_cue_s"] + 0.002:
            add(i, "too_long", f"{c.duration:.2f}s on screen (max {r['max_cue_s']}s)")
        if c.duration > 0 and c.chars / c.duration > r["max_cps"] + 0.05:
            add(i, "reading_speed", f"{c.chars / c.duration:.1f} chars/s (max {r['max_cps']})")
        if i > 1 and c.start < cues[i - 2].end - 0.001:
            add(i, "overlap", f"starts {c.start:.3f}s before the previous cue ends at {cues[i - 2].end:.3f}s")
    return problems


# ---- SRT ---------------------------------------------------------------------------------------

def _srt_ts(t: float) -> str:
    ms = max(0, round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(cues: list[Cue], path: Path | str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    blocks = [f"{i}\n{_srt_ts(c.start)} --> {_srt_ts(c.end)}\n{c.text}" for i, c in enumerate(cues, 1)]
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n\n".join(blocks) + ("\n" if blocks else ""), encoding="utf-8")
    tmp.replace(path)
    return path


_SRT_TS = re.compile(r"(\d+):(\d\d):(\d\d)[,.](\d{1,3})")


def _parse_srt_ts(s: str) -> float:
    h, m, sec, ms = _SRT_TS.match(s.strip()).groups()
    return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms.ljust(3, "0")) / 1000


def parse_srt(path: Path | str) -> list[Cue]:
    cues = []
    for block in re.split(r"\n\s*\n", Path(path).read_text(encoding="utf-8-sig").replace("\r\n", "\n").strip()):
        lines = block.split("\n")
        t = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if t is None:
            continue
        a, b = lines[t].split("-->")
        cues.append(Cue(_parse_srt_ts(a), _parse_srt_ts(b), "\n".join(lines[t + 1:]).strip()))
    return cues


# ---- fonts -------------------------------------------------------------------------------------

def default_fonts_dirs() -> list[Path]:
    d = config.REPO_ROOT / "motion-kit" / "brand" / "fonts"
    return [d] if d.is_dir() else []


def _fc_match(pattern: str) -> tuple[str, str, int] | None:
    if not shutil.which("fc-match"):
        return None
    r = subprocess.run(["fc-match", "-f", "%{family}|%{file}|%{index}", pattern], capture_output=True, text=True)
    if r.returncode != 0 or r.stdout.count("|") < 2:
        return None
    fam, file, idx = r.stdout.rsplit("|", 2)
    return fam, file, int(idx or 0)


def _bundled_fonts(dirs) -> list[tuple[str, str]]:
    from PIL import ImageFont
    found = []
    for d in dirs or []:
        for f in sorted(Path(d).rglob("*")):
            if f.suffix.lower() in FONT_EXTS:
                try:
                    found.append((ImageFont.truetype(str(f), 20).getname()[0], str(f)))
                except Exception:
                    continue
    return found


def resolve_font(candidates: list[str], fonts_dirs=None, bold: bool = False) -> dict:
    """First usable family: bundled files (motion-kit/brand/fonts) beat system fonts. Never raises; warns instead."""
    warnings: list[str] = []
    bundled = _bundled_fonts(default_fonts_dirs() if fonts_dirs is None else fonts_dirs)
    for cand in candidates:
        for fam, file in bundled:
            if fam.lower() == cand.lower():
                return {"family": fam, "file": file, "index": 0, "warnings": warnings, "bundled": True}
        m = _fc_match(cand + (":bold" if bold else ""))
        if m and cand.lower() in m[0].lower():
            if warnings:   # a preferred font was missing; say so once, tersely
                warnings = [f"preferred font(s) not installed ({', '.join(w.split(chr(39))[1] for w in warnings)}); using {cand!r}"]
            return {"family": cand, "file": m[1], "index": m[2], "warnings": warnings, "bundled": False}
        warnings.append(f"font {cand!r} not found")
    m = _fc_match("sans-serif:bold" if bold else "sans-serif")
    fam = (m[0].split(",")[0] if m else "Helvetica")
    warnings.append(f"none of the style's fonts are installed ({', '.join(candidates)}); using {fam!r}. "
                    "Bundle a font in motion-kit/brand/fonts or install one.")
    return {"family": fam, "file": m[1] if m else None, "index": m[2] if m else 0, "warnings": warnings, "bundled": False}


def _text_metrics(font: dict):
    """(width_of(text, size_px), line_height_ratio) measured with PIL when the font file loads, else estimates."""
    try:
        from PIL import ImageFont
        probe = ImageFont.truetype(font["file"], 100, index=font.get("index", 0))
        asc, desc = probe.getmetrics()
        return (lambda text, size: probe.getlength(text) * size / 100.0), (asc + desc) / 100.0
    except Exception:
        return (lambda text, size: 0.62 * size * len(text)), DEFAULT_LINE_HEIGHT


# ---- ASS ---------------------------------------------------------------------------------------

def _ass_color(spec, default_alpha: float = 1.0) -> str:
    hexv, alpha = (spec if isinstance(spec, (list, tuple)) else (spec, default_alpha))
    hexv = hexv.lstrip("#")
    r, g, b = hexv[0:2], hexv[2:4], hexv[4:6]
    a = round((1.0 - float(alpha)) * 255)
    return f"&H{a:02X}{b}{g}{r}".upper()


def _ass_ts(cs: int) -> str:
    h, cs = divmod(cs, 360_000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _esc(text: str) -> str:
    """Neutralise ASS control characters in caption text (braces open override blocks; backslashes start commands)."""
    return text.replace("\\", "/").replace("{", "(").replace("}", ")")


def safe_rect(style: dict, canvas: tuple[int, int]) -> tuple[float, float, float, float]:
    """(x0, y0, x1, y1) in pixels that rendered caption pixels must stay inside."""
    W, H = canvas
    s = style["safe"]
    return s["left"] * W, s["top"] * H, (1 - s["right"]) * W, (1 - s["bottom"]) * H


def _karaoke_text(cue: Cue, S: int, E: int) -> str:
    """{\\kf<cs>}word ... one tag per word; the durations sum to exactly E - S (the event length in centiseconds)."""
    lines = [l.split() for l in cue.text.split("\n")]
    toks = [t for l in lines for t in l]
    n = len(toks)
    if len(cue.words) == n:
        starts = [round(w["start"] * 100) for w in cue.words]
    else:                                   # no usable word times: spread evenly
        starts = [S + round((E - S) * i / n) for i in range(n)]
    b = [S]
    for i in range(1, n):
        b.append(min(max(starts[i], b[-1] + 1), E - (n - i)))
    b.append(E)
    parts, idx = [], 0
    for li, l in enumerate(lines):
        for ti, tok in enumerate(l):
            last_in_line = ti == len(l) - 1
            sep = "" if idx == n - 1 else ("\\N" if last_in_line else " ")
            parts.append(f"{{\\kf{b[idx + 1] - b[idx]}}}{_esc(tok)}{sep}")
            idx += 1
    return "".join(parts)


def write_ass(cues: list[Cue], style: dict, canvas: tuple[int, int], path: Path | str, fonts_dirs=None) -> dict:
    """Write a styled ASS file. Returns {"path","font","font_file","font_size","warnings"}.

    Font size, outline, margins and position all derive from the canvas, and a cue whose widest line (or height) would
    leave the safe zone gets its own smaller \\fs, so text never spills into platform UI or off the frame.
    """
    path = Path(path)
    W, H = int(canvas[0]), int(canvas[1])
    ss = min(W, H)
    font = resolve_font(style["fonts"], fonts_dirs, bool(style.get("bold")))
    width_of, lh_ratio = _text_metrics(font)
    bold = bool(style.get("bold"))
    size = max(8, round(style["size_ss"] * ss))
    outline = round(style["outline_ss"] * ss * 100) / 100
    shadow = round(style.get("shadow_ss", 0.0) * ss * 100) / 100
    karaoke = bool(style.get("karaoke"))
    x0, y0, x1, y1 = safe_rect(style, (W, H))
    pos = style["position"]
    at_point = isinstance(pos, dict)
    mv = 0 if at_point else round(style.get("margin_v", 0.08) * H)
    ml, mr = round(style["safe"]["left"] * W), round(style["safe"]["right"] * W)
    primary = _ass_color(style["highlight_color"] if karaoke else style["color"])
    secondary = _ass_color(style["color"])
    out_c = _ass_color(style["outline_color"])
    back_c = _ass_color(["#000000", 0.55])

    header = [
        "[Script Info]", "; Generated by ave captions", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}",
        "WrapStyle: 2", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, "
        "StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{font['family']},{size},{primary},{secondary},{out_c},{back_c},{-1 if bold else 0},0,0,0,100,100,0,0,1,"
        f"{outline},{shadow},{5 if at_point else 2},{ml},{mr},{mv},1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    events = []
    for c in cues:
        S, E = round(c.start * 100), round(c.end * 100)
        E = max(E, S + 1)
        lines = c.text.split("\n")
        bf = BOLD_WIDTH_FACTOR if bold and font["bundled"] else 1.0
        widest = max(width_of(l, size) for l in lines) * bf
        allowed_w = (x1 - x0) * WIDTH_SLACK - 2 * outline
        if at_point:
            half = min(pos["y"] * H - y0, y1 - pos["y"] * H)
            allowed_h = 2 * half
        else:
            allowed_h = (H - mv) - y0
        k = min(1.0, allowed_w / widest if widest else 1.0, allowed_h / (len(lines) * size * lh_ratio))
        tag = ""
        if at_point:
            tag += f"\\an5\\pos({round(pos['x'] * W)},{round(pos['y'] * H)})"
        if k < 0.999:
            tag += f"\\fs{max(8, int(size * k))}"
        body = _karaoke_text(c, S, E) if karaoke else "\\N".join(_esc(l) for l in lines)
        events.append(f"Dialogue: 0,{_ass_ts(S)},{_ass_ts(E)},Default,,0,0,0,,{{{tag}}}{body}" if tag else
                      f"Dialogue: 0,{_ass_ts(S)},{_ass_ts(E)},Default,,0,0,0,,{body}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(header + events) + "\n", encoding="utf-8")
    tmp.replace(path)
    return {"path": str(path), "font": font["family"], "font_file": font["file"], "font_size": size, "warnings": font["warnings"]}


_ASS_TAGS = re.compile(r"\{[^}]*\}")


def parse_ass(path: Path | str) -> list[Cue]:
    """Read cues back from an ASS file (override tags stripped). Word timings are not recovered."""
    def ts(s):
        h, m, sec = s.strip().split(":")
        return int(h) * 3600 + int(m) * 60 + float(sec)
    cues = []
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        if line.startswith("Dialogue:"):
            f = line.split(":", 1)[1].split(",", 9)
            text = _ASS_TAGS.sub("", f[9]).replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ")
            cues.append(Cue(ts(f[1]), ts(f[2]), text.strip()))
    return cues


# ---- burn --------------------------------------------------------------------------------------

def burn(video_in: Path | str, ass: Path | str, out: Path | str, fonts_dirs=None, overwrite: bool = False) -> Path:
    """Draw an ASS file onto a video with ffmpeg's `ass` filter (re-encodes video, copies audio).

    The ASS and fonts are staged in a temp dir and referenced by relative name so paths with spaces, commas or quotes
    cannot break the filter graph. Output is written to a partial file and renamed, so a failure leaves nothing behind.
    """
    video_in, ass, out = Path(video_in), Path(ass), Path(out)
    if not video_in.exists():
        raise FileNotFoundError(f"video not found: {video_in}")
    if not ass.exists():
        raise FileNotFoundError(f"ASS file not found: {ass}")
    if out.exists() and not overwrite:
        raise FileExistsError(f"{out} exists (pass overwrite=True to replace it)")
    ffmpeg = config.binary("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH")
    from . import ffutil
    ffutil.require_libass()
    out.parent.mkdir(parents=True, exist_ok=True)
    partial = out.parent / f".{out.stem}.partial{out.suffix}"
    dirs = [Path(d) for d in (default_fonts_dirs() if fonts_dirs is None else fonts_dirs) if Path(d).is_dir()]
    with tempfile.TemporaryDirectory(prefix="ave-burn-") as td:
        td = Path(td)
        shutil.copyfile(ass, td / "captions.ass")
        vf = "ass=filename=captions.ass"
        if dirs:
            (td / "fonts").mkdir()
            for i, d in enumerate(dirs):
                (td / "fonts" / f"d{i}").symlink_to(d.resolve())
            vf += ":fontsdir=fonts"
        cmd = [str(ffmpeg), "-nostdin", "-v", "error", "-y", "-i", str(video_in.resolve()), "-vf", vf,
               "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
               "-c:a", "copy", "-movflags", "+faststart", str(partial.resolve())]
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=td)
    if proc.returncode != 0:
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg caption burn failed ({proc.returncode}): {proc.stderr[-1500:]}")
    partial.replace(out)
    return out


# ---- retranscribe ------------------------------------------------------------------------------

def _duration(wav: Path) -> float:
    import wave
    with wave.open(str(wav)) as w:
        return w.getnframes() / w.getframerate()


def retranscribe(ep: Episode, video: Path | str, asr: str = "fluidaudio", language: str | None = None,
                 diarize: bool = False) -> dict:
    """ASR the rendered final so captions match exactly what is heard (timing and wording after all edits).

    Extracts 16 kHz mono PCM (what the ASR engines expect) and reuses ave.transcribe's engine wrappers. Falls back to
    whisper.cpp, with a warning, when FluidAudio is not built. Returns a transcript-like dict and saves it under work/retranscribe/.
    """
    video = Path(video)
    if not video.exists() and (ep.root / video).exists():
        video = ep.root / video
    if not video.exists():
        raise FileNotFoundError(f"video not found: {video}")
    ffmpeg = config.binary("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH")
    work = ep.work / "retranscribe"
    work.mkdir(parents=True, exist_ok=True)
    stem = video.stem
    wav = work / f"{stem}.16k.wav"
    r = subprocess.run([str(ffmpeg), "-nostdin", "-v", "error", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
                        "-c:a", "pcm_s16le", str(wav)], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"could not extract audio from {video}: {r.stderr[-800:]}")
    warnings: list[str] = []
    if asr == "fluidaudio" and not config.fluidaudio_cli():
        warnings.append("FluidAudio is not built; used whisper.cpp instead (run scripts/build-fluidaudio.sh for Parakeet).")
        asr = "whisper"
    words = (transcribe.run_fluidaudio_asr(wav, work / f"{stem}.fluidaudio.json", language) if asr == "fluidaudio"
             else transcribe.run_whisper_asr(wav, work / f"{stem.replace('.', '_')}_whisper", language))  # no dots: it uses with_suffix
    if not words:
        raise RuntimeError("ASR returned no words. Is there speech in the rendered video?")
    diar = None
    if diarize and config.fluidaudio_cli():
        segs = transcribe.run_fluidaudio_diarization(wav, work / f"{stem}.diar.json", None)["segments"]
        transcribe.assign_speakers(words, segs)
        diar = "fluidaudio"
    else:
        for w in words:
            w["speaker"] = "S1"
    utts = transcribe.build_utterances(words)
    totals: dict[str, float] = {}
    for u in utts:
        totals[u["speaker"]] = totals.get(u["speaker"], 0.0) + (u["end"] - u["start"])
    try:
        source = str(video.resolve().relative_to(ep.root))
    except ValueError:
        source = str(video)
    result = {"source": source, "engine": {"asr": asr, "diarization": diar}, "duration": _duration(wav),
              "speakers": {k: {"talk_seconds": round(v, 2), "name": None} for k, v in sorted(totals.items())},
              "words": words, "utterances": utts, "warnings": warnings}
    write_json(work / f"{stem}.transcript.json", result)
    return result


# ---- the stage command's engine ----------------------------------------------------------------

def _ffprobe_size(video: Path) -> tuple[int, int]:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                        "-of", "csv=p=0", str(video)], capture_output=True, text=True)
    w, h = r.stdout.strip().split(",")
    return int(w), int(h)


def make_captions(ep: Episode, stage: str = "rough", style_name: str = "podcast", name: str | None = None,
                  version: int | None = None, styles_path=None, burn_video: Path | str | None = None) -> dict:
    """Captions for an edit stage: writes captions/<stage>.srt|.ass and records them in a NEW EditDoc version.

    With `burn_video`, also writes <video>.captions.mp4 and sets captions.burn in the new version. The ASS used for burning is
    regenerated at the video's real resolution if it differs from the doc canvas.
    """
    path = editdoc.path_for(ep, stage, version, name) if version else editdoc.latest_path(ep, stage, name)
    if not path or not path.exists():
        raise SystemExit(f"No {stage} edit{' ' + name if name else ''} yet for {ep.root.name}. Build it first (see `ave status`)." + sessions.hint(ep, name))
    doc = editdoc.load(path)
    style = get_style(style_name, ep=ep, path=styles_path)
    cues = cues_from_edit(ep, doc, style)
    if not cues:
        raise SystemExit("No speech falls inside this edit's dialogue, so there is nothing to caption.")
    tag = f"{stage}.{name}" if name else stage
    srt = write_srt(cues, ep.captions / f"{tag}.srt")
    canvas = (doc["canvas"]["w"], doc["canvas"]["h"])
    info = write_ass(cues, style, canvas, ep.captions / f"{tag}.ass")
    problems = check(cues, style)

    new = editdoc.clone(doc)
    new["captions"] = {"srt": ep.rel(srt), "ass": ep.rel(info["path"]), "burn": bool(burn_video)}
    burned = None
    if burn_video:
        v = Path(burn_video)
        v = v if v.is_absolute() or v.exists() else ep.root / v
        ass_path = Path(info["path"])
        size = _ffprobe_size(v)
        if size != canvas:
            ass_path = write_ass(cues, style, size, ep.work / "render" / f"{tag}.{size[0]}x{size[1]}.ass")["path"]
        out = v.with_name(f"{v.stem}.captions{v.suffix}")
        burn(v, ass_path, out, overwrite=True)
        burned = str(out.relative_to(ep.root)) if out.is_relative_to(ep.root) else str(out)
    saved = editdoc.save_new_version(ep, new, name)
    ep.mark_stage("captions", edit=saved.name, style=style_name, cues=len(cues))
    return {"stage": stage, "style": style_name, "edit": saved.name, "srt": new["captions"]["srt"], "ass": new["captions"]["ass"],
            "cues": len(cues), "problems": problems, "font": info["font"], "font_size": info["font_size"],
            "warnings": info["warnings"], "burned": burned}
