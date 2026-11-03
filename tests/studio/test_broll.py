"""B-roll (WS1): provider parsing against documentation-derived (UNVERIFIED) fixtures, critic, human pick, download ordering, keys, hooks."""

import json
import time

import pytest

import ave.library as lib
from ave import broll
from ave.assets import licenses
from ave.broll import fetch, queries
from ave.broll.providers import Candidate, Coverr, Local, Pexels, Pixabay, ProviderError, Unsplash
from ave.library import registry
from ave.studio.service import Studio
from studio_broll_helpers import KEY, BytesResp, StockHttp, assert_no_secret, jpeg_bytes, png_bytes
from studio_helpers import FakeClock


@pytest.fixture
def cache(tmp_path):
    return tmp_path / "cache"


def prov(cls, http, cache, **kw):
    return cls(api_key=KEY, http=http, cache_dir=cache, now=time.time, **kw)


# ---------------------------------------------------------------------------------------------------- parsing

def test_pexels_photo_and_video(cache):
    http = StockHttp("pexels.json")
    p = prov(Pexels, http, cache)
    photos = p.search("bread", kind="photo", orientation="portrait", per_page=5)
    assert [c.asset_id for c in photos] == ["2284", "3301"]
    c = photos[0]
    assert (c.provider, c.kind, c.w, c.h, c.author) == ("pexels", "photo", 4000, 6000, "A. Baker")
    assert c.download_url.endswith("pexels-photo-2284.jpeg") and "h=350" in c.preview_url and c.key == "pexels:2284"
    assert c.attribution_text == "Photo by A. Baker on Pexels (https://www.pexels.com/photo/bread-crust-on-a-wooden-board-2284/)"
    assert c.extra["alt"].startswith("A loaf of bread") and not c.attribution_required
    assert photos[1].title == "hands kneading dough"                        # no alt text: the slug stands in as title
    call = http.calls[0]
    assert call["headers"] == {"Authorization": KEY} and call["params"]["orientation"] == "portrait" and "key" not in call["params"]
    v = p.search("bread", kind="video")[0]
    assert (v.kind, v.w, v.h, v.duration, v.asset_id) == ("video", 1920, 1080, 14, "8801") and v.download_url.endswith("hd_1920_1080_25fps.mp4")
    assert v.attribution_text.startswith("Video by C. Oven on Pexels")


def test_pixabay_photo_and_video(cache):
    http = StockHttp("pixabay.json")
    p = prov(Pixabay, http, cache)
    photos = p.search("bread", kind="photo", orientation="landscape")
    assert [c.asset_id for c in photos] == ["195893", "88"]
    c = photos[0]
    assert c.tags == ("bread", "crust", "kitchen") and c.download_url == "https://pixabay.com/get/g1f2e3d_1280.jpg"
    assert c.preview_url.endswith("_640.jpg") and (c.w, c.h) == (4000, 2667)
    assert c.author_url == "https://pixabay.com/users/Josch13-48777/" and c.attribution_text.startswith("Image by Josch13 from Pixabay")
    q = http.calls[0]["params"]
    assert q["image_type"] == "photo" and q["orientation"] == "horizontal" and q["q"] == "bread" and q["safesearch"] == "true"
    v = p.search("bread", kind="video")[0]
    assert (v.kind, v.w, v.h, v.duration) == ("video", 1920, 1080, 18) and v.download_url.endswith("4401-large.mp4")


def test_unsplash_requires_attribution_and_parses_urls(cache):
    p = prov(Unsplash, StockHttp("unsplash.json"), cache)
    res = p.search("bread", kind="photo", orientation="square")
    assert [c.asset_id for c in res] == ["Dwu85P9SOIk", "ab12"]
    c = res[0]
    assert c.attribution_required and c.author == "D. Ough" and c.author_url == "https://unsplash.com/@dough"
    assert c.download_url.endswith("fm=jpg") and c.preview_url.endswith("w=400") and c.tags == ("bread", "bakery")
    assert c.attribution_text.startswith("Photo by D. Ough on Unsplash (https://unsplash.com/photos/Dwu85P9SOIk")
    assert p.http.calls[0]["headers"]["Authorization"] == f"Client-ID {KEY}" and p.http.calls[0]["params"]["orientation"] == "squarish"
    assert res[1].title == "Flour on a table"                              # description when there is no alt_description
    assert p.license_info(c)["attribution"]["required"] is True and p.license_info(c)["terms_snapshot"]["verified"] is False
    with pytest.raises(ProviderError, match="does not offer video"):
        p.search("bread", kind="video")


