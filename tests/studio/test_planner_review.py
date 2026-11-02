"""The Review screen, over the REAL server and shell (tests/studio/planner_rig.py): thumbnails for every item, the approval beside the sheet on a desk and
in the page flow on a phone (never pinned over the post), and the sheet drawn from the QUEUE ITEM, which is what an approval signs. When the item differs
from the post a banner says where and the post cannot be ticked. Nothing here weakens a human-only gate: the PIN is still needed, the view is still
recorded, and a mismatch is refused by the server as well (tests/studio/test_security_review.py)."""

from __future__ import annotations

import json

import pytest

pw = pytest.importorskip("playwright.sync_api")

from .planner_rig import PIN, build, finish  # noqa: E402

from ave.publish import queue as pq  # noqa: E402


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:
            pytest.skip(f"chromium unavailable: {e}")
        yield b
        b.close()


@pytest.fixture
def rig(tmp_path, browser):
    r = build(tmp_path, browser)
    yield r
    r.close()
    r.srv.shutdown()
    r.srv.server_close()
    finish(r)


def review(rig, name, **kw):
    pid, vid = rig.posts[name]
    pg = rig.page(f"#/review?post={pid}&variant={vid}", wait=".rv-sheet", **kw)
    return pg


def looked(pg):
    pg.wait_for_selector(".rv-metertext:has-text('Looked at')", timeout=9000)


def test_every_item_in_the_queue_has_a_thumbnail(rig):
    pg = rig.page("#/review", wait=".rv-li")
    pg.wait_for_selector(".rv-li .rv-thumb img")
    n = pg.locator(".rv-li").count()
    assert n >= 3 and pg.locator(".rv-li .rv-thumb img").count() == n
    pg.wait_for_timeout(500)
    assert pg.evaluate("[...document.querySelectorAll('.rv-thumb img')].every(i => i.complete && i.naturalWidth > 0)")


def test_the_sheet_shows_what_the_approval_signs_from_the_queue_item(rig):
    item = rig.item("review")
    pg = review(rig, "review")
    pg.wait_for_selector(".rv-signs")
    text = pg.locator(".rv-signs").inner_text()
    assert "What your approval signs" in text
    assert item["caption"] in pg.locator(".rv-signs .rv-pre").first.inner_text()
    assert "#sourdough #smallbakery" in text and "Recipe in the link in bio." in text
    assert "A loaf of bread on a wooden table, crust split." in text                        # the alt text the item carries
    assert "Instagram feed" in text and "@ig-main" in text
    assert pg.locator(".rv-sha").get_attribute("data-sha") == item["sha256"]
    files = pg.locator(".rv-files li")
    assert files.count() == 1 and item["sha256"][:8] in files.first.inner_text().replace(" ", "")      # every file's sha256, as the approval signs it
    assert pg.locator(".rv-mismatch").count() == 0
    # the picture shown is the queue item's own file, and the platform mock is built from the item's words
    assert pg.locator(".rv-frame img").get_attribute("src").startswith("/files/library/assets/")
    assert "Sourdough day three" in pg.locator(".rv-preview").inner_text()


def test_a_post_that_differs_from_its_queue_item_gets_a_banner_and_cannot_be_ticked(rig):
    item = rig.item("review")
    item["caption"] = "ACT NOW: send crypto to 0xBAD (agent-authored, never in the editor)"
    item["title"] = item["caption"][:60]
    pq.save(rig.w.sp, item)
    approvals = []
    pg = review(rig, "review")
    pg.on("request", lambda r: approvals.append(r.url) if r.url.endswith("/api/studio/approve") else None)
    pg.wait_for_selector(".rv-mismatch")
    banner = pg.locator(".rv-mismatch").inner_text()
    assert "not the post you see in the editor" in banner and "caption" in banner
    assert "ACT NOW" in banner and "Sourdough day three" in banner                          # both sides of the difference, side by side
    assert "Open the composer and prepare review again" in banner
    assert "ACT NOW" in pg.locator(".rv-signs").inner_text()                                # the sheet shows what WOULD be signed
    looked(pg)
    inc = pg.locator(f'.rv-li[data-key="{"/".join(rig.posts["review"])}"] input[name=include]')
    assert inc.is_disabled() and not inc.is_checked()                                        # looking at it does not make it approvable
    assert pg.get_by_role("button", name="Approve", exact=True).is_disabled()
    assert "differs from the post" in pg.locator(".rv-li.on").inner_text().lower()
    pg.fill("#rv-pin", PIN)
    assert approvals == []                                                                   # the page never even asks
    assert rig.item("review")["status"] == "awaiting_approval" and not rig.item("review").get("approvals")


