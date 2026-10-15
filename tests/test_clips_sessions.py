"""T16 (field note N16): clips, captions, shorts and content understand per-session (named) edits.

The multi-session synthetic episode (T10) is taken through plan-cuts, assemble and cleanup once; every test works on a private copy.
Both sessions' transcripts start at sentence s000, so any id that is not prefixed by its session would collide.
"""
import json
import shutil

import pytest

from ave import clips, editdoc, shorts
from ave.cli import main
from ave.content import sources
from ave.episode import Episode, read_json

from clips_synth import pick
from conftest import needs_ffmpeg

pytestmark = needs_ffmpeg

NO_FACE = lambda img: []  # noqa: E731


def cli(capsys, *argv, rc=0):
    got = main([str(a) for a in argv])
    assert got == rc, (argv, got)
    out = capsys.readouterr().out
    return json.loads(out) if out.strip().startswith(("{", "[")) else out


@pytest.fixture(scope="module")
def rough_root(ms_roots, tmp_path_factory):
    """The multi-session episode with rough.s1.v1 and rough.s2.v1 (built once, copied per test)."""
    root = tmp_path_factory.mktemp("clips-sessions") / "episode"
    shutil.copytree(ms_roots("map"), root)
    for argv in (["plan-cuts", str(root)], ["assemble", str(root), "--canvas", "64x36"],
                 ["cleanup", str(root), "--name", "s1"], ["cleanup", str(root), "--name", "s2"]):
        assert main(argv) == 0
    return root


@pytest.fixture
def ep(rough_root, tmp_path):
    dst = tmp_path / "episode"
    shutil.copytree(rough_root, dst)
    return Episode(dst)


def prep(ep, name, **kw):
    return clips.prepare(ep, min_s=10, max_s=40, name=name, **kw)


def prop(ep, name, picks):
    return clips.propose(ep, picks, name=name)


def both(ep):
    prep(ep, "s1")
    prep(ep, "s2")
    r1 = prop(ep, "s1", [pick(0, 1, "Opening thought"), pick(4, 5, "Later point")])
    r2 = prop(ep, "s2", [pick(0, 1, "Opening thought")])
    assert r1["rejected"] == [] and r2["rejected"] == []
    return ep


def all_clips(ep):
    return read_json(ep.artifact("clips.json"))["clips"]


def newer_rough(ep, name, keep_s):
    """rough.<name>.v2: the same edit truncated to its first keep_s seconds (a different sentence table)."""
    base = editdoc.load_latest(ep, "rough", name)
    sl = editdoc.slice_doc(base, 0.0, keep_s, stage="rough")
    return editdoc.save_new_version(ep, sl, name=name)


# ---- no --name on a multi-session episode ------------------------------------------------------------------------

@pytest.mark.parametrize("call", [
    lambda ep: clips.prepare(ep),
    lambda ep: clips.propose(ep, [pick(0, 1)]),
    lambda ep: clips.build(ep),
    lambda ep: clips.decide(ep, [{"id": "s1-c01", "decision": "approve"}]),
    lambda ep: clips.list_rows(ep),
])
def test_missing_name_is_an_error_that_lists_the_sessions(ep, call):
    with pytest.raises(SystemExit) as e:
        call(ep)
    msg = str(e.value)
    assert "s1" in msg and "s2" in msg and "--name" in msg


def test_missing_name_for_review_and_unknown_name(ep):
    from ave import review_pack
    with pytest.raises(SystemExit, match="--name"):
        review_pack.build_pack(ep)
    with pytest.raises(SystemExit, match="s9"):
        clips.prepare(ep, name="s9")


def test_cli_missing_name_exits_with_the_sessions(ep):
    with pytest.raises(SystemExit, match="s1, s2"):
        main(["clips", "prepare", str(ep.root)])


# ---- prepare ------------------------------------------------------------------------------------------------------

def test_prepare_per_session_reads_that_sessions_rough_and_writes_its_own_files(ep):
    r1, r2 = prep(ep, "s1"), prep(ep, "s2")
    for name, r in (("s1", r1), ("s2", r2)):
        assert r["name"] == name and r["stage"] == "rough" and r["version"] == 1
        assert ep.artifact(f"clips_candidates.{name}.json").exists() and ep.artifact(f"clips_transcript.{name}.txt").exists()
        cand = read_json(ep.artifact(f"clips_candidates.{name}.json"))
        assert cand["source"]["name"] == name and cand["source"]["path"].endswith(f"rough.{name}.v1.json")
    assert not ep.artifact("clips_candidates.json").exists()
    assert r1["sentences"] == 8 and r2["sentences"] == 5
    assert r1["fingerprint"] != r2["fingerprint"]


