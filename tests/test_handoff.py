import pytest

from ave import editdoc, handoff
from conftest import needs_ffmpeg

pytestmark = needs_ffmpeg


def test_plan_places_every_segment_contiguously_with_exclusive_ends(synth_episode):
    doc = editdoc.load_latest(synth_episode, "assembly")
    p = handoff.plan(synth_episode, doc, name="t")
    v = p["video"]
    assert all(a["record_frame"] + a["frames"] == b["record_frame"] for a, b in zip(v, v[1:]))
    assert all(x["end_frame"] - x["start_frame"] == x["frames"] for x in v)         # exclusive end
    assert p["timeline_frames"] == v[-1]["record_frame"] + v[-1]["frames"]
    assert p["warnings"] and "hard-cut" in p["warnings"][0]                           # no master: warned


def test_camera_b_uses_its_own_clock(synth_episode):
    doc = editdoc.load_latest(synth_episode, "assembly")
    p = handoff.plan(synth_episode, doc, name="t")
    seg = next(s for s in doc["tracks"]["video"] if s["src"] == "B")
    item = next(x for x in p["video"] if x["id"] == seg["id"])
    assert item["start_frame"] == round((seg["e0"] - 1.0) * doc["fps"])               # B started 1.0 s late


def test_master_audio_replaces_cut_audio_and_music_overlays_get_tracks(synth_episode, tmp_path):
    doc = editdoc.clone(editdoc.load_latest(synth_episode, "assembly"))
    d = doc["duration"]
    doc["tracks"]["music"] = [{"id": "m1", "file": "work/m.wav", "o0": 0, "o1": d, "in": 2.0, "gain_db": -20}]
    doc["tracks"]["overlays"] = [{"id": "o1", "file": "work/lt.mov", "o0": 3, "o1": 7, "x": 0, "y": 0, "w": 1, "h": 1, "layer": 1}]
    master = tmp_path / "master.wav"; master.write_bytes(b"x")
    p = handoff.plan(synth_episode, doc, name="t", audio_master=master)
    assert len(p["audio"]) == 1 and p["audio"][0]["frames"] == p["timeline_frames"] and not p["warnings"]
    assert p["music"][0]["track_index"] == 2 and p["music"][0]["start_frame"] == 60
    assert p["overlays"][0]["track_index"] == 2 and p["overlays"][0]["frames"] == 120


def test_camera_not_recording_is_an_error_not_a_bad_placement(synth_episode):
    doc = editdoc.clone(editdoc.load_latest(synth_episode, "assembly"))
    doc["tracks"]["video"][0]["src"] = "B"        # B starts 1.0 s into the episode but this segment is at e0=0
    with pytest.raises(ValueError, match="not recording"):
        handoff.plan(synth_episode, doc, name="t")


def test_handoff_source_frames_apply_drift(synth_episode):
    from ave import editdoc, handoff
    doc = editdoc.load_latest(synth_episode, "assembly")
    doc["sources"]["B"]["drift_ppm"] = 5000.0                # absurd on purpose: 0.5% so the frame shift is unmistakable
    seg = next(s for s in doc["tracks"]["video"] if s["src"] == "B")
    p = handoff.plan(synth_episode, doc, name="x")
    got = next(v for v in p["video"] if v["id"] == seg["id"])["start_frame"]
    assert got == round(editdoc.source_time(doc, "B", seg["e0"]) * doc["fps"])


def test_timeline_names_use_the_episode_title_and_plan_carries_the_project(synth_episode):
    import json
    m = json.loads(synth_episode.meta_path.read_text()); m["title"] = "The Long Table 00"; synth_episode.meta_path.write_text(json.dumps(m))
    doc = editdoc.load_latest(synth_episode, "assembly")
    name = handoff.timeline_name(synth_episode, doc, 2, "rough s1")
    assert name == "The Long Table 00 rough s1 v2"                       # not the folder name
    assert handoff.plan(synth_episode, doc, name=name, project="The Long Table 00")["project"] == "The Long Table 00"
    assert handoff.plan(synth_episode, doc, name=name)["project"] is None
