"""Ingest paths into the studio (spec 9): `idea`, `blog`, `audio`, `video`, `episode`, and `adopt`.

Every path ends the same way: something on disk the next recipe step can read, plus the exact next commands. Nothing here posts, spends,
fetches a URL (a URL is never fetched: the human saves the page as Markdown, spec N7) or approves.

    idea(studio, text)              a one-line idea -> an idea-stage post
    blog(sp, file)                  Markdown -> studio/work/writing/ -> sentence-addressable passages (content.sources.ingest_writing)
    audio(sp, file) / video(...)    a NORMAL episode beside the studio (Episode.create), the file COPIED into raw/, then `ave ingest` + `ave audio`;
                                    transcription stays a separate, visible step (it is the slow one)
    single_camera_shim(ep)          spec 9.3: the multicam path refuses one file; this writes the minimal cameras/sync entries the planner expects
    episode(sp, path)               what an existing episode already offers (approved clips, quote cards, package copy, queue items)
    adopt(sp, path)                 each episode queue item -> a Post + Variant that REFERENCES the item (queue.root = the episode), assets copied by sha
    (shorts)                        human-approved clips' exported shorts -> posts with a variant per account: `ingest_shorts.shorts_ingest`
"""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path
from typing import Any

from ..episode import Episode, read_json, write_json
from ..publish import queue as pq, safety
from . import model, store
from .clock import iso_utc
from .events import AGENT, Actor

AUDIO_EXTS = (".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".aif", ".aiff")
VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".m4v", ".webm", ".avi")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "item"


# ------------------------------------------------------------------------------------------------ idea

def idea(studio, text: str, *, campaign: str | None = None, actor: Actor = AGENT, reason: str = "", confidence: float | None = None,
         origin: dict | None = None) -> dict:
    text = (text or "").strip()
    if not text:
        raise ValueError("an idea is at least a few words")
    return studio.create_post(idea=text, campaign=campaign, actor=actor, reason=reason, confidence=confidence,
                              origin=origin or {"type": "idea", "ref": None, "passages": []})


# ------------------------------------------------------------------------------------------------ blog

def blog(sp, file: str | Path) -> dict:
    """Copy `file` (Markdown) into `work/writing/` and split every file there into passages. The original is only read. A URL is refused."""
    from ..content import sources as S
    raw = str(file)
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
        raise ValueError("a URL is not fetched (no scraping): save the page as Markdown and pass that file")
    src = Path(raw).expanduser()
    if not src.is_file():
        raise FileNotFoundError(f"no such file: {src}")
    if src.suffix.lower() not in (".md", ".markdown", ".txt"):
        raise ValueError("blog ingest takes a Markdown file (.md)")
    dest_dir = sp.work / "writing"
    dest_dir.mkdir(parents=True, exist_ok=True)
    text = src.read_text(encoding="utf-8")
    name = src.stem + ".md"
    dest = dest_dir / name
    if dest.exists() and dest.read_text(encoding="utf-8") != text:
        name = f"{src.stem}-{hashlib.sha256(text.encode()).hexdigest()[:8]}.md"
        dest = dest_dir / name
    dest.write_text(text, encoding="utf-8")
    out = S.ingest_writing(sp, dest_dir)
    f = next(x for x in out["files"] if x["file"] == name)
    return {"file": name, "title": f["title"], "writing": str(sp.work / "content" / "writing.json"),
            "passages": [{"id": p["id"], "words": len(p["text"].split()), "text": p["text"][:160] + ("..." if len(p["text"]) > 160 else ""),
                          "sentences": [s["id"] for s in p["sentences"]]} for p in f["passages"]],
            "totals": out["totals"],
            "next": "Choose 4-8 quotable passages (they must be verbatim), then `ave studio render <post> --source '{\"writing\": \"%s\", \"passages\": [...]}'`" % name}


# ------------------------------------------------------------------------------------------------ audio / video -> a normal episode

def _new_episode(sp, src: Path, name: str | None, exts: tuple[str, ...], kind: str) -> tuple[Episode, bool]:
    if not src.is_file():
        raise FileNotFoundError(f"no such file: {src}")
    if src.suffix.lower() not in exts:
        raise ValueError(f"{kind} ingest takes {', '.join(exts)} (got {src.suffix or 'no extension'})")
    ep = Episode(sp.root.parent / (name or _slug(src.stem)))
    created = not ep.meta_path.exists()
    ep.create(title=(name or src.stem))
    dest = ep.raw / src.name
    if dest.exists():
        if safety.sha256_file(dest) != safety.sha256_file(src):
            raise ValueError(f"{dest} already exists with different bytes; pass --name for a new episode")
    else:
        shutil.copy2(src, dest)                     # copy, never move: the original stays where the human put it
    return ep, created


def _probe_and_audio(ep: Episode) -> dict:
    from .. import audio, media
    info = media.ingest(ep)
    outs = audio.extract_all(ep)
    return {"files": [f["file"] for f in info.get("files", [])], "analysis_audio": [ep.rel(p) for p in outs]}


