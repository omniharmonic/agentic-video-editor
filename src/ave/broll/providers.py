"""Stock providers for the studio: Pexels, Pixabay, Unsplash, Coverr and the studio's own library (spec 5.1).

EVERY provider fact in this file (endpoints, parameter names, response shapes, auth schemes) is UNVERIFIED: written from the public
documentation as remembered, exercised only against the fixtures in tests/data_studio/broll/, never against a live API. Expect to fix
field names on first contact. Pexels/Pixabay VIDEO parsing reuses the existing, equally unverified `assets.providers` classes.

Rules honoured here: API keys come from env (`PEXELS_API_KEY`, `PIXABAY_API_KEY`, `UNSPLASH_ACCESS_KEY`, `COVERR_API_KEY`) or the OS keychain
(`safety.get_secret("<provider>_api_key")`), never from a file, and are never cached, logged, or stored in a URL we keep. Search responses
are cached 24 h (Pixabay's terms). Nothing here downloads a full-size file: that is `broll.fetch.download`, after a named human pick.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..assets import licenses
from ..assets import providers as legacy
from ..publish import safety
from . import terms

CACHE_TTL_S = 24 * 3600
TIMEOUT_S = 30
KINDS = ("photo", "video")


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class Candidate:
    provider: str
    asset_id: str
    kind: str                       # "photo" | "video"
    page_url: str
    preview_url: str
    download_url: str
    w: int
    h: int
    duration: float | None = None
    author: str | None = None
    author_url: str | None = None
    tags: tuple[str, ...] = ()
    title: str = ""
    attribution_required: bool = False
    attribution_text: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.asset_id}"

    def as_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["tags"] = list(self.tags)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Candidate":
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{**{k: v for k, v in d.items() if k in names}, "tags": tuple(d.get("tags") or ())})


class StockProvider(Protocol):
    name: str
    kinds: frozenset
    env_key: str

    def search(self, query: str, *, kind: str, orientation: str | None = None, per_page: int = 15, page: int = 1) -> list[Candidate]: ...
    def license_info(self, c: Candidate) -> dict: ...
    def on_download(self, c: Candidate) -> None: ...


def _slug_words(url: str) -> list[str]:
    slug = re.sub(r"-\d+/?$", "", url.rstrip("/").rsplit("/", 1)[-1])
    return [t for t in slug.split("-") if t and not t.isdigit()]


def _orientation_ok(w: int, h: int, orientation: str | None) -> bool:
    if not orientation or not w or not h:
        return True
    r = w / h
    return {"landscape": r > 1.15, "portrait": r < 0.87, "square": 0.87 <= r <= 1.15, "squarish": 0.87 <= r <= 1.15}.get(orientation, True)


class HttpProvider:
    """Shared plumbing: key lookup, 24 h response cache, status handling. Subclasses implement `_search_photo` / `_search_video`."""
    name = ""
    env_key = ""
    kinds: frozenset = frozenset()

    def __init__(self, api_key: str | None = None, http=None, cache_dir: Path | str | None = None, now=time.time, ttl_s: int = CACHE_TTL_S):
        self._api_key = api_key
        if http is None:
            import requests
            http = requests.Session()
        self.http, self.now, self.ttl_s = http, now, ttl_s
        self.cache_dir = Path(cache_dir) if cache_dir else legacy.default_cache_dir()

    # ---- keys
    def key(self) -> str:
        k = self._api_key or os.environ.get(self.env_key) or safety.get_secret(f"{self.name}_api_key")
        if not k:
            raise ProviderError(f"{self.env_key} is not set. Get a free key from {self.name} and export it (or store it in the keychain "
                                f"as {self.name}_api_key); it is never written to a file.")
        safety.register_secret(k)
        return k

    # ---- http
    def _scrub(self, obj: Any) -> Any:
        """Remove API-key query strings and, defensively, the literal key value from anything we persist."""
        obj = licenses.redact(obj)
        k = self._api_key or os.environ.get(self.env_key)
        if k and len(k) >= 6:
            return json.loads(json.dumps(obj).replace(k, "REDACTED"))
        return obj

    def _cache_path(self, url: str, params: dict) -> Path:
        safe = {k: v for k, v in sorted(params.items()) if k not in ("key", "api_key")}
        h = hashlib.sha256(json.dumps([url, safe], sort_keys=True).encode()).hexdigest()[:32]
        return self.cache_dir / self.name / f"{h}.json"

    def _get_json(self, url: str, params: dict, headers: dict | None = None) -> dict:
        path = self._cache_path(url, params)
        if path.exists():
            try:
                c = json.loads(path.read_text())
                if self.now() - c["fetched_at"] < self.ttl_s:
                    return c["response"]
            except (ValueError, KeyError):
                pass                                   # corrupt cache entry: refetch
        r = self.http.get(url, params=params, headers=headers or {}, timeout=TIMEOUT_S)
        self._check(r)
        data = r.json()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"fetched_at": self.now(), "response": self._scrub(data)}))
        tmp.replace(path)
        return data

    def _check(self, r) -> None:
        sc = r.status_code
        if sc == 429:
            raise ProviderError(f"{self.name} rate limit reached; wait and retry (cached queries still work)")
        if sc in (401, 403):
            raise ProviderError(f"{self.name} rejected the API key (HTTP {sc}); check {self.env_key}")
        if sc >= 400:
            raise ProviderError(f"{self.name} answered HTTP {sc}")

    # ---- interface
    def search(self, query: str, *, kind: str, orientation: str | None = None, per_page: int = 15, page: int = 1) -> list[Candidate]:
        if kind not in self.kinds:
            raise ProviderError(f"{self.name} does not offer {kind} (it has: {', '.join(sorted(self.kinds))})")
        fn = self._search_photo if kind == "photo" else self._search_video
        return [c for c in fn(query, orientation, per_page, page) if c]

    def _search_photo(self, query, orientation, per_page, page) -> list[Candidate]:
        raise NotImplementedError

    def _search_video(self, query, orientation, per_page, page) -> list[Candidate]:
        raise NotImplementedError

    def license_info(self, c: Candidate) -> dict:
        t = terms.terms_for(self.name)
        return {"license": t["license"], "license_url": t["license_url"], "license_text": t["text"], "restrictions": list(t["restrictions"]),
                "attribution": {"required": t["attribution_required"], "text": c.attribution_text},
                "terms_snapshot": {"date": t["snapshot_date"], "verified": t["verified"]}}

    def on_download(self, c: Candidate) -> None:
        return None

    def _from_legacy(self, d: dict, kind: str) -> Candidate:
        """A dict from `assets.providers.<Provider>._parse` (video) as a Candidate."""
        c = Candidate(provider=self.name, asset_id=d["asset_id"], kind=kind, page_url=d["page_url"], preview_url=d.get("thumb_url") or "",
                      download_url=d["download_url"], w=int(d["width"]), h=int(d["height"]), duration=d.get("duration"),
                      author=d.get("author"), author_url=d.get("author_url"), tags=tuple(d.get("tags") or ()), title=d.get("title") or "",
                      extra={k: d[k] for k in ("fps", "type") if d.get(k) is not None})
        return dataclasses.replace(c, attribution_text=self.attribution(c))

    def attribution(self, c: Candidate) -> str:
        raise NotImplementedError


class Pexels(HttpProvider):
    name, env_key, kinds = "pexels", "PEXELS_API_KEY", frozenset({"photo", "video"})
    PHOTO_URL = "https://api.pexels.com/v1/search"            # (U) documented response: {photos:[{id,width,height,url,photographer,...,src:{...},alt}]}
    VIDEO_URL = "https://api.pexels.com/videos/search"        # (U) same as assets.providers.Pexels

    def _search_photo(self, query, orientation, per_page, page):
        params = {"query": query, "per_page": per_page, "page": page}
        if orientation:
            params["orientation"] = orientation
        data = self._get_json(self.PHOTO_URL, params, {"Authorization": self.key()})
        return [self._parse_photo(p) for p in data.get("photos", [])]

    def _search_video(self, query, orientation, per_page, page):
        params = {"query": query, "per_page": per_page, "page": page}
        if orientation:
            params["orientation"] = orientation
        data = self._get_json(self.VIDEO_URL, params, {"Authorization": self.key()})
        return [self._from_legacy(d, "video") for d in (legacy.Pexels._parse(v) for v in data.get("videos", [])) if d]

    def _parse_photo(self, p: dict) -> Candidate | None:
        src = p.get("src") or {}
        dl = src.get("original") or src.get("large2x") or src.get("large")
        if not dl or not p.get("id"):
            return None
        words = _slug_words(p.get("url", ""))
        c = Candidate(provider="pexels", asset_id=str(p["id"]), kind="photo", page_url=p.get("url", ""),
                      preview_url=src.get("medium") or src.get("small") or dl, download_url=dl, w=int(p.get("width") or 0),
                      h=int(p.get("height") or 0), author=p.get("photographer"), author_url=p.get("photographer_url"), tags=tuple(words),
                      title=(p.get("alt") or " ".join(words)), extra={"alt": p["alt"]} if p.get("alt") else {})
        return dataclasses.replace(c, attribution_text=self.attribution(c))

    def attribution(self, c):
        what = "Video" if c.kind == "video" else "Photo"
        return f"{what} by {c.author or 'a Pexels contributor'} on Pexels ({c.page_url})"


class Pixabay(HttpProvider):
    name, env_key, kinds = "pixabay", "PIXABAY_API_KEY", frozenset({"photo", "video"})
    PHOTO_URL = "https://pixabay.com/api/"                    # (U) {hits:[{id,pageURL,tags,previewURL,webformatURL,largeImageURL,imageWidth,...,user,user_id}]}
    VIDEO_URL = "https://pixabay.com/api/videos/"
    ORIENT = {"landscape": "horizontal", "portrait": "vertical"}

    def _params(self, query, per_page, page):
        return {"key": self.key(), "q": query, "per_page": max(3, per_page), "page": page, "safesearch": "true"}

    def _search_photo(self, query, orientation, per_page, page):
        params = {**self._params(query, per_page, page), "image_type": "photo"}
        if orientation in self.ORIENT:
            params["orientation"] = self.ORIENT[orientation]
        data = self._get_json(self.PHOTO_URL, params)
        out = []
        for h in data.get("hits", []):
            dl = h.get("largeImageURL") or h.get("webformatURL")
            if not dl or not h.get("id"):
                continue
            tags = tuple(t.strip() for t in (h.get("tags") or "").split(",") if t.strip())
            c = Candidate(provider="pixabay", asset_id=str(h["id"]), kind="photo", page_url=h.get("pageURL", ""),
                          preview_url=h.get("webformatURL") or h.get("previewURL") or dl, download_url=dl,
                          w=int(h.get("imageWidth") or h.get("webformatWidth") or 0), h=int(h.get("imageHeight") or h.get("webformatHeight") or 0),
                          author=h.get("user"), author_url=(f"https://pixabay.com/users/{h['user']}-{h.get('user_id')}/" if h.get("user") else None),
                          tags=tags, title=", ".join(tags), extra={"download_variant": "largeImageURL"})
            out.append(dataclasses.replace(c, attribution_text=self.attribution(c)))
        return out

    def _search_video(self, query, orientation, per_page, page):
        data = self._get_json(self.VIDEO_URL, self._params(query, per_page, page))
        out = [self._from_legacy(d, "video") for d in (legacy.Pixabay._parse(h) for h in data.get("hits", [])) if d]
        return [c for c in out if _orientation_ok(c.w, c.h, orientation)]

    def attribution(self, c):
        what = "Video" if c.kind == "video" else "Image"
        return f"{what} by {c.author or 'a Pixabay contributor'} from Pixabay ({c.page_url})"


class Unsplash(HttpProvider):
    name, env_key, kinds = "unsplash", "UNSPLASH_ACCESS_KEY", frozenset({"photo"})
    URL = "https://api.unsplash.com/search/photos"            # (U) header "Authorization: Client-ID <key>"; {results:[{id,width,height,urls,links,user,...}]}
    ORIENT = {"landscape": "landscape", "portrait": "portrait", "square": "squarish", "squarish": "squarish"}
    UTM = "utm_source=ave_studio&utm_medium=referral"

    def _headers(self) -> dict:
        return {"Authorization": f"Client-ID {self.key()}", "Accept-Version": "v1"}

    def _search_photo(self, query, orientation, per_page, page):
        params = {"query": query, "per_page": min(30, per_page), "page": page}
        if orientation in self.ORIENT:
            params["orientation"] = self.ORIENT[orientation]
        data = self._get_json(self.URL, params, self._headers())
        out = []
        for r in data.get("results", []):
            urls, links, user = r.get("urls") or {}, r.get("links") or {}, r.get("user") or {}
            dl = urls.get("full") or urls.get("regular") or urls.get("raw")
            if not dl or not r.get("id"):
                continue
            ulinks = user.get("links") or {}
            tags = tuple(t["title"] for t in (r.get("tags") or []) if isinstance(t, dict) and t.get("title"))
            alt = r.get("alt_description") or r.get("description") or ""
            extra = {"download_location": links.get("download_location")}
            if alt:
                extra["alt"] = alt
            c = Candidate(provider="unsplash", asset_id=str(r["id"]), kind="photo",
                          page_url=(links.get("html") or f"https://unsplash.com/photos/{r['id']}"),
                          preview_url=urls.get("small") or urls.get("thumb") or dl, download_url=dl, w=int(r.get("width") or 0),
                          h=int(r.get("height") or 0), author=user.get("name"), author_url=ulinks.get("html"), tags=tags,
                          title=alt or " ".join(tags), extra=extra)
            out.append(dataclasses.replace(c, attribution_required=True, attribution_text=self.attribution(c)))
        return out

    def attribution(self, c):
        who = c.author or "an Unsplash photographer"
        return f"Photo by {who} on Unsplash ({c.page_url}?{self.UTM})"

    def on_download(self, c: Candidate) -> None:
        """Download-tracking ping the Unsplash API guidelines ask for (U). Called after a human picked and the bytes are safe."""
        loc = (c.extra or {}).get("download_location")
        if not loc:
            raise ProviderError("no download_location on the candidate; the tracking ping was not sent")
        r = self.http.get(loc, headers=self._headers(), timeout=TIMEOUT_S)
        self._check(r)

    def license_info(self, c):
        info = super().license_info(c)
        info["attribution"]["required"] = True
        return info


class Coverr(HttpProvider):
    name, env_key, kinds = "coverr", "COVERR_API_KEY", frozenset({"video"})
    URL = "https://api.coverr.co/videos"                      # (U) header "Authorization: Bearer <key>"; {hits:[{id,title,tags,duration,urls:{...},...}]}

    def _search_video(self, query, orientation, per_page, page):
        params = {"query": query, "page_size": per_page, "page": page, "urls": "true"}
        data = self._get_json(self.URL, params, {"Authorization": f"Bearer {self.key()}"})
        out = []
        for h in data.get("hits", []):
            urls = h.get("urls") or {}
            dl = urls.get("mp4_download") or urls.get("mp4")
            if not dl or not h.get("id"):
                continue
            w, hh = int(h.get("max_width") or 0), int(h.get("max_height") or 0)
            if h.get("is_vertical") and w > hh:
                w, hh = hh, w
            if not _orientation_ok(w, hh, orientation):
                continue
            tags = tuple(str(t) for t in (h.get("tags") or []))
            c = Candidate(provider="coverr", asset_id=str(h["id"]), kind="video", page_url=h.get("url") or f"https://coverr.co/videos/{h['id']}",
                          preview_url=h.get("poster") or h.get("thumbnail") or urls.get("mp4_preview") or dl, download_url=dl, w=w, h=hh,
                          duration=h.get("duration"), author=h.get("author") or None, author_url=None, tags=tags,
                          title=h.get("title") or " ".join(tags), extra={"preview_video": urls.get("mp4_preview")})
            out.append(dataclasses.replace(c, attribution_text=self.attribution(c)))
        return out

    def attribution(self, c):
        return f"Video from Coverr ({c.page_url})"


class Local:
    """The studio's own library as a provider: searching it costs nothing and 'downloading' a hit is a no-op (it is already here)."""
    name, env_key, kinds = "local", "", frozenset({"photo", "video"})
    http = None

    def __init__(self, sp):
        self.sp = sp

    def search(self, query: str, *, kind: str, orientation: str | None = None, per_page: int = 15, page: int = 1) -> list[Candidate]:
        from ..library import index
        want = {"photo": "image", "video": "video"}[kind]
        rows = index.search(self.sp, query, kind=want, limit=10_000)
        rows = [r for r in rows if _orientation_ok(r.get("w") or 0, r.get("h") or 0, orientation)]
        out = []
        for r in rows[(page - 1) * per_page: page * per_page]:
            att = (r.get("license") or {}).get("attribution") or {}
            out.append(Candidate(provider="local", asset_id=r["id"], kind=kind, page_url=f"library:{r['id']}", preview_url="/files/" + r["file"],
                                 download_url=f"library:{r['id']}", w=r.get("w") or 0, h=r.get("h") or 0, duration=r.get("duration"),
                                 author=r.get("added_by"), tags=tuple(r.get("tags") or ()), title=r.get("alt") or r["id"],
                                 attribution_required=bool(att.get("required")), attribution_text=att.get("text") or "",
                                 extra={"license": (r.get("license") or {}).get("summary"), "alt": r.get("alt") or ""}))
        return out

    def license_info(self, c: Candidate) -> dict:
        return {"license": (c.extra or {}).get("license") or "library", "attribution": {"required": c.attribution_required, "text": c.attribution_text}}

    def on_download(self, c: Candidate) -> None:
        return None


PROVIDER_CLASSES = {"pexels": Pexels, "pixabay": Pixabay, "unsplash": Unsplash, "coverr": Coverr}
ALL_NAMES = (*PROVIDER_CLASSES, "local")


def make_providers(sp=None, names=None, *, http=None, cache_dir=None, now=time.time) -> dict:
    """Instances for `names` (default all). `http` is injected into every network provider (tests pass FakeHttp). Keys are looked up lazily,
    so constructing a provider never fails for a missing key; searching it does, naming the env variable."""
    out = {}
    for n in names or ALL_NAMES:
        if n == "local":
            if sp is None:
                raise ProviderError("the local provider needs a studio")
            out[n] = Local(sp)
        elif n in PROVIDER_CLASSES:
            out[n] = PROVIDER_CLASSES[n](http=http, cache_dir=cache_dir, now=now)
        else:
            raise ProviderError(f"unknown provider {n!r}; known: {', '.join(ALL_NAMES)}")
    return out
