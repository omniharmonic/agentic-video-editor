"""Approval queue: one JSON file per post under <episode>/publish/queue/<id>.json.

Status machine:  draft -> awaiting_approval -> approved -> publishing -> published | failed   (+ rejected)

Nothing is sent unless ALL hold: status approved/publishing/failed WITH a valid signed approval, the file's current
sha256 equals the approved one, the metadata hash equals the approved one, the caller passed `send=True`
(the CLI's `--yes`), and every platform passes preflight. Per-platform results make re-runs idempotent: a
published platform is never re-sent, and a platform left in state "sending" by a crash is UNKNOWN, never resent
until recover() proves it absent or a human resolves it.
"""

from __future__ import annotations

import fcntl
import re
import secrets
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from ..episode import Episode, read_json, write_json
from . import safety
from .adapters import ALL_PLATFORMS, get_publisher
from .adapters.base import Absent, PublishError, Publisher, image_problems, load_platforms, probe_video

log = safety.get_logger("ave.publish.queue")

STATUSES = ("draft", "awaiting_approval", "approved", "publishing", "published", "failed", "rejected")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def queue_dir(ep: Episode) -> Path:
    return ep.publish / "queue"


def _path(ep: Episode, item_id: str) -> Path:
    if not _ID_RE.match(item_id) or ".." in item_id:
        raise ValueError(f"bad queue id {item_id!r}")
    return queue_dir(ep) / f"{item_id}.json"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:30] or "post"


def resolve_video(ep: Episode, item: dict) -> Path:
    p = Path(item["video"])
    return p if p.is_absolute() else ep.root / p


MEDIA_TYPES = ("video", "image", "carousel")


def media_type(item: dict) -> str:
    """`video` for every item queued before images existed (the key is absent), so old items behave exactly as before."""
    return item.get("media_type") or "video"


def resolve_files(ep: Episode, item: dict) -> list[Path]:
    """Every file the item publishes, in order (a video: one file; an image: one; a carousel: every slide)."""
    if media_type(item) == "video":
        return [resolve_video(ep, item)]
    return [(Path(f) if Path(f).is_absolute() else ep.root / f) for f in item["files"]]


def current_sha(ep: Episode, item: dict) -> str:
    """The hash of what is on disk NOW, the same way the approval hash was made (see safety.media_sha256)."""
    return safety.media_sha256(resolve_files(ep, item), media_type(item))


def load(ep: Episode, item_id: str) -> dict:
    p = _path(ep, item_id)
    if not p.exists():
        raise FileNotFoundError(f"no queue item {item_id!r} in {queue_dir(ep)}")
    return read_json(p)


def save(ep: Episode, item: dict) -> None:
    write_json(_path(ep, item["id"]), item)


def list_items(ep: Episode) -> list[dict]:
    d = queue_dir(ep)
    return [read_json(p) for p in sorted(d.glob("*.json"))] if d.is_dir() else []


def _event(item: dict, what: str, **kw) -> None:
    item.setdefault("events", []).append({"at": now_iso(), "event": what, **safety.redact_obj(kw)})


# --------------------------------------------------------------------------- creating

def preflight_item(ep: Episode, item: dict, publishers: dict[str, Publisher] | None = None) -> list[str]:
    """All problems across all platforms. Pure (no network)."""
    mt = media_type(item)
    files = resolve_files(ep, item)
    missing = [f for f in files if not f.is_file()]
    if missing:
        return [f"{'video' if mt == 'video' else 'image'} not found: {m}" for m in missing]
    video = files[0]
    problems: list[str] = []
    if not item.get("title"):
        problems.append("title is required")
    if not item.get("platforms"):
        problems.append("at least one platform is required")
    for name in item.get("platforms", []):
        if name not in ALL_PLATFORMS:
            problems.append(f"unknown platform {name!r}")
            continue
        pub = (publishers or {}).get(name) or get_publisher(name)
        if mt != "video":
            problems += image_problems(pub.cfg if hasattr(pub, "cfg") else load_platforms(), name, item, files)
        try:
            problems += pub.preflight(item, video)
        except ValueError as e:
            problems.append(str(e))
    return problems


