"""WS3: post -> queue sync. It NEVER approves; it demotes an approved item only when bytes or bound metadata change, never for a retime
(spec 2.8, 3.3 E1/E2, 6.3)."""

import json
import random
from datetime import timedelta

import pytest
from sched_helpers import make_world, no_agent_markers, utc  # noqa: F401
from studio_helpers import add_asset, make_episode_with_queue, make_png

from ave.publish import queue as pq
from ave.studio import store
from ave.studio.events import AGENT, HUMAN
from ave.studio.sync import sync_queue


def sync(w, pid, **kw):
    return sync_queue(w.sp, pid, publishers=w.pubs, now=w.clock.now(), **kw)


def edit(w, pid, patch, actor=AGENT):
    return w.studio.edit_post(pid, patch, expected_rev=store.load_post(w.sp, pid)["rev"], actor=actor)


def add_registry_line(w, rec):
    with open(w.sp.library_dir / "assets.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")


def test_sync_creates_one_item_per_variant_bound_to_its_account(tmp_path):
    w = make_world(tmp_path)
    pid, vid = w.add_post("Day 3: the starter smells like pears.", at="2026-10-21T15:00:00Z", sync=False)
    rep = sync(w, pid)
    assert rep["variants"][0]["action"] == "created" and rep["variants"][0]["queue_status"] == "awaiting_approval" and rep["problems"] == []
    v = w.variant(pid, vid)
    item = pq.load(w.sp, v["queue"]["id"])
    assert v["queue"]["root"] == "." and v["queue_sha"] == item["sha256"]
    assert (item["meta_v"], item["account"], item["post_type"], item["platforms"], item["media_type"]) == (2, "ig-main", "feed", ["instagram"], "image")
    assert item["caption"] == "Day 3: the starter smells like pears." and item["title"] == "Day 3: the starter smells like pears."
    assert item["schedule"] == "2026-10-21T15:00:00+00:00" and item["ai_disclosure"] is False and item["approvals"] == []
    assert item["alt_texts"] == ["A loaf of bread on a table."] and item["visibility"] == "public"


def test_sync_is_idempotent(tmp_path):
    w = make_world(tmp_path)
    pid, vid = w.add_post(at="2026-10-21T15:00:00Z", sync=False)
    sync(w, pid)
    qid, rev = w.variant(pid, vid)["queue"]["id"], store.load_post(w.sp, pid)["rev"]
    for _ in range(3):
        rep = sync(w, pid)
        assert rep["variants"][0]["action"] == "unchanged" and rep["variants"][0]["queue"] == qid
    assert store.load_post(w.sp, pid)["rev"] == rev and len(pq.list_items(w.sp)) == 1


def test_sync_carries_copy_extras_and_carousels(tmp_path):
    w = make_world(tmp_path)
    a1, a2 = w.asset("First slide of the loaf."), w.asset("Second slide of the crumb.")
    post = w.studio.create_post(copy={"base": "A two-slide post.", "link": "https://acme.example/starter", "first_comment": "Recipe in bio",
                                      "hashtags": ["sourdough", "baking"]},
                                media=[{"slot": 1, "asset": a2["id"], "role": "image", "alt": "Second slide of the crumb."},
                                       {"slot": 0, "asset": a1["id"], "role": "image", "alt": "First slide of the loaf."}])
    post = w.studio.add_variant(post["id"], account="ig-main", platform="instagram", post_type="carousel", expected_rev=post["rev"])
    sync(w, post["id"])
    item = w.item(post["id"], post["variants"][0]["id"])
    assert item["media_type"] == "carousel" and item["link_url"] == "https://acme.example/starter" and item["first_comment"] == "Recipe in bio"
    assert item["tags"] == ["sourdough", "baking"] and item["alt_texts"] == ["First slide of the loaf.", "Second slide of the crumb."]
    assert [f.split("/")[-1] for f in item["files"]] == [f"{a1['sha256']}.png", f"{a2['sha256']}.png"]     # slot order, not list order


