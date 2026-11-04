"""Post store: one JSON file per post under `posts/`, compare-and-swap on `rev` (spec 2.2). Ledger helpers are re-exported here.

    load_post / list_posts / save_post          posts/<id>.json, atomic write, flock `posts/<id>.lock`, event on every write
    append_event / append_feedback / open_feedback    (see events.py, ledger.py)
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import stat
from contextlib import contextmanager, nullcontext
from datetime import datetime
from pathlib import Path

from ..episode import read_json, write_json
from ..publish import safety
from . import events as _events, ledger as _ledger
from .clock import SystemClock
from .events import Actor
from .model import RevConflict, check_id, require_valid, stage_for


def post_path(sp, post_id: str) -> Path:
    return sp.posts_dir / f"{check_id(post_id, 'post id')}.json"


def post_sha(post: dict) -> str:
    """Content hash of a post as stored (used in events as before_sha/after_sha)."""
    return hashlib.sha256(safety.canonical(post).encode()).hexdigest()


def check_write_paths(sp, post_id: str) -> None:
    """Refuse planted store sidecars before a write can touch their targets.

    This protects ordinary commands from following a stale/malicious symlink;
    it is not isolation from another process with the same user's authority.
    """
    path = post_path(sp, post_id)
    for p in (path, path.with_suffix('.json.tmp'), path.with_suffix('.lock'),
              sp.ledger_dir / 'events.jsonl', sp.ledger_dir / '.lock'):
        try:
            relative = p.relative_to(sp.root)
        except ValueError:
            raise ValueError('post storage must stay inside the project') from None
        cursor = sp.root
        for part in relative.parts:
            cursor /= part
            if cursor.is_symlink():
                raise ValueError('post storage and sidecars must not be symlinks')
        try:                    # one lstat: a racing writer may rename the .tmp away between an exists() and an is_file()
            mode = p.lstat().st_mode
        except FileNotFoundError:
            mode = None
        if mode is not None and not stat.S_ISREG(mode):
            raise ValueError('post storage sidecars must be regular files')
        if any(p == raw or raw in p.parents for raw in sp.raw_dirs):
            raise ValueError('post storage must not write into raw folders')


@contextmanager
def post_lock(sp, post_id: str):
    """Serialize read-check-write of one post (same pattern as queue._lock, but BLOCKING: edits are milliseconds)."""
    sp.posts_dir.mkdir(parents=True, exist_ok=True)
    f = open(post_path(sp, post_id).with_suffix(".lock"), "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        f.close()


def load_post(sp, post_id: str) -> dict:
    p = post_path(sp, post_id)
    if not p.is_file():
        raise FileNotFoundError(f"no post {post_id!r} in {sp.posts_dir}")
    return read_json(p)


def list_posts(sp, *, stage: str | None = None, campaign: str | None = None, account: str | None = None,
               platform: str | None = None, q: str | None = None) -> list[dict]:
    """All posts, sorted by id, optionally filtered. `q` is a case-insensitive substring over the idea, copy and variant captions.
    (Status filters need the queue; they live in the service layer, which derives it.)"""
    if not sp.posts_dir.is_dir():
        return []
    out = []
    for p in sorted(sp.posts_dir.glob("*.json")):
        try:
            post = read_json(p)
        except (OSError, ValueError):
            continue
        if stage and post.get("stage") != stage:
            continue
        if campaign and post.get("campaign") != campaign:
            continue
        vs = post.get("variants") or []
        if account and not any(v.get("account") == account for v in vs):
            continue
        if platform and not any(v.get("platform") == platform for v in vs):
            continue
        if q:
            hay = json.dumps([post.get("idea"), post.get("copy"), [v.get("copy") for v in vs]], ensure_ascii=False).lower()
            if q.lower() not in hay:
                continue
        out.append(post)
    return out


def save_post(sp, post: dict, *, expected_rev: int | None, actor: Actor, op: str, detail: dict | None = None,
              variant: str | None = None, now: datetime | None = None, _commit_context=None) -> dict:
    """Write `post` if and only if the stored `rev` equals `expected_rev`; the stored rev becomes `expected_rev + 1`.

    `expected_rev=None` means CREATE: the post must not exist yet (it is written at rev 1). Any mismatch raises
    `RevConflict(latest)` carrying the stored post, and writes nothing. The post is schema-validated and its `stage` recomputed
    (`dropped` is sticky) BEFORE anything is written; an event (actor, op, rev, before/after sha) is appended after the write.
    Returns the post as stored."""
    pid = check_id(post["id"], "post id")
    check_write_paths(sp, pid)
    with post_lock(sp, pid):
        p = post_path(sp, pid)
        current = read_json(p) if p.is_file() else None
        if expected_rev is None:
            if current is not None:
                raise RevConflict(current, f"post {pid} already exists")
            new_rev = 1
        else:
            if current is None:
                raise RevConflict(None, f"post {pid} does not exist")
            if current.get("rev") != expected_rev:
                raise RevConflict(current, f"post {pid} is at rev {current.get('rev')}, not {expected_rev}")
            new_rev = expected_rev + 1
        out = json.loads(json.dumps(post))                 # detach from the caller's object
        out["rev"] = new_rev
        out["stage"] = stage_for(out)
        out["updated"] = (now or SystemClock().now()).strftime("%Y-%m-%dT%H:%M:%S%z")
        require_valid("post", out)
        # A trusted caller may bind current native eligibility to this exact
        # CAS. Never supplied by request JSON; no media hashing belongs here.
        with _commit_context() if _commit_context else nullcontext():
            write_json(p, out)
        _events.append_event(sp, _events.make_event(
            actor=actor, op=op, post=pid, variant=variant, rev=new_rev, before_sha=post_sha(current) if current else None,
            after_sha=post_sha(out), detail=detail, now=now), now=now)
        return out


def append_event(sp, ev: dict) -> None:
    _events.append_event(sp, ev)


def append_feedback(sp, line: dict) -> dict:
    return _ledger.append_feedback(sp, line)


def open_feedback(sp, **f) -> list[dict]:
    return _ledger.open_feedback(sp, **f)


def read_assets(sp) -> dict[str, dict]:
    """library/assets.jsonl folded to id -> line (a later line for the same id replaces an earlier one). Read-only; the registry
    API proper (add_file, search, coverage) is WS1's `ave.library`."""
    rows, _ = _ledger.read_jsonl(sp.library_dir / "assets.jsonl")
    return {r["id"]: r for r in rows if r.get("id")}
