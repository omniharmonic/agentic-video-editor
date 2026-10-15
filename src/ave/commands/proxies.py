"""`ave proxies`: intraframe 1080p review proxies of the footage an edit uses, in a `proxies/` folder next to the footage."""

from __future__ import annotations

import json

from .. import proxies
from ..episode import Episode
from .handoff import ORDER, _pick


def cmd(a) -> int:
    ep = Episode(a.episode)
    ep.require()
    stage, version, doc = _pick(ep, a.stage, a.name)
    files = proxies.footage_used(ep, doc)
    if not files:
        raise SystemExit("The edit uses no video footage; nothing to proxy.")
    res = proxies.run(ep, files, a.codec, a.height, hw=False if a.no_hw else None, force=a.force, dry_run=a.dry_run,
                      log=lambda m: print(m, flush=True))
    out = {"ok": not res["failed"], "edit": f"{stage}{'.' + a.name if a.name else ''}.v{version}", "codec": res["codec"],
           "height": res["height"], "dry_run": a.dry_run, "estimate_gb": round(res["estimate_bytes"] / 1e9, 2),
           "free_gb": round(res["free_bytes"] / 1e9, 2), "made": res["made"], "kept": res["kept"], "failed": res["failed"],
           "to_make": [i["proxy"] for i in res["items"] if i["action"] != "keep"] if a.dry_run else None,
           "next": "ave handoff <episode> --use-proxies  (links them; renders still come from the originals)"}
    print(json.dumps(out, indent=2))
    return 0 if out["ok"] else 2


def register(sub) -> None:
    s = sub.add_parser("proxies", help="make intraframe 1080p review proxies of the footage an edit uses (never touches originals)")
    s.add_argument("episode")
    s.add_argument("--stage", choices=list(ORDER) + ["select"], help="default: latest of final, rough, assembly")
    s.add_argument("--name", help="named edit within the stage (a session's edit, e.g. s1)")
    s.add_argument("--codec", choices=sorted(proxies.PROFILES), default="prores-proxy")
    s.add_argument("--height", type=int, default=proxies.DEFAULT_HEIGHT, help="proxy height in pixels (default 1080; never upscales)")
    s.add_argument("--no-hw", action="store_true", help="software decode only (default: VideoToolbox decode when available)")
    s.add_argument("--force", action="store_true", help="rebuild proxies that already verify")
    s.add_argument("--dry-run", action="store_true", help="print the plan, the size estimate and the free-space check; write nothing")
    s.set_defaults(fn=cmd)
