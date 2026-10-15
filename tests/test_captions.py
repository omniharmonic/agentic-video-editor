"""Caption logic: transcript -> output-time cues, segmentation rules, SRT/ASS files. No rendering here."""

import copy
import json
import re

import pytest

import fixtures
from ave import captions, editdoc
from ave.episode import read_json, write_json


# ---- helpers -----------------------------------------------------------------------------------

def make_edit(ep, keeps, stage="rough"):
    """A valid EditDoc that keeps only the given episode ranges (video + dialogue, one segment per range)."""
    base = editdoc.load_latest(ep, "assembly")
    doc = editdoc.new_doc(stage, base["fps"], (base["canvas"]["w"], base["canvas"]["h"]), base["sources"])
    o = 0.0
    for i, (a, b) in enumerate(keeps, 1):
        doc["tracks"]["video"].append({"id": f"v{i}", "src": "A", "o0": o, "o1": o + b - a, "e0": a, "crop": None, "zoom": 1.0})
        doc["tracks"]["dialogue"].append({"id": f"d{i}", "src": "A", "o0": o, "o1": o + b - a, "e0": a, "xfade_ms": 0, "gain_db": 0.0})
        o += b - a
    doc["duration"] = o
    assert editdoc.validate(doc) == []
    return doc


def talk(text, start=0.0, wps=2.8, speaker="S1", gap_before=0.0):
    """Words for `text` at a steady pace; punctuation stays attached to the word like real ASR output."""
    out, t = [], start + gap_before
    for tok in text.split():
        d = max(0.2, len(tok) * 0.07)          # ~15 characters per second, like relaxed speech
        out.append({"w": tok, "start": round(t, 3), "end": round(t + d, 3), "conf": 0.9, "speaker": speaker})
        t += d + 0.05
    return out


LONG_TEXT = ("So the thing nobody tells you about building a company is that most of the work is invisible, and honestly "
             "that is the part I love. We spent two years on plumbing before anyone clapped. Then one morning the whole "
             "system just worked, which felt strange, because nothing about it looked different from the outside.")


# ---- cues_from_edit ----------------------------------------------------------------------------

def test_cues_from_edit_maps_to_output_time_and_drops_removed_words(synth_episode):
    ep = synth_episode
    keeps = [(0.0, 6.0), (8.0, 13.0), (13.4, 16.0), (17.0, 30.0)]   # dead air and the "um" removed
    doc = make_edit(ep, keeps)
    cues = captions.cues_from_edit(ep, doc)
    words = [w for c in cues for w in c.words]
    texts = [w["w"] for w in words]
    assert "um" not in texts
    assert "thanks" in texts and "welcome" in texts
    thanks = next(w for w in words if w["w"] == "thanks")
    assert thanks["start"] == pytest.approx(6.0, abs=1e-6)          # episode 8.0 sits right after the 6 s kept block
    # every mapped word lies inside the doc and nothing precedes its predecessor
    starts = [w["start"] for w in words]
    assert starts == sorted(starts) and starts[-1] < doc["duration"]
    # no word from a removed range survives: count equals transcript words minus the "um"
    src_words = [w for w in fixtures.words() if w["w"] != "um"]
    assert len(words) == len(src_words)


def test_cues_from_edit_drops_partial_cut_words_and_keeps_order(synth_episode):
    ep = synth_episode
    # cut straight through the middle of the first sentence (words spread over 0.5-6.0 s)
    doc = make_edit(ep, [(0.0, 2.0), (4.0, 30.0)])
    words = [w for c in captions.cues_from_edit(ep, doc) for w in c.words]
    src = fixtures.words()
    kept = [w for w in src if (w["start"] + w["end"]) / 2 < 2.0 or (w["start"] + w["end"]) / 2 >= 4.0]
    assert [w["w"] for w in words] == [w["w"] for w in kept if w["w"] != "um"]
    for w in words:
        assert 0 <= w["start"] < w["end"] <= doc["duration"] + 1e-6


def test_fill_segments_produce_no_words(synth_episode):
    ep = synth_episode
    doc = make_edit(ep, [(0.0, 6.0), (8.0, 30.0)])
    # replace the second dialogue segment by room tone: its words must not be captioned
    doc["tracks"]["dialogue"][1] = {"id": "d2", "o0": 6.0, "o1": doc["duration"], "fill": "room_tone"}
    words = [w for c in captions.cues_from_edit(ep, doc) for w in c.words]
    assert words and max(w["end"] for w in words) <= 6.0 + 1e-6


