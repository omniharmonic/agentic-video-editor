"""The planner features of the studio UI, each driven in headless Chromium through the REAL server and shell over a small two-brand studio
(tests/studio/planner_rig.py): a non-drag reschedule (date and time picker, next free slot), multi-select with bulk actions and undo, brand and
campaign filters that persist, the side preview, the Ideas page, the explanation chips, empty and error states, and the keyboard help. The review
screen has its own file (test_planner_review.py).

Every flow also asserts the human-only gates stay put: nothing here approves anything, and retiming an APPROVED post keeps its approval."""

from __future__ import annotations

import pytest

pw = pytest.importorskip("playwright.sync_api")

from .planner_rig import WEEK, build, finish  # noqa: E402

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


def open_peek(rig, pg, name):
    rig.card(pg, name).locator(".card-link").click()
    pg.wait_for_selector("#peek.on .peek-actions")


def note(pg):
    return pg.locator("#note")


# ---------------------------------------------------------------------------------------------------- (1) reschedule without dragging

def test_a_card_opens_a_side_preview_not_the_editor(rig):
    pg = rig.page()
    pg.wait_for_selector(".cal-grid .card")
    open_peek(rig, pg, "review")
    assert pg.evaluate("location.hash").startswith("#/calendar")                      # still on the calendar
    assert pg.locator("#peek .peek-title").inner_text().startswith("Sourdough day three")
    assert pg.locator("#peek [data-act='open-editor']").get_attribute("href").startswith("#/post/")


def test_change_time_with_the_date_and_time_picker_then_undo_with_z(rig):
    pg = rig.page()
    pg.wait_for_selector(".cal-grid .card")
    open_peek(rig, pg, "review")
    pg.click("#peek [data-act='change-time']")
    pg.wait_for_selector("dialog[open] #rs-date")
    assert pg.input_value("#rs-date") == "2026-10-21" and pg.input_value("#rs-time") == "09:00"
    pg.fill("#rs-date", "2026-10-22")
    pg.fill("#rs-time", "14:30")
    pg.click("dialog[open] button[type=submit]")
    rig.until(pg, "!document.querySelector('dialog[open]')")
    assert rig.local("review") == "2026-10-22T14:30"                                  # an exact time, not snapped to a slot
    assert "Moved" in note(pg).inner_text() and note(pg).locator(".note-act").inner_text().strip() == "Undo"
    assert rig.item("review")["status"] == "awaiting_approval"                        # moving never approves
    pg.keyboard.press("z")                                                            # undo from anywhere
    rig.until(pg, "document.querySelector('#note').innerText.includes('Undone')")
    assert rig.local("review") == "2026-10-21T09:00"


def test_the_picker_lists_the_next_free_slots_and_a_slot_fills_the_fields(rig):
    pg = rig.page()
    pg.wait_for_selector(".cal-grid .card")
    rig.card(pg, "draft", "tray").locator(".card-link").click()                         # a draft with no time yet
    pg.wait_for_selector("#peek.on .peek-actions")
    pg.click("#peek [data-act='change-time']")
    pg.wait_for_selector("dialog[open] .rs-chip")
    chips = pg.locator("dialog[open] .rs-chip").all_inner_texts()
    assert chips[0] == "Mon 26 Oct, 09:00"                                            # Wed 21 and Fri 23 hold the review and the approved post on ig-main
    assert "Wed 21 Oct, 09:00" not in chips and "Fri 23 Oct, 09:00" not in chips
    pg.locator("dialog[open] .rs-chip").first.click()
    assert pg.input_value("#rs-time") == "09:00" and pg.input_value("#rs-date") == "2026-10-26"
    pg.click("dialog[open] button[type=submit]")
    rig.until(pg, "!document.querySelector('dialog[open]')")
    assert rig.local("draft") == "2026-10-26T09:00"


