"""The Workspace in a real browser: every artifact across the projects on this computer, edited by hand as the person.

A studio folder ("Grounded conversations") holds two posts and two adopted episode folders: ep12 ("Episode 12", in Refine, with a
short as its part, a decision Claude made and a task Claude is on) and ep13 ("Episode 13", in Plan). Everything here writes the
real project logs; the assertions read those logs back.
"""

from __future__ import annotations

import json
import re
import threading
import time
from types import SimpleNamespace

import pytest
from conftest import CI_SLOW

from ave.project import Project
from ave.studio.project import StudioProject
from ave.studio.service import open_studio
from ave.studio.ui import workspace as ws_mod
from ave.studio.ui.server import StudioServer

from . import board_bridge


@pytest.fixture(scope="module")
def browser():
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:
            pytest.skip(f"Chromium unavailable: {e}")
        yield b
        b.close()


@pytest.fixture(autouse=True)
def fast_watch(monkeypatch):
    monkeypatch.setattr(ws_mod, "WATCH_S", 0.3)


def _serve(root):
    srv = StudioServer(open_studio(root), pin="424242")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, t


def _stop(srv, t):
    srv.shutdown()
    t.join(5)
    srv.server_close()


def _events(p: Project) -> list[dict]:
    return sorted(p.read_events(), key=lambda e: (e["hlc"], e["id"]))


def _new(p: Project, aid: str, title: str, kind: str, stage: str, actor: str = "human", **fields):
    p.log.append("artifact.created", artifact=aid, data={"kind": kind, "title": title, "stage": stage, **fields}, actor=actor)


@pytest.fixture
def ws(tmp_path):
    sp = StudioProject(tmp_path / "studio")
    sp.create(title="Grounded conversations", tz="UTC")
    studio = Project.create(sp.root, "Grounded conversations", person="Maren")
    _new(studio, "art_post_launch", "Launch post", "post", "Draft")
    _new(studio, "art_thumb", "Thumbnail for 12", "thumbnail", "Plan", due="2026-11-20")
    p12 = Project.create(sp.root / "ep12", "Episode 12", person="Maren")
    _new(p12, "art_ep", "Episode 12", "episode", "Refine")
    p12.log.append("version.added", artifact="art_ep", data={"version": "rough.s2.v8", "file": "edits/rough.s2.v8.json"}, actor="claude")
    p12.log.append("version.added", artifact="art_ep", data={"version": "rough.s2.v9", "file": "edits/rough.s2.v9.json"}, actor="claude")
    _new(p12, "art_short", "The van story", "short", "Draft", actor="claude")
    p12.log.append("artifact.linked", artifact="art_ep", data={"to": "art_short", "rel": "part"}, actor="claude")
    p12.log.append("version.added", artifact="art_short", data={"version": "short.van.v1", "file": "edits/short.van.v1.json"}, actor="claude")
    p12.log.append("decision.recorded", artifact="art_ep", actor="claude",
                   data={"decision": "dec_wide", "summary": "Opened on the wide shot", "options": ["Wide shot", "Close-up"],
                         "chosen": "Wide shot", "reason": "The close-up is soft for two seconds"})
    p12.log.append("task.updated", artifact="art_ep", actor="claude", data={"task": "t1", "title": "tightening the opening", "status": "doing"})
    p13 = Project.create(sp.root / "ep13", "Episode 13", person="Maren")
    _new(p13, "art_ep", "Episode 13", "episode", "Plan")
    srv, t = _serve(sp.root)
    yield SimpleNamespace(srv=srv, root=sp.root, studio=studio, p12=p12, p13=p13,
                          ep12=f"{p12.project_id}:art_ep", short=f"{p12.project_id}:art_short", ep13=f"{p13.project_id}:art_ep",
                          post=f"{studio.project_id}:art_post_launch", thumb=f"{studio.project_id}:art_thumb")
    _stop(srv, t)


TEST_FLAG = "window.__AVE_TEST__ = true;"


def _open(browser, srv, route="#/workspace", size=(1440, 900), bridge=False):
    pg = browser.new_page(viewport={"width": size[0], "height": size[1]})
    pg.add_init_script(TEST_FLAG)
    if bridge:                                  # the app's native board bridge: writes are the person's (final review I1)
        board_bridge.install(pg, srv)
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(srv.app_url() + route)
    return pg, errors


@pytest.fixture(autouse=True)
def no_agent_markers(monkeypatch):
    """The suite may run inside an agent harness; the app's bridge refuses every write while a marker is set."""
    from ave import humanpresence
    for k in humanpresence.AGENT_MARKERS:
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def page(browser, ws):
    """The Workspace as the person uses it in the app: its board writes go through the app's native bridge."""
    pg, errors = _open(browser, ws.srv, bridge=True)
    pg.wait_for_selector(".ws-row[data-key]")
    pg.wait_for_function("() => window.__ws && window.__ws.live === 'live'")
    yield pg
    pg.close()
    assert not errors


def row(page, key):
    return page.locator(f'.ws-row[data-key="{key}"]').first


def cell(page, key, col):
    return row(page, key).locator(f'[data-col="{col}"]')


