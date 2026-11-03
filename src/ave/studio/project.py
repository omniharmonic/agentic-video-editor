"""StudioProject: a workspace-level project that IS an Episode (spec 2.1.2).

The marker file is `studio.json` instead of `episode.json`, so `workspace.episodes()` ignores it, while everything that only
touches `.root/.work/.publish/.exports/.meta_path/.rel()` (the publish queue, approval, the license log, the content renderer)
runs on it unchanged.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .. import workspace
from ..episode import Episode, read_json, write_json

STUDIO_DEFAULTS: dict[str, Any] = {
    "v": 1, "kind": "studio", "speakers": {}, "tz": "UTC",
    "layout": {"preset": "flat", "raw_dirs": ["inbox"]},
    "approval": {"mode": "pin_after_review", "ttl_days": 14, "agent_retime_approved": "propose_only"},
    "dedupe_days": 90, "late_after_min": 120,
}
DIRS = ("posts", "campaigns", "accounts", "library/assets", "schedule/proposals", "schedule/state", "ledger", "policy", "recipes",
        "publish/queue", "work", "exports", "inbox")


class StudioProject(Episode):
    @property
    def meta_path(self) -> Path:
        return self.root / "studio.json"

    @property
    def posts_dir(self) -> Path:
        return self.root / "posts"

    @property
    def campaigns_dir(self) -> Path:
        return self.root / "campaigns"

    @property
    def accounts_dir(self) -> Path:
        return self.root / "accounts"

    @property
    def ledger_dir(self) -> Path:
        return self.root / "ledger"

    @property
    def library_dir(self) -> Path:
        return self.root / "library"

    @property
    def schedule_dir(self) -> Path:
        return self.root / "schedule"

    @property
    def policy_dir(self) -> Path:
        return self.root / "policy"

    @property
    def recipes_dir(self) -> Path:
        return self.root / "recipes"

    @property
    def pause_path(self) -> Path:
        return self.root / "PAUSE"

    def is_studio(self) -> bool:
        return self.meta_path.is_file()

    def require(self) -> None:
        if not self.is_studio():
            raise SystemExit(f"{self.root} is not a studio (no studio.json). Run: ave studio init {self.root}")

    def config(self) -> dict:
        """studio.json with defaults filled in (a missing key never breaks a reader)."""
        from .policy import studio_config
        return studio_config(self)

    def create(self, title: str | None = None, layout: Any = None, *, tz: str = "UTC") -> None:  # type: ignore[override]
        """Make the studio folder. Idempotent and non-destructive: an existing studio.json is left exactly as it is."""
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.meta_path.exists():
            name = title or self.root.name
            meta = {**STUDIO_DEFAULTS, "title": name, "show": name, "tz": tz,
                    "created": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
            if layout is not None:
                meta["layout"] = layout
            write_json(self.meta_path, meta)
        self._load_layout()
        for d in DIRS:
            (self.root / d).mkdir(parents=True, exist_ok=True)
        for d in (self.work, self.exports, *self.raw_dirs):
            d.mkdir(parents=True, exist_ok=True)

    def read_meta(self) -> dict:
        return read_json(self.meta_path)


def find_studio(arg: str | Path | None = None) -> StudioProject:
    """`arg` (a path, or a name inside the workspace), else `<workspace>/studio`."""
    if arg:
        return StudioProject(arg)
    ws = workspace.get()
    if ws is None:
        raise SystemExit("no studio given and no workspace set: pass a path, or `ave workspace <dir>` first")
    return StudioProject(ws / "studio")
