"""Send-time gates (spec 6.2 b, 6.3, 6.4): everything that must be true at the moment a variant goes out, plus the crash bookkeeping.

`blockers(...)` is the ONLY place the gate list lives. Nothing is sent unless it returns empty AND the caller passed `send=True` AND the
queue's own checks pass again inside `queue.run` (signature, bytes, metadata, immediately before each platform). The gates, in order:

    dropped / no queue item / not a studio (meta_v 2) item / queue item not bound to THIS account
    queue.send_blockers: status approved, valid HMAC signature, file sha == approved sha, metadata hash == approved hash
    the queue item still equals the post (caption, copy extras, alt texts, every file sha): what the human reviewed is what goes out
    the approval's signed binding (account, slot, lifetime policy) is intact: no hand-edited slot, no raised TTL since approval
    the approval was not already used (single-use: a signed dispatch ledger, so a hand-reset queue file cannot send it twice)
    approval age <= the studio/account TTL (policy.approval_ttl_days), judged on the injected clock
    the account exists and holds a valid, HMAC-attested `live` file (account_live)
    no open rework/retime/drop feedback on the variant (BLOCKING_FEEDBACK)
    no studio PAUSE and no per-account pause (a pause also lives in the sealed control ledger: `rm PAUSE` does not resume)
    license closure over every file of the QUEUE ITEM (derived_from included) is covered and human-attested
    a media host is configured when the adapter needs a public URL
    on send: an account-bound adapter exists and its own `ready_problems()` are empty (credentials, live)

Crash bookkeeping (6.4): `dispatch_key` = sha256(variant | queue sha | metadata hash | slot utc), written as a `dispatch_started` event
BEFORE `queue.run(send=True)` and closed by `dispatch_finished` / `dispatch_resolved`. A started key with no close means "may have posted":
`open_dispatches` reports it and the tick does nothing for that variant until a human runs `resolve_uncertain`.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from ..publish import queue as pq, safety
from . import ledger, model, policy, seal, state, store
from .clock import SystemClock, iso_utc, parse_ts
from .events import Actor, append_event, make_event


# Send-back feedback a human has filed and no agent has resolved yet. WS4's send-back does not demote the queue approval, so the
# dispatch gate itself must refuse: a variant the human said to redo, retime or drop never goes out while that note is open.
BLOCKING_FEEDBACK = ("rework_copy", "rework_visual", "retime", "drop")


@dataclass(frozen=True)
class Blocker:
    code: str
    message: str

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


# ------------------------------------------------------------------------------------------------ live attestation
# ONE implementation lives in ave.platforms.attest (`verify_live`, HMAC over account|by|at|on/off). These are thin views of it.

def live_problems(sp, account_id: str) -> list[str]:
    """Why `account_id` is NOT attested live (empty = a human attested it and the file is untouched). No attestation = dry-run only, always."""
    from ..platforms import attest
    st = attest.verify_live(sp.accounts_dir, model.check_id(account_id, "account id"))
    return [] if st.ok else [f"account {account_id} is not live: {st.reason}"]


def account_live(sp, account_id: str) -> bool:
    return not live_problems(sp, account_id)


# ------------------------------------------------------------------------------------------------ adapters

def adapter_for(publishers: Mapping[str, Any] | None, variant: Mapping) -> Any | None:
    """The account-bound adapter: keyed by account id first (two Instagram accounts need two adapters), else by platform."""
    pubs = publishers or {}
    return pubs.get(variant.get("account")) or pubs.get(variant.get("platform"))


def queue_publishers(publishers: Mapping[str, Any] | None, variant: Mapping) -> dict:
    """The `publishers={platform: adapter}` map `queue.run` expects, holding ONLY this variant's adapter (never a global fallback)."""
    a = adapter_for(publishers, variant)
    return {variant["platform"]: a} if a is not None else {}


# ------------------------------------------------------------------------------------------------ license closure

