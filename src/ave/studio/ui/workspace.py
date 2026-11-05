"""The Workspace: every artifact across the projects on this computer, read from their logs and written as the person.

Projects are the folders this computer's workspace can see that carry `.ave/project.json`: the studio folder itself, the folders
directly inside it, and the episode folders registered with the studio (best effort; a registry that cannot be read only hides
those). Each project is reduced on its own and cached by its log fingerprint, so polling for change is a few `stat` calls.

Attribution follows the channel. Only what comes through the desktop app's own native bridge (`via="app"`, from
desktop_approvals) is written as `actor: "human"` with this computer's person; and a confirm of what Claude reported, or an answer
that changes authority (a settings conflict), also needs the app's per-act device-owner check. Anything that arrives over HTTP
with only the room token is Claude's: Claude's MCP holds that token, so the request cannot show it is the person. It is written
as `actor: "claude"`, and a sign-off or a decision answer carries `relayed_by: "claude"`, so the director confirms or undoes it
on the board exactly like one Claude relayed through the MCP tools (a browser outside the app reads that way too). Stage moves
by hand are sign-offs (or reopens, going back) without a quote, and Out is refused: it needs the publishing approval.

Routes (served by server.py):
  GET   /api/workspace/board               every project's rows, the person, and recent activity
  GET   /api/workspace/artifact/<key>      one artifact: fields, versions, updates, decisions, activity
  POST  /api/workspace/artifact            a new artifact at Plan, made by the person
  PATCH /api/workspace/artifact/<key>      fields, stage, an update, confirm or undo what Claude relayed, answer a decision
  GET   /api/workspace/sync                each project's sync state in plain words (live as `sync` events on the stream)
  GET   /api/workspace/sources             each project's source files and the studio library, with who has each one, in words
  GET   /api/workspace/people              Settings' People: each project's members in words (no device ids)
  GET   /api/workspace/approvals           the approval sheet's trims: Claude's draft trims waiting on the director
  GET   /api/workspace/room/<ref>          for the Room: a stage changed on two computers, and who else is working there
  POST  /api/workspace/presence            {artifact, view}: the Room is open there (presence.set, at most about once a minute)

Conflicts and presence: a row's `conflict` says "Maren and Imani both changed Refine" with one choice per side, and picking one
is a stage change by hand that saw both (a new log event). A row's `presence` lists the other computers' people working on
it ("Imani is in Cut"), dropped once it runs out at `clock()`. Each project's `notices` carry what the conflict watcher
(syncthing/conflicts.py: its worker runs after arrivals and answers, and a sweep every CONFLICT_S) found: copies outside the
settings zone, and settings decisions on an artifact that has no row.

Readiness (project/readiness.py): each row carries `ready` ("arriving" or "damaged" when its current version cannot be opened
yet, else None) and `ready_text`. The log watcher looks again every WATCH_S, and at once when the background hasher finishes a
file, and publishes a `readiness` event with every version of the projects whose readiness changed. Hashing never runs on
the watcher, a request or the stream.

Posts: the studio's own change log is looked at every tick (a `stat`), and a `posts` event with its new cursor is published
when it grows, so the Workspace's Calendar, Previews and Ideas read again without polling anything themselves.

An artifact's `key` is `<project_id>:<artifact_id>`, since artifact ids are only unique within a project.
"""

from __future__ import annotations

import fcntl
import math
import os
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from ...project import board
from ...project import presence as _presence
from ...project import readiness as _ready
from ...project import hlc as _hlc
from ...project.adopt import folder_kind
from ...project.model import Project, ProjectError
from ...project.reducer import STAGES, reduce
from .api import StudioError

SYNC_S = 3.0                  # how often each project's sync state is looked at while someone follows the stream
SYNC_CALL_S = 2.0             # the longest any one Syncthing call in a look may take
WATCH_S = 2.0                 # how often the logs are looked at for writes from other processes and other computers
REGISTRY_S = 30.0             # how often the studio's registered episode folders are read again
CONFLICT_S = 600.0            # the conflict watcher's settings sweep (its worker also runs after every arrival and answer)
ACTIVITY = 40                 # events in the Home feed
FIELDS = {"title", "due", "owner", "order", "archived"}
PATCH_OPS = ("fields", "stage", "note", "confirm", "undo", "resolve", "reopen_decision")
KINDS = ("episode", "short", "post", "thumbnail", "newsletter", "season", "artifact")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TRIM_DECISION = re.compile(r"^Trim for the draft \(([A-Za-z0-9_-]{1,40})\)")   # the summary start.py and `ave trim propose` write
OUT_REFUSED = ("Out needs the publish approval, which you give in the approval sheet. Moving the stage by hand cannot "
               "publish anything.")


def _ms(hlc: str | None) -> int | None:
    try:
        return _hlc.parse(hlc).ms if hlc else None
    except ValueError:
        return None


def _reports(d: dict) -> str:
    """How a relayed act opens on the board: the director's words when Claude gave them, else that it gave none."""
    q = d.get("quote")
    return f"Claude reports you said: “{q}”, so" if isinstance(q, str) and q else "Claude did this without your words, so"


def _who(e: dict) -> str:
    if e.get("actor") == "scheduler":
        return "AVE"                    # the app on its own (the conflict watcher), never a person
    return "Claude" if e.get("actor") == "claude" else (e.get("person") or "Someone")


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


class _Cached:
    __slots__ = ("fingerprint", "data")

    def __init__(self, fingerprint: str, data: dict):
        self.fingerprint, self.data = fingerprint, data


