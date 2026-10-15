"""Pacing pass (T12 item 1): reaction shots that break long holds. Pure-function tests on hand-built video tracks and words;
the whole-command behaviour on rendered synthetic media is in test_pacing_flow.py."""

import copy
import random

import pytest

from ave import cleanup
from ave.editdoc import TimeMap

POL = cleanup.load_policy()
PAC = POL["pacing"]


def W(text, start, end, spk="S1"):
    return {"w": text, "n": cleanup.norm(text), "start": start, "end": end, "speaker": spk, "conf": 0.95}


def monologue(total=60.0, spk="S1", sent_words=5, word_s=0.5, gap_s=0.1, sent_gap_s=0.3, t0=0.0):
    """One speaker talking in sentences of `sent_words` words; returns words (sorted)."""
    out, t, n = [], t0, 0
    while t < total - 2:
        for k in range(sent_words):
            end = t + word_s
            last = k == sent_words - 1
            out.append(W(f"w{n}" + ("." if last else ""), round(t, 3), round(end, 3), spk))
            n += 1
            t = end + (sent_gap_s if last else gap_s)
    return out


def sources(wide=False, listener=True):
    s = {"A": {"file": "a.mp4", "kind": "video", "start_offset": 0.0, "role": "host"}}
    if listener:
        s["B"] = {"file": "b.mp4", "kind": "video", "start_offset": 0.0, "role": "guest"}
    if wide:
        s["W"] = {"file": "w.mp4", "kind": "video", "start_offset": 0.0, "role": "wide"}
    return s


def cov_of(src, lo=0.0, hi=1000.0):
    return {k: (lo, hi) for k in src}


def video(total, src="A", speaker="S1", zoom=1.0):
    return [{"id": "v1", "src": src, "o0": 0.0, "o1": total, "e0": 0.0, "crop": None, "zoom": zoom,
             "reason": "speaker change", "confidence": 1.0, "speaker": speaker}]


def run(vid, words, src=None, cov=None, pol=None, total=None, cam_speaker=None):
    src = src or sources()
    total = total or vid[-1]["o1"]
    tm = TimeMap([(0.0, total)])
    return cleanup.apply_pacing(copy.deepcopy(vid), words, tm, src, cov or cov_of(src), pol or POL,
                                cam_speaker or {"A": "S1", "B": "S2"})


def holds(vid, delta=0.08):
    out, s = [], 0
    for i in range(1, len(vid) + 1):
        if i == len(vid) or not cleanup._same_framing(vid[i - 1], vid[i], delta):
            out.append((vid[s]["o0"], vid[i - 1]["o1"], vid[s]["src"]))
            s = i
    return out


def free_gaps(words, t, tol=1e-6):
    """True when t is not strictly inside any word."""
    return not any(w["start"] + tol < t < w["end"] - tol for w in words)


def test_policy_has_a_valid_pacing_block():
    cleanup.validate_policy(POL)
    assert PAC["enabled"] is True and PAC["max_hold_s"] == 14 and PAC["max_cuts_per_min"] == 8
    assert (PAC["reaction"]["min_s"], PAC["reaction"]["max_s"], PAC["reaction"]["min_gap_after_cut_s"]) == (2.0, 4.0, 2.5)
    assert PAC["camera_order"] == ["wide", "listener", "punch_in"]
    bad = copy.deepcopy(POL)
    bad["pacing"]["reaction"]["min_s"] = 5.0
    with pytest.raises(ValueError, match="pacing.reaction"):
        cleanup.validate_policy(bad)
    bad = copy.deepcopy(POL)
    bad["pacing"]["camera_order"] = ["listener", "drone"]
    with pytest.raises(ValueError, match="camera_order"):
        cleanup.validate_policy(bad)


def test_long_hold_gets_listener_reactions_and_nothing_stays_over_the_limit():
    words = monologue(60.0)
    vid, rep = run(video(60.0), words, total=60.0)
    assert len(holds(video(60.0))) == 1                                 # before: one 60 s hold
    hs = holds(vid)
    assert all(b - a <= PAC["max_hold_s"] + 1e-6 for a, b, _ in hs), hs
    reactions = [s for s in vid if s["reason"] == "pacing: reaction"]
    assert reactions and rep["inserted"] == len(reactions) and rep["still_over_limit"] == []
    for s in reactions:
        assert s["src"] == "B" and s["zoom"] == 1.0 and s["speaker"] == "S2"
        assert PAC["reaction"]["min_s"] - 1e-6 <= s["o1"] - s["o0"] <= PAC["reaction"]["max_s"] + 1e-6
        assert 0 < s["confidence"] <= 1
    assert rep["by_mask"] == {"listener": len(reactions)}
    assert rep["before"]["holds_over_limit"] == 1 and rep["after"]["holds_over_limit"] == 0
    assert rep["after"]["avg_shot_s"] < rep["before"]["avg_shot_s"]
    # contiguous, validates as a track
    assert vid[0]["o0"] == 0 and vid[-1]["o1"] == 60.0
    for a, b in zip(vid, vid[1:]):
        assert a["o1"] == pytest.approx(b["o0"], abs=1e-9)


