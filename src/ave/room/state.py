"""What is on stage, plus the inbox of director flags and recent clicks that `room_read` hands to Claude."""

from __future__ import annotations

import secrets
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Literal, get_args

from .bus import Bus

View = Literal["cut", "script", "selects", "layout"]
VIEWS = get_args(View)
RECENT_CLICKS = 20
MAX_FLAGS = 200
MAX_HISTORY = 50
MAX_SUMMARY = 300
FLAG_KEYS = {"artifact", "start", "end", "note", "view", "text", "clip"}
MAX_FLAG_TEXT = 2000
RECENT_READ = 50                                         # flags Claude has read, kept so a reloaded page still says "Claude has it"
VIA = ("button", "chat")                                 # how a Keep or Go back reached the app: the director's click, or Claude relaying their words
TIME_BASES = ("cut", "transcript")                       # cut seconds (the Cut view), transcript seconds (Script and Selects)


def base_of(view: str | None) -> str:
    return "transcript" if view in ("script", "selects") else "cut"


@dataclass
class Stage:
    artifact: str
    view: View
    version: str | None = None
    position_s: float = 0.0
    compare_to: str | None = None
    highlights: list[dict] = field(default_factory=list)
    time_base: str = ""                                  # what position_s counts: cut seconds or transcript seconds (from the view)

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
        if not self.time_base:
            self.time_base = base_of(self.view)
        if self.time_base not in TIME_BASES:
            raise ValueError(f"'time_base' must be one of {', '.join(TIME_BASES)}")


def clean_flag(flag: dict) -> dict:
    """A director's flag as `{artifact, start, end, note}`, plus where it was made: `view` (cut: times are cut seconds; script and
    selects: transcript seconds), `text` (the words flagged) and `clip` (the select it is about). Anything else is refused."""
    if not isinstance(flag, dict) or set(flag) - FLAG_KEYS or not {"artifact", "start", "end"} <= set(flag):
        raise ValueError("a flag is {artifact, start, end, note, view, text, clip}")
    a, s0, e, n = flag["artifact"], flag["start"], flag["end"], flag.get("note", "")
    if not isinstance(a, str) or not 0 < len(a) <= 200:
        raise ValueError("'artifact' must be a string of 1 to 200 characters")
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not 0 <= x <= 1e7 or x != x for x in (s0, e)) or e < s0:
        raise ValueError("'start' and 'end' must be seconds, 0 or more, with end not before start")
    if not isinstance(n, str) or len(n) > 500:
        raise ValueError("'note' must be a string of at most 500 characters")
    out = {"artifact": a, "start": float(s0), "end": float(e), "note": n, "time_base": base_of(flag.get("view"))}
    if flag.get("view") is not None:
        if flag["view"] not in VIEWS:
            raise ValueError(f"'view' must be one of {', '.join(VIEWS)}")
        out["view"] = flag["view"]
    if flag.get("text") is not None:
        if not isinstance(flag["text"], str) or len(flag["text"]) > MAX_FLAG_TEXT:
            raise ValueError(f"'text' must be a string of at most {MAX_FLAG_TEXT} characters")
        out["text"] = flag["text"]
    if flag.get("clip") is not None:
        if not isinstance(flag["clip"], str) or not 0 < len(flag["clip"]) <= 100:
            raise ValueError("'clip' must be a string of 1 to 100 characters")
        out["clip"] = flag["clip"]
    return out


