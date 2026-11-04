"""How Syncthing's numbers become a status, including the early window right after a member connects, and the plain
answers for a project that is not shared, has no project.json, or is a copy of a shared one."""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

import pytest

from ave.syncthing import SyncError, decide

from . import harness as H

IDLE = {"state": "idle", "needBytes": 0, "needTotalItems": 0}
DONE = {"remoteState": "valid", "completion": 100, "needBytes": 0, "needItems": 0, "needDeletes": 0}


def test_up_to_date_needs_a_valid_complete_member_that_indexed_since_connecting():
    assert decide(IDLE, ["J"], {"J"}, {"J": DONE}, {"J"}).state == "up_to_date"


def test_the_early_window_after_connecting_is_syncing_not_up_to_date():
    # Connected, completion already says 100, but no index has arrived since the connection: we do not know yet.
    assert decide(IDLE, ["J"], {"J"}, {"J": DONE}, set()).state == "syncing"
    assert decide(IDLE, ["J"], {"J"}, {"J": {**DONE, "remoteState": "unknown"}}, {"J"}).state == "syncing"


def test_a_member_not_sharing_yet_or_offline_is_waited_for():
    s = decide(IDLE, ["J"], {"J"}, {"J": {**DONE, "remoteState": "notSharing"}}, {"J"}, lambda d: "imani")
    assert s.state == "waiting" and s.waiting_for == ["imani"]
    s = decide(IDLE, ["J", "K"], {"J"}, {"J": DONE}, {"J"}, {"J": "imani", "K": "kai"}.get)
    assert s.state == "waiting" and s.waiting_for == ["kai"]


def test_work_left_on_either_side_is_syncing():
    assert decide({**IDLE, "state": "syncing"}, ["J"], {"J"}, {"J": DONE}, {"J"}).state == "syncing"
    assert decide(IDLE, ["J"], {"J"}, {"J": {**DONE, "completion": 90, "needBytes": 5}}, {"J"}).state == "syncing"
    assert decide({**IDLE, "needTotalItems": 3}, ["J"], {"J"}, {"J": DONE}, {"J"}).state == "syncing"


def test_a_solo_project_is_up_to_date():
    assert decide(IDLE, [], set(), {}, set()).state == "up_to_date"


def _proj(tmp_path, name, pid="prj_SOLO"):
    root = tmp_path / name
    H.write_project_json(root, pid, name, "flat", [{"person": "maren", "devices": ["dev_SOLO"], "media": "full"}], "dev_SOLO")
    return root


def test_plain_answers_for_unshared_unreadable_and_copied_projects(sync_home, tmp_path):
    m = sync_home.manager
    root = _proj(tmp_path, "orig")
    assert m.status(root).state == "not_shared"
    m.share(root, [])
    assert m.wait(root, "up_to_date", 30).state == "up_to_date"
    copy = tmp_path / "copy"
    shutil.copytree(root, copy)
    with pytest.raises(SyncError, match="already synced from"):
        m.share(copy, [])
    s = m.status(copy)
    assert s.state == "error" and "looks like a copy" in s.error
    bare = tmp_path / "bare"
    bare.mkdir()
    s = m.status(bare)
    assert s.state == "error" and "project.json" in s.error
    assert m.status(tmp_path / "never").state == "error"


# ------------------------------------------------------------------------------------------------ Task 17: words and drives
from ave.syncthing import MemberStatus, SyncStatus  # noqa: E402
from ave.syncthing import words as W  # noqa: E402

SID_J = "JJJJJJJ-JJJJJJJ-JJJJJJJ-JJJJJJJ-JJJJJJJ-JJJJJJJ-JJJJJJJ-JJJJJJJ"