def _stored(ep: Episode, path: str | Path) -> tuple[Path, str]:
    vp = Path(path).expanduser()
    vp = vp if vp.is_absolute() else Path.cwd() / vp
    vp = vp.resolve()
    if not vp.is_file():
        raise FileNotFoundError(f"file not found: {vp}")
    try:
        return vp, str(vp.relative_to(ep.root))
    except ValueError:
        return vp, str(vp)


def create(ep: Episode, *, video: str | Path | None = None, platforms: list[str], title: str, description: str = "",
           tags: list[str] | None = None, caption: str = "", schedule: str | None = None,
           visibility: str = "private", ai_disclosure: bool | None = None, made_for_kids: bool = False,
           fmt: str = "auto", publishers: dict[str, Publisher] | None = None,
           media_type: str = "video", files: list[str | Path] | None = None, alt_texts: list[str] | None = None,
           origin: dict | None = None) -> dict:
    """Queue a post. Visibility defaults to private. The file is hashed now; status becomes awaiting_approval only if
    preflight passes (otherwise it stays draft with the problems recorded).

    `media_type` is `video` (default; `video=` names the file, exactly as before), `image` (one file in `files`) or `carousel`
    (several, in order). The approval hash covers EVERY file (see safety.media_sha256) and image items bind their alt texts."""
    if media_type not in MEDIA_TYPES:
        raise ValueError(f"media_type must be one of {', '.join(MEDIA_TYPES)}")
    if media_type == "video":
        if video is None:
            raise ValueError("video is required for a video item")
        vp, stored = _stored(ep, video)
        hashed, stored_files = safety.sha256_file(vp), None
    else:
        if not files:
            raise ValueError(f"{media_type} items need files=[...]")
        pairs = [_stored(ep, f) for f in files]
        vp, stored = pairs[0][0], pairs[0][1]
        stored_files = [s for _, s in pairs]
        hashed = safety.media_sha256([p for p, _ in pairs], media_type)
    if visibility not in ("private", "unlisted", "public"):
        raise ValueError("visibility must be private, unlisted or public")
    if fmt not in ("auto", "short", "long"):
        raise ValueError("format must be auto, short or long")
    item_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{_slug(title)}-{secrets.token_hex(2)}"
    item = {
        "id": item_id, "video": stored, "sha256": hashed, "platforms": list(dict.fromkeys(platforms)),
        "title": title, "description": description, "tags": tags or [], "caption": caption, "schedule": schedule,
        "visibility": visibility, "ai_disclosure": ai_disclosure, "made_for_kids": bool(made_for_kids), "format": fmt,
        "status": "draft", "approvals": [], "results": {}, "created": now_iso(), "events": [],
    }
    if media_type != "video":      # absent for video, so a video item's file is byte-for-byte what it was before images existed
        item.update(media_type=media_type, files=stored_files, alt_texts=list(alt_texts or []))
        item["file_sha256"] = [safety.sha256_file(p) for p in resolve_files(ep, item)]
    if origin:
        item["origin"] = origin
    problems = preflight_item(ep, item, publishers)
    item["preflight_problems"] = problems
    item["status"] = "draft" if problems else "awaiting_approval"
    _event(item, "queued", problems=len(problems))
    save(ep, item)
    return item


# --------------------------------------------------------------------------- approving

