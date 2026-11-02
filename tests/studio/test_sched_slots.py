"""WS3: slots, calendar shape, check_move, move / swap / unschedule, proposals, and the Studio hooks (spec 2.6, 6.3, 7.3, 7.5)."""

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sched_helpers import make_world, no_agent_markers, utc  # noqa: F401

from ave.publish import queue as pq
from ave.schedule import proposals, slots
from ave.schedule.slots import MoveRefused, resolve_local
from ave.studio import dispatch, state, store
from ave.studio.events import AGENT, HUMAN
from ave.studio.service import NotImplementedYet, Studio

API = Path(__file__).parent.parent / "data_studio" / "api"
DENVER = "America/Denver"


# ------------------------------------------------------------------------------------------------ DST (spec 2.6)

def test_resolve_local_plain_time():
    dt = resolve_local("2026-10-21T09:00", DENVER)
    assert dt.astimezone(timezone.utc).isoformat() == "2026-10-21T15:00:00+00:00" and dt.utcoffset() == timedelta(hours=-6)


def test_resolve_local_spring_forward_gap_shifts_to_the_next_valid_minute():
    # 2026-03-08 02:00 -> 03:00 in America/Denver: 02:30 does not exist
    dt = resolve_local("2026-03-08T02:30", DENVER)
    assert dt.strftime("%H:%M") == "03:00" and dt.astimezone(timezone.utc).strftime("%H:%M") == "09:00"
    assert resolve_local("2026-03-08T03:00", DENVER) == dt
    assert resolve_local("2026-03-08T01:59", DENVER).strftime("%H:%M") == "01:59"


def test_resolve_local_fall_back_ambiguity_takes_the_first_occurrence():
    # 2026-11-01 01:30 happens twice; the first is MDT (UTC-6) = 07:30Z
    dt = resolve_local("2026-11-01T01:30", DENVER)
    assert dt.utcoffset() == timedelta(hours=-6) and dt.astimezone(timezone.utc).strftime("%H:%M") == "07:30"


def test_resolve_local_rejects_an_offset_and_junk():
    with pytest.raises(ValueError):
        resolve_local("2026-10-21T09:00-06:00", DENVER)
    with pytest.raises(ValueError):
        resolve_local("tomorrow", DENVER)


def test_make_schedule_round_trips_local_tz_utc():
    s = slots.make_schedule(resolve_local("2026-11-02T09:00", DENVER), slot="r1", set_by="human")
    assert s == {"local": "2026-11-02T09:00", "tz": DENVER, "utc": "2026-11-02T16:00:00Z", "slot": "r1", "locked": False, "set_by": "human"}


def test_slots_follow_local_time_across_the_fall_back_change(tmp_path):
    w = make_world(tmp_path)
    got = [(s["local"], s["utc"].strftime("%Y-%m-%dT%H:%MZ")) for s in
           slots.iter_slots(w.sp, "ig-main", utc("2026-10-30T00:00:00Z"), utc("2026-11-05T00:00:00Z"), "feed")]
    # Fri 10-30 09:00 MDT = 15:00Z; Mon 11-02 09:00 MST = 16:00Z (the wall-clock time is kept, the UTC time moves)
    assert got == [("2026-10-30T09:00", "2026-10-30T15:00Z"), ("2026-11-02T09:00", "2026-11-02T16:00Z"), ("2026-11-04T09:00", "2026-11-04T16:00Z")]


def test_overrides_skip_a_date_and_post_types_filter(tmp_path):
    w = make_world(tmp_path)
    import yaml
    from sched_helpers import RULES
    (w.sp.schedule_dir / "ig-main.yaml").write_text(yaml.safe_dump({**RULES, "overrides": [{"date": "2026-10-21", "skip": True}]}))
    feed = [s["local"] for s in slots.iter_slots(w.sp, "ig-main", utc("2026-10-19T00:00:00Z"), utc("2026-10-26T00:00:00Z"), "feed")]
    story = [s["local"] for s in slots.iter_slots(w.sp, "ig-main", utc("2026-10-19T00:00:00Z"), utc("2026-10-26T00:00:00Z"), "story")]
    assert feed == ["2026-10-19T09:00", "2026-10-23T09:00"] and story == ["2026-10-20T12:30", "2026-10-22T12:30"]


