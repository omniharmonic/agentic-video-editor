"""`ave content ...`: the promotion kit (quote posts, thumbnails, carousels, reel cards). Opt-in; see docs/content.md.

Commands are mechanical and print JSON. The agent chooses words and design (skills/ave-content-*); code validates, renders,
measures and hashes. Nothing here publishes: `queue` only adds approved items to the publish queue, whose approval is the human's.
"""

from __future__ import annotations

import getpass
import json
from pathlib import Path

from .. import state
from ..episode import Episode, read_json
from ..content import manifest as M
from ..content import settings


def _out(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def _ep(a) -> Episode:
    settings.require_enabled()
    ep = Episode(a.episode)
    ep.require()
    return ep


def _read(path: str, what: str):
    p = Path(path).expanduser()
    if not p.is_file():
        raise SystemExit(f"{what} not found: {p}")
    try:
        return read_json(p)
    except ValueError as e:
        raise SystemExit(f"{p} is not valid JSON: {e}")


def cmd_none(a) -> int:
    cfg = settings.load()
    _out({"enabled": bool(cfg.get("enabled")),
          "message": "ave content is ON for this install." if cfg.get("enabled") else settings.OFF_MESSAGE.format(ov=settings.override_path()),
          "commands": ["templates", "plan", "ingest-writing", "propose", "render", "review", "decide", "queue", "status", "show",
                       "canva-brief", "canva-status", "canva-approve", "canva-log", "canva-import", "canva-fail"],
          "docs": "docs/content.md"})
    return 0 if cfg.get("enabled") else 2


def cmd_templates(a) -> int:
    from ..content import registry as R
    settings.require_enabled()
    reg = R.load_registry()
    _out({"templates": [t.summary() for t in reg.templates.values()], "matrix": reg.matrix(),
          "styles": R.style_names(), "tokens": "motion-kit/brand/tokens.json", "styles_file": "motion-kit/brand/styles.json"})
    return 0


def cmd_plan(a) -> int:
    from ..content import sources
    ep = _ep(a)
    cfg = settings.load()
    out = sources.plan(ep, int(cfg["planning"]["max_quote_chars"]))
    _out(out)
    return 0


def cmd_ingest(a) -> int:
    from ..content import sources
    ep = _ep(a)
    r = sources.ingest_writing(ep, a.folder)
    _out({"writing": str(M.content_dir(ep) / "writing.json"), **r["totals"],
          "files": [{"file": f["file"], "title": f["title"], "passages": len(f["passages"]),
                     "sentences": sum(len(p["sentences"]) for p in f["passages"])} for f in r["files"]],
          "next": "Pick passages/sentence ids (they look like p003 and p003.s2), write picks.json, then: ave content propose <project> picks.json"})
    return 0


def cmd_propose(a) -> int:
    from ..content import propose
    ep = _ep(a)
    r = propose.propose(ep, _read(a.picks, "picks file"), a.id, a.allow_unapproved, allow_incomplete=a.allow_incomplete)
    if r.get("doc"):
        state.set_review(ep, "content plan proposed: trim it, then render and review (ave content render / review)")
    _out(r)
    return 2 if r["rejected"] else 0


def _canva():
    from ..content import render_canva as C
    return C


def cmd_render(a) -> int:
    if a.renderer == "canva":
        return _cmd_render_canva(a)
    from ..content import registry as R
    from ..content import render_motionkit as RM
    ep = _ep(a)
    cfg = settings.load()
    doc = M.load_latest(ep, a.id)
    try:
        new, rep = RM.render_doc(ep, doc, item_ids=a.item or None, force=a.force, allow_unlicensed=a.allow_unlicensed, cfg=cfg)
    except RM.LicenseError as e:
        raise SystemExit(str(e))
    if a.video:
        reg = R.load_registry(cfg=cfg)
        rep["videos"] = []
        for it in new["items"]:
            if it["template"] == "reel-card" and (not a.item or it["id"] in a.item) and it["review"]["status"] != "rejected":
                from ..publish import safety
                out = ep.root / rep["out_dir"] / f"{it['id']}.mov"       # same versioned folder as the PNGs: never over an earlier file
                r = RM.render_card_video(ep, it, out, reg, cfg)
                it["provenance"]["video"] = {"file": ep.rel(out), "sha256": safety.sha256_file(out), "frames": r["frames"], "fps": r["fps"]}
                rep["videos"].append({"id": it["id"], "file": ep.rel(out), "format": r["format"], "place": "ave motion place wraps a template; this card is a standalone overlay clip"})
    if rep["rendered"] or rep.get("videos"):
        path = M.save_new_version(ep, new)
        rep.update(doc=str(path), version=M.list_versions(ep, a.id)[-1])
    rep["next"] = "ave content review <project>   (LOOK at the PNGs first: Read each file and critique it)"
    _out(rep)
    return 2 if rep["failed"] else 0


def _cmd_render_canva(a) -> int:
    """Phase 1 of the Canva handshake: write job files. Calls nothing; the agent makes the MCP calls after the human approves."""
    C = _canva()
    ep = _ep(a)
    cfg = settings.load()
    doc = M.load_latest(ep, a.id)
    rep = C.write_jobs(ep, doc, cfg, item_ids=a.item or None, export_format=a.export)
    rep["canva_calls_made"] = 0
    rep["next"] = ("Show the human each job (fields, steps, the writes it will make). They approve with:  ! ave content canva-approve <project> <job>. "
                   "Then follow skills/ave-canva-render. Failed items stay on the in-house renderer: ave content render <project> --item <id>")
    _out(rep)
    return 2 if rep["failed"] and not rep["jobs"] else 0


def _canva_guard(fn):
    C = _canva()
    try:
        return fn()
    except C.CanvaError as e:
        raise SystemExit(str(e))


def cmd_canva_brief(a) -> int:
    ep = None
    if a.episode:
        ep = _ep(a)
    _out(_canva().brand_brief(ep))
    return 0


def cmd_canva_status(a) -> int:
    C = _canva()
    cfg = settings.require_enabled()
    ep = _ep(a)
    c = C.canva_cfg(cfg)
    _out({"canva_enabled": bool(c.get("enabled")), "kit_id": c.get("kit_id"), "mapped_templates": sorted((c.get("templates") or {})),
          "limits": c.get("limits"),
          "jobs": [{"id": j["id"], "item": j["item"], "status": j["status"], "calls": len(j["log"]), "design_id": j.get("design_id")} for j in C.list_jobs(ep)],
          "note": "This reports local config and job files only; the CLI cannot reach Canva. Ask the agent for live status (skills/ave-canva-render)."})
    return 0


def cmd_canva_approve(a) -> int:
    """The human approves the WRITES a job will make in their Canva. Needs a terminal, like `ave publish approve`."""
    import os
    import sys
    C = _canva()
    ep = _ep(a)
    job = _canva_guard(lambda: C.load_job(ep, a.job))
    _out({"approving": {k: job[k] for k in ("id", "item", "brand_template_id", "format", "target", "export", "fields", "warnings")},
          "will_write": [{"n": s["n"], "tool": s["tool"], "note": s.get("note", "")} for s in job["steps"] if s["writes"]],
          "fields_sha256": job["fields_sha256"]})
    unattended = os.environ.get("AVE_ALLOW_UNATTENDED_APPROVAL") == "1"      # tests/CI only; agents must never set it
    if a.confirm and not unattended:
        raise SystemExit("--confirm is disabled: approval must be typed by the human at a terminal. Ask them to run:  "
                         "! ave content canva-approve %s %s" % (a.episode, a.job))
    confirm = a.confirm if unattended else None
    if not confirm:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise SystemExit("approval needs a human at a terminal. Ask them to run (the leading ! runs it in this session):\n"
                             "  ! ave content canva-approve %s %s" % (a.episode, a.job))
        confirm = input("Type the first 8 characters of fields_sha256 above to allow these Canva writes: ").strip()
    done = _canva_guard(lambda: C.approve_job(ep, a.job, a.by or getpass.getuser(), confirm))
    _out({"id": done["id"], "status": done["status"], "approval": done["approval"]})
    return 0


def cmd_canva_log(a) -> int:
    C = _canva()
    ep = _ep(a)
    job = _canva_guard(lambda: C.log_call(ep, a.job, a.tool, not a.failed, a.summary or "", a.design_id))
    _out({"id": job["id"], "status": job["status"], "calls": len(job["log"])})
    return 0


def cmd_canva_import(a) -> int:
    from ..content import registry as R
    C = _canva()
    ep = _ep(a)
    doc = M.load_latest(ep, a.id)
    reg = R.load_registry(cfg=settings.load())
    new, rep = _canva_guard(lambda: C.import_export(ep, doc, a.job, Path(a.file), reg, design_id=a.design_id))
    path = M.save_new_version(ep, new, reg)
    state.set_review(ep, "content items await your review (open the page, decide, then `ave content decide`)")
    rep.update(doc=str(path), version=M.list_versions(ep, a.id)[-1])
    _out(rep)
    return 0


def cmd_canva_fail(a) -> int:
    from ..content import registry as R
    C = _canva()
    ep = _ep(a)
    doc = M.load_latest(ep, a.id)
    new, job = _canva_guard(lambda: C.fail_job(ep, doc, a.job, a.reason))
    path = M.save_new_version(ep, new, R.load_registry(cfg=settings.load()))
    _out({"job": job["id"], "status": job["status"], "doc": str(path), "item": job["item"],
          "next": f"ave content render {a.episode} --item {job['item']}   (falls back to the in-house renderer; the fallback is recorded in provenance.canva)"})
    return 0


def cmd_review(a) -> int:
    from ..content import review
    ep = _ep(a)
    r = review.build_review(ep, a.id, Path(a.out) if a.out else None)
    state.set_review(ep, "content items await your review (open the page, decide, then `ave content decide`)")
    _out(r)
    return 0


def cmd_decide(a) -> int:
    from ..content import review
    ep = _ep(a)
    r = review.decide(ep, _read(a.decisions, "decisions file"), a.id, a.by or getpass.getuser())
    if r.get("doc"):
        doc = M.load(r["doc"])
        if not any(i["review"]["status"] == "draft" for i in doc["items"]):
            state.set_review(ep, None)
    _out(r)
    return 2 if r["refused"] else 0


def cmd_queue(a) -> int:
    from ..content import queueing
    ep = _ep(a)
    r = queueing.queue_approved(ep, a.id, platforms=[p.strip() for p in a.platforms.split(",") if p.strip()] or None,
                                visibility=a.visibility, schedule=a.schedule, dry_run=a.dry_run)
    _out(r)
    return 2 if r["refused"] or any(q.get("problems") for q in r["queued"]) else 0


def cmd_status(a) -> int:
    ep = _ep(a)
    ids = M.doc_ids(ep)
    docs = []
    for i in ids:
        d = M.load_latest(ep, i)
        counts: dict = {}
        for it in d["items"]:
            counts[it["review"]["status"]] = counts.get(it["review"]["status"], 0) + 1
        docs.append({"id": i, "version": M.list_versions(ep, i)[-1], "items": len(d["items"]), "review": counts,
                     "rendered": sum(1 for it in d["items"] if it["outputs"]),
                     "critic_flagged": [it["id"] for it in d["items"] if (it["review"].get("critic") or {}).get("ok") is False]})
    _out({"enabled": True, "content_dir": str(M.content_dir(ep)), "docs": docs})
    return 0


def cmd_show(a) -> int:
    ep = _ep(a)
    _out(M.load_latest(ep, a.id))
    return 0


def register(sub) -> None:
    p = sub.add_parser("content", help="promotion content: quote posts, thumbnails, carousels, reel cards (opt-in: config/content.yaml)")
    p.set_defaults(fn=cmd_none)
    cs = p.add_subparsers(dest="content_cmd")

    def proj(s, with_id: bool = True):
        s.add_argument("episode")
        if with_id:
            s.add_argument("--id", default="main", help="content doc id (default: main)")

    s = cs.add_parser("templates", help="the registry: templates, slots, and the style x format matrix")
    s.set_defaults(fn=cmd_templates)

    s = cs.add_parser("plan", help="raw material for the agent: approved clips' quotes, episode facts, ingested writing")
    proj(s, False)
    s.set_defaults(fn=cmd_plan)

    s = cs.add_parser("ingest-writing", help="split a folder of Markdown into sentence-addressable passages")
    proj(s, False)
    s.add_argument("folder")
    s.set_defaults(fn=cmd_ingest)

    s = cs.add_parser("propose", help="validate the agent's picks (verbatim quotes!) and write a new ContentDoc version")
    proj(s)
    s.add_argument("picks", help="picks JSON")
    s.add_argument("--allow-unapproved", action="store_true", help="quote clips the human has not approved (only when they said to)")
    s.add_argument("--allow-incomplete", action="store_true",
                   help="OVERRIDE: quote a clip or a passage that starts or ends mid-sentence (otherwise refused; recorded on the source)")
    s.set_defaults(fn=cmd_propose)

    s = cs.add_parser("render", help="render draft items to PNG with the in-house renderer (+ critic); new doc version")
    proj(s)
    s.add_argument("--item", action="append", help="render only this item id (repeatable; re-rendering an approved item voids its approval)")
    s.add_argument("--force", action="store_true")
    s.add_argument("--video", action="store_true", help="also render reel-card items as ProRes 4444 clips")
    s.add_argument("--allow-unlicensed", action="store_true", help="render despite a missing license-log entry (you own the rights)")
    s.add_argument("--renderer", choices=["motion-kit", "canva"], default="motion-kit",
                   help="motion-kit (default, in-house) | canva: write Canva job files only (opt-in, two-phase handshake; calls nothing)")
    s.add_argument("--export", default="png", choices=["png", "jpg", "mp4"], help="with --renderer canva: the export format")
    s.set_defaults(fn=cmd_render)

    s = cs.add_parser("review", help="static contact-sheet page with critic results and approve/reject/nudge controls")
    proj(s)
    s.add_argument("--out", help="folder for index.html (default: <work>/content/review)")
    s.set_defaults(fn=cmd_review)

    s = cs.add_parser("decide", help="apply the downloaded decisions.json (HUMAN's choices) as a new doc version")
    proj(s)
    s.add_argument("decisions")
    s.add_argument("--by", help="who decided (default: the OS user)")
    s.set_defaults(fn=cmd_decide)

    s = cs.add_parser("queue", help="queue APPROVED items into the publish queue as image/carousel posts (never approves or sends)")
    proj(s)
    s.add_argument("--platforms", default="", help="restrict to these platforms")
    s.add_argument("--visibility", default="private", choices=["private", "unlisted", "public"])
    s.add_argument("--schedule")
    s.add_argument("--dry-run", action="store_true", help="show what would be queued")
    s.set_defaults(fn=cmd_queue)

    s = cs.add_parser("status", help="plan counts, render and review state")
    proj(s, False)
    s.set_defaults(fn=cmd_status)

    s = cs.add_parser("show", help="the latest ContentDoc in full")
    proj(s)
    s.set_defaults(fn=cmd_show)

    s = cs.add_parser("canva-brief", help="final palette, fonts (bundled files + licenses), names and card layout to enter by hand in Canva's Brand Kit (one way; nothing is read from Canva)")
    s.add_argument("episode", nargs="?", help="project path: adds the speakers' names and titles from episode.json")
    s.set_defaults(fn=cmd_canva_brief)

    s = cs.add_parser("canva-status", help="Canva config and job files (local only; the CLI cannot reach Canva)")
    proj(s, False)
    s.set_defaults(fn=cmd_canva_status)

    s = cs.add_parser("canva-approve", help="HUMAN act: allow the Canva writes listed in a job (needs a terminal)")
    proj(s, False)
    s.add_argument("job")
    s.add_argument("--by")
    s.add_argument("--confirm", help="disabled unless AVE_ALLOW_UNATTENDED_APPROVAL=1 (tests only)")
    s.set_defaults(fn=cmd_canva_approve)

    s = cs.add_parser("canva-log", help="record one Canva MCP call the agent made for a job (write calls are refused before approval)")
    proj(s, False)
    s.add_argument("job")
    s.add_argument("--tool", required=True, help="the MCP tool name that was called")
    s.add_argument("--summary", default="")
    s.add_argument("--design-id")
    s.add_argument("--failed", action="store_true")
    s.set_defaults(fn=cmd_canva_log)

    s = cs.add_parser("canva-import", help="freeze a downloaded Canva export into the ContentDoc (hash, provenance) as a draft for review")
    proj(s)
    s.add_argument("job")
    s.add_argument("file", help="the exported file, downloaded immediately (export URLs expire in 24 h)")
    s.add_argument("--design-id")
    s.set_defaults(fn=cmd_canva_import)

    s = cs.add_parser("canva-fail", help="record that a Canva job failed; the item falls back to the in-house renderer (recorded, never silent)")
    proj(s)
    s.add_argument("job")
    s.add_argument("--reason", required=True)
    s.set_defaults(fn=cmd_canva_fail)
