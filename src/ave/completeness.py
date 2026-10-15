"""Complete statements are a HARD GUARANTEE for anything that cuts a clip into a product (shorts, quote picks).

The clip finder (`clips.py`, `segment.py`) already snaps clips OUT to where a thought begins and ends and records
`start_complete` / `end_complete` / `complete_statement`, `needs_context`, `context_variant` on every clip. Those fields
are advice there. Here they are a gate (creator feedback 2026-10-14: clip selection must always start and end on complete
sentences/phrases; the trial shorts were cut from old picks that predate the logic).

  gate(ep, clip, policy)    -> (clip, record). The clip to cut (the original, or its `context_variant` when the original is
                               incomplete and the variant is a complete statement) and a record for the sidecar. Raises
                               SystemExit with every reason when the clip cannot be cut cleanly. `allow_incomplete=True`
                               is the explicit override: it is recorded in the record, and the caller logs it.
  audit_edges(...)          -> the boundary judge re-run on the exact words at the clip's edges (does not trust stored flags).
  snap_edges(doc, words)    -> moves the cut itself to word gaps (with pad) so no word is ever sliced.

Pure except `audit_edges` (reads the clip's source edit and the transcript).
"""

from __future__ import annotations

import copy
import re
from typing import Any

from . import editdoc, segment
from .episode import Episode, read_json

EPS = 1e-3

DEFAULTS: dict[str, Any] = {
    "require": True,              # False only for tests of the old behaviour; the CLI has no switch for it
    "use_variant": True,          # an incomplete clip with a `context_variant` is cut from the variant if the variant is complete
    "needs_context": "warn",      # warn: cut as picked, record the reason in the sidecar | variant: use `context_variant` when there is one
    "audit": True,                # re-judge the edges on the source edit's words instead of trusting stored flags alone
    "pad_start_s": 0.10,          # a cut starts this long before the first word (never into the previous word)
    "pad_end_s": 0.18,            # ...and ends this long after the last word (never into the next word)
    "min_gap_s": 0.0,
}


def config(cfg: dict | None) -> dict:
    out = copy.deepcopy(DEFAULTS)
    out.update({k: v for k, v in ((cfg or {}).get("completeness") or {}).items() if k in DEFAULTS})
    return out


# ---- stored flags ---------------------------------------------------------------------------------------------------

def flag_problems(clip: dict) -> list[str]:
    """Why the clip's OWN fields say it cannot be cut as it stands (empty = complete)."""
    out = []
    if clip.get("stale"):
        out.append("the clip is stale (picked from an older edit: " + str(clip.get("stale_reason") or "its sentences changed") + ")")
    s, e, c = clip.get("start_complete"), clip.get("end_complete"), clip.get("complete_statement")
    if s is None or e is None or c is None:
        out.append("completeness was never assessed for this clip (it predates the complete-statement logic): "
                   "re-run `ave clips prepare` and `ave clips propose`")
        return out
    if not s:
        out.append("starts mid-sentence" + (": " + clip["completeness_note"] if clip.get("completeness_note") else ""))
    if not e:
        out.append("ends mid-thought" + (": " + clip["completeness_note"] if clip.get("completeness_note") and s else ""))
    if not c and s and e:
        out.append("complete_statement is false" + (": " + clip["completeness_note"] if clip.get("completeness_note") else ""))
    return out


def with_variant(clip: dict) -> dict | None:
    """The clip re-bounded to its `context_variant` (same clip started 1-2 sentences earlier), or None. The select is cut anew."""
    v = clip.get("context_variant")
    if not v:
        return None
    out = copy.deepcopy(clip)
    out.update(start=v["start"], end=v["end"], duration=v["duration"], sentence_ids=list(v["sentence_ids"]), edit=None,
               text=(v.get("added_text", "") + " " + clip.get("text", "")).strip(), needs_context=False,
               start_complete=True, boundary="context_variant", context_variant=None,
               completeness_note=None)
    out["variant_of"] = {"start": clip["start"], "end": clip["end"], "added_text": v.get("added_text"), "note": v.get("note")}
    return out


# ---- re-judging the edges on the real words -------------------------------------------------------------------------

def audit_edges(ep: Episode, clip: dict, rubric: dict | None = None) -> dict | None:
    """Re-run `segment.Segmenter` on the words at the clip's edges in the edit it was picked from. None when that cannot be done
    (no `source`, edit missing): the stored flags then stand alone and the record says the audit was skipped."""
    from . import clips as C
    src = clip.get("source")
    if not src:
        return None
    p = editdoc.path_for(ep, src["stage"], src["version"], src.get("name"))
    if not p.exists():
        return None
    try:
        rubric = rubric or C.load_rubric(ep)
    except SystemExit:
        return None
    doc = editdoc.load(p)
    words = C.mapped_words(ep, doc)
    seg = segment.Segmenter(words, {f.lower() for f in rubric["snap"]["filler_words"]}, rubric)
    inside = [k for k, w in enumerate(words) if w["start"] >= clip["start"] - 0.05 and w["end"] <= clip["end"] + 0.05]
    if not inside:
        return None
    a, b = inside[0], inside[-1]
    while a < b and seg._filler(a):
        a += 1
    while b > a and seg._filler(b):
        b -= 1
    v = seg.verdict(a, b)
    v.update(first_words=" ".join(w["w"] for w in words[a:a + 6]), last_words=" ".join(w["w"] for w in words[max(b - 5, a):b + 1]),
             needs_context_reason=seg.needs_context(a))
    return v


