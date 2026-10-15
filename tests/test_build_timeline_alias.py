"""W9: `ave build-timeline` is a thin deprecated alias of `ave handoff`. The tests prove the two build the SAME timeline, which is
what justified deleting scripts/resolve_build_timeline.py (the duplicate Resolve code path)."""
import json
import subprocess
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from ave import cli, editdoc, handoff
from ave.commands import handoff as cmd_mod
from ave.episode import read_json
from conftest import needs_ffmpeg

pytestmark = needs_ffmpeg
REPO = Path(__file__).resolve().parents[1]


def test_handoff_plan_places_exactly_what_the_cut_list_and_the_legacy_script_placed(synth_episode):
    """Legacy script: video at (source_start_frame, source_end_frame + 1, record_frame) per shot, audio file at its offset.
    handoff.plan of the assembly edit must give the same numbers for every shot and the same audio timing."""
    cut = read_json(synth_episode.artifact("cutlist.json"))
    doc = editdoc.load_latest(synth_episode, "assembly")
    plan = handoff.plan(synth_episode, doc, name="t")
    assert plan["fps"] == cut["fps"]
    assert len(plan["video"]) == len(cut["shots"])
    for shot, item in zip(cut["shots"], plan["video"]):
        assert Path(item["file"]).resolve() == (synth_episode.root / shot["file"]).resolve()
        assert (item["record_frame"], item["start_frame"], item["end_frame"], item["frames"]) == \
               (shot["record_frame"], shot["source_start_frame"], shot["source_end_frame"] + 1, shot["frames"])
    assert plan["timeline_frames"] == cut["shots"][-1]["record_frame"] + cut["shots"][-1]["frames"]
    # audio: legacy put the whole file at record max(0, off*fps) / source max(0, -off*fps); handoff places cut source segments.
    # Whatever the segmentation, a given timeline frame must read the same source frame (to a frame of rounding).
    off, fps = float(cut["audio"]["start_offset"]), cut["fps"]
    for a in plan["audio"]:
        legacy_src = a["record_frame"] - round(off * fps)
        assert abs(a["start_frame"] - legacy_src) <= 1, (a, legacy_src)


def _run(monkeypatch, args, runs):
    monkeypatch.setattr(cmd_mod, "subprocess", SimpleNamespace(run=lambda c, **k: runs.append(c) or subprocess.CompletedProcess(c, 0)))
    return cli.main(args)


def test_build_timeline_prints_the_deprecation_and_runs_the_handoff(synth_episode, monkeypatch, capsys):
    runs = []
    rc = _run(monkeypatch, ["build-timeline", str(synth_episode.root), "--name", "Ep rough", "--dry-run"], runs)
    assert rc == 0
    assert "DEPRECATED" in capsys.readouterr().err
    assert len(runs) == 1 and runs[0][1].endswith("resolve_handoff.py") and "--dry-run" in runs[0]
    plan = json.loads(Path(runs[0][2]).read_text())
    assert "Ep rough" in plan["name"] and plan["stage"] == "assembly"


def test_alias_and_handoff_write_the_same_plan(synth_episode, monkeypatch):
    runs = []
    assert _run(monkeypatch, ["build-timeline", str(synth_episode.root)], runs) == 0
    legacy = json.loads((synth_episode.work / "handoff_plan.json").read_text())
    assert _run(monkeypatch, ["handoff", str(synth_episode.root), "--stage", "assembly"], runs) == 0
    new = json.loads((synth_episode.work / "handoff_plan.json").read_text())
    assert legacy == new


def test_build_timeline_makes_the_assembly_from_a_cut_list_when_none_exists(synth_episode, monkeypatch, capsys):
    for p in (synth_episode.edits).glob("assembly*.json"):
        p.unlink()
    runs = []
    assert _run(monkeypatch, ["build-timeline", str(synth_episode.root)], runs) == 0
    assert editdoc.list_versions(synth_episode, "assembly") == [1]
    assert "assemble" in capsys.readouterr().err
    assert len(runs) == 1


def test_the_duplicate_code_path_is_gone():
    assert not (REPO / "scripts" / "resolve_build_timeline.py").exists()
    with pytest.raises(SystemExit):
        cli.main(["build-timeline", "--bogus"])
