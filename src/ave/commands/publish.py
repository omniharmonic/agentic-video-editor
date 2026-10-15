"""`ave publish ...`: approval-gated publishing. Nothing here is ever called by another command."""

from __future__ import annotations

import getpass
import os
import json
import sys

from ..episode import Episode
from ..publish import queue, safety
from ..publish.adapters import ALL_PLATFORMS, get_publisher


def _ep(a) -> Episode:
    ep = Episode(a.episode)
    ep.require()
    return ep


def _out(obj) -> None:
    print(json.dumps(safety.redact_obj(obj), indent=2, ensure_ascii=False))


def _yesno(v: str | None):
    if v is None:
        return None
    if v.lower() in ("yes", "y", "true", "1"):
        return True
    if v.lower() in ("no", "n", "false", "0"):
        return False
    raise SystemExit("--ai-disclosure takes yes or no")


def _platforms(s: str) -> list[str]:
    ps = [x.strip().lower() for x in s.split(",") if x.strip()]
    bad = [p for p in ps if p not in ALL_PLATFORMS]
    if bad:
        raise SystemExit(f"unknown platform(s) {bad}; choose from {', '.join(ALL_PLATFORMS)}")
    return ps


# shorts export platform -> publisher platform (Reels go through the aggregator as "instagram")
_EXPORT_TO_PUBLISH = {"youtube": "youtube", "facebook": "facebook", "reels": "instagram", "x": "x"}


def _from_sidecars(ep, a, common) -> tuple[list, list]:
    """Queue the LATEST export per (clip, platform), from the sidecar `ave shorts export` wrote next to each mp4.

    The sidecar carries the title, description and the preflight verdicts, so nothing is guessed from filenames. An export whose
    limits or license coverage failed is skipped (and reported), never queued.
    """
    import re
    from ..episode import read_json
    best: dict[tuple[str, str], tuple[int, dict]] = {}
    for side in sorted((ep.exports / "shorts").glob("*.json")):
        try:
            sc = read_json(side)
        except Exception:
            continue
        m = re.match(r"^(?P<slug>.+)\.v(?P<v>\d+)\.(?P<plat>[a-z]+)$", side.stem)
        if not m or not (ep.root / sc.get("video", "")).exists():
            continue
        key = (m["slug"], m["plat"])
        if key not in best or int(m["v"]) > best[key][0]:
            best[key] = (int(m["v"]), sc)
    if not best:
        raise SystemExit(f"no shorts exports with sidecars in {ep.exports / 'shorts'}; run `ave shorts export {ep.root}` first")
    items, skipped = [], []
    for (slug, plat), (ver, sc) in sorted(best.items()):
        target = _EXPORT_TO_PUBLISH.get(plat)
        why = []
        if target is None or target not in ALL_PLATFORMS:
            why.append(f"no publisher for export platform {plat!r}")
        if not (sc.get("limits") or {}).get("ok", False):
            why.append("platform limits not met: " + "; ".join((sc.get("limits") or {}).get("problems", [])))
        if (sc.get("licenses") or {}).get("ok") is False:
            why.append("license coverage incomplete")
        if (sc.get("encode") or {}).get("problems"):
            why.append("encode problems: " + "; ".join(sc["encode"]["problems"]))
        if why:
            skipped.append({"clip": slug, "platform": plat, "version": ver, "reasons": why})
            continue
        kw = dict(common)
        kw.update(platforms=[target], description=sc.get("description") or kw.get("description"),
                  caption=sc.get("caption") or kw.get("caption"), tags=sc.get("tags") or kw.get("tags") or [])
        items.append(queue.create(ep, video=ep.root / sc["video"], title=sc.get("title") or slug, **kw))
    return items, skipped


def cmd_queue(a) -> int:
    ep = _ep(a)
    if not a.from_clips and not a.platforms:
        raise SystemExit("--platforms is required (or use --from-clips)")
    common = dict(platforms=_platforms(a.platforms) if a.platforms else [], description=a.description, tags=[t for t in a.tags.split(",") if t],
                  caption=a.caption, schedule=a.schedule, visibility=a.visibility, ai_disclosure=_yesno(a.ai_disclosure),
                  made_for_kids=a.made_for_kids, fmt=a.format)
    if a.from_clips:
        items, skipped = _from_sidecars(ep, a, common)
        if skipped:
            print(json.dumps({"skipped": skipped}, indent=2), file=sys.stderr)
    else:
        if not a.video or not a.title:
            raise SystemExit("--video and --title are required (or use --from-clips)")
        items = [queue.create(ep, video=a.video, title=a.title, **common)]
    _out([{"id": i["id"], "status": i["status"], "sha256": i["sha256"], "problems": i["preflight_problems"]} for i in items])
    return 0 if all(i["status"] == "awaiting_approval" for i in items) else 2


