"""Facebook Page: photo and multi-photo ("carousel") posts on top of the repo's video adapter (spec 4.2). Tier 1.

Video items are delegated to `FacebookPublisher` unchanged (Reels / chunked /videos), bound to the account through `secret_ns`.
Photo flow (documentation-derived, UNVERIFIED live; https://developers.facebook.com/docs/graph-api/reference/page/photos/):
  one photo   POST /{page}/photos   multipart `source` + caption + published=true|false                         (FINAL step)
  several     POST /{page}/photos   published=false, one per image -> ids                                       (not final)
              POST /{page}/feed     message + attached_media[n]={"media_fbid": id} + published / scheduled_publish_time   (FINAL step)

Safety choice kept from facebook.py: a post goes live ONLY when the item's visibility is "public" or a schedule is set; anything else is
created unpublished. Page token needs `pages_manage_posts`; secrets `facebook_page_token@<ref>` and (or public `credentials.page_id`)
`facebook_page_id@<ref>`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from ..publish import safety
from ..publish.adapters.base import Uncertain, PublishError, parse_schedule, platform_schedule
from ..publish.adapters.facebook import TIMEOUT, FacebookPublisher
from .accounts import Account
from .base import AccountBound, AuthError, MediaHost, QuotaError, compose_text, item_files, media_type, mime_of


class FacebookPageAdapter(AccountBound, FacebookPublisher):
    platform = "facebook"
    capabilities = frozenset({"video", "image", "carousel"})
    required = ("page_token", "page_id")
    carries_images = True

    def __init__(self, account: Account, *, http=None, host: MediaHost | None = None, now=None, platforms_cfg=None, sleep=None):
        FacebookPublisher.__init__(self, session=http, secret_ns=account.cred_ref if account.bound else None, platforms_cfg=platforms_cfg, now=now)
        self._bind(account, host, sleep)

    # the account supplies the page id (public id in the YAML, else the keychain); the token is always a per-account secret
    def _page(self) -> str:
        return self.account.credential("page_id") or ""

    def _token(self) -> str:
        return self.account.secret("page_token") or ""

    def _verify(self) -> dict:
        d = self._call("verify", False, lambda: self.http.get(f"{self._base()}/{self._page()}", params={"fields": "id,name", "access_token": self._token()}, timeout=TIMEOUT))
        if str(d.get("id"))!=str(self._page()): raise PublishError("Facebook Page identity mismatch")
        return {"id": d.get("id"), "name": d.get("name"), "page_tasks_verified":False}

    def _call(self, phase, final, fn):
        """FacebookPublisher._call, with auth and quota refusals told apart so the scheduler can classify them (spec 6.5)."""
        try:
            return FacebookPublisher._call(self, phase, final, fn)
        except (AuthError,QuotaError):
            raise
        except PublishError as e:
            m = str(e)
            if "HTTP 401" in m or "(code 190)" in m:
                raise AuthError(m)
            if "HTTP 429" in m or any(f"(code {c})" in m for c in (4, 17, 32, 613)):
                raise QuotaError(m)
            raise

    # ---- validation (pure)
    def preflight(self, item: dict, video: Path) -> list[str]:
        p = self.bound_problems(item)
        if media_type(item) == "video":
            return p + FacebookPublisher.preflight(self, item, video)
        p += self.alt_problems(item)
        text = compose_text(item)
        if len(text) > self.f.get("description_max_chars", 5000):
            p.append("facebook: text too long")
        p += self.check_schedule(item, self.f.get("schedule_min_minutes", 10), self.f.get("schedule_max_days", 75))
        return p

    # ---- sending
    def publish(self, item: dict, video: Path, progress: Callable[[dict], None]) -> dict:
        if media_type(item) == "video":
            out = FacebookPublisher.publish(self, item, video, progress)
            return safety.redact_obj({**out, "platform": "facebook", "account": self.account.id})
        problems = self.cred_problems()
        if problems:
            raise PublishError("; ".join(problems))
        files = item_files(item, video)
        alts = item.get("alt_texts") or []
        if media_type(item) == "image" or len(files) == 1:
            return self._photo(item, files[0], alts[0] if alts else "", progress)
        return self._multi(item, files, alts, progress)

    def _form(self, item: dict, **extra) -> dict:
        form = {"access_token": self._token(), **extra}
        if platform_schedule(item):
            form.update(published="false", scheduled_publish_time=int(parse_schedule(item["schedule"]).timestamp()))
        else:
            form["published"] = "true" if self._live(item) else "false"
        return form

    @staticmethod
    def _file_part(f: Path):
        return {"source": (Path(f).name, Path(f).read_bytes(), mime_of(f))}

    def _photo(self, item, f, alt, progress):
        page = self._page()
        form = self._form(item, caption=compose_text(item), **({"alt_text_custom": alt} if alt else {}))
        progress({"phase": "creating"})
        d = self._call("photo", True, lambda: self.http.post(f"{self._base()}/{page}/photos", data=form, files=self._file_part(f), timeout=TIMEOUT))
        post_id = d.get("post_id")
        if not post_id: raise Uncertain('facebook returned no publication id; photo id is not post identity')
        return self.result(post_id, f"https://www.facebook.com/{post_id}" if post_id else None, kind="photo", phase="scheduled" if platform_schedule(item) else "published" if self._live(item) else "draft", live=self._live(item),
                           published_requested=form["published"])

    def _multi(self, item, files, alts, progress):
        page, tok = self._page(), self._token()
        ids = []
        for i, f in enumerate(files):
            form = {"access_token": tok, "published": "false", "temporary": "true", **({"alt_text_custom": alts[i]} if i < len(alts) and alts[i] else {})}
            d = self._call(f"photo {i + 1}/{len(files)}", False, lambda f=f, form=form: self.http.post(
                f"{self._base()}/{page}/photos", data=form, files=self._file_part(f), timeout=TIMEOUT))
            if not d.get("id"):
                raise PublishError("facebook photo upload returned no id; nothing was posted")
            ids.append(d["id"])
            progress({"photo_ids": list(ids), "phase": "uploading"})
        form = self._form(item, message=compose_text(item))
        for n, pid in enumerate(ids):
            form[f"attached_media[{n}]"] = json.dumps({"media_fbid": pid})
        progress({"phase": "creating"})
        d = self._call("feed post", True, lambda: self.http.post(f"{self._base()}/{page}/feed", data=form, timeout=TIMEOUT))
        post_id = d.get("id")
        if not post_id: raise Uncertain('facebook returned no post id')
        return self.result(post_id, f"https://www.facebook.com/{post_id}" if post_id else None, kind="carousel", phase="scheduled" if platform_schedule(item) else "published" if self._live(item) else "draft", photo_ids=ids,
                           live=self._live(item), published_requested=form["published"])

    def recover(self, item: dict, record: dict):
        if media_type(item) == "video":
            return FacebookPublisher.recover(self, item, record)
        return None            # no idempotency key for photo posts: a human checks the Page, then `ave studio resolve`
