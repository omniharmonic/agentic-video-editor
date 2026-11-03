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
import os
import re
import secrets
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from ..episode import Episode, read_json, write_json
from . import safety, receipts
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


MEDIA_TYPES = ("video", "image", "carousel", "text", "document")


def media_type(item: dict) -> str:
    """`video` for every item queued before images existed (the key is absent), so old items behave exactly as before."""
    return item.get("media_type") or "video"


def resolve_files(ep: Episode, item: dict) -> list[Path]:
    """Every file the item publishes, in order (a video: one file; an image: one; a carousel: every slide)."""
    if media_type(item) == 'text': return []
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
    video = files[0] if files else None
    problems: list[str] = []
    if item.get('meta_v') == 2 and (not item.get('caption') and item.get('title') or 'facebook' in item.get('platforms',[]) and item.get('caption')==item.get('description')):
        problems.append('Legacy text composition is ambiguous; prepare version 3 and review the exact caption again')
    if not item.get("title"):
        problems.append("title is required")
    if not item.get("platforms"):
        problems.append("at least one platform is required")
    if item.get("meta_v") == 3:
        delivery=item.get('delivery')
        presentation=item.get('presentation') or {}
        if not isinstance(delivery,dict) or set(delivery)!={'method'}: return ['Invalid delivery options']
        if not isinstance(presentation,dict) or set(presentation)-{'feed_presence'} or any(type(v) is not bool for v in presentation.values()): return ['Invalid feed-presence options']
        method = delivery.get("method")
        if method not in ("manual", "automatic"): return ["Choose manual or automatic delivery"]
        cover = item.get("cover")
        if cover:
            from ..studio.manual_export import _safe
            try:
                path = _safe(ep.root / cover["file"], ep.root)
                if not path.is_file() or safety.sha256_file(path) != cover["sha256"]: problems.append("Cover bytes changed or are missing")
            except (KeyError, ValueError, OSError): problems.append("Cover is invalid")
        if method == "manual":
            if mt != "video" and any(not str(a).strip() for a in item.get("alt_texts", [])): problems.append("Every image needs alt text")
            return problems
        if cover:
            if item.get('platforms')!=['instagram'] or item.get('post_type')!='reel': problems.append('Automatic cover delivery is supported only for Instagram Reels; choose manual delivery')
            else:
                try:
                    from PIL import Image
                    path=ep.root/cover['file']
                    with Image.open(path) as im:
                        if im.format!='JPEG' or im.mode!='RGB' or path.stat().st_size>8388608: problems.append('Instagram Reel cover requires RGB JPEG up to 8 MB')
                except (OSError,KeyError): problems.append('Cover is not a readable JPEG')
        if item.get('presentation') and (item.get('platforms')!=['instagram'] or item.get('post_type')!='reel'):
            problems.append('Feed-presence options apply to Instagram Reels only')
        if problems: return problems
    for name in item.get("platforms", []):
        if name not in ALL_PLATFORMS:
            problems.append(f"unknown platform {name!r}")
            continue
        # the studio hands over {account id: adapter} or {platform: adapter}; an item bound to an account looks its own adapter up first
        # (two Instagram accounts need two adapters), then the platform, and only then the unbound legacy adapter
        pub = selected_publisher(ep,item,name,publishers)
        if mt in ('image','carousel'):
            problems += image_problems(pub.cfg if hasattr(pub, "cfg") else load_platforms(), name, item, files)
        if mt == 'document':
            if len(files)!=1 or files[0].suffix.lower()!='.pdf': problems.append('Document delivery needs one PDF')
            elif files[0].stat().st_size>100*1024*1024: problems.append('PDF exceeds the 100 MB document limit')
            else:
                try:
                    from pypdf import PdfReader
                    with files[0].open("rb") as source:
                        doc=PdfReader(source,strict=True)
                        if doc.is_encrypted or not 1<=len(doc.pages)<=300: problems.append('PDF must be unencrypted with 1–300 pages')
                except Exception: problems.append('Document is not a readable PDF')
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
           origin: dict | None = None, meta_v: int | None = None, account: str | None = None,
           post_type: str | None = None, first_comment: str | None = None, link_url: str | None = None, delivery: dict | None = None, cover: dict | None = None, presentation: dict | None = None) -> dict:
    """Queue a post. Visibility defaults to private. The file is hashed now; status becomes awaiting_approval only if
    preflight passes (otherwise it stays draft with the problems recorded).

    Studio items pass `meta_v=2` (the schedule is then NOT part of the approval's metadata hash, see safety.META_FIELDS_V2) and may
    bind an `account`, `post_type`, `first_comment` and `link_url`. Each is stored only when given, so existing items are unchanged.

    `media_type` is `video` (default; `video=` names the file, exactly as before), `image` (one file in `files`) or `carousel`
    (several, in order). The approval hash covers EVERY file (see safety.media_sha256) and image items bind their alt texts."""
    if media_type not in MEDIA_TYPES:
        raise ValueError(f"media_type must be one of {', '.join(MEDIA_TYPES)}")
    if media_type == 'text':
        if files or video: raise ValueError('text items have no media files')
        vp, stored, stored_files = None, None, []
        hashed = safety.media_sha256([], 'text')
    elif media_type == "video":
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
    if meta_v != 3 and any(value is not None for value in (delivery, cover, presentation)):
        raise ValueError('Delivery, cover and presentation require payload version 3')
    if meta_v is not None:
        if meta_v not in (2, 3):
            raise ValueError("meta_v must be 2 or 3 (or omitted)")
        item["meta_v"] = meta_v
    for k, v in (("account", account), ("post_type", post_type), ("first_comment", first_comment), ("link_url", link_url)):
        if v is not None:
            item[k] = v
    if meta_v == 3:
        item.update(delivery=delivery or {"method":"automatic"}, cover=cover, presentation=presentation or {})
    problems = preflight_item(ep, item, publishers)
    item["preflight_problems"] = problems
    item["status"] = "draft" if problems else "awaiting_approval"
    _event(item, "queued", problems=len(problems))
    save(ep, item)
    return item


