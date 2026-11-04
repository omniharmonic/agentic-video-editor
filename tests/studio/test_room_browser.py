"""The Room in a real browser: the stage follows the stream, an offered version can be kept or undone, and every control answers.

A studio holds one episode folder ("ep", the synthetic two-camera episode) with two versions of the rough cut: rough.s2.v8 (the
assembly, with a draft render in exports/) and rough.s2.v9 (two shots swapped). Offers render a real preview with ffmpeg.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from types import SimpleNamespace

import pytest

from ave import editdoc, preview
from ave.room import cut as room_cut
from ave.studio.project import StudioProject
from ave.studio.service import open_studio
from ave.studio.ui.server import StudioServer
from conftest import CI_SLOW, needs_ffmpeg

from . import board_bridge

pytestmark = needs_ffmpeg


@pytest.fixture(scope="module")
def browser():
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:
            pytest.skip(f"Chromium unavailable: {e}")
        yield b
        b.close()


def _swap(doc: dict, i: int) -> dict:
    out = editdoc.clone(doc)
    v = out["tracks"]["video"]
    a, b = v[i], v[i + 1]
    mid = a["o0"] + (b["o1"] - b["o0"])
    v[i], v[i + 1] = dict(b, o0=a["o0"], o1=mid), dict(a, o0=mid, o1=b["o1"])
    return out


@pytest.fixture
def room_server(tmp_path, synth_root):
    sp = StudioProject(tmp_path / "studio")
    sp.create(title="Room", tz="UTC")
    ep_root = sp.root / "ep"
    shutil.copytree(synth_root, ep_root)
    from ave.episode import Episode
    ep = Episode(ep_root)
    base = editdoc.clone(editdoc.load_latest(ep, "assembly"))
    base["stage"] = "rough"
    (ep.edits / "rough.s2.v8.json").write_text(json.dumps(base))
    (ep.edits / "rough.s2.v9.json").write_text(json.dumps(_swap(base, 1)))
    ep.exports.mkdir(parents=True, exist_ok=True)
    shutil.copy(ep.raw / "camA.mp4", ep.exports / "rough.s2.v8.proxy.mp4")       # a playable stand-in for the v8 draft render
    srv = StudioServer(open_studio(sp.root), pin="424242")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    t.join(5)
    srv.server_close()
    preview.wait_for_measurements(60)


TEST_FLAG = "window.__AVE_TEST__ = true;"      # exposes window.__room (event count, state) to the tests only


@pytest.fixture(autouse=True)
def no_agent_markers(monkeypatch):
    """The suite may run inside an agent harness; the app's bridge refuses every write while a marker is set."""
    from ave import humanpresence
    for k in humanpresence.AGENT_MARKERS:
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def page(browser, room_server):
    """The Room as the director uses it in the app: keep / maybe / skip and nudges go through the app's native bridge."""
    pg = browser.new_page(viewport={"width": 1440, "height": 900})
    pg.add_init_script(TEST_FLAG)
    board_bridge.install(pg, room_server)
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(room_server.app_url() + "#/room/ep")
    pg.wait_for_selector(".room")
    pg.wait_for_function("() => window.__room && window.__room.state.live === 'live'")
    yield pg
    pg.close()
    assert not errors


def show_v8(srv, page, at=0.0):
    srv.room.show(artifact="ep", view="cut", version="rough.s2.v8", position_s=at)
    page.wait_for_function("() => document.querySelector('video') && document.querySelector('video').readyState >= 1")


def offer_v9(srv, page, span=(0, 20), wait_ready=False):
    srv.offer("rough.s2.v9", span=span, summary="Opens on the question")
    page.get_by_role("button", name="Keep this").wait_for()
    if wait_ready:
        page.get_by_text("Playing the change", exact=False).wait_for(timeout=60_000)


def test_stage_follows_claude(page, room_server):
    room_server.bus.publish("stage", {"view": "cut", "version": "rough.s2.v8", "position_s": 12})
    page.wait_for_function("() => document.querySelector('video').currentTime > 11")
    assert page.locator(".version-chip").inner_text() == "Rough cut, session 2, version 8"
    assert page.locator(".version-chip").get_attribute("title") == "rough.s2.v8"
    room_server.room.show(artifact="ep", view="cut", version="rough.s2.v8", position_s=3)
    page.wait_for_function("() => Math.abs(document.querySelector('video').currentTime - 3) < 0.5")


def test_an_offered_version_shows_keep_and_go_back(page, room_server):
    show_v8(room_server, page)
    room_server.offer("rough.s2.v9", span=(0, 20), summary="Opens on the question")
    page.get_by_role("button", name="Keep this").wait_for()
    assert page.get_by_role("button", name="Keep this").is_visible()
    assert page.get_by_role("button", name="Go back").is_visible()
    assert page.get_by_text("Opens on the question").is_visible()
    assert page.locator(".room-replacing").inner_text() == "replacing version 8"
    assert page.get_by_text("Go back returns to version 8.").is_visible()
    page.get_by_text("Playing the change", exact=False).wait_for(timeout=60_000)
    assert "/preview/" in page.evaluate("document.querySelector('video').src")
    assert page.locator(".tl-changed").is_visible()
    assert page.get_by_text("Level approximate").is_visible()          # the first preview of a cut never waits on the full measure


def test_go_back_returns_to_the_previous_version(page, room_server):
    show_v8(room_server, page)
    offer_v9(room_server, page)
    page.get_by_role("button", name="Go back").click()
    page.wait_for_function("() => document.querySelector('.version-chip').title === 'rough.s2.v8'")
    page.get_by_text("Back on version 8").wait_for()
    assert room_server.stage.version == "rough.s2.v8"
    assert not page.get_by_role("button", name="Keep this").is_visible()
    assert room_server.room.read()["clicks"][-1]["action"] == "back"
    page.wait_for_function("() => document.querySelector('video').src.includes('/exports/rough.s2.v8')")
    page.locator(".toast").get_by_role("button", name="Undo").click()       # undo puts the offer back
    page.get_by_role("button", name="Keep this").wait_for()
    assert room_server.stage.version == "rough.s2.v9"


def test_keep_this_records_the_choice_and_stays(page, room_server):
    show_v8(room_server, page)
    offer_v9(room_server, page)
    page.get_by_role("button", name="Keep this").click()
    page.get_by_text("Kept version 9", exact=False).wait_for()
    assert not page.locator(".offer").is_visible()
    assert page.locator(".version-chip").inner_text() == "Rough cut, session 2, version 9"
    assert room_server.room.read()["clicks"][-1] == {**room_server.room.read()["clicks"][-1], "action": "keep", "version": "rough.s2.v9"}


def test_a_keep_said_in_chat_shows_kept_in_chat(page, room_server):
    show_v8(room_server, page)
    offer_v9(room_server, page)
    room_server.room.keep("rough.s2.v9", via="chat")                  # what room_keep does through the route
    page.get_by_text("Kept in chat: version 9", exact=False).wait_for()
    assert not page.locator(".offer").is_visible()


def test_a_go_back_said_in_chat_shows_back_in_chat(page, room_server):
    show_v8(room_server, page)
    offer_v9(room_server, page)
    room_server.room.back("rough.s2.v9", via="chat")
    page.get_by_text("Back in chat: version 8", exact=False).wait_for()


def test_a_change_too_large_to_preview_opens_the_full_cut(page, room_server, monkeypatch):
    monkeypatch.setattr(room_cut, "preview_offer", lambda ep, a, v, c, s: {"artifact": a, "version": v, "status": "too_large", "span": [0, 400]})
    show_v8(room_server, page)
    offer_v9(room_server, page)
    page.get_by_text("Change too large to preview here, open the full cut").wait_for()
    page.get_by_role("button", name="Open the full cut").click()
    page.wait_for_function("() => document.querySelector('video').src.includes('/exports/rough.s2.v9')", timeout=120_000)
    assert page.locator(".cut-overlay").is_hidden()


def test_a_flag_waits_for_the_next_message(page, room_server):
    show_v8(room_server, page, at=6)
    page.get_by_role("button", name="Flag this moment").click()
    page.get_by_role("textbox", name="Note for Claude").fill("Too long here")
    page.get_by_role("button", name="Flag for Claude").click()
    page.get_by_text("1 flag waiting for your next message").wait_for()
    got = room_server.room.read()
    assert got["flags"][-1]["note"] == "Too long here" and got["flags"][-1]["artifact"] == "ep"
    room_server.room.read(got["ack"])                                       # Claude's next read confirms it
    page.get_by_text("Claude read your flags").wait_for()
    assert not page.locator(".room-flags").is_visible()


