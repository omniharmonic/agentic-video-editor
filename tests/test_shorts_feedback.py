"""Creator feedback 2026-10-14 on the trial shorts: complete statements as a hard guarantee, cuts that never slice a word, a title
opening instead of the quote card, and the quote card as its own product (the quote reel). Synthetic episode, no private footage.

Synthetic transcript (all lowercase, no punctuation; timings in tests/fixtures.py):
  S1 0.5-6.0  "welcome to the show today we are talking about building things that last"   (12 words, 0.458 s each)
  S2 8.0-12.0 "thanks for having me it is great to be here with you"
"""

from conftest import needs_libass  # noqa: E402
import json
import subprocess

import pytest

from ave import clips as C
from ave import completeness, editdoc, ffutil, opening, progress, shorts
from ave.episode import read_json, write_json

from test_shorts import NO_FACE, _make_clip

pytestmark = pytest.mark.skipif(not __import__("shutil").which("ffmpeg"), reason="ffmpeg not installed")

SRC = {"stage": "assembly", "name": None, "version": 1, "fingerprint": "x"}


def words_of(ep):
    return completeness.transcript_words(ep)


def first(ep, word, after=0.0):
    """The first occurrence of `word` starting at or after `after` seconds."""
    return next(x for x in words_of(ep) if x["w"] == word and x["start"] >= after)


def make(ep, o0, o1, status="proposed", **over):
    """A clip cut from the assembly between o0 and o1 (output seconds), complete-flagged unless `over` says otherwise."""
    clip = _make_clip(ep, o0, o1, status=status)
    clip.update({"start_complete": True, "end_complete": True, "complete_statement": True, "source": dict(SRC),
                 "text": "thanks for having me it is great to be here with you", **over})
    write_json(ep.work / "clips.json", {"clips": [clip], "rubric": {}})
    return clip


def cfg_no_opening(ep, **extra):
    cfg = shorts.load_config(ep)
    cfg["opening"]["mode"] = "none"
    for k, v in extra.items():
        cfg["completeness"][k] = v
    return cfg


# ---- 1. complete statements are a hard guarantee --------------------------------------------------------------------

def test_incomplete_clip_is_refused_with_every_reason(synth_episode):
    ep = synth_episode
    clip = make(ep, 7.5, 12.6, start_complete=False, complete_statement=False, completeness_note="starts mid-sentence: no break before 'we'")
    with pytest.raises(SystemExit) as e:
        shorts.build_short(ep, clip, cfg_no_opening(ep), detector=NO_FACE)
    msg = str(e.value)
    assert "not a complete statement" in msg and "starts mid-sentence" in msg and "--allow-incomplete" in msg
    assert editdoc.list_versions(ep, "short", clip["slug"]) == []            # nothing was built


@pytest.mark.parametrize("field,why", [("end_complete", "ends mid-thought"), ("start_complete", "starts mid-sentence")])
def test_either_edge_incomplete_refuses(synth_episode, field, why):
    ep = synth_episode
    clip = make(ep, 7.5, 12.6, **{field: False, "complete_statement": False})
    with pytest.raises(SystemExit, match=why):
        shorts.build_short(ep, clip, cfg_no_opening(ep), detector=NO_FACE)


def test_clip_without_completeness_fields_or_stale_is_refused(synth_episode):
    ep = synth_episode
    clip = make(ep, 7.5, 12.6)
    old = {k: v for k, v in clip.items() if k not in ("start_complete", "end_complete", "complete_statement")}
    with pytest.raises(SystemExit, match="never assessed"):
        shorts.build_short(ep, old, cfg_no_opening(ep), detector=NO_FACE)
    stale = {**clip, "stale": True, "stale_reason": "picked from rough.s1 v2"}
    with pytest.raises(SystemExit, match="stale"):
        shorts.build_short(ep, stale, cfg_no_opening(ep), detector=NO_FACE)