@pytest.mark.parametrize("patch", [
    {"copy.base": "A different caption."},
    {"copy.hashtags": ["new"]},
    {"copy.first_comment": "Added a first comment"},
    {"copy.link": "https://acme.example/other"},
    {"media.0.alt": "A new description of the same picture."},
    {"variants.VID.copy.caption": "A variant specific caption."},
], ids=["caption", "hashtags", "first_comment", "link", "alt_text", "variant_caption"])
def test_a_copy_change_demotes_an_approved_item(tmp_path, patch):
    w = make_world(tmp_path)
    pid, vid = w.add_post(at="2026-10-21T15:00:00Z")
    w.approve(pid, vid)
    assert w.item(pid, vid)["status"] == "approved"
    edit(w, pid, {k.replace("VID", vid): v for k, v in patch.items()})
    rep = sync(w, pid)["variants"][0]
    item = w.item(pid, vid)
    assert rep["action"] == "updated" and rep["invalidated"] is True and item["status"] == "awaiting_approval"
    assert [e["event"] for e in item["events"]][-2:] == ["approval_invalidated", "updated"]
    assert w.events("approval_invalidated")[-1]["variant"] == vid and pq.send_blockers(w.sp, item)
    assert sync(w, pid)["variants"][0]["action"] == "unchanged"                      # stays at awaiting_approval; nothing re-approves


def test_a_changed_picture_demotes_and_a_new_hash_is_recorded(tmp_path):
    w = make_world(tmp_path)
    pid, vid = w.add_post(at="2026-10-21T15:00:00Z")
    w.approve(pid, vid)
    old_sha = w.variant(pid, vid)["queue_sha"]
    new_asset = w.asset("A loaf of bread on a table.")
    edit(w, pid, {"media.0.asset": new_asset["id"]})
    rep = sync(w, pid)["variants"][0]
    assert rep["invalidated"] and w.item(pid, vid)["status"] == "awaiting_approval"
    assert w.variant(pid, vid)["queue_sha"] == new_asset["sha256"] != old_sha


def test_a_pure_retime_does_not_demote_and_keeps_the_signature_valid(tmp_path):
    w = make_world(tmp_path)
    pid, vid = w.add_post(at="2026-10-21T15:00:00Z")
    approved = w.approve(pid, vid)
    w.set_schedule(pid, vid, utc("2026-10-23T15:00:00Z"))
    rep = sync(w, pid)["variants"][0]
    item = w.item(pid, vid)
    assert rep["action"] == "updated" and rep["invalidated"] is False and item["status"] == "approved"
    assert item["schedule"] == "2026-10-23T15:00:00+00:00" and item["approvals"] == approved["approvals"] and pq.send_blockers(w.sp, item) == []
    assert w.events("approval_invalidated") == []


def test_sync_never_approves_in_any_sequence_of_edits(tmp_path):
    """Property: after any random mix of edits, retimes and syncs, the only approvals are the ones the test made as the human, and an
    approved item always holds a valid approval for its current bytes and bound metadata."""
    w = make_world(tmp_path)
    rng = random.Random(7)
    pid, vid = w.add_post(at="2026-10-21T15:00:00Z")
    human_approvals = 0
    for step in range(40):
        op = rng.choice(["caption", "retime", "sync", "sync", "approve", "alt"])
        if op == "caption":
            edit(w, pid, {"copy.base": f"caption {step}"})
        elif op == "alt":
            edit(w, pid, {"media.0.alt": f"picture number {step} of bread"})
        elif op == "retime":
            w.set_schedule(pid, vid, utc("2026-10-21T15:00:00Z") + timedelta(days=rng.randint(1, 20)))
        elif op == "approve":
            sync(w, pid)
            if w.item(pid, vid)["status"] == "awaiting_approval":
                w.approve(pid, vid)
                human_approvals += 1
        else:
            sync(w, pid)
        item = w.item(pid, vid)
        assert len(item["approvals"]) == human_approvals
        if item["status"] == "approved":
            assert item["approvals"] and pq.send_blockers(w.sp, item) == [], f"step {step}: an approved item must hold a valid approval"
        if op in ("caption", "alt"):
            sync(w, pid)
            assert w.item(pid, vid)["status"] != "approved", "bound copy changed: nothing may stay approved"
            assert len(w.item(pid, vid)["approvals"]) == human_approvals