def test_every_state_has_plain_words_with_names_never_device_ids():
    names = {SID_J: "imani", "K": "kai"}.get
    off = W.say(decide(IDLE, [SID_J], set(), {}, set(), names))
    assert off["text"] == "Waiting for Imani (offline)" and off["members"][0]["text"] == "Waiting for Imani (offline)"
    sending = W.say(decide(IDLE, [SID_J], {SID_J}, {SID_J: {**DONE, "completion": 40, "needItems": 3, "needBytes": 3_200_000}},
                           {SID_J}, names))
    assert sending["text"] == "Sending 3 files to Imani" and sending["detail"] == "3.2 MB to go." and sending["tone"] == "busy"
    one = W.say(decide(IDLE, [SID_J], {SID_J}, {SID_J: {**DONE, "completion": 90, "needItems": 1}}, {SID_J}, names))
    assert one["text"] == "Sending 1 file to Imani"
    recv = W.say(decide({**IDLE, "needTotalItems": 2, "needBytes": 5}, [SID_J], {SID_J}, {SID_J: DONE}, {SID_J}, names))
    assert recv["text"] == "Receiving 2 files from Imani"
    ok = W.say(decide(IDLE, [SID_J], {SID_J}, {SID_J: DONE}, {SID_J}, names))
    assert ok["text"] == "Up to date" and ok["tone"] == "ok" and ok["members"] == [
        {"name": "Imani", "state": "up_to_date", "text": "Imani is up to date"}]
    two = W.say(decide(IDLE, [SID_J, "K"], set(), {}, set(), names))
    assert two["text"] == "Waiting for Imani and Kai (offline)"
    joining = W.say(decide(IDLE, [SID_J], {SID_J}, {SID_J: {**DONE, "remoteState": "notSharing"}}, {SID_J}, names))
    assert joining["text"] == "Waiting for Imani to join"
    low = W.say(SyncStatus("low_disk", free_bytes=2_400_000_000, min_free_bytes=5_000_000_000))
    assert low["text"] == "Paused: the drive is low on space"
    assert low["detail"] == "2.4 GB free, and sync needs at least 5.0 GB. Nothing new arrives until there is room."
    assert low["next"] and low["tone"] == "warn"
    gone = W.say(SyncStatus("drive_missing", error="folder path missing"))
    assert gone["text"] == "Drive not connected" and gone["next"] == W.DRIVE_NEXT
    ts = W.say(SyncStatus("tailscale_off", error="Tailscale is not available (no socket)"))
    assert ts["text"] == "Tailscale is off" and ts["next"] == W.TAILSCALE_NEXT
    denied = W.say(SyncStatus("error", error="scanning: open /Volumes/X/proj: permission denied"))
    assert denied["text"] == "Sync stopped: this computer cannot write to the project folder" and "Get Info" in denied["next"]
    both = W.say(decide({**IDLE, "needTotalItems": 2}, [SID_J], {SID_J}, {SID_J: {**DONE, "completion": 50, "needItems": 4}},
                        {SID_J}, names))
    # Work both ways: the headline is what is arriving here; what goes out is on the member's own line.
    assert both["text"] == "Receiving 2 files from Imani" and both["members"][0]["text"] == "Sending 4 files to Imani"
    mac = W.say(SyncStatus("low_disk", error="insufficient space on disk for database (/x): current 1 GB", free_bytes=10**9))
    assert mac["text"] == "Paused: this Mac is low on space" and "this Mac's disk" in mac["next"]
    for w in (off, sending, recv, ok, two, joining, low, gone, ts, denied, both, mac):
        assert SID_J not in str(w) and "syncthing" not in str(w).lower(), w


def test_tailscale_off_is_said_for_shared_projects_only(tmp_path):
    from ave.syncthing import SyncManager
    m = SyncManager(tmp_path / "st", ave_device="dev_A", listen_provider=lambda: (_ for _ in ()).throw(SyncError("no tailscale")),
                    tailscale_poll_s=60)
    m.start()
    try:
        assert m.state == "waiting_for_tailscale"
        shared = tmp_path / "shared"
        H.write_project_json(shared, "prj_SH", "Shared", "flat", [{"person": "maren", "devices": ["dev_A"]},
                                                                {"person": "imani", "devices": ["dev_B"]}], "dev_A")
        solo = tmp_path / "solo"
        H.write_project_json(solo, "prj_SO", "Solo", "flat", [{"person": "maren", "devices": ["dev_A"]}], "dev_A")
        assert m.status(shared).state == "tailscale_off"
        assert W.say(m.status(shared))["text"] == "Tailscale is off"
        assert m.status(solo).state == "not_shared"
    finally:
        m.stop()


def _side(base, side, pid, paths=(), layout="numbered"):
    root = base / side / pid
    H.write_project_json(root, pid, pid, layout, [{"person": "maren", "devices": ["dev_A"], "media": "full"},
                                                     {"person": "imani", "devices": ["dev_B"], "media": "full"}], "dev_A")
    H.write_tree(root, list(paths))
    return root


