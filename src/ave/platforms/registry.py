"""Which adapter serves which platform, and how an account gets its own bound adapter (spec 4.1, 11 WS2).

    adapter_for(account, **kw)            -> a Publisher bound to ONE account (its secrets, its live switch)
    publishers_for_item(sp, item, **kw)   -> {platform: adapter} for `queue.run(publishers=...)` (one queue item = one platform = one account)
    get_publisher(name, **kw)             -> an UNBOUND adapter (always dry-run): what `ave.publish.adapters.get_publisher` falls through to
    register(studio)                      -> plugs this package into the `Studio` facade

`mode: aggregator` accounts, and the existing YouTube adapter, are wrapped in `Gated` so that THEIR `ready_problems()` also refuses
when the account has no valid live attestation.
"""

from __future__ import annotations

import importlib
from typing import Any

from ..publish import safety
from ..publish.adapters.base import Publisher
from .accounts import Account, AccountError, list_accounts, load_account

# platform -> (module, class)
ADAPTERS: dict[str, tuple[str, str]] = {
    "bluesky": ("bluesky", "BlueskyAdapter"),
    "facebook": ("facebook_page", "FacebookPageAdapter"),
    "instagram": ("instagram", "InstagramAdapter"),
    "threads": ("threads", "ThreadsAdapter"),
    "linkedin": ("linkedin", "LinkedInAdapter"),
    "x": ("x", "XAdapter"),
    "pinterest": ("pinterest", "PinterestAdapter"),
    "tiktok": ("tiktok", "TikTokAdapter"),
    "manual": ("manual", "ManualAdapter"),
}
# platforms whose adapter is the repo's existing one, bound to an account through `secret_ns`
EXISTING = ("youtube",)


def adapter_class(platform: str):
    mod, cls = ADAPTERS[platform]
    return getattr(importlib.import_module(f"{__package__}.{mod}"), cls)


def platforms() -> tuple[str, ...]:
    return tuple(ADAPTERS) + EXISTING


class Gated:
    """Wraps an existing `Publisher` so a studio account's live switch gates it too. Everything else passes through."""

    def __init__(self, inner: Publisher, account: Account):
        self._inner, self.account = inner, account

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def ready_problems(self) -> list[str]:
        from .base import AccountBound
        out = list(self._inner.ready_problems())
        out += AccountBound.live_problems(self)            # uses self.account only
        return out

    @property
    def platform(self): return self.account.platform

    @property
    def capabilities(self): return frozenset(delivery_contract(self)['shapes'])

    def preflight(self, item: dict, video):
        from .base import AccountBound
        out = list(self._inner.preflight(item, video)) + AccountBound.bound_problems(self,item)
        if item.get("account") and item["account"] != self.account.id:
            out.append(f"{self.account.platform}: the item is for account {item['account']!r} but this adapter is bound to {self.account.id!r}")
        return out

    def publish(self, item, video, progress):
        from ..publish.adapters.base import PublishError
        problems=self.preflight(item,video)
        if problems: raise PublishError('; '.join(problems))
        return self._inner.publish(item, video, progress)

    def recover(self, item, record):
        return self._inner.recover(item, record)

    def status(self) -> dict:
        s = dict(self._inner.status())
        p = self.ready_problems()
        s.update(account=self.account.id, ready=not p, live=self.account.live_ok(), problems=p, verified=False)
        return safety.redact_obj(s)


def delivery_contract(adapter) -> dict:
    """Implemented request fields of the selected adapter. No secret lookup or network."""
    inner=adapter._inner if isinstance(adapter,Gated) else adapter
    if hasattr(inner,'delivery_contract'): return inner.delivery_contract()
    platform=getattr(inner,'platform',None) or getattr(inner,'name','')
    return {'adapter':type(inner).__name__,'shapes':sorted(set(getattr(inner,'capabilities',{'video'} if platform=='youtube' else ())) & {'text','image','carousel','video','document'}),
            'first_comment':platform in ('instagram','threads'),'cover':platform=='instagram','feed_presence':platform=='instagram'}


def adapter_for(account: Account, **kw) -> Publisher:
    """An adapter bound to `account`. kw: http, host, now, platforms_cfg, sleep (all optional)."""
    plat = account.platform
    if account.mode == "aggregator":
        from ..publish.adapters.aggregator import AggregatorPublisher
        return Gated(AggregatorPublisher(network=plat, **_existing_kw(kw)), account)        # type: ignore[return-value]
    if plat == "youtube":
        from ..publish.adapters.youtube import YouTubePublisher
        return Gated(YouTubePublisher(secret_ns=account.cred_ref if account.bound else None, **_existing_kw(kw, session=False)), account)  # type: ignore[return-value]
    if plat not in ADAPTERS:
        raise ValueError(f"unknown platform {plat!r}; choose from {', '.join(platforms())}")
    return adapter_class(plat)(account, **kw)


def _existing_kw(kw: dict, session: bool = True) -> dict:
    out = {k: v for k, v in kw.items() if k in ("now", "platforms_cfg")}
    if session and kw.get("http") is not None:
        out["session"] = kw["http"]
    return out


def get_publisher(name: str, **kw) -> Publisher:
    """UNBOUND adapter for `name`: never live, legacy secret names. Raises ValueError for a name nobody serves."""
    if name in ADAPTERS:
        return adapter_class(name)(Account.unbound(name), **kw)
    from ..publish.adapters import ALL_PLATFORMS
    raise ValueError(f"unknown platform {name!r}; choose from {', '.join(ALL_PLATFORMS)}. "
                     "Direct Instagram/X/TikTok are not implemented here: they route through the aggregator adapter "
                     "(studio accounts bind their own adapters).")


def publishers_for_account(sp, account_id: str, **kw) -> dict[str, Publisher]:
    acct = load_account(sp, account_id)
    return {acct.platform: adapter_for(acct, **kw)}


def publishers_for_item(sp, item: dict, **kw) -> dict[str, Publisher]:
    """`{platform: adapter}` for `queue.run(sp, item_id, publishers=...)`. Raises AccountError when the item names no/unknown account."""
    account_id = item.get("account")
    if not account_id:
        raise AccountError(f"queue item {item.get('id')} names no account")
    return publishers_for_account(sp, account_id, **kw)


def adapter_report(sp, **kw) -> list[dict]:
    """For `ave studio doctor` / accounts page: per account, readiness and live state. No secret values, no network."""
    out = []
    for acct in list_accounts(sp):
        try:
            st = adapter_for(acct, **kw).status()
        except Exception as e:                    # a broken adapter must not hide the other accounts
            st = {"platform": acct.platform, "account": acct.id, "ready": False, "problems": [safety.redact(e)], "verified": False}
        out.append(st)
    return out


def register(studio) -> None:
    """Plug into the Studio facade (service.py is not edited): `studio.platform_publishers(item)` gives account-bound adapters using the
    studio's host, and the `account_live` hook is registered when the facade defines it."""
    def platform_publishers(item: dict) -> dict[str, Publisher]:
        return publishers_for_item(studio.sp, item, host=studio.host, now=studio.clock.now)

    studio.platform_publishers = platform_publishers          # type: ignore[attr-defined]
    studio.adapter_report = lambda: adapter_report(studio.sp, host=studio.host, now=studio.clock.now)   # type: ignore[attr-defined]
    from ..studio.service import HOOKS
    if "account_live" in HOOKS:
        def account_live(st, account_id: str) -> bool:
            try:
                return load_account(st.sp, account_id).live_ok()
            except AccountError:
                return False
        studio.register("account_live", account_live)
