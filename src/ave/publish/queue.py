"""Approval queue: one JSON file per post under <episode>/publish/queue/<id>.json.

Status machine:  draft -> awaiting_approval -> approved -> publishing -> published | failed   (+ rejected)

Nothing is sent unless ALL hold: status approved/publishing/failed WITH a valid signed approval, the file's current
sha256 equals the approved one, the metadata hash equals the approved one, the caller passed `send=True`
(the CLI's `--yes`), and every platform passes preflight. Per-platform results make re-runs idempotent: a
published platform is never re-sent, and a platform left in state "sending" by a crash is UNKNOWN, never resent
until recover() proves it absent or a human resolves it.

In a shared project only the publisher computer has a queue. Which computer that is comes from `members.publisher_of`: the pinned
owner and the chain of handover events each publisher wrote in its own log, never from project.json alone, and it fails closed.
Everywhere else, create, approve, update, run, resolve and refresh refuse with one plain sentence ("Publishing happens on Maren's
computer..."), and `overview` shows what the publisher noted in the log, read-only. Each send is also recorded in this computer's
own dispatch record (outside the project folder), keyed on the post and platform (`dispatch_key`: the post's reference, never
the content hash, so re-rendered bytes are still the same post), so a restored or copied folder can never send the same thing
twice (see `_reconcile_record`).

The queue, the dispatch record and the publish notes' memory are local zone: they never sync. So in a project a newer AVE saved
(read-only here), they are still written: a send already in flight always records its receipt. Only starting a new send is
refused there.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
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
    """The queue file is local zone (it never syncs), so it is written even in a project a newer AVE saved: a send in flight there
    must still record its result. Only a file that syncs is refused (`guard_write`); the log note is best effort."""
    from ..project.model import guard_write
    path = _path(ep, item["id"])
    guard_write(path)
    write_json(path, item)
    _note(ep, item)


def list_items(ep: Episode) -> list[dict]:
    d = queue_dir(ep)
    return [read_json(p) for p in sorted(d.glob("*.json"))] if d.is_dir() else []


def _event(item: dict, what: str, **kw) -> None:
    item.setdefault("events", []).append({"at": now_iso(), "event": what, **safety.redact_obj(kw)})


# --------------------------------------------------------------------------- one computer publishes
# A shared project (it has .ave/project.json) publishes from exactly one computer. Which one is decided by
# `members.publisher_of`, from the pinned owner and the owner-chain of handover events, never from project.json alone, and it
# fails closed. Every other computer, including a copy of the folder on a new Mac, has no queue: it cannot queue, approve or
# send, and it shows what the publisher noted in the log, read-only. The queue, the dispatch record and the approval key are
# all in the local zone, so none of them syncs (zones.rules). A folder with no project.json is a single-computer episode.

def _project_meta(ep: Episode) -> dict | None:
    pj = Path(ep.root) / ".ave" / "project.json"
    if not pj.is_file():
        return None
    from ..syncthing import members
    return members.read_meta(ep.root)


def _this_device() -> str:
    from ..project.model import Device
    return Device.load().id


def not_publisher(ep: Episode) -> str | None:
    """Why this computer may not queue, approve or send for `ep` (None = it may). Never touches the network."""
    if not (Path(ep.root) / ".ave" / "project.json").is_file():
        return None
    from ..syncthing import members
    try:
        return members.publisher_message(ep.root)
    except ValueError as exc:
        return f"Cannot tell which computer publishes this project ({exc}). Nothing sends until its settings can be read."


def _require_publisher(ep: Episode) -> None:
    why = not_publisher(ep)
    if why:
        raise ValueError(why)


def post_ref(item: dict) -> str | None:
    """The post's identity beyond this queue file: the studio's post and variant, or the package or content item it came from.
    It survives a queue rebuild (a handover, a re-queue), where the queue id does not."""
    if isinstance(item.get("post_ref"), str) and item["post_ref"]:
        return item["post_ref"]
    key = (item.get("origin") or {}).get("key") if isinstance(item.get("origin"), dict) else None
    return f"origin:{key}" if isinstance(key, str) and key else None


def _delivery(item: dict) -> tuple[list[str], list[str]]:
    """(platforms this item reached in any accepted form, platforms with a send in flight or unknown)."""
    res = item.get("results") or {}
    done = [p for p, r in res.items() if r.get("state") in receipts.LOCKED - {"sending"}]
    flying = [p for p, r in res.items() if r.get("state") == "sending"]
    return done, flying


def _note(ep: Episode, item: dict) -> None:
    """Tell the other computers, through the log, what is queued and how far it got. Only words a person reads and platform
    names: no approval, signature, platform id, url or history. Written only by the publisher, only when something shown changed."""
    try:
        meta = _project_meta(ep)
        if meta is None or not_publisher(ep) is not None:
            return
        done, flying = _delivery(item)
        shown = {"item": item["id"], "post": post_ref(item), "title": item.get("title"), "platforms": list(item.get("platforms") or []),
                 "status": item.get("status"), "schedule": item.get("schedule"), "sent": sorted(done), "sending": sorted(flying)}
        seen_file = Path(ep.root) / ".ave" / "local" / "publish_noted.json"
        try:
            seen = read_json(seen_file)
        except (OSError, ValueError):
            seen = {}
        if not isinstance(seen, dict) or seen.get(item["id"]) == shown:
            return
        from ..project.model import Project
        # the queue's own note of where things stand (written for any change, a reject over the room token included), not a
        # person's act: written as the scheduler, like the conflict watcher's records
        Project.open(ep.root).log.append("publish.noted", data=shown, actor="scheduler")
        write_json(seen_file, {**seen, item["id"]: shown})
    except Exception as exc:          # noqa: BLE001 - the note is a courtesy for other computers; it never blocks the queue
        log.warning("could not note the queue item in the project log: %s", safety.redact(exc))


def _tenure(tenures: list[tuple[str, str | None]], device: str | None, hlc: str | None) -> int:
    """The place in the publisher chain during which `device` wrote an event stamped `hlc`, or -1 when it was not publishing."""
    from ..project import hlc as _hlc
    try:
        at = _hlc.parse(hlc) if hlc else None
    except ValueError:
        return -1
    best = -1
    for k, (d, start) in enumerate(tenures):
        try:
            if d == device and (start is None or (at is not None and _hlc.parse(start) <= at)):
                best = k
        except ValueError:
            continue
    return best


def _notes(ep: Episode) -> tuple[list[dict], list[str]]:
    """(every `publish.noted` written by a computer while it was the publisher, each with its `tenure` (place in the chain),
    that chain of computers)."""
    from ..project.log import read_logs
    from ..project.reducer import reduce
    from ..syncthing import members
    tenures = members.publisher_tenures(ep.root)
    st = reduce(read_logs(Path(ep.root) / ".ave" / "log"))
    out = []
    for n in st.publishing.values():
        k = _tenure(tenures, n.get("device"), n.get("hlc"))
        if k >= 0:
            out.append({**n, "tenure": k})
    return sorted(out, key=lambda n: (n["tenure"], n.get("hlc") or "")), [d for d, _ in tenures]


def _current_tenure(ep: Episode) -> int:
    if _project_meta(ep) is None:
        return 0
    from ..syncthing import members
    return max(len(members.publisher_tenures(ep.root)) - 1, 0)


def overview(ep: Episode) -> dict:
    """What this computer shows for the queue. The publisher: its own queue, writable. Every other computer: the latest note per
    item from any computer that has published the project (so an item queued before a handover stays visible), read-only, with the
    plain sentence saying where publishing happens. Each row has `id` (the queue id), as the publisher's rows do."""
    why = not_publisher(ep)
    if why is None:
        return {"publisher": True, "read_only": False, "message": None,
                "items": [{k: i.get(k) for k in ("id", "title", "platforms", "status", "schedule")} for i in list_items(ep)]}
    items: list[dict] = []
    try:
        notes, _ = _notes(ep)
        meta = _project_meta(ep) or {}
        from ..syncthing import members
        latest = {n["data"].get("item"): n for n in notes}           # notes are in chain order: the last per item wins
        for n in latest.values():
            d = n["data"]
            items.append({"id": d.get("item"), "title": d.get("title"), "platforms": d.get("platforms") or [], "status": d.get("status"),
                          "schedule": d.get("schedule"), "noted_by": members._title(members._person(meta, n.get("device")))})
    except (OSError, ValueError) as exc:
        log.warning("could not read the noted queue: %s", safety.redact(exc))
    return {"publisher": False, "read_only": True, "message": why, "items": items}