def test_reactions_start_and_end_between_words_and_keep_distance_from_cuts():
    for seed in range(25):
        rng = random.Random(seed)
        words, t = [], 0.0
        while t < 118:
            d = rng.uniform(0.15, 0.7)
            words.append(W(rng.choice(["so", "we", "go.", "then,", "now", "it."]), round(t, 3), round(t + d, 3)))
            t += d + rng.choice([0.0, 0.02, 0.1, 0.3, 0.8])
        vid, rep = run(video(120.0), words, total=120.0)
        hs = holds(vid)
        for s in vid:
            if s["reason"] != "pacing: reaction":
                continue
            assert free_gaps(words, s["o0"]) and free_gaps(words, s["o1"]), (seed, s)
        for i, s in enumerate(vid):
            if s["reason"] != "pacing: reaction":
                continue
            assert vid[i - 1]["o1"] - vid[i - 1]["o0"] >= PAC["reaction"]["min_gap_after_cut_s"] - 1e-6, (seed, i)
            if i + 1 < len(vid):
                assert vid[i + 1]["o1"] - vid[i + 1]["o0"] >= PAC["reaction"]["min_gap_after_cut_s"] - 1e-6, (seed, i)
        assert all(b - a <= PAC["max_hold_s"] + 1e-6 for a, b, _ in hs) or rep["still_over_limit"], seed


def test_prefers_a_strong_sentence_end_over_other_boundaries():
    words = monologue(40.0, sent_words=6)
    vid, rep = run(video(40.0), words, total=40.0)
    ends = [w["end"] for w in words if w["w"].endswith(".")]
    firsts = [s for s in vid if s["reason"] == "pacing: reaction"]
    assert firsts
    for s in firsts:
        # reaction starts in the gap right after a sentence end
        assert any(abs(s["o0"] - e) < 0.31 for e in ends), (s["o0"], ends[:8])
    d = rep["decisions"][0]
    assert d["boundary"] == "sentence" and d["confidence"] == pytest.approx(PAC["confidence"]["sentence"], abs=0.05)


def test_wide_camera_is_preferred_when_it_covers_the_moment():
    src = sources(wide=True)
    vid, rep = run(video(60.0), monologue(60.0), src=src)
    rs = [s for s in vid if s["reason"] == "pacing: reaction"]
    assert rs and all(s["src"] == "W" and s["speaker"] is None for s in rs)
    assert rep["by_mask"] == {"wide": len(rs)}
    # a wide that has no footage for the moment is skipped, the listener is used instead
    cov = cov_of(src)
    cov["W"] = (0.0, 5.0)
    vid2, rep2 = run(video(60.0), monologue(60.0), src=src, cov=cov)
    assert {s["src"] for s in vid2 if s["reason"] == "pacing: reaction"} == {"B"}


def test_punch_in_fallback_alternates_zooms_on_the_same_camera():
    src = sources(listener=False)
    vid, rep = run(video(90.0), monologue(90.0), src=src, cam_speaker={"A": "S1"})
    rs = [s for s in vid if s["reason"] == "pacing: reaction"]
    assert len(rs) >= 4 and all(s["src"] == "A" for s in rs)
    assert [s["zoom"] for s in rs[:4]] == [1.10, 1.20, 1.10, 1.20]
    assert rep["by_mask"] == {"punch_in": len(rs)}
    assert all(b - a <= PAC["max_hold_s"] + 1e-6 for a, b, _ in holds(vid))
    assert all(s["confidence"] < 0.9 for s in rs)           # a punch-in is a weaker answer than a real second angle


def test_never_shows_the_listener_while_they_speak():
    words = monologue(60.0)
    # the listener keeps putting in words (one every 1.5 s) from 4 to 40: any 2 s+ listener cutaway there shows them talking
    talk = [W("mm", t, round(t + 0.2, 3), "S2") for t in [4 + 1.5 * k for k in range(24)]]
    words = sorted(words + talk, key=lambda w: w["start"])
    vid, rep = run(video(60.0), words)
    rs = [s for s in vid if s["reason"] == "pacing: reaction"]
    assert rs
    for s in rs:
        if s["src"] == "B":
            assert not any(w["speaker"] == "S2" and w["start"] < s["o1"] and w["end"] > s["o0"] for w in words), s
    assert any(s["src"] == "A" and s["zoom"] > 1.0 for s in rs if 4 <= s["o0"] < 40)         # punch-in while they talk
    pol = copy.deepcopy(POL)
    pol["pacing"]["listener_may_speak"] = True
    vid2, _ = run(video(60.0), words, pol=pol)
    assert any(s["src"] == "B" and 4 <= s["o0"] < 40 for s in vid2 if s["reason"] == "pacing: reaction")


def test_falls_back_to_punch_in_when_the_listener_never_stops_talking():
    talk = [W("mm", round(i * 1.0 + 0.5, 3), round(i * 1.0 + 0.7, 3), "S2") for i in range(40)]      # a word every second
    words = sorted(monologue(40.0) + talk, key=lambda w: w["start"])
    vid, rep = run(video(40.0), words)
    rs = [s for s in vid if s["reason"] == "pacing: reaction"]
    assert rs and all(s["src"] == "A" and s["zoom"] > 1.0 for s in rs)


