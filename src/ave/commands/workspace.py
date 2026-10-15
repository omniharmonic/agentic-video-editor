"""`ave workspace` and `ave episodes`: where episodes live, and what state each is in."""

from __future__ import annotations

import json

from .. import state, workspace
from ..episode import Episode


def cmd_workspace(a) -> int:
    if a.path:
        p = workspace.set_workspace(a.path)
        print(json.dumps({"workspace": str(p), "note": "episodes can now be named, e.g. `ave init-episode ep-001`, `ave resume ep-001`"}, indent=2))
    else:
        w = workspace.get()
        print(json.dumps({"workspace": str(w) if w else None,
                          "hint": None if w else "set one: ave workspace ~/Movies/MyShow  (outside the repo)"}, indent=2))
    return 0


def cmd_episodes(a) -> int:
    rows = []
    for d in workspace.episodes():
        ep = Episode(d)
        try:
            st = state.status(ep)
            nxt = st["next"]
            rows.append({"episode": d.name, "next": nxt["title"] if nxt else "nothing pending",
                         "command": nxt["command"] if nxt else None, "edits": {k: v[-1] for k, v in st["edits"].items()}})
        except Exception as e:  # a half-made folder must not hide the others
            rows.append({"episode": d.name, "error": str(e)})
    print(json.dumps({"workspace": str(workspace.get()) if workspace.get() else None, "episodes": rows}, indent=2))
    return 0


def register(sub) -> None:
    s = sub.add_parser("workspace", help="show or set the folder (outside the repo) that holds all episodes")
    s.add_argument("path", nargs="?")
    s.set_defaults(fn=cmd_workspace)
    s = sub.add_parser("episodes", help="list the workspace's episodes and what each needs next")
    s.set_defaults(fn=cmd_episodes)