def test_a_malformed_rules_file_is_an_error_not_silence(tmp_path):
    w = make_world(tmp_path)
    (w.sp.schedule_dir / "ig-main.yaml").write_text("rules: [{id: r1, dow: [funday], time: '9am'}]\n")
    with pytest.raises(ValueError):
        list(slots.iter_slots(w.sp, "ig-main", utc("2026-10-19T00:00:00Z"), utc("2026-10-26T00:00:00Z")))


def test_next_slot_skips_taken_slots_and_respects_post_type(tmp_path):
    w = make_world(tmp_path)
    nxt = slots.next_slot(w.sp, "ig-main", after=w.clock.now(), now=w.clock.now(), post_type="feed")
    assert nxt["local"] == "2026-10-21T09:00" and nxt["utc"] == "2026-10-21T15:00:00Z" and nxt["rule"] == "r1"
    w.add_post(at="2026-10-21T15:00:00Z", sync=False)
    again = slots.next_slot(w.sp, "ig-main", after=w.clock.now(), now=w.clock.now(), post_type="feed")
    assert again["local"] == "2026-10-23T09:00"
    assert slots.next_slot(w.sp, "ig-main", after=w.clock.now(), now=w.clock.now(), post_type="story")["local"] == "2026-10-20T12:30"
    assert slots.next_slot(w.sp, "ig-main", after=w.clock.now(), now=w.clock.now(), post_type="pin") is None


def test_next_slot_none_without_rules(tmp_path):
    w = make_world(tmp_path, rules=False)
    assert slots.next_slot(w.sp, "ig-main", after=w.clock.now(), now=w.clock.now()) is None


# ------------------------------------------------------------------------------------------------ check_move

def test_check_move_reports_each_kind_of_conflict(tmp_path):
    w = make_world(tmp_path, quiet_hours=[{"from": "22:00", "to": "07:00"}])
    now = w.clock.now()
    pid, vid = w.add_post(at="2026-10-21T15:00:00Z", sync=False)               # Wed 09:00 local
    assert slots.check_move(w.sp, "ig-main", utc("2026-10-23T15:00:00Z"), now=now) == []
    assert slots.check_move(w.sp, "ig-main", utc("2026-10-21T15:00:00Z"), now=now) == [f"slot taken by {pid}"]
    assert slots.check_move(w.sp, "ig-main", utc("2026-10-21T15:00:00Z"), now=now, ignore=vid) == []          # the mover's own slot does not count
    assert slots.check_move(w.sp, "ig-main", utc("2026-10-21T15:00:00Z"), now=now, ignore=f"{pid}:{vid}") == []
    gap = slots.check_move(w.sp, "ig-main", utc("2026-10-21T16:30:00Z"), now=now)                              # 90 min later, gap is 180
    assert len(gap) == 1 and "too close" in gap[0] and "180" in gap[0]
    assert slots.check_move(w.sp, "ig-main", utc("2026-10-21T19:00:00Z"), now=now) == []                       # exactly 240 min later
    assert any("quiet hours" in m for m in slots.check_move(w.sp, "ig-main", utc("2026-10-24T05:00:00Z"), now=now))   # 23:00 local
    assert slots.check_move(w.sp, "ig-main", utc("2026-10-19T15:00:00Z"), now=now) == ["that time is in the past"]
    assert slots.check_move(w.sp, "nope", utc("2026-10-23T15:00:00Z"), now=now) == ["unknown account 'nope'"]


def test_check_move_enforces_max_per_day(tmp_path):
    w = make_world(tmp_path)                                  # max_per_day 2, min_gap 180
    now = w.clock.now()
    w.add_post(at="2026-10-21T14:00:00Z", sync=False)         # 08:00 local
    w.add_post(at="2026-10-21T18:00:00Z", sync=False)         # 12:00 local
    out = slots.check_move(w.sp, "ig-main", utc("2026-10-21T22:00:00Z"), now=now)
    assert any("limit 2 per day" in m for m in out)


# ------------------------------------------------------------------------------------------------ calendar (7.3 shapes)

