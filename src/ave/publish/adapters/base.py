"""Publisher interface, shared exceptions, probing and preflight against config/platforms.yaml.

WHY the outcome taxonomy matters: after a crash we must know whether a post may have gone out. `PublishError`
means the platform definitely refused (safe to retry later); any other exception or a hard crash leaves the
platform state "sending", which the queue treats as UNKNOWN and never blindly resends.
"""

from __future__ import annotations

import json
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from ... import resources

PLATFORMS_YAML = resources.config_file("platforms.yaml")


class PublishError(Exception):
    """The platform DEFINITELY did not publish (rejected, quota, auth). Safe to retry after fixing the cause."""


class Uncertain(Exception):
    """A post may or may not have gone out. Never resend without confirming."""


@dataclass
class Absent:
    """recover() verdict: the platform confirms nothing was posted, so a retry cannot double-post."""
    detail: str = ""


def load_platforms(path: Path | None = None) -> dict:
    return yaml.safe_load(Path(path or PLATFORMS_YAML).read_text()) or {}


def probe_video(path: str | Path) -> dict:
    """duration, width, height, size via ffprobe (kept local so publish has no dependency on other modules)."""
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise ValueError(f"ffprobe could not read {path}: {out.stderr.strip()[:200]}")
    data = json.loads(out.stdout)
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if not v:
        raise ValueError(f"{path} has no video stream")
    rot = 0
    for sd in v.get("side_data_list") or []:
        rot = int(sd.get("rotation", rot) or 0)
    rot = int((v.get("tags") or {}).get("rotate", rot) or rot)
    w, h = int(v["width"]), int(v["height"])
    if abs(rot) % 180 == 90:
        w, h = h, w
    dur = float(data["format"].get("duration") or v.get("duration") or 0)
    return {"duration": dur, "width": w, "height": h, "size": int(data["format"].get("size") or Path(path).stat().st_size)}


def orientation(w: int, h: int) -> str:
    if w == h:
        return "square"
    return "vertical" if h > w else "horizontal"


def effective_format(item: dict, info: dict) -> str:
    """'short' or 'long'. `auto` means vertical => short, otherwise long."""
    f = item.get("format") or "auto"
    if f in ("short", "long"):
        return f
    return "short" if orientation(info["width"], info["height"]) == "vertical" else "long"


def limit_problems(spec: dict | None, info: dict, label: str) -> list[str]:
    """Compare a probed video with one limits block. Each key is optional so config can stay sparse."""
    if not spec:
        return []
    p: list[str] = []
    d = info["duration"]
    if "max_duration_s" in spec and d > spec["max_duration_s"]:
        p.append(f"{label}: duration {d:.1f}s exceeds max {spec['max_duration_s']}s")
    if "min_duration_s" in spec and d < spec["min_duration_s"]:
        p.append(f"{label}: duration {d:.1f}s is under min {spec['min_duration_s']}s")
    if "max_file_bytes" in spec and info["size"] > spec["max_file_bytes"]:
        p.append(f"{label}: file is {info['size']} bytes, max {spec['max_file_bytes']}")
    o = orientation(info["width"], info["height"])
    if spec.get("aspect") and o not in spec["aspect"]:
        p.append(f"{label}: needs {'/'.join(spec['aspect'])} video but this file is {info['width']}x{info['height']} ({o})")
    if "min_width" in spec and info["width"] < spec["min_width"]:
        p.append(f"{label}: width {info['width']} under min {spec['min_width']}")
    if "min_height" in spec and info["height"] < spec["min_height"]:
        p.append(f"{label}: height {info['height']} under min {spec['min_height']}")
    return p


# ---- image posts (media_type image | carousel) ---------------------------------------------------

def probe_image(path: str | Path) -> dict:
    """format, width, height, size of an image file (Pillow; no network)."""
    from PIL import Image
    try:
        with Image.open(path) as im:
            fmt, (w, h) = (im.format or "").lower(), im.size
    except Exception as e:
        raise ValueError(f"cannot read image {path}: {e}")
    return {"format": fmt, "width": w, "height": h, "size": Path(path).stat().st_size}


def image_dimension_problems(spec: dict | None, w: int, h: int, carousel: bool = False) -> list[str]:
    """Pixel-size and aspect problems for one image against a platform's `image:` block (every key optional)."""
    if not spec:
        return []
    p: list[str] = []
    if "min_width" in spec and w < spec["min_width"]:
        p.append(f"image width {w} is under the minimum {spec['min_width']}")
    if "max_width" in spec and w > spec["max_width"]:
        p.append(f"image width {w} is over the maximum {spec['max_width']}")
    ratio = w / h
    if "aspect_min" in spec and ratio < spec["aspect_min"] - 1e-6:
        p.append(f"image aspect {w}:{h} ({ratio:.2f}) is narrower than the minimum {spec['aspect_min']:.2f} (4:5 is 0.80)")
    if "aspect_max" in spec and ratio > spec["aspect_max"] + 1e-6:
        p.append(f"image aspect {w}:{h} ({ratio:.2f}) is wider than the maximum {spec['aspect_max']:.2f}")
    return p


