"""Synthetic episodes for the release-package tests. No network, no private footage, no real HOME or config."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from ave import editdoc
from ave.episode import Episode, write_json

QUOTE = "A goal is a wish and a system is a habit"
LONG_QUOTE = ("The ground remembers what we do to it long after we have stopped paying attention to it and that is the whole reason "
              "we tend a field for a generation instead of a season")
SPEAKERS = {"S1": {"name": "Maren Okafor", "channel": "left"}, "S2": {"name": "Tomas", "channel": "right"}}
FINAL_S = 40


def sha(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


@pytest.fixture(scope="session")
def final_video(tmp_path_factory):
    """A 40 s, 640x360 video with a steady tone (so loudness normalisation has something real to measure)."""
    d = tmp_path_factory.mktemp("pkgvideo")
    p = d / "final.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=gray:s=640x360:r=10:d={FINAL_S}",
                    "-f", "lavfi", "-i", f"sine=f=220:d={FINAL_S}", "-filter:a", "volume=0.3", "-shortest", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", str(p)], check=True)
    return p


def png(path: Path, size=(1080, 1350), color=(27, 24, 20)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    return path


def noisy_png(path: Path, size=(1280, 720)) -> Path:
    """A PNG that does not compress (random pixels): over 2 MB at 1280x720."""
    import os
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3)).save(path)
    return path


def make_episode(root: Path, layout: str | None = None) -> Episode:
    ep = Episode(root)
    ep.create("The Long Table 00", layout=layout)
    meta = json.loads(ep.meta_path.read_text())
    meta["show"] = "The Long Table"
    meta["speakers"] = SPEAKERS
    write_json(ep.meta_path, meta)
    return ep


def add_final(ep: Episode, final_video: Path, *, version: int = 1, srt: bool = True, chapters: bool = True, doc: bool = True) -> Path:
    ep.exports.mkdir(parents=True, exist_ok=True)
    out = ep.exports / f"final.v{version}.master.mp4"
    shutil.copy(final_video, out)
    if srt:
        (ep.exports / f"final.v{version}.srt").write_text("1\n00:00:01,000 --> 00:00:03,500\nHello and welcome.\n\n2\n00:00:04,000 --> 00:00:08,000\nA second line\nover two rows.\n",
                                                         encoding="utf-8")
    if chapters:
        write_json(ep.work / "chapters.json", [{"start": 0, "title": "Opening"}, {"start": 15, "title": "Soil and habit"}, {"start": 28, "title": "What stays"}])
    if doc:
        d = editdoc.new_doc("final", 10, (640, 360), {"cam": {"kind": "video", "file": "x.mov", "start_offset": 0.0}}, duration=float(FINAL_S))
        d["tracks"]["video"] = [{"id": "v1", "src": "cam", "e0": 100.0, "o0": 0.0, "o1": float(FINAL_S)}]
        d["tracks"]["dialogue"] = [{"id": "d1", "src": "cam", "e0": 100.0, "o0": 0.0, "o1": float(FINAL_S)}]
        write_json(editdoc.path_for(ep, "final", version), d)
    return out


def add_clips(ep: Episode, statuses: dict[str, str]) -> None:
    clips = [{"id": cid, "slug": f"clip-{cid}", "title": f"Clip {cid}", "status": st, "hook": f"hook {cid}", "hook_quote": QUOTE if cid == "c01" else None,
              "speakers": ["S1"], "text": QUOTE, "total": 70.0, "duration": 4.0, "start": 1.0, "end": 5.0} for cid, st in statuses.items()]
    write_json(ep.work / "clips.json", {"version": 1, "clips": clips})


def add_short(ep: Episode, vertical: Path, cid: str, plat: str = "youtube", ver: int = 1, ok: bool = True) -> Path:
    d = ep.exports / "shorts"
    d.mkdir(parents=True, exist_ok=True)
    slug = f"clip-{cid}"
    mp4 = d / f"{slug}.v{ver}.{plat}.mp4"
    shutil.copy(vertical, mp4)
    side = {"name": mp4.stem, "platform": plat, "title": f"Clip {cid}", "hook": f"hook {cid}", "description": "d", "caption": f"hook {cid}", "tags": [],
            "video": ep.rel(mp4), "sha256": sha(mp4), "duration": 4.0, "width": 360, "height": 640, "limits": {"ok": ok, "problems": [] if ok else ["too long"]},
            "licenses": {"ok": True, "attribution": []}, "encode": {"problems": []}, "clip": {"id": cid, "slug": slug}}
    (d / f"{slug}.v{ver}.{plat}.json").write_text(json.dumps(side))
    return mp4


def add_content(ep: Episode, *, thumb: Path | None = None, status: str = "approved", cards: str = "approved", carousel: str = "approved") -> None:
    """A ContentDoc with a thumbnail, a quote card and a 3-slide carousel (written directly; the renderer needs Chromium)."""
    items = []

    def out(item_id, name, size, platform, slide=None, alt="alt text"):
        p = png(ep.work / "content" / "renders" / name, size)
        o = {"platform": platform, "w": size[0], "h": size[1], "file": ep.rel(p), "sha256": sha(p), "alt_text": alt}
        if slide:
            o["slide"] = slide
        return o

    def item(item_id, template, slots, outputs, st, platforms, caption=""):
        items.append({"id": item_id, "template": template, "style": "ink", "format": "4x5", "renderer": "motion-kit", "slots": slots, "source": None,
                      "platforms": platforms, "caption": caption, "alt_text": None, "outputs": outputs,
                      "review": {"status": st, "critic": None, "by": "ben" if st == "approved" else None, "at": "2026-10-14T10:00:00" if st == "approved" else None, "note": ""},
                      "provenance": {}, "license": {"assets": [], "ai_generated": False}, "reason": "r", "confidence": 0.9})

    if thumb is not None:
        o = {"platform": "youtube", "w": 1280, "h": 720, "file": ep.rel(thumb), "sha256": sha(thumb), "alt_text": "Thumbnail: The Long Table 00"}
        item("thumb-1", "thumbnail", {"title": "The Long Table 00"}, [o], status, ["youtube"])
    item("quote-q1", "quote-post", {"quote": QUOTE, "attribution": "Maren Okafor", "show": "The Long Table"},
         [out("quote-q1", "quote-q1-4x5.png", (1080, 1350), "instagram", alt="Quote card: a goal is a wish.")], cards, ["instagram", "x"])
    item("carousel-c1", "carousel", {"cover": {"title": "Small repairs"}, "closing": {"cta": ""}},
         [out("carousel-c1", f"carousel-c1-{n}.png", (1080, 1350), "instagram", slide=n, alt=f"Slide {n}") for n in (1, 2, 3)], carousel, ["instagram"])
    write_json(ep.work / "content" / "main.v1.json", {"version": 1, "kind": "content", "id": "main", "episode": ep.root.name, "sources": {}, "items": items})


def full_episode(root: Path, final_video: Path, video_pool: Path, **kw) -> Episode:
    """Everything present and approved: final cut + SRT + chapters, an approved clip with a short, thumbnail, quote card, carousel."""
    ep = make_episode(root, kw.pop("layout", None))
    add_final(ep, final_video)
    add_clips(ep, {"c01": "approved", "c02": "proposed"})
    add_short(ep, video_pool / "vertical.mp4", "c01", "youtube")
    add_short(ep, video_pool / "vertical.mp4", "c01", "reels")
    add_short(ep, video_pool / "vertical.mp4", "c02", "youtube")        # clip c02 is only proposed: this export is a DRAFT
    thumb = png(ep.work / "content" / "renders" / "thumb.png", (1280, 720), (30, 60, 50))
    add_content(ep, thumb=thumb)
    return ep


def tree(root: Path) -> dict[str, int]:
    return {str(p.relative_to(root)): p.stat().st_size for p in sorted(root.rglob("*")) if p.is_file()}
