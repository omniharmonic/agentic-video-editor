"""The workspace: ONE folder, outside this repo, that holds all of a creator's episodes.

    ~/Movies/MyShow/           <- workspace (set once)
        ep-001/  raw/ work/ edits/ exports/ captions/ publish/ episode.json

Resolution order: AVE_WORKSPACE env var, then ~/.config/ave/workspace.json. Episode arguments to `ave` may be a full path,
or just a name (`ep-001`) that is looked up inside the workspace. Nothing here is ever written inside the repo.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import resources

CONFIG = Path(os.environ.get("AVE_CONFIG_DIR", Path.home() / ".config/ave")) / "workspace.json"


def get() -> Path | None:
    env = os.environ.get("AVE_WORKSPACE")
    if env:
        return Path(env).expanduser()
    try:
        return Path(json.loads(CONFIG.read_text())["path"]).expanduser()
    except Exception:
        return None


def set_workspace(path: str | Path) -> Path:
    p = Path(path).expanduser().resolve()
    repo = resources.checkout_root() or resources.home()
    if p == repo or repo in p.parents:
        raise SystemExit(f"The workspace must be outside the repo ({repo}) so footage never gets committed.")
    p.mkdir(parents=True, exist_ok=True)
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps({"path": str(p)}, indent=2) + "\n")
    return p


def resolve_episode(arg: str | Path) -> Path:
    """A path that exists wins; otherwise a bare name is looked up in the workspace (existing, or where it WOULD be created)."""
    p = Path(arg).expanduser()
    if p.exists() or p.is_absolute() or len(p.parts) > 1:
        return p
    ws = get()
    return (ws / p) if ws else p


def episodes() -> list[Path]:
    ws = get()
    if not ws or not ws.is_dir():
        return []
    return sorted(d for d in ws.iterdir() if (d / "episode.json").exists())


def in_git_checkout(p: Path) -> bool:
    """True when `p` is inside any git checkout (a .git up the tree), e.g. this repo."""
    p = p.expanduser().resolve()
    return any((d / ".git").exists() for d in (p, *p.parents))


def refuse_repo_relative(arg: str | Path, root: Path) -> None:
    """`ave init-episode ep-001` with no workspace set would create ./ep-001 in the cwd: refuse when that cwd is inside a git repo.

    Absolute and ~ paths, an existing episode, or a bare name that resolved into the workspace are fine."""
    a = Path(arg)
    if a.is_absolute() or str(arg).startswith("~") or (root / "episode.json").exists():
        return
    ws = get()
    if ws and (root == ws.resolve() or ws.resolve() in root.parents):
        return
    if in_git_checkout(root):
        raise SystemExit(
            f"refusing to create '{arg}' at {root}: that is inside a git checkout, so footage and renders could end up committed.\n"
            "  Set a workspace once:  ave workspace ~/Movies/MyShow   then  ave init-episode ep-001\n"
            f"  or give a full path:   ave init-episode ~/Movies/MyShow/{a.name}")