def _via(via) -> str:
    if via not in VIA:
        raise ValueError(f"'via' must be one of {', '.join(VIA)}")
    return via


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
        self._read_flags: deque[dict] = deque(maxlen=RECENT_READ)
        self.offer: dict | None = None                      # the open offer: {artifact, version, compare_to, span, summary}
        self.preview: dict | None = None                    # the latest preview event's data for the open offer
        self._history: list[str] = []                       # earlier versions of the artifact on stage, oldest first
        self._kept: dict | None = None                      # the keep click for the version on stage, until the stage or offer changes
        self._lock = threading.Lock()

    def _set_stage(self, stage: Stage, cause: str | None = None) -> None:
        """Make `stage` current (caller holds the lock). A new artifact starts a new history; a new version of the same one remembers
        the version it replaces, so Go back has somewhere to go even without an offer."""
        old = self.stage
        if old is None or old.artifact != stage.artifact:
            self._history = []
            self.offer = self.preview = None
        elif old.version and old.version != stage.version:
            self._history = [v for v in self._history if v != old.version][-(MAX_HISTORY - 1):] + [old.version]
        if self.offer and self.offer["version"] != stage.version:
            self.offer = self.preview = None
        if old is None or (old.artifact, old.version) != (stage.artifact, stage.version):
            self._kept = None
        self.stage = stage
        self.bus.publish("stage", {**asdict(stage), **({"cause": cause} if cause else {})})

    def show(self, **fields) -> Stage:
        """Claude puts something on stage. The time base follows the view unless given; the event says it came from a show, so an open
        Script or Selects reloads what it shows."""
        stage = Stage(**fields)
        with self._lock:
            self._set_stage(stage, cause="show")
        return stage

    def highlight(self, highlights: list[dict]) -> Stage:
        with self._lock:
            if self.stage is None:
                raise LookupError("nothing is on stage: call room_show first")
            stage = Stage(**{**asdict(self.stage), "highlights": highlights})
            self._set_stage(stage)
        return stage

    def set_view(self, view: str) -> Stage:
        """The director switched the Room's view by hand: the stage keeps everything else, and Claude sees the view in room_read."""
        with self._lock:
            if self.stage is None:
                raise LookupError("nothing is on stage")
            if self.stage.view == view:
                return self.stage
            stage = Stage(**{**asdict(self.stage), "view": view})
            self.stage = stage                               # not _set_stage: a view change keeps the offer and the kept click
            self.bus.publish("stage", asdict(stage))
        return stage

    def offer_version(self, artifact: str, version: str, compare_to: str | None = None, *, span: list | tuple | None = None,
                      summary: str | None = None) -> Stage:
        """Put a new version on stage next to the one it replaces; the app's Keep and Go back answer it. For an artifact that is not
        on stage the stage moves to it (view `cut`, no highlights); for the one on stage its view and position stay. With no
        `compare_to`, the version on stage is the one being replaced. `span` (cut seconds) and `summary` (a line for the director)
        are optional and travel with the `offer` event."""
        if not isinstance(version, str) or not version:
            raise ValueError("'version' must be a non-empty string")
        if summary is not None and (not isinstance(summary, str) or len(summary) > MAX_SUMMARY):
            raise ValueError(f"'summary' must be a string of at most {MAX_SUMMARY} characters")
        if span is not None:
            if (not isinstance(span, (list, tuple)) or len(span) != 2
                    or any(isinstance(x, bool) or not isinstance(x, (int, float)) or x != x or not 0 <= x <= 1e7 for x in span)
                    or span[1] < span[0]):
                raise ValueError("'span' must be [start, end] in seconds, with end not before start")
            span = [float(span[0]), float(span[1])]
        with self._lock:
            same = self.stage is not None and self.stage.artifact == artifact
            keep = asdict(self.stage) if same else {"artifact": artifact, "view": "cut"}
            if compare_to is None and same and self.stage.version:      # re-offering the version on stage keeps what it replaces
                compare_to = self.stage.version if self.stage.version != version else self.stage.compare_to
            stage = Stage(**{**keep, "version": version, "compare_to": compare_to})
            self._set_stage(stage)
            self.offer = {"artifact": artifact, "version": version, "compare_to": compare_to, "span": span, "summary": summary}
            self._kept = None
            self.preview = None
            self.bus.publish("offer", dict(self.offer))
        return stage

    def set_preview(self, data: dict) -> int | None:
        """Publish how the preview of an offered version went ({version, status, ...}); kept only while that offer is open."""
        with self._lock:
            if self.offer is None or self.offer["version"] != data.get("version") or self.offer["artifact"] != data.get("artifact"):
                return None                                  # the director already moved on: a late preview is not news
            self.preview = dict(data)
            return self.bus.publish("preview", dict(data))

    def keep(self, version: str | None = None, via: str = "button") -> dict:
        """The director keeps the version on stage: recorded as a click for Claude, the offer closes, the stage stays. `via` says
        whether they clicked it or said it in chat. Keeping the version already kept, with no new offer since, changes nothing and
        returns the first click marked `already_kept`, so a relayed Keep after a click never records the choice twice."""
        via = _via(via)
        with self._lock:
            st = self.stage
            if st is None or not st.version:
                raise LookupError("no version is on stage to keep")
            if version is not None and version != st.version:
                raise ValueError(f"{version} is not the version on stage ({st.version})")
            if self.offer is None and self._kept and self._kept["version"] == st.version and self._kept["artifact"] == st.artifact:
                return {**self._kept, "already_kept": True}
            click = {"action": "keep", "artifact": st.artifact, "version": st.version, "via": via, "at": time.time()}
            self._clicks.append(click)
            self.bus.publish("click", click)
            self.offer = self.preview = None
            self._set_stage(Stage(**{**asdict(st), "compare_to": None}))
            self._kept = click
            return click

    def back(self, version: str, via: str = "button") -> dict:
        """Go back from `version` (which must be the one on stage, so a click on a stale screen cannot undo a newer version): the
        version it replaced (the offer's, else the last one shown) returns to the stage."""
        if not isinstance(version, str) or not version:
            raise ValueError("'version' must name the version on stage")
        via = _via(via)
        with self._lock:
            st = self.stage
            if st is None or not st.version:
                raise LookupError("no version is on stage")
            if version != st.version:
                raise ValueError(f"{version} is not the version on stage ({st.version})")
            to = st.compare_to or next((v for v in reversed(self._history) if v != st.version), None)
            if not to:
                raise LookupError("there is no earlier version to go back to")
            click = {"action": "back", "artifact": st.artifact, "version": to, "from": st.version, "via": via, "at": time.time()}
            self._clicks.append(click)
            self.bus.publish("click", click)
            self.offer = self.preview = self._kept = None
            self.stage = Stage(**{**asdict(st), "version": to, "compare_to": None})
            self._history = [v for v in self._history if v not in (to, st.version)]
            self.bus.publish("stage", asdict(self.stage))
            return click

    def snapshot(self) -> dict:
        """Everything the app needs to draw the Room, with the seq it is current to: follow the stream from `seq`."""
        with self._lock:
            return {"seq": self.bus.latest, "stage": asdict(self.stage) if self.stage else None,
                    "offer": dict(self.offer) if self.offer else None, "flags_waiting": len(self._flags), "preview": dict(self.preview) if self.preview else None,
                    "flags": [dict(f) for f in self._flags], "flags_read": [dict(f) for f in self._read_flags],
                    "can_go_back": bool(self.stage and self.stage.version and (self.stage.compare_to or
                                        any(v != self.stage.version for v in self._history)))}

    def flag(self, flag: dict) -> int:
        """A director's flag from the UI: queued for `read` until acknowledged, and shown on the stream."""
        return self.add_flag(flag)["seq"]

    def add_flag(self, flag: dict) -> dict:
        """As `flag`, returning the flag's id with the event's seq: `{id, seq}`."""
        flag = clean_flag(flag)
        with self._lock:
            if len(self._flags) >= MAX_FLAGS:
                raise OverflowError(f"{MAX_FLAGS} flags are waiting unread")
            self._flag_id += 1
            flag = {"id": self._flag_id, **flag}
            self._flags.append(flag)
            return {"id": flag["id"], "seq": self.bus.publish("flag", flag)}

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
                done = [f for f in self._flags if f["id"] <= int(n)]
                self._flags = [f for f in self._flags if f["id"] > int(n)]
                if done:                                    # the app shows the director that Claude has read them
                    self._read_flags.extend(done)
                    self.bus.publish("read", {"flags_waiting": len(self._flags), "through": int(n), "ids": [f["id"] for f in done]})
            return {"stage": asdict(self.stage) if self.stage else None, "flags": list(self._flags), "clicks": list(self._clicks),
                    "offer": self._offer_status(), "ack": f"{self._epoch}:{self._flag_id}"}

    def _offer_status(self) -> dict | None:
        """The open offer and how its preview stands, for Claude (caller holds the lock). `preview.status` is `ok` only once the
        preview has rendered: `pending` (not started), `rendering`, `too_large` (the change is wider than the quick preview takes),
        `unchanged`, or `failed` with a `reason`."""
        if self.offer is None:
            return None
        p = self.preview or {}
        status = {"ready": "ok", None: "pending"}.get(p.get("status"), p.get("status"))
        return {"artifact": self.offer["artifact"], "version": self.offer["version"], "compare_to": self.offer["compare_to"],
                "summary": self.offer["summary"],
                "preview": {"status": status, "reason": p.get("message"), "span": p.get("span", self.offer["span"]),
                            "level_approximate": bool(p.get("level_approximate", False))}}
