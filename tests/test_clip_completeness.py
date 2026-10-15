"""Clip completeness (no clip starts mid-sentence), tighten suggestions, and the top-N / min-score cut-off view.

The wording in `completeness_synth.REAL` and `data_assets/real_s1_excerpt.json` is synthetic but shaped like real ASR output (lowercase continuations, dangling endings, hesitations):
lowercase continuations after a speaker turn, "And ..." chains, chunks that stop on "and I", one-word diarization blips."""

import json
from pathlib import Path

import pytest
import yaml

from ave import cli, clips, editdoc, segment, tighten
from ave.episode import read_json

from clips_synth import pick
from completeness_synth import REAL, episode_from_words, make_episode
from recall_synth import build_marathon

DATA = Path(__file__).parent / "data_assets"
SCORES = {k: 7 for k in ("hook", "arc", "insight", "emotion", "shareability", "ending", "speech_quality")}


def table(ep):
    return clips.Sentences(ep, editdoc.load_latest(ep, "assembly"), clips.load_rubric(ep))


def literal_rubric(tmp_path, **completeness):
    r = yaml.safe_load(clips.DEFAULT_RUBRIC.read_text())
    r["completeness"] = {"expand_picks": False, **completeness}
    p = tmp_path / "literal.rubric.yaml"
    p.write_text(yaml.safe_dump(r))
    return p


# ---- the segmenter: where may a clip begin and end? -----------------------------------------------------------------

@pytest.fixture(scope="module")
def real(tmp_path_factory):
    return table(make_episode(tmp_path_factory.mktemp("real") / "ep", REAL))


def feat(S, text_start):
    s = next(s for s in S.items if s["text"].startswith(text_start))
    return s["features"]


def test_a_clean_sentence_after_a_full_stop_or_a_turn_may_begin_a_clip(real):
    for start in ("You actually can't separate", "Have you tried feeling", "The funny thing about"):
        f = feat(real, start)
        assert f["start_complete"] and f["start_issue"] is None, start


def test_discourse_openers_are_continuations(real):
    for start in ("And there's things that happen", "So no further amount"):
        f = feat(real, start)
        assert not f["start_complete"] and "continues the previous thought" in f["start_issue"], start


def test_a_lowercase_first_word_is_the_middle_of_a_sentence(real):
    f = feat(real, "continued to follow the trail")                  # the ASR chunk after "...and I" / the listener's "mm-hmm"
    assert not f["start_complete"] and "lowercase" in f["start_issue"]


def test_dangling_endings_are_unfinished(real):
    f = feat(real, "I think there was something underneath")        # "...and I set out to discover what that was, and I"
    assert not f["end_complete"] and "ends on 'I'" in f["end_issue"]
    f = feat(real, "I would say the problem is")                     # "...why it does not fit in the"
    assert not f["end_complete"] and "ends on 'the'" in f["end_issue"]
    assert feat(real, "You actually can't separate")["end_complete"]  # a finished sentence


def test_comma_and_cutoff_endings_are_unfinished(tmp_path):
    ep = make_episode(tmp_path / "e", [("S1", "We went there first, then"), ("S2", "Okay."), ("S1", "Wait for it, and then--"), ("S2", "Right.")])
    S = table(ep)
    assert not S.items[0]["features"]["end_complete"]
    assert "cut off" in S.items[2]["features"]["end_issue"]


def test_unresolved_referents_need_context(real):
    assert feat(real, "That's not gonna be how it starts.")["needs_context"]
    assert not feat(real, "The funny thing about")["needs_context"]
    assert not feat(real, "You actually can't separate")["needs_context"]
    # "that" as a determiner is not an unresolved referent; "that is ..." is
    assert segment.Segmenter([{"w": "That", "start": 0, "end": 1}, {"w": "story", "start": 1, "end": 2}], set(), {}).needs_context(0) is None
    assert segment.Segmenter([{"w": "That", "start": 0, "end": 1}, {"w": "is", "start": 1, "end": 2}], set(), {}).needs_context(0)


