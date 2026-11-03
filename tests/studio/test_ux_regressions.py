"""Regressions for what the UX review of the demo studio found (docs: scratchpad qa/F-studio-ux.md), driven through the REAL server and shell over a
demo studio in headless Chromium: the panel pages are styled at all, nothing overflows a phone, the library is not silently cut at 50, the
review list names posts rather than ids, the campaigns page exists, and the day view / week agenda do not clip or misplace things."""

from __future__ import annotations

import json
import threading
import urllib.request
from datetime import timedelta

import pytest

pw = pytest.importorskip("playwright.sync_api")

from ave.studio import demo
from ave.studio.service import open_studio
from ave.studio.ui.server import StudioServer

PIN = "424242"
KEY = "ux-test-key"


@pytest.fixture(scope="module")
def rig(tmp_path_factory):
    from content_helpers import BROWSERS
    root = tmp_path_factory.mktemp("ux") / "studio"
    with pytest.MonkeyPatch.context() as mp:
        if BROWSERS:
            mp.setenv("PLAYWRIGHT_BROWSERS_PATH", BROWSERS)
        mp.setenv("AVE_APPROVAL_KEY", KEY)
        demo.build_demo(root)
        st = open_studio(root)
        srv = StudioServer(st, port=0, by="tester", pin=PIN)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        with pw.sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except Exception as e:
                srv.shutdown()
                pytest.skip(f"chromium unavailable: {e}")
            yield srv, browser, st
            browser.close()
        srv.shutdown()
        srv.server_close()


def page_at(rig, width=1440, scheme="light", route="#/calendar"):
    srv, browser, _ = rig
    ctx = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=scheme)
    pg = ctx.new_page()
    # Keep actual failure bodies in pytest capture rather than only selector timeouts.
    pg.on('pageerror',lambda e:print(f'Browser page error: {e}'))
    pg.on('response',lambda r:print(f'HTTP {r.status} {r.url}: {r.text()}') if r.status>=400 else None)
    pg.goto(srv.url())
    pg.wait_for_selector("main[data-page]", timeout=10000)
    pg.evaluate("(h) => { location.hash = h }", route[1:])
    pg.wait_for_timeout(700)
    return ctx, pg


def api(rig, path):
    srv, _, _ = rig
    token = srv.token
    req = urllib.request.Request(f"http://127.0.0.1:{srv.port}/api/studio/{path}", headers={"X-AVE-Token": token})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


# ---------------------------------------------------------------------------------------------------- the panel pages are styled

@pytest.mark.legacy_ui
@pytest.mark.parametrize("scheme,blue", [("light", "#335cde"), ("dark", "#b7aaff")])
def test_the_panel_stylesheet_is_linked_and_takes_the_shells_accent(rig, scheme, blue):
    ctx, pg = page_at(rig, scheme=scheme, route="#/alerts")
    try:
        pg.evaluate("scheme => localStorage.setItem('ave.studio.theme', JSON.stringify(scheme))", scheme)
        pg.reload()
        pg.wait_for_selector(".st-page")
        hrefs = pg.evaluate("[...document.styleSheets].map(s => s.href || '')")
        assert any(h.endswith("/static/panels.css") for h in hrefs), hrefs
        assert any(h.endswith("/static/studio.css") for h in hrefs) and [i for i, h in enumerate(hrefs) if "panels.css" in h][0] < [i for i, h in enumerate(hrefs) if "studio.css" in h][0]
        accent = pg.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--st-accent').trim()")
        assert accent.lower() == blue
        # a panel page is laid out, not a column of bare controls: the alert cards have a border and padding
        card = pg.locator(".st-page .st-card, .st-page article, .st-page .st-list > *").first
        assert card.evaluate("el => parseFloat(getComputedStyle(el).paddingLeft)") > 4
        # dark: the pencil-red and marker tokens are the dark values (the light fallbacks would be unreadable on a dark sheet)
        pencil = pg.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--st-bad').trim()").lower()
        assert pencil == ("#f08577" if scheme == "dark" else "#a52f24")
    finally:
        ctx.close()


# ---------------------------------------------------------------------------------------------------- phones

PHONE_ROUTES = ["#/calendar?view=month", "#/calendar?view=week", "#/calendar?view=day", "#/grid/ig-longtable", "#/post/p_lt_carousel_miller", "#/review",
                "#/library", "#/feedback", "#/alerts", "#/accounts", "#/campaigns"]


@pytest.mark.legacy_ui
@pytest.mark.parametrize("route", PHONE_ROUTES)
def test_no_page_scrolls_sideways_on_a_phone(rig, route):
    ctx, pg = page_at(rig, width=390, route=route)
    try:
        pg.wait_for_timeout(500)
        assert pg.evaluate("document.documentElement.scrollWidth") <= 390, route
    finally:
        ctx.close()


