"""Where Playwright keeps its browsers, per platform (several tests repoint HOME, so they resolve it once from the real home)."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def playwright_cache_dir(home: Path | None = None, platform: str | None = None, env: dict | None = None) -> Path:
    """Playwright's own default: PLAYWRIGHT_BROWSERS_PATH, else ~/Library/Caches/ms-playwright on macOS, ~/.cache/ms-playwright elsewhere
    (XDG_CACHE_HOME is honoured on Linux, as Playwright does)."""
    env = os.environ if env is None else env
    if env.get("PLAYWRIGHT_BROWSERS_PATH") and env["PLAYWRIGHT_BROWSERS_PATH"] != "0":
        return Path(env["PLAYWRIGHT_BROWSERS_PATH"])
    home = home or Path.home()
    platform = platform or sys.platform
    if platform == "darwin":
        return home / "Library/Caches/ms-playwright"
    if platform.startswith("win"):
        return Path(env.get("LOCALAPPDATA") or home / "AppData/Local") / "ms-playwright"
    return Path(env.get("XDG_CACHE_HOME") or home / ".cache") / "ms-playwright"
