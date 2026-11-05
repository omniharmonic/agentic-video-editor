"""What the board shows, worked out from a reduced `State`: parts to review after a reopen, open decisions, plain names.

Pure functions over `State`; nothing here reads or writes a file. The MCP tools in `ave.agent` use them, so Claude and the app
read the same answers.
"""

from __future__ import annotations

import re

from . import hlc as _hlc
from .reducer import STAGES, Artifact, State

STAGE_WORDS = {"assembly": "assembly", "rough": "rough cut", "final": "final cut", "select": "selects", "short": "short"}
_VERSION_ID = re.compile(r"^(?P<stage>[a-z]+)(?:\.(?P<name>[\w-]+))?\.v(?P<n>\d+)(?:~(?P<device>[a-z0-9]{4}))?$", re.ASCII)


def name_of(a: Artifact | None) -> str:
    """The artifact's plain name: its title, else its kind, else a neutral phrase. Never its id."""
    if a is None:
        return "an artifact that is not on the board"
    return a.title or a.kind or "an untitled artifact"


def version_name(vid: str, number: int | None = None) -> str:
    """`rough.s2.v8~abcd` -> `rough cut, session 2, version 8` (the number shown is the log's, when given)."""
    m = _VERSION_ID.match(vid or "")
    if not m:
        return f"version {number}" if number else "a version"
    word = STAGE_WORDS.get(m["stage"], m["stage"])
    part = ""
    if m["name"]:
        s = re.fullmatch(r"s(\d+)", m["name"])
        part = f", session {int(s[1])}" if s else f" {m['name'].replace('-', ' ').replace('_', ' ')}"
    return f"{word}{part}, version {number if number else int(m['n'])}"


def next_stage(stage: str) -> str | None:
    i = STAGES.index(stage)
    return STAGES[i + 1] if i + 1 < len(STAGES) else None


def _stamp(text: str | None):
    return _hlc.parse(text) if text else None


def parts_of(state: State, aid: str) -> list[str]:
    """Every artifact reachable from `aid` through `artifact.linked` links (its parts, their parts...), nearest first."""
    seen: list[str] = []
    todo = [aid]
    while todo:
        cur = todo.pop(0)
        a = state.artifacts.get(cur)
        for link in (a.links if a else []):
            t = link["to"]
            if t != aid and t not in seen and t in state.artifacts:
                seen.append(t)
                todo.append(t)
    return seen


def _last_touch(a: Artifact):
    """When someone last dealt with this artifact: its own sign-off, a new version or a keep. A task or field update does not count."""
    stamps = [e["hlc"] for e in a.stage_history if e["op"] == "stage.signed_off"] + [v["hlc"] for v in a.versions.values()]
    stamps += [h["hlc"] for h in a.history.get("kept", [])]
    parsed = [_stamp(s) for s in stamps if s]
    return max(parsed) if parsed else None


def needs_review(state: State) -> dict[str, dict]:
    """Parts flagged because a parent was reopened: `{part: {"because": parent, "since": hlc}}`.

    A parent's reopen flags every part. The flag stays until the part is dealt with afterwards (a new version, a keep, or its own
    sign-off), so nothing goes stale silently and a part that was already redone is not flagged again."""
    flags: dict[str, dict] = {}
    for pid, parent in state.artifacts.items():
        reopens = [e for e in parent.stage_history if e["op"] == "stage.reopened"]
        if not reopens:
            continue
        since = max(reopens, key=lambda e: _stamp(e["hlc"]))["hlc"]
        for part in parts_of(state, pid):
            touched = _last_touch(state.artifacts[part])
            if touched is not None and touched > _stamp(since):
                continue
            if part not in flags or _stamp(since) > _stamp(flags[part]["since"]):
                flags[part] = {"because": pid, "since": since}
    return flags


def decision_open(d: dict) -> bool:
    """True while a decision waits on the director: never resolved, or its latest resolution was undone in the app
    (`decision.resolved` with `reopen`)."""
    return d.get("status") == "open" or bool((d.get("resolution") or {}).get("reopen"))


def open_decisions(state: State, artifact: str | None = None) -> list[dict]:
    """Decisions Claude made alone that the director has not resolved, oldest first; one artifact's, or all."""
    out = []
    for d in state.decisions.values():
        if not decision_open(d) or "data" not in d:
            continue
        if artifact is not None and d.get("artifact") != artifact:
            continue
        data = d["data"]
        row = {"decision": d["id"], "artifact": d.get("artifact"), "summary": data.get("summary"),
               "options": data.get("options") or [], "chosen": data.get("chosen"), "reason": data.get("reason"),
               "recorded": d["recorded"]["hlc"]}
        if data.get("kind") == "setting":            # two copies of a settings file: what each says, key by key
            row.update({"kind": "setting", "file": data.get("file"), "whole": bool(data.get("whole")),
                        "keys": data.get("keys") or [], "file_who": data.get("file_who"), "copy_who": data.get("copy_who"),
                        "file_label": data.get("file_label"), "copy_label": data.get("copy_label")})
            if data.get("whole"):                    # both whole files, capped, so they can be shown in chat
                row["texts"] = {k: _capped(v) for k, v in (data.get("texts") or {}).items() if isinstance(v, str)}
        out.append(row)
    return sorted(out, key=lambda x: _stamp(x["recorded"]))


TEXT_CAP = 2000


def _capped(t: str) -> str:
    return t if len(t) <= TEXT_CAP else t[:TEXT_CAP] + "\n… (cut here; the rest is on the board)"


def _person(o: dict) -> str:
    who = "Claude" if o.get("actor") == "claude" else (o.get("person") or "Someone")
    return who[:1].upper() + who[1:]


def _and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def stage_conflict(state: State, c) -> dict:
    """A stage changed on two computers at once, in words: {text, base, choices: [{stage, who, label}]}, choices in stage
    order. "Maren and Imani both changed Refine": both moved it on from the stage they agreed on, and neither saw the other."""
    a = state.artifacts.get(c.artifact)
    by: dict[str, list[str]] = {}
    for o in c.options:
        names = by.setdefault(o["to"], [])
        if _person(o) not in names:
            names.append(_person(o))
    people: list[str] = []
    for o in sorted(c.options, key=lambda o: _stamp(o["hlc"])):
        if _person(o) not in people:
            people.append(_person(o))
    if len(people) == 1:
        people = [people[0], f"{people[0]} on another computer"]
    order = [s for s in STAGES if s in by]
    choices = [{"stage": s, "who": _and(by[s]), "label": f"{s}, as {_and(by[s])} set it"} for s in order]
    return {"id": c.id, "artifact": c.artifact, "name": name_of(a), "base": c.base,
            "text": f"{_and(people)} both changed {c.base}",
            "detail": "; ".join(f"{ch['who']} moved it to {ch['stage']}" for ch in choices) + ". Both are kept until you choose.",
            "choices": choices}


def resolve_artifact(state: State, ref: str, folder: str | None = None, home: str | None = None) -> str | None:
    """An artifact id from what Claude passed: the id itself, else a title that matches exactly one artifact (any case), else the
    project's home artifact for the project's own folder name (the way the Room names an episode)."""
    if ref in state.artifacts:
        return ref
    hits = [i for i, a in state.artifacts.items() if a.title and a.title.casefold() == ref.casefold()]
    if len(hits) == 1:
        return hits[0]
    return home if home in state.artifacts and ref == folder else None