def cmd_list(a) -> int:
    _out([{"id": i["id"], "status": i["status"], "title": i["title"], "platforms": i["platforms"]}
          for i in queue.list_items(_ep(a))])
    return 0


def cmd_show(a) -> int:
    _out(queue.load(_ep(a), a.id))
    return 0


def cmd_approve(a) -> int:
    """Approval is a HUMAN act. It needs the hash prefix of the exact file reviewed (typed at a prompt, or --confirm-sha)."""
    ep = _ep(a)
    item = queue.load(ep, a.id)
    sha = queue.current_sha(ep, item)
    shown = {k: item.get(k) for k in ("id", "video", "platforms", "title", "description", "visibility", "schedule", "ai_disclosure", "made_for_kids")}
    if queue.media_type(item) != "video":
        # an image/carousel approval covers every file and the alt texts: show them all, per file, so the human reviews each one
        shown.pop("video", None)
        shown.update(media_type=item["media_type"], files=[{"file": f, "sha256": safety.sha256_file(p)}
                                                          for f, p in zip(item["files"], queue.resolve_files(ep, item))],
                     alt_texts=item.get("alt_texts"), caption=item.get("caption"))
    _out({"approving": shown, "sha256": sha})
    # Approval must be a human at a real terminal. The hash is printed above, so a flag that skips the terminal would let an
    # agent approve its own post; it is honoured only when the human has explicitly opted in via AVE_ALLOW_UNATTENDED_APPROVAL=1
    # (meant for tests/CI). Agents must NEVER set that variable.
    unattended = os.environ.get("AVE_ALLOW_UNATTENDED_APPROVAL") == "1"
    confirm = a.confirm_sha if unattended else None
    if a.confirm_sha and not unattended:
        raise SystemExit("--confirm-sha is disabled: approval must be typed by the human at a terminal. "
                         "Ask them to run:  ! ave publish approve %s %s" % (a.episode, a.id))
    if not confirm:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise SystemExit("approval needs a human at a terminal. Ask them to run (the leading ! runs it in this session):\n"
                             "  ! ave publish approve %s %s" % (a.episode, a.id))
        confirm = input("Type the first 8 characters of the sha256 above to approve: ").strip()
    try:
        done = queue.approve(ep, a.id, a.by or getpass.getuser(), confirm)
    except ValueError as e:
        raise SystemExit(str(e))
    _out({"id": done["id"], "status": done["status"], "approval": done["approvals"][-1]})
    return 0


def cmd_reject(a) -> int:
    try:
        item = queue.reject(_ep(a), a.id, a.by or getpass.getuser(), a.reason)
    except ValueError as e:
        raise SystemExit(str(e))
    _out({"id": item["id"], "status": item["status"]})
    return 0


def cmd_run(a) -> int:
    """Dry-run unless --yes is given (and --dry-run always wins)."""
    send = bool(a.yes) and not a.dry_run
    try:
        rep = queue.run(_ep(a), a.id, send=send)
    except (RuntimeError, ValueError, FileNotFoundError) as e:
        raise SystemExit(str(e))
    _out(rep)
    if not send and rep["ok"]:
        print("DRY RUN: nothing was sent. Re-run with --yes to publish.", file=sys.stderr)
    return 0 if rep["ok"] else 2


def cmd_resolve(a) -> int:
    try:
        item = queue.resolve(_ep(a), a.id, a.platform, posted_url=a.posted, retry=a.retry, by=getpass.getuser())
    except (ValueError, RuntimeError) as e:
        raise SystemExit(str(e))
    _out({"id": item["id"], "status": item["status"], "results": item["results"]})
    return 0


