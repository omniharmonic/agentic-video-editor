"""A small client for Syncthing's REST API over its GUI Unix socket.

The GUI listens only on `<home>/gui.sock` (0600, inside the 0700 home), so no other local user can reach it, and it has a
random GUI user and bcrypt password so nothing gets in without the API key (a CSRF cookie from `GET /` is not enough).
Every request carries `X-API-Key`. No proxy can see the traffic: it never leaves the socket.
"""

from __future__ import annotations

import http.client
import json
import socket
import urllib.parse
from pathlib import Path
from typing import Any


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect(self._path)
        except OSError:
            s.close()
            raise
        self.sock = s


class Api:
    def __init__(self, socket_path: str | Path, key: str, timeout: float = 10.0):
        self.socket_path, self.key, self.timeout = str(socket_path), key, timeout

    @property
    def base(self) -> str:
        return f"unix://{self.socket_path}"

    def request(self, method: str, path: str, params: dict | None = None, body: Any = None,
                timeout: float | None = None, key: bool = True) -> Any:
        url = path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = None if body is None else json.dumps(body).encode()
        headers = {"X-API-Key": self.key} if key else {}
        if data is not None:
            headers["Content-Type"] = "application/json"
        conn = _UnixConnection(self.socket_path, timeout or self.timeout)
        try:
            conn.request(method, url, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
        except (OSError, http.client.HTTPException) as exc:
            raise ApiError(f"{method} {path}: {exc}") from None
        finally:
            conn.close()
        if resp.status >= 400:
            raise ApiError(f"{method} {path}: HTTP {resp.status} {raw.decode(errors='replace').strip()[:300]}", resp.status)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return raw.decode(errors="replace")

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params or None)

    def put(self, path: str, body: Any) -> Any:
        return self.request("PUT", path, body=body)

    def patch(self, path: str, body: Any) -> Any:
        return self.request("PATCH", path, body=body)

    def post(self, path: str, body: Any = None, **params: Any) -> Any:
        return self.request("POST", path, params or None, body=body if body is not None else {})

    def delete(self, path: str) -> Any:
        return self.request("DELETE", path)

    # ------------------------------------------------------------------ helpers
    def ping(self) -> bool:
        try:
            return (self.get("/rest/system/ping") or {}).get("ping") == "pong"
        except ApiError:
            return False

    def events(self, since: int = 0, timeout_s: int = 30, types: list[str] | None = None) -> list[dict]:
        """Long-poll `/rest/events`: returns at the first event after `since`, or an empty list after `timeout_s`."""
        params: dict[str, Any] = {"since": since, "timeout": timeout_s}
        if types:
            params["events"] = ",".join(types)
        try:
            return self.request("GET", "/rest/events", params, timeout=timeout_s + 10) or []
        except ApiError as exc:
            if "timed out" in str(exc):
                return []
            raise


def q(segment: str) -> str:
    """One URL path segment, quoted."""
    return urllib.parse.quote(segment, safe="")
