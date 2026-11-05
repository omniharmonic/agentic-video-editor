"""The studio app's localhost server (`ave studio ui`, `ave desktop`): the Room, the Workspace and Settings at `/app/`.

`StudioHandler` subclasses the base `Handler` (studio/ui/http.py), so the loopback-only bind, the per-run token (`?t=`, cookie,
`X-AVE-Token`), the Host and Origin checks, the CSP headers and the ranged `/files/` serving are inherited unchanged. What is new:

  * `/api/studio/*` (api.py), JSON errors as `{error, code, latest?}`; `/api/workspace/*` (workspace.py, episodes.py); `/api/room/*`;
  * `/` sends the browser to `/app/`; `/tools/` and `/static/` serve the four older tools kept under the Workspace's "More tools"
    menu (Production, Media tools, Feed planner, Post editor), unchanged;
  * `PUT /api/studio/workbench/upload?name=` (Media tools) streamed to a temp file under `work/uploads/` with its own cap;
  * the Origin check also covers PUT, and `/files/` only serves what the UI needs (library assets, exports, work);
  * the approval PIN, printed ONLY on the human's terminal by `run_cli` (never in a response, a file or a log). No HTTP route
    approves anything: approvals run in the app's native sheets (desktop_approvals.py), which spend a device-owner grant.

The change feed is polled (`GET /api/studio/changes?cursor=`, about every 3 s). The live Room is separate: `/api/room/*` (stage, flags, the
SSE stream `GET /api/room/stream`, resumable with Last-Event-ID), and `<root>/.ave/local/room.json` ({port, token}, 0600) tells the MCP
tools where it is for as long as the server runs.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import secrets
import select
import socket
import sys
import tempfile
import threading
import time
from dataclasses import asdict
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from ... import humanpresence
from .http import MAX_BODY, ApiError, Handler
from ...room.client import remove_room_file, take_queue, write_room_file
from ...room import cut as room_cut
from ...room import review as room_review
from ...room.state import Room
from .. import store
from ..service import FixtureStudio, Studio, open_studio
from .api import MAX_UPLOAD, StudioApp, StudioError, map_error
from .episodes import EpisodeError, Episodes
from .workspace import Workspace

STATIC = Path(__file__).parent / "static"
APP_STATIC = Path(__file__).resolve().parents[2] / "app" / "static"     # the Director's Room app, served at /app/
API = "/api/studio/"
ROOM = "/api/room/"
WORKSPACE = "/api/workspace/"
HEARTBEAT_S = 30.0
NOT_POST_S = 5.0                                         # how long an artifact found not to be a post is left alone by the post poll
MAX_STREAMS = 8
SERVED_PREFIXES = ("library/assets/", "exports/", "work/")          # the only parts of the studio folder /files/ will serve
STATIC_EXT = {".html", ".js", ".mjs", ".css", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".json", ".woff", ".woff2", ".txt"}
_TEXTUAL = {".html", ".js", ".mjs", ".css", ".svg", ".json", ".txt"}
_TYPES = {".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css", ".html": "text/html", ".svg": "image/svg+xml"}


class StudioServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, studio: Studio, port: int = 0, token: str | None = None, by: str | None = None, pin: str | None = None,
                 fixtures: bool | None = None, max_upload: int = MAX_UPLOAD, native_authority=None):
        self.token = token or secrets.token_urlsafe(24)
        self.app = StudioApp(studio, by=by, pin=pin, fixtures=fixtures, max_upload=max_upload, native_authority=native_authority)
        super().__init__(("127.0.0.1", port), StudioHandler)         # never anything but loopback
        self.port = self.server_address[1]
        self.room = Room()
        self.project_root = self.app.studio.sp.root if getattr(self.app.studio, "sp", None) is not None and not self.app.fixtures else None
        self._jobs: dict[tuple, threading.Thread] = {}
        self._reviews: dict[tuple, dict] = {}
        self._served: dict[tuple, str] = {}                   # (artifact, view) -> the files stamp of the last script or selects sent
        self._jobs_lock = threading.Lock()
        self.room_root = self.app.studio.sp.root if getattr(self.app.studio, "sp", None) is not None and not self.app.fixtures else None
        self.streams = threading.BoundedSemaphore(MAX_STREAMS)
        sp = getattr(self.app.studio, "sp", None) if not self.app.fixtures else None
        self._followers_lock = threading.Lock()
        self.followers = 0                                   # open streams; the Workspace watches the logs only while someone follows
        self.room_followers = 0                              # of those, the Room's own (?room=1): the Layout post poll runs only for them
        self._not_post: dict[str, float] = {}                # artifact -> when it was last found not to be a post (looked at again later)
        self.workspace = Workspace(self.project_root, sp=sp, publish=self.room.bus.publish, watching=lambda: self.followers > 0)
        self.workspace.authority = getattr(self.app, "native_authority", None)   # Confirm on a relayed sign-off needs its grant
        self.episodes = Episodes(self.workspace, self.app.by)   # the episode projects' content items, publish queue, trims and jobs
        self._post_seen: dict[tuple, int] = {}               # (artifact, post) -> the revision the Layout view was last told about
        if self.project_root is not None:
            self.workspace.start()                           # board events when another process or computer writes a log
        if sp is not None:
            threading.Thread(target=self._watch_files, daemon=True, name="room-files").start()
        if self.room_root is not None:
            try:
                self._replay(take_queue(self.room_root))      # what Claude did while the app was closed, before anyone can queue more
                write_room_file(self.room_root, self.port, self.token)
            except OSError:                                  # a read-only folder costs the MCP tools their way in, not the UI its server
                self.room_root = None

    def _replay(self, calls: list[dict]) -> None:
        """Apply the room tool calls queued while the app was closed, oldest first, so the latest stage, highlights and offer stand."""
        for c in calls:
            args = c.get("args")
            try:
                if c.get("tool") == "room_show":
                    self.room.show(**args)
                elif c.get("tool") == "room_highlight":
                    self.room.highlight(args["highlights"])
                elif c.get("tool") == "room_offer_version":
                    self.offer(args["version"], artifact=args["artifact"], compare_to=args.get("compare_to"), summary=args.get("summary"))
            except (TypeError, ValueError, LookupError, KeyError, AttributeError):
                continue                                     # a stale or hand-edited line is dropped, not fatal

    bus = property(lambda self: self.room.bus)
    stage = property(lambda self: self.room.stage)

    def flag(self, flag: dict) -> int:
        return self.room.flag(flag)

    def offer(self, version: str, *, artifact: str | None = None, compare_to: str | None = None, span=None, summary: str | None = None):
        """Offer a version on stage (artifact defaults to the one on stage) and start rendering its changed part; the result arrives on
        the stream as a `preview` event, so the offer never waits on the render."""
        artifact = artifact or (self.room.stage.artifact if self.room.stage else None)
        if not isinstance(artifact, str) or not artifact:
            raise ValueError("'artifact' is required when nothing is on stage")
        stage = self.room.offer_version(artifact, version, compare_to, span=span, summary=summary)
        offer = self.room.offer
        self._background(("preview", artifact, version), lambda: self._preview(offer))
        return stage

    def ready(self, ep, version: str | None) -> dict:
        """Readiness of one version of an episode: {status, reason, from, text}. A version the log does not know is judged by
        its file, as before (ready; a missing file is the cut view's own 404)."""
        got = self.workspace.version_ready(ep.root, version) if isinstance(version, str) else None
        return got or {"status": "ready", "reason": None, "from": None, "text": None}

    def _not_ready(self, ep, version: str) -> str | None:
        r = self.ready(ep, version)
        return None if r["status"] == "ready" else r["text"]

    def _preview(self, offer: dict) -> None:
        a, v = offer["artifact"], offer["version"]
        try:
            ep = room_cut.find_episode(self.project_root, a) if self.project_root else None
        except room_cut.NotFound as e:
            ep, why = None, str(e)
        else:
            why = "This app has no project folder open"
        if ep is not None and (wait := self._not_ready(ep, v)):
            ep, why = None, wait
        if ep is None:
            self.room.set_preview({"artifact": a, "version": v, "status": "failed", "message": why})
            return
        self.room.set_preview({"artifact": a, "version": v, "status": "rendering"})
        self.room.set_preview(room_cut.preview_offer(ep, a, v, offer["compare_to"], offer["span"]))

    def render_full(self, artifact: str, version: str) -> dict:
        """Start rendering a whole version for the player (deduplicated); `media` events say when it is rendering, ready or failed."""
        if not self.project_root:
            raise LookupError("This app has no project folder open")
        ep = room_cut.find_episode(self.project_root, artifact)
        if isinstance(version, str) and (wait := self._not_ready(ep, version)):
            raise LookupError(wait)                          # never render a version before its files arrive
        room_cut.version_file(ep, version)

        def run():
            self.bus.publish("media", {"artifact": artifact, "version": version, "status": "rendering"})
            try:
                self.bus.publish("media", room_cut.render_full(ep, artifact, version))
            except Exception as e:
                self.bus.publish("media", {"artifact": artifact, "version": version, "status": "failed",
                                           "message": f"The full cut could not be rendered: {e}"[:400]})
        started = self._background(("render", artifact, version), run)
        return {"artifact": artifact, "version": version, "status": "rendering" if started else "already rendering"}

    def review_copy(self, artifact: str, key: str) -> dict:
        """Start making the review copy of one recording for the Script and Selects viewer (deduplicated); `review` events say how
        it goes: waiting (another copy is being made), rendering (with progress), ready (with the url) or failed (a plain reason).
        The latest state of every job is in GET /api/room/state as `reviews`, so a page that reconnects picks it up."""
        if not self.project_root:
            raise LookupError("This app has no project folder open")
        ep = room_cut.find_episode(self.project_root, artifact)
        src = room_review.source(ep, key)
        base = {"artifact": artifact, "key": key}
        if src["ready"]:
            return self._review_state({**base, "status": "ready", "url": room_review.source_url(artifact, key)}, publish=False)

        def run():
            last = [0.0]

            def progress(x):
                if x - last[0] >= 0.02 or x >= 1.0:
                    last[0] = x
                    self._review_state({**base, "status": "rendering", "progress": round(x, 3)})
            self._review_state({**base, "status": "rendering", "progress": 0.0})
            try:
                room_review.make_review_copy(ep, key, progress, waiting=lambda n: self._review_state({**base, "status": "waiting", "ahead": n}))
                self._review_state({**base, "status": "ready", "url": room_review.source_url(artifact, key)})
            except room_review.CopyError as e:
                self._review_state({**base, "status": "failed", "message": str(e)[:300]})
            except Exception:
                self._review_state({**base, "status": "failed", "message": "the copy could not be made"})
        started = self._background(("review", artifact, key), run)
        if started:
            return self._review_state({**base, "status": "rendering", "progress": 0.0}, publish=False)
        with self._jobs_lock:
            return dict(self._reviews.get((artifact, key)) or {**base, "status": "rendering", "progress": 0.0})

    def _review_state(self, d: dict, publish: bool = True) -> dict:
        with self._jobs_lock:
            self._reviews[(d["artifact"], d["key"])] = dict(d)
        if publish:
            self.bus.publish("review", d)
        return d

    def reviews(self) -> list[dict]:
        with self._jobs_lock:
            return [dict(v) for v in self._reviews.values()]

    def layout_of(self, artifact) -> dict:
        """The studio post the Layout view composes for an artifact on stage: the artifact is a post's own id, or a board artifact
        whose `post` field names one (artifact_update sets it). NotFound when it is neither."""
        if not isinstance(artifact, str) or not artifact:
            raise ValueError("Name the artifact on stage.")
        sp = getattr(self.app.studio, "sp", None) if not self.app.fixtures else None
        if sp is None:
            raise room_cut.NotFound("This app has no studio folder open")
        try:
            if store.post_path(sp, artifact).is_file():
                return {"artifact": artifact, "post": artifact}
        except ValueError:                                   # not a post id at all: maybe a board artifact
            pass
        try:
            post = self.workspace.post_of(artifact)
        except StudioError:
            post = None
        try:
            if post and store.post_path(sp, post).is_file():
                return {"artifact": artifact, "post": post}
        except ValueError:
            pass
        raise room_cut.NotFound("Claude put something on stage that is not a post in this studio")

    def _watch_post(self, artifact: str) -> bool:
        """A post is on stage and the Room is open: tell the page when the post's revision moves (Claude edited it through its tools,
        or another window did), so the layout follows without a reload. Decided by the artifact, not the stage's view: the Room shows
        Layout for a post whatever view it was staged with. The first look also reports, so an edit made before it is never missed.
        Returns whether the artifact is a post (an episode is looked at again only every few seconds)."""
        if time.monotonic() - self._not_post.get(artifact, -1e9) < NOT_POST_S:
            return False
        try:
            pid = self.layout_of(artifact)["post"]
        except (room_cut.NotFound, ValueError):
            self._not_post[artifact] = time.monotonic()
            return False
        except Exception:  # noqa: BLE001 - the board mid-write: look again in a second
            return False
        self._not_post.pop(artifact, None)
        try:
            rev = store.load_post(self.app.studio.sp, pid).get("rev")
        except Exception:  # noqa: BLE001 - a post mid-write or gone: look again in a second
            return True
        key = (artifact, pid)
        with self._jobs_lock:
            if self._post_seen.get(key) == rev:
                return True
            self._post_seen[key] = rev
        self.bus.publish("post", {"artifact": artifact, "post": pid, "rev": rev})
        return True

    def _watch_files(self) -> None:
        """While someone follows the stream and the stage shows Script or Selects, notice a changed transcript or clip list (Claude
        re-ran the clip finder, say) and tell the page, which reloads that view. On Layout, follow the post's revision instead."""
        while not self.room.bus.closed:
            time.sleep(1.0)
            st = self.room.stage
            if not st or not self.followers:
                continue
            if self.room_followers and self._watch_post(st.artifact):
                continue
            if st.view == "layout":
                continue
            if not self.project_root:
                continue
            try:
                ep = room_cut.find_episode(self.project_root, st.artifact)
                stamp = room_review.files_stamp(ep)
            except Exception:
                continue
            for view in ("script", "selects"):              # compared with what the page was last given, so no change slips by
                with self._jobs_lock:
                    old = self._served.get((st.artifact, view))
                    if old is None or old == stamp[view]:
                        continue
                    self._served[(st.artifact, view)] = stamp[view]
                self.bus.publish("files", {"artifact": st.artifact, "view": view})

    def served(self, artifact: str, view: str, stamp: str) -> None:
        with self._jobs_lock:
            self._served[(artifact, view)] = stamp

    def _background(self, key: tuple, fn) -> bool:
        from ...updates import refuse_if_draining
        refuse_if_draining()                                  # the restart for an update was approved: nothing new starts
        with self._jobs_lock:
            t = self._jobs.get(key)
            if t is not None and t.is_alive():
                return False

            def run():
                try:
                    fn()
                finally:
                    with self._jobs_lock:
                        self._jobs.pop(key, None)
            self._jobs[key] = t = threading.Thread(target=run, name=f"room-{key[0]}", daemon=True)
            t.start()
            return True

    def selects_write(self, route: str, body: dict, *, via: str | None = None) -> dict:
        """The director's keep / maybe / skip, or a nudge of a select's ends. Attribution follows the channel: only the app's own
        native bridge (`via="app"`) records it as the person's, as the clip finder's human UI decision (source "ui", which the
        shorts path trusts). Over HTTP the request carries only the room token, which Claude holds, so it is Claude's: by
        "claude", source "relayed", actor "claude" in the project log, and never a human-kept clip."""
        from ...humanpresence import agent_markers_set
        if route not in ("selects/decide", "selects/nudge") or not isinstance(body, dict):
            raise StudioError(404, "not found", "not_found")
        why = self.update_needed_for(body.get("artifact"))
        if why:
            raise StudioError(409, why, "read_only")
        actor = "human" if via == "app" and not agent_markers_set() else "claude"
        root = self.project_root
        if root is None:
            raise StudioError(404, "This app has no project folder open", "not_found")
        ep = room_cut.find_episode(root, body.get("artifact"))
        try:
            if route == "selects/decide":
                got = room_review.decide(ep, body.get("clip"), body.get("verdict"), reasons=body.get("reasons"), note=body.get("note"),
                                         actor=actor)
            else:                                            # the director moves a select's start or end by whole sentences
                got = room_review.nudge(ep, body.get("clip"), body.get("nudge"), actor=actor)
        except room_review.StaleSelect as e:
            raise StudioError(409, str(e), "stale") from None
        self.bus.publish("selects", {"artifact": body.get("artifact"), **got, "by": actor})
        return got

    def update_needed_for(self, artifact) -> str | None:
        """Why the Room must not edit this artifact's project here: "This project was saved by a newer AVE (x.y). Update to
        edit it." (project.model.read_only_reason), or None. The episode folder's project, else the studio's."""
        from ...project.model import read_only_reason
        root = self.project_root
        if root is None:
            return None
        try:
            return read_only_reason(room_cut.find_episode(root, artifact).root) or read_only_reason(root)
        except Exception:  # noqa: BLE001 - not an episode on stage (a post, nothing): the studio itself
            return read_only_reason(root)

    def busy(self) -> list[str]:
        """What the Room is writing right now (full-cut and preview renders, review copies), in plain words. A restart for an
        update waits for these (ave.updates.busy_reasons)."""
        with self._jobs_lock:
            live = [key for key, t in self._jobs.items() if t.is_alive()]
        out = ["a render is running" if key[0] in ("render", "preview") else
               "a review copy is being made" if key[0] == "review" else f"a {key[0]} is running" for key in live]
        return list(dict.fromkeys(out))

    def app_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/app/?t={self.token}"

    def url(self) -> str:
        """The More tools window: the four older tools the app keeps unchanged (Production, Media tools, Feed planner, Post editor)."""
        return f"http://127.0.0.1:{self.port}/tools/?t={self.token}"

    tools_url = url

    def server_close(self) -> None:
        if not hasattr(self, "room"):                        # the bind failed inside __init__: let its OSError (port in use) through
            return super().server_close()
        self.room.bus.close()                                # ends any open stream
        room_review.stop_copies()
        self.workspace.stop()
        if self.room_root is not None:
            remove_room_file(self.room_root, self.token)
        super().server_close()
        self.app.close()


class StudioHandler(Handler):
    server: StudioServer
    server_version = "ave-studio"
    timeout = 120

    # ---- static tree (E5 hook)
    def static_resolver(self, path: str) -> Path | None:
        if path in ("/app", "/app/") or path.startswith("/app/"):
            rel = unquote(path[len("/app/"):]) or "index.html"
            if "\x00" in rel or "\\" in rel or rel.startswith("/"):
                return None
            if any(p in ("..", "") or p.startswith(".") for p in Path(rel).parts):
                return None
            f = (APP_STATIC / rel).resolve()
            try:
                f.relative_to(APP_STATIC)
            except ValueError:
                return None
            return f if f.is_file() and f.suffix.lower() in STATIC_EXT else None
        if path in ("/tools", "/tools/"):
            rel = "index.html"
        elif path.startswith("/static/"):
            rel = unquote(path[len("/static/"):])
        else:
            return None
        if not rel or "\x00" in rel or "\\" in rel or rel.startswith("/"):
            return None
        parts = Path(rel).parts
        if any(p in ("..", "") or p.startswith(".") for p in parts):
            return None
        f = (STATIC / rel).resolve()
        try:
            f.relative_to(STATIC.resolve())
        except ValueError:
            return None
        if f.is_file() and f.suffix.lower() in STATIC_EXT:
            return f
        return None

    def _moved(self, where: str) -> None:
        self.send_response(302)
        self.send_header("Location", where)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _send_static(self, f: Path) -> None:
        ext = f.suffix.lower()
        ctype = _TYPES.get(ext) or mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        self._send(200, f.read_bytes(), ctype + ("; charset=utf-8" if ext in _TEXTUAL else ""))

    # ---- gate: the inherited one plus the Origin rule for PUT/DELETE
    def _gate(self) -> tuple[bool, bool]:
        ok, in_query = super()._gate()
        if ok and self.command in ("PUT", "DELETE", "PATCH"):
            origin = self.headers.get("Origin")
            if origin and origin not in (f"http://127.0.0.1:{self.server.port}", f"http://localhost:{self.server.port}"):
                self._err(403, "bad Origin")
                return False, False
        return ok, in_query

    def _studio_err(self, e: BaseException) -> None:
        se = map_error(e, self.server.app.studio)
        self._json(se.status, se.body())

    # ---- verbs
    def do_GET(self):
        ok, in_query = self._gate()
        if not ok:
            return
        path = urlsplit(self.path).path
        try:
            if path == ROOM + "stream":
                return self._room_stream()
            if path.startswith(ROOM):
                return self._room_get(path[len(ROOM):].strip("/"), parse_qs(urlsplit(self.path).query))
            if path.startswith(WORKSPACE):
                return self._workspace("GET", unquote(path[len(WORKSPACE):]).strip("/"), None)
            if in_query and not path.startswith("/api/"):
                return self._redirect(path)
            if path in ("/", "/index.html"):                 # the old shell is retired: the app is the front door
                return self._moved("/app/")
            f = self.static_resolver(path)
            if f is not None:
                return self._send_static(f)
            if path.startswith("/files/"):
                return self._studio_file(unquote(path[len("/files/"):]))
            if path.startswith(API):
                route = unquote(path[len(API):]).strip("/")
                qs = parse_qs(urlsplit(self.path).query)
                if route.startswith("files/"):
                    return self._studio_file(route[len("files/"):])
                if route.startswith('episodes/') and '/artifacts/' in route:
                    from .. import episode_registry as ER, episode_workshop as EW
                    bits=route.split('/')
                    if len(bits)!=4 or bits[2]!='artifacts': raise StudioError(404,'Unknown episode artifact')
                    with ER.resolve(self.server.app.studio,bits[1],verify_sources=False):
                        artifact=EW.artifact(self.server.app.studio,bits[1],bits[3])
                        return self._studio_file(artifact['file'],episode_artifact=True)
                if route.startswith("workbench/preview/"):
                    from ..image_workshop import rendition
                    bits=route.split("/")
                    if len(bits)!=4: raise StudioError(400,"Invalid preview request")
                    app=self.server.app
                    path=rendition(app.studio.sp,bits[2],bits[3])
                    return self._studio_file(app.studio.sp.rel(path))
                status, obj = self.server.app.get(route, qs)
                return self._json(status, obj)
            raise StudioError(404, "not found", "not_found")
        except (Exception, SystemExit) as e:
            self._studio_err(e)

    def do_POST(self):
        ok, _ = self._gate()
        if not ok:
            return
        path = urlsplit(self.path).path
        try:
            if path.startswith(ROOM):
                return self._room_post(path[len(ROOM):].strip("/"), self._json_body())
            if path.startswith(WORKSPACE):
                return self._workspace("POST", unquote(path[len(WORKSPACE):]).strip("/"), self._json_body())
            if not path.startswith(API):
                raise StudioError(404, "not found", "not_found")
            body = self._json_body()
            status, obj = self.server.app.post(unquote(path[len(API):]).strip("/"), body)
            self._json(status, obj)
        except (Exception, SystemExit) as e:
            self._studio_err(e)

    def do_PUT(self):
        ok, _ = self._gate()
        if not ok:
            return
        sp = urlsplit(self.path)
        try:
            route = unquote(sp.path)
            if route != API + "workbench/upload":               # the library takes files only through the app's native panel
                raise StudioError(404, "not found", "not_found")
            self._upload(parse_qs(sp.query).get("name", [""])[0], workbench=True)
        except (Exception, SystemExit) as e:
            self._studio_err(e)

    def do_PATCH(self):
        ok, _ = self._gate()
        if not ok:
            return
        path = urlsplit(self.path).path
        try:
            if not path.startswith(WORKSPACE):
                raise StudioError(404, "not found", "not_found")
            return self._workspace("PATCH", unquote(path[len(WORKSPACE):]).strip("/"), self._json_body())
        except (Exception, SystemExit) as e:
            self._studio_err(e)

    # ---- the Workspace board
    def _workspace(self, verb: str, route: str, body: dict | None) -> None:
        ws = self.server.workspace
        if verb == "GET" and route == "board":
            seq = self.server.bus.latest                     # read first: a write after this arrives as a board event
            return self._json(200, {**ws.board(), "seq": seq})
        if verb == "GET" and route == "sync":
            return self._json(200, ws.sync_status())
        if verb == "GET" and route == "people":                     # Settings: each project's members, in words
            return self._json(200, ws.people())
        if verb == "GET" and route == "approvals":                  # the approval sheet's trims, and how many posts wait (the rail's count)
            app = self.server.app
            waiting = 0
            try:
                from ..state import load_queue_item           # only the publishing computer has the queue: count what can be approved here
                posts = app.get("posts", {"status": ["in_review"]})[1].get("posts", [])
                waiting = sum(1 for p in posts for v in p.get("variants") or []
                              if v.get("status") == "in_review" and v.get("queue") and load_queue_item(app.studio.sp, v) is not None)
            except Exception:                                    # no studio posts here: the sheet says so when it opens
                pass
            eps = self.server.episodes
            return self._json(200, {"trims": ws.trims(), "posts_waiting": waiting, "native": app.native_authority is not None,
                                    "content": _quiet(eps.content), "episode_posts": _quiet(eps.publish)})
        if route.startswith(("content", "publish", "jobs", "file/")):
            return self._episodes(verb, route, body or {})
        if verb == "GET" and route == "sources":                    # the Sources view: footage, graphics, music and the library
            app = self.server.app
            library = (lambda: app.get("library", {"limit": ["2000"]})[1]) if not app.fixtures else None
            return self._json(200, ws.sources(library=library))
        if route.startswith("artifact/") and verb in ("GET", "PATCH"):
            key = route[len("artifact/"):]
            return self._json(200, ws.detail(key) if verb == "GET" else ws.patch(key, body))
        if route == "artifact" and verb == "POST":
            return self._json(201, ws.create(body))
        if route.startswith("room/") and verb == "GET":            # the Room: a stage chosen on two computers, who else is here
            return self._json(200, ws.room_info(route[len("room/"):]))
        if route == "presence" and verb == "POST":                  # the Room is open: presence, at most about once a minute
            return self._json(200, ws.touch((body or {}).get("artifact"), (body or {}).get("view")))
        raise StudioError(404, "not found", "not_found")

    # ---- the episode projects' review work and jobs (studio/ui/episodes.py)
    def _episodes(self, verb: str, route: str, body: dict) -> None:
        eps = self.server.episodes
        bits = route.split("/")
        try:
            if verb == "GET" and bits[0] == "file" and len(bits) >= 3:       # a content output or a queued file, never raw footage
                _, ep = eps._one(bits[1])
                return self._file("/".join(bits[2:]), ep=ep)
            if verb == "GET" and route == "content":
                return self._json(200, {"items": eps.content()})
            # A reject over the room token is recorded as Claude's (the MCP tools hold that token, so it cannot show it is the
            # person); the person's own reject goes through the app (aveNative.rejectContent / rejectEpisodePost). Approving is only
            # the app's native sheet, and an episode post's approval stays `ave publish approve` in a terminal.
            if verb == "POST" and route == "content/reject":
                return self._json(200, eps.content_decide(_s(body, "project"), str(body.get("doc") or "main"), _s(body, "id"), "reject",
                                                          note=str(body.get("note") or ""), by="claude"))
            if verb == "GET" and route == "publish":
                return self._json(200, {"items": eps.publish()})
            if verb == "GET" and len(bits) == 3 and bits[0] == "publish":
                return self._json(200, eps.publish_item(bits[1], bits[2]))
            if verb == "POST" and route == "publish/reject":
                return self._json(200, eps.publish_reject(_s(body, "project"), _s(body, "id"), str(body.get("reason") or ""), by="claude"))
            if verb == "GET" and route == "jobs":
                return self._json(200, eps.jobs())
            if verb == "GET" and len(bits) == 3 and bits[0] == "jobs":
                return self._json(200, eps.job_tail(bits[1], bits[2]))
            if verb == "POST" and route == "jobs":
                args = body.get("args") or {}
                if not isinstance(args, dict):
                    raise EpisodeError(400, "args must be an object")
                return self._json(201, eps.job_start(_s(body, "project"), _s(body, "cmd"), args))
            if verb == "POST" and len(bits) == 4 and bits[0] == "jobs" and bits[3] == "cancel":
                return self._json(200, eps.job_cancel(bits[1], bits[2]))
        except EpisodeError as e:
            raise StudioError(e.status, str(e), "bad_request" if e.status == 400 else "conflict" if e.status == 409 else "not_found") from None
        raise StudioError(404, "not found", "not_found")

    # ---- the live Room
    # The director's edits from the Room. On a project a newer AVE saved they are refused (409), like every other write.
    ROOM_EDITS = ("keep", "back", "flag", "selects/decide", "selects/nudge")

    def _room_post(self, route: str, body: dict) -> None:
        room = self.server.room
        if route in self.ROOM_EDITS or route.startswith("selects/"):
            st = room.stage
            artifact = body.get("artifact") if isinstance(body, dict) and body.get("artifact") else (st.artifact if st else None)
            why = self.server.update_needed_for(artifact)
            if why:
                raise StudioError(409, why, "read_only")
        try:
            if route == "show":
                out = room.show(**body)
            elif route == "highlight":
                out = room.highlight(body.get("highlights"))
            elif route == "offer":
                out = self.server.offer(body.get("version"), artifact=body.get("artifact"), compare_to=body.get("compare_to"),
                                        span=body.get("span"), summary=body.get("summary"))
            elif route == "keep":
                st = room.stage
                if st is not None and st.version and self.server.project_root:
                    try:
                        wait = self.server._not_ready(room_cut.find_episode(self.server.project_root, st.artifact), st.version)
                    except room_cut.NotFound:
                        wait = None
                    if wait:
                        raise StudioError(409, wait, "not_ready")    # a version cannot be kept before it has arrived
                return self._json(200, {"click": room.keep(body.get("version"), body.get("via", "button")), "stage": asdict(room.stage)})
            elif route == "back":
                return self._json(200, {"click": room.back(body.get("version"), body.get("via", "button")), "stage": asdict(room.stage)})
            elif route == "render":
                return self._json(202, self.server.render_full(body.get("artifact"), body.get("version")))
            elif route == "flag":
                return self._json(200, room.add_flag(body))
            elif route == "review":
                return self._json(202, self.server.review_copy(body.get("artifact"), body.get("key")))
            elif route == "view":
                out = room.set_view(body.get("view"))
            elif route in ("selects/decide", "selects/nudge"):    # over the room token: Claude's (see selects_write)
                return self._json(200, self.server.selects_write(route, body))
            elif route == "read":
                return self._json(200, room.read(body.get("ack") if isinstance(body.get("ack"), str) else None))
            else:
                raise StudioError(404, "not found", "not_found")
        except room_cut.NotFound as e:
            raise StudioError(404, str(e), "not_found") from None
        except LookupError as e:
            raise StudioError(409, str(e), "conflict") from None
        except OverflowError as e:
            raise StudioError(429, str(e), "full") from None
        except (TypeError, ValueError) as e:                 # TypeError: an unknown or missing stage field
            raise StudioError(400, str(e), "bad_request") from None
        self._json(200, {"stage": asdict(out)})

    def _room_get(self, route: str, qs: dict) -> None:
        one = lambda k: (qs.get(k) or [None])[0]
        root = self.server.project_root
        try:
            if route == "state":
                snap = self.server.room.snapshot()
                return self._json(200, {**snap, "reviews": self.server.reviews(),
                                        "update_needed": self.server.update_needed_for((snap.get("stage") or {}).get("artifact"))})
            if route == "layout":
                return self._json(200, {**self.server.layout_of(one("artifact")), "update_needed": self.server.update_needed_for(one("artifact"))})
            if root is None:
                raise StudioError(404, "This app has no project folder open", "not_found")
            if route == "cut":
                ep = room_cut.find_episode(root, one("artifact"))
                ready = self.server.ready(ep, one("version"))
                if ready["status"] != "ready":                   # never open a version before its files arrive
                    return self._json(200, {"artifact": one("artifact"), "version": one("version"), "readiness": ready,
                                            "title": room_cut.title_of(ep, one("artifact"))})
                return self._json(200, {**room_cut.cut_info(ep, one("artifact"), one("version")), "readiness": ready,
                                        "update_needed": self.server.update_needed_for(one("artifact"))})
            if route in ("script", "selects"):
                ep = room_cut.find_episode(root, one("artifact"))
                info = (room_review.script_info if route == "script" else room_review.selects_info)(ep, one("artifact"))
                self.server.served(one("artifact"), route, info["stamp"])
                return self._json(200, {**info, "update_needed": self.server.update_needed_for(one("artifact"))})
            bits = route.split("/")
            if len(bits) == 3 and bits[0] in ("source", "thumb"):
                ep = room_cut.find_episode(root, unquote(bits[1]))
                f = (room_review.source_path if bits[0] == "source" else room_review.thumb)(ep, unquote(bits[2]))
                return self._file(ep.rel(f), ep=ep)
            if len(bits) == 4 and bits[0] == "media":
                ep = room_cut.find_episode(root, unquote(bits[1]))
                f = room_cut.media_path(ep, bits[2], unquote(bits[3]))
                return self._file(ep.rel(f), ep=ep)
        except room_cut.NotFound as e:
            raise StudioError(404, str(e), "not_found") from None
        except ValueError as e:
            raise StudioError(400, str(e), "bad_request") from None
        raise StudioError(404, "not found", "not_found")

    def _room_stream(self) -> None:
        """Server-sent events `{seq, type, data}`: the backlog after Last-Event-ID, then live, with a heartbeat comment every 30 s.
        A Last-Event-ID from the future (the app restarted) or from before the 500 events kept gets a `resync` event first, carrying the
        stage; a future one then replays from the start. At most MAX_STREAMS at once."""
        if not self.server.streams.acquire(blocking=False):
            self.close_connection = True
            return self._err(503, f"at most {MAX_STREAMS} streams are open")
        in_room = parse_qs(urlsplit(self.path).query).get("room", [""])[0] == "1"     # the Room's stream, not the Workspace's
        with self.server._followers_lock:
            self.server.followers += 1
            self.server.room_followers += in_room
        try:
            raw = self.headers.get("Last-Event-ID") or parse_qs(urlsplit(self.path).query).get("last_event_id", ["0"])[0]
            last = int(raw) if raw.isascii() and raw.isdigit() and len(raw) < 16 else 0
            room = self.server.room
            bus = room.bus
            hello = []
            if last > bus.latest:
                last, hello = 0, [("future", bus.latest)]
            elif last and bus.oldest > last + 1:
                hello = [("window", bus.oldest)]
            self.close_connection = True
            self.send_response(200)
            for k, v in (("Content-Type", "text/event-stream; charset=utf-8"), ("Cache-Control", "no-store"), ("Connection", "close"),
                         ("X-Content-Type-Options", "nosniff")):
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(b": open\n\n")
            for why, seq in hello:                           # no `id:` line: a resync must not move the follower's cursor
                st = room.stage
                data = {"seq": 0, "type": "resync", "data": {"reason": why, "latest": seq, "stage": asdict(st) if st else None}}
                self.wfile.write(f"data: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()
            sent = time.monotonic()
            while not bus.closed:
                events = bus.since(last) or bus.wait(last, min(HEARTBEAT_S, 1.0))
                for e in events:
                    self.wfile.write(f"id: {e['seq']}\ndata: {json.dumps(e, ensure_ascii=False, default=str)}\n\n".encode())
                    last = e["seq"]
                if not events:
                    if self._peer_gone():
                        break
                    if time.monotonic() - sent >= HEARTBEAT_S:
                        self.wfile.write(b": heartbeat\n\n")
                    else:
                        continue
                self.wfile.flush()
                sent = time.monotonic()
        except OSError:                                      # the follower went away
            pass
        finally:
            with self.server._followers_lock:
                self.server.followers -= 1
                self.server.room_followers -= in_room
            self.server.streams.release()

    def _peer_gone(self) -> bool:
        """True once the follower closed its end (a quiet stream would otherwise hold its slot until the next heartbeat)."""
        try:
            if not select.select([self.connection], [], [], 0)[0]:
                return False
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True

    # ---- bodies
    def _content_length(self, limit: int, what: str) -> int | None:
        raw = self.headers.get("Content-Length")
        if raw is None:
            return None
        try:
            n = int(raw)
        except ValueError:
            raise StudioError(400, "bad Content-Length", "bad_request") from None
        if n < 0 or n > limit:
            self.close_connection = True                    # the body (if any) is never read
            raise StudioError(413, f"{what} must be at most {limit} bytes", "body.too_large")
        return n

    def _json_body(self) -> dict:
        n = self._content_length(MAX_BODY, "the request body") or 0
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            raise StudioError(400, "body must be JSON", "bad_request") from None
        if not isinstance(body, dict):
            raise StudioError(400, "body must be a JSON object", "bad_request")
        return body

    def _upload(self, name: str, *, workbench: bool = False) -> None:
        app = self.server.app
        if not name or len(name) > 200 or any(c in name for c in "/\\\x00") or name.startswith(".") or any(ord(c) < 32 for c in name):
            raise StudioError(400, "'name' must be a plain file name", "bad_request")
        n = self._content_length(app.max_upload, "an upload")
        if n is None:
            self.close_connection = True
            raise StudioError(411, "Content-Length is required for an upload", "bad_request")
        if n == 0:
            raise StudioError(400, "empty upload", "bad_request")
        tmp_dir = app.ep.work / "uploads"
        from ..desk import _safe_path
        _safe_path(tmp_dir, app.ep.root)
        if workbench and app.fixtures:
            raise StudioError(400, "Sample mode is read-only", "invalid")
        tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".upload-", dir=tmp_dir)
        h, got = hashlib.sha256(), 0
        try:
            with os.fdopen(fd, "wb") as out:
                while got < n:
                    chunk = self.rfile.read(min(65536, n - got))
                    if not chunk:
                        break
                    out.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
            if got != n:
                self.close_connection = True
                raise StudioError(400, f"the connection ended after {got} of {n} bytes", "bad_request")
            result = (app.workbench.upload if workbench else app.upload)(Path(tmp), name, n, h.hexdigest())
        finally:
            Path(tmp).unlink(missing_ok=True)
        self._json(201, result)                                # after the cleanup: a client that sees the reply sees a clean uploads folder

    # ---- files
    def _studio_file(self, rel: str, *, episode_artifact: bool = False) -> None:
        if not rel or "\x00" in rel or rel.startswith(("/", "\\")):
            raise ApiError(404, "not found")
        work_prefix = self.server.app.ep.rel(self.server.app.ep.work)
        # Check both normalized components and the resolved target. The shared
        # resolver collapses dots/separators and permits in-root symlink aliases.
        ep = self.server.app.ep
        protected_reviews=ep.ledger_dir/'manual_reviews'
        release_root=ep.work/'production'/'episode-release'
        package_members=None
        release_candidate=(ep.root/rel).resolve()
        if release_candidate==release_root.resolve() or release_root.resolve() in release_candidate.parents:
            if not episode_artifact:
                from ..episode_release import downloadable
                package_members=downloadable(self.server.app.studio,release_candidate,index_policy=True,export=parse_qs(urlsplit(self.path).query).get('export')==['1'])
        resolved=(ep.root/rel).resolve()
        for folder in ('episode-previews','episode-renders'):
            protected=(ep.work/'production'/folder).resolve()
            if (resolved==protected or protected in resolved.parents) and not episode_artifact:
                raise ApiError(403,'Use the registered episode artifact endpoint')
        candidate=(ep.root/rel).resolve()
        if candidate==protected_reviews.resolve() or protected_reviews.resolve() in candidate.parents:
            raise ApiError(403,'Historical review attachments are not downloadable')
        managed = ep.work / "production" / "episodes"
        for candidate in (ep.root / Path(rel), (ep.root / rel).resolve()):
            try:
                parts = candidate.relative_to(managed.resolve()).parts
            except ValueError:
                continue
            if len(parts) >= 2 and parts[1] == "raw":
                raise ApiError(403, "Managed episode raw folders are not served. Use a registered source preview.")
        manual_prefix = work_prefix + "/manual_exports/"
        if not rel.startswith(SERVED_PREFIXES) and not rel.startswith(manual_prefix):
            raise ApiError(404, "not found")
        if rel.startswith(manual_prefix):
            from ..manual_export import _safe
            parts = rel[len(manual_prefix):].split("/")
            if len(parts) > 1:
                folder = _safe(self.server.app.ep.work / "manual_exports" / parts[0], self.server.app.ep.root)
                marker = _safe(folder / ".incomplete", self.server.app.ep.root)
                if marker.exists():
                    raise ApiError(404, "manual export is incomplete")
        if self.server.app.fixtures and rel.startswith("library/assets/") and not (self.server.app.ep.root / rel).is_file():
            return self._send(200, _placeholder(rel), "image/jpeg")        # fixtures mode: the committed JSON names files nobody has
        try:
            if package_members is not None:
                # This exact parent-verified, escaped package page needs its
                # own origin for relative media. Scripts remain prohibited;
                # arbitrary uploaded HTML retains the opaque file sandbox.
                origin='http://'+self.headers['Host'] # Host was validated before routing.
                members=' '.join(origin+member for member in package_members) or "'none'"
                return self._file(rel,csp=f"default-src 'none'; img-src {members}; media-src {members}; style-src 'unsafe-inline'; sandbox allow-same-origin; base-uri 'none'; form-action 'none'; object-src 'none'; script-src 'none'")
            return self._file(rel)
        except ApiError as e:
            raise StudioError(e.status, e.message, "not_found" if e.status == 404 else "forbidden") from None


def _s(body: dict, key: str) -> str:
    v = body.get(key)
    if not isinstance(v, str) or not v.strip() or len(v) > 200:
        raise EpisodeError(400, f"{key} is required")
    return v.strip()


def _quiet(fn) -> list:
    """A list the approval summary shows; a broken episode file leaves its part empty instead of failing the whole summary."""
    try:
        return fn()
    except Exception:
        return []


def _placeholder(seed: str) -> bytes:
    """A deterministic stand-in picture for a fixture asset (fixtures mode only; nothing real is behind it)."""
    import io

    from PIL import Image, ImageDraw
    n = sum(map(ord, seed))
    im = Image.new("RGB", (480, 600))
    d = ImageDraw.Draw(im)
    for y in range(600):
        t = y / 600
        d.line([(0, y), (480, y)], fill=(int(226 - 110 * t - n % 25), int(190 - 100 * t), int(146 - 70 * t + n % 20)))
    d.ellipse([90, 300, 390, 470], fill=(170, 108, 60))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80)
    return buf.getvalue()