def test_deterministic_and_idempotent():
    words = monologue(120.0)
    a, ra = run(video(120.0), words, src=sources(wide=True))
    b, rb = run(video(120.0), words, src=sources(wide=True))
    assert a == b and ra == rb
    # feeding the result through the pass again changes nothing (the holds are already short)
    tm = TimeMap([(0.0, 120.0)])
    again, rep2 = cleanup.apply_pacing(copy.deepcopy(a), words, tm, sources(wide=True), cov_of(sources(wide=True)), POL, {"A": "S1", "B": "S2"})
    strip = lambda v: [{k: x for k, x in s.items() if k != "id"} for s in v]  # noqa: E731
    assert strip(again) == strip(a) and rep2["inserted"] == 0


def test_the_cut_rate_cap_is_respected_and_reported():
    words = monologue(60.0)
    pol = copy.deepcopy(POL)
    pol["pacing"]["max_cuts_per_min"] = 2          # a reaction is two cuts: one fits per minute at most
    pol["pacing"]["hard_rate_cap"] = True
    vid, rep = run(video(60.0), words, pol=pol)
    assert sum(1 for s in vid if s["reason"] == "pacing: reaction") <= 1
    assert rep["still_over_limit"] and "max_cuts_per_min" in rep["still_over_limit"][0]["why"] and rep["rate_overrides"] == 0


def test_a_busy_minute_yields_only_for_a_hold_past_the_tolerance():
    pol = copy.deepcopy(POL)
    pol["pacing"]["max_cuts_per_min"] = 2
    # soft cap, 60 s hold (far past 14 + 4): the first reaction respects the cap, later ones override it and say so
    vid, rep = run(video(60.0), monologue(60.0), pol=pol)
    assert all(b - a <= PAC["max_hold_s"] + 1e-6 for a, b, _ in holds(vid)) and rep["rate_overrides"] >= 1
    assert any(d["rate_override"] and "max_cuts_per_min" in d["reason"] for d in rep["decisions"])
    assert not rep["decisions"][0]["rate_override"]
    # a 16 s hold is within the tolerance (14 + 4): in a busy minute the cap wins and the hold is reported as tolerated
    busy = copy.deepcopy(POL)
    busy["pacing"]["max_cuts_per_min"] = 0.1
    seg = {"id": "v1", "src": "A", "o0": 0.0, "o1": 0.4, "e0": 0.0, "crop": None, "zoom": 1.0, "reason": "x", "confidence": 1.0, "speaker": "S1"}
    dense = [seg, {**seg, "id": "v2", "src": "B", "o0": 0.4, "o1": 0.8, "e0": 0.4}, {**seg, "id": "v3", "o0": 0.8, "o1": 16.0, "e0": 0.8}]
    _, r4 = run(dense, monologue(16.0), pol=busy, total=16.0)
    assert r4["inserted"] == 0 and r4["still_over_limit"][0]["within_tolerance"] is True


def test_a_hold_needs_room_on_both_sides_or_it_is_reported():
    # 15 s hold, but the only boundaries are inside the first/last 2.5 s: no valid reaction
    words = [W("long", 2.6, 12.0)]
    vid, rep = run(video(15.0), words, total=15.0)
    assert [s for s in vid if s["reason"] == "pacing: reaction"] == []
    assert len(rep["still_over_limit"]) == 1 and rep["still_over_limit"][0]["o1"] == pytest.approx(15.0)


def test_disabled_pacing_changes_nothing():
    pol = copy.deepcopy(POL)
    pol["pacing"]["enabled"] = False
    base = video(60.0)
    vid, rep = run(base, monologue(60.0), pol=pol)
    assert vid == base and rep["inserted"] == 0 and rep["enabled"] is False


def test_splices_inside_the_hold_keep_the_dialogue_clock():
    # kept ranges 0-20 and 25-60 (a 5 s cut at episode time 20): output time is the concatenation; the camera resumes in step
    tm = TimeMap([(0.0, 20.0), (25.0, 60.0)])
    words = [w for w in monologue(60.0) if w["end"] <= 20.0 or w["start"] >= 25.0]
    vid = [{"id": "v1", "src": "A", "o0": 0.0, "o1": 20.0, "e0": 0.0, "crop": None, "zoom": 1.0, "reason": "x", "confidence": 1.0, "speaker": "S1"},
           {"id": "v2", "src": "A", "o0": 20.0, "o1": 55.0, "e0": 25.0, "crop": None, "zoom": 1.0, "reason": "x", "confidence": 1.0, "speaker": "S1"}]
    out, rep = cleanup.apply_pacing(vid, words, tm, sources(), cov_of(sources()), POL, {"A": "S1", "B": "S2"})
    for s in out:
        assert s["e0"] == pytest.approx(tm.to_episode(s["o0"] + 1e-9), abs=1e-6)       # every segment sits at the right episode time
    assert all(b - a <= PAC["max_hold_s"] + 1e-6 for a, b, _ in holds(out))
