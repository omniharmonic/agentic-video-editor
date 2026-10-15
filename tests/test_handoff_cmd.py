"""`ave handoff --layers` at the CLI edge: dry runs write nothing, a real run builds the sum WAVs once and names the second timeline."""
import json
import subprocess
from types import SimpleNamespace
from argparse import Namespace

import soundfile as sf

from ave import editdoc
from ave.commands import handoff as cmd_mod
from conftest import needs_ffmpeg

pytestmark = needs_ffmpeg


def _args(ep, **kw):
    base = dict(episode=str(ep.root), stage="assembly", name=None, label=None, no_master=True, project=None, dry_run=False, layers=True)
    return Namespace(**{**base, **kw})


def _snapshot(root):
    return sorted((str(p.relative_to(root)), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file())


def test_layers_dry_run_writes_nothing_under_the_episode(synth_episode, monkeypatch):
    seen = []
    monkeypatch.setattr(cmd_mod, "subprocess", SimpleNamespace(run=lambda c, **k: seen.append(c) or subprocess.CompletedProcess(c, 0)))
    before = _snapshot(synth_episode.root)
    assert cmd_mod.cmd(_args(synth_episode, dry_run=True, no_master=False)) == 0
    assert _snapshot(synth_episode.root) == before            # no plan file, no sum WAV, no rendered master
    plan_path = seen[0][2]
    assert str(synth_episode.root) not in plan_path and "--dry-run" in seen[0]
    plan = json.loads(open(plan_path).read())
    assert plan["name"].endswith(" layers") and plan["layers"]["baked_master"] is not None


def test_layers_run_builds_sum_wavs_once_and_keeps_the_consolidated_files_apart(synth_episode, monkeypatch):
    runs = []
    monkeypatch.setattr(cmd_mod, "subprocess", SimpleNamespace(run=lambda c, **k: runs.append(c) or subprocess.CompletedProcess(c, 0)))
    assert cmd_mod.cmd(_args(synth_episode)) == 0
    plan = json.loads((synth_episode.work / "handoff_layers_plan.json").read_text())
    wavs = plan["layers"]["sum_wavs"]
    assert len(wavs) == 1 and sf.info(wavs[0]).subtype == "PCM_24" and sf.info(wavs[0]).channels == 2
    assert not (synth_episode.work / "handoff_plan.json").exists() and not (synth_episode.work / "handoff.json").exists()
    assert (synth_episode.work / "handoff_layers.json").exists()
    m = (wavs[0], __import__("os").stat(wavs[0]).st_mtime_ns)
    assert cmd_mod.cmd(_args(synth_episode)) == 0              # second run reuses the WAV
    assert __import__("os").stat(wavs[0]).st_mtime_ns == m[1]
    ep_src = editdoc.load_latest(synth_episode, "assembly")["sources"]["A"]["file"]
    (synth_episode.root / ep_src).touch()                      # the source changed: rebuilt
    assert cmd_mod.cmd(_args(synth_episode)) == 0
    assert __import__("os").stat(wavs[0]).st_mtime_ns != m[1]


def test_default_run_is_unchanged(synth_episode, monkeypatch):
    monkeypatch.setattr(cmd_mod, "subprocess", SimpleNamespace(run=lambda c, **k: subprocess.CompletedProcess(c, 0)))
    assert cmd_mod.cmd(_args(synth_episode, layers=False)) == 0
    plan = json.loads((synth_episode.work / "handoff_plan.json").read_text())
    assert "layers" not in plan and not plan["name"].endswith("layers")
    assert (synth_episode.work / "handoff.json").exists()