def license_gaps(sp, asset_ids: list[str]) -> list[str]:
    """Unlicensed leaves in the `derived_from` closure of `asset_ids`. Uses the library's `coverage` once WS1 provides it; until then a
    registry-only check: every leaf asset needs a license entry, its file must exist, and its bytes must match the registered sha256."""
    try:
        from .. import library
        cov = getattr(library, "coverage", None)
    except ImportError:
        cov = None
    if cov is not None:
        res = cov(sp, list(asset_ids))
        return [f"{m.get('id', m) if isinstance(m, dict) else m}: not covered by a license entry" for m in res.get("missing", [])]
    assets = store.read_assets(sp)
    gaps, seen, todo = [], set(), list(asset_ids)
    while todo:
        a = todo.pop()
        if a in seen:
            continue
        seen.add(a)
        rec = assets.get(a)
        if rec is None:
            gaps.append(f"{a}: not in the library registry")
            continue
        f = sp.root / (rec.get("file") or "missing")
        if not f.is_file():
            gaps.append(f"{a}: file missing from the library")
        elif safety.sha256_file(f) != rec.get("sha256"):
            gaps.append(f"{a}: bytes differ from the registered sha256")
        parents = rec.get("derived_from") or []
        if parents:
            todo.extend(parents)
        elif not (rec.get("license") or {}).get("entry"):
            gaps.append(f"{a}: no license entry")
    return sorted(gaps)


def variant_asset_ids(post: Mapping, variant: Mapping) -> list[str]:
    return [m["asset"] for m in model.effective_media(post, variant)]


# ------------------------------------------------------------------------------------------------ the gates

def approval_age_problem(sp, variant: Mapping, item: Mapping, now: datetime) -> str | None:
    ttl = policy.approval_ttl_days(sp, variant.get("account"))
    exp = policy.approval_expires(item, ttl)
    if exp is not None and now >= exp:
        return f"approval expired {exp.strftime('%Y-%m-%d %H:%MZ')} (limit {ttl} days); approve again"
    return None


def queue_asset_ids(sp, ep, item: Mapping) -> tuple[list[str], list[str]]:
    """(library asset ids, problems) for the files the QUEUE ITEM would send. A file whose bytes are not a registered asset is a gap: the
    closure is computed over what is sent, never over what the post merely references."""
    from ..library import registry
    ids, problems = [], []
    for f in pq.resolve_files(ep, dict(item)):
        try:
            sha = safety.sha256_file(f)
        except OSError as e:
            problems.append(f"{Path(str(f)).name}: cannot read the file ({e})")
            continue
        rec = registry.find_by_sha(sp, sha)
        if rec is None:
            problems.append(f"{Path(str(f)).name}: these bytes are not a registered library asset (no license trail)")
        else:
            ids.append(rec["id"])
    return ids, problems


def approval_bind(sp, variant: Mapping) -> dict:
    """What the studio's human approval surfaces (CLI, UI) sign into an approval besides bytes and copy (`safety.attach_bind`): the variant's slot
    NOW and the studio/account approval lifetime in force NOW."""
    out={"slot": safety.norm_slot((variant.get("schedule") or {}).get("utc")),
         "ttl": {"studio_days": policy.approval_ttl_days(sp, variant.get("account"))}}
    cfg=policy.account_config(sp,variant.get('account'))
    if (cfg.get('connection') or {}).get('root_revision'):
        from .connections import binding
        from ..platforms.accounts import load_account
        out['connection']=binding(load_account(sp,variant['account']))
    return out


def approval_sig(item: Mapping) -> str | None:
    ap = (item.get("approvals") or [])
    return str(ap[-1].get("sig")) if ap and ap[-1].get("sig") else None


