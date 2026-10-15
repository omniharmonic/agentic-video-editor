"""Synthetic two-camera episode with KNOWN ground truth, so media stages are testable (and demoable) without private footage.

Shipped in the package so `ave demo` and the test suite share one generator (tests/fixtures.py re-exports it).

Episode time layout (seconds), 30 s total. Camera A is BLUE and frames speaker S1; camera B is RED, frames S2, and
started 1.0 s after A (start_offset 1.0). Audio: S1 = 220 Hz bursts, S2 = 330 Hz bursts, filler "um" = 150 Hz
burst, silence = low noise floor. Both cameras carry the same audio. A real recorder file M (audio only,
start_offset 0.5) is also produced.

  S1 0.5-6.0 | dead air 6.0-8.0 | S2 8.0-12.0 | S1 "yeah" 12.2-12.6 | S2 12.6-16.0 (um 13.0-13.4)
  | pause 16.0-17.0 | S1 17.0-25.0 | S2 25.5-29.5
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


DURATION = 30.0
FPS = 30
SIZE = "640x360"
B_OFFSET = 1.0
M_OFFSET = 0.5

# (speaker, start, end, text) in episode time. Word timings are spread evenly inside each span.
UTTERANCES = [
    ("S1", 0.5, 6.0, "welcome to the show today we are talking about building things that last"),
    ("S2", 8.0, 12.0, "thanks for having me it is great to be here with you"),
    ("S1", 12.2, 12.6, "yeah"),
    ("S2", 12.6, 13.0, "and the thing is"),
    ("S2", 13.0, 13.4, "um"),
    ("S2", 13.4, 16.0, "most people never finish what they start"),
    ("S1", 17.0, 25.0, "that is exactly right and it is why we need systems that carry the work forward"),
    ("S2", 25.5, 29.5, "absolutely and that is what this whole conversation is about"),
]
FILLERS = {"um": (13.0, 13.4)}
DEAD_AIR = [(6.0, 8.0), (16.0, 17.0)]


def _run(*cmd: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *cmd], check=True)


def _audio_expr() -> str:
    """One ffmpeg lavfi expression for the whole 30 s: bursts at speaker-specific tones over a quiet noise floor."""
    parts = []
    for spk, a, b, text in UTTERANCES:
        freq = 150 if text == "um" else (220 if spk == "S1" else 330)
        parts.append(f"0.5*sin(2*PI*{freq}*t)*between(t,{a},{b})")
    return "+".join(parts)


def build_media(raw: Path, audio_dir: Path | None = None) -> None:
    """Cameras go in `raw`; the recorder file goes in `audio_dir` (default: the same folder)."""
    raw.mkdir(parents=True, exist_ok=True)
    audio_dir = audio_dir or raw
    audio_dir.mkdir(parents=True, exist_ok=True)
    wav = raw / "_base.wav"
    _run("-f", "lavfi", "-i", f"aevalsrc='{_audio_expr()}':s=48000:d={DURATION}", "-ac", "1", str(wav))
    noise = ["-f", "lavfi", "-i", f"anoisesrc=color=pink:amplitude=0.004:sample_rate=48000:d={DURATION}"]
    # Camera A: blue, full length. Camera B: red, starts B_OFFSET late. Both carry the same mixed audio.
    for name, color, ss in (("camA", "0x2040ff", 0.0), ("camB", "0xff2020", B_OFFSET)):
        _run("-f", "lavfi", "-i", f"color=c={color}:s={SIZE}:r={FPS}:d={DURATION}", "-i", str(wav), *noise,
             "-filter_complex", "[1:a][2:a]amix=inputs=2:duration=first:normalize=0[a]", "-map", "0:v", "-map", "[a]",
             "-ss", str(ss), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
             "-shortest", str(raw / f"{name}.mp4"))
    _run("-ss", str(M_OFFSET), "-i", str(wav), "-c:a", "pcm_s16le", str(audio_dir / "recorder.wav"))
    wav.unlink()


def words() -> list[dict]:
    out = []
    for spk, a, b, text in UTTERANCES:
        toks = text.split()
        step = (b - a) / len(toks)
        for i, t in enumerate(toks):
            out.append({"w": t, "start": round(a + i * step, 3), "end": round(a + (i + 1) * step - 0.05, 3), "conf": 0.95, "speaker": spk})
    return out


def transcript(cam_a: str = "raw/camA.mp4") -> dict:
    ws = words()
    utts = []
    for spk, a, b, text in UTTERANCES:
        idx = [i for i, w in enumerate(ws) if w["speaker"] == spk and a - 1e-6 <= w["start"] <= b]
        utts.append({"speaker": spk, "start": a, "end": b, "first_word": idx[0], "last_word": idx[-1], "text": text})
    return {"source": cam_a, "engine": {"asr": "synthetic", "diarization": "synthetic"}, "duration": DURATION,
            "speakers": {"S1": {"talk_seconds": 14.0, "name": "Host"}, "S2": {"talk_seconds": 12.0, "name": "Guest"}},
            "words": ws, "utterances": utts}