def test_next_free_slot_from_the_keyboard_and_from_the_preview(rig):
    pg = rig.page()
    pg.wait_for_selector(".cal-grid .card")
    pid, vid = rig.posts["draft"]
    pg.locator(f'.tray .card[data-variant="{vid}"] .card-link').focus()
    pg.keyboard.press("n")
    rig.until(pg, "document.querySelector('#note').innerText.includes('Moved')")
    first = rig.local("draft")
    assert first and first.endswith("T09:00") and first > "2026-10-19"
    # the preview's button does the same for a scheduled post: its next free slot is strictly later than now, never its own
    open_peek(rig, pg, "fb")
    before = rig.local("fb")
    pg.click("#peek [data-act='next-slot']")
    rig.wait(pg, lambda: rig.local("fb") != before)
    assert rig.local("fb") == "2026-10-23T09:00"


def test_a_taken_slot_is_refused_in_words_and_nothing_moves(rig):
    pg = rig.page()
    pg.wait_for_selector(".cal-grid .card")
    open_peek(rig, pg, "approved")                                                    # Fri 23 Oct 09:00, ig-main
    pg.click("#peek [data-act='change-time']")
    pg.wait_for_selector("dialog[open] #rs-date")
    pg.fill("#rs-date", "2026-10-21")                                                 # the review post is on ig-main Wed 21 Oct 09:00
    pg.fill("#rs-time", "09:00")
    pg.click("dialog[open] button[type=submit]")
    pg.wait_for_selector("dialog[open] .rs-err:not(:empty)")
    assert "taken" in pg.locator("dialog[open] .rs-err").inner_text()
    assert rig.local("approved") == "2026-10-23T09:00"
    rig.allow_errors = False


def test_retiming_an_approved_post_keeps_the_approval_and_explains_itself(rig):
    pg = rig.page()
    pg.wait_for_selector(".cal-grid .card")
    before = rig.item("approved")
    assert before["status"] == "approved"
    open_peek(rig, pg, "approved")
    pg.click("#peek [data-act='change-time']")
    pg.wait_for_selector("dialog[open] .rs-note")
    assert "approval stays" in pg.locator("dialog[open] .rs-note").inner_text()
    pg.fill("#rs-date", "2026-10-24")
    pg.fill("#rs-time", "10:00")
    pg.click("dialog[open] button[type=submit]")
    rig.until(pg, "!document.querySelector('dialog[open]')")
    after = rig.item("approved")
    assert after["status"] == "approved" and after["approvals"] == before["approvals"]    # the approval is untouched
    assert "moved_after_approval" in rig.variant("approved")["flags"]
    assert "the approval still holds" in note(pg).inner_text()
    # the card says so, the preview says why, and the history shows who moved it
    chip = rig.card(pg, "approved").locator(".flag.f-moved_after_approval")
    chip.wait_for()
    assert "Moved after approval" in chip.inner_text()
    pg.wait_for_selector("#peek .peek-flag")
    assert "covers the words and pictures, not the time" in pg.locator("#peek .peek-why").inner_text()
    assert "Moved from" in pg.locator("#peek .peek-history").inner_text()


def test_the_editor_has_the_same_controls_for_time(rig, width=1440):
    pid, vid = rig.posts["review"]
    pg = rig.page(f"#/post/{pid}?variant={vid}", wait=".ed", width=width)
    pg.wait_for_selector(".ed-when [data-act='change-time']")
    assert pg.get_by_role('region',name='Time',exact=True).count()==1
    assert "Wed" in pg.locator(".ed-when").inner_text()
    pg.locator('.ed-when').scroll_into_view_if_needed()
    pg.screenshot(path=f'/private/tmp/ave-integration-editor-scheduled-{width}.png')
    assert pg.locator('body').evaluate('(el)=>el.scrollWidth<=innerWidth')
    pg.click(".ed-when [data-act='next-slot']")
    rig.until(pg, "document.querySelector('.ed-when').innerText.includes('Mon')")
    assert rig.local("review") == "2026-10-26T09:00"                                  # Fri 23 holds the approved post, so the next free slot is Monday
    pg.click(".ed-when [data-act='change-time']")
    pg.wait_for_selector("dialog[open] #rs-date")
    pg.fill("#rs-date", "2026-10-26")
    pg.fill("#rs-time", "11:15")
    pg.click("dialog[open] button[type=submit]")
    rig.until(pg, "!document.querySelector('dialog[open]')")
    assert rig.local("review") == "2026-10-26T11:15"
    rig.until(pg, "document.querySelector('.ed-when').innerText.includes('11:15')")
    pg.click(".ed-when [data-act='unschedule']")
    rig.until(pg, "document.querySelector('.ed-when').innerText.includes('Not scheduled')")
    assert rig.local("review") is None
    assert pg.locator('.ed-when').count()==1
    pg.locator('.ed-when').scroll_into_view_if_needed()
    pg.screenshot(path=f'/private/tmp/ave-integration-editor-unscheduled-{width}.png')