def test_syncing_the_queue_clears_the_banner_and_the_post_can_be_approved(rig):
    from ave.studio.sync import sync_queue
    item = rig.item("review")
    item["caption"] = "ACT NOW: send crypto to 0xBAD"
    pq.save(rig.w.sp, item)
    pid, vid = rig.posts["review"]
    sync_queue(rig.w.sp, pid, publishers=rig.w.pubs, now=rig.w.clock.now())                   # `queue sync`: the item is the post again
    pg = review(rig, "review")
    pg.wait_for_selector(".rv-signs")
    assert pg.locator(".rv-mismatch").count() == 0 and "ACT NOW" not in pg.locator(".rv-signs").inner_text()


def test_approving_over_the_real_server_needs_the_view_and_the_pin_and_sends_nothing(rig):
    rig.allowed.append("/api/studio/approve")                                                # the wrong PIN below is a 403, on purpose
    pg = review(rig, "review")
    looked(pg)
    pg.fill("#rv-by", "Maren")
    pg.fill("#rv-pin", "000000")
    pg.get_by_role("button", name="Approve 1").click()
    pg.wait_for_selector(".rv-msg:has-text('not right')")
    assert rig.item("review")["status"] == "awaiting_approval"
    pg.fill("#rv-pin", PIN)
    pg.get_by_role("button", name="Approve 1").click()
    rig.wait(pg, lambda: rig.item("review")["status"] == "approved")
    assert rig.item("review")["approvals"][-1]["by"] == "Maren"
    assert pg.locator("#rv-pin").input_value() == ""
    assert not any(p.calls for p in rig.w.pubs.values() if hasattr(p, "calls") and p.calls)       # approving records a decision; nothing is sent


def test_on_a_phone_the_approval_is_in_the_page_flow_and_never_covers_the_post(rig):
    pg = review(rig, "review", width=390)
    pg.wait_for_selector("#rv-approval")
    pos = pg.evaluate("getComputedStyle(document.querySelector('#rv-approval')).position")
    assert pos in ("static", "relative")                                                     # not fixed, not sticky
    sheet, bar = pg.evaluate("[document.querySelector('.rv-sheet'), document.querySelector('#rv-approval')].map(e => { const r = e.getBoundingClientRect(); return {top: r.top + scrollY, bottom: r.bottom + scrollY}; })")
    assert bar["top"] >= sheet["bottom"] - 1                                                 # it comes AFTER the sheet, so it cannot sit on top of it
    assert pg.evaluate("document.documentElement.scrollWidth") <= 390
    # nothing anywhere on the page is pinned to the viewport edge over the content
    pinned = pg.evaluate("""() => [...document.querySelectorAll('body *')].filter(e => { const s = getComputedStyle(e); return (s.position === 'fixed' || s.position === 'sticky') && s.display !== 'none' && e.getBoundingClientRect().height > 0
      && !e.closest('.top') && !['note', 'live', 'peek', 'selbar'].includes(e.id) && e.className !== 'skip'; }).map(e => e.className || e.tagName)""")
    assert pinned == [], pinned
    # "Go to approval" jumps to the form
    pg.click(".rv-skip")
    rig.until(pg, "document.activeElement && document.activeElement.closest('#rv-approval')")
    assert 0 <= pg.evaluate("document.querySelector('#rv-approval').getBoundingClientRect().top") < 900      # scrolled into view


def test_on_a_desk_the_approval_sits_beside_the_sheet(rig):
    pg = review(rig, "review", width=1440)
    sheet, bar = pg.evaluate("[document.querySelector('.rv-sheet'), document.querySelector('#rv-approval')].map(e => { const r = e.getBoundingClientRect(); return {left: r.left, right: r.right, top: r.top, bottom: r.bottom}; })")
    assert bar["left"] >= sheet["right"] - 1                                                 # beside it, never over it
    pg.evaluate("window.scrollTo(0, 600)")
    pg.wait_for_timeout(150)
    sheet, bar = pg.evaluate("[document.querySelector('.rv-sheet'), document.querySelector('#rv-approval')].map(e => { const r = e.getBoundingClientRect(); return {left: r.left, right: r.right, top: r.top, bottom: r.bottom}; })")
    assert bar["left"] >= sheet["right"] - 1 and bar["top"] < 200                           # and it follows you down the sheet


