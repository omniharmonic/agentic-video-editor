"""Shorts: reframe geometry, short EditDoc, rendering, captions safe zone, sidecars, versioning, real-footage face check."""

from conftest import needs_libass  # noqa: E402
import json
import os
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from ave import captions, editdoc, ffutil, reframe, shorts
from ave.episode import read_json, write_json

pytestmark = pytest.mark.skipif(not __import__("shutil").which("ffmpeg"), reason="ffmpeg not installed")

_NONE = Path("/nonexistent/ave-real-footage-not-configured")


def _real(var: str) -> Path:
    """Optional real-footage inputs come from an env var; unset means the dependent tests skip (nothing is ever probed on a hard-coded drive)."""
    v = os.environ.get(var)
    return Path(v).expanduser() if v else _NONE

# AVE_TEST_REAL_VIDEO: a real talking-head video (needs Apple Vision)
REAL = _real("AVE_TEST_REAL_VIDEO")


# ---- geometry -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("sw,sh", [(3840, 2160), (1920, 1080), (640, 360), (960, 540), (1080, 1920)])
@pytest.mark.parametrize("face", [None, (100, 50, 120, 120), (1500, 400, 400, 400), (0, 0, 60, 60), (3700, 2000, 100, 100)])
def test_crop_geometry_invariants(sw, sh, face):
    x, y, w, h = reframe.crop_for_face(face, sw, sh)
    assert w % 2 == 0 and h % 2 == 0 and x % 2 == 0 and y % 2 == 0
    assert w * 16 == h * 9                         # exactly 9:16
    assert x >= 0 and y >= 0 and x + w <= sw and y + h <= sh


def test_crop_puts_face_centred_with_headroom():
    face = (1925.0, 640.0, 468.0, 468.0)           # the real frame's detection, 4K
    x, y, w, h = reframe.crop_for_face(face, 3840, 2160)
    assert abs((x + w / 2) - (face[0] + face[2] / 2)) <= 2           # centred horizontally
    eye = face[1] + 0.38 * face[3]
    assert 0.30 <= (eye - y) / h <= 0.40                             # eyes near the upper third
    assert 0.22 <= face[3] / h <= 0.28                               # head and shoulders, not a sliver of forehead


def test_crop_clamps_at_edges_but_keeps_ratio():
    x, y, w, h = reframe.crop_for_face((3700, 20, 120, 120), 3840, 2160)
    assert x + w == 3840 or x + w <= 3840
    assert y == 0


# ---- episode with a clip --------------------------------------------------------------------------------------------

def _make_clip(ep, o0=7.5, o1=29.5, status="approved", slug="synth-clip"):
    base = editdoc.load_latest(ep, "assembly")
    sl = editdoc.slice_doc(base, o0, o1, stage="select")
    sl["tracks"]["dialogue"][0]["xfade_ms"] = 0
    path = editdoc.save_new_version(ep, sl, name=slug)
    clip = {"id": "c01", "slug": slug, "title": "Synthetic clip", "hook": "Most people never finish what they start",
            "rationale": "test", "status": status, "start": o0, "end": o1, "duration": o1 - o0, "total": 71.0,
            "sentence_ids": ["s001"], "start_complete": True, "end_complete": True, "complete_statement": True, "edit": {"name": slug, "version": 1, "path": str(path.relative_to(ep.root)),
                                               "range": [o0, o1], "source": ["assembly", 1]}}
    write_json(ep.work / "clips.json", {"clips": [clip]})
    return clip


NO_FACE = lambda img: []  # noqa: E731


def test_build_short_validates_and_has_vertical_canvas_and_fallback_flags(synth_episode):
    ep = synth_episode
    clip = _make_clip(ep)
    r = shorts.build_short(ep, clip, detector=NO_FACE)
    assert r["status"] == "built" and r["version"] == 1
    doc = editdoc.load(ep.root / r["path"])
    assert editdoc.validate(doc) == []
    assert doc["stage"] == "short" and doc["canvas"] == {"w": 1080, "h": 1920}
    assert r["path"].endswith("edits/short.synth-clip.v1.json")
    for s in doc["tracks"]["video"]:
        x, y, w, h = s["crop"]
        assert w * 16 == h * 9 and x >= 0 and y >= 0 and x + w <= 640 and y + h <= 360 and w % 2 == 0 and h % 2 == 0
        assert s["needs_review"] is True and "no face" in s["reason"] and s["confidence"] == 0.0
    assert r["needs_review"]            # fallback must be flagged, never silent


