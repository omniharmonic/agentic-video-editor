"""Clip preparation, pick validation, decisions, and selects building (logic only; no media needed)."""

import json

import pytest

from ave import clips, editdoc
from ave.episode import read_json, write_json

from clips_synth import SENTENCES, build_long_episode, layout, pick


@pytest.fixture
def ep(tmp_path):
    return build_long_episode(tmp_path / "ep")


@pytest.fixture
def prepared(ep):
    clips.prepare(ep, min_s=10, max_s=25)
    return ep


@pytest.fixture
def literal_rubric(tmp_path):
    """The default rubric with `completeness.expand_picks` off: the agent's sentence ids are used exactly as given. Several tests below
    exercise snapping, penalties and nudges on picks that deliberately start on "Um, so" / "And that is why"; with expansion on (the default
    since the clip-completeness work) such a pick is snapped OUT to a complete thought start instead, which has its own tests in
    test_clip_completeness.py."""
    import yaml
    r = yaml.safe_load(clips.DEFAULT_RUBRIC.read_text())
    r["completeness"] = {"expand_picks": False}
    p = tmp_path / "literal.rubric.yaml"
    p.write_text(yaml.safe_dump(r))
    return p


def rubric(ep):
    return clips.load_rubric(ep)


# ---- sentences ---------------------------------------------------------------------------------------------

def test_sentences_follow_punctuation_and_carry_output_times(ep):
    S = clips.Sentences(ep, editdoc.load_latest(ep, "assembly"), rubric(ep))
    assert len(S.items) == len(SENTENCES)
    assert [s["id"] for s in S.items[:3]] == ["s000", "s001", "s002"]
    for s, (spk, text, a, b) in zip(S.items, layout()):
        assert s["text"] == text and s["speaker"] == spk
        assert s["start"] == pytest.approx(a, abs=2e-3) and s["end"] == pytest.approx(b, abs=2e-3)


def test_sentence_features(ep):
    S = clips.Sentences(ep, editdoc.load_latest(ep, "assembly"), rubric(ep))
    f = {s["id"]: s["features"] for s in S.items}
    assert f["s003"]["leading_fillers"] == 1 and f["s003"]["filler_count"] == 1       # "Um, so ..."
    assert f["s004"]["is_question"] and not f["s003"]["is_question"]
    assert f["s006"]["starts_mid_thought"]                                             # "And that is why"
    assert f["s007"]["refs_prior_context"] and f["s007"]["has_context_phrase"]          # "That is ... earlier"
    assert not f["s009"]["starts_mid_thought"] and not f["s009"]["refs_prior_context"]
    assert all(v["ends_complete"] for v in f.values())


def test_unpunctuated_run_splits_on_speaker_change_and_pause_and_length(ep):
    r = rubric(ep)
    words = [{"w": f"w{k}", "start": k * 0.3, "end": k * 0.3 + 0.25, "e0": k * 0.3, "speaker": "S1"} for k in range(100)]
    words += [{"w": "reply", "start": 31.0, "end": 31.3, "e0": 31.0, "speaker": "S2"}]
    groups = clips._group_sentences(words, r)
    assert all(len(g) <= r["sentences"]["max_words"] for g in groups)
    assert groups[-1][0]["w"] == "reply" and len(groups[-1]) == 1                    # speaker change
    assert sum(len(g) for g in groups) == 101


def test_abbreviation_does_not_end_a_sentence(ep):
    r = rubric(ep)
    toks = ["Dr.", "Smith", "said", "yes."]
    words = [{"w": t, "start": k * 0.3, "end": k * 0.3 + 0.25, "e0": k * 0.3, "speaker": "S1"} for k, t in enumerate(toks)]
    assert len(clips._group_sentences(words, r)) == 1


def test_removed_section_drops_words_and_shifts_output_time(tmp_path):
    lay = layout()
    cut = (lay[8][2], lay[12][2])                        # remove sentences 8..11 exactly
    ep = build_long_episode(tmp_path / "ep2", remove=cut)
    S = clips.Sentences(ep, editdoc.load_latest(ep, "assembly"), rubric(ep))
    assert len(S.items) == len(SENTENCES) - 4
    texts = [s["text"] for s in S.items]
    assert SENTENCES[8][1] not in texts and SENTENCES[12][1] in texts
    after = next(s for s in S.items if s["text"] == SENTENCES[12][1])
    assert after["start"] == pytest.approx(lay[8][2], abs=2e-3)                   # it now starts where the cut was
    assert after["e0"] == pytest.approx(lay[12][2], abs=2e-3)                     # but remembers its episode time


