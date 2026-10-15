"""N4: a stereo file can have ONE live channel (session 1 right-only, session 2 left-only). Detect it per time range,
extract analysis audio from the live channel (averaging costs 6 dB), and fold it to dual mono at render time."""
import numpy as np
import pytest
import soundfile as sf

from ave import audio, editdoc, media
from ave import compile as comp
from ave.episode import Episode, read_json

from conftest import needs_ffmpeg

SR = 8000


def bursts(seconds, seed=0):
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    env = (np.sin(2 * np.pi * np.arange(n) / SR * 1.3) > -0.3).astype(np.float32)        # speech-like on/off, pauses < 1 s
    return (rng.standard_normal(n).astype(np.float32) * 0.1 * env)


def db_pair(l, r):
    return (l, r)


def test_derive_map_finds_one_live_channel_per_range():
    # 2 s windows: 0-20 s right is live (L 20 dB down), 20-40 s left is live
    lv = [(-52.0, -32.0)] * 10 + [(-28.0, -48.0)] * 10
    m = media.derive_channel_map(np.array(lv), win_s=2.0, duration=40.0)
    assert [(r["start"], r["end"], r["use"]) for r in m] == [(0.0, 20.0, "right"), (20.0, 40.0, "left")]


def test_both_channels_live_is_a_single_mix_range():
    m = media.derive_channel_map(np.array([(-30.0, -31.0)] * 20), win_s=2.0, duration=40.0)
    assert [(r["use"]) for r in m] == ["mix"]


def test_quiet_windows_inherit_and_short_flickers_are_absorbed():
    lv = [(-52.0, -32.0)] * 10 + [(-60.0, -61.0)] * 4 + [(-52.0, -32.0)] * 10       # a pause: both channels at the noise floor
    lv[3] = (-30.0, -31.0)                                                           # a one-window flicker
    m = media.derive_channel_map(np.array(lv), win_s=2.0, duration=48.0)
    assert [r["use"] for r in m] == ["right"] and m[0]["end"] == 48.0


def test_a_switch_inside_a_silence_lands_in_the_silence():
    lv = [(-52.0, -32.0)] * 8 + [(-70.0, -70.0)] * 6 + [(-28.0, -48.0)] * 8
    m = media.derive_channel_map(np.array(lv), win_s=2.0, duration=44.0)
    assert [r["use"] for r in m] == ["right", "left"] and 16.0 <= m[0]["end"] <= 28.0


@needs_ffmpeg
def test_ingest_records_channel_map_and_analysis_audio_uses_the_live_channel(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "CHANNEL_WINDOW_S", 2.0)
    ep = Episode(tmp_path / "ep"); ep.create()
    sig = bursts(40, seed=3)
    x = np.zeros((40 * SR, 2), np.float32)
    x[:20 * SR, 1] = sig[:20 * SR]; x[:20 * SR, 0] = sig[:20 * SR] * 0.01              # right live, left 40 dB down
    x[20 * SR:, 0] = sig[20 * SR:]; x[20 * SR:, 1] = sig[20 * SR:] * 0.01              # left live
    sf.write(ep.raw / "master.wav", x, SR, subtype="PCM_16")
    r = media.ingest(ep)
    cm = r["files"][0]["channel_map"]
    assert [m["use"] for m in cm] == ["right", "left"] and abs(cm[0]["end"] - 20.0) <= 2.0
    assert any("left channel" in w and "master.wav" in w for w in r["warnings"])
    assert read_json(ep.artifact("media.json"))["files"][0]["channel_map"] == cm            # persisted
    wav = audio.extract_all(ep)[0]
    y, sr = sf.read(str(wav), dtype="float32")
    live_rms = np.sqrt(np.mean(sig[2 * SR:18 * SR] ** 2))
    got_rms = np.sqrt(np.mean(y[2 * sr:18 * sr] ** 2))
    assert abs(20 * np.log10(got_rms / live_rms)) < 0.7, "analysis audio must carry the live channel at full level, not the 6 dB-down average"
    got2 = np.sqrt(np.mean(y[22 * sr:38 * sr] ** 2)); live2 = np.sqrt(np.mean(sig[22 * SR:38 * SR] ** 2))
    assert abs(20 * np.log10(got2 / live2)) < 0.7


@needs_ffmpeg
def test_compile_folds_the_live_channel_to_dual_mono(tmp_path):
    ep = Episode(tmp_path / "ep"); ep.create()
    sig = bursts(40, seed=4) * 3
    x = np.zeros((40 * SR, 2), np.float32)
    x[:20 * SR, 1] = sig[:20 * SR]; x[:20 * SR, 0] = sig[:20 * SR] * 0.01
    x[20 * SR:, 0] = sig[20 * SR:]; x[20 * SR:, 1] = sig[20 * SR:] * 0.01
    sf.write(ep.raw / "master.wav", x, SR, subtype="PCM_16")
    cm = [{"start": 0.0, "end": 20.0, "use": "right"}, {"start": 20.0, "end": 40.0, "use": "left"}]
    src = {"M": {"file": "raw/master.wav", "kind": "audio", "start_offset": 0.0, "channel_map": cm}}
    doc = editdoc.new_doc("rough", 30, (64, 36), {**src, "V": {"file": "raw/master.wav", "kind": "video", "start_offset": 0.0}}, 40.0)
    doc["tracks"]["dialogue"].append({"id": "d1", "src": "M", "o0": 0.0, "o1": 40.0, "e0": 0.0, "xfade_ms": 0, "gain_db": 0.0})
    out = comp.compile_audio(ep, doc, tmp_path / "d.wav")
    y, sr = sf.read(str(out), dtype="float32", always_2d=True)
    assert sr == 48000
    for a, b, live in ((2, 18, sig[2 * SR:18 * SR]), (22, 38, sig[22 * SR:38 * SR])):
        L, R = y[a * sr:b * sr, 0], y[a * sr:b * sr, 1]
        assert np.sqrt(np.mean((L - R) ** 2)) < 0.02 * np.sqrt(np.mean(L ** 2)), "both ears must carry the same dual-mono signal"
        assert abs(20 * np.log10(np.sqrt(np.mean(L ** 2)) / np.sqrt(np.mean(live ** 2)))) < 0.7
    # without the map the same file would be one-eared: prove the test can fail
    doc["sources"]["M"].pop("channel_map")
    y2, _ = sf.read(str(comp.compile_audio(ep, doc, tmp_path / "d2.wav")), dtype="float32", always_2d=True)
    assert np.sqrt(np.mean(y2[2 * sr:18 * sr, 0] ** 2)) < 0.05 * np.sqrt(np.mean(y2[2 * sr:18 * sr, 1] ** 2))


@needs_ffmpeg
def test_assemble_copies_channel_map_to_sources(synth_episode):
    import json
    from ave import assemble
    ep = synth_episode
    p = ep.artifact("media.json")
    m = json.loads(p.read_text())
    cm = [{"start": 0.0, "end": 30.0, "use": "right"}]
    next(f for f in m["files"] if f["file"].endswith("recorder.wav"))["channel_map"] = cm
    next(f for f in m["files"] if f["file"].endswith("camA.mp4"))["channel_map"] = cm
    p.write_text(json.dumps(m))
    doc = assemble.build(ep, (640, 360))
    assert doc["sources"]["A"]["channel_map"] == cm
    assert "channel_map" not in doc["sources"]["B"]
