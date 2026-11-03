"""`ave studio doctor` (spec 2.5, 4 "Honesty rule", 12): read-only checks, no network, no secret values.

Every check is {name, status: ok|warn|fail, detail}. `fail` = something that would make a send unsafe or the studio unusable; `warn` = worth knowing.
Verified-ness of every adapter is printed as `verified: false` until a burn-in flips it in config/platforms.yaml.
"""

from __future__ import annotations

from pathlib import Path

from .. import library
from ..platforms import attest
from ..platforms.accounts import check_accounts, find_secrets, list_accounts
from ..publish import queue as pq
from . import dispatch, model, seal, sendgate, state, store


def _c(name: str, status: str, detail: str, **extra) -> dict:
    return {"name": name, "status": status, "detail": detail, **extra}


def run(studio) -> dict:
    sp, now = studio.sp, studio.now()
    checks: list[dict] = []
    # project
    probs = model.validate("studio", sp.read_meta())
    checks.append(_c("studio.json", "fail" if probs else "ok", "; ".join(probs) if probs else f"{sp.read_meta().get('title')} ({sp.config().get('tz')})"))
    bad_posts = []
    for p in sorted(sp.posts_dir.glob("*.json")) if sp.posts_dir.is_dir() else []:
        try:
            post = store.load_post(sp, p.stem)
            pr = model.validate("post", post)
            if pr:
                bad_posts.append(f"{p.name}: {pr[0]}")
        except Exception as e:                      # a corrupt post file must not hide the rest
            bad_posts.append(f"{p.name}: {type(e).__name__}")
    checks.append(_c("posts", "fail" if bad_posts else "ok", "; ".join(bad_posts[:5]) if bad_posts else f"{len(list(sp.posts_dir.glob('*.json')))} post file(s) valid"))
    # accounts
    broken = check_accounts(sp)
    accts = list_accounts(sp)
    checks.append(_c("accounts", "fail" if broken else ("warn" if not accts else "ok"),
                     "; ".join(f"{k}: {v}" for k, v in broken.items()) if broken else (f"{len(accts)} account(s)" if accts else "no accounts yet: `ave studio account add`")))
    # secrets in files
    home = Path.home() / ".config" / "ave"
    leaks = find_secrets(sp.root) + (find_secrets(home) if home.is_dir() else [])
    checks.append(_c("secrets_in_files", "fail" if leaks else "ok", ("token-shaped values in: " + ", ".join(leaks)) if leaks else "no token-shaped value in the studio or ~/.config/ave"))
    # live attestations: a file that exists but does not verify is a hand edit, a copy, or another machine's key
    for a in accts:
        lp = attest.live_path(sp.accounts_dir, a.id)
        st = attest.verify_live(sp.accounts_dir, a.id)
        if lp.exists() and not st.ok:
            checks.append(_c(f"live:{a.id}", "fail", st.reason))
        else:
            checks.append(_c(f"live:{a.id}", "ok", f"LIVE (attested by {st.by} at {st.at})" if st.ok else "dry-run (no attestation)"))
    # every queue item: approvals must verify against the CURRENT bytes and metadata
    stale = []
    for item in pq.list_items(sp):
        if item["status"] == "approved":
            try:
                for b in pq.send_blockers(sp, item):
                    stale.append(f"{item['id']}: {b}")
            except (OSError, ValueError, KeyError) as e:
                stale.append(f"{item['id']}: cannot verify ({e})")
    checks.append(_c("queue_approvals", "fail" if stale else "ok", "; ".join(stale[:5]) if stale else "every approved item verifies (signature, bytes, metadata)"))
    # library
    lib = library.verify(sp)
    checks.append(_c("library", "fail" if not lib["ok"] else "ok", "; ".join(lib["problems"][:5]) if not lib["ok"] else f"{lib['assets']} asset(s): hashes and license chain intact"))
    # adapters, honestly unverified
    adapters = []
    try:
        from ..platforms import registry
        for r in registry.adapter_report(sp, now=studio.clock.now):
            adapters.append({"account": r.get("account"), "platform": r.get("platform"), "ready": r.get("ready"), "live": r.get("live"),
                             "verified": bool(r.get("verified", False)), "problems": r.get("problems", [])})
    except Exception as e:
        checks.append(_c("adapters", "warn", f"adapter report failed: {type(e).__name__}"))
    for r in adapters:
        checks.append(_c(f"adapter:{r['account']}", "warn" if r["problems"] else "ok",
                         f"{r['platform']}: verified: {str(r['verified']).lower()} (documentation-derived, never run against a live account); "
                         + ("ready" if r["ready"] else "; ".join(r["problems"][:2])), verified=r["verified"]))
    # media host
    needs_host = [a.id for a in accts if a.platform in ("instagram", "threads") and a.mode == "graph"]
    if needs_host:
        from ..platforms import host as hostmod
        hs = hostmod.host_status()
        checks.append(_c("media_host", "ok" if hs["configured"] else "warn",
                         "configured" if hs["configured"] else f"Instagram/Threads image posts need a media host ({', '.join(needs_host)}): missing " + ", ".join(hs["missing"])))
    # scheduler
    try:
        from ..episode import read_json
        from ..schedule import tick as _tick
        hb = _tick.heartbeat_path(sp)
        if hb.is_file():
            from .clock import parse_ts
            age = (now - parse_ts(read_json(hb)["at"])).total_seconds()
            checks.append(_c("heartbeat", "warn" if age > 180 else "ok", f"last sending tick {int(age)} s ago"))
        else:
            checks.append(_c("heartbeat", "ok", "the scheduler has not sent anything yet (rehearsals leave no heartbeat)"))
    except Exception:
        pass
    paused = dispatch.is_paused(sp)
    checks.append(_c("pause", "warn" if paused else "ok", "the studio is PAUSED (nothing sends)" if paused else "not paused"))
    gone = dispatch.pause_removed(sp)
    checks.append(_c("pause_files", "fail" if gone else "ok", ("a pause was removed without `ave studio resume`: " + ", ".join(gone) + " (still treated as paused)")
                     if gone else "every pause file matches the sealed control ledger"))
    # tamper evidence: the hash-chained ledgers (feedback, dispatch, control) and the unattended-sender permission
    for name in seal.LOGS:
        v = seal.log(sp, name).verify()
        checks.append(_c(f"ledger:{name}", "fail" if not v.ok else "ok", "; ".join(v.problems[:4]) if not v.ok else
                         ("sealed, intact" if v.n else "empty" + (" (unsealed legacy lines are sealed on the next write)" if v.legacy else ""))))
    if sendgate.path(sp).exists():
        st = sendgate.verify(sp)
        checks.append(_c("send.enabled", "ok" if st.ok else "fail", f"unattended sending allowed by {st.by} at {st.at}" if st.ok else st.reason))
    else:
        checks.append(_c("send.enabled", "ok", "no unattended sender is allowed (a human starts `tick --send` in a terminal)"))
    open_fb, open_al = len(studio.feedback("open")), len(studio.alerts())
    checks.append(_c("inbox", "ok", f"{open_fb} open feedback, {open_al} unacknowledged alert(s)"))
    fails = [c for c in checks if c["status"] == "fail"]
    return {"ok": not fails, "failures": len(fails), "warnings": len([c for c in checks if c["status"] == "warn"]), "checks": checks,
            "verified_platforms": [], "note": "No platform adapter has run against a live account: every `verified` is false until a human burn-in."}
