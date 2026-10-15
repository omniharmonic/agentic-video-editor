"""The whole content flow through the `ave content` commands: plan, propose, render, review, decide, queue; flag; state."""

import json
import re
from pathlib import Path

import pytest

from ave import cli, state
from ave.content import manifest as M
from ave.content import review as RV
from ave.publish import queue
from content_helpers import apply_env, cep, make_episode, pick_quote  # noqa: F401
from publish_helpers import isolated_secrets  # noqa: F401


@pytest.fixture(autouse=True)
def content_env(isolated_secrets, monkeypatch, tmp_path):
    """Runs AFTER publish_helpers' autouse isolation (which strips every AVE_* variable), then turns the flag on."""
    apply_env(monkeypatch, tmp_path)


def run(capsys, *argv, ok=True):
    code = cli.main(["content", *map(str, argv)])
    out = capsys.readouterr().out
    if ok:
        assert code == 0, out
    return code, (json.loads(out) if out.strip().startswith(("{", "[")) else out)


def picks_file(tmp_path, picks):
    p = tmp_path / "picks.json"
    p.write_text(json.dumps(picks))
    return p


@pytest.fixture
def planned(cep, tmp_path, capsys):
    pf = picks_file(tmp_path, [pick_quote(), pick_quote(id="thumb", template="thumbnail", style="paper", format="16x9", source="episode",
                                                       slots={"title": "Soil is a habit", "guest": "Maren Okafor", "show": "The Long Table"},
                                                       platforms=["youtube"], reason="Episode thumbnail from the hook line.")])
    run(capsys, "propose", cep.root, pf)
    return cep


# ---- the flag ------------------------------------------------------------------------------------

def test_off_by_default_explains_clearly_and_does_nothing(cep, monkeypatch, capsys):
    monkeypatch.setenv("AVE_CONTENT_ENABLED", "0")
    code, out = run(capsys, ok=False)
    assert code == 2 and out["enabled"] is False and "switched off" in out["message"] and "AVE_CONTENT_ENABLED" in out["message"]
    with pytest.raises(SystemExit, match="switched off"):
        cli.main(["content", "plan", str(cep.root)])
    assert not (cep.work / "content").exists()


def test_repo_default_is_false_and_an_install_override_turns_it_on(cep, monkeypatch, tmp_path):
    from ave.content import settings
    monkeypatch.delenv("AVE_CONTENT_ENABLED")
    assert settings.enabled() is False                                  # config/content.yaml in the repo
    (tmp_path / "cfg").mkdir(exist_ok=True)
    (tmp_path / "cfg" / "content.yaml").write_text("enabled: true\nplanning: {max_items_per_episode: 3}\n")
    assert settings.enabled() is True
    assert settings.load()["planning"]["max_quote_chars"] == 240 and settings.load()["planning"]["max_items_per_episode"] == 3   # merged


def test_templates_command_lists_registry_and_matrix(cep, capsys):
    _, out = run(capsys, "templates")
    assert {t["id"] for t in out["templates"]} == {"quote-post", "thumbnail", "carousel", "reel-card"}
    assert len(out["matrix"]) == 18 and out["styles"] == ["ink", "paper", "clay"]


# ---- plan ----------------------------------------------------------------------------------------

def test_plan_offers_only_approved_clips_with_verified_candidates(cep, capsys):
    _, out = run(capsys, "plan", cep.root)
    assert [c["clip"] for c in out["approved_clips"]] == ["c01"]
    c = out["approved_clips"][0]
    quotes = [q["quote"] for q in c["quote_candidates"]]
    assert "A goal is a wish and a system is a habit" in quotes and all(len(q) <= 240 for q in quotes)
    hq = next(q for q in c["quote_candidates"] if q["quote"].startswith("A goal is a wish"))
    assert hq["speaker_name"] == "Maren Okafor" and hq["t1"] > hq["t0"]
    assert out["episode_meta"]["people"] == ["Maren Okafor", "Tomas"]
    assert (M.content_dir(cep) / "candidates.json").exists()


def test_plan_says_what_is_missing_when_nothing_is_approved(tmp_path, capsys):
    ep = make_episode(tmp_path / "e", approved=False)
    _, out = run(capsys, "plan", ep.root)
    assert out["approved_clips"] == [] and "Nothing to quote yet" in out["note"]


