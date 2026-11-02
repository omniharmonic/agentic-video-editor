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
from .base import PublishError, Publisher, Uncertain, effective_format, limit_problems, platform_schedule, probe_video

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


LOOPBACK = ("localhost", "127.0.0.1", "::1")


def allowed_hosts() -> set[str]:
    """Hosts the API key may be sent to: pinned by the human with `ave publish auth aggregator --host H` (keychain) or AVE_AGGREGATOR_HOSTS
    (comma list). Never read from the aggregator config file, which an agent could edit."""
    raw = safety.get_secret("aggregator_hosts") or ""
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def host_problems(base_url: str) -> list[str]:
    u = urlparse(base_url or "")
    host = (u.hostname or "").lower()
    if not host:
        return ["aggregator config: base_url missing"]
    if host not in allowed_hosts():
        return [f"aggregator host {host!r} is not pinned, so the API key will not be sent there. The human pins it with: "
                f"ave publish auth aggregator --host {host}"]
    return []


def validate_config(cfg: dict) -> list[str]:
    p = []
    base = cfg.get("base_url", "")
    u = urlparse(base)
    insecure_ok = os.environ.get("AVE_AGGREGATOR_ALLOW_INSECURE") == "1"        # the human's explicit opt-in, e.g. a LAN test server
    if not u.scheme or not u.netloc:
        p.append("aggregator config: base_url missing")
    elif u.scheme not in ("https", "http"):
        p.append("aggregator config: base_url must be https")
    elif u.scheme == "http" and u.hostname not in LOOPBACK and not insecure_ok:
        p.append("aggregator config: base_url must be https (plain http only to localhost, or with AVE_AGGREGATOR_ALLOW_INSECURE=1)")
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
        if self.conf.get("base_url"):
            p += host_problems(self.conf["base_url"])
        if not safety.get_secret("aggregator_api_key"):
            p.append("no aggregator API key; run `ave publish auth aggregator`")
        return p

    def status(self) -> dict:
        p = self.ready_problems()
        return {"platform": self.network, "adapter": "aggregator (experimental)", "ready": not p, "problems": p}

    def delivery_contract(self) -> dict:
        # Template capability is local configuration evidence, never provider certification.
        def values(node):
            if isinstance(node,str): return [node]
            if isinstance(node,dict): return [s for v in node.values() for s in values(v)]
            if isinstance(node,list): return [s for v in node for s in values(v)]
            return []
        fields=values((self.conf.get('post') or {}).get('json',{}))
        upload=self.conf.get('upload') or {}
        media_keys=[key for key,path in (('media_id','id_path'),('media_ids','id_path'),('media_url','url_path'),('media_urls','url_path'))
                    if upload.get('path') and upload.get(path) and '{'+key+'}' in fields]
        shapes=['video','image'] if media_keys else []
        if any(key in media_keys for key in ('media_ids','media_urls')): shapes.append('carousel')
        return {'adapter':'aggregator','shapes':shapes,'first_comment':False,'cover':False,'feed_presence':False,
                'alt_text':'{alt_texts}' in fields,'exact_caption':'{caption}' in fields,
                'visibility':bool(self.conf.get('supports_visibility') and '{visibility}' in fields)}

    def contract_problems(self,item):
        contract=self.delivery_contract();out=[]
        if item.get('visibility')!='public' and not contract['visibility']:
            out.append(f'{self.network}: selected aggregator template cannot transmit private/unlisted visibility; choose manual delivery')
        mt=item.get('media_type') or 'video'
        if mt not in contract['shapes']:
            out.append(f'{self.network}: selected aggregator template cannot deliver {mt}; choose manual delivery or configure a supported upload/media template')
        for field in ('first_comment','cover','presentation'):
            if item.get(field): out.append(f'{self.network}: selected aggregator has no automatic {field.replace("_"," ")} operation; choose manual delivery or clear it')
        if item.get('meta_v')==3 and not contract['exact_caption']:
            out.append(f'{self.network}: selected aggregator template must transmit the reviewed caption using {{caption}}')
        if any(item.get('alt_texts') or []) and not contract['alt_text']:
            out.append(f'{self.network}: selected aggregator template cannot transmit reviewed alt text; include {{alt_texts}} or choose manual delivery')
        return out

    def preflight(self, item: dict, video: Path) -> list[str]:
        p = self.contract_problems(item)
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
        bad = validate_config(self.conf) + host_problems(self.conf.get("base_url", ""))
        if bad:
            raise PublishError("refusing to send the aggregator API key: " + "; ".join(bad))
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
        probs = self.contract_problems(item)
        if probs: raise PublishError("; ".join(probs))
        probs = self.ready_problems()
        if probs:
            raise PublishError("; ".join(probs))
        c = self.conf
        vals = {"title": item["title"], "description": item.get("description", ""), "caption": item.get("caption", ""),
                "tags": item.get("tags") or [], "schedule": platform_schedule(item) or "", "visibility": item.get("visibility"),
                "network": c["networks"][self.network], "media_id": "", "media_url": "", "media_ids": [], "media_urls": [],
                "media_type": item.get("media_type") or "video", "alt_texts": item.get("alt_texts") or []}
        up = c.get("upload")
        files = [Path(f) for f in (item.get("_files") or [video])]       # image/carousel: every file, in order
        if up:
            def do_up(path: Path):
                def go():
                    with open(path, "rb") as fh:
                        return self.http.request(up.get("method", "POST"), self._url(up["path"]), headers=self._headers(),
                                                 files={up.get("file_field", "file"): fh}, timeout=None, allow_redirects=False)
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
            json=render(post.get("json", {}), vals), timeout=120, allow_redirects=False))
        from ..receipts import result
        # Generic JSON-path adapters have no certified final-publication status contract.
        return result(self.network,dig(d,post.get('id_path')),phase='processing',object_kind='aggregator_operation',adapter='aggregator')
