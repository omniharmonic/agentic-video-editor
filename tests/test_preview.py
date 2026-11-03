"""Fast section preview: find what an edit changed and render only that stretch, small, cached by content."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from ave import editdoc, ffutil, preview
from conftest import needs_ffmpeg


@pytest.fixture(autouse=True)
def _settle_measurements():
    yield
    preview.wait_for_measurements(60)           # a background gain measurement must not outlive its test's temp dir


def doc_of(ep):
    return editdoc.clone(editdoc.load_latest(ep, "assembly"))


def swap_video(doc: dict, i: int) -> dict:
    """Swap video segments i and i+1 in time (a moved shot); the track stays contiguous."""
    out = editdoc.clone(doc)
    v = out["tracks"]["video"]
    a, b = v[i], v[i + 1]
    start, end = a["o0"], b["o1"]
    mid = start + (b["o1"] - b["o0"])
    b2, a2 = dict(b, o0=start, o1=mid), dict(a, o0=mid, o1=end)
    v[i], v[i + 1] = b2, a2
    return out


def long_doc(dur: float = 2000.0) -> dict:
    """A hand-built long cut (no media needed): 20 s shots, one dialogue run, a music bed, one overlay."""
    doc = editdoc.new_doc("rough", 30.0, (1920, 1080), {"A": {"file": "raw/a.mp4", "kind": "video", "start_offset": 0.0, "drift_ppm": 0.0}}, dur)
    n = int(dur // 20)
    doc["tracks"]["video"] = [{"id": f"v{i}", "src": "A", "o0": i * 20.0, "o1": (i + 1) * 20.0, "e0": i * 20.0, "crop": None, "zoom": 1.0}
                              for i in range(n)]
    doc["tracks"]["dialogue"] = [{"id": f"d{i}", "src": "A", "o0": i * 20.0, "o1": (i + 1) * 20.0, "e0": i * 20.0, "xfade_ms": 0, "gain_db": 0.0}
                                 for i in range(n)]
    doc["tracks"]["music"] = [{"id": "m1", "file": "m.wav", "o0": 100.0, "o1": 160.0, "in": 0.0, "gain_db": -18.0}]
    doc["tracks"]["overlays"] = [{"id": "o1", "kind": "image", "file": "x.png", "o0": 500.0, "o1": 510.0}]
    assert not editdoc.validate(doc), editdoc.validate(doc)
    return doc


def test_identical_docs_have_no_span(synth_episode):
    doc = doc_of(synth_episode)
    assert preview.changed_span(doc, editdoc.clone(doc)) == []


def test_audio_only_change_is_found():
    before = long_doc()
    after = editdoc.clone(before)
    after["tracks"]["dialogue"][30]["gain_db"] = -6.0
    assert preview.changed_span(before, after) == [(600.0, 620.0)]


def test_music_change_is_found():
    before = long_doc()
    after = editdoc.clone(before)
    after["tracks"]["music"][0]["gain_db"] = -9.0
    assert preview.changed_span(before, after) == [(100.0, 160.0)]


def test_overlay_change_is_found():
    before = long_doc()
    after = editdoc.clone(before)
    after["tracks"]["overlays"][0]["file"] = "y.png"
    assert preview.changed_span(before, after) == [(500.0, 510.0)]


def test_a_moved_music_cue_is_a_change_where_it_was_and_where_it_is():
    before = long_doc()
    after = editdoc.clone(before)
    after["tracks"]["music"][0].update(o0=1000.0, o1=1060.0)
    assert preview.changed_span(before, after) == [(100.0, 160.0), (1000.0, 1060.0)]


def test_a_segment_slid_into_a_gap_is_a_change():
    before = long_doc()
    before["tracks"]["overlays"] = [{"id": "o1", "kind": "image", "file": "x.png", "o0": 500.0, "o1": 510.0}]
    after = editdoc.clone(before)
    after["tracks"]["overlays"][0].update(o0=520.0, o1=530.0)       # same material, 20 s later
    spans = preview.changed_span(before, after)
    assert spans and spans[0][0] <= 510.0 and spans[-1][1] >= 530.0


def test_a_lengthened_gap_is_found_once_not_for_everything_after_it():
    before = long_doc()
    before["tracks"]["overlays"] = [{"id": f"o{i}", "kind": "image", "file": "x.png", "o0": 100.0 * i, "o1": 100.0 * i + 10.0} for i in range(1, 15)]
    after = editdoc.clone(before)
    for o in after["tracks"]["overlays"][4:]:                        # the gap before o5 grows by 5 s, everything after rides along
        o["o0"] += 5.0
        o["o1"] += 5.0
    spans = preview.changed_span(before, after)
    assert len(spans) == 1 and spans[0][0] <= 500.0 and spans[0][1] < 700.0


def test_two_changes_far_apart_are_two_spans_and_close_ones_merge():
    before = long_doc()
    after = editdoc.clone(before)
    after["tracks"]["video"][1]["zoom"] = 1.2                        # 20-40 s
    after["tracks"]["video"][70]["zoom"] = 1.2                       # 1400-1420 s
    assert preview.changed_span(before, after) == [(20.0, 40.0), (1400.0, 1420.0)]
    after["tracks"]["video"][2]["zoom"] = 1.2                        # 40-60 s: touches the first
    assert preview.changed_span(before, after) == [(20.0, 60.0), (1400.0, 1420.0)]


@pytest.mark.parametrize("key,value", [("media_mode", {"version": 1, "kind": "audio"}), ("loudness", {"lufs": -14.0})])
def test_media_mode_and_loudness_changes_mark_the_first_minute(key, value):
    before = long_doc()
    after = editdoc.clone(before)
    after[key] = value
    assert preview.changed_span(before, after) == [(0.0, 60.0)]


def test_a_window_wider_than_the_limit_is_refused(synth_episode):
    doc = long_doc(600.0)
    with pytest.raises(ValueError, match="wider than"):
        preview.render_section(synth_episode, doc, (10.0, 150.0))


def test_span_covers_a_moved_segment(synth_episode):
    before = doc_of(synth_episode)
    after = swap_video(before, 1)
    v = after["tracks"]["video"]
    [(lo, hi)] = preview.changed_span(before, after)
    assert lo <= v[1]["o0"] + 1e-6 and hi >= v[2]["o1"] - 1e-6
    assert lo >= v[0]["o1"] - 1e-6 and hi <= v[3]["o0"] + 1e-6      # and nothing wider than the two moved shots


def test_span_of_a_removed_segment_is_the_join(synth_episode):
    before = doc_of(synth_episode)
    after = editdoc.clone(before)
    v = after["tracks"]["video"]
    gone = v.pop(1)
    width = gone["o1"] - gone["o0"]
    for s in v[1:]:
        s["o0"] -= width
        s["o1"] -= width
    after["duration"] -= width
    [(lo, hi)] = preview.changed_span(before, after)
    assert lo == pytest.approx(hi) and lo == pytest.approx(v[1]["o0"])


def test_a_source_change_marks_the_start_of_the_cut(synth_episode):
    before = doc_of(synth_episode)
    after = editdoc.clone(before)
    after["sources"]["A"]["start_offset"] += 0.5
    assert preview.changed_span(before, after) == [(0.0, after["duration"])]      # a 30 s cut is shorter than the first minute


def test_section_is_cached_by_edit_hash(synth_episode, monkeypatch):
    """Reuse is proven by counting renders. The gain is fixed: left to itself the first (approximate) and a later (exact, once the
    background measurement lands) preview legitimately differ in key, which made this test depend on thread timing."""
    calls: list = []
    stub_render(monkeypatch, calls)
    doc = swap_video(doc_of(synth_episode), 1)
    a = preview.render_section(synth_episode, doc, (2.0, 6.0), gain_db=0.0)["path"]
    assert len(calls) == 1
    assert preview.render_section(synth_episode, doc, (2.0, 6.0), gain_db=0.0)["path"] == a
    assert len(calls) == 1                                           # reused, not re-rendered
    assert a.parent == synth_episode.work / "preview"
    assert preview.render_section(synth_episode, doc, (2.0, 6.0), pad_s=1.0, gain_db=0.0)["path"] != a     # a different pad
    assert preview.render_section(synth_episode, swap_video(doc, 2), (2.0, 6.0), gain_db=0.0)["path"] != a  # a different edit
    assert preview.render_section(synth_episode, doc, (2.0, 6.0), gain_db=3.0)["path"] != a               # a different level
    assert len(calls) == 4


@needs_ffmpeg
def test_section_renders_only_the_padded_span_small(synth_episode):
    doc = doc_of(synth_episode)
    info = preview.render_section(synth_episode, doc, (10.0, 12.0), pad_s=3.0)
    assert (info["start"], info["end"]) == (7.0, 15.0)               # where the file sits in the full cut
    assert info["seek"] == pytest.approx(3.0)                        # the change, in the file's own time
    assert info["duration"] == pytest.approx(8.0)
    p = ffutil.probe(info["path"])
    assert p["duration"] == pytest.approx(8.0, abs=0.1)
    assert p["h"] <= 540 and p["h"] == doc["canvas"]["h"]            # the proxy preset never upscales a small canvas


@needs_ffmpeg
def test_section_clamps_to_the_cut(synth_episode):
    doc = doc_of(synth_episode)
    info = preview.render_section(synth_episode, doc, (0.5, 2.0), pad_s=4.0)
    assert info["start"] == 0.0 and info["seek"] == pytest.approx(0.5)


@needs_ffmpeg
def test_section_uses_a_proxy_when_present(synth_episode, monkeypatch):
    doc = doc_of(synth_episode)
    seen = []
    monkeypatch.setattr("ave.proxies.proxy_map", lambda ep, d: ({str(synth_episode.root / "raw" / "camA.mp4"): "/p/a.mov"}, []))

    def spy(ep, d, out, **kw):
        seen.append({k: s["file"] for k, s in d["sources"].items()})
        raise RuntimeError("stop")
    monkeypatch.setattr(preview, "render", spy)
    with pytest.raises(RuntimeError):
        preview.section(synth_episode, doc, (2.0, 4.0))
    assert seen[0]["A"] == "/p/a.mov" and seen[0]["B"] == doc["sources"]["B"]["file"]


@needs_ffmpeg
def test_section_stays_within_time_budget(synth_episode):
    doc = doc_of(synth_episode)
    start = time.monotonic()
    preview.section(synth_episode, doc, (2.0, 6.0))
    assert time.monotonic() - start < 15


def stub_render(monkeypatch, calls=None):
    """Replace the real render with one that writes a placeholder, recording the file it was asked for."""
    def fake(ep, piece, out, **kw):
        if calls is not None:
            calls.append((Path(out), kw))
        time.sleep(0.2)
        Path(out).write_bytes(b"x" * 10)
    monkeypatch.setattr(preview, "render", fake)


def test_proxy_use_is_part_of_the_cache_key(synth_episode, monkeypatch):
    stub_render(monkeypatch)
    doc = doc_of(synth_episode)
    plain = preview.render_section(synth_episode, doc, (2.0, 4.0), gain_db=0.0)["path"]
    monkeypatch.setattr("ave.proxies.proxy_map", lambda ep, d: ({str(synth_episode.root / "raw" / "camA.mp4"): "/p/a.mov"}, []))
    assert preview.render_section(synth_episode, doc, (2.0, 4.0), gain_db=0.0)["path"] != plain


def test_old_previews_are_pruned(synth_episode, monkeypatch):
    stub_render(monkeypatch)
    monkeypatch.setattr(preview, "KEEP_FILES", 3)
    monkeypatch.setattr(preview, "MIN_AGE_S", 0.0)
    doc = doc_of(synth_episode)
    paths = [preview.render_section(synth_episode, doc, (float(i), i + 0.5), pad_s=1.0, gain_db=0.0)["path"] for i in range(6)]
    left = sorted((synth_episode.work / "preview").glob("*.mp4"))
    assert len(left) == 3 and paths[-1] in left


def test_concurrent_renders_of_one_key_render_once(synth_episode, monkeypatch):
    calls: list = []
    stub_render(monkeypatch, calls)
    doc = doc_of(synth_episode)
    got: list = []
    threads = [threading.Thread(target=lambda: got.append(preview.section(synth_episode, doc, (2.0, 4.0)))) for _ in range(4)]
    stub_gain = lambda ep, d: 0.0
    monkeypatch.setattr(preview, "full_cut_gain_db", stub_gain)
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(got) == 4 and len(set(got)) == 1 and got[0].exists()
    assert len(calls) == 1                                             # the others waited, re-checked and reused it
    assert not [p for p in got[0].parent.glob(".*.mp4")]               # no temp files left behind


@needs_ffmpeg
def test_preview_level_is_the_full_cuts_gain_not_its_own(synth_episode):
    doc = doc_of(synth_episode)
    quiet = preview.render_section(synth_episode, doc, (2.0, 4.0), pad_s=2.0, gain_db=-12.0)["path"]
    loud = preview.render_section(synth_episode, doc, (2.0, 4.0), pad_s=2.0, gain_db=-4.0)["path"]
    assert ffutil.measure_loudness(loud)["lufs"] - ffutil.measure_loudness(quiet)["lufs"] == pytest.approx(8.0, abs=1.0)


@needs_ffmpeg
def test_full_cut_gain_is_measured_once_and_cached(synth_episode):
    doc = doc_of(synth_episode)
    g = preview.full_cut_gain_db(synth_episode, doc)
    assert list((synth_episode.work / "preview").glob("gain-*.json"))
    assert preview.full_cut_gain_db(synth_episode, doc) == g


def test_render_changed_starts_from_the_cut_befores_gain(synth_episode, monkeypatch):
    calls: list = []
    stub_render(monkeypatch, calls)
    monkeypatch.setattr(preview, "full_cut_gain_db", lambda ep, d: 3.5)
    before = doc_of(synth_episode)
    after = swap_video(before, 1)
    (synth_episode.work / "preview").mkdir(parents=True)
    (synth_episode.work / "preview" / f"gain-{preview._sha(before)}.json").write_text('{"gain_db": 3.5}')
    r = preview.render_changed(synth_episode, before, after)
    assert r["path"].exists() and r["level_approximate"] is True and calls[0][1]["preset"]["fixed_gain_db"] == 3.5
    assert preview.render_changed(synth_episode, before, editdoc.clone(before)) is None


def test_a_cold_render_does_not_wait_for_the_gain_measurement(synth_episode, monkeypatch):
    calls, measured = [], []
    stub_render(monkeypatch, calls)
    release = threading.Event()
    doc = doc_of(synth_episode)

    def slow_measurer(ep, d):
        measured.append(1)
        release.wait(30)                                               # stands in for the 45 s full-cut measurement
        (ep.work / "preview" / f"gain-{preview._sha(d)}.json").write_text('{"gain_db": 5.0}')
        return 5.0
    monkeypatch.setattr(preview, "full_cut_gain_db", slow_measurer)
    try:
        t0 = time.monotonic()
        cold = preview.render_section(synth_episode, doc, (2.0, 4.0))
        again = preview.render_section(synth_episode, doc, (6.0, 8.0))      # a second cold preview: no second measurement
        assert time.monotonic() - t0 < 5
        assert cold["level_approximate"] is True and cold["path"].exists()
        assert calls[0][1]["preset"]["fixed_gain_db"] == 0.0               # no parent, no cache: unity
        assert again["level_approximate"] is True
        time.sleep(0.2)
        assert measured == [1]                                             # deduplicated per EditDoc
    finally:
        release.set()
    preview.wait_for_measurements(10)
    warm = preview.render_section(synth_episode, doc, (2.0, 4.0))
    assert warm["level_approximate"] is False and warm["path"] != cold["path"]
    assert calls[-1][1]["preset"]["fixed_gain_db"] == 5.0


def test_a_failed_measurement_is_remembered_and_not_retried_at_once(synth_episode, monkeypatch, caplog):
    stub_render(monkeypatch)
    doc = doc_of(synth_episode)
    attempts = []

    def failing(ep, d):
        attempts.append(1)
        raise RuntimeError("compile blew up")
    monkeypatch.setattr(preview, "full_cut_gain_db", failing)
    with caplog.at_level("WARNING", logger="ave.preview"):
        r = preview.render_section(synth_episode, doc, (2.0, 4.0))
        preview.wait_for_measurements(10)
        preview.render_section(synth_episode, doc, (6.0, 8.0))          # within 10 minutes: no retry
        preview.wait_for_measurements(10)
    assert r["level_approximate"] is True and len(attempts) == 1
    assert sum("compile blew up" in m for m in caplog.messages) == 1
    rec = preview._read_failures(synth_episode, doc)
    assert rec["count"] == 1 and rec["at"] > 0


def test_measurement_retries_after_ten_minutes_and_stops_after_three_failures(synth_episode, monkeypatch):
    stub_render(monkeypatch)
    doc = doc_of(synth_episode)
    attempts = []
    monkeypatch.setattr(preview, "full_cut_gain_db", lambda ep, d: attempts.append(1) or (_ for _ in ()).throw(RuntimeError("no")))
    clock = [1_000_000.0]
    monkeypatch.setattr(preview.time, "time", lambda: clock[0])
    for i in range(5):
        preview.render_section(synth_episode, doc, (2.0, 4.0 + i))
        preview.wait_for_measurements(10)
        clock[0] += preview.RETRY_AFTER_S + 1
    assert len(attempts) == 3 and preview._read_failures(synth_episode, doc)["count"] == 3


def test_a_cache_hit_refreshes_the_file_and_young_previews_are_never_pruned(synth_episode, monkeypatch):
    stub_render(monkeypatch)
    monkeypatch.setattr(preview, "KEEP_FILES", 1)
    doc = doc_of(synth_episode)
    first = preview.render_section(synth_episode, doc, (1.0, 1.5), pad_s=1.0, gain_db=0.0)["path"]
    os.utime(first, (1.0, 1.0))                                         # looks ancient
    preview.render_section(synth_episode, doc, (1.0, 1.5), pad_s=1.0, gain_db=0.0)     # a hit
    assert time.time() - first.stat().st_mtime < 5
    others = [preview.render_section(synth_episode, doc, (float(i), i + 0.5), pad_s=1.0, gain_db=0.0)["path"] for i in range(3, 6)]
    assert first.exists() and all(p.exists() for p in others)           # all under 60 s old: KEEP_FILES=1 does not take them


def test_pruning_drops_old_locks_and_stale_dotfiles(synth_episode):
    d = synth_episode.work / "preview"
    d.mkdir(parents=True)
    old = time.time() - 2 * 3600
    stale = [d / ".abc.lock", d / ".gain-abc.json.lock", d / ".gain-9f.wav", d / ".x.1234.mp4"]
    fresh = [d / ".new.lock", d / ".gain-fresh.wav"]
    live = d / "live.mp4"
    live.write_bytes(b"x")
    (d / ".live.lock").write_text("")
    os.utime(d / ".live.lock", (old, old))
    for p in stale + fresh:
        p.write_text("")
    for p in stale:
        os.utime(p, (old, old))
    preview._prune(d, live)
    assert not any(p.exists() for p in stale) and all(p.exists() for p in fresh)
    assert (d / ".live.lock").exists()                                  # its preview still exists
