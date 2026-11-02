"""`ave review ...`: the agent's side of timestamped cut notes left on the Review page of `ave ui`."""

from __future__ import annotations

import json

from .. import review
from ..episode import Episode


def _ep(a) -> Episode:
    ep = Episode(a.episode)
    ep.require()
    return ep


def _out(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def _guard(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except ValueError as e:
        raise SystemExit(str(e))


def _list(a) -> None:
    _out({"notes": review.notes(_ep(a), a.status)})


def _next(a) -> None:
    _out({"note": review.next_note(_ep(a), agent=a.agent if a.claim else None), "untrusted_text": True})


def _add(a) -> None:
    _out(_guard(review.add_note, _ep(a), edit=a.edit, t0=a.t0, t1=a.t1, text=a.text, kind=a.kind, by=a.by))


def _resolve(a) -> None:
    _out(_guard(review.resolve, _ep(a), a.note, by=a.agent, text=a.text, edit=a.edit))


def _picture(a) -> None:
    _out(_guard(review.set_picture, _ep(a), a.edit, a.t0, a.t1, src=a.src, by=a.agent, note=a.note))


def register(sub) -> None:
    p = sub.add_parser("review", help="timestamped cut notes from the Review page: list, claim, resolve")
    s = p.add_subparsers(dest="review_cmd", required=True)
    c = s.add_parser("list", help="notes by status")
    c.add_argument("episode")
    c.add_argument("--status", choices=("open", "claimed", "resolved", "withdrawn", "all"), default="open")
    c.set_defaults(fn=_list)
    c = s.add_parser("next", help="the oldest open note")
    c.add_argument("episode")
    c.add_argument("--claim", action="store_true", help="hold it for 30 minutes")
    c.add_argument("--agent", default="agent")
    c.set_defaults(fn=_next)
    c = s.add_parser("add", help="add a note from the CLI")
    c.add_argument("episode")
    c.add_argument("--edit", required=True, help="edit name, such as rough.v3")
    c.add_argument("--t0", type=float, required=True)
    c.add_argument("--t1", type=float)
    c.add_argument("--kind", choices=review.KINDS, default="other")
    c.add_argument("--by", default="agent")
    c.add_argument("text")
    c.set_defaults(fn=_add)
    c = s.add_parser("picture", help="change the picture in one range and save a new edit version; words and timing stay exact")
    c.add_argument("episode")
    c.add_argument("--edit", required=True)
    c.add_argument("--t0", type=float, required=True)
    c.add_argument("--t1", type=float, required=True)
    c.add_argument("--src", help="camera to show; omit to hold the shot before the range")
    c.add_argument("--note", help="the note this answers")
    c.add_argument("--agent", default="agent")
    c.set_defaults(fn=_picture)
    c = s.add_parser("resolve", help="close a note after applying it")
    c.add_argument("episode")
    c.add_argument("note")
    c.add_argument("--edit", help="the new edit version that answers it")
    c.add_argument("--agent", default="agent")
    c.add_argument("--text", default="", help="what you changed")
    c.set_defaults(fn=_resolve)