def test_stored_flags_are_not_trusted_the_edges_are_re_judged_on_the_real_words(synth_episode):
    """The clip says complete, but it begins on 'we' in the middle of "...today we are talking about": refused by the audit."""
    ep = synth_episode
    clip = make(ep, round(first(ep, "we")["start"], 3), round(first(ep, "last")["end"], 3))
    with pytest.raises(SystemExit, match="edge audit"):
        shorts.build_short(ep, clip, cfg_no_opening(ep), detector=NO_FACE)
    # the same clip with the audit switched off is cut: the audit, not the flags, is what caught it
    r = shorts.build_short(ep, clip, cfg_no_opening(ep, audit=False), detector=NO_FACE)
    assert r["status"] == "built" and r["completeness"]["complete"] is True


def test_a_clip_that_ends_on_a_dangling_word_is_refused_by_the_audit(synth_episode):
    ep = synth_episode
    clip = make(ep, round(first(ep, "thanks")["start"], 3), round(first(ep, "to", 8.0)["end"], 3))     # "...it is great to"
    with pytest.raises(SystemExit, match="edge audit.*ends mid-thought|ends mid-thought"):
        shorts.build_short(ep, clip, cfg_no_opening(ep), detector=NO_FACE)


@needs_libass
def test_complete_clip_builds_and_the_sidecar_shows_completeness(synth_episode):
    ep = synth_episode
    w = {x["w"]: x for x in words_of(ep)}
    clip = make(ep, round(w["thanks"]["start"], 3), round(w["you"]["end"], 3))     # a whole utterance
    r = shorts.build_short(ep, clip, cfg_no_opening(ep), detector=NO_FACE)
    assert r["completeness"] == {"complete": True, "boundary": "clip", "override": False, "problems": []}
    out = shorts.export_short(ep, clip, ["youtube"], cfg_no_opening(ep))
    side = read_json(ep.root / out["exports"][0]["sidecar"])
    c = side["completeness"]
    assert c["complete"] and c["boundary"] == "clip" and not c["override"] and c["audit_ran"]
    assert c["audit"]["complete_statement"] and c["audit"]["first_words"].startswith("thanks for having me")
    assert side["edges"]["snapped"] and side["draft"] is True


@needs_libass
def test_override_is_explicit_logged_and_recorded(synth_episode, monkeypatch):
    ep = synth_episode
    clip = make(ep, 7.5, 12.6, start_complete=False, complete_statement=False)
    logged = []
    monkeypatch.setattr(progress, "auto_log", lambda *a, **k: logged.append((a, k)))
    r = shorts.build_short(ep, clip, cfg_no_opening(ep), detector=NO_FACE, allow_incomplete=True)
    assert r["completeness"]["override"] is True and r["completeness"]["complete"] is False and r["completeness"]["problems"]
    assert logged and "OVERRIDE --allow-incomplete" in logged[0][0][1] and clip["id"] in logged[0][0][1]
    doc = editdoc.load(ep.root / r["path"])
    assert doc["short"]["completeness"]["override"] is True
    out = shorts.export_short(ep, clip, ["youtube"], cfg_no_opening(ep))
    side = read_json(ep.root / out["exports"][0]["sidecar"])
    assert side["completeness"]["override"] is True and side["completeness"]["problems"]


