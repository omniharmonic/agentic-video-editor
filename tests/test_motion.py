"""Motion kit: tokens, schema validation, deterministic alpha renderer, overlay placement."""

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from ave import editdoc, motion
from ave.episode import Episode

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")

SMALL = (640, 360)


def _spec(template="lower-third", **over):
    ex = motion.example(template)
    spec = {"template": template, "props": ex["props"], "duration": 2.0, "fps": 10, "canvas": SMALL}
    spec.update(over)
    return motion.normalize_spec(spec)


def _alpha_extrema(png: Path):
    a = Image.open(png).convert("RGBA").getchannel("A")
    return a.getextrema()


# ---- kit hygiene -------------------------------------------------------------------------------

def test_lists_six_production_templates():
    ids = [t["id"] for t in motion.list_templates()]
    assert ids == sorted(["lower-third", "quote-card", "chapter-card", "stat-callout", "title-card", "short-title"])


def test_templates_do_not_hardcode_brand_values_or_use_gsap():
    kit = motion.KIT
    for t in motion.list_templates():
        src = (kit / "templates" / t["id"] / "template.html").read_text()
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b", src), f"{t['id']} hardcodes a hex color"
        for fam in re.findall(r"font-family:\s*([^;]+);", src):
            assert fam.strip().startswith("var(--f-"), f"{t['id']} hardcodes font-family {fam}"
        assert "gsap" not in src.lower()
    for f in (kit / "lib").iterdir():
        assert "gsap" not in f.read_text().lower()


def test_easing_curves_never_overshoot():
    """Restraint is a token rule: control-point y stays in [0, 1], so nothing bounces or springs."""
    for name, (_x1, y1, _x2, y2) in motion.load_tokens()["easing"].items():
        assert 0 <= y1 <= 1 and 0 <= y2 <= 1, name


def test_safe_areas_sit_inside_layout_margins():
    tk = motion.load_tokens()
    assert tk["layout"]["margin_x"] >= tk["safe_area"]["title"]
    assert tk["layout"]["margin_y"] >= tk["safe_area"]["title"]


# ---- spec validation ---------------------------------------------------------------------------

def test_invalid_props_are_rejected_with_a_clear_message():
    with pytest.raises(motion.SpecError) as e:
        motion.normalize_spec({"template": "lower-third", "props": {}})
    assert "name" in str(e.value) and "required" in str(e.value)
    with pytest.raises(motion.SpecError) as e:
        motion.normalize_spec({"template": "lower-third", "props": {"name": "x" * 99}})
    assert "name" in str(e.value) and "40" in str(e.value)
    with pytest.raises(motion.SpecError) as e:
        motion.normalize_spec({"template": "lower-third", "props": {"name": "A", "colour": "red"}})
    assert "colour" in str(e.value)
    with pytest.raises(motion.SpecError) as e:
        motion.normalize_spec({"template": "lower-third", "props": {"name": "A", "side": "up"}})
    assert "left" in str(e.value)
    with pytest.raises(motion.SpecError) as e:
        motion.normalize_spec({"template": "nope", "props": {}})
    assert "lower-third" in str(e.value)


def test_defaults_are_filled_and_canvas_parsed():
    s = motion.normalize_spec({"template": "lower-third", "props": {"name": "A"}, "canvas": "1080x1920"})
    assert s["props"]["side"] == "left" and s["canvas"] == (1080, 1920)
    assert s["duration"] == 5.0 and s["fps"] == 30.0 and s["outro"] is True


def test_every_example_validates():
    for t in motion.list_templates():
        motion.normalize_spec({"template": t["id"], **motion.example(t["id"])})


# ---- stills ------------------------------------------------------------------------------------

def test_still_has_alpha_and_content(tmp_path):
    out = motion.render_still(_spec(), 1.2, tmp_path / "a.png")
    im = Image.open(out)
    assert im.mode == "RGBA" and im.size == SMALL
    lo, hi = _alpha_extrema(out)
    assert lo == 0, "background must be transparent"
    assert hi == 255, "template content must be present and opaque somewhere"


def test_every_template_renders_a_still_with_transparent_background(tmp_path):
    for t in motion.list_templates():
        sp = _spec(t["id"])
        out = motion.render_still(sp, 1.8, tmp_path / f"{t['id']}.png")
        lo, hi = _alpha_extrema(out)
        assert hi > 0, t["id"]
        if t["id"] in ("lower-third", "stat-callout"):
            assert lo == 0, f"{t['id']} must leave the picture untouched around the graphic"


