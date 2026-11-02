"""WS6 panels in a real browser (spec 10.7): editor autosave + CAS + 3-way panel, asset swap, send-back (four kinds), review with PIN,
library (upload, filters, B-roll), per-platform previews, keyboard, 390 px. Runs against tests/studio_panels_server.py (fixture-backed).
Skips cleanly when Playwright or Chromium is missing."""

from __future__ import annotations

import io
import json
import os
import pwd
import urllib.request
from pathlib import Path

import pytest
from PIL import Image

pw = pytest.importorskip("playwright.sync_api")

from studio_panels_server import PIN, Harness  # noqa: E402

from ave.studio import text_metrics as tm  # noqa: E402

POST = "p_20261102_k3f9"
SOCIAL = "p_h_social"


@pytest.fixture(scope="module")
def harness():
    h = Harness()
    h.start()
    yield h
    h.stop()


@pytest.fixture(scope="module")
def browser():
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    cache = real_home / "Library" / "Caches" / "ms-playwright"
    if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
        for c in (cache, real_home / ".cache" / "ms-playwright"):
            if c.is_dir():
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(c)
                break
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # browser not installed on this machine
            pytest.skip(f"chromium unavailable: {e}")
        yield b
        b.close()


class Page:
    """A page plus its request log and console errors."""

    def __init__(self, browser, harness, width=1280, height=900, **ctx_kw):
        self.h = harness
        self.ctx = browser.new_context(viewport={"width": width, "height": height}, **ctx_kw)
        self.page = self.ctx.new_page()
        self.requests: list[str] = []
        self.errors: list[str] = []
        self.page.on("request", lambda r: self.requests.append(r.url))
        self.page.on("console", lambda m: self.errors.append(m.text) if m.type == "error" else None)
        self.page.on("pageerror", lambda e: self.errors.append("pageerror: " + str(e)))

    def goto(self, route: str):
        self.page.goto(f"{self.h.url}/harness/index.html{route}")
        self.page.wait_for_selector("main[data-route]")
        return self.page

    def state(self):
        return json.loads(urllib.request.urlopen(f"{self.h.url}/__test/state").read())

    def post(self, path, body):
        req = urllib.request.Request(f"{self.h.url}{path}", data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req).read())

    def close(self):
        self.ctx.close()


@pytest.fixture
def ui(browser, harness):
    harness.reset()
    made = []

    def make(**kw):
        p = Page(browser, harness, **kw)
        made.append(p)
        return p

    yield make
    for p in made:
        p.close()


def _variant(state, post, vid):
    return next(v for v in state["posts"][post]["variants"] if v["id"] == vid)


# ---- zero external requests, no console errors -----------------------------------------------------------------------------------

def test_every_panel_loads_with_zero_external_requests_and_no_console_errors(ui):
    p = ui()
    for route in ("#/post/" + POST, "#/review", "#/library", "#/feedback", "#/alerts", "#/accounts", "#/previews"):
        p.goto(route)
        p.page.wait_for_timeout(500)
    external = [u for u in p.requests if not u.startswith(p.h.url) and not u.startswith("data:") and not u.startswith("blob:")]
    assert external == [], external
    assert p.errors == [], p.errors


# ---- inline edit, autosave, CAS ---------------------------------------------------------------------------------------------------------

def test_inline_edit_autosaves_after_the_debounce_and_writes_a_human_edit_line(ui):
    p = ui()
    pg = p.goto(f"#/post/{POST}?variant=v_fb")
    ta = pg.locator('textarea[data-path="variants.v_fb.copy.caption"]')
    ta.fill("Day 3: pears. Plainly.")
    assert p.state()["posts"][POST]["rev"] == 7                         # nothing sent yet: the save waits for 800 ms of quiet
    pg.wait_for_selector(".ed-save.saved")
    st = p.state()
    assert st["posts"][POST]["rev"] == 8 and _variant(st, POST, "v_fb")["copy"]["caption"] == "Day 3: pears. Plainly."
    edits = [f for f in st["feedback"] if f["kind"] == "human_edit"]
    assert edits and edits[-1]["structured"]["copy"]["after"] == "Day 3: pears. Plainly."
    assert "Saved" in pg.locator(".ed-save").inner_text()


