"""The studio adapter contract (spec 4.1) on top of `ave.publish.adapters.base.Publisher`.

    PlatformAdapter(account, *, http=None, host=None, now=None, platforms_cfg=None, sleep=None)

* `http` is anything with requests-style `get/post/put/delete(url, **kw)` returning an object with `status_code`, `json()`, `headers`
  and `.text`/`.content` (a `requests.Session` by default; tests pass `FakeHttp`). Adapters never open a socket themselves.
* Dry-run is the default at every layer: `preflight` is pure, `ready_problems` is only asked on a real send (queue.py), and nothing
  here touches the network until `publish`.
* Outcome taxonomy (queue.py): `PublishError` = the platform DEFINITELY did not publish; `Uncertain` (or any other exception) leaves
  the queue state `sending`. A failure BEFORE the final step is always `PublishError` (nothing is live); a transport error or 5xx on the
  FINAL step is `Uncertain`; a 4xx on the final step is a definite refusal.
* `AuthError` and `QuotaError` are `PublishError`s the scheduler can classify (spec 6.5): no retry on auth, wait on quota.

Every adapter ships `verified: false` (config/platforms.yaml) until a human burn-in flips it. Secrets are resolved per account on demand
(`Account.credential`) and are never stored on the adapter, returned from `status()`, or written to a result.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

from ..publish import safety
from ..publish.adapters.base import Absent, PublishError, Publisher, Uncertain, limit_problems, load_platforms, probe_video  # noqa: F401
from .accounts import Account

TIMEOUT = 60
UPLOAD_TIMEOUT = 600

log = safety.get_logger("ave.platforms")


class AuthError(PublishError):
    """401 / expired or invalid token. Not retried; the human re-authorises (`ave studio account auth`)."""


class QuotaError(PublishError):
    """429 or a platform quota. `retry_after` seconds when the response said so."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class HostError(PublishError):
    """The media host failed or served other bytes than the approved ones. Nothing was sent to the platform."""


@dataclass
class CredStatus:
    """Read-only answer to "can this account talk to the platform?". Holds NO secret values."""
    ready: bool
    checked: bool = False                      # True only when a read-only API call was made
    missing: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return safety.redact_obj({"ready": self.ready, "checked": self.checked, "missing": self.missing, "detail": self.detail})


@runtime_checkable
class MediaHost(Protocol):
    def put(self, path: Path, *, ttl_s: int = 3600) -> dict: ...      # {"url","key","sha256","expires"}

    def delete(self, key: str) -> None: ...


@dataclass
class Reply:
    data: dict
    status: int
    headers: dict


def _lower(h: Any) -> dict:
    try:
        return {str(k).lower(): v for k, v in dict(h or {}).items()}
    except Exception:
        return {}


def secret_env_name(name: str) -> str:
    return safety._env_name(name)


def compose_text(item: dict) -> str:
    """The text a post carries: caption, else description, else title; the link appended once when it is not already in the text."""
    # v3 explicitly binds the authoritative composed caption, including emptiness.
    # Earlier signatures retain their historical fallback interpretation.
    if item.get("meta_v") == 3 and "caption" in item:
        return item["caption"]
    text = (item.get("caption") or item.get("description") or item.get("title") or "").strip()
    link = (item.get("link_url") or "").strip()
    if link and link not in text:
        text = f"{text}\n{link}" if text else link
    return text


def item_files(item: dict, video: Path | None = None) -> list[Path]:
    """The verified copies the queue handed us (`_files`), else the single file passed to preflight."""
    if item.get("_files"):
        return [Path(f) for f in item["_files"]]
    return [Path(video)] if video else []


def media_type(item: dict) -> str:
    """image | carousel | video, or `text` for an item that names no media at all (the queue always stores `video` for a video item)."""
    return item.get("media_type") or ("video" if item.get("video") or item.get("_files") else "text")


MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp",
        ".mp4": "video/mp4", ".mov": "video/quicktime", ".pdf": "application/pdf"}


def mime_of(path: Path) -> str:
    return MIME.get(Path(path).suffix.lower(), "application/octet-stream")


