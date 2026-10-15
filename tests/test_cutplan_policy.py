"""Cutplan policy from YAML (T12 item 2): data-driven interjection rule, and role: wide cameras in the planner (ROADMAP W8)."""

import dataclasses
import json
import shutil

import pytest

import pacing_synth
from ave import cutplan, editdoc
from ave.cutplan import CutPolicy, plan
from ave.episode import Episode, read_json, write_json
from conftest import needs_ffmpeg

COV = {"A": (0.0, 100.0), "B": (0.0, 100.0)}
MAP = {"S1": "A", "S2": "B"}


def u(spk, a, b, text="word " * 6, margin=None):
    d = {"speaker": spk, "start": a, "end": b, "text": text.strip()}
    if margin is not None:
        d["margin_db"] = margin
    return d


def cams(shots):
    return [s["camera"] for s in shots]


# ---- the YAML is the policy ------------------------------------------------------------------------------------------

def test_default_yaml_matches_the_dataclass_and_has_the_agreed_numbers():
    pol = cutplan.load_policy()
    assert pol == CutPolicy() and pol.reaction_min_s == 0.8 and pol.min_hold_before_s == 6.0 and pol.laugh_min_s == 1.2
    assert {"mm-hm", "yeah", "right", "okay", "uh-huh"} <= set(pol.ignore_words)
    assert pol.legacy_never_cut_to_listener is False
    assert (pol.lead_s, pol.min_shot_s, pol.interjection_max_s, pol.interjection_max_words) == (0.25, 1.5, 1.2, 3)


def test_policy_resolution_order_and_partial_files(tmp_path):
    ep = Episode(tmp_path / "e")
    ep.create()
    home = tmp_path / "home"
    (home / ".config/ave").mkdir(parents=True)
    (home / ".config/ave/cutplan.yaml").write_text("lead_s: 0.4\n")
    pol, src = cutplan.resolve_policy(ep, home=home)
    assert pol.lead_s == 0.4 and pol.min_shot_s == 1.5 and src.endswith(".config/ave/cutplan.yaml")
    (ep.root / "policy").mkdir()
    (ep.root / "policy/cutplan.yaml").write_text("lead_s: 0.5\ninterjection: {min_hold_before_s: 3}\n")
    pol, src = cutplan.resolve_policy(ep, home=home)
    assert pol.lead_s == 0.5 and pol.min_hold_before_s == 3 and pol.reaction_min_s == 0.8 and src.endswith("policy/cutplan.yaml")
    explicit = tmp_path / "mine.yaml"
    explicit.write_text("lead_s: 0.6\n")
    pol, src = cutplan.resolve_policy(ep, explicit, home=home)
    assert pol.lead_s == 0.6 and src == str(explicit.resolve())
    assert cutplan.resolve_policy(Episode(tmp_path / "other"), home=tmp_path / "nohome")[0] == CutPolicy()
    with pytest.raises(FileNotFoundError):
        cutplan.resolve_policy(ep, tmp_path / "missing.yaml")


