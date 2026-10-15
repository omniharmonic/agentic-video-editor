"""How a short OPENS: a title treatment, never the quote.

Creator feedback 2026-10-14: starting a short with a motion-graphic QUOTE card and then having the speaker say that same quote is
awkward. The quote must never be shown as a graphic before or while it is spoken. A short opens with one of:

  none            straight into the clip.
  title_overlay   (default) a lower-area title panel over the first ~2 s of the clip. No extra time: the clip starts at 0.
  title_card      a brief (~1.3 s) full-frame title screen (show name + topic title), then the clip. This adds `card_s` to the
                  short: video holds the clip's first frame under the card, dialogue gets a silent lead-in, captions start after it.

The title is a TOPIC written from the clip ("Why we started this table"), a draft the human edits. Its text comes from, in order,
`--title`, `work/shorts/titles.json` (`{"<slug or clip id>": "..."}`), the clip's own `title`. It must pass the repo's voice critic
(`package.postcopy.critique`: no hype, no emoji, no exclamation, no question in our own words) and must not be the speaker's words
(`max_verbatim_share`): a title that is a quote of the clip is exactly what the creator rejected. A title that fails leaves the short
without an opening (recorded, with the reason), it never ships a bad one.

The quote-card motion template is its own product (`ave motion quote-reel`); `refuse_quote_cards` rejects one placed on a short.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from . import completeness, motion
from .episode import Episode, read_json

MODES = ("none", "title_card", "title_overlay")
TEMPLATE = "short-title"
DEFAULTS: dict[str, Any] = {
    "mode": "title_overlay",
    "show": None,                      # None = episode.json show.name, else the title without its trailing number
    "title_card": {"duration_s": 1.3, "lead_gain_db": -120.0},
    "title_overlay": {"at_s": 0.2, "duration_s": 2.0, "bottom": 0.555},
    "title": {"max_chars": 48, "min_verbatim_words": 4, "max_verbatim_share": 0.7},
}


def config(cfg: dict | None) -> dict:
    out = copy.deepcopy(DEFAULTS)
    for k, v in ((cfg or {}).get("opening") or {}).items():
        out[k] = {**out[k], **v} if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


# ---- the title text -------------------------------------------------------------------------------------------------

def show_name(ep: Episode, cfg: dict) -> str:
    if cfg.get("show"):
        return str(cfg["show"])
    try:
        meta = read_json(ep.meta_path)
    except (OSError, ValueError):
        meta = {}
    show = meta.get("show")
    name = show.get("name") if isinstance(show, dict) else show
    if name:
        return str(name)
    title = str(meta.get("title") or "").strip()
    m = re.match(r"^(?P<n>.*?)[\s#:\-–]*\d{1,4}$", title)
    return (m["n"].strip() if m and m["n"].strip() else title)


def titles_file(ep: Episode) -> Path:
    return ep.work / "shorts" / "titles.json"


def _titles(ep: Episode) -> dict:
    p = titles_file(ep)
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except (OSError, ValueError):
        return {}


def verbatim_share(title: str, spoken: str) -> tuple[int, float]:
    """(longest run of the title's words that appears, in order and contiguous, in the spoken text; that run's share of the title)."""
    t, s = completeness.norm_words(title), completeness.norm_words(spoken)
    if not t:
        return 0, 0.0
    best = 0
    for i in range(len(t)):
        for j in range(len(s)):
            k = 0
            while i + k < len(t) and j + k < len(s) and t[i + k] == s[j + k]:
                k += 1
            best = max(best, k)
    return best, best / len(t)


def critique(ep: Episode, title: str, spoken: str, cfg: dict) -> list[str]:
    """Why this text cannot open a short (empty = fine). Voice: the package voice critic; and it must not be the speaker's words."""
    from .package import collect, postcopy
    tc = cfg["title"]
    out = []
    t = " ".join(str(title or "").split())
    if not t:
        return ["no title text"]
    if len(t) > int(tc["max_chars"]):
        out.append(f"{len(t)} characters (max {tc['max_chars']}): a title is a short topic, not a sentence")
    if t.endswith(".") or re.search(r"[\"“”]", t):
        out.append("a title has no quotation marks and no full stop (it is a label, not a quote)")
    out += postcopy.critique(t, "youtube", collect.load_policy(ep))
    run, share = verbatim_share(t, spoken)
    if run >= int(tc["min_verbatim_words"]) and share >= float(tc["max_verbatim_share"]):
        out.append(f"the title is the speaker's own words ({run} of its {len(completeness.norm_words(t))} words are said in the clip): "
                   "show a topic title, never the quote before or while it is spoken")
    return out