def test_keyboard_focus_is_visible_on_every_control(page, room_server):
    show_v8(room_server, page)
    offer_v9(room_server, page)
    page.locator("body").focus()
    seen = 0
    for _ in range(60):
        page.keyboard.press("Tab")
        info = page.evaluate("""() => { const e = document.activeElement; if (!e || e === document.body) return null;
            const s = getComputedStyle(e); return {name: e.getAttribute('aria-label') || e.textContent.trim(),
            ring: s.boxShadow !== 'none' || (s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) > 0)}; }""")
        if info is None:
            continue
        assert info["ring"], f"no visible focus on {info['name']!r}"
        seen += 1
    assert seen >= 8


def test_reduced_motion_is_respected(browser, room_server):
    pg = browser.new_page(viewport={"width": 1024, "height": 768}, reduced_motion="reduce")
    pg.add_init_script(TEST_FLAG)
    try:
        pg.goto(room_server.app_url() + "#/room/ep")
        pg.wait_for_function("() => window.__room && window.__room.state.live === 'live'")
        show_v8(room_server, pg)
        offer_v9(room_server, pg)
        styles = pg.evaluate("""() => [...document.querySelectorAll('.offer, .cut-frame, .btn, .tl-seg')].map(e => {
            const s = getComputedStyle(e); return [s.animationDuration, s.transitionDuration]; })""")
        assert styles and all(a in ("0s", "") and all(x == "0s" for x in t.split(", ")) for a, t in styles), styles
    finally:
        pg.close()


SIGNATURE = """() => { const v = document.querySelector('video');
  return JSON.stringify({events: window.__room.events, text: document.querySelector('.room').innerText,
    pressed: [...document.querySelectorAll('[aria-pressed],[aria-expanded]')].map(e => e.getAttribute('aria-pressed') + e.getAttribute('aria-expanded')),
    paused: v && v.paused, muted: v && v.muted, src: v && v.src, t: v && Math.round(v.currentTime * 4),
    popover: !!document.querySelector('.popover'), toasts: document.querySelectorAll('.toast').length}); }"""


def test_every_visible_button_does_something(page, room_server):
    show_v8(room_server, page)
    offer_v9(room_server, page, span=(10, 20), wait_ready=True)
    page.evaluate("document.querySelector('video').pause()")                     # the change autoplays; start from a still frame
    page.get_by_role("button", name="Play", exact=True).wait_for()
    names = page.evaluate("""() => [...document.querySelectorAll('.room button:not([role=tab])')].filter(b => b.offsetParent && !b.disabled)
        .map(b => b.getAttribute('aria-label') || b.textContent.trim())""")
    last = ["Watch the full cut", "Go back", "Keep this"]
    names = [n for n in names if n not in last] + [n for n in last if n in names]
    assert {"Play", "Back 5 seconds", "Forward 5 seconds", "Flag this moment", "Mute", "Go back", "Keep this"} <= set(names), names
    for name in names:
        if name in ("Go back", "Keep this") and not page.get_by_role("button", name=name, exact=True).is_visible():
            offer_v9(room_server, page, span=(10, 20))
        btn = page.locator(".room").get_by_role("button", name=name, exact=True).first
        if not btn.is_visible() or not btn.is_enabled():
            continue                                    # a control that an earlier click legitimately removed (the offer closed)
        before = page.evaluate(SIGNATURE)
        btn.click()
        page.wait_for_function(f"() => ({SIGNATURE})() !== {json.dumps(before)}", timeout=5000)
        if name == "Go back":
            page.locator(".offer").wait_for(state="hidden")         # the reply lands after the button's busy state shows
        page.keyboard.press("Escape")                   # close a popover the click opened
        if name == "Play":
            page.get_by_role("button", name="Pause").click()
    toast_close = page.locator(".toast").get_by_role("button", name="Close")
    assert toast_close.count(), "the clicks above left no toast to close"
    for _ in range(20):                                  # toasts also expire on their own, so take the first one each time
        n = page.locator(".toast").count()
        if not n:
            break
        page.evaluate("document.querySelector('.toast [aria-label=Close]').click()")
        page.wait_for_function(f"() => document.querySelectorAll('.toast').length < {n}")


def test_the_kit_shows_every_component(page, room_server):
    page.goto(room_server.app_url().replace("/app/", "/app/kit.html"))
    for heading in ("Type", "Colour", "Buttons", "Split button and menu", "Text fields", "Popover and toast", "Tabs",
                    "Avatars and status", "Table cells", "Loading and scrolling"):
        assert page.get_by_role("heading", name=heading, exact=True).is_visible()
    fonts = page.evaluate("document.fonts.ready.then(() => [...document.fonts].filter(f => f.status === 'loaded').map(f => f.family))")
    assert any("Figtree" in f for f in fonts) and any("Poppins" in f for f in fonts)
    page.get_by_role("button", name="Dark theme").click()
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"


NO_RAW_NAMES = ("rough.s2", ".mp4", "camA", "camB", ".json")


def test_people_read_names_not_files(page, room_server):
    show_v8(room_server, page)
    offer_v9(room_server, page, wait_ready=True)
    text = page.locator(".room").inner_text() + page.title()
    assert not [n for n in NO_RAW_NAMES if n in text], text
    assert page.get_by_text("Coming in").is_visible() and page.get_by_text("Going out").is_visible()
    assert page.locator(".strip-in .strip-item").first.inner_text().endswith("camera")


def test_back_carries_the_version_on_stage(page, room_server):
    show_v8(room_server, page)
    offer_v9(room_server, page)
    seen = []
    page.on("request", lambda r: seen.append(r.post_data_json) if r.url.endswith("/api/room/back") else None)
    page.get_by_role("button", name="Go back").click()
    page.get_by_text("Back on version 8").wait_for()
    assert seen == [{"version": "rough.s2.v9"}]


def test_focus_stays_put_while_the_preview_arrives_and_lands_on_play_after_a_decision(page, room_server):
    show_v8(room_server, page)
    room_server.offer("rough.s2.v9", span=(0, 20), summary="Opens on the question")
    keep = page.get_by_role("button", name="Keep this")
    keep.focus()                                          # before the preview is ready
    page.get_by_text("Playing the change", exact=False).wait_for(timeout=60_000)
    assert page.evaluate("document.activeElement.textContent.trim()") == "Keep this"
    page.keyboard.press("Enter")
    page.get_by_text("Kept version 9", exact=False).wait_for()
    page.wait_for_function("() => document.activeElement && document.activeElement.classList.contains('cut-play')")


def test_the_shots_are_one_tab_stop_moved_with_arrows(page, room_server):
    show_v8(room_server, page)
    page.locator(".tl-ruler").focus()
    assert page.evaluate("document.activeElement.getAttribute('role')") == "slider"
    assert page.locator(".tl-ruler .tl-seg").count() == 0                    # no buttons inside the slider
    page.keyboard.press("Tab")
    first = page.evaluate("document.activeElement.getAttribute('aria-label')")
    assert page.evaluate("document.activeElement.classList.contains('tl-seg')")
    assert page.locator(".tl-seg[tabindex='0']").count() == 1
    page.keyboard.press("ArrowRight")
    second = page.evaluate("document.activeElement.getAttribute('aria-label')")
    assert second != first and page.evaluate("document.activeElement.classList.contains('tl-seg')")
    page.keyboard.press("Enter")
    assert page.evaluate("document.activeElement.getAttribute('aria-pressed')") == "true"
    assert page.locator(".tl-detail").is_visible()
    page.keyboard.press("Tab")
    assert not page.evaluate("document.activeElement.classList.contains('tl-seg')")    # one Tab leaves all the shots


def test_blocked_playback_shows_a_play_prompt(browser, room_server):
    pg = browser.new_page(viewport={"width": 1440, "height": 900})
    pg.add_init_script(TEST_FLAG + """
      window.__blockPlay = true;
      const real = HTMLMediaElement.prototype.play;
      HTMLMediaElement.prototype.play = function () {
        return window.__blockPlay ? Promise.reject(new DOMException('blocked', 'NotAllowedError')) : real.call(this); };""")
    try:
        pg.goto(room_server.app_url() + "#/room/ep")
        pg.wait_for_function("() => window.__room && window.__room.state.live === 'live'")
        show_v8(room_server, pg)
        offer_v9(room_server, pg, wait_ready=True)
        prompt = pg.get_by_role("button", name="Play the change")
        prompt.wait_for()
        pg.evaluate("window.__blockPlay = false")
        prompt.click()
        pg.wait_for_function("() => !document.querySelector('video').paused")
        assert not prompt.is_visible()
    finally:
        pg.close()


