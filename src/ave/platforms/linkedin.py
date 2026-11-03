"""LinkedIn Posts API. Tier 2, member posting first. UNVERIFIED live; documentation-derived
(https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api).

  POST /rest/images?action=initializeUpload      {"initializeUploadRequest": {"owner": author}}  -> value.uploadUrl, value.image (urn)
  PUT  <uploadUrl>                               image bytes
  POST /rest/documents?action=initializeUpload   same for a PDF "carousel" (render the slides to a PDF deliverable BEFORE review)
  POST /rest/posts                               author, commentary, visibility, distribution, content{media | multiImage}, lifecycleState (FINAL)
                                                 -> 201 with the new post's urn in the `x-restli-id` header and an empty body

Headers on every call: `Authorization: Bearer`, `Linkedin-Version: <YYYYMM>` (pinned in config/platforms.yaml `linkedin.api_version`; each
version sunsets after about a year, so the contract test is rerun monthly), `X-Restli-Protocol-Version: 2.0.0`.
Member posting (`w_member_social`) is self-serve; a Company Page needs `w_organization_social` and the Community Management API approval:
use an `urn:li:organization:<id>` author only once that is granted. Commentary <= 3000 characters; reserved "little text" characters are
escaped. Visibility: `public` posts to the feed; `unlisted` to connections; anything else (including the queue default `private`) is refused before upload. Credentials: secret `linkedin_token@<ref>`, public `credentials.author` (the author urn).
"""

from __future__ import annotations

import re
from urllib.parse import quote
from pathlib import Path
from typing import Callable

from ..publish.adapters.base import PublishError, Uncertain
from .base import PlatformAdapter, compose_text, item_files, media_type, mime_of

API = "https://api.linkedin.com/rest"
_RESERVED = re.compile(r"([\\|{}@\[\]()<>*_~])")
_HASH = re.compile(r"#(?![A-Za-z0-9])")


def escape_commentary(text: str) -> str:
    """Escape the characters LinkedIn's "little text" format treats as markup (U). `#word` stays a plain hashtag."""
    return _HASH.sub(r"\\#", _RESERVED.sub(r"\\\1", text))


class LinkedInAdapter(PlatformAdapter):
    platform = "linkedin"
    capabilities = frozenset({"text", "image", "carousel", "document"})
    required = ("token", "author")

    def _headers(self, **extra) -> dict:
        return {"Authorization": f"Bearer {self.account.secret('token')}", "Linkedin-Version": str(self.limit("api_version", default="202610")),
                "X-Restli-Protocol-Version": "2.0.0", **extra}

    def preflight(self, item: dict, video: Path) -> list[str]:
        p = self.bound_problems(item) + self.alt_problems(item)
        if item.get('visibility') not in ('public','unlisted'):
            p.append('linkedin: private creation is unsupported; keep a local draft or manual package')
        if item.get('visibility') == 'unlisted' and str(self.account.credential('author') or '').startswith('urn:li:organization:'):
            p.append('linkedin: organization posts require public visibility')
        if item.get("media_type") in ("image","carousel") and not set(self.account.connection.get("scopes",[])) & {"rw_ads","w_organization_social","w_power_creators"}:
            p.append("linkedin: image readiness requires a recorded image-read-capable grant; w_member_social alone is write-only. Connect the appropriate product or use manual delivery")
        destination=self.account.connection.get("destination_id")
        if destination and destination!=self.account.credential("author"): p.append("linkedin: connection destination differs from configured author")
        text = compose_text(item)
        limit = int(self.limit("post", "caption_max_chars", default=3000))
        if len(text) > limit:
            p.append(f"linkedin: commentary is {len(text)} characters, max {limit}")
        return p

    def _verify(self) -> dict:
        d = self._send("verify", "GET", "https://api.linkedin.com/v2/userinfo", headers=self._headers()).data
        return {"sub": d.get("sub"), "name": d.get("name"), "checked":False, "note":"OIDC identity does not establish author or organization posting rights"}

    def _upload(self, kind: str, path: Path, author: str) -> str:
        """kind: images | documents. Returns the asset urn."""
        d = self._send(f"{kind} initializeUpload", "POST", f"{API}/{kind}", params={"action": "initializeUpload"}, headers=self._headers(),
                       json={"initializeUploadRequest": {"owner": author}}).data.get("value") or {}
        url, urn = d.get("uploadUrl"), d.get("image") or d.get("document")
        if not url or not urn:
            raise PublishError(f"linkedin {kind} initializeUpload returned no upload url; nothing was posted")
        self._send(f"{kind} upload", "PUT", url, headers={"Authorization": f"Bearer {self.account.secret('token')}", "Content-Type": mime_of(path)},
                   data=Path(path).read_bytes(), timeout=300)
        for _ in range(20):
            status=self._send(f"{kind} readiness","GET",f"{API}/{kind}/{quote(urn,safe='')}",headers=self._headers()).data.get("status")
            if status=="AVAILABLE": return urn
            if status in ("PROCESSING_FAILED","WAITING_UPLOAD"): raise PublishError(f"linkedin {kind} not available: {status}; no post created")
            self.sleep(2)
        raise PublishError(f"linkedin {kind} readiness not confirmed; no post created")

    def publish(self, item: dict, video: Path, progress: Callable[[dict], None]) -> dict:
        problems = self.preflight(item, video) + self.cred_problems()
        if problems:
            raise PublishError("; ".join(problems))
        author = self.account.credential("author")
        files = item_files(item, video) if media_type(item) in ("image", "carousel", "document") else []
        alts = item.get("alt_texts") or []
        content = None
        urns: list[str] = []
        if files and all(Path(f).suffix.lower() == ".pdf" for f in files):
            urn = self._upload("documents", files[0], author)
            urns = [urn]
            content = {"media": {"title": (item.get("title") or "Document")[:200], "id": urn}}
        elif files:
            for i, f in enumerate(files):
                urns.append(self._upload("images", f, author))
                progress({"assets": list(urns), "phase": "uploading"})
            imgs = [{"id": u, "altText": alts[i] if i < len(alts) else ""} for i, u in enumerate(urns)]
            content = {"media": imgs[0]} if len(imgs) == 1 else {"multiImage": {"images": imgs}}
        vis = item.get("visibility") or "private"
        body = {"author": author, "commentary": escape_commentary(compose_text(item)),
                "visibility": "CONNECTIONS" if vis == "unlisted" else "PUBLIC",
                "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
                "lifecycleState": "PUBLISHED", "isReshareDisabledByAuthor": False}
        if content:
            body["content"] = content
        progress({"phase": "creating", "assets": urns})
        r = self._send("create post", "POST", f"{API}/posts", final=True, headers=self._headers(), json=body)
        urn = r.headers.get("x-restli-id") or r.data.get("id")
        if not urn:
            raise Uncertain("linkedin returned no post id; the post may or may not be live")
        return self.result(urn, f"https://www.linkedin.com/feed/update/{urn}/" if urn else None, live=body["lifecycleState"] == "PUBLISHED",
                           lifecycle=body["lifecycleState"], assets=urns)
