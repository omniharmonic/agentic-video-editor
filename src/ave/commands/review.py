"""`ave review ...`: the agent's side of the director's timestamped notes on a cut. They are left in the Room (flags on the Cut,
the Script or a select); `next` reads the Room's open flags first, and falls back to the older notes file (`work/review/notes.jsonl`)."""

from __future__ import annotations

import json
from pathlib import Path

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


def _room_flags(ep: Episode) -> list[dict]:
    """The Room's open flags on this episode, oldest first, shaped like a note. Empty when no app is open."""
    from ..room import cut as room_cut
    from ..room.client import waiting_flags
    out = []
    for f in waiting_flags(ep.root) or []:
        try:
            if room_cut.find_episode(Path(f["root"]), f.get("artifact")).root.resolve() != ep.root.resolve():
                continue
        except (room_cut.NotFound, OSError, ValueError):
            continue
        out.append({"id": f"flag:{f.get('id')}", "source": "room", "view": f.get("view"), "edit": f.get("version"), "t0": f.get("start"),
                    "t1": f.get("end"), "text": f.get("note"), "words": f.get("text"), "clip": f.get("clip"), "status": "open"})
    return out


def _next(a) -> None:
    ep = _ep(a)
    flags = _room_flags(ep)
    if flags:                     # answered in the Room: room_read acknowledges them, so nothing here claims or resolves one
        _out({"note": flags[0], "source": "room", "waiting": len(flags), "untrusted_text": True,
              "how": "Read it with room_read, make the change as a new version, then show it with room_offer_version."})
        return
    _out({"note": review.next_note(ep, agent=a.agent if a.claim else None), "source": "notes", "untrusted_text": True})


def _add(a) -> None:
    _out(_guard(review.add_note, _ep(a), edit=a.edit, t0=a.t0, t1=a.t1, text=a.text, kind=a.kind, by=a.by))


def _resolve(a) -> None:
    _out(_guard(review.resolve, _ep(a), a.note, by=a.agent, text=a.text, edit=a.edit))


def _picture(a) -> None:
    _out(_guard(review.set_picture, _ep(a), a.edit, a.t0, a.t1, src=a.src, by=a.agent, note=a.note))


def register(sub) -> None:
    p = sub.add_parser("review", help="the director's timestamped notes on a cut: the Room's flags first, then the notes file")
    s = p.add_subparsers(dest="review_cmd", required=True)
    c = s.add_parser("list", help="notes by status")
    c.add_argument("episode")
    c.add_argument("--status", choices=("open", "claimed", "resolved", "withdrawn", "all"), default="open")
    c.set_defaults(fn=_list)
    c = s.add_parser("next", help="the oldest open note: the Room's open flags first, else the notes file")
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
