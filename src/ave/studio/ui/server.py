"""`ave studio ui`: the studio surface of the localhost server (spec 7.1).

`StudioHandler` subclasses the `ave ui` Handler, so the loopback-only bind, the per-run token (`?t=`, cookie, `X-AVE-Token`), the Host
and Origin checks, the CSP headers and the ranged `/files/` serving are inherited unchanged. What is new:

  * `/api/studio/*` (api.py), JSON errors as `{error, code, latest?}`;
  * a static resolver for a directory tree (`studio/ui/static/`, owned by the UI workstreams) with `style.css` shared with `ave ui`;
  * `PUT /api/studio/library/upload?name=` streamed to a temp file under `work/uploads/` with its own cap (default 512 MB);
  * the Origin check also covers PUT, and `/files/` only serves what the UI needs (library assets, exports, work);
  * the approval PIN, printed ONLY on the human's terminal by `run_cli` (never in a response, a file or a log).

The change feed is polled (`GET /api/studio/changes?cursor=`, about every 3 s); there is no SSE.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import secrets
import sys
import tempfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from ... import humanpresence
from ...ui import server as ui_server
from ...ui.api import ApiError
from ...ui.server import MAX_BODY, Handler
from ..service import FixtureStudio, Studio, open_studio
from .api import MAX_UPLOAD, StudioApp, StudioError, map_error

STATIC = Path(__file__).parent / "static"
API = "/api/studio/"
UPLOAD_ROUTE = "library/upload"
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

    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    def server_close(self) -> None:
        super().server_close()
        self.app.close()


class StudioHandler(Handler):
    server: StudioServer
    server_version = "ave-studio"
    timeout = 120

    # ---- static tree (E5 hook)
    def static_resolver(self, path: str) -> Path | None:
        if path in ("/", "/index.html"):
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
        roots = [STATIC] + ([ui_server.STATIC] if rel == "style.css" else [])      # the shared tokens from `ave ui`
        for root in roots:
            f = (root / rel).resolve()
            try:
                f.relative_to(root.resolve())
            except ValueError:
                continue
            if f.is_file() and f.suffix.lower() in STATIC_EXT:
                return f
        return None

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
            if in_query and not path.startswith("/api/"):
                return self._redirect(path)
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
            if route not in (API + UPLOAD_ROUTE, API + "workbench/upload"):
                raise StudioError(404, "not found", "not_found")
            self._upload(parse_qs(sp.query).get("name", [""])[0], workbench=route.endswith("workbench/upload"))
        except (Exception, SystemExit) as e:
            self._studio_err(e)

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
            self._json(201, (app.workbench.upload if workbench else app.upload)(Path(tmp), name, n, h.hexdigest()))
        finally:
            Path(tmp).unlink(missing_ok=True)

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
    """`ave studio ui [DIR] [--port N] [--open] [--by NAME] [--fixtures]`. Refuses without a human terminal unless `--fixtures`
    (which serves committed sample data and changes nothing). The PIN is printed here, to the terminal, and nowhere else."""
    from ..project import find_studio
    fixtures = bool(getattr(a, "fixtures", False))
    if fixtures:
        studio = FixtureStudio()
    else:
        humanpresence.require_human("`ave studio ui` (it can approve posts)", "ave studio ui" + (f" {a.dir}" if getattr(a, "dir", None) else ""))
        sp = find_studio(getattr(a, "dir", None))
        sp.require()
        from ..cli import OPEN_KW, real_studio
        studio = real_studio(sp, **OPEN_KW)          # every hook registered (calendar, library, B-roll, ...) and the adapters/host attached
    srv = StudioServer(studio, port=a.port, by=a.by, fixtures=fixtures)
    url = srv.url()
    where = "FIXTURES (sample data, nothing is real)" if fixtures else str(studio.sp.root)
    print(f"ave studio ui for {where}\n  open: {url}\n  bound to 127.0.0.1:{srv.port} only; the token above is required on every request.", flush=True)
    if fixtures:
        print(f"  fixtures mode: approvals change nothing; the PIN is {srv.app.pin.reveal()}", flush=True)
    else:
        print(f"  approval PIN (this terminal only, never stored): {srv.app.pin.reveal()}\n"
              "  Approving a post needs this PIN AND having viewed the exact media in the UI. Keep both to yourself.", flush=True)
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