# --------------------------------------------------------------------------- approving

def approve(ep: Episode, item_id: str, by: str, confirm_sha: str | None = None, publishers=None, source: str = "cli",
            bind: dict | None = None) -> dict:
    """Record who approved which bytes (`source`: the human surface used, "cli" or "ui"; recorded, not part of the signature). `confirm_sha` (>= 8 hex chars of the file hash the human was shown) must match,
    so approval is bound to what was reviewed rather than to whatever happens to be on disk later."""
    item = load(ep, item_id)
    from ..studio import dispatch
    terminal=dispatch.terminal_review(ep,item)
    if item["status"] not in ("awaiting_approval", "draft") and not (item["status"] == "failed" and terminal):
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
        if item["status"] == "draft":           # a refused approval never DEMOTES an item that was waiting for one
            save(ep, item)
        raise ValueError("preflight problems block approval: " + "; ".join(problems))
    approval = (safety.make_renewed_approval(item,sha,by.strip(),now_iso(),terminal['approval']) if terminal
                else safety.make_approval(item, sha, by.strip(), now_iso()))
    approval["source"] = source
    if item.get("meta_v") in (2, 3):                 # studio item: also sign the account, the slot and the lifetime policy (safety.attach_bind)
        safety.attach_bind(approval, item, bind)
    if terminal:
        historical={**item.get('results',{}),terminal['provider']:terminal['result']}
        _event(item,'delivery_history',approval_identity=dispatch.approval_identity(terminal['approval']),results=historical)
        item.get('results',{}).pop(terminal['provider'],None)
    item["approvals"].append(approval)
    item.pop("delivery_review_required", None)
    item["status"] = "approved"
    _event(item, "approved", by=by, sha256=sha, source=source)
    save(ep, item)
    return item


def reject(ep: Episode, item_id: str, by: str, reason: str = "") -> dict:
    item = load(ep, item_id)
    if item["status"] == "published":
        raise ValueError("already published; nothing to reject")
    if item["status"] == "publishing" or any(r.get("state") in receipts.LOCKED for r in item.get("results", {}).values()):
        raise ValueError("a publish is in flight or uncertain; resolve it first")
    item["status"] = "rejected"
    item["approvals"] = []   # a rejection voids any earlier approval
    _event(item, "rejected", by=by, reason=reason)
    save(ep, item)
    return item


