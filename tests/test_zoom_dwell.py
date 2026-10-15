"""Zoom-state limiter (no in-out-in jumpiness) and the cut-inside-a-word check. Synthetic fixtures only.

Real-episode origin (first real episode, session 2): the rough cut had 43 same-camera zoom changes, 19 of them closer than 8 s to the
previous one and 4 in-out-in runs inside 8 s; and a cut at the ASR end of "right." sliced its decaying tail."""

import copy

import numpy as np
import pytest

from ave import cleanup, cutwords, zoomstate
from ave.editdoc import TimeMap
from conftest import needs_ffmpeg
from test_cleanup import W, _mask_world
from test_pacing import monologue, sources, video as long_video

POL = cleanup.load_policy()


def seg(i, src, o0, o1, zoom=1.0, e0=None):
    return {"id": f"v{i}", "src": src, "o0": o0, "o1": o1, "e0": o0 if e0 is None else e0, "crop": None, "zoom": zoom}


# ---- the validator -------------------------------------------------------------------------------------------------------

def test_validator_reports_in_out_in_and_short_dwell():
    vid = [seg(1, "A", 0, 10), seg(2, "A", 10, 11, 1.18), seg(3, "A", 11, 12), seg(4, "A", 12, 20, 1.12)]
    rep = zoomstate.validate(vid, 8.0, 0.08)
    assert rep["zoom_changes"] == 3 and rep["ins"] == 2 and rep["outs"] == 1
    assert rep["in_out_in"] == 1 and rep["short_dwell"] == 2 and not rep["ok"]
    assert {v["kind"] for v in rep["violations"]} == {"short_dwell", "in_out_in"}
    assert any("in 10.0s, out 11.0s, in 12.0s" in line for line in zoomstate.format_violations(rep))


def test_a_slow_in_hold_out_is_natural_and_a_camera_change_is_not_a_zoom_change():
    slow = [seg(1, "A", 0, 10), seg(2, "A", 10, 25, 1.12), seg(3, "A", 25, 40), seg(4, "A", 40, 55, 1.18)]
    rep = zoomstate.validate(slow, 8.0, 0.08)
    assert rep["ok"] and rep["zoom_changes"] == 3 and rep["min_interval_s"] == 15.0
    # punched 1.12 on A, cut to B (base), back to A at base: nothing zoomed on screen, no zoom change
    cams = [seg(1, "A", 0, 5, 1.12), seg(2, "B", 5, 6), seg(3, "A", 6, 9, 1.0)]
    assert zoomstate.validate(cams, 8.0, 0.08)["zoom_changes"] == 0


def test_validator_flags_a_shot_punched_in_deeper_without_coming_out():
    vid = [seg(1, "A", 0, 10), seg(2, "A", 10, 30, 1.10), seg(3, "A", 30, 50, 1.20)]
    rep = zoomstate.validate(vid, 8.0, 0.08, max_ins=1)
    assert rep["multiple_ins"] == 1 and rep["short_dwell"] == 0


def test_allows_blocks_inside_the_dwell_and_permits_after_it():
    vid = [seg(1, "A", 0, 10), seg(2, "A", 10, 40, 1.12)]
    assert not zoomstate.allows(vid, 14.0, "A", "out", 8.0, 0.08)[0]
    assert "4.0s ago" in zoomstate.allows(vid, 14.0, "A", "out", 8.0, 0.08)[1]
    assert zoomstate.allows(vid, 18.0, "A", "out", 8.0, 0.08)[0]
    assert not zoomstate.allows(vid, 30.0, "A", "in", 8.0, 0.08)[0]            # already punched in: max one level per shot
    assert zoomstate.allows(vid, 14.0, "A", "out", 0.0, 0.08)[0]               # dwell 0 = legacy, no rule
    assert zoomstate.allows(vid, 14.0, "B", "in", 8.0, 0.08)[0]                # another camera has its own clock


# ---- masks ---------------------------------------------------------------------------------------------------------------

def test_masks_never_go_in_out_in_inside_the_dwell_and_say_why():
    """Cuts at output ~10, 13.5, 17, 22.5, 26. Only A is missing, so every cut would be a punch-in: in, (blocked), (blocked), out, (blocked)."""
    mk, cuts, tm = _mask_world([10.0, 14.0, 18.0, 24.0, 28.0], a_cov=(0.0, 0.5))
    types = [k["mask"]["type"] for k in cuts]
    assert types == ["punch_in", "none", "none", "punch_in", "none"]
    assert cuts[0]["mask"]["zoom"] > 1.1 and cuts[3]["mask"]["zoom"] == 1.0           # one way in, a real dwell later one way out
    for k in (cuts[1], cuts[2], cuts[4]):
        assert k["review"] is True and "zoom limit" in k["mask"]["reason"] and "zoom_min_dwell_s" in k["mask"]["reason"]
    vid = cleanup._emit_video(mk.plan, tm)
    rep = zoomstate.validate(vid, 8.0, 0.08)
    assert rep["ok"] and rep["ins"] == 1 and rep["outs"] == 1