def test_calendar_matches_the_committed_fixture_shape(tmp_path):
    w = make_world(tmp_path)
    p1, v1 = w.add_post("Day 3: the starter smells like pears. That is normal.", at="2026-10-21T15:00:00Z")
    p2, v2 = w.add_post("Day 5: bubbles everywhere.", at="2026-10-23T15:00:00Z")
    w.approve(p1, v1)
    w.add_post("A draft without a slot.")
    w.studio.create_post(idea="Why we weigh flour", copy={"base": "We weigh the flour."})
    ref = json.loads((API / "GET_calendar.json").read_text())["response"]
    cal = w.studio.calendar(date(2026, 10, 19), date(2026, 10, 31))
    assert set(cal) == set(ref)
    assert cal["tz"] == "America/Denver" and cal["cursor"].startswith("ev:")
    assert set(cal["accounts"][0]) == set(ref["accounts"][0]) and cal["accounts"][0] == {"id": "ig-main", "platform": "instagram", "live": False, "paused": False, "brand": None, "label": "@ig-main"}
    day = cal["days"]["2026-10-21"][0]
    assert set(day) == set(ref["days"]["2026-11-02"][0])
    assert day["at"] == "2026-10-21T09:00:00-06:00" and day["status"] == "scheduled" and day["title"] == "Day 3: the starter smells like pears"
    assert day["thumb"].startswith("/files/library/assets/") and day["locked"] is False and day["post"] == p1
    assert cal["days"]["2026-10-23"][0]["status"] == "in_review"
    slot = cal["slots"]["2026-10-21"][0]
    assert set(slot) == set(ref["slots"]["2026-11-04"][0]) and slot["free"] is False and slot["rule"] == "r1"
    assert cal["slots"]["2026-10-26"][0]["free"] is True
    tray = {t["variant"] for t in cal["unscheduled"]}
    assert None in tray and len(cal["unscheduled"]) == 2
    assert set(next(t for t in cal["unscheduled"] if t["variant"])) == set(ref["unscheduled"][0])
    assert set(next(t for t in cal["unscheduled"] if t["variant"] is None)) == set(ref["unscheduled"][0])


def test_calendar_filters_by_account_and_range(tmp_path):
    w = make_world(tmp_path, accounts=(("ig-main", "instagram"), ("fb-page", "facebook")))
    w.add_post(at="2026-10-21T15:00:00Z", account="ig-main", sync=False)
    w.add_post(at="2026-10-21T15:00:00Z", account="fb-page", sync=False)
    both = w.studio.calendar("2026-10-21", "2026-10-21")
    assert len(both["days"]["2026-10-21"]) == 2 and [a["id"] for a in both["accounts"]] == ["fb-page", "ig-main"]
    only = w.studio.calendar("2026-10-21", "2026-10-21", ["fb-page"])
    assert [c["account"] for c in only["days"]["2026-10-21"]] == ["fb-page"] and only["unscheduled"] == []
    assert w.studio.calendar("2026-10-22", "2026-10-22")["days"] == {}


def test_calendar_marks_live_accounts_and_pauses(tmp_path):
    w = make_world(tmp_path)
    assert w.studio.calendar("2026-10-21", "2026-10-21")["accounts"][0]["live"] is False
    w.go_live()
    cal = w.studio.calendar("2026-10-21", "2026-10-21")
    assert cal["accounts"][0]["live"] is True and cal["accounts"][0]["paused"] is False
    dispatch.pause(w.sp, now=w.clock.now())
    assert w.studio.calendar("2026-10-21", "2026-10-21")["accounts"][0]["paused"] is True


def test_calendar_proposals_listed(tmp_path):
    w = make_world(tmp_path)
    pid = proposals.create(w.sp, "claude", [{"post": "p_x", "variant": "v_x", "to": {"local": "2026-10-23T09:00", "tz": DENVER}, "why": "free"}], now=w.clock.now())
    assert [p["id"] for p in w.studio.calendar("2026-10-19", "2026-10-31")["proposals"]] == [pid] == ["sp_0001"]


# ------------------------------------------------------------------------------------------------ move

