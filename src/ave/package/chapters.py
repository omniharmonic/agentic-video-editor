"""Chapters for the release package.

Order of preference:
  1. `<work>/chapters.json` written by the human or an agent: `[{"start": seconds IN THE FINAL VIDEO, "title": "..."}]`
     (or `{"chapters": [...]}`). Used as given, never second-guessed.
  2. Topic breaks derived from the transcript (episode time) and mapped to final-video time through the final EditDoc.
     The titles are machine DRAFTS (the words that mark each section); the checklist says so.
Both paths are normalised to what YouTube accepts: the first chapter at 0:00, each at least `youtube_min_gap_s` long.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path

from ..episode import Episode, read_json

STOP = set("""a about above after again all also am an and any are aren't as at be because been before being below between both but by can can't
cannot could couldn't did didn't do does doesn't doing don't down during each few for from further had hadn't has hasn't have haven't having he
he'd he'll he's her here here's hers herself him himself his how how's i i'd i'll i'm i've if in into is isn't it it's its itself just let's me
more most mustn't my myself no nor not of off oh okay on once only or other ought our ours ourselves out over own really same shan't she she'd
she'll she's should shouldn't so some such than that that's the their theirs them themselves then there there's these they they'd they'll
they're they've this those through to too uh um under until up very was wasn't we we'd we'll we're we've were weren't what what's when when's
where where's which while who who's whom why why's with won't would wouldn't yeah yes you you'd you'll you're you've your yours yourself
yourselves like know think kind going gonna thing things something way lot get got say said mean right well actually people sort stuff
mm hmm mhm""".split())
_WORD = re.compile(r"[a-z][a-z']+")


def words_of(text: str) -> list[str]:
    out = []
    for w in _WORD.findall(text.lower()):
        w = w.strip("'")
        if len(w) >= 4 and w not in STOP:
            out.append(w[:-1] if w.endswith("s") and not w.endswith("ss") and len(w) > 4 else w)
    return out


def fmt_ts(t: float) -> str:
    """YouTube style: m:ss, or h:mm:ss from one hour."""
    t = int(round(t))
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ---- episode time -> final-video time -----------------------------------------------------------

def episode_to_output(doc: dict, e: float) -> float:
    """Where episode time `e` lands in the final video. A moment inside a removed section snaps to where the cut lands."""
    segs = doc["tracks"].get("dialogue") or doc["tracks"].get("video") or []
    segs = sorted(segs, key=lambda s: s["o0"])
    for s in segs:
        if s["e0"] - 1e-6 <= e <= s["e0"] + (s["o1"] - s["o0"]) + 1e-6:
            return s["o0"] + (e - s["e0"])
    later = [s for s in segs if s["e0"] > e]
    if later:
        return min(later, key=lambda s: s["e0"])["o0"]
    return segs[-1]["o1"] if segs else 0.0


# ---- reading a chapter list ---------------------------------------------------------------------

def chapters_path(ep: Episode) -> Path:
    return ep.work / "chapters.json"


def load_list(ep: Episode) -> list[dict] | None:
    p = chapters_path(ep)
    if not p.is_file():
        return None
    data = read_json(p)
    items = data.get("chapters") if isinstance(data, dict) else data
    out = []
    for c in items or []:
        if isinstance(c, dict) and "start" in c and str(c.get("title", "")).strip():
            out.append({"start": float(c["start"]), "title": str(c["title"]).strip()})
    return sorted(out, key=lambda c: c["start"]) or None


def normalise(chapters: list[dict], duration: float, min_gap_s: float = 10.0) -> list[dict]:
    """First chapter at 0, none closer than min_gap_s to the previous, none past the end."""
    cs = sorted((dict(c) for c in chapters if 0 <= c["start"] < max(duration, 1e-9)), key=lambda c: c["start"])
    if not cs:
        return []
    cs[0]["start"] = 0.0
    out = [cs[0]]
    for c in cs[1:]:
        if c["start"] - out[-1]["start"] >= min_gap_s and duration - c["start"] >= min_gap_s:
            out.append(c)
    return out


# ---- deriving topic breaks from the transcript --------------------------------------------------

def _blocks(utterances: list[dict], block_s: float) -> list[dict]:
    blocks, cur = [], None
    for u in utterances:
        text = (u.get("text") or "").strip()
        if not text:
            continue
        if cur is None:
            cur = {"start": u["start"], "end": u["end"], "text": text}
        else:
            cur["end"] = u["end"]
            cur["text"] += " " + text
        if cur["end"] - cur["start"] >= block_s:
            blocks.append(cur)
            cur = None
    if cur:
        if blocks and cur["end"] - cur["start"] < block_s / 2:
            blocks[-1]["end"] = cur["end"]
            blocks[-1]["text"] += " " + cur["text"]
        else:
            blocks.append(cur)
    return blocks


def _cos(a: Counter, b: Counter) -> float:
    num = sum(v * b.get(k, 0) for k, v in a.items())
    den = math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values()))
    return num / den if den else 0.0


def derive_breaks(transcript: dict, policy: dict) -> list[dict]:
    """Chapter starts in EPISODE time with draft keyword titles: `[{"start", "title", "keywords"}]`. Empty when the talk is too short
    to split. Lexical cohesion (a TextTiling-style comparison of the words before and after each candidate), spaced at least
    min_chapter_s apart, strongest breaks first."""
    cfg = policy["chapters"]
    blocks = _blocks(transcript.get("utterances") or [], cfg["block_s"])
    k = cfg["window_blocks"]
    if len(blocks) < 2 * k or (blocks[-1]["end"] - blocks[0]["start"]) < 2 * cfg["min_chapter_s"]:
        return []
    bags = [Counter(words_of(b["text"])) for b in blocks]
    sims = []   # (i, similarity across the boundary BEFORE block i)
    for i in range(1, len(blocks)):
        left, right = Counter(), Counter()
        for b in bags[max(0, i - k):i]:
            left.update(b)
        for b in bags[i:i + k]:
            right.update(b)
        sims.append((i, _cos(left, right)))
    vals = [s for _, s in sims]
    mean = sum(vals) / len(vals)
    sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals))
    cands = [(s, i) for i, s in sims if s < mean - 0.5 * sd and all(s <= t for j, t in sims if abs(j - i) <= 2)]
    chosen: list[int] = []
    t0 = blocks[0]["start"]
    for s, i in sorted(cands):
        st = blocks[i]["start"]
        near = [blocks[j]["start"] for j in chosen] + [t0]
        if all(abs(st - x) >= cfg["min_chapter_s"] for x in near) and (blocks[-1]["end"] - st) >= cfg["min_chapter_s"] / 2:
            chosen.append(i)
        if len(chosen) >= cfg["max_chapters"] - 1:
            break
    starts = [0] + sorted(chosen)
    spans = [(starts[n], starts[n + 1] if n + 1 < len(starts) else len(blocks)) for n in range(len(starts))]
    chapter_bags = [sum((bags[i] for i in range(a, b)), Counter()) for a, b in spans]
    df = Counter(w for bag in chapter_bags for w in bag)
    n = len(chapter_bags)
    out = []
    for idx, ((a, b), bag) in enumerate(zip(spans, chapter_bags)):
        scored = sorted(((c * (math.log((n + 1) / df[w]) + 0.5), w) for w, c in bag.items()), key=lambda x: (-x[0], x[1]))
        kws = [w for _, w in scored[:3]]
        out.append({"start": blocks[a]["start"], "keywords": kws, "title": (", ".join(kws).capitalize() if kws else f"Part {idx + 1}")})
    return out


def build(ep: Episode, final_doc: dict | None, duration: float, policy: dict) -> dict:
    """Resolve the chapter list for the final video. Returns {"chapters", "source", "draft_titles", "note"}."""
    given = load_list(ep)
    if given:
        cs = normalise(given, duration, policy["chapters"]["youtube_min_gap_s"])
        return {"chapters": cs, "source": "chapters.json", "draft_titles": False, "note": ""}
    tpath = ep.work / "transcript.json"
    if not tpath.is_file():
        return {"chapters": [], "source": None, "draft_titles": False,
                "note": "no chapters: there is no work/chapters.json and no transcript to derive topic breaks from"}
    if final_doc is None:
        return {"chapters": [], "source": None, "draft_titles": False,
                "note": "no chapters: topic breaks are in episode time and the final EditDoc that maps them to the video was not found; "
                        "write work/chapters.json (start seconds in the final video, title) instead"}
    br = derive_breaks(read_json(tpath), policy)
    if not br:
        return {"chapters": [], "source": None, "draft_titles": False,
                "note": "no chapters: the talk is too short to find topic breaks; write work/chapters.json to add them by hand"}
    cs = [{"start": episode_to_output(final_doc, b["start"]), "title": b["title"]} for b in br]
    return {"chapters": normalise(cs, duration, policy["chapters"]["youtube_min_gap_s"]), "source": "transcript topic breaks",
            "draft_titles": True,
            "note": "chapter titles are machine drafts (the words that mark each section): write work/chapters.json to replace them"}


def youtube_block(chapters: list[dict], policy: dict) -> tuple[str, list[str]]:
    """The timestamp list YouTube parses, plus reasons it will be ignored (if any)."""
    problems = []
    if len(chapters) < policy["chapters"]["youtube_min_chapters"]:
        problems.append(f"YouTube needs at least {policy['chapters']['youtube_min_chapters']} chapters (have {len(chapters)}); it ignores the list otherwise")
    return "\n".join(f"{fmt_ts(c['start'])} {c['title']}" for c in chapters), problems


def ffmetadata(chapters: list[dict], duration: float, tags: dict[str, str]) -> str:
    """FFmpeg metadata file with chapters; the mp3/m4a muxers write them as ID3 CHAP frames / MP4 chapters."""
    esc = lambda s: re.sub(r"([=;#\\\n])", r"\\\1", s)
    lines = [";FFMETADATA1"] + [f"{k}={esc(v)}" for k, v in tags.items() if v]
    for i, c in enumerate(chapters):
        end = chapters[i + 1]["start"] if i + 1 < len(chapters) else duration
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(round(c['start'] * 1000))}", f"END={int(round(end * 1000))}", f"title={esc(c['title'])}"]
    return "\n".join(lines) + "\n"
