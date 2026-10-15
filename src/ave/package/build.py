"""`ave package`: plan, then assemble, the release package for one episode.

    <exports>/package/<date>[-vN]/
      episode/        the latest final render
      captions/       SRT (the exact-match one from the final) + VTT
      audio/          podcast MP3 or M4A with chapters (ID3 CHAP) at the podcast loudness
      thumbnails/     approved, 1280x720, under 2 MB
      shorts/<plat>/  approved clips' vertical exports, one per platform
      images/         approved quote cards and carousels (alt text in the manifest and copy/alt-text.md)
      copy/           per-platform post copy drafts + copy.json (the editable source `ave package queue` reads)
      drafts/         everything NOT approved or not up to the delivery rules, each marked DRAFT; never in the main folders
      manifest.json  checksums.sha256  index.html  README.md

Rules: a folder is never overwritten (a changed re-run becomes -v2, -v3; an identical one reports "unchanged"); a missing piece
becomes a checklist line, never a failure; files are COPIED when the volume has room (keeping a free-space reserve), otherwise
REFERENCED by path in the manifest with a flag. No symlinks, no hardlinks (ExFAT has neither). Nothing here sends anything.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import shutil
import time
from pathlib import Path

from .. import captions as capmod, ffutil, progress
from ..episode import Episode, read_json
from . import audio as A, chapters as C, collect as K, postcopy as P

INCLUDE_ALL = ("video", "captions", "audio", "thumbnails", "shorts", "images", "copy", "drafts")
GENERATED_TEXT_ESTIMATE = 400_000      # copy files, index.html, manifest, README: generous
CHUNK = 8 * 1024 * 1024
BACKUP_SUFFIX = ".partial"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def package_root(ep: Episode) -> Path:
    return ep.exports / "package"


def status_path(ep: Episode) -> Path:
    return ep.work / "package" / "status.json"


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_path(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def parse_include(spec: str | None) -> set[str]:
    if not spec:
        return set(INCLUDE_ALL)
    want = {s.strip() for s in spec.split(",") if s.strip()}
    bad = want - set(INCLUDE_ALL)
    if bad:
        raise SystemExit(f"--include takes a comma list of: {', '.join(INCLUDE_ALL)} (got {', '.join(sorted(bad))})")
    return want


def vtt_from_srt(srt: Path) -> str:
    cues = capmod.parse_srt(srt)
    ts = lambda t: capmod._srt_ts(t).replace(",", ".")
    blocks = [f"{ts(c.start)} --> {ts(c.end)}\n{c.text}" for c in cues]
    return "WEBVTT\n\n" + "\n\n".join(blocks) + ("\n" if blocks else "")


def draft_dest(dest: str) -> str:
    d, _, name = dest.rpartition("/")
    return "drafts/" + (d + "/" if d else "") + "DRAFT-" + name


def existing_ancestor(p: Path) -> Path:
    p = p.resolve() if p.exists() else p
    while not p.exists():
        p = p.parent
    return p


def list_packages(ep: Episode) -> list[Path]:
    root = package_root(ep)
    if not root.is_dir():
        return []
    return sorted(d for d in root.iterdir() if d.is_dir() and not d.name.startswith(".") and (d / "manifest.json").is_file())


def folder_name(date: str, n: int) -> str:
    return date if n == 1 else f"{date}-v{n}"


def next_version(ep: Episode, date: str) -> int:
    root = package_root(ep)
    n = 1
    while (root / folder_name(date, n)).exists():
        n += 1
    return n


def latest_for_date(ep: Episode, date: str) -> Path | None:
    root = package_root(ep)
    n, found = 1, None
    while (root / folder_name(date, n)).exists():
        found = root / folder_name(date, n)
        n += 1
    return found


# ---- planning ----------------------------------------------------------------------------------

def plan(ep: Episode, *, include: set[str] | None = None, audio_fmt: str | None = None) -> dict:
    """Everything the package would hold and every gap, without writing, rendering or hashing anything large."""
    include = include or set(INCLUDE_ALL)
    policy = K.load_policy(ep)
    from ..publish.adapters.base import load_platforms
    plat_cfg = load_platforms()
    meta = K.episode_meta(ep)
    fmt = audio_fmt or policy["audio"]["format"]
    fmts = ["mp3", "aac"] if fmt == "both" else [fmt]
    checklist: list[dict] = []
    entries: list[dict] = []
    notes: list[str] = []

    def check(key, label, state, detail="", required=False):
        checklist.append({"key": key, "label": label, "state": state, "detail": detail, "required": required})

    # the final cut, captions, podcast audio ----------------------------------------------------
    fin = K.find_final(ep)
    doc = duration = None
    chap = {"chapters": [], "source": None, "draft_titles": False, "note": ""}
    credits = {"lines": [], "missing": [], "assets": 0}
    video_entry = None
    if fin:
        prim = fin["primary"]
        doc = K.final_doc(ep, prim)
        try:
            duration = ffutil.probe(prim["path"])["duration"]
        except Exception as e:
            notes.append(f"could not probe {prim['path'].name}: {e}")
            duration = (doc or {}).get("duration")
        credits = K.credits_for(ep, doc)
        chap = C.build(ep, doc, duration or 0.0, policy) if duration else chap
    if "video" in include:
        if fin:
            others = [v for k, v in fin["all"].items() if v is not fin["primary"]]
            for v in [fin["primary"]] + others:
                tag = f".{v['name']}" if v["name"] else ""
                entries.append(K.entry(id=f"episode{tag}", role="episode_video", src=v["path"], dest=f"episode/{v['path'].name}",
                                       approval={"status": "unrecorded", "note": "the final cut is a human checkpoint; its approval is not machine-recorded"},
                                       source={"stage": "final", "version": v["version"], "file": ep.rel(v["path"]), "session": v["name"]},
                                       license={"credits": credits["lines"], "missing": credits["missing"]},
                                       extra={"duration": duration if v is fin["primary"] else None, "preset": v["preset"]}))
            video_entry = entries[0]
            if not fin["primary"]["name"] and not others:
                check("final_cut", "Final cut", "ok", f"{fin['primary']['path'].name} (final v{fin['primary']['version']})", True)
            elif fin["primary"]["name"]:
                check("final_cut", "Final cut", "warn", "only per-session finals exist (" + ", ".join(sorted(k for k in fin["all"] if k)) +
                      "); no combined episode final was rendered", True)
            else:
                check("final_cut", "Final cut", "ok", f"{fin['primary']['path'].name} (+ {len(others)} per-session final(s))", True)
        else:
            edits = K.final_edits(ep)
            have = (" (final EditDoc exists: " + ", ".join(f"{k or 'main'} v{v}" for k, v in sorted(edits.items(), key=lambda kv: str(kv[0]))) +
                    "; render it with `ave final`/`ave render`)") if edits else ""
            check("final_cut", "Final cut", "missing", "final cut not rendered" + have, True)
    srt = K.find_srt(ep, fin["primary"], doc) if fin else None
    if "captions" in include:
        if srt:
            entries.append(K.entry(id="captions.srt", role="captions_srt", src=srt, dest=f"captions/{srt.name}",
                                   approval={"status": "unrecorded"}, source={"stage": "final", "version": fin["primary"]["version"], "file": ep.rel(srt)}))
            entries.append(K.entry(id="captions.vtt", role="captions_vtt", src=None, dest=f"captions/{srt.stem}.vtt", approval={"status": "unrecorded"},
                                   source={"stage": "final", "version": fin["primary"]["version"], "derived_from": ep.rel(srt)},
                                   extra={"gen": "vtt", "gen_src": srt, "text": vtt_from_srt(srt)}))
            check("captions", "Captions (SRT + VTT)", "ok", srt.name, True)
        else:
            check("captions", "Captions (SRT + VTT)", "missing", "no SRT for the final" + ("" if fin else " (there is no final cut yet)"), True)
    if "audio" in include:
        if not fin:
            check("podcast_audio", "Podcast audio", "missing", "no final cut to take the audio from", True)
        else:
            for f in fmts:
                if not A.encoder_available(f):
                    check("podcast_audio", "Podcast audio", "missing", f"this ffmpeg has no {f} encoder", True)
                    continue
                stem = fin["primary"]["path"].stem
                entries.append(K.entry(id=f"podcast.{f}", role="podcast_audio", src=None, dest=f"audio/{stem}{A.EXT[f]}",
                                       approval={"status": "unrecorded"}, source={"stage": "final", "version": fin["primary"]["version"],
                                                                                  "derived_from": ep.rel(fin["primary"]["path"])},
                                       extra={"gen": "audio", "format": f, "video_src": fin["primary"]["path"], "estimate": A.estimate_bytes(duration or 0.0, f, policy), "duration": duration}))
            if any(e["role"] == "podcast_audio" for e in entries):
                check("podcast_audio", "Podcast audio with chapters", "ok", f"{'/'.join(fmts)} at {policy['audio']['target_lufs']} LUFS (measured after encoding)", True)
    if fin and ({"audio", "copy"} & include):
        if chap["chapters"]:
            ok_yt = len(chap["chapters"]) >= policy["chapters"]["youtube_min_chapters"]
            check("chapters", "Chapters", "warn" if chap["draft_titles"] or not ok_yt else "ok",
                  f"{len(chap['chapters'])} from {chap['source']}" + (f"; {chap['note']}" if chap["note"] else "") +
                  ("" if ok_yt else f"; YouTube needs {policy['chapters']['youtube_min_chapters']}+"))
        else:
            check("chapters", "Chapters", "missing", chap["note"] or "no chapters")
    if fin and credits.get("assets"):
        if credits["missing"]:
            check("licenses", "License credits", "warn", f"{len(credits['missing'])} of {credits['assets']} music/B-roll files have no license-log entry: " +
                  ", ".join(credits["missing"][:3]))
        else:
            check("licenses", "License credits", "ok", f"{len(credits['lines'])} credit line(s) for {credits['assets']} licensed asset(s)")
    elif credits.get("error"):
        check("licenses", "License credits", "warn", credits["error"])

    # content: thumbnails, quote cards, carousels -----------------------------------------------
    content = K.find_content(ep, policy)
    if "thumbnails" in include:
        ts = content["thumbnails"]
        good = [e for e in ts if not e["draft"]]
        if good:
            check("thumbnails", "Thumbnail (1280x720, under 2 MB)", "ok", ", ".join(Path(e["dest"]).name for e in good), True)
        elif ts:
            check("thumbnails", "Thumbnail (1280x720, under 2 MB)", "draft", "; ".join(dict.fromkeys(r for e in ts for r in e["draft_reasons"])), True)
        else:
            check("thumbnails", "Thumbnail (1280x720, under 2 MB)", "missing", "no thumbnail in the content pipeline (ave content plan, template thumbnail)", True)
        entries += ts
    if "images" in include:
        im = content["images"]
        good = [e for e in im if not e["draft"]]
        if good:
            check("images", "Quote cards and carousels", "ok", f"{len({e['source']['item'] for e in good})} approved item(s), {len(good)} image(s)")
        elif im:
            check("images", "Quote cards and carousels", "draft", f"{len(im)} image(s) not approved yet")
        else:
            check("images", "Quote cards and carousels", "missing", "no approved quote cards or carousels")
        entries += im

    # shorts ------------------------------------------------------------------------------------
    clips = K.load_clips(ep)
    if "shorts" in include:
        sh = K.find_shorts(ep)
        approved_clips = [c for c in clips.values() if c.get("status") == "approved"]
        good = [e for e in sh if not e["draft"]]
        if good:
            check("shorts", "Shorts", "ok", f"{len({e['extra']['slug'] for e in good})} approved clip(s), {len(good)} platform variant(s)")
        elif not approved_clips:
            check("shorts", "Shorts", "missing", "no approved clips" + (f" ({len(clips)} proposed; decide them in `ave clips review`)" if clips else ""))
        elif not sh:
            check("shorts", "Shorts", "missing", f"{len(approved_clips)} approved clip(s) but no exports (run `ave shorts export <ep> --all-approved`)")
        else:
            check("shorts", "Shorts", "draft", f"{len(sh)} export(s) did not pass: " + "; ".join(dict.fromkeys(r for e in sh for r in e["draft_reasons"]))[:300])
        entries += sh

    # drafts: move to drafts/, or drop them if --include omits them -----------------------------
    for e in entries:
        if e["draft"]:
            e["dest"] = draft_dest(e["dest"])
    drafts = [e for e in entries if e["draft"]]
    if "drafts" not in include:
        entries = [e for e in entries if not e["draft"]]
    if drafts:
        check("drafts", "Not approved (in drafts/, marked DRAFT)", "draft",
              f"{len(drafts)} item(s): " + ", ".join(sorted({e['role'] for e in drafts})) + ". Approve them, then run `ave package` again for a new version.")

    # post copy ---------------------------------------------------------------------------------
    blocks: list[dict] = []
    if "copy" in include:
        quotes = [*dict.fromkeys(q["text"] for q in content["quotes"] + K.clip_quotes(clips, meta))]
        q_objs = []
        for text in quotes:
            q_objs.append(next(q for q in content["quotes"] + K.clip_quotes(clips, meta) if q["text"] == text))
        chapter_view = [{**c, "draft": chap["draft_titles"]} for c in chap["chapters"]]
        facts = P.make_facts(meta, q_objs, [c for c in clips.values() if c.get("status") == "approved"], chapter_view, credits["lines"])
        block_txt, ch_problems = C.youtube_block(chap["chapters"], policy) if chap["chapters"] else ("", ["no chapters"] if fin else [])
        blocks += P.compose_youtube(facts, plat_cfg, policy, block_txt, [] if not fin else ch_problems)
        for plat in policy["copy"]["platforms"]:
            if plat in P.PLAN:
                blocks.append(P.compose_post(plat, facts, plat_cfg, policy))
        for e in entries:
            if e["role"] == "short" and not e["draft"]:
                clip, sc = e["extra"]["clip"] or {}, e["extra"]["sidecar"]
                b = P.compose_short(e["extra"]["export_platform"], clip, sc, facts, plat_cfg, policy)
                b["ref"] = e["id"]
                blocks.append(b)
        seen_items = set()
        for e in entries:
            if e["role"] in ("quote_card", "carousel_slide") and not e["draft"] and e["source"]["item"] not in seen_items:
                seen_items.add(e["source"]["item"])
                item = e["extra"]["item"]
                from ..content.queueing import derive_caption
                cap = derive_caption(item)
                plats = [p for p in (item.get("platforms") or []) if p in P.PLAN] or ["instagram", "facebook", "x", "linkedin", "threads", "bluesky"]
                for plat in plats:
                    b = P.compose_image(plat, cap, e["extra"].get("quote"), facts, plat_cfg, policy, f"{plat}: {item['template']} {item['id']}")
                    b["ref"] = item["id"]
                    blocks.append(b)
        for b in blocks:
            b.setdefault("ref", "episode")
            b["id"] = f"{b['kind']}:{b['ref']}:{b['platform']}"
        holders = P.placeholders(blocks)
        n_prob = sum(1 for b in blocks if b["problems"])
        check("post_copy", "Post copy drafts", "warn" if (n_prob or holders) else "ok",
              f"{len(blocks)} block(s) on {len({b['platform'] for b in blocks})} platform(s)" +
              (f"; {n_prob} with problems to look at" if n_prob else "") + (f"; fill in {', '.join(holders)}" if holders else ""))
        if not q_objs:
            checklist[-1]["detail"] += "; no approved quote yet, so the posts carry none"
        entries += _copy_entries(blocks, content, plat_cfg)

    required_missing = [c for c in checklist if c["required"] and c["state"] == "missing"]
    order = {"missing": 0, "warn": 1, "draft": 2, "ok": 3}
    return {"episode": ep.root.name, "title": meta.get("title") or ep.root.name, "include": sorted(include), "policy": policy, "entries": entries,
            "checklist": checklist, "complete": not required_missing, "required_missing": [c["key"] for c in required_missing],
            "blocks": blocks, "chapters": chap, "credits": credits, "duration": duration, "notes": notes, "audio_formats": fmts,
            "meta": meta, "order": order}


def _copy_entries(blocks: list[dict], content: dict, plat_cfg: dict) -> list[dict]:
    """The copy/ files: one .md per platform, copy.json, alt-text.md."""
    out = []
    by_plat: dict[str, list[dict]] = {}
    for b in blocks:
        by_plat.setdefault(b["platform"], []).append(b)
    header = ("DRAFT: written from templates and the quotes you approved, for you to edit. Nothing here has been posted. "
              "Fill every {{PLACEHOLDER}} and TODO before you use it.")
    for plat, bs in sorted(by_plat.items()):
        out.append(K.entry(id=f"copy.{plat}", role="post_copy", dest=f"copy/{plat}.md", approval={"status": "draft", "note": "copy is always a draft for the human"},
                           extra={"text": P.markdown(plat, bs, header), "platform": plat}, platforms=[plat]))
    out.append(K.entry(id="copy.json", role="post_copy", dest="copy/copy.json", approval={"status": "draft"},
                       extra={"text": json.dumps({"version": 1, "draft": True, "note": header, "placeholders": P.placeholders(blocks),
                                                  "blocks": [{k: v for k, v in b.items()} for b in blocks]}, indent=2, ensure_ascii=False) + "\n"}))
    alts = [(e["dest"], e["alt_text"]) for e in content["images"] + content["thumbnails"] if not e["draft"] and e["alt_text"]]
    if alts:
        out.append(K.entry(id="copy.alt-text", role="post_copy", dest="copy/alt-text.md", approval={"status": "draft"},
                           extra={"text": "# Alt text (DRAFT: check each against the image)\n\n" + "\n".join(f"- `{d}`: {t}" for d, t in alts) + "\n"}))
    return out


# ---- sizes and fingerprint ---------------------------------------------------------------------

def entry_bytes(e: dict) -> int:
    x = e["extra"]
    if x.get("gen") == "audio":
        return int(x["estimate"])
    if x.get("text") is not None:
        return len(x["text"].encode("utf-8"))
    if e["src"] is not None and Path(e["src"]).is_file():
        if x.get("convert_jpeg"):
            return 600_000
        return Path(e["src"]).stat().st_size
    return 0


def fingerprint(pl: dict) -> str:
    rows = []
    for e in sorted(pl["entries"], key=lambda e: e["dest"]):
        x = e["extra"]
        if x.get("text") is not None:
            rows.append([e["dest"], "text", sha256_bytes(x["text"].encode("utf-8"))])
        elif x.get("gen") == "audio":
            rows.append([e["dest"], "audio", x["format"], _stat_key(x["video_src"])])
        else:
            rows.append([e["dest"], str(e["src"]), _stat_key(e["src"])])
    return sha256_bytes(json.dumps([sorted(pl["include"]), pl["audio_formats"], rows], sort_keys=True).encode())


def _stat_key(p) -> list | None:
    try:
        st = Path(p).stat()
        return [st.st_size, st.st_mtime_ns]
    except (OSError, TypeError):
        return None


def volume_plan(ep: Episode, entries: list[dict], reserve: int) -> dict:
    """Decide copy vs reference per entry, smallest first, so the most files fit and only the largest are referenced."""
    root = package_root(ep)
    free = shutil.disk_usage(existing_ancestor(root)).free
    gens = sum(entry_bytes(e) for e in entries if e["extra"].get("text") is not None)
    budget = free - reserve - max(gens, GENERATED_TEXT_ESTIMATE)      # text files (copy, index, manifest) are always written
    files = [e for e in entries if e["extra"].get("text") is None]
    copy, ref, skipped = [], [], []
    for e in sorted(files, key=entry_bytes):
        sz = entry_bytes(e)
        if sz <= budget:
            copy.append(e)
            budget -= sz
        elif e["extra"].get("gen"):
            skipped.append(e)       # generated here (audio): cannot be referenced
        else:
            ref.append(e)
    total = sum(entry_bytes(e) for e in entries)
    return {"free_bytes": free, "reserve_bytes": reserve, "total_bytes": total, "copy": copy, "reference": ref, "skipped": skipped,
            "fits": not ref and not skipped}


def summary(pl: dict, vol: dict, ep: Episode, date: str, version: int, unchanged_dir: Path | None) -> dict:
    ref_ids = {id(e) for e in vol["reference"]}
    skip_ids = {id(e) for e in vol["skipped"]}
    files = []
    for e in sorted(pl["entries"], key=lambda e: e["dest"]):
        action = "skipped (no room)" if id(e) in skip_ids else ("reference" if id(e) in ref_ids else ("generate" if e["src"] is None else "copy"))
        files.append({"path": e["dest"], "role": e["role"], "bytes": entry_bytes(e), "action": action, "draft": e["draft"],
                      "approval": e["approval"]["status"], **({"why_draft": e["draft_reasons"]} if e["draft"] else {})})
    return {"episode": pl["episode"], "package_dir": str(package_root(ep) / folder_name(date, version)), "date": date, "package_version": version,
            "unchanged_from": str(unchanged_dir) if unchanged_dir else None,
            "complete": pl["complete"], "missing_required": pl["required_missing"], "checklist": pl["checklist"],
            "files": files, "totals": {"files": len(files), "bytes": vol["total_bytes"], "main_files": sum(1 for f in files if not f["draft"]),
                                       "draft_files": sum(1 for f in files if f["draft"])},
            "volume": {"free_bytes": vol["free_bytes"], "reserve_bytes": vol["reserve_bytes"], "fits": vol["fits"],
                       "will_reference": [e["dest"] for e in vol["reference"]], "will_skip": [e["dest"] for e in vol["skipped"]],
                       "note": "" if vol["fits"] else "not everything fits with the free-space reserve: large files are referenced by path in the manifest (no hardlinks on ExFAT)"}}


# ---- building ----------------------------------------------------------------------------------

def _copy_hash(src: Path, dst: Path) -> str:
    dst.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        while chunk := fi.read(CHUNK):
            h.update(chunk)
            fo.write(chunk)
    return h.hexdigest()


def _jpeg_under(src: Path, dst: Path, max_bytes: int) -> None:
    from PIL import Image
    dst.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as im:
        im = im.convert("RGB")
        for q in (92, 88, 84, 80, 75, 70, 60, 50):
            im.save(dst, "JPEG", quality=q, optimize=True)
            if dst.stat().st_size < max_bytes:
                return
    raise ValueError(f"could not get {src.name} under {max_bytes} bytes as a JPEG")


def build(ep: Episode, *, date: str | None = None, include: set[str] | None = None, audio_fmt: str | None = None,
          dry_run: bool = False, write_status: bool = False, new_version: bool = False, announce=None) -> dict:
    """Plan, then (unless dry_run) assemble the package. Returns the summary dict. Never sends anything."""
    date = date or time.strftime("%Y-%m-%d")
    pl = plan(ep, include=include, audio_fmt=audio_fmt)
    policy = pl["policy"]
    vol = volume_plan(ep, pl["entries"], policy["disk"]["reserve_bytes"])
    fp = fingerprint(pl)
    prev = latest_for_date(ep, date)
    unchanged = None
    if prev and not new_version:
        try:
            if read_json(prev / "manifest.json").get("fingerprint") == fp:
                unchanged = prev
        except (OSError, ValueError):
            pass
    version = next_version(ep, date)
    rep = summary(pl, vol, ep, date, version, unchanged)
    rep["fingerprint"] = fp
    if announce:
        announce(rep)       # the size and free space are reported BEFORE anything is copied
    if unchanged:
        rep.update(status="unchanged", package_dir=str(unchanged),
                   note="nothing changed since this package was built; pass --new-version to make another")
        if write_status and not dry_run:
            write_status_file(ep, rep, built=True, package_dir=unchanged)
        return rep
    if dry_run:
        rep["status"] = "dry-run"
        if write_status:
            write_status_file(ep, rep, built=False, package_dir=None)
        return rep
    final_dir = package_root(ep) / folder_name(date, version)
    tmp = package_root(ep) / ("." + final_dir.name + BACKUP_SUFFIX)
    if tmp.exists():
        shutil.rmtree(tmp)         # our own half-built folder from a crashed run; never a finished package
    tmp.mkdir(parents=True)
    try:
        manifest = _assemble(ep, pl, vol, tmp, date, version, fp)
        tmp.replace(final_dir)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    rep.update(status="built", package_dir=str(final_dir), manifest=str(final_dir / "manifest.json"), checklist=manifest["checklist"],
               totals=manifest["totals"], complete=manifest["complete"])
    write_status_file(ep, rep, built=True, package_dir=final_dir)
    progress.auto_log(ep, f"release package built: {final_dir.name} ({manifest['totals']['files']} files, "
                          f"{'complete' if manifest['complete'] else 'missing ' + ', '.join(pl['required_missing'])}); nothing was sent", "done", "package")
    return rep


def _assemble(ep: Episode, pl: dict, vol: dict, out: Path, date: str, version: int, fp: str) -> dict:
    policy = pl["policy"]
    ref_ids = {id(e) for e in vol["reference"]}
    skip_ids = {id(e) for e in vol["skipped"]}
    files: list[dict] = []
    checklist = [dict(c) for c in pl["checklist"]]
    audio_report = None
    chapters = pl["chapters"]["chapters"]
    meta = pl["meta"]

    def record(e: dict, dest_rel: str | None, sha: str | None, size: int, copied: bool, flag: str | None = None, **more) -> None:
        x = e["extra"]
        rec = {"id": e["id"], "role": e["role"], "path": dest_rel if copied else None,
               "package_path": e["dest"], "reference": None if copied or e["src"] is None else str(Path(e["src"]).resolve()),
               "copied": copied, "flag": flag, "bytes": size, "sha256": sha,
               "duration_s": round(x["duration"], 3) if x.get("duration") else None, "width": x.get("width"), "height": x.get("height"),
               "draft": e["draft"], "draft_reasons": e["draft_reasons"], "approval": e["approval"], "source": e["source"], "license": e["license"],
               "alt_text": e["alt_text"], "platforms": e["platforms"], "generated": e["src"] is None}
        rec.update(more)
        files.append(rec)

    # media first, smallest first is irrelevant here; text last so it can describe what happened
    for e in sorted(pl["entries"], key=lambda e: (e["extra"].get("text") is not None, e["dest"])):
        x = e["extra"]
        dst = out / e["dest"]
        if id(e) in skip_ids:
            for c in checklist:
                if c["key"] == "podcast_audio":
                    c["state"], c["detail"] = "missing", f"not enough free space on the volume for the audio file ({entry_bytes(e)} bytes estimated)"
            continue
        if x.get("gen") == "audio":
            names = [s["name"] for s in (meta.get("speakers") or {}).values() if isinstance(s, dict) and s.get("name")]
            tags = {"title": meta.get("title") or ep.root.name, "artist": ", ".join(names),
                    "album": meta.get("show") or "", "date": time.strftime("%Y"), "comment": "Chapters from " + (pl["chapters"]["source"] or "none")}
            rep = A.build(Path(x["video_src"]), dst, x["format"], chapters, tags, policy)
            audio_report = rep
            for c in checklist:
                if c["key"] == "podcast_audio":
                    c["state"] = "ok" if rep["loudness"]["ok"] else "warn"
                    c["detail"] = (f"{x['format']}, {rep['duration']:.0f}s, {rep['loudness']['lufs']} LUFS (target {rep['loudness']['target_lufs']}), "
                                   f"true peak {rep['loudness']['true_peak_db']} dB, {rep['chapters']} chapter(s)") + \
                                  ("" if not rep["warnings"] else "; " + "; ".join(rep["warnings"]))
            record(e, e["dest"], sha256_path(dst), dst.stat().st_size, True, loudness=rep["loudness"], chapters=rep["chapters"], duration_s=rep["duration"])
        elif x.get("text") is not None:
            dst.parent.mkdir(parents=True, exist_ok=True)
            b = x["text"].encode("utf-8")
            dst.write_bytes(b)
            record(e, e["dest"], sha256_bytes(b), len(b), True)
        elif id(e) in ref_ids:
            record(e, None, sha256_path(Path(e["src"])), Path(e["src"]).stat().st_size, False,
                   flag=f"NOT COPIED: only {vol['free_bytes'] // (1 << 20)} MB free on the volume (reserve {vol['reserve_bytes'] // (1 << 20)} MB); referenced by path, "
                        "this package is not self-contained for this file")
        elif x.get("convert_jpeg"):
            _jpeg_under(Path(e["src"]), dst, policy["thumbnails"]["max_bytes"])
            record(e, e["dest"], sha256_path(dst), dst.stat().st_size, True, flag="converted from PNG to JPEG to fit the 2 MB thumbnail limit")
        else:
            sha = _copy_hash(Path(e["src"]), dst)
            record(e, e["dest"], sha, dst.stat().st_size, True)
    # chapters list (youtube block) -------------------------------------------------------------
    if chapters:
        blk, _ = C.youtube_block(chapters, policy)
        b = (blk + "\n").encode("utf-8")
        (out / "audio").mkdir(exist_ok=True)
        (out / "audio" / "chapters.txt").write_bytes(b)
        files.append({"id": "chapters.txt", "role": "chapters", "path": "audio/chapters.txt", "package_path": "audio/chapters.txt", "reference": None,
                      "copied": True, "flag": "machine-drafted titles" if pl["chapters"]["draft_titles"] else None, "bytes": len(b), "sha256": sha256_bytes(b),
                      "duration_s": None, "width": None, "height": None, "draft": False, "draft_reasons": [], "approval": {"status": "draft"},
                      "source": {"stage": "chapters", "from": pl["chapters"]["source"]}, "license": {}, "alt_text": None, "platforms": ["youtube"], "generated": True})
    drafts = [f for f in files if f["draft"]]
    if drafts:
        (out / "drafts").mkdir(exist_ok=True)
        txt = ("DRAFT: nothing in this folder is approved or ready to publish.\n\n"
               "Each file here is either not approved by the human yet, or did not pass a delivery rule. The reason is next to each file in "
               "manifest.json and the README. Approve it (ave clips review / ave content review), fix the cause, and run `ave package` again: "
               "the result is a new package version, and the item moves to the main folders.\n\n" +
               "\n".join(f"- {d['package_path']}: {'; '.join(d['draft_reasons'])}" for d in drafts) + "\n")
        b = txt.encode("utf-8")
        (out / "drafts" / "DRAFT.txt").write_bytes(b)
        files.append({"id": "drafts.txt", "role": "draft_notice", "path": "drafts/DRAFT.txt", "package_path": "drafts/DRAFT.txt", "reference": None, "copied": True,
                      "flag": None, "bytes": len(b), "sha256": sha256_bytes(b), "duration_s": None, "width": None, "height": None, "draft": True,
                      "draft_reasons": [], "approval": {"status": "draft"}, "source": {}, "license": {}, "alt_text": None, "platforms": [], "generated": True})
    files.sort(key=lambda f: (f["draft"], f["package_path"]))
    totals = {"files": len(files), "bytes": sum(f["bytes"] for f in files), "main_files": sum(1 for f in files if not f["draft"]),
              "draft_files": sum(1 for f in files if f["draft"]), "referenced": sum(1 for f in files if not f["copied"])}
    if totals["referenced"]:
        checklist.append({"key": "volume", "label": "Disk space", "state": "warn", "required": False,
                          "detail": f"{totals['referenced']} large file(s) are referenced by path, not copied (not enough free space with the reserve)"})
    required_missing = [c["key"] for c in checklist if c["required"] and c["state"] == "missing"]
    manifest = {"version": 1, "kind": "release-package", "episode": ep.root.name, "title": pl["title"], "show": meta.get("show"), "date": date,
                "package_version": version, "created": now_iso(), "fingerprint": fp, "complete": not required_missing,
                "missing_required": required_missing, "checklist": checklist, "credits": pl["credits"]["lines"],
                "chapters": {"source": pl["chapters"]["source"], "draft_titles": pl["chapters"]["draft_titles"], "items": chapters},
                "audio": audio_report and {k: audio_report[k] for k in ("format", "duration", "loudness", "chapters", "warnings")},
                "volume": {"free_bytes": vol["free_bytes"], "reserve_bytes": vol["reserve_bytes"], "copied_everything": totals["referenced"] == 0,
                           "note": "copied files only: ExFAT has no hardlinks or symlinks, so nothing is linked"},
                "totals": totals, "files": files,
                "sending": "nothing was sent, uploaded, posted or approved; `ave package queue` only adds drafts to the publish queue"}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    # checksums: sha256sum/shasum -a 256 -c format; referenced files are listed apart, with their absolute path
    lines = [f"{f['sha256']}  {f['path']}" for f in files if f["copied"] and f["sha256"]]
    (out / "checksums.sha256").write_text("\n".join(sorted(lines, key=lambda l: l.split("  ", 1)[1])) + "\n", encoding="utf-8")
    refs = [f"{f['sha256']}  {f['reference']}" for f in files if not f["copied"]]
    if refs:
        (out / "checksums.referenced.sha256").write_text("\n".join(refs) + "\n", encoding="utf-8")
    from . import render_html
    (out / "README.md").write_text(render_html.readme(manifest, pl), encoding="utf-8")
    (out / "index.html").write_text(render_html.index(manifest, pl["blocks"]), encoding="utf-8")
    return manifest


# ---- status for the UI -------------------------------------------------------------------------

def write_status_file(ep: Episode, rep: dict, *, built: bool, package_dir: Path | None) -> Path:
    """`<work>/package/status.json`: what the UI shows. Written by real builds (and by `--dry-run --write-status`)."""
    p = status_path(ep)
    p.parent.mkdir(parents=True, exist_ok=True)
    doc = {"version": 1, "updated": now_iso(), "built": built, "package_dir": str(package_dir) if package_dir else None,
           "package_dir_rel": ep.rel(package_dir) if package_dir and package_dir.is_relative_to(ep.root) else None,
           "package_version": rep.get("package_version"), "date": rep.get("date"), "complete": rep.get("complete"),
           "missing_required": rep.get("missing_required") or [c["key"] for c in rep.get("checklist", []) if c.get("required") and c["state"] == "missing"],
           "checklist": rep.get("checklist", []), "totals": rep.get("totals"), "dry_run": not built,
           "queue_hint": f"ave package queue {ep.root}  (adds drafts to the publish queue; the human approves)"}
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(p)
    return p