def test_a_published_post_offers_no_time_controls(rig, width=1440):
    pid, vid = rig.posts["fb"]
    item = rig.item("fb")
    item["status"] = "published"
    pq.save(rig.w.sp, item)
    pg = rig.page(f"#/post/{pid}?variant={vid}", wait=".ed", width=width)
    pg.wait_for_selector(".ed-when")
    assert pg.locator(".ed-when [data-act='change-time']").count() == 0
    assert "cannot be moved" in pg.locator(".ed-when").inner_text()
    assert pg.locator('.ed-when').count()==1
    assert pg.locator('.ed-when button').count()==0
    pg.locator('.ed-when').scroll_into_view_if_needed()
    pg.screenshot(path=f'/private/tmp/ave-integration-editor-published-{width}.png')
    assert pg.locator('body').evaluate('(el)=>el.scrollWidth<=innerWidth')


def test_phone_editor_time_journey(rig):
    test_the_editor_has_the_same_controls_for_time(rig,width=390)


def test_phone_published_editor_time_lock(rig):
    test_a_published_post_offers_no_time_controls(rig,width=390)


# ---------------------------------------------------------------------------------------------------- (2) multi-select, bulk, undo

def select_cards(rig, pg, names):
    pg.click("#sel-toggle")
    for n in names:
        rig.card(pg, n).locator(".card-link").click()


def test_select_several_and_shift_them_a_week_then_undo_the_batch(rig):
    pg = rig.page()
    pg.wait_for_selector(".cal-grid .card")
    select_cards(rig, pg, ["review", "fb"])
    pg.wait_for_selector("#selbar:not([hidden])")
    assert pg.locator("#selbar .sel-count").inner_text().startswith("2")
    assert pg.locator(".card.is-selected").count() == 2
    pg.click("#selbar >> text=+1 week")
    rig.until(pg, "document.querySelector('#note').innerText.includes('Shifted 2 posts')")
    assert rig.local("review") == "2026-10-28T09:00" and rig.local("fb") == "2026-10-28T09:00"
    assert rig.item("review")["status"] == "awaiting_approval"                       # bulk never approves
    assert pg.locator("#note .note-act").inner_text().strip() == "Undo"
    pg.click("#note .note-act")
    rig.until(pg, "document.querySelector('#note').innerText.includes('Undone')")
    assert rig.local("review") == "2026-10-21T09:00" and rig.local("fb") == "2026-10-21T09:00"


def test_a_refused_member_stays_selected_with_its_reason_and_the_rest_move(rig):
    pg = rig.page()
    pg.wait_for_selector(".cal-grid .card")
    pid, vid = rig.posts["loaf2"]
    # the second loaf post would land on ig-loaf's Wed 28 Oct slot, which a third post already holds
    rig.add_post("Loaf Co: a taken slot.", account="ig-loaf", at="2026-10-30T15:00:00Z", sync=False)
    select_cards(rig, pg, ["loaf2", "review"])
    pg.click("#selbar >> text=+1 week")
    pg.wait_for_selector("#selbar .sel-refused")
    assert rig.local("review") == "2026-10-28T09:00"                                  # the one that could move did
    assert rig.local("loaf2") == "2026-10-23T09:00"                                   # the one that could not stayed put
    assert "taken" in pg.locator("#selbar .sel-refused").inner_text() and "Loaf Co: Friday is rye day" in pg.locator("#selbar .sel-refused").inner_text()
    assert pg.locator("#selbar .sel-count").inner_text().startswith("1")              # only the refused one stays selected
    assert rig.item("review")["status"] == "awaiting_approval"


