"""The `Studio` facade: the thin layer the CLI (WS7) and the server (WS4) both call (spec 3.1, 11 WS0).

WS0 implements everything that only needs the files in `studio/` (posts, feedback, alerts, changes, accounts, summary, status). The
rest is a HOOK: another workstream registers its function under a fixed name, and until then the call raises `NotImplementedYet`
(the API maps it to HTTP 501):

    studio.register("calendar", fn)    # WS3  fn(studio, start, end, accounts=None) -> dict (shape: tests/data_studio/api/GET_calendar.json)
    studio.calendar(start, end)        # -> fn(studio, start, end)  i.e. hooks receive the Studio first

`FixtureStudio` answers every public method with the committed JSON in tests/data_studio/api/ (the single source of API shapes), so
WS4/WS5/WS6 can build against it before WS1/WS3 exist (`ave studio ui --fixtures`).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from .. import resources
from . import dispatch, ledger, state, store
from .clock import Clock, SystemClock, iso_utc
from .events import AGENT, Actor, append_event, changes as _changes, make_event
from .model import VARIANT_STATUSES, ai_generated, apply_patch, find_variant, media_asset_ids, new_post, new_variant
from .project import StudioProject

HOOKS = (
    "calendar", "grid", "preview", "library", "library_asset", "broll", "move", "swap", "unschedule", "swap_asset",
    "sync_queue", "tick", "approve", "reject", "record_view", "pause", "resume", "accept_proposal", "broll_search", "broll_pick",
    "upload", "rehearse", "account_live",
)

# Hooks `open_studio` leaves unregistered on purpose: the server (ui/api.py StudioApp) implements these three itself because they sit behind
# the per-run PIN and the view-event rule, and the CLI approves through `queue.approve` after a typed sha. Every other hook must be
# registered on a real Studio (tests/studio/test_open_studio.py enforces this list).
INTENTIONALLY_UNREGISTERED = ("approve", "reject", "record_view")


class NotImplementedYet(NotImplementedError):
    """A facade hook no workstream has registered yet."""


class Studio:
    def __init__(self, sp: StudioProject, *, clock: Clock | None = None, publishers: Mapping[str, Any] | None = None,
                 host: Any = None, providers: Mapping[str, Any] | None = None):
        self.sp = sp
        self.clock: Clock = clock or SystemClock()
        self.publishers = dict(publishers or {})
        self.host = host
        self.providers = dict(providers or {})
        self._hooks: dict[str, Callable[..., Any]] = {}

    # ---- hooks (how other workstreams plug in)
    def register(self, name: str, fn: Callable[..., Any]) -> None:
        if name not in HOOKS:
            raise ValueError(f"unknown hook {name!r}; known: {', '.join(HOOKS)}")
        self._hooks[name] = fn

    def has(self, name: str) -> bool:
        return name in self._hooks

    def call(self, name: str, *args, **kw):
        fn = self._hooks.get(name)
        if fn is None:
            raise NotImplementedYet(f"studio.{name} is not implemented yet (no workstream has registered it)")
        return fn(self, *args, **kw)

    def __getattr__(self, name: str):
        if name in HOOKS:
            return lambda *a, **kw: self.call(name, *a, **kw)
        raise AttributeError(name)

    def now(self) -> datetime:
        return self.clock.now()

    # ---- status
    def variant_status(self, post: dict, variant: dict, *, feedback_open: list[dict] | None = None) -> dict:
        """{"status", "flags", "reason"} for one variant, derived now (state.explain)."""
        fb = feedback_open if feedback_open is not None else ledger.open_feedback(self.sp, post=post["id"], now=self.now())
        return state.explain(variant, state.load_queue_item(self.sp, variant), fb, self.now(), self.sp)

    def view(self, post: dict, *, feedback_open: list[dict] | None = None) -> dict:
        """The post as the API shows it: the stored post plus, per variant, `status`/`status_reasons`/`flags` (derived), the live queue
        summary, and the derived `disclosure.ai_generated`."""
        fb = feedback_open if feedback_open is not None else ledger.open_feedback(self.sp, post=post["id"], now=self.now())
        out = json.loads(json.dumps(post))
        for v in out["variants"]:
            item = state.load_queue_item(self.sp, v)
            ex = state.explain(v, item, fb, self.now(), self.sp)
            v["status"], v["status_reason"] = ex["status"], ex["reason"]
            v["flags"] = sorted(set(v.get("flags") or []) | set(ex["flags"]))
            v["queue_status"] = item["status"] if item else None
            # Queue persistence precedes post projection at a sending crash.
            # Expose only local identity/state, never raw provider receipts or URLs.
            from ..publish import receipts
            from .connections import digest
            result=(item or {}).get('results',{}).get(v['platform'],{})
            v['delivery_operation']=({'queue':item['id'],'digest':digest(item),'state':result.get('state'),
                'recoverable':(v.get('delivery') or {}).get('method')!='manual' and result.get('state') in receipts.LOCKED-{'published'}} if item else None)
            v["approval"] = ({"by": item["approvals"][-1]["by"], "at": item["approvals"][-1]["at"]}
                             if item and item.get("approvals") and item["status"] == "approved" else None)
        out["disclosure"] = {**(post.get("disclosure") or {}),
                             "ai_generated": ai_generated(store.read_assets(self.sp), media_asset_ids(post))}
        return out

    def posts(self, *, stage=None, campaign=None, account=None, platform=None, status=None, q=None) -> list[dict]:
        """Post views, filtered. `status` keeps posts having at least one variant in that derived status."""
        if status and status not in VARIANT_STATUSES:
            raise ValueError(f"status must be one of {', '.join(VARIANT_STATUSES)}")
        fb = ledger.open_feedback(self.sp, now=self.now())
        views = [self.view(p, feedback_open=[f for f in fb if f.get("post") == p["id"]])
                 for p in store.list_posts(self.sp, stage=stage, campaign=campaign, account=account, platform=platform, q=q)]
        if status:
            views = [v for v in views if any(x["status"] == status for x in v["variants"])]
        return views

    def post(self, post_id: str) -> dict:
        return self.view(store.load_post(self.sp, post_id))

    # ---- writes (compare-and-swap on rev; every write is an event)
    def create_post(self, *, idea: str | None = None, copy: Mapping | None = None, media=(), campaign: str | None = None,
                    actor: Actor = AGENT, origin: Mapping | None = None, reason: str = "", confidence: float | None = None) -> dict:
        post = new_post(idea=idea, copy=copy, media=media, campaign=campaign, by=actor.name, now=self.now(), origin=origin,
                        reason=reason, confidence=confidence)
        saved = store.save_post(self.sp, post, expected_rev=None, actor=actor, op="create", now=self.now())
        return self.view(saved)

    def add_variant(self, post_id: str, *, account: str, platform: str, post_type: str, expected_rev: int, actor: Actor = AGENT,
                    copy: Mapping | None = None, media=None) -> dict:
        post = store.load_post(self.sp, post_id)
        if any(v["account"] == account and v["post_type"] == post_type and not v.get("dropped") for v in post["variants"]):
            raise ValueError(f"post {post_id} already has a {post_type} variant for {account}")
        v = new_variant(account=account, platform=platform, post_type=post_type, copy=copy, media=media)
        post["variants"].append(v)
        saved = store.save_post(self.sp, post, expected_rev=expected_rev, actor=actor, op="variant_add",
                                detail={"account": account, "post_type": post_type}, variant=v["id"], now=self.now())
        return self.view(saved)

    def edit_post(self, post_id: str, patch: Mapping[str, Any], *, expected_rev: int, actor: Actor = AGENT,
                  source: str = "cli") -> dict:
        """Apply a dotted-path patch (model.apply_patch) under compare-and-swap. A HUMAN edit of copy also appends a born-resolved
        `human_edit` feedback line per changed field (before/after), so taste can be learned from it (spec 2.7)."""
        post = store.load_post(self.sp, post_id)
        from . import composer
        touched = [v for v in post["variants"] if any(not p.startswith("variants.") or p.startswith("variants."+v["id"]+".") for p in patch)]
        for v in touched: composer.editable(self,post,v)
        edited = apply_patch(post, patch)
        for v in edited["variants"]:
            if any(p.startswith("variants."+v["id"]+".delivery") or p.startswith("variants."+v["id"]+".presentation") for p in patch): v["payload_version"]=3
            if v.get("payload_version")==3: composer.require_projection(edited,v,store.read_assets(self.sp))
        saved = store.save_post(self.sp, edited, expected_rev=expected_rev, actor=actor, op="edit",
                                detail={"paths": sorted(patch)}, now=self.now())
        if actor.kind == "human":
            for path in sorted(patch):
                before, after = _get(post, path), _get(saved, path)
                if before != after and _is_copy_path(path):
                    vid = path.split(".")[1] if path.startswith("variants.") else None
                    ledger.append_feedback(self.sp, {"post": post_id, "variant": vid, "rev": saved["rev"], "kind": "human_edit",
                                                     "structured": {"copy": {"field": path, "before": before, "after": after}},
                                                     "by": actor.name, "source": source}, now=self.now())
        return self.view(saved)

    def production(self, **filters):
        from .production import inventory
        return inventory(self, **filters)

    def update_production(self, post_id, fields, *, expected_rev, actor=AGENT):
        from .production import update
        return update(self, post_id, fields, expected_rev=expected_rev, actor=actor)

    def production_task(self, post_id, op, task, *, expected_rev, actor=AGENT):
        from .production import task as update_task
        return update_task(self, post_id, op, task, expected_rev=expected_rev, actor=actor)

    def production_comments(self, post_id):
        from .production import comments
        return comments(self, post_id)

    # ---- planner helpers (UI): next free slots for the picker, and bulk retime. Neither approves or sends anything.
    def free_slots(self, post_id: str, variant_id: str, *, count: int = 6) -> dict:
        from . import bulk
        return bulk.free_slots(self, post_id, variant_id, count=count)

    def bulk(self, op: str, items: list, *, actor: Actor = AGENT, days: int | None = None, account: str | None = None) -> dict:
        from . import bulk
        return bulk.run(self, op, items, actor=actor, days=days, account=account)

    # ---- feedback and alerts
    def add_feedback(self, post_id: str, kind: str, *, note: str = "", variant: str | None = None, structured: dict | None = None,
                     asset: str | None = None, by: str = "maren", source: str = "cli") -> dict:
        """File a human -> agent note. A send-back that asks for a CHANGE (rework_copy, rework_visual, retime) also VOIDS the variant's approval in the
        same step (queue status -> `awaiting_approval`, event `approval_invalidated`): the human's earlier approval was of the thing they have now
        rejected, and only a fresh human approval re-enables sending. The note records a hash of the part of the post it is about, so
        `resolve_feedback` can later check that something actually changed."""
        post = store.load_post(self.sp, post_id)
        if variant:
            find_variant(post, variant)
        line = ledger.append_feedback(self.sp, {"post": post_id, "variant": variant, "rev": post["rev"], "asset": asset, "kind": kind,
                                                "note": note, "structured": structured or {}, "by": by, "source": source,
                                                "state": note_state(post, variant, kind)},
                                      now=self.now())
        append_event(self.sp, make_event(actor=Actor("human", by), op="feedback", post=post_id, variant=variant, rev=post["rev"],
                                         detail={"fb": line["id"], "kind": kind}, now=self.now()))
        if kind in ("rework_copy", "rework_visual", "retime"):
            self._void_approvals(post, variant, f"{kind} send-back {line['id']}")
        return line

    def _void_approvals(self, post: dict, variant: str | None, why: str) -> list[str]:
        """Demote every approved queue item of the variant (or of the whole post when `variant` is None) to `awaiting_approval`."""
        from ..publish import queue as pq
        voided = []
        for v in post.get("variants") or []:
            if v.get("dropped") or (variant and v["id"] != variant):
                continue
            item = state.load_queue_item(self.sp, v)
            if item is None or item.get("status") != "approved":
                continue
            ep = state.queue_episode(self.sp, v)
            with pq._lock(ep, item["id"]):
                cur = pq.load(ep, item["id"])
                if cur["status"] != "approved":
                    continue
                cur["status"] = "awaiting_approval"
                pq._event(cur, "approval_invalidated", reasons=[why])
                pq.save(ep, cur)
            append_event(self.sp, make_event(actor=Actor("human", "send-back"), op="approval_invalidated", post=post["id"], variant=v["id"],
                                             detail={"queue": item["id"], "why": why}, now=self.now()), now=self.now())
            voided.append(v["id"])
        return voided

    def feedback(self, status: str = "open") -> list[dict]:
        """`open` (not resolved; claimed ones carry `claim`), `claimed`, `resolved` or `all`."""
        rows = ledger.all_feedback(self.sp, now=self.now())
        pick = {"open": lambda f: not f["resolved"], "claimed": lambda f: not f["resolved"] and f["claim"],
                "resolved": lambda f: bool(f["resolved"]), "all": lambda f: True}
        if status not in pick:
            raise ValueError("status must be open, claimed, resolved or all")
        return [f for f in rows if pick[status](f)]

    def claim_next_feedback(self, agent: str) -> dict | None:
        """The oldest open, unclaimed item with its full post context, or None."""
        fb = ledger.claim_feedback(self.sp, agent, now=self.now())
        if fb is None:
            return None
        try:
            fb = {**fb, "post_view": self.post(fb["post"])}
        except FileNotFoundError:
            pass
        return fb

    def resolve_feedback(self, fb: str, by: str, *, rev: int | None = None, note: str = "", human: bool = False) -> dict:
        """Close a feedback item. The human's send-backs that block sending (rework_copy, rework_visual, retime, drop) are only closed by
        EVIDENCE: the post's rev advanced since the note AND the part of the post the note is about actually changed (copy, media or slot).
        `human=True` (a person at a terminal) may close any note: it is their own act. A drop note is executed by `post drop`, never resolved
        by hand. Even with evidence, the human's approval was voided by the send-back, so sending needs a fresh approval."""
        from .ledger import all_feedback
        item = next((f for f in all_feedback(self.sp, now=self.now()) if f["id"] == fb), None)
        if item is None:
            raise ValueError(f"no feedback {fb!r}")
        evidence = None
        if item.get("kind") in dispatch_blocking() and not human:
            if item["kind"] == "drop":
                raise ValueError(f"{fb} is a human drop note: `ave studio post drop` executes it (an agent does not resolve it by hand)")
            post = store.load_post(self.sp, item["post"])
            was = item.get("state")
            now_state = note_state(post, item.get("variant"), item["kind"])
            if not isinstance(item.get("rev"), int) or post["rev"] <= item["rev"]:
                raise ValueError(f"cannot resolve {fb}: post {item['post']} is still at rev {post['rev']}, the rev the human sent it back at; "
                                 "make the change they asked for first (or ask them to close the note)")
            if was is not None and was == now_state:
                raise ValueError(f"cannot resolve {fb}: the {_KIND_PART[item['kind']]} the human sent back is unchanged since the note")
            evidence = {"note_rev": item["rev"], "post_rev": post["rev"], "state_before": was, "state_after": now_state}
        return ledger.resolve_feedback(self.sp, fb, by, rev=rev, note=note, now=self.now(), human=human, evidence=evidence)

    def alerts(self, include_acked: bool = False) -> list[dict]:
        return ledger.list_alerts(self.sp, include_acked=include_acked)

    def changes(self, cursor: str | None = None) -> dict:
        return _changes(self.sp, cursor)

    # ---- project
    def accounts(self) -> list[dict]:
        """Accounts from accounts/*.yaml with their TRUE state: `live` is the HMAC attestation verified now (platforms.attest.verify_live;
        absent/forged/off = False), `paused` is PAUSE or accounts/<id>.paused, plus credential readiness (names, never values), the
        24 h quota, and the last tick (views.account_rows)."""
        from . import views
        return views.account_rows(self)

    def summary(self) -> dict:
        posts = self.posts()
        counts: dict[str, int] = {}
        for p in posts:
            for v in p["variants"]:
                counts[v["status"]] = counts.get(v["status"], 0) + 1
        return {"title": self.sp.read_meta().get("title"), "tz": self.sp.config().get("tz"), "now": iso_utc(self.now()),
                "paused": dispatch.is_paused(self.sp), "posts": len(posts),
                "stages": {s: sum(1 for p in posts if p["stage"] == s) for s in ("idea", "draft", "active", "dropped", "archived")},
                "variants_by_status": counts, "open_feedback": len(self.feedback("open")), "open_alerts": len(self.alerts()),
                "accounts": len(self.accounts()), "cursor": self.changes()["cursor"]}


