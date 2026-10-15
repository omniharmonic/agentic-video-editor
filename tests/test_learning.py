"""The decision ledger (`ave.learning`) and the single decide path that writes clips.json status AND the ledger."""

import json

import pytest
import yaml

from ave import cli, clips, learning
from ave.episode import read_json

from recall_synth import build_marathon

SPEC_KEYS = {"v", "ts", "episode", "clip_id", "session", "edit", "verdict", "reasons", "note", "by", "source", "features"}
FEATURE_KEYS = {"score", "rubric", "duration_s", "tags", "speaker_balance", "hook_pos_s", "ends_on_sentence", "text_sha"}


@pytest.fixture
def ep(tmp_path):
    ep, _ = build_marathon(tmp_path / "ep", blocks=5, minutes=15)
    clips.prepare(ep, min_s=20, max_s=60)
    clips.propose(ep)
    return ep


def ids(ep):
    return [c["id"] for c in clips.load_clips(ep)["clips"]]


def status(ep):
    return {c["id"]: (c["status"], bool(c.get("maybe"))) for c in clips.load_clips(ep)["clips"]}


def lines(ep):
    return [json.loads(x) for x in learning.ledger_path(ep).read_text().splitlines()]


def test_ledger_lives_where_the_contract_says(ep):
    assert learning.ledger_path(ep) == ep.work / "learning" / "clip_decisions.jsonl"


def test_a_record_has_exactly_the_contract_fields(ep):
    c = ids(ep)[0]
    rec = learning.record_decision(ep, c, "keep", ["other"], "nice", by="maren", source="ui")
    assert set(rec) == SPEC_KEYS and rec["v"] == 1 and rec["verdict"] == "keep" and rec["source"] == "ui" and rec["by"] == "maren"
    assert rec["episode"] == ep.root.name and rec["clip_id"] == c and rec["session"] is None and rec["edit"] == "assembly.v1"
    assert rec["reasons"] == ["other"] and rec["note"] == "nice" and rec["ts"].endswith("Z") and len(rec["ts"]) == 20
    assert set(rec["features"]) == FEATURE_KEYS
    f = rec["features"]
    assert isinstance(f["score"], float) and set(f["rubric"]) == set(clips.load_clips(ep)["rubric"]["weights"])
    assert all(0 <= v <= 1 for v in f["rubric"].values()) and f["tags"] and len(f["text_sha"]) == 16
    assert lines(ep) == [rec]                                                                  # one JSON object per line


def test_latest_decision_wins_and_history_is_kept(ep):
    c = ids(ep)[0]
    learning.record_decision(ep, c, "keep", by="a")
    learning.record_decision(ep, c, "skip", ["weak_ending", "hook_late"], by="a")
    learning.record_decision(ep, ids(ep)[1], "maybe", by="a")
    learning.record_decision(ep, c, "maybe", note="second thoughts", by="a")
    latest = learning.latest_decisions(ep)
    assert set(latest) == {c, ids(ep)[1]} and latest[c]["verdict"] == "maybe" and latest[c]["note"] == "second thoughts"
    hist = [r["verdict"] for r in learning.iter_decisions(ep) if r["clip_id"] == c]
    assert hist == ["keep", "skip", "maybe"]


def test_a_damaged_line_never_breaks_reading(ep):
    c = ids(ep)[0]
    learning.record_decision(ep, c, "keep", by="a")
    with learning.ledger_path(ep).open("a") as f:
        f.write("{not json\n\n[1,2]\n" + json.dumps({"clip_id": "x", "verdict": "nonsense"}) + "\n")
    learning.record_decision(ep, ids(ep)[1], "skip", by="a")
    assert [r["clip_id"] for r in learning.iter_decisions(ep)] == [c, ids(ep)[1]]


def test_no_ledger_reads_as_empty(tmp_path):
    ep, _ = build_marathon(tmp_path / "e", blocks=2, minutes=6)
    assert list(learning.iter_decisions(ep)) == [] and learning.latest_decisions(ep) == {}


def test_bad_verdict_source_or_reason_is_refused_and_nothing_is_written(ep):
    c = ids(ep)[0]
    with pytest.raises(ValueError, match="verdict"):
        learning.record_decision(ep, c, "approve")
    with pytest.raises(ValueError, match="source"):
        learning.record_decision(ep, c, "keep", source="robot")
    with pytest.raises(ValueError, match="unknown reason.*not_a_reason.*weak_ending"):
        learning.record_decision(ep, c, "skip", ["not_a_reason"])
    assert not learning.ledger_path(ep).exists()


