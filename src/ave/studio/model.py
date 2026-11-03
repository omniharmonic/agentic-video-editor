"""The data model: schema validation (no new dependency), ids, the Post/Variant helpers and the edit-patch rules (spec 2.2).

`validate(kind, obj)` checks an object against `schemas/<kind>.schema.json` with a small built-in validator (the JSON-Schema subset
the schemas use: type, enum, const, required, properties, additionalProperties, items, pattern, minimum, maximum, minLength,
minItems, oneOf, anyOf, $ref to `#/definitions/..`). It returns a list of problems; empty means valid.
"""

from __future__ import annotations

import copy as _copy
import json
import math
import re
import secrets
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

from .clock import SystemClock

SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"
KINDS = ("post", "campaign", "asset", "account", "feedback", "event", "alert", "studio", "live", "proposal", "schedule_rules")
POST_STAGES = ("idea", "draft", "active", "dropped", "archived")
PLATFORMS = ("youtube", "facebook", "instagram", "threads", "x", "linkedin", "tiktok", "bluesky", "pinterest", "manual")
POST_TYPES = ("feed", "carousel", "story", "reel", "video", "text", "document", "pin", "short")
VARIANT_STATUSES = ("dropped", "published", "publishing", "failed", "missed", "needs_rework", "scheduled", "approved", "in_review", "draft")
TERMINAL_STATUSES = ("dropped", "published", "failed", "missed")

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")


class RevConflict(Exception):
    """A compare-and-swap lost the race. `latest` is the current stored object (the caller shows it, then retries on top of it)."""

    def __init__(self, latest: dict | None, message: str = "stale write"):
        super().__init__(message)
        self.latest = latest


# ------------------------------------------------------------------------------------------------ validator

@lru_cache(maxsize=None)
def schema(kind: str) -> dict:
    if kind not in KINDS:
        raise ValueError(f"unknown schema {kind!r}; known: {', '.join(KINDS)}")
    return json.loads((SCHEMA_DIR / f"{kind}.schema.json").read_text())


def _type_ok(t: str, v: Any) -> bool:
    if t == "null":
        return v is None
    if t == "boolean":
        return isinstance(v, bool)
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
    if t == "string":
        return isinstance(v, str)
    if t == "array":
        return isinstance(v, list)
    if t == "object":
        return isinstance(v, dict)
    raise ValueError(f"unsupported schema type {t!r}")


def _walk(sch: dict, v: Any, path: str, root: dict, out: list[str]) -> None:
    if "$ref" in sch:
        ref = sch["$ref"]
        if not ref.startswith("#/definitions/"):
            raise ValueError(f"unsupported $ref {ref!r}")
        _walk(root["definitions"][ref.rsplit("/", 1)[1]], v, path, root, out)
        return
    if "const" in sch and v != sch["const"]:
        out.append(f"{path}: must be {sch['const']!r}")
        return
    if "enum" in sch and v not in sch["enum"]:
        out.append(f"{path}: {v!r} is not one of {sch['enum']}")
        return
    if "type" in sch:
        types = sch["type"] if isinstance(sch["type"], list) else [sch["type"]]
        if not any(_type_ok(t, v) for t in types):
            out.append(f"{path}: expected {'|'.join(types)}, got {type(v).__name__}")
            return
    for key in ("anyOf", "oneOf"):
        if key in sch:
            hits = []
            for sub in sch[key]:
                trial: list[str] = []
                _walk(sub, v, path, root, trial)
                hits.append(trial)
            ok = sum(1 for t in hits if not t)
            if (key == "anyOf" and ok < 1) or (key == "oneOf" and ok != 1):
                best = min(hits, key=len)
                out.append(f"{path}: matches {ok} of the {len(hits)} allowed shapes ({key}); closest: " + "; ".join(best[:3]))
            return
    if isinstance(v, str):
        if "maxLength" in sch and len(v) > sch["maxLength"]:
            out.append(f"{path}: longer than {sch['maxLength']}")
        if "minLength" in sch and len(v) < sch["minLength"]:
            out.append(f"{path}: shorter than {sch['minLength']}")
        if "pattern" in sch and not re.search(sch["pattern"], v):
            out.append(f"{path}: {v!r} does not match {sch['pattern']}")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if "minimum" in sch and v < sch["minimum"]:
            out.append(f"{path}: {v} is below {sch['minimum']}")
        if "maximum" in sch and v > sch["maximum"]:
            out.append(f"{path}: {v} is above {sch['maximum']}")
    if isinstance(v, list):
        if "maxItems" in sch and len(v) > sch["maxItems"]:
            out.append(f"{path}: exceeds {sch['maxItems']} items")
        if "minItems" in sch and len(v) < sch["minItems"]:
            out.append(f"{path}: needs at least {sch['minItems']} items")
        if "items" in sch:
            for i, x in enumerate(v):
                _walk(sch["items"], x, f"{path}[{i}]", root, out)
    if isinstance(v, dict):
        for r in sch.get("required", ()):
            if r not in v:
                out.append(f"{path}: missing required {r!r}")
        props = sch.get("properties", {})
        for k, x in v.items():
            if k in props:
                _walk(props[k], x, f"{path}.{k}", root, out)
            elif isinstance(sch.get("additionalProperties"), dict):
                _walk(sch["additionalProperties"], x, f"{path}.{k}", root, out)
            elif sch.get("additionalProperties") is False:
                out.append(f"{path}: unexpected key {k!r}")


