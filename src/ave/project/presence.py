"""Who is working on what right now: `presence.set` events in the shared log.

A computer writes `presence.set` while a person works in an episode: input in the Room open on it (playing, scrubbing,
flagging, choosing), and every log event this computer writes for that artifact (from the app, the CLI or Claude's MCP
tools). An open Room nobody touches stops refreshing, so it lapses after TTL_S:

    {"op": "presence.set", "artifact": "art_ep", "data": {"view": "cut", "at": <ms>, "until": <ms>}}

Presence lasts TTL_S (10 minutes) from `at` and is refreshed while the work continues. It ends by itself: nothing is written
when a person stops, and `active()` simply stops listing it once `until` has passed. Each computer's latest event replaces
its earlier ones (the reducer keeps one per device).

Writes are rare on purpose, because every one travels to every member: `touch()` never writes more than once per
MIN_GAP_S (a minute), and renews the same place once REFRESH_S (also a minute) has passed while work goes on,
so presence ends TTL_S after the last work (less at most the minute since the last write); a new place is written
(another artifact or view) as long as the minute has passed.

`at` and `until` come from the writer's wall clock; a computer whose clock is far off shows presence for longer or shorter.
`until` is never trusted beyond `at` + TTL_S.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

TTL_S = 600
MIN_GAP_S = 60
REFRESH_S = 60                     # = MIN_GAP_S: while work goes on, presence is renewed each minute, so it lasts
                                   # (nearly) the full TTL_S after the last work
VIEWS = {"cut": "Cut", "script": "Script", "selects": "Selects", "layout": "Layout"}

_lock = threading.Lock()
_last: dict[tuple[str, str], dict] = {}    # (project id, device) -> what this process last wrote, so it never re-reads to throttle


def now_ms() -> int:
    return int(time.time() * 1000)


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def entry(device: str, p: dict) -> dict | None:
    """One reduced presence (State.presence[device]) as {device, person, artifact, view, where, at, until}, or None when
    it carries no usable times."""
    d = p.get("data") or {}
    at, until = d.get("at"), d.get("until")
    if not (_num(at) and _num(until)):
        return None
    until = min(until, at + TTL_S * 1000)
    view = d.get("view") if d.get("view") in VIEWS else None
    return {"device": device, "person": p.get("person"), "artifact": p.get("artifact"), "view": view,
            "where": VIEWS.get(view) if view else None, "at": at, "until": until}


def active(state, now: int | None = None, *, exclude: str | None = None, artifact: str | None = None) -> list[dict]:
    """Presence that has not run out at `now` (ms), newest first. `exclude` leaves out one device (this computer's own),
    `artifact` keeps only one artifact's."""
    now = now_ms() if now is None else now
    out = []
    for dev, p in (state.presence or {}).items():
        if dev == exclude:
            continue
        e = entry(dev, p)
        if e is None or e["until"] <= now or (artifact is not None and e["artifact"] != artifact):
            continue
        out.append({**e, "text": say(e)})
    return sorted(out, key=lambda e: -e["at"])


def say(e: dict) -> str:
    who = _cap(e.get("person") or "Someone")
    return f"{who} is in {e['where']}" if e.get("where") else f"{who} is working on it"


def _due(last: dict | None, artifact: str | None, view: str | None, now: int) -> bool:
    if last is None:
        return True
    at = last.get("at")
    if not _num(at) or now < at:                    # a clock that went back: write, so presence does not end early
        return True
    if now - at < MIN_GAP_S * 1000:
        return False
    same = last.get("artifact") == artifact and last.get("view") == view
    return not same or now - at >= REFRESH_S * 1000


def touch(project, artifact: str | None, view: str | None = None, *, now: int | None = None,
          clock: Callable[[], int] | None = None, keep_view: bool = False) -> dict | None:
    """This person is working on `artifact` (in `view`, one of VIEWS). Writes `presence.set` when one is due, and returns
    the event, else None. Safe to call as often as you like. `keep_view`: work that does not say where (a log write) keeps
    the view the last presence named on the same artifact."""
    now = (clock or now_ms)() if now is None else now
    view = view if view in VIEWS else None
    key = (project.project_id, project.device.id)
    with _lock:
        last = _last.get(key)
        if last is None:
            mine = (project.state().presence or {}).get(project.device.id)
            if mine is not None:
                last = {"artifact": mine.get("artifact"), **{k: (mine.get("data") or {}).get(k) for k in ("view", "at")}}
        if keep_view and last is not None and last.get("artifact") == artifact and last.get("view") in VIEWS:
            view = last["view"]
        if not _due(last, artifact, view, now):
            return None
        e = project.log.append("presence.set", artifact=artifact, data={"view": view, "at": now, "until": now + TTL_S * 1000},
                               actor="human")
        _last[key] = {"artifact": artifact, "view": view, "at": now}
        return e


def forget() -> None:
    """Drop what this process remembers about its own writes (tests)."""
    with _lock:
        _last.clear()
