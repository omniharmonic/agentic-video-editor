"""Local visual order, deliberately independent of schedules, queues and approval.

Plans contain only stable tile keys. A process lock and compare-and-swap revision
prevent two planners from silently overwriting each other. Live membership/status
is checked again on every move; published and publishing tiles are never moved.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
from contextlib import contextmanager

from ..episode import read_json, write_json
from . import model, store, views


class PlanConflict(ValueError):
    code = "conflict.feed_plan"


def _paths(sp, account):
    if not isinstance(account, str):
        raise ValueError("account must be a string")
    if account:
        model.check_id(account, "account id")
    scope = "account-" + account if account else "local"
    path = sp.work / "feed-plans" / (scope + ".json")
    for candidate in (path, path.with_suffix(".json.tmp"), path.with_suffix(".lock")):
        try:
            relative = candidate.relative_to(sp.root)
        except ValueError:
            raise ValueError("feed plans must stay inside the studio") from None
        cursor = sp.root
        for part in relative.parts:
            cursor /= part
            if cursor.is_symlink():
                raise ValueError("feed plan paths must not be symlinks")
        if candidate.exists() and not candidate.is_file():
            raise ValueError("feed plan sidecars must be regular files")
        if any(candidate == raw or raw in candidate.parents for raw in sp.raw_dirs):
            raise ValueError("feed plans cannot write to raw folders")
    return path


@contextmanager
def _lock(sp, account):
    path = _paths(sp, account)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield path


def _inventory(studio, account):
    accounts = views.account_rows(studio)
    acct = next((a for a in accounts if a["id"] == account), None)
    if account and acct is None:
        raise FileNotFoundError(f"no account {account!r}")
    assets = store.read_assets(studio.sp)
    cells = []
    for p in studio.posts():
        if p.get("stage") == "dropped":
            continue
        assignments = p.get("variants") or []
        variants = [v for v in assignments if not v.get("dropped") and v.get("status") != "dropped"]
        if p.get("stage") == "archived":
            # Completed publishing remains visible as locked history. Retired
            # or failed assignments must never reappear as movable drafts.
            variants = [v for v in variants if v.get("status") in views.LOCKED]
        if (assignments or p.get("stage") == "archived") and not variants:
            continue
        if account:
            selected = [v for v in variants if v.get("account") == account]
            if not assignments:
                selected = [None]
        else:
            # Local planning needs no platform account. Show each post once.
            selected = [next((v for v in variants if v.get("platform") == "instagram"), variants[0] if variants else None)]
        for variant in selected:
            v = variant or {"copy": {}, "media": None}
            from .composer import projection
            from .image_workshop import preview_url
            projected=projection(p,v,assets)
            media=projected["content"]+([projected["cover"]] if projected["cover"] else [])
            content=projected["content"]
            first = content[0] if content else {}
            rec = assets.get(first.get("asset"), {})
            cover = next((assets.get(m.get("asset"), {}) for m in media if m.get("role") == "cover"), {})
            preview = cover if cover.get("kind") == "image" else rec
            status = v.get("status", "draft")
            vid = v.get("id")
            cells.append({"key": f"{p['id']}:{vid if account and vid else 'base'}", "post": p["id"], "variant": vid,
                          "title": views._title(p, v) or "Untitled draft", "status": status,
                          "at": (v.get("schedule") or {}).get("utc"), "rev": p["rev"],
                          "locked": status in views.LOCKED, "unassigned": not bool(variant),
                          "thumb": preview_url(preview),
                          "media_url": "/files/" + rec["file"] if rec.get("file") else None,
                          "w": preview.get("w"), "h": preview.get("h"), "media_count": len(content),
                          "feed_presence":(v.get("presentation") or {}).get("feed_presence"), "crop":(projected["cover"] or first).get("crop"), "post_type": v.get("post_type", "feed"), "media_kind": rec.get("kind"), "created": p.get("created", "")})
    # New material appears first. History stays in a separate immutable suffix.
    cells.sort(key=lambda c: (c["at"] or c["created"], c["key"]), reverse=True)
    return acct or {"id": "", "label": "All drafts", "platform": "instagram", "local": True}, cells


def _read(studio, account, path):
    saved = read_json(path) if path.is_file() else {"rev": 0, "order": []}
    if (type(saved.get("rev")) is not int or saved["rev"] < 0 or not isinstance(saved.get("order"), list)
            or any(not isinstance(k, str) for k in saved["order"]) or len(set(saved["order"])) != len(saved["order"])):
        raise ValueError("feed plan is invalid; restore the saved plan before editing")
    acct, inventory = _inventory(studio, account)
    assets=store.read_assets(studio.sp)
    reported_ids={str((v.get('result') or {}).get('id') or (v.get('result') or {}).get('platform_id')) for p in store.list_posts(studio.sp) for v in p['variants'] if v.get('account')==account}
    reported_urls={(v.get('result') or {}).get('url') for p in store.list_posts(studio.sp) for v in p['variants'] if v.get('account')==account}
    for e in saved.get('external') or []:
        if e['url'] in reported_urls or (e.get('platform_id') and e['platform_id'] in reported_ids): continue
        rec=assets.get(e.get('thumbnail'),{})
        inventory.append({'key':e['key'],'external':True,'title':e['title'],'url':e['url'],'at':e['posted_at'],'status':'reported','locked':True,'rev':0,'post':None,'variant':None,'post_type':e['post_type'],'media_count':1,'media_kind':'image','thumb':'/files/'+rec['file'] if rec else None,'media_url':None,'w':rec.get('w'),'h':rec.get('h')})
    movable = {c["key"]: c for c in inventory if not c["locked"]}
    order = []
    for key in saved["order"]:
        if key in movable:
            order.append(key)
        elif key.endswith(":base"):
            # Assigning an account to an unassigned draft preserves its place.
            replacement = next((k for k, c in movable.items() if c["post"] == key.rsplit(":", 1)[0]), None)
            if replacement and replacement not in order:
                order.append(replacement)
    order += [k for k in movable if k not in order]
    cells = [movable[k] for k in order] + [c for c in inventory if c["locked"]]
    pins=[k for k in saved.get("pins",[]) if any(c["key"]==k for c in cells)]
    for c in cells: c["pinned"]=c["key"] in pins
    cells=[next(c for c in cells if c["key"]==k) for k in pins]+[c for c in cells if c["key"] not in pins]
    snapshot = hashlib.sha256(json.dumps([(c["key"], c["rev"], c["status"], c["locked"]) for c in cells], separators=(",", ":")).encode()).hexdigest()
    kind = views.LAYOUT_KIND.get(acct.get("platform"), "feed")
    columns, aspect = {"profile_grid": (3, "3:4"), "vertical_grid": (3, "9:16"), "pin_grid": (2, "2:3")}.get(kind, (1, "4:3"))
    return {"account": acct, "rev": saved["rev"], "snapshot": snapshot, "cells": cells,
            "layout": {"kind": kind, "columns": columns, "cell_aspect": aspect, "approximate": True},
            "base_order":order, "pins":pins, "external":saved.get("external",[]), "read_only": False, "order_kind": "local_visual_plan"}


def read(studio, account=""):
    """Read the complete plan; GET never creates or updates storage."""
    return _read(studio, account, _paths(studio.sp, account))


def move(studio, *, account="", expected_rev, key, before=None, snapshot=None):
    """Insert a tile before another, or at the end of the mutable plan."""
    if type(expected_rev) is not int or expected_rev < 0:
        raise ValueError("expected_rev must be a non-negative integer")
    if not isinstance(key, str) or (before is not None and not isinstance(before, str)):
        raise ValueError("key and before must be tile keys")
    if not isinstance(snapshot, str) or not snapshot:
        raise ValueError("snapshot is required")
    with _lock(studio.sp, account) as path:
        current = _read(studio, account, path)
        if current["rev"] != expected_rev or current["snapshot"] != snapshot:
            raise PlanConflict("The feed changed in another window. Reload the plan and try again.")
        cells = {c["key"]: c for c in current["cells"]}
        if key not in cells or (before is not None and before not in cells):
            raise PlanConflict("This post is no longer in the plan. Reload and try again.")
        if cells[key].get("pinned") or (before is not None and cells[before].get("pinned")): raise PlanConflict("Unpin this local preview tile before moving it")
        if cells[key]["locked"] or (before is not None and cells[before]["locked"]):
            raise PlanConflict("Published and publishing posts keep their place.")
        order = list(current["base_order"])
        if key == before:
            return current
        original = list(order)
        order.remove(key)
        order.insert(order.index(before) if before is not None else len(order), key)
        if original != order:
            saved=read_json(path) if path.exists() else {}
            write_json(path, {**saved,"v": saved.get("v",1), "kind": "studio.feed-plan", "account": account or None,
                              "rev": expected_rev + 1, "order": order})
        return _read(studio, account, path)


def decorate(studio, *, account, expected_rev, snapshot, op, key=None, entry=None):
    """Local pins and human-reported context; never writes publishing records."""
    from . import authoring, production
    from .clock import parse_ts
    import secrets
    if not account: raise ValueError('Choose a profile before adding context or pins')
    with _lock(studio.sp, account) as path:
        current=_read(studio,account,path)
        if type(expected_rev) is not int or current['rev']!=expected_rev or current['snapshot']!=snapshot: raise PlanConflict('The feed changed. Reload before editing preview context.')
        saved=read_json(path) if path.exists() else {'v':2,'account':account,'rev':0,'order':[]}
        pins=list(saved.get('pins') or []);external=list(saved.get('external') or [])
        if op=='pin':
            if key not in {c['key'] for c in current['cells']}: raise ValueError('This tile is no longer in the plan')
            if key in pins: pins.remove(key)
            else:
                if len(pins)>=3: raise ValueError('This local preview allows three pins')
                pins.append(key)
        elif op=='external':
            production.only(entry,{'title','url','posted_at','thumbnail','post_type','platform_id'})
            production.text(entry.get('title'),'Title',300,True);authoring.url(entry.get('url',''))
            if not entry.get('url'): raise ValueError('A reported publication URL is required')
            when=parse_ts(entry.get('posted_at',''))
            if when is None: raise ValueError('Provide the reported publication time')
            if entry.get('post_type') not in ('feed','reel','story'): raise ValueError('Choose post, Reel or Story')
            assets=store.read_assets(studio.sp)
            if entry.get('thumbnail') and assets.get(entry['thumbnail'],{}).get('kind')!='image': raise ValueError('Thumbnail must be a registered image')
            if entry.get('platform_id'): production.text(entry['platform_id'],'Platform identifier',300)
            if any(e['url']==entry['url'] for e in external): raise ValueError('This reported publication is already in the preview')
            external.append({**entry,'key':'external_'+secrets.token_hex(10),'source':'human','imported_at':studio.now().isoformat()})
        elif op=='remove-external':
            external=[e for e in external if e['key']!=key];pins=[k for k in pins if k!=key]
        else: raise ValueError('Unknown feed context operation')
        saved.update(v=2,rev=expected_rev+1,pins=pins,external=external)
        write_json(path,saved)
        return _read(studio,account,path)
