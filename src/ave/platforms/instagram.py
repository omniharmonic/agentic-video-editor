"""Instagram via the Graph API container flow. Tier 1, behind the gate. UNVERIFIED live; documentation-derived
(https://developers.facebook.com/docs/instagram-platform/content-publishing).

  POST /{ig-user-id}/media          image_url | video_url, caption, alt_text, media_type (REELS | STORIES | CAROUSEL), children   -> {id: container}
  GET  /{container}?fields=status_code   IN_PROGRESS -> FINISHED | ERROR | EXPIRED                                              (poll)
  GET  /{ig-user-id}/content_publishing_limit?fields=quota_usage,config      (checked before each publish; ~100 API posts / 24 h)
  POST /{ig-user-id}/media_publish  {creation_id}                                                                      (FINAL step)

Media is FETCHED BY META from a public URL, so every file goes through the `MediaHost` (platforms/host.py): the host stores exactly the
approved bytes (hash checked before and after upload), the URL is deleted in `finally`. Images are JPEG only (config/platforms.yaml
`instagram.image.formats`; the content renderer converts at render time). There is no native scheduling: our scheduler posts.
The container id is saved with `progress()` so `recover` can ask Meta whether it was published instead of posting twice.
Credentials: secret `instagram_token@<ref>` (long-lived, 60 days; see `refresh`), public `credentials.ig_user_id`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..publish import safety
from ..publish.adapters.base import Absent, PublishError, Uncertain, limit_problems, probe_video
from .base import PlatformAdapter, compose_text, item_files, media_type

POLL_TRIES, POLL_INTERVAL = 40, 3.0


class InstagramAdapter(PlatformAdapter):
    platform = "instagram"
    capabilities = frozenset({"image", "carousel", "video"})
    needs_public_url = True
    required = ("token", "ig_user_id")
    refresh_url = "https://graph.instagram.com/refresh_access_token"
    refresh_grant = "ig_refresh_token"

    @property
    def base(self) -> str:
        product=self.account.connection.get('product','facebook_login')
        host='graph.instagram.com' if product=='instagram_login' else 'graph.facebook.com'
        return str(self.limit("api_base",default=f"https://{host}/{self.limit('graph_version',default='v21.0')}")).rstrip('/')

    def kind(self, item: dict) -> str:
        """CAROUSEL | REELS | STORIES | IMAGE from the item's media type and post type."""
        mt = media_type(item)
        if mt == "carousel":
            return "CAROUSEL"
        if mt == "video":
            return "STORIES" if item.get("post_type") == "story" else "REELS"
        return "STORIES" if item.get("post_type") == "story" else "IMAGE"

    # ---- validation (pure)
    def preflight(self, item: dict, video: Path) -> list[str]:
        p = self.bound_problems(item) + self.alt_problems(item) + self.host_problems()
        if self.account.connection.get('product','facebook_login') not in ('facebook_login','instagram_facebook','instagram_login'):
            p.append('instagram: choose a documented Facebook Login or Instagram Login product')
        if self.kind(item)=='STORIES' and (compose_text(item) or any(item.get('alt_texts') or [])):
            p.append('instagram: Story caption and alt text require native app steps; use manual delivery')
        if self.kind(item)=='REELS' and any(item.get('alt_texts') or []):
            p.append('instagram: Reels do not accept image alt text; use manual delivery')
        if item.get('cover') and self.kind(item)!='REELS': p.append('instagram: automatic covers are supported only for Reels')
        if item.get('first_comment') and len(item['first_comment'])>2200: p.append('instagram: first comment exceeds 2200 characters')
        if media_type(item) in ('image','carousel') and self.kind(item)!='STORIES':
            from ..publish.adapters.base import probe_image
            for f in item_files(item,video):
                try:
                    image=probe_image(f)
                    if image['width']/image['height']<0.8: p.append('instagram: feed images must be at least 4:5; choose a reviewed crop or manual delivery')
                except ValueError as e: p.append(str(e))
        if media_type(item) == "video":
            try:
                p += limit_problems(self.limit("reel"), probe_video(video), "instagram reel")
            except ValueError as e:
                p.append(f"instagram: {e}")
        return p

    def _verify(self) -> dict:
        d = self._send("verify", "GET", f"{self.base}/{self.account.credential('ig_user_id')}",
                       params={"fields": "id,username", "access_token": self.account.secret("token")}).data
        if str(d.get("id"))!=str(self.account.credential("ig_user_id")): raise PublishError("Instagram destination identity mismatch")
        return {"id": d.get("id"), "username": d.get("username"), "posting_scopes_verified":False}

    def refresh(self) -> dict:
        """`ave studio account refresh`: exchange the long-lived token for a fresh one and store it in the keychain only."""
        if self.account.connection.get('product','facebook_login')!='instagram_login' or self.account.connection.get('token_family') not in (None,'instagram_user'):
            raise PublishError('Instagram refresh requires Instagram Login; reconnect Facebook Login through its own token flow')
        d = self._send("refresh", "GET", self.refresh_url, params={"grant_type": self.refresh_grant, "access_token": self.account.secret("token")}).data
        if not d.get("access_token"):
            raise PublishError(f"{self.platform} refresh returned no token")
        safety.set_secret(self.account.secret_name("token"), d["access_token"])
        return {"refreshed": True, "expires_in": d.get("expires_in")}

    # ---- sending
    def _quota(self, ig: str, tok: str) -> None:
        d = self._send("publishing limit", "GET", f"{self.base}/{ig}/content_publishing_limit",
                       params={"fields": "quota_usage,config", "access_token": tok}).data
        row = (d.get("data") or [{}])[0]
        used, total = row.get("quota_usage"), (row.get("config") or {}).get("quota_total", self.limit("rate", "api_posts_per_24h", default=100))
        if isinstance(used, int) and isinstance(total, int) and used >= total:
            from .base import QuotaError
            raise QuotaError(f"instagram: {used} of {total} API posts used in the last 24 h; nothing was posted")

    def _wait(self, cid: str, tok: str) -> None:
        self._poll("container", f"{self.base}/{cid}", params={"fields": "status_code", "access_token": tok}, field_="status_code",
                   done={"FINISHED", "PUBLISHED"}, failed={"ERROR", "EXPIRED"}, tries=POLL_TRIES, interval=POLL_INTERVAL)

    def publish(self, item: dict, video: Path, progress: Callable[[dict], None]) -> dict:
        problems = self.bound_problems(item) + self.cred_problems()
        if problems:
            raise PublishError("; ".join(problems))
        kind, files = self.kind(item), item_files(item, video)
        alts = item.get("alt_texts") or []
        ig, tok = self.account.credential("ig_user_id"), self.account.secret("token")
        hosted, cleanup = self.host_files(files, progress)
        try:
            self._quota(ig, tok)
            urls = [h["url"] for h in hosted]
            media = f"{self.base}/{ig}/media"
            caption = compose_text(item)
            if kind == "CAROUSEL":
                kids: list[str] = []
                for i, u in enumerate(urls):
                    body = {"image_url": u, "is_carousel_item": True, "access_token": tok, **({"alt_text": alts[i]} if i < len(alts) and alts[i] else {})}
                    kids.append(self._send(f"carousel item {i + 1}/{len(urls)}", "POST", media, json=body).data["id"])
                    progress({"children": list(kids)})
                for k in kids:
                    self._wait(k, tok)
                body = {"media_type": "CAROUSEL", "children": ",".join(kids), "caption": caption, "access_token": tok}
            elif kind == "REELS":
                body = {"media_type": "REELS", "video_url": urls[0], "caption": caption, "share_to_feed": (item.get("presentation") or {}).get("feed_presence", True), "access_token": tok}
            elif kind == "STORIES":
                body = {"media_type": "STORIES", ("video_url" if media_type(item)=="video" else "image_url"): urls[0], "access_token": tok}
            else:
                body = {"image_url": urls[0], "caption": caption, "access_token": tok, **({"alt_text": alts[0]} if alts and alts[0] else {})}
            if kind=='REELS' and item.get('_cover'):
                cover_host,_=self.host_files([Path(item['_cover'])],progress)
                body['cover_url']=cover_host[0]['url']
            cid = self._send(kind.lower() + " container", "POST", media, json=body).data.get("id")
            if not cid:
                raise PublishError("instagram returned no container id; nothing was posted")
            progress({"container": cid, "phase": "container"})
            self._wait(cid, tok)
            progress({"phase": "publishing"})
            pub = self._send("media_publish", "POST", f"{self.base}/{ig}/media_publish", final=True, json={"creation_id": cid, "access_token": tok}).data
            mid = pub.get("id")
            if not mid:
                raise Uncertain("instagram media_publish returned no media id; the post may or may not be live")
            primary = self.result(mid, None, container=cid)
            progress(primary)  # durable before permalink or adjunct requests
            url = self._permalink(mid, tok)
            extra: dict = {"container": cid, "kind": kind.lower()}
            if item.get("first_comment"):
                extra["adjuncts"] = {"first_comment": self._comment(mid, item["first_comment"], tok, progress)}
            return self.result(mid, url, **extra)
        finally:
            cleanup()

    def _permalink(self, mid: str, tok: str) -> str | None:
        try:
            return self._send("permalink", "GET", f"{self.base}/{mid}", params={"fields": "permalink", "access_token": tok}).data.get("permalink")
        except PublishError:
            return None             # the post is live; a missing link must never look like a failure

    def _comment(self, mid: str, text: str, tok: str, progress) -> dict:
        try:
            progress({'adjuncts':{'first_comment':{'key':mid+':first_comment','state':'sending'}}})
            d=self._send("first comment", "POST", f"{self.base}/{mid}/comments", final=True, json={"message": text, "access_token": tok}).data
            out={'key':mid+':first_comment','state':'published' if d.get('id') else 'uncertain','id':d.get('id')}
        except Exception as e:
            out={'key':mid+':first_comment','state':'failed' if isinstance(e,PublishError) else 'uncertain','error':safety.redact(e)}
        progress({'adjuncts':{'first_comment':out}})
        return out

    def recover(self, item: dict, record: dict):
        if (record.get('receipt') or {}).get('phase') == 'published': return record
        cid = record.get("container")
        if not cid:
            return None
        if self.cred_problems():
            return None
        try:
            st = self._send("recover", "GET", f"{self.base}/{cid}", params={"fields": "status_code", "access_token": self.account.secret("token")}).data.get("status_code")
        except PublishError:
            raise
        if st == "PUBLISHED":
            return self.result(None, None, phase="uncertain", container=cid, recovered=True, publication_observed=True)
        if st in ("ERROR", "EXPIRED"):
            return Absent(f"container {cid} ended as {st}; it was never published")
        return None             # FINISHED / IN_PROGRESS: media_publish may still be in flight; a human checks the account