# ---- prepare -----------------------------------------------------------------------------------------------

def test_prepare_writes_candidates_with_aligned_windows(ep):
    info = clips.prepare(ep, min_s=10, max_s=25)
    c = read_json(ep.artifact("clips_candidates.json"))
    assert info["sentences"] == len(SENTENCES) == len(c["sentences"])
    assert c["source"]["stage"] == "assembly" and c["source"]["version"] == 1 and c["source"]["fingerprint"]
    assert "words" not in c["sentences"][0]                                          # keep the agent's file small
    assert c["windows"], "expected candidate windows"
    ids = {s["id"]: s["i"] for s in c["sentences"]}
    spans = []
    for w in c["windows"]:
        assert 10 <= w["duration"] <= 25
        i, j = ids[w["start_sid"]], ids[w["end_sid"]]
        assert w["n_sentences"] == j - i + 1
        for key in ("starts_mid_thought", "refs_prior_context", "question_at_start", "filler_density", "speaker_turns", "ends_complete"):
            assert key in w["features"]
        assert 0 <= w["hint"] <= 1
        spans.append((i, j))
    for n, (a, b) in enumerate(spans):                                               # non-overlapping variants
        for x, y in spans[n + 1:]:
            assert b < x or a > y
    hints = [w["hint"] for w in c["windows"]]
    assert hints == sorted(hints, reverse=True)
    txt = ep.artifact("clips_transcript.txt").read_text()
    assert "s003" in txt and "Um, so the thing" in txt


def test_prepare_penalises_mid_thought_and_context_starts_in_hints(ep):
    clips.prepare(ep, min_s=10, max_s=25)
    c = read_json(ep.artifact("clips_candidates.json"))
    flagged = [w for w in c["windows"] if w["features"]["starts_mid_thought"] or w["features"]["refs_prior_context"]]
    for w in flagged:
        assert w["hint"] < 1.0 and any("mid-thought" in r or "earlier context" in r for r in w["hint_reasons"])


def test_prepare_uses_rough_when_it_exists(tmp_path):
    ep = build_long_episode(tmp_path / "e", stage="rough")
    editdoc.save_new_version(ep, editdoc.load_latest(ep, "rough"))                  # rough v2
    clips.prepare(ep, stage="rough", min_s=10, max_s=25)
    assert read_json(ep.artifact("clips_candidates.json"))["source"]["version"] == 2


def test_prepare_requires_a_transcript(tmp_path):
    from ave.episode import Episode
    e = Episode(tmp_path / "empty")
    e.create()
    with pytest.raises(SystemExit, match="transcribe"):
        clips.prepare(e)


# ---- snapping ----------------------------------------------------------------------------------------------

def test_snap_trims_leading_filler_and_adds_lead_and_tail(ep):
    S = clips.Sentences(ep, editdoc.load_latest(ep, "assembly"), rubric(ep))
    sn = S.snap(3, 5)                                                                # s003 starts with "Um,"
    assert sn["trimmed_leading"] == ["Um,"]
    first_real = S.words[S.items[3]["w0"] + 1]
    assert sn["start"] <= first_real["start"] and first_real["start"] - sn["start"] <= 0.1 + 1e-6
    last = S.words[S.items[5]["w1"] - 1]
    nxt = S.words[S.items[5]["w1"]]
    assert sn["end"] == pytest.approx(min(last["end"] + 0.3, (last["end"] + nxt["start"]) / 2 if nxt["start"] - last["end"] < 0.6 else last["end"] + 0.3), abs=2e-3)
    assert last["end"] < sn["end"] < nxt["start"]                                    # tail never reaches the next word


def test_snap_never_crosses_into_neighbouring_words(ep):
    S = clips.Sentences(ep, editdoc.load_latest(ep, "assembly"), rubric(ep))
    for i in range(1, len(S.items) - 2):
        if S.items[i]["features"]["leading_fillers"]:
            continue
        sn = S.snap(i, i + 1)
        assert S.items[i - 1]["end"] <= sn["start"] <= S.items[i]["start"]
        assert S.items[i + 1]["end"] <= sn["end"] <= S.items[i + 2]["start"]


