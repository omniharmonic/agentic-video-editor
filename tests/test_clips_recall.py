"""Recall-first proposing: every clip-worthy section above a low floor, spread over the whole edit, with evidence; alternates per
overlapping cluster; the sanity cap spreads and reports; the shipped config carries the knobs."""

import json
import time

import pytest

from ave import clips
from ave.episode import read_json

from clips_synth import build_long_episode, pick
from recall_synth import build_marathon


@pytest.fixture(scope="module")
def marathon(tmp_path_factory):
    root = tmp_path_factory.mktemp("marathon") / "ep"
    ep, planted = build_marathon(root, blocks=40, minutes=110)
    t0 = time.time()
    info = clips.prepare(ep, min_s=20, max_s=60)
    return ep, planted, info, time.time() - t0


def _cand(ep):
    return read_json(ep.artifact("clips_candidates.json"))


def test_a_110_minute_edit_with_40_spread_candidates_is_not_truncated(marathon):
    """REGRESSION (N17 + the old 5-clip habit): 40 clip-worthy sections spread over 110 minutes must ALL be surfaced, in order across
    the whole edit, with nothing silently dropped."""
    ep, planted, info, _ = marathon
    c = _cand(ep)
    assert c["source"]["duration"] > 100 * 60
    assert info["windows_available"] >= 40 and info["windows"] == info["windows_available"] and info["truncated"] is False
    assert info["cap"] == 80 and info["floor"] == 30
    for t in planted:                      # every planted block is inside some surfaced section
        assert any(w["start"] - 1 <= t + 5 and w["end"] + 1 >= t + 5 for w in c["windows"]), f"planted block at {t:.0f}s was not surfaced"
    starts = sorted(w["start"] for w in c["windows"])
    assert starts[0] < 0.1 * c["source"]["duration"] and starts[-1] > 0.9 * c["source"]["duration"]
    third = c["source"]["duration"] / 3
    assert all(sum(1 for s in starts if k * third <= s < (k + 1) * third) >= 10 for k in range(3))


def test_plain_conversation_stays_below_the_floor(marathon):
    ep, planted, info, _ = marathon
    c = _cand(ep)
    # only the planted blocks (and their clustered neighbours) clear the floor: about one cluster per block, not hundreds
    assert info["windows_available"] <= 60
    assert all(w["score"] >= 30 for w in c["windows"])


def test_every_candidate_carries_the_evidence(marathon):
    ep, _, _, _ = marathon
    for w in _cand(ep)["windows"]:
        assert 30 <= w["score"] <= 100 and 20 <= w["duration"] <= 60
        assert set(w["breakdown"]) == {"weighted", "penalties"} and set(w["scores"]) >= {"hook", "arc", "insight"}
        assert set(w["tags"]) <= {"hook", "story", "claim", "funny", "tangent", "quotable"} and w["tags"]
        assert isinstance(w["ends_on_sentence"], bool) and w["reason"] and "s" in w["reason"]
        assert w["hook_pos_s"] is None or w["hook_pos_s"] >= 0
        assert w["id"].startswith("w")
    top = _cand(ep)["windows"][0]
    assert {"hook", "story", "claim"} <= set(top["tags"]) or "hook" in top["tags"]


def test_scoring_and_ordering_are_best_first_and_the_best_of_a_cluster_wins(marathon):
    ep, _, _, _ = marathon
    ws = _cand(ep)["windows"]
    assert [w["score"] for w in ws] == sorted((w["score"] for w in ws), reverse=True)
    spans = sorted((int(w["start_sid"][1:]), int(w["end_sid"][1:])) for w in ws)
    assert all(b[0] > a[1] for a, b in zip(spans, spans[1:])), "listed sections must not overlap each other"
    with_alts = [w for w in ws if w["alternates"]]
    assert with_alts, "overlapping variants should hang off the best one as alternates"
    for w in with_alts:
        assert len(w["alternates"]) <= 3
        starts, ends = {w["start_sid"]}, {w["end_sid"]}
        for a in w["alternates"]:
            assert a["score"] <= w["score"] and a["score"] >= 30
            assert a["start_sid"] not in starts and a["end_sid"] not in ends          # different in AND out
            starts.add(a["start_sid"])
            ends.add(a["end_sid"])


def test_the_candidate_search_is_fast_enough_for_a_long_session(marathon):
    assert marathon[3] < 120


def test_a_sanity_cap_spreads_the_survivors_and_says_so(tmp_path):
    ep, planted = build_marathon(tmp_path / "ep", blocks=40, minutes=110)
    info = clips.prepare(ep, min_s=20, max_s=60, max_candidates=10)
    c = _cand(ep)
    assert info["windows"] == 10 and info["windows_available"] >= 40 and info["truncated"] is True and info["cap"] == 10
    starts = sorted(w["start"] for w in c["windows"])
    assert starts[0] < 0.15 * c["source"]["duration"] and starts[-1] > 0.85 * c["source"]["duration"]


def test_floor_is_a_parameter(tmp_path):
    ep, _ = build_marathon(tmp_path / "ep", blocks=4, minutes=12)
    hi = clips.prepare(ep, min_s=20, max_s=60, floor=60)["windows"]
    lo = clips.prepare(ep, min_s=20, max_s=60, floor=0)["windows"]
    assert lo > hi and lo >= 4


