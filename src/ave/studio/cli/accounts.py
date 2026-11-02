"""Accounts group: account add|list|show|set|remove|auth|live|refresh, pause, resume.

Credentials NEVER go through argv or a file: `account auth` (a human act) reads each secret from a hidden prompt into the OS keychain.
`account live --on/--off` (a human act) writes or removes the HMAC attestation; nothing else makes an account live. `pause` is open to any
actor (the safe direction); `resume` needs a human.
"""

from __future__ import annotations

import getpass
import json
import sys
from pathlib import Path

import yaml

from ...platforms import attest, registry
from ...platforms.accounts import PUBLIC_FIELDS, Account, AccountError, account_path, list_accounts, load_account, save_account
from ...publish import safety
from .. import dispatch, model, store
from . import Blocked, add_dir, csv, guarded, human_gate, out, parse_value, say, studio, project, with_dir


def _view(sp, acct: Account) -> dict:
    st = attest.verify_live(sp.accounts_dir, acct.id)
    return {"id": acct.id, "platform": acct.platform, "mode": acct.mode, "label": acct.label, "tz": acct.tz, "post_types": list(acct.post_types),
            "live": st.ok, "live_reason": "" if st.ok else st.reason, "paused": acct.paused(), "limits": acct.limits, "credentials_ref": acct.cred_ref}


@guarded
def cmd_add(a) -> int:
    sp = project(a)
    if account_path(sp, a.id).exists() and not a.replace:
        raise Blocked(f"account {a.id} already exists (use `account set`, or --replace to rewrite it)")
    creds: dict = {"ref": a.cred_ref or a.id}
    for pair in a.public or []:
        k, _, v = pair.partition("=")
        if k not in PUBLIC_FIELDS or not v:
            raise ValueError(f"--public takes key=value with key one of: {', '.join(PUBLIC_FIELDS)}")
        creds[k] = v
    cfg = {"id": a.id, "platform": a.platform, "mode": a.mode, "label": a.label or a.id, "tz": a.tz or sp.config().get("tz") or "UTC",
           "post_types": csv(a.post_types) or ["feed"], "credentials": creds, "catch_up": False,
           **({"brand": a.brand} if getattr(a, "brand", None) else {}),
           "limits": {"max_per_day": a.max_per_day, "min_gap_min": a.min_gap_min}}
    acct = save_account(sp, cfg)
    out({"ok": True, "account": _view(sp, acct), "next": [f"ave studio account auth {a.id}    (human: stores the credentials in the keychain)",
                                                          f"ave studio account live {a.id} --on    (human: only when you want real posting)"]})
    return 0


@guarded
def cmd_list(a) -> int:
    s = studio(a, adapters=False)
    out({"ok": True, "accounts": s.accounts()})
    return 0


@guarded
def cmd_show(a) -> int:
    sp = project(a)
    acct = load_account(sp, a.id)
    adapter = registry.adapter_for(acct)
    st = adapter.status()
    out({"ok": True, "account": _view(sp, acct), "config": yaml.safe_load(account_path(sp, a.id).read_text()),
         "adapter": {k: st.get(k) for k in ("platform", "mode", "ready", "live", "problems", "verified", "capabilities", "needs_public_url")},
         "secrets_needed": [acct.secret_name(f) for f in getattr(adapter, "required", ())]})
    return 0


def _set_path(cfg: dict, dotted: str, value) -> None:
    cur = cfg
    parts = dotted.split(".")
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
        if not isinstance(cur, dict):
            raise ValueError(f"{dotted}: {p} is not a mapping")
    cur[parts[-1]] = value


@guarded
def cmd_set(a) -> int:
    sp = project(a)
    load_account(sp, a.id)                      # must exist and parse
    cfg = yaml.safe_load(account_path(sp, a.id).read_text())
    for pair in a.pairs:
        k, sep, v = pair.partition("=")
        if not sep or not k:
            raise ValueError(f"expected key=value, got {pair!r}")
        if k in ("id",):
            raise ValueError("an account's id cannot change (remove and add it)")
        value = csv(v) if k == "post_types" and not v.strip().startswith("[") else parse_value(v)
        _set_path(cfg, k, value)
    was_live = attest.verify_live(sp.accounts_dir, a.id).ok
    acct = save_account(sp, cfg)                # validates; refuses anything secret-shaped
    now_live = attest.verify_live(sp.accounts_dir, a.id)
    note = "the live attestation is untouched: only `account live` changes it"
    if was_live and not now_live.ok:
        note = ("the live attestation is now VOID (" + now_live.reason + "). The account posts nothing until a human runs "
                f"`ave studio account live {a.id} --on` for the new configuration.")
    out({"ok": True, "account": _view(sp, acct), "live_voided": bool(was_live and not now_live.ok), "note": note})
    return 0


@guarded
def cmd_remove(a) -> int:
    sp = project(a)
    load_account(sp, a.id)
    using = [(p["id"], v["id"]) for p in store.list_posts(sp) for v in p.get("variants") or []
             if v.get("account") == a.id and not v.get("dropped") and (v.get("result") or {}).get("state") != "published"]
    if using and not a.force:
        raise Blocked(f"{len(using)} unfinished variant(s) still use {a.id} (e.g. {using[0][0]}/{using[0][1]}); drop them first or pass --force",
                      variants=[f"{p}/{v}" for p, v in using[:10]])
    attest.revoke_live(sp.accounts_dir, a.id)
    (sp.accounts_dir / f"{a.id}.paused").unlink(missing_ok=True)
    account_path(sp, a.id).unlink()
    out({"ok": True, "removed": a.id, "note": "keychain secrets are not touched; remove them in Keychain Access if you want them gone"})
    return 0


