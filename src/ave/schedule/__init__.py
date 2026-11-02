"""Scheduler and sync (WS3). `register(studio)` plugs this workstream into the `Studio` facade (service.py HOOKS):

    calendar        fn(studio, start, end, accounts=None)                      -> calendar JSON (7.3)
    move            fn(studio, post_id, variant_id, to, *, expected_rev, actor=AGENT, snap="slot")   -> {"post" (a Studio.view), "warnings", "applied"}
    swap            fn(studio, post_id, variant_a, variant_b, *, expected_rev, actor=AGENT)          -> {"post", "other", "warnings", "applied"}
    unschedule      fn(studio, post_id, variant_id, *, expected_rev, actor=AGENT)                    -> {"post", "warnings", "applied"}
    accept_proposal fn(studio, proposal_id, *, actor)                          -> {"proposal", "applied", "failed"}   (human actors only)
    sync_queue      fn(studio, post_id, *, publishers=None, actor=AGENT)       -> sync report (never approves)
    tick            fn(studio, *, send=False, now=None)                        -> TickReport.as_dict()
    rehearse        fn(studio)                                                 -> a dry-run tick as a dict
    pause           fn(studio, account=None, *, by="claude")                   -> {"paused": True, "scope"}    (any actor)
    resume          fn(studio, account=None, *, by="maren", pin_verified=False)  -> {"paused", "scope"}          (needs a human terminal, or a PIN the caller verified)

Refusals raise `slots.MoveRefused` (`.code`, `.latest`: the API's 409 body); a stale `expected_rev` is `MoveRefused(code="conflict.rev")`.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

HOOKS_PROVIDED = ("calendar", "move", "swap", "unschedule", "accept_proposal", "sync_queue", "tick", "rehearse", "pause", "resume")


class _FixedClock:
    """`--now ISO`: time frozen at one instant (a tick never sleeps)."""

    def __init__(self, at: datetime):
        self.at = at

    def now(self) -> datetime:
        return self.at

    def sleep(self, s: float) -> None:
        return None


def _date(d: date | str) -> date:
    return d if isinstance(d, date) and not isinstance(d, datetime) else date.fromisoformat(str(d)[:10])


def register(studio) -> None:
    """Register every hook this workstream provides on `studio` (a `Studio`)."""
    from ..studio import dispatch
    from ..studio.events import AGENT
    from ..studio.sync import sync_queue as _sync
    from . import proposals, slots
    from . import tick as _tick

    def view_all(studio, res: dict) -> dict:
        out = dict(res)
        out["post"] = studio.view(res["post"])
        if res.get("other"):
            out["other"] = studio.view(res["other"])
        return out

    def calendar(studio, start, end, accounts=None):
        return slots.calendar(studio.sp, _date(start), _date(end), accounts, now=studio.now())

    def move(studio, post_id, variant_id, to, *, expected_rev, actor=AGENT, snap="slot"):
        return view_all(studio, slots.move(studio.sp, post_id, variant_id, to, expected_rev=expected_rev, actor=actor, snap=snap,
                                           now=studio.now(), publishers=studio.publishers or None))

    def swap(studio, post_id, variant_a, variant_b, *, expected_rev, actor=AGENT):
        return view_all(studio, slots.swap(studio.sp, post_id, variant_a, variant_b, expected_rev=expected_rev, actor=actor,
                                           now=studio.now(), publishers=studio.publishers or None))

    def unschedule(studio, post_id, variant_id, *, expected_rev, actor=AGENT):
        return view_all(studio, slots.unschedule(studio.sp, post_id, variant_id, expected_rev=expected_rev, actor=actor,
                                                 now=studio.now(), publishers=studio.publishers or None))

    def accept_proposal(studio, proposal_id, *, actor):
        return proposals.accept(studio.sp, proposal_id, actor=actor, now=studio.now(), publishers=studio.publishers or None)

    def sync_queue(studio, post_id, *, publishers=None, actor=AGENT):
        return _sync(studio.sp, post_id, publishers=publishers if publishers is not None else (studio.publishers or None), actor=actor,
                     now=studio.now())

    def tick(studio, *, send=False, now=None):
        clock = _FixedClock(now if isinstance(now, datetime) else datetime.fromisoformat(str(now).replace("Z", "+00:00"))) if now else studio.clock
        return _tick.tick(studio.sp, clock=clock, send=send, publishers=studio.publishers, host=studio.host).as_dict()

    def rehearse(studio):
        return _tick.tick(studio.sp, clock=studio.clock, send=False, publishers=studio.publishers, host=studio.host).as_dict()

    def pause(studio, account=None, *, by="claude"):
        return dispatch.pause(studio.sp, account=account, by=by, now=studio.now())

    def resume(studio, account=None, *, by="maren", pin_verified=False):
        if not pin_verified:
            from .. import humanpresence
            humanpresence.require_human("resume", "ave studio resume" + (f" --account {account}" if account else ""))
        return dispatch.resume(studio.sp, account=account, by=by, now=studio.now())

    for name, fn in (("calendar", calendar), ("move", move), ("swap", swap), ("unschedule", unschedule), ("accept_proposal", accept_proposal),
                     ("sync_queue", sync_queue), ("tick", tick), ("rehearse", rehearse), ("pause", pause), ("resume", resume)):
        studio.register(name, fn)
