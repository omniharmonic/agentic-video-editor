"""Music analysis and selection: what a track IS (tempo, key, density, brightness) and whether it can sit under a voice.

We analyse files the human already has the right to use. We do NOT download from music services (see
docs/asset-licensing.md); a track entering an edit still needs a license-log entry (`ave assets log-license`).
Targets (moods, speech-masking limits) live in config/assets.critic.yaml so taste stays data.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".flac", ".aif", ".aiff", ".ogg", ".aac"}
SR = 22050
HOP = 512
MAX_ANALYSIS_S = 300  # a 6-minute track is judged by its first 5; enough for tempo/key/density and bounded in time

# Krumhansl-Kessler key profiles (public-domain psychology data), C-rooted.
_KS_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_KS_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
_NOTES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def estimate_key(chroma_mean: np.ndarray) -> dict:
    """Best of 24 keys by correlation with the KK profiles; confidence = margin over the runner-up (0-1)."""
    scores = []
    for mode, prof in (("major", _KS_MAJOR), ("minor", _KS_MINOR)):
        for i in range(12):
            scores.append((float(np.corrcoef(chroma_mean, np.roll(prof, i))[0, 1]), _NOTES[i], mode))
    scores.sort(reverse=True)
    best, second = scores[0], scores[1]
    return {"tonic": best[1], "mode": best[2], "confidence": round(float(max(0.0, min(1.0, (best[0] - second[0]) * 4 + 0.2 * best[0]))), 3),
            "correlation": round(best[0], 3)}


_BEAT_SCRIPT = (
    "import sys, numpy as np, librosa\n"
    "env = np.load(sys.argv[1]); sr = int(sys.argv[2]); hop = int(sys.argv[3])\n"
    "t, b = librosa.beat.beat_track(onset_envelope=env, sr=sr, hop_length=hop, start_bpm=100)\n"
    "np.save(sys.argv[4], np.concatenate([np.atleast_1d(t).astype(float), np.asarray(b, dtype=float)]))\n"
)


def _numpy_beats(env: np.ndarray, sr: int) -> tuple[float, np.ndarray]:
    """Dependency-light fallback: tempo from the onset envelope's autocorrelation (60-200 bpm), beats at its strongest phase."""
    e = env - env.mean()
    ac = np.correlate(e, e, mode="full")[len(e) - 1:]
    fps = sr / HOP
    lags = np.arange(int(fps * 60 / 200), int(fps * 60 / 60) + 1)
    lags = lags[lags < len(ac)]
    if not len(lags):
        return 0.0, np.array([], dtype=int)
    w = np.exp(-0.5 * (np.log2(60 * fps / lags / 100)) ** 2)      # prior centred on 100 bpm
    lag = int(lags[np.argmax(ac[lags] * w)])
    phase = int(np.argmax([e[o::lag].sum() for o in range(lag)]))
    return 60 * fps / lag, np.arange(phase, len(e), lag)


def _track_beats(env: np.ndarray, sr: int) -> tuple[float, np.ndarray]:
    """librosa's beat tracker, run in a child process: its numba kernel has segfaulted on some Linux machines, and a crash
    there must not take the whole run down. Falls back to the numpy estimate."""
    import subprocess
    import sys
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        src, out = Path(td) / "env.npy", Path(td) / "out.npy"
        np.save(src, env)
        try:
            r = subprocess.run([sys.executable, "-c", _BEAT_SCRIPT, str(src), str(sr), str(HOP), str(out)],
                               capture_output=True, timeout=120)
            if r.returncode == 0 and out.exists():
                arr = np.load(out)
                return float(arr[0]), arr[1:].astype(int)
        except (subprocess.TimeoutExpired, OSError):
            pass
    return _numpy_beats(env, sr)