def test_snap_filler_only_returns_none(ep):
    S = clips.Sentences(ep, editdoc.load_latest(ep, "assembly"), rubric(ep))
    for w in S.words[S.items[3]["w0"]:S.items[3]["w1"]]:
        w["w"] = "um"
    assert S.snap(3, 3) is None


# ---- propose: the hallucination wall -----------------------------------------------------------------------

def propose(ep, picks):
    return clips.propose(ep, picks)


def test_good_pick_is_accepted_snapped_and_scored(prepared, literal_rubric):
    r = clips.propose(prepared, [pick(3, 6, "Systems beat goals")], rubric=literal_rubric)
    assert r["rejected"] == [] and len(r["accepted"]) == 1
    c = read_json(prepared.artifact("clips.json"))["clips"][0]
    assert c["id"] == "c01" and c["slug"] == "systems-beat-goals" and c["status"] == "proposed"
    assert c["sentence_ids"] == ["s003", "s004", "s005", "s006"]
    assert c["trimmed"]["leading"] == ["Um,"] and c["text"].startswith("so the thing")
    # all weights: hook 8*25 + arc 7*20 + insight 7*15 + emotion 6*15 + share 7*10 + ending 7*10 + speech 8*5 = 70.5 ... on 0-100
    base = (8 * 25 + 7 * 20 + 7 * 15 + 6 * 15 + 7 * 10 + 7 * 10 + 8 * 5) / 10
    assert c["total"] == pytest.approx(base - sum(p["points"] for p in c["penalties"]), abs=0.05)
    assert c["duration"] == pytest.approx(c["end"] - c["start"], abs=1e-3)
    assert read_json(prepared.artifact("clips.json"))["rubric"]["weights"]["hook"] == 25   # rubric recorded for audit


def test_perfect_scores_without_penalties_total_100(prepared):
    p = pick(9, 14, "Perfect", scores={k: 10 for k in ("hook", "arc", "insight", "emotion", "shareability", "ending", "speech_quality")})
    propose(prepared, [p])
    c = read_json(prepared.artifact("clips.json"))["clips"][0]
    assert c["penalties"] == [] and c["total"] == 100.0


def test_hallucinated_and_malformed_ids_are_rejected(prepared):
    r = propose(prepared, [pick(3, 6, "ok"), {**pick(3, 6), "sentence_ids": ["s003", "s999"]},
                           {**pick(3, 6), "sentence_ids": ["s003", "s004", "s006"]},         # not contiguous
                           {**pick(3, 6), "sentence_ids": ["s003", "s003", "s004"]},
                           {**pick(3, 6), "sentence_ids": []},
                           {**pick(3, 6), "sentence_ids": "s003"}])
    assert len(r["accepted"]) == 1 and len(r["rejected"]) == 5
    msgs = [" ".join(x["problems"]) for x in r["rejected"]]
    assert "unknown sentence ids: s999" in msgs[0]
    assert "not contiguous" in msgs[1] and "s005" in msgs[1]
    assert "duplicates" in msgs[2]
    assert "non-empty list" in msgs[3] and "non-empty list" in msgs[4]


def test_overlapping_picks_are_rejected_not_silently_merged(prepared):
    r = propose(prepared, [pick(3, 6, "first"), pick(5, 8, "second"), pick(7, 10, "third")])
    assert [a["title"] for a in r["accepted"]] == ["first", "third"]
    assert "overlaps c01" in r["rejected"][0]["problems"][0]


def test_length_limits(prepared):
    r = propose(prepared, [pick(0, 0, "tiny"), pick(0, 28, "huge")])
    assert not r["accepted"]
    assert "too short" in " ".join(r["rejected"][0]["problems"]) and "too long" in " ".join(r["rejected"][1]["problems"])


def test_scores_must_be_complete_known_and_in_range(prepared):
    bad = pick(3, 6)
    del bad["scores"]["hook"]
    bad["scores"]["charisma"] = 9
    worse = pick(9, 12)
    worse["scores"]["arc"] = 11
    worse["scores"]["ending"] = "high"
    r = propose(prepared, [bad, worse])
    p1, p2 = " ".join(r["rejected"][0]["problems"]), " ".join(r["rejected"][1]["problems"])
    assert "missing criteria: hook" in p1 and "unknown criteria: charisma" in p1
    assert "'arc' must be a number 0-10" in p2 and "'ending'" in p2


