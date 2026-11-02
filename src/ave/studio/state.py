"""Variant status derivation (spec 2.8). Status is computed on every read from the queue item, the schedule and the open feedback;
the stored value is only a cache for filters and is never written by hand.

First match wins, in this order:

    dropped       variant.dropped, or the queue item was rejected
    published     queue status published
    publishing    queue status publishing, or any results[*].state == "sending" (UNKNOWN: needs `ave studio resolve`)
    failed        queue status failed (and not publishing)
    missed        queue approved, slot passed by more than late_after_min, not published
    needs_rework  an open rework_copy / rework_visual feedback exists
    scheduled     queue approved, approval valid and unexpired, has schedule.utc
    approved      queue approved, no schedule (pool), or approval expired (flag approval_expired)
    in_review     queue awaiting_approval
    draft         no queue item yet, or queue draft

Two documented interpretations beyond the table: a queue-`rejected` item is `dropped` (rejection is the only way the studio produces it);
and when `sp` is given and an `approved` item no longer has a valid approval for its CURRENT bytes and metadata (file changed, signature
invalid), it is `in_review` with flag `approval_stale`, because it needs a human again and will not be sent.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Mapping

from ..episode import Episode
from ..publish import queue as pq
from . import policy
from .clock import parse_ts

REWORK = ("rework_copy", "rework_visual")


def queue_episode(sp, variant: Mapping) -> Episode | None:
    """The project that owns this variant's queue item: the studio itself (`root == "."`) or an adopted episode folder."""
    ref = variant.get("queue")
    if not ref:
        return None
    root = ref.get("root") or "."
    if root == ".":
        return sp
    p = Path(root)
    if not p.is_absolute() and (sp.root.parent / p).exists():
        p = sp.root.parent / p
    return Episode(p)


def load_queue_item(sp, variant: Mapping) -> dict | None:
    """The variant's queue item, or None (no queue ref, or the file is gone)."""
    ep = queue_episode(sp, variant)
    if ep is None:
        return None
    try:
        return pq.load(ep, variant["queue"]["id"])
    except (FileNotFoundError, ValueError):
        return None


def stale_approval(sp, variant: Mapping, item: dict) -> list[str]:
    """Reasons the item's approval would NOT survive a send right now (bytes, metadata, signature). Reads files; no network."""
    ep = queue_episode(sp, variant)
    try:
        return [b for b in pq.send_blockers(ep, item)]
    except (OSError, ValueError, KeyError) as e:
        return [f"cannot verify: {e}"]


def explain(variant: Mapping, queue_item: dict | None, feedback_open: list[dict], now: datetime, sp=None) -> dict:
    """{"status": <one of model.VARIANT_STATUSES>, "flags": [...], "reason": str}. `derive` is the status alone."""
    flags: list[str] = []
    qs = (queue_item or {}).get("status")
    results = (queue_item or {}).get("results") or {}
    mine = [f for f in feedback_open if f.get("variant") in (None, variant.get("id"))]

    def done(status: str, reason: str) -> dict:
        return {"status": status, "flags": flags, "reason": reason}

    if variant.get("dropped") or qs == "rejected":
        return done("dropped", "variant dropped" if variant.get("dropped") else "queue item rejected")
    if qs == "published":
        human=any((r.get('receipt') or {}).get('source')=='human_report' for r in results.values())
        adjuncts=[a for r in results.values() for a in (r.get('receipt',{}).get('adjuncts') or {}).values()]
        uncertain=any(a.get('state') in ('uncertain','sending') for a in adjuncts)
        failed=any(a.get('state')=='failed' for a in adjuncts)
        if uncertain: flags.append('adjunct_uncertain')
        if failed: flags.append('adjunct_failed')
        reason="Published · reported by a person" if human else "Primary post published"
        if uncertain: reason+=". First comment outcome is uncertain; inspect the platform before adding it manually."
        elif failed: reason+=". First comment failed; inspect the platform to finish it manually."
        return done("published",reason)
    for r in results.values():
        from ..publish.receipts import expired_leases
        if expired_leases(r,now) and r.get('state') not in ('published','failed'):
            flags.append('host_lease_expired')
            return done('publishing','Hosted media expired after 24 hours; refresh provider status or review the unresolved outcome before any new send')
        phase=(r.get('receipt') or {}).get('phase')
        if phase in ('processing','scheduled','awaiting_user_publish','sandbox','draft'):
            flags.append('delivery_'+phase)
            return done('publishing', {'processing':'Processing at provider','scheduled':'Scheduled at provider','awaiting_user_publish':'Awaiting your publication in the platform app','sandbox':'Sandbox only — not a public post','draft':'Provider draft — not published'}[phase])
    if qs == "publishing" or any(r.get("state") == "sending" for r in results.values()):
        if qs != "publishing":
            flags.append("uncertain")
        return done("publishing", "a send is in flight or UNKNOWN (ave studio resolve)")
    if qs == "failed":
        human_negative=any((r.get('receipt') or {}).get('source')=='human_report' and (r.get('receipt') or {}).get('diagnostics',{}).get('decision')=='not_published' for r in results.values())
        if human_negative:return done('failed','Not published · reported by a person. Fresh human review and approval are required before another send.')
        return done("failed", "Provider rejected the accepted operation. Fresh human review and approval are required before another send." if (queue_item or {}).get("delivery_review_required") else "queue item failed")

    sched = variant.get("schedule") or {}
    utc = None
    if sched.get("utc"):
        try:
            utc = parse_ts(sched["utc"])
        except ValueError:
            flags.append("bad_schedule")
    late = timedelta(minutes=policy.late_after_min(sp) if sp is not None else policy.DEFAULT_LATE_AFTER_MIN)
    if qs == "approved" and utc is not None and now > utc + late:
        return done("missed", f"slot passed by more than {int(late.total_seconds() // 60)} min")
    if any(f.get("kind") in REWORK for f in mine):
        return done("needs_rework", "open rework feedback")

    if qs == "approved":
        ttl = policy.approval_ttl_days(sp, variant.get("account")) if sp is not None else policy.DEFAULT_TTL_DAYS
        expired = policy.approval_expired(queue_item, now, ttl)
        if expired:
            flags.append("approval_expired")
        if sp is not None:
            stale = stale_approval(sp, variant, queue_item)
            if stale:
                flags.append("approval_stale")
                return done("in_review", "approval no longer valid: " + "; ".join(stale))
        if utc is not None and not expired:
            return done("scheduled", "approved, unexpired, has a slot")
        return done("approved", "approval expired" if expired else "approved, no slot (pool)")
    if qs == "awaiting_approval":
        return done("in_review", "awaiting the human's approval")
    return done("draft", "no queue item yet" if queue_item is None else "queue item is a draft")


def derive(variant: dict, queue_item: dict | None, feedback_open: list[dict], now: datetime, sp=None) -> str:
    """The variant's status (see the module docstring). Pure when `sp` is None; with `sp` it also uses the studio's late/TTL policy
    and re-verifies the approval against the bytes on disk."""
    return explain(variant, queue_item, feedback_open, now, sp)["status"]