def test_cues_from_edit_applies_episode_glossary(synth_episode):
    ep = synth_episode
    meta = read_json(ep.meta_path)
    meta["glossary"] = {"welcome": "Welcome", "building things": "Building Things"}
    write_json(ep.meta_path, meta)
    doc = make_edit(ep, [(0.0, 30.0)])
    text = " ".join(c.text.replace("\n", " ") for c in captions.cues_from_edit(ep, doc))
    assert "Welcome to the show" in text and "Building Things that last" in text


# ---- segmentation rules ------------------------------------------------------------------------

@pytest.fixture(scope="module")
def long_cues():
    return captions.cues_from_words(talk(LONG_TEXT * 3), captions.get_style("podcast")["rules"])


def test_long_form_rules_hold(long_cues):
    rules = captions.get_style("podcast")["rules"]
    assert len(long_cues) >= 6
    for c in long_cues:
        lines = c.text.split("\n")
        assert len(lines) <= rules["max_lines"]
        assert all(len(l) <= rules["max_chars_per_line"] for l in lines)
        if len(lines) == 2:   # never leave a one-word orphan line
            assert all(len(l.split()) >= 2 for l in lines), c.text
        assert c.end - c.start >= rules["min_cue_s"] - 1e-9
        assert c.end - c.start <= rules["max_cue_s"] + 1e-9
    for a, b in zip(long_cues, long_cues[1:]):
        assert b.start - a.end >= rules["min_gap_s"] - 1e-9, "cues overlap or touch"


def test_cues_keep_every_word_in_order(long_cues):
    src = talk(LONG_TEXT * 3)
    assert " ".join(w["w"] for c in long_cues for w in c.words) == " ".join(w["w"] for w in src)
    assert " ".join(c.text.replace("\n", " ") for c in long_cues) == " ".join(w["w"] for w in src)


def test_breaks_prefer_punctuation_over_mid_phrase(long_cues):
    ends = [c.text.rstrip()[-1] for c in long_cues[:-1]]
    assert sum(ch in ".,;:?!" for ch in ends) >= len(ends) * 0.5, ends


def test_pause_and_speaker_change_start_new_cues():
    a = talk("hello there my friend", 0.0, speaker="S1")
    b = talk("hi how are you", a[-1]["end"] + 0.05, speaker="S2")     # same breath, new speaker
    c = talk("fine thanks a lot", b[-1]["end"] + 1.5, speaker="S2")   # long silence
    cues = captions.cues_from_words(a + b + c, captions.get_style("podcast")["rules"])
    assert [x.text for x in cues] == ["hello there my friend", "hi how are you", "fine thanks a lot"]
    assert [x.speaker for x in cues] == ["S1", "S2", "S2"]


def test_short_cue_is_extended_to_minimum_without_overlapping_next():
    rules = captions.get_style("podcast")["rules"]
    w = talk("yes", 0.0)[:1] + talk("and then we went on to talk about it for a while", 3.0)
    cues = captions.cues_from_words(w, rules)
    assert cues[0].text == "yes" and cues[0].end - cues[0].start >= rules["min_cue_s"] - 1e-9
    assert cues[0].end <= cues[1].start - rules["min_gap_s"] + 1e-9


def test_reading_speed_extends_into_gap_when_possible():
    rules = captions.get_style("podcast")["rules"]
    fast = [{"w": "extraordinarily", "start": 0.0, "end": 0.3, "speaker": "S1"},
            {"w": "complicated", "start": 0.3, "end": 0.6, "speaker": "S1"},
            {"w": "arrangements", "start": 0.6, "end": 0.9, "speaker": "S1"}]
    cues = captions.cues_from_words(fast + talk("later on", 5.0), rules)
    c = cues[0]
    assert len(c.text) / (c.end - c.start) <= rules["max_cps"] + 1e-6
    assert captions.check(cues, captions.get_style("podcast")) == []


def test_fillers_dropped_by_policy():
    cues = captions.cues_from_words(talk("so um we uh went home"), captions.get_style("podcast")["rules"])
    assert cues[0].text == "so we went home"
    rules = dict(captions.get_style("podcast")["rules"], drop_fillers=[])
    assert "um" in captions.cues_from_words(talk("so um we went home"), rules)[0].text


