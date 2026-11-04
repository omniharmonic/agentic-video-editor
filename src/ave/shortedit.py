"""Two small edits an agent can make to a finished short without rebuilding it from scratch: `ave shorts recut` and `ave shorts retitle`.

`ave shorts` already builds (clip -> short EditDoc, with the complete-statement gate, word-gap snapping and the title opening), previews and
exports. What it could not do is the two things an editor does next to a short that is nearly right:

  recut     move the START and/or END of a short inward by whole WORDS (or by seconds, snapped to word edges), keeping the complete-statement
            gate. The new range becomes a new `select.<slug>` version cut from the exact edit the clip was picked from, and the short is built
            from it through `shorts.build_short` (so reframing, captions, the opening and the gate all run as for any build). The clip in
            `work/clips.json` is NOT touched: the human approved that clip, and a trimmed cut is flagged `needs_review` ("recut") in the doc and
            the sidecar until they have looked at it. Moving a boundary OUTWARD is the sentence-level `ave clips decide --nudge` (it re-judges
            completeness and rescoring), not this.
  retitle   swap the title overlay (the topic the short opens with) in a NEW short version, without redoing the crops or the captions. The new
            title must pass the same critic as at build time (no hype, no quote of the speaker). `work/shorts/titles.json` is updated so a later
            `ave shorts build --force` keeps it.

Both write new versions and never overwrite; neither renders (that is `ave shorts export`), posts, approves or spends.
"""

from __future__ import annotations

import copy
import json
import time

from . import clips as C, completeness, editdoc, opening, shorts
from .episode import Episode, write_json


def _clip(ep: Episode, slug: str, name: str | None, allow_unapproved: bool) -> dict:
    hit = shorts.select_clips(ep, slug, False, name=name, allow_unapproved=allow_unapproved)
    return copy.deepcopy(hit[0])


def _prev_short(ep: Episode, slug: str) -> tuple[dict, int]:
    vs = editdoc.list_versions(ep, "short", slug)
    if not vs:
        raise SystemExit(f"no short for {slug}; run `ave shorts build {ep.root} --clip {slug}` first")
    return editdoc.load(editdoc.latest_path(ep, "short", slug)), vs[-1]


def _source(ep: Episode, clip: dict) -> dict:
    """The exact edit the clip was picked from: a session's clips record it, a single-session episode's clips share `clips.json` `source`."""
    src = clip.get("source") or C.load_clips(ep)["source"]
    return {**src, "name": src.get("name")}


def _source_doc(ep: Episode, clip: dict, src: dict) -> dict:
    data = C.load_clips(ep)
    docs = C._source_docs(ep, data["rubric"], [{**clip, "source": src}])      # refuses a stale clip (the edit or transcript moved since it was picked)
    return next(iter(docs.values()))


# ---- recut --------------------------------------------------------------------------------------------------------------------

def _inside(words: list[dict], clip: dict) -> list[int]:
    return [k for k, w in enumerate(words) if w["start"] >= clip["start"] - 0.05 and w["end"] <= clip["end"] + 0.05]


def plan_recut(words: list[dict], clip: dict, drop_start_words: int = 0, drop_end_words: int = 0,
               trim_start_s: float = 0.0, trim_end_s: float = 0.0) -> dict:
    """The new first and last word of a clip after moving each end inward, in the source edit's output time. Pure.

    Seconds are measured from the clip's own first/last word and then snapped to the next word edge INSIDE the clip, so a word is never cut:
    `trim_start_s` keeps the first word that begins at or after that point, `trim_end_s` the last word that ends at or before it."""
    if min(drop_start_words, drop_end_words) < 0 or min(trim_start_s, trim_end_s) < 0:
        raise SystemExit("recut only moves a boundary INWARD (values are >= 0). To start earlier or end later, nudge the clip by sentences: "
                         "`ave clips decide <ep> <id> --verdict keep` with a nudge in a decisions file, then `ave shorts build --force`.")
    inside = _inside(words, clip)
    if not inside:
        raise SystemExit(f"clip {clip['id']}: no transcript words fall inside {clip['start']:.2f}-{clip['end']:.2f}s of its source edit")
    a, b = inside[0], inside[-1]
    a2, b2 = a + int(drop_start_words), b - int(drop_end_words)
    if trim_start_s:
        t = words[a]["start"] + trim_start_s
        a2 = max(a2, next((k for k in range(a, b + 1) if words[k]["start"] >= t - 1e-6), b + 1))
    if trim_end_s:
        t = words[b]["end"] - trim_end_s
        b2 = min(b2, next((k for k in range(b, a - 1, -1) if words[k]["end"] <= t + 1e-6), a - 1))
    if a2 > b2:
        raise SystemExit("that trim leaves no words")
    return {"first": a2, "last": b2, "old_first": a, "old_last": b, "start": words[a2]["start"], "end": words[b2]["end"],
            "dropped_start": [w["w"] for w in words[a:a2]], "dropped_end": [w["w"] for w in words[b2 + 1:b + 1]]}


