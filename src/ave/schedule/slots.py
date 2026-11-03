"""Slots, the calendar, and retiming (spec 2.6, 7.3, 7.5).

Time zones: a post stores `local` + IANA `tz` + resolved `utc`. `resolve_local` follows spec 2.6: a nonexistent local time (spring
forward) shifts forward to the next valid minute; an ambiguous one (fall back) takes the first occurrence (`fold=0`). The scheduler
compares UTC only.

Retiming never touches the approval: studio queue items are `meta_v` 2, whose metadata hash excludes `schedule` (safety.META_FIELDS_V2).
An AGENT that retimes an already-scheduled, approved variant is not applied: it becomes a proposal (policy `agent_retime_approved`).
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterator
from zoneinfo import ZoneInfo

from ..publish import queue as pq
from ..studio import ledger, model, policy, state, store
from ..studio.clock import Clock, SystemClock, iso_utc, parse_ts
from ..studio.events import AGENT, Actor, changes
from . import proposals
from .rules import DOW, account_tz, load_rules, skipped_dates

UTC = timezone.utc
LOCAL_FMT = "%Y-%m-%dT%H:%M"
LOCKED_STATUSES = ("published", "publishing", "dropped")


class MoveRefused(ValueError):
    """A retime/swap that cannot be applied. `code` is the API error code (`conflict.slot`, `conflict.gap`, `conflict.quiet`,
    `conflict.past`, `conflict.limit`, `conflict.day`, `conflict.account`, `conflict.rev`, `locked`); `latest` is the current post
    (the HTTP 409 body)."""

    def __init__(self, message: str, code: str = "conflict.slot", latest: dict | None = None):
        super().__init__(message)
        self.code, self.latest = code, latest


# ------------------------------------------------------------------------------------------------ time

def resolve_local(local: str, tz: str) -> datetime:
    """`'2026-11-01T01:30'` in `tz` -> an AWARE datetime in that zone (spec 2.6 DST rules)."""
    z = ZoneInfo(tz)
    naive = datetime.fromisoformat(local)
    if naive.tzinfo is not None:
        raise ValueError(f"local time {local!r} must not carry an offset (pass the zone as tz)")
    naive = naive.replace(microsecond=0)

    def valid(n: datetime) -> bool:
        return n.replace(tzinfo=z, fold=0).astimezone(UTC).astimezone(z).replace(tzinfo=None) == n

    probe = naive
    for _ in range(24 * 60):                      # a gap is at most a few hours; walk forward minute by minute
        if valid(probe):
            return probe.replace(tzinfo=z, fold=0)
        probe += timedelta(minutes=1)
    raise ValueError(f"cannot resolve local time {local!r} in {tz}")


def make_schedule(at: datetime, *, slot: str | None, set_by: str, locked: bool = False) -> dict:
    """The `variant.schedule` block for an aware datetime `at` (its zone becomes `tz`)."""
    tzname = getattr(at.tzinfo, "key", None)
    if not tzname:
        raise ValueError("schedule needs a named IANA zone")
    return {"local": at.strftime(LOCAL_FMT), "tz": tzname, "utc": iso_utc(at), "slot": slot, "locked": locked, "set_by": set_by}


def slot_id(account_id: str, at_local: datetime) -> str:
    return f"{account_id}:{at_local.strftime(LOCAL_FMT)}"


def schedule_utc(variant: dict) -> datetime | None:
    s = variant.get("schedule") or {}
    if not s.get("utc"):
        return None
    try:
        return parse_ts(s["utc"])
    except ValueError:
        return None


# ------------------------------------------------------------------------------------------------ rule slots

def iter_slots(sp, account_id: str, start: datetime, end: datetime, post_type: str | None = None) -> Iterator[dict]:
    """Rule instances with start <= utc < end, in time order. Each: {account, slot_id, rule, at (aware local), utc, local, tz, post_types}."""
    cfg = load_rules(sp, account_id)
    if not cfg["rules"]:
        return
    z = ZoneInfo(cfg["tz"])
    skip = skipped_dates(cfg)
    d, last = start.astimezone(z).date() - timedelta(days=1), end.astimezone(z).date() + timedelta(days=1)
    found: dict[tuple, dict] = {}
    while d <= last:
        if d.isoformat() not in skip:
            for r in cfg["rules"]:
                if DOW[d.weekday()] not in r["dow"]:
                    continue
                if post_type and r.get("post_types") and post_type not in r["post_types"]:
                    continue
                at = resolve_local(f"{d.isoformat()}T{r['time']}", cfg["tz"])
                utc = at.astimezone(UTC)
                if start <= utc < end:
                    found[(utc, r["id"])] = {"account": account_id, "slot_id": slot_id(account_id, at), "rule": r["id"], "at": at,
                                             "utc": utc, "local": at.strftime(LOCAL_FMT), "tz": cfg["tz"],
                                             "post_types": list(r.get("post_types") or [])}
        d += timedelta(days=1)
    for k in sorted(found):
        yield found[k]


# ------------------------------------------------------------------------------------------------ occupancy and conflicts

@dataclass(frozen=True)
class Occupant:
    post: str
    variant: str
    utc: datetime


def occupants(sp, account_id: str, posts: list[dict] | None = None) -> list[Occupant]:
    """Every non-dropped scheduled variant of the account (published ones still occupy their slot)."""
    out = []
    for p in posts if posts is not None else store.list_posts(sp):
        for v in p.get("variants") or []:
            if v.get("account") != account_id or v.get("dropped"):
                continue
            u = schedule_utc(v)
            if u is not None:
                out.append(Occupant(p["id"], v["id"], u))
    return sorted(out, key=lambda o: (o.utc, o.post, o.variant))


def _ignored(o: Occupant, ignore: str | None) -> bool:
    return bool(ignore) and ignore in (o.variant, f"{o.post}:{o.variant}")


def in_quiet_hours(cfg: dict, at_utc: datetime, tz: str) -> bool:
    t = at_utc.astimezone(ZoneInfo(tz)).strftime("%H:%M")
    for q in cfg.get("quiet_hours") or []:
        a, b = str(q.get("from")), str(q.get("to"))
        if (a <= t < b) if a <= b else (t >= a or t < b):
            return True
    return False


NEVER = datetime(9999, 12, 31, 23, 59, tzinfo=UTC)


def quiet_hours_end(cfg: dict, at_utc: datetime, tz: str) -> datetime | None:
    """The first instant after `at_utc` outside every quiet window (None when not in quiet hours). A window set that covers ALL day never ends:
    that returns `NEVER`, and the sender FAILS CLOSED (defers) instead of reading "no end found" as "not quiet"."""
    if not in_quiet_hours(cfg, at_utc, tz):
        return None
    t = at_utc
    for _ in range(24 * 60 // 5 + 1):
        t += timedelta(minutes=5)
        if not in_quiet_hours(cfg, t, tz):
            return t
    return NEVER


def _problems(sp, account_id: str, at_utc: datetime, occ: list[Occupant], *, ignore: str | None, now: datetime) -> list[tuple[str, str]]:
    cfg = policy.account_config(sp, account_id)
    if not cfg.get("id"):
        return [("conflict.account", f"unknown account {account_id!r}")]
    tz = account_tz(sp, account_id)
    out: list[tuple[str, str]] = []
    if at_utc <= now:
        out.append(("conflict.past", "that time is in the past"))
    if in_quiet_hours(cfg, at_utc, tz):
        out.append(("conflict.quiet", f"inside quiet hours for {account_id}"))
    limits = cfg.get("limits") or {}
    gap = int(limits.get("min_gap_min") or 0)
    per_day = limits.get("max_per_day")
    others = [o for o in occ if not _ignored(o, ignore)]
    for o in others:
        if o.utc == at_utc:
            out.append(("conflict.slot", f"slot taken by {o.post}"))
        elif gap and abs((o.utc - at_utc).total_seconds()) < gap * 60:
            out.append(("conflict.gap", f"too close to {o.post} (minimum gap {gap} min)"))
    if isinstance(per_day, int) and per_day > 0:
        zone = ZoneInfo(tz)
        day = at_utc.astimezone(zone).date()
        n = sum(1 for o in others if o.utc.astimezone(zone).date() == day)
        if n >= per_day:
            out.append(("conflict.limit", f"{account_id} already has {n} post(s) on {day} (limit {per_day} per day)"))
    return out


def check_move(sp, account_id: str, at_utc: datetime, *, ignore: str | None = None, now: datetime | None = None,
               posts: list[dict] | None = None) -> list[str]:
    """Why `account_id` cannot take a post at `at_utc`: slot taken, too close to another post (min gap), quiet hours, daily limit, in the
    past. `ignore` = a variant id (or `post:variant`) whose own current slot does not count (the one being moved). Empty = free."""
    now = now or SystemClock().now()
    return [m for _, m in _problems(sp, account_id, at_utc.astimezone(UTC), occupants(sp, account_id, posts), ignore=ignore, now=now)]


def next_slot(sp, account_id: str, *, after: datetime, post_type: str | None = None, now: datetime | None = None,
              ignore: str | None = None, horizon_days: int = 120) -> dict | None:
    """The first rule slot strictly after `after` that passes `check_move`, or None within `horizon_days`.
    Returns {account, slot_id, rule, at (ISO, local offset), utc (ISO Z), local, tz, post_types}."""
    now = now or SystemClock().now()
    occ = occupants(sp, account_id)
    after = after.astimezone(UTC)
    for s in iter_slots(sp, account_id, after + timedelta(seconds=1), after + timedelta(days=horizon_days), post_type):
        if not _problems(sp, account_id, s["utc"], occ, ignore=ignore, now=now):
            return {**s, "at": s["at"].isoformat(), "utc": iso_utc(s["utc"])}
    return None


# ------------------------------------------------------------------------------------------------ calendar

def _first_sentence(text: str, cap: int = 80) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    line = text.splitlines()[0]
    m = re.match(r"(.+?[.!?])(\s|$)", line)
    return (m.group(1) if m else line).rstrip(".")[:cap]


def _thumb(assets: dict, post: dict, variant: dict | None) -> str | None:
    media = model.effective_media(post, variant) if variant else post.get("media") or []
    for m in media:
        rec = assets.get(m.get("asset"))
        if rec and rec.get("file"):
            return "/files/" + rec["file"]
    return None


def account_paused(sp, account_id: str) -> bool:
    from ..studio import dispatch
    return dispatch.is_paused(sp) or dispatch.is_paused(sp, account_id)


def _accounts(sp) -> list[dict]:
    out = []
    for p in sorted(sp.accounts_dir.glob("*.yaml")) if sp.accounts_dir.is_dir() else []:
        cfg = policy.account_config(sp, p.stem)
        if cfg.get("id"):
            out.append(cfg)
    return out


def calendar(sp, start: date, end: date, accounts: list[str] | None = None, *, now: datetime | None = None) -> dict:
    """The 7.3 calendar JSON (tests/data_studio/api/GET_calendar.json): per-account lanes, scheduled cards by LOCAL day (inclusive of
    both `start` and `end`), free/taken rule slots, the unscheduled tray, and pending proposals."""
    from ..studio import dispatch
    now = now or SystemClock().now()
    cfgs = [c for c in _accounts(sp) if not accounts or c["id"] in accounts]
    ids = {c["id"] for c in cfgs}
    assets = store.read_assets(sp)
    posts = store.list_posts(sp)
    fb = ledger.open_feedback(sp, now=now)
    days: dict[str, list] = {}
    unscheduled: list[dict] = []
    lo = datetime.combine(start, datetime.min.time(), tzinfo=UTC) - timedelta(days=1)
    hi = datetime.combine(end, datetime.min.time(), tzinfo=UTC) + timedelta(days=2)

    def card(p: dict, v: dict, ex: dict) -> dict:
        eff = model.effective_copy(p, v)
        return {"post": p["id"], "variant": v["id"], "status": ex["status"],
                "title": _first_sentence(eff["caption"]) or (p.get("idea") or {}).get("text", ""),
                "thumb": _thumb(assets, p, v), "rev": p["rev"], "account": v["account"], "campaign": p.get("campaign")}

    for p in posts:
        if p.get("stage") == "dropped":
            continue
        mine = [f for f in fb if f.get("post") == p["id"]]
        if not p.get("variants") and p.get("stage") == "draft" and not accounts:
            unscheduled.append({"post": p["id"], "variant": None, "status": "draft", "title": _first_sentence((p.get("copy") or {}).get("base", "")),
                                "thumb": _thumb(assets, p, None), "rev": p["rev"], "account": None, "campaign": p.get("campaign")})
        for v in p.get("variants") or []:
            if v.get("account") not in ids or v.get("dropped"):
                continue
            ex = state.explain(v, state.load_queue_item(sp, v), mine, now, sp)
            u = schedule_utc(v)
            if u is None:
                if ex["status"] not in ("published", "dropped"):
                    unscheduled.append(card(p, v, ex))
                continue
            at = u.astimezone(ZoneInfo(account_tz(sp, v["account"])))
            if not (start <= at.date() <= end):
                continue
            c = card(p, v, ex)
            c.update(at=at.isoformat(), tz=at.tzinfo.key, slot=(v.get("schedule") or {}).get("slot"),
                     locked=bool((v.get("schedule") or {}).get("locked")) or ex["status"] in LOCKED_STATUSES,
                     flags=sorted(set(v.get("flags") or []) | set(ex["flags"])))
            days.setdefault(at.date().isoformat(), []).append(c)
    for lst in days.values():
        lst.sort(key=lambda c: (c["at"], c["post"]))
    slots: dict[str, list] = {}
    for cfg in cfgs:
        taken = {o.utc for o in occupants(sp, cfg["id"], posts)}
        for s in iter_slots(sp, cfg["id"], lo, hi):
            d = s["at"].date()
            if start <= d <= end:
                slots.setdefault(d.isoformat(), []).append({"account": cfg["id"], "at": s["at"].isoformat(), "rule": s["rule"],
                                                            "free": s["utc"] not in taken})
    return {
        "tz": sp.config().get("tz"), "cursor": changes(sp)["cursor"],
        "accounts": [{"id": c["id"], "platform": c.get("platform"), "live": dispatch.account_live(sp, c["id"]),
                      "paused": account_paused(sp, c["id"]), "brand": c.get("brand") or None, "label": c.get("label") or c["id"]} for c in cfgs],
        "days": dict(sorted(days.items())), "slots": dict(sorted(slots.items())),
        "unscheduled": sorted(unscheduled, key=lambda c: (c["post"], c["variant"] or "")),
        "proposals": proposals.pending(sp),
    }


# ------------------------------------------------------------------------------------------------ retiming

def _clock_now(clock: Clock | None, now: datetime | None) -> datetime:
    return now or (clock or SystemClock()).now()


def _status(sp, variant: dict, now: datetime) -> dict:
    return state.explain(variant, state.load_queue_item(sp, variant), [], now, sp)


def _is_approved(sp, variant: dict) -> bool:
    item = state.load_queue_item(sp, variant)
    return bool(item) and item.get("status") == "approved"


def _target(sp, variant: dict, to: dict, snap: str, occ: list[Occupant], ignore: str, now: datetime) -> tuple[datetime, str | None]:
    """Resolve a `to` ({"local","tz"} or {"slot": slot_id}) to (aware local datetime, rule id or None)."""
    acct = variant["account"]
    if "slot" in to:
        sid = str(to["slot"])
        a, _, local = sid.partition(":")
        if a != acct:
            raise MoveRefused(f"slot {sid} belongs to {a}, not {acct}", "conflict.slot")
        at = resolve_local(local, load_rules(sp, acct)["tz"])
        utc = at.astimezone(UTC)
        rule = next((s["rule"] for s in iter_slots(sp, acct, utc, utc + timedelta(minutes=1))), None)
        if rule is None:
            raise MoveRefused(f"{sid} is not a slot of {acct}'s rules", "conflict.slot")
        return at, rule
    if not to.get("local") or not to.get("tz"):
        raise ValueError("`to` needs {local, tz} or {slot}")
    if snap not in ("slot", "none"):
        raise ValueError("snap must be 'slot' or 'none'")
    at = resolve_local(to["local"], to["tz"])
    cfg = load_rules(sp, acct)
    if snap == "none" or not cfg["rules"]:
        return at, None
    rtz = ZoneInfo(cfg["tz"])
    day0 = at.astimezone(rtz).replace(hour=0, minute=0, second=0, microsecond=0)
    day_slots = list(iter_slots(sp, acct, day0.astimezone(UTC), (day0 + timedelta(days=1)).astimezone(UTC), variant.get("post_type")))
    hit = next((s for s in day_slots if s["utc"] == at.astimezone(UTC)), None)
    if hit:
        return hit["at"], hit["rule"]
    free = [s for s in day_slots if not _problems(sp, acct, s["utc"], occ, ignore=ignore, now=now)]
    pick = [s for s in free if s["utc"] >= at.astimezone(UTC)] or free
    if not pick:
        raise MoveRefused(f"no free slot for {acct} on {at.astimezone(rtz).date()}", "conflict.day")
    return pick[0]["at"], pick[0]["rule"]


def _mirror(sp, variant: dict, utc: datetime | None, publishers) -> list[str]:
    """Copy the new time onto the queue item for display (studio items only: `meta_v` 2 excludes `schedule` from the approval hash, so
    this can never void an approval; a legacy/adopted item would be re-hashed, so it is left alone)."""
    ref = variant.get("queue")
    if not ref:
        return []
    ep = state.queue_episode(sp, variant)
    try:
        item = pq.load(ep, ref["id"])
        if item.get("meta_v") not in (2, 3):
            return ["queue item is not meta_v 2 (adopted/legacy); its own schedule field was not changed"]
        before = item["status"]
        out = pq.update(ep, ref["id"], schedule=utc.isoformat() if utc else None, publishers=publishers)
        if before == "approved" and out["status"] != "approved":      # cannot happen for meta_v 2; never hide it if it does
            return [f"retime changed the queue item to {out['status']}: approval needs review"]
    except (ValueError, FileNotFoundError) as e:
        return [f"queue item not updated: {e}"]
    return []


def _note_slot(sp, saved_post: dict, variant_id: str, actor: Actor, now: datetime) -> None:
    """A sanctioned write of `variant.schedule`: tell the sealed control ledger (dispatch.record_slot) so the sender can tell it from a hand edit."""
    from ..studio import dispatch
    dispatch.record_slot(sp, model.find_variant(saved_post, variant_id), actor, now)


def _save(sp, post: dict, expected_rev: int, actor: Actor, op: str, detail: dict, variant: str, now: datetime) -> dict:
    try:
        return store.save_post(sp, post, expected_rev=expected_rev, actor=actor, op=op, detail=detail, variant=variant, now=now)
    except model.RevConflict as c:
        raise MoveRefused(str(c), "conflict.rev", c.latest) from c


def _load_checked(sp, post_id: str, expected_rev: int) -> dict:
    post = store.load_post(sp, post_id)
    if post["rev"] != expected_rev:
        raise MoveRefused(f"post {post_id} is at rev {post['rev']}, not {expected_rev}", "conflict.rev", post)
    return post


def _refuse_if_locked(sp, post: dict, v: dict, actor: Actor, now: datetime) -> None:
    ex = _status(sp, v, now)
    if ex["status"] in LOCKED_STATUSES:
        raise MoveRefused(f"{v['id']} is {ex['status']} and cannot be moved", "locked", post)
    if (v.get("schedule") or {}).get("locked") and actor.kind != "human":
        raise MoveRefused(f"{v['id']} is locked in the calendar", "locked", post)


def move(sp, post_id: str, variant_id: str, to: dict, *, expected_rev: int, actor: Actor = AGENT, snap: str = "slot",
         clock: Clock | None = None, now: datetime | None = None, publishers=None) -> dict:
    """Retime one variant. `to` = {"local": "2026-11-03T09:00", "tz": "America/Denver"} or {"slot": "<account>:<local>"}; `snap="slot"`
    snaps a time to the day's next free rule slot when the account has rules, `"none"` takes the exact time.

    Returns {"post", "warnings", "applied": True}. Refusals raise `MoveRefused(code=...)` (the API's 409 body); a stale `expected_rev`
    raises it with code `conflict.rev` and `latest`. An AGENT retiming a variant whose queue item is APPROVED and already scheduled is not
    applied (policy `agent_retime_approved: propose_only`): a proposal is written and {"applied": False, "proposal": id} returned.
    A HUMAN move applies and marks the variant `moved_after_approval`."""
    now = _clock_now(clock, now)
    post = _load_checked(sp, post_id, expected_rev)
    v = model.find_variant(post, variant_id)
    _refuse_if_locked(sp, post, v, actor, now)
    occ = occupants(sp, v["account"])
    ignore = f"{post_id}:{variant_id}"
    at, rule = _target(sp, v, to, snap, occ, ignore, now)
    probs = _problems(sp, v["account"], at.astimezone(UTC), occ, ignore=ignore, now=now)
    if probs:
        raise MoveRefused(probs[0][1], probs[0][0], post)
    approved = _is_approved(sp, v)
    if approved and v.get("schedule") and actor.kind == "agent" and policy.agent_retime_approved(sp) == "propose_only":
        pid = proposals.create(sp, actor.name, [{"post": post_id, "variant": variant_id,
                                                 "to": {"local": at.strftime(LOCAL_FMT), "tz": at.tzinfo.key},
                                                 "why": "agent retime of an approved post (needs a human accept)"}], now=now)
        return {"post": post, "warnings": ["approved post: the retime was written as a proposal for a human to accept"],
                "applied": False, "proposal": pid}
    before = (v.get("schedule") or {}).get("utc")
    new = copy.deepcopy(post)
    nv = model.find_variant(new, variant_id)
    nv["schedule"] = make_schedule(at, slot=rule, set_by="human" if actor.kind == "human" else "agent",
                                   locked=bool((v.get("schedule") or {}).get("locked")))
    if approved and actor.kind == "human" and "moved_after_approval" not in nv["flags"]:
        nv["flags"] = [*nv["flags"], "moved_after_approval"]
    saved = _save(sp, new, expected_rev, actor, "move", {"from": before, "to": nv["schedule"]["utc"], "snap": snap}, variant_id, now)
    _note_slot(sp, saved, variant_id, actor, now)
    return {"post": saved, "warnings": _mirror(sp, nv, at.astimezone(UTC), publishers), "applied": True}


def unschedule(sp, post_id: str, variant_id: str, *, expected_rev: int, actor: Actor = AGENT, clock: Clock | None = None,
               now: datetime | None = None, publishers=None) -> dict:
    """Back to the tray/pool: clears `variant.schedule`. An approved post stays approved (status `approved`). Never sends."""
    now = _clock_now(clock, now)
    post = _load_checked(sp, post_id, expected_rev)
    v = model.find_variant(post, variant_id)
    _refuse_if_locked(sp, post, v, actor, now)
    new = copy.deepcopy(post)
    nv = model.find_variant(new, variant_id)
    before = (nv.get("schedule") or {}).get("utc")
    nv["schedule"] = None
    saved = _save(sp, new, expected_rev, actor, "unschedule", {"from": before}, variant_id, now)
    _note_slot(sp, saved, variant_id, actor, now)
    return {"post": saved, "warnings": _mirror(sp, nv, None, publishers), "applied": True}


def _find_other(sp, variant_b: str, exclude_post: str) -> tuple[dict, dict]:
    ref_post, _, vid = variant_b.rpartition(":")
    hits = []
    for p in store.list_posts(sp):
        if p["id"] == exclude_post or (ref_post and p["id"] != ref_post):
            continue
        hits += [(p, v) for v in p.get("variants") or [] if v["id"] == vid]
    if len(hits) != 1:
        raise ValueError(f"variant {variant_b!r} matches {len(hits)} variants outside post {exclude_post}; use 'post:variant'")
    return hits[0]


def swap(sp, post_id: str, variant_a: str, variant_b: str, *, expected_rev: int, actor: Actor = AGENT, clock: Clock | None = None,
         now: datetime | None = None, publishers=None) -> dict:
    """Exchange the schedules of two variants of ONE account (a grid reorder). `variant_b` is in the same post or another one (bare id when
    unique, else `post:variant`). Returns {"post", "other" (the other post when it differs, else None), "warnings", "applied"}. Both
    variants are validated before anything is written; if the second write loses a race the first is rolled back. An agent swapping an
    approved, scheduled variant produces a proposal (two moves) instead of applying it."""
    now = _clock_now(clock, now)
    post = _load_checked(sp, post_id, expected_rev)
    va = model.find_variant(post, variant_a)
    if any(x["id"] == variant_b for x in post["variants"]):
        other, vb = post, model.find_variant(post, variant_b)
    else:
        other, vb = _find_other(sp, variant_b, post_id)
    if other is post and va["id"] == vb["id"]:
        raise ValueError("cannot swap a variant with itself")
    if va["account"] != vb["account"]:
        raise MoveRefused("swap needs two variants of one account", "conflict.account", post)
    for x in (va, vb):
        _refuse_if_locked(sp, post, x, actor, now)
    sa, sb = copy.deepcopy(va.get("schedule")), copy.deepcopy(vb.get("schedule"))
    approved = {id(x): _is_approved(sp, x) for x in (va, vb)}
    if actor.kind == "agent" and policy.agent_retime_approved(sp) == "propose_only" and any(approved[id(x)] and x.get("schedule") for x in (va, vb)):
        moves = [{"post": p["id"], "variant": x["id"], "to": {"local": s["local"], "tz": s["tz"]},
                  "why": "agent swap involving an approved post (needs a human accept)"}
                 for p, x, s in ((post, va, sb), (other, vb, sa)) if s]
        pid = proposals.create(sp, actor.name, moves, now=now)
        return {"post": post, "other": None, "warnings": ["approved post: the swap was written as a proposal"], "applied": False, "proposal": pid}

    set_by = "human" if actor.kind == "human" else "agent"
    na = copy.deepcopy(post)
    nb = copy.deepcopy(other) if other is not post else None
    ta = model.find_variant(na, variant_a)
    tb = model.find_variant(nb if nb is not None else na, vb["id"])
    ta["schedule"] = {**sb, "set_by": set_by} if sb else None
    tb["schedule"] = {**sa, "set_by": set_by} if sa else None
    if actor.kind == "human":
        for t, src in ((ta, va), (tb, vb)):
            if approved[id(src)] and t["schedule"] and "moved_after_approval" not in t["flags"]:
                t["flags"] = [*t["flags"], "moved_after_approval"]
    detail = {"with": vb["id"], "other_post": other["id"]}
    if nb is None:
        saved, saved_other = _save(sp, na, expected_rev, actor, "swap", detail, variant_a, now), None
    else:
        saved_other = _save(sp, nb, other["rev"], actor, "swap", detail, vb["id"], now)
        try:
            saved = _save(sp, na, expected_rev, actor, "swap", detail, variant_a, now)
        except MoveRefused:
            store.save_post(sp, other, expected_rev=saved_other["rev"], actor=actor, op="swap_rollback", variant=vb["id"], now=now)
            raise
    _note_slot(sp, saved, variant_a, actor, now)
    _note_slot(sp, saved_other or saved, vb["id"], actor, now)
    warnings: list[str] = []
    for t in (ta, tb):
        warnings += _mirror(sp, t, parse_ts(t["schedule"]["utc"]) if t["schedule"] else None, publishers)
    return {"post": saved, "other": saved_other, "warnings": warnings, "applied": True}
