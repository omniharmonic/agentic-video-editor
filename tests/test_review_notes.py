"""Timestamped cut notes: a human note on an exact edit reaches the agent, survives a re-cut, and closes with the answering version."""

import pytest

from ave import editdoc, review
from ave.episode import Episode


def _doc():
    seg = lambda i, o0, o1, e0: {"id": f"d{i}", "src": "cam", "o0": o0, "o1": o1, "e0": e0, "xfade_ms": 0, "confidence": 1.0, "reason": "t"}
    video = [{**seg(1, 0, 4, 10), "id": "v1"}, {**seg(2, 4, 8, 20), "id": "v2"}]
    return {"schema": 1, "stage": "rough", "duration": 8.0, "sources": {"cam": {"file": "raw/cam.mp4", "start_offset": 0}},
            "tracks": {"video": video, "dialogue": [seg(1, 0, 4, 10), seg(2, 4, 8, 20)], "music": [], "overlays": [], "captions": {"burn": False}}}


@pytest.fixture
def ep(tmp_path, monkeypatch):
    monkeypatch.setattr(editdoc, "validate", lambda doc: [])
    e = Episode(tmp_path / "ep")
    e.init() if hasattr(e, "init") else None
    editdoc.edits_dir(e).mkdir(parents=True, exist_ok=True)
    e.work.mkdir(parents=True, exist_ok=True)
    (editdoc.path_for(e, "rough", 1)).write_text(__import__("json").dumps(_doc()))
    return e


def test_note_records_episode_time_so_it_survives_a_recut(ep):
    n = review.add_note(ep, edit="rough.v1", t0=5.0, t1=6.5, text="Trim this pause", kind="pacing")
    assert (n["e0"], n["e1"]) == (21.0, 22.5) and n["id"] == "rn_0001"


def test_note_is_refused_outside_the_edit_or_empty(ep):
    for kw in ({"t0": 9.0}, {"t0": 3.0, "t1": 2.0}):
        with pytest.raises(ValueError):
            review.add_note(ep, edit="rough.v1", text="x", **kw)
    with pytest.raises(ValueError):
        review.add_note(ep, edit="rough.v1", t0=1.0, text="   ")
    with pytest.raises(ValueError):
        review.add_note(ep, edit="../../secret.v1", t0=1.0, text="x")
    with pytest.raises(ValueError):
        review.add_note(ep, edit="rough.v1", t0=1.0, text="x", kind="command")


def test_claim_hides_a_note_from_others_then_resolve_closes_it(ep):
    review.add_note(ep, edit="rough.v1", t0=1.0, text="first")
    review.add_note(ep, edit="rough.v1", t0=2.0, text="second")
    first = review.next_note(ep, agent="a")
    assert first["text"] == "first" and first["status"] == "claimed"
    assert review.next_note(ep, agent="b")["text"] == "second"
    assert review.next_note(ep) is None
    done = review.resolve(ep, first["id"], by="a", text="shortened", edit="rough.v1")
    assert done["status"] == "resolved" and done["resolution"]["edit"] == "rough.v1"
    with pytest.raises(ValueError):
        review.resolve(ep, first["id"], by="a")


def test_expired_claim_returns_to_open(ep):
    from datetime import timedelta
    review.add_note(ep, edit="rough.v1", t0=1.0, text="first")
    review.next_note(ep, agent="a")
    later = review._now() + timedelta(minutes=review.CLAIM_MINUTES + 1)
    assert review.notes(ep, "open", now=later)[0]["claim"] is None


def test_withdrawn_note_is_never_offered_and_a_torn_line_is_skipped(ep):
    n = review.add_note(ep, edit="rough.v1", t0=1.0, text="first")
    review.withdraw(ep, n["id"])
    with review.notes_path(ep).open("a") as f:
        f.write('{"op": "no')
    assert review.next_note(ep) is None and review.notes(ep)[0]["status"] == "withdrawn"


