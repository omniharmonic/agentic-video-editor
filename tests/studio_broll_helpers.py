"""Helpers for the library / B-roll tests (WS1): scripted stock HTTP with byte downloads, tiny real images, and a tree-wide secret scan.

`StockHttp` wraps `FakeHttp` (JSON routes from tests/data_studio/broll/*.json, documentation-derived and UNVERIFIED) and adds byte
responses for CDN URLs, which FakeHttp's JSON-only responses cannot express. An unexpected URL is an AssertionError, never a silent 200.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from PIL import Image

from studio_helpers import DATA, FakeHttp

KEY = "sk-test-0123456789abcdef"


def jpeg_bytes(color=(120, 90, 60), size=(64, 48)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


def png_bytes(color=(10, 20, 30), size=(32, 32)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


class BytesResp:
    def __init__(self, content: bytes, status: int = 200, ctype: str | None = "image/jpeg"):
        self.content, self.status_code = content, status
        self.headers = {"Content-Type": ctype} if ctype else {}
        self.text = ""

    def iter_content(self, n):
        for i in range(0, len(self.content), n):
            yield self.content[i:i + n]

    def json(self):
        return json.loads(self.content)


class StockHttp:
    def __init__(self, *files: str, downloads: dict[str, "BytesResp | bytes"] | None = None, extra_routes: list[dict] | None = None):
        routes: list[dict] = list(extra_routes or [])
        for f in files:
            routes += json.loads((DATA / "broll" / f).read_text())
        self.fake = FakeHttp(routes)
        self.downloads = dict(downloads or {})
        self.calls: list[dict] = []

    def get(self, url, **kw):
        self.calls.append({"url": url, **kw})
        if url in self.downloads:
            d = self.downloads[url]
            return d if isinstance(d, BytesResp) else BytesResp(d)
        return self.fake.get(url, **kw)

    def request(self, method, url, **kw):
        return self.fake.request(method, url, **kw)


def tree_text(root: Path):
    """(path, text) for every file under root, binary files decoded loosely."""
    for p in Path(root).rglob("*"):
        if p.is_file():
            yield p, p.read_bytes().decode("utf-8", "ignore")


def assert_no_secret(root: Path, secret: str = KEY) -> None:
    hits = [str(p) for p, t in tree_text(root) if secret in t]
    assert not hits, f"secret value found in files: {hits}"
