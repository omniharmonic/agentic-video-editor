"""The scheduler pass (spec 6.1-6.8). `tick` is stateless, idempotent, and DRY-RUN BY DEFAULT.

A variant is sent only when ALL of these hold at the moment of sending (invariants 6.3, each with a test in tests/studio/test_sched_*):

    send=True was passed                         (default is a rehearsal that posts nothing and writes only `would_post` events)
    no `PAUSE` file, no account pause
    its slot has arrived (utc <= now) and is not older than `late_after_min`  (a stale slot is `missed`, never posted)
    the queue item is `approved` with a valid HMAC signature over the CURRENT bytes and the CURRENT bound metadata
    the approval is younger than the TTL (judged on the injected clock)
    the account holds a valid HMAC-attested `live` file, and its adapter independently agrees (`ready_problems`)
    the license closure of its media is covered
    no limit defers it, it is not a duplicate of a recent post, and no earlier dispatch of it is unresolved

Safety design:
  * Two ticks cannot overlap: a non-blocking flock on `studio/.tick.lock` (the second returns `locked`), and underneath it the queue's own
    per-item lock, and underneath that the `dispatch_started` key.
  * A `dispatch_started` event is written BEFORE `queue.run(send=True)` (which itself writes `sending` before the network call). A crash
    anywhere after leaves the variant UNKNOWN: no later tick resends it; only `dispatch.resolve_uncertain` (a human) settles it.
  * Only a `PublishError` (the platform definitely did not post) is ever retried, with the backoff of schedule/retry.py, and never past
    the slot's late window.
  * Sending refuses when an agent marker is set in the environment (`ave.humanpresence.agent_markers_set`): an agent may rehearse, never send.
    It also needs a human terminal OR the human's signed `send.enabled` (studio/sendgate.py): that is how a launchd job with no tty sends.
  * Tamper evidence: the hash-chained ledgers (feedback, dispatch, control) are verified at the start of every pass. A removed, edited or truncated
    line, or a pause file deleted without `resume`, raises a `tamper` alert and the pass SENDS NOTHING until a human looks (`ave studio doctor`).
  * Every approval is single-use: `dispatch_started` goes into the SEALED dispatch ledger keyed by the approval's signature before the send, and
    `finished` after it, so a queue file reset by hand cannot make the same approval send twice.
"""

from __future__ import annotations

import fcntl
import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping

from .. import humanpresence
from ..episode import write_json
from ..publish import queue as pq, safety
from ..publish.adapters.base import load_platforms
from ..studio import dispatch, ledger, model, policy, seal, sendgate, state, store
from ..studio.clock import Clock, iso_utc, parse_ts
from ..studio.events import SCHEDULER, append_event, make_event
from . import limits, retry, slots


class TickRefused(RuntimeError):
    """`tick(send=True)` was asked to run where an agent must not send."""


@dataclass
class TickReport:
    mode: str
    now: str
    paused: bool = False
    locked: bool = False
    would_post: list[dict] = field(default_factory=list)
    posted: list[dict] = field(default_factory=list)
    blocked: list[dict] = field(default_factory=list)
    deferred: list[dict] = field(default_factory=list)
    missed: list[dict] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)
    uncertain: list[dict] = field(default_factory=list)
    retry_scheduled: list[dict] = field(default_factory=list)
    tamper: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"mode": self.mode, "paused": self.paused, "locked": self.locked, "now": self.now, "would_post": self.would_post,
                "posted": self.posted, "blocked": self.blocked, "deferred": self.deferred, "missed": self.missed, "failed": self.failed,
                "uncertain": self.uncertain, "retry_scheduled": self.retry_scheduled, "tamper": self.tamper}


def lock_path(sp):
    return sp.root / ".tick.lock"


@contextmanager
def tick_lock(sp):
    """Non-blocking exclusive lock; yields True when held, False when another tick holds it."""
    f = open(lock_path(sp), "a")
    got = False
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            got = True
        except BlockingIOError:
            pass
        yield got
    finally:
        f.close()


def heartbeat_path(sp):
    return sp.schedule_dir / "heartbeat.json"


def write_heartbeat(sp, now: datetime, mode: str) -> None:
    import os
    write_json(heartbeat_path(sp), {"at": iso_utc(now), "mode": mode, "pid": os.getpid()})


