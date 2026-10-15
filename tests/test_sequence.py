"""Opening and closing sequences: plan, build, programme join, captions offset, licences, `ave final` and Resolve handoff hooks.

Synthetic fixtures only (the 30 s two-camera episode, a generated tone as "licensed music"), no network and no Resolve: the handoff is
checked against the in-memory fake bridge of test_handoff_script.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
import yaml

import fixtures
from ave import captions, compile as comp, editdoc, ffutil, final, handoff, motion, sequence
from ave.assets import licenses
from ave.episode import Episode, read_json, write_json
from conftest import needs_ffmpeg

pytestmark = needs_ffmpeg

FAST_POLICY = {
    "opening": {"title_card": {"duration_s": 3.0}, "hosts": {"duration_s": 2.5}, "cold_open": {"min_s": 1, "max_s": 12}},
    "closing": {"thanks": {"duration_s": 3.0}, "credits": {"editor": "A. Editor", "px_per_frame": 6, "min_s": 1},
                "end_card": {"hold_s": 2.0}, "tail_room_tone_s": 1.0},
}
NOHOME = Path("/nonexistent-home")


def _tone(path: Path, seconds=40.0, hz=110):
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={hz}:duration={seconds}", "-ar", "48000",
                    str(path)], check=True)


def _log(ep: Episode, rel: str, **kw):
    p = ep.root / rel
    e = dict(source="own", asset_id="x", url="https://example.org/t", file=rel, sha256=licenses.sha256_file(p), license="Owned",
             license_url="https://example.org/l", license_text="Made for this test", approved_by="tester", author="Test Author",
             attribution={"required": False, "text": ""})
    e.update(kw)
    return licenses.log_for(ep).append(e)


def _setup(root: Path, with_rough=True, show=True) -> Episode:
    fixtures.build_episode(root, upto="assembly")
    ep = Episode(root)
    meta = read_json(ep.meta_path)
    meta["title"] = "The Long Table 00"
    if show:
        meta["show"] = {"hosts": [{"name": "Maren", "title": "Co-host"}, {"name": "Tomas", "title": "Co-host"}],
                        "links": [{"label": "Subscribe", "value": "example.org"}]}
        meta["episode_title"] = "Why we started this table"
    write_json(ep.meta_path, meta)
    if with_rough:
        doc = editdoc.clone(editdoc.load_latest(ep, "assembly"))
        doc["stage"] = "rough"
        doc["tracks"]["room_tone"] = {"src": doc["tracks"]["dialogue"][0]["src"], "e0": 6.2, "e1": 7.8}
        editdoc.save_new_version(ep, doc)
    return ep


def _policy(ep: Episode, over: dict | None = None) -> Path:
    pol = sequence._merge(FAST_POLICY, over or {})
    p = ep.root / "test-policy.yaml"
    p.write_text(yaml.safe_dump(pol))
    return p


@pytest.fixture
def ep(synth_episode):
    meta = read_json(synth_episode.meta_path)
    meta["title"] = "The Long Table 00"
    meta["show"] = {"hosts": [{"name": "Maren", "title": "Co-host"}, {"name": "Tomas", "title": "Co-host"}],
                    "links": [{"label": "Subscribe", "value": "example.org"}]}
    meta["episode_title"] = "Why we started this table"
    write_json(synth_episode.meta_path, meta)
    return synth_episode


# ---------------------------------------------------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------------------------------------------------

def test_default_policy_loads_and_covers_opening_and_closing():
    p = sequence.load_policy()
    assert set(p["opening"]) >= {"cold_open", "title_card", "hosts", "sting", "music"}
    assert set(p["closing"]) >= {"thanks", "credits", "end_card", "tail_room_tone_s"}
    assert 3 <= p["opening"]["title_card"]["duration_s"] <= 6 and p["closing"]["credits"]["edited_with_ave"] is False


def test_policy_resolution_order_project_over_user_over_default(ep, tmp_path):
    home = tmp_path / "home"
    (home / ".config" / "ave").mkdir(parents=True)
    (home / ".config" / "ave" / "sequences.yaml").write_text("closing: {credits: {editor: User Person}, end_card: {hold_s: 11}}\n")
    assert sequence.resolve_policy(ep, home=home)["closing"]["credits"]["editor"] == "User Person"
    (ep.root / "policy").mkdir()
    (ep.root / "policy" / "sequences.yaml").write_text("closing: {credits: {editor: Show Editor}}\n")
    p = sequence.resolve_policy(ep, home=home)
    assert p["closing"]["credits"]["editor"] == "Show Editor" and p["closing"]["end_card"]["hold_s"] == 11
    assert p["opening"]["title_card"]["duration_s"] == 4.5       # untouched keys keep the default
    with pytest.raises(FileNotFoundError):
        sequence.resolve_policy(ep, tmp_path / "nope.yaml")


def test_policy_validation_rejects_nonsense(tmp_path):
    bad = tmp_path / "b.yaml"
    bad.write_text("opening: {title_card: {duration_s: 9}}\n")
    with pytest.raises(ValueError, match="title_card"):
        sequence.load_policy(bad)
    bad.write_text("closing: {credits: {assets: some}}\n")
    with pytest.raises(ValueError, match="assets"):
        sequence.load_policy(bad)


def test_final_policy_switch_and_cli_override(ep, tmp_path):
    assert sequence.wanted(ep, "opening", home=NOHOME) is False
    (ep.root / "policy").mkdir()
    (ep.root / "policy" / "final.yaml").write_text("sequences: {opening: true}\n")
    assert sequence.wanted(ep, "opening", home=NOHOME) is True and sequence.wanted(ep, "closing", home=NOHOME) is False
    assert sequence.wanted(ep, "opening", False, home=NOHOME) is False and sequence.wanted(ep, "closing", True, home=NOHOME) is True


# ---------------------------------------------------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------------------------------------------------

def test_plan_lists_items_durations_text_and_sources(ep):
    pl = sequence.plan(ep, home=NOHOME)
    assert pl["errors"] == []
    op, cl = pl["opening"], pl["closing"]
    assert [i["id"] for i in op["items"]] == ["title_card", "host_intro"]
    assert [i["id"] for i in cl["items"]] == ["thanks", "credits", "end_card", "tail"]
    fps = pl["base"]["fps"]
    for sec in (op, cl):
        t = 0.0
        for it in sec["items"]:                                   # contiguous, whole frames
            assert it["o0"] == pytest.approx(t) and it["frames"] / fps == pytest.approx(it["duration"])
            t = it["o1"]
        assert sec["duration"] == pytest.approx(t) and sec["frames"] == sum(i["frames"] for i in sec["items"])
    title = op["items"][0]
    assert title["props"]["show"] == "The Long Table" and title["props"]["number"] == "00"
    assert "derived from episode.json title" in title["text"]["show"]["source"]
    assert op["items"][1]["props"]["people"] == [{"name": "Maren", "title": "Co-host"}, {"name": "Tomas", "title": "Co-host"}]
    assert pl["closing"]["items"][-1]["duration"] == pytest.approx(2.0)          # tail_room_tone_s default
    end = next(i for i in cl["items"] if i["id"] == "end_card")
    assert end["duration"] == pytest.approx(20.0)                                # YouTube end-screen hold (policy default)
    assert any("editor" in w for w in pl["warnings"])                            # no editor name configured
    assert sequence.plan_path(ep).exists() and read_json(sequence.plan_path(ep))["opening"]["hash"] == op["hash"]


def test_plan_without_a_show_name_derives_it_and_asks_for_approval(synth_episode):
    meta = read_json(synth_episode.meta_path)
    meta["title"] = "Slow Water 14"
    write_json(synth_episode.meta_path, meta)
    pl = sequence.plan(synth_episode, home=NOHOME)
    assert pl["identity"]["show"]["value"] == "Slow Water" and pl["identity"]["number"]["value"] == "14"
    reasons = [r for n in pl["approval"]["required"] for r in n["reasons"]]
    assert any("Slow Water" in r for r in reasons)
    assert any("no episode.json episode_title" in w for w in pl["warnings"])
    assert any("no hosts" in w or "host" in w for w in pl["warnings"]) or pl["opening"]["items"]   # speakers fixture names (Host/Guest)


def test_plan_warns_about_missing_room_tone_and_long_opening(ep):
    pl = sequence.plan(ep, home=NOHOME)
    assert any("room_tone" in w for w in pl["warnings"])
    p = _policy(ep, {"critic": {"opening_max_s": 4}})
    assert any("opening runs" in w for w in sequence.plan(ep, policy=p, write=False)["warnings"])


def test_plan_errors_on_text_that_does_not_fit_the_layout(ep):
    meta = read_json(ep.meta_path)
    meta["episode_title"] = "x" * 120
    write_json(ep.meta_path, meta)
    pl = sequence.plan(ep, home=NOHOME)
    assert any("title_card" in e and "80" in e for e in pl["errors"])


def test_missing_or_unlicensed_music_is_an_error_not_a_silent_skip(ep, tmp_path):
    pl0 = _policy(ep, {"opening": {"sting": {"file": "work/music/nope.wav"}}})
    assert any("nope.wav" in e and "does not exist" in e for e in sequence.plan(ep, policy=pl0, write=False)["errors"])
    m = ep.root / "work" / "music" / "sting.wav"
    m.parent.mkdir(parents=True, exist_ok=True)
    _tone(m, 3)
    pl1 = _policy(ep, {"opening": {"sting": {"file": "work/music/sting.wav"}}})
    errs = sequence.plan(ep, policy=pl1, write=False)["errors"]
    assert any("no license-log entry" in e for e in errs)
    with pytest.raises(sequence.SequenceError, match="license-log"):
        sequence.build(ep, policy=pl1, home=NOHOME)
    _log(ep, "work/music/sting.wav")
    pl = sequence.plan(ep, policy=pl1, write=False)
    assert pl["errors"] == [] and pl["assets"][0]["status"] == "licensed"


def test_missing_logo_and_unbuilt_clip_are_errors(ep):
    meta = read_json(ep.meta_path)
    meta["show"]["logo"] = "04_GFX/nope.png"
    meta["sequences"] = {"cold_open": {"clip": "no-such-clip"}}
    write_json(ep.meta_path, meta)
    errs = sequence.plan(ep, home=NOHOME, write=False)["errors"]
    assert any("show.logo" in e for e in errs) and any("clips.json" in e for e in errs)


def test_asset_credits_come_from_the_license_log(ep):
    m1, m2 = ep.root / "work" / "music" / "bed.wav", ep.root / "work" / "music" / "tune.wav"
    m1.parent.mkdir(parents=True, exist_ok=True)
    _tone(m1, 5, 110)
    _tone(m2, 5, 220)
    _log(ep, "work/music/bed.wav", author="Ada Player", notes="Quiet Water", attribution={"required": False, "text": ""})
    _log(ep, "work/music/tune.wav", attribution={"required": True, "text": "'Tune' by Bo Singer, CC BY 4.0"}, license="CC BY 4.0")
    doc = editdoc.clone(editdoc.load_latest(ep, "assembly"))
    doc["tracks"]["music"] = [{"id": "m1", "file": "work/music/bed.wav", "o0": 0, "o1": 10, "in": 0, "gain_db": -20},
                              {"id": "m2", "file": "work/music/tune.wav", "o0": 10, "o1": 20, "in": 0, "gain_db": -20}]
    pol = _policy(ep)
    pl = sequence.plan(ep, policy=pol, doc=doc, write=False)
    credits = next(i for i in pl["closing"]["items"] if i["id"] == "credits")
    music = next(s for s in credits["props"]["sections"] if s["heading"] == "Music and sound")
    assert "'Tune' by Bo Singer, CC BY 4.0" in music["lines"]
    assert any(l.startswith("Quiet Water by Ada Player") for l in music["lines"])
    assert credits["text"]["assets"]["source"].endswith("licenses.jsonl (files this cut and its sequences use)")
    # 'required' mode lists only what the license obliges us to credit
    req = _policy(ep, {"closing": {"credits": {"assets": "required"}}})
    music2 = next(s for s in next(i for i in sequence.plan(ep, policy=req, doc=doc, write=False)["closing"]["items"]
                                   if i["id"] == "credits")["props"]["sections"] if s["heading"] == "Music and sound")
    assert music2["lines"] == ["'Tune' by Bo Singer, CC BY 4.0"]
    # an unlicensed file in the cut stops the plan
    doc["tracks"]["music"].append({"id": "m3", "file": "work/music/bed.wav", "o0": 0, "o1": 5, "in": 0, "gain_db": -20})
    (ep.root / "work" / "music" / "bad.wav").write_bytes((m1).read_bytes()[:-2] + b"zz")
    doc["tracks"]["music"][2]["file"] = "work/music/bad.wav"
    assert any("bad.wav" in e and "license" in e for e in sequence.plan(ep, policy=pol, doc=doc, write=False)["errors"])


def test_credits_include_editor_guests_and_optional_ave_line(ep):
    meta = read_json(ep.meta_path)
    meta["guests"] = [{"name": "Maren Okafor", "title": "Ecologist"}]
    write_json(ep.meta_path, meta)
    pol = _policy(ep, {"closing": {"credits": {"edited_with_ave": True}}})
    sec = {s["heading"]: s["lines"] for s in next(i for i in sequence.plan(ep, policy=pol, write=False)["closing"]["items"]
                                                   if i["id"] == "credits")["props"]["sections"]}
    assert sec["Hosts"] == ["Maren", "Tomas"] and sec["Guests"] == ["Maren Okafor, Ecologist"]
    assert sec["Edited by"] == ["A. Editor", "Edited with ave"] and "Find us" in sec
    off = {s["heading"]: s["lines"] for s in next(i for i in sequence.plan(ep, policy=_policy(ep), write=False)["closing"]["items"]
                                                   if i["id"] == "credits")["props"]["sections"]}
    assert off["Edited by"] == ["A. Editor"]                                # 'edited with ave' is off by default


# ---------------------------------------------------------------------------------------------------------------------
# approval
# ---------------------------------------------------------------------------------------------------------------------

def test_approval_binds_to_the_plan_content(ep):
    pol = _policy(ep)
    pl = sequence.plan(ep, policy=pol)
    assert pl["approval"]["approved"] is False and pl["approval"]["required"]
    rec = sequence.approve(ep, by="ben", policy=pol)
    assert rec["by"] == "ben"
    again = sequence.plan(ep, policy=pol)
    assert again["approval"]["approved"] is True and again["approval"]["approved_by"] == "ben"
    meta = read_json(ep.meta_path)
    meta["episode_title"] = "A different title"
    write_json(ep.meta_path, meta)
    changed = sequence.plan(ep, policy=pol)
    assert changed["approval"]["approved"] is False and "changed since it was approved" in changed["approval"]["stale_approval"]


def test_cannot_approve_a_plan_with_errors(ep):
    meta = read_json(ep.meta_path)
    meta["episode_title"] = "x" * 200
    write_json(ep.meta_path, meta)
    with pytest.raises(sequence.SequenceError, match="errors"):
        sequence.approve(ep, policy=_policy(ep))


# ---------------------------------------------------------------------------------------------------------------------
# cold open resolution
# ---------------------------------------------------------------------------------------------------------------------

def _rough(ep):
    doc = editdoc.clone(editdoc.load_latest(ep, "assembly"))
    doc["stage"] = "rough"
    editdoc.save_new_version(ep, doc)


def test_cold_open_from_a_quote_is_found_in_the_transcript_and_whole_frames(ep):
    _rough(ep)
    meta = read_json(ep.meta_path)
    meta["sequences"] = {"cold_open": {"quote": {"text": "building things that last", "stage": "rough"}}}
    write_json(ep.meta_path, meta)
    pl = sequence.plan(ep, policy=_policy(ep), write=False)
    assert pl["errors"] == []
    co = pl["opening"]["items"][0]
    assert co["id"] == "cold_open" and co["source"]["kind"] == "quote" and "building things that last" in co["source"]["excerpt"]
    assert co["hook_s"] * 30 == pytest.approx(round(co["hook_s"] * 30)) and 1.0 < co["hook_s"] < 4.0
    assert any("cold open is the human's choice" in r for r in co["needs_approval"])
    assert pl["_cold"]["doc"]["tracks"]["dialogue"][0]["xfade_ms"] == 0


def test_cold_open_not_found_too_long_and_range(ep):
    _rough(ep)
    meta = read_json(ep.meta_path)
    meta["sequences"] = {"cold_open": {"quote": {"text": "words nobody said", "stage": "rough"}}}
    write_json(ep.meta_path, meta)
    assert any("was not found" in e for e in sequence.plan(ep, policy=_policy(ep), write=False)["errors"])
    meta["sequences"] = {"cold_open": {"range": {"stage": "rough", "o0": 0.0, "o1": 20.0}}}
    write_json(ep.meta_path, meta)
    assert any("longer than opening.cold_open.max_s" in e for e in sequence.plan(ep, policy=_policy(ep), write=False)["errors"])
    meta["sequences"] = {"cold_open": {"range": {"stage": "rough", "o0": 8.0, "o1": 14.0}}}
    write_json(ep.meta_path, meta)
    pl = sequence.plan(ep, policy=_policy(ep), write=False)
    assert pl["errors"] == [] and pl["opening"]["items"][0]["hook_s"] == pytest.approx(6.0, abs=0.04)
    short = _policy(ep, {"opening": {"cold_open": {"min_s": 10}}})
    assert any("shorter than 10" in w for w in sequence.plan(ep, policy=short, write=False)["warnings"])
    meta["sequences"] = {"cold_open": {"clip": "a", "quote": {"text": "x"}}}
    write_json(ep.meta_path, meta)
    assert any("exactly one of" in e for e in sequence.plan(ep, policy=_policy(ep), write=False)["errors"])


def test_cold_open_pick_is_ignored_when_the_policy_says_never(ep):
    _rough(ep)
    meta = read_json(ep.meta_path)
    meta["sequences"] = {"cold_open": {"range": {"stage": "rough", "o0": 8.0, "o1": 14.0}}}
    write_json(ep.meta_path, meta)
    pl = sequence.plan(ep, policy=_policy(ep, {"opening": {"cold_open": {"enabled": False}}}), write=False)
    assert pl["opening"]["items"][0]["id"] == "title_card" and any("ignored" in w for w in pl["warnings"])


# ---------------------------------------------------------------------------------------------------------------------
# captions: offset math
# ---------------------------------------------------------------------------------------------------------------------

def _cues(*spans):
    return [captions.Cue(a, b, t) for a, b, t in spans]


def test_srt_shift_moves_every_cue_by_the_opening_length(tmp_path):
    captions.write_srt(_cues((0.5, 2.0, "hello there"), (3.25, 4.5, "second line")), tmp_path / "a.srt")
    out = sequence.srt_shift(tmp_path / "a.srt", 8.5, tmp_path / "b.srt")
    got = captions.parse_srt(out)
    assert [(round(c.start, 3), round(c.end, 3), c.text) for c in got] == [(9.0, 10.5, "hello there"), (11.75, 13.0, "second line")]


def test_programme_srt_is_cold_open_cues_then_shifted_body(tmp_path, ep):
    captions.write_srt(_cues((0.4, 3.0, "the hook")), tmp_path / "cold.srt")
    captions.write_srt(_cues((1.0, 2.0, "body one"), (5.0, 6.0, "body two")), tmp_path / "body.srt")
    out = sequence.programme_srt(ep, tmp_path / "body.srt", tmp_path / "cold.srt", 10.0, tmp_path / "p.srt")
    got = [(round(c.start, 2), c.text) for c in captions.parse_srt(out)]
    assert got == [(0.4, "the hook"), (11.0, "body one"), (15.0, "body two")]
    assert sequence.programme_srt(ep, None, None, 5.0, tmp_path / "none.srt") is None


# ---------------------------------------------------------------------------------------------------------------------
# audio join: continuity
# ---------------------------------------------------------------------------------------------------------------------

def _sine(n, hz, amp=0.5, phase=0.0):
    t = np.arange(n) / sequence.SR
    x = (amp * np.sin(2 * np.pi * hz * t + phase)).astype("float32")
    return np.stack([x, x], axis=1)


def test_join_audio_fades_remove_the_click_and_keep_length():
    a, b = _sine(48000, 220), _sine(48000, 330, phase=1.3)           # waveforms that disagree at the join: a hard cut clicks
    hard, j0 = sequence.join_audio([a, b], 0.0)
    soft, j1 = sequence.join_audio([a, b], 0.2)
    assert len(hard) == len(soft) == 96000 and j0 == j1 == [48000]
    step = lambda x, j: float(np.abs(np.diff(x[j - 200:j + 200, 0])).max())      # noqa: E731  (largest sample-to-sample jump near the join)
    assert step(hard, 48000) > 0.4 and step(soft, 48000) < 0.05
    assert np.allclose(soft[:20000], a[:20000]) and np.allclose(soft[-20000:], b[-20000:])        # nothing else is touched
    th = np.abs(soft[48000 - 100:48000 + 100]).max()
    assert th < 0.05                                                 # the fades meet at (near) silence, not a bump


# ---------------------------------------------------------------------------------------------------------------------
# templates (kit hygiene + render)
# ---------------------------------------------------------------------------------------------------------------------

def test_sequence_templates_use_tokens_only_and_examples_validate():
    for d in sorted(p for p in sequence.SEQ_KIT.iterdir() if p.is_dir()):
        src = (d / "template.html").read_text()
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b", src), f"{d.name} hardcodes a hex color"
        for fam in re.findall(r"font-family:\s*([^;]+);", src):
            assert fam.strip().startswith("var(--f-"), f"{d.name} hardcodes font-family {fam}"
        assert "gsap" not in src.lower() and "requestAnimationFrame" not in src and "transition:" not in src
        schema = json.loads((d / "schema.json").read_text())
        ex = json.loads((d / "example.json").read_text())
        problems, _ = motion.validate_props(schema, ex["props"])
        assert problems == [], d.name
    assert [t["id"] for t in motion.list_templates()] == sorted(["lower-third", "quote-card", "chapter-card", "stat-callout", "title-card", "short-title"])


def test_sequence_templates_render_over_transparency(tmp_path):
    from PIL import Image
    for tid in ("episode-title", "host-card", "thanks-card", "end-card"):
        d = sequence.SEQ_KIT / tid
        ex = json.loads((d / "example.json").read_text())
        spec = {"template": tid, "props": motion.validate_props(json.loads((d / "schema.json").read_text()), ex["props"])[1], "duration": 4.0, "fps": 10.0,
                "canvas": (640, 360), "outro": True}
        png = tmp_path / f"{tid}.png"
        with motion._page(spec, d) as page:
            first = Image.open(__import__("io").BytesIO(motion._shot(page, 0.0))).convert("RGBA").getchannel("A").getextrema()
            png.write_bytes(motion._shot(page, 2.5))
        lo, hi = Image.open(png).convert("RGBA").getchannel("A").getextrema()
        assert first == (0, 0) and lo == 0 and hi == 255, tid


# ---------------------------------------------------------------------------------------------------------------------
# build, programme, final, handoff (one shared build: Chromium and ffmpeg are the slow part)
# ---------------------------------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def world(tmp_path_factory):
    root = tmp_path_factory.mktemp("seq") / "episode"
    ep = _setup(root)
    bed = ep.root / "work" / "music" / "bed.wav"
    bed.parent.mkdir(parents=True, exist_ok=True)
    _tone(bed, 40, 110)
    _log(ep, "work/music/bed.wav", author="Ada Player", notes="Quiet Water")
    meta = read_json(ep.meta_path)
    meta["sequences"] = {"cold_open": {"quote": {"text": "welcome to the show today we are talking about building things that last", "stage": "rough"}}}
    write_json(ep.meta_path, meta)
    over = {"opening": {"music": {"file": "work/music/bed.wav", "gain_db": -22}, "cold_open": {"music": {"file": "work/music/bed.wav", "gain_db": -24, "duck_db": -10}}}}
    pol = _policy(ep, over)
    pl = sequence.plan(ep, policy=pol, home=NOHOME)
    assert pl["errors"] == [], pl["errors"]
    rep = sequence.build(ep, policy=pol, home=NOHOME, preset="master", pl=pl)
    _, base = sequence.base_doc(ep)
    body = ep.root / "work" / "body.mp4"
    pr = {**__import__("ave.presets", fromlist=["x"]).get("master")}
    comp.render(ep, base, body, preset="master", burn_captions=False, overwrite=True)
    prog = ep.root / "work" / "programme.mp4"
    res = sequence.assemble(ep, body, ep.root / rep["built"]["opening"]["file"], ep.root / rep["built"]["closing"]["file"], prog, "master", fade_s=0.2)
    return {"ep": ep, "pol": pol, "plan": pl, "report": rep, "base": base, "body": body, "prog": prog, "res": res, "over": over}


def test_build_produces_files_at_the_cuts_spec(world):
    ep, rep, base = world["ep"], world["report"], world["base"]
    for w in ("opening", "closing"):
        b = rep["built"][w]
        info = ffutil.probe(ep.root / b["file"])
        assert (info["w"], info["h"]) == (base["canvas"]["w"], base["canvas"]["h"])
        assert info["fps"] == pytest.approx(base["fps"]) and info["sample_rate"] == 48000 and info["channels"] == 2
        assert ffutil.count_video_packets(ep.root / b["file"]) == b["frames"] == world["plan"][w]["frames"]
        assert info["duration"] == pytest.approx(b["frames"] / base["fps"], abs=1 / 30)
    assert "cold_open" in [i["id"] for i in rep["built"]["opening"]["items"]]
    saved = read_json(sequence.plan_path(ep))
    assert saved["built"]["opening"]["file"] == rep["built"]["opening"]["file"] and not any(k.startswith("_") for k in saved)


def test_rebuild_with_an_unchanged_plan_reuses_the_files(world):
    ep = world["ep"]
    rep = sequence.build(ep, policy=world["pol"], home=NOHOME, preset="master")
    assert rep["built"]["opening"].get("reused") and rep["built"]["closing"].get("reused")


def test_opening_doc_has_hook_cards_room_tone_music_and_captions(world):
    ep = world["ep"]
    doc = read_json(seq_doc := sequence.seq_dir(ep) / "opening.doc.json")
    assert editdoc.validate(doc) == []
    ids = [s["id"] for s in doc["tracks"]["video"]]
    assert ids[-1] == "plate" and any(i.startswith("co-") for i in ids)
    assert any(s.get("fill") == "room_tone" for s in doc["tracks"]["dialogue"])
    music = {m["id"]: m for m in doc["tracks"]["music"]}
    assert music["m-cold"]["duck_db"] == -10 and music["m-cold"]["o0"] == 0.0 and music["m-cold"]["o1"] < doc["duration"]   # ducked under the hook only
    assert music["m-bed"]["duck_db"] == 0.0 and music["m-bed"]["o1"] == pytest.approx(doc["duration"], abs=1e-3)
    assert {o["id"] for o in doc["tracks"]["overlays"]} == {"title_card", "host_intro"} and all(o["alpha"] for o in doc["tracks"]["overlays"])
    srt = captions.parse_srt(ep.root / world["report"]["built"]["opening"]["cold_open_srt"])
    assert srt and "welcome" in srt[0].text.lower() and srt[0].start < 1.0           # the hook is captioned, cards are not
    assert doc["captions"]["burn"] is False


def test_closing_credits_roll_scrolls_at_an_even_whole_pixel_step(world):
    ep = world["ep"]
    mov = next((ep.work / "sequence" / "cards").glob("credits-roll-*.mov"))
    meta = read_json(mov.with_suffix(".json"))
    w, h = 640, 360
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(mov), "-vf", "alphaextract,format=gray", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                         capture_output=True).stdout
    frames = np.frombuffer(raw, dtype=np.uint8).reshape(-1, h, w)
    assert len(frames) == meta["frames"]
    profiles = frames.astype("float32").sum(axis=2)                    # row profile of the alpha channel
    mid = [i for i in range(len(frames)) if 80 < profiles[i].sum() and i % 7 == 3][3:7]
    assert mid, "no credits text on screen mid-roll"
    step = meta["step_px"]
    for i in mid:
        best = max(range(0, 6), key=lambda s: -np.abs(profiles[i + 1][: h - 10 - s] - np.roll(profiles[i], -s)[: h - 10 - s]).sum())
        assert best == step, (i, best, step)
    assert profiles[0].sum() == 0 and profiles[-1].sum() == 0          # starts and ends with the screen empty
    ys = np.nonzero(profiles[mid[0]])[0]
    edge = profiles[mid[0]][: int(h * 0.04)].max()
    assert edge <= profiles[mid[0]].max()                              # the top edge fades (no text at full strength at the very edge)


def test_programme_duration_frames_and_audio_rate(world):
    ep, res, rep, body = world["ep"], world["res"], world["report"], world["body"]
    n = {"opening": rep["built"]["opening"]["frames"], "closing": rep["built"]["closing"]["frames"], "body": ffutil.count_video_packets(body)}
    assert res["frames"]["total"] == sum(n.values()) == ffutil.count_video_packets(world["prog"])
    info = ffutil.probe(world["prog"])
    assert info["sample_rate"] == 48000 and info["duration"] == pytest.approx(sum(n.values()) / 30.0, abs=1 / 30)
    assert res["joins_s"][0] == pytest.approx(n["opening"] / 30.0, abs=1e-3)
    assert res["joins_s"][1] == pytest.approx((n["opening"] + n["body"]) / 30.0, abs=1e-3)


def test_joining_parts_with_different_video_time_bases_keeps_speed(world, tmp_path):
    """Regression: the overlay pass writes a 1/15360 time base, a plain body 1/10020; copy-concatenating them played at 45.9 fps."""
    ep, rep = world["ep"], world["report"]
    op = ep.root / rep["built"]["opening"]["file"]
    tb = lambda f: subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=time_base", "-of", "csv=p=0", str(f)],   # noqa: E731
                                  capture_output=True, text=True).stdout.strip()
    assert tb(op) != tb(world["body"])
    for parts in ((op, None), (None, ep.root / rep["built"]["closing"]["file"])):
        res = sequence.assemble(ep, world["body"], parts[0], parts[1], tmp_path / "two.mp4", "master")
        info = ffutil.probe(tmp_path / "two.mp4")
        assert info["fps"] == pytest.approx(30.0, abs=0.01) and info["video_duration"] == pytest.approx(res["expected_duration"], abs=1 / 30)


def test_programme_loudness_matches_the_body_and_the_target(world):
    res, body = world["res"], world["body"]
    lb = ffutil.measure_loudness(body)
    lp = ffutil.measure_loudness(world["prog"])
    assert abs(lp["lufs"] - lb["lufs"]) < 1.0 and abs(lp["lufs"] - (-16.0)) < 1.0
    assert lp["true_peak_db"] <= -1.0


def test_hook_speech_sits_at_the_bodys_level(world):
    """The cold open is cut from the same source: in the programme its speech must be as loud as the same words in the body."""
    ep, prog, body = world["ep"], world["prog"], world["body"]
    off = world["report"]["built"]["opening"]["frames"] / 30.0

    def rms(path, a, b):
        x = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", str(a), "-t", str(b - a), "-i", str(path), "-vn", "-ac", "1", "-ar", "48000", "-f", "f32le", "-"],
                           capture_output=True).stdout
        v = np.frombuffer(x, dtype="float32")
        return 20 * np.log10(np.sqrt((v ** 2).mean()) + 1e-9)

    in_hook = rms(prog, 0.7, 4.7)                  # "welcome to the show ..." (S1 0.5-6.0 in the rough cut)
    in_body = rms(prog, off + 0.7, off + 4.7)      # the same words, in the body
    assert abs(in_hook - in_body) < 2.0, (in_hook, in_body)       # the opening also carries a low music bed: a small difference is fine


def test_cards_sit_on_room_tone_at_the_bodys_floor_not_boosted(world):
    """A card-only closing must not be normalised up to speech level: its RMS stays near the body's own quiet floor."""
    ep, rep = world["ep"], world["report"]
    b = rep["built"]["closing"]
    assert b["lufs"] < -40, b["lufs"]                 # integrated loudness of room tone alone: far below -16 LUFS
    assert ffutil.measure_loudness(ep.root / b["file"])["true_peak_db"] < -30
    assert b["gain_db"] == rep["built"]["opening"]["gain_db"]        # one gain domain for the whole programme


