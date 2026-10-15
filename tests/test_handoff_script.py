"""scripts/resolve_handoff.py against an IN-MEMORY fake of the Resolve MCP bridge (never the live one).

The fake models only what the script relies on: a timeline per name with numbered tracks, AppendToTimeline with exclusive end frames,
per-track item lists, track enable/name, and injectable bridge failures (a chunk that half-lands, a track that keeps failing).
"""
import importlib.util
import json
import os
import sys
import types
from pathlib import Path

import pytest

from ave import editdoc, handoff
from conftest import needs_ffmpeg

pytestmark = needs_ffmpeg
SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "resolve_handoff.py"
TL0 = 108000          # timelines start at 01:00:00:00 (30 fps)


class FakeResolve:
    def __init__(self):
        self.pool: dict[str, str] = {}          # id -> path
        self.timelines: dict[str, dict] = {}
        self.cur: str | None = None
        self.settings = {"timelineFrameRate": "30"}
        self.appends: list[tuple] = []
        self.fail_track: tuple | None = None    # (media_type, track_index) whose appends always fail
        self.half_land_once: int | None = None  # the Nth append call lands its first item, then errors
        self.calls = 0
        self.raise_on_call: int | None = None   # the Nth append call RAISES (like the bridge's stale_handle) instead of returning an error
        self.corrupt: tuple | None = None       # (media_type, track_index): shorten the last item by a frame on append
        self.refuse_name = False
        self.hide_new_for_probes = 0            # newly imported clips stay invisible to the first N probes (Resolve registers big files late)
        self._fresh: set[str] = set()

    # ---- helpers
    @property
    def tl(self):
        return self.timelines[self.cur]

    def _track(self, kind, idx):
        return self.tl["tracks"].setdefault((kind, idx), [])

    # ---- the bridge tools used by the script
    def project_settings(self, action, params=None):
        p = params or {}
        if action == "get_name":
            return {"name": "fake"}
        if action == "get_setting":
            return {"settings": self.settings[p["name"]]}
        if action == "set_setting":
            self.settings[p["name"]] = p["value"]
            return {"success": True}
        raise AssertionError(action)

    def media_storage(self, action, params=None):
        assert action == "import_to_pool"
        for f in params["items"]:
            cid = f"c{len(self.pool) + 1}"
            self.pool[cid] = f
            if self.hide_new_for_probes:
                self._fresh.add(cid)
        return {"success": True}

    def media_pool(self, action, params=None):
        p = params or {}
        if action == "probe_media_pool":
            hidden = self._fresh if self.hide_new_for_probes > 0 else set()
            if self.hide_new_for_probes > 0:
                self.hide_new_for_probes -= 1
            return {"root": {"clips": [{"file_path": v, "id": k} for k, v in self.pool.items() if k not in hidden]}, "current_folder": {"clips": []}}
        if action == "create_timeline":
            self.timelines[p["name"]] = {"tracks": {}, "n": {"video": 1, "audio": 1}, "enabled": {}, "names": {}}
            self.cur = p["name"]
            return {"success": True}
        if action == "append_to_timeline":
            self.calls += 1
            if self.raise_on_call == self.calls:
                raise RuntimeError("stale_handle: that handle is no longer held by the bridge")
            infos = p.get("clip_infos") or []
            kind_of = {1: "video", 2: "audio"}
            first = infos[0]
            key = (first["media_type"], first["track_index"])
            if self.fail_track == key:
                return {"error": "stale_handle"}
            land = len(infos)
            err = None
            if self.half_land_once == self.calls:
                land, err = 1, {"error": "missing timeline item id"}
            for info in infos[:land]:
                dur = info["end_frame"] - info["start_frame"]
                if self.corrupt == (info["media_type"], info["track_index"]) and info is infos[-1]:
                    dur -= 1
                kind = kind_of[info["media_type"]]
                self._track(kind, info["track_index"]).append(
                    {"name": os.path.basename(self.pool[info["clip_id"]]), "id": f"i{len(self.appends)}", "start": TL0 + info["record_frame"],
                     "duration": dur, "end": TL0 + info["record_frame"] + dur})
                self.appends.append((kind, info["track_index"], info["record_frame"]))
            return err or {"success": True}
        raise AssertionError(action)

    def timeline(self, action, params=None):
        p = params or {}
        if action == "list":
            return {"timelines": [{"name": n} for n in self.timelines]}
        if action == "set_current":
            self.cur = p["name"]
            return {"success": True}
        if action == "get_current":
            return {"id": self.cur}
        if action == "get_start_frame":
            return {"frame": TL0}
        if action == "get_track_count":
            return {"count": self.tl["n"][p["track_type"]]}
        if action == "add_track":
            self.tl["n"][p["track_type"]] += 1
            return {"success": True}
        if action == "get_items":
            return {"items": [dict(i) for i in self._track(p["track_type"], p["track_index"])]}
        if action == "set_track_enable":
            self.tl["enabled"][(p["track_type"], p["index"])] = p["enabled"]
            return {"success": True}
        if action == "get_track_enabled":
            return {"enabled": self.tl["enabled"].get((p["track_type"], p["index"]), True)}
        if action == "set_track_name":
            self.tl["names"][(p["track_type"], p["index"])] = p["name"]
            return {"success": True}
        if action == "get_track_name":
            return {"name": self.tl["names"].get((p["track_type"], p["index"]), "")}
        if action == "detect_gaps_overlaps":
            g = o = 0
            for items in self.tl["tracks"].values():
                s = sorted(items, key=lambda i: i["start"])
                for a, b in zip(s, s[1:]):
                    d = b["start"] - a["end"]
                    g += d > 0
                    o += d < 0
            return {"gap_count": g, "overlap_count": o}
        if action == "get_subtitle":
            return {}
        raise AssertionError(action)


