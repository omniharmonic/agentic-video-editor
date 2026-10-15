"""Sync must recover a known offset to within a fraction of a video frame, and detect clock drift."""
import numpy as np
import soundfile as sf
from scipy.signal import butter, lfilter, resample_poly

from ave.sync import sync_pair

SR = 16000


def speechlike(seconds: float, seed: int = 1) -> np.ndarray:
    """Bursty band-limited noise: irregular 'syllables' separated by pauses, like speech energy."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    out = np.zeros(n, dtype=np.float32)
    pos = 0
    while pos < n:
        dur = int(rng.uniform(0.08, 0.5) * SR)
        gap = int(rng.uniform(0.05, 0.6) * SR)
        burst = rng.standard_normal(min(dur, n - pos)).astype(np.float32)
        b, a = butter(2, [rng.uniform(150, 400), rng.uniform(1500, 3500)], btype="band", fs=SR)
        out[pos:pos + len(burst)] = lfilter(b, a, burst) * rng.uniform(0.3, 1.0)
        pos += dur + gap
    return out / np.abs(out).max()


def other_camera(ref: np.ndarray, start_offset: float, seed: int = 7) -> np.ndarray:
    """Same event heard by another camera: different EQ and noise floor, starting `start_offset` later."""
    rng = np.random.default_rng(seed)
    b, a = butter(2, 2500, btype="low", fs=SR)
    x = lfilter(b, a, ref) * 0.6 + rng.standard_normal(len(ref)).astype(np.float32) * 0.02
    k = int(round(abs(start_offset) * SR))
    if start_offset >= 0:
        return x[k:].astype(np.float32)               # started later: missed the first k samples
    return np.concatenate([rng.standard_normal(k).astype(np.float32) * 0.02, x]).astype(np.float32)  # started earlier


def write(tmp_path, name, x):
    p = tmp_path / name
    sf.write(p, x, SR, subtype="PCM_16")
    return p


def test_recovers_positive_and_negative_offsets(tmp_path):
    ref = speechlike(90)
    ref_wav = write(tmp_path, "ref.wav", ref)
    for true in (3.2, -1.5, 0.0, 11.737):
        r = sync_pair(ref_wav, write(tmp_path, f"o{true}.wav", other_camera(ref, true)))
        assert abs(r["start_offset"] - true) < 0.005, (true, r)   # < 5 ms, far under one frame at 30 fps (33 ms)
        assert r["psr"] > 25


def test_detects_clock_drift(tmp_path):
    ref = speechlike(1500, seed=3)
    ref_wav = write(tmp_path, "ref.wav", ref)
    cam = other_camera(ref, 2.0)
    # 100 ppm clock difference => 1500 s * 1e-4 = 150 ms of drift by the end.
    drifted = resample_poly(cam, 10000, 10001).astype(np.float32)
    r = sync_pair(ref_wav, write(tmp_path, "drift.wav", drifted))
    assert abs(r["start_offset"] - 2.0) < 0.01
    # 100 ppm = 360 ms/hour. Sign: the drifted camera runs slow, so it falls behind (negative).
    assert r["drift_ms_per_hour"] is not None and 300 < abs(r["drift_ms_per_hour"]) < 420, r
    assert 100 < abs(r["drift_ms"]) < 200, r


def test_no_shared_audio_has_low_confidence(tmp_path):
    ref_wav = write(tmp_path, "ref.wav", speechlike(60, seed=1))
    r = sync_pair(ref_wav, write(tmp_path, "unrelated.wav", speechlike(60, seed=99)))
    assert r["psr"] < 25


# ---- N2: offsets past half the FFT, and the lag mapping -------------------------------------------------------------

def test_lag_axis_maps_positive_lags_below_len_ref_and_negative_above_nfft_minus_len_sig():
    from ave.sync import lag_axis
    nfft, nref, nsig = 64, 40, 10
    idx, lags = lag_axis(nfft, nref, nsig)
    assert set(lags) == set(range(-(nsig - 1), nref))                    # every valid lag exactly once
    assert dict(zip(lags, idx))[39] == 39 and dict(zip(lags, idx))[-1] == nfft - 1 and dict(zip(lags, idx))[-9] == nfft - 9
    assert dict(zip(lags, idx))[nfft // 2 + 2] == nfft // 2 + 2 if nfft // 2 + 2 < nref else True


def test_gcc_phat_recovers_a_lag_beyond_half_the_fft_length():
    """Regression: the old code split lags at nfft//2 and reported a +90 s lag as a negative number."""
    from ave.sync import gcc_phat
    ref = speechlike(100, seed=11)
    sig = ref[90 * SR:100 * SR]                       # starts 90 s into ref; nfft = 2^21 samples (131 s), half = 65 s
    lag, psr = gcc_phat(ref, sig, sr=SR)
    assert abs(lag - 90.0) < 0.001 and psr > 25