def audio(sp, file: str | Path, *, name: str | None = None, run: bool = True) -> dict:
    ep, created = _new_episode(sp, Path(file).expanduser(), name, AUDIO_EXTS, "audio")
    res = _probe_and_audio(ep) if run else {}
    return {"episode": str(ep.root), "created": created, **res, "next": [
        f"ave transcribe {ep.root} --speakers 2", f"ave clips prepare {ep.root}", f"ave clips propose {ep.root}",
        "(human) ave ui to keep/skip clips", f"ave shorts build {ep.root} --layout audiogram", f"ave studio adopt {ep.root}"]}


def single_camera_shim(ep: Episode) -> dict:
    """Spec 9.3. FIXTURE-PROVEN (tests/studio/test_ingest.py): with exactly one camera that carries its own audio and no recorder, plan-cuts,
    assemble, cleanup and render all work, but `ave sync` refuses ("Need at least two files with audio") and the planner expects a `cameras`
    entry. This writes exactly those two things (a one-file sync.json with offset 0, and `cameras: {A: ...}`) and nothing else; the planner
    is unchanged. Speakers are mapped to camera A once a transcript exists. Idempotent."""
    info = read_json(ep.artifact("media.json")) if ep.artifact("media.json").exists() else {}
    videos = [f for f in info.get("files", []) if f.get("kind") == "video"]
    audios = [f for f in info.get("files", []) if f.get("kind") == "audio"]
    if len(videos) != 1 or audios:
        return {"applied": False, "why": f"needs exactly one video file and no separate audio recorder (found {len(videos)} video, {len(audios)} audio)"}
    cam = videos[0]["file"]
    meta = read_json(ep.meta_path)
    changed = []
    if (meta.get("cameras") or {}) == {}:
        meta["cameras"] = {"A": {"file": cam, "role": "host"}}
        changed.append("cameras")
    meta.setdefault("audio_source", cam)
    if not meta.get("audio_source"):
        meta["audio_source"] = cam
    tr = ep.artifact("transcript.json")
    if tr.exists() and not (meta.get("speakers") or {}):
        sp_ids = sorted((read_json(tr).get("speakers") or {}))
        if sp_ids:
            meta["speakers"] = {s: {"name": s, "camera": "A"} for s in sp_ids}
            changed.append("speakers")
    write_json(ep.meta_path, meta)
    syn = ep.artifact("sync.json")
    if not syn.exists():
        write_json(syn, {"reference": cam, "warnings": ["single camera: nothing to align (written by `ave studio ingest video`)"],
                         "files": {cam: {"start_offset": 0.0, "psr": None, "drift_ms": 0.0}}})
        changed.append("sync.json")
    return {"applied": True, "camera": cam, "changed": changed}


def video(sp, file: str | Path, *, name: str | None = None, run: bool = True) -> dict:
    """A video file (or an existing episode folder, to finish the shim after transcription) -> an episode ready for the shorts path."""
    p = Path(file).expanduser()
    if p.is_dir():
        ep = Episode(p)
        ep.require()
        created = False
    else:
        ep, created = _new_episode(sp, p, name, VIDEO_EXTS, "video")
        if run:
            _probe_and_audio(ep)
    shim = single_camera_shim(ep) if ep.artifact("media.json").exists() else {"applied": False, "why": "run `ave ingest` first"}
    return {"episode": str(ep.root), "created": created, "single_camera": shim, "next": [
        f"ave transcribe {ep.root} --speakers 1", f"ave studio ingest video {ep.root}    (maps the speakers to the camera after transcription)",
        f"ave plan-cuts {ep.root}", f"ave assemble {ep.root}", f"ave cleanup {ep.root}", f"ave clips prepare {ep.root}", f"ave clips propose {ep.root}",
        f"ave shorts build {ep.root}", f"ave studio adopt {ep.root}"]}


# ------------------------------------------------------------------------------------------------ episode

def episode(sp, path: str | Path) -> dict:
    """What an existing episode already offers a campaign: approved clips, content cards, package drafts, queue items. Read-only."""
    ep = Episode(path)
    ep.require()
    out: dict[str, Any] = {"episode": str(ep.root), "title": read_json(ep.meta_path).get("title")}
    clips = ep.artifact("clips.json")
    out["approved_clips"] = ([{"id": c["id"], "title": c.get("title"), "duration": c.get("duration"), "text": (c.get("text") or "")[:160]}
                              for c in read_json(clips).get("clips", []) if c.get("status") == "approved"] if clips.exists() else [])
    cdir = ep.work / "content"
    cards = []
    for f in sorted(cdir.glob("*.v*.json")) if cdir.is_dir() else []:
        try:
            doc = read_json(f)
        except (OSError, ValueError):
            continue
        cards += [{"doc": f.name, "item": i["id"], "template": i["template"], "status": i["review"]["status"], "outputs": len(i.get("outputs") or [])}
                  for i in doc.get("items", [])]
    out["content_items"] = cards
    out["queue"] = [{"id": i["id"], "status": i["status"], "title": i.get("title"), "platforms": i.get("platforms")} for i in pq.list_items(ep)]
    pkg = sorted((ep.exports / "package").glob("*/manifest.json")) if (ep.exports / "package").is_dir() else []
    out["packages"] = [str(p.parent.relative_to(ep.root)) for p in pkg]
    out["next"] = [f"ave studio adopt {ep.root}    (queue items become posts that reference the episode's queue)",
                   "ave studio recipe show episode-to-campaign"]
    return out