def test_a_cutaway_is_still_preferred_to_a_blocked_punch_in():
    mk, cuts, _ = _mask_world([10.0, 20.0], pol_over={"zoom_min_dwell_s": 8.0})
    assert [k["mask"]["type"] for k in cuts] == ["cutaway", "cutaway"]               # other masks are untouched by the limiter


def test_dwell_zero_restores_legacy_alternation():
    mk, cuts, tm = _mask_world([10.0, 14.0, 18.0], a_cov=(0.0, 0.5), pol_over={"zoom_min_dwell_s": 0})
    assert [k["mask"]["type"] for k in cuts] == ["punch_in"] * 3


# ---- pacing --------------------------------------------------------------------------------------------------------------

def _pace(pol, total=60.0):
    src = sources(listener=False)               # one camera only: the punch-in is the only reaction available
    words = monologue(total)
    tm = TimeMap([(0.0, total)])
    vid, rep = cleanup.apply_pacing(copy.deepcopy(long_video(total)), words, tm, src, {k: (0.0, 1000.0) for k in src}, pol, {"A": "S1"})
    return vid, rep


def test_pacing_punch_in_is_a_sustained_reframe_not_a_blip():
    vid, rep = _pace(POL)
    ins = [d for d in rep["decisions"] if d["mask"] == "punch_in"]
    assert ins and all(d["sustained"] for d in ins)
    for d in ins:
        assert d["o1"] - d["o0"] >= POL["pacing"]["zoom_min_dwell_s"] - 0.01           # a real dwell, not 2-4 s
        assert "sustained reframe" in d["reason"]
    z = zoomstate.validate(vid, 8.0, POL["masks"]["min_zoom_delta"])
    assert z["ok"], z["violations"]
    assert z["min_interval_s"] is None or z["min_interval_s"] >= 8.0


def test_pacing_legacy_blip_when_the_dwell_is_zero():
    pol = copy.deepcopy(POL)
    pol["pacing"]["zoom_min_dwell_s"] = 0
    vid, rep = _pace(pol)
    ins = [d for d in rep["decisions"] if d["mask"] == "punch_in"]
    assert ins and all(2.0 - 0.01 <= d["o1"] - d["o0"] <= 4.0 + 0.01 for d in ins) and not any(d["sustained"] for d in ins)


def test_pacing_hold_that_ends_in_a_camera_change_keeps_the_punch_to_its_end():
    """A 20 s hold on A followed by B: the reframe runs to the camera change (no zoom-out blip at the end)."""
    src = sources()
    words = monologue(40.0)
    vid = [{**long_video(40.0)[0], "o1": 20.0}, {"id": "v2", "src": "B", "o0": 20.0, "o1": 40.0, "e0": 20.0, "crop": None, "zoom": 1.0,
                                                  "reason": "speaker change", "confidence": 1.0, "speaker": "S2"}]
    pol = copy.deepcopy(POL)
    pol["pacing"]["camera_order"] = ["punch_in"]
    out, rep = cleanup.apply_pacing(vid, words, TimeMap([(0.0, 40.0)]), src, {k: (0.0, 1000.0) for k in src}, pol, {"A": "S1", "B": "S2"})
    d = rep["decisions"][0]
    assert d["mask"] == "punch_in" and d["o1"] == pytest.approx(20.0) and "to the end of the hold" in d["reason"]
    assert zoomstate.validate(out, 8.0, 0.08)["ok"]


# ---- cut inside a word ----------------------------------------------------------------------------------------------------

def _doc_with_splice(src_clock, e_out, e_in):
    """A dialogue track with one splice: kept episode [0, e_out] then [e_in, e_in + 5]."""
    sources_ = {"M": dict({"file": "m.wav", "kind": "audio"}, **src_clock)}
    d1 = e_out
    return {"sources": sources_, "tracks": {
        "dialogue": [{"id": "d1", "src": "M", "o0": 0.0, "o1": d1, "e0": 0.0},
                     {"id": "d2", "src": "M", "o0": d1, "o1": d1 + 5.0, "e0": e_in}], "video": []}}


