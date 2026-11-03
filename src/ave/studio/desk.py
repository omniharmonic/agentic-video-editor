"""Read-only production desk, plus text intake. No credentials or sending hooks.

A desk is a projection of disk state, never another workflow state machine.
"""
from __future__ import annotations

import hashlib
import fcntl
import json
import os
import shlex
import tempfile
from pathlib import Path

from .. import state
from ..episode import Episode
from ..content.sources import parse_markdown
from . import events
from .events import Actor

MAX_TEXT = 200_000
MAX_EPISODES = 100
MAX_STATE_FILE = 2_000_000
MAX_STATE_TOTAL = 16_000_000


def _safe_path(path: Path, root: Path) -> None:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        raise ValueError("project state must stay inside the project") from None
    current = root
    for part in parts:
        current /= part
        if current.is_symlink():
            raise ValueError("symlinked project state is not available on the desk")


def _json_file(path: Path, root: Path) -> dict:
    _safe_path(path, root)
    with path.open("rb") as f:
        raw = f.read(MAX_STATE_FILE + 1)
    if len(raw) > MAX_STATE_FILE:
        raise ValueError("project state exceeds the desk read limit; use the CLI")
    try:
        obj = json.loads(raw)
    except (ValueError, RecursionError):
        raise ValueError("project state is not valid bounded JSON") from None
    if not isinstance(obj, dict):
        raise ValueError("project state must be a JSON object")
    return obj


def _episode_inputs(ep) -> None:
    """Preflight only the metadata trees read by state.status; never walk footage.

    Limits are intentionally a UI boundary, not limits on the editing engine.
    An oversized project stays available through the CLI.
    """
    total, count = 0, 0
    for directory in (ep.work, ep.edits):
        _safe_path(directory, ep.root)
        if not directory.exists():
            continue
        for parent, dirs, files in os.walk(directory, followlinks=False):
            for name in dirs:
                _safe_path(Path(parent) / name, ep.root)
            count += len(dirs) + len(files)
            if count > 2000:
                raise ValueError("project has too many metadata entries for the desk")
            for name in files:
                if not name.endswith(".json"):
                    continue
                path = Path(parent) / name
                _safe_path(path, ep.root)
                size = path.stat().st_size
                total += size
                if size > MAX_STATE_FILE or total > MAX_STATE_TOTAL:
                    raise ValueError("project state exceeds the desk read limit; use the CLI")


def _label(post: dict) -> str:
    return str((post.get("idea") or {}).get("text") or (post.get("copy") or {}).get("base") or post["id"])[:180]


def overview(studio, *, fixtures: bool = False, include_episodes: bool = True) -> dict:
    summary = studio.summary()
    posts = studio.posts()
    if isinstance(posts, dict):
        posts = posts.get("posts", [])
    groups = {"ideas": [], "drafts": [], "review": [], "rework": [], "scheduled": [], "published": []}
    for p in posts:
        row = {"id": p["id"], "rev": p["rev"], "title": _label(p)}
        if p.get("stage") == "idea": groups["ideas"].append(row)
        if p.get("stage") == "draft" or any(
            v.get("status") == "draft" and not v.get("dropped") for v in p.get("variants", [])
        ):
            groups["drafts"].append(row)
        for key, statuses in (("review", {"in_review"}), ("rework", {"needs_rework", "failed", "missed"}),
                              ("scheduled", {"scheduled", "approved"}), ("published", {"published"})):
            matches = [v for v in p.get("variants", []) if v.get("status") in statuses]
            if matches:
                groups[key].append({**row, "variants": [{"id": v["id"], "status": v["status"],
                    "reason": v.get("status_reason"), "account": v.get("account")} for v in matches]})
    account_rows = studio.accounts()
    if isinstance(account_rows, dict):
        account_rows = account_rows.get("accounts", [])
    accounts = [{k: a.get(k) for k in ("id", "label", "platform", "mode", "live", "paused")} | {
        "credentials_ready": bool((a.get("credentials") or {}).get("ready")), "live_verified": False,
    } for a in account_rows]
    sources, episodes, warnings = [], [], []
    sp = studio.sp
    if sp is not None and not fixtures:
        writing = sp.work / "content/writing.json"
        if writing.exists() or writing.is_symlink():
            try:
                files = _json_file(writing, sp.root).get("files", [])
                if not isinstance(files, list) or any(not isinstance(f, dict) for f in files):
                    raise ValueError("invalid writing index")
                for f in files[:200]:
                    sources.append({"kind": "writing", "name": f["file"], "title": f.get("title") or f["file"],
                                    "passages": len(f.get("passages", []))})
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                warnings.append("The writing index could not be read. Re-ingest the source text.")
        # Episodes live alongside studio by the established workspace convention.
        # Never follow symlinked directories or recursively search a user's drive.
        for folder in (sorted(sp.root.parent.iterdir(), key=lambda p: p.name)[:1000] if include_episodes else []):
            marker = folder / "episode.json"
            if folder == sp.root or folder.is_symlink() or not folder.is_dir() or not marker.is_file() or marker.is_symlink():
                continue
            if len(episodes) >= MAX_EPISODES:
                warnings.append(f"Showing the first {MAX_EPISODES} episodes.")
                break
            try:
                _json_file(marker, folder)
                ep = Episode(folder)
                meta = _json_file(ep.meta_path, ep.root)
                _episode_inputs(ep)
                st = state.status(ep)
                nxt = st.get("next")
                pending = (meta.get("review") or {}).get("pending")
                episodes.append({"id": folder.name, "title": meta.get("title") or folder.name,
                    "stages": [{k: r[k] for k in ("stage", "title", "done", "skipped", "human_gate")} for r in st["stages"]],
                    "review": pending, "next": nxt,
                    "resume": shlex.join(["ave", "resume", str(ep.root)])})
            except (OSError, ValueError, TypeError, KeyError, AttributeError, SystemExit):
                episodes.append({"id": folder.name, "title": folder.name, "error": "Project state could not be read; run ave resume for details."})
    if fixtures:
        sources = [{"kind": "writing", "name": "sample-essay.md", "title": "Sample source material", "passages": 4}]
    from ..platforms.registry import platforms
    return {"summary": summary, "groups": {k: {"count": len(v), "items": v[:12]} for k, v in groups.items()},
            "sources": sources, "episodes": episodes, "accounts": accounts, "warnings": warnings,
            "integrations": [
                {"id": "canva", "title": "Canva", "state": "Human connection required", "detail": "Use Canva MCP in your agent. Designs and exports need a real account check."},
                {"id": "resolve", "title": "DaVinci Resolve", "state": "External review", "detail": "Start the Resolve bridge in Resolve, then ask your agent to hand off the edit."},
                {"id": "publishing", "title": "Publishing", "state": "Live verification pending", "detail": "Adapters are covered by synthetic tests. Account setup and a supervised first post are still required.", "platforms": list(platforms())},
            ], "fixtures": fixtures}