def test_missing_required_text_fields(prepared):
    r = propose(prepared, [{"sentence_ids": ["s003"], "scores": {}}])
    t = " ".join(r["rejected"][0]["problems"])
    assert "title is required" in t and "hook is required" in t and "rationale is required" in t


def test_quotes_must_appear_verbatim_inside_the_clip(prepared):
    ok = pick(9, 14, "quoted", hook_quote="we spent eighteen months, building a product", end_quote="Who is already paying someone")
    fake = pick(16, 20, "invented", hook_quote="Nobody ever finishes anything", quotes=["you call ten people"])
    outside = pick(22, 26, "outside", hook_quote="Welcome back to the show")                 # real, but not in these sentences
    r = propose(prepared, [ok, fake, outside])
    assert [a["title"] for a in r["accepted"]] == ["quoted"]
    assert "hook_quote" in r["rejected"][0]["problems"][0] and "does not appear verbatim" in r["rejected"][0]["problems"][0]
    assert "hook_quote" in r["rejected"][1]["problems"][0]


def test_trimmed_filler_is_not_a_valid_quote_but_is_still_reported(prepared, literal_rubric):
    r = clips.propose(prepared, [pick(3, 6, "t", hook_quote="um so the thing")], rubric=literal_rubric)
    assert r["rejected"] and "does not appear verbatim" in r["rejected"][0]["problems"][0]
    # with expansion on (default) the same pick is snapped out to s002, so "Um," is no longer a trimmed edge but sits inside the clip
    assert clips.propose(prepared, [pick(3, 6, "t", hook_quote="um so the thing")])["rejected"] == []


def test_penalties_are_explained(prepared, literal_rubric):
    c = lambda **kw: clips.propose(prepared, [pick(6, 9, "mid", **kw)], rubric=literal_rubric)
    c()
    d = read_json(prepared.artifact("clips.json"))["clips"][0]
    names = {p["name"] for p in d["penalties"]}
    assert "start_incomplete" in names and "starts_mid_thought" not in names               # s006 begins "And that is why": one penalty, not two
    reasons = " ".join(p["reason"] for p in d["penalties"])
    assert "And" in reasons
    c(sensitive=True, scores={k: 10 for k in ("hook", "arc", "insight", "emotion", "shareability", "ending", "speech_quality")})
    d = read_json(prepared.artifact("clips.json"))["clips"][0]
    assert any(p["name"] == "sensitive" and p["points"] == 20 for p in d["penalties"])


def test_context_penalty_auto_and_agent_override(prepared):
    propose(prepared, [pick(7, 10, "ctx")])                                               # s007 refers to "earlier"
    d = read_json(prepared.artifact("clips.json"))["clips"][0]
    assert any(p["name"] == "needs_prior_context" for p in d["penalties"])
    propose(prepared, [pick(7, 10, "ctx", needs_context=False)])
    d = read_json(prepared.artifact("clips.json"))["clips"][0]
    assert not any(p["name"] == "needs_prior_context" for p in d["penalties"])
    assert any("needs_context=false" in w for w in d["warnings"])                          # the override is surfaced to the human


def test_soft_length_penalty_and_minimum_score(prepared):
    cand = read_json(prepared.artifact("clips_candidates.json"))
    cand["params"]["min_s"], cand["params"]["max_s"] = 20, 22
    write_json(prepared.artifact("clips_candidates.json"), cand)
    propose(prepared, [pick(9, 13, "short-ish")])
    d = read_json(prepared.artifact("clips.json"))["clips"][0]
    assert any(p["name"] == "length_out_of_range" for p in d["penalties"])
    weak = pick(14, 18, "weak", scores={k: 1 for k in ("hook", "arc", "insight", "emotion", "shareability", "ending", "speech_quality")})
    r = propose(prepared, [weak])
    assert "below the minimum" in r["rejected"][0]["problems"][0]


def test_stale_candidates_are_refused(prepared):
    tr = read_json(prepared.artifact("transcript.json"))
    tr["words"][5]["w"] = "changed"
    write_json(prepared.artifact("transcript.json"), tr)
    with pytest.raises(SystemExit, match="stale"):
        propose(prepared, [pick(3, 6)])


def test_propose_requires_prepare_first(ep):
    with pytest.raises(SystemExit, match="prepare"):
        clips.propose(ep, [pick(3, 6)])


