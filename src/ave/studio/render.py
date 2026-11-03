"""`ave studio render <post>`: the content renderer on a studio, outputs registered in the library and attached to the post (spec 8, 9.1 step 3).

The studio IS an Episode to the content code (spec 2.1.1), so the existing pipeline runs unchanged: `propose` (verbatim quotes, slot limits, platform fit),
`render_doc` (headless Chromium, critic measured on the page), `save_new_version`. This module only adds the studio's bookkeeping:

  * one ContentDoc per post (`work/content/<post id>.vN.json`), every render a new version, never overwritten;
  * each distinct output file is added to the library as `source.type: render` with `derived_from` = the library assets its slots used
    (so a stock photo on a card is license-checked through the closure), alt text from the renderer, tags from the template;
  * the post's media is replaced by the rendered files (slide order), unless the critic failed (then nothing is attached; `--keep-failed` overrides);
  * Instagram gets its JPEG at sync time (`studio.deliverables`), so the rendered PNG here is the platform-neutral master.

Nothing posts, approves or spends.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..content import manifest as M
from ..content import propose as P
from ..content import registry as R
from ..content import render_motionkit as RM
from ..content import settings
from ..publish import safety
from . import model, store
from .events import AGENT, Actor


class RenderError(ValueError):
    def __init__(self, message: str, report: dict | None = None):
        super().__init__(message)
        self.report = report or {}


def _origin_source(post: dict) -> Any:
    o = (post.get("idea") or {}).get("origin") or {}
    if o.get("type") == "blog" and o.get("ref"):
        return {"writing": o["ref"], "passages": list(o.get("passages") or [])} if o.get("passages") else None
    if o.get("type") == "episode" and o.get("clip"):
        return {"clip": o["clip"]}
    return "episode"


def _writing_text(sp, source: dict) -> str | None:
    from ..content import sources as S
    try:
        return S.writing_source(sp, source["writing"], source.get("passages"), source.get("sentences"))["text"]
    except (ValueError, KeyError):
        return None


def build_pick(sp, post: dict, *, template: str, style: str, fmt: str | None, slots: dict | None, source: Any, caption: str | None,
               alt_text: Any, reason: str, confidence: float | None = None) -> dict:
    reg = R.load_registry(cfg=settings.load())
    tpl = reg.get(template)
    src = source if source is not None else _origin_source(post)
    if slots is None:
        if template in ("quote-post", "testimonial") and isinstance(src, dict) and "writing" in src:
            text = _writing_text(sp, src)
            if not text:
                raise RenderError("could not read the source passages: ingest the blog first (`ave studio ingest blog <file>`)")
            slots = {"quote": text}
        else:
            raise RenderError(f"template {template} needs its slots: pass --slots FILE.json (see `ave content templates` for the schema)")
    pick: dict = {"template": template, "style": style, "format": fmt or tpl.formats[0], "slots": slots, "platforms": ["any"],
                  "reason": reason or f"studio render for {post['id']}", "confidence": confidence if confidence is not None else (post.get("idea") or {}).get("confidence") or 0.7}
    if src is not None:
        pick["source"] = src
    if caption:
        pick["caption"] = caption
    if alt_text:
        pick["alt_text"] = alt_text
    return pick


def _asset_for_slot_paths(sp, tpl, slots: dict) -> list[str]:
    """Library asset ids behind every `x-asset` slot value (a path into the studio). A slot naming a file the library does not know is an
    error: no render may use media without a license trail."""
    from .. import library
    ids: list[str] = []
    for path, value, _ in R.walk_flagged(tpl.slots, R.with_defaults(tpl.slots, slots), "x-asset"):
        if not value:
            continue
        f = Path(value) if Path(value).is_absolute() else sp.root / value
        if not f.is_file():
            raise RenderError(f"{path}: file not found: {value}")
        rec = library.registry.find_by_sha(sp, safety.sha256_file(f))
        if rec is None:
            raise RenderError(f"{path}: {value} is not in the library; add it first (`ave studio library add`) so its license is on record")
        ids.append(rec["id"])
    return list(dict.fromkeys(ids))


def render_post(studio, post_id: str, *, template: str = "quote-post", style: str = "ink", fmt: str | None = None, slots: dict | None = None,
                source: Any = None, caption: str | None = None, alt_text: Any = None, reason: str = "", confidence: float | None = None, by: str = "claude",
                actor: Actor = AGENT, keep_failed: bool = False, attach: bool = True, expected_rev: int | None = None) -> dict:
    from .. import library
    sp = studio.sp
    post = store.load_post(sp, post_id)
    cfg = settings.load()
    reg = R.load_registry(cfg=cfg)
    tpl = reg.get(template)
    pick = build_pick(sp, post, template=template, style=style, fmt=fmt, slots=slots, source=source, caption=caption, alt_text=alt_text, reason=reason, confidence=confidence)
    derived = _asset_for_slot_paths(sp, tpl, pick["slots"])
    if derived:
        gaps = library.coverage(sp, derived)
        if gaps["missing"]:
            raise RenderError("an input of this card has no license entry: " + ", ".join(m.get("id", str(m)) if isinstance(m, dict) else str(m) for m in gaps["missing"]))
    doc_id = post_id
    prop = P.propose(sp, [pick], doc_id, allow_unapproved=False, reg=reg)
    if prop["rejected"]:
        raise RenderError("the content planner refused this card: " + "; ".join(p for r in prop["rejected"] for p in r["problems"]), prop)
    doc = M.load_latest(sp, doc_id)
    item_ids = [prop["accepted"][0]["id"]]
    try:
        new, rep = RM.render_doc(sp, doc, item_ids=item_ids, force=True, allow_unlicensed=bool(derived), reg=reg, cfg=cfg)
    except RM.LicenseError as e:
        raise RenderError(str(e)) from e
    if rep["failed"]:
        raise RenderError("render failed: " + "; ".join(f"{f['id']}: {f['error']}" for f in rep["failed"]), rep)
    path = M.save_new_version(sp, new)
    item = next(i for i in new["items"] if i["id"] == item_ids[0])
    critic = (item["review"] or {}).get("critic") or {}
    files: dict[str, dict] = {}
    for o in sorted(item["outputs"], key=lambda o: o.get("slide", 0)):
        files.setdefault(o["file"], o)
    registered, media = [], []
    for slot, (rel, o) in enumerate(files.items()):
        rec = library.add_file(sp, sp.root / rel, source={"type": "render"}, by=by, derived_from=derived, alt=o["alt_text"],
                               tags=[template, style, item["format"]], origin={"content_doc": doc_id, "version": M.list_versions(sp, doc_id)[-1],
                                                                              "item": item["id"], "file": rel}, now=studio.now())
        registered.append({"asset": rec["id"], "file": rec["file"], "sha256": rec["sha256"], "w": rec["w"], "h": rec["h"], "alt": o["alt_text"]})
        media.append({"slot": slot, "asset": rec["id"], "role": "image", "alt": o["alt_text"], "crop": {"grid_focus": [0.5, 0.5]}})
    if not registered:
        raise RenderError("Renderer produced no output files; the draft was left unchanged", rep)
    ok = bool(critic.get("ok", True))
    out = {"post": post_id, "doc": str(path), "item": item["id"], "template": template, "style": style, "format": item["format"],
           "critic": {"ok": ok, "contrast": critic.get("contrast"), "notes": critic.get("notes", [])}, "assets": registered,
           "warnings": item.get("warnings", []), "attached": False}
    if attach and (ok or keep_failed):
        cur = store.load_post(sp, post_id)
        if expected_rev is not None and cur["rev"] != expected_rev:
            out["attachment_conflict"] = True
            out["note"] = "The draft changed while rendering. The finished artifact is available here; newer draft media was preserved."
            return out
        cur["media"] = media
        try:
            saved = store.save_post(sp, cur, expected_rev=cur["rev"], actor=actor, op="render", detail={"item": item["id"], "assets": [m["asset"] for m in media]},
                                now=studio.now())
        except model.RevConflict:
            out["attachment_conflict"] = True
            out["note"] = "The draft changed while attaching. Finished artifacts remain available; newer draft media was preserved."
            return out
        out.update(attached=True, rev=saved["rev"])
    elif attach:
        out["note"] = "the critic failed (text outside the safe area or low contrast): nothing was attached. Shorten the words or choose another style/format, or pass --keep-failed."
    return out
