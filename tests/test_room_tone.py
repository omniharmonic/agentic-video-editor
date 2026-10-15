"""W5: cleanup fills tracks.room_tone from the quietest stretch of the recorder; compile renders gaps with it at matched level.

Synthetic recorder with a KNOWN noise floor: the assertions measure the rendered gap against the floor that surrounds it.
"""

from __future__ import annotations

import numpy as np
import pytest
import soundfile as sf
from scipy.signal import butter, lfilter

from ave import audio, cleanup, editdoc
from ave import compile as comp
from ave.episode import Episode
from conftest import needs_ffmpeg

SR = 48000
RT = cleanup.load_policy()["room_tone"]


def db(x: np.ndarray) -> float:
    return float(20 * np.log10(np.sqrt(np.mean(np.asarray(x, np.float64) ** 2)) + 1e-12))


def noise(n: int, rms_db: float, seed: int, sr: int = SR) -> np.ndarray:
    """Low-passed (room-like) noise at exactly `rms_db` dBFS."""
    b, a = butter(2, 3000, btype="low", fs=sr)
    x = lfilter(b, a, np.random.default_rng(seed).standard_normal(n))
    return (x / np.sqrt(np.mean(x ** 2)) * 10 ** (rms_db / 20)).astype(np.float32)


def speech_bursts(n: int, seed: int, sr: int = SR, level_db: float = -22.0) -> np.ndarray:
    """Syllable-like bursts separated by real pauses (so a 'noise floor' can be measured between them)."""
    rng = np.random.default_rng(seed)
    out = np.zeros(n, np.float32)
    pos = int(0.3 * sr)
    b, a = butter(2, [200, 2500], btype="band", fs=sr)
    while pos < n:
        d = int(rng.uniform(0.15, 0.6) * sr)
        burst = lfilter(b, a, rng.standard_normal(min(d, n - pos)))
        out[pos:pos + len(burst)] = burst / np.sqrt(np.mean(burst ** 2)) * 10 ** (level_db / 20)
        pos += d + int(rng.uniform(0.25, 0.8) * sr)
    return out


def recorder(seconds: float, floor_db: float, quiet_at: float, quiet_db: float, seed: int = 1) -> tuple[np.ndarray, np.ndarray]:
    """(mono recorder, its noise bed). Speech over a stationary floor, with one genuinely quiet 3 s stretch at `quiet_at`
    (no speech there, floor lowered to `quiet_db`)."""
    n = int(seconds * SR)
    bed = noise(n, floor_db, seed)
    q0, q1 = int(quiet_at * SR), int((quiet_at + 3.0) * SR)
    bed[q0:q1] = noise(q1 - q0, quiet_db, seed + 100)
    sp = speech_bursts(n, seed + 7)
    sp[q0 - SR:q1 + SR] = 0.0
    return bed + sp, bed


# ---- picking the stretch ----------------------------------------------------------------------------------------------

def test_picks_the_quietest_steady_window_and_skips_transients_and_digital_silence():
    sr = 16000
    x, _ = recorder(120.0, -50.0, 70.0, -58.0)
    x = x[::3].copy()                                          # decimate like the 16 kHz analysis audio
    x[int(20 * sr):int(24 * sr)] = 0.0                         # a recorder pad of digital silence: quieter than anything, not a room
    x[int(92 * sr):int(96 * sr)] = noise(4 * sr, -64.0, 5, sr)   # an even quieter stretch...
    for k in (92.5, 93.5, 94.5, 95.5):
        x[int(k * sr)] += 0.5                                  # ...with clicks in it: not steady
    hit = cleanup.pick_room_tone(x, sr, 0.0, 120.0, RT)
    assert hit is not None
    assert 69.9 <= hit["f0"] and hit["f1"] <= 73.1, hit         # inside the real quiet stretch
    assert hit["f1"] - hit["f0"] >= RT["min_s"] - 1e-6
    assert hit["rms_db"] == pytest.approx(-58.0, abs=1.0)


def test_no_quiet_stretch_gives_none():
    sr = 16000
    x = speech_bursts(30 * sr, 3, sr=sr, level_db=-20.0)       # speech with gaps shorter than 2 s and NO floor: nothing steady and quiet
    assert cleanup.pick_room_tone(x, sr, 0.0, 30.0, RT) is None


