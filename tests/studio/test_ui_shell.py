"""WS5: the studio UI shell, calendar, grid and drag and drop, driven in headless Chromium against the fixture server
(tests/studio_ui_server.py serving the committed tests/data_studio/api/ JSON). Skipped cleanly when Playwright or Chromium is missing.

Covered (spec 10.7 + WS5 "done when"): calendar render and ARIA grid; pointer drag (day, slot, tray, conflict snap-back); keyboard drag
(pick up, step, drop, cancel, unschedule); live-region announcements; unscheduled tray; ghost proposals; grid crop overlays for 1:1, 4:5
and 9:16; grid swap by pointer and keyboard; agent edits arriving through the change feed; 390 px layout; reduced motion; zero
non-loopback requests; no console errors; contrast of the colour tokens in both themes.
"""

from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path

import pytest

from studio_ui_server import API, FixtureStudioServer

STATIC = Path(__file__).resolve().parents[2] / "src" / "ave" / "studio" / "ui" / "static"
MONTH = "/?poll=250#/calendar?view=month&d=2026-11-02"


# ------------------------------------------------------------------ contrast of the tokens (no browser needed)
def _vars(block: str) -> dict:
    return dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})", block))


def _lum(hexc: str) -> float:
    r, g, b = (int(hexc[i:i + 2], 16) / 255 for i in (1, 3, 5))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def _ratio(a: str, b: str) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _themes() -> dict:
    css = (STATIC / "studio.css").read_text()
    light = _vars(re.search(r":root \{(.*?)\n\}", css, re.S).group(1))
    dark = dict(light)
    dark.update(_vars(re.search(r':root\[data-theme="dark"\] \{(.*?)\n\}', css, re.S).group(1)))
    auto = re.search(r"@media \(prefers-color-scheme: dark\) \{\s*:root:not\(\[data-theme=\"light\"\]\) \{(.*?)\n  \}", css, re.S).group(1)
    return {"light": light, "dark": dark, "auto-dark": {**light, **_vars(auto)}}


STATUSES = ["draft", "in_review", "approved", "scheduled", "publishing", "published", "failed", "missed", "needs_rework", "dropped"]


@pytest.mark.parametrize("theme", ["light", "dark", "auto-dark"])
def test_colour_tokens_meet_contrast(theme):
    t = _themes()[theme]
    pairs = [("--ink", "--sheet"), ("--ink", "--table"), ("--ink", "--sheet-2"), ("--ink-2", "--sheet"), ("--ink-2", "--table"), ("--ink-3", "--sheet"),
             ("--ink-3", "--sheet-2"), ("--ink-3", "--table"), ("--blue", "--sheet"), ("--blue", "--sheet-2"), ("--blue", "--blue-wash"), ("--blue-ink", "--blue"),
             ("--ink", "--blue-wash"), ("--film-ink", "--film"), ("--table", "--ink")]
    pairs += [(f"--st-{s}-fg", f"--st-{s}-bg") for s in STATUSES]
    pairs += [("--blue", "--table")]
    bad = [(a, b, round(_ratio(t[a], t[b]), 2)) for a, b in pairs if _ratio(t[a], t[b]) < 4.5]
    assert not bad, f"{theme}: {bad}"
    for i in range(4):                                  # account monograms: white text on the hue
        assert _ratio("#ffffff", t[f"--hue-{i}"]) >= 4.5


def test_fixture_server_serves_the_committed_fixture_content():
    """The helper must not drift from tests/data_studio/api/: with demo off it returns the fixture's posts, tray and proposals."""
    with FixtureStudioServer(demo=False) as srv:
        fx = json.loads((API / "GET_calendar.json").read_text())["response"]
        q = "from=2026-11-01&to=2026-11-30&accounts=ig-main"
        got = json.load(urllib.request.urlopen(f"{srv.url}/api/studio/calendar?{q}"))
        assert got["days"] == fx["days"]
        assert got["unscheduled"] == fx["unscheduled"] and got["proposals"] == fx["proposals"]
        assert got["tz"] == fx["tz"]


# ------------------------------------------------------------------ browser plumbing
pw = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:                           # browser not installed on this machine
            pytest.skip(f"chromium unavailable: {e}")
        yield b
        b.close()


