"""Pinterest API v5. Tier 2. UNVERIFIED live; documentation-derived (https://developers.pinterest.com/docs/api/v5/pins-create/).

  POST /v5/pins   {board_id, title, description, link, alt_text, media_source}                              (the only, FINAL step)
      media_source = {"source_type": "image_base64", "content_type": "image/jpeg", "data": <base64>}        one image
                   | {"source_type": "multiple_image_base64", "items": [{title, description, link, content_type, data}], "index": 0}   carousel pin

Base64 bodies mean NO media host is needed. A "trial access" app can only see its pins in the sandbox: set `credentials.sandbox: true` on the
account to use api-sandbox.pinterest.com. Images are best at 2:3 (the `2x3` format); title <= 100, description <= 800, alt text <= 500.
Credentials: secret `pinterest_token@<ref>`, public `credentials.board_id`. A pin has no idempotency key, so `recover` cannot prove
anything and a human checks the board.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Callable

from ..publish.adapters.base import PublishError, Uncertain
from .base import PlatformAdapter, compose_text, item_files, mime_of


class PinterestAdapter(PlatformAdapter):
    platform = "pinterest"
    capabilities = frozenset({"image", "carousel", "pin"})
    required = ("token", "board_id")

    @property
    def base(self) -> str:
        sandbox = str(self.account.public.get("sandbox", "")).lower() in ("true", "1", "yes")
        return "https://api-sandbox.pinterest.com/v5" if sandbox else "https://api.pinterest.com/v5"

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self.account.secret('token')}"}

    def preflight(self, item: dict, video: Path) -> list[str]:
        p = self.bound_problems(item) + self.alt_problems(item)
        tmax, dmax = int(self.limit("post", "title_max_chars", default=100)), int(self.limit("post", "caption_max_chars", default=800))
        if len(item.get("title") or "") > tmax:
            p.append(f"pinterest: title is {len(item['title'])} characters, max {tmax}")
        if len(compose_text(item)) > dmax:
            p.append(f"pinterest: description is {len(compose_text(item))} characters, max {dmax}")
        return p

    def _verify(self) -> dict:
        d = self._send("verify", "GET", f"{self.base}/user_account", headers=self._h()).data
        return {"username": d.get("username"), "account_type": d.get("account_type")}

    def publish(self, item: dict, video: Path, progress: Callable[[dict], None]) -> dict:
        problems = self.bound_problems(item) + self.cred_problems()
        if problems:
            raise PublishError("; ".join(problems))
        files = item_files(item, video)
        alts = item.get("alt_texts") or []
        enc = [{"content_type": mime_of(f), "data": base64.b64encode(Path(f).read_bytes()).decode()} for f in files]
        title, desc, link = (item.get("title") or "")[:100], compose_text(item), item.get("link_url")
        if len(enc) == 1:
            media = {"source_type": "image_base64", **enc[0]}
        else:
            media = {"source_type": "multiple_image_base64", "index": 0,
                     "items": enc if item.get('meta_v')==3 else [{"title": title, "description": desc, **({"link": link} if link else {}), **e} for e in enc]}
        body = {"board_id": self.account.credential("board_id"), "title": title, "description": desc, "media_source": media}
        if link:
            body["link"] = link
        if alts and alts[0]:
            body["alt_text"] = alts[0][:500]
        progress({"phase": "creating"})
        d = self._send("create pin", "POST", f"{self.base}/pins", final=True, headers=self._h(), json=body, timeout=300).data
        pid = d.get("id")
        if not pid:
            raise Uncertain("pinterest returned no pin id; the pin may or may not exist")
        return self.result(pid, f"https://www.pinterest.com/pin/{pid}/", phase="sandbox" if self.base.startswith("https://api-sandbox") else "published", sandbox=self.base.startswith("https://api-sandbox"))
