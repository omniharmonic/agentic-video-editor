"""`ave package queue`: add a package's APPROVED items to the publish queue as drafts awaiting the human's approval.

This is the only bridge from the package to publishing, and it only QUEUES. It never approves (that is `ave publish approve`, typed by
the human at a terminal, bound to the sha256 of the exact files) and it has no send path: nothing in this module imports or calls
`queue.run`, an adapter's `publish`, or anything that touches a network.

Before queueing, every copied file is re-hashed against the manifest, so the bytes the human approves are the bytes that were
packaged. Items in `drafts/` are never queued. An item already in the queue (same origin key, not rejected) is skipped.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..episode import Episode, read_json
from ..publish import queue as pq, safety
from ..publish.adapters import ALL_PLATFORMS, get_publisher
from . import build as B, postcopy as P

# shorts export platform -> publisher platform (Reels go through the aggregator as "instagram")
EXPORT_TO_PUBLISH = {"youtube": "youtube", "facebook": "facebook", "reels": "instagram", "instagram": "instagram", "x": "x", "tiktok": "tiktok"}


def latest_package(ep: Episode, which: str | None = None) -> Path:
    if which:
        p = Path(which)
        p = p if p.is_absolute() else B.package_root(ep) / which
        if not (p / "manifest.json").is_file():
            raise SystemExit(f"no package at {p} (no manifest.json)")
        return p
    pkgs = B.list_packages(ep)
    if not pkgs:
        raise SystemExit(f"no package yet for {ep.root.name}: run `ave package {ep.root}` first")
    return max(pkgs, key=lambda d: (read_json(d / "manifest.json")["created"], d.name))


def verify(pkg: Path, manifest: dict) -> list[str]:
    """Problems with the package's files (missing or changed since it was built). Copied files and referenced ones are both checked."""
    bad = []
    for f in manifest["files"]:
        p = pkg / f["path"] if f["copied"] else Path(f["reference"])
        if f["role"] == "post_copy":       # copy is the editable part: it is meant to change
            continue
        if not p.is_file():
            bad.append(f"{f['package_path']} is missing ({p})")
        elif B.sha256_path(p) != f["sha256"]:
            bad.append(f"{f['package_path']} changed since the package was built")
    return bad


def _block(copy: dict, kind: str, ref: str, platform: str) -> dict | None:
    return next((b for b in copy["blocks"] if b["kind"] == kind and b.get("ref") == ref and b["platform"] == platform), None)


def _path(pkg: Path, f: dict) -> Path:
    return pkg / f["path"] if f["copied"] else Path(f["reference"])


