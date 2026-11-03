"""Schemas validate the fixtures (builders and the committed API shapes) and reject the obvious breakage; the mini validator itself."""

import json
from pathlib import Path

import pytest

from ave.studio import ledger, model, store
from ave.studio.events import AGENT

API = Path(__file__).parent.parent / "data_studio" / "api"


def api(stem):
    return json.loads((API / f"{stem}.json").read_text())


def test_every_schema_file_loads_and_all_kinds_are_covered():
    for kind in model.KINDS:
        assert isinstance(model.schema(kind), dict)
    assert {p.stem.replace(".schema", "") for p in model.SCHEMA_DIR.glob("*.schema.json")} == set(model.KINDS)


def test_built_posts_assets_and_accounts_validate(built):
    for pid in built.post_ids:
        assert model.validate("post", store.load_post(built.sp, pid)) == []
    for a in built.assets:
        assert model.validate("asset", a) == []
    for acct in built.accounts:
        assert model.validate("account", acct) == []
    assert model.validate("studio", built.sp.read_meta()) == []


def test_committed_api_fixtures_embed_valid_objects():
    for stem in ("GET_posts", "GET_post", "POST_posts", "POST_edit_ok", "POST_move_ok", "POST_unschedule", "POST_swap_asset"):
        r = api(stem)["response"]
        for p in ([r["post"]] if "post" in r else []) + r.get("posts", []):
            assert model.validate("post", p) == [], (stem, p["id"])
    assert model.validate("asset", api("GET_library_asset")["response"]["asset"]) == []
    for a in api("GET_library")["response"]["assets"]:
        assert model.validate("asset", a) == []
    for f in api("GET_feedback")["response"]["feedback"]:
        assert model.validate("feedback", {k: v for k, v in f.items() if k not in ("resolved", "claim")}) == []
    for a in api("GET_alerts")["response"]["alerts"]:
        assert model.validate("alert", a) == []
    for e in api("GET_changes")["response"]["events"]:
        assert model.validate("event", e) == []
    for p in api("GET_calendar")["response"]["proposals"]:
        assert model.validate("proposal", p) == []


@pytest.mark.parametrize("mutate,expect", [
    (lambda p: p.pop("rev"), "missing required 'rev'"),
    (lambda p: p.update(stage="published"), "not one of"),
    (lambda p: p.update(id="../etc"), "does not match"),
    (lambda p: p["variants"][0].update(platform="myspace"), "not one of"),
    (lambda p: p["variants"][0].update(schedule={"local": "tomorrow", "tz": "UTC", "utc": "x"}), "matches 0 of"),
    (lambda p: p["media"][0].update(asset="a_nothex"), "does not match"),
    (lambda p: p.update(rev=0), "below 1"),
    (lambda p: p.update(rev=True), "expected integer"),
])
def test_post_schema_rejects(built, mutate, expect):
    post = store.load_post(built.sp, built.posts["scheduled"])
    mutate(post)
    problems = model.validate("post", post)
    assert problems and any(expect in x for x in problems), problems


def test_feedback_oneof_tells_the_three_shapes_apart():
    fb = {"v": 1, "op": "feedback", "id": "fb_0001", "ts": "t", "post": "p_x", "kind": "note", "by": "maren"}
    assert model.validate("feedback", fb) == []
    assert model.validate("feedback", {"op": "claim", "fb": "fb_0001", "by": "claude", "ts": "t"}) == []
    assert model.validate("feedback", {"op": "resolve", "fb": "fb_0001", "by": "claude", "ts": "t", "rev": 8, "note": "done"}) == []
    assert model.validate("feedback", {**fb, "kind": "gossip"})
    assert model.validate("feedback", {"op": "claim"})
    assert model.validate("feedback", {"op": "burn", "fb": "x", "by": "y", "ts": "t"})


def test_validator_semantics():
    sch = {"type": "object", "required": ["a"], "additionalProperties": False,
           "properties": {"a": {"type": ["integer", "null"], "minimum": 1}, "b": {"enum": ["x"]}}}
    root = {"definitions": {}}
    out: list[str] = []
    model._walk(sch, {"a": 2}, "t", root, out)
    assert out == []
    for bad, frag in (({}, "missing"), ({"a": 0}, "below"), ({"a": 1.5}, "expected"), ({"a": 1, "z": 1}, "unexpected"),
                      ({"a": 1, "b": "y"}, "not one of"), ({"a": False}, "expected")):
        out = []
        model._walk(sch, bad, "t", root, out)
        assert out and frag in out[0], (bad, out)


def test_unknown_kind_is_an_error():
    with pytest.raises(ValueError):
        model.validate("nonsense", {})


def test_save_post_refuses_an_invalid_post_and_writes_nothing(bare):
    post = model.new_post(idea="x", post_id="p_bad001")
    post["variants"].append({"id": "nope"})
    with pytest.raises(ValueError, match="invalid post"):
        store.save_post(bare, post, expected_rev=None, actor=AGENT, op="create")
    assert not store.post_path(bare, "p_bad001").exists()


def test_ledger_lines_are_validated_on_append(bare):
    with pytest.raises(ValueError, match="invalid feedback"):
        ledger.append_feedback(bare, {"post": "p_x", "kind": "gossip", "by": "maren"})