def test_bulk_unschedule_keeps_an_approval_and_undo_puts_the_time_back(rig):
    pg = rig.page()
    pg.wait_for_selector(".cal-grid .card")
    before = rig.item("approved")
    select_cards(rig, pg, ["approved"])
    pg.click("#selbar >> text=Unscheduled")
    rig.until(pg, "document.querySelector('#note').innerText.includes('Unscheduled')")
    assert rig.local("approved") is None and rig.item("approved")["status"] == "approved" and rig.item("approved")["approvals"] == before["approvals"]
    pg.click("#note .note-act")
    rig.until(pg, "document.querySelector('#note').innerText.includes('Undone')")
    assert rig.local("approved") == "2026-10-23T09:00"


def test_bulk_next_free_slots_places_the_whole_tray(rig):
    pg = rig.page()
    pg.wait_for_selector(".cal-grid .card")
    pid, vid = rig.posts["draft"]
    pg.click("#sel-toggle")
    pg.locator(f'.tray .card[data-variant="{vid}"] .card-link').click()
    pg.click("#selbar >> text=Next free slots")
    rig.until(pg, "document.querySelector('#note').innerText.includes('Moved')")
    assert rig.local("draft") and rig.local("draft") > "2026-10-19"


def test_move_to_account_only_for_unqueued_posts_of_the_same_platform(rig):
    pg = rig.page()
    pg.wait_for_selector(".cal-grid .card")
    pid, vid = rig.posts["draft"]
    pg.click("#sel-toggle")
    pg.locator(f'.tray .card[data-variant="{vid}"] .card-link').click()
    rig.card(pg, "approved").locator(".card-link").click()                            # approved: bound to its account
    pg.click("#selbar >> text=Move to account")
    pg.wait_for_selector("dialog[open] #mv-acct")
    pg.select_option("#mv-acct", "ig-loaf")
    pg.click("dialog[open] button.primary")
    pg.wait_for_selector("#selbar .sel-refused")
    assert rig.variant("draft")["account"] == "ig-loaf"                              # the draft moved
    assert rig.variant("approved")["account"] == "ig-main"                           # the approved one did not
    assert "approval is bound" in pg.locator("#selbar .sel-refused").inner_text()
    assert rig.item("approved")["status"] == "approved"


def test_x_selects_r_changes_time_n_takes_the_next_slot_and_escape_clears(rig):
    pg = rig.page()
    pg.wait_for_selector(".cal-grid .card")
    rig.card(pg, "review").locator(".card-link").focus()
    pg.keyboard.press("x")
    pg.wait_for_selector("#selbar:not([hidden])")
    assert pg.locator("#selbar .sel-count").inner_text().startswith("1")
    pg.keyboard.press("r")
    pg.wait_for_selector("dialog[open] #rs-date")
    pg.keyboard.press("Escape")
    rig.until(pg, "!document.querySelector('dialog[open]')")
    pg.keyboard.press("Escape")                                                       # nothing open: Esc clears the selection
    rig.until(pg, "document.querySelector('#selbar').hidden")
    assert pg.locator(".card.is-selected").count() == 0


def test_published_cards_cannot_be_selected(rig):
    item = rig.item("fb")
    item["status"] = "published"
    pq.save(rig.w.sp, item)
    pg = rig.page()
    pg.wait_for_selector(".cal-grid .card")
    assert rig.card(pg, "fb").locator(".sel").count() == 1 and not rig.card(pg, "fb").locator(".sel").is_visible()
    pg.click("#sel-toggle")
    rig.card(pg, "fb").locator(".card-link").click()
    rig.until(pg, "document.querySelector('#note').innerText.length > 0")
    assert pg.locator("#selbar").is_hidden() and pg.locator(".card.is-selected").count() == 0


# ---------------------------------------------------------------------------------------------------- (3) filters that persist

