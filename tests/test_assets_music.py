"""Music analysis on synthetic signals with known ground truth (tempo, key, density, silence)."""
import numpy as np
import pytest
import soundfile as sf

from ave.assets import critic, music

SR = 22050


def click_track(bpm: float, seconds: float, lead_silence: float = 0.0, amp=0.8) -> np.ndarray:
    y = np.zeros(int((lead_silence + seconds) * SR), dtype=np.float32)
    t = np.arange(int(0.03 * SR)) / SR
    click = (np.sin(2 * np.pi * 1000 * t) * np.exp(-t * 150)).astype(np.float32) * amp
    for b in np.arange(lead_silence, lead_silence + seconds - 0.05, 60.0 / bpm):
        i = int(b * SR)
        y[i:i + len(click)] += click[: len(y) - i]
    return y


def chord(freqs, seconds: float, amp=0.15, lead_silence: float = 0.0) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    y = sum(sum(amp / h * np.sin(2 * np.pi * f * h * t) for h in (1, 2, 3)) for f in freqs).astype(np.float32)
    fade = np.minimum(1, np.minimum(t, seconds - t) / 0.5).astype(np.float32)
    return np.concatenate([np.zeros(int(lead_silence * SR), dtype=np.float32), y * fade])


def busy_hats(seconds: float, per_s: float = 9.0) -> np.ndarray:
    rng = np.random.default_rng(3)
    y = np.zeros(int(seconds * SR), dtype=np.float32)
    n = int(0.02 * SR)
    for s in np.arange(0, seconds - 0.05, 1 / per_s):
        i = int(s * SR)
        y[i:i + n] += (rng.standard_normal(n) * np.exp(-np.arange(n) / (0.004 * SR))).astype(np.float32) * 0.5
    return y


def drum_loop(bpm: float, seconds: float) -> np.ndarray:
    """Loud kick on every beat, noisy hat on every off-beat: dense, bright, energetic."""
    rng = np.random.default_rng(5)
    y = np.zeros(int(seconds * SR), dtype=np.float32)
    beat = 60.0 / bpm
    tk = np.arange(int(0.25 * SR)) / SR
    kick = (np.sin(2 * np.pi * 70 * tk) * np.exp(-tk * 12)).astype(np.float32) * 0.9
    th = np.arange(int(0.12 * SR)) / SR
    for i, s in enumerate(np.arange(0, seconds - 0.3, beat / 2)):
        j = int(s * SR)
        if i % 2 == 0:
            y[j:j + len(kick)] += kick[: len(y) - j]
        else:
            y[j:j + len(th)] += (rng.standard_normal(len(th)) * np.exp(-th * 25)).astype(np.float32)[: len(y) - j] * 0.5
    return np.clip(y, -1, 1)


def write(path, y):
    sf.write(path, y, SR)
    return path


def test_tempo_of_a_click_track(tmp_path):
    a = music.analyze(write(tmp_path / "c100.wav", click_track(100, 20)))
    assert a["tempo"] == pytest.approx(100, rel=0.03)
    assert a["duration"] == pytest.approx(20, abs=0.1)
    assert a["onset_density"] == pytest.approx(100 / 60, abs=0.4)
    assert a["rms_mean"] > 0 and a["centroid_hz"] > 500 and 0 <= a["energy"] <= 1


def test_tempo_of_a_faster_click_track(tmp_path):
    a = music.analyze(write(tmp_path / "c128.wav", click_track(128, 20)))
    assert a["tempo"] == pytest.approx(128, rel=0.03)


@pytest.mark.parametrize("freqs,expected", [((220.0, 261.63, 329.63), ("A", "minor")), ((261.63, 329.63, 392.0), ("C", "major")),
                                            ((196.0, 246.94, 293.66), ("G", "major"))])
def test_key_estimate_of_a_triad(tmp_path, freqs, expected):
    a = music.analyze(write(tmp_path / "k.wav", chord(freqs, 10)))
    assert (a["key"]["tonic"], a["key"]["mode"]) == expected
    assert 0 < a["key"]["confidence"] <= 1


