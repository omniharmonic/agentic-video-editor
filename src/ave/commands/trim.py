"""`ave trim suggest | set | show | clear`: find the real start/end of a session and record the human's decision (ROADMAP W2).

`suggest` only reports; `set` is the director's approval and a human act, and so is `clear`. `propose` records Claude's suggestion for DRAFTS only
(`by: "claude"`, `draft_only`), flagged on the board; `ave final` refuses it until the director confirms with `set`.
`ave assemble` honours the recorded trim."""

from __future__ import annotations

import json

from .. import humanpresence, trim
from ..episode import Episode, read_json


def _ep(a) -> Episode:
    ep = Episode(a.episode)
    ep.require()
    return ep


def cmd_suggest(a) -> int:
    ep = _ep(a)
    s = trim.suggest(ep, session=a.session, minutes=a.minutes)
    print(json.dumps(s, indent=2) if a.json else trim.report(s))
    return 0


def cmd_propose(a) -> int:
    ep = _ep(a)
    r = trim.propose(ep, session=a.session, minutes=a.minutes)
    out = {k: v for k, v in r.items() if k != "suggestion"}
    if r["applied"] and (ep.root / ".ave" / "project.json").is_file():
        from ..start import Board
        rec = r["trim"]
        ends = " ".join(x for x in (f"in {rec['in']:.2f}s" if rec.get("in") is not None else "",
                                    f"out {rec['out']:.2f}s" if rec.get("out") is not None else "") if x)
        out["flagged"] = Board(ep.root).decide(f"trim-{r['session']}", f"Trim for the draft ({r['session']}): {ends}",
                                               [ends, "keep the whole recording"], ends,
                                               r["reason"] + ". Used for drafts only: the final needs your `ave trim set`.")["decision"]
    out["draft_only"] = True
    out["next"] = (f"re-run `ave assemble {ep.root}` and `ave cleanup {ep.root}` for the draft; before `ave final` the director confirms it: "
                   f"`ave trim set {ep.root} {r['session']} --in S --out S`") if r["applied"] else None
    print(json.dumps(out, indent=2))
    return 0


def cmd_set(a) -> int:
    line = "ave trim set %s %s%s%s" % (a.episode, a.session, "" if a.in_s is None else " --in %s" % a.in_s, "" if a.out_s is None else " --out %s" % a.out_s)
    humanpresence.require_human("setting the trim", line)
    ep = _ep(a)
    rec = trim.confirm(ep, a.session, in_s=a.in_s, out_s=a.out_s, quote=line)   # also answers Claude's open draft-trim question
    print(json.dumps({"ok": True, "trim": rec, "next": f"re-run `ave assemble {ep.root} --session {a.session}` and then `ave cleanup {ep.root} --name {a.session}`"
                      if a.session != trim.MAIN else f"re-run `ave assemble {ep.root}` and then `ave cleanup {ep.root}`"}, indent=2))
    return 0


def cmd_show(a) -> int:
    ep = _ep(a)
    p = ep.artifact(trim.SUGGESTIONS)
    print(json.dumps({"trim": read_json(ep.meta_path).get("trim") or {}, "pending_questions": trim.pending_questions(ep),
                      "suggested": sorted(read_json(p)) if p.exists() else []}, indent=2))
    return 0


def cmd_clear(a) -> int:
    # Clearing a trim is the director's call like setting one: the cut then runs the whole recording, and a confirmed trim is gone.
    humanpresence.require_human("clearing the trim", f"ave trim clear {a.episode} {a.session}")
    print(json.dumps({"ok": True, "trim": trim.clear_trim(_ep(a), a.session)}, indent=2))
    return 0


def register(sub) -> None:
    s = sub.add_parser("trim", help="real start/end of a session: `suggest` reports, `propose` applies Claude's suggestion to drafts only, `set` records the director's in/out (human act), `show`, `clear`")
    t = s.add_subparsers(dest="trim_cmd", required=True)
    x = t.add_parser("suggest", help="scan the first and last minutes of the transcript for setup chatter, the welcome and the sign-off; print evidence and a proposed in/out")
    x.add_argument("episode")
    x.add_argument("--session", help="session id (s1, s2...); omit for an episode without sessions")
    x.add_argument("--minutes", type=float, help="minutes scanned at each end (default: trim policy scan_minutes, 6)")
    x.add_argument("--json", action="store_true")
    x.set_defaults(fn=cmd_suggest)
    x = t.add_parser("propose", help="Claude's trim for DRAFTS only: record the confident ends of `suggest` (by claude, flagged); `ave final` refuses it until the director runs `set`")
    x.add_argument("episode")
    x.add_argument("--session", help="session id (s1, s2...); omit for an episode without sessions")
    x.add_argument("--minutes", type=float, help="minutes scanned at each end (default: trim policy scan_minutes, 6)")
    x.set_defaults(fn=cmd_propose)
    x = t.add_parser("set", help="record a human-approved trim in episode.json (episode seconds); `ave assemble` honours it")
    x.add_argument("episode")
    x.add_argument("session", help=f"session id, or `{trim.MAIN}` for an episode without sessions")
    x.add_argument("--in", dest="in_s", type=float, help="episode second where the session really starts")
    x.add_argument("--out", dest="out_s", type=float, help="episode second where it really ends")
    x.set_defaults(fn=cmd_set)
    x = t.add_parser("show", help="the recorded trims and any suggestion still waiting for approval")
    x.add_argument("episode")
    x.set_defaults(fn=cmd_show)
    x = t.add_parser("clear", help="forget a recorded trim (human act)")
    x.add_argument("episode")
    x.add_argument("session")
    x.set_defaults(fn=cmd_clear)
