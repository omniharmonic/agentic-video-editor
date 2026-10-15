"""Taste profile: exemplars in the agent's pack, the hit-rate report math, suggest (needs data, explainable), apply (human-run only)."""

import json

import pytest
import yaml

from ave import cli, clips, learning, taste
from ave.episode import read_json

from recall_synth import build_marathon

CRIT = ["hook", "arc", "insight", "emotion", "shareability", "ending", "speech_quality"]


def row(cid, verdict, score, tags=(), reasons=(), rubric=None, ep="e1"):
    return {"clip_id": cid, "verdict": verdict, "reasons": list(reasons), "_ep": ep, "_root": "/nowhere",
            "features": {"score": score, "tags": list(tags), "rubric": rubric or {}}}


# ---- report math -------------------------------------------------------------------------------------------

def test_report_math_precision_recall_reasons_and_tags():
    rows = [row("a", "keep", 90, ["story"]), row("b", "skip", 85, ["claim"], ["weak_ending", "hook_late"]), row("c", "keep", 80, ["story", "funny"]),
            row("d", "maybe", 70, ["claim"], ["too_long"]), row("e", "skip", 60, ["tangent"], ["off_topic", "weak_ending"]),
            row("f", "keep", 50, ["funny"]), row("g", "skip", 40, [], ["weak_ending"])]
    rep = taste.build_report(rows, top_n=4)
    assert rep["decided"] == 7 and rep["counts"] == {"keep": 3, "maybe": 1, "skip": 3} and rep["keep_rate"] == pytest.approx(3 / 7, abs=1e-3)
    t = rep["top_n"]                                                      # top 4 by score: a(keep) b(skip) c(keep) d(maybe)
    assert (t["n"], t["keep"], t["maybe"], t["skip"]) == (4, 2, 1, 1) and t["precision"] == 0.5
    assert t["kept_total"] == 3 and t["recall"] == pytest.approx(2 / 3, abs=1e-3)       # 2 of the 3 keeps were in my top 4
    assert [(b["clip_id"], b["rank"]) for b in rep["buried_keeps"]] == [("f", 6)]
    assert [b["clip_id"] for b in rep["high_scored_skips"]] == ["b"]
    assert rep["reasons"]["weak_ending"] == {"keep": 0, "maybe": 0, "skip": 3, "total": 3} and list(rep["reasons"])[0] == "weak_ending"
    tags = rep["tags"]
    assert tags["story"] == {"n": 2, "keep": 2, "maybe": 0, "skip": 0, "keep_rate": 1.0}
    assert tags["claim"]["keep_rate"] == 0.0 and tags["funny"]["keep_rate"] == 1.0 and tags["tangent"]["skip"] == 1


def test_report_with_ties_and_fewer_decisions_than_n_and_no_decisions():
    rows = [row("b", "keep", 50), row("a", "skip", 50)]
    rep = taste.build_report(rows, top_n=10)
    assert rep["top_n"]["n"] == 2 and rep["top_n"]["precision"] == 0.5
    assert [r["clip_id"] for r in rep["high_scored_skips"]] == ["a"]
    empty = taste.build_report([], 10)
    assert empty["decided"] == 0 and empty["keep_rate"] is None and empty["top_n"]["precision"] is None
    assert "No decisions yet" in taste.format_report(empty)


def test_report_text_is_readable():
    txt = taste.format_report(taste.build_report([row("a", "keep", 90, ["story"]), row("b", "skip", 40, ["claim"], ["weak_ending"])], 1))
    assert "Decisions: 2" in txt and "precision 100%" in txt and "weak_ending" in txt and "story" in txt and "Keep rate by tag" in txt


# ---- suggest -----------------------------------------------------------------------------------------------

WEIGHTS = {"hook": 25, "arc": 20, "insight": 15, "emotion": 15, "shareability": 10, "ending": 10, "speech_quality": 5}


def many(n, keep_when_hook_high=True):
    rows = []
    for k in range(n):
        hook = 0.9 if k % 2 == 0 else 0.3
        verdict = ("keep" if hook > 0.5 else "skip") if keep_when_hook_high else ("keep" if k % 4 < 2 else "skip")
        rows.append(row(f"c{k}", verdict, 50 + k, ["story"], rubric={c: (hook if c == "hook" else 0.6 if k % 4 < 2 else 0.5) for c in CRIT}))
    return rows


def test_suggest_says_not_enough_data_below_forty_decisions():
    s = taste.build_suggestion(many(39), WEIGHTS)
    assert s["enough_data"] is False and s["decided"] == 39 and s["needed"] == 40 and "Not enough data" in s["message"] and "weights" not in s
    assert taste.build_suggestion(many(40), WEIGHTS)["enough_data"] is True