def wait_log(p: Project, pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        for e in reversed(_events(p)):
            if pred(e):
                return e
        time.sleep(0.05)
    raise AssertionError("the expected event was not written")


# ---------------------------------------------------------------------------------------------------- the brief's tests
def test_rows_nest_parts_under_their_artifact(page, ws):
    assert row(page, ws.ep12).is_visible()
    assert not row(page, ws.short).is_visible()                     # a part is folded under its artifact
    expand = page.get_by_role("button", name="Show 1 part of Episode 12")
    assert expand.is_visible()
    expand.click()
    part = row(page, ws.short)
    part.wait_for()
    assert part.get_attribute("data-parent") == ws.ep12
    keys = page.eval_on_selector_all(".ws-row[data-key]", "rs => rs.map(r => r.dataset.key)")
    assert keys.index(ws.short) == keys.index(ws.ep12) + 1           # directly under its artifact
    assert "is-part" in part.get_attribute("class")
    page.get_by_role("button", name="Hide 1 part of Episode 12").click()
    part.wait_for(state="hidden")


def test_group_by_stage_and_by_project(page, ws):
    titles = lambda: page.eval_on_selector_all(".grp .grp-title", "ts => ts.map(t => t.textContent)")
    assert set(titles()) == {"Grounded conversations", "Episode 12", "Episode 13"}
    page.get_by_role("button", name="Group by").click()
    page.get_by_role("menuitemradio", name="Stage").click()
    page.wait_for_function("() => [...document.querySelectorAll('.grp .grp-title')].some(t => t.textContent === 'Refine')")
    assert titles() == ["Plan", "Draft", "Refine"]                    # stages in order, empty ones left out
    refine = page.locator(".grp", has=page.locator(".grp-title", has_text="Refine"))
    assert refine.locator(f'.ws-row[data-key="{ws.ep12}"]').count() == 1
    page.get_by_role("button", name="Group by").click()
    page.get_by_role("menuitemradio", name="Project").click()
    page.wait_for_function("() => [...document.querySelectorAll('.grp .grp-title')].some(t => t.textContent === 'Episode 13')")


def test_a_manual_edit_is_logged_with_the_person(page, ws):
    before = len(_events(ws.p13))
    cell(page, ws.ep13, "name").locator(".ws-name-text").click()
    edit = page.locator(".ws-edit")
    edit.fill("Episode 13: the river")
    edit.press("Escape")                                              # Esc cancels: nothing written
    assert cell(page, ws.ep13, "name").locator(".ws-name-text").inner_text() == "Episode 13"
    time.sleep(0.2)
    assert len(_events(ws.p13)) == before
    cell(page, ws.ep13, "name").locator(".ws-name-text").click()
    page.locator(".ws-edit").fill("Episode 13: the river")
    page.locator(".ws-edit").press("Enter")
    e = wait_log(ws.p13, lambda e: e["op"] == "artifact.updated")
    assert e["actor"] == "human" and e["person"] == "Maren" and e["data"] == {"title": "Episode 13: the river"}
    page.wait_for_function(f"() => document.querySelector('.ws-row[data-key=\"{ws.ep13}\"] .ws-name-text').textContent === 'Episode 13: the river'")


def test_claude_activity_updates_the_row_live(page, ws):
    ws.p13.log.append("stage.signed_off", artifact="art_ep", actor="claude",
                      data={"from": "Plan", "to": "Draft", "quote": "lock the brief", "relayed_by": "claude"})
    page.wait_for_function(f"() => document.querySelector('.ws-row[data-key=\"{ws.ep13}\"] [data-col=stage]').textContent.includes('Draft')",
                           timeout=8000)
    stage = cell(page, ws.ep13, "stage")
    assert "is-changed" in stage.get_attribute("class")              # animates in softly, with who changed it
    assert stage.locator(".changed-by").get_attribute("aria-label") == "Changed by Claude"
    assert cell(page, ws.ep13, "waiting").inner_text().strip().startswith("Waiting on you")


def test_row_opens_in_the_room(page, ws):
    row(page, ws.ep12).hover()
    page.get_by_role("button", name="Actions for Episode 12").click()
    page.get_by_role("menuitem", name="Open in the Room").click()
    page.wait_for_selector(".room")
    assert page.evaluate("location.hash") == "#/room/ep12"
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"
    page.get_by_role("link", name="Workspace").click()
    page.wait_for_selector(".ws-row[data-key]")
    assert page.evaluate("document.documentElement.dataset.theme") == "light"


def test_empty_workspace_says_how_to_start(browser, tmp_path):
    sp = StudioProject(tmp_path / "empty")
    sp.create(title="Empty", tz="UTC")
    srv, t = _serve(sp.root)
    try:
        pg, errors = _open(browser, srv)
        pg.wait_for_selector(".ws-empty")
        text = pg.locator(".ws-empty").inner_text()
        assert "No projects yet" in text and "Claude" in text
        assert pg.locator(".ws-empty .btn").count() >= 1
        assert not errors
        pg.close()
    finally:
        _stop(srv, t)


def test_changed_parts_show_needs_review(page, ws):
    assert not cell(page, ws.ep12, "name").locator(".flag-review").count()
    ws.p12.log.append("stage.reopened", artifact="art_ep", data={"from": "Refine", "to": "Draft"}, actor="human")
    page.get_by_role("button", name="Show 1 part of Episode 12").click()
    flag = cell(page, ws.short, "name").locator(".flag-review")
    flag.wait_for(timeout=8000)
    assert flag.inner_text() == "Needs review"
    assert "Episode 12 was reopened" in flag.get_attribute("data-tip")


# ---------------------------------------------------------------------------------------------------- editing by hand
def test_stage_change_is_a_sign_off_by_the_person_and_undo_reopens(page, ws):
    cell(page, ws.ep13, "stage").click()
    pop = page.locator(".popover")
    pop.get_by_role("menuitemradio", name="Draft").click()
    e = wait_log(ws.p13, lambda e: e["op"] == "stage.signed_off")
    assert e["actor"] == "human" and e["person"] == "Maren" and e["data"] == {"from": "Plan", "to": "Draft"}
    assert "Draft" in cell(page, ws.ep13, "stage").inner_text()
    page.locator(".toast").get_by_role("button", name="Undo").click()
    e = wait_log(ws.p13, lambda e: e["op"] == "stage.reopened")
    assert e["actor"] == "human" and e["data"] == {"from": "Draft", "to": "Plan"}
    page.wait_for_function(f"() => document.querySelector('.ws-row[data-key=\"{ws.ep13}\"] [data-col=stage]').textContent.includes('Plan')")


def test_out_is_refused_with_an_explanation(page, ws):
    cell(page, ws.ep12, "stage").click()
    out = page.locator(".popover").get_by_role("menuitemradio", name="Out")
    assert out.is_disabled()
    assert "publish approval" in page.locator(".popover .stage-why").inner_text()
    page.keyboard.press("Escape")
    status = page.evaluate(f"""async () => (await fetch('/api/workspace/artifact/' + encodeURIComponent('{ws.ep12}'),
        {{method: 'PATCH', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify({{stage: 'Out'}})}})).status""")
    assert status == 409
    assert not [e for e in _events(ws.p12) if e["op"] == "stage.signed_off"]


RELAY_STUB = TEST_FLAG + """ window.__asked = []; Object.defineProperty(window, 'aveNative', {configurable: true, value: {
    confirmRelayed: async (key, event) => { window.__asked.push(['confirm', key, event]); return {}; },
    undoRelayed: async (key, event) => { window.__asked.push(['undo', key, event]); return {}; }}});"""


def test_relayed_sign_off_can_be_confirmed_or_undone(browser, ws, page, monkeypatch):
    """Without the app, Confirm cannot be made (it is the check on Claude) and says why; Undo still works and is the person's.
    In the app, Confirm and Undo go to the app, which checks Confirm against the device owner (tested above, without a page)."""
    from ave import humanpresence
    for k in humanpresence.AGENT_MARKERS:                    # the suite may run inside an agent harness: an undo would be Claude's
        monkeypatch.delenv(k, raising=False)
    e1 = ws.p13.log.append("stage.signed_off", artifact="art_ep", actor="claude",
                           data={"from": "Plan", "to": "Draft", "quote": "yes, lock the brief", "relayed_by": "claude"})
    waiting = cell(page, ws.ep13, "waiting")
    page.wait_for_function(f"() => document.querySelector('.ws-row[data-key=\"{ws.ep13}\"] [data-col=waiting]').textContent.includes('Waiting on you')",
                           timeout=8000)
    waiting.click()
    pop = page.locator(".popover")
    assert "Claude reports you said: “yes, lock the brief”" in pop.inner_text()
    confirm = pop.get_by_role("button", name="Confirm")
    assert confirm.is_disabled() and "AVE Studio app" in confirm.get_attribute("data-tip")
    pop.get_by_role("button", name="Undo").click()
    e = wait_log(ws.p13, lambda e: e["op"] == "stage.reopened")
    assert e["actor"] == "human" and e["data"] == {"from": "Draft", "to": "Plan", "undoes": e1["id"]}
    page.wait_for_function(f"() => document.querySelector('.ws-row[data-key=\"{ws.ep13}\"] [data-col=stage]').textContent.includes('Plan')")
    assert not [x for x in _events(ws.p13) if x["op"] == "note.added"]                    # nothing confirmed
    # in the app: both go to the app, naming only the artifact and the sign-off
    e2 = ws.p13.log.append("stage.signed_off", artifact="art_ep", actor="claude",
                           data={"from": "Plan", "to": "Draft", "quote": "good", "relayed_by": "claude"})
    pg = browser.new_page(viewport={"width": 1440, "height": 900})
    pg.add_init_script(RELAY_STUB)
    pg.goto(ws.srv.app_url() + "#/workspace")
    pg.wait_for_function(f"() => (document.querySelector('.ws-row[data-key=\"{ws.ep13}\"] [data-col=waiting]')?.textContent || '').includes('Waiting on you')",
                         timeout=8000)
    cell(pg, ws.ep13, "waiting").click()
    pg.locator(".popover").get_by_role("button", name="Confirm").click()
    pg.wait_for_function("() => window.__asked.length === 1")
    cell(pg, ws.ep13, "waiting").click()
    pg.locator(".popover").get_by_role("button", name="Undo").click()
    pg.wait_for_function("() => window.__asked.length === 2")
    assert pg.evaluate("window.__asked") == [["confirm", ws.ep13, e2["id"]], ["undo", ws.ep13, e2["id"]]]
    assert not [x for x in _events(ws.p13) if x["op"] == "note.added"]                    # the page itself wrote nothing
    pg.close()


def test_an_open_decision_opens_with_its_options(page, ws):
    waiting = cell(page, ws.ep12, "waiting")
    assert waiting.inner_text().strip().startswith("Waiting on you")
    waiting.click()
    pop = page.locator(".popover")
    assert "Opened on the wide shot" in pop.inner_text()
    assert "Claude chose" in pop.inner_text()
    pop.get_by_role("button", name="Close-up").click()
    e = wait_log(ws.p12, lambda e: e["op"] == "decision.resolved")
    assert e["actor"] == "human" and e["data"] == {"decision": "dec_wide", "choice": "Close-up"}


def test_new_artifact_is_made_by_the_person_at_plan(page, ws):
    page.get_by_role("button", name="New artifact", exact=True).click()
    edit = page.locator(".ws-addrow .ws-add-input:focus")
    edit.fill("Newsletter for 12")
    edit.press("Enter")
    e = wait_log(ws.studio, lambda e: e["op"] == "artifact.created" and e["data"].get("title") == "Newsletter for 12")
    assert e["actor"] == "human" and e["person"] == "Maren" and e["data"]["stage"] == "Plan"
    page.locator(".ws-name-text", has_text="Newsletter for 12").wait_for()
    page.get_by_role("button", name="More ways to add").click()
    files = page.get_by_role("menuitem", name="From files")
    assert files.is_disabled()                                       # never a web file input: files come through the app
    assert page.locator(".menu-note", has_text="AVE Studio app").count() == 1


def test_files_go_through_the_app(browser, ws):
    pg = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.add_init_script(TEST_FLAG + """ window.__picked = 0; Object.defineProperty(window, 'aveNative', {configurable: true,
        value: {importFiles: async () => { window.__picked += 1; return {added: []}; }, selectEpisode: async () => ({})}});""")
    pg.goto(ws.srv.app_url() + "#/workspace")
    pg.wait_for_selector(".ws-row[data-key]")
    pg.get_by_role("button", name="More ways to add").click()
    pg.get_by_role("menuitem", name="From files").click()
    pg.wait_for_function("() => window.__picked === 1")
    assert pg.locator("input[type=file]").count() == 0
    pg.close()
    assert not errors


def test_due_date_and_person_edit_inline(page, ws):
    cell(page, ws.ep13, "due").click()
    inp = page.locator(".popover input")
    inp.fill("2026-12-01")
    inp.press("Enter")
    e = wait_log(ws.p13, lambda e: e["op"] == "artifact.updated")
    assert e["data"] == {"due": "2026-12-01"} and e["actor"] == "human"
    assert cell(page, ws.ep13, "due").inner_text().strip() == "Dec 1"
    cell(page, ws.ep13, "who").click()
    page.locator(".popover").get_by_role("menuitemradio", name="Claude").click()
    e = wait_log(ws.p13, lambda e: e["op"] == "artifact.updated" and "owner" in e["data"])
    assert e["data"] == {"owner": "Claude"} and e["actor"] == "human"


# ---------------------------------------------------------------------------------------------------- the rest of the anatomy
def test_item_panel_shows_updates_versions_decisions_and_activity(page, ws):
    row(page, ws.ep12).hover()
    page.get_by_role("button", name="Open Episode 12", exact=True).click()
    panel = page.locator(".ws-panel")
    panel.wait_for()
    assert panel.locator("h2").inner_text() == "Episode 12"
    box = panel.get_by_role("textbox", name="Write an update")
    box.fill("The opening drags; try the question first.")
    panel.get_by_role("button", name="Post update").click()
    e = wait_log(ws.p12, lambda e: e["op"] == "note.added")
    assert e["actor"] == "human" and e["data"] == {"text": "The opening drags; try the question first."}
    panel.get_by_text("The opening drags; try the question first.").wait_for()
    panel.get_by_role("tab", name="Versions").click()
    text = panel.inner_text()
    assert "Rough cut, session 2, version 2" in text and ".json" not in text and "rough.s2" not in text
    panel.get_by_role("tab", name="Decisions").click()
    assert "Opened on the wide shot" in panel.inner_text()
    panel.get_by_role("tab", name="Activity").click()
    assert "Claude" in panel.inner_text()
    page.keyboard.press("Escape")
    panel.wait_for(state="hidden")


def test_multi_select_batch_bar_and_undo(page, ws):
    cell(page, ws.ep13, "check").click()
    cell(page, ws.thumb, "check").click()
    bar = page.locator(".batch")
    bar.wait_for()
    assert "2" in bar.locator(".batch-count").inner_text()
    bar.get_by_role("button", name="Stage").click()
    page.locator(".popover").get_by_role("menuitemradio", name="Draft").click()
    wait_log(ws.p13, lambda e: e["op"] == "stage.signed_off" and e["data"]["to"] == "Draft")
    wait_log(ws.studio, lambda e: e["op"] == "stage.signed_off" and e["artifact"] == "art_thumb")
    page.locator(".toast").get_by_role("button", name="Undo").click()
    wait_log(ws.p13, lambda e: e["op"] == "stage.reopened")
    wait_log(ws.studio, lambda e: e["op"] == "stage.reopened" and e["artifact"] == "art_thumb")
    page.keyboard.press("Escape")
    bar.wait_for(state="hidden")


def test_drag_a_row_to_reorder(page, ws):
    keys = lambda: page.eval_on_selector_all(f'.grp[data-group="{ws.studio.project_id}"] .ws-row[data-key]', "rs => rs.map(r => r.dataset.key)")
    assert keys() == [ws.post, ws.thumb]
    row(page, ws.thumb).hover()
    handle = row(page, ws.thumb).locator(".row-handle")
    box = handle.bounding_box()
    target = row(page, ws.post).bounding_box()
    page.mouse.move(box["x"] + 6, box["y"] + 6)
    page.mouse.down()
    page.mouse.move(box["x"] + 6, target["y"] + 4, steps=8)
    page.mouse.up()
    e = wait_log(ws.studio, lambda e: e["op"] == "artifact.updated" and "order" in e["data"])
    assert e["artifact"] == "art_thumb" and e["actor"] == "human"
    page.wait_for_function(f"() => document.querySelector('.grp[data-group=\"{ws.studio.project_id}\"] .ws-row[data-key]').dataset.key === '{ws.thumb}'")


def test_kanban_moves_a_card_and_refuses_out(page, ws):
    page.get_by_role("tab", name="Kanban").click()
    card = page.locator(f'.kb-card[data-key="{ws.ep13}"]')
    card.wait_for()
    draft = page.locator('.kb-col[data-stage="Draft"] .kb-list').bounding_box()
    b = card.bounding_box()
    page.mouse.move(b["x"] + 20, b["y"] + 20)
    page.mouse.down()
    page.mouse.move(draft["x"] + 40, draft["y"] + 30, steps=10)
    page.mouse.up()
    e = wait_log(ws.p13, lambda e: e["op"] == "stage.signed_off")
    assert e["data"] == {"from": "Plan", "to": "Draft"} and e["actor"] == "human"
    page.locator(f'.kb-col[data-stage="Draft"] .kb-card[data-key="{ws.ep13}"]').wait_for()
    card = page.locator(f'.kb-card[data-key="{ws.ep13}"]')
    out = page.locator('.kb-col[data-stage="Out"] .kb-list').bounding_box()
    b = card.bounding_box()
    page.mouse.move(b["x"] + 20, b["y"] + 20)
    page.mouse.down()
    page.mouse.move(out["x"] + 40, out["y"] + 30, steps=10)
    page.mouse.up()
    page.locator(".toast", has_text="publish").wait_for()
    assert page.locator(f'.kb-col[data-stage="Draft"] .kb-card[data-key="{ws.ep13}"]').count() == 1


def test_keyboard(page, ws):
    page.locator("body").click(position={"x": 700, "y": 10})
    page.keyboard.press("/")
    assert page.evaluate("document.activeElement.getAttribute('aria-label')") == "Search artifacts"
    page.keyboard.type("thumb")
    page.wait_for_function("() => document.querySelectorAll('.ws-row[data-key]').length === 1")
    page.keyboard.press("Escape")
    page.wait_for_function("() => document.querySelectorAll('.ws-row[data-key]').length > 1")
    page.keyboard.press("?")
    page.locator(".popover", has_text="Keyboard shortcuts").wait_for()
    page.keyboard.press("Escape")
    cell(page, ws.ep13, "name").focus()
    page.keyboard.press("ArrowRight")
    assert page.evaluate("document.activeElement.dataset.col") == "stage"
    page.keyboard.press("Enter")
    page.locator(".popover").get_by_role("menuitemradio", name="Plan").wait_for()
    page.keyboard.press("Escape")
    assert page.evaluate("document.activeElement.dataset.col") == "stage"
    page.keyboard.press("ArrowLeft")
    page.keyboard.press("Enter")
    page.locator(".ws-edit").wait_for()
    page.keyboard.press("Escape")
    page.keyboard.press("o")
    page.locator(".ws-panel").wait_for()
    page.keyboard.press("Escape")


def test_home_shows_projects_what_waits_and_activity(browser, ws):
    pg, errors = _open(browser, ws.srv, "#/workspace/home")
    pg.wait_for_selector(".home")
    assert pg.locator(".home h1").inner_text().startswith(("Good morning", "Good afternoon", "Good evening"))
    cards = pg.locator(".proj-card")
    assert cards.count() == 3
    assert "Opened on the wide shot" in pg.locator(".home-waiting").inner_text()
    assert pg.locator(".home-activity .feed-item").count() > 3
    pg.locator(".proj-card", has_text="Episode 12").click()
    pg.wait_for_selector(".ws-row[data-key]")
    assert pg.locator(".ws-head h1").inner_text() == "Episode 12"
    assert pg.locator(".grp").count() == 1
    pg.close()
    assert not errors


def test_load_error_is_designed_and_retries(browser, ws):
    pg = browser.new_page(viewport={"width": 1440, "height": 900})
    pg.add_init_script(TEST_FLAG)
    pg.route("**/api/workspace/board", lambda r: r.fulfill(status=500, body='{"error": "boom"}', content_type="application/json"))
    pg.goto(ws.srv.app_url() + "#/workspace")
    pg.wait_for_selector(".ws-error")
    assert "could not load" in pg.locator(".ws-error").inner_text()
    pg.unroute("**/api/workspace/board")
    pg.get_by_role("button", name="Try again").click()
    pg.wait_for_selector(".ws-row[data-key]")
    pg.close()


def test_500_rows_without_jank(browser, tmp_path):
    sp = StudioProject(tmp_path / "big")
    sp.create(title="Big", tz="UTC")
    stages = ["Plan", "Draft", "Refine", "Finish", "Out"]
    for n in range(5):
        p = Project.create(sp.root / f"season{n}", f"Season {n + 1}", person="Maren")
        lines = []
        for i in range(100):
            _new(p, f"art_{i:03d}", f"Episode {n * 100 + i}: a long title that keeps going to test the cell", "episode", stages[i % 5],
                 due=f"2026-12-{i % 28 + 1:02d}")
    srv, t = _serve(sp.root)
    try:
        pg, errors = _open(browser, srv)
        pg.wait_for_function("() => document.querySelectorAll('.ws-row[data-key]').length === 500", timeout=20000)
        first = pg.evaluate("window.__ws.renderMs")
        assert first < 1500 * CI_SLOW, f"first render took {first} ms"
        frames = pg.evaluate("""async () => {
            const sc = document.querySelector('.ws-view'); const times = []; let last = performance.now();
            for (let i = 0; i < 90; i++) { sc.scrollTop += 120; await new Promise(r => requestAnimationFrame(r));
              const now = performance.now(); times.push(now - last); last = now; }
            times.sort((a, b) => a - b); return {p95: times[Math.floor(times.length * .95)], max: times.at(-1)}; }""")
        assert frames["p95"] < 50 * CI_SLOW, frames
        ms = pg.evaluate("""async () => { const t = performance.now(); await window.__ws.reload(); return performance.now() - t; }""")
        assert ms < 1500 * CI_SLOW, f"a live update of 500 rows took {ms} ms"
        assert not errors
        pg.close()
    finally:
        _stop(srv, t)


# Every visible control, in document order. The tab already selected and the rail link to the page already open are where you
# are, not controls that should change anything, so they are left out. Focus alone does not count as an answer.
CONTROLS = """() => [...document.querySelectorAll('button, [role=tab], [role=checkbox], a[href], [data-act]')]
  .filter(el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && !el.disabled && el.getAttribute('aria-disabled') !== 'true'
      && el.getAttribute('aria-selected') !== 'true' && el.getAttribute('aria-current') !== 'page'
      && r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth; })
  .map(el => el.getAttribute('aria-label') || el.dataset.act && (el.closest('[data-key]')?.dataset.key + '|' + el.dataset.col) || el.textContent.trim())"""

SIG = """() => JSON.stringify([location.hash, document.body.innerHTML.length, document.querySelectorAll('.popover, .menu, .toast, .ws-panel:not([hidden]), .ws-edit').length,
  [...document.querySelectorAll('[aria-pressed], [aria-expanded], [aria-selected], [aria-checked]')].map(e => e.getAttribute('aria-pressed') + e.getAttribute('aria-expanded') + e.getAttribute('aria-selected') + e.getAttribute('aria-checked')).join(''),
  window.__ws.events, window.__ws.writes])"""


def test_every_visible_control_answers(browser, ws):
    pg, errors = _open(browser, ws.srv)
    pg.wait_for_selector(".ws-row[data-key]")
    names = pg.evaluate(CONTROLS)
    assert len(names) > 25
    pg.close()
    dead, skipped = [], []
    for i, name in enumerate(names):
        pg, errors = _open(browser, ws.srv)
        pg.wait_for_selector(".ws-row[data-key]")
        pg.wait_for_function("() => window.__ws.live === 'live'")
        now = pg.evaluate(CONTROLS)
        if i >= len(now) or now[i] != name:
            pg.close()
            skipped.append(name)
            continue                                                # the board changed under a previous click (a write it made)
        before = pg.evaluate(SIG)
        pg.evaluate(f"""() => {{ const all = [...document.querySelectorAll('button, [role=tab], [role=checkbox], a[href], [data-act]')]
            .filter(el => {{ const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
              return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && !el.disabled && el.getAttribute('aria-disabled') !== 'true'
                && el.getAttribute('aria-selected') !== 'true' && el.getAttribute('aria-current') !== 'page'
                && r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth; }});
            all[{i}].click(); }}""")
        time.sleep(0.35)
        if pg.evaluate(SIG) == before:
            dead.append(name)
        pg.close()
        assert not errors, (name, errors)
    assert not dead, f"controls that did nothing: {dead}"
    assert len(skipped) < len(names) // 4, f"too many controls could not be reached again: {skipped}"


# ---------------------------------------------------------------------------------------------------- fix round 1
from ave.studio.ui.api import StudioError  # noqa: E402


def test_a_read_never_hides_a_write_from_the_watcher(ws):
    w = ws.srv.workspace
    w.board()
    ws.p13.log.append("stage.signed_off", artifact="art_ep", actor="claude",
                      data={"from": "Plan", "to": "Draft", "quote": "go", "relayed_by": "claude"})
    w.board()                                                     # a read straight after Claude's write
    assert ws.p13.project_id in w.poll()                          # the watcher still sees it, so a board event goes out


def test_the_watcher_only_runs_while_someone_follows(ws):
    assert ws.srv.followers == 0
    w = ws.srv.workspace
    sent = []
    w._publish = lambda t, d: sent.append(d)
    ws.p13.log.append("note.added", artifact="art_ep", data={"text": "hi"}, actor="claude")
    time.sleep(1.0)
    assert not sent
    ws.srv.followers = 1
    try:
        end = time.time() + 3
        while not sent and time.time() < end:
            time.sleep(0.05)
        assert sent and ws.p13.project_id in sent[0]["projects"]
    finally:
        ws.srv.followers = 0


def test_keyboard_expands_parts_and_opens_the_row_menu(page, ws):
    cell(page, ws.ep12, "name").focus()
    page.keyboard.press("Alt+ArrowRight")
    row(page, ws.short).wait_for()
    page.keyboard.press("Alt+ArrowLeft")
    row(page, ws.short).wait_for(state="hidden")
    expand = row(page, ws.ep12).locator(".ws-expand")
    assert expand.get_attribute("tabindex") is None              # in the Tab order
    expand.focus()
    page.keyboard.press("Enter")
    row(page, ws.short).wait_for()
    handle = row(page, ws.ep13).locator(".row-handle")
    assert handle.get_attribute("tabindex") is None
    handle.focus()
    page.keyboard.press("Enter")
    menu = page.get_by_role("menu", name="Actions for Episode 13")
    menu.wait_for()
    page.keyboard.press("Escape")
    cell(page, ws.ep13, "stage").focus()
    page.keyboard.press("Shift+F10")
    menu.wait_for()
    menu.get_by_role("menuitem", name="Open in the Room").click()
    page.wait_for_selector(".room")
    assert page.evaluate("location.hash") == "#/room/ep13"


def test_long_project_names_keep_the_end_that_tells_them_apart(browser, tmp_path):
    sp = StudioProject(tmp_path / "series")
    sp.create(title="Series", tz="UTC")
    for n in (12, 13, 14):
        p = Project.create(sp.root / f"ep{n}", f"Neighbourhood Assembly, episode {n}", person="Maren")
        _new(p, "art_ep", f"Neighbourhood Assembly, episode {n}", "episode", "Plan")
    srv, t = _serve(sp.root)
    try:
        pg, errors = _open(browser, srv)
        pg.wait_for_selector(".rail-list .rail-item")
        items = pg.locator(".rail-list .rail-item")
        pg.wait_for_timeout(100)
        for i, n in enumerate((12, 13, 14)):
            lab = items.nth(i).locator(".rail-label")
            text = lab.inner_text()
            assert text.startswith("Neighbour") and "\u2026" in text and text.endswith(f"episode {n}"), text
            assert lab.evaluate("e => e.scrollWidth <= e.clientWidth + 1")       # the end that differs is on screen, not clipped
        assert [items.nth(i).get_attribute("title") for i in range(3)] == [f"Neighbourhood Assembly, episode {n}" for n in (12, 13, 14)]
        assert [items.nth(i).locator(".rail-swatch").inner_text() for i in range(3)] == ["12", "13", "14"]
        assert not errors
        pg.close()
    finally:
        _stop(srv, t)


def _status(fn):
    with pytest.raises(StudioError) as e:
        fn()
    return e.value.status


def _owner(ws, monkeypatch):
    """The app's device-owner check, as the Workspace sees it in the desktop app (desktop_auth.NativeAuthority)."""
    from ave import humanpresence
    from ave.desktop_auth import NativeAuthority
    for k in humanpresence.AGENT_MARKERS:
        monkeypatch.delenv(k, raising=False)
    a = NativeAuthority()
    ws.srv.workspace.authority = a
    return a


def _confirm_in_app(ws, a, key, eid):
    """What the app's Confirm sheet does after the check: spend the grant bound to exactly this sign-off."""
    grant = a._mint("confirmRelayed", [f"{key}/{eid}"])
    with a.use(grant, "confirmRelayed", [f"{key}/{eid}"]):
        return ws.srv.workspace.patch(key, {"confirm": eid}, via="app")


def test_relayed_undo_and_confirm_are_refused_once_settled(ws, monkeypatch):
    w = ws.srv.workspace
    a = _owner(ws, monkeypatch)
    e1 = ws.p13.log.append("stage.signed_off", artifact="art_ep", actor="claude",
                           data={"from": "Plan", "to": "Draft", "quote": "go", "relayed_by": "claude"})
    _confirm_in_app(ws, a, ws.ep13, e1["id"])
    assert _status(lambda: w.patch(ws.ep13, {"undo": e1["id"]})) == 409
    assert _status(lambda: _confirm_in_app(ws, a, ws.ep13, e1["id"])) == 409
    e2 = ws.p13.log.append("stage.signed_off", artifact="art_ep", actor="claude",
                           data={"from": "Draft", "to": "Refine", "quote": "yes", "relayed_by": "claude"})
    w.patch(ws.ep13, {"undo": e2["id"]})
    assert _status(lambda: w.patch(ws.ep13, {"undo": e2["id"]})) == 409
    assert _status(lambda: _confirm_in_app(ws, a, ws.ep13, e2["id"])) == 409
    # an answer Claude relayed that the person has since replaced cannot be undone
    r = ws.p12.log.append("decision.resolved", artifact="art_ep", actor="claude",
                          data={"decision": "dec_wide", "choice": "Close-up", "quote": "close", "relayed_by": "claude"})
    w.patch(ws.ep12, {"reopen_decision": "dec_wide"})
    w.patch(ws.ep12, {"resolve": "dec_wide", "choice": "Wide shot"})
    assert _status(lambda: w.patch(ws.ep12, {"undo": r["id"]})) == 409


def test_confirming_a_relayed_sign_off_needs_the_device_owner_check_bound_to_it(ws, monkeypatch):
    """The confirm is the anti-fabrication check on what Claude reported: Claude must not be able to make it. With only the
    room token (no grant), with a grant for another sign-off, under an agent marker, or with no app at all, it is refused and
    nothing is written. Only the app's sheet, after the device-owner check for this sign-off, records it as the person."""
    from ave.studio.ui.api import StudioError
    w = ws.srv.workspace
    e1 = ws.p13.log.append("stage.signed_off", artifact="art_ep", actor="claude",
                           data={"from": "Plan", "to": "Draft", "quote": "lock the brief", "relayed_by": "claude"})
    before = len(_events(ws.p13))
    assert _status(lambda: w.patch(ws.ep13, {"confirm": e1["id"]})) == 403                 # no app: nobody can prove a person
    a = _owner(ws, monkeypatch)
    assert _status(lambda: w.patch(ws.ep13, {"confirm": e1["id"]})) == 403                 # the room token: no grant
    other = a._mint("confirmRelayed", [f"{ws.ep13}/01SOMEOTHERSIGNOFF0000000"])
    with a.use(other, "confirmRelayed", [f"{ws.ep13}/01SOMEOTHERSIGNOFF0000000"]):
        assert _status(lambda: w.patch(ws.ep13, {"confirm": e1["id"]})) == 403             # a check for another sign-off
    grant = a._mint("confirmRelayed", [f"{ws.ep13}/{e1['id']}"])
    monkeypatch.setenv("CLAUDECODE", "1")
    with pytest.raises(StudioError):
        with a.use(grant, "confirmRelayed", [f"{ws.ep13}/{e1['id']}"]):
            w.patch(ws.ep13, {"confirm": e1["id"]}, via="app")
    monkeypatch.delenv("CLAUDECODE")
    assert len(_events(ws.p13)) == before                                                   # nothing was recorded
    _confirm_in_app(ws, a, ws.ep13, e1["id"])
    e = wait_log(ws.p13, lambda e: e["op"] == "note.added")
    assert e["actor"] == "human" and e["data"]["confirms"] == e1["id"]


def test_undoing_a_relayed_sign_off_is_the_persons_only_through_the_app(ws, monkeypatch):
    """Undo stays unchecked (it only takes Claude's record back), but it is written as the person only when it comes through
    the app. In the app, a bare room-token request, or any request while an agent marker is set, is recorded as Claude's."""
    w = ws.srv.workspace
    _owner(ws, monkeypatch)
    relay = lambda frm, to: ws.p13.log.append("stage.signed_off", artifact="art_ep", actor="claude",
                                              data={"from": frm, "to": to, "quote": "yes", "relayed_by": "claude"})
    e1 = relay("Plan", "Draft")
    w.patch(ws.ep13, {"undo": e1["id"]})                                                     # the room token, not the app
    assert _events(ws.p13)[-1]["actor"] == "claude" and _events(ws.p13)[-1]["data"]["undoes"] == e1["id"]
    e2 = relay("Plan", "Draft")
    w.patch(ws.ep13, {"undo": e2["id"]}, via="app")                                          # the app's Undo
    assert _events(ws.p13)[-1]["actor"] == "human" and _events(ws.p13)[-1]["data"]["undoes"] == e2["id"]
    e3 = relay("Plan", "Draft")
    monkeypatch.setenv("AVE_AGENT", "1")
    w.patch(ws.ep13, {"undo": e3["id"]}, via="app")                                          # an agent-marked caller
    assert _events(ws.p13)[-1]["actor"] == "claude"


def test_the_apps_confirm_sheet_shows_what_claude_reported_and_spends_only_its_own_check(ws, monkeypatch):
    from ave.desktop_approvals import Approvals
    from ave.studio.ui.api import StudioError
    a = _owner(ws, monkeypatch)
    ws.srv.app.native_authority = a
    sheets = Approvals(ws.srv.app, ws.srv.workspace)
    e1 = ws.p13.log.append("stage.signed_off", artifact="art_ep", actor="claude",
                           data={"from": "Plan", "to": "Draft", "quote": "lock the brief", "relayed_by": "claude"})
    req = sheets.protected_request("confirmRelayed", {"artifact": ws.ep13, "event": e1["id"]})
    assert "Claude reports you said: “lock the brief”" in req["detail"] and req["keys"] == [f"{ws.ep13}/{e1['id']}"]
    with pytest.raises(StudioError):
        sheets.run_protected(req, None)                                                    # no check
    sheets.run_protected(req, a._mint(req["action"], req["keys"]))
    e = wait_log(ws.p13, lambda e: e["op"] == "note.added")
    assert e["actor"] == "human" and e["data"]["confirms"] == e1["id"]
    e2 = ws.p13.log.append("stage.signed_off", artifact="art_ep", actor="claude",
                           data={"from": "Draft", "to": "Refine", "quote": "good", "relayed_by": "claude"})
    sheets.undo_relayed({"artifact": ws.ep13, "event": e2["id"]})                          # the app's Undo: no check, the person's
    assert _events(ws.p13)[-1]["actor"] == "human" and _events(ws.p13)[-1]["data"]["undoes"] == e2["id"]


def test_bad_patches_are_refused(page, ws):
    send = lambda body: page.evaluate(f"""async () => (await fetch('/api/workspace/artifact/' + encodeURIComponent('{ws.ep13}'),
        {{method: 'PATCH', headers: {{'Content-Type': 'application/json'}}, body: {json.dumps(body)}}})).status""")
    assert send('{"fields": {"order": NaN}}') == 400
    assert send('{"fields": {"order": Infinity}}') == 400
    assert send('{"fields": {"title": "x"}, "stage": "Draft"}') == 400
    assert not [e for e in _events(ws.p13) if e["op"] in ("artifact.updated", "stage.signed_off")]
    ws.studio.log.append("artifact.created", artifact="art_studio", data={"kind": "studio", "title": "Studio"}, actor="human")
    key = f"{ws.studio.project_id}:art_studio"
    assert page.evaluate(f"async () => (await fetch('/api/workspace/artifact/' + encodeURIComponent('{key}'))).status") == 404


def test_nothing_shows_an_internal_id_or_path(page, ws):
    row(page, ws.ep12).hover()
    page.get_by_role("button", name="Open Episode 12", exact=True).click()
    page.locator(".ws-panel").get_by_role("tab", name="Versions").click()
    page.locator(".ws-panel .panel-row").first.wait_for()
    page.get_by_role("button", name="About this board").click()
    seen = page.evaluate("""() => document.body.innerText + ' ' + [...document.querySelectorAll('[title], [data-tip], [aria-label]')]
        .map(e => (e.getAttribute('title') || '') + ' ' + (e.getAttribute('data-tip') || '') + ' ' + (e.getAttribute('aria-label') || '')).join(' ')""")
    for bad in ("rough.s2", ".json", str(ws.root), "prj_", "art_", "/ep12"):
        assert bad not in seen, bad


# ---------------------------------------------------------------------------------------------------- fix round 2: a drag that never drops
def _start_drag(page, handle):
    b = handle.bounding_box()
    page.mouse.move(b["x"] + b["width"] / 2, b["y"] + b["height"] / 2)
    page.mouse.down()
    page.mouse.move(b["x"] + 10, b["y"] + 60, steps=6)
    page.locator(".ws-drag-ghost").wait_for()


def _claude_moves_ep13(page, ws, to="Draft", sel=None):
    ws.p13.log.append("stage.signed_off", artifact="art_ep", actor="claude",
                      data={"from": "Plan" if to == "Draft" else "Draft", "to": to, "quote": "ok", "relayed_by": "claude"})
    if sel:                                                    # a card shows its stage by the column it sits in
        page.locator(sel).wait_for(timeout=8000)
        return
    sel = f'.ws-row[data-key="{ws.ep13}"] [data-col=stage]'
    page.wait_for_function(f"() => (document.querySelector('{sel}')?.textContent || '').includes('{to}')", timeout=8000)


@pytest.mark.parametrize("how", ["pointercancel", "escape", "blur"])
def test_an_abandoned_row_drag_lets_live_updates_through(page, ws, how):
    row(page, ws.thumb).hover()
    _start_drag(page, row(page, ws.thumb).locator(".row-handle"))
    if how == "pointercancel":
        page.evaluate("window.dispatchEvent(new PointerEvent('pointercancel', {bubbles: true}))")
    elif how == "escape":
        page.keyboard.press("Escape")
    else:
        page.evaluate("window.dispatchEvent(new Event('blur'))")
    _claude_moves_ep13(page, ws)
    assert page.locator(".ws-drag-ghost, .ws-row.is-dragging, .ws-drop-line").count() == 0
    page.mouse.up()
    assert not [e for e in _events(ws.studio) if e["op"] == "artifact.updated"]   # abandoned, so nothing was moved


def test_an_abandoned_card_drag_lets_live_updates_through(page, ws):
    page.get_by_role("tab", name="Kanban").click()
    card = page.locator(f'.kb-card[data-key="{ws.thumb}"]')
    card.wait_for()
    _start_drag(page, card)
    page.evaluate("window.dispatchEvent(new PointerEvent('pointercancel', {bubbles: true}))")
    _claude_moves_ep13(page, ws, sel=f'.kb-col[data-stage="Draft"] .kb-card[data-key="{ws.ep13}"]')
    assert page.locator(".ws-drag-ghost, .kb-card.is-dragging, .kb-drop-line").count() == 0
    page.mouse.up()


def test_a_stuck_drag_gives_up_after_the_safety_timeout(page, ws):
    page.evaluate("window.__aveDragSafetyMs = 400")                     # a minute in the app; shortened here
    row(page, ws.thumb).hover()
    _start_drag(page, row(page, ws.thumb).locator(".row-handle"))
    page.locator(".ws-drag-ghost").wait_for(state="detached", timeout=3000)   # no movement, no drop: it lets go on its own
    _claude_moves_ep13(page, ws)
    assert page.locator(".ws-row.is-dragging").count() == 0
    page.mouse.up()


def test_a_route_change_ends_a_drag(page, ws):
    row(page, ws.thumb).hover()
    _start_drag(page, row(page, ws.thumb).locator(".row-handle"))
    page.evaluate("location.hash = '#/workspace/'")
    page.locator(".ws-drag-ghost").wait_for(state="detached", timeout=3000)
    _claude_moves_ep13(page, ws)
    page.mouse.up()


# ---------------------------------------------------------------------------------------------------- Task 16: sharing
SHARE_STUB = TEST_FLAG + """ window.__asked = []; Object.defineProperty(window, 'aveNative', {configurable: true, value: {
    shareProject: async (id) => { window.__asked.push(['share', id]); return {state: 'shared', person: 'Imani', title: 'Episode 12'}; },
    joinProject: async () => { window.__asked.push(['join']); return {state: 'joined', title: 'The Long Table'}; },
    removeMember: async (id) => { window.__asked.push(['remove', id]); return {state: 'removed', message: 'Imani is no longer on Episode 12. They keep everything they already received.'}; },
    handOverPublishing: async (id) => { window.__asked.push(['handover', id]); throw new Error('Publishing stays with this computer'); },
    selectEpisode: async () => ({})}});"""


def test_share_a_project_goes_through_the_app(browser, ws):
    meta = json.loads(ws.p12.project_json.read_text())
    meta["members"].append({"person": "Imani", "devices": ["dev_B"], "media": "proxies"})
    ws.p12.project_json.write_text(json.dumps(meta, indent=2) + "\n")
    pg = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.add_init_script(SHARE_STUB)
    pg.goto(ws.srv.app_url() + f"#/workspace/p/{ws.p12.project_id}")
    pg.wait_for_selector(".ws-row[data-key]")
    share = pg.get_by_role("button", name="Share", exact=True)
    assert share.is_visible() and share.is_enabled()
    share.click()
    pg.wait_for_function("() => window.__asked.length === 1")
    assert pg.evaluate("window.__asked") == [["share", ws.p12.project_id]]          # the page names the project, nothing else
    assert pg.get_by_text("Imani is on Episode 12 now").is_visible()
    pg.get_by_role("button", name="Board options").click()
    pg.get_by_role("menuitem", name="Join a shared project").click()
    pg.wait_for_function("() => window.__asked.length === 2")
    pg.get_by_role("button", name="Board options").click()
    pg.get_by_role("menuitem", name="Hand over publishing").click()
    pg.wait_for_function("() => window.__asked.length === 3")
    assert pg.get_by_text("Publishing stays with this computer").is_visible()
    pg.get_by_role("button", name="Board options").click()
    pg.get_by_role("menuitem", name="Remove someone from this project").click()
    pg.wait_for_function("() => window.__asked.length === 4")
    assert pg.get_by_text("They keep everything they already received").is_visible()
    assert [a[0] for a in pg.evaluate("window.__asked")] == ["share", "join", "handover", "remove"]
    assert pg.locator("input[type=file]").count() == 0
    pg.close()
    assert not errors


def test_share_without_the_app_says_why(page, ws):
    page.goto(ws.srv.app_url() + f"#/workspace/p/{ws.p12.project_id}")
    page.wait_for_selector(".ws-row[data-key]")
    share = page.get_by_role("button", name="Share", exact=True)
    assert share.is_visible() and share.is_disabled()
    assert "AVE Studio app" in share.get_attribute("data-tip")
    page.get_by_role("button", name="Board options").click()
    assert page.get_by_role("menuitem", name="Share this project").is_disabled()
    assert page.locator(".menu-note", has_text="Sharing needs the AVE Studio app").count() == 1
    page.keyboard.press("Escape")
    page.goto(ws.srv.app_url() + "#/workspace")
    page.wait_for_selector(".ws-row[data-key]")
    assert page.get_by_role("button", name="Share", exact=True).is_hidden()        # sharing is per project


def test_membership_changes_read_plainly_in_activity(ws):
    ws.p12.log.append("member.added", data={"person": "Imani", "device": "dev_B", "media": "proxies"})
    ws.p12.log.append("project.publisher_changed", data={"from": "a", "to": "dev_B", "person": "Imani"})
    texts = [a["text"] for a in ws.srv.workspace.board()["activity"]]
    assert "shared the project with Imani" in texts and "handed publishing to Imani" in texts


# ==================================================================================================== Calendar, Previews and Sources
# Ported from the old screens' tests (test_planner_ui.py, test_visual_planner.py, the Library tests in test_ui_panels.py and
# test_ux_regressions.py), now against the Workspace's views. The world is the planner rig's two-brand studio (fixed clock: Mon
# 19 Oct 2026, 10:00 in Denver) made a Workspace project with two other members, Imani (review proxies only) and Sam (all the
# media), plus an episode folder with footage, and an artifact due on Thu 22 Oct.

from .planner_rig import build as _build_rig, retry as _retry                             # noqa: E402

from ave.publish import queue as _pq                                                     # noqa: E402

POLL = " window.__avePollMs = 300;"


def _join(p: Project, *people):
    meta = json.loads(p.project_json.read_text())
    for person, media in people:
        meta["members"].append({"person": person, "devices": [f"dev-{person.lower()}-7f3a"], "media": media})
    p.project_json.write_text(json.dumps(meta, indent=2))


def build_plan_world(tmp_path, browser=None):
    """The planner rig's studio as a Workspace project, with an episode next to it. Returns a namespace (see the fixture)."""
    rig = _build_rig(tmp_path, browser)
    sp = rig.w.sp
    studio = Project.create(sp.root, "Acme Bakery", person="Maren")
    _join(studio, ("Imani", "proxies"), ("Sam", "full"))
    _new(studio, "art_launch", "Launch film", "episode", "Draft", due="2026-10-22")
    ep = Project.create(sp.root / "ep12", "Episode 12", person="Maren")
    _join(ep, ("Imani", "proxies"), ("Sam", "full"))
    _new(ep, "art_ep", "Episode 12", "episode", "Refine")
    for rel, data in (("raw/cam-a/A001.mov", b"a" * 2048), ("raw/cam-a/A002.mov", b"b" * 1024), ("raw/proxies/cam-a/A001.mov", b"p"),
                      ("work/music/bed.wav", b"m" * 512), ("work/cache/render.mp4", b"c"), ("raw/._A001.mov", b"x")):
        f = ep.root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
    # Episode 13 is Imani's on this computer, which keeps review proxies only: the originals are on Maren's computer
    jo = Project.create(sp.root / "ep13", "Episode 13", person="Imani")
    meta = json.loads(jo.project_json.read_text())
    meta["members"][0]["media"] = "proxies"
    meta["members"].append({"person": "Maren", "devices": ["dev-maren-7f3a"], "media": "full"})
    jo.project_json.write_text(json.dumps(meta, indent=2))
    (jo.root / "raw/proxies/cam-b").mkdir(parents=True)
    (jo.root / "raw/proxies/cam-b/B001.mov").write_bytes(b"proxy")
    # a carousel with two pictures, unscheduled, and one picture in the library with no licence entry
    from studio_helpers import add_asset, make_png
    a1, a2 = rig.w.asset(alt="The first loaf, whole."), rig.w.asset(alt="The second loaf, sliced.")
    post = _retry(lambda: rig.w.studio.create_post(idea="Two loaves", copy={"base": "Two loaves, two ways: whole for the table, sliced for the week."},
                                                   media=[{"slot": 0, "asset": a1["id"], "role": "image", "alt": a1["alt"], "crop": {}},
                                                          {"slot": 1, "asset": a2["id"], "role": "image", "alt": a2["alt"], "crop": {}}]))
    post = rig.w.studio.add_variant(post["id"], account="ig-main", platform="instagram", post_type="carousel", expected_rev=post["rev"])
    rig.posts["carousel"] = (post["id"], post["variants"][-1]["id"])
    loose = add_asset(sp, make_png(tmp_path / "flour-sack.png", color=(200, 180, 120)), alt="A flour sack on a shelf.")
    rig.srv.workspace.look_posts()            # the world is built: only changes from here on are news on the stream
    return SimpleNamespace(rig=rig, srv=rig.srv, sp=sp, studio=studio, ep=ep, jo=jo, launch=f"{studio.project_id}:art_launch", loose=loose)


@pytest.fixture
def plan(tmp_path, browser):
    world = build_plan_world(tmp_path, browser)
    yield world
    world.rig.close()
    world.srv.shutdown()
    world.srv.server_close()


def _view(browser, world, view, *, size=(1440, 900), init="", route="#/workspace"):
    pg = browser.new_page(viewport={"width": size[0], "height": size[1]}, accept_downloads=True)
    pg.add_init_script(TEST_FLAG + POLL + init)
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(world.srv.app_url() + route)
    pg.wait_for_selector(".ws-tabs .tab")
    pg.get_by_role("tab", name=view, exact=True).click()
    return pg, errors


def _item(pg, world, name):
    pid, vid = world.rig.posts[name]
    return pg.locator(f'.cal-item[data-variant="{vid}"]')


def _week(pg):
    pg.locator(".cal .seg-opt[data-mode=week]").click()
    pg.wait_for_selector(".cal-grid.is-week .cal-item")


def _sheet(pg, world, name):
    _item(pg, world, name).first.click()
    pg.wait_for_selector(".post-sheet:not([hidden]) .ps-actions")
    return pg.locator(".post-sheet")


DND = """([src, dst]) => {
  const a = document.querySelector(src), b = document.querySelector(dst);
  if (!a || !b) return false;
  const dt = new DataTransfer();
  const fire = (el, type) => el.dispatchEvent(new DragEvent(type, { bubbles: true, cancelable: true, dataTransfer: dt }));
  fire(a, "dragstart"); fire(b, "dragenter"); fire(b, "dragover"); fire(b, "drop"); fire(a, "dragend");
  return true;
}"""


def _drag(pg, src, dst):
    """An HTML5 drag from one element to another, as real DragEvents (Playwright's mouse drag misses targets under load)."""
    assert pg.evaluate(DND, [src, dst]), (src, dst)


def _until(pg, fn, timeout=6.0):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return
        pg.wait_for_timeout(50)
    raise AssertionError("timed out waiting for the server-side state to change")


@pytest.fixture
def cal(browser, plan):
    pg, errors = _view(browser, plan, "Calendar")
    pg.wait_for_selector(".cal-grid .cal-item")
    yield pg
    pg.close()
    assert not errors


# ---------------------------------------------------------------------------------------------------- the calendar
def test_calendar_shows_posts_on_their_days_due_dates_and_the_unscheduled_tray(cal, plan):
    assert cal.locator(".cal-title").inner_text() == "October 2026"                          # the studio's today, not the browser's
    assert cal.locator('.cal-day[data-date="2026-10-19"]').get_attribute("class").count("is-today") == 1
    wed = cal.locator('.cal-day[data-date="2026-10-21"] .cal-item')
    assert wed.count() == 3
    assert "09:00" in _item(cal, plan, "review").inner_text() and "Sourdough day three" in _item(cal, plan, "review").inner_text()
    assert _item(cal, plan, "approved").first.evaluate("el => el.closest('.cal-day').dataset.date") == "2026-10-23"
    due = cal.locator(f'.cal-item.is-due[data-key="{plan.launch}"]')
    assert due.evaluate("el => el.closest('.cal-day').dataset.date") == "2026-10-22" and "Launch film" in due.inner_text()
    tray = cal.locator(".cal-tray")
    assert _item(cal, plan, "draft").evaluate("el => !!el.closest('.cal-tray')") and "Unscheduled" in tray.inner_text()
    # Month and Week, and the arrows
    _week(cal)
    assert cal.locator(".cal-week .cal-day").count() == 7 and cal.locator(".cal-title").inner_text().startswith("Mon 19 Oct")
    cal.get_by_role("button", name="Next", exact=True).click()
    cal.wait_for_function("() => document.querySelector('.cal-title').innerText.startsWith('Mon 26 Oct')")
    cal.get_by_role("button", name="Today", exact=True).click()
    cal.wait_for_function("() => document.querySelector('.cal-title').innerText.startsWith('Mon 19 Oct')")


def test_a_post_opens_its_sheet_with_the_preview_words_alt_text_status_and_history(cal, plan):
    _week(cal)
    sheet = _sheet(cal, plan, "review")
    cal.wait_for_selector(".post-sheet .ps-phone .pv")
    assert cal.evaluate("location.hash") == "#/workspace"                                    # a sheet, not another page
    assert sheet.locator("#ps-title").inner_text().startswith("Sourdough day three")
    body = sheet.inner_text()
    assert "On Instagram" in body and "Approximate" in body
    assert "#sourdough" in body and "#smallbakery" in body and "Recipe in the link in bio." in body
    assert "A loaf of bread on a wooden table, crust split." in body
    assert "Waiting for your approval" in sheet.locator(".ps-reason").inner_text()
    assert sheet.locator(".ps-history li").count() >= 1
    assert sheet.get_by_role("button", name="Open in the Room").is_visible()
    for word in ("Approve", "Publish", "Send", "Queue"):                                    # human-only acts never live here
        assert sheet.get_by_role("button", name=word, exact=True).count() == 0
    cal.keyboard.press("Escape")
    cal.wait_for_selector(".post-sheet[hidden]", state="attached")
    assert cal.evaluate("document.activeElement.dataset.variant") == plan.rig.posts["review"][1]   # focus is back on the post


def test_a_missing_alt_text_and_a_rework_note_are_called_out(cal, plan):
    from ave.studio import store
    from ave.studio.events import AGENT
    pid, vid = plan.rig.posts["fb"]
    plan.rig.w.studio.edit_post(pid, {"media.0.alt": ""}, expected_rev=store.load_post(plan.sp, pid)["rev"], actor=AGENT)
    plan.rig.w.studio.add_feedback(pid, "rework_copy", note="Too salesy. Say what the bread tastes like.", variant=vid, by="maren")
    cal.reload()
    cal.wait_for_selector(".cal-grid .cal-item")
    _week(cal)
    sheet = _sheet(cal, plan, "fb")
    cal.wait_for_selector(".post-sheet .ps-alts li.missing")
    assert "Missing" in sheet.locator(".ps-alts li.missing").inner_text()
    assert "Too salesy" in sheet.locator(".ps-note").inner_text() and "Sent back to Claude" in sheet.locator(".ps-reason").inner_text()


def test_change_time_with_a_date_and_a_time_then_undo(cal, plan):
    _week(cal)
    sheet = _sheet(cal, plan, "review")
    sheet.locator("[data-act=change-time]").click()
    pop = cal.locator(".ct-pop")
    assert cal.input_value("#ct-date") == "2026-10-21" and cal.input_value("#ct-time") == "09:00"
    assert pop.locator("input[type=date], input[type=time], select").count() == 0          # the kit's fields, not browser widgets
    cal.fill("#ct-date", "Oct 22")
    cal.fill("#ct-time", "14:30")
    pop.get_by_role("button", name="Move", exact=True).click()
    _until(cal, lambda: plan.rig.local("review") == "2026-10-22T14:30")                       # an exact time, not snapped to a slot
    assert plan.rig.item("review")["status"] == "awaiting_approval"                          # moving never approves
    toast = cal.locator(".toast", has_text="Moved")
    toast.wait_for()
    toast.get_by_role("button", name="Undo").click()
    _until(cal, lambda: plan.rig.local("review") == "2026-10-21T09:00")
    cal.wait_for_function(f"() => document.querySelector('.cal-day[data-date=\"2026-10-21\"] .cal-item[data-variant=\"{plan.rig.posts['review'][1]}\"]')")


def test_the_picker_lists_the_next_free_slots_and_a_slot_fills_the_fields(cal, plan):
    sheet = _sheet(cal, plan, "draft")                                                       # in the Unscheduled tray
    sheet.locator("[data-act=change-time]").click()
    cal.wait_for_selector(".ct-pop .ct-chip")
    chips = cal.locator(".ct-pop .ct-chip").all_inner_texts()
    assert chips[0] == "Mon 26 Oct, 09:00"                       # Wed 21 and Fri 23 hold the review and the approved post on ig-main
    assert "Wed 21 Oct, 09:00" not in chips and "Fri 23 Oct, 09:00" not in chips
    cal.locator(".ct-pop .ct-chip").first.click()
    assert cal.input_value("#ct-date") == "2026-10-26" and cal.input_value("#ct-time") == "09:00"
    cal.locator(".ct-pop").get_by_role("button", name="Move", exact=True).click()
    _until(cal, lambda: plan.rig.local("draft") == "2026-10-26T09:00")


def test_next_free_slot_from_the_sheet(cal, plan):
    _week(cal)
    sheet = _sheet(cal, plan, "fb")
    sheet.locator("[data-act=next-slot]").click()
    _until(cal, lambda: plan.rig.local("fb") == "2026-10-23T09:00")                          # strictly later than now, never its own
    cal.locator(".toast", has_text="Moved").wait_for()


def test_a_taken_slot_is_refused_in_words_and_nothing_moves(cal, plan):
    _week(cal)
    sheet = _sheet(cal, plan, "approved")                                                    # Fri 23 Oct 09:00, ig-main
    sheet.locator("[data-act=change-time]").click()
    cal.fill("#ct-date", "2026-10-21")                                                       # the review post holds Wed 21 09:00 on ig-main
    cal.fill("#ct-time", "09:00")
    cal.locator(".ct-pop").get_by_role("button", name="Move", exact=True).click()
    cal.wait_for_selector(".ct-pop .ct-err:not(:empty)")
    assert "taken" in cal.locator(".ct-pop .ct-err").inner_text()
    assert plan.rig.local("approved") == "2026-10-23T09:00"


def test_a_date_that_cannot_be_read_is_explained_before_anything_is_sent(cal, plan):
    sheet = _sheet(cal, plan, "draft")
    sheet.locator("[data-act=change-time]").click()
    cal.fill("#ct-date", "someday")
    cal.locator(".ct-pop").get_by_role("button", name="Move", exact=True).click()
    assert "Write a date" in cal.locator(".ct-pop .ct-err").inner_text() and cal.get_attribute("#ct-date", "aria-invalid") == "true"
    assert plan.rig.local("draft") is None


def test_retiming_an_approved_post_keeps_the_approval_and_explains_itself(cal, plan):
    before = plan.rig.item("approved")
    assert before["status"] == "approved"
    _week(cal)
    sheet = _sheet(cal, plan, "approved")
    sheet.locator("[data-act=change-time]").click()
    assert "the approval stays" in cal.locator(".ct-pop .ct-note").inner_text()
    cal.fill("#ct-date", "2026-10-24")
    cal.fill("#ct-time", "10:00")
    cal.locator(".ct-pop").get_by_role("button", name="Move", exact=True).click()
    _until(cal, lambda: plan.rig.local("approved") == "2026-10-24T10:00")
    after = plan.rig.item("approved")
    assert after["status"] == "approved" and after["approvals"] == before["approvals"]       # the approval is untouched
    assert "moved_after_approval" in plan.rig.variant("approved")["flags"]
    assert "the approval still holds" in cal.locator(".toast", has_text="Moved").inner_text()
    cal.wait_for_selector(".post-sheet .flag-chip.f-moved_after_approval")
    assert "covers the words and pictures, not the time" in sheet.locator(".ps-why").inner_text()
    assert "Moved from" in sheet.locator(".ps-history").inner_text()


def test_a_published_post_offers_no_time_controls(browser, plan):
    item = plan.rig.item("fb")
    item["status"] = "published"
    _pq.save(plan.sp, item)
    pg, errors = _view(browser, plan, "Calendar")
    pg.wait_for_selector(".cal-grid .cal-item")
    assert "is-locked" in _item(pg, plan, "fb").get_attribute("class") and _item(pg, plan, "fb").get_attribute("draggable") == "false"
    sheet = _sheet(pg, plan, "fb")
    assert sheet.locator("[data-act=change-time], [data-act=next-slot], [data-act=unschedule]").count() == 0
    assert "cannot be moved" in sheet.locator(".ps-actions").inner_text()
    pg.close()
    assert not errors


def test_drag_a_post_to_another_day_then_undo(cal, plan):
    _week(cal)
    _drag(cal, f'.cal-item[data-variant="{plan.rig.posts["review"][1]}"]', '.cal-day[data-date="2026-10-22"]')
    _until(cal, lambda: plan.rig.local("review") == "2026-10-22T09:00")                     # keeps its time of day
    assert plan.rig.item("review")["status"] == "awaiting_approval"
    toast = cal.locator(".toast", has_text="Moved")
    toast.wait_for()
    toast.get_by_role("button", name="Undo").click()
    _until(cal, lambda: plan.rig.local("review") == "2026-10-21T09:00")
    cal.locator(".toast", has_text="Undone").wait_for()
    cal.wait_for_function(f"() => document.querySelector('.cal-day[data-date=\"2026-10-21\"] .cal-item[data-variant=\"{plan.rig.posts['review'][1]}\"]')")
    # and onto the tray: off the calendar, with the approval kept
    _drag(cal, f'.cal-item[data-variant="{plan.rig.posts["approved"][1]}"]', ".cal-tray")
    _until(cal, lambda: plan.rig.local("approved") is None)
    assert plan.rig.item("approved")["status"] == "approved"


def test_drag_an_artifact_to_another_day_moves_its_due_date_as_the_person(cal, plan):
    board_bridge.install(cal, plan.srv, now=True)                     # in the app: the person's write, through its bridge
    _week(cal)
    _drag(cal, f'.cal-item.is-due[data-key="{plan.launch}"]', '.cal-day[data-date="2026-10-24"]')
    e = wait_log(plan.studio, lambda e: e["op"] == "artifact.updated" and (e.get("data") or {}).get("due") == "2026-10-24")
    assert e["actor"] == "human"
    cal.locator(".toast", has_text="due").wait_for()


def test_in_a_browser_a_board_write_is_claudes(browser, ws):
    """Final review I1: outside the app's bridge the page holds only the room token, which Claude holds too, so a hand edit in a
    browser is recorded as Claude's and a stage move waits on the board for the director to confirm or undo."""
    pg, errors = _open(browser, ws.srv)                               # no bridge
    pg.wait_for_selector(".ws-row[data-key]")
    pg.wait_for_function("() => window.__ws && window.__ws.live === 'live'")
    cell(pg, ws.ep13, "name").locator(".ws-name-text").click()
    pg.locator(".ws-edit").fill("Episode 13: the river")
    pg.locator(".ws-edit").press("Enter")
    e = wait_log(ws.p13, lambda e: e["op"] == "artifact.updated")
    assert e["actor"] == "claude"
    cell(pg, ws.ep13, "stage").click()
    pg.locator(".popover").get_by_role("menuitemradio", name="Draft").click()
    e = wait_log(ws.p13, lambda e: e["op"] == "stage.signed_off")
    assert e["actor"] == "claude" and e["data"]["relayed_by"] == "claude"
    row = next(r for p in ws.srv.workspace.board()["projects"] for r in p["artifacts"] if r["key"] == ws.ep13)
    assert row["waiting"] == "you" and [r["type"] for r in row["relayed"]] == ["sign_off"]
    pg.close()
    assert not errors


def test_the_brand_filter_narrows_the_calendar_and_is_remembered(cal, plan):
    cal.locator(".cal-filter").click()
    cal.get_by_role("menuitemradio", name="Loaf Co").click()
    cal.wait_for_function(f"() => !document.querySelector('.cal-item[data-variant=\"{plan.rig.posts['review'][1]}\"]')")
    assert _item(cal, plan, "loaf").count() == 1 and _item(cal, plan, "fb").count() == 0
    assert cal.locator(".cal-filter").inner_text() == "Loaf Co"
    cal.reload()
    cal.wait_for_selector(".cal-grid .cal-item")
    assert cal.locator(".cal-filter").inner_text() == "Loaf Co" and _item(cal, plan, "review").count() == 0
    assert cal.locator(f'.cal-item.is-due[data-key="{plan.launch}"]').count() == 1      # due dates are not a brand's


def test_filters_that_match_nothing_say_so_and_offer_to_clear(cal, plan):
    cal.locator(".ws-toolbar .search-field input").fill("nothing-matches-this")
    cal.wait_for_selector(".cal-empty")
    assert "Nothing matches these filters" in cal.locator(".cal-empty").inner_text()
    cal.locator(".cal-empty").get_by_role("button", name="Clear filters").click()
    cal.wait_for_selector(".cal-grid .cal-item")
    assert cal.locator(".ws-toolbar .search-field input").input_value() == ""


def test_the_calendar_says_so_when_the_studio_is_gone_and_offers_to_try_again(browser, plan):
    pg, errors = _view(browser, plan, "Main table")
    pg.wait_for_selector(".ws-row[data-key]")
    pg.route("**/api/studio/calendar*", lambda r: r.abort())
    pg.get_by_role("tab", name="Calendar", exact=True).click()
    pg.wait_for_selector(".cal-notice .notice-negative")
    assert "could not be read" in pg.locator(".cal-notice").inner_text()
    assert pg.locator(f'.cal-item.is-due[data-key="{plan.launch}"]').count() == 1           # the due dates still show
    pg.unroute("**/api/studio/calendar*")
    pg.locator(".cal-notice").get_by_role("button", name="Try again").click()
    pg.wait_for_selector(f'.cal-item[data-variant="{plan.rig.posts["review"][1]}"]')
    assert pg.locator(".cal-notice").is_hidden()
    pg.close()


def test_a_post_claude_adds_appears_without_a_reload(cal, plan):
    cal.wait_for_timeout(400)                                                                 # the first look at the change cursor
    pid, vid = plan.rig.add_post("Claude's idea: rye crackers for the market.", account="ig-loaf", at="2026-10-22T18:00:00Z")
    cal.wait_for_selector(f'.cal-day[data-date="2026-10-22"] .cal-item[data-variant="{vid}"]', timeout=8000)


def test_an_episode_project_shows_its_due_dates_not_the_studios_posts(browser, plan):
    _new(plan.ep, "art_short", "The van story", "short", "Draft", due="2026-10-20")
    pg, errors = _view(browser, plan, "Calendar", route=f"#/workspace/p/{plan.ep.project_id}")
    pg.wait_for_selector(".cal-grid .cal-item.is-due")
    assert pg.locator(".cal-item.is-post").count() == 0 and pg.locator(".cal-tray").is_hidden()
    assert "Posts are planned in the studio" in pg.locator(".cal-sub").inner_text()
    pg.close()
    assert not errors


# ---------------------------------------------------------------------------------------------------- several posts at once
def _select(pg, world, names):
    pg.locator(".cal-selbtn").click()
    for n in names:
        _item(pg, world, n).first.click()
    pg.wait_for_selector(".cal-batch:not([hidden])")


def _batch(pg, label):
    pg.locator(".cal-batch .batch-act", has_text=label).click()


def test_select_several_and_shift_them_a_week_then_undo_the_batch(cal, plan):
    _week(cal)
    _select(cal, plan, ["review", "fb"])
    assert cal.locator(".cal-batch .batch-count").inner_text() == "2" and cal.locator(".cal-item.is-selected").count() == 2
    _batch(cal, "+1 week")
    _until(cal, lambda: plan.rig.local("review") == "2026-10-28T09:00" and plan.rig.local("fb") == "2026-10-28T09:00")
    assert plan.rig.item("review")["status"] == "awaiting_approval"                          # bulk never approves
    toast = cal.locator(".toast", has_text="Shifted 2 posts")
    toast.wait_for()
    cal.wait_for_selector(".cal-batch[hidden]", state="attached")                             # what moved leaves the selection
    toast.get_by_role("button", name="Undo").click()
    _until(cal, lambda: plan.rig.local("review") == "2026-10-21T09:00" and plan.rig.local("fb") == "2026-10-21T09:00")


def test_a_refused_member_stays_selected_with_its_reason_and_the_rest_move(browser, plan):
    plan.rig.add_post("Loaf Co: a taken slot.", account="ig-loaf", at="2026-10-30T15:00:00Z", sync=False)
    pg, errors = _view(browser, plan, "Calendar")
    pg.wait_for_selector(".cal-grid .cal-item")
    _week(pg)
    _select(pg, plan, ["loaf2", "review"])
    _batch(pg, "+1 week")
    pg.wait_for_selector(".cal-batch .cal-refused")
    assert plan.rig.local("review") == "2026-10-28T09:00" and plan.rig.local("loaf2") == "2026-10-23T09:00"
    refused = pg.locator(".cal-batch .cal-refused").inner_text()
    assert "taken" in refused and "Loaf Co: Friday is rye day" in refused
    assert pg.locator(".cal-batch .batch-count").inner_text() == "1"                         # only the refused one stays selected
    pg.close()
    assert not errors


def test_bulk_unscheduled_keeps_an_approval_and_undo_puts_the_time_back(cal, plan):
    before = plan.rig.item("approved")
    _week(cal)
    _select(cal, plan, ["approved"])
    _batch(cal, "Unscheduled")
    _until(cal, lambda: plan.rig.local("approved") is None)
    assert plan.rig.item("approved")["status"] == "approved" and plan.rig.item("approved")["approvals"] == before["approvals"]
    cal.locator(".toast", has_text="Unscheduled").get_by_role("button", name="Undo").click()
    _until(cal, lambda: plan.rig.local("approved") == "2026-10-23T09:00")


def test_bulk_next_free_slots_places_the_tray(cal, plan):
    _select(cal, plan, ["draft", "carousel"])
    _batch(cal, "Next free slots")
    _until(cal, lambda: (plan.rig.local("draft") or "") > "2026-10-19" and (plan.rig.local("carousel") or "") > "2026-10-19")
    assert plan.rig.local("draft") != plan.rig.local("carousel")                              # each its own slot


def test_move_to_account_only_for_unqueued_posts_of_the_same_platform(cal, plan):
    _week(cal)
    _select(cal, plan, ["draft", "approved"])
    _batch(cal, "Move to account")
    cal.get_by_role("menuitem", name="@ig-loaf (Instagram)").click()
    cal.wait_for_selector(".cal-batch .cal-refused")
    assert plan.rig.variant("draft")["account"] == "ig-loaf"                                  # the draft moved
    assert plan.rig.variant("approved")["account"] == "ig-main"                               # the approved one did not
    assert "approval is bound" in cal.locator(".cal-batch .cal-refused").inner_text()
    assert plan.rig.item("approved")["status"] == "approved"
    cal.locator(".toast", has_text="Moved").get_by_role("button", name="Undo").click()
    _until(cal, lambda: plan.rig.variant("draft")["account"] == "ig-main")


def test_x_selects_r_changes_time_n_takes_the_next_slot_and_escape_clears(cal, plan):
    _week(cal)
    _item(cal, plan, "review").focus()
    cal.keyboard.press("x")
    cal.wait_for_selector(".cal-batch:not([hidden])")
    assert cal.locator(".cal-batch .batch-count").inner_text() == "1"
    cal.keyboard.press("r")
    cal.wait_for_selector(".ct-pop #ct-date")
    cal.keyboard.press("Escape")
    cal.wait_for_selector(".ct-pop", state="detached")
    _item(cal, plan, "review").focus()
    cal.keyboard.press("Escape")                                                              # nothing open: Esc clears the selection
    cal.wait_for_selector(".cal-batch[hidden]", state="attached")
    assert cal.locator(".cal-item.is-selected").count() == 0
    _item(cal, plan, "fb").focus()
    cal.keyboard.press("n")
    _until(cal, lambda: plan.rig.local("fb") == "2026-10-23T09:00")
    assert cal.locator(".ws-add-input:focus").count() == 0                                    # n did not start a new artifact


def test_published_posts_cannot_be_selected(browser, plan):
    item = plan.rig.item("fb")
    item["status"] = "published"
    _pq.save(plan.sp, item)
    pg, errors = _view(browser, plan, "Calendar")
    pg.wait_for_selector(".cal-grid .cal-item")
    _week(pg)
    pg.locator(".cal-selbtn").click()
    _item(pg, plan, "fb").click()
    pg.locator(".toast", has_text="Published posts cannot be moved").wait_for()
    assert pg.locator(".cal-batch").is_hidden() and pg.locator(".cal-item.is-selected").count() == 0
    pg.close()
    assert not errors


# ---------------------------------------------------------------------------------------------------- Claude's suggestions, campaigns
def _propose(plan, name, local, why="Monday 09:00 is free on ig-main"):
    from ave.schedule import proposals
    pid, vid = plan.rig.posts[name]
    return proposals.create(plan.sp, "claude", [{"post": pid, "variant": vid, "to": {"local": local, "tz": "America/Denver"}, "why": why}],
                            now=plan.rig.w.clock.now())


def test_claudes_suggested_moves_show_as_ghosts_and_accept_applies_them(browser, plan):
    _propose(plan, "draft", "2026-10-26T09:00")
    pg, errors = _view(browser, plan, "Calendar")
    pg.wait_for_selector(".cal-item.is-ghost")
    ghost = pg.locator('.cal-day[data-date="2026-10-26"] .cal-item.is-ghost')
    assert "Weekly special" in ghost.inner_text()                                          # Month shows the name; the time is in its label
    assert "at 09:00" in ghost.get_by_role("button", name=re.compile("Accept Claude's suggestion")).get_attribute("aria-label")
    side = pg.locator(".cal-suggest")
    assert "Claude suggests" in side.inner_text() and "Monday 09:00 is free" in side.inner_text()
    assert plan.rig.local("draft") is None                                                    # nothing moves until a person accepts
    ghost.get_by_role("button", name=re.compile("Accept Claude's suggestion")).click()
    _until(pg, lambda: plan.rig.local("draft") == "2026-10-26T09:00")
    pg.locator(".toast", has_text="Applied 1 suggested move").wait_for()
    pg.wait_for_selector(".cal-item.is-ghost", state="detached")
    pg.close()
    assert not errors


def test_a_suggestion_is_hidden_while_a_filter_hides_its_post(browser, plan):
    _propose(plan, "draft", "2026-10-26T09:00")
    pg, errors = _view(browser, plan, "Calendar")
    pg.wait_for_selector(".cal-item.is-ghost")
    pg.locator(".cal-filter").click()
    pg.get_by_role("menuitemradio", name="Loaf Co").click()
    pg.wait_for_selector(".cal-item.is-ghost", state="detached")
    assert "1 suggestion is hidden" in pg.locator(".cal-tray").inner_text()
    pg.close()
    assert not errors


def test_the_campaign_filter_narrows_to_one_campaign(cal, plan):
    cal.locator(".cal-camp").click()
    cal.get_by_role("menuitemradio", name="Spring launch").click()
    cal.wait_for_function(f"() => !document.querySelector('.cal-item[data-variant=\"{plan.rig.posts['review'][1]}\"]')")
    assert _item(cal, plan, "loaf").count() == 1 and _item(cal, plan, "loaf2").count() == 0
    assert cal.locator(".cal-camp").inner_text() == "Spring launch" and "is-on" in cal.locator(".cal-camp").get_attribute("class")


def test_a_stale_approval_is_explained_in_the_sheet(browser, plan):
    item = plan.rig.item("approved")
    item["caption"] = item["caption"] + " (edited behind the studio's back)"
    _pq.save(plan.sp, item)
    pg, errors = _view(browser, plan, "Calendar")
    pg.wait_for_selector(".cal-grid .cal-item")
    sheet = _sheet(pg, plan, "approved")
    pg.wait_for_selector(".post-sheet .flag-chip.f-approval_stale")
    text = sheet.locator(".ps-why").inner_text()
    assert "Approval stale" in text and "will not go out until you approve it again" in text
    pg.close()
    assert not errors


# ---------------------------------------------------------------------------------------------------- live posts: the stream, not a poll
def test_the_watcher_publishes_a_posts_event_when_the_change_log_grows(plan):
    seen = []
    ws = ws_mod.Workspace(plan.sp.root, sp=plan.sp, publish=lambda t, d: seen.append((t, d)))
    ws.start()
    try:
        assert ws.look_posts() is None                                                        # nothing new since it started
        plan.rig.add_post("Claude's new post.", account="ig-main", at="2026-10-22T18:00:00Z")
        end = time.time() + 5
        while time.time() < end and not any(t == "posts" for t, _ in seen):
            time.sleep(0.05)
        kinds = [d for t, d in seen if t == "posts"]
        assert kinds and kinds[-1]["cursor"].startswith("ev:")
    finally:
        ws.stop()


def test_the_calendar_reads_no_change_feed_of_its_own(cal, plan):
    reads = []
    cal.on("request", lambda r: reads.append(r.url) if "/api/studio/" in r.url else None)
    cal.wait_for_timeout(2500)
    assert reads == [], reads                                                                 # idle: no summary or cursor poll


def test_a_post_added_while_the_first_read_is_in_flight_still_arrives(browser, plan):
    held = []

    def hold(route):
        if not held:
            held.append((route, route.fetch()))                                              # the read as it was, answered later
        else:
            route.continue_()
    pg, errors = _view(browser, plan, "Main table")
    pg.wait_for_selector(".ws-row[data-key]")
    pg.route("**/api/studio/calendar*", hold)
    pg.get_by_role("tab", name="Calendar", exact=True).click()
    _until(pg, lambda: bool(held))
    pid, vid = plan.rig.add_post("Claude, mid-read: rye crackers.", account="ig-loaf", at="2026-10-22T18:00:00Z")
    pg.wait_for_function("() => (window.__ws.posts || 0) > 0", timeout=8000)                 # the stream carried the change
    route, stale = held[0]
    route.fulfill(response=stale)                                                             # the first read lands after it, without the post
    pg.wait_for_selector(f'.cal-item[data-variant="{vid}"]', timeout=8000)
    pg.close()
    assert not errors


# ---------------------------------------------------------------------------------------------------- previews
def test_cards_show_every_channel_and_open_the_phone_preview(browser, plan):
    pg, errors = _view(browser, plan, "Previews")
    pg.wait_for_selector(".pvw-card[data-variant]")
    assert pg.locator(".pvw-card[data-variant]").count() == 7                     # six one-channel posts and the carousel, never the idea
    first = pg.locator(f'.pvw-card[data-variant="{plan.rig.posts["review"][1]}"]')
    assert "Sourdough day three" in first.inner_text() and "In review" in first.inner_text() and "@ig-main" in first.inner_text()
    first.click()
    pg.wait_for_selector(".post-sheet .ps-phone .pv img")
    pg.wait_for_function("() => document.querySelector('.post-sheet .ps-phone .pv img').naturalWidth > 0")
    assert pg.evaluate("document.documentElement.scrollWidth <= innerWidth")
    pg.close()
    assert not errors


def test_the_feed_shows_the_planned_profile_grid_and_a_tile_opens_its_post(browser, plan):
    from ave.studio import store
    pg, errors = _view(browser, plan, "Previews")
    pg.locator(".pvw .seg-opt[data-mode=feed]").click()
    grid = pg.get_by_role("region", name="Planned Instagram profile")
    grid.wait_for()
    pg.locator(".pvw .btn-tool").click()                                                     # whose feed
    pg.get_by_role("menuitemradio", name="@ig-main").click()
    pg.wait_for_function("() => document.querySelector('.pvw-handle').innerText === '@ig-main'")
    pg.wait_for_selector(".pvw-tile")
    assert pg.locator(".pvw-tile").count() >= 3
    pid, vid = plan.rig.posts["review"]
    pg.locator(f'.pvw-tile[data-variant="{vid}"]').click()
    img = pg.locator(".post-sheet .ps-phone .pv img").first
    img.wait_for()
    asset = store.load_post(plan.sp, pid)["media"][0]["asset"]
    sha = store.read_assets(plan.sp)[asset]["sha256"]
    assert sha in (img.get_attribute("src") or "") or asset in (img.get_attribute("src") or "")
    pg.close()
    assert not errors


def test_a_carousel_steps_through_its_slides_in_export_order(browser, plan):
    from ave.studio import manual_export, store
    pid, vid = plan.rig.posts["carousel"]
    manifest = manual_export.describe(plan.sp, pid, vid, draft=True)["manifest"]
    assets = store.read_assets(plan.sp)
    by_sha = {a["sha256"]: a for a in assets.values()}
    pg, errors = _view(browser, plan, "Previews")
    pg.locator(f'.pvw-card[data-variant="{vid}"]').click()
    pg.wait_for_selector(".post-sheet .ps-phone .pv img")

    def shown():
        src = pg.locator(".post-sheet .ps-phone .pv img").first.get_attribute("src") or ""
        return next(sha for sha, a in by_sha.items() if sha in src or a["id"] in src)
    assert shown() == manifest["media"][0]["sha256"]
    assert pg.locator(".ps-slidepos").inner_text() == "Slide 1 of 2"
    pg.get_by_role("button", name="Next slide", exact=True).click()
    pg.wait_for_function("() => document.querySelector('.ps-slidepos').innerText === 'Slide 2 of 2'")
    assert shown() == manifest["media"][1]["sha256"]
    assert pg.evaluate("document.activeElement.getAttribute('aria-label')") == "Previous slide"
    assert manifest["caption"].split("\n")[0][:40] in pg.locator(".post-sheet .ps-phone").inner_text()
    pg.close()
    assert not errors


def test_download_an_unreviewed_draft_gives_the_original_bytes_and_keeps_each_package(browser, plan, tmp_path):
    import hashlib
    import zipfile

    from ave.studio import store
    pg, errors = _view(browser, plan, "Previews")
    pg.locator(f'.pvw-card[data-variant="{plan.rig.posts["review"][1]}"]').click()
    pg.wait_for_selector(".post-sheet .ps-deliver")
    pg.locator(".post-sheet [data-act=download]").click()
    history = pg.get_by_role("region", name="Package history")
    link = history.get_by_role("link", name="Download full-resolution package")
    link.first.wait_for()
    assert "Nothing has been posted" in pg.locator(".post-sheet .ps-delstatus").inner_text()
    assert "Exported · unreviewed draft" in history.inner_text()
    assert history.get_by_role("button", name="I posted this").count() == 0                # only a reviewed package can be reported
    with pg.expect_download() as dl:
        link.first.click()
    path = tmp_path / "package.zip"
    dl.value.save_as(path)
    with zipfile.ZipFile(path) as z:
        manifest = json.loads(z.read("manifest.json"))
        assert manifest["reviewed"] is False and manifest["media"]
        for m in manifest["media"]:
            src = plan.sp.root / store.read_assets(plan.sp)[m["asset"]]["file"]
            assert z.read(m["filename"]) == src.read_bytes() and hashlib.sha256(z.read(m["filename"])).hexdigest() == m["sha256"]
        assert "caption.txt" in z.namelist()
    first = link.first.get_attribute("href")
    pg.locator(".post-sheet [data-act=download]").click()                                   # a second package is added, the first kept
    pg.wait_for_function("() => document.querySelectorAll('.post-sheet .ps-pack').length === 2")
    links = history.get_by_role("link", name="Download full-resolution package").evaluate_all("ns => ns.map(n => n.getAttribute('href'))")
    assert len(set(links)) == 2 and first in links
    pg.close()
    assert not errors


POSTED_STUB = """ window.__reports = []; Object.defineProperty(window, 'aveNative', {configurable: true, value: {
  recordManualPublication: (r) => { window.__reports.push(r); return Promise.resolve({by: r.by}); } }});"""


def _reviewed_package(pg):
    pg.locator(".post-sheet [data-act=export-reviewed]").click()
    pg.locator(".post-sheet .ps-pack", has_text="Exported · ready to post").wait_for()
    pg.get_by_role("button", name="I posted this").click()
    pg.wait_for_selector(".post-sheet .ps-reportform")


def test_a_reviewed_package_can_be_reported_as_posted_only_through_the_app(browser, plan):
    pg, errors = _view(browser, plan, "Previews", init=POSTED_STUB)
    pg.locator(f'.pvw-card[data-variant="{plan.rig.posts["approved"][1]}"]').click()
    pg.wait_for_selector(".post-sheet .ps-deliver")
    _reviewed_package(pg)
    form = pg.locator(".post-sheet .ps-reportform")
    form.get_by_label("Published URL").fill("not a link")
    form.get_by_role("button", name="Record publication").click()
    assert "Paste the link" in form.locator(".ct-err").inner_text() and pg.evaluate("window.__reports.length") == 0
    form.get_by_label("Published URL").fill("https://www.instagram.com/p/abc123/")
    form.get_by_label("Publication time").fill("2026-10-23 09:05")
    form.get_by_label("Reported by").fill("Maren")
    form.get_by_role("button", name="Record publication").click()
    pg.locator(".post-sheet .ps-delstatus", has_text="Published · reported by Maren").wait_for()
    sent = pg.evaluate("window.__reports[0]")
    pid, vid = plan.rig.posts["approved"]
    assert sent["post_id"] == pid and sent["variant_id"] == vid and sent["package_id"] and sent["reviewed_payload_hash"]
    assert sent["posted_url"] == "https://www.instagram.com/p/abc123/" and sent["by"] == "Maren" and sent["request_id"]
    pg.close()
    assert not errors


def test_without_the_app_the_publication_report_says_why_and_sends_nothing(browser, plan):
    pg, errors = _view(browser, plan, "Previews")
    pg.locator(f'.pvw-card[data-variant="{plan.rig.posts["approved"][1]}"]').click()
    pg.wait_for_selector(".post-sheet .ps-deliver")
    _reviewed_package(pg)
    form = pg.locator(".post-sheet .ps-reportform")
    assert form.get_by_role("button", name="Record publication").is_disabled()
    assert "needs the AVE Studio app" in form.inner_text()
    pg.close()
    assert not errors


# ---------------------------------------------------------------------------------------------------- sources
DEVICE_IDS = ("dev-imani-7f3a", "dev-sam-7f3a")


def _src(browser, plan, **kw):
    pg, errors = _view(browser, plan, "Sources", **kw)
    pg.wait_for_selector(".src-row[data-key]")
    return pg, errors


def _src_row(pg, name):
    return pg.locator(".src-row[data-key]", has=pg.locator(".src-name", has_text=name)).first


def test_sources_say_where_each_file_is_in_plain_words(browser, plan):
    pg, errors = _src(browser, plan)
    ep = pg.locator(".src-group", has=pg.locator(".src-gtitle", has_text="Episode 12"))
    assert "With Imani and Sam" in ep.locator(".src-ghead").inner_text()
    assert _src_row(pg, "A001.mov").locator(".where").inner_text() == "Shared with Sam · Proxies only on Imani's computer"
    assert _src_row(pg, "A002.mov").locator(".where").inner_text() == "Shared with Sam · Not on Imani's computer: no proxy yet"
    assert _src_row(pg, "bed.wav").locator(".where").inner_text() == "Shared with Imani and Sam"
    assert ep.locator(".src-name", has_text="render.mp4").count() == 0                     # renders and caches are not sources
    assert ep.locator(".src-name", has_text="._A001").count() == 0 and ep.locator(".src-row[data-key]").count() == 3
    # the studio's library: licences, and a library file is shared with everyone (permanent zone)
    studio = pg.locator(".src-group", has=pg.locator(".src-gtitle", has_text="Acme Bakery"))
    lib = studio.locator(".src-row[data-type=asset]")
    from ave.studio import store
    assert lib.count() == len(store.read_assets(plan.sp))
    loose = _src_row(pg, "A flour sack on a shelf.")
    assert loose.locator(".lic").inner_text() == "No licence" and "is-missing" in loose.locator(".lic").get_attribute("class")
    assert loose.locator(".where").inner_text() == "Shared with Imani and Sam"
    assert studio.locator(".lic.is-ok", has_text="Owned").count() >= 6
    text = pg.locator(".src").inner_text()
    for dev in DEVICE_IDS:
        assert dev not in text                                                                # names, never ids
    assert plan.loose["id"] not in text and "library/assets" not in text
    pg.close()
    assert not errors


def test_a_file_opens_its_details_with_each_persons_copy(browser, plan):
    pg, errors = _src(browser, plan)
    _src_row(pg, "A001.mov").locator(".src-name").click()
    pop = pg.locator(".popover .src-pop")
    pop.wait_for()
    t = pop.inner_text()
    assert "Imani" in t and "has the review proxy" in t and "Sam" in t and "has it" in t and "Review proxy" in t and "Made" in t
    pg.keyboard.press("Escape")
    _src_row(pg, "A flour sack on a shelf.").locator(".src-name").click()
    t = pg.locator(".popover .src-pop").inner_text()
    assert "No licence entry covers this file" in t and "Unused" in t
    pg.close()
    assert not errors


def test_sources_filter_by_kind_place_and_words(browser, plan):
    pg, errors = _src(browser, plan)
    pg.locator(".src .btn-tool", has_text="Any kind").click()
    pg.get_by_role("menuitemradio", name="Audio").click()
    pg.wait_for_function("() => document.querySelectorAll('.src-row[data-key]').length === 1")
    assert _src_row(pg, "bed.wav").count() == 1
    pg.locator(".src .btn-tool", has_text="Audio").click()
    pg.get_by_role("menuitemradio", name="Any kind").click()
    pg.locator(".src .btn-tool", has_text="Anywhere").click()
    pg.get_by_role("menuitemradio", name="Not on every computer").click()
    pg.wait_for_function("() => document.querySelectorAll('.src-row[data-key]').length === 3")
    pg.locator(".src .btn-tool", has_text="Not on every computer").click()
    pg.get_by_role("menuitemradio", name="Anywhere").click()
    pg.locator(".ws-toolbar .search-field input").fill("flour")
    pg.wait_for_function("() => document.querySelectorAll('.src-row[data-key]').length === 1")
    pg.locator(".ws-toolbar .search-field input").fill("nothing-matches-this")
    pg.wait_for_selector(".src .ws-empty")
    assert "Nothing matches these filters" in pg.locator(".src .ws-empty").inner_text()
    pg.locator(".src .ws-empty").get_by_role("button", name="Clear filters").click()
    pg.wait_for_selector(".src-row[data-key]")
    pg.close()
    assert not errors


IMPORT_STUB = """ window.__asked = 0; Object.defineProperty(window, 'aveNative', {configurable: true, value: {
  importLibrary: () => { window.__asked += 1; return new Promise((ok) => setTimeout(() => ok({added: [{name: 'quote-card.png',
    asset: {id: 'a_quote'}, duplicate: false}]}), 50)); } }});"""


def test_add_files_goes_through_the_apps_own_panel(browser, plan, tmp_path):
    from studio_helpers import make_png
    pg, errors = _src(browser, plan, init=IMPORT_STUB)
    assert pg.locator("input[type=file]").count() == 0                                       # never a web file input
    before = pg.locator(".src-row[data-type=asset]").count()
    from ave import library
    library.add_file(plan.sp, make_png(tmp_path / "quote-card.png", color=(90, 40, 140)), source={"type": "own"}, by="test", alt="A quote card.")
    pg.locator(".src").get_by_role("button", name="Add files").click()
    pg.locator(".toast", has_text="Added quote-card.png to the library").wait_for()
    assert pg.evaluate("window.__asked") == 1
    pg.wait_for_function(f"() => document.querySelectorAll('.src-row[data-type=asset]').length === {before + 1}")
    pg.close()
    assert not errors


def test_add_files_without_the_app_says_why(browser, plan):
    pg, errors = _src(browser, plan)
    add = pg.locator(".src").get_by_role("button", name="Add files")
    assert add.is_disabled() and "needs the AVE Studio app" in add.get_attribute("data-tip")
    assert pg.locator("input[type=file]").count() == 0
    pg.close()
    assert not errors


def test_the_sources_load_error_is_designed_and_retries(browser, plan):
    pg, errors = _view(browser, plan, "Main table")
    pg.wait_for_selector(".ws-row[data-key]")
    pg.route("**/api/workspace/sources", lambda r: r.abort())
    pg.get_by_role("tab", name="Sources", exact=True).click()
    pg.wait_for_selector(".src .ws-error")
    assert "could not load" in pg.locator(".src .ws-error").inner_text()
    pg.unroute("**/api/workspace/sources")
    pg.locator(".src .ws-error").get_by_role("button", name="Try again").click()
    pg.wait_for_selector(".src-row[data-key]")
    pg.close()


def test_views_are_keyboard_reachable_and_remembered(browser, plan):
    pg, errors = _view(browser, plan, "Main table")
    pg.wait_for_selector(".ws-row[data-key]")
    pg.locator("body").click(position={"x": 5, "y": 5})
    pg.keyboard.press("3")
    pg.wait_for_selector(".cal-grid")
    pg.keyboard.press("4")
    pg.wait_for_selector(".pvw-card")
    pg.keyboard.press("5")
    pg.wait_for_selector(".src-row[data-key]")
    pg.reload()
    pg.wait_for_selector(".src-row[data-key]")                                                # the view is remembered
    assert pg.get_by_role("tab", name="Sources", exact=True).get_attribute("aria-selected") == "true"
    for tool in ("Person", "Filter", "Sort", "Hide", "Group by"):                            # the board's own tools step aside
        assert pg.locator(".ws-toolbar").get_by_role("button", name=tool, exact=True).is_hidden()
    pg.close()
    assert not errors


def test_a_computer_that_keeps_proxies_sees_its_footage_through_them(browser, plan):
    pg, errors = _src(browser, plan)
    group = pg.locator(".src-group", has=pg.locator(".src-gtitle", has_text="Episode 13"))
    row = group.locator(".src-row[data-key]", has=pg.locator(".src-name", has_text="B001.mov"))
    assert row.locator(".src-cell.c-1").inner_text() == "Review proxy"
    assert row.locator(".where").inner_text() == "Proxy on this computer; original on Maren's computer"
    assert "With Maren" in group.locator(".src-ghead").inner_text()
    pg.close()
    assert not errors


def test_sources_filter_the_library_by_licence_and_use(browser, plan):
    pg, errors = _src(browser, plan)
    pg.locator(".src .btn-tool", has_text="Any licence").click()
    pg.get_by_role("menuitemradio", name="Licence missing").click()
    pg.wait_for_function("() => document.querySelectorAll('.src-row[data-key]').length === 1")
    assert _src_row(pg, "A flour sack on a shelf.").count() == 1
    pg.locator(".src .btn-tool", has_text="Licence missing").click()
    pg.get_by_role("menuitemradio", name="Any licence state").click()
    pg.locator(".src .btn-tool", has_text="Any licence").click()
    pg.get_by_role("menuitemradio", name="Unused only").click()
    pg.wait_for_function("() => document.querySelectorAll('.src-row[data-key]').length >= 1")
    names = pg.locator(".src-row[data-key] .src-name").all_inner_texts()
    assert "A flour sack on a shelf." in names and "The first loaf, whole." not in names     # the carousel uses the loaves
    pg.close()
    assert not errors


def test_a_cut_library_says_so(browser, plan):
    pg, errors = _view(browser, plan, "Main table")
    pg.wait_for_selector(".ws-row[data-key]")

    def cut(route):
        data = json.loads(route.fetch().text())
        for p in data["projects"]:
            if p["library"]:
                p["library"], p["library_total"] = p["library"][:3], 99
        route.fulfill(status=200, content_type="application/json", body=json.dumps(data))
    pg.route("**/api/workspace/sources", cut)
    pg.get_by_role("tab", name="Sources", exact=True).click()
    pg.locator(".src-cut", has_text="Showing 3 of 99 library files").wait_for()
    pg.close()


CANDIDATES = {"search": {"id": "bs_1", "candidates": [
    {"provider": "pexels", "asset_id": "2284", "kind": "photo", "w": 4000, "h": 6000, "title": "Bread crust on a wooden board", "author": "A. Baker",
     "attribution_required": False, "attribution_text": "Photo by A. Baker on Pexels", "critic": {"verdict": "ok"},
     "preview_url": "https://images.pexels.com/photos/2284/preview.jpeg", "page_url": "https://www.pexels.com/photo/2284/"},
    {"provider": "pixabay", "asset_id": "88", "kind": "photo", "w": 4000, "h": 2667, "title": "Kitchen", "author": "Josch13",
     "attribution_required": True, "attribution_text": "Image by Josch13 from Pixabay", "critic": {"verdict": "warn", "problems": ["looks staged"]}}]}}


def test_find_broll_shows_licences_and_downloads_only_on_a_named_pick(browser, plan):
    pg, errors = _src(browser, plan)
    asked, external = [], []
    pg.on("request", lambda r: external.append(r.url) if "pexels.com" in r.url or "pixabay.com" in r.url else None)
    pg.route("**/api/studio/broll/search", lambda r: (asked.append(json.loads(r.request.post_data)),
                                                       r.fulfill(status=200, content_type="application/json", body=json.dumps(CANDIDATES))))
    picks = []
    pg.route("**/api/studio/broll/pick", lambda r: (picks.append(json.loads(r.request.post_data)),
                                                     r.fulfill(status=200, content_type="application/json", body=json.dumps({"asset": {"id": "a_br000002284"}, "license_entry": "abcdef0123"}))))
    field = pg.get_by_label("Find B-roll", exact=True)
    field.fill("bread crust")
    field.press("Enter")
    card = pg.locator('.src-cand[data-cand="pexels:2284"]')
    card.wait_for()
    t = card.inner_text()
    assert "Pexels licence" in t and "Bread crust on a wooden board by A. Baker" in t and "Credit line: Photo by A. Baker on Pexels" in t and "Authenticity check: ok" in t
    flagged = pg.locator('.src-cand[data-cand="pixabay:88"]')
    assert "Attribution required" in flagged.inner_text() and "Authenticity check: warn, looks staged" in flagged.inner_text()
    assert asked[0]["query"] == "bread crust" and set(asked[0]["providers"]) == {"pexels", "local"}       # stock and the library
    assert card.locator("img").count() == 0 and not external                                 # the provider's preview is never loaded
    card.get_by_role("button", name="Download and add").click()
    assert "named person approves every download" in pg.locator(".src-brollhost").inner_text() and not picks
    pg.get_by_label("Who is approving the download").fill("maren")
    pg.locator('.src-cand[data-cand="pexels:2284"]').get_by_role("button", name="Download and add").click()
    pg.locator(".toast", has_text="approved by maren").wait_for()
    assert picks == [{"search": "bs_1", "candidate": "pexels:2284", "by": "maren"}]
    pg.close()
    assert not errors


# ---------------------------------------------------------------------------------------------------- ideas, as board artifacts
def _ideas(browser, plan):
    pg, errors = _view(browser, plan, "Main table")
    pg.wait_for_selector(".ideas-grp .idea")
    return pg, errors


def test_ideas_are_a_group_of_idea_artifacts_with_their_reason_and_confidence(browser, plan):
    pg, errors = _ideas(browser, plan)
    grp = pg.locator(".ideas-grp")
    row = grp.locator(".idea").first
    assert row.get_attribute("data-kind") == "idea"
    t = row.inner_text()
    assert "Behind the scenes" in t and "Asked about twice this month" in t and "60% sure" in t
    assert "1 idea" in grp.locator(".grp-count").inner_text()
    pg.close()
    assert not errors


def test_turn_an_idea_into_a_draft_and_undo(browser, plan):
    from ave.studio import store
    pid, _ = plan.rig.posts["idea"]
    pg, errors = _ideas(browser, plan)
    pg.locator(".ideas-grp [data-act=promote]").click()
    pg.locator(".toast", has_text="Turned into a draft").wait_for()
    post = store.load_post(plan.sp, pid)
    assert post["stage"] == "draft" and post["copy"]["base"].startswith("Behind the scenes")
    pg.wait_for_selector(".ideas-grp .idea", state="detached")
    pg.locator(".toast", has_text="Turned into a draft").get_by_role("button", name="Undo").click()
    pg.wait_for_selector(".ideas-grp .idea")
    post = store.load_post(plan.sp, pid)
    assert post["stage"] == "idea" and post["copy"]["base"] == ""
    pg.close()
    assert not errors


def test_set_an_idea_aside_and_undo_it(browser, plan):
    from ave.studio import store
    pid, _ = plan.rig.posts["idea"]
    pg, errors = _ideas(browser, plan)
    pg.locator(".ideas-grp [data-act=dismiss]").click()
    pg.wait_for_selector(".ideas-grp .idea-empty")
    assert store.load_post(plan.sp, pid)["stage"] == "dropped"
    pg.locator(".toast", has_text="Set aside").get_by_role("button", name="Undo").click()
    pg.wait_for_selector(".ideas-grp .idea")
    assert store.load_post(plan.sp, pid)["stage"] == "idea"
    pg.close()
    assert not errors


def test_an_idea_with_no_brand_can_be_given_one(browser, plan):
    from ave.studio import store
    pid, _ = plan.rig.posts["idea"]
    pg, errors = _ideas(browser, plan)
    pg.locator(".ideas-grp").get_by_role("button", name="Give it a brand").click()
    pg.get_by_role("menuitem", name="Loaf Co").click()
    pg.locator(".ideas-grp .idea-brand", has_text="Loaf Co").wait_for()
    assert store.load_post(plan.sp, pid)["brand"] == "Loaf Co"
    pg.close()
    assert not errors


def test_add_an_idea_by_hand(browser, plan):
    from ave.studio import store
    pg, errors = _ideas(browser, plan)
    add = pg.get_by_label("Add an idea")
    add.fill("A loaf a day for a week")
    add.press("Enter")
    pg.locator(".ideas-grp .idea", has_text="A loaf a day for a week").wait_for()
    assert any((store.load_post(plan.sp, f.stem).get("idea") or {}).get("text") == "A loaf a day for a week" for f in plan.sp.posts_dir.glob("*.json"))
    pg.close()
    assert not errors


def test_the_empty_ideas_group_says_how_ideas_arrive(browser, plan):
    from ave.studio import store
    from ave.studio.events import AGENT
    pid, _ = plan.rig.posts["idea"]
    plan.rig.w.studio.edit_post(pid, {"stage": "dropped"}, expected_rev=store.load_post(plan.sp, pid)["rev"], actor=AGENT)
    pg, errors = _view(browser, plan, "Main table")
    pg.wait_for_selector(".ideas-grp .idea-empty")
    t = pg.locator(".ideas-grp .idea-empty").inner_text()
    assert "No ideas waiting" in t and "ave studio ingest idea" in t
    pg.close()
    assert not errors


def test_an_idea_claude_files_appears_without_a_reload(browser, plan):
    from ave.studio.events import AGENT
    pg, errors = _ideas(browser, plan)
    _retry(lambda: plan.rig.w.studio.create_post(idea="Claude's idea: the oven at dawn.", actor=AGENT, reason="People like mornings", confidence=0.5))
    pg.locator(".ideas-grp .idea", has_text="the oven at dawn").wait_for(timeout=8000)
    pg.close()
    assert not errors


def test_a_project_saved_by_a_newer_ave_shows_update_needed_and_is_read_only(browser, ws, monkeypatch):
    import ave
    with monkeypatch.context() as m:                       # a teammate on a newer AVE writes to Episode 13
        m.setattr(ave, "__version__", "99.0.0")
        m.setattr(__import__("sys"), "frozen", True, raising=False)      # a release build stamps its version
        ws.p13.log.append("note.added", artifact="art_ep", data={"text": "from the newer app"})
    pg, errors = _open(browser, ws.srv)
    pg.wait_for_selector(".ws-row[data-key]")
    banner = pg.locator(".notice.notice-update")
    banner.wait_for()
    assert "Update needed" in banner.inner_text() and "Episode 13" in banner.inner_text()
    assert "This project was saved by a newer AVE (99.0.0). Update to edit it." in banner.inner_text()
    assert row(pg, ws.ep13).get_attribute("aria-readonly") == "true"
    assert row(pg, ws.ep12).get_attribute("aria-readonly") is None
    before = len(_events(ws.p13))
    cell(pg, ws.ep13, "stage").click()                     # editing controls do nothing on a read-only project
    pg.wait_for_timeout(200)
    assert not pg.locator("[role=menu]:visible, [role=listbox]:visible, .ws-edit:visible").count()
    cell(pg, ws.ep12, "stage").click()                     # while the other project still edits
    pg.locator("[role=menu]:visible, [role=listbox]:visible, .ws-edit:visible").first.wait_for()
    pg.keyboard.press("Escape")
    # and the server refuses a write all the same
    status = pg.evaluate("""async (key) => { const r = await fetch('/api/workspace/artifact/' + encodeURIComponent(key), {method: 'PATCH', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({fields: {owner: 'Maren'}})}); return [r.status, await r.json()]; }""", ws.ep13)
    assert status[0] == 409 and "newer AVE" in status[1]["error"]
    assert len(_events(ws.p13)) == before
    pg.close()
    assert not errors
