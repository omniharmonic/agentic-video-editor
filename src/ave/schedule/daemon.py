"""`ave studio run`: a loop around `tick` plus the heartbeat (spec 6.1). No APScheduler, no new dependency; cron/launchd can call `tick` instead.

The loop takes the same non-blocking lock as `tick`, so a second daemon (or a manual tick) never double-sends. SIGTERM handling lives in the
CLI (WS7), which sets `stop`; `run_loop` itself only needs an object with `is_set()`.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Mapping

from ..episode import read_json
from ..studio import ledger
from ..studio.clock import Clock, iso_utc, parse_ts
from . import tick as _tick


def run_loop(sp, *, clock: Clock, send: bool = False, interval: float = 60.0, publishers: Mapping[str, Any] | None = None, host: Any = None,
             stop=None, max_ticks: int | None = None, on_report=None) -> int:
    """Tick every `interval` seconds (via `clock.sleep`) until `stop.is_set()` or `max_ticks`. Returns the number of ticks run. A tick that
    raises `TickRefused` ends the loop (re-raised): the daemon never keeps running in a state where sending is forbidden."""
    n = 0
    while not (stop is not None and stop.is_set()) and (max_ticks is None or n < max_ticks):
        rep = _tick.tick(sp, clock=clock, send=send, publishers=publishers, host=host)
        n += 1
        if on_report:
            on_report(rep)
        if (max_ticks is not None and n >= max_ticks) or (stop is not None and stop.is_set()):
            break
        clock.sleep(interval)
    return n


def check_heartbeat(sp, *, clock: Clock, interval: float = 60.0) -> dict:
    """{"alive": bool, "age_s": float|None}. A sending daemon that has not ticked for 3 intervals raises a `daemon_stale` alert (once per
    heartbeat value). No heartbeat file at all means the daemon was never run here: reported, not alerted."""
    p = _tick.heartbeat_path(sp)
    if not p.is_file():
        return {"alive": False, "age_s": None, "never_ran": True}
    try:
        hb = read_json(p)
        at = parse_ts(hb["at"])
    except (OSError, ValueError, KeyError):
        return {"alive": False, "age_s": None, "never_ran": False}
    now = clock.now()
    age = (now - at).total_seconds()
    stale = age > 3 * interval
    if stale:
        marker = f"heartbeat {hb['at']}"
        if not any(a["kind"] == "daemon_stale" and f"[{marker}]" in a["message"] for a in ledger.list_alerts(sp, include_acked=True)):
            ledger.append_alert(sp, severity="warn", kind="daemon_stale", account=None, post=None, now=now,
                                message=f"the scheduler has not ticked since {hb['at']} ({int(age)} s ago); scheduled posts will be missed [{marker}]")
    return {"alive": not stale, "age_s": age, "never_ran": False}
