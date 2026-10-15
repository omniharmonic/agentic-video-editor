import json
from datetime import datetime, timedelta, timezone

import httplib2
import pytest
import yaml
from googleapiclient.errors import HttpError
from publish_helpers import *  # noqa: F401,F403
from publish_helpers import FakeResp, FakeSession

from ave.publish import queue, safety
from ave.publish.adapters import get_publisher
from ave.publish.adapters.aggregator import AggregatorPublisher, render
from ave.publish.adapters.base import Absent, PublishError, Uncertain, load_platforms
from ave.publish.adapters.facebook import FacebookPublisher
from ave.publish.adapters.youtube import YouTubePublisher


def item(**kw):
    d = {"id": "i1", "title": "Title", "description": "d", "tags": ["a"], "caption": "", "schedule": None,
         "visibility": "private", "ai_disclosure": False, "made_for_kids": False, "format": "auto", "platforms": []}
    d.update(kw)
    return d


def http_error(status, reason=""):
    body = json.dumps({"error": {"errors": [{"reason": reason}], "message": reason}}).encode()
    return HttpError(httplib2.Response({"status": status}), body)


# ------------------------------------------------------------------ platforms.yaml
def test_platforms_yaml_marks_verification():
    cfg = load_platforms()
    for name in ("youtube", "facebook", "instagram", "x", "tiktok"):
        assert cfg[name]["verified"] in (True, False) and cfg[name]["source"].startswith("https://")


def test_registry():
    assert get_publisher("youtube").name == "youtube"
    assert get_publisher("instagram").network == "instagram"
    with pytest.raises(ValueError, match="not implemented"):
        get_publisher("myspace")


# ------------------------------------------------------------------ preflight
def test_facebook_reel_rejects_16x9(wide):
    p = FacebookPublisher(session=FakeSession(None)).preflight(item(format="short"), wide)
    assert any("vertical" in x for x in p)


def test_facebook_vertical_short_ok(vertical):
    # 4 s vertical but 360x640 is under the 540x960 reel minimum: must be flagged, not silently accepted
    p = FacebookPublisher(session=FakeSession(None)).preflight(item(), vertical)
    assert any("width" in x for x in p) and not any("duration" in x for x in p)


def test_youtube_short_too_long_and_aspect(video_pool, wide):
    yt = YouTubePublisher(service_factory=lambda: None)
    p = yt.preflight(item(format="short"), wide)          # horizontal declared as short
    assert any("vertical" in x or "square" in x for x in p)
    cfg = load_platforms()
    cfg["youtube"]["long"]["max_duration_s"] = 2
    p = YouTubePublisher(platforms_cfg=cfg).preflight(item(), wide)
    assert any("exceeds max" in x for x in p)


def test_youtube_requires_ai_disclosure_and_title_rules(wide):
    yt = YouTubePublisher()
    assert any("ai_disclosure" in x for x in yt.preflight(item(ai_disclosure=None), wide))
    assert any("title" in x for x in yt.preflight(item(title="x" * 101), wide))
    assert any("<" in x for x in yt.preflight(item(title="a<b"), wide))
    assert yt.preflight(item(), wide) == []


def test_schedule_rules(wide):
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    yt = YouTubePublisher(now=lambda: now)
    assert any("past" in x for x in yt.preflight(item(schedule="2026-10-01T00:00:00+00:00"), wide))
    assert any("timezone" in x for x in yt.preflight(item(schedule="2026-12-01T00:00:00"), wide))
    assert yt.preflight(item(schedule="2026-12-01T00:00:00+00:00"), wide) == []
    fb = FacebookPublisher(now=lambda: now)
    soon = (now + timedelta(minutes=2)).isoformat()
    assert any("at least" in x for x in fb.preflight(item(schedule=soon), wide))


def test_bad_file_is_a_problem(tmp_path):
    f = tmp_path / "x.mp4"
    f.write_text("not a video")
    assert any("ffprobe" in x for x in YouTubePublisher().preflight(item(), f))


# ------------------------------------------------------------------ YouTube flow
class FakeReq:
    def __init__(self, steps):
        self.steps = list(steps)
        self.resumable_uri = None
        self.calls = 0

    def next_chunk(self):
        self.calls += 1
        s = self.steps.pop(0)
        if isinstance(s, Exception):
            raise s
        self.resumable_uri = "https://upload.example/session/xyz"
        return s


