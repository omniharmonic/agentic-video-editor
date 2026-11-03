"""Planner helpers for the UI: next free slots and bulk retime (src/ave/studio/bulk.py), plus the two endpoints that expose them
(`GET /api/studio/slots`, `POST /api/studio/bulk`) behind the same token / Host / Origin gate as every other route.

The rules that matter, each pinned here: a bulk call never approves, an approved post keeps its approval across a retime (the schedule is
not signed) and the move is recorded, one refused item does not stop the others, an undo (`set` with `if_utc`) never tramples a later
move, and an ACCOUNT change is refused when a queue item (so an approval) is bound to the old account."""

import http.client
import json
import threading

import pytest
from sched_helpers import make_world, no_agent_markers  # noqa: F401

from ave.publish import queue as pq
from ave.studio import store
from ave.studio.events import HUMAN
from ave.studio.ui.server import StudioServer
from studio_helpers import write_account

DENVER = "America/Denver"


def rev(w, pid):
    return store.load_post(w.sp, pid)["rev"]


def sched(w, pid, vid):
    return (w.variant(pid, vid).get("schedule") or {}).get("local")


# ------------------------------------------------------------------------------------------------ free slots

def test_free_slots_lists_the_next_open_rule_slots_and_skips_taken_ones(tmp_path):
    w = make_world(tmp_path)
    taken, _ = w.add_post(at="2026-10-21T15:00:00Z", sync=False)                 # Wed 21 Oct 09:00 is taken
    pid, vid = w.add_post(sync=False)
    out = w.studio.free_slots(pid, vid, count=3)
    assert out["has_rules"] and out["account"] == "ig-main" and out["tz"] == DENVER
    assert [s["local"] for s in out["slots"]] == ["2026-10-23T09:00", "2026-10-26T09:00", "2026-10-28T09:00"]
    assert out["slots"][0]["slot"] == "ig-main:2026-10-23T09:00" and out["slots"][0]["utc"] == "2026-10-23T15:00:00Z"
    assert taken not in (s["slot"] for s in out["slots"])


def test_free_slots_without_rules_is_empty_but_says_so(tmp_path):
    w = make_world(tmp_path, rules=False)
    pid, vid = w.add_post(sync=False)
    assert w.studio.free_slots(pid, vid) == {"account": "ig-main", "tz": DENVER, "has_rules": False, "slots": []}


# ------------------------------------------------------------------------------------------------ bulk

def test_shift_moves_every_item_by_n_days_keeping_the_wall_clock(tmp_path):
    w = make_world(tmp_path)
    a = w.add_post(at="2026-10-21T15:00:00Z", sync=False)
    b = w.add_post(at="2026-10-23T15:00:00Z", sync=False)
    out = w.studio.bulk("shift", [{"post": a[0], "variant": a[1]}, {"post": b[0], "variant": b[1]}], actor=HUMAN, days=7)
    assert not out["refused"] and out["approved"] == 0 and len(out["done"]) == 2
    assert sched(w, *a) == "2026-10-28T09:00" and sched(w, *b) == "2026-10-30T09:00"
    row = next(d for d in out["done"] if d["post"] == a[0])
    assert row["before"]["local"] == "2026-10-21T09:00" and row["after"]["local"] == "2026-10-28T09:00" and row["rev"] == rev(w, a[0])
    assert [e["op"] for e in w.events("move")] == ["move", "move"]


def test_shift_back_by_a_day_and_over_the_dst_change_keep_the_local_time(tmp_path):
    w = make_world(tmp_path)
    a = w.add_post(at="2026-10-30T15:00:00Z", sync=False)                       # Fri 30 Oct 09:00 MDT
    out = w.studio.bulk("shift", [{"post": a[0], "variant": a[1]}], actor=HUMAN, days=7)
    assert sched(w, *a) == "2026-11-06T09:00" and out["done"][0]["after"]["utc"] == "2026-11-06T16:00:00Z"     # MST now: the UTC hour moved


