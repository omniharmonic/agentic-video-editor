import json
import threading

import pytest
from publish_helpers import *  # noqa: F401,F403
from publish_helpers import RecordingPublisher, approve_item, queue_item

from ave.publish import queue, safety
from ave.publish.adapters.base import Absent


def pubs(*names, **kw):
    return {n: RecordingPublisher(n, **kw) for n in names}


def test_create_hashes_file_and_defaults_private(ep, wide):
    it = queue_item(ep, wide)
    assert it["status"] == "awaiting_approval" and it["visibility"] == "private"
    assert it["sha256"] == safety.sha256_file(wide)
    assert it["video"] == "exports/final.mp4"
    assert (ep.root / "publish" / "queue" / f"{it['id']}.json").exists()


def test_preflight_problems_keep_draft(ep, wide):
    it = queue.create(ep, video=wide, platforms=["youtube"], title="t", publishers={"youtube": RecordingPublisher(problems=["bad"])})
    assert it["status"] == "draft" and it["preflight_problems"] == ["bad"]


def test_unapproved_never_sends(ep, wide):
    it = queue_item(ep, wide)
    p = pubs("youtube")
    rep = queue.run(ep, it["id"], send=True, publishers=p)
    assert not rep["ok"] and p["youtube"].publish_calls == 0
    assert any("not approved" in b for b in rep["blocked"])


def test_dry_run_is_default_and_writes_nothing(ep, wide):
    it = queue_item(ep, wide)
    approve_item(ep, it["id"], pubs("youtube"))
    before = (queue.queue_dir(ep) / f"{it['id']}.json").read_text()
    p = pubs("youtube")
    rep = queue.run(ep, it["id"], publishers=p)          # send defaults False
    assert rep["mode"] == "dry-run" and rep["ok"] and p["youtube"].publish_calls == 0
    assert (queue.queue_dir(ep) / f"{it['id']}.json").read_text() == before


def test_approved_send_publishes_once_and_is_idempotent(ep, wide):
    it = queue_item(ep, wide, platforms=("youtube", "facebook"))
    approve_item(ep, it["id"], pubs("youtube", "facebook"))
    p = pubs("youtube", "facebook")
    rep = queue.run(ep, it["id"], send=True, publishers=p)
    assert rep["ok"] and queue.load(ep, it["id"])["status"] == "published"
    assert p["youtube"].publish_calls == 1 and p["facebook"].publish_calls == 1
    rep2 = queue.run(ep, it["id"], send=True, publishers=p)
    assert rep2["ok"] and "already published" in rep2["note"]
    assert p["youtube"].publish_calls == 1 and p["facebook"].publish_calls == 1


def test_file_change_after_approval_blocks_and_invalidates(ep, wide):
    it = queue_item(ep, wide)
    approve_item(ep, it["id"], pubs("youtube"))
    wide.write_bytes(wide.read_bytes() + b"\0")        # "re-encode"
    p = pubs("youtube")
    rep = queue.run(ep, it["id"], send=True, publishers=p)
    assert not rep["ok"] and p["youtube"].publish_calls == 0
    assert any("sha256" in b for b in rep["blocked"])
    assert queue.load(ep, it["id"])["status"] == "awaiting_approval"


def test_metadata_edit_after_approval_blocks(ep, wide):
    it = queue_item(ep, wide)
    approve_item(ep, it["id"], pubs("youtube"))
    d = queue.load(ep, it["id"])
    d["title"] = "A different title"
    queue.save(ep, d)
    p = pubs("youtube")
    assert not queue.run(ep, it["id"], send=True, publishers=p)["ok"] and p["youtube"].publish_calls == 0


def test_hand_forged_approval_blocks(ep, wide):
    it = queue_item(ep, wide)
    d = queue.load(ep, it["id"])
    d["status"] = "approved"
    d["approvals"] = [{"by": "agent", "at": "now", "sha256": d["sha256"], "meta_sha256": safety.meta_hash(d)}]
    queue.save(ep, d)
    p = pubs("youtube")
    rep = queue.run(ep, it["id"], send=True, publishers=p)
    assert not rep["ok"] and p["youtube"].publish_calls == 0 and any("signature" in b for b in rep["blocked"])