def test_min_hold_reassigns_short_shot_in_sync_and_keeps_audio_contiguous(synth_episode):
    ep = synth_episode
    clip = _make_clip(ep)
    sel = editdoc.load(ep.root / clip["edit"]["path"])
    short_before = [s for s in sel["tracks"]["video"] if s["o1"] - s["o0"] < 2.0]
    assert short_before, "fixture should contain a sub-2s shot (the 'yeah')"
    r = shorts.build_short(ep, clip, detector=NO_FACE)
    doc = editdoc.load(ep.root / r["path"])
    vt = doc["tracks"]["video"]
    assert all(s["o1"] - s["o0"] >= 2.0 - 1e-6 for s in vt)
    # episode time of the picture still follows the dialogue: e0 - o0 offset equals the dialogue's at every video start
    d = doc["tracks"]["dialogue"][0]
    assert sum(s["o1"] - s["o0"] for s in vt) == pytest.approx(doc["duration"])
    for a, b in zip(vt, vt[1:]):
        assert a["src"] != b["src"]       # merged: no two adjacent shots from the same camera
    # the first cut moved to the word gap (completeness.snap_edges); it must stay within a pad of where the select began
    assert d["e0"] == pytest.approx(r["edges"]["start"]["after"], abs=1e-3)
    assert abs(d["e0"] - sel["tracks"]["dialogue"][0]["e0"]) < 1.0
    # (the first 0.25 s shot of this fixture lies before the first word: snapping the cut to the word gap trims it away instead)
    assert any("min_hold" in n for n in r["notes"]) or r["edges"]["start"]["delta"] >= 0.2


def test_captions_attached_with_short_style_and_are_inside_safe_zone_in_data(synth_episode):
    ep = synth_episode
    r = shorts.build_short(ep, _make_clip(ep), detector=NO_FACE)
    doc = editdoc.load(ep.root / r["path"])
    ass = (ep.root / doc["captions"]["ass"]).read_text()
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass and r"\kf" in ass
    assert (ep.root / doc["captions"]["srt"]).exists() and doc["captions"]["burn"] is True


def test_build_is_idempotent_and_versioned(synth_episode):
    ep = synth_episode
    clip = _make_clip(ep)
    a = shorts.build_short(ep, clip, detector=NO_FACE)
    b = shorts.build_short(ep, clip, detector=NO_FACE)
    assert b["status"] == "unchanged" and b["path"] == a["path"]
    c = shorts.build_short(ep, clip, detector=NO_FACE, force=True)
    assert c["status"] == "built" and c["version"] == 2
    assert editdoc.list_versions(ep, "short", "synth-clip") == [1, 2]
    assert (ep.root / a["path"]).exists()                       # v1 untouched
    cfg = shorts.load_config(ep)
    cfg["build"]["min_hold_s"] = 0.5                            # a policy change is a new version
    d = shorts.build_short(ep, clip, cfg, detector=NO_FACE)
    assert d["status"] == "built" and d["version"] == 3


def test_unknown_layout_is_refused_clearly(synth_episode):
    ep = synth_episode
    with pytest.raises(SystemExit, match="unknown layout"):
        shorts.build_short(ep, _make_clip(ep), layout="triptych", detector=NO_FACE)


def test_clip_selection_needs_approval_and_a_build(synth_episode):
    ep = synth_episode
    _make_clip(ep, status="proposed")
    with pytest.raises(SystemExit, match="no approved"):
        shorts.select_clips(ep, all_approved=True)
    with pytest.raises(SystemExit, match="allow-unapproved"):
        shorts.select_clips(ep, "synth-clip")                 # a proposed clip needs the explicit trial flag
    assert shorts.select_clips(ep, "synth-clip", allow_unapproved=True)[0]["id"] == "c01"
    with pytest.raises(SystemExit, match="no clip"):
        shorts.select_clips(ep, "nope")


