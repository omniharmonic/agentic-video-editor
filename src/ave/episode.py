"""Episode folder layout and shared JSON artifact helpers.

An episode is a self-describing folder. Agents resume from disk, never from conversation memory. WHERE things live is
configurable (`episode.json` -> "layout") so the tool can work inside a creator's own numbered project folder:

    flat (default; also used when "layout" is absent, so old episodes keep working)
      raw/  work/  edits/  captions/  publish/  exports/  work/gfx  work/music
    numbered (e.g. an external drive: 01_PROJECT 02_FOOTAGE 03_AUDIO 04_GFX 05_MUSIC 06_EXPORTS)
      02_FOOTAGE + 03_AUDIO are raw (read-only); generated files go in 01_PROJECT/{work,edits,captions,publish} and 06_EXPORTS

Every artifact path stored inside an artifact is RELATIVE TO THE EPISODE ROOT (e.g. `02_FOOTAGE/IMG_0002.mov`), never absolute.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

MEDIA_EXTS = {
    ".mp4", ".mov", ".mxf", ".m4v", ".mkv", ".avi", ".mts", ".m2ts",
    ".wav", ".aif", ".aiff", ".mp3", ".m4a", ".flac", ".aac", ".braw", ".r3d",
}

PRESETS: dict[str, dict[str, Any]] = {
    "flat": {"raw_dirs": ["raw"], "work": "work", "edits": "edits", "captions": "captions", "publish": "publish",
             "exports": "exports", "gfx": "work/gfx", "music": "work/music"},
    "numbered": {"raw_dirs": ["02_FOOTAGE", "03_AUDIO"], "work": "01_PROJECT/work", "edits": "01_PROJECT/edits",
                 "captions": "01_PROJECT/captions", "publish": "01_PROJECT/publish", "exports": "06_EXPORTS",
                 "gfx": "04_GFX", "music": "05_MUSIC"},
}
LAYOUT_KEYS = tuple(PRESETS["flat"])
# Folders that never hold footage: macOS metadata (AppleDouble `._x`, .DS_Store, .Spotlight-V100, .fseventsd, .Trashes)
# are all dot-prefixed; the Windows/ExFAT ones are not.
_SKIP_NAMES = {"$RECYCLE.BIN", "System Volume Information"}
PROXY_DIRNAME = "proxies"   # review proxies (`ave proxies`) live in <raw dir>/proxies: derived files, never footage


def resolve_layout(spec: Any) -> dict[str, Any]:
    """`episode.json["layout"]` -> a complete layout dict. None = legacy flat; a string or {"preset": ..} picks a preset;
    any other keys in a dict override the preset (default flat)."""
    if spec is None:
        spec = "flat"
    if isinstance(spec, str):
        spec = {"preset": spec}
    if not isinstance(spec, dict):
        raise SystemExit(f"episode.json layout must be a preset name or an object, got {spec!r}")
    name = spec.get("preset", "flat")
    if name not in PRESETS:
        raise SystemExit(f"unknown layout preset {name!r}; use one of {sorted(PRESETS)} or give explicit keys")
    out = json.loads(json.dumps(PRESETS[name]))
    for k, v in spec.items():
        if k == "preset":
            continue
        if k not in LAYOUT_KEYS:
            raise SystemExit(f"unknown layout key {k!r}; valid keys: {', '.join(LAYOUT_KEYS)}")
        out[k] = v
    if isinstance(out["raw_dirs"], str):
        out["raw_dirs"] = [out["raw_dirs"]]
    if not out["raw_dirs"]:
        raise SystemExit("layout raw_dirs must list at least one folder")
    return out


def detect_layout(root: Path) -> str:
    """For `init-episode --layout auto`: an existing 02_FOOTAGE means the creator's numbered project folder."""
    return "numbered" if (Path(root) / "02_FOOTAGE").is_dir() else "flat"


def _ignored(rel_parts: tuple[str, ...]) -> bool:
    return any(p.startswith(".") or p in _SKIP_NAMES or p == PROXY_DIRNAME for p in rel_parts)