# ---- propose -------------------------------------------------------------------------------------

def test_propose_accepts_good_picks_rejects_bad_ones_with_every_problem(cep, tmp_path, capsys):
    pf = picks_file(tmp_path, [
        pick_quote(),
        pick_quote(id="para", slots={"quote": "Goals are only wishes, systems are habits", "attribution": "Maren Okafor"}),
        pick_quote(id="unapproved", source={"clip": "c02"}, slots={"quote": "There is not one", "attribution": "Tomas"}),
        pick_quote(id="neon", style="neon", confidence=None, reason=""),
        {"template": "poster", "slots": {}},
    ])
    code, out = run(capsys, "propose", cep.root, pf, ok=False)
    assert code == 2
    assert [a["id"] for a in out["accepted"]] == ["quote-c01-4x5"]
    probs = {r["id"]: " | ".join(r["problems"]) for r in out["rejected"]}
    assert "does not appear verbatim" in probs["para"]
    assert "not approved" in probs["unapproved"]
    assert "neon" in probs["neon"] and "confidence" in probs["neon"] and "reason is required" in probs["neon"]
    assert any("not in the registry" in v for v in probs.values())
    doc = M.load_latest(cep)
    assert [i["id"] for i in doc["items"]] == ["quote-c01-4x5"] and list(doc["sources"]) == ["q1"]
    q = doc["items"][0]["provenance"]["quotes"][0]
    assert q["located"] and q["speaker_name"] == "Maren Okafor"


def test_propose_warns_when_the_attribution_is_not_who_spoke(cep, tmp_path, capsys):
    pf = picks_file(tmp_path, [pick_quote(slots={"quote": "A goal is a wish and a system is a habit", "attribution": "Tomas"})])
    _, out = run(capsys, "propose", cep.root, pf)
    assert "Maren Okafor" in out["accepted"][0]["warnings"][0] and "Tomas" in out["accepted"][0]["warnings"][0]


def test_propose_enforces_the_item_limit_and_unknown_platform_formats(cep, tmp_path, monkeypatch, capsys):
    (tmp_path / "cfg").mkdir(exist_ok=True)
    (tmp_path / "cfg" / "content.yaml").write_text("planning: {max_items_per_episode: 1}\n")
    pf = picks_file(tmp_path, [pick_quote(), pick_quote(id="two", format="1x1")])
    code, out = run(capsys, "propose", cep.root, pf, ok=False)
    assert len(out["accepted"]) == 1 and "limit is 1" in out["rejected"][0]["problems"][-1]
    pf = picks_file(tmp_path, [pick_quote(id="wide", format="9x16", platforms=["youtube"])])
    code, out = run(capsys, "propose", cep.root, pf, ok=False)
    assert any("youtube" in p for p in out["rejected"][0]["problems"])


def test_carousel_pick_with_per_slide_sources_and_a_paraphrased_slide(cep, tmp_path, capsys):
    car = {"id": "car1", "template": "carousel", "style": "paper", "format": "4x5", "source": {"clip": "c01"},
           "reason": "The argument in five swipes.", "confidence": 0.7, "platforms": ["instagram"],
           "slots": {"show": "The Long Table", "cover": {"title": "What soil teaches"}, "closing": {"line": "Listen to the episode"},
                     "slides": [{"kind": "quote", "quote": "We do not fix a field in a season, we tend it for a generation.", "attribution": "Maren Okafor"},
                                {"kind": "point", "text": "Repair needs people who stay."}]}}
    _, out = run(capsys, "propose", cep.root, picks_file(tmp_path, [car]))
    assert out["accepted"][0]["id"] == "car1"
    car2 = json.loads(json.dumps(car))
    car2["id"] = "car2"
    car2["slots"]["slides"][0]["quote"] = "we nurture it across a generation"
    code, out = run(capsys, "propose", cep.root, picks_file(tmp_path, [car2]), ok=False)
    assert code == 2 and "slots.slides[0].quote" in out["rejected"][0]["problems"][0]


# ---- render, review, decide ----------------------------------------------------------------------