def test_one_refused_item_does_not_stop_the_others_and_says_why(tmp_path):
    w = make_world(tmp_path)
    a = w.add_post(at="2026-10-21T15:00:00Z", sync=False)
    blocker = w.add_post(at="2026-10-28T15:00:00Z", sync=False)                 # exactly where `a` would land
    c = w.add_post(at="2026-10-23T15:00:00Z", sync=False)
    out = w.studio.bulk("shift", [{"post": a[0], "variant": a[1]}, {"post": c[0], "variant": c[1]}], actor=HUMAN, days=7)
    assert [r["post"] for r in out["refused"]] == [a[0]] and out["refused"][0]["code"] == "conflict.slot" and blocker[0] in out["refused"][0]["reason"]
    assert [d["post"] for d in out["done"]] == [c[0]]
    assert sched(w, *a) == "2026-10-21T09:00" and sched(w, *c) == "2026-10-30T09:00"       # the refused one stayed where it was


def test_an_unscheduled_item_cannot_be_shifted(tmp_path):
    w = make_world(tmp_path)
    a = w.add_post(sync=False)
    out = w.studio.bulk("shift", [{"post": a[0], "variant": a[1]}], actor=HUMAN, days=1)
    assert out["refused"][0]["code"] == "noop" and "Unscheduled" in out["refused"][0]["reason"]


def test_a_retime_of_an_approved_post_keeps_the_approval_and_records_the_event(tmp_path):
    w = make_world(tmp_path)
    pid, vid = w.add_post(at="2026-10-21T15:00:00Z")
    w.approve(pid, vid)
    item_before = w.item(pid, vid)
    assert item_before["status"] == "approved"
    out = w.studio.bulk("shift", [{"post": pid, "variant": vid}], actor=HUMAN, days=7)
    assert out["approved"] == 0 and not out["refused"]
    item = w.item(pid, vid)
    assert item["status"] == "approved" and item["approvals"] == item_before["approvals"]          # the approval is untouched
    assert "moved_after_approval" in w.variant(pid, vid)["flags"] and out["done"][0]["status"] == "scheduled"
    ev = w.events("move")[-1]
    assert ev["actor"]["kind"] == "human" and ev["detail"]["from"] == "2026-10-21T15:00:00Z" and ev["detail"]["to"] == "2026-10-28T15:00:00Z"


def test_bulk_never_approves_anything(tmp_path):
    w = make_world(tmp_path)
    pid, vid = w.add_post(at="2026-10-21T15:00:00Z")
    assert w.item(pid, vid)["status"] == "awaiting_approval"
    for op, kw in (("shift", {"days": 7}), ("slots", {}), ("unschedule", {})):
        w.studio.bulk(op, [{"post": pid, "variant": vid}], actor=HUMAN, **kw)
        assert w.item(pid, vid)["status"] == "awaiting_approval" and not w.item(pid, vid).get("approvals")


def test_unschedule_keeps_an_approval_and_drops_the_time(tmp_path):
    w = make_world(tmp_path)
    pid, vid = w.add_post(at="2026-10-21T15:00:00Z")
    w.approve(pid, vid)
    out = w.studio.bulk("unschedule", [{"post": pid, "variant": vid}], actor=HUMAN)
    assert out["done"][0]["after"] is None and out["done"][0]["status"] == "approved"
    assert w.item(pid, vid)["status"] == "approved"
    again = w.studio.bulk("unschedule", [{"post": pid, "variant": vid}], actor=HUMAN)
    assert again["refused"][0]["code"] == "noop"


def test_slots_spreads_posts_over_the_next_free_slots_in_order(tmp_path):
    w = make_world(tmp_path)
    ids = [w.add_post(sync=False) for _ in range(3)]
    out = w.studio.bulk("slots", [{"post": p, "variant": v} for p, v in ids], actor=HUMAN)
    assert not out["refused"]
    assert sorted(sched(w, p, v) for p, v in ids) == ["2026-10-21T09:00", "2026-10-23T09:00", "2026-10-26T09:00"]


