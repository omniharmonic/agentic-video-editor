"""The multi-session synthetic episode (field notes N1-N4): sessions, long offsets, drift recovery, dead channels."""
import json

import pytest

import fixtures as fx
from ave.episode import read_json

from conftest import needs_ffmpeg

pytestmark = needs_ffmpeg


def test_ingest_groups_two_sessions_and_flags_the_recorder(ms_copy):
    ep = ms_copy("media")
    m = read_json(ep.artifact("media.json"))
    assert [s["id"] for s in m["sessions"]] == ["s1", "s2"]
    assert [sorted(s["files"]) for s in m["sessions"]] == [["raw/camA1.mov", "raw/camB1.mov"], ["raw/camA2.mov", "raw/camB2.mov"]]
    rec = m["recorders"][0]
    assert rec["file"] == "raw/recorder.wav" and rec["spans_sessions"] is True
    assert rec["sum_session_durations"] == pytest.approx(90.0 + 80.0, abs=0.5)      # 195 s of audio vs 170 s of sessions
    assert any("may span" in w for w in m["warnings"])


def test_ingest_finds_the_dead_channel_per_session(ms_copy):
    ep = ms_copy("media")
    f = next(x for x in read_json(ep.artifact("media.json"))["files"] if x["file"] == "raw/recorder.wav")
    uses = [(r["use"], r["start"], r["end"]) for r in f["channel_map"]]
    assert [u[0] for u in uses] == ["right", "left"], uses
    assert fx.MS_SWITCH - 20 <= uses[0][2] <= fx.MS_SWITCH + 16       # the switch is inside the 93-111 s dead space
    assert next(w for w in read_json(ep.artifact("media.json"))["warnings"] if "recorder.wav" in w and "channel" in w)


def test_sync_recovers_offsets_beyond_half_the_fft_and_drift_within_10ppm(ms_copy):
    ep = ms_copy("sync")
    s = read_json(ep.artifact("sync.json"))
    assert s["reference"] == "raw/recorder.wav"                         # the recorder is longer than every camera
    for key, (off, ppm) in fx.ms_truth().items():
        r = s["files"][f"raw/cam{key}.mov"]
        assert abs(r["start_offset"] - off) < 0.010, (key, r["start_offset"], off)
        assert abs(r["drift_ppm"] - ppm) < 10.0, (key, r["drift_ppm"], ppm)
        assert r["psr_min"] > 25 and len(r["points"]) == 5 and r["residual_ms"] < 10
    assert not [w for w in s["warnings"] if "no sync" in w]
