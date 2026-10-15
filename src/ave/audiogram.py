"""Audiogram fallback short (`ave shorts build --layout audiogram`): for clips with no usable picture (audio-only recordings, a camera
that did not cover the moment). A dark 1080x1920 card, an animated waveform in the middle and the same karaoke captions as every
other short. Audio, loudness, caption burn, limits and sidecars are the normal short pipeline: only the PICTURE differs, and it is
rendered once into work/shorts/ and used as the doc's single video source (same trick as ave.stack).
"""

from __future__ import annotations

import copy
from pathlib import Path

from . import compile as comp
from . import ffutil
from .episode import Episode

DEFAULTS = {"bg": "#14110F", "wave": "#FFD23F", "wave_w": 920, "wave_h": 420, "wave_y": 0.30}


def settings(cfg: dict) -> dict:
    return {**DEFAULTS, **(cfg.get("audiogram") or {})}


def _hex(c: str) -> str:
    return "0x" + c.lstrip("#")


def render_picture(ep: Episode, doc: dict, out: Path, cfg: dict) -> Path:
    """Waveform video (no audio) of exactly round(duration*fps) frames at the doc's canvas size, from the doc's own dialogue."""
    st = settings(cfg)
    W, H = doc["canvas"]["w"], doc["canvas"]["h"]
    fps = ffutil.fps_fraction(doc["fps"])
    n = ffutil.frame_of(doc["duration"], fps)
    out.parent.mkdir(parents=True, exist_ok=True)
    wav = out.with_suffix(".wav")
    partial = out.with_name(out.stem + ".partial.mp4")
    try:
        comp.compile_audio(ep, doc, wav)
        y = int(st["wave_y"] * H)
        fc = (f"[0:a]aformat=channel_layouts=mono,showwaves=s={st['wave_w']}x{st['wave_h']}:mode=cline:rate={fps.numerator}/{fps.denominator}"
              f":colors=white:scale=sqrt:draw=full,format=gray[m];"      # white-on-black mask: showwaves' own colour blends dim, so colour it by alphamerge
              f"color=c={_hex(st['wave'])}:s={st['wave_w']}x{st['wave_h']}:r={fps.numerator}/{fps.denominator},format=rgba[c];"
              f"[c][m]alphamerge[w];"
              f"color=c={_hex(st['bg'])}:s={W}x{H}:r={fps.numerator}/{fps.denominator},format=rgba[bg];"
              f"[bg][w]overlay=(W-w)/2:{y},fps={fps.numerator}/{fps.denominator},tpad=stop_mode=clone:stop_duration=2,format=yuv420p[v]")
        ffutil.run(["-y", "-i", str(wav), "-filter_complex", fc, "-map", "[v]", "-an", "-frames:v", str(n), "-c:v", "libx264", "-preset", "fast",
                    "-crf", "16", "-pix_fmt", "yuv420p", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
                    "-color_range", "tv", "-movflags", "+faststart", str(partial)])
        partial.replace(out)
    finally:
        wav.unlink(missing_ok=True)
        partial.unlink(missing_ok=True)
    got = ffutil.count_video_packets(out)
    if got != n:
        raise RuntimeError(f"audiogram picture has {got} frames, expected {n}")
    return out


def attach(ep: Episode, doc: dict, slug: str, version: int, cfg: dict) -> dict:
    """Make `doc` an audiogram: render the picture and replace the video track by it. Returns the picture record."""
    pic = ep.work / "shorts" / f"audiogram.{slug}.v{version}.mp4"
    render_picture(ep, doc, pic, cfg)
    doc["sources"]["WAVE"] = {"kind": "video", "file": str(pic.relative_to(ep.root)), "start_offset": 0.0, "drift_ppm": 0.0}
    doc["tracks"]["video"] = [{"id": "wave", "src": "WAVE", "o0": 0.0, "o1": doc["duration"], "e0": 0.0, "crop": None, "zoom": 1.0,
                               "reason": "audiogram: waveform card (no usable picture)"}]
    return {"picture": str(pic.relative_to(ep.root))}
