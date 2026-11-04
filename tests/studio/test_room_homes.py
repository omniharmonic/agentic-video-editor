"""Task 24a: the retired clip desk's nudge and trim clear, now in the Room."""

from __future__ import annotations

import json

from ave import clips as clip_finder

from .test_room_browser import (browser, card, no_agent_markers, page, room_server, selects_ep, selects_page, show_v8)  # noqa: F401  (fixtures)


def test_a_select_moves_its_end_by_a_sentence_and_a_bad_move_says_why(page, room_server, selects_ep):
    data = clip_finder.load_clips(selects_ep.ep)
    data["clips"] = [c for c in data["clips"] if c["id"] != "c03"]          # room after "Systems that carry you" to move its end
    (selects_ep.ep.work / "clips.json").write_text(json.dumps(data))
    selects_page(room_server, page)
    card(page, "A warm welcome").locator(".sel-open").click()
    page.locator(".sel-detail .sel-nudge").get_by_role("button", name="End a sentence earlier").click()
    page.get_by_text("That move does not work: the select would be 11.7 s long, and selects run 12 to 90 s.").wait_for()
    assert next(c for c in clip_finder.load_clips(selects_ep.ep)["clips"] if c["id"] == "c01")["status"] == "approved"   # unchanged
    before = next(c for c in clip_finder.load_clips(selects_ep.ep)["clips"] if c["id"] == "c02")
    card(page, "Systems that carry you").locator(".sel-open").click()
    page.locator(".sel-detail .sel-nudge").get_by_role("button", name="End a sentence later").click()
    page.get_by_text("now ends a sentence later").wait_for()
    after = next(c for c in clip_finder.load_clips(selects_ep.ep)["clips"] if c["id"] == "c02")
    assert after["end"] > before["end"] and after["start"] == before["start"] and after["status"] == "proposed"
    ev = [e for e in selects_ep.project.read_events() if e["op"] == "clip.decided"][-1]
    assert ev["actor"] == "human" and ev["data"] == {"clip": "c02", "choice": "nudge", "moves": {"end": 1}, "title": "Systems that carry you"}


def test_the_strip_shows_the_trim_and_clearing_it_goes_through_the_app(browser, room_server, selects_ep):
    meta = json.loads(selects_ep.ep.meta_path.read_text())
    meta["trim"] = {"s2": {"in": 1.5, "out": 20.0, "by": "human", "at": "2026-11-02T10:00:00+0000"}}
    selects_ep.ep.meta_path.write_text(json.dumps(meta))
    calls = []
    pg = browser.new_page(viewport={"width": 1440, "height": 900})
    pg.expose_function("__clearTrim", lambda project, session: calls.append((project, session)) or {"ok": True})
    pg.add_init_script("window.__AVE_TEST__ = true; window.aveNative = { clearTrim: (p, s) => window.__clearTrim(p, s) };")
    pg.goto(room_server.app_url() + "#/room/ep")
    pg.wait_for_function("() => window.__room && window.__room.state.live === 'live'")
    show_v8(room_server, pg)
    chip = pg.locator(".strip-trim")
    chip.wait_for()
    assert chip.inner_text() == "Trim 0:01.5 to 0:20.0"
    chip.click()
    pg.get_by_role("button", name="Clear the trim").click()
    pg.get_by_text("Trim cleared").wait_for()
    assert calls == [(selects_ep.project.project_id, "s2")]
    pg.close()


def test_review_next_reads_the_rooms_open_flags_then_falls_back_to_the_notes_file(room_server, capsys):
    from ave import cli, review
    from ave.episode import Episode
    ep = Episode(room_server.project_root / "ep")
    review.add_note(ep, edit="rough.s2.v8", t0=3.0, t1=4.0, text="older note from the file")
    room_server.room.add_flag({"artifact": "ep", "start": 12.0, "end": 14.5, "note": "Cut the cough", "view": "cut"})
    cli.main(["review", "next", str(ep.root)])
    out = json.loads(capsys.readouterr().out)
    assert out["source"] == "room" and out["note"]["text"] == "Cut the cough" and out["note"]["t0"] == 12.0 and out["untrusted_text"]
    assert room_server.room.snapshot()["flags_waiting"] == 1                       # reading here acknowledges nothing
    room_server.room.read(room_server.room.read()["ack"])                         # Claude read it in the Room
    cli.main(["review", "next", str(ep.root)])
    out = json.loads(capsys.readouterr().out)
    assert out["source"] == "notes" and out["note"]["text"] == "older note from the file"


def test_a_selects_call_takes_reasons_and_a_note_where_taste_learning_reads_them(page, room_server, selects_ep):
    from ave import learning
    selects_page(room_server, page)
    c = card(page, "Systems that carry you")
    c.locator(".sel-open").click()
    c.get_by_role("button", name="Skip").click()
    c.locator(".sel-state", has_text="Skipped").wait_for()
    why = page.locator(".sel-detail .sel-why-form")
    why.get_by_role("button", name="Hook comes too late").click()
    assert why.get_by_role("button", name="Great but long").count() == 0                  # a maybe-only reason is not offered for a skip
    why.get_by_role("textbox", name="Note about this call").fill("The point lands after 20 seconds")
    why.get_by_role("button", name="Save why").click()
    page.get_by_text("Claude learns from it").wait_for()
    rec = learning.latest_decisions(selects_ep.ep)["c02"]                                    # what `ave learn taste` reads
    assert rec["verdict"] == "skip" and rec["reasons"] == ["hook_late"] and rec["note"] == "The point lands after 20 seconds"
    assert rec["source"] == "ui"
    from ave.cli import main
    import io, contextlib
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        main(["learn", "taste", "report", str(selects_ep.ep.root)])
    assert "hook_late" in out.getvalue() or "Hook comes too late" in out.getvalue()
    page.reload()                                                                            # it is shown again after a reload
    selects_page(room_server, page)
    card(page, "Systems that carry you").locator(".sel-open").click()
    assert page.locator('.sel-reason[data-reason="hook_late"]').get_attribute("aria-pressed") == "true"