def test_rerun_keeps_human_decisions_and_blocks_overlap_with_them(prepared):
    propose(prepared, [pick(3, 6, "keeper"), pick(9, 13, "other")])
    clips.decide(prepared, [{"id": "c01", "decision": "approve"}])
    r = propose(prepared, [pick(5, 8, "collides"), pick(14, 18, "fresh")])
    assert r["kept_human_decisions"] == ["c01"]
    assert "overlaps c01" in r["rejected"][0]["problems"][0]
    d = read_json(prepared.artifact("clips.json"))["clips"]
    assert [c["title"] for c in d] == ["keeper", "fresh"]                                  # undecided "other" was replaced
    assert d[1]["id"] == "c03"                                                             # ids are never reused


# ---- decide ------------------------------------------------------------------------------------------------

def test_decide_approve_reject_and_log(prepared):
    propose(prepared, [pick(3, 6, "a"), pick(9, 13, "b")])
    r = clips.decide(prepared, [{"id": "c01", "decision": "approve", "note": "love it"}, {"id": "c02", "decision": "reject"}])
    assert r["approved"] == ["c01"] and not r["errors"]
    d = {c["id"]: c for c in read_json(prepared.artifact("clips.json"))["clips"]}
    assert d["c01"]["status"] == "approved" and d["c01"]["notes"] == ["love it"] and d["c02"]["status"] == "rejected"
    log = [json.loads(x) for x in prepared.artifact("clips_decisions.jsonl").read_text().splitlines()]
    assert [x["decision"] for x in log] == ["approve", "reject"] and log[0]["applied"] and log[0]["before"]["scores"]["hook"] == 8


def test_decide_nudge_resnaps_and_rescores(prepared):
    propose(prepared, [pick(9, 13, "n")])
    before = read_json(prepared.artifact("clips.json"))["clips"][0]
    r = clips.decide(prepared, [{"id": "c01", "decision": "approve", "nudge": {"start": -1, "end": 2}}])
    assert not r["errors"]
    after = read_json(prepared.artifact("clips.json"))["clips"][0]
    assert after["sentence_ids"] == [f"s{k:03d}" for k in range(8, 16)]
    assert after["start"] < before["start"] and after["end"] > before["end"] and after["status"] == "approved"
    assert after["duration"] == pytest.approx(after["end"] - after["start"], abs=1e-3)
    S = clips.Sentences(prepared, editdoc.load_latest(prepared, "assembly"), rubric(prepared))
    assert after["start"] == S.snap(8, 15)["start"] and after["end"] == S.snap(8, 15)["end"]
    log = [json.loads(x) for x in prepared.artifact("clips_decisions.jsonl").read_text().splitlines()]
    assert log[-1]["nudge"] == {"start": -1, "end": 2} and log[-1]["after"]["sentence_ids"] == ["s008", "s015"]


def test_nudge_alone_returns_clip_to_review(prepared):
    propose(prepared, [pick(9, 13, "n")])
    clips.decide(prepared, [{"id": "c01", "decision": "approve"}])
    clips.decide(prepared, [{"id": "c01", "decision": "nudge", "nudge": {"end": 1}}])
    assert read_json(prepared.artifact("clips.json"))["clips"][0]["status"] == "proposed"


def test_bad_nudges_and_ids_are_errors_not_changes(prepared, literal_rubric):
    clips.propose(prepared, [pick(0, 3, "a"), pick(5, 8, "b")], rubric=literal_rubric)
    before = read_json(prepared.artifact("clips.json"))
    r = clips.decide(prepared, [{"id": "c01", "decision": "approve", "nudge": {"start": -5}},      # leaves the transcript
                                {"id": "c01", "decision": "approve", "nudge": {"end": 1}},        # fine: s004 is free
                                {"id": "c01", "decision": "approve", "nudge": {"end": 2}},        # now reaches s005..s006: overlaps c02
                                {"id": "c01", "decision": "approve", "nudge": {"end": -3}},       # too short
                                {"id": "c99", "decision": "approve"},
                                {"id": "c02", "decision": "maybe"}])
    msgs = [e["error"] for e in r["errors"]]
    assert len(msgs) == 5 and "leaves the transcript" in msgs[0]
    assert "overlap" in msgs[1] and "outside" in msgs[2] and "unknown clip id" in msgs[3] and "must be approve" in msgs[4]
    after = {c["id"]: c for c in read_json(prepared.artifact("clips.json"))["clips"]}
    assert after["c02"]["status"] == "proposed"                                                    # bad decision changed nothing
    assert len(prepared.artifact("clips_decisions.jsonl").read_text().splitlines()) == 6           # failures are logged too
    assert before["clips"][1]["sentence_ids"] == after["c02"]["sentence_ids"]


