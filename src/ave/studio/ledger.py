"""Append-only JSONL ledgers (spec 2.7): events, feedback, alerts. Torn-line tolerant like src/ave/ui/ledger.py.

The FEEDBACK ledger is sealed (studio/seal.py): hash-chained and HMAC-signed with a signed head record, so a removed, edited or truncated
line is detected (doctor, and the tick refuses to send). Events and alerts stay plain append-only logs.

Rules: a line is written with one `write` under an exclusive flock; a file that does not end in a newline (a crash mid-write) gets a
newline first, so the torn fragment can never swallow the next record; readers skip unparsable lines; consumption of a feedback item
is a SEPARATE line (`claim` / `resolve`), history is never rewritten.
"""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from .clock import SystemClock, iso_utc, parse_ts

CLAIM_TTL_MIN = 30
FEEDBACK_KINDS = ("rework_copy", "rework_visual", "drop", "retime", "human_edit", "note")
REWORK_KINDS = ("rework_copy", "rework_visual")


def events_path(sp) -> Path:
    return sp.ledger_dir / "events.jsonl"


def feedback_path(sp) -> Path:
    return sp.ledger_dir / "feedback.jsonl"


def alerts_path(sp) -> Path:
    return sp.ledger_dir / "alerts.jsonl"


@contextmanager
def ledger_lock(sp):
    """One writer at a time across all three ledgers (appends are tiny; contention is irrelevant)."""
    sp.ledger_dir.mkdir(parents=True, exist_ok=True)
    f = open(sp.ledger_dir / ".lock", "a")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        f.close()