def test_a_video_that_will_not_play_says_so_and_offers_the_full_cut(page, room_server):
    page.route("**/api/room/media/ep/preview/**", lambda r: r.abort())
    show_v8(room_server, page)
    offer_v9(room_server, page)
    page.get_by_text("This version could not play").wait_for(timeout=60_000)
    page.locator(".cut-overlay").get_by_role("button", name="Open the full cut").click()
    page.wait_for_function("() => document.querySelector('video').src.includes('/exports/rough.s2.v9')", timeout=120_000)


def test_a_stream_that_closes_retries_then_offers_try_again(browser, room_server):
    pg = browser.new_page(viewport={"width": 1440, "height": 900})
    pg.add_init_script(TEST_FLAG)
    refuse = lambda r: r.fulfill(status=503, body="at most 8 streams are open")
    pg.route("**/api/room/stream*", refuse)
    try:
        pg.goto(room_server.app_url() + "#/room/ep")
        pg.get_by_text("Reconnecting").wait_for()
        pg.get_by_text("Lost the connection to the app").wait_for(timeout=20_000)
        pg.unroute("**/api/room/stream*", refuse)
        pg.locator(".room-head").get_by_role("button", name="Try again").click()
        pg.wait_for_function("() => window.__room.state.live === 'live'")
        room_server.room.show(artifact="ep", view="cut", version="rough.s2.v8")
        pg.wait_for_function("() => document.querySelector('.version-chip').title === 'rough.s2.v8'")
    finally:
        pg.close()


# ---------------------------------------------------------------------------------------------------------------------------------
# Script and Selects (Task 12). The synthetic episode's transcript: eight lines between Host and Guest over 30 s, on the camA clock.
# Selects: the clip finder ran on the assembly; Claude picked two clips, the recall fill surfaced one more, and one of Claude's picks
# is already kept (so it is in the cut).

from ave import clips as clip_finder                                     # noqa: E402
from ave.episode import Episode                                         # noqa: E402
from ave.project import Project                                         # noqa: E402


def _pick(ids, title, why):
    return {"title": title, "hook": why, "sentence_ids": ids, "rationale": why,
            "scores": {"hook": 8, "arc": 7, "insight": 7, "emotion": 6, "shareability": 7, "ending": 7, "speech_quality": 8}}


@pytest.fixture
def selects_ep(room_server):
    ep = Episode(room_server.project_root / "ep")
    project = Project.create(ep.root, "synthetic", person="Maren")
    clip_finder.prepare(ep, stage="assembly", min_s=3, max_s=12)
    clip_finder.propose(ep, [_pick(["s000", "s001", "s002"], "A warm welcome", "Opens the show with both voices"),
                             _pick(["s003", "s004"], "Systems that carry you", "The guest's whole argument in one line")])
    clip_finder.decide(ep, [{"id": "c01", "verdict": "keep"}], by="Maren", source="ui")
    add_alternate(ep)
    return SimpleNamespace(ep=ep, project=project)


def add_alternate(ep, first="s003", last="s005", cid="c03"):
    """30 s leaves the recall fill nothing above its floor, so make the section it would surface with the finder's own auto_clip
    (floor lowered): the real heuristic shape, with its scoring note in `rationale`."""
    import copy
    cand = clip_finder.load_candidates(ep, None)
    rubric = copy.deepcopy(clip_finder.load_rubric(ep))
    rubric.setdefault("recall", {})["floor"] = 0
    table, _ = clip_finder._live_table(ep, cand, rubric)
    alt, problems = clip_finder.auto_clip(table, {"id": "w", "start_sid": first, "end_sid": last}, cand["params"], rubric)
    assert alt and alt["origin"] == "heuristic" and ";" in alt["rationale"], problems
    alt.update(id=cid, slug=cid, status="proposed", created="2026-11-03T00:00:00+0000", edit=None)
    alt.update(clip_finder.finalize_fields(table, alt, cand["params"], rubric))
    data = clip_finder.load_clips(ep)
    data["clips"].append(alt)
    clip_finder.assign_ranks(data)
    (ep.work / "clips.json").write_text(json.dumps(data))
    return alt


def script_page(srv, page, highlights=None, at=None):
    srv.room.show(artifact="ep", view="script", **({"position_s": at} if at is not None else {}))
    if highlights is not None:
        srv.room.highlight(highlights)
    page.locator(".sc-line").first.wait_for()


def selects_page(srv, page):
    srv.room.show(artifact="ep", view="selects")
    page.locator(".sel-card:not(.is-skeleton)").first.wait_for()          # the skeleton cards are painted first


def test_highlighted_sections_play_their_clip(page, room_server):
    script_page(room_server, page, [{"start": 8.0, "end": 12.0, "note": "The guest says thanks"}])
    pull = page.locator(".sc-pull").first
    assert "The guest says thanks" in pull.inner_text()
    assert page.locator(".sc-line.is-pulled").count() == 1
    pull.click()
    page.wait_for_function("() => { const v = document.querySelector('.script video'); return v && !v.paused && v.currentTime >= 8; }")
    assert "/api/room/source/ep/" in page.evaluate("document.querySelector('.script video').src")
    page.wait_for_function("() => { const v = document.querySelector('.script video'); return v.paused && v.currentTime >= 11.7; }", timeout=15_000)
    assert page.evaluate("document.querySelector('.script video').currentTime") < 12.6       # it stops at the end of the section
    assert page.locator(".sc-line.is-playing").inner_text().find("thanks for having me") >= 0


def test_clicking_a_line_plays_it_and_the_playing_line_follows(page, room_server):
    script_page(room_server, page)
    page.locator(".sc-line").nth(3).locator(".sc-time").click()          # "and the thing is um..." at 12.6 s
    page.wait_for_function("() => { const v = document.querySelector('.script video'); return !v.paused && v.currentTime >= 12.5; }")
    page.wait_for_function("() => document.querySelector('.sc-line.is-playing') && document.querySelector('.sc-line.is-playing').dataset.i === '4'",
                           timeout=10_000)                               # playback moved on to the next line, and so did the mark


def test_search_finds_and_marks_lines(page, room_server):
    script_page(room_server, page)
    page.keyboard.press("/")
    assert page.evaluate("document.activeElement.getAttribute('aria-label')") == "Search the transcript"
    page.keyboard.type("that")
    page.wait_for_function("() => document.querySelectorAll('mark.sc-hit').length === 4")
    assert page.locator(".sc-count").inner_text() == "1 of 4"
    assert page.locator("mark.sc-hit.is-current").count() == 1
    page.keyboard.press("Enter")
    assert page.locator(".sc-count").inner_text() == "2 of 4"
    page.keyboard.press("Shift+Enter")
    assert page.locator(".sc-count").inner_text() == "1 of 4"
    page.get_by_role("button", name="Next match").click()
    assert page.locator(".sc-count").inner_text() == "2 of 4"
    page.get_by_role("button", name="Previous match").click()
    assert page.locator(".sc-count").inner_text() == "1 of 4"
    page.keyboard.press("Escape")                                       # leaves the field, keeps the matches
    page.keyboard.press("n")
    assert page.locator(".sc-count").inner_text() == "2 of 4"
    page.keyboard.press("Shift+N")
    assert page.locator(".sc-count").inner_text() == "1 of 4"
    page.get_by_role("searchbox", name="Search the transcript").fill("zebra")
    page.get_by_text("No line says that").wait_for()
    assert page.locator("mark.sc-hit").count() == 0


def test_flag_shows_as_waiting_for_your_next_message(page, room_server):
    script_page(room_server, page)
    line = page.locator(".sc-line").nth(1)
    line.hover()
    line.get_by_role("button", name="Flag this line for Claude").click()
    page.get_by_role("textbox", name="Note for Claude").fill("Use this as the cold open")
    page.get_by_role("button", name="Flag for Claude").click()
    mark = line.locator(".sc-flag")
    mark.get_by_text("Waiting for your next message").wait_for()
    got = room_server.room.read()
    f = got["flags"][-1]
    assert f["view"] == "script" and f["note"] == "Use this as the cold open" and f["start"] == 8.0 and f["end"] == 11.95
    assert f["text"].startswith("thanks for having me")
    room_server.room.read(got["ack"])
    mark.get_by_text("Claude has it").wait_for()


def test_selected_words_can_be_flagged_with_f(page, room_server):
    script_page(room_server, page)
    page.evaluate("""() => { const t = document.querySelectorAll('.sc-line')[0].querySelector('.sc-text').firstChild;
        const r = document.createRange(); r.setStart(t, 0); r.setEnd(t, 'welcome to the show'.length);
        const s = getSelection(); s.removeAllRanges(); s.addRange(r); document.dispatchEvent(new Event('selectionchange')); }""")
    page.get_by_role("button", name="Flag the selection for Claude").wait_for()
    page.keyboard.press("f")
    page.get_by_role("button", name="Flag for Claude").click()
    page.locator(".sc-line").first.locator(".sc-flag").get_by_text("Waiting for your next message").wait_for()
    f = room_server.room.read()["flags"][-1]
    assert f["text"] == "welcome to the show" and f["start"] == 0.5 and f["end"] < 2.2