def validate(kind: str, obj: Any) -> list[str]:
    """Problems with `obj` against the `kind` schema (see KINDS). Empty list = valid. `kind="variant"` validates one post variant."""
    if kind == "variant":
        root = schema("post")
        out: list[str] = []
        _walk(root["definitions"]["variant"], obj, "variant", root, out)
        return out
    root = schema(kind)
    out = []
    _walk(root, obj, kind, root, out)
    if kind == "post" and isinstance(obj, dict) and "production" in obj:
        from .production import validate_shape
        try:
            validate_shape(obj["production"])
        except (ValueError, TypeError) as e:
            out.append(str(e))
    return out


def require_valid(kind: str, obj: Any) -> None:
    problems = validate(kind, obj)
    if problems:
        raise ValueError(f"invalid {kind}: " + "; ".join(problems[:8]))


# ------------------------------------------------------------------------------------------------ ids

def check_id(s: str, what: str = "id") -> str:
    """A safe file-name component (post ids become `posts/<id>.json`)."""
    if not isinstance(s, str) or not _ID_RE.match(s) or ".." in s:
        raise ValueError(f"bad {what} {s!r}")
    return s


def new_post_id(now: datetime | None = None) -> str:
    return f"p_{(now or SystemClock().now()).strftime('%Y%m%d')}_{secrets.token_hex(2)}"


def new_variant_id() -> str:
    return f"v_{secrets.token_hex(3)}"


def asset_id(sha256: str) -> str:
    return "a_" + sha256[:12]


# ------------------------------------------------------------------------------------------------ Post / Variant

def new_post(*, idea: str | None = None, copy: Mapping | None = None, media: Iterable[Mapping] = (), campaign: str | None = None,
             by: str = "claude", now: datetime | None = None, post_id: str | None = None, origin: Mapping | None = None,
             reason: str = "", confidence: float | None = None) -> dict:
    """A new Post at rev 1. Stage is derived from what it has: idea (text only) -> draft (copy or media) -> active (see `stage_for`)."""
    t = (now or SystemClock().now()).strftime("%Y-%m-%dT%H:%M:%S%z")
    post: dict = {"v": 1, "kind": "studio.post", "id": post_id or new_post_id(now), "rev": 1, "stage": "idea", "campaign": campaign,
                  "copy": {"base": "", "link": None, "first_comment": None, "hashtags": [], "cta": None, **(copy or {})},
                  "media": [dict(m) for m in media], "variants": [], "disclosure": {"ai_generated": False},
                  "created": t, "updated": t, "by": by}
    if idea:
        post["idea"] = {"text": idea, "origin": dict(origin) if origin else None, "reason": reason, "confidence": confidence}
    post["stage"] = stage_for(post)
    return post


def new_variant(*, account: str, platform: str, post_type: str, copy: Mapping | None = None,
                media: Iterable[Mapping] | None = None, variant_id: str | None = None) -> dict:
    return {"id": variant_id or new_variant_id(), "account": account, "platform": platform, "post_type": post_type,
            "copy": {"caption": None, "first_comment": None, "hashtags": None, **(copy or {})},
            "media": None if media is None else [dict(m) for m in media],
            "schedule": None, "queue": None, "queue_sha": None, "flags": [], "attempts": [], "result": None, "dropped": False}


def stage_for(post: Mapping) -> str:
    """Post stage rule (2.8): idea (text only) -> draft (copy+media, no variants) -> active (>= 1 variant); archived when every variant
    is terminal; dropped stays dropped. Terminal is judged from `variant.dropped` / `variant.result` here (the cheap, stored facts);
    the live status table is state.derive."""
    if post.get("stage") == "dropped":
        return "dropped"
    variants = post.get("variants") or []
    if variants:
        if all(v.get("dropped") or (v.get("result") or {}).get("state") in ("published", "failed") for v in variants):
            return "archived"
        return "active"
    copy = post.get("copy") or {}
    if post.get("media") or (copy.get("base") or "").strip():
        return "draft"
    return "idea"


