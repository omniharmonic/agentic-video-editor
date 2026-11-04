"""`ave ui` is retired: its clip desk, trim, notes, content review, publish queue and jobs live in the app (the Room, the Workspace
and its approval sheet). The command stays only to say where they went."""

from __future__ import annotations

POINTER = "ave ui is retired: open AVE Studio (ave desktop, or ave studio ui <studio>) and use the Room and the Workspace."


def cmd_ui(a) -> int:
    print(POINTER)
    return 0


def register(sub) -> None:
    s = sub.add_parser("ui", help="retired: prints where its screens went (the Room and the Workspace in AVE Studio)")
    s.add_argument("episode", nargs="?")
    s.add_argument("--port", type=int, default=0, help=__import__("argparse").SUPPRESS)
    s.add_argument("--open", action="store_true", help=__import__("argparse").SUPPRESS)
    s.add_argument("--by", help=__import__("argparse").SUPPRESS)
    s.set_defaults(fn=cmd_ui)
