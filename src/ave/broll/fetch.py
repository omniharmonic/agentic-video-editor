"""Download one HUMAN-PICKED stock candidate into the library (spec 5.3 step 3).

Generalizes `assets.providers.download`: same promises, different destination and kinds.
  * a named human (`approved_by`) is required before ANY network call; an empty name is refused first;
  * bytes are streamed to a temp folder and checked (not an HTML error page, the right kind of file) before the library sees them;
  * then `library.add_file` copies them to `library/assets/<sha[:2]>/<sha><ext>`, appends exactly one hash-chained license entry and the
    registry line, and deletes its copy if either step fails: a failed download leaves nothing in the library, the log or the registry;
  * idempotent per provider + asset id: asking again returns the existing asset and logs nothing new;
  * no API key is ever sent to the CDN, and none reaches a file;
  * the download URL must be https on a host the PROVIDER is known to serve media from (`ALLOWED_HOSTS`), and redirects are followed by hand, each
    hop re-checked against the same list: a planted or poisoned search file cannot point a pick at an internal address or another site.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path

from ..assets import licenses
from ..library import ingest, registry
from .providers import Candidate, ProviderError, make_providers

MAX_BYTES = 2 * 1024 ** 3
MAX_HOPS = 3
# Hosts each provider serves full-size media from (documentation-derived, UNVERIFIED like the rest of the provider facts: extend on first contact).
ALLOWED_HOSTS = {
    "pexels": ("images.pexels.com", "videos.pexels.com"),
    "pixabay": ("pixabay.com", "cdn.pixabay.com"),
    "unsplash": ("images.unsplash.com",),
    "coverr": ("cdn.coverr.co", "storage.coverr.co"),
}


def check_download_url(provider: str, url: str) -> str:
    """The URL, if it is https on one of `provider`'s media hosts (exact host match, default port); else ProviderError. No IP literals, no userinfo."""
    from urllib.parse import urlsplit
    allowed = ALLOWED_HOSTS.get(provider)
    if not allowed:
        raise ProviderError(f"no download hosts are known for provider {provider!r}")
    try:
        u = urlsplit(url)
        host, port = (u.hostname or "").lower(), u.port
    except ValueError:
        raise ProviderError("the download URL is malformed") from None
    if u.scheme != "https" or host not in allowed or port not in (None, 443) or u.username or u.password:
        raise ProviderError(f"refusing to download from {host or url!r}: {provider} serves media only from {', '.join(allowed)} over https")
    return url
_CT_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif", "video/mp4": ".mp4",
           "video/quicktime": ".mov", "video/webm": ".webm"}
_KIND_OF = {"photo": "image", "video": "video"}