def test_brand_chips_separate_two_brands_and_the_choice_persists(rig):
    pg = rig.page()
    pg.wait_for_selector(".cal-grid .card")
    assert pg.locator(".brand-chip").all_inner_texts()[0].startswith("All brands")
    assert rig.card(pg, "loaf").count() == 1 and rig.card(pg, "review").count() == 1
    pg.click('.brand-chip[data-brand="Loaf Co"]')
    pg.wait_for_selector(f'.card[data-variant="{rig.posts["loaf"][1]}"]')
    rig.until(pg, f"!document.querySelector('.card[data-variant=\"{rig.posts['review'][1]}\"]')")
    assert rig.card(pg, "review").count() == 0 and rig.card(pg, "fb").count() == 0 and rig.card(pg, "loaf").count() == 1
    pg.click(".fbar-fold summary")                                                        # the account chips live in a fold when brands exist
    assert [c.split("\n")[-1] for c in pg.locator(".fbar-accts .acct-chip").all_inner_texts()] == ["@ig-loaf"]       # only this brand's accounts are offered
    # a reload keeps the brand, and so does the next page
    pg.reload()
    pg.wait_for_selector(".cal-grid .card")
    assert pg.locator('.brand-chip[data-brand="Loaf Co"]').get_attribute("aria-pressed") == "true"
    assert rig.card(pg, "review").count() == 0
    pg.evaluate("location.hash = '#/ideas'")
    pg.wait_for_selector(".ideas")
    assert pg.locator('.brand-chip[data-brand="Loaf Co"]').get_attribute("aria-pressed") == "true"
    # clearing the filters brings everything back
    pg.evaluate(f"location.hash = '{WEEK[1:]}'")
    pg.wait_for_selector(".cal-grid .card")
    pg.click(".fbar-clear")
    pg.wait_for_selector(f'.card[data-variant="{rig.posts["review"][1]}"]')


def test_the_campaign_filter_narrows_to_one_campaign(rig):
    pg = rig.page()
    pg.wait_for_selector(".cal-grid .card")
    pg.wait_for_selector("#f-campaign option:nth-child(2)", state="attached")
    pg.select_option("#f-campaign", rig.campaign)
    rig.until(pg, f"!document.querySelector('.card[data-variant=\"{rig.posts['review'][1]}\"]')")
    assert rig.card(pg, "loaf").count() == 1 and rig.card(pg, "loaf2").count() == 0
    assert pg.locator(".fbar-clear").count() == 1


def test_filters_that_match_nothing_say_so_and_offer_to_clear(rig):
    pg = rig.page("#/calendar?view=week&d=2026-12-07")                                   # a week with nothing in it
    pg.wait_for_selector(".cal-grid")
    pg.select_option("#f-campaign", rig.campaign)
    pg.wait_for_selector(".empty-filter")
    assert "Nothing matches these filters" in pg.locator(".empty-filter").inner_text()
    pg.click(".empty-filter button")
    rig.until(pg, "!document.querySelector('.empty-filter')")


def test_accounts_are_summarised_in_one_line_not_seven_chips(rig):
    pg = rig.page()
    pg.wait_for_selector(".cal-grid .card")
    assert pg.locator("#modes summary").inner_text() == "3 accounts: all Dry run"
    assert pg.locator("#modes .mode").count() == 3 and not pg.locator("#modes .mode-list").is_visible()
    pg.click("#modes summary")
    assert pg.locator("#modes .mode-list").is_visible()


# ---------------------------------------------------------------------------------------------------- (4) the side preview

