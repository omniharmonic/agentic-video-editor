"""Render a whole ContentDoc and the animated reel card (each opens its own browser, so these live apart from the shared-session tests)."""

import json
import shutil
import subprocess

import pytest

from ave.content import manifest as M
from ave.content import registry as R
from ave.content import render_motionkit as RM
from ave.content import settings
from content_helpers import cep, content_env, make_episode, pick_quote  # noqa: F401


def _item(template, style="ink", fmt=None, slots=None, **kw):
    t = R.load_registry().get(template)
    return M.new_item(f"t-{template}", template, style, fmt or t.formats[0], slots, reason="test", confidence=0.9, **kw)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_reel_card_renders_to_prores_with_alpha_tag_through_ave_motion(cep, tmp_path):
    ep, cfg, reg = cep, settings.load(), R.load_registry()
    out = tmp_path / "card.mov"
    res = RM.render_card_video(ep, _item("reel-card", slots={"kind": "intro", "title": "Soil is a habit, not a goal", "subtitle": "Maren Okafor", "show": "The Long Table"}), out, reg, cfg, duration=1.0, fps=10)
    assert res["frames"] == 10 and res["w"] == 1080 and res["h"] == 1920
    probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(out)], capture_output=True, text=True).stdout)
    v = probe["streams"][0]
    assert v["codec_name"] == "prores" and (v["width"], v["height"]) == (1080, 1920) and v["pix_fmt"].startswith("yuva444p")


def test_render_doc_fills_outputs_and_critic_and_skips_what_is_current(cep):
    doc = M.new_doc(cep)
    from ave.content import sources as S
    doc["sources"]["q1"] = S.clip_source(cep, "c01")
    it = M.new_item("quote-c01-4x5", "quote-post", "ink", "4x5", {"quote": "A goal is a wish and a system is a habit", "attribution": "Maren Okafor"},
                    source="q1", platforms=["instagram", "facebook"], reason="the hinge", confidence=0.8)
    doc["items"].append(it)
    new, rep = RM.render_doc(cep, doc)
    assert [r["id"] for r in rep["rendered"]] == ["quote-c01-4x5"]
    o = new["items"][0]["outputs"]
    assert {x["platform"] for x in o} == {"instagram", "facebook"} and len({x["file"] for x in o}) == 1
    assert new["items"][0]["review"]["critic"]["ok"] and new["items"][0]["provenance"]["template_rev"]
    M.save_new_version(cep, new)
    again, rep2 = RM.render_doc(cep, new)
    assert rep2["rendered"] == [] and "already rendered" in rep2["skipped"][0]["why"]
