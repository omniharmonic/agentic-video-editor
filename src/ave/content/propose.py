"""`ave content propose`: the agent's picks in, a validated ContentDoc version out.

The agent chooses what to make and says why (`reason`, `confidence`); this module only checks. Like `ave clips propose`, a pick
is accepted or rejected whole with EVERY problem listed, so the agent can fix and re-run in one go. Items a human already
approved or rejected survive a re-propose untouched; undecided drafts are replaced by the new batch.

A pick:
  {"id": "quote-c01-4x5",                       optional; generated from template, source and format when absent
   "template": "quote-post", "style": "ink", "format": "4x5",
   "source": {"clip": "c01"} | {"writing": "soil.md", "passages": ["p003"]} | "episode",
   "slots": {...}, "platforms": ["instagram"], "caption": "...", "alt_text": "...",
   "assets": ["04_GFX/guest.jpg"], "ai_generated": false,
   "reason": "why this words, this template", "confidence": 0.8}
In a carousel, a slide may carry its own `source` spec; it is replaced by a source id.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from ..episode import Episode
from . import manifest as M
from . import people
from . import registry as R
from . import settings
from . import sources as S


_SHORT = {"quote-post": "quote", "thumbnail": "thumb", "carousel": "car", "reel-card": "card"}


def _source_key(src: dict) -> tuple:
    return (src["type"], src.get("clip") or src.get("file") or "", src.get("text_sha256") or src.get("title"))


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "item"


def _intern_source(doc: dict, src: dict) -> str:
    """The id of an identical source already in the doc, else a new one (q1.. clips, w1.. writing, ep)."""
    for sid, s in doc["sources"].items():
        if _source_key(s) == _source_key(src):
            return sid
    if src["type"] == "episode_meta":
        sid = "ep"
    else:
        prefix = "q" if src["type"] == "clip_quote" else "w"
        n = 1
        while f"{prefix}{n}" in doc["sources"]:
            n += 1
        sid = f"{prefix}{n}"
    doc["sources"][sid] = src
    return sid


def _resolve_slide_sources(ep: Episode, doc: dict, slots: Any, allow_unapproved: bool, allow_incomplete: bool = False) -> Any:
    """Replace any {"source": {...spec}} found inside list items of the slots with the interned source id."""
    slots = copy.deepcopy(slots)
    if isinstance(slots, dict):
        for lst in (v for v in slots.values() if isinstance(v, list)):
            for it in lst:
                if isinstance(it, dict) and isinstance(it.get("source"), (dict,)):
                    it["source"] = _intern_source(doc, S.resolve_pick_source(ep, it["source"], allow_unapproved, allow_incomplete))
    return slots


def _quote_provenance(ep: Episode, reg: R.Registry, item: dict) -> tuple[list[dict], list[str]]:
    """Where in the recording each quote was spoken, and a warning when the attribution names someone else."""
    tpl = reg.get(item["template"])
    slots = R.with_defaults(tpl.slots, item["slots"])
    words = S.transcript_words(ep)
    out, warn = [], []
    for path, text, _ in R.walk_flagged(tpl.slots, slots, "x-verbatim"):
        loc = S.locate(words, text)
        rec = {"slot": path, "located": bool(loc)}
        if loc:
            name = S.speaker_name(ep, loc["speaker"])
            rec.update(t0=loc["t0"], t1=loc["t1"], speaker=loc["speaker"], speaker_name=name, occurrences=loc["occurrences"])
            attr = (slots.get("attribution") or "").strip()
            if attr and name and attr.lower() != name.lower() and loc["speaker"]:
                warn.append(f"{path}: the transcript has {name} ({loc['speaker']}) saying this, but the attribution is {attr!r}. "
                            "Check the speaker before this goes out (diarization is not perfect)")
        elif words:
            warn.append(f"{path}: words not contiguous in the raw transcript (cleanup removed something inside the clip, or it is writing); "
                        "no timestamp recorded")
        out.append(rec)
        dis = S.disfluencies(text)
        if dis:
            warn.append(f"{path}: the words are verbatim but read like typos on a graphic ({', '.join(dict.fromkeys(dis))}). Prefer a cleaner line; "
                        "do not edit the words")
    return out, warn


def propose(ep: Episode, picks: Any, doc_id: str = "main", allow_unapproved: bool = False, reg: R.Registry | None = None,
            allow_incomplete: bool = False) -> dict:
    """Validate picks and write the accepted ones as the next ContentDoc version. Nothing is rendered or published."""
    cfg = settings.load()
    reg = reg or R.load_registry(cfg=cfg)
    if isinstance(picks, dict):
        picks = picks.get("items") or picks.get("picks")
    if not isinstance(picks, list) or not picks:
        raise SystemExit('picks.json must be a non-empty JSON list of picks (or {"items": [...]}). See skills/ave-content-plan.')
    prev = M.load(M.latest_path(ep, doc_id)) if M.latest_path(ep, doc_id) else None
    doc = M.new_doc(ep, doc_id)
    kept: list[dict] = []
    if prev:
        doc["sources"] = copy.deepcopy(prev["sources"])
        kept = [it for it in prev["items"] if it["review"]["status"] in ("approved", "rejected")]
        doc["notes"] = prev.get("notes", "")
    used = {it["id"] for it in kept}
    accepted, rejected, added = [], [], []
    limit = int(cfg["planning"]["max_items_per_episode"])
    for n, pick in enumerate(picks):
        if not isinstance(pick, dict):
            rejected.append({"index": n, "id": None, "problems": ["a pick must be an object"]})
            continue
        problems: list[str] = []
        tid = pick.get("template")
        tpl = reg.templates.get(tid)
        if tpl is None:
            rejected.append({"index": n, "id": pick.get("id"), "problems": [f"template {tid!r} is not in the registry (have: {', '.join(reg.ids())})"]})
            continue
        trial = M.new_doc(ep, doc_id)
        trial["sources"] = copy.deepcopy(doc["sources"])
        src_id = None
        spec = pick.get("source")
        try:
            if spec is not None:
                src_id = _intern_source(trial, S.resolve_pick_source(ep, spec, allow_unapproved, allow_incomplete))
            slots = _resolve_slide_sources(ep, trial, pick.get("slots", {}), allow_unapproved, allow_incomplete)
        except (ValueError, KeyError) as e:
            rejected.append({"index": n, "id": pick.get("id"), "problems": [str(e)]})
            continue
        fmt = pick.get("format") or tpl.formats[0]
        words = _slug(str(slots.get("quote") or slots.get("title") or (slots.get("cover") or {}).get("title") or ""))[:14].strip("-")
        iid = pick.get("id") or "-".join(x for x in (_SHORT.get(tid, tid), src_id, words, fmt) if x)
        base, k = iid, 2
        while iid in used or iid in {i["id"] for i in added}:
            iid, k = f"{base}-{k}", k + 1
        item = M.new_item(iid, tid, pick.get("style") or "ink", fmt, slots, source=src_id,
                          renderer=pick.get("renderer") or tpl.default_renderer, platforms=pick.get("platforms") or tpl.default_platforms,
                          caption=pick.get("caption") or "", alt_text=pick.get("alt_text"), reason=(pick.get("reason") or "").strip(),
                          confidence=pick.get("confidence", None), assets=pick.get("assets") or [], ai_generated=bool(pick.get("ai_generated")))
        trial["items"] = [item]
        problems += M.validate(trial, reg)
        if not allow_incomplete:
            twords = S.transcript_words(ep)
            for path_, text_, _ in R.walk_flagged(tpl.slots, R.with_defaults(tpl.slots, item["slots"]), "x-verbatim"):
                problems += [f"{path_}: a quote must begin and end on a complete sentence, but it " + x for x in S.quote_completeness(ep, text_, twords)]
        problems += _platform_fit(item, reg, cfg)
        problems += [f"slots.{k} repeats the show name already in slots.show; leave {k} empty (the show prints once, in its own slot)"
                     for k in R.show_duplicates(R.with_defaults(tpl.slots, item["slots"]), tpl.slots)]
        if len(kept) + len(added) + 1 > limit:
            problems.append(f"the plan would hold {len(kept) + len(added) + 1} items; the limit is {limit} (config/content.yaml planning.max_items_per_episode). Drop the weakest first")
        if problems:
            rejected.append({"index": n, "id": iid, "problems": problems})
            continue
        item["slots"] = R.normalize_slots(tpl.slots, item["slots"], people.load_speakers(ep))   # a host's name and CO-HOST title, role -> title
        prov, warn = _quote_provenance(ep, reg, item)
        item["provenance"]["quotes"] = prov
        item["warnings"] = warn
        doc["sources"] = trial["sources"]
        added.append(item)
        accepted.append({"id": iid, "template": tid, "style": item["style"], "format": fmt, "source": src_id, "warnings": warn})
    doc["items"] = kept + added
    keep_src = {i["source"] for i in doc["items"] if i.get("source")}
    for it in doc["items"]:
        for lst in (v for v in it["slots"].values() if isinstance(v, list)):
            keep_src |= {x.get("source") for x in lst if isinstance(x, dict) and x.get("source")}
    doc["sources"] = {k: v for k, v in doc["sources"].items() if k in keep_src}
    out = {"accepted": accepted, "rejected": rejected, "kept_human_decisions": [i["id"] for i in kept]}
    if added or not prev:
        if not doc["items"]:
            out["note"] = "nothing accepted, so no new version was written"
        else:
            path = M.save_new_version(ep, doc, reg)
            out.update(doc=str(path), version=M.list_versions(ep, doc_id)[-1])
    out["next"] = ("Fix the rejected picks and re-run propose" if rejected else "ave content render <project>   (then: ave content review <project>)")
    return out


def _platform_fit(item: dict, reg: R.Registry, cfg: dict) -> list[str]:
    """A format a platform cannot take (aspect or size from config/platforms.yaml `image:`) is a planning error, caught early."""
    from ..publish.adapters import base
    plats = base.load_platforms()
    w, h = reg.get(item["template"]).size(item["format"])
    out = []
    for p in item.get("platforms") or []:
        spec = (plats.get(p) or {}).get("image")
        if spec:
            out += [f"{p}: {m}" for m in base.image_dimension_problems(spec, w, h, carousel=item["template"] == "carousel")]
    return out