class Ui:
    def __init__(self, browser):
        self.browser = browser
        self.srv = FixtureStudioServer()
        self.srv.start()
        self.errors: list[str] = []
        self.requests: list[str] = []
        self.ctx = None
        self.page = None

    def open(self, path=MONTH, width=1440, height=1500, **ctx_kw):
        self.ctx = self.browser.new_context(viewport={"width": width, "height": height}, **ctx_kw)
        self.page = self.ctx.new_page()
        # a 409 the UI is SUPPOSED to receive (a refused drop) is logged by Chromium as a console error; every other error counts
        self.page.on("console", lambda m: self.errors.append(f"console.{m.type}: {m.text}") if m.type in ("error", "warning") and "status of 409" not in m.text else None)
        self.page.on("pageerror", lambda e: self.errors.append(f"pageerror: {e}"))
        self.page.on("request", lambda r: self.requests.append(r.url))
        self.page.goto(self.srv.url + path)
        self.page.wait_for_selector(".cal-grid, .g-list, .empty", timeout=8000)
        return self.page

    def close(self):
        if self.ctx:
            self.ctx.close()
        self.srv.stop()

    # --- helpers
    def live(self) -> str:
        return self.page.locator("#live").inner_text()

    def wait_js(self, expr: str, arg=None, timeout=6.0):
        """Poll a JS expression through CDP evaluate. (page.wait_for_function evals inside the page, which the studio CSP forbids.)"""
        import time
        end = time.time() + timeout
        while time.time() < end:
            if self.page.evaluate(f"(arg) => {expr}", arg):
                return
            self.page.wait_for_timeout(50)
        raise AssertionError(f"timed out waiting for: {expr}")

    def wait_live(self, startswith: str):
        self.wait_js("document.querySelector('#live').textContent.startsWith(arg)", startswith)

    def drag(self, src, dst, *, where="center", steps=14):
        for attempt in range(4):             # the 250 ms change poll repaints the lists right after a move: re-resolve a detached card
            try:
                self.page.wait_for_timeout(120)
                src.scroll_into_view_if_needed(timeout=2000)
                a = src.bounding_box()
                if a is None:
                    raise AssertionError("source card repainted before drag; re-resolve it")
                break
            except Exception:
                if attempt == 3:
                    raise
        self.page.mouse.move(a["x"] + a["width"] / 2, a["y"] + a["height"] / 2)
        self.page.mouse.down()
        self.page.mouse.move(a["x"] + a["width"] / 2 + 12, a["y"] + a["height"] / 2 + 12, steps=3)
        b = dst.bounding_box()
        y = b["y"] + b["height"] - 8 if where == "bottom" else b["y"] + b["height"] / 2
        self.page.mouse.move(b["x"] + b["width"] / 2, y, steps=steps)
        self.page.mouse.up()


@pytest.fixture
def ui(browser):
    u = Ui(browser)
    yield u
    try:
        assert not u.errors, f"console errors: {u.errors}"
        offsite = [r for r in u.requests if not (r.startswith(u.srv.url) or r.startswith("data:"))]
        assert not offsite, f"requests that left the loopback server: {offsite}"
        assert u.requests, "the page made no requests at all"
    finally:
        u.close()


def card(page, variant, within=None):
    root = page.locator(within) if within else page
    return root.locator(f'.card[data-variant="{variant}"]').first


def day(page, d):
    return page.locator(f'.day[data-date="{d}"]')


