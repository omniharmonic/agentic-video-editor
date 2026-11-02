"""Ingest + recipe groups: ingest idea|blog|audio|video|episode|shorts, recipe list|show|check."""

from __future__ import annotations

from ...episode import write_json
from .. import ingest, recipes
from . import Blocked, add_dir, actor_for, csv, guarded, out, project, say, studio


@guarded
def cmd_idea(a) -> int:
    s = studio(a, adapters=False)
    post = ingest.idea(s, a.text, campaign=a.campaign, actor=actor_for(False, a.by), reason=a.reason or "", confidence=a.confidence)
    out({"ok": True, "post": {"id": post["id"], "rev": post["rev"], "stage": post["stage"], "idea": post["idea"]["text"]},
         "next": "Add copy and visuals: `ave studio render` / `post edit`, then `post variant add`."})
    return 0


@guarded
def cmd_blog(a) -> int:
    sp = project(a)
    out({"ok": True, **ingest.blog(sp, a.file)})
    return 0


@guarded
def cmd_audio(a) -> int:
    sp = project(a)
    out({"ok": True, **ingest.audio(sp, a.file, name=a.name, run=not a.no_run)})
    return 0


@guarded
def cmd_video(a) -> int:
    sp = project(a)
    out({"ok": True, **ingest.video(sp, a.file, name=a.name, run=not a.no_run)})
    return 0


@guarded
def cmd_episode(a) -> int:
    s = studio(a, adapters=False)
    res = ingest.episode(s.sp, a.path)
    write_json(s.sp.work / "survey.json", res)               # the recipe's `done_when` evidence: what was surveyed, from where
    out({"ok": True, **res, "survey": str(s.sp.work / "survey.json")})
    return 0


@guarded
def cmd_shorts(a) -> int:
    from ..ingest_shorts import shorts_ingest
    s = studio(a, adapters=False)
    res = shorts_ingest(s, a.episode, clips=csv(a.clips) or None, accounts=csv(a.accounts) or None, by=a.by, sync=a.sync, dry_run=a.dry_run,
                        actor=actor_for(False, a.by))
    made = [p for p in res["posts"] if p.get("created") or p.get("would_create") or any(v.get("status") == "created" for v in p["variants"])]
    blocked = bool(res["refused"]) or not res["posts"]
    out({"ok": not blocked, **res})
    for r in res["refused"]:
        say(f"refused {r.get('clip') or ''}: " + "; ".join(r["reasons"]))
    if not res["posts"] and not res["refused"]:
        say("nothing to ingest: " + "; ".join(x["why"] for x in res["skipped"][:3]))
    return 2 if blocked else 0 if (made or res["posts"]) else 2


def _add_ingest(ss) -> None:
    ing = ss.add_parser("ingest", help="ingest: idea, blog, audio, video, episode, shorts")
    sub = ing.add_subparsers(dest="ingest_cmd", required=True)
    p = sub.add_parser("idea", help="a one-line idea -> an idea-stage post")
    p.add_argument("text")
    p.add_argument("--campaign")
    p.add_argument("--reason")
    p.add_argument("--confidence", type=float)
    p.add_argument("--by")
    add_dir(p)
    p.set_defaults(fn=cmd_idea)
    p = sub.add_parser("blog", help="a Markdown file -> sentence-addressable passages (a URL is never fetched)")
    p.add_argument("file")
    add_dir(p)
    p.set_defaults(fn=cmd_blog)
    p = sub.add_parser("audio", help="an audio file -> a normal episode beside the studio (copied into raw/, probed)")
    p.add_argument("file")
    p.add_argument("--name", help="episode folder name (default: from the file name)")
    p.add_argument("--no-run", action="store_true", help="create the episode and copy the file; skip ffprobe/audio extraction")
    add_dir(p)
    p.set_defaults(fn=cmd_audio)
    p = sub.add_parser("video", help="a video file (or an episode folder, to finish the one-camera shim) -> an episode")
    p.add_argument("file")
    p.add_argument("--name")
    p.add_argument("--no-run", action="store_true")
    add_dir(p)
    p.set_defaults(fn=cmd_video)
    p = sub.add_parser("episode", help="what an existing episode offers a campaign (read-only survey)")
    p.add_argument("path")
    add_dir(p)
    p.set_defaults(fn=cmd_episode)
    p = sub.add_parser("shorts", help="human-approved clips' exported shorts -> studio posts (a variant per account, drafted copy, library derived_from); "
                                      "never approves; queue items only with --sync")
    p.add_argument("episode", help="the episode folder")
    p.add_argument("--clips", help="comma list of clip ids or slugs (default: every human-approved clip). Any clip that is not human-approved refuses the whole run")
    p.add_argument("--accounts", help="comma list of studio account ids (default: every account that takes a vertical short)")
    p.add_argument("--sync", action="store_true", help="also run `queue sync` for the posts (queue items + every preflight; NEVER an approval)")
    p.add_argument("--dry-run", action="store_true", help="check everything and draft the copy; create and register nothing")
    p.add_argument("--by", help="who is ingesting (default $AVE_USER or the login name)")
    add_dir(p)
    p.set_defaults(fn=cmd_shorts)


@guarded
def cmd_recipe_list(a) -> int:
    sp = project(a, must_exist=False) if getattr(a, "dir", None) else None
    try:
        sp = project(a)
    except SystemExit:
        sp = None
    allr = recipes.load_all(sp)
    out({"ok": True, "recipes": [{"name": n, "title": r["title"], "summary": " ".join(str(r["summary"]).split()),
                                  "steps": [f"{s['kind']}:{s['id']}" for s in r["steps"]], "inputs": [i["name"] for i in r.get("inputs", [])]}
                                 for n, r in sorted(allr.items())]})
    return 0


@guarded
def cmd_recipe_show(a) -> int:
    sp = project(a)
    r = recipes.load(sp, a.name)
    problems = recipes.validate(r)
    out({"ok": not problems, "recipe": {k: v for k, v in r.items() if not k.startswith("_")}, "problems": problems})
    return 0 if not problems else 2


@guarded
def cmd_recipe_check(a) -> int:
    s = studio(a, adapters=False)
    res = recipes.check(s, a.name, recipes.parse_args(a.args or []))
    blocked = bool(res["problems"]) or res["needs_human"]
    out({"ok": not blocked, **res})
    return 2 if blocked else 0


def _add_recipe(ss) -> None:
    r = ss.add_parser("recipe", help="recipes: list, show, check <name> --args k=v (what is done, what is next; never skips a human step)")
    sub = r.add_subparsers(dest="recipe_cmd", required=True)
    p = sub.add_parser("list")
    add_dir(p)
    p.set_defaults(fn=cmd_recipe_list)
    p = sub.add_parser("show")
    p.add_argument("name")
    add_dir(p)
    p.set_defaults(fn=cmd_recipe_show)
    p = sub.add_parser("check")
    p.add_argument("name")
    p.add_argument("--args", nargs="*", metavar="K=V", default=[])
    add_dir(p)
    p.set_defaults(fn=cmd_recipe_check)


def add(ss) -> None:
    _add_ingest(ss)
    _add_recipe(ss)
