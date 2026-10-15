"""ContentDoc v1: `content/<id>.vN.json`, the contract between planning, rendering, review and publishing.

It parallels EditDoc (`ave.editdoc`): one JSON document, versioned, validated, NEVER overwritten (every change is a new
version), so a decision or a render can always be traced back. Shape (see docs/content.md):

    {"version": 1, "kind": "content", "id": "main", "episode": "ep012",
     "brand": {"tokens": "motion-kit/brand/tokens.json", "tokens_sha256": "..."},
     "sources": {"q1": {"type": "clip_quote", ...}, "w1": {"type": "markdown", ...}, "ep": {"type": "episode_meta", ...}},
     "items": [{"id", "template", "style", "format", "renderer", "slots", "source", "platforms", "caption", "alt_text",
                "outputs": [{"platform", "w", "h", "file", "sha256", "alt_text"}],
                "review": {"status": "draft|approved|rejected", "critic": {...}, "by", "at", "note"},
                "provenance": {...}, "license": {"assets": [], "ai_generated": false}, "reason", "confidence"}],
     "notes": ""}

`validate` never raises and reports every problem. Words are the risk in this pipeline, so every slot a template marks
`x-verbatim` is checked word for word against the item's source text (the same rule `ave clips propose` applies to hook quotes).
"""

from __future__ import annotations

import copy
import hashlib
import re
import time
from pathlib import Path

from .. import motion
from ..episode import Episode, read_json, write_json
from . import registry as R
from . import sources as S

VERSION = 1
KIND = "content"
SOURCE_TYPES = ("clip_quote", "markdown", "episode_meta")
REVIEW_STATUSES = ("draft", "approved", "rejected")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_V = re.compile(r"^(?P<id>[A-Za-z0-9][A-Za-z0-9._-]*)\.v(?P<n>\d+)\.json$")


def content_dir(ep: Episode) -> Path:
    """The content working area: `<work>/content/` in whatever layout the episode uses. Absent = stage not started."""
    return ep.work / "content"


def tokens_sha() -> str:
    return hashlib.sha256((motion.KIT / "brand" / "tokens.json").read_bytes()).hexdigest()


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def new_doc(ep: Episode | str, doc_id: str = "main") -> dict:
    name = ep.root.name if isinstance(ep, Episode) else str(ep)
    return {"version": VERSION, "kind": KIND, "id": doc_id, "episode": name,
            "brand": {"tokens": "motion-kit/brand/tokens.json", "tokens_sha256": tokens_sha()},
            "sources": {}, "items": [], "notes": ""}


def new_item(item_id: str, template: str, style: str, format: str, slots: dict, *, source: str | None = None,
             renderer: str = "motion-kit", platforms: list[str] | None = None, caption: str = "", alt_text=None,
             reason: str = "", confidence: float = 0.0, assets: list[str] | None = None, ai_generated: bool = False) -> dict:
    return {"id": item_id, "template": template, "style": style, "format": format, "renderer": renderer, "slots": slots,
            "source": source, "platforms": list(platforms or []), "caption": caption, "alt_text": alt_text,
            "outputs": [], "review": {"status": "draft", "critic": None, "by": None, "at": None, "note": ""},
            "provenance": {"renderer_version": None, "template_rev": None, "canva": {"design_id": None, "template_id": None}},
            "license": {"assets": list(assets or []), "ai_generated": bool(ai_generated)},
            "reason": reason, "confidence": confidence}


# ---- validation -----------------------------------------------------------------------------------

def _validate_source(sid: str, s: dict) -> list[str]:
    p: list[str] = []
    t = s.get("type")
    if t not in SOURCE_TYPES:
        return [f"source {sid}: type must be one of {SOURCE_TYPES}"]
    if t in ("clip_quote", "markdown"):
        if not isinstance(s.get("text"), str) or not s["text"].strip():
            p.append(f"source {sid}: text is required (the words every quote from this source is checked against)")
        elif s.get("text_sha256") != hashlib.sha256(s["text"].encode()).hexdigest():
            p.append(f"source {sid}: text_sha256 does not match its text (was the source text edited by hand?)")
    if t == "clip_quote" and not s.get("clip"):
        p.append(f"source {sid}: clip is required (a clip id or slug from work/clips.json)")
    if t == "markdown" and not s.get("file"):
        p.append(f"source {sid}: file is required")
    if t == "episode_meta" and not s.get("title"):
        p.append(f"source {sid}: title is required")
    return p


