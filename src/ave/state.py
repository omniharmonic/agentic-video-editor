"""Episode state machine: where is each edit in the process, and what should the next session do?

State is derived from artifacts on disk (never from memory), so it cannot drift. `ave resume` prints a briefing a
brand-new agent session can act on immediately.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import editdoc, sessions
from .episode import Episode, read_json


@dataclass(frozen=True)
class Stage:
    key: str
    title: str
    needs: tuple[str, ...]          # stage keys that must be done first
    command: str                    # how to run it ({ep} is the episode dir)
    skill: str                      # which skill explains it
    human_gate: bool = False        # stop for human confirmation after this stage
    optional: bool = False


# Order matters: this is the pipeline.
STAGES: tuple[Stage, ...] = (
    Stage("ingest", "Probe footage", (), "ave ingest {ep}", "ave-ingest-transcribe"),
    Stage("audio", "Analysis audio", ("ingest",), "ave audio {ep}", "ave-ingest-transcribe"),
    Stage("transcribe", "Transcript + speakers", ("audio",), "ave transcribe {ep} --speakers N", "ave-ingest-transcribe", True),
    Stage("sync", "Sync cameras", ("audio",), "ave sync {ep}", "ave-multicam-sync", optional=True),
    Stage("map", "Map cameras to speakers", ("transcribe",), "edit episode.json (see skill)", "ave-map-cameras", True),
    Stage("cutplan", "Plan shot/reverse-shot", ("map",), "ave plan-cuts {ep}", "ave-rough-cut"),
    Stage("assembly", "Assembly edit", ("cutplan",), "ave assemble {ep}", "ave-assembly", True),
    Stage("rough", "Rough cut (cleanup)", ("assembly",), "ave cleanup {ep}", "ave-cleanup", True),
    Stage("final", "Final cut (captions, overlays, music, loudness)", ("rough",), "ave final {ep}", "ave-final-cut", True),
    Stage("selects", "A-clip selects", ("transcribe",), "ave clips prepare {ep}", "ave-clip-finder", True, optional=True),
    Stage("content", "Promotion content (posts, thumbnails, carousels)", ("selects",), "ave content plan {ep}", "ave-content-plan", True, optional=True),
    Stage("shorts", "Vertical shorts", ("selects", "final"), "ave shorts {ep}", "ave-shorts", True, optional=True),
    Stage("package", "Release package (video, captions, audio, thumbnails, shorts, copy)", ("final",), "ave package {ep}", "ave-package", True, optional=True),
    Stage("publish", "Publish (approval-gated)", ("final",), "ave publish queue {ep}", "ave-publish", True, optional=True),
    Stage("handoff", "Hand off to DaVinci Resolve", ("assembly",), "ave handoff {ep}", "ave-resolve-handoff", optional=True),
)
BY_KEY = {s.key: s for s in STAGES}


def _done(ep: Episode, key: str) -> bool:
    """A stage is done when its artifact exists (truth on disk), not just because a flag was set."""
    w = ep.work
    per = _per_session(ep)

    def edit_done(stage: str) -> bool:
        """An unnamed edit, or (multi-session) a named edit for EVERY session."""
        return bool(editdoc.list_versions(ep, stage)) or (bool(per) and all(v[stage] for v in per.values()))

    checks = {
        "ingest": lambda: (w / "media.json").exists(),
        "audio": lambda: any((w / "audio").glob("*.wav")) if (w / "audio").is_dir() else False,
        "transcribe": lambda: (w / "transcript.json").exists(),
        "sync": lambda: (w / "sync.json").exists(),
        "map": lambda: _mapped(ep),
        "cutplan": lambda: (w / "cutlist.json").exists() or (bool(per) and all(v["cutlist"] for v in per.values())),
        "assembly": lambda: edit_done("assembly"),
        "rough": lambda: edit_done("rough"),
        "final": lambda: edit_done("final"),
        "selects": lambda: bool(editdoc.list_versions(ep, "select")) or (w / "clips.json").exists(),
        "content": lambda: (w / "content").is_dir() and any((w / "content").glob("*.v*.json")),
        "shorts": lambda: (ep.exports / "shorts").is_dir() and any((ep.exports / "shorts").glob("*.mp4")),
        "package": lambda: _package_built(ep),
        "publish": lambda: (ep.publish / "queue").is_dir() and any((ep.publish / "queue").glob("*.json")),
        "handoff": lambda: (w / "handoff.json").exists(),
    }
    return bool(checks[key]())


def _package_built(ep: Episode) -> bool:
    """Done when `ave package` has built a package (work/package/status.json says so). A dry run is not a build."""
    p = ep.work / "package" / "status.json"
    try:
        return bool(p.is_file() and read_json(p).get("built"))
    except (OSError, ValueError):
        return False


def _per_session(ep: Episode) -> dict[str, dict]:
    try:
        return sessions.stage_status(ep)
    except (OSError, ValueError, KeyError):
        return {}


def _mapped(ep: Episode) -> bool:
    try:
        m = read_json(ep.meta_path)
    except Exception:
        return False
    return bool(m.get("cameras")) and any(s.get("camera") for s in (m.get("speakers") or {}).values())


def _single_camera(ep: Episode) -> bool:
    p = ep.work / "media.json"
    if not p.exists():
        return False
    return sum(1 for f in read_json(p)["files"] if f["kind"] == "video") <= 1


def status(ep: Episode) -> dict:
    ep.require()
    single = _single_camera(ep)
    rows = []
    for s in STAGES:
        if s.key == "content" and not (ep.work / "content").is_dir():
            continue          # the optional content stage exists only once `ave content` has started on this episode: nothing else changes
        done = _done(ep, s.key)
        skipped = s.key == "sync" and single and not done
        blocked = [n for n in s.needs if not _done(ep, n) and not (n == "sync" and single)]
        rows.append({
            "stage": s.key, "title": s.title, "done": done, "skipped": skipped, "optional": s.optional,
            "blocked_by": blocked, "command": s.command.format(ep=ep.root), "skill": s.skill, "human_gate": s.human_gate,
        })
    edits = {st: editdoc.list_versions(ep, st) for st in editdoc.STAGES if editdoc.list_versions(ep, st)}
    per = _per_session(ep)
    if per:    # each session flows through the cut stages separately: name the session still to do
        for r in rows:
            sid = next((s for s, v in per.items() if r["stage"] in v and not v[r["stage"]]), None) if r["stage"] in ("rough", "final") else None
            if sid:
                r["command"] += f" --name {sid}"
            elif r["stage"] == "selects":      # clips are per session: never suggest a command that errors on a multi-session episode
                r["command"] += " --name <session>   (s1, s2, ...)"
        for sid, v in per.items():
            for st in ("assembly", "rough", "final"):
                if v[st]:
                    edits.setdefault(f"{st}.{sid}", editdoc.list_versions(ep, st, sid))
    todo = [r for r in rows if not r["done"] and not r["skipped"] and not r["blocked_by"]]
    core = [r for r in todo if not r["optional"]]
    nxt = (core or todo or [None])[0]
    return {"episode": ep.root.name, "path": str(ep.root), "single_camera": single, "stages": rows,
            "sessions": per, "edits": edits, "next": nxt}


def _channel_layouts(ep: Episode) -> list[str]:
    """`file: layout` for every stereo/mono file ingest classified (speaker_per_channel is the one that changes what you do)."""
    p = ep.work / "media.json"
    if not p.exists():
        return []
    try:
        return [f"{f['file']}: {f['channel_layout']}" for f in read_json(p)["files"] if f.get("channel_layout")]
    except (OSError, ValueError, KeyError):
        return []


def briefing(ep: Episode) -> str:
    """Plain-text orientation for a fresh agent session."""
    st = status(ep)
    meta = read_json(ep.meta_path)
    lines = [f"Episode: {meta.get('title', st['episode'])}  ({st['path']})", ""]
    for r in st["stages"]:
        mark = "x" if r["done"] else ("-" if r["skipped"] else " ")
        tag = " (optional)" if r["optional"] else ""
        gate = "  [human checkpoint]" if r["human_gate"] and r["done"] else ""
        blocked = f"  blocked by {', '.join(r['blocked_by'])}" if r["blocked_by"] and not r["done"] else ""
        lines.append(f"[{mark}] {r['title']}{tag}{gate}{blocked}")
    layouts = _channel_layouts(ep)
    if layouts:
        lines += ["", "Channel layout: " + "; ".join(layouts)]
    if st["sessions"]:
        labels = {s["id"]: s.get("label") for s in sessions.media_sessions(ep)}
        lines += ["", "Sessions (each is its own timeline; run cut stages with --name <session>):"]
        for sid, v in st["sessions"].items():
            tags = " ".join(f"{k} {'x' if v[k] else '-'}" for k in ("cutlist", "assembly", "rough", "final"))
            lines.append(f"  {sid}{' (' + labels[sid] + ')' if labels.get(sid) else ''}: {tags}")
    if st["edits"]:
        lines += ["", "Edits on disk: " + ", ".join(f"{k} v{v[-1]}" for k, v in st["edits"].items())]
    pending = (meta.get("review") or {}).get("pending")
    if pending:
        lines += ["", f"Waiting on the human: {pending}"]
    n = st["next"]
    lines.append("")
    if n:
        gate = " Then STOP and get the human's confirmation." if n["human_gate"] else ""
        lines.append(f"NEXT: {n['title']}. Run: {n['command']}   (skill: {n['skill']}).{gate}")
    else:
        lines.append("NEXT: nothing pending. Ask the human what to do next (selects, shorts, publish).")
    from . import progress
    lines += progress.resume_extras(ep)
    return "\n".join(lines)


def set_review(ep: Episode, pending: str | None) -> None:
    """Record (or clear) what the human must review next. Persisted so the next session knows."""
    from .episode import write_json
    m = read_json(ep.meta_path)
    m.setdefault("review", {})["pending"] = pending
    write_json(ep.meta_path, m)
    from . import progress
    progress.auto_log(ep, f"waiting on the human: {pending}" if pending else "review cleared", "note", refresh_checklist=True)
