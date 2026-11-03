"""Adding bytes to the library: copy by sha, probe, license-log, registry line (spec 2.4, 5.4).

Order is the same as `assets.providers.download`: the bytes are safely in `library/assets/<sha[:2]>/<sha><ext>` first, then the license entry,
then the registry line; if anything after the copy fails, the file this call created is deleted, so a file never exists without its trail.
The registry line is validated against the schema BEFORE the copy, so the usual failure (a bad record) writes nothing at all.
Copy, never move: the source (a raw drop, an episode export) is left exactly as it was.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from ..assets import licenses
from ..studio import model
from ..studio.clock import SystemClock
from . import registry

SOURCE_TYPES = ("own", "stock", "render", "generated", "import")


def _probe(path: Path, kind: str) -> dict:
    out = {"w": None, "h": None, "duration": None, "fps": None}
    if kind == "image":
        from PIL import Image
        with Image.open(path) as im:
            im.load()                      # a truncated or corrupt image must fail here, not at posting time
            out["w"], out["h"] = im.size
        return out
    if kind in ("video", "audio"):
        try:
            p = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
                               capture_output=True, text=True, timeout=60)
            j = json.loads(p.stdout or "{}") if p.returncode == 0 else {}
        except (OSError, subprocess.SubprocessError, ValueError):
            j = {}
        v = next((s for s in j.get("streams", []) if s.get("codec_type") == "video" and s.get("width")), None)
        if v:
            out["w"], out["h"] = v["width"], v["height"]
            num, _, den = (v.get("avg_frame_rate") or "0/1").partition("/")
            try:
                out["fps"] = round(float(num) / float(den or 1), 3) if float(den or 1) else None
            except ValueError:
                pass
        try:
            out["duration"] = float((j.get("format") or {}).get("duration"))
        except (TypeError, ValueError):
            pass
    return out


def _faces(path: Path, kind: str, duration: float | None) -> int | None:
    """Faces in the image (or a mid-clip frame). None = could not be measured: unknown is never reported as 'no faces'."""
    try:
        from ..assets import index
        if kind == "image":
            return index.measure(path)["face"]
        if kind == "video":
            with tempfile.TemporaryDirectory() as td:
                thumb = Path(td) / "f.jpg"
                if index._thumb(path, (duration or 0) / 2, thumb):
                    return index.measure(thumb)["face"]
    except Exception:
        pass
    return None


def default_own_entry(by: str, at: str) -> dict:
    """The log entry for the creator's own footage (docs/asset-licensing.md 'Own footage')."""
    return {"source": "own-footage", "url": "file://own", "license": "Owned", "license_url": "file://own",
            "license_text": f"Made or shot by {by} (added {at[:10]}).", "attribution": {"required": False, "text": None}}


