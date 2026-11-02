"""Posts group: post new|list|show|edit|attach|swap-asset|variant add|drop|critique, render, deliverables.

Edits are compare-and-swap on `rev`: `--expected-rev N` names the revision you read; a stale one exits 2 with the current rev (re-read, retry).
Nothing here approves, schedules over an approval, or sends.
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import copycheck, deliverables, ingest, ledger, model, policy, render, store, views
from ..events import Actor
from . import Blocked, add_dir, actor_for, csv, guarded, out, parse_value, project, read_json_arg, studio


def _summary(p: dict) -> dict:
    return {"id": p["id"], "stage": p["stage"], "rev": p["rev"], "idea": (p.get("idea") or {}).get("text"), "campaign": p.get("campaign"),
            "media": len(p.get("media") or []),
            "variants": [{"id": v["id"], "account": v["account"], "post_type": v["post_type"], "status": v["status"],
                          "at": (v.get("schedule") or {}).get("utc")} for v in p.get("variants", [])]}


@guarded
def cmd_new(a) -> int:
    s = studio(a, adapters=False)
    actor = actor_for(False, a.by)
    origin = None
    text = a.idea
    if a.from_file:
        res = ingest.blog(s.sp, a.from_file)
        origin = {"type": "blog", "ref": res["file"], "passages": []}
        text = text or res["title"]
    elif a.from_blog:
        passages = csv(a.passages)
        origin = {"type": "blog", "ref": a.from_blog, "passages": passages}
        if not text:
            from ...content import sources as S
            text = " ".join(S.writing_source(s.sp, a.from_blog, passages)["text"].split()[:8]) + "..." if passages else a.from_blog
    elif a.from_episode:
        origin = {"type": "episode", "ref": str(Path(a.from_episode).expanduser().resolve()), "passages": [], **({"clip": a.clip} if a.clip else {})}
        text = text or f"From {Path(a.from_episode).name}" + (f" ({a.clip})" if a.clip else "")
    if not (text or "").strip():
        raise ValueError("give --idea TEXT, or --from-file / --from-blog / --from-episode")
    post = ingest.idea(s, text, campaign=a.campaign, actor=actor, reason=a.reason or "", confidence=a.confidence, origin=origin or {"type": "idea", "ref": None, "passages": []})
    if a.copy:
        post = s.edit_post(post["id"], {"copy.base": a.copy}, expected_rev=post["rev"], actor=actor)
    out({"ok": True, "post": _summary(post), "rev": post["rev"]})
    return 0


@guarded
def cmd_list(a) -> int:
    s = studio(a, adapters=False)
    posts = s.posts(stage=a.stage, campaign=a.campaign, account=a.account, platform=a.platform, status=a.status, q=a.q)
    out({"ok": True, "total": len(posts), "posts": [_summary(p) for p in posts]})
    return 0


@guarded
def cmd_show(a) -> int:
    s = studio(a, adapters=False)
    view = s.post(a.id)
    from .. import state
    queue = {}
    for v in view["variants"]:
        item = state.load_queue_item(s.sp, v)
        if item:
            queue[v["id"]] = {"id": item["id"], "status": item["status"], "sha256": item.get("sha256"), "problems": item.get("preflight_problems") or []}
    out({"ok": True, "post": view, "queue": queue, "feedback_open": [f for f in s.feedback("open") if f.get("post") == a.id]})
    return 0


@guarded
def cmd_edit(a) -> int:
    s = studio(a, adapters=False)
    patch = {}
    for pair in a.set or []:
        k, sep, v = pair.partition("=")
        if not sep or not k:
            raise ValueError(f"--set takes path=value (got {pair!r}); paths look like copy.base or variants.v_ig.copy.caption")
        patch[k] = parse_value(v)
    if not patch:
        raise ValueError("nothing to change: pass --set path=value")
    post = s.edit_post(a.id, patch, expected_rev=a.expected_rev, actor=actor_for(False, a.by), source="cli")
    out({"ok": True, "post": _summary(post), "rev": post["rev"], "changed": sorted(patch),
         "note": "approved queue items follow only after `ave studio queue sync`, which voids an approval whose bytes or bound copy changed"})
    return 0


@guarded
def cmd_link(a) -> int:
    s = studio(a, adapters=False)
    post = s.post(a.id)
    links = [*(post.get("production") or {}).get("links", []), {"tool": a.tool, "label": a.label, "url": a.url}]
    saved = s.update_production(a.id, {"links": links}, expected_rev=a.expected_rev, actor=actor_for(False, a.by))
    out({"ok": True, "post": _summary(saved), "rev": saved["rev"], "links": links})
    return 0


@guarded
def cmd_attach(a) -> int:
    s = studio(a, adapters=False)
    sp = s.sp
    post = store.load_post(sp, a.id)
    rec = store.read_assets(sp).get(a.asset)
    if rec is None:
        raise KeyError(f"no such asset {a.asset!r} in the library")
    alt = (a.alt if a.alt is not None else rec.get("alt") or "").strip()
    if rec["kind"] == "image" and not alt:
        raise ValueError("an image needs alt text: pass --alt TEXT (or `ave studio library tag <asset> --alt TEXT`)")
    media = [dict(m) for m in post.get("media") or []]
    slot = a.slot if a.slot is not None else len(media)
    if any(m["slot"] == slot for m in media):
        raise ValueError(f"slot {slot} is taken; pass another --slot or use `post swap-asset`")
    media.append({"slot": slot, "asset": a.asset, "role": "video" if rec["kind"] == "video" else "image" if rec["kind"] == "image" else rec["kind"],
                  "alt": alt, "crop": {"grid_focus": [0.5, 0.5]} if rec["kind"] == "image" else None})
    post["media"] = sorted(media, key=lambda m: m["slot"])
    saved = store.save_post(sp, post, expected_rev=a.expected_rev if a.expected_rev is not None else post["rev"], actor=actor_for(False, a.by), op="attach",
                            detail={"asset": a.asset, "slot": slot}, now=s.now())
    out({"ok": True, "post": _summary(s.view(saved)), "rev": saved["rev"]})
    return 0


@guarded
def cmd_swap_asset(a) -> int:
    s = studio(a, adapters=True)
    post = store.load_post(s.sp, a.post)
    res = views.swap_asset(s, a.post, a.variant, a.index, a.asset, expected_rev=a.expected_rev if a.expected_rev is not None else post["rev"],
                           actor=actor_for(False, a.by))
    out({"ok": True, "post": _summary(res["post"]), "warnings": res["warnings"]})
    return 0


@guarded
def cmd_variant_add(a) -> int:
    s = studio(a, adapters=False)
    cfg = policy.account_config(s.sp, a.account)
    if not cfg.get("id"):
        raise KeyError(f"no account {a.account!r}: `ave studio account add`")
    ptype = a.type
    if cfg.get("post_types") and ptype not in cfg["post_types"]:
        raise ValueError(f"account {a.account} takes {', '.join(cfg['post_types'])}, not {ptype}")
    post = store.load_post(s.sp, a.id)
    res = s.add_variant(a.id, account=a.account, platform=cfg["platform"], post_type=ptype,
                        expected_rev=a.expected_rev if a.expected_rev is not None else post["rev"], actor=actor_for(False, a.by))
    out({"ok": True, "post": _summary(res), "variant": res["variants"][-1]["id"], "rev": res["rev"]})
    return 0


@guarded
def cmd_drop(a) -> int:
    """An agent may drop only what a human sent back with a `drop` note; a human at a terminal may drop anything."""
    from ... import humanpresence
    from ...publish import queue as pq
    from .. import state
    s = studio(a, adapters=False)
    sp = s.sp
    post = store.load_post(sp, a.id)
    human = humanpresence.is_human_terminal()
    notes = [f for f in ledger.open_feedback(sp, post=a.id, variant=a.variant, kind="drop", now=s.now())]
    if not human and not notes:
        raise Blocked("an agent may drop a post only after a human sent it back with a drop note (`ave studio feedback list`); "
                      "otherwise ask the human to drop it", needs_human=True, run=f"ave studio post drop {a.id}" + (f" --variant {a.variant}" if a.variant else ""))
    if not human and not a.variant and not any(n.get("variant") is None for n in notes):
        raise Blocked("the human's drop note names a variant: pass --variant V (an agent drops only what it was told to)")
    actor = actor_for(human, a.by)
    targets = [model.find_variant(post, a.variant)] if a.variant else [v for v in post["variants"] if not v.get("dropped")]
    rejected = []
    for v in targets:
        item = state.load_queue_item(sp, v)
        if item is not None and item["status"] not in ("published", "publishing", "rejected"):
            pq.reject(state.queue_episode(sp, v), item["id"], actor.name, a.reason or "dropped")
            rejected.append(item["id"])
    patch = {f"variants.{v['id']}.dropped": True for v in targets}
    if not a.variant:
        patch = patch or {}
    cur = store.load_post(sp, a.id)
    for v in cur["variants"]:
        if v["id"] in {t["id"] for t in targets}:
            v["dropped"] = True
    if not a.variant:
        cur["stage"] = "dropped"
    saved = store.save_post(sp, cur, expected_rev=cur["rev"], actor=actor, op="drop", detail={"reason": a.reason, "variant": a.variant}, now=s.now())
    for n in notes:
        ledger.resolve_feedback(sp, n["id"], actor.name, rev=saved["rev"], note="dropped as asked", now=s.now())
    out({"ok": True, "post": _summary(s.view(saved)), "queue_rejected": rejected, "resolved_feedback": [n["id"] for n in notes]})
    return 0


@guarded
def cmd_critique(a) -> int:
    s = studio(a, adapters=False)
    post = store.load_post(s.sp, a.id)
    rows = copycheck.critique_post(s.sp, post)
    problems = sum(len(r["problems"]) for r in rows)
    out({"ok": problems == 0, "post": a.id, "variants": rows, "note": "advice, not a gate; the human has the final say on voice"})
    return 0 if problems == 0 else 2


# ---------------------------------------------------------------------------------------------------------------- render / deliverables

def _slots(a):
    return read_json_arg(a.slots, "--slots")


@guarded
def cmd_render(a) -> int:
    s = studio(a, adapters=False)
    actor = actor_for(False, a.by)
    common = dict(template=a.template, style=a.style, fmt=a.format, by=a.by or "claude", actor=actor, keep_failed=a.keep_failed)
    if a.all:
        rows, failed = [], []
        for p in store.list_posts(s.sp):
            o = (p.get("idea") or {}).get("origin") or {}
            if p.get("media") or o.get("type") != "blog" or not o.get("passages") or p.get("stage") == "dropped":
                continue
            try:
                rows.append(render.render_post(s, p["id"], reason="studio render --all", **common))
            except render.RenderError as e:
                failed.append({"post": p["id"], "error": str(e)})
        out({"ok": not failed, "rendered": [{"post": r["post"], "attached": r["attached"], "critic_ok": r["critic"]["ok"], "assets": [x["asset"] for x in r["assets"]],
                                             "files": [x["file"] for x in r["assets"]]} for r in rows], "failed": failed})
        return 2 if failed or any(not r["attached"] for r in rows) else 0
    if not a.post:
        raise ValueError("give a post id, or --all")
    res = render.render_post(s, a.post, slots=_slots(a), source=read_json_arg(a.source, "--source"), caption=a.caption, alt_text=a.alt, reason=a.reason or "",
                             confidence=a.confidence, **common)
    out({"ok": res["attached"] or not a.attach_required, **res, "next": "LOOK at each file (Read the PNG) before you write copy; then `ave studio post variant add`"})
    return 0 if res["attached"] else 2


@guarded
def cmd_deliverables(a) -> int:
    s = studio(a, adapters=False)
    changes = []
    if a.apply:
        changes = deliverables.ensure(s.sp, a.id, actor=actor_for(False, a.by), now=s.now())
    post = store.load_post(s.sp, a.id)
    rows = deliverables.report(s.sp, post)
    problems = [p for r in rows for p in r["problems"]]
    out({"ok": not problems, "post": a.id, "converted": changes, "variants": rows,
         "next": "`ave studio deliverables <post> --apply` (or `queue sync`) converts what a platform cannot take" if problems else None})
    return 0 if not problems else 2


def add(ss) -> None:
    post = ss.add_parser("post", help="posts: new, list, show, edit, attach, swap-asset, variant add, drop, critique")
    sub = post.add_subparsers(dest="post_cmd", required=True)
    p = sub.add_parser("new", help="create a post from an idea, a Markdown file, ingested passages, or an episode")
    p.add_argument("--idea")
    p.add_argument("--from-file", help="a Markdown file: ingested as a blog, then the post records it as its origin")
    p.add_argument("--from-blog", metavar="NAME", help="an already-ingested writing file name (see `ingest blog`)")
    p.add_argument("--passages", help="comma list of passage ids (p003,p004) the post quotes")
    p.add_argument("--from-episode", help="an episode folder")
    p.add_argument("--clip", help="clip id inside --from-episode")
    p.add_argument("--campaign")
    p.add_argument("--copy", help="the base caption")
    p.add_argument("--reason")
    p.add_argument("--confidence", type=float)
    p.add_argument("--by")
    add_dir(p)
    p.set_defaults(fn=cmd_new)
    p = sub.add_parser("list")
    for k in ("stage", "campaign", "account", "platform", "status", "q"):
        p.add_argument(f"--{k}")
    add_dir(p)
    p.set_defaults(fn=cmd_list)
    p = sub.add_parser("show")
    p.add_argument("id")
    add_dir(p)
    p.set_defaults(fn=cmd_show)
    p = sub.add_parser("edit", help="change fields (dotted paths) under compare-and-swap")
    p.add_argument("id")
    p.add_argument("--expected-rev", type=int, required=True)
    p.add_argument("--set", action="append", metavar="PATH=VALUE", help="e.g. copy.base='Day 3...'  variants.v_ig.copy.caption='...'  (JSON values allowed)")
    p.add_argument("--by")
    add_dir(p)
    p.set_defaults(fn=cmd_edit)
    p = sub.add_parser("link", help="record where the work lives: a Resolve timeline by name, a Canva design by URL")
    p.add_argument("id")
    p.add_argument("--expected-rev", type=int, required=True)
    p.add_argument("--tool", choices=("resolve", "canva", "other"), required=True)
    p.add_argument("--label", required=True)
    p.add_argument("--url")
    p.add_argument("--by")
    add_dir(p)
    p.set_defaults(fn=cmd_link)
    p = sub.add_parser("attach", help="add a library asset to the post's media")
    p.add_argument("id")
    p.add_argument("--asset", required=True)
    p.add_argument("--alt")
    p.add_argument("--slot", type=int)
    p.add_argument("--expected-rev", type=int)
    p.add_argument("--by")
    add_dir(p)
    p.set_defaults(fn=cmd_attach)
    p = sub.add_parser("swap-asset", help="replace one media slot of a variant (a changed byte voids an approval)")
    p.add_argument("post")
    p.add_argument("variant")
    p.add_argument("--index", type=int, default=0)
    p.add_argument("--asset", required=True)
    p.add_argument("--expected-rev", type=int)
    p.add_argument("--by")
    add_dir(p)
    p.set_defaults(fn=cmd_swap_asset)
    v = sub.add_parser("variant", help="variants: add")
    vs = v.add_subparsers(dest="variant_cmd", required=True)
    p = vs.add_parser("add")
    p.add_argument("id")
    p.add_argument("--account", required=True)
    p.add_argument("--type", required=True, help="feed|carousel|story|reel|video|text|document|pin|short")
    p.add_argument("--expected-rev", type=int)
    p.add_argument("--by")
    add_dir(p)
    p.set_defaults(fn=cmd_variant_add)
    p = sub.add_parser("drop", help="drop a post or variant (a human, or an agent answering a human's drop note)")
    p.add_argument("id")
    p.add_argument("--variant")
    p.add_argument("--reason")
    p.add_argument("--by")
    add_dir(p)
    p.set_defaults(fn=cmd_drop)
    p = sub.add_parser("critique", help="voice and length critic over every variant's copy")
    p.add_argument("id")
    add_dir(p)
    p.set_defaults(fn=cmd_critique)

    p = ss.add_parser("render", help="render a post's card with the content renderer; outputs go to the library and onto the post")
    p.add_argument("post", nargs="?")
    p.add_argument("--all", action="store_true", help="every blog-origin post with passages and no media yet")
    p.add_argument("--template", default="quote-post")
    p.add_argument("--style", default="ink")
    p.add_argument("--format", help="1x1|4x5|9x16|16x9|2x3|191x100 (per template; default: the template's first)")
    p.add_argument("--slots", help="JSON or @file: the template's slots (not needed for a quote from ingested passages)")
    p.add_argument("--source", help="JSON or @file: {\"writing\": file, \"passages\": [...]} | {\"clip\": id} | \"episode\"")
    p.add_argument("--caption")
    p.add_argument("--alt", help="alt text (default: written from the slots)")
    p.add_argument("--reason")
    p.add_argument("--confidence", type=float)
    p.add_argument("--keep-failed", action="store_true", help="attach the render even when the critic failed")
    p.add_argument("--by")
    p.set_defaults(attach_required=True)
    add_dir(p)
    p.set_defaults(fn=cmd_render)
    p = ss.add_parser("deliverables", help="what each platform will get (format, size, hash); --apply converts what it cannot take (Instagram: JPEG)")
    p.add_argument("id")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--by")
    add_dir(p)
    p.set_defaults(fn=cmd_deliverables)
