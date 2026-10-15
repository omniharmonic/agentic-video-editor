"""Trim suggestions and `trim set` (T12 item 3): setup chatter and the real start/end, human-approved, honoured by assemble."""

import json
import os
from pathlib import Path

import pytest

from ave import assemble, cli, cutplan, editdoc, state, trim
from ave.episode import Episode, read_json, write_json

_NONE = Path("/nonexistent/ave-real-footage-not-configured")


def _real(var: str) -> Path:
    """Optional real-footage inputs come from an env var; unset means the dependent tests skip (nothing is ever probed on a hard-coded drive)."""
    v = os.environ.get(var)
    return Path(v).expanduser() if v else _NONE

# AVE_TEST_REAL_TRANSCRIPT: a real work/transcript.json (cutlist.s1.json must sit beside it)
REAL = _real("AVE_TEST_REAL_TRANSCRIPT")


def build_words(spec):
    """spec: list of (start, speaker, text). Words get 0.3 s each with 0.05 s between; returns transcript-like dict."""
    words = []
    for t, spk, text in spec:
        for k, tok in enumerate(text.split()):
            words.append({"w": tok, "start": round(t + 0.35 * k, 3), "end": round(t + 0.35 * k + 0.3, 3), "conf": 0.9, "speaker": spk})
    return {"source": "x.wav", "duration": 1500.0, "engine": {}, "speakers": {}, "words": words, "utterances": []}


def slate_transcript():
    spec = [
        (3.0, "S1", "Okay uh take one."), (6.0, "S2", "We're recording."), (9.0, "S2", "Can you hear me okay?"),
        (13.0, "S1", "Yeah sounds good."), (17.0, "S1", "Welcome to the show everybody, today we are talking about building things that last."),
        (60.0, "S1", "And so the first thing I would say is that the whole point of a podcast is conversation."),
        (400.0, "S2", "That is a very different thing and I think we should say so."),
        (1000.0, "S1", "So that is the end of what I wanted to say about this topic today."),
        (1440.0, "S1", "Thank you for listening, see you next time."),
        (1446.0, "S2", "Okay stop recording."), (1449.0, "S1", "Cut."),
    ]
    t = build_words(spec)
    return t


def make_ep(tmp_path, transcript, hints=None):
    ep = Episode(tmp_path / "ep")
    ep.create(title="t")
    write_json(ep.artifact("transcript.json"), transcript)
    if hints:
        m = read_json(ep.meta_path)
        m["trim_hints"] = hints
        write_json(ep.meta_path, m)
    return ep


def free(words, t):
    return not any(w["start"] + 1e-6 < t < w["end"] - 1e-6 for w in words)


def test_policy_loads_and_resolves(tmp_path):
    pol = trim.load_policy()
    assert pol["scan_minutes"] == 6 and "take one" in pol["slate"] and "welcome to" in pol["welcome"]["strong"]
    ep = Episode(tmp_path / "e")
    ep.create()
    (ep.root / "policy").mkdir()
    (ep.root / "policy/trim.yaml").write_text("scan_minutes: 3\nslate: [clapper]\n")
    p = trim.resolve_policy(ep, home=tmp_path / "nohome")
    assert p["scan_minutes"] == 3 and p["slate"] == ["clapper"] and p["pause_ms"] == 250