class AccountBound:
    """Behaviour every account-bound adapter shares: the live gate, credential problems, capability checks, redacted status."""

    platform = "base"
    capabilities: frozenset = frozenset()
    needs_public_url = False
    native_schedule = False
    required: tuple[str, ...] = ()                 # credential fields that must resolve (secret or public id) before a send
    api_label = "documentation-derived"

    def _bind(self, account: Account, host: MediaHost | None, sleep: Callable[[float], None] | None) -> None:
        self.account = account
        self.host = host
        self.sleep = sleep or time.sleep

    # ---- gates
    def cred_problems(self) -> list[str]:
        return [f"missing credential {n}; run `ave studio account auth {self.account.id}` or set {secret_env_name(n)}"
                for n in self.account.missing(self.required)]

    def live_problems(self) -> list[str]:
        st = self.account.live_status()
        out = [] if st.ok else [f"account {self.account.id} is not live ({st.reason}); a human enables it with `ave studio account live {self.account.id} --on`"]
        if self.account.paused():
            out.append(f"account {self.account.id} is paused")
        return out

    def host_problems(self) -> list[str]:
        return []

    def ready_problems(self) -> list[str]:
        return self.cred_problems() + self.live_problems() + self.host_problems()

    @property
    def verified(self) -> bool:
        return bool((self.cfg.get(self.platform) or {}).get("verified", False))

    def status(self) -> dict:
        p = self.ready_problems()
        return safety.redact_obj({"platform": self.platform, "account": self.account.id, "mode": self.account.mode, "ready": not p,
                                  "live": self.account.live_ok(), "problems": p, "verified": self.verified,
                                  "capabilities": sorted(self.capabilities), "needs_public_url": self.needs_public_url})

    # ---- credentials
    def verify_credentials(self) -> CredStatus:
        miss = self.account.missing(self.required)
        if miss:
            return CredStatus(False, False, miss)
        try:
            detail = self._verify() or {}
        except PublishError as e:
            return CredStatus(False, True, [], {"error": safety.redact(e)})
        return CredStatus(True, detail.pop("checked",True), [], detail)

    def _verify(self) -> dict:
        """One read-only call that proves the credentials work; returns non-secret detail (a handle, an id)."""
        return {}

    # ---- shared preflight pieces (pure)
    def bound_problems(self, item: dict) -> list[str]:
        out = []
        if self.platform in ('instagram','threads','x','bluesky','pinterest') and item.get('visibility')!='public':
            out.append(f'{self.platform}: this API path supports public creation only; private/unlisted must remain a local draft or manual package')
        if item.get('first_comment') and self.platform not in ('instagram','threads'):
            out.append(f'{self.platform}: automatic first comment is unsupported; choose manual delivery or clear it')
        if item.get('cover') and self.platform != 'instagram':
            out.append(f'{self.platform}: automatic cover is unsupported; choose manual delivery')
        if item.get('presentation') and self.platform != 'instagram':
            out.append(f'{self.platform}: automatic feed presence is unsupported; choose manual delivery')
        acct = item.get("account")
        if acct and self.account.bound and acct != self.account.id:
            out.append(f"{self.platform}: the item is for account {acct!r} but this adapter is bound to {self.account.id!r}")
        pt = item.get("post_type")
        if pt and self.account.post_types and pt not in self.account.post_types:
            out.append(f"{self.platform}: account {self.account.id} does not take post type {pt!r} (accepts {', '.join(self.account.post_types)})")
        mt = media_type(item)
        if mt not in self.capabilities:
            out.append(f"{self.platform}: this adapter does not post {mt} items (it posts {', '.join(sorted(self.capabilities))})")
        return out

    def alt_problems(self, item: dict) -> list[str]:
        if media_type(item) in ("image", "carousel"):
            alts = item.get("alt_texts") or []
            if not alts or not all(isinstance(a, str) and a.strip() for a in alts):
                return [f"{self.platform}: alt text is required for every image"]
        return []

    def limit(self, *path: str, default: Any = None) -> Any:
        cur: Any = self.cfg.get(self.platform) or {}
        for p in path:
            cur = cur.get(p) if isinstance(cur, dict) else None
        return default if cur is None else cur

    def result(self, id_: str | None, url: str | None, **extra) -> dict:
        from ..publish.receipts import result
        return result(self.platform, id_, url, account=self.account.id, **extra)