def test_the_preview_shows_the_platform_mock_hashtags_alt_text_status_and_history(rig):
    pg = rig.page()
    pg.wait_for_selector(".cal-grid .card")
    open_peek(rig, pg, "review")
    pg.wait_for_selector("#peek .peek-mock .pv")
    body = pg.locator("#peek").inner_text()
    assert "On Instagram" in body and "approximate" in body.lower()
    assert "#sourdough" in body and "#smallbakery" in body
    assert "Recipe in the link in bio." in body
    assert "A loaf of bread on a wooden table, crust split." in body                    # alt text, from the post
    assert "Waiting for your approval" in pg.locator("#peek .peek-why").inner_text()
    assert pg.locator("#peek .peek-history li").count() >= 1
    # the caption fold: hidden text is marked, and the toggle reveals it
    assert pg.locator("#peek .pv-more, #peek .pv-text").count() >= 1
    pg.keyboard.press("Escape")
    rig.until(pg, "!document.querySelector('#peek.on')")
    assert pg.evaluate("document.activeElement.closest('.card').dataset.variant") == rig.posts["review"][1]      # focus is back where it was


def test_a_missing_alt_text_is_called_out_in_the_preview(rig):
    pid, vid = rig.posts["fb"]
    from ave.studio import store
    from ave.studio.events import AGENT
    rig.w.studio.edit_post(pid, {"media.0.alt": ""}, expected_rev=store.load_post(rig.w.sp, pid)["rev"], actor=AGENT)
    pg = rig.page()
    pg.wait_for_selector(".cal-grid .card")
    open_peek(rig, pg, "fb")
    pg.wait_for_selector("#peek .peek-alts li.missing")
    assert "Missing" in pg.locator("#peek .peek-alts li.missing").inner_text()


def test_the_preview_is_a_bottom_sheet_on_a_phone_and_nothing_scrolls_sideways(rig):
    pg = rig.page(width=390)
    pg.wait_for_selector(".cal-grid .card")
    open_peek(rig, pg, "review")
    box = pg.locator("#peek").bounding_box()
    assert box["width"] <= 390 and box["y"] > 60 and box["y"] + box["height"] <= 900 + 1      # a sheet from the bottom, not the whole page
    assert pg.evaluate("document.documentElement.scrollWidth") <= 390
    pg.click("#peek .peek-x")
    rig.until(pg, "!document.querySelector('#peek.on')")


def test_the_needs_rework_note_is_on_the_preview(rig):
    pid, vid = rig.posts["fb"]
    rig.w.studio.add_feedback(pid, "rework_copy", note="Too salesy. Say what the bread tastes like.", variant=vid, by="maren")
    pg = rig.page()
    pg.wait_for_selector(".cal-grid .card")
    open_peek(rig, pg, "fb")
    pg.wait_for_selector("#peek .peek-note")
    assert "Too salesy" in pg.locator("#peek .peek-note").inner_text()
    assert "Sent back to Claude" in pg.locator("#peek .peek-reason").inner_text()


def test_a_stale_approval_is_explained_on_the_card_and_in_the_preview(rig):
    item = rig.item("approved")
    item["caption"] = item["caption"] + " (edited behind the studio's back)"
    pq.save(rig.w.sp, item)                                                               # the approved item no longer matches what was signed
    pg = rig.page()
    pg.wait_for_selector(".cal-grid .card")
    chip = rig.card(pg, "approved").locator(".flag.f-approval_stale")
    chip.wait_for()
    assert "Approval stale" in chip.inner_text()
    open_peek(rig, pg, "approved")
    text = pg.locator("#peek .peek-why").inner_text()
    assert "Approval stale" in text and "will not go out until you approve it again" in text


# ---------------------------------------------------------------------------------------------------- (5) ideas

def test_the_ideas_page_lists_ideas_with_their_reason_and_confidence(rig):
    pg = rig.page("#/ideas", wait=".ideas")
    pg.wait_for_selector(".idea")
    t = pg.locator(".idea").first.inner_text()
    assert "Behind the scenes" in t and "Asked about twice this month" in t and "60% sure" in t
    assert pg.locator("#nav a[data-p=ideas] .count").inner_text() == "1"


def test_turn_an_idea_into_a_draft_and_undo(rig):
    pid, _ = rig.posts["idea"]
    pg = rig.page("#/ideas", wait=".ideas")
    pg.click('.idea [data-act="draft"]')
    pg.wait_for_selector(".empty")                                                         # the only idea left the list
    from ave.studio import store
    post = store.load_post(rig.w.sp, pid)
    assert post["stage"] == "draft" and post["copy"]["base"].startswith("Behind the scenes")
    pg.click("#note .note-act")
    pg.wait_for_selector(".idea")
    post = store.load_post(rig.w.sp, pid)
    assert post["stage"] == "idea" and post["copy"]["base"] == ""


