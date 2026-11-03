"""Live attestation (spec 2.5): `accounts/<id>.live.json` = {"account","live","by","at","seq","cfg","sig"} with
sig = HMAC(approval_key(), "live|<account>|<by>|<at>|on|off|<seq>|<cfg>"), the same primitive as `safety.sign_approval`.

A hand-edited, copied, or unsigned file fails `verify_live`, and an account without a valid attestation is dry-run only, always.
`cfg` binds the attestation to the account's platform, mode, time zone and credentials (changing them voids it); `seq` is a counter the sealed
control ledger also holds, so `--off` writes a signed "off" record instead of unlinking and a saved copy of an older "on" file cannot be replayed.

HONEST LIMIT (same as ave.humanpresence): the HMAC key lives in the keychain of the same OS user; this stops accidents and hand edits, it
does not stop an agent that deliberately reads the key. The real control is the harness deny rules for `ave studio account live`.
`write_live` is the library primitive (tests call it); the human act is `account_live_command`, which calls `require_human` first and
makes the human type the account handle.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .. import humanpresence
from ..publish import safety


@dataclass(frozen=True)
class LiveStatus:
    ok: bool
    reason: str = ""
    by: str | None = None
    at: str | None = None


def live_path(accounts_dir: Path, account_id: str) -> Path:
    return Path(accounts_dir) / f"{account_id}.live.json"


CRED_FIELDS = ("platform", "mode", "credentials", "tz")


def config_hash(accounts_dir: Path, account_id: str) -> str:
    """What the human attested live posting FOR: the account's platform, mode, time zone and EVERY credential-bearing field (`credentials.*`:
    ref, page_id, ig_user_id, handle ...). Changing any of them (`ave studio account set`, or an edit of the YAML) changes this hash and so
    voids the attestation until a human attests again. Label, post types, limits and quiet hours are policy, not identity, and are not in it."""
    import yaml
    try:
        cfg = yaml.safe_load((Path(accounts_dir) / f"{account_id}.yaml").read_text()) or {}
    except (OSError, yaml.YAMLError):
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    bound={"id":account_id,**{k:cfg.get(k) for k in CRED_FIELDS}}
    if (cfg.get('connection') or {}).get('root_revision'):bound['native_connection']=cfg['connection']
    return hashlib.sha256(safety.canonical(bound).encode()).hexdigest()


def sign_live(account_id: str, by: str, at: str, live: bool = True, seq: int = 0, cfg: str = "") -> str:
    """HMAC over the account, who, when, the boolean state, the sequence number AND the account-config hash, so a signed "off" file cannot be
    hand-flipped to "on", an older signed file cannot be replayed over a newer one, and re-pointing the account at other credentials voids it."""
    return hmac.new(safety.approval_key(), f"live|{account_id}|{by}|{at}|{'on' if live else 'off'}|{seq}|{cfg}".encode(), hashlib.sha256).hexdigest()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _control(accounts_dir: Path):
    """The sealed control ledger next to the accounts folder (the studio root is the accounts folder's parent)."""
    from types import SimpleNamespace

    from ..studio import seal
    return seal.log(SimpleNamespace(ledger_dir=Path(accounts_dir).parent / "ledger"), "control")


def latest_seq(accounts_dir: Path, account_id: str) -> int | None:
    """Highest sequence number the sealed control ledger recorded for this account's live attestation (None = none recorded)."""
    best = None
    for r in _control(accounts_dir).rows():
        if r.get("op") == "live" and r.get("account") == account_id and isinstance(r.get("seq_live"), int):
            best = r["seq_live"] if best is None else max(best, r["seq_live"])
    return best


def write_live(accounts_dir: Path, account_id: str, by: str, at: str | None = None, live: bool = True) -> dict:
    """Write a signed attestation (`live=False` writes a signed "off" file with the next sequence number). Library primitive: NOT human-gated by itself
    (the CLI hook below is). Every write is also recorded in the sealed control ledger, which is what makes an older signed file unreplayable."""
    if not by or not by.strip():
        raise ValueError("an attestation names who enabled live posting")
    at = at or _now_iso()
    cfg = config_hash(accounts_dir, account_id)
    seq = (latest_seq(accounts_dir, account_id) or 0) + 1
    rec = {"account": account_id, "live": bool(live), "by": by.strip(), "at": at, "seq": seq, "cfg": cfg,
           "sig": sign_live(account_id, by.strip(), at, bool(live), seq, cfg)}
    _control(accounts_dir).append({"op": "live", "account": account_id, "seq_live": seq, "live": bool(live), "by": by.strip(), "at": at, "cfg": cfg})
    p = live_path(accounts_dir, account_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, indent=2) + "\n")
    os.replace(tmp, p)
    return rec