def analyze(path: Path | str) -> dict:
    import librosa
    path = Path(path)
    y, sr = librosa.load(str(path), sr=SR, mono=True, duration=MAX_ANALYSIS_S)
    full = librosa.get_duration(path=str(path))
    if len(y) < SR:
        raise ValueError(f"{path.name}: under one second of audio")
    env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP)
    tempo, beats = _track_beats(env, sr)
    beat_t = librosa.frames_to_time(beats, sr=sr, hop_length=HOP)
    ibi = np.diff(beat_t)
    tempo_conf = float(max(0.0, 1 - np.std(ibi) / np.mean(ibi))) if len(ibi) > 3 and np.mean(ibi) > 0 else 0.0
    onsets = librosa.onset.onset_detect(onset_envelope=env, sr=sr, hop_length=HOP, backtrack=False)
    dur = len(y) / sr
    rms = librosa.feature.rms(y=y, hop_length=HOP)[0]
    centroid = librosa.feature.spectral_centroid(y=y, sr=sr, hop_length=HOP)[0]
    loud = rms > rms.max() * 0.01 if rms.max() > 0 else rms > 0   # -40 dB of the peak frame
    lead = float(np.argmax(loud) * HOP / sr) if loud.any() else dur
    weights = rms / rms.sum() if rms.sum() > 0 else None
    chroma = librosa.feature.chroma_cqt(y=y[: sr * 90], sr=sr, hop_length=HOP * 4).mean(axis=1)
    od = len(onsets) / dur
    rms_mean = float(rms.mean())
    return {
        "file": str(path), "duration": round(full, 3), "analyzed_s": round(dur, 2), "tempo": round(tempo, 1),
        "tempo_confidence": round(tempo_conf, 3), "beat_times": [round(float(b), 3) for b in beat_t[:600]],
        "key": estimate_key(chroma), "onset_density": round(od, 3), "rms_mean": round(rms_mean, 5),
        "rms_cv": round(float(rms.std() / rms.mean()) if rms.mean() > 0 else 0.0, 3),
        "centroid_hz": round(float(np.average(centroid, weights=weights)) if weights is not None else 0.0, 1),
        "peak_db": round(float(20 * np.log10(max(1e-9, np.abs(y).max()))), 2), "leading_silence_s": round(lead, 3),
        "energy": round(float(min(1.0, 0.5 * od / 5 + 0.5 * rms_mean / 0.2)), 3),
    }


def _range_fit(x: float, lo: float, hi: float) -> float:
    if lo <= x <= hi:
        return 1.0
    return max(0.0, 1 - (lo - x if x < lo else x - hi) / max(hi - lo, 1e-6))


def score_mood(a: dict, mood: str, cfg: dict) -> dict:
    moods = cfg["music"]["moods"]
    if mood not in moods:
        raise ValueError(f"unknown mood '{mood}' (known: {', '.join(moods)})")
    m = moods[mood]
    # Tempo estimates are often an octave off; take the best reading but charge a little for doubting the estimate.
    tempo_fit = max(_range_fit(a["tempo"], *m["tempo"]), 0.9 * _range_fit(a["tempo"] / 2, *m["tempo"]), 0.9 * _range_fit(a["tempo"] * 2, *m["tempo"]))
    parts = {"tempo": tempo_fit, "centroid": _range_fit(a["centroid_hz"], *m["centroid"]), "rms": _range_fit(a["rms_mean"], *m["rms"]),
             "onset_density": _range_fit(a["onset_density"], *m["onset_density"])}
    # A track with no steady pulse says little by its "tempo"; weight it down rather than let noise decide.
    w = {"tempo": 0.3 if a["tempo_confidence"] >= 0.5 else 0.1, "centroid": 0.25, "rms": 0.2, "onset_density": 0.25}
    score = sum(parts[k] * w[k] for k in parts) / sum(w.values())
    off = [f"{k} {a[{'centroid': 'centroid_hz', 'rms': 'rms_mean'}.get(k, k)]} outside {m[k]}" for k, v in parts.items() if v < 0.6]
    return {"mood": mood, "score": round(score, 3), "parts": {k: round(v, 3) for k, v in parts.items()}, "off_target": off}


