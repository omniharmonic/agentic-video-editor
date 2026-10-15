"""N1 end to end: per-session cut lists and assemblies, uncovered regions excluded, named edits through every stage."""
import json

import pytest

import fixtures as fx
from ave import assemble, cleanup, cutplan, editdoc, final, handoff, sessions, state
from ave import compile as comp
from ave.cli import main
from ave.episode import read_json

from conftest import needs_ffmpeg

pytestmark = needs_ffmpeg

S1_END = 3.0 + 90.0 * 1.00008        # camera A1 covers 3.0 .. 93.007 on the recorder timeline (drift applied)
S2_START, S2_END = 111.0, 111.0 + 80.0 * 1.00007


def cli(capsys, *argv):
    assert main(list(argv)) == 0
    return json.loads(capsys.readouterr().out)


# ---- pure pieces ----------------------------------------------------------------------------------------------------

def test_regions_are_the_union_of_coverage_clipped_to_the_audio():
    cov = {"A": (3.0, 93.0), "B": (3.6, 92.6), "C": (110.0, 190.0)}
    assert cutplan.regions_from_coverage(cov, 0.0, 195.0) == [(3.0, 93.0), (110.0, 190.0)]
    assert cutplan.regions_from_coverage(cov, 0.0, 100.0) == [(3.0, 93.0)]
    assert cutplan.regions_from_coverage({"A": (0.0, 10.0), "B": (9.0, 20.0)}, 0.0, 30.0) == [(0.0, 20.0)]    # overlapping cameras merge
    assert cutplan.complement([(3.0, 93.0), (110.0, 190.0)], 0.0, 195.0) == [(0.0, 3.0), (93.0, 110.0), (190.0, 195.0)]


def test_plan_starts_at_the_regions_start_not_zero():
    u = [{"speaker": "S1", "start": 10.0, "end": 20.0, "text": "a b c d e f"}, {"speaker": "S2", "start": 21.0, "end": 30.0, "text": "a b c d e f"}]
    shots = cutplan.plan(u, {"S1": "A", "S2": "B"}, {"A": (5.0, 100.0), "B": (5.0, 100.0)}, 30.0, timeline_start=5.0)
    assert shots[0]["start"] == 5.0 and shots[-1]["end"] == 30.0


# ---- plan-cuts per session ------------------------------------------------------------------------------------------

def test_plan_cuts_session_covers_only_where_its_cameras_exist(ms_copy, capsys):
    ep = ms_copy("map")
    out = cli(capsys, "plan-cuts", str(ep.root), "--session", "s1")
    c = read_json(ep.artifact("cutlist.s1.json"))
    assert c["session"] == "s1" and out["session"] == "s1"
    (a, b), = c["regions"]
    assert a == pytest.approx(3.0, abs=0.01) and b == pytest.approx(S1_END, abs=0.02)              # union of A1 and B1, drift applied
    assert c["timeline_seconds"] == pytest.approx(b - a, abs=0.01)
    assert [0.0, 3.0] in c["excluded"] and any(x[0] == pytest.approx(b, abs=0.02) for x in c["excluded"])
    assert {s["camera"] for s in c["shots"]} <= {"A1", "B1"}
    assert c["shots"][0]["start_s"] == pytest.approx(3.0, abs=0.01) and c["shots"][0]["o0"] == 0.0
    assert c["shots"][-1]["end_s"] == pytest.approx(b, abs=0.01)
    assert all(a_["end_s"] == pytest.approx(b_["start_s"], abs=0.002) for a_, b_ in zip(c["shots"], c["shots"][1:]))


def test_plan_cuts_without_a_flag_plans_every_session(ms_copy, capsys):
    ep = ms_copy("map")
    out = cli(capsys, "plan-cuts", str(ep.root))
    assert sorted(out["sessions"]) == ["s1", "s2"]
    c2 = read_json(ep.artifact("cutlist.s2.json"))
    assert {s["camera"] for s in c2["shots"]} <= {"A2", "B2"}
    assert c2["regions"][0][0] == pytest.approx(S2_START, abs=0.02)
    assert not ep.artifact("cutlist.json").exists()


def test_single_session_episode_keeps_the_legacy_cutlist(synth_episode):
    cutplan.build_cutlist(synth_episode)
    assert synth_episode.artifact("cutlist.json").exists()
    assert read_json(synth_episode.artifact("cutlist.json"))["session"] is None


def test_unknown_session_is_a_clear_error(ms_copy):
    with pytest.raises(SystemExit, match="session 's9'"):
        cutplan.build_cutlist(ms_copy("map"), session="s9")


# ---- assemblies -----------------------------------------------------------------------------------------------------

