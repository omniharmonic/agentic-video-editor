"""Offline editorial exchange, deliberately separate from publishing authority.

Bundles contain projected drafts and optional, hash-checked library media. They never
contain operational queue fields, signed logs, licenses, credentials or raw footage.
Each peer has a retained common base; edits never resolve by timestamp or overwrite
on a revision race. A checksum detects corruption, not an untrusted sender: exchange
bundles over an authenticated channel with people you trust.
"""
from __future__ import annotations

import base64
import binascii
import copy
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

from .publish.safety import sha256_file
from .studio import model, store
from .studio.events import Actor

MAX_BUNDLE_BYTES = 32 * 1024 * 1024
MAX_MEDIA_BYTES = 20 * 1024 * 1024
MAX_STATE_BYTES = 32 * 1024 * 1024
MAX_POSTS = 500
MAX_DEPTH = 20
ACTOR = Actor("agent", "collaboration")
_HEX = re.compile(r"^[0-9a-f]{64}$")
_UUID = re.compile(r"^[0-9a-f]{32}$")
_MEDIA_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mov", ".webm", ".mp3", ".wav", ".m4a", ".pdf"}
_COPY = {"base", "link", "first_comment", "hashtags", "cta"}
_VCOPY = {"caption", "first_comment", "hashtags"}
_CROP = {"x", "y", "w", "h", "focal_x", "focal_y", "scale", "aspect"}


def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _sha(obj) -> str:
    return hashlib.sha256(_canonical(obj)).hexdigest()


def _no_symlink(path: Path) -> Path:
    path = Path(os.path.abspath(path.expanduser()))
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError(f"symlink paths are not accepted: {path}")
    return path


def _inside(sp, path: Path) -> Path:
    p = _no_symlink(path)
    try:
        p.relative_to(sp.root)
    except ValueError:
        raise ValueError("collaboration storage must stay inside the studio") from None
    if any(p == r.resolve() or r.resolve() in p.parents for r in sp.raw_dirs):
        raise ValueError("collaboration never writes raw folders")
    return p


def _home(sp) -> Path:
    return _inside(sp, sp.work / "collaboration")