class Episode:
    def __init__(self, root: str | Path):
        from . import workspace  # a bare name like "ep-001" resolves inside the workspace
        self.root = workspace.resolve_episode(root).resolve()
        self._load_layout()

    def _load_layout(self) -> None:
        spec = None
        if self.meta_path.exists():
            try:
                spec = json.loads(self.meta_path.read_text()).get("layout")
            except (OSError, ValueError):
                spec = None
        self.layout = resolve_layout(spec)

    def _dir(self, key: str) -> Path:
        return self.root / self.layout[key]

    @property
    def raw_dirs(self) -> list[Path]:
        return [self.root / d for d in self.layout["raw_dirs"]]

    @property
    def raw(self) -> Path:
        return self.raw_dirs[0]

    @property
    def work(self) -> Path:
        return self._dir("work")

    @property
    def edits(self) -> Path:
        return self._dir("edits")

    @property
    def captions(self) -> Path:
        return self._dir("captions")

    @property
    def publish(self) -> Path:
        return self._dir("publish")

    @property
    def exports(self) -> Path:
        return self._dir("exports")

    @property
    def gfx(self) -> Path:
        return self._dir("gfx")

    @property
    def music(self) -> Path:
        return self._dir("music")

    @property
    def meta_path(self) -> Path:
        return self.root / "episode.json"

    def rel(self, p: str | Path) -> str:
        """Path relative to the episode root, as stored in artifacts (posix separators)."""
        p = Path(p)
        return (p.relative_to(self.root) if p.is_absolute() else p).as_posix()

    def artifact(self, name: str) -> Path:
        return self.work / name

    def create(self, title: str | None = None, layout: Any = None) -> None:
        """Make the episode folder. Idempotent and non-destructive: only MISSING dirs/files are created; an existing
        episode.json (and any footage) is left exactly as it is. `layout` is only written when episode.json is new."""
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.meta_path.exists():
            meta: dict[str, Any] = {
                "title": title or self.root.name,
                "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                # Filled in during the pipeline; the agent proposes, the human confirms.
                "speakers": {},   # {"host": {"name": "...", "camera": "A", "mic": "..."}}
                "cameras": {},    # {"A": {"file": "raw/camA.mp4", "role": "wide|host|guest"}}
                "stages": {},     # {"ingest": {"done": true, "at": "..."}}
            }
            if layout is not None:
                meta["layout"] = layout
            write_json(self.meta_path, meta)
        self._load_layout()
        dirs = [*self.raw_dirs, self.work, self.exports]
        if self.layout != PRESETS["flat"]:
            dirs += [self.edits, self.gfx, self.music]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)

    def require(self) -> None:
        if not self.meta_path.exists():
            raise SystemExit(f"{self.root} is not an episode (no episode.json). Run: ave init-episode {self.root}")

    def mark_stage(self, stage: str, **info: Any) -> None:
        meta = read_json(self.meta_path)
        meta.setdefault("stages", {})[stage] = {"done": True, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **info}
        write_json(self.meta_path, meta)
        from . import progress  # the automatic trail: one session-log line per stage, even if the agent forgets `ave log`
        detail = ", ".join(f"{k}={v}" for k, v in info.items())
        progress.auto_log(self, f"stage {stage} done" + (f" ({detail[:160]})" if detail else ""), "done", stage)

    def media_files(self) -> list[Path]:
        """Every media file under every raw dir, sorted. Ignores dotfiles (AppleDouble `._x`, .DS_Store) and anything
        under dot-folders (.Spotlight-V100, .fseventsd) so ExFAT/macOS drives do not poison ingest."""
        out: list[Path] = []
        for d in self.raw_dirs:
            if not d.is_dir():
                continue
            for p in d.rglob("*"):
                if p.is_file() and p.suffix.lower() in MEDIA_EXTS and not _ignored(p.relative_to(d).parts):
                    out.append(p)
        return sorted(out)


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text())


def write_json(path: Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)  # atomic: an interrupted run never leaves a half-written artifact
