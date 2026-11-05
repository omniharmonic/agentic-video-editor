"""The REAL `StudioServer` serving the More tools shell (index.html + app.js at /tools/), walked page by page in headless Chromium:
the four older tools the app keeps unchanged (Production, Media tools, Feed planner, Post editor). Every other old page is gone.

Two backends, same walk: the committed fixtures (`ave studio ui --fixtures`) and a real wired `Studio` over `build_studio` data. Each page must
mount through app.js `mountPage` (its own root element appears, never the "not part of this build" / "could not be drawn" notice), with no
console error, no page error, no failed request (4xx/5xx) and no non-loopback request. Skipped cleanly without Playwright or Chromium.
"""

from __future__ import annotations

import threading

import pytest

pw = pytest.importorskip("playwright.sync_api")

from ave.studio.service import FixtureStudio, open_studio
from ave.studio.ui.server import StudioServer
from studio_helpers import build_studio

PIN = "424242"
# route -> (selector proving the page itself mounted, heading text expected somewhere inside <main>)
PAGES = [
    ("#/pipeline", ".production-table, .production-empty, main h1", None),
    ("#/workbench", ".wb-heading", None),
    ("#/grid/ig-main", ".g-list", None),
]
NOT_MOUNTED = ("is not part of this build", "could not be drawn", "does not exist", "is not reachable")


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:
            pytest.skip(f"chromium unavailable: {e}")
        yield b
        b.close()


def serve(studio, **kw):
    s = StudioServer(studio, port=0, by="tester", pin=PIN, **kw)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


class Walk:
    def __init__(self, browser, srv):
        self.srv = srv
        self.ctx = browser.new_context(viewport={"width": 1280, "height": 1000})
        self.page = self.ctx.new_page()
        self.errors: list[str] = []
        self.requests: list[str] = []
        self.bad_api: list[str] = []
        self.page.on("console", lambda m: self.errors.append(f"console.{m.type}: {m.text}") if m.type in ("error", "warning") else None)
        self.page.on("pageerror", lambda e: (self.errors.append(f"pageerror: {e}"), print(f"Browser page error: {e}")))
        self.page.on("request", lambda r: self.requests.append(r.url))
        self.page.on("response", lambda r: (self.bad_api.append(f"{r.status} {r.url}: {r.text()}"), print(self.bad_api[-1])) if r.status >= 400 else None)

    def mounted(self, hash_: str, selector: str, timeout=8000) -> str:
        pg = self.page
        pg.evaluate("(h) => { location.hash = h }", hash_[1:])
        pg.wait_for_selector(f"main[data-page] {selector}".replace(", ", ", main[data-page] "), timeout=timeout)
        pg.wait_for_timeout(250)
        text = pg.locator("main").inner_text()
        for bad in NOT_MOUNTED:
            assert bad not in text, f"{hash_}: {text[:200]!r}"
        return text

    def close(self):
        self.ctx.close()


def walk_everything(w: Walk, post_id: str, *, accounts: int, caption: str):
    pg = w.page
    pg.goto(w.srv.url() + "#/pipeline")
    pg.wait_for_selector("main[data-page=pipeline]", timeout=10000)
    for hash_, sel, _ in PAGES:
        w.mounted(hash_, sel)
        assert pg.locator("main").get_attribute("data-page") == hash_[2:].split("/")[0].split("?")[0]
    w.mounted("#/grid/ig-main", ".g-list")
    pg.wait_for_selector(".gcell", timeout=5000)
    w.mounted(f"#/post/{post_id}", ".ed")                 # the editor (post route -> editor.js -> mountEditorFromHash)
    pg.locator('.ed textarea[data-path$=".copy.caption"]').wait_for(timeout=5000)
    assert caption in pg.locator('.ed textarea[data-path$=".copy.caption"]').input_value() + pg.locator("main").inner_text()
    # every retired page says it moved, and the nav names only the four tools and the way back
    for gone in ("#/calendar", "#/review", "#/library", "#/feedback", "#/alerts", "#/accounts", "#/campaigns", "#/desk", "#/ideas", "#/phone"):
        pg.evaluate("(h) => { location.hash = h }", gone[1:])
        pg.wait_for_function("() => document.querySelector('main').innerText.includes('That page moved to the app')")
    assert [t.strip() for t in pg.locator("#nav a").all_inner_texts()] == ["Production", "Media tools", "Feed planner", "Back to the Workspace"]
    pg.locator('#nav a[data-p="workbench"]').click()
    pg.wait_for_selector("main[data-page=workbench]")
    assert pg.locator('#nav a[aria-current="page"]').get_attribute("data-p") == "workbench"


def finish(w: Walk):
    ext = [u for u in w.requests if not (u.startswith(f"http://127.0.0.1:{w.srv.port}/") or u.startswith("data:") or u.startswith("blob:"))]
    assert not ext, f"non-loopback requests: {ext}"
    assert not w.bad_api, w.bad_api
    assert not w.errors, w.errors


@pytest.mark.legacy_ui
def test_the_real_shell_mounts_every_page_in_fixtures_mode(browser):
    srv = serve(FixtureStudio())
    w = Walk(browser, srv)
    try:
        w.page.goto(srv.url())
        w.page.wait_for_selector("main[data-page]", timeout=10000)
        walk_everything(w, "p_20261102_k3f9", accounts=2, caption="starter smells like pears")
        finish(w)
    finally:
        w.close()
        srv.shutdown()
        srv.server_close()


@pytest.mark.legacy_ui
def test_the_real_shell_mounts_every_page_over_a_real_wired_studio(browser, tmp_path):
    b = build_studio(tmp_path)
    st = open_studio(b.sp.root, clock=b.clock, publishers=b.publishers)
    srv = serve(st)
    w = Walk(browser, srv)
    try:
        w.page.goto(srv.url())
        w.page.wait_for_selector("main[data-page]", timeout=10000)
        walk_everything(w, b.posts["in_review"], accounts=3, caption="starter smells like pears")
        finish(w)
    finally:
        w.close()
        srv.shutdown()
        srv.server_close()
