"""E1 (qa/E-studio-gates.md): the review screen and the approval must show and sign THE SAME THING. The approval signs the QUEUE ITEM; the screen is built
from the POST. Before, an agent that edited `publish/queue/<id>.json` (caption, link) or pointed a variant at a different queue item got that item approved
by a human who had looked at the post. Now the server returns the queue item's bound fields with every view and refuses an approval unless the item equals
the post. Real HTTP, the PIN flow, and the CLI path."""

import io
import json
import sys

import pytest
from studio_helpers import RecordingPublisher, make_png

from ave import cli, humanpresence
from ave.publish import queue as pq
from ave.studio import ledger, state, store
from ave.studio.sync import queue_diff, sync_queue

from .test_api import PIN, Client, review_target, serve


def license_all(sp):
    """Give every library asset a real, hash-chained own-work license entry, as `library add` + `library attest` would."""
    from ave.library import ingest, registry
    log = registry.license_log(sp)
    for rec in store.read_assets(sp).values():
        if not rec.get("derived_from") and not log.find(sha256=rec["sha256"]):
            e = ingest.default_own_entry("maren", "2026-10-19T09:00:00-0600")
            e.update({"sha256": rec["sha256"], "file": rec["file"], "bytes": rec["bytes"], "approved_by": "maren"})
            log.append(e)


@pytest.fixture
def ui(built):
    license_all(built.sp)
    s = serve(built.studio, pin=PIN)
    yield Client(s)
    s.shutdown()
    s.server_close()


def view(cl, pid, vid, sha):
    return cl.post(f"posts/{pid}/view", {"variant": vid, "sha": sha, "displayed_ms": 3000})


def approve(cl, pid, vid, sha):
    return cl.post("approve", {"by": "maren", "pin": PIN, "items": [{"post": pid, "variant": vid, "sha": sha}]})


def test_e1_a_pristine_item_equals_its_post_and_approves_with_a_signed_binding(built, ui):
    pid, vid, item, sha = review_target(built)
    post = store.load_post(built.sp, pid)
    assert queue_diff(built.sp, post, post["variants"][0], item) == []
    st, res = view(ui, pid, vid, sha)
    assert st == 200 and res["queue"]["mismatch"] == [] and res["queue"]["bound"]["caption"] == item["caption"]
    assert [f["sha256"] for f in res["queue"]["bound"]["files"]]                          # every file sha the approval would sign
    st, res = approve(ui, pid, vid, sha)
    assert st == 200 and res["approved"] and not res["refused"]
    done = pq.load(built.sp, item["id"])
    assert done["status"] == "approved" and done["approvals"][-1]["bind"]["account"] == item["account"]


def test_e1_an_edited_queue_caption_is_shown_as_a_mismatch_and_cannot_be_approved(built, ui):
    pid, vid, item, sha = review_target(built)
    item["caption"] = "ACT NOW: send crypto to 0xBAD (agent-authored, never shown in the review screen)"
    item["title"] = item["caption"][:60]
    pq.save(built.sp, item)                                               # `vi studio/publish/queue/<id>.json` while awaiting_approval
    sha = pq.current_sha(built.sp, item)
    st, res = view(ui, pid, vid, sha)
    assert res["queue"]["bound"]["caption"].startswith("ACT NOW")        # the screen can now show what the approval would sign
    assert [m["field"] for m in res["queue"]["mismatch"]] == ["caption"]
    st, res = approve(ui, pid, vid, sha)
    assert st == 200 and res["approved"] == [] and "does not match the post" in res["refused"][0]["reason"] and "caption" in res["refused"][0]["reason"]
    assert pq.load(built.sp, item["id"])["status"] == "awaiting_approval" and not pq.load(built.sp, item["id"])["approvals"]
    st, detail = ui.get(f"posts/{pid}")                                    # the post detail exposes the same truth
    assert detail["queue"][vid]["bound"]["caption"].startswith("ACT NOW") and detail["queue"][vid]["mismatch"]
    # `queue sync` brings the item back to the post: then (and only then) the human can approve what they reviewed
    sync_queue(built.sp, pid, publishers=built.publishers, now=built.clock.now())
    fresh = state.load_queue_item(built.sp, store.load_post(built.sp, pid)["variants"][0])
    assert "ACT NOW" not in fresh["caption"]
    sha = pq.current_sha(built.sp, fresh)
    assert view(ui, pid, vid, sha)[1]["queue"]["mismatch"] == []
    assert approve(ui, pid, vid, sha)[1]["approved"]