def test_move_snaps_to_the_days_slot_and_returns_the_view_shape(tmp_path):
    w = make_world(tmp_path)
    pid, vid = w.add_post(at="2026-10-21T15:00:00Z")
    rev = store.load_post(w.sp, pid)["rev"]
    res = w.studio.move(pid, vid, {"local": "2026-10-23T14:00", "tz": DENVER}, expected_rev=rev, actor=HUMAN)
    ref = json.loads((API / "POST_move_ok.json").read_text())["response"]
    assert set(res) >= set(ref) and res["applied"] is True and res["warnings"] == []
    v = res["post"]["variants"][0]
    assert v["schedule"] == {"local": "2026-10-23T09:00", "tz": DENVER, "utc": "2026-10-23T15:00:00Z", "slot": "r1", "locked": False, "set_by": "human"}
    assert res["post"]["rev"] == rev + 1 and "status" in v and "queue_status" in v
    exact = w.studio.move(pid, vid, {"local": "2026-10-23T14:00", "tz": DENVER}, expected_rev=rev + 1, actor=HUMAN, snap="none")
    assert exact["post"]["variants"][0]["schedule"]["utc"] == "2026-10-23T20:00:00Z" and exact["post"]["variants"][0]["schedule"]["slot"] is None
    assert w.events("move")[-1]["detail"]["to"] == "2026-10-23T20:00:00Z"


def test_move_to_a_slot_chip_exactly(tmp_path):
    w = make_world(tmp_path)
    pid, vid = w.add_post(at="2026-10-21T15:00:00Z", sync=False)
    out = slots.move(w.sp, pid, vid, {"slot": "ig-main:2026-10-23T09:00"}, expected_rev=store.load_post(w.sp, pid)["rev"], actor=AGENT, now=w.clock.now())
    assert out["post"]["variants"][0]["schedule"]["slot"] == "r1" and out["post"]["variants"][0]["schedule"]["set_by"] == "agent"
    with pytest.raises(MoveRefused) as bad:
        slots.move(w.sp, pid, vid, {"slot": "ig-main:2026-10-23T10:00"}, expected_rev=out["post"]["rev"], actor=AGENT, now=w.clock.now())
    assert bad.value.code == "conflict.slot"
    with pytest.raises(MoveRefused):
        slots.move(w.sp, pid, vid, {"slot": "other:2026-10-23T09:00"}, expected_rev=out["post"]["rev"], actor=AGENT, now=w.clock.now())


def test_move_conflict_is_a_409_body_with_code_and_latest(tmp_path):
    w = make_world(tmp_path)
    p1, v1 = w.add_post(at="2026-10-21T15:00:00Z", sync=False)
    p2, v2 = w.add_post(at="2026-10-23T15:00:00Z", sync=False)
    with pytest.raises(MoveRefused) as c:
        w.studio.move(p2, v2, {"local": "2026-10-21T09:00", "tz": DENVER}, expected_rev=store.load_post(w.sp, p2)["rev"], actor=HUMAN)
    ref = json.loads((API / "POST_move_conflict.json").read_text())
    assert c.value.code == ref["response"]["code"] == "conflict.slot" and str(c.value) == f"slot taken by {p1}"
    assert c.value.latest["id"] == p2 and c.value.latest["variants"][0]["schedule"]["utc"] == "2026-10-23T15:00:00Z"
    assert store.load_post(w.sp, p2)["variants"][0]["schedule"]["utc"] == "2026-10-23T15:00:00Z"            # nothing was written


def test_move_with_a_stale_rev_is_conflict_rev_with_latest(tmp_path):
    w = make_world(tmp_path)
    pid, vid = w.add_post(at="2026-10-21T15:00:00Z", sync=False)
    cur = store.load_post(w.sp, pid)["rev"]
    with pytest.raises(MoveRefused) as c:
        w.studio.move(pid, vid, {"local": "2026-10-23T09:00", "tz": DENVER}, expected_rev=cur - 1, actor=HUMAN)
    assert c.value.code == "conflict.rev" and c.value.latest["rev"] == cur


def test_move_refuses_past_quiet_and_a_full_day(tmp_path):
    w = make_world(tmp_path, quiet_hours=[{"from": "08:00", "to": "09:30"}])
    pid, vid = w.add_post(at="2026-10-21T15:00:00Z", sync=False)
    rev = store.load_post(w.sp, pid)["rev"]
    for to, code in (({"local": "2026-10-19T09:00", "tz": DENVER}, "conflict.past"), ({"local": "2026-10-23T09:00", "tz": DENVER}, "conflict.quiet")):
        with pytest.raises(MoveRefused) as c:
            w.studio.move(pid, vid, to, expected_rev=rev, actor=HUMAN)
        assert c.value.code == code


