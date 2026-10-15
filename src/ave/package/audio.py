"""Audio-only podcast file from the final video: loudness-normalised to the podcast target, chapters written as ID3 CHAP frames."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from .. import ffutil
from . import chapters as C

EXT = {"mp3": ".mp3", "aac": ".m4a"}


def estimate_bytes(duration_s: float, fmt: str, policy: dict) -> int:
    kbps = policy["audio"]["mp3_kbps" if fmt == "mp3" else "aac_kbps"]
    return int(duration_s * kbps * 1000 / 8 * 1.02) + 20000


def encoder_available(fmt: str) -> bool:
    enc = "libmp3lame" if fmt == "mp3" else "aac"
    try:
        out = subprocess.run([ffutil._bin("ffmpeg"), "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    except OSError:
        return False
    return enc in out


def build(video: Path, out: Path, fmt: str, chapters: list[dict], tags: dict[str, str], policy: dict) -> dict:
    """Encode `video`'s audio to `out` (mp3 or aac/m4a). Two-pass loudnorm to the target, then MEASURE the encoded file:
    the result records the measurement and whether it is within tolerance, so the checklist reports a fact, not a hope."""
    a = policy["audio"]
    target, tp, tol = a["target_lufs"], a["true_peak_db"], a["tolerance_lu"]
    duration = ffutil.probe(video)["duration"]
    m = ffutil.loudnorm_measure(video, target, tp)
    warnings: list[str] = []
    if m is None:
        af = None
        warnings.append("the audio is digital silence: nothing to normalise")
    else:
        af = (f"loudnorm=I={target}:TP={tp}:LRA=11:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
              f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")
    cmd = [ffutil._bin("ffmpeg"), "-nostdin", "-hide_banner", "-v", "error", "-y", "-i", str(video)]
    with tempfile.TemporaryDirectory(prefix="ave-package-") as td:
        meta = Path(td) / "chapters.ffmeta"
        meta.write_text(C.ffmetadata(chapters, duration, tags), encoding="utf-8")
        cmd += ["-i", str(meta), "-map", "0:a:0", "-map_metadata", "1", "-map_chapters", "1", "-vn"]
        if af:
            cmd += ["-af", af]
        cmd += ["-ar", str(a["sample_rate"]), "-ac", str(a["channels"])]
        if fmt == "mp3":
            cmd += ["-c:a", "libmp3lame", "-b:a", f"{a['mp3_kbps']}k", "-id3v2_version", "3", "-write_id3v1", "1"]
        else:
            cmd += ["-c:a", "aac", "-b:a", f"{a['aac_kbps']}k", "-movflags", "+faststart"]
        tmp = out.with_name(out.name + ".part" + out.suffix)
        cmd += [str(tmp)]
        out.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            tmp.unlink(missing_ok=True)
            raise ffutil.FFmpegError(f"podcast audio encode failed: {proc.stderr[-600:]}")
        tmp.replace(out)
    got = ffutil.measure_loudness(out)
    ok = abs(got["lufs"] - target) <= tol and got["true_peak_db"] <= tp + 0.5
    if not ok:
        warnings.append(f"loudness is {got['lufs']:.1f} LUFS / true peak {got['true_peak_db']:.1f} dB, target {target} LUFS (+-{tol}) and {tp} dBTP")
    info = ffutil.probe(out)
    return {"file": str(out), "format": fmt, "duration": round(info["duration"], 3), "bytes": out.stat().st_size,
            "loudness": {"lufs": round(got["lufs"], 2), "true_peak_db": round(got["true_peak_db"], 2), "target_lufs": target,
                         "tolerance_lu": tol, "ok": ok},
            "chapters": len(chapters), "warnings": warnings}