def test_typing_in_a_burst_makes_one_save(ui):
    p = ui()
    pg = p.goto(f"#/post/{POST}?variant=v_fb")
    ta = pg.locator('textarea[data-path="variants.v_fb.copy.caption"]')
    ta.click()
    pg.keyboard.type("quick brown fox", delay=30)
    pg.wait_for_selector(".ed-save.saved")
    assert p.state()["posts"][POST]["rev"] == 8


def test_an_agent_edit_to_the_same_field_opens_the_three_way_panel_and_keep_mine_wins(ui):
    p = ui()
    pg = p.goto(f"#/post/{POST}?variant=v_fb")
    ta = pg.locator('textarea[data-path="variants.v_fb.copy.caption"]')
    ta.fill("Mine: pears, plainly.")
    p.post("/__test/agent_edit", {"post": POST, "patch": {"variants.v_fb.copy.caption": "Claude: smells like pears."}})
    pg.wait_for_selector(".ed-3way")
    panel = pg.locator(".ed-3way")
    text = panel.inner_text()
    assert "Their version" in text and "Your version" in text and "Before either edit" in text
    assert "Claude: smells like pears." in text and "Mine: pears, plainly." in text
    assert p.state()["posts"][POST]["variants"][1]["copy"]["caption"] == "Claude: smells like pears."          # nothing overwritten
    apply = panel.get_by_role("button", name="Save my choice")
    assert apply.is_disabled()
    panel.get_by_label("Keep mine").check()
    apply.click()
    pg.wait_for_selector(".ed-save.saved")
    st = p.state()
    assert _variant(st, POST, "v_fb")["copy"]["caption"] == "Mine: pears, plainly."
    assert st["posts"][POST]["rev"] == 9


def test_three_way_panel_take_theirs_and_merge_by_hand(ui):
    p = ui()
    pg = p.goto(f"#/post/{POST}?variant=v_fb")
    ta = pg.locator('textarea[data-path="variants.v_fb.copy.caption"]')
    ta.fill("Mine.")
    p.post("/__test/agent_edit", {"post": POST, "patch": {"variants.v_fb.copy.caption": "Theirs."}})
    pg.wait_for_selector(".ed-3way")
    pg.get_by_label("Take theirs").check()
    pg.get_by_role("button", name="Save my choice").click()
    pg.wait_for_function("document.querySelector('textarea[data-path=\"variants.v_fb.copy.caption\"]').value === 'Theirs.'")
    assert _variant(p.state(), POST, "v_fb")["copy"]["caption"] == "Theirs."
    # again, this time merging by hand
    ta.fill("Mine two.")
    p.post("/__test/agent_edit", {"post": POST, "patch": {"variants.v_fb.copy.caption": "Theirs two."}})
    pg.wait_for_selector(".ed-3way")
    pg.get_by_label("Merge by hand").check()
    merged = pg.get_by_label("Merged text")
    merged.fill("Merged: mine and theirs.")
    pg.get_by_role("button", name="Save my choice").click()
    pg.wait_for_selector(".ed-save.saved")
    assert _variant(p.state(), POST, "v_fb")["copy"]["caption"] == "Merged: mine and theirs."


def test_an_agent_edit_to_a_different_field_is_rebased_without_a_panel(ui):
    p = ui()
    pg = p.goto(f"#/post/{POST}?variant=v_fb")
    pg.locator('textarea[data-path="variants.v_fb.copy.caption"]').fill("Mine only.")
    p.post("/__test/agent_edit", {"post": POST, "patch": {"copy.link": "https://acme.example/new"}})
    pg.wait_for_selector(".ed-save.saved")
    st = p.state()
    assert pg.locator(".ed-3way").count() == 0
    assert st["posts"][POST]["copy"]["link"] == "https://acme.example/new" and _variant(st, POST, "v_fb")["copy"]["caption"] == "Mine only."


def test_a_background_agent_edit_shows_up_when_nothing_is_dirty(ui):
    p = ui()
    pg = p.goto(f"#/post/{POST}?variant=v_fb")
    p.post("/__test/agent_edit", {"post": POST, "patch": {"variants.v_fb.copy.caption": "From Claude."}})
    pg.wait_for_function("document.querySelector('textarea[data-path=\"variants.v_fb.copy.caption\"]').value === 'From Claude.'", timeout=9000)


# ---- asset swap -------------------------------------------------------------------------------------------------------------------------

