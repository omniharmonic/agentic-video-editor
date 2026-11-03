"""Bluesky (AT Protocol). Tier 1, documentation-derived; UNVERIFIED live.

Flow (documentation-derived, UNVERIFIED live; https://docs.bsky.app/docs/api/):
  1. POST {pds}/xrpc/com.atproto.server.createSession        {identifier, password(app password)}  -> accessJwt, did, handle
  2. POST {pds}/xrpc/com.atproto.repo.uploadBlob              raw bytes, Content-Type image/*       -> {"blob": {...}}   (one per image)
  3. POST {pds}/xrpc/com.atproto.repo.createRecord            app.bsky.feed.post + embed.images + facets -> {uri, cid}      (FINAL step)

Constraints (U): text <= 300 graphemes, <= 4 images, per-image blob cap ~1 MB (render a smaller deliverable BEFORE review: config
`bluesky.image.max_file_bytes`), alt text mandatory here. Links and hashtags become facets (UTF-8 byte offsets). Video is not implemented.

Crash safety: `createdAt` is generated and saved with `progress()` BEFORE createRecord, so `recover` can list the repo's recent posts and
prove whether that exact record exists (published) or not (Absent).
Credentials: secret `bluesky_app_password@<ref>`; the handle (public) is `credentials.handle` in the account file.
"""

from __future__ import annotations

import re
import hashlib
import json
import uuid
from pathlib import Path
from typing import Callable

from ..publish import safety
from ..publish.adapters.base import Absent, PublishError, Uncertain, probe_image
from .base import PlatformAdapter, compose_text, item_files, media_type, mime_of
from .textlen import graphemes, url_spans, with_margin

DEFAULT_PDS = "https://bsky.social"
_TAG = re.compile(r"(?<![\w&])#([^\s#\d][^\s#]*)")


class BlueskyAdapter(PlatformAdapter):
    platform = "bluesky"
    capabilities = frozenset({"text", "image", "carousel"})
    required = ("app_password", "handle")

    @property
    def pds(self) -> str:
        return str(self.account.public.get("pds") or DEFAULT_PDS).rstrip("/")

    def _xrpc(self, name: str) -> str:
        return f"{self.pds}/xrpc/{name}"

    # ---- validation (pure)
    def preflight(self, item: dict, video: Path) -> list[str]:
        p = self.bound_problems(item) + self.alt_problems(item)
        text = compose_text(item)
        limit = int(self.limit("post", "caption_max_chars", default=300))
        n = graphemes(text)
        if n > with_margin(limit):
            p.append(f"bluesky: text is about {n} graphemes (limit {limit}, checked with a 2% margin; approximate)")
        if not text and media_type(item) == "text":
            p.append("bluesky: empty post")
        return p

    # ---- session
    def _session(self) -> dict:
        if self.account.connection.get('root_revision'):
            token=self.account.secret('access_token')
            if not token:raise PublishError('Connect or renew the Bluesky session in Connections')
            return {'accessJwt':token,'did':self.account.public['did'],'handle':self.account.public['handle']}
        r = self._send("createSession", "POST", self._xrpc("com.atproto.server.createSession"),
                       json={"identifier": self.account.credential("handle"), "password": self.account.secret("app_password")})
        d = r.data
        if not d.get("accessJwt") or not d.get("did"):
            raise PublishError("bluesky createSession returned no session; nothing was posted")
        safety.register_secret(d["accessJwt"])
        safety.register_secret(d.get("refreshJwt"))
        return d

    def _verify(self) -> dict:
        s = self._session()
        return {"handle": s.get("handle"), "did": s.get("did")}

    # ---- sending
    def _facets(self, text: str) -> list[dict]:
        out = [{"index": {"byteStart": a, "byteEnd": b}, "features": [{"$type": "app.bsky.richtext.facet#link", "uri": url}]}
               for a, b, url in url_spans(text)]
        for m in _TAG.finditer(text):
            start = len(text[:m.start()].encode())
            out.append({"index": {"byteStart": start, "byteEnd": start + len(m.group(0).encode())},
                        "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": m.group(1)}]})
        return out

    def publish(self, item: dict, video: Path, progress: Callable[[dict], None]) -> dict:
        problems = self.bound_problems(item) + self.cred_problems()
        if problems:
            raise PublishError("; ".join(problems))
        files = item_files(item, video) if media_type(item) in ("image", "carousel") else []
        text = compose_text(item)
        alts = item.get("alt_texts") or []
        sess = self._session()
        auth = {"Authorization": f"Bearer {sess['accessJwt']}"}
        images = []
        for i, f in enumerate(files):
            blob = self._send(f"uploadBlob {i + 1}/{len(files)}", "POST", self._xrpc("com.atproto.repo.uploadBlob"), data=Path(f).read_bytes(),
                              headers={**auth, "Content-Type": mime_of(f)}, timeout=120).data.get("blob")
            if not blob:
                raise PublishError("bluesky uploadBlob returned no blob; nothing was posted")
            info = probe_image(f)
            images.append({"alt": alts[i] if i < len(alts) else "", "image": blob,
                           "aspectRatio": {"width": info["width"], "height": info["height"]}})
        created = self.now().strftime("%Y-%m-%dT%H:%M:%S.000Z")
        record: dict = {"$type": "app.bsky.feed.post", "text": text, "createdAt": created, "langs": item.get("langs") or ["en"]}
        facets = self._facets(text)
        if facets:
            record["facets"] = facets
        if images:
            record["embed"] = {"$type": "app.bsky.embed.images", "images": images}
        rkey=uuid.uuid4().hex
        digest=hashlib.sha256(safety.canonical(record).encode()).hexdigest()
        progress({"phase": "creating", "created_at": created, "did": sess["did"], 'rkey':rkey,'record_digest':digest})
        r = self._send("createRecord", "POST", self._xrpc("com.atproto.repo.createRecord"), final=True, headers=auth,
                       json={"repo": sess["did"], "collection": "app.bsky.feed.post", "rkey":rkey, "record": record})
        uri = r.data.get("uri")
        if not uri:
            raise Uncertain("bluesky createRecord returned no uri; the post may or may not be live")
        return self.result(uri, self._web_url(sess.get("handle") or self.account.credential("handle"), uri), cid=r.data.get("cid"))

    @staticmethod
    def _web_url(handle: str | None, uri: str) -> str | None:
        rkey = uri.rsplit("/", 1)[-1]
        return f"https://bsky.app/profile/{handle}/post/{rkey}" if handle and rkey else None

    def recover(self, item: dict, record: dict):
        if not all(record.get(k) for k in ('did','rkey','record_digest')): return None
        if self.cred_problems(): return None
        try:
            sess=self._session()
            if sess['did'] != record['did']: return None
            d=self._send('getRecord','GET',self._xrpc('com.atproto.repo.getRecord'),
                params={'repo':record['did'],'collection':'app.bsky.feed.post','rkey':record['rkey']},
                headers={'Authorization':f"Bearer {sess['accessJwt']}"}).data
        except PublishError:
            return None  # missing/failed read cannot prove that create was not accepted
        digest=hashlib.sha256(safety.canonical(d.get('value') or {}).encode()).hexdigest()
        expected=f"at://{record['did']}/app.bsky.feed.post/{record['rkey']}"
        if d.get('uri') != expected or digest != record['record_digest']: return None
        return self.result(expected,self._web_url(sess.get('handle'),expected),cid=d.get('cid'),recovered=True)