class FakeYT:
    def __init__(self, steps, uploads=None):
        self.req = FakeReq(steps)
        self.inserted = None
        self.uploads = uploads or []

    def videos(self):
        outer = self

        class V:
            def insert(self, part, body, media_body):
                outer.inserted = (part, body, media_body)
                return outer.req
        return V()

    def channels(self):
        class C:
            def list(self, **kw):
                return type("E", (), {"execute": lambda s: {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]}})()
        return C()

    def playlistItems(self):
        outer = self

        class P:
            def list(self, **kw):
                return type("E", (), {"execute": lambda s: {"items": outer.uploads}})()
        return P()


def yt_pub(svc):
    return YouTubePublisher(service_factory=lambda: svc, media_factory=lambda path: ("media", path))


def test_youtube_resumable_upload_flow(wide):
    svc = FakeYT([(None, None), (None, None), (None, {"id": "VID1", "status": {"privacyStatus": "private"}})])
    seen = []
    res = yt_pub(svc).publish(item(visibility="private", ai_disclosure=True, made_for_kids=False, tags=["t"]), wide, seen.append)
    assert res["id"] == "VID1" and res["url"] == "https://youtu.be/VID1" and res["warnings"] == []
    part, body, media = svc.inserted
    assert part == "snippet,status" and media == ("media", str(wide))
    assert body["status"] == {"privacyStatus": "private", "selfDeclaredMadeForKids": False, "containsSyntheticMedia": True}
    assert body["snippet"]["title"] == "Title" and svc.req.calls == 3
    assert seen == [{"upload_uri": "https://upload.example/session/xyz"}]    # persisted for crash recovery


def test_youtube_schedule_forces_private_with_publish_at(wide):
    svc = FakeYT([(None, {"id": "V", "status": {"privacyStatus": "private"}})])
    yt_pub(svc).publish(item(visibility="public", schedule="2026-12-01T09:00:00+00:00"), wide, lambda p: None)
    st = svc.inserted[1]["status"]
    assert st["privacyStatus"] == "private" and st["publishAt"] == "2026-12-01T09:00:00+00:00"


def test_youtube_locked_private_is_surfaced_plainly(wide):
    svc = FakeYT([(None, {"id": "V", "status": {"privacyStatus": "private"}})])
    res = yt_pub(svc).publish(item(visibility="public"), wide, lambda p: None)
    assert res["warnings"] and "FORCED PRIVATE" in res["warnings"][0] and "audit" in res["warnings"][0]


def test_youtube_quota_exceeded_is_definite_failure(wide):
    svc = FakeYT([http_error(403, "quotaExceeded")])
    with pytest.raises(PublishError, match="quota"):
        yt_pub(svc).publish(item(), wide, lambda p: None)


def test_youtube_5xx_retries_same_upload_then_succeeds(wide):
    svc = FakeYT([http_error(503), (None, {"id": "V", "status": {"privacyStatus": "private"}})])
    assert yt_pub(svc).publish(item(), wide, lambda p: None)["id"] == "V"
    assert svc.req.calls == 2


def test_youtube_persistent_5xx_is_uncertain(wide):
    svc = FakeYT([http_error(503)] * 7)
    with pytest.raises(Uncertain):
        yt_pub(svc).publish(item(), wide, lambda p: None)


def test_youtube_recover_found_and_absent():
    started = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    up = [{"snippet": {"title": "Title", "publishedAt": "2999-01-01T00:00:00Z", "resourceId": {"videoId": "FOUND"}}}]
    r = yt_pub(FakeYT([], uploads=up)).recover(item(), {"started": started})
    assert r["id"] == "FOUND"
    old = [{"snippet": {"title": "Title", "publishedAt": "2000-01-01T00:00:00Z", "resourceId": {"videoId": "OLD"}}}]
    assert isinstance(yt_pub(FakeYT([], uploads=old)).recover(item(), {"started": started}), Absent)

    class Boom(FakeYT):
        def channels(self):
            raise RuntimeError("net")
    assert yt_pub(Boom([])).recover(item(), {"started": started}) is None


def test_youtube_ready_problems_and_status_hide_secrets(isolated_secrets):
    yt = YouTubePublisher()
    assert yt.ready_problems()
    safety.set_secret("youtube_credentials", json.dumps({"client_id": "cid", "client_secret": "csecretvalue", "refresh_token": "rtokenvalue123"}))
    assert YouTubePublisher().ready_problems() == []
    assert "rtokenvalue123" not in json.dumps(YouTubePublisher().status())