def _alert_once(sp, now: datetime, *, kind: str, severity: str, message: str, account: str | None, post: str | None, marker: str) -> None:
    for a in ledger.list_alerts(sp, include_acked=True):
        if a["kind"] == kind and a.get("post") == post and f"[{marker}]" in a["message"]:
            return
    ledger.append_alert(sp, severity=severity, kind=kind, message=f"{message} [{marker}]", account=account, post=post, now=now)


_BLOCK_ALERT = {"not_live": ("account_not_live", "warn"), "approval_expired": ("approval_expired", "warn"), "license_gap": ("license_gap", "error"),
                "host": ("host_failed", "error"), "credentials": ("credentials_missing", "error"), "duplicate": ("duplicate", "warn"),
                # a send-time gate that only a hand edit (or a bug) can trip: the human must look
                "schedule_changed": ("tamper", "error"), "ttl_policy": ("tamper", "error"), "approval_binding": ("tamper", "error"),
                "queue_mismatch": ("tamper", "error"), "approval_consumed": ("tamper", "error"), "not_studio_item": ("tamper", "error")}


def _record(sp, post_id: str, variant_id: str, mutate, now: datetime, op: str, detail: dict) -> None:
    """Apply `mutate(variant)` and save under compare-and-swap, re-reading on a lost race (the human may be editing the post)."""
    for _ in range(6):
        post = store.load_post(sp, post_id)
        mutate(model.find_variant(post, variant_id))
        try:
            store.save_post(sp, post, expected_rev=post["rev"], actor=SCHEDULER, op=op, detail=detail, variant=variant_id, now=now)
            return
        except model.RevConflict:
            continue
    raise model.RevConflict(store.load_post(sp, post_id), f"post {post_id} kept changing while recording the send result")


def tick(sp, *, clock: Clock, send: bool = False, publishers: Mapping[str, Any] | None = None, host: Any = None,
         platforms_cfg: dict | None = None) -> TickReport:
    """One scheduler pass at `clock.now()`. See the module docstring for the gates. `publishers` maps ACCOUNT ID (preferred) or platform to
    an account-bound adapter; sending needs one, dry-run does not. Returns a TickReport (`as_dict()` for JSON)."""
    now = clock.now()
    rep = TickReport(mode="send" if send else "dry-run", now=iso_utc(now))
    if send:
        auth = sendgate.authorized(sp)
        if not auth.ok:
            raise TickRefused(auth.reason + ("" if humanpresence.agent_markers_set() else
                                             ". Run it in your own terminal, or allow an unattended sender once: ave studio send enable"))
    with tick_lock(sp) as got:
        if not got:
            rep.locked = True
            return rep
        tamper = _tamper(sp, now)
        if send and not tamper:
            from ..studio.manual_publication import recover_locked
            recover_locked(sp)
        rep.tamper = tamper
        if dispatch.is_paused(sp):
            rep.paused = True
        elif tamper:
            pass                                    # a ledger or a pause was tampered with: nothing is sent until a human looks (alert raised)
        else:
            if send and host is not None:
                _sweep_host(sp, host, now)
            _pass(sp, rep, now, clock, send, publishers or {}, host, platforms_cfg)
        if send:
            write_heartbeat(sp, now, rep.mode)
    return rep


def _tamper(sp, now: datetime) -> list[str]:
    """Problems that make the studio's own records untrustworthy (see studio/seal.py), each raised ONCE as a `tamper` alert."""
    problems = list(seal.verify_all(sp))
    problems += [f"the {'studio' if scope == 'studio' else 'account ' + scope} pause file was removed without `ave studio resume` (it stays paused)"
                 for scope in dispatch.pause_removed(sp)]
    for msg in problems:
        _alert_once(sp, now, kind="tamper", severity="error", account=None, post=None, marker=seal_marker(msg),
                    message=f"{msg}. Nothing is sent until a human checks it (`ave studio doctor`)")
    return problems


def seal_marker(msg: str) -> str:
    import hashlib
    return "tamper " + hashlib.sha256(msg.encode()).hexdigest()[:12]


