"""Recurring slot rules (`schedule/<account>.yaml`, spec 2.6). A slot is DERIVED from a rule, never stored."""

from __future__ import annotations

from pathlib import Path

import yaml

from ..studio import model, policy

DOW = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def rules_path(sp, account_id: str) -> Path:
    return sp.schedule_dir / f"{model.check_id(account_id, 'account id')}.yaml"


def account_tz(sp, account_id: str) -> str:
    return str(policy.account_config(sp, account_id).get("tz") or sp.config().get("tz") or "UTC")


def load_rules(sp, account_id: str) -> dict:
    """{"tz", "rules": [...], "overrides": [...]}. A missing file is "no rules" (any time is allowed); a malformed one is an error
    (a silently ignored rules file would post at times the operator did not choose)."""
    p = rules_path(sp, account_id)
    out = {"tz": account_tz(sp, account_id), "rules": [], "overrides": []}
    if not p.is_file():
        return out
    try:
        data = yaml.safe_load(p.read_text()) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"{p.name}: not valid YAML: {e}") from e
    data = {"tz": out["tz"], "rules": [], **data}
    problems = model.validate("schedule_rules", data)
    if problems:
        raise ValueError(f"{p.name}: " + "; ".join(problems[:5]))
    return {"tz": str(data["tz"]), "rules": list(data["rules"] or []), "overrides": list(data.get("overrides") or [])}


def skipped_dates(cfg: dict) -> set[str]:
    return {str(o["date"]) for o in cfg.get("overrides", []) if o.get("skip")}