def run_script(monkeypatch, capsys, fake, plan_path, *, chunk=None, dry=False):
    mod = types.ModuleType("server")
    for n in ("project_settings", "media_storage", "media_pool", "timeline"):
        setattr(mod, n, getattr(fake, n))
    monkeypatch.setitem(sys.modules, "server", mod)
    spec = importlib.util.spec_from_file_location("resolve_handoff_under_test", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    if chunk:
        m.APPEND_CHUNK = chunk
    monkeypatch.setattr(sys, "argv", ["resolve_handoff.py", str(plan_path), "--mcp-src", "/nonexistent"] + (["--dry-run"] if dry else []))
    code = 0
    try:
        m.main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    out = capsys.readouterr()
    try:
        data = json.loads(out.out)
    except Exception:
        data = {}
    return code, data, out.err


def _doc_with_cuts(ep):
    doc = editdoc.clone(editdoc.load_latest(ep, "assembly"))
    V = lambda i, s, o0, o1, e0: {"id": i, "src": s, "o0": o0, "o1": o1, "e0": e0, "crop": None, "zoom": 1.0}  # noqa: E731
    doc["tracks"]["video"] = [V("v1", "A", 0.0, 0.9, 0.0), V("v2", "B", 0.9, 3.9, 10.0), V("v3", "A", 3.9, 9.9, 13.0), V("v4", "B", 9.9, 12.0, 19.0)]
    D = lambda i, o0, o1, e0: {"id": i, "src": "A", "o0": o0, "o1": o1, "e0": e0, "xfade_ms": 0, "gain_db": 0.0}  # noqa: E731
    doc["tracks"]["dialogue"] = [D("d1", 0.0, 0.9, 0.0), D("d2", 0.9, 5.0, 10.0), D("d3", 5.0, 12.0, 14.1)]
    doc["duration"] = 12.0
    return doc


@pytest.fixture
def layered_plan(synth_episode, tmp_path):
    doc = _doc_with_cuts(synth_episode)
    master = tmp_path / "master.wav"
    master.write_bytes(b"x")
    p = handoff.plan(synth_episode, doc, name="T assembly v1 layers", audio_master=master, layers=True,
                     sum_wavs={"A": tmp_path / "A.sum.wav"})
    for f in p["import"]:                       # the script refuses missing files: touch what the plan imports
        Path(f).parent.mkdir(parents=True, exist_ok=True)
        Path(f).touch(exist_ok=True)
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(p))
    return p, path


def test_layered_run_places_every_track_disables_alternates_and_verifies(monkeypatch, capsys, layered_plan):
    p, path = layered_plan
    fake = FakeResolve()
    code, data, err = run_script(monkeypatch, capsys, fake, path, chunk=2)
    assert code == 0 and data["ok"] is True, (data, err)
    tr = data["tracks"]
    assert {k: v["items"] for k, v in tr.items()} == {"V1": 4, "V2": 2, "V3": 1, "A1": 3, "A2": 1}
    assert tr["V2"]["state"] == tr["V3"]["state"] == tr["A2"]["state"] == "disabled/muted" and tr["V1"]["state"] == "enabled"
    assert fake.tl["enabled"][("video", 2)] is False and fake.tl["enabled"][("audio", 2)] is False
    assert fake.tl["names"][("video", 2)].endswith("(alt)") and fake.tl["names"][("audio", 2)].startswith("Baked")
    assert tr["V3"]["frames"] == p["layers"]["counts"]["alt_frames"]["3"]
    assert tr["A1"]["gaps"] == 0 and tr["A1"]["overlaps"] == 0 and tr["V1"]["gaps"] == 0
    assert "frame" in data["note"]