# ------------------------------------------------------------------ Facebook flows
def fb_session(fail_at=None, status=200):
    def h(method, url, kw):
        data = kw.get("data")
        phase = data.get("upload_phase") if isinstance(data, dict) else None
        if "rupload" in url:
            return FakeResp({"success": True})
        if url.endswith("/video_reels"):
            if phase == "start":
                return FakeResp({"video_id": "R1", "upload_url": "https://rupload.facebook.com/video-upload/v21.0/R1"})
            if fail_at == "finish":
                return FakeResp({"error": {"message": "nope", "code": 100}}, 400) if status == 400 else FakeResp({}, 502)
            return FakeResp({"success": True})
        if url.endswith("/videos"):
            if phase == "start":
                return FakeResp({"upload_session_id": "S1", "video_id": "L1", "start_offset": "0", "end_offset": "100"})
            if phase == "transfer":
                return FakeResp({"start_offset": "100", "end_offset": "100"})
            return FakeResp({"success": True})
        return FakeResp({}, 404)
    return FakeSession(h)


@pytest.fixture
def fb_creds(monkeypatch):
    monkeypatch.setenv("AVE_FACEBOOK_PAGE_TOKEN", "EAAPAGETOKEN1234567890abc")
    monkeypatch.setenv("AVE_FACEBOOK_PAGE_ID", "12345")


def test_facebook_reel_three_step_flow(vertical, fb_creds):
    s = fb_session()
    seen = []
    res = FacebookPublisher(session=s).publish(item(visibility="public", caption="cap"), vertical, seen.append)
    methods = [(c[0], c[1].split("/")[-1], (c[2].get("data") or {}).get("upload_phase") if isinstance(c[2].get("data"), dict) else "bytes") for c in s.calls]
    assert [m[2] for m in methods] == ["start", "bytes", "finish"]
    up = s.calls[1]
    assert up[2]["headers"]["Authorization"] == "OAuth EAAPAGETOKEN1234567890abc" and up[2]["headers"]["file_size"] == str(vertical.stat().st_size)
    fin = s.calls[2][2]["data"]
    assert fin["video_state"] == "PUBLISHED" and fin["video_id"] == "R1" and "cap" in fin["description"]
    assert res["id"] == "R1" and res["live"] is True
    assert seen[0]["video_id"] == "R1"
    assert "/v21.0/12345/video_reels" in s.calls[0][1]


def test_facebook_private_default_becomes_draft_never_live(vertical, fb_creds):
    s = fb_session()
    res = FacebookPublisher(session=s).publish(item(visibility="private"), vertical, lambda p: None)
    assert s.calls[2][2]["data"]["video_state"] == "DRAFT" and res["live"] is False


def test_facebook_schedule(vertical, fb_creds):
    s = fb_session()
    FacebookPublisher(session=s).publish(item(schedule="2030-01-01T00:00:00+00:00"), vertical, lambda p: None)
    fin = s.calls[2][2]["data"]
    assert fin["video_state"] == "SCHEDULED" and fin["scheduled_publish_time"] == 1893456000


def test_facebook_longform_chunked(wide, fb_creds):
    s = fb_session()
    res = FacebookPublisher(session=s).publish(item(visibility="public"), wide, lambda p: None)
    phases = [c[2]["data"]["upload_phase"] for c in s.calls]
    assert phases == ["start", "transfer", "finish"]
    assert s.calls[1][2]["files"]["video_file_chunk"][1] == wide.read_bytes()[:100]
    assert s.calls[2][2]["data"]["published"] == "true" and res["id"] == "L1"


def test_facebook_error_before_finish_is_definite(vertical, fb_creds):
    def h(method, url, kw):
        return FakeResp({"error": {"message": "bad token", "code": 190}}, 400)
    with pytest.raises(PublishError) as e:
        FacebookPublisher(session=FakeSession(h)).publish(item(), vertical, lambda p: None)
    assert "EAAPAGETOKEN" not in str(e.value) and "Nothing was posted" in str(e.value)


def test_facebook_finish_5xx_is_uncertain_but_400_is_definite(vertical, fb_creds):
    with pytest.raises(Uncertain):
        FacebookPublisher(session=fb_session("finish", 502)).publish(item(), vertical, lambda p: None)
    with pytest.raises(PublishError):
        FacebookPublisher(session=fb_session("finish", 400)).publish(item(), vertical, lambda p: None)


def test_facebook_finish_network_error_is_uncertain(vertical, fb_creds):
    class S(FakeSession):
        def post(self, url, **kw):
            if kw.get("data") and isinstance(kw["data"], dict) and kw["data"].get("upload_phase") == "finish":
                raise TimeoutError("timed out")
            return super().post(url, **kw)
    base = fb_session()
    with pytest.raises(Uncertain):
        FacebookPublisher(session=S(base.handler)).publish(item(), vertical, lambda p: None)