def find_variant(post: Mapping, variant_id: str) -> dict:
    for v in post.get("variants", []):
        if v["id"] == variant_id:
            return v
    raise KeyError(f"post {post.get('id')} has no variant {variant_id!r}")


def effective_copy(post: Mapping, variant: Mapping) -> dict:
    """Copy for a variant = post.copy overlaid with the variant's non-null fields (spec 2.2)."""
    pc, vc = post.get("copy") or {}, variant.get("copy") or {}
    return {"caption": vc["caption"] if vc.get("caption") is not None else pc.get("base", ""),
            "first_comment": vc["first_comment"] if vc.get("first_comment") is not None else pc.get("first_comment"),
            "hashtags": list(vc["hashtags"] if vc.get("hashtags") is not None else pc.get("hashtags") or []),
            "link": pc.get("link"), "cta": pc.get("cta")}


def effective_media(post: Mapping, variant: Mapping) -> list[dict]:
    return list(variant["media"] if variant.get("media") is not None else post.get("media") or [])


def media_asset_ids(post: Mapping) -> list[str]:
    """Every asset id the post references (post media + every variant's own media), in first-seen order."""
    seen: dict[str, None] = {}
    for m in post.get("media") or []:
        seen.setdefault(m["asset"])
    for v in post.get("variants") or []:
        for m in v.get("media") or []:
            seen.setdefault(m["asset"])
    return list(seen)


def ai_generated(assets: Mapping[str, Mapping], asset_ids: Iterable[str]) -> bool:
    """DERIVED disclosure flag (2.2): true if any asset, or any asset in its `derived_from` closure, is `ai_generated`.
    `assets` maps id -> registry line. An unknown id counts as not AI (coverage/verify report unknown ids separately)."""
    todo, seen = list(asset_ids), set()
    while todo:
        a = todo.pop()
        if a in seen:
            continue
        seen.add(a)
        rec = assets.get(a)
        if rec:
            if rec.get("ai_generated"):
                return True
            todo.extend(rec.get("derived_from") or [])
    return False


# Fields no `edit` patch may touch: identity, the revision counter, and everything the queue/scheduler/move code owns.
PATCH_DENY_TOP = {"v", "kind", "id", "rev", "created"}
PATCH_DENY_VARIANT = {"id", "queue", "queue_sha", "attempts", "result", "schedule"}


def apply_patch(post: dict, patch: Mapping[str, Any]) -> dict:
    """Apply a patch of dotted paths to a COPY of `post` and return it, e.g. {"copy.base": "..", "variants.v_ig.copy.caption": "..",
    "media.0.alt": ".."}. A list segment is an `id` (variants) or an integer index. Setting a value to None is allowed (null = inherit).
    Identity, `rev` and the queue/schedule/result fields are refused; the schedule changes through `move`, the queue through sync."""
    out = _copy.deepcopy(post)
    for path, value in patch.items():
        parts = path.split(".")
        if parts[0] == "production":
            raise ValueError("Use the typed production update operation")
        if parts[0] in PATCH_DENY_TOP:
            raise ValueError(f"cannot edit {parts[0]!r}")
        if parts[0] == "variants" and len(parts) >= 3 and parts[2] in PATCH_DENY_VARIANT:
            raise ValueError(f"cannot edit variants.*.{parts[2]} by hand (it is set by move / queue sync / the scheduler)")
        if parts[0] == "variants" and len(parts) <= 2:
            raise ValueError("replace fields of a variant, not the variant (use variant add / drop)")
        cur: Any = out
        for i, seg in enumerate(parts[:-1]):
            cur = _step(cur, seg, path)
        _set(cur, parts[-1], value, path)
    return out


def _step(cur: Any, seg: str, path: str) -> Any:
    if isinstance(cur, dict):
        if seg not in cur or cur[seg] is None:
            if seg in ("copy", "disclosure", "crop"):
                cur[seg] = {}
            else:
                raise KeyError(f"patch path {path!r}: no {seg!r}")
        return cur[seg]
    if isinstance(cur, list):
        for x in cur:
            if isinstance(x, dict) and x.get("id") == seg:
                return x
        if seg.isdigit() and int(seg) < len(cur):
            return cur[int(seg)]
    raise KeyError(f"patch path {path!r}: nothing at {seg!r}")


def _set(cur: Any, seg: str, value: Any, path: str) -> None:
    if isinstance(cur, dict):
        cur[seg] = value
    elif isinstance(cur, list) and seg.isdigit() and int(seg) < len(cur):
        cur[int(seg)] = value
    else:
        raise KeyError(f"patch path {path!r}: cannot set {seg!r}")