def test_sync_pair_finds_an_offset_beyond_half_the_envelope_fft(tmp_path):
    ref = speechlike(400, seed=5)
    ref_wav = write(tmp_path, "ref.wav", ref)
    cam = other_camera(ref[350 * SR:380 * SR], 0.0)   # a 30 s camera that started 350 s into the recorder
    r = sync_pair(ref_wav, write(tmp_path, "cam.wav", cam))
    assert abs(r["start_offset"] - 350.0) < 0.005, r
    assert r["psr_min"] > 25


# ---- N3: drift is fitted, not just flagged --------------------------------------------------------------------------

def test_fit_recovers_offset_and_drift_ppm_with_points_across_the_take(tmp_path):
    ref = speechlike(600, seed=3)
    ref_wav = write(tmp_path, "ref.wav", ref)
    cam = resample_poly(other_camera(ref, 5.0), 12500, 12501).astype(np.float32)   # camera clock 80 ppm slow
    r = sync_pair(ref_wav, write(tmp_path, "cam.wav", cam))
    assert abs(r["start_offset"] - 5.0) < 0.005, r
    assert abs(r["drift_ppm"] - 80.0) < 10.0, r
    assert len(r["points"]) == 5 and r["psr_min"] == min(p["psr"] for p in r["points"]) and r["psr"] == r["psr_min"]
    assert r["residual_ms"] < 5.0
    spread = [p["t"] for p in r["points"]]
    assert spread[0] < 60 and spread[-1] > 540        # points really span the take
    r3 = sync_pair(ref_wav, write(tmp_path, "cam.wav", cam), points=3)
    assert len(r3["points"]) == 3


def test_nonlinear_drift_shows_up_as_residual(tmp_path):
    ref = speechlike(600, seed=4)
    cam = other_camera(ref, 2.0)
    warped = np.concatenate([cam[:300 * SR], resample_poly(cam[300 * SR:], 2500, 2501)])    # the clock rate changes by 400 ppm halfway
    r = sync_pair(write(tmp_path, "ref.wav", ref), write(tmp_path, "w.wav", warped))
    assert r["linear_residual_ms"] > 20, r        # a clean linear case leaves < 5 ms
    assert r["model"] == "piecewise" and r["sync_map"], r      # ...and the measured points are kept as a map
    assert r["residual_ms"] < 10, r                              # which follows the warp at every sample point


def test_reference_prefers_a_recorder_longer_than_every_camera():
    from ave.sync import pick_reference
    cam1 = {"file": "a.mov", "kind": "video", "duration": 100.0, "audio": [1]}
    cam2 = {"file": "b.mov", "kind": "video", "duration": 120.0, "audio": [1]}
    rec = {"file": "r.wav", "kind": "audio", "duration": 150.0, "audio": [1]}
    assert pick_reference([cam1, cam2, rec], [], None)["file"] == "r.wav"
    short = dict(rec, duration=90.0)
    assert pick_reference([cam1, cam2, short], [], None)["file"] == "b.mov"      # not longer than every camera: longest file
    assert pick_reference([cam1, cam2, rec], [], "a.mov")["file"] == "a.mov"


def test_coverage_warning_when_the_reference_cannot_contain_a_file():
    from ave.sync import coverage_warnings
    assert coverage_warnings("x.mov", 0.0, 500.0, 100.0)
    assert coverage_warnings("x.mov", 90.0, 30.0, 100.0)
    assert not coverage_warnings("x.mov", 10.0, 30.0, 100.0)


