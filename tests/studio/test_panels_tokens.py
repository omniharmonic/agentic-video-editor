"""Spec 10.7 token contrast: the --st-* tokens in panels.css resolve to ave ui tokens, and every text/background pair the panels use is >= 4.5:1
in light and dark. Also: panels.css defines only --st-* custom properties (so the shell's studio.css can override them) and loads nothing remote."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STYLE = (ROOT / "src" / "ave" / "ui" / "static" / "style.css").read_text()
PANELS = (ROOT / "src" / "ave" / "studio" / "ui" / "static" / "panels.css").read_text()


def _tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{3,6})", block))


def _themes():
    light = _tokens(STYLE.split(":root {", 1)[1].split("}", 1)[0])
    dark_block = STYLE.split(':root[data-theme="dark"] {', 1)[1].split("}", 1)[0]
    dark = {**light, **_tokens(dark_block)}
    return {"light": light, "dark": dark}


def _lum(hexc: str) -> float:
    h = hexc.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    rgb = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


# (foreground token, background token): text pairs the panels actually draw
PAIRS = [
    ("ink", "sheet"), ("ink", "bg"), ("ink-2", "sheet"), ("ink-2", "sheet-2"), ("ink-3", "sheet"), ("ink-3", "sheet-2"),
    ("spruce", "sheet"), ("spruce-ink", "spruce"), ("spruce", "spruce-wash"),
    ("pencil", "sheet"), ("pencil", "pencil-wash"), ("amber", "amber-wash"), ("amber", "sheet-2"), ("slate", "slate-wash"),
    ("marker-ink", "marker"), ("ink", "marker-wash"), ("ink-2", "bg"),
]


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("fg,bg", PAIRS)
def test_text_pairs_meet_4_5_to_1(theme, fg, bg):
    t = _themes()[theme]
    assert contrast(t[fg], t[bg]) >= 4.5, f"{theme}: {fg} on {bg} = {contrast(t[fg], t[bg]):.2f}"


def test_panels_tokens_are_all_st_prefixed_and_fall_back_to_ave_ui_tokens():
    root = PANELS.split(":root {", 1)[1].split("\n}", 1)[0]
    names = re.findall(r"^\s*(--[a-z0-9-]+):", root, re.M)
    assert names and all(n.startswith("--st-") for n in names)
    # every colour token echoes an ave ui token with a literal fallback equal to the ave ui light value
    light = _themes()["light"]
    for name, var, fallback in re.findall(r"(--st-[a-z0-9-]+):\s*var\(--([a-z0-9-]+),\s*(#[0-9a-fA-F]{3,6})\)", root):
        assert light[var].lower() == fallback.lower(), f"{name}: fallback {fallback} drifted from style.css --{var}={light[var]}"


def test_panels_load_nothing_remote():
    assert "http://" not in PANELS and "https://" not in PANELS and "@import" not in PANELS and "url(" not in PANELS