def blockers(sp, post: Mapping, variant: Mapping, *, now: datetime, publishers: Mapping[str, Any] | None = None, host: Any = None,
             send: bool = False) -> list[Blocker]:
    """Every reason `variant` may NOT be sent now (see the module docstring for the list). Empty = all gates open. No network."""
    out: list[Blocker] = []
    if (variant.get("delivery") or {}).get("method") == "manual":
        return [Blocker("manual_delivery", "Manual delivery never dispatches automatically")]
    if variant.get("dropped"):
        return [Blocker("dropped", "variant is dropped")]
    item = state.load_queue_item(sp, variant)
    if item is None:
        return [Blocker("no_queue", "no queue item (run queue sync)")]
    if item.get("meta_v") == 3 and (item.get("delivery") or {}).get("method") == "manual":
        return [Blocker("manual_delivery", "Manual delivery never dispatches automatically")]
    if item.get("meta_v") not in (2, 3):
        out.append(Blocker("not_studio_item", "the queue item is not a studio item (an adopted or legacy episode item carries no account binding): "
                                              "run `ave studio queue sync` to build a studio item and approve that"))
    if item.get("account") != variant["account"]:
        out.append(Blocker("account_mismatch", f"queue item is bound to account {item.get('account')!r}, not {variant['account']!r}"))
    ep = state.queue_episode(sp, variant)
    try:
        for b in pq.send_blockers(ep, item):
            out.append(Blocker("approval_expired" if "expired" in b else "approval", b))
    except (OSError, ValueError, KeyError) as e:
        out.append(Blocker("approval", f"cannot verify approval: {e}"))
    late = approval_age_problem(sp, variant, item, now)
    if late:
        out.append(Blocker("approval_expired", late))
    out += _binding_blockers(sp, variant, item)
    if item.get("meta_v") in (2, 3):
        try:
            from .sync import queue_mismatch
            diffs = queue_mismatch(sp, post, variant, item)
        except (OSError, ValueError, KeyError) as e:
            diffs = [f"cannot compare the queue item with the post: {e}"]
        if diffs:
            out.append(Blocker("queue_mismatch", "the queue item does not match the post (" + "; ".join(diffs) + "): run `ave studio queue sync`; "
                                                                                                         "a changed item needs a new approval"))
    acct = policy.account_config(sp, variant["account"])
    if not acct.get("id"):
        out.append(Blocker("no_account", f"unknown account {variant['account']!r}"))
    else:
        for m in live_problems(sp, variant["account"]):
            out.append(Blocker("not_live", m))
    for fb in ledger.open_feedback(sp, post=post["id"], variant=variant["id"], now=now):
        if fb.get("kind") in BLOCKING_FEEDBACK:
            out.append(Blocker("open_feedback", f"open {fb['kind']} feedback {fb['id']} (the human sent this back; an agent must resolve it first)"))
    if is_paused(sp):
        out.append(Blocker("paused", "the studio is paused (PAUSE)"))
    if is_paused(sp, variant["account"]):
        out.append(Blocker("account_paused", f"account {variant['account']} is paused"))
    ids = variant_asset_ids(post, variant)
    qids, qprob = queue_asset_ids(sp, ep, item) if item.get("meta_v") in (2, 3) else ([], [])
    gaps = license_gaps(sp, list(dict.fromkeys([*ids, *qids]))) + qprob
    if gaps:
        out.append(Blocker("license_gap", "license closure not covered: " + "; ".join(gaps)))
    adapter = adapter_for(publishers, variant)
    if getattr(adapter, "needs_public_url", False) and host is None:
        out.append(Blocker("host", "needs_public_url: configure a media host"))
    if send:
        if adapter is None:
            out.append(Blocker("no_adapter", f"no account-bound adapter for {variant['account']} ({variant['platform']})"))
        else:
            for m in adapter.ready_problems():
                out.append(Blocker("not_live" if "not live" in m else "credentials", f"{variant['account']}: {m}"))
    return out


