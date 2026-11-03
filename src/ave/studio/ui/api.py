"""The studio API (spec 7.3, 7.6): every `/api/studio/...` endpoint, written against the `Studio` facade.

`StudioApp.get(route, query)` and `StudioApp.post(route, body)` return `(status, json_obj)` or raise; `map_error` turns any exception into
the `{error, code, latest?}` shape (409 on a lost compare-and-swap carries the latest post; a facade hook nobody registered yet is 501).
With a `FixtureStudio` (`ave studio ui --fixtures`) every endpoint answers from the committed fixtures and writes nothing.

Approval is a HUMAN act and is gated here, not in the facade:
  * the PIN exists only in this process's memory and on the human's terminal (`PinGate`); it is never in a response, a file or a log,
    and five wrong tries lock approval until the server restarts;
  * `pin_after_review` (default) also needs a recorded VIEW of the exact sha (>= 2 s on screen), consumed by the approval so a replay is
    refused; `typed` mode needs the human to type >= 8 hex of the sha per item instead of the view;
  * the actual approval is `queue.approve(..., source="studio-ui")`; approving only records approval, it never sends.
"""

from __future__ import annotations

import getpass
import hmac
import secrets
import tempfile
import threading
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from ...publish import queue as pq, safety
from ...ui.api import ApiError
from .. import dispatch, ledger, policy, state, store
from ..clock import iso_utc
from ..events import Actor, append_event, make_event
from ..model import RevConflict, check_id, find_variant
from ..project import StudioProject
from ..bulk import BulkError
from ..service import FixtureStudio, NotImplementedYet, Studio
from ..sync import queue_diff

MAX_UPLOAD = 512 * 1024 * 1024
MIN_VIEW_MS = 2000
MAX_WRONG_PINS = 5
FIXTURE_PIN = "123456"          # `--fixtures` only: nothing behind it is real
NOT_VIEWED = "not viewed: open the exact media for at least 2 seconds first"
FEEDBACK_KINDS = ("rework_copy", "rework_visual", "drop", "retime", "note")      # `human_edit` is written by `edit`, never by hand


class StudioError(ApiError):
    def __init__(self, status: int, message: str, code: str = "error", latest: Any = None):
        super().__init__(status, message)
        self.code = code
        self.latest = latest

    def body(self) -> dict:
        out = {"error": self.message, "code": self.code}
        if self.latest is not None:
            out["latest"] = self.latest
        return out


class Refused(Exception):
    """One approval/rejection item that was not done (reported in `refused`, the batch carries on)."""


class PinGate:
    """The per-run approval PIN. `reveal()` is for the terminal banner only; nothing else ever reads it back out."""

    def __init__(self, pin: str | None = None):
        self._pin = pin or f"{secrets.randbelow(10 ** 6):06d}"
        self._wrong = 0
        self._lock = threading.Lock()

    def reveal(self) -> str:
        return self._pin

    @property
    def locked(self) -> bool:
        return self._wrong >= MAX_WRONG_PINS

    def check(self, supplied: Any) -> None:
        with self._lock:
            if self.locked:
                raise StudioError(403, "approval is locked after too many wrong PINs: restart `ave studio ui`", "pin.locked")
            ok = isinstance(supplied, str) and hmac.compare_digest(supplied.encode(), self._pin.encode())
            if not ok:
                self._wrong += 1
                left = MAX_WRONG_PINS - self._wrong
                raise StudioError(403, f"wrong PIN ({left} tries left before approval locks)" if left else
                                  "wrong PIN: approval is now locked, restart `ave studio ui`", "pin.wrong")

    def __repr__(self) -> str:
        return f"PinGate(locked={self.locked})"


def _need(body: Mapping, key: str, typ=str):
    v = body.get(key)
    if typ is int and isinstance(v, bool):
        v = None
    if not isinstance(v, typ) or (typ is str and not v.strip()):
        raise StudioError(400, f"{key!r} is required" + (" (an integer)" if typ is int else ""), "bad_request")
    return v.strip() if typ is str else v


def _q(qs: Mapping, key: str, default=None):
    v = qs.get(key)
    return (v[0] if isinstance(v, list) and v else v) if v else default


def _flag(v) -> bool:
    return str(v).lower() in ("1", "true", "yes", "on")


def _post_resp(studio: Studio, result: Any) -> dict:
    """Normalize a mutation result to `{"post": <view>, "warnings": []}` (a hook may return the bare post or the full shape)."""
    if isinstance(result, dict) and isinstance(result.get("post"), dict):
        return result
    post = result
    if isinstance(result, dict) and result.get("kind") == "studio.post" and getattr(studio, "sp", None) is not None:
        post = studio.view(result)
    return {"post": post, "warnings": []}


def map_error(e: BaseException, studio: Studio | None = None) -> StudioError:
    if isinstance(e, StudioError):
        return e
    if isinstance(e, NotImplementedYet):
        return StudioError(501, str(e), "not_implemented")
    if isinstance(e, RevConflict):
        latest = e.latest
        try:
            if latest and studio is not None and getattr(studio, "sp", None) is not None:
                latest = studio.view(latest)
        except Exception:
            pass
        return StudioError(409, str(e), "conflict.rev", latest)
    if isinstance(e, ApiError):
        return StudioError(e.status, e.message, "error")
    code = getattr(e, "code", None)
    if isinstance(code, str) and code.startswith("conflict"):
        return StudioError(409, str(e), code, getattr(e, "latest", None))
    if isinstance(e, FileNotFoundError):
        return StudioError(404, str(e) or "not found", "not_found")
    if isinstance(e, KeyError):
        return StudioError(404, str(e.args[0]) if e.args else "not found", "not_found")
    if isinstance(e, ValueError):
        return StudioError(400, str(e), code if isinstance(code, str) else "invalid")
    if isinstance(e, RuntimeError) and type(e).__name__ == "ClaimConflict":
        return StudioError(409, str(e), "conflict.claim")
    return StudioError(500, safety.redact_obj(f"{type(e).__name__}: {e}"), "internal")