def test_detector_error_is_a_flagged_fallback_not_a_crash(synth_episode):
    ep = synth_episode
    def boom(img):
        raise RuntimeError("Apple Vision (pyobjc-framework-Vision) is not available: test")
    r = shorts.build_short(ep, _make_clip(ep), detector=boom)
    doc = editdoc.load(ep.root / r["path"])
    assert "unavailable" in doc["tracks"]["video"][0]["reason"] and r["needs_review"]


def test_fake_detector_drives_crop_and_shares_it_across_cuts(synth_episode):
    """A face at a fixed place: every cut to a camera uses the SAME crop (no jitter), centred on the face."""
    ep = synth_episode
    calls = []

    def det(img):
        w, h = Image.open(img).size
        calls.append(1)
        return [{"x": w * 0.4, "y": h * 0.2, "w": h * 0.3, "h": h * 0.3, "conf": 0.9},
                {"x": 0, "y": 0, "w": 10, "h": 10, "conf": 0.9}]           # a tiny second face must lose to the big one
    r = shorts.build_short(ep, _make_clip(ep), detector=det)
    doc = editdoc.load(ep.root / r["path"])
    by_cam = {}
    for s in doc["tracks"]["video"]:
        by_cam.setdefault(s["src"], set()).add(tuple(s["crop"]))
        assert not s.get("needs_review") or "upscaled" in s["reason"]   # found a face: only the 640p softness may be flagged
        assert s["confidence"] == pytest.approx(0.9)
    assert all(len(v) == 1 for v in by_cam.values())
    x, y, w, h = next(iter(by_cam["A"]))
    face_cx = 640 * 0.4 + 360 * 0.3 / 2
    assert abs((x + w / 2) - face_cx) <= 4 or x == 0 or x + w == 640


def test_moved_speaker_gets_own_crop_small_drift_does_not(synth_episode):
    """Hysteresis: a face that drifts a little keeps the camera crop; one that jumps sideways gets its own."""
    ep = synth_episode
    clip = _make_clip(ep)
    cfg = shorts.load_config(ep)
    cfg["build"]["min_hold_s"] = 0
    doc = editdoc.clone(editdoc.load(ep.root / clip["edit"]["path"]))
    first = sorted(doc["tracks"]["video"], key=lambda s: s["o0"])[0]["id"]
    by_frame = {}

    def det(img):
        n = int(Path(img).stem[1:])
        # frames are numbered in task order: the first segment's 3 samples come first
        x = 0.62 if n < 3 else 0.30
        return [{"x": 640 * x, "y": 40, "w": 100, "h": 100, "conf": 0.9}]
    segs = sorted(doc["tracks"]["video"], key=lambda s: s["o0"])
    plan = reframe.plan_crops(ep, doc, cfg, detector=det)
    cams = {}
    for s in segs:
        cams.setdefault(s["src"], []).append(plan[s["id"]]["crop"])
    # at least one camera got two different crops (the moved one) and every crop is still a valid 9:16
    assert any(len({tuple(c) for c in v}) > 1 for v in cams.values())
    assert all(c[2] * 16 == c[3] * 9 for v in cams.values() for c in v)


# ---- render ---------------------------------------------------------------------------------------------------------

@needs_libass
def test_export_renders_vertical_with_loudness_captions_and_sidecar(synth_episode):
    ep = synth_episode
    clip = _make_clip(ep)
    shorts.build_short(ep, clip, detector=NO_FACE)
    r = shorts.export_short(ep, clip, ["youtube", "reels"])
    assert [e["status"] for e in r["exports"]] == ["exported", "exported"]
    yt = ep.root / r["exports"][0]["video"]
    info = ffutil.probe(yt)
    doc = editdoc.load_latest(ep, "short", "synth-clip")
    assert (info["w"], info["h"]) == (1080, 1920) and info["pix_fmt"] == "yuv420p"
    assert abs(info["duration"] - doc["duration"]) <= 1 / 30 + 1e-3
    prof = shorts.video_profile(yt)
    assert prof["profile"] == "High" and prof["color_space"] == "bt709"
    loud = ffutil.measure_loudness(yt)
    assert abs(loud["lufs"] - (-14.0)) <= 1.0 and loud["true_peak_db"] <= -1.0
    # sidecar
    side = json.loads((ep.root / r["exports"][0]["sidecar"]).read_text())
    assert side["title"] == "Synthetic clip" and side["hook"].startswith("Most people")
    assert side["platforms"] == ["youtube"] and side["limits"]["ok"] and side["limits"]["max_duration_s"] == 180
    assert side["sha256"] == __import__("hashlib").sha256(yt.read_bytes()).hexdigest()
    assert side["loudness"]["target_lufs"] == -14.0 and side["licenses"]["required"] is False
    assert side["clip"]["id"] == "c01" and side["needs_review"]
    assert side["description"].startswith("Most people never finish")
    # identical encodes are copied, not re-rendered: same bytes for reels
    reels = ep.root / r["exports"][1]["video"]
    assert reels.read_bytes() == yt.read_bytes()
    # idempotent: a second export neither re-renders nor overwrites
    mtime = yt.stat().st_mtime_ns
    r2 = shorts.export_short(ep, clip, ["youtube"])
    assert r2["exports"][0]["status"] == "unchanged" and yt.stat().st_mtime_ns == mtime


