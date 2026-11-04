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
import time
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
    return {"path": str(target), "status": "installed"}       # the app strips PATH, so it cannot tell what the shell sees


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
    folder = (home or Path.home()) / SCRIPTS
    folder.mkdir(parents=True, exist_ok=True)      # Resolve only makes this tree once a script exists
    started = time.time() - 2
    argv = sys.argv
    sys.argv = [str(script)]
    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as e:
        if e.code not in (None, 0):
            return {"status": f"failed: installer exited {e.code}"}
    finally:
        sys.argv = argv
    launcher = folder / "resolve_bridge.py"     # the upstream installer treats a failed folder as a skip, so check the menu entry itself
    if not launcher.exists() or launcher.stat().st_mtime < started:
        return {"status": "failed: the resolve_bridge script was not written"}
    return {"status": "installed", "next": "Restart Resolve, then choose Workspace > Scripts > resolve_bridge"}


def run(home: Path | None = None, executable: Path | None = None, data: Path | None = None) -> int:
    home = home or Path.home()
    executable = executable or Path(sys.executable).resolve()
    data = data or Path(sys._MEIPASS)
    report = {"ave": install_shim(home, executable), "skills": link_skills(home, data / "ave/_data/skills"),
              "resolve_bridge": install_bridge(data / "resolve-mcp/scripts/install_resolve_bridge.py", home)}
    print(json.dumps(report, indent=2))
    return 0 if "failed" not in report["resolve_bridge"]["status"] else 1


def refresh_after_update(version: str, home: Path | None = None, executable: Path | None = None, data: Path | None = None,
                         state: Path | None = None) -> dict | None:
    """On the first launch of a new version, copy the support files out of the new bundle again: the Resolve bridge, the
    `ave` shim and the skill links. Only what was installed before is refreshed (nothing new is installed without being
    asked), and the version is recorded so this runs once. None when this version already refreshed."""
    from . import updates
    if not updates.first_launch_of(version, state):
        return None
    home = home or Path.home()
    executable = executable or Path(sys.executable).resolve()
    data = data or Path(sys._MEIPASS)
    report: dict = {"version": version}
    shim = home / ".local/bin/ave"
    if shim.is_file() and not shim.is_symlink() and MARKER in shim.read_text(errors="replace"):
        report["ave"] = install_shim(home, executable)
    links = home / ".claude/skills"
    skills = data / "ave/_data/skills"
    if skills.is_dir() and links.is_dir() and any(p.is_symlink() and "AVE Studio" in os.readlink(p) for p in links.iterdir()):
        report["skills"] = link_skills(home, skills)
    if (home / SCRIPTS / "resolve_bridge.py").exists():
        report["resolve_bridge"] = install_bridge(data / "resolve-mcp/scripts/install_resolve_bridge.py", home)
    if "failed" not in str(report.get("resolve_bridge", {}).get("status", "")):
        updates.record_launch(version, state)      # a failed bridge refresh tries again next launch
    return report