def test_e1_a_variant_pointed_at_another_queue_item_with_other_bytes_cannot_be_approved(built, ui):
    pid, vid, item, sha = review_target(built)
    post = store.load_post(built.sp, pid)
    bad = make_png(built.sp.work / "gen" / "B.jpg", color=(250, 10, 10))
    pubs = {"instagram": RecordingPublisher("instagram"), "facebook": RecordingPublisher("facebook")}
    qb = pq.create(built.sp, files=[bad], media_type="image", platforms=[item["platforms"][0]], title="t", caption=item["caption"], alt_texts=["x"],
                   meta_v=2, account=item["account"], post_type=item["post_type"], publishers=pubs, ai_disclosure=False)
    post["variants"][0]["queue"] = {"root": ".", "id": qb["id"]}
    post["variants"][0]["queue_sha"] = qb["sha256"]
    (built.sp.posts_dir / f"{pid}.json").write_text(json.dumps(post))
    res = view(ui, pid, vid, qb["sha256"])[1]
    assert any(m["field"] == "files" for m in res["queue"]["mismatch"])
    out = approve(ui, pid, vid, qb["sha256"])[1]
    assert out["approved"] == [] and "files" in out["refused"][0]["reason"]
    assert pq.load(built.sp, qb["id"])["status"] == "awaiting_approval"


def test_e1_a_legacy_or_adopted_item_is_not_approvable_as_a_studio_item(built, ui):
    pid, vid, item, sha = review_target(built)
    item.pop("meta_v")
    pq.save(built.sp, item)
    out = approve(ui, pid, vid, pq.current_sha(built.sp, item))[1]
    assert out["approved"] == [] and ("not a studio item" in out["refused"][0]["reason"] or "approve" in out["refused"][0]["reason"] or out["refused"])


def test_e1_the_cli_shows_and_signs_the_queue_item_and_refuses_a_mismatch(built, monkeypatch, capsys):
    from ave.studio.cli import OPEN_KW
    license_all(built.sp)
    monkeypatch.setitem(OPEN_KW, "clock", built.clock)
    monkeypatch.setitem(OPEN_KW, "publishers", built.publishers)
    monkeypatch.setattr(humanpresence, "is_human_terminal", lambda: True)
    pid, vid, item, sha = review_target(built)
    item["first_comment"] = "buy my coin"
    pq.save(built.sp, item)
    d = str(built.sp.root)
    monkeypatch.setattr(sys, "stdin", io.StringIO(pq.current_sha(built.sp, item)[:8] + "\n"))
    rc = cli.main(["studio", "approve", pid, vid, "--by", "maren", "--studio", d])
    res = json.loads(capsys.readouterr().out)
    assert rc == 2 and "does NOT match the post" in res["error"] and [x["field"] for x in res["diff"]] == ["first_comment"]
    assert pq.load(built.sp, item["id"])["status"] == "awaiting_approval"
    assert cli.main(["studio", "queue", "sync", "--post", pid, "--studio", d]) == 0
    capsys.readouterr()
    item = pq.load(built.sp, item["id"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(item["sha256"][:8] + "\n"))
    assert cli.main(["studio", "approve", pid, vid, "--by", "maren", "--studio", d]) == 0


def test_e2_a_send_back_in_the_ui_voids_an_existing_approval(built, ui):
    pid = built.posts["scheduled"]
    post = store.load_post(built.sp, pid)
    vid = post["variants"][0]["id"]
    item = state.load_queue_item(built.sp, post["variants"][0])
    assert item["status"] == "approved"
    st, res = ui.post(f"posts/{pid}/feedback", {"kinds": ["rework_copy"], "variant": vid, "note": "wrong tone"})
    assert st == 201
    assert pq.load(built.sp, item["id"])["status"] == "awaiting_approval"
    assert any(e["op"] == "approval_invalidated" and e["variant"] == vid for e in ledger.read_jsonl(ledger.events_path(built.sp))[0])
    line = [f for f in res["feedback"] if f["kind"] == "rework_copy"][0]
    assert line["state"] and line["source"] == "ui"