def _add_file_result(sp, path, *, source: dict, license_entry: dict | None = None, by: str, tags=(), alt: str = "", derived_from=(),
                    ai_generated: bool = False, origin: dict | None = None, now: datetime | None = None,
                    attested: bool = True, _occurrence: dict | None = None) -> tuple[dict, bool]:
    """`add_file` plus whether a new registry line was written (False = these bytes were already in the library).

    `attested=False` is what an AGENT-reachable path (`ave studio library add`, `ave studio adopt`) passes: the license entry says who claimed
    the license, not that a human checked it. The closure (`library.coverage`) does not count such an entry until a human runs
    `ave studio library attest` (a signed record in the sealed control ledger). Human-gated paths (a named B-roll pick, the UI upload)
    leave the default."""
    if not (by or "").strip():
        raise ValueError("by is required: every library asset records who added it")
    src = Path(path)
    if not src.is_file():
        raise FileNotFoundError(f"no such file: {src}")
    stype = (source or {}).get("type")
    if stype not in SOURCE_TYPES:
        raise ValueError(f"source.type must be one of {', '.join(SOURCE_TYPES)} (got {stype!r})")
    ext = src.suffix.lower()
    kind = registry.KIND_BY_EXT.get(ext)
    if kind is None:
        raise ValueError(f"unsupported file type {ext or '(none)'}; the library takes {', '.join(sorted(registry.KIND_BY_EXT))}")
    sha = licenses.sha256_file(src)
    occurrence_id = model.asset_id(_occurrence['sha256']) if _occurrence else None
    existing = registry.read_all(sp).get(occurrence_id) if _occurrence else registry.find_by_sha(sp, sha)
    if _occurrence and existing:
        if (existing.get('derivation') != _occurrence or existing['sha256'] != sha or
                existing.get('derived_from') != list(derived_from) or existing.get('origin') != origin):
            raise ValueError('Derivative occurrence ID collision; existing history preserved')
        if not registry.asset_file(sp, existing).is_file() or licenses.sha256_file(registry.asset_file(sp, existing)) != sha:
            raise ValueError('Derivative occurrence bytes changed; existing history preserved')
        return existing, False
    if existing and (existing["sha256"] != sha or existing.get("derivation")):
        raise ValueError("Asset ID collision; existing history preserved")
    if existing and registry.asset_file(sp, existing).is_file():
        return existing, False

    known = registry.read_all(sp)
    derived = list(dict.fromkeys(derived_from))
    unknown = [d for d in derived if d not in known]
    if unknown:
        raise ValueError(f"derived_from names unknown assets: {', '.join(unknown)} (register the inputs first)")
    stamp = (now or SystemClock().now()).strftime("%Y-%m-%dT%H:%M:%S%z")
    rel = registry.rel_file(sha, ext)
    size = src.stat().st_size

    entry = None
    if stype != "render":
        entry = dict(license_entry or {})
        if not entry and stype == "own":
            entry = default_own_entry(by, stamp)
        if not entry:
            raise ValueError(f"a {stype} asset needs a license_entry (source, url, license, license_url, license_text): "
                             "no bytes enter the library without a license trail")
        entry.setdefault("source", (source or {}).get("provider") or stype)
        entry.setdefault("url", (source or {}).get("url"))
        entry.update({"sha256": sha, "file": rel, "bytes": size})
        entry.setdefault("approved_by", by)
        entry.setdefault("attribution", {"required": False, "text": None})
        if not attested:
            entry["attested"] = False             # inside the hash-chained license log, so removing the flag breaks the chain
        missing = [k for k in licenses.REQUIRED if not str(entry.get(k) or "").strip()]
        if missing:
            raise ValueError(f"license entry incomplete, missing: {', '.join(missing)}")

    media = _probe(src, kind)
    faces = _faces(src, kind, media["duration"])
    people = {"faces": faces, "release": None} if faces is not None else {"release": None, "faces_checked": False}
    att_summary = {"required": False, "text": None}
    if entry:
        a = entry.get("attribution") or {}
        att_summary = {"required": bool(a.get("required")), "text": a.get("text")}

    log = registry.license_log(sp)
    dest = sp.root / rel
    created = False
    logged = None
    try:
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            part = dest.with_name(f".{dest.name}.part")
            try:
                shutil.copyfile(src, part)
                if part.stat().st_size != size:
                    raise OSError("copy is shorter than the source (disk full or source changing)")
                os.replace(part, dest)
                created = True
            finally:
                part.unlink(missing_ok=True)
        if _occurrence and licenses.sha256_file(dest) != sha:
            raise ValueError("Content-addressed derivative bytes changed")
        if entry:
            logged = log.find(sha256=sha)           # a crash after the log line but before the registry line must not log twice
            if logged is None:
                logged = log.append(entry)
        rec = {"v": 1, "id": occurrence_id or model.asset_id(sha), "sha256": sha, "kind": kind, "ext": ext, "file": rel, "bytes": size,
               "w": media["w"], "h": media["h"], "duration": media["duration"], "fps": media["fps"],
               "source": {"type": stype, "provider": source.get("provider"), "asset_id": source.get("asset_id"), "url": source.get("url")},
               "license": ({"entry": licenses.entry_hash(logged), "summary": logged["license"], "attribution": att_summary,
                            "attested": logged.get("attested") is not False} if logged else
                           {"entry": None, "summary": "derived: licensed through its inputs", "attribution": att_summary}),
               "ai_generated": bool(ai_generated or stype == "generated"), "derived_from": derived,
               "tags": list(dict.fromkeys(str(t).strip() for t in tags if str(t).strip())), "alt": alt or "", "people": people,
               "origin": origin, "added": stamp, "added_by": by.strip()}
        if _occurrence: rec["derivation"] = _occurrence
        model.require_valid("asset", rec)
        registry.append(sp, rec)
    except Exception:
        if created:
            dest.unlink(missing_ok=True)
        raise
    return rec, True


