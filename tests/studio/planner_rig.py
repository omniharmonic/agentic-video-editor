"""A small two-brand studio, a real `StudioServer` over it and a Chromium page, for the planner UI tests (test_planner_ui.py, test_planner_review.py).

Fixed clock (Mon 19 Oct 2026, 10:00 in Denver). Three accounts with rule slots Mon/Wed/Fri 09:00: `ig-main` and `fb-page` belong to the brand "Acme Bakery",
`ig-loaf` to "Loaf Co". No network: loopback only; the keychain is the in-memory one from tests/conftest. Every page this rig opens fails the test on a
console error, a failed request or a request that is not loopback, unless the test says it expects one (`rig.allow_errors`).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

import pytest

pw = pytest.importorskip("playwright.sync_api")

from sched_helpers import make_world  # noqa: E402
from studio_helpers import write_account  # noqa: E402

from ave.studio import campaigns, store  # noqa: E402
from ave.studio.events import AGENT  # noqa: E402
from ave.studio.service import open_studio  # noqa: E402
from ave.studio.sync import sync_queue  # noqa: E402
from ave.studio.ui.server import StudioServer  # noqa: E402

PIN = "424242"
WEEK = "#/calendar?view=week&d=2026-10-19"
ACCOUNTS = (("ig-main", "instagram", "Acme Bakery"), ("fb-page", "facebook", "Acme Bakery"), ("ig-loaf", "instagram", "Loaf Co"))


@dataclass
class Rig:
    w: object
    srv: StudioServer
    browser: object
    posts: dict = field(default_factory=dict)            # name -> (post id, variant id)
    pages: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    bad: list = field(default_factory=list)
    external: list = field(default_factory=list)
    allow_errors: bool = False
    allowed: list = field(default_factory=list)          # substrings of failed requests / console errors a test expects (a wrong PIN is a 403)

    # ---- pages
    def page(self, route: str = WEEK, width: int = 1280, height: int = 900, scheme: str = "light", reduced: bool = False, wait: str = "main[data-page]"):
        ctx = self.browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme, reduced_motion="reduce" if reduced else "no-preference")
        pg = ctx.new_page()
        self.pages.append(ctx)
        pg.on("console", lambda m: self.errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
        pg.on("pageerror", lambda e: self.errors.append(f"pageerror: {e}"))
        pg.on("response", lambda r: self.bad.append(f"{r.status} {r.url}") if r.status >= 400 else None)
        pg.on("request", lambda r: self.external.append(r.url) if not (r.url.startswith(f"http://127.0.0.1:{self.srv.port}/") or r.url.startswith(("data:", "blob:"))) else None)
        pg.goto(self.srv.url() + route)
        pg.wait_for_selector(wait, timeout=10000)
        return pg

    def until(self, pg, js: str, timeout: float = 6.0, arg=None):
        """Poll a JS expression through evaluate (the studio CSP forbids wait_for_function)."""
        import time
        end = time.time() + timeout
        while time.time() < end:
            if pg.evaluate(f"(arg) => {js}", arg):
                return
            pg.wait_for_timeout(50)
        raise AssertionError(f"timed out waiting for: {js}")

    def wait(self, pg, fn, timeout: float = 6.0):
        """Poll a Python predicate over the server-side state (the browser has its own clock; the files are the truth)."""
        import time
        end = time.time() + timeout
        while time.time() < end:
            if fn():
                return
            pg.wait_for_timeout(50)
        raise AssertionError("timed out waiting for the server-side state to change")

    # ---- server-side truth
    def variant(self, name: str) -> dict:
        pid, vid = self.posts[name]
        from ave.studio.model import find_variant
        return find_variant(store.load_post(self.w.sp, pid), vid)

    def local(self, name: str):
        return (self.variant(name).get("schedule") or {}).get("local")

    def item(self, name: str) -> dict:
        pid, vid = self.posts[name]
        return self.w.item(pid, vid)

    def card(self, pg, name: str, mode: str = "week"):
        pid, vid = self.posts[name]
        return pg.locator(f'.card[data-variant="{vid}"][data-mode="{mode}"]')

    def close(self):
        for c in self.pages:
            c.close()


def retry(fn, tries: int = 8):
    """A post id is `p_<date>_<4 hex>` and the clock is fixed, so two posts can (rarely) draw the same id: draw again."""
    from ave.studio.model import RevConflict
    for i in range(tries):
        try:
            return fn()
        except RevConflict as e:
            if "already exists" not in str(e) or i == tries - 1:
                raise


def build(tmp_path, browser) -> Rig:
    w = make_world(tmp_path, accounts=tuple((a, p) for a, p, _ in ACCOUNTS))
    for aid, plat, brand in ACCOUNTS:
        write_account(w.sp, aid, plat, brand=brand)
    posts: dict[str, tuple[str, str]] = {}

    def make(name, caption, account, at, *, hashtags=None, first_comment=None, approve=False, sync=True, alt="A loaf of bread on a wooden table, crust split."):
        pid, vid = retry(lambda: w.add_post(caption, account=account, at=at, sync=False, alt=alt))
        edit = {}
        if hashtags:
            edit[f"variants.{vid}.copy.hashtags"] = hashtags
        if first_comment:
            edit[f"variants.{vid}.copy.first_comment"] = first_comment
        if edit:
            w.studio.edit_post(pid, edit, expected_rev=store.load_post(w.sp, pid)["rev"], actor=AGENT)
        if sync:
            sync_queue(w.sp, pid, publishers=w.pubs, now=w.clock.now())
        if approve:
            w.approve(pid, vid)
        posts[name] = (pid, vid)

    make("review", "Sourdough day three: the starter smells like pears. That is normal, and it means the yeast is waking up properly.", "ig-main", "2026-10-21T15:00:00Z",
         hashtags=["sourdough", "smallbakery"], first_comment="Recipe in the link in bio.")
    make("approved", "Rye loaves on Friday: dense, tangy and made for butter. Come early, they go by ten.", "ig-main", "2026-10-23T15:00:00Z", approve=True, hashtags=["rye"])
    make("draft", "Weekly special: a seeded loaf with the rye starter.", "ig-main", None, sync=False)
    make("fb", "Market day is Saturday. We will have the long loaves and the cardamom buns.", "fb-page", "2026-10-21T15:00:00Z")
    make("loaf", "Loaf Co opens its doors on Wednesday with three kinds of bread.", "ig-loaf", "2026-10-21T15:00:00Z")
    make("loaf2", "Loaf Co: Friday is rye day.", "ig-loaf", "2026-10-23T15:00:00Z")
    cid = campaigns.new(w.sp, "Spring launch", goal="Open the doors", start="2026-10-19", end="2026-11-30", accounts=["ig-loaf"])["id"]
    campaigns.add_post(w.sp, cid, posts["loaf"][0], actor=AGENT, now=w.clock.now())
    idea = retry(lambda: w.studio.create_post(idea="Behind the scenes: shaping the Saturday loaves at 5 a.m.", actor=AGENT, reason="Asked about twice this month", confidence=0.6))
    posts["idea"] = (idea["id"], "")
    st = open_studio(w.sp.root, clock=w.clock, publishers=w.pubs)
    srv = StudioServer(st, port=0, by="tester", pin=PIN)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    rig = Rig(w=w, srv=srv, browser=browser, posts=posts)
    rig.campaign = cid
    rig.add_post = lambda *a, **k: retry(lambda: w.add_post(*a, **k))
    return rig


def finish(rig: Rig):
    if rig.allow_errors:
        return
    assert not rig.external, f"non-loopback requests: {rig.external}"
    bad = [b for b in rig.bad if not any(a in b for a in rig.allowed)]
    errors = [e for e in rig.errors if not any(a in e for a in rig.allowed + (["Failed to load resource"] if rig.allowed else []))]
    assert not bad, f"failed requests: {bad}"
    assert not errors, f"console errors: {errors}"