def test_sync_module_has_no_path_to_approve():
    import inspect

    from ave.studio import sync as mod
    src = inspect.getsource(mod)
    assert "pq.approve" not in src and "make_approval" not in src and "sign_approval" not in src


def test_an_unlicensed_asset_blocks_the_queue_item(tmp_path):
    w = make_world(tmp_path)
    pid, vid = w.add_post("No license yet.", at="2026-10-21T15:00:00Z", licensed=False)
    rep = sync(w, pid)["variants"][0]
    assert rep["action"] == "blocked" and "license entry" in rep["problems"][0]
    assert w.variant(pid, vid)["queue"] is None and "license_gap" in w.variant(pid, vid)["flags"] and pq.list_items(w.sp) == []
    rec = store.read_assets(w.sp)[store.load_post(w.sp, pid)["media"][0]["asset"]]
    w.license_it(rec)
    assert sync(w, pid)["variants"][0]["action"] == "created"
    assert "license_gap" not in w.variant(pid, vid)["flags"]


def test_license_closure_covers_a_render_through_its_inputs(tmp_path):
    w = make_world(tmp_path)
    stock = w.asset("A stock photo of flour.", licensed=False)
    render = add_asset(w.sp, make_png(w.sp.work / "gen" / "render.png", color=(1, 2, 3)), alt="A quote card on flour.", derived_from=[stock["id"]])
    add_registry_line(w, {**render, "license": {**render["license"], "entry": "a" * 64}})      # the render has an entry; its input does not
    post = w.studio.create_post(copy={"base": "Quote card."}, media=[{"slot": 0, "asset": render["id"], "role": "image", "alt": "A quote card on flour."}])
    post = w.studio.add_variant(post["id"], account="ig-main", platform="instagram", post_type="feed", expected_rev=post["rev"])
    rep = sync(w, post["id"])["variants"][0]
    assert rep["action"] == "blocked" and stock["id"] in rep["problems"][0]


def test_missing_alt_and_unknown_assets_blocked_text_has_its_own_shape(tmp_path):
    w = make_world(tmp_path)
    pid, vid = w.add_post(alt="", sync=False)
    assert "no alt text" in sync(w, pid)["variants"][0]["problems"][0]
    p2 = w.studio.create_post(copy={"base": "words only"})
    p2 = w.studio.add_variant(p2["id"], account="ig-main", platform="instagram", post_type="feed", expected_rev=p2["rev"])
    # The injected publisher supports text; actual adapter eligibility is tested separately.
    assert not sync(w,p2["id"])["problems"]
    assert w.item(p2["id"],p2["variants"][0]["id"])["media_type"]=="text"
    p3 = w.studio.create_post(copy={"base": "ghost"}, media=[{"slot": 0, "asset": "a_000000000000", "role": "image", "alt": "x"}])
    p3 = w.studio.add_variant(p3["id"], account="ig-main", platform="instagram", post_type="feed", expected_rev=p3["rev"])
    assert "not in the library registry" in sync(w, p3["id"])["variants"][0]["problems"][0]
    assert len(pq.list_items(w.sp)) == 1