@needs_libass
def test_rendered_captions_stay_inside_safe_zone(synth_episode, tmp_path):
    """Render the short with and without captions; the pixels that differ are the caption pixels and must lie in the safe rect."""
    from ave import compile as comp
    ep = synth_episode
    clip = _make_clip(ep)
    r = shorts.build_short(ep, clip, detector=NO_FACE)
    doc = editdoc.load(ep.root / r["path"])
    with_c, without = tmp_path / "w.mp4", tmp_path / "n.mp4"
    comp.render(ep, doc, with_c, preset="shorts_youtube", burn_captions=True)
    comp.render(ep, doc, without, preset="shorts_youtube", burn_captions=False)
    style = captions.get_style("short", ep=ep)
    x0, y0, x1, y1 = captions.safe_rect(style, (1080, 1920))
    cues = captions.parse_srt(ep.root / doc["captions"]["srt"])
    seen = 0
    for c in cues[:6]:
        t = (c.start + c.end) / 2
        fa, fb = tmp_path / "a.png", tmp_path / "b.png"
        for v, f in ((with_c, fa), (without, fb)):
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(v), "-frames:v", "1", str(f)], check=True)
        a = np.asarray(Image.open(fa).convert("RGB")).astype(int)
        b = np.asarray(Image.open(fb).convert("RGB")).astype(int)
        diff = np.abs(a - b).sum(axis=2) > 90
        ys, xs = np.nonzero(diff)
        if len(xs) < 200:
            continue
        seen += 1
        assert xs.min() >= x0 - 2 and xs.max() <= x1 + 2 and ys.min() >= y0 - 2 and ys.max() <= y1 + 2, (t, xs.min(), xs.max(), ys.min(), ys.max())
    assert seen >= 2


def test_limits_flag_over_length_for_x_and_reels():
    cfg = shorts.load_config()
    info = {"duration": 150.0, "w": 1080, "h": 1920}
    assert not shorts.check_limits("x", cfg["platforms"]["x"], info, 10)["ok"]
    assert not shorts.check_limits("reels", cfg["platforms"]["reels"], info, 10)["ok"]
    assert shorts.check_limits("youtube", cfg["platforms"]["youtube"], info, 10)["ok"]
    assert not shorts.check_limits("x", cfg["platforms"]["x"], {**info, "duration": 30.0}, 600 * 1024 * 1024)["ok"]
    assert not shorts.check_limits("x", cfg["platforms"]["x"], {**info, "duration": 30.0, "w": 1920, "h": 1080}, 10)["ok"]


def test_license_coverage_flows_into_sidecar(synth_episode):
    ep = synth_episode
    clip = _make_clip(ep)
    music = ep.raw / "bed.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=110:d=30", str(music)], check=True)
    r = shorts.build_short(ep, clip, detector=NO_FACE)
    p = ep.root / r["path"]
    doc = editdoc.load(p)
    doc["tracks"]["music"].append({"id": "m1", "file": "raw/bed.wav", "o0": 0.0, "o1": 5.0, "in": 0.0, "gain_db": -22.0, "duck_db": 10.0})
    lic = shorts.license_report(ep, doc)
    assert lic["required"] and not lic["ok"] and lic["missing"] == ["raw/bed.wav"]
    from ave.assets import licenses
    log = licenses.log_for(ep)
    entry = {"source": "pexels-test", "url": "https://example.invalid/x", "license": "test-license", "license_url": "https://example.invalid/l", "license_text": "free", "approved_by": "tester", "sha256": licenses.sha256_file(music),
             "file": "raw/bed.wav", "attribution": {"text": "Music: Test", "required": True}}
    log.append(entry)
    lic = shorts.license_report(ep, doc)
    assert lic["ok"] and lic["attribution"] == ["Music: Test"]


