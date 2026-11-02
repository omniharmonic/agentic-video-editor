"""Agent-proposed moves (`schedule/proposals/<id>.json`, spec 2.6). Shown as ghost cards; ACCEPTING one (a human act) applies its moves
as ordinary `move` ops. A proposal carries no authority: it never approves and never sends."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ..episode import read_json, write_json
from ..studio import model, store
from ..studio.clock import SystemClock, iso_utc
from ..studio.events import Actor, append_event, make_event


def proposals_dir(sp) -> Path:
    return sp.schedule_dir / "proposals"


def create(sp, by: str, moves: list[dict], *, now: datetime | None = None) -> str:
    """Write a proposal; returns its id (`sp_0001`, ...). `moves` = [{post, variant, to: {local, tz}, why}]."""
    d = proposals_dir(sp)
    d.mkdir(parents=True, exist_ok=True)
    n = len(list(d.glob("sp_*.json"))) + 1
    while (d / f"sp_{n:04d}.json").exists():
        n += 1
    pid = f"sp_{n:04d}"
    prop = {"id": pid, "by": by, "at": iso_utc(now or SystemClock().now()), "moves": moves}
    model.require_valid("proposal", prop)
    write_json(d / f"{pid}.json", prop)
    return pid


def _all(sp) -> list[dict]:
    out = []
    for p in sorted(proposals_dir(sp).glob("sp_*.json")) if proposals_dir(sp).is_dir() else []:
        try:
            out.append(read_json(p))
        except (OSError, ValueError):
            continue
    return out


def pending(sp) -> list[dict]:
    return [p for p in _all(sp) if not p.get("status")]


def accept(sp, proposal_id: str, *, actor: Actor, clock=None, now: datetime | None = None, publishers=None) -> dict:
    """Apply every move of a pending proposal as a HUMAN move. Refuses a non-human actor. A move that no longer applies (slot taken since)
    is reported in `failed` and the rest still apply. The proposal is then marked accepted."""
    from . import slots
    if actor.kind != "human":
        raise PermissionError("accepting a schedule proposal is a human act")
    p = next((x for x in _all(sp) if x["id"] == proposal_id), None)
    if p is None or p.get("status"):
        raise ValueError(f"no pending proposal {proposal_id!r}")
    applied, failed = [], []
    for m in p["moves"]:
        try:
            rev = store.load_post(sp, m["post"])["rev"]
            slots.move(sp, m["post"], m["variant"], m["to"], expected_rev=rev, actor=actor, snap="slot", clock=clock, now=now, publishers=publishers)
            applied.append(m)
        except (slots.MoveRefused, ValueError, KeyError, FileNotFoundError, model.RevConflict) as e:
            failed.append({**m, "error": str(e)})
    done = {**p, "status": "accepted", "accepted_by": actor.name, "accepted_at": iso_utc(now or SystemClock().now()),
            "applied": len(applied), "failed": failed}
    write_json(proposals_dir(sp) / f"{proposal_id}.json", done)
    append_event(sp, make_event(actor=actor, op="proposal_accept", detail={"proposal": proposal_id, "applied": len(applied), "failed": len(failed)}, now=now))
    return {"proposal": proposal_id, "applied": applied, "failed": failed}
