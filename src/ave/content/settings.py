"""`config/content.yaml` plus the per-install override and the feature flag.

WHY a flag: the feature is opt-in per install. The repo ships `enabled: false`; one install turns it on with
`~/.config/ave/content.yaml` (`enabled: true`) or AVE_CONTENT_ENABLED=1, so nobody's checkout changes behaviour.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from .. import config

REPO_CONFIG = config.REPO_ROOT / "config" / "content.yaml"


def override_path() -> Path:
    return Path(os.environ.get("AVE_CONFIG_DIR", Path.home() / ".config/ave")) / "content.yaml"


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load() -> dict:
    """Repo defaults, then the install override, then the env flag."""
    cfg = yaml.safe_load(Path(os.environ.get("AVE_CONTENT_CONFIG", REPO_CONFIG)).read_text()) or {}
    ov = override_path()
    if ov.is_file():
        cfg = _merge(cfg, yaml.safe_load(ov.read_text()) or {})
    env = os.environ.get("AVE_CONTENT_ENABLED")
    if env is not None:
        cfg["enabled"] = env.strip().lower() in ("1", "true", "yes", "on")
    return cfg


def enabled() -> bool:
    return bool(load().get("enabled"))


OFF_MESSAGE = ("`ave content` is switched off on this install. It is opt-in: set `enabled: true` in {ov} "
               "(or export AVE_CONTENT_ENABLED=1). The repo default in config/content.yaml stays false. "
               "Docs: docs/content.md. Nothing else in the pipeline depends on it.")


def require_enabled() -> dict:
    cfg = load()
    if not cfg.get("enabled"):
        raise SystemExit(OFF_MESSAGE.format(ov=override_path()))
    return cfg


def format_size(cfg: dict, fmt: str) -> tuple[int, int]:
    f = cfg["formats"][fmt]
    return int(f["w"]), int(f["h"])
