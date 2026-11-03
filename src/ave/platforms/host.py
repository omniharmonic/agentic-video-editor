"""Media hosting for URL-fetch APIs (Instagram, Threads, TikTok photos): spec 4.5.

`MediaHost.put(path, ttl_s)` stores the EXACT approved bytes under an unguessable key and returns a short-lived presigned GET URL; the
adapter checks the hash, calls the platform, and `delete`s in `finally`. Implementation: any S3-compatible bucket (Cloudflare R2, S3, B2)
with SigV4 query-string presigning written on the standard library (no boto3).

    ~/.config/ave/host.yaml      endpoint: https://<account>.r2.cloudflarestorage.com   bucket: ave-media   region: auto   prefix: ""
    keychain / env               host_access_key, host_secret_key   (AVE_HOST_ACCESS_KEY, AVE_HOST_SECRET_KEY)

Creating the bucket, the keys and the cost are the human's (spec 12). Without a configured host `load_host()` returns None and
Instagram/Threads preflight says "needs_public_url: configure a media host".
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets as _secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlsplit

import yaml

from ..publish import safety
from .base import HostError, mime_of

ALGO = "AWS4-HMAC-SHA256"


def _q(s: str) -> str:
    return quote(str(s), safe="-_.~")


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def presign(method: str, host: str, path: str, *, access_key: str, secret_key: str, region: str, expires: int, now: datetime,
            headers: dict | None = None, service: str = "s3", scheme: str = "https", extra_query: dict | None = None) -> str:
    """A SigV4 query-string presigned URL (payload UNSIGNED). `headers` are extra headers the caller MUST send exactly as given
    (they are signed); `host` is always signed. Verified against the worked example in AWS's documentation (see the test)."""
    now = now.astimezone(timezone.utc)
    amz_date, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    scope = f"{day}/{region}/{service}/aws4_request"
    hdrs = {"host": host, **{k.lower(): " ".join(str(v).split()) for k, v in (headers or {}).items()}}
    signed = ";".join(sorted(hdrs))
    query = {"X-Amz-Algorithm": ALGO, "X-Amz-Credential": f"{access_key}/{scope}", "X-Amz-Date": amz_date,
             "X-Amz-Expires": str(int(expires)), "X-Amz-SignedHeaders": signed, **{str(k): str(v) for k, v in (extra_query or {}).items()}}
    cq = "&".join(f"{_q(k)}={_q(v)}" for k, v in sorted(query.items()))
    canon = "\n".join([method.upper(), quote(path, safe="/-_.~"), cq, "".join(f"{k}:{hdrs[k]}\n" for k in sorted(hdrs)), signed, "UNSIGNED-PAYLOAD"])
    sts = "\n".join([ALGO, amz_date, scope, hashlib.sha256(canon.encode()).hexdigest()])
    k = _hmac(_hmac(_hmac(_hmac(("AWS4" + secret_key).encode(), day), region), service), "aws4_request")
    sig = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
    return f"{scheme}://{host}{quote(path, safe='/-_.~')}?{cq}&X-Amz-Signature={sig}"


