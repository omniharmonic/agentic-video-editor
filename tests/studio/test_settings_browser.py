"""Settings and the approval sheet in a real browser.

Settings holds what the old Accounts (Connections), Alerts and Collaborate screens did, plus People (Task 16's membership
sheets). The approval sheet holds publishing approval and trim approval: the page shows exactly what is approved and hands only
its identity to the app (aveNative.approvePublish / approveTrim), where the native sheet, the device-owner check and the PIN
live (tests/studio/test_security_gates.py covers that side). The world is the Workspace's planner world: a two-brand studio with
posts in review (real queue items), an episode project with Imani and Sam, and Claude's draft trim on that episode.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .test_workspace_browser import TEST_FLAG, build_plan_world, browser  # noqa: F401  (the module's browser fixture)

SHOTS = Path(__file__).resolve().parents[2] / ".superpowers/sdd/2026-11-02-director-room/task-23-shots"

NATIVE = TEST_FLAG + """ window.__asked = []; Object.defineProperty(window, 'aveNative', {configurable: true, value: {
    connectionAction: async (action, args) => { window.__asked.push(['connection', action, args]); return {state: 'done', message: 'Saved in the app'}; },
    approvePublish: async (items) => { window.__asked.push(['approvePublish', items]);
      const bad = window.__refuse || []; return {state: 'approved', approved: items.filter((x) => !bad.includes(x.post)),
        refused: items.filter((x) => bad.includes(x.post)).map((x) => ({post: x.post, variant: x.variant, reason: 'the media changed since you reviewed it: view it again'}))}; },
    ackAlert: async (id) => { window.__asked.push(['ackAlert', id]); return {op: 'ack', alert: id}; },
    approveTrim: async (...a) => { window.__asked.push(['approveTrim', ...a]); return {state: 'confirmed'}; },
    shareProject: async (id) => { window.__asked.push(['share', id]); return {state: 'shared', person: 'Imani', title: 'Episode 12'}; },
    joinProject: async () => { window.__asked.push(['join']); return {state: 'joined', title: 'The Long Table'}; },
    removeMember: async (id) => { window.__asked.push(['remove', id]); return {state: 'removed', message: 'Imani is no longer on Episode 12.'}; },
    handOverPublishing: async (id) => { window.__asked.push(['handover', id]); return {state: 'handed_over', message: 'Sam publishes Episode 12 now.'}; },
    openResolve: async () => { window.__asked.push(['resolve']); return {}; },
    agentConfig: async () => { window.__asked.push(['agent']); return {}; },
    selectEpisode: async () => ({})}});"""


@pytest.fixture
def world(tmp_path, browser):  # noqa: F811
    w = build_plan_world(tmp_path, browser)
    (w.ep.root / "episode.json").write_text(json.dumps({"title": "Episode 12", "trim": {
        "main": {"in": 4.25, "out": 2711.5, "by": "claude", "at": "2026-10-19T10:00:00-0600", "draft_only": True}}}))
    from ave.studio import ledger
    ledger.append_alert(w.sp, severity="warn", kind="account_not_live", message="ig-main is not live: posts wait.", account="ig-main")
    yield w
    w.rig.close()
    w.srv.shutdown()
    w.srv.server_close()


def _open(browser, world, route, *, native=True, size=(1440, 900)):  # noqa: F811
    pg = browser.new_page(viewport={"width": size[0], "height": size[1]})
    pg.add_init_script(NATIVE if native else TEST_FLAG)
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(world.srv.app_url() + route)
    pg.errors = errors
    return pg


def _shot(pg, name):
    SHOTS.mkdir(parents=True, exist_ok=True)
    pg.wait_for_timeout(250)
    pg.screenshot(path=str(SHOTS / f"{name}-{pg.viewport_size['width']}.png"))


def _asked(pg):
    return pg.evaluate("window.__asked")


# ---------------------------------------------------------------------------------------------------- reaching Settings
def test_settings_is_reached_from_the_workspace_and_the_room(browser, world):  # noqa: F811
    pg = _open(browser, world, "#/workspace")
    pg.wait_for_selector(".ws-row[data-key]")
    pg.get_by_role("link", name="Settings").click()
    pg.wait_for_selector(".set-main h1")
    assert pg.evaluate("location.hash") == "#/settings" and pg.locator(".set-main h1").inner_text() == "Accounts"
    assert pg.evaluate("document.documentElement.dataset.theme") == "light"
    assert pg.locator(".set-nav .rail-nav .rail-item").all_inner_texts()[:4] == ["Accounts", "Alerts\n1", "People", "Collaboration"] or \
        [t.split("\n")[0] for t in pg.locator(".set-nav .rail-nav .rail-item").all_inner_texts()] == ["Accounts", "Alerts", "People", "Collaboration"]
    pg.get_by_role("link", name="Workspace").click()
    pg.wait_for_selector(".ws-row[data-key]")
    pg.goto(world.srv.app_url() + "#/room/ep12")
    pg.wait_for_selector(".room")
    pg.get_by_role("link", name="Settings").click()
    pg.wait_for_selector(".set-main h1")
    assert pg.evaluate("location.hash") == "#/settings"
    assert not pg.errors
    pg.close()


# ---------------------------------------------------------------------------------------------------- Accounts
def test_accounts_show_state_in_words_and_never_credential_values(browser, world):  # noqa: F811
    pg = _open(browser, world, "#/settings/accounts")
    pg.wait_for_selector(".set-profile")
    pg.locator(".set-profile", has_text="instagram").first.click()
    pg.wait_for_selector('.set-profile[aria-pressed="true"]:has-text("instagram")')
    text = pg.locator(".set-detail").inner_text()
    assert "Not verified" in text and "Not enabled" in text and "Not tested" in text
    assert pg.locator("input[type=password]").count() == 0 and "ave studio account live" not in pg.content()
    assert pg.get_by_text("Facebook Page ID", exact=True).count() == 0
    pg.get_by_label("Login product", exact=True).select_option("instagram_facebook")
    assert pg.get_by_text("Facebook Page ID", exact=True).is_visible()
    pg.get_by_label("Instagram professional account ID").fill("17841400000000000")
    pg.get_by_role("button", name="Confirm destination").click()
    pg.wait_for_function("() => window.__asked.length === 1")
    kind, action, args = _asked(pg)[0]
    assert action == "connectionConfigure" and set(args) == {"account", "revision", "values"}      # only public identifiers
    assert args["values"] == {"product": "instagram_facebook", "public": {"ig_user_id": "17841400000000000"}}
    assert pg.get_by_text("Saved in the app").is_visible()
    _shot(pg, "settings-accounts")
    pg.set_viewport_size({"width": 1024, "height": 768})
    _shot(pg, "settings-accounts")
    assert not pg.errors
    pg.close()


def test_accounts_hosting_delivery_and_tools_have_their_controls(browser, world):  # noqa: F811
    pg = _open(browser, world, "#/settings/accounts/hosting")
    pg.wait_for_selector(".set-card")
    assert "Platform-readable media" in pg.locator(".set-pane").inner_text()
    pg.get_by_label("Bucket").fill("acme-media")
    pg.get_by_role("button", name="Configure host and keys").click()
    pg.wait_for_function("() => window.__asked.length === 1")
    assert _asked(pg)[0][1] == "hostConfigure" and _asked(pg)[0][2]["values"]["bucket"] == "acme-media"
    pg.get_by_role("tab", name="Background delivery").click()
    pg.wait_for_selector("text=Posting permission")
    for name in ("Start service", "Stop service", "Repair service", "Remove service", "Enable automatic posting", "Disable automatic posting",
                 "Pause delivery", "Resume delivery", "Run diagnostics"):
        assert pg.get_by_role("button", name=name, exact=True).is_visible(), name
    pg.get_by_role("button", name="Start service", exact=True).click()
    pg.wait_for_function("() => window.__asked.length === 2")
    assert _asked(pg)[1][1] == "serviceStart"
    pg.get_by_role("button", name="Pause delivery", exact=True).click()
    pg.wait_for_selector("text=Delivery paused immediately.")
    _shot(pg, "settings-delivery")
    pg.get_by_role("tab", name="Creative tools and agents").click()
    pg.get_by_role("button", name="Open Resolve").click()
    pg.get_by_role("button", name="Preview and save agent configuration").click()
    pg.wait_for_function("() => window.__asked.length === 4")
    assert [a[0] for a in _asked(pg)[2:]] == ["resolve", "agent"]
    assert pg.get_by_role("link", name="Open Canva").get_attribute("href") == "https://www.canva.com/"
    assert not pg.errors
    pg.close()


def test_a_local_profile_is_made_without_signing_in(browser, world):  # noqa: F811
    pg = _open(browser, world, "#/settings/accounts")
    pg.wait_for_selector(".set-profile")
    before = pg.locator(".set-profile").count()
    pg.get_by_role("button", name="New local profile").click()
    pg.get_by_label("Profile display name").fill("Acme Bakery on Bluesky")
    pg.get_by_label("Platform").select_option("bluesky")
    pg.get_by_role("button", name="Create profile").click()
    pg.wait_for_function(f"() => document.querySelectorAll('.set-profile').length === {before + 1}")
    assert "Acme Bakery on Bluesky" in pg.locator(".set-profiles").inner_text()
    assert not pg.errors
    pg.close()


def test_without_the_app_account_controls_say_why(browser, world):  # noqa: F811
    pg = _open(browser, world, "#/settings/accounts/service", native=False)
    pg.wait_for_selector("text=Posting permission")
    pg.get_by_role("button", name="Start service", exact=True).click()
    pg.wait_for_selector("text=Open this workspace in the AVE Studio app")
    assert not pg.errors
    pg.close()


# ---------------------------------------------------------------------------------------------------- Alerts
def test_alerts_show_advice_and_acknowledge(browser, world):  # noqa: F811
    pg = _open(browser, world, "#/settings/alerts")
    pg.wait_for_selector(".set-alert")
    card = pg.locator(".set-alert").first
    assert "What to do" in card.inner_text() and "not live" in card.inner_text() and "Open Accounts" in card.inner_text()
    assert pg.locator(".set-nav .rail-count:not([hidden])").inner_text() == "1"
    _shot(pg, "settings-alerts")
    card.get_by_role("button", name="Acknowledge").click()
    pg.wait_for_function("() => window.__asked.length === 1")
    from ave.studio import ledger
    aid = ledger.list_alerts(world.sp)[0]["id"]
    assert _asked(pg) == [["ackAlert", aid]]                        # the app checks it is the person; the page names the alert only
    assert not pg.errors
    pg.close()


def test_without_the_app_an_alert_is_not_acknowledged_and_the_terminal_line_shows(browser, world):  # noqa: F811
    from ave.studio import ledger
    pg = _open(browser, world, "#/settings/alerts", native=False)
    pg.wait_for_selector(".set-alert")
    aid = ledger.list_alerts(world.sp)[0]["id"]
    assert pg.get_by_role("button", name="Acknowledge").is_disabled()
    assert f"ave studio alerts ack {aid}" in pg.locator(".set-alert").inner_text()
    status = pg.evaluate(f"fetch('/api/studio/alerts/{aid}/ack', {{method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify({{by: 'Maren'}})}}).then(r => r.status)")
    assert status == 403 and [a["id"] for a in ledger.list_alerts(world.sp)] == [aid]           # the route records nothing
    pg.close()


# ---------------------------------------------------------------------------------------------------- People
def test_people_lists_members_in_words_and_changes_go_through_the_app(browser, world):  # noqa: F811
    pg = _open(browser, world, "#/settings/people")
    sec = pg.locator(f'.set-project[data-project="{world.ep.project_id}"]')
    sec.wait_for()
    text = sec.inner_text()
    assert "Imani" in text and "review proxies plus what they work on" in text and "Sam" in text and "all the media" in text
    assert sec.locator('tr[data-person="Maren"]').get_by_text("You").is_visible()
    assert "dev-" not in pg.content()                                                     # no device ids, ever
    _shot(pg, "settings-people")
    sec.get_by_role("button", name="Share", exact=True).click()
    pg.wait_for_function("() => window.__asked.length === 1")
    sec.get_by_role("button", name="More for Episode 12").click()
    pg.get_by_role("menuitem", name="Hand over publishing").click()
    pg.wait_for_function("() => window.__asked.length === 2")
    pg.get_by_role("button", name="Join a shared project").click()
    pg.wait_for_function("() => window.__asked.length === 3")
    assert _asked(pg) == [["share", world.ep.project_id], ["handover", world.ep.project_id], ["join"]]   # the page names the project only
    assert not pg.errors
    pg.close()


def test_people_without_the_app_says_why(browser, world):  # noqa: F811
    pg = _open(browser, world, "#/settings/people", native=False)
    pg.wait_for_selector(".set-project")
    share = pg.locator(".set-project").first.get_by_role("button", name="Share", exact=True)
    assert share.is_disabled() and "AVE Studio app" in share.get_attribute("data-tip")
    assert pg.get_by_role("button", name="Join a shared project").is_disabled()
    pg.close()


# ---------------------------------------------------------------------------------------------------- Collaboration
def test_collaboration_holds_the_editorial_exchange(browser, world):  # noqa: F811
    pg = _open(browser, world, "#/settings/collaboration")
    pg.wait_for_selector("text=Sharing is stopped")
    for name in ("Enable sharing", "Invite collaborator", "Add collaborator", "Preview outgoing bundle"):
        assert pg.get_by_role("button", name=name, exact=True).count() == 1, name
    assert pg.get_by_role("button", name="Send a file", exact=True).count() == 0              # retired: Syncthing carries project media
    assert pg.get_by_role("button", name="Invite collaborator").is_disabled()                # not until sharing is on
    assert "Outbox" in pg.locator(".set-pane").inner_text() and "Inbox" in pg.locator(".set-pane").inner_text()
    assert pg.locator(".set-choices li").count() >= 1                                       # the drafts that could be shared
    _shot(pg, "settings-collaboration")
    pg.get_by_role("button", name="Enable sharing").click()
    pg.wait_for_function("() => window.__asked.length === 1")
    assert _asked(pg)[0] == ["connection", "exchangeEnable", {}]                           # the folder and the confirmation are native
    assert not pg.errors
    pg.close()


# ---------------------------------------------------------------------------------------------------- the approval sheet
def _sheet(browser, world, *, native=True, size=(1440, 900)):  # noqa: F811
    pg = _open(browser, world, "#/workspace/approvals", native=native, size=size)
    pg.wait_for_selector(".approval-sheet:not([hidden]) .ap-item")
    return pg


def test_the_approval_sheet_lists_posts_in_review_and_claudes_trims(browser, world):  # noqa: F811
    pg = _sheet(browser, world)
    sheet = pg.locator(".approval-sheet")
    pid, vid = world.rig.posts["review"]
    assert sheet.locator(f'.ap-item[data-variant="{vid}"]').count() == 1
    assert "Sourdough day three" in sheet.locator(f'.ap-item[data-variant="{vid}"]').inner_text()
    trim = sheet.locator('.ap-item[data-kind="trim"]')
    assert trim.count() == 1 and "Episode 12" in trim.inner_text() and "starts 0:04.25, ends 45:11.50" in trim.inner_text()
    rail = pg.locator(".rail-item", has_text="Approvals")
    assert rail.locator(".rail-count").inner_text() == str(sheet.locator(".ap-item").count())
    _shot(pg, "approvals-list")
    pg.set_viewport_size({"width": 1024, "height": 768})
    _shot(pg, "approvals-list")
    assert not pg.errors
    pg.close()


def test_a_post_is_approved_in_the_app_after_its_exact_files_were_on_screen(browser, world):  # noqa: F811
    pg = _sheet(browser, world)
    pid, vid = world.rig.posts["review"]
    sha = world.rig.item("review")["sha256"]
    pg.locator(f'.ap-item[data-variant="{vid}"]').click()
    pg.wait_for_selector(".ap-seal")
    assert pg.locator(".ap-sha").get_attribute("data-sha") == sha
    body = pg.locator(".approval-sheet").inner_text()
    assert "What your approval signs" in body and "#sourdough" in body and "Recipe in the link in bio." in body
    approve = pg.get_by_role("button", name="Approve on this Mac")
    assert approve.is_disabled()                                                            # not before two seconds of looking
    _shot(pg, "approvals-post")
    pg.wait_for_function("() => !document.querySelector('[data-act=approve]').disabled", timeout=6000)
    approve.click()
    pg.wait_for_function("() => window.__asked.length === 1")
    assert _asked(pg) == [["approvePublish", [{"post": pid, "variant": vid, "sha": sha}]]]    # identity only: no PIN, no name, no view time
    assert world.rig.item("review")["status"] == "awaiting_approval"                        # the stub approved nothing; the app would
    if pg.viewport_size["width"] == 1440:
        pg.set_viewport_size({"width": 1024, "height": 768})
        pg.locator(f'.ap-item[data-variant="{vid}"]').click()
        pg.wait_for_selector(".ap-seal")
        _shot(pg, "approvals-post")
    assert not pg.errors
    pg.close()


def test_without_the_app_the_approval_sheet_says_why_and_never_approves(browser, world):  # noqa: F811
    pg = _sheet(browser, world, native=False)
    pid, vid = world.rig.posts["review"]
    pg.locator(f'.ap-item[data-variant="{vid}"]').click()
    pg.wait_for_selector(".ap-seal")
    pg.wait_for_timeout(2300)
    assert pg.get_by_role("button", name="Approve on this Mac").is_disabled()
    assert "needs the AVE Studio app" in pg.locator(".ap-why").inner_text()
    assert f"ave studio approve {pid} {vid}" in pg.locator(".ap-cli").inner_text()
    assert pg.locator("input[type=password], input[name=pin]").count() == 0
    pg.close()


def test_a_trim_is_confirmed_in_the_app(browser, world):  # noqa: F811
    pg = _sheet(browser, world)
    pg.locator('.ap-item[data-kind="trim"]').click()
    pg.wait_for_selector(".ap-ends")
    assert pg.locator('[data-end="in"]').inner_text() == "0:04.25" and pg.locator('[data-end="out"]').inner_text() == "45:11.50"
    assert pg.get_by_role("link", name="Listen in the Room").get_attribute("href") == "#/room/ep12"
    _shot(pg, "approvals-trim")
    pg.set_viewport_size({"width": 1024, "height": 768})
    _shot(pg, "approvals-trim")
    pg.get_by_role("button", name="Confirm the trim on this Mac").click()
    pg.wait_for_function("() => window.__asked.length === 1")
    assert _asked(pg) == [["approveTrim", world.ep.project_id, "main"]]
    assert json.loads((world.ep.root / "episode.json").read_text())["trim"]["main"]["by"] == "claude"   # the app confirms, not the page
    assert not pg.errors
    pg.close()


def test_the_post_sheet_opens_the_approval_sheet_for_a_post_in_review(browser, world):  # noqa: F811
    pg = _open(browser, world, "#/workspace")
    pg.wait_for_selector(".ws-tabs .tab")
    pg.get_by_role("tab", name="Calendar", exact=True).click()
    pid, vid = world.rig.posts["review"]
    pg.locator(f'.cal-item[data-variant="{vid}"]').first.click()
    pg.wait_for_selector(".post-sheet:not([hidden]) [data-act=review-approval]")
    pg.locator(".post-sheet [data-act=review-approval]").click()
    pg.wait_for_selector(".approval-sheet:not([hidden]) .ap-seal")
    assert pg.locator("#ap-title").inner_text().startswith("Sourdough day three")
    assert not pg.errors
    pg.close()


def test_asking_for_changes_sends_the_post_back_to_claude(browser, world):  # noqa: F811
    pg = _sheet(browser, world)
    pid, vid = world.rig.posts["review"]
    pg.locator(f'.ap-item[data-variant="{vid}"]').click()
    pg.wait_for_selector(".ap-seal")
    pg.get_by_role("button", name="Ask for changes").click()
    pg.get_by_label("What should change").fill("Say what the bread tastes like.")
    pg.get_by_role("button", name="Send to Claude").click()
    pg.wait_for_selector(".approval-sheet .ap-group")
    status = lambda: next(v for v in world.rig.w.studio.post(pid)["variants"] if v["id"] == vid)["status"]
    world.rig.wait(pg, lambda: status() != "in_review")
    assert status() == "needs_rework"
    assert not pg.errors
    pg.close()


def test_the_board_points_claudes_trim_question_at_the_trim_sheet(browser, world):  # noqa: F811
    world.ep.log.append("decision.recorded", artifact="art_ep", actor="claude",
                        data={"decision": "dec_trim", "summary": "Trim for the draft (main): in 4.25s out 2711.50s",
                              "options": ["in 4.25s out 2711.50s", "keep the whole recording"], "chosen": "in 4.25s out 2711.50s",
                              "reason": "Used for drafts only: the final needs your `ave trim set`."})
    pg = _open(browser, world, f"#/workspace/p/{world.ep.project_id}")
    pg.wait_for_selector(".ws-row[data-key]")
    key = f"{world.ep.project_id}:art_ep"
    pg.wait_for_function(f"() => (document.querySelector('.ws-row[data-key=\"{key}\"] [data-col=waiting]')?.textContent || '').includes('Waiting on you')",
                         timeout=8000)
    pg.locator(f'.ws-row[data-key="{key}"] [data-col=waiting]').click()
    pg.get_by_role("button", name="Confirm it in the trim sheet").click()
    pg.wait_for_selector(".approval-sheet:not([hidden]) .ap-ends")
    assert not pg.errors
    pg.close()


def test_several_posts_are_approved_together_and_each_gets_its_own_answer(browser, world):  # noqa: F811
    """The old Review page's batch: tick the posts you looked at, approve them with one check; one refused post is shown."""
    pg = _sheet(browser, world)
    names = ("review", "fb")
    for n in names:
        pid, vid = world.rig.posts[n]
        tick = pg.locator(f'.ap-row:has(.ap-item[data-variant="{vid}"]) .ap-tick')
        assert tick.is_disabled()                                                           # not before it was looked at
        pg.locator(f'.ap-item[data-variant="{vid}"]').click()
        pg.wait_for_function("() => document.querySelector('.approval-sheet [data-act=approve]')?.disabled === false", timeout=8000)
        pg.get_by_role("button", name="All approvals").click()
        pg.wait_for_selector(".ap-group")
    for n in names:
        assert pg.locator(f'.ap-row:has(.ap-item[data-variant="{world.rig.posts[n][1]}"]) .ap-tick').get_attribute("aria-checked") == "true"
    pg.evaluate(f"window.__refuse = ['{world.rig.posts['fb'][0]}']")
    _shot(pg, "approvals-batch")
    pg.get_by_role("button", name="Approve 2 posts on this Mac").click()
    pg.wait_for_function("() => window.__asked.length === 1")
    sent = _asked(pg)[0]
    assert sent[0] == "approvePublish" and sorted((x["post"], x["variant"]) for x in sent[1]) == sorted(world.rig.posts[n] for n in names)
    assert all(set(x) == {"post", "variant", "sha"} for x in sent[1])                       # one call, one check, identities only
    row = pg.locator(f'.ap-row:has(.ap-item[data-variant="{world.rig.posts["fb"][1]}"])')
    row.locator(".ap-result").wait_for()
    assert "Not approved: the media changed" in row.inner_text()
    assert not pg.errors
    pg.close()