def test_claude_switching_view_moves_the_stage(page, room_server):
    room_server.room.show(artifact="ep", view="script")
    page.get_by_role("tab", name="Script").wait_for()
    page.wait_for_function("() => document.querySelector('[role=tab][aria-selected=true]').textContent.trim() === 'Script'")
    assert page.locator(".script").is_visible() and not page.locator(".cut").is_visible()
    room_server.room.show(artifact="ep", view="selects")
    page.wait_for_function("() => document.querySelector('[role=tab][aria-selected=true]').textContent.trim() === 'Selects'")
    assert page.locator(".selects").is_visible() and not page.locator(".script").is_visible()
    assert [t.strip() for t in page.get_by_role("tab").all_inner_texts()] == ["Cut", "Script", "Selects"]
    page.locator("body").press("1")                                    # the director switches by hand; Claude sees it in room_read
    page.wait_for_function("() => document.querySelector('[role=tab][aria-selected=true]').textContent.trim() === 'Cut'")
    deadline = time.monotonic() + 5
    while room_server.stage.view != "cut" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert room_server.stage.view == "cut"
    page.locator("body").press("2")
    page.wait_for_function("() => document.querySelector('[role=tab][aria-selected=true]').textContent.trim() === 'Script'")
    page.get_by_role("tab", name="Cut").click()
    page.wait_for_function("() => document.querySelector('[role=tab][aria-selected=true]').textContent.trim() === 'Cut'")
    assert page.get_by_text("No version of this on stage yet").is_visible()
    page.locator("body").press("3")
    page.wait_for_function("() => document.querySelector('[role=tab][aria-selected=true]').textContent.trim() === 'Selects'")


def test_script_follows_claude_to_a_position(page, room_server):
    script_page(room_server, page, at=17.5)
    page.wait_for_function("() => document.querySelector('.sc-line.is-playing') && document.querySelector('.sc-line.is-playing').dataset.i === '6'")
    page.wait_for_function("() => Math.abs(document.querySelector('.script video').currentTime - 17.5) < 0.5")


def card(page, title):
    return page.locator(".sel-card:not(.is-skeleton)", has_text=title)


def test_selects_show_in_the_cut_alternate_and_new(page, room_server, selects_ep):
    selects_page(room_server, page)
    assert card(page, "A warm welcome").locator(".sel-state").first.inner_text() == "Picked"
    assert card(page, "Systems that carry you").locator(".sel-state").first.inner_text() == "New"
    alts = page.locator(".sel-card:not(.is-skeleton)", has=page.locator(".sel-state:visible", has_text="Alternate"))
    assert alts.count() == 1
    why = alts.locator(".sel-why").inner_text()
    assert why.startswith("The clip finder also found this") and ";" not in why and "hook at" not in why        # never a scoring note
    c = card(page, "Systems that carry you")
    assert "The guest's whole argument in one line" in c.inner_text()
    assert "Host and Guest" in c.locator(".sel-meta").inner_text() and "0:12" in c.locator(".sel-meta").inner_text()
    page.wait_for_function("() => [...document.querySelectorAll('.sel-thumb img')].every(i => i.complete && i.naturalWidth > 0)", timeout=30_000)
    assert list((selects_ep.ep.work / "room" / "thumbs").glob("*.jpg"))               # made once, kept under work/


def test_keep_maybe_skip_are_recorded_as_the_directors(page, room_server, selects_ep):
    selects_page(room_server, page)
    c = card(page, "Systems that carry you")
    c.get_by_role("button", name="Keep").click()
    c.locator(".sel-state", has_text="Picked").wait_for()
    assert c.get_by_role("button", name="Keep").get_attribute("aria-pressed") == "true"
    data = clip_finder.load_clips(selects_ep.ep)
    assert next(x for x in data["clips"] if x["title"] == "Systems that carry you")["status"] == "approved"
    ledger = [json.loads(l) for l in (selects_ep.ep.work / "learning" / "clip_decisions.jsonl").read_text().splitlines()]
    assert ledger[-1]["verdict"] == "keep" and ledger[-1]["source"] == "ui"
    ev = [e for e in selects_ep.project.read_events() if e["op"] == "clip.decided"][-1]
    assert ev["actor"] == "human" and ev["data"]["choice"] == "keep" and ev["artifact"] == "art_ep"
    page.locator(".toast").get_by_role("button", name="Undo").click()
    c.locator(".sel-state", has_text="New").wait_for()
    assert next(x for x in clip_finder.load_clips(selects_ep.ep)["clips"] if x["title"] == "Systems that carry you")["status"] == "proposed"
    from ave import learning
    assert "c02" not in learning.latest_decisions(selects_ep.ep)                       # the undone keep is out of the ledger's view
    c.get_by_role("button", name="Maybe").click()
    c.locator(".sel-state", has_text="Maybe").wait_for()
    c.get_by_role("button", name="Skip").click()
    c.locator(".sel-state", has_text="Skipped").wait_for()
    verdicts = [e["data"]["choice"] for e in selects_ep.project.read_events() if e["op"] == "clip.decided"]
    assert verdicts[-3:] == ["pending", "maybe", "skip"] and all(e["actor"] == "human" for e in selects_ep.project.read_events())


def test_a_select_previews_in_the_viewer_and_can_be_flagged(page, room_server, selects_ep):
    selects_page(room_server, page)
    c = card(page, "Systems that carry you")
    c.locator(".sel-open").click()
    page.wait_for_function("() => { const v = document.querySelector('.selects video'); return v && !v.paused && v.currentTime > 11.4; }")
    assert page.locator(".sel-detail-title").inner_text() == "Systems that carry you"
    assert page.locator(".selects .viewer-label").inner_text() == "Host and Guest, 0:12 to 0:25"
    page.keyboard.press("f")
    page.get_by_role("textbox", name="Note for Claude").fill("Too long")
    page.get_by_role("button", name="Flag for Claude").click()
    c.locator(".sel-flagged", has_text="Waiting for your next message").wait_for()
    got = room_server.room.read()
    f = got["flags"][-1]
    assert f["view"] == "selects" and f["clip"] == "c02" and f["note"] == "Too long" and abs(f["start"] - 12.575) < 0.01
    room_server.room.read(got["ack"])
    c.locator(".sel-flagged", has_text="Claude has it").wait_for()


@pytest.fixture
def long_ep(room_server):
    """Two hours, 20,000 words, 2,000 lines: the size of a long conversation."""
    root = room_server.project_root / "long"
    shutil.copytree(room_server.project_root / "ep", root, ignore=shutil.ignore_patterns(".ave"))
    meta = json.loads((root / "episode.json").read_text())
    meta.pop("speakers", None)                                          # nobody named: the script says Speaker 1 and Speaker 2
    (root / "episode.json").write_text(json.dumps(meta))
    vocab = "we kept building the thing because people kept asking for it and that is how a small idea becomes a habit".split()
    words, utts, t = [], [], 0.5
    for n in range(2000):
        first = len(words)
        for k in range(10):
            w = vocab[(n * 7 + k) % len(vocab)] + (" lighthouse" if n == 1999 and k == 9 else "")
            words.append({"w": w, "start": round(t, 2), "end": round(t + 0.3, 2), "speaker": "S1" if n % 2 == 0 else "S2"})
            t += 0.36
        utts.append({"speaker": words[first]["speaker"], "start": words[first]["start"], "end": words[-1]["end"], "first_word": first,
                     "last_word": len(words) - 1, "text": " ".join(x["w"] for x in words[first:])})
        t += 0.0
    (root / "work" / "transcript.json").write_text(json.dumps({"source": "raw/camA.mp4", "duration": t, "words": words, "utterances": utts,
                                                               "speakers": {"S1": {"name": None}, "S2": {"name": None}}}))
    return root


LONG_TASKS = """window.__long = []; new PerformanceObserver(l => window.__long.push(...l.getEntries().map(e => e.duration)))
  .observe({type: 'longtask', buffered: true});"""