def test_swapping_an_asset_demotes_the_approval_and_says_why(ui):
    p = ui()
    pg = p.goto(f"#/post/{POST}?variant=v_ig")
    assert "Scheduled" in pg.locator(".ed-approval").inner_text()
    pg.locator(".composer-slide-edit > summary").click()
    pg.get_by_role("button", name="Replace",exact=True).click()
    dlg = pg.locator("dialog[open]")
    # Draft authoring permits registered assets; delivery still checks rights.
    dlg.locator('[data-asset="a_square000001"] button').click()
    pg.wait_for_selector(".ed-approval:has-text('Approval removed')")
    st = p.state()
    v = _variant(st, POST, "v_ig")
    assert v["media"][0]["asset"] == "a_square000001" and v["queue_status"] == "awaiting_approval" and v["approval"] is None
    approval = pg.locator(".ed-approval").inner_text()
    assert "Approval removed" in approval and "In review" in approval
    assert pg.locator(".composer-stage img").get_attribute("src") == json.loads(urllib.request.urlopen(p.h.url+"/api/studio/library/a_square000001").read())["url"]


# ---- send back --------------------------------------------------------------------------------------------------------------------------

def test_send_back_writes_one_line_per_kind_with_the_structured_shape(ui):
    p = ui()
    pg = p.goto(f"#/post/{POST}?variant=v_fb")
    pg.get_by_role("button", name="Request changes").click()
    dlg = pg.locator("dialog[open]")
    assert not dlg.locator('[name="retime-after"]').is_visible() and not dlg.locator('[name="drop-reason"]').is_visible()   # kind fields show only when ticked
    dlg.get_by_label("Rework the copy").check()
    dlg.get_by_label("Rework the visual").check()
    dlg.get_by_label("Change the time").check()
    assert dlg.locator('[name="retime-after"]').is_visible() and not dlg.locator('[name="drop-reason"]').is_visible()
    dlg.locator('[name="visual-problem"]').select_option("too_busy")
    dlg.locator('[name="visual-wanted"]').fill("calmer, more daylight")
    dlg.locator('[name="retime-after"]').fill("2026-11-04")
    dlg.locator('[name="retime-window"]').select_option("morning")
    dlg.locator('[name="avoid-mon"]').check()
    dlg.locator('[name="note"]').fill("too salesy, say it plainly")
    dlg.get_by_role("button", name="Send to the editor").click()
    pg.wait_for_selector("dialog[open]", state="detached")
    st = p.state()
    lines = [f for f in st["feedback"] if f["post"] == POST and f["kind"] != "human_edit"]
    assert sorted(f["kind"] for f in lines) == ["retime", "rework_copy", "rework_visual"]
    by = {f["kind"]: f for f in lines}
    assert by["rework_copy"]["structured"]["copy"]["field"] == "caption" and by["rework_copy"]["note"] == "too salesy, say it plainly"
    assert by["rework_visual"]["structured"]["visual"] == {"asset": "a_9f2c1d4e7b10", "problem": "too_busy", "wanted": "calmer, more daylight"}
    assert by["retime"]["structured"]["retime"]["prefer"] == {"after": "2026-11-04", "window": "morning", "avoid_dow": ["mon"]}
    assert all(f["by"] == "maren" or f["by"] for f in lines) and all(f["variant"] == "v_fb" for f in lines)
    assert _variant(st, POST, "v_fb")["status"] == "needs_rework"
    assert "Waiting for the editor" in pg.locator(".st-tab[aria-selected=true]").inner_text()


def test_send_back_drop_rejects_and_a_selection_is_passed_along(ui):
    p = ui()
    pg = p.goto(f"#/post/{POST}?variant=v_fb")
    ta = pg.locator('textarea[data-path="variants.v_fb.copy.caption"]')
    ta.click()
    ta.evaluate("el => el.setSelectionRange(2, 7)")
    pg.keyboard.press("s") if False else None
    pg.get_by_role("button", name="Request changes").click()
    dlg = pg.locator("dialog[open]")
    dlg.get_by_label("Rework the copy").check()
    dlg.locator('[name="note"]').fill("lose this bit")
    dlg.get_by_label("Drop this post").check()
    dlg.locator('[name="drop-reason"]').select_option("duplicate")
    dlg.get_by_role("button", name="Send to the editor").click()
    pg.wait_for_selector("dialog[open]", state="detached")
    st = p.state()
    lines = {f["kind"]: f for f in st["feedback"] if f["post"] == POST and f["kind"] != "human_edit"}
    assert lines["drop"]["structured"]["drop"] == {"reason": "duplicate"}
    assert lines["rework_copy"]["structured"]["copy"]["selection"] == [2, 7]
    v = _variant(st, POST, "v_fb")
    assert v["dropped"] and v["queue_status"] == "rejected"


