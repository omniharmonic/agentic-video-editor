"""The studio asset library (WS1, spec 2.4, 5.4): content-addressed bytes, an append-only registry, a hash-chained license log, a derived
search index, and license coverage over the derived_from closure.

    add_file(sp, path, *, source, license_entry, by, ...) -> asset     search(sp, q, *, kind, tag, unused, limit) -> [asset]
    get(sp, asset_id) -> asset                                         coverage(sp, asset_ids) -> {"covered", "missing", ...}
    reindex(sp) -> int                                                 verify(sp) -> {"ok", "problems", "assets"}
    register(studio)   plug the facade hooks (library, library_asset, upload) into a `Studio`
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from .coverage import coverage
from .index import reindex, search, usage
from .ingest import add_derivative, add_file, add_file_result, attest_license, attested_shas, unattested
from .registry import get, read_all, update as update_asset, verify

__all__ = ["add_derivative", "add_file", "add_file_result", "attest_license", "attested_shas", "unattested", "coverage", "get", "read_all", "reindex", "search", "update_asset", "usage", "verify", "register"]


def _view(sp, rec: dict, *, with_usage: bool = True) -> dict:
    out = dict(rec)
    if with_usage:
        out["usage"] = usage(sp, rec["id"])
    out["license_ok"] = coverage(sp, [rec["id"]])["ok"]
    out["url"] = "/files/" + rec["file"]
    from ..studio.image_workshop import preview_url
    out["preview_url"] = preview_url(rec)
    return out


def hook_library(studio, q=None, kind=None, tag=None, unused=False, limit=50, license=None, source=None) -> dict:
    """GET /api/studio/library -> {"assets": [asset + usage + license_ok + url], "total"} (shape: tests/data_studio/api/GET_library.json).
    `license` = "ok" | "missing" keeps assets whose license closure is / is not covered; `source` keeps one source type (own, stock, render...)."""
    if license not in (None, "", "ok", "missing"):
        raise ValueError("license must be ok or missing")
    rows = search(studio.sp, q, kind=kind, tag=tag, unused=unused, limit=100000)
    if source:
        rows = [r for r in rows if (r.get("source") or {}).get("type") == source]
    views = [_view(studio.sp, r) for r in rows]
    if license:
        views = [v for v in views if v["license_ok"] == (license == "ok")]
    total = len(views)                       # the count that matches, not the count returned: the UI says "50 of 99" when it is cut
    views = views[:max(0, limit)]
    return {"assets": views, "total": total}


def hook_library_asset(studio, asset_id: str) -> dict:
    """GET /api/studio/library/<id> -> {"asset", "usage", "url", "license": {"covered", "entries", "missing"}}."""
    sp = studio.sp
    rec = get(sp, asset_id)
    cov = coverage(sp, [asset_id])
    from ..studio.image_workshop import preview_url
    return {"preview_url": preview_url(rec,"large"), "asset": rec, "usage": usage(sp, asset_id), "url": "/files/" + rec["file"],
            "license": {"covered": cov["ok"],
                        "entries": [{"entry": c["entry"], "provider": c["source"], "summary": c["license"]} for c in cov["covered"]],
                        "missing": cov["missing"]}}


def hook_upload(studio, name: str, data, *, by: str = "maren", alt: str = "", tags=(), derived_from=(), source_type: str | None = None) -> dict:
    """PUT /api/studio/library/upload -> {"asset", "duplicate"}. `data` is bytes or a path to a file the server already streamed to disk.
    A file with `derived_from` is a render; otherwise it is the human's own file (`source_type="import"` is refused here: it needs a
    license entry, so it goes through add_file)."""
    stype = source_type or ("render" if derived_from else "own")
    if stype not in ("own", "render"):
        raise ValueError("upload accepts own files and renders; use add_file with a license entry for anything else")
    name = Path(name).name
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / name
        if isinstance(data, (bytes, bytearray)):
            src.write_bytes(bytes(data))
        else:
            src.write_bytes(Path(data).read_bytes())
        rec, created = add_file_result(studio.sp, src, source={"type": stype}, by=by, alt=alt, tags=tags, derived_from=derived_from,
                                       origin={"upload": name}, now=studio.now())
    return {"asset": rec, "duplicate": not created}


def register(studio) -> None:
    """Plug the library hooks into a `Studio` (WS0 facade): `library`, `library_asset`, `upload`."""
    studio.register("library", hook_library)
    studio.register("library_asset", hook_library_asset)
    studio.register("upload", hook_upload)