def test_glossary_wrong_to_right_and_capitalisation():
    w = talk("we met with open ai and then maren said that the cloud flare team agreed")
    gl = {"open ai": "OpenAI", "cloud flare": "Cloudflare", "Maren": "Maren"}
    text = " ".join(c.text.replace("\n", " ") for c in captions.cues_from_words(w, captions.get_style("podcast")["rules"], gl))
    assert "OpenAI" in text and "Cloudflare" in text and "Maren said" in text
    assert "open ai" not in text and "cloud flare" not in text
    # list form = proper names with their correct capitalisation
    text2 = " ".join(c.text.replace("\n", " ") for c in
                     captions.cues_from_words(talk("ask maren about it"), captions.get_style("podcast")["rules"], ["Maren"]))
    assert text2 == "ask Maren about it"


def test_short_style_rules_are_tighter():
    rules = captions.get_style("short")["rules"]
    cues = captions.cues_from_words(talk(LONG_TEXT), rules)
    assert rules["max_chars_per_line"] <= 32
    for c in cues:
        assert all(len(l) <= rules["max_chars_per_line"] for l in c.text.split("\n"))
        assert c.end - c.start <= rules["max_cue_s"] + 1e-9


# ---- check -------------------------------------------------------------------------------------

def test_check_flags_bad_input_and_passes_good(long_cues):
    style = captions.get_style("podcast")
    assert captions.check(long_cues, style) == []
    C = captions.Cue
    bad = [C(0.0, 0.3, "x" * 60), C(0.2, 1.5, "a\nb\nc"), C(2.0, 2.5, "one\ntwo words here")]
    bad.append(C(10.0, 18.0, "slow"))
    kinds = {p["kind"] for p in captions.check(bad, style)}
    assert {"line_too_long", "too_many_lines", "overlap", "too_short", "too_long", "reading_speed", "orphan_line"} <= kinds
    for p in captions.check(bad, style):
        assert {"cue", "kind", "detail"} <= set(p)


# ---- SRT ---------------------------------------------------------------------------------------

def test_srt_is_valid_and_round_trips(tmp_path, long_cues):
    p = tmp_path / "x.srt"
    captions.write_srt(long_cues, p)
    raw = p.read_text()
    blocks = [b for b in raw.strip().split("\n\n")]
    assert len(blocks) == len(long_cues)
    pat = re.compile(r"^\d+\n\d\d:\d\d:\d\d,\d{3} --> \d\d:\d\d:\d\d,\d{3}\n.+", re.S)
    assert all(pat.match(b) for b in blocks)
    assert [int(b.split("\n")[0]) for b in blocks] == list(range(1, len(long_cues) + 1))
    back = captions.parse_srt(p)
    assert [c.text for c in back] == [c.text for c in long_cues]
    for a, b in zip(back, long_cues):
        assert a.start == pytest.approx(b.start, abs=0.0011) and a.end == pytest.approx(b.end, abs=0.0011)


def test_srt_timestamps_over_an_hour(tmp_path):
    p = tmp_path / "h.srt"
    captions.write_srt([captions.Cue(3725.5, 3727.0, "late")], p)
    assert "01:02:05,500 --> 01:02:07,000" in p.read_text()


# ---- ASS ---------------------------------------------------------------------------------------

def _events(path):
    return [l for l in path.read_text().splitlines() if l.startswith("Dialogue:")]


def _cs(ts):
    h, m, s = ts.split(":")
    return round((int(h) * 3600 + int(m) * 60 + float(s)) * 100)


def test_karaoke_has_one_kf_per_word_summing_to_cue_duration(tmp_path):
    style = captions.get_style("short")
    cues = captions.cues_from_words(talk(LONG_TEXT), style["rules"])
    p = tmp_path / "k.ass"
    captions.write_ass(cues, style, (1080, 1920), p)
    ev = _events(p)
    assert len(ev) == len(cues)
    for line, cue in zip(ev, cues):
        f = line.split(",", 9)
        durs = [int(x) for x in re.findall(r"\\kf(\d+)", f[9])]
        assert len(durs) == len(cue.words)
        assert sum(durs) == _cs(f[2]) - _cs(f[1])
        assert all(d >= 1 for d in durs)


