"""Two real Syncthing homes on loopback: the generated .stignore against real Syncthing, deletes to the trashcan, an
unplugged drive, and the status states. Uses tests/sync/harness.py; skipped when there is no syncthing binary."""

from __future__ import annotations

import shutil
import time

from ave.syncthing import _MISSING as MISSING
from ave.syncthing import ApiError

from . import harness as H

MEMBERS = lambda b_media: [{"person": "maren", "devices": ["dev_A"], "media": "full"},  # noqa: E731
                           {"person": "imani", "devices": ["dev_B"], "media": b_media}]


def _project(base, side, pid, layout, b_media, studio=False):
    root = base / side / pid
    H.write_project_json(root, pid, pid, layout, MEMBERS(b_media), "dev_A")
    if studio:
        # The invitation carries studio.json too: without it the joining side's rules are not the studio rules, and its
        # .stignore would keep studio.json itself (and posts/, campaigns/...) from ever arriving.
        H.write_tree(root, ["studio.json"])
    return root


def _share_both(a, b, roots_a, roots_b):
    for ra, rb in zip(roots_a, roots_b):
        a.manager.share(ra, [b.peer])
        b.manager.share(rb, [a.peer])


def test_two_homes_sync_a_file(sync_pair, tmp_path):
    a, b = sync_pair
    ra = _project(tmp_path, "A", "prj_ONE", "flat", "full")
    rb = _project(tmp_path, "B", "prj_ONE", "flat", "full")
    _share_both(a, b, [ra], [rb])
    (ra / "edits").mkdir()
    (ra / "edits" / "rough.v1.json").write_text('{"cut": 1}\n')
    a.manager.rescan(ra)
    H.wait_until(lambda: (rb / "edits" / "rough.v1.json").is_file(), what="file on B")
    assert (rb / "edits" / "rough.v1.json").read_text() == '{"cut": 1}\n'
    assert a.manager.wait(ra, "up_to_date", 60).state == "up_to_date"
    assert b.manager.wait(rb, "up_to_date", 60).state == "up_to_date"


def test_generated_stignore_matches_real_syncthing(sync_pair, tmp_path):
    """The test that matters most: on a realistic tree, exactly the files zone_of says sync arrive, and nothing else."""
    a, b = sync_pair
    cases = [("prj_FLAT", "flat", "full", False), ("prj_NUMB", "numbered", "proxies", False),
             ("prj_STUD", {"preset": "flat", "raw_dirs": ["inbox"]}, "full", True), ("prj_ODD", H.ODD, "proxies", False)]
    roots_a = [_project(tmp_path, "A", pid, lay, media, studio) for pid, lay, media, studio in cases]
    roots_b = [_project(tmp_path, "B", pid, lay, media, studio) for pid, lay, media, studio in cases]
    for (pid, lay, media, studio), ra in zip(cases, roots_a):
        H.write_tree(ra, H.tree_paths(lay, studio, "dev_A"))
    _share_both(a, b, roots_a, roots_b)
    for (pid, *_), ra, rb in zip(cases, roots_a, roots_b):
        want = H.expected_on_receiver(ra, "dev_A", rb, "dev_B")
        assert len(want) > 10 and len(H.files_in(ra)) - len(want) > 10, pid    # both sides of the line are exercised
        H.wait_until(lambda: want <= H.files_in(rb), timeout=120, what=f"{pid} on B")
        a.manager.wait(ra, "up_to_date", 60)
    time.sleep(2)                                                           # anything extra would have landed by now
    for (pid, *_), ra, rb in zip(cases, roots_a, roots_b):
        want = H.expected_on_receiver(ra, "dev_A", rb, "dev_B")
        got = H.files_in(rb)
        assert got == want, (pid, sorted(got - want), sorted(want - got))
        for rel in want:
            assert (rb / rel).read_bytes() == (ra / rel).read_bytes()
        assert not any("sync-conflict" in p for p in got), pid
        # No private folder arrives as an empty shell either: every folder on B holds something that synced.
        assert all(any(f.startswith(d + "/") for f in got) for d in H.dirs_in(rb)), (pid, sorted(H.dirs_in(rb)))
    # The proxies member gets proxies, never originals.
    numb_b = roots_b[1]
    assert (numb_b / "02_FOOTAGE" / "proxies" / "a.mp4").is_file() and not (numb_b / "02_FOOTAGE" / "a.mov").exists()
    odd_b = roots_b[3]
    assert (odd_b / "01 [PROJECT]" / "ed{its}" / "rough.v1.json").is_file() and (odd_b / "#music" / "bed.wav").is_file()


