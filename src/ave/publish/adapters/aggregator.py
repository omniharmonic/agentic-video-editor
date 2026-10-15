"""EXPERIMENTAL generic HTTP adapter for hosted posting services (Instagram / X / TikTok via e.g. Ayrshare,
Upload-Post or Late). Nothing about any vendor is hard-coded: base URL, auth header and endpoint/body templates all
come from a YAML file you write after reading your vendor's docs (see config/aggregator.example.yaml).

Config lookup: constructor `config=` (dict), env AVE_AGGREGATOR_CONFIG, then ~/.config/ave/aggregator.yaml.
API key: keychain/env secret `aggregator_api_key`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import yaml

from .. import safety
from .base import PublishError, Publisher, Uncertain, effective_format, limit_problems, probe_video

log = safety.get_logger("ave.publish.aggregator")
_WHOLE = re.compile(r"^\{(\w+)\}$")


def default_config_path() -> Path:
    return Path(os.environ.get("AVE_AGGREGATOR_CONFIG") or Path("~/.config/ave/aggregator.yaml").expanduser())


def render(template: Any, values: dict) -> Any:
    """Fill {placeholders}. A string that is exactly one placeholder keeps the value's type (lists, ints)."""
    if isinstance(template, str):
        m = _WHOLE.match(template)
        if m and m.group(1) in values:
            return values[m.group(1)]
        return re.sub(r"\{(\w+)\}", lambda mm: str(values.get(mm.group(1), "")), template)
    if isinstance(template, dict):
        return {k: render(v, values) for k, v in template.items()}
    if isinstance(template, list):
        return [render(v, values) for v in template]
    return template


def dig(data: Any, path: str | None) -> Any:
    for part in (path or "").split("."):
        if not part:
            continue
        data = data.get(part) if isinstance(data, dict) else None
    return data


def validate_config(cfg: dict) -> list[str]:
    p = []
    base = cfg.get("base_url", "")
    u = urlparse(base)
    if not u.scheme or not u.netloc:
        p.append("aggregator config: base_url missing")
    elif u.scheme != "https" and u.hostname not in ("localhost", "127.0.0.1"):
        p.append("aggregator config: base_url must be https")
    if not (cfg.get("post") or {}).get("path"):
        p.append("aggregator config: post.path missing")
    if not (cfg.get("auth") or {}).get("header"):
        p.append("aggregator config: auth.header missing")
    return p


class AggregatorPublisher(Publisher):
    name = "aggregator"
    carries_images = True         # through the upload/post templates (media_urls / media_ids / alt_texts placeholders)

    def __init__(self, network: str = "instagram", config: dict | None = None, session: Any = None, **kw):
        super().__init__(**kw)
        self.network = network
        self.name = network
        self._cfg = config
        self._session = session

    @property
    def conf(self) -> dict:
        if self._cfg is None:
            p = default_config_path()
            self._cfg = yaml.safe_load(p.read_text()) if p.is_file() else {}
        return self._cfg

    @property
    def http(self):
        if self._session is None:
            import requests
            self._session = requests.Session()
        return self._session

    def ready_problems(self) -> list[str]:
        p = validate_config(self.conf) if self.conf else [
            f"no aggregator config at {default_config_path()} (copy config/aggregator.example.yaml)"]
        if self.network not in (self.conf.get("networks") or {}):
            p.append(f"aggregator config does not list network {self.network!r} under `networks`")
        if not safety.get_secret("aggregator_api_key"):
            p.append("no aggregator API key; run `ave publish auth aggregator`")
        return p

    def status(self) -> dict:
        p = self.ready_problems()
        return {"platform": self.network, "adapter": "aggregator (experimental)", "ready": not p, "problems": p}

    def preflight(self, item: dict, video: Path) -> list[str]:
        p: list[str] = []
        if item.get("visibility") != "public" and not self.conf.get("supports_visibility"):
            p.append(f"{self.network}: aggregator cannot hold a post back; set visibility=public deliberately "
                     "(or set supports_visibility: true in the aggregator config if your service has a draft mode)")
        p += self.check_schedule(item)
        if (item.get("media_type") or "video") != "video":
            return p          # image/carousel limits are checked once for every platform by the queue (base.image_problems)
        try:
            info = probe_video(video)
        except ValueError as e:
            return p + [f"{self.network}: {e}"]
        fmt = effective_format(item, info)
        spec = self.cfg.get(self.network) or {}
        p += limit_problems(spec.get("reel" if fmt == "short" else "long"), info, f"{self.network} {fmt}")
        return p

    def _headers(self) -> dict:
        a = self.conf["auth"]
        return {a["header"]: f"{a.get('prefix', '')}{safety.get_secret('aggregator_api_key')}"}

    def _url(self, path: str) -> str:
        return self.conf["base_url"].rstrip("/") + "/" + path.lstrip("/")

    def _send(self, step: str, final: bool, fn: Callable[[], Any]) -> dict:
        try:
            r = fn()
        except Exception as e:
            msg = safety.redact(f"{type(e).__name__}: {e}")
            if final:
                raise Uncertain(f"aggregator {step}: {msg}")
            raise PublishError(f"aggregator {step} failed before posting: {msg}")
        try:
            data = r.json()
        except Exception:
            data = {}
        if r.status_code >= 400:
            msg = safety.redact(f"aggregator {step} HTTP {r.status_code}: {str(data)[:300]}")
            if final and r.status_code >= 500:
                raise Uncertain(msg)
            raise PublishError(msg + ". Nothing was posted.")
        return data if isinstance(data, dict) else {}

    def publish(self, item: dict, video: Path, progress: Callable[[dict], None]) -> dict:
        probs = self.ready_problems()
        if probs:
            raise PublishError("; ".join(probs))
        c = self.conf
        vals = {"title": item["title"], "description": item.get("description", ""), "caption": item.get("caption", ""),
                "tags": item.get("tags") or [], "schedule": item.get("schedule") or "", "visibility": item.get("visibility"),
                "network": c["networks"][self.network], "media_id": "", "media_url": "", "media_ids": [], "media_urls": [],
                "media_type": item.get("media_type") or "video", "alt_texts": item.get("alt_texts") or []}
        up = c.get("upload")
        files = [Path(f) for f in (item.get("_files") or [video])]       # image/carousel: every file, in order
        if up:
            def do_up(path: Path):
                def go():
                    with open(path, "rb") as fh:
                        return self.http.request(up.get("method", "POST"), self._url(up["path"]), headers=self._headers(),
                                                 files={up.get("file_field", "file"): fh}, timeout=None)
                return go
            ids, urls = [], []
            for path in files:
                d = self._send("upload", False, do_up(path))
                ids.append(dig(d, up.get("id_path")) or "")
                urls.append(dig(d, up.get("url_path")) or "")
                progress({"media_id": ids[0], "media_ids": list(ids), "phase": "uploaded"})
            vals["media_id"], vals["media_url"] = ids[0], urls[0]
            vals["media_ids"], vals["media_urls"] = ids, urls
        elif c.get("requires_public_url") and not item.get("public_url"):
            raise PublishError("this service needs a public video URL (item.public_url); hosting is out of scope")
        post = c["post"]
        d = self._send("post", True, lambda: self.http.request(
            post.get("method", "POST"), self._url(post["path"]), headers=self._headers(),
            json=render(post.get("json", {}), vals), timeout=120))
        return {"id": dig(d, post.get("id_path")), "url": dig(d, post.get("url_path")), "network": self.network,
                "adapter": "aggregator"}