def test_non_karaoke_ass_has_plain_text_and_correct_header(tmp_path):
    style = captions.get_style("podcast")
    cues = captions.cues_from_words(talk(LONG_TEXT), style["rules"])
    p = tmp_path / "p.ass"
    captions.write_ass(cues, style, (1920, 1080), p)
    txt = p.read_text()
    assert "PlayResX: 1920" in txt and "PlayResY: 1080" in txt
    assert "\\kf" not in txt
    back = captions.parse_ass(p)
    assert [c.text for c in back] == [c.text for c in cues]
    for a, b in zip(back, cues):
        assert a.start == pytest.approx(b.start, abs=0.0101) and a.end == pytest.approx(b.end, abs=0.0101)


def test_ass_text_escapes_braces_and_backslashes(tmp_path):
    style = captions.get_style("podcast")
    p = tmp_path / "e.ass"
    captions.write_ass([captions.Cue(0, 2, "use {curly} \\ braces")], style, (1920, 1080), p)
    line = _events(p)[0]
    assert "{curly}" not in line.split(",", 9)[9]


def test_ass_geometry_scales_with_canvas(tmp_path):
    style = captions.get_style("podcast")
    cues = [captions.Cue(0, 2, "hello world")]
    sizes = {}
    for cv in ((1920, 1080), (3840, 2160), (1080, 1920)):
        p = tmp_path / f"{cv[0]}.ass"
        info = captions.write_ass(cues, style, cv, p)
        sizes[cv] = info["font_size"]
    assert sizes[(3840, 2160)] == pytest.approx(2 * sizes[(1920, 1080)], rel=0.03)
    assert sizes[(1080, 1920)] == pytest.approx(sizes[(1920, 1080)], rel=0.03)   # short side is 1080 in both


def test_font_is_never_a_hard_failure(tmp_path):
    style = copy.deepcopy(captions.get_style("podcast"))
    style["fonts"] = ["Definitely Not A Font 9000"]
    info = captions.write_ass([captions.Cue(0, 2, "hello")], style, (1920, 1080), tmp_path / "f.ass")
    assert info["font"] and any("font" in w.lower() for w in info["warnings"])


def test_bundled_fonts_dir_is_preferred(tmp_path):
    """A font file in a fonts dir is picked by its internal family name before system fonts."""
    from PIL import ImageFont
    sys_font = next((p for p in ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Verdana.ttf")
                     if __import__("pathlib").Path(p).exists()), None)
    if not sys_font:
        pytest.skip("no system TTF to copy")
    d = tmp_path / "fonts"
    d.mkdir()
    (d / "Bundled.ttf").write_bytes(open(sys_font, "rb").read())
    family = ImageFont.truetype(str(d / "Bundled.ttf"), 20).getname()[0]
    style = copy.deepcopy(captions.get_style("podcast"))
    style["fonts"] = [family]
    info = captions.write_ass([captions.Cue(0, 2, "hi")], style, (1920, 1080), tmp_path / "b.ass", fonts_dirs=[d])
    assert info["font"] == family and info["font_file"].startswith(str(d))


def test_styles_load_and_override_by_episode(synth_episode):
    ep = synth_episode
    assert {"podcast", "short", "minimal"} <= set(captions.load_styles()["styles"])
    (ep.root / "policy").mkdir()
    (ep.root / "policy" / "captions.styles.yaml").write_text("rules:\n  long:\n    max_chars_per_line: 30\n")
    s = captions.get_style("podcast", ep=ep)
    assert s["rules"]["max_chars_per_line"] == 30 and s["rules"]["max_lines"] == 2   # partial override merges
    with pytest.raises(KeyError):
        captions.get_style("nope")


def test_write_for_edit_records_new_edit_version(synth_episode):
    ep = synth_episode
    doc = make_edit(ep, [(0.0, 6.0), (8.0, 30.0)])
    editdoc.save_new_version(ep, doc)
    res = captions.make_captions(ep, "rough", "podcast")
    assert (ep.root / res["srt"]).exists() and (ep.root / res["ass"]).exists()
    assert res["srt"] == "captions/rough.srt" and res["ass"] == "captions/rough.ass"
    new = editdoc.load_latest(ep, "rough")
    assert new["captions"]["srt"] == "captions/rough.srt" and new["captions"]["ass"] == "captions/rough.ass"
    assert editdoc.list_versions(ep, "rough") == [1, 2]
    assert editdoc.load(editdoc.path_for(ep, "rough", 1))["captions"]["srt"] is None   # v1 untouched
    assert res["problems"] == []
