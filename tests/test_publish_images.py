"""Publish queue: image and carousel items. Approval is bound to the hash of EVERY file; video items behave exactly as before."""

import hashlib
import json

import pytest
from PIL import Image

from ave.publish import queue, safety
from ave.publish.adapters import get_publisher
from ave.publish.adapters.aggregator import AggregatorPublisher
from ave.publish.adapters.base import image_dimension_problems, image_problems, load_platforms
from publish_helpers import (FakeResp, FakeSession, RecordingPublisher, approve_item, ep, isolated_secrets, queue_item,  # noqa: F401
                             video_pool, vertical, wide)

ALT = "Quote card: a goal is a wish and a system is a habit."


def png(ep, name, size=(1080, 1350), color=(27, 24, 20)):
    p = ep.root / "content" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(p)
    return p


def make(ep, files, media_type="image", platforms=("instagram",), alts=None, **kw):
    kw.setdefault("ai_disclosure", False)
    return queue.create(ep, files=files, media_type=media_type, alt_texts=alts or [ALT] * len(files), platforms=list(platforms),
                        title=kw.pop("title", "A goal is a wish"), publishers={p: RecordingPublisher(p) for p in platforms}, **kw)


def pubs(*names):
    return {n: RecordingPublisher(n) for n in names}


# ---- image items ---------------------------------------------------------------------------------

def test_image_item_is_hashed_like_its_file_and_awaits_approval(ep):
    f = png(ep, "a.png")
    it = make(ep, [f])
    assert it["media_type"] == "image" and it["files"] == ["content/a.png"] and it["alt_texts"] == [ALT]
    assert it["sha256"] == hashlib.sha256(f.read_bytes()).hexdigest()      # one file: `shasum -a 256` gives the same value
    assert it["status"] == "awaiting_approval" and it["preflight_problems"] == []


def test_approval_binds_the_image_bytes_and_changing_the_file_invalidates_it(ep):
    f = png(ep, "a.png")
    it = make(ep, [f])
    done = approve_item(ep, it["id"], pubs("instagram"))
    assert done["status"] == "approved" and queue.send_blockers(ep, done) == []
    Image.new("RGB", (1080, 1350), (28, 24, 20)).save(f)                     # one pixel value different
    blockers = queue.send_blockers(ep, queue.load(ep, it["id"]))
    assert any("media files changed since approval" in b for b in blockers)
    rep = queue.run(ep, it["id"], send=True, publishers=pubs("instagram"))
    assert not rep["ok"] and queue.load(ep, it["id"])["status"] == "awaiting_approval"


def test_wrong_confirmation_hash_is_refused_for_images(ep):
    it = make(ep, [png(ep, "a.png")])
    with pytest.raises(ValueError, match="confirmation hash"):
        queue.approve(ep, it["id"], "ben", "deadbeef0000", publishers=pubs("instagram"))


def test_editing_alt_text_after_approval_invalidates_it(ep):
    it = make(ep, [png(ep, "a.png")])
    approve_item(ep, it["id"], pubs("instagram"))
    item = queue.load(ep, it["id"])
    item["alt_texts"] = ["Something else entirely"]
    queue.save(ep, item)
    assert any("changed since approval" in b for b in queue.send_blockers(ep, queue.load(ep, it["id"])))


def test_default_is_a_dry_run_and_nothing_is_sent_without_yes(ep):
    it = make(ep, [png(ep, "a.png")])
    p = pubs("instagram")
    approve_item(ep, it["id"], p)
    rep = queue.run(ep, it["id"], send=False, publishers=p)
    assert rep["ok"] and rep["mode"] == "dry-run" and p["instagram"].publish_calls == 0
    assert queue.load(ep, it["id"])["status"] == "approved"
    rep = queue.run(ep, it["id"], send=True, publishers=p)
    assert rep["ok"] and p["instagram"].publish_calls == 1


# ---- carousel ------------------------------------------------------------------------------------

def _car(ep, n=3):
    return [png(ep, f"s{i}.png", color=(27 + i, 24, 20)) for i in range(1, n + 1)]


