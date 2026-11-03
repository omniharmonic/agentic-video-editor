"""Spec 10.9: every previews.yaml rule carries `verified` + `source`, none is verified, and the browser's JSON mirror matches the YAML.
Regenerate the mirror with `AVE_REGEN_PREVIEWS=1 pytest tests/studio/test_previews_config.py`."""

from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
YAML = ROOT / "config" / "previews.yaml"
MIRROR = ROOT / "src" / "ave" / "studio" / "ui" / "static" / "previews" / "previews.json"
REGEN = bool(os.environ.get("AVE_REGEN_PREVIEWS"))        # read at import: the autouse fixture scrubs AVE_* per test
UNITS = {"chars", "graphemes", "x_weighted", "lines"}


def load():
    return yaml.safe_load(YAML.read_text())


def _blocks(node, path=(), covered=False):
    """Every dict that directly carries a number (or a list of numbers) is a 'rule' and needs verified + source."""
    if isinstance(node, dict):
        has_number = any(isinstance(v, (int, float)) and not isinstance(v, bool) and k not in ("verified",) for k, v in node.items()) or any(
            isinstance(v, list) and v and all(isinstance(x, (int, float, list)) for x in v) for v in node.values())
        own = "verified" in node
        if has_number and not (covered and not own):          # a nested block (e.g. `safe`) is covered by its parent's verified/source
            yield path, node
        for k, v in node.items():
            yield from _blocks(v, path + (k,), covered or own)


def test_every_rule_has_verified_false_and_a_source():
    cfg = load()
    assert cfg["version"] == 1 and "Approximate" in cfg["approximate_label"]
    assert cfg["platforms"], "no platforms"
    for name, plat in cfg["platforms"].items():
        assert plat.get("verified") is False, f"{name}: verified must be present and false until a burn-in flips it"
        assert plat.get("source"), f"{name}: missing source"
    seen = 0
    for path, node in _blocks(cfg["platforms"]):
        if len(path) < 2:                                      # the platform entry itself was checked above
            continue
        seen += 1
        assert node.get("verified") is False, f"{'.'.join(path)}: needs `verified: false`"
        assert node.get("source"), f"{'.'.join(path)}: needs a `source`"
    assert seen > 10


def test_the_seven_launch_platforms_and_their_fold_and_counter_rules_exist():
    plats = load()["platforms"]
    for name in ("instagram", "facebook", "x", "linkedin", "tiktok", "bluesky", "threads"):
        assert name in plats, name
        assert plats[name]["counter"]["unit"] in UNITS - {"lines"}
        assert plats[name]["fold"]["unit"] in {"chars", "lines"}
    assert plats["instagram"]["fold"]["at"] == 125 and plats["bluesky"]["counter"]["soft_limit"] == 300
    assert plats["x"]["counter"]["soft_limit"] == 280 and plats["instagram"]["grid"]["cell_aspect"] == [3, 4]


def test_story_and_reel_safe_zones_match_content_yaml():
    content = yaml.safe_load((ROOT / "config" / "content.yaml").read_text())["safe_area"]["9x16"]
    for name, surface in (("instagram", "story"), ("instagram", "reel"), ("tiktok", "video")):
        safe = load()["platforms"][name]["surfaces"][surface]["safe"]
        assert safe == {"top": content["top"], "bottom": content["bottom"], "x": content["x"]}


def test_json_mirror_for_the_browser_equals_the_yaml():
    want = load()
    if REGEN:
        MIRROR.parent.mkdir(parents=True, exist_ok=True)
        MIRROR.write_text(json.dumps(want, indent=1, sort_keys=False) + "\n")
    assert MIRROR.exists(), "run once with AVE_REGEN_PREVIEWS=1"
    assert json.loads(MIRROR.read_text()) == want, "previews.json is stale: AVE_REGEN_PREVIEWS=1 pytest tests/studio/test_previews_config.py"
