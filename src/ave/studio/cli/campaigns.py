"""Campaigns group: new, show, list, add-post. A campaign is a planning envelope: no approval or send authority."""

from __future__ import annotations

from datetime import timedelta

from ...schedule import slots
from .. import campaigns
from ..events import AGENT
from . import add_dir, csv, guarded, out, project, studio, actor_for


@guarded
def cmd_new(a) -> int:
    s = studio(a, adapters=False)
    start, end = a.start, a.end
    if a.weeks and not (start and end):
        z = slots.ZoneInfo(s.sp.config().get("tz") or "UTC")
        d0 = s.now().astimezone(z).date()
        start, end = d0.isoformat(), (d0 + timedelta(weeks=a.weeks) - timedelta(days=1)).isoformat()
    per_week = {}
    for pair in a.per_week or []:
        k, _, v = pair.partition("=")
        if not v.isdigit():
            raise ValueError("--per-week takes account=N")
        per_week[k] = int(v)
    c = campaigns.new(s.sp, a.name, goal=a.goal or "", start=start, end=end, accounts=csv(a.accounts) or list(per_week), per_week=per_week,
                      themes=csv(a.themes), brief=a.brief or "", now=s.now())
    out({"ok": True, "campaign": c})
    return 0


@guarded
def cmd_show(a) -> int:
    sp = project(a)
    out({"ok": True, **campaigns.progress(sp, a.id)})
    return 0


@guarded
def cmd_list(a) -> int:
    sp = project(a)
    out({"ok": True, "campaigns": [{"id": c["id"], "name": c["name"], "status": c["status"], "window": c.get("window"), "posts": len(c.get("posts") or [])}
                                   for c in campaigns.list_all(sp)]})
    return 0


@guarded
def cmd_add_post(a) -> int:
    s = studio(a, adapters=False)
    c = campaigns.add_post(s.sp, a.id, a.post, actor=actor_for(False, a.by), now=s.now())
    out({"ok": True, "campaign": a.id, "posts": c["posts"]})
    return 0


def add(ss) -> None:
    c = ss.add_parser("campaign", help="campaigns: new, show, list, add-post")
    sub = c.add_subparsers(dest="campaign_cmd", required=True)
    p = sub.add_parser("new")
    p.add_argument("name")
    p.add_argument("--goal")
    p.add_argument("--start", metavar="YYYY-MM-DD")
    p.add_argument("--end", metavar="YYYY-MM-DD")
    p.add_argument("--weeks", type=int, help="a window of N weeks starting today (instead of --start/--end)")
    p.add_argument("--accounts", help="comma list")
    p.add_argument("--per-week", action="append", metavar="ACCOUNT=N", help="cadence (repeatable)")
    p.add_argument("--themes")
    p.add_argument("--brief")
    add_dir(p)
    p.set_defaults(fn=cmd_new)
    p = sub.add_parser("show", help="the campaign with its cadence against what is actually scheduled")
    p.add_argument("id")
    add_dir(p)
    p.set_defaults(fn=cmd_show)
    p = sub.add_parser("list")
    add_dir(p)
    p.set_defaults(fn=cmd_list)
    p = sub.add_parser("add-post")
    p.add_argument("id")
    p.add_argument("post")
    p.add_argument("--by")
    add_dir(p)
    p.set_defaults(fn=cmd_add_post)
