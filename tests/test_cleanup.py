"""Cleanup (T2): pure detection/planning logic, audio snapping, masking priority, and the whole command on the
synthetic episode (see tests/fixtures.py for the known layout: dead air 6-8 s and 16-17 s, "um" 13.0-13.4, "yeah" 12.2-12.6).
"""

import json
import random

import numpy as np
import pytest
import soundfile as sf

from ave import audio, cleanup, editdoc, state
from ave.editdoc import TimeMap
from ave.episode import Episode, read_json
from conftest import needs_ffmpeg

POL = cleanup.load_policy()


def W(text, start, end, spk="S1", conf=0.95):
    return {"w": text, "start": start, "end": end, "conf": conf, "speaker": spk}


def words_of(*ws):
    return cleanup.prepare_words({"words": list(ws)})


# ---- policy --------------------------------------------------------------------------------------------------------

def test_default_policy_is_valid_and_complete():
    p = cleanup.load_policy()
    cleanup.validate_policy(p)
    for key in ("pause", "fillers", "backchannels", "repeats", "false_starts", "nonlexical", "protect", "snap", "xfade", "masks"):
        assert key in p
    assert p["pause"]["floor_ms"] <= p["pause"]["target_ms"] <= p["pause"]["max_ms"]
    assert "yeah" in p["backchannels"]["keep"] and "um" in p["fillers"]["cut"]
    assert p["repeats"]["allow"].count(False) == 0 and "no" in p["repeats"]["allow"]    # YAML 1.1 "no" must stay a string


def test_policy_resolution_order_and_partial_merge(tmp_path):
    ep = Episode(tmp_path / "e")
    ep.create()
    home = tmp_path / "home"
    (home / ".config/ave").mkdir(parents=True)
    (home / ".config/ave/cleanup.yaml").write_text("pause: {target_ms: 400}\n")
    pol, src = cleanup.resolve_policy(ep, home=home)
    assert pol["pause"]["target_ms"] == 400 and pol["pause"]["max_ms"] == 700 and src.endswith(".config/ave/cleanup.yaml")
    (ep.root / "policy").mkdir()
    (ep.root / "policy/cleanup.yaml").write_text("pause: {target_ms: 450}\n")
    assert cleanup.resolve_policy(ep, home=home)[0]["pause"]["target_ms"] == 450
    explicit = tmp_path / "mine.yaml"
    explicit.write_text("pause: {target_ms: 500}\n")
    pol, src = cleanup.resolve_policy(ep, explicit, home=home)
    assert pol["pause"]["target_ms"] == 500 and src == str(explicit.resolve())
    assert cleanup.resolve_policy(Episode(tmp_path / "other"), home=tmp_path / "nohome")[0]["pause"]["target_ms"] == 350