# ------------------------------------------------------------------ calendar
def test_calendar_renders_fixture_posts_slots_ghosts_and_tray(ui):
    page = ui.open()
    assert page.locator("#cal-title").inner_text() == "November 2026"
    assert page.locator('[role="grid"]').count() == 1
    assert page.locator('[role="columnheader"]').count() == 7
    assert page.locator('[role="gridcell"]').count() == 42            # Mon 26 Oct .. Sun 6 Dec
    assert page.locator('[role="row"]').count() == 7
    c = card(page, "v_ig", '.day[data-date="2026-11-02"]')
    assert c.is_visible() and "Day 3: the starter smells like pears" in c.inner_text()
    assert c.locator(".chip").inner_text().strip() == "Scheduled"
    assert "Moved after approval" in c.inner_text()
    assert card(page, "v_ig2", '.day[data-date="2026-11-04"]').locator(".chip").inner_text().strip() == "Needs rework"
    # a free rule slot is drawn as a target, a taken one is not
    assert day(page, "2026-11-06").locator('.slot[data-account="ig-main"]').count() == 1
    assert day(page, "2026-11-04").locator('.slot[data-account="ig-main"]').count() == 0
    # ghost proposal on its day, with the reason, plus the panel
    ghost = day(page, "2026-11-06").locator(".card.mode-ghost")
    assert ghost.count() == 1 and "Suggested" in ghost.inner_text()
    assert "Friday morning is free" in page.locator(".suggest").inner_text()
    # unscheduled tray
    tray = page.locator(".tray")
    assert "Unscheduled" in tray.inner_text() and tray.locator(".card").count() == 2
    assert "Why we weigh flour" in tray.inner_text()
    # every status chip is an icon AND a word
    chips = page.locator(".chip")
    assert chips.count() >= 8
    for i in range(chips.count()):
        assert chips.nth(i).locator("svg").count() == 1 and chips.nth(i).inner_text().strip()
    # posting mode is words, per account
    assert "Dry run" in page.locator("#modes").inner_text()
    # exactly one roving tab stop among the gridcells
    assert page.locator('.day[tabindex="0"]').count() == 1


def test_calendar_week_day_and_account_lanes(ui):
    page = ui.open("/?poll=250#/calendar?view=week&d=2026-11-02")
    assert "2 Nov" in page.locator("#cal-title").inner_text()
    assert page.locator('[role="gridcell"]').count() == 7
    assert day(page, "2026-11-03").locator(".lane").count() == 1        # per-account lane label
    page.click('.seg-b[data-view="day"]')
    ui.wait_js("document.querySelectorAll('[role=gridcell]').length === 1")
    assert "Monday 2 November" in page.locator("#cal-title").inner_text()
    page.click('.acct-chip:has-text("Acme Bakery")')                   # turn the Facebook page off
    ui.wait_js("!document.querySelector('.card[data-variant=v_f1]')")
    assert page.locator('.acct-chip[aria-pressed="false"]').count() == 1


def test_pointer_drag_card_to_another_day(ui):
    page = ui.open()
    ui.drag(card(page, "v_ig"), day(page, "2026-11-03"), where="bottom")
    ui.wait_live('Moved "Day 3: the starter smells like pears" to Tuesday 3 November, 09:00')
    assert ui.srv.moves[-1]["to"] == "2026-11-03T09:00" and ui.srv.moves[-1]["snap"] == "slot"
    assert card(page, "v_ig", '.day[data-date="2026-11-03"]').is_visible()
    assert card(page, "v_ig", '.day[data-date="2026-11-02"]').count() == 0
    assert page.locator("#note.on").is_visible()                       # the visible mirror of the live region


def test_pointer_drop_onto_a_free_slot_is_exact(ui):
    page = ui.open()
    ui.drag(card(page, "v_ig"), day(page, "2026-11-18").locator(".slot"))
    ui.wait_live("Moved ")
    assert ui.srv.moves[-1]["to"] == "2026-11-18T09:00"
    assert card(page, "v_ig", '.day[data-date="2026-11-18"]').is_visible()


def test_drop_on_a_taken_slot_snaps_back_and_says_why(ui):
    page = ui.open()
    ui.drag(card(page, "v_ig"), day(page, "2026-11-04"), where="bottom")
    ui.wait_live("Could not move")
    assert "taken" in ui.live() and "It stays on Monday 2 November" in ui.live()
    assert page.locator('#note[data-tone="error"]').count() == 1
    assert ui.srv.moves == []
    assert card(page, "v_ig", '.day[data-date="2026-11-02"]').is_visible()