def _append(path: Path, obj: dict) -> int:
    """Append one line; returns the file size after the write (the next byte offset)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    with open(path, "ab+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        size = f.seek(0, os.SEEK_END)
        if size:
            f.seek(size - 1)
            if f.read(1) != b"\n":
                data = b"\n" + data
        f.write(data)
        f.flush()
        return f.seek(0, os.SEEK_END)


def append_jsonl(sp, path: Path, obj: dict) -> int:
    with ledger_lock(sp):
        return _append(path, obj)


def read_jsonl(path: Path, offset: int = 0) -> tuple[list[dict], int]:
    """(rows, next_offset). Only COMPLETE lines (ending in a newline) are consumed, so a reader polling with `next_offset` never
    sees half a record; unparsable complete lines are skipped."""
    if not path.is_file():
        return [], offset
    with open(path, "rb") as f:
        f.seek(offset)
        chunk = f.read()
    rows: list[dict] = []
    end = chunk.rfind(b"\n") + 1
    for raw in chunk[:end].splitlines():
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows, offset + end


def _now(now: datetime | None) -> datetime:
    return now or SystemClock().now()


def _fb_append(sp, row: dict) -> dict:
    """Append one line to the sealed feedback ledger (the caller holds `ledger_lock`)."""
    from . import seal
    return seal.log(sp, "feedback").append_locked(row)


def feedback_problems(sp) -> list[str]:
    """Why the feedback ledger cannot be trusted (empty = intact): a removed, edited, reordered or truncated line."""
    from . import seal
    return seal.log(sp, "feedback").verify().problems


# ------------------------------------------------------------------------------------------------ feedback

def _validate_line(kind: str, line: dict) -> None:
    from .model import validate
    problems = validate(kind, line)
    if problems:
        raise ValueError(f"invalid {kind} line: " + "; ".join(problems))


def append_feedback(sp, line: dict, *, now: datetime | None = None) -> dict:
    """Append a human -> agent `feedback` line. Fills `v`, `op`, `id` (fb_0001...), `ts`, `source`. `human_edit` lines are born
    resolved (an automatic `resolve` line by `system`). Returns the stored line."""
    t = iso_utc(_now(now))
    with ledger_lock(sp):
        rows, _ = read_jsonl(feedback_path(sp))
        n = sum(1 for r in rows if r.get("op") == "feedback") + 1
        row = {"v": 1, "op": "feedback", "id": f"fb_{n:04d}", "ts": t, "source": "cli", **line}
        row["op"] = "feedback"
        _validate_line("feedback", row)
        row = _fb_append(sp, row)
        if row["kind"] == "human_edit":
            _fb_append(sp, {"op": "resolve", "fb": row["id"], "by": "system", "ts": t, "rev": row.get("rev"), "note": "human edit recorded"})
    return row


def _fold(rows: list[dict], now: datetime) -> dict[str, dict]:
    """Feedback id -> line + {"resolved": resolve-line|None, "claim": {"by","ts","expires"}|None}."""
    out: dict[str, dict] = {}
    for r in rows:
        op = r.get("op")
        if op == "feedback" and "id" in r:
            out[r["id"]] = {**r, "resolved": None, "claim": None}
        elif op == "claim" and r.get("fb") in out:
            try:
                exp = parse_ts(r["ts"]) + timedelta(minutes=CLAIM_TTL_MIN)
            except (ValueError, KeyError):
                continue
            out[r["fb"]]["claim"] = {"by": r.get("by"), "ts": r["ts"], "expires": iso_utc(exp)} if exp > now else None
        elif op == "resolve" and r.get("fb") in out:
            out[r["fb"]]["resolved"] = r
    return out


def all_feedback(sp, *, now: datetime | None = None) -> list[dict]:
    """Every feedback line with its `resolved` and active `claim` folded in, oldest first."""
    rows, _ = read_jsonl(feedback_path(sp))
    return list(_fold(rows, _now(now)).values())


def open_feedback(sp, *, post: str | None = None, variant: str | None = None, kind: str | None = None,
                  now: datetime | None = None) -> list[dict]:
    """Feedback lines with no `resolve`, oldest first. `variant=` also returns post-wide lines (variant null)."""
    out = []
    for fb in all_feedback(sp, now=now):
        if fb["resolved"]:
            continue
        if post and fb.get("post") != post:
            continue
        if variant and fb.get("variant") not in (variant, None):
            continue
        if kind and fb.get("kind") != kind:
            continue
        out.append(fb)
    return out


class ClaimConflict(RuntimeError):
    pass


def claim_feedback(sp, by: str, *, fb: str | None = None, now: datetime | None = None) -> dict | None:
    """Claim `fb`, or the oldest open unclaimed item. Returns it (with its claim), or None when nothing is open.
    A claim held by someone else and not yet expired (30 min) raises ClaimConflict for a named `fb`."""
    t = _now(now)
    with ledger_lock(sp):
        rows, _ = read_jsonl(feedback_path(sp))
        folded = _fold(rows, t)
        if fb is not None:
            item = folded.get(fb)
            if item is None or item["resolved"]:
                raise ValueError(f"no open feedback {fb!r}")
            if item["claim"] and item["claim"]["by"] != by:
                raise ClaimConflict(f"{fb} is claimed by {item['claim']['by']} until {item['claim']['expires']}")
        else:
            item = next((x for x in folded.values() if not x["resolved"] and not x["claim"]), None)
            if item is None:
                return None
        _fb_append(sp, {"op": "claim", "fb": item["id"], "by": by, "ts": iso_utc(t)})
    return {**item, "claim": {"by": by, "ts": iso_utc(t), "expires": iso_utc(t + timedelta(minutes=CLAIM_TTL_MIN))}}


def resolve_feedback(sp, fb: str, by: str, *, rev: int | None = None, note: str = "", now: datetime | None = None,
                     human: bool = False, evidence: dict | None = None) -> dict:
    """Close a feedback item. This is the LOW-LEVEL write: `Studio.resolve_feedback` decides whether the evidence is enough. `human` records that a
    human-recognized path (a terminal, or the UI) closed it; `evidence` is what the resolver proved (kept on the line)."""
    t = _now(now)
    with ledger_lock(sp):
        rows, _ = read_jsonl(feedback_path(sp))
        item = _fold(rows, t).get(fb)
        if item is None:
            raise ValueError(f"no feedback {fb!r}")
        if item["resolved"]:
            raise ValueError(f"{fb} is already resolved")
        line = {"op": "resolve", "fb": fb, "by": by, "ts": iso_utc(t), "rev": rev, "note": note, "human": bool(human)}
        if evidence:
            line["evidence"] = evidence
        line = _fb_append(sp, line)
    return line


# ------------------------------------------------------------------------------------------------ alerts

ALERT_KINDS = ("tamper", "auth_expired", "auth_expiring", "quota", "rejected", "uncertain", "missed", "late", "approval_expired", "license_gap",
               "host_failed", "duplicate", "daemon_stale", "credentials_missing", "account_not_live", "manual_post_due", "note")


def append_alert(sp, *, severity: str, kind: str, message: str, account: str | None = None, post: str | None = None,
                 now: datetime | None = None) -> dict:
    from ..publish import safety
    t = iso_utc(_now(now))
    with ledger_lock(sp):
        rows, _ = read_jsonl(alerts_path(sp))
        n = sum(1 for r in rows if r.get("id")) + 1
        row = {"id": f"al_{n:04d}", "ts": t, "severity": severity, "kind": kind, "account": account, "post": post,
               "message": safety.redact(message), "ack": None}
        _validate_line("alert", row)
        _append(alerts_path(sp), row)
    return row


def list_alerts(sp, *, include_acked: bool = False) -> list[dict]:
    rows, _ = read_jsonl(alerts_path(sp))
    out: dict[str, dict] = {}
    for r in rows:
        if r.get("id"):
            out[r["id"]] = dict(r)
        elif r.get("op") == "ack" and r.get("alert") in out:
            out[r["alert"]]["ack"] = {"by": r.get("by"), "ts": r.get("ts")}
    return [a for a in out.values() if include_acked or not a.get("ack")]


def ack_alert(sp, alert_id: str, by: str, *, now: datetime | None = None) -> dict:
    with ledger_lock(sp):
        rows, _ = read_jsonl(alerts_path(sp))
        if not any(r.get("id") == alert_id for r in rows):
            raise ValueError(f"no alert {alert_id!r}")
        line = {"op": "ack", "alert": alert_id, "by": by, "ts": iso_utc(_now(now))}
        _append(alerts_path(sp), line)
    return line