def _ep_with_analysis(tmp_path, x16: np.ndarray, offset: float = 0.0, drift_ppm: float = 0.0, sync_map=None):
    ep = Episode(tmp_path / "ep")
    ep.create("rt")
    (ep.raw).mkdir(parents=True, exist_ok=True)
    wav = audio.analysis_wav_path(ep, "raw/master.wav")
    wav.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(wav), x16, 16000, subtype="PCM_16")
    src = {"file": "raw/master.wav", "kind": "audio", "start_offset": offset, "drift_ppm": drift_ppm}
    if sync_map:
        src["sync_map"] = sync_map
    doc = editdoc.new_doc("rough", 30, (640, 360), {"M": src}, 100.0)
    doc["tracks"]["dialogue"] = [{"id": "d0", "src": "M", "o0": 0.0, "o1": 100.0, "e0": offset + 5.0}]
    return ep, doc


def test_auto_room_tone_is_in_episode_time_and_follows_offset_and_the_piecewise_map(tmp_path):
    x, _ = recorder(130.0, -50.0, 60.0, -58.0)
    x16 = x[::3]
    ep, doc = _ep_with_analysis(tmp_path, x16, offset=4.0)
    rt, warn = cleanup.auto_room_tone(ep, doc, cleanup.load_policy())
    assert warn is None and rt["src"] == "M" and rt["auto"]
    assert 64.0 <= rt["e0"] and rt["e1"] <= 67.2 and rt["e1"] - rt["e0"] >= 2.0       # file 60-63 s + 4 s offset
    # the same file with a sync map: episode time goes through editdoc.episode_time, so the range moves with the map
    sm = [[0.0, 4.0], [65.0, 4.5], [130.0, 4.9]]
    ep2, doc2 = _ep_with_analysis(tmp_path / "b", x16, offset=4.0, sync_map=sm)
    rt2, _ = cleanup.auto_room_tone(ep2, doc2, cleanup.load_policy())
    src = doc2["sources"]["M"]
    assert editdoc.src_time(src, rt2["e0"]) == pytest.approx(60.0, abs=1.1)


def test_speaker_per_channel_master_uses_the_sum_for_the_choice(tmp_path):
    """Two mics, one host each. The analysis audio is the L+R sum, so a stretch where EITHER mic is loud is not room tone."""
    n = 100 * 16000
    left = noise(n, -52.0, 1, 16000) + speech_bursts(n, 2, 16000)
    right = noise(n, -52.0, 3, 16000) + speech_bursts(n, 4, 16000)
    left[40 * 16000:43 * 16000] = noise(3 * 16000, -60.0, 8, 16000)      # left quiet here, right is still talking
    right[70 * 16000:73 * 16000] = noise(3 * 16000, -60.0, 9, 16000)
    left[70 * 16000:73 * 16000] = noise(3 * 16000, -60.0, 10, 16000)     # both quiet here: the only real room tone
    both = audio.soft_limit(left + right)
    hit = cleanup.pick_room_tone(both, 16000, 0.0, 100.0, RT)
    assert hit is not None and 69.9 <= hit["f0"] and hit["f1"] <= 73.1, hit
    only_left = cleanup.pick_room_tone(left, 16000, 0.0, 100.0, RT)     # judged on one channel alone the wrong stretch can win
    assert only_left is not None


def test_cleanup_disabled_or_unsuitable_leaves_the_track_empty_with_a_warning(tmp_path):
    x16 = speech_bursts(40 * 16000, 5, 16000, level_db=-20.0)
    ep, doc = _ep_with_analysis(tmp_path, x16)
    rt, warn = cleanup.auto_room_tone(ep, doc, cleanup.load_policy())
    assert rt is None and "no room tone" in warn
    pol = cleanup.load_policy()
    pol["room_tone"]["enabled"] = False
    assert cleanup.auto_room_tone(ep, doc, pol) == (None, None)


# ---- rendering the gap --------------------------------------------------------------------------------------------------