@pytest.mark.legacy_ui
def test_the_top_bar_does_not_pin_three_rows_to_a_phone(rig):
    ctx, pg = page_at(rig, width=390)
    try:
        # A closed phone drawer is outside the viewport; only the compact bar
        # occupies the planning surface. Opening it exposes working navigation.
        assert pg.locator('.top').bounding_box()['x'] + pg.locator('.top').bounding_box()['width'] <= 0
        assert pg.locator('.workspace-bar').bounding_box()['height'] <= 56
        pg.get_by_role('button', name='Menu', exact=True).click()
        assert pg.get_by_role('button', name='Menu', exact=True).get_attribute('aria-expanded') == 'true'
        assert pg.locator('#nav a[data-p="calendar"]').is_visible()
        # Click the exposed backdrop outside the 220px drawer.
        pg.get_by_role('button', name='Close menu', exact=True).click(position={'x': 370, 'y': 100})
        assert pg.get_by_role('button', name='Menu', exact=True).get_attribute('aria-expanded') == 'false'
        ctx.close()
        ctx, pg = page_at(rig, width=1440)
        assert pg.evaluate("getComputedStyle(document.querySelector('.top')).position") == "fixed"      # the desktop sidebar stays visible
    finally:
        ctx.close()


@pytest.mark.legacy_ui
def test_a_status_word_in_the_phone_agenda_is_not_covered_by_the_lock(rig):
    # Three days back so the week always holds published posts: this module-scoped server cannot verify "scheduled" once the per-test fixtures clear AVE_*.
    day = (rig[2].now() - timedelta(days=3)).strftime("%Y-%m-%d")
    ctx, pg = page_at(rig, width=390, route="#/calendar?view=week&d=" + day)
    try:
        pg.wait_for_selector('.card[data-status="published"], .card[data-status="scheduled"]')
        rows = pg.evaluate("""() => [...document.querySelectorAll('.card.mode-week')].map(c => {
          const chip = c.querySelector('.cap .st-chip, .cap .chip'); const lock = c.querySelector('.lock, .grip');
          if (!chip || !lock) return null;
          const a = chip.getBoundingClientRect(), b = lock.getBoundingClientRect();
          return { overlap: a.right > b.left + 1 && a.bottom > b.top && a.top < b.bottom, chipClipped: chip.scrollWidth > chip.clientWidth + 1 };
        }).filter(Boolean)""")
        assert rows and not any(r["overlap"] for r in rows), rows[:3]
    finally:
        ctx.close()


# ---------------------------------------------------------------------------------------------------- the day view

@pytest.mark.legacy_ui
def test_day_view_lane_labels_take_a_row_of_their_own(rig):
    # The live-clock demo does not promise two accounts on *today*. Select a
    # seeded multi-account day, preserving the actual full-width-lane assertion.
    from datetime import timedelta
    today = rig[2].now().date()
    status, calendar = api(rig, f"calendar?from={today-timedelta(days=20)}&to={today+timedelta(days=20)}")
    assert status == 200
    candidates = [(day, items) for day, items in calendar['days'].items() if len({it['account'] for it in items}) >= 2]
    assert candidates, 'the demo must contain a multi-account day for this regression'
    day, items = candidates[0]
    ctx, pg = page_at(rig, route="#/calendar?view=day&d=" + day)
    try:
        pg.wait_for_selector(".view-day .lane, .cal-host")
        lanes = pg.locator(".view-day .lane")
        assert lanes.count() >= len({it['account'] for it in items}) >= 2
        assert pg.locator('.view-day .card').count() == len(items)
        span = pg.evaluate("""() => { const l = document.querySelector('.view-day .lane'); const list = l.parentElement;
          return { lane: l.getBoundingClientRect().width, list: list.getBoundingClientRect().width, col: getComputedStyle(l).gridColumnStart + '/' + getComputedStyle(l).gridColumnEnd }; }""")
        assert span["lane"] > span["list"] * 0.9, span
    finally:
        ctx.close()


# ---------------------------------------------------------------------------------------------------- library

@pytest.mark.legacy_ui
def test_library_api_reports_the_true_total_and_takes_a_limit(rig):
    st, res = api(rig, "library?limit=7")
    assert st == 200 and len(res["assets"]) == 7 and res["total"] > 50
    st, res = api(rig, "library")
    assert len(res["assets"]) == res["total"] > 50                      # the old default silently cut at 50
    assert api(rig, "library?limit=lots")[0] == 400