def test_prepare_falls_back_to_the_sessions_assembly(ep):
    for f in ep.edits.glob("rough.s2.*"):
        f.unlink()
    r = prep(ep, "s2")
    assert r["stage"] == "assembly"
    with pytest.raises(SystemExit, match="rough"):
        clips.prepare(ep, stage="rough", name="s2")


def test_prepare_cli_is_json_and_takes_name(ep, capsys):
    out = cli(capsys, "clips", "prepare", ep.root, "--name", "s2", "--min-s", "10", "--max-s", "40", "--floor", "0")
    assert out["name"] == "s2" and out["windows"] >= 1                                  # the plain fixture only surfaces with a zero floor


# ---- propose ------------------------------------------------------------------------------------------------------

def test_two_sessions_coexist_with_unique_prefixed_ids_and_recorded_sources(ep):
    both(ep)
    cl = all_clips(ep)
    assert sorted(c["id"] for c in cl) == ["s1-c01", "s1-c02", "s2-c01"]
    assert len({c["slug"] for c in cl}) == 3 and all(c["slug"].startswith(c["session"] + "-") for c in cl)
    for c in cl:
        assert set(c["source"]) == {"stage", "name", "version", "fingerprint"}
        assert c["source"]["stage"] == "rough" and c["source"]["name"] == c["session"] and c["source"]["version"] == 1
    fp = {n: read_json(ep.artifact(f"clips_candidates.{n}.json"))["source"]["fingerprint"] for n in ("s1", "s2")}
    assert {c["id"]: c["source"]["fingerprint"] for c in cl} == {"s1-c01": fp["s1"], "s1-c02": fp["s1"], "s2-c01": fp["s2"]}


def test_proposing_one_session_never_touches_the_others_picks(ep):
    both(ep)
    before = [c for c in all_clips(ep) if c["session"] == "s1"]
    prop(ep, "s2", [pick(2, 3, "Another")])
    assert [c for c in all_clips(ep) if c["session"] == "s1"] == before
    ids = sorted(c["id"] for c in all_clips(ep) if c["session"] == "s2")
    assert ids == ["s2-c02"]            # the undecided s2 proposal was replaced, numbers keep counting


def test_propose_validates_against_its_own_sessions_table(ep):
    prep(ep, "s2")
    r = prop(ep, "s2", [pick(6, 7, "past the end of s2")])       # s2 has only s000..s004
    assert r["accepted"] == [] and "unknown sentence ids" in r["rejected"][0]["problems"][0]


def test_overlap_is_checked_within_a_session_only(ep):
    prep(ep, "s1")
    prep(ep, "s2")
    prop(ep, "s1", [pick(0, 1, "a")])
    r = prop(ep, "s2", [pick(0, 1, "b")])           # same sentence ids, other session: fine
    assert r["rejected"] == [] and r["accepted"][0]["id"] == "s2-c01"


def test_cli_propose_with_name(ep, capsys, tmp_path):
    prep(ep, "s1")
    picks = tmp_path / "picks.json"
    picks.write_text(json.dumps([pick(0, 1, "From the cli")]))
    out = cli(capsys, "clips", "propose", ep.root, "--picks", picks, "--name", "s1")
    assert out["accepted"][0]["id"] == "s1-c01"


# ---- decide / list ------------------------------------------------------------------------------------------------

def test_decide_applies_within_the_named_session_only(ep):
    both(ep)
    r = clips.decide(ep, [{"id": "s2-c01", "decision": "approve"}, {"id": "s1-c01", "decision": "approve"}], name="s2")
    assert [a["id"] for a in r["applied"]] == ["s2-c01"]
    assert r["errors"][0]["id"] == "s1-c01" and "session s1" in r["errors"][0]["error"]
    assert r["approved"] == ["s2-c01"]
    by = {c["id"]: c["status"] for c in all_clips(ep)}
    assert by == {"s1-c01": "proposed", "s1-c02": "proposed", "s2-c01": "approved"}


