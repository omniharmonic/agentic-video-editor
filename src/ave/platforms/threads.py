"""Threads via the Threads API container flow. Tier 1; same shape as Instagram and the same `MediaHost` for images.
UNVERIFIED live; documentation-derived (https://developers.facebook.com/docs/threads/posts).

  POST /{threads-user-id}/threads          media_type TEXT | IMAGE | CAROUSEL, text, image_url, alt_text, is_carousel_item, children  -> {id}
  GET  /{container}?fields=status          IN_PROGRESS -> FINISHED | ERROR | EXPIRED                                                  (poll)
  GET  /{threads-user-id}/threads_publishing_limit?fields=quota_usage,config     (~250 posts / 24 h)
  POST /{threads-user-id}/threads_publish  {creation_id}                                                                              (FINAL step)

Text <= 500 characters. A text-only post needs no host. Credentials: secret `threads_token@<ref>` (long-lived, refreshable),
public `credentials.threads_user_id`. Scope `threads_content_publish`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..publish import safety
from ..publish.adapters.base import Absent, PublishError, Uncertain
from .base import PlatformAdapter, QuotaError, compose_text, item_files, media_type
from .instagram import POLL_INTERVAL, POLL_TRIES


class ThreadsAdapter(PlatformAdapter):
    platform = "threads"
    capabilities = frozenset({"text", "image", "carousel"})
    needs_public_url = True
    required = ("token", "threads_user_id")
    refresh_url = "https://graph.threads.net/refresh_access_token"

    @property
    def base(self) -> str:
        return str(self.limit("api_base", default=f"https://graph.threads.net/{self.limit('graph_version', default='v1.0')}")).rstrip("/")

    def _has_media(self, item: dict) -> bool:
        return media_type(item) in ("image", "carousel") or bool(item.get("_files"))

    def host_problems(self) -> list[str]:
        return []        # a text-only post needs no host; media items are judged in preflight and again in publish (host_files)

    def preflight(self, item: dict, video: Path) -> list[str]:
        p = self.bound_problems(item) + self.alt_problems(item)
        if media_type(item) in ("image", "carousel"):
            p += PlatformAdapter.host_problems(self)
        text = compose_text(item)
        limit = int(self.limit("post", "caption_max_chars", default=500))
        if len(text) > limit:
            p.append(f"threads: text is {len(text)} characters, max {limit}")
        return p

    def _verify(self) -> dict:
        d = self._send("verify", "GET", f"{self.base}/me", params={"fields": "id,username", "access_token": self.account.secret("token")}).data
        if str(d.get('id')) != str(self.account.credential('threads_user_id')):
            raise PublishError('Threads token identity does not match the configured destination')
        return {"id": d.get("id"), "username": d.get("username"), "posting_scopes_verified":False}

    def refresh(self) -> dict:
        d = self._send("refresh", "GET", self.refresh_url, params={"grant_type": "th_refresh_token", "access_token": self.account.secret("token")}).data
        if not d.get("access_token"):
            raise PublishError("threads refresh returned no token")
        safety.set_secret(self.account.secret_name("token"), d["access_token"])
        return {"refreshed": True, "expires_in": d.get("expires_in")}

    def _quota(self, uid: str, tok: str) -> None:
        d = self._send("publishing limit", "GET", f"{self.base}/{uid}/threads_publishing_limit",
                       params={"fields": "quota_usage,config", "access_token": tok}).data
        row = (d.get("data") or [{}])[0]
        used, total = row.get("quota_usage"), (row.get("config") or {}).get("quota_total", self.limit("rate", "api_posts_per_24h", default=250))
        if isinstance(used, int) and isinstance(total, int) and used >= total:
            raise QuotaError(f"threads: {used} of {total} API posts used in the last 24 h; nothing was posted")

    def _wait(self, cid: str, tok: str) -> None:
        self._poll("container", f"{self.base}/{cid}", params={"fields": "status", "access_token": tok}, field_="status",
                   done={"FINISHED", "PUBLISHED"}, failed={"ERROR", "EXPIRED"}, tries=POLL_TRIES, interval=POLL_INTERVAL)

    def publish(self, item: dict, video: Path, progress: Callable[[dict], None]) -> dict:
        problems = self.bound_problems(item) + self.cred_problems()
        if problems:
            raise PublishError("; ".join(problems))
        uid, tok = self.account.credential("threads_user_id"), self.account.secret("token")
        text, alts = compose_text(item), item.get("alt_texts") or []
        media = f"{self.base}/{uid}/threads"
        files = item_files(item, video) if self._has_media(item) else []
        hosted, cleanup = self.host_files(files, progress) if files else ([], lambda: None)
        try:
            self._quota(uid, tok)
            urls = [h["url"] for h in hosted]
            if media_type(item) == "carousel" and len(urls) > 1:
                kids: list[str] = []
                for i, u in enumerate(urls):
                    body = {"media_type": "IMAGE", "image_url": u, "is_carousel_item": True, "access_token": tok,
                            **({"alt_text": alts[i]} if i < len(alts) and alts[i] else {})}
                    kids.append(self._send(f"carousel item {i + 1}/{len(urls)}", "POST", media, json=body).data["id"])
                    progress({"children": list(kids)})
                for k in kids:
                    self._wait(k, tok)
                body = {"media_type": "CAROUSEL", "children": ",".join(kids), "text": text, "access_token": tok}
            elif urls:
                body = {"media_type": "IMAGE", "image_url": urls[0], "text": text, "access_token": tok,
                        **({"alt_text": alts[0]} if alts and alts[0] else {})}
            else:
                body = {"media_type": "TEXT", "text": text, "access_token": tok}
            cid = self._send("container", "POST", media, json=body).data.get("id")
            if not cid:
                raise PublishError("threads returned no container id; nothing was posted")
            progress({"container": cid, "phase": "container"})
            self._wait(cid, tok)
            progress({"phase": "publishing"})
            pub = self._send("threads_publish", "POST", f"{self.base}/{uid}/threads_publish", final=True,
                             json={"creation_id": cid, "access_token": tok}).data
            mid = pub.get("id")
            if not mid:
                raise Uncertain("threads_publish returned no id; the post may or may not be live")
            progress(self.result(mid, None, container=cid))
            extra: dict = {"container": cid}
            if item.get("first_comment"):
                extra["adjuncts"] = {"first_comment": self._reply(uid, mid, item["first_comment"], tok, progress)}
            return self.result(mid, self._permalink(mid, tok), **extra)
        finally:
            cleanup()

    def _permalink(self, mid: str, tok: str) -> str | None:
        try:
            return self._send("permalink", "GET", f"{self.base}/{mid}", params={"fields": "permalink", "access_token": tok}).data.get("permalink")
        except PublishError:
            return None

    def _reply(self, uid: str, mid: str, text: str, tok: str, progress) -> dict:
        """The first comment is a reply thread post. Best effort: the main post is already live."""
        try:
            cid = self._send("reply container", "POST", f"{self.base}/{uid}/threads",
                             json={"media_type": "TEXT", "text": text, "reply_to_id": mid, "access_token": tok}).data["id"]
            progress({'adjuncts':{'first_comment':{'key':mid+':first_comment','container':cid,'state':'sending'}}})
            self._wait(cid, tok)
            d=self._send("reply publish", "POST", f"{self.base}/{uid}/threads_publish", final=True, json={"creation_id": cid, "access_token": tok}).data
            out={'key':mid+':first_comment','container':cid,'state':'published' if d.get('id') else 'uncertain','id':d.get('id')}
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
            st = self._send("recover", "GET", f"{self.base}/{cid}", params={"fields": "status", "access_token": self.account.secret("token")}).data.get("status")
        except PublishError:
            raise
        if st == "PUBLISHED":
            return self.result(None, None, phase="uncertain", container=cid, recovered=True, publication_observed=True)
        if st in ("ERROR", "EXPIRED"):
            return Absent(f"container {cid} ended as {st}; it was never published")
        return None
