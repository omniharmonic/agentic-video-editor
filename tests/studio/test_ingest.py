"""Ingest paths (spec 9): idea, blog, audio, video (and the one-camera question of 9.3), episode, adopt. No network; ffmpeg for the media ones."""

import json
import shutil
from pathlib import Path

import pytest
from conftest import needs_ffmpeg
from sched_helpers import no_agent_markers  # noqa: F401
from studio_helpers import FakeClock, RecordingPublisher, make_episode_with_queue, write_account

from ave import cli
from ave.episode import Episode, read_json, write_json
from ave.studio import ingest, store
from ave.studio.project import StudioProject
from ave.studio.service import Studio, open_studio
from ave.platforms import attest


@pytest.fixture
def sp(tmp_path):
    p = StudioProject(tmp_path / "ws" / "studio")
    p.create(title="Acme Bakery", tz="America/Denver")
    return p


# ---------------------------------------------------------------------------------------------------------------- idea / blog

def test_idea_makes_an_idea_post_with_its_origin(sp):
    st = open_studio(sp.root)
    post = ingest.idea(st, "  Starter day 3, the pears smell  ", reason="evergreen", confidence=0.7)
    assert post["stage"] == "idea" and post["idea"]["text"] == "Starter day 3, the pears smell" and post["idea"]["origin"]["type"] == "idea"
    assert post["idea"]["confidence"] == 0.7
    with pytest.raises(ValueError, match="few words"):
        ingest.idea(st, "   ")


def test_blog_ingest_splits_passages_and_never_fetches_a_url(sp, tmp_path):
    md = tmp_path / "essay.md"
    md.write_text("# Bread\n\nFirst paragraph here. It has two sentences.\n\nSecond paragraph stands alone.\n")
    res = ingest.blog(sp, md)
    assert res["file"] == "essay.md" and res["title"] == "Bread" and [p["id"] for p in res["passages"]] == ["p001", "p002"]
    assert (sp.work / "writing" / "essay.md").read_text() == md.read_text() and md.read_text().startswith("# Bread")     # the original is only read
    for url in ("https://example.com/post", "http://example.com/x.md"):
        with pytest.raises(ValueError, match="not fetched"):
            ingest.blog(sp, url)
    (tmp_path / "x.pdf").write_bytes(b"%PDF")
    with pytest.raises(ValueError, match="Markdown"):
        ingest.blog(sp, tmp_path / "x.pdf")
    md.write_text("# Bread\n\nA changed first paragraph.\n")
    again = ingest.blog(sp, md)                                   # different bytes under the same name: a new file, never an overwrite
    assert again["file"] != "essay.md" and again["file"].startswith("essay-") and (sp.work / "writing" / "essay.md").exists()


# ---------------------------------------------------------------------------------------------------------------- audio

@needs_ffmpeg
def test_audio_ingest_creates_a_normal_episode_and_copies_the_file(sp, tmp_path):
    from ave.synth import build_media
    raw = tmp_path / "drop"
    build_media(raw)
    wav = raw / "recorder.wav"
    before = wav.read_bytes()
    res = ingest.audio(sp, wav, name="pod-001")
    ep = Episode(res["episode"])
    assert res["created"] and ep.root == sp.root.parent / "pod-001" and (ep.raw / "recorder.wav").read_bytes() == before
    assert wav.read_bytes() == before and wav.exists()                                       # copy, never move
    assert read_json(ep.artifact("media.json"))["files"][0]["kind"] == "audio" and res["analysis_audio"]
    assert any("ave transcribe" in c for c in res["next"])
    again = ingest.audio(sp, wav, name="pod-001")                                            # idempotent: same bytes, same episode
    assert not again["created"]
    other = tmp_path / "other" / "recorder.wav"
    other.parent.mkdir()
    other.write_bytes(before + b"\0")
    with pytest.raises(ValueError, match="different bytes"):                                  # same name, other bytes: never overwritten
        ingest.audio(sp, other, name="pod-001")


# ---------------------------------------------------------------------------------------------------------------- one camera (spec 9.3)