def test_a_cut_inside_a_word_is_reported_in_source_time_with_drift():
    words = [{"w": "wonderful", "start": 2000.0, "end": 2000.6, "speaker": "S1"}]
    # episode 2000.10 + offset 100 + drift 1000 ppm  ->  source 2000.1*... : the out-point lands at source ~ (e-100)/1.001
    e_cut = 100.0 + 2000.3 * 1.001                 # the source clock says 2000.3: in the middle of "wonderful"
    doc = _doc_with_splice({"start_offset": 100.0, "drift_ppm": 1000.0}, e_cut, e_cut + 3.0)
    hits = cutwords.check(doc, words)
    assert len(hits) == 1 and hits[0]["side"] == "out" and hits[0]["word"] == "wonderful"
    assert hits[0]["t"] == pytest.approx(2000.3, abs=1e-6) and hits[0]["kind"] == "sliced"
    assert hits[0]["o"] == pytest.approx(e_cut) and "wonderful" in cutwords.format_hits(hits)[0]
    # the same episode time WITHOUT the drift would have fallen in the gap after the word: the check must not use a hand-written formula
    flat = _doc_with_splice({"start_offset": 100.0, "drift_ppm": 0.0}, e_cut, e_cut + 3.0)
    assert cutwords.check(flat, words) == []


def test_cuts_in_gaps_and_at_word_edges_within_the_margin_are_fine():
    words = [{"w": "one", "start": 1.0, "end": 1.5, "speaker": "S1"}, {"w": "two", "start": 2.0, "end": 2.5, "speaker": "S1"}]
    ok = _doc_with_splice({"start_offset": 0.0}, 1.75, 1.99)             # out in the gap, in 10 ms before "two"
    assert cutwords.check(ok, words) == []
    edge = _doc_with_splice({"start_offset": 0.0}, 1.49, 2.01)           # 10 ms inside the margin on both sides
    assert cutwords.check(edge, words, margin_s=0.02) == []
    assert len(cutwords.check(edge, words, margin_s=0.0)) == 2


def test_a_short_fragment_of_a_removed_word_is_a_stub_not_a_slice():
    words = [{"w": "uhm", "start": 1.0, "end": 1.6, "speaker": "S1"}]
    doc = _doc_with_splice({"start_offset": 0.0}, 1.06, 3.0)               # 60 ms of a 600 ms removed word left behind
    h = cutwords.check(doc, words, stub_max_s=0.1)[0]
    assert h["kind"] == "stub" and h["kept_fragment_s"] == pytest.approx(0.06)


def test_hot_edge_is_reported_when_speech_runs_across_a_cut_at_the_asr_end():
    sr = 16000
    t = np.arange(sr * 4) / sr
    x = (0.4 * np.sin(2 * np.pi * 220 * t) * (t < 2.0)).astype(np.float32) + np.random.default_rng(3).normal(0, 0.002, len(t)).astype(np.float32)
    env = cleanup.Envelope(x, sr, 0.0)
    words = [{"w": "right.", "start": 1.0, "end": 1.5, "speaker": "S1"}]          # ASR says the word ended at 1.5; the tone really runs to 2.0
    doc = _doc_with_splice({"start_offset": 0.0}, 1.5, 3.5)
    out = cutwords.run_checks(doc, words, env, POL)
    assert [h["side"] for h in out["hot_edges"]] == ["out"] and out["hot_edges"][0]["word"] == "right."
    assert any("speech continuing" in w for w in out["warnings"])
    quiet = _doc_with_splice({"start_offset": 0.0}, 2.5, 3.5)                      # same word, cut in real silence: nothing to report
    assert cutwords.run_checks(quiet, [{**words[0], "end": 2.5}], env, POL)["hot_edges"] == []


# ---- the cut itself: tail pad ---------------------------------------------------------------------------------------------

def _tail_world(first_conf, first_len):
    """Kept "right" ends at 1.0 by the ASR, but its tail decays until 1.12; a removed run starts at 1.0 (first word `first_len` long)."""
    sr = 16000
    t = np.arange(sr * 4) / sr
    env_amp = np.where(t < 1.0, 0.4, np.where(t < 1.12, 0.4 * np.exp(-(t - 1.0) * 30.0), 0.0005))
    x = (env_amp * np.sin(2 * np.pi * 220 * t) + np.random.default_rng(1).normal(0, 0.0005, len(t))).astype(np.float32)
    env = cleanup.Envelope(x, sr, 0.0)
    words = [W("right.", 0.5, 1.0), W("oh", 1.0, 1.0 + first_len), W("my", 1.4, 1.6), W("god.", 1.6, 2.0), W("then", 2.6, 3.0)]
    words[1]["conf"] = first_conf
    for i, w in enumerate(words):
        w["i"], w["n"] = i, cleanup.norm(w["w"])
    regions, _ = cleanup.plan_regions(words, {1, 2, 3}, POL)
    return env, regions[0]


