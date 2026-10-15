"""F2 regressions from the first real-episode shorts run (4K, two cameras, trial clips)."""

import json

import pytest

from ave import editdoc, ffutil, reframe, shorts
from ave.episode import write_json

from test_shorts import NO_FACE, _make_clip

pytestmark = pytest.mark.skipif(not __import__("shutil").which("ffmpeg"), reason="ffmpeg not installed")


def test_trial_clip_without_select_is_cut_on_the_fly_and_exported_only_to_drafts(synth_episode):
    """--allow-unapproved: a never-built proposed clip gets a select EditDoc without touching clips.json, and its files go to
    exports/shorts_drafts/ (the publish folder exports/shorts/ stays empty)."""
    ep = synth_episode
    clip = _make_clip(ep, status="proposed")
    base = editdoc.load_latest(ep, "assembly")
    clip.pop("edit")
    clip["source"] = {"stage": "assembly", "name": None, "version": 1, "fingerprint": "x"}
    write_json(ep.work / "clips.json", {"clips": [clip], "rubric": {}})
    before = (ep.work / "clips.json").read_text()
    from ave import clips as C
    orig = C._source_docs
    C._source_docs = lambda ep_, rubric, chosen: {("assembly", None, 1): base}
    try:
        got = shorts.select_clips(ep, "synth-clip", allow_unapproved=True)[0]
        again = shorts.select_clips(ep, "synth-clip", allow_unapproved=True)[0]
    finally:
        C._source_docs = orig
    assert got["edit"]["path"] == again["edit"]["path"]                 # reused, not re-cut each time
    assert (ep.work / "clips.json").read_text() == before
    sel = editdoc.load(ep.root / got["edit"]["path"])
    assert sel["tracks"]["dialogue"][0]["xfade_ms"] == 0 and sel["draft_select"]["clip"] == "c01"
    shorts.build_short(ep, got, detector=NO_FACE)
    r = shorts.export_short(ep, got, ["youtube"])
    assert "shorts_drafts" in r["exports"][0]["video"]
    assert not (ep.exports / "shorts").exists() or not list((ep.exports / "shorts").glob("*"))
    side = json.loads((ep.root / r["exports"][0]["sidecar"]).read_text())
    assert side["draft"] is True


def test_short_loudness_lands_on_target_not_below_it(synth_episode):
    """Regression: the long-form limiter ceiling (-3.5 dBFS) left real shorts at -14.8/-14.9 LUFS; shorts start higher."""
    ep = synth_episode
    clip = _make_clip(ep)
    shorts.build_short(ep, clip, detector=NO_FACE)
    r = shorts.export_short(ep, clip, ["youtube"])
    loud = ffutil.measure_loudness(ep.root / r["exports"][0]["video"])
    assert abs(loud["lufs"] + 14.0) <= 0.5 and loud["true_peak_db"] <= -1.0


def test_limits_check_bitrate_and_fps():
    spec = {"label": "X", "max_duration_s": 100, "min_duration_s": 0, "max_bytes": 10**12, "max_kbps": 1000, "max_fps": 30}
    info = {"duration": 10.0, "w": 1080, "h": 1920, "fps": 60.0}
    res = shorts.check_limits("x", spec, info, 5_000_000)           # 4000 kbps, 60 fps
    assert not res["ok"] and any("bitrate" in p for p in res["problems"]) and any("fps" in p for p in res["problems"])
    assert shorts.check_limits("x", spec, {**info, "fps": 30.0}, 1_000_000)["ok"]


def test_crop_motion_stats_static_camera_is_zero_and_moves_are_measured():
    def seg(i, cam, c, o0):
        return {"id": i, "src": cam, "o0": o0, "o1": o0 + 2, "crop": c}
    doc = {"tracks": {"video": [seg("a", "A", [100, 0, 540, 960], 0), seg("b", "B", [300, 0, 540, 960], 2),
                                seg("c", "A", [100, 0, 540, 960], 4), seg("d", "A", [154, 0, 540, 960], 6)]}}
    st = reframe.crop_motion_stats(doc)
    assert st["B"]["max_centre_move"] == 0 and st["A"]["distinct_crops"] == 2
    assert st["A"]["max_centre_move"] == pytest.approx(0.1)          # 54 px of a 540 px wide crop; the A->B cut is not motion