def _wait_state(m, root, state, timeout=60):
    return H.wait_until(lambda: (s := m.status(root)).state == state and s, timeout=timeout, what=f"status {state}")


def test_unplugged_drive_reports_missing_and_deletes_nothing(sync_pair, tmp_path):
    a, b = sync_pair
    pid = "prj_DRIVE"
    ra = _side(tmp_path, "A", pid, H.tree_paths("numbered", False, "dev_A"))
    rb = _side(tmp_path, "B", pid)
    a.manager.share(ra, [b.peer])
    b.manager.share(rb, [a.peer])
    want = H.expected_on_receiver(ra, "dev_A", rb, "dev_B")
    H.wait_until(lambda: want <= H.files_in(rb), timeout=120, what="first sync")
    a.manager.wait(ra, "up_to_date", 60)
    b.manager.wait(rb, "up_to_date", 60)
    on_a = H.files_in(ra)
    away = ra.with_name(pid + ".unplugged")

    # 1. The drive is unmounted but its mount point stays behind as an empty folder: the worst case for "all deleted".
    ra.rename(away)
    ra.mkdir()
    try:
        a.manager.rescan(ra)
    except (SyncError, Exception):  # noqa: BLE001 - Syncthing may refuse the scan outright, which is the point
        pass
    s = _wait_state(a.manager, ra, "drive_missing")
    w = W.say(s)
    assert w["text"] == "Drive not connected" and w["next"] == W.DRIVE_NEXT
    err = a.manager.api.get("/rest/db/status", folder=pid)
    assert err["state"] == "error" and "marker missing" in err["error"].lower(), err   # Syncthing refuses: no scan as empty
    # Nothing the app does can turn the empty mount point into the project: no marker is ever written into it.
    with pytest.raises(Exception, match="project.json"):
        a.manager.share(ra, [b.peer])
    with pytest.raises(Exception, match="project.json"):
        a.manager.refresh(ra)
    assert not (ra / ".stfolder").exists() and not any(ra.iterdir())

    # 2. The mount point goes too: the folder path is simply not there.
    ra.rmdir()
    try:
        a.manager.rescan(ra)
    except Exception:  # noqa: BLE001
        pass
    H.wait_until(lambda: "path missing" in a.manager.api.get("/rest/db/status", folder=pid)["error"].lower(), timeout=60,
                 what="Syncthing to see the path is gone")
    assert W.say(a.manager.status(ra))["text"] == "Drive not connected"

    time.sleep(4)                                                          # any delete would have reached B by now
    assert H.files_in(rb) == want, sorted(want - H.files_in(rb))           # the other computer keeps every file
    assert not (rb / ".stversions").exists() or not any(p.is_file() for p in (rb / ".stversions").rglob("*"))
    st_b = b.manager.api.get("/rest/db/status", folder=pid)
    assert int(st_b.get("needDeletes") or 0) == 0 and st_b["globalFiles"] >= len(want)
    assert b.manager.status(rb).state in ("up_to_date", "syncing", "waiting")

    # 3. Plugged back in: everything is still on both sides and sync carries on.
    away.rename(ra)
    a.manager.rescan(ra)
    assert a.manager.wait(ra, "up_to_date", 60).state == "up_to_date"
    assert H.files_in(ra) == on_a and H.files_in(rb) == want
    new = next(p for p in want if p.endswith("rough.v1.json")).replace("rough.v1", "rough.v2")
    (ra / new).write_text("back\n")
    a.manager.rescan(ra)
    H.wait_until(lambda: (rb / new).is_file(), what="a new file after reconnecting")


