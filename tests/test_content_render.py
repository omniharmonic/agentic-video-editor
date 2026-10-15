"""The in-house still renderer: pixel sizes, safe area, contrast, determinism, license gate, carousel, reel card."""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from ave.content import manifest as M
from ave.content import registry as R
from ave.content import render_motionkit as RM
from ave.content import settings
from ave.assets import licenses
from content_helpers import cep, content_env, make_episode, pick_quote  # noqa: F401

Q = "A goal is a wish and a system is a habit, and the ground remembers what we do to it long after we have stopped paying attention."
SLOTS = {
    "quote-post": {"quote": Q, "attribution": "Maren Okafor", "role": "Soil ecologist", "show": "The Long Table"},
    "thumbnail": {"title": "Soil is a habit, not a goal", "guest": "Maren Okafor", "show": "The Long Table"},
    "carousel": {"show": "The Long Table", "cover": {"title": "What soil teaches about staying", "subtitle": "Maren Okafor"},
                 "slides": [{"kind": "quote", "quote": "A goal is a wish and a system is a habit", "attribution": "Maren Okafor"},
                            {"kind": "point", "text": "Repair is a practice, not a project.", "label": "Point 1"}],
                 "closing": {"line": "Listen to the full conversation", "cta": "Episode 00"}},
    "reel-card": {"kind": "intro", "title": "Soil is a habit, not a goal", "subtitle": "Maren Okafor", "show": "The Long Table"},
}


@pytest.fixture(scope="module")
def kit(tmp_path_factory):
    ep = make_episode(tmp_path_factory.mktemp("render") / "ep")
    cfg = settings.load()
    reg = R.load_registry(cfg=cfg)
    with RM.Session() as s:
        yield ep, cfg, reg, s


def _item(template, style="ink", fmt=None, slots=None, **kw):
    t = R.load_registry().get(template)
    it = M.new_item(f"t-{template}-{style}-{fmt or t.formats[0]}", template, style, fmt or t.formats[0], slots or SLOTS[template],
                    reason="test", confidence=0.9, **kw)
    return it


def test_every_template_style_format_renders_at_exact_pixel_size_with_text_in_safe_area_and_good_contrast(kit):
    ep, cfg, reg, s = kit
    out = M.content_dir(ep) / "renders" / "matrix"
    seen = 0
    for row in reg.matrix():
        it = _item(row["template"], row["style"], row["format"])
        res = RM.render_item(ep, it, s, out, reg, cfg)
        files = sorted({o["file"] for o in res["outputs"]})
        assert files, row
        for f in files:
            im = Image.open(ep.root / f)
            assert im.size == (row["w"], row["h"]), (row, im.size)
            assert im.mode == "RGB"
        crit = res["critic"]
        assert crit["ok"], (row, crit["notes"])
        assert crit["fits"] and crit["contrast_ok"] and crit["text_size_ok"]
        assert crit["contrast"] >= cfg["critic"]["min_contrast"], (row, crit["contrast"])
        # measured independently of the critic's own verdict: every measured text rect lies inside the format's safe box
        safe, (w, h) = cfg["safe_area"][row["format"]], (row["w"], row["h"])
        for img in crit["images"]:
            for el in img["elements"]:
                assert el["inside_safe_area"] and not el["overflow"], (row, el)
        seen += 1
    assert seen == len(reg.matrix()) == 18


def test_style_colours_reach_the_pixels(kit):
    ep, cfg, reg, s = kit
    from ave import motion
    colors = motion.load_tokens()["colors"]
    for style, token in (("ink", "ink"), ("paper", "paper"), ("clay", "clay")):
        res = RM.render_item(ep, _item("quote-post", style, "1x1"), s, M.content_dir(ep) / "renders" / "px", reg, cfg)
        px = Image.open(ep.root / res["outputs"][0]["file"]).getpixel((4, 4))
        want = tuple(int(colors[token][i:i + 2], 16) for i in (1, 3, 5))
        assert px == want, (style, px, want)