@needs_ffmpeg
def test_one_camera_video_path_fixture_then_shim(sp, tmp_path):
    """Spec 9.3 / WS7's first task, answered. On a synthetic episode with ONE camera that carries its own audio and NO recorder:
    plan-cuts, assemble, cleanup and render all work; the single failure is `ave sync` ("Need at least two files with audio"), and the planner
    expects a `cameras` entry. The shim in `ingest video` writes exactly those (a one-file sync.json and `cameras`), the planner is unchanged."""
    from ave import assemble, audio, cleanup, cutplan, editdoc, media, sync
    from ave.synth import build_media, transcript
    drop = tmp_path / "drop"
    build_media(drop)
    cam = drop / "camA.mp4"

    # (1) the fixture, without the shim: what the multicam path does with one camera
    solo = Episode(tmp_path / "bare")
    solo.create(title="bare")
    shutil.copy(cam, solo.raw / "camA.mp4")
    media.ingest(solo)
    audio.extract_all(solo)
    with pytest.raises(SystemExit, match="at least two files"):
        sync.sync_episode(solo)                                              # the one thing that fails

    # (2) `ave studio ingest video` = copy + probe + audio + the shim
    res = ingest.video(sp, cam, name="solo")
    assert res["single_camera"]["applied"] and set(res["single_camera"]["changed"]) >= {"cameras", "sync.json"}
    ep = Episode(res["episode"])
    meta = read_json(ep.meta_path)
    f = "raw/camA.mp4"
    assert meta["cameras"] == {"A": {"file": f, "role": "host"}} and meta["audio_source"] == f
    assert read_json(ep.artifact("sync.json"))["files"] == {f: {"start_offset": 0.0, "psr": None, "drift_ms": 0.0}}
    # idempotent
    assert ingest.video(sp, ep.root)["single_camera"]["changed"] == []
    assert (cam.exists() and (ep.raw / "camA.mp4").exists())                 # the original is untouched

    # (3) after transcription the same command maps every transcript speaker to the one camera
    write_json(ep.artifact("transcript.json"), transcript(f))
    res2 = ingest.video(sp, ep.root)
    assert res2["single_camera"]["changed"] == ["speakers"]
    assert {k: v["camera"] for k, v in read_json(ep.meta_path)["speakers"].items()} == {"S1": "A", "S2": "A"}

    # (4) the unchanged planner, assembly, cleanup and render run on it
    cl = cutplan.build_cutlist(ep)
    assert cl["shots"] if "shots" in cl else cl
    editdoc.save_new_version(ep, assemble.build(ep, (640, 360)))
    totals = cleanup.run(ep)["totals"]
    assert totals["output_duration_s"] > 0 and totals["output_duration_s"] <= 30
    assert cli.main(["render", str(ep.root), "--stage", "rough", "--preset", "preview"]) == 0
    assert list(ep.exports.glob("rough.v1.preview.mp4"))


def test_single_camera_shim_declines_what_is_not_one_camera(sp, tmp_path):
    ep = Episode(tmp_path / "multi")
    ep.create(title="multi")
    write_json(ep.artifact("media.json"), {"files": [{"file": "raw/a.mp4", "kind": "video"}, {"file": "raw/b.mp4", "kind": "video"}]})
    r = ingest.single_camera_shim(ep)
    assert not r["applied"] and "exactly one video" in r["why"]
    write_json(ep.artifact("media.json"), {"files": [{"file": "raw/a.mp4", "kind": "video"}, {"file": "raw/rec.wav", "kind": "audio"}]})
    assert not ingest.single_camera_shim(ep)["applied"]                        # a separate recorder: the real multicam path


# ---------------------------------------------------------------------------------------------------------------- episode survey and adopt

def test_episode_survey_lists_what_is_there_read_only(sp, tmp_path):
    ep, item = make_episode_with_queue(tmp_path)
    write_json(ep.artifact("clips.json"), {"clips": [{"id": "c01", "title": "Soil", "status": "approved", "duration": 30, "text": "words"},
                                                      {"id": "c02", "title": "Nope", "status": "proposed", "text": "x"}]})
    res = ingest.episode(sp, ep.root)
    assert [c["id"] for c in res["approved_clips"]] == ["c01"] and res["queue"][0]["id"] == item["id"] and res["packages"] == []


def adopt_rig(tmp_path, approved: bool):
    clock = FakeClock("2026-10-19T16:00:00Z")
    sp = StudioProject(tmp_path / "ws" / "studio")
    sp.create(title="Acme Bakery", tz="America/Denver")
    write_account(sp, "ig-main", "instagram", mode="graph")
    pub = RecordingPublisher("instagram")
    st = Studio(sp, clock=clock, publishers={"ig-main": pub})
    from ave.schedule import register
    register(st)
    ep, item = make_episode_with_queue(tmp_path, approve=approved)
    return clock, sp, st, pub, ep, item


