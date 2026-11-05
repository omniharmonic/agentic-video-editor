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


def page_at(rig, width=1440, scheme="light", route="#/pipeline"):
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

# ---------------------------------------------------------------------------------------------------- phones

PHONE_ROUTES = ["#/pipeline", "#/workbench", "#/grid/ig-longtable", "#/post/p_lt_carousel_miller"]      # the four tools under More tools


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
        assert pg.locator('#nav a[data-p="pipeline"]').is_visible()
        # Click the exposed backdrop outside the 220px drawer.
        pg.get_by_role('button', name='Close menu', exact=True).click(position={'x': 370, 'y': 100})
        assert pg.get_by_role('button', name='Menu', exact=True).get_attribute('aria-expanded') == 'false'
        ctx.close()
        ctx, pg = page_at(rig, width=1440)
        assert pg.evaluate("getComputedStyle(document.querySelector('.top')).position") == "fixed"      # the desktop sidebar stays visible
    finally:
        ctx.close()


# ---------------------------------------------------------------------------------------------------- the day view

# ---------------------------------------------------------------------------------------------------- library

@pytest.mark.legacy_ui
def test_library_api_reports_the_true_total_and_takes_a_limit(rig):
    st, res = api(rig, "library?limit=7")
    assert st == 200 and len(res["assets"]) == 7 and res["total"] > 50
    st, res = api(rig, "library")
    assert len(res["assets"]) == res["total"] > 50                      # the old default silently cut at 50
    assert api(rig, "library?limit=lots")[0] == 400


# ---------------------------------------------------------------------------------------------------- names, not ids

# ---------------------------------------------------------------------------------------------------- campaigns

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
def test_a_stale_post_link_is_a_plain_not_found_without_a_server_path(bare_rig):
    ctx, pg = page_at(bare_rig, route="#/post/p_nope")
    try:
        pg.wait_for_selector(".st-empty")
        text = pg.locator("main").inner_text()
        assert "This post was not found" in text and str(bare_rig[2]) not in text and "/private" not in text and "/var/" not in text
        assert not pg.locator('[data-act="sendback"]').is_visible()
    finally:
        ctx.close()

