"""Clip previews: contract shape, offsets, tight variant, resumability, bad input. Real renders on the synthetic episode."""

import json

import pytest

from ave import editdoc, ffutil, previews
from ave.cli import main
from ave.episode import read_json, write_json

from conftest import needs_ffmpeg

pytestmark = needs_ffmpeg

KEYS = {"file", "tight_file", "pad_before_s", "pad_after_s", "clip_in_s", "clip_out_s", "edit", "rendered_at", "sha"}


def make_clips(ep, specs):
    """clips.json with hand-placed clips over the assembly edit: specs = [(id, start, end, extra)]."""
    doc = editdoc.load_latest(ep, "assembly")
    src = {"stage": "assembly", "name": None, "version": editdoc.list_versions(ep, "assembly")[-1]}
    clips = [{"id": i, "slug": i, "title": i, "start": a, "end": b, "duration": b - a, "status": "proposed", "total": 80 - n,
              "session": None, "source": src, **extra} for n, (i, a, b, extra) in enumerate(specs)]
    write_json(ep.artifact("clips.json"), {"version": 2, "sessions": {}, "clips": clips, "rejected_picks": [], "source": src})
    return doc, clips


def quiet(ep, **kw):
    kw.setdefault("workers", 2)
    kw.setdefault("progress_log", False)
    return previews.run(ep, log=lambda m: None, **kw)


@pytest.fixture
def ep(synth_episode):
    doc = editdoc.load_latest(synth_episode, "assembly")
    assert doc["duration"] > 12
    return synth_episode


def test_contract_shape_and_offsets(ep):
    doc, _ = make_clips(ep, [("c1", 5.0, 8.0, {}), ("c2", 0.5, 2.5, {})])
    r = quiet(ep, pad=2.0)
    assert not r["failed"] and len(r["rendered"]) == 2
    idx = read_json(previews.index_path(ep))
    assert set(idx) == {"c1", "c2"}
    for k, e in idx.items():
        assert set(e) == KEYS and e["tight_file"] is None and e["edit"].startswith("assembly.v")
    c1, c2 = idx["c1"], idx["c2"]
    assert (c1["pad_before_s"], c1["pad_after_s"], c1["clip_in_s"], c1["clip_out_s"]) == (2.0, 2.0, 2.0, 5.0)
    # clamped at the start of the edit: only 0.5 s of lead-in
    assert (c2["pad_before_s"], c2["clip_in_s"], c2["clip_out_s"]) == (0.5, 0.5, 2.5)
    for e, want in ((c1, 7.0), (c2, 4.5)):
        info = ffutil.probe(previews.previews_dir(ep) / e["file"])
        assert info["has_audio"] and info["has_video"] and info["h"] <= 540
        assert info["duration"] == pytest.approx(want, abs=1 / 30 + 0.01)
    assert not list((ep.work / "render").glob("*")) if (ep.work / "render").exists() else True


def test_clamped_at_the_end_and_height(ep):
    d = editdoc.load_latest(ep, "assembly")
    make_clips(ep, [("c1", d["duration"] - 3.0, d["duration"] - 1.0, {})])
    quiet(ep, pad=5.0, height=180)
    e = read_json(previews.index_path(ep))["c1"]
    assert e["pad_after_s"] == pytest.approx(1.0, abs=1e-3) and e["pad_before_s"] == 5.0
    info = ffutil.probe(previews.previews_dir(ep) / e["file"])
    assert info["h"] == 180
    assert info["duration"] == pytest.approx(5.0 + 2.0 + 1.0, abs=0.05)


def test_resumable_and_force(ep):
    make_clips(ep, [("c1", 3.0, 6.0, {})])
    assert len(quiet(ep, pad=1.0)["rendered"]) == 1
    p = previews.previews_dir(ep) / "c1.mp4"
    m1 = p.stat().st_mtime_ns
    r = quiet(ep, pad=1.0)
    assert r["rendered"] == [] and r["skipped"] == ["c1"] and p.stat().st_mtime_ns == m1
    assert len(quiet(ep, pad=1.0, force=True)["rendered"]) == 1
    # a different pad changes the sha: re-rendered
    assert len(quiet(ep, pad=2.0)["rendered"]) == 1
    assert read_json(previews.index_path(ep))["c1"]["pad_before_s"] == 2.0
    # a lost file is re-rendered even though the sha still matches
    p.unlink()
    assert len(quiet(ep, pad=2.0)["rendered"]) == 1 and p.exists()


def test_other_entries_survive_and_ids_top_select(ep):
    make_clips(ep, [("c1", 2.0, 4.0, {}), ("c2", 5.0, 7.0, {}), ("c3", 8.0, 10.0, {})])
    quiet(ep, pad=1.0, ids=["c3"])
    quiet(ep, pad=1.0, top=1)           # best by total = c1
    assert set(read_json(previews.index_path(ep))) == {"c1", "c3"}
    with pytest.raises(SystemExit, match="Unknown clip id"):
        quiet(ep, ids=["nope"])