def test_set_an_idea_aside_and_undo_it(rig):
    pid, _ = rig.posts["idea"]
    pg = rig.page("#/ideas", wait=".ideas")
    pg.click('.idea [data-act="dismiss"]')
    pg.wait_for_selector(".empty")
    from ave.studio import store
    assert store.load_post(rig.w.sp, pid)["stage"] == "dropped"
    pg.click("#note .note-act")
    pg.wait_for_selector(".idea")
    assert store.load_post(rig.w.sp, pid)["stage"] == "idea"


def test_an_idea_with_no_brand_can_be_given_one_and_the_brand_filter_then_finds_it(rig):
    pg = rig.page("#/ideas", wait=".ideas")
    pg.wait_for_selector(".idea-brandpick select")
    pg.select_option(".idea-brandpick select", "Loaf Co")
    rig.until(pg, "document.querySelector('.idea .idea-brand') && document.querySelector('.idea .idea-brand').innerText === 'Loaf Co'")
    pg.click('.brand-chip[data-brand="Acme Bakery"]')
    pg.wait_for_selector(".empty-filter")
    pg.click('.brand-chip[data-brand="Loaf Co"]')
    pg.wait_for_selector(".idea")


def test_the_empty_ideas_page_says_how_ideas_arrive(rig):
    from ave.studio import store
    from ave.studio.events import AGENT
    pid, _ = rig.posts["idea"]
    rig.w.studio.edit_post(pid, {"stage": "dropped"}, expected_rev=store.load_post(rig.w.sp, pid)["rev"], actor=AGENT)
    pg = rig.page("#/ideas", wait=".ideas")
    pg.wait_for_selector(".empty")
    t = pg.locator(".empty").inner_text()
    assert "No ideas waiting" in t and "ave studio ingest idea" in t


# ---------------------------------------------------------------------------------------------------- (8) states, help, themes, phones

def test_the_help_dialog_documents_the_new_keys(rig):
    pg = rig.page()
    pg.wait_for_selector(".cal-grid .card")
    pg.keyboard.press("?")
    pg.wait_for_selector("dialog[open]")
    t = pg.locator("dialog[open]").inner_text()
    for words in ("Preview the focused post", "Change time", "next free slot", "Undo the last move", "Select or unselect", "Next and previous post in Review", "Send the post back"):
        assert words.lower() in t.lower(), words


def test_the_calendar_says_so_when_the_server_is_gone_and_offers_to_try_again(rig):
    pg = rig.page()
    pg.wait_for_selector(".cal-grid .card")
    rig.allow_errors = True
    pg.route("**/api/studio/calendar*", lambda r: r.abort())
    pg.click(".pager button:nth-child(3)")                                                 # next period: the calendar request fails
    pg.wait_for_selector(".empty-error, #note[data-tone=error].on", timeout=6000)
    pg.unroute("**/api/studio/calendar*")


def test_the_boot_error_has_a_try_again_button(rig):
    rig.allow_errors = True
    ctx = rig.browser.new_context(viewport={"width": 1280, "height": 800})
    rig.pages.append(ctx)
    pg = ctx.new_page()
    pg.route("**/api/studio/summary", lambda r: r.abort())
    pg.goto(rig.srv.url())
    pg.wait_for_selector(".empty-error")
    assert "not reachable" in pg.locator(".empty-error").inner_text() and pg.locator(".empty-error button").inner_text() == "Try again"


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_new_surfaces_follow_the_colour_scheme(rig, scheme):
    pg = rig.page(scheme=scheme)
    pg.evaluate("scheme => localStorage.setItem('ave.studio.theme', JSON.stringify(scheme))", scheme)
    pg.reload()
    pg.wait_for_selector(".cal-grid .card")
    open_peek(rig, pg, "review")
    bg = pg.evaluate("getComputedStyle(document.querySelector('#peek')).backgroundColor")
    assert bg == ("rgb(27, 29, 41)" if scheme == "dark" else "rgb(255, 255, 255)")
    pg.keyboard.press("Escape")
    pg.click("#sel-toggle")
    rig.card(pg, "fb").locator(".card-link").click()
    pg.wait_for_selector("#selbar:not([hidden])")
    film = pg.evaluate("getComputedStyle(document.querySelector('#selbar')).backgroundColor")
    assert film == ("rgb(42, 58, 70)" if scheme == "dark" else "rgb(15, 23, 29)")


