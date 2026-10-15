import copy

import pytest

from ave import editdoc
from ave.editdoc import TimeMap, new_doc, validate

SRC = {"A": {"file": "raw/a.mov", "kind": "video", "start_offset": 0.0},
       "B": {"file": "raw/b.mov", "kind": "video", "start_offset": 4.25}}


def good():
    d = new_doc("assembly", 30, (1920, 1080), copy.deepcopy(SRC), 10.0)
    d["tracks"]["video"] = [
        {"id": "v1", "src": "A", "o0": 0, "o1": 4, "e0": 0}, {"id": "v2", "src": "B", "o0": 4, "o1": 10, "e0": 4}]
    d["tracks"]["dialogue"] = [{"id": "d1", "src": "A", "o0": 0, "o1": 10, "e0": 0}]
    return d


def test_valid_doc_passes():
    assert validate(good()) == []


def test_reports_every_problem_not_just_the_first():
    d = good()
    d["tracks"]["video"][1]["o0"] = 5            # gap
    d["tracks"]["dialogue"][0]["src"] = "Z"      # unknown source
    d["duration"] = 12.0                          # tracks end early
    p = validate(d)
    assert any("gap" in x for x in p) and any("unknown source" in x for x in p) and any("duration" in x for x in p)


def test_overlap_allowed_only_within_crossfade():
    d = good()
    d["tracks"]["dialogue"] = [{"id": "d1", "src": "A", "o0": 0, "o1": 5.02, "e0": 0},
                               {"id": "d2", "src": "A", "o0": 5.0, "o1": 10, "e0": 9, "xfade_ms": 20}]
    assert validate(d) == []
    d["tracks"]["dialogue"][1]["xfade_ms"] = 0
    assert any("overlap" in x for x in validate(d))


def test_timemap_removed_sections():
    m = TimeMap([(0, 5), (8, 12)])               # episode 5..8 removed
    assert m.duration == 9
    assert m.to_output(2) == 2 and m.to_output(9) == 6
    assert m.to_output(6) is None and m.to_output_snap(6) == 5
    assert m.to_episode(5.5) == 8.5


def test_source_time_applies_offset():
    assert editdoc.source_time(good(), "B", 10.0) == pytest.approx(5.75)


def test_versioning_never_overwrites(tmp_path):
    from ave.episode import Episode
    ep = Episode(tmp_path); ep.create()
    p1 = editdoc.save_new_version(ep, good()); p2 = editdoc.save_new_version(ep, good())
    assert p1.name == "assembly.v1.json" and p2.name == "assembly.v2.json"
    assert editdoc.latest_path(ep, "assembly") == p2
    bad = good(); bad["tracks"]["video"] = []
    with pytest.raises(ValueError):
        editdoc.save_new_version(ep, bad)


def test_slice_doc_shifts_and_validates():
    d = good()
    s = editdoc.slice_doc(d, 3.0, 7.0, stage="select")
    assert s["stage"] == "select" and s["duration"] == 4.0 and validate(s) == []
    v = s["tracks"]["video"]
    assert [(x["src"], x["o0"], x["o1"], x["e0"]) for x in v] == [("A", 0.0, 1.0, 3.0), ("B", 1.0, 4.0, 4.0)]
    assert s["tracks"]["dialogue"][0]["e0"] == 3.0
    with pytest.raises(ValueError):
        editdoc.slice_doc(d, 5, 20)


def test_source_time_applies_drift_and_inverts():
    doc = good()
    doc["sources"]["B"]["drift_ppm"] = 80.0           # B's clock runs slow: it has recorded less file time than episode time
    t = editdoc.source_time(doc, "B", 1004.25)
    assert t == pytest.approx(1000.0 / 1.00008)
    assert t < 1000.0 - 0.07                           # ~80 ms of drift after 1000 s
    assert editdoc.episode_time(doc["sources"]["B"], t) == pytest.approx(1004.25)
    assert editdoc.source_time(doc, "A", 12.0) == 12.0   # no drift_ppm key = legacy behaviour