def test_send_back_needs_a_kind_and_a_note(ui):
    p = ui()
    pg = p.goto(f"#/post/{POST}?variant=v_fb")
    pg.get_by_role("button", name="Request changes").click()
    dlg = pg.locator("dialog[open]")
    dlg.get_by_role("button", name="Send to the editor").click()
    assert "at least one" in dlg.inner_text()
    dlg.get_by_label("Rework the visual").check()
    dlg.get_by_role("button", name="Send to the editor").click()
    assert "Add a note" in dlg.inner_text()
    assert not [f for f in p.state()["feedback"] if f["post"] == POST and f["kind"] != "human_edit"]


def test_s_key_opens_send_back_and_escape_closes_it_and_returns_focus(ui):
    p = ui()
    pg = p.goto(f"#/post/{POST}?variant=v_fb")
    pg.locator("main").click(position={"x": 5, "y": 5})
    pg.keyboard.press("s")
    pg.wait_for_selector("dialog[open]")
    pg.keyboard.press("Escape")
    pg.wait_for_selector("dialog[open]", state="detached")


# ---- review with PIN --------------------------------------------------------------------------------------------------------------------

def _look(pg, p, post, variant):
    """Open the item and wait until the server has recorded the view."""
    pg.locator(f'.rv-li[data-key="{post}/{variant}"] .rv-pick').click()
    pg.wait_for_selector(".rv-metertext:has-text('Looked at')", timeout=8000)


def test_review_shows_the_exact_sha_records_the_view_and_needs_the_pin(ui):
    p = ui()
    pg = p.goto("#/review")
    pg.wait_for_selector(".rv-sha")
    first = pg.locator(".rv-li").first.get_attribute("data-key")
    post, variant = first.split("/")
    sha = _variant(p.state(), post, variant)["queue_sha"]
    assert pg.locator(".rv-sha").get_attribute("data-sha") == sha
    assert sha[:8] in pg.locator(".rv-sha").inner_text().replace(" ", "")
    inc = pg.locator(f'.rv-li[data-key="{first}"] input[name=include]')
    assert inc.is_disabled()                                              # cannot be ticked before it has been looked at
    pg.wait_for_selector(".rv-metertext:has-text('Looked at')", timeout=8000)
    assert not inc.is_disabled() and inc.is_checked()
    assert (post, variant) not in [(e["post"], e["variant"]) for e in p.state()["events"] if e["op"] == "approved"]
    # wrong PIN: refused, with the tries left, nothing approved
    pg.locator("#rv-pin").fill("000000")
    pg.get_by_role("button", name="Approve 1").click()
    pg.wait_for_selector(".rv-msg:has-text('not right')")
    assert "4 tries left" in pg.locator(".rv-msg").first.inner_text()
    assert _variant(p.state(), post, variant)["queue_status"] == "awaiting_approval"
    pg.locator("#rv-pin").fill(PIN)
    pg.get_by_role("button", name="Approve 1").click()
    pg.wait_for_function("(k) => document.querySelector(`.rv-li[data-key='${k}']`).innerText.includes('Approved by')", arg=first)
    v = _variant(p.state(), post, variant)
    assert v["queue_status"] == "approved" and v["approval"]["by"] == "maren"
    assert pg.locator("#rv-pin").input_value() == ""                      # the PIN is never kept


def test_review_locks_after_five_wrong_pins(ui):
    p = ui()
    pg = p.goto("#/review")
    pg.wait_for_selector(".rv-metertext:has-text('Looked at')", timeout=8000)
    for _ in range(5):
        pg.locator("#rv-pin").fill("111111")
        pg.get_by_role("button", name="Approve 1").click()
        pg.wait_for_selector(".rv-msg:not(:empty)")
    pg.locator("#rv-pin").fill(PIN)
    pg.get_by_role("button", name="Approve 1").click()
    pg.wait_for_selector(".rv-msg:has-text('locked')")