_KIND_PART = {"rework_copy": "copy", "rework_visual": "media", "retime": "slot", "drop": "variant"}


def dispatch_blocking() -> tuple:
    from .dispatch import BLOCKING_FEEDBACK
    return BLOCKING_FEEDBACK


def note_state(post: Mapping, variant: str | None, kind: str) -> str | None:
    """A short hash of the part of the post a send-back is about: the effective copy (rework_copy), the media and crops (rework_visual) or the
    slot (retime), for one variant or, when the note names none, for all of them. None for kinds that need no evidence."""
    import hashlib
    from .model import effective_copy, effective_media
    variants = [v for v in post.get("variants") or [] if not variant or v["id"] == variant]
    if kind == "rework_copy":
        part = [post.get("copy"), [(v["id"], effective_copy(post, v)) for v in variants]]
    elif kind == "rework_visual":
        part = [[(v["id"], effective_media(post, v)) for v in variants]]
    elif kind == "retime":
        part = [[(v["id"], (v.get("schedule") or {}).get("utc")) for v in variants]]
    else:
        return None
    return hashlib.sha256(json.dumps(part, sort_keys=True, default=str).encode()).hexdigest()[:24]


def _get(obj: Any, path: str) -> Any:
    cur = obj
    for seg in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(seg)
        elif isinstance(cur, list):
            hit = next((x for x in cur if isinstance(x, dict) and x.get("id") == seg), None)
            cur = hit if hit is not None else (cur[int(seg)] if seg.isdigit() and int(seg) < len(cur) else None)
        else:
            return None
    return cur