# ---------------------------------------------------------------------------------------------------- CLI

def run_cli(a) -> int:
    """`ave studio ui [DIR] [--port N] [--open] [--by NAME] [--fixtures]`: the app (the Room, the Workspace, Settings) in a browser.
    Refuses without a human terminal unless `--fixtures` (which serves committed sample data and changes nothing). No HTTP route
    approves anything, so the PIN is never printed here: only the app's native sheets show it."""
    from ..project import find_studio
    fixtures = bool(getattr(a, "fixtures", False))
    if fixtures:
        studio = FixtureStudio()
    else:
        humanpresence.require_human("`ave studio ui` (it acts as you in the studio)", "ave studio ui" + (f" {a.dir}" if getattr(a, "dir", None) else ""))
        sp = find_studio(getattr(a, "dir", None))
        sp.require()
        from ..cli import OPEN_KW, real_studio
        studio = real_studio(sp, **OPEN_KW)          # every hook registered (calendar, library, B-roll, ...) and the adapters/host attached
    srv = StudioServer(studio, port=a.port, by=a.by, fixtures=fixtures)
    url = srv.app_url()
    where = "FIXTURES (sample data, nothing is real)" if fixtures else str(studio.sp.root)
    print(f"ave studio ui for {where}\n  open: {url}\n  bound to 127.0.0.1:{srv.port} only; the token above is required on every request.", flush=True)
    print("  In a browser nothing can be approved: approving a post or a trim runs in AVE Studio's own sheets (ave desktop),\n"
          "  which check that it is you. More tools (Production, Media tools, Feed planner, Post editor): " + srv.tools_url(), flush=True)
    print("  Ctrl-C stops the server.", flush=True)
    if a.open:
        import webbrowser
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
    finally:
        srv.server_close()
    return 0