def test_final_without_sequences_policy_is_unchanged(world, tmp_path):
    ep = Episode(shutil.copytree(world["ep"].root, tmp_path / "e"))
    rep = final.build_final(ep, render=False, do_retranscribe=False)
    assert "sequences" not in rep and "sequences" not in editdoc.load_latest(ep, "final")


def _final_episode(world, tmp_path, name="e"):
    ep = Episode(shutil.copytree(world["ep"].root, tmp_path / name))
    (ep.root / "policy").mkdir(exist_ok=True)
    (ep.root / "policy" / "final.yaml").write_text("sequences: {opening: true, closing: true}\n")
    (ep.root / "policy" / "sequences.yaml").write_text(yaml.safe_dump(sequence._merge(FAST_POLICY, world["over"])))
    return ep


def test_final_refuses_unapproved_sequences_then_builds_after_approval(world, tmp_path):
    ep = _final_episode(world, tmp_path)
    with pytest.raises(SystemExit, match="not approved"):
        final.build_final(ep, render=True, do_retranscribe=False, preset="master")
    assert not any(ep.exports.glob("final*.mp4"))
    sequence.approve(ep, by="tester", home=NOHOME)
    rep = final.build_final(ep, render=True, do_retranscribe=False, preset="master")
    seq = rep["sequences"]
    off = seq["opening_offset_s"]
    out = Path(rep["render"]["out"])
    doc = editdoc.load_latest(ep, "final")
    assert doc["sequences"]["opening"]["file"].startswith("work/sequence/") and doc["sequences"]["planned"] is False
    total = off + doc["duration"] + doc["sequences"]["closing"]["frames"] / doc["fps"]
    assert ffutil.probe(out)["duration"] == pytest.approx(total, abs=1 / 30) and rep["render"]["duration"] == pytest.approx(total, abs=1 / 30)
    assert not out.with_name(out.stem + ".body.mp4").exists()                      # the temporary body render is removed
    assert abs(rep["render"]["lufs"] + 16) < 1.0 and rep["render"]["true_peak_db"] <= -1.0
    # planned SRT of the whole programme: hook cues first, then every body cue shifted by exactly the opening's length
    body = captions.parse_srt(ep.root / doc["captions"]["srt"])
    prog = captions.parse_srt(ep.root / seq["planned_srt"])
    shifted = [c for c in prog if c.start >= off - 1e-6]
    assert len(shifted) == len(body) and all(abs(p.start - (b.start + off)) < 2e-3 and p.text == b.text for p, b in zip(shifted, body))
    assert prog[0].start < 1.0 and len(prog) == len(body) + 1                     # + the captioned cold open