# --------------------------------------------------------------------------- this computer's dispatch record
# Idempotency for a send. A row is written "sending" BEFORE the network call and closed after. The post is identified by its queue
# id AND its `post_ref` (studio post + variant, or the package/content key), so a rebuilt queue item still matches; the content hash
# is kept only as information, so editing and re-approving a restored copy never makes it a new post. Once this computer has sent a
# post to a platform, it is never sent there again unless a person says so (`ave publish resolve <ep> <id> <platform>
# --send-again`, human-only).
#
# The record lives in two local places: the project's publish folder (local zone, never syncs) and this computer's own AVE folder,
# filed under the folder's stable `publish_id` (written into episode.json / studio.json, which sync, when it is missing). A Time
# Machine restore, a Finder duplicate or a drive that remounts under a new name keeps that id, so the home record is always found.
#
# Only rows this computer wrote count. A row from another computer (a copied folder) makes the send ask the platform first. A
# post that a previous publisher noted as sent or sending is "sent elsewhere": nothing sends until a person confirms.

RECORD_STATES = ("sending", "sent", "not_sent", "rejected_after_accept", "send_again")
SCOPE_KEY = "publish_id"


def content_hash(item: dict) -> str:
    return hashlib.sha256(f"{item.get('sha256')}|{safety.meta_hash(item)}".encode()).hexdigest()