class S3Host:
    """MediaHost over an S3-compatible bucket (path-style URLs, which R2, S3 and B2 all accept)."""

    def __init__(self, *, endpoint: str, bucket: str, access_key: str, secret_key: str, region: str = "auto", prefix: str = "",
                 http: Any = None, now: Callable[[], datetime] | None = None, token: Callable[[], str] | None = None):
        parts = urlsplit(endpoint if "://" in endpoint else f"https://{endpoint}")
        self.scheme, self.netloc = parts.scheme or "https", parts.netloc
        self.bucket, self.region, self.prefix = bucket, region, prefix.strip("/")
        self._ak, self._sk = access_key, secret_key
        safety.register_secret(secret_key)
        safety.register_secret(access_key)
        self._http = http
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._token = token or (lambda: _secrets.token_urlsafe(18))

    @property
    def http(self):
        if self._http is None:
            import requests
            self._http = requests.Session()
        return self._http

    def _path(self, key: str) -> str:
        return "/" + "/".join(x for x in (self.bucket, key) if x)

    def _url(self, method: str, key: str, ttl_s: int, headers: dict | None = None, *, extra_query: dict | None = None) -> str:
        return presign(method, self.netloc, self._path(key), access_key=self._ak, secret_key=self._sk, region=self.region,
                       expires=ttl_s, now=self.now(), headers=headers, scheme=self.scheme, extra_query=extra_query)

    def put(self, path: Path, *, ttl_s: int = 900) -> dict:
        path = Path(path)
        sha = safety.sha256_file(path)
        key = "/".join(x for x in (self.prefix, self._token(), sha + path.suffix.lower()) if x)
        ctype = mime_of(path)
        # `x-amz-checksum-sha256` is checked by the STORE against the bytes it received (our own x-amz-meta-sha256 only records what we claimed)
        hdrs = {"Content-Type": ctype, "x-amz-meta-sha256": sha, "x-amz-checksum-sha256": base64.b64encode(bytes.fromhex(sha)).decode()}
        try:
            r = self.http.put(self._url("PUT", key, 600, hdrs), data=path.read_bytes(), headers=hdrs, timeout=300)
        except Exception as e:
            raise HostError(safety.redact(f"media host upload failed: {type(e).__name__}: {e}"))
        if int(getattr(r, "status_code", 200)) >= 300:
            raise HostError(f"media host upload refused (HTTP {r.status_code})")
        return {"url": self._url("GET", key, ttl_s), "key": key, "sha256": sha, "bytes": path.stat().st_size,
                "expires": (self.now() + timedelta(seconds=ttl_s)).strftime("%Y-%m-%dT%H:%M:%SZ")}

    def verify(self, hosted: dict) -> None:
        """HEAD the object (a presigned HEAD, since a GET signature does not cover HEAD) and compare its stored hash and size."""
        try:
            hdrs = {"x-amz-checksum-mode": "ENABLED"}
            r = self.http.request("HEAD", self._url("HEAD", hosted["key"], 300, hdrs), headers=hdrs, timeout=60)
        except Exception as e:
            raise HostError(safety.redact(f"media host HEAD failed: {type(e).__name__}: {e}"))
        if int(getattr(r, "status_code", 200)) >= 300:
            raise HostError(f"media host HEAD returned HTTP {r.status_code}")
        h = {str(k).lower(): v for k, v in dict(getattr(r, "headers", {}) or {}).items()}
        stored = h.get("x-amz-checksum-sha256")        # computed by the store from the bytes it holds, when it reports one
        if stored is not None and stored != base64.b64encode(bytes.fromhex(str(hosted.get("sha256")))).decode():
            raise HostError("the store's own checksum of the hosted object differs from the approved file; aborted")
        if h.get("x-amz-meta-sha256") != hosted.get("sha256"):
            raise HostError("the hosted object's sha256 differs from the approved file; aborted")
        if hosted.get("bytes") is not None and h.get("content-length") not in (None, str(hosted["bytes"])):
            raise HostError("the hosted object's size differs from the approved file; aborted")

    _OURS = re.compile(r"^[A-Za-z0-9_-]{16,}/[0-9a-f]{64}\.[a-z0-9]{2,5}$")

    def sweep(self, older_than_s: int = 86400) -> int:
        """Delete objects a crashed or killed send left behind: keys under this host's prefix that have OUR shape (`<token>/<sha256><ext>`) and are
        older than `older_than_s`. The presigned GET dies at its TTL, but the OBJECT would otherwise stay in the bucket for good. Returns the count."""
        import xml.etree.ElementTree as ET
        q = {"list-type": "2", **({"prefix": self.prefix + "/"} if self.prefix else {})}
        r = self.http.get(presign("GET", self.netloc, "/" + self.bucket, access_key=self._ak, secret_key=self._sk, region=self.region, expires=300,
                                  now=self.now(), scheme=self.scheme, extra_query=q), timeout=60)
        if int(getattr(r, "status_code", 200)) >= 300:
            raise HostError(f"media host list returned HTTP {r.status_code}")
        cutoff = self.now() - timedelta(seconds=older_than_s)
        removed = 0
        for el in ET.fromstring(r.text).iter():
            if not el.tag.endswith("Contents"):
                continue
            kids = {c.tag.split("}")[-1]: (c.text or "") for c in el}
            key, when = kids.get("Key", ""), kids.get("LastModified", "")
            rel = key[len(self.prefix) + 1:] if self.prefix and key.startswith(self.prefix + "/") else key
            if not self._OURS.match(rel) or (self.prefix and not key.startswith(self.prefix + "/")):
                continue
            try:
                t = datetime.fromisoformat(when.replace("Z", "+00:00"))
            except ValueError:
                continue
            if t < cutoff:
                self.delete(key)
                removed += 1
        return removed

    def delete(self, key: str) -> None:
        r = self.http.delete(self._url("DELETE", key, 300), timeout=60)
        if int(getattr(r, "status_code", 204)) >= 300 and int(r.status_code) != 404:
            raise HostError(f"media host delete returned HTTP {r.status_code}")


def config_path() -> Path:
    base = os.environ.get("AVE_CONFIG_DIR") or str(Path(os.environ.get("HOME", "~")).expanduser() / ".config" / "ave")
    return Path(base) / "host.yaml"


def load_host(path: Path | None = None, **kw) -> S3Host | None:
    """The configured host, or None (no file, incomplete file, or keys missing from the keychain/env)."""
    p = Path(path) if path else config_path()
    try:
        cfg = yaml.safe_load(p.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not (isinstance(cfg, dict) and cfg.get("endpoint") and cfg.get("bucket")):
        return None                       # no host configured: do not touch the keychain at all
    ak, sk = safety.get_secret("host_access_key"), safety.get_secret("host_secret_key")
    if not (ak and sk):
        return None
    return S3Host(endpoint=cfg["endpoint"], bucket=cfg["bucket"], region=str(cfg.get("region") or "auto"), prefix=str(cfg.get("prefix") or ""),
                  access_key=ak, secret_key=sk, **kw)


def host_status(path: Path | None = None) -> dict:
    """For doctor: configured or not and what is missing. Never a key."""
    p = Path(path) if path else config_path()
    missing = []
    try:
        cfg = yaml.safe_load(p.read_text()) or {}
    except (OSError, yaml.YAMLError):
        cfg = {}
        missing.append(f"{p} (endpoint, bucket)")
    for k in ("endpoint", "bucket"):
        if p.exists() and not cfg.get(k):
            missing.append(f"host.yaml: {k}")
    for s in ("host_access_key", "host_secret_key"):
        if not safety.get_secret(s):
            missing.append(f"secret {s} (AVE_{s.upper()})")
    return {"configured": not missing, "missing": missing}
