"""Facebook Page publishing via the Graph API: Reels (start/upload/finish) and long-form /videos (chunked).

UNVERIFIED against live Meta endpoints (no account was available when this was written); the flow follows Meta's
published docs (see config/platforms.yaml sources). Credentials: Page access token (`facebook_page_token`) and Page id
(`facebook_page_id`) from the keychain or AVE_FACEBOOK_PAGE_TOKEN / AVE_FACEBOOK_PAGE_ID.

Safety choice: a Page post goes live ONLY when item visibility is "public" or a schedule is set. Anything else is
uploaded as an unpublished draft, so the default (private) can never post publicly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .. import safety
from .base import (PublishError, Publisher, Uncertain, effective_format, limit_problems, parse_schedule, platform_schedule, probe_video)

log = safety.get_logger("ave.publish.facebook")
TIMEOUT = 120


class FacebookPublisher(Publisher):
    name = "facebook"

    def __init__(self, session: Any = None, secret_ns: str | None = None, **kw):
        super().__init__(**kw)
        self.secret_ns = secret_ns        # studio accounts: secret names get an `@<account>` suffix (spec 2.5, E4)
        self._session = session
        self.f = self.cfg.get("facebook", {})
        self.ver = self.f.get("graph_version", "v21.0")

    @property
    def http(self):
        if self._session is None:
            import requests
            self._session = requests.Session()
        return self._session

    def _secret(self, base: str) -> str:
        return f"{base}@{self.secret_ns}" if self.secret_ns else base

    def _token(self) -> str:
        return safety.get_secret(self._secret("facebook_page_token")) or ""

    def _page(self) -> str:
        return safety.get_secret(self._secret("facebook_page_id")) or ""

    def ready_problems(self) -> list[str]:
        out = []
        if not self._token():
            out.append("no Facebook Page token; run `ave publish auth facebook` or set AVE_FACEBOOK_PAGE_TOKEN")
        if not self._page():
            out.append("no Facebook Page id; run `ave publish auth facebook` or set AVE_FACEBOOK_PAGE_ID")
        return out

    def status(self) -> dict:
        p = self.ready_problems()
        return {"platform": "facebook", "ready": not p, "problems": p}

    # -- validation
    def preflight(self, item: dict, video: Path) -> list[str]:
        if (item.get("media_type") or "video") != "video":
            return ["facebook: this adapter publishes video only (Page reels and videos); photo and carousel posts are not implemented"]
        p: list[str] = []
        if len(self._desc(item)) > self.f.get("description_max_chars", 5000):
            p.append("facebook: description/caption too long")
        p += self.check_schedule(item, self.f.get("schedule_min_minutes", 10), self.f.get("schedule_max_days", 75))
        try:
            info = probe_video(video)
        except ValueError as e:
            return p + [f"facebook: {e}"]
        fmt = effective_format(item, info)
        p += limit_problems(self.f.get("reel" if fmt == "short" else "long"), info,
                            f"facebook {'reel' if fmt == 'short' else 'video'}")
        return p

    # -- http helpers
    def _call(self, phase: str, final: bool, fn: Callable[[], Any]) -> dict:
        """Run one HTTP step. Before the finish step a failure means nothing is live (PublishError). A failure of the
        finish step with 5xx/network error is Uncertain; a 4xx on finish is a definite refusal."""
        try:
            r = fn()
        except Exception as e:
            msg = safety.redact(f"{type(e).__name__}: {e}")
            if final:
                raise Uncertain(f"facebook {phase}: {msg}")
            raise PublishError(f"facebook {phase} failed before anything was published: {msg}")
        try:
            data = r.json()
        except Exception:
            data = {}
        code = getattr(r, "status_code", 200)
        if code >= 400 or "error" in data:
            err = data.get("error", {}) if isinstance(data, dict) else {}
            msg = safety.redact(f"facebook {phase} HTTP {code}: {err.get('message', data)} (code {err.get('code')})")
            from ...platforms.base import AuthError, QuotaError
            if code==401 or err.get('code')==190: raise AuthError(msg)
            if code==429 or err.get('code') in (4,17,32,613):
                try: wait=float((getattr(r,'headers',{}) or {}).get('Retry-After') or (getattr(r,'headers',{}) or {}).get('retry-after'))
                except (ValueError,TypeError): wait=None
                raise QuotaError(msg,wait)
            if final and code >= 500:
                raise Uncertain(msg)
            raise PublishError(msg + ". Nothing was posted.")
        return data

    def _base(self, host: str = "graph.facebook.com") -> str:
        return f"https://{host}/{self.ver}"

    def _live(self, item: dict) -> bool:
        return bool(platform_schedule(item)) or item.get("visibility") == "public"

    # -- sending
    def publish(self, item: dict, video: Path, progress: Callable[[dict], None]) -> dict:
        info = probe_video(video)
        fmt = effective_format(item, info)
        if not self._token() or not self._page():
            raise PublishError("; ".join(self.ready_problems()))
        return (self._reel if fmt == "short" else self._long)(item, video, info, progress)

    def _desc(self, item: dict) -> str:
        if item.get('meta_v') == 3:
            from ...platforms.base import compose_text
            return compose_text(item)
        return "\n\n".join(x for x in (item.get("caption"), item.get("description")) if x)

    def _reel(self, item, video, info, progress):
        tok, page = self._token(), self._page()
        url = f"{self._base()}/{page}/video_reels"
        d = self._call("reel start", False, lambda: self.http.post(url, data={"upload_phase": "start", "access_token": tok}, timeout=TIMEOUT))
        vid, upload_url = d["video_id"], d["upload_url"]
        progress({"video_id": vid, "phase": "started"})
        size = Path(video).stat().st_size

        def up():
            with open(video, "rb") as fh:
                return self.http.post(upload_url, data=fh, headers={"Authorization": f"OAuth {tok}", "offset": "0",
                                                                     "file_size": str(size)}, timeout=None)
        self._call("reel upload", False, up)
        progress({"phase": "uploaded"})
        fin = {"upload_phase": "finish", "video_id": vid, "access_token": tok, "description": self._desc(item),
               "title": item["title"]}
        if platform_schedule(item):
            fin.update(video_state="SCHEDULED", scheduled_publish_time=int(parse_schedule(item["schedule"]).timestamp()))
        else:
            fin["video_state"] = "PUBLISHED" if self._live(item) else "DRAFT"
        progress({"phase": "finishing"})
        ack=self._call("reel finish", True, lambda: self.http.post(url, data=fin, timeout=TIMEOUT))
        if ack.get('success') is not True: raise Uncertain('facebook finish did not confirm acceptance')
        from ..receipts import result
        phase='scheduled' if platform_schedule(item) else 'processing' if self._live(item) else 'draft'
        return result('facebook',vid,phase=phase,object_kind='video',state_requested=fin['video_state'])

    def _long(self, item, video, info, progress):
        tok, page = self._token(), self._page()
        url = f"{self._base('graph-video.facebook.com')}/{page}/videos"
        size = Path(video).stat().st_size
        d = self._call("video start", False, lambda: self.http.post(
            url, data={"upload_phase": "start", "file_size": size, "access_token": tok}, timeout=TIMEOUT))
        sid, vid = d["upload_session_id"], d.get("video_id")
        progress({"upload_session_id": sid, "video_id": vid, "phase": "started"})
        start, end = int(d["start_offset"]), int(d["end_offset"])
        while start < end:
            with open(video, "rb") as fh:
                fh.seek(start)
                chunk = fh.read(end - start)
            d = self._call("video transfer", False, lambda: self.http.post(
                url, data={"upload_phase": "transfer", "upload_session_id": sid, "start_offset": start, "access_token": tok},
                files={"video_file_chunk": ("chunk", chunk, "application/octet-stream")}, timeout=None))
            start, end = int(d["start_offset"]), int(d["end_offset"])
        progress({"phase": "uploaded"})
        fin = {"upload_phase": "finish", "upload_session_id": sid, "access_token": tok, "title": item["title"],
               "description": self._desc(item)}
        if platform_schedule(item):
            fin.update(published="false", scheduled_publish_time=int(parse_schedule(item["schedule"]).timestamp()))
        else:
            fin["published"] = "true" if self._live(item) else "false"
        progress({"phase": "finishing"})
        ack=self._call("video finish", True, lambda: self.http.post(url, data=fin, timeout=TIMEOUT))
        if ack.get('success') is not True: raise Uncertain('facebook finish did not confirm acceptance')
        from ..receipts import result
        phase='scheduled' if platform_schedule(item) else 'processing' if self._live(item) else 'draft'
        return result('facebook',vid,phase=phase,object_kind='video',published_requested=fin['published'])

    def recover(self, item: dict, record: dict):
        vid = record.get("video_id")
        if not vid or self.ready_problems():
            return None
        data=self._call('status refresh',False,lambda:self.http.get(f"{self._base()}/{vid}",params={'fields':'status','access_token':self._token()},timeout=TIMEOUT))
        st=data.get('status') or {}
        if (st.get("publishing_phase") or {}).get("status") == "complete":
            from ..receipts import result
            return result('facebook',vid,f'https://www.facebook.com/{vid}',recovered=True)
        return None    # still processing or never finished: a human must decide