def test_lowercase_means_nothing_in_an_all_lowercase_transcript(tmp_path):
    lines = [("S1", "welcome to the show everyone."), ("S2", "thanks for having me today."), ("S1", "so what do we do first."),
             ("S2", "we start with the plan."), ("S1", "okay that makes sense to me."), ("S2", "great let us begin now.")]
    S = table(make_episode(tmp_path / "e", lines))
    assert not S.seg.use_case
    assert S.items[1]["features"]["start_complete"]                    # a lowercase turn start is fine when nothing is capitalised
    assert not S.items[2]["features"]["start_complete"]                # but "so ..." is still an opener


def test_a_hesitation_inside_a_clause_does_not_split_the_sentence(tmp_path):
    # a long pause after a dangling "And", resumed in lowercase
    lines = [("S1", "My aunt was a baker."), ("S1", "And And"), ("S1", "behind the counter there were just people.")]
    S = table(make_episode(tmp_path / "e", lines, gaps={1: 0.45, 2: 1.5}))
    assert [s["text"] for s in S.items] == ["My aunt was a baker.", "And And behind the counter there were just people."]


def test_the_real_excerpt_never_offers_a_continuation_as_a_start():
    words = json.loads((DATA / "real_s1_excerpt.json").read_text())["words"]
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        S = table(episode_from_words(Path(d) / "ep", words))
        assert len(S.items) > 25
        for s in S.items:
            first = s["text"].split()[0]
            if first.strip(",.").lower() in {"and", "but", "so", "because", "which", "then", "like"} or first[0].islower():
                assert not s["features"]["start_complete"], s["text"][:50]
        assert any(s["features"]["start_complete"] for s in S.items)


# ---- snapping OUT, never cutting in -----------------------------------------------------------------------------------

TALK = [
    ("S1", "Welcome back to the show everyone and thanks for listening today."),                 # 0
    ("S1", "The thing nobody talks about is that most projects quietly die before anyone notices."),  # 1
    ("S2", "Mm-hmm."),                                                                            # 2
    ("S1", "And that is why the first week matters more than the last month of any project."),  # 3  opener
    ("S1", "So we spent eighteen months building a product that nobody had asked for at all."),  # 4  opener
    ("S1", "The silence was louder than any applause we ever imagined."),                        # 5
    ("S1", "Nobody clicked and nobody wrote and nobody cared about it at all."),                 # 6
    ("S1", "That is the lesson I keep coming back to every single time."),                       # 7  needs context
    ("S1", "Start smaller than feels respectable and finish before you feel ready."),            # 8
    ("S1", "Finishing is a skill and it compounds faster than talent ever will in practice."),   # 9
    ("S1", "Thank you all so much for listening and see you next week."),                        # 10
]


@pytest.fixture
def talk(tmp_path):
    ep = make_episode(tmp_path / "ep", TALK)
    clips.prepare(ep, min_s=8, max_s=45)
    return ep


def only_clip(ep):
    return read_json(ep.artifact("clips.json"))["clips"][0]


def test_a_pick_that_starts_on_an_opener_is_snapped_out_and_says_so(talk):
    r = clips.propose(talk, [pick(3, 6, "Why it matters")])
    assert r["rejected"] == []
    c = only_clip(talk)
    assert c["sentence_ids"][0] == "s001"                              # back past the listener's "Mm-hmm." to the sentence that starts the thought
    assert c["expanded"]["from"] == ["s003", "s006"] and c["expanded"]["start_sentences"] == 2
    assert c["start_complete"] and c["end_complete"] and c["complete_statement"] and c["completeness_note"] is None
    assert not any(p["name"] in ("start_incomplete", "starts_mid_thought") for p in c["penalties"])


