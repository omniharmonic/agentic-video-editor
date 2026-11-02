"""Post store: compare-and-swap on rev (RevConflict carries the latest), events on every write, concurrency, filters, patches."""

import json
import threading

import pytest

from ave.studio import events, model, store
from ave.studio.events import AGENT, HUMAN, Actor
from ave.studio.model import RevConflict


@pytest.fixture
def sp(bare):
    return bare


def fresh(sp, pid="p_t0001", **kw):
    return store.save_post(sp, model.new_post(idea="An idea", post_id=pid, **kw), expected_rev=None, actor=AGENT, op="create")


def test_create_is_rev_1_and_refuses_to_overwrite(sp):
    p = fresh(sp)
    assert p["rev"] == 1 and p["stage"] == "idea"
    with pytest.raises(RevConflict) as e:
        store.save_post(sp, model.new_post(idea="again", post_id="p_t0001"), expected_rev=None, actor=AGENT, op="create")
    assert e.value.latest["rev"] == 1 and store.load_post(sp, "p_t0001")["idea"]["text"] == "An idea"


def test_save_increments_rev_and_stale_write_conflicts_with_latest(sp):
    p = fresh(sp)
    p["copy"]["base"] = "agent copy"
    p2 = store.save_post(sp, p, expected_rev=1, actor=AGENT, op="edit")
    assert p2["rev"] == 2 and p2["stage"] == "draft"
    mine = json.loads(json.dumps(p))
    mine["copy"]["base"] = "human copy"
    with pytest.raises(RevConflict) as e:
        store.save_post(sp, mine, expected_rev=1, actor=HUMAN, op="edit")          # the human started from rev 1
    assert e.value.latest["rev"] == 2 and e.value.latest["copy"]["base"] == "agent copy"
    assert store.load_post(sp, "p_t0001")["copy"]["base"] == "agent copy"             # nothing was overwritten
    assert store.save_post(sp, mine, expected_rev=2, actor=HUMAN, op="edit")["rev"] == 3


def test_updating_a_missing_post_conflicts(sp):
    with pytest.raises(RevConflict) as e:
        store.save_post(sp, model.new_post(idea="x", post_id="p_ghost"), expected_rev=3, actor=AGENT, op="edit")
    assert e.value.latest is None


def test_two_racing_writers_exactly_one_wins(sp):
    fresh(sp)
    results, barrier = [], threading.Barrier(8)

    def worker(i):
        p = store.load_post(sp, "p_t0001")
        p["copy"]["base"] = f"writer {i}"
        barrier.wait()
        try:
            store.save_post(sp, p, expected_rev=1, actor=Actor("agent", f"w{i}"), op="edit")
            results.append("ok")
        except RevConflict:
            results.append("conflict")

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count("ok") == 1 and results.count("conflict") == 7
    assert store.load_post(sp, "p_t0001")["rev"] == 2


def test_every_write_appends_an_event_with_actor_op_rev_and_shas(sp, clock):
    p = store.save_post(sp, model.new_post(idea="x", post_id="p_ev001"), expected_rev=None, actor=AGENT, op="create", now=clock.now())
    p["copy"]["base"] = "hello"
    p2 = store.save_post(sp, p, expected_rev=1, actor=HUMAN, op="edit", detail={"paths": ["copy.base"]}, now=clock.now())
    rows = events.changes(sp, "ev:0")["events"]
    assert [(r["op"], r["rev"], r["actor"]["kind"]) for r in rows] == [("create", 1, "agent"), ("edit", 2, "human")]
    assert rows[0]["before_sha"] is None and rows[1]["before_sha"] == rows[0]["after_sha"]
    assert rows[1]["after_sha"] == store.post_sha(p2) and rows[1]["detail"] == {"paths": ["copy.base"]}
    assert rows[0]["ts"] == "2026-10-19T16:00:00Z"


def test_event_detail_is_redacted(sp):
    store.append_event(sp, events.make_event(actor=AGENT, op="x", detail={"note": "token=abcdef123456", "access_token": "hunter2xyz"}))
    detail = events.changes(sp, "ev:0")["events"][0]["detail"]
    assert "abcdef123456" not in json.dumps(detail) and "hunter2xyz" not in json.dumps(detail)


