"""YouTube Data API v3 resumable upload (videos.insert), scope youtube.upload only.

Credentials: a JSON blob {client_id, client_secret, refresh_token} in the OS keychain (`ave publish auth youtube`)
or env AVE_YOUTUBE_CREDENTIALS. Never written to the repo.

Outcomes surfaced plainly: quota exhaustion (an upload costs ~1600 of the default 10000 daily units) and the
"unverified API project => uploads are locked private" outcome, where the upload succeeds but the video is private
until the Google audit passes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .. import safety
from .base import (Absent, PublishError, Publisher, Uncertain, effective_format, limit_problems, load_platforms,
                   probe_video)

SCOPE = "https://www.googleapis.com/auth/youtube.upload"
READ_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
CHUNK = 8 * 1024 * 1024
MAX_RETRIES = 5
log = safety.get_logger("ave.publish.youtube")


def _http_status(e: Exception) -> int | None:
    resp = getattr(e, "resp", None)
    try:
        return int(getattr(resp, "status", None))
    except (TypeError, ValueError):
        return None


def _http_reason(e: Exception) -> str:
    raw = getattr(e, "content", b"") or b""
    try:
        data = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        errs = data.get("error", {}).get("errors") or []
        return errs[0].get("reason", "") if errs else str(data.get("error", {}).get("message", ""))
    except Exception:
        return ""


class YouTubePublisher(Publisher):
    name = "youtube"

    def __init__(self, service_factory: Callable[[], Any] | None = None, media_factory: Callable[..., Any] | None = None,
                 **kw):
        super().__init__(**kw)
        self._service_factory = service_factory
        self._media_factory = media_factory
        self.y = self.cfg.get("youtube", {})

    # -- credentials
    def _creds_json(self) -> dict | None:
        raw = safety.get_secret("youtube_credentials")
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return None

    def ready_problems(self) -> list[str]:
        c = self._creds_json()
        if not c or not all(c.get(k) for k in ("client_id", "client_secret", "refresh_token")):
            return ["no YouTube credentials; run `ave publish auth youtube` (see docs/publishing-setup.md)"]
        return []

    def status(self) -> dict:
        return {"platform": "youtube", "ready": not self.ready_problems(), "problems": self.ready_problems()}

    def _service(self):
        if self._service_factory:
            return self._service_factory()
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        c = self._creds_json() or {}
        creds = Credentials(None, refresh_token=c["refresh_token"], client_id=c["client_id"],
                            client_secret=c["client_secret"], token_uri="https://oauth2.googleapis.com/token",
                            scopes=[SCOPE, READ_SCOPE])
        return build("youtube", "v3", credentials=creds, cache_discovery=False)

    # -- validation
    def preflight(self, item: dict, video: Path) -> list[str]:
        if (item.get("media_type") or "video") != "video":
            return ["youtube: this adapter publishes video only; images and carousels are not supported "
                    "(set a thumbnail in YouTube Studio, or use an adapter that carries images)"]
        p: list[str] = []
        t = item.get("title") or ""
        if not t.strip():
            p.append("youtube: title is required")
        if len(t) > self.y.get("title_max_chars", 100):
            p.append(f"youtube: title is {len(t)} chars, max {self.y.get('title_max_chars', 100)}")
        if "<" in t or ">" in t:
            p.append("youtube: title may not contain < or >")
        d = item.get("description") or ""
        if len(d.encode()) > self.y.get("description_max_bytes", 5000):
            p.append("youtube: description is over the byte limit")
        if "<" in d or ">" in d:
            p.append("youtube: description may not contain < or >")
        if sum(len(x) for x in item.get("tags") or []) > self.y.get("tags_max_chars", 500):
            p.append("youtube: tags exceed the total character limit")
        if item.get("ai_disclosure") is None:
            p.append("youtube: ai_disclosure must be set explicitly (--ai-disclosure yes|no)")
        if item.get("visibility") not in ("private", "unlisted", "public"):
            p.append("youtube: visibility must be private/unlisted/public")
        p += self.check_schedule(item)
        try:
            info = probe_video(video)
        except ValueError as e:
            return p + [f"youtube: {e}"]
        fmt = effective_format(item, info)
        p += limit_problems({k: v for k, v in self.y.items() if k == "max_file_bytes"}, info, "youtube")
        p += limit_problems(self.y.get("short" if fmt == "short" else "long"), info, f"youtube {fmt}")
        return p

    # -- sending
    def _body(self, item: dict) -> dict:
        status: dict = {"privacyStatus": item.get("visibility", "private"),
                        "selfDeclaredMadeForKids": bool(item.get("made_for_kids", False)),
                        "containsSyntheticMedia": bool(item.get("ai_disclosure"))}
        if item.get("schedule"):
            status["privacyStatus"] = "private"       # the API requires private + publishAt for scheduling
            status["publishAt"] = item["schedule"]
        return {"snippet": {"title": item["title"], "description": item.get("description", ""),
                            "tags": item.get("tags") or [], "categoryId": "22"}, "status": status}

    def publish(self, item: dict, video: Path, progress: Callable[[dict], None]) -> dict:
        from googleapiclient.errors import HttpError
        service = self._service()
        body = self._body(item)
        media = (self._media_factory or _media)(str(video))
        req = service.videos().insert(part="snippet,status", body=body, media_body=media)
        response, retries, announced = None, 0, False
        while response is None:
            try:
                _, response = req.next_chunk()
            except HttpError as e:
                st = _http_status(e)
                if st in (500, 502, 503, 504) and retries < MAX_RETRIES:
                    retries += 1     # resumable protocol: re-asking continues the same upload, no duplicate video
                    continue
                reason = _http_reason(e)
                if st == 403 and reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
                    raise PublishError("YouTube API daily quota exhausted (an upload costs ~1600 of the default 10000 "
                                       "units). Nothing was posted. Retry after midnight Pacific or request more quota.")
                if st == 403 and reason == "uploadLimitExceeded":
                    raise PublishError("YouTube upload limit reached for this channel. Nothing was posted; try later.")
                if st in (400, 401, 403, 404):
                    raise PublishError(f"YouTube rejected the upload (HTTP {st} {reason}). Nothing was posted.")
                raise Uncertain(f"YouTube upload ended with HTTP {st}; it may or may not exist")
            uri = getattr(req, "resumable_uri", None)
            if uri and not announced:
                progress({"upload_uri": uri})
                announced = True
        vid = response["id"]
        got = (response.get("status") or {}).get("privacyStatus")
        out = {"id": vid, "url": f"https://youtu.be/{vid}", "privacy_status": got, "warnings": []}
        want = body["status"]["privacyStatus"]
        if got and got != want:
            out["warnings"].append(
                f"UPLOADED BUT FORCED {got.upper()}: requested {want}. Unverified API projects are locked private until "
                "the YouTube API audit passes (see docs/publishing-setup.md). Change visibility in YouTube Studio.")
        return out

    def recover(self, item: dict, record: dict):
        """Look in the channel's uploads playlist for a video with this title created since the attempt started."""
        try:
            svc = self._service()
            ch = svc.channels().list(part="contentDetails", mine=True).execute()
            uploads = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
            items = svc.playlistItems().list(part="snippet", playlistId=uploads, maxResults=25).execute().get("items", [])
        except Exception as e:
            log.warning("youtube recovery lookup failed: %s", e)
            return None
        started = record.get("started", "")
        for it in items:
            sn = it.get("snippet", {})
            if sn.get("title") == item["title"] and sn.get("publishedAt", "") >= _utc_prefix(started):
                vid = sn.get("resourceId", {}).get("videoId")
                return {"id": vid, "url": f"https://youtu.be/{vid}", "recovered": True}
        return Absent("no matching upload in the channel's uploads playlist")


def _utc_prefix(local_iso: str) -> str:
    """Queue timestamps are local with offset; YouTube's are UTC 'Z'. Compare in UTC."""
    from datetime import datetime, timezone
    try:
        return datetime.strptime(local_iso, "%Y-%m-%dT%H:%M:%S%z").astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return ""


def _media(path: str):
    from googleapiclient.http import MediaFileUpload
    return MediaFileUpload(path, chunksize=CHUNK, resumable=True, mimetype="video/*")


def run_oauth_flow(client_secrets_file: str) -> dict:
    """Installed-app OAuth with a local redirect server; stores the refresh token in the keychain. Interactive."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(client_secrets_file, scopes=[SCOPE, READ_SCOPE])
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    if not creds.refresh_token:
        raise SystemExit("Google returned no refresh token. Revoke the app's access in your Google account and retry.")
    blob = {"client_id": creds.client_id, "client_secret": creds.client_secret, "refresh_token": creds.refresh_token}
    safety.set_secret("youtube_credentials", json.dumps(blob))
    return {"stored": "keychain", "platform": "youtube"}
