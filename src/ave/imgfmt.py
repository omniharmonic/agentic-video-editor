"""Per-platform image deliverable format (spec 4.2: Instagram takes JPEG only). The reviewed file IS the posted file, so the conversion
happens when a deliverable is RENDERED or SYNCED (before the human looks), never inside an adapter at send time.

    accepted(platform)              -> ["jpeg"] | ["jpeg", "png"] | None   (config/platforms.yaml `<platform>.image.formats`; None = no rule)
    needs_jpeg(platform)            -> True when the platform does not accept PNG but accepts JPEG
    to_jpeg_bytes(data, max_bytes)  -> deterministic baseline JPEG bytes (same pixels, same bytes)
"""

from __future__ import annotations

import io


def accepted(platform: str) -> list[str] | None:
    try:
        from .publish.adapters import base
        spec = (base.load_platforms().get(platform) or {}).get("image") or {}
    except Exception:
        return None
    fmts = spec.get("formats")
    return [str(f).lower() for f in fmts] if fmts else None


def needs_jpeg(platform: str) -> bool:
    fmts = accepted(platform)
    return bool(fmts) and "jpeg" in fmts and "png" not in fmts


def max_file_bytes(platform: str) -> int | None:
    try:
        from .publish.adapters import base
        v = ((base.load_platforms().get(platform) or {}).get("image") or {}).get("max_file_bytes")
    except Exception:
        return None
    return int(v) if isinstance(v, int) else None


def to_jpeg_bytes(data: bytes, max_bytes: int | None = None) -> bytes:
    """Flatten any alpha onto white, drop metadata, encode a 4:4:4 JPEG at quality 95 (lowered only to fit `max_bytes`)."""
    from PIL import Image
    im = Image.open(io.BytesIO(data))
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.getchannel("A"))
        im = bg
    else:
        im = im.convert("RGB")
    for q in (95, 90, 85, 80, 70):
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=q, subsampling=0, optimize=False, progressive=False)
        if max_bytes is None or buf.tell() <= max_bytes:
            return buf.getvalue()
    return buf.getvalue()