def _sweep_host(sp, host, now: datetime) -> None:
    """Delete media-host objects a crashed or killed send left behind (older than the presign lifetime). Best effort: a sweep failure never stops a
    pass, and an alert says so once."""
    sweep = getattr(host, "sweep", None)
    if sweep is None:
        return
    stamp = sp.schedule_dir / "host-sweep.json"
    try:
        last = parse_ts(json.loads(stamp.read_text())["at"])
        if now - last < timedelta(minutes=15):
            return
    except (OSError, ValueError, KeyError):
        pass
    try:
        sweep()
        write_json(stamp, {"at": iso_utc(now)})
    except Exception as e:                                       # noqa: BLE001 - a bucket problem must not block unrelated posts
        _alert_once(sp, now, kind="host_failed", severity="warn", account=None, post=None, marker=f"sweep {iso_utc(now)[:13]}",
                    message=f"the media host sweep failed: {safety.redact(f'{type(e).__name__}: {e}')}")


def _pass(sp, rep: TickReport, now: datetime, clock: Clock, send: bool, publishers: Mapping[str, Any], host: Any, platforms_cfg: dict | None) -> None:
    posts = store.list_posts(sp)
    fb = ledger.open_feedback(sp, now=now)
    open_keys = dispatch.open_dispatches(sp)
    late = timedelta(minutes=policy.late_after_min(sp))
    due: list[tuple[datetime, str, str, dict, dict, dict, dict, dict]] = []
    for p in posts:
        if p.get("stage") == "dropped":
            continue
        mine = [f for f in fb if f.get("post") == p["id"]]
        for v in p.get("variants") or []:
            if v.get("dropped"):
                continue
            item = state.load_queue_item(sp, v)
            ex = state.explain(v, item, mine, now, sp)
            st, utc = ex["status"], slots.schedule_utc(v)
            row = {"post": p["id"], "variant": v["id"], "account": v["account"]}
            if st in ("published", "dropped"):
                continue
            if st == "publishing":                               # in flight or UNKNOWN: never touched by the scheduler
                rep.uncertain.append({**row, "queue": (item or {}).get("id")})
                if send:
                    _alert_once(sp, now, kind="uncertain", severity="error", account=v["account"], post=p["id"], marker=f"queue {(item or {}).get('id')}",
                                message=f"{v['account']}: a publish may or may not have gone out; check the platform, then resolve it "
                                        f"(ave studio resolve {p['id']} {v['id']} --posted URL | --retry)")
                continue
            if st == "failed":
                last = (v.get("attempts") or [{}])[-1]
                if not last.get("retry_at"):
                    continue
                at = parse_ts(last["retry_at"])
                if now > parse_ts(last.get("window_until") or last["retry_at"]):
                    _give_up(sp, rep, p, v, now, send)
                    continue
                if at > now:
                    continue
                due.append((at, p["id"], v["id"], p, v, item, ex, row))
                continue
            if utc is None or utc > now:
                continue
            if st == "missed":
                rep.missed.append({**row, "slot": iso_utc(utc)})
                if send:
                    _alert_once(sp, now, kind="missed", severity="warn", account=v["account"], post=p["id"], marker=f"slot {iso_utc(utc)}",
                                message=f"{v['account']}: slot {iso_utc(utc)} passed more than {int(late.total_seconds() // 60)} min ago and was NOT posted "
                                        f"(never posts stale); retime it to post it")
                continue
            if st != "scheduled":
                code = "approval_expired" if "approval_expired" in ex["flags"] else st
                rep.blocked.append({**row, "reasons": [ex["reason"]], "codes": [code]})
                if send and code == "approval_expired":
                    _alert_once(sp, now, kind="approval_expired", severity="warn", account=v["account"], post=p["id"], marker=f"slot {iso_utc(utc)}",
                                message=f"{v['account']}: approval has expired; approve again to post")
                continue
            due.append((utc, p["id"], v["id"], p, v, item, ex, row))
    pcfg = platforms_cfg
    if pcfg is None:
        try:
            pcfg = load_platforms()
        except (OSError, ValueError):
            pcfg = {}
    for at, _, _, p, v, item, ex, row in sorted(due, key=lambda t: t[:3]):
        _consider(sp, rep, now, send, publishers, host, pcfg, posts, open_keys, p, v, item, row)


