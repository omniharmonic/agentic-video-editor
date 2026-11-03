"""Per-platform deliverables of a post (spec 4.2, 8 `deliverables`): the file the human reviews is the file that gets posted.

    ensure(sp, post_id)   for every variant whose platform does not accept an image's format (Instagram: JPEG only), derive a JPEG asset
                          in the library (`source.type: render`, `derived_from: [original]`, so licensing follows the original) and point
                          the variant's own media at it. Idempotent: the same original always maps to the same JPEG asset.
    report(sp, post)      what each variant would post: asset, size, format, accepted formats, problems (read-only)

`studio.sync.sync_queue` calls `ensure` first, so the queue item's file list, hash and approval all bind the JPEG. Nothing here approves.
"""

from __future__ import annotations

import copy
from datetime import datetime
from pathlib import Path

from .. import imgfmt
from ..publish import safety
from . import model, store
from .clock import SystemClock
from .events import AGENT, Actor


def _format_of(path: Path) -> str | None:
    from PIL import Image
    try:
        with Image.open(path) as im:
            return (im.format or "").lower()
    except Exception:
        return None


def _derived_jpeg(sp, rec: dict) -> dict | None:
    for a in store.read_assets(sp).values():
        if a.get("derived_from") == [rec["id"]] and a.get("ext") in (".jpg", ".jpeg") and (a.get("source") or {}).get("type") == "render":
            return a
    return None


def _make_jpeg(sp, rec: dict, platform: str, by: str, now: datetime) -> dict:
    existing = _derived_jpeg(sp, rec)
    if existing:
        return existing
    src = sp.root / rec["file"]
    data = imgfmt.to_jpeg_bytes(src.read_bytes(), imgfmt.max_file_bytes(platform))
    tmp = sp.work / "deliverables" / f"{rec['sha256'][:16]}.jpg"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(data)
    from .. import library
    return library.add_file(sp, tmp, source={"type": "render"}, by=by, derived_from=[rec["id"]], alt=rec.get("alt") or "",
                            tags=list(rec.get("tags") or []), origin={"deliverable_of": rec["id"], "for": platform}, now=now)


def ensure(sp, post_id: str, *, actor: Actor = AGENT, now: datetime | None = None, variant_id: str | None = None) -> list[dict]:
    """Convert what must be converted; returns [{"variant", "index", "from", "to", "platform"}] (empty = nothing to do)."""
    now = now or SystemClock().now()
    post = store.load_post(sp, post_id)
    assets = store.read_assets(sp)
    changes: list[dict] = []
    edited = copy.deepcopy(post)
    for v in edited["variants"]:
        if variant_id is not None and v['id'] != variant_id: continue
        ref = v.get("queue")
        if v.get("dropped") or (ref and ref.get("root", ".") != "."):
            continue
        from .composer import delivery
        if delivery(v) == "manual": continue
        if not imgfmt.needs_jpeg(v["platform"]):
            continue
        media = copy.deepcopy(model.effective_media(post, v))
        touched = False
        for i, m in enumerate(media):
            if m.get("role")=="cover": continue
            rec = assets.get(m["asset"])
            if not rec or rec.get("kind") != "image" or not (sp.root / rec["file"]).is_file():
                continue
            if _format_of(sp.root / rec["file"]) == "jpeg":
                continue
            new = _make_jpeg(sp, rec, v["platform"], actor.name if actor.name else "claude", now)
            media[i] = {**m, "asset": new["id"]}
            changes.append({"variant": v["id"], "index": i, "from": rec["id"], "to": new["id"], "platform": v["platform"]})
            touched = True
        if touched:
            v["media"] = media
    if changes:
        store.save_post(sp, edited, expected_rev=post["rev"], actor=actor, op="deliverables", detail={"converted": changes}, now=now)
    return changes


def report(sp, post: dict) -> list[dict]:
    assets = store.read_assets(sp)
    out = []
    for v in post.get("variants") or []:
        if v.get("dropped"):
            continue
        from .composer import projection, delivery
        projected = projection(post,v,assets)
        accepted = None if delivery(v) == "manual" else imgfmt.accepted(v["platform"])
        rows, problems = [], []
        for m in projected["content"]:
            rec = assets.get(m["asset"])
            if not rec:
                problems.append(f"asset {m['asset']} is not in the library")
                continue
            f = sp.root / rec["file"]
            fmt = _format_of(f) if rec.get("kind") == "image" and f.is_file() else None
            ok = rec.get("kind") != "image" or not accepted or fmt in accepted
            if not ok:
                problems.append(f"{m['asset']} is {fmt}; {v['platform']} accepts {', '.join(accepted)} (run `ave studio queue sync` or `ave studio deliverables --apply`)")
            rows.append({"asset": m["asset"], "kind": rec.get("kind"), "format": fmt, "w": rec.get("w"), "h": rec.get("h"),
                         "sha256": safety.sha256_file(f) if f.is_file() else None, "ok": ok})
        out.append({"variant": v["id"], "account": v["account"], "platform": v["platform"], "post_type": v["post_type"],
                    "accepted_formats": accepted, "media": rows, "cover": projected["cover"], "problems": problems + projected["problems"]})
    return out
