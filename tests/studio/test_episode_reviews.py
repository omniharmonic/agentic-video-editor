"""Task 24a: the retired `ave ui`'s content review, episode publish-queue reject, trim clear and jobs, now in the Workspace.

Gates are the old ones, exactly: approving a content item and clearing a trim are the device-owner's acts (the app's native sheet,
a single-use grant); rejecting records the person; an episode post is approved only in a terminal (`ave publish approve`), and no
HTTP route approves one.
"""

from __future__ import annotations

import http.client
import json
import shutil
import threading
import time

import pytest

from ave import cli
from ave.content import manifest as M
from ave.desktop_approvals import Approvals, approval_args
from ave.desktop_auth import NativeAuthority
from ave.episode import Episode
from ave.project import Project
from ave.publish import queue
from ave.studio.project import StudioProject
from ave.studio.service import open_studio
from ave.studio.ui.server import StudioServer

from content_helpers import apply_env, make_episode, pick_quote
from publish_helpers import isolated_secrets, queue_item, video_pool  # noqa: F401  (fixtures)
from sched_helpers import no_agent_markers  # noqa: F401  (autouse: this computer's own agent markers are not the test's)


@pytest.fixture
def world(tmp_path, monkeypatch, capsys, video_pool):
    apply_env(monkeypatch, tmp_path)
    sp = StudioProject(tmp_path / "studio")
    sp.create(title="The Long Table", tz="UTC")
    root = sp.root / "ep12"
    ep = make_episode(root)
    Project.create(root, "Episode 12", person="Maren")
    picks = tmp_path / "picks.json"
    picks.write_text(json.dumps([pick_quote()]))
    assert cli.main(["content", "propose", str(root), str(picks)]) == 0
    assert cli.main(["content", "render", str(root)]) == 0
    capsys.readouterr()
    video = ep.exports / "shorts" / "clip1.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(video_pool / "vertical.mp4", video)
    item = queue_item(ep, video, title="Soil is a habit")
    authority = NativeAuthority()
    srv = StudioServer(open_studio(sp.root), pin="424242", by="Maren", native_authority=authority)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    pid = next(p.project_id for p in srv.workspace.projects() if p.root == root.resolve() or p.root == root)
    yield type("W", (), dict(srv=srv, sp=sp, ep=Episode(root), pid=pid, item=item, authority=authority,
                             sheets=Approvals(srv.app, srv.workspace, srv.episodes)))
    srv.shutdown()
    t.join(5)
    srv.server_close()


def _req(srv, method, path, body=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=20)
    data = json.dumps(body).encode() if body is not None else None
    c.request(method, path, body=data, headers={"X-AVE-Token": srv.token, "Host": f"127.0.0.1:{srv.port}",
                                                 **({"Content-Type": "application/json"} if data else {})})
    r = c.getresponse()
    raw = r.read()
    c.close()
    return r.status, json.loads(raw or b"{}")


def _grant(authority, req):
    return authority._mint(req["action"], req["keys"])


