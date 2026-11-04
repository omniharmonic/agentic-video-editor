"""The loopback HTTP base for the studio app: 127.0.0.1 only, a per-run token on every request, Host/Origin checks, JSON helpers and
ranged file serving that never serves raw footage. Moved here from the retired `ave ui` (its server and api modules), unchanged.

Auth: the token may arrive as `?t=` (the URL the app opens), the `ave_ui` cookie (set on that first visit, then the URL is redirected
to a clean one) or an `X-AVE-Token` header. Every request is checked, static pages and files included. A Host header other than
127.0.0.1/localhost on OUR port is refused (DNS rebinding), and a POST with a foreign Origin is refused.
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import os
import re
import socket
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ...episode import Episode

MAX_BODY = 1_000_000
COOKIE = "ave_ui"
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")
PAGE_CSP = "default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
FILE_CSP = "default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'; sandbox"


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _in_raw(ep: Episode, p: Path) -> bool:
    """True when `p` is inside a raw footage dir. Case-insensitive (APFS is by default: `/files/RAW/x` is the same file as `/files/raw/x`)
    and identity-based (`samefile` on every ancestor), so neither case nor a symlinked or hard-linked directory gets around the block."""
    ps = os.path.normcase(str(p)).casefold()
    for raw in ep.raw_dirs:
        rs = os.path.normcase(str(raw.resolve())).casefold()
        if ps == rs or ps.startswith(rs.rstrip(os.sep) + os.sep):
            return True
        if raw.exists():
            for parent in p.parents:
                try:
                    if os.path.samefile(parent, raw):
                        return True
                except OSError:
                    continue
    return False


def resolve_inside(ep: Episode, rel: str) -> Path:
    """A file under the episode root, or ApiError. Refuses traversal, symlink escapes, dot-files/dirs and the raw footage dirs."""
    if not rel or "\x00" in rel or rel.startswith(("/", "\\")):
        raise ApiError(404, "not found")
    parts = Path(rel).parts
    if any(p in ("..", "") or p.startswith(".") for p in parts):
        raise ApiError(404, "not found")
    root = ep.root.resolve()
    p = (root / rel).resolve()
    try:
        p.relative_to(root)
    except ValueError:
        raise ApiError(404, "not found") from None
    if _in_raw(ep, p):
        raise ApiError(403, "raw footage is not served")
    if not p.is_file():
        raise ApiError(404, "not found")
    return p


def _servable(ep: Episode, p: Path) -> str | None:
    """`/files/...` URL for a path if the file server would serve it, else None."""
    try:
        rel = p.resolve().relative_to(ep.root.resolve()).as_posix()
        resolve_inside(ep, rel)
    except (ValueError, ApiError, OSError):
        return None
    return "/files/" + rel



class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ave-studio"

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
            self._err(401, "missing or wrong token: open AVE Studio again")
            return False, False
        if self.command == "POST":
            origin = self.headers.get("Origin")
            if origin and origin not in (f"http://127.0.0.1:{self.server.port}", f"http://localhost:{self.server.port}"):
                self._err(403, "bad Origin")
                return False, False
        return True, in_query

    def static_resolver(self, path: str) -> Path | None:
        """The file to serve for a static URL path, or None. The studio's handler serves its trees by overriding this."""
        return None

    # ---- verbs
    def do_HEAD(self):
        self.do_GET()

    # ---- files
    def _redirect(self, path: str) -> None:
        tok = parse_qs(urlsplit(self.path).query)["t"][0]
        self.send_response(303)
        self.send_header("Set-Cookie", f"{COOKIE}={tok}; Path=/; HttpOnly; SameSite=Strict")
        self.send_header("Location", path)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _file(self, rel: str, *, csp: str = FILE_CSP, ep: Episode | None = None) -> None:
        p = resolve_inside(ep or self.server.app.ep, rel)
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
        self.send_header("Content-Security-Policy", csp)
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
