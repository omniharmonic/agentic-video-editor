"""The asset registry: `library/assets.jsonl` (append-only, a later line for an id replaces an earlier one) and the hash-chained
`library/licenses.jsonl` (spec 2.4). Nothing here edits history: a tag change appends a new line, a license entry is never rewritten."""

from __future__ import annotations

from pathlib import Path

from ..assets import licenses
from ..studio import ledger, model, store

KIND_BY_EXT = {
    **dict.fromkeys((".jpg", ".jpeg", ".png", ".webp", ".gif", ".tif", ".tiff", ".bmp"), "image"),
    **dict.fromkeys((".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"), "video"),
    **dict.fromkeys((".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg", ".aif", ".aiff"), "audio"),
    ".pdf": "document",
}


def assets_path(sp) -> Path:
    return sp.library_dir / "assets.jsonl"


def license_log(sp) -> licenses.LicenseLog:
    """The studio's license log (reuses `LicenseLog`: hash chain, flock, redaction)."""
    return licenses.LicenseLog(sp.library_dir / "licenses.jsonl")


def asset_file(sp, rec: dict) -> Path:
    return sp.root / rec["file"]


def rel_file(sha: str, ext: str) -> str:
    return f"library/assets/{sha[:2]}/{sha}{ext}"


def read_all(sp) -> dict[str, dict]:
    """id -> latest registry line."""
    return store.read_assets(sp)


def get(sp, asset_id: str) -> dict:
    rec = read_all(sp).get(asset_id)
    if rec is None:
        raise KeyError(f"no such asset: {asset_id}")
    return rec


def find_by_sha(sp, sha: str) -> dict | None:
    return read_all(sp).get(model.asset_id(sha))


def append(sp, rec: dict) -> dict:
    """Validate against the Asset schema, then append. Raises ValueError before anything is written."""
    model.require_valid("asset", rec)
    ledger.append_jsonl(sp, assets_path(sp), rec)
    return rec


def update(sp, asset_id: str, **changes) -> dict:
    """Append a replacement line for `asset_id` (tags, alt, people). Immutable fields cannot change."""
    frozen = {"id", "sha256", "file", "ext", "kind", "bytes", "source", "license", "ai_generated", "derived_from", "derivation", "origin", "added", "untrusted_import"}
    bad = sorted(set(changes) & frozen)
    if bad:
        raise ValueError(f"cannot change {', '.join(bad)} of an asset; the bytes and their provenance are fixed")
    return append(sp, {**get(sp, asset_id), **changes})


def verify(sp) -> dict:
    """Integrity of the whole library: chain intact, every file present with the bytes its name claims, every asset's closure licensed."""
    from .coverage import coverage as compute
    problems = [f"license log: {p}" for p in license_log(sp).verify()]
    recs = read_all(sp)
    for rec in recs.values():
        p = asset_file(sp, rec)
        if not p.is_file():
            problems.append(f"{rec['id']}: file missing ({rec['file']})")
        elif licenses.sha256_file(p) != rec["sha256"]:
            problems.append(f"{rec['id']}: bytes do not match sha256 (file edited)")
    log = license_log(sp)
    for rec in recs.values():                  # an entry edited after it was registered (the chain alone cannot see a change to its newest line)
        want = (rec.get("license") or {}).get("entry")
        e = log.find(sha256=rec["sha256"]) if want else None
        if want and e is not None and licenses.entry_hash(e) != want:
            problems.append(f"{rec['id']}: its license-log entry was edited after it was registered")
    cov = compute(sp, list(recs))["missing"]
    pending = [m["asset"] for m in cov if "not attested by a human" in m["why"]]      # waiting for `library attest`: not corruption
    problems += [f"{m['asset']}: {m['why']}" for m in cov if "file missing" not in m["why"] and "bytes do not match" not in m["why"]
                 and m["asset"] not in pending]
    return {"ok": not problems, "problems": problems, "assets": len(recs), "pending_attestation": pending}