# ---- the gate -------------------------------------------------------------------------------------------------------

def record_of(clip: dict, boundary: str, audit: dict | None, problems: list[str], override: bool, cfg: dict) -> dict:
    return {
        "complete": not problems, "boundary": boundary, "override": bool(override), "problems": list(problems),
        "start_complete": clip.get("start_complete"), "end_complete": clip.get("end_complete"),
        "complete_statement": clip.get("complete_statement"), "note": clip.get("completeness_note"),
        "needs_context": bool(clip.get("needs_context")), "needs_context_reason": clip.get("needs_context_reason"),
        "audit": ({k: audit[k] for k in ("start_complete", "end_complete", "complete_statement", "completeness_note", "first_words", "last_words")}
                  if audit else None),
        "audit_ran": audit is not None,
        **({"variant_of": clip["variant_of"]} if clip.get("variant_of") else {}),
        "policy": {k: cfg[k] for k in ("use_variant", "needs_context", "audit")},
    }


def gate(ep: Episode, clip: dict, cfg: dict | None = None, allow_incomplete: bool = False, rubric: dict | None = None) -> tuple[dict, dict]:
    """Decide what may be cut. Returns (clip to cut, completeness record). The returned clip is `clip` itself, or its
    `context_variant` boundary (with `edit: None`: the caller cuts the select for the new range).

    Order: stored flags and the edge audit judge the clip as picked. If it is incomplete and a variant exists and the variant
    passes, the variant is used (recorded as `boundary: context_variant`). Otherwise refuse, unless `allow_incomplete`,
    which cuts the clip as picked and records every reason with `override: true`."""
    p = config(cfg)
    if not p["require"]:
        return clip, record_of(clip, "clip", None, [], False, p)
    audit = audit_edges(ep, clip, rubric) if p["audit"] else None
    problems = flag_problems(clip)
    if audit and not audit["complete_statement"] and not any("audit" in x for x in problems):
        problems.append("edge audit on the real words: " + (audit["completeness_note"] or "incomplete"))
    boundary = "clip"
    use = clip
    if problems and p["use_variant"] and not clip.get("stale"):
        v = with_variant(clip)
        if v is not None:
            a2 = audit_edges(ep, v, rubric) if p["audit"] else None
            if clip.get("end_complete") is not False and (a2 is None or a2["complete_statement"]):
                use, boundary, audit, problems = v, "context_variant", a2, []
    elif not problems and clip.get("needs_context") and p["needs_context"] == "variant":
        v = with_variant(clip)
        if v is not None:
            a2 = audit_edges(ep, v, rubric) if p["audit"] else None
            if a2 is None or a2["complete_statement"]:
                use, boundary, audit = v, "context_variant", a2
    if problems and not allow_incomplete:
        raise SystemExit(
            f"clip {clip['id']} cannot be cut: it is not a complete statement.\n  - " + "\n  - ".join(problems) +
            "\nPick a clip that passes (`ave clips list` shows complete_statement), re-propose, or - only if the human accepts a mid-thought "
            "edge - pass --allow-incomplete (recorded in the sidecar and PROGRESS.md).")
    return use, record_of(use, boundary, audit, problems, bool(problems), p)


# ---- the cut itself: never slice a word -----------------------------------------------------------------------------

def _gap_edge(words: list[dict], idx: int, side: str, pad: float) -> float:
    """Episode time of a cut that sits in the gap before words[idx] (side='start') or after it (side='end'), `pad` seconds
    from the word but never closer to the neighbour than the middle of the gap."""
    w = words[idx]
    if side == "start":
        want = w["start"] - pad
        if idx > 0:
            mid = (words[idx - 1]["end"] + w["start"]) / 2
            want = max(want, mid) if words[idx - 1]["end"] <= w["start"] else w["start"]
        return max(want, 0.0)
    want = w["end"] + pad
    if idx + 1 < len(words):
        mid = (w["end"] + words[idx + 1]["start"]) / 2
        want = min(want, mid) if w["end"] <= words[idx + 1]["start"] else w["end"]
    return want


def _speech(doc: dict) -> list[dict]:
    return sorted((s for s in doc["tracks"]["dialogue"] if not s.get("fill") and "e0" in s), key=lambda s: s["o0"])


def slices_word(words: list[dict], t: float, eps: float = 0.02) -> dict | None:
    """The word (if any) that episode time `t` falls strictly inside."""
    lo, hi = 0, len(words)
    while lo < hi:
        m = (lo + hi) // 2
        if words[m]["end"] <= t:
            lo = m + 1
        else:
            hi = m
    if lo < len(words) and words[lo]["start"] + eps < t < words[lo]["end"] - eps:
        return words[lo]
    return None


