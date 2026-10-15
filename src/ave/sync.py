"""Multicam sync by audio -> work/sync.json: where each file's t=0 sits on the reference timeline, AND how fast its clock runs.

Convention: `start_offset` (a) is where a file's t=0 sits on the reference timeline and `drift_ppm` (b, parts per million)
is how much slower the file's clock runs than the reference: reference time e = a + (1 + b*1e-6) * t_file, so the file's
own time for episode time e is  (e - a) / (1 + b*1e-6)  (editdoc.source_time). +3.2 means the file started 3.2 s AFTER the
reference. Cameras drift 20-100 ppm against a recorder: 0.33 s (10 frames) over a 2 hour take (field note N3).

Algorithm (field notes N2/N3), chosen because the first real episode broke the old one:
  1. COARSE: correlate a decimated (10 ms) log-energy envelope of the file against the WHOLE reference. Envelopes ignore
     mic, gain and EQ differences, and one FFT searches hours. The lag mapping is exact: positive lags are k < len(ref),
     negative lags are k > nfft - len(sig). (The old code split lags at nfft/2 and wrapped anything past it: a 2 h offset
     came out as -3366 s.)
  2. REFINE: GCC-PHAT at >= 5 points spread across the take (windows up to ~60 s, a tight search around the running
     prediction). PHAT whitens the spectrum so it locks onto shared speech/clap transients.
  3. FIT: offset(t) = a + b*t by least squares over the points that passed. Report the straight-line residual. When it
     exceeds MAP_RESIDUAL_MS (20 ms; real cameras wobble 15-50 ms) the measured points are ALSO stored as `sync_map`
     ([[file_t, offset], ...], piecewise linear) and editdoc.src_time interpolates through them, so the lines stay in sync
     mid-take. (a, b) are still written: they are the line fit, and files without `sync_map` behave exactly as before.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.ndimage import uniform_filter1d
from scipy.signal import resample_poly

from .audio import analysis_wav_path
from . import editdoc
from .episode import Episode, read_json, write_json

SR = 8000                 # PHAT correlation rate. 125 us/sample, refined below to sub-sample
MIN_PSR = 25.0            # peak-to-sidelobe ratio. Measured: true matches score 100s-1000s, unrelated audio ~9-10
POINTS = 5                # measurement points across a take
WINDOW_S = 60.0           # PHAT window (shorter for short takes)
MIN_WINDOW_S = 4.0
SEARCH_S = 2.5            # +/- search around the running prediction at each point
ENV_HOP_S = 0.01          # coarse envelope resolution
COARSE_SEG_S = 300.0      # long takes: also correlate a 5 min piece (clock drift smears a 2 h envelope by ~0.6 s)
RESIDUAL_WARN_MS = 35.0   # fit residual above this (about 1 frame at 30 fps) = the clock is not linear; real cameras wobble 15-50 ms
MAP_RESIDUAL_MS = 20.0    # straight-line residual above this: store the measured points as a piecewise map (W3)
MAP_SLOPE_WARN_PPM = 1500.0   # local clock rate between two map points beyond this = a point probably mis-locked
DRIFT_WARN_PPM = 300.0    # real camera clocks are within ~100 ppm; far more means a bad fit
DRIFT_WARN_MS = 20.0      # (legacy key) projected drift over the overlap, about half a frame at 25 fps


def _load(path: Path, start_s: float = 0.0, dur_s: float | None = None) -> np.ndarray:
    info = sf.info(str(path))
    start = int(start_s * info.samplerate)
    frames = -1 if dur_s is None else int(dur_s * info.samplerate)
    data, sr = sf.read(str(path), start=start, frames=frames, dtype="float32", always_2d=False)
    if data.ndim > 1:
        data = data.mean(axis=1)
    if sr != SR:
        g = np.gcd(sr, SR)
        data = resample_poly(data, SR // g, sr // g).astype(np.float32)
    # Remove DC and normalise so loud files don't dominate the correlation.
    data = data - data.mean()
    peak = np.abs(data).max()
    return data / peak if peak > 0 else data


def lag_axis(nfft: int, n_ref: int, n_sig: int) -> tuple[np.ndarray, np.ndarray]:
    """(cc indices, lags) of every VALID lag of a circular cross-correlation of `sig` against `ref`.

    cc[k] for k in [0, n_ref) is lag +k (sig starts k samples after ref's start); a negative lag -m sits at index nfft - m
    for m in [1, n_sig). Everything between is wrap-around garbage. (Splitting at nfft//2 is wrong whenever ref is longer
    than half the FFT: that was the bug that turned a 7119 s offset into -3366 s.)"""
    pos = np.arange(0, n_ref)
    neg = np.arange(nfft - (n_sig - 1), nfft) if n_sig > 1 else np.arange(0)
    idx = np.concatenate([neg, pos])
    lags = np.concatenate([neg - nfft, pos])
    return idx, lags


def gcc_phat(ref: np.ndarray, sig: np.ndarray, max_lag: int | None = None, sr: int = SR) -> tuple[float, float]:
    """Return (lag_seconds, psr). lag > 0 means `sig` starts that many seconds after `ref`.
    PSR (peak-to-sidelobe ratio) is the confidence: how far the peak stands above the noise floor."""
    n = len(ref) + len(sig)
    nfft = 1 << (n - 1).bit_length()
    X = np.fft.rfft(ref, nfft)
    Y = np.fft.rfft(sig, nfft)
    R = X * np.conj(Y)
    R /= np.abs(R) + 1e-12
    cc = np.fft.irfft(R, nfft)
    idx, lags = lag_axis(nfft, len(ref), len(sig))
    if max_lag is not None:
        keep = np.abs(lags) <= max_lag
        idx, lags = idx[keep], lags[keep]
    vals = np.abs(cc[idx])
    j = int(np.argmax(vals))
    k, peak = int(idx[j]), float(vals[j])
    # Peak-to-sidelobe: how far the peak stands above the rest of the search band.
    side = vals[np.abs(lags - lags[j]) > 40]  # exclude the peak's own skirt (5 ms @ 8 kHz)
    psr = float(peak / (side.std() + 1e-12)) if len(side) else 0.0
    # Parabolic interpolation for sub-sample accuracy.
    frac = 0.0
    a, b, c = abs(cc[(k - 1) % nfft]), abs(cc[k]), abs(cc[(k + 1) % nfft])
    denom = a - 2 * b + c
    if denom != 0:
        frac = 0.5 * (a - c) / denom
    return float((lags[j] + frac) / sr), psr


# ---- coarse search ------------------------------------------------------------------------------------------------

def envelope(path: Path) -> np.ndarray:
    """Normalised, detrended log-energy per 10 ms of an audio file (streamed: a 2.5 h file never sits in memory whole).
    Detrending (subtract a 2 s moving mean) removes slow gain and room-level differences between microphones."""
    info = sf.info(str(path))
    frame = max(1, int(round(info.samplerate * ENV_HOP_S)))
    out: list[np.ndarray] = []
    carry = np.zeros(0, np.float32)
    for blk in sf.blocks(str(path), blocksize=frame * 6000, dtype="float32", always_2d=True):
        x = np.concatenate([carry, blk.mean(axis=1)])
        n = len(x) // frame
        carry = x[n * frame:]
        if n:
            out.append((x[:n * frame].reshape(n, frame).astype(np.float64) ** 2).mean(axis=1))
    if len(carry) >= frame // 2:
        out.append(np.array([(carry.astype(np.float64) ** 2).mean()]))
    e = 10 * np.log10(np.concatenate(out) + 1e-10) if out else np.zeros(1)
    e = np.maximum(e, e.max() - 70.0)         # digital silence must not dominate the statistics
    e = e - uniform_filter1d(e, int(2.0 / ENV_HOP_S), mode="nearest")
    sd = e.std()
    return e / sd if sd > 0 else e


def coarse_candidates(ref_env: np.ndarray, sig_env: np.ndarray, sig_start: int = 0, n: int = 4, min_overlap: float = 0.5) -> list[tuple[float, float]]:
    """Top (offset_seconds, score) for where the file's t=0 sits on the reference, searching the WHOLE reference.

    `sig_env` may be a piece of the file starting at envelope frame `sig_start`. Score = mean product over the overlap (a
    correlation coefficient); overlaps under `min_overlap` of the shorter signal are ignored (edge flukes)."""
    N, M = len(ref_env), len(sig_env)
    nfft = 1 << (N + M - 1).bit_length()
    cc = np.fft.irfft(np.fft.rfft(ref_env, nfft) * np.conj(np.fft.rfft(sig_env, nfft)), nfft)
    idx, lags = lag_axis(nfft, N, M)
    overlap = np.minimum(N, lags + M) - np.maximum(0, lags)
    ok = overlap >= max(1, min_overlap * min(N, M))
    idx, lags, overlap = idx[ok], lags[ok], overlap[ok]
    score = cc[idx] / overlap
    out: list[tuple[float, float]] = []
    work = score.copy()
    guard = int(3.0 / ENV_HOP_S)
    for _ in range(n):
        if not len(work) or not np.isfinite(work.max()) or work.max() <= -1e8:
            break
        j = int(np.argmax(work))
        out.append((float((lags[j] - sig_start) * ENV_HOP_S), float(score[j])))
        work[np.abs(lags - lags[j]) <= guard] = -1e9
    return out


def _candidates(ref_env: np.ndarray, oth_env: np.ndarray) -> list[float]:
    """Candidate offsets, best first, from the whole file and (for long takes) a piece at each end of its first half."""
    M = len(oth_env)
    seg = int(COARSE_SEG_S / ENV_HOP_S)
    found = coarse_candidates(ref_env, oth_env)
    if M > 2 * seg:
        for s0 in (0, (M - seg) // 2):
            found += coarse_candidates(ref_env, oth_env[s0:s0 + seg], sig_start=s0)
    found.sort(key=lambda c: -c[1])
    out: list[float] = []
    for off, _ in found:
        if all(abs(off - o) > 2.0 for o in out):
            out.append(off)
    return out[:6]


# ---- refine + fit -------------------------------------------------------------------------------------------------

def _offset_from_lag(lag_s: float, ref_start_s: float, other_start_s: float) -> float:
    """For windows starting at ref_start_s / other_start_s, a GCC lag of L means the file's t=0
    sits at  L + ref_start_s - other_start_s  on the reference timeline."""
    return lag_s + ref_start_s - other_start_s


def fit_line(t_file: np.ndarray, offset: np.ndarray) -> tuple[float, float]:
    """Least squares offset = a + b * t_file -> (a seconds, b as a fraction). One point = a constant offset."""
    if len(t_file) >= 2 and np.ptp(t_file) > 1.0:
        b, a = np.polyfit(t_file, offset, 1)
        return float(a), float(b)
    return float(np.mean(offset)), 0.0


def _measure(ref_wav: Path, oth_wav: Path, ref_dur: float, oth_dur: float, a0: float, points: int) -> dict:
    """Measure `points` offsets across the overlap implied by a0, predicting each from the fit so far."""
    lo, hi = max(0.0, a0), min(ref_dur, a0 + oth_dur)
    if hi - lo > 40:
        lo, hi = lo + 2.0, hi - 2.0
    span = hi - lo
    w = float(min(WINDOW_S, max(MIN_WINDOW_S, span / (points + 1)), span, oth_dur))
    if span <= 0 or w < 1.0:
        return {"points": [], "a": a0, "b": 0.0, "window_s": 0.0}
    centers = [lo + w / 2 + (span - w) * (i / (points - 1) if points > 1 else 0.5) for i in range(points)]
    meas: list[dict] = []
    a, b = a0, 0.0
    for e in centers:
        t_cam = (e - a) / (1.0 + b)
        t_cam = min(max(t_cam, w / 2), oth_dur - w / 2)
        oth_t = t_cam - w / 2
        ref_t = e - w / 2
        pred = e - t_cam
        if ref_t < 0 or ref_t + w > ref_dur:
            continue
        lag, psr = gcc_phat(_load(ref_wav, ref_t, w), _load(oth_wav, oth_t, w), max_lag=int(SEARCH_S * SR))
        off = _offset_from_lag(lag, ref_t, oth_t)
        if psr < MIN_PSR and w > 12.0:
            # A fast clock smears a long window (1000 ppm = 60 ms over 60 s) and PHAT decorrelates; a short one survives.
            w2 = w / 3
            ref_t2, oth_t2 = e - w2 / 2, t_cam - w2 / 2
            lag2, psr2 = gcc_phat(_load(ref_wav, ref_t2, w2), _load(oth_wav, oth_t2, w2), max_lag=int(SEARCH_S * SR))
            if psr2 > psr:
                lag, psr, off = lag2, psr2, _offset_from_lag(lag2, ref_t2, oth_t2)
        meas.append({"t": round(e, 3), "t_file": round(t_cam, 3), "offset": round(off, 5), "psr": round(psr, 1), "predicted": round(pred, 5)})
        good = [m for m in meas if m["psr"] >= MIN_PSR]
        if good:
            a, b = fit_line(np.array([m["t_file"] for m in good]), np.array([m["offset"] for m in good]))
    return {"points": meas, "a": a, "b": b, "window_s": w}


def sync_pair(ref_wav: Path, other_wav: Path, points: int = POINTS, ref_env: np.ndarray | None = None) -> dict:
    """Measure one file against the reference. Returns start_offset, drift_ppm, psr (= psr_min), residual_ms, points[]
    (plus legacy drift_ms / drift_ms_per_hour). psr below MIN_PSR means "no shared audio found": do not trust start_offset."""
    ref_dur, oth_dur = sf.info(str(ref_wav)).duration, sf.info(str(other_wav)).duration
    ref_env = envelope(ref_wav) if ref_env is None else ref_env
    cands = _candidates(ref_env, envelope(other_wav))
    best: dict | None = None
    for a0 in cands:
        m = _measure(ref_wav, other_wav, ref_dur, oth_dur, a0, max(1, points))
        good = [p for p in m["points"] if p["psr"] >= MIN_PSR]
        m["coarse"] = a0
        m["n_good"] = len(good)
        if best is None or (len(good), max([p["psr"] for p in m["points"]] or [0])) > (best["n_good"], max([p["psr"] for p in best["points"]] or [0])):
            best = m
        if len(good) >= min(2, max(1, points)) and len(good) >= (len(m["points"]) + 1) // 2:
            best = m
            break
    if best is None or not best["points"]:
        a0 = cands[0] if cands else 0.0
        return {"start_offset": round(a0, 4), "drift_ppm": 0.0, "psr": 0.0, "psr_min": 0.0, "residual_ms": None, "points": [],
                "drift_ms": None, "drift_ms_per_hour": None, "method": "envelope+gcc-phat", "coarse_offset": round(a0, 4)}
    good = [p for p in best["points"] if p["psr"] >= MIN_PSR]
    if not good:
        top = max(p["psr"] for p in best["points"])
        return {"start_offset": round(best["coarse"], 4), "drift_ppm": 0.0, "psr": top, "psr_min": top, "residual_ms": None,
                "points": best["points"], "drift_ms": None, "drift_ms_per_hour": None, "method": "envelope+gcc-phat",
                "coarse_offset": round(best["coarse"], 4)}
    a, b = best["a"], best["b"]
    tf = np.array([p["t_file"] for p in best["points"]])
    res = np.array([p["offset"] for p in best["points"]]) - (a + b * tf)
    for p, r in zip(best["points"], res):
        p["residual_ms"] = round(float(r) * 1000, 2)
    used = np.array([p["psr"] >= MIN_PSR for p in best["points"]])
    overlap = min(ref_dur, oth_dur + a) - max(0.0, a)
    psr_min = min(p["psr"] for p in good)
    linear_ms = round(float(np.abs(res[used]).max()) * 1000, 2)
    out = {"start_offset": round(a, 4), "drift_ppm": round(b * 1e6, 2), "psr": psr_min, "psr_min": psr_min,
           "residual_ms": linear_ms, "points": best["points"],
           "drift_ms_per_hour": round(b * 3.6e6, 1), "drift_ms": round(b * overlap * 1000.0, 1),
           "n_points": len(best["points"]), "n_good": len(good), "window_s": round(best["window_s"], 1),
           "coarse_offset": round(best["coarse"], 4), "method": "envelope+gcc-phat"}
    if linear_ms > MAP_RESIDUAL_MS and len(good) >= 3:
        knots = sorted([p["t_file"], p["offset"]] for p in good)
        out["sync_map"] = [[round(t, 3), round(o, 5)] for t, o in knots]
        out["linear_residual_ms"] = linear_ms
        src = {"start_offset": a, "drift_ppm": b * 1e6, "sync_map": out["sync_map"]}
        for p in best["points"]:       # distance of every measured point from the model that will actually be used
            p["map_residual_ms"] = round((p["offset"] - (editdoc.episode_time(src, p["t_file"]) - p["t_file"])) * 1000, 2)
        out["residual_ms"] = round(max(abs(p["map_residual_ms"]) for p in best["points"] if p["psr"] >= MIN_PSR), 2)
        out["model"] = "piecewise"
    else:
        out["model"] = "linear"
    return out


# ---- episode level ------------------------------------------------------------------------------------------------

def pick_reference(syncable: list[dict], media_files: list[dict], reference: str | None) -> dict:
    """--reference wins. Otherwise a recorder (audio-only file) longer than every camera, else the longest file: episode
    time is the recorder's timeline when one exists, so a camera never defines it if a master is available."""
    if reference:
        hit = next((m for m in syncable if reference in (m["file"], Path(m["file"]).name)), None)
        if not hit:
            raise SystemExit(f"--reference {reference!r} not found among {[m['file'] for m in syncable]}")
        return hit
    cams = [m for m in syncable if m["kind"] == "video"]
    recs = [m for m in syncable if m["kind"] == "audio"]
    longest_cam = max((m["duration"] for m in cams), default=0.0)
    masters = [m for m in recs if m["duration"] > longest_cam]
    return max(masters or syncable, key=lambda m: m["duration"])


def coverage_warnings(name: str, a: float, dur: float, ref_dur: float) -> list[str]:
    out = []
    if dur > ref_dur + 1.0:
        out.append(f"{name}: {dur:.0f}s is longer than the reference ({ref_dur:.0f}s), which cannot contain it. "
                   "Use the longer file as --reference, or this file is a different recording.")
    elif a < -1.0 or a + dur > ref_dur + 1.0:
        out.append(f"{name}: spans {a:.1f}..{a + dur:.1f}s but the reference covers 0..{ref_dur:.1f}s, so part of this file "
                   "lies outside the reference timeline and will be unusable.")
    return out


def sync_episode(ep: Episode, reference: str | None = None, points: int = POINTS) -> dict:
    media = read_json(ep.artifact("media.json"))["files"]
    syncable = [m for m in media if m["audio"]]
    if len(syncable) < 2:
        raise SystemExit("Need at least two files with audio to sync.")
    ref_item = pick_reference(syncable, media, reference)
    ref_wav = analysis_wav_path(ep, ref_item["file"])
    ref_env = envelope(ref_wav)
    out = {"reference": ref_item["file"], "reference_duration": ref_item["duration"], "points_requested": points,
           "files": {ref_item["file"]: {"start_offset": 0.0, "drift_ppm": 0.0, "psr": None, "psr_min": None, "residual_ms": 0.0,
                                         "drift_ms": 0.0, "points": []}},
           "warnings": []}
    tp = ep.artifact("transcript.json")
    if tp.exists() and read_json(tp).get("source") != ref_item["file"]:
        out["warnings"].append(
            f"transcript.json was made from {read_json(tp).get('source')} but the sync reference is {ref_item['file']}: transcript "
            f"times are NOT episode times. Re-run `ave transcribe {ep.root} --source {ref_item['file']}` (or sync with --reference).")
    for m in syncable:
        if m["file"] == ref_item["file"]:
            continue
        r = sync_pair(ref_wav, analysis_wav_path(ep, m["file"]), points=points, ref_env=ref_env)
        r["duration"] = m["duration"]
        out["files"][m["file"]] = r
        if r["psr"] < MIN_PSR:
            out["warnings"].append(
                f"{m['file']}: no sync found (best PSR {r['psr']}, need {MIN_PSR:g}). It shares no audio with {ref_item['file']}, or was "
                "recorded in another session. Try a clap-based manual offset or a different --reference.")
            continue
        weak = [p for p in r["points"] if p["psr"] < MIN_PSR]
        if weak:
            out["warnings"].append(f"{m['file']}: {len(weak)} of {len(r['points'])} sync points were weak (PSR < {MIN_PSR:g}) and ignored.")
        if r["n_good"] < 3:
            out["warnings"].append(f"{m['file']}: only {r['n_good']} usable sync point(s), so drift is poorly constrained.")
        if r.get("sync_map"):
            rates = [(o1 - o0) / (t1 - t0) * 1e6 for (t0, o0), (t1, o1) in zip(r["sync_map"], r["sync_map"][1:]) if t1 - t0 > 1.0]
            if any(abs(x - r["drift_ppm"]) > MAP_SLOPE_WARN_PPM for x in rates):
                out["warnings"].append(
                    f"{m['file']}: the piecewise sync map has a segment whose clock rate differs from the fitted line by more than "
                    f"{MAP_SLOPE_WARN_PPM:g} ppm: a sync point probably mis-locked. Re-run with --points 9 and inspect `points`.")
        elif r["residual_ms"] is not None and r["residual_ms"] > RESIDUAL_WARN_MS:
            out["warnings"].append(
                f"{m['file']}: linear drift fit leaves {r['residual_ms']} ms residual: the clock is not drifting linearly "
                "(or a point mis-locked). Inspect `points` in sync.json and spot-check lips in the take.")
        if abs(r["drift_ppm"]) > DRIFT_WARN_PPM:
            out["warnings"].append(f"{m['file']}: drift {r['drift_ppm']} ppm is implausibly large for a camera clock; distrust it.")
        out["warnings"] += coverage_warnings(m["file"], r["start_offset"], m["duration"], ref_item["duration"])
    write_json(ep.artifact("sync.json"), out)
    ep.mark_stage("sync", reference=ref_item["file"], files=len(out["files"]))
    return out