# ---- W3: piecewise-linear drift --------------------------------------------------------------------------------------

def wobbly_camera(ref: np.ndarray, a: float, b: float, amp_s: float, period_s: float) -> tuple[np.ndarray, callable]:
    """A camera whose clock wobbles: file time t sits at reference time e(t) = a + (1+b)*t + amp*sin(2 pi t / period)."""
    def e_of(t):
        return a + (1 + b) * np.asarray(t, float) + amp_s * np.sin(2 * np.pi * np.asarray(t, float) / period_s)
    n = int((len(ref) / SR - a) * 0.98 * SR)
    t = np.arange(n) / SR
    rng = np.random.default_rng(9)
    bb, aa = butter(2, 2500, btype="low", fs=SR)
    mic = lfilter(bb, aa, ref) * 0.6 + rng.standard_normal(len(ref)).astype(np.float32) * 0.02
    cam = np.interp(e_of(t) * SR, np.arange(len(mic)), mic).astype(np.float32)
    return cam, e_of


def test_piecewise_map_follows_a_wobbling_clock_within_10ms_at_every_sample_point(tmp_path):
    from ave import editdoc
    ref = speechlike(900, seed=12)
    ref_wav = write(tmp_path, "ref.wav", ref)
    cam, e_of = wobbly_camera(ref, 3.0, 60e-6, 0.045, 700.0)        # 45 ms wobble: a straight line cannot follow it
    r = sync_pair(ref_wav, write(tmp_path, "cam.wav", cam), points=9)
    assert r["linear_residual_ms"] > 20, r                           # the line alone is bad...
    assert r["model"] == "piecewise" and len(r["sync_map"]) >= 3, r
    src = {"start_offset": r["start_offset"], "drift_ppm": r["drift_ppm"], "sync_map": r["sync_map"]}
    for p in r["points"]:
        err = abs(editdoc.episode_time(src, p["t_file"]) - float(e_of(p["t_file"])))
        assert err < 0.010, (p, err)                                 # ...and the map is under 10 ms from the TRUTH at each point
    assert r["residual_ms"] < 10, r


def test_clean_linear_clock_stays_linear_and_has_no_map(tmp_path):
    ref = speechlike(600, seed=3)
    cam = resample_poly(other_camera(ref, 5.0), 12500, 12501).astype(np.float32)
    r = sync_pair(write(tmp_path, "ref.wav", ref), write(tmp_path, "cam.wav", cam))
    assert r["model"] == "linear" and "sync_map" not in r


def test_sync_episode_warns_only_when_the_map_is_still_bad(tmp_path, monkeypatch):
    from ave import sync as S
    good = {"start_offset": 1.0, "drift_ppm": 50.0, "psr": 90.0, "psr_min": 90.0, "residual_ms": 3.0, "n_good": 5, "model": "piecewise",
            "linear_residual_ms": 41.0, "points": [], "sync_map": [[10.0, 1.0], [500.0, 1.03], [1000.0, 1.06]]}
    bad = dict(good, sync_map=[[10.0, 1.0], [20.0, 1.2], [1000.0, 1.06]])        # 20 ms/s between the first two = 20000 ppm
    media = {"files": [{"file": "r.wav", "audio": [1], "kind": "audio", "duration": 2000.0},
                       {"file": "a.mov", "audio": [1], "kind": "video", "duration": 1500.0}]}
    from ave.episode import Episode, write_json
    ep = Episode(tmp_path); ep.create()
    write_json(ep.artifact("media.json"), media)
    monkeypatch.setattr(S, "analysis_wav_path", lambda ep_, f: tmp_path / f)
    monkeypatch.setattr(S, "envelope", lambda p: np.zeros(4))
    for res, expect_warn in ((good, False), (bad, True)):
        monkeypatch.setattr(S, "sync_pair", lambda *a, _r=res, **k: dict(_r))
        out = S.sync_episode(ep)
        assert any("piecewise" in w for w in out["warnings"]) is expect_warn, out["warnings"]
        assert not any("not drifting linearly" in w for w in out["warnings"])
