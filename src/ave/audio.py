"""Extract analysis audio (16 kHz mono WAV) from every source.

Analysis audio is a disposable derivative used for sync, ASR and diarization.
Originals in raw/ are never modified or transcoded.

A stereo file may have ONE live channel (field note N4): averaging it to mono costs 6 dB and halves ASR/sync energy, so
when media.json carries a `channel_map` with a dead channel the analysis audio takes the LIVE channel, per time range.

A speaker-per-channel master (two mics, one host each; `use: "sum"`) is the opposite case: folding to the "live" channel
would gate each mic on and off and drop interjections, so the analysis audio is L+R (NOT the average, which loses 6 dB),
passed through `soft_limit` so the sum can never clip.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf

from . import config
from .episode import Episode, read_json

SAMPLE_RATE = 16000
LIMIT_KNEE = 0.8      # below this the sum is untouched; above it peaks are compressed smoothly toward (never past) 1.0


def soft_limit(x: np.ndarray, knee: float = LIMIT_KNEE) -> np.ndarray:
    """Transparent below `knee`, tanh-compressed above it, bounded by 1.0 (continuous, monotonic): the peak guard for L+R."""
    x = np.asarray(x, np.float32)
    a = np.abs(x)
    over = a > knee
    if not over.any():
        return x
    out = x.copy()
    room = 1.0 - knee
    out[over] = np.sign(x[over]) * (knee + room * np.tanh((a[over] - knee) / room))
    return out


def analysis_wav_path(ep: Episode, rel_file: str) -> Path:
    # Flatten the relative path so cam/A.mp4 and cam/B.mp4 can't collide.
    safe = rel_file.replace("/", "__").rsplit(".", 1)[0]
    return ep.work / "audio" / f"{safe}.16k.wav"


def _live(item: dict) -> list[dict] | None:
    """The channel_map when it says some range has a dead channel (anything but all-mix), else None."""
    cm = item.get("channel_map")
    return cm if cm and any(m["use"] != "mix" for m in cm) else None


def _sidecar(wav: Path) -> Path:
    return wav.with_name(wav.name + ".channels.json")


def _have_map(wav: Path):
    try:
        return json.loads(_sidecar(wav).read_text())
    except (OSError, ValueError):
        return None


def _extract_live(ffmpeg: Path, src: Path, out: Path, cm: list[dict]) -> None:
    """Mono analysis audio taking left / right / sum / mix per time range of the source's clock (streamed in 60 s blocks)."""
    block = 60 * SAMPLE_RATE
    proc = subprocess.Popen([str(ffmpeg), "-nostdin", "-v", "error", "-i", str(src), "-vn", "-map", "0:a:0",
                             "-af", "pan=stereo|c0=c0|c1=c1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "-"], stdout=subprocess.PIPE)
    pos = 0
    try:
        with sf.SoundFile(str(out), "w", samplerate=SAMPLE_RATE, channels=1, subtype="PCM_16", format="WAV") as w:
            while True:
                raw = proc.stdout.read(block * 8)
                if not raw:
                    break
                x = np.frombuffer(raw[: len(raw) // 8 * 8], np.float32).reshape(-1, 2)
                y = x.mean(axis=1)
                for m in cm:
                    a, b = int(round(m["start"] * SAMPLE_RATE)) - pos, int(round(m["end"] * SAMPLE_RATE)) - pos
                    a, b = max(a, 0), min(b, len(x))
                    if b > a and m["use"] in ("left", "right"):
                        y[a:b] = x[a:b, 0 if m["use"] == "left" else 1]
                    elif b > a and m["use"] == "sum":
                        y[a:b] = soft_limit(x[a:b, 0] + x[a:b, 1])
                w.write(y)
                pos += len(x)
    finally:
        proc.stdout.close()
        rc = proc.wait()
    if rc:
        raise subprocess.CalledProcessError(rc, "ffmpeg")


def extract_all(ep: Episode, force: bool = False) -> list[Path]:
    ffmpeg = config.binary("ffmpeg")
    if not ffmpeg:
        raise SystemExit("ffmpeg not found on PATH")
    media = read_json(ep.artifact("media.json"))
    outputs: list[Path] = []
    for item in media["files"]:
        if not item["audio"]:
            continue
        out = analysis_wav_path(ep, item["file"])
        out.parent.mkdir(parents=True, exist_ok=True)
        live = _live(item)
        # a wav made before the channel map existed (or from another map) is stale: it was averaged
        if out.exists() and not force and _have_map(out) == live:
            outputs.append(out)
            continue
        tmp = out.with_suffix(".partial.wav")
        if live:
            _extract_live(ffmpeg, ep.root / item["file"], tmp, live)
        else:
            subprocess.run(
                [str(ffmpeg), "-nostdin", "-v", "error", "-y", "-i", str(ep.root / item["file"]),
                 "-vn", "-map", "0:a:0", "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(tmp)],
                check=True,
            )
        tmp.replace(out)
        if live:
            _sidecar(out).write_text(json.dumps(live))
        elif _sidecar(out).exists():
            _sidecar(out).unlink()
        outputs.append(out)
    ep.mark_stage("audio", files=len(outputs))
    return outputs