def _binding_blockers(sp, variant: Mapping, item: Mapping) -> list[Blocker]:
    """The approval's signed binding: who it is for, which slot it covers, which lifetime policy was in force, and whether it was already used."""
    out: list[Blocker] = []
    ap = (item.get("approvals") or [None])[-1]
    if not ap or item.get("status") not in ("approved", "publishing", "failed"):
        return out                                          # `pq.send_blockers` already says there is no valid approval
    why = safety.bind_problem(ap)
    if why:
        out.append(Blocker("approval_binding", why))
        return out
    bind = ap["bind"]
    current_connection=approval_bind(sp,variant).get('connection')
    if current_connection and bind.get('connection')!=current_connection:
        out.append(Blocker('approval_binding','Connection identity or root credentials changed; review this payload for the new connection'))
    if bind.get("account") != variant["account"]:
        out.append(Blocker("account_mismatch", f"the approval was given for account {bind.get('account')!r}, not {variant['account']!r}"))
    ttl = bind.get("ttl") or {}
    now_ttl = {"publish_days": safety.approval_ttl_days(), "studio_days": policy.approval_ttl_days(sp, variant["account"])}
    raised = [k for k in ("publish_days", "studio_days") if k in ttl and now_ttl[k] > ttl[k]]
    if raised:
        out.append(Blocker("ttl_policy", "approval lifetime settings were raised after this approval (" + ", ".join(
            f"{k.split('_')[0]}: {ttl[k]:g} -> {now_ttl[k]:g} days" for k in raised) + "); approve again under the new policy"))
    expected = expected_slot(sp, ap)
    current = safety.norm_slot((variant.get("schedule") or {}).get("utc"))
    if current != expected:
        out.append(Blocker("schedule_changed", f"the slot changed after approval without a recorded retime (approved for {expected or 'no slot'}, now "
                                               f"{current or 'no slot'}); a human moves it in the studio, or approves again"))
    state_ = consumption(sp, ap.get("sig"))
    if state_ == "published":
        out.append(Blocker("approval_consumed", "this approval was already used to publish; approve the post again to post it again"))
    elif state_ == "review_required":
        out.append(Blocker("review_required", "Provider rejected an accepted operation under this approval; fresh human review and approval are required"))
    elif state_ == "open":
        out.append(Blocker("uncertain", "an earlier send with this approval did not finish; check the platform, then `ave studio resolve`"))
    return out


# ------------------------------------------------------------------------------------------------ the signed dispatch ledger (single-use approvals)

def consumption(sp, sig: str | None) -> str:
    """`free` | `open` (started, not closed, or ended uncertain) | `published` for the approval whose signature is `sig`, from the SEALED
    dispatch ledger; `review_required` permanently consumes a terminally failed accepted operation.
    A queue file reset by hand cannot change these states."""
    if not sig:
        return "free"
    rows=seal.log(sp, "dispatch").rows()
    # Terminal consumption is irreversible for this identity. Later ordinary failure,
    # release, or restored queue records cannot make the old approval free again.
    if any(r.get('approval')==sig and r.get('op')=='review_required' for r in rows):
        return 'review_required'
    st = "free"
    for r in rows:
        if r.get("approval") != sig:
            continue
        op = r.get("op")
        if op == "started":
            st = "open"
        elif op == "finished":
            st = {"published": "published", "uncertain": "open", "processing":"open", "scheduled":"open", "awaiting_user_publish":"open", "sandbox":"published", "draft":"open"}.get(r.get("outcome"), "free")
        elif op == "released":
            st = "free"
    return st


def queue_ledger_project(ep):
    """Studio queues use the existing sealed dispatch log; standalone episode queues
    use the same ledger implementation beside their publish artifacts."""
    if hasattr(ep,'ledger_dir'): return ep
    if (ep.root/'studio.json').is_file():
        from .project import StudioProject
        return StudioProject(ep.root)
    from types import SimpleNamespace
    return SimpleNamespace(ledger_dir=ep.publish/'ledger')


def approval_identity(sig):
    """Public correlation only: never expose a reusable approval signature in results."""
    return hashlib.sha256(('delivery-approval|'+sig).encode()).hexdigest() if sig else None


def terminal_review(ep,item):
    """Verified durable terminal evidence for the current approval, or None."""
    sig=approval_sig(item)
    if not sig: return None
    log=seal.log(queue_ledger_project(ep),'dispatch')
    verified=log.verify()
    if not verified.ok: raise ValueError('Cannot verify sealed dispatch ledger: '+'; '.join(verified.problems))
    return next((r for r in reversed(log.rows()) if r.get('approval')==sig and r.get('op')=='review_required'),None)