def test_decide_nudge_resnaps_with_the_sessions_sentence_table(ep):
    both(ep)
    r = clips.decide(ep, [{"id": "s1-c01", "decision": "nudge", "nudge": {"end": 1}}], name="s1")
    assert r["errors"] == [] and r["applied"][0]["duration"] > 20
    assert all_clips(ep)[0]["sentence_ids"] == ["s000", "s001", "s002"]


def test_list_filters_by_session_or_all(ep):
    both(ep)
    assert [c["id"] for c in clips.list_rows(ep, name="s2")] == ["s2-c01"]
    assert [c["id"] for c in clips.list_rows(ep, show_all=True)] == ["s1-c01", "s1-c02", "s2-c01"]


# ---- build --------------------------------------------------------------------------------------------------------

def test_build_per_session_writes_prefixed_selects_from_that_sessions_edit(ep):
    both(ep)
    r1 = clips.build(ep, name="s1")
    assert sorted(r1["built"]) == ["s1-c01", "s1-c02"]
    assert editdoc.list_versions(ep, "select", "s1-selects") == [1]
    assert not editdoc.list_versions(ep, "select", "s2-selects")
    slug = next(c["slug"] for c in all_clips(ep) if c["id"] == "s1-c01")
    doc = editdoc.load_latest(ep, "select", slug)
    assert editdoc.validate(doc) == [] and doc["stage"] == "select"
    assert not {"A2", "B2"} & set(doc["sources"])                    # only session 1's cameras (+ the master audio M)
    r2 = clips.build(ep, name="s2")
    assert r2["built"] == ["s2-c01"] and editdoc.list_versions(ep, "select", "s2-selects") == [1]
    assert not {"A1", "B1"} & set(editdoc.load_latest(ep, "select", "s2-selects")["sources"])
    assert editdoc.list_versions(ep, "select", "s1-selects") == [1]    # building s2 did not rebuild s1
    cl = {c["id"]: c for c in all_clips(ep)}
    assert cl["s1-c01"]["edit"]["path"].endswith(f"select.{slug}.v1.json")


def test_build_is_idempotent_per_session(ep):
    both(ep)
    clips.build(ep, name="s1")
    again = clips.build(ep, name="s1")
    assert again["built"] == [] and sorted(again["unchanged"]) == ["s1-c01", "s1-c02"]
    assert editdoc.list_versions(ep, "select", "s1-selects") == [1]


def test_build_approved_only_and_selects_per_session_survive_each_other(ep):
    both(ep)
    clips.decide(ep, [{"id": "s1-c02", "decision": "approve"}], name="s1")
    r = clips.build(ep, approved_only=True, name="s1")
    assert list(r["clips"]) == ["s1-c02"]
    clips.build(ep, name="s2")
    d = read_json(ep.artifact("clips.json"))
    assert d["sessions"]["s1"]["selects"]["path"].endswith("select.s1-selects.v1.json")
    assert d["sessions"]["s2"]["selects"]["path"].endswith("select.s2-selects.v1.json")


# ---- stale: the session's rough changed ---------------------------------------------------------------------------

def test_build_uses_the_exact_edit_the_clip_was_picked_from(ep):
    both(ep)
    newer_rough(ep, "s2", 40.0)               # rough.s2.v2 now exists, nobody re-prepared
    r = clips.build(ep, name="s2")
    assert r["built"] == ["s2-c01"] and "stale_skipped" in r and r["stale_skipped"] == []
    doc = editdoc.load_latest(ep, "select", next(c["slug"] for c in all_clips(ep) if c["id"] == "s2-c01"))
    base = editdoc.load(editdoc.path_for(ep, "rough", 1, "s2"))
    assert doc["sources"] == {k: v for k, v in base["sources"].items() if k in doc["sources"]}
    assert clips.list_rows(ep, name="s2")[0]["newer_edit"] == 2        # list says a newer rough exists


