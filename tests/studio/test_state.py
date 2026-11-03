"""Status derivation: one test per row of the spec 2.8 table (and the precedence between rows), pure, then with real queue items."""

from datetime import datetime, timedelta, timezone

import pytest
from studio_helpers import RecordingPublisher, approve_item, make_png, queue_variant, add_asset

from ave.publish import queue as pq
from ave.studio import model, state
from ave.studio.model import VARIANT_STATUSES

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
APPROVED_AT = "2026-11-01T05:00:00+0000"


def var(**kw):
    v = model.new_variant(account="ig-main", platform="instagram", post_type="feed", variant_id="v_t")
    v.update(kw)
    return v


def sched(dt):
    return {"local": dt.strftime("%Y-%m-%dT%H:%M"), "tz": "UTC", "utc": dt.strftime("%Y-%m-%dT%H:%M:%SZ"), "slot": None,
            "locked": False, "set_by": "agent"}


def qi(status, approved_at=APPROVED_AT, results=None):
    item = {"id": "q1", "status": status, "approvals": [], "results": results or {}}
    if status in ("approved", "publishing", "failed", "published"):
        item["approvals"] = [{"by": "maren", "at": approved_at, "sha256": "x", "meta_sha256": "y", "sig": "z"}]
    return item


def fb(kind, variant="v_t"):
    return {"id": "fb_0001", "op": "feedback", "kind": kind, "variant": variant, "post": "p_x"}


def d(variant, item, fbs=(), now=NOW):
    return state.derive(variant, item, list(fbs), now)


# ---- one test per row, in table order -------------------------------------------------------------------------

def test_row_dropped_by_variant_flag():
    assert d(var(dropped=True), qi("awaiting_approval")) == "dropped"


def test_row_dropped_by_rejected_queue_item():
    assert d(var(), qi("rejected")) == "dropped"


def test_row_published():
    assert d(var(), qi("published")) == "published"


def test_row_publishing_by_status():
    assert d(var(), qi("publishing")) == "publishing"


def test_row_publishing_by_an_unknown_sending_platform_even_when_status_says_failed():
    item = qi("failed", results={"instagram": {"state": "sending"}})
    assert state.explain(var(), item, [], NOW)["status"] == "publishing"
    assert "uncertain" in state.explain(var(), item, [], NOW)["flags"]


def test_row_failed():
    assert d(var(), qi("failed", results={"instagram": {"state": "failed"}})) == "failed"


def test_row_missed_when_the_slot_passed_by_more_than_late_after_min():
    late = NOW - timedelta(minutes=121)
    assert d(var(schedule=sched(late)), qi("approved")) == "missed"


def test_slot_within_late_after_min_is_still_scheduled():
    assert d(var(schedule=sched(NOW - timedelta(minutes=119))), qi("approved")) == "scheduled"


def test_row_needs_rework_for_rework_copy_and_rework_visual():
    assert d(var(), qi("awaiting_approval"), [fb("rework_copy")]) == "needs_rework"
    assert d(var(), qi("awaiting_approval"), [fb("rework_visual")]) == "needs_rework"
    assert d(var(), None, [fb("rework_copy")]) == "needs_rework"                          # even before any queue item exists


def test_other_feedback_kinds_and_other_variants_do_not_cause_rework():
    for kind in ("note", "retime", "human_edit", "drop"):
        assert d(var(), qi("awaiting_approval"), [fb(kind)]) == "in_review"
    assert d(var(), qi("awaiting_approval"), [fb("rework_copy", variant="v_other")]) == "in_review"
    assert d(var(), qi("awaiting_approval"), [fb("rework_copy", variant=None)]) == "needs_rework"        # post-wide feedback applies


def test_row_scheduled():
    assert d(var(schedule=sched(NOW + timedelta(days=1))), qi("approved")) == "scheduled"


def test_row_approved_pool_has_no_schedule():
    assert d(var(), qi("approved")) == "approved"