def dispatch_key(item: dict, platform: str) -> str:
    """The identity a send is recorded under: the post (its post_ref, else its queue id) and the platform. Not the content."""
    return hashlib.sha256(f"{post_ref(item) or item['id']}|{platform}".encode()).hexdigest()


def _settings_file(ep: Episode) -> Path | None:
    for name in ("studio.json", "episode.json"):
        f = Path(ep.root) / name
        if f.is_file():
            return f
    return None


def record_scope(ep: Episode) -> str:
    """The folder's stable id for this computer's home record (`publish_id`). It is kept in two places, and once set it is never
    replaced by a new one: episode.json / studio.json (which sync) and `publish/dispatch/publish_id` beside the queue (which a copy
    or a restore carries with the queue). If a writer rewrites episode.json from a copy loaded before the id was there, the id is
    read back from beside the queue and written into episode.json again. A new id is made only when neither place has one: the
    project's id for a project, else a random `pub_…`."""
    f = _settings_file(ep)
    data = None
    if f is not None:
        try:
            data = read_json(f)
        except (OSError, ValueError):
            data = None
    idf = ep.publish / "dispatch" / "publish_id"
    try:
        beside = idf.read_text().strip()
    except OSError:
        beside = ""
    have = data.get(SCOPE_KEY) if isinstance(data, dict) else None
    chosen = next((x for x in (have, beside) if isinstance(x, str) and _ID_RE.match(x)), None)
    if chosen is None:
        if f is not None and not isinstance(data, dict):
            raise ValueError(f"{f.name} cannot be read, so this folder's dispatch record cannot be found; nothing sends until it can")
        try:
            pid = (_project_meta(ep) or {}).get("project_id")
        except ValueError:
            pid = None
        chosen = pid if isinstance(pid, str) and _ID_RE.match(pid) else "pub_" + secrets.token_hex(12)
    if beside != chosen:
        idf.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        idf.write_text(chosen + "\n")
    if isinstance(data, dict) and have != chosen:
        from ..project.model import ReadOnlyProject
        try:
            write_json(f, {**data, SCOPE_KEY: chosen})
        except ReadOnlyProject:       # a newer AVE saved the project: the id beside the queue (local) still finds the record
            pass
    return chosen