# ---- propose: recall fill ----------------------------------------------------------------------------------

def test_propose_without_picks_proposes_every_surfaced_section(tmp_path):
    ep, planted = build_marathon(tmp_path / "ep", blocks=6, minutes=18)
    clips.prepare(ep, min_s=20, max_s=60)
    n = len(_cand(ep)["windows"])
    r = clips.propose(ep)
    assert r["agent_picks"] == 0 and r["recall_added"] == n >= 6 and r["rejected"] == []
    data = clips.load_clips(ep)
    assert len(data["clips"]) == n
    for c in data["clips"]:
        assert c["origin"] == "heuristic" and c["status"] == "proposed" and c["tags"] and c["reason"] and c["total"] >= 30
        assert set(c["inputs"]["scores"]) == set(data["rubric"]["weights"])
        assert "text_sha" in c and "speaker_balance" in c and "ends_on_sentence" in c
    assert r["accepted"][0]["origin"] == "heuristic"


def test_agent_picks_come_first_and_recall_fills_around_them(tmp_path):
    ep, planted = build_marathon(tmp_path / "ep", blocks=6, minutes=18)
    clips.prepare(ep, min_s=20, max_s=60)
    S = clips.Sentences(ep, clips.load_stage_doc(ep)[0], clips.load_rubric(ep))
    first = next(s for s in S.items if s["start"] >= planted[0] - 0.5)["i"]
    r = clips.propose(ep, [pick(first, first + 4, "Mine")], recall_fill=True)
    data = clips.load_clips(ep)
    origins = {c["title"]: c["origin"] for c in data["clips"]}
    assert origins["Mine"] == "agent" and r["agent_picks"] == 1 and r["recall_added"] >= 4
    spans = sorted((S.by_id[c["sentence_ids"][0]]["i"], S.by_id[c["sentence_ids"][-1]]["i"]) for c in data["clips"])
    assert all(b[0] > a[1] for a, b in zip(spans, spans[1:])), "no two proposals may share a sentence"


def test_picks_only_keeps_the_old_behaviour(tmp_path):
    ep, planted = build_marathon(tmp_path / "ep", blocks=4, minutes=12)
    clips.prepare(ep, min_s=20, max_s=60)
    S = clips.Sentences(ep, clips.load_stage_doc(ep)[0], clips.load_rubric(ep))
    first = next(s for s in S.items if s["start"] >= planted[0] - 0.5)["i"]
    r = clips.propose(ep, [pick(first, first + 4, "Mine")])
    assert r["recall_added"] == 0 and [c["title"] for c in clips.load_clips(ep)["clips"]] == ["Mine"]


def test_rerunning_propose_keeps_human_decisions_and_maybes(tmp_path):
    ep, _ = build_marathon(tmp_path / "ep", blocks=4, minutes=12)
    clips.prepare(ep, min_s=20, max_s=60)
    clips.propose(ep)
    ids = [c["id"] for c in clips.load_clips(ep)["clips"]]
    clips.decide(ep, [{"id": ids[0], "verdict": "keep"}, {"id": ids[1], "verdict": "maybe"}, {"id": ids[2], "verdict": "skip"}])
    clips.propose(ep)
    after = {c["id"]: c for c in clips.load_clips(ep)["clips"]}
    assert after[ids[0]]["status"] == "approved" and after[ids[1]].get("maybe") and after[ids[2]]["status"] == "rejected"
    assert len({tuple(c["sentence_ids"][:1]) for c in after.values()}) == len(after)


def test_the_floor_rejects_low_agent_picks_with_the_configured_number(tmp_path):
    ep = build_long_episode(tmp_path / "ep")
    clips.prepare(ep, min_s=10, max_s=25)
    low = pick(0, 3, "weak", scores={k: 1 for k in ("hook", "arc", "insight", "emotion", "shareability", "ending", "speech_quality")})
    r = clips.propose(ep, [low])
    assert "below the minimum 30" in r["rejected"][0]["problems"][0]


def test_prepare_pack_is_agent_facing(tmp_path):
    ep, _ = build_marathon(tmp_path / "ep", blocks=4, minutes=12)
    clips.prepare(ep, min_s=20, max_s=60)
    txt = ep.artifact("clips_transcript.txt").read_text()
    assert "Surfaced sections" in txt and "score" in txt and "hook at" in txt
    c = _cand(ep)
    assert c["params"]["floor"] == 30 and "exemplars" in c


def test_cli_prepare_and_propose_defaults(tmp_path, capsys):
    from ave import cli
    ep, _ = build_marathon(tmp_path / "ep", blocks=4, minutes=12)
    assert cli.main(["clips", "prepare", str(ep.root), "--min-s", "20", "--max-s", "60"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["floor"] == 30 and out["cap"] == 80 and out["windows"] >= 4
    assert cli.main(["clips", "propose", str(ep.root)]) == 0
    r = json.loads(capsys.readouterr().out)
    assert r["recall_added"] == out["windows"]
    assert cli.main(["clips", "list", str(ep.root)]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == out["windows"] and rows[0]["tags"] and rows[0]["origin"] == "heuristic"