# ---- build: selects EditDocs -------------------------------------------------------------------------------

def test_build_makes_valid_select_docs_with_right_episode_times(prepared):
    propose(prepared, [pick(9, 13, "story"), pick(16, 20, "advice")])
    r = clips.build(prepared)
    assert set(r["clips"]) == {"c01", "c02"}
    lay = layout()
    for cid, (a, b) in (("c01", (9, 12)), ("c02", (16, 20))):
        doc = editdoc.load(prepared.root / r["clips"][cid])
        assert editdoc.validate(doc) == [] and doc["stage"] == "select"
        c = next(x for x in read_json(prepared.artifact("clips.json"))["clips"] if x["id"] == cid)
        assert doc["duration"] == pytest.approx(c["duration"], abs=2e-3)
        d0 = doc["tracks"]["dialogue"][0]
        assert d0["e0"] == pytest.approx(c["start"], abs=2e-3)                                  # assembly: output == episode time
        assert d0["e0"] <= lay[a][2] and d0["e0"] >= lay[a][2] - 0.11
        assert c["title"] in doc["notes"] and "hook:" in doc["notes"]
    assert editdoc.list_versions(prepared, "select", "story") == [1]


def test_build_through_a_cut_maps_to_the_right_episode_times(tmp_path):
    lay = layout()
    ep = build_long_episode(tmp_path / "ep", remove=(lay[8][2], lay[12][2]))              # sentences 8..11 are gone
    clips.prepare(ep, min_s=10, max_s=25)
    S = clips.Sentences(ep, editdoc.load_latest(ep, "assembly"), rubric(ep))
    k = next(s["i"] for s in S.items if s["text"] == SENTENCES[12][1])                     # "That sounds brutal." now follows the cut
    assert k == 8
    clips.propose(ep, [pick(k, k + 4, "after cut")])
    doc = editdoc.load(ep.root / clips.build(ep)["clips"]["c01"])
    assert editdoc.validate(doc) == []
    c = read_json(ep.artifact("clips.json"))["clips"][0]
    assert c["start"] == pytest.approx(lay[8][2] - 0.1, abs=0.01)                          # output time: right where the cut closed up
    d = doc["tracks"]["dialogue"]
    assert d[0]["e0"] == pytest.approx(lay[8][2] - 0.1, abs=0.01)                          # the pre-roll is the tail of the segment before the cut
    assert d[1]["e0"] == pytest.approx(lay[12][2], abs=2e-3)                               # then the audio jumps to episode time after the removed range


def test_selects_timeline_is_valid_and_concatenates_with_gaps(prepared):
    propose(prepared, [pick(16, 20, "advice"), pick(9, 13, "story")])
    r = clips.build(prepared, gap_s=1.5)
    sel = editdoc.load(prepared.root / r["selects"])
    assert editdoc.validate(sel) == [] and sel["stage"] == "select"
    docs = [editdoc.load(prepared.root / p) for p in r["clips"].values()]
    assert sel["duration"] == pytest.approx(sum(d["duration"] for d in docs) + 1.5, abs=1e-3)
    fills = [s for s in sel["tracks"]["dialogue"] if s.get("fill") == "room_tone"]
    assert len(fills) == 1 and fills[0]["o1"] - fills[0]["o0"] == pytest.approx(1.5)
    assert sel["tracks"]["room_tone"] == docs[0]["tracks"]["room_tone"]
    ids = [s["id"] for n in ("video", "dialogue") for s in sel["tracks"][n]]
    assert len(ids) == len(set(ids))
    order = [s["id"].split("_")[0] for s in sel["tracks"]["dialogue"] if not s.get("fill")]
    assert order[0] == "c02" and order[-1] == "c01"                                              # chronological: story (s009) before advice (s016)... by time
    assert "story" in sel["notes"] and "advice" in sel["notes"]