def test_drag_to_tray_unschedules_and_back_out_to_a_slot(ui):
    page = ui.open()
    ui.drag(card(page, "v_ig"), page.locator(".tray"))
    ui.wait_live('Moved "Day 3: the starter smells like pears" to Unscheduled')
    assert ui.srv.unscheduled_calls == [{"post": "p_20261102_k3f9", "variant": "v_ig"}]
    assert card(page, "v_ig", ".tray").is_visible()
    assert page.locator(".tray-n").inner_text() == "3"
    ui.drag(card(page, "v_d5", ".tray"), day(page, "2026-11-20").locator(".slot"))      # Scoring the loaf, from the tray onto a Friday slot
    ui.wait_live('Moved "Scoring the loaf"')
    assert ui.srv.moves[-1]["to"] == "2026-11-20T09:00"


def test_post_without_an_account_cannot_be_scheduled_and_says_so(ui):
    page = ui.open()
    ui.drag(page.locator('.tray .card:has-text("Why we weigh flour")'), day(page, "2026-11-12"), where="bottom")
    ui.wait_live('"Why we weigh flour" has no account yet')
    assert ui.srv.moves == []


def test_published_posts_are_locked_with_a_reason_and_no_handle(ui):
    page = ui.open()
    pub = card(page, "v_pub1")
    assert pub.locator(".grip").count() == 0 and pub.locator(".lock").count() == 1
    assert "cannot be moved" in (pub.locator(".card-link").get_attribute("title") or "")
    ui.drag(pub, day(page, "2026-11-12"))
    page.wait_for_timeout(300)
    assert ui.srv.moves == [] and page.locator(".dnd-ghost").count() == 0
    page.focus('.card[data-variant="v_pub1"] .card-link')
    page.keyboard.press("m")
    ui.wait_js("document.querySelector('#live').textContent.includes('cannot be moved')")


def test_accept_a_ghost_proposal(ui):
    page = ui.open()
    day(page, "2026-11-06").locator(".ghost-accept").click()
    ui.wait_live("Applied 1 suggested move")
    assert ui.srv.accepted == ["sp_0001"]
    assert card(page, "v_ig2", '.day[data-date="2026-11-06"]').is_visible()
    assert page.locator(".suggest").count() == 0 and page.locator(".card.mode-ghost").count() == 0


def test_keyboard_move_by_slot_then_drop(ui):
    page = ui.open()
    page.focus('.card[data-variant="v_ig"] .card-link')
    page.keyboard.press("m")
    ui.wait_live("Picked up")
    assert page.evaluate("document.body.classList.contains('kb-moving')")
    page.keyboard.press("ArrowRight")                                  # next FREE slot: Wed 4 is taken, so Friday 6
    ui.wait_live("Friday 6 November at 09:00, free slot")
    assert page.locator(".kb-target").count() == 1
    page.keyboard.press("Enter")
    ui.wait_live('Moved "Day 3: the starter smells like pears" to Friday 6 November, 09:00')
    assert ui.srv.moves[-1]["to"] == "2026-11-06T09:00"
    assert not page.evaluate("document.body.classList.contains('kb-moving')")
    assert page.evaluate("document.activeElement.closest('.card')?.dataset.variant") == "v_ig"      # focus follows the card


def test_keyboard_shift_arrow_moves_a_whole_day_and_escape_cancels(ui):
    page = ui.open()
    page.focus('.card[data-variant="v_ig"] .card-link')
    page.keyboard.press("m")
    page.keyboard.press("Shift+ArrowRight")
    ui.wait_live("Tuesday 3 November, keeps 09:00")
    page.keyboard.press("Escape")
    ui.wait_live("Move cancelled")
    assert ui.srv.moves == [] and page.locator(".kb-target").count() == 0
    page.keyboard.press("m")
    page.keyboard.press("Shift+ArrowRight")
    page.keyboard.press("Enter")
    ui.wait_live("Moved ")
    assert ui.srv.moves[-1]["to"] == "2026-11-03T09:00"


def test_keyboard_unschedule_and_conflict_announcement(ui):
    page = ui.open()
    page.focus('.card[data-variant="v_ig"] .card-link')
    page.keyboard.press("m")
    for _ in range(2):
        page.keyboard.press("Shift+ArrowRight")                         # onto Wednesday 4, whose 09:00 is taken
    page.keyboard.press("Enter")
    ui.wait_live("Could not move")
    assert "taken" in ui.live()
    page.focus('.card[data-variant="v_ig"] .card-link')
    page.keyboard.press("u")
    ui.wait_live('Moved "Day 3: the starter smells like pears" to Unscheduled')
    assert ui.srv.unscheduled_calls[-1]["variant"] == "v_ig"