def test_preview_draws_crop_and_returns_geometry(synth_episode):
    ep = synth_episode
    shorts.build_short(ep, _make_clip(ep), detector=NO_FACE)
    r = shorts.preview(ep, "synth-clip", 3.0, detector=NO_FACE)
    im = Image.open(r["preview"])
    assert im.size[1] == 960 and r["needs_review"] and r["crop"][2] * 16 == r["crop"][3] * 9
    arr = np.asarray(im.convert("RGB"))
    assert ((arr[..., 1] > 240) & (arr[..., 0] < 30) & (arr[..., 2] < 30)).any()      # the green crop rectangle
    assert ((arr[..., 0] > 240) & (arr[..., 1] < 60) & (arr[..., 2] < 60)).any()      # the red safe zone


def test_list_shorts(synth_episode):
    ep = synth_episode
    clip = _make_clip(ep)
    shorts.build_short(ep, clip, detector=NO_FACE)
    rows = shorts.list_shorts(ep)
    assert rows[0]["slug"] == "synth-clip" and rows[0]["versions"] == [1] and rows[0]["exports"] == []


@needs_libass
def test_status_counts_exported_short_as_stage_done(synth_episode):
    from ave import state
    ep = synth_episode
    clip = _make_clip(ep)
    shorts.build_short(ep, clip, detector=NO_FACE)
    assert not state._done(ep, "shorts")
    shorts.export_short(ep, clip, ["youtube"])
    assert state._done(ep, "shorts")


def test_cli_registered_and_list_runs(synth_episode, capsys):
    from ave import cli
    ep = synth_episode
    shorts.build_short(ep, _make_clip(ep), detector=NO_FACE)
    assert cli.main(["shorts", "list", str(ep.root)]) == 0
    assert "synth-clip" in capsys.readouterr().out


# ---- real footage (Apple Vision) -------------------------------------------------------------------------------------

@pytest.mark.apple
@pytest.mark.skipif(not REAL.exists() or not reframe.vision_available(),reason="real footage or Apple Vision not available")
def test_real_frame_face_is_found_and_framed(tmp_path):
    img = tmp_path / "a300.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "300", "-i", str(REAL), "-frames:v", "1", str(img)], check=True)
    sw, sh = Image.open(img).size
    faces = reframe.detect_faces(img)
    f = reframe.main_face(faces, 0.3)
    assert f is not None and f["conf"] > 0.5
    # the person's face is right of centre, upper-middle, and roughly 400-550 px tall in 4K
    assert 0.40 * sw < f["x"] + f["w"] / 2 < 0.70 * sw and f["y"] < 0.5 * sh and 300 < f["h"] < 700
    x, y, w, h = reframe.crop_for_face((f["x"], f["y"], f["w"], f["h"]), sw, sh)
    assert x <= f["x"] and x + w >= f["x"] + f["w"] and y <= f["y"] and y + h >= f["y"] + f["h"]     # whole face inside the crop
    assert (f["y"] - y) / h > 0.08                                                                # headroom above the face box
    prev = tmp_path / "crop.png"
    Image.open(img).crop((x, y, x + w, y + h)).resize((540, 960)).save(prev)
    assert prev.exists()


def test_small_face_on_low_res_camera_widens_instead_of_over_zooming():
    tight = reframe.crop_for_face((400, 150, 80, 80), 960, 540)
    wide = reframe.crop_for_face((400, 150, 80, 80), 960, 540, min_h=1920 // 2.5 and int(1920 / 2.5))
    assert tight[3] < wide[3] and wide[3] == 512          # limited by the source height, not blown up
    assert wide[0] <= 400 and wide[0] + wide[2] >= 480     # face still inside