def test_adopt_makes_posts_with_unattested_claims_and_no_link_to_the_episode_queue(tmp_path):
    clock, sp, st, pub, ep, item = adopt_rig(tmp_path, approved=False)
    res = ingest.adopt(st, ep.root, by="claude")
    assert len(res["adopted"]) == 1 and res["adopted"][0]["item"] == item["id"] and res["skipped"] == []
    post = store.load_post(sp, res["adopted"][0]["post"])
    v = post["variants"][0]
    assert v["account"] == "ig-main" and not v.get("queue")                                   # the episode's approval does not transfer
    assert post["idea"]["origin"]["type"] == "episode" and post["media"][0]["alt"] == "A card announcing the new episode."
    asset = store.read_assets(sp)[post["media"][0]["asset"]]
    assert asset["source"]["type"] == "own" and (sp.root / asset["file"]).read_bytes() == (ep.exports / "card.png").read_bytes()    # copied by sha
    assert (ep.exports / "card.png").exists()                                                # never moved
    assert asset["license"]["attested"] is False                                             # a claim by `claude`, not a human's word
    from ave import library
    assert not library.coverage(sp, [asset["id"]])["ok"] and [r["asset"] for r in library.unattested(sp)] == [asset["id"]]
    assert st.post(post["id"])["variants"][0]["status"] == "draft"                            # no queue item yet
    assert pq_stamp(ep, item)["post"] == post["id"]                                           # the episode item is stamped: `ave publish run` refuses it
    again = ingest.adopt(st, ep.root)
    assert again["adopted"] == [] and again["skipped"][0]["why"] == "already adopted"          # origin key prevents double adoption


def test_adopt_reports_items_with_no_account_instead_of_guessing(tmp_path):
    clock, sp, st, pub, ep, item = adopt_rig(tmp_path, approved=False)
    (sp.accounts_dir / "ig-main.yaml").unlink()
    res = ingest.adopt(st, ep.root)
    assert res["adopted"] == [] and "no studio account for instagram" in res["skipped"][0]["why"]


def test_an_adopted_item_is_sent_only_through_a_fresh_studio_approval_exactly_once(tmp_path):
    from ave import library
    from ave.publish import queue as pq
    from ave.schedule.tick import tick
    from ave.studio import dispatch, sendgate
    from ave.studio.sync import sync_queue
    clock, sp, st, pub, ep, item = adopt_rig(tmp_path, approved=True)
    sendgate.write(sp, "maren")
    res = ingest.adopt(st, ep.root)
    pid, vid = res["adopted"][0]["post"], res["adopted"][0]["variants"][0]
    post = store.load_post(sp, pid)
    library.attest_license(sp, post["media"][0]["asset"], "maren")                              # the human stands behind the license claim
    attest.write_live(sp.accounts_dir, "ig-main", "maren")
    clock.set("2026-10-26T14:00:00Z")
    sync_queue(sp, pid, publishers={"ig-main": pub}, now=clock.now())
    v = store.load_post(sp, pid)["variants"][0]
    own = pq.load(sp, v["queue"]["id"])
    assert v["queue"]["root"] == "." and own["meta_v"] == 2 and own["id"] != item["id"] and own["status"] == "awaiting_approval"
    pq.approve(sp, own["id"], "maren", own["sha256"][:12], publishers={"instagram": pub}, bind=dispatch.approval_bind(sp, v))
    st.move(pid, vid, {"local": "2026-10-26T09:00", "tz": "America/Denver"}, expected_rev=store.load_post(sp, pid)["rev"], snap="none")
    clock.set("2026-10-26T15:00:30Z")
    rep = tick(sp, clock=clock, send=True, publishers={"ig-main": pub})
    assert len(rep.posted) == 1 and pub.publish_calls == 1
    assert pq_status(sp, own) == "published" and pq_status(ep, item) == "approved"           # the episode's own approved item was never touched
    assert tick(sp, clock=clock, send=True, publishers={"ig-main": pub}).posted == [] and pub.publish_calls == 1


def pq_stamp(ep, item):
    from ave.publish import queue as pq
    return pq.load(ep, item["id"])["studio_bound"]


def pq_status(ep, item):
    from ave.publish import queue as pq
    return pq.load(ep, item["id"])["status"]