def edge_report(doc: dict, words: list[dict]) -> dict:
    """Where the doc's cuts fall relative to the transcript's words: the first and last edge and every interior dialogue seam."""
    sp = _speech(doc)
    out: dict = {"start": None, "end": None, "interior_slices": []}
    if not sp:
        return out
    t0, t1 = sp[0]["e0"], sp[-1]["e0"] + (sp[-1]["o1"] - sp[-1]["o0"])
    for key, t in (("start", t0), ("end", t1)):
        w = slices_word(words, t)
        out[key] = {"episode_t": round(t, 3), "slices_word": w["w"] if w else None}
    for a, b in zip(sp, sp[1:]):
        for t, which in ((a["e0"] + (a["o1"] - a["o0"]), "end of " + str(a.get("id"))), (b["e0"], "start of " + str(b.get("id")))):
            w = slices_word(words, t)
            if w:
                out["interior_slices"].append({"at_o": round(a["o1"], 3), "episode_t": round(t, 3), "edge": which, "word": w["w"]})
    return out


def snap_edges(doc: dict, words: list[dict], cfg: dict | None = None) -> dict:
    """Move the first and last cut of a short/select doc to word gaps with pad, IN PLACE (episode times in `words`).

    The first spoken word is the first word whose middle is after the cut; the last is the last whose middle is before it. The
    new start is `pad_start_s` before that word, the new end `pad_end_s` after the last, and never past the middle of the gap to the
    neighbouring word, so no word (kept or neighbouring, removed by the rough cut or not) is ever sliced. All times after the
    start shift with it; video, dialogue, music and overlays stay in sync. Returns a report for the sidecar."""
    p = config(cfg)
    sp = _speech(doc)
    rep: dict = {"snapped": False, "pad_start_s": p["pad_start_s"], "pad_end_s": p["pad_end_s"]}
    if not sp or not words:
        rep["note"] = "no speech segments or no transcript words: edges left as they were"
        return rep
    e_start, e_end = sp[0]["e0"], sp[-1]["e0"] + (sp[-1]["o1"] - sp[-1]["o0"])
    mids = [(w["start"] + w["end"]) / 2 for w in words]
    import bisect
    fi = bisect.bisect_left(mids, e_start - 1e-6)
    li = bisect.bisect_right(mids, e_end + 1e-6) - 1
    if fi >= len(words) or li < fi:
        rep["note"] = "no transcript word inside the cut: edges left as they were"
        return rep
    new_s = _gap_edge(words, fi, "start", p["pad_start_s"])
    new_e = _gap_edge(words, li, "end", p["pad_end_s"])
    ds, de = new_s - e_start, new_e - e_end
    rep.update(first_word=words[fi]["w"], last_word=words[li]["w"],
               start={"before": round(e_start, 3), "after": round(new_s, 3), "delta": round(ds, 3), "was_slicing": (slices_word(words, e_start) or {}).get("w")},
               end={"before": round(e_end, 3), "after": round(new_e, 3), "delta": round(de, 3), "was_slicing": (slices_word(words, e_end) or {}).get("w")})
    if abs(ds) < 1e-4 and abs(de) < 1e-4:
        rep["snapped"] = True
        return rep
    keep0, keep1 = max(ds, 0.0), doc["duration"] + min(de, 0.0)      # output range that survives (old time)
    front, back = max(-ds, 0.0), max(de, 0.0)                            # time added before / after
    new_dur = round(keep1 - keep0 + front + back, 4)
    for name in ("video", "dialogue"):
        out: list[dict] = []
        for s in sorted(doc["tracks"].get(name) or [], key=lambda x: x["o0"]):
            a, b = max(s["o0"], keep0), min(s["o1"], keep1)
            if b - a <= 1e-4:
                continue
            n = dict(s)
            if "e0" in s:
                n["e0"] = round(s["e0"] + (a - s["o0"]), 4)
            n["o0"], n["o1"] = round(a - keep0 + front, 4), round(b - keep0 + front, 4)
            out.append(n)
        if out:
            out[0]["o0"] = 0.0
            if front and "e0" in out[0]:
                out[0]["e0"] = round(out[0]["e0"] - front, 4)
            out[-1]["o1"] = new_dur
        doc["tracks"][name] = out
    for name in ("music", "overlays"):
        kept = []
        for s in doc["tracks"].get(name) or []:
            s["o0"], s["o1"] = max(round(s["o0"] - ds, 4), 0.0), min(round(s["o1"] - ds, 4), new_dur)
            if s["o1"] - s["o0"] > 1e-3:
                kept.append(s)
        doc["tracks"][name] = kept
    doc["duration"] = new_dur
    rep["snapped"] = True
    return rep


def norm_words(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9']+", text.lower().replace("’", "'")) if t]


def transcript_words(ep: Episode) -> list[dict]:
    return sorted(read_json(ep.artifact("transcript.json"))["words"], key=lambda w: w["start"])