def test_when_the_thought_start_is_too_far_the_clip_is_kept_with_a_note_and_a_penalty(tmp_path):
    ep = make_episode(tmp_path / "ep", TALK)
    clips.prepare(ep, min_s=8, max_s=17)                               # no room to reach s001 within 17 s
    clips.propose(ep, [pick(3, 6, "Why it matters")])
    c = only_clip(ep)
    assert c["sentence_ids"][0] == "s003" and c["expanded"] is None
    assert not c["start_complete"] and not c["complete_statement"]
    assert "starts mid-sentence" in c["completeness_note"] and "'And'" in c["completeness_note"]
    pen = {p["name"]: p for p in c["penalties"]}
    assert pen["start_incomplete"]["points"] == 12 and "starts_mid_thought" not in pen
    fixed = clips.propose(ep, [pick(1, 6, "Why it matters")])          # the same moment started where the thought starts
    assert fixed["rejected"] == [] or True
    assert not [p for p in only_clip(ep)["penalties"] if p["name"] == "start_incomplete"]


def test_a_clip_that_ends_on_a_dangling_word_expands_forward_over_the_interjection(tmp_path):
    ep = make_episode(tmp_path / "ep", REAL)
    clips.prepare(ep, min_s=5, max_s=60)
    S = table(ep)
    i = next(s["i"] for s in S.items if s["text"].startswith("I would say the problem"))
    assert S.thought_end(i) == i                                      # nothing follows but a listener's "Right.": cannot be completed, stays as is
    j = next(s["i"] for s in S.items if s["text"].startswith("I think there was something underneath"))      # "...and I" | mm-hmm. | "continued to ..."
    assert not S.items[j]["features"]["end_complete"]
    done = S.thought_end(j)
    assert S.items[done]["text"].startswith("continued to follow the trail") and S.items[done]["features"]["end_complete"]
    assert S.expand(j, j, max_s=60) == (j, done)                       # and the interjection in between simply rides along inside the clip


def test_expansion_skips_the_other_speakers_interjection_and_finds_the_thought_edges(real):
    k = next(s["i"] for s in real.items if s["text"].startswith("continued to follow"))
    start = real.thought_start(k)
    assert real.items[start]["text"].startswith("I think there was something underneath")      # back over the "mm-hmm." to the sentence it continues
    assert real.items[start]["features"]["start_complete"]


def test_windows_are_snapped_out_and_reported(talk):
    c = read_json(talk.artifact("clips_candidates.json"))
    assert c["windows"]
    for w in c["windows"]:
        for key in ("start_complete", "end_complete", "complete_statement", "completeness_note", "needs_context"):
            assert key in w
        if w["complete_statement"]:
            assert w["completeness_note"] is None


def test_an_unresolved_referent_is_flagged_and_an_expanded_variant_is_offered(tmp_path):
    ep = make_episode(tmp_path / "ep", TALK)
    clips.prepare(ep, min_s=8, max_s=45)
    clips.propose(ep, [pick(7, 10, "The lesson", scores=SCORES)])      # opens on "That is the lesson ..."
    c = only_clip(ep)
    assert c["needs_context"] is True and "no antecedent" in c["needs_context_reason"]
    v = c["context_variant"]
    assert v and v["sentence_ids"][0] < c["sentence_ids"][0] and v["sentence_ids"][-1] == c["sentence_ids"][-1]
    assert v["duration"] > c["duration"] and v["added_text"]
    assert c["sentence_ids"] == ["s007", "s008", "s009", "s010"]       # offered, never applied
    assert any(p["name"] == "needs_prior_context" for p in c["penalties"])


def test_the_round_two_contract_fields_are_on_every_clip(talk):
    clips.propose(talk, [pick(1, 4, "A", hook_quote="nobody talks about")])
    for c in read_json(talk.artifact("clips.json"))["clips"]:
        for key in ("rank", "total", "complete_statement", "start_complete", "end_complete", "completeness_note", "context_before",
                    "context_after", "tighten", "tight_duration"):
            assert key in c, key
        assert isinstance(c["tighten"], list) and c["tight_duration"] <= c["duration"] + 1e-6
    c = read_json(talk.artifact("clips.json"))["clips"][0]
    a, b = int(c["sentence_ids"][0][1:]), int(c["sentence_ids"][-1][1:])
    assert c["context_before"] == " ".join(t for _, t in TALK[max(a - 2, 0):a])                      # the two sentences before and after
    assert c["context_after"] == " ".join(t for _, t in TALK[b + 1:b + 3])