def test_the_reason_vocabulary_ships_and_is_extensible(ep):
    base = learning.load_reasons(ep)
    assert {"weak_ending", "hook_late", "off_topic", "too_long", "too_short", "needs_context", "great_but_long", "other"} <= set(base)
    (ep.root / "policy").mkdir(exist_ok=True)
    (ep.root / "policy" / "clip_reasons.yaml").write_text(yaml.safe_dump({"reasons": [{"id": "boring_intro", "label": "Boring intro", "feature": "hook"}]}))
    assert "boring_intro" in learning.load_reasons(ep) and "weak_ending" in learning.load_reasons(ep)
    learning.record_decision(ep, ids(ep)[0], "skip", ["boring_intro"], by="a")


# ---- clips.decide: the single path ---------------------------------------------------------------------------

def test_verdicts_map_to_status_per_the_contract_and_are_logged(ep):
    a, b, c, d = ids(ep)[:4]
    r = clips.decide(ep, [{"id": a, "verdict": "keep", "reasons": ["other"], "note": "love it"},
                          {"id": b, "verdict": "maybe", "reasons": ["too_long"]},
                          {"id": c, "verdict": "skip", "reasons": ["weak_ending", "off_topic"], "note": "meh"}], by="maren")
    assert r["errors"] == [] and r["approved"] == [a]
    st = status(ep)
    assert st[a] == ("approved", False) and st[b] == ("proposed", True) and st[c] == ("rejected", False) and st[d] == ("proposed", False)
    got = {x["clip_id"]: x for x in lines(ep)}
    assert got[a]["verdict"] == "keep" and got[b]["verdict"] == "maybe" and got[c]["verdict"] == "skip"
    assert got[c]["reasons"] == ["weak_ending", "off_topic"] and got[c]["note"] == "meh" and got[c]["by"] == "maren" and got[c]["source"] == "cli"
    assert set(got[a]["features"]) == FEATURE_KEYS and got[a]["features"]["score"] == next(x["total"] for x in clips.load_clips(ep)["clips"] if x["id"] == a)


def test_old_approve_and_reject_still_work_and_also_reach_the_ledger(ep):
    a, b = ids(ep)[:2]
    r = clips.decide(ep, [{"id": a, "decision": "approve"}, {"id": b, "decision": "reject", "note": "weak ending"}])
    assert r["errors"] == [] and status(ep)[a][0] == "approved" and status(ep)[b][0] == "rejected"
    latest = learning.latest_decisions(ep)
    assert latest[a]["verdict"] == "keep" and latest[b]["verdict"] == "skip" and latest[b]["note"] == "weak ending"
    assert len(ep.artifact("clips_decisions.jsonl").read_text().splitlines()) == 2             # the older log is still written


def test_changing_your_mind_updates_status_and_adds_a_ledger_line(ep):
    a = ids(ep)[0]
    clips.decide(ep, [{"id": a, "verdict": "keep"}])
    clips.decide(ep, [{"id": a, "verdict": "maybe"}])
    assert status(ep)[a] == ("proposed", True)
    clips.decide(ep, [{"id": a, "verdict": "skip", "reasons": ["off_topic"]}])
    assert status(ep)[a] == ("rejected", False)
    assert [r["verdict"] for r in learning.iter_decisions(ep)] == ["keep", "maybe", "skip"] and learning.latest_decisions(ep)[a]["verdict"] == "skip"
    clips.decide(ep, [{"id": a, "decision": "pending"}])                                      # back to undecided clears maybe/status
    assert status(ep)[a] == ("proposed", False)


def test_a_nudge_alone_is_not_a_verdict_but_a_nudged_keep_logs_the_new_features(ep):
    a = ids(ep)[0]
    before = next(c for c in clips.load_clips(ep)["clips"] if c["id"] == a)
    r = clips.decide(ep, [{"id": a, "decision": "nudge", "nudge": {"end": -1}}])
    assert r["errors"] == [] and not learning.ledger_path(ep).exists()
    clips.decide(ep, [{"id": a, "verdict": "keep"}])
    f = learning.latest_decisions(ep)[a]["features"]
    now = next(c for c in clips.load_clips(ep)["clips"] if c["id"] == a)
    assert now["duration"] < before["duration"] and f["duration_s"] == now["duration"] and f["text_sha"] == now["text_sha"] != before["text_sha"]