def test_calendar_grid_keyboard_navigation_and_enter_previews_the_post_and_o_opens_it(ui):
    page = ui.open()
    page.focus('.day[data-date="2026-11-02"]')
    page.keyboard.press("ArrowRight")
    assert page.evaluate("document.activeElement.dataset.date") == "2026-11-03"
    page.keyboard.press("ArrowDown")
    assert page.evaluate("document.activeElement.dataset.date") == "2026-11-10"
    assert page.locator('.day[tabindex="0"]').count() == 1 and page.locator('.day[data-date="2026-11-10"]').get_attribute("tabindex") == "0"
    page.keyboard.press("ArrowLeft")
    page.keyboard.press("ArrowUp")
    assert page.evaluate("document.activeElement.dataset.date") == "2026-11-02"
    page.focus('.day[data-date="2026-11-02"]')
    page.keyboard.press("Enter")                                       # a day with a post: Enter steps into it
    assert page.evaluate("document.activeElement.closest('.card')?.dataset.variant") == "v_ig"
    page.keyboard.press("Enter")                                       # Enter on the post previews it beside the calendar: the calendar is not left
    page.wait_for_selector("#peek.on")
    assert page.evaluate("location.hash").startswith("#/calendar")
    page.keyboard.press("Escape")                                      # Esc closes the preview and gives focus back to the post
    page.wait_for_selector("#peek.on", state="detached") if False else ui.wait_js("!document.querySelector('#peek.on')")
    assert page.evaluate("document.activeElement.closest('.card')?.dataset.variant") == "v_ig"
    page.keyboard.press("o")                                           # o opens the editor
    ui.wait_js("location.hash.startsWith('#/post/p_20261102_k3f9')")
    page.wait_for_selector("main[data-page=post] p")


def test_keyboard_walks_off_the_month_into_the_next(ui):
    page = ui.open()
    page.focus('.day[data-date="2026-11-30"]')
    page.keyboard.press("PageDown")
    ui.wait_js("document.querySelector('#cal-title').textContent === 'December 2026'")


def test_live_changes_from_an_agent_appear_without_reload(ui):
    page = ui.open()
    assert card(page, "v_d3", '.day[data-date="2026-11-13"]').is_visible()
    ui.srv.agent_move("v_d3", "2026-11-12T09:00")                       # what `ave studio move` does from a terminal
    page.wait_for_selector('.day[data-date="2026-11-12"] .card[data-variant="v_d3"]', timeout=6000)
    assert card(page, "v_d3", '.day[data-date="2026-11-13"]').count() == 0
    ui.wait_live("Updated: an agent changed a post")


def test_a_change_feed_refresh_waits_for_a_drag_in_progress(ui):
    page = ui.open()
    c = card(page, "v_ig")
    a = c.bounding_box()
    page.mouse.move(a["x"] + 20, a["y"] + 20)
    page.mouse.down()
    page.mouse.move(a["x"] + 60, a["y"] + 80, steps=4)
    ui.srv.agent_move("v_d3", "2026-11-12T09:00")
    page.wait_for_timeout(900)                                          # several polls
    assert page.locator(".dnd-ghost").count() == 1                      # still dragging, DOM not rebuilt under the pointer
    assert card(page, "v_d3", '.day[data-date="2026-11-13"]').count() == 1
    page.keyboard.press("Escape")
    page.mouse.up()
    page.wait_for_selector('.day[data-date="2026-11-12"] .card[data-variant="v_d3"]', timeout=6000)


# ------------------------------------------------------------------ grid
def test_grid_full_images_and_crop_geometry(ui):
    page = ui.open("/?poll=250#/grid/ig-main", height=1300)
    geo = page.evaluate("import('/static/grid.js').then(m => [m.cropGeometry(1080, 1350, 0.75), m.cropGeometry(1080, 1080, 0.75), m.cropGeometry(1080, 1920, 0.75)])")
    assert geo[0]["lostX"] == pytest.approx(0.0625) and geo[1]["lostX"] == pytest.approx(0.25) and geo[2]["lostY"] == pytest.approx(0.25)
    assert page.locator('.planner-image img').first.evaluate('(e)=>getComputedStyle(e).objectFit') == 'cover'
    assert page.locator('.planner-image img').first.evaluate('(e)=>getComputedStyle(e).objectPosition') == '50% 40%'
    page.get_by_role('button', name='Full images', exact=True).click()
    assert page.locator('.planner-image img').first.evaluate('(e)=>getComputedStyle(e).objectFit') == 'contain'