# --------------------------------------------------------------------------- updating

UPDATABLE = ("platforms", "title", "description", "tags", "caption", "schedule", "visibility", "ai_disclosure", "made_for_kids",
             "format", "account", "accounts", "post_type", "first_comment", "link_url", "alt_texts", "origin", "meta_v", "delivery", "cover", "presentation")
_MEDIA_FIELDS = ("video", "files", "media_type")


def update(ep: Episode, item_id: str, *, publishers: dict[str, Publisher] | None = None, **fields) -> dict:
    """Edit a not-yet-sent item: set `fields`, re-hash the media, re-run preflight, and keep the approval honest.

    Metadata in UPDATABLE, plus the media (`video=` for a video, or `files=[...]` with `media_type=image|carousel`, which swaps the bytes).
    If the media hash or the metadata hash (safety.meta_hash) changed, an `approved` (or failed) item drops to `awaiting_approval`
    (`draft` when preflight now fails) with an `approval_invalidated` event, exactly like a changed file does in `run`. If neither changed
    (for a `meta_v` 2 item, a retime changes only `schedule`), the approval stands and the status is untouched. Never approves.
    Refuses items that are published, publishing, rejected, or have an uncertain ("sending") platform. Holds the per-item lock."""
    unknown = sorted(set(fields) - set(UPDATABLE) - set(_MEDIA_FIELDS))
    if unknown:
        raise ValueError(f"cannot update {', '.join(unknown)}; allowed: {', '.join(UPDATABLE + _MEDIA_FIELDS)}")
    with _lock(ep, item_id):
        item = load(ep, item_id)
        version = fields.get('meta_v', item.get('meta_v'))
        if version not in (None, 2, 3) or (item.get('meta_v') == 3 and version != 3):
            raise ValueError('Payload versions cannot be downgraded')
        if version != 3 and any(k in fields for k in ('delivery','cover','presentation')):
            raise ValueError('Delivery, cover and presentation require payload version 3')
        if item["status"] in ("published", "publishing", "rejected") or \
                any(r.get("state") in receipts.LOCKED for r in item.get("results", {}).values()):
            raise ValueError(f"cannot update an item in status {item['status']!r} (or with an uncertain platform); resolve it first")
        before_meta = safety.meta_hash(item)
        before_sha = item["sha256"]
        if "visibility" in fields and fields["visibility"] not in ("private", "unlisted", "public"):
            raise ValueError("visibility must be private, unlisted or public")
        if "format" in fields and fields["format"] not in ("auto", "short", "long"):
            raise ValueError("format must be auto, short or long")
        if "platforms" in fields:
            fields["platforms"] = list(dict.fromkeys(fields["platforms"]))
        for k in UPDATABLE:
            if k in fields:
                item[k] = fields[k]
        if any(k in fields for k in _MEDIA_FIELDS):
            mt = fields.get("media_type") or media_type(item)
            if mt not in MEDIA_TYPES:
                raise ValueError(f"media_type must be one of {', '.join(MEDIA_TYPES)}")
            if mt == 'text':
                if fields.get('files') or fields.get('video'): raise ValueError('text items have no files')
                item.update(media_type='text',files=[],video=None,alt_texts=[],file_sha256=[])
            elif mt == "video":
                video = fields.get("video")
                if video is None:
                    raise ValueError("video is required for a video item")
                _, item["video"] = _stored(ep, video)
                for k in ("media_type", "files", "file_sha256"):
                    item.pop(k, None)
                if "alt_texts" not in fields:
                    item.pop("alt_texts", None)
            else:
                if not fields.get("files"):
                    raise ValueError(f"{mt} items need files=[...]")
                pairs = [_stored(ep, f) for f in fields["files"]]
                item.update(media_type=mt, files=[s for _, s in pairs], video=pairs[0][1])
                item.setdefault("alt_texts", [])
        new_sha = current_sha(ep, item) if all(f.is_file() for f in resolve_files(ep, item)) else before_sha
        item["sha256"] = new_sha
        if media_type(item) != "video" and all(f.is_file() for f in resolve_files(ep, item)):
            item["file_sha256"] = [safety.sha256_file(p) for p in resolve_files(ep, item)]
        problems = preflight_item(ep, item, publishers)
        item["preflight_problems"] = problems
        changed = new_sha != before_sha or safety.meta_hash(item) != before_meta
        if item["status"] in ("draft", "awaiting_approval") or _terminal_review(ep,item):
            item["status"] = "draft" if problems else "awaiting_approval"
        elif changed:                    # approved / failed: the human reviewed other bytes or words
            reasons = [r for r, hit in (("media", new_sha != before_sha), ("metadata", safety.meta_hash(item) != before_meta)) if hit]
            item["status"] = "draft" if problems else "awaiting_approval"
            _event(item, "approval_invalidated", reasons=reasons)
        _event(item, "updated", fields=sorted(fields), changed=changed, problems=len(problems))
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


