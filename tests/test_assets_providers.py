"""Providers against recorded-style sample JSON. No network: a fake session stands in for requests."""
import json
import time
from pathlib import Path

import pytest

from ave.assets import licenses, providers

DATA = Path(__file__).parent / "data_assets"


class FakeResp:
    def __init__(self, payload=None, content=b"", status=200, headers=None):
        self._p, self._c, self.status_code, self.headers = payload, content, status, headers or {}

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=1 << 16):
        for i in range(0, len(self._c), chunk_size):
            yield self._c[i:i + chunk_size]


class FakeSession:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, params=None, headers=None, timeout=None, stream=False):
        self.calls.append({"url": url, "params": params, "headers": headers or {}})
        for prefix, resp in self.routes.items():
            if url.startswith(prefix):
                return resp
        raise AssertionError(f"unmocked URL {url}")


def load(name):
    return json.loads((DATA / name).read_text())


def pexels(http, tmp_path, **kw):
    return providers.Pexels(api_key="PXKEY", http=http, cache_dir=tmp_path / "cache", **kw)


def pixabay(http, tmp_path, **kw):
    return providers.Pixabay(api_key="PBKEY", http=http, cache_dir=tmp_path / "cache", **kw)


def test_pexels_parse_picks_hd_mp4_and_keeps_credit(tmp_path):
    http = FakeSession({"https://api.pexels.com/videos/search": FakeResp(load("pexels_videos_search.json"))})
    res = pexels(http, tmp_path).search("hands clay", per_page=3)
    assert [r["asset_id"] for r in res] == ["3195394", "855564"]
    a = res[0]
    assert a["download_url"].endswith("hd_1920_1080_25fps.mp4") and (a["width"], a["height"]) == (1920, 1080)
    assert a["duration"] == 14 and a["author"] == "Ada Example" and a["page_url"].startswith("https://www.pexels.com/video/")
    assert a["provider"] == "pexels" and a["aspect"] == "landscape" and res[1]["aspect"] == "portrait"
    assert http.calls[0]["headers"]["Authorization"] == "PXKEY"


def test_pixabay_parse_prefers_large_and_never_hotlinks_thumbs(tmp_path):
    http = FakeSession({"https://pixabay.com/api/videos/": FakeResp(load("pixabay_videos_search.json"))})
    res = pixabay(http, tmp_path).search("river")
    assert res[0]["download_url"].endswith("125-large.mp4") and res[0]["width"] == 1920
    assert res[1]["download_url"].endswith("9001-medium.mp4")  # no large -> best available
    assert res[0]["author"] == "forest_fan" and "river" in res[0]["tags"]
    assert http.calls[0]["params"]["key"] == "PBKEY"


def test_missing_key_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    with pytest.raises(providers.ProviderError, match="PEXELS_API_KEY"):
        providers.Pexels(http=FakeSession({}), cache_dir=tmp_path).search("x")


def test_cache_serves_second_identical_query_without_network(tmp_path):
    http = FakeSession({"https://pixabay.com/api/videos/": FakeResp(load("pixabay_videos_search.json"))})
    p = pixabay(http, tmp_path)
    p.search("river")
    p.search("river")
    assert len(http.calls) == 1
    p.search("moss")  # a different query is a new request
    assert len(http.calls) == 2


def test_pixabay_cache_expires_after_24h(tmp_path):
    http = FakeSession({"https://pixabay.com/api/videos/": FakeResp(load("pixabay_videos_search.json"))})
    t = [1_000_000.0]
    p = pixabay(http, tmp_path, now=lambda: t[0])
    p.search("river")
    t[0] += 23 * 3600
    p.search("river")
    assert len(http.calls) == 1
    t[0] += 2 * 3600
    p.search("river")
    assert len(http.calls) == 2


def test_cache_never_stores_api_key(tmp_path):
    http = FakeSession({"https://pixabay.com/api/videos/": FakeResp(load("pixabay_videos_search.json"))})
    pixabay(http, tmp_path).search("river")
    blob = "".join(p.read_text() for p in (tmp_path / "cache").rglob("*") if p.is_file())
    assert "PBKEY" not in blob


