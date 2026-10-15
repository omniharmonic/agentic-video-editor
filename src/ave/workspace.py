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
