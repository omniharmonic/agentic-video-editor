"""The shared fakes themselves (so WS1-WS7 can trust them): clock, recording publisher, scripted HTTP, host, offline guard, builders."""

import json
import socket
from datetime import datetime, timezone

import pytest
from studio_helpers import (FakeClock, FakeHost, FakeHttp, RecordingPublisher, add_asset, approve_item, build_studio, make_png,
                            queue_variant)

from ave.publish import queue as pq
from ave.publish.adapters.base import PublishError, Uncertain
from ave.studio.clock import SystemClock, iso_utc, parse_ts


def test_fake_clock_only_moves_when_told():
    c = FakeClock("2026-11-01T07:30:00Z")
    assert c.now() == datetime(2026, 11, 1, 7, 30, tzinfo=timezone.utc) == c.now()
    c.advance(hours=1, minutes=5)
    c.sleep(30)
    assert iso_utc(c.now()) == "2026-11-01T08:35:30Z" and c.slept == [30]
    with pytest.raises(AssertionError):
        FakeClock(datetime(2026, 1, 1))                       # naive time is refused


def test_system_clock_is_aware_and_timestamps_parse_every_form_the_repo_writes():
    assert SystemClock().now().tzinfo is not None
    want = datetime(2026, 11, 2, 16, 0, tzinfo=timezone.utc)
    for s in ("2026-11-02T16:00:00Z", "2026-11-02T16:00:00+00:00", "2026-11-02T09:00:00-0700", "2026-11-02T09:00:00-07:00"):
        assert parse_ts(s) == want
    for bad in ("2026-11-02T09:00", "", None, "soon"):
        with pytest.raises(ValueError):
            parse_ts(bad)


def test_recording_publisher_scripts_outcomes(tmp_path):
    item = {"id": "q", "title": "t", "caption": "c", "account": "a"}
    noop = lambda partial: None
    p = RecordingPublisher("x", behaviors=["ok", "refuse", "uncertain", "network", "crash", "crash_after_send", "ok"])
    assert p.publish(item, None, noop)["id"] == "x-1"
    with pytest.raises(PublishError):
        p.publish(item, None, noop)
    with pytest.raises(Uncertain):
        p.publish(item, None, noop)
    with pytest.raises(ConnectionError):
        p.publish(item, None, noop)
    with pytest.raises(KeyboardInterrupt):
        p.publish(item, None, noop)
    n = len(p.sent)
    with pytest.raises(KeyboardInterrupt):
        p.publish(item, None, noop)
    assert len(p.sent) == n + 1                               # crash_after_send: the post reached the platform
    assert p.publish(item, None, noop) and p.publish_calls == 7 and p.http_calls == []
    assert RecordingPublisher("x", live=False).ready_problems() == ["account is not live (no valid live attestation)"]
    assert RecordingPublisher("x", missing_credentials=["token missing"]).ready_problems() == ["token missing"]


def test_fake_http_matches_asserts_body_shape_and_refuses_the_unexpected(tmp_path):
    routes = [{"method": "POST", "url": "/xrpc/com.atproto.server.createSession", "response": {"accessJwt": "j"},
               "expect": {"json_keys": ["identifier", "password"]}},
              {"method": "POST", "url": "/xrpc/com.atproto.repo.createRecord", "status": 429, "response": {"error": "RateLimit"}, "times": 1}]
    h = FakeHttp(routes)
    assert h.post("https://bsky.invalid/xrpc/com.atproto.server.createSession", json={"identifier": "a", "password": "b"}).json() == {"accessJwt": "j"}
    with pytest.raises(AssertionError, match="lacks 'password'"):
        h.post("https://bsky.invalid/xrpc/com.atproto.server.createSession", json={"identifier": "a"})
    assert h.post("https://bsky.invalid/xrpc/com.atproto.repo.createRecord", json={}).status_code == 429
    with pytest.raises(AssertionError, match="unexpected HTTP call"):
        h.post("https://bsky.invalid/xrpc/com.atproto.repo.createRecord", json={})        # the route was single-use
    with pytest.raises(AssertionError, match="unexpected HTTP call"):
        h.get("https://elsewhere.invalid/")
    assert [c["method"] for c in h.calls] == ["POST", "POST", "POST", "POST", "GET"]
    f = tmp_path / "r.json"
    f.write_text(json.dumps(routes))
    assert FakeHttp(json.loads(f.read_text())).routes[0]["url"].endswith("createSession")


def test_fake_host_records_puts_and_deletes(tmp_path):
    png = make_png(tmp_path / "a.png")
    h = FakeHost()
    rec = h.put(png)
    assert rec["url"].startswith("https://media.example.invalid/") and rec["sha256"] in rec["key"]
    h.delete(rec["key"])
    assert h.deleted == [rec["key"]] and FakeHost(corrupt=True).put(png)["sha256"] == "0" * 64


def test_offline_guard_blocks_real_network_but_not_loopback():
    s = socket.socket()
    try:
        with pytest.raises(AssertionError, match="network access attempted"):
            s.connect(("93.184.216.34", 80))
    finally:
        s.close()
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    c = socket.socket()
    try:
        c.connect(srv.getsockname())                           # loopback is allowed
    finally:
        c.close()
        srv.close()


def test_build_studio_covers_every_lifecycle_and_leaves_valid_signed_approvals(built):
    assert sorted(built.posts) == ["approved", "draft", "idea", "in_review", "scheduled", "variant"]
    for kind in ("in_review", "scheduled", "approved"):
        post = built.studio.post(built.posts[kind])
        item = pq.load(built.sp, post["variants"][0]["queue"]["id"])
        assert item["meta_v"] == 2 and item["account"] == post["variants"][0]["account"]
        assert (pq.send_blockers(built.sp, item) == []) == (kind != "in_review")
    sched = pq.load(built.sp, built.studio.post(built.posts["scheduled"])["variants"][0]["queue"]["id"])
    assert sched["schedule"] and sched["approvals"][0]["by"] == "maren"


def test_build_studio_is_deterministic_in_ids_and_sizes(tmp_path):
    a = build_studio(tmp_path / "a", accounts=2, posts=6)
    b = build_studio(tmp_path / "b", accounts=2, posts=6)
    assert a.post_ids == b.post_ids == [f"p_fx{i:03d}" for i in range(6)]
    assert [x["id"] for x in a.assets] == [x["id"] for x in b.assets] and len(a.accounts) == 2


def test_approval_helper_calls_the_queue_function_and_no_environment_switch(tmp_path, monkeypatch):
    b = build_studio(tmp_path, accounts=1, posts=4)
    post = b.studio.post(b.posts["in_review"])
    item = pq.load(b.sp, post["variants"][0]["queue"]["id"])
    assert item["status"] == "awaiting_approval"
    approve_item(b.sp, item["id"])
    assert pq.load(b.sp, item["id"])["status"] == "approved"
