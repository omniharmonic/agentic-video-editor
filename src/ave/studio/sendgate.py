"""The human's standing permission for an UNATTENDED sender: `<studio>/send.enabled`.

`ave studio tick --send` normally needs a human terminal. A launchd job has none, so the human installs the job AND writes this one-line signed
file with `ave studio send enable` (a human act: terminal, typed confirmation). The tick then accepts the file instead of a terminal:

    {"studio": "<resolved studio path>", "enabled": true, "by": "...", "at": "...", "seq": N, "sig": HMAC(key, "send|studio|by|at|on|seq")}

The file is bound to the studio's own path (a copy in another studio fails), carries a sequence number the sealed control ledger also holds
(`send disable` writes a signed "off" with a higher number; restoring an older "on" copy fails), and is IGNORED when an agent marker is set in the
environment (an agent may rehearse, never send). Like every file-level control it cannot stop an agent that reads the HMAC key; it makes a hand
edit, a copy and a replay detectable and refused. The harness deny rules keep the agent away from `send enable` and from this file.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from .. import humanpresence
from . import seal


@dataclass(frozen=True)
class SendStatus:
    ok: bool
    reason: str = ""
    by: str | None = None
    at: str | None = None


def path(sp) -> Path:
    return Path(sp.root) / "send.enabled"


def _studio_id(sp) -> str:
    return str(Path(sp.root).resolve())


def _sign(studio: str, by: str, at: str, enabled: bool, seq: int) -> str:
    return seal.mac("send", studio, by, at, "on" if enabled else "off", seq)


def latest_seq(sp) -> int | None:
    best = None
    for r in seal.log(sp, "control").rows():
        if r.get("op") == "send" and isinstance(r.get("seq_send"), int):
            best = r["seq_send"] if best is None else max(best, r["seq_send"])
    return best


def write(sp, by: str, *, enabled: bool = True, at: str | None = None) -> dict:
    """Library primitive (tests call it); the human act is `ave studio send enable|disable`."""
    if not by or not by.strip():
        raise ValueError("the send permission names who gave it")
    at = at or time.strftime("%Y-%m-%dT%H:%M:%S%z")
    seq = (latest_seq(sp) or 0) + 1
    studio = _studio_id(sp)
    rec = {"studio": studio, "enabled": bool(enabled), "by": by.strip(), "at": at, "seq": seq, "sig": _sign(studio, by.strip(), at, bool(enabled), seq)}
    seal.log(sp, "control").append({"op": "send", "seq_send": seq, "enabled": bool(enabled), "by": by.strip(), "at": at})
    p = path(sp)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, separators=(",", ":")) + "\n")
    os.replace(tmp, p)
    return rec


def verify(sp) -> SendStatus:
    p = path(sp)
    if not p.is_file():
        return SendStatus(False, "no send.enabled (a human has not allowed an unattended sender: ave studio send enable)")
    try:
        rec = json.loads(p.read_text())
    except (OSError, ValueError):
        return SendStatus(False, "send.enabled is unreadable")
    if not isinstance(rec, dict):
        return SendStatus(False, "send.enabled is malformed")
    studio, by, at, seq, enabled, sig = (rec.get(k) for k in ("studio", "by", "at", "seq", "enabled", "sig"))
    if not (isinstance(studio, str) and isinstance(by, str) and by and isinstance(at, str) and isinstance(seq, int) and not isinstance(seq, bool)
            and isinstance(enabled, bool)):
        return SendStatus(False, "send.enabled is incomplete")
    if studio != _studio_id(sp):
        return SendStatus(False, "send.enabled belongs to another studio folder (copied?)")
    if not seal.verify_mac(sig, "send", studio, by, at, "on" if enabled else "off", seq):
        return SendStatus(False, "send.enabled signature invalid (edited by hand?)")
    if not enabled:
        return SendStatus(False, "send.enabled is signed off", by, at)
    newest = latest_seq(sp)
    if newest is None:
        return SendStatus(False, "no sealed record of send.enabled (a copied file?): run ave studio send enable", by, at)
    if seq < newest:
        return SendStatus(False, f"stale send.enabled (sequence {seq}, the studio is at {newest}): an older signed file was restored", by, at)
    return SendStatus(True, "", by, at)


def disable(sp, by: str) -> dict:
    """The safe direction: a signed "off" (not an unlink, so a saved copy of the "on" file cannot be restored)."""
    return write(sp, by, enabled=False)


def authorized(sp) -> SendStatus:
    """May THIS process run a sending tick? Never when an agent marker is set; yes for a human terminal or a genuine send.enabled."""
    markers = humanpresence.agent_markers_set()
    if markers:
        return SendStatus(False, "sending is refused while an agent marker is set (" + ", ".join(markers) +
                                 "): an agent may rehearse (tick without --send), the human starts the sending scheduler")
    if humanpresence.is_human_terminal():
        return SendStatus(True, "a human terminal")
    st = verify(sp)
    if st.ok:
        return st
    return SendStatus(False, f"no human terminal and no valid send.enabled: {st.reason}")