def test_invalid_policy_is_rejected_with_the_broken_key(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("pause: {floor_ms: 900}\n")
    with pytest.raises(ValueError, match="floor_ms"):
        cleanup.load_policy(bad)


# ---- detection (no media) ------------------------------------------------------------------------------------------

def test_fillers_cut_but_backchannel_and_lone_like_kept():
    ws = words_of(W("so", 0, .3), W("um", .4, .7), W("I", .8, .9), W("like", 1.0, 1.2), W("it", 1.3, 1.4),
                  W("yeah", 5.0, 5.3, "S2"), W("ok", 5.4, 5.6, "S1"))
    c = cleanup.detect_candidates(ws, POL)
    cut = [x for x in c if x["action"] == "cut"]
    assert [ws[x["idx"][0]]["w"] for x in cut] == ["um"] and cut[0]["confidence"] > 0.8
    assert not [x for x in c if x["type"] == "review_phrase"]            # one "like" alone is not a tic
    assert all(x["reason"] for x in c)


def test_clustered_like_and_you_know_are_flagged_never_cut():
    ws = words_of(W("it", 0, .2), W("was", .3, .5), W("like", .6, .8), W("you", .9, 1.0), W("know", 1.05, 1.2), W("great", 1.3, 1.6))
    c = cleanup.detect_candidates(ws, POL)
    flagged = [x for x in c if x["type"] == "review_phrase"]
    assert len(flagged) == 2 and all(x["action"] == "flag" and x["review"] for x in flagged)
    assert not [x for x in c if x["action"] == "cut"]


def test_repeats_remove_the_earlier_copy_and_respect_allow_list_and_speakers():
    ws = words_of(W("we", 0, .2), W("need", .25, .5), W("to", .55, .7), W("we", .8, 1.0), W("need", 1.05, 1.3), W("to", 1.35, 1.5),
                  W("go", 1.6, 1.8), W("the", 2.0, 2.1), W("the", 2.2, 2.3), W("idea", 2.4, 2.8), W("very", 3.0, 3.2), W("very", 3.3, 3.5),
                  W("good", 3.6, 3.9), W("so", 4.0, 4.1, "S1"), W("so", 4.2, 4.3, "S2"))
    cuts = [x for x in cleanup.detect_candidates(ws, POL) if x["action"] == "cut"]
    texts = sorted(cleanup._text(ws, x["idx"]) for x in cuts)
    assert texts == ["the", "we need to"]            # "very very" is emphasis; "so"/"so" are two different speakers
    assert all(x["type"] == "repeat" for x in cuts)


def test_false_start_cut_when_speaker_restarts_and_flagged_otherwise():
    ws = words_of(W("I", 0, .1), W("think", .15, .4), W("we-", .45, .6), W("I", .8, .9), W("think", .95, 1.2), W("we", 1.25, 1.4), W("should", 1.45, 1.8),
                  W("go", 5.0, 5.2), W("to", 5.25, 5.3), W("th-", 5.35, 5.45), W("store", 5.6, 5.9))
    fs = [x for x in cleanup.detect_candidates(ws, POL) if x["type"] == "false_start"]
    by = {cleanup._text(ws, x["idx"]): x for x in fs}
    assert by["I think we-"]["action"] == "cut"
    assert by["go to th-"]["action"] == "flag" and by["go to th-"]["review"]      # no clean restart: a human decides


def test_overrides_veto_and_add_but_only_on_whole_words():
    ws = words_of(W("hello", 0, .4), W("um", .5, .8), W("there", .9, 1.3), W("big", 1.4, 1.6), W("world", 1.7, 2.0))
    base = cleanup.detect_candidates(ws, POL)
    assert base[0]["action"] == "cut"
    warn: list[str] = []
    out = cleanup.apply_overrides(base, ws, {"keep": [{"id": base[0]["id"], "reason": "intentional hesitation"}],
                                             "cut": [{"words": [3, 3], "type": "false_start", "reason": "llm: tangent", "confidence": 0.7},
                                                     {"words": [40, 41]}, {"e0": 1.65, "e1": 2.05, "text": "nope"}]}, warn)
    assert [c["action"] for c in out] == ["keep", "cut"] and out[1]["idx"] == [3] and "llm: tangent" in out[1]["reason"]
    assert len(warn) == 2
    out2 = cleanup.apply_overrides([], ws, {"cut": [{"e0": 1.35, "e1": 1.9}]}, [])      # time range -> the whole words inside it
    assert out2[0]["idx"] == [3, 4]


# ---- audio snapping -------------------------------------------------------------------------------------------------

def _signal(sr=16000):
    t = np.arange(sr * 4) / sr
    x = 0.4 * np.sin(2 * np.pi * 220 * t) * (((t < 1.0) | (t > 2.5)).astype(float))
    rng = np.random.default_rng(1)
    x += rng.normal(0, 0.003, len(t))
    x[int(1.7 * sr):int(1.75 * sr)] += rng.normal(0, 0.02, int(0.05 * sr))   # a small click inside the silence
    return x.astype(np.float32), sr


def test_valley_finds_the_quiet_spot_and_zero_crossing_lands_on_a_sign_change():
    x, sr = _signal()
    env = cleanup.Envelope(x, sr, offset=0.0)
    v = env.valley(desired=1.02, lo=0.9, hi=1.3, window_s=0.2, tol_db=3)
    assert v >= 1.0 and env.db_at(v) < env.speech_db - 20          # moved off the tone into the silence
    near_click = env.valley(desired=1.72, lo=1.6, hi=1.9, window_s=0.1, tol_db=3)
    assert not (1.705 < near_click < 1.745) and env.db_at(near_click) < env.floor_db + 6                            # avoids the click
    z = env.zero_cross(1.5, 1.4, 1.6, 0.003)
    k = int(round(z * sr))
    assert np.sign(x[k]) != np.sign(x[k + 1]) or abs(x[k]) < 0.01
    # the window limits are hard: a valley outside [lo, hi] is never returned
    assert 1.0 <= env.valley(1.01, 1.0, 1.05, 0.5, 3) <= 1.05


def test_envelope_respects_source_offset():
    x, sr = _signal()
    env = cleanup.Envelope(x, sr, offset=1.0)       # file started 1 s after episode zero
    assert env.db_at(1.5) > env.speech_db - 6 and env.db_at(3.0) < env.speech_db - 20     # silence at file 2.0 s == episode 3.0 s


# ---- planning: protect rules, floors, never inside a word --------------------------------------------------------

def _plan_cuts(ws, pol=POL, env=None):
    cands = cleanup.detect_candidates(ws, pol)
    return cleanup.build_cuts(ws, cands, env, pol)


def test_protected_pauses_are_kept_and_capped():
    # 2.5 s gaps: after a laughter marker, after "!", before "turns out"; the control gap at the end is shortened
    ws = words_of(W("[laughter]", 0, .5), W("ok", 3.0, 3.2), W("fine", 3.3, 3.5), W("amazing!", 3.6, 4.0),
                  W("then", 6.5, 6.7), W("so", 6.8, 7.0), W("turns", 9.5, 9.8), W("out", 9.9, 10.0), W("fine", 10.1, 10.3),
                  W("again", 14.0, 14.3))
    cuts, dec, _ = _plan_cuts(ws)
    dead = {round(d["e0"], 1): d for d in dec if d["type"] == "dead_air"}
    assert dead[0.5]["action"] == "keep" and "laughter" in dead[0.5]["reason"]
    assert dead[4.0]["action"] == "keep" and "beat" in dead[4.0]["reason"]
    assert dead[7.0]["action"] == "keep" and "punchline" in dead[7.0]["reason"]
    assert dead[10.3]["action"] == "shorten"
    assert len(cuts) == 1 and cuts[0]["kept_gap_s"] == pytest.approx(0.35, abs=1e-6)
    # a protected pause longer than protect.cap_ms is still capped
    ws2 = words_of(W("[laughter]", 0, .5), W("ok", 9.0, 9.2))
    cuts2, dec2, _ = _plan_cuts(ws2)
    assert cuts2[0]["kept_gap_s"] == pytest.approx(POL["protect"]["cap_ms"] / 1000, abs=1e-6)
    assert "protected" in [d for d in dec2 if d["type"] == "dead_air"][0]["reason"]


def test_never_cut_inside_a_word_and_floor_respected_property():
    """Random transcripts + random audio: no splice ever overlaps a kept word; no kept gap under the floor."""
    pol = cleanup.load_policy()
    floor = pol["pause"]["floor_ms"] / 1000
    for seed in range(60):
        rng = random.Random(seed)
        t, ws, spk = 0.0, [], "S1"
        for _ in range(60):
            t += rng.choice([0.03, 0.05, 0.1, 0.2, 0.5, 0.9, 1.6, 2.5])
            d = rng.uniform(0.1, 0.6)
            tok = rng.choice(["um", "uh", "the", "the", "we", "go", "now", "so-", "yeah", "thing"])
            if rng.random() < 0.15:
                spk = "S2" if spk == "S1" else "S1"
            ws.append(W(tok, round(t, 3), round(t + d, 3), spk))
            t += d
        sr = 16000
        n = int((t + 1) * sr)
        x = np.random.default_rng(seed).normal(0, 0.003, n)
        for w in ws:
            a, b = int(w["start"] * sr), int(w["end"] * sr)
            x[a:b] += 0.3 * np.sin(2 * np.pi * 200 * np.arange(b - a) / sr)
        env = cleanup.Envelope(x.astype(np.float32), sr, 0.0)
        words = cleanup.prepare_words({"words": ws})
        cands = cleanup.detect_candidates(words, pol)
        cuts, dec, _ = cleanup.build_cuts(words, cands, env, pol)
        removed = {i for c in cands if c["action"] == "cut" for i in c["idx"]}
        gone = {i for d in dec if d["action"] == "cut" for c in cands if c["id"] == d["id"] for i in c["idx"]}
        for k in cuts:
            assert k["e1"] > k["e0"]
            for w in words:
                if w["i"] in gone:
                    continue
                assert w["end"] <= k["e0"] + 1e-6 or w["start"] >= k["e1"] - 1e-6, (seed, k["id"], w)
            if all(d["type"] == "dead_air" for d in dec if d["id"] in k["decisions"]):
                assert k["kept_gap_s"] >= floor - 1e-6
        assert removed >= gone
        # cuts never overlap each other
        for a, b in zip(cuts, cuts[1:]):
            assert a["e1"] <= b["e0"] + 1e-9


def test_filler_amid_other_speakers_speech_is_not_cut():
    ws = words_of(W("so", 0, .3, "S1"), W("um", .35, .8, "S1"), W("right", .5, .9, "S2"), W("then", 1.0, 1.3, "S1"))
    cuts, dec, _ = _plan_cuts(ws)
    um = next(d for d in dec if d["text"] == "um")
    assert not cuts and um["action"] == "flag" and um["review"] and "another speaker" in um["reason"]


def test_nonlexical_burst_in_a_gap_is_flagged_and_not_cut_by_default():
    sr = 16000
    x = np.random.default_rng(0).normal(0, 0.003, sr * 6)
    x[int(1.0 * sr):int(1.4 * sr)] += 0.3 * np.sin(2 * np.pi * 150 * np.arange(int(0.4 * sr)) / sr)     # a missed "um" at 1.0-1.4
    for a, b in ((0.0, 0.5), (2.2, 3.0)):
        x[int(a * sr):int(b * sr)] += 0.3 * np.sin(2 * np.pi * 220 * np.arange(int(b * sr) - int(a * sr)) / sr)
    env = cleanup.Envelope(x.astype(np.float32), sr, 0.0)
    ws = words_of(W("one", 0, .5), W("two", 2.2, 2.6), W("three", 2.7, 3.0))
    cuts, dec, _ = _plan_cuts(ws, env=env)
    nl = [d for d in dec if d["type"] == "nonlexical"]
    assert len(nl) == 1 and nl[0]["e0"] == pytest.approx(1.0, abs=0.03)
    # the 1.7 s gap is shortened; if the burst falls inside the removed middle it is reported as removed with it, else flagged
    assert nl[0]["action"] in ("cut", "flag") and nl[0]["reason"]
    pol = cleanup.load_policy()
    pol["pause"]["max_ms"] = 5000
    pol["pause"]["target_ms"] = 400
    cuts2, dec2, _ = _plan_cuts(ws, pol, env)
    assert not cuts2 and [d for d in dec2 if d["type"] == "nonlexical"][0]["action"] == "flag"
    pol["nonlexical"]["auto_cut"] = True
    cuts3, dec3, _ = _plan_cuts(ws, pol, env)
    assert len(cuts3) == 1 and 0.9 < cuts3[0]["e0"] < 1.1 and 1.3 < cuts3[0]["e1"] < 1.5


# ---- masks (direct, with a hand-built plan) ---------------------------------------------------------------------------

def _mask_world(cut_ts, a_cov=(0.0, 100.0), wide=None, pol_over=None):
    pol = cleanup.load_policy()
    for k, v in (pol_over or {}).items():
        pol["masks"][k] = v
    src = {"A": {"file": "a.mp4", "kind": "video", "start_offset": 0.0, "role": "host"},
           "B": {"file": "b.mp4", "kind": "video", "start_offset": 0.0, "role": "guest"}}
    cov = {"A": a_cov, "B": (0.0, 100.0)}
    if wide:
        src["W"] = {"file": "w.mp4", "kind": "video", "start_offset": 0.0, "role": "wide"}
        cov["W"] = wide
    keeps, pos, cuts = [], 0.0, []
    for n, t in enumerate(cut_ts, 1):          # cuts are removals of 0.5 s at episode time t (so output t' = t minus earlier removals)
        keeps.append((pos, t))
        cuts.append({"id": f"k{n:03d}", "e0": t, "e1": t + 0.5})
        pos = t + 0.5
    keeps.append((pos, 40.0))
    tm = TimeMap(keeps)
    av = [{"id": "v1", "src": "B", "o0": 0, "o1": 40, "e0": 0, "crop": None, "zoom": 1.0, "reason": "speaker", "confidence": 1.0}]
    plan = cleanup._video_plan(av, tm)
    mk = cleanup._Masker(plan, tm, cuts, {"sources": src}, cov, pol)
    mk.run()
    return mk, cuts, tm


def test_cutaway_to_other_camera_when_it_covers_and_hold_allows():
    mk, cuts, tm = _mask_world([10.0])
    k = cuts[0]
    assert k["mask"]["type"] == "cutaway" and k["mask"]["camera"] == "A" and not k["review"]
    t = tm.to_output_snap(10.0)
    vid = cleanup._emit_video(mk.plan, tm)
    at = [s for s in vid if s["o0"] <= t + 0.001 < s["o1"]][0]
    assert at["src"] == "A" and abs(at["o0"] - t) < 1e-6 and 1.2 <= at["o1"] - at["o0"] <= 2.01
    ret = [s for s in vid if abs(s["o0"] - at["o1"]) < 1e-6][0]
    assert ret["src"] == "B" and abs(ret["e0"] - (10.5 + (at["o1"] - at["o0"]))) < 1e-6     # B resumes in step with the dialogue
    assert editdoc.validate({**_doc_stub(vid, tm)}) == []


def _doc_stub(vid, tm):
    doc = editdoc.new_doc("rough", 30, (640, 360), {"A": {"file": "a", "kind": "video", "start_offset": 0.0},
                                                   "B": {"file": "b", "kind": "video", "start_offset": 0.0}}, tm.duration)
    doc["tracks"]["video"] = vid
    doc["tracks"]["dialogue"] = [{"id": "d1", "src": "A", "o0": 0, "o1": tm.duration, "e0": 0}]
    return doc


def test_punch_in_when_other_camera_does_not_cover_the_moment():
    mk, cuts, tm = _mask_world([10.0], a_cov=(0.0, 9.0))
    assert cuts[0]["mask"]["type"] == "punch_in" and "covers" in cuts[0]["mask"]["reason"]
    z = cuts[0]["mask"]["zoom"]
    assert 1.10 <= z <= 1.20
    t = tm.to_output_snap(10.0)
    vid = cleanup._emit_video(mk.plan, tm)
    before = [s for s in vid if s["o0"] < t - 0.001 < s["o1"]][0]
    after = [s for s in vid if s["o0"] <= t + 0.001 < s["o1"]][0]
    assert before["zoom"] == 1.0 and after["zoom"] == z and after["src"] == before["src"] == "B"


def test_wide_is_used_after_other_camera_and_before_punch_in():
    mk, cuts, _ = _mask_world([10.0], a_cov=(0.0, 9.0), wide=(0.0, 100.0))
    assert cuts[0]["mask"]["type"] == "wide_cutaway" and cuts[0]["mask"]["camera"] == "W"
    mk, cuts, _ = _mask_world([10.0], wide=(0.0, 100.0))
    assert cuts[0]["mask"]["type"] == "cutaway" and cuts[0]["mask"]["camera"] == "A"      # other camera outranks the wide


def test_min_hold_blocks_cutaway_and_falls_back_to_punch_in():
    mk, cuts, _ = _mask_world([1.0])           # B has only been on screen 1.0 s < min_hold 1.2
    assert cuts[0]["mask"]["type"] == "punch_in" and "min_hold_s" in cuts[0]["mask"]["reason"]


def test_punch_ins_alternate_and_return_to_base_framing():
    """The legacy behaviour (zoom_min_dwell_s 0): every cut flips the framing. The default policy limits this, see test_zoom_dwell.py."""
    mk, cuts, tm = _mask_world([10.0, 14.0, 18.0], a_cov=(0.0, 0.5), pol_over={"zoom_min_dwell_s": 0})
    zs = [k["mask"]["zoom"] for k in cuts]
    assert zs[0] > 1.1 and zs[1] == 1.0 and zs[2] > 1.1 and zs[0] != zs[2]      # in, back out, in (next factor in the list)
    vid = cleanup._emit_video(mk.plan, tm)
    assert [s["zoom"] for s in vid] == [1.0, zs[0], 1.0, zs[2]]


def test_same_camera_cutaway_is_not_repeated_inside_the_window():
    mk, cuts, _ = _mask_world([10.0, 13.5])     # second cut 3.5 s after the first cutaway: inside repeat_window_s (4)
    assert cuts[0]["mask"]["type"] == "cutaway" and cuts[1]["mask"]["type"] == "punch_in"
    assert "repeat_window_s" in cuts[1]["mask"]["reason"]
    mk, cuts, _ = _mask_world([10.0, 13.5], pol_over={"repeat_window_s": 0.0})
    assert cuts[1]["mask"]["type"] == "cutaway"


def test_no_mask_available_means_crossfade_only_and_needs_review():
    mk, cuts, _ = _mask_world([10.0], a_cov=(0.0, 9.0), pol_over={"punch_in": False})
    assert cuts[0]["mask"]["type"] == "none" and cuts[0]["review"] is True


# ---- the command on the synthetic episode -----------------------------------------------------------------------------


def kept_ranges(doc):
    return [(s["e0"], s["e0"] + s["o1"] - s["o0"]) for s in doc["tracks"]["dialogue"]]


def removed_ranges(doc, total=30.0):
    out, pos = [], 0.0
    for a, b in kept_ranges(doc):
        if a - pos > 1e-3:
            out.append((pos, a))
        pos = b
    if total - pos > 1e-3:
        out.append((pos, total))
    return out


@pytest.fixture
def cleaned(synth_episode):
    report = cleanup.run(synth_episode)
    return synth_episode, report, editdoc.load_latest(synth_episode, "rough")


@needs_ffmpeg
def test_rough_cut_removes_the_expected_material_and_validates(cleaned):
    ep, report, doc = cleaned
    assert editdoc.validate(doc) == [] and doc["stage"] == "rough"
    rem = removed_ranges(doc)
    assert len(rem) == 3
    # dead air 6-8 s and 16-17 s shortened to the policy target (350 ms kept between the neighbouring words)
    (a0, a1), (u0, u1), (b0, b1) = rem
    assert 5.95 < a0 and a1 < 8.0 and (a0 - 5.95) + (8.0 - a1) == pytest.approx(0.35, abs=0.03)
    assert 15.95 < b0 and b1 < 17.0 and (b0 - 15.95) + (17.0 - b1) == pytest.approx(0.35, abs=0.03)
    # the "um" (13.0-13.4 in the fixture) is gone; "yeah" (12.2-12.6) is kept
    assert u0 == pytest.approx(13.0, abs=0.06) and u1 == pytest.approx(13.4, abs=0.06)
    assert not any(r0 < 12.55 and r1 > 12.2 for r0, r1 in rem)
    expected = 30.0 - ((2.05 - 0.35) + 0.35 + (1.05 - 0.35))
    assert doc["duration"] == pytest.approx(expected, abs=0.1)
    assert report["totals"]["output_duration_s"] == pytest.approx(doc["duration"], abs=1e-3)
    assert report["totals"]["fillers_cut"] == 1 and report["totals"]["dead_air_shortened"] == 2
    assert report["totals"]["time_removed_s"] == pytest.approx(30.0 - doc["duration"], abs=1e-3)
    # the video track covers exactly the same duration, gap free (validate checks), and dialogue keeps the master source
    assert doc["tracks"]["video"][-1]["o1"] == pytest.approx(doc["duration"], abs=1e-3)


@needs_ffmpeg
def test_no_cut_falls_inside_a_word(cleaned):
    ep, report, doc = cleaned
    words = read_json(ep.artifact("transcript.json"))["words"]
    removed_words = {d["text"] for d in report["decisions"] if d["action"] == "cut" and d["type"] == "filler"}
    for k in report["cuts"]:
        for w in words:
            if w["w"] in removed_words:
                continue
            assert w["end"] <= k["e0"] + 1e-6 or w["start"] >= k["e1"] - 1e-6, (k, w)


@needs_ffmpeg
def test_every_cut_has_a_mask_or_needs_review_and_every_decision_is_explained(cleaned):
    ep, report, _ = cleaned
    assert report["cuts"]
    for k in report["cuts"]:
        assert k["mask"]["type"] in ("camera_change", "cutaway", "wide_cutaway", "punch_in") or k["review"]
        assert k["mask"]["reason"] and 0 < k["confidence"] <= 1 and k["xfade_ms"] > 0
    for d in report["decisions"]:
        for key in ("id", "type", "e0", "e1", "action", "reason", "confidence", "mask", "review"):
            assert key in d
        assert d["reason"] and 0 <= d["confidence"] <= 1
    kinds = {k["mask"]["type"] for k in report["cuts"]}
    assert "camera_change" in kinds                       # the two speaker-change pauses are masked by the camera switch already
    um = next(d for d in report["decisions"] if d["text"] == "um")
    assert um["mask"]["type"] == "cutaway" and um["mask"]["camera"] == "A"      # same-speaker cut: hidden by cutting to the listener


@needs_ffmpeg
def test_crossfade_length_follows_context(cleaned):
    _, report, doc = cleaned
    by = {k["id"]: k for k in report["cuts"]}
    um = next(d for d in report["decisions"] if d["text"] == "um")["cut"]
    assert by[um]["context"] == "intra_speech" and 5 <= by[um]["xfade_ms"] <= 15
    for k in report["cuts"]:
        if k["id"] != um:
            assert k["context"] == "across_gap" and 20 <= k["xfade_ms"] <= 40
    # the segment after each cut carries the crossfade; the first segment never does
    assert doc["tracks"]["dialogue"][0]["xfade_ms"] == 0
    assert sorted(s["xfade_ms"] for s in doc["tracks"]["dialogue"][1:]) == sorted(k["xfade_ms"] for k in report["cuts"])


@needs_ffmpeg
def test_cuts_land_in_low_energy_and_at_zero_crossings(cleaned):
    ep, report, _ = cleaned
    wav = audio.analysis_wav_path(ep, "raw/camA.mp4")
    x, sr = sf.read(str(wav), dtype="float64")
    env = cleanup.Envelope(x.astype(np.float32), sr, 0.0)
    dead = [k for k in report["cuts"] if all(d.startswith("p") for d in k["decisions"])]
    assert len(dead) == 2
    for k in dead:
        for t in (k["e0"], k["e1"]):
            assert env.db_at(t) < env.speech_db - 25, "dead-air splice must sit in the silence, not on speech"
            lo, hi = max(t - 0.06, 0.0), t + 0.06
            window = env.db[env._k(lo):env._k(hi) + 1]
            assert env.db_at(t) <= window.min() + POL["snap"]["valley_tolerance_db"] + 1.0     # as quiet as the policy's tolerance allows
            i = int(round(t * sr))
            assert x[i - 1] * x[i] <= 0 or x[i] * x[i + 1] <= 0    # a sign change sits at the splice point
    assert all(k["snap"]["valley"] for k in dead)


@needs_ffmpeg
def test_um_cut_boundaries_stay_inside_the_gaps_around_it(cleaned):
    _, report, _ = cleaned
    um = next(k for k in report["cuts"] if any(d.startswith("c") for d in k["decisions"]))
    assert 12.95 - 1e-6 <= um["e0"] <= 13.0 + 1e-6 and 13.35 - 1e-6 <= um["e1"] <= 13.4 + 1e-6
    assert um["snap"]["valley"] is False and um["confidence"] < 0.9      # fixture audio is a continuous tone: honest about it


@needs_ffmpeg
def test_dialogue_and_video_map_through_the_timemap_consistently(cleaned):
    ep, report, doc = cleaned
    tm = TimeMap(kept_ranges(doc))
    assert tm.duration == pytest.approx(doc["duration"], abs=1e-3)
    for s in doc["tracks"]["video"]:      # a video segment's e0 must be where the dialogue is at that output time
        assert s["e0"] == pytest.approx(tm.to_episode(s["o0"] + 1e-6), abs=1e-3)   # nudge: o0 sits exactly on a keep boundary


@needs_ffmpeg
def test_um_cutaway_is_applied_in_the_rough_video_track(cleaned):
    _, report, doc = cleaned
    um_cut = next(k for k in report["cuts"] if k["mask"]["type"] == "cutaway")
    vids = doc["tracks"]["video"]
    at = next(s for s in vids if abs(s["o0"] - um_cut["mask"]["o0"]) < 1e-3)
    prev = vids[vids.index(at) - 1]
    assert prev["src"] == "B" and at["src"] == "A" and "masks cut" in at["reason"]
    nxt = vids[vids.index(at) + 1]
    assert nxt["src"] == "B" and nxt["confidence"] == 1.0


@needs_ffmpeg
def test_punch_in_replaces_cutaway_when_other_camera_has_no_footage_there(synth_episode):
    media = read_json(synth_episode.artifact("media.json"))
    for f in media["files"]:
        if f["file"] == "raw/camA.mp4":
            f["duration"] = 12.0                      # camera A stops before the um
    from ave.episode import write_json
    write_json(synth_episode.artifact("media.json"), media)
    report = cleanup.run(synth_episode)
    um = next(d for d in report["decisions"] if d["text"] == "um")
    assert um["mask"]["type"] == "punch_in" and 1.10 <= um["mask"]["zoom"] <= 1.20
    doc = editdoc.load_latest(synth_episode, "rough")
    zoomed = [s for s in doc["tracks"]["video"] if s["zoom"] > 1.0]
    assert len(zoomed) == 1 and zoomed[0]["src"] == "B" and editdoc.validate(doc) == []


@needs_ffmpeg
def test_running_twice_makes_v1_then_v2_and_never_touches_the_assembly(synth_episode):
    before = (synth_episode.root / "edits/assembly.v1.json").read_bytes()
    r1 = cleanup.run(synth_episode)
    r2 = cleanup.run(synth_episode)
    assert editdoc.list_versions(synth_episode, "rough") == [1, 2]
    assert r1["rough"]["version"] == 1 and r2["rough"]["version"] == 2
    d1, d2 = (editdoc.load(editdoc.path_for(synth_episode, "rough", v)) for v in (1, 2))
    assert d1["tracks"] == d2["tracks"]                       # idempotent: same assembly, same result
    assert (synth_episode.root / "edits/assembly.v1.json").read_bytes() == before
    assert read_json(synth_episode.artifact("cleanup.json"))["rough"]["version"] == 2


@needs_ffmpeg
def test_a_policy_override_changes_the_outcome(synth_episode, tmp_path):
    base = cleanup.run(synth_episode, dry_run=True)
    p = tmp_path / "policy.yaml"
    p.write_text("pause: {max_ms: 3000}\nfillers: {cut: [um, yeah]}\n")
    alt = cleanup.run(synth_episode, policy_path=p, dry_run=True)
    assert base["totals"]["dead_air_shortened"] == 2 and alt["totals"]["dead_air_shortened"] == 0
    assert any(d["text"] == "yeah" and d["action"] == "cut" for d in alt["decisions"])
    assert not any(d["text"] == "yeah" and d["action"] == "cut" for d in base["decisions"])
    assert alt["policy"]["resolved"]["pause"]["max_ms"] == 3000 and alt["policy"]["source"] == str(p.resolve())
    p2 = tmp_path / "t.yaml"
    p2.write_text("pause: {target_ms: 600, floor_ms: 300}\n")
    longer = cleanup.run(synth_episode, policy_path=p2, dry_run=True)
    assert longer["totals"]["output_duration_s"] > base["totals"]["output_duration_s"] + 0.4


@needs_ffmpeg
def test_policy_protect_rules_apply_in_the_command(synth_episode):
    t = read_json(synth_episode.artifact("transcript.json"))
    last = next(w for w in t["words"] if w["w"] == "last")
    last["w"] = "[laughter]"
    from ave.episode import write_json
    write_json(synth_episode.artifact("transcript.json"), t)
    r = cleanup.run(synth_episode, dry_run=True)
    p = next(d for d in r["decisions"] if d["type"] == "dead_air" and d["e0"] < 7)
    assert p["action"] == "keep" and "laughter" in p["reason"]
    assert r["totals"]["dead_air_shortened"] == 1


@needs_ffmpeg
def test_overrides_file_is_honored(synth_episode):
    from ave.episode import write_json
    words = read_json(synth_episode.artifact("transcript.json"))["words"]
    um_ti = next(i for i, w in enumerate(words) if w["w"] == "um")
    start_ti = next(i for i, w in enumerate(words) if w["w"] == "start")
    write_json(synth_episode.artifact("cleanup_overrides.json"), {
        "keep": [{"e0": 13.0, "e1": 13.4, "reason": "he is thinking out loud"}],
        "cut": [{"words": [start_ti - 1, start_ti], "type": "false_start", "reason": "llm: trailing tangent", "confidence": 0.7}]})
    r = cleanup.run(synth_episode, dry_run=True)
    um = next(d for d in r["decisions"] if d["text"] == "um")
    assert um["action"] == "keep" and "he is thinking out loud" in um["reason"]
    ov = next(d for d in r["decisions"] if d["id"].startswith("o"))
    assert ov["type"] == "false_start" and ov["text"] == "they start" and ov["action"] == "cut" and "llm: trailing tangent" in ov["reason"]
    assert um_ti >= 0


@needs_ffmpeg
def test_dry_run_changes_nothing_and_run_records_state(synth_episode):
    r = cleanup.run(synth_episode, dry_run=True)
    assert r["dry_run"] and r["rough"] is None
    assert editdoc.list_versions(synth_episode, "rough") == [] and not synth_episode.artifact("cleanup.json").exists()
    cleanup.run(synth_episode)
    saved = read_json(synth_episode.artifact("cleanup.json"))
    assert saved["policy"]["resolved"]["pause"]["target_ms"] == 350 and saved["policy"]["source"].endswith("cleanup.default.yaml")
    assert set(saved["totals"]) >= {"time_removed_s", "fillers_cut", "dead_air_shortened", "cuts", "output_duration_s"}
    pending = read_json(synth_episode.meta_path)["review"]["pending"]
    assert "rough cut ready" in pending and "rough.v1.json" in pending
    assert next(r for r in state.status(synth_episode)["stages"] if r["stage"] == "rough")["done"]


@needs_ffmpeg
def test_from_version_and_missing_prerequisites(synth_episode):
    with pytest.raises(SystemExit, match="assembly v7"):
        cleanup.run(synth_episode, from_version=7)
    assert cleanup.run(synth_episode, from_version=1, dry_run=True)["assembly"]["version"] == 1
    (synth_episode.root / "edits/assembly.v1.json").unlink()
    with pytest.raises(SystemExit, match="ave assemble"):
        cleanup.run(synth_episode)


@needs_ffmpeg
def test_explain_is_human_readable(cleaned):
    _, report, _ = cleaned
    text = cleanup.explain(report)
    assert "um" in text and "shortened to" in text and "mask:" in text and "conf" in text
    assert text.index("NEEDS A HUMAN LOOK") < text.index("All decisions:") if report["totals"]["flagged_for_review"] else True


@needs_ffmpeg
def test_cli_cleanup_and_explain(synth_episode, capsys):
    from ave import cli
    assert cli.main(["cleanup", str(synth_episode.root), "--dry-run"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True and out["totals"]["cuts"] == 3 and "saved" not in out
    assert cli.main(["cleanup", str(synth_episode.root)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["saved"].endswith("rough.v1.json") and out["cleanup_json"].endswith("cleanup.json")
    assert cli.main(["cleanup", "explain", str(synth_episode.root)]) == 0
    assert "Cleanup of" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["cleanup", "explain", str(synth_episode.root / "nope")])


def test_envelope_follows_clock_drift():
    """A burst at file time 1000 s of a camera 80 ppm slow sits at episode time 1000.08 s, not 1000.0 s."""
    sr = 1000
    x = np.zeros(1100 * sr, np.float32)
    x[1000 * sr:1000 * sr + 200] = 0.5                      # 0.2 s burst at file time 1000 s
    flat = cleanup.Envelope(x, sr, 0.0)
    slow = cleanup.Envelope(x, sr, 0.0, drift=80e-6)
    assert flat.db_at(1000.04) > slow.db_at(1000.04) + 20    # drift-aware: episode 1000.04 maps to file 999.96 s = silence
    assert slow.db_at(1000.25) > flat.db_at(1000.25) + 20    # the burst now lives at episode 1000.08..1000.28
