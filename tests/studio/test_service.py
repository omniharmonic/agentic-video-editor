"""The Studio facade (post views, edit with human_edit capture, feedback flow, hooks) and FixtureStudio over the committed API shapes."""

import json
from pathlib import Path

import pytest

from ave.studio import ledger, model, store
from ave.studio.events import AGENT, HUMAN
from ave.studio.model import RevConflict
from ave.studio.service import FIXTURE_FOR, HOOKS, FixtureStudio, NotImplementedYet, Studio

API = Path(__file__).parent.parent / "data_studio" / "api"


def test_post_views_carry_derived_status_and_never_trust_a_cache(built, studio):
    views = studio.posts()
    assert len(views) == 12
    by_status = {}
    for p in views:
        for v in p["variants"]:
            by_status.setdefault(v["status"], 0)
            by_status[v["status"]] += 1
    assert set(by_status) == {"draft", "in_review", "scheduled", "approved"}
    assert [p["id"] for p in studio.posts(status="scheduled")] == [p["id"] for p in views if any(v["status"] == "scheduled" for v in p["variants"])]
    with pytest.raises(ValueError):
        studio.posts(status="bogus")
    sched = studio.post(built.posts["scheduled"])["variants"][0]
    assert sched["queue_status"] == "approved" and sched["approval"]["by"] == "maren"


def test_summary_counts(built, studio):
    s = studio.summary()
    assert s["title"] == "Acme Bakery" and s["posts"] == 12 and s["accounts"] == 3 and not s["paused"]
    assert s["stages"] == {"idea": 2, "draft": 2, "active": 8, "dropped": 0, "archived": 0}
    assert s["variants_by_status"] == {"draft": 2, "in_review": 2, "scheduled": 2, "approved": 2}
    built.sp.pause_path.write_text("")
    assert studio.summary()["paused"] and all(a["paused"] for a in studio.accounts())


def test_accounts_never_claim_live_without_a_verifier(studio):
    accts = studio.accounts()
    assert [a["id"] for a in accts] == ["fb-page", "ig-alt", "ig-main"] and not any(a["live"] for a in accts)
    assert all(set(a) >= {"id", "platform", "mode", "label", "tz", "post_types", "live", "paused"} for a in accts)


def test_create_edit_and_conflict_through_the_facade(bare, clock):
    s = Studio(bare, clock=clock)
    p = s.create_post(idea="Why we weigh flour", copy={"base": "We weigh the flour."})
    assert p["rev"] == 1 and p["stage"] == "draft" and p["id"].startswith("p_20261019_")
    e1 = s.edit_post(p["id"], {"copy.base": "We weigh the flour. Always."}, expected_rev=1, actor=AGENT)
    assert e1["rev"] == 2
    with pytest.raises(RevConflict) as c:
        s.edit_post(p["id"], {"copy.base": "stale"}, expected_rev=1, actor=HUMAN)
    assert c.value.latest["copy"]["base"] == "We weigh the flour. Always."
    with pytest.raises(ValueError):
        s.edit_post(p["id"], {"rev": 50}, expected_rev=2)


def test_human_copy_edits_record_before_after_agent_edits_do_not(bare, clock):
    s = Studio(bare, clock=clock)
    p = s.create_post(copy={"base": "Buy now!"})
    s.edit_post(p["id"], {"copy.base": "Machine rewrite"}, expected_rev=1, actor=AGENT)
    assert ledger.all_feedback(bare, now=clock.now()) == []
    s.edit_post(p["id"], {"copy.base": "Plainly: it is ready.", "copy.cta": ""}, expected_rev=2, actor=HUMAN)
    rows = ledger.all_feedback(bare, now=clock.now())
    assert len(rows) == 2 and {r["structured"]["copy"]["field"] for r in rows} == {"copy.base", "copy.cta"}
    edit = next(r for r in rows if r["structured"]["copy"]["field"] == "copy.base")
    assert edit["kind"] == "human_edit" and edit["structured"]["copy"]["before"] == "Machine rewrite"
    assert edit["structured"]["copy"]["after"] == "Plainly: it is ready." and edit["resolved"]["by"] == "system"
    assert s.feedback("open") == [] and len(s.feedback("resolved")) == len(rows)


def test_variant_add_and_duplicate_guard(bare, clock):
    s = Studio(bare, clock=clock)
    p = s.create_post(idea="x")
    v = s.add_variant(p["id"], account="ig-main", platform="instagram", post_type="feed", expected_rev=1)
    assert v["stage"] == "active" and v["variants"][0]["status"] == "draft"
    with pytest.raises(ValueError, match="already has"):
        s.add_variant(p["id"], account="ig-main", platform="instagram", post_type="feed", expected_rev=2)