def test_cut_out_slides_through_a_ghost_word_to_a_real_valley_after_the_tail():
    env, r = _tail_world(first_conf=0.7, first_len=0.08)
    plain = copy.deepcopy(POL)
    plain["pause"]["word_tail_pad_ms"] = plain["pause"]["word_tail_pad_ghost_ms"] = 0
    before = cleanup._cut_for_region(r, env, plain)
    after = cleanup._cut_for_region(r, env, POL)
    assert before["e0"] == pytest.approx(1.0, abs=0.01) and before["snap"]["valley"] is False
    assert 1.1 <= after["e0"] <= 1.0 + 0.15 + 1e-6 and env.db_at(after["e0"]) < env.speech_db - 20       # tail kept whole, cut in the quiet
    assert after["e0"] < r["le"]                                                                              # never past the removed run


def test_cut_out_never_eats_into_a_confident_real_word():
    env, r = _tail_world(first_conf=1.0, first_len=0.3)       # "oh" is a real, confident, 0.3 s word
    cut = cleanup._cut_for_region(r, env, POL)
    assert cut["e0"] <= 1.0 + POL["pause"]["word_tail_pad_ms"] / 1000.0 + 1e-6          # at most one ASR frame (80 ms)


# ---- the command and the report -------------------------------------------------------------------------------------------

def test_policy_validation_and_default_keys():
    assert POL["masks"]["zoom_min_dwell_s"] == 8.0 and POL["pacing"]["zoom_min_dwell_s"] == 8.0
    bad = copy.deepcopy(POL)
    bad["masks"]["zoom_min_dwell_s"] = -1
    with pytest.raises(ValueError, match="zoom_min_dwell_s"):
        cleanup.validate_policy(bad)
    assert zoomstate.policy_limits(POL) == (8.0, POL["masks"]["min_zoom_delta"], 1)


def test_shorts_jump_cut_masks_respect_the_dwell():
    from ave import shorts
    doc = {"tracks": {"video": [{"id": f"v{i}", "src": "B", "o0": o0, "o1": o1, "e0": e0, "zoom": 1.0}
                                for i, (o0, o1, e0) in enumerate([(0, 3, 100), (3, 6, 110), (6, 9, 120), (20, 30, 130), (30, 40, 140)])]}}
    legacy = copy.deepcopy(doc)
    shorts.mask_jump_cuts(legacy, 1.15, dwell_s=0)
    assert zoomstate.validate(legacy["tracks"]["video"], 8.0, 0.05)["short_dwell"] >= 1          # the old behaviour: in at 3 s, out at 6 s
    notes = shorts.mask_jump_cuts(doc, 1.15, dwell_s=8.0)
    zs = [s["zoom"] for s in doc["tracks"]["video"]]
    assert zs == [1.0, 1.15, 1.15, 1.15, 1.0]                       # in at 3 s, held through the jump cuts, one way out at 30 s
    assert any("held at 1.15x" in n for n in notes)
    assert zoomstate.validate(doc["tracks"]["video"], 8.0, 0.05)["ok"]


@needs_ffmpeg
def test_cleanup_report_and_final_carry_the_post_pass_checks(synth_episode):
    from ave import editdoc, final
    rep = cleanup.run(synth_episode)
    assert rep["zoom"]["ok"] and set(rep["cutcheck"]) >= {"inside_word", "stubs", "hot_edges", "audio_checked"}
    assert rep["cutcheck"]["audio_checked"] and "Zoom:" in cleanup.explain(rep) and "Cuts in words:" in cleanup.explain(rep)
    f = final.build_final(synth_episode, render=False, do_retranscribe=False)
    assert f["checks"]["zoom"]["ok"] and f["checks"]["word_cuts"]["inside_word"] == 0
    # a hand-made in-out-in on the finished edit is reported as a warning (the validator warns, it never edits)
    doc = editdoc.load_latest(synth_episode, "rough")
    v = doc["tracks"]["video"][0]
    d = v["o1"] - v["o0"]
    q = lambda i, a, b, z: {**v, "id": f"x{i}", "o0": v["o0"] + d * a, "o1": v["o0"] + d * b, "zoom": z}   # noqa: E731
    doc["tracks"]["video"] = [q(1, 0, .25, 1.0), q(2, .25, .5, 1.18), q(3, .5, .75, 1.0), q(4, .75, 1, 1.12)]
    chk = cleanup.check_doc(synth_episode, doc)
    assert not chk["zoom"]["ok"] and any("zoom rule broken" in w for w in chk["warnings"])