def _flag_schedule_change(sp, p: dict, v: dict, item: dict, now: datetime, why: str) -> None:
    """The slot differs from the one the approval covers and no sanctioned retime was recorded: an event the human sees and a variant flag, once."""
    marker = "schedule " + iso_utc(now)[:10] + " " + str((v.get("schedule") or {}).get("utc"))
    for e in ledger.read_jsonl(ledger.events_path(sp))[0]:
        if e.get("op") == "schedule_changed_after_approval" and e.get("variant") == v["id"] and (e.get("detail") or {}).get("marker") == marker:
            return
    append_event(sp, make_event(actor=SCHEDULER, op="schedule_changed_after_approval", post=p["id"], variant=v["id"],
                                detail={"queue": item["id"], "marker": marker, "why": why}, now=now), now=now)

    def mutate(x):
        if "schedule_changed_after_approval" not in (x.get("flags") or []):
            x["flags"] = [*(x.get("flags") or []), "schedule_changed_after_approval"]
    try:
        _record(sp, p["id"], v["id"], mutate, now, "flag", {"flag": "schedule_changed_after_approval"})
    except model.RevConflict:
        pass


def _give_up(sp, rep: TickReport, p: dict, v: dict, now: datetime, send: bool) -> None:
    row = {"post": p["id"], "variant": v["id"], "account": v["account"]}
    rep.failed.append({**row, "error": "retry window passed"})
    if not send:
        return
    last = v["attempts"][-1]

    def mutate(x):
        x["result"] = {"state": "failed", "at": iso_utc(now), "error": "retry window passed", "kind": last.get("kind")}
        x["attempts"] = [*x["attempts"], {"at": iso_utc(now), "outcome": "gave_up", "detail": "retry window passed; the slot is missed"}]
    _record(sp, p["id"], v["id"], mutate, now, "retry_give_up", {})
    _alert_once(sp, now, kind="missed", severity="error", account=v["account"], post=p["id"], marker=f"retry {last['at']}",
                message=f"{v['account']}: the retry window passed after a failed attempt; the post was NOT sent")


def _consider(sp, rep, now, send, publishers, host, pcfg, posts, open_keys, p, v, item, row) -> None:
    acct = policy.account_config(sp, v["account"])
    slot = (v.get("schedule") or {}).get("utc") or iso_utc(now)
    if acct.get("platform") == "manual" or v["platform"] == "manual":
        rep.blocked.append({**row, "reasons": ["manual platform: post it by hand, then record it with ave studio resolve"], "codes": ["manual"]})
        if send:
            _alert_once(sp, now, kind="manual_post_due", severity="info", account=v["account"], post=p["id"], marker=f"slot {slot}",
                        message=f"{v['account']}: a manual post is due; post the files by hand")
        return
    key = dispatch.key_for(v, item, slot)
    if key in open_keys:
        rep.uncertain.append({**row, "queue": item["id"], "reason": "an earlier dispatch of this slot was never closed"})
        if send:
            _alert_once(sp, now, kind="uncertain", severity="error", account=v["account"], post=p["id"], marker=f"queue {item['id']}",
                        message=f"{v['account']}: an earlier send of this post did not finish; verify on the platform, then resolve it")
        return
    bl = dispatch.blockers(sp, p, v, now=now, publishers=publishers, host=host, send=send)
    if bl:
        rep.blocked.append({**row, "reasons": [b.message for b in bl], "codes": [b.code for b in bl]})
        if send:
            for b in bl:
                if b.code in _BLOCK_ALERT:
                    kind, sev = _BLOCK_ALERT[b.code]
                    _alert_once(sp, now, kind=kind, severity=sev, account=v["account"], post=p["id"], marker=f"slot {slot}", message=b.message)
                if b.code == "schedule_changed":
                    _flag_schedule_change(sp, p, v, item, now, b.message)
        return
    tz = slots.account_tz(sp, v["account"])
    qend = slots.quiet_hours_end(acct, now, tz)
    if qend is not None:
        rep.deferred.append({**row, "reason": "quiet hours", "until": iso_utc(qend)})
        return
    lim = limits.check(sp, v["account"], v["platform"], now, pcfg)
    if lim:
        reason, until = max(lim, key=lambda t: t[1])
        rep.deferred.append({**row, "reason": reason, "until": iso_utc(until)})
        if send and "reached" in reason and v["platform"] in reason:
            _alert_once(sp, now, kind="quota", severity="warn", account=v["account"], post=p["id"], marker=f"slot {slot}", message=f"{v['account']}: {reason}")
        return
    dup = dispatch.duplicate_of(sp, p, v, item, now, posts)
    if dup:
        rep.blocked.append({**row, "reasons": [f"duplicate of {dup} published within {policy.dedupe_days(sp)} days"], "codes": ["duplicate"]})
        if send:
            _alert_once(sp, now, kind="duplicate", severity="warn", account=v["account"], post=p["id"], marker=f"slot {slot}",
                        message=f"{v['account']}: same media and caption as {dup} published recently; set the allow_duplicate flag to post it anyway")
        return
    ep = state.queue_episode(sp, v)
    pubs = dispatch.queue_publishers(publishers, v)
    if not send:
        res = pq.run(ep, item["id"], send=False, publishers=pubs or None)
        if res["ok"]:
            rep.would_post.append({**row, "slot": slot, "queue": item["id"]})
            append_event(sp, make_event(actor=SCHEDULER, op="would_post", post=p["id"], variant=v["id"], detail={"slot": slot, "queue": item["id"]}, now=now), now=now)
        else:
            rep.blocked.append({**row, "reasons": list(res["blocked"]), "codes": ["queue"]})
        return
    _send(sp, rep, now, pcfg, p, v, item, row, key, slot, ep, pubs)


