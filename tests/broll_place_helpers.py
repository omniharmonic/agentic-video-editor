"""Synthetic fixtures for the B-roll placement tests: a studio, B-roll whose frames and samples are known, and frame-level measurements.
No network, no footage. The B-roll video is twelve 0.4 s colour blocks cycling lime, magenta, yellow, cyan with a 440 Hz tone, so any frame or
sample of the render can be traced to the exact second of the asset it came from."""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

from ave import library
from ave.studio.project import StudioProject

BLOCKS = (("lime", (0, 255, 0)), ("magenta", (255, 0, 255)), ("yellow", (255, 255, 0)), ("cyan", (0, 255, 255)))
STOCK = {"source": "pexels", "asset_id": "991", "url": "https://www.pexels.com/video/991/", "license": "Pexels License",
         "license_url": "https://www.pexels.com/license/", "license_text": "Free to use; credit appreciated.",
         "attribution": {"required": False, "text": "Video by A. Maker on Pexels"}}
CREDIT_REQUIRED = {**STOCK, "asset_id": "992", "url": "https://www.pexels.com/video/992/", "license": "Attribution license",
                   "attribution": {"required": True, "text": "Clay hands by A. Maker on Pexels"}}


def ffm(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *args], check=True)


def make_studio(tmp: Path, name: str = "studio") -> StudioProject:
    sp = StudioProject(tmp / name)
    sp.create(title="Test Studio", tz="UTC")
    return sp


def make_broll_video(path: Path, *, block_s: float = 0.4, blocks: int = 12, fps: int = 25, size=(320, 180), audio: bool = True) -> Path:
    """`blocks` colour blocks of `block_s` seconds each (block k is BLOCKS[k % 4]; 0.4 s is a whole 10 frames at 25 fps, so the block edges
    are exact); with `audio`, a 440 Hz sine of the same length."""
    path.parent.mkdir(parents=True, exist_ok=True)
    w, h = size
    ins, labels = [], []
    for i in range(blocks):
        rgb = BLOCKS[i % len(BLOCKS)][1]
        ins += ["-f", "lavfi", "-i", f"color=c=0x{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}:s={w}x{h}:r={fps}:d={block_s}"]
        labels.append(f"[{i}:v]")
    total = round(block_s * blocks, 3)
    args = [*ins]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={total}"]
    args += ["-filter_complex", "".join(labels) + f"concat=n={blocks}:v=1:a=0,format=yuv420p[v]", "-map", "[v]"]
    if audio:
        args += ["-map", f"{blocks}:a", "-c:a", "aac", "-b:a", "128k"]
    args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "12", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
             "-t", f"{total}", str(path)]
    ffm(*args)
    return path


def make_still(path: Path, size=(800, 600)) -> Path:
    """A still with a left-to-right gradient (so a Ken Burns pan or push is visible in the pixels)."""
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    w, h = size
    arr = np.zeros((h, w, 3), np.uint8)
    arr[:, :, 0] = np.linspace(0, 255, w, dtype=np.uint8)[None, :]
    arr[:, :, 1] = 160
    arr[:, :, 2] = np.linspace(255, 0, h, dtype=np.uint8)[:, None]
    Image.fromarray(arr).save(path)
    return path


def add_stock(sp: StudioProject, path: Path, entry: dict | None = None, *, alt: str = "", by: str = "Maren") -> dict:
    """A stock asset the way a named human's pick registers it: attested."""
    return library.add_file(sp, path, source={"type": "stock", "provider": "pexels", "asset_id": "991"}, license_entry=dict(entry or STOCK),
                            by=by, alt=alt)


def add_unattested(sp: StudioProject, path: Path, *, by: str = "claude") -> dict:
    """What an agent-reachable path records (`ave studio library add`): a claim, not counted until a human attests."""
    return library.add_file(sp, path, source={"type": "own"}, by=by, attested=False)


def decode(path: Path, size=(32, 18)) -> np.ndarray:
    """Every frame of `path`, downscaled, as (n, h, w, 3) uint8."""
    w, h = size
    out = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-vf", f"scale={w}:{h}:flags=area", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(out, np.uint8).reshape(-1, h, w, 3)


def nearest_color(frame: np.ndarray) -> str:
    """Name of the closest B-roll block colour to the frame's mean, or 'other' (the camera blue/red picture, a mix)."""
    m = frame.reshape(-1, 3).mean(axis=0)
    d = {n: float(np.linalg.norm(m - np.array(c, float))) for n, c in BLOCKS}
    n = min(d, key=d.get)
    return n if d[n] < 70 else "other"
