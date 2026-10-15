"""Extract stills so an agent can LOOK at the footage (who is on this camera? is the shot framed well?)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import config
from .episode import Episode


def extract(ep: Episode, rel_file: str, time_s: float, width: int = 960) -> Path:
    ffmpeg = config.binary("ffmpeg")
    if not ffmpeg:
        raise SystemExit("ffmpeg not found on PATH")
    src = ep.root / rel_file
    if not src.exists():
        raise SystemExit(f"{src} does not exist")
    out_dir = ep.work / "stills"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"{Path(rel_file).stem}_{time_s:09.3f}.jpg"
    subprocess.run([str(ffmpeg), "-nostdin", "-v", "error", "-y", "-ss", f"{time_s:.3f}", "-i", str(src),
                    "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "3", str(out)], check=True)
    if not out.exists():
        raise SystemExit(f"no frame at {time_s}s in {rel_file} (past the end?)")
    return out