def test_low_disk_pauses_with_a_clear_reason(sync_pair, tmp_path):
    import shutil as sh
    a, b = sync_pair
    pid = "prj_FULL"
    ra = _side(tmp_path, "A", pid, ["01_PROJECT/edits/rough.v1.json"])     # numbered: where cut versions really live
    rb = _side(tmp_path, "B", pid)
    free_gb = sh.disk_usage(rb).free / 1e9
    b.manager.min_disk_free = {"value": round(free_gb * 2 + 100), "unit": "GB"}   # past this disk: no need to fill it
    assert b.manager.share(rb, [a.peer])["minDiskFree"] == b.manager.min_disk_free
    assert a.manager.share(ra, [b.peer])["minDiskFree"] == {"value": 5, "unit": "GB"}  # the real floor
    (ra / "01_PROJECT" / "edits" / "rough.v2.json").write_bytes(b"x" * 200_000)
    a.manager.rescan(ra)
    s = _wait_state(b.manager, rb, "low_disk", timeout=90)
    w = W.say(s)
    assert w["text"] == "Paused: the drive is low on space"
    assert s.free_bytes and w["detail"].startswith(f"{W.size(s.free_bytes)} free"), w
    assert "Free up space" in w["next"]
    assert not (rb / "01_PROJECT" / "edits" / "rough.v2.json").exists()
    # The computer with the file says plainly what it is waiting to send.
    sending = H.wait_until(lambda: (x := W.say(a.manager.status(ra)))["text"].startswith("Sending") and x, what="sending words")
    n = next(m for m in a.manager.status(ra).members if m.name == "imani").need_items
    assert n >= 1 and re.fullmatch(r"Sending \d+ files? to Imani", sending["text"]), sending
    # Room again (the floor back to normal): sync carries on by itself.
    b.manager.min_disk_free = {"value": 1, "unit": "%"}
    b.manager.share(rb, [a.peer])
    H.wait_until(lambda: (rb / "01_PROJECT" / "edits" / "rough.v2.json").is_file(), timeout=90, what="the file once there is room")
    assert b.manager.wait(rb, "up_to_date", 60).state == "up_to_date"


# ------------------------------------------------------------------------------------------------ the Workspace pill
class FakeSync:
    """Stands in for this computer's SyncManager: the Workspace only asks it for status and folders."""
    state = "running"

    def __init__(self):
        self.now: dict[str, SyncStatus] = {}
        self.extra: list[dict] = []

    def status(self, root):
        return self.now.get(Path(root).name, SyncStatus("not_shared"))

    def folders(self):
        return self.extra


@pytest.fixture(scope="module")
def browser():
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"Chromium unavailable: {e}")
        yield b
        b.close()


@pytest.fixture
def ws(tmp_path, monkeypatch):
    import threading
    from types import SimpleNamespace

    from ave.project import Project
    from ave.studio.project import StudioProject
    from ave.studio.service import open_studio
    from ave.studio.ui import workspace as ws_mod
    from ave.studio.ui.server import StudioServer
    monkeypatch.setattr(ws_mod, "WATCH_S", 0.3)
    monkeypatch.setattr(ws_mod, "SYNC_S", 0.3)
    sp = StudioProject(tmp_path / "studio")
    sp.create(title="Grounded conversations", tz="UTC")
    Project.create(sp.root, "Grounded conversations", person="Maren")
    p = Project.create(sp.root / "commons", "The Long Table", person="Maren")
    p.log.append("artifact.created", artifact="art_ep", data={"kind": "episode", "title": "Episode 12", "stage": "Draft"}, actor="human")
    fake = FakeSync()
    fake.now["commons"] = SyncStatus("waiting", waiting_for=["imani"], members=[MemberStatus("imani", "offline")])
    srv = StudioServer(open_studio(sp.root), pin="424242")
    srv.workspace.sync = lambda: fake
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield SimpleNamespace(srv=srv, fake=fake, pid=p.project_id, root=sp.root)
    srv.shutdown()
    t.join(5)
    srv.server_close()


@pytest.fixture
def page(browser, ws):
    pg = browser.new_page(viewport={"width": 1440, "height": 900})
    pg.add_init_script("window.__AVE_TEST__ = true;")
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(ws.srv.app_url() + "#/workspace")
    pg.wait_for_selector(".ws-row[data-key]")
    pg.wait_for_function("() => window.__ws && window.__ws.live === 'live'")
    pg.wait_for_selector(".sync-pill:not([hidden])")
    yield pg
    pg.close()
    assert not errors