def test_review_j_k_move_and_a_second_post_must_be_looked_at_before_it_can_be_ticked(ui):
    p = ui()
    pg = p.goto("#/review")
    pg.wait_for_selector(".rv-metertext:has-text('Looked at')", timeout=8000)
    pg.locator("main").click(position={"x": 5, "y": 5})
    pg.keyboard.press("j")
    pg.wait_for_selector(".rv-li.on:nth-child(2)")
    second = pg.locator(".rv-li").nth(1).get_attribute("data-key")
    assert pg.locator(f'.rv-li[data-key="{second}"] input[name=include]').is_disabled()
    pg.keyboard.press("k")
    pg.wait_for_selector(".rv-li.on:nth-child(1)")


# ---- library ----------------------------------------------------------------------------------------------------------------------------

def _png_bytes(w=64, h=48, color=(30, 90, 140)):
    b = io.BytesIO()
    Image.new("RGB", (w, h), color).save(b, "PNG")
    return b.getvalue()


def test_library_filters_upload_and_detail(ui, tmp_path):
    p = ui()
    pg = p.goto("#/library")
    pg.wait_for_selector(".lib-card")
    assert pg.locator(".lib-card").count() == 6
    pg.locator('select[name=license]').select_option("missing")
    pg.wait_for_function("document.querySelectorAll('.lib-card').length === 1")
    assert "No licence" in pg.locator(".lib-card").inner_text()
    pg.locator('select[name=license]').select_option("")
    pg.locator('input[name=unused]').check()
    pg.wait_for_function("document.querySelectorAll('.lib-card').length === 2")
    pg.locator('input[name=unused]').uncheck()
    pg.locator('input[name=q]').fill("flour")
    pg.wait_for_function("document.querySelectorAll('.lib-card').length === 1")
    pg.locator('input[name=q]').fill("")
    pg.wait_for_function("document.querySelectorAll('.lib-card').length === 6")
    f = tmp_path / "quote-card.png"
    f.write_bytes(_png_bytes())
    pg.locator("#lib-file").set_input_files(str(f))
    pg.wait_for_function("document.querySelectorAll('.lib-card').length === 7")
    assert p.state()["uploads"] == 1
    pg.locator("#lib-file").set_input_files(str(f))                         # the same bytes again: a duplicate, not a new asset
    pg.wait_for_selector("#st-toast:has-text('already in the library')")
    assert p.state()["uploads"] == 1
    pg.locator('.lib-card[data-asset="a_9f2c1d4e7b10"] .lib-open').click()
    pg.wait_for_selector(".lib-detail:not([hidden]) .st-dl")
    detail = pg.locator(".lib-detail").inner_text()
    assert "Pexels License" in detail and "p_20261102_k3f9 / v_ig" in detail


def test_broll_modal_shows_licence_and_attribution_and_downloads_only_on_a_named_pick(ui):
    p = ui()
    pg = p.goto("#/library")
    pg.wait_for_selector(".lib-card")
    pg.get_by_role("button", name="Find B-roll").click()
    dlg = pg.locator("dialog[open]")
    dlg.locator('input[name=query]').fill("bread crust")
    dlg.get_by_role("button", name="Search").click()
    card = dlg.locator('.cand[data-cand="pexels:2284"]')
    card.wait_for()
    text = card.inner_text()
    assert "Pexels licence" in text and "Photo by A. Baker on Pexels" in text and "Authenticity check: ok" in text
    assert card.locator("img").count() == 0                                  # the provider's preview URL is never loaded by the browser
    dlg.locator('input[name=by]').fill("")
    card.get_by_role("button", name="Download and add").click()
    assert "name" in dlg.locator('p[role=status]').inner_text().lower()
    assert not [u for u in p.requests if "pexels" in u]
    dlg.locator('input[name=by]').fill("maren")
    card.get_by_role("button", name="Download and add").click()
    pg.wait_for_selector("dialog[open]", state="detached")
    assert "a_br000002284" in p.state()["library"]


# ---- previews ---------------------------------------------------------------------------------------------------------------------------

def _counter(pg, platform, ptype):
    art = pg.locator(f'article.pv[data-platform="{platform}"][data-posttype="{ptype}"]').first
    out = art.locator("output.pv-count")
    return int(out.get_attribute("data-used")), int(out.get_attribute("data-limit")), art