def intake(studio, *, kind: str, text: str, actor: Actor, title: str = "") -> dict:
    """An idea or pasted essay, copied into a bounded, content-addressed source.

    No path argument and no URL fetching. Text is untrusted source material.
    """
    if kind not in ("idea", "writing"):
        raise ValueError("kind must be idea or writing")
    if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > MAX_TEXT:
        raise ValueError(f"text must be non-empty and at most {MAX_TEXT} UTF-8 bytes")
    if not isinstance(title, str) or len(title) > 200 or any(c in title for c in "\r\n\x00"):
        raise ValueError("title must be a single line of at most 200 characters")
    if kind == "idea":
        return {"kind": kind, "post": studio.create_post(idea=text.strip(), actor=actor, reason="Added through the studio desk")}
    body = ("# " + title.strip() + "\n\n" if title.strip() else "") + text
    parsed = parse_markdown(body, "source.md")
    if not parsed["passages"]:
        raise ValueError("the source must contain a passage of prose")
    sp = studio.sp
    directory = sp.work / "writing"
    index = sp.work / "content/writing.json"
    lock = sp.work / ".intake.lock"
    sha = hashlib.sha256(body.encode()).hexdigest()
    target = directory / f"source-{sha[:20]}.md"
    # Check all destinations before any mutation. Intake does not rescan the
    # source directory: unrelated files (including symlinks) are never opened.
    destinations = (target, index, index.with_suffix(".json.tmp"), lock,
                    sp.ledger_dir / "events.jsonl", sp.ledger_dir / ".lock")
    def check_destinations():
        for path in destinations:
            _safe_path(path, sp.root)
            if any(path == raw or raw in path.parents for raw in sp.raw_dirs):
                raise ValueError("intake never writes raw folders")
            if path.exists() and not path.is_file():
                raise ValueError("intake destinations must be regular files")
    check_destinations()
    sp.work.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        check_destinations()
        current = _json_file(index, sp.root) if index.exists() else {"version": 1, "files": []}
        files = current.get("files")
        if not isinstance(files, list) or any(not isinstance(f, dict) or not isinstance(f.get("file"), str)
                                              or not isinstance(f.get("passages"), list) for f in files):
            raise ValueError("writing index is invalid; preserve it and repair with the CLI")
        entry = {"file": target.name, "sha256": sha, **parsed}
        merged = {f["file"]: f for f in files}
        merged[target.name] = entry
        rows = [merged[k] for k in sorted(merged)]
        totals = {"files": len(rows), "passages": sum(len(f["passages"]) for f in rows),
                  "sentences": sum(len(p.get("sentences", [])) for f in rows for p in f["passages"])}
        updated = {"version": 1, "dir": str(directory), "files": rows, "totals": totals}
        data = (json.dumps(updated, indent=2, ensure_ascii=False) + "\n").encode()
        if len(data) > MAX_STATE_FILE:
            raise ValueError("writing index exceeds the desk limit; use the CLI for larger collections")
        directory.mkdir(parents=True, exist_ok=True)
        index.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            with target.open("rb") as existing:
                if existing.read(MAX_TEXT + 1000) != body.encode():
                    raise ValueError("source name collision; existing source preserved")
        else:
            _atomic_bytes(target, body.encode())
        _atomic_bytes(index, data)
        events.append_event(sp, events.make_event(actor=actor, op="intake_writing", after_sha=sha,
                            detail={"file": target.name, "title": parsed["title"]}, now=studio.now()))
    return {"kind": kind, "source": {"file": target.name, "title": parsed["title"],
            "passages": [{"id": p["id"], "text": p["text"][:160], "sentences": [x["id"] for x in p["sentences"]]} for p in parsed["passages"]],
            "writing": str(index), "totals": totals, "next": "Ask your agent to plan content from these passages."}}


def _atomic_bytes(path: Path, data: bytes) -> None:
    fd, name = tempfile.mkstemp(prefix=".intake-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