def record_paths(ep: Episode) -> list[Path]:
    from ..project.model import device_file
    return [ep.publish / "dispatch" / "record.jsonl", device_file().parent / "dispatch" / f"{record_scope(ep)}.jsonl"]


def _read_rows(paths: list[Path]) -> list[dict]:
    rows: dict[tuple, dict] = {}
    for p in paths:
        try:
            lines = p.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                r = json.loads(line)
            except ValueError:
                continue                  # a torn last line from a crash: the row before it still stands
            if isinstance(r, dict) and r.get("state") in RECORD_STATES and isinstance(r.get("platform"), str) and isinstance(r.get("seq"), int):
                rows[(r.get("device"), r["seq"])] = r
    # seq is a counter per computer (one more than the highest it has seen), not a clock, so a clock step cannot reorder rows
    return sorted(rows.values(), key=lambda r: r["seq"])


def _record_rows(ep: Episode) -> list[dict]:
    return _read_rows(record_paths(ep))


def _matches(row: dict, item: dict, platform: str) -> bool:
    ref = post_ref(item)
    return row.get("platform") == platform and (row.get("post") == item["id"] or (ref is not None and row.get("ref") == ref))


def _append_record(ep: Episode, item: dict, platform: str, state: str, **extra) -> None:
    paths = record_paths(ep)
    seq = max((r["seq"] for r in _read_rows(paths)), default=0) + 1
    sig = ((item.get("approvals") or [{}])[-1] or {}).get("sig")
    from ..studio.dispatch import approval_identity
    row = {"v": 2, "key": dispatch_key(item, platform), "post": item["id"], "ref": post_ref(item), "platform": platform,
           "content": content_hash(item), "approval": approval_identity(sig), "approvals": len(item.get("approvals") or []),
           "state": state, "device": _this_device(), "tenure": _current_tenure(ep),
           "project": record_scope(ep), "at": now_iso(), "seq": seq,
           **safety.redact_obj({k: v for k, v in extra.items() if v is not None})}
    line = (json.dumps(row, sort_keys=True) + "\n").encode()
    for p in paths:
        p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)


def record_mark(ep: Episode, item: dict, platform: str) -> tuple[str | None, dict | None, list[dict]]:
    """(this computer's last state for this post and platform or None, that row, rows other computers wrote for it)."""
    me = _this_device()
    rows = [r for r in _record_rows(ep) if _matches(r, item, platform)]
    own = [r for r in rows if r.get("device") == me]
    foreign = [r for r in rows if r.get("device") != me]
    return (own[-1]["state"], own[-1], foreign) if own else (None, None, foreign)


def _record_outcome(ep: Episode, item: dict, platform: str, out: dict, *, accepted: bool = False) -> None:
    """Close the record from a platform result. Accepted in any form is "sent" (never sent again). A definite no is "not_sent",
    unless the platform had accepted the post first (`accepted`): that is "rejected_after_accept", which needs a fresh review."""
    st = out.get("state")
    if st == "failed":
        _append_record(ep, item, platform, "rejected_after_accept" if accepted else "not_sent")
    elif st in receipts.LOCKED - {"sending"}:
        _append_record(ep, item, platform, "sent", platform_id=out.get("id") or ((out.get("receipt") or {}).get("object") or {}).get("id"),
                       url=out.get("url"), phase=st)


