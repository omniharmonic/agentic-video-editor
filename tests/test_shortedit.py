"""`ave shorts recut` / `retitle`: the two edits to a finished short that `ave shorts` had no way to make, on the synthetic demo episode taken to an
approved, exported short through the real commands. Word boundaries and the complete-statement gate are measured on the transcript."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import shorts_flow_helpers as F
from ave import completeness, editdoc, ffutil, shortedit, shorts
from ave import clips as C
from ave.episode import read_json, write_json
from conftest import needs_ffmpeg, needs_libass

pytestmark = needs_ffmpeg


@pytest.fixture(scope="module")
def flow_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("shortflow") / "demo"
    F.demo_with_short(root)
    return root


@pytest.fixture
def ep(flow_root, tmp_path):
    return F.copy_episode(flow_root, tmp_path / "demo")


def sha(p) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def clip_of(ep):
    return shorts.select_clips(ep, "c01")[0]


def first_speech(doc):
    return min((s for s in doc["tracks"]["dialogue"] if not s.get("fill")), key=lambda s: s["o0"])


def test_the_fixture_is_an_approved_short_of_complete_sentences(ep):
    clip = clip_of(ep)
    assert clip["status"] == "approved" and clip["text"].startswith("Most people never finish")
    doc = editdoc.load_latest(ep, "short", clip["slug"])
    assert doc["short"]["completeness"]["complete"] and editdoc.list_versions(ep, "short", clip["slug"]) == [1]


def test_dry_run_names_the_new_edges_and_writes_nothing(ep):
    clip = clip_of(ep)
    before = sha(ep.work / "clips.json")
    r = shortedit.recut(ep, "c01", drop_start_words=7, dry_run=True)
    assert r["dry_run"] and r["complete"] is True and r["would"] == "build"
    assert r["first_words"].startswith("That is exactly right") and r["last_words"].endswith("about.")
    assert r["dropped_start_words"] == ["Most", "people", "never", "finish", "what", "they", "start."] and r["dropped_end_words"] == []
    assert r["new_range"][0] > clip["start"] and r["new_duration"] < clip["duration"]
    assert editdoc.list_versions(ep, "short", clip["slug"]) == [1] and editdoc.list_versions(ep, "select", clip["slug"]) == [1]
    assert sha(ep.work / "clips.json") == before


def test_a_trim_that_lands_mid_sentence_is_refused_with_both_edges_quoted(ep):
    clip = clip_of(ep)
    r = shortedit.recut(ep, "c01", drop_start_words=2, dry_run=True)
    assert r["complete"] is False and "mid-sentence" in r["completeness_note"] and r["would"].startswith("refuse")
    with pytest.raises(SystemExit) as e:
        shortedit.recut(ep, "c01", drop_start_words=2)
    msg = str(e.value.code)
    assert "not a complete statement" in msg and "would start" in msg and "never finish what" in msg and "--dry-run" in msg
    assert editdoc.list_versions(ep, "short", clip["slug"]) == [1] and editdoc.list_versions(ep, "select", clip["slug"]) == [1]     # nothing was written


def test_recut_start_builds_a_new_select_and_short_and_leaves_the_approved_clip_alone(ep):
    clip = clip_of(ep)
    slug = clip["slug"]
    clips_before = sha(ep.work / "clips.json")
    select1 = sha(editdoc.path_for(ep, "select", 1, slug))
    short1 = sha(editdoc.path_for(ep, "short", 1, slug))
    r = shortedit.recut(ep, "c01", drop_start_words=7, by="Maren")
    assert r["complete"] and r["built"]["version"] == 2 and r["built"]["status"] == "built"
    # new versions; the old ones and clips.json are byte-identical
    assert editdoc.list_versions(ep, "select", slug) == [1, 2] and editdoc.list_versions(ep, "short", slug) == [1, 2]
    assert sha(ep.work / "clips.json") == clips_before and sha(editdoc.path_for(ep, "select", 1, slug)) == select1 and sha(editdoc.path_for(ep, "short", 1, slug)) == short1
    doc = editdoc.load_latest(ep, "short", slug)
    assert editdoc.validate(doc) == []
    rc = doc["short"]["recut"]
    assert rc["by"] == "Maren" and rc["from_version"] == 1 and rc["dropped_start_words"][0] == "Most" and rc["select"].endswith("select." + slug + ".v2.json")
    # the cut starts on a word gap: no word is sliced, the first word heard is "That"
    words = completeness.transcript_words(ep)
    rep = completeness.edge_report(doc, words)
    assert rep["start"]["slices_word"] is None and rep["end"]["slices_word"] is None and not rep["interior_slices"]
    assert doc["short"]["edges"]["first_word"] == "That" and doc["short"]["completeness"]["complete"]
    assert doc["duration"] < editdoc.load(editdoc.path_for(ep, "short", 1, slug))["duration"] - 2.5
    # the human has not seen this range: it says so, in the doc and the notes
    assert any("recut by Maren" in x and "human has not seen" in x for x in doc["short"]["needs_review"])
    # captions were rebuilt for the new range
    assert (ep.root / doc["captions"]["srt"]).read_text().lstrip().startswith("1") and "Most" not in (ep.root / doc["captions"]["srt"]).read_text()
    log = (ep.root / "PROGRESS.md").read_text()
    assert "RECUT short" in log


def test_recut_end_by_words_and_by_seconds_agree_and_end_on_a_full_stop(ep):
    clip = clip_of(ep)
    by_words = shortedit.recut(ep, "c01", drop_end_words=10, dry_run=True)
    by_seconds = shortedit.recut(ep, "c01", trim_end_s=4.0, dry_run=True)
    assert by_words["new_range"] == by_seconds["new_range"] and by_words["last_words"].endswith("forward.")
    assert by_words["complete"] and by_seconds["dropped_end_words"] == by_words["dropped_end_words"] and len(by_words["dropped_end_words"]) == 10
    r = shortedit.recut(ep, "c01", drop_end_words=10)
    doc = editdoc.load_latest(ep, "short", clip["slug"])
    assert doc["short"]["edges"]["last_word"] == "forward." and r["new_duration"] == pytest.approx(by_words["new_duration"])
    assert doc["short"]["recut"]["dropped_end_words"][-1].endswith("about.")


def test_recut_both_ends_and_the_minimum_length(ep):
    both = shortedit.recut(ep, "c01", drop_start_words=7, drop_end_words=10, dry_run=True)
    assert both["complete"] and 7 < both["new_duration"] < 9
    rub = read_json(ep.work / "clips.json")
    rub["rubric"]["limits"]["hard_min_s"] = 12
    write_json(ep.work / "clips.json", rub)
    with pytest.raises(SystemExit, match="under the 12s minimum"):
        shortedit.recut(ep, "c01", drop_start_words=7, drop_end_words=10)


def test_recut_only_moves_inward_and_needs_something_to_do(ep):
    with pytest.raises(SystemExit, match="only moves a boundary INWARD"):
        shortedit.recut(ep, "c01", drop_start_words=-1)
    with pytest.raises(SystemExit, match="nothing to trim"):
        shortedit.recut(ep, "c01")


def test_recut_refuses_an_unapproved_clip_and_a_stale_one(ep):
    data = read_json(ep.work / "clips.json")
    data["clips"][0]["status"] = "proposed"
    write_json(ep.work / "clips.json", data)
    with pytest.raises(SystemExit, match="not approved"):
        shortedit.recut(ep, "c01", drop_start_words=7)
    data["clips"][0]["status"] = "approved"
    write_json(ep.work / "clips.json", data)
    # the edit the clip was picked from moved on: its sentence ids mean nothing any more
    tr = read_json(ep.work / "transcript.json")
    tr["words"][3]["w"] = "changed"
    write_json(ep.work / "transcript.json", tr)
    with pytest.raises(SystemExit, match="stale"):
        shortedit.recut(ep, "c01", drop_start_words=7)


def test_allow_incomplete_is_the_logged_override(ep):
    r = shortedit.recut(ep, "c01", drop_start_words=2, allow_incomplete=True)
    doc = editdoc.load_latest(ep, "short", r["slug"])
    assert doc["short"]["completeness"]["override"] is True and doc["short"]["completeness"]["problems"]
    assert "OVERRIDE --allow-incomplete" in (ep.root / "PROGRESS.md").read_text()


@needs_libass
def test_a_recut_short_exports_as_a_new_version_flagged_for_review(ep):
    clip = clip_of(ep)
    shortedit.recut(ep, "c01", drop_start_words=7)
    res = shorts.export_short(ep, clip, ["reels"], shorts.load_config(ep))
    assert res["version"] == 2 and res["exports"][0]["status"] == "exported" and not res["exports"][0]["problems"]
    side = read_json(ep.root / res["exports"][0]["sidecar"])
    assert side["needs_review"] and any("recut by" in x for x in side["needs_review"]) and side["version"] == 2
    assert side["completeness"]["complete"] and (ep.exports / "shorts" / f"{clip['slug']}.v1.reels.mp4").is_file()       # v1 export untouched
    info = ffutil.probe(ep.root / side["video"])
    assert abs(info["duration"] - side["duration"]) < 0.1 and side["duration"] < clip["duration"] - 2.5


# ---- retitle -----------------------------------------------------------------------------------------------------------------------------

def test_retitle_swaps_only_the_title_overlay_in_a_new_version(ep):
    clip = clip_of(ep)
    slug = clip["slug"]
    v1 = editdoc.load_latest(ep, "short", slug)
    old_title = v1["short"]["opening"]["title"]
    r = shortedit.retitle(ep, "c01", "Why finishing is a habit", by="Maren")
    assert r["version"] == 2 and r["was"] == old_title and r["title"] == "Why finishing is a habit"
    v2 = editdoc.load_latest(ep, "short", slug)
    assert editdoc.validate(v2) == [] and editdoc.list_versions(ep, "short", slug) == [1, 2]
    # crops, captions, timing: all copied as they were
    assert v2["tracks"]["video"] == v1["tracks"]["video"] and v2["tracks"]["dialogue"] == v1["tracks"]["dialogue"]
    assert v2["captions"] == v1["captions"] and v2["duration"] == v1["duration"]
    t1 = next(o for o in v1["tracks"]["overlays"] if o.get("template") == "short-title")
    t2 = next(o for o in v2["tracks"]["overlays"] if o.get("template") == "short-title")
    assert t2["props"]["title"] == "Why finishing is a habit" and t2["file"] != t1["file"] and (t2["o0"], t2["o1"]) == (t1["o0"], t1["o1"])
    assert v2["short"]["opening"]["title"] == "Why finishing is a habit" and v2["short"]["opening"]["title_source"] == "shorts retitle"
    assert v2["short"]["retitle"]["from"] == old_title and v2["short"]["retitle"]["by"] == "Maren"
    assert json.loads((ep.work / "shorts" / "titles.json").read_text())[slug] == "Why finishing is a habit"      # a later build --force keeps it
    assert opening_overlays(v2) == 1                                                                              # swapped, not stacked


def opening_overlays(doc) -> int:
    return len([o for o in doc["tracks"]["overlays"] if o.get("template") == "short-title"])


@pytest.mark.parametrize("title,msg", [
    ("Unlock the game-changer", "hype phrase"),
    ("Most people never finish what they start", "speaker's own words"),
    ("Is this why we never finish?", "question"),
    ("A title that is far too long to be a topic and keeps going and going", "characters"),
    ("", "no title text"),
])
def test_retitle_runs_the_same_critic_as_the_build(ep, title, msg):
    clip = clip_of(ep)
    with pytest.raises(SystemExit, match=msg):
        shortedit.retitle(ep, "c01", title)
    assert editdoc.list_versions(ep, "short", clip["slug"]) == [1]


def test_retitle_needs_a_title_opening(ep):
    clip = clip_of(ep)
    shorts.build_short(ep, clip, shorts.load_config(ep), detector=F.NO_FACE, opening_mode="none", force=True)
    with pytest.raises(SystemExit, match="no title opening to swap"):
        shortedit.retitle(ep, "c01", "A plain topic")


def test_a_recut_then_a_retitle_keep_the_recut(ep):
    clip = clip_of(ep)
    shortedit.recut(ep, "c01", drop_start_words=7)
    shortedit.retitle(ep, "c01", "Why systems matter")
    doc = editdoc.load_latest(ep, "short", clip["slug"])
    assert editdoc.list_versions(ep, "short", clip["slug"]) == [1, 2, 3]
    assert doc["short"]["recut"] and doc["short"]["retitle"] and doc["short"]["edges"]["first_word"] == "That"


def test_cli_recut_and_retitle(ep, capsys):
    rc, res = F.ave("shorts", "recut", str(ep.root), "--clip", "c01", "--drop-start-words", "7", "--dry-run")
    assert rc == 0 and res["dry_run"] and res["complete"]
    rc, res = F.ave("shorts", "recut", str(ep.root), "--clip", "c01", "--drop-start-words", "2", expect=1)
    assert rc == 1 and "not a complete statement" in res["error"]
    rc, res = F.ave("shorts", "recut", str(ep.root), "--clip", "c01", "--drop-start-words", "7", "--by", "Maren")
    assert rc == 0 and res["built"]["version"] == 2
    rc, res = F.ave("shorts", "retitle", str(ep.root), "--clip", "c01", "--title", "How systems carry the work")
    assert rc == 0 and res["version"] == 3
    rc, res = F.ave("shorts", "list", str(ep.root))
    assert res[0]["versions"] == [1, 2, 3]