def require_terminal_review(ep,item,provider,result):
    """Persist before queue/post projection. Never grants or releases authority."""
    sp=queue_ledger_project(ep)
    with ledger.ledger_lock(sp):
        identity=result.get('approval_identity')
        sig=approval_sig(item)
        if not sig: raise ValueError('Accepted operation has no approval identity to consume')
        if identity and identity!=approval_identity(sig):
            raise ValueError('Operation result belongs to another approval; its sealed history must be projected separately')
        existing=terminal_review(ep,item)
        if existing: return existing
        result={**result,'approval_identity':approval_identity(sig)}
        return seal.log(sp,'dispatch').append_locked({'op':'review_required','approval':sig,'item':item['id'],
            'account':item.get('account'),'provider':provider,'result':result,'at':iso_utc(SystemClock().now())})


def project_terminal_reviews(source_ep,sp,item,provider):
    """Mirror verified producing records, never infer authority from current results.
    This also repairs adopted-queue projection after renewal archived the old result."""
    source=seal.log(queue_ledger_project(source_ep),'dispatch')
    verified=source.verify()
    if not verified.ok: raise ValueError('Cannot verify terminal evidence: '+'; '.join(verified.problems))
    evidence=[r for r in source.rows() if r.get('op')=='review_required' and r.get('item')==item['id']
        and r.get('account')==item.get('account') and r.get('provider')==provider]
    with ledger.ledger_lock(sp):
        for record in evidence:
            existing=terminal_review(sp,{**item,'approvals':[{'sig':record['approval']}]})
            if not existing: seal.log(sp,'dispatch').append_locked({k:v for k,v in record.items() if k not in ('sig','seq','prev')})
    return evidence


def latest_dispatch_approval(sp,key):
    return next((r.get('approval') for r in reversed(seal.log(sp,'dispatch').rows()) if r.get('op')=='started' and r.get('key')==key),None)


def record_consumption(sp, op: str, item: Mapping, post_id: str, variant_id: str, *, key: str | None = None, outcome: str | None = None,
                       by: str = "tick", now: datetime | None = None) -> None:
    sig = approval_sig(item)
    if not sig:
        return
    row = {"op": op, "approval": sig, "item": item.get("id"), "post": post_id, "variant": variant_id, "by": by,
           "at": iso_utc(now or SystemClock().now())}
    if key:
        row["key"] = key
    if outcome:
        row["outcome"] = outcome
    seal.log(sp, "dispatch").append(row)


# ------------------------------------------------------------------------------------------------ slots (the schedule is not in the approval hash)

def expected_slot(sp, approval: Mapping) -> str | None:
    """The slot the approval covers: the one signed into it, or the latest slot a sanctioned retime (move/swap/unschedule/accept) recorded
    in the sealed control ledger for THIS approval."""
    slot = (approval.get("bind") or {}).get("slot")
    for r in seal.log(sp, "control").rows():
        if r.get("op") == "slot" and r.get("approval") == approval.get("sig"):
            slot = r.get("utc")
    return slot


def record_slot(sp, variant: Mapping, actor: Actor, now: datetime) -> None:
    """Called by every sanctioned write of `variant.schedule`: when the variant's queue item is approved, append the new slot to the sealed
    control ledger (so the sender can tell it from a hand edit) and, for a non-human actor, an event the human can see."""
    item = state.load_queue_item(sp, variant)
    sig = approval_sig(item or {})
    if not item or item.get("status") != "approved" or not sig:
        return
    utc = safety.norm_slot((variant.get("schedule") or {}).get("utc"))
    seal.log(sp, "control").append({"op": "slot", "approval": sig, "variant": variant["id"], "utc": utc, "by": actor.name, "kind": actor.kind,
                                    "at": iso_utc(now)})
    if actor.kind != "human":
        append_event(sp, make_event(actor=actor, op="retime_after_approval", variant=variant["id"], detail={"utc": utc, "queue": item["id"]}, now=now),
                     now=now)


# ------------------------------------------------------------------------------------------------ dedupe

def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def fingerprint(sp, variant: Mapping, item: Mapping) -> tuple:
    shas = item.get("file_sha256") or [item.get("sha256")]
    return (variant["account"], tuple(shas), _norm(item.get("caption", "")))