def _atomic(path: Path, obj) -> None:
    _no_symlink(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".collab-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(_canonical(obj) + b"\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def _pairs(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"duplicate JSON key: {k}")
        out[k] = v
    return out


def _bounded(obj, depth=0):
    if depth > MAX_DEPTH:
        raise ValueError("JSON nesting is too deep")
    if isinstance(obj, str) and len(obj) > MAX_BUNDLE_BYTES:
        raise ValueError("JSON string is too large")
    if isinstance(obj, float) and not math.isfinite(obj):
        raise ValueError("non-finite numbers are not accepted")
    if isinstance(obj, dict):
        for k, v in obj.items():
            _bounded(k, depth + 1)
            _bounded(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _bounded(v, depth + 1)


def _read(path: Path, *, limit: int | None = None):
    limit = MAX_BUNDLE_BYTES if limit is None else limit
    path = _no_symlink(path)
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError("bundle/state size must be at most 32 MiB and it must be a regular file")
    with path.open("rb") as f:
        raw = f.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("bundle size exceeds the limit")
    try:
        obj = json.loads(raw, object_pairs_hook=_pairs)
    except (RecursionError, UnicodeError, json.JSONDecodeError) as e:
        raise ValueError("invalid bundle JSON") from e
    _bounded(obj)
    return obj


def _state(sp, *, create=False):
    path = _home(sp) / "state.json"
    if path.exists():
        state = _read(path, limit=MAX_STATE_BYTES)
        if not isinstance(state, dict) or state.get("v") != 1 or not _UUID.fullmatch(str(state.get("studio", ""))):
            raise ValueError("invalid local collaboration state; preserve it and investigate")
        return state
    return {"v": 1, "studio": uuid.uuid4().hex if create else None, "peers": {}, "receipts": {}}


def _check_state_capacity(state):
    # Check before publishing an export or mutating any post. A readable state is
    # more valuable than accepting an exchange that cannot later be resumed.
    if len(_canonical(state)) + 1 > MAX_STATE_BYTES:
        raise ValueError("collaboration state capacity reached; preserve this studio and use a separate studio for further exchanges")


def _prospective_import_state(state, connection, bundle, peer):
    candidate = copy.deepcopy(state)
    future = copy.deepcopy(connection)
    future["studio"] = bundle["source"]
    for entry in bundle["entries"]:
        base = {"sha": entry["sha"], "content": entry["content"]}
        old = future["base"].get(entry["id"])
        # Actual reconciliation may retain the old base or replace it. Reserve
        # the larger representation before the first post write, including races.
        if old is None or len(_canonical(base)) > len(_canonical(old)):
            future["base"][entry["id"]] = base
    candidate["peers"][peer] = future
    candidate["receipts"][bundle["id"]] = {"peer": peer, "conflicts": len(bundle["entries"])}
    return candidate


def _stage_media(sp, home, rec, data):
    dest = _inside(sp, home / "media" / (rec["sha256"] + rec["ext"]))
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and sha256_file(dest) == rec["sha256"]:
        return dest
    fd, tmp = tempfile.mkstemp(prefix=".collab-media-", dir=dest.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        # The final name becomes visible only after a complete durable write.
        # Preserve a corrupt/legacy partial staged file rather than destroying it.
        if dest.exists():
            recovered = _inside(sp, dest.parent / "recovered" / (dest.name + "." + uuid.uuid4().hex + ".partial"))
            recovered.parent.mkdir(parents=True, exist_ok=True)
            os.replace(dest, recovered)
        os.replace(tmp, dest)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return dest


def _replayed_conflicts(sp, home, bundle, expected_count):
    unresolved, found = [], 0
    for entry in bundle["entries"]:
        path = _inside(sp, home / "conflicts" / f"{bundle['id'][:16]}-{entry['id']}.json")
        if not path.exists():
            continue
        obj = _read(path)
        if obj.get("bundle") != bundle["id"]:
            raise ValueError("conflict record does not match its bundle; preserve it and investigate")
        found += 1
        if not obj.get("resolution"):
            unresolved.append({"id": obj["id"], "post": obj["post"], "reason": obj["reason"], "file": str(path)})
    if found < expected_count:
        unresolved.append({"post": None, "reason": "a recorded conflict snapshot is missing; preserve state and investigate"})
    return unresolved


@contextmanager
def _locked(sp):
    sp.require()
    home = _home(sp)
    home.mkdir(parents=True, exist_ok=True)
    lock = _inside(sp, home / ".lock")
    with lock.open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield home


def _project_media(items):
    if items is None:
        return None
    out = []
    for m in items:
        item = {k: copy.deepcopy(v) for k, v in m.items() if k in {"slot", "asset", "role", "alt"}}
        crop = m.get("crop")
        if isinstance(crop, dict):
            item["crop"] = {k: v for k, v in crop.items() if k in _CROP and isinstance(v, (int, float)) and not isinstance(v, bool)}
        out.append(item)
    return out


def editorial(post: dict) -> dict:
    """Strict projection, also the comparison domain. No operation/identity fields."""
    out = {"copy": {k: copy.deepcopy(v) for k, v in (post.get("copy") or {}).items() if k in _COPY},
           "media": _project_media(post.get("media") or []), "variants": []}
    if post.get("idea") is not None:
        out["idea"] = {k: copy.deepcopy(v) for k, v in post["idea"].items() if k in {"text", "reason", "confidence"}}
    for key in ("campaign", "brand"):
        if key in post:
            out[key] = post[key]
    if post.get("disclosure") is not None:
        out["disclosure"] = {"ai_generated": bool(post["disclosure"].get("ai_generated", False))}
    for v in post.get("variants") or []:
        out["variants"].append({"id": v["id"], "account": v["account"], "platform": v["platform"], "post_type": v["post_type"],
                                "copy": {k: copy.deepcopy(x) for k, x in (v.get("copy") or {}).items() if k in _VCOPY},
                                "media": _project_media(v.get("media"))})
    if post.get("production") is not None:
        from .studio.production import portable
        out["production"] = portable(post["production"])
    _bounded(out)
    return out


def _draft(pid, content, current=None):
    # Imported drafts carry no queue links, schedule, send result or claimed approval.
    out = copy.deepcopy(current) if current is not None else model.new_post(post_id=pid, by="collaboration")
    for key in ("idea", "brand", "campaign"):
        out.pop(key, None)
    out.update(copy.deepcopy(content))
    if "production" in content and current and current.get("production"):
        # Remote IDs are never interpreted as local people/approval authority.
        # Existing local assignments remain; new peer work is unassigned.
        old = copy.deepcopy(current["production"])
        old.update(content["production"])
        for task in old.get("checklist", []):
            prior = next((t for t in current["production"].get("checklist", []) if t["id"] == task["id"]), None)
            if prior: task["owner"] = prior.get("owner")
        if old.get("status") == "ready" and any(not t["done"] for t in old.get("checklist", [])):
            old["status"] = "blocked"
            old["blocked_reason"] = "Imported task changes need production review"
        out["production"] = old
    if current and "idea" in out and (current.get("idea") or {}).get("origin") is not None:
        out["idea"]["origin"] = copy.deepcopy(current["idea"]["origin"])
    out["stage"] = "draft"
    out["variants"] = []
    for v in content["variants"]:
        variant = model.new_variant(account=v["account"], platform=v["platform"], post_type=v["post_type"],
                                    variant_id=v["id"], copy=v["copy"], media=v["media"])
        variant["flags"] = ["needs_review"]
        out["variants"].append(variant)
    model.require_valid("post", out)
    return out


def _valid_content(pid, content):
    if not isinstance(content, dict):
        raise ValueError("entry content must be an editorial object")
    try:
        if editorial(content) != content:
            raise ValueError("bundle contains fields outside the editorial allowlist")
        _draft(pid, content)
        ids = [v["id"] for v in content["variants"]]
        for v in content["variants"]:
            model.check_id(v["account"], "account")
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate variant id")
    except (KeyError, TypeError, AttributeError) as e:
        raise ValueError("malformed editorial content") from e


def _post_paths(sp, pid):
    # Existing store APIs are trusted only after these filesystem selectors are checked.
    for p in (sp.posts_dir / f"{pid}.json", sp.posts_dir / f"{pid}.json.tmp", sp.posts_dir / f"{pid}.lock",
              sp.ledger_dir / "events.jsonl", sp.ledger_dir / ".lock"):
        _inside(sp, p)


def _load_posts(sp, ids=None):
    _inside(sp, sp.posts_dir)
    paths = sorted(p for p in sp.posts_dir.glob("*.json") if not p.name.startswith("._")) if ids is None else [store.post_path(sp, x) for x in ids]
    if len(paths) > MAX_POSTS:
        raise ValueError(f"select at most {MAX_POSTS} posts per bundle")
    result = []
    for p in paths:
        _inside(sp, p)
        obj = _read(p)
        pid = model.check_id(obj.get("id"), "post id")
        if p.name != f"{pid}.json":
            raise ValueError("post id and filename disagree")
        _post_paths(sp, pid)
        content = editorial(obj)
        _valid_content(pid, content)
        result.append((pid, content))
    return result


def _asset_blobs(sp, contents):
    from .library import registry
    ids = sorted({m["asset"] for c in contents for m in (c["media"] + [m for v in c["variants"] for m in v["media"] or []])})
    _inside(sp, sp.library_dir / "assets.jsonl")
    known = registry.read_all(sp)
    out, total = [], 0
    for aid in ids:
        rec = known.get(aid)
        if rec is None:
            continue
        sha, ext = rec.get("sha256"), rec.get("ext")
        if not isinstance(sha, str) or not _HEX.fullmatch(sha) or ext not in _MEDIA_EXTS or model.asset_id(sha) != aid:
            raise ValueError(f"invalid library record for {aid}")
        path = _inside(sp, sp.root / rec["file"])
        try:
            path.relative_to(sp.library_dir / "assets")
        except ValueError:
            raise ValueError("only registered library/assets media may be bundled") from None
        if not path.is_file() or path.stat().st_size + total > MAX_MEDIA_BYTES:
            raise ValueError("bundled media must be present and at most 20 MiB total; transfer larger media separately")
        data = path.read_bytes()
        total += len(data)
        if total > MAX_MEDIA_BYTES or hashlib.sha256(data).hexdigest() != sha:
            raise ValueError(f"library media hash/size mismatch: {aid}")
        out.append({"id": aid, "sha256": sha, "ext": ext, "data": base64.b64encode(data).decode("ascii")})
    return out


def export_bundle(sp, output: str | Path, *, peer: str, post_ids: list[str] | None = None, include_media=False,
                  version=1, visuals=(), media_ids=()) -> dict:
    """Create an immutable bundle outside the live studio. Initial export retains the base for a later reply."""
    peer = model.check_id(peer, "peer name")
    output = _no_symlink(Path(output))
    if output == sp.root or sp.root in output.parents:
        raise ValueError("export outside the live studio, into a deliberately shared exchange folder")
    if output.exists():
        raise FileExistsError(f"bundle already exists: {output}")
    with _locked(sp) as home:
        state = _state(sp, create=True)
        connection = state["peers"].setdefault(peer, {"studio": None, "base": {}})
        rows = _load_posts(sp, post_ids)
        entries = []
        for pid, content in rows:
            base = connection["base"].get(pid)
            if base is None:
                base = {"sha": _sha(content), "content": content}
                connection["base"][pid] = base
            entries.append({"id": pid, "base": base, "content": content, "sha": _sha(content)})
        if version not in (1, 2) or type(version) is not int:
            raise ValueError("Unsupported bundle version")
        bundle = {"v": version, "kind": "ave.editorial.bundle", "source": state["studio"], "target": connection["studio"],
                  "entries": entries, "assets": _asset_blobs(sp, [c for _, c in rows]) if include_media and version == 1 else []}
        if version == 2:
            from . import collab_v2
            selected_media = collab_v2.referenced(entries) if include_media else media_ids
            bundle.update(collab_v2.extension(sp, entries, state['studio'], visuals=visuals, media_ids=selected_media))
        bundle["id"] = _sha(bundle)
        _validate_bundle(bundle)
        raw = _canonical(bundle) + b"\n"
        if len(raw) > MAX_BUNDLE_BYTES:
            raise ValueError("bundle size exceeds 32 MiB; select fewer posts or omit media")
        _check_state_capacity(state)
        # Persist identity and offered bases BEFORE any bundle can be observed.
        # An output failure can then be retried without changing this studio ID.
        _atomic(home / "state.json", state)
        output.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive create: even an identical export never overwrites a previous exchange.
        with output.open("xb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        return {"ok": True, "bundle": str(output), "id": bundle["id"], "source": state["studio"], "peer": peer,
                "posts": len(entries), "media": len(bundle["assets"]), "bytes": len(raw),
                "note": "Editorial drafts only. Licenses, approvals, accounts, schedules and publishing state are not transferred."}


def _validate_bundle(bundle):
    if isinstance(bundle, dict) and type(bundle.get('v')) is int and bundle['v'] == 2:
        from .collab_v2 import validate
        return validate(bundle)
    keys = {"v", "kind", "id", "source", "target", "entries", "assets"}
    if not isinstance(bundle, dict) or set(bundle) != keys or type(bundle.get("v")) is not int or bundle.get("v") != 1 or bundle.get("kind") != "ave.editorial.bundle":
        raise ValueError("not a supported editorial bundle")
    if not isinstance(bundle["id"], str) or not _HEX.fullmatch(bundle["id"]) or _sha({k: v for k, v in bundle.items() if k != "id"}) != bundle["id"]:
        raise ValueError("bundle checksum mismatch")
    if not isinstance(bundle["source"], str) or not _UUID.fullmatch(bundle["source"]):
        raise ValueError("invalid source studio identity")
    if bundle["target"] is not None and (not isinstance(bundle["target"], str) or not _UUID.fullmatch(bundle["target"])):
        raise ValueError("invalid target studio identity")
    entries = bundle["entries"]
    if not isinstance(entries, list) or len(entries) > MAX_POSTS:
        raise ValueError(f"bundle must contain at most {MAX_POSTS} posts")
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"id", "base", "content", "sha"}:
            raise ValueError("malformed bundle entry")
        pid = model.check_id(entry["id"], "post id")
        if pid in seen:
            raise ValueError("duplicate post id")
        seen.add(pid)
        _valid_content(pid, entry["content"])
        base = entry["base"]
        if not isinstance(base, dict) or set(base) != {"sha", "content"}:
            raise ValueError("invalid base snapshot")
        _valid_content(pid, base["content"])
        if entry["sha"] != _sha(entry["content"]) or base["sha"] != _sha(base["content"]):
            raise ValueError("editorial hash mismatch")
    if not isinstance(bundle["assets"], list) or len(bundle["assets"]) > MAX_POSTS * 20:
        raise ValueError("invalid media list")
    blobs, total = [], 0
    referenced = {m["asset"] for e in entries for m in e["content"]["media"] + [m for v in e["content"]["variants"] for m in v["media"] or []]}
    seen_assets = set()
    for rec in bundle["assets"]:
        if not isinstance(rec, dict) or set(rec) != {"id", "sha256", "ext", "data"}:
            raise ValueError("invalid bundled media")
        sha = rec["sha256"]
        if (not isinstance(sha, str) or not _HEX.fullmatch(sha) or rec["id"] != model.asset_id(sha) or
                rec["id"] not in referenced or rec["id"] in seen_assets or not isinstance(rec["ext"], str) or rec["ext"] not in _MEDIA_EXTS or not isinstance(rec["data"], str)):
            raise ValueError("invalid media hash, extension or reference")
        try:
            data = base64.b64decode(rec["data"], validate=True)
        except (ValueError, binascii.Error) as e:
            raise ValueError("invalid media encoding") from e
        total += len(data)
        if total > MAX_MEDIA_BYTES or hashlib.sha256(data).hexdigest() != sha:
            raise ValueError("bundled media hash/size mismatch")
        seen_assets.add(rec["id"])
        blobs.append((rec, data))
    return blobs, referenced


def _operational(post):
    return post.get("stage") in ("archived", "dropped") or any(v.get("queue") or v.get("schedule") or v.get("attempts") or v.get("result") or v.get("dropped") for v in post.get("variants") or [])


def _write_conflict(sp, home, bundle, entry, current, reason):
    name = f"{bundle['id'][:16]}-{entry['id']}.json"
    path = _inside(sp, home / "conflicts" / name)
    if not path.exists():
        _atomic(path, {"v": 1, "id": name.removesuffix(".json"), "bundle": bundle["id"], "source": bundle["source"],
                       "post": entry["id"], "reason": reason, "base": entry["base"]["content"],
                       "local": editorial(current) if current else None, "local_rev": (current or {}).get("rev"),
                       "incoming": entry["content"], "resolution": None})
    return str(path)


def import_bundle(sp, path: str | Path, *, peer: str, apply=False) -> dict:
    """Preview by default. Apply only unambiguous editorial updates; preserve each conflicting version."""
    peer = model.check_id(peer, "peer name")
    bundle = _read(Path(path))
    try:
        blobs, referenced = _validate_bundle(bundle)     # validate the WHOLE input before any mutations
    except (TypeError, KeyError, AttributeError) as e:
        raise ValueError("malformed editorial bundle") from e
    sp.require()
    # Preview stays read-only, including a pristine collaboration directory.
    if apply:
        with _locked(sp) as home:
            return _import(sp, home, bundle, blobs, referenced, peer, True)
    return _import(sp, _home(sp), bundle, blobs, referenced, peer, False)


def _import(sp, home, bundle, blobs, referenced, peer, apply):
    state = _state(sp, create=apply)
    connection = state["peers"].get(peer, {"studio": None, "base": {}})
    if connection["studio"] not in (None, bundle["source"]):
        raise ValueError("peer studio identity changed; use a new peer name and investigate")
    if state["studio"] == bundle["source"]:
        raise ValueError("cannot import a bundle from this studio's own identity")
    if bundle["target"] is not None and bundle["target"] != state["studio"]:
        raise ValueError("bundle target identity is a different studio")
    result = {"ok": True, "id": bundle["id"], "peer": peer, "applied": apply, "replayed": False,
              "created": [], "updated": [], "unchanged": [], "conflicts": [], "staged_media": []}
    _inside(sp, sp.library_dir / "assets.jsonl")
    result["missing_assets"] = sorted(referenced - set(store.read_assets(sp)))
    if bundle['v'] == 2:
        from . import collab_v2
        destinations = collab_v2.check_local(sp, bundle)
        result['dependencies'] = collab_v2.dependencies(sp, bundle)
        if apply:
            # Stage immutable identity first. A later interrupted editorial apply
            # can safely retry; these receipts never confer library authority.
            for destination, value in destinations:
                if not destination.exists(): _atomic(destination, value)
    if bundle["id"] in state["receipts"]:
        result["replayed"] = True
        result["conflicts"] = _replayed_conflicts(sp, home, bundle, state["receipts"][bundle["id"]].get("conflicts", 0))
        result["ok"] = not result["conflicts"]
        return result
    if apply:
        _check_state_capacity(_prospective_import_state(state, connection, bundle, peer))
    # Validate every destination before the first write.
    for entry in bundle["entries"]:
        _post_paths(sp, entry["id"])
    for rec, _ in blobs:
        _inside(sp, home / "media" / (rec["sha256"] + rec["ext"]))
    for entry in bundle["entries"]:
        pid, incoming, base = entry["id"], entry["content"], entry["base"]
        try:
            current = store.load_post(sp, pid)
        except FileNotFoundError:
            current = None
        cur = editorial(current) if current else None
        same = cur is not None and _sha(cur) == entry["sha"]
        incoming_unchanged = entry["sha"] == base["sha"]
        known_base = connection["base"].get(pid)
        safe_reply = known_base is not None and known_base["sha"] == base["sha"] and bundle["target"] == state["studio"]
        reason = None
        if same:
            result["unchanged"].append(pid)
            connection["base"][pid] = {"sha": entry["sha"], "content": incoming}
            continue
        if current is not None and incoming_unchanged and (connection["studio"] is not None or safe_reply):
            result["unchanged"].append(pid)
            continue      # local edits survive an unchanged remote branch
        if current is not None:
            if _operational(current):
                reason = "local post has publishing/scheduling history; reconcile as a separate draft"
            elif not connection["studio"] and not safe_reply:
                reason = "post id exists before a common peer base was established"
            elif _sha(cur) != base["sha"]:
                reason = "both creators changed this post since the common base"
        if reason is None:
            key = "updated" if current is not None else "created"
            if apply:
                try:
                    store.save_post(sp, _draft(pid, incoming, current), expected_rev=current["rev"] if current else None,
                                    actor=ACTOR, op="collaboration.import", detail={"bundle": bundle["id"], "peer": peer})
                except model.RevConflict as e:
                    current, reason = e.latest, "local revision changed during import"
            if reason is None:
                result[key].append(pid)
                connection["base"][pid] = {"sha": entry["sha"], "content": incoming}
                continue
        record = {"post": pid, "reason": reason}
        if apply:
            record["file"] = _write_conflict(sp, home, bundle, entry, current, reason)
            record["id"] = Path(record["file"]).stem
        result["conflicts"].append(record)
    if apply:
        for rec, data in blobs:
            dest = _stage_media(sp, home, rec, data)
            result["staged_media"].append({"asset": rec["id"], "file": str(dest), "licensed": False})
        connection["studio"] = bundle["source"]
        state["peers"][peer] = connection
        state["receipts"][bundle["id"]] = {"peer": peer, "conflicts": len(result["conflicts"])}
        _check_state_capacity(state)
        _atomic(home / "state.json", state)
    result["ok"] = not result["conflicts"]
    result["next"] = "Review imported drafts. Register missing media with its actual license; a human must attest locally. Queue sync and publishing approval stay local."
    return result


def status(sp) -> dict:
    sp.require()
    state = _state(sp)
    conflicts = []
    directory = _home(sp) / "conflicts"
    _inside(sp, directory)
    for p in sorted(directory.glob("*.json")) if directory.exists() else []:
        obj = _read(p)
        if not obj.get("resolution"):
            conflicts.append({"id": obj["id"], "post": obj["post"], "reason": obj["reason"], "file": str(p)})
    return {"ok": True, "studio": state["studio"], "peers": [{"name": name, "studio": row["studio"], "bases": len(row["base"])}
             for name, row in sorted(state["peers"].items())], "imports": len(state["receipts"]), "conflicts": conflicts,
            "mode": "offline editorial exchange; publishing authority stays local"}


def resolve_conflict(sp, conflict_id: str, *, take: str, expected_rev: int, apply=False) -> dict:
    """Explicit editorial reconciliation. `take=local` also acknowledges a manual merge made in the editor."""
    model.check_id(conflict_id, "conflict id")
    if take not in ("local", "incoming") or not isinstance(expected_rev, int) or isinstance(expected_rev, bool) or expected_rev < 1:
        raise ValueError("choose local or incoming and provide the current positive expected revision")
    with _locked(sp) as home:
        path = _inside(sp, home / "conflicts" / (conflict_id + ".json"))
        obj = _read(path)
        if obj.get("resolution"):
            return {"ok": True, "resolved": True, "unchanged": True, "resolution": obj["resolution"]}
        pid = model.check_id(obj["post"], "post id")
        _post_paths(sp, pid)
        current = store.load_post(sp, pid)
        if current["rev"] != expected_rev:
            raise model.RevConflict(current)
        if take == "incoming" and _operational(current):
            raise ValueError("publishing/scheduling history must be retained; copy the incoming words into a new draft")
        _valid_content(pid, obj["incoming"])
        if apply:
            if take == "incoming":
                current = store.save_post(sp, _draft(pid, obj["incoming"], current), expected_rev=expected_rev,
                                          actor=ACTOR, op="collaboration.resolve", detail={"conflict": conflict_id, "take": take})
            obj["resolution"] = {"take": take, "rev": current["rev"], "sha": _sha(editorial(current))}
            _atomic(path, obj)
        return {"ok": True, "resolved": apply, "post": pid, "take": take, "rev": current["rev"]}


TAILSCALE_APP = Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")


def tailscale_status() -> dict:
    """Read-only inventory, never network/ACL/auth mutation. Do not expose peer IPs in diagnostics."""
    executable = shutil.which("tailscale") or (str(TAILSCALE_APP) if TAILSCALE_APP.is_file() else None)     # the standalone app has no shell PATH
    if executable is None:
        return {"installed": False, "connected": False, "next": "Install Tailscale on both computers, then follow docs/collaboration.md. No network settings were changed."}
    try:
        run = subprocess.run([executable, "status", "--json"], capture_output=True, timeout=5, check=False, env={**os.environ, "TAILSCALE_BE_CLI": "1"})
        if len(run.stdout) > 2 * 1024 * 1024:
            raise ValueError("oversized Tailscale status")
        obj = json.loads(run.stdout)
        connected = run.returncode == 0 and obj.get("BackendState") == "Running"
        return {"installed": True, "connected": connected, "state": obj.get("BackendState"),
                "online_peers": sum(1 for p in (obj.get("Peer") or {}).values() if p.get("Online")),
                "next": "Exchange bundle files through an explicitly shared folder or approved Tailscale SSH. Never expose the approval UI or synchronize live studio folders."}
    except (OSError, ValueError, TypeError, AttributeError, subprocess.TimeoutExpired):
        return {"installed": True, "connected": False, "error": "Tailscale status could not be read; open Tailscale to check your connection."}
