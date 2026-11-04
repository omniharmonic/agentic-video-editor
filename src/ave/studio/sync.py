"""Post -> queue sync (spec 2.2, 2.8, 8 `queue sync`). One queue item per (post, account) variant, kept in line with the post.

    sync_queue(sp, post_id)   create or update the queue item of every live variant, run all preflight and the license closure.

HARD RULES (each has a test):
  * It NEVER approves. Approval is a human act (`queue.approve`); this module does not import or call it.
  * It never changes an approval's validity by itself: `queue.update` re-hashes and demotes an approved item to `awaiting_approval`
    (event `approval_invalidated`) exactly when the BYTES or the bound METADATA (caption, title, tags, alt texts, account, link, first
    comment, ai disclosure, post type, platforms) changed. A pure retime changes only `schedule`, which `meta_v` 2 items do not bind.
  * A variant whose media closure has an unlicensed leaf, or whose images lack alt text, gets NO queue item (and keeps none it has
    been changed on): the problem is reported and the variant flagged `license_gap`.
  * Items adopted from an episode (`queue.root != "."`) belong to that episode's queue and are never rewritten here; the studio no longer SENDS
    them (no account binding, not meta_v 2): a variant that still points at one gets a fresh studio-owned item, which needs its own approval.
  * Items that are published, publishing, or uncertain are left alone.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from ..publish import queue as pq, safety
from ..publish.adapters import base
from . import deliverables, dispatch, model, posttext, state, store
from .clock import Clock, SystemClock, parse_ts
from .events import AGENT, Actor, append_event, make_event

VISIBILITY = "public"          # the human's approval covers it (visibility is bound by the metadata hash); the platform adapters decide the rest


def _title(post: Mapping, caption: str) -> str:
    line = (caption or "").strip().splitlines()[0] if (caption or "").strip() else ""
    return (line or (post.get("idea") or {}).get("text") or "post")[:80]


def _spec(sp, post: Mapping, v: Mapping, assets: Mapping[str, Mapping], platforms: Mapping | None = None) -> tuple[dict | None, list[str]]:
    """({media + metadata fields for queue.create/update}, problems). `None` spec = the variant cannot be queued yet."""
    problems: list[str] = []
    from .composer import projection, delivery
    projected = projection(post, v, assets)
    if projected["problems"]: return None, projected["problems"]
    media = projected["content"]
    files, alts, kinds = [], [], set()
    for m in media:
        rec = assets.get(m["asset"])
        if rec is None:
            problems.append(f"media slot {m.get('slot')}: asset {m['asset']} is not in the library registry")
            continue
        from .production import safe_path
        try: path = safe_path(sp, sp.root / rec["file"])
        except ValueError as exc:
            problems.append(str(exc)); continue
        if not path.is_file():
            problems.append(f"media slot {m.get('slot')}: file for {m['asset']} is missing")
            continue
        if safety.sha256_file(path) != rec['sha256']:
            problems.append(f"media slot {m.get('slot')}: asset bytes changed since registration")
            continue
        kinds.add(rec.get("kind"))
        files.append(path)
        alt = (m.get("alt") or rec.get("alt") or "").strip()
        alts.append(alt)
        if rec.get("kind") == "image" and not alt:
            problems.append(f"media slot {m.get('slot')}: image {m['asset']} has no alt text")
    if problems:
        return None, problems
    if not media:
        mt='text'
    elif kinds == {'document'} and len(files)==1:
        mt='document'
    elif kinds == {"image"}:
        mt = "image" if len(files) == 1 else "carousel"
    elif kinds == {"video"} and len(files) == 1:
        mt = "video"
    else:
        return None, [f"unsupported media mix {sorted(map(str, kinds))} x {len(files)}: mixed-media automatic delivery is pending the media task; select one video, one PDF, images, or text only"]
    if delivery(v)=="manual":
        if v["post_type"] in ("reel","short","video") and mt!="video": return None,["This format needs one video"]
        if v["post_type"]=="carousel" and mt!="carousel": return None,["A carousel needs at least two images"]
    eff = model.effective_copy(post, v)
    final = posttext.compose_variant(post, v, assets, platforms=platforms)          # caption + link + credits + hashtags: THE text the approval binds and the adapter sends
    if final.problems:
        return None, final.problems
    if mt=='text' and not final.text: return None,['Enter a caption for a text-only post']
    sched = (v.get("schedule") or {}).get("utc")
    fields: dict[str, Any] = {
        "platforms": [v["platform"]], "title": _title(post, eff["caption"]), "description": final.text, "tags": list(final.hashtags),
        "caption": final.text, "visibility": VISIBILITY, "ai_disclosure": model.ai_generated(assets, [m["asset"] for m in media]),
        "account": v["account"], "post_type": v["post_type"], "first_comment": final.first_comment, "link_url": eff["link"] or None,
        "schedule": parse_ts(sched).isoformat() if sched else None,
    }
    if v.get("payload_version") == 3 or v.get("delivery") is not None or projected["cover"] or not final.text or v["platform"]=="facebook":
        cover = projected['cover']
        record = assets.get(cover['asset']) if cover else None
        if record:
            try:
                cover_path=safe_path(sp,sp.root/record['file'])
                if not cover_path.is_file() or safety.sha256_file(cover_path)!=record['sha256']:
                    return None, ['Cover bytes changed or missing since registration']
            except ValueError as exc: return None, [str(exc)]
        fields.update(meta_v=3, delivery={'method':delivery(v)}, presentation=v.get('presentation') or {},
                      cover={**{k:record[k] for k in ('id','file','sha256')},'alt':cover.get('alt') or record.get('alt') or ''} if record else None)
    if mt == "video":
        fields.update(video=files[0], media_type="video")
    else:
        fields.update(files=files, media_type=mt, alt_texts=alts)
    return fields, []


def queue_diff(sp, post: Mapping, v: Mapping, item: Mapping, assets: Mapping[str, Mapping] | None = None) -> list[dict]:
    """How the queue item differs from what the post says it should be: [{"field", "queue", "post"}]. Empty = the item IS the post, so the human
    who reviewed the post reviewed what the approval would sign. Compares everything a reader sees or the approval binds: caption, first comment,
    link, account, post type, platform, hashtags, alt texts and the sha256 of every file, in order."""
    ep = state.queue_episode(sp, v)
    fields, problems = _spec(sp, post, v, assets if assets is not None else store.read_assets(sp))
    if fields is None:
        return [{"field": "post", "queue": None, "post": "; ".join(problems)}]
    want_files = [safety.sha256_file(p) for p in (fields["files"] if fields["media_type"] != "video" else [fields["video"]])]
    try:
        have_files = [safety.sha256_file(p) for p in pq.resolve_files(ep, dict(item))]
    except (OSError, KeyError, ValueError) as e:
        have_files = [f"unreadable: {e}"]
    pairs = [("caption", item.get("caption") or "", fields["caption"] or ""), ("first_comment", item.get("first_comment") or None, fields["first_comment"] or None),
             ("link", item.get("link_url") or None, fields["link_url"] or None), ("account", item.get("account"), fields["account"]),
             ("post_type", item.get("post_type"), fields["post_type"]), ("platforms", list(item.get("platforms") or []), list(fields["platforms"])),
             ("hashtags", list(item.get("tags") or []), list(fields["tags"])), ("files", have_files, want_files)]
    if fields.get("meta_v") == 3:
        pairs += [(k, item.get(k), fields.get(k)) for k in ("meta_v","delivery","cover","presentation")]
    if fields["media_type"] != "video":
        pairs.append(("alt_texts", list(item.get("alt_texts") or []), list(fields["alt_texts"])))
    return [{"field": k, "queue": a, "post": b} for k, a, b in pairs if a != b]


def queue_mismatch(sp, post: Mapping, v: Mapping, item: Mapping) -> list[str]:
    """`queue_diff` as short messages (empty = the queue item equals the post)."""
    return [d["field"] + (" differs" if d["field"] != "post" else f": {d['post']}") for d in queue_diff(sp, post, v, item)]


def _create(sp, fields: dict, publishers, post_ref: str | None = None) -> dict:
    kw = {k: fields[k] for k in ("platforms", "title", "description", "tags", "caption", "visibility", "ai_disclosure", "account",
                                 "post_type", "first_comment", "link_url", "schedule", "media_type")}
    if fields["media_type"] == "video":
        kw["video"] = fields["video"]
    else:
        kw.update(files=fields["files"], alt_texts=fields["alt_texts"])
    kw.update({k:fields[k] for k in ("delivery","cover","presentation") if k in fields})
    return pq.create(sp, meta_v=fields.get("meta_v",2), publishers=publishers, post_ref=post_ref, **kw)


def _update(sp, item_id: str, fields: dict, publishers) -> dict:
    return pq.update(sp, item_id, publishers=publishers, **fields)


def sync_queue(sp, post_id: str, *, publishers: Mapping[str, Any] | None = None, actor: Actor = AGENT, clock: Clock | None = None,
               now: datetime | None = None, variant_id: str | None = None) -> dict:
    """Create or update the queue item of every non-dropped variant of `post_id`; run preflight and the license closure; NEVER approves.

    `publishers` may be keyed by account id or platform (as for the tick); they are used for preflight only. Returns
    {"post", "variants": [{"variant","action","queue","queue_status","problems","invalidated"}], "problems": [...]} where `action` is one of
    created | updated | unchanged | blocked | skipped. A variant whose approval was voided by a byte/metadata change reports
    `invalidated: true` and the new status `awaiting_approval` (or `draft` when preflight now fails)."""
    now = now or (clock or SystemClock()).now()
    if variant_id is not None: model.find_variant(store.load_post(sp,post_id),variant_id)
    converted = deliverables.ensure(sp, post_id, actor=actor, now=now, variant_id=variant_id)
    post = store.load_post(sp, post_id)
    assets = store.read_assets(sp)
    platforms = base.load_platforms()                 # parsed once per sync, not per variant
    report: list[dict] = []
    edits: dict[str, dict] = {}
    for v in post["variants"]:
        if variant_id is not None and v['id'] != variant_id: continue
        if v.get("dropped"):
            continue
        row = {"variant": v["id"], "action": "unchanged", "queue": (v.get("queue") or {}).get("id"), "queue_status": None, "problems": [],
               "invalidated": False}
        report.append(row)
        ref = v.get("queue")
        adopted = bool(ref and ref.get("root", ".") != ".")
        fields, problems = _spec(sp, post, v, assets, platforms)
        gaps = dispatch.license_gaps(sp, dispatch.variant_asset_ids(post, v)) if fields else []
        if gaps:
            fields, problems = None, [f"license: {g}" for g in gaps]
        flag_gap = bool(gaps)
        if fields is None:
            row.update(action="blocked", problems=problems)
            edits.setdefault(v["id"], {}).update(license_gap=flag_gap)
            continue
        pubs = dispatch.queue_publishers(publishers, v) or None
        item = None if adopted else state.load_queue_item(sp, v)
        edit = edits.setdefault(v["id"], {})
        edit["license_gap"] = False
        try:
            if item is None or item["status"] == "rejected":
                item = _create(sp, fields, pubs, post_ref=f"studio:{post['id']}:{v['id']}")
                row["action"] = "created"
            else:
                before = (item["status"], item["sha256"], safety.meta_hash(item), item.get("schedule"))
                item = _update(sp, item["id"], fields, pubs)
                after = (item["status"], item["sha256"], safety.meta_hash(item), item.get("schedule"))
                row["action"] = "unchanged" if after == before else "updated"
                if before[0] == "approved" and item["status"] != "approved":
                    row["invalidated"] = True
                    edit["voided"] = True
                    append_event(sp, make_event(actor=actor, op="approval_invalidated", post=post_id, variant=v["id"],
                                                detail={"queue": item["id"], "status": item["status"],
                                                        "bytes": after[1] != before[1], "metadata": after[2] != before[2]}, now=now), now=now)
        except ValueError as e:
            row.update(action="skipped", problems=[str(e)])
            continue
        edit.update(queue={"root": ".", "id": item["id"]}, queue_sha=item["sha256"])
        row.update(queue=item["id"], queue_status=item["status"], problems=list(item.get("preflight_problems") or []))
    _apply_edits(sp, post_id, edits, actor, now)
    return {"post": post_id, "variants": report, "problems": [p for r in report for p in r["problems"]], "converted": converted}


def _apply_edits(sp, post_id: str, edits: dict[str, dict], actor: Actor, now: datetime) -> None:
    """Write queue refs / flags back to the post under compare-and-swap, re-applying on top of a concurrent edit (a few tries)."""
    for _ in range(4):
        post = store.load_post(sp, post_id)
        changed = False
        for v in post["variants"]:
            e = edits.get(v["id"])
            if not e:
                continue
            if "queue" in e and (v.get("queue") != e["queue"] or v.get("queue_sha") != e["queue_sha"]):
                v["queue"], v["queue_sha"] = e["queue"], e["queue_sha"]
                changed = True
            flags = list(v.get("flags") or [])
            if "license_gap" in e:
                flags = [f for f in flags if f != "license_gap"] + (["license_gap"] if e["license_gap"] else [])
            if e.get("voided"):
                flags = [f for f in flags if f != "moved_after_approval"]
            if flags != list(v.get("flags") or []):
                v["flags"], changed = flags, True
        if not changed:
            return
        try:
            store.save_post(sp, post, expected_rev=post["rev"], actor=actor, op="queue_sync", detail={"variants": sorted(edits)}, now=now)
            return
        except model.RevConflict:
            continue
    raise model.RevConflict(store.load_post(sp, post_id), f"post {post_id} kept changing while the queue sync wrote back")
