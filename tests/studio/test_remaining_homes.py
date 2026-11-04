"""Task 24a: the old tools that had no home in the new app get one (campaigns, pasted text, feedback history, content and episode
publish sheets, clip nudge, trim clear, jobs). Browser tests drive the real Workspace and Room against a real studio on disk."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from ave.project import Project
from ave.studio import campaigns as C, store
from ave.studio.project import StudioProject
from ave.studio.service import open_studio

from .test_workspace_browser import (TEST_FLAG, _new, _serve, _stop, _sheet, _week, browser, cal, fast_watch,  # noqa: F401  (fixtures)
                                     plan)


@pytest.fixture
def cw(tmp_path):
    sp = StudioProject(tmp_path / "studio")
    sp.create(title="Acme Bakery", tz="UTC")
    studio = open_studio(sp.root)
    post = studio.create_post(idea="Two loaves", copy={"base": "Two loaves, two ways."})
    other = studio.create_post(idea="Flour sack", copy={"base": "Where the flour comes from."})
    spring = C.new(sp, "Spring launch", goal="Open the bakery")
    proj = Project.create(sp.root, "Acme Bakery", person="Maren")
    _new(proj, "art_loaves", "Two loaves post", "post", "Draft", post=post["id"])
    _new(proj, "art_thumb", "Thumbnail", "thumbnail", "Plan")
    srv, t = _serve(sp.root)
    yield SimpleNamespace(srv=srv, sp=sp, proj=proj, post=post, other=other, spring=spring, loaves=f"{proj.project_id}:art_loaves")
    _stop(srv, t)


def _page(browser, srv, route="#/workspace"):
    pg = browser.new_page(viewport={"width": 1440, "height": 900})
    pg.add_init_script(TEST_FLAG)
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(srv.app_url() + route)
    pg.wait_for_selector(".ws-row[data-key]")
    return pg, errors


def _until(fn, timeout=6.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("never happened")


def _group_by_campaign(pg):
    pg.get_by_role("button", name="Group by").click()
    pg.get_by_role("menuitemradio", name="Campaign").click()
    pg.wait_for_function("() => [...document.querySelectorAll('.grp .grp-title')].some(t => t.textContent === 'Spring launch')")


def test_campaigns_group_the_board_and_are_managed_from_it(browser, cw):
    pg, errors = _page(browser, cw.srv)
    _group_by_campaign(pg)
    titles = pg.eval_on_selector_all(".grp .grp-title", "ts => ts.map(t => t.textContent)")
    assert titles == ["Spring launch", "No campaign"]          # an empty campaign still shows, so its menu is there
    none = pg.locator(".grp", has=pg.locator(".grp-title", has_text="No campaign"))
    assert none.locator(f'.ws-row[data-key="{cw.loaves}"]').count() == 1

    # the item panel puts the artifact's post in a campaign
    pg.locator(f'.ws-row[data-key="{cw.loaves}"]').first.hover()
    pg.get_by_role("button", name="Open Two loaves post", exact=True).click()
    panel = pg.locator(".ws-panel")
    panel.get_by_role("button", name="Campaign: none").click()
    pg.get_by_role("menuitemradio", name="Spring launch").click()
    _until(lambda: store.load_post(cw.sp, cw.post["id"]).get("campaign") == cw.spring["id"])
    spring = pg.locator(".grp", has=pg.locator(".grp-title", has_text="Spring launch"))
    spring.locator(f'.ws-row[data-key="{cw.loaves}"]').wait_for()
    pg.keyboard.press("Escape")

    # the group header renames it and keeps its milestones
    spring.get_by_role("button", name="Group options for Spring launch").click()
    pg.get_by_role("menuitem", name="Rename campaign").click()
    box = pg.get_by_role("textbox", name="Campaign name")
    box.fill("Spring opening")
    box.press("Enter")
    _until(lambda: C.load(cw.sp, cw.spring["id"])["name"] == "Spring opening")
    pg.wait_for_function("() => [...document.querySelectorAll('.grp .grp-title')].some(t => t.textContent === 'Spring opening')")

    grp = pg.locator(".grp", has=pg.locator(".grp-title", has_text="Spring opening"))
    grp.get_by_role("button", name="Group options for Spring opening").click()
    pg.get_by_role("menuitem", name="Milestones…").click()
    pg.get_by_role("textbox", name="New milestone").fill("Doors open")
    pg.get_by_role("textbox", name="Milestone date").fill("2026-12-01")
    pg.get_by_role("button", name="Add", exact=True).click()
    _until(lambda: [m["title"] for m in C.load(cw.sp, cw.spring["id"]).get("milestones") or []] == ["Doors open"])
    pg.get_by_role("checkbox", name="Doors open is done").click()
    _until(lambda: C.load(cw.sp, cw.spring["id"])["milestones"][0]["done"] is True)
    pg.get_by_role("button", name="Done", exact=True).click()

    # add or remove posts: the other post joins, the first leaves
    grp.get_by_role("button", name="Group options for Spring opening").click()
    pg.get_by_role("menuitem", name="Add or remove posts…").click()
    pg.get_by_role("checkbox", name="Include Where the flour comes from").click()
    pg.get_by_role("checkbox", name="Include Two loaves, two ways").click()
    pg.locator(".cmp-posts").get_by_role("button", name="Save").click()
    _until(lambda: store.load_post(cw.sp, cw.other["id"]).get("campaign") == cw.spring["id"]
           and not store.load_post(cw.sp, cw.post["id"]).get("campaign"))

    # a new campaign from the header
    grp.get_by_role("button", name="Group options for Spring opening").click()
    pg.get_by_role("menuitem", name="New campaign…").click()
    pg.get_by_role("textbox", name="Campaign name").fill("Autumn")
    pg.get_by_role("button", name="Create campaign").click()
    _until(lambda: any(c["name"] == "Autumn" for c in C.list_all(cw.sp)))
    pg.wait_for_function("() => [...document.querySelectorAll('.grp .grp-title')].some(t => t.textContent === 'Autumn')")
    actors = [json.loads(line).get("actor", {}) for line in (cw.sp.root / "ledger" / "events.jsonl").read_text().splitlines() if "campaign" in line]
    assert actors and all(a.get("kind") != "agent" for a in actors if isinstance(a, dict))
    assert not errors
    pg.close()


def test_paste_text_in_sources_saves_a_source_or_an_idea(browser, cw):
    pg, errors = _page(browser, cw.srv)
    pg.get_by_role("tab", name="Sources", exact=True).click()
    pg.get_by_role("button", name="Paste text").click()
    pg.get_by_role("textbox", name="Title").fill("Why we bake at night")
    pg.get_by_role("textbox", name="Text to add").fill("We bake at night because the ovens hold heat. By dawn the bread has rested.")
    pg.get_by_role("button", name="Save as a source").click()
    pg.get_by_text("Saved as a source").wait_for()
    files = list((cw.sp.work / "writing").glob("source-*.md"))
    assert len(files) == 1 and files[0].read_text().startswith("# Why we bake at night")
    before = {p["id"] for p in store.list_posts(cw.sp)}
    pg.get_by_role("button", name="Paste text").click()
    pg.get_by_role("button", name="An idea").click()
    pg.get_by_role("textbox", name="Text to add").fill("A loaf a day for a week")
    pg.get_by_role("button", name="Add to Ideas").click()
    pg.get_by_text("Idea added").wait_for()
    new = [p for p in store.list_posts(cw.sp) if p["id"] not in before]
    assert len(new) == 1 and new[0]["idea"]["text"] == "A loaf a day for a week" and new[0]["by"] != "claude"
    assert not errors
    pg.close()


def test_the_post_sheet_keeps_claimed_and_settled_notes_under_the_open_ones(browser, plan, cal):
    pid, vid = plan.rig.posts["fb"]
    st = plan.rig.w.studio
    first = st.add_feedback(pid, "rework_copy", note="Say what the bread tastes like.", variant=vid, by="maren")
    st.claim_next_feedback("claude")
    second = st.add_feedback(pid, "note", note="Shorter, please.", variant=vid, by="maren")
    st.claim_next_feedback("claude")
    st.resolve_feedback(second["id"], "claude", note="Cut it to two lines", human=True)
    st.add_feedback(pid, "rework_visual", note="The crust is too dark.", variant=vid, by="maren")
    cal.reload()
    cal.wait_for_selector(".cal-grid .cal-item")
    _week(cal)
    sheet = _sheet(cal, plan, "fb")
    open_notes = sheet.locator(".ps-why > .ps-note")
    assert open_notes.count() == 1 and "too dark" in open_notes.inner_text()
    earlier = sheet.locator(".ps-earlier")
    assert "(2)" in earlier.locator("summary").inner_text() and not earlier.locator(".ps-note").first.is_visible()
    earlier.locator("summary").click()
    text = earlier.inner_text()
    assert "Claude took it" in text and "Say what the bread tastes like." in text
    assert "Settled by Claude" in text and "Cut it to two lines" in text
    assert first["id"] not in text and second["id"] not in text                                   # words, not ids


def test_edit_campaign_has_every_field_the_old_page_had(browser, cw):
    cw.sp.accounts_dir.mkdir(parents=True, exist_ok=True)
    (cw.sp.accounts_dir / "ig-main.yaml").write_text("id: ig-main\nplatform: instagram\nmode: manual\nlabel: Instagram main\n")
    C.edit(cw.sp, cw.spring["id"], {"milestones": [{"id": "milestone_a", "title": "Doors open", "date": "2026-12-01", "done": False}]},
           expected_rev=C.load(cw.sp, cw.spring["id"])["rev"])
    pg, errors = _page(browser, cw.srv)
    _group_by_campaign(pg)
    grp = pg.locator(".grp", has=pg.locator(".grp-title", has_text="Spring launch"))
    grp.get_by_role("button", name="Group options for Spring launch").click()
    pg.get_by_role("menuitem", name="Edit campaign…").click()
    form = pg.locator(".cmp-edit")
    form.get_by_role("textbox", name="Brief").fill("Warm, early, local.")
    form.get_by_role("textbox", name="Starts").fill("2026-11-10")
    form.get_by_role("textbox", name="Ends").fill("2026-12-20")
    form.get_by_role("textbox", name="Themes").fill("bread, mornings")
    form.get_by_role("checkbox", name="Use Instagram main").click()
    form.get_by_role("textbox", name="Posts per week on Instagram main").fill("3")
    form.get_by_role("button", name="Save campaign").click()
    _until(lambda: C.load(cw.sp, cw.spring["id"]).get("brief") == "Warm, early, local.")
    c = C.load(cw.sp, cw.spring["id"])
    assert c["window"] == {"start": "2026-11-10", "end": "2026-12-20"} and c["themes"] == ["bread", "mornings"]
    assert c["accounts"] == ["ig-main"] and c["cadence"] == {"ig-main": {"per_week": 3}} and c["goal"] == "Open the bakery"
    # a milestone's title and date are edited in place
    grp.get_by_role("button", name="Group options for Spring launch").click()
    pg.get_by_role("menuitem", name="Milestones…").click()
    pg.get_by_role("button", name="Edit Doors open").click()
    pg.get_by_role("textbox", name="Title of Doors open").fill("Doors open at six")
    pg.get_by_role("textbox", name="Date of Doors open").fill("2026-12-02")
    pg.locator(".cmp-ms").get_by_role("button", name="Save", exact=True).click()
    _until(lambda: C.load(cw.sp, cw.spring["id"])["milestones"][0] == {"id": "milestone_a", "title": "Doors open at six", "date": "2026-12-02", "done": False})
    pg.get_by_role("button", name="Remove Doors open at six").click()
    _until(lambda: C.load(cw.sp, cw.spring["id"])["milestones"] == [])
    assert not errors
    pg.close()
