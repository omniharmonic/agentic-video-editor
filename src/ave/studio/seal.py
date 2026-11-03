"""Tamper-EVIDENT ledgers and signed control records (docs/studio.md "Threat model").

HONEST LIMIT. An agent with a same-user shell can always edit files and can read the HMAC key (keychain, env or `~/.config/ave/approval.key`),
so file-level tampering cannot be made impossible. What this module buys:

  * a hand edit, a deleted line, a truncated file or a removed ledger is DETECTED (`verify`), not silently honoured: the tick refuses to send
    while any sealed ledger fails verification, `doctor` reports it, and an alert is raised;
  * every sanctioned write is signed, so "the file says X" can be told apart from "an ave command wrote X";
  * rolling a ledger back to an EARLIER consistent state (file AND its head record together, for example from a backup) is not detected.

A sealed log is `ledger/<name>.jsonl` where every line carries `seq`, `prev` (the previous line's signature) and `sig` =
HMAC(approval key, "line|<name>|" + canonical(line without sig)), plus a signed head record `ledger/.<name>.head` = {n, head, mac}. Deleting
or editing a line breaks the chain; deleting the tail leaves the head ahead of the file; deleting the head record leaves lines without a head.
A file that already existed unsealed (a studio created before this module) is sealed in place: the first append writes a `seal` line holding the
sha256 of the legacy bytes, so the old content is covered too.

Three logs use it: `feedback` (the human's send-backs and their resolution), `dispatch` (single-use approvals: started / finished / released)
and `control` (pause / resume, post-approval slot changes, live-attestation sequence numbers).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from ..publish import safety

GENESIS = "0" * 64
LOGS = ("feedback", "dispatch", "control")


def mac(domain: str, *parts) -> str:
    """HMAC-SHA256 under the approval key over `domain|part|part...` (hex)."""
    msg = "|".join([domain, *[str(p) for p in parts]]).encode()
    return hmac.new(safety.approval_key(), msg, hashlib.sha256).hexdigest()


def verify_mac(sig: object, domain: str, *parts) -> bool:
    return isinstance(sig, str) and hmac.compare_digest(sig, mac(domain, *parts))


def _line_sig(name: str, obj: dict) -> str:
    body = {k: v for k, v in obj.items() if k != "sig"}
    return mac("line|" + name, safety.canonical(body))


@dataclass
class Verification:
    ok: bool = True
    problems: list[str] = field(default_factory=list)
    n: int = 0                  # sealed lines (including the seal line)
    legacy: bool = False        # a pre-seal file with no sealed line yet (fine until the first append)


class SealedLog:
    def __init__(self, sp, name: str):
        if name not in LOGS:
            raise ValueError(f"unknown sealed log {name!r}")
        self.sp, self.name = sp, name
        self.path = Path(sp.ledger_dir) / f"{name}.jsonl"
        self.head_path = Path(sp.ledger_dir) / f".{name}.head"

    # ---- reading
    def _raw(self) -> list[bytes]:
        """Complete lines only (a torn trailing fragment is a crash artefact the readers also skip)."""
        try:
            data = self.path.read_bytes()
        except FileNotFoundError:
            return []
        end = data.rfind(b"\n") + 1
        return [ln for ln in data[:end].split(b"\n") if ln.strip()]

    def _head(self) -> dict | None:
        try:
            h = json.loads(self.head_path.read_text())
        except (OSError, ValueError):
            return None
        return h if isinstance(h, dict) else None

    # ---- verification
    def verify(self) -> Verification:
        v = Verification()
        raws = self._raw()
        head = self._head()
        if not raws:
            if head and int(head.get("n") or 0) >= 0 and head.get("head"):
                v.ok = False
                v.problems.append(f"{self.name} ledger is missing or empty but its signed head says it held lines (removed or truncated)")
            return v
        prev, seq, sealed_seen, legacy_bytes = GENESIS, -1, False, []
        for i, raw in enumerate(raws, 1):
            try:
                obj = json.loads(raw)
            except ValueError:
                obj = None
            if not isinstance(obj, dict):
                if sealed_seen:
                    v.problems.append(f"{self.name} line {i}: not a JSON object (edited by hand?)")
                else:
                    legacy_bytes.append(raw)          # a torn line written before the ledger was sealed: covered by the seal's hash
                continue
            if "sig" not in obj:
                if sealed_seen:
                    v.problems.append(f"{self.name} line {i}: unsigned line after the ledger was sealed (appended by hand)")
                else:
                    legacy_bytes.append(raw)
                continue
            if not sealed_seen:
                sealed_seen = True
                if obj.get("op") == "seal":
                    want = hashlib.sha256(b"\n".join(legacy_bytes) + (b"\n" if legacy_bytes else b"")).hexdigest()
                    if obj.get("legacy_sha256") != want or obj.get("legacy_lines") != len(legacy_bytes):
                        v.problems.append(f"{self.name}: lines written before the ledger was sealed were changed")
                elif legacy_bytes:
                    v.problems.append(f"{self.name}: unsealed lines precede the first signed line without a seal record")
            if obj.get("prev") != prev:
                v.problems.append(f"{self.name} line {i}: chain broken (a line was removed, inserted or reordered)")
            if obj.get("seq") != seq + 1:
                v.problems.append(f"{self.name} line {i}: sequence number {obj.get('seq')!r}, expected {seq + 1}")
            if not hmac.compare_digest(str(obj.get("sig")), _line_sig(self.name, obj)):
                v.problems.append(f"{self.name} line {i}: signature invalid (edited by hand, or written with another key)")
            prev = str(obj.get("sig"))
            seq = obj.get("seq") if isinstance(obj.get("seq"), int) else seq + 1
            v.n += 1
        if not sealed_seen:
            v.legacy = True
            if head and head.get("head"):
                v.problems.append(f"{self.name}: the signed head exists but no signed line does (lines were replaced)")
        else:
            if head is None:
                v.problems.append(f"{self.name}: the signed head record is missing (removed to hide a truncation?)")
            else:
                hn = head.get("n")
                if not verify_mac(head.get("mac"), "head|" + self.name, hn, head.get("head")):
                    v.problems.append(f"{self.name}: the signed head record was edited")
                elif not isinstance(hn, int) or hn > seq:
                    v.problems.append(f"{self.name}: the file ends at line {seq} but its signed head says {hn} (tail removed)")
                elif hn < seq - 1:
                    v.problems.append(f"{self.name}: lines were appended after the head without going through ave")
                elif hn == seq and head.get("head") != prev:
                    v.problems.append(f"{self.name}: the last line is not the one the signed head recorded")
        v.ok = not v.problems
        return v

    def rows(self) -> list[dict]:
        out = []
        for raw in self._raw():
            try:
                o = json.loads(raw)
            except ValueError:
                continue
            if isinstance(o, dict):
                out.append(o)
        return out

    # ---- writing (the caller holds `ledger.ledger_lock`)
    def append_locked(self, obj: dict) -> dict:
        from . import ledger
        rows = self.rows()
        sealed = [r for r in rows if "sig" in r]
        if not sealed:
            legacy = [ln for ln in self._raw()]
            if legacy:                                  # a file written before sealing existed: the seal line covers its bytes
                data = b"\n".join(legacy) + b"\n"
                seal = {"op": "seal", "legacy_lines": len(legacy), "legacy_sha256": hashlib.sha256(data).hexdigest(), "seq": 0, "prev": GENESIS}
                seal["sig"] = _line_sig(self.name, seal)
                ledger._append(self.path, seal)
                self._write_head(0, seal["sig"])
                sealed = [seal]
        last = sealed[-1] if sealed else None
        row = {**obj, "seq": int(last["seq"]) + 1 if last else 0, "prev": last["sig"] if last else GENESIS}
        row["sig"] = _line_sig(self.name, row)
        ledger._append(self.path, row)
        self._write_head(row["seq"], row["sig"])
        return row

    def _write_head(self, n: int, sig: str) -> None:
        self.head_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.head_path.with_name(self.head_path.name + ".tmp")
        tmp.write_text(json.dumps({"n": n, "head": sig, "mac": mac("head|" + self.name, n, sig)}) + "\n")
        os.replace(tmp, self.head_path)

    def append(self, obj: dict) -> dict:
        from . import ledger
        with ledger.ledger_lock(self.sp):
            return self.append_locked(obj)


def log(sp, name: str) -> SealedLog:
    return SealedLog(sp, name)


def verify_all(sp) -> list[str]:
    """Every problem in every sealed ledger (empty = all intact). Reads only; never writes."""
    out: list[str] = []
    for name in LOGS:
        out += SealedLog(sp, name).verify().problems
    return out