def test_new_rough_marks_that_sessions_old_picks_stale_not_corrupt(ep):
    both(ep)
    clips.decide(ep, [{"id": "s2-c01", "decision": "approve"}], name="s2")
    before = {c["id"]: dict(c) for c in all_clips(ep)}
    newer_rough(ep, "s2", 40.0)
    r = prep(ep, "s2")
    assert r["version"] == 2 and r["stale_clips"] == ["s2-c01"]
    cl = {c["id"]: c for c in all_clips(ep)}
    assert cl["s2-c01"]["stale"] is True and "v1" in cl["s2-c01"]["stale_reason"] and "v2" in cl["s2-c01"]["stale_reason"]
    assert cl["s2-c01"]["status"] == "approved" and cl["s2-c01"]["text"] == before["s2-c01"]["text"]       # nothing corrupted
    assert cl["s2-c01"]["source"]["version"] == 1
    assert cl["s1-c01"] == before["s1-c01"] and not cl["s1-c01"].get("stale")                       # other session untouched


def test_stale_clips_are_not_built_or_decided_and_do_not_block_new_picks(ep):
    both(ep)
    clips.decide(ep, [{"id": "s2-c01", "decision": "approve"}], name="s2")
    newer_rough(ep, "s2", 40.0)
    prep(ep, "s2")
    r = prop(ep, "s2", [pick(0, 1, "Fresh pick")])
    assert r["rejected"] == [] and r["accepted"][0]["id"] == "s2-c02"       # ids keep counting; the stale pick does not overlap it
    by = {c["id"]: c for c in all_clips(ep)}
    assert by["s2-c02"]["source"]["version"] == 2 and by["s2-c01"]["stale"]
    out = clips.decide(ep, [{"id": "s2-c01", "decision": "nudge", "nudge": {"end": 1}}], name="s2")
    assert "stale" in out["errors"][0]["error"]
    b = clips.build(ep, name="s2")
    assert b["built"] == ["s2-c02"] and b["stale_skipped"] == ["s2-c01"]
    b2 = clips.build(ep, name="s2", include_stale=True, force=True)
    assert sorted(b2["built"]) == ["s2-c01", "s2-c02"]


def test_prepare_with_the_same_table_keeps_clips_fresh(ep):
    both(ep)
    prep(ep, "s1")
    assert not any(c.get("stale") for c in all_clips(ep))


# ---- unnamed single-session behaviour is unchanged ------------------------------------------------------------------

def test_single_session_flow_keeps_the_legacy_shapes(tmp_path):
    from clips_synth import build_long_episode
    e = build_long_episode(tmp_path / "solo")
    r = clips.prepare(e, min_s=10, max_s=25)
    assert "name" not in r and e.artifact("clips_candidates.json").exists() and e.artifact("clips_transcript.txt").exists()
    clips.propose(e, [pick(9, 13, "story")])
    d = read_json(e.artifact("clips.json"))
    assert set(d) == {"version", "source", "params", "rubric", "rubric_path", "clips", "rejected_picks", "selects"}
    c = d["clips"][0]
    assert c["id"] == "c01" and c["slug"] == "story" and "session" not in c and "source" not in c and "stale" not in c
    b = clips.build(e)
    assert "stale_skipped" not in b and editdoc.list_versions(e, "select", "selects") == [1]
    with pytest.raises(SystemExit, match="no sessions"):
        clips.prepare(e, name="s1")


# ---- review pack ---------------------------------------------------------------------------------------------------

def test_review_pack_is_per_session(ep):
    from ave import review_pack
    both(ep)
    r = review_pack.build_pack(ep, name="s2")
    assert r["name"] == "s2" and r["clips"] == 1 and "s2" in r["index"]
    page = open(r["index"]).read()
    assert "s2-c01" in page and "s1-c01" not in page and "rough.s2 v1" in page and "--name s2" in page
    r1 = review_pack.build_pack(ep, name="s1")
    assert r1["index"] != r["index"] and r1["clips"] == 2


def test_review_pack_hides_stale_clips_and_says_so(ep):
    from ave import review_pack
    both(ep)
    newer_rough(ep, "s2", 40.0)
    prep(ep, "s2")
    r = review_pack.build_pack(ep, name="s2")
    assert r["clips"] == 0 and any("stale" in n for n in r["notes"])


# ---- shorts and content read the clip's own source ------------------------------------------------------------------

