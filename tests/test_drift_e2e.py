"""N3 end to end: a camera whose clock drifts 80 ppm still lands its marker on the right frame at the END of a long take.

The unit tests of drift detection passed while the product was wrong (nothing applied the drift). This is the test that
matters: measure the drift with the real sync, render with the real compiler, and look at the rendered pixels and samples.
It also runs the same render with the drift ignored and requires it to FAIL, so the test cannot pass by accident.
"""
from __future__ import annotations

import subprocess

import numpy as np
import pytest
import soundfile as sf

import drift_synth as ds
from ave import compile as comp
from ave import editdoc, sync
from ave.episode import Episode
from conftest import needs_ffmpeg

pytestmark = needs_ffmpeg
MARK_O = 1.0                       # the render starts 1 s before the marker, so it must appear at output time 1.0 s


@pytest.fixture(scope="module")
def take(tmp_path_factory):
    root = tmp_path_factory.mktemp("longtake") / "ep"
    ep = Episode(root)
    ep.create()
    files = ds.build(ep.raw)
    measured = sync.sync_pair(files["master"], files["cam_wav"])
    return ep, files, measured


def doc_for(ep, measured, drift_ppm):
    src = {"C": {"file": "raw/cam.mov", "kind": "video", "start_offset": measured["start_offset"], "drift_ppm": drift_ppm}}
    d = editdoc.new_doc("rough", ds.FPS, (64, 36), src, 3.0)
    e0 = ds.T_MARK - MARK_O
    d["tracks"]["video"].append({"id": "v1", "src": "C", "o0": 0.0, "o1": 3.0, "e0": e0, "crop": None, "zoom": 1.0})
    d["tracks"]["dialogue"].append({"id": "d1", "src": "C", "o0": 0.0, "o1": 3.0, "e0": e0, "xfade_ms": 0, "gain_db": 0.0})
    return d


def flash_frame(mp4) -> int:
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(mp4), "-vf", "scale=8:4", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                         capture_output=True, check=True).stdout
    luma = np.frombuffer(raw, np.uint8).reshape(-1, 32).mean(axis=1)
    return int(np.argmax(luma > 128))


def beep_time(wav) -> float:
    x, sr = sf.read(str(wav), dtype="float32", always_2d=True)
    return float(np.argmax(np.abs(x).max(axis=1) > 0.1)) / sr


def test_sync_measures_the_injected_clock(take):
    ep, files, m = take
    assert abs(m["start_offset"] - ds.A_TRUE) < 0.005, m
    assert abs(m["drift_ppm"] - ds.B_TRUE * 1e6) < 10.0, m
    assert m["psr_min"] > 25 and m["residual_ms"] < 5


def test_marker_lands_within_one_frame_when_drift_is_applied_and_misses_when_ignored(take, tmp_path):
    ep, files, m = take
    frame_s = 1.0 / ds.FPS
    exp_frame = int(round(MARK_O * ds.FPS))

    good = comp.compile_video(ep, doc_for(ep, m, m["drift_ppm"]), tmp_path / "good.mp4")
    assert abs(flash_frame(good) - exp_frame) <= 1, flash_frame(good)
    comp.compile_audio(ep, doc_for(ep, m, m["drift_ppm"]), tmp_path / "good.wav")
    assert abs(beep_time(tmp_path / "good.wav") - MARK_O) < frame_s, beep_time(tmp_path / "good.wav")

    # the negative control: the same offset with the drift thrown away is 55 ms (1.65 frames) off at this point of the take
    bad = comp.compile_video(ep, doc_for(ep, m, 0.0), tmp_path / "bad.mp4")
    assert abs(flash_frame(bad) - exp_frame) > 1, "ignoring drift should have missed the marker by more than a frame"
    comp.compile_audio(ep, doc_for(ep, m, 0.0), tmp_path / "bad.wav")
    assert abs(beep_time(tmp_path / "bad.wav") - MARK_O) > frame_s
