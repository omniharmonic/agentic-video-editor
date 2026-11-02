"""E2: queue.update, and queue.create(meta_v=, account=...). Approval is made by calling queue.approve directly."""

import pytest
from studio_helpers import RecordingPublisher, approve_item, make_png

from ave.publish import queue as pq, safety
from ave.studio.project import StudioProject


@pytest.fixture
def proj(tmp_path):
    sp = StudioProject(tmp_path / "studio")
    sp.create(title="T", tz="UTC")
    return sp


def pubs():
    return {"instagram": RecordingPublisher("instagram")}


def make(sp, name="a.png", **kw):
    f = make_png(sp.exports / name)
    kw.setdefault("alt_texts", ["A picture of bread."])
    return pq.create(sp, files=[f], media_type="image", platforms=["instagram"], title="T", caption="c1", ai_disclosure=False,
                     publishers=pubs(), **kw)


def test_create_stores_v2_fields_only_when_given(proj):
    plain = make(proj)
    for k in ("meta_v", "account", "post_type", "first_comment", "link_url"):
        assert k not in plain
    v2 = make(proj, "b.png", meta_v=2, account="ig-main", post_type="feed", first_comment="hi", link_url="https://x.example")
    assert (v2["meta_v"], v2["account"], v2["post_type"], v2["first_comment"]) == (2, "ig-main", "feed", "hi")
    with pytest.raises(ValueError):
        make(proj, "c.png", meta_v=4)


def test_content_edit_demotes_an_approved_item_with_an_event(proj):
    it = make(proj, meta_v=2, account="ig-main")
    approve_item(proj, it["id"], publishers=pubs())
    assert pq.send_blockers(proj, pq.load(proj, it["id"])) == []
    out = pq.update(proj, it["id"], caption="c2", publishers=pubs())
    assert out["status"] == "awaiting_approval" and out["caption"] == "c2"
    ev = [e for e in out["events"] if e["event"] == "approval_invalidated"]
    assert ev and ev[0]["reasons"] == ["metadata"]
    assert pq.send_blockers(proj, out)                         # the old approval no longer covers it


def test_schedule_only_change_keeps_a_v2_approval(proj):
    it = make(proj, meta_v=2, account="ig-main", schedule="2026-11-02T16:00:00+00:00")
    approve_item(proj, it["id"], publishers=pubs())
    out = pq.update(proj, it["id"], schedule="2026-11-09T16:00:00+00:00", publishers=pubs())
    assert out["status"] == "approved" and out["schedule"] == "2026-11-09T16:00:00+00:00"
    assert pq.send_blockers(proj, out) == []
    assert not any(e["event"] == "approval_invalidated" for e in out["events"])


def test_schedule_change_on_a_legacy_item_still_voids_approval(proj):
    it = make(proj, schedule="2026-11-02T16:00:00+00:00")
    approve_item(proj, it["id"], publishers=pubs())
    out = pq.update(proj, it["id"], schedule="2026-11-09T16:00:00+00:00", publishers=pubs())
    assert out["status"] == "awaiting_approval"


def test_swapping_the_media_rehashes_and_demotes(proj):
    it = make(proj, meta_v=2, account="ig-main")
    approve_item(proj, it["id"], publishers=pubs())
    other = make_png(proj.exports / "other.png", color=(1, 2, 3))
    out = pq.update(proj, it["id"], files=[other], media_type="image", publishers=pubs())
    assert out["status"] == "awaiting_approval"
    assert out["sha256"] == safety.sha256_file(other) and out["file_sha256"] == [safety.sha256_file(other)]
    assert out["files"] == ["exports/other.png"]
    assert [e["reasons"] for e in out["events"] if e["event"] == "approval_invalidated"] == [["media"]]


def test_unchanged_update_leaves_approval_alone(proj):
    it = make(proj, meta_v=2, account="ig-main")
    approve_item(proj, it["id"], publishers=pubs())
    out = pq.update(proj, it["id"], caption="c1", publishers=pubs())
    assert out["status"] == "approved"


def test_preflight_failure_makes_it_a_draft_and_fixing_it_restores_review(proj):
    it = make(proj, meta_v=2, account="ig-main")
    bad = pq.update(proj, it["id"], alt_texts=[""], publishers=pubs())
    assert bad["status"] == "draft" and bad["preflight_problems"]
    good = pq.update(proj, it["id"], alt_texts=["Now described."], publishers=pubs())
    assert good["status"] == "awaiting_approval" and good["preflight_problems"] == []


def test_update_refuses_unknown_fields_and_sent_items(proj):
    it = make(proj)
    with pytest.raises(ValueError, match="cannot update"):
        pq.update(proj, it["id"], status="approved")
    with pytest.raises(ValueError, match="cannot update"):
        pq.update(proj, it["id"], approvals=[])
    approve_item(proj, it["id"], publishers=pubs())
    rep = pq.run(proj, it["id"], send=True, publishers=pubs())
    assert rep["ok"]
    with pytest.raises(ValueError, match="published"):
        pq.update(proj, it["id"], caption="late edit")


def test_update_refuses_while_a_platform_is_uncertain(proj):
    it = make(proj)
    approve_item(proj, it["id"], publishers=pubs())
    p = {"instagram": RecordingPublisher("instagram", behaviors=["network"])}
    pq.run(proj, it["id"], send=True, publishers=p)
    assert pq.load(proj, it["id"])["results"]["instagram"]["state"] == "sending"
    with pytest.raises(ValueError, match="uncertain|published|status"):
        pq.update(proj, it["id"], caption="x")


def test_update_validates_enums(proj):
    it = make(proj)
    with pytest.raises(ValueError, match="visibility"):
        pq.update(proj, it["id"], visibility="everyone")
    with pytest.raises(ValueError, match="format"):
        pq.update(proj, it["id"], format="huge")
