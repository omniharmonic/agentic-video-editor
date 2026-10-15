"""`ave content queue`: approved content items enter the publish queue as image or carousel posts.

This only QUEUES. The queue's own gate is unchanged and is the human's: `ave publish approve` binds an approval to the sha256 of
every file (a carousel hashes its ordered slides) and to the alt texts, and `ave publish run` is a dry run unless `--yes`.
A queued item records where it came from (`origin`), so queueing twice never duplicates a post.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..episode import Episode
from ..publish import queue, safety
from ..publish.adapters import ALL_PLATFORMS, get_publisher
from . import manifest as M
from . import people
from . import registry as R


def derive_title(item: dict, speakers: list[dict] | None = None) -> str:
    """The queue requires a title; images have none, so use the plainest true one."""
    s = item["slots"]
    if item["template"] == "quote-post":
        s = R.effective_slots(R.load_registry().get("quote-post").slots, s, speakers)
        who = f" ({s['attribution']})" if s.get("attribution") else ""
        return (s["quote"][:70].rstrip() + ("..." if len(s["quote"]) > 70 else "")) + who
    if item["template"] == "thumbnail":
        return s["title"]
    if item["template"] == "carousel":
        return s["cover"]["title"]
    return item["id"]


def derive_caption(item: dict, speakers: list[dict] | None = None) -> str:
    """A caption from the slots when the agent wrote none: the words, who said them, nothing added. The show name is not repeated."""
    if item.get("caption"):
        return item["caption"]
    s = item["slots"]
    if item["template"] == "quote-post":
        s = R.effective_slots(R.load_registry().get("quote-post").slots, s, speakers)
        who = f"\n\n{s['attribution']}" + (f", {s['title']}" if s.get("title") else "") if s.get("attribution") else ""
        return f"“{s['quote']}”{who}"
    if item["template"] == "thumbnail":
        return s["title"] + (f" — with {s['guest']}" if s.get("guest") else "")
    if item["template"] == "carousel":
        return s["cover"]["title"] + ("\n\n" + s["closing"]["cta"] if s["closing"].get("cta") else "")
    return ""


def queue_approved(ep: Episode, doc_id: str = "main", *, platforms: list[str] | None = None, visibility: str = "private",
                   schedule: str | None = None, dry_run: bool = False, publishers=None) -> dict:
    """Queue every approved item (once). Returns {queued, skipped, problems}. Never approves and never sends."""
    doc = M.load_latest(ep, doc_id)
    version = M.list_versions(ep, doc_id)[-1]
    existing = {(i.get("origin") or {}).get("key"): i["id"] for i in queue.list_items(ep) if i.get("origin")
                and i["status"] != "rejected"}
    out = {"queued": [], "skipped": [], "refused": []}
    spk = people.load_speakers(ep)
    for item in doc["items"]:
        if item["review"]["status"] != "approved":
            out["skipped"].append({"item": item["id"], "why": f"{item['review']['status']}, not approved"})
            continue
        bad = []
        for o in item["outputs"]:
            f = ep.root / o["file"]
            if not f.is_file():
                bad.append(f"{o['file']} is missing")
            elif safety.sha256_file(f) != o["sha256"]:
                bad.append(f"{o['file']} changed since it was reviewed; re-render and approve again")
            if not o["alt_text"].strip():
                bad.append(f"{o['file']} has no alt text")
        if bad:
            out["refused"].append({"item": item["id"], "problems": bad})
            continue
        # one queue item per distinct file list; platforms sharing the same files go together
        by_platform: dict[str, list[dict]] = {}
        for o in item["outputs"]:
            if not platforms or o["platform"] in platforms:
                by_platform.setdefault(o["platform"], []).append(o)
        groups: dict[tuple, dict] = {}
        for plat, outs in by_platform.items():
            outs = sorted(outs, key=lambda x: x.get("slide", 0))
            g = groups.setdefault(tuple(o["file"] for o in outs), {"platforms": [], "outs": outs})
            g["platforms"].append(plat)
        mtype = "carousel" if item["template"] == "carousel" else "image"
        for files, g in groups.items():
            plats = [p for p in g["platforms"] if p in ALL_PLATFORMS]
            for p in [p for p in g["platforms"] if p not in ALL_PLATFORMS]:
                out["skipped"].append({"item": item["id"], "platform": p, "why": f"no publisher named {p!r}"})
            sendable = []
            for p in plats:
                pub = (publishers or {}).get(p) or get_publisher(p)
                if getattr(pub, "carries_images", True):
                    sendable.append(p)
                else:
                    out["skipped"].append({"item": item["id"], "platform": p, "why": f"the {p} adapter publishes video only; this image cannot be posted there yet"})
            if not sendable:
                continue
            key = f"{doc_id}:{item['id']}:{','.join(sorted(sendable))}:{visibility}:{schedule or ''}:" + safety.media_sha256([ep.root / f for f in files], mtype)
            if key in existing:
                out["skipped"].append({"item": item["id"], "platforms": sendable, "why": f"already queued as {existing[key]}"})
                continue
            if dry_run:
                out["queued"].append({"item": item["id"], "platforms": sendable, "files": list(files), "dry_run": True})
                continue
            it = queue.create(ep, files=[ep.root / f for f in files], media_type=mtype,
                              alt_texts=[o["alt_text"] for o in g["outs"]], platforms=sendable, title=derive_title(item, spk),
                              description=derive_caption(item, spk), caption=derive_caption(item, spk), visibility=visibility, schedule=schedule,
                              ai_disclosure=item["license"]["ai_generated"], publishers=publishers,
                              origin={"key": key, "doc": doc_id, "version": version, "item": item["id"]})
            out["queued"].append({"item": item["id"], "queue_id": it["id"], "status": it["status"], "platforms": sendable,
                                  "sha256": it["sha256"], "problems": it["preflight_problems"]})
    out["next"] = ("Ask the human to review the exact files and run `! ave publish approve <project> <id>` for each queued id; "
                   "`ave publish run <project> <id>` is a dry run until they have, and sends nothing without --yes.") if out["queued"] else "nothing queued"
    return out