def test_cli_has_the_override_flag_and_it_is_off_by_default(synth_episode, capsys):
    from ave import cli
    ep = synth_episode
    make(ep, 7.5, 12.6, start_complete=False, complete_statement=False)
    with pytest.raises(SystemExit) as e:
        cli.main(["shorts", "build", str(ep.root), "--clip", "synth-clip", "--allow-unapproved", "--opening", "none"])
    assert "not a complete statement" in str(e.value)
    assert cli.main(["shorts", "build", str(ep.root), "--clip", "synth-clip", "--allow-unapproved", "--opening", "none", "--allow-incomplete"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["completeness"]["override"] is True


def test_export_refuses_a_short_without_a_completeness_record_or_with_an_unrecorded_failure(synth_episode):
    ep = synth_episode
    w = {x["w"]: x for x in words_of(ep)}
    clip = make(ep, round(w["thanks"]["start"], 3), round(w["you"]["end"], 3))
    r = shorts.build_short(ep, clip, cfg_no_opening(ep), detector=NO_FACE)
    doc = editdoc.load(ep.root / r["path"])
    doc["short"].pop("completeness")                       # a short built before the gate existed
    editdoc.save_new_version(ep, doc, name=clip["slug"])
    with pytest.raises(SystemExit, match="before the complete-statement gate"):
        shorts.export_short(ep, clip, ["youtube"], cfg_no_opening(ep))
    doc["short"]["completeness"] = {"complete": False, "override": False, "problems": ["starts mid-sentence"]}
    editdoc.save_new_version(ep, doc, name=clip["slug"])
    with pytest.raises(SystemExit, match="no override was recorded"):
        shorts.export_short(ep, clip, ["youtube"], cfg_no_opening(ep))


def test_context_variant_is_cut_when_the_clip_is_incomplete_and_the_variant_is_complete(synth_episode, monkeypatch):
    ep = synth_episode
    w = {k: first(ep, k) for k in ("welcome", "we", "last")}
    base = editdoc.load_latest(ep, "assembly")
    v0, v1 = round(w["welcome"]["start"], 3), round(w["last"]["end"], 3)
    clip = make(ep, round(w["we"]["start"], 3), v1, start_complete=False, complete_statement=False,
                context_variant={"sentence_ids": ["s001"], "start": v0, "end": v1, "duration": v1 - v0, "added_text": "welcome to the show today",
                                 "note": "starts 1 sentence earlier so the opening has its antecedent"})
    clip.pop("edit")
    write_json(ep.work / "clips.json", {"clips": [clip], "rubric": {}})
    monkeypatch.setattr(C, "_source_docs", lambda ep_, rubric, chosen: {("assembly", None, 1): base})
    r = shorts.build_short(ep, clip, cfg_no_opening(ep), detector=NO_FACE)
    assert r["completeness"]["boundary"] == "context_variant" and r["completeness"]["complete"] and not r["completeness"]["override"]
    doc = editdoc.load(ep.root / r["path"])
    assert doc["short"]["clip_range"] == [v0, v1]
    assert doc["short"]["completeness"]["variant_of"]["added_text"] == "welcome to the show today"
    head = min(doc["tracks"]["dialogue"], key=lambda s: s["o0"])
    assert head["e0"] <= w["welcome"]["start"] + 1e-6 and head["e0"] > w["welcome"]["start"] - 0.2     # starts on "welcome"
    assert clip["start_complete"] is False                                   # the caller's clip was not mutated


# ---- 1b. the cut never slices a word --------------------------------------------------------------------------------

def _gap_ok(words, t):
    return completeness.slices_word(words, t) is None


def test_snap_moves_a_start_that_is_inside_a_word_into_the_gap_with_pad(synth_episode):
    ep = synth_episode
    words = words_of(ep)
    base = editdoc.load_latest(ep, "assembly")
    k = next(i for i, x in enumerate(words) if x["w"] == "are")
    inside = words[k]["start"] + 0.1                                          # 0.1 s into "are"
    end_inside = words[k + 3]["start"] + 0.3                                  # and the end inside "building"
    doc = editdoc.slice_doc(base, inside, end_inside, stage="select")
    assert completeness.slices_word(words, doc["tracks"]["dialogue"][0]["e0"]) is not None
    rep = completeness.snap_edges(doc, words, {"completeness": {"pad_start_s": 0.1, "pad_end_s": 0.18}})
    assert rep["snapped"] and rep["first_word"] == "are" and rep["last_word"] == words[k + 3]["w"]
    assert rep["start"]["was_slicing"] == "are" and rep["end"]["was_slicing"] == words[k + 3]["w"]
    sp = completeness._speech(doc)
    t0, t1 = sp[0]["e0"], sp[-1]["e0"] + sp[-1]["o1"] - sp[-1]["o0"]
    assert _gap_ok(words, t0) and _gap_ok(words, t1)
    assert words[k - 1]["end"] <= t0 <= words[k]["start"] + 1e-6                # between the previous word and "are"
    assert words[k + 3]["end"] - 1e-6 <= t1 <= words[k + 4]["start"] + 1e-6
    assert editdoc.validate(doc) == [] and abs(doc["duration"] - (t1 - t0)) < 1e-3


def test_snap_uses_the_pad_in_a_wide_gap_and_never_crosses_the_middle_of_a_narrow_one(synth_episode):
    ep = synth_episode
    words = words_of(ep)
    base = editdoc.load_latest(ep, "assembly")
    th = next(x for x in words if x["w"] == "thanks")              # after 2 s of dead air: wide gap before it
    last = next(x for x in words if x["w"] == "you")
    doc = editdoc.slice_doc(base, th["start"], last["end"], stage="select")
    rep = completeness.snap_edges(doc, words, {"completeness": {"pad_start_s": 0.1, "pad_end_s": 0.18}})
    assert rep["start"]["after"] == pytest.approx(th["start"] - 0.1, abs=1e-3)
    assert rep["end"]["after"] <= words[words.index(last) + 1]["start"]
    w = next(x for x in words if x["w"] == "are")
    i = words.index(w)
    doc2 = editdoc.slice_doc(base, w["start"], words[i + 2]["end"], stage="select")
    rep2 = completeness.snap_edges(doc2, words, {"completeness": {"pad_start_s": 0.5, "pad_end_s": 0.5}})
    assert rep2["start"]["after"] == pytest.approx((words[i - 1]["end"] + w["start"]) / 2, abs=1e-3)     # 0.05 s gap: the middle, not 0.5 s back
    assert editdoc.validate(doc2) == []


def test_snap_keeps_all_tracks_in_sync(synth_episode):
    ep = synth_episode
    words = words_of(ep)
    base = editdoc.load_latest(ep, "assembly")
    k = next(i for i, x in enumerate(words) if x["w"] == "we")
    doc = editdoc.slice_doc(base, words[k]["start"] + 0.1, words[k + 6]["end"] - 0.05, stage="select")
    before = {(n, s["id"]): s["e0"] - s["o0"] for n in ("video", "dialogue") for s in doc["tracks"][n]}
    rep = completeness.snap_edges(doc, words, None)
    ds = rep["start"]["delta"]
    for n in ("video", "dialogue"):                    # the whole doc moved by the same amount: picture and sound still agree
        for s in doc["tracks"][n]:
            assert s["e0"] - s["o0"] == pytest.approx(before[(n, s["id"])] + ds, abs=2e-3), (n, s["id"])
    assert doc["tracks"]["video"][0]["e0"] == pytest.approx(doc["tracks"]["dialogue"][0]["e0"], abs=1e-3)
    assert editdoc.validate(doc) == []


def test_built_short_starts_and_ends_in_word_gaps_and_the_report_says_so(synth_episode):
    ep = synth_episode
    words = words_of(ep)
    th = next(x for x in words if x["w"] == "thanks")
    you = next(x for x in words if x["w"] == "you")
    clip = make(ep, th["start"] + 0.1, you["end"] - 0.1)         # a cut that slices "thanks" and "you"
    r = shorts.build_short(ep, clip, cfg_no_opening(ep, audit=False), detector=NO_FACE)
    doc = editdoc.load(ep.root / r["path"])
    rep = completeness.edge_report(doc, words)
    assert rep["start"]["slices_word"] is None and rep["end"]["slices_word"] is None
    assert doc["short"]["edges"]["start"]["was_slicing"] == "thanks" and doc["short"]["edges"]["end"]["was_slicing"] == "you"
    assert editdoc.validate(doc) == []
    cues = (ep.root / doc["captions"]["srt"]).read_text()
    assert "thanks" in cues.lower() and "you" in cues.lower()           # the sliced words are inside the cut now, and captioned


# ---- 2. the opening is a title, never the quote ---------------------------------------------------------------------

def whole_utterance(ep, **over):
    words = words_of(ep)
    th, you = next(x for x in words if x["w"] == "thanks"), next(x for x in words if x["w"] == "you")
    return make(ep, round(th["start"], 3), round(you["end"], 3), **over)


def test_default_opening_is_a_title_overlay_over_the_first_two_seconds_with_no_extra_time(synth_episode):
    ep = synth_episode
    assert shorts.load_config(ep)["opening"]["mode"] == "title_overlay"
    clip = whole_utterance(ep)
    plain = shorts.build_short(ep, clip, cfg_no_opening(ep), detector=NO_FACE)
    r = shorts.build_short(ep, clip, detector=NO_FACE)
    doc, base = editdoc.load(ep.root / r["path"]), editdoc.load(ep.root / plain["path"])
    assert doc["duration"] == pytest.approx(base["duration"])                       # no extra time
    ov = [o for o in doc["tracks"]["overlays"] if o.get("template") == "short-title"]
    assert len(ov) == 1 and ov[0]["o0"] == pytest.approx(0.2) and ov[0]["o1"] == pytest.approx(2.2)
    assert ov[0]["props"]["mode"] == "overlay" and ov[0]["props"]["title"] == "Synthetic clip" and ov[0]["props"]["show"]
    assert not [o for o in doc["tracks"]["overlays"] if o.get("template") == "quote-card"]
    assert editdoc.validate(doc) == []
    assert r["opening"]["mode"] == "title_overlay" and r["opening"]["title_source"] == "clip.title"


def test_title_card_adds_about_a_second_and_a_half_holds_the_first_frame_and_silences_the_lead(synth_episode):
    ep = synth_episode
    clip = whole_utterance(ep)
    plain = shorts.build_short(ep, clip, cfg_no_opening(ep), detector=NO_FACE)
    r = shorts.build_short(ep, clip, detector=NO_FACE, opening_mode="title_card", title="Why systems matter")
    doc, base = editdoc.load(ep.root / r["path"]), editdoc.load(ep.root / plain["path"])
    lead = doc["duration"] - base["duration"]
    assert 1.0 <= lead <= 1.5 and r["opening"]["added_s"] == pytest.approx(lead)
    ov = [o for o in doc["tracks"]["overlays"] if o.get("template") == "short-title"][0]
    assert ov["o0"] == 0.0 and ov["o1"] == pytest.approx(lead) and ov["props"]["mode"] == "card" and ov["props"]["title"] == "Why systems matter"
    d = sorted(doc["tracks"]["dialogue"], key=lambda s: s["o0"])
    assert d[0]["id"] == "dlead" and d[0]["gain_db"] <= -100 and d[0]["o1"] == pytest.approx(lead)
    assert d[1]["e0"] == pytest.approx(base["tracks"]["dialogue"][0]["e0"], abs=1e-3)       # the speech itself is untouched, just later
    assert min(x["o0"] for x in doc["tracks"]["video"]) == 0.0
    assert editdoc.validate(doc) == []
    first_cue = (ep.root / doc["captions"]["srt"]).read_text().split("-->")[0].split("\n")[-1].strip()
    h, m, s = first_cue.replace(",", ".").split(":")
    assert float(h) * 3600 + float(m) * 60 + float(s) >= lead - 0.01                 # captions start when the clip does, not under the card


def test_title_card_never_captions_or_plays_the_words_before_the_clip(synth_episode):
    """The lead-in is the master audio just BEFORE the clip, muted. Those words must not become captions."""
    ep = synth_episode
    a, last = first(ep, "absolutely"), first(ep, "about", 25.0)
    clip = make(ep, round(a["start"], 3), round(last["end"], 3))
    assert any(w["end"] > a["start"] - 1.4 and w["start"] < a["start"] for w in words_of(ep))           # speech does sit in the lead region
    r = shorts.build_short(ep, clip, cfg_no_opening(ep, audit=False), detector=NO_FACE, opening_mode="title_card", title="A quiet start")
    doc = editdoc.load(ep.root / r["path"])
    text = (ep.root / doc["captions"]["srt"]).read_text().lower()
    assert "absolutely" in text and "forward" not in text and "carry" not in text
    lead = [s for s in doc["tracks"]["dialogue"] if s["id"] == "dlead"][0]
    assert lead["fill"] == "muted" and lead["gain_db"] <= -100


@needs_libass
def test_title_card_audio_is_silent_until_the_clip_starts(synth_episode):
    ep = synth_episode
    clip = whole_utterance(ep)
    r = shorts.build_short(ep, clip, detector=NO_FACE, opening_mode="title_card", title="Why systems matter")
    out = shorts.export_short(ep, clip, ["youtube"])
    mp4 = ep.root / out["exports"][0]["video"]
    lead = r["opening"]["added_s"]
    vol = subprocess.run(["ffmpeg", "-v", "info", "-t", str(lead - 0.05), "-i", str(mp4), "-af", "volumedetect", "-vn", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    mx = float(vol.split("max_volume:")[1].split("dB")[0])
    assert mx < -60, vol
    assert ffutil.probe(mp4)["duration"] == pytest.approx(read_json(ep.root / out["exports"][0]["sidecar"])["duration"], abs=0.05)
    side = read_json(ep.root / out["exports"][0]["sidecar"])
    assert side["opening"]["mode"] == "title_card" and side["opening"]["draft"] is True and side["opening"]["title"] == "Why systems matter"


def test_opening_none_adds_nothing(synth_episode):
    ep = synth_episode
    r = shorts.build_short(ep, whole_utterance(ep), detector=NO_FACE, opening_mode="none")
    doc = editdoc.load(ep.root / r["path"])
    assert not doc["tracks"].get("overlays") and r["opening"]["mode"] == "none"


def test_unknown_opening_is_refused(synth_episode):
    ep = synth_episode
    with pytest.raises(SystemExit, match="unknown opening"):
        shorts.build_short(ep, whole_utterance(ep), detector=NO_FACE, opening_mode="quote_card")


def test_a_title_that_is_the_speakers_own_words_is_refused_and_no_opening_is_added(synth_episode):
    """The creator's complaint: the quote on screen before/while it is spoken. A topic title passes; the quote does not."""
    ep = synth_episode
    clip = whole_utterance(ep)
    r = shorts.build_short(ep, clip, detector=NO_FACE, title="Thanks for having me it is great to be here")
    assert r["opening"]["mode"] == "none" and any("speaker's own words" in p for p in r["opening"]["problems"])
    doc = editdoc.load(ep.root / r["path"])
    assert not doc["tracks"].get("overlays") and any("no title opening" in x for x in doc["short"]["needs_review"])
    ok = shorts.build_short(ep, clip, detector=NO_FACE, title="A short hello", force=True)
    assert ok["opening"]["mode"] == "title_overlay" and ok["opening"]["title"] == "A short hello"


def test_the_clip_title_is_not_used_when_it_is_the_quote_but_an_explicit_title_still_is(synth_episode):
    ep = synth_episode
    clip = whole_utterance(ep)
    clip["title"] = "Thanks for having me, it is great to be here"
    got = opening.resolve_title(ep, clip, opening.config(shorts.load_config(ep)))
    assert got["problems"] and got["source"] == "clip.title"
    good = opening.resolve_title(ep, clip, opening.config(shorts.load_config(ep)), explicit="On being invited")
    assert not good["problems"] and good["source"] == "--title"


@pytest.mark.parametrize("bad,why", [("Unlock the power of systems", "unlock"), ("Don't miss this!", "exclamation"), ("A deep dive into soil", "deep dive"),
                                     ("Is this the end of systems?", "question"), ("“We tend it”", "quotation"), ("x" * 60, "characters")])
def test_titles_pass_the_voice_critic(synth_episode, bad, why):
    ep = synth_episode
    problems = opening.critique(ep, bad, "something else entirely", opening.config(shorts.load_config(ep)))
    assert problems and any(why in p.lower() for p in problems)
    assert opening.critique(ep, "Why we started this table", "building things that last", opening.config(shorts.load_config(ep))) == []


def test_title_comes_from_titles_json_before_the_clip_title_and_is_editable(synth_episode):
    ep = synth_episode
    clip = whole_utterance(ep)
    p = opening.titles_file(ep)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({clip["slug"]: "Meeting each other"}))
    r = shorts.build_short(ep, clip, detector=NO_FACE)
    assert r["opening"]["title"] == "Meeting each other" and r["opening"]["title_source"].endswith(f"[{clip['slug']}]")
    p.write_text(json.dumps({clip["slug"]: "Starting over"}))
    r2 = shorts.build_short(ep, clip, detector=NO_FACE)                    # a changed title is a new version, not 'unchanged'
    assert r2["status"] == "built" and r2["version"] == r["version"] + 1 and r2["opening"]["title"] == "Starting over"


def test_show_name_comes_from_episode_json_or_the_title_without_its_number(synth_episode):
    ep = synth_episode
    meta = read_json(ep.meta_path)
    meta["title"] = "The Long Table 00"
    meta.pop("show", None)
    write_json(ep.meta_path, meta)
    assert opening.show_name(ep, {}) == "The Long Table"
    meta["show"] = {"name": "The Open Table"}
    write_json(ep.meta_path, meta)
    assert opening.show_name(ep, {}) == "The Open Table"


def test_a_quote_card_overlay_never_reaches_a_short(synth_episode):
    ep = synth_episode
    clip = whole_utterance(ep)
    r = shorts.build_short(ep, clip, detector=NO_FACE, opening_mode="none")
    doc = editdoc.load(ep.root / r["path"])
    doc["tracks"].setdefault("overlays", []).append({"id": "m1", "file": "work/motion/x.mov", "o0": 0.0, "o1": 2.0, "x": 0, "y": 0, "w": 1080, "h": 1920,
                                                     "layer": 20, "kind": "motion", "alpha": True, "template": "quote-card", "props": {"quote": "x"}})
    editdoc.save_new_version(ep, doc, name=clip["slug"])
    with pytest.raises(SystemExit, match="quote-card"):
        shorts.export_short(ep, clip, ["youtube"], cfg_no_opening(ep))
    assert opening.refuse_quote_cards(doc)


def test_shorts_build_never_prepends_a_quote_card_by_any_route(synth_episode):
    ep = synth_episode
    clip = whole_utterance(ep)
    for mode in opening.MODES:
        r = shorts.build_short(ep, clip, detector=NO_FACE, opening_mode=mode, force=True)
        doc = editdoc.load(ep.root / r["path"])
        assert all(o.get("template") != "quote-card" for o in doc["tracks"].get("overlays") or []), mode


@pytest.mark.real_motion
@needs_libass
def test_the_real_title_overlay_renders_and_is_composited_in_the_export(synth_episode):
    pytest.importorskip("playwright")
    ep = synth_episode
    clip = whole_utterance(ep)
    shorts.build_short(ep, clip, detector=NO_FACE, title="Why systems matter")
    out = shorts.export_short(ep, clip, ["youtube"])
    mp4 = ep.root / out["exports"][0]["video"]
    def red(t):                                            # the speaker's camera is solid red; the title panel is the brand ink
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(mp4), "-frames:v", "1", "-vf", "crop=40:40:300:1000,scale=1:1",
                            "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
        return r[0]
    assert red(1.2) < 150 and red(3.6) > 220            # the title is up at 1.2 s and gone by 3.6 s
    ov = ep.root / [o for o in editdoc.load_latest(ep, "short", clip["slug"])["tracks"]["overlays"] if o["template"] == "short-title"][0]["file"]
    assert ov.exists() and ffutil.probe(ov)["w"] == 1080