def cmd_auth(a) -> int:
    p = a.platform
    if p == "youtube":
        from ..publish.adapters.youtube import run_oauth_flow
        if not a.client_secrets:
            raise SystemExit("--client-secrets <desktop-client.json> is required (see docs/publishing-setup.md)")
        _out(run_oauth_flow(a.client_secrets))
    elif p == "facebook":
        if not a.page_id:
            raise SystemExit("--page-id is required; the Page access token is read from a hidden prompt")
        safety.set_secret("facebook_page_id", a.page_id)
        safety.set_secret("facebook_page_token", getpass.getpass("Facebook Page access token (hidden): ").strip())
        _out({"stored": "keychain", "platform": "facebook"})
    elif p == "aggregator":
        safety.set_secret("aggregator_api_key", getpass.getpass("Aggregator API key (hidden): ").strip())
        _out({"stored": "keychain", "platform": "aggregator"})
    else:
        raise SystemExit("auth platforms: youtube, facebook, aggregator")
    return 0


def cmd_status(a) -> int:
    ep = _ep(a)
    items = queue.list_items(ep)
    counts: dict = {}
    for i in items:
        counts[i["status"]] = counts.get(i["status"], 0) + 1
    uncertain = [i["id"] for i in items if any(r.get("state") == "sending" for r in i.get("results", {}).values())]
    _out({"queue": counts, "uncertain": uncertain, "platforms": [get_publisher(p).status() for p in ALL_PLATFORMS]})
    return 0


def register(sub) -> None:
    pub = sub.add_parser("publish", help="approval-gated publishing (dry-run by default; nothing posts without approve + --yes)")
    ps = pub.add_subparsers(dest="publish_cmd", required=True)

    s = ps.add_parser("queue", help="add a video to the approval queue")
    s.add_argument("episode")
    s.add_argument("--video")
    s.add_argument("--platforms", default="", help="comma list: " + ",".join(ALL_PLATFORMS) + " (required unless --from-clips, which takes them from the sidecars)")
    s.add_argument("--title", default="")
    s.add_argument("--description", default="")
    s.add_argument("--tags", default="", help="comma separated")
    s.add_argument("--caption", default="")
    s.add_argument("--schedule", help="ISO 8601 with timezone offset")
    s.add_argument("--visibility", default="private", choices=["private", "unlisted", "public"])
    s.add_argument("--ai-disclosure", choices=["yes", "no"], help="is realistic synthetic/AI-altered media present? (required for YouTube)")
    s.add_argument("--made-for-kids", action="store_true")
    s.add_argument("--format", default="auto", choices=["auto", "short", "long"])
    s.add_argument("--from-clips", action="store_true", help="queue the latest shorts export per clip and platform, from the sidecars (skips any that failed limits/licenses)")
    s.set_defaults(fn=cmd_queue)

    s = ps.add_parser("list", help="queue items and status")
    s.add_argument("episode")
    s.set_defaults(fn=cmd_list)
    s = ps.add_parser("show", help="one queue item in full")
    s.add_argument("episode")
    s.add_argument("id")
    s.set_defaults(fn=cmd_show)
    s = ps.add_parser("approve", help="HUMAN ONLY: record approval bound to the file hash")
    s.add_argument("episode")
    s.add_argument("id")
    s.add_argument("--by")
    s.add_argument("--confirm-sha", help="first 8+ chars of the sha256 of the reviewed file")
    s.set_defaults(fn=cmd_approve)
    s = ps.add_parser("reject", help="reject an item (voids approvals)")
    s.add_argument("episode")
    s.add_argument("id")
    s.add_argument("--by")
    s.add_argument("--reason", default="")
    s.set_defaults(fn=cmd_reject)
    s = ps.add_parser("run", help="publish an approved item. DRY-RUN unless --yes")
    s.add_argument("episode")
    s.add_argument("id")
    s.add_argument("--yes", action="store_true", help="actually send")
    s.add_argument("--dry-run", action="store_true", help="force dry-run (wins over --yes)")
    s.set_defaults(fn=cmd_run)
    s = ps.add_parser("resolve", help="settle a platform left uncertain by a crash: --posted URL or --retry")
    s.add_argument("episode")
    s.add_argument("id")
    s.add_argument("platform")
    s.add_argument("--posted", metavar="URL")
    s.add_argument("--retry", action="store_true")
    s.set_defaults(fn=cmd_resolve)
    s = ps.add_parser("auth", help="store credentials in the OS keychain")
    s.add_argument("platform", choices=["youtube", "facebook", "aggregator"])
    s.add_argument("--client-secrets")
    s.add_argument("--page-id")
    s.set_defaults(fn=cmd_auth)
    s = ps.add_parser("status", help="queue counts, uncertain items, credential readiness (no secrets shown)")
    s.add_argument("episode")
    s.set_defaults(fn=cmd_status)