def test_a_deletion_goes_to_the_trashcan(sync_pair, tmp_path):
    a, b = sync_pair
    ra = _project(tmp_path, "A", "prj_DEL", "flat", "full")
    rb = _project(tmp_path, "B", "prj_DEL", "flat", "full")
    H.write_tree(ra, ["edits/rough.v1.json", "edits/rough.v2.json"])
    _share_both(a, b, [ra], [rb])
    H.wait_until(lambda: (rb / "edits" / "rough.v1.json").is_file(), what="file on B")
    (ra / "edits" / "rough.v1.json").unlink()
    a.manager.rescan(ra)
    H.wait_until(lambda: not (rb / "edits" / "rough.v1.json").exists(), what="delete on B")
    kept = rb / ".stversions" / "edits" / "rough.v1.json"
    assert kept.is_file() and kept.read_bytes() == b"edits/rough.v1.json\n" * 3
    assert (rb / "edits" / "rough.v2.json").is_file()


def test_an_unplugged_drive_deletes_nothing(sync_pair, tmp_path):
    a, b = sync_pair
    ra = _project(tmp_path, "A", "prj_USB", "numbered", "full")
    rb = _project(tmp_path, "B", "prj_USB", "numbered", "full")
    H.write_tree(ra, H.tree_paths("numbered", False, "dev_A"))
    _share_both(a, b, [ra], [rb])
    want = H.expected_on_receiver(ra, "dev_A", rb, "dev_B")
    H.wait_until(lambda: want <= H.files_in(rb), timeout=120, what="first sync")
    a.manager.wait(ra, "up_to_date", 60)
    shutil.move(str(ra), str(ra.with_name("prj_USB.unplugged")))            # the drive goes away
    try:
        a.manager.rescan(ra)
    except ApiError:
        pass
    H.wait_until(lambda: a.manager.api.get("/rest/db/status", folder="prj_USB")["state"] == "error", timeout=60,
                 what="Syncthing to stop the folder")
    err = a.manager.api.get("/rest/db/status", folder="prj_USB")["error"].lower()
    assert any(m in err for m in MISSING), err                              # Syncthing itself says the folder is gone
    assert a.manager.status(ra).state == "drive_missing"
    time.sleep(3)
    assert H.files_in(rb) == want                                           # nothing was deleted on the other computer
    assert b.manager.status(rb).state in ("up_to_date", "syncing")


def test_status_waits_for_an_offline_member(sync_pair, tmp_path):
    a, b = sync_pair
    ra = _project(tmp_path, "A", "prj_WAIT", "flat", "full")
    rb = _project(tmp_path, "B", "prj_WAIT", "flat", "full")
    _share_both(a, b, [ra], [rb])
    a.manager.wait(ra, "up_to_date", 60)
    b.manager.stop()
    s = a.manager.wait(ra, "waiting", 60)
    assert s.waiting_for == ["imani"]


def _synced_pair(a, b, tmp_path, pid):
    ra = _project(tmp_path, "A", pid, "flat", "full")
    rb = _project(tmp_path, "B", pid, "flat", "full")
    H.write_tree(ra, ["edits/rough.v1.json"])
    _share_both(a, b, [ra], [rb])
    H.wait_until(lambda: (rb / "edits" / "rough.v1.json").is_file(), what="file on B")
    a.manager.wait(ra, "up_to_date", 60)
    b.manager.wait(rb, "up_to_date", 60)
    return ra, rb


def test_up_to_date_again_soon_after_a_member_restarts(sync_pair, tmp_path):
    a, b = sync_pair
    ra, rb = _synced_pair(a, b, tmp_path, "prj_RECON")
    b.manager.stop()
    a.manager.wait(ra, "waiting", 60)
    b.manager.start()
    t = time.monotonic()
    assert a.manager.wait(ra, "up_to_date", 20).state == "up_to_date"     # an idle folder sends no index on reconnect
    assert b.manager.wait(rb, "up_to_date", 20).state == "up_to_date"
    assert time.monotonic() - t < 20


def test_up_to_date_again_soon_after_this_app_restarts(sync_pair, tmp_path):
    a, b = sync_pair
    ra, rb = _synced_pair(a, b, tmp_path, "prj_OWN")
    a.manager.stop()
    a.manager.start()
    assert a.manager.wait(ra, "up_to_date", 20).state == "up_to_date"
    assert b.manager.wait(rb, "up_to_date", 20).state == "up_to_date"


def test_a_stored_index_is_not_taken_for_up_to_date_after_reconnecting(sync_pair, tmp_path):
    """B edits while A is away. When A comes back, its stored copy of B's index says valid and 100 % before B's delta
    arrives; A must not say up to date until the change is actually here."""
    a, b = sync_pair
    ra, rb = _synced_pair(a, b, tmp_path, "prj_STALE")
    a.manager.stop()
    (rb / "edits" / "rough.v2.json").write_text("new cut\n")
    b.manager.rescan(rb)
    b.manager.wait(rb, "waiting", 30)
    a.manager.start()
    deadline = time.monotonic() + 60
    while True:
        s = a.manager.status(ra)
        if s.state == "up_to_date":
            assert (ra / "edits" / "rough.v2.json").read_text() == "new cut\n"
            break
        assert time.monotonic() < deadline, s
        time.sleep(0.05)