def queue_package(ep: Episode, *, package: str | None = None, platforms: list[str] | None = None, visibility: str = "private",
                  ai_disclosure: bool | None = None, dry_run: bool = False, publishers=None) -> dict:
    pkg = latest_package(ep, package)
    manifest = read_json(pkg / "manifest.json")
    problems = verify(pkg, manifest)
    if problems:
        return {"package": str(pkg), "queued": [], "skipped": [], "refused": problems,
                "next": "the package files do not match its manifest; rebuild it with `ave package <ep> --new-version`"}
    copy = read_json(pkg / "copy" / "copy.json") if (pkg / "copy" / "copy.json").is_file() else {"blocks": []}
    existing = {(i.get("origin") or {}).get("key") for i in pq.list_items(ep) if i.get("origin") and i["status"] != "rejected"}
    out: dict = {"package": str(pkg), "queued": [], "skipped": [], "refused": []}
    main = [f for f in manifest["files"] if not f["draft"]]
    out["skipped"] += [{"item": f["package_path"], "why": "draft, not approved"} for f in manifest["files"] if f["draft"] and f["role"] != "draft_notice"]

    def add(key: str, label: str, **kw) -> None:
        if key in existing:
            out["skipped"].append({"item": label, "why": "already in the publish queue"})
            return
        if dry_run:
            out["queued"].append({"item": label, "platforms": kw["platforms"], "dry_run": True})
            return
        it = pq.create(ep, visibility=visibility, origin={"key": key, "package": pkg.name, "item": label}, publishers=publishers, **kw)
        existing.add(key)
        out["queued"].append({"item": label, "queue_id": it["id"], "status": it["status"], "platforms": it["platforms"], "sha256": it["sha256"],
                              "problems": it["preflight_problems"]})

    def want(ps: list[str]) -> list[str]:
        return [p for p in ps if p in ALL_PLATFORMS and (not platforms or p in platforms)]

    for f in main:
        role = f["role"]
        p = _path(pkg, f)
        if role == "episode_video" and not f["source"].get("session"):
            for plat in want(["youtube", "facebook"]):
                if plat == "youtube":
                    t, d, tg = _block(copy, "youtube_title", "episode", "youtube"), _block(copy, "youtube_description", "episode", "youtube"), \
                        _block(copy, "youtube_tags", "episode", "youtube")
                    if not (t and d):
                        out["skipped"].append({"item": f["package_path"], "platform": plat, "why": "no YouTube copy in the package (build with --include copy)"})
                        continue
                    kw = dict(title=t["text"], description=d["text"], tags=[x.strip() for x in (tg["text"] if tg else "").split(",") if x.strip()])
                else:
                    post = _block(copy, "episode_post", "episode", "facebook")
                    t = _block(copy, "youtube_title", "episode", "youtube")
                    kw = dict(title=(t or {"text": manifest["title"]})["text"], description=(post or {"text": ""})["text"], caption=(post or {"text": ""})["text"])
                add(f"package:video:{f['sha256']}:{plat}:{visibility}", f"{f['package_path']} -> {plat}", video=p, platforms=[plat],
                    ai_disclosure=ai_disclosure, **kw)
        elif role == "short":
            plat = EXPORT_TO_PUBLISH.get(f["platforms"][0] if f["platforms"] else "")
            if not want([plat] if plat else []):
                out["skipped"].append({"item": f["package_path"], "why": f"no publisher for {f['platforms']}" if not plat else f"{plat} not selected"})
                continue
            b = _block(copy, "short_post", f["id"], P.EXPORT_TO_COPY.get(f["platforms"][0], f["platforms"][0]))
            sc_title = (b or {}).get("title") or f["id"]
            text = (b or {}).get("text", "")
            tags = ["Shorts"] if plat == "youtube" else []
            add(f"package:short:{f['sha256']}:{plat}:{visibility}", f"{f['package_path']} -> {plat}", video=p, platforms=[plat], title=sc_title,
                description=text, caption=text, tags=tags, ai_disclosure=ai_disclosure)
    # images: a quote card is one file per format, a carousel is the ordered slides of one item
    items: dict[str, list[dict]] = {}
    for f in main:
        if f["role"] in ("quote_card", "carousel_slide"):
            items.setdefault(f["source"]["item"], []).append(f)
    for item_id, fs in sorted(items.items()):
        fs = sorted(fs, key=lambda x: x["package_path"])
        carousel = fs[0]["role"] == "carousel_slide"
        groups = [fs] if carousel else [[x] for x in fs]
        for g in groups:
            plats = want(sorted({pl for x in g for pl in x["platforms"]}) or [])
            sendable = []
            for pl in plats:
                pub = (publishers or {}).get(pl) or get_publisher(pl)
                if getattr(pub, "carries_images", True):
                    sendable.append(pl)
                else:
                    out["skipped"].append({"item": g[0]["package_path"], "platform": pl, "why": f"the {pl} adapter publishes video only; this image cannot be posted there yet"})
            if not sendable:
                continue
            caption = ""
            for pl in sendable:
                b = _block(copy, "image_post", item_id, pl)
                if b:
                    caption = b["text"]
                    break
            if any(not (x.get("alt_text") or "").strip() for x in g):
                out["refused"].append(f"{g[0]['package_path']} has no alt text")
                continue
            shas = ",".join(x["sha256"] for x in g)
            add(f"package:image:{shas}:{','.join(sendable)}:{visibility}", f"{item_id} -> {','.join(sendable)}", files=[_path(pkg, x) for x in g],
                media_type="carousel" if carousel else "image", alt_texts=[x["alt_text"] for x in g], platforms=sendable,
                title=(caption.split("\n")[0][:70] or item_id), description=caption, caption=caption, ai_disclosure=ai_disclosure)
    out["next"] = ("Nothing was approved or sent. Ask the human to review the exact files and run `! ave publish approve <project> <id>` for each queued id "
                   "(they type the start of its sha256). `ave publish run <project> <id>` is a dry run until then, and it sends only when the human adds the send flag."
                   if out["queued"] else "nothing queued")
    if not dry_run:
        try:
            st = B.status_path(ep)
            doc = read_json(st) if st.is_file() else {}
            doc["queued"] = {"at": B.now_iso(), "package": pkg.name, "items": [q["queue_id"] for q in out["queued"] if q.get("queue_id")]}
            st.parent.mkdir(parents=True, exist_ok=True)
            st.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass
    return out