def test_approve_requires_matching_hash_confirmation(ep, wide):
    it = queue_item(ep, wide)
    for bad in (None, "", "abc", "0" * 12):
        with pytest.raises(ValueError):
            queue.approve(ep, it["id"], "ben", bad, publishers=pubs("youtube"))
    assert queue.load(ep, it["id"])["status"] == "awaiting_approval"
    with pytest.raises(ValueError):
        queue.approve(ep, it["id"], "  ", it["sha256"][:12], publishers=pubs("youtube"))


def test_approve_records_who_when_hash(ep, wide):
    it = queue_item(ep, wide)
    d = approve_item(ep, it["id"], pubs("youtube"))
    a = d["approvals"][-1]
    assert a["by"] == "ben" and a["sha256"] == it["sha256"] and a["at"] and a["sig"]


def test_reject_voids_approval(ep, wide):
    it = queue_item(ep, wide)
    approve_item(ep, it["id"], pubs("youtube"))
    queue.reject(ep, it["id"], "ben", "nope")
    p = pubs("youtube")
    assert not queue.run(ep, it["id"], send=True, publishers=p)["ok"] and p["youtube"].publish_calls == 0


def test_definite_failure_is_retryable_and_partial_success_not_resent(ep, wide):
    it = queue_item(ep, wide, platforms=("youtube", "facebook"))
    approve_item(ep, it["id"], pubs("youtube", "facebook"))
    p = {"youtube": RecordingPublisher("youtube"), "facebook": RecordingPublisher("facebook", "refuse")}
    rep = queue.run(ep, it["id"], send=True, publishers=p)
    d = queue.load(ep, it["id"])
    assert not rep["ok"] and d["status"] == "failed"
    assert d["results"]["youtube"]["state"] == "published" and d["results"]["facebook"]["state"] == "failed"
    p["facebook"].behavior = "ok"
    queue.run(ep, it["id"], send=True, publishers=p)
    assert queue.load(ep, it["id"])["status"] == "published"
    assert p["youtube"].publish_calls == 1 and p["facebook"].publish_calls == 2


def test_crash_mid_publish_never_double_posts(ep, wide):
    it = queue_item(ep, wide)
    approve_item(ep, it["id"], pubs("youtube"))
    crash = RecordingPublisher("youtube", "crash")
    with pytest.raises(KeyboardInterrupt):
        queue.run(ep, it["id"], send=True, publishers={"youtube": crash})
    d = queue.load(ep, it["id"])
    assert d["results"]["youtube"]["state"] == "sending" and d["results"]["youtube"]["upload_id"] == "u1"
    # restart: recovery unknown -> must NOT publish again
    again = RecordingPublisher("youtube", "ok", recover=None)
    rep = queue.run(ep, it["id"], send=True, publishers={"youtube": again})
    assert again.publish_calls == 0 and rep["platforms"]["youtube"]["outcome"] == "uncertain" and not rep["ok"]
    # operator says it was posted
    queue.resolve(ep, it["id"], "youtube", posted_url="https://youtu.be/abc", by="ben")
    d = queue.load(ep, it["id"])
    assert d["status"] == "published"
    assert queue.run(ep, it["id"], send=True, publishers={"youtube": again})["ok"] and again.publish_calls == 0


def test_crash_recovery_found_marks_published_without_resend(ep, wide):
    it = queue_item(ep, wide)
    approve_item(ep, it["id"], pubs("youtube"))
    with pytest.raises(KeyboardInterrupt):
        queue.run(ep, it["id"], send=True, publishers={"youtube": RecordingPublisher("youtube", "crash")})
    rec = RecordingPublisher("youtube", recover={"id": "found", "url": "https://youtu.be/found"})
    queue.run(ep, it["id"], send=True, publishers={"youtube": rec})
    d = queue.load(ep, it["id"])
    assert rec.publish_calls == 0 and d["status"] == "published" and d["results"]["youtube"]["id"] == "found"


