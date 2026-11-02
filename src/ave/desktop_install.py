"""`AVE Studio --install-cli`: make the standalone app reachable as `ave` for its owner and their agent.

Run only on request. It writes a small shim, links the agent skills and installs the Resolve bridge, and it never
replaces a file it did not write. Human gates are unchanged: the shim runs the same `ave` CLI, in the same terminal.
"""
from __future__ import annotations

import json
import os
import runpy
import shlex
import sys
from pathlib import Path

MARKER = "# ave-studio-shim"
SCRIPTS = "Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"


def shim_text(executable: Path) -> str:
    return f'#!/bin/sh\n{MARKER}\nexec {shlex.quote(str(executable))} --cli "$@"\n'


def install_shim(home: Path, executable: Path) -> dict:
    target = home / ".local/bin/ave"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or (target.exists() and MARKER not in target.read_text(errors="replace")):
        return {"path": str(target), "status": "skipped: an ave command that is not ours is already there"}
    target.write_text(shim_text(executable))
    target.chmod(0o755)
    on_path = str(target.parent) in os.environ.get("PATH", "").split(":")
    return {"path": str(target), "status": "installed", "on_path": on_path}


def link_skills(home: Path, skills: Path) -> dict:
    target = home / ".claude/skills"
    target.mkdir(parents=True, exist_ok=True)
    linked, skipped = [], []
    for skill in sorted(p for p in skills.iterdir() if p.is_dir()):
        link = target / skill.name
        if link.is_symlink() and "AVE Studio" in os.readlink(link):     # ours, perhaps from a moved app: refresh it
            link.unlink()
        elif link.exists() or link.is_symlink():
            skipped.append(skill.name)
            continue
        link.symlink_to(skill)
        linked.append(skill.name)
    return {"path": str(target), "linked": len(linked), "skipped": skipped}


def install_bridge(script: Path, home: Path | None = None, app: Path = Path("/Applications/DaVinci Resolve")) -> dict:
    """Blackmagic's Scripts folder is where free Resolve finds the bridge; the upstream installer knows every variant."""
    if not app.exists():
        return {"status": "skipped: DaVinci Resolve is not installed yet; install it, then run this again"}
    (home or Path.home()).joinpath(SCRIPTS).mkdir(parents=True, exist_ok=True)    # Resolve only makes this tree once a script exists
    argv = sys.argv
    sys.argv = [str(script)]
    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as e:
        if e.code not in (None, 0):
            return {"status": f"failed: installer exited {e.code}"}
    finally:
        sys.argv = argv
    return {"status": "installed", "next": "Restart Resolve, then choose Workspace > Scripts > resolve_bridge"}


def run(home: Path | None = None, executable: Path | None = None, data: Path | None = None) -> int:
    home = home or Path.home()
    executable = executable or Path(sys.executable).resolve()
    data = data or Path(sys._MEIPASS)
    report = {"ave": install_shim(home, executable), "skills": link_skills(home, data / "ave/_data/skills"),
              "resolve_bridge": install_bridge(data / "resolve-mcp/scripts/install_resolve_bridge.py", home)}
    print(json.dumps(report, indent=2))
    return 0 if "failed" not in report["resolve_bridge"]["status"] else 1
