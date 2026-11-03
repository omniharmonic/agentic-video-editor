"""TikTok Content Posting API. Tier 3; `mode: inbox` is the default: the video lands as an inbox upload in the creator's TikTok app and the human taps
publish on the phone. UNVERIFIED live; documentation-derived (https://developers.tiktok.com/doc/content-posting-api-get-started).

  POST /v2/post/publish/creator_info/query/            (Direct Post product only; never called by inbox uploads)
  POST /v2/post/publish/inbox/video/init/              {source_info: {source: FILE_UPLOAD, video_size, chunk_size, total_chunk_count}}
         -> data.publish_id, data.upload_url
  PUT  <upload_url>                                    Content-Range: bytes a-b/size, one request per chunk (5-64 MB; the last may take the remainder)
  POST /v2/post/publish/status/fetch/                  {publish_id}: PROCESSING_UPLOAD -> SEND_TO_USER_INBOX (inbox) | PUBLISH_COMPLETE | FAILED
  photos:  POST /v2/post/publish/content/init/         {media_type: PHOTO, post_mode: MEDIA_UPLOAD, source_info: {source: PULL_FROM_URL, photo_images}}
           (TikTok pulls from a URL on a VERIFIED DOMAIN; a bounded 24-hour lease retains it across restarts)

`mode: direct` is blocked because this private utility has no eligible provider-approved Direct Post product.
A local audited flag cannot unlock it. Inbox acceptance is awaiting_user_publish, never public publication evidence.
Every response wraps {data,error}; only error.code=ok is success.
Credentials: secret `tiktok_token@<ref>` (scope `video.upload`; direct needs `video.publish`).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from ..publish import safety, receipts
from urllib.parse import urlsplit
from ..publish.adapters.base import Absent, PublishError, Uncertain, limit_problems, probe_video
from .base import PlatformAdapter, compose_text, item_files, media_type

API = "https://open.tiktokapis.com/v2"
MB = 1024 * 1024
STATUS_TRIES, STATUS_INTERVAL = 40, 3.0
_AUTH = {"access_token_invalid", "scope_not_authorized", "access_token_expired"}
_QUOTA = {"rate_limit_exceeded", "spam_risk_too_many_posts", "reached_active_user_cap"}


class TikTokAdapter(PlatformAdapter):
    platform = "tiktok"
    capabilities = frozenset({"video", "image", "carousel"})
    required = ("token",)

    @property
    def mode(self) -> str:
        return "direct" if self.account.mode == "direct" else "inbox"

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self.account.secret('token')}", "Content-Type": "application/json; charset=UTF-8"}

    # ---- the {"data","error"} envelope
    def _body_error(self, data: dict) -> bool:
        err = data.get("error")
        return isinstance(err, dict) and err.get("code") not in (None, "ok")

    def _is_auth(self, status: int, data: dict) -> bool:
        return status == 401 or (data.get("error") or {}).get("code") in _AUTH

    def _is_quota(self, status: int, data: dict) -> bool:
        return status == 429 or (data.get("error") or {}).get("code") in _QUOTA

    def _message(self, data: dict) -> str:
        err = data.get("error") or {}
        return f"{err.get('code')}: {err.get('message')}" if err else str(data)[:200]

    # ---- gates
    def live_problems(self) -> list[str]:
        out = super().live_problems()
        if self.mode == "direct":
            out.append("tiktok direct posting is locked: this local utility has no eligible approved Direct Post product; use inbox or manual delivery")
        return out

    def preflight(self, item: dict, video: Path) -> list[str]:
        p = self.bound_problems(item) + self.alt_problems(item)
        if self.mode == "direct":
            p.append("tiktok: Direct Post requires an eligible provider-approved product; a local audited flag cannot enable it")
        if media_type(item) == "video":
            try:
                p += limit_problems(self.limit("reel"), probe_video(video), "tiktok video")
            except ValueError as e:
                p.append(f"tiktok: {e}")
        else:
            domain=getattr(self.host,"netloc",None) or urlsplit(getattr(self.host,"base","")).hostname
            if domain not in self.account.connection.get("verified_domains",[]):
                p.append("tiktok: photo inbox delivery needs provider-verified ownership of the configured media-host domain")
            if self.host is None:                    # photo posts are pulled from a URL on a verified domain
                p.append("tiktok: needs_public_url: photo posts need a media host (~/.config/ave/host.yaml); video posts do not")
        cap = int(self.limit("image", "caption_max_chars", default=2200))
        if len(compose_text(item)) > cap:
            p.append(f"tiktok: caption is {len(compose_text(item))} characters, max {cap}")
        return p

    def host_problems(self) -> list[str]:
        return []                                   # only photo posts need a host; judged per item in preflight and in publish

    def _verify(self) -> dict:
        if self.mode == 'inbox': return {'scope':'video.upload','checked':False,'note':'Inbox authorization is separate from Direct Post creator-info'}
        d = self._send("verify", "POST", f"{API}/post/publish/creator_info/query/", headers=self._h(), json={}).data.get("data") or {}
        return {"creator_username": d.get("creator_username"), "privacy_options": d.get("privacy_level_options")}

    # ---- sending
    def publish(self, item: dict, video: Path, progress: Callable[[dict], None]) -> dict:
        problems = self.preflight(item,video) + self.cred_problems() + ([p for p in self.live_problems() if "locked" in p])
        if problems:
            raise PublishError("; ".join(problems))
        info = {}  # Inbox does not call the video.publish-only creator-info endpoint.
        if media_type(item) == "video":
            return self._video(item, Path(item_files(item, video)[0]), info, progress)
        return self._photos(item, [Path(f) for f in item_files(item, video)], info, progress)

    def _post_info(self, item: dict, info: dict) -> dict:
        opts = info.get("privacy_level_options") or ["SELF_ONLY"]
        want = "PUBLIC_TO_EVERYONE" if item.get("visibility") == "public" and "PUBLIC_TO_EVERYONE" in opts else "SELF_ONLY"
        out = {"title": compose_text(item)[:2200], "privacy_level": want}
        if item.get("ai_disclosure"):
            out["is_aigc"] = True
        return out

    @staticmethod
    def _chunks(size: int) -> tuple[int, int]:
        """(chunk_size, total_chunk_count): one chunk up to 64 MB, else 32 MB chunks with the remainder folded into the last (docs: last <= 128 MB)."""
        if size <= 64 * MB:
            return size, 1
        return 32 * MB, size // (32 * MB)

    def _video(self, item, f: Path, info: dict, progress):
        size = f.stat().st_size
        dur = info.get("max_video_post_duration_sec")
        chunk, count = self._chunks(size)
        body: dict = {"source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": count}}
        if self.mode == "direct":
            body["post_info"] = self._post_info(item, info)
            path = "/post/publish/video/init/"
        else:
            path = "/post/publish/inbox/video/init/"
        d = self._send("init", "POST", f"{API}{path}", headers=self._h(), json=body).data.get("data") or {}
        pid, url = d.get("publish_id"), d.get("upload_url")
        if not pid or not url:
            raise PublishError("tiktok init returned no publish_id/upload_url; nothing was posted")
        progress({"publish_id": pid, "phase": "uploading", **({"max_duration_s": dur} if dur else {})})
        with open(f, "rb") as fh:
            for i in range(count):
                start = i * chunk
                end = size - 1 if i == count - 1 else start + chunk - 1
                fh.seek(start)
                data = fh.read(end - start + 1)
                self._send(f"upload chunk {i + 1}/{count}", "PUT", url, final=(i == count - 1), data=data, timeout=600,
                           headers={"Content-Type": "video/mp4", "Content-Length": str(len(data)), "Content-Range": f"bytes {start}-{end}/{size}"})
        progress({"phase": "uploaded"})
        return self._settle(pid)

    def _photos(self, item, files, info, progress):
        hosted, cleanup = self.host_files(files, progress)
        try:
            body = {"post_info": {"title": (item.get("title") or "")[:90], "description": compose_text(item)[:4000]},
                    "source_info": {"source": "PULL_FROM_URL", "photo_cover_index": 0, "photo_images": [h["url"] for h in hosted]},
                    "post_mode": "DIRECT_POST" if self.mode == "direct" else "MEDIA_UPLOAD", "media_type": "PHOTO"}
            if self.mode == "direct":
                body["post_info"].update(self._post_info(item, info))
            progress({"phase": "creating"})
            d = self._send("photo init", "POST", f"{API}/post/publish/content/init/", final=True, headers=self._h(), json=body).data.get("data") or {}
            pid = d.get("publish_id")
            if not pid:
                raise Uncertain("tiktok returned no publish_id; the post may or may not exist")
            progress({"publish_id": pid, "phase": "pulling"})
            return self._settle(pid)
        finally:
            cleanup()

    def _status(self, pid: str, *, final: bool) -> dict:
        return self._send("status", "POST", f"{API}/post/publish/status/fetch/", final=final, headers=self._h(), json={"publish_id": pid}).data.get("data") or {}

    def _settle(self, pid: str) -> dict:
        """Wait for TikTok to finish. The bytes are already uploaded, so a transport error here is Uncertain, not a clean failure."""
        last = None
        for _ in range(STATUS_TRIES):
            try:
                d = self._status(pid, final=True)
            except Exception as e:
                return self.result(pid, None, phase='processing', object_kind='publish_operation', diagnostics=receipts.error(e))
            last = d.get("status")
            if last in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"):
                post_ids = d.get("publicaly_available_post_id") or []
                url = None
                return self.result(str(post_ids[0]) if post_ids else pid, url, phase="awaiting_user_publish" if last == "SEND_TO_USER_INBOX" else "published" if post_ids else "processing", object_kind="publish_operation", inbox=last == "SEND_TO_USER_INBOX", status=last, post_ids=[str(x) for x in post_ids],
                                   note="waiting in the creator's TikTok inbox: tap publish in the app" if last == "SEND_TO_USER_INBOX" else None)
            if last == "FAILED":
                raise PublishError(safety.redact(f"tiktok processing failed: {d.get('fail_reason')}"))
            self.sleep(STATUS_INTERVAL)
        return self.result(pid, None, phase="processing", object_kind="publish_operation", status=last)

    def recover(self, item: dict, record: dict):
        pid = record.get("publish_id")
        if not pid:
            return Absent("tiktok init never returned a publish_id; no upload started") if media_type(item) == "video" else None
        if self.cred_problems():
            return None
        try:
            d = self._status(pid, final=False)
        except PublishError:
            raise
        st = d.get("status")
        if st in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"):
            ids=d.get('publicaly_available_post_id') or []
            return self.result(str(ids[0]) if ids else pid, None, phase='awaiting_user_publish' if st=='SEND_TO_USER_INBOX' else 'published' if ids else 'processing', object_kind='publish_operation', status=st, recovered=True)
        if st == "FAILED":
            return self.result(pid, None, phase='failed', object_kind='publish_operation',
                               error=safety.redact(f"TikTok rejected the accepted operation: {d.get('fail_reason')}"),
                               diagnostics={'kind':'provider_terminal','status':'FAILED','reason':safety.redact(str(d.get('fail_reason') or 'Unknown provider reason'))})
        return None
