"""Shared fixtures for the content-pipeline tests. No network, no private footage, no real HOME or config."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from ave.episode import Episode, write_json

CLIP_TEXT = ("Okay so here is the thing about soil. A goal is a wish and a system is a habit, and the ground remembers "
             "what we do to it long after we have stopped paying attention. We do not fix a field in a season, we tend it "
             "for a generation.")
CLIP2_TEXT = ("People keep asking me for the one big idea. There is not one. There is a practice of small repairs, done "
              "again and again, by people who stay.")
SPEAKERS = {"S1": {"name": "Maren Okafor", "channel": "left"}, "S2": {"name": "Tomas", "channel": "right"}}


def _browser_cache() -> str | None:
    """Where Playwright's Chromium lives under the REAL home (publish_helpers repoints HOME for every test)."""
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return os.environ["PLAYWRIGHT_BROWSERS_PATH"]
    p = Path.home() / ("Library/Caches/ms-playwright" if sys.platform == "darwin" else ".cache/ms-playwright")
    return str(p) if p.is_dir() else None


BROWSERS = _browser_cache()


def apply_env(monkeypatch, tmp_path) -> None:
    if BROWSERS:
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", BROWSERS)
    monkeypatch.setenv("AVE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AVE_CONTENT_ENABLED", "1")
    monkeypatch.delenv("AVE_CONTENT_CONFIG", raising=False)


@pytest.fixture(autouse=True)
def content_env(monkeypatch, tmp_path):
    """Flag on, config dir isolated so the developer's real ~/.config/ave never leaks into a test."""
    apply_env(monkeypatch, tmp_path)


def _words(text: str, speaker: str, t0: float) -> tuple[list[dict], float]:
    out, t = [], t0
    for w in text.split():
        out.append({"w": w, "start": round(t, 3), "end": round(t + 0.3, 3), "conf": 0.9, "speaker": speaker})
        t += 0.35
    return out, t


def make_episode(root: Path, approved: bool = True) -> Episode:
    ep = Episode(root)
    ep.create("The Long Table 00")
    meta = json.loads(ep.meta_path.read_text())
    meta["speakers"] = SPEAKERS
    meta["show"] = "The Long Table"
    write_json(ep.meta_path, meta)
    w1, t = _words("Okay uh this is the recording take one.", "S2", 5.0)
    w2, t = _words(CLIP_TEXT, "S1", t + 2)
    w3, t = _words(CLIP2_TEXT, "S2", t + 2)
    write_json(ep.artifact("transcript.json"), {"source": "x.wav", "duration": t + 5, "speakers": SPEAKERS, "words": w1 + w2 + w3, "utterances": []})
    clips = [
        {"id": "c01", "slug": "soil-is-a-habit", "title": "Soil is a habit", "status": "approved" if approved else "proposed",
         "hook": "A goal is a wish.", "hook_quote": "A goal is a wish and a system is a habit", "end_quote": "we tend it for a generation",
         "text": CLIP_TEXT, "speakers": ["S1"], "total": 82.0, "duration": 31.0, "start": 10.0, "end": 41.0, "sentence_ids": ["s001"],
         "start_complete": True, "end_complete": True, "complete_statement": True},
        {"id": "c02", "slug": "small-repairs", "title": "Small repairs", "status": "proposed", "hook": "No big idea.", "hook_quote": None,
         "end_quote": None, "text": CLIP2_TEXT, "speakers": ["S2"], "total": 70.0, "duration": 20.0, "start": 50.0, "end": 70.0, "sentence_ids": ["s002"],
         "start_complete": True, "end_complete": True, "complete_statement": True},
    ]
    write_json(ep.artifact("clips.json"), {"version": 1, "clips": clips, "source": {}, "params": {}, "rubric": {}})
    return ep


@pytest.fixture
def cep(tmp_path) -> Episode:
    return make_episode(tmp_path / "ep")


def pick_quote(**over) -> dict:
    p = {"id": "quote-c01-4x5", "template": "quote-post", "style": "ink", "format": "4x5",
         "source": {"clip": "c01"},
         "slots": {"quote": "A goal is a wish and a system is a habit", "attribution": "Maren Okafor", "role": "Soil ecologist", "show": "The Long Table"},
         "platforms": ["instagram"], "reason": "The line the whole conversation turns on; short enough to read in one breath.", "confidence": 0.85}
    p.update(over)
    return p
