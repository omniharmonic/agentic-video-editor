"""The event log (`ledger/events.jsonl`, spec 2.7): every mutation, append-only; the UI change feed is a byte-offset cursor into it.

`actor.kind` is advisory (UX defaults and audit), NOT a security boundary: the boundaries are signatures and attestations.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..publish import safety
from . import ledger
from .clock import SystemClock, iso_utc

ACTOR_KINDS = ("human", "agent", "scheduler")


@dataclass(frozen=True)
class Actor:
    kind: str
    name: str

    def __post_init__(self):
        if self.kind not in ACTOR_KINDS:
            raise ValueError(f"actor kind must be one of {', '.join(ACTOR_KINDS)}")

    def as_dict(self) -> dict:
        return {"kind": self.kind, "name": self.name}


HUMAN = Actor("human", "maren")
AGENT = Actor("agent", "claude")
SCHEDULER = Actor("scheduler", "tick")


def make_event(*, actor: Actor, op: str, post: str | None = None, variant: str | None = None, rev: int | None = None,
               before_sha: str | None = None, after_sha: str | None = None, detail: dict | None = None,
               now: datetime | None = None) -> dict:
    return {"ts": iso_utc(now or SystemClock().now()), "actor": actor.as_dict(), "post": post, "variant": variant, "op": op,
            "rev": rev, "before_sha": before_sha, "after_sha": after_sha, "detail": safety.redact_obj(detail or {})}


def append_event(sp, ev: dict, *, now: datetime | None = None) -> dict:
    """Append one event (adds `ts` when missing, redacts `detail`). Returns the stored event."""
    from .model import validate
    ev = dict(ev)
    ev.setdefault("ts", iso_utc(now or SystemClock().now()))
    if isinstance(ev.get("actor"), Actor):
        ev["actor"] = ev["actor"].as_dict()
    ev["detail"] = safety.redact_obj(ev.get("detail") or {})
    problems = validate("event", ev)
    if problems:
        raise ValueError("invalid event: " + "; ".join(problems))
    ledger.append_jsonl(sp, ledger.events_path(sp), ev)
    return ev


def cursor_str(offset: int) -> str:
    return f"ev:{offset}"


def parse_cursor(cursor: str | None) -> int | None:
    if cursor in (None, ""):
        return None
    if not isinstance(cursor, str) or not cursor.startswith("ev:") or not cursor[3:].isdigit():
        raise ValueError(f"bad cursor {cursor!r}")
    return int(cursor[3:])


def changes(sp, cursor: str | None = None) -> dict:
    """The change feed. No cursor: `{"cursor": <end of log>, "events": []}` (start watching from now). With `cursor`: every event
    appended since, and the new cursor. Cursors are opaque `ev:<byte offset>` strings."""
    path = ledger.events_path(sp)
    off = parse_cursor(cursor)
    if off is None:
        size = path.stat().st_size if path.is_file() else 0
        _, end = ledger.read_jsonl(path, 0) if size else ([], 0)
        return {"cursor": cursor_str(end), "events": []}
    if path.is_file() and off > path.stat().st_size:
        off = 0                                   # the log was truncated or replaced: replay from the start
    rows, nxt = ledger.read_jsonl(path, off)
    return {"cursor": cursor_str(nxt), "events": rows}