def test_carousel_hash_is_over_the_ordered_list_of_every_file(ep):
    fs = _car(ep)
    shas = [hashlib.sha256(f.read_bytes()).hexdigest() for f in fs]
    it = make(ep, fs, "carousel")
    want = hashlib.sha256(json.dumps(shas, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert it["sha256"] == want and it["file_sha256"] == shas
    assert it["status"] == "awaiting_approval"


@pytest.mark.parametrize("change", ["edit_middle", "swap_order", "drop_last"])
def test_any_change_to_any_slide_invalidates_the_carousel_approval(ep, change):
    fs = _car(ep)
    it = make(ep, fs, "carousel")
    approve_item(ep, it["id"], pubs("instagram"))
    assert queue.send_blockers(ep, queue.load(ep, it["id"])) == []
    item = queue.load(ep, it["id"])
    if change == "edit_middle":
        Image.new("RGB", (1080, 1350), (200, 0, 0)).save(fs[1])
    elif change == "swap_order":
        item["files"][0], item["files"][1] = item["files"][1], item["files"][0]
        item["alt_texts"][0], item["alt_texts"][1] = item["alt_texts"][1], item["alt_texts"][0]
    else:
        item["files"].pop()
        item["alt_texts"].pop()
    queue.save(ep, item)
    assert queue.send_blockers(ep, queue.load(ep, it["id"]))


# ---- preflight -----------------------------------------------------------------------------------

def test_alt_text_is_required_for_every_image(ep):
    f = png(ep, "a.png")
    it = queue.create(ep, files=[f], media_type="image", alt_texts=[""], platforms=["instagram"], title="t", ai_disclosure=False,
                      publishers=pubs("instagram"))
    assert it["status"] == "draft" and any("alt text is required" in p for p in it["preflight_problems"])


def test_image_limits_from_platforms_yaml_aspect_size_count_and_format(ep):
    cfg = load_platforms()
    assert image_dimension_problems(cfg["instagram"]["image"], 1080, 1350) == []
    assert any("wider than" in p for p in image_dimension_problems(cfg["instagram"]["image"], 3000, 500))
    assert any("under the minimum" in p for p in image_dimension_problems(cfg["instagram"]["image"], 200, 250))
    five = [png(ep, f"x{i}.png") for i in range(5)]
    item = {"media_type": "carousel", "alt_texts": [ALT] * 5, "caption": "hi"}
    assert any("at most 4" in p for p in image_problems(cfg, "x", item, five))
    mixed = [png(ep, "m1.png"), png(ep, "m2.png", size=(1080, 1080))]
    assert any("same size" in p for p in image_problems(cfg, "instagram", {"media_type": "carousel", "alt_texts": [ALT, ALT]}, mixed))
    one = {"media_type": "image", "alt_texts": [ALT]}
    assert any("exactly one image" in p for p in image_problems(cfg, "instagram", one, mixed))
    assert any("no image limits" in p for p in image_problems({}, "mastodon", one, mixed[:1]))
    gif = ep.root / "content" / "g.gif"
    Image.new("P", (1080, 1080)).save(gif)
    assert any("gif" in p for p in image_problems(cfg, "instagram", one, [gif]))


def test_youtube_and_facebook_adapters_stay_video_only_and_say_so(ep):
    f = png(ep, "t.png", size=(1280, 720))
    for name in ("youtube", "facebook"):
        problems = get_publisher(name).preflight({"media_type": "image", "title": "t"}, f)
        assert len(problems) == 1 and "video only" in problems[0]
        assert get_publisher(name).carries_images is False
    it = queue.create(ep, files=[f], media_type="image", alt_texts=[ALT], platforms=["youtube"], title="Thumb", ai_disclosure=False)
    assert it["status"] == "draft" and any("video only" in p for p in it["preflight_problems"])


def test_missing_image_file_is_reported(ep):
    f = png(ep, "a.png")
    it = make(ep, [f])
    f.unlink()
    assert any("image not found" in p for p in queue.preflight_item(ep, queue.load(ep, it["id"]), pubs("instagram")))
    assert any("image not found" in b for b in queue.send_blockers(ep, queue.load(ep, it["id"])))


# ---- aggregator carries images behind its template mechanism -------------------------------------

def test_aggregator_uploads_every_slide_and_fills_media_lists_in_the_post_template(ep, isolated_secrets):
    safety.set_secret("aggregator_api_key", "agg-key-123456")
    conf = {"base_url": "https://agg.example", "auth": {"header": "X-Key", "prefix": "Key "}, "networks": {"instagram": "ig"},
            "supports_visibility": False,
            "upload": {"path": "/media", "id_path": "data.id", "url_path": "data.url"},
            "post": {"path": "/post", "json": {"net": ["{network}"], "text": "{caption}", "media": "{media_urls}", "alt": "{alt_texts}", "type": "{media_type}"},
                     "id_path": "post.id", "url_path": "post.url"}}
    n = {"i": 0}

    def h(method, url, kw):
        if url.endswith("/media"):
            n["i"] += 1
            return FakeResp({"data": {"id": f"M{n['i']}", "url": f"https://cdn/m{n['i']}.png"}})
        return FakeResp({"post": {"id": "P1", "url": "https://ig/p1"}})
    s = FakeSession(h)
    fs = _car(ep)
    it = {"title": "t", "caption": "cap", "visibility": "public", "media_type": "carousel", "alt_texts": ["a", "b", "c"], "_files": [str(f) for f in fs]}
    res = AggregatorPublisher(network="instagram", config=conf, session=s).publish(it, fs[0], lambda p: None)
    assert res["id"] == "P1"
    assert [c[1] for c in s.calls] == ["https://agg.example/media"] * 3 + ["https://agg.example/post"]
    assert s.calls[3][2]["json"] == {"net": ["ig"], "text": "cap", "media": ["https://cdn/m1.png", "https://cdn/m2.png", "https://cdn/m3.png"],
                                     "alt": ["a", "b", "c"], "type": "carousel"}


def test_queue_run_hands_every_file_to_the_adapter(ep):
    fs = _car(ep)
    seen = {}

    class Spy(RecordingPublisher):
        def publish(self, item, video, progress):
            seen["files"] = item["_files"]
            seen["first"] = str(video)
            return {"id": "p", "url": "https://x/p"}
    p = {"instagram": Spy("instagram")}
    it = queue.create(ep, files=fs, media_type="carousel", alt_texts=[ALT] * 3, platforms=["instagram"], title="t", ai_disclosure=False, publishers=p)
    approve_item(ep, it["id"], p)
    assert queue.run(ep, it["id"], send=True, publishers=p)["ok"]
    assert seen["files"] == [str(f) for f in fs] and seen["first"] == str(fs[0])
    assert "_files" not in queue.load(ep, it["id"])


# ---- video items are untouched -------------------------------------------------------------------

def test_a_video_item_queues_and_hashes_exactly_as_before(ep, vertical):
    it = queue_item(ep, vertical)
    assert "media_type" not in it and "files" not in it and "alt_texts" not in it and "origin" not in it
    assert it["sha256"] == hashlib.sha256(vertical.read_bytes()).hexdigest() and it["video"] == "exports/shorts/clip1.mp4"
    assert queue.media_type(it) == "video"
    # the meta hash is the pre-image formula, byte for byte, so an approval signed before images existed still verifies
    old = hashlib.sha256(safety.canonical({k: it.get(k) for k in safety.META_FIELDS}).encode()).hexdigest()
    assert safety.meta_hash(it) == old
    done = approve_item(ep, it["id"])
    assert queue.send_blockers(ep, done) == [] and done["approvals"][0]["meta_sha256"] == old


def test_unknown_media_type_and_missing_files_are_refused(ep):
    with pytest.raises(ValueError, match="media_type"):
        queue.create(ep, media_type="gif", files=["x"], platforms=["instagram"], title="t")
    with pytest.raises(ValueError, match="need files"):
        queue.create(ep, media_type="image", platforms=["instagram"], title="t")
    with pytest.raises(ValueError, match="video is required"):
        queue.create(ep, platforms=["youtube"], title="t")