class StudioApp:
    """What the handler calls. `ep` is the StudioProject (the shared `/files/` route and the upload temp dir hang off it)."""

    def __init__(self, studio: Studio, *, by: str | None = None, pin: str | None = None, fixtures: bool | None = None,
                 max_upload: int = MAX_UPLOAD, native_authority=None):
        self.studio = studio
        self.native_authority = native_authority
        self.fixtures = isinstance(studio, FixtureStudio) if fixtures is None else fixtures
        self._tmp = None
        if studio.sp is None:                       # fixtures: no files behind it, but /files/ and uploads need a (empty) root
            self._tmp = tempfile.TemporaryDirectory(prefix="ave-studio-fixtures-")
            sp = StudioProject(Path(self._tmp.name) / "studio")
            sp.create(title="Fixtures", tz="UTC")
            self.ep = sp
        else:
            self.ep = studio.sp
        self.by = (by or getpass.getuser())[:80]
        self.pin = PinGate(pin or (FIXTURE_PIN if self.fixtures else None))
        self.max_upload = max_upload
        self.views: dict[tuple[str, str], dict] = {}
        self.write_lock = threading.Lock()
        from ..workbench import Workbench
        self.workbench = Workbench(studio, fixtures=self.fixtures, by=self.by)
        from ..episode_checkpoints import Checkpoints
        self.episode_checkpoints = Checkpoints(self)
        from ..collaboration import Collaboration
        self.collaboration = Collaboration(studio)

    def close(self) -> None:
        self.collaboration.close()
        self.workbench.close()
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None

    # ------------------------------------------------------------------ helpers
    def _actor(self, body: Mapping) -> Actor:
        name = str(body.get("by") or self.by).strip()[:80] or self.by
        return Actor("human", name)

    def _by(self, body: Mapping) -> str:
        return _need(body, "by")[:80]

    def _approval_mode(self) -> str:
        sp = self.studio.sp
        return str((policy.studio_config(sp).get("approval") or {}).get("mode") or "pin_after_review") if sp else "pin_after_review"

    def _shape(self, key: str, result: Any, total: bool = False) -> dict:
        if isinstance(result, dict):
            return result
        out: dict = {key: result}
        if total:
            out["total"] = len(result)
        return out

    def _hook(self, name: str, *a, **kw):
        return self.studio.call(name, *a, **kw)

    # ------------------------------------------------------------------ GET
    def get(self, route: str, qs: Mapping) -> tuple[int, Any]:
        s = self.studio
        parts = [p for p in route.split("/") if p != ""]
        if not parts:
            raise StudioError(404, "no such endpoint", "not_found")
        head, rest = parts[0], parts[1:]
        if head == "episodes":
            from .. import episode_registry as ER, episode_workshop as EW
            if not rest: return 200, ER.catalog(s)
            if len(rest)==1: return 200, EW.load(s,rest[0],_q(qs,'session','main'))
            if len(rest) in (2,3) and rest[1]=='view':
                from .. import episode_view as EV
                if len(rest)==2:return 200,EV.load(s,rest[0],_q(qs,'session','main'))
                if rest[2]=='release':return 200,EV.release(s,rest[0],_q(qs,'session','main'))
                if rest[2]=='candidates':return 200,EV.candidates(s,rest[0],_q(qs,'session','main'))
                if rest[2]=='caption-words':return 200,EV.caption_words(s,rest[0],_q(qs,'session','main'),int(_q(qs,'page',0)))
            if len(rest)==2 and rest[1]=='transcript': return 200, EW.transcript_page(s,rest[0],_q(qs,'source'),int(_q(qs,'page',0)))
            if len(rest)==2 and rest[1]=='release':
                from ..episode_release import catalog
                return 200,catalog(s,rest[0],_q(qs,'session','main'))
            if len(rest)==2 and rest[1]=='candidates':
                from ..episode_release import candidates
                return 200,candidates(s,rest[0],_q(qs,'session','main'))
            if len(rest)==2 and rest[1]=='caption-words': return 200, EW.caption_words(s,rest[0],_q(qs,'session','main'),int(_q(qs,'page',0)))
        if head == "workbench":
            from .. import image_workshop, media_workshop, manuscripts, video_workshop
            if rest==["kept-clips"]: return 200,video_workshop.kept_clips(s.sp)
            if rest==["video-projects"]: return 200,video_workshop.catalog(s.sp)
            if len(rest)==2 and rest[0]=="video-projects": return 200,video_workshop.load(s.sp,rest[1],int(_q(qs,"version")) if _q(qs,"version") else None)
            if len(rest)==3 and rest[0]=="video-projects" and rest[2]=="words": return 200,video_workshop.transcript_page(s.sp,rest[1],_q(qs,"source"),int(_q(qs,"page",0)),_q(qs,"programme")=="1",int(_q(qs,"version")) if _q(qs,"version") else None)
            if len(rest)==3 and rest[0]=="media" and rest[2]=="capabilities": return 200,image_workshop.capabilities(s.sp,rest[1])
            if rest==["designs"]:
                from ...content import manifest as M
                rows=[]
                for did in M.doc_ids(s.sp):
                    if not did.startswith("design_"): continue
                    info=media_workshop.load_design(s.sp,did)
                    rows.append({"id":did,"version":info["version"],"title":next((e["text"][:80] for e in info["doc"]["items"][0]["visual"]["elements"] if e["kind"]=="text"),did)})
                return 200,{"designs":rows[:200]}
            if len(rest)==2 and rest[0]=="designs": return 200,media_workshop.load_design(s.sp,rest[1],int(_q(qs,"version")) if _q(qs,"version") else None)
            if len(rest)==2 and rest[0]=="manuscripts": return 200,manuscripts.page(s.sp,rest[1],int(_q(qs,"page",0)))
            if not rest: return 200, self.workbench.overview()
            if len(rest) == 2 and rest[0] == "jobs": return 200, self.workbench.tail(rest[1])
        if head == "feed-plan" and not rest:
            if s.sp is None:
                sample = dict(s.grid(_q(qs, "account", "")))
                sample.update(rev=0, snapshot="read-only-sample", read_only=True, order_kind="sample")
                sample["cells"] = [{**c, "key": f"{c['post']}:{c.get('variant') or 'base'}"} for c in sample.get("cells", [])]
                return 200, sample
            from .. import feed_plan
            result = feed_plan.read(s, account=_q(qs, "account", ""))
            result["read_only"] = self.fixtures
            return 200, result
        if head == "production" and not rest:
            return 200, s.production(**{k: _q(qs, k) for k in ("campaign", "owner", "reviewer", "status", "priority", "q")})
        if head in ("people", "workflows", "production-views") and not rest:
            from ..production import config
            return 200, config(s.sp, head)
        if head == "posts" and len(rest) == 2 and rest[1] == "comments":
            return 200, {"comments": s.production_comments(rest[0])}
        if head == "desk" and not rest:
            from ..desk import overview
            return 200, overview(s, fixtures=self.fixtures)
        if head == "summary" and not rest:
            out = dict(s.summary())
            out["ui"] = {"approval_mode": self._approval_mode(), "pin_locked": self.pin.locked, "fixtures": self.fixtures, "native": self.native_authority is not None, "native_authorized": bool(self.native_authority and self.native_authority.active)}
            return 200, out
        if head in ("profiles","copy-groups") and not rest:
            if self.fixtures: return 200, {"profiles":[]} if head=="profiles" else {"v":1,"rev":0,"items":[]}
            from .. import authoring
            return 200, {"profiles":authoring.profiles(s)} if head=="profiles" else authoring.groups(s)
        if head == 'collaboration':
            if not rest: return 200, self.collaboration.snapshot()
            if len(rest) == 2 and rest[0] == 'conflict': return 200, self.collaboration.conflict(rest[1])
            raise StudioError(404, 'No such collaboration view', 'not_found')
        if head == "connections" and not rest:
            if self.fixtures:
                from ...desktop_tools import fixture_connections
                return 200, fixture_connections(s.accounts())
            from ..connections import overview
            from ...desktop_service import Service
            from ...desktop_tools import readiness
            data=overview(s)
            data['service']=Service(s.sp).snapshot()
            data['tools']=readiness(s.sp)
            data['native']=self.native_authority is not None
            return 200, data
        if head == "accounts" and not rest:
            return 200, self._shape("accounts", s.accounts())
        if head == "calendar" and not rest:
            try:
                start, end = date.fromisoformat(_need(qs_flat(qs), "from")), date.fromisoformat(_need(qs_flat(qs), "to"))
            except ValueError:
                raise StudioError(400, "from and to must be ISO dates (YYYY-MM-DD)", "bad_request") from None
            accts = [a for a in (_q(qs, "accounts") or "").split(",") if a]
            return 200, s.calendar(start, end, accounts=accts or None)
        if head == "grid" and len(rest) == 1:
            return 200, s.grid(rest[0], limit=_int(_q(qs, "limit"), 30, 1, 200))
        if head == "campaigns" and not rest:
            if s.sp is None:                                    # fixtures: no files behind it
                return 200, {"campaigns": [], "total": 0}
            from .. import campaigns as C
            rows = [C.progress(s.sp, c["id"]) for c in C.list_all(s.sp)]
            return 200, {"campaigns": rows, "total": len(rows)}
        if head == "slots" and not rest:
            if self.fixtures:
                return 200, {"account": None, "tz": "UTC", "has_rules": False, "slots": []}
            check_id(_need(qs_flat(qs), "post"), "post id")
            return 200, s.free_slots(_need(qs_flat(qs), "post"), _need(qs_flat(qs), "variant"), count=_int(_q(qs, "count"), 6, 1, 20))
        if head == "posts":
            if not rest:
                rows = s.posts(stage=_q(qs, "stage"), campaign=_q(qs, "campaign"), account=_q(qs, "account"),
                               platform=_q(qs, "platform"), status=_q(qs, "status"), q=_q(qs, "q"))
                if _flag(_q(qs, "thumbs", "")) and not self.fixtures:
                    self._add_thumbs(rows)
                return 200, self._shape("posts", rows, total=True)
            if len(rest) == 1:
                return 200, self._post_detail(rest[0])
            if len(rest)==2 and rest[1]=='delivery-check':
                if self.fixtures: return 200, {'problems':[],'fixtures':True}
                from ..delivery import capabilities
                return 200, capabilities(s,rest[0],_q(qs,'variant'))
            if len(rest) == 2 and rest[1] == 'export-history':
                if self.fixtures: return 200, {'packages':[]}
                from ..manual_export import history
                packages=history(s.sp,rest[0],_q(qs,'variant') or None)
                for package in packages:
                    package['zip_url']='/files/'+s.sp.rel(s.sp.work/'manual_exports'/(package['package_id']+'.zip'))
                return 200, {'packages':packages}
            if len(rest) == 2 and rest[1] == "export":
                if self.fixtures:
                    return 200, {"ok": False, "fixtures": True, "warnings": ["Sample mode has no original files to export."]}
                from ..manual_export import describe
                return 200, describe(s.sp, rest[0], _q(qs, "variant"), draft=_flag(_q(qs, "draft", "")), now=s.now())
            if len(rest) == 3 and rest[1] == "preview":
                return 200, s.preview(rest[0], rest[2])
        if head == "library":
            if not rest:
                try:
                    limit = max(1, min(int(_q(qs, "limit", 500)), 2000))
                except ValueError:
                    raise StudioError(400, "limit must be an integer", "bad_request") from None
                res = s.library(q=_q(qs, "q"), kind=_q(qs, "kind"), tag=_q(qs, "tag"), license=_q(qs, "license"),
                                source=_q(qs, "source"), unused=_flag(_q(qs, "unused", "")), limit=limit)
                return 200, self._shape("assets", res, total=True)
            if len(rest) == 1:
                return 200, s.library_asset(rest[0])
        if head == "broll" and len(rest) == 1:
            return 200, s.broll(rest[0])
        if head == "feedback" and not rest:
            return 200, self._shape("feedback", s.feedback(_q(qs, "status", "open")))
        if head == "alerts" and not rest:
            return 200, self._shape("alerts", s.alerts(include_acked=_flag(_q(qs, "all", ""))))
        if head == "changes" and not rest:
            return 200, s.changes(_q(qs, "cursor"))
        raise StudioError(404, "no such endpoint", "not_found")

    def _add_thumbs(self, rows: list) -> None:
        """`thumb` (a /files/ URL or None) on every variant, from one read of the asset index: review and idea lists show pictures."""
        from ...schedule.slots import _thumb
        from ..image_workshop import preview_url
        assets = store.read_assets(self.studio.sp)
        for p in rows:
            for v in p.get("variants") or []:
                v["thumb"] = _thumb(assets, p, v)
                v["thumb"] = next((preview_url(a) for a in assets.values() if v["thumb"] == "/files/"+a["file"]),v["thumb"])
            if not p.get("variants"):
                p["thumb"] = _thumb(assets, p, None)
                p["thumb"] = next((preview_url(a) for a in assets.values() if p["thumb"] == "/files/"+a["file"]),p["thumb"])

    def _post_detail(self, post_id: str) -> dict:
        s = self.studio
        if self.fixtures:
            return s.post(post_id)
        check_id(post_id, "post id")
        view = s.post(post_id)
        fb = [f for f in s.feedback("all") if f.get("post") == post_id]
        rows, _ = ledger.read_jsonl(ledger.events_path(s.sp))
        events = [e for e in rows if e.get("post") == post_id][-50:]
        queue: dict = {}
        for v in view["variants"]:
            item = state.load_queue_item(s.sp, v)
            if item is None:
                continue
            ttl = policy.approval_ttl_days(s.sp, v.get("account"))
            at, exp = policy.approved_at(item), policy.approval_expires(item, ttl)
            ok = item.get("status") == "approved" and item.get("approvals")
            queue[v["id"]] = {"id": item["id"], "root": (v.get("queue") or {}).get("root", "."), "status": item.get("status"),
                              **_bound_view(s.sp, store.load_post(s.sp, post_id), find_variant(store.load_post(s.sp, post_id), v["id"]), item),
                              "sha256": item.get("sha256"), "approved_by": item["approvals"][-1].get("by") if ok else None,
                              "approved_at": item["approvals"][-1].get("at") if ok else None,
                              "expires_at": exp.strftime("%Y-%m-%dT%H:%M:%S%z") if ok and exp and at else None,
                              "preflight_problems": item.get("preflight_problems") or []}
        from ..composer import projection
        assets=store.read_assets(s.sp)
        return {"post": view, "feedback": fb, "events": events, "queue": queue, "media_projection": {v["id"]:projection(view,v,assets) for v in view["variants"]}}

    # ------------------------------------------------------------------ POST
    def post(self, route: str, body: dict) -> tuple[int, Any]:
        s = self.studio
        parts = [p for p in route.split("/") if p != ""]
        if self.native_authority is not None:
            # Explicit protected operations; adding future account/service routes
            # requires a native handler, never a caller-supplied authority flag.
            protected = (parts in (["approve"], ["resume"], ["broll", "pick"])
                         or (len(parts) == 4 and parts[:2] == ["schedule", "proposals"] and parts[3] == "accept"))
            if protected: self.native_authority.require()
            if parts and parts[0] in ("accounts", "auth", "live", "send", "service"):
                raise StudioError(403, "Use the native Connections action", "native.protected_action")
            if len(parts)==3 and parts[0]=='posts' and parts[2]=='delivery-refresh':
                raise StudioError(403, "Provider status refresh requires the trusted native Connections action", "native.protected_action")
        actor = self._actor(body)
        if parts and parts[0] == 'collaboration':
            from ..production import only
            if parts == ['collaboration', 'prepare-send']:
                return 200, self.collaboration.prepare_send(body)
            if parts == ['collaboration', 'preview']:
                only(body, {'digest'});return 200, self.collaboration.preview(body['digest'])
            if parts in (['collaboration', 'apply'], ['collaboration', 'send']):
                only(body, {'token'});return 200, getattr(self.collaboration, parts[1])(body['token'])
            if parts == ['collaboration', 'resolve']:
                return 200, self.collaboration.resolve(body)
            if parts == ['collaboration', 'cancel']:
                only(body, {'digest'});self.collaboration.require_exchange().cancel(body['digest']);return 200, {'state':'cancelled'}
            if parts == ['collaboration', 'promote']:
                only(body, {'digest','asset'});return 200, self.collaboration.promote(body['digest'],body['asset'])
            raise StudioError(403, 'Use the native sharing sheet', 'native.protected_action')
        if parts and parts[0] in ("profiles","copy-groups"):
            if self.fixtures: raise StudioError(501,"Sample mode is read-only","not_implemented")
            from .. import authoring
            if parts==["copy-groups"]: return 200,authoring.group_save(s,body,actor)
            if parts==["profiles"]: return 201,{'profile':authoring.profile_save(s,body,actor)}
            if len(parts)==3 and parts[2]=='edit': return 200,{'profile':authoring.profile_save(s,body,actor,parts[1])}
            if len(parts)==3 and parts[2]=='remove': return 200,authoring.profile_delete(s,parts[1],body,actor)
            raise StudioError(404,'No such authoring action','not_found')
        if parts and parts[0]=='episodes':
            if self.fixtures: raise ValueError('Sample mode is read-only')
            if parts==['episodes','prepare']: return 200,self.episode_checkpoints.prepare(body)
            if parts==['episodes','view']: return 200,self.episode_checkpoints.view(body)
            raise StudioError(403,'Editorial confirmation requires the native review sheet','native.protected_action')
        if len(parts)==2 and parts[0]=='workbench' and parts[1].startswith('episode-'):
            if self.fixtures: raise ValueError('Sample mode is read-only')
            return 202,self.workbench.start_media(parts[1],body)
        if len(parts)==2 and parts[0]=='workbench' and parts[1] in ('video-projects','video-edit','video-source','video-render','video-kept','video-cleanup','audio-attach'):
            if self.fixtures or s.sp is None: raise ValueError('A writable workspace is required')
            from .. import video_workshop as V
            if parts[1]=='video-projects': return 201,V.create(s,body)
            if parts[1]=='video-edit': return 200,V.mutate(s,body)
            if parts[1]=='audio-attach': return 200,V.attach_audio(s,body)
            return 202,self.workbench.start_media(parts[1],body)
        if parts and parts[0]=='workbench' and len(parts)==2 and parts[1] in ('image-transform','design-render','image-split','designs','attach'):
            if self.fixtures: raise StudioError(501,'Sample mode is read-only','not_implemented')
            from .. import media_workshop
            if parts[1]=='designs': return 201,media_workshop.save_design(s,body)
            if parts[1]=='attach': return 200,media_workshop.attach(s,body,actor)
            return 202,self.workbench.start_media(parts[1],body)
        if parts == ["workbench", "import-library-recording"]:
            if self.fixtures or s.sp is None: raise ValueError('A writable workspace is required')
            from .. import production, video_workshop
            production.only(body,{'asset'})
            row,path=video_workshop.input_source(s.sp,body.get('asset'))
            return 202,self.workbench._register_staged(path,path.name,path.stat().st_size,row['sha256'])
        if parts == ["workbench", "cancel-media"]: return 200, self.workbench.cancel_media(body)
        if parts == ["workbench", "cancel-frame"]: return 200, self.workbench.cancel_frame(body)
        if parts == ["workbench", "frame"]: return 202, self.workbench.start_frame(body)
        if parts == ["workbench", "render"]: return 202, self.workbench.start_render(body)
        if parts == ["workbench", "transcribe"]: return 202, self.workbench.start_transcribe(body)
        if parts == ["workbench", "retry"]: return 202, self.workbench.retry(body)
        if parts == ["feed-plan", "context"]:
            if self.fixtures: raise StudioError(501,"Sample mode is read-only","not_implemented")
            from .. import feed_plan, production
            production.only(body,{'account','expected_rev','snapshot','op','key','entry'})
            return 200,feed_plan.decorate(s,**body)
        if parts == ["feed-plan"]:
            if self.fixtures or s.sp is None:
                raise StudioError(501, "Saved feed plans require a studio folder. Sample data is read-only.", "not_implemented")
            from .. import feed_plan
            return 200, feed_plan.move(s, account=body.get("account", ""), expected_rev=_need(body, "expected_rev", int),
                                       key=_need(body, "key"), before=body.get("before"), snapshot=body.get("snapshot"))
        if parts and parts[0] in ("people", "workflows", "production-views", "campaigns"):
            if self.fixtures or s.sp is None:
                raise StudioError(501, "A writable workspace is required", "not_implemented")
            from .. import production as P, campaigns as C
            if len(parts) == 1 and parts[0] != "campaigns":
                P.only(body, {"expected_rev", "item"})
                return 200, P.save_config(s, parts[0], _need(body, "item", dict), expected_rev=_need(body, "expected_rev", int), actor=actor)
            if parts == ["campaigns"]:
                P.only(body, {"name", "goal", "brief", "window", "accounts", "themes", "cadence"})
                window = body.get("window") or {}
                P.only(window, {"start", "end"})
                c = C.new(s.sp, _need(body, "name"), goal=body.get("goal", ""), brief=body.get("brief", ""),
                          start=window.get("start"), end=window.get("end"), accounts=body.get("accounts", []),
                          themes=body.get("themes", []), per_week=body.get("cadence", {}), now=s.now())
                return 201, {"campaign": c}
            if len(parts) == 3 and parts[0] == "campaigns":
                if parts[2] == "edit":
                    P.only(body, {"expected_rev", "fields"})
                    return 200, {"campaign": C.edit(s.sp, parts[1], _need(body, "fields", dict), expected_rev=_need(body, "expected_rev", int), actor=actor, now=s.now())}
                if parts[2] == "members":
                    P.only(body, {"expected_rev", "post_revs", "add", "remove"})
                    return 200, {"campaign": C.members(s.sp, parts[1], expected_rev=_need(body, "expected_rev", int), post_revs=_need(body, "post_revs", dict), add=body.get("add", []), remove=body.get("remove", []), actor=actor)}
        if parts == ["intake"]:
            if self.fixtures:
                return 200, {"fixtures": True, "note": "Sample mode does not save source material."}
            from ..desk import intake
            with self.write_lock:
                return 201, intake(s, kind=_need(body, "kind"), text=_need(body, "text"),
                                   title=body.get("title", ""), actor=actor)
        if parts == ["posts"]:
            if self.fixtures:
                return 201, s.create_post()
            from ..production import only
            only(body,{"idea","copy","media","campaign","by"})
            if "by" in body:
                by = _need(body, "by")
                if len(by) > 80:
                    raise StudioError(400, "by must be at most 80 characters", "bad_request")
            copy = body.get("copy")
            if copy is not None and not isinstance(copy, dict):
                raise StudioError(400, "'copy' must be an object", "bad_request")
            media = body.get("media") or []
            if not isinstance(media, list):
                raise StudioError(400, "'media' must be a list", "bad_request")
            from ..composer import require_projection
            require_projection({"media":media},{},store.read_assets(s.sp))
            post = s.create_post(idea=body.get("idea") or None, copy=copy, media=media, campaign=body.get("campaign") or None, actor=actor)
            return 201, {"post": post}
        if len(parts) == 3 and parts[0] == "posts":
            return self._post_action(parts[1], parts[2], body, actor)
        if parts == ["bulk"]:
            return 200, self.bulk(body, actor)
        if parts == ["approve"]:
            return 200, self.approve(body)
        if parts == ["reject"]:
            return 200, self.reject(body, actor)
        if parts == ["broll", "search"]:
            res = s.broll_search(post=body.get("post"), query=_need(body, "query"), providers=body.get("providers"),
                                 kind=body.get("kind"), orientation=body.get("orientation"))
            return 201, res if "search" in res else {"search": res}
        if parts == ["broll", "pick"]:
            res = s.broll_pick(_need(body, "search"), _need(body, "candidate"), by=self._by(body))
            return 201, res
        if len(parts) == 4 and parts[:2] == ["schedule", "proposals"] and parts[3] == "accept":
            return 200, s.accept_proposal(parts[2], actor=actor)
        if parts == ["pause"]:
            return 200, self.pause(body, actor)
        if parts == ["resume"]:
            return 200, self.resume(body, actor)
        if parts == ["tick", "rehearse"]:
            return 200, s.rehearse()
        raise StudioError(404, "no such endpoint", "not_found")

    def _post_action(self, post_id: str, action: str, body: dict, actor: Actor) -> tuple[int, Any]:
        s = self.studio
        if action in ("media", "variants", "prepare-review", "duplicate"):
            if self.fixtures: raise StudioError(501, "Sample mode is read-only", "not_implemented")
            from .. import composer, production
            with self.write_lock:
                if action == "media": edited = composer.media_action(s, post_id, body, actor)
                elif action == "variants": edited = composer.add_variant(s, post_id, body, actor)
                elif action == "duplicate":
                    production.only(body, {"expected_rev"})
                    old=store.load_post(s.sp,post_id)
                    if old['rev'] != _need(body,'expected_rev',int): raise RevConflict(old)
                    import copy as copy_module
                    new=copy_module.deepcopy(old)
                    from ..model import new_post_id
                    new.update(id=new_post_id(),rev=1,stage='draft')
                    for v in new['variants']:
                        v.update(queue=None,queue_sha=None,result=None,attempts=[],schedule=None,flags=[],dropped=False,delivery={'method':'manual'},payload_version=3)
                    saved=store.save_post(s.sp,new,expected_rev=None,actor=actor,op='composer.duplicate',now=s.now())
                    return 201, {'post':s.view(saved)}
                else:
                    production.only(body, {'expected_rev','variant'})
                    old=store.load_post(s.sp,post_id)
                    if old['rev'] != _need(body,'expected_rev',int): raise RevConflict(old)
                    from ..sync import sync_queue
                    result=sync_queue(s.sp,post_id,actor=actor,publishers=s.publishers,clock=s.clock,variant_id=_need(body,'variant'))
                    return 200, {**self._post_detail(post_id),'preparation':result}
                self._follow_queue(post_id,edited,actor)
                return 200, self._post_detail(post_id)
        if action in ("production", "tasks", "comments", "comment-resolve"):
            if self.fixtures or s.sp is None:
                raise StudioError(501, "A writable workspace is required", "not_implemented")
            from .. import production as P
            allowed = {"expected_rev"} | {"production": {"fields"}, "tasks": {"op", "task"}, "comments": {"text", "reply_to", "anchor"}, "comment-resolve": {"comment_id", "resolved"}}[action]
            P.only(body, allowed)
            rev = _need(body, "expected_rev", int)
            if action == "production":
                return 200, {"post": s.update_production(post_id, _need(body, "fields", dict), expected_rev=rev, actor=actor)}
            if action == "tasks":
                return 200, {"post": s.production_task(post_id, _need(body, "op"), _need(body, "task", dict), expected_rev=rev, actor=actor)}
            return 200, P.comment(s, post_id, expected_rev=rev, text_value=body.get("text"), reply_to=body.get("reply_to"), anchor=body.get("anchor"), comment_id=body.get("comment_id"), resolved=body.get("resolved"), actor=actor)
        if action == 'delivery-refresh':
            if self.fixtures: raise StudioError(400,'Sample has no live delivery operation','bad_request')
            from ..delivery import refresh
            return 200,refresh(s,post_id,_need(body,'variant'))
        if action == "export":
            if self.fixtures:
                return 200, {"ok": False, "fixtures": True, "warnings": ["Sample mode has no original files to export."]}
            from ..manual_export import build
            draft = body.get("draft", False)
            if not isinstance(draft, bool):
                raise StudioError(400, "draft must be a boolean", "bad_request")
            rev = _need(body, "expected_rev", int)
            if isinstance(rev, bool) or rev < 1:
                raise StudioError(400, "expected_rev must be a positive integer", "bad_request")
            variant = body.get("variant")
            if variant is not None and not isinstance(variant, str):
                raise StudioError(400, "variant must be an id", "bad_request")
            with self.write_lock:
                result = build(s.sp, post_id, variant, draft=draft, expected_rev=rev, now=s.now())
            result["zip_url"] = "/files/" + s.sp.rel(Path(result["zip"]))
            result["media_urls"] = {m["filename"]: "/files/" + s.sp.rel(Path(result["directory"]) / m["filename"])
                                    for m in result["manifest"]["media"]}
            return 201, result
        if action == "view":
            return 200, self.record_view(post_id, body)
        if action == "feedback":
            return self.feedback(post_id, body, actor)
        rev = _need(body, "expected_rev", int)
        if action == "edit":
            patch = body.get("patch")
            if not isinstance(patch, dict) or not patch:
                raise StudioError(400, "'patch' must be a non-empty object of dotted paths", "bad_request")
            if self.fixtures:
                return 200, s.edit_post(post_id, patch)
            try:
                edited = s.edit_post(post_id, patch, expected_rev=rev, actor=actor, source="ui")
            except KeyError as e:                       # a patch path that points at nothing
                raise StudioError(400, str(e.args[0]) if e.args else "bad patch path", "bad_patch") from None
            warnings = self._follow_queue(post_id, edited, actor)
            if s.has("sync_queue"):
                edited = s.post(post_id)                # a sync may have written queue refs back (rev + 1): answer with the current post
            return 200, {"post": edited, "warnings": warnings}
        variant = _need(body, "variant")
        if action == "move":
            to = body.get("to")
            if not isinstance(to, dict):
                raise StudioError(400, "'to' must be {local, tz} or {slot}", "bad_request")
            res = s.move(post_id, variant, to, expected_rev=rev, actor=actor, snap=body.get("snap") or "slot")
        elif action == "swap":
            res = s.swap(post_id, variant, _need(body, "with"), expected_rev=rev, actor=actor)
        elif action == "unschedule":
            res = s.unschedule(post_id, variant, expected_rev=rev, actor=actor)
        elif action == "swap_asset":
            res = s.swap_asset(post_id, variant, _need(body, "index", int), _need(body, "asset"), expected_rev=rev, actor=actor)
        else:
            raise StudioError(404, "no such endpoint", "not_found")
        if isinstance(res, dict) and isinstance(res.get("post"), dict):
            return 200, res
        return 200, _post_resp(s, res)

    def _follow_queue(self, post_id: str, edited: dict, actor: Actor) -> list[str]:
        """A human's edit of copy or media must reach the queue (spec 2.8: a pre-publish edit of bound fields re-hashes the item and drops an
        approval to `awaiting_approval`): otherwise the UI would show an edit as live that the approved item does not contain. Returns the
        warnings the editor shows. Only runs when the studio has the sync hook and a variant already has a queue item."""
        s = self.studio
        if not s.has("sync_queue") or not any(v.get("queue") and not v.get("dropped") for v in edited["variants"]):
            return []
        try:
            rep = s.sync_queue(post_id, actor=actor)
        except Exception as e:                                           # the edit is saved; say that the queue could not follow
            return [f"saved, but the queue could not follow the edit: {type(e).__name__}: {e}"]
        out = []
        for row in rep.get("variants", []):
            if row.get("invalidated"):
                out.append(f"approval dropped: the copy or media changed; {row['variant']} is {row.get('queue_status') or 'in review'} again")
            elif row.get("problems") and row["action"] in ("blocked", "skipped"):
                out += [str(p) for p in row["problems"]]
        return out

    # ------------------------------------------------------------------ bulk retime (a HUMAN planner act; never approves)
    def bulk(self, body: dict, actor: Actor) -> dict:
        """Shift, set, spread over free slots, unschedule or reassign several variants. Each item runs the same checks as one drag and is
        reported in `done` or `refused`; `approved` is always 0: an approved post keeps its approval across a retime (the schedule is not
        signed) and the move is recorded as an event, and nothing here can approve anything."""
        op = body.get("op")
        if self.fixtures:
            return {"op": op, "done": [], "refused": [], "approved": 0}
        try:
            with self.write_lock:
                return self.studio.bulk(op, body.get("items"), actor=actor, days=body.get("days"), account=body.get("account"))
        except BulkError as e:
            raise StudioError(400, str(e), "bad_request") from None

    # ------------------------------------------------------------------ feedback (send back to Claude)
    def feedback(self, post_id: str, body: dict, actor: Actor) -> tuple[int, Any]:
        s = self.studio
        kinds = body.get("kinds") if body.get("kinds") is not None else ([body["kind"]] if body.get("kind") else None)
        if not isinstance(kinds, list) or not kinds or not all(isinstance(k, str) for k in kinds):
            raise StudioError(400, "'kinds' must be a non-empty list of: " + ", ".join(FEEDBACK_KINDS), "bad_request")
        bad = [k for k in kinds if k not in FEEDBACK_KINDS]
        if bad:
            raise StudioError(400, f"unknown feedback kind {bad[0]!r}; allowed: {', '.join(FEEDBACK_KINDS)}", "bad_request")
        variant = body.get("variant") or None
        structured = body.get("structured") if isinstance(body.get("structured"), dict) else {}
        if self.fixtures:
            return 201, s.add_feedback(post_id, kinds[0])
        lines, warnings = [], []
        for k in dict.fromkeys(kinds):
            lines.append(s.add_feedback(post_id, k, note=str(body.get("note") or "")[:4000], variant=variant, structured=structured,
                                        asset=body.get("asset") or None, by=actor.name, source="ui"))
        if "drop" in kinds and variant:
            warnings += self._drop(post_id, variant, actor, str(body.get("note") or ""))
        out = {"feedback": lines, "post": s.post(post_id)}
        if warnings:
            out["warnings"] = warnings
        return 201, out

    def _drop(self, post_id: str, variant_id: str, actor: Actor, reason: str) -> list[str]:
        """A drop feedback also rejects the queue item (when it can still be) and marks the variant dropped."""
        s, warnings = self.studio, []
        post = s.post(post_id)
        v = find_variant(post, variant_id)
        item = state.load_queue_item(s.sp, v)
        if item is not None:
            try:
                with self.write_lock:
                    pq.reject(state.queue_episode(s.sp, v), item["id"], actor.name, reason[:500] or "dropped from the studio UI")
            except ValueError as e:
                warnings.append(f"queue item not rejected: {e}")
        s.edit_post(post_id, {f"variants.{variant_id}.dropped": True}, expected_rev=post["rev"], actor=actor, source="ui")
        return warnings

    # ------------------------------------------------------------------ view + approve + reject (HUMAN acts)
    def _served_sha(self, post_id: str, variant_id: str) -> str | None:
        """The sha the studio would approve for this variant right now (None in fixture mode: no files to hash)."""
        if self.fixtures:
            return None
        s = self.studio
        post = s.post(post_id)
        v = find_variant(post, variant_id)
        item = state.load_queue_item(s.sp, v)
        if item is None:
            raise StudioError(409, "this variant has no queue item yet: run `ave studio queue sync` first", "view.no_queue")
        try:
            return pq.current_sha(state.queue_episode(s.sp, v), item)
        except (OSError, ValueError, KeyError) as e:
            raise StudioError(409, f"cannot hash the media: {e}", "view.no_media") from None

    def record_view(self, post_id: str, body: dict) -> dict:
        if self.native_authority is not None: self.native_authority.require()
        variant = _need(body, "variant")
        sha = _need(body, "sha").lower()
        ms = _need(body, "displayed_ms", int) if "displayed_ms" in body else 0
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise StudioError(400, "'sha' must be the 64-hex sha256 the UI was shown", "bad_request")
        served = self._served_sha(post_id, variant)
        if served is not None and served != sha:
            raise StudioError(409, "the media on disk is not what you were shown: reload and view again", "view.stale_sha")
        now = iso_utc(self.studio.now())
        with self.write_lock:
            prev = self.views.get((post_id, variant))
            keep = prev["ms"] if prev and prev["sha"] == sha else 0
            self.views[(post_id, variant)] = {"sha": sha, "ms": max(keep, max(ms, 0)), "at": now}
        if not self.fixtures:
            try:
                append_event(self.studio.sp, make_event(actor=Actor("human", self.by), op="viewed", post=post_id, variant=variant,
                                                        after_sha=sha, detail={"displayed_ms": max(ms, 0)}, now=self.studio.now()))
            except ValueError:
                pass
        out = {"ok": True, "viewed": {"post": post_id, "variant": variant, "sha": sha, "at": now}}
        if not self.fixtures:
            out["queue"] = self._queue_view(post_id, variant)
        return out

    def _queue_view(self, post_id: str, variant_id: str) -> dict | None:
        """What an approval of this variant would actually SIGN, read from the QUEUE ITEM (caption, first comment, link, account, alt texts, the
        sha256 of every file) plus how it differs from the post (`mismatch`; empty = the item IS the post, which is the only state the server will
        approve). The review screen must show this, not the post's own fields."""
        s = self.studio
        raw = store.load_post(s.sp, post_id)
        v = find_variant(raw, variant_id)
        item = state.load_queue_item(s.sp, v)
        if item is None:
            return None
        return _bound_view(s.sp, raw, v, item)

    def approve(self, body: dict) -> dict:
        if self.native_authority is not None: self.native_authority.require()
        by = self._by(body)
        items = body.get("items")
        if not isinstance(items, list) or not items or len(items) > 50 or not all(isinstance(i, dict) for i in items):
            raise StudioError(400, "'items' must be a list of 1 to 50 {post, variant, sha} objects", "bad_request")
        self.pin.check(body.get("pin"))
        typed = self._approval_mode() == "typed"
        approved, refused = [], []
        with self.write_lock:
            for it in items:
                try:
                    approved.append(self._approve_one(it, by, typed))
                except Refused as r:
                    refused.append({"post": it.get("post"), "variant": it.get("variant"), "reason": str(r)})
        return {"approved": approved, "refused": refused}

    def _approve_one(self, it: dict, by: str, typed: bool) -> dict:
        post_id, variant_id, sha = it.get("post"), it.get("variant"), str(it.get("sha") or "").lower()
        if not (isinstance(post_id, str) and isinstance(variant_id, str) and len(sha) == 64):
            raise Refused("each item needs post, variant and the full 64-hex sha")
        if typed:
            typed_sha = str(it.get("confirm") or "").strip().lower()
            if len(typed_sha) < 8 or not sha.startswith(typed_sha):
                raise Refused("typed mode: type at least 8 hex characters of the sha you reviewed")
        else:
            view = self.views.get((post_id, variant_id))
            if view is None or view["ms"] < MIN_VIEW_MS:
                raise Refused(NOT_VIEWED)
            if view["sha"] != sha:
                raise Refused("the media you viewed is not the sha being approved: view it again")
        if self.fixtures:
            self.views.pop((post_id, variant_id), None)
            return {"post": post_id, "variant": variant_id, "queue_id": "fixture-queue-item", "sha": sha, "by": by,
                    "at": iso_utc(self.studio.now()), "source": "studio-ui"}
        s = self.studio
        try:
            post = s.post(post_id)
            v = find_variant(post, variant_id)
        except (FileNotFoundError, KeyError, ValueError) as e:
            raise Refused(str(e.args[0]) if isinstance(e, KeyError) and e.args else str(e)) from None
        if v.get("dropped"):
            raise Refused("this variant is dropped")
        item = state.load_queue_item(s.sp, v)
        if item is None:
            raise Refused("no queue item yet: run `ave studio queue sync` first")
        ep = state.queue_episode(s.sp, v)
        if item.get("meta_v") not in (2, 3):
            raise Refused("this queue item is not a studio item (an adopted or legacy episode item): run queue sync to build one")
        try:
            cur = pq.current_sha(ep, item)
        except (OSError, ValueError, KeyError) as e:
            raise Refused(f"cannot hash the media: {e}") from None
        if cur != sha:
            raise Refused("the media changed since you reviewed it: view it again")
        raw = store.load_post(s.sp, post_id)
        rv = find_variant(raw, variant_id)
        diffs = queue_diff(s.sp, raw, rv, item)
        if diffs:
            raise Refused("the queue item does not match the post you reviewed (" + ", ".join(d["field"] for d in diffs) +
                          "): the approval would sign the queue item, not the post. Run queue sync and review again")
        try:
            done = pq.approve(ep, item["id"], by, sha, publishers=dispatch.queue_publishers(s.publishers, rv) or None, source="studio-ui",
                              bind=dispatch.approval_bind(s.sp, rv))
        except (ValueError, FileNotFoundError) as e:
            raise Refused(str(e)) from None
        self.views.pop((post_id, variant_id), None)
        ap = done["approvals"][-1]
        append_event(s.sp, make_event(actor=Actor("human", by), op="approved", post=post_id, variant=variant_id, rev=post.get("rev"),
                                      after_sha=sha, detail={"queue_id": item["id"], "source": "studio-ui"}, now=s.now()))
        return {"post": post_id, "variant": variant_id, "queue_id": item["id"], "sha": sha, "by": by, "at": ap.get("at"),
                "source": "studio-ui"}

    def reject(self, body: dict, actor: Actor) -> dict:
        s = self.studio
        by = self._by(body)
        items = body.get("items")
        if not isinstance(items, list) or not items or len(items) > 50 or not all(isinstance(i, dict) for i in items):
            raise StudioError(400, "'items' must be a list of 1 to 50 {post, variant} objects", "bad_request")
        reason = str(body.get("reason") or "")[:500]
        if self.fixtures:
            return s.reject()
        rejected, refused, last = [], [], None
        with self.write_lock:
            for it in items:
                pid, vid = it.get("post"), it.get("variant")
                try:
                    post = s.post(pid)
                    v = find_variant(post, vid)
                    item = state.load_queue_item(s.sp, v)
                    if item is None:
                        raise Refused("no queue item")
                    pq.reject(state.queue_episode(s.sp, v), item["id"], by, reason)
                except (Refused, ValueError, FileNotFoundError, KeyError, TypeError) as e:
                    refused.append({"post": pid, "variant": vid, "reason": str(e.args[0]) if isinstance(e, KeyError) and e.args else str(e)})
                    continue
                self.views.pop((pid, vid), None)
                append_event(s.sp, make_event(actor=Actor("human", by), op="rejected", post=pid, variant=vid, rev=post.get("rev"),
                                              detail={"queue_id": item["id"], "reason": reason}, now=s.now()))
                rejected.append({"post": pid, "variant": vid, "queue_id": item["id"]})
                last = pid
        out = {"post": s.post(last) if last else None, "rejected": rejected}
        if refused:
            out["refused"] = refused
        return out

    # ------------------------------------------------------------------ pause / resume
    def _pause_path(self, account: str | None) -> Path:
        sp = self.studio.sp
        if not account:
            return sp.pause_path
        check_id(account, "account id")
        if not (sp.accounts_dir / f"{account}.yaml").is_file():
            raise StudioError(404, f"no account {account!r}", "not_found")
        return sp.accounts_dir / f"{account}.paused"

    def pause(self, body: dict, actor: Actor) -> dict:
        account = body.get("account") or None
        if self.fixtures:
            return self.studio.pause()
        if self.studio.has("pause"):
            return self.studio.pause(account=account, by=actor.name)
        p = self._pause_path(account)
        p.write_text(iso_utc(self.studio.now()) + "\n")
        append_event(self.studio.sp, make_event(actor=actor, op="pause", detail={"account": account}, now=self.studio.now()))
        return {"paused": True, "scope": account or "studio"}

    def resume(self, body: dict, actor: Actor) -> dict:
        """Resuming needs the PIN (spec 6.7: `resume` is a human act)."""
        self.pin.check(body.get("pin"))
        account = body.get("account") or None
        if self.fixtures:
            return self.studio.resume()
        if self.studio.has("resume"):
            return self.studio.resume(account=account, by=actor.name, pin_verified=True)     # the PIN was checked above
        p = self._pause_path(account)
        p.unlink(missing_ok=True)
        append_event(self.studio.sp, make_event(actor=actor, op="resume", detail={"account": account}, now=self.studio.now()))
        return {"paused": self.studio.sp.pause_path.exists()}

    # ------------------------------------------------------------------ upload (streamed by the handler)
    def upload(self, tmp_path: Path, name: str, size: int, sha256: str) -> dict:
        res = self.studio.upload(name, tmp_path, by=self.by)
        return res if isinstance(res, dict) and "asset" in res else {"asset": res, "duplicate": False}