def test_shorts_select_clips_from_any_session_without_a_name(ep):
    both(ep)
    clips.decide(ep, [{"id": "s1-c01", "decision": "approve"}], name="s1")
    clips.decide(ep, [{"id": "s2-c01", "decision": "approve"}], name="s2")
    clips.build(ep, approved_only=True, name="s1")
    clips.build(ep, approved_only=True, name="s2")
    assert [c["id"] for c in shorts.select_clips(ep, all_approved=True)] == ["s1-c01", "s2-c01"]
    assert [c["id"] for c in shorts.select_clips(ep, all_approved=True, name="s2")] == ["s2-c01"]
    assert [c["id"] for c in shorts.select_clips(ep, "s2-c01")] == ["s2-c01"]
    assert [c["id"] for c in shorts.select_clips(ep, "s2-c01", name="s2")] == ["s2-c01"]
    with pytest.raises(SystemExit, match="session s1"):
        shorts.select_clips(ep, "s1-c01", name="s2")
    clips.decide(ep, [{"id": "s1-c01", "decision": "reject"}], name="s1")
    with pytest.raises(SystemExit, match="no approved clips"):
        shorts.select_clips(ep, all_approved=True, name="s1")


def test_short_is_built_from_the_clips_select_in_its_session(ep):
    both(ep)
    clips.decide(ep, [{"id": "s2-c01", "decision": "approve"}], name="s2")
    clips.build(ep, approved_only=True, name="s2")
    clip = shorts.select_clips(ep, "s2-c01")[0]
    res = shorts.build_short(ep, clip, detector=NO_FACE)
    assert res["slug"] == clip["slug"] and res["slug"].startswith("s2-")
    doc = editdoc.load_latest(ep, "short", clip["slug"])
    assert doc["short"]["clip_id"] == "s2-c01" and not {"A1", "B1"} & set(doc["sources"])
    assert editdoc.validate(doc) == []


def test_content_sources_use_clips_from_any_session(ep):
    both(ep)
    clips.decide(ep, [{"id": "s2-c01", "decision": "approve"}], name="s2")
    clips.decide(ep, [{"id": "s1-c01", "decision": "approve"}], name="s1")
    src = sources.clip_source(ep, "s2-c01")
    assert src["clip"] == "s2-c01" and src["session"] == "s2" and src["source"]["name"] == "s2"
    assert sources.clip_source(ep, "s1-c01")["session"] == "s1"
    plan = sources.plan(ep)
    assert sorted(c["clip"] for c in plan["approved_clips"]) == ["s1-c01", "s2-c01"]
    assert {c["clip"]: c["session"] for c in plan["approved_clips"]} == {"s1-c01": "s1", "s2-c01": "s2"}


# ---- captions ------------------------------------------------------------------------------------------------------

def test_captions_name_is_the_same_as_stage_rough_colon_name(ep, capsys):
    a = cli(capsys, "captions", ep.root, "--name", "s2", "--style", "podcast")
    assert a["edit"] == "rough.s2.v2.json" and (ep.root / a["srt"]).name == "rough.s2.srt"
    b = cli(capsys, "captions", ep.root, "--stage", "rough:s2", "--style", "podcast")
    assert b["edit"] == "rough.s2.v3.json" and b["srt"] == a["srt"]
    assert not editdoc.list_versions(ep, "rough", "s1")[1:]           # s1 untouched


def test_captions_without_a_name_on_a_multi_session_episode_lists_the_sessions(ep):
    with pytest.raises(SystemExit, match="s1, s2"):
        main(["captions", str(ep.root)])
    with pytest.raises(SystemExit, match="s1, s2"):
        main(["captions", str(ep.root), "--stage", "final"])


def test_captions_conflicting_name_and_stage_is_an_error(ep):
    with pytest.raises(SystemExit, match="rough:s1"):
        main(["captions", str(ep.root), "--stage", "rough:s1", "--name", "s2"])
    main(["captions", str(ep.root), "--stage", "rough:s1", "--name", "s1"])       # the same name twice is fine


def test_captions_for_a_select_need_no_session_name(ep, capsys):
    both(ep)
    clips.build(ep, name="s2")
    slug = next(c["slug"] for c in all_clips(ep) if c["id"] == "s2-c01")
    out = cli(capsys, "captions", ep.root, "--stage", f"select:{slug}", "--style", "short")
    assert out["edit"].startswith(f"select.{slug}.v")


def test_single_session_captions_still_default_to_rough(synth_episode, capsys):
    assert main(["captions", str(synth_episode.root), "--stage", "assembly"]) == 0
    assert json.loads(capsys.readouterr().out)["stage"] == "assembly"