def revoke_live(accounts_dir: Path, account_id: str, by: str = "ave") -> bool:
    """Back to dry-run. The safe direction, so this needs no human check (the CLI hook still asks for one). It does NOT unlink the file: it
    writes a SIGNED "off" record with the next sequence number, so restoring a copy of the older "on" file fails (its sequence is stale)."""
    p = live_path(accounts_dir, account_id)
    if not p.exists():
        return False
    write_live(accounts_dir, account_id, by, live=False)
    return True


def verify_live(accounts_dir: Path, account_id: str) -> LiveStatus:
    p = live_path(accounts_dir, account_id)
    if not p.is_file():
        return LiveStatus(False, "no live attestation (dry-run only)")
    try:
        rec = json.loads(p.read_text())
    except (OSError, ValueError):
        return LiveStatus(False, "live attestation is unreadable")
    if not isinstance(rec, dict):
        return LiveStatus(False, "live attestation is malformed")
    by, at, sig, seq, cfg = rec.get("by"), rec.get("at"), rec.get("sig"), rec.get("seq"), rec.get("cfg")
    if rec.get("account") != account_id:
        return LiveStatus(False, "live attestation belongs to another account")
    flag = rec.get("live")
    if (not isinstance(flag, bool) or not isinstance(by, str) or not by or not isinstance(at, str) or not at or not isinstance(sig, str)
            or not isinstance(seq, int) or isinstance(seq, bool) or not isinstance(cfg, str)):
        return LiveStatus(False, "live attestation signature invalid (incomplete, or made by an older version: attest again)")
    if not hmac.compare_digest(sig, sign_live(account_id, by, at, flag, seq, cfg)):
        return LiveStatus(False, "live attestation signature invalid (file edited by hand?)")
    if flag is not True:
        return LiveStatus(False, "live attestation is signed off (live: false)", by, at)
    if cfg != config_hash(accounts_dir, account_id):
        return LiveStatus(False, "the account's platform, mode, time zone or credentials changed since a human attested it live: attest again "
                                 "(ave studio account live --on)", by, at)
    newest = latest_seq(accounts_dir, account_id)
    if newest is None:
        return LiveStatus(False, "no sealed record of this attestation (a copied file?): attest again", by, at)
    if seq < newest:
        return LiveStatus(False, f"stale attestation (sequence {seq}, the account is at {newest}): an older signed file was restored", by, at)
    return LiveStatus(True, "", by, at)


def account_live_command(sp, account_id: str, *, on: bool, by: str, ask: Callable[[str], str] = input) -> dict:
    """`ave studio account live <id> --on|--off`: a HUMAN act. Refuses without a human terminal (`require_human`), then for --on makes
    the human type the account's handle (its label without `@`, else its id), as `ave publish approve` makes them type a hash."""
    from .accounts import load_account
    humanpresence.require_human("changing an account's live posting switch",
                                f"ave studio account live {account_id} {'--on' if on else '--off'}")
    acct = load_account(sp, account_id)
    if not on:
        return {"account": account_id, "live": False, "removed": revoke_live(sp.accounts_dir, account_id, by)}
    handle = (acct.label or acct.id).lstrip("@")
    typed = ask(f"Enable LIVE posting to {acct.platform} account {acct.label or acct.id}. Type its handle ({handle}) to confirm: ").strip().lstrip("@")
    if typed != handle:
        raise SystemExit(f"typed handle {typed!r} does not match {handle!r}; nothing changed")
    rec = write_live(sp.accounts_dir, account_id, by)
    return {"account": account_id, "live": True, "by": rec["by"], "at": rec["at"]}