def test_status_words_are_plain(page, ws):
    assert page.get_by_text("Waiting for Imani").is_visible()
    pill = page.locator(f'.grp .sync-pill[data-sync="{ws.pid}"]')
    assert pill.inner_text().strip() == "Waiting for Imani (offline)"
    assert pill.get_attribute("data-tone") == "wait"
    pill.click()
    pop = page.get_by_role("dialog", name="Sync for The Long Table")
    assert pop.get_by_text("Changes go across when you are both online.").is_visible()
    assert not pop.locator(".sync-members").count()          # one member saying what the headline says: not repeated
    assert pill.get_attribute("data-tip") == "Waiting for Imani (offline)"
    page.keyboard.press("Escape")
    # Live, with no reload: Imani comes online and is sent three files, then the drive goes away.
    ws.fake.now["commons"] = SyncStatus("syncing", members=[MemberStatus("imani", "sending", 3, 4_100_000)])
    page.wait_for_function(f"() => document.querySelector('.grp .sync-pill[data-sync=\"{ws.pid}\"]')?.textContent === 'Sending 3 files to Imani'")
    assert pill.get_attribute("data-tone") == "busy"
    ws.fake.now["commons"] = SyncStatus("drive_missing", error="folder path missing")
    page.wait_for_function(f"() => document.querySelector('.grp .sync-pill[data-sync=\"{ws.pid}\"]')?.textContent === 'Drive not connected'")
    pill.click()
    assert page.get_by_role("dialog", name="Sync for The Long Table").get_by_text(W.DRIVE_NEXT).is_visible()
    page.keyboard.press("Escape")
    ws.fake.now["commons"] = SyncStatus("low_disk", free_bytes=2_400_000_000, min_free_bytes=5_000_000_000)
    page.wait_for_function(f"() => document.querySelector('.grp .sync-pill[data-sync=\"{ws.pid}\"]')?.textContent === 'Paused: the drive is low on space'")
    # A shared project whose drive is unplugged has no folder to read, so it shows as its own group saying so.
    ws.fake.extra = [{"id": "prj_OLD", "label": "Old interviews", "path": str(ws.root / "gone")}]
    gone = page.locator('.grp-missing[data-missing="prj_OLD"]')
    gone.wait_for()
    assert gone.locator(".sync-pill").inner_text().strip() == "Drive not connected"
    assert gone.get_by_text(W.DRIVE_NEXT).is_visible()
    # The one project open: the pill sits in the board header instead of the group header.
    page.evaluate(f"location.hash = '#/workspace/p/{ws.pid}'")
    head = page.locator(f'.ws-head .sync-pill[data-sync="{ws.pid}"]')
    head.wait_for()
    assert head.inner_text().strip() == "Paused: the drive is low on space"
    assert not page.locator(f'.grp .sync-pill[data-sync="{ws.pid}"]').count()
    html = page.content()
    assert "syncthing" not in html.lower() and "remoteState" not in html


# ------------------------------------------------------------------------------------------------ fix round 1
def test_names_never_fall_back_to_ids(tmp_path):
    from ave.syncthing import SOMEONE, SyncManager
    nameless = W.say(decide(IDLE, [SID_J], set(), {}, set()))
    assert nameless["text"] == "Waiting for another computer (offline)" and SID_J[:7] not in str(nameless)
    m = SyncManager(tmp_path / "st", ave_device="dev_A")
    for raw in ("", "dev_01JABCDEF", "ave-join:x1:dev_B:100.64.0.2", SID_J, SID_J[:7]):
        m._peer_names[SID_J] = raw
        assert m._name_of(SID_J) == SOMEONE, raw
    m._peer_names[SID_J] = "imani"
    assert m._name_of(SID_J) == "imani"
    # The person comes from project.json's members and devices before anything Syncthing says.
    root = tmp_path / "p"
    meta = H.write_project_json(root, "prj_NM", "N", "flat", [{"person": "maren", "devices": ["dev_A"]},
                                                           {"person": "imani", "devices": ["dev_B"]}], "dev_A")
    (root / ".ave" / "project.json").write_text(json.dumps({**meta, "devices": {"dev_B": {"sync_id": SID_J, "address": "x"}}}))
    assert m._people(root) == {SID_J: "imani"}


def test_unshared_projects_stay_neutral_when_sync_is_down(tmp_path):
    from ave.syncthing import SyncManager
    m = SyncManager(tmp_path / "st", ave_device="dev_A")          # never started: "stopped"
    shared = tmp_path / "shared"
    H.write_project_json(shared, "prj_SH", "S", "flat", [{"person": "maren", "devices": ["dev_A"]},
                                                        {"person": "imani", "devices": ["dev_B"]}], "dev_A")
    solo = tmp_path / "solo"
    H.write_project_json(solo, "prj_SO", "S", "flat", [{"person": "maren", "devices": ["dev_A"]}], "dev_A")
    assert m.status(solo).state == "not_shared"
    w = W.say(m.status(shared))
    assert m.status(shared).state == "sync_off" and w["tone"] == "wait" and w["text"] == "Sync is off"
    restarting = W.say(SyncStatus("sync_off", error="GET /rest/config/folders: timed out"))
    assert restarting["text"] == "Sync is restarting" and restarting["tone"] == "wait" and not restarting["next"]
    bad = W.say(SyncStatus("sync_off", error="refusing 10.0.0.2: not a Tailscale address"))
    assert bad["text"] == "Sync could not start" and "Tailscale" in bad["next"] and bad["tone"] == "wait"


