"""Read models and one write the UI needs that no earlier workstream owned (integration, WS7):

    account_rows(studio)      accounts with true live state, credential readiness (names only), quota, last tick   (GET accounts, grid header)
    grid(studio, account)     the per-account grid in the order it will look on the platform                           (GET grid/<account>)
    preview(studio, post, v)  caption fold + counter in the platform's own unit, media crop overlay                     (GET posts/<id>/preview/<v>)
    swap_asset(...)           replace one media slot of a variant, then re-sync its queue item so a changed byte voids approval

Shapes are the committed fixtures in tests/data_studio/api/ (GET_accounts, GET_grid, GET_preview, POST_swap_asset). Every layout and
fold number comes from config/previews.yaml, which is entirely unverified, and the UI labels the result "approximate".
"""

from __future__ import annotations
from .image_workshop import preview_url

import copy
from datetime import datetime, timedelta
from typing import Any

import yaml

from .. import resources
from . import dispatch, ledger, model, policy, posttext, state, store, text_metrics
from .clock import iso_utc, parse_ts
from .events import AGENT, Actor

LAYOUT_KIND = {"instagram": "profile_grid", "tiktok": "vertical_grid", "pinterest": "pin_grid", "facebook": "page_timeline"}
LOCKED = ("published", "publishing")


# ------------------------------------------------------------------------------------------------ config

def previews_config() -> dict:
    try:
        data = yaml.safe_load((resources.home() / "config" / "previews.yaml").read_text())
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _platforms_cfg() -> dict:
    try:
        from ..publish.adapters import base
        return base.load_platforms()
    except Exception:
        return {}


# ------------------------------------------------------------------------------------------------ accounts

def _credentials(acct) -> dict:
    """{"ready", "missing"}: names of the secrets still absent. Never a value."""
    from ..platforms import registry
    try:
        adapter = registry.adapter_for(acct)
    except Exception:
        return {"ready": False, "missing": []}
    required = getattr(adapter, "required", None)
    if required is None:                    # an existing-adapter wrapper (youtube, aggregator): its own readiness list
        problems = [p for p in adapter.ready_problems() if "not live" not in p and "paused" not in p]
        return {"ready": not problems, "missing": []}
    missing = acct.missing(tuple(required))
    return {"ready": not missing, "missing": missing}


def _quota(sp, account_id: str, cfg: dict, now: datetime) -> dict:
    from ..schedule import limits
    times = limits._times(limits.load_state(sp, account_id))
    used = len([t for t in times if now - t < limits.WINDOW])
    cap = (cfg.get("limits") or {}).get("max_per_day", limits.DEFAULT_MAX_PER_DAY)
    return {"used_24h": used, "limit_24h": cap if isinstance(cap, int) else limits.DEFAULT_MAX_PER_DAY}


def _last_tick(sp) -> str | None:
    from ..episode import read_json
    from ..schedule import tick as _tick
    try:
        return read_json(_tick.heartbeat_path(sp)).get("at")
    except (OSError, ValueError):
        return None


def account_rows(studio) -> list[dict]:
    from ..platforms import attest
    from ..platforms.accounts import AccountError, load_account
    sp, now = studio.sp, studio.now()
    out = []
    for p in sorted(sp.accounts_dir.glob("*.yaml")) if sp.accounts_dir.is_dir() else []:
        cfg = policy.account_config(sp, p.stem)
        if not cfg.get("id"):
            continue
        row = {"id": cfg["id"], "platform": cfg.get("platform"), "mode": cfg.get("mode"), "label": cfg.get("label", ""), "brand": cfg.get("brand") or None,
               "tz": cfg.get("tz"), "post_types": cfg.get("post_types", []),
               "live": attest.verify_live(sp.accounts_dir, cfg["id"]).ok,
               "paused": dispatch.is_paused(sp) or dispatch.is_paused(sp, cfg["id"]),
               "credentials": {"ready": False, "missing": []}, "quota": _quota(sp, cfg["id"], cfg, now),
               "last_tick": _last_tick(sp), "token_expires": None}
        try:
            if getattr(studio, "native_authoring", False):
                row["credentials"] = {"ready": None, "missing": [], "status": "unverified", "note": "Credential checks require an explicit Connections action."}
            else:
                row["credentials"] = {"ready":False,"missing":[]} if cfg.get("profile") and cfg.get("mode")=="manual" else _credentials(load_account(sp, cfg["id"]))
        except AccountError:
            row["credentials"] = {"ready": False, "missing": []}
        row["profile"] = dict(cfg.get("profile") or {})
        avatar=store.read_assets(sp).get(row["profile"].get("avatar"),{})
        if avatar.get("kind")=="image": row["profile"]["avatar_url"]="/files/"+avatar["file"]
        out.append(row)
    return out


# ------------------------------------------------------------------------------------------------ grid