def test_the_approval_summary_lists_content_items_and_episode_posts(world):
    s, data = _req(world.srv, "GET", "/api/workspace/approvals")
    assert s == 200
    assert [(c["project"], c["id"]) for c in data["content"]] == [(world.pid, "quote-c01-4x5")]
    assert data["content"][0]["outputs"][0]["url"].startswith(f"/api/workspace/file/{world.pid}/")
    assert [(e["project"], e["id"]) for e in data["episode_posts"]] == [(world.pid, world.item["id"])]
    s, raw = 0, b""
    c = http.client.HTTPConnection("127.0.0.1", world.srv.port, timeout=10)
    c.request("GET", data["content"][0]["outputs"][0]["url"], headers={"X-AVE-Token": world.srv.token, "Host": f"127.0.0.1:{world.srv.port}"})
    r = c.getresponse()
    assert r.status == 200 and r.read()[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1", b"\xff\xd8\xff\xdb")
    (world.ep.raw / "cam.mp4").parent.mkdir(parents=True, exist_ok=True)
    (world.ep.raw / "cam.mp4").write_bytes(b"raw")
    rel = world.ep.raw.relative_to(world.ep.root).as_posix()
    assert _req(world.srv, "GET", f"/api/workspace/file/{world.pid}/{rel}/cam.mp4")[0] == 403            # never raw footage


def test_rejecting_a_content_item_records_the_person_and_no_route_approves_one(world):
    for route in ("/api/workspace/content/approve", "/api/workspace/content/decide"):
        assert _req(world.srv, "POST", route, {"project": world.pid, "doc": "main", "id": "quote-c01-4x5", "decision": "approve"})[0] == 404
    assert M.load_latest(world.ep)["items"][0]["review"]["status"] == "draft"
    s, out = _req(world.srv, "POST", "/api/workspace/content/reject", {"project": world.pid, "doc": "main", "id": "quote-c01-4x5", "note": "too dark"})
    assert s == 200, out
    rv = M.load_latest(world.ep)["items"][0]["review"]
    assert rv["status"] == "rejected" and rv["by"] == "claude" and rv["note"] == "too dark"     # the room token cannot show it is the person


def test_approving_a_content_item_needs_the_device_owner_grant(world, monkeypatch):
    from ave.studio.ui.api import StudioError
    args = approval_args("approveContent", {"project": world.pid, "doc": "main", "id": "quote-c01-4x5"})
    with pytest.raises(ValueError):
        approval_args("approveContent", {"project": world.pid, "doc": "main", "id": "quote-c01-4x5", "by": "claude"})
    req = world.sheets.protected_request("approveContent", args)
    assert "Approve this content item?" == req["title"] and "Nothing is posted" in req["detail"]
    with pytest.raises(StudioError) as e:
        world.sheets.run_protected(req, None, by="Maren")
    assert e.value.code == "native.authentication_required"
    assert M.load_latest(world.ep)["items"][0]["review"]["status"] == "draft"
    grant = _grant(world.authority, req)
    monkeypatch.setenv("AVE_AGENT", "1")
    with pytest.raises(ValueError, match="agent marker"):
        world.sheets.run_protected(req, grant, by="Maren")
    monkeypatch.delenv("AVE_AGENT")
    world.sheets.run_protected(req, _grant(world.authority, req), by="Maren")
    rv = M.load_latest(world.ep)["items"][0]["review"]
    assert rv["status"] == "approved" and rv["by"] == "Maren"


def test_an_episode_post_is_rejected_here_and_approved_only_in_a_terminal(world):
    s, d = _req(world.srv, "GET", f"/api/workspace/publish/{world.pid}/{world.item['id']}")
    assert s == 200 and d["approve_line"] == f"ave publish approve {world.ep.root} {world.item['id']}"
    for route in (f"/api/workspace/publish/{world.pid}/{world.item['id']}/approve", "/api/workspace/publish/approve"):
        assert _req(world.srv, "POST", route, {"project": world.pid, "id": world.item["id"], "confirm_sha": world.item["sha256"]})[0] == 404
    assert queue.load(world.ep, world.item["id"])["status"] == "awaiting_approval"
    s, out = _req(world.srv, "POST", "/api/workspace/publish/reject", {"project": world.pid, "id": world.item["id"], "reason": "wrong cut"})
    assert s == 200 and out["status"] == "rejected"
    ev = queue.load(world.ep, world.item["id"])["events"][-1]
    assert ev["event"] == "rejected" and ev["by"] == "claude" and ev["reason"] == "wrong cut"     # over the room token: Claude's


def test_clearing_a_trim_is_the_persons_act_through_the_app(world):
    from ave.studio.ui.api import StudioError
    meta = json.loads(world.ep.meta_path.read_text())
    meta["trim"] = {"main": {"in": 3.2, "out": 60.0, "by": "human", "at": "2026-11-02T10:00:00+0000"}}
    world.ep.meta_path.write_text(json.dumps(meta))
    for route in ("/api/workspace/trim/clear", "/api/room/trim/clear"):
        assert _req(world.srv, "POST", route, {"project": world.pid, "session": "main"})[0] == 404
    req = world.sheets.protected_request("clearTrim", approval_args("clearTrim", {"project": world.pid, "session": "main"}))
    assert "starts at 0:03.20" in req["detail"] and "yours" in req["detail"]
    with pytest.raises(StudioError):
        world.sheets.run_protected(req, None)
    assert "main" in json.loads(world.ep.meta_path.read_text())["trim"]
    world.sheets.run_protected(req, _grant(world.authority, req))
    assert "main" not in (json.loads(world.ep.meta_path.read_text()).get("trim") or {})


def test_jobs_run_list_and_cancel_from_the_workspace(world):
    s, cat = _req(world.srv, "GET", "/api/workspace/jobs")
    assert s == 200 and "status" in cat["catalog"] and [p["id"] for p in cat["projects"]] == [world.pid]
    assert _req(world.srv, "POST", "/api/workspace/jobs", {"project": world.pid, "cmd": "publish run", "args": {}})[0] == 400
    assert _req(world.srv, "POST", "/api/workspace/jobs", {"project": world.pid, "cmd": "status", "args": {"yes": True}})[0] == 400
    s, job = _req(world.srv, "POST", "/api/workspace/jobs", {"project": world.pid, "cmd": "status", "args": {}})
    assert s == 201 and job["status"] == "running"
    end = time.time() + 120
    while time.time() < end:
        s, tail = _req(world.srv, "GET", f"/api/workspace/jobs/{world.pid}/{job['id']}")
        if tail["status"] != "running":
            break
        time.sleep(0.2)
    assert tail["status"] == "done" and "$ ave status" in tail["tail"]
    rows = _req(world.srv, "GET", "/api/workspace/jobs")[1]["jobs"]
    assert rows[0]["id"] == job["id"] and rows[0]["title"] == "Episode 12" and "log" not in rows[0]
    from ave import jobs as J                                        # a long job, cancelled through the route
    J_code = J.CHILD_CODE
    try:
        J.CHILD_CODE = "import time; time.sleep(60)"
        s, slow = _req(world.srv, "POST", "/api/workspace/jobs", {"project": world.pid, "cmd": "status", "args": {}})
    finally:
        J.CHILD_CODE = J_code
    s, out = _req(world.srv, "POST", f"/api/workspace/jobs/{world.pid}/{slow['id']}/cancel", {})
    assert s == 200 and out["status"] == "stopping"
    r = world.srv.episodes._runner(world.pid)[1]
    end = time.time() + 30
    while r.jobs[slow["id"]]["status"] != "cancelled" and time.time() < end:
        time.sleep(0.05)
    assert r.jobs[slow["id"]]["status"] == "cancelled"
    assert _req(world.srv, "POST", f"/api/workspace/jobs/{world.pid}/{slow['id']}/cancel", {})[0] == 409


@pytest.fixture
def browser(world):            # after the world: rendering the content item drives its own Playwright first
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:
            pytest.skip(f"Chromium unavailable: {e}")
        yield b
        b.close()


def _app(browser, world, route="#/workspace"):
    pg = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.add_init_script("window.__AVE_TEST__ = true;")
    pg.goto(world.srv.app_url() + route)
    return pg, errors


def test_the_approval_sheet_shows_content_and_episode_posts_and_rejects_them(world, browser):
    pg, errors = _app(browser, world, "#/workspace/approvals")
    sheet = pg.locator(".approval-sheet")
    sheet.get_by_role("region", name="Content to review").wait_for()
    sheet.locator('[data-kind="content"]').click()
    assert sheet.get_by_role("button", name="Approve on this Mac").is_disabled()          # a browser cannot check that it is you
    sheet.get_by_role("textbox", name="Note for Claude").fill("The quote is cut off")
    sheet.get_by_role("button", name="Reject").click()
    pg.get_by_text("Rejected: Quote post").wait_for()
    assert M.load_latest(world.ep)["items"][0]["review"]["status"] == "rejected"
    sheet.locator('[data-kind="episode_post"]').click()
    line = sheet.locator(".ap-cli-line")
    line.wait_for()
    assert line.inner_text() == f"ave publish approve {world.ep.root} {world.item['id']}"
    assert sheet.get_by_role("button", name="Approve on this Mac").count() == 0              # only a terminal approves an episode post
    sheet.get_by_role("button", name="Reject").click()
    pg.get_by_text("Rejected: Soil is a habit").wait_for()
    assert queue.load(world.ep, world.item["id"])["status"] == "rejected"
    assert not errors
    pg.close()


def test_the_jobs_panel_runs_a_step_and_shows_its_log(world, browser):
    pg, errors = _app(browser, world)
    pg.get_by_role("button", name="Jobs", exact=True).click()
    panel = pg.locator(".jobs-panel")
    panel.get_by_role("button", name="Step: ave status").wait_for()
    panel.get_by_role("button", name="Run", exact=True).click()
    panel.locator('.job[data-status="done"]').wait_for(timeout=60_000)
    panel.get_by_role("button", name="Show the log").click()
    panel.locator(".job-log", has_text="$ ave status").wait_for()
    assert not errors
    pg.close()


def _flag(world):
    doc = M.clone(M.load_latest(world.ep))
    doc["items"][0]["review"]["critic"] = {"ok": False, "notes": ["the quote runs into the margin"]}
    M.save_new_version(world.ep, doc)


def test_a_flagged_content_item_approves_only_with_a_note_that_overrides_the_critic(world):
    """The old page's "Approve despite the critic": the person's note is the override, under the same native grant."""
    _flag(world)
    args = {"project": world.pid, "doc": "main", "id": "quote-c01-4x5"}
    with pytest.raises(ValueError, match="Say why it is fine anyway"):
        world.sheets.protected_request("approveContent", approval_args("approveContent", args))
    req = world.sheets.protected_request("approveContent", approval_args("approveContent", {**args, "note": "The margin is intentional"}))
    assert "despite the critic" in req["title"] and "You are overriding the critic: The margin is intentional" in req["detail"]
    world.sheets.run_protected(req, _grant(world.authority, req), by="Maren")
    rv = M.load_latest(world.ep)["items"][0]["review"]
    assert rv["status"] == "approved" and rv["by"] == "Maren" and rv.get("critic_override") is True and rv["note"] == "The margin is intentional"


def test_a_content_approval_covers_only_the_files_the_sheet_showed(world):
    req = world.sheets.protected_request("approveContent", approval_args("approveContent", {"project": world.pid, "doc": "main", "id": "quote-c01-4x5"}))
    req["episodes"][2]["shas"] = ["0" * 64]                      # as if Claude re-rendered while the sheet was open
    with pytest.raises(ValueError, match="files changed"):
        world.sheets.run_protected(req, _grant(world.authority, req), by="Maren")
    assert M.load_latest(world.ep)["items"][0]["review"]["status"] == "draft"


def test_the_jobs_panel_updates_in_place_and_keeps_focus(world, browser, monkeypatch):
    from ave import jobs as J
    monkeypatch.setattr(J, "CHILD_CODE", "import sys, time\nfor i in range(400):\n    print('line', i, flush=True); time.sleep(0.05)\n")
    pg, errors = _app(browser, world)
    pg.get_by_role("button", name="Jobs", exact=True).click()
    panel = pg.locator(".jobs-panel")
    panel.get_by_role("button", name="Run", exact=True).click()
    panel.locator('.job[data-status="running"]').wait_for()
    panel.get_by_role("button", name="Show the log").click()
    cancel = panel.get_by_role("button", name="Cancel", exact=True)
    cancel.focus()
    pg.evaluate("() => { document.activeElement.__mark = 1; window.__log = document.querySelector('.job-log'); }")
    pg.wait_for_timeout(3500)                                                   # two polls at least
    assert pg.evaluate("() => document.activeElement.__mark === 1 && document.activeElement.isConnected")      # the same button, still focused
    assert pg.evaluate("() => window.__log === document.querySelector('.job-log') && document.querySelector('.job-log').textContent.includes('line')")
    pg.keyboard.press("Enter")                                                  # Cancel, from the keyboard
    panel.locator('.job[data-status="cancelled"]').wait_for(timeout=15000)
    assert not errors
    pg.close()


def test_a_reject_through_the_app_is_recorded_as_the_person(world):
    """The ruling on minor 7: over the room token a reject is Claude's; through the app's sheet it is the person's (no check needed)."""
    world.sheets.app_reject("rejectContent", approval_args("rejectContent", {"project": world.pid, "doc": "main", "id": "quote-c01-4x5", "note": "no"}), "Maren")
    rv = M.load_latest(world.ep)["items"][0]["review"]
    assert rv["status"] == "rejected" and rv["by"] == "Maren"
    world.sheets.app_reject("rejectEpisodePost", approval_args("rejectEpisodePost", {"project": world.pid, "id": world.item["id"], "reason": "wrong cut"}), "Maren")
    ev = queue.load(world.ep, world.item["id"])["events"][-1]
    assert ev["event"] == "rejected" and ev["by"] == "Maren"
    with pytest.raises(ValueError):
        approval_args("rejectContent", {"project": world.pid, "doc": "main", "id": "quote-c01-4x5", "by": "Maren"})   # no name from the page