def verified_copy(files: list[Path]) -> tuple[Path, list[Path]]:
    """Copy each file to a private (0700) temp dir, read-only, keeping its name. The caller hashes and sends THESE bytes.
    The caller removes the returned directory."""
    root = Path(tempfile.mkdtemp(prefix="ave-send-"))
    try:
        os.chmod(root, 0o700)
        out = []
        for i, f in enumerate(files):
            d = root / str(i)
            d.mkdir(mode=0o700)
            dst = d / Path(f).name
            shutil.copyfile(f, dst)
            os.chmod(dst, 0o400)
            out.append(dst)
        return root, out
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise


def selected_publisher(ep, item, name, publishers=None):
    selected=(publishers or {}).get(item.get('account') or '') or (publishers or {}).get(name)
    if selected is not None: return selected
    if item.get('account') and hasattr(ep,'accounts_dir'):
        from ..platforms.registry import publishers_for_account
        selected=publishers_for_account(ep,item['account']).get(name)
        if selected is None: raise ValueError('Queue platform does not match the selected account')
        return selected
    return get_publisher(name)


def _terminal_review(ep,item):
    from ..studio.dispatch import terminal_review
    return terminal_review(ep,item)


def _associate_result(item,record,result):
    from ..studio.dispatch import approval_identity,approval_sig
    return {**result,'approval_identity':record.get('approval_identity') or approval_identity(approval_sig(item))}


def _require_terminal_review(ep,item,provider,result):
    from ..studio.dispatch import require_terminal_review
    require_terminal_review(ep,item,provider,result)
    item['delivery_review_required']=True