def test_the_layout_follows_the_episode_after_start(tmp_path):
    """Adopted while empty (flat), then 02_FOOTAGE arrives and `ave start` makes a numbered episode: project.json and the
    sync rules must follow, or cuts, footage and exports would never leave this computer."""
    from ave import start
    from ave.project import zones
    from ave.project.adopt import adopt
    from ave.syncthing import config as C
    root = tmp_path / "ep7"
    root.mkdir()
    adopt(root)
    assert json.loads((root / ".ave" / "project.json").read_text())["layout"] == "flat"
    C.prepare_folder(root, zones._this_device_id())                       # the project is already shared
    assert "!/01_PROJECT/edits" not in (root / ".stignore").read_text()
    (root / "02_FOOTAGE").mkdir()
    (root / "02_FOOTAGE" / "a.mov").write_bytes(b"\0")
    ep = start._prepare(root)
    assert ep.edits == root / "01_PROJECT" / "edits"
    assert json.loads((root / ".ave" / "project.json").read_text())["layout"] == "numbered"
    rules = zones.project_rules(root)
    for rel in ("01_PROJECT/edits/rough.v1.json", "02_FOOTAGE/a.mov", "06_EXPORTS/final.mp4"):
        assert zones.zone_of(rel, rules) != "local", rel
    ignore = (root / ".stignore").read_text()
    assert "!/01_PROJECT/edits" in ignore and "!/06_EXPORTS" in ignore
    # The episode's layout wins even before project.json is rewritten (a member whose project.json is behind).
    meta = json.loads((root / ".ave" / "project.json").read_text())
    (root / ".ave" / "project.json").write_text(json.dumps({**meta, "layout": "flat"}))
    assert zones.zone_of("01_PROJECT/edits/rough.v1.json", zones.project_rules(root)) != "local"
    adopt(root)                                                           # adopt aligns it again
    assert json.loads((root / ".ave" / "project.json").read_text())["layout"] == "numbered"


class _Hung:
    """A sync manager whose every call hangs, like a Syncthing that stopped answering."""
    state = "running"

    def __init__(self):
        self.release = __import__("threading").Event()

    def folders(self):
        self.release.wait(30)
        return []

    def status(self, root):
        self.release.wait(30)
        return SyncStatus("up_to_date")


def test_a_hung_sync_never_delays_board_events(tmp_path, monkeypatch):
    import threading

    from ave.project import Project
    from ave.studio.project import StudioProject
    from ave.studio.service import open_studio
    from ave.studio.ui import workspace as ws_mod
    from ave.studio.ui.server import StudioServer
    monkeypatch.setattr(ws_mod, "WATCH_S", 0.2)
    monkeypatch.setattr(ws_mod, "SYNC_S", 0.2)
    sp = StudioProject(tmp_path / "studio")
    sp.create(title="G", tz="UTC")
    p = Project.create(sp.root / "ep", "Episode", person="Maren")
    hung = _Hung()
    srv = StudioServer(open_studio(sp.root), pin="424242")
    srv.workspace.sync = lambda: hung
    srv.followers = 1                                                       # someone follows the stream
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        time.sleep(0.6)                                                     # the sync look is now stuck inside folders()
        t0 = time.monotonic()
        assert srv.workspace.sync_status()["available"] is False            # the reader never waits for a look
        assert time.monotonic() - t0 < 0.2
        seq = srv.bus.latest
        p.log.append("artifact.created", artifact="art_x", data={"kind": "short", "title": "New", "stage": "Plan"}, actor="claude")
        t0 = time.monotonic()
        evs = []
        while time.monotonic() - t0 < 3 and not any(e["type"] == "board" for e in evs):
            evs = srv.bus.wait(seq, 0.5)
        assert any(e["type"] == "board" for e in evs), evs
        assert time.monotonic() - t0 < 2.0
    finally:
        hung.release.set()
        srv.shutdown()
        t.join(5)
        srv.server_close()