def duplicate_of(sp, post: Mapping, variant: Mapping, item: Mapping, now: datetime, posts: list[dict] | None = None) -> str | None:
    """`post:variant` of an earlier PUBLISHED variant of the same account with the same media bytes and caption inside `dedupe_days`, else
    None. A variant flagged `allow_duplicate` (a human decision) is never blocked."""
    if "allow_duplicate" in (variant.get("flags") or []):
        return None
    mine = fingerprint(sp, variant, item)
    horizon = timedelta(days=policy.dedupe_days(sp))
    for p in posts if posts is not None else store.list_posts(sp):
        for v in p.get("variants") or []:
            if (p["id"], v["id"]) == (post["id"], variant["id"]) or v.get("account") != variant["account"]:
                continue
            res = v.get("result") or {}
            if res.get("state") != "published":
                continue
            try:
                if now - parse_ts(res["at"]) > horizon:
                    continue
            except (KeyError, ValueError):
                continue
            other = state.load_queue_item(sp, v)
            if other and fingerprint(sp, v, other) == mine:
                return f"{p['id']}:{v['id']}"
    return None


# ------------------------------------------------------------------------------------------------ crash bookkeeping

def dispatch_key(variant_id: str, queue_sha: str, meta_hash: str, slot_utc: str) -> str:
    return hashlib.sha256("|".join([variant_id, queue_sha, meta_hash, slot_utc]).encode()).hexdigest()


def key_for(variant: Mapping, item: Mapping, slot_utc: str) -> str:
    return dispatch_key(variant["id"], item["sha256"], safety.meta_hash(dict(item)), slot_utc)


def open_dispatches(sp) -> dict[str, dict]:
    """key -> the `dispatch_started` event, for every key started more often than it was finished/resolved."""
    rows, _ = ledger.read_jsonl(ledger.events_path(sp))
    net: dict[str, int] = {}
    first: dict[str, dict] = {}
    for r in rows:
        key = (r.get("detail") or {}).get("key")
        if not key:
            continue
        if r.get("op") == "dispatch_started":
            net[key] = net.get(key, 0) + 1
            first[key] = r
        elif r.get("op") in ("dispatch_finished", "dispatch_resolved"):
            net[key] = net.get(key, 0) - 1
    return {k: first[k] for k, n in net.items() if n > 0}


def log_dispatch(sp, op: str, post: str, variant: str, key: str, now: datetime, actor: Actor, **detail) -> None:
    append_event(sp, make_event(actor=actor, op=op, post=post, variant=variant, detail={"key": key, **detail}, now=now), now=now)


def resolve_uncertain(sp, post_id: str, variant_id: str, *, posted_url: str | None = None, retry: bool = False, by: str,
                      actor: Actor | None = None, now: datetime | None = None) -> dict:
    """A HUMAN settles an UNKNOWN publish (the CLI/server enforce the human gate; this is the state change): `posted_url` = it WAS posted
    (recorded), `retry=True` = it was NOT (a fresh `late_after_min` window opens from now and the next tick may send it again, once).
    Wraps queue.resolve and closes every open dispatch key of the variant."""
    if bool(posted_url) == bool(retry):
        raise ValueError("give exactly one of posted_url or retry")
    now = now or SystemClock().now()
    actor = actor or Actor("human", by)
    post = store.load_post(sp, post_id)
    v = model.find_variant(post, variant_id)
    item = state.load_queue_item(sp, v)
    if item is None:
        raise ValueError("variant has no queue item")
    ep = state.queue_episode(sp, v)
    sending = [p for p, r in item.get("results", {}).items() if r.get("state") == "sending"]
    open_keys = [k for k, ev in open_dispatches(sp).items() if ev.get("variant") == variant_id and ev.get("post") == post_id]
    if sending:
        for plat in sending:
            item = pq.resolve(ep, item["id"], plat, posted_url=posted_url, retry=retry, by=by)
    elif posted_url:
        raise ValueError("the queue holds no uncertain platform for this variant; nothing to record as posted (use retry)")
    elif not open_keys:
        raise ValueError("nothing uncertain to resolve for this variant")
    for k in open_keys:
        log_dispatch(sp, "dispatch_resolved", post_id, variant_id, k, now, actor, by=by, posted=bool(posted_url))
    if posted_url:
        record_consumption(sp, "finished", item, post_id, variant_id, outcome="published", by=by, now=now)
    else:
        record_consumption(sp, "released", item, post_id, variant_id, by=by, now=now)
    new = copy.deepcopy(post)
    nv = model.find_variant(new, variant_id)
    if posted_url:
        nv["result"] = copy.deepcopy(item["results"][v["platform"]])
        nv["attempts"] = [*nv["attempts"], {"at": iso_utc(now), "outcome": "resolved_posted", "detail": "human confirmed it was posted"}]
    else:
        window = now + timedelta(minutes=policy.late_after_min(sp))
        nv["result"] = None
        nv["attempts"] = [*nv["attempts"], {"at": iso_utc(now), "outcome": "resolved_retry", "detail": "human confirmed it was NOT posted",
                                            "retry_at": iso_utc(now), "window_until": iso_utc(window)}]
    store.save_post(sp, new, expected_rev=post["rev"], actor=actor, op="resolve", variant=variant_id,
                    detail={"posted": bool(posted_url), "retry": retry}, now=now)
    return {"post": post_id, "variant": variant_id, "queue_status": item["status"], "resolved": "posted" if posted_url else "retry"}


