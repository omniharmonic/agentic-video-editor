"""`ave trim suggest | set | show | clear`: find the real start/end of a session and record the human's decision (ROADMAP W2).

`suggest` only reports; `set` is the human's approval (or the agent acting on their explicit word). `ave assemble` honours the recorded trim."""

from __future__ import annotations

import json

from .. import trim
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


def cmd_set(a) -> int:
    ep = _ep(a)
    rec = trim.set_trim(ep, a.session, in_s=a.in_s, out_s=a.out_s)
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
    print(json.dumps({"ok": True, "trim": trim.clear_trim(_ep(a), a.session)}, indent=2))
    return 0


def register(sub) -> None:
    s = sub.add_parser("trim", help="real start/end of a session: `suggest` proposes (never applies), `set` records the human's in/out, `show`, `clear`")
    t = s.add_subparsers(dest="trim_cmd", required=True)
    x = t.add_parser("suggest", help="scan the first and last minutes of the transcript for setup chatter, the welcome and the sign-off; print evidence and a proposed in/out")
    x.add_argument("episode")
    x.add_argument("--session", help="session id (s1, s2...); omit for an episode without sessions")
    x.add_argument("--minutes", type=float, help="minutes scanned at each end (default: trim policy scan_minutes, 6)")
    x.add_argument("--json", action="store_true")
    x.set_defaults(fn=cmd_suggest)
    x = t.add_parser("set", help="record a human-approved trim in episode.json (episode seconds); `ave assemble` honours it")
    x.add_argument("episode")
    x.add_argument("session", help=f"session id, or `{trim.MAIN}` for an episode without sessions")
    x.add_argument("--in", dest="in_s", type=float, help="episode second where the session really starts")
    x.add_argument("--out", dest="out_s", type=float, help="episode second where it really ends")
    x.set_defaults(fn=cmd_set)
    x = t.add_parser("show", help="the recorded trims and any suggestion still waiting for approval")
    x.add_argument("episode")
    x.set_defaults(fn=cmd_show)
    x = t.add_parser("clear", help="forget a recorded trim")
    x.add_argument("episode")
    x.add_argument("session")
    x.set_defaults(fn=cmd_clear)
