"""`ave progress | log | learn | adopt`: the PROGRESS.md session protocol (see skills/ave-session-protocol)."""

from __future__ import annotations

import json

from .. import progress
from ..episode import Episode


def _ep(a) -> Episode:
    ep = Episode(a.episode)
    ep.require()
    return ep


def cmd_progress(a) -> int:
    ep = _ep(a)
    progress._read(ep)                    # creates PROGRESS.md for legacy episodes that have none
    r = progress.refresh(ep)
    print(json.dumps({**progress.summary(ep), "checklist_changed": r["changed"]}, indent=2))
    return 0


def cmd_log(a) -> int:
    print(json.dumps(progress.log(_ep(a), a.message or "", kind=a.kind, step=a.step, resolve=a.resolve), indent=2))
    return 0


def cmd_learn(a) -> int:
    ep = _ep(a)
    if a.apply:
        r = progress.apply_note(ep, a.apply, a.commit)
    elif a.list or not a.insight:
        r = {"proposed": progress.field_notes(ep, "proposed"), "applied": len(progress.field_notes(ep, "applied"))}
    else:
        r = progress.learn(ep, a.insight, skill=a.skill, change=a.change)
    print(json.dumps(r, indent=2))
    return 0


def cmd_adopt(a) -> int:
    from ..cli import cmd_init
    a.title, a.layout = None, "auto"
    return cmd_init(a)


def register(sub) -> None:
    s = sub.add_parser("progress", help="regenerate the PROGRESS.md checklist from disk truth; print a compact summary (JSON)")
    s.add_argument("episode")
    s.set_defaults(fn=cmd_progress)

    s = sub.add_parser("log", help="append to PROGRESS.md: a note, decision, question, done, or blocker (JSON)")
    s.add_argument("episode")
    s.add_argument("message", nargs="?", help="one line; say what was done/decided and why")
    s.add_argument("--kind", choices=list(progress.KINDS), default="note")
    s.add_argument("--step", help="pipeline stage this belongs to, e.g. rough")
    s.add_argument("--resolve", type=int, metavar="N", help="mark open question N answered (message = the answer)")
    s.set_defaults(fn=cmd_log)

    s = sub.add_parser("learn", help="record a field note (status proposed); --list shows proposed, --apply F3 marks one applied. Clip taste: `ave learn taste report|suggest|apply <ep>`")
    s.add_argument("episode")
    s.add_argument("insight", nargs="?")
    s.add_argument("--skill", help="skill this insight should change")
    s.add_argument("--change", help="the proposed edit to the skill/doc/tool")
    s.add_argument("--list", action="store_true")
    s.add_argument("--apply", metavar="F#", help="mark a note applied after promoting it into the repo")
    s.add_argument("--commit", help="commit that applied it (with --apply)")
    s.set_defaults(fn=cmd_learn)

    s = sub.add_parser("adopt", help="adopt an existing project folder: episode.json (layout auto), PROGRESS.md, AGENTS.md, CLAUDE.md; never overwrites")
    s.add_argument("episode")
    s.set_defaults(fn=cmd_adopt)
