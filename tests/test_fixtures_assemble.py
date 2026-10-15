import subprocess

import pytest

from ave import editdoc
from conftest import needs_ffmpeg

pytestmark = needs_ffmpeg


def test_synthetic_episode_assembles_to_a_valid_editdoc(synth_episode):
    doc = editdoc.load_latest(synth_episode, "assembly")
    assert editdoc.validate(doc) == []
    assert doc["stage"] == "assembly" and abs(doc["duration"] - 29.5) < 1.0
    cams = {s["src"] for s in doc["tracks"]["video"]}
    assert cams == {"A", "B"}                       # both cameras are used
    assert doc["sources"]["B"]["start_offset"] == 1.0


def test_synthetic_media_has_expected_streams(synth_episode):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                          str(synth_episode.raw / "camA.mp4")], capture_output=True, text=True).stdout
    assert abs(float(out) - 30.0) < 0.3


def test_assemble_copies_drift_ppm_from_sync(synth_episode):
    import json
    from ave import assemble
    ep = synth_episode
    sync = json.loads(ep.artifact("sync.json").read_text())
    sync["files"]["raw/camB.mp4"]["drift_ppm"] = 42.5
    ep.artifact("sync.json").write_text(json.dumps(sync))
    doc = assemble.build(ep, (640, 360))
    assert doc["sources"]["B"]["drift_ppm"] == 42.5 and doc["sources"]["A"]["drift_ppm"] == 0.0
