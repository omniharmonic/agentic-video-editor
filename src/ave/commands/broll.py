"""`ave broll place|apply|proposals|list|check|remove|undo`: put studio-library B-roll into an edit, explicitly and reversibly.

Mechanical only; JSON on stdout. Which shot, where, how long and whether it is allowed to cover a moment is judgment (skills/ave-broll-music,
skills/ave-studio-broll); the license and attestation it depends on are the library's (`ave studio library attest` is a human act).
`place` without `--apply` only writes a proposal. Nothing here downloads, spends or posts.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..broll import place
from ..episode import Episode


def _ep(a) -> Episode:
    ep = Episode(a.episode)
    ep.require()
    return ep


def _out(o) -> None:
    print(json.dumps(o, indent=2, ensure_ascii=False))


def _load(path: str | None) -> list[dict] | None:
    if not path:
        return None
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        raise SystemExit(f"cannot read {path}: {e}")
    return data if isinstance(data, list) else data.get("beats", [])


def _box(s: str | None) -> list[float] | None:
    if not s:
        return None
    try:
        v = [float(x) for x in s.split(",")]
    except ValueError:
        v = []
    if len(v) != 4:
        raise SystemExit("--box is x,y,w,h in canvas pixels")
    return v


def cmd_place(a) -> int:
    ep = _ep(a)
    res = place.place(ep, apply=a.apply, asset=a.asset, at=a.at, dur=a.dur, mode=a.mode, stage=a.stage, name=a.name, studio=a.studio, src_in=a.src_in,
                      audio=a.audio, gain_db=a.gain_db, duck_db=a.duck_db, ken_burns=a.ken_burns, corner=a.corner, scale=a.scale, box=_box(a.box),
                      fit=a.fit, credit=a.credit, beats=_load(a.beats), allow_hold=a.allow_hold or None, strict_pacing=a.strict_pacing,
                      reason=a.reason or "", by=a.by, layer=a.layer)
    _out(res)
    return 0


def cmd_apply(a) -> int:
    ep = _ep(a)
    _out(place.apply_proposal(ep, a.proposal, by=a.by, reason=a.reason))
    return 0


def cmd_proposals(a) -> int:
    _out({"proposals": place.list_proposals(_ep(a))})
    return 0


def cmd_list(a) -> int:
    _out(place.listing(_ep(a), stage=a.stage, name=a.name))
    return 0


def cmd_check(a) -> int:
    res = place.check(_ep(a), stage=a.stage, name=a.name, beats=_load(a.beats))
    _out(res)
    return 0 if res["ok"] else 1


def cmd_remove(a) -> int:
    _out(place.remove(_ep(a), a.id, stage=a.stage, name=a.name, by=a.by, reason=a.reason or ""))
    return 0


def cmd_undo(a) -> int:
    _out(place.undo(_ep(a), stage=a.stage, name=a.name, by=a.by))
    return 0


def _target(p) -> None:
    p.add_argument("--stage", choices=["assembly", "rough", "final", "select", "short"], help="which edit (default: the latest of final, rough, assembly)")
    p.add_argument("--name", help="named doc: a session (final --name s1) or a short's slug with --stage short")


def register(sub) -> None:
    b = sub.add_parser("broll", help="put studio-library B-roll into an edit: place (proposal; --apply to apply) / apply / list / check / remove / undo")
    bs = b.add_subparsers(dest="broll_cmd", required=True)

    p = bs.add_parser("place", help="a library video/image as a cutaway, overlay or insert; SUGGEST-ONLY unless --apply (then a NEW edit version + history entry)")
    p.add_argument("episode")
    p.add_argument("--asset", required=True, help="library asset id (a_...); it must be licensed AND human-attested in the studio library")
    p.add_argument("--at", type=float, required=True, help="output-time seconds where it starts")
    p.add_argument("--dur", type=float, required=True, help="seconds on screen")
    p.add_argument("--mode", choices=list(place.MODES), default="cutaway",
                   help="cutaway (default): the whole frame, dialogue untouched | overlay: a picture-in-picture box | insert: ripple, the cut gets longer and the dialogue holds")
    p.add_argument("--studio", help="the studio holding the asset (default: <workspace>/studio)")
    _target(p)
    p.add_argument("--in", dest="src_in", type=float, default=0.0, help="start this many seconds into a video asset (default 0)")
    p.add_argument("--audio", choices=["mute", "keep"], default="mute", help="B-roll is silent unless you say keep (its sound is mixed under the dialogue and ducked)")
    p.add_argument("--gain-db", type=float, help="level of kept sound (config/broll_place.yaml audio.gain_db)")
    p.add_argument("--duck-db", type=float, help="extra reduction while the dialogue speaks (audio.duck_db; 0 = never duck)")
    p.add_argument("--ken-burns", nargs="?", const="push_in", help="slow move on a still: a preset (push_in, pull_out, pan_left, pan_right, drift) or JSON {zoom_from, zoom_to, from, to}")
    p.add_argument("--corner", choices=["tl", "tr", "bl", "br", "center"], help="overlay mode: which corner (default br)")
    p.add_argument("--scale", type=float, help="overlay mode: box width as a fraction of the canvas width")
    p.add_argument("--box", help="overlay mode: x,y,w,h in canvas pixels (overrides --corner/--scale)")
    p.add_argument("--fit", choices=["cover", "contain", "stretch"], help="how the picture fills its box (default cover)")
    p.add_argument("--credit", choices=["auto", "always", "never"], help="on-screen attribution caption: auto = when the license requires it")
    p.add_argument("--beats", help="JSON file of emotional beats [{t0, t1, label}] B-roll must not cover (checked, advisory)")
    p.add_argument("--allow-hold", action="store_true", help="allow a placement longer than the footage: the last frame is held")
    p.add_argument("--strict-pacing", action="store_true", help="refuse when the pacing rules (hold length, coverage, beats) fail")
    p.add_argument("--layer", type=int)
    p.add_argument("--reason", help="why this shot goes here (recorded in the edit's history)")
    p.add_argument("--by", help="who is placing it (default $AVE_USER or the login name)")
    p.add_argument("--apply", action="store_true", help="apply now: a new edit version with a history entry (without it nothing in the edit changes)")
    p.set_defaults(fn=cmd_place)

    p = bs.add_parser("apply", help="apply a stored proposal as a new edit version (refuses if the edit changed since)")
    p.add_argument("episode")
    p.add_argument("proposal")
    p.add_argument("--by")
    p.add_argument("--reason")
    p.set_defaults(fn=cmd_apply)

    p = bs.add_parser("proposals", help="proposals written by `place` and whether they were applied")
    p.add_argument("episode")
    p.set_defaults(fn=cmd_proposals)

    p = bs.add_parser("list", help="the B-roll placements in the latest edit, their history and license closure")
    p.add_argument("episode")
    _target(p)
    p.set_defaults(fn=cmd_list)

    p = bs.add_parser("check", help="license closure (exit 1 when an asset is unlicensed/unattested) and pacing of the B-roll; lists the credits")
    p.add_argument("episode")
    _target(p)
    p.add_argument("--beats", help="JSON file of emotional beats [{t0, t1, label}]")
    p.set_defaults(fn=cmd_check)

    p = bs.add_parser("remove", help="drop one placement (and its credit) in a new version; an insert is reversed with `undo`")
    p.add_argument("episode")
    p.add_argument("--id", required=True, help="placement id (b1, b2, ...)")
    _target(p)
    p.add_argument("--by")
    p.add_argument("--reason")
    p.set_defaults(fn=cmd_remove)

    p = bs.add_parser("undo", help="restore the edit as it was before the latest B-roll step (as a new version; refuses if something was saved since)")
    p.add_argument("episode")
    _target(p)
    p.add_argument("--by")
    p.set_defaults(fn=cmd_undo)