def send_blockers(ep: Episode, item: dict) -> list[str]:
    """Reasons this item may not be sent now (approval, hash, file). Does not touch the network."""
    if item["status"] == "published":
        return ["already published"]
    try: terminal=_terminal_review(ep,item)
    except ValueError as exc: return [str(exc)]
    if terminal or item.get("delivery_review_required"):
        return ["Provider rejected the accepted operation; fresh human review and approval are required before another send"]
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
        if item.get("meta_v") == 3 and (item.get("delivery") or {}).get("method") == "manual":
            report["blocked"] = ["Manual delivery: download the reviewed files and post them yourself. Automatic dispatch is disabled."]
            return report
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
            if send and blockers and any("sha256" in b or "changed" in b or "expired" in b for b in blockers) and item["status"] != "published":
                item["status"] = "awaiting_approval"      # approval no longer valid for these bytes/metadata
                _event(item, "approval_invalidated", reasons=blockers)
                save(ep, item)
            return report
        if not send:
            report["ok"] = True
            return report
        for name in todo:
            ready = selected_publisher(ep,item,name,publishers).ready_problems()
            if ready:
                report["blocked"] += [f"{name}: {r}" for r in ready]
        if report["blocked"]:
            return report

        item["status"] = "publishing"
        _event(item, "run_started")
        save(ep, item)
        scratch: list[Path] = []          # private verified copies, removed in the finally below
        try:
            for name in todo:
                pub = selected_publisher(ep,item,name,publishers)
                rec = item["results"].get(name, {})
                out: dict
                if rec.get("state") in receipts.LOCKED:
                    try:
                        verdict = pub.recover(item, rec)
                    except Exception as exc:
                        rec['refresh_diagnostics']=receipts.error(exc)
                        save(ep,item)
                        verdict = None
                    if isinstance(verdict,Absent) and rec.get('state')!='sending':
                        verdict=_terminal_absence(verdict,rec,item,name)
                    if isinstance(verdict, dict):
                        out = _associate_result(item,rec,receipts.normalize(verdict, item, name))
                        item["results"][name] = {**rec, **out}
                        if out["state"] == "failed": _require_terminal_review(ep,item,name,out)
                        report["platforms"][name] = {**out, "outcome": out['receipt']['phase']}
                        save(ep, item)
                        continue
                    if isinstance(verdict, Absent) and rec.get("state") == "sending":
                        _event(item, "recovered_absent", platform=name, detail=verdict.detail)
                    else:
                        report["platforms"][name] = {"outcome": "uncertain",
                                                     "detail": "a previous attempt may have posted; verify on the platform then run "
                                                               "`ave publish resolve` (--posted URL or --retry)"}
                        continue
                # Re-verify the bytes immediately before each send AND send the very bytes that were hashed: the approved file(s) are
                # copied to a private temp dir, hashed THERE, and the adapter is handed the copy (a swap of the original is harmless).
                while scratch:                      # one copy on disk at a time (videos are large)
                    shutil.rmtree(scratch.pop(), ignore_errors=True)
                try:
                    vdir, copies = verified_copy(resolve_files(ep, item))
                except OSError as e:
                    report["platforms"][name] = {"outcome": "blocked", "detail": f"could not snapshot the file(s) to send: {e}"}
                    continue
                scratch.append(vdir)
                again = safety.approval_problems(item, safety.media_sha256(copies, media_type(item)))
                if again:
                    report["platforms"][name] = {"outcome": "blocked", "detail": "; ".join(again)}
                    continue
                item["results"][name] = _associate_result(item,{}, {"state": "sending", "started": now_iso()})
                save(ep, item)             # BEFORE the network call: a crash leaves "sending", never nothing

                def progress(partial: dict, _n=name) -> None:
                    if partial.get('host_leases'):
                        existing=item['results'][_n].get('host_leases',[])
                        partial['host_leases']=list({x['key']:x for x in [*existing,*partial['host_leases']]}.values())
                    if partial.get('receipt'):
                        partial = receipts.normalize(partial, item, _n)
                    partial=_associate_result(item,item["results"][_n],partial)
                    item["results"][_n].update(safety.redact_obj(partial))
                    record=item['results'][_n]
                    if partial.get('adjuncts') and record.get('receipt'):
                        record['receipt']['adjuncts'].update(safety.redact_obj(partial['adjuncts']))
                    reference=((record.get('receipt') or {}).get('object') or {}).get('id') or record.get('container') or record.get('publish_id')
                    for lease in record.get('host_leases',[]):
                        if reference: lease['provider_reference']=reference
                    save(ep, item)

                try:
                    to_send = item if media_type(item) == "video" else {**item, "_files": [str(f) for f in copies]}
                    if item.get('cover'):
                        cdir,ccopies=verified_copy([ep.root/item['cover']['file']]);scratch.append(cdir)
                        if safety.sha256_file(ccopies[0])!=item['cover']['sha256']: raise PublishError('Cover changed after review')
                        to_send={**to_send,'_cover':str(ccopies[0])}
                    result = pub.publish(to_send, copies[0] if copies else None, progress)
                except PublishError as e:
                    # Primary success was durably persisted before optional work.
                    if item["results"][name].get("receipt", {}).get("phase") == "published":
                        report["platforms"][name] = {**item["results"][name], "outcome":"published"}
                        continue
                    item["results"][name] = {**item["results"][name], "state": "failed", "error": safety.redact(e), "diagnostics": receipts.error(e), "at": now_iso()}
                    report["platforms"][name] = {"outcome": "failed", "error": safety.redact(e), "diagnostics": receipts.error(e)}
                    log.warning("publish to %s failed: %s", name, e)
                    save(ep, item)
                    continue
                except Exception as e:     # unknown outcome: keep "sending"
                    if item["results"][name].get("receipt", {}).get("phase") == "published":
                        report["platforms"][name] = {**item["results"][name], "outcome":"published"}
                        continue
                    item["results"][name]["error"] = safety.redact(f"{type(e).__name__}: {e}")
                    report["platforms"][name] = {"outcome": "uncertain", "error": item["results"][name]["error"]}
                    log.error("publish to %s ended uncertain: %s", name, e)
                    save(ep, item)
                    continue
                out = _associate_result(item,item["results"][name],receipts.normalize(result, item, name))
                item["results"][name] = {**item["results"][name], **out}
                report["platforms"][name] = {**out, "outcome":out['receipt']['phase']}
                save(ep, item)

        finally:
            for d in scratch:
                shutil.rmtree(d, ignore_errors=True)
        states = [item["results"].get(p, {}).get("state") for p in item["platforms"]]
        item["status"] = receipts.aggregate(item)
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
            evidence=receipts.receipt(provider=platform,account=item.get('account'),phase='published',url=posted_url,source='human_report',certainty='reported')
            item["results"][platform] = {**rec, **receipts.normalize({'receipt':evidence},item,platform), "resolved_by": by, "at": now_iso()}
        else:
            evidence=receipts.receipt(provider=platform,account=item.get('account'),phase='failed',source='human_report',certainty='reported')
            item["results"][platform] = {**receipts.normalize({'receipt':evidence},item,platform), "error": "operator confirmed not posted", "resolved_by": by, "at": now_iso()}
        states = [item["results"].get(p, {}).get("state") for p in item["platforms"]]
        item["status"] = receipts.aggregate(item)
        _event(item, "resolved", platform=platform, by=by, posted=bool(posted_url))
        save(ep, item)
        return item