def _send(sp, rep, now, pcfg, p, v, item, row, key, slot, ep, pubs) -> None:
    dispatch.log_dispatch(sp, "dispatch_started", p["id"], v["id"], key, now, SCHEDULER, queue=item["id"], slot=slot)
    dispatch.record_consumption(sp, "started", item, p["id"], v["id"], key=key, now=now)
    try:
        qr = pq.run(ep, item["id"], send=True, publishers=pubs)
    except RuntimeError as e:
        if "already" in str(e) and "working on" in str(e):          # another process holds this item: it is theirs; nothing was sent by us
            dispatch.log_dispatch(sp, "dispatch_finished", p["id"], v["id"], key, now, SCHEDULER, outcome="busy")
            dispatch.record_consumption(sp, "finished", item, p["id"], v["id"], key=key, outcome="busy", now=now)
            rep.deferred.append({**row, "reason": "queue item is locked by another process", "until": None})
            return
        _uncertain(sp, rep, now, p, v, item, row, f"RuntimeError: {e}")
        return
    except BaseException as e:                                    # a crash/interrupt after the key was written: UNKNOWN, never retried
        try:
            _uncertain(sp, rep, now, p, v, item, row, f"{type(e).__name__}: {e}")
        finally:
            raise
    po = (qr.get("platforms") or {}).get(v["platform"], {})
    outcome = po.get("outcome")
    if outcome in ("published", "recovered_published", "already_sent"):    # already_sent: this computer's own record says it went out
        def ok(x):
            x["result"] = {**po, "state": "published", "at": iso_utc(now), "platform": v["platform"], "account": v["account"]}
            x["attempts"] = [*x["attempts"], {"at": iso_utc(now), "outcome": "published", "detail": po.get("url") or ""}]
        _record(sp, p["id"], v["id"], ok, now, "published", {"queue": item["id"], "url": po.get("url"), "receipt": po.get("receipt")})
        limits.record_success(sp, v["account"], now)
        dispatch.log_dispatch(sp, "dispatch_finished", p["id"], v["id"], key, now, SCHEDULER, outcome="published")
        dispatch.record_consumption(sp, "finished", item, p["id"], v["id"], key=key, outcome="published", now=now)
        rep.posted.append({**row, "slot": slot, "url": po.get("url"), "id": po.get("id")})
    elif outcome in ('processing','scheduled','awaiting_user_publish','sandbox','draft'):
        def accepted(x):
            x['result']={**po,'state':outcome,'at':iso_utc(now)}
            x['attempts']=[*x['attempts'],{'at':iso_utc(now),'outcome':outcome,'detail':'Provider accepted operation; not an ordinary published post'}]
        _record(sp,p['id'],v['id'],accepted,now,'delivery_accepted',{'queue':item['id'],'phase':outcome})
        dispatch.log_dispatch(sp,'dispatch_finished',p['id'],v['id'],key,now,SCHEDULER,outcome=outcome)
        dispatch.record_consumption(sp,'finished',item,p['id'],v['id'],key=key,outcome=outcome,now=now)
        rep.deferred.append({**row,'reason':outcome,'until':None})
    elif outcome == "failed":
        dispatch.log_dispatch(sp, "dispatch_finished", p["id"], v["id"], key, now, SCHEDULER, outcome="failed")
        dispatch.record_consumption(sp, "finished", item, p["id"], v["id"], key=key, outcome="failed", now=now)
        _failed(sp, rep, now, pcfg, p, v, row, slot, str(po.get("error") or "publish failed"), po.get("diagnostics"))
    elif outcome == "uncertain":
        _uncertain(sp, rep, now, p, v, item, row, str(po.get("error") or po.get("detail") or "uncertain"))
    else:                                                          # blocked inside queue.run (bytes/approval changed, adapter not ready)
        reasons = list(qr.get("blocked") or []) + ([po["detail"]] if po.get("detail") else [])
        dispatch.log_dispatch(sp, "dispatch_finished", p["id"], v["id"], key, now, SCHEDULER, outcome="blocked")
        dispatch.record_consumption(sp, "finished", item, p["id"], v["id"], key=key, outcome="blocked", now=now)
        rep.blocked.append({**row, "reasons": reasons or ["blocked by the queue"], "codes": ["queue"]})