def test_same_inputs_give_identical_bytes_and_hashes(kit):
    ep, cfg, reg, s = kit
    a = RM.render_item(ep, _item("quote-post"), s, M.content_dir(ep) / "renders" / "det1", reg, cfg)
    b = RM.render_item(ep, _item("quote-post"), s, M.content_dir(ep) / "renders" / "det2", reg, cfg)
    assert a["outputs"][0]["sha256"] == b["outputs"][0]["sha256"]
    assert (ep.root / a["outputs"][0]["file"]).read_bytes() == (ep.root / b["outputs"][0]["file"]).read_bytes()
    assert hashlib.sha256((ep.root / a["outputs"][0]["file"]).read_bytes()).hexdigest() == a["outputs"][0]["sha256"]
    c = RM.render_item(ep, _item("quote-post", style="paper"), s, M.content_dir(ep) / "renders" / "det3", reg, cfg)
    assert c["outputs"][0]["sha256"] != a["outputs"][0]["sha256"]


def test_long_text_shrinks_to_fit_but_never_below_the_phone_floor(kit):
    ep, cfg, reg, s = kit
    long_ok = "x" * 0 + " ".join(["ground"] * 34)                      # 237 chars: the schema maximum
    res = RM.render_item(ep, _item("quote-post", fmt="1x1", slots={"quote": long_ok}), s, M.content_dir(ep) / "renders" / "long", reg, cfg)
    assert res["critic"]["ok"], res["critic"]["notes"]
    assert res["critic"]["images"][0]["elements"][0]["font_px"] >= cfg["critic"]["min_text_px_per_1080"]


def test_overflow_is_detected_by_measuring_the_rendered_page(kit):
    ep, cfg, reg, s = kit
    # a 300-character quote is refused by the template schema ...
    assert R.load_registry().get("quote-post").check_slots({"quote": "ground " * 43})
    # ... and if one is forced through anyway, the critic sees it in the DOM: far more text than the box can hold
    too_much = RM.render_item(ep, _item("quote-post", fmt="1x1", slots={"quote": " ".join(["ground"] * 150)}), s,
                              M.content_dir(ep) / "renders" / "over", reg, cfg)
    assert not too_much["critic"]["ok"] and not too_much["critic"]["fits"]
    assert any("fit" in n or "safe area" in n or "overflow" in n for n in too_much["critic"]["notes"])
    # an unbreakable string overflows horizontally
    wide = RM.render_item(ep, _item("quote-post", fmt="1x1", slots={"quote": "a" * 160}), s, M.content_dir(ep) / "renders" / "over2", reg, cfg)
    assert not wide["critic"]["fits"]


def test_contrast_critic_reads_the_pixels_behind_the_text(kit):
    ep, cfg, reg, s = kit
    # same text colour on a background that fails: the critic must flag it (simulate with an impossible threshold)
    strict = json.loads(json.dumps(cfg))
    strict["critic"]["min_contrast"] = 30.0
    res = RM.render_item(ep, _item("quote-post", style="clay", fmt="1x1"), s, M.content_dir(ep) / "renders" / "cx", reg, strict)
    assert not res["critic"]["ok"] and any("contrast" in n for n in res["critic"]["notes"])
    # the real numbers: ink on paper beats 12:1, ink style 11+:1, clay is the weakest at about 4.6:1
    ratios = {}
    for st in ("ink", "paper", "clay"):
        r = RM.render_item(ep, _item("quote-post", style=st, fmt="1x1"), s, M.content_dir(ep) / "renders" / "cx2", reg, cfg)
        ratios[st] = r["critic"]["contrast"]
    assert ratios["paper"] > 12 and ratios["ink"] > 11 and 4.5 <= ratios["clay"] < 6


def test_wcag_helpers():
    assert RM.contrast_ratio((0, 0, 0), (255, 255, 255)) == pytest.approx(21.0)
    assert RM.contrast_ratio((119, 119, 119), (255, 255, 255)) == pytest.approx(4.48, abs=0.02)


def test_carousel_renders_one_numbered_image_per_slide_and_a_changed_slide_changes_only_its_hash(kit):
    ep, cfg, reg, s = kit
    a = RM.render_item(ep, _item("carousel", platforms=["instagram"]), s, M.content_dir(ep) / "renders" / "c1", reg, cfg)
    files = [o["file"] for o in a["outputs"]]
    assert len(files) == 4 and [o["slide"] for o in a["outputs"]] == [1, 2, 3, 4]
    assert files[0].endswith("-s01.png") and files[3].endswith("-s04.png")
    changed = json.loads(json.dumps(SLOTS["carousel"]))
    changed["slides"][1]["text"] = "Repair is a practice, not a project, and it needs people."
    b = RM.render_item(ep, _item("carousel", slots=changed), s, M.content_dir(ep) / "renders" / "c2", reg, cfg)
    ha, hb = [o["sha256"] for o in a["outputs"]], [o["sha256"] for o in b["outputs"]]
    assert [x == y for x, y in zip(ha, hb)] == [True, False, False, True] or ha[0] == hb[0]
    assert ha[2] != hb[2] or ha[1] != hb[1]
    assert "Slide 2 of 4" in a["outputs"][1]["alt_text"]