def _terminal_absence(verdict,record,item,provider):
    obj=(record.get('receipt') or {}).get('object') or {}
    evidence=receipts.receipt(provider=provider,account=item.get('account'),phase='failed',
        object_id=obj.get('id'),object_kind=obj.get('kind','operation'),source='provider_status',
        diagnostics={'kind':'provider_terminal','reason':safety.redact(verdict.detail)})
    return {'receipt':evidence,'error':safety.redact(verdict.detail)}


def refresh(ep, item_id, *, publishers):
    """Status-only refresh. Never calls publish, never retries an absent/unknown operation."""
    with _lock(ep,item_id):
        item=load(ep,item_id)
        terminal=_terminal_review(ep,item)
        if terminal:
            # Recover a crash after the sealed decision but before the queue write.
            item.setdefault('results',{})[terminal['provider']]=terminal['result']
            item['delivery_review_required']=True
            item['status']=receipts.aggregate(item)
            save(ep,item)
            return item
        changed=False
        for name,record in list(item.get('results',{}).items()):
            if record.get('state') not in receipts.LOCKED-{'published'}: continue
            pub=publishers.get(name)
            if pub is None: continue
            expired=receipts.expired_leases(record)
            if expired: record['host_lease_diagnostics']={'kind':'lease_expired','keys':expired,'action':'Hosted media expired after 24 hours. Check provider status or use protected outcome review; do not send again while unresolved.'}
            try: verdict=pub.recover(item,record)
            except Exception as exc:
                record['refresh_diagnostics']=receipts.error(exc);save(ep,item);continue
            if isinstance(verdict,Absent): verdict=_terminal_absence(verdict,record,item,name)
            if isinstance(verdict,dict):
                out=_associate_result(item,record,receipts.normalize(verdict,item,name));changed=True
                item['results'][name]={**record,**out}
                if out['state']=='failed': _require_terminal_review(ep,item,name,out)
        if changed: item['status']=receipts.aggregate(item)
        _event(item,'status_refreshed');save(ep,item)
        return item
