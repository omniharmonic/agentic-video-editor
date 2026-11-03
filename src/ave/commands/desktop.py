"""Optional local native studio window and macOS launcher."""
from __future__ import annotations

import json


def cmd(a) -> int:
    from .. import desktop
    if a.check:
        print(json.dumps(desktop.availability(), indent=2))
        return 0
    if a.build_launcher:
        try:
            result = desktop.build_launcher(a.build_launcher, studio_path=a.studio)
        except (OSError, ValueError) as e:
            print(json.dumps({"ok": False, "error": str(e)}))
            return 2
        print(json.dumps({"ok": True, "launcher": str(result), "portable": False,
                          "note": "Uses this Python installation. Open it yourself in Finder; Terminal retains the approval PIN."}, indent=2))
        return 0
    return desktop.launch(a.studio, fixtures=a.fixtures, by=a.by)


def register(sub) -> None:
    p = sub.add_parser("desktop", help="open the studio in a native window (human terminal required)")
    p.add_argument("studio", nargs="?", help="studio directory (default: workspace/studio)")
    p.add_argument("--fixtures", action="store_true", help="checkout sample data; no real approval or publishing")
    p.add_argument("--by", help="editor name in the history")
    p.add_argument("--check", action="store_true", help="report runtime availability without opening anything")
    p.add_argument("--build-launcher", metavar="NAME.app", help="create a macOS Finder launcher for this installation, without launching")
    p.set_defaults(fn=cmd)
