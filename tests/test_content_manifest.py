"""ContentDoc v1: validation, versioning, verbatim quotes; template registry."""

import json

import pytest

from ave.content import manifest as M
from ave.content import registry as R
from ave.content import sources as S
from content_helpers import CLIP_TEXT, cep, content_env, make_episode, pick_quote  # noqa: F401


def _doc_with_clip(ep, **slot_over):
    doc = M.new_doc(ep)
    doc["sources"]["q1"] = S.clip_source(ep, "c01")
    slots = {"quote": "A goal is a wish and a system is a habit", "attribution": "Maren Okafor"}
    slots.update(slot_over)
    doc["items"].append(M.new_item("quote-q1-4x5", "quote-post", "ink", "4x5", slots, source="q1", platforms=["instagram"],
                                   reason="the hinge of the episode", confidence=0.8))
    return doc


# ---- registry ------------------------------------------------------------------------------------

def test_registry_loads_four_templates_and_the_matrix():
    reg = R.load_registry()
    assert reg.ids() == ["carousel", "quote-post", "reel-card", "thumbnail"]
    m = {(r["template"], r["style"], r["format"]): (r["w"], r["h"]) for r in reg.matrix()}
    assert m[("quote-post", "ink", "1x1")] == (1080, 1080)
    assert m[("quote-post", "paper", "4x5")] == (1080, 1350)
    assert m[("quote-post", "clay", "9x16")] == (1080, 1920)
    assert m[("thumbnail", "ink", "16x9")] == (1280, 720)
    assert m[("carousel", "ink", "4x5")] == (1080, 1350)
    assert len(m) == 3 * 3 + 3 + 3 + 3          # styles x formats per template


def test_styles_are_token_variants_not_new_templates():
    from ave import motion
    colors = motion.load_tokens()["colors"]
    for name in R.style_names():
        for role, hexv in R.resolve_style(name).items():
            assert hexv in colors.values()


def test_slot_validation_messages_are_actionable():
    t = R.load_registry().get("quote-post")
    assert t.check_slots({"quote": "A goal is a wish and a system is a habit"}) == []
    assert any("required" in p for p in t.check_slots({}))
    assert any("at most 240" in p for p in t.check_slots({"quote": "x" * 300}))
    assert any("not a slot" in p for p in t.check_slots({"quote": "A goal is a wish.", "emoji": "x"}))
    c = R.load_registry().get("carousel")
    assert any("slides[0].kind" in p for p in c.check_slots({"cover": {"title": "T t"}, "closing": {"line": "See you"}, "slides": [{"kind": "poem"}]}))


def test_malformed_template_yaml_is_rejected(tmp_path):
    d = tmp_path / "t" / "bad"
    d.mkdir(parents=True)
    (d / "template.yaml").write_text("id: bad\nslots: {type: object}\nformats: [7x3]\nstyles: [ink]\nrenderers: {motion-kit: {template: quote-post}}\n")
    with pytest.raises(R.RegistryError, match="unknown format"):
        R.load_registry(tmp_path / "t")


# ---- ContentDoc ----------------------------------------------------------------------------------

def test_valid_doc_saves_as_v1_then_v2_and_never_overwrites(cep):
    doc = _doc_with_clip(cep)
    assert M.validate(doc) == []
    p1 = M.save_new_version(cep, doc)
    p2 = M.save_new_version(cep, doc)
    assert p1.name == "main.v1.json" and p2.name == "main.v2.json"
    assert p1.parent == cep.work / "content"
    assert M.list_versions(cep) == [1, 2]
    before = p1.read_text()
    M.save_new_version(cep, doc)
    assert p1.read_text() == before
    assert M.load_latest(cep)["saved"]["version"] == 3


def test_invalid_doc_is_refused_with_every_problem(cep):
    doc = _doc_with_clip(cep)
    doc["version"] = 2
    doc["items"][0]["style"] = "neon"
    doc["items"][0]["confidence"] = 3
    doc["items"][0]["reason"] = ""
    with pytest.raises(ValueError) as e:
        M.save_new_version(cep, doc)
    msg = str(e.value)
    assert "version must be 1" in msg and "neon" in msg and "confidence" in msg and "reason is required" in msg
    assert M.list_versions(cep) == []


def test_paraphrase_is_rejected_but_case_and_punctuation_are_ignored(cep):
    assert M.validate(_doc_with_clip(cep, quote="a goal is a wish, and a system is a habit.")) == []
    problems = M.validate(_doc_with_clip(cep, quote="A goal is only a wish while a system is a habit"))
    assert any("does not appear verbatim" in p for p in problems)
    assert any("Closest passage" in p for p in problems)


def test_ellipsis_and_bracketed_edits_are_rejected(cep):
    assert any("ellipsis" in p for p in M.validate(_doc_with_clip(cep, quote="A goal is a wish ... a system is a habit")))
    assert any("ellipsis" in p for p in M.validate(_doc_with_clip(cep, quote="A goal is a wish and a [good] system is a habit")))


def test_quote_without_source_and_with_unknown_source_is_rejected(cep):
    doc = _doc_with_clip(cep)
    doc["items"][0]["source"] = None
    assert any("has no source" in p for p in M.validate(doc))
    doc["items"][0]["source"] = "q9"
    assert any("unknown source" in p for p in M.validate(doc))


def test_tampering_with_source_text_is_caught_by_its_hash(cep):
    doc = _doc_with_clip(cep)
    doc["sources"]["q1"]["text"] += " And also a made-up sentence."
    assert any("text_sha256 does not match" in p for p in M.validate(doc))


def test_source_drift_is_detected_against_disk(cep):
    doc = _doc_with_clip(cep)
    assert S.verify_sources(cep, doc) == []
    cl = json.loads(cep.artifact("clips.json").read_text())
    cl["clips"][0]["text"] += " Changed."
    cep.artifact("clips.json").write_text(json.dumps(cl))
    assert any("changed since this plan was made" in p for p in S.verify_sources(cep, doc))


def test_only_approved_clips_are_quotable(tmp_path):
    ep = make_episode(tmp_path / "e2", approved=False)
    with pytest.raises(ValueError, match="not approved"):
        S.clip_source(ep, "c01")
    assert S.clip_source(ep, "c01", allow_unapproved=True)["status"] == "proposed"


def test_approved_item_needs_render_alt_text_and_approver(cep):
    doc = _doc_with_clip(cep)
    doc["items"][0]["review"]["status"] = "approved"
    p = M.validate(doc)
    assert any("never rendered" in x for x in p) and any("review.by" in x for x in p)


def test_asset_slot_must_be_listed_for_the_license_gate(cep):
    doc = M.new_doc(cep)
    doc["items"].append(M.new_item("thumb", "thumbnail", "ink", "16x9", {"title": "Soil is a habit", "image": "04_GFX/guest.jpg"},
                                   reason="guest photo", confidence=0.7))
    assert any("not listed in license.assets" in p for p in M.validate(doc))
    doc["items"][0]["license"]["assets"] = ["04_GFX/guest.jpg"]
    assert M.validate(doc) == []


def test_locate_quote_in_transcript_gives_time_and_speaker(cep):
    loc = S.locate(S.transcript_words(cep), "the ground remembers what we do to it")
    assert loc["speaker"] == "S1" and loc["t1"] > loc["t0"] and loc["occurrences"] == 1
    assert S.locate(S.transcript_words(cep), "the ground forgets everything") is None