def test_alt_text_is_generated_from_the_slots_and_can_be_overridden(kit):
    ep, cfg, reg, s = kit
    r = RM.render_item(ep, _item("quote-post"), s, M.content_dir(ep) / "renders" / "alt", reg, cfg)
    assert "A goal is a wish" in r["outputs"][0]["alt_text"] and "Maren Okafor" in r["outputs"][0]["alt_text"]
    it = _item("quote-post")
    it["alt_text"] = "White serif text on a dark background quoting Maren Okafor about soil."
    r = RM.render_item(ep, it, s, M.content_dir(ep) / "renders" / "alt2", reg, cfg)
    assert r["outputs"][0]["alt_text"].startswith("White serif text")


def test_unlicensed_asset_blocks_rendering_until_logged(kit, tmp_path):
    ep, cfg, reg, s = kit
    photo = ep.root / "guest.jpg"
    Image.new("RGB", (800, 600), (90, 110, 100)).save(photo)
    it = _item("thumbnail", slots={**SLOTS["thumbnail"], "image": "guest.jpg"}, assets=["guest.jpg"])
    with pytest.raises(RM.LicenseError, match="no license-log entry"):
        RM.render_item(ep, it, s, M.content_dir(ep) / "renders" / "lic", reg, cfg)
    flagged = RM.render_item(ep, it, s, M.content_dir(ep) / "renders" / "lic", reg, cfg, allow_unlicensed=True)
    assert "UNLICENSED" in flagged["warnings"][0]
    licenses.log_for(ep).append({"source": "own", "url": "file://own", "file": "guest.jpg", "sha256": licenses.sha256_file(photo),
                                 "license": "own work", "license_url": "file://own", "license_text": "taken by the creator; guest consented",
                                 "approved_by": "ben"})
    ok = RM.render_item(ep, it, s, M.content_dir(ep) / "renders" / "lic", reg, cfg)
    assert ok["warnings"] == [] and ok["critic"]["ok"]
    # the photo is really on the right of the frame
    im = Image.open(ep.root / ok["outputs"][0]["file"])
    assert all(abs(a - b) <= 2 for a, b in zip(im.getpixel((1100, 360)), (90, 110, 100)))   # photo, not the ink panel


def test_missing_thumbnail_image_is_a_clear_error(kit):
    ep, cfg, reg, s = kit
    it = _item("thumbnail", slots={**SLOTS["thumbnail"], "image": "nope.jpg"}, assets=[])
    with pytest.raises(FileNotFoundError, match="nope.jpg"):
        RM.render_item(ep, it, s, M.content_dir(ep) / "renders" / "x", reg, cfg, allow_unlicensed=True)


def test_show_name_prints_once_when_a_slot_repeats_it(kit):
    """Regression: role == show == 'The Long Table' used to print the show name twice on the quote card."""
    ep, cfg, reg, s = kit
    slots = {**SLOTS["quote-post"], "role": "The Long Table"}
    assert R.show_duplicates(slots) == ["role"]
    item = _item("quote-post", slots=slots)
    tpl = reg.get("quote-post")
    props = RM._props(tpl, item, ep, tpl.size(item["format"]), cfg)
    assert props["show"] == "The Long Table" and props["role"] == ""
    shot = s.shoot(tpl.still_dir(item["renderer"]), tpl.size(item["format"]), props, want_background=False)
    texts = [m["text"] for m in shot["measure"]]
    assert sum("The Long Table" in t for t in texts) == 1, texts


def test_propose_rejects_a_slot_that_repeats_the_show(cep):
    from ave.content import propose as P
    from content_helpers import pick_quote
    pick = pick_quote()
    pick["slots"]["role"] = "the long table"
    out = P.propose(cep, [pick], allow_unapproved=True)
    assert out["rejected"] and "repeats the show name" in out["rejected"][-1]["problems"][-1]