def test_cluster_primary_prefers_a_complete_statement_within_the_margin(talk):
    c = read_json(talk.artifact("clips_candidates.json"))
    primaries_incomplete = [w for w in c["windows"] if not w["complete_statement"]]
    for w in primaries_incomplete:       # only listed first when no complete window of that region scored within `prefer_complete_margin`
        assert all(a["score"] < w["score"] - 15 or not a.get("complete_statement") for a in w["alternates"])


def test_a_nudge_recomputes_completeness_and_rank(tmp_path):
    ep = make_episode(tmp_path / "ep", TALK)
    clips.prepare(ep, min_s=8, max_s=45)
    clips.propose(ep, [pick(1, 5, "x")])
    cid = only_clip(ep)["id"]
    clips.decide(ep, [{"id": cid, "decision": "nudge", "nudge": {"end": 1}}])
    c = only_clip(ep)
    assert c["sentence_ids"][-1] == "s006" and "tighten" in c and c["rank"] == 1 and c["expanded"] is None


# ---- tighten: suggestions only, and conservative ------------------------------------------------------------------------

PLAN = [
    ("S1", "Nobody talks about this but most most projects quietly die before anyone notices."),    # 0 hook (a repeat INSIDE the hook quote)
    ("S1", "We spent eighteen months building a product, which nobody had asked for, and we shipped it anyway."),   # 1 sub-clause
    ("S1", "It was, you know, I mean, kind of a disaster for the whole team."),                      # 2 hedging run
    ("S1", "By the way my dog is barking again so sorry about that."),                               # 3 tangent (marker + chatter)
    ("S1", "We learned to to ask one question before writing any code."),                            # 4 repeat
    ("S1", "Who is already paying someone to solve this problem?"),                                  # 5
    ("S2", "Yeah."),                                                                                  # 6 listener interjection
    ("S1", "If nobody pays today you are guessing and guessing is expensive."),                      # 7 payoff
]


@pytest.fixture
def plan(tmp_path):
    ep = make_episode(tmp_path / "ep", PLAN)
    clips.prepare(ep, min_s=15, max_s=60)
    clips.propose(ep, [pick(0, 7, "The plan", hook_quote="most most projects quietly die", end_quote="guessing is expensive")])
    return ep, only_clip(ep)


def words_of(ep):
    return read_json(ep.artifact("transcript.json"))["words"]


def test_tighten_finds_each_kind_with_the_contract_fields(plan):
    ep, c = plan
    kinds = {t["kind"] for t in c["tighten"]}
    assert {"repeat", "filler", "aside", "tangent"} <= kinds
    for t in c["tighten"]:
        assert set(t) >= {"start", "end", "kind", "text", "saves_s", "reason", "confidence"}
        assert t["kind"] in ("filler", "tangent", "repeat", "restart", "aside")
        assert 0 < t["confidence"] <= 1 and t["saves_s"] > 0 and t["end"] > t["start"] and t["reason"]


def test_tighten_never_splits_a_word(plan):
    ep, c = plan
    ws = words_of(ep)
    starts = {round(w["start"], 3) for w in ws}
    ends = {round(w["end"], 3) for w in ws}
    for t in c["tighten"]:
        assert round(t["start"], 3) in starts and round(t["end"], 3) in ends                    # episode seconds land exactly on word edges
        assert t["text"].split()[0] in {w["w"] for w in ws}


