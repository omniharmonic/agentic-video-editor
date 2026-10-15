"""Pacing pass through the real command on rendered synthetic media (a 50 s monologue; see tests/pacing_synth.py)."""

import json
import shutil

import pytest

import pacing_synth
from ave import cleanup, cli, editdoc
from ave.episode import Episode, read_json
from conftest import needs_ffmpeg

pytestmark = needs_ffmpeg


@pytest.fixture(scope="module")
def mono_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("mono") / "episode"
    pacing_synth.build_episode(root)
    return root


@pytest.fixture(scope="module")
def wide_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("monowide") / "episode"
    pacing_synth.build_episode(root, wide=True)
    return root


@pytest.fixture
def mono(mono_root, tmp_path):
    dst = tmp_path / "episode"
    shutil.copytree(mono_root, dst)
    return Episode(dst)


@pytest.fixture
def wide(wide_root, tmp_path):
    dst = tmp_path / "episode"
    shutil.copytree(wide_root, dst)
    return Episode(dst)


def holds(doc):
    v = doc["tracks"]["video"]
    out, s = [], 0
    for i in range(1, len(v) + 1):
        if i == len(v) or not cleanup._same_framing(v[i - 1], v[i], 0.08):
            out.append((v[s]["o0"], v[i - 1]["o1"], v[s]["src"]))
            s = i
    return out


def latest(ep, stage="rough"):
    return editdoc.load_latest(ep, stage)


def test_before_and_after_pacing_on_a_long_monologue(mono):
    asm = latest(mono, "assembly")
    assert max(b - a for a, b, _ in holds(asm)) > 45                      # the assembly holds A for ~50 s
    r0 = cleanup.run(mono, pacing=False)
    d0 = latest(mono)
    assert r0["pacing"]["enabled"] is False and r0["pacing"]["inserted"] == 0
    assert max(b - a for a, b, _ in holds(d0)) > 45
    r1 = cleanup.run(mono)
    d1 = latest(mono)
    assert editdoc.validate(d1) == []
    hs = holds(d1)
    assert max(b - a for a, b, _ in hs) <= 14.0 + 1e-6, hs
    p = r1["pacing"]
    assert p["inserted"] >= 4 and p["still_over_limit"] == [] and p["by_mask"] == {"listener": p["inserted"]}
    assert p["before"]["holds_over_limit"] >= 1 and p["after"]["holds_over_limit"] == 0
    assert p["after"]["avg_shot_s"] < p["before"]["avg_shot_s"] and p["after"]["cuts_per_min"] <= 8.0 + 1e-9
    # the dialogue is untouched
    assert d1["tracks"]["dialogue"] == d0["tracks"]["dialogue"]
    assert d1["duration"] == d0["duration"]
    # every insertion is a segment with reason and confidence, on the listener's camera, 2-4 s, between words
    reactions = [s for s in d1["tracks"]["video"] if s["reason"] == "pacing: reaction"]
    assert len(reactions) == p["inserted"] and all(s["src"] in "AB" and 0 < s["confidence"] <= 1 for s in reactions)
    assert all(s["src"] == "B" for s in reactions if s["o0"] < 50)           # S1 holds A: the listener is B (and A for S2's hold)
    words = pacing_synth.words()
    who = {"A": "S1", "B": "S2"}
    for s in reactions:
        assert 2.0 - 1e-6 <= s["o1"] - s["o0"] <= 4.0 + 1e-6
        for t in (s["o0"], s["o1"]):
            assert not any(w["start"] + 1e-6 < t < w["end"] - 1e-6 for w in words)
        assert not any(w["speaker"] == who[s["src"]] and w["start"] < s["o1"] and w["end"] > s["o0"] for w in words)
    # cleanup.json and explain report it
    rep = read_json(mono.artifact("cleanup.json"))
    assert rep["pacing"]["inserted"] == p["inserted"] and rep["pacing"]["decisions"][0]["reason"]
    text = cleanup.explain(rep)
    assert "Pacing:" in text and "pacing: reaction" not in text.split("Pacing:")[0]
    assert not [w for w in rep["warnings"] if w.startswith("long hold")]


def test_rerunning_gives_the_same_rough_cut(mono):
    cleanup.run(mono)
    cleanup.run(mono)
    v1, v2 = editdoc.load(editdoc.path_for(mono, "rough", 1)), editdoc.load(editdoc.path_for(mono, "rough", 2))
    assert v1["tracks"] == v2["tracks"] and v1["duration"] == v2["duration"]


def test_wide_camera_is_preferred_for_reactions(wide):
    r = cleanup.run(wide)
    d = latest(wide)
    reactions = [s for s in d["tracks"]["video"] if s["reason"] == "pacing: reaction"]
    assert reactions and all(s["src"] == "W" for s in reactions)
    assert r["pacing"]["by_mask"] == {"wide": len(reactions)}
    assert editdoc.validate(d) == []


def test_policy_can_turn_pacing_off_or_tighten_it(mono, tmp_path):
    off = tmp_path / "off.yaml"
    off.write_text("pacing: {enabled: false}\n")
    assert cleanup.run(mono, policy_path=off)["pacing"]["inserted"] == 0
    tight = tmp_path / "tight.yaml"
    tight.write_text("pacing: {max_hold_s: 9.0, max_cuts_per_min: 20}\n")
    r = cleanup.run(mono, policy_path=tight)
    assert max(b - a for a, b, _ in holds(latest(mono))) <= 9.0 + 1e-6
    assert r["pacing"]["inserted"] > 3


def test_cli_no_pacing_flag(mono, capsys):
    assert cli.main(["cleanup", str(mono.root), "--no-pacing"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["pacing"]["enabled"] is False and out["pacing"]["inserted"] == 0
    assert max(b - a for a, b, _ in holds(latest(mono))) > 45
    assert cli.main(["cleanup", str(mono.root)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["pacing"]["inserted"] >= 3
    assert cli.main(["cleanup", "explain", str(mono.root)]) == 0
    assert "Pacing:" in capsys.readouterr().out
