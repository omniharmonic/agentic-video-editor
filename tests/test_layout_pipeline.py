"""The same pipeline on the legacy flat layout AND the creator's numbered layout (02_FOOTAGE/03_AUDIO ... 06_EXPORTS).

`layout_episode` runs every test twice. Passing in both proves nothing is hardcoded to raw/ work/ edits/ exports/, and that
every path stored in an artifact is relative to the episode root."""

import json
import shutil

import pytest

from ave import audio, cleanup, clips, editdoc, final, handoff, media, shorts, state
from ave import compile as comp
from ave.episode import Episode, read_json, write_json
from clips_synth import build_long_episode, pick
from conftest import needs_ffmpeg
from test_shorts import NO_FACE, _make_clip

pytestmark = needs_ffmpeg


def under(ep, p, folder):
    return (ep.root / p).resolve().is_relative_to(folder.resolve())


def test_ingest_probes_every_raw_dir_with_relative_paths(layout_episode):
    ep = layout_episode
    for f in ep.work.glob("media.json"):
        f.unlink()
    (ep.raw / "._camA.mp4").write_bytes(b"appledouble junk")        # must not be probed
    r = media.ingest(ep)
    files = sorted(i["file"] for i in r["files"])
    assert files == sorted(ep.rel(p) for p in (ep.raw / "camA.mp4", ep.raw / "camB.mp4", ep.raw_dirs[-1] / "recorder.wav"))
    assert not any(f.startswith("/") for f in files)
    assert read_json(ep.work / "media.json")["files"] and ep.meta_path.exists()


def test_audio_lands_in_work_and_originals_are_untouched(layout_episode):
    ep = layout_episode
    before = {p: p.stat().st_mtime_ns for p in ep.media_files()}
    shutil.rmtree(ep.work / "audio")
    outs = audio.extract_all(ep)
    assert outs and all(under(ep, o, ep.work / "audio") for o in outs)
    assert before == {p: p.stat().st_mtime_ns for p in ep.media_files()}


def test_assembly_edit_lives_in_the_layouts_edits_dir(layout_episode):
    ep = layout_episode
    doc = editdoc.load_latest(ep, "assembly")
    assert editdoc.validate(doc) == []
    assert editdoc.path_for(ep, "assembly", 1).parent == ep.edits and editdoc.path_for(ep, "assembly", 1).exists()
    assert all(s["file"].startswith(ep.rel(ep.raw)) for s in doc["sources"].values())
    assert state.status(ep)["edits"] == {"assembly": [1]}
    assert state.status(ep)["next"]["stage"] == "rough"


def test_cleanup_then_final_render_through_the_layout(layout_episode):
    ep = layout_episode
    rep = cleanup.run(ep)
    assert under(ep, rep["rough"]["file"], ep.edits)
    out = final.build_final(ep, render=True, do_retranscribe=False, preset="proxy")
    assert editdoc.list_versions(ep, "final") == [1, 2]
    doc = editdoc.load_latest(ep, "final")
    assert under(ep, doc["captions"]["srt"], ep.captions) and under(ep, doc["captions"]["ass"], ep.captions)
    assert (ep.root / doc["captions"]["srt"]).exists()
    assert out["render"]["duration"] == pytest.approx(doc["duration"], abs=0.1)
    assert any(ep.exports.glob("final.v*.mp4"))
    assert state.status(ep)["stages"][[s["stage"] for s in state.status(ep)["stages"]].index("final")]["done"]


def test_render_writes_to_the_layouts_exports(layout_episode):
    ep = layout_episode
    out = ep.exports / "assembly.mp4"
    comp.render(ep, editdoc.load_latest(ep, "assembly"), out, preset="proxy")
    assert out.exists()


def test_handoff_plan_places_every_segment(layout_episode):
    ep = layout_episode
    doc = editdoc.load_latest(ep, "assembly")
    p = handoff.plan(ep, doc, name="t")
    v = p["video"]
    assert v and all(a["record_frame"] + a["frames"] == b["record_frame"] for a, b in zip(v, v[1:]))
    assert all(under(ep, x["file"], ep.raw) or under(ep, x["file"], ep.raw_dirs[-1]) for x in v)


def test_shorts_build_and_export_inside_the_layout(layout_episode):
    ep = layout_episode
    clip = _make_clip(ep)
    assert str(ep.edits.relative_to(ep.root)) in clip["edit"]["path"]
    r = shorts.build_short(ep, clip, detector=NO_FACE)
    doc = editdoc.load(ep.root / r["path"])
    assert under(ep, doc["captions"]["srt"], ep.captions)
    ex = shorts.export_short(ep, clip, ["youtube"])
    assert under(ep, ex["exports"][0]["video"], ep.exports / "shorts")
    assert under(ep, ex["exports"][0]["sidecar"], ep.exports / "shorts")
    assert shorts.list_shorts(ep)[0]["slug"] == "synth-clip"


@pytest.mark.parametrize("layout", ["flat", "numbered"])
def test_clips_prepare_propose_build(tmp_path, layout):
    ep = build_long_episode(tmp_path / "ep", layout=layout)
    clips.prepare(ep, min_s=10, max_s=25)
    assert (ep.work / "clips_candidates.json").exists()
    clips.propose(ep, [pick(5, 9)])
    built = clips.build(ep)
    sel = ep.root / built["clips"]["c01"]
    assert sel.exists() and sel.parent == ep.edits
    assert editdoc.load(sel)["sources"]["A"]["file"].startswith(ep.rel(ep.raw))