def approve(ep: Episode, item_id: str, by: str, confirm_sha: str | None = None, publishers=None, source: str = "cli") -> dict:
    """Record who approved which bytes (`source`: the human surface used, "cli" or "ui"; recorded, not part of the signature). `confirm_sha` (>= 8 hex chars of the file hash the human was shown) must match,
    so approval is bound to what was reviewed rather than to whatever happens to be on disk later."""
    item = load(ep, item_id)
    if item["status"] not in ("awaiting_approval", "draft"):
        raise ValueError(f"cannot approve an item in status {item['status']!r}")
    if not by.strip():
        raise ValueError("approver name required")
    sha = current_sha(ep, item)
    if not confirm_sha or len(confirm_sha) < 8 or not sha.startswith(confirm_sha.lower()):
        raise ValueError(f"confirmation hash does not match the file (current sha256 starts {sha[:12]}). "
                         "Review the exact file, then pass its hash prefix.")
    item["sha256"] = sha
    if media_type(item) != "video":
        item["file_sha256"] = [safety.sha256_file(p) for p in resolve_files(ep, item)]
    problems = preflight_item(ep, item, publishers)
    item["preflight_problems"] = problems
    if problems:
        item["status"] = "draft"
        save(ep, item)
        raise ValueError("preflight problems block approval: " + "; ".join(problems))
    approval = safety.make_approval(item, sha, by.strip(), now_iso())
    approval["source"] = source
    item["approvals"].append(approval)
    item["status"] = "approved"
    _event(item, "approved", by=by, sha256=sha, source=source)
    save(ep, item)
    return item


def reject(ep: Episode, item_id: str, by: str, reason: str = "") -> dict:
    item = load(ep, item_id)
    if item["status"] == "published":
        raise ValueError("already published; nothing to reject")
    if item["status"] == "publishing" or any(r.get("state") == "sending" for r in item.get("results", {}).values()):
        raise ValueError("a publish is in flight or uncertain; resolve it first")
    item["status"] = "rejected"
    item["approvals"] = []   # a rejection voids any earlier approval
    _event(item, "rejected", by=by, reason=reason)
    save(ep, item)
    return item


# --------------------------------------------------------------------------- running

@contextmanager
def _lock(ep: Episode, item_id: str):
    """One run per item at a time (two terminals must never both post)."""
    p = _path(ep, item_id).with_suffix(".lock")
    f = open(p, "w")
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f"another `ave publish run` is already working on {item_id}")
        yield
    finally:
        f.close()


def send_blockers(ep: Episode, item: dict) -> list[str]:
    """Reasons this item may not be sent now (approval, hash, file). Does not touch the network."""
    if item["status"] == "published":
        return ["already published"]
    gone = [f for f in resolve_files(ep, item) if not f.is_file()]
    if gone:
        return [f"{'video' if media_type(item) == 'video' else 'image'} not found: {g}" for g in gone]
    return safety.approval_problems(item, current_sha(ep, item))