def test_a_scheduled_post_moves_on_to_the_slot_after_its_own(tmp_path):
    w = make_world(tmp_path)
    pid, vid = w.add_post(at="2026-10-21T15:00:00Z", sync=False)                       # Wed 21 Oct 09:00, already the next slot
    out = w.studio.bulk("slots", [{"post": pid, "variant": vid}], actor=HUMAN)
    assert not out["refused"] and sched(w, pid, vid) == "2026-10-23T09:00"             # not the same slot again
    again = w.studio.bulk("slots", [{"post": pid, "variant": vid}], actor=HUMAN)
    assert sched(w, pid, vid) == "2026-10-26T09:00" and again["done"][0]["before"]["local"] == "2026-10-23T09:00"


def test_set_restores_the_old_time_but_only_if_nothing_moved_it_since(tmp_path):
    w = make_world(tmp_path)
    pid, vid = w.add_post(at="2026-10-21T15:00:00Z", sync=False)
    moved = w.studio.bulk("shift", [{"post": pid, "variant": vid}], actor=HUMAN, days=7)["done"][0]
    undo = {"post": pid, "variant": vid, "to": {"local": moved["before"]["local"], "tz": moved["before"]["tz"]}, "if_utc": moved["after"]["utc"]}
    # someone moves it again first: the undo must not trample that
    w.studio.bulk("shift", [{"post": pid, "variant": vid}], actor=HUMAN, days=2)
    out = w.studio.bulk("set", [undo], actor=HUMAN)
    assert out["refused"][0]["code"] == "conflict.changed" and sched(w, pid, vid) == "2026-10-30T09:00"
    # untouched since: the undo restores it exactly
    w.studio.bulk("set", [{"post": pid, "variant": vid, "to": {"local": "2026-10-21T09:00", "tz": DENVER}, "if_utc": "2026-10-30T15:00:00Z"}], actor=HUMAN)
    assert sched(w, pid, vid) == "2026-10-21T09:00"


def test_set_with_null_is_an_unschedule_and_a_stale_expected_rev_is_refused(tmp_path):
    w = make_world(tmp_path)
    pid, vid = w.add_post(at="2026-10-21T15:00:00Z", sync=False)
    stale = w.studio.bulk("set", [{"post": pid, "variant": vid, "to": None, "expected_rev": rev(w, pid) - 1}], actor=HUMAN)
    assert stale["refused"][0]["code"] == "conflict.rev"
    ok = w.studio.bulk("set", [{"post": pid, "variant": vid, "to": None}], actor=HUMAN)
    assert ok["done"][0]["after"] is None


def test_published_posts_are_refused_not_moved(tmp_path):
    w = make_world(tmp_path)
    pid, vid = w.add_post(at="2026-10-21T15:00:00Z")
    item = w.item(pid, vid)
    item["status"] = "published"
    pq.save(w.sp, item)
    out = w.studio.bulk("shift", [{"post": pid, "variant": vid}], actor=HUMAN, days=7)
    assert out["refused"][0]["code"] == "locked" and sched(w, pid, vid) == "2026-10-21T09:00"


def test_moving_to_another_account_needs_the_same_platform_and_no_queue_item(tmp_path):
    w = make_world(tmp_path, accounts=(("ig-main", "instagram"), ("ig-alt", "instagram"), ("fb-page", "facebook")))
    draft = w.add_post(at="2026-10-21T15:00:00Z", sync=False)
    queued = w.add_post(at="2026-10-23T15:00:00Z")
    w.approve(*queued)
    bad_platform = w.studio.bulk("account", [{"post": draft[0], "variant": draft[1]}], actor=HUMAN, account="fb-page")
    assert bad_platform["refused"][0]["code"] == "conflict.account" and "facebook" in bad_platform["refused"][0]["reason"]
    bound = w.studio.bulk("account", [{"post": queued[0], "variant": queued[1]}], actor=HUMAN, account="ig-alt")
    assert bound["refused"][0]["code"] == "conflict.account" and "approval is bound" in bound["refused"][0]["reason"]
    assert w.variant(*queued)["account"] == "ig-main" and w.item(*queued)["status"] == "approved"      # the approval did not move
    ok = w.studio.bulk("account", [{"post": draft[0], "variant": draft[1]}], actor=HUMAN, account="ig-alt")
    assert not ok["refused"] and w.variant(*draft)["account"] == "ig-alt" and w.variant(*draft)["schedule"] is None
    missing = w.studio.bulk("account", [{"post": draft[0], "variant": draft[1]}], actor=HUMAN, account="nope")
    assert missing["refused"][0]["code"] == "conflict.account"