def test_feedback_flow_send_back_claim_apply_resolve(built, studio):
    pid = built.posts["in_review"]
    post = studio.post(pid)
    vid = post["variants"][0]["id"]
    assert post["variants"][0]["status"] == "in_review"
    fb = studio.add_feedback(pid, "rework_copy", note="too salesy", variant=vid, by="maren", source="ui",
                             structured={"copy": {"field": "caption", "before": "a", "after": None}})
    assert studio.post(pid)["variants"][0]["status"] == "needs_rework"           # derived from the open feedback line
    claimed = studio.claim_next_feedback("claude-7")
    assert claimed["id"] == fb["id"] and claimed["claim"]["by"] == "claude-7" and claimed["post_view"]["id"] == pid
    assert studio.claim_next_feedback("claude-8") is None
    from ave.studio.events import AGENT
    with pytest.raises(ValueError, match="still at rev"):                      # a resolve with no change behind it is refused
        studio.resolve_feedback(fb["id"], "claude-7", rev=post["rev"] + 1, note="shortened")
    studio.edit_post(pid, {"copy.base": "Shorter and less salesy."}, expected_rev=post["rev"], actor=AGENT)
    studio.resolve_feedback(fb["id"], "claude-7", rev=post["rev"] + 1, note="shortened")
    assert studio.post(pid)["variants"][0]["status"] == "in_review"
    assert studio.feedback("open") == [] and studio.feedback("resolved")[0]["resolved"]["note"] == "shortened"
    kinds = [e["op"] for e in studio.changes("ev:0")["events"]]
    assert "feedback" in kinds
    with pytest.raises(KeyError):
        studio.add_feedback(pid, "note", variant="v_nope")


def test_hooks_are_stubs_until_registered(bare):
    s = Studio(bare)
    assert not s.has("calendar")
    with pytest.raises(NotImplementedYet, match="calendar"):
        s.calendar("2026-11-01", "2026-11-30")
    s.register("calendar", lambda studio, start, end, accounts=None: {"from": start, "to": end, "n": len(studio.accounts())})
    assert s.calendar("2026-11-01", "2026-11-30") == {"from": "2026-11-01", "to": "2026-11-30", "n": 0}
    with pytest.raises(ValueError):
        s.register("made_up", lambda *_: None)
    with pytest.raises(AttributeError):
        s.not_a_thing


def test_every_hook_has_a_fixture_or_a_documented_reason():
    # these hooks are internal call-throughs with no GET/POST shape of their own
    internal = {"sync_queue", "tick", "account_live"}
    assert set(HOOKS) - internal <= set(FIXTURE_FOR) | {"pause", "resume"}, set(HOOKS) - internal - set(FIXTURE_FOR)


# ---- fixture mode ---------------------------------------------------------------------------------------------

def test_fixture_studio_answers_with_the_committed_shapes():
    fx = FixtureStudio(API)
    assert fx.summary()["posts"] == 4 and fx.accounts()["accounts"][0]["id"] == "ig-main"
    cal = fx.calendar("2026-11-01", "2026-11-30")
    assert set(cal) == {"tz", "cursor", "accounts", "days", "slots", "unscheduled", "proposals"}
    assert cal["days"]["2026-11-02"][0]["status"] == "scheduled"
    assert fx.post("p_20261102_k3f9")["post"]["rev"] == 7
    assert fx.library()["assets"] and fx.move("p", "v", {})["post"]["rev"] == 8
    assert fx.fixture("POST_move_conflict")["status"] == 409


def test_every_fixture_file_is_well_formed_and_named_for_its_route():
    names = sorted(p.stem for p in API.glob("*.json"))
    assert set(FIXTURE_FOR.values()) <= set(names)
    for stem in names:
        f = json.loads((API / f"{stem}.json").read_text())
        assert {"method", "path", "status", "response"} <= set(f), stem
        assert f["method"] == stem.split("_", 1)[0] and f["path"].startswith("/api/studio/"), stem
        assert isinstance(f["response"], dict), stem
        if f["status"] >= 400:
            assert {"error", "code"} <= set(f["response"]), stem
        if f["method"] in ("POST", "PUT") and stem not in ("PUT_library_upload",):
            assert "request" in f, stem


def test_conflict_fixtures_carry_the_latest_post():
    for stem in ("POST_edit_conflict", "POST_move_conflict"):
        r = json.loads((API / f"{stem}.json").read_text())
        assert r["status"] == 409 and r["response"]["code"].startswith("conflict.") and r["response"]["latest"]["kind"] == "studio.post"


def test_calendar_fixture_matches_the_spec_7_3_shape():
    cal = json.loads((API / "GET_calendar.json").read_text())["response"]
    entry = cal["days"]["2026-11-02"][0]
    assert {"post", "variant", "at", "slot", "status", "title", "thumb", "rev", "locked", "flags"} <= set(entry)
    assert {"account", "at", "rule", "free"} == set(cal["slots"]["2026-11-06"][0])
    assert {"id", "platform", "live", "paused"} <= set(cal["accounts"][0])
    assert entry["status"] in model.VARIANT_STATUSES and entry["at"].endswith("-07:00") and cal["cursor"].startswith("ev:")


def test_the_move_example_of_the_spec_round_trips():
    ok = json.loads((API / "POST_move_ok.json").read_text())
    assert ok["request"] == {"variant": "v_ig", "expected_rev": 7, "to": {"local": "2026-11-03T09:00", "tz": "America/Denver"}, "snap": "slot"}
    assert ok["status"] == 200 and ok["response"]["post"]["rev"] == 8 and ok["response"]["warnings"] == []
    conflict = json.loads((API / "POST_move_conflict.json").read_text())
    assert conflict["response"]["code"] == "conflict.slot" and conflict["response"]["error"].startswith("slot taken by")


def test_fixture_statuses_are_all_known_values():
    for stem in ("GET_posts", "GET_post"):
        r = json.loads((API / f"{stem}.json").read_text())["response"]
        for p in r.get("posts") or [r["post"]]:
            for v in p["variants"]:
                assert v["status"] in model.VARIANT_STATUSES