def _validate_item(item: dict, doc: dict, reg: R.Registry | None) -> list[str]:
    iid = item.get("id", "?")
    p: list[str] = []

    def bad(msg: str) -> None:
        p.append(f"item {iid}: {msg}")

    if not isinstance(item.get("id"), str) or not _ID.match(item["id"]):
        p.append(f"item {iid!r}: id must be letters, digits, . _ - (max 81 chars)")
    if reg is not None:
        for m in reg.check_item_shape(item):
            bad(m)
    elif not item.get("template"):
        bad("template is required")
    src = item.get("source")
    if src is not None and src not in doc["sources"]:
        bad(f"unknown source {src!r} (have: {', '.join(doc['sources']) or 'none'})")
    if reg is not None and item.get("template") in reg.templates and isinstance(item.get("slots"), dict):
        tpl = reg.get(item["template"])
        slots = R.with_defaults(tpl.slots, item["slots"])
        for path, text, parent in R.walk_flagged(tpl.slots, slots, "x-verbatim"):
            sid = parent.get("source") or src
            if not sid:
                bad(f"{path} is a quote but the item has no source; name the clip or writing it came from")
                continue
            if sid not in doc["sources"]:
                bad(f"{path}: unknown source {sid!r}")
                continue
            hay = doc["sources"][sid].get("text")
            if not hay:
                bad(f"{path}: source {sid} ({doc['sources'][sid].get('type')}) has no text to check a quote against")
                continue
            for m in S.quote_problems(text, hay):
                bad(f"{path}: {m}")
        assets = [a for _, a, _ in R.walk_flagged(tpl.slots, slots, "x-asset")]
        listed = {a if isinstance(a, str) else a.get("file") for a in (item.get("license") or {}).get("assets", [])}
        for a in assets:
            if a not in listed:
                bad(f"asset {a!r} is used by a slot but not listed in license.assets; list it so the license gate can check it")
    if item.get("platforms") is not None and not (isinstance(item["platforms"], list) and all(isinstance(x, str) for x in item["platforms"])):
        bad("platforms must be a list of platform names")
    rv = item.get("review") or {}
    if rv.get("status") not in REVIEW_STATUSES:
        bad(f"review.status must be one of {REVIEW_STATUSES}")
    c = item.get("confidence")
    if not isinstance(c, (int, float)) or isinstance(c, bool) or not 0 <= c <= 1:
        bad("confidence must be a number between 0 and 1 (every judgment records how sure the agent is)")
    if not isinstance(item.get("reason"), str) or not item["reason"].strip():
        bad("reason is required: why this quote, this template, this style (the human reads it at review)")
    lic = item.get("license")
    if not isinstance(lic, dict) or not isinstance(lic.get("assets"), list) or not isinstance(lic.get("ai_generated"), bool):
        bad("license must be {assets: [...], ai_generated: true|false}")
    outs = item.get("outputs")
    if not isinstance(outs, list):
        bad("outputs must be a list")
        outs = []
    for k, o in enumerate(outs):
        tag = f"outputs[{k}]"
        for key in ("platform", "file"):
            if not isinstance(o.get(key), str) or not o[key]:
                bad(f"{tag}.{key} is required")
        if not (isinstance(o.get("w"), int) and isinstance(o.get("h"), int) and o["w"] > 0 and o["h"] > 0):
            bad(f"{tag}: w and h must be positive integers")
        if not isinstance(o.get("sha256"), str) or not _SHA.match(o["sha256"]):
            bad(f"{tag}.sha256 must be a 64-character hex digest")
        if "alt_text" not in o or not isinstance(o["alt_text"], str):
            bad(f"{tag}.alt_text is required (may be empty only until review)")
        elif rv.get("status") == "approved" and not o["alt_text"].strip():
            bad(f"{tag}: an approved item needs alt text")
    if rv.get("status") == "approved":
        if not outs:
            bad("approved but never rendered")
        if not rv.get("by") or not rv.get("at"):
            bad("approved items record who approved them (review.by, review.at)")
    return p