def test_malformed_bulk_requests_are_errors(tmp_path):
    from ave.studio.bulk import BulkError
    w = make_world(tmp_path)
    pid, vid = w.add_post(sync=False)
    item = [{"post": pid, "variant": vid}]
    for args, kw in ((("nope", item), {}), (("shift", item), {}), (("shift", item), {"days": 0}), (("shift", item), {"days": True}), (("shift", item), {"days": 9999}),
                     (("shift", []), {"days": 1}), (("account", item), {}), (("set", [{"post": "../x", "variant": vid}]), {}),
                     (("shift", [{"post": pid, "variant": vid}] * 101), {"days": 1})):
        with pytest.raises((BulkError, ValueError)):
            w.studio.bulk(*args, actor=HUMAN, **kw)
    junk = w.studio.bulk("set", [{"post": pid, "variant": vid, "to": 5}], actor=HUMAN)          # a bad item is reported, not a crash
    assert junk["refused"][0]["code"] == "refused" and not junk["done"]


# ------------------------------------------------------------------------------------------------ the HTTP endpoints

class Client:
    def __init__(self, srv):
        self.srv = srv

    def req(self, method, path, body=None, headers=None, token="header", host=None):
        h = {"Host": host or f"127.0.0.1:{self.srv.port}", **(headers or {})}
        if token == "header":
            h["X-AVE-Token"] = self.srv.token
        data = json.dumps(body).encode() if body is not None else None
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=10)
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in h.items():
            c.putheader(k, v)
        c.putheader("Content-Length", str(len(data) if data else 0))
        c.endheaders(data)
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, (json.loads(raw) if raw else None)


@pytest.fixture
def served(tmp_path):
    w = make_world(tmp_path)
    s = StudioServer(w.studio, port=0, by="tester", pin="424242")
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield w, Client(s)
    s.shutdown()
    s.server_close()


def test_the_slots_endpoint(served):
    w, cl = served
    pid, vid = w.add_post(sync=False)
    st, body = cl.req("GET", f"/api/studio/slots?post={pid}&variant={vid}&count=2")
    assert st == 200 and [s["local"] for s in body["slots"]] == ["2026-10-21T09:00", "2026-10-23T09:00"]
    assert cl.req("GET", f"/api/studio/slots?post={pid}")[0] == 400                      # variant is required
    assert cl.req("GET", "/api/studio/slots?post=p_nope&variant=v_x")[0] == 404
    assert cl.req("GET", f"/api/studio/slots?post=../x&variant={vid}")[0] == 400


def test_the_bulk_endpoint_does_the_work_and_reports_per_item(served):
    w, cl = served
    a = w.add_post(at="2026-10-21T15:00:00Z", sync=False)
    st, body = cl.req("POST", "/api/studio/bulk", {"op": "shift", "days": 7, "items": [{"post": a[0], "variant": a[1]}, {"post": "p_missing", "variant": "v_x"}]})
    assert st == 200 and body["approved"] == 0
    assert [d["post"] for d in body["done"]] == [a[0]] and body["done"][0]["after"]["local"] == "2026-10-28T09:00"
    assert body["refused"][0]["post"] == "p_missing" and body["refused"][0]["reason"]
    ev = w.events("move")[-1]
    assert ev["actor"]["kind"] == "human" and ev["actor"]["name"] == "tester"