def _bound_view(sp, raw_post: Mapping, v: Mapping, item: Mapping) -> dict:
    from ...publish import safety as _safety
    ep = state.queue_episode(sp, v)
    try:
        files = [{"file": str(f), "sha256": _safety.sha256_file(f)} for f in pq.resolve_files(ep, dict(item))]
    except (OSError, KeyError, ValueError) as e:
        files = [{"file": None, "sha256": None, "error": str(e)}]
    try:
        mismatch = queue_diff(sp, raw_post, v, item)
    except (OSError, ValueError, KeyError) as e:
        mismatch = [{"field": "post", "queue": None, "post": f"cannot compare: {e}"}]
    for f in files:
        f["name"] = Path(f["file"]).name if f.get("file") else None
        f["url"] = _served_url(sp, f.get("file"))
    return {"bound": {"caption": item.get("caption"), "title": item.get("title"), "first_comment": item.get("first_comment"),
                      "link_url": item.get("link_url"), "account": item.get("account"), "post_type": item.get("post_type"),
                      "platforms": item.get("platforms"), "tags": item.get("tags"), "alt_texts": item.get("alt_texts"),
                      "delivery":item.get("delivery"), "cover":{**item["cover"],"url":_served_url(sp,str(sp.root/item["cover"]["file"]))} if item.get("cover") else None, "presentation":item.get("presentation"), "ai_disclosure": item.get("ai_disclosure"), "files": files, "sha256": item.get("sha256")},
            "mismatch": mismatch}


def _served_url(sp, file: str | None) -> str | None:
    """The `/files/` URL of a queue file when it sits under a part of the studio folder the server serves, else None."""
    from .server import SERVED_PREFIXES
    if not file:
        return None
    try:
        rel = Path(file).resolve().relative_to(Path(sp.root).resolve()).as_posix()
    except (OSError, ValueError):
        return None
    return "/files/" + rel if rel.startswith(SERVED_PREFIXES) else None


def qs_flat(qs: Mapping) -> dict:
    return {k: (v[0] if isinstance(v, list) and v else v) for k, v in qs.items()}


def _int(v, default: int, lo: int, hi: int) -> int:
    try:
        return min(max(int(v), lo), hi) if v not in (None, "") else default
    except ValueError:
        raise StudioError(400, "limit must be an integer", "bad_request") from None
