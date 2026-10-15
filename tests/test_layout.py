"""Configurable project layout: resolution, ignored junk files, and a non-destructive init-episode."""

import json

import pytest

from ave import cli
from ave.episode import PRESETS, Episode, resolve_layout, write_json


def make(tmp_path, layout=None):
    ep = Episode(tmp_path / "ep")
    ep.create(title="t", layout=layout)
    return ep


def test_absent_layout_is_the_legacy_flat_layout(tmp_path):
    ep = make(tmp_path)
    assert "layout" not in json.loads(ep.meta_path.read_text())
    r = ep.root
    assert (ep.raw, ep.work, ep.edits, ep.captions, ep.publish, ep.exports, ep.gfx, ep.music) == (
        r / "raw", r / "work", r / "edits", r / "captions", r / "publish", r / "exports", r / "work/gfx", r / "work/music")
    assert ep.raw_dirs == [r / "raw"]


def test_numbered_preset(tmp_path):
    ep = make(tmp_path, "numbered")
    r = ep.root
    assert ep.raw_dirs == [r / "02_FOOTAGE", r / "03_AUDIO"] and ep.raw == r / "02_FOOTAGE"
    assert (ep.work, ep.edits, ep.captions, ep.publish) == tuple(r / "01_PROJECT" / n for n in ("work", "edits", "captions", "publish"))
    assert (ep.exports, ep.gfx, ep.music) == (r / "06_EXPORTS", r / "04_GFX", r / "05_MUSIC")
    assert json.loads(ep.meta_path.read_text())["layout"] == "numbered"
    assert Episode(ep.root).layout == ep.layout                      # re-read from episode.json


def test_preset_dict_and_explicit_overrides(tmp_path):
    ep = make(tmp_path, {"preset": "numbered", "exports": "OUT", "raw_dirs": ["A", "B", "C"]})
    assert ep.exports == ep.root / "OUT" and [d.name for d in ep.raw_dirs] == ["A", "B", "C"]
    assert ep.work == ep.root / "01_PROJECT/work"                    # untouched keys come from the preset
    flat_plus = resolve_layout({"edits": "cuts"})                    # no preset = flat + override
    assert flat_plus["edits"] == "cuts" and flat_plus["work"] == "work"
    assert resolve_layout({"raw_dirs": "footage"})["raw_dirs"] == ["footage"]


def test_bad_layouts_fail_clearly():
    with pytest.raises(SystemExit, match="unknown layout preset"):
        resolve_layout("weird")
    with pytest.raises(SystemExit, match="unknown layout key"):
        resolve_layout({"preset": "flat", "wrok": "x"})


def test_media_files_scan_every_raw_dir_and_ignore_junk(tmp_path):
    ep = make(tmp_path, "numbered")
    (ep.raw_dirs[0] / "day1").mkdir()
    good = [ep.raw_dirs[0] / "IMG_0002.mov", ep.raw_dirs[0] / "day1" / "IMG_0003.MOV", ep.raw_dirs[1] / "rec.wav"]
    junk = [ep.raw_dirs[0] / "._IMG_0002.mov", ep.raw_dirs[1] / "._rec.wav", ep.raw_dirs[0] / ".DS_Store",
            ep.raw_dirs[0] / ".Spotlight-V100" / "Store.mov", ep.raw_dirs[0] / ".fseventsd" / "x.mp4",
            ep.raw_dirs[0] / "notes.txt", ep.raw_dirs[0] / "$RECYCLE.BIN" / "y.mp4"]
    for p in good + junk:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    assert ep.media_files() == sorted(good)


def test_rel_is_root_relative_posix(tmp_path):
    ep = make(tmp_path, "numbered")
    assert ep.rel(ep.raw / "a.mov") == "02_FOOTAGE/a.mov" and ep.rel("work/x") == "work/x"


# ---- ave init-episode --------------------------------------------------------------------------------------------

def run_init(capsys, *argv):
    assert cli.main(["init-episode", *map(str, argv)]) == 0
    return json.loads(capsys.readouterr().out)


def test_init_auto_picks_numbered_when_footage_folder_exists_and_never_touches_files(tmp_path, capsys):
    d = tmp_path / "00"
    (d / "02_FOOTAGE").mkdir(parents=True)
    mov, apple = d / "02_FOOTAGE" / "IMG_0002.mov", d / "02_FOOTAGE" / "._IMG_0002.mov"
    mov.write_bytes(b"footage"); apple.write_bytes(b"junk")
    (d / "01_PROJECT").mkdir(); keep = d / "01_PROJECT" / "my-notes.txt"; keep.write_text("mine")
    before = {p: p.read_bytes() for p in d.rglob("*") if p.is_file()}
    r = run_init(capsys, d)
    assert r["layout"] == "numbered" and r["created_episode_json"]
    assert {p: p.read_bytes() for p in before} == before             # nothing moved or rewritten
    assert (d / "06_EXPORTS").is_dir() and (d / "03_AUDIO").is_dir() and (d / "01_PROJECT/work").is_dir()
    assert Episode(d).media_files() == [mov]


def test_init_is_idempotent_and_keeps_existing_episode_json(tmp_path, capsys):
    d = tmp_path / "e"
    run_init(capsys, d, "--title", "First")
    meta = json.loads((d / "episode.json").read_text())
    meta["speakers"] = {"S1": {"name": "Ada"}}
    write_json(d / "episode.json", meta)
    (d / "02_FOOTAGE").mkdir()                                        # now it LOOKS numbered, but the episode is already flat
    r = run_init(capsys, d, "--title", "Second", "--layout", "numbered")
    assert not r["created_episode_json"] and r["layout"] == "flat" and "untouched" in r["note"]
    assert json.loads((d / "episode.json").read_text()) == meta


def test_init_explicit_layouts(tmp_path, capsys):
    assert run_init(capsys, tmp_path / "a", "--layout", "numbered")["layout"] == "numbered"
    assert run_init(capsys, tmp_path / "b")["layout"] == "flat"
    assert PRESETS["flat"]["raw_dirs"] == ["raw"]
