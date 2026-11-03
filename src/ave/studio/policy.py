"""Studio policy: studio.json defaults, per-studio overrides of `config/studio/*.yaml`, approval TTL (spec 2.1.1, 2.5, 6.3)."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from .. import resources
from ..episode import read_json
from .clock import parse_ts

DEFAULT_TTL_DAYS = 14
DEFAULT_LATE_AFTER_MIN = 120
DEFAULT_DEDUPE_DAYS = 90


def deep_merge(base: Any, over: Any) -> Any:
    """`over` laid on `base`: dicts merge key by key, everything else (lists included) is replaced."""
    if isinstance(base, dict) and isinstance(over, dict):
        out = copy.deepcopy(base)
        for k, v in over.items():
            out[k] = deep_merge(base[k], v) if k in base else copy.deepcopy(v)
        return out
    return copy.deepcopy(over)


def studio_config(sp) -> dict:
    """studio.json deep-merged over the defaults (so a hand-trimmed file never breaks a reader)."""
    from .project import STUDIO_DEFAULTS
    try:
        meta = read_json(sp.meta_path)
    except (OSError, ValueError):
        meta = {}
    return deep_merge(STUDIO_DEFAULTS, meta)


def load_policy(sp, name: str) -> dict:
    """`config/studio/<name>.yaml` (shipped default; may be absent) deep-merged with `<studio>/policy/<name>.yaml` (per-studio)."""
    def read(p: Path) -> dict:
        try:
            data = yaml.safe_load(p.read_text())
        except (OSError, yaml.YAMLError):
            return {}
        return data if isinstance(data, dict) else {}
    shipped = read(resources.home() / "config" / "studio" / f"{name}.yaml")
    return deep_merge(shipped, read(sp.policy_dir / f"{name}.yaml"))


def account_config(sp, account_id: str) -> dict:
    """The parsed accounts/<id>.yaml, or {} (the account loader proper is WS2's; this is the tolerant read policy needs)."""
    try:
        data = yaml.safe_load((sp.accounts_dir / f"{account_id}.yaml").read_text())
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def approval_ttl_days(sp, account_id: str | None = None) -> int:
    """Studio default, which an account may only TIGHTEN (spec 2.5)."""
    ttl = int((studio_config(sp).get("approval") or {}).get("ttl_days") or DEFAULT_TTL_DAYS)
    if account_id:
        own = (account_config(sp, account_id).get("approval") or {}).get("ttl_days")
        if isinstance(own, int) and 0 < own < ttl:
            ttl = own
    return ttl


def late_after_min(sp) -> int:
    v = studio_config(sp).get("late_after_min")
    return int(v) if isinstance(v, int) else DEFAULT_LATE_AFTER_MIN


def dedupe_days(sp) -> int:
    v = studio_config(sp).get("dedupe_days")
    return int(v) if isinstance(v, int) else DEFAULT_DEDUPE_DAYS


def agent_retime_approved(sp) -> str:
    return str((studio_config(sp).get("approval") or {}).get("agent_retime_approved") or "propose_only")


def approved_at(item: dict | None) -> datetime | None:
    """When the item's LATEST approval was recorded, or None."""
    approvals = (item or {}).get("approvals") or []
    if not approvals:
        return None
    try:
        return parse_ts(approvals[-1].get("at", ""))
    except ValueError:
        return None


def approval_expires(item: dict | None, ttl_days: int) -> datetime | None:
    at = approved_at(item)
    return at + timedelta(days=ttl_days) if at else None


def approval_expired(item: dict | None, now: datetime, ttl_days: int) -> bool:
    """True when the latest approval is older than `ttl_days`. No approval = not "expired" (it is simply absent)."""
    exp = approval_expires(item, ttl_days)
    return exp is not None and now >= exp
