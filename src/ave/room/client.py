"""How the MCP tools reach the running app: loopback HTTP with the token in `<root>/.ave/local/room.json`.

A closed room (no room.json, or nothing listening) is a normal answer, not a failure: the call is kept in `room-queue.jsonl` and the next
server start replays it. A room that is there but slow or odd is NOT closed: it is reported as an error and nothing is queued.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

REL = Path(".ave") / "local" / "room.json"
QUEUE_REL = Path(".ave") / "local" / "room-queue.jsonl"
QUEUE_MAX = 200
TIMEOUT = 5.0
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))      # the token never goes to a proxy


def room_file(root: str | Path) -> Path:
    return Path(root) / REL


def queue_file(root: str | Path) -> Path:
    return Path(root) / QUEUE_REL


def _local_dir(root: str | Path) -> Path:
    d = room_file(root).parent
    d.parent.mkdir(mode=0o700, exist_ok=True)
    d.mkdir(mode=0o700, exist_ok=True)
    return d


def write_room_file(root: str | Path, port: int, token: str) -> Path:
    p = _local_dir(root) / REL.name
    fd = os.open(p, os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as f:
        os.fchmod(f.fileno(), 0o600)                     # before the token is written, whatever mode an older file had
        f.truncate(0)
        f.write(json.dumps({"port": port, "token": token}))
    return p


def remove_room_file(root: str | Path, token: str) -> None:
    """Unlink room.json only if it is still this server's (another instance may have replaced it)."""
    p = room_file(root)
    try:
        if json.loads(p.read_text()).get("token") == token:
            p.unlink(missing_ok=True)
    except (OSError, ValueError, AttributeError):
        pass


def take_queue(root: str | Path) -> list[dict]:
    """The queued calls in order, then the file emptied. Unreadable lines are skipped."""
    p = queue_file(root)
    try:
        lines = p.read_text().splitlines()
        p.write_text("")
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            if isinstance(c := json.loads(line), dict):
                out.append(c)
        except ValueError:
            pass
    return out


class RoomClient:
    def __init__(self, project_root: str | Path):
        self.root = Path(project_root)
        self.ack: str | None = None                      # the last read this client got, confirmed to the app on the next read

    def _queue(self, tool: str, args: dict) -> dict:
        try:
            p = queue_file(self.root)
            _local_dir(self.root)
            try:
                lines = p.read_text().splitlines()
            except OSError:
                lines = []
            lines = (lines + [json.dumps({"tool": tool, "args": args})])[-QUEUE_MAX:]
            tmp = p.with_name(p.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write("\n".join(lines) + "\n")
            os.replace(tmp, p)
            return {"room": "closed", "queued": True}
        except OSError:
            return {"room": "closed", "queued": False}

    def _call(self, route: str, body: dict) -> dict | None:
        """The app's answer, or None when it cannot be reached (no room.json, connection refused)."""
        try:
            info = json.loads(room_file(self.root).read_text())
            port, token = int(info["port"]), str(info["token"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/room/{route}", method="POST", data=json.dumps(body).encode(),
                                     headers={"X-AVE-Token": token, "Content-Type": "application/json"})
        try:
            with _OPENER.open(req, timeout=TIMEOUT) as r:
                return {"room": "open", **json.loads(r.read() or b"{}")}
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error", "")
            except ValueError:
                msg = ""
            return {"room": "open", "error": msg or f"HTTP {e.code}", "status": e.code}
        except urllib.error.URLError as e:
            if isinstance(e.reason, ConnectionRefusedError):
                return None
            return {"room": "open", "error": f"no usable reply: {e.reason}"}        # a timeout is not a closed room
        except (OSError, ValueError) as e:               # includes a timeout mid-reply and a reply that is not JSON
            return {"room": "open", "error": f"no usable reply: {type(e).__name__}"}

    def _write(self, tool: str, route: str, body: dict) -> dict:
        got = self._call(route, body)
        return got if got is not None else self._queue(tool, body)

    def show(self, **stage) -> dict:
        return self._write("room_show", "show", stage)

    def highlight(self, highlights: list[dict]) -> dict:
        return self._write("room_highlight", "highlight", {"highlights": highlights})

    def offer_version(self, artifact: str, version: str, compare_to: str | None = None, summary: str | None = None) -> dict:
        body = {"artifact": artifact, "version": version, "compare_to": compare_to}
        if summary is not None:
            body["summary"] = summary
        return self._write("room_offer_version", "offer", body)

    def keep(self, version: str) -> dict:
        """Keep said in chat: the same click the button records, marked `via: chat`. Never queued: with the app closed there is no offer to answer."""
        return self._answer("keep", version)

    def back(self, version: str) -> dict:
        """Go back said in chat, as the button does it, marked `via: chat`. Never queued, for the same reason as `keep`."""
        return self._answer("back", version)

    def _answer(self, route: str, version: str) -> dict:
        got = self._call(route, {"version": version, "via": "chat"})
        return got if got is not None else {"room": "closed", "queued": False}

    def read(self) -> dict:
        """Nothing to keep when closed. Flags are confirmed to the app by the next read, once this reply was actually received."""
        got = self._call("read", {"ack": self.ack})
        if got is None:
            return {"room": "closed"}
        if "ack" in got:
            self.ack = got["ack"]
        return got


def waiting_flags(start: str | Path) -> list[dict] | None:
    """The director's open flags in the running app's Room, read without acknowledging them (room_read does that), for the app
    whose room.json sits at `start` or one of its parents; None when no app is open. Each flag carries `root`, the folder of the
    app it came from."""
    here = Path(start).resolve()
    for root in (here, *here.parents):
        try:
            info = json.loads(room_file(root).read_text())
            port, token = int(info["port"]), str(info["token"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/room/state", headers={"X-AVE-Token": token})
        try:
            with _OPENER.open(req, timeout=TIMEOUT) as r:
                snap = json.loads(r.read() or b"{}")
        except (OSError, ValueError):
            return None
        return [{**f, "root": str(root)} for f in snap.get("flags") or []]
    return None
