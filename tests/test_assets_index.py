"""Local footage index: scene detection, thumbnails, quality metrics, agent tags, search, contact sheets."""
import hashlib
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from ave.assets import index

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def ff(*args):
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *args], check=True)


@pytest.fixture(scope="module")
def footage(tmp_path_factory):
    d = tmp_path_factory.mktemp("footage")
    enc = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-an"]
    # 3 flat scenes of 2 s each: blue -> yellow -> dark green (luma-contrasting: ffmpeg's scene score is luma-led), hard cuts
    ff(*sum((["-f", "lavfi", "-i", f"color=c={c}:s=320x180:r=25:d=2"] for c in ("0x2040ff", "0xf0e060", "0x108020")), []),
       "-filter_complex", "[0][1][2]concat=n=3:v=1:a=0[v]", "-map", "[v]", *enc, str(d / "scenes.mp4"))
    ff("-f", "lavfi", "-i", "testsrc2=s=320x180:r=25:d=3", *enc, str(d / "sharp.mp4"))
    ff("-f", "lavfi", "-i", "testsrc2=s=320x180:r=25:d=3", "-vf", "gblur=sigma=10", *enc, str(d / "soft.mp4"))
    ff("-f", "lavfi", "-i", "color=c=black:s=320x180:r=25:d=2", *enc, str(d / "dark.mp4"))
    ff("-f", "lavfi", "-i", "color=c=white:s=320x180:r=25:d=2", *enc, str(d / "white.mp4"))
    ff("-f", "lavfi", "-i", "testsrc2=s=180x320:r=25:d=2", *enc, str(d / "portrait.mp4"))
    (d / "notes.txt").write_text("not media")
    return d


@pytest.fixture
def db(tmp_path):
    return tmp_path / "assets.db"


@pytest.fixture
def indexed(footage, db):
    index.index_folder(footage, db_path=db)
    return db


def shots_of(db, name):
    return [s for s in index.all_shots(db) if Path(s["file"]).name == name]


def test_scene_detection_finds_the_two_hard_cuts(indexed):
    s = shots_of(indexed, "scenes.mp4")
    assert len(s) == 3
    assert [round(x["t0"], 1) for x in s] == pytest.approx([0.0, 2.0, 4.0], abs=0.2)
    assert s[-1]["t1"] == pytest.approx(6.0, abs=0.1)


def test_non_media_skipped_and_raw_untouched(footage, db):
    before = {p.name: hashlib.md5(p.read_bytes()).hexdigest() for p in footage.iterdir()}
    res = index.index_folder(footage, db_path=db)
    after = {p.name: hashlib.md5(p.read_bytes()).hexdigest() for p in footage.iterdir()}
    assert before == after
    assert res["files"] == 6 and "notes.txt" not in str(res)


def test_thumbnail_per_shot_shows_the_scene_color(indexed):
    s = shots_of(indexed, "scenes.mp4")
    for shot, dominant in zip(s, (2, None, 1)):  # blue, yellow (r and g high), green channels (RGB)
        im = np.asarray(Image.open(shot["thumb"]).convert("RGB")).reshape(-1, 3).mean(0)
        if dominant is None:
            assert im[0] > 180 and im[1] > 180 and im[2] < 140
        else:
            assert im.argmax() == dominant


def test_quality_metrics_are_sane(indexed):
    sharp, soft = shots_of(indexed, "sharp.mp4")[0], shots_of(indexed, "soft.mp4")[0]
    dark, white = shots_of(indexed, "dark.mp4")[0], shots_of(indexed, "white.mp4")[0]
    assert sharp["blur"] > 3 * soft["blur"]
    assert dark["brightness"] < 0.05 and "underexposed" in dark["flags"]
    assert white["brightness"] > 0.95 and "overexposed" in white["flags"]
    assert sharp["quality"] > soft["quality"] > dark["quality"]
    assert "blurry" in soft["flags"] and "blurry" not in sharp["flags"]
    assert sharp["face"] in (0, None) and 0 <= sharp["quality"] <= 1


def test_orientation_and_geometry(indexed):
    p = shots_of(indexed, "portrait.mp4")[0]
    assert (p["w"], p["h"], p["aspect"]) == (180, 320, "portrait")
    assert shots_of(indexed, "sharp.mp4")[0]["aspect"] == "landscape"
    assert shots_of(indexed, "sharp.mp4")[0]["fps"] == pytest.approx(25, abs=0.1)