def resolve_title(ep: Episode, clip: dict, cfg: dict, explicit: str | None = None) -> dict:
    """{text, source, problems}: the first candidate that passes; if none does, the first one's problems."""
    spoken = clip.get("text") or ""
    titles = _titles(ep)
    cands = []
    if explicit:
        cands.append(("--title", explicit))
    for key in (clip.get("slug"), clip.get("id")):
        if key and titles.get(key):
            cands.append((f"{titles_file(ep).name}[{key}]", titles[key]))
            break
    if clip.get("title"):
        cands.append(("clip.title", clip["title"]))
    first = None
    for src, text in cands:
        problems = critique(ep, text, spoken, cfg)
        if not problems:
            return {"text": " ".join(text.split()), "source": src, "problems": []}
        first = first or {"text": " ".join(str(text).split()), "source": src, "problems": problems}
        if src == "--title":        # an explicit title that fails is the human's to fix, not ours to replace silently
            break
    return first or {"text": "", "source": None, "problems": ["no title: pass --title or add one to work/shorts/titles.json"]}


# ---- rendering and placing it ---------------------------------------------------------------------------------------

def render_overlay(ep: Episode, props: dict, duration: float, canvas: tuple[int, int], fps: float) -> Path:
    """The title as an alpha .mov in work/motion (reused when the same spec was rendered before)."""
    spec = motion.normalize_spec({"template": TEMPLATE, "props": props, "duration": duration, "fps": fps, "canvas": canvas})
    rel = ep.work.relative_to(ep.root) / "motion" / f"{TEMPLATE}-{motion.spec_hash(spec)}.mov"
    target = ep.root / rel
    if not target.exists():
        motion.render(spec, target)
    return rel


def _lead(doc: dict, lead: float, gain_db: float) -> None:
    """Make room for a title card: everything moves `lead` later; video holds the clip's first frame under the card, dialogue gets a
    silent lead-in (a muted piece of the master just before the clip, so no tool needs a room-tone track). It is marked
    `fill: "muted"` so the caption and word mapping, which skip fills, never treat the muted words as speech."""
    vt = sorted(doc["tracks"]["video"], key=lambda s: s["o0"])
    dt = sorted(doc["tracks"]["dialogue"], key=lambda s: s["o0"])
    for name, segs in (("video", vt), ("dialogue", dt)):
        for s in segs:
            s["o0"], s["o1"] = round(s["o0"] + lead, 4), round(s["o1"] + lead, 4)
    head = vt[0]
    head["o0"] = 0.0
    head["e0"] = round(head["e0"] - lead, 4)
    d0 = dt[0]
    pre = {k: v for k, v in d0.items() if k in ("src", "gain_db")}
    pre.update(id="dlead", o0=0.0, o1=round(lead, 4), e0=round(max(d0["e0"] - lead, 0.0), 4), xfade_ms=0, gain_db=gain_db,
               confidence=1.0, fill="muted", reason="title card: silent lead-in (the clip's speech starts after the card)")
    d0["xfade_ms"] = 0
    doc["tracks"]["dialogue"] = [pre] + dt
    for name in ("music", "overlays"):
        for s in doc["tracks"].get(name) or []:
            s["o0"], s["o1"] = round(s["o0"] + lead, 4), round(s["o1"] + lead, 4)
    doc["duration"] = round(doc["duration"] + lead, 4)
    vt[-1]["o1"] = doc["duration"]
    doc["tracks"]["dialogue"][-1]["o1"] = doc["duration"]


