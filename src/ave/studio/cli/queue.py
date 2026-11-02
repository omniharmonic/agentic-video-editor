"""Queue + review group: queue sync|list|dry-run, review list, approve (HUMAN, typed hash).

`queue sync` creates or updates the queue items and runs every preflight; it NEVER approves. `approve` is the human act: a terminal, the exact files
shown, and the first 8 characters of the sha256 typed (the `ave publish approve` flow).
"""

from __future__ import annotations

import getpass

from ...publish import queue as pq, safety
from .. import dispatch, ledger, state, store
from ..events import Actor, append_event, make_event
from ..sync import queue_diff
from . import Blocked, add_dir, actor_for, csv, guarded, human_gate, out, project, studio


@guarded
def cmd_sync(a) -> int:
    s = studio(a)
    ids = [a.post] if a.post else [p["id"] for p in store.list_posts(s.sp) if p.get("variants") and p.get("stage") != "dropped"]
    reports, blocked = [], 0
    for pid in ids:
        rep = s.sync_queue(pid, actor=actor_for(False, a.by))
        rows = [{k: r.get(k) for k in ("variant", "action", "queue", "queue_status", "problems", "invalidated")} for r in rep["variants"]]
        blocked += sum(1 for r in rows if r["action"] in ("blocked", "skipped") or r["problems"])
        reports.append({"post": pid, "variants": rows, "converted": rep.get("converted", [])})
    out({"ok": blocked == 0, "posts": len(reports), "reports": reports,
         "next": "Ask the human to review: `! ave studio ui` (Review) or `ave studio approve <post> <variant>`. You never approve." if not blocked else
                 "Fix each variant's problems (alt text, media, license) and sync again."})
    return 0 if blocked == 0 else 2


@guarded
def cmd_list(a) -> int:
    sp = project(a)
    items = pq.list_items(sp)
    out({"ok": True, "items": [{"id": i["id"], "status": i["status"], "title": i.get("title"), "platforms": i.get("platforms"), "account": i.get("account"),
                                "schedule": i.get("schedule"), "sha256": i.get("sha256"), "problems": i.get("preflight_problems") or []} for i in items]})
    return 0


@guarded
def cmd_dry_run(a) -> int:
    s = studio(a)
    post = store.load_post(s.sp, a.post)
    rows = []
    for v in post["variants"]:
        if v.get("dropped"):
            continue
        item = state.load_queue_item(s.sp, v)
        row = {"variant": v["id"], "account": v["account"], "queue": (v.get("queue") or {}).get("id"), "queue_status": item["status"] if item else None}
        if item is None:
            row.update(would_post=False, gates=[{"code": "no_queue", "message": "no queue item: run `ave studio queue sync`"}])
        else:
            gates = [b.as_dict() for b in dispatch.blockers(s.sp, post, v, now=s.now(), publishers=s.publishers, host=s.host, send=False)]
            ep = state.queue_episode(s.sp, v)
            res = pq.run(ep, item["id"], send=False, publishers=dispatch.queue_publishers(s.publishers, v) or None)
            row.update(would_post=not gates and res["ok"], gates=gates, queue_run={"ok": res["ok"], "blocked": res.get("blocked")})
        rows.append(row)
    out({"ok": True, "post": a.post, "dry_run": True, "variants": rows, "note": "nothing was sent; `would_post` also needs a slot (`schedule`) and an attested live account at send time"})
    return 0


@guarded
def cmd_review_list(a) -> int:
    s = studio(a, adapters=False)
    rows = []
    for p in s.posts(status="in_review"):
        for v in p["variants"]:
            if v["status"] != "in_review":
                continue
            item = state.load_queue_item(s.sp, v)
            rows.append({"post": p["id"], "variant": v["id"], "account": v["account"], "post_type": v["post_type"], "sha256": item.get("sha256") if item else None,
                         "reason": v.get("status_reason"), "idea": (p.get("idea") or {}).get("text")})
    out({"ok": True, "waiting": rows, "next": "The human approves: `! ave studio ui` (hash-bound PIN flow) or `! ave studio approve <post> <variant>` (typed hash)."})
    return 0