def sniff_ext(head: bytes) -> str | None:
    """File type from the first bytes (a provider's content-type header can lie or be missing; an HTML error page must never pass)."""
    if head[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[:4] == b"GIF8":
        return ".gif"
    if head[4:8] == b"ftyp":
        return ".mov" if head[8:12] == b"qt  " else ".mp4"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return ".webm"
    return None


def _chunks(r):
    if hasattr(r, "iter_content"):
        yield from r.iter_content(1 << 16)
    else:
        data = r.content
        for i in range(0, len(data), 1 << 16):
            yield data[i:i + (1 << 16)]


def _stream_to(http, url: str, dest_dir: Path, provider: str | None = None) -> tuple[Path, str | None]:
    r = http.get(url, timeout=120, stream=True, allow_redirects=False)          # no API key goes to the CDN; redirects are followed below, checked
    hops = 0
    while provider and 300 <= r.status_code < 400:
        hops += 1
        loc = ((getattr(r, "headers", None) or {}).get("Location") or "").strip()
        if hops > MAX_HOPS or not loc:
            raise ProviderError("download redirected too many times (or without a Location); nothing was kept")
        from urllib.parse import urljoin
        url = check_download_url(provider, urljoin(url, loc))
        r = http.get(url, timeout=120, stream=True, allow_redirects=False)
    sc = r.status_code
    if sc >= 400:
        raise ProviderError(f"download failed: HTTP {sc}")
    ctype = ((getattr(r, "headers", None) or {}).get("Content-Type") or "").split(";")[0].strip().lower() or None
    fd, name = tempfile.mkstemp(dir=dest_dir, suffix=".part")
    total = 0
    with os.fdopen(fd, "wb") as f:
        for chunk in _chunks(r):
            total += len(chunk)
            if total > MAX_BYTES:
                raise ProviderError("download is larger than the safety cap; refusing")
            f.write(chunk)
    if total == 0:
        raise ProviderError("download was empty")
    return Path(name), ctype


def _final_name(part: Path, ctype: str | None, url: str, kind: str) -> Path:
    head = part.read_bytes()[:16] if part.stat().st_size >= 16 else part.read_bytes()
    ext = sniff_ext(head) or _CT_EXT.get(ctype or "")
    if ext is None or head.lstrip()[:1] == b"<":     # the bytes decide; the URL's suffix is never trusted
        raise ProviderError(f"provider returned something that is not a {kind} (content-type {ctype or 'unknown'}); nothing was kept")
    if registry.KIND_BY_EXT.get(ext) != _KIND_OF[kind]:
        raise ProviderError(f"expected a {kind} but the bytes are {ext}; nothing was kept")
    return part.with_name(f"download{ext}")


def download(sp, cand, *, approved_by: str, http=None, provider=None, query: str = "", now: datetime | None = None,
             origin: dict | None = None) -> dict:
    """Download `cand` (a Candidate or its dict) and register it. Returns {"asset", "license_entry", "reused"}.

    `provider` is the provider object that produced the candidate (for its license text and download ping); it defaults to the stock
    provider named by `cand.provider`. `http` defaults to the provider's own client."""
    if not (approved_by or "").strip():
        raise ValueError("approved_by is required: a named human approves every download (AGENTS.md checkpoint rule)")
    approved_by = approved_by.strip()
    c = cand if isinstance(cand, Candidate) else Candidate.from_dict(cand)
    if provider is None:
        provider = make_providers(sp, [c.provider], http=http)[c.provider]

    if c.provider == "local":                           # already in the library: nothing to fetch, nothing to log
        rec = registry.get(sp, c.asset_id)
        return {"asset": rec, "license_entry": (rec.get("license") or {}).get("entry"), "reused": True}

    log = registry.license_log(sp)
    prior = log.find(source=c.provider, asset_id=c.asset_id)
    if prior:
        rec = registry.find_by_sha(sp, prior["sha256"])
        if rec and registry.asset_file(sp, rec).is_file():
            return {"asset": rec, "license_entry": licenses.entry_hash(prior), "reused": True}

    client = http if http is not None else provider.http
    tmp_root = sp.work / "broll"
    tmp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=tmp_root, prefix=".dl-") as td:
        check_download_url(c.provider, c.download_url)          # before ANY request
        part, ctype = _stream_to(client, c.download_url, Path(td), c.provider)
        final = part.rename(_final_name(part, ctype, c.download_url, c.kind))
        notice = None
        try:
            provider.on_download(c)
        except Exception as e:                            # best effort: a failed tracking ping must not lose a pick the human made
            notice = f"{type(e).__name__}: {e}"[:300]
        info = provider.license_info(c)
        entry = {"source": c.provider, "asset_id": c.asset_id, "url": c.page_url, "download_url": c.download_url, "author": c.author,
                 "author_url": c.author_url, "query": query, "approved_by": approved_by, "duration": c.duration, "size": [c.w, c.h], **info}
        if notice:
            entry["provider_notice"] = notice
        rec, created = ingest.add_file_result(
            sp, final, source={"type": "stock", "provider": c.provider, "asset_id": c.asset_id, "url": c.page_url}, license_entry=entry,
            by=approved_by, tags=list(c.tags)[:12], alt=(c.extra or {}).get("alt") or "", origin={"broll_query": query, **(origin or {})},
            now=now)
    ent = log.find(sha256=rec["sha256"])
    return {"asset": rec, "license_entry": licenses.entry_hash(ent) if ent else None, "reused": not created}


__all__ = ["download", "sniff_ext", "MAX_BYTES"]