def test_reduced_motion_removes_the_toast_and_panel_transitions(rig):
    pg = rig.page(reduced=True)
    pg.wait_for_selector(".cal-grid .card")
    assert pg.evaluate("getComputedStyle(document.querySelector('#note')).transitionDuration") in ("0s", "0.000001s")
    open_peek(rig, pg, "review")
    assert pg.evaluate("getComputedStyle(document.querySelector('#peek')).transitionDuration") in ("0s", "0.000001s")


@pytest.mark.parametrize("route", [WEEK, "#/calendar?view=month&d=2026-10-19", "#/ideas"])
def test_no_planner_surface_scrolls_sideways_on_a_phone(rig, route):
    pg = rig.page(route, width=390)
    pg.wait_for_timeout(600)
    pg.evaluate("document.querySelector('#sel-toggle') && document.querySelector('#sel-toggle').click()")
    card = pg.locator(".card .card-link").first
    if card.count():
        card.click()
    pg.wait_for_timeout(300)
    assert pg.evaluate("document.documentElement.scrollWidth") <= 390, route


def test_a_card_has_its_own_change_time_button_that_needs_no_drag(rig):
    pg = rig.page()
    pg.wait_for_selector(".cal-grid .card")
    card = rig.card(pg, "review")
    card.hover()
    card.locator(".card-act").click()
    pg.wait_for_selector("dialog[open] #rs-date")
    assert pg.locator("dialog[open]").inner_text().startswith("Change time")
    pg.keyboard.press("Escape")
    rig.until(pg, "!document.querySelector('dialog[open]')")
    assert rig.local("review") == "2026-10-21T09:00" and pg.evaluate("location.hash").startswith("#/calendar")
    assert rig.card(pg, "approved", "week").locator(".card-act").count() == 1


def test_the_grid_page_follows_the_brand_choice(rig):
    pg = rig.page("#/grid/ig-main", wait=".planner-feed")
    assert pg.get_by_label("Feed account", exact=True).locator("option").count() == 4
    pg.click('.brand-chip[data-brand="Loaf Co"]')
    rig.until(pg, "location.hash.startsWith('#/grid/ig-loaf')")
    rig.until(pg, "document.querySelector('select[aria-label=\"Feed account\"]').value === 'ig-loaf'")
    assert pg.get_by_label("Feed account", exact=True).locator("option").count() == 2


@pytest.mark.parametrize("route,pattern", [("#/alerts", "**/api/studio/alerts*"), ("#/feedback", "**/api/studio/feedback*"), ("#/accounts", "**/api/studio/connections"),
                                           ("#/library", "**/api/studio/library*"), ("#/campaigns", "**/api/studio/campaigns*")])
def test_a_panel_page_with_the_server_gone_says_why_and_can_try_again(rig, route, pattern):
    rig.allow_errors = True
    pg = rig.page("#/calendar?view=week&d=2026-10-19", wait=".cal-grid")
    pg.route(pattern, lambda r: r.abort())
    pg.evaluate(f"location.hash = '{route[1:]}'")
    pg.wait_for_selector(".st-alert.bad", timeout=8000)
    assert "Is `ave studio ui` still running?" in pg.locator(".st-alert.bad").inner_text()     # not "Failed to fetch"
    pg.unroute(pattern)
    pg.click('.st-alert [data-act="retry"]')
    rig.until(pg, "!document.querySelector('.st-alert.bad')")
