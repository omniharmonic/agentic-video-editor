"""Time as a dependency. Nothing in the studio calls `datetime.now()` directly; tests inject a FakeClock (tests/studio_helpers.py)."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """The current instant as an AWARE datetime (UTC)."""

    def sleep(self, s: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, s: float) -> None:
        time.sleep(s)


def parse_ts(s: str) -> datetime:
    """Parse any timestamp the studio or the queue writes ('2026-11-02T16:00:00Z', '2026-10-19T10:15:00-0600', '...+00:00') into an
    aware datetime. A naive value is an error: a schedule without a zone is a bug, not a guess."""
    if not isinstance(s, str) or not s:
        raise ValueError(f"bad timestamp {s!r}")
    t = s.strip()
    if t.endswith(("Z", "z")):
        t = t[:-1] + "+00:00"
    dt = datetime.fromisoformat(t)       # Python >= 3.11 also reads the '-0600' form that strftime("%z") writes
    if dt.tzinfo is None:
        raise ValueError(f"timestamp {s!r} has no timezone offset")
    return dt


def iso_utc(dt: datetime) -> str:
    """'2026-11-02T16:00:00Z' (seconds precision) for an aware datetime."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