def test_safe_area_draws_guides(tmp_path):
    sp = _spec(template="lower-third")
    plain = motion.render_still(sp, 0.0, tmp_path / "plain.png")
    guided = motion.render_still(sp, 0.0, tmp_path / "guided.png", safe_area=True)
    assert _alpha_extrema(plain) == (0, 0), "t=0 is fully transparent"
    tk = motion.load_tokens()
    x = round(SMALL[0] * tk["safe_area"]["title"])
    y = SMALL[1] // 2
    im = Image.open(guided).convert("RGBA")
    assert im.getpixel((x, y))[3] > 200, "title-safe guide line missing"
    ax = round(SMALL[0] * tk["safe_area"]["action"])
    assert im.getpixel((ax, y))[3] > 200, "action-safe guide line missing"


def test_animation_starts_transparent_and_ends_visible_then_leaves(tmp_path):
    sp = _spec(duration=3.0)
    first = motion.render_still(sp, 0.0, tmp_path / "f.png")
    mid = motion.render_still(sp, 1.5, tmp_path / "m.png")
    last = motion.render_still(sp, 3.0, tmp_path / "l.png")
    assert _alpha_extrema(first) == (0, 0)
    assert _alpha_extrema(mid)[1] == 255
    assert _alpha_extrema(last) == (0, 0), "outro leaves the screen clean"
    held = motion.normalize_spec({**sp, "outro": False})
    end = motion.render_still(held, 3.0, tmp_path / "e.png")
    assert _alpha_extrema(end)[1] == 255, "without an outro the last frame stays on screen"
    assert _png_hash(first) != _png_hash(mid)


def _png_hash(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def test_vertical_canvas_scales_layout(tmp_path):
    sp = _spec(canvas=(540, 960))
    out = motion.render_still(sp, 1.0, tmp_path / "v.png")
    assert Image.open(out).size == (540, 960) and _alpha_extrema(out)[1] == 255


# ---- video render ------------------------------------------------------------------------------

def _probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                        "-show_entries", "stream=codec_name,color_space,color_primaries,color_transfer,pix_fmt,r_frame_rate,nb_read_frames,width,height,profile",
                        "-of", "json", str(path)], capture_output=True, text=True, check=True)
    return json.loads(r.stdout)["streams"][0]


def test_render_prores4444_with_alpha_and_frame_count(tmp_path):
    sp = _spec(duration=1.5, fps=12)
    rep = motion.render(sp, tmp_path / "lt.mov")
    st = _probe(tmp_path / "lt.mov")
    assert st["codec_name"] == "prores" and st["pix_fmt"].startswith("yuva444p")
    assert "4444" in st["profile"]
    assert (st["color_space"], st["color_primaries"], st["color_transfer"]) == ("bt709",) * 3
    assert int(st["nb_read_frames"]) == 18 == rep["frames"]
    png = tmp_path / "last.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(tmp_path / "lt.mov"), "-vf", "select=eq(n\\,17)", "-vframes", "1", "-pix_fmt", "rgba", str(png)], check=True)
    assert _alpha_extrema(png)[1] <= 2, "the outro must finish on the last frame"
    assert st["r_frame_rate"] == "12/1" and (st["width"], st["height"]) == SMALL


def test_rendered_movie_keeps_alpha_through_decode(tmp_path):
    sp = _spec(duration=2.0, fps=10)
    motion.render(sp, tmp_path / "lt.mov")
    for t, want_visible in ((0.0, False), (1.0, True)):
        png = tmp_path / f"f{t}.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(t), "-i", str(tmp_path / "lt.mov"),
                        "-frames:v", "1", "-pix_fmt", "rgba", str(png)], check=True)
        lo, hi = _alpha_extrema(png)
        assert (hi > 200) == want_visible


def test_png_sequence_output(tmp_path):
    sp = _spec(duration=1.0, fps=8)
    rep = motion.render(sp, tmp_path / "seq", png_sequence=True)
    files = sorted((tmp_path / "seq").glob("*.png"))
    assert len(files) == 8 == rep["frames"] and files[0].name == "00001.png"


def test_rendering_is_deterministic(tmp_path):
    sp = _spec(duration=1.0, fps=8)
    motion.render(sp, tmp_path / "a", png_sequence=True)
    motion.render(sp, tmp_path / "b", png_sequence=True)
    for fa in sorted((tmp_path / "a").glob("*.png")):
        assert _png_hash(fa) == _png_hash(tmp_path / "b" / fa.name), fa.name