def sent_elsewhere(ep: Episode, item: dict, platform: str, after_tenure: int = -1) -> dict | None:
    """The latest note from ANOTHER computer, written while it published this project and later in the chain than
    `after_tenure` (this computer's latest own row), saying this post reached `platform` or was being sent there (matched on the
    queue id or the post_ref). None when there is none. Only ever a reason NOT to send."""
    if _project_meta(ep) is None:
        return None
    me, ref = _this_device(), post_ref(item)
    try:
        notes, _ = _notes(ep)
    except (OSError, ValueError):
        return None
    hits = [n for n in notes if n.get("device") != me and n["tenure"] > after_tenure
            and (n["data"].get("item") == item["id"] or (ref is not None and n["data"].get("post") == ref))
            and platform in ((n["data"].get("sent") or []) + (n["data"].get("sending") or [])
                             + (list(n["data"].get("platforms") or []) if n["data"].get("status") in ("published", "publishing") else []))]
    return hits[-1] if hits else None


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
           post_type: str | None = None, first_comment: str | None = None, link_url: str | None = None, delivery: dict | None = None, cover: dict | None = None, presentation: dict | None = None,
           post_ref: str | None = None) -> dict:
    """Queue a post. Visibility defaults to private. The file is hashed now; status becomes awaiting_approval only if
    preflight passes (otherwise it stays draft with the problems recorded).

    Studio items pass `meta_v=2` (the schedule is then NOT part of the approval's metadata hash, see safety.META_FIELDS_V2) and may
    bind an `account`, `post_type`, `first_comment` and `link_url`. Each is stored only when given, so existing items are unchanged.

    `media_type` is `video` (default; `video=` names the file, exactly as before), `image` (one file in `files`) or `carousel`
    (several, in order). The approval hash covers EVERY file (see safety.media_sha256) and image items bind their alt texts."""
    _require_publisher(ep)            # only the publisher has a queue
    record_scope(ep)                  # the folder's publish_id exists before any backup of a queued post can be taken
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
    if post_ref:                       # the post this item sends, beyond this queue file (see `post_ref`): survives a queue rebuild
        item["post_ref"] = str(post_ref)
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
    _require_publisher(ep)            # first: on another computer there is no queue, and the person gets the plain sentence
    item = load(ep, item_id)
    from ..syncthing import members
    try:
        members.note_publisher(ep.root)    # a handover is noticed before this approval is stamped, so it counts as fresh
    except ValueError:
        pass
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
    if (Path(ep.root) / ".ave" / "project.json").is_file():   # which computer approved: the handover gate keys on it
        from ..project.model import Device
        approval["device"] = Device.load().id
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
    _require_publisher(ep)
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
    _require_publisher(ep)
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
    why = not_publisher(ep)
    if why:
        return [why]                  # alone: the approval and hash words would only confuse a person on another computer
    if item["status"] == "published":
        return ["already published"]
    try: terminal=_terminal_review(ep,item)
    except ValueError as exc: return [str(exc)]
    if terminal or item.get("delivery_review_required"):
        return ["Provider rejected the accepted operation; fresh human review and approval are required before another send"]
    gone = [f for f in resolve_files(ep, item) if not f.is_file()]
    if gone:
        return [f"{'video' if media_type(item) == 'video' else 'image'} not found: {g}" for g in gone]
    return safety.approval_problems(item, current_sha(ep, item)) + _handover_blockers(ep, item)


def _handover_blockers(ep: Episode, item: dict) -> list[str]:
    """After publishing moves to another member's computer, only that computer sends, and only an approval made there after it saw the move."""
    from ..syncthing import members
    a = (item.get("approvals") or [{}])[-1] or {}
    try:
        return members.publish_blockers(ep.root, a.get("at"), approval_device=a.get("device"))
    except ValueError as exc:
        return [f"cannot tell which computer publishes this project ({exc})"]