def test_render_review_decide_approve_flow(planned, capsys):
    ep = planned
    _, rep = run(capsys, "render", ep.root)
    assert {r["id"] for r in rep["rendered"]} == {"quote-c01-4x5", "thumb"} and rep["version"] == 2
    doc = M.load_latest(ep)
    for it in doc["items"]:
        assert it["review"]["critic"]["ok"] and it["outputs"][0]["sha256"]
    _, rv = run(capsys, "review", ep.root)
    page = Path(rv["index"]).read_text()
    refs = re.findall(r'src="([^"]+\.png)"', page)
    assert len(refs) == 2
    for r in refs:
        assert (Path(rv["index"]).parent / r).resolve().is_file(), r              # the page references real files
    assert "<mark>A goal is a wish and a system is a habit" in page and "Maren Okafor at" in page
    assert "contrast" in page and "decisions.json" in page and 'id="phone"' in page
    d = ep.root / "decisions.json"
    d.write_text(json.dumps([{"id": "quote-c01-4x5", "decision": "approve", "note": "yes"}, {"id": "thumb", "decision": "reject", "note": "too plain"}]))
    code, out = run(capsys, "decide", ep.root, d, "--by", "ben")
    assert out["approved_items"] == ["quote-c01-4x5"] and out["version"] == 3
    doc = M.load_latest(ep)
    by_id = {i["id"]: i for i in doc["items"]}
    assert by_id["quote-c01-4x5"]["review"]["status"] == "approved" and by_id["quote-c01-4x5"]["review"]["by"] == "ben"
    assert by_id["thumb"]["review"]["status"] == "rejected"
    assert (M.content_dir(ep) / "decisions.jsonl").read_text().count("\n") == 2
    assert M.list_versions(ep) == [1, 2, 3]                                          # nothing overwritten


def test_decide_refuses_a_file_that_changed_since_render_and_unknown_ids(planned, capsys):
    ep = planned
    run(capsys, "render", ep.root)
    doc = M.load_latest(ep)
    f = ep.root / doc["items"][0]["outputs"][0]["file"]
    f.write_bytes(f.read_bytes() + b"x")
    d = ep.root / "decisions.json"
    d.write_text(json.dumps([{"id": doc["items"][0]["id"], "decision": "approve"}, {"id": "ghost", "decision": "approve"}]))
    code, out = run(capsys, "decide", ep.root, d, "--by", "ben", ok=False)
    assert code == 2 and out["applied"] == []
    assert "changed on disk since it was rendered" in out["refused"][0]["problems"][0] and "no item 'ghost'" in out["refused"][1]["problems"][0]


def test_critic_failure_blocks_approval_unless_overridden_with_a_note(planned, capsys):
    ep = planned
    run(capsys, "render", ep.root)
    doc = M.clone(M.load_latest(ep))
    doc["items"][0]["review"]["critic"] = {**doc["items"][0]["review"]["critic"], "ok": False, "notes": ["quote: contrast 3.1:1 is under the required 4.5:1"]}
    M.save_new_version(ep, doc)
    iid = doc["items"][0]["id"]
    d = ep.root / "d.json"
    d.write_text(json.dumps([{"id": iid, "decision": "approve"}]))
    code, out = run(capsys, "decide", ep.root, d, "--by", "ben", ok=False)
    assert code == 2 and "the critic flagged this item" in out["refused"][0]["problems"][0]
    d.write_text(json.dumps([{"id": iid, "decision": "approve", "override": True}]))
    code, out = run(capsys, "decide", ep.root, d, "--by", "ben", ok=False)
    assert "needs a note" in out["refused"][0]["problems"][0]
    d.write_text(json.dumps([{"id": iid, "decision": "approve", "override": True, "note": "intentional, large display type"}]))
    code, out = run(capsys, "decide", ep.root, d, "--by", "ben")
    assert out["approved_items"] == [iid] and M.load_latest(ep)["items"][0]["review"]["critic_override"] is True