def _doc(*segs):
    return {"tracks": {"video": [{"id": f"v{i}", "src": s, "o0": o0, "o1": o1, "e0": e0, "zoom": z}
                                 for i, (s, o0, o1, e0, z) in enumerate(segs)]}}


def test_jump_cut_on_same_camera_gets_alternating_punch_in():
    """Real episode: shots B1,B1,B1 with content cut out between them came out at one framing (the rough cut's 1.18x punch-ins
    were dropped when the crop was planned) = visible jump cuts. Now they alternate."""
    doc = _doc(("B", 0, 3, 100, 1.0), ("B", 3, 4, 104, 1.0), ("B", 4, 9, 110, 1.0), ("A", 9, 12, 115, 1.0), ("A", 12, 14, 120, 1.0))
    assert len(shorts.unmasked_jump_cuts(doc)) == 3
    notes = shorts.mask_jump_cuts(doc, 1.15, dwell_s=0)       # dwell 0 = the alternating behaviour; the dwell rule is in test_zoom_dwell.py
    assert [s["zoom"] for s in doc["tracks"]["video"]] == [1.0, 1.15, 1.0, 1.0, 1.15] and len(notes) == 2     # v2 already differs from v1 once v1 is zoomed
    assert shorts.unmasked_jump_cuts(doc) == []
    assert shorts.mask_jump_cuts(_doc(("B", 0, 3, 100, 1.0), ("B", 3, 4, 110, 1.0)), 1.0) == []      # 1.0 = off


def test_continuous_shots_and_existing_punch_ins_are_left_alone():
    doc = _doc(("B", 0, 3, 100, 1.0), ("B", 3, 5, 103, 1.0), ("B", 5, 8, 120, 1.18))
    assert shorts.unmasked_jump_cuts(doc) == []                      # first pair is continuous, second already differs in framing


def test_build_keeps_rough_cut_punch_in(synth_episode):
    ep = synth_episode
    clip = _make_clip(ep)
    sel_p = ep.root / clip["edit"]["path"]
    sel = editdoc.load(sel_p)
    for seg in sel["tracks"]["video"]:
        seg["zoom"] = 1.18
    write_json(sel_p, sel)
    r = shorts.build_short(ep, clip, detector=NO_FACE, force=True)
    doc = editdoc.load(ep.root / r["path"])
    assert max(s["zoom"] for s in doc["tracks"]["video"]) >= 1.18


def _pixel(mp4, t, x, y):
    import subprocess
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(mp4), "-frames:v", "1", "-vf", f"crop=2:2:{x}:{y},scale=1:1",
                        "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True)
    return tuple(r.stdout[:3])


def test_stack_layout_puts_both_speakers_on_screen_one_above_the_other(synth_episode):
    """Opt-in two-up: camera A (blue, S1) in the top 1080x960, camera B (red, S2) below, for the whole clip; captions on the seam."""
    ep = synth_episode
    clip = _make_clip(ep)
    r = shorts.build_short(ep, clip, layout="stack", detector=NO_FACE)
    doc = editdoc.load(ep.root / r["path"])
    assert editdoc.validate(doc) == [] and doc["short"]["layout"] == "stack"
    sp = doc["short"]["stack"]
    assert sp["order"] == ["A", "B"]
    for cam in sp["order"]:
        for lay in sp["layers"][cam]:
            x, y, w, h = lay["crop"]
            assert w * 8 == h * 9 and x >= 0 and y >= 0 and x + w <= 640 and y + h <= 360       # 9:8 = 1080x960
    assert "PlayResY: 1920" in (ep.root / doc["captions"]["ass"]).read_text()
    out = shorts.export_short(ep, clip, ["youtube"])
    mp4 = ep.root / out["exports"][0]["video"]
    info = ffutil.probe(mp4)
    assert (info["w"], info["h"]) == (1080, 1920)
    for t in (3.0, 10.0, 18.0):
        top, bottom = _pixel(mp4, t, 40, 100), _pixel(mp4, t, 40, 1800)
        assert top[2] > 150 and top[0] < 100, top                    # blue on top
        assert bottom[0] > 150 and bottom[2] < 100, bottom           # red below
    assert abs(ffutil.measure_loudness(mp4)["lufs"] + 14.0) <= 0.5