@guarded
def cmd_approve(a) -> int:
    human_gate(a, "approving a post", f"ave studio approve {a.post} {a.variant}")
    s = studio(a)
    post = store.load_post(s.sp, a.post)
    v = next((x for x in post["variants"] if x["id"] == a.variant), None)
    if v is None:
        raise KeyError(f"post {a.post} has no variant {a.variant!r}")
    item = state.load_queue_item(s.sp, v)
    if item is None:
        raise Blocked("this variant has no queue item yet: run `ave studio queue sync` first")
    ep = state.queue_episode(s.sp, v)
    if item.get("meta_v") not in (2, 3):
        raise Blocked("this queue item is not a studio item (an adopted or legacy episode item): run `ave studio queue sync` to build one, then approve that")
    diffs = queue_diff(s.sp, post, v, item)
    if diffs:
        raise Blocked("the queue item does NOT match the post you would be reviewing (the approval would sign the queue item, not the post): "
                      "run `ave studio queue sync` first, then approve", diff=diffs)
    sha = pq.current_sha(ep, item)
    files = [{"file": f, "sha256": safety.sha256_file(p)} for f, p in zip(item.get("files") or [item.get("video")], pq.resolve_files(ep, item))]
    shown = {k: item.get(k) for k in ("id", "platforms", "account", "post_type", "title", "caption", "first_comment", "link_url", "schedule", "ai_disclosure",
                                      "tags")}
    out({"approving": shown, "files": files, "alt_texts": item.get("alt_texts"), "sha256": sha,
         "look": "Open each file listed above and look at it before you type anything."})
    confirm = input("Type the first 8 characters of the sha256 above to approve: ").strip()
    try:
        done = pq.approve(ep, item["id"], a.by or getpass.getuser(), confirm, publishers=dispatch.queue_publishers(s.publishers, v) or None,
                          source="studio-cli", bind=dispatch.approval_bind(s.sp, v))
    except ValueError as e:
        raise Blocked(str(e)) from None
    append_event(s.sp, make_event(actor=Actor("human", a.by or getpass.getuser()), op="approved", post=a.post, variant=a.variant, rev=post["rev"], after_sha=sha,
                                  detail={"queue_id": item["id"], "source": "studio-cli"}, now=s.now()), now=s.now())
    out({"ok": True, "id": done["id"], "status": done["status"], "approval": done["approvals"][-1]})
    return 0


def add(ss) -> None:
    q = ss.add_parser("queue", help="queue: sync (never approves), list, dry-run")
    sub = q.add_subparsers(dest="queue_cmd", required=True)
    p = sub.add_parser("sync", help="create/update queue items for every variant (runs all preflight; NEVER approves)")
    p.add_argument("--post", help="one post (default: all with variants)")
    p.add_argument("--by")
    add_dir(p)
    p.set_defaults(fn=cmd_sync)
    p = sub.add_parser("list")
    add_dir(p)
    p.set_defaults(fn=cmd_list)
    p = sub.add_parser("dry-run", help="every gate and the queue's own dry run for one post; sends nothing")
    p.add_argument("post")
    add_dir(p)
    p.set_defaults(fn=cmd_dry_run)
    r = ss.add_parser("review", help="review: list what waits for the human")
    rs = r.add_subparsers(dest="review_cmd", required=True)
    p = rs.add_parser("list")
    add_dir(p)
    p.set_defaults(fn=cmd_review_list)
    p = ss.add_parser("approve", help="HUMAN: approve one variant (a terminal, the files shown, the typed hash)")
    p.add_argument("post")
    p.add_argument("variant")
    p.add_argument("--by")
    add_dir(p)
    p.set_defaults(fn=cmd_approve)
