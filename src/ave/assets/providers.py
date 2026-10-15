"""Stock video providers: Pexels and Pixabay search + download.

Terms we honour (see licenses.PROVIDER_TERMS, UNVERIFIED snapshots):
  * Pixabay: results cached 24 h, files downloaded and hosted by us, never hotlinked.
  * Pexels: credit is appreciated, so every download records ready-to-paste attribution text in the license log.
  * API keys come only from env (PEXELS_API_KEY / PIXABAY_API_KEY), are never cached, logged or put in URLs we store.
Nothing here judges taste: candidates go through `critic` and a human approval before `download` is called.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

from . import licenses

CACHE_TTL_S = 24 * 3600
TIMEOUT_S = 30


class ProviderError(RuntimeError):
    pass


def default_cache_dir() -> Path:
    return Path(os.environ.get("AVE_ASSETS_CACHE", Path.home() / ".cache" / "ave" / "assets"))


def _aspect(w, h) -> str:
    if not w or not h:
        return "unknown"
    r = w / h
    return "landscape" if r > 1.15 else "portrait" if r < 0.87 else "square"


class Provider:
    name = ""
    env_key = ""

    def __init__(self, api_key: str | None = None, http=None, cache_dir: Path | str | None = None, now=time.time,
                 ttl_s: int = CACHE_TTL_S):
        self.api_key = api_key if api_key is not None else os.environ.get(self.env_key)
        if http is None:
            import requests
            http = requests.Session()
        self.http, self.now, self.ttl_s = http, now, ttl_s
        self.cache_dir = Path(cache_dir) if cache_dir else default_cache_dir()

    def _need_key(self) -> str:
        if not self.api_key:
            raise ProviderError(f"{self.env_key} is not set. Get a free key from {self.name} and export it; it is never stored.")
        return self.api_key

    def _cache_path(self, url: str, params: dict) -> Path:
        safe = {k: v for k, v in sorted(params.items()) if k != "key"}  # the key must not influence or leak into the cache
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
                pass  # corrupt cache entry: refetch
        r = self.http.get(url, params=params, headers=headers or {}, timeout=TIMEOUT_S)
        if r.status_code == 429:
            raise ProviderError(f"{self.name} rate limit reached; wait and retry (cached queries still work)")
        if r.status_code in (401, 403):
            raise ProviderError(f"{self.name} rejected the API key (HTTP {r.status_code}); check {self.env_key}")
        r.raise_for_status()
        data = r.json()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"fetched_at": self.now(), "response": licenses.redact(data)}))
        tmp.replace(path)
        return data

    def search(self, query: str, per_page: int = 15, orientation: str | None = None) -> list[dict]:
        raise NotImplementedError

    def license_info(self, cand: dict) -> dict:
        t = licenses.PROVIDER_TERMS[self.name]
        return {"license": t["license"], "license_url": t["license_url"], "license_text": t["text"],
                "restrictions": list(t["restrictions"]),
                "attribution": {"required": t["attribution_required"], "text": self.attribution_text(cand)},
                "terms_snapshot": {"date": t["snapshot_date"], "verified": t["verified"]}}

    def attribution_text(self, cand: dict) -> str:
        raise NotImplementedError


class Pexels(Provider):
    name, env_key = "pexels", "PEXELS_API_KEY"
    URL = "https://api.pexels.com/videos/search"

    def search(self, query, per_page=15, orientation=None):
        params = {"query": query, "per_page": per_page}
        if orientation:
            params["orientation"] = orientation
        data = self._get_json(self.URL, params, {"Authorization": self._need_key()})
        return [c for c in (self._parse(v) for v in data.get("videos", [])) if c]

    @staticmethod
    def _parse(v: dict) -> dict | None:
        files = [f for f in v.get("video_files", []) if f.get("file_type") == "video/mp4" and f.get("width") and f.get("link")]
        if not files:
            return None
        # Closest to 1080 on the long edge, preferring not to go below it: sharp enough for a 1080p timeline without 4K weight.
        best = min(files, key=lambda f: (max(f["width"], f["height"]) < 1920, abs(max(f["width"], f["height"]) - 1920)))
        slug = re.sub(r"-\d+/?$", "", v["url"].rstrip("/").rsplit("/", 1)[-1])
        pics = v.get("video_pictures") or []
        return {"provider": "pexels", "asset_id": str(v["id"]), "page_url": v["url"], "download_url": best["link"],
                "thumb_url": (pics[0]["picture"] if pics else v.get("image")), "width": best["width"], "height": best["height"],
                "fps": best.get("fps"), "duration": v.get("duration"), "aspect": _aspect(best["width"], best["height"]),
                "author": v.get("user", {}).get("name"), "author_url": v.get("user", {}).get("url"),
                "tags": [t for t in slug.split("-") if t], "title": slug.replace("-", " ")}

    def attribution_text(self, cand):
        return f"Video by {cand.get('author') or 'a Pexels contributor'} on Pexels ({cand['page_url']})"


class Pixabay(Provider):
    name, env_key = "pixabay", "PIXABAY_API_KEY"
    URL = "https://pixabay.com/api/videos/"

    def search(self, query, per_page=15, orientation=None):
        params = {"key": self._need_key(), "q": query, "per_page": max(3, per_page), "safesearch": "true"}
        data = self._get_json(self.URL, params)
        return [c for c in (self._parse(h) for h in data.get("hits", [])) if c]

    @staticmethod
    def _parse(h: dict) -> dict | None:
        vids = h.get("videos") or {}
        pick = next((vids[k] for k in ("large", "medium", "small", "tiny") if vids.get(k, {}).get("url")), None)
        if not pick:
            return None
        pid = h.get("picture_id")
        return {"provider": "pixabay", "asset_id": str(h["id"]), "page_url": h["pageURL"], "download_url": pick["url"],
                "thumb_url": pick.get("thumbnail") or (f"https://i.vimeocdn.com/video/{pid}_640x360.jpg" if pid else None),
                "width": pick["width"], "height": pick["height"], "fps": None, "duration": h.get("duration"),
                "aspect": _aspect(pick["width"], pick["height"]), "author": h.get("user"),
                "author_url": f"https://pixabay.com/users/{h.get('user')}-{h.get('user_id')}/" if h.get("user") else None,
                "tags": [t.strip() for t in (h.get("tags") or "").split(",") if t.strip()], "title": h.get("tags", ""),
                "type": h.get("type")}

    def attribution_text(self, cand):
        return f"Video by {cand.get('author') or 'a Pixabay contributor'} from Pixabay ({cand['page_url']})"


PROVIDERS = {"pexels": Pexels, "pixabay": Pixabay}


def download(provider: Provider, cand: dict, ep, log: licenses.LicenseLog, query: str, approved_by: str) -> dict:
    """Download one approved candidate into the episode and log its licence. Idempotent per provider+asset.

    The log entry is written only after the bytes are safely on disk, and a failed download leaves nothing behind,
    so the log never claims a file that does not exist and no file exists without a log entry.
    """
    if not (approved_by or "").strip():
        raise ValueError("approved_by is required: a named human approves every download (AGENTS.md checkpoint rule)")
    root = licenses.ep_root(ep)
    prior = log.find(source=cand["provider"], asset_id=cand["asset_id"])
    if prior and (root / prior["file"]).is_file():
        return {"file": prior["file"], "sha256": prior["sha256"], "reused": True}
    rel = (licenses.work_dir(ep).relative_to(root) / "assets" / "downloads" / f"{cand['provider']}-{cand['asset_id']}.mp4").as_posix()
    dest = root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(".mp4.part")
    try:
        r = provider.http.get(cand["download_url"], timeout=120, stream=True)  # no API key is sent to the CDN
        r.raise_for_status()
        with open(part, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)
    try:
        digest = licenses.sha256_file(dest)
        info = provider.license_info(cand)
        log.append({"source": cand["provider"], "asset_id": cand["asset_id"], "url": cand["page_url"],
                    "download_url": cand["download_url"], "author": cand.get("author"), "author_url": cand.get("author_url"),
                    "file": rel, "sha256": digest, "bytes": dest.stat().st_size, "query": query, "approved_by": approved_by.strip(),
                    "duration": cand.get("duration"), "size": [cand.get("width"), cand.get("height")], **info})
    except Exception:
        dest.unlink(missing_ok=True)  # never leave an unlogged file in the episode
        raise
    return {"file": rel, "sha256": digest, "reused": False}