def test_a_long_transcript_renders_and_scrolls_smoothly(browser, room_server, long_ep):
    pg = browser.new_page(viewport={"width": 1440, "height": 900})
    pg.add_init_script(TEST_FLAG)
    try:
        pg.goto(room_server.app_url() + "#/room/long")
        pg.wait_for_function("() => window.__room && window.__room.state.live === 'live'")
        pg.evaluate(LONG_TASKS)
        room_server.room.show(artifact="long", view="script")
        pg.wait_for_function("() => document.querySelectorAll('.sc-line').length === 2000", timeout=20_000)
        pg.evaluate("""() => new Promise(done => { const s = document.querySelector('.sc-scroll'); let n = 0;
            const step = () => { s.scrollTop += 2400; if (++n < 60) requestAnimationFrame(step); else done(); }; requestAnimationFrame(step); })""")
        pg.get_by_role("searchbox", name="Search the transcript").fill("lighthouse")
        pg.wait_for_function("() => document.querySelector('.sc-count').textContent === '1 of 1'")
        pg.wait_for_function("() => { const m = document.querySelector('mark.sc-hit.is-current'); const r = m && m.getBoundingClientRect(); return r && r.top > 0 && r.bottom < innerHeight; }")
        pg.get_by_role("searchbox", name="Search the transcript").fill("the")
        pg.wait_for_function("() => /^1 of [0-9]{3,}$/.test(document.querySelector('.sc-count').textContent)", timeout=10_000)
        pg.wait_for_function("""() => { const n = Number(document.querySelector('.sc-count').textContent.split(' of ')[1]);
            return n > 500 && document.querySelectorAll('mark.sc-hit').length === n; }""", timeout=10_000)
        pg.wait_for_timeout(300)
        long = pg.evaluate("window.__long")
        assert not [d for d in long if d > 50 * CI_SLOW], long
        assert "Speaker 1" in pg.locator(".sc-line").first.inner_text()          # no names in episode.json or the transcript
    finally:
        pg.close()


INTERNAL = ("c01", "c02", "c03", "rough.s2", ".mp4", ".jpg", "camA", "camB", ".json", "S1", "S2", "/api/")


def test_script_and_selects_show_names_not_ids(page, room_server, selects_ep):
    script_page(room_server, page, [{"clip": "c02", "note": "Claude's pick"}])
    selects = None
    for view in ("script", "selects"):
        if view == "selects":
            selects_page(room_server, page)
        seen = page.evaluate("""() => { const r = document.querySelector('.room'); const out = [r.innerText];
            for (const e of r.querySelectorAll('*')) for (const a of ['title', 'data-tip', 'aria-label', 'alt', 'placeholder'])
              if (e.getAttribute(a) && e.offsetParent) out.push(e.getAttribute(a));
            return out.join('\\n'); }""")
        assert not [n for n in INTERNAL if n in seen], (view, seen)
        selects = seen
    assert "Host" in selects and "Guest" in selects


VIEW_SIGNATURE = """(sel) => { const root = document.querySelector(sel); const v = root.querySelector('video');
  return JSON.stringify({events: window.__room.events, text: root.innerText, cls: [...root.querySelectorAll('.is-current,.is-on,.is-playing,[aria-pressed=true],[aria-selected=true],[aria-expanded=true]')].length,
    pressed: [...root.querySelectorAll('[aria-pressed],[aria-expanded]')].map(e => e.getAttribute('aria-pressed') + e.getAttribute('aria-expanded')),
    paused: v && v.paused, muted: v && v.muted, src: v && v.src, t: v && Math.round(v.currentTime * 4), focus: document.activeElement === root.querySelector('input'),
    popover: !!document.querySelector('.popover'), menu: !!document.querySelector('.menu'), toasts: document.querySelectorAll('.toast').length}); }"""


@pytest.mark.parametrize("view", ["script", "selects"])
def test_every_visible_control_in_script_and_selects_does_something(page, room_server, selects_ep, view):
    if view == "script":
        script_page(room_server, page, [{"start": 8.0, "end": 12.0, "note": "Thanks"}])
    else:
        selects_page(room_server, page)
    sel = f".{view}"
    ctrls = page.evaluate(f"""() => [...document.querySelector('{sel}').querySelectorAll('button, input, [role=tab]')]
        .filter(b => b.offsetParent && !b.disabled).map((b, i) => (b.dataset.probe = i, i))""")
    assert len(ctrls) >= 8
    for i in ctrls:
        el = page.locator(f'[data-probe="{i}"]')
        if not el.count() or not el.is_visible() or not el.is_enabled():
            continue
        el.scroll_into_view_if_needed()
        el.hover()
        before = page.evaluate(VIEW_SIGNATURE, sel)
        if el.evaluate("e => e.tagName") == "INPUT":
            el.fill("the")
        else:
            el.click()
        page.wait_for_function(f"(sel) => ({VIEW_SIGNATURE})(sel) !== {json.dumps(before)}", arg=sel, timeout=5000)
        page.keyboard.press("Escape")
        page.evaluate("() => document.querySelectorAll('video').forEach(v => v.pause())")


# ---- Task 12, fix round 1

def _clips_edit(ep, fn):
    data = clip_finder.load_clips(ep)
    fn(data)
    (ep.work / "clips.json").write_text(json.dumps(data))


def test_an_alternate_marked_maybe_still_says_alternate(page, room_server, selects_ep):
    selects_page(room_server, page)
    c = page.locator(".sel-card:not(.is-skeleton)", has=page.locator(".sel-state:visible", has_text="Alternate"))
    c.get_by_role("button", name="Maybe").click()
    c.locator(".sel-state", has_text="Maybe").wait_for()
    assert c.locator(".sel-origin").is_visible() and c.locator(".sel-origin").inner_text() == "Alternate"
    page.locator(".sel-filter", has_text="Alternates").click()
    assert page.locator(".sel-card:not(.is-skeleton)").count() == 1


def test_a_stale_select_can_only_be_skipped(page, room_server, selects_ep):
    _clips_edit(selects_ep.ep, lambda d: next(c for c in d["clips"] if c["id"] == "c02").update(stale=True, stale_reason="edit changed"))
    selects_page(room_server, page)
    c = card(page, "Systems that carry you")
    assert c.get_by_role("button", name="Keep").is_disabled() and c.get_by_role("button", name="Maybe").is_disabled()
    assert "older version of the cut" in c.locator(".sel-stale-note").inner_text()
    assert post_status(room_server, "selects/decide", {"artifact": "ep", "clip": "c02", "verdict": "keep"}) == 409
    c.get_by_role("button", name="Skip").click()
    c.locator(".sel-state", has_text="Skipped").wait_for()
    assert not page.get_by_text("The stage changed").count()