def test_crash_recovery_absent_allows_single_retry(ep, wide):
    it = queue_item(ep, wide)
    approve_item(ep, it["id"], pubs("youtube"))
    with pytest.raises(KeyboardInterrupt):
        queue.run(ep, it["id"], send=True, publishers={"youtube": RecordingPublisher("youtube", "crash")})
    rec = RecordingPublisher("youtube", recover=Absent("none"))
    queue.run(ep, it["id"], send=True, publishers={"youtube": rec})
    assert rec.publish_calls == 1 and queue.load(ep, it["id"])["status"] == "published"


def test_network_error_leaves_uncertain_not_failed(ep, wide):
    it = queue_item(ep, wide)
    approve_item(ep, it["id"], pubs("youtube"))
    p = {"youtube": RecordingPublisher("youtube", "network")}
    queue.run(ep, it["id"], send=True, publishers=p)
    assert queue.load(ep, it["id"])["results"]["youtube"]["state"] == "sending"
    p["youtube"].behavior = "ok"
    rep = queue.run(ep, it["id"], send=True, publishers=p)
    assert p["youtube"].publish_calls == 1 and rep["platforms"]["youtube"]["outcome"] == "uncertain"


def test_resolve_retry_then_resend(ep, wide):
    it = queue_item(ep, wide)
    approve_item(ep, it["id"], pubs("youtube"))
    p = {"youtube": RecordingPublisher("youtube", "network")}
    queue.run(ep, it["id"], send=True, publishers=p)
    queue.resolve(ep, it["id"], "youtube", retry=True, by="ben")
    p["youtube"].behavior = "ok"
    queue.run(ep, it["id"], send=True, publishers=p)
    assert queue.load(ep, it["id"])["status"] == "published" and p["youtube"].publish_calls == 2
    with pytest.raises(ValueError):
        queue.resolve(ep, it["id"], "youtube", retry=True)


def test_concurrent_run_is_refused(ep, wide):
    it = queue_item(ep, wide)
    approve_item(ep, it["id"], pubs("youtube"))
    with queue._lock(ep, it["id"]):
        with pytest.raises(RuntimeError, match="already working"):
            queue.run(ep, it["id"], send=True, publishers=pubs("youtube"))


def test_missing_credentials_block_send_but_not_dry_run(ep, wide):
    it = queue_item(ep, wide)
    approve_item(ep, it["id"], pubs("youtube"))
    class NoCreds(RecordingPublisher):
        def ready_problems(self): return ["no creds"]
    p = {"youtube": NoCreds("youtube")}
    assert queue.run(ep, it["id"], publishers=p)["ok"]
    rep = queue.run(ep, it["id"], send=True, publishers=p)
    assert not rep["ok"] and p["youtube"].publish_calls == 0


def test_results_never_store_secrets(ep, wide):
    it = queue_item(ep, wide)
    approve_item(ep, it["id"], pubs("youtube"))
    class Leaky(RecordingPublisher):
        def publish(self, item, video, progress):
            progress({"access_token": "ya29.a0AfH6SMBzzzzzzzzzzzzzzzz"})
            raise ConnectionError("POST https://x?access_token=EAAB1234567890abcdefghijk failed")
    queue.run(ep, it["id"], send=True, publishers={"youtube": Leaky("youtube")})
    raw = (queue.queue_dir(ep) / f"{it['id']}.json").read_text()
    assert "ya29.a0AfH6" not in raw and "EAAB12345" not in raw


def test_bad_ids_rejected(ep):
    for bad in ("../x", "a/b", ".hidden", ""):
        with pytest.raises(ValueError):
            queue.load(ep, bad)
