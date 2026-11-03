"""Per-adapter behaviour beyond the shared contract: request shapes, platform rules, recovery verdicts. Same scripted-HTTP rules (no network)."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess

import pytest
from PIL import Image
from studio_helpers import DATA, FakeHost, make_png

from ave.platforms import attest, registry
from ave.platforms.accounts import load_account
from ave.platforms.base import AuthError, HostError, QuotaError
from ave.platforms.linkedin import escape_commentary
from ave.platforms.tiktok import MB, TikTokAdapter
from ave.publish import queue as pq, safety
from ave.publish.adapters.base import Absent, PublishError, Uncertain

from .test_adapter_contract import CASES, Case, Http, S, break_route, build, direct_publish, mp4, send  # noqa: F401

BY_ID = {c.id: c for c in CASES}
IG = BY_ID["instagram"]


def calls(rig, method=None, suffix=None):
    return [c for c in rig.http.calls if (method is None or c["method"] == method) and (suffix is None or c["url"].endswith(suffix))]


def progress_log(rig):
    seen = []
    item = {**rig.item, "_files": [str(f) for f in rig.files]}
    out = rig.adapter.publish(item, rig.files[0], seen.append)
    return out, seen


def routes_of(case, name=None):
    return json.loads((DATA / case.platform / (name or case.fixture)).read_text())


# ------------------------------------------------------------------------------------------------ Bluesky

def test_bluesky_record_has_facets_images_alt_text_and_aspect_ratio(tmp_path, mp4):
    rig = build(tmp_path, BY_ID["bluesky-carousel"], mp4)
    rig.item["caption"] = "Café day 3 #sourdough https://acme.example/starter."
    out, seen = progress_log(rig)
    rec = calls(rig, "POST", "createRecord")[0]["json"]["record"]
    assert rec["$type"] == "app.bsky.feed.post" and rec["text"].startswith("Café")
    kinds = {f["features"][0]["$type"] for f in rec["facets"]}
    assert kinds == {"app.bsky.richtext.facet#link", "app.bsky.richtext.facet#tag"}
    link = next(f for f in rec["facets"] if f["features"][0]["$type"].endswith("link"))
    assert rec["text"].encode()[link["index"]["byteStart"]:link["index"]["byteEnd"]].decode() == "https://acme.example/starter"
    imgs = rec["embed"]["images"]
    assert len(imgs) == 2 and imgs[0]["alt"].startswith("Slide 1") and imgs[0]["aspectRatio"] == {"width": 1080, "height": 1350}
    blobs = calls(rig, "POST", "uploadBlob")
    assert len(blobs) == 2 and blobs[0]["headers"]["Content-Type"] == "image/jpeg" and isinstance(blobs[0]["data"], bytes)
    assert seen[0]["created_at"] == rec["createdAt"] and seen[0]["phase"] == "creating", "createdAt is saved BEFORE createRecord"
    assert out["url"] == "https://bsky.app/profile/acme.bsky.social/post/3kkz2ofqbl22q"


def test_bluesky_text_only_post_and_the_grapheme_limit(tmp_path, mp4):
    rig = build(tmp_path, BY_ID["bluesky"], mp4)
    item = {"id": "q", "visibility":"public", "caption": "Just words.", "account": rig.acct.id}
    out = rig.adapter.publish(item, None, lambda p: None)
    assert "embed" not in calls(rig, "POST", "createRecord")[0]["json"]["record"] and out["id"].startswith("at://")
    long = {**rig.item, "caption": "👨‍👩‍👧" * 290}
    assert rig.adapter.preflight(long, rig.files[0]) == [], "ZWJ families are one grapheme each"
    assert any("graphemes" in p for p in rig.adapter.preflight({**rig.item, "caption": "x" * 295}, rig.files[0]))
    assert rig.adapter.preflight({**rig.item, "caption": "x" * 294}, rig.files[0]) == []


def test_bluesky_images_over_the_blob_cap_are_blocked_at_queue_time(tmp_path, mp4):
    rig = build(tmp_path, BY_ID["bluesky"], mp4)
    from PIL import Image
    import random
    big = rig.sp.work / "big.jpg"
    rnd = random.Random(1)
    Image.frombytes("RGB", (1500, 1500), bytes(rnd.getrandbits(8) for _ in range(1500 * 1500 * 3))).save(big, quality=95)
    assert big.stat().st_size > 976560
    it = pq.create(rig.sp, files=[big], media_type="image", platforms=["bluesky"], title="t", caption="c", alt_texts=["noise"], ai_disclosure=False,
                   publishers=rig.pubs, account=rig.acct.id, post_type="feed", meta_v=2)
    assert it["status"] == "draft" and any("max 976560" in p for p in it["preflight_problems"])


def test_bluesky_recover_requires_exact_record_identity(tmp_path,mp4):
    rig=build(tmp_path,BY_ID['bluesky'],mp4,routes=routes_of(BY_ID['bluesky'],'recover.json'))
    for record in ({'state':'sending','created_at':'2026-10-19T16:00:00.000Z'}, {'state':'sending'}):
        assert rig.adapter.recover(rig.item,record) is None
    assert rig.http.calls==[], 'same-second or latest-page text is not operation identity'
    exact={'$type':'app.bsky.feed.post','text':'Exact record','createdAt':'2026-10-19T16:00:00.000Z'}
    import hashlib
    did='did:plc:abc';rkey='fixedrecord';uri=f'at://{did}/app.bsky.feed.post/{rkey}'
    rig.adapter._session=lambda:{'did':did,'handle':'acme.bsky.social','accessJwt':'fixture'}
    rig.adapter._http=Http([{'method':'GET','url':'getRecord','response':{'uri':uri,'value':exact,'cid':'cid'}}])
    record={'did':did,'rkey':rkey,'record_digest':hashlib.sha256(safety.canonical(exact).encode()).hexdigest()}
    assert rig.adapter.recover(rig.item,record)['id']==uri
    assert rig.adapter.recover(rig.item,{**record,'record_digest':'0'*64}) is None
    assert rig.http.calls==[]



def test_quota_and_auth_refusals_are_classified(tmp_path, mp4):
    case = BY_ID["bluesky"]
    r = build(tmp_path, case, mp4, routes=break_route(routes_of(case), lambda x: x.get("early"), status=429, headers={"retry-after": "120"},
                                                      response={"error": "RateLimitExceeded", "message": "slow down"}))
    with pytest.raises(QuotaError) as q:
        direct_publish(r)
    assert q.value.retry_after == 120.0 and isinstance(q.value, PublishError)
    r = build(tmp_path / "b", case, mp4, routes=break_route(routes_of(case), lambda x: x.get("early"), status=400,
                                                            response={"error": "ExpiredToken", "message": "Token has expired"}))
    with pytest.raises(AuthError):
        direct_publish(r)


# ------------------------------------------------------------------------------------------------ Instagram

def test_instagram_image_flow_order_and_request_shapes(tmp_path, mp4):
    rig = build(tmp_path, IG, mp4)
    out, seen = progress_log(rig)
    seq = [(c["method"], c["url"].rsplit("/", 1)[-1]) for c in rig.http.calls]
    assert seq == [("GET", "content_publishing_limit"), ("POST", "media"), ("GET", "17890000000000001"), ("POST", "media_publish"), ("GET", "17900000000000001")]
    body = calls(rig, "POST", "/media")[0]["json"]
    assert body["image_url"].startswith("https://media.example.invalid/") and body["alt_text"].startswith("Slide 1") and "pears" in body["caption"]
    assert body["image_url"].endswith(".jpg") and "media_type" not in body
    assert {p.get("container") for p in seen if p.get("container")} == {"17890000000000001"}, "container id is saved for recover"
    assert seen[-1]["receipt"]["phase"] == "published" and out["url"] == "https://www.instagram.com/p/Cabc123/"
    assert not rig.host.deleted and all(h["expires"]==86400 for h in rig.host.puts)


def test_instagram_carousel_and_story_and_reel_bodies(tmp_path, mp4):
    rig = build(tmp_path, BY_ID["instagram-carousel"], mp4)
    direct_publish(rig)
    bodies = [c["json"] for c in calls(rig, "POST", "/media")]
    assert [b.get("is_carousel_item") for b in bodies] == [True, True, None]
    assert bodies[2]["media_type"] == "CAROUSEL" and bodies[2]["children"] == "17890000000000001,17890000000000001"
    story = build(tmp_path / "s", IG, mp4)
    story.item["post_type"] = "story"
    direct_publish(story)
    sb = calls(story, "POST", "/media")[0]["json"]
    assert sb["media_type"] == "STORIES" and "caption" not in sb
    reel = build(tmp_path / "r", Case("ig-reel", "instagram", media="video", secrets=S, public=IG.public, needs_host=True), mp4)
    direct_publish(reel)
    rb = calls(reel, "POST", "/media")[0]["json"]
    assert rb["media_type"] == "REELS" and rb["video_url"].endswith(".mp4") and rb["share_to_feed"] is True


def test_instagram_refuses_bytes_the_host_did_not_store_and_cleans_up(tmp_path, mp4):
    rig = build(tmp_path, IG, mp4)
    rig.adapter.host = FakeHost(corrupt=True)
    with pytest.raises(HostError, match="different bytes"):
        direct_publish(rig)
    assert rig.http.calls == [] and rig.adapter.host.deleted == [h["key"] for h in rig.adapter.host.puts]
    rig.adapter.host = None
    with pytest.raises(PublishError, match="needs_public_url"):
        direct_publish(rig)


def test_instagram_quota_and_processing_failures_stop_before_publishing(tmp_path, mp4):
    rig = build(tmp_path, IG, mp4, routes=routes_of(IG, "quota_full.json"))
    with pytest.raises(QuotaError, match="100 of 100"):
        direct_publish(rig)
    assert rig.host.puts and not rig.host.deleted, "bounded leases survive provider pulls"
    for status in ("ERROR", "EXPIRED"):
        routes = break_route(routes_of(IG), lambda r: r["url"] == "/17890000000000001", response={"status_code": status})
        rig = build(tmp_path / status, IG, mp4, routes=routes)
        with pytest.raises(PublishError, match=status):
            direct_publish(rig)
        assert all(r["_used"] == 0 for r in rig.http.routes if r.get("final"))


def test_instagram_container_that_never_finishes_is_not_published(tmp_path, mp4):
    routes = break_route(routes_of(IG), lambda r: r["url"] == "/17890000000000001", response={"status_code": "IN_PROGRESS"})
    rig = build(tmp_path, IG, mp4, routes=routes)
    slept = []
    rig.adapter.sleep = slept.append
    with pytest.raises(PublishError, match="still 'IN_PROGRESS'"):
        direct_publish(rig)
    assert slept and all(r["_used"] == 0 for r in rig.http.routes if r.get("final"))


def test_instagram_recover_verdicts(tmp_path, mp4):
    rec = {"state": "sending", "container": "17890000000000001"}
    pub = build(tmp_path / "p", IG, mp4, routes=routes_of(IG, "recover_published.json"))
    assert pub.adapter.recover(pub.item, rec)["recovered"] is True
    exp = build(tmp_path / "e", IG, mp4, routes=routes_of(IG, "recover_expired.json"))
    assert isinstance(exp.adapter.recover(exp.item, rec), Absent)
    fin = build(tmp_path / "f", IG, mp4, routes=break_route(routes_of(IG, "recover_published.json"), lambda r: True, response={"status_code": "FINISHED"}))
    assert fin.adapter.recover(fin.item, rec) is None, "FINISHED may still be publishing: a human decides"
    none = build(tmp_path / "n", IG, mp4, routes=[])
    assert none.adapter.recover(none.item, {"state": "sending"}) is None and none.http.calls == []


def test_instagram_first_comment_failure_never_fails_a_live_post(tmp_path, mp4):
    routes = routes_of(IG) + [{"method": "POST", "url": "/17900000000000001/comments", "status": 400, "response": {"error": {"message": "nope", "code": 100}}}]
    rig = build(tmp_path, IG, mp4, routes=routes)
    rig.item["first_comment"] = "Recipe in the link."
    out = direct_publish(rig)
    assert out["id"] == "17900000000000001" and out["receipt"]["adjuncts"]["first_comment"]["state"]=="failed"


def test_instagram_refresh_stores_the_new_token_in_the_keychain_only(tmp_path, mp4):
    rig = build(tmp_path, IG, mp4, routes=[{"method": "GET", "url": "refresh_access_token", "response": {"access_token": "IGQ-new-long-lived-token-value", "expires_in": 5184000}}])
    with pytest.raises(PublishError,match='Instagram Login'):rig.adapter.refresh()
    from dataclasses import replace
    rig.adapter.account=replace(rig.acct,connection={'product':'instagram_login'})
    assert rig.adapter.refresh() == {"refreshed": True, "expires_in": 5184000}
    assert rig.acct.secret("token") == "IGQ-new-long-lived-token-value"
    assert "IGQ-new-long-lived-token-value" not in "".join(p.read_text() for p in rig.sp.root.rglob("*.yaml"))


# ------------------------------------------------------------------------------------------------ Threads

TH = BY_ID["threads"]


def test_threads_text_only_needs_no_host_and_enforces_500_chars(tmp_path, mp4):
    rig = build(tmp_path, TH, mp4)
    rig.adapter.host = None
    assert rig.adapter.ready_problems() == [], "a host is only needed for media"
    assert rig.adapter.preflight({"id": "q", "visibility":"public", "caption": "x" * 501}, None) and not rig.adapter.preflight({"id": "q", "visibility":"public", "caption": "x" * 500}, None)
    out = rig.adapter.publish({"id": "q", "visibility":"public", "caption": "Hello Threads", "account": rig.acct.id}, None, lambda p: None)
    body = calls(rig, "POST", f"/{TH.public['threads_user_id']}/threads")[0]["json"]
    assert body["media_type"] == "TEXT" and body["text"] == "Hello Threads" and out["url"].startswith("https://www.threads.net/")
    assert rig.host.puts == []
    # an image item without a host is a preflight problem, not a surprise at send time
    imgrig = build(tmp_path / "b", TH, mp4)
    imgrig.adapter.host = None
    assert any("needs_public_url" in p for p in imgrig.adapter.preflight(imgrig.item, imgrig.files[0]))


def test_threads_image_and_carousel_bodies_and_the_publishing_limit(tmp_path, mp4):
    rig = build(tmp_path, BY_ID["threads-carousel"], mp4)
    direct_publish(rig)
    bodies = [c["json"] for c in calls(rig, "POST", f"/{TH.public['threads_user_id']}/threads")]
    assert [b["media_type"] for b in bodies] == ["IMAGE", "IMAGE", "CAROUSEL"] and bodies[2]["children"].count(",") == 1
    full = routes_of(TH)
    full[0] = {**full[0], "response": {"data": [{"quota_usage": 250, "config": {"quota_total": 250}}]}}
    rig = build(tmp_path / "q", TH, mp4, routes=full)
    with pytest.raises(QuotaError):
        direct_publish(rig)


def test_threads_first_comment_is_a_reply_post(tmp_path, mp4):
    rig = build(tmp_path, TH, mp4)
    rig.item["first_comment"] = "More below."
    out = direct_publish(rig)
    replies = [c["json"] for c in calls(rig, "POST", f"/{TH.public['threads_user_id']}/threads") if c["json"].get("reply_to_id")]
    assert replies and replies[0]["reply_to_id"] == "18000000000000001" and out["receipt"]["adjuncts"]["first_comment"]["state"] == "published"


# ------------------------------------------------------------------------------------------------ Facebook Page

FB = BY_ID["facebook"]


def test_facebook_photo_goes_live_only_when_public_or_scheduled(tmp_path, mp4):
    forms = {}
    for vis in ("private", "public"):
        rig = build(tmp_path / vis, FB, mp4)
        rig.item["visibility"] = vis
        direct_publish(rig)
        forms[vis] = calls(rig, "POST", "/photos")[0]["data"]
    assert forms["private"]["published"] == "false" and forms["public"]["published"] == "true"
    assert forms["public"]["caption"].startswith("Day 3") and forms["public"]["alt_text_custom"].startswith("Slide 1")
    rig = build(tmp_path / "sched", FB, mp4)
    rig.item.pop("meta_v", None)  # this branch explicitly tests legacy native scheduling
    rig.item["schedule"] = "2026-10-21T16:00:00+00:00"
    rig.adapter.now = lambda: __import__("datetime").datetime(2026, 10, 19, 16, tzinfo=__import__("datetime").timezone.utc)
    assert rig.adapter.preflight(rig.item, rig.files[0]) == []
    direct_publish(rig)
    f = calls(rig, "POST", "/photos")[0]["data"]
    assert f["published"] == "false" and f["scheduled_publish_time"] == 1792598400
    rig.item["schedule"] = "2026-10-19T16:05:00+00:00"
    assert any("at least 10 minutes" in p for p in rig.adapter.preflight(rig.item, rig.files[0]))


def test_facebook_carousel_uploads_unpublished_photos_then_attaches_them(tmp_path, mp4):
    rig = build(tmp_path, BY_ID["facebook-carousel"], mp4)
    seen = []
    rig.adapter.publish({**rig.item, "_files": [str(f) for f in rig.files]}, rig.files[0], seen.append)
    photos = calls(rig, "POST", "/photos")
    assert len(photos) == 2 and all(c["data"]["published"] == "false" for c in photos)
    feed = calls(rig, "POST", "/feed")[0]["data"]
    assert json.loads(feed["attached_media[0]"]) == {"media_fbid": "1001"} and "attached_media[1]" in feed and feed["message"].startswith("Day 3")
    assert any(p.get("photo_ids") for p in seen)


def test_facebook_video_is_delegated_to_the_existing_adapter_with_account_secrets(tmp_path, mp4):
    rig = build(tmp_path, FB, mp4)
    assert rig.adapter._token() == FB.secrets["page_token"] and rig.adapter.secret_ns == rig.acct.cred_ref
    vid = {"id": "q", "media_type": "video", "title": "t", "account": rig.acct.id, "format": "auto"}
    p = rig.adapter.preflight(vid, mp4)                    # the legacy adapter's rules, not the photo rules
    assert any("facebook reel: width 360 under min 540" in x for x in p)
    assert any("too long" in x for x in rig.adapter.preflight({**vid, "description": "d" * 6000}, mp4))


# ------------------------------------------------------------------------------------------------ LinkedIn

LI = BY_ID["linkedin"]


def test_linkedin_commentary_is_escaped_and_headers_are_pinned():
    assert escape_commentary("a_b *c* (d) [e] {f} <g> ~h| @i \\ #tag # x") == "a\\_b \\*c\\* \\(d\\) \\[e\\] \\{f\\} \\<g\\> \\~h\\| \\@i \\\\ #tag \\# x"


def test_linkedin_visibility_maps_to_lifecycle_and_carousel_uses_multi_image(tmp_path, mp4):
    out = {}
    for vis in ("public", "unlisted"):
        rig = build(tmp_path / vis, LI, mp4)
        rig.item["visibility"] = vis
        direct_publish(rig)
        out[vis] = calls(rig, "POST", "/rest/posts")[0]["json"]
    assert (out["public"]["lifecycleState"], out["public"]["visibility"]) == ("PUBLISHED", "PUBLIC")
    assert (out["unlisted"]["lifecycleState"], out["unlisted"]["visibility"]) == ("PUBLISHED", "CONNECTIONS")
    private=build(tmp_path/'private',LI,mp4);private.item['visibility']='private'
    with pytest.raises(PublishError,match='private creation'): direct_publish(private)
    assert private.http.calls==[], 'private refusal happens before upload'
    assert out["public"]["author"] == "urn:li:person:abc123" and out["public"]["content"]["media"]["altText"].startswith("Slide 1")
    car = build(tmp_path / "car", BY_ID["linkedin-carousel"], mp4)
    direct_publish(car)
    assert len(calls(car, "POST", "/rest/posts")[0]["json"]["content"]["multiImage"]["images"]) == 2
    hdr = calls(car, "POST", "/rest/posts")[0]["headers"]
    assert hdr["Linkedin-Version"] == "202610" and hdr["X-Restli-Protocol-Version"] == "2.0.0"


def test_linkedin_pdf_carousel_goes_through_the_documents_api(tmp_path, mp4):
    rig = build(tmp_path, LI, mp4, routes=[
        {"method": "POST", "url": "/rest/documents", "response": {"value": {"uploadUrl": "https://www.linkedin.com/dms-uploads/doc", "document": "urn:li:document:D1"}}},
        {"method": "PUT", "url": "dms-uploads", "status": 201, "response": {}},
        {"method":"GET","url":"/rest/documents/","response":{"status":"AVAILABLE"}},
        {"method": "POST", "url": "/rest/posts", "status": 201, "response": {}, "headers": {"x-restli-id": "urn:li:share:9"}}])
    pdf = rig.sp.work / "deck.pdf"
    Image.new('RGB',(612,792),'#f2deaa').save(pdf,'PDF')
    out = rig.adapter.publish({**rig.item, "_files": [str(pdf)]}, pdf, lambda p: None)
    assert calls(rig, "POST", "/rest/posts")[0]["json"]["content"]["media"]["id"] == "urn:li:document:D1" and out["id"] == "urn:li:share:9"


# ------------------------------------------------------------------------------------------------ X

XC = BY_ID["x"]


def test_x_weighted_length_and_cost_logging(tmp_path, mp4):
    rig = build(tmp_path, XC, mp4)
    assert rig.adapter.preflight({**rig.item, "caption": "日" * 135}, rig.files[0]) == []
    assert any("weighs" in p for p in rig.adapter.preflight({**rig.item, "caption": "日" * 140}, rig.files[0]))
    assert rig.adapter.preflight({**rig.item, "caption": "go https://example.com/" + "p" * 300}, rig.files[0]) == [], "a URL counts 23 whatever its length"
    out = direct_publish(rig)
    assert out["est_cost_usd"] == 0.01 and out["url"] == "https://x.com/i/status/1850000000000000000"
    body = calls(rig, "POST", "/2/tweets")[0]["json"]
    assert body["media"] == {"media_ids": ["1880000000000000001"]}
    meta = calls(rig, "POST", "/2/media/metadata")[0]["json"]
    assert meta["metadata"]["alt_text"]["text"].startswith("Slide 1")
    init = calls(rig, "POST", "/initialize")[0]["json"]
    assert init["media_type"] == "image/jpeg" and init["media_category"] == "tweet_image" and init["total_bytes"] == rig.files[0].stat().st_size


def test_x_recovery_never_claims_matching_caption_identity(tmp_path,mp4):
    rig=build(tmp_path,XC,mp4,routes=[])
    rig.item['caption']='same text'
    assert rig.adapter.recover(rig.item,{'state':'sending','started':'2026-10-19T09:59:00-0600','media_ids':['different-media']}) is None
    assert rig.http.calls==[]



# ------------------------------------------------------------------------------------------------ Pinterest

PI = BY_ID["pinterest"]


def test_pinterest_bodies_and_sandbox_switch(tmp_path, mp4):
    rig = build(tmp_path, PI, mp4)
    rig.item["link_url"] = "https://acme.example/starter"
    direct_publish(rig)
    c = calls(rig, "POST", "/v5/pins")[0]
    assert c["url"].startswith("https://api.pinterest.com/v5/")
    ms = c["json"]["media_source"]
    assert ms["source_type"] == "image_base64" and ms["content_type"] == "image/jpeg" and len(ms["data"]) > 100
    assert c["json"]["board_id"] == "549755885175" and c["json"]["link"] == "https://acme.example/starter" and c["json"]["alt_text"].startswith("Slide 1")
    car = build(tmp_path / "car", BY_ID["pinterest-carousel"], mp4)
    direct_publish(car)
    cm = calls(car, "POST", "/v5/pins")[0]["json"]["media_source"]
    assert cm["source_type"] == "multiple_image_base64" and len(cm["items"]) == 2 and cm["index"] == 0
    sandbox = build(tmp_path / "sb", Case("pin-sb", "pinterest", secrets=S, public={"board_id": "1", "sandbox": "true"}), mp4)
    sandbox.http.routes = [dict(r, _used=0) for r in routes_of(PI)]
    direct_publish(sandbox)
    assert calls(sandbox, "POST", "/v5/pins")[0]["url"].startswith("https://api-sandbox.pinterest.com/")
    assert any("title" in p for p in rig.adapter.preflight({**rig.item, "title": "t" * 101}, rig.files[0]))


# ------------------------------------------------------------------------------------------------ TikTok

TT = BY_ID["tiktok"]


def test_tiktok_chunk_math_follows_the_documented_limits():
    assert TikTokAdapter._chunks(3 * MB) == (3 * MB, 1)
    assert TikTokAdapter._chunks(64 * MB) == (64 * MB, 1)
    size, (chunk, n) = 100 * MB, TikTokAdapter._chunks(100 * MB)
    assert chunk == 32 * MB and n == 3 and size - (n - 1) * chunk <= 128 * MB, "the last chunk takes the remainder"


def test_tiktok_inbox_upload_shape_and_result(tmp_path, mp4):
    rig = build(tmp_path, TT, mp4)
    out, seen = progress_log(rig)
    init = calls(rig, "POST", "/post/publish/inbox/video/init/")[0]["json"]["source_info"]
    size = rig.files[0].stat().st_size
    assert init == {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": size, "total_chunk_count": 1}
    put = calls(rig, "PUT")[0]
    assert put["headers"]["Content-Range"] == f"bytes 0-{size - 1}/{size}" and len(put["data"]) == size
    assert out["inbox"] is True and out["url"] is None and "tap publish" in out["note"]
    assert seen[0]["publish_id"] == "v_inbox_file~v2.123456789"
    assert not calls(rig, suffix="/post/publish/video/init/"), "inbox mode never uses the direct endpoint"


def test_tiktok_local_audited_flag_never_unlocks_direct_post(tmp_path,mp4):
    from ave.platforms.accounts import save_account
    locked=build(tmp_path,TT,mp4)
    for audited in (False,True):
        aid='tt-direct-'+str(audited).lower()
        save_account(locked.sp,{'id':aid,'platform':'tiktok','mode':'direct','audited':audited,'post_types':['feed'],'credentials':{'ref':aid}})
        acct=load_account(locked.sp,aid);safety.set_secret(acct.secret_name('token'),S['token']);attest.write_live(locked.sp.accounts_dir,aid,'fixture')
        adapter=registry.adapter_for(acct,http=Http([]))
        assert any('eligible' in p for p in adapter.ready_problems())
        with pytest.raises(PublishError,match='locked'):adapter.publish({**locked.item,'account':aid},locked.files[0],lambda p:None)
        assert adapter.http.calls==[]



def test_tiktok_error_envelope_and_failed_processing(tmp_path, mp4):
    routes = break_route(routes_of(TT), lambda r: r["url"].endswith("inbox/video/init/"), response={"data": {}, "error": {"code": "access_token_invalid", "message": "bad token", "log_id": "x"}})
    with pytest.raises(AuthError):
        direct_publish(build(tmp_path, TT, mp4, routes=routes))
    failed = break_route(routes_of(TT), lambda r: r["url"].endswith("status/fetch/"), response={"data": {"status": "FAILED", "fail_reason": "file_format_check_failed"}, "error": {"code": "ok"}})
    with pytest.raises(PublishError, match="file_format_check_failed"):
        direct_publish(build(tmp_path / "f", TT, mp4, routes=failed))
    slow = break_route(routes_of(TT), lambda r: r["url"].endswith("status/fetch/"), response={"data": {"status": "PROCESSING_UPLOAD"}, "error": {"code": "ok"}})
    result=direct_publish(build(tmp_path / "s", TT, mp4, routes=slow))
    assert result['receipt']['phase']=='processing' and result['id'] is None


def test_tiktok_photo_post_pulls_from_the_media_host_and_cleans_up(tmp_path, mp4):
    case = Case("tt-photo", "tiktok", "photo_ok.json", media="carousel", mode="inbox", secrets=S, needs_host=True)
    rig = build(tmp_path, case, mp4)
    assert rig.adapter.host_problems() == [] and rig.adapter.preflight(rig.item, rig.files[0]) == []
    out = direct_publish(rig)
    body = calls(rig, "POST", "/post/publish/content/init/")[0]["json"]
    assert body["media_type"] == "PHOTO" and body["post_mode"] == "MEDIA_UPLOAD" and len(body["source_info"]["photo_images"]) == 2
    assert body["source_info"]["source"] == "PULL_FROM_URL" and out["inbox"] is True
    assert not rig.host.deleted and rig.host.puts and all(h["expires"]==86400 for h in rig.host.puts)
    nohost = build(tmp_path / "n", case, mp4)
    nohost.adapter.host = None
    assert any("needs_public_url" in p for p in nohost.adapter.preflight(nohost.item, nohost.files[0]))


def test_tiktok_recover_verdicts(tmp_path, mp4):
    rec = {"state": "sending", "publish_id": "v_inbox_file~v2.1"}
    done = build(tmp_path / "d", TT, mp4, routes=routes_of(TT, "recover_done.json"))
    assert done.adapter.recover(done.item, rec)["recovered"] is True
    failed = build(tmp_path / "f", TT, mp4, routes=routes_of(TT, "recover_failed.json"))
    # Terminal failure is persisted evidence, not permission to recreate in recovery.
    verdict=failed.adapter.recover(failed.item, rec)
    assert verdict["receipt"]["phase"] == "failed"
    assert verdict["receipt"]["object"]["id"] == rec["publish_id"]
    nope = build(tmp_path / "n", TT, mp4, routes=[])
    assert isinstance(nope.adapter.recover(nope.item, {"state": "sending"}), Absent), "a video upload that never got a publish_id started nothing"
    ph = build(tmp_path / "p", Case("tt-photo", "tiktok", "photo_ok.json", media="image", secrets=S, needs_host=True), mp4, routes=[])
    assert ph.adapter.recover(ph.item, {"state": "sending"}) is None, "a photo init may already have started the pull"


# ------------------------------------------------------------------------------------------------ manual

def test_manual_folder_holds_the_exact_approved_files_and_an_alert(tmp_path, mp4):
    rig = build(tmp_path, BY_ID["manual"], mp4)
    rep = send(rig)
    folder = rig.sp.root / "exports" / "manual" / rig.acct.id / rig.item["id"]
    names = sorted(p.name for p in folder.iterdir())
    assert names[:3] == ["01-s0.jpg", "README.txt", "alt-texts.txt"] and "caption.txt" in names and "manifest.json" in names
    assert safety.sha256_file(folder / "01-s0.jpg") == rig.item["sha256"]
    man = json.loads((folder / "manifest.json").read_text())
    assert man["files"][0]["sha256"] == rig.item["sha256"] and man["account"] == rig.acct.id
    from ave.studio import ledger
    assert [a["kind"] for a in ledger.list_alerts(rig.sp)] == ["manual_post_due"]
    assert rep["platforms"]["manual"]["outcome"] == "uncertain" and "resolve" in rep["platforms"]["manual"]["error"]
    assert rig.http.calls == []


def test_studio_facebook_photo_due_slot_delivers_with_reviewed_visibility(tmp_path,mp4):
    rig=build(tmp_path/'due-photo',FB,mp4)
    rig.item['schedule']='2026-10-01T16:00:00+00:00'
    rig.item['visibility']='public'
    assert rig.item['meta_v']==2
    assert rig.adapter.preflight(rig.item,rig.files[0])==[]
    direct_publish(rig)
    form=calls(rig,'POST','/photos')[0]['data']
    assert form['published']=='true' and 'scheduled_publish_time' not in form