def test_swatch_has_half_alpha_box(tmp_path):
    rep = motion.swatch(tmp_path / "sw.mov", canvas=SMALL, fps=10)
    png = tmp_path / "s.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", rep["out"], "-frames:v", "1", "-pix_fmt", "rgba", str(png)], check=True)
    im = Image.open(png).convert("RGBA")
    half = im.getpixel((int(SMALL[0] * 0.3), SMALL[1] // 2))
    full = im.getpixel((int(SMALL[0] * 0.65), SMALL[1] // 2))
    assert abs(half[3] - 128) <= 3 and half[0] > 240, half
    assert full[3] >= 253, full
    assert im.getpixel((5, 5))[3] == 0


# ---- place -------------------------------------------------------------------------------------

def test_place_adds_overlay_in_a_new_doc_version(synth_episode):
    ep = synth_episode
    before = editdoc.list_versions(ep, "assembly")
    res = motion.place(ep, "lower-third", {"name": "Maren Okafor", "title": "Ecologist"}, at=2.0, dur=1.5,
                       stage="assembly", fps=10)
    after = editdoc.list_versions(ep, "assembly")
    assert after == before + [before[-1] + 1]
    doc = editdoc.load(res["doc"])
    assert editdoc.validate(doc) == []
    ov = doc["tracks"]["overlays"][-1]
    assert ov["kind"] == "motion" and ov["alpha"] is True and ov["layer"] >= 1
    assert (ov["o0"], ov["o1"]) == (2.0, 3.5)
    assert (ov["w"], ov["h"]) == (doc["canvas"]["w"], doc["canvas"]["h"]) and (ov["x"], ov["y"]) == (0, 0)
    mov = ep.root / ov["file"]
    assert mov.exists() and mov.is_relative_to(ep.work / "motion")
    assert _probe(mov)["pix_fmt"].startswith("yuva444p")
    # the old version is untouched, and a second placement stacks above the first
    res2 = motion.place(ep, "stat-callout", {"value": "38%", "label": "lost"}, at=5.0, dur=2.0, stage="assembly", fps=10)
    doc2 = editdoc.load(res2["doc"])
    layers = [o["layer"] for o in doc2["tracks"]["overlays"]]
    assert layers == sorted(layers) and len(set(layers)) == 2


def test_place_targets_a_named_session_edit(synth_episode):
    """Found on the real episode: `ave motion place` could not address assembly.s2/rough.s2/final.s2 (it only saw unnamed edits)."""
    ep = synth_episode
    base = editdoc.load_latest(ep, "assembly")
    editdoc.save_new_version(ep, editdoc.clone(base), "s2")
    unnamed_before = editdoc.list_versions(ep, "assembly")
    res = motion.place(ep, "lower-third", {"name": "Tomas", "title": "CO-HOST"}, at=1.0, dur=1.5, fps=10, name="s2")
    assert editdoc.list_versions(ep, "assembly", "s2") == [1, 2]
    assert editdoc.list_versions(ep, "assembly") == unnamed_before            # the unnamed episode edit is untouched
    assert res["doc"].endswith("assembly.s2.v2.json")
    assert editdoc.load(res["doc"])["tracks"]["overlays"][-1]["props"]["name"] == "Tomas"
    with pytest.raises(SystemExit):                                              # a session with no edit says so
        motion.place(ep, "lower-third", {"name": "A"}, at=1, dur=1, fps=10, name="s9")


def test_place_rejects_out_of_range_and_bad_props(synth_episode):
    with pytest.raises(motion.SpecError):
        motion.place(synth_episode, "lower-third", {"name": "A"}, at=9999, dur=3, stage="assembly", fps=10)
    with pytest.raises(motion.SpecError):
        motion.place(synth_episode, "lower-third", {}, at=1, dur=3, stage="assembly", fps=10)
    with pytest.raises(SystemExit):
        motion.place(synth_episode, "lower-third", {"name": "A"}, at=1, dur=3, stage="final", fps=10)


# ---- CLI ---------------------------------------------------------------------------------------

def test_cli_list_and_still(tmp_path, capsys):
    from ave import cli
    assert cli.main(["motion", "list"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert {t["id"] for t in out["templates"]} >= {"lower-third", "title-card"}
    png = tmp_path / "cli.png"
    rc = cli.main(["motion", "still", "--template", "quote-card", "--props", json.dumps(motion.example("quote-card")["props"]),
                   "--t", "2.5", "--canvas", "640x360", "--out", str(png), "--preview"])
    assert rc == 0 and png.exists()
    res = json.loads(capsys.readouterr().out)
    assert Path(res["preview"]).exists()