def apply(ep: Episode, doc: dict, clip: dict, cfg: dict, mode: str | None = None, title: str | None = None,
          layout: str = "crop") -> dict:
    """Add the opening to a short doc IN PLACE (before captions are made) and return the sidecar record."""
    oc = config(cfg)
    requested = mode or oc["mode"]
    if requested not in MODES:
        raise SystemExit(f"unknown opening {requested!r}; choose one of {', '.join(MODES)}")
    rec: dict = {"mode": "none", "requested": requested, "draft": True, "title": None, "show": None, "title_source": None,
                 "problems": [], "added_s": 0.0, "overlay": None,
                 "edit": "the title is a draft: change it with --title or work/shorts/titles.json, then re-run `ave shorts build --force` and export"}
    if requested == "none":
        return rec
    got = resolve_title(ep, clip, oc, title)
    rec.update(title=got["text"] or None, title_source=got["source"], problems=got["problems"])
    if got["problems"]:
        rec["note"] = "no opening was added: " + "; ".join(got["problems"])
        return rec
    mode_used = requested
    if requested == "title_card" and layout != "crop":
        mode_used = "title_overlay"
        rec["note"] = f"title_card is only built for the crop layout; {layout} gets the overlay instead"
    show = show_name(ep, oc)
    cv = (doc["canvas"]["w"], doc["canvas"]["h"])
    if mode_used == "title_card":
        dur = float(oc["title_card"]["duration_s"])
        _lead(doc, dur, float(oc["title_card"]["lead_gain_db"]))
        at, props, add = 0.0, {"title": got["text"], "show": show, "mode": "card"}, dur
    else:
        dur = float(oc["title_overlay"]["duration_s"])
        at = float(oc["title_overlay"]["at_s"])
        dur = min(dur, doc["duration"] - at)
        props, add = {"title": got["text"], "show": show, "mode": "overlay", "bottom": float(oc["title_overlay"]["bottom"])}, 0.0
    rel = render_overlay(ep, props, dur, cv, doc["fps"])
    overlays = doc["tracks"].setdefault("overlays", [])
    used = {o.get("id") for o in overlays}
    n = 1
    while f"t{n}" in used:
        n += 1
    layer = max([o.get("layer", 0) for o in overlays] + [motion.MOTION_LAYER_BASE - 1]) + 1
    ov = {"id": f"t{n}", "file": str(rel), "o0": round(at, 3), "o1": round(at + dur, 3), "x": 0, "y": 0, "w": cv[0], "h": cv[1], "layer": layer,
          "kind": "motion", "alpha": True, "template": TEMPLATE, "props": props}
    overlays.append(ov)
    rec.update(mode=mode_used, show=show, added_s=add, overlay={"id": ov["id"], "file": ov["file"], "o0": ov["o0"], "o1": ov["o1"], "props": props})
    return rec


def refuse_quote_cards(doc: dict) -> list[str]:
    """A short never carries the quote-card motion (it shows the words before or while they are spoken): that is `ave motion quote-reel`."""
    return [f"overlay {o.get('id')} uses the quote-card template: a quote graphic must not play before or while the speaker says it. "
            "Use the title opening (config/shorts.yaml opening) and keep quote cards as their own product (`ave motion quote-reel`)."
            for o in doc["tracks"].get("overlays") or [] if o.get("template") == "quote-card"]


def signature(oc: dict, mode: str | None, title: str | None, clip: dict, ep: Episode) -> str:
    blob = json.dumps({"oc": oc, "mode": mode, "title": title, "titles": _titles(ep).get(clip.get("slug")) or _titles(ep).get(clip.get("id")),
                       "clip_title": clip.get("title")}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]