def test_tight_variant_removes_the_ranges(ep):
    d = editdoc.load_latest(ep, "assembly")
    e0 = d["tracks"]["dialogue"][0]["e0"]
    # clip 3-9 s of the edit, tighten 5-6 s (episode time == output time here: the assembly removes nothing)
    tighten = [{"start": e0 + 5.0, "end": e0 + 6.0, "kind": "filler", "text": "um", "saves_s": 1.0, "reason": "t", "confidence": 0.9}]
    make_clips(ep, [("c1", 3.0, 9.0, {"tighten": tighten})])
    r = quiet(ep, pad=1.0)
    assert not r["failed"] and r["rendered"][0]["tight_file"] == "c1.tight.mp4"
    e = read_json(previews.index_path(ep))["c1"]
    assert e["tight_file"] == "c1.tight.mp4"
    t = ffutil.probe(previews.previews_dir(ep) / e["tight_file"])
    assert t["has_audio"] and t["duration"] == pytest.approx(5.0, abs=1 / 30 + 0.01)
    full = ffutil.probe(previews.previews_dir(ep) / e["file"])
    assert full["duration"] == pytest.approx(8.0, abs=1 / 30 + 0.01)
    # tighten changes the sha; removing it re-renders without a tight file
    make_clips(ep, [("c1", 3.0, 9.0, {})])
    assert len(quiet(ep, pad=1.0)["rendered"]) == 1
    assert read_json(previews.index_path(ep))["c1"]["tight_file"] is None


def test_tight_joins_are_covered_by_the_other_camera():
    doc = {"sources": {"A": {"file": "a", "kind": "video", "start_offset": 0.0}, "B": {"file": "b", "kind": "video", "start_offset": 0.0}},
           "tracks": {"video": [{"id": "x1", "src": "A", "o0": 0.0, "o1": 3.0, "e0": 0.0}, {"id": "x2", "src": "A", "o0": 3.0, "o1": 8.0, "e0": 4.0}]}}
    assert previews.cover_joins(doc, [3.0]) == 1
    v = doc["tracks"]["video"]
    assert [(s["src"], s["o0"], s["o1"], s["e0"]) for s in v] == [("A", 0.0, 3.0, 0.0), ("B", 3.0, 3.6, 4.0), ("A", 3.6, 8.0, 4.6)]
    assert previews.cover_joins(doc, [3.0]) == 0      # already covered: B on one side


def test_dry_run_estimates_and_writes_nothing(ep):
    make_clips(ep, [("c1", 3.0, 6.0, {})])
    r = previews.run(ep, dry_run=True, log=lambda m: None, progress_log=False)
    assert r["would_render"] == ["c1"] and r["estimate_bytes"] > 0 and r["free_bytes"] > 0
    assert not previews.previews_dir(ep).exists()


def test_refuses_when_the_disk_is_too_full(ep, monkeypatch):
    make_clips(ep, [("c1", 3.0, 6.0, {})])
    monkeypatch.setattr(previews.shutil, "disk_usage", lambda p: type("U", (), {"free": 1000})())
    with pytest.raises(SystemExit, match="Refusing"):
        quiet(ep)
    assert not previews.previews_dir(ep).exists()


def test_bad_input_errors(ep):
    with pytest.raises(SystemExit, match="No clips"):
        quiet(ep)
    make_clips(ep, [("c1", 3.0, 6.0, {})])
    with pytest.raises(SystemExit, match="--pad"):
        quiet(ep, pad=-1)
    with pytest.raises(SystemExit, match="--height"):
        quiet(ep, height=9999)
    make_clips(ep, [("c1", 3.0, 9999.0, {})])
    with pytest.raises(SystemExit, match="outside"):
        quiet(ep)
    make_clips(ep, [("c1", 3.0, 6.0, {})])
    editdoc.path_for(ep, "assembly", 1).unlink()
    with pytest.raises(SystemExit, match="missing"):
        quiet(ep)


def test_a_failing_render_is_reported_loudly_and_others_survive(ep, monkeypatch):
    make_clips(ep, [("c1", 2.0, 4.0, {}), ("c2", 5.0, 7.0, {})])
    real = previews.comp.render

    def boom(ep_, doc, out, **kw):
        if "c1" in str(out):
            raise RuntimeError("encoder exploded")
        return real(ep_, doc, out, **kw)
    monkeypatch.setattr(previews.comp, "render", boom)
    r = quiet(ep, pad=1.0)
    assert [f["id"] for f in r["failed"]] == ["c1"] and "encoder exploded" in r["failed"][0]["error"]
    assert set(read_json(previews.index_path(ep))) == {"c2"}


def test_verification_catches_a_wrong_length(ep, tmp_path):
    make_clips(ep, [("c1", 3.0, 6.0, {})])
    quiet(ep, pad=1.0)
    with pytest.raises(previews.PreviewError, match="duration"):
        previews._verify(previews.previews_dir(ep) / "c1.mp4", 20.0, "c1")


def test_cli(ep, capsys):
    make_clips(ep, [("c1", 3.0, 6.0, {})])
    assert main(["clips", "previews", str(ep.root), "--pad", "1", "--no-progress-log", "--workers", "1"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["rendered"][0]["id"] == "c1"
    assert main(["clips", "previews", str(ep.root), "--pad", "1", "--no-progress-log", "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["skipped"] == ["c1"]


def test_probe_reports_nominal_rate(tmp_path):
    """Verification uses the nominal r_frame_rate; avg_frame_rate flaked 29.95-29.97 on frame-exact preview files."""
    from ave import ffutil
    import subprocess
    out = tmp_path / "t.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=30:duration=1",
                    "-f", "lavfi", "-i", "sine=duration=1", "-shortest", "-c:v", "libx264", str(out)], check=True)
    assert abs(ffutil.probe(out)["r_fps"] - 30.0) < 1e-6