def _gap_doc(rt: dict, gap_s: float = 3.0):
    d = editdoc.new_doc("select", 30, (640, 360), {"M": {"file": "raw/master.wav", "kind": "audio", "start_offset": 0.0}}, 6.0 + gap_s + 6.0)
    d["tracks"]["dialogue"] = [
        {"id": "a", "src": "M", "o0": 0.0, "o1": 6.0, "e0": 10.0, "xfade_ms": 0, "gain_db": 0.0},
        {"id": "gap", "src": "M", "o0": 6.0, "o1": 6.0 + gap_s, "e0": 16.0, "xfade_ms": 0, "gain_db": 0.0, "fill": "room_tone"},
        {"id": "b", "src": "M", "o0": 6.0 + gap_s, "o1": 12.0 + gap_s, "e0": 20.0, "xfade_ms": 0, "gain_db": 0.0}]
    d["tracks"]["room_tone"] = rt
    return d


def _recorder_ep(tmp_path, floor_db: float, quiet_db: float):
    x, bed = recorder(100.0, floor_db, 60.0, quiet_db)
    ep = Episode(tmp_path / "ep")
    ep.create("rt")
    ep.raw.mkdir(parents=True, exist_ok=True)
    sf.write(str(ep.raw / "master.wav"), np.stack([x, x], axis=1), SR, subtype="FLOAT")
    return ep, bed


def _render_gap(tmp_path, floor_db: float, quiet_db: float):
    ep, bed = _recorder_ep(tmp_path, floor_db, quiet_db)
    hit = cleanup.pick_room_tone(x16 := ep_mono16(ep), 16000, 0.0, 100.0, RT)
    assert hit is not None
    doc = _gap_doc({"src": "M", "e0": round(hit["f0"], 3), "e1": round(hit["f1"], 3)})
    out = tmp_path / "gap.wav"
    comp.compile_audio(ep, doc, out)
    data, sr = sf.read(str(out), dtype="float32", always_2d=True)
    gap = data[int(6.2 * sr):int(8.8 * sr), 0]                        # inside the fill, clear of the seams
    # the surroundings' floor, measured from the clean bed under the two neighbouring clips (the truth, not an estimate)
    around = np.concatenate([bed[int(10.0 * SR):int(16.0 * SR)], bed[int(20.0 * SR):int(26.0 * SR)]])
    return db(gap), db(around), x16


def ep_mono16(ep: Episode) -> np.ndarray:
    x, sr = sf.read(str(ep.raw / "master.wav"), dtype="float32", always_2d=True)
    return x[::3, 0]


@needs_ffmpeg
def test_gap_is_rendered_at_the_level_of_the_surrounding_noise_floor(tmp_path):
    """The quietest window is a few dB under the typical floor; the gap must still sit at the floor around it, not below it."""
    gap_db, around_db, _ = _render_gap(tmp_path, floor_db=-48.0, quiet_db=-52.0)
    assert abs(gap_db - around_db) <= 1.5, (gap_db, around_db)


@needs_ffmpeg
def test_gap_matches_even_when_the_quiet_stretch_is_much_quieter(tmp_path):
    gap_db, around_db, _ = _render_gap(tmp_path, floor_db=-46.0, quiet_db=-54.0)
    assert abs(gap_db - around_db) <= 1.5, (gap_db, around_db)


@needs_ffmpeg
def test_gap_with_matching_floor_is_left_alone(tmp_path):
    ep, bed = _recorder_ep(tmp_path, -50.0, -50.0)
    room = bed[int(60.0 * SR):int(63.0 * SR)]
    around = [speech_bursts(3 * SR, 11) + noise(3 * SR, -50.0, 12)]
    assert comp.match_gain_db(np.stack([room, room], axis=1), [np.stack([a, a], axis=1) for a in around]) == 0.0


def test_neighbours_without_a_pause_are_not_trusted_as_a_floor():
    sr_block = 3 * SR
    tone = (0.2 * np.sin(np.arange(sr_block) / SR * 2 * np.pi * 220)).astype(np.float32)
    room = noise(2 * SR, -50.0, 1)
    assert comp.noise_floor_db(tone) is None
    assert comp.match_gain_db(room, [tone]) == 0.0
