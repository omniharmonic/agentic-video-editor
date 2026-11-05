"""Project identity, per-device append-only logs, the HLC and the disposable index."""

from __future__ import annotations

import errno
import json
import os
import random
import re
import sqlite3
import threading
import time

import pytest

from ave.project import Device, Index, Log, LogError, Project, ProjectError, read_log, read_logs, reduce
from ave.project import hlc
from ave.project.hlc import iso_to_ms

GOOD = json.dumps({"id": "01A", "hlc": "2026-11-02T10:00:00.000Z/0000/abcd", "device": "abcd", "person": "maren",
                   "actor": "human", "op": "note.added", "artifact": None, "data": {"text": "hi"}, "needs": []}) + "\n"


def hlc_at(device, wall):
    return hlc.at(device, wall)


def hlc_after(prev, device, wall):
    return hlc.after(prev, device, wall)


def fixed(iso):
    ms = iso_to_ms(iso)
    return lambda: ms


# ------------------------------------------------------------------------------------------------ HLC

def test_clock_skew_keeps_causal_order():
    a = hlc_at(device="a", wall="2026-11-02T10:00:00Z")
    b = hlc_after(a, device="b", wall="2020-01-01T00:00:00Z")
    assert b > a
    assert hlc.parse(b) > hlc.parse(a)


def test_hlc_format_matches_the_architecture():
    s = hlc_at("abcd", "2026-11-02T21:04:11.120Z")
    assert s == "2026-11-02T21:04:11.120Z/0000/abcd"
    assert hlc.parse(s).ms == iso_to_ms("2026-11-02T21:04:11.120Z")


def test_ticks_on_a_frozen_clock_stay_ordered_past_the_counter_width():
    c = hlc.Clock("abcd", wall=fixed("2026-11-02T10:00:00Z"))
    stamps = [c.tick() for _ in range(10050)]
    assert stamps == sorted(stamps) and len(set(stamps)) == len(stamps)
    assert [hlc.parse(s) for s in stamps] == sorted(hlc.parse(s) for s in stamps)


def test_a_stamp_far_ahead_is_adopted_with_a_loud_warning_naming_the_device():
    c = hlc.Clock("bbbb", wall=fixed("2026-11-02T10:00:00Z"))
    future = hlc_at("aaaa", "2030-01-01T00:00:00Z")
    c.observe(future)
    assert c.tick() > future
    assert c.warnings and "CLOCK WARNING" in c.warnings[0] and "aaaa" in c.warnings[0]


def test_a_stamp_within_a_day_ahead_is_quiet():
    c = hlc.Clock("bbbb", wall=fixed("2026-11-02T10:00:00Z"))
    c.observe(hlc_at("aaaa", "2026-11-03T09:00:00Z"))
    assert not c.warnings


def test_ids_sort_by_creation():
    ids = [hlc.ulid() for _ in range(500)]
    assert ids == sorted(ids) and len(set(ids)) == 500 and all(len(i) == 26 for i in ids)


# ------------------------------------------------------------------------------------------------ device and project

def test_device_id_is_stable_and_lives_under_home(private_home):
    d1, d2 = Device.load(), Device.load()
    assert d1 == d2
    assert (private_home / "Library/Application Support/AVE/device.json").is_file()
    assert re.fullmatch(r"[a-z0-9]{4}", d1.short)


def test_create_and_open(project, tmp_path):
    meta = json.loads(project.project_json.read_text())
    assert meta["v"] == 1 and meta["project_id"].startswith("prj_") and meta["title"] == "The Long Table"
    assert meta["members"] == [{"person": "maren", "devices": [project.device.id], "media": "full"}]
    assert meta["publisher_device"] == project.device.id
    assert str(tmp_path) not in project.project_json.read_text()  # paths differ per computer: never stored
    again = Project.open(project.root)
    assert again.project_id == project.project_id and again.person == "maren"
    with pytest.raises(ProjectError):
        Project.create(project.root, "again")