def test_assemble_session_is_a_valid_named_edit_without_the_uncovered_regions(ms_copy, capsys):
    ep = ms_copy("map")
    cli(capsys, "plan-cuts", str(ep.root))
    out = cli(capsys, "assemble", str(ep.root), "--session", "s1", "--canvas", "64x36")
    assert out["name"] == "s1" and out["saved"].endswith("assembly.s1.v1.json")
    doc = editdoc.load_latest(ep, "assembly", "s1")
    assert editdoc.validate(doc) == []
    assert doc["session"] == "s1" and [0.0, 3.0] in doc["excluded"]
    assert set(doc["sources"]) == {"A1", "B1", "M"}                              # this session's cameras + the recorder
    assert doc["duration"] == pytest.approx(S1_END - 3.0, abs=0.02)
    d = doc["tracks"]["dialogue"][0]
    assert d["src"] == "M" and d["e0"] == pytest.approx(3.0, abs=0.01)           # episode time = the recorder's timeline
    assert doc["tracks"]["video"][0]["e0"] == pytest.approx(3.0, abs=0.01)
    assert doc["sources"]["A1"]["drift_ppm"] == pytest.approx(80.0, abs=10.0)    # the measured drift rides along
    assert doc["sources"]["M"]["channel_map"][0]["use"] == "right"               # and the recorder's dead channel
    assert not editdoc.list_versions(ep, "assembly")                             # no unnamed edit was created


def test_assemble_defaults_to_all_sessions_on_a_multi_session_episode(ms_copy, capsys):
    ep = ms_copy("map")
    cli(capsys, "plan-cuts", str(ep.root))
    out = cli(capsys, "assemble", str(ep.root), "--canvas", "64x36")
    assert sorted(out) == ["s1", "s2"]
    assert editdoc.list_versions(ep, "assembly", "s1") == [1] and editdoc.list_versions(ep, "assembly", "s2") == [1]


def test_combined_joins_sessions_with_the_gaps_removed(ms_copy, capsys):
    ep = ms_copy("map")
    cli(capsys, "plan-cuts", str(ep.root))
    out = cli(capsys, "assemble", str(ep.root), "--combined", "--canvas", "64x36")
    doc = editdoc.load_latest(ep, "assembly")
    assert editdoc.validate(doc) == [] and doc["sessions"] == ["s1", "s2"]
    s1, s2 = (read_json(ep.artifact(f"cutlist.{s}.json"))["timeline_seconds"] for s in ("s1", "s2"))
    assert doc["duration"] == pytest.approx(s1 + s2, abs=0.01) and out["duration"] == doc["duration"]
    v = doc["tracks"]["video"]
    assert v[0]["e0"] == pytest.approx(3.0, abs=0.01)
    jump = next(x for a, x in zip(v, v[1:]) if x["e0"] > 100)                     # first shot of session 2
    assert jump["o0"] == pytest.approx(s1, abs=0.01) and jump["e0"] == pytest.approx(S2_START, abs=0.02)   # the 18 s gap is gone
    assert {x["src"] for x in v} == {"A1", "B1", "A2", "B2"}
    assert len(doc["tracks"]["dialogue"]) == 2


def test_combined_on_a_single_session_episode_is_refused(synth_episode, capsys):
    with pytest.raises(SystemExit, match="only one"):
        main(["assemble", str(synth_episode.root), "--combined"])


# ---- named edits through cleanup / final / render / handoff -----------------------------------------------------------

def test_cleanup_runs_per_session_and_only_judges_that_sessions_speech(ms_copy, capsys):
    ep = ms_copy("map")
    cli(capsys, "plan-cuts", str(ep.root))
    cli(capsys, "assemble", str(ep.root), "--canvas", "64x36")
    out = cli(capsys, "cleanup", str(ep.root), "--name", "s1")
    assert out["assembly"]["file"].endswith("assembly.s1.v1.json")
    assert editdoc.list_versions(ep, "rough", "s1") == [1] and not editdoc.list_versions(ep, "rough", "s2")
    rep = read_json(ep.artifact("cleanup.s1.json"))
    assert rep["name"] == "s1" and rep["rough"]["file"].endswith("rough.s1.v1.json")
    assert all(d["e0"] < S1_END for d in rep["decisions"])                         # session 2's speech was not judged
    rough = editdoc.load_latest(ep, "rough", "s1")
    assert editdoc.validate(rough) == [] and rough["duration"] <= editdoc.load_latest(ep, "assembly", "s1")["duration"]
    assert rough["session"] == "s1"
    with pytest.raises(SystemExit, match="use --name"):
        main(["cleanup", str(ep.root)])


