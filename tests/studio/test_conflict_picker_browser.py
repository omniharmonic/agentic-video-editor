"""Two copies of a settings file, answered in a real browser: the per-setting picker on the board's Waiting on you.

An episode folder inside a studio has episode.json and a Syncthing conflict copy that disagree on three settings. The
picker shows one row per setting with both files' full values; Apply stays disabled until every row is picked, and the
answer written to the log, and then to the file, is exactly the picks.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from ave.project import Project
from ave.studio.project import StudioProject
from ave.studio.service import open_studio
from ave.studio.ui import workspace as ws_mod
from ave.studio.ui.server import StudioServer
from ave.syncthing import conflicts
from ave.syncthing.members import add_member, canonical

LONG = "Episode 13: who keeps the van when the long road finally ends for both of them"


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


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(ws_mod, "WATCH_S", 0.3)
    sp = StudioProject(tmp_path / "studio")
    sp.create(title="The Long Table", tz="UTC")
    root = sp.root / "ep13"
    root.mkdir()
    (root / "episode.json").write_text(json.dumps({"title": "Episode 13", "tz": "UTC", "draft": True}, indent=2) + "\n")
    p = Project.create(root, "Episode 13", person="maren")
    meta = add_member(p.meta, "maren", p.device.id, "full", "-".join(["AAAAAAA"] * 8), "a")
    meta = add_member(meta, "tomas", "dev_B", "proxies", "-".join(["BBBBBBB"] * 8), "b")
    (root / ".ave" / "project.json").write_bytes(canonical(meta))
    p = Project.open(root)
    p.log.append("artifact.created", artifact="art_ep", data={"kind": "episode", "title": "Episode 13", "stage": "Plan"})
    copy = root / "episode.sync-conflict-20261102-101500-BBBBBBB.json"
    copy.write_text(json.dumps({"title": LONG, "tz": "America/Denver"}, indent=2) + "\n")
    (asked,) = conflicts.check(root).asked
    srv = StudioServer(open_studio(sp.root), pin="424242")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield p, copy, asked, srv
    srv.shutdown()
    t.join(5)
    srv.server_close()


def test_the_picker_applies_exactly_what_was_picked(browser, setup):
    p, copy, asked, srv = setup
    errors = []
    pg = browser.new_page(viewport={"width": 1440, "height": 900})
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(srv.app_url() + "#/workspace")
    key = f"{p.project_id}:art_ep"
    waiting = pg.locator(f'.ws-row[data-key="{key}"] [data-col=waiting]')
    waiting.filter(has_text="Waiting on you").wait_for()
    waiting.click()
    pg.locator(".pick-row").first.wait_for()
    assert pg.locator(".pick-key").all_inner_texts() == ["Title", "Time zone", "Draft"]
    assert LONG in pg.locator(".pick-row").nth(0).inner_text()                      # the full value, not a tooltip
    assert pg.locator(".pick-row").nth(2).locator(".pick").nth(1).inner_text().endswith("(not set)")
    apply = pg.get_by_role("button", name="Apply my choices")
    assert apply.is_disabled()
    picks = {"title": 1, "tz": 0, "draft": 1}                                     # copy, file, copy (the copy has no draft)
    for i, (k, side) in enumerate(picks.items()):
        assert apply.is_disabled()
        pg.locator(".pick-row").nth(i).locator(".pick").nth(side).click()
        assert pg.locator(".pick-row").nth(i).locator('.pick[aria-pressed="true"]').count() == 1
    assert apply.is_enabled()
    apply.click()
    end = time.time() + 5
    ev = None
    while time.time() < end and ev is None:
        ev = next((e for e in Project.open(p.root).read_events() if e["op"] == "decision.resolved"), None)
        time.sleep(0.05)
    # a browser holds only the room token, so the answer is Claude's, relayed, for the director to confirm or undo (final review
    # I1); in the app it goes through the bridge and the device-owner check (tests/studio/test_attribution.py)
    assert ev["actor"] == "claude" and ev["data"] == {"decision": asked["decision"], "choice": conflicts.PER_KEY, "relayed_by": "claude",
                                                      "picks": {"title": "copy", "tz": "file", "draft": "copy"}}
    assert conflicts.drain(10)
    end = time.time() + 5
    while time.time() < end and copy.exists():
        time.sleep(0.05)
    assert json.loads((p.root / "episode.json").read_text()) == {"title": LONG, "tz": "UTC"}
    assert not copy.exists() and not errors
    pg.close()


def test_three_members_read_the_records_own_labels(browser, setup):
    """K: in a three-person project the file's writer is not known, so the record says "This computer's copy"; the
    picker shows exactly the record's words, never "This file's file" or "Use all of This file's"."""
    p, copy, _, srv = setup
    root = p.root
    meta = add_member(json.loads((root / ".ave" / "project.json").read_text()), "eve", "dev_E", "full", "-".join(["EEEEEEE"] * 8), "e")
    (root / ".ave" / "project.json").write_bytes(canonical(meta))
    copy.unlink()
    conflicts.check(root)                                                          # the first question closes
    copy2 = root / "episode.sync-conflict-20261102-111500-BBBBBBB.json"
    copy2.write_text(json.dumps({"title": "Van stories", "tz": "UTC", "draft": True}, indent=2) + "\n")
    (asked,) = conflicts.check(root).asked
    errors = []
    pg = browser.new_page(viewport={"width": 1024, "height": 900})
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(srv.app_url() + "#/workspace")
    waiting = pg.locator(f'.ws-row[data-key="{p.project_id}:art_ep"] [data-col=waiting]')
    waiting.filter(has_text="Waiting on you").wait_for()
    waiting.click()
    pg.locator(".pick-row").first.wait_for()
    assert pg.locator(".pick-key").all_inner_texts() == ["Title"]
    assert pg.locator(".pick .pick-who").all_inner_texts() == ["This computer's copy", "Tomas's file"]
    assert pg.get_by_role("button", name="Use all of this computer's copy").count() == 1
    assert pg.get_by_role("button", name="Use all of Tomas's").count() == 1
    text = pg.locator(".pop-list").inner_text()
    assert "This file" not in text and "'s file's" not in text and "copy's file" not in text
    assert "episode.json was changed on two computers at once (Title)" in text
    assert pg.evaluate("document.documentElement.scrollWidth") <= 1024 and not errors
    pg.get_by_role("button", name="Use all of Tomas's").click()
    end = time.time() + 5
    while time.time() < end and copy2.exists():
        conflicts.drain(1)
        time.sleep(0.05)
    assert json.loads((root / "episode.json").read_text())["title"] == "Van stories" and not copy2.exists()
    pg.close()
