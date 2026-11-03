"""The whole studio on fakes (spec 11 WS7 "done when"): blog post -> quote cards -> posts -> sync -> approval -> tick -> posted exactly once.

Every step before the approval goes through the real CLI (`ave.cli.main`), so the commands, the content renderer (headless Chromium), the library,
the Instagram JPEG conversion, the queue and the gates are all the real code. What is faked: the clock (FakeClock), the platform (RecordingPublisher,
no HTTP), the keychain (MemKeyring). The approval is `ave.publish.queue.approve` called directly (the human act; there is no unattended switch).
"""

import json

import pytest
from PIL import Image
from content_helpers import BROWSERS
from sched_helpers import no_agent_markers  # noqa: F401
from studio_helpers import FakeClock, RecordingPublisher

from ave import cli, humanpresence
from ave.platforms import attest
from ave.publish import queue as pq
from ave.studio import ledger, state, store
from ave.studio.cli import OPEN_KW

BLOG = """# Sourdough starter, day by day

A starter is just flour and water that you feed every day. On the third day it smells like pears, and that is normal.

Do not throw it away on day four when it goes quiet. The yeast is changing shifts, and the quiet is part of the work.

By day seven the jar doubles in four hours. That is when it is ready to bake with.

Feed it at the same time every day, and write the time on the lid.
"""


def run(capsys, *argv):
    rc = cli.main(["studio", *argv])
    cap = capsys.readouterr()
    return rc, (json.loads(cap.out) if cap.out.strip().startswith(("{", "[")) else cap.out), cap.err


@pytest.fixture
def rig(tmp_path, monkeypatch, capsys):
    clock = FakeClock("2026-10-19T16:00:00Z")
    pub = RecordingPublisher("instagram")
    monkeypatch.setitem(OPEN_KW, "clock", clock)
    monkeypatch.setitem(OPEN_KW, "publishers", {"ig-main": pub})
    if BROWSERS:
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", BROWSERS)         # the suite repoints HOME; Chromium lives under the real one
    monkeypatch.setenv("AVE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AVE_WORKSPACE", str(tmp_path / "ws"))
    studio = str(tmp_path / "ws" / "studio")
    assert run(capsys, "init", studio, "--name", "Acme Bakery", "--tz", "America/Denver")[0] == 0
    assert run(capsys, "account", "add", "ig-main", "--platform", "instagram", "--label", "@acmebakery", "--post-types", "feed",
               "--public", "ig_user_id=17841400000000000", "--studio", studio)[0] == 0
    blog = tmp_path / "starter.md"
    blog.write_text(BLOG)
    return clock, pub, studio, blog, capsys