def test_invalid_policy_names_the_key(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("interjection: {reaction_min_s: -1}\n")
    with pytest.raises(ValueError, match="reaction_min_s"):
        cutplan.load_policy(bad)
    bad.write_text("interjection: {nonsense: 1}\n")
    with pytest.raises(ValueError, match="nonsense"):
        cutplan.load_policy(bad)


# ---- should_cut_to_listener -----------------------------------------------------------------------------------------------

def test_a_long_laugh_on_the_listeners_own_channel_cuts_to_them():
    shots = plan([u("S1", 0, 20), u("S2", 10.0, 12.0, "ha ha ha", margin=18.0), u("S1", 20.5, 30)], MAP, COV, 30.0)
    assert cams(shots) == ["A", "B", "A"] and shots[1]["reason"] == "reaction"
    assert shots[1]["start"] == pytest.approx(9.75) and shots[1]["end"] == pytest.approx(20.25)      # returns at the next speaker change


def test_mm_hm_and_yeah_stay_on_the_speaker_unless_laughter_length():
    base = [u("S1", 0, 20)]
    for txt, dur, expect in (("yeah", 0.5, ["A"]), ("Mm-hmm.", 1.0, ["A"]), ("mm-hm", 1.0, ["A"]), ("Uh-huh", 1.1, ["A"]),
                             ("right", 0.79, ["A"]), ("Yeah", 1.5, ["A", "B", "A"]), ("okay okay", 1.3, ["A", "B", "A"])):
        shots = plan(base + [u("S2", 10.0, 10.0 + dur, txt, margin=20.0), u("S1", 20.5, 30)], MAP, COV, 30.0)
        assert cams(shots) == expect, (txt, dur)


def test_a_real_reaction_that_is_not_a_backchannel_cuts_when_long_enough():
    assert cams(plan([u("S1", 0, 20), u("S2", 10.0, 11.0, "oh wow really", margin=20.0), u("S1", 20.5, 30)], MAP, COV, 30.0)) == ["A", "B", "A"]
    assert cams(plan([u("S1", 0, 20), u("S2", 10.0, 10.7, "oh wow", margin=20.0), u("S1", 20.5, 30)], MAP, COV, 30.0)) == ["A"]      # shorter than reaction_min_s


def test_too_soon_after_the_last_cut_is_suppressed():
    shots = plan([u("S1", 0, 20), u("S2", 3.0, 5.0, "ha ha ha", margin=20.0), u("S1", 20.5, 30)], MAP, COV, 30.0)      # held 3 s < 6 s
    assert cams(shots) == ["A"]
    p = CutPolicy(min_hold_before_s=2.0)
    assert cams(plan([u("S1", 0, 20), u("S2", 3.0, 5.0, "ha ha ha", margin=20.0), u("S1", 20.5, 30)], MAP, COV, 30.0, p)) == ["A", "B", "A"]


def test_the_interjection_must_be_clearly_on_the_listeners_channel():
    low = plan([u("S1", 0, 20), u("S2", 10.0, 12.0, "ha ha ha", margin=3.0), u("S1", 20.5, 30)], MAP, COV, 30.0)
    assert cams(low) == ["A"]                                                    # 3 dB: bleed, not a clean second mic
    unknown = plan([u("S1", 0, 20), u("S2", 10.0, 12.0, "ha ha ha"), u("S1", 20.5, 30)], MAP, COV, 30.0)
    assert cams(unknown) == ["A", "B", "A"]                                      # no channel data (a one-mic recording): the other rules decide
    strict = CutPolicy(interjection_require_margin=True)
    assert cams(plan([u("S1", 0, 20), u("S2", 10.0, 12.0, "ha ha ha"), u("S1", 20.5, 30)], MAP, COV, 30.0, strict)) == ["A"]


def test_the_legacy_knob_restores_never_cut_away():
    p = CutPolicy(legacy_never_cut_to_listener=True)
    assert cams(plan([u("S1", 0, 20), u("S2", 10.0, 12.0, "ha ha ha", margin=20.0), u("S1", 20.5, 30)], MAP, COV, 30.0, p)) == ["A"]


# ---- wide cameras in the planner ------------------------------------------------------------------------------------------

WCOV = {"A": (0.0, 100.0), "B": (0.0, 100.0), "W": (0.0, 100.0)}


def test_a_wide_camera_opens_each_region_for_a_moment():
    shots = plan([u("S1", 0, 10), u("S2", 10.5, 20)], MAP, WCOV, 20.0, wide="W")
    assert cams(shots) == ["W", "A", "B"] and shots[0]["reason"] == "establishing wide"
    assert shots[0]["start"] == 0.0 and shots[0]["end"] == pytest.approx(CutPolicy().wide_opening_s)
    assert all(a["end"] == b["start"] for a, b in zip(shots, shots[1:])) and shots[-1]["end"] == 20.0
    # no wide, or wide disabled, or no room: the plan is exactly the old one
    assert cams(plan([u("S1", 0, 10), u("S2", 10.5, 20)], MAP, COV, 20.0)) == ["A", "B"]
    assert cams(plan([u("S1", 0, 10), u("S2", 10.5, 20)], MAP, WCOV, 20.0, CutPolicy(wide_enabled=False), wide="W")) == ["A", "B"]
    assert cams(plan([u("S1", 0, 3.5), u("S2", 4.0, 20)], MAP, WCOV, 20.0, wide="W"))[0] == "W"


def test_crosstalk_goes_to_the_wide():
    utts = [u("S1", 0, 20), u("S2", 8.0, 11.0, "no no no you cannot be serious", margin=20.0), u("S1", 20.5, 30)]
    shots = plan(utts, MAP, WCOV, 30.0, CutPolicy(wide_opening_s=0), wide="W")
    w = [s for s in shots if s["camera"] == "W"]
    # the overlap is 8-11 s; B's shot (from 7.75) would be left with a 0.25 s sliver before it, so the wide takes that too
    assert len(w) == 1 and w[0]["reason"] == "crosstalk wide" and w[0]["start"] == pytest.approx(7.75) and w[0]["end"] == pytest.approx(11.0)
    assert cams(shots) == ["A", "W", "B", "A"]
    # overlap shorter than wide_crosstalk_min_s stays on the speaker
    short = plan([u("S1", 0, 20), u("S2", 8.0, 9.0, "no no", margin=20.0), u("S1", 20.5, 30)], MAP, WCOV, 30.0, CutPolicy(wide_opening_s=0), wide="W")
    assert "W" not in cams(short)


def test_wide_that_is_not_recording_is_never_used_and_can_be_the_fallback():
    cov = {"A": (0.0, 100.0), "B": (0.0, 100.0), "W": (50.0, 100.0)}
    assert "W" not in cams(plan([u("S1", 0, 10), u("S2", 10.5, 20)], MAP, cov, 20.0, wide="W"))
    cov = {"A": (0.0, 5.0), "B": (0.0, 5.0), "W": (0.0, 100.0)}                 # both speaker cameras stopped: the wide still has a picture
    shots = plan([u("S1", 0, 10), u("S2", 10.5, 20)], MAP, cov, 20.0, CutPolicy(wide_opening_s=0), wide="W")
    assert cams(shots) == ["W"] and "fallback" in shots[0]["reason"]


# ---- through build_cutlist on rendered media -----------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def wide_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("cpwide") / "episode"
    pacing_synth.build_episode(root, wide=True)
    return root


@pytest.fixture
def wide_ep(wide_root, tmp_path):
    dst = tmp_path / "episode"
    shutil.copytree(wide_root, dst)
    return Episode(dst)


@needs_ffmpeg
def test_cutlist_uses_the_wide_camera_and_carries_the_policy(wide_ep):
    cut = cutplan.build_cutlist(wide_ep)
    assert cut["cameras"] == ["A", "B", "W"]
    assert cut["shots"][0]["camera"] == "W" and cut["shots"][0]["reason"] == "establishing wide"
    assert [s["camera"] for s in cut["shots"]] == ["W", "A", "B"]
    assert cut["policy"]["reaction_min_s"] == 0.8 and cut["policy_source"].endswith("cutplan.default.yaml")
    from ave import assemble
    doc = assemble.build(wide_ep, (320, 180))
    assert editdoc.validate(doc) == [] and doc["sources"]["W"]["role"] == "wide"
    assert [s["src"] for s in doc["tracks"]["video"]] == ["W", "A", "B"]


@needs_ffmpeg
def test_cutlist_cuts_to_a_laughing_listener_using_the_transcript_margins(wide_ep, tmp_path):
    t = read_json(wide_ep.artifact("transcript.json"))
    laugh = [{"w": "Ha", "start": 20.0, "end": 20.7, "conf": 0.9, "speaker": "S2", "speaker_margin_db": 18.0},
             {"w": "ha", "start": 20.8, "end": 21.5, "conf": 0.9, "speaker": "S2", "speaker_margin_db": 18.0}]
    i = len(t["words"])
    t["words"] += laugh
    t["utterances"].append({"speaker": "S2", "start": 20.0, "end": 21.5, "first_word": i, "last_word": i + 1, "text": "Ha ha", "confidence": 1.0})
    t["utterances"].sort(key=lambda x: x["start"])
    write_json(wide_ep.artifact("transcript.json"), t)
    pol = tmp_path / "p.yaml"
    pol.write_text("wide: {enabled: false}\n")
    cut = cutplan.build_cutlist(wide_ep, policy_path=pol)
    reactions = [s for s in cut["shots"] if s["reason"] == "reaction"]
    assert len(reactions) == 1 and reactions[0]["camera"] == "B" and reactions[0]["start_s"] == pytest.approx(19.75)
    # the same laugh heard at only 2 dB over the other mic is bleed: no cut
    for w in t["words"][-2:]:
        w["speaker_margin_db"] = 2.0
    write_json(wide_ep.artifact("transcript.json"), t)
    cut2 = cutplan.build_cutlist(wide_ep, policy_path=pol)
    assert not [s for s in cut2["shots"] if s["reason"] == "reaction"]


@needs_ffmpeg
def test_plan_cuts_cli_takes_a_policy(wide_ep, tmp_path, capsys):
    from ave import cli
    pol = tmp_path / "p.yaml"
    pol.write_text("wide: {enabled: false}\nlead_s: 0.4\n")
    assert cli.main(["plan-cuts", str(wide_ep.root), "--policy", str(pol)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["policy_source"] == str(pol.resolve()) and out["stats"]["wide_shots"] == 0
    cut = read_json(wide_ep.artifact("cutlist.json"))
    assert cut["policy"]["lead_s"] == 0.4 and cut["cameras"] == ["A", "B", "W"]
    assert cli.main(["plan-cuts", str(wide_ep.root)]) == 0
    assert json.loads(capsys.readouterr().out)["stats"]["wide_shots"] == 1
    with pytest.raises(SystemExit, match="policy file not found"):
        cli.main(["plan-cuts", str(wide_ep.root), "--policy", str(tmp_path / "nope.yaml")])
