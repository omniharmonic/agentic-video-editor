"""Synthetic LONG TAKE with a drifting camera clock and a visible + audible marker near its end (field note N3).

Physics: the camera starts at master time A_TRUE and its clock runs slow by B_TRUE, so camera time t_c sits at master time
e = A_TRUE + (1 + B_TRUE) * t_c. The marker (a white flash and a 1 kHz beep) is recorded at camera time TC_MARK, which is
on the camera's frame grid, so the frame it lands on is unambiguous. 80 ppm over 690 s is 55 ms: over one frame at 30 fps,
which is exactly why a test that only checks the measured drift would pass while the product was wrong.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import butter, lfilter, resample_poly

SR = 8000
FPS = 30
MASTER_S = 700.0
A_TRUE = 2.0
B_TRUE = 80e-6
TC_MARK = 20637 / FPS                                  # 687.9 s on the camera's own clock (a frame boundary)
T_MARK = A_TRUE + (1 + B_TRUE) * TC_MARK               # where the marker sits on the master timeline
BEEP_S = 0.4


def speechlike(seconds: float, seed: int = 1, sr: int = SR) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = int(seconds * sr)
    out = np.zeros(n, dtype=np.float32)
    pos = 0
    while pos < n:
        dur = int(rng.uniform(0.08, 0.5) * sr)
        gap = int(rng.uniform(0.05, 0.6) * sr)
        burst = rng.standard_normal(min(dur, n - pos)).astype(np.float32)
        b, a = butter(2, [rng.uniform(150, 400), rng.uniform(1200, 3500)], btype="band", fs=sr)
        out[pos:pos + len(burst)] = lfilter(b, a, burst) * rng.uniform(0.3, 1.0)
        pos += dur + gap
    return out / np.abs(out).max() * 0.5


def build(root: Path) -> dict:
    """Write master.wav (the reference), cam.mov (drifting camera: picture + audio) and cam.wav (its analysis audio)."""
    root.mkdir(parents=True, exist_ok=True)
    master = speechlike(MASTER_S, seed=21)
    lo, hi = int((T_MARK - 1.5) * SR), int((T_MARK + 2.5) * SR)
    master[lo:hi] = 0.0                                              # quiet around the marker so its onset is unambiguous
    t = np.arange(int(BEEP_S * SR)) / SR
    k = int(round(T_MARK * SR))
    master[k:k + len(t)] = (0.5 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
    sf.write(root / "master.wav", master, SR, subtype="PCM_16")

    # the camera heard the same room through its own mic, started A_TRUE late and keeps slow time
    rng = np.random.default_rng(5)
    b, a = butter(2, 2500, btype="low", fs=SR)
    mic = (lfilter(b, a, master) * 0.7 + rng.standard_normal(len(master)).astype(np.float32) * 0.01).astype(np.float32)
    cam_audio = resample_poly(mic[int(A_TRUE * SR):], 12500, 12501).astype(np.float32)        # 12500/12501 = 80 ppm slow
    sf.write(root / "cam.wav", cam_audio, SR, subtype="PCM_16")
    dur = len(cam_audio) / SR
    flash = f"drawbox=x=0:y=0:w=64:h=36:color=white:t=fill:enable='between(t,{TC_MARK},{TC_MARK + 0.5})'"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=black:s=64x36:r={FPS}:d={dur}",
                    "-i", str(root / "cam.wav"), "-vf", flash, "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-c:a", "pcm_s16le", "-shortest", str(root / "cam.mov")], check=True)
    return {"master": root / "master.wav", "cam_wav": root / "cam.wav", "cam": root / "cam.mov", "cam_duration": dur}