def test_coverr_video_with_orientation(cache):
    p = prov(Coverr, StockHttp("coverr.json"), cache)
    res = p.search("flour", kind="video")
    assert [c.asset_id for c in res] == ["cvr01", "cvr02"]
    assert (res[0].w, res[0].h, res[0].duration) == (1920, 1080, 11.5) and res[0].download_url.endswith("download=true")
    assert (res[1].w, res[1].h) == (1080, 1920) and res[1].download_url.endswith("1080p.mp4")      # vertical swaps the dimensions
    only_portrait = prov(Coverr, StockHttp("coverr.json"), cache / "b").search("flour", kind="video", orientation="portrait")
    assert [c.asset_id for c in only_portrait] == ["cvr02"]
    assert p.http.calls[0]["headers"]["Authorization"] == f"Bearer {KEY}"


def test_terms_are_present_and_unverified_for_every_provider(cache):
    for cls in (Pexels, Pixabay, Unsplash, Coverr):
        pv = prov(cls, StockHttp(), cache)
        kind = "video" if cls is Coverr else "photo"
        c = Candidate(provider=pv.name, asset_id="1", kind=kind, page_url="https://x/1", preview_url="", download_url="https://x/d", w=1, h=1)
        info = pv.license_info(c)
        assert info["license"] and info["license_url"].startswith("https://") and info["terms_snapshot"]["verified"] is False


# ---------------------------------------------------------------------------------------------------- keys & cache