def test_create_keeps_the_folder_layout(tmp_path):
    root = tmp_path / "drive"
    (root / "02_FOOTAGE").mkdir(parents=True)
    (root / "02_FOOTAGE" / "cam.mov").write_bytes(b"x")
    p = Project.create(root, "T")
    assert p.layout == "numbered"
    assert (root / "02_FOOTAGE" / "cam.mov").read_bytes() == b"x"


def test_open_refuses_a_folder_that_is_not_a_project(tmp_path):
    with pytest.raises(ProjectError):
        Project.open(tmp_path)


# ------------------------------------------------------------------------------------------------ reading

def test_torn_final_line_is_skipped_then_recovers(tmp_path):
    p = tmp_path / "abcd.jsonl"
    p.write_text(GOOD + '{"id":"x","op')
    r = read_log(p)
    assert len(r) == 1 and any("torn" in w for w in r.warnings)
    Log(tmp_path, "abcd", "maren").append("note.added", data={"text": "after the crash"})
    r = read_log(p)
    assert [e["data"]["text"] for e in r] == ["hi", "after the crash"]
    assert not any("torn" in w for w in r.warnings) and any("line 2" in w for w in r.warnings)
    assert p.read_text().startswith(GOOD + '{"id":"x","op\n')  # the fragment is kept, never rewritten


def test_malformed_lines_are_skipped_and_reported_never_raised(tmp_path):
    p = tmp_path / "abcd.jsonl"
    p.write_bytes(GOOD.encode() + b"\xff\xfe garbage\n[1,2]\n{\"id\":\"y\"}\n\n" + GOOD.replace("01A", "01B").encode())
    r = read_log(p)
    assert [e["id"] for e in r] == ["01A", "01B"]
    assert len(r.warnings) == 3


def test_reading_a_missing_log_is_empty(tmp_path):
    assert read_log(tmp_path / "none.jsonl") == [] and read_logs(tmp_path / "nothing") == []


# ------------------------------------------------------------------------------------------------ writing

def test_append_writes_one_complete_line(project):
    e = project.log.append("artifact.created", "art_ep", {"kind": "episode", "title": "Ep 1"}, actor="claude")
    assert set(e) == {"id", "hlc", "device", "person", "actor", "op", "artifact", "data", "needs", "seen"}   # a source run: no "app"
    assert hlc.parse(e["hlc"]).device == project.device.id
    assert e["device"] == project.device.id and e["person"] == "maren" and e["actor"] == "claude"
    lines = project.log.path.read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0]) == e


def test_every_event_carries_what_this_computer_had_seen(project):
    other = Log(project.log_dir, "efgh", "imani")
    theirs = other.append("artifact.created", "art", {"stage": "Draft"})
    first = project.log.append("note.added", data={"text": "x"})
    assert first["seen"] == {"efgh": theirs["hlc"]}
    e = project.log.append("stage.signed_off", "art", {"from": "Draft", "to": "Refine"})
    assert e["seen"] == {"efgh": theirs["hlc"], project.device.id: first["hlc"]}
    assert hlc.parse(e["hlc"]) > hlc.parse(theirs["hlc"])


def test_append_validates(project):
    with pytest.raises(ValueError):
        project.log.append("artifact.deleted", "art")
    with pytest.raises(ValueError):
        project.log.append("note.added", actor="robot")
    with pytest.raises(ValueError):
        project.log.append("version.added", "art", {"version": "v"}, needs=[{"sha256": "x"}])


def test_append_is_durable_and_syncs_a_new_file_s_folder(project, monkeypatch):
    import ave.project.log as logmod
    calls, dirs = [], []
    real_sync, real_dir = logmod._sync, logmod._sync_dir
    monkeypatch.setattr(logmod, "_sync", lambda fd: (calls.append(fd), real_sync(fd)))
    monkeypatch.setattr(logmod, "_sync_dir", lambda p: (dirs.append(p), real_dir(p)))
    project.log.append("note.added", data={"text": "x"})
    project.log.append("note.added", data={"text": "y"})
    assert len(calls) >= 2 and dirs == [project.log_dir]


