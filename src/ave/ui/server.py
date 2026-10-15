"""The HTTP layer: 127.0.0.1 only, a per-run token on every request, Host/Origin checks, JSON API, ranged file serving.

Auth: the token may arrive as `?t=` (the URL printed on the terminal), the `ave_ui` cookie (set on that first visit, then the URL
is redirected to a clean one) or an `X-AVE-Token` header. Every request is checked, static pages and files included. A Host header
other than 127.0.0.1/localhost on OUR port is refused (DNS rebinding), and a POST with a foreign Origin is refused.
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import re
import secrets
import socket
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from ..episode import Episode
from .api import ApiError, App, resolve_inside

STATIC = Path(__file__).parent / "static"
STATIC_FILES = {"/": "index.html", "/index.html": "index.html", "/static/app.js": "app.js", "/static/style.css": "style.css"}
MAX_BODY = 1_000_000
COOKIE = "ave_ui"
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")
PAGE_CSP = "default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
FILE_CSP = "default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'; sandbox"


class UiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, ep: Episode, port: int = 0, token: str | None = None, by: str | None = None, publishers=None):
        self.token = token or secrets.token_urlsafe(24)
        self.app = App(ep, by=by, publishers=publishers)
        super().__init__(("127.0.0.1", port), Handler)         # never anything but loopback
        self.port = self.server_address[1]

    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"


class Handler(BaseHTTPRequestHandler):
    server: UiServer
    protocol_version = "HTTP/1.1"
    server_version = "ave-ui"

    def log_message(self, fmt, *args):      # quiet: tokens appear in URLs
        pass

    # ---- plumbing
    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None, csp: str = PAGE_CSP) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", csp)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8")

    def _err(self, status: int, msg: str) -> None:
        self._json(status, {"error": msg})

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        return host in (f"127.0.0.1:{self.server.port}", f"localhost:{self.server.port}")

    def _token_from(self) -> tuple[str | None, bool]:
        q = parse_qs(urlsplit(self.path).query).get("t", [None])[0]
        if q:
            return q, True
        h = self.headers.get("X-AVE-Token")
        if h:
            return h, False
        c = SimpleCookie(self.headers.get("Cookie") or "")
        return (c[COOKIE].value if COOKIE in c else None), False

    def _gate(self) -> tuple[bool, bool]:
        """(allowed, token_was_in_query). Sends the refusal itself."""
        if not self._host_ok():
            self._err(403, "bad Host header")
            return False, False
        tok, in_query = self._token_from()
        if not tok or not hmac.compare_digest(tok.encode(), self.server.token.encode()):
            self._err(401, "missing or wrong token: open the URL printed by `ave ui`")
            return False, False
        if self.command == "POST":
            origin = self.headers.get("Origin")
            if origin and origin not in (f"http://127.0.0.1:{self.server.port}", f"http://localhost:{self.server.port}"):
                self._err(403, "bad Origin")
                return False, False
        return True, in_query

    # ---- verbs
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        ok, in_query = self._gate()
        if not ok:
            return
        path = urlsplit(self.path).path
        try:
            if in_query and not path.startswith("/api/"):       # first visit: park the token in a cookie, drop it from the URL
                return self._redirect(path)
            if path in STATIC_FILES:
                f = STATIC / STATIC_FILES[path]
                return self._send(200, f.read_bytes(), (mimetypes.guess_type(f.name)[0] or "text/plain") + "; charset=utf-8")
            if path.startswith("/files/"):
                return self._file(unquote(path[len("/files/"):]))
            if path.startswith("/api/"):
                return self._json(200, self._route_get(unquote(path[len("/api/"):]).strip("/"), parse_qs(urlsplit(self.path).query)))
            self._err(404, "not found")
        except ApiError as e:
            self._err(e.status, e.message)
        except Exception as e:                                  # a broken episode file must not kill the server
            self._err(500, f"{type(e).__name__}: {e}")

    def do_POST(self):
        ok, _ = self._gate()
        if not ok:
            return
        path = urlsplit(self.path).path
        try:
            if not path.startswith("/api/"):
                raise ApiError(404, "not found")
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ApiError(413, "body too large")
            raw = self.rfile.read(n) if n else b"{}"
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                raise ApiError(400, "body must be JSON") from None
            if not isinstance(body, dict):
                raise ApiError(400, "body must be a JSON object")
            self._json(200, self._route_post(unquote(path[len("/api/"):]).strip("/"), body))
        except ApiError as e:
            self._err(e.status, e.message)
        except Exception as e:
            self._err(500, f"{type(e).__name__}: {e}")

    # ---- routes
    def _route_get(self, r: str, qs: dict | None = None):
        a = self.server.app
        if r == "dashboard":
            return a.dashboard()
        if r == "clips":
            return a.clips(qs)
        if r.startswith("clips/"):
            return a.clip_detail(r.split("/", 1)[1])
        if r == "trim":
            return a.trim()
        if r == "content":
            return a.content()
        if r == "publish":
            return a.publish()
        if r.startswith("publish/"):
            return a.publish_item(r.split("/", 1)[1])
        if r == "jobs":
            return a.job_catalog()
        if r.startswith("jobs/"):
            return a.job(r.split("/", 1)[1])
        raise ApiError(404, "no such endpoint")

    def _route_post(self, r: str, body: dict):
        a = self.server.app
        parts = r.split("/")
        if len(parts) == 3 and parts[0] == "clips" and parts[2] == "decide":
            return a.decide_clip(parts[1], body)
        if r == "trim/set":
            return a.trim_set(body)
        if r == "trim/clear":
            return a.trim_clear(body)
        if r == "content/decide":
            return a.content_decide(body)
        if len(parts) == 3 and parts[0] == "publish" and parts[2] == "approve":
            return a.publish_approve(parts[1], body)
        if len(parts) == 3 and parts[0] == "publish" and parts[2] == "reject":
            return a.publish_reject(parts[1], body)
        if r == "jobs":
            return a.job_start(body)
        raise ApiError(404, "no such endpoint")

    # ---- files
    def _redirect(self, path: str) -> None:
        tok = parse_qs(urlsplit(self.path).query)["t"][0]
        self.send_response(303)
        self.send_header("Set-Cookie", f"{COOKIE}={tok}; Path=/; HttpOnly; SameSite=Strict")
        self.send_header("Location", path)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _file(self, rel: str) -> None:
        p = resolve_inside(self.server.app.ep, rel)
        size = p.stat().st_size
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng:
            m = _RANGE.match(rng.strip())
            if not m or (not m.group(1) and not m.group(2)):
                return self._range_unsatisfiable(size)
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:                                               # suffix: the last N bytes
                start = max(size - int(m.group(2)), 0)
            if start >= size or start > end:
                return self._range_unsatisfiable(size)
            status = 206
        length = end - start + 1 if size else 0
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", FILE_CSP)
        self.end_headers()
        if self.command == "HEAD" or not length:
            return
        try:
            with p.open("rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass                                                # the player seeked away

    def _range_unsatisfiable(self, size: int) -> None:
        self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
        self.send_header("Content-Range", f"bytes */{size}")
        self.send_header("Content-Length", "0")
        self.end_headers()


def port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0