class Workspace:
    def __init__(self, root: Path | None, *, roots: Callable[[], list[Path]] | None = None, sp=None,
                 publish: Callable[[str, dict], Any] | None = None, watching: Callable[[], bool] | None = None,
                 sync: Callable[[], Any] | None = None, clock: Callable[[], int] | None = None):
        self.root = Path(root) if root else None
        self.clock = clock or _presence.now_ms   # ms; presence runs out against it (tests pass a fake one)
        self._conflicts: dict[str, str] = {}      # project id -> what the conflict watcher last reported there
        self._conflicts_off: Callable[[], None] | None = None
        self._worked: dict[str, str] = {}         # project id -> the newest own event already turned into presence
        self._conflicts_at = 0.0
        self.sync = sync                  # -> this computer's SyncManager, or None (a browser, or sync never started)
        self._sync_seen: str | None = None
        self._sync_last: dict | None = None
        self._sync_kick = threading.Event()
        self._sync_thread: threading.Thread | None = None
        self.sp = sp
        self._roots_fn = roots
        self._publish = publish
        self._watching = watching or (lambda: True)
        self._opened: dict[Path, tuple[int, Project]] = {}
        self._cache: dict[str, _Cached] = {}
        self._known: dict[str, str] = {}
        self._registry: tuple[float, list[Path]] = (0.0, [])
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._states: dict[str, tuple[str, Any]] = {}      # project id -> (log fingerprint, reduced State), for readiness looks
        self._ready: dict[str, dict] = {}                  # project id -> readiness.details() of the last look
        self._ready_kick = threading.Event()
        self._ready_off: Callable[[], None] | None = None
        self._posts_size: int | None = None                # the studio change log's size at the last look (posts events)

    # ---------------------------------------------------------------- finding projects
    def _registered(self) -> list[Path]:
        if self.sp is None:
            return []
        at, roots = self._registry
        if time.monotonic() - at < REGISTRY_S:
            return roots
        found: list[Path] = []
        try:
            from ..episode_registry import records
            for row in records(self.sp).values():
                hint = row.get("root_hint")
                if isinstance(hint, str) and not row.get("revoked"):
                    p = Path(hint) if Path(hint).is_absolute() else self.sp.root / hint
                    found.append(p)
        except Exception:  # noqa: BLE001 - a registry that cannot be read hides those folders, never the board
            found = []
        self._registry = (time.monotonic(), found)
        return found

    def roots(self) -> list[Path]:
        if self._roots_fn is not None:
            return [Path(p) for p in self._roots_fn()]
        out: list[Path] = []
        if self.root is None:
            return out
        cands = [self.root]
        try:
            cands += sorted(d for d in self.root.iterdir() if d.is_dir() and not d.name.startswith("."))
        except OSError:
            pass
        cands += self._registered()
        seen = set()
        for c in cands:
            try:
                r = c.resolve()
            except OSError:
                continue
            if r in seen or not (r / ".ave" / "project.json").is_file():
                continue
            seen.add(r)
            out.append(r)
        return out

    def projects(self) -> list[Project]:
        """The projects, opened once and kept until their project.json changes (a write never reopens every project)."""
        out = []
        for r in self.roots():
            try:
                mt = (r / ".ave" / "project.json").stat().st_mtime_ns
                with self._lock:
                    hit = self._opened.get(r)
                if hit is None or hit[0] != mt:
                    hit = (mt, Project.open(r))
                    with self._lock:
                        self._opened[r] = hit
                out.append(hit[1])
            except (ProjectError, OSError):
                continue
        return out

    def _project(self, pid: str) -> Project:
        for p in self.projects():
            if p.project_id == pid:
                return p
        raise StudioError(404, "That project is not in this workspace any more.", "not_found")

    # ---------------------------------------------------------------- reading
    def _read(self, project: Project) -> dict:
        """Rows and activity for one project, cached by its log fingerprint."""
        fp = project.index().fingerprint()
        with self._lock:
            c = self._cache.get(project.project_id)
        if c is not None and c.fingerprint == fp:
            return self._with_update(project, c.data)
        events = project.read_events()
        st = reduce(events)
        data = self._shape(project, st, events)
        with self._lock:
            self._cache[project.project_id] = _Cached(fp, data)   # only the watcher moves `_known`, so a read never hides a write
        return self._with_update(project, data)

    @staticmethod
    def _with_update(project: Project, data: dict) -> dict:
        """A project saved by a newer AVE Studio says "update needed" and is read-only here (project.model.update_needed)."""
        needed = getattr(project, "update_needed", None)
        if not needed:
            return dict(data, update_needed=None, read_only=False)
        return dict(data, update_needed=needed, read_only=True, warnings=[needed] + [w for w in data.get("warnings", []) if w != needed])

    def _shape(self, project: Project, st, events) -> dict:
        pid = project.project_id
        flags = board.needs_review(st)
        by_art: dict[str, list[dict]] = {}
        for e in events:
            if e.get("artifact") and e.get("op") != "presence.set":     # presence is not a change to the artifact
                by_art.setdefault(e["artifact"], []).append(e)
        confirmed = {(n["data"] or {}).get("confirms") for n in st.notes.values() if (n.get("data") or {}).get("confirms")}
        undone = {(e.get("data") or {}).get("undoes") for e in events if (e.get("data") or {}).get("undoes")}
        parent_of: dict[str, str] = {}
        for aid, a in st.artifacts.items():
            for link in a.links:
                if link["to"] in st.artifacts and link["to"] not in parent_of and link["to"] != aid:
                    parent_of[link["to"]] = aid
        members = [m.get("person") for m in project.members if m.get("person")]
        rows = []
        for aid, a in st.artifacts.items():
            if a.created is None or a.kind == "studio":
                continue
            evs = sorted(by_art.get(aid, []), key=lambda e: (e["hlc"], e["id"]))
            rows.append(self._row(project, st, a, evs, flags, confirmed, undone, parent_of))
        activity = [self._activity(pid, st, e) for e in events if e.get("op") != "presence.set"]
        activity = [x for x in activity if x]
        activity.sort(key=lambda x: x["hlc"], reverse=True)
        return {"id": pid, "title": project.title or project.root.name,
                "kind": folder_kind(project.root), "person": project.person, "members": members,
                "artifacts": rows, "activity": activity[:ACTIVITY], "warnings": st.warnings[:20]}

    def _relayed(self, a, evs, st, confirmed, undone) -> list[dict]:
        """What Claude reported the director said, still to be confirmed or undone here."""
        out = []
        later_human = None
        for e in reversed(evs):
            if e["op"] in ("stage.signed_off", "stage.reopened") and e.get("actor") == "human":
                later_human = e["hlc"]
                break
        for e in evs:
            d = e.get("data") or {}
            if d.get("relayed_by") != "claude" or e["id"] in confirmed or e["id"] in undone:
                continue
            if e["op"] in ("stage.signed_off", "stage.reopened") and d.get("settles"):
                if later_human and later_human > e["hlc"]:
                    continue
                out.append({"event": e["id"], "type": "settle", "quote": d.get("quote"), "from": d.get("from"), "to": d.get("to"),
                            "can_undo": a.stage == d.get("to") and d.get("from") != d.get("to"), "hlc": e["hlc"], "at": _ms(e["hlc"]),
                            "text": f"{_reports(d)} the stage changed on two computers is settled on {d.get('to')}."})
            elif e["op"] == "stage.signed_off":
                if later_human and later_human > e["hlc"]:
                    continue                      # the person moved the stage by hand since: that settles it
                out.append({"event": e["id"], "type": "sign_off", "quote": d.get("quote"), "from": d.get("from"), "to": d.get("to"),
                            "can_undo": a.stage == d.get("to"), "hlc": e["hlc"], "at": _ms(e["hlc"]),
                            "text": f"{_reports(d)} {d.get('from')} is signed off and it moved to {d.get('to')}."})
            elif e["op"] == "decision.resolved":
                dec = st.decisions.get(d.get("decision")) or {}
                res = dec.get("resolution") or {}
                if res.get("reopen") or (dec.get("history") or [{}])[-1].get("event") != e["id"]:
                    continue
                summary = (dec.get("data") or {}).get("summary") or "a decision"
                out.append({"event": e["id"], "type": "resolution", "decision": d.get("decision"), "quote": d.get("quote"),
                            "choice": d.get("choice"), "can_undo": True, "hlc": e["hlc"], "at": _ms(e["hlc"]),
                            "text": f"{_reports(d)} you chose “{d.get('choice')}” for: {summary}"})
        return out

    def _row(self, project, st, a, evs, flags, confirmed, undone, parent_of) -> dict:
        pid = project.project_id
        vs = sorted(a.versions.values(), key=lambda v: v["number"])
        latest = vs[-1] if vs else None
        decisions = board.open_decisions(st, a.id)
        relayed = self._relayed(a, evs, st, confirmed, undone)
        conflict = next((c for c in st.conflicts if c.artifact == a.id), None)
        tasks = [t for t in st.tasks.values() if t.get("artifact") == a.id]
        doing = [t for t in tasks if (t["fields"].get("status") or "todo") in ("doing", "blocked")]
        claude_is = a.fields.get("claude_is") if isinstance(a.fields.get("claude_is"), str) else None
        claude_state = "working" if claude_is else None
        if not claude_is and doing:
            t = max(doing, key=lambda t: max((h[-1]["hlc"] for h in t["history"].values()), default=""))
            title = t["fields"].get("title") or "a task"
            blocked = t["fields"].get("status") == "blocked"
            claude_is, claude_state = (f"Stuck on: {title}" if blocked else _cap(title)), ("blocked" if blocked else "working")
        creator = evs[0] if evs else {}
        owner = a.fields.get("owner")
        if not isinstance(owner, str) or not owner:
            owner = None if a.fields.get("owner") is None and "owner" in a.fields else _who(creator) if creator else None
        waiting = None
        if conflict or decisions or relayed:
            waiting = "you"
        elif claude_state == "working":
            waiting = "claude"
        last = evs[-1] if evs else None
        notes = [n for n in st.notes.values() if n.get("artifact") == a.id and not (n.get("data") or {}).get("confirms")]
        fl = flags.get(a.id)
        order = a.fields.get("order")
        return {
            "key": f"{pid}:{a.id}", "project": pid, "artifact": a.id, "name": board.name_of(a), "kind": a.kind,
            "post": a.fields.get("post") if isinstance(a.fields.get("post"), str) and a.fields.get("post") else None,
            "stage": a.stage, "owner": owner,
            "waiting": waiting,
            "decisions": [self._decision(st, d) for d in decisions],
            "relayed": relayed,
            "conflict": {"options": sorted({o["to"] for o in conflict.options}, key=STAGES.index),
                         **board.stage_conflict(st, conflict)} if conflict else None,
            "claude_is": claude_is, "claude_state": claude_state,
            "due": a.fields.get("due") if isinstance(a.fields.get("due"), str) and DATE_RE.match(a.fields.get("due") or "") else None,
            "version": _cap(board.version_name(latest["version"], latest["number"])) if latest else None,
            "version_id": latest["version"] if latest else None,
            "versions": len(vs),
            "needs_review": bool(fl),
            "review_reason": f"{board.name_of(st.artifacts.get(fl['because']))} was reopened after this was made" if fl else None,
            "parent": f"{pid}:{parent_of[a.id]}" if a.id in parent_of else None,
            "parts": [f"{pid}:{l['to']}" for l in a.links if l["to"] in st.artifacts and parent_of.get(l["to"]) == a.id],
            "updates": len(notes),
            "archived": bool(a.fields.get("archived")),
            "order": order if isinstance(order, (int, float)) and not isinstance(order, bool) else None,
            "room": self._room_ref(project, a.id),
            "updated": last["hlc"] if last else a.updated, "updated_at": _ms(last["hlc"]) if last else _ms(a.updated),
            "updated_by": {"name": _who(last), "actor": last.get("actor")} if last else None,
            "created_at": _ms(a.created),
        }

    @staticmethod
    def _decision(st, d: dict) -> dict:
        out = {k: d[k] for k in ("decision", "summary", "options", "chosen", "reason")}
        trim = TRIM_DECISION.match(d.get("summary") or "")
        if trim:                                     # Claude's draft trim: answering it here is not the director's trim; the sheet is
            out.update({"approval": "trim", "session": trim.group(1)})
        data = (st.decisions.get(d["decision"]) or {}).get("data") or {}
        if data.get("kind") == "setting":            # two people changed one setting: who made each value
            out.update({"kind": "setting", "file": data.get("file"), "whole": bool(data.get("whole")),
                        "file_who": data.get("file_who"), "copy_who": data.get("copy_who"), "texts": data.get("texts"),
                        "file_label": data.get("file_label"), "copy_label": data.get("copy_label"),
                        "people": data.get("people"), "forms": data.get("forms"),
                        "keys": [{k: x.get(k) for k in ("key", "label", "file", "copy")} for x in data.get("keys") or []
                                 if isinstance(x, dict)],
                        "values": [{k: v.get(k) for k in ("option", "person", "preview")} for v in data.get("values") or []
                                   if isinstance(v, dict)]})
        return out

    def _room_ref(self, project: Project, aid: str) -> str:
        """What the Room calls this artifact: an episode folder inside the studio by its folder name, anything else by its id."""
        if aid == "art_ep" and self.root is not None and project.root.resolve().parent == self.root.resolve():
            return project.root.name
        return aid

    def _activity(self, pid: str, st, e: dict) -> dict | None:
        text = _sentence(st, e)
        if not text:
            return None
        aid = e.get("artifact")
        a = st.artifacts.get(aid) if aid else None
        if a is not None and a.kind == "studio":
            aid, a = None, None
        return {"event": e["id"], "hlc": e["hlc"], "at": _ms(e["hlc"]), "who": _who(e), "actor": e.get("actor"), "text": text,
                "key": f"{pid}:{aid}" if a is not None and a.created else None, "name": board.name_of(a) if a else None,
                "project": pid}

    def board(self) -> dict:
        projects = [self._with_ready(p, self._read(p)) for p in self.projects()]
        people = [p["person"] for p in projects if p.get("person")]
        activity = sorted((x for p in projects for x in p["activity"]), key=lambda x: x["hlc"], reverse=True)[:ACTIVITY]
        return {"projects": [{k: v for k, v in p.items() if k != "activity"} for p in projects],
                "me": people[0] if people else None, "activity": activity, "now": int(time.time() * 1000),
                "out_refused": OUT_REFUSED}

    def detail(self, key: str) -> dict:
        project, aid = self._split(key)
        events = project.read_events()
        st = reduce(events)
        a = st.artifacts.get(aid)
        shaped = self._with_ready(project, self._read(project))
        ready = self._ready_of(project)
        row = next((r for r in shaped["artifacts"] if r["artifact"] == aid), None)
        if a is None or a.created is None or row is None:
            raise StudioError(404, "That artifact is not on the board any more.", "not_found")
        mine = sorted((e for e in events if e.get("artifact") == aid), key=lambda e: (e["hlc"], e["id"]))
        versions = [{"version": v["version"], "name": _cap(board.version_name(v["version"], v["number"])), "number": v["number"],
                     "by": "Claude" if v.get("actor") == "claude" else v.get("person"), "at": _ms(v["hlc"]),
                     "kept": v["version"] == a.kept, "ready": _not_ready(ready.get(v["version"]))}
                    for v in sorted(a.versions.values(), key=lambda v: v["number"], reverse=True)]
        notes = [{"note": n["id"], "text": (n.get("data") or {}).get("text") or "", "who": _who(n), "actor": n.get("actor"),
                  "at": _ms(n["hlc"])} for n in st.notes.values()
                 if n.get("artifact") == aid and not (n.get("data") or {}).get("confirms")]
        notes.sort(key=lambda n: n["at"] or 0, reverse=True)
        resolved = []
        for d in st.decisions.values():
            if d.get("artifact") != aid or board.decision_open(d) or "data" not in d:
                continue
            res = d.get("resolution") or {}
            resolved.append({"decision": d["id"], "summary": d["data"].get("summary"), "choice": res.get("choice"),
                             "quote": res.get("quote"), "relayed": res.get("relayed_by") == "claude",
                             "options": d["data"].get("options") or []})
        activity = [x for x in (self._activity(project.project_id, st, e) for e in reversed(mine)) if x]
        parts = [r for r in shaped["artifacts"] if r["parent"] == row["key"]]
        return {"row": row, "fields": {k: v for k, v in a.fields.items() if isinstance(v, (str, int, float, bool)) or v is None},
                "project": {"id": project.project_id, "title": project.title or project.root.name,
                            "members": [m.get("person") for m in project.members if m.get("person")], "person": project.person},
                "versions": versions, "updates": notes, "decisions_resolved": resolved, "activity": activity,
                "parts": [{k: r[k] for k in ("key", "name", "stage", "needs_review")} for r in parts]}

    # ---------------------------------------------------------------- readiness: never open a version before its files arrive
    def _state_of(self, project: Project):
        fp = project.index().fingerprint()
        with self._lock:
            hit = self._states.get(project.project_id)
        if hit is not None and hit[0] == fp:
            return hit[1]
        st = reduce(project.read_events())
        with self._lock:
            self._states[project.project_id] = (fp, st)
        return st

    def _ready_of(self, project: Project) -> dict:
        """The watcher's last look at one project's readiness while it is looking; otherwise a look now (stats and cached
        hashes only: an unknown hash is left to the background hasher)."""
        live = self._thread is not None and self._thread.is_alive() and self._watching()
        with self._lock:
            got = self._ready.get(project.project_id) if live else None
        if got is None:
            got = _ready.details(self._state_of(project), project.root)
            with self._lock:
                self._ready.setdefault(project.project_id, got)
        return got

    def _with_ready(self, project: Project, data: dict) -> dict:
        """The cached rows with this moment's readiness of the version each row shows (its latest; a file can arrive with no
        log write)."""
        ready = self._ready_of(project)
        here = self._present(project)
        rows = []
        for r in data["artifacts"]:
            d = _not_ready(ready.get(r["version_id"])) if r.get("version_id") else None
            rows.append({**r, "ready": d["status"] if d else None, "ready_text": d["text"] if d else None,
                         "presence": here.get(r["artifact"], [])})
        return {**data, "artifacts": rows, "notices": self._notices(project)}

    # ---------------------------------------------------------------- presence: who is working on what, right now
    def _present(self, project: Project) -> dict[str, list[dict]]:
        """{artifact: [{person, where, text, until}]} of the other computers whose presence has not run out. This
        computer's own is left out: the person knows where they are."""
        try:
            st = self._state_of(project)
        except Exception:  # noqa: BLE001 - presence never breaks a board read
            return {}
        out: dict[str, list[dict]] = {}
        for p in _presence.active(st, self.clock(), exclude=project.device.id):
            if p["artifact"]:
                out.setdefault(p["artifact"], []).append({"person": _cap(p["person"] or "Someone"), "where": p["where"],
                                                          "text": p["text"], "until": p["until"]})
        return out

    def touch(self, ref: str, view: str | None = None) -> dict:
        """The person worked in the Room on `ref` (a room ref or a board key) in `view`: write presence when one is due
        (at most about once a minute; see project/presence.py). Returns who else is there. The Room calls it on input
        only, so an idle Room lapses."""
        project, aid = self._find(ref)
        e = _presence.touch(project, aid, view, now=self.clock())
        if e is not None:
            with self._lock:
                self._cache.pop(project.project_id, None)
        return self.room_info(ref)

    def look_work(self) -> list[str]:
        """Presence from work: the newest event this computer wrote in each project (from the app, the CLI or Claude), if
        it is recent and names an artifact, refreshes this person's presence there (throttled in presence.touch). Reads
        only this computer's own log, and only when it changed."""
        from ...project.log import read_log
        touched = []
        now = self.clock()
        for p in self.projects():
            try:
                st = p.log.path.stat()
            except OSError:
                continue
            sig = f"{st.st_size}:{st.st_mtime_ns}"
            with self._lock:
                if self._worked.get(p.project_id) == sig:
                    continue
                self._worked[p.project_id] = sig
            work = [e for e in read_log(p.log.path) if e.get("op") != "presence.set" and e.get("actor") != "scheduler"
                    and e.get("artifact")]
            if not work:
                continue
            e = max(work, key=lambda e: (_ms(e["hlc"]) or 0, e["id"]))
            at = _ms(e["hlc"]) or 0
            if now - at > _presence.MIN_GAP_S * 1000 * 2:
                continue                           # old work (the app just started): not a sign of someone here now
            if _presence.touch(p, e["artifact"], None, now=now, keep_view=True) is not None:
                touched.append(p.project_id)
        return touched

    def room_info(self, ref: str) -> dict:
        """What the Room shows about the artifact on stage beyond the cut: a stage chosen on two computers, and who else
        is working on it."""
        project, aid = self._find(ref)
        row = self._row_of(project, aid)
        here = self._present(project).get(aid, [])
        return {"key": f"{project.project_id}:{aid}", "name": row["name"] if row else None, "stage": row["stage"] if row else None,
                "kind": row["kind"] if row else None, "conflict": row["conflict"] if row else None, "presence": here, "now": self.clock()}

    def post_of(self, ref: str) -> str | None:
        """The studio post a board artifact is composed in (its `post` field, set with artifact_update), or None."""
        project, aid = self._find(ref)
        a = self._state_of(project).artifacts.get(aid)
        post = a.fields.get("post") if a is not None else None
        return post if isinstance(post, str) and post else None

    def _find(self, ref: str) -> tuple[Project, str]:
        if not isinstance(ref, str) or not ref:
            raise StudioError(400, "Name the artifact on stage.", "bad_request")
        if ":" in ref:
            return self._split(ref)
        hits = []
        for p in self.projects():                 # the cached rows: no project is reduced again to find one
            try:
                rows = self._read(p)["artifacts"]
            except Exception:  # noqa: BLE001
                continue
            hits += [(p, r["artifact"]) for r in rows if r.get("room") == ref or r["artifact"] == ref]
        if len(hits) != 1:
            raise StudioError(404, "That artifact is not on the board.", "not_found")
        return hits[0]

    # ---------------------------------------------------------------- sync conflicts: both sides kept, a person asked
    def _notices(self, project: Project) -> list[dict]:
        """What the conflict watcher found in this project, in words: conflict copies outside settings (an alert, left in
        place) and settings two people changed at once that wait on a person (also on the home row as decisions)."""
        from ...syncthing import conflicts
        out = []
        needed = getattr(project, "update_needed", None)
        if needed:                                        # a newer AVE wrote this project: the banner, and the board reads it only
            out.append({"type": "update", "text": needed})
        for a in conflicts.alerts(project.root):          # the watcher's record, checked against the disk: a few stats
            out.append({"type": "alert", "text": a["text"], "file": a["file"], "conflict": a["conflict"]})
        for w in conflicts.waiting(project.root):         # answered here or elsewhere; only the owner's computer applies it
            out.append({"type": "waiting", "text": w["text"], "file": w["file"], "conflict": w["conflict"], "owner": w["owner"]})
        try:
            st = self._state_of(project)
        except Exception:  # noqa: BLE001
            return out
        on_board = {aid for aid, a in st.artifacts.items() if a.created is not None and a.kind != "studio"}
        for d in board.open_decisions(st):
            if d["artifact"] in on_board:
                continue                          # it shows on its row's Waiting on you
            full = self._decision(st, d)
            if full.get("kind") == "setting":
                out.append({"type": "setting", "key": f"{project.project_id}:{d['artifact']}", **full})
        return out

    def _manager(self):
        try:
            return self.sync() if self.sync else None
        except Exception:  # noqa: BLE001
            return None

    def queue_conflicts(self, project: Project, *, sweep: bool = False) -> None:
        """Ask the conflict watcher's worker to look at one project: the settings sweep, or only the copies it knows.
        Never runs the pass here (a request or the sync thread)."""
        from ...syncthing import conflicts
        conflicts.queue(project.root, items=None if sweep else [], manager=self._manager())

    def look_conflicts(self) -> None:
        for p in self.projects():
            self.queue_conflicts(p, sweep=True)

    def _conflicts_reported(self, root: Path, rep) -> None:
        """The worker finished a pass: the board reads again when anything a person sees changed."""
        try:
            want = Path(root).resolve()
        except OSError:
            return
        p = next((p for p in self.projects() if p.root.resolve() == want), None)
        if p is None:
            return
        d = rep.to_dict()
        sig = repr((d["settled"], sorted(x["decision"] for x in d["asked"]), d["waiting"], d["alerts"]))
        with self._lock:
            old = self._conflicts.get(p.project_id)
            self._conflicts[p.project_id] = sig
        if sig != old and (d["settled"] or d["asked"] or d["waiting"] or d["alerts"] or old is not None) and self._publish:
            with self._lock:
                self._cache.pop(p.project_id, None)
            self._publish("board", {"projects": [p.project_id], "by": "sync"})

    def version_ready(self, root: Path, version: str) -> dict | None:
        """{status, reason, from, text} of one version of the project at `root`, or None when the log does not know it there."""
        try:
            want = Path(root).resolve()
        except OSError:
            return None
        for p in self.projects():
            if p.root.resolve() != want:
                continue
            try:
                st = self._state_of(p)
            except Exception:  # noqa: BLE001 - an unreadable log keeps the old behaviour
                return None
            hit = _ready.find_version(st, version)
            if hit is None:
                return None
            d = _ready.version_detail(p.root, hit[1])
            return {**d, "from": _cap(d["from"]) if d.get("from") else None, "text": None if d["status"] == "ready" else _ready.say(d)}
        return None

    def look_ready(self) -> list[str]:
        """Look at every project's readiness again; publish one `readiness` event for those that changed. Returns their ids."""
        changed, items = [], []
        for p in self.projects():
            try:
                st = self._state_of(p)
                now = _ready.details(st, p.root)
            except Exception:  # noqa: BLE001 - one project never stops the others' look
                continue
            with self._lock:
                old = self._ready.get(p.project_id)
                self._ready[p.project_id] = now
            sig = lambda d: {k: (v["status"], v["reason"]) for k, v in (d or {}).items()}
            if old is not None and sig(old) == sig(now):
                continue
            if old is None and all(v["status"] == "ready" for v in now.values()):
                continue                                   # the first look of a project with nothing waiting is not news
            changed.append(p.project_id)
            was, is_ = sig(old), sig(now)
            for aid, a in st.artifacts.items():
                cur = _ready.current_version(a)
                for vid in a.versions:
                    if old is not None and was.get(vid) == is_.get(vid):
                        continue                           # only what changed travels on the stream
                    d = now.get(vid) or {}
                    items.append({"project": p.project_id, "key": f"{p.project_id}:{aid}", "artifact": aid,
                                  "room": self._room_ref(p, aid), "version": vid, "current": bool(cur and cur["version"] == vid),
                                  "status": d.get("status", "ready"), "reason": d.get("reason"),
                                  "from": _cap(d["from"]) if d.get("from") else None,
                                  "text": None if d.get("status", "ready") == "ready" else _ready.say(d)})
        if changed and self._publish:
            self._publish("readiness", {"projects": changed, "items": items})
        return changed

    # ---------------------------------------------------------------- writing (always as the person)
    def _split(self, key: str) -> tuple[Project, str]:
        if not isinstance(key, str) or ":" not in key:
            raise StudioError(400, "Name an artifact as <project>:<artifact>.", "bad_request")
        pid, aid = key.split(":", 1)
        return self._project(pid), aid

    @contextmanager
    def _writing(self, project: Project):
        """The same per-computer lock the MCP tools hold, so a stage is read and moved in one step."""
        locks = project.local_dir / "locks"
        locks.mkdir(parents=True, exist_ok=True)
        fd = os.open(locks / "board.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _changed(self, project: Project) -> None:
        with self._lock:
            self._cache.pop(project.project_id, None)
        if self._publish:
            self._publish("board", {"projects": [project.project_id], "by": "human"})

    @staticmethod
    def _fields(raw: Any) -> dict:
        if not isinstance(raw, dict) or not raw:
            raise StudioError(400, "Say which fields to change.", "bad_request")
        out = {}
        for k, v in raw.items():
            if k not in FIELDS:
                raise StudioError(400, f"{k} cannot be changed here.", "bad_request")
            if k == "title":
                if not isinstance(v, str) or not v.strip():
                    raise StudioError(400, "An artifact needs a name.", "bad_request")
                v = v.strip()[:200]
            elif k == "due":
                if v in ("", None):
                    v = None
                elif not isinstance(v, str) or not DATE_RE.match(v):
                    raise StudioError(400, "A due date looks like 2026-11-04.", "bad_request")
            elif k == "owner":
                if v in ("", None):
                    v = None
                elif not isinstance(v, str) or len(v) > 80:
                    raise StudioError(400, "Choose a person.", "bad_request")
            elif k == "order":
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                    raise StudioError(400, "order must be a finite number.", "bad_request")
            elif k == "archived":
                v = bool(v)
            out[k] = v
        return out

    def create(self, body: dict, *, via: str | None = None) -> dict:
        actor = self._actor(via)
        project = self._project(body.get("project")) if isinstance(body.get("project"), str) else None
        if project is None:
            raise StudioError(400, "Choose the project the artifact belongs to.", "bad_request")
        title = body.get("title")
        if not isinstance(title, str) or not title.strip():
            raise StudioError(400, "An artifact needs a name.", "bad_request")
        kind = body.get("kind") or "artifact"
        if kind not in KINDS:
            raise StudioError(400, f"kind is one of {', '.join(KINDS)}.", "bad_request")
        data: dict[str, Any] = {"kind": kind, "title": title.strip()[:200], "stage": "Plan"}
        if body.get("order") is not None:
            data["order"] = self._fields({"order": body["order"]})["order"]
        with self._writing(project):
            st = project.state()
            parent = None
            if body.get("parent"):
                ppid, paid = self._split(body["parent"])
                if ppid.project_id != project.project_id or paid not in st.artifacts:
                    raise StudioError(400, "A part belongs to an artifact in the same project.", "bad_request")
                parent = paid
            aid = "art_" + _hlc.ulid().lower()
            project.log.append("artifact.created", artifact=aid, data=data, actor=actor)
            if parent:
                project.log.append("artifact.linked", artifact=parent, data={"to": aid, "rel": "part"}, actor=actor)
        self._changed(project)
        return {"row": self._row_of(project, aid)}

    def _row_of(self, project: Project, aid: str) -> dict | None:
        return next((r for r in self._read(project)["artifacts"] if r["artifact"] == aid), None)

    def patch(self, key: str, body: dict, *, via: str | None = None) -> dict:
        """`via="app"` only from the desktop app's own native code (never an HTTP request): the act came through the app, and is the
        person's. Anything else is Claude's (see the module docstring)."""
        project, aid = self._split(key)
        actor = self._actor(via)
        relay = {} if actor == "human" else {"relayed_by": "claude", **self._quote_of(body)}
        if not isinstance(body, dict) or not body:
            raise StudioError(400, "Say what to change.", "bad_request")
        ops = [k for k in PATCH_OPS if k in body]
        if len(ops) > 1:
            raise StudioError(400, f"Change one thing at a time, not {' and '.join(ops)}.", "bad_request")
        with self._writing(project):
            st = project.state()
            a = st.artifacts.get(aid)
            if a is None or a.created is None:
                raise StudioError(404, "That artifact is not on the board any more.", "not_found")
            if "fields" in body:
                project.log.append("artifact.updated", artifact=aid, data=self._fields(body["fields"]), actor=actor)
            elif "stage" in body:
                self._stage(project, st, a, body, actor=actor, relay=relay)
            elif "note" in body:
                text = body["note"]
                if not isinstance(text, str) or not text.strip() or len(text) > 5000:
                    raise StudioError(400, "Write the update first (at most 5000 characters).", "bad_request")
                project.log.append("note.added", artifact=aid, data={"text": text.strip()}, actor=actor)
            elif "confirm" in body:
                self._confirm_check(key, body["confirm"])          # before anything is read or written
                e = self._relayed_event(project, aid, body["confirm"], settled=True)
                project.log.append("note.added", artifact=aid, data={"confirms": e["id"], "text": "Confirmed"}, actor="human")
            elif "undo" in body:
                self._undo(project, st, a, self._relayed_event(project, aid, body["undo"], settled=True), actor=self._undo_actor(via))
            elif "resolve" in body:
                d = st.decisions.get(body["resolve"])
                if d is None or d.get("artifact") != aid or "data" not in d:
                    raise StudioError(404, "That decision is not on this artifact.", "not_found")
                choice = body.get("choice")
                data = {"decision": d["id"], "choice": choice}
                from ...syncthing import conflicts
                if choice == conflicts.PER_KEY and d["data"].get("kind") == "setting":
                    picks = conflicts.valid_picks(d, body.get("picks"))
                    if picks is None:
                        raise StudioError(400, "Choose a file for every setting.", "bad_request")
                    data["picks"] = picks
                elif choice not in (d["data"].get("options") or []):
                    raise StudioError(400, "Choose one of the options.", "bad_request")
                if not board.decision_open(d):
                    raise StudioError(409, "That decision was already answered.", "conflict")
                if actor == "human" and d["data"].get("kind") == conflicts.DECISION_KIND:
                    self._grant_check("answerDecision", f"{key}/{d['id']}")     # it changes a shared settings file
                project.log.append("decision.resolved", artifact=aid, data={**data, **relay}, actor=actor)
            elif "reopen_decision" in body:
                d = st.decisions.get(body["reopen_decision"])
                if d is None or d.get("artifact") != aid or board.decision_open(d):
                    raise StudioError(409, "That decision is already open.", "conflict")
                project.log.append("decision.resolved", artifact=aid, data={"decision": d["id"], "reopen": True}, actor=actor)
            else:
                raise StudioError(400, "Say what to change.", "bad_request")
        self._changed(project)
        did = body.get("resolve") or body.get("reopen_decision")
        if "undo" in body:
            did = ((self._event_data(project, body["undo"]) or {}).get("decision"))
        if isinstance(did, str) and ((st.decisions.get(did) or {}).get("data") or {}).get("kind") == "setting":
            self.queue_conflicts(project)        # the worker writes the chosen value and archives, then removes, the conflict copy;
                                                 # or, for a question opened again, puts both copies back
        return {"row": self._row_of(project, aid)}

    def _stage(self, project, st, a, body, *, actor: str = "human", relay: dict | None = None) -> None:
        to = body.get("stage")
        if to not in STAGES:
            raise StudioError(400, f"A stage is one of {', '.join(STAGES)}.", "bad_request")
        if to == "Out":
            raise StudioError(409, OUT_REFUSED, "gate.publish")
        expect = body.get("from")
        if expect is not None and expect != a.stage:
            raise StudioError(409, f"{board.name_of(a)} moved to {a.stage} a moment ago. Look again and choose.", "conflict")
        conflict = next((c for c in st.conflicts if c.artifact == a.id), None)
        if to == a.stage and conflict is None:
            return
        op = "stage.reopened" if STAGES.index(to) < STAGES.index(a.stage) else "stage.signed_off"
        if actor != "human" and to == "Finish" and (a.stage != "Finish" or conflict is not None):
            why = self._finish_block(project, a)
            if why:                            # the same check Claude's stage_sign_off makes: a trim needs the director first
                raise StudioError(409, f"{board.name_of(a)} cannot move to Finish yet: a trim needs the director's confirmation first. {why}",
                                  "gate.trim")
        data = {"from": a.stage, "to": to}
        if conflict is not None:
            data["settles"] = conflict.id      # this event saw both changes, so the reducer stops seeing a conflict
        if relay and (op == "stage.signed_off" or conflict is not None):
            data.update(relay)                 # Claude's: the director confirms or undoes it on the board
        project.log.append(op, artifact=a.id, data=data, actor=actor)

    @staticmethod
    def _finish_block(project, a) -> str | None:
        from ...project.adopt import home_artifact
        root = Path(project.root)
        if a.id != home_artifact(root) or not (root / "episode.json").is_file():
            return None
        from ... import trim
        from ...episode import Episode
        try:
            return trim.finish_block(Episode(root))
        except (OSError, ValueError, SystemExit):
            return None

    @staticmethod
    def _quote_of(body: dict) -> dict:
        q = body.get("quote") if isinstance(body, dict) else None
        return {"quote": q.strip()[:500]} if isinstance(q, str) and q.strip() else {}

    @staticmethod
    def _event_data(project, eid) -> dict | None:
        e = next((e for e in project.read_events() if e["id"] == eid), None) if isinstance(eid, str) else None
        return (e or {}).get("data")

    @staticmethod
    def _relayed_event(project, aid, eid, settled=False) -> dict:
        events = project.read_events()
        e = next((e for e in events if e["id"] == eid), None)
        if e is None or e.get("artifact") != aid or (e.get("data") or {}).get("relayed_by") != "claude":
            raise StudioError(404, "That is not something Claude reported for this artifact.", "not_found")
        if settled:
            for x in events:
                d = x.get("data") or {}
                if x["op"] == "note.added" and d.get("confirms") == eid:
                    raise StudioError(409, "You already confirmed that.", "conflict")
                if d.get("undoes") == eid:
                    raise StudioError(409, "That was already undone.", "conflict")
        return e

    # Confirming what Claude reported is the check against a fabricated sign-off, so Claude must never be able to make it. It
    # needs the app's device-owner check, a single-use grant bound to exactly this sign-off (desktop_auth), spent by the app's
    # own native sheet. A request with only the room token, a grant for anything else, an agent marker, or no app: refused.
    authority = None                   # the desktop app's NativeAuthority (set by StudioServer); None outside the app

    def _confirm_check(self, key: str, eid) -> None:
        self._grant_check("confirmRelayed", f"{key}/{eid}",
                          "Confirm what Claude reported in the AVE Studio app: it checks that it is you.")

    def _grant_check(self, action: str, target: str, refused: str = "Answer this in the AVE Studio app: it checks that it is you.") -> None:
        """The app's device-owner check for one act on one target (desktop_auth): a grant spent by the app's own native sheet."""
        from ...humanpresence import agent_markers_set
        if self.authority is None or agent_markers_set():
            raise StudioError(403, refused, "native.protected_action")
        self.authority.require(action, (target,))

    @staticmethod
    def _actor(via: str | None) -> str:
        """The person only through the app's own native bridge, and never while an agent marker is set; else Claude."""
        from ...humanpresence import agent_markers_set
        return "human" if via == "app" and not agent_markers_set() else "claude"

    def _undo_actor(self, via: str | None) -> str:
        """Undo only takes Claude's record back, so it is unchecked; but it is the person's act only when it came through the
        app's bridge. A room-token request (in the app or in a browser), or anything while an agent marker is set, is Claude's."""
        return self._actor(via)

    @staticmethod
    def _undo(project, st, a, e, actor: str = "human") -> None:
        d = e.get("data") or {}
        if e["op"] in ("stage.signed_off", "stage.reopened") and d.get("settles"):
            back = d.get("from")                   # where both computers last agreed
            if a.stage != d.get("to") or back not in STAGES or back == a.stage:
                raise StudioError(409, f"{board.name_of(a)} has moved on to {a.stage} since. Set the stage by hand instead.", "conflict")
            op = "stage.reopened" if STAGES.index(back) < STAGES.index(a.stage) else "stage.signed_off"
            project.log.append(op, artifact=a.id, data={"from": a.stage, "to": back, "undoes": e["id"]}, actor=actor)
        elif e["op"] == "stage.signed_off":
            if a.stage != d.get("to"):
                raise StudioError(409, f"{board.name_of(a)} has moved on to {a.stage} since. Set the stage by hand instead.", "conflict")
            project.log.append("stage.reopened", artifact=a.id, data={"from": a.stage, "to": d.get("from"), "undoes": e["id"]},
                               actor=actor)
        elif e["op"] == "decision.resolved":
            dec = st.decisions.get(d.get("decision")) or {}
            if board.decision_open(dec):
                raise StudioError(409, "That decision is already open again.", "conflict")
            if (dec.get("history") or [{}])[-1].get("event") != e["id"]:
                raise StudioError(409, "That answer has been replaced since. Answer the decision again instead.", "conflict")
            project.log.append("decision.resolved", artifact=a.id, data={"decision": d.get("decision"), "reopen": True,
                                                                         "undoes": e["id"]}, actor=actor)
        else:
            raise StudioError(400, "Only a sign-off or an answer Claude reported can be undone here.", "bad_request")

    # ---------------------------------------------------------------- sync state, in plain words
    # ---------------------------------------------------------------- sources: the files each project is made from, and who has them
    def sources(self, library: Callable[[], dict] | None = None) -> dict:
        """Every project's source files (footage, graphics and music folders, and the studio's library) with where each one is,
        in words (project/where.py). `library()` is the studio's own library listing (GET /api/studio/library), so licences and
        usage read exactly as the old Library screen read them. People are named; device ids never appear."""
        from ...project.where import Where
        out = []
        for project in self.projects():
            try:
                where = Where.of(project.root, project.device.id, project.person)
            except (OSError, ValueError, KeyError):
                where = None
            is_studio = self.sp is not None and project.root.resolve() == Path(self.sp.root).resolve()
            files, total = _source_files(project.root, where)
            lib, lib_total = [], 0
            if is_studio and library is not None:
                try:
                    got = library() or {}
                    lib = list(got.get("assets") or [])
                    lib_total = got.get("total") if isinstance(got.get("total"), int) else len(lib)
                except Exception:  # noqa: BLE001 - a library that cannot be read hides the library, never the project's footage
                    lib, lib_total = [], 0
            for a in lib:
                a["where"] = where.describe(a.get("file") or "") if where and a.get("file") else None
            out.append({"id": project.project_id, "title": project.title or project.root.name, "kind": folder_kind(project.root),
                        "person": project.person, "others": [m["person"] for m in where.others] if where else [],
                        "proxies_here": bool(where and where.media == "proxies"),
                        "studio": is_studio, "files": files, "total": total, "library": lib, "library_total": lib_total})
        return {"projects": out}

    # ---------------------------------------------------------------- Settings: people, and what waits on an approval sheet
    def people(self) -> dict:
        """Settings' People: each project's members in words (who, how much media they keep, who publishes, which one is this
        computer's person). Device ids never leave the server; sharing changes go through the app's native sheets."""
        from ...syncthing import members as M
        out = []
        for p in self.projects():
            try:
                rows = M.members(p.root)
                me = p.device.id
            except Exception:              # an unreadable project.json leaves the project out of People, never the page
                continue
            out.append({"id": p.project_id, "title": p.title or p.root.name, "kind": folder_kind(p.root), "person": p.person,
                        "members": [{"person": m["person"] or "Someone", "media": M.MEDIA_WORDS.get(m["media"], m["media"]),
                                     "publisher": bool(m["publisher"]), "you": me in m["devices"], "computers": len(m["devices"])}
                                    for m in rows]})
        return {"projects": out}

    def trims(self) -> list[dict]:
        """Claude's draft trims waiting on the director, across the episode projects here (the trim approval sheet's list)."""
        from ...desktop_approvals import Approvals
        rooms = {p.project_id: self._room_ref(p, "art_ep") for p in self.projects()}
        return [{**t, "room": rooms.get(t["project"])} for t in Approvals(None, self).trims()]

    # Sync is looked at on its own thread (`workspace-sync`), never on the log watcher's: a Syncthing that is restarting
    # or hung costs at most a few short calls there and never delays a board or Room event. Readers get the last look.
    def sync_status(self) -> dict:
        """{"available", "projects": {project id: words}, "missing": [shared projects whose drive is not connected]}: the
        last look, at once. Before the first look (or with no sync thread, outside the app) it looks now."""
        with self._lock:
            cached = self._sync_last
        if cached is not None:
            self._sync_kick.set()
            return cached
        if self._sync_thread is not None:
            self._sync_kick.set()
            return {"available": False, "projects": {}, "missing": [], "pending": True}
        return self._remember(self.look_sync())

    def look_sync(self) -> dict:
        from contextlib import nullcontext

        from ...syncthing import SyncStatus
        from ...syncthing.words import say
        m = self.sync() if self.sync else None
        if m is None:
            return {"available": False, "projects": {}, "missing": []}
        quick = getattr(m, "quick", None)
        down_of = getattr(m, "down", None)
        offline = getattr(m, "without_sync", None)
        out: dict[str, dict] = {}
        with quick(SYNC_CALL_S) if quick else nullcontext():
            down = None
            try:
                folders = m.folders()               # one call per look, and the first sign that sync is not answering
            except Exception as exc:  # noqa: BLE001
                folders = []
                down = down_of(str(exc) or "not answering") if down_of else SyncStatus("sync_off", error=str(exc))
            for p in self.projects():
                try:
                    if down is None:
                        st = m.status(p.root, folders=folders) if quick else m.status(p.root)
                        if st.state == "error" and "timed out" in st.error.lower() and down_of:
                            down = down_of(st.error)         # stop asking a Syncthing that has stopped answering
                    if down is not None:
                        st = offline(p.root, down) if offline else down
                except Exception as exc:  # noqa: BLE001 - one project's status never hides the others'
                    st = SyncStatus("error", error=str(exc))
                out[p.project_id] = say(st)
        missing = []
        for f in folders:
            if f.get("id") in out or (Path(f.get("path", "")) / ".ave" / "project.json").is_file():
                continue
            missing.append({"id": f.get("id"), "title": f.get("label") or f.get("id"), "path": f.get("path"),
                            **say(SyncStatus("drive_missing"))})
        return {"available": True, "projects": out, "missing": missing}

    def _remember(self, data: dict) -> dict:
        with self._lock:
            self._sync_last = data
        return data

    def _sync_once(self) -> None:
        import json
        data = self._remember(self.look_sync())
        sig = json.dumps(data, sort_keys=True)
        if sig != self._sync_seen:
            self._sync_seen = sig
            if self._publish and data["available"]:
                self._publish("sync", data)

    def _sync_loop(self) -> None:
        while not self._stop.is_set():
            kicked = self._sync_kick.wait(SYNC_S)
            self._sync_kick.clear()
            if self._stop.is_set():
                return
            if time.monotonic() - self._conflicts_at >= CONFLICT_S:      # watched or not
                self._conflicts_at = time.monotonic()
                try:
                    self.look_conflicts()               # conflict copies, also those Syncthing left before this app started
                except Exception:  # noqa: BLE001
                    pass
            if self.sync is None or not (kicked or self._watching()):
                continue
            try:
                self._sync_once()
            except Exception:  # noqa: BLE001 - a failed look waits for the next one
                pass

    # ---------------------------------------------------------------- watching for other writers
    def poll(self) -> list[str]:
        """Project ids whose logs changed since last looked at (another process, another computer via sync)."""
        changed = []
        current = {}
        for p in self.projects():
            try:
                fp = p.index().fingerprint()
            except OSError:
                continue
            current[p.project_id] = fp
            with self._lock:
                seen = self._known.get(p.project_id)
            if seen != fp:
                changed.append(p.project_id)
        with self._lock:
            gone = [pid for pid in self._known if pid not in current]
            self._known = current
        return changed + gone

    def look_posts(self) -> str | None:
        """The studio's change log (ledger/events.jsonl) grew since the last look: its new cursor, else None. A `stat`, so it runs
        every tick whether anyone follows the stream or not; the stream replays what a late follower missed."""
        if self.sp is None:
            return None
        from .. import ledger
        from ..events import cursor_str
        try:
            size = ledger.events_path(self.sp).stat().st_size
        except OSError:
            size = 0
        with self._lock:
            seen, self._posts_size = self._posts_size, size
        return cursor_str(size) if seen is not None and seen != size else None

    def start(self) -> None:
        if self._thread is not None or self._publish is None:
            return
        self.look_posts()                          # where the studio's change log stands now

        try:
            self.poll()                            # where the logs stand now, so the first look reports only real changes
        except Exception:  # noqa: BLE001
            pass

        try:
            self.look_ready()                      # where readiness stands now, so the first look reports only real changes
        except Exception:  # noqa: BLE001
            pass
        self._ready_off = _ready.on_hashed(lambda root: self._ready_kick.set())

        from ...syncthing import conflicts
        self._conflicts_off = conflicts.on_report(self._conflicts_reported)
        for p in self.projects():                  # work written before the app started is not a sign of anyone here now
            try:
                st = p.log.path.stat()
                self._worked[p.project_id] = f"{st.st_size}:{st.st_mtime_ns}"
            except OSError:
                pass

        def run():
            while not self._stop.is_set():
                self._ready_kick.wait(WATCH_S)     # every WATCH_S, or at once when a hash is known
                self._ready_kick.clear()
                if self._stop.is_set():
                    return
                try:
                    self.look_work()               # presence follows this computer's own work, watched or not
                except Exception:  # noqa: BLE001
                    pass
                try:
                    moved = self.look_posts()      # a post changed (Claude, the CLI, the scheduler, another screen)
                    if moved:
                        self._publish("posts", {"cursor": moved})
                except Exception:  # noqa: BLE001
                    pass
                if not self._watching():
                    continue                       # nobody is following the stream: the next board read is fresh anyway
                try:
                    changed = self.poll()
                except Exception:  # noqa: BLE001 - a bad read waits for the next look
                    changed = None
                if changed:
                    with self._lock:
                        for pid in changed:
                            self._cache.pop(pid, None)
                    self._publish("board", {"projects": changed, "by": "log"})
                try:
                    self.look_ready()
                except Exception:  # noqa: BLE001
                    pass
        self._thread = threading.Thread(target=run, name="workspace-watch", daemon=True)
        self._thread.start()
        self._sync_thread = threading.Thread(target=self._sync_loop, name="workspace-sync", daemon=True)
        self._sync_thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._sync_kick.set()
        self._ready_kick.set()
        if self._ready_off is not None:
            self._ready_off()
            self._ready_off = None
        if self._conflicts_off is not None:
            self._conflicts_off()
            self._conflicts_off = None


SOURCE_CAP = 1000                 # files listed per project; the total still counts every one
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff", ".heic", ".svg", ".psd"}
_AUDIO_EXTS = {".wav", ".aif", ".aiff", ".mp3", ".m4a", ".flac", ".aac"}


def _kind_of(name: str) -> str:
    from ...episode import MEDIA_EXTS
    ext = Path(name).suffix.lower()
    if ext in _AUDIO_EXTS:
        return "audio"
    if ext in MEDIA_EXTS:
        return "video"
    if ext in _IMAGE_EXTS:
        return "image"
    return "document"


def _source_files(root: Path, where) -> tuple[list[dict], int]:
    """The footage, graphics and music a project is made from, never its own edits, renders or caches. Proxies are not rows of
    their own: a footage file says whether its proxy exists."""
    from ...episode import MEDIA_EXTS, PROXY_DIRNAME
    if where is None:
        return [], 0
    folders = [(d, "Footage") for d in where.layout["raw_dirs"]] + [(where.layout["gfx"], "Graphics"), (where.layout["music"], "Music")]
    wanted = set(MEDIA_EXTS) | _IMAGE_EXTS
    found: list[dict] = []
    total = 0
    seen: set[str] = set()
    for folder, label in folders:
        base = root / folder
        if not base.is_dir():
            continue
        stack = [base]
        while stack:
            d = stack.pop()
            try:
                entries = sorted(d.iterdir(), key=lambda p: p.name.lower())
            except OSError:
                continue
            for p in entries:
                if p.name.startswith(".") or p.is_symlink():
                    continue
                if p.is_dir():
                    if p.name not in ("$RECYCLE.BIN", "System Volume Information") and (p.name != PROXY_DIRNAME or label == "Footage"):
                        stack.append(p)
                    continue
                if p.suffix.lower() not in wanted:
                    continue
                rel = p.relative_to(root).as_posix()
                if rel in seen:
                    continue
                orig = where.original_dir_of(rel) if label == "Footage" else None
                if orig is not None:                   # a review proxy: a row only when its original is not on this computer
                    odir = root / orig[0]
                    try:
                        here = odir.is_dir() and any(x.stem == orig[1] and x.is_file() for x in odir.iterdir())
                    except OSError:
                        here = False
                    if here:
                        continue
                seen.add(rel)
                total += 1
                if len(found) >= SOURCE_CAP:
                    continue
                try:
                    size = p.stat().st_size
                except OSError:
                    size = None
                if orig is not None:
                    found.append({"name": p.name, "path": rel, "folder": label, "kind": "video", "bytes": size, "proxy": True,
                                  "has_proxy": True, "where": where.describe_proxy(rel)})
                    continue
                proxy = where.proxy_of(rel) if label == "Footage" else None
                has_proxy = (root / proxy).is_file() if proxy else None
                found.append({"name": p.name, "path": rel, "folder": label, "kind": _kind_of(p.name), "bytes": size,
                              "has_proxy": has_proxy, "where": where.describe(rel, has_proxy=has_proxy)})
    return found, total


def _not_ready(d: dict | None) -> dict | None:
    """{status, text} for a version or artifact that cannot be opened yet, else None."""
    if not d or d.get("status") == "ready":
        return None
    who = _cap(d["from"]) if d.get("from") else "another computer"
    if d["status"] == "arriving":
        text = f"Arriving from {who}…"
    elif d["status"] == "stuck":
        text = (f"Stuck on the way from {who}: ask {who} to resend it" if d.get("reason") == "resend"
                else f"Stuck on the way from {who}: check sync status")
    else:
        text = f"Damaged on the way from {who}: ask {who} to resend it"
    return {"status": d["status"], "text": text, "from": who}


def _sentence(st, e: dict) -> str | None:
    """One plain line about an event, for activity feeds. Version and file names never appear."""
    op, d = e.get("op"), e.get("data") or {}
    a = st.artifacts.get(e.get("artifact")) if e.get("artifact") else None
    name = board.name_of(a) if a else "the project"
    if op == "artifact.created":
        return f"added {d.get('title') or name} in {d.get('stage') or 'Plan'}"
    if op == "artifact.linked":
        b = st.artifacts.get(d.get("to"))
        return f"made {board.name_of(b)} a part of {name}" if b else None
    if op == "artifact.updated":
        bits = []
        for k, v in d.items():
            if k in ("request_id", "args_sha"):
                continue
            if k == "title":
                bits.append(f"renamed it {v}")
            elif k == "due":
                bits.append(f"set the due date to {v}" if v else "cleared the due date")
            elif k == "owner":
                bits.append(f"gave it to {v}" if v else "left it with nobody")
            elif k == "archived":
                bits.append("archived it" if v else "brought it back from the archive")
            elif k == "order":
                bits.append("moved it on the board")
            elif k == "claude_is":
                bits.append(f"is {str(v)[:1].lower()}{str(v)[1:]}" if v else "finished what it was doing")
            else:
                bits.append(f"changed {k.replace('_', ' ')}")
        return f"{', '.join(bits)} on {name}" if bits else None
    if op == "version.added":
        vid = d.get("version") or ""
        v = a.versions.get(vid) if a else None
        return f"made {board.version_name(vid, v['number'] if v else None)} of {name}"
    if op == "version.kept":
        v = a.versions.get(d.get("version")) if a else None
        return f"kept {board.version_name(d.get('version'), v['number'] if v else None)} of {name}"
    if op in ("stage.signed_off", "stage.reopened") and d.get("settles"):
        q = f": “{d['quote']}”" if d.get("quote") else ""
        return f"settled the stage of {name} on {d.get('to')}{q}"
    if op == "stage.signed_off":
        q = f": “{d['quote']}”" if d.get("quote") else ""
        return f"signed off {d.get('from')} on {name}, now in {d.get('to')}{q}"
    if op == "stage.reopened":
        return f"{'undid the sign-off' if d.get('undoes') else 'reopened'} {name}, back to {d.get('to')}"
    if op == "note.added":
        if d.get("confirms"):
            return f"confirmed what Claude reported on {name}"
        if d.get("kind") == "settings_merged":
            return str(d.get("text") or "merged two copies of a setting")[:300]
        return f"posted an update on {name}"
    if op == "decision.recorded":
        if d.get("kind") == "setting":
            return f"kept both copies: {d.get('summary')}" if d.get("summary") else None
        return f"decided on {name}: {d.get('summary')}" if d.get("summary") else None
    if op == "clip.decided":                               # a keep / maybe / skip on a select, from the Room
        t = f"“{d.get('title')}”" if d.get("title") else "a select"
        return {"keep": f"picked {t} for {name}", "maybe": f"marked {t} as a maybe for {name}", "skip": f"skipped {t} for {name}",
                "pending": f"took back the call on {t} for {name}", "nudge": f"moved where {t} starts or ends for {name}"}.get(d.get("choice"), f"decided on {t} for {name}")
    if op == "decision.resolved":
        if d.get("gone"):
            return None
        if d.get("reopen"):
            return f"opened a decision again on {name}"
        if d.get("picks"):
            return f"chose setting by setting between the two copies on {name}"
        return f"chose “{d.get('choice')}” on {name}"
    if op == "member.added":
        return f"shared the project with {d.get('person') or 'someone'}"
    if op == "member.removed":
        return f"took {d.get('person') or 'someone'} off the project"
    if op == "project.publisher_changed":
        return f"handed publishing to {d.get('person') or 'another computer'}"
    if op == "task.updated":
        if d.get("title"):
            return f"is on {d['title']} for {name}" if d.get("status") == "doing" else f"updated the task {d['title']}"
        return f"marked a task {d['status']}" if d.get("status") else None
    return None
