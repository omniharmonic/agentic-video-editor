"""Cheap, explainable evidence for recall-first clip proposing: tags, hook position, ends-on-sentence, a heuristic 0-10 score
per rubric criterion, and a one-line reason. Pure functions over a sentence table (`ave.clips.Sentences`); no I/O.

None of this is a verdict. It decides what is SURFACED to the human (everything above a low floor) and how it is ordered; the
agent's own 0-10 scores replace the heuristic ones whenever it picks a section itself.
"""

from __future__ import annotations

import copy
import hashlib
import re
from typing import Any

TAGS = ("hook", "story", "claim", "funny", "tangent", "quotable")
_NUM = re.compile(r"\b\d[\d,.]*\b|\b(?:percent|million|billion|thousand|hundred)\b", re.I)
_AXIOM = re.compile(r"\b(?:is|are|isn't|aren't|means)\s+(?:a|an|the|not|just|only)\b", re.I)

DEFAULTS: dict[str, Any] = {
    "floor": 30, "max_per_session": 80, "alternates": 3, "tangent_penalty": 5, "top_n": None, "min_score": None,
    "quotable": {"min_words": 6, "max_words": 18},
    "tags": {
        "hook": ["what if", "here's the thing", "the truth is", "the secret", "most people", "imagine", "have you ever", "why do", "why is", "why are"],
        "story": ["i remember", "one day", "years ago", "when i was", "we spent", "the day we", "the first time", "i was", "we were", "it was", "i learned", "story"],
        "claim": ["always", "never", "every single", "the problem is", "the reason", "matters more", "most people", "nobody", "everyone", "actually", "wrong", "beats", "the whole point", "is just", "instead of"],
        "funny": ["haha", "hahaha", "lol", "laugh", "funny", "joke", "hilarious", "ridiculous", "absurd", "[laughter]"],
        "tangent": ["by the way", "anyway", "side note", "off topic", "tangent", "speaking of", "random", "unrelated", "totally different"],
    },
    "points": {
        "hook": {"base": 1.0, "clean_open": 1.5, "hook_tag_in_first_two": 3.5, "early_hook_3s": 2.0, "early_hook_8s": 1.0},
        "arc": {"base": 0.5, "story": 4.0, "builds_to_complete": 1.5, "speaker_turn": 1.0, "four_or_more_sentences": 1.0},
        "insight": {"base": 0.5, "claim": 3.5, "quotable": 2.5, "specific_number": 1.0},
        "emotion": {"base": 0.0, "funny": 5.0, "exclamation_each": 1.5, "exclamation_max": 3.0},
        "shareability": {"base": 0.5, "quotable": 3.0, "hook_tag": 1.5, "clean_open": 1.5, "story": 1.0},
        "ending": {"base": 1.0, "ends_complete": 4.0, "ends_on_claim_or_quotable": 3.0},
        "speech_quality": {"base": 1.0, "clean": 7.0, "filler_penalty_per_unit_density": 60, "long_pause_penalty": 2.0},
    },
}