def test_final_and_render_and_handoff_accept_a_session_name(ms_copy, capsys, tmp_path):
    ep = ms_copy("map")
    cli(capsys, "plan-cuts", str(ep.root))
    cli(capsys, "assemble", str(ep.root), "--canvas", "64x36")
    cli(capsys, "cleanup", str(ep.root), "--name", "s2")
    rep = final.build_final(ep, name="s2", render=False)
    assert rep["name"] == "s2" and rep["edit"].startswith("final.s2.v2")   # v1 = the doc, v2 = with captions
    assert editdoc.list_versions(ep, "final", "s2") == [1, 2] and not editdoc.list_versions(ep, "final")
    assert (ep.captions / "final.s2.srt").exists()
    # render: the named edit lands in the right file and has the picture of THIS session's cameras
    out = cli(capsys, "render", str(ep.root), "--name", "s2", "--proxy", "--no-captions", "--overwrite")
    assert out["edit"].endswith("final.s2.v2.json") and "final.s2.v2" in out["out"]
    assert out["duration"] == pytest.approx(editdoc.load_latest(ep, "final", "s2")["duration"], abs=0.1)
    plan = handoff.plan(ep, editdoc.load_latest(ep, "final", "s2"), name="x")
    assert plan["timeline_frames"] == round(out["duration"] * 30) or abs(plan["timeline_frames"] - round(out["duration"] * 30)) <= 2
    # the camera files used by the handoff belong to session 2 only
    assert all("A2" in v["file"] or "B2" in v["file"] for v in plan["video"])


def test_state_reports_per_session_stages_and_resume_lists_sessions(ms_copy, capsys):
    ep = ms_copy("map")
    st = state.status(ep)
    assert sorted(st["sessions"]) == ["s1", "s2"] and not any(st["sessions"]["s1"].values())
    cli(capsys, "plan-cuts", str(ep.root))
    cli(capsys, "assemble", str(ep.root), "--session", "s1", "--canvas", "64x36")
    st = state.status(ep)
    assert st["sessions"]["s1"] == {"cutlist": True, "assembly": True, "rough": False, "final": False}
    assert st["sessions"]["s2"]["assembly"] is False
    row = {r["stage"]: r for r in st["stages"]}
    assert row["cutplan"]["done"] and not row["assembly"]["done"]                  # assembly is done only when EVERY session has one
    assert "assembly.s1" in st["edits"]
    text = state.briefing(ep)
    assert "Sessions" in text and "s1:" in text and "s2:" in text
    cli(capsys, "assemble", str(ep.root), "--session", "s2", "--canvas", "64x36")
    row = {r["stage"]: r for r in state.status(ep)["stages"]}
    assert row["assembly"]["done"] and row["rough"]["command"].endswith("--name s1")


def test_a_hole_in_a_sessions_coverage_is_excluded_not_an_error_and_renders_the_right_pictures(ms_copy, capsys, tmp_path):
    """Camera A1 stops at 43 s and B1 only starts at 60 s: 43-60 s has no picture. The assembly skips it, validates, and the
    rendered frames are the cameras the shots name (A1 is blue, B1 is red)."""
    import subprocess
    import numpy as np
    ep = ms_copy("map")
    media = json.loads(ep.artifact("media.json").read_text())
    next(f for f in media["files"] if f["file"].endswith("camA1.mov"))["duration"] = 40.0
    ep.artifact("media.json").write_text(json.dumps(media))
    sync = json.loads(ep.artifact("sync.json").read_text())
    sync["files"]["raw/camB1.mov"]["start_offset"] = 60.0
    ep.artifact("sync.json").write_text(json.dumps(sync))
    cli(capsys, "plan-cuts", str(ep.root), "--session", "s1")
    c = read_json(ep.artifact("cutlist.s1.json"))
    assert len(c["regions"]) == 2 and c["regions"][0][1] == pytest.approx(43.0, abs=0.01) and c["regions"][1][0] == pytest.approx(60.0, abs=0.01)
    assert [43.003, 60.0] == pytest.approx(c["excluded"][1], abs=0.01)
    cli(capsys, "assemble", str(ep.root), "--session", "s1", "--canvas", "64x36")
    doc = editdoc.load_latest(ep, "assembly", "s1")
    assert editdoc.validate(doc) == [] and len(doc["tracks"]["dialogue"]) == 2
    assert doc["duration"] == pytest.approx(c["timeline_seconds"], abs=0.01)
    tm = editdoc.TimeMap([tuple(r) for r in c["regions"]])
    assert tm.to_output(50.0) is None                                              # 50 s of episode time is not in the program
    out = tmp_path / "hole.mp4"
    comp.render(ep, doc, out, preset="proxy", burn_captions=False)
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(out), "-vf", "scale=8:4", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True).stdout
    fr = np.frombuffer(raw, np.uint8).reshape(-1, 4, 8, 3).mean(axis=(1, 2))
    for seg in doc["tracks"]["video"]:
        mid = fr[min(int((seg["o0"] + seg["o1"]) / 2 * 30), len(fr) - 1)]
        want_blue = seg["src"] == "A1"
        assert (mid[2] > mid[0]) == want_blue, (seg["id"], seg["src"], mid)