def record_holds(ep: Episode, item: dict, name: str) -> dict | None:
    """A reason in this computer's record (or a previous publisher's note) that `name` must not be sent now, checked before any
    send: {"outcome", "detail", "review": True when the item needs a fresh approval}. None = no hold."""
    if (item.get("results") or {}).get(name, {}).get("state") in receipts.LOCKED:
        return None
    mark, row, foreign = record_mark(ep, item, name)
    if mark == "rejected_after_accept":
        from ..studio.dispatch import approval_identity, approval_sig
        fresh = (approval_identity(approval_sig(item)) != row.get("approval")
                 or len(item.get("approvals") or []) > row.get("approvals", 0))
        if not fresh:
            return {"outcome": "blocked", "review": True, "detail": f"{name} accepted this and then rejected it ({row.get('at')}). "
                    f"Review it again and approve it again before it can go out."}
    if mark == "sent" or (mark is None and foreign):
        return None                   # already sent here (`_reconcile_record` says so), or a copied record: the platform is asked
    # Whatever this computer's own last row says, a later publisher's note that the post was sent or sending holds it: that
    # publisher came after this row, so only a person who checked the platform may send it again.
    hit = sent_elsewhere(ep, item, name, after_tenure=row.get("tenure", 0) if row else -1)
    if hit:
        from ..syncthing import members
        who = members._title(members._person(_project_meta(ep) or {}, hit.get("device")))
        who = f"{who}'s computer" if who != "someone" else "Another computer"
        return {"outcome": "sent_elsewhere", "review": False, "detail": (
            f"{who} reported this as sent to {name} while it published this project. Check {name}: if the post is not there, "
            f"send it again with `{send_again_command(ep, item, name)}` in your own terminal.")}
    return None


def send_again_command(ep: Episode, item: dict, platform: str) -> str:
    """The exact human-only command that sends this item again: the studio's for a studio post, else `ave publish resolve`."""
    import shlex
    ref = post_ref(item) or ""
    if ref.startswith("studio:") and len(ref.split(":")) == 3:
        _, post, variant = ref.split(":")
        return f"ave studio resolve {post} {variant} --send-again --studio {shlex.quote(str(ep.root))}"
    return f"ave publish resolve {shlex.quote(str(ep.root))} {item['id']} {platform} --send-again"


def _reconcile_record(ep: Episode, item: dict, name: str, report: dict) -> bool:
    """Bring the queue item in line with this computer's dispatch record before anything is sent. True = handled, send nothing.

    sent (by this computer)      the folder was restored or duplicated after the send: record the post again, report it, send nothing
    sending (by this computer)   a send began and never closed: the queue item becomes "sending", so only the platform or a person decides
    rejected_after_accept        the platform accepted it and then rejected it: only an approval made after that may send it
    not_sent, send_again         a definite no, or a person said to send it again: it may go out
    nothing here, but another    a copied folder carries another computer's rows: they never count as sent, and they never let this one
    computer's row says sent     send blind either: the item becomes "sending" with their platform id, so the platform is asked first
    nothing here, but a previous a handover: "sent elsewhere". Nothing sends until a person checks the platform and says to send it
    publisher noted it sent      (`ave publish resolve ... --send-again`, human-only)
    """
    mark, row, foreign = record_mark(ep, item, name)
    rec = item["results"].get(name, {})
    if rec.get("state") in receipts.LOCKED:
        return False                  # the queue item already holds an in-flight or accepted state: the normal recovery path decides
    if mark in ("not_sent", "send_again", "rejected_after_accept"):
        return False                  # `record_holds` already stopped a rejected one that was not approved again
    if mark == "sent" and row.get("phase", "published") != "published":
        # accepted but not yet a post (processing, scheduled...): the platform says what became of it, nothing is sent again
        phase = row["phase"] if row.get("phase") in receipts.LOCKED else "sending"
        item["results"][name] = {**rec, "state": phase, "started": row.get("at"), "from_record": True,
                                 "receipt": {"object": {"id": row.get("platform_id"), "kind": "post"}}}
        save(ep, item)
        return False
    if mark == "sent":
        pid, url = row.get("platform_id"), row.get("url")
        if pid:
            out = receipts.normalize(receipts.result(name, pid, url, account=item.get("account"), recovered=True), item, name)
        elif url:
            ev = receipts.receipt(provider=name, account=item.get("account"), phase="published", url=url, source="human_report", certainty="reported")
            out = receipts.normalize({"receipt": ev}, item, name)
        else:
            out = {"state": "sending", "started": row.get("at")}
        item["results"][name] = {**rec, **_associate_result(item, rec, out), "from_record": True}
        _event(item, "already_sent", platform=name, at=row.get("at"))
        save(ep, item)
        report["platforms"][name] = {"outcome": "already_sent", "url": url,
                                     "detail": f"This computer already sent this to {name} on {row.get('at')}. Nothing was sent again."}
        return True
    if mark == "sending":
        item["results"][name] = {**rec, "state": "sending", "started": row.get("at"), "from_record": True}
    elif mark is None and any(r["state"] in ("sent", "sending") for r in foreign):
        last = [r for r in foreign if r["state"] in ("sent", "sending")][-1]
        item["results"][name] = {"state": "sending", "started": last.get("at"), "from_record": "another computer",
                                 "receipt": {"object": {"id": last.get("platform_id"), "kind": "post"}}}
    else:
        return False
    save(ep, item)
    return False