def test_audiogram_fallback_short_has_waveform_captions_and_normal_audio(synth_episode):
    ep = synth_episode
    clip = _make_clip(ep)
    r = shorts.build_short(ep, clip, layout="audiogram", detector=NO_FACE)
    doc = editdoc.load(ep.root / r["path"])
    assert editdoc.validate(doc) == [] and doc["short"]["layout"] == "audiogram" and doc["tracks"]["video"][0]["src"] == "WAVE"
    assert (ep.root / doc["sources"]["WAVE"]["file"]).exists() and r["notes"]
    out = shorts.export_short(ep, clip, ["youtube"])
    mp4 = ep.root / out["exports"][0]["video"]
    info = ffutil.probe(mp4)
    assert (info["w"], info["h"]) == (1080, 1920) and out["exports"][0]["limits_ok"]
    # the card is the dark background at the top and carries a bright waveform band during speech
    assert max(_pixel(mp4, 1.0, 540, 20)) < 60
    import subprocess
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", "2", "-i", str(mp4), "-frames:v", "1", "-vf", "crop=900:380:90:600,scale=90:38",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    assert sum(1 for i in range(0, len(raw), 3) if raw[i] > 180 and raw[i + 1] > 150) > 20       # yellow waveform pixels
    assert abs(ffutil.measure_loudness(mp4)["lufs"] + 14.0) <= 0.5


def test_vertical_lower_third_clears_platform_ui_and_the_caption_line(tmp_path):
    """Real run: the landscape lower-third margin put the name card at ~82% of a 1080x1920 frame, inside the bottom-25% platform UI
    zone, phone-small. In portrait it now sits above the caption block (65%) and clear of the UI zone."""
    from ave import motion
    sp = motion.normalize_spec({"template": "lower-third", "props": {"name": "Maren", "title": "The Long Table"}, "duration": 5,
                                "fps": 30, "canvas": (1080, 1920)})
    out = motion.render_still(sp, 2.5, tmp_path / "lt.png")
    from PIL import Image
    bbox = Image.open(out).getchannel("A").point(lambda a: 255 if a > 8 else 0).getbbox()
    assert bbox and bbox[3] <= 0.57 * 1920, bbox                  # bottom edge above the caption block's top (~60%)
    assert bbox[2] - bbox[0] >= 0.3 * 1080                        # phone-readable size, not a 1080p-sized sliver


def test_motion_place_on_a_named_short_doc(synth_episode):
    from ave import motion
    ep = synth_episode
    clip = _make_clip(ep)
    r = shorts.build_short(ep, clip, detector=NO_FACE)
    res = motion.place(ep, "lower-third", {"name": "Host", "title": "Show"}, at=1.0, dur=3.0, stage="short", name="synth-clip")
    assert res["doc"].endswith("short.synth-clip.v2.json")
    doc = editdoc.load(res["doc"])
    assert doc["canvas"] == {"w": 1080, "h": 1920} and doc["tracks"]["overlays"][0]["h"] == 1920
    assert editdoc.list_versions(ep, "short", "synth-clip") == [1, 2] and r["version"] == 1


def test_stack_needs_two_speaker_cameras(synth_episode):
    ep = synth_episode
    from ave import stack
    with pytest.raises(SystemExit, match="two speaker cameras"):
        stack.pick_cameras(ep, {}, {"A": "S1", "B": "S1"})


def test_listener_cutaways_are_flagged_and_speaker_mode_restores_the_talker(synth_episode):
    ep = synth_episode
    r = shorts.build_short(ep, _make_clip(ep), detector=NO_FACE)
    doc = editdoc.load(ep.root / r["path"])
    assert "cutaways" in doc["short"]
    cams = shorts.camera_speakers(ep, doc)
    spk = shorts.segment_speakers(ep, doc)
    s = next(x for x in doc["tracks"]["video"] if spk[x["id"]] and cams[x["src"]] == spk[x["id"]])
    s["src"] = next(c for c in cams if c != s["src"])                # force a cutaway to the listener
    assert s["id"] in [c["id"] for c in shorts.listener_cutaways(ep, doc)]
    notes = shorts.prefer_speaker_camera(ep, doc)
    assert notes and shorts.listener_cutaways(ep, doc) == []
    assert sum(x["o1"] - x["o0"] for x in doc["tracks"]["video"]) == pytest.approx(doc["duration"])
