"""Shorts: approved clip -> vertical `short` EditDoc -> per-platform exports with publish sidecars.

Pipeline (each step is deterministic and writes a NEW version, never overwriting):
  select.<slug>.vN  --build-->  short.<slug>.vM   (canvas 1080x1920, face-centred crops, min-hold, karaoke captions)
  short.<slug>.vM   --export--> exports/shorts/<slug>.vM.<platform>.mp4 + .json sidecar (limits, loudness, licenses)

Design notes (WHY):
  * Cuts are preserved from the select but a shot shorter than `min_hold_s` is not shown: its time is given to the neighbouring
    camera with the SAME episode time (`e0`), so picture and dialogue stay in sync while a 30 s clip is not a blur of cuts.
  * Platform presets are identical encodes today; one render is made per distinct encode and copied per platform, so exporting
    to four platforms costs one render.
  * The sidecar is the contract with `ave publish queue`: title, hook, description draft, platform, limits, license coverage.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
import time
from pathlib import Path

import yaml

from . import completeness, editdoc, ffutil, opening, presets, reframe, resources, zoomstate
from .episode import Episode, read_json, write_json

CONFIG = resources.config_file("shorts.yaml")
EPS = 1e-3


def _merge(a: dict, b: dict) -> dict:
    out = copy.deepcopy(a)
    for k, v in (b or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def load_config(ep: Episode | None = None, path: Path | str | None = None) -> dict:
    """Packaged defaults deep-merged with <episode>/policy/shorts.yaml (and an explicit path)."""
    cfg = yaml.safe_load(CONFIG.read_text())
    layers = ([ep.root / "policy" / "shorts.yaml"] if ep else []) + ([Path(path)] if path else [])
    for p in layers:
        if p.exists():
            cfg = _merge(cfg, yaml.safe_load(p.read_text()) or {})
    return cfg


DRAFTS = "shorts_drafts"


def shorts_dir(ep: Episode, draft: bool = False) -> Path:
    """Where finished shorts land. Trial (unapproved) shorts never go to `shorts/`, which `ave publish queue` reads."""
    return ep.exports / (DRAFTS if draft else "shorts")


# ---- choosing clips -------------------------------------------------------------------------------------------------

def load_clips(ep: Episode) -> list[dict]:
    p = ep.work / "clips.json"
    if not p.exists():
        raise SystemExit("No clips yet. Run `ave clips propose` / `decide` / `build` first (skill: ave-clip-finder).")
    return read_json(p)["clips"]


def select_clips(ep: Episode, slug: str | None = None, all_approved: bool = False, name: str | None = None,
                 allow_unapproved: bool = False) -> list[dict]:
    """Clips to make shorts from. Clips of every session live in one work/clips.json with unique ids and each carries the
    select EditDoc cut from the exact edit it was picked from, so no session flag is needed; `name` only narrows the choice
    to one session (with --all-approved) or checks that a named clip belongs to it.

    `allow_unapproved` is the TRIAL mechanism (same name as the content pipeline's): a proposed clip may be built when the
    human said to, its select EditDoc is cut on the fly (`ensure_select`; clips.json is not touched) and its exports go to
    `exports/shorts_drafts/`, never to `exports/shorts/`."""
    clips = load_clips(ep)
    if name and name not in {c.get("session") for c in clips}:
        have = sorted({c["session"] for c in clips if c.get("session")})
        raise SystemExit(f"no clips for session {name!r}" + (f"; clips exist for {', '.join(have)}" if have else "; this episode's clips have no sessions (drop --name)"))
    if slug:
        hit = [c for c in clips if slug in (c["slug"], c["id"])]
        if not hit:
            raise SystemExit(f"no clip {slug!r}; known: {', '.join(c['slug'] for c in clips)}")
        notok = [c for c in hit if c["status"] != "approved"]
        if notok and not allow_unapproved:
            raise SystemExit(f"clip {notok[0]['id']} is {notok[0]['status']}, not approved. Ask the human to approve it (`ave clips decide`), "
                             "or pass --allow-unapproved if they said to make a trial short (drafts go to exports/shorts_drafts/, never to the publish folder)")
        wrong = [c for c in hit if name and c.get("session") != name]
        if wrong:
            raise SystemExit(f"clip {wrong[0]['id']} is in session {wrong[0].get('session')}, not {name}: use --name {wrong[0].get('session')} or drop --name")
    elif all_approved:
        hit = [c for c in clips if c["status"] == "approved" and (not name or c.get("session") == name)]
        if not hit:
            raise SystemExit("no approved clips" + (f" in session {name}" if name else "") + " yet. Review with `ave clips review`, `ave clips decide`, "
                             "then `ave clips build --approved-only`" + (f" (each with --name {name})." if name else "."))
    else:
        raise SystemExit("choose --clip <slug> or --all-approved")
    for c in hit:
        if not c.get("edit") and allow_unapproved and c["status"] != "approved":
            c["edit"] = ensure_select(ep, c)
        if not c.get("edit"):
            raise SystemExit(f"clip {c['slug']} has no select EditDoc yet; run `ave clips build {ep.root} --approved-only"
                             + (f" --name {c['session']}`" if c.get("session") else "`"))
    return hit


def ensure_select(ep: Episode, clip: dict) -> dict:
    """The select EditDoc of a clip that was never built (trial mode): sliced from the exact edit the clip was picked from
    (fingerprint-checked by clips), saved as a NEW `select.<slug>` version, reused when the same range/source is asked again.
    Returns the clip's `edit` record. work/clips.json is not modified."""
    from . import clips as C
    src = clip["source"]
    key = [src["stage"], src["version"]]
    for v in reversed(editdoc.list_versions(ep, "select", clip["slug"])):
        p = editdoc.path_for(ep, "select", v, clip["slug"])
        prev = editdoc.load(p)
        if (prev.get("draft_select") or {}).get("range") == [clip["start"], clip["end"]] and prev["draft_select"]["source"] == key:
            return {"name": clip["slug"], "version": v, "path": str(p.relative_to(ep.root)),
                    "range": [clip["start"], clip["end"]], "source": key}
    data = C.load_clips(ep)
    docs = C._source_docs(ep, data["rubric"], [clip])
    doc = next(iter(docs.values()))
    sl = editdoc.slice_doc(doc, clip["start"], clip["end"], stage="select")
    if sl["tracks"]["dialogue"]:
        sl["tracks"]["dialogue"][0]["xfade_ms"] = 0
    sl["notes"] = C._clip_notes(clip, src) + "\nTRIAL select (clip not approved)"
    sl["draft_select"] = {"clip": clip["id"], "range": [clip["start"], clip["end"]], "source": key}
    path = editdoc.save_new_version(ep, sl, name=clip["slug"])
    return {"name": clip["slug"], "version": editdoc.list_versions(ep, "select", clip["slug"])[-1], "path": str(path.relative_to(ep.root)),
            "range": [clip["start"], clip["end"]], "source": key}


# ---- the short EditDoc ----------------------------------------------------------------------------------------------

def _covers(doc: dict, src: str, e0: float, e1: float, durations: dict[str, float]) -> bool:
    return editdoc.source_time(doc, src, e0) >= -EPS and editdoc.source_time(doc, src, e1) <= durations[src] + EPS


def apply_min_hold(doc: dict, ep: Episode, min_hold_s: float) -> list[str]:
    """Re-assign shots shorter than min_hold_s to the neighbouring camera (same e0, so still in sync) and merge. Returns notes."""
    vt = sorted(doc["tracks"]["video"], key=lambda s: s["o0"])
    cams = {s["src"] for s in vt}
    if len(cams) < 2 or min_hold_s <= 0:
        return []
    dur = {c: ffutil.probe(ep.root / doc["sources"][c]["file"])["duration"] for c in cams}
    notes: list[str] = []
    for _ in range(len(vt) * 2):
        vt = _merge_contiguous(vt)
        short = sorted((s for s in vt if s["o1"] - s["o0"] < min_hold_s - EPS and not s.get("min_hold_unmet")),
                       key=lambda s: s["o1"] - s["o0"])
        if not short or len(vt) == 1:
            break
        s = short[0]
        i = vt.index(s)
        nbrs = [n for n in (vt[i - 1] if i else None, vt[i + 1] if i + 1 < len(vt) else None) if n and n["src"] != s["src"]]
        nbrs = [n for n in nbrs if _covers(doc, n["src"], s["e0"], s["e0"] + s["o1"] - s["o0"], dur)]
        if not nbrs:
            s["min_hold_unmet"] = True       # nothing can absorb it (same camera both sides, or no coverage): keep, do not retry
            continue
        n = max(nbrs, key=lambda x: x["o1"] - x["o0"])
        notes.append(f"{s['id']} ({s['o1'] - s['o0']:.2f}s on {s['src']}) shown on {n['src']} to honour min_hold {min_hold_s:g}s")
        s["src"] = n["src"]
        s["reason"] = "min_hold: short shot shown on the neighbouring camera"
    doc["tracks"]["video"] = _merge_contiguous(vt)
    for s in doc["tracks"]["video"]:
        s.pop("min_hold_unmet", None)
    return notes


def camera_speakers(ep: Episode, doc: dict) -> dict[str, str]:
    """{source key: speaker id} for the cameras of `doc`, from episode.json (`speakers.<sid>.cameras` per session, or `.camera`)."""
    meta = read_json(ep.meta_path)
    out: dict[str, str] = {}
    for sid, sp in (meta.get("speakers") or {}).items():
        for cam in list((sp.get("cameras") or {}).values()) + ([sp["camera"]] if sp.get("camera") else []):
            if cam in doc["sources"]:
                out[cam] = sid
    return out


def segment_speakers(ep: Episode, doc: dict) -> dict[str, str | None]:
    """{video segment id: the speaker with the most word time while the segment is on screen} (None = no speech)."""
    from . import captions
    tr = read_json(ep.artifact("transcript.json"))
    words = captions.map_words_to_output(tr["words"], doc)
    out: dict[str, str | None] = {}
    for s in doc["tracks"]["video"]:
        acc: dict[str, float] = {}
        for w in words:
            ov = min(w["end"], s["o1"]) - max(w["start"], s["o0"])
            if ov > 0 and w.get("speaker"):
                acc[w["speaker"]] = acc.get(w["speaker"], 0.0) + ov
        out[s["id"]] = max(acc, key=acc.get) if acc else None
    return out


def listener_cutaways(ep: Episode, doc: dict) -> list[dict]:
    """Video segments whose camera shows someone who is NOT the one talking (reaction shots from the rough cut's pacing pass)."""
    cams, spk = camera_speakers(ep, doc), segment_speakers(ep, doc)
    return [{"id": s["id"], "src": s["src"], "o0": round(s["o0"], 3), "o1": round(s["o1"], 3), "speaker": spk[s["id"]]}
            for s in doc["tracks"]["video"] if spk.get(s["id"]) and cams.get(s["src"]) and cams[s["src"]] != spk[s["id"]]]


def prefer_speaker_camera(ep: Episode, doc: dict) -> list[str]:
    """Opt-in (`build.cutaways: speaker`): show the talking person's camera in every shot (same e0, so still in sync) and merge.
    Right for a short, where a reaction shot every two seconds on a phone screen reads as a flicker. Returns notes."""
    cams = camera_speakers(ep, doc)
    by_spk = {sid: cam for cam, sid in cams.items()}
    notes = []
    dur = {c: ffutil.probe(ep.root / doc["sources"][c]["file"])["duration"] for c in cams}
    for c in listener_cutaways(ep, doc):
        s = next(x for x in doc["tracks"]["video"] if x["id"] == c["id"])
        cam = by_spk.get(c["speaker"])
        if cam and cam != s["src"] and _covers(doc, cam, s["e0"], s["e0"] + s["o1"] - s["o0"], dur):
            notes.append(f"{s['id']} ({s['o1'] - s['o0']:.2f}s) cutaway to {s['src']} replaced by speaker camera {cam}")
            s["src"], s["reason"] = cam, "cutaways=speaker: the talking person stays on screen"
    doc["tracks"]["video"] = _merge_contiguous(doc["tracks"]["video"])
    return notes


def unmasked_jump_cuts(doc: dict) -> list[tuple[dict, dict]]:
    """Adjacent shots on the SAME camera whose episode time is not continuous (content was cut out between them) and whose
    framing is the same (equal zoom): a visible jump cut. The rough cut hides these with a cutaway or a punch-in."""
    vt = sorted(doc["tracks"]["video"], key=lambda s: s["o0"])
    return [(a, b) for a, b in zip(vt, vt[1:])
            if a["src"] == b["src"] and abs(a["e0"] + (a["o1"] - a["o0"]) - b["e0"]) > EPS
            and abs(float(a.get("zoom") or 1.0) - float(b.get("zoom") or 1.0)) < 0.05]


def mask_jump_cuts(doc: dict, zoom: float, dwell_s: float = 8.0) -> list[str]:
    """Alternate a centred punch-in on shots that follow a visible jump cut (what an editor does when there is one camera on
    the talker). Returns notes. zoom <= 1 disables.

    Shares the zoom-state rule with the rough cut (`ave.zoomstate`): a framing change on a camera comes at least `dwell_s` after the
    previous one, so a run of jump cuts becomes in ... (held) ... out, never in-out-in within seconds. A jump cut the rule forbids
    masking is left as it is and noted. dwell_s <= 0 restores the old behaviour (alternate on every jump cut)."""
    if zoom <= 1.0:
        return []
    notes, left = [], set()
    for _ in range(len(doc["tracks"]["video"])):
        jc = [p for p in unmasked_jump_cuts(doc) if p[1]["id"] not in left]
        if not jc:
            break
        a, b = jc[0]
        going_in = float(a.get("zoom") or 1.0) < 1.05
        ok, why = zoomstate.allows(doc["tracks"]["video"], b["o0"], b["src"], "in" if going_in else "out", dwell_s, 0.05)
        if not ok:
            left.add(b["id"])
            notes.append(f"{b['id']}: jump cut after {a['id']} on {b['src']} left unmasked: {why}")
            continue
        b["zoom"] = zoom if going_in else 1.0
        notes.append(f"{b['id']}: jump cut after {a['id']} on {b['src']} masked with a {b['zoom']:.2f}x punch-in")
    if dwell_s > 0:
        notes += _hold_zoom_through_dwell(doc, dwell_s)
    return notes


def _hold_zoom_through_dwell(doc: dict, dwell_s: float) -> list[str]:
    """A shot that follows a punched-in shot on the same camera comes back at the base framing by default, which is a quick zoom-out a few
    seconds after the zoom-in. Where that would break the dwell, keep the previous framing instead (the cut between them is then a plain
    jump cut, noted) so the way out comes no sooner than `dwell_s` after the way in."""
    notes, last, prev = [], {}, None
    for s in sorted(doc["tracks"]["video"], key=lambda x: x["o0"]):
        if prev and prev["src"] == s["src"] and prev.get("crop") == s.get("crop") and abs(float(s.get("zoom") or 1.0) - float(prev.get("zoom") or 1.0)) >= 0.05:
            if s["src"] in last and s["o0"] - last[s["src"]] < dwell_s - 1e-6:
                notes.append(f"{s['id']}: zoom {float(s.get('zoom') or 1.0):.2f}x -> held at {float(prev.get('zoom') or 1.0):.2f}x, the previous zoom change on "
                             f"{s['src']} was {s['o0'] - last[s['src']]:.1f}s ago (zoom_min_dwell_s {dwell_s:g}); the cut stays a plain jump cut")
                s["zoom"] = prev["zoom"]
            else:
                last[s["src"]] = s["o0"]
        prev = s
    return notes


def _merge_contiguous(vt: list[dict]) -> list[dict]:
    out: list[dict] = []
    for s in sorted(vt, key=lambda x: x["o0"]):
        p = out[-1] if out else None
        if p and p["src"] == s["src"] and abs(float(p.get("zoom") or 1.0) - float(s.get("zoom") or 1.0)) < 1e-6 and abs(p["e0"] + (p["o1"] - p["o0"]) - s["e0"]) < EPS and abs(p["o1"] - s["o0"]) < EPS:
            p["o1"] = s["o1"]
        else:
            out.append(dict(s))
    return out


def _signature(select_path: str, select_mtime_hash: str, cfg: dict, style: str, extra: str = "") -> str:
    blob = json.dumps({"select": select_path, "sel_hash": select_mtime_hash, "reframe": cfg["reframe"], "build": cfg["build"],
                       "canvas": cfg["canvas"], "style": style, "completeness": cfg.get("completeness"), "opening": cfg.get("opening"),
                       "extra": extra}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def build_short(ep: Episode, clip: dict, cfg: dict | None = None, layout: str = "crop", detector=None, force: bool = False,
                allow_incomplete: bool = False, opening_mode: str | None = None, title: str | None = None) -> dict:
    """Build (or find unchanged) the short EditDoc for one clip. Returns {slug, path, version, status, needs_review, notes}.

    Hard guarantees (see `completeness.py`, `opening.py`):
      * the clip must be a complete statement (start and end), judged from its stored fields AND re-judged on the real words at its
        edges. An incomplete clip is cut from its `context_variant` when that is complete, else REFUSED (SystemExit).
        `allow_incomplete` is the explicit override; it is recorded in the doc, the sidecar and PROGRESS.md.
      * the cut never slices a word: the first and last edge are moved to word gaps with a little pad.
      * the opening is a title (overlay or card), never the quote (`opening_mode`: none | title_card | title_overlay)."""
    from . import captions
    cfg = cfg or load_config(ep)
    if layout not in ("crop", "stack", "audiogram"):
        raise SystemExit(f"unknown layout {layout!r}; choose crop (default, one speaker at a time), stack (two-up, 1080x960 each) "
                         "or audiogram (waveform card for audio-only clips)")
    orig = clip
    clip, comp = completeness.gate(ep, clip, cfg, allow_incomplete)
    if comp["override"]:
        from . import progress
        progress.auto_log(ep, f"OVERRIDE --allow-incomplete: short {orig['slug']} cut from clip {orig['id']} although it is not a complete "
                              "statement: " + "; ".join(comp["problems"]), kind="decision", step="shorts")
    if not clip.get("edit"):                     # the context variant: cut its own select (new version, nothing overwritten)
        clip["edit"] = ensure_select(ep, clip)
    slug = clip["slug"]
    sel_path = ep.root / clip["edit"]["path"]
    sel = editdoc.load(sel_path)
    style_name = cfg["captions_style"]
    oc = opening.config(cfg)
    sig = _signature(clip["edit"]["path"], hashlib.sha256(sel_path.read_bytes()).hexdigest()[:16], cfg,
                     style_name + ("" if layout == "crop" else f"|{layout}"),
                     extra=f"{opening.signature(oc, opening_mode, title, clip, ep)}|override={comp['override']}|{comp['boundary']}")
    prev_path = editdoc.latest_path(ep, "short", slug)
    if prev_path and not force:
        prev = editdoc.load(prev_path)
        if (prev.get("short") or {}).get("sig") == sig:
            return {"slug": slug, "path": str(prev_path.relative_to(ep.root)), "version": editdoc.list_versions(ep, "short", slug)[-1],
                    "status": "unchanged", "needs_review": prev["short"]["needs_review"], "notes": []}

    doc = editdoc.clone(sel)
    doc["stage"] = "short"
    doc["canvas"] = dict(cfg["canvas"])
    try:
        twords = completeness.transcript_words(ep)
    except (OSError, ValueError, KeyError):
        twords = []
    edges = completeness.snap_edges(doc, twords, cfg.get("completeness"))      # never slice a word at the first/last cut
    stack_plan = None
    if layout == "stack":
        from . import stack
        notes = []
        stack_plan = stack.plan(ep, doc, cfg, camera_speakers(ep, doc), detector)
        top = stack_plan["order"][0]
        doc["tracks"]["video"] = [{"id": f"st{i}", "src": top, "o0": l["o0"], "o1": l["o1"], "e0": l["e0"], "crop": l["crop"], "zoom": 1.0,
                                   "reason": f"two-up stack: {top} on top, {stack_plan['order'][1]} below (picture is rendered by ave.stack)"}
                                  for i, l in enumerate(stack_plan["layers"][top])]
        cut = []
        plan = {s["id"]: {"crop": s["crop"], "zoom": 1.0, "reason": s["reason"], "confidence": 1.0, "needs_review": False}
                for s in doc["tracks"]["video"]}
    elif layout == "audiogram":
        from . import audiogram
        notes, cut = ["audiogram: waveform card instead of camera picture (no face framing, nothing to cut between)"], []
        audiogram.attach(ep, doc, slug, (editdoc.list_versions(ep, "short", slug) or [0])[-1] + 1, cfg)
        plan = {"wave": {"crop": None, "zoom": 1.0, "reason": doc["tracks"]["video"][0]["reason"], "confidence": 1.0, "needs_review": False}}
    else:
        notes = apply_min_hold(doc, ep, cfg["build"]["min_hold_s"])
        if cfg["build"].get("cutaways", "keep") == "speaker":
            notes += prefer_speaker_camera(ep, doc)
        cut = listener_cutaways(ep, doc)
        if cut:
            notes.append(f"{len(cut)} listener cutaway(s) ({sum(c['o1'] - c['o0'] for c in cut):.1f}s of {doc['duration']:.1f}s) show the person NOT talking; "
                         "set build.cutaways: speaker to keep the talker on screen")
        plan = reframe.plan_crops(ep, doc, cfg, **({"detector": detector} if detector else {}))
    review: list[str] = list(stack_plan["needs_review"]) if stack_plan else []
    for s in doc["tracks"]["video"]:
        p = plan[s["id"]]
        # keep the punch-in the rough cut used to hide a jump cut (it was dropped: same framing on both sides = visible jump)
        s["crop"], s["zoom"] = p["crop"], max(p["zoom"], float(s.get("zoom") or 1.0))
        s["reason"] = f"{p['reason']} | {s.get('reason', '')}".strip(" |")
        s["confidence"] = p["confidence"]
        if p["needs_review"]:
            s["needs_review"] = True
            review.append(f"{s['id']}: {p['reason']}")
    if not stack_plan:
        notes += mask_jump_cuts(doc, float(cfg["build"].get("jump_cut_zoom", 1.15)), float(cfg["build"].get("zoom_min_dwell_s", 8.0)))
    if doc["tracks"].get("overlays"):
        notes.append("overlays kept as-is; their x/y/w/h were placed for the source canvas, check them in the vertical frame")
        review.append("overlays are positioned for the 16:9 canvas")
    qc = opening.refuse_quote_cards(doc)
    if qc:
        raise SystemExit("; ".join(qc))
    cut_rep = completeness.edge_report(doc, twords) if twords else {}
    if cut_rep.get("interior_slices"):
        review.append(f"{len(cut_rep['interior_slices'])} interior cut(s) fall inside a word: " +
                      ", ".join(f"'{x['word']}' at {x['at_o']}s" for x in cut_rep["interior_slices"][:4]))
    open_rec = opening.apply(ep, doc, clip, cfg, opening_mode, title, layout)
    if open_rec["mode"] == "none" and open_rec["requested"] != "none":
        review.append("no title opening: " + "; ".join(open_rec["problems"] or [open_rec.get("note", "")]))
    elif open_rec["mode"] != "none":
        notes.append(f"opening {open_rec['mode']}: title {open_rec['title']!r} (a draft: edit it; source {open_rec['title_source']})")

    style = captions.get_style(style_name, ep=ep)
    if stack_plan and isinstance(style.get("position"), dict):
        style = {**style, "position": {**style["position"], "y": stack_plan["caption_y"]}}      # on the seam between the two halves
    cues = captions.cues_from_edit(ep, doc, style)
    if not cues:
        raise SystemExit(f"clip {slug}: no speech falls inside the edit, so there is nothing to caption")
    v = (editdoc.list_versions(ep, "short", slug) or [0])[-1] + 1
    srt = captions.write_srt(cues, ep.captions / f"short.{slug}.v{v}.srt")
    ass = captions.write_ass(cues, style, (doc["canvas"]["w"], doc["canvas"]["h"]), ep.captions / f"short.{slug}.v{v}.ass")
    problems = captions.check(cues, style)
    doc["captions"] = {"srt": str(srt.relative_to(ep.root)), "ass": str(Path(ass["path"]).relative_to(ep.root)), "burn": True}
    doc["notes"] = (clip.get("title") or slug) + (" | needs review: " + "; ".join(review) if review else "")
    doc["short"] = {"slug": slug, "clip_id": clip["id"], "title": clip["title"], "layout": layout, "sig": sig,
                    "select": clip["edit"]["path"], "draft": clip["status"] != "approved", "needs_review": review,
                    "completeness": {**comp, "cut": cut_rep}, "edges": edges, "opening": open_rec, "clip_range": [clip["start"], clip["end"]],
                    "crop_motion": reframe.crop_motion_stats(doc), "cutaways": cut, "min_hold_notes": notes}
    if stack_plan:
        doc["short"]["stack"] = stack_plan
    path = editdoc.save_new_version(ep, doc, name=slug)
    return {"slug": slug, "path": str(path.relative_to(ep.root)), "version": v, "status": "built", "needs_review": review, "notes": notes,
            "crop_motion": doc["short"]["crop_motion"], "caption_problems": problems, "segments": len(doc["tracks"]["video"]), "duration": doc["duration"],
            "completeness": {k: comp[k] for k in ("complete", "boundary", "override", "problems")}, "edges": edges,
            "opening": {k: open_rec.get(k) for k in ("mode", "requested", "title", "title_source", "problems", "added_s")}}


# ---- export ---------------------------------------------------------------------------------------------------------

def check_limits(platform: str, spec: dict, info: dict, size: int) -> dict:
    problems = []
    if info["duration"] > spec["max_duration_s"]:
        problems.append(f"duration {info['duration']:.1f}s exceeds {spec['label']} max {spec['max_duration_s']}s")
    if info["duration"] < spec.get("min_duration_s", 0):
        problems.append(f"duration {info['duration']:.1f}s is under {spec['label']} min {spec['min_duration_s']}s")
    if size > spec["max_bytes"]:
        problems.append(f"file is {size / 1e6:.0f} MB, over {spec['label']} max {spec['max_bytes'] / 1e6:.0f} MB")
    kbps = size * 8 / max(info["duration"], 1e-6) / 1000
    if spec.get("max_kbps") and kbps > spec["max_kbps"]:
        problems.append(f"overall bitrate {kbps:.0f} kbps exceeds {spec['label']} max {spec['max_kbps']} kbps")
    if spec.get("max_fps") and info.get("fps") and info["fps"] > spec["max_fps"] + 0.01:
        problems.append(f"{info['fps']:.2f} fps exceeds {spec['label']} max {spec['max_fps']}")
    if (info["w"], info["h"]) != (1080, 1920):
        problems.append(f"picture is {info['w']}x{info['h']}, expected 1080x1920")
    return {"platform": platform, "ok": not problems, "problems": problems, "max_duration_s": spec["max_duration_s"],
            "kbps": round(kbps), "max_kbps": spec.get("max_kbps"), "fps": info.get("fps"), "max_fps": spec.get("max_fps"),
            "max_bytes": spec["max_bytes"], "verified": spec.get("verified", False), "source": spec.get("source")}


def video_profile(path: Path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=profile,pix_fmt,color_space,color_primaries,color_transfer", "-of", "json", str(path)],
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)["streams"][0]


def licensed_files(doc: dict) -> list[str]:
    files = [m["file"] for m in doc["tracks"].get("music") or [] if m.get("file")]
    files += [o["file"] for o in doc["tracks"].get("overlays") or [] if o.get("file") and o.get("kind") != "motion"]
    files += [s["file"] for s in doc["sources"].values() if s.get("role") in ("broll", "stock", "music")]
    return list(dict.fromkeys(files))


def license_report(ep: Episode, doc: dict) -> dict:
    files = licensed_files(doc)
    if not files:
        return {"required": False, "ok": True, "covered": [], "missing": [], "attribution": []}
    from .assets import licenses
    cov = licenses.coverage(ep, files)
    entries = [c["entry"] for c in cov["covered"] if c.get("entry")]
    return {"required": True, "ok": not cov["missing"] and not cov["unreadable"],
            "covered": [{k: v for k, v in c.items() if k != "entry"} for c in cov["covered"]],
            "missing": [m["file"] for m in cov["missing"]], "unreadable": cov["unreadable"],
            "attribution": licenses.attribution_lines(entries, include_optional=True)}


def description_draft(clip: dict, lic: dict, platform: str) -> str:
    parts = [clip["hook"].strip()]
    if lic.get("attribution"):
        parts.append("Credits:\n" + "\n".join(lic["attribution"]))
    return "\n\n".join(parts)


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def export_short(ep: Episode, clip: dict, platforms: list[str], cfg: dict | None = None) -> dict:
    """Render the latest short doc for each platform (one render per distinct encode), verify, write sidecars. Idempotent per version."""
    from . import compile as comp
    cfg = cfg or load_config(ep)
    slug = clip["slug"]
    path = editdoc.latest_path(ep, "short", slug)
    if not path:
        raise SystemExit(f"no short for {slug}; run `ave shorts build {ep.root} --clip {slug}`")
    doc = editdoc.load(path)
    ver = editdoc.list_versions(ep, "short", slug)[-1]
    draft = clip["status"] != "approved"
    sh = doc.get("short") or {}
    crec = sh.get("completeness")
    if not crec:
        raise SystemExit(f"{slug}: this short was built before the complete-statement gate existed. Rebuild it: `ave shorts build {ep.root} --clip {slug} --force`")
    if not crec.get("complete") and not crec.get("override"):
        raise SystemExit(f"{slug}: the short is not a complete statement and no override was recorded: " + "; ".join(crec.get("problems") or []))
    qc = opening.refuse_quote_cards(doc)
    if qc:
        raise SystemExit(f"{slug}: " + "; ".join(qc))
    out_dir = shorts_dir(ep, draft)
    lic = license_report(ep, doc)
    results, rendered = [], {}
    for plat in platforms:
        spec = cfg["platforms"].get(plat)
        if not spec:
            raise SystemExit(f"unknown platform {plat!r}; choose from {', '.join(cfg['platforms'])}")
        pr = presets.get(spec["preset"])
        mp4 = out_dir / f"{slug}.v{ver}.{plat}.mp4"
        side = mp4.with_suffix(".json")
        if mp4.exists() and side.exists():
            results.append({"platform": plat, "status": "unchanged", "video": str(mp4.relative_to(ep.root)), "sidecar": str(side.relative_to(ep.root))})
            continue
        key = json.dumps({k: v for k, v in pr.items() if k != "about"}, sort_keys=True)
        warnings: list[str] = []
        if key in rendered:
            shutil.copy(rendered[key]["file"], mp4)
            rep = dict(rendered[key]["report"])
        else:
            rdoc = doc
            if (doc.get("short") or {}).get("layout") == "stack":
                from . import stack
                rdoc = stack.render_doc(ep, doc, ver)
            rep = comp.render(ep, rdoc, mp4, preset=spec["preset"], burn_captions=True, overwrite=False,
                              start_limit_db=cfg.get("audio", {}).get("start_limit_db", 0.0))
            rendered[key] = {"file": mp4, "report": rep}
        warnings += rep.get("warnings", [])
        info = ffutil.probe(mp4)
        prof = video_profile(mp4)
        size = mp4.stat().st_size
        limits = check_limits(plat, {**spec}, info, size)
        enc_problems = []
        if prof.get("pix_fmt") != "yuv420p":
            enc_problems.append(f"pix_fmt {prof.get('pix_fmt')} (want yuv420p)")
        if prof.get("profile") != "High":
            enc_problems.append(f"H.264 profile {prof.get('profile')} (want High)")
        if prof.get("color_space") not in ("bt709", None):
            enc_problems.append(f"color_space {prof.get('color_space')} (want bt709)")
        sidecar = {
            "name": mp4.stem, "draft": draft, "platform": plat, "platforms": [plat], "title": clip["title"], "hook": clip["hook"],
            "description": description_draft(clip, lic, plat), "caption": clip["hook"].strip(),
            "tags": ["Shorts"] if plat == "youtube" else [], "ai_disclosure": None,
            "video": str(mp4.relative_to(ep.root)), "sha256": _sha(mp4), "duration": round(info["duration"], 3),
            "width": info["w"], "height": info["h"], "fps": info["fps"], "bytes": size,
            "loudness": {"lufs": rep["lufs"], "true_peak_db": rep["true_peak_db"], "target_lufs": pr["lufs"], "tp_ceiling_db": pr["tp"]},
            "encode": {"profile": prof.get("profile"), "pix_fmt": prof.get("pix_fmt"), "color_space": prof.get("color_space"),
                       "problems": enc_problems},
            "limits": limits, "licenses": lic,
            "clip": {"id": clip["id"], "slug": slug, "score": clip.get("total"), "rationale": clip.get("rationale"),
                     "sentence_ids": clip.get("sentence_ids"), "source_range": sh.get("clip_range") or [clip["start"], clip["end"]]},
            "completeness": crec, "edges": sh.get("edges"), "opening": sh.get("opening"),
            "edit": str(path.relative_to(ep.root)), "version": ver,
            "needs_review": (doc.get("short") or {}).get("needs_review", []),
            "warnings": warnings, "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        write_json(side, sidecar)
        results.append({"platform": plat, "status": "exported", "video": sidecar["video"], "sidecar": str(side.relative_to(ep.root)),
                        "lufs": rep["lufs"], "true_peak_db": rep["true_peak_db"], "limits_ok": limits["ok"],
                        "problems": limits["problems"] + enc_problems, "warnings": warnings})
    return {"slug": slug, "version": ver, "exports": results}


def list_shorts(ep: Episode) -> list[dict]:
    out = []
    names = sorted({m.group("name") for f in ep.edits.glob("short.*.json")
                    if (m := editdoc._V.match(f.name)) and m["stage"] == "short" and m["name"]} if ep.edits.is_dir() else [])
    for n in names:
        vs = editdoc.list_versions(ep, "short", n)
        exp = sorted(f"{d.name}/{p.name}" for d in (shorts_dir(ep), shorts_dir(ep, True)) if d.is_dir() for p in d.glob(f"{n}.v*.mp4"))
        d = editdoc.load(editdoc.path_for(ep, "short", vs[-1], n))
        out.append({"slug": n, "versions": vs, "duration": d["duration"], "needs_review": (d.get("short") or {}).get("needs_review", []),
                    "exports": exp})
    return out


# ---- preview --------------------------------------------------------------------------------------------------------

def preview(ep: Episode, slug: str, t: float | None = None, out: Path | None = None, detector=reframe.detect_faces) -> dict:
    """PNG for the agent to LOOK at: left = source frame with the detected face (yellow) and crop (green); right = the vertical
    result at half size with the caption safe zone (red) and the 65% caption line (cyan). t = OUTPUT seconds (default: middle)."""
    from PIL import Image, ImageDraw
    from . import captions
    p = editdoc.latest_path(ep, "short", slug)
    if not p:
        raise SystemExit(f"no short for {slug}; run `ave shorts build` first")
    doc = editdoc.load(p)
    t = doc["duration"] / 2 if t is None else min(max(t, 0.0), doc["duration"] - 0.05)
    seg = next(s for s in doc["tracks"]["video"] if s["o0"] - EPS <= t < s["o1"] + EPS)
    e = seg["e0"] + (t - seg["o0"])
    srcf = ep.root / doc["sources"][seg["src"]]["file"]
    wd = ep.work / "shorts"
    wd.mkdir(parents=True, exist_ok=True)
    frame = reframe.extract_frame(srcf, editdoc.source_time(doc, seg["src"], e), wd / f"_pv_{slug}.png")
    im = Image.open(frame).convert("RGB")
    sw, sh = im.size
    c = seg["crop"] or [0, 0, sw, sh]
    face = None
    try:
        face = reframe.main_face(detector(frame), 0.3)
    except RuntimeError:
        pass
    frame.unlink(missing_ok=True)
    left = im.copy()
    d = ImageDraw.Draw(left)
    lw = max(3, sw // 300)
    d.rectangle([c[0], c[1], c[0] + c[2], c[1] + c[3]], outline=(0, 255, 0), width=lw * 2)
    if face:
        d.rectangle([face["x"], face["y"], face["x"] + face["w"], face["y"] + face["h"]], outline=(255, 220, 0), width=lw)
    H = 960
    left = left.resize((int(sw * H / sh), H))
    right = im.crop((c[0], c[1], c[0] + c[2], c[1] + c[3])).resize((540, 960))
    r = ImageDraw.Draw(right)
    style = captions.get_style(load_config(ep)["captions_style"], ep=ep)
    x0, y0, x1, y1 = captions.safe_rect(style, (540, 960))
    r.rectangle([x0, y0, x1, y1], outline=(255, 40, 40), width=2)
    cy = style["position"]["y"] * 960 if isinstance(style["position"], dict) else 960 * 0.65
    r.line([0, cy, 540, cy], fill=(0, 220, 255), width=1)
    canvas = Image.new("RGB", (left.size[0] + 540 + 12, H), (20, 20, 20))
    canvas.paste(left, (0, 0))
    canvas.paste(right, (left.size[0] + 12, 0))
    out = Path(out) if out else wd / f"preview.{slug}.t{int(t * 1000):06d}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out)
    return {"preview": str(out), "t": round(t, 3), "segment": seg["id"], "camera": seg["src"], "crop": c,
            "face": ({k: round(v, 1) for k, v in face.items()} if face else None), "reason": seg.get("reason"),
            "needs_review": bool(seg.get("needs_review"))}