def validate(doc: dict, reg: R.Registry | None = None, *, check_registry: bool = True) -> list[str]:
    """Problems (empty = valid). Never raises. Loads the default registry unless `check_registry=False` or one is passed."""
    p: list[str] = []
    if doc.get("version") != VERSION:
        p.append(f"version must be {VERSION}")
    if doc.get("kind") != KIND:
        p.append(f"kind must be {KIND!r}")
    if not isinstance(doc.get("id"), str) or not _ID.match(doc.get("id") or ""):
        p.append("id must be letters, digits, . _ - (it names the file content/<id>.vN.json)")
    if not doc.get("episode"):
        p.append("episode is required")
    b = doc.get("brand") or {}
    if not b.get("tokens") or not _SHA.match(b.get("tokens_sha256") or ""):
        p.append("brand needs tokens and tokens_sha256")
    srcs = doc.get("sources")
    if not isinstance(srcs, dict):
        return p + ["sources must be an object"]
    for sid, s in srcs.items():
        p += _validate_source(sid, s)
    items = doc.get("items")
    if not isinstance(items, list):
        return p + ["items must be a list"]
    if reg is None and check_registry:
        try:
            reg = R.load_registry()
        except R.RegistryError as e:
            return p + [f"template registry: {e}"]
    for it in items:
        p += _validate_item(it, doc, reg)
    ids = [i.get("id") for i in items]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        p.append(f"duplicate item ids: {dup}")
    return p


# ---- persistence: <work>/content/<id>.vN.json -----------------------------------------------------

def list_versions(ep: Episode, doc_id: str = "main") -> list[int]:
    d = content_dir(ep)
    out = []
    for f in d.glob(f"{doc_id}.v*.json") if d.is_dir() else []:
        m = _V.match(f.name)
        if m and m["id"] == doc_id:
            out.append(int(m["n"]))
    return sorted(out)


def doc_ids(ep: Episode) -> list[str]:
    d = content_dir(ep)
    return sorted({m["id"] for f in d.glob("*.json") if (m := _V.match(f.name))}) if d.is_dir() else []


def path_for(ep: Episode, version: int, doc_id: str = "main") -> Path:
    return content_dir(ep) / f"{doc_id}.v{version}.json"


def latest_path(ep: Episode, doc_id: str = "main") -> Path | None:
    v = list_versions(ep, doc_id)
    return path_for(ep, v[-1], doc_id) if v else None


def save_new_version(ep: Episode, doc: dict, reg: R.Registry | None = None) -> Path:
    """Write the doc as the next version. Refuses an invalid doc; never overwrites an existing version."""
    problems = validate(doc, reg)
    if problems:
        raise ValueError("invalid ContentDoc:\n  - " + "\n  - ".join(problems))
    v = (list_versions(ep, doc["id"]) or [0])[-1] + 1
    path = path_for(ep, v, doc["id"])
    if path.exists():
        raise FileExistsError(path)
    doc = copy.deepcopy(doc)
    doc["saved"] = {"version": v, "at": now()}
    write_json(path, doc)
    return path


def load(path: Path | str) -> dict:
    return read_json(Path(path))


def load_latest(ep: Episode, doc_id: str = "main") -> dict:
    p = latest_path(ep, doc_id)
    if not p:
        raise SystemExit(f"No content plan yet for {ep.root.name} (id {doc_id!r}). Run: ave content plan {ep.root}, then ave content propose.")
    return load(p)


def clone(doc: dict) -> dict:
    return copy.deepcopy(doc)


def find_item(doc: dict, item_id: str) -> dict:
    for it in doc["items"]:
        if it["id"] == item_id:
            return it
    raise KeyError(f"no item {item_id!r} (have: {', '.join(i['id'] for i in doc['items'])})")