def test_leading_silence_detected_and_in_point_skips_it(tmp_path):
    p = write(tmp_path / "s.wav", chord((220.0, 329.63), 12, lead_silence=2.0))
    a = music.analyze(p)
    assert a["leading_silence_s"] == pytest.approx(2.0, abs=0.25)
    s = music.suggest_points(a, need_s=8)
    assert s["in_point"] >= a["leading_silence_s"] - 0.05 and s["fits"] is True
    assert s["fade_in_s"] > 0 and s["fade_out_s"] > s["fade_in_s"]
    assert s["out_point"] == pytest.approx(s["in_point"] + 8, abs=1e-6)


def test_track_too_short_is_reported_not_hidden(tmp_path):
    a = music.analyze(write(tmp_path / "short.wav", chord((220.0, 329.63), 5)))
    s = music.suggest_points(a, need_s=30)
    assert s["fits"] is False and "loop" in s["note"].lower()


def test_sparse_pad_sits_under_speech_better_than_busy_hats(tmp_path):
    cfg = critic.load_config()
    pad = music.analyze(write(tmp_path / "pad.wav", chord((220.0, 261.63, 329.63), 20, amp=0.08)))
    busy = music.analyze(write(tmp_path / "busy.wav", busy_hats(20)))
    sp, sb = music.score_under_speech(pad, cfg), music.score_under_speech(busy, cfg)
    assert sp["score"] > 0.8 > sb["score"] + 0.2
    assert sb["problems"] and any("onset" in p for p in sb["problems"])


def test_mood_scoring_prefers_the_matching_energy(tmp_path):
    cfg = critic.load_config()
    calm = music.analyze(write(tmp_path / "calm.wav", chord((220.0, 261.63, 329.63), 20, amp=0.08)))
    drive = music.analyze(write(tmp_path / "drive.wav", drum_loop(128, 20)))
    assert music.score_mood(calm, "reflective", cfg)["score"] > music.score_mood(drive, "reflective", cfg)["score"]
    assert music.score_mood(drive, "driving", cfg)["score"] > music.score_mood(calm, "driving", cfg)["score"]
    with pytest.raises(ValueError, match="unknown mood"):
        music.score_mood(calm, "spooky-fun", cfg)


def test_pick_ranks_folder_with_reasons_and_skips_non_audio(tmp_path):
    d = tmp_path / "lib"
    d.mkdir()
    write(d / "pad.wav", chord((220.0, 261.63, 329.63), 40, amp=0.08))
    write(d / "busy.wav", busy_hats(40))
    write(d / "tiny.wav", chord((220.0,), 4))
    (d / "readme.txt").write_text("x")
    res = music.pick(d, mood="reflective", need_s=20)
    names = [r["file"].split("/")[-1] for r in res["ranked"]]
    assert names[0] == "pad.wav" and "readme.txt" not in names
    top = res["ranked"][0]
    assert top["fits"] and 0 <= top["score"] <= 1 and top["reason"] and 0 < top["confidence"] <= 1
    assert top["suggest"]["in_point"] >= 0
    tiny = next(r for r in res["ranked"] if r["file"].endswith("tiny.wav"))
    assert tiny["fits"] is False and tiny["score"] < top["score"]
    assert "license" in res["reminder"].lower()


def test_pick_on_empty_or_missing_folder_is_a_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        music.pick(tmp_path / "nope", mood="warm", need_s=10)
    (tmp_path / "empty").mkdir()
    assert music.pick(tmp_path / "empty", mood="warm", need_s=10)["ranked"] == []


def test_unreadable_audio_is_reported_per_file(tmp_path):
    d = tmp_path / "lib"
    d.mkdir()
    (d / "broken.mp3").write_bytes(b"not audio")
    write(d / "ok.wav", chord((220.0,), 10))
    res = music.pick(d, mood="warm", need_s=5)
    assert any("broken.mp3" in e for e in res["errors"]) and len(res["ranked"]) == 1
