"""WS2 core: accounts, the live attestation, the registry (E3), per-account secrets (E4), config discipline (E6), the media host, text metrics."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
import yaml
from studio_helpers import FakeClock, FakeHttp, FakeResp, write_account

from ave import humanpresence
from ave.platforms import attest, registry
from ave.platforms.accounts import Account, AccountError, check_accounts, find_secrets, list_accounts, load_account, save_account, secret_name
from ave.platforms.host import S3Host, host_status, load_host, presign
from ave.platforms.textlen import graphemes, url_spans, with_margin, x_weighted_len
from ave.publish import queue as pq, safety
from ave.publish.adapters import ALL_PLATFORMS, get_publisher
from ave.publish.adapters.base import PublishError, load_platforms


# ------------------------------------------------------------------------------------------------ accounts

def cfg(**kw):
    return {"id": "ig-main", "platform": "instagram", "mode": "graph", "label": "@acmebakery", "tz": "America/Denver",
            "post_types": ["feed"], "credentials": {"ref": "ig-main", "ig_user_id": "1784"}, **kw}


def test_account_loads_validated_and_names_secrets_per_account(bare, monkeypatch):
    save_account(bare, cfg())
    a = load_account(bare, "ig-main")
    assert a.platform == "instagram" and a.bound and a.cred_ref == "ig-main" and a.public == {"ig_user_id": "1784"}
    assert a.secret_name("token") == "instagram_token@ig-main" == secret_name("instagram", "token", "ig-main")
    assert a.secret("token") is None and a.missing(("token", "ig_user_id")) == ["instagram_token@ig-main"]
    monkeypatch.setenv("AVE_INSTAGRAM_TOKEN_IG_MAIN", "tok-from-env-123456")          # the documented env spelling
    assert a.secret("token") == "tok-from-env-123456" and a.credential("ig_user_id") == "1784"
    assert Account.unbound("instagram").secret_name("token") == "instagram_token", "unbound adapters keep the legacy names"


def test_two_accounts_on_one_platform_do_not_share_secrets(bare):
    save_account(bare, cfg())
    save_account(bare, cfg(id="ig-alt", credentials={"ref": "ig-alt", "ig_user_id": "9"}))
    safety.set_secret(load_account(bare, "ig-main").secret_name("token"), "main-token-value-1")
    assert load_account(bare, "ig-alt").secret("token") is None
    assert load_account(bare, "ig-main").secret("token") == "main-token-value-1"


def test_an_account_file_may_not_hold_a_secret(bare):
    with pytest.raises(AccountError, match="credentials may only hold"):
        save_account(bare, cfg(credentials={"ref": "ig-main", "access_token": "x"}))
    with pytest.raises(AccountError, match="looks like a secret"):
        save_account(bare, cfg(label="EAAB" + "q" * 40))
    with pytest.raises(AccountError):
        save_account(bare, {"id": "x", "platform": "nope", "mode": "graph"})
    assert not list(bare.accounts_dir.glob("*.yaml"))


def test_broken_account_files_are_reported_not_hidden(bare):
    save_account(bare, cfg())
    (bare.accounts_dir / "bad.yaml").write_text("id: bad\nplatform: [unclosed\n")
    (bare.accounts_dir / "wrongid.yaml").write_text(yaml.safe_dump(cfg(id="other")))
    assert [a.id for a in list_accounts(bare)] == ["ig-main"]
    assert set(check_accounts(bare)) == {"bad", "wrongid"}
    with pytest.raises(AccountError):
        load_account(bare, "../escape")


def test_find_secrets_names_files_never_values(tmp_path):
    (tmp_path / "ok.yaml").write_text("label: fine\n")
    (tmp_path / "leak.yaml").write_text("note: " + "EAAB" + "z" * 30 + "\n")
    hits = find_secrets(tmp_path)
    assert hits == [str(tmp_path / "leak.yaml")]


# ------------------------------------------------------------------------------------------------ live attestation

def test_attestation_round_trip_and_rejects_every_tamper(bare):
    save_account(bare, cfg())
    d = bare.accounts_dir
    assert attest.verify_live(d, "ig-main").reason.startswith("no live attestation")
    rec = attest.write_live(d, "ig-main", "maren")
    st = attest.verify_live(d, "ig-main")
    assert st.ok and st.by == "maren" and rec["sig"] and len(rec["sig"]) == 64
    p = attest.live_path(d, "ig-main")
    good = json.loads(p.read_text())
    for field, value in (("by", "claude"), ("at", "2030-01-01T00:00:00+0000"), ("account", "other"), ("live", False), ("sig", "0" * 64), ("sig", None)):
        p.write_text(json.dumps({**good, field: value}))
        assert not attest.verify_live(d, "ig-main").ok, field
    p.write_text("not json")
    assert not attest.verify_live(d, "ig-main").ok
    p.write_text(json.dumps({k: v for k, v in good.items() if k != "sig"}))
    assert not attest.verify_live(d, "ig-main").ok
    p.write_text(json.dumps(good))
    assert attest.verify_live(d, "ig-main").ok
    assert load_account(bare, "ig-main").live_ok()
    assert attest.revoke_live(d, "ig-main") and not load_account(bare, "ig-main").live_ok()


def test_signed_off_file_cannot_be_hand_flipped_to_on(bare):
    save_account(bare, cfg())
    d = bare.accounts_dir
    rec = attest.write_live(d, "ig-main", "maren", live=False)
    assert rec["live"] is False and not attest.verify_live(d, "ig-main").ok
    assert "signed off" in attest.verify_live(d, "ig-main").reason
    p = attest.live_path(d, "ig-main")
    p.write_text(json.dumps({**rec, "live": True}))
    st = attest.verify_live(d, "ig-main")
    assert not st.ok and "signature invalid" in st.reason and not load_account(bare, "ig-main").live_ok()
    # an old-format signature (no live flag in the HMAC) is not live either
    import hashlib, hmac
    from ave.publish import safety
    old = hmac.new(safety.approval_key(), f"live|ig-main|maren|{rec['at']}".encode(), hashlib.sha256).hexdigest()
    p.write_text(json.dumps({**rec, "live": True, "sig": old}))
    assert not attest.verify_live(d, "ig-main").ok


def test_dispatch_and_attest_share_one_implementation(bare):
    from ave.studio import dispatch
    assert not hasattr(dispatch, "live_signature") and not hasattr(dispatch, "live_path")
    save_account(bare, cfg())
    assert dispatch.account_live(bare, "ig-main") is False
    attest.write_live(bare.accounts_dir, "ig-main", "maren")
    assert dispatch.account_live(bare, "ig-main") is True
    p = attest.live_path(bare.accounts_dir, "ig-main")
    p.write_text(json.dumps({**json.loads(p.read_text()), "by": "claude"}))
    assert dispatch.account_live(bare, "ig-main") is False and attest.verify_live(bare.accounts_dir, "ig-main").ok is False


def test_attestation_is_bound_to_the_approval_key(bare, monkeypatch):
    save_account(bare, cfg())
    attest.write_live(bare.accounts_dir, "ig-main", "maren")
    monkeypatch.setenv("AVE_APPROVAL_KEY", "a-different-key-entirely-0123456789")
    assert not attest.verify_live(bare.accounts_dir, "ig-main").ok, "a file signed with another key must not verify"


def test_enabling_live_is_a_human_act(bare, monkeypatch):
    save_account(bare, cfg())
    monkeypatch.setattr(humanpresence, "is_human_terminal", lambda: False)
    with pytest.raises(SystemExit, match="human act"):
        attest.account_live_command(bare, "ig-main", on=True, by="maren", ask=lambda q: "acmebakery")
    assert not load_account(bare, "ig-main").live_ok()
    monkeypatch.setattr(humanpresence, "is_human_terminal", lambda: True)
    with pytest.raises(SystemExit, match="does not match"):
        attest.account_live_command(bare, "ig-main", on=True, by="maren", ask=lambda q: "wrong")
    assert not load_account(bare, "ig-main").live_ok()
    out = attest.account_live_command(bare, "ig-main", on=True, by="maren", ask=lambda q: "@acmebakery")
    assert out["live"] and load_account(bare, "ig-main").live_ok()
    assert attest.account_live_command(bare, "ig-main", on=False, by="maren")["live"] is False and not load_account(bare, "ig-main").live_ok()
    monkeypatch.setattr(humanpresence, "is_human_terminal", lambda: False)
    with pytest.raises(SystemExit, match="human act"):
        attest.account_live_command(bare, "ig-main", on=False, by="maren")


def test_pause_blocks_a_live_account(bare):
    save_account(bare, cfg())
    attest.write_live(bare.accounts_dir, "ig-main", "maren")
    a = load_account(bare, "ig-main")
    assert not a.paused()
    bare.pause_path.write_text("")
    assert a.paused()
    from ave.platforms.instagram import InstagramAdapter
    assert any("paused" in p for p in InstagramAdapter(a).ready_problems())


# ------------------------------------------------------------------------------------------------ registry and the edits to existing files

def test_e3_get_publisher_falls_through_to_the_studio_registry():
    for name in ("bluesky", "threads", "linkedin", "pinterest", "manual"):
        assert name in ALL_PLATFORMS
        p = get_publisher(name)
        assert p.name == name and not p.account.live_ok()
    assert get_publisher("instagram").network == "instagram", "names the repo already served keep their adapter"
    assert get_publisher("youtube").name == "youtube" and get_publisher("facebook").name == "facebook"
    with pytest.raises(ValueError, match="unknown platform"):
        get_publisher("myspace")
    assert set(registry.ADAPTERS) >= {"bluesky", "threads", "linkedin", "pinterest", "manual", "x", "tiktok", "instagram", "facebook"}


def test_a_queue_item_for_a_studio_platform_preflights_without_account_bound_adapters(bare, tmp_path):
    from studio_helpers import make_png
    png = make_png(bare.work / "a.jpg", size=(1000, 1000))
    item = pq.create(bare, files=[png], media_type="image", platforms=["bluesky"], title="t", caption="hello", alt_texts=["a grey square"], ai_disclosure=False, visibility="public")
    assert item["status"] == "awaiting_approval", item["preflight_problems"]
    rep = pq.run(bare, item["id"], send=False)
    assert rep["blocked"] and "no approval recorded" in " ".join(rep["blocked"])


@pytest.mark.parametrize("visibility", [None, "private", "unlisted"])
def test_bluesky_refuses_nonpublic_automatic_queue_items(bare, visibility):
    from studio_helpers import make_png
    png = make_png(bare.work / "private.jpg", size=(1000,1000))
    fields = {} if visibility is None else {"visibility":visibility}
    item = pq.create(bare, files=[png], media_type="image", platforms=["bluesky"], title="t", caption="hello", alt_texts=["a grey square"], ai_disclosure=False, **fields)
    assert item["visibility"] == (visibility or "private")
    assert item["status"] == "draft" and "public creation only" in " ".join(item["preflight_problems"])


def test_e4_secret_ns_namespaces_the_existing_adapters_and_defaults_are_unchanged(monkeypatch):
    from ave.publish.adapters.facebook import FacebookPublisher
    from ave.publish.adapters.youtube import YouTubePublisher
    monkeypatch.setenv("AVE_FACEBOOK_PAGE_TOKEN", "legacy-token-value-1")
    monkeypatch.setenv("AVE_FACEBOOK_PAGE_ID", "legacy-page-1")
    assert FacebookPublisher().ready_problems() == []
    ns = FacebookPublisher(secret_ns="fb-page")
    assert len(ns.ready_problems()) == 2, "a namespaced adapter does not see the un-suffixed secret"
    monkeypatch.setenv("AVE_FACEBOOK_PAGE_TOKEN_FB_PAGE", "ns-token-value-12")
    monkeypatch.setenv("AVE_FACEBOOK_PAGE_ID_FB_PAGE", "ns-page-1")
    assert ns.ready_problems() == [] and ns._token() == "ns-token-value-12"
    monkeypatch.setenv("AVE_YOUTUBE_CREDENTIALS", json.dumps({"client_id": "a", "client_secret": "b", "refresh_token": "c"}))
    assert YouTubePublisher().ready_problems() == [] and YouTubePublisher(secret_ns="yt-main").ready_problems()
    monkeypatch.setenv("AVE_YOUTUBE_CREDENTIALS_YT_MAIN", json.dumps({"client_id": "a", "client_secret": "b", "refresh_token": "c"}))
    assert YouTubePublisher(secret_ns="yt-main").ready_problems() == []


def test_aggregator_and_youtube_accounts_are_gated_by_the_live_switch(bare):
    save_account(bare, cfg(mode="aggregator"))
    save_account(bare, {"id": "yt-main", "platform": "youtube", "mode": "native", "credentials": {"ref": "yt-main"}})
    for aid in ("ig-main", "yt-main"):
        a = load_account(bare, aid)
        pub = registry.adapter_for(a)
        assert isinstance(pub, registry.Gated)
        assert any("not live" in p for p in pub.ready_problems()) and pub.status()["live"] is False
        assert any("bound to" in p for p in pub.preflight({"account": "someone-else"}, None)) if aid == "yt-main" else True
        attest.write_live(bare.accounts_dir, aid, "maren")
        assert not any("not live" in p for p in pub.ready_problems())


def test_the_studio_facade_gets_account_bound_publishers(built):
    save_account(built.sp, cfg(id="bsky", platform="bluesky", credentials={"ref": "bsky", "handle": "a.bsky.social"}, post_types=["feed"]))
    registry.register(built.studio)
    pubs = built.studio.platform_publishers({"id": "q1", "account": "bsky"})
    assert list(pubs) == ["bluesky"] and pubs["bluesky"].account.id == "bsky"
    report = built.studio.adapter_report()
    assert {r["account"] for r in report} >= {"bsky", "ig-main"} and all(r["verified"] is False for r in report)
    assert "problems" in json.dumps(report) and "EAA" not in json.dumps(report)
    with pytest.raises(AccountError):
        built.studio.platform_publishers({"id": "q2"})


# ------------------------------------------------------------------------------------------------ E6: config discipline

STUDIO_PLATFORMS = ("bluesky", "threads", "linkedin", "pinterest", "x", "tiktok", "instagram", "facebook", "manual")


def test_e6_every_studio_platform_is_unverified_and_every_rate_entry_cites_a_source():
    cfg_ = load_platforms()
    for plat in STUDIO_PLATFORMS:
        block = cfg_[plat]
        assert block.get("verified") is False and block.get("source"), plat
        for sub in ("rate", "image"):
            if sub in block:
                assert block[sub].get("verified") is False and block[sub].get("source"), f"{plat}.{sub} needs verified and source"
    assert cfg_["youtube"]["rate"]["verified"] is False


def test_e6_instagram_takes_jpeg_only_and_the_new_image_blocks_exist():
    cfg_ = load_platforms()
    assert cfg_["instagram"]["image"]["formats"] == ["jpeg"]
    for plat in ("threads", "bluesky", "linkedin", "pinterest", "tiktok", "manual"):
        assert cfg_[plat]["image"]["carousel"]["max_items"] >= 2, plat
    assert cfg_["bluesky"]["image"]["carousel"]["max_items"] == 4 and cfg_["bluesky"]["image"]["max_file_bytes"] < 1_000_000
    assert cfg_["threads"]["post"]["caption_max_chars"] == 500 and cfg_["bluesky"]["post"]["caption_max_chars"] == 300
    assert cfg_["x"]["rate"]["est_cost_usd_per_post"] is not None


def test_instagram_rejects_a_png_at_queue_time(bare):
    from studio_helpers import FakeHost
    from ave.platforms.instagram import InstagramAdapter
    save_account(bare, cfg())
    a = InstagramAdapter(load_account(bare, "ig-main"), host=FakeHost())
    from PIL import Image
    png = bare.work / "a.png"
    png.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1080, 1350), (9, 9, 9)).save(png, format="PNG")
    item = pq.create(bare, files=[png], media_type="image", platforms=["instagram"], title="t", caption="c", alt_texts=["x"], ai_disclosure=False,
                     publishers={"instagram": a}, account="ig-main", post_type="feed", meta_v=2)
    assert item["status"] == "draft" and any("png" in p and "jpeg" in p for p in item["preflight_problems"])


# ------------------------------------------------------------------------------------------------ the media host

def test_sigv4_presign_matches_the_worked_example_in_the_aws_docs():
    url = presign("GET", "examplebucket.s3.amazonaws.com", "/test.txt", access_key="AKIAIOSFODNN7EXAMPLE", secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                  region="us-east-1", expires=86400, now=datetime(2013, 5, 24, tzinfo=timezone.utc))
    assert url.endswith("X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404")
    assert "X-Amz-SignedHeaders=host" in url and "AKIAIOSFODNN7EXAMPLE%2F20130524%2Fus-east-1%2Fs3%2Faws4_request" in url


class S3Http(FakeHttp):
    def __init__(self, sha=None, status=200):
        super().__init__([])
        self.objects, self.sha, self.status = {}, sha, status

    def put(self, url, **kw):
        self.calls.append({"method": "PUT", "url": url, **kw})
        self.objects[url.split("?")[0]] = (kw["data"], kw["headers"])
        return FakeResp({}, self.status)

    def request(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        data, hdrs = self.objects[url.split("?")[0]]
        return type("R", (), {"status_code": 200, "headers": {"x-amz-meta-sha256": self.sha or hdrs["x-amz-meta-sha256"], "content-length": str(len(data))}})()

    def delete(self, url, **kw):
        self.calls.append({"method": "DELETE", "url": url})
        self.objects.pop(url.split("?")[0], None)
        return FakeResp({}, 204)


def host(http, clock=None):
    clock = clock or FakeClock()
    return S3Host(endpoint="https://acct.r2.cloudflarestorage.com", bucket="ave-media", access_key="AKIAHOSTKEY12345", secret_key="host-secret-key-value-9876",
                  http=http, now=clock.now, token=lambda: "unguessable")


def test_s3_host_stores_exact_bytes_under_an_unguessable_key_and_cleans_up(tmp_path):
    f = tmp_path / "pic.jpg"
    f.write_bytes(b"jpeg-bytes-123")
    http = S3Http()
    h = host(http)
    got = h.put(f, ttl_s=900)
    sha = safety.sha256_file(f)
    assert got["sha256"] == sha and got["key"] == f"unguessable/{sha}.jpg" and "X-Amz-Expires=900" in got["url"] and "X-Amz-Signature=" in got["url"]
    assert got["url"].startswith("https://acct.r2.cloudflarestorage.com/ave-media/unguessable/")
    (data, hdrs), = http.objects.values()
    assert data == b"jpeg-bytes-123" and hdrs["x-amz-meta-sha256"] == sha and hdrs["Content-Type"] == "image/jpeg"
    h.verify(got)
    h.delete(got["key"])
    assert http.objects == {}
    assert "host-secret-key-value-9876" not in json.dumps(http.calls, default=str), "the secret key is never sent, only a signature"
    assert "host-secret-key-value-9876" not in safety.redact("x host-secret-key-value-9876 y")


def test_s3_host_refuses_when_the_stored_object_is_not_the_approved_file(tmp_path):
    from ave.platforms.base import HostError
    f = tmp_path / "pic.jpg"
    f.write_bytes(b"abc")
    h = host(S3Http(sha="f" * 64))
    got = h.put(f)
    with pytest.raises(HostError, match="differs"):
        h.verify(got)
    with pytest.raises(HostError, match="refused"):
        host(S3Http(status=403)).put(f)


def test_load_host_needs_a_config_file_and_both_keys(tmp_path, monkeypatch):
    p = tmp_path / "host.yaml"
    assert load_host(p) is None and host_status(p)["configured"] is False
    p.write_text(yaml.safe_dump({"endpoint": "https://x.r2.cloudflarestorage.com", "bucket": "b", "region": "auto"}))
    assert load_host(p) is None and any("host_access_key" in m for m in host_status(p)["missing"])
    monkeypatch.setenv("AVE_HOST_ACCESS_KEY", "AKIAHOSTKEY12345")
    monkeypatch.setenv("AVE_HOST_SECRET_KEY", "host-secret-key-value-9876")
    h = load_host(p)
    assert isinstance(h, S3Host) and host_status(p) == {"configured": True, "missing": []}
    assert "host-secret-key-value-9876" not in json.dumps(host_status(p))


# ------------------------------------------------------------------------------------------------ text metrics

def test_graphemes_count_what_a_person_sees():
    assert graphemes("hello") == 5
    assert graphemes("é") == 1                                  # combining accent
    assert graphemes("👨‍👩‍👧") == 1                         # ZWJ family
    assert graphemes("👍🏽") == 1                                      # skin tone
    assert graphemes("🇺🇸🇫🇷") == 2                                    # two flags
    assert graphemes("❤️") == 1
    assert graphemes("日本語") == 3 and graphemes("") == 0


def test_x_weight_counts_urls_as_23_and_wide_characters_as_2():
    assert x_weighted_len("hello") == 5
    assert x_weighted_len("see https://example.com/a/very/long/path?with=query now") == 4 + 23 + 4
    assert x_weighted_len("日本") == 4 and x_weighted_len("😀") == 2
    assert with_margin(300) == 294 and with_margin(280) == 274


def test_url_spans_are_utf8_byte_offsets_for_bluesky_facets():
    text = "café https://a.example/x."
    (a, b, url), = url_spans(text)
    assert url == "https://a.example/x" and text.encode()[a:b].decode() == url and a == len("café ".encode())