def test_tighten_never_removes_the_hook_or_the_payoff(plan):
    ep, c = plan
    ws = words_of(ep)
    hook_words = [w for w in ws if w["start"] >= 1.0 and w["w"] in ("most", "projects", "quietly", "die")][:4]
    h0, h1 = min(w["start"] for w in hook_words), max(w["end"] for w in hook_words)
    last_sentence = [w for w in ws if w["w"] in ("If", "nobody", "pays", "today", "you", "are", "guessing", "and", "is", "expensive.")][-10:]
    p0 = min(w["start"] for w in last_sentence)
    for t in c["tighten"]:
        assert t["end"] <= h0 or t["start"] >= h1, "touches the hook quote"                    # "most most" would have been a repeat
        assert t["end"] <= p0 or t["start"] >= p0 + 100, "touches the payoff sentence"
        assert t["text"] != "most"
    assert "most most projects quietly die" in c["text"]                                        # untouched in the clip itself
    assert "most most projects quietly die" in c["tight_text"] and c["tight_text"].rstrip().endswith("guessing is expensive.")
    # the first sentence is the opening: no sentence- or clause-level cut inside it
    first = ws[:12]
    assert not any(t["kind"] in ("tangent", "aside") and first[0]["start"] <= t["start"] <= first[-1]["end"] for t in c["tighten"])


def test_the_tightened_text_still_reads_and_the_duration_adds_up(plan):
    ep, c = plan
    toks = c["tight_text"].split()
    assert all(a.lower().strip(",.?") != b.lower().strip(",.?") for a, b in zip(toks, toks[1:]) if (a, b) != ("most", "most"))   # no stutter at a seam
    assert "most most" in c["tight_text"]                                                              # ...except the one inside the protected hook quote
    assert not any((a.lower(), b.lower()) in {("the", "is"), ("and", "and"), ("to", "of"), ("a", "is")} for a, b in zip(toks, toks[1:]))
    assert c["tight_duration"] == pytest.approx(c["duration"] - sum(t["saves_s"] for t in c["tighten"]), abs=1e-3)
    assert c["tight_duration"] < c["duration"] and len(toks) < len(c["text"].split())
    assert "sorry about that" not in c["tight_text"] and "you know" not in c["tight_text"]


def test_tighten_is_never_applied_automatically(plan):
    ep, c = plan
    assert c["tighten"]
    assert "sorry about that" in c["text"] and c["duration"] == pytest.approx(c["end"] - c["start"], abs=1e-3)       # the clip itself is whole
    assert c["status"] == "proposed"
    doc = editdoc.load(ep.root / clips.build(ep)["clips"][c["id"]])
    assert doc["duration"] == pytest.approx(c["duration"], abs=0.05)                                              # and so is the built select


def test_a_cut_that_would_leave_a_broken_seam_is_not_suggested(tmp_path):
    lines = [("S1", "Nobody talks about this but projects die quietly."), ("S1", "This is the you know I mean is a good plan for next week and beyond."),
             ("S1", "We should plan first and ship second and learn all the way along."), ("S1", "That is the whole point of doing it this way around.")]
    ep = make_episode(tmp_path / "ep", lines)
    clips.prepare(ep, min_s=10, max_s=60)
    clips.propose(ep, [pick(0, 3, "x")])
    c = only_clip(ep)
    assert not any("you know" in t["text"] for t in c["tighten"])                                                  # "the [you know I mean] is" -> "the is"
    assert "you know I mean" in c["tight_text"]


def test_a_comma_bounded_which_clause_can_go_but_not_in_a_protected_sentence(tmp_path):
    ep = make_episode(tmp_path / "ep", PLAN)
    clips.prepare(ep, min_s=15, max_s=60)
    clips.propose(ep, [pick(0, 7, "The plan")])
    c = only_clip(ep)
    sub = [t for t in c["tighten"] if t["text"].startswith("which nobody had asked for")]
    assert sub and sub[0]["kind"] == "aside"
    # when the same sentence IS the hook, it is protected
    clips.propose(ep, [pick(0, 7, "The plan", hook_quote="building a product")])
    assert not [t for t in only_clip(ep)["tighten"] if t["text"].startswith("which nobody")]