def test_full_fsync_is_used_on_macos(monkeypatch):
    import ave.project.log as logmod
    if not hasattr(logmod.fcntl, "F_FULLFSYNC"):
        pytest.skip("no F_FULLFSYNC on this platform")
    seen = []
    real = logmod.fcntl.fcntl
    monkeypatch.setattr(logmod.fcntl, "fcntl", lambda fd, cmd, *a: (seen.append(cmd), real(fd, cmd, *a))[1])
    import tempfile
    with tempfile.TemporaryFile() as f:
        logmod._sync(f.fileno())
    assert logmod.fcntl.F_FULLFSYNC in seen


def test_only_this_device_file_is_written(project):
    other = project.log_dir / "efgh.jsonl"
    theirs = GOOD.replace("abcd", "efgh")
    other.write_text(theirs)
    for i in range(5):
        project.log.append("note.added", data={"text": str(i)})
    project.log.append("stage.signed_off", "art", {"to": "Draft"})
    assert other.read_text() == theirs
    assert sorted(p.name for p in project.log_dir.iterdir()) == sorted(["efgh.jsonl", f"{project.device.id}.jsonl"])
    assert all(e["device"] == project.device.id for e in read_log(project.log.path))


def test_two_writers_on_one_computer_stay_ordered(project):
    def write(n):
        log = Log(project.log_dir, project.device.id, "maren")
        for i in range(40):
            log.append("note.added", data={"text": f"{n}-{i}"})
    threads = [threading.Thread(target=write, args=(n,)) for n in range(3)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    events = read_log(project.log.path)
    assert len(events) == 120 and not events.warnings
    stamps = [hlc.parse(e["hlc"]) for e in events]
    assert stamps == sorted(stamps) and len(set(stamps)) == 120


def test_a_computer_whose_clock_is_far_behind_still_writes_after_what_it_saw(tmp_path):
    a = Log(tmp_path, "aaaa", "maren", wall=fixed("2026-11-02T10:00:00Z")).append("artifact.created", "art")
    b = Log(tmp_path, "bbbb", "imani", wall=fixed("2020-01-01T00:00:00Z")).append("artifact.updated", "art", {"title": "x"})
    assert hlc.parse(b["hlc"]) > hlc.parse(a["hlc"])


def test_a_reset_clock_orders_its_next_edit_after_everything_it_read(tmp_path):
    b_log = Log(tmp_path, "bbbb", "imani", wall=fixed("2020-01-01T00:00:00Z"))
    b_log.append("artifact.updated", "art", {"title": "old 1"})
    b_log.append("artifact.updated", "art", {"title": "old 2"})
    a = Log(tmp_path, "aaaa", "maren", wall=fixed("2026-11-02T10:00:00Z")).append("artifact.updated", "art", {"title": "A"})
    b = b_log.append("artifact.updated", "art", {"title": "B after reading A"})
    assert hlc.parse(b["hlc"]) > hlc.parse(a["hlc"])
    assert b["seen"]["aaaa"] == a["hlc"]
    events = read_logs(tmp_path)
    for seed in range(5):
        random.Random(seed).shuffle(events)
        assert reduce(events).artifacts["art"].title == "B after reading A"
    assert any("aaaa" in w for w in b_log.warnings)


def test_clock_warnings_reach_the_state(tmp_path):
    p = Project.create(tmp_path / "p", "T", wall=fixed("2026-11-02T10:00:00Z"))
    Log(p.log_dir, "cccc", "c", wall=fixed("2026-11-02T09:00:00Z")).append("note.added")
    Log(p.log_dir, "aaaa", "a", wall=fixed("2030-01-01T00:00:00Z")).append("note.added")
    p.log.append("note.added")
    assert any("aaaa" in w for w in p.state().warnings)


# ------------------------------------------------------------------------------------------------ index

def test_index_can_be_deleted_and_rebuilt(project):
    project.log.append("artifact.created", "art_ep", {"kind": "episode", "title": "Ep 1", "stage": "Draft"})
    idx = project.index()
    idx.refresh()
    first = idx.artifacts()
    assert [(a["id"], a["stage"], a["title"]) for a in first] == [("art_ep", "Draft", "Ep 1")]
    idx.path.unlink()
    assert Index(project).artifacts() == first
    assert idx.path.is_file()


def test_index_follows_new_events_without_an_explicit_refresh(project):
    project.log.append("artifact.created", "a1", {"title": "one"})
    idx = project.index()
    assert len(idx.artifacts()) == 1
    project.log.append("artifact.created", "a2", {"title": "two"})
    assert [a["id"] for a in idx.artifacts()] == ["a1", "a2"]


def test_a_damaged_index_is_rebuilt(project):
    project.log.append("artifact.created", "a1", {"title": "one"})
    idx = project.index()
    idx.path.parent.mkdir(parents=True, exist_ok=True)
    idx.path.write_bytes(b"not a database at all")
    assert [a["id"] for a in idx.artifacts()] == ["a1"]
    sqlite3.connect(idx.path).execute("SELECT 1 FROM artifacts").fetchall()


def test_index_lists_conflicts_and_warnings(project):
    Log(project.log_dir, "efgh", "imani").append("stage.signed_off", "art", {"from": "Draft", "to": "Plan"}, seen={})
    project.log.append("stage.signed_off", "art", {"from": "Draft", "to": "Refine"}, seen={})
    (project.log_dir / "zzzz.jsonl").write_text("{torn")
    idx = project.index()
    assert len(idx.conflicts()) == 1 and idx.artifacts()[0]["stage"] == "Draft"
    assert any("torn" in w for w in idx.warnings())


# ------------------------------------------------------------------------------------------------ identity (R2)

def test_log_files_are_named_by_the_full_device_id(project):
    project.log.append("note.added")
    assert project.log.path.name == project.device.id + ".jsonl" and project.device.id.startswith("dev_")
    assert len(project.device.short) == 4


def test_two_ids_that_would_share_one_file_are_refused(tmp_path):
    (tmp_path / "DEV_X.jsonl").write_text("")
    with pytest.raises(LogError):
        Log(tmp_path, "dev_x", "maren").append("note.added")


def test_a_project_whose_members_would_share_a_log_file_is_refused(project):
    meta = json.loads(project.project_json.read_text())
    meta["members"].append({"person": "imani", "devices": ["dev_ABC"], "media": "proxies"})
    meta["members"].append({"person": "sam", "devices": ["DEV_abc"], "media": "proxies"})
    project.project_json.write_text(json.dumps(meta))
    with pytest.raises(ProjectError):
        Project.open(project.root)


def test_a_device_id_that_cannot_name_a_file_is_refused(tmp_path):
    with pytest.raises(LogError):
        Log(tmp_path, "../escape", "maren")


# ------------------------------------------------------------------------------------------------ durability and locking

def test_a_volume_without_flock_falls_back_to_a_lock_file(project, monkeypatch):
    import ave.project.log as logmod

    def no_flock(fd, op):
        raise OSError(errno.ENOTSUP, "Operation not supported")
    monkeypatch.setattr(logmod.fcntl, "flock", no_flock)

    def write(n):
        log = Log(project.log_dir, project.device.id, "maren", lock_dir=project.local_dir / "locks")
        for i in range(25):
            log.append("note.added", data={"text": f"{n}-{i}"})
    threads = [threading.Thread(target=write, args=(n,)) for n in range(3)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    events = read_log(project.log.path)
    assert len(events) == 75 and not events.warnings
    stamps = [hlc.parse(e["hlc"]) for e in events]
    assert stamps == sorted(stamps) and len(set(stamps)) == 75
    assert not list((project.local_dir / "locks").iterdir())  # released


def test_a_stale_lock_file_is_recovered(project, monkeypatch):
    import ave.project.log as logmod
    monkeypatch.setattr(logmod.fcntl, "flock", lambda fd, op: (_ for _ in ()).throw(OSError(errno.EINVAL, "no")))
    lock = project.local_dir / "locks" / (project.log.filename + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("crashed 123\n")
    old = time.time() - 3600
    os.utime(lock, (old, old))
    project.log.append("note.added")
    assert len(read_log(project.log.path)) == 1 and not lock.exists()


def test_other_flock_errors_are_not_swallowed(project, monkeypatch):
    import ave.project.log as logmod
    monkeypatch.setattr(logmod.fcntl, "flock", lambda fd, op: (_ for _ in ()).throw(OSError(errno.EIO, "io")))
    with pytest.raises(OSError):
        project.log.append("note.added")


# ------------------------------------------------------------------------------------------------ minor rulings

def test_a_restart_never_stamps_below_its_own_torn_last_line(tmp_path):
    p = tmp_path / "bbbb.jsonl"
    p.write_text('{"id":"T","hlc":"2026-11-02T10:00:00.000Z/0005/bbbb","device":"bbbb","op":"no')
    e = Log(tmp_path, "bbbb", "b", wall=fixed("2026-11-01T00:00:00Z")).append("note.added")
    assert hlc.parse(e["hlc"]) > hlc.parse("2026-11-02T10:00:00.000Z/0005/bbbb")


def test_caller_seen_cannot_be_ahead_of_what_was_read(tmp_path):
    a = Log(tmp_path, "aaaa", "a", wall=fixed("2026-11-02T10:00:00Z")).append("note.added")
    b = Log(tmp_path, "bbbb", "b", wall=fixed("2026-11-02T10:00:00Z"))
    assert b.append("stage.signed_off", "art", {"to": "Draft"}, seen={"aaaa": a["hlc"]})["seen"] == {"aaaa": a["hlc"]}
    with pytest.raises(ValueError):
        b.append("stage.signed_off", "art", {"to": "Draft"}, seen={"aaaa": hlc.after(a["hlc"], "aaaa", 0)})
    with pytest.raises(ValueError):
        b.append("stage.signed_off", "art", {"to": "Draft"}, seen={"zzzz": a["hlc"]})


def test_append_reads_only_what_is_new(project, monkeypatch):
    import ave.project.log as logmod
    other = Log(project.log_dir, "efgh", "imani")
    for i in range(30):
        other.append("note.added", data={"text": str(i)})
    project.log.append("note.added")
    parsed = []
    real = logmod._parse_lines
    monkeypatch.setattr(logmod, "_parse_lines", lambda lines, *a: (parsed.append(len(lines)), real(lines, *a))[1])
    project.log.append("note.added")
    assert sum(parsed) == 1  # only our own previous line, not the 30 already read
    theirs = other.append("note.added")
    assert project.log.append("note.added")["seen"]["efgh"] == theirs["hlc"]


def test_a_log_replaced_by_sync_is_read_again(project):
    other = project.log_dir / "efgh.jsonl"
    other.write_text(GOOD.replace("abcd", "efgh"))
    project.log.append("note.added")
    tmp = project.log_dir / ".efgh.tmp"
    later = GOOD.replace("abcd", "efgh").replace("10:00:00.000Z", "11:00:00.000Z").replace("01A", "01C")
    tmp.write_text(GOOD.replace("abcd", "efgh") + later)
    os.replace(tmp, other)
    e = project.log.append("note.added")
    assert e["seen"]["efgh"] == "2026-11-02T11:00:00.000Z/0000/efgh"


def test_index_refreshes_from_threads_without_sharing_a_temp_file(project):
    project.log.append("artifact.created", "a1", {"title": "one"})
    errors = []

    def go():
        try:
            Index(project).refresh()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
    threads = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and [a["id"] for a in Index(project).artifacts()] == ["a1"]
    assert not [p for p in project.local_dir.iterdir() if p.name.endswith(".tmp")]


# ------------------------------------------------------------------------------------------------ fix round 2

def test_a_crashed_lock_is_taken_over_by_exactly_one_waiter(tmp_path):
    from ave.project.log import lock_file
    trials_with_overlap = 0
    for trial in range(200):
        lp = tmp_path / f"t{trial}" / "x.lock"
        lp.parent.mkdir()
        lp.write_text("dead 1\n")
        os.utime(lp, (time.time() - 100, time.time() - 100))
        holders, overlap = [], []
        barrier = threading.Barrier(6)

        def worker():
            barrier.wait()
            with lock_file(lp, stale_s=30, timeout_s=5):
                holders.append(1)
                if len(holders) > 1:
                    overlap.append(1)
                time.sleep(0.002)
                holders.pop()
        ts = [threading.Thread(target=worker) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        trials_with_overlap += bool(overlap)
        assert not lp.exists() and sorted(p.name for p in lp.parent.iterdir()) == []
    assert trials_with_overlap == 0


def test_a_holder_that_was_taken_over_leaves_its_successor_s_lock(tmp_path):
    from ave.project.log import lock_file
    lp = tmp_path / "x.lock"
    with lock_file(lp, stale_s=30):
        successor = json.dumps({"token": "next", "t": time.time()}).encode()
        lp.write_bytes(successor)  # as if a waiter had taken the lock over while we stalled
    assert lp.read_bytes() == successor


def test_staleness_comes_from_the_lock_s_own_time_not_mtime(tmp_path):
    from ave.project.log import lock_file
    fresh = tmp_path / "fresh.lock"
    fresh.write_text(json.dumps({"token": "t", "t": time.time()}))
    os.utime(fresh, (time.time() - 3600, time.time() - 3600))
    with pytest.raises(TimeoutError):
        with lock_file(fresh, stale_s=30, timeout_s=0.05):
            pass
    old = tmp_path / "old.lock"
    old.write_text(json.dumps({"token": "t", "t": time.time() - 3600}))
    with lock_file(old, stale_s=30, timeout_s=1):
        assert json.loads(old.read_text())["token"] != "t"


def test_stray_files_in_the_log_folder_do_not_stop_a_project_from_opening(project):
    other = "dev_01IMANI"
    (project.log_dir / f"._{other}.jsonl").write_bytes(b"\x00\x05\x16\x07 AppleDouble")
    (project.log_dir / f"{other}.sync-conflict-20261102-101500-ABCDEFG.jsonl").write_text(GOOD.replace("abcd", other))
    p = Project.open(project.root)
    assert any("sync-conflict" in w for w in p.state().warnings)
    assert not any("._" in w for w in p.state().warnings)
    p.log.append("note.added")
    assert len(p.state().notes) == 2  # the conflict copy is still read


def test_a_stamp_beyond_year_9000_is_never_adopted(tmp_path):
    c = hlc.Clock("bbbb", wall=fixed("2026-11-02T10:00:00Z"))
    c.observe("9500-01-01T00:00:00.000Z/0000/aaaa")
    assert c.tick().startswith("2026-11-02") and c.warnings
    p = tmp_path / "bbbb.jsonl"
    p.write_text('{"id":"T","hlc":"9999-12-31T23:59:59.999Z/9999/bbbb","device":"bbbb","op":"no')
    e = Log(tmp_path, "bbbb", "b", wall=fixed("2026-11-02T10:00:00Z")).append("note.added")
    assert e["hlc"].startswith("2026-11-02")
