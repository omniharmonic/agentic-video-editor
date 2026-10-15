"""Thin, strict wrappers around ffmpeg/ffprobe used by the compiler and anything that must VERIFY media.

WHY a module of its own: every render is checked with real measurements (ffprobe stream facts, EBU R128 loudness), and
those checks must not depend on the code that produced the file.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
from fractions import Fraction
from pathlib import Path

SAMPLE_RATE = 48000
SILENCE_DB = -120.0   # what we report instead of -inf for digital silence (JSON cannot carry -inf)

# NTSC-style rates that people write as decimals; they must become exact rationals or frame counts drift.
_NTSC = {23.976: Fraction(24000, 1001), 29.97: Fraction(30000, 1001), 47.952: Fraction(48000, 1001),
         59.94: Fraction(60000, 1001), 119.88: Fraction(120000, 1001)}


class FFmpegError(RuntimeError):
    """An ffmpeg/ffprobe invocation failed. The message carries the command tail and stderr."""


def _bin(name: str) -> str:
    from . import config
    found = config.binary(name)
    if not found:
        raise FFmpegError(f"{name} not found on PATH (install ffmpeg)")
    return str(found)


def run(args: list[str], *, check: bool = True, input: bytes | None = None, text: bool = True,
        cwd: str | Path | None = None) -> subprocess.CompletedProcess:
    """Run ffmpeg with quiet, non-interactive defaults. `args` excludes the binary name."""
    cmd = [_bin("ffmpeg"), "-nostdin", "-hide_banner", "-v", "error", *args]
    proc = subprocess.run(cmd, capture_output=True, input=input, text=text and input is None, cwd=cwd)
    if check and proc.returncode != 0:
        err = proc.stderr if isinstance(proc.stderr, str) else proc.stderr.decode("utf-8", "replace")
        raise FFmpegError(f"ffmpeg failed ({proc.returncode}): {' '.join(cmd[-14:])}\n{err[-2500:]}")
    return proc


def fps_fraction(fps: float) -> Fraction:
    """Exact frame rate for a float. 29.97 -> 30000/1001, 30.0 -> 30. Never returns an approximation of an NTSC rate."""
    for k, v in _NTSC.items():
        if abs(fps - k) < 0.005:
            return v
    return Fraction(fps).limit_denominator(1001) if abs(fps - round(fps)) > 1e-9 else Fraction(round(fps))


def frame_of(t: float, fps: Fraction) -> int:
    """Nearest frame boundary for output time t. Using one rounding rule everywhere means adjacent segments never gap or overlap."""
    return int(math.floor(t * fps + 0.5 + 1e-9))


def probe(path: str | Path) -> dict:
    """Facts about a media file: duration, fps, w, h, has_audio, has_video, pix_fmt, codec, plus audio details."""
    p = Path(path)
    proc = subprocess.run([_bin("ffprobe"), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(p)],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise FFmpegError(f"ffprobe failed for {p}: {proc.stderr.strip()[-500:]}")
    info = json.loads(proc.stdout)
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic") != 1), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = info.get("format", {})

    def fnum(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    r_fps = None
    if v:
        try:
            rf = Fraction(v.get("r_frame_rate") or "0/1")
            r_fps = float(rf) if rf > 0 else None
        except (ValueError, ZeroDivisionError):
            r_fps = None
    fps = None
    if v:
        for key in ("avg_frame_rate", "r_frame_rate"):
            try:
                f = Fraction(v.get(key) or "0/1")
            except (ValueError, ZeroDivisionError):
                continue
            if f > 0:
                fps = float(f)
                break
    return {
        "path": str(p), "duration": fnum(fmt.get("duration")) or 0.0,
        "has_video": v is not None, "has_audio": a is not None,
        "fps": fps, "r_fps": r_fps, "w": v.get("width") if v else None, "h": v.get("height") if v else None,
        "pix_fmt": v.get("pix_fmt") if v else None, "codec": (v or a or {}).get("codec_name"),
        "video_duration": fnum(v.get("duration")) if v else None,
        "audio_codec": a.get("codec_name") if a else None, "audio_duration": fnum(a.get("duration")) if a else None,
        "sample_rate": int(a["sample_rate"]) if a and a.get("sample_rate") else None,
        "channels": a.get("channels") if a else None,
        "video_packets": None,
    }


def count_video_packets(path: str | Path) -> int:
    """Exact number of video packets (== frames for our encodes) without decoding."""
    proc = subprocess.run([_bin("ffprobe"), "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries",
                           "stream=nb_read_packets", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        return int(proc.stdout.strip().split(",")[0])
    except ValueError as e:
        raise FFmpegError(f"could not count frames in {path}: {proc.stderr.strip()[-300:]}") from e


def _db(token: str) -> float:
    return SILENCE_DB if "inf" in token else float(token)


def measure_loudness(path: str | Path) -> dict:
    """EBU R128 integrated loudness, true peak and loudness range of a file's first audio stream (ffmpeg ebur128).

    Digital silence reports SILENCE_DB (-120) instead of -inf.
    """
    proc = subprocess.run([_bin("ffmpeg"), "-nostdin", "-hide_banner", "-nostats", "-i", str(path), "-vn",
                           "-af", "ebur128=peak=true:framelog=quiet", "-f", "null", "-"], capture_output=True, text=True)
    if proc.returncode != 0:
        raise FFmpegError(f"loudness measurement failed for {path}: {proc.stderr[-500:]}")
    tail = proc.stderr[proc.stderr.rfind("Summary:"):]
    i = re.search(r"\bI:\s+(-?inf|-?[\d.]+) LUFS", tail)
    lra = re.search(r"\bLRA:\s+(-?inf|-?[\d.]+) LU", tail)
    tp = re.search(r"True peak:\s*\n\s*Peak:\s+(-?inf|-?[\d.]+) dBFS", tail)
    if not (i and lra and tp):
        raise FFmpegError(f"could not parse ebur128 summary for {path}:\n{tail[-600:]}")
    return {"lufs": _db(i[1]), "true_peak_db": _db(tp[1]), "lra": float(lra[1]) if "inf" not in lra[1] else 0.0}


def loudnorm_measure(path: str | Path, target: float, tp: float, lra: float = 11.0) -> dict | None:
    """Pass 1 of two-pass loudnorm: measure the file. Returns None for digital silence (nothing to normalize)."""
    proc = subprocess.run([_bin("ffmpeg"), "-nostdin", "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af",
                           f"loudnorm=I={target}:TP={tp}:LRA={lra}:print_format=json", "-f", "null", "-"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise FFmpegError(f"loudnorm measurement failed: {proc.stderr[-800:]}")
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", proc.stderr, re.S)
    if not m:
        raise FFmpegError(f"could not parse loudnorm output:\n{proc.stderr[-800:]}")
    data = json.loads(m.group(0))
    if "inf" in str(data["input_i"]) or float(data["input_i"]) < -70.0:
        return None
    return {k: float(v) for k, v in data.items() if k.startswith(("input_", "target_offset"))}