def test_blog_to_one_post_across_two_ticks(rig, monkeypatch):
    clock, pub, studio, blog, capsys = rig

    # 1. ingest the writing: passages are addressable and nothing is fetched
    rc, res, _ = run(capsys, "ingest", "blog", str(blog), "--studio", studio)
    assert rc == 0 and [p["id"] for p in res["passages"]] == ["p001", "p002", "p003", "p004"]

    # 2. the agent picks passages: one post per quote, origin recorded
    posts = []
    for passage, copy in (("p002", "Day 4 goes quiet. The quiet is part of the work."), ("p003", "By day seven the jar doubles in four hours.")):
        rc, res, _ = run(capsys, "post", "new", "--from-blog", "starter.md", "--passages", passage, "--copy", copy, "--reason", "reads well alone",
                         "--confidence", "0.8", "--studio", studio)
        assert rc == 0
        posts.append(res["post"]["id"])

    # 3. render the cards (the real renderer); a quote that is not in the file would be refused, so the words are exactly the passage
    rc, res, _ = run(capsys, "render", "--all", "--style", "paper", "--format", "4x5", "--studio", studio)
    if "Could not start Chromium" in json.dumps(res):
        pytest.skip("Chromium is not installed (the content renderer needs it)")
    assert rc == 0 and [r["attached"] for r in res["rendered"]] == [True, True], res
    for pid in posts:
        post = store.load_post(__import__("ave.studio.project", fromlist=["StudioProject"]).StudioProject(studio), pid)
        assert len(post["media"]) == 1 and post["media"][0]["alt"].startswith("Quote card")

    # 4. variants, one Instagram variant per post, and one slot rule
    for pid in posts:
        rc, res, _ = run(capsys, "post", "variant", "add", pid, "--account", "ig-main", "--type", "feed", "--studio", studio)
        assert rc == 0
    rc, res, _ = run(capsys, "post", "critique", posts[0], "--studio", studio)
    assert rc == 0, res
    assert run(capsys, "schedule", "rules", "set", "ig-main", "--rule", "id=r1;dow=mon,wed,fri;time=09:00;post_types=feed", "--studio", studio)[0] == 0

    # 5. sync: the PNG master becomes the JPEG Instagram takes, and the queue item binds THAT file
    rc, res, _ = run(capsys, "queue", "sync", "--studio", studio)
    assert rc == 0 and all(r["queue_status"] == "awaiting_approval" for rep in res["reports"] for r in rep["variants"])
    assert all(rep["converted"] for rep in res["reports"])
    from ave.studio.project import StudioProject
    sp = StudioProject(studio)
    item = state.load_queue_item(sp, store.load_post(sp, posts[0])["variants"][0])
    jpg = sp.root / item["files"][0]
    assert jpg.suffix == ".jpg" and Image.open(jpg).format == "JPEG"

    # 6. times: the first post goes to the next Monday 09:00 (Denver), the second is left unscheduled
    rc, res, _ = run(capsys, "move", posts[0], store.load_post(sp, posts[0])["variants"][0]["id"], "--to", "2026-10-26T09:00", "--studio", studio)
    assert rc == 0 and res["schedule"]["utc"] == "2026-10-26T15:00:00Z"

    # 7. the HUMAN approves (test-only: the library function, exactly as a human's typed hash would call it)
    rc, res, err = run(capsys, "approve", posts[0], store.load_post(sp, posts[0])["variants"][0]["id"], "--studio", studio)
    assert rc == 2 and res["needs_human"] and res["run"].startswith("ave studio approve ")      # an agent cannot
    pq.approve(sp, item["id"], "maren", item["sha256"][:12], publishers={"instagram": pub})
    assert run(capsys, "review", "list", "--studio", studio)[1]["waiting"][0]["post"] == posts[1]    # only the unapproved one still waits

    # 8. not live yet: a sending tick refuses for the human-less agent, a rehearsal posts nothing, and nothing posted so far
    clock.set("2026-10-26T15:00:30Z")
    rc, res, err = run(capsys, "tick", "--send", "--studio", studio)
    assert rc == 2 and res["needs_human"] and res["run"].startswith("ave studio tick --send")
    rc, res, _ = run(capsys, "tick", "--studio", studio)
    assert res["mode"] == "dry-run" and res["posted"] == [] and any("not live" in r for b in res["blocked"] for r in b["reasons"])
    assert pub.publish_calls == 0

    # 9. the human turns the account live (library primitive = what `account live --on` writes) and lets the scheduler send
    attest.write_live(sp.accounts_dir, "ig-main", "maren")
    monkeypatch.setattr(humanpresence, "is_human_terminal", lambda: True)
    rc, res, _ = run(capsys, "tick", "--send", "--studio", studio)
    assert rc == 0 and len(res["posted"]) == 1 and res["posted"][0]["post"] == posts[0]
    assert pub.publish_calls == 1 and pub.sent[0]["files"][0].endswith(".jpg") and pub.sent[0]["account"] == "ig-main"

    # 10. a second tick posts nothing; neither does a third a day later
    rc, res, _ = run(capsys, "tick", "--send", "--studio", studio)
    assert res["posted"] == [] and pub.publish_calls == 1
    clock.advance(hours=24)
    rc, res, _ = run(capsys, "tick", "--send", "--studio", studio)
    assert res["posted"] == [] and pub.publish_calls == 1
    final = store.load_post(sp, posts[0])["variants"][0]
    assert final["result"]["state"] == "published" and pq.load(sp, item["id"])["status"] == "published"
    # the unapproved post never went out
    assert store.load_post(sp, posts[1])["variants"][0]["result"] is None
    assert [e["op"] for e in ledger.read_jsonl(ledger.events_path(sp))[0] if e["op"] == "dispatch_started"] == ["dispatch_started"]