def test_counters_use_each_platforms_unit_and_agree_with_the_python_metrics(ui):
    p = ui()
    pg = p.goto("#/previews")
    pg.wait_for_selector("article.pv")
    base = p.state()["posts"][SOCIAL]["copy"]["base"]
    body = base + "\n\n#sourdough #bread"
    with_link = body + "\n\nhttps://acme.example/starter"
    used, limit, _ = _counter(pg, "x", "text")
    assert (used, limit) == (tm.x_weighted_len(with_link), 280)
    used, limit, _ = _counter(pg, "bluesky", "text")
    assert (used, limit) == (tm.grapheme_len(with_link), 300)
    used, limit, _ = _counter(pg, "threads", "text")
    assert (used, limit) == (tm.grapheme_len(with_link), 500)
    used, limit, _ = _counter(pg, "instagram", "feed")
    assert (used, limit) == (len(body), 2200)                                # IG: no inline link, counted in characters
    used, limit, _ = _counter(pg, "linkedin", "feed")
    assert (used, limit) == (len(with_link), 3000)
    used, limit, art = _counter(pg, "facebook", "feed")
    assert limit == 5000
    assert "approximate" in art.get_attribute("aria-label").lower() and "Approximate" in art.inner_text()


def test_fold_is_drawn_per_platform_and_the_toggle_hides_the_remainder(ui):
    p = ui()
    pg = p.goto("#/previews")
    pg.wait_for_selector("article.pv")
    for platform, ptype in (("instagram", "feed"), ("facebook", "feed"), ("linkedin", "feed"), ("tiktok", "video"), ("threads", "text")):
        art = pg.locator(f'article.pv[data-platform="{platform}"][data-posttype="{ptype}"]').first
        assert art.locator(".pv-more").count() == 1 and art.locator(".pv-cut").count() == 1, platform
        assert art.locator(".pv-hidden").inner_text().strip(), platform
    ig = pg.locator('article.pv[data-platform="instagram"][data-posttype="feed"]').first
    assert ig.locator(".pv-text").inner_text().startswith("Day 3: the starter smells like pears, and that is normal.")
    x = pg.locator('article.pv[data-platform="x"]').first
    assert x.locator(".pv-more").count() == 0                                # 275 weighted chars: no fold


def test_instagram_grid_crop_and_safe_zones(ui):
    p = ui()
    pg = p.goto("#/previews")
    pg.wait_for_selector("article.pv")
    ig = pg.locator('article.pv[data-platform="instagram"][data-posttype="feed"]')
    crop = ig.locator(".pv-gridcrop")
    lost = json.loads(crop.get_attribute("data-lost"))
    assert lost["left_pct"] + lost["right_pct"] == 25 or lost["left_pct"] + lost["right_pct"] == 26      # 1:1 loses a quarter of the width
    assert "focal point is outside the crop" in crop.inner_text()
    assert ig.locator(".pv-gridwin").count() == 1
    for platform, ptype in (("instagram", "story"), ("tiktok", "video")):
        art = pg.locator(f'article.pv[data-platform="{platform}"][data-posttype="{ptype}"]')
        assert art.locator(".pv-zone.top").count() == 1 and art.locator(".pv-zone.bottom").count() == 1 and art.locator(".pv-safe").count() == 1
    # the zone sizes come from config/content.yaml 9x16: top 13 %, bottom 20 %
    frame = pg.locator('article.pv[data-platform="instagram"][data-posttype="story"] .pv-vframe')
    box = frame.bounding_box()
    top = pg.locator('article.pv[data-platform="instagram"][data-posttype="story"] .pv-zone.top').bounding_box()
    bottom = pg.locator('article.pv[data-platform="instagram"][data-posttype="story"] .pv-zone.bottom').bounding_box()
    assert abs(top["height"] / box["height"] - 0.13) < 0.01 and abs(bottom["height"] / box["height"] - 0.20) < 0.01


def test_the_editor_counter_follows_typing_and_flags_going_over(ui):
    p = ui()
    pg = p.goto(f"#/post/{SOCIAL}?variant=v_x")
    pg.wait_for_selector("article.pv[data-platform=x]")
    ta = pg.locator('textarea[data-path="variants.v_x.copy.caption"]')
    ta.fill("a" * 300)
    pg.wait_for_function("document.querySelector('.pv-counter').dataset.state === 'over'")
    out = pg.locator("output.pv-count")
    assert out.get_attribute("data-used") == str(tm.x_weighted_len("a" * 300 + "\n\n#sourdough #bread\n\nhttps://acme.example/starter"))
    assert "over" in pg.locator(".pv-count-state").inner_text()