def test_selects_order_by_score(prepared):
    low = pick(9, 13, "low", scores={k: 6 for k in ("hook", "arc", "insight", "emotion", "shareability", "ending", "speech_quality")})
    high = pick(16, 20, "high", scores={k: 9 for k in ("hook", "arc", "insight", "emotion", "shareability", "ending", "speech_quality")})
    propose(prepared, [low, high])
    sel = editdoc.load(prepared.root / clips.build(prepared, order="score")["selects"])
    assert [s["id"].split("_")[0] for s in sel["tracks"]["dialogue"] if not s.get("fill")][0] == "c02"


def test_build_is_idempotent_until_something_changes(prepared):
    propose(prepared, [pick(9, 13, "story")])
    clips.build(prepared)
    r2 = clips.build(prepared)
    assert r2["built"] == [] and r2["unchanged"] == ["c01"]
    assert editdoc.list_versions(prepared, "select", "story") == [1] and editdoc.list_versions(prepared, "select", "selects") == [1]
    clips.decide(prepared, [{"id": "c01", "decision": "approve", "nudge": {"end": 1}}])
    r3 = clips.build(prepared)
    assert r3["built"] == ["c01"] and editdoc.list_versions(prepared, "select", "story") == [1, 2]   # never overwritten
    assert editdoc.list_versions(prepared, "select", "selects") == [1, 2]
    assert clips.build(prepared, force=True)["built"] == ["c01"]


def test_build_approved_only(prepared):
    propose(prepared, [pick(9, 13, "a"), pick(16, 20, "b")])
    with pytest.raises(SystemExit, match="approved"):
        clips.build(prepared, approved_only=True)
    clips.decide(prepared, [{"id": "c02", "decision": "approve"}])
    r = clips.build(prepared, approved_only=True)
    assert list(r["clips"]) == ["c02"]


def test_build_refuses_when_source_changed(prepared):
    propose(prepared, [pick(9, 13, "a")])
    tr = read_json(prepared.artifact("transcript.json"))
    tr["words"][40]["w"] = "different"
    write_json(prepared.artifact("transcript.json"), tr)
    with pytest.raises(SystemExit, match="stale"):
        clips.build(prepared)


def test_concat_rejects_mismatched_sources():
    d1 = editdoc.new_doc("select", 30, (640, 360), {"A": {"file": "a.mp4", "kind": "video", "start_offset": 0.0}}, 1.0)
    d2 = editdoc.new_doc("select", 30, (640, 360), {"A": {"file": "a.mp4", "kind": "video", "start_offset": 5.0}}, 1.0)
    for d in (d1, d2):
        d["tracks"]["video"] = [{"id": "v", "src": "A", "o0": 0, "o1": 1, "e0": 0}]
        d["tracks"]["dialogue"] = [{"id": "d", "src": "A", "o0": 0, "o1": 1, "e0": 0}]
    with pytest.raises(ValueError, match="differs"):
        clips.concat_docs([("x", d1), ("y", d2)], 0.5)


def test_a_small_cap_spreads_candidates_across_the_whole_episode(tmp_path, monkeypatch):
    """REAL-FOOTAGE REGRESSION (first real episode): all windows tie on hint, ties sorted by start time, and a cap of 60 kept only the
    first 36 minutes of a 103-minute session without saying so. Truncation must be spread over the whole edit and must be reported."""
    monkeypatch.setattr(clips, "_hint", lambda f, d, h: (1.0, []))       # the real session: EVERY window scored the same hint
    ep = build_long_episode(tmp_path / "ep")
    full = clips.prepare(ep, "assembly", None, 6, 10, None, 10_000)
    avail = full["windows"]
    assert avail >= 9, "the fixture needs enough windows to truncate"
    small = clips.prepare(ep, "assembly", None, 6, 10, None, 5)
    cand = json.loads(ep.artifact("clips_candidates.json").read_text())
    starts = sorted(w["start"] for w in cand["windows"])
    duration = cand["source"]["duration"]
    assert len(starts) == 5 and small["windows_available"] == avail
    assert starts[0] < 0.25 * duration and starts[-1] > 0.7 * duration, f"windows cluster at the start: {starts}"


def test_the_default_cap_is_a_configured_sanity_cap_not_five(ep):
    from ave import recall
    r = clips.load_rubric(ep)
    cfg = recall.config(r)
    assert r['recall']['max_per_session'] == 80 and r['recall']['floor'] == 30 and cfg['alternates'] == 3
    assert recall.floor(r) == 30 and recall.floor({'accept_min_score': 45}) == 45          # an older policy file still works
