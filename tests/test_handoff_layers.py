"""Layered (work-in-progress) handoff: plan-level tests on the synthetic episode, plus compile_source_sum."""
import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from ave import compile as comp
from ave import editdoc, handoff
from conftest import needs_ffmpeg

pytestmark = needs_ffmpeg
FPS = 30


def _doc_with_cuts(ep):
    """12 s edit with two removed sections. Cameras: A (whole episode) and B (starts 1.0 s into the episode).
    r1 = episode 0.0-0.9 (B not recording), r2 = episode 10-19 (v2+v3 are contiguous and must merge, d2+d3 too)."""
    doc = editdoc.clone(editdoc.load_latest(ep, "assembly"))
    V = lambda i, s, o0, o1, e0: {"id": i, "src": s, "o0": o0, "o1": o1, "e0": e0, "crop": None, "zoom": 1.0}  # noqa: E731
    doc["tracks"]["video"] = [V("v1", "A", 0.0, 0.9, 0.0), V("v2", "B", 0.9, 3.9, 10.0), V("v3", "A", 3.9, 9.9, 13.0),
                              V("v4", "B", 9.9, 12.0, 19.0)]
    D = lambda i, o0, o1, e0: {"id": i, "src": "A", "o0": o0, "o1": o1, "e0": e0, "xfade_ms": 0, "gain_db": 0.0}  # noqa: E731
    doc["tracks"]["dialogue"] = [D("d1", 0.0, 0.9, 0.0), D("d2", 0.9, 5.0, 10.0), D("d3", 5.0, 12.0, 14.1)]
    doc["duration"] = 12.0
    assert editdoc.validate(doc) == []
    return doc


def _plan(ep, doc=None, **kw):
    doc = doc or editdoc.load_latest(ep, "assembly")
    return handoff.plan(ep, doc, name="t", layers=True, **kw)


def test_default_plan_is_untouched_by_the_layers_flag(synth_episode):
    doc = editdoc.load_latest(synth_episode, "assembly")
    a = handoff.plan(synth_episode, doc, name="t")
    b = handoff.plan(synth_episode, doc, name="t", layers=False)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert "layers" not in a and set(a) == {"name", "project", "stage", "fps", "canvas", "timeline_frames", "import", "video", "audio",
                                            "music", "overlays", "subtitles", "warnings"}


def test_alt_track_per_camera_named_by_role_and_disabled(layout_episode):
    ep = layout_episode
    p = _plan(ep)
    L = p["layers"]
    vt = L["tracks"]["video"]
    assert vt["1"]["name"] == "Edit" and vt["1"]["disabled"] is False
    names = {v["name"]: v for k, v in vt.items() if k != "1"}
    assert set(names) == {"host (alt)", "guest (alt)"}
    assert all(v["disabled"] for v in names.values())
    assert sorted(int(k) for k in vt) == [1, 2, 3]
    assert {x["track_index"] for x in L["alt_video"]} == {2, 3}
    assert all(x["media_type"] == 1 for x in L["alt_video"])


def test_alt_clips_follow_kept_ranges_merge_contiguous_and_skip_uncovered(synth_episode):
    doc = _doc_with_cuts(synth_episode)
    p = _plan(synth_episode, doc)
    alt = p["layers"]["alt_video"]
    a = [x for x in alt if x["camera"] == "A"]
    b = [x for x in alt if x["camera"] == "B"]
    # v2 ends at episode 13.0 == v3.e0 and v3 ends at 19.0 == v4.e0: v2..v4 are ONE range; only the removed 0.9 -> 10 s jump splits.
    assert [x["frames"] for x in a] == [27, 333]                       # 0.9 s, then 11.1 s (333 frames)
    assert [x["record_frame"] for x in a] == [0, 27]
    assert a[0]["start_frame"] == 0 and a[1]["start_frame"] == 300      # episode 10 s on a camera that started at 0
    # camera B was not recording during r1 (episode 0-0.9 s): skipped, with a warning, and r2 follows in sync
    assert [x["record_frame"] for x in b] == [27] and b[0]["start_frame"] == round((10.0 - 1.0) * FPS)
    assert any("guest" in w and "not recording" in w for w in p["warnings"])


def test_contiguity_tolerance_is_one_millisecond(synth_episode):
    doc = _doc_with_cuts(synth_episode)
    v = doc["tracks"]["video"]
    v[3]["e0"] += 0.0005                                               # v4 now starts 0.5 ms late: still contiguous
    n_ok = len([x for x in _plan(synth_episode, doc)["layers"]["alt_video"] if x["camera"] == "A"])
    v[3]["e0"] += 0.02                                                 # 20 ms jump: a real cut
    n_cut = len([x for x in _plan(synth_episode, doc)["layers"]["alt_video"] if x["camera"] == "A"])
    assert n_cut == n_ok + 1