def test_suggestion_finds_slate_welcome_and_signoff_with_evidence(tmp_path):
    t = slate_transcript()
    ep = make_ep(tmp_path, t)
    before = (ep.meta_path.read_text(), sorted(p.name for p in ep.work.iterdir()))
    s = trim.suggest(ep)
    head, tail = s["head"], s["tail"]
    kinds = {(h["kind"], h["phrase"]) for h in head["evidence"]}
    assert ("slate", "take one") in kinds and ("slate", "we're recording") in kinds and ("slate", "can you hear me") in kinds
    assert ("welcome", "welcome to") in kinds
    assert all(0 <= h["e0"] < h["e1"] and h["text"] and h["speaker"] for h in head["evidence"])
    # the real start is the welcome sentence, placed in the pause before it
    welcome = next(w for w in t["words"] if w["w"] == "Welcome")
    prev = max(w["end"] for w in t["words"] if w["end"] <= welcome["start"])
    assert prev <= head["proposed_in"] <= welcome["start"] and welcome["start"] - head["proposed_in"] <= 0.3 + 1e-6
    assert free(t["words"], head["proposed_in"]) and head["confidence"] >= 0.8 and "welcome" in head["reason"]
    # the sign-off: out sits just after "next time."
    last = next(w for w in t["words"] if w["w"] == "time.")
    assert last["end"] <= tail["proposed_out"] <= last["end"] + 0.6 + 1e-6 and free(t["words"], tail["proposed_out"])
    assert any(h["phrase"] == "stop recording" for h in tail["evidence"]) and any(h["kind"] == "signoff" for h in tail["evidence"])
    # nothing was applied or changed: episode.json untouched, no trim recorded
    assert (ep.meta_path.read_text(), sorted(p.name for p in ep.work.iterdir() if p.name != "trim_suggestions.json")) == (before[0], [n for n in before[1]])
    assert "trim" not in read_json(ep.meta_path) and s["applied"] is False


def test_slate_without_a_welcome_proposes_the_first_real_sentence(tmp_path):
    spec = [(2.0, "S1", "Take one."), (4.0, "S2", "We're recording."), (8.0, "S2", "Yep, move your mic a bit."), (12.0, "S1", "Okay."),
            (15.0, "S1", "So I think you often use this phrase that we are starting in the middle of things."),
            (200.0, "S1", "More talk here about the thing we came to talk about today.")]
    t = build_words(spec)
    ep = make_ep(tmp_path, t)
    h = trim.suggest(ep)["head"]
    first = next(w for w in t["words"] if w["w"] == "So")
    assert h["proposed_in"] == pytest.approx(first["start"] - 0.3, abs=0.01) and 0.4 <= h["confidence"] <= 0.7
    assert "last slate phrase" in h["reason"]


def test_weak_welcome_inside_the_chatter_is_not_the_start(tmp_path):
    spec = [(2.0, "S1", "We're recording."), (5.0, "S2", "Welcome."), (6.5, "S2", "That's not going to be how it starts."),
            (10.0, "S1", "Can you hear me?"), (14.0, "S1", "I like this plan and I think the plan is sound, so let us begin the conversation now.")]
    t = build_words(spec)
    h = trim.suggest(make_ep(tmp_path, t))["head"]
    assert any(e["kind"] == "welcome_weak" for e in h["evidence"])
    start = next(w for w in t["words"] if w["w"] == "I")
    assert h["proposed_in"] >= start["start"] - 0.31


def test_nothing_to_find_proposes_nothing(tmp_path):
    t = build_words([(5.0, "S1", "So here is a long and unremarkable sentence about nothing in particular at all."), (900.0, "S2", "And another one near the end of the recording.")])
    s = trim.suggest(make_ep(tmp_path, t))
    assert s["head"]["proposed_in"] is None and s["tail"]["proposed_out"] is None and s["head"]["evidence"] == []


def test_episode_hints_extend_the_phrase_lists(tmp_path):
    t = build_words([(3.0, "S1", "Okay."), (8.0, "S1", "This is The Long Table, the show where we talk and we listen to each other."),
                     (1400.0, "S1", "Until the next one, keep your eyes open and your doors unlocked.")])
    plain = trim.suggest(make_ep(tmp_path / "a", t))
    assert plain["head"]["proposed_in"] is None
    ep = make_ep(tmp_path / "b", t, {"welcome": ["this is the long table"], "signoff": ["until the next one"]})
    s = trim.suggest(ep)
    sent = next(w for w in t["words"] if w["w"] == "This")
    assert s["head"]["proposed_in"] == pytest.approx(sent["start"] - 0.3, abs=0.01)
    assert s["tail"]["proposed_out"] is not None and s["tail"]["proposed_out"] > 1400.0