def test_move_on_a_day_with_no_free_slot_is_refused_when_snapping(tmp_path):
    w = make_world(tmp_path)
    w.add_post(at="2026-10-23T15:00:00Z", sync=False)
    pid, vid = w.add_post(at="2026-10-21T15:00:00Z", sync=False)
    with pytest.raises(MoveRefused) as c:
        w.studio.move(pid, vid, {"local": "2026-10-23T11:00", "tz": DENVER}, expected_rev=store.load_post(w.sp, pid)["rev"], actor=HUMAN)
    assert c.value.code == "conflict.day"


def test_retime_keeps_a_humans_approval_valid_and_marks_the_card(tmp_path):
    w = make_world(tmp_path)
    pid, vid = w.add_post(at="2026-10-21T15:00:00Z")
    w.approve(pid, vid)
    before = w.item(pid, vid)
    out = w.studio.move(pid, vid, {"local": "2026-10-23T09:00", "tz": DENVER}, expected_rev=store.load_post(w.sp, pid)["rev"], actor=HUMAN)
    after = w.item(pid, vid)
    assert out["applied"] and after["status"] == "approved" and after["approvals"] == before["approvals"]
    assert after["schedule"] == "2026-10-23T15:00:00+00:00" and pq.send_blockers(w.sp, after) == []
    assert "moved_after_approval" in out["post"]["variants"][0]["flags"] and out["post"]["variants"][0]["status"] == "scheduled"


def test_an_agent_retime_of_an_approved_post_is_only_a_proposal(tmp_path):
    w = make_world(tmp_path)
    pid, vid = w.add_post(at="2026-10-21T15:00:00Z")
    w.approve(pid, vid)
    rev = store.load_post(w.sp, pid)["rev"]
    out = w.studio.move(pid, vid, {"local": "2026-10-23T09:00", "tz": DENVER}, expected_rev=rev, actor=AGENT)
    assert out["applied"] is False and out["proposal"] == "sp_0001"
    assert store.load_post(w.sp, pid)["rev"] == rev and w.variant(pid, vid)["schedule"]["utc"] == "2026-10-21T15:00:00Z"
    assert [p["id"] for p in proposals.pending(w.sp)] == ["sp_0001"]
    with pytest.raises(PermissionError):
        w.studio.accept_proposal("sp_0001", actor=AGENT)
    res = w.studio.accept_proposal("sp_0001", actor=HUMAN)
    assert len(res["applied"]) == 1 and res["failed"] == []
    v = w.variant(pid, vid)
    assert v["schedule"]["utc"] == "2026-10-23T15:00:00Z" and v["schedule"]["set_by"] == "human" and "moved_after_approval" in v["flags"]
    assert proposals.pending(w.sp) == []
    with pytest.raises(ValueError):
        proposals.accept(w.sp, "sp_0001", actor=HUMAN)


def test_an_agent_may_schedule_an_approved_post_that_has_no_slot_yet(tmp_path):
    w = make_world(tmp_path)
    pid, vid = w.add_post()
    w.approve(pid, vid)
    out = w.studio.move(pid, vid, {"local": "2026-10-23T09:00", "tz": DENVER}, expected_rev=store.load_post(w.sp, pid)["rev"], actor=AGENT)
    assert out["applied"] is True and out["post"]["variants"][0]["status"] == "scheduled"


def test_published_and_publishing_variants_cannot_be_moved(tmp_path):
    w = make_world(tmp_path)
    pid, vid = w.add_post(at="2026-10-21T15:00:00Z")
    item = w.item(pid, vid)
    item["status"] = "published"
    pq.save(w.sp, item)
    with pytest.raises(MoveRefused) as c:
        w.studio.move(pid, vid, {"local": "2026-10-23T09:00", "tz": DENVER}, expected_rev=store.load_post(w.sp, pid)["rev"], actor=HUMAN)
    assert c.value.code == "locked"
    with pytest.raises(MoveRefused):
        w.studio.unschedule(pid, vid, expected_rev=store.load_post(w.sp, pid)["rev"], actor=HUMAN)


def test_a_locked_card_only_moves_for_a_human(tmp_path):
    w = make_world(tmp_path)
    pid, vid = w.add_post(at="2026-10-21T15:00:00Z", sync=False)
    post = store.load_post(w.sp, pid)
    post["variants"][0]["schedule"]["locked"] = True
    store.save_post(w.sp, post, expected_rev=post["rev"], actor=HUMAN, op="lock")
    rev = store.load_post(w.sp, pid)["rev"]
    with pytest.raises(MoveRefused) as c:
        w.studio.move(pid, vid, {"local": "2026-10-23T09:00", "tz": DENVER}, expected_rev=rev, actor=AGENT)
    assert c.value.code == "locked"
    assert w.studio.move(pid, vid, {"local": "2026-10-23T09:00", "tz": DENVER}, expected_rev=rev, actor=HUMAN)["applied"]