class PlatformAdapter(AccountBound, Publisher):
    """Base for the REST adapters. Subclasses implement `preflight`, `publish`, `_verify` and (optionally) `recover`."""

    carries_images = True

    def __init__(self, account: Account, *, http: Any = None, host: MediaHost | None = None, now: Callable | None = None,
                 platforms_cfg: dict | None = None, sleep: Callable[[float], None] | None = None):
        Publisher.__init__(self, platforms_cfg=platforms_cfg, now=now)
        self._bind(account, host, sleep)
        self._http = http
        self.name = self.platform

    @property
    def http(self):
        if self._http is None:
            import requests
            self._http = requests.Session()
        return self._http

    # ---- HTTP with the outcome taxonomy
    def _send(self, phase: str, method: str, url: str, *, final: bool = False, **kw) -> Reply:
        kw.setdefault("timeout", TIMEOUT)
        try:
            r = getattr(self.http, method.lower())(url, **kw)
        except Exception as e:
            msg = safety.redact(f"{type(e).__name__}: {e}")
            if final:
                raise Uncertain(f"{self.platform} {phase}: {msg} (the post may or may not be live)")
            raise PublishError(f"{self.platform} {phase} failed before anything was published: {msg}")
        status = int(getattr(r, "status_code", 200) or 200)
        headers = _lower(getattr(r, "headers", None))
        try:
            data = r.json()
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {"_list": data} if isinstance(data, list) else {}
        if status >= 400 or self._body_error(data):
            self._raise(phase, final, status, data, headers)
        return Reply(data, status, headers)

    def _body_error(self, data: dict) -> bool:
        return isinstance(data.get("error"), dict) and bool(data["error"])

    def _message(self, data: dict) -> str:
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("error_user_msg") or err)
        for k in ("message", "detail", "error_description", "error"):
            if data.get(k):
                return str(data[k])
        return str(data)[:300]

    def _is_auth(self, status: int, data: dict) -> bool:
        err = data.get("error")
        code = err.get("code") if isinstance(err, dict) else None
        return status == 401 or code == 190 or err in ("ExpiredToken", "InvalidToken", "AuthenticationRequired", "AuthMissing")

    def _is_quota(self, status: int, data: dict) -> bool:
        err = data.get("error")
        code = err.get("code") if isinstance(err, dict) else None
        return status == 429 or code in (4, 17, 32, 613)

    def _raise(self, phase: str, final: bool, status: int, data: dict, headers: dict) -> None:
        msg = safety.redact(f"{self.platform} {phase} HTTP {status}: {self._message(data)}")
        if self._is_auth(status, data):
            raise AuthError(msg + ". Nothing was posted; re-authorise the account.")
        if self._is_quota(status, data):
            ra = headers.get("retry-after")
            try:
                wait = float(ra) if ra is not None else None
            except ValueError:
                wait = None
            raise QuotaError(msg + ". Nothing was posted; try again after the limit resets.", wait)
        if final and status >= 500:
            raise Uncertain(msg + " (the post may or may not be live)")
        raise PublishError(msg + ". Nothing was posted." if not final else msg + ". The platform refused the post.")

    def _poll(self, phase: str, url: str, *, params: dict, field_: str, done: set, failed: set, tries: int = 30,
              interval: float = 2.0, headers: dict | None = None) -> dict:
        """Wait for a media container to finish processing. A container that never finishes is NOT published, so a timeout is a PublishError."""
        last = None
        for _ in range(tries):
            d = self._send(phase, "GET", url, params=params, headers=headers or {}).data
            last = d.get(field_)
            if last in done:
                return d
            if last in failed:
                raise PublishError(safety.redact(f"{self.platform} {phase}: processing ended as {last}: {self._message(d) if d.get('error') else d.get('error_message') or ''}"))
            self.sleep(interval)
        raise PublishError(f"{self.platform} {phase}: media still {last!r} after {tries} checks; nothing was published")

    # ---- hosting for URL-fetch APIs
    def host_problems(self) -> list[str]:
        if self.needs_public_url and self.host is None:
            return [f"{self.platform}: needs_public_url: configure a media host (~/.config/ave/host.yaml) or use mode aggregator/manual"]
        return []

    def host_files(self, files: list[Path], progress=None) -> tuple[list[dict], Callable[[], None]]:
        """Put exact approved bytes on a 24-hour lease; retain after acceptance/restart."""
        if self.host is None:
            raise PublishError(f"{self.platform}: needs_public_url: no media host configured. Nothing was posted.")
        hosted: list[dict] = []

        def cleanup() -> None:
            for h in hosted:
                for attempt in (1, 2):                      # one retry; whatever still fails is swept by the next tick (`S3Host.sweep`)
                    try:
                        self.host.delete(h["key"])
                        break
                    except Exception as e:                  # never mask the real outcome
                        if attempt == 2:
                            log.warning("media host delete failed for %s (the next tick's sweep removes it): %s", h.get("key"), safety.redact(e))
            hosted.clear()

        try:
            for f in files:
                want = safety.sha256_file(f)
                try:
                    h = self.host.put(f, ttl_s=86400)
                except Exception as e:
                    raise HostError(safety.redact(f"{self.platform}: media host failed: {type(e).__name__}: {e}. Nothing was posted."))
                h={**h,"provider":self.platform,"account":self.account.id}
                hosted.append(h)
                if progress:
                    progress({'host_leases':[{k:v for k,v in x.items() if k != 'url'} for x in hosted], 'host_policy':'expire_after_24h'})
                if h.get("sha256") != want:
                    raise HostError(f"{self.platform}: media host stored different bytes than the approved file ({str(h.get('sha256'))[:12]} vs {want[:12]}); aborted. Nothing was posted.")
                verify = getattr(self.host, "verify", None)
                if verify:
                    try:
                        verify(h)
                    except HostError:
                        raise
                    except Exception as e:
                        raise HostError(safety.redact(f"{self.platform}: could not verify the hosted file: {e}. Nothing was posted."))
        except BaseException:
            cleanup()
            raise
        # The bounded expiry policy owns deletion after provider pulls, including
        # worker crashes and unresolved operations. Do not delete in finally.
        return hosted, lambda: None


__all__ = ["Absent", "AccountBound", "AuthError", "CredStatus", "HostError", "MediaHost", "PlatformAdapter", "PublishError", "QuotaError", "Reply",
           "Uncertain", "compose_text", "item_files", "media_type", "mime_of", "limit_problems", "load_platforms", "probe_video"]