def test_only_the_scanned_minutes_are_searched(tmp_path):
    t = build_words([(3.0, "S1", "So here is where the recording really begins, with a long sentence about nothing at all."),
                     (700.0, "S1", "Welcome to the show, which is a phrase that appears much too late to be the start."), (1000.0, "S1", "A middle sentence that is long enough to be a sentence.")])
    assert trim.suggest(make_ep(tmp_path, t))["head"]["evidence"] == []


def test_text_report_has_evidence_lines_and_never_applies(tmp_path, capsys):
    ep = make_ep(tmp_path, slate_transcript())
    assert cli.main(["trim", "suggest", str(ep.root)]) == 0
    out = capsys.readouterr().out
    assert "take one" in out and "proposed in" in out and "proposed out" in out and "NOT applied" in out and "ave trim set" in out
    assert "trim" not in read_json(ep.meta_path)
    assert cli.main(["trim", "suggest", str(ep.root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["applied"] is False


# ---- trim set + assemble + resume ------------------------------------------------------------------------------------------

def test_trim_set_records_and_logs_and_validates(tmp_path, capsys):
    ep = make_ep(tmp_path, slate_transcript())
    cli.main(["progress", str(ep.root)])
    capsys.readouterr()
    assert cli.main(["trim", "set", str(ep.root), "main", "--in", "16.7", "--out", "1444.0"]) == 0
    r = json.loads(capsys.readouterr().out)
    t = read_json(ep.meta_path)["trim"]["main"]
    assert t["in"] == 16.7 and t["out"] == 1444.0 and t["by"] == "human" and t["at"] and r["trim"]["main"] == t
    assert "trim main" in (ep.root / "PROGRESS.md").read_text()
    with pytest.raises(SystemExit, match="in must be before out"):
        cli.main(["trim", "set", str(ep.root), "main", "--in", "50", "--out", "40"])
    with pytest.raises(SystemExit, match="give --in and/or --out"):
        cli.main(["trim", "set", str(ep.root), "main"])
    with pytest.raises(SystemExit, match="outside"):
        cli.main(["trim", "set", str(ep.root), "main", "--in", "-5"])
    # in alone keeps the recorded out
    cli.main(["trim", "set", str(ep.root), "main", "--in", "17.0"])
    capsys.readouterr()
    assert read_json(ep.meta_path)["trim"]["main"]["out"] == 1444.0


def test_assemble_honours_the_trim_and_validates(synth_episode):
    ep = synth_episode
    full = editdoc.load_latest(ep, "assembly")
    m = read_json(ep.meta_path)
    m["trim"] = {"main": {"in": 2.0, "out": 25.0, "by": "human", "at": "now"}}
    write_json(ep.meta_path, m)
    cutplan.build_cutlist(ep)
    doc = assemble.build(ep, (640, 360))
    assert editdoc.validate(doc) == []
    assert doc["duration"] == pytest.approx(23.0, abs=0.01) and full["duration"] > 29
    assert doc["trim"]["in"] == 2.0 and doc["trim"]["out"] == 25.0
    v, d = doc["tracks"]["video"], doc["tracks"]["dialogue"]
    assert v[0]["e0"] == pytest.approx(2.0) and d[0]["e0"] == pytest.approx(2.0) and d[0]["o0"] == 0.0
    assert v[-1]["o1"] == pytest.approx(23.0, abs=0.01) and d[-1]["o1"] == pytest.approx(23.0, abs=0.01)
    assert all(abs(s["e0"] - (2.0 + s["o0"])) < 0.01 for s in v)               # output time == episode time minus the trim
    assert [x for x in doc["excluded"] if x[0] == 0.0] and doc["excluded"][-1][1] >= 29.0
    # without a trim, nothing changes
    m["trim"] = {}
    write_json(ep.meta_path, m)
    assert assemble.build(ep, (640, 360))["duration"] == pytest.approx(full["duration"], abs=0.01)
    # a trim that leaves nothing is an error that says what to do
    m["trim"] = {"main": {"in": 100.0, "out": 120.0}}
    write_json(ep.meta_path, m)
    with pytest.raises(SystemExit, match="ave trim"):
        assemble.build(ep, (640, 360))


def test_assemble_trims_one_session_of_a_multisession_episode(ms_copy):
    ep = ms_copy("map")
    cutplan.build_cutlist(ep, session="s1")
    cutplan.build_cutlist(ep, session="s2")
    base = assemble.build(ep, (320, 180), session="s1")
    lo = base["tracks"]["dialogue"][0]["e0"]
    m = read_json(ep.meta_path)
    m["trim"] = {"s1": {"in": lo + 10.0, "out": lo + 60.0, "by": "human", "at": "now"}}
    write_json(ep.meta_path, m)
    doc = assemble.build(ep, (320, 180), session="s1")
    assert editdoc.validate(doc) == [] and doc["duration"] == pytest.approx(50.0, abs=0.05) and doc["session"] == "s1"
    assert doc["tracks"]["dialogue"][0]["e0"] == pytest.approx(lo + 10.0)
    other = assemble.build(ep, (320, 180), session="s2")                     # s2 has no trim of its own
    assert other["duration"] == pytest.approx(80.0, abs=1.0) and "trim" not in other
    comb = assemble.build_combined(ep, (320, 180))
    assert editdoc.validate(comb) == [] and comb["duration"] == pytest.approx(other["duration"] + 50.0, abs=1.0)


def test_resume_shows_an_unapproved_suggestion_as_an_open_question(tmp_path, capsys):
    ep = make_ep(tmp_path, slate_transcript())
    cli.main(["progress", str(ep.root)])
    assert "trim" not in state.briefing(ep).lower().split("open questions")[-1]
    trim.suggest(ep)
    text = state.briefing(ep)
    assert "Open questions for the human" in text and "trim" in text.split("Open questions for the human")[-1].lower()
    assert "ave trim set" in text
    cli.main(["trim", "set", str(ep.root), "main", "--in", "16.7", "--out", "1444.0"])
    assert "ave trim set" not in state.briefing(ep)
    # a suggestion with nothing to propose is not a question
    ep2 = make_ep(tmp_path / "q", build_words([(5.0, "S1", "So here is a long and unremarkable sentence about nothing in particular at all.")]))
    trim.suggest(ep2)
    assert "ave trim set" not in state.briefing(ep2)


@pytest.mark.skipif(not REAL.exists(), reason="real episode transcript (external drive) not present")
def test_real_transcript_opening_and_end_of_session_one():
    import shutil
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        ep = Episode(Path(d) / "ep")
        ep.create(title="real")
        shutil.copy(REAL, ep.artifact("transcript.json"))
        shutil.copy(REAL.parent / "cutlist.s1.json", ep.artifact("cutlist.s1.json"))
        s = trim.suggest(ep, session="s1")
    assert s["span"][0] == pytest.approx(4.145, abs=0.01) and s["span"][1] == pytest.approx(7035.729, abs=0.01)
    ev = s["head"]["evidence"]
    welcome = [h for h in ev if h["kind"].startswith("welcome")]
    assert welcome and welcome[0]["e0"] < 180.0                                 # "Welcome. That's not gonna be how it starts."
    assert any(h["phrase"] == "take one" for h in ev) and any(h["phrase"] == "we're recording" for h in ev)
    assert s["head"]["proposed_in"] is not None and 150.0 < s["head"]["proposed_in"] < 240.0      # after the mic fiddling, near 189 s
    assert s["tail"]["proposed_out"] is not None and 6990.0 < s["tail"]["proposed_out"] < 7035.0     # "Okay. We're done... Um, let's stop."
