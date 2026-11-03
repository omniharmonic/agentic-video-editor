"""X (Twitter) API v2. Tier 2. UNVERIFIED live; documentation-derived (https://docs.x.com/x-api/).

  POST /2/media/upload/initialize        {media_type, total_bytes, media_category: tweet_image}   -> data.id          (per image)
  POST /2/media/upload/{id}/append       multipart media + segment_index
  POST /2/media/upload/{id}/finalize
  POST /2/media/metadata                 {id, metadata: {alt_text: {text}}}
  POST /2/tweets                         {text, media: {media_ids}}                                (FINAL) -> 201 data.id

OAuth 2.0 user-context bearer token (secret `x_token@<ref>`; the PKCE consent is a human act, `ave studio account auth`). Weighted length
280 (URL = 23, emoji and CJK = 2; checked with a 2 % margin), images <= 5 MB, <= 4 per post. Access and pricing are in flux: the cost per
post is a config number (`x.rate.est_cost_usd_per_post`) that every result carries so the scheduler can log it, and `x.rate.max_posts_per_day`
caps spend. Recovery never uses text/time matching as proof of identity; an unknown create remains uncertain.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..publish.adapters.base import PublishError, Uncertain
from ..studio.clock import parse_ts
from .base import PlatformAdapter, compose_text, item_files, media_type, mime_of
from .textlen import with_margin, x_weighted_len

API = "https://api.x.com/2"


class XAdapter(PlatformAdapter):
    platform = "x"
    capabilities = frozenset({"text", "image", "carousel"})
    required = ("token",)

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self.account.secret('token')}"}

    def _body_error(self, data: dict) -> bool:
        return False                      # X signals failure with an HTTP status; a 2xx body is a success

    def preflight(self, item: dict, video: Path) -> list[str]:
        p = self.bound_problems(item) + self.alt_problems(item)
        text = compose_text(item)
        limit = int(self.limit("post", "caption_max_chars", default=280))
        n = x_weighted_len(text)
        if n > with_margin(limit):
            p.append(f"x: text weighs about {n} (limit {limit}; URLs count 23, emoji and CJK 2; 2% margin; approximate)")
        cap = self.limit("rate", "max_posts_per_day")
        if cap is not None and int(cap) <= 0:
            p.append("x: rate.max_posts_per_day is 0, so posting is switched off")
        return p

    def _verify(self) -> dict:
        d = self._send("verify", "GET", f"{API}/users/me", headers=self._h()).data.get("data") or {}
        return {"id": d.get("id"), "username": d.get("username")}

    def _upload(self, f: Path, alt: str) -> str:
        data = Path(f).read_bytes()
        mid = (self._send("media initialize", "POST", f"{API}/media/upload/initialize", headers=self._h(),
                          json={"media_type": mime_of(f), "total_bytes": len(data), "media_category": "tweet_image"}).data.get("data") or {}).get("id")
        if not mid:
            raise PublishError("x media initialize returned no id; nothing was posted")
        self._send("media append", "POST", f"{API}/media/upload/{mid}/append", headers=self._h(), data={"segment_index": 0},
                   files={"media": (Path(f).name, data, mime_of(f))}, timeout=300)
        finalized=self._send("media finalize", "POST", f"{API}/media/upload/{mid}/finalize", headers=self._h()).data
        info=(finalized.get('data') or finalized).get('processing_info') or {}
        for _ in range(30):
            if not info or info.get('state')=='succeeded': break
            if info.get('state')=='failed': raise PublishError('x media processing failed before post creation')
            self.sleep(min(30,max(1,float(info.get('check_after_secs',2)))))
            d=self._send('media status','GET',f'{API}/media/upload',headers=self._h(),params={'media_id':mid}).data
            info=(d.get('data') or d).get('processing_info') or {}
        else: raise PublishError('x media is still processing; no post was created')
        if alt:
            self._send("media alt text", "POST", f"{API}/media/metadata", headers=self._h(), json={"id": mid, "metadata": {"alt_text": {"text": alt}}})
        return str(mid)

    def publish(self, item: dict, video: Path, progress: Callable[[dict], None]) -> dict:
        problems = self.bound_problems(item) + self.cred_problems()
        if problems:
            raise PublishError("; ".join(problems))
        files = item_files(item, video) if media_type(item) in ("image", "carousel") else []
        alts = item.get("alt_texts") or []
        ids: list[str] = []
        for i, f in enumerate(files):
            ids.append(self._upload(f, alts[i] if i < len(alts) else ""))
            progress({"media_ids": list(ids), "phase": "uploading"})
        body: dict = {"text": compose_text(item)}
        if ids:
            body["media"] = {"media_ids": ids}
        progress({"phase": "creating", "media_ids": ids})
        d = self._send("create post", "POST", f"{API}/tweets", final=True, headers=self._h(), json=body).data.get("data") or {}
        tid = d.get("id")
        if not tid:
            raise Uncertain("x returned no post id; the post may or may not be live")
        return self.result(tid, f"https://x.com/i/status/{tid}", est_cost_usd=self.limit("rate", "est_cost_usd_per_post"), media_ids=ids)

    def recover(self, item: dict, record: dict):
        # Caption/time matches are not an identity proof. Retain uncertainty.
        return record if (record.get('receipt') or {}).get('phase') == 'published' else None