def test_alt_clip_frames_apply_source_drift(synth_episode):
    doc = _doc_with_cuts(synth_episode)
    doc["sources"]["B"]["drift_ppm"] = 5000.0
    p = _plan(synth_episode, doc)
    b = [x for x in p["layers"]["alt_video"] if x["camera"] == "B"][0]
    assert b["start_frame"] == round(editdoc.source_time(doc, "B", 10.0) * FPS)
    assert b["end_frame"] - b["start_frame"] == b["frames"]            # exclusive end


def test_camera_that_starts_late_is_trimmed_not_misplaced(synth_episode):
    p = _plan(synth_episode)                                           # one range from episode 0 s; B starts at 1.0 s
    b = [x for x in p["layers"]["alt_video"] if x["camera"] == "B"]
    assert len(b) == 1 and b[0]["record_frame"] == FPS and b[0]["start_frame"] == 0
    assert b[0]["record_frame"] + b[0]["frames"] == p["timeline_frames"]
    assert any("starts" in w or "trimmed" in w for w in p["warnings"])


def test_camera_that_ends_early_is_trimmed(synth_episode):
    meta = json.loads(synth_episode.artifact("media.json").read_text())
    for f in meta["files"]:
        if f["file"].endswith("camB.mp4"):
            f["duration"] = 20.0                                       # B file ends at episode 21 s
    synth_episode.artifact("media.json").write_text(json.dumps(meta))
    p = _plan(synth_episode)
    b = [x for x in p["layers"]["alt_video"] if x["camera"] == "B"][0]
    assert b["start_frame"] + b["frames"] == 20 * FPS and b["end_frame"] == 20 * FPS


def test_dialogue_clips_tile_the_timeline_with_exclusive_ends(synth_episode):
    doc = _doc_with_cuts(synth_episode)
    p = _plan(synth_episode, doc)
    clips = p["layers"]["audio_clips"]
    assert [c["id"] for c in clips] == ["d1", "d2", "d3"]              # one clip per dialogue segment (NOT merged)
    assert clips[0]["record_frame"] == 0
    for a, b in zip(clips, clips[1:]):
        assert a["record_frame"] + a["frames"] == b["record_frame"]
    assert clips[-1]["record_frame"] + clips[-1]["frames"] == p["timeline_frames"]
    assert all(c["end_frame"] - c["start_frame"] == c["frames"] and c["track_index"] == 1 and c["media_type"] == 2 for c in clips)
    assert clips[1]["start_frame"] == 300 and clips[2]["start_frame"] == round(14.1 * FPS)
    sums = {c["file"] for c in clips}
    assert len(sums) == 1 and next(iter(sums)).endswith(".sum.wav") and next(iter(sums)) in p["import"]


def test_baked_master_goes_on_a2_muted_and_replaces_the_top_level_audio(synth_episode, tmp_path):
    master = tmp_path / "master.wav"; master.write_bytes(b"x")
    p = _plan(synth_episode, audio_master=master)
    L = p["layers"]
    assert p["audio"] == []                                            # A1 now belongs to the dialogue clips
    assert L["baked_master"]["track_index"] == 2 and L["baked_master"]["frames"] == p["timeline_frames"]
    assert L["baked_master"]["file"] == str(master.resolve())
    at = L["tracks"]["audio"]
    assert at["1"]["disabled"] is False and at["2"]["disabled"] is True and "baked" in at["2"]["name"].lower()
    assert not any("hard-cut" in w for w in p["warnings"])
    p2 = _plan(synth_episode)                                          # no master: say so, no A2
    assert p2["layers"]["baked_master"] is None and "2" not in p2["layers"]["tracks"]["audio"]
    assert any("baked" in w for w in p2["warnings"])


def test_music_and_overlays_move_above_the_layer_tracks(synth_episode):
    doc = editdoc.clone(editdoc.load_latest(synth_episode, "assembly"))
    d = doc["duration"]
    doc["tracks"]["music"] = [{"id": "m1", "file": "work/m.wav", "o0": 0, "o1": d, "in": 0.0}]
    doc["tracks"]["overlays"] = [{"id": "o1", "file": "work/lt.mov", "o0": 3, "o1": 7, "x": 0, "y": 0, "w": 1, "h": 1, "layer": 1}]
    p = _plan(synth_episode, doc)
    n_alt = len(p["layers"]["tracks"]["video"]) - 1
    assert p["music"][0]["track_index"] == 3 and p["overlays"][0]["track_index"] == 2 + n_alt


def test_counts_and_snapping_note(synth_episode):
    p = _plan(synth_episode, _doc_with_cuts(synth_episode))
    c = p["layers"]["counts"]
    assert c["video"]["1"] == 4 and c["audio"]["1"] == 3 and c["video"]["2"] == 2
    assert "frame" in p["layers"]["note"] and "crossfade" in p["layers"]["note"]


def test_layers_refuses_an_invalid_doc(synth_episode):
    doc = editdoc.clone(editdoc.load_latest(synth_episode, "assembly"))
    doc["tracks"]["video"][0]["src"] = "nope"
    with pytest.raises(ValueError, match="invalid EditDoc"):
        _plan(synth_episode, doc)


