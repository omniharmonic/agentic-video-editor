"""Bulk retime for the planner UI: shift several posts by N days, set exact times (also how an undo restores the old ones), spread them
over the next free slots, drop them back to Unscheduled, or move unqueued drafts to another account.

Every item goes through the SAME checked path as a single drag (`Studio.move` / `unschedule`: slot taken, minimum gap, quiet hours,
daily limit, past, locked), so a bulk call can never do what one move could not. Hard rules, each with a test:

  * nothing here approves, rejects or sends: a retime of an APPROVED post keeps the approval (the schedule is not in the signed hash) and
    `slots.move` records the event and sets `moved_after_approval`; changing a variant's ACCOUNT is refused when it has a queue item,
    because the approval is bound to the account;
  * a refusal on one item never stops the others, and each refusal says why (`reason`, `code`);
  * `if_utc` on an item makes it a compare-and-set on the current slot, so an undo cannot trample a move made since.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from ..schedule import slots as S
from . import policy, store
from .clock import iso_utc
from .events import Actor
from .model import RevConflict, check_id, find_variant

MAX_ITEMS = 100
OPS = ("shift", "set", "slots", "unschedule", "account")


class BulkError(ValueError):
    """The request itself is malformed (the API's 400)."""


def _sched(v: Mapping) -> dict | None:
    s = v.get("schedule")
    return {"local": s.get("local"), "tz": s.get("tz"), "utc": s.get("utc")} if s and s.get("utc") else None


def free_slots(studio, post_id: str, variant_id: str, *, count: int = 6, after: datetime | None = None) -> dict:
    """The next `count` free rule slots for this variant's account (the ones a move would accept), for the reschedule picker."""
    sp = studio.sp
    check_id(post_id, "post id")
    post = store.load_post(sp, post_id)
    v = find_variant(post, variant_id)
    acct = v["account"]
    now = studio.now()
    cfg = S.load_rules(sp, acct)
    out, cursor = [], max(after or now, now)
    ignore = f"{post_id}:{variant_id}"
    for _ in range(max(1, min(int(count), 20))):
        nxt = S.next_slot(sp, acct, after=cursor, post_type=v.get("post_type"), now=now, ignore=ignore)
        if nxt is None:
            break
        out.append({"slot": nxt["slot_id"], "local": nxt["local"], "tz": nxt["tz"], "utc": nxt["utc"], "at": nxt["at"], "rule": nxt["rule"]})
        cursor = datetime.fromisoformat(nxt["utc"].replace("Z", "+00:00"))
    return {"account": acct, "tz": cfg["tz"], "has_rules": bool(cfg["rules"]), "slots": out}


def run(studio, op: str, items: list, *, actor: Actor, days: int | None = None, account: str | None = None) -> dict:
    if op not in OPS:
        raise BulkError(f"op must be one of: {', '.join(OPS)}")
    if not isinstance(items, list) or not items or len(items) > MAX_ITEMS or not all(isinstance(i, dict) for i in items):
        raise BulkError(f"'items' must be a list of 1 to {MAX_ITEMS} {{post, variant}} objects")
    if op == "shift" and (isinstance(days, bool) or not isinstance(days, int) or not -366 <= days <= 366 or days == 0):
        raise BulkError("'days' must be a non-zero integer between -366 and 366")
    if op == "account" and not (isinstance(account, str) and account.strip()):
        raise BulkError("'account' is required to move posts to an account")
    sp, now = studio.sp, studio.now()
    work = []
    for it in items:
        pid, vid = it.get("post"), it.get("variant")
        try:
            check_id(pid, "post id")
            check_id(vid, "variant id")
        except ValueError as e:
            raise BulkError(str(e)) from None
        work.append(it)
    # Order matters for the gap/slot checks between the batch's own members: moving later keeps the latest first, earlier keeps the earliest first.
    def key(it):
        try:
            v = find_variant(store.load_post(sp, it["post"]), it["variant"])
            u = S.schedule_utc(v)
        except (FileNotFoundError, KeyError, ValueError):
            u = None
        return u or datetime.max.replace(tzinfo=timezone.utc)
    work.sort(key=key, reverse=(op == "shift" and (days or 0) > 0))
    done, refused = [], []
    cursor: dict[str, datetime] = {}
    for it in work:
        pid, vid = it["post"], it["variant"]
        try:
            post = store.load_post(sp, pid)
            v = find_variant(post, vid)
            want_rev = it.get("expected_rev")
            if isinstance(want_rev, int) and not isinstance(want_rev, bool) and want_rev != post["rev"]:
                raise S.MoveRefused(f"{pid} changed since you looked at it (rev {post['rev']}, not {want_rev})", "conflict.rev", post)
            if "if_utc" in it and (_sched(v) or {}).get("utc") != it["if_utc"]:             # null = "still in Unscheduled"
                raise S.MoveRefused("it was moved again since, so it was left where it is", "conflict.changed", post)
            before = _sched(v)
            if op == "unschedule":
                if before is None:
                    raise S.MoveRefused("it is already in Unscheduled", "noop")
                res = studio.unschedule(pid, vid, expected_rev=post["rev"], actor=actor)
            elif op == "account":
                res = _reassign(studio, post, v, account.strip(), actor)
            else:
                to, snap = _target(sp, op, it, v, days, now, cursor)
                if to is None:
                    res = studio.unschedule(pid, vid, expected_rev=post["rev"], actor=actor)
                else:
                    res = studio.move(pid, vid, to, expected_rev=post["rev"], actor=actor, snap=snap)
            nv = find_variant(res["post"], vid)
            after = _sched(nv)
            if op == "slots" and after:
                cursor[nv["account"]] = datetime.fromisoformat(after["utc"].replace("Z", "+00:00"))
            done.append({"post": pid, "variant": vid, "rev": res["post"]["rev"], "before": before, "after": after,
                         "account": nv.get("account"), "status": nv.get("status"), "flags": nv.get("flags") or [],
                         "warnings": list(res.get("warnings") or [])})
        except S.MoveRefused as e:
            refused.append({"post": pid, "variant": vid, "reason": str(e), "code": e.code})
        except RevConflict as e:
            refused.append({"post": pid, "variant": vid, "reason": str(e), "code": "conflict.rev"})
        except (ValueError, KeyError, FileNotFoundError) as e:
            refused.append({"post": pid, "variant": vid, "reason": str(e.args[0]) if isinstance(e, KeyError) and e.args else str(e), "code": "refused"})
    return {"op": op, "done": done, "refused": refused, "approved": 0, "at": iso_utc(now)}


def _target(sp, op: str, it: Mapping, v: Mapping, days: int | None, now: datetime, cursor: dict) -> tuple[dict | None, str]:
    if op == "shift":
        s = v.get("schedule")
        if not (s and s.get("local") and s.get("tz")):
            raise S.MoveRefused("it has no time to shift: it is in Unscheduled", "noop")
        local = datetime.fromisoformat(s["local"]) + timedelta(days=days)
        return {"local": local.strftime(S.LOCAL_FMT), "tz": s["tz"]}, "none"
    if op == "slots":
        after = max(cursor.get(v["account"], now), now, S.schedule_utc(v) or now)      # a post that has a time moves on to the slot AFTER it; one with none starts from now
        nxt = S.next_slot(sp, v["account"], after=after, post_type=v.get("post_type"), now=now, ignore=f"{it['post']}:{it['variant']}")
        if nxt is None:
            raise S.MoveRefused(f"{v['account']} has no free slot in the next 120 days (set schedule rules, or pick a time)", "conflict.day")
        return {"slot": nxt["slot_id"]}, "slot"
    to = it.get("to")                                           # op == "set"
    if to is None:
        return None, "none"
    if not isinstance(to, dict):
        raise BulkError("'to' must be {local, tz}, {slot} or null")
    snap = it.get("snap") or "none"
    if snap not in ("none", "slot"):
        raise BulkError("snap must be 'none' or 'slot'")
    return to, snap


def _reassign(studio, post: dict, v: dict, target: str, actor: Actor) -> dict:
    """Give an UNQUEUED variant to another account of the same platform. A variant with a queue item is refused: its approval (and its
    queue item) are bound to the old account, and nothing here may carry an approval across."""
    sp = studio.sp
    cfg = policy.account_config(sp, target)
    if not cfg.get("id"):
        raise S.MoveRefused(f"there is no account {target!r}", "conflict.account")
    if v["account"] == target:
        raise S.MoveRefused(f"it is already on {target}", "noop")
    if v.get("queue"):
        raise S.MoveRefused(f"it is already in the review queue for {v['account']}, and an approval is bound to its account. "
                            "Drop this one and add a variant for the other account", "conflict.account")
    if cfg.get("platform") != v.get("platform"):
        raise S.MoveRefused(f"{target} is {cfg.get('platform')}, this is a {v.get('platform')} post: only accounts of the same platform can take it", "conflict.account")
    if v.get("post_type") not in (cfg.get("post_types") or [v.get("post_type")]):
        raise S.MoveRefused(f"{target} does not take {v.get('post_type')} posts", "conflict.account")
    if any(x is not v and x["id"] != v["id"] and x["account"] == target and x["post_type"] == v["post_type"] and not x.get("dropped") for x in post["variants"]):
        raise S.MoveRefused(f"this post already has a {v['post_type']} variant for {target}", "conflict.account")
    rev = post["rev"]
    if v.get("schedule"):                                       # the old account's slot means nothing on the new one
        res = studio.unschedule(post["id"], v["id"], expected_rev=rev, actor=actor)
        rev = res["post"]["rev"]
    edited = studio.edit_post(post["id"], {f"variants.{v['id']}.account": target}, expected_rev=rev, actor=actor, source="ui")
    return {"post": edited, "warnings": []}