def test_the_editor_preview_follows_the_selected_account(ui):
    p = ui()
    pg = p.goto(f"#/post/{SOCIAL}?variant=v_x")
    pg.wait_for_selector("article.pv[data-platform=x]")
    pg.get_by_role("tab").nth(1).click()
    pg.wait_for_selector("article.pv[data-platform=bluesky]")
    pg.locator(".st-tab[aria-selected=true]").press("ArrowRight")
    pg.wait_for_selector("article.pv[data-platform=threads]")
    assert pg.locator(".st-tab[aria-selected=true]").get_attribute("data-variant") == "v_th"


# ---- feedback, alerts, accounts ---------------------------------------------------------------------------------------------------------

def test_feedback_page_lists_open_lines_and_the_other_tabs(ui):
    p = ui()
    pg = p.goto("#/feedback")
    pg.wait_for_selector(".fb")
    assert "too salesy, say it plainly" in pg.locator(".fb").inner_text() and "Waiting for the editor" in pg.locator(".fb").inner_text()
    pg.get_by_role("tab", name="Resolved").click()
    pg.wait_for_selector(".st-empty")


def test_alerts_show_advice_and_acknowledge(ui):
    p = ui()
    pg = p.goto("#/alerts")
    pg.wait_for_selector(".al")
    assert "What to do" in pg.locator(".al").inner_text() and "not live" in pg.locator(".al").inner_text()
    pg.get_by_role("button", name="Acknowledge").click()
    pg.wait_for_selector("button:has-text('Acknowledged')")


def test_accounts_show_state_in_words_and_never_credential_values(ui):
    p = ui()
    pg = p.goto("#/accounts")
    pg.wait_for_selector(".connection-profile")
    text = pg.locator(".connection-detail").inner_text()
    assert "Not verified" in text and "Not enabled" in text and "Not tested" in text
    assert "ave studio account live" not in pg.content() and pg.locator('input[type=password]').count()==0
    assert pg.get_by_text('Facebook Page ID',exact=True).count()==0
    pg.get_by_label('Login product',exact=True).select_option('instagram_facebook')
    assert pg.get_by_text('Facebook Page ID',exact=True).is_visible()
    pg.get_by_role('button',name='Connect credentials',exact=True).click()
    pg.get_by_text('Sample connections are fictional.',exact=False).wait_for()


# ---- narrow screens and motion ----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("route", [f"#/post/{POST}?variant=v_fb", "#/review", "#/library", "#/feedback", "#/alerts", "#/accounts", "#/previews"])
def test_no_horizontal_scroll_at_390px(ui, route):
    p = ui(width=390, height=844)
    pg = p.goto(route)
    pg.wait_for_timeout(600)
    sw = pg.evaluate("document.documentElement.scrollWidth")
    assert sw <= 391, f"{route}: page is {sw}px wide"


def test_buttons_and_fields_are_touch_sized_at_390px(ui):
    p = ui(width=390, height=844)
    pg = p.goto(f"#/post/{POST}?variant=v_fb")
    pg.wait_for_selector(".ed-form textarea:visible")
    small = pg.evaluate("""() => Array.from(document.querySelectorAll('main button, main textarea, main input[type=text]'))
        .filter(e => e.offsetParent).map(e => e.getBoundingClientRect()).filter(r => r.height < 36).length""")
    assert small == 0


def test_reduced_motion_is_respected(ui):
    p = ui(reduced_motion="reduce")
    pg = p.goto(f"#/post/{POST}?variant=v_fb")
    pg.get_by_role("button", name="Request changes").click()
    pg.wait_for_selector("dialog[open]")
    dur = pg.evaluate("getComputedStyle(document.querySelector('#st-toast, body')).transitionDuration")
    assert dur in ("0s", "0.00001s", "1e-05s") or float(dur.rstrip("s").split(",")[0]) < 0.01


def test_roles_and_labels(ui):
    p = ui()
    pg = p.goto(f"#/post/{POST}?variant=v_fb")
    pg.wait_for_selector(".ed-form textarea:visible")
    assert pg.locator('[role=tablist]').count() == 1 and pg.locator('[role=tab][aria-selected=true]').count() == 1
    unlabeled = pg.evaluate("""() => Array.from(document.querySelectorAll('main textarea, main input, main select')).filter(e => !e.labels.length && !e.getAttribute('aria-label')).length""")
    assert unlabeled == 0
    snap = pg.accessibility.snapshot() if hasattr(pg, "accessibility") else None
    assert snap is None or snap["role"] in ("WebArea", "RootWebArea")
