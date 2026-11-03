"""Rate limits and counters (spec 6.6). State lives in `schedule/state/<account>.json`:

    {"posts": ["2026-11-02T16:00:05Z", ...],      our own successful posts (rolling 24 h window; older entries are pruned)
     "deferred_until": "…Z" | null, "deferred_reason": str | null}      set after a 429 / quota answer

`check` returns DEFERRALS, never blockers: a deferred slot posts later in the same tick run (if still inside `late_after_min`) or becomes
`missed`. Limits come from the account (`limits: {max_per_day, min_gap_min}`, studio default max_per_day 2) and the platform's `rate:` block in
config/platforms.yaml (`api_posts_per_24h`, `max_posts_per_day`; every number there is unverified).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..episode import read_json, write_json
from ..studio import model, policy
from ..studio.clock import iso_utc, parse_ts

DEFAULT_MAX_PER_DAY = 2
WINDOW = timedelta(hours=24)


def state_path(sp, account_id: str):
    return sp.schedule_dir / "state" / f"{model.check_id(account_id, 'account id')}.json"


def load_state(sp, account_id: str) -> dict:
    try:
        data = read_json(state_path(sp, account_id))
    except (OSError, ValueError):
        data = {}
    return {"posts": list(data.get("posts") or []), "deferred_until": data.get("deferred_until"),
            "deferred_reason": data.get("deferred_reason")}


def save_state(sp, account_id: str, st: dict) -> None:
    write_json(state_path(sp, account_id), st)


def _times(st: dict) -> list[datetime]:
    out = []
    for s in st["posts"]:
        try:
            out.append(parse_ts(s))
        except ValueError:
            continue
    return sorted(out)


def record_success(sp, account_id: str, at: datetime) -> None:
    st = load_state(sp, account_id)
    keep = [t for t in _times(st) if at - t < 2 * WINDOW]
    st["posts"] = [iso_utc(t) for t in [*keep, at]]
    save_state(sp, account_id, st)


def defer(sp, account_id: str, until: datetime, reason: str) -> None:
    """A 429 / quota answer: nothing is sent for this account before `until` (a server-reported reset wins over our own counters)."""
    st = load_state(sp, account_id)
    st["deferred_until"], st["deferred_reason"] = iso_utc(until), reason
    save_state(sp, account_id, st)


def check(sp, account_id: str, platform: str | None, now: datetime, platforms_cfg: dict | None = None) -> list[tuple[str, datetime]]:
    """[(reason, retry_not_before)] for everything that defers a send NOW (empty = clear to send)."""
    cfg = policy.account_config(sp, account_id)
    lim = cfg.get("limits") or {}
    st = load_state(sp, account_id)
    times = _times(st)
    out: list[tuple[str, datetime]] = []
    if st.get("deferred_until"):
        try:
            until = parse_ts(st["deferred_until"])
            if until > now:
                out.append((f"deferred after {st.get('deferred_reason') or 'a quota answer'}", until))
        except ValueError:
            pass
    gap = int(lim.get("min_gap_min") or 0)
    if gap and times:
        nxt = times[-1] + timedelta(minutes=gap)
        if nxt > now:
            out.append((f"minimum gap of {gap} min since the last post", nxt))
    day = [t for t in times if now - t < WINDOW]
    caps = [("max_per_day", lim.get("max_per_day", DEFAULT_MAX_PER_DAY))]
    rate = ((platforms_cfg or {}).get(platform or "") or {}).get("rate") or {}
    for key in ("api_posts_per_24h", "max_posts_per_day"):
        caps.append((f"{platform} {key}", rate.get(key)))
    for label, cap in caps:
        if isinstance(cap, int) and not isinstance(cap, bool) and cap >= 0 and len(day) >= cap:
            reset = (day[len(day) - cap] + WINDOW) if cap else now + WINDOW
            out.append((f"{label} reached ({len(day)}/{cap} in 24 h)", reset))
    return out