def test_long_shots_are_split_when_sampling_window_set(footage, db):
    index.index_folder(footage, db_path=db, max_shot_s=1.2)
    assert len(shots_of(db, "sharp.mp4")) >= 3


def test_reindex_is_idempotent_and_keeps_tags(footage, db):
    index.index_folder(footage, db_path=db)
    n = len(index.all_shots(db))
    sid = shots_of(db, "scenes.mp4")[0]["id"]
    index.tag_shot(db, sid, "blue wall, calm")
    res = index.index_folder(footage, db_path=db)
    assert len(index.all_shots(db)) == n and res["skipped_unchanged"] == 6
    assert "calm" in index.get_shot(db, sid)["tags"]


def test_changed_file_is_reindexed_and_old_shots_dropped(tmp_path, db):
    d = tmp_path / "f"
    d.mkdir()
    enc = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-an"]
    ff("-f", "lavfi", "-i", "color=c=blue:s=160x90:r=25:d=2", *enc, str(d / "a.mp4"))
    index.index_folder(d, db_path=db)
    ff("-f", "lavfi", "-i", "testsrc2=s=160x90:r=25:d=4", *enc, str(d / "a.mp4"))
    index.index_folder(d, db_path=db)
    s = index.all_shots(db)
    assert len(s) == 1 and s[0]["t1"] == pytest.approx(4.0, abs=0.1)


def test_tags_are_first_class_and_searchable(indexed):
    s = shots_of(indexed, "scenes.mp4")
    index.tag_shot(indexed, s[0]["id"], "Hands, wet clay, studio window light")
    index.tag_shot(indexed, s[1]["id"], "rain on glass")
    r = index.search(indexed, "clay window")
    assert [x["id"] for x in r] == [s[0]["id"]] and r[0]["matched"] == ["clay", "window"]
    assert index.search(indexed, "rain")[0]["id"] == s[1]["id"]
    assert index.search(indexed, "volcano") == []
    index.tag_shot(indexed, s[0]["id"], "dusty", replace=True)
    assert index.get_shot(indexed, s[0]["id"])["tags"] == ["dusty"]


def test_search_filters_duration_aspect_quality(indexed):
    for s in index.all_shots(indexed):
        index.tag_shot(indexed, s["id"], "texture")
    names = lambda rs: {Path(r["file"]).name for r in rs}  # noqa: E731
    assert names(index.search(indexed, "texture", aspect="portrait")) == {"portrait.mp4"}
    assert "scenes.mp4" not in names(index.search(indexed, "texture", min_duration=2.5))
    assert "sharp.mp4" in names(index.search(indexed, "texture", min_duration=2.5))
    good = index.search(indexed, "texture", min_quality=0.5)
    assert all(r["quality"] >= 0.5 for r in good) and "dark.mp4" not in names(good)
    ranked = [r["quality"] for r in index.search(indexed, "texture")]
    assert ranked == sorted(ranked, reverse=True)


def test_untagged_shots_do_not_match_text_but_match_pure_filters(indexed):
    assert index.search(indexed, "anything") == []
    assert len(index.search(indexed, None, aspect="portrait")) == 1


def test_tag_unknown_shot_is_a_clear_error(indexed):
    with pytest.raises(KeyError, match="999"):
        index.tag_shot(indexed, 999, "x")


def test_contact_sheet_is_a_labeled_grid_of_the_requested_shots(indexed, tmp_path):
    s = shots_of(indexed, "scenes.mp4")
    out = index.contact_sheet(indexed, [x["id"] for x in s], tmp_path / "sheet.jpg", cols=3, cell_w=200)
    im = Image.open(out).convert("RGB")
    assert im.width == 3 * 200 and im.height > 112  # one row, label strip under each thumb
    a = np.asarray(im)
    cell = a[10:60, 10:60].reshape(-1, 3).mean(0)  # first cell is the blue scene
    assert cell.argmax() == 2
    # the label strip is not blank: it contains the drawn id text
    strip = a[-30:, :200].reshape(-1, 3)
    assert strip.std() > 5
    with pytest.raises(KeyError):
        index.contact_sheet(indexed, [424242], tmp_path / "x.jpg")


def test_embeddings_degrade_with_clear_message(monkeypatch):
    import builtins
    real = builtins.__import__

    def fake(name, *a, **k):
        if name.startswith("open_clip") or name == "torch":
            raise ImportError(name)
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake)
    assert index.embeddings_available() is False
    with pytest.raises(index.EmbeddingsUnavailable, match="open_clip"):
        index.embed_shots("unused.db", [1])