def run(ep: Episode, item_id: str, *, send: bool = False, publishers: dict[str, Publisher] | None = None,
        progress_cb: Callable[[str], None] | None = None) -> dict:
    """See `_run`. With AVE_PUBLISH_FAKE set, every report also says, in words, that no platform was contacted."""
    report = _run(ep, item_id, send=send, publishers=publishers, progress_cb=progress_cb)
    from .adapters.fake import ENV, fake_file
    if fake_file() is not None and not publishers:
        try:
            unbound = not load(ep, item_id).get("account")     # an account-bound item uses its account's adapter, never the fake
        except (OSError, ValueError):
            unbound = False
        if unbound:
            report["fake"] = f"FAKE (no platform contacted): {ENV} is set, so posts are written to {fake_file()}"
    return report


def _run(ep: Episode, item_id: str, *, send: bool = False, publishers: dict[str, Publisher] | None = None,
         progress_cb: Callable[[str], None] | None = None) -> dict:
    """Dry-run by default. With send=True, publish every not-yet-published platform of an approved item.

    Returns {"mode", "ok", "blocked": [...], "platforms": {name: outcome}}. Dry-run never writes to the queue.
    """
    publishers = dict(publishers or {})
    why = not_publisher(ep)
    if why:                           # checked before anything is read, locked or written: a copied folder stays exactly as it was
        return {"id": item_id, "mode": "send" if send else "dry-run", "ok": False, "blocked": [why], "platforms": {}}
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
        holds = {}
        for p in todo:
            st = item["results"].get(p, {}).get("state")
            mark = record_mark(ep, item, p)[0]
            report["platforms"][p] = {"would": "nothing: already sent from this computer" if mark == "sent"
                                      else "check-recovery" if st == "sending" or mark == "sending" else "publish"}
            hold = record_holds(ep, item, p)
            if hold:
                holds[p] = hold
                report["platforms"][p] = {"would": "nothing", "outcome": hold["outcome"], "detail": hold["detail"]}
        if holds and not report["blocked"]:
            report["blocked"] = [h["detail"] for h in holds.values()]
            if send and any(h["review"] for h in holds.values()) and item["status"] in ("approved", "failed"):
                item["status"] = "awaiting_approval"      # the platform rejected what was approved: a person reviews it again
                _event(item, "approval_invalidated", reasons=["the platform accepted this and then rejected it"])
                save(ep, item)
            return report
        if report["blocked"]:
            if send and blockers and any("sha256" in b or "changed" in b or "expired" in b for b in blockers) and item["status"] != "published":
                item["status"] = "awaiting_approval"      # approval no longer valid for these bytes/metadata
                _event(item, "approval_invalidated", reasons=blockers)
                save(ep, item)
            return report
        if not send:
            report["ok"] = True
            return report
        from ..project.model import read_only_reason
        why = read_only_reason(ep.root)
        if why:                       # a new send never starts in a project this AVE cannot read in full; one in flight finishes
            report["blocked"] = [why]
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
                if _reconcile_record(ep, item, name, report):
                    continue                        # this computer already sent exactly this: never again
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
                        _record_outcome(ep, item, name, out, accepted=True)
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
                _append_record(ep, item, name, "sending")     # and this computer's own record says so too, outside the queue file

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
                        _record_outcome(ep, item, name, {**item["results"][name], "state": "published"})
                        continue
                    item["results"][name] = {**item["results"][name], "state": "failed", "error": safety.redact(e), "diagnostics": receipts.error(e), "at": now_iso()}
                    report["platforms"][name] = {"outcome": "failed", "error": safety.redact(e), "diagnostics": receipts.error(e)}
                    log.warning("publish to %s failed: %s", name, e)
                    save(ep, item)
                    _append_record(ep, item, name, "not_sent")
                    continue
                except Exception as e:     # unknown outcome: keep "sending"
                    if item["results"][name].get("receipt", {}).get("phase") == "published":
                        report["platforms"][name] = {**item["results"][name], "outcome":"published"}
                        _record_outcome(ep, item, name, {**item["results"][name], "state": "published"})
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
                _record_outcome(ep, item, name, out)

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
            by: str = "", send_again: bool = False) -> dict:
    """Human settles a platform. An uncertain one: it WAS posted (record url) or it was NOT (allow retry). `send_again`: a person
    checked and wants this post to go out on `platform` again, although this computer already sent it, or a previous publisher
    reported it sent. It is the only way past that record (the CLIs are human-only), and the item then needs a fresh approval."""
    if sum(map(bool, (posted_url, retry, send_again))) != 1:
        raise ValueError("give exactly one of --posted URL, --retry or --send-again")
    _require_publisher(ep)
    if send_again:
        return _send_again(ep, item_id, platform, by)
    with _lock(ep, item_id):
        item = load(ep, item_id)
        rec = item["results"].get(platform)
        if not rec or rec.get("state") != "sending":
            raise ValueError(f"{platform} is not in an uncertain state")
        if posted_url:
            evidence=receipts.receipt(provider=platform,account=item.get('account'),phase='published',url=posted_url,source='human_report',certainty='reported')
            item["results"][platform] = {**rec, **receipts.normalize({'receipt':evidence},item,platform), "resolved_by": by, "at": now_iso()}
            _append_record(ep, item, platform, "sent", url=posted_url, by=by or None)
        else:
            evidence=receipts.receipt(provider=platform,account=item.get('account'),phase='failed',source='human_report',certainty='reported')
            item["results"][platform] = {**receipts.normalize({'receipt':evidence},item,platform), "error": "operator confirmed not posted", "resolved_by": by, "at": now_iso()}
            _append_record(ep, item, platform, "not_sent", by=by or None)
        states = [item["results"].get(p, {}).get("state") for p in item["platforms"]]
        item["status"] = receipts.aggregate(item)
        _event(item, "resolved", platform=platform, by=by, posted=bool(posted_url))
        save(ep, item)
        return item