def _uncertain(sp, rep, now, p, v, item, row, detail: str) -> None:
    detail = safety.redact(detail)

    def mutate(x):
        x["result"] = {"state": "uncertain", "at": iso_utc(now), "detail": detail}
        x["attempts"] = [*x["attempts"], {"at": iso_utc(now), "outcome": "uncertain", "detail": detail}]
    _record(sp, p["id"], v["id"], mutate, now, "uncertain", {"queue": item["id"]})
    dispatch.record_consumption(sp, "finished", item, p["id"], v["id"], outcome="uncertain", now=now)
    rep.uncertain.append({**row, "queue": item["id"], "error": detail})
    _alert_once(sp, now, kind="uncertain", severity="error", account=v["account"], post=p["id"], marker=f"queue {item['id']}",
                message=f"{v['account']}: the send ended uncertain ({detail}); it will NOT be retried. Check the platform, then "
                        f"ave studio resolve {p['id']} {v['id']} --posted URL | --retry")


def _failed(sp, rep, now, pcfg, p, v, row, slot, error: str, diagnostics=None) -> None:
    error = safety.redact(error)
    prior = 0
    for a in reversed(v.get("attempts") or []):
        if a.get("outcome") == "resolved_retry":
            break
        if a.get("outcome") == "failed" and a.get("kind") == "network":
            prior += 1
    plan = retry.plan(error, now=now, slot_utc=parse_ts(slot), late_after_min=policy.late_after_min(sp), prior_network_failures=prior, diagnostics=diagnostics)

    def mutate(x):
        x["attempts"] = [*x["attempts"], {"at": iso_utc(now), "outcome": "failed", "kind": plan.kind, "detail": error,
                                          "retry_at": iso_utc(plan.retry_at) if plan.retry_at else None, "window_until": iso_utc(plan.window_until)}]
        x["result"] = None if plan.retry_at else {"state": "failed", "at": iso_utc(now), "error": error, "kind": plan.kind}
    _record(sp, p["id"], v["id"], mutate, now, "send_failed", {"kind": plan.kind, "retry_at": iso_utc(plan.retry_at) if plan.retry_at else None})
    if plan.kind == "quota":
        limits.defer(sp, v["account"], plan.retry_at or now + timedelta(minutes=retry.QUOTA_DEFAULT_MIN), error[:120])
    marker = f"slot {slot} at {iso_utc(now)}"
    if plan.retry_at:
        rep.retry_scheduled.append({**row, "kind": plan.kind, "retry_at": iso_utc(plan.retry_at), "error": error})
        if plan.kind == "quota":
            _alert_once(sp, now, kind="quota", severity="warn", account=v["account"], post=p["id"], marker=marker,
                        message=f"{v['account']}: quota/rate limit hit ({error}); retrying at {iso_utc(plan.retry_at)}")
        return
    rep.failed.append({**row, "kind": plan.kind, "error": error})
    kind = "auth_expired" if plan.kind == "auth" else "quota" if plan.kind == "quota" else "rejected"
    _alert_once(sp, now, kind=kind, severity="error", account=v["account"], post=p["id"], marker=marker,
                message=f"{v['account']}: publish failed ({plan.kind}): {error}. {plan.reason}")