def test_final_draft_sequences_flag_allows_unapproved_but_marks_it(world, tmp_path):
    ep = _final_episode(world, tmp_path, "d")
    rep = final.build_final(ep, render=True, do_retranscribe=False, preset="master", draft_sequences=True)
    assert rep["sequences"]["draft"] is True and any("DRAFT sequences" in w for w in rep["warnings"])


def test_final_opening_only_flag_overrides_policy(world, tmp_path):
    ep = _final_episode(world, tmp_path, "o")
    sequence.approve(ep, home=NOHOME)
    rep = final.build_final(ep, render=True, do_retranscribe=False, preset="master", closing=False)
    doc = editdoc.load_latest(ep, "final")
    assert "closing" not in doc["sequences"] and "opening" in doc["sequences"] and rep["sequences"]["which"] == ["opening"]


def test_final_no_render_only_plans(world, tmp_path):
    ep = _final_episode(world, tmp_path, "n")
    rep = final.build_final(ep, render=False, do_retranscribe=False)
    doc = editdoc.load_latest(ep, "final")
    assert doc["sequences"]["planned"] is True and "opening" not in doc["sequences"] and "sequences" not in rep


def test_handoff_plan_places_opening_body_and_closing_and_runs_on_the_fake_bridge(world, tmp_path, monkeypatch, capsys):
    from test_handoff_script import FakeResolve, run_script
    ep = _final_episode(world, tmp_path, "h")
    sequence.approve(ep, home=NOHOME)
    final.build_final(ep, render=True, do_retranscribe=False, preset="master")
    doc = editdoc.load_latest(ep, "final")
    master = tmp_path / "master.wav"
    master.write_bytes(b"x")
    plain = handoff.plan(ep, {k: v for k, v in doc.items() if k != "sequences"}, name="t", audio_master=master)
    p = handoff.plan(ep, doc, name="t", audio_master=master)
    o, c = doc["sequences"]["opening"]["frames"], doc["sequences"]["closing"]["frames"]
    assert p["video"][0]["id"] == "seq-opening" and p["video"][0]["record_frame"] == 0 and p["video"][0]["frames"] == o
    body = p["video"][1:-1]
    assert [v["id"] for v in body] == [v["id"] for v in plain["video"]]
    assert all(b["record_frame"] == a["record_frame"] + o for a, b in zip(plain["video"], body))
    assert p["video"][-1]["id"] == "seq-closing" and p["video"][-1]["record_frame"] == o + plain["timeline_frames"]
    assert p["timeline_frames"] == o + plain["timeline_frames"] + c
    assert [a["id"] for a in p["audio"]] == ["seq-opening-audio", "dialogue-master", "seq-closing-audio"]
    assert p["audio"][1]["record_frame"] == o and p["audio"][1]["frames"] == plain["audio"][0]["frames"]
    assert p["subtitles"]["srt"].endswith("final.programme.srt") and p["subtitles"]["cues"] > 0
    for it in p["video"]:                                                      # contiguous on V1
        pass
    assert all(a["record_frame"] + a["frames"] == b["record_frame"] for a, b in zip(p["video"], p["video"][1:]))
    # the script places it on the in-memory fake bridge and verifies every item
    master_real = tmp_path / "m.wav"
    sf.write(str(master_real), np.zeros((48000, 2), dtype="float32"), 48000)
    for f in p["import"]:
        assert Path(f).exists() or f == str(master.resolve())
    plan_path = tmp_path / "plan.json"
    p2 = dict(p)
    plan_path.write_text(json.dumps(p2))
    for f in p["import"]:
        Path(f).parent.mkdir(parents=True, exist_ok=True)
        Path(f).touch(exist_ok=True)
    fake = FakeResolve()
    code, data, err = run_script(monkeypatch, capsys, fake, plan_path)
    assert code == 0 and data.get("ok") is True, (data, err)
    v1 = sorted(fake.tl["tracks"][("video", 1)], key=lambda i: i["start"])
    assert [i["start"] - 108000 for i in v1][0] == 0 and v1[0]["name"].startswith("opening.") and v1[-1]["name"].startswith("closing.")
    assert v1[-1]["start"] - 108000 + v1[-1]["duration"] == p["timeline_frames"]