def test_unschedule_returns_to_the_pool_and_keeps_the_approval(tmp_path):
    w = make_world(tmp_path)
    pid, vid = w.add_post(at="2026-10-21T15:00:00Z")
    w.approve(pid, vid)
    out = w.studio.unschedule(pid, vid, expected_rev=store.load_post(w.sp, pid)["rev"], actor=HUMAN)
    ref = json.loads((API / "POST_unschedule.json").read_text())["response"]
    assert set(out) >= set(ref)
    v = out["post"]["variants"][0]
    assert v["schedule"] is None and v["status"] == "approved" and v["status_reason"] == "approved, no slot (pool)"
    item = w.item(pid, vid)
    assert item["status"] == "approved" and item["schedule"] is None and pq.send_blockers(w.sp, item) == []
    assert w.events("unschedule")


# ------------------------------------------------------------------------------------------------ swap

def test_swap_two_variants_in_different_posts(tmp_path):
    w = make_world(tmp_path)
    p1, v1 = w.add_post(at="2026-10-21T15:00:00Z", sync=False)
    p2, v2 = w.add_post(at="2026-10-23T15:00:00Z", sync=False)
    r1, r2 = store.load_post(w.sp, p1)["rev"], store.load_post(w.sp, p2)["rev"]
    out = w.studio.swap(p1, v1, v2, expected_rev=r1, actor=HUMAN)
    ref = json.loads((API / "POST_swap_ok.json").read_text())["response"]
    assert set(out) >= set(ref)
    assert out["post"]["variants"][0]["schedule"]["utc"] == "2026-10-23T15:00:00Z" and out["other"]["variants"][0]["schedule"]["utc"] == "2026-10-21T15:00:00Z"
    assert out["post"]["rev"] == r1 + 1 and out["other"]["rev"] == r2 + 1
    assert out["post"]["variants"][0]["schedule"]["set_by"] == "human"
    assert w.variant(p2, v2)["schedule"]["utc"] == "2026-10-21T15:00:00Z"


def test_swap_within_one_post_and_with_an_unscheduled_variant(tmp_path):
    w = make_world(tmp_path)
    p1, v1 = w.add_post(at="2026-10-21T15:00:00Z", sync=False)
    p2, v2 = w.add_post(sync=False)
    out = slots.swap(w.sp, p1, v1, f"{p2}:{v2}", expected_rev=store.load_post(w.sp, p1)["rev"], actor=HUMAN, now=w.clock.now())
    assert out["post"]["variants"][0]["schedule"] is None and out["other"]["variants"][0]["schedule"]["utc"] == "2026-10-21T15:00:00Z"
    post = store.load_post(w.sp, p1)
    first = post["variants"][0]["id"]
    post["variants"].append({**post["variants"][0], "id": "v_second", "post_type": "carousel", "schedule": slots.make_schedule(
        resolve_local("2026-10-23T09:00", DENVER), slot="r1", set_by="agent")})
    saved = store.save_post(w.sp, post, expected_rev=post["rev"], actor=AGENT, op="t")
    same = slots.swap(w.sp, p1, first, "v_second", expected_rev=saved["rev"], actor=HUMAN, now=w.clock.now())
    assert same["other"] is None and same["post"]["variants"][0]["schedule"]["utc"] == "2026-10-23T15:00:00Z"
    assert same["post"]["variants"][1]["schedule"] is None
    with pytest.raises(ValueError):
        slots.swap(w.sp, p1, first, first, expected_rev=same["post"]["rev"], actor=HUMAN, now=w.clock.now())