def test_room_tone_fill_is_tiled_by_clips_of_the_room_tone(synth_episode):
    doc = _doc_with_cuts(synth_episode)
    doc["tracks"]["dialogue"][0] = {"id": "d1", "src": "A", "fill": "room_tone", "o0": 0.0, "o1": 0.9, "e0": 0.0}
    doc["tracks"]["room_tone"] = {"src": "A", "e0": 6.0, "e1": 6.5}                      # 0.5 s of tone for a 0.9 s fill
    p = _plan(synth_episode, doc)
    clips = p["layers"]["audio_clips"]
    first = [c for c in clips if c["id"].startswith("d1")]
    assert sum(c["frames"] for c in first) == 27 and len(first) == 2 and first[0]["start_frame"] == 180
    assert clips[0]["record_frame"] == 0 and clips[-1]["record_frame"] + clips[-1]["frames"] == p["timeline_frames"]


# ---- compile_source_sum --------------------------------------------------------------------------------------------

def _stereo_source(ep, tmp_name, left, right, channel_map=None, dur=3.0, sr=48000):
    t = np.arange(int(dur * sr)) / sr
    L = left * np.sin(2 * np.pi * 220 * t)
    R = right * np.sin(2 * np.pi * 330 * t)
    f = ep.root / tmp_name
    sf.write(f, np.stack([L, R], axis=1), sr, subtype="PCM_24")
    src = {"file": tmp_name, "kind": "audio", "start_offset": 0.0, "role": "audio"}
    if channel_map:
        src["channel_map"] = channel_map
    doc = {"sources": {"M": src}}
    return doc


def _read(path):
    return sf.read(path, dtype="float64")


@pytest.mark.parametrize("use,expect_peak", [("sum", 0.3 + 0.0), ("left", 0.3), ("right", 0.3)])
def test_compile_source_sum_dual_mono_24bit_and_channel_map(synth_episode, tmp_path, use, expect_peak):
    # left carries 0.3, right is a dead channel (0.0): every mapping must put the live signal on BOTH ears
    live = {"sum": (0.3, 0.0), "left": (0.3, 0.0), "right": (0.0, 0.3)}[use]
    doc = _stereo_source(synth_episode, "m.wav", *live, channel_map=[{"start": 0.0, "end": 3.0, "use": use}])
    out = comp.compile_source_sum(synth_episode, doc, "M", tmp_path / "m.sum.wav")
    info = sf.info(out)
    assert (info.samplerate, info.channels, info.subtype) == (48000, 2, "PCM_24")
    x, _ = _read(out)
    assert np.array_equal(x[:, 0], x[:, 1])                              # dual mono
    assert abs(len(x) - 3 * 48000) <= 48
    assert abs(np.abs(x).max() - expect_peak) < 0.01


def test_compile_source_sum_sums_both_speakers_and_soft_limits(synth_episode, tmp_path):
    doc = _stereo_source(synth_episode, "m.wav", 0.4, 0.4, channel_map=[{"start": 0.0, "end": 3.0, "use": "sum"}])
    x, _ = _read(comp.compile_source_sum(synth_episode, doc, "M", tmp_path / "s.wav"))
    ref, _ = sf.read(synth_episode.root / "m.wav", dtype="float64")
    expect = ref.sum(axis=1)
    assert np.abs(expect).max() > 0.7                                    # both tones present in the sum
    assert np.abs(x[:, 0]).max() <= 1.0
    assert np.corrcoef(x[1000:-1000, 0], expect[1000:-1000])[0, 1] > 0.99


def test_compile_source_sum_mix_is_the_average_and_levels_match(synth_episode, tmp_path):
    doc = _stereo_source(synth_episode, "m.wav", 0.4, 0.4, channel_map=[{"start": 0.0, "end": 3.0, "use": "mix"}])
    x, _ = _read(comp.compile_source_sum(synth_episode, doc, "M", tmp_path / "x.wav"))
    ref, _ = sf.read(synth_episode.root / "m.wav", dtype="float64")
    assert np.allclose(x[2000:-2000, 0], ref.mean(axis=1)[2000:-2000], atol=2e-4)


def test_compile_source_sum_on_a_real_recorder(synth_episode, tmp_path):
    doc = editdoc.load_latest(synth_episode, "assembly")
    doc["sources"]["R"] = {"file": str(next(synth_episode.root.rglob("recorder.wav")).relative_to(synth_episode.root)), "kind": "audio",
                           "start_offset": 0.5, "role": "audio"}
    out = comp.compile_source_sum(synth_episode, doc, "R", tmp_path / "r.sum.wav")
    x, sr = _read(out)
    assert sr == 48000 and abs(len(x) - 29.5 * 48000) < 200 and np.abs(x).max() > 0.3


def test_sum_wav_path_is_per_source_and_inside_the_episode_work_dir(synth_episode):
    doc = editdoc.load_latest(synth_episode, "assembly")
    pa, pb = handoff.sum_wav_path(synth_episode, doc, "A"), handoff.sum_wav_path(synth_episode, doc, "B")
    assert pa != pb and pa.parent == synth_episode.work / "render" and pa.name.endswith(".sum.wav")