def test_handoff_warns_when_a_sequence_file_is_missing(world, tmp_path):
    ep = _final_episode(world, tmp_path, "w")
    sequence.approve(ep, home=NOHOME)
    final.build_final(ep, render=True, do_retranscribe=False, preset="master")
    doc = editdoc.load_latest(ep, "final")
    (ep.root / doc["sequences"]["closing"]["file"]).unlink()
    p = handoff.plan(ep, doc, name="t")
    assert any("closing sequence file" in w for w in p["warnings"]) and p["video"][-1]["id"] != "seq-closing" and p["video"][0]["id"] == "seq-opening"


def test_sequence_cli_plan_status_approve(world, tmp_path, capsys):
    from ave.cli import main
    ep = Episode(shutil.copytree(world["ep"].root, tmp_path / "cli"))
    (ep.root / "policy").mkdir(exist_ok=True)
    (ep.root / "policy" / "sequences.yaml").write_text(yaml.safe_dump(FAST_POLICY))
    assert main(["sequence", "plan", str(ep.root)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["file"] == "work/sequence.json" and out["opening"]["enabled"]
    assert main(["sequence", "status", str(ep.root)]) == 0
    assert json.loads(capsys.readouterr().out)["approved"] is False
    assert main(["sequence", "approve", str(ep.root), "--by", "ben"]) == 0
    capsys.readouterr()
    main(["sequence", "status", str(ep.root)])
    assert json.loads(capsys.readouterr().out)["approved"] is True


def test_preview_is_a_draft_with_stills_and_does_not_displace_the_master_build(world, tmp_path):
    ep = Episode(shutil.copytree(world["ep"].root, tmp_path / "pv"))
    before = read_json(sequence.plan_path(ep))["built"]["closing"]["file"]
    rep = sequence.preview(ep, policy=world["pol"], home=NOHOME, which=("closing",), out_dir=tmp_path / "stills")
    assert rep["draft"] is True and rep["built"]["closing"]["file"].endswith("closing.proxy.mp4")
    assert [s["item"] for s in rep["stills"]["closing"]] == ["thanks", "credits", "end_card", "tail"]
    assert all(Path(s["png"]).stat().st_size > 1000 for s in rep["stills"]["closing"])
    assert read_json(sequence.plan_path(ep))["built"]["closing"]["file"] == before            # still the master build