def config(rubric: dict) -> dict:
    """Packaged defaults deep-merged under the rubric's `recall` section (an older policy file without one still works)."""
    out = copy.deepcopy(DEFAULTS)
    for k, v in (rubric.get("recall") or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            for k2, v2 in v.items():
                if isinstance(v2, dict) and isinstance(out[k].get(k2), dict):
                    out[k][k2].update(v2)
                else:
                    out[k][k2] = v2
        else:
            out[k] = v
    return out


def floor(rubric: dict) -> float:
    """The lowest total a section needs to be surfaced or a pick needs to be accepted: recall.floor, else the legacy accept_min_score."""
    r = rubric.get("recall") or {}
    if r.get("floor") is not None:
        return float(r["floor"])
    return float(rubric.get("accept_min_score", DEFAULTS["floor"]))


def _compile(cfg: dict) -> dict[str, re.Pattern]:
    out = {}
    for tag, phrases in cfg["tags"].items():
        parts = [re.escape(p.lower()) for p in phrases if p]
        if parts:
            out[tag] = re.compile(r"(?<![a-z0-9])(?:" + "|".join(parts) + r")(?![a-z0-9])")
    return out


def sentence_tags(S: Any) -> list[set[str]]:
    """Per sentence: which of TAGS it carries. Cached on the table."""
    cached = getattr(S, "_stags", None)
    if cached is not None:
        return cached
    cfg = config(S.rubric)
    pats = _compile(cfg)
    q = cfg["quotable"]
    out = []
    for s in S.items:
        low = s["text"].lower().replace("’", "'")
        f = s["features"]
        t: set[str] = set()
        for tag, pat in pats.items():
            if pat.search(low):
                t.add(tag)
        if f["is_question"]:
            t.add("hook")
        if (f["ends_complete"] and q["min_words"] <= f["n_words"] <= q["max_words"] and not f["is_question"]
                and not f["refs_prior_context"] and ("claim" in t or _AXIOM.search(low) or " but " in low)):
            t.add("quotable")
        out.append(t)
    S._stags = out
    return out


def window_profile(S: Any, i: int, j: int, sn: dict) -> dict:
    """Tags, hook position (seconds from the clip's start; None if nothing hooky), ends-on-sentence, speaker balance, text hash."""
    stags = sentence_tags(S)
    run = list(range(i, j + 1))
    tags: set[str] = set()
    for k in run:
        tags |= stags[k] - {"hook"}
    if any("hook" in stags[k] for k in run[:2]):
        tags.add("hook")
    hook_pos = None
    for k in run:
        if stags[k] & {"hook", "claim", "quotable"}:
            hook_pos = round(max(0.0, S.items[k]["start"] - sn["start"]), 1)
            break
    words = S.words[sn["first_word"]:sn["last_word"] + 1]
    counts: dict[Any, int] = {}
    for w in words:
        counts[w.get("speaker")] = counts.get(w.get("speaker"), 0) + 1
    balance = round(max(counts.values()) / len(words), 2) if words else 1.0
    text = " ".join(re.sub(r"[^a-z0-9']", "", w["w"].lower()) for w in words)
    v = S.seg.verdict(sn["first_word"], sn["last_word"])
    nc = S.seg.needs_context(sn["first_word"])
    return {"tags": [t for t in TAGS if t in tags], "hook_pos_s": hook_pos, "ends_on_sentence": bool(v["end_complete"]),
            "speaker_balance": balance, "text_sha": hashlib.sha1(text.encode()).hexdigest()[:16],
            "start_complete": v["start_complete"], "end_complete": v["end_complete"], "complete_statement": v["complete_statement"],
            "completeness_note": v["completeness_note"], "needs_context": bool(nc), "needs_context_reason": nc}


def heuristic_scores(S: Any, i: int, j: int, f: dict, prof: dict) -> dict[str, float]:
    """0-10 per criterion from evidence, using the points table in the rubric's `recall.points`."""
    P = config(S.rubric)["points"]
    stags = sentence_tags(S)
    run = S.items[i:j + 1]
    tags = set(prof["tags"])
    clean = not f["starts_mid_thought"] and not f["refs_prior_context"] and f.get("start_complete", True)
    hook_first_two = any("hook" in stags[k] for k in range(i, min(j, i + 1) + 1))
    pos = prof["hook_pos_s"]
    last_t = stags[j]
    text = " ".join(s["text"] for s in run)
    excl = sum(1 for s in run if s["text"].rstrip().endswith("!"))

    def clamp(x: float) -> float:
        return round(max(0.0, min(10.0, x)), 2)

    p = P["hook"]
    hook = p["base"] + (p["clean_open"] if clean else 0) + (p["hook_tag_in_first_two"] if hook_first_two else 0)
    if pos is not None:
        hook += p["early_hook_3s"] if pos <= 3.0 else (p["early_hook_8s"] if pos <= 8.0 else 0)
    p = P["arc"]
    arc = (p["base"] + (p["story"] if "story" in tags else 0) + (p["builds_to_complete"] if len(run) >= 3 and prof["ends_on_sentence"] else 0)
           + (p["speaker_turn"] if f["speaker_turns"] >= 1 else 0) + (p["four_or_more_sentences"] if len(run) >= 4 else 0))
    p = P["insight"]
    insight = (p["base"] + (p["claim"] if "claim" in tags else 0) + (p["quotable"] if "quotable" in tags else 0)
               + (p["specific_number"] if _NUM.search(text) else 0))
    p = P["emotion"]
    emotion = p["base"] + (p["funny"] if "funny" in tags else 0) + min(p["exclamation_max"], excl * p["exclamation_each"])
    p = P["shareability"]
    share = (p["base"] + (p["quotable"] if "quotable" in tags else 0) + (p["hook_tag"] if "hook" in tags else 0)
             + (p["clean_open"] if clean else 0) + (p["story"] if "story" in tags else 0))
    p = P["ending"]
    ending = (p["base"] + (p["ends_complete"] if prof["ends_on_sentence"] else 0)
              + (p["ends_on_claim_or_quotable"] if last_t & {"claim", "quotable"} and prof["ends_on_sentence"] else 0))
    p = P["speech_quality"]
    speech = p["base"] + p["clean"] - f["filler_density"] * p["filler_penalty_per_unit_density"] - (p["long_pause_penalty"] if f["long_pause"] else 0)
    return {"hook": clamp(hook), "arc": clamp(arc), "insight": clamp(insight), "emotion": clamp(emotion), "shareability": clamp(share),
            "ending": clamp(ending), "speech_quality": clamp(speech)}


def reason_line(prof: dict, f: dict, duration: float, penalties: list[dict]) -> str:
    """One line a human can scan: what it is, where the hook lands, how it ends, what is wrong."""
    parts = []
    parts.append(" + ".join(prof["tags"]) if prof["tags"] else "plain section, no strong signal")
    if prof["hook_pos_s"] is not None:
        parts.append(f"hook at {prof['hook_pos_s']:g}s")
    parts.append("ends on a full sentence" if prof["ends_on_sentence"] else "ends mid-sentence")
    parts.append(f"{duration:.0f}s")
    flags = [p["name"].replace("_", " ") for p in penalties]
    if flags:
        parts.append("watch: " + ", ".join(flags))
    return "; ".join(parts)