def _asset_dims(rec: dict | None) -> tuple[int | None, int | None]:
    return (rec.get("w"), rec.get("h")) if rec else (None, None)


def _title(post: dict, v: dict) -> str:
    cap = (model.effective_copy(post, v)["caption"] or "").strip()
    return (cap.splitlines()[0] if cap else (post.get("idea") or {}).get("text", ""))[:90]


def grid(studio, account: str, limit: int = 30) -> dict:
    sp, now = studio.sp, studio.now()
    rows = [r for r in account_rows(studio) if r["id"] == account]
    if not rows:
        raise FileNotFoundError(f"no account {account!r}")
    acct = rows[0]
    kind = LAYOUT_KIND.get(acct["platform"], "feed")
    grid_cfg = ((previews_config().get("platforms") or {}).get(acct["platform"]) or {}).get("grid") or {}
    layout: dict[str, Any] = {"kind": kind, "approximate": True, "verified": bool(grid_cfg.get("verified", False))}
    if grid_cfg.get("cell_aspect"):
        layout.update(columns=grid_cfg.get("columns", 3), cell_aspect=":".join(str(x) for x in grid_cfg["cell_aspect"]), crop="center_cover")
    elif kind == "vertical_grid":
        layout.update(columns=3, cell_aspect="9:16", crop="center_cover")
    elif kind == "pin_grid":
        layout.update(columns=2, cell_aspect="2:3", crop="center_cover")
    else:
        layout.update(columns=1)
    assets = store.read_assets(sp)
    fb = ledger.open_feedback(sp, now=now)
    cells = []
    for p in store.list_posts(sp):
        if p.get("stage") == "dropped":
            continue
        mine = [f for f in fb if f.get("post") == p["id"]]
        for v in p.get("variants") or []:
            if v.get("account") != account or v.get("dropped"):
                continue
            ex = state.explain(v, state.load_queue_item(sp, v), mine, now, sp)
            media = sorted(model.effective_media(p, v), key=lambda m: m.get("slot", 0))
            first = media[0] if media else None
            rec = assets.get(first["asset"]) if first else None
            w, h = _asset_dims(rec)
            sched = v.get("schedule") or {}
            at = None
            if sched.get("utc"):
                from zoneinfo import ZoneInfo
                try:
                    at = parse_ts(sched["utc"]).astimezone(ZoneInfo(acct["tz"] or "UTC")).isoformat()
                except Exception:
                    at = None
            cells.append({"post": p["id"], "variant": v["id"], "at": at, "status": ex["status"], "title": _title(p, v),
                          "thumb": preview_url(rec), "w": w, "h": h, "rev": p["rev"],
                          "locked": bool(sched.get("locked")) or ex["status"] in LOCKED,
                          "crop": (first or {}).get("crop") or {"grid_focus": [0.5, 0.5]}, "text_cut": False})
    # newest first, the way a profile reads: scheduled posts by time (later = earlier in the list), unscheduled ones last
    cells.sort(key=lambda c: (c["at"] is None, "" if c["at"] is None else _neg(c["at"]), c["post"]))
    return {"account": acct, "layout": layout, "cells": cells[:max(1, int(limit))]}


def _neg(iso: str) -> str:
    """A sort key that orders ISO times newest first without parsing: invert each digit."""
    return "".join(str(9 - int(c)) if c.isdigit() else c for c in parse_ts(iso).strftime("%Y%m%d%H%M%S"))


# ------------------------------------------------------------------------------------------------ preview

def _ratio(rec: dict) -> float | None:
    w, h = rec.get("w"), rec.get("h")
    return (w / h) if w and h else None


def _crop_overlay(rec: dict, cell: list[int] | None) -> dict | None:
    r = _ratio(rec)
    if not cell or not r:
        return None
    ca = cell[0] / cell[1]
    lost = {"left_pct": 0, "right_pct": 0, "top_pct": 0, "bottom_pct": 0}
    if r > ca:
        side = round((1 - ca / r) / 2 * 100)
        lost.update(left_pct=side, right_pct=side)
    elif r < ca:
        side = round((1 - r / ca) / 2 * 100)
        lost.update(top_pct=side, bottom_pct=side)
    return {"cell_aspect": f"{cell[0]}:{cell[1]}", "lost": lost}


def _hard_limit(platform: str, post_type: str, soft: int | None) -> int | None:
    cfg = _platforms_cfg().get(platform) or {}
    for block in ("image", "post"):
        lim = (cfg.get(block) or {}).get("caption_max_chars")
        if isinstance(lim, int):
            return lim
    return soft


