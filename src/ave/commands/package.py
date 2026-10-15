"""`ave package <ep>` builds the release package; `ave package queue <ep>` queues its approved items as drafts. Neither ever sends."""

from __future__ import annotations

import json
import sys

from ..episode import Episode
from ..package import build as B
from ..package import queue as Q
from ..publish.adapters import ALL_PLATFORMS


def _out(o) -> None:
    print(json.dumps(o, indent=2, ensure_ascii=False))


def _ep(path: str) -> Episode:
    ep = Episode(path)
    ep.require()
    return ep


def _mb(n: int) -> str:
    return f"{n / (1 << 20):.1f} MB"


def cmd_package(a) -> int:
    args = list(a.args)
    if args[0] == "queue":
        if len(args) != 2:
            raise SystemExit("usage: ave package queue <episode> [--package NAME] [--platforms a,b] [--visibility private] [--ai-disclosure yes|no] [--dry-run]")
        return _queue(a, _ep(args[1]))
    if len(args) != 1:
        raise SystemExit("usage: ave package <episode> [--date D] [--include a,b] [--dry-run]   |   ave package queue <episode>")
    ep = _ep(args[0])
    inc = B.parse_include(a.include)

    def announce(r):
        v = r["volume"]
        print(f"package size {_mb(r['totals']['bytes'])}, free on the volume {_mb(v['free_bytes'])} (reserve {_mb(v['reserve_bytes'])})"
              + ("" if v["fits"] else ": NOT everything fits, large files will be referenced by path, not copied"), file=sys.stderr)

    rep = B.build(ep, date=a.date, include=inc, audio_fmt=a.audio, dry_run=a.dry_run, write_status=a.write_status,
                  new_version=a.new_version, announce=announce)
    _out(rep)
    for c in rep["checklist"]:
        if c["state"] in ("missing", "warn", "draft"):
            print(f"  [{c['state']}] {c['label']}: {c['detail']}", file=sys.stderr)
    if a.dry_run:
        print("DRY RUN: nothing was written.", file=sys.stderr)
    return 2 if (a.strict and not rep["complete"]) else 0


def _queue(a, ep: Episode) -> int:
    plats = None
    if a.platforms:
        plats = [p.strip().lower() for p in a.platforms.split(",") if p.strip()]
        bad = [p for p in plats if p not in ALL_PLATFORMS]
        if bad:
            raise SystemExit(f"unknown platform(s) {bad}; choose from {', '.join(ALL_PLATFORMS)}")
    ai = None if a.ai_disclosure is None else a.ai_disclosure == "yes"
    rep = Q.queue_package(ep, package=a.package, platforms=plats, visibility=a.visibility, ai_disclosure=ai, dry_run=a.dry_run)
    _out(rep)
    return 2 if rep["refused"] else 0


def register(sub) -> None:
    p = sub.add_parser("package", help="release package: final video, captions, podcast audio, thumbnails, shorts, images, post copy, manifest, index.html "
                                       "(`package queue <ep>` queues the approved items as drafts; nothing is ever sent)")
    p.add_argument("args", nargs="+", metavar="episode | queue episode")
    p.add_argument("--date", help="folder date, YYYY-MM-DD (default today)")
    p.add_argument("--include", help="comma list of parts: video,captions,audio,thumbnails,shorts,images,copy,drafts (default all)")
    p.add_argument("--audio", choices=["mp3", "aac", "both"], help="podcast file format (default from config/package.default.yaml)")
    p.add_argument("--dry-run", action="store_true", help="plan only: report sizes, free space and the checklist; write nothing")
    p.add_argument("--write-status", action="store_true", help="with --dry-run: also write work/package/status.json for the UI")
    p.add_argument("--new-version", action="store_true", help="build a new -vN folder even when nothing changed since the last one")
    p.add_argument("--strict", action="store_true", help="exit 2 when a required piece is missing")
    p.add_argument("--package", help="queue: package folder name or path (default the newest)")
    p.add_argument("--platforms", help="queue: limit to these publish platforms, comma list")
    p.add_argument("--visibility", default="private", choices=["private", "unlisted", "public"], help="queue: default private")
    p.add_argument("--ai-disclosure", choices=["yes", "no"], help="queue: is realistic synthetic/AI-altered media present? (YouTube requires an answer; left blank the item stays a draft)")
    p.set_defaults(fn=cmd_package)
