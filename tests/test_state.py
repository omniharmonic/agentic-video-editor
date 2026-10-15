import json

from ave import state
from ave.episode import Episode, write_json


def test_fresh_episode_next_is_ingest(tmp_path):
    ep = Episode(tmp_path); ep.create()
    st = state.status(ep)
    assert st["next"]["stage"] == "ingest"
    assert "NEXT: Probe footage" in state.briefing(ep)


def test_state_follows_artifacts_not_flags(tmp_path):
    ep = Episode(tmp_path); ep.create()
    ep.mark_stage("ingest")                      # flag set but no artifact: must NOT count as done
    assert not next(r for r in state.status(ep)["stages"] if r["stage"] == "ingest")["done"]
    write_json(ep.artifact("media.json"), {"files": [{"kind": "video"}]})
    assert next(r for r in state.status(ep)["stages"] if r["stage"] == "ingest")["done"]


def test_single_camera_skips_sync_and_review_is_reported(tmp_path):
    ep = Episode(tmp_path); ep.create()
    write_json(ep.artifact("media.json"), {"files": [{"kind": "video", "file": "raw/a.mov"}]})
    st = state.status(ep)
    assert st["single_camera"] and next(r for r in st["stages"] if r["stage"] == "sync")["skipped"]
    state.set_review(ep, "check the rough cut")
    assert "Waiting on the human: check the rough cut" in state.briefing(ep)