def test_nudge_changes_style_clears_renders_and_rerenders_only_that_item(planned, capsys):
    ep = planned
    run(capsys, "render", ep.root)
    before = {i["id"]: i for i in M.load_latest(ep)["items"]}
    d = ep.root / "d.json"
    d.write_text(json.dumps([{"id": "quote-c01-4x5", "decision": "nudge", "nudge": {"style": "paper", "format": "1x1"}}]))
    _, out = run(capsys, "decide", ep.root, d, "--by", "ben")
    assert out["applied"][0]["needs_render"] and "re-render" in out["next"]
    nd = {i["id"]: i for i in M.load_latest(ep)["items"]}
    assert nd["quote-c01-4x5"]["outputs"] == [] and nd["quote-c01-4x5"]["style"] == "paper"
    _, rep = run(capsys, "render", ep.root)
    assert [r["id"] for r in rep["rendered"]] == ["quote-c01-4x5"] and rep["skipped"][0]["id"] == "thumb"
    after = {i["id"]: i for i in M.load_latest(ep)["items"]}
    assert after["quote-c01-4x5"]["outputs"][0]["w"] == 1080 and after["quote-c01-4x5"]["outputs"][0]["h"] == 1080
    assert after["thumb"]["outputs"] == before["thumb"]["outputs"]


def test_nudging_a_quote_to_a_paraphrase_is_refused(planned, capsys):
    ep = planned
    d = ep.root / "d.json"
    d.write_text(json.dumps([{"id": "quote-c01-4x5", "decision": "nudge", "nudge": {"slots": {"quote": "Goals are wishes and systems are habits"}}}]))
    code, out = run(capsys, "decide", ep.root, d, "--by", "ben", ok=False)
    assert code == 2 and "does not appear verbatim" in " ".join(out["refused"][0]["problems"])


def test_alt_text_nudge_rewrites_the_description_without_rerendering(planned, capsys):
    ep = planned
    run(capsys, "render", ep.root)
    before = M.load_latest(ep)["items"][0]["outputs"][0]["sha256"]
    d = ep.root / "d.json"
    d.write_text(json.dumps([{"id": "quote-c01-4x5", "decision": "nudge", "nudge": {"alt_text": "Cream serif words on a dark brown field."}}]))
    _, out = run(capsys, "decide", ep.root, d, "--by", "ben")
    assert out["applied"][0]["needs_render"] is False
    o = M.load_latest(ep)["items"][0]["outputs"][0]
    assert o["alt_text"].startswith("Cream serif") and o["sha256"] == before


def test_reproposing_keeps_human_decisions_and_replaces_drafts(planned, tmp_path, capsys):
    ep = planned
    run(capsys, "render", ep.root)
    d = ep.root / "d.json"
    d.write_text(json.dumps([{"id": "quote-c01-4x5", "decision": "approve"}]))
    run(capsys, "decide", ep.root, d, "--by", "ben")
    pf = picks_file(tmp_path, [pick_quote(id="other", format="1x1", slots={"quote": "We do not fix a field in a season, we tend it for a generation.", "attribution": "Maren Okafor"})])
    _, out = run(capsys, "propose", ep.root, pf)
    assert out["kept_human_decisions"] == ["quote-c01-4x5"]
    assert [i["id"] for i in M.load_latest(ep)["items"]] == ["quote-c01-4x5", "other"]


# ---- queue ---------------------------------------------------------------------------------------

def test_queue_adds_approved_items_as_image_posts_once_and_never_approves(planned, capsys, isolated_secrets):
    ep = planned
    run(capsys, "render", ep.root)
    d = ep.root / "d.json"
    d.write_text(json.dumps([{"id": "quote-c01-4x5", "decision": "approve"}, {"id": "thumb", "decision": "approve"}]))
    run(capsys, "decide", ep.root, d, "--by", "ben")
    code, out = run(capsys, "queue", ep.root, "--dry-run")
    assert [q["item"] for q in out["queued"]] == ["quote-c01-4x5"] and all(q["dry_run"] for q in out["queued"])
    assert any(s.get("platform") == "youtube" and "video only" in s["why"] for s in out["skipped"])
    assert queue.list_items(ep) == []
    # private is the default, and the aggregator cannot hold a post back: the item is queued as a DRAFT that says so
    code, out = run(capsys, "queue", ep.root, ok=False)
    assert code == 2 and "cannot hold a post back" in out["queued"][0]["problems"][0] and queue.list_items(ep)[0]["status"] == "draft"
    code, out = run(capsys, "queue", ep.root, "--visibility", "public")
    q = [i for i in queue.list_items(ep) if i["status"] == "awaiting_approval"]
    assert len(q) == 1 and q[0]["media_type"] == "image" and q[0]["approvals"] == []
    assert q[0]["platforms"] == ["instagram"] and q[0]["visibility"] == "public"
    doc = M.load_latest(ep)
    o = doc["items"][0]["outputs"][0]
    assert q[0]["sha256"] == o["sha256"] and q[0]["alt_texts"] == [o["alt_text"]]
    assert q[0]["caption"].startswith("“A goal is a wish") and q[0]["origin"]["item"] == "quote-c01-4x5"
    code, out = run(capsys, "queue", ep.root, "--visibility", "public")
    assert out["queued"] == [] and any("already queued" in s["why"] for s in out["skipped"])
    assert len(queue.list_items(ep)) == 2