def test_swap_validates_before_writing_anything(tmp_path):
    w = make_world(tmp_path, accounts=(("ig-main", "instagram"), ("fb-page", "facebook")))
    p1, v1 = w.add_post(at="2026-10-21T15:00:00Z", account="ig-main", sync=False)
    p2, v2 = w.add_post(at="2026-10-23T15:00:00Z", account="fb-page", sync=False)
    rev = store.load_post(w.sp, p1)["rev"]
    with pytest.raises(MoveRefused) as c:
        w.studio.swap(p1, v1, f"{p2}:{v2}", expected_rev=rev, actor=HUMAN)
    assert c.value.code == "conflict.account"
    p3, v3 = w.add_post(at="2026-10-26T15:00:00Z", account="ig-main", sync=False)
    with pytest.raises(MoveRefused) as c:
        w.studio.swap(p1, v1, f"{p3}:{v3}", expected_rev=rev - 1, actor=HUMAN)
    assert c.value.code == "conflict.rev"
    assert w.variant(p1, v1)["schedule"]["utc"] == "2026-10-21T15:00:00Z" and w.variant(p3, v3)["schedule"]["utc"] == "2026-10-26T15:00:00Z"


def test_swap_rolls_back_when_the_second_write_loses_a_race(tmp_path, monkeypatch):
    w = make_world(tmp_path)
    p1, v1 = w.add_post(at="2026-10-21T15:00:00Z", sync=False)
    p2, v2 = w.add_post(at="2026-10-23T15:00:00Z", sync=False)
    real = store.save_post
    state_ = {"n": 0}

    def flaky(sp, post, **kw):
        state_["n"] += 1
        if state_["n"] == 2:                             # the write of post 1 (the second one) is the one that loses
            raise __import__("ave.studio.model", fromlist=["RevConflict"]).RevConflict(post, "raced")
        return real(sp, post, **kw)

    monkeypatch.setattr(store, "save_post", flaky)
    with pytest.raises(MoveRefused):
        slots.swap(w.sp, p1, v1, f"{p2}:{v2}", expected_rev=store.load_post(w.sp, p1)["rev"], actor=HUMAN, now=w.clock.now())
    monkeypatch.setattr(store, "save_post", real)
    assert w.variant(p1, v1)["schedule"]["utc"] == "2026-10-21T15:00:00Z"
    assert w.variant(p2, v2)["schedule"]["utc"] == "2026-10-23T15:00:00Z"                  # rolled back


def test_an_agent_swap_involving_an_approved_post_is_a_proposal(tmp_path):
    w = make_world(tmp_path)
    p1, v1 = w.add_post(at="2026-10-21T15:00:00Z")
    p2, v2 = w.add_post(at="2026-10-23T15:00:00Z")
    w.approve(p1, v1)
    out = w.studio.swap(p1, v1, f"{p2}:{v2}", expected_rev=store.load_post(w.sp, p1)["rev"], actor=AGENT)
    assert out["applied"] is False and out["proposal"] == "sp_0001" and len(proposals.pending(w.sp)[0]["moves"]) == 2
    assert w.variant(p1, v1)["schedule"]["utc"] == "2026-10-21T15:00:00Z"


# ------------------------------------------------------------------------------------------------ hooks

def test_register_provides_exactly_the_documented_hooks(tmp_path):
    from ave.schedule import HOOKS_PROVIDED
    w = make_world(tmp_path)
    assert all(w.studio.has(h) for h in HOOKS_PROVIDED)
    bare = Studio(w.sp, clock=w.clock)
    with pytest.raises(NotImplementedYet):
        bare.calendar("2026-10-19", "2026-10-31")


def test_pause_is_open_to_anyone_resume_needs_a_human(tmp_path, monkeypatch):
    w = make_world(tmp_path)
    assert w.studio.pause() == {"paused": True, "scope": "studio"} and w.sp.pause_path.exists()
    assert w.studio.pause("ig-main") == {"paused": True, "scope": "ig-main"} and (w.sp.accounts_dir / "ig-main.paused").exists()
    with pytest.raises(SystemExit) as e:
        w.studio.resume()
    assert "human act" in str(e.value) and w.sp.pause_path.exists()
    monkeypatch.setattr("ave.humanpresence.is_human_terminal", lambda: True)
    assert w.studio.resume("ig-main", by="maren") == {"paused": True, "scope": "ig-main"} and not (w.sp.accounts_dir / "ig-main.paused").exists()
    assert w.studio.resume(by="maren")["paused"] is False and not w.sp.pause_path.exists()
    assert [e["op"] for e in w.events() if e["op"] in ("pause", "resume")] == ["pause", "pause", "resume", "resume"]


def test_resume_with_a_verified_pin_does_not_need_a_tty(tmp_path):
    w = make_world(tmp_path)
    w.studio.pause()
    assert w.studio.resume(by="maren", pin_verified=True)["paused"] is False