def run(ep: Episode, item_id: str, *, send: bool = False, publishers: dict[str, Publisher] | None = None,
        progress_cb: Callable[[str], None] | None = None) -> dict:
    """Dry-run by default. With send=True, publish every not-yet-published platform of an approved item.

    Returns {"mode", "ok", "blocked": [...], "platforms": {name: outcome}}. Dry-run never writes to the queue.
    """
    publishers = dict(publishers or {})
    with _lock(ep, item_id):
        item = load(ep, item_id)
        report: dict = {"id": item_id, "mode": "send" if send else "dry-run", "ok": False, "blocked": [], "platforms": {}}
        if item["status"] == "published" and all(r.get("state") == "published" for r in item["results"].values()):
            report.update(ok=True, note="already published; nothing sent")
            return report
        blockers = send_blockers(ep, item)
        pre = preflight_item(ep, item, publishers)
        report["blocked"] = blockers + pre
        todo = [p for p in item["platforms"] if item["results"].get(p, {}).get("state") != "published"]
        for p in todo:
            st = item["results"].get(p, {}).get("state")
            report["platforms"][p] = {"would": "check-recovery" if st == "sending" else "publish"}
        if report["blocked"]:
            if send and blockers and any("sha256" in b or "changed" in b for b in blockers) and item["status"] != "published":
                item["status"] = "awaiting_approval"      # approval no longer valid for these bytes/metadata
                _event(item, "approval_invalidated", reasons=blockers)
                save(ep, item)
            return report
        if not send:
            report["ok"] = True
            return report
        for name in todo:
            ready = (publishers.get(name) or get_publisher(name)).ready_problems()
            if ready:
                report["blocked"] += [f"{name}: {r}" for r in ready]
        if report["blocked"]:
            return report

        item["status"] = "publishing"
        _event(item, "run_started")
        save(ep, item)
        video = resolve_files(ep, item)[0]
        for name in todo:
            pub = publishers.get(name) or get_publisher(name)
            rec = item["results"].get(name, {})
            out: dict
            if rec.get("state") == "sending":
                verdict = pub.recover(item, rec)
                if isinstance(verdict, dict):
                    item["results"][name] = {**rec, **safety.redact_obj(verdict), "state": "published", "at": now_iso()}
                    report["platforms"][name] = {"outcome": "recovered_published"}
                    save(ep, item)
                    continue
                if isinstance(verdict, Absent):
                    _event(item, "recovered_absent", platform=name, detail=verdict.detail)
                else:
                    report["platforms"][name] = {"outcome": "uncertain",
                                                 "detail": "a previous attempt may have posted; verify on the platform then run "
                                                           "`ave publish resolve` (--posted URL or --retry)"}
                    continue
            # Re-verify the bytes immediately before each send: closes the window between check and use.
            again = safety.approval_problems(item, current_sha(ep, item))
            if again:
                report["platforms"][name] = {"outcome": "blocked", "detail": "; ".join(again)}
                continue
            item["results"][name] = {"state": "sending", "started": now_iso()}
            save(ep, item)             # BEFORE the network call: a crash leaves "sending", never nothing

            def progress(partial: dict, _n=name) -> None:
                item["results"][_n].update(safety.redact_obj(partial))
                save(ep, item)

            try:
                to_send = item if media_type(item) == "video" else {**item, "_files": [str(f) for f in resolve_files(ep, item)]}
                result = pub.publish(to_send, video, progress)
            except PublishError as e:
                item["results"][name] = {**item["results"][name], "state": "failed", "error": safety.redact(e), "at": now_iso()}
                report["platforms"][name] = {"outcome": "failed", "error": safety.redact(e)}
                log.warning("publish to %s failed: %s", name, e)
                save(ep, item)
                continue
            except Exception as e:     # unknown outcome: keep "sending"
                item["results"][name]["error"] = safety.redact(f"{type(e).__name__}: {e}")
                report["platforms"][name] = {"outcome": "uncertain", "error": item["results"][name]["error"]}
                log.error("publish to %s ended uncertain: %s", name, e)
                save(ep, item)
                continue
            item["results"][name] = {**item["results"][name], **safety.redact_obj(result), "state": "published", "at": now_iso()}
            report["platforms"][name] = {"outcome": "published", **safety.redact_obj(result)}
            save(ep, item)

        states = [item["results"].get(p, {}).get("state") for p in item["platforms"]]
        item["status"] = "published" if all(s == "published" for s in states) else "failed"
        _event(item, "run_finished", status=item["status"])
        save(ep, item)
        report["ok"] = item["status"] == "published"
        report["status"] = item["status"]
        return report


def resolve(ep: Episode, item_id: str, platform: str, *, posted_url: str | None = None, retry: bool = False,
            by: str = "") -> dict:
    """Human settles an uncertain platform: it WAS posted (record url) or it was NOT (allow retry)."""
    if bool(posted_url) == bool(retry):
        raise ValueError("give exactly one of --posted URL or --retry")
    with _lock(ep, item_id):
        item = load(ep, item_id)
        rec = item["results"].get(platform)
        if not rec or rec.get("state") != "sending":
            raise ValueError(f"{platform} is not in an uncertain state")
        if posted_url:
            item["results"][platform] = {**rec, "state": "published", "url": posted_url, "resolved_by": by, "at": now_iso()}
        else:
            item["results"][platform] = {"state": "failed", "error": "operator confirmed not posted", "resolved_by": by, "at": now_iso()}
        states = [item["results"].get(p, {}).get("state") for p in item["platforms"]]
        item["status"] = "published" if all(s == "published" for s in states) else "failed"
        _event(item, "resolved", platform=platform, by=by, posted=bool(posted_url))
        save(ep, item)
        return item