def test_grid_inserts_by_pointer_without_schedule_swaps_and_locks_history(ui):
    page = ui.open("/?poll=250#/grid/ig-main", height=1500)
    order = lambda: page.eval_on_selector_all(".gcell", "els => els.map(e => e.dataset.variant)")
    original = order()
    dates = {i['variant']:i['at'] for i in ui.srv.items}
    locked = page.locator('.gcell[data-variant="v_pub1"]')
    assert locked.locator('.planner-badge').inner_text() == 'Published'
    ui.drag(page.locator('.gcell[data-variant="v_d4"]'), page.locator('.gcell[data-variant="v_d2"]'))
    ui.wait_live('Moved ')
    assert order() == original[1:3] + original[:1] + original[3:]
    assert ui.srv.swaps == []
    assert {i['variant']:i['at'] for i in ui.srv.items} == dates
    ui.drag(page.locator('.gcell[data-variant="v_d4"]'), locked)
    page.wait_for_timeout(200)
    assert ui.srv.feed_revs['ig-main'] == 1


def test_grid_inserts_by_keyboard(ui):
    page = ui.open("/?poll=250#/grid/ig-main", height=1300)
    original = page.locator('.gcell').evaluate_all('(els)=>els.map(e=>e.dataset.variant)')
    page.locator('.gcell .g-link').first.focus()
    page.keyboard.press("m")
    ui.wait_live('Picked up ')
    page.keyboard.press("ArrowRight")
    ui.wait_live("Position 2")
    page.keyboard.press("Enter")
    ui.wait_live("Moved ")
    assert page.locator('.gcell').evaluate_all('(els)=>els.map(e=>e.dataset.variant)') == original[1:2] + original[:1] + original[2:]
    assert ui.srv.swaps == []
    assert page.evaluate("document.activeElement.closest('.gcell')?.dataset.variant") == original[0]


def test_grid_arrow_keys_move_focus_and_account_selector_switches_layout(ui):
    page = ui.open("/#/grid/ig-main", height=1300)
    page.focus('.gcell[data-index="0"] .g-link')
    page.keyboard.press("ArrowRight")
    assert page.evaluate("document.activeElement.closest('.gcell').dataset.index") == "1"
    page.keyboard.press("ArrowDown")
    assert page.evaluate("document.activeElement.closest('.gcell').dataset.index") == "4"
    page.get_by_label('Feed account', exact=True).select_option('fb-page')
    page.wait_for_selector(".g-list.kind-page_timeline")
    assert page.locator('.g-list').evaluate('(e)=>getComputedStyle(e).gridTemplateColumns.split(" ").length') == 1


# ------------------------------------------------------------------ phones, motion, themes
def test_phone_width_has_no_sideways_scroll_and_week_is_the_default(ui):
    page = ui.open("/#/calendar", width=390, height=844, has_touch=True, is_mobile=True)
    page.wait_for_selector(".view-week")
    for path in ("#/calendar?view=week&d=2026-11-02", "#/calendar?view=month&d=2026-11-02", "#/calendar?view=day&d=2026-11-02", "#/grid/ig-main", "#/grid/fb-page"):
        page.evaluate("p => { location.hash = p }", path)
        page.wait_for_selector(".cal-grid, .g-list")
        page.wait_for_timeout(250)
        assert page.evaluate("document.documentElement.scrollWidth") <= 390, path
        assert page.evaluate("document.body.scrollWidth") <= 390, path
    page.evaluate("location.hash = '#/calendar?view=week&d=2026-11-02'")
    page.wait_for_selector(".view-week .card")
    assert page.locator(".today-b").bounding_box()["height"] >= 36
    grip = page.locator('.card[data-variant="v_ig"] .grip').bounding_box()
    assert grip["width"] >= 26 and grip["height"] >= 26