# ------------------------------------------------------------------------------------------------ adopt

def _account_for(sp, platform: str) -> dict | None:
    from . import policy
    for p in sorted(sp.accounts_dir.glob("*.yaml")) if sp.accounts_dir.is_dir() else []:
        cfg = policy.account_config(sp, p.stem)
        if cfg.get("platform") == platform:
            return cfg
    return None


def adopt(studio, path: str | Path, *, by: str = "claude", actor: Actor = AGENT) -> dict:
    """For each queue item of the episode create a Post with a Variant per platform that REFERENCES the item (`queue = {root, id}`):
    approval, bytes, results and crash recovery stay in the episode's queue; the scheduler builds `Episode(root)` for such variants. The
    item's files are COPIED (never moved) into the library by sha as `source.type: own`. The item's `origin.key` (else its id) is recorded
    on the post so adopting twice never duplicates. Items with no studio account for their platform are reported, not guessed."""
    from .. import library
    sp = studio.sp
    ep = Episode(path)
    ep.require()
    root = str(ep.root)
    seen = {((p.get("idea") or {}).get("origin") or {}).get("ref") for p in store.list_posts(sp)}
    adopted, skipped = [], []
    for item in pq.list_items(ep):
        key = f"{root}::{(item.get('origin') or {}).get('key') or item['id']}"
        if key in seen:
            skipped.append({"item": item["id"], "why": "already adopted"})
            continue
        if item["status"] in ("rejected",):
            skipped.append({"item": item["id"], "why": "rejected in the episode"})
            continue
        files = [Path(f) for f in pq.resolve_files(ep, item)]
        media = []
        alts = item.get("alt_texts") or []
        for i, f in enumerate(files):
            if not f.is_file():
                continue
            kind_ok = f.suffix.lower() in library.registry.KIND_BY_EXT
            if not kind_ok:
                continue
            rec = library.add_file(sp, f, source={"type": "own"}, by=by, alt=(alts[i] if i < len(alts) else ""), attested=False,
                                   origin={"episode": ep.root.name, "path": ep.rel(f), "queue_item": item["id"]})
            media.append({"slot": i, "asset": rec["id"], "role": "video" if rec["kind"] == "video" else "image",
                          "alt": alts[i] if i < len(alts) else "", "crop": None})
        variants = []
        for platform in item.get("platforms") or []:
            acct = _account_for(sp, platform)
            if acct is None:
                skipped.append({"item": item["id"], "platform": platform, "why": f"no studio account for {platform}: `ave studio account add`"})
                continue
            variants.append((acct, platform))
        if not variants:
            continue
        post = studio.create_post(idea=item.get("title") or item["id"], actor=actor, copy={"base": item.get("caption") or item.get("description") or ""},
                                  media=media, origin={"type": "episode", "ref": key, "passages": []},
                                  reason=f"adopted from {ep.root.name} queue item {item['id']}")
        rev = post["rev"]
        for acct, platform in variants:
            post = studio.add_variant(post["id"], account=acct["id"], platform=platform,
                                      post_type=item.get("post_type") or (acct.get("post_types") or ["video"])[0], expected_rev=rev, actor=actor)
            rev = post["rev"]
        raw = store.load_post(sp, post["id"])
        for v in raw["variants"]:
            if item.get("schedule"):
                from ..schedule import slots
                from datetime import datetime
                from zoneinfo import ZoneInfo
                try:
                    at = datetime.fromisoformat(item["schedule"]).astimezone(ZoneInfo(acct["tz"] if acct.get("tz") else "UTC"))
                    v["schedule"] = slots.make_schedule(at, slot=None, set_by="agent")
                except (ValueError, KeyError):
                    pass
        store.save_post(sp, raw, expected_rev=raw["rev"], actor=actor, op="adopt", detail={"episode": root, "item": item["id"]}, now=studio.now())
        with pq._lock(ep, item["id"]):                 # the legacy `ave publish run` now refuses this item (it belongs to the studio's gates)
            cur = pq.load(ep, item["id"])
            cur["studio_bound"] = {"studio": str(sp.root), "post": post["id"], "at": iso_utc(studio.now())}
            pq.save(ep, cur)
        adopted.append({"item": item["id"], "post": post["id"], "variants": [v["id"] for v in raw["variants"]], "status": item["status"]})
    return {"episode": root, "adopted": adopted, "skipped": skipped,
            "note": "Adopted posts reference the episode's files in the library as UNATTESTED `own` claims and carry NO queue item: the episode's approval "
                    "does not transfer. Next: a human runs `ave studio library attest`, then `ave studio queue sync`, then approves the studio item "
                    "(`ave studio approve`). The episode's queue item is stamped so the legacy `ave publish run` refuses it. The episode's inputs (music, "
                    "B-roll) keep their license trail in the episode's own license log (`ave final` checks it)."}