def post_status(srv, route, body):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(f"http://127.0.0.1:{srv.port}/api/room/{route}", data=json.dumps(body).encode(), method="POST",
                                 headers={"X-AVE-Token": srv.token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_undo_holds_only_while_the_call_does_and_a_new_call_replaces_its_toast(page, room_server, selects_ep):
    from ave.room import review
    selects_page(room_server, page)
    c = card(page, "Systems that carry you")
    c.get_by_role("button", name="Keep").click()
    c.locator(".sel-state", has_text="Picked").wait_for()
    got = review.decide(selects_ep.ep, "c02", "skip")                    # the call changes elsewhere (another window, the CLI)
    room_server.bus.publish("selects", {"artifact": "ep", **got})
    c.locator(".sel-state", has_text="Skipped").wait_for()
    page.locator(".toast", has_text="Picked").get_by_role("button", name="Undo").click()
    page.get_by_text("Nothing to undo: the call on this select changed since.").wait_for()
    assert next(x for x in clip_finder.load_clips(selects_ep.ep)["clips"] if x["id"] == "c02")["status"] == "rejected"
    w = card(page, "A warm welcome")
    w.get_by_role("button", name="Maybe").click()
    w.locator(".sel-state", has_text="Maybe").wait_for()
    assert page.locator(".toast[data-key='select-call']").count() == 1                   # one toast per kind of call
    for _ in range(4):
        w.locator(".sel-flag-btn").click()
        page.get_by_role("button", name="Flag for Claude").click()
        page.locator(".popover").wait_for(state="detached")
    assert page.locator(".toast").count() <= 2
    box, row = page.locator(".toasts").bounding_box(), page.locator(".sel-main").bounding_box()
    assert box["x"] >= row["x"] + row["width"] - 2                                        # over the viewer column, not the cards


def test_a_review_copy_that_never_finishes_offers_try_again(browser, room_server):
    pg = browser.new_page(viewport={"width": 1440, "height": 900})
    pg.add_init_script(TEST_FLAG + "window.__aveReviewQuietMs = 1500;")
    stuck = lambda r: r.fulfill(status=202, content_type="application/json", body='{"artifact": "ep", "key": "0", "status": "rendering"}')
    pg.route("**/api/room/review", stuck)
    try:
        pg.goto(room_server.app_url() + "#/room/ep")
        pg.wait_for_function("() => window.__room && window.__room.state.live === 'live'")
        script_page(room_server, pg)
        pg.get_by_text("This recording could not be prepared").wait_for(timeout=10_000)
        assert pg.get_by_text("stopped making progress", exact=False).is_visible()
        pg.unroute("**/api/room/review", stuck)
        pg.locator(".script .viewer").get_by_role("button", name="Try again").click()
        pg.wait_for_function("() => document.querySelector('.script video').readyState >= 1", timeout=30_000 * CI_SLOW)
    finally:
        pg.close()


def test_script_and_selects_reload_when_their_files_change(page, room_server, selects_ep):
    script_page(room_server, page)
    tr_path = selects_ep.ep.work / "transcript.json"
    tr = json.loads(tr_path.read_text())
    tr["words"][13]["w"] = "cheers"
    tr_path.write_text(json.dumps(tr))
    page.locator(".sc-line", has_text="cheers for having me").wait_for(timeout=10_000)
    tr_path.write_text("{")
    page.get_by_text("The transcript could not be read").wait_for(timeout=10_000)
    tr_path.unlink()
    page.get_by_text("No transcript yet").wait_for(timeout=10_000)
    selects_page(room_server, page)
    _clips_edit(selects_ep.ep, lambda d: next(c for c in d["clips"] if c["id"] == "c02").update(title="Systems, renamed"))
    card(page, "Systems, renamed").wait_for(timeout=10_000)
    (selects_ep.ep.work / "clips.json").unlink()
    page.get_by_text("No selects yet").wait_for(timeout=10_000)


def test_a_cue_before_the_lines_exist_lands_once_they_do(browser, room_server, long_ep):
    pg = browser.new_page(viewport={"width": 1440, "height": 900})
    pg.add_init_script(TEST_FLAG)
    try:
        pg.goto(room_server.app_url() + "#/room/long")
        pg.wait_for_function("() => window.__room && window.__room.state.live === 'live'")
        room_server.room.show(artifact="long", view="script", position_s=7000)
        pg.wait_for_function("() => document.querySelectorAll('.sc-line').length === 2000", timeout=20_000)
        pg.wait_for_function("""() => { const l = document.querySelector('.sc-line.is-playing'); if (!l) return false;
            const r = l.getBoundingClientRect(); return r.top > 0 && r.bottom < innerHeight; }""", timeout=10_000)
        assert int(pg.evaluate("document.querySelector('.sc-line.is-playing').dataset.i")) > 1900
    finally:
        pg.close()


def test_a_cut_position_is_never_cued_in_the_script(page, room_server):
    room_server.room.show(artifact="ep", view="cut", position_s=17.5)
    page.get_by_role("tab", name="Script").click()
    page.locator(".sc-line").first.wait_for()
    page.wait_for_function("() => document.querySelector('.sc-line.is-playing')")
    assert page.evaluate("document.querySelector('.sc-line.is-playing').dataset.i") == "0"
    assert room_server.stage.time_base == "cut"


def test_enter_while_typing_goes_to_the_first_match(page, room_server):
    script_page(room_server, page, at=17.5)
    page.wait_for_function("() => document.querySelector('.sc-line.is-playing') && document.querySelector('.sc-line.is-playing').dataset.i === '6'")
    box = page.get_by_role("searchbox", name="Search the transcript")
    box.click()
    page.keyboard.type("that")
    page.keyboard.press("Enter")                                        # inside the debounce
    page.wait_for_function("() => document.querySelector('.sc-count').textContent === '1 of 4'")


def test_the_viewer_follows_a_piecewise_sync_map(page, room_server):
    from ave import editdoc
    script_page(room_server, page)
    src = {"start_offset": 1.0, "drift_ppm": 80.0, "sync_map": [[0.0, 1.0], [10.0, 1.2], [25.0, 1.1]]}
    clock = {"start_offset": 1.0, "k": 1 + 80e-6, "ft": [0.0, 10.0, 25.0], "et": [1.0, 11.2, 26.1]}
    for e in (0.5, 5.0, 11.2, 20.0, 40.0):
        got = page.evaluate("([c, e]) => import('/app/room/script.js').then(m => m.toFileTime(c, e))", [clock, e])
        assert abs(got - editdoc.src_time(src, e)) < 1e-6, e
        back = page.evaluate("([c, m]) => import('/app/room/script.js').then(m2 => m2.toEpisodeTime(c, m))", [clock, got])
        assert abs(back - e) < 1e-6


def test_arrow_keys_move_between_cards_and_a_slow_still_is_retried(page, room_server, selects_ep):
    misses = {"n": 0}

    def once(route):
        if misses["n"] == 0:
            misses["n"] += 1
            return route.fulfill(status=404, body="{}")
        return route.continue_()
    page.route("**/api/room/thumb/ep/c02*", once)
    selects_page(room_server, page)
    page.wait_for_function("() => [...document.querySelectorAll('.sel-thumb img')].every(i => i.complete && i.naturalWidth > 0)", timeout=30_000)
    assert misses["n"] == 1
    opens = page.locator(".sel-open")
    opens.nth(0).focus()
    page.keyboard.press("ArrowRight")
    assert page.evaluate("document.activeElement === document.querySelectorAll('.sel-open')[1]")
    page.keyboard.press("End")
    assert page.evaluate("document.activeElement === [...document.querySelectorAll('.sel-open')].at(-1)")
    page.keyboard.press("Home")
    assert page.evaluate("document.activeElement === document.querySelectorAll('.sel-open')[0]")


def test_a_copy_queued_behind_another_camera_waits_without_timing_out(browser, room_server, monkeypatch):
    from ave.room import review
    monkeypatch.setattr(review, "WAITING_EVERY_S", 0.4)
    other = object()                                                    # another camera's copy, encoding first
    with review._QUEUE_CV:
        review._QUEUE.append(other)
    pg = browser.new_page(viewport={"width": 1440, "height": 900})
    pg.add_init_script(TEST_FLAG + "window.__aveReviewQuietMs = 1500;")   # 90 s, shortened: the wait below is twice this
    try:
        pg.goto(room_server.app_url() + "#/room/ep")
        pg.wait_for_function("() => window.__room && window.__room.state.live === 'live'")
        script_page(room_server, pg)
        pg.get_by_text("Waiting for another camera to finish: 1 copy ahead of this one.").wait_for(timeout=10_000)
        pg.wait_for_timeout(3500)
        assert not pg.get_by_text("This recording could not be prepared").count()
        assert pg.get_by_text("Waiting for another camera to finish").is_visible()
    finally:
        with review._QUEUE_CV:
            review._QUEUE.remove(other)
            review._QUEUE_CV.notify_all()
    try:
        pg.wait_for_function("() => document.querySelector('.script video').readyState >= 1", timeout=30_000)
        assert not pg.get_by_text("This recording could not be prepared").count()
    finally:
        pg.close()


# ---------------------------------------------------------------- Layout: a post composed beside a phone preview (Task 21)
from datetime import datetime, timezone                                 # noqa: E402

from ave.studio import store                                            # noqa: E402
from ave.studio.events import AGENT                                     # noqa: E402
from ave.studio.model import new_post, new_variant                      # noqa: E402
from studio_helpers import add_asset, make_png, write_account           # noqa: E402

NOW = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)
IMPORT_STUB = """window.__imported = 0;
Object.defineProperty(window, 'aveNative', { value: Object.freeze({ importLibrary: async () => {
  window.__imported += 1; return { added: (window.__importRows || []).map((id) => ({ name: id + '.jpg', asset: { id }, duplicate: false })) }; } }) });"""


@pytest.fixture
def post_studio(room_server):
    """One Instagram post with a photo, and a second photo in the library for the import (the native picker is stubbed)."""
    sp = room_server.app.studio.sp
    write_account(sp, "ig-main", "instagram")
    one = add_asset(sp, make_png(sp.work / "gen" / "one.jpg", color=(196, 132, 74)), alt="A sourdough loaf on a wooden board.")
    two = add_asset(sp, make_png(sp.work / "gen" / "two.jpg", color=(54, 92, 150)), alt="The crumb, cut open.")
    post = new_post(idea="Sourdough day", post_id="p_layout", now=NOW, copy={"base": "Day one: the starter smells like pears."},
                    media=[{"slot": 0, "asset": one["id"], "role": "image", "alt": one["alt"]}])
    post["variants"].append(new_variant(account="ig-main", platform="instagram", post_type="feed", variant_id="v_ig"))
    saved = store.save_post(sp, post, expected_rev=None, actor=AGENT, op="create", now=NOW)
    return SimpleNamespace(sp=sp, id=saved["id"], one=one, two=two)


def layout_page(srv, page, artifact):
    srv.room.show(artifact=artifact, view="layout")
    page.locator(".lay-screen .pv").wait_for()                       # the skeleton screen is painted first
    page.locator(".lay-input").first.wait_for()


def claude_edits(sp, post_id, patch):
    """Claude's edit, made the way its tools make one: another process writes the post file, as the agent."""
    studio = open_studio(sp.root)
    rev = studio.post(post_id)["rev"]
    return studio.edit_post(post_id, patch, expected_rev=rev, actor=AGENT)


def caption(page):
    return page.locator('.lay-input[data-key="caption"]')


def test_a_post_opens_in_layout_with_a_phone_preview(page, room_server, post_studio):
    layout_page(room_server, page, post_studio.id)
    assert page.get_by_role("heading", name="Sourdough day").is_visible()
    tabs = page.locator(".room-tabs [role=tab]:visible")
    assert tabs.all_inner_texts() == ["Layout"]                      # a post has no Cut, Script or Selects
    assert page.get_by_role("tab", name="Layout").get_attribute("aria-selected") == "true"
    assert page.get_by_role("tab", name="@ig-main Instagram feed").get_attribute("aria-selected") == "true"
    phone = page.locator(".lay-phone")
    assert phone.is_visible() and phone.get_attribute("aria-label") == "Phone preview"
    assert "Day one: the starter smells like pears." in page.locator(".lay-screen .pv").inner_text()
    assert page.locator(".lay-phone-title").inner_text() == "Instagram post"
    assert caption(page).input_value() == "Day one: the starter smells like pears."
    assert post_studio.one["id"] in page.locator(".lay-canvas img").get_attribute("src")
    page.wait_for_function("() => document.querySelector('.lay-canvas img').naturalWidth > 0")
    names = page.evaluate("() => [...document.querySelectorAll('.room button')].map(b => (b.getAttribute('aria-label') || b.textContent).trim())")
    assert not [n for n in names if any(w in n.lower() for w in ("approve", "publish", "send", "schedule", "queue"))], names
    assert page.locator(".layout input[type=file]").count() == 0     # the app's web view returns nothing from a file input
    assert room_server.room.read()["stage"]["view"] == "layout"


def test_typing_in_layout_saves_and_the_phone_follows(page, room_server, post_studio):
    layout_page(room_server, page, post_studio.id)
    caption(page).fill("Day two: the loaf sings when it cools.")
    page.wait_for_function("() => document.querySelector('.lay-screen .pv').innerText.includes('the loaf sings')")
    page.locator(".lay-save", has_text="Saved").wait_for()
    post = store.load_post(post_studio.sp, post_studio.id)
    assert post["variants"][0]["copy"]["caption"] == "Day two: the loaf sings when it cools."
    assert post["rev"] == 2


def test_claudes_edit_appears_live(page, room_server, post_studio):
    layout_page(room_server, page, post_studio.id)
    page.evaluate("window.__sameDocument = true")
    hashtags = page.locator('.lay-input[data-key="hashtags"]')
    hashtags.fill("sourdough")                                       # the director's own edit, saved before Claude's arrives
    page.locator(".lay-save", has_text="Saved").wait_for()
    claude_edits(post_studio.sp, post_studio.id, {"variants.v_ig.copy.caption": "Claude: the crust crackles as it cools."})
    page.wait_for_function("() => document.querySelector('.lay-input[data-key=caption]').value.startsWith('Claude: the crust')", timeout=10_000)
    assert page.locator('.lay-field[data-key="caption"] .lay-changed-text').inner_text() == "Claude changed this"
    assert page.locator('.lay-field[data-key="caption"] .lay-avatar').is_visible()          # with Claude's avatar
    assert page.locator(".lay-field.is-changed").count() == 0      # Layout's own highlight, never the board's cell ring
    assert "the crust crackles" in page.locator(".lay-screen .pv").inner_text()
    assert page.locator(".lay-save").inner_text() == "Claude changed the caption"
    assert hashtags.input_value() == "sourdough"                     # what the director saved stays
    assert page.evaluate("window.__sameDocument") is True            # followed the stream, no reload


def test_a_draft_claude_edits_through_its_tools_follows_live(page, room_server, post_studio):
    from ave.agent import AgentApp
    agent = AgentApp(open_studio(post_studio.sp.root), by="claude")
    draft = agent.call("create_draft", {"request_id": "layout-1", "idea": "Starter care", "copy": {"base": "Feed it twice a day."}})["post"]
    layout_page(room_server, page, draft["id"])
    assert page.locator(".lay-phone-title").inner_text() == "Instagram post · no channel yet"
    assert page.locator(".lay-nochannel").inner_text() == "No channel yet"
    agent.call("edit_draft", {"post_id": draft["id"], "expected_rev": draft["rev"], "fields": {"base": "Feed it twice a day, at nine and nine."}})
    page.wait_for_function("() => document.querySelector('.lay-screen .pv').innerText.includes('at nine and nine')", timeout=10_000)
    assert caption(page).input_value() == "Feed it twice a day, at nine and nine."
    assert page.locator(".lay-changed-text").first.inner_text() == "Claude changed this"


def test_media_imports_through_ave_native(browser, room_server, post_studio):
    pg = browser.new_page(viewport={"width": 1440, "height": 900})
    pg.add_init_script(TEST_FLAG + IMPORT_STUB + f"window.__importRows = {json.dumps([post_studio.two['id']])};")
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(room_server.app_url() + "#/room/" + post_studio.id)
    pg.wait_for_function("() => window.__room && window.__room.state.live === 'live'")
    layout_page(room_server, pg, post_studio.id)
    pg.locator(".lay-media").get_by_role("button", name="Import media").click()
    pg.locator(".lay-thumb").nth(1).wait_for()
    assert pg.evaluate("window.__imported") == 1
    assert pg.locator(".lay-slide-label").inner_text() == "Slide 2 of 2"
    post = store.load_post(post_studio.sp, post_studio.id)
    assert [m["asset"] for m in post["media"]] == [post_studio.one["id"], post_studio.two["id"]]
    pg.locator(".toast", has_text="Added 1 file to the post").wait_for()
    pg.get_by_role("button", name="Move earlier").click()            # the editor's own media operations, from the Room
    pg.wait_for_function(f"() => document.querySelector('.lay-thumb img').src.includes('{post_studio.two['id']}')")
    assert [m["asset"] for m in sorted(store.load_post(post_studio.sp, post_studio.id)["media"], key=lambda m: m["slot"])] == \
        [post_studio.two["id"], post_studio.one["id"]]
    assert pg.locator(".layout input[type=file]").count() == 0
    pg.close()
    assert not errors


def test_a_board_post_opens_its_studio_post(page, room_server, post_studio):
    from ave.agent import AgentApp
    Project.create(post_studio.sp.root, "Bakery", person="Maren")
    agent = AgentApp(open_studio(post_studio.sp.root), by="claude")
    got = agent.call("artifact_create", {"request_id": "post-art", "kind": "post", "title": "Sourdough day", "fields": {"post": post_studio.id}})
    layout_page(room_server, page, got["artifact"])
    assert caption(page).input_value() == "Day one: the starter smells like pears."


HOLD = "window.__aveAutosaveMs = 600000;"       # the autosave waits ten minutes, so a test decides when the director's edit is saved


@pytest.fixture
def held(browser, room_server):
    """A Room page whose Layout autosave is held: what the director types stays unsaved until the field is left."""
    pg = browser.new_page(viewport={"width": 1440, "height": 900})
    pg.add_init_script(TEST_FLAG + HOLD)
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(room_server.app_url() + "#/room/ep")
    pg.wait_for_function("() => window.__room && window.__room.state.live === 'live'")
    yield pg
    pg.close()
    assert not errors


def type_unsaved(page, key, text):
    page.evaluate(f"""() => {{ const t = document.querySelector('.lay-input[data-key={key}]'); t.focus(); t.value = {json.dumps(text)};
        t.dispatchEvent(new Event('input', {{ bubbles: true }})); }}""")


def caption_on_disk(post_studio):
    return store.load_post(post_studio.sp, post_studio.id)["variants"][0]["copy"]


def test_claudes_edit_of_another_field_keeps_what_the_director_is_typing(held, room_server, post_studio):
    page = held
    layout_page(room_server, page, post_studio.id)
    type_unsaved(page, "first_comment", "Recipe in the link")
    claude_edits(post_studio.sp, post_studio.id, {"variants.v_ig.copy.caption": "Claude: a better opening line."})
    page.wait_for_function("() => document.querySelector('.lay-input[data-key=caption]').value.startsWith('Claude:')", timeout=10_000)
    assert page.locator('.lay-input[data-key="first_comment"]').input_value() == "Recipe in the link"   # rebased, not replaced
    assert caption_on_disk(post_studio)["first_comment"] is None     # still unsaved: the merge path really ran
    assert not page.get_by_role("button", name="Keep mine").is_visible()
    page.locator('.lay-input[data-key="first_comment"]').blur()      # leaving the field saves it
    page.locator(".lay-save", has_text="Saved").wait_for()
    v = caption_on_disk(post_studio)
    assert v["caption"] == "Claude: a better opening line." and v["first_comment"] == "Recipe in the link"


@pytest.mark.parametrize("choice", ["Keep mine", "Use Claude’s"])
def test_both_changing_the_caption_asks_which_to_keep(held, room_server, post_studio, choice):
    page = held
    layout_page(room_server, page, post_studio.id)
    type_unsaved(page, "caption", "Mine: the starter is ready.")
    claude_edits(post_studio.sp, post_studio.id, {"variants.v_ig.copy.caption": "Claude: the starter doubled overnight."})
    page.get_by_text("Claude changed the caption while you were typing").wait_for(timeout=10_000)
    assert "Claude: the starter doubled overnight." in page.locator(".lay-notice").inner_text()
    assert caption(page).input_value() == "Mine: the starter is ready."               # nothing typed is replaced
    page.get_by_role("button", name=choice).click()
    want = "Mine: the starter is ready." if choice == "Keep mine" else "Claude: the starter doubled overnight."
    page.locator(".lay-notice").wait_for(state="hidden")
    page.wait_for_function(f"() => document.querySelector('.lay-input[data-key=caption]').value === {json.dumps(want)}")
    deadline = time.time() + 10
    while caption_on_disk(post_studio)["caption"] != want and time.time() < deadline:
        time.sleep(0.1)
    assert caption_on_disk(post_studio)["caption"] == want


def test_a_refused_save_rebases_and_saves_again(held, room_server, post_studio):
    page = held
    layout_page(room_server, page, post_studio.id)
    page.route(f"**/api/studio/posts/{post_studio.id}", lambda r: r.abort())   # the page never reads Claude's edit before it saves
    type_unsaved(page, "caption", "Mine: crust first.")
    claude_edits(post_studio.sp, post_studio.id, {"variants.v_ig.copy.hashtags": ["bread"]})
    time.sleep(1.5)                                                  # the event arrives, and its read is refused
    edits = []
    page.on("response", lambda r: edits.append(r.status) if r.url.endswith("/edit") else None)
    caption(page).blur()
    page.locator(".lay-save", has_text="Saved").wait_for(timeout=10_000)
    assert edits[:2] == [409, 200]                                    # refused against the old revision, then saved on Claude's
    v = caption_on_disk(post_studio)
    assert v["caption"] == "Mine: crust first." and v["hashtags"] == ["bread"]
    assert page.locator('.lay-input[data-key="hashtags"]').input_value() == "bread"


def test_a_stage_change_during_a_conflict_keeps_the_directors_text(held, room_server, post_studio):
    page = held
    layout_page(room_server, page, post_studio.id)
    type_unsaved(page, "caption", "MINE typed")
    claude_edits(post_studio.sp, post_studio.id, {"variants.v_ig.copy.caption": "Claude caption"})
    page.get_by_role("button", name="Keep mine").wait_for(timeout=10_000)
    room_server.room.show(artifact="ep", view="cut")                 # Claude moves the stage before the director answers
    toast = page.locator(".toast", has_text="Kept your caption. Claude’s version was replaced.")
    toast.wait_for(timeout=10_000)
    assert caption_on_disk(post_studio)["caption"] == "MINE typed"
    toast.get_by_role("button", name="Use Claude’s").click()          # and Claude's version is one click away
    page.locator(".toast", has_text="Back to Claude’s caption").wait_for()
    assert caption_on_disk(post_studio)["caption"] == "Claude caption"


def test_unsaved_text_is_saved_when_the_stage_moves_on(held, room_server, post_studio):
    page = held
    layout_page(room_server, page, post_studio.id)
    type_unsaved(page, "hashtags", "crumb")
    page.locator(".lay-alt").fill("The loaf, still warm.")
    room_server.room.show(artifact="ep", view="cut")
    deadline = time.time() + 10
    while time.time() < deadline and caption_on_disk(post_studio).get("hashtags") != ["crumb"]:
        time.sleep(0.1)
    post = store.load_post(post_studio.sp, post_studio.id)
    assert post["variants"][0]["copy"]["hashtags"] == ["crumb"]
    assert post["media"][0]["alt"] == "The loaf, still warm."          # alt text too, though it saves through the media operation


def test_a_board_post_shown_in_the_cut_view_still_follows_live(page, room_server, post_studio):
    from ave.agent import AgentApp
    Project.create(post_studio.sp.root, "Bakery", person="Maren")
    agent = AgentApp(open_studio(post_studio.sp.root), by="claude")
    got = agent.call("artifact_create", {"request_id": "post-art", "kind": "post", "title": "Sourdough day", "fields": {"post": post_studio.id}})
    room_server.room.show(artifact=got["artifact"], view="cut")      # the view Claude uses for everything else
    page.locator(".lay-screen .pv").wait_for(timeout=15_000)
    page.locator(".lay-input").first.wait_for()
    deadline = time.time() + 5
    while room_server.stage.view != "layout" and time.time() < deadline:
        time.sleep(0.05)
    assert room_server.room.read()["stage"]["view"] == "layout"      # the Room told the app which view the director sees
    claude_edits(post_studio.sp, post_studio.id, {"variants.v_ig.copy.caption": "Claude: live?"})
    page.wait_for_function("() => document.querySelector('.lay-input[data-key=caption]').value.startsWith('Claude: live')", timeout=10_000)


def test_a_post_staged_before_the_view_is_known_follows_live(page, room_server, post_studio):
    """The server follows the post whatever view the stage carries: here the stage still says cut while the edit lands."""
    room_server.room.show(artifact=post_studio.id, view="cut")
    room_server._post_seen.clear()
    seq = room_server.bus.latest
    claude_edits(post_studio.sp, post_studio.id, {"variants.v_ig.copy.caption": "Claude: any view."})
    deadline = time.time() + 5
    while time.time() < deadline and not any(e["type"] == "post" for e in room_server.bus.since(seq)):
        time.sleep(0.1)
    assert any(e["type"] == "post" and e["data"]["post"] == post_studio.id for e in room_server.bus.since(seq))


def test_the_post_poll_stops_when_the_director_leaves_the_room(page, room_server, post_studio):
    layout_page(room_server, page, post_studio.id)
    page.evaluate("location.hash = '#/workspace'")
    deadline = time.time() + 5
    while room_server.room_followers and time.time() < deadline:
        time.sleep(0.05)
    assert room_server.room_followers == 0
    seq = room_server.bus.latest
    claude_edits(post_studio.sp, post_studio.id, {"variants.v_ig.copy.caption": "Claude: nobody is looking."})
    time.sleep(2.5)
    assert not [e for e in room_server.bus.since(seq) if e["type"] == "post"]


def test_a_project_saved_by_a_newer_ave_is_read_only_in_the_room(page, room_server, monkeypatch):
    import ave
    from ave.project import Project
    show_v8(room_server, page)
    with monkeypatch.context() as m:                          # a teammate on a newer AVE made this a project and wrote to it
        m.setattr(ave, "__version__", "99.0.0")
        m.setattr(__import__("sys"), "frozen", True, raising=False)      # a release build stamps its version
        Project.create(room_server.project_root / "ep", "Episode", person="Maren")
    offer_v9(room_server, page)
    page.reload()
    page.wait_for_function("() => window.__room && window.__room.state.live === 'live'")
    banner = page.locator(".room .notice-update")
    banner.wait_for()
    assert "Update needed" in banner.inner_text()
    assert "This project was saved by a newer AVE (99.0.0). Update to edit it." in banner.inner_text()
    assert page.locator(".room.is-readonly").count() == 1
    keep = page.get_by_role("button", name="Keep this")
    keep.wait_for()
    for name in ("Keep this", "Go back", "Flag this moment"):
        assert page.get_by_role("button", name=name).get_attribute("aria-disabled") == "true", name
    assert page.get_by_role("button", name="Play").get_attribute("aria-disabled") is None     # watching still works
    keep.click(force=True)
    page.keyboard.press("y")
    page.wait_for_timeout(400)
    assert room_server.room.snapshot()["offer"] is not None and not room_server.room._clicks     # nothing was kept
    got = page.evaluate("""async () => { const r = await fetch('/api/room/keep', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({})}); return [r.status, (await r.json()).error]; }""")
    assert got[0] == 409 and "newer AVE" in got[1]                                            # and the server refuses it too
    page.get_by_role("tab", name="Script").click()
    line_flag = page.locator('[aria-label="Flag this line for Claude"]').first
    line_flag.wait_for(state="attached")
    assert line_flag.get_attribute("aria-disabled") == "true"
    assert page.locator(".room .notice-update").is_visible()
