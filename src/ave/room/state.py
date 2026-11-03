"""What is on stage, plus the inbox of director flags and recent clicks that `room_read` hands to Claude."""

from __future__ import annotations

import secrets
import threading
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Literal, get_args

from .bus import Bus

View = Literal["cut", "script", "selects", "layout"]
VIEWS = get_args(View)
RECENT_CLICKS = 20
MAX_FLAGS = 200
FLAG_KEYS = {"artifact", "start", "end", "note"}


@dataclass
class Stage:
    artifact: str
    view: View
    version: str | None = None
    position_s: float = 0.0
    compare_to: str | None = None
    highlights: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.artifact, str) or not self.artifact:
            raise ValueError("'artifact' must be a non-empty string")
        if self.view not in VIEWS:
            raise ValueError(f"'view' must be one of {', '.join(VIEWS)}")
        if isinstance(self.position_s, bool) or not isinstance(self.position_s, (int, float)) or self.position_s < 0:
            raise ValueError("'position_s' must be a number of seconds, 0 or more")
        self.position_s = float(self.position_s)
        for k in ("version", "compare_to"):
            if getattr(self, k) is not None and not isinstance(getattr(self, k), str):
                raise ValueError(f"'{k}' must be a string or null")
        if not isinstance(self.highlights, list) or not all(isinstance(h, dict) for h in self.highlights):
            raise ValueError("'highlights' must be a list of objects")


def clean_flag(flag: dict) -> dict:
    """A director's flag as `{artifact, start, end, note}`; anything else is refused."""
    if not isinstance(flag, dict) or set(flag) - FLAG_KEYS or not {"artifact", "start", "end"} <= set(flag):
        raise ValueError("a flag is {artifact, start, end, note}")
    a, s0, e, n = flag["artifact"], flag["start"], flag["end"], flag.get("note", "")
    if not isinstance(a, str) or not 0 < len(a) <= 200:
        raise ValueError("'artifact' must be a string of 1 to 200 characters")
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not 0 <= x <= 1e7 or x != x for x in (s0, e)) or e < s0:
        raise ValueError("'start' and 'end' must be seconds, 0 or more, with end not before start")
    if not isinstance(n, str) or len(n) > 500:
        raise ValueError("'note' must be a string of at most 500 characters")
    return {"artifact": a, "start": float(s0), "end": float(e), "note": n}


class Room:
    """Thread-safe: HTTP handlers write, the stream follows `bus`. Events are published under the same lock that changes the state,
    so the stream and the stage never disagree about the order of changes."""

    def __init__(self) -> None:
        self.bus = Bus()
        self.stage: Stage | None = None
        self._flags: list[dict] = []
        self._flag_id = 0
        self._epoch = secrets.token_hex(4)                   # a client's ack from an earlier run of the app means nothing here
        self._clicks: deque[dict] = deque(maxlen=RECENT_CLICKS)
        self._lock = threading.Lock()

    def show(self, **fields) -> Stage:
        stage = Stage(**fields)
        with self._lock:
            self.stage = stage
            self.bus.publish("stage", asdict(stage))
        return stage

    def highlight(self, highlights: list[dict]) -> Stage:
        with self._lock:
            if self.stage is None:
                raise LookupError("nothing is on stage: call room_show first")
            stage = Stage(**{**asdict(self.stage), "highlights": highlights})
            self.stage = stage
            self.bus.publish("stage", asdict(stage))
        return stage

    def offer_version(self, artifact: str, version: str, compare_to: str | None = None) -> Stage:
        """Put a new version on stage next to the one it replaces; the app's Keep and Go back answer it. For an artifact that is not
        on stage the stage moves to it (view `cut`, no highlights); for the one on stage its view and position stay."""
        with self._lock:
            keep = asdict(self.stage) if self.stage and self.stage.artifact == artifact else {"artifact": artifact, "view": "cut"}
            stage = Stage(**{**keep, "version": version, "compare_to": compare_to})
            self.stage = stage
            self.bus.publish("stage", asdict(stage))
            self.bus.publish("offer", {"artifact": artifact, "version": version, "compare_to": compare_to})
        return stage

    def flag(self, flag: dict) -> int:
        """A director's flag from the UI: queued for `read` until acknowledged, and shown on the stream."""
        flag = clean_flag(flag)
        with self._lock:
            if len(self._flags) >= MAX_FLAGS:
                raise OverflowError(f"{MAX_FLAGS} flags are waiting unread")
            self._flag_id += 1
            flag = {"id": self._flag_id, **flag}
            self._flags.append(flag)
            return self.bus.publish("flag", flag)

    def click(self, click: dict) -> int:
        with self._lock:
            self._clicks.append(click)
            return self.bus.publish("click", click)

    def read(self, ack: str | None = None) -> dict:
        """The stage, the unread flags and the recent clicks. Flags stay until a later read acknowledges them with the `ack` this
        returned, so a reply that never arrived loses nothing."""
        with self._lock:
            epoch, _, n = (ack or "").partition(":")
            if epoch == self._epoch and n.isascii() and n.isdigit():
                self._flags = [f for f in self._flags if f["id"] > int(n)]
            return {"stage": asdict(self.stage) if self.stage else None, "flags": list(self._flags), "clicks": list(self._clicks),
                    "ack": f"{self._epoch}:{self._flag_id}"}
