"""The critic encodes the creator's authenticity standard as data and enforces its arithmetic and hard gates."""
import pytest

from ave.assets import critic


@pytest.fixture(scope="module")
def cfg():
    return critic.load_config()


GOOD_SCORES = {"texture_light": 2, "handheld_observational": 2, "specific_over_generic": 2,
               "restraint": 1, "ambient_sound": 1, "no_logos_faces_unreleased": 2}


def good(**kw):
    c = {"id": "pexels-1", "query": "hands shaping wet clay window light", "tags": ["hands", "clay", "studio"],
         "filename": "pexels-3195394.mp4", "duration": 6.0, "has_audio": True, "scores": dict(GOOD_SCORES)}
    c.update(kw)
    return c


@pytest.mark.parametrize("text", [
    "business handshake", "man typing on laptop", "laptop typing", "drone shot over city", "aerial city skyline at dusk",
    "neon AI brain", "glowing digital brain", "ukulele corporate upbeat", "whoosh transition", "cinematic riser",
    "light bulb idea", "team high five", "hologram interface", "binary code rain", "handshakes"])
def test_cliche_terms_are_caught_in_queries(cfg, text):
    assert critic.find_cliches(text, cfg)["reject"], text


@pytest.mark.parametrize("text", ["hands shaping wet clay", "rain on a bus window", "drone-free handheld market morning",
                                  "old man repairing a bicycle chain", "hand shake of a vintage bottle"])
def test_specific_texts_are_not_flagged(cfg, text):
    assert critic.find_cliches(text, cfg)["reject"] == []


def test_word_order_free_combos_and_punctuation(cfg):
    assert critic.find_cliches("City, aerial", cfg)["reject"]
    assert critic.find_cliches("Typing... on a (laptop)", cfg)["reject"]


def test_good_candidate_passes_with_reasons_and_confidence(cfg):
    r = critic.critique(good(), cfg)
    assert r["pass"] is True and r["total"] == 10 and r["max"] == 12
    assert r["reasons"] and 0 < r["confidence"] <= 1
    assert r["hard_fails"] == []


def test_cliche_in_query_tags_or_filename_auto_rejects_even_with_perfect_scores(cfg):
    for field, val in (("query", "handshake deal"), ("tags", ["laptop", "typing"]), ("filename", "drone-city-aerial.mp4"), ("title", "ukulele corporate")):
        r = critic.critique(good(**{field: val}), cfg)
        assert r["pass"] is False and any("cliche" in h for h in r["hard_fails"]), field
        assert r["confidence"] >= 0.9


def test_unscored_candidate_cannot_pass_because_nobody_looked(cfg):
    r = critic.critique(good(scores={}), cfg)
    assert r["pass"] is False and "not visually judged" in " ".join(r["hard_fails"]).lower()
    r = critic.critique(good(scores={"texture_light": 2}), cfg)
    assert r["pass"] is False and "handheld_observational" in " ".join(r["hard_fails"])


def test_below_threshold_fails(cfg):
    s = {k: 1 for k in GOOD_SCORES} | {"no_logos_faces_unreleased": 1}
    s.update(texture_light=1, restraint=1)
    r = critic.critique(good(scores={k: 1 for k in GOOD_SCORES}), cfg)  # 6/12
    assert r["pass"] is False and r["total"] == 6


def test_unreleased_faces_or_logos_is_a_hard_gate_regardless_of_total(cfg):
    s = dict(GOOD_SCORES, no_logos_faces_unreleased=0, restraint=2, ambient_sound=2)
    r = critic.critique(good(scores=s), cfg)
    assert r["total"] == 10 and r["pass"] is False
    assert any("release" in h.lower() for h in r["hard_fails"])


def test_two_zero_criteria_fail_even_above_threshold(cfg):
    s = dict(GOOD_SCORES, restraint=0, ambient_sound=0, texture_light=2, specific_over_generic=2, handheld_observational=2)
    s["no_logos_faces_unreleased"] = 2
    r = critic.critique(good(scores=s), cfg)
    assert r["total"] == 8 and r["pass"] is False  # 8 meets the total, but two zeros is two weak spots
    assert any("zero" in x.lower() for x in r["hard_fails"])


def test_one_weak_spot_is_tolerated(cfg):
    s = dict(GOOD_SCORES, ambient_sound=0, restraint=2)
    assert critic.critique(good(scores=s), cfg)["pass"] is True


def test_silent_clip_cannot_claim_great_ambient_sound(cfg):
    r = critic.critique(good(has_audio=False, scores=dict(GOOD_SCORES, ambient_sound=2)), cfg)
    assert r["scores"]["ambient_sound"] == 1 and any("silent" in w.lower() for w in r["warnings"])


def test_score_evidence_raises_confidence(cfg):
    base = critic.critique(good(), cfg)["confidence"]
    ev = {k: {"score": v, "evidence": f"saw {k}"} for k, v in GOOD_SCORES.items()}
    assert critic.critique(good(scores=ev, looked_at="work/assets/sheet.jpg"), cfg)["confidence"] > base