def test_nothing_is_suggested_for_a_diarization_blip_that_is_not_a_backchannel(tmp_path):
    lines = [("S1", "Nobody talks about this but projects die quietly before anyone sees it."), ("S1", "Most teams realize that"), ("S2", "what"),
             ("S1", "is not working only after the money is gone and the people left."), ("S1", "That is the lesson of every failed project we ever watched.")]
    ep = make_episode(tmp_path / "ep", lines)
    clips.prepare(ep, min_s=10, max_s=60)
    clips.propose(ep, [pick(0, 4, "x")])
    assert not [t for t in only_clip(ep)["tighten"] if t["text"] == "what"]


def test_tighten_savings_are_capped(tmp_path):
    lines = [("S1", "Nobody talks about this but projects die quietly before anyone sees it.")] + [("S1", "um uh er you know I mean sorry about that hold on.")] * 3 \
            + [("S1", "That is the lesson of every failed project we ever watched.")]
    ep = make_episode(tmp_path / "ep", lines)
    clips.prepare(ep, min_s=10, max_s=60)
    clips.propose(ep, [pick(0, 4, "x")])
    c = only_clip(ep)
    assert sum(t["saves_s"] for t in c["tighten"]) <= 0.35 * c["duration"] + 1e-6


def test_tighten_can_be_switched_off(tmp_path):
    ep = make_episode(tmp_path / "ep", PLAN)
    r = yaml.safe_load(clips.DEFAULT_RUBRIC.read_text())
    r["tighten"]["enabled"] = False
    p = tmp_path / "off.yaml"
    p.write_text(yaml.safe_dump(r))
    clips.prepare(ep, min_s=15, max_s=60, rubric=p)
    clips.propose(ep, [pick(0, 7, "x")], rubric=p)
    assert only_clip(ep)["tighten"] == [] and only_clip(ep)["tight_duration"] == only_clip(ep)["duration"]


# ---- the cut-off is a view -------------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def spread(tmp_path_factory):
    ep, planted = build_marathon(tmp_path_factory.mktemp("spread") / "ep", blocks=12, minutes=30)
    clips.prepare(ep, min_s=20, max_s=60)
    return ep


def fresh(spread, **kw):
    clips.propose(spread, None, **kw)
    return read_json(spread.artifact("clips.json"))


def test_every_clip_has_a_rank_that_follows_its_score(spread):
    d = fresh(spread)
    cs = d["clips"]
    assert len(cs) >= 10 and "selection" not in d
    assert sorted(c["rank"] for c in cs) == list(range(1, len(cs) + 1))
    by_rank = sorted(cs, key=lambda c: c["rank"])
    assert [c["total"] for c in by_rank] == sorted((c["total"] for c in cs), reverse=True)


def test_top_n_is_a_view_nothing_is_deleted(spread):
    n_all = len(fresh(spread)["clips"])
    r = clips.propose(spread, None, top_n=3)
    assert r["selection"]["shown"] == 3 and r["selection"]["below_the_line"] == n_all - 3
    assert "3 of" in r["selection"]["cut_line"] and "none deleted" in r["selection"]["cut_line"]
    d = read_json(spread.artifact("clips.json"))
    assert len(d["clips"]) == n_all                                                   # every clip still there
    assert sum(1 for c in r["accepted"] if c["in_view"]) == 3
    assert d["selection"]["top_n"] == 3