def test_row_approved_when_the_approval_expired_with_a_flag():
    old = (NOW - timedelta(days=15)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    v = var(schedule=sched(NOW + timedelta(days=1)))
    ex = state.explain(v, qi("approved", approved_at=old), [], NOW)
    assert ex["status"] == "approved" and "approval_expired" in ex["flags"]
    fresh = (NOW - timedelta(days=13)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    assert d(v, qi("approved", approved_at=fresh)) == "scheduled"


def test_row_in_review():
    assert d(var(), qi("awaiting_approval")) == "in_review"


def test_row_draft():
    assert d(var(), None) == "draft"
    assert d(var(), qi("draft")) == "draft"


def test_every_status_in_the_table_is_reachable_and_nothing_else_is_returned():
    cases = [(var(dropped=True), qi("draft"), []), (var(), qi("published"), []), (var(), qi("publishing"), []), (var(), qi("failed"), []),
             (var(schedule=sched(NOW - timedelta(days=1))), qi("approved"), []), (var(), qi("draft"), [fb("rework_copy")]),
             (var(schedule=sched(NOW + timedelta(days=1))), qi("approved"), []), (var(), qi("approved"), []),
             (var(), qi("awaiting_approval"), []), (var(), None, [])]
    got = [d(*c) for c in cases]
    assert got == ["dropped", "published", "publishing", "failed", "missed", "needs_rework", "scheduled", "approved", "in_review", "draft"]
    assert set(got) == set(VARIANT_STATUSES)


# ---- precedence: first match wins -----------------------------------------------------------------------------

@pytest.mark.parametrize("v,item,fbs,expected", [
    (dict(dropped=True), qi("published"), [], "dropped"),                                             # dropped beats published
    (dict(), qi("published"), [fb("rework_copy")], "published"),                                      # published beats rework
    (dict(), qi("publishing"), [fb("rework_copy")], "publishing"),
    (dict(), qi("failed"), [fb("rework_copy")], "failed"),                                            # failed beats rework
    (dict(schedule=sched(NOW - timedelta(days=2))), qi("approved"), [fb("rework_copy")], "missed"),    # missed beats rework
    (dict(schedule=sched(NOW + timedelta(days=2))), qi("approved"), [fb("rework_visual")], "needs_rework"),   # rework beats scheduled
])
def test_first_match_wins(v, item, fbs, expected):
    assert d(var(**v), item, fbs) == expected


def test_unapproved_item_past_its_slot_is_not_missed():
    assert d(var(schedule=sched(NOW - timedelta(days=2))), qi("awaiting_approval")) == "in_review"


# ---- with a real project: policy and the approval being re-verified -------------------------------------------

def test_with_a_studio_the_derived_status_follows_the_live_queue(built, studio):
    for lifecycle, expected in (("variant", "draft"), ("in_review", "in_review"), ("scheduled", "scheduled"), ("approved", "approved")):
        post = studio.post(built.posts[lifecycle])
        assert post["variants"][0]["status"] == expected, lifecycle
    assert studio.post(built.posts["draft"])["variants"] == []


def test_changed_bytes_make_an_approved_item_in_review_with_a_flag(built, studio):
    pid = built.posts["scheduled"]
    post = studio.post(pid)
    v = post["variants"][0]
    item = pq.load(built.sp, v["queue"]["id"])
    (built.sp.root / item["files"][0]).write_bytes(make_png(built.sp.work / "x.png", color=(9, 9, 9)).read_bytes())
    ex = state.explain(v, item, [], built.clock.now(), built.sp)
    assert ex["status"] == "in_review" and "approval_stale" in ex["flags"]


def test_studio_ttl_and_account_may_only_tighten(built):
    from ave.studio import policy
    from studio_helpers import write_account
    assert policy.approval_ttl_days(built.sp) == 14 and policy.approval_ttl_days(built.sp, "ig-main") == 14
    write_account(built.sp, "ig-main", "instagram", approval={"ttl_days": 3})
    write_account(built.sp, "fb-page", "facebook", approval={"ttl_days": 90})
    assert policy.approval_ttl_days(built.sp, "ig-main") == 3 and policy.approval_ttl_days(built.sp, "fb-page") == 14


def test_expiry_uses_the_account_ttl_via_the_clock(built, studio):
    from studio_helpers import write_account
    pid = built.posts["scheduled"]
    v = studio.post(pid)["variants"][0]
    assert v["status"] == "scheduled"
    write_account(built.sp, v["account"], v["platform"], approval={"ttl_days": 1})
    built.clock.advance(days=2)
    after = studio.post(pid)["variants"][0]            # the slot is days away, the approval is 2 days old, the TTL is 1
    assert after["status"] == "approved" and "approval_expired" in after["flags"]


def test_missed_uses_the_studio_late_after_min(built, studio):
    import json
    pid = built.posts["scheduled"]
    v = studio.post(pid)["variants"][0]
    at = datetime.fromisoformat(v["schedule"]["utc"].replace("Z", "+00:00"))
    built.clock.set(at + timedelta(minutes=60))
    assert studio.post(pid)["variants"][0]["status"] == "scheduled"
    meta = built.sp.read_meta()
    meta["late_after_min"] = 30
    built.sp.meta_path.write_text(json.dumps(meta))
    assert studio.post(pid)["variants"][0]["status"] == "missed"


def test_a_sent_item_is_published_and_the_cache_free_derivation_sees_it(built, studio):
    pid = built.posts["approved"]
    v = studio.post(pid)["variants"][0]
    rep = pq.run(built.sp, v["queue"]["id"], send=True, publishers={"instagram": RecordingPublisher("instagram"),
                                                                    "facebook": RecordingPublisher("facebook")})
    assert rep["ok"]
    assert studio.post(pid)["variants"][0]["status"] == "published"