def _uncertain(world, name="approved"):
    from ave.studio import store
    from ave.studio.model import find_variant
    pid, vid = world.rig.posts[name]
    post = store.load_post(world.sp, pid)
    find_variant(post, vid)["result"] = {"receipt": {"phase": "uncertain", "source": "provider", "object": {}}}
    (world.sp.posts_dir / f"{pid}.json").write_text(json.dumps(post))          # what a send that timed out leaves behind
    return pid, vid, post["rev"]


def test_the_post_panel_holds_the_delivery_outcome_and_its_protected_acts(browser, world):  # noqa: F811
    pid, vid, rev = _uncertain(world)
    pg = _open(browser, world, f"#/workspace/post/{pid}")
    pg.wait_for_selector(".post-sheet:not([hidden]) .ps-outcome")
    out = pg.locator(".ps-outcome")
    assert "Evidence from the provider response." in out.inner_text()
    out.scroll_into_view_if_needed()
    _shot(pg, "post-delivery-outcome")
    out.get_by_role("button", name="Check with provider").click()
    pg.wait_for_function("() => window.__asked.length === 1")
    out.get_by_role("button", name="Review publication outcome").click()
    pg.wait_for_function("() => window.__asked.length === 2")
    asked = _asked(pg)
    assert [a[1] for a in asked] == ["providerRefresh", "reviewProviderOutcome"]
    for _, _, args in asked:                                   # exactly the editor's context: the post, variant, revision and operation
        assert set(args) <= {"post", "variant", "revision", "operation"} and (args["post"], args["variant"], args["revision"]) == (pid, vid, rev)
    assert not pg.errors
    pg.close()


def test_without_the_app_the_outcome_review_is_not_offered_and_the_alert_links_to_the_post(browser, world):  # noqa: F811
    from ave.studio import ledger
    pid, vid, rev = _uncertain(world)
    ledger.append_alert(world.sp, severity="error", kind="uncertain", message="Instagram may have the post.", account="ig-main", post=pid)
    pg = _open(browser, world, "#/settings/alerts", native=False)
    card = pg.locator(".set-alert", has_text="Instagram may have the post.")
    card.wait_for()
    assert card.get_by_role("link", name="Open the post").get_attribute("href") == f"#/workspace/post/{pid}"
    card.get_by_role("link", name="Open the post").click()
    pg.wait_for_selector(".post-sheet:not([hidden]) .ps-outcome")
    assert pg.locator(".ps-outcome").get_by_role("button", name="Review publication outcome").count() == 0
    assert "needs the AVE Studio app" in pg.locator(".ps-outcome").inner_text()
    pg.close()
