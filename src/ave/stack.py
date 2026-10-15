"""Two-up stacked short layout (opt-in: `ave shorts build --layout stack`): both speakers on screen at once, one above the other,
1080x960 each. For crosstalk-heavy clips, where cutting between speakers every second reads as flicker.

How it works (WHY it does not touch the compiler's video model, which has ONE picture per moment):
  * plan   : the picture timeline is the select's video timeline with the camera choice thrown away (both cameras always run in
             sync at the same episode time `e0`). Each speaker's camera gets a static 9:8 face crop (Apple Vision, same medians as
             the portrait layout); a face tighter than the portrait layout (`stack.face_frac`) because each half is only 960 px high.
  * render : each half is rendered by the normal compiler as its own 1080x960 picture, the two are stacked with ffmpeg into ONE
             intermediate 1080x1920 file in work/shorts/, and the short then renders as an ordinary single-source doc whose picture is
             that file (so audio, loudness, caption burn, verification and sidecars are the same code as every other short).
Captions sit on the seam between the halves (config `stack.caption_y`): the one place with no faces.
"""

from __future__ import annotations

import copy
import subprocess
from pathlib import Path

from . import compile as comp
from . import editdoc, ffutil, reframe
from .episode import Episode

EPS = 1e-3
HALF = (1080, 960)
INTERMEDIATE = {"kind": "video", "ext": ".mp4", "crf": 14, "x264_preset": "fast", "audio": "aac", "audio_kbps": 192,
                "lufs": -14.0, "tp": -1.5, "height": None}


def timeline(doc: dict) -> list[dict]:
    """The video timeline as [{o0, o1, e0}] with neighbours merged when the episode time is continuous (the camera is irrelevant)."""
    out: list[dict] = []
    for s in sorted(doc["tracks"]["video"], key=lambda s: s["o0"]):
        p = out[-1] if out else None
        if p and abs(p["e0"] + (p["o1"] - p["o0"]) - s["e0"]) < EPS and abs(p["o1"] - s["o0"]) < EPS:
            p["o1"] = s["o1"]
        else:
            out.append({"o0": s["o0"], "o1": s["o1"], "e0": s["e0"]})
    return out


def _half_doc(doc: dict, cam: str, segs: list[dict]) -> dict:
    d = copy.deepcopy(doc)
    d["canvas"] = {"w": HALF[0], "h": HALF[1]}
    d["tracks"]["video"] = [{"id": f"{cam}-{i}", "src": cam, "o0": s["o0"], "o1": s["o1"], "e0": s["e0"], "crop": None, "zoom": 1.0}
                            for i, s in enumerate(segs)]
    d["tracks"]["overlays"] = []
    d["captions"] = {"srt": None, "ass": None, "burn": False}
    return d


def pick_cameras(ep: Episode, doc: dict, cams_of_speakers: dict[str, str]) -> list[str]:
    """Two cameras, one per distinct speaker, ordered by speaker id (S1 on top). Refuses with a reason if there are not two."""
    by_spk: dict[str, str] = {}
    for cam, sid in sorted(cams_of_speakers.items()):
        by_spk.setdefault(sid, cam)
    if len(by_spk) < 2:
        raise SystemExit("--layout stack needs two speaker cameras in the edit (episode.json speakers.<id>.camera/cameras); "
                         f"found {sorted(by_spk.values()) or 'none'}. Use --layout crop.")
    return [by_spk[k] for k in sorted(by_spk)[:2]]


def plan(ep: Episode, doc: dict, cfg: dict, cams_of_speakers: dict[str, str], detector=None) -> dict:
    """{order: [top cam, bottom cam], timeline, layers: {cam: [{o0,o1,e0,crop,zoom}]}, needs_review: [..], notes: [..]}."""
    st = cfg.get("stack", {})
    order = pick_cameras(ep, doc, cams_of_speakers)
    tl = timeline(doc)
    cfg2 = copy.deepcopy(cfg)
    cfg2["canvas"] = {"w": HALF[0], "h": HALF[1]}
    cfg2["reframe"]["face_frac"] = st.get("face_frac", 0.42)
    cfg2["reframe"]["eye_line"] = st.get("eye_line", 0.38)
    dur = {c: ffutil.probe(ep.root / doc["sources"][c]["file"])["duration"] for c in order}
    layers, review = {}, []
    for cam in order:
        for s in tl:
            lo = editdoc.source_time(doc, cam, s["e0"])
            hi = editdoc.source_time(doc, cam, s["e0"] + s["o1"] - s["o0"])
            if lo < -EPS or hi > dur[cam] + EPS:
                raise SystemExit(f"--layout stack: camera {cam} does not cover episode time {s['e0']:.1f}s ({s['o1'] - s['o0']:.1f}s); "
                                 "use --layout crop for this clip")
        half = _half_doc(doc, cam, tl)
        kw = {"detector": detector} if detector else {}
        pl = reframe.plan_crops(ep, half, cfg2, aspect=(9, 8), **kw)
        layers[cam] = []
        for s, hs in zip(tl, half["tracks"]["video"]):
            p = pl[hs["id"]]
            layers[cam].append({**s, "crop": p["crop"], "zoom": 1.0})
            if p["needs_review"]:
                review.append(f"stack {cam}: {p['reason']}")
    return {"order": order, "timeline": tl, "layers": layers, "needs_review": sorted(set(review)), "caption_y": st.get("caption_y", 0.5)}


def render_picture(ep: Episode, doc: dict, out: Path) -> Path:
    """Render both halves with the compiler, stack them into `out` (1080x1920, frame-exact, no audio)."""
    sp = doc["short"]["stack"]
    out.parent.mkdir(parents=True, exist_ok=True)
    halves = []
    wd = out.parent / (out.stem + ".parts")
    wd.mkdir(parents=True, exist_ok=True)
    try:
        for i, cam in enumerate(sp["order"]):
            d = _half_doc(doc, cam, sp["timeline"])
            for hs, lay in zip(d["tracks"]["video"], sp["layers"][cam]):
                hs["crop"], hs["zoom"] = lay["crop"], lay["zoom"]
            f = wd / f"half{i}.mp4"
            comp.compile_video(ep, d, f, preset=INTERMEDIATE, burn_captions=False)
            halves.append(f)
        partial = out.with_name(out.stem + ".partial.mp4")
        ffutil.run(["-y", "-i", str(halves[0]), "-i", str(halves[1]), "-filter_complex", "[0:v][1:v]vstack=inputs=2,setsar=1,format=yuv420p[v]",
                    "-map", "[v]", "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "14", "-pix_fmt", "yuv420p", "-colorspace", "bt709",
                    "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv", "-movflags", "+faststart", str(partial)])
        partial.replace(out)
    finally:
        for f in wd.glob("*"):
            f.unlink(missing_ok=True)
        wd.rmdir()
    return out


def render_doc(ep: Episode, doc: dict, version: int) -> dict:
    """The doc `comp.render` gets for a stack short: same audio/captions, picture = the stacked intermediate (rendered once per version)."""
    slug = doc["short"]["slug"]
    pic = ep.work / "shorts" / f"stack.{slug}.v{version}.mp4"
    if not pic.exists():
        render_picture(ep, doc, pic)
    d = copy.deepcopy(doc)
    d["sources"]["STACK"] = {"kind": "video", "file": str(pic.relative_to(ep.root)), "start_offset": 0.0, "drift_ppm": 0.0}
    d["tracks"]["video"] = [{"id": "stack", "src": "STACK", "o0": 0.0, "o1": doc["duration"], "e0": 0.0, "crop": None, "zoom": 1.0}]
    return d