def test_download_writes_file_and_complete_license_entry(tmp_path):
    http = FakeSession({"https://pixabay.com/api/videos/": FakeResp(load("pixabay_videos_search.json")),
                        "https://cdn.pixabay.com/": FakeResp(content=b"VIDEO-BYTES" * 100)})
    p = pixabay(http, tmp_path)
    cand = p.search("river forest")[0]
    log = licenses.LicenseLog(tmp_path / "ep" / "work" / "assets" / "licenses.jsonl")
    out = providers.download(p, cand, tmp_path / "ep", log, query="river forest", approved_by="ben")
    f = tmp_path / "ep" / out["file"]
    assert f.read_bytes() == b"VIDEO-BYTES" * 100 and not list(f.parent.glob("*.part"))
    e = log.entries()[0]
    for k in ("source", "asset_id", "url", "download_url", "license", "license_url", "license_text", "at", "query", "approved_by", "sha256", "file"):
        assert e[k], k
    assert e["source"] == "pixabay" and e["query"] == "river forest" and e["approved_by"] == "ben"
    assert e["sha256"] == licenses.sha256_file(f)
    assert e["author"] == "forest_fan"
    assert "PBKEY" not in json.dumps(e)


def test_download_is_idempotent_and_logs_once(tmp_path):
    http = FakeSession({"https://api.pexels.com/videos/search": FakeResp(load("pexels_videos_search.json")),
                        "https://videos.pexels.com/": FakeResp(content=b"abc" * 50)})
    p = pexels(http, tmp_path)
    cand = p.search("hands")[0]
    log = licenses.LicenseLog(tmp_path / "ep" / "work" / "assets" / "licenses.jsonl")
    providers.download(p, cand, tmp_path / "ep", log, query="hands", approved_by="ben")
    n = len(http.calls)
    providers.download(p, cand, tmp_path / "ep", log, query="hands", approved_by="ben")
    assert len(http.calls) == n and len(log.entries()) == 1


def test_download_requires_named_approver_and_logs_nothing_on_failure(tmp_path):
    http = FakeSession({"https://api.pexels.com/videos/search": FakeResp(load("pexels_videos_search.json")),
                        "https://videos.pexels.com/": FakeResp(status=500)})
    p = pexels(http, tmp_path)
    cand = p.search("hands")[0]
    log = licenses.LicenseLog(tmp_path / "ep" / "work" / "assets" / "licenses.jsonl")
    with pytest.raises(ValueError, match="approved_by"):
        providers.download(p, cand, tmp_path / "ep", log, query="hands", approved_by="")
    with pytest.raises(Exception):
        providers.download(p, cand, tmp_path / "ep", log, query="hands", approved_by="ben")
    assert log.entries() == []
    assert not list((tmp_path / "ep").rglob("*.mp4")) and not list((tmp_path / "ep").rglob("*.part"))


def test_pexels_entry_notes_attribution_and_restrictions(tmp_path):
    http = FakeSession({"https://api.pexels.com/videos/search": FakeResp(load("pexels_videos_search.json")),
                        "https://videos.pexels.com/": FakeResp(content=b"x" * 10)})
    p = pexels(http, tmp_path)
    cand = p.search("hands")[0]
    log = licenses.LicenseLog(tmp_path / "ep" / "work" / "assets" / "licenses.jsonl")
    providers.download(p, cand, tmp_path / "ep", log, query="hands", approved_by="ben")
    e = log.entries()[0]
    assert e["attribution"]["required"] is False and "Ada Example" in e["attribution"]["text"] and "Pexels" in e["attribution"]["text"]
    assert any("identifiable" in r.lower() for r in e["restrictions"])


def test_rate_limit_is_surfaced_plainly(tmp_path):
    http = FakeSession({"https://api.pexels.com/videos/search": FakeResp(status=429, headers={"X-Ratelimit-Remaining": "0"})})
    with pytest.raises(providers.ProviderError, match="rate limit"):
        pexels(http, tmp_path).search("x")