def test_validate_rejects_nonsense_drift():
    doc = good()
    doc["sources"]["B"]["drift_ppm"] = "fast"
    assert any("drift_ppm" in p for p in editdoc.validate(doc))


def test_validate_checks_channel_map_ranges():
    doc = good()
    doc["sources"]["B"]["channel_map"] = [{"start": 0.0, "end": 10.0, "use": "right"}]
    assert editdoc.validate(doc) == []
    doc["sources"]["B"]["channel_map"] = [{"start": 5.0, "end": 1.0, "use": "up"}]
    assert any("channel_map" in p for p in editdoc.validate(doc))


def test_piecewise_sync_map_interpolates_and_round_trips():
    src = {"start_offset": 2.0, "drift_ppm": 50.0, "sync_map": [[0.0, 2.0], [100.0, 2.04], [200.0, 2.01]]}
    for t in (0.0, 37.5, 100.0, 150.0, 200.0, 260.0, -5.0):      # inside, on the knots, and extrapolated both ways
        assert editdoc.src_time(src, editdoc.episode_time(src, t)) == pytest.approx(t, abs=1e-9)
    assert editdoc.episode_time(src, 100.0) == pytest.approx(102.04)             # exactly through the measured point
    assert editdoc.episode_time(src, 50.0) == pytest.approx(52.02)               # halfway between knots
    assert editdoc.source_time({"sources": {"X": src}}, "X", 102.04) == pytest.approx(100.0)
    assert editdoc.clock_rate(src, 60.0) == pytest.approx(1.0004)
    assert editdoc.clock_rate(src, 160.0) == pytest.approx(0.9997)
    assert editdoc.src_time(src, __import__("numpy").array([2.0, 102.04]))[1] == pytest.approx(100.0)


def test_sources_without_a_sync_map_behave_exactly_as_before():
    src = {"start_offset": 4.25, "drift_ppm": 80.0}
    assert editdoc.src_time(src, 100.0) == pytest.approx((100.0 - 4.25) / (1 + 80e-6))
    assert editdoc.clock_rate(src, 50.0) == pytest.approx(1 + 80e-6)
    one = dict(src, sync_map=[[1.0, 4.25]])                    # a single point is not a map
    assert editdoc.src_time(one, 100.0) == editdoc.src_time(src, 100.0)


def test_validate_rejects_a_malformed_sync_map():
    d = good()
    d["sources"]["B"]["sync_map"] = [1, 2]
    assert any("sync_map" in x for x in validate(d))
    d["sources"]["B"]["sync_map"] = [[0, 4.25], [100, 4.3]]
    assert validate(d) == []


def test_every_consumer_follows_the_sync_map_not_a_copy_of_the_formula():
    """Envelope (cleanup snapping) and cutplan.to_frames derive source positions; with a map they must agree with src_time."""
    import numpy as np
    from ave import cleanup, cutplan
    sm = [[0.0, 2.0], [100.0, 2.05], [200.0, 2.0]]
    src = {"start_offset": 2.0, "drift_ppm": 0.0, "sync_map": sm}
    env = cleanup.Envelope(np.zeros(16000 * 5, np.float32), 16000, 2.0, drift=0.0, sync_map=sm)
    for e in (2.0, 60.0, 102.05, 150.0):
        assert env._file_t(e) == pytest.approx(editdoc.src_time(src, e), abs=1e-9)
        assert env._episode_t(env._file_t(e)) == pytest.approx(e, abs=1e-9)
    shot = {"camera": "A", "speaker": "S1", "reason": "x", "start": 102.05, "end": 104.05}
    fr = cutplan.to_frames([shot], 30.0, {"A": 2.0}, {"A": "a.mov"}, {"A": 0.0}, {"A": src})
    assert fr[0]["source_start_frame"] == round(100.0 * 30)                       # through the map, not start_offset alone
    plain = cutplan.to_frames([shot], 30.0, {"A": 2.0}, {"A": "a.mov"}, {"A": 0.0})
    assert plain[0]["source_start_frame"] == round(100.05 * 30)                    # legacy call signature unchanged