def recut(ep: Episode, slug: str, *, drop_start_words: int = 0, drop_end_words: int = 0, trim_start_s: float = 0.0, trim_end_s: float = 0.0,
          name: str | None = None, allow_unapproved: bool = False, allow_incomplete: bool = False, dry_run: bool = False, by: str | None = None,
          detector=None) -> dict:
    """Move a short's start and/or end inward on word boundaries. `dry_run` says what would change and whether the new edges are complete
    statements, and writes nothing. Otherwise: a new `select.<slug>` version and a new `short.<slug>` version (`shorts.build_short`, so the
    complete-statement gate refuses an edge that is not one unless `allow_incomplete`, which is logged)."""
    clip = _clip(ep, slug, name, allow_unapproved)
    cfg = shorts.load_config(ep)
    prev, prev_v = _prev_short(ep, clip["slug"])
    src = _source(ep, clip)
    clip["source"] = src
    src_doc = _source_doc(ep, clip, src)
    words = C.mapped_words(ep, src_doc)
    plan = plan_recut(words, clip, drop_start_words, drop_end_words, trim_start_s, trim_end_s)
    if not (plan["dropped_start"] or plan["dropped_end"]):
        raise SystemExit("nothing to trim: pass --drop-start-words / --drop-end-words / --trim-start / --trim-end")
    data = C.load_clips(ep)
    lim = data["rubric"]["limits"]
    dur = plan["end"] - plan["start"]
    if dur < float(lim["hard_min_s"]):
        raise SystemExit(f"the trimmed clip would be {dur:.1f}s, under the {lim['hard_min_s']:g}s minimum (config/clips.rubric.yaml limits.hard_min_s)")
    new = copy.deepcopy(clip)
    new.update(source=src, start=round(plan["start"], 3), end=round(plan["end"], 3), duration=round(dur, 3), edit=None, context_variant=None,
               text=" ".join(w["w"] for w in words[plan["first"]:plan["last"] + 1]), needs_context=False)
    audit = completeness.audit_edges(ep, new, data["rubric"])
    if audit is None:
        raise SystemExit("cannot judge the new edges (the edit the clip was picked from is missing): `ave clips prepare` and re-pick")
    new.update(start_complete=audit["start_complete"], end_complete=audit["end_complete"], complete_statement=audit["complete_statement"],
               completeness_note=audit["completeness_note"])
    hook_q = new.get("hook_quote") or new.get("hook") or ""
    hook_kept = (not hook_q) or all(t in completeness.norm_words(new["text"]) for t in completeness.norm_words(hook_q)[:12])
    report = {"slug": clip["slug"], "from_version": prev_v, "old_range": [clip["start"], clip["end"]], "new_range": [new["start"], new["end"]],
              "old_duration": clip["duration"], "new_duration": new["duration"], "dropped_start_words": plan["dropped_start"],
              "dropped_end_words": plan["dropped_end"], "first_words": audit["first_words"], "last_words": audit["last_words"],
              "complete": bool(audit["complete_statement"]), "completeness_note": audit["completeness_note"], "hook_still_inside": hook_kept}
    if dry_run:
        return {**report, "dry_run": True, "would": ("build" if audit["complete_statement"] or allow_incomplete else "refuse: not a complete statement"),
                "next": f"ave shorts recut {ep.root} --clip {clip['slug']} " + _flags(drop_start_words, drop_end_words, trim_start_s, trim_end_s)}
    if not audit["complete_statement"] and not allow_incomplete:
        raise SystemExit(f"the trimmed short is not a complete statement: {audit['completeness_note']}.\n  would start: {audit['first_words']!r}\n"
                         f"  would end:   {audit['last_words']!r}\nTrim a different number of words (`--dry-run` shows each try), or, only if the human accepts a "
                         "mid-thought edge, --allow-incomplete (recorded).")
    # the new select: sliced from the EXACT edit the clip was picked from, as a new version of select.<slug> (the one clips.json points at is untouched)
    sl = editdoc.slice_doc(src_doc, new["start"], new["end"], stage="select")
    if sl["tracks"]["dialogue"]:
        sl["tracks"]["dialogue"][0]["xfade_ms"] = 0
    sl["notes"] = (C._clip_notes(clip, src) + f"\nRECUT (words): {new['start']:.2f}-{new['end']:.2f}s of the source edit "
                   f"(was {clip['start']:.2f}-{clip['end']:.2f}s)")
    sl["recut"] = {"clip": clip["id"], "range": [new["start"], new["end"]], "from_range": [clip["start"], clip["end"]],
                   "source": [src["stage"], src["version"]]}
    sel_path = editdoc.save_new_version(ep, sl, name=clip["slug"])
    new["edit"] = {"name": clip["slug"], "version": editdoc.list_versions(ep, "select", clip["slug"])[-1], "path": str(sel_path.relative_to(ep.root)),
                   "range": [new["start"], new["end"]], "source": [src["stage"], src["version"]]}
    sh = prev.get("short") or {}
    op = sh.get("opening") or {}
    who = by or _who()
    note = (f"recut by {who}: starts {new['start'] - clip['start']:+.2f}s / ends {new['end'] - clip['end']:+.2f}s against the approved clip "
            f"(dropped {len(plan['dropped_start'])} word(s) at the start, {len(plan['dropped_end'])} at the end): the human has not seen this range")
    extra = {"recut": {**report, "by": who, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "select": new["edit"]["path"]}}
    res = shorts.build_short(ep, new, cfg, layout=sh.get("layout") or "crop", force=False, allow_incomplete=allow_incomplete,
                             opening_mode=op.get("requested") or None, title=op.get("title") if op.get("title_source") in ("--title", "shorts retitle") else None,
                             detector=detector, extra_short=extra, extra_review=[note] + ([] if hook_kept else ["the clip's hook quote is no longer inside the trimmed range"]))
    from . import progress
    progress.auto_log(ep, f"RECUT short {clip['slug']}: {report['old_range']} -> {report['new_range']} ({', '.join(plan['dropped_start'][:4]) or '-'} | "
                          f"{', '.join(plan['dropped_end'][-4:]) or '-'} dropped); review it, then re-export", kind="decision", step="shorts")
    return {**report, "built": res, "needs_review": res["needs_review"], "select": new["edit"]["path"],
            "next": [f"ave shorts preview {ep.root} {clip['slug']} --t 1", f"ave shorts export {ep.root} --clip {clip['slug']}   (new version, new files)"]}