def test_suggest_raises_a_weight_whose_high_scores_get_kept_and_explains_it():
    s = taste.build_suggestion(many(48), WEIGHTS)
    h = s["features"]["hook"]
    assert h["keep_rate_high"] == 1.0 and h["keep_rate_low"] == 0.0 and h["lift"] == 1.0
    assert s["changes"]["hook"] == {"from": 25, "to": 31} and s["weights"]["hook"] == 31 and "raise" in h["why"]
    assert s["weights"]["arc"] == 20                                      # no lift, no change
    assert all(abs(v["to"] - v["from"]) <= 0.25 * v["from"] + 1 for v in s["changes"].values())      # never more than ~25% per apply


def test_suggest_lowers_a_weight_that_predicts_skips():
    rows = many(48)
    for r in rows:
        r["verdict"] = "skip" if r["verdict"] == "keep" else "keep"
    s = taste.build_suggestion(rows, WEIGHTS)
    assert s["changes"]["hook"]["to"] < 25 and "lower" in s["features"]["hook"]["why"]


def test_suggest_ignores_noise_and_thin_groups():
    rows = [row(f"c{k}", "keep" if k % 2 == 0 else "skip", 50, rubric={c: (0.8 if k % 2 == 0 else 0.4) if c != "hook" else 0.5 + (k % 5) * 0.1 for c in CRIT})
            for k in range(60)]
    s = taste.build_suggestion(rows, WEIGHTS)
    assert "hook" not in s["changes"]
    thin = taste.build_suggestion([dict(r, features={**r["features"], "rubric": {}}) for r in many(48)], WEIGHTS)
    assert thin["changes"] == {} and thin["features"]["hook"]["status"].startswith("too few")


def test_suggest_notes_the_reason_that_points_at_a_criterion():
    rows = many(48)
    for r in rows:
        if r["verdict"] == "skip":
            r["reasons"] = ["weak_ending"]
    rep = taste.build_report(rows)
    s = taste.build_suggestion(rows, WEIGHTS, reasons=rep["reasons"])
    assert any("weak_ending" in n and "ending" in n for n in s["notes"])


# ---- on a real episode: ledger -> report / exemplars / apply ----------------------------------------------

@pytest.fixture
def ep(tmp_path):
    ep, _ = build_marathon(tmp_path / "ep", blocks=8, minutes=24)
    clips.prepare(ep, min_s=20, max_s=60)
    clips.propose(ep)
    return ep


def decide_all(ep, n=None):
    ids = [c["id"] for c in clips.load_clips(ep)["clips"]]
    items = []
    for k, cid in enumerate(ids[:n]):
        items.append({"id": cid, "verdict": ("keep", "skip", "maybe")[k % 3], "reasons": [] if k % 3 == 0 else ["weak_ending"]})
    clips.decide(ep, items)
    return ids


def test_profile_lives_under_the_taste_dir_env(ep, tmp_path):
    assert taste.taste_path(ep).name == "clips.yaml" and taste.taste_path(ep).parent.name == "taste"
    assert taste.load_taste(ep) == {}


def test_exemplars_reach_the_agent_pack_capped_with_reasons(ep):
    ids = decide_all(ep)
    ex = taste.exemplars(ep, cap=2)
    assert len(ex["keep"]) == 2 and len(ex["skip"]) == 2
    assert ex["skip"][0]["reasons"] == ["weak_ending"] and ex["keep"][0]["quote"] and ex["keep"][0]["score"] is not None and ex["keep"][0]["tags"]
    clips.prepare(ep, min_s=20, max_s=60)                                 # the next prepare shows the agent what the human did
    c = read_json(ep.artifact("clips_candidates.json"))
    assert len(c["exemplars"]["keep"]) == 3 and len(c["exemplars"]["skip"]) == 3          # all there is, under the default cap of 5
    txt = ep.artifact("clips_transcript.txt").read_text()
    assert "What the human kept and skipped before" in txt and "KEEP" in txt and "SKIP" in txt and "weak_ending" in txt


def test_curated_exemplars_come_first(ep, tmp_path):
    p = taste.taste_path(ep)
    p.parent.mkdir(parents=True)
    p.write_text(yaml.safe_dump({"exemplar_cap": 3, "exemplars": {"keep": [{"quote": "a perfect clip", "reasons": []}], "skip": []}}))
    decide_all(ep)
    ex = taste.exemplars(ep)
    assert ex["keep"][0]["quote"] == "a perfect clip" and len(ex["keep"]) == 3