@guarded
def cmd_auth(a) -> int:
    """HUMAN. Each secret the platform needs is read from a hidden prompt into the keychain (never argv, never a file)."""
    human_gate(a, f"entering credentials for {a.id}", f"ave studio account auth {a.id}")
    sp = project(a)
    acct = load_account(sp, a.id)
    adapter = registry.adapter_for(acct)
    names = list(getattr(adapter, "required", ()))
    if not names:
        raise Blocked(f"{acct.platform} ({acct.mode}) has no secrets to enter here; see docs/publishing-setup.md for its own flow")
    stored = []
    for f in names:
        if acct.public.get(f):
            continue                                  # a public id kept in the account file
        secret = getpass.getpass(f"{acct.secret_name(f)} (hidden): ").strip()
        if secret:
            safety.set_secret(acct.secret_name(f), secret)
            stored.append(acct.secret_name(f))
    res = {"ok": True, "stored_in_keychain": stored, "still_missing": acct.missing(tuple(names))}
    if a.verify:
        cs = adapter.verify_credentials()           # one read-only network call, only because the human asked
        res["verify"] = {"valid": cs.valid, "present": cs.present, "detail": cs.detail}
    out(res)
    return 0


@guarded
def cmd_live(a) -> int:
    """HUMAN. `--on` makes the human type the account's handle; the file is HMAC-signed (platforms.attest)."""
    flag = "--on" if a.on else "--off"
    human_gate(a, "changing an account's live posting switch", f"ave studio account live {a.id} {flag}")
    sp = project(a)
    res = attest.account_live_command(sp, a.id, on=a.on, by=a.by or getpass.getuser())
    out({"ok": True, **res})
    return 0


@guarded
def cmd_refresh(a) -> int:
    sp = project(a)
    acct = load_account(sp, a.id)
    adapter = registry.adapter_for(acct)
    fn = getattr(adapter, "refresh", None)
    if fn is None:
        raise Blocked(f"{acct.platform} has no token refresh here (its tokens are not long-lived exchanges); re-run `ave studio account auth {a.id}` if it expired")
    out({"ok": True, **fn()})
    return 0


@guarded
def cmd_pause(a) -> int:
    s = studio(a, adapters=False)
    out({"ok": True, **s.pause(account=a.account, by=a.by or "claude")})
    return 0


@guarded
def cmd_resume(a) -> int:
    human_gate(a, "resuming sends", "ave studio resume" + (f" --account {a.account}" if a.account else ""))
    s = studio(a, adapters=False)
    out({"ok": True, **s.resume(account=a.account, by=a.by or getpass.getuser(), pin_verified=True)})
    return 0


def add(ss) -> None:
    acc = ss.add_parser("account", help="accounts: add, list, show, set, remove, auth (human), live (human), refresh")
    sub = acc.add_subparsers(dest="account_cmd", required=True)
    p = sub.add_parser("add", help="write accounts/<id>.yaml (no secrets)")
    p.add_argument("id")
    p.add_argument("--platform", required=True)
    p.add_argument("--mode", default="graph", choices=["graph", "aggregator", "inbox", "manual", "direct", "native"])
    p.add_argument("--label")
    p.add_argument("--brand", help="group accounts into brands (one studio, two businesses); the UI's brand filter reads it")
    p.add_argument("--tz")
    p.add_argument("--post-types", help="comma list, e.g. feed,carousel,story")
    p.add_argument("--cred-ref", help="secret suffix (default: the account id)")
    p.add_argument("--public", action="append", metavar="KEY=VALUE", help="a non-secret id the adapter needs (ig_user_id=..., page_id=..., handle=...)")
    p.add_argument("--max-per-day", type=int, default=2)
    p.add_argument("--min-gap-min", type=int, default=180)
    p.add_argument("--replace", action="store_true")
    add_dir(p)
    p.set_defaults(fn=cmd_add)
    p = sub.add_parser("list")
    add_dir(p)
    p.set_defaults(fn=cmd_list)
    p = sub.add_parser("show")
    p.add_argument("id")
    add_dir(p)
    p.set_defaults(fn=cmd_show)
    p = sub.add_parser("set", help="change fields: key=value (dotted keys; JSON values)")
    p.add_argument("id")
    p.add_argument("pairs", nargs="+", metavar="KEY=VALUE")
    add_dir(p)
    p.set_defaults(fn=cmd_set)
    p = sub.add_parser("remove")
    p.add_argument("id")
    p.add_argument("--force", action="store_true")
    add_dir(p)
    p.set_defaults(fn=cmd_remove)
    p = sub.add_parser("auth", help="HUMAN: enter credentials into the keychain from hidden prompts")
    p.add_argument("id")
    p.add_argument("--verify", action="store_true", help="after storing, make one read-only call to check the credentials (network)")
    add_dir(p)
    p.set_defaults(fn=cmd_auth)
    p = sub.add_parser("live", help="HUMAN: switch real posting on or off for an account (signed attestation)")
    p.add_argument("id")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--on", action="store_true")
    g.add_argument("--off", dest="on", action="store_false")
    p.add_argument("--by")
    add_dir(p)
    p.set_defaults(fn=cmd_live)
    p = sub.add_parser("refresh", help="re-exchange a long-lived token (adapters that support it; network)")
    p.add_argument("id")
    add_dir(p)
    p.set_defaults(fn=cmd_refresh)
    p = ss.add_parser("pause", help="stop all sending (any actor may); --account pauses one account")
    p.add_argument("--account")
    p.add_argument("--by")
    add_dir(p)
    p.set_defaults(fn=cmd_pause)
    p = ss.add_parser("resume", help="HUMAN: clear a pause")
    p.add_argument("--account")
    p.add_argument("--by")
    add_dir(p)
    p.set_defaults(fn=cmd_resume)