def _is_copy_path(path: str) -> bool:
    return path.startswith("copy.") or (path.startswith("variants.") and ".copy." in path)


# ------------------------------------------------------------------------------------------------ fixture mode

FIXTURE_FOR = {
    "summary": "GET_summary", "accounts": "GET_accounts", "calendar": "GET_calendar", "grid": "GET_grid", "posts": "GET_posts",
    "post": "GET_post", "preview": "GET_preview", "library": "GET_library", "library_asset": "GET_library_asset", "broll": "GET_broll",
    "feedback": "GET_feedback", "alerts": "GET_alerts", "changes": "GET_changes", "create_post": "POST_posts",
    "edit_post": "POST_edit_ok", "move": "POST_move_ok", "swap": "POST_swap_ok", "unschedule": "POST_unschedule",
    "swap_asset": "POST_swap_asset", "add_feedback": "POST_feedback", "record_view": "POST_view", "approve": "POST_approve",
    "reject": "POST_reject", "broll_search": "POST_broll_search", "broll_pick": "POST_broll_pick",
    "accept_proposal": "POST_proposal_accept", "pause": "POST_pause", "resume": "POST_resume", "rehearse": "POST_tick_rehearse",
    "upload": "PUT_library_upload",
}


def fixtures_dir() -> Path:
    root = resources.checkout_root()
    if root is None or not (root / "tests" / "data_studio" / "api").is_dir():
        raise SystemExit("API fixtures live in tests/data_studio/api of a source checkout; pass fixtures_dir explicitly")
    return root / "tests" / "data_studio" / "api"


