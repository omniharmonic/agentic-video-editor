"""Instagram takes JPEG only, so the JPEG is made BEFORE review and the approval binds that JPEG (integration item e)."""

import hashlib
from pathlib import Path

import pytest
from PIL import Image
from sched_helpers import make_world, no_agent_markers  # noqa: F401

from ave import library
from ave.publish import queue as pq
from ave.studio import deliverables, store
from ave.studio.sync import sync_queue


def real_png(path: Path, color=(30, 60, 90), size=(1080, 1350)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="PNG")
    return path


def png_post(w, account="ig-main", alt="A blue card with a quote."):
    png = real_png(w.sp.work / "gen" / "card.png")
    rec = library.add_file(w.sp, png, source={"type": "own"}, by="test", alt=alt)
    post = w.studio.create_post(idea="card", copy={"base": "Day 3: the starter smells like pears."},
                                media=[{"slot": 0, "asset": rec["id"], "role": "image", "alt": alt, "crop": {}}])
    plat = "instagram" if account.startswith("ig") else "facebook"
    post = w.studio.add_variant(post["id"], account=account, platform=plat, post_type="feed", expected_rev=post["rev"])
    return rec, post["id"], post["variants"][-1]["id"]


def test_a_png_on_an_instagram_variant_becomes_a_reviewed_jpeg(tmp_path):
    w = make_world(tmp_path)
    rec, pid, vid = png_post(w)
    assert rec["ext"] == ".png"
    rep = sync_queue(w.sp, pid, publishers=w.pubs, now=w.clock.now())
    assert rep["problems"] == [] and rep["converted"] == [{"variant": vid, "index": 0, "from": rec["id"], "to": rep["converted"][0]["to"], "platform": "instagram"}]
    v = w.variant(pid, vid)
    jpg = store.read_assets(w.sp)[v["media"][0]["asset"]]
    assert jpg["ext"] == ".jpg" and jpg["derived_from"] == [rec["id"]] and jpg["source"]["type"] == "render" and jpg["alt"] == rec["alt"]
    f = w.sp.root / jpg["file"]
    assert f.read_bytes()[:3] == b"\xff\xd8\xff" and Image.open(f).format == "JPEG" and Image.open(f).size == (1080, 1350)
    item = w.item(pid, vid)
    assert item["status"] == "awaiting_approval" and item["preflight_problems"] == []
    # the queue binds exactly that file: what the human is shown is what would be posted
    assert [Path(p).name for p in item["files"]] == [f.name] and item["sha256"] == pq.current_sha(w.sp, item)
    assert hashlib.sha256(f.read_bytes()).hexdigest() == jpg["sha256"]
    done = w.approve(pid, vid)
    assert done["approvals"][-1]["sha256"] == item["sha256"] and done["status"] == "approved"
    # the post keeps the original for other platforms; only the Instagram variant points at the JPEG
    assert store.load_post(w.sp, pid)["media"][0]["asset"] == rec["id"]


def test_conversion_is_idempotent_and_does_not_void_an_approval(tmp_path):
    w = make_world(tmp_path)
    rec, pid, vid = png_post(w)
    sync_queue(w.sp, pid, publishers=w.pubs, now=w.clock.now())
    w.approve(pid, vid)
    n_assets = len(store.read_assets(w.sp))
    for _ in range(3):
        rep = sync_queue(w.sp, pid, publishers=w.pubs, now=w.clock.now())
        assert rep["converted"] == [] and rep["variants"][0]["invalidated"] is False
    assert len(store.read_assets(w.sp)) == n_assets and w.item(pid, vid)["status"] == "approved"
    assert deliverables.ensure(w.sp, pid) == []


def test_platforms_that_take_png_are_left_alone(tmp_path):
    w = make_world(tmp_path, accounts=(("ig-main", "instagram"), ("fb-page", "facebook")))
    rec, pid, vid = png_post(w, account="fb-page")
    rep = sync_queue(w.sp, pid, publishers=w.pubs, now=w.clock.now())
    assert rep["converted"] == [] and w.variant(pid, vid)["media"] is None
    assert w.item(pid, vid)["files"][0].endswith(rec["ext"])


def test_a_jpeg_saved_with_a_png_name_is_not_reconverted(tmp_path):
    w = make_world(tmp_path)
    pid, vid = w.add_post()                          # tests' make_png writes real JPEG bytes into a .png name
    assert w.item(pid, vid)["preflight_problems"] == [] and w.variant(pid, vid)["media"] is None


def test_report_names_the_problem_before_sync_converts_it(tmp_path):
    w = make_world(tmp_path)
    rec, pid, vid = png_post(w)
    rows = deliverables.report(w.sp, store.load_post(w.sp, pid))
    assert rows[0]["accepted_formats"] == ["jpeg"] and rows[0]["media"][0]["format"] == "png" and rows[0]["problems"]
    deliverables.ensure(w.sp, pid)
    assert deliverables.report(w.sp, store.load_post(w.sp, pid))[0]["problems"] == []


def test_alpha_is_flattened_onto_white():
    from ave import imgfmt
    import io
    im = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    out = Image.open(io.BytesIO(imgfmt.to_jpeg_bytes(buf.getvalue())))
    assert out.format == "JPEG" and out.getpixel((5, 5))[0] > 240
    assert imgfmt.to_jpeg_bytes(buf.getvalue()) == imgfmt.to_jpeg_bytes(buf.getvalue())                 # deterministic
    assert imgfmt.needs_jpeg("instagram") and not imgfmt.needs_jpeg("bluesky") and not imgfmt.needs_jpeg("nonesuch")