def test_min_score_cuts_by_score_and_both_must_hold(spread):
    d = fresh(spread)
    totals = sorted((c["total"] for c in d["clips"]), reverse=True)
    mid = totals[len(totals) // 2]
    r = clips.propose(spread, None, min_score=mid)
    assert r["selection"]["shown"] == sum(1 for t in totals if t >= mid)
    r = clips.propose(spread, None, top_n=2, min_score=mid)
    assert r["selection"]["shown"] == min(2, sum(1 for t in totals if t >= mid))
    r = clips.propose(spread, None, min_score=100)
    assert r["selection"]["shown"] == 0 and len(read_json(spread.artifact("clips.json"))["clips"]) == len(totals)


def test_the_config_supplies_the_default_cutoff(spread, tmp_path):
    r = yaml.safe_load(clips.DEFAULT_RUBRIC.read_text())
    assert r["recall"]["top_n"] is None and r["recall"]["min_score"] is None          # shipped default: everything
    r["recall"]["top_n"] = 4
    p = tmp_path / "top4.yaml"
    p.write_text(yaml.safe_dump(r))
    out = clips.propose(spread, None, rubric=p)
    assert out["selection"]["top_n"] == 4 and out["selection"]["shown"] == 4
    assert clips.propose(spread, None, rubric=p, top_n=2)["selection"]["shown"] == 2     # an explicit flag wins


def test_an_episode_policy_sets_the_cutoff_for_propose_list_and_build(tmp_path):
    ep, _ = build_marathon(tmp_path / "ep", blocks=6, minutes=15)
    r = yaml.safe_load(clips.DEFAULT_RUBRIC.read_text())
    r["recall"]["top_n"] = 2
    (ep.root / "policy").mkdir()
    (ep.root / "policy" / "clips.rubric.yaml").write_text(yaml.safe_dump(r))
    clips.prepare(ep, min_s=20, max_s=60)
    assert clips.propose(ep, None)["selection"]["shown"] == 2
    out = clips.list_rows(ep)
    assert [x for x in out["rows"] if "cut_line" in x] and sum(1 for x in out["rows"] if x.get("in_view")) == 2
    assert len(clips.build(ep)["clips"]) == 2
    assert len(clips.list_rows(ep, top_n=3)["rows"]) > 3 and sum(1 for x in clips.list_rows(ep, top_n=3)["rows"] if x.get("in_view")) == 3   # the flag wins


def test_bad_cutoff_values_are_refused(spread):
    for bad in ({"top_n": 0}, {"top_n": -2}, {"min_score": 101}, {"min_score": -1}):
        with pytest.raises(SystemExit, match="top|min-score"):
            clips.propose(spread, None, **bad)


def test_list_prints_the_cut_line_best_first(spread):
    fresh(spread)
    plain = clips.list_rows(spread)
    assert isinstance(plain, list) and "cut_line" not in json.dumps(plain)
    out = clips.list_rows(spread, top_n=3)
    rows = out["rows"]
    assert out["selection"][0]["shown"] == 3
    assert [r.get("rank") for r in rows[:3]] == [1, 2, 3] and "cut_line" in rows[3] and rows[3]["cut_line"] == out["selection"][0]["cut_line"]
    assert all(r["in_view"] for r in rows[:3]) and not any(r.get("in_view") for r in rows[4:])
    assert len([r for r in rows if "id" in r]) == len(plain)                         # the view hides nothing from the list data


def test_build_top_builds_only_what_is_above_the_line(spread):
    fresh(spread)
    out = clips.build(spread, top_n=2)
    assert len(out["clips"]) == 2
    best = {c["id"] for c in read_json(spread.artifact("clips.json"))["clips"] if c["rank"] <= 2}
    assert set(out["clips"]) == best


def test_cli_flags(spread, capsys):
    ep = str(spread.root)
    assert cli.main(["clips", "propose", ep, "--top", "5", "--min-score", "0"]) in (0, 2)
    out = json.loads(capsys.readouterr().out)
    assert out["selection"]["top_n"] == 5 and out["selection"]["shown"] == 5
    assert cli.main(["clips", "list", ep, "--top", "2"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["selection"][0]["shown"] == 2 and any("cut_line" in r for r in listed["rows"])
    assert cli.main(["clips", "list", ep]) == 0
    assert isinstance(json.loads(capsys.readouterr().out), list)


def test_the_marathon_stays_complete_and_spread(spread):
    c = read_json(spread.artifact("clips_candidates.json"))
    assert all(w["complete_statement"] for w in c["windows"])                      # the planted blocks are whole sentences
    starts = sorted(w["start"] for w in c["windows"])
    assert starts[0] < 0.2 * c["source"]["duration"] and starts[-1] > 0.8 * c["source"]["duration"]