def test_no_decisions_no_exemplars_and_no_noise_in_the_pack(ep):
    assert taste.exemplars(ep) == {"keep": [], "skip": []}
    assert "What the human kept" not in ep.artifact("clips_transcript.txt").read_text()


def test_report_reads_the_ledger_and_the_cli_prints_it(ep, capsys):
    decide_all(ep)
    rep = taste.report(ep, top_n=3)
    n = len(learning.latest_decisions(ep))
    assert rep["decided"] == n and sum(rep["counts"].values()) == n and rep["top_n"]["n"] == 3
    assert cli.main(["learn", "taste", "report", str(ep.root), "--top", "3"]) == 0
    assert "Decisions:" in capsys.readouterr().out
    assert cli.main(["learn", "taste", "report", str(ep.root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["decided"] == n


def test_the_old_learn_command_is_untouched(ep, capsys):
    assert cli.main(["learn", str(ep.root), "--list"]) == 0
    assert "proposed" in json.loads(capsys.readouterr().out)


def test_suggest_cli_without_enough_data(ep, capsys):
    decide_all(ep, n=6)
    assert cli.main(["learn", "taste", "suggest", str(ep.root)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["enough_data"] is False and out["needed"] == 40


def fake_ledger(ep, n):
    """n synthetic decisions (hook-driven) in the ledger, bypassing clips.json."""
    ids = [c["id"] for c in clips.load_clips(ep)["clips"]]
    for k in range(n):
        hook = 0.9 if k % 2 == 0 else 0.3
        learning.record_decision(ep, f"x{k}", "keep" if hook > 0.5 else "skip", by="t", features={"score": 50 + k, "tags": ["story"], "rubric": {c: (hook if c == "hook" else 0.5) for c in CRIT},
                                                                                              "duration_s": 30})
    return ids


def test_apply_writes_weights_only_when_run_and_the_next_prepare_uses_them(ep, capsys):
    fake_ledger(ep, 48)
    assert not taste.taste_path(ep).exists()
    assert cli.main(["learn", "taste", "suggest", str(ep.root)]) == 0
    s = json.loads(capsys.readouterr().out)
    assert s["enough_data"] and s["changes"]["hook"]["to"] > 25
    assert not taste.taste_path(ep).exists(), "suggest must never write"
    assert cli.main(["learn", "taste", "apply", str(ep.root), "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["applied"] is False and not taste.taste_path(ep).exists()
    clips.prepare(ep, min_s=20, max_s=60)
    assert read_json(ep.artifact("clips_candidates.json"))["criteria"]["hook"] == 25         # nothing changed on its own
    assert cli.main(["learn", "taste", "apply", str(ep.root)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["applied"] is True
    prof = yaml.safe_load(taste.taste_path(ep).read_text())
    assert prof["weights"]["hook"] == s["changes"]["hook"]["to"] and prof["weights"]["arc"] == 20 and "weights applied from 48 decisions" in prof["notes"]
    clips.prepare(ep, min_s=20, max_s=60)
    c = read_json(ep.artifact("clips_candidates.json"))
    assert c["criteria"]["hook"] == prof["weights"]["hook"] and c["params"]["taste"]["weights"]["hook"] == prof["weights"]["hook"]
    explicit = ep.root / "mine.yaml"
    explicit.write_text(clips.DEFAULT_RUBRIC.read_text())
    assert clips.load_rubric(ep, explicit)["weights"]["hook"] == 25                           # an explicit --rubric is taken as written


def test_apply_refuses_without_enough_data_and_keeps_curated_keys(ep):
    fake_ledger(ep, 10)
    with pytest.raises(SystemExit, match="Not enough data"):
        taste.apply_suggestion(ep)
    assert not taste.taste_path(ep).exists()
    fake_ledger(ep, 48)
    p = taste.taste_path(ep)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump({"exemplars": {"keep": [{"quote": "keep me"}]}, "notes": "my note"}))
    taste.apply_suggestion(ep)
    prof = yaml.safe_load(p.read_text())
    assert prof["exemplars"]["keep"][0]["quote"] == "keep me" and prof["notes"].startswith("my note") and "weights" in prof


def test_taste_weights_ignore_unknown_criteria_and_junk(ep):
    p = taste.taste_path(ep)
    p.parent.mkdir(parents=True)
    p.write_text(yaml.safe_dump({"weights": {"hook": 40, "nonsense": 9, "arc": -3, "ending": "x"}}))
    w = clips.load_rubric(ep)["weights"]
    assert w["hook"] == 40 and w["arc"] == 20 and "nonsense" not in w and w["ending"] == 10