def test_a_status_look_gives_up_quickly_on_a_syncthing_that_does_not_answer(tmp_path):
    """A real SyncManager on a socket that accepts and never replies: the look takes about one short timeout, and says
    "Sync is restarting" for the shared project and nothing alarming for the solo one."""
    import socket
    import tempfile
    from types import SimpleNamespace

    from ave.syncthing import Api, SyncManager
    from ave.studio.ui.workspace import Workspace
    sock_dir = Path(tempfile.mkdtemp(prefix="avq", dir="/tmp"))
    path = sock_dir / "gui.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(8)
    try:
        m = SyncManager(tmp_path / "st", ave_device="dev_A")
        m.supervisor = SimpleNamespace(wanted=True, state="running", running=lambda: True, api=Api(path, "k"), serial=1,
                                       restarts=0, wait_ready=lambda t=30: (_ for _ in ()).throw(AssertionError("waited")))
        shared = tmp_path / "w" / "shared"
        H.write_project_json(shared, "prj_SH", "S", "flat", [{"person": "maren", "devices": ["dev_A"]},
                                                            {"person": "imani", "devices": ["dev_B"]}], "dev_A")
        solo = tmp_path / "w" / "solo"
        H.write_project_json(solo, "prj_SO", "S", "flat", [{"person": "maren", "devices": ["dev_A"]}], "dev_A")
        ws = Workspace(None, roots=lambda: [shared, solo], sync=lambda: m)
        t0 = time.monotonic()
        data = ws.look_sync()
        took = time.monotonic() - t0
        assert took < 4, took
        words = {k: v["text"] for k, v in data["projects"].items()}
        assert words == {"prj_SH": "Sync is restarting", "prj_SO": "Not shared"}, words
    finally:
        srv.close()
        import shutil as sh
        sh.rmtree(sock_dir, ignore_errors=True)


# ------------------------------------------------------------------------------------------------ fix round 2
@pytest.mark.parametrize("episode_json", ['{"layout": "nope"}', '{"layout": {"preset": "numbered", "bogus": 1}}',
                                          "episode.json\n" * 3, '["not", "an", "object"]'])
def test_an_invalid_or_unreadable_episode_layout_keeps_project_jsons(tmp_path, episode_json):
    from ave.episode import Episode
    from ave.project import zones
    from ave.project.adopt import align_layout
    from ave.syncthing import config as C
    root = tmp_path / "p"
    H.write_project_json(root, "prj_BAD", "B", "numbered", [{"person": "maren", "devices": ["dev_A"], "media": "full"}], "dev_A")
    (root / "episode.json").write_text(episode_json)
    rules = zones.project_rules(root, "dev_A")                 # no SystemExit: the numbered rules from project.json
    assert zones.zone_of("01_PROJECT/edits/rough.v1.json", rules) != "local"
    assert "!/01_PROJECT/edits" in C.prepare_folder(root, "dev_A")
    assert align_layout(root) is False                         # an invalid layout is never copied into project.json
    assert json.loads((root / ".ave" / "project.json").read_text())["layout"] == "numbered"
    if not episode_json.startswith("{"):
        assert Episode(root).layout["edits"] == "edits"           # writers still fall back to flat on an unreadable file


def test_an_invalid_project_json_layout_is_a_plain_error_not_an_exit(tmp_path):
    from ave.project import zones
    root = tmp_path / "p"
    H.write_project_json(root, "prj_BAD", "B", "nope", [{"person": "maren", "devices": ["dev_A"]}], "dev_A")
    with pytest.raises(ValueError, match="unknown layout preset"):
        zones.project_rules(root, "dev_A")


def test_a_look_while_sync_starts_says_starting(tmp_path):
    from types import SimpleNamespace

    from ave.syncthing import SyncManager
    m = SyncManager(tmp_path / "st", ave_device="dev_A")
    m.supervisor = SimpleNamespace(wanted=True, state="starting", running=lambda: False, api=None, serial=1, restarts=0)
    assert m.down() is None                                    # an ordinary caller (wait()) waits for readiness
    with m.quick():
        s = m.down()
    assert s.state == "sync_off" and W.say(s)["text"] == "Sync is starting" and W.say(s)["tone"] == "wait"