def test_facebook_without_credentials(vertical):
    p = FacebookPublisher(session=FakeSession(None))
    assert len(p.ready_problems()) == 2
    with pytest.raises(PublishError):
        p.publish(item(), vertical, lambda x: None)


def test_facebook_recover(fb_creds):
    s = FakeSession(lambda m, u, kw: FakeResp({"status": {"video_status": "ready"}}))
    assert FacebookPublisher(session=s).recover(item(), {"video_id": "R1"})["id"] == "R1"
    s = FakeSession(lambda m, u, kw: FakeResp({"status": {"video_status": "processing"}}))
    assert FacebookPublisher(session=s).recover(item(), {"video_id": "R1"}) is None
    assert FacebookPublisher(session=s).recover(item(), {}) is None


# ------------------------------------------------------------------ Aggregator
CONF = {"base_url": "https://agg.example", "auth": {"header": "X-Key", "prefix": "Key "}, "networks": {"instagram": "ig"},
        "supports_visibility": False,
        "upload": {"path": "/media", "id_path": "data.id", "url_path": "data.url"},
        "post": {"path": "/post", "json": {"net": ["{network}"], "text": "{caption}", "media": "{media_url}", "tags": "{tags}"},
                 "id_path": "post.id", "url_path": "post.url"}}


def agg_session():
    def h(method, url, kw):
        if url.endswith("/media"):
            return FakeResp({"data": {"id": "M1", "url": "https://cdn/m1.mp4"}})
        return FakeResp({"post": {"id": "P1", "url": "https://ig/p1"}})
    return FakeSession(h)


def test_render_templates():
    assert render({"a": "{x}", "b": "hi {x}", "c": ["{l}"], "d": "{l}"}, {"x": "X", "l": [1, 2]}) == \
        {"a": "X", "b": "hi X", "c": [[1, 2]], "d": [1, 2]}


def test_aggregator_flow_uses_config_only(vertical, isolated_secrets):
    safety.set_secret("aggregator_api_key", "agg-key-123456")
    s = agg_session()
    a = AggregatorPublisher(network="instagram", config=CONF, session=s)
    res = a.publish(item(visibility="public", caption="cap", tags=["a"]), vertical, lambda p: None)
    assert res["id"] == "P1" and res["url"] == "https://ig/p1"
    assert s.calls[0][2]["headers"] == {"X-Key": "Key agg-key-123456"}
    assert s.calls[1][2]["json"] == {"net": ["ig"], "text": "cap", "media": "https://cdn/m1.mp4", "tags": ["a"]}
    assert s.calls[1][1] == "https://agg.example/post"


def test_aggregator_blocks_private_and_http_and_missing(vertical, isolated_secrets):
    a = AggregatorPublisher(network="instagram", config=CONF)
    assert any("cannot hold a post back" in x for x in a.preflight(item(visibility="private"), vertical))
    bad = dict(CONF, base_url="http://evil.example")
    assert any("https" in x for x in AggregatorPublisher(network="instagram", config=bad).ready_problems())
    empty = AggregatorPublisher(network="x", config={})
    assert any("no aggregator config" in x for x in empty.ready_problems())
    assert any("API key" in x for x in a.ready_problems())


def test_aggregator_post_failures(vertical, isolated_secrets):
    safety.set_secret("aggregator_api_key", "agg-key-123456")
    def mk(code, post_exc=False):
        def h(method, url, kw):
            if url.endswith("/media"):
                return FakeResp({"data": {"id": "M1", "url": "u"}})
            if post_exc:
                raise TimeoutError("t/o")
            return FakeResp({"err": "x"}, code)
        return AggregatorPublisher(network="instagram", config=CONF, session=FakeSession(h))
    with pytest.raises(PublishError):
        mk(400).publish(item(), vertical, lambda p: None)
    with pytest.raises(Uncertain):
        mk(502).publish(item(), vertical, lambda p: None)
    with pytest.raises(Uncertain):
        mk(0, True).publish(item(), vertical, lambda p: None)


def test_example_config_parses_and_has_no_live_endpoint():
    from pathlib import Path
    cfg = yaml.safe_load((Path(__file__).parents[1] / "config" / "aggregator.example.yaml").read_text())
    assert ".invalid" in cfg["base_url"] and cfg["post"]["path"]