def test_queue_refuses_an_item_whose_file_changed_after_approval(planned, capsys, isolated_secrets):
    ep = planned
    run(capsys, "render", ep.root)
    d = ep.root / "d.json"
    d.write_text(json.dumps([{"id": "quote-c01-4x5", "decision": "approve"}]))
    run(capsys, "decide", ep.root, d, "--by", "ben")
    f = ep.root / M.load_latest(ep)["items"][0]["outputs"][0]["file"]
    f.write_bytes(f.read_bytes() + b"x")
    code, out = run(capsys, "queue", ep.root, ok=False)
    assert code == 2 and "changed since it was reviewed" in out["refused"][0]["problems"][0] and queue.list_items(ep) == []


def test_unapproved_items_are_never_queued(planned, capsys, isolated_secrets):
    ep = planned
    run(capsys, "render", ep.root)
    _, out = run(capsys, "queue", ep.root)
    assert out["queued"] == [] and queue.list_items(ep) == []


def test_rerendering_an_approved_item_voids_its_approval(planned, capsys):
    ep = planned
    run(capsys, "render", ep.root)
    d = ep.root / "d.json"
    d.write_text(json.dumps([{"id": "quote-c01-4x5", "decision": "approve"}]))
    run(capsys, "decide", ep.root, d, "--by", "ben")
    _, rep = run(capsys, "render", ep.root)
    assert any("approved" in s["why"] for s in rep["skipped"])
    _, rep = run(capsys, "render", ep.root, "--item", "quote-c01-4x5")
    assert rep["rendered"] and M.load_latest(ep)["items"][0]["review"]["status"] == "draft"


# ---- state ---------------------------------------------------------------------------------------

def test_ave_status_is_unchanged_for_episodes_without_content_and_the_stage_appears_once_started(cep, planned):
    keys = [r["stage"] for r in state.status(planned)["stages"]]
    assert "content" in keys and keys.index("content") == keys.index("selects") + 1
    row = next(r for r in state.status(planned)["stages"] if r["stage"] == "content")
    assert row["done"] and row["optional"] and row["human_gate"] and row["blocked_by"] == []


def test_stage_list_without_content_is_exactly_the_old_list(tmp_path):
    from ave.episode import Episode
    ep = Episode(tmp_path / "plain")
    ep.create("Plain")
    st = state.status(ep)
    assert [r["stage"] for r in st["stages"]] == ["ingest", "audio", "transcribe", "sync", "map", "cutplan", "assembly", "rough", "final",
                                                  "selects", "shorts", "package", "publish", "handoff"]
    assert st["next"]["stage"] == "ingest" and "Promotion content" not in state.briefing(ep)


def test_disfluent_quotes_are_flagged_in_plan_and_propose_but_stay_verbatim(cep, tmp_path, capsys):
    from ave.content import sources as S
    assert S.disfluencies("it is is so much more, uh, complex") == ["repeated word “is”", "filler “uh”"]
    assert S.disfluencies("A goal is a wish and a system is a habit") == []
    cl = json.loads(cep.artifact("clips.json").read_text())
    cl["clips"][0]["text"] = cl["clips"][0]["text"].replace("the ground remembers", "the the ground remembers")
    cep.artifact("clips.json").write_text(json.dumps(cl))
    pf = picks_file(tmp_path, [pick_quote(slots={"quote": "A goal is a wish and a system is a habit, and the the ground remembers what we do to it long after we have stopped paying attention.", "attribution": "Maren Okafor"})])
    _, out = run(capsys, "propose", cep.root, pf)
    assert "repeated word" in out["accepted"][0]["warnings"][-1] and "Prefer a cleaner line" in out["accepted"][0]["warnings"][-1]
