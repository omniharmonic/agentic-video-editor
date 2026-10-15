"""The one place that knows where the shipped data lives: `config/`, `skills/`, `motion-kit/`, `content/`.

Resolution order for the resource root:
  1. `AVE_HOME` (explicit override; must contain `config/`)
  2. a source checkout (this file sits at `<repo>/src/ave/resources.py` next to `pyproject.toml` and `config/`), which also
     covers `uv sync` / `pip install -e .` development installs
  3. packaged data inside an installed wheel (`ave/_data/`, put there by the hatch `force-include` in pyproject.toml)

Everything else asks this module (usually through `config.REPO_ROOT`, kept as the historical name) and never uses
`Path(__file__).parents[N]`.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

DATA_DIRS = ("config", "skills", "motion-kit", "content")
_PKG = Path(__file__).resolve().parent


def checkout_root() -> Path | None:
    """The repo root when running from a source checkout, else None."""
    root = _PKG.parents[1]
    if (root / "pyproject.toml").is_file() and (root / "config").is_dir() and (root / "src" / "ave").is_dir():
        return root
    return None


def packaged_root() -> Path | None:
    d = _PKG / "_data"
    return d if (d / "config").is_dir() else None


@lru_cache(maxsize=1)
def _home() -> Path:
    env = os.environ.get("AVE_HOME")
    if env:
        p = Path(env).expanduser().resolve()
        if not (p / "config").is_dir():
            raise SystemExit(f"AVE_HOME={env} has no config/ folder. Point it at an agentic-video-editor checkout or unset it.")
        return p
    found = checkout_root() or packaged_root()
    if found is None:
        raise SystemExit("Cannot find the ave config/skills/motion-kit data (neither a source checkout nor packaged data). "
                         "Set AVE_HOME to a checkout of the repository.")
    return found


def home() -> Path:
    return _home()


def is_checkout() -> bool:
    """True when resources come from a source/editable checkout (so helper files may be written next to them)."""
    return checkout_root() is not None and home() == checkout_root()


def source() -> str:
    """Human-readable answer to 'where is my data coming from' (shown by `ave doctor`)."""
    if os.environ.get("AVE_HOME"):
        return f"AVE_HOME={home()}"
    return f"source checkout {home()}" if is_checkout() else f"packaged data {home()}"


def path(*parts: str) -> Path:
    return home().joinpath(*parts)


def config_file(name: str) -> Path:
    return path("config", name)


def user_data_dir() -> Path:
    """Where downloaded third-party tools (vendor/) live when ave is installed rather than checked out."""
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base else Path.home() / ".local/share") / "ave"