def _send_again(ep: Episode, item_id: str, platform: str, by: str) -> dict:
    with _lock(ep, item_id):
        item = load(ep, item_id)
        if platform not in item.get("platforms", []):
            raise ValueError(f"{platform} is not one of this post's platforms")
        rec = item["results"].get(platform) or {}
        if rec.get("state") == "sending" and not rec.get("from_record"):
            raise ValueError(f"a send to {platform} may still be in flight; settle it with --posted URL or --retry first")
        mark = record_mark(ep, item, platform)[0]
        if mark != "sent" and rec.get("state") not in receipts.LOCKED and sent_elsewhere(ep, item, platform) is None:
            raise ValueError(f"nothing stops {platform} from sending this post; there is nothing to send again")
        _append_record(ep, item, platform, "send_again", by=by or None)
        if rec:
            _event(item, "delivery_history", results={platform: rec})
        item["results"].pop(platform, None)
        if item["status"] not in ("draft", "rejected"):
            item["status"] = "awaiting_approval"  # sending again is a new decision: a person approves these exact bytes once more
        _event(item, "send_again", platform=platform, by=by)
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
    _require_publisher(ep)
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
                _record_outcome(ep,item,name,out,accepted=True)
                if out['state']=='failed': _require_terminal_review(ep,item,name,out)
        if changed: item['status']=receipts.aggregate(item)
        _event(item,'status_refreshed');save(ep,item)
        return item