def test_change_feed_cursor_only_returns_new_events(sp):
    assert events.changes(sp)["events"] == []
    c0 = events.changes(sp)["cursor"]
    fresh(sp)
    feed = events.changes(sp, c0)
    assert [e["op"] for e in feed["events"]] == ["create"]
    assert events.changes(sp, feed["cursor"])["events"] == []
    fresh(sp, "p_t0002")
    assert [e["post"] for e in events.changes(sp, feed["cursor"])["events"]] == ["p_t0002"]
    with pytest.raises(ValueError):
        events.changes(sp, "garbage")


def test_stage_is_derived_on_save_and_dropped_is_sticky(sp):
    p = fresh(sp)
    assert p["stage"] == "idea"
    p["media"] = [{"slot": 0, "asset": "a_0123456789ab", "role": "image"}]
    p = store.save_post(sp, p, expected_rev=1, actor=AGENT, op="attach")
    assert p["stage"] == "draft"
    p["variants"].append(model.new_variant(account="ig-main", platform="instagram", post_type="feed", variant_id="v_a"))
    p = store.save_post(sp, p, expected_rev=2, actor=AGENT, op="variant_add")
    assert p["stage"] == "active"
    p["variants"][0]["dropped"] = True
    p = store.save_post(sp, p, expected_rev=3, actor=AGENT, op="drop")
    assert p["stage"] == "archived"
    p["stage"] = "dropped"
    p = store.save_post(sp, p, expected_rev=4, actor=AGENT, op="drop")
    p["variants"][0]["dropped"] = False
    assert store.save_post(sp, p, expected_rev=5, actor=AGENT, op="x")["stage"] == "dropped"


def test_path_traversal_ids_are_refused(sp):
    for bad in ("../x", "a/b", "", ".hidden", "p..x"):
        with pytest.raises(ValueError):
            store.post_path(sp, bad)


def test_list_posts_filters(built):
    sp = built.sp
    assert len(store.list_posts(sp)) == 12
    assert {p["stage"] for p in store.list_posts(sp)} == {"idea", "draft", "active"}
    assert all(p["stage"] == "active" for p in store.list_posts(sp, stage="active"))
    assert store.list_posts(sp, account="fb-page") and all(any(v["account"] == "fb-page" for v in p["variants"]) for p in store.list_posts(sp, account="fb-page"))
    assert all(any(v["platform"] == "facebook" for v in p["variants"]) for p in store.list_posts(sp, platform="facebook"))
    assert len(store.list_posts(sp, q="IDEA NUMBER 0")) == 1
    assert store.list_posts(sp, campaign="c_none") == []


def test_apply_patch_rules(built):
    post = store.load_post(built.sp, built.posts["scheduled"])
    vid = post["variants"][0]["id"]
    out = model.apply_patch(post, {"copy.base": "new base", f"variants.{vid}.copy.caption": "only here", "media.0.alt": "new alt",
                                   f"variants.{vid}.copy.hashtags": None})
    assert out["copy"]["base"] == "new base" and out["variants"][0]["copy"]["caption"] == "only here"
    assert out["media"][0]["alt"] == "new alt" and post["copy"]["base"] != "new base"          # input untouched
    for bad in ({"rev": 99}, {"id": "p_other"}, {f"variants.{vid}.queue": {"root": ".", "id": "x"}}, {f"variants.{vid}.schedule": None},
                {f"variants.{vid}.result": {}}, {f"variants.{vid}": {}}):
        with pytest.raises(ValueError):
            model.apply_patch(post, bad)
    with pytest.raises(KeyError):
        model.apply_patch(post, {"variants.v_nope.copy.caption": "x"})


def test_effective_copy_and_media_inherit(built):
    post = store.load_post(built.sp, built.posts["scheduled"])
    v = post["variants"][0]
    assert model.effective_copy(post, v)["caption"] == post["copy"]["base"]
    v["copy"]["caption"] = "own"
    v["media"] = None
    assert model.effective_copy(post, v)["caption"] == "own" and model.effective_media(post, v) == post["media"]


def test_ai_generated_follows_the_derived_from_closure():
    assets = {"a_1": {"ai_generated": False, "derived_from": ["a_2"]}, "a_2": {"ai_generated": False, "derived_from": ["a_3"]},
              "a_3": {"ai_generated": True, "derived_from": []}, "a_4": {"ai_generated": False, "derived_from": []}}
    assert model.ai_generated(assets, ["a_1"]) and not model.ai_generated(assets, ["a_4"]) and not model.ai_generated(assets, ["a_unknown"])
