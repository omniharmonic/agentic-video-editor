"""`ave shorts build|preview|export|list`: vertical shorts from approved clips."""

from __future__ import annotations

import json

from .. import opening, shorts
from ..episode import Episode


def _ep(a) -> Episode:
    ep = Episode(a.episode)
    ep.require()
    return ep


def _out(o) -> None:
    print(json.dumps(o, indent=2, ensure_ascii=False))


def _clips(a, ep):
    return shorts.select_clips(ep, a.clip, a.all_approved or getattr(a, "all", False), name=a.name,
                               allow_unapproved=getattr(a, "allow_unapproved", False))


def cmd_build(a) -> int:
    ep = _ep(a)
    cfg = shorts.load_config(ep)
    if a.cutaways:
        cfg["build"]["cutaways"] = a.cutaways
    res = [shorts.build_short(ep, c, cfg, layout=a.layout, force=a.force, allow_incomplete=a.allow_incomplete,
                              opening_mode=a.opening, title=a.title) for c in _clips(a, ep)]
    _out(res)
    return 0


def cmd_preview(a) -> int:
    _out(shorts.preview(_ep(a), a.slug, a.t, a.out))
    return 0


def cmd_export(a) -> int:
    ep = _ep(a)
    cfg = shorts.load_config(ep)
    plats = [p.strip() for p in a.platforms.split(",") if p.strip()] if a.platforms else list(cfg["platforms"])
    res = []
    for c in _clips(a, ep):
        try:
            res.append(shorts.export_short(ep, c, plats, cfg))
        except (FileExistsError, ValueError) as e:
            raise SystemExit(f"export failed for {c['slug']}: {e}") from e
    _out(res)
    bad = [x for r in res for x in r["exports"] if x.get("problems")]
    return 2 if bad else 0


def cmd_list(a) -> int:
    _out(shorts.list_shorts(_ep(a)))
    return 0


def register(sub) -> None:
    s = sub.add_parser("shorts", help="vertical shorts: build / preview / export / list")
    ss = s.add_subparsers(dest="shorts_cmd", required=True)

    def pick(p):
        p.add_argument("--clip", help="clip slug or id")
        p.add_argument("--all-approved", action="store_true", help="every approved clip")
        p.add_argument("--allow-unapproved", action="store_true",
                       help="TRIAL: build a proposed (not yet approved) clip named with --clip, only when the human said to; "
                            "exports go to exports/shorts_drafts/ and are never publishable")
        p.add_argument("--name", metavar="SESSION", help="only this session's clips (clips of every session are found without it; "
                                                         "each short is cut from the select of the edit its clip was picked from)")

    b = ss.add_parser("build", help="clip select -> short EditDoc (1080x1920, face-centred crops, karaoke captions)")
    b.add_argument("episode")
    pick(b)
    b.add_argument("--layout", default="crop", choices=["crop", "stack", "audiogram"], help="crop (default): one speaker at a time, cutting on speaker change; stack: both speakers at once, one above the other "
                                                                            "(1080x960 each), for crosstalk-heavy clips")
    b.add_argument("--cutaways", choices=["keep", "speaker"],
                   help="keep = reaction cutaways from the rough cut stay (default, config build.cutaways); speaker = the talker stays on screen")
    b.add_argument("--opening", choices=list(opening.MODES),
                   help="how the short opens (default: config/shorts.yaml opening.mode, title_overlay): none | title_card (a ~1.3 s title screen, then the clip) | "
                        "title_overlay (the title in the lower area over the first ~2 s, no extra time). Never the quote.")
    b.add_argument("--title", help="the opening's title text (a plain topic, not the speaker's words); default work/shorts/titles.json, then the clip title")
    b.add_argument("--allow-incomplete", action="store_true",
                   help="OVERRIDE the complete-statement guarantee: cut a clip that starts or ends mid-thought anyway. Recorded in the sidecar and PROGRESS.md; "
                        "only when the human accepts it")
    b.add_argument("--force", action="store_true", help="new version even if nothing changed")
    b.set_defaults(fn=cmd_build)

    p = ss.add_parser("preview", help="PNG with the crop, face box and caption safe zone drawn, to look at")
    p.add_argument("episode")
    p.add_argument("slug")
    p.add_argument("--t", type=float, help="output seconds (default: middle)")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_preview)

    x = ss.add_parser("export", help="render per-platform MP4s + publish sidecars into exports/shorts/")
    x.add_argument("episode")
    pick(x)
    x.add_argument("--all", action="store_true", help="alias of --all-approved")
    x.add_argument("--platforms", help="comma list: youtube,reels,facebook,x (default all)")
    x.set_defaults(fn=cmd_export)

    l = ss.add_parser("list", help="shorts built and exported")
    l.add_argument("episode")
    l.set_defaults(fn=cmd_list)