def test_bad_verdicts_reasons_and_ids_are_errors_and_change_nothing(ep):
    a = ids(ep)[0]
    r = clips.decide(ep, [{"id": a, "verdict": "love"}, {"id": a, "verdict": "skip", "reasons": ["bogus"]}, {"id": "nope", "verdict": "keep"},
                          {"id": a, "decision": "maybe"}])
    assert len(r["errors"]) == 4 and "must be" in r["errors"][0]["error"] and "reasons must be a list" in r["errors"][1]["error"]
    assert status(ep)[a] == ("proposed", False) and not learning.ledger_path(ep).exists()


def test_maybe_survives_a_propose_rerun(ep):
    a = ids(ep)[0]
    clips.decide(ep, [{"id": a, "verdict": "maybe"}])
    clips.propose(ep)
    assert status(ep)[a] == ("proposed", True)


# ---- CLI -----------------------------------------------------------------------------------------------------

def run(capsys, *argv):
    rc = cli.main([str(a) for a in argv])
    out = capsys.readouterr().out
    return rc, (json.loads(out) if out.strip().startswith(("{", "[")) else out)


def test_cli_decide_one_clip_with_verdict_and_repeated_reasons(ep, capsys):
    a = ids(ep)[0]
    rc, out = run(capsys, "clips", "decide", ep.root, a, "--verdict", "skip", "--reason", "weak_ending", "--reason", "hook_late", "--note", "sags", "--by", "ben")
    assert rc == 0 and out["applied"][0]["status"] == "rejected"
    rec = learning.latest_decisions(ep)[a]
    assert rec["reasons"] == ["weak_ending", "hook_late"] and rec["note"] == "sags" and rec["by"] == "ben" and rec["source"] == "cli"
    assert status(ep)[a][0] == "rejected"


def test_cli_decide_still_takes_a_decisions_file(ep, capsys, tmp_path):
    a, b = ids(ep)[:2]
    f = tmp_path / "d.json"
    f.write_text(json.dumps([{"id": a, "decision": "approve"}, {"id": b, "verdict": "keep", "reasons": ["other"]}]))
    rc, out = run(capsys, "clips", "decide", ep.root, f)
    assert rc == 0 and sorted(out["approved"]) == sorted([a, b])
    assert len(list(learning.iter_decisions(ep))) == 2


def test_cli_decide_argument_errors(ep, capsys):
    with pytest.raises(SystemExit, match="decisions file, or a clip id"):
        cli.main(["clips", "decide", str(ep.root)])
    with pytest.raises(SystemExit, match="--verdict"):
        cli.main(["clips", "decide", str(ep.root), ids(ep)[0], "--reason", "other"])
    rc = cli.main(["clips", "decide", str(ep.root), ids(ep)[0], "--verdict", "skip", "--reason", "bogus"])
    assert rc == 2 and not learning.ledger_path(ep).exists()


def test_import_decisions_ingests_a_page_export(ep, capsys, tmp_path):
    a, b, c = ids(ep)[:3]
    f = tmp_path / "clip-decisions.json"
    f.write_text(json.dumps({"kind": "ave-clip-decisions", "v": 1, "episode": ep.root.name, "session": None, "fp": "x", "by": "pat",
                             "decisions": [{"clip_id": a, "verdict": "keep", "reasons": [], "note": "yes"},
                                           {"clip_id": b, "verdict": "skip", "reasons": ["too_short"]},
                                           {"clip_id": c, "decision": "nudge", "nudge": {"end": -1}}]}))
    rc, out = run(capsys, "clips", "import-decisions", ep.root, f)
    assert rc == 0 and len(out["applied"]) == 3
    latest = learning.latest_decisions(ep)
    assert set(latest) == {a, b} and latest[a]["by"] == "pat" and latest[a]["source"] == "cli" and latest[b]["reasons"] == ["too_short"]
    assert status(ep)[a][0] == "approved" and status(ep)[b][0] == "rejected" and status(ep)[c] == ("proposed", False)
    assert read_json(ep.meta_path)["review"]["pending"]                                       # others still await a decision


def test_import_decisions_refuses_other_json(ep, tmp_path):
    f = tmp_path / "x.json"
    f.write_text(json.dumps([{"id": "c01", "decision": "approve"}]))
    with pytest.raises(SystemExit, match="not a review-page export"):
        cli.main(["clips", "import-decisions", str(ep.root), str(f)])