def _flags(a: int, b: int, c: float, d: float) -> str:
    return " ".join(x for x in (f"--drop-start-words {a}" if a else "", f"--drop-end-words {b}" if b else "", f"--trim-start {c:g}" if c else "",
                                f"--trim-end {d:g}" if d else "") if x)


def _who() -> str:
    from .learning import _by
    return _by()


# ---- retitle ---------------------------------------------------------------------------------------------------------------------

def retitle(ep: Episode, slug: str, title: str, *, name: str | None = None, allow_unapproved: bool = False, by: str | None = None) -> dict:
    """Swap the opening's title in a NEW short version. The crops, captions and timing are copied as they are; only the title overlay is re-rendered."""
    clip = _clip(ep, slug, name, allow_unapproved)
    doc, ver = _prev_short(ep, clip["slug"])
    rec = (doc.get("short") or {}).get("opening") or {}
    ov_rec = rec.get("overlay")
    if rec.get("mode", "none") == "none" or not ov_rec:
        raise SystemExit(f"{clip['slug']} has no title opening to swap (mode {rec.get('mode', 'none')!r}): "
                         f"`ave shorts build {ep.root} --clip {clip['slug']} --opening title_overlay --title \"...\" --force` adds one")
    cfg = opening.config(shorts.load_config(ep))
    title = " ".join(str(title or "").split())
    spoken = clip.get("text") or ""
    problems = opening.critique(ep, title, spoken, cfg)
    if problems:
        raise SystemExit("this title cannot open the short:\n  - " + "\n  - ".join(problems))
    new = editdoc.clone(doc)
    ov = next((o for o in new["tracks"].get("overlays") or [] if o.get("id") == ov_rec["id"]), None)
    if ov is None:
        raise SystemExit(f"the short's title overlay {ov_rec['id']} is missing from {clip['slug']}.v{ver}; rebuild it with --force")
    props = {**(ov.get("props") or ov_rec.get("props") or {}), "title": title}
    dur = ov["o1"] - ov["o0"]
    cv = (new["canvas"]["w"], new["canvas"]["h"])
    rel = opening.render_overlay(ep, props, dur, cv, new["fps"])
    old_title = rec.get("title")
    ov["file"], ov["props"] = str(rel), props
    rec = {**rec, "title": title, "title_source": "shorts retitle", "problems": [], "overlay": {**ov_rec, "file": str(rel), "props": props}}
    new["short"]["opening"] = rec
    who = by or _who()
    new["short"]["retitle"] = {"from": old_title, "to": title, "by": who, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "from_version": ver}
    new["short"]["needs_review"] = list(new["short"].get("needs_review") or [])
    new["notes"] = (new.get("notes") or "") + f" | retitled: {title!r}"
    path = editdoc.save_new_version(ep, new, name=clip["slug"])
    tf = opening.titles_file(ep)
    titles = json.loads(tf.read_text()) if tf.exists() else {}
    titles[clip["slug"]] = title                                   # a later `ave shorts build --force` keeps the new title
    write_json(tf, titles)
    return {"slug": clip["slug"], "from_version": ver, "version": editdoc.list_versions(ep, "short", clip["slug"])[-1], "path": str(path.relative_to(ep.root)),
            "title": title, "was": old_title, "overlay": rec["overlay"], "titles_json": str(tf.relative_to(ep.root)),
            "next": [f"ave shorts preview {ep.root} {clip['slug']} --t 1", f"ave shorts export {ep.root} --clip {clip['slug']}   (new version, new files)"]}