def score_under_speech(a: dict, cfg: dict) -> dict:
    u = cfg["music"]["under_speech"]
    problems = []
    od = 1.0 if a["onset_density"] <= u["onset_density_max"] else max(0.0, 1 - (a["onset_density"] - u["onset_density_max"]) / u["onset_density_max"])
    if od < 1:
        problems.append(f"onset density {a['onset_density']}/s exceeds {u['onset_density_max']}/s: busy rhythm fights consonants")
    ct = 1.0 if a["centroid_hz"] <= u["centroid_max_hz"] else max(0.0, 1 - (a["centroid_hz"] - u["centroid_max_hz"]) / u["centroid_max_hz"])
    if ct < 1:
        problems.append(f"spectral centroid {a['centroid_hz']} Hz above {u['centroid_max_hz']} Hz: energy in the speech band")
    cv = 1.0 if a["rms_cv"] <= u["rms_variation_max"] else max(0.0, 1 - (a["rms_cv"] - u["rms_variation_max"]))
    if cv < 1:
        problems.append(f"level swings (cv {a['rms_cv']}) exceed {u['rms_variation_max']}: hard to keep a steady mix")
    return {"score": round(0.45 * od + 0.3 * ct + 0.25 * cv, 3), "problems": problems}


def suggest_points(a: dict, need_s: float, cfg: dict | None = None) -> dict:
    """In-point after any lead-in silence (snapped forward to the next beat when the pulse is steady), fades, and fit."""
    from . import critic
    mcfg = (cfg or critic.load_config())["music"]
    start = a["leading_silence_s"]
    beats = [b for b in a.get("beat_times", []) if b >= start - 1e-6]
    if a["tempo_confidence"] >= 0.5 and beats and beats[0] - start < 60 / max(a["tempo"], 1):
        start = beats[0]
    start = round(start, 3)
    fits = start + need_s <= a["duration"] + 1e-6
    fin = round(min(mcfg["default_fade_in_s"], need_s * 0.2), 2)
    fout = round(min(mcfg["default_fade_out_s"], need_s * 0.3), 2)
    note = "fits" if fits else (f"track has {max(0.0, a['duration'] - start):.1f}s after the in-point but {need_s:.1f}s are needed: "
                               "loop a clean section, use a shorter cut of the song, or pick another track")
    return {"in_point": start, "out_point": round(start + need_s, 3), "fade_in_s": fin, "fade_out_s": fout, "fits": fits, "note": note}


def pick(folder: Path | str, mood: str, need_s: float, cfg: dict | None = None, under_speech: bool = True, limit: int = 10) -> dict:
    """Rank every track in a folder for a mood and for sitting under speech. Reasons and confidence on every row."""
    from . import critic
    cfg = cfg or critic.load_config()
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        raise FileNotFoundError(f"{folder} is not a folder of tracks")
    rows, errors = [], []
    for f in sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTS and not p.name.startswith(".")):
        try:
            a = analyze(f)
        except Exception as e:  # one corrupt file must not hide the rest
            errors.append(f"{f.name}: {e}")
            continue
        mo = score_mood(a, mood, cfg)
        sp = score_under_speech(a, cfg)
        sug = suggest_points(a, need_s, cfg)
        score = (0.5 * mo["score"] + 0.5 * sp["score"]) if under_speech else mo["score"]
        if not sug["fits"]:
            score *= 0.5
        why = [f"mood '{mood}' fit {mo['score']:.2f}"] + ([f"under-speech {sp['score']:.2f}"] if under_speech else [])
        why += mo["off_target"] + (sp["problems"] if under_speech else []) + ([] if sug["fits"] else [sug["note"]])
        conf = round(max(0.2, min(0.9, 0.4 + 0.3 * a["tempo_confidence"] + 0.3 * a["key"]["confidence"])), 2)
        rows.append({"file": str(f), "score": round(score, 3), "fits": sug["fits"], "reason": "; ".join(why), "confidence": conf,
                     "analysis": {k: a[k] for k in ("duration", "tempo", "tempo_confidence", "key", "onset_density", "rms_mean", "centroid_hz", "energy")},
                     "mood": mo, "under_speech": sp, "suggest": sug})
    rows.sort(key=lambda r: -r["score"])
    return {"mood": mood, "need_s": need_s, "ranked": rows[:limit], "errors": errors,
            "reminder": "Scores say how a track SOUNDS, not that you may use it. Before it enters an edit, record its license: "
                        "ave assets log-license <episode> --file ... (see docs/asset-licensing.md)."}