def test_ai_generated_assets_force_the_disclosure_flag(tmp_path):
    w = make_world(tmp_path)
    ai = add_asset(w.sp, make_png(w.sp.work / "gen" / "ai.png", color=(9, 9, 9)), alt="A generated loaf.", ai=True)
    w.license_it(ai)
    post = w.studio.create_post(copy={"base": "Generated."}, media=[{"slot": 0, "asset": ai["id"], "role": "image", "alt": "A generated loaf."}])
    post = w.studio.add_variant(post["id"], account="ig-main", platform="instagram", post_type="feed", expected_rev=post["rev"])
    sync(w, post["id"])
    assert w.item(post["id"], post["variants"][0]["id"])["ai_disclosure"] is True


def test_adopted_and_sent_items_are_left_alone(tmp_path):
    w = make_world(tmp_path)
    ep, item = make_episode_with_queue(tmp_path, approve=True)
    pid, vid = w.add_post(sync=False)
    post = store.load_post(w.sp, pid)
    post["variants"][0]["queue"] = {"root": str(ep.root), "id": item["id"]}
    store.save_post(w.sp, post, expected_rev=post["rev"], actor=AGENT, op="adopt")
    edit(w, pid, {"copy.base": "changed after adoption"})
    rep = sync(w, pid)["variants"][0]
    # the episode's item is never rewritten, and the studio no longer sends it: the variant gets its OWN studio item (meta_v 2, account-bound),
    # which needs a fresh approval
    assert rep["action"] == "created" and pq.load(ep, item["id"]) == item
    own = w.item(pid, vid)
    assert own["id"] != item["id"] and own["meta_v"] == 2 and own["account"] == "ig-main" and own["status"] == "awaiting_approval"
    pid2, vid2 = w.add_post(at="2026-10-21T15:00:00Z")
    done = w.item(pid2, vid2)
    done["status"] = "published"
    pq.save(w.sp, done)
    with pytest.raises(ValueError, match="locked"):
        edit(w, pid2, {"copy.base": "too late"})
    assert sync(w, pid2)["variants"][0]["action"] == "skipped" and w.item(pid2, vid2)["caption"] != "too late"


def test_a_rejected_item_is_replaced_by_a_fresh_one(tmp_path):
    w = make_world(tmp_path)
    pid, vid = w.add_post(at="2026-10-21T15:00:00Z")
    old = w.item(pid, vid)["id"]
    pq.reject(w.sp, old, "maren", "reworking")
    rep = sync(w, pid)["variants"][0]
    assert rep["action"] == "created" and rep["queue"] != old and w.variant(pid, vid)["queue"]["id"] == rep["queue"]
    assert pq.load(w.sp, old)["status"] == "rejected"


def test_dropped_variants_are_ignored(tmp_path):
    w = make_world(tmp_path)
    pid, vid = w.add_post(sync=False)
    post = store.load_post(w.sp, pid)
    post["variants"][0]["dropped"] = True
    store.save_post(w.sp, post, expected_rev=post["rev"], actor=HUMAN, op="drop")
    assert sync(w, pid)["variants"] == [] and pq.list_items(w.sp) == []


def test_write_back_survives_a_concurrent_edit(tmp_path, monkeypatch):
    w = make_world(tmp_path)
    pid, vid = w.add_post(sync=False)
    real = store.save_post
    hits = {"n": 0}

    def racing(sp, post, **kw):
        if kw.get("op") == "queue_sync" and hits["n"] == 0:
            hits["n"] += 1
            cur = store.load_post(sp, pid)
            real(sp, {**cur, "copy": {**cur["copy"], "link": "https://x.example"}}, expected_rev=cur["rev"], actor=HUMAN, op="edit")   # the human edits mid-sync
        return real(sp, post, **kw)

    monkeypatch.setattr(store, "save_post", racing)
    sync(w, pid)
    monkeypatch.setattr(store, "save_post", real)
    post = store.load_post(w.sp, pid)
    assert post["variants"][0]["queue"] is not None and post["copy"]["link"] == "https://x.example"      # both survive