def add_file_result(sp, path, **kwargs) -> tuple[dict, bool]:
    """Serialize registrations while preserving ordinary byte-deduplicated imports."""
    import fcntl
    sp.library_dir.mkdir(parents=True,exist_ok=True)
    with (sp.library_dir/'.registration.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        return _add_file_result(sp,path,**kwargs)


def add_derivative(sp, path, *, derived_from, origin, by, alt='', now=None) -> dict:
    """Immutable operation occurrence; byte storage remains content-addressed.

    An occurrence ID addresses output bytes + exact parent references + operation
    provenance. It never mutates or inherits an unrelated byte-equal record.
    """
    import hashlib
    parents=sorted(set(derived_from))
    references=[]
    for aid in parents:
        rec=registry.get(sp,aid)
        if licenses.sha256_file(registry.asset_file(sp,rec))!=rec['sha256']:
            raise ValueError('Derivative parent bytes changed')
        references.append({'id':aid,'sha256':rec['sha256']})
    payload={'schema':'derivative_occurrence.v1','output_sha256':licenses.sha256_file(Path(path)),
             'parents':references,'origin':origin}
    canonical=json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
    if len(canonical)>1_000_000: raise ValueError('Derivative occurrence exceeds identity budget')
    identity={'schema':'derivative_occurrence.v1','sha256':hashlib.sha256(canonical).hexdigest()}
    return add_file_result(sp,path,source={'type':'render'},by=by,alt=alt,derived_from=parents,origin=origin,now=now,_occurrence=identity)[0]


def add_file(sp, path, *, source: dict, license_entry: dict | None = None, by: str, tags=(), alt: str = "", derived_from=(),
             ai_generated: bool = False, origin: dict | None = None, now: datetime | None = None, attested: bool = True) -> dict:
    """Copy `path` into `library/assets/<sha[:2]>/<sha><ext>`, log its license, append the registry line, return it.

    Idempotent per bytes: adding the same file again returns the existing record and writes nothing. `source` is the Asset `source` block
    (`{"type": "own|stock|render|generated|import", "provider", "asset_id", "url"}`). `license_entry` is the fields of a license-log entry
    (source, url, license, license_url, license_text, optional attribution); sha256/file/bytes/approved_by are filled in. `own` assets get
    the standard own-footage entry when none is given; `render` assets need none (they are licensed through `derived_from`); everything
    else is refused without one."""
    return add_file_result(sp, path, source=source, license_entry=license_entry, by=by, tags=tags, alt=alt, derived_from=derived_from,
                           ai_generated=ai_generated, origin=origin, now=now, attested=attested)[0]


# ------------------------------------------------------------------------------------------------ human attestation of a license claim

def attestation_events(sp) -> list[dict]:
    """Read only a verified control ledger; a `sig` field alone is not verification."""
    from ..studio import seal
    log=seal.log(sp,'control');verification=log.verify()
    if not verification.ok:raise ValueError('Protected control ledger verification failed: '+'; '.join(verification.problems))
    return [r for r in log.rows() if r.get('sig') and r.get('op') in ('license_attest','license_withdraw')]


def attestation_for(events: list[dict], asset: str, sha: str, entry: str | None) -> str:
    """Legacy grants bind SHA; v2 grants bind asset, SHA, license entry and purpose.

    Once withdrawn, the bytes remain withdrawn until a matching v2 re-attestation.
    A stale re-attestation cannot revive a previous withdrawal or a changed entry.
    """
    state='legacy'
    for event in events:
        if event.get('sha256')!=sha:continue
        if event.get('schema')=='license_attestation.v2':
            if event.get('purpose')!='license-use' or not isinstance(event.get('asset'),str) or not isinstance(event.get('entry'),str):continue
            if event['op']=='license_withdraw':state='withdrawn'
            elif event['op']=='license_attest':
                if event['asset']==asset and event['entry']==entry:state='attested'
                elif state!='withdrawn':state='stale'
        elif event['op']=='license_attest' and state=='legacy':state='legacy_attested'
    return state


def attested_shas(sp) -> set[str]:
    """Current verified attestations, retaining the legacy SHA-only contract."""
    events=attestation_events(sp);log=registry.license_log(sp)
    result=set()
    for rec in registry.read_all(sp).values():
        e=log.find(sha256=rec['sha256'])
        entry=licenses.entry_hash(e) if e else None
        if attestation_for(events,rec['id'],rec['sha256'],entry) in ('legacy_attested','attested'):result.add(rec['sha256'])
    return result


def unattested(sp) -> list[dict]:
    """Registry assets whose license-log entry was claimed through an agent-reachable path and has no human attestation yet."""
    done = attested_shas(sp)
    log = registry.license_log(sp)
    out = []
    for rec in registry.read_all(sp).values():
        if rec.get("derived_from"):
            continue
        e = log.find(sha256=rec["sha256"])
        if e and e.get("attested") is False and rec["sha256"] not in done:
            out.append({"asset": rec["id"], "sha256": rec["sha256"], "file": rec["file"], "kind": rec["kind"], "added_by": rec.get("added_by"),
                        "source": (rec.get("source") or {}).get("type"), "license": e.get("license"), "license_text": e.get("license_text"),
                        "license_url": e.get("license_url"), "origin": rec.get("origin")})
    return sorted(out, key=lambda r: r["asset"])


def attest_license(sp, asset_id: str, by: str, *, now: datetime | None = None) -> dict:
    """Record that a HUMAN looked at this asset's license claim and stands behind it. Library primitive: the human act is
    `ave studio library attest`, which calls `require_human` first."""
    from ..studio import seal
    if not (by or "").strip():
        raise ValueError("by is required: a named human attests every license claim")
    rec = registry.get(sp, asset_id)
    row = {"op": "license_attest", "asset": rec["id"], "sha256": rec["sha256"], "by": by.strip(),
           "at": (now or SystemClock().now()).strftime("%Y-%m-%dT%H:%M:%S%z")}
    return seal.log(sp, "control").append(row)