def test_missing_key_names_the_env_variable_not_a_value(cache, monkeypatch):
    for k in ("PEXELS_API_KEY", "PIXABAY_API_KEY", "UNSPLASH_ACCESS_KEY", "COVERR_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ProviderError, match="UNSPLASH_ACCESS_KEY is not set"):
        Unsplash(http=StockHttp(), cache_dir=cache).search("x", kind="photo")


def test_key_resolves_from_env_then_keychain(cache, monkeypatch):
    http = StockHttp("pexels.json")
    monkeypatch.setenv("PEXELS_API_KEY", "env-key-123456")
    Pexels(http=http, cache_dir=cache).search("a", kind="photo")
    assert http.calls[0]["headers"]["Authorization"] == "env-key-123456"
    monkeypatch.delenv("PEXELS_API_KEY")
    from ave.publish import safety
    import keyring
    keyring.set_password(safety.SERVICE, "pexels_api_key", "keychain-key-123456")
    Pexels(http=StockHttp("pexels.json"), cache_dir=cache / "k").search("a", kind="photo")
    h2 = StockHttp("pexels.json")
    Pexels(http=h2, cache_dir=cache / "k2").search("a", kind="photo")
    assert h2.calls[0]["headers"]["Authorization"] == "keychain-key-123456"


def test_http_errors_become_provider_errors(cache):
    def one(status):
        return StockHttp(extra_routes=[{"method": "GET", "url": "api.pexels.com", "status": status, "response": {}}])
    with pytest.raises(ProviderError, match="rate limit"):
        prov(Pexels, one(429), cache).search("a", kind="photo")
    with pytest.raises(ProviderError, match="rejected the API key"):
        prov(Pexels, one(401), cache / "2").search("a", kind="photo")
    with pytest.raises(ProviderError, match="HTTP 500"):
        prov(Pexels, one(500), cache / "3").search("a", kind="photo")


def test_responses_are_cached_for_24h_and_the_key_never_reaches_the_cache(cache):
    http = StockHttp("pixabay.json")
    t = [1000.0]
    p = Pixabay(api_key=KEY, http=http, cache_dir=cache, now=lambda: t[0])
    p.search("bread", kind="photo")
    p.search("bread", kind="photo")
    assert len(http.calls) == 1
    t[0] += 24 * 3600 + 1
    http2 = StockHttp("pixabay.json")
    Pixabay(api_key=KEY, http=http2, cache_dir=cache, now=lambda: t[0]).search("bread", kind="photo")
    assert len(http2.calls) == 1                                         # expired: fetched again
    assert_no_secret(cache)
    assert any(cache.rglob("*.json"))


# ---------------------------------------------------------------------------------------------------- search, critic

def test_search_writes_a_document_with_critic_verdicts(bare, cache):
    now = FakeClock().now()
    http = StockHttp("pexels.json", "pixabay.json", downloads={})
    doc = broll.search(bare, "bread crust", providers=[prov(Pexels, http, cache), prov(Pixabay, http, cache)], kind="photo", now=now,
                       post="p_1", previews=False)
    assert doc["id"].startswith("s_20261019_") and doc["providers"] == ["pexels", "pixabay"] and doc["post"] == "p_1" and doc["errors"] == []
    assert [c["asset_id"] for c in doc["candidates"]] == ["2284", "3301", "195893", "88"]
    assert all(c["critic"]["verdict"] == "ok" and c["critic"]["unscored"] is True for c in doc["candidates"])
    assert json.loads((bare.work / "broll" / f"{doc['id']}.json").read_text()) == doc
    assert broll.get_search(bare, doc["id"]) == doc
    assert not list((bare.library_dir / "assets").glob("*/*"))                # nothing downloaded by a search
    assert_no_secret(bare.root)


def test_critic_rejects_a_cliche_candidate_and_warns_on_discouraged(bare, cache):
    routes = [{"method": "GET", "url": "api.pexels.com/v1/search", "response": {"photos": [
        {"id": 1, "width": 100, "height": 100, "url": "https://www.pexels.com/photo/handshake-deal-1/", "photographer": "X",
         "src": {"original": "https://images.pexels.com/1.jpg", "medium": "https://images.pexels.com/1m.jpg"}, "alt": "business handshake"}]}}]
    doc = broll.search(bare, "deal", providers=[prov(Pexels, StockHttp(extra_routes=routes), cache)], kind="photo", previews=False)
    crit = doc["candidates"][0]["critic"]
    assert crit["verdict"] == "reject" and any("handshake" in p for p in crit["problems"])


def test_agent_scores_replace_the_unscored_verdict(bare, cache):
    doc = broll.search(bare, "bread", providers=[prov(Pexels, StockHttp("pexels.json"), cache)], kind="photo", previews=False)
    key = "pexels:2284"
    from ave.assets import critic
    names = list(critic.load_config(ep=bare)["criteria"])
    v = broll.rescore(bare, doc["id"], key, {n: 2 for n in names})
    assert v["pass"] is True and v["unscored"] is False and v["verdict"] == "ok" and v["total"] == v["max"]
    bad = broll.rescore(bare, doc["id"], key, {n: 0 for n in names})
    assert bad["verdict"] == "reject" and bad["problems"]
    assert broll.get_search(bare, doc["id"])["candidates"][0]["critic"] == bad


def test_one_failing_provider_does_not_sink_the_search(bare, cache):
    bad = StockHttp(extra_routes=[{"method": "GET", "url": "pixabay.com", "status": 429, "response": {}}])
    doc = broll.search(bare, "bread", providers=[prov(Pexels, StockHttp("pexels.json"), cache), prov(Pixabay, bad, cache / "p")],
                       kind="photo", previews=False)
    assert len(doc["candidates"]) == 2 and doc["errors"] == [{"provider": "pixabay", "error": doc["errors"][0]["error"]}]
    assert "rate limit" in doc["errors"][0]["error"]
    with pytest.raises(ProviderError):
        broll.search(bare, "bread", providers=[prov(Pixabay, bad, cache / "q")], kind="photo", previews=False)


def test_query_budget_per_post(bare, cache):
    (bare.policy_dir).mkdir(exist_ok=True)
    meta = bare.read_meta()
    meta["broll"] = {"max_queries": 2}
    bare.meta_path.write_text(json.dumps(meta))
    p = prov(Pexels, StockHttp("pexels.json"), cache)
    for q in ("one", "two"):
        broll.search(bare, q, providers=[p], kind="photo", post="p_9", previews=False)
    with pytest.raises(broll.BudgetExceeded, match="max_queries 2"):
        broll.search(bare, "three", providers=[p], kind="photo", post="p_9", previews=False)
    broll.search(bare, "other post", providers=[p], kind="photo", post="p_10", previews=False)


def test_previews_are_fetched_once_into_the_search_folder(bare, cache):
    http = StockHttp("pexels.json", downloads={
        "https://images.pexels.com/photos/2284/pexels-photo-2284.jpeg?auto=compress&cs=tinysrgb&h=350": jpeg_bytes(),
        "https://images.pexels.com/photos/3301/m.jpeg": BytesResp(b"<html>nope</html>", ctype="text/html")})
    doc = broll.search(bare, "bread", providers=[prov(Pexels, http, cache)], kind="photo")
    first, second = doc["candidates"]
    assert first["preview_file"].endswith("pexels-2284.jpg") and (bare.root / first["preview_file"]).read_bytes()[:3] == b"\xff\xd8\xff"
    assert second["preview_file"] is None                                    # a non-image preview is dropped, not saved
    assert first["thumb"] == "/files/" + first["preview_file"] and first["thumb"].startswith("/files/work/broll/") and second["thumb"] is None
    assert "http" not in first["thumb"]


def test_thumbnails_are_small_local_jpegs(bare, cache):
    import io
    from PIL import Image
    big = io.BytesIO()
    Image.new("RGB", (4000, 6000), (200, 120, 60)).save(big, format="PNG")
    http = StockHttp("pexels.json", downloads={
        "https://images.pexels.com/photos/2284/pexels-photo-2284.jpeg?auto=compress&cs=tinysrgb&h=350": big.getvalue(),
        "https://images.pexels.com/photos/3301/m.jpeg": jpeg_bytes()})
    doc = broll.search(bare, "bread", providers=[prov(Pexels, http, cache)], kind="photo")
    c = doc["candidates"][0]
    f = bare.root / c["preview_file"]
    im = Image.open(f)
    assert im.format == "JPEG" and max(im.size) <= 360 and f.suffix == ".jpg" and f.stat().st_size < 20_000
    assert c["thumb"] == "/files/" + c["preview_file"]
    # served by the real /files/ route (work/ is a served prefix) and persisted in the search document
    assert broll.get_search(bare, doc["id"])["candidates"][0]["thumb"] == c["thumb"]


# ---------------------------------------------------------------------------------------------------- download

def search_then(bare, cache, downloads, *, provider_cls=Pexels, file="pexels.json", kind="photo"):
    http = StockHttp(file, downloads=downloads)
    p = prov(provider_cls, http, cache)
    doc = broll.search(bare, "bread", providers=[p], kind=kind, previews=False)
    return doc, p, http


def test_pick_downloads_logs_once_and_registers(bare, cache):
    orig = "https://images.pexels.com/photos/2284/pexels-photo-2284.jpeg"
    doc, p, http = search_then(bare, cache, {orig: jpeg_bytes()})
    res = broll.pick(bare, doc["id"], "pexels:2284", by="maren", providers=[p], now=FakeClock().now())
    rec = res["asset"]
    assert res["reused"] is False and rec["source"] == {"type": "stock", "provider": "pexels", "asset_id": "2284",
                                                         "url": "https://www.pexels.com/photo/bread-crust-on-a-wooden-board-2284/"}
    assert rec["file"].endswith(".jpg") and (bare.root / rec["file"]).is_file() and (rec["w"], rec["h"]) == (64, 48)
    assert rec["alt"].startswith("A loaf of bread") and rec["added_by"] == "maren" and rec["origin"]["search"] == doc["id"]
    entries = registry.license_log(bare).entries()
    assert len(entries) == 1
    e = entries[0]
    assert (e["source"], e["asset_id"], e["approved_by"], e["license"], e["query"]) == ("pexels", "2284", "maren", "Pexels License", "bread")
    assert e["sha256"] == rec["sha256"] and e["author"] == "A. Baker" and e["attribution"]["text"].startswith("Photo by A. Baker")
    assert e["terms_snapshot"]["verified"] is False
    assert res["license_entry"] == licenses.entry_hash(e) == rec["license"]["entry"]
    assert registry.license_log(bare).verify() == [] and lib.coverage(bare, [rec["id"]])["ok"]
    assert broll.get_search(bare, doc["id"])["picked"][0]["by"] == "maren"
    dl = [c for c in http.calls if c["url"] == orig][0]
    assert "headers" not in dl and "Authorization" not in json.dumps(dl, default=str)       # the key is not sent to the CDN
    again = broll.pick(bare, doc["id"], "pexels:2284", by="maren", providers=[p])
    assert again["reused"] is True and again["asset"]["id"] == rec["id"] and len(registry.license_log(bare).entries()) == 1
    assert [c["url"] for c in http.calls].count(orig) == 1                  # not downloaded twice
    assert_no_secret(bare.root)


def test_download_refuses_without_a_named_human_before_any_request(bare, cache):
    orig = "https://images.pexels.com/photos/2284/pexels-photo-2284.jpeg"
    doc, p, http = search_then(bare, cache, {orig: jpeg_bytes()})
    n = len(http.calls)
    cand = Candidate.from_dict(doc["candidates"][0])
    for who in ("", "   ", None):
        with pytest.raises(ValueError, match="approved_by is required"):
            broll.download(bare, cand, approved_by=who, provider=p)
    with pytest.raises(ValueError, match="by is required"):
        broll.pick(bare, doc["id"], "pexels:2284", by="", providers=[p])
    assert len(http.calls) == n
    assert not list((bare.library_dir).rglob("*.jsonl"))


@pytest.mark.parametrize("response", [
    BytesResp(b"", ctype="image/jpeg"),
    BytesResp(b"<!doctype html><title>blocked</title>", ctype="image/jpeg"),
    BytesResp(b"nope", status=404),
    BytesResp(b"plain text", ctype="text/plain"),
    BytesResp(b"\xff\xd8\xff\xe0 truncated, not a decodable jpeg", ctype="image/jpeg"),
])
def test_a_failed_download_leaves_nothing(bare, cache, response):
    orig = "https://images.pexels.com/photos/2284/pexels-photo-2284.jpeg"
    doc, p, http = search_then(bare, cache, {orig: response})
    cand = Candidate.from_dict(doc["candidates"][0])
    with pytest.raises((ProviderError, ValueError, OSError)):               # OSError: PIL refuses a corrupt image after the copy
        broll.download(bare, cand, approved_by="maren", provider=p, query="bread")
    files = [x for x in bare.library_dir.rglob("*") if x.is_file() and x.name != ".registration.lock"]
    assert files == [], files
    assert not [x for x in (bare.work / "broll").rglob("*") if x.is_file() and ".dl-" in str(x)]


def test_a_video_candidate_must_not_arrive_as_a_photo_and_vice_versa(bare, cache):
    orig = "https://images.pexels.com/photos/2284/pexels-photo-2284.jpeg"
    doc, p, _ = search_then(bare, cache, {orig: b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 40})
    with pytest.raises(ProviderError, match="expected a photo"):
        broll.download(bare, Candidate.from_dict(doc["candidates"][0]), approved_by="maren", provider=p)
    assert not (bare.library_dir / "licenses.jsonl").exists()


def test_license_log_failure_after_the_bytes_arrive_leaves_no_file(bare, cache, monkeypatch):
    orig = "https://images.pexels.com/photos/2284/pexels-photo-2284.jpeg"
    doc, p, _ = search_then(bare, cache, {orig: jpeg_bytes()})

    def boom(self, entry):
        raise OSError("disk full")
    monkeypatch.setattr(licenses.LicenseLog, "append", boom)
    with pytest.raises(OSError):
        broll.pick(bare, doc["id"], "pexels:2284", by="maren", providers=[p])
    assert [x for x in bare.library_dir.rglob("*") if x.is_file() and x.name != ".registration.lock"] == []


def test_unsplash_pick_pings_download_tracking_and_survives_a_failed_ping(bare, cache):
    orig = "https://images.unsplash.com/photo-1?q=85&fm=jpg"
    doc, p, http = search_then(bare, cache, {orig: jpeg_bytes()}, provider_cls=Unsplash, file="unsplash.json")
    res = broll.pick(bare, doc["id"], "unsplash:Dwu85P9SOIk", by="maren", providers=[p])
    assert any(c["url"].endswith("/photos/Dwu85P9SOIk/download") for c in http.fake.calls)
    e = registry.license_log(bare).entries()[0]
    assert e["attribution"]["required"] is True and "provider_notice" not in e and res["asset"]["source"]["provider"] == "unsplash"
    # second candidate: its tracking endpoint has no route -> the ping fails, the pick still lands and the failure is recorded
    http.downloads["https://images.unsplash.com/photo-2?w=1080"] = jpeg_bytes((1, 1, 1))
    res2 = broll.pick(bare, doc["id"], "unsplash:ab12", by="maren", providers=[p])
    assert res2["asset"]["id"] != res["asset"]["id"]
    assert "provider_notice" in registry.license_log(bare).entries()[1]


def test_video_pick_from_pexels_and_coverr(bare, cache):
    mp4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
    doc, p, _ = search_then(bare, cache, {"https://videos.pexels.com/video-files/8801/8801-hd_1920_1080_25fps.mp4": BytesResp(mp4, ctype="video/mp4")},
                            kind="video")
    res = broll.pick(bare, doc["id"], "pexels:8801", by="maren", providers=[p])
    assert res["asset"]["kind"] == "video" and res["asset"]["ext"] == ".mp4"
    assert registry.license_log(bare).entries()[0]["duration"] == 14
    doc2, p2, _ = search_then(bare, cache / "c", {"https://cdn.coverr.co/videos/cvr01/1080p.mp4?download=true": BytesResp(mp4 + b"1", ctype="video/mp4")},
                              provider_cls=Coverr, file="coverr.json", kind="video")
    r2 = broll.pick(bare, doc2["id"], "coverr:cvr01", by="maren", providers=[p2])
    assert r2["asset"]["source"]["provider"] == "coverr" and len(registry.license_log(bare).entries()) == 2


def test_pick_of_an_unknown_candidate(bare, cache):
    doc, p, _ = search_then(bare, cache, {})
    with pytest.raises(KeyError, match="not in search"):
        broll.pick(bare, doc["id"], "pexels:999", by="maren", providers=[p])
    with pytest.raises(KeyError):
        broll.get_search(bare, "s_20260101_0000")


# ---------------------------------------------------------------------------------------------------- own library as a provider

def test_local_provider_searches_the_library_and_picking_logs_nothing_new(bare, cache):
    from studio_helpers import make_png
    a = lib.add_file(bare, make_png(bare.work / "l.png", size=(50, 80), color=(9, 9, 9)), source={"type": "own"}, by="maren", tags=["bread"],
                     alt="Loaf close up.")
    lib.add_file(bare, make_png(bare.work / "l2.png", size=(80, 50), color=(8, 8, 8)), source={"type": "own"}, by="maren", tags=["bread"])
    doc = broll.search(bare, "bread", providers=[Local(bare)], kind="photo", orientation="portrait", previews=False)
    assert [c["asset_id"] for c in doc["candidates"]] == [a["id"]]
    assert doc["candidates"][0]["provider"] == "local" and doc["candidates"][0]["preview_url"] == "/files/" + a["file"]
    n = len(registry.license_log(bare).entries())
    res = broll.pick(bare, doc["id"], f"local:{a['id']}", by="maren", providers=[Local(bare)])
    assert res["reused"] is True and res["asset"]["id"] == a["id"] and len(registry.license_log(bare).entries()) == n


# ---------------------------------------------------------------------------------------------------- queries

def test_queries_are_mechanical_capped_and_deterministic():
    text = "Sourdough starter day three. The sourdough starter smells like pears, warm kitchen, morning bread."
    a = queries.generate(text, post_type="reel")
    assert a == queries.generate(text, post_type="reel")
    qs = [q["q"] for q in a["queries"]]
    assert qs[0] == "sourdough starter" and len(qs) == a["max_queries"] == 6 and len(set(qs)) == len(qs)
    assert all(q["kind"] == "video" and q["orientation"] == "portrait" and q["why"] for q in a["queries"])
    assert {"morning", "warm"} <= set(a["moods"]) and "mechanical draft" in a["note"]
    wide = queries.generate(text, post_type="video", max_queries=2)
    assert len(wide["queries"]) == 2 and wide["dropped"] > 0 and wide["queries"][0]["orientation"] == "landscape"
    assert queries.generate("", post_type="feed")["queries"] == []
    assert queries.generate(text, kind="photo", orientation="square")["queries"][0]["kind"] == "photo"
    hinted = queries.generate("bread bread", mood_hints=["quiet"])
    assert "quiet" in hinted["moods"] and any("empty room" in q["q"] for q in hinted["queries"])
    with pytest.raises(ValueError):
        queries.generate(text, kind="gif")


def test_queries_for_a_post_and_per_studio_mood_override(built):
    sp = built.sp
    qs = queries.for_post(sp, built.posts["scheduled"])
    assert qs["queries"] and all(q["kind"] == "photo" for q in qs["queries"])        # feed variant
    (sp.policy_dir).mkdir(exist_ok=True)
    (sp.policy_dir / "moods.yaml").write_text("max_queries: 1\nmoods:\n  loud:\n    triggers: [starter]\n    modifiers: [crowd shouting]\n")
    out = queries.for_post(sp, built.posts["scheduled"])
    assert out["max_queries"] == 1 and len(out["queries"]) == 1


# ---------------------------------------------------------------------------------------------------- facade hooks

def test_facade_hooks_search_and_pick(built, cache):
    http = StockHttp("pexels.json", downloads={"https://images.pexels.com/photos/2284/pexels-photo-2284.jpeg": jpeg_bytes()})
    studio = Studio(built.sp, clock=built.clock, providers={"pexels": prov(Pexels, http, cache)})
    broll.register(studio)
    out = studio.broll_search(query="bread crust", providers=["pexels"], kind="photo", post=built.posts["draft"])
    sid = out["search"]["id"]
    assert studio.broll(sid)["candidates"][0]["critic"]["verdict"] == "ok"
    picked = studio.broll_pick(sid, "pexels:2284", "maren")
    assert picked["asset"]["source"]["provider"] == "pexels" and picked["license_entry"] == picked["asset"]["license"]["entry"]
    assert picked["asset"]["added"].startswith("2026-10-19")                         # FakeClock, not wall time
    with pytest.raises(ValueError):
        studio.broll_pick(sid, "pexels:2284", " ")


def test_no_key_value_survives_anywhere_in_the_studio_or_cache(built, cache):
    http = StockHttp("pixabay.json", downloads={"https://pixabay.com/get/g1f2e3d_1280.jpg": jpeg_bytes((4, 5, 6))})
    p = prov(Pixabay, http, cache)
    doc = broll.search(built.sp, "bread", providers=[p], kind="photo", previews=False)
    broll.pick(built.sp, doc["id"], "pixabay:195893", by="maren", providers=[p])
    assert http.calls[0]["params"]["key"] == KEY                                     # sent to Pixabay (its API takes a query key) ...
    assert_no_secret(built.sp.root)                                                  # ... and kept nowhere
    assert_no_secret(cache)
    assert fetch.sniff_ext(jpeg_bytes()[:16]) == ".jpg"
