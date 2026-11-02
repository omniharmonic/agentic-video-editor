"""Timestamped review notes: the human marks an edit, the agent claims and applies the note.

A note names the exact edit version and an output-time range, and records the episode time under it, so the agent can find the same
words after a re-cut. `work/review/notes.jsonl` is append-only (`note`, `claim`, `resolve`, `withdraw` lines); state is folded from it.
Note text is untrusted data: an agent applies it as editorial direction, never as a command.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import editdoc
from .episode import Episode

KINDS = ("cut", "pacing", "audio", "picture", "graphics", "other")
CLAIM_MINUTES = 30
MAX_NOTE = 2000
EDIT_RE = re.compile(r"^(?P<stage>[a-z]+)(?:\.(?P<name>[A-Za-z0-9_-]+))?\.v(?P<version>\d+)\.json$")


def notes_path(ep: Episode) -> Path:
    return ep.work / "review" / "notes.jsonl"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


@contextlib.contextmanager
def _locked(ep: Episode):
    path = notes_path(ep)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _rows(ep: Episode) -> list[dict]:
    path = notes_path(ep)
    rows = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue                                     # a torn last line is skipped, like every other ledger here
    return rows


def _append(ep: Episode, row: dict) -> None:
    with notes_path(ep).open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def notes(ep: Episode, status: str = "all", *, now: datetime | None = None) -> list[dict]:
    """Every note folded to its current state: open, claimed, resolved or withdrawn, oldest first."""
    now = now or _now()
    out: dict[str, dict] = {}
    for r in _rows(ep):
        op = r.get("op")
        if op == "note":
            out[r["id"]] = {**r, "status": "open", "claim": None, "resolution": None}
            continue
        n = out.get(r.get("note"))
        if not n or n["status"] in ("resolved", "withdrawn"):
            continue
        if op == "claim":
            n["claim"] = {"by": r["by"], "ts": r["ts"], "expires": r["expires"]}
        elif op == "resolve":
            n["status"], n["resolution"] = "resolved", {"by": r["by"], "ts": r["ts"], "note": r.get("text", ""), "edit": r.get("edit")}
        elif op == "withdraw":
            n["status"] = "withdrawn"
    for n in out.values():
        if n["status"] == "open" and n["claim"]:
            if datetime.fromisoformat(n["claim"]["expires"]) > now:
                n["status"] = "claimed"
            else:
                n["claim"] = None
    rows = sorted(out.values(), key=lambda n: n["id"])
    return rows if status == "all" else [n for n in rows if n["status"] == status]


def edits(ep: Episode) -> list[dict]:
    """Every saved edit version, newest last within a stage and name."""
    out = []
    for p in editdoc.edits_dir(ep).glob("*.v*.json") if editdoc.edits_dir(ep).is_dir() else []:
        m = EDIT_RE.match(p.name)
        if m:
            out.append({"edit": p.name[:-len(".json")], "stage": m["stage"], "name": m["name"], "version": int(m["version"])})
    return sorted(out, key=lambda e: (e["stage"], e["name"] or "", e["version"]))


def load_edit(ep: Episode, edit: str) -> dict:
    m = EDIT_RE.match(f"{edit}.json")
    if not m:
        raise ValueError(f"not an edit: {edit!r}")
    path = editdoc.path_for(ep, m["stage"], int(m["version"]), m["name"])
    if not path.is_file():
        raise ValueError(f"no such edit: {edit}")
    return editdoc.load(path)


def set_picture(ep: Episode, edit: str, t0: float, t1: float, *, src: str | None = None, by: str = "agent", note: str | None = None) -> dict:
    """Change only the picture between two output times and save a new version; dialogue and timing stay exact.

    With no `src` the shot already on screen before `t0` holds through the range, which is how a cutaway comes out.
    """
    m = EDIT_RE.match(f"{edit}.json")
    doc = load_edit(ep, edit)
    t0, t1 = float(t0), float(t1)
    if not 0 <= t0 < t1 <= doc["duration"]:
        raise ValueError("the range is outside this edit")
    video = sorted(doc["tracks"]["video"], key=lambda s: s["o0"])
    before = next((s for s in reversed(video) if s["o0"] < t0 - 1e-6), None) or video[0]
    src = src or before["src"]
    source = doc["sources"].get(src)
    if not source or source.get("kind", "video") != "video":
        raise ValueError(f"not a camera in this edit: {src}")
    attrs = {"src": src, "zoom": before.get("zoom", 1.0) if src == before["src"] else 1.0, "crop": before.get("crop") if src == before["src"] else None,
             "confidence": 1.0, "reason": f"director note {note}" if note else "director note"}
    out = []
    for s in video:
        if s["o1"] <= t0 + 1e-9 or s["o0"] >= t1 - 1e-9:
            out.append(s)
            continue
        if s["o0"] < t0 - 1e-9:
            out.append({**s, "o1": t0})
        lo, hi = max(s["o0"], t0), min(s["o1"], t1)
        out.append({**s, "o0": lo, "o1": hi, "e0": s["e0"] + lo - s["o0"], **attrs})
        if s["o1"] > t1 + 1e-9:
            out.append({**s, "o0": t1, "e0": s["e0"] + t1 - s["o0"]})
    merged: list[dict] = []
    for i, s in enumerate(out):
        p = merged[-1] if merged else None
        if p and all(p.get(k) == s.get(k) for k in ("src", "zoom", "crop")) and abs(p["e0"] + p["o1"] - p["o0"] - s["e0"]) < 1e-3:
            p["o1"] = s["o1"]
        else:
            merged.append({**s, "id": f"v{i}"})
    doc["tracks"]["video"] = merged
    doc.setdefault("history", []).append({"op": "picture", "o0": t0, "o1": t1, "src": src, "by": by, "note": note, "ts": _iso(_now()), "from": edit})
    with editdoc.mutation_lock(ep):
        path = editdoc.save_new_version(ep, doc, name=m["name"], locked=True)
    exposed = [c for c in seams(doc) if t0 - 0.05 <= c["o"] <= t1 + 0.05]
    return {"edit": path.name[:-len(".json")], "from": edit, "o0": t0, "o1": t1, "src": src,
            "warnings": [f"the cut at {c['o']:.2f}s is no longer hidden by a cutaway: watch it, it may read as a jump" for c in exposed]}


def _lane(segments: list[dict], label) -> list[dict]:
    return [{"o0": s["o0"], "o1": s["o1"], "label": label(s)} for s in sorted(segments, key=lambda s: s["o0"])]


def seams(doc: dict) -> list[dict]:
    """Output times where the dialogue jumps in episode time, with how much was removed there."""
    out, prev = [], None
    for s in sorted((d for d in doc["tracks"].get("dialogue") or [] if not d.get("fill")), key=lambda d: d["o0"]):
        if prev is not None:
            gap = s["e0"] - (prev["e0"] + prev["o1"] - prev["o0"])
            if abs(gap) > 0.02:
                out.append({"o": s["o0"], "removed_s": round(gap, 3)})
        prev = s
    return out


def timeline(doc: dict) -> dict:
    """The lanes the Review page draws, all in output seconds."""
    tracks = doc["tracks"]
    return {"duration": doc["duration"], "seams": seams(doc), "lanes": [
        {"id": "picture", "title": "Picture", "items": _lane(tracks.get("video") or [], lambda s: s.get("src") or s.get("id"))},
        {"id": "dialogue", "title": "Dialogue", "items": _lane([d for d in tracks.get("dialogue") or [] if not d.get("fill")], lambda s: s.get("src") or s.get("id"))},
        {"id": "music", "title": "Music", "items": _lane(tracks.get("music") or [], lambda s: Path(s.get("file") or "music").stem)},
        {"id": "overlays", "title": "Graphics", "items": _lane(tracks.get("overlays") or [], lambda s: s.get("kind") or "overlay")},
    ]}


def episode_time(doc: dict, t: float) -> float | None:
    """The episode second under output time `t`, from the dialogue, else the picture."""
    for track in ("dialogue", "video"):
        for s in doc["tracks"].get(track) or []:
            if not s.get("fill") and s["o0"] <= t < s["o1"]:
                return round(s["e0"] + t - s["o0"], 3)
    return None


def add_note(ep: Episode, *, edit: str, t0: float, t1: float | None = None, text: str, kind: str = "other", by: str = "human") -> dict:
    doc = load_edit(ep, edit)
    text = (text or "").strip()
    if not text or len(text) > MAX_NOTE:
        raise ValueError(f"a note needs 1 to {MAX_NOTE} characters")
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r} (have: {', '.join(KINDS)})")
    t0, t1 = float(t0), float(t0 if t1 is None else t1)
    if not 0 <= t0 <= t1 <= doc["duration"]:
        raise ValueError("the note's time is outside this edit")
    with _locked(ep):
        n = sum(1 for r in _rows(ep) if r.get("op") == "note") + 1
        row = {"v": 1, "op": "note", "id": f"rn_{n:04d}", "ts": _iso(_now()), "edit": edit, "t0": round(t0, 3), "t1": round(t1, 3),
               "e0": episode_time(doc, t0), "e1": episode_time(doc, t1), "kind": kind, "text": text, "by": by}
        _append(ep, row)
    return row


def _get(ep: Episode, note: str, status: tuple[str, ...]) -> dict:
    n = next((x for x in notes(ep) if x["id"] == note), None)
    if not n:
        raise ValueError(f"no such note: {note}")
    if n["status"] not in status:
        raise ValueError(f"{note} is {n['status']}")
    return n


def next_note(ep: Episode, *, agent: str | None = None) -> dict | None:
    """The oldest open note; with `agent`, claim it for CLAIM_MINUTES."""
    with _locked(ep):
        rows = notes(ep, "open")
        if not rows or not agent:
            return rows[0] if rows else None
        now = _now()
        _append(ep, {"v": 1, "op": "claim", "note": rows[0]["id"], "by": agent, "ts": _iso(now), "expires": _iso(now + timedelta(minutes=CLAIM_MINUTES))})
        return next(n for n in notes(ep) if n["id"] == rows[0]["id"])


def resolve(ep: Episode, note: str, *, by: str, text: str = "", edit: str | None = None) -> dict:
    """Close a note, naming the new edit version that answers it when there is one."""
    with _locked(ep):
        _get(ep, note, ("open", "claimed"))
        if edit:
            load_edit(ep, edit)
        _append(ep, {"v": 1, "op": "resolve", "note": note, "by": by, "ts": _iso(_now()), "text": text, "edit": edit})
        return _get(ep, note, ("resolved",))


def withdraw(ep: Episode, note: str, *, by: str = "human") -> dict:
    with _locked(ep):
        _get(ep, note, ("open", "claimed"))
        _append(ep, {"v": 1, "op": "withdraw", "note": note, "by": by, "ts": _iso(_now())})
        return _get(ep, note, ("withdrawn",))