def test_discouraged_terms_warn_and_lower_confidence_but_do_not_fail(cfg):
    plain = critic.critique(good(), cfg)
    r = critic.critique(good(tags=["time lapse", "clay"]), cfg)
    assert r["pass"] is True and r["warnings"] and r["confidence"] < plain["confidence"]


def test_too_short_candidate_fails_pacing_floor(cfg):
    r = critic.critique(good(duration=1.0), cfg)
    assert r["pass"] is False and any("shorter" in h for h in r["hard_fails"])


def test_invalid_scores_are_rejected_loudly(cfg):
    with pytest.raises(ValueError, match="0-2"):
        critic.critique(good(scores=dict(GOOD_SCORES, restraint=3)), cfg)
    with pytest.raises(ValueError, match="unknown criterion"):
        critic.critique(good(scores=dict(GOOD_SCORES, vibes=2)), cfg)


def test_config_is_data_every_criterion_has_guidance_for_the_agent(cfg):
    assert set(cfg["criteria"]) == set(GOOD_SCORES)
    for c in cfg["criteria"].values():
        assert c["look_for"] and set(c["anchors"]) == {0, 1, 2}


def test_user_config_overrides_default(tmp_path):
    p = tmp_path / "mine.yaml"
    p.write_text("version: 1\ncriteria: {a: {label: A, look_for: x, anchors: {0: a, 1: b, 2: c}}}\n"
                 "scoring: {max_per_criterion: 2, pass_total: 2, hard_gate_zero: [], weak_zero_limit: 1, require_all_scores: true, "
                 "silent_ambient_cap: 1, min_candidate_duration_s: 1}\ncliche_reject: [banana]\ncliche_discourage: []\n")
    c = critic.load_config(p)
    assert critic.find_cliches("banana", c)["reject"] and not critic.find_cliches("handshake", c)["reject"]


# ---- placement / pacing rules ---------------------------------------------------------------------------------

P = {"broll_min_s": 1.5, "broll_max_s": 4.0}


def test_pacing_flags_each_rule(cfg):
    placements = [{"id": "b1", "o0": 10.0, "o1": 10.8},   # too short
                  {"id": "b2", "o0": 20.0, "o1": 26.0},   # too long
                  {"id": "b3", "o0": 40.0, "o1": 42.0},   # over an emotional beat
                  {"id": "b4", "o0": 42.5, "o1": 44.0}]   # too close to b3
    r = critic.check_placements(placements, duration=100.0, beats=[{"t0": 41.0, "t1": 41.5, "label": "laugh"}], cfg=cfg)
    rules = {(v["rule"], v["id"]) for v in r["violations"]}
    assert ("length", "b1") in rules and ("length", "b2") in rules and ("emotional_beat", "b3") in rules and ("gap", "b4") in rules
    assert r["coverage"] == pytest.approx((0.8 + 6 + 2 + 1.5) / 100)


def test_pacing_coverage_cap(cfg):
    pl = [{"id": f"b{i}", "o0": i * 6.0, "o1": i * 6.0 + 3.5} for i in range(10)]
    r = critic.check_placements(pl, duration=100.0, beats=[], cfg=cfg)
    assert r["coverage"] == pytest.approx(0.35) and not any(v["rule"] == "coverage" for v in r["violations"])
    pl = [{"id": f"b{i}", "o0": i * 4.2, "o1": i * 4.2 + 3.5} for i in range(12)]
    assert any(v["rule"] == "coverage" for v in critic.check_placements(pl, duration=100.0, beats=[], cfg=cfg)["violations"])


def test_pacing_music_level_and_silence_after_punchline(cfg):
    music = [{"id": "m1", "o0": 0, "o1": 60, "gain_db": -12, "duck_db": -4}]
    r = critic.check_placements([], duration=60.0, beats=[], cfg=cfg, music=music, punchlines=[{"t": 30.0}])
    assert any(v["rule"] == "music_level" and "too loud" in v["why"] for v in r["violations"])
    assert any(v["rule"] == "silence_after_punchline" for v in r["violations"])
    dips = [{"id": "m1", "o0": 0, "o1": 30.0, "gain_db": -20, "duck_db": -6}, {"id": "m2", "o0": 31.0, "o1": 60, "gain_db": -20, "duck_db": -6}]
    ok = critic.check_placements([], 60.0, [], cfg, music=dips, punchlines=[{"t": 30.0}])
    assert ok["violations"] == []


def test_music_bed_level_window(cfg):
    def v(gain):
        return critic.check_placements([], 60.0, [], cfg, music=[{"id": "m", "o0": 0, "o1": 60, "gain_db": gain, "duck_db": -6}])["violations"]
    assert v(-20) == [] and v(-18) == [] and v(-24) == []
    assert v(-16)[0]["rule"] == "music_level" and "too loud" in v(-16)[0]["why"]
    assert "too quiet" in v(-30)[0]["why"]


def test_broll_starting_inside_punchline_silence_is_flagged(cfg):
    r = critic.check_placements([{"id": "b", "o0": 30.2, "o1": 32.0}], 60.0, [], cfg, punchlines=[{"t": 30.0}])
    assert any(v["rule"] == "silence_after_punchline" and v["id"] == "b" for v in r["violations"])