def test_failed_track_keeps_partial_work_and_the_rerun_resumes_without_double_placing(monkeypatch, capsys, layered_plan):
    p, path = layered_plan
    fake = FakeResolve()
    fake.fail_track = (1, 3)                      # the bridge keeps refusing V3
    code, data, err = run_script(monkeypatch, capsys, fake, path, chunk=2)
    assert code != 0 and "failed" in data["error"]
    assert "partial_timeline" in err and "items_per_track" in err                  # says what is there and how to continue
    before = len(fake.tl["tracks"][("video", 1)]), len(fake.tl["tracks"][("video", 2)])
    assert before == (4, 2) and not fake.tl["tracks"].get(("video", 3))            # nothing was deleted, nothing half-placed
    fake.fail_track = None
    code, data, err = run_script(monkeypatch, capsys, fake, path, chunk=2)
    assert code == 0 and data["ok"], (data, err)
    assert {k: v["items"] for k, v in data["tracks"].items()} == {"V1": 4, "V2": 2, "V3": 1, "A1": 3, "A2": 1}
    assert len(fake.appends) == len(set(fake.appends)) == 11                       # every item placed exactly once across both runs


def test_half_landed_chunk_is_recovered_inside_one_run(monkeypatch, capsys, layered_plan):
    p, path = layered_plan
    fake = FakeResolve()
    fake.half_land_once = 2                       # the 2nd append call lands its first item and then errors
    code, data, err = run_script(monkeypatch, capsys, fake, path, chunk=2)
    assert code == 0 and data["ok"], (data, err)
    assert len(fake.appends) == len(set(fake.appends)) == 11


def test_resume_refuses_a_track_that_is_not_a_prefix_of_its_plan(monkeypatch, capsys, layered_plan):
    p, path = layered_plan
    fake = FakeResolve()
    fake.fail_track = (1, 3)
    run_script(monkeypatch, capsys, fake, path, chunk=2)
    fake.fail_track = None
    fake.tl["tracks"][("video", 2)][1]["start"] += 5                              # somebody moved a clip on V2
    n = len(fake.appends)
    code, data, err = run_script(monkeypatch, capsys, fake, path, chunk=2)
    assert code != 0 and "not a prefix" in data["error"] and len(fake.appends) == n


def test_readback_mismatch_exits_nonzero_and_keeps_the_timeline(monkeypatch, capsys, layered_plan):
    p, path = layered_plan
    fake = FakeResolve()
    fake.corrupt = (2, 1)                         # the last dialogue clip lands one frame short
    code, data, err = run_script(monkeypatch, capsys, fake, path, chunk=2)
    assert code == 2 and data["ok"] is False
    assert any(x.startswith("A1") for x in data["problems"])
    assert fake.tl["tracks"][("audio", 1)]                                       # kept, not removed


def test_dry_run_reports_items_per_track(monkeypatch, capsys, layered_plan):
    p, path = layered_plan
    code, data, _ = run_script(monkeypatch, capsys, FakeResolve(), path, dry=True)
    assert code == 0 and data["dry_run"] and data["layers"]["items_per_track"] == {"audio1": 3, "audio2": 1, "video1": 4, "video2": 2, "video3": 1}


def test_consolidated_handoff_still_works_through_the_same_script(monkeypatch, capsys, synth_episode, tmp_path):
    doc = _doc_with_cuts(synth_episode)
    master = tmp_path / "master.wav"
    master.write_bytes(b"x")
    p = handoff.plan(synth_episode, doc, name="T assembly v1", audio_master=master)
    for f in p["import"]:
        Path(f).touch(exist_ok=True)
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(p))
    fake = FakeResolve()
    code, data, err = run_script(monkeypatch, capsys, fake, path, chunk=3)
    assert code == 0 and data["ok"] and data["video_items"] == 4 and data["audio_items"] == 1 and "tracks" not in data
    assert fake.tl["n"] == {"video": 1, "audio": 1}                              # no layer tracks leaked into the default handoff


def test_large_imports_that_register_late_are_waited_for_not_failed(monkeypatch, capsys, layered_plan):
    """REAL-FOOTAGE REGRESSION: import_to_pool returned before Resolve registered a 2.4 GB WAV, so the first pool read missed it and the
    whole handoff failed with 'imported file not found in the media pool'. The script must poll."""
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)
    fake = FakeResolve()
    fake.hide_new_for_probes = 3                      # the first three probes after the import do not show the new clips
    _, plan_path = layered_plan
    code, data, err = run_script(monkeypatch, capsys, fake, plan_path)
    assert code == 0 and data.get("ok") is True, (data, err)


def test_a_bridge_exception_mid_run_is_recovered_like_a_returned_error(monkeypatch, capsys, layered_plan):
    """REAL-FOOTAGE REGRESSION: on the 1413-cut session the bridge RAISED stale_handle from an append call, which escaped the
    retry-and-resume logic that only handled returned errors and aborted the run with a partial timeline."""
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)
    fake = FakeResolve()
    fake.raise_on_call = 2
    _, plan_path = layered_plan
    code, data, err = run_script(monkeypatch, capsys, fake, plan_path, chunk=2)
    assert code == 0 and data.get("ok") is True, (data, err[-600:])