def preview(studio, post_id: str, variant_id: str) -> dict:
    sp = studio.sp
    post = store.load_post(sp, post_id)
    v = model.find_variant(post, variant_id)
    plat = v["platform"]
    pcfg = (previews_config().get("platforms") or {}).get(plat) or {}
    assets = store.read_assets(sp)
    composed = posttext.compose_variant(post, v, assets)        # the FINAL text (caption, link, credits, hashtags), as `queue sync` writes it
    text = composed.text
    counter_cfg = pcfg.get("counter") or {}
    unit = counter_cfg.get("unit", "chars")
    fold = pcfg.get("fold") or {}
    hard = _hard_limit(plat, v["post_type"], counter_cfg.get("soft_limit"))
    used = text_metrics.counter(text, unit)
    cell = (pcfg.get("grid") or {}).get("cell_aspect")
    warnings: list[str] = []
    media = []
    from .composer import projection
    projected=projection(post,v,assets)
    for m in projected["content"]:
        rec = assets.get(m["asset"]) or {}
        media.append({"asset": m["asset"], "preview_url":preview_url(rec,"large"), "url": ("/files/" + rec["file"]) if rec.get("file") else None, "w": rec.get("w"), "h": rec.get("h"),
                      "alt": m.get("alt") or rec.get("alt") or "", "crop_overlay": _crop_overlay(rec, cell) if plat == "instagram" else None,
                      "text_cut": False})
        if rec.get("kind") == "image" and not (m.get("alt") or rec.get("alt")):
            warnings.append(f"slot {m.get('slot', 0)}: no alt text")
    if hard and text_metrics.over_limit(text, unit, hard):
        warnings.append(f"caption is {used} {unit}, over the {hard} limit (approximate count)")
    warnings += [p for p in composed.problems if "over the" not in p and p not in warnings]   # the length warning above already says it
    if not media and v["post_type"] not in ("text",):
        warnings.append("no media attached")
    return {"post": post_id, "variant": variant_id, "platform": plat, "kind": "profile_grid_cell" if plat == "instagram" else "feed_card",
            "approximate": True,
            "caption": {"text": text, "fold_at": fold.get("at"), "fold_unit": fold.get("unit"), "counter": {"unit": unit, "used": used, "limit": hard}},
            "first_comment": composed.first_comment,
            "media": media, "cover": projected["cover"], "warnings": warnings+projected["problems"]}


# ------------------------------------------------------------------------------------------------ swap asset

def swap_asset(studio, post_id: str, variant_id: str, index: int, asset_id: str, *, expected_rev: int, actor: Actor = AGENT) -> dict:
    """Replace media slot `index` of one variant with `asset_id`. The variant gets its own media list (copied from the post's when it
    inherited). Alt text is the new asset's, else the old slot's, and one is required. Then the queue item is re-synced so changed bytes
    void an approval (event `approval_invalidated`); nothing here approves."""
    sp = studio.sp
    post = store.load_post(sp, post_id)
    v = model.find_variant(post, variant_id)
    from .composer import editable
    editable(studio, post, v)
    rec = store.read_assets(sp).get(asset_id)
    if rec is None:
        raise FileNotFoundError(f"asset {asset_id} is not in the library")
    media = copy.deepcopy(model.effective_media(post, v))
    if not (0 <= index < len(media)):
        raise ValueError(f"variant {variant_id} has {len(media)} media slots; index {index} is out of range")
    old = media[index]
    if rec.get('kind') != ('image' if old['role']=='cover' else old['role']):
        raise ValueError('Replacement media must match the slot kind')
    alt = (rec.get("alt") or old.get("alt") or "").strip()
    if rec.get("kind") == "image" and not alt:
        raise ValueError("the new image has no alt text and the old slot had none either: set alt text first")
    media[index] = {**old, "asset": asset_id, "alt": alt, "crop": None}
    edited = copy.deepcopy(post)
    model.find_variant(edited, variant_id)["media"] = media
    store.save_post(sp, edited, expected_rev=expected_rev, actor=actor, op="swap_asset", variant=variant_id,
                    detail={"index": index, "from": old["asset"], "to": asset_id}, now=studio.now())
    warnings: list[str] = []
    if state.load_queue_item(sp, v) is not None:
        before = state.load_queue_item(sp, v)["status"]
        rep = studio.sync_queue(post_id) if studio.has("sync_queue") else None
        row = next((r for r in (rep or {}).get("variants", []) if r["variant"] == variant_id), None)
        if row and row.get("invalidated"):
            warnings.append(f"approval dropped: media changed; {variant_id} is {row.get('queue_status') or before} again")
        elif row and row.get("problems"):
            warnings += [str(p) for p in row["problems"]]
    return {"post": studio.view(store.load_post(sp, post_id)), "warnings": warnings}


def register(studio) -> None:
    studio.register("grid", lambda st, account, limit=30: grid(st, account, limit))
    studio.register("preview", lambda st, post_id, variant_id: preview(st, post_id, variant_id))
    studio.register("swap_asset", swap_asset)