def test_seams_and_lanes_describe_the_cut(ep):
    t = review.timeline(review.load_edit(ep, "rough.v1"))
    assert t["seams"] == [{"o": 4, "removed_s": 6}]
    assert [l["id"] for l in t["lanes"]] == ["picture", "dialogue", "music", "overlays"] and len(t["lanes"][0]["items"]) == 2


def test_picture_change_holds_the_shot_and_leaves_words_exact(ep):
    doc = review.load_edit(ep, "rough.v1")
    done = review.set_picture(ep, "rough.v1", 4.0, 6.0, src=None, note="rn_0001")
    new = review.load_edit(ep, done["edit"])
    assert done["edit"] == "rough.v2" and new["tracks"]["dialogue"] == doc["tracks"]["dialogue"] and new["duration"] == doc["duration"]
    video = sorted(new["tracks"]["video"], key=lambda s: s["o0"])
    assert [(s["o0"], s["o1"], s["e0"]) for s in video] == [(0, 4, 10), (4.0, 6.0, 20), (6.0, 8, 22)] and video[1]["reason"] == "director note rn_0001"
    assert review.load_edit(ep, "rough.v1")["tracks"]["video"] == doc["tracks"]["video"]


def test_picture_change_refuses_a_camera_that_is_not_in_the_edit(ep):
    with pytest.raises(ValueError):
        review.set_picture(ep, "rough.v1", 1.0, 2.0, src="Z9")
    with pytest.raises(ValueError):
        review.set_picture(ep, "rough.v1", 7.0, 9.0)


def test_review_page_note_reaches_the_agent(tmp_path, monkeypatch):
    import shutil
    import subprocess
    import threading
    from clips_synth import build_long_episode, pick
    from ave import clips
    from ave.ui.server import UiServer
    playwright = pytest.importorskip("playwright.sync_api")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg required")
    e = build_long_episode(tmp_path / "ep")
    clips.prepare(e, min_s=10, max_s=25)
    clips.propose(e, [pick(1, 5, "Systems beat goals")])
    edit = review.edits(e)[-1]["edit"]
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=c=0x708a76:s=320x180:d=20", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(e.exports / f"{edit}.proxy.mp4")], check=True)
    srv = UiServer(e, port=0, by="director")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with playwright.sync_playwright() as p:
            b = p.chromium.launch()
            page = b.new_page(viewport={"width": 1440, "height": 900})
            errors = []
            page.on("pageerror", lambda x: errors.append(str(x)))
            page.goto(f"http://127.0.0.1:{srv.port}/?t={srv.token}#/review")
            page.locator(".rv-track").wait_for()
            box = page.locator(".rv-track").bounding_box()
            page.mouse.move(box["x"] + box["width"] * .3, box["y"] + 40)
            page.mouse.down()
            page.mouse.move(box["x"] + box["width"] * .4, box["y"] + 40, steps=4)
            page.mouse.up()
            assert " to " in page.locator(".rv-form strong").inner_text()
            page.get_by_label("Kind of note").select_option("pacing")
            page.get_by_label("Note for the editor").fill("Tighten this pause")
            page.get_by_role("button", name="Send to the editor").click()
            page.locator(".rv-note").filter(has_text="Tighten this pause").wait_for()
            assert page.locator(".rv-mark").count() == 1
            import os
            shot = os.environ.get("AVE_REVIEW_SHOT")
            if shot:
                page.screenshot(path=shot)
            assert not errors, errors
            b.close()
    finally:
        srv.shutdown()
        srv.server_close()
    note = review.next_note(e, agent="claude")
    assert note["text"] == "Tighten this pause" and note["kind"] == "pacing" and note["by"] == "director" and note["t1"] > note["t0"]


def test_a_dark_clip_measures_as_low_light(tmp_path):
    import shutil
    import subprocess
    from ave import shorts
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg required")
    dark, bright = tmp_path / "dark.mp4", tmp_path / "bright.mp4"
    for path, color in ((dark, "0x101010"), (bright, "0xb0b0b0")):
        subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", f"color=c={color}:s=160x90:d=6", "-pix_fmt", "yuv420p", str(path)], check=True)
    assert shorts.median_luma(dark) < shorts.LOW_LIGHT_LUMA < shorts.median_luma(bright)