def test_the_bulk_endpoint_rejects_bad_bodies_with_400(served):
    w, cl = served
    a = w.add_post(at="2026-10-21T15:00:00Z", sync=False)
    item = [{"post": a[0], "variant": a[1]}]
    for body in ({"op": "launch", "items": item}, {"op": "shift", "items": item}, {"op": "shift", "days": "7", "items": item}, {"op": "set", "items": []},
                 {"op": "set", "items": "x"}, {"op": "account", "items": item}, {"items": item}):
        st, out = cl.req("POST", "/api/studio/bulk", body)
        assert st == 400 and out["code"] == "bad_request", (body, st, out)
    assert sched(w, *a) == "2026-10-21T09:00"


def test_the_new_routes_go_through_the_same_token_host_and_origin_gate(served):
    w, cl = served
    a = w.add_post(at="2026-10-21T15:00:00Z", sync=False)
    body = {"op": "shift", "days": 7, "items": [{"post": a[0], "variant": a[1]}]}
    assert cl.req("POST", "/api/studio/bulk", body, token=None)[0] in (401, 403)
    assert cl.req("POST", "/api/studio/bulk", body, token="wrong")[0] in (401, 403)
    assert cl.req("POST", "/api/studio/bulk", body, host="evil.example:80")[0] in (400, 403)
    assert cl.req("POST", "/api/studio/bulk", body, headers={"Origin": "http://evil.example"})[0] == 403
    assert cl.req("GET", f"/api/studio/slots?post={a[0]}&variant={a[1]}", token=None)[0] in (401, 403)
    assert cl.req("GET", f"/api/studio/slots?post={a[0]}&variant={a[1]}", host="evil.example:80")[0] in (400, 403)
    assert sched(w, *a) == "2026-10-21T09:00"                                            # none of the refused calls moved anything


def test_no_pin_is_asked_for_or_accepted_as_a_way_to_approve(served):
    """Bulk is a planner act: it has no approval path at all, so a PIN in the body is simply ignored."""
    w, cl = served
    pid, vid = w.add_post(at="2026-10-21T15:00:00Z")
    st, body = cl.req("POST", "/api/studio/bulk", {"op": "shift", "days": 7, "pin": "424242", "approve": True, "items": [{"post": pid, "variant": vid}]})
    assert st == 200 and body["approved"] == 0 and w.item(pid, vid)["status"] == "awaiting_approval"


def test_posts_list_can_carry_thumbnails_for_review_and_ideas(served):
    w, cl = served
    pid, vid = w.add_post()
    st, body = cl.req("GET", "/api/studio/posts?thumbs=1")
    row = next(p for p in body["posts"] if p["id"] == pid)
    assert row["variants"][0]["thumb"] and row["variants"][0]["thumb"].startswith(("/files/library/assets/", "/api/studio/workbench/preview/"))
    st, plain = cl.req("GET", "/api/studio/posts")
    assert "thumb" not in next(p for p in plain["posts"] if p["id"] == pid)["variants"][0]


def test_the_review_detail_carries_a_served_url_and_a_name_for_every_queue_file(served):
    w, cl = served
    pid, vid = w.add_post()
    st, body = cl.req("GET", f"/api/studio/posts/{pid}")
    files = body["queue"][vid]["bound"]["files"]
    assert files and files[0]["name"] and files[0]["url"].startswith("/files/library/assets/") and files[0]["sha256"]
    assert body["queue"][vid]["mismatch"] == []


def test_calendar_cards_carry_what_the_filters_and_undo_need(served):
    w, cl = served
    a = w.add_post(at="2026-10-21T15:00:00Z", sync=False)
    write_account(w.sp, "ig-main", "instagram", brand="Acme Bakery")
    st, cal = cl.req("GET", "/api/studio/calendar?from=2026-10-19&to=2026-10-25")
    card = cal["days"]["2026-10-21"][0]
    assert card["tz"] == DENVER and "campaign" in card
    assert cal["accounts"][0]["brand"] == "Acme Bakery" and cal["accounts"][0]["label"]
    st, accts = cl.req("GET", "/api/studio/accounts")
    assert accts["accounts"][0]["brand"] == "Acme Bakery"