def image_problems(cfg: dict, platform: str, item: dict, files: list[Path]) -> list[str]:
    """Everything wrong with an image/carousel post for one platform, from config/platforms.yaml `<platform>.image` / `.carousel`.
    Pure (reads only the files). Alt text is required for every image."""
    spec = (cfg.get(platform) or {}).get("image")
    if spec is None:
        return [f"{platform}: no image limits in config/platforms.yaml (add an `image:` block before posting images there)"]
    p: list[str] = []
    mt = item.get("media_type")
    car = (spec.get("carousel") or {})
    if mt == "carousel":
        if not car:
            p.append(f"{platform}: carousels are not configured for this platform")
        if "min_items" in car and len(files) < car["min_items"]:
            p.append(f"{platform}: a carousel needs at least {car['min_items']} images (has {len(files)})")
        if "max_items" in car and len(files) > car["max_items"]:
            p.append(f"{platform}: a carousel holds at most {car['max_items']} images (has {len(files)})")
    elif len(files) != 1:
        p.append(f"{platform}: an image post has exactly one image (has {len(files)}); use media_type carousel for several")
    alts = item.get("alt_texts") or []
    if len(alts) != len(files) or not all(isinstance(a, str) and a.strip() for a in alts):
        p.append(f"{platform}: alt text is required for every image ({len(files)} images, {len([a for a in alts if str(a).strip()])} alt texts)")
    for a in alts:
        if "alt_text_max_chars" in spec and len(a) > spec["alt_text_max_chars"]:
            p.append(f"{platform}: alt text is {len(a)} characters, max {spec['alt_text_max_chars']}")
    text = item.get("caption") or item.get("description") or ""
    if "caption_max_chars" in spec and len(text) > spec["caption_max_chars"]:
        p.append(f"{platform}: caption is {len(text)} characters, max {spec['caption_max_chars']}")
    sizes = set()
    for f in files:
        try:
            info = probe_image(f)
        except ValueError as e:
            p.append(f"{platform}: {e}")
            continue
        sizes.add((info["width"], info["height"]))
        if spec.get("formats") and info["format"] not in spec["formats"]:
            p.append(f"{platform}: {Path(f).name} is {info['format']}, accepted: {', '.join(spec['formats'])}")
        if "max_file_bytes" in spec and info["size"] > spec["max_file_bytes"]:
            p.append(f"{platform}: {Path(f).name} is {info['size']} bytes, max {spec['max_file_bytes']}")
        p += [f"{platform}: {Path(f).name}: {m}" for m in image_dimension_problems(spec, info["width"], info["height"], mt == "carousel")]
    if mt == "carousel" and len(sizes) > 1:
        p.append(f"{platform}: every carousel image must be the same size (found {sorted(sizes)}); platforms crop to the first")
    return p


def parse_schedule(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"schedule {s!r} needs a timezone offset (e.g. 2026-11-01T09:00:00-06:00)")
    return dt.astimezone(timezone.utc)


class Publisher(ABC):
    """One platform. Adapters never read the queue or approval state; the queue decides whether to call them."""

    name = "base"
    carries_images = False        # True only for an adapter that can post image/carousel items (media_type != video)

    def __init__(self, platforms_cfg: dict | None = None, now: Callable[[], datetime] | None = None):
        self.cfg = platforms_cfg if platforms_cfg is not None else load_platforms()
        self.now = now or (lambda: datetime.now(timezone.utc))

    @abstractmethod
    def preflight(self, item: dict, video: Path) -> list[str]:
        """Return human-readable problems that make this post invalid. Pure: no network, no side effects."""

    @abstractmethod
    def publish(self, item: dict, video: Path, progress: Callable[[dict], None]) -> dict:
        """Send the post. `progress(partial)` persists identifiers (upload ids) as soon as they exist so a crash is
        recoverable. Raise PublishError only when the platform definitely did not publish."""

    def ready_problems(self) -> list[str]:
        """Credentials/config missing for a real send. Checked only when sending, so dry-runs work on any machine."""
        return []

    def recover(self, item: dict, record: dict) -> dict | Absent | None:
        """After a crash (state 'sending'): return a result if the post exists, Absent if provably not posted,
        None if unknown (the human must `ave publish resolve`)."""
        return None

    def status(self) -> dict:
        """Credentials present? Never returns secret values."""
        return {"platform": self.name, "ready": False}

    def check_schedule(self, item: dict, lo_min: float | None = None, hi_days: float | None = None) -> list[str]:
        try:
            dt = parse_schedule(item.get("schedule"))
        except ValueError as e:
            return [str(e)]
        if dt is None:
            return []
        delta = (dt - self.now()).total_seconds()
        if delta <= 0:
            return [f"{self.name}: schedule {item['schedule']} is in the past"]
        if lo_min is not None and delta < lo_min * 60:
            return [f"{self.name}: schedule must be at least {lo_min:g} minutes ahead"]
        if hi_days is not None and delta > hi_days * 86400:
            return [f"{self.name}: schedule must be within {hi_days:g} days"]
        return []