@pytest.mark.legacy_ui
def test_library_page_lists_everything_and_says_when_it_is_cut(rig):
    srv, browser, st = rig
    total = api(rig, "library")[1]["total"]
    ctx, pg = page_at(rig, route="#/library")
    try:
        pg.wait_for_selector(".lib-card")
        assert pg.locator(".lib-card").count() == total
        assert pg.get_by_text(f"{total} assets").count() == 1
        # the stock B-roll and its licence are in the list (they used to sit past the cut)
        assert pg.locator(".lib-card", has_text="Demo Stock License").count() >= 6
        # the licence words wrap instead of being cut with an ellipsis
        chip = pg.locator(".lib-card .st-chip.lic").first
        assert chip.evaluate("el => getComputedStyle(el).whiteSpace") == "normal"
    finally:
        ctx.close()
    # a cut list says so
    ctx, pg = page_at(rig, route="#/library")
    try:
        pg.route("**/api/studio/library**", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(
            {"assets": api(rig, "library?limit=3")[1]["assets"], "total": 99})))
        pg.evaluate("location.hash = '#/accounts'")
        pg.evaluate("location.hash = '#/library'")
        pg.wait_for_selector(".lib-card")
        assert "Showing 3 of 99" in pg.locator("main").inner_text()
    finally:
        ctx.close()


# ---------------------------------------------------------------------------------------------------- names, not ids

@pytest.mark.legacy_ui
def test_review_and_editor_name_posts_by_their_words(rig):
    ctx, pg = page_at(rig, route="#/review")
    try:
        pg.wait_for_selector(".rv-li")
        titles = pg.locator(".rv-li-title").all_inner_texts()
        assert titles and not any(t.startswith("p_") for t in titles), titles
        assert not pg.locator(".rv-sheet-head h2").inner_text().startswith("p_")
        pg.evaluate("location.hash = '#/post/p_lt_quote_ines'")
        pg.wait_for_selector(".ed-title")
        pw.expect(pg.locator(".ed-title")).to_contain_text("Good flour")
        assert pg.locator(".ed-title").inner_text().startswith("Good flour")
    finally:
        ctx.close()


# ---------------------------------------------------------------------------------------------------- campaigns

@pytest.mark.legacy_ui
def test_the_campaigns_page_is_real(rig):
    st, res = api(rig, "campaigns")
    assert st == 200 and res["total"] == 2 and {r["campaign"]["name"] for r in res["campaigns"]} == {"Season 4 launch", "Thanksgiving loaf box"}
    ctx, pg = page_at(rig, route="#/campaigns")
    try:
        pg.wait_for_selector(".campaign-detail")
        assert pg.locator('.campaign-list button').count() == res['total']
        for row in res['campaigns']:
            campaign = row['campaign']
            pg.locator('.campaign-list').get_by_role('button', name=campaign['name'], exact=True).click()
            assert pg.locator('.campaign-detail h2').inner_text() == campaign['name']
            assert campaign['goal'] in pg.locator('.campaign-detail').inner_text()
            assert pg.locator('.campaign-content-list a').count() == len(row['posts'])
            assert pg.get_by_label('Production completion', exact=True).count() == 1
            assert pg.get_by_label('Publication completion', exact=True).count() == 1
            if row['posts']:
                assert pg.locator('.campaign-content-list a').first.get_attribute('href').startswith('#/post/')
    finally:
        ctx.close()


# ---------------------------------------------------------------------------------------------------- empty and missing

@pytest.fixture(scope="module")
def bare_rig(rig, tmp_path_factory):
    """An empty studio on its own server, in the browser the demo rig already started (two sync Playwrights cannot share a thread)."""
    from ave.studio.project import StudioProject
    root = tmp_path_factory.mktemp("bare") / "studio"
    sp = StudioProject(root)
    sp.create(title="Brand new", tz="America/Denver")
    srv = StudioServer(open_studio(root), port=0, by="tester", pin=PIN)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, rig[1], root
    srv.shutdown()
    srv.server_close()


@pytest.mark.legacy_ui
def test_a_new_studio_calendar_says_what_to_do(bare_rig):
    ctx, pg = page_at(bare_rig, route="#/calendar?view=week")
    try:
        pg.wait_for_selector(".cal-sub")
        assert "Nothing is planned yet" in pg.locator(".cal-sub").inner_text()
    finally:
        ctx.close()


@pytest.mark.legacy_ui
def test_a_stale_post_link_is_a_plain_not_found_without_a_server_path(bare_rig):
    ctx, pg = page_at(bare_rig, route="#/post/p_nope")
    try:
        pg.wait_for_selector(".st-empty")
        text = pg.locator("main").inner_text()
        assert "This post was not found" in text and str(bare_rig[2]) not in text and "/private" not in text and "/var/" not in text
        assert not pg.locator('[data-act="sendback"]').is_visible()
    finally:
        ctx.close()


@pytest.mark.legacy_ui
def test_feedback_cards_read_as_words_not_field_names(rig):
    ctx, pg = page_at(rig, route="#/feedback")
    try:
        pg.wait_for_selector(".st-card, article")
        pg.wait_for_timeout(300)
        text = pg.locator("main").inner_text()
        assert "wrong crop" in text and "wrong_crop" not in text
        assert not pg.locator("dt", has_text="Was").count()            # an empty "Was" row says nothing
    finally:
        ctx.close()
