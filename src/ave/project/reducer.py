"""Deterministic reducer: every computer turns the same set of log events into the same State.

Events are validated, de-duplicated by id, then applied in (hlc, id) order, so arrival order and duplicate delivery never
matter. One bad event never stops a project from reading: it is skipped with a warning.

Merge rules (architecture, "The log"):
  notes, decisions, versions   union by id.
  scalar fields                a write that saw another write wins over it, whatever the stamps say. Only writes where
                               neither saw the other fall back to the higher (hlc, id). Every write is kept in `history`.
  stage                        a change wins only if it saw every other change. Changes where neither saw the other become a
                               `State.conflicts` entry, and the stage stays at the last value both sides agreed on.

"Saw" is read from the `seen` map every event carries (every entry must be before the event's own stamp, so "saw" can
never form a cycle and a set of writes always has at least one head), `{device: newest hlc read from that device's log}`. A device's log
only grows, so having its event at hlc h means having all of its events up to h. An event always saw earlier events of its
own device.

Version numbers: each artifact's versions are numbered 1..n by the stamp of their own `version.added` event, which never
changes. A number can shift only while an earlier-stamped version has not yet arrived on this computer.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from typing import Any, Iterable

from . import hlc as _hlc

STAGES = ("Plan", "Draft", "Refine", "Finish", "Out")
DEFAULT_STAGE = "Plan"
ACTORS = ("human", "claude", "scheduler")
OPS = (
    "artifact.created", "artifact.linked", "artifact.updated", "version.added", "version.kept", "stage.signed_off",
    "stage.reopened", "note.added", "decision.recorded", "decision.resolved", "task.updated", "presence.set", "clip.decided",
    "member.added", "member.removed", "project.publisher_changed", "publish.noted",
)
MEMBERSHIP_OPS = ("member.added", "member.removed", "project.publisher_changed")
STAGE_OPS = ("stage.signed_off", "stage.reopened")
ARTIFACT_OPS = ("artifact.created", "artifact.linked", "artifact.updated", "version.added", "version.kept", *STAGE_OPS)
RESERVED_FIELDS = ("stage",)
STAMP_FIELDS = ("request_id", "args_sha")  # retry stamps on events Claude writes: log bookkeeping, never board fields
ID_FIELDS = ("version", "note", "decision", "task", "from", "to", "rel", "clip")
MAX_DEPTH = 64


@dataclass
class Artifact:
    id: str
    kind: str | None = None
    title: str | None = None
    stage: str = DEFAULT_STAGE
    fields: dict[str, Any] = field(default_factory=dict)
    history: dict[str, list[dict]] = field(default_factory=dict)
    versions: dict[str, dict] = field(default_factory=dict)
    kept: str | None = None
    links: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    stage_history: list[dict] = field(default_factory=list)
    needs: list[dict] = field(default_factory=list)
    created: str | None = None
    updated: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Conflict:
    id: str
    artifact: str
    field: str
    base: str
    options: list[dict]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class State:
    artifacts: dict[str, Artifact] = field(default_factory=dict)
    conflicts: list[Conflict] = field(default_factory=list)
    decisions: dict[str, dict] = field(default_factory=dict)
    tasks: dict[str, dict] = field(default_factory=dict)
    notes: dict[str, dict] = field(default_factory=dict)
    presence: dict[str, dict] = field(default_factory=dict)
    clip_decisions: dict[str, dict] = field(default_factory=dict)   # select id -> the director's latest keep / maybe / skip / pending
    membership: list[dict] = field(default_factory=list)   # who was added, removed or given publishing, in (hlc, id) order
    # "<device>|<queue item id>" -> that computer's latest `publish.noted`: what the publisher says is queued, for other computers to show read-only.
    # Display only: nothing sends, approves or skips a send because of it (publish/queue.py decides from its own local record).
    publishing: dict[str, dict] = field(default_factory=dict)
    frontier: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@lru_cache(maxsize=65536)
def _stamp(text: str) -> _hlc.Stamp:
    return _hlc.parse(text)


def _depth_ok(value: Any) -> bool:
    stack = [(value, 1)]
    while stack:
        v, d = stack.pop()
        if isinstance(v, (dict, list)):
            if d > MAX_DEPTH:
                return False
            stack.extend((x, d + 1) for x in (v.values() if isinstance(v, dict) else v))
    return True


def _str_or_none(v: Any) -> bool:
    return v is None or isinstance(v, str)


def validate_event(e: Any) -> str | None:
    """Why `e` is not a usable event (None = usable). Shared by the log reader and the reducer."""
    if not isinstance(e, dict):
        return "not an object"
    if not isinstance(e.get("id"), str) or not e["id"]:
        return "no id"
    if not isinstance(e.get("device"), str) or not e["device"]:
        return "no device"
    try:
        s = _stamp(e["hlc"]) if isinstance(e.get("hlc"), str) else None
    except ValueError:
        s = None
    if s is None:
        return f"bad hlc {str(e.get('hlc'))[:80]!r}"
    if s.ms >= _hlc.MAX_MS:
        return "hlc beyond year 9000"
    if s.device != e["device"]:
        return f"hlc device {s.device!r} is not the event's device"
    if not isinstance(e.get("op"), str):
        return "no op"
    for k in ("artifact", "person", "actor"):
        if not _str_or_none(e.get(k)):
            return f"{k} is not a string"
    data = e.get("data")
    if data is not None:
        if not isinstance(data, dict):
            return "data is not an object"
        for k in ID_FIELDS:
            if not _str_or_none(data.get(k)):
                return f"data.{k} is not a string"
    needs = e.get("needs")
    if needs is not None and (not isinstance(needs, list) or not all(isinstance(n, dict) for n in needs)):
        return "needs is not a list of objects"
    seen = e.get("seen")
    if seen is not None:
        if not isinstance(seen, dict):
            return "seen is not an object of stamps"
        for d, v in seen.items():
            try:
                sv = _stamp(v) if isinstance(v, str) else None
            except ValueError:
                sv = None
            if sv is None:
                return f"seen[{str(d)[:64]!r}] is not a stamp"
            if sv >= s:
                return f"seen[{str(d)[:64]!r}] is not before the event's own hlc"  # keeps "saw" acyclic
    if not _depth_ok(e):
        return f"nested more than {MAX_DEPTH} deep"
    return None


def _canon(e: dict) -> str:
    return json.dumps(e, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _key(e: dict) -> tuple:
    return (_stamp(e["hlc"]), e["id"])


def _who(e: dict) -> dict:
    return {"event": e["id"], "hlc": e["hlc"], "device": e["device"], "person": e.get("person"), "actor": e.get("actor")}


def saw(x: dict, y: dict) -> bool:
    """True when event `x` was written by a device that had already read event `y`."""
    if x["id"] == y["id"]:
        return False
    if x["device"] == y["device"]:
        return _key(x) > _key(y)
    v = (x.get("seen") or {}).get(y["device"])
    try:
        return isinstance(v, str) and _stamp(v) >= _stamp(y["hlc"])
    except ValueError:
        return False


def heads(events: list[dict]) -> list[dict]:
    """The events no other event in the list saw, in (hlc, id) order."""
    return sorted((e for e in events if not any(saw(o, e) for o in events)), key=_key)


class _Writes:
    """Scalar writes collected during the pass and resolved at the end by `saw`, then (hlc, id)."""

    def __init__(self):
        self.by_slot: dict[tuple, list[tuple[dict, Any]]] = {}

    def add(self, slot: tuple, e: dict, value: Any) -> None:
        self.by_slot.setdefault(slot, []).append((e, value))

    def resolve(self, slot: tuple) -> tuple[Any, list[dict]]:
        writes = self.by_slot[slot]
        evs = [e for e, _ in writes]
        winner = heads(evs)[-1]
        value = next(v for e, v in writes if e is winner)
        history = [{"value": v, **_who(e)} for e, v in sorted(writes, key=lambda w: _key(w[0]))]
        return value, history


def reduce(events: Iterable[dict]) -> State:
    """Validate, de-duplicate, sort by (hlc, id), apply. Same events give the same State on every computer."""
    warnings: set[str] = set()
    by_id: dict[str, dict] = {}
    canon: dict[str, str] = {}
    for e in events:
        try:
            why = validate_event(e)
            if why:
                eid = e.get("id") if isinstance(e, dict) else None
                warnings.add(f"event {str(eid)[:80] if isinstance(eid, str) else '?'}: skipped, {why}")
                continue
            c = _canon(e)
        except (ValueError, TypeError, RecursionError) as exc:
            warnings.add(f"event ?: skipped, unreadable ({type(exc).__name__})")
            continue
        prev = canon.get(e["id"])
        if prev is None:
            by_id[e["id"]], canon[e["id"]] = e, c
        elif prev != c:
            warnings.add(f"event {e['id']}: two different events share this id; the first in canonical order is used")
            if c < prev:
                by_id[e["id"]], canon[e["id"]] = e, c
    ordered = sorted(by_id.values(), key=_key)

    st = State()
    w = _Writes()
    stage_events: dict[str, list[dict]] = {}
    initial_stage: dict[str, str] = {}
    version_events: dict[str, dict[str, dict]] = {}
    frontier: dict[str, _hlc.Stamp] = {}

    def art(aid: str, e: dict) -> Artifact:
        a = st.artifacts.get(aid)
        if a is None:
            a = st.artifacts[aid] = Artifact(id=aid)
        a.updated = e["hlc"]
        return a

    def apply(e: dict) -> None:
        op, data, aid = e["op"], e.get("data") or {}, e.get("artifact")
        if op not in OPS:
            warnings.add(f"event {e['id']}: unknown op {op!r} ignored")
            return
        if op in ARTIFACT_OPS and not aid:
            warnings.add(f"event {e['id']}: {op} without an artifact ignored")
            return
        required = {"artifact.linked": "to", "version.kept": "version", "decision.resolved": "decision",
                    "task.updated": "task"}.get(op)
        if required and not data.get(required):
            warnings.add(f"event {e['id']}: {op} without `{required}` ignored")
            return
        if op in STAGE_OPS and data.get("to") not in STAGES:
            warnings.add(f"event {e['id']}: {op} to unknown stage {data.get('to')!r} ignored")
            return

        if op == "artifact.created":
            a = art(aid, e)
            if a.created is None:
                a.created = e["hlc"]
                if data.get("stage") in STAGES:
                    initial_stage[aid] = data["stage"]
            for k, v in data.items():
                if k not in RESERVED_FIELDS and k not in STAMP_FIELDS:
                    w.add(("field", aid, k), e, v)
        elif op == "artifact.updated":
            art(aid, e)
            for k, v in data.items():
                if k in STAMP_FIELDS:
                    continue
                if k in RESERVED_FIELDS:
                    warnings.add(f"event {e['id']}: stage cannot be set by artifact.updated; use a stage op")
                else:
                    w.add(("field", aid, k), e, v)
        elif op == "artifact.linked":
            a = art(aid, e)
            link = {"to": data["to"], "rel": data.get("rel") or "related"}
            if link not in a.links:
                a.links.append(link)
        elif op == "version.added":
            art(aid, e)
            vid = data.get("version") or e["id"]
            version_events.setdefault(aid, {}).setdefault(vid, e)  # applied in (hlc, id) order: the first stamp is kept
        elif op == "version.kept":
            art(aid, e)
            w.add(("kept", aid), e, data["version"])
        elif op in STAGE_OPS:
            a = art(aid, e)
            a.stage_history.append({"op": op, "from": data.get("from"), "to": data["to"], **_who(e)})
            stage_events.setdefault(aid, []).append(e)
        elif op == "note.added":
            nid = data.get("note") or e["id"]
            if nid not in st.notes:
                st.notes[nid] = {"id": nid, "artifact": aid, "data": data, **_who(e)}
                if aid:
                    art(aid, e).notes.append(nid)
        elif op == "decision.recorded":
            did = data.get("decision") or e["id"]
            d = st.decisions.setdefault(did, {"id": did, "status": "open", "resolution": None, "history": []})
            if "recorded" not in d:
                d.update({"artifact": aid, "data": data, "recorded": _who(e)})
        elif op == "decision.resolved":
            st.decisions.setdefault(data["decision"], {"id": data["decision"], "status": "open", "resolution": None,
                                                       "history": []})
            w.add(("decision", data["decision"]), e, data)
        elif op == "task.updated":
            st.tasks.setdefault(data["task"], {"id": data["task"], "artifact": aid, "fields": {}, "history": {}})
            for k, v in data.items():
                if k != "task" and k not in STAMP_FIELDS:
                    w.add(("task", data["task"], k), e, v)
        elif op == "clip.decided":
            if aid:
                art(aid, e)
            if isinstance(data.get("clip"), str):
                st.clip_decisions[data["clip"]] = {"artifact": aid, "choice": data.get("choice"), "title": data.get("title"),
                                                   "event": e["id"], **_who(e)}
        elif op == "presence.set":
            # one per device, the latest wins; it never touches an artifact's `updated`. project/presence.py reads `at` and
            # `until` and drops what has run out, so expiry needs no event.
            st.presence[e["device"]] = {"data": data, "artifact": aid, **_who(e)}
        elif op in MEMBERSHIP_OPS:
            st.membership.append({"op": op, "data": data, **_who(e)})
        elif op == "publish.noted":
            if isinstance(data.get("item"), str):     # per device and item, so no computer's note can replace another's
                st.publishing[f"{e['device']}|{data['item']}"] = {"data": data, **_who(e)}

    for e in ordered:
        s = _stamp(e["hlc"])
        if e["device"] not in frontier or s > frontier[e["device"]]:
            frontier[e["device"]] = s
        try:
            apply(e)
        except Exception as exc:  # noqa: BLE001 - one bad event must never stop a project from reading
            warnings.add(f"event {e['id']}: skipped, could not be applied ({type(exc).__name__})")

    for slot in sorted(w.by_slot, key=repr):
        try:
            value, history = w.resolve(slot)
            if slot[0] == "field":
                a = st.artifacts[slot[1]]
                a.fields[slot[2]], a.history[slot[2]] = value, history
            elif slot[0] == "kept":
                a = st.artifacts[slot[1]]
                a.kept, a.history["kept"] = value, history
            elif slot[0] == "decision":
                d = st.decisions[slot[1]]
                d["status"], d["resolution"], d["history"] = "resolved", value, history
            elif slot[0] == "task":
                t = st.tasks[slot[1]]
                t["fields"][slot[2]], t["history"][slot[2]] = value, history
        except Exception as exc:  # noqa: BLE001
            warnings.add(f"{slot[0]} {slot[1]}: could not be merged ({type(exc).__name__})")

    for aid, versions in version_events.items():
        a = st.artifacts[aid]
        for n, (vid, e) in enumerate(sorted(versions.items(), key=lambda kv: _key(kv[1])), 1):
            data = e.get("data") or {}
            a.versions[vid] = {"version": vid, "file": data.get("file"), "data": data, "needs": list(e.get("needs") or []),
                               "number": n, **_who(e)}
            for need in e.get("needs") or []:
                if need not in a.needs:
                    a.needs.append(need)

    for aid, a in st.artifacts.items():
        if aid in stage_events:
            try:
                a.stage, conflict = _stage(aid, stage_events[aid], initial_stage.get(aid, DEFAULT_STAGE))
                if conflict:
                    st.conflicts.append(conflict)
            except Exception as exc:  # noqa: BLE001
                warnings.add(f"artifact {aid}: stage could not be merged ({type(exc).__name__})")
        elif aid in initial_stage:
            a.stage = initial_stage[aid]
        a.kind = a.fields.get("kind") if isinstance(a.fields.get("kind"), str) else None
        a.title = a.fields.get("title") if isinstance(a.fields.get("title"), str) else None

    st.conflicts.sort(key=lambda c: c.id)
    st.frontier = {d: str(s) for d, s in sorted(frontier.items())}
    st.warnings = sorted(warnings)
    return st


def _stage(aid: str, evs: list[dict], initial: str) -> tuple[str, Conflict | None]:
    hs = heads(evs)
    targets = sorted({e["data"]["to"] for e in hs})
    if len(targets) == 1:
        return targets[0], None
    agreed = [e for e in evs if all(saw(h, e) for h in hs)]
    if agreed:
        base = max(agreed, key=_key)["data"]["to"]
    else:
        froms = {e["data"].get("from") for e in hs}
        only = froms.pop() if len(froms) == 1 else None
        base = only if only in STAGES else initial
    options = [{"to": e["data"]["to"], "op": e["op"], **_who(e)} for e in hs]
    cid = "stage:" + aid + ":" + "+".join(e["id"] for e in hs)
    return base, Conflict(id=cid, artifact=aid, field="stage", base=base, options=options)
