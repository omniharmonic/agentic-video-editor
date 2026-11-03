"""Retry policy (spec 6.5). Only errors that PROVE nothing was posted are ever retried.

A `PublishError` is the platform (or the adapter before any upload) saying "definitely not posted"; the queue records it as a failed
platform. Anything else (`Uncertain`, a bare exception, a crash) leaves the platform in state `sending` = UNKNOWN, and is NEVER retried
here: only `ave studio resolve` (a human) settles it.

    auth        401 / invalid or expired token    no retry; alert `auth_expired`
    quota       429 / quota / rate limit          retry after the reset time in the message, else QUOTA_DEFAULT_MIN
    network     failed before any upload          backoff 5 min, 30 min, 2 h; max 3 attempts
    validation  anything else                     no retry; status failed; alert `rejected` with the (redacted) platform message

Every retry stays inside the slot's `late_after_min` window: a post is never sent later than that after its slot.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

BACKOFF_MIN = (5, 30, 120)
QUOTA_DEFAULT_MIN = 60

_AUTH = re.compile(r"\b401\b|unauthori[sz]ed|invalid[ _-]?token|expired[ _-]?token|token (is )?(expired|invalid)|auth(entication)? (failed|error)|oauth", re.I)
_QUOTA = re.compile(r"\b429\b|quota|rate[ _-]?limit|too many requests|limit exceeded", re.I)
_NETWORK = re.compile(r"connection|timed? ?out|timeout|dns|network|temporarily unavailable|\b50[234]\b|reset by peer|unreachable", re.I)
_AFTER = re.compile(r"retry[- _]?after\D{0,3}(\d+)", re.I)


def classify(error: str) -> str:
    """auth | quota | network | validation (checked in that order: an expired token that also mentions a 'connection' is still auth)."""
    s = error or ""
    if _AUTH.search(s):
        return "auth"
    if _QUOTA.search(s):
        return "quota"
    if _NETWORK.search(s):
        return "network"
    return "validation"


@dataclass(frozen=True)
class Plan:
    kind: str
    retry_at: datetime | None          # None = no (further) retry
    window_until: datetime             # nothing is sent after this instant
    reason: str


def plan(error: str, *, now: datetime, slot_utc: datetime, late_after_min: int, prior_network_failures: int, diagnostics=None) -> Plan:
    """What to do after a definite failure. `prior_network_failures` counts earlier network-class failures of this dispatch."""
    kind = (diagnostics or {}).get("kind") or classify(error)
    window = slot_utc + timedelta(minutes=late_after_min)
    if kind in ("auth", "validation"):
        return Plan(kind, None, window, "not retryable")
    if kind == "quota":
        m = _AFTER.search(error or "")
        seconds=(diagnostics or {}).get('retry_after')
        wait = timedelta(seconds=max(0,float(seconds))) if seconds is not None else timedelta(seconds=int(m.group(1))) if m else timedelta(minutes=QUOTA_DEFAULT_MIN)
        at = now + wait
    else:
        if prior_network_failures >= len(BACKOFF_MIN):
            return Plan(kind, None, window, f"gave up after {len(BACKOFF_MIN)} attempts")
        at = now + timedelta(minutes=BACKOFF_MIN[prior_network_failures])
    if at > window:
        return Plan(kind, None, window, "the retry would land after the late window; the slot is missed")
    return Plan(kind, at, window, "retry scheduled")
