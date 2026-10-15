"""`ave sequence`: the opening (cold open, title card, host introductions) and closing (thanks, credits, end card) of the final cut."""

from __future__ import annotations

import json

from .. import sequence
from ..episode import Episode, read_json, write_json


def _ep(a) -> Episode:
    ep = Episode(a.episode)
    ep.require()
    return ep


def _out(x) -> None:
    print(json.dumps(x, indent=2, default=str))


def _set_cold_open(ep: Episode, a) -> None:
    """--cold-open-clip / --cold-open-quote / --cold-open-range write the human's pick into episode.json (sequences.cold_open)."""
    picks = {}
    if a.cold_open_clip:
        picks["clip"] = a.cold_open_clip
    if a.cold_open_quote:
        picks["quote"] = {"text": a.cold_open_quote, "stage": a.cold_open_stage, **({"name": a.name} if a.name else {})}
    if a.cold_open_range:
        picks["range"] = {"stage": a.cold_open_stage, "o0": a.cold_open_range[0], "o1": a.cold_open_range[1], **({"name": a.name} if a.name else {})}
    if len(picks) > 1:
        raise SystemExit("give only one of --cold-open-clip / --cold-open-quote / --cold-open-range")
    if a.cold_open_trim and picks.get("clip"):
        picks["trim"] = list(a.cold_open_trim)
    if picks:
        meta = read_json(ep.meta_path)
        meta.setdefault("sequences", {})["cold_open"] = picks
        write_json(ep.meta_path, meta)


def _wrap(fn):
    try:
        return fn()
    except sequence.SequenceError as e:
        raise SystemExit(str(e))


def cmd_plan(a) -> int:
    ep = _ep(a)
    _set_cold_open(ep, a)
    pl = _wrap(lambda: sequence.plan(ep, a.name, a.policy))
    view = {k: v for k, v in pl.items() if not k.startswith("_")}
    view["file"] = ep.rel(sequence.plan_path(ep, a.name))
    _out(view)
    return 1 if pl["errors"] else 0


def cmd_build(a) -> int:
    ep = _ep(a)
    which = tuple(a.only) if a.only else sequence.WHICH
    _out(_wrap(lambda: sequence.build(ep, a.name, a.preset, a.policy, which=which, burn=a.burn, force=a.force)))
    return 0


def cmd_preview(a) -> int:
    ep = _ep(a)
    which = tuple(a.only) if a.only else sequence.WHICH
    _out(_wrap(lambda: sequence.preview(ep, a.name, which, a.policy)))
    return 0


def cmd_approve(a) -> int:
    ep = _ep(a)
    rec = _wrap(lambda: sequence.approve(ep, a.name, a.by, a.policy))
    _out({"approved": True, **rec})
    return 0


def cmd_status(a) -> int:
    ep = _ep(a)
    p = sequence.plan_path(ep, a.name)
    if not p.exists():
        _out({"planned": False, "next": f"ave sequence plan {ep.root}"})
        return 0
    pl = read_json(p)
    ap = sequence.read_approval(ep, a.name)
    _out({"planned": True, "errors": pl["errors"], "warnings": pl["warnings"], "approved": bool(ap and ap.get("hash") == pl["approval"]["hash"]),
          "needs_approval": pl["approval"]["required"], "built": {w: b["file"] for w, b in (pl.get("built") or {}).items()}})
    return 0


def register(sub) -> None:
    p = sub.add_parser("sequence", help="opening and closing sequences: plan, build, preview, approve (config/sequences.default.yaml)")
    m = p.add_subparsers(dest="sequence_cmd", required=True)

    def common(s):
        s.add_argument("episode")
        s.add_argument("--name", help="named edit / session (rough.<name>)")
        s.add_argument("--policy", help="sequences policy YAML (default: <project>/policy/sequences.yaml, ~/.config/ave/sequences.yaml, config/)")

    s = m.add_parser("plan", help="write work/sequence.json: what will be built, text with sources, licenses, warnings, items to approve")
    common(s)
    s.add_argument("--cold-open-clip", help="clip id (work/clips.json) to use as the cold open; recorded in episode.json sequences.cold_open")
    s.add_argument("--cold-open-quote", help="exact words to use as the cold open (found in the transcript of --cold-open-stage)")
    s.add_argument("--cold-open-range", nargs=2, type=float, metavar=("O0", "O1"), help="output-time range of --cold-open-stage to use")
    s.add_argument("--cold-open-stage", default="rough", help="edit stage the quote/range is cut from (default rough)")
    s.add_argument("--cold-open-trim", nargs=2, type=float, metavar=("A", "B"), help="use only seconds A..B inside the clip")
    s.set_defaults(fn=cmd_plan)

    s = m.add_parser("build", help="render the opening and closing to video+audio files at the cut's spec")
    common(s)
    s.add_argument("--preset", default="master", help="render preset (master|youtube|proxy); `ave final --preset` must match")
    s.add_argument("--only", nargs="+", choices=list(sequence.WHICH))
    s.add_argument("--burn", action="store_true", help="burn the cold open's captions into the picture")
    s.add_argument("--force", action="store_true", help="rebuild even when the plan is unchanged")
    s.set_defaults(fn=cmd_build)

    s = m.add_parser("preview", help="a 540p draft of the sequences plus a still of every item (never used by the final)")
    common(s)
    s.add_argument("--only", nargs="+", choices=list(sequence.WHICH))
    s.set_defaults(fn=cmd_preview)

    s = m.add_parser("approve", help="HUMAN ONLY: approve the current plan so `ave final` may use it (agents: ask the human to run this)")
    common(s)
    s.add_argument("--by", help="who approves (default: human)")
    s.set_defaults(fn=cmd_approve)

    s = m.add_parser("status", help="is there a plan, is it approved, what is built")
    common(s)
    s.set_defaults(fn=cmd_status)