# ------------------------------------------------------------------------------------------------ pause / resume

def _scope(account: str | None) -> str:
    return "studio" if account is None else model.check_id(account, "account id")


def pause_state(sp) -> dict[str, bool]:
    """scope -> paused, from the LAST pause/resume record in the sealed control ledger."""
    st: dict[str, bool] = {}
    for r in seal.log(sp, "control").rows():
        if r.get("op") in ("pause", "resume") and r.get("scope"):
            st[r["scope"]] = r["op"] == "pause"
    return st


def _file_for(sp, scope: str) -> Path:
    return sp.pause_path if scope == "studio" else sp.accounts_dir / f"{scope}.paused"


def is_paused(sp, account: str | None = None) -> bool:
    """Paused when the PAUSE file exists OR the sealed ledger's last word for the scope is `pause`. Deleting the file therefore does NOT resume:
    only `resume` (a human act) writes the matching sealed record."""
    scope = _scope(account)
    return _file_for(sp, scope).exists() or bool(pause_state(sp).get(scope))


def pause_removed(sp) -> list[str]:
    """Scopes the sealed ledger says are paused whose pause file has disappeared without a `resume` (tampering, or a clumsy `rm`)."""
    return sorted(scope for scope, paused in pause_state(sp).items() if paused and not _file_for(sp, scope).exists())


def pause(sp, *, account: str | None = None, by: str = "claude", kind: str = "agent", now: datetime | None = None) -> dict:
    """Stop sending (any actor may: it is the safe direction). `account=None` = the whole studio (`PAUSE`), else `accounts/<id>.paused`."""
    now = now or SystemClock().now()
    scope = _scope(account)
    path = _file_for(sp, scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"by": by, "at": iso_utc(now)}) + "\n")
    seal.log(sp, "control").append({"op": "pause", "scope": scope, "by": by, "kind": kind, "at": iso_utc(now)})
    append_event(sp, make_event(actor=Actor(kind, by), op="pause", detail={"scope": account or "studio"}, now=now), now=now)
    return {"paused": True, "scope": account or "studio"}


def resume(sp, *, account: str | None = None, by: str = "maren", now: datetime | None = None) -> dict:
    """Clear a pause. The caller MUST have established a human (tty via ave.humanpresence.require_human, or the UI PIN): see the hook."""
    now = now or SystemClock().now()
    scope = _scope(account)
    _file_for(sp, scope).unlink(missing_ok=True)
    seal.log(sp, "control").append({"op": "resume", "scope": scope, "by": by, "kind": "human", "at": iso_utc(now)})
    append_event(sp, make_event(actor=Actor("human", by), op="resume", detail={"scope": account or "studio"}, now=now), now=now)
    return {"paused": is_paused(sp), "scope": account or "studio"}
