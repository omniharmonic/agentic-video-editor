"""Spec 4.4 / 10.8: the Python metrics and the browser twin agree on a shared fixture list."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ave.studio import text_metrics as tm

ROOT = Path(__file__).resolve().parents[2]
FIX = json.loads((ROOT / "tests" / "data_studio" / "text_metrics_fixtures.json").read_text())
JS = ROOT / "src" / "ave" / "app" / "static" / "shared" / "text_metrics.js"


@pytest.mark.parametrize("case", FIX["cases"], ids=lambda c: c["name"])
def test_python_matches_the_shared_fixtures(case):
    t = case["text"]
    assert tm.counter(t, "chars") == case["chars"]
    assert tm.grapheme_len(t) == case["graphemes"]
    assert tm.x_weighted_len(t) == case["x_weighted"]
    assert [tm.fold_index(t, f) for f in FIX["folds"]] == case["folds"]


def test_hand_checked_anchors():
    """Numbers worked out by hand, so the fixtures cannot just echo the implementation."""
    assert tm.grapheme_len("\U0001F468‍\U0001F469‍\U0001F467") == 1         # ZWJ family is one character
    assert tm.grapheme_len("\U0001F1FA\U0001F1F8\U0001F1EF\U0001F1F5") == 2           # two flags
    assert tm.grapheme_len("\U0001F44D\U0001F3FD") == 1                               # skin tone modifier
    assert tm.grapheme_len("é") == 1
    assert tm.x_weighted_len("https://acme.example/very/long/path?with=query and more") == 23 + len(" and more")
    assert tm.x_weighted_len("日本") == 4                                     # CJK weigh 2
    assert tm.x_weighted_len("a" * 280) == 280


def test_bluesky_counts_graphemes_not_chars():
    family = "\U0001F468‍\U0001F469‍\U0001F467"
    text = family * 100
    assert tm.counter(text, "chars") == 500 and tm.counter(text, "graphemes") == 100


def test_margin_is_two_percent_below_the_platform_number():
    assert tm.with_margin(300) == 294 and tm.with_margin(280) == 274
    assert tm.over_limit("a" * 295, "graphemes", 300) and not tm.over_limit("a" * 294, "graphemes", 300)


def test_ig_fold_cuts_at_125_and_keeps_the_rest_as_hidden():
    text = "x" * 200
    r = tm.ig_visible_fold(text)
    assert r["folded"] and len(r["visible"]) == 125 and len(r["hidden"]) == 75
    short = tm.ig_visible_fold("short caption")
    assert not short["folded"] and short["hidden"] == ""


def test_fold_never_splits_an_emoji():
    text = "a" * 124 + "\U0001F468‍\U0001F469‍\U0001F467" + "b" * 10
    r = tm.ig_visible_fold(text)
    assert r["visible"].endswith("\U0001F468‍\U0001F469‍\U0001F467") and r["hidden"] == "b" * 10


def test_line_based_fold_wraps_at_words():
    text = "one two three four five six seven eight nine ten eleven twelve"
    r = tm.visible_fold(text, {"unit": "lines", "at": 2, "chars_per_line": 20})
    assert r["folded"] and len(r["visible"]) <= 40
    assert (r["visible"] + " " + r["hidden"]) == text or (r["visible"] + r["hidden"]) == text


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_twin_agrees_with_python_on_the_shared_fixtures(tmp_path):
    script = tmp_path / "run.mjs"
    script.write_text(
        f"import * as m from {json.dumps(JS.as_uri())};\n"
        f"import fs from 'node:fs';\n"
        f"const fx = JSON.parse(fs.readFileSync({json.dumps(str(ROOT / 'tests/data_studio/text_metrics_fixtures.json'))}, 'utf8'));\n"
        "const out = fx.cases.map(c => ({name: c.name, chars: m.counter(c.text, 'chars'), graphemes: m.graphemeLen(c.text),\n"
        "  approx: m.graphemesApprox(c.text).length, x_weighted: m.xWeightedLen(c.text), folds: fx.folds.map(f => m.foldIndex(c.text, f))}));\n"
        "console.log(JSON.stringify(out));\n"
    )
    res = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    got = {r["name"]: r for r in json.loads(res.stdout)}
    for case in FIX["cases"]:
        r = got[case["name"]]
        assert r["chars"] == case["chars"], case["name"]
        assert r["graphemes"] == case["graphemes"], case["name"]           # Intl.Segmenter
        assert r["approx"] == case["graphemes"], case["name"]              # the fallback splitter agrees too
        assert r["x_weighted"] == case["x_weighted"], case["name"]
        assert r["folds"] == case["folds"], case["name"]