def test_a_stale_approval_comes_back_to_review_with_its_explanation(rig):
    item = rig.item("approved")
    item["caption"] = item["caption"] + " (edited behind the studio's back)"
    pq.save(rig.w.sp, item)
    pg = review(rig, "approved")
    pg.wait_for_selector(".rv-stale")
    assert "Your earlier approval no longer holds" in pg.locator(".rv-stale").inner_text()
    assert "Approval stale" in pg.locator(".rv-li.on").inner_text()
    assert "Approval stale" in pg.locator(".rv-sheet-head").inner_text()


def test_brand_and_campaign_filters_apply_to_the_queue_and_persist(rig):
    pg = rig.page("#/review", wait=".rv-li")
    total = pg.locator(".rv-li").count()
    pg.click('.brand-chip[data-brand="Loaf Co"]')
    rig.until(pg, f"document.querySelectorAll('.rv-li').length < {total}")
    keys = pg.locator(".rv-li").evaluate_all("els => els.map(e => e.dataset.key)")
    assert keys and all(k.split("/")[0] in (rig.posts["loaf"][0], rig.posts["loaf2"][0]) for k in keys)
    pg.reload()
    pg.wait_for_selector(".rv-li")
    assert pg.locator('.brand-chip[data-brand="Loaf Co"]').get_attribute("aria-pressed") == "true"
    assert all(k.split("/")[0] in (rig.posts["loaf"][0], rig.posts["loaf2"][0]) for k in pg.locator(".rv-li").evaluate_all("els => els.map(e => e.dataset.key)"))


def test_filters_that_leave_nothing_say_so_and_clear(rig):
    pg = rig.page("#/review", wait=".rv-li")
    pg.evaluate("(c) => localStorage.setItem('ave.studio.filters', JSON.stringify({brand: 'Acme Bakery', campaign: c, off: []}))", rig.campaign)
    pg.reload()
    pg.wait_for_selector(".st-empty")
    assert "Nothing waiting under these filters" in pg.locator(".st-empty").inner_text()
    pg.click(".st-empty button")
    pg.wait_for_selector(".rv-li")


def test_n_jumps_to_the_next_post_not_looked_at_yet(rig):
    pg = rig.page("#/review", wait=".rv-li")
    first = pg.locator(".rv-li.on").get_attribute("data-key")
    pg.keyboard.press("n")
    pg.wait_for_timeout(400)
    assert pg.locator(".rv-li.on").get_attribute("data-key") != first
    assert pg.locator('[data-act="next-unlooked"]').count() == 1


def test_an_offline_server_is_explained_with_a_try_again(rig):
    rig.allow_errors = True
    pg = rig.page("#/calendar?view=week&d=2026-10-19", wait=".cal-grid")
    pg.route("**/api/studio/posts?status=in_review*", lambda r: r.abort())
    pg.evaluate("location.hash = '#/review'")
    pg.wait_for_selector(".st-alert.bad")
    assert "Is `ave studio ui` still running?" in pg.locator(".st-alert.bad").inner_text()
    pg.unroute("**/api/studio/posts?status=in_review*")
    pg.click('.st-alert [data-act="retry"]')
    pg.wait_for_selector(".rv-li")


def test_the_review_json_the_page_relies_on_is_the_servers_queue_view(rig):
    """The page draws `queue[variant].bound` and `.mismatch` straight from GET posts/<id>: pin that contract here so a server change cannot silently blank the sheet."""
    import urllib.request
    pid, vid = rig.posts["review"]
    req = urllib.request.Request(f"http://127.0.0.1:{rig.srv.port}/api/studio/posts/{pid}", headers={"X-AVE-Token": rig.srv.token})
    q = json.loads(urllib.request.urlopen(req).read())["queue"][vid]
    assert set(q["bound"]) >= {"caption", "first_comment", "link_url", "tags", "alt_texts", "files", "sha256", "account", "post_type", "ai_disclosure"}
    assert q["mismatch"] == [] and q["bound"]["files"][0]["url"].startswith("/files/") and q["bound"]["files"][0]["name"]