class FixtureStudio(Studio):
    """A Studio with no files behind it: every public method returns the `response` of its committed fixture (FIXTURE_FOR). Writes do
    nothing. `fixture(stem)` returns the whole file ({method, path, query, request, status, response})."""

    def __init__(self, fixtures: str | Path | None = None, *, clock: Clock | None = None):
        self.dir = Path(fixtures) if fixtures else fixtures_dir()
        self.clock = clock or SystemClock()
        self.publishers, self.host, self.providers, self._hooks, self.sp = {}, None, {}, {}, None

    def fixture(self, stem: str) -> dict:
        return json.loads((self.dir / f"{stem}.json").read_text())

    def respond(self, name: str) -> Any:
        return self.fixture(FIXTURE_FOR[name])["response"]

    def __getattr__(self, name: str):
        if name in FIXTURE_FOR:
            return lambda *a, **kw: self.respond(name)
        raise AttributeError(name)


for _name in FIXTURE_FOR:             # real methods on Studio win over __getattr__, so route them for the fixture subclass
    if hasattr(Studio, _name):
        setattr(FixtureStudio, _name, (lambda n: lambda self, *a, **kw: self.respond(n))(_name))


def open_studio(path: str | Path | None = None, **kw) -> Studio:
    """Studio for the project at `path` (default `<workspace>/studio`); refuses a folder that is not a studio."""
    from .project import find_studio
    sp = find_studio(path)
    sp.require()
    studio = Studio(sp, **kw)
    wire(studio)
    return studio


def wire(studio: Studio) -> Studio:
    """Register every workstream's hooks on `studio`: platforms (account_live + adapters), schedule, library, broll, and the views the
    UI reads (grid, preview, swap_asset). A real Studio has every hook except INTENTIONALLY_UNREGISTERED."""
    from .. import broll, library, schedule
    from ..platforms import registry
    from . import views
    registry.register(studio)
    schedule.register(studio)
    library.register(studio)
    broll.register(studio)
    views.register(studio)
    return studio