def test_touch_drag_starts_only_from_the_grip(ui):
    page = ui.open("/#/calendar", width=390, height=844, has_touch=True, is_mobile=True)
    page.evaluate("location.hash = '#/calendar?view=week&d=2026-11-02'")
    page.wait_for_selector('.card[data-variant="v_ig"] .grip')
    fire = """([sel, type, dx, dy]) => {
      const el = document.querySelector(sel); const r = el.getBoundingClientRect();
      const x = r.left + r.width / 2 + dx, y = r.top + r.height / 2 + dy;
      const target = type === 'pointerdown' ? el : window;
      target.dispatchEvent(new PointerEvent(type, {pointerType: 'touch', isPrimary: true, pointerId: 7, button: 0, bubbles: true, clientX: x, clientY: y}));
    }"""
    card_sel = '.card[data-variant="v_ig"] .card-link'
    page.evaluate(fire, [card_sel, "pointerdown", 0, 0])
    page.evaluate(fire, [card_sel, "pointermove", 0, 60])
    assert page.locator(".dnd-ghost").count() == 0                       # a finger on the card scrolls the page instead
    page.evaluate(fire, [card_sel, "pointerup", 0, 60])
    grip_sel = '.card[data-variant="v_ig"] .grip'
    page.evaluate(fire, [grip_sel, "pointerdown", 0, 0])
    page.evaluate(fire, [grip_sel, "pointermove", 0, 30])
    assert page.locator(".dnd-ghost").count() == 1
    assert page.evaluate("getComputedStyle(document.querySelector('.card[data-variant=v_ig] .grip')).touchAction") == "none"
    page.locator(".tray").scroll_into_view_if_needed()                  # the edge auto-scroll does this for a real finger
    tray = page.locator(".tray").bounding_box()
    page.evaluate("""([x, y]) => { for (const t of ['pointermove', 'pointerup']) window.dispatchEvent(new PointerEvent(t, {pointerType: 'touch', isPrimary: true, pointerId: 7, button: 0, bubbles: true, clientX: x, clientY: y})); }""",
                  [tray["x"] + tray["width"] / 2, tray["y"] + 40])
    ui.wait_live('Moved "Day 3: the starter smells like pears" to Unscheduled')
    assert ui.srv.unscheduled_calls[-1]["variant"] == "v_ig"


def test_reduced_motion_is_respected(browser):
    u = Ui(browser)
    try:
        page = u.open("/#/grid/ig-main", reduced_motion="reduce")
        assert page.evaluate("getComputedStyle(document.querySelector('.planner-image')).transitionDuration") in ("0s", "0.000001s")
        assert page.evaluate("getComputedStyle(document.querySelector('#note')).transitionDuration") in ("0s", "0.000001s")
        assert page.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches")
        assert not u.errors
    finally:
        u.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_both_color_schemes_render_with_the_right_palette(browser, scheme):
    u = Ui(browser)
    try:
        page = u.open(color_scheme=scheme)
        bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
        assert bg == "rgb(246, 247, 251)"  # fresh workspaces are explicitly light, independent of OS
        page.click("#theme")  # light -> explicit dark; the override wins over the OS
        assert page.evaluate("document.documentElement.dataset.theme") == "dark"
        assert page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(20, 21, 29)"
        assert not u.errors
    finally:
        u.close()


def test_other_workstreams_pages_degrade_to_a_notice_or_their_module(ui):
    page = ui.open()
    page.evaluate("location.hash = '#/review'")
    page.wait_for_selector("main[data-page=review] p")                   # the fixture server stands in for WS6's module
    page.evaluate("location.hash = '#/nowhere'")
    page.wait_for_selector(".empty h1")
    assert "does not exist" in page.locator(".empty h1").inner_text()


def test_help_dialog_lists_the_keyboard_path(ui):
    page = ui.open()
    page.keyboard.press("?")
    page.wait_for_selector("dialog[open]")
    text = page.locator("dialog").inner_text()
    for word in ("Pick up", "Escape", "Unscheduled", "Shift"):
        assert word.lower() in text.lower() or word == "Escape"
    page.keyboard.press("Escape")
    page.wait_for_selector("dialog[open]", state="detached")
