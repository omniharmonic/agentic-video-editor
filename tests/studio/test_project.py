"""StudioProject is an Episode: the existing queue functions run on it; `ave studio init` / `status`; adoption by reference."""

import json
from datetime import datetime, timezone

import pytest
from studio_helpers import RecordingPublisher, make_episode_with_queue, make_png

from ave import cli, workspace
from ave.episode import Episode
from ave.publish import queue as pq
from ave.studio import state
from ave.studio.model import new_variant
from ave.studio.project import StudioProject, find_studio


def test_studio_project_is_an_episode_with_its_own_marker(bare):
    assert isinstance(bare, Episode)
    assert bare.meta_path == bare.root / "studio.json" and not (bare.root / "episode.json").exists()
    assert bare.publish == bare.root / "publish" and pq.queue_dir(bare) == bare.root / "publish" / "queue"
    assert bare.raw_dirs == [bare.root / "inbox"] and bare.exports == bare.root / "exports"
    for d in ("posts", "campaigns", "accounts", "ledger", "library/assets", "schedule/proposals", "schedule/state", "policy", "recipes",
              "work", "exports", "inbox", "publish/queue"):
        assert (bare.root / d).is_dir(), d
    assert bare.rel(bare.exports / "a.png") == "exports/a.png"


def test_create_is_idempotent_and_never_rewrites_studio_json(tmp_path):
    sp = StudioProject(tmp_path / "s")
    sp.create(title="One", tz="America/Denver")
    first = sp.meta_path.read_text()
    sp.create(title="Two", tz="UTC")
    assert sp.meta_path.read_text() == first and sp.read_meta()["title"] == "One"
    assert sp.config()["approval"]["ttl_days"] == 14 and sp.config()["late_after_min"] == 120


def test_require_says_how_to_fix_it(tmp_path):
    with pytest.raises(SystemExit, match="ave studio init"):
        StudioProject(tmp_path / "nope").require()


def test_workspace_episodes_ignores_a_studio(tmp_path, monkeypatch):
    monkeypatch.setenv("AVE_WORKSPACE", str(tmp_path))
    Episode(tmp_path / "ep-001").create("E")
    StudioProject(tmp_path / "studio").create(title="S")
    assert [p.name for p in workspace.episodes()] == ["ep-001"]
    assert find_studio().root == (tmp_path / "studio").resolve()


def test_queue_create_approve_with_typed_sha_and_dry_run_on_a_studio(bare):
    png = make_png(bare.exports / "card.png")
    pubs = {"instagram": RecordingPublisher("instagram")}
    item = pq.create(bare, files=[png], media_type="image", platforms=["instagram"], title="Card", caption="Hello",
                     alt_texts=["A card that says hello."], ai_disclosure=False, meta_v=2, account="ig-main", publishers=pubs)
    assert item["status"] == "awaiting_approval" and (bare.root / "publish" / "queue" / f"{item['id']}.json").exists()
    with pytest.raises(ValueError, match="confirmation hash"):
        pq.approve(bare, item["id"], "maren", "deadbeefdead", publishers=pubs)               # a wrong typed sha is refused
    done = pq.approve(bare, item["id"], "maren", item["sha256"][:10], publishers=pubs)       # the human typed >= 8 hex of the real sha
    assert done["status"] == "approved" and done["approvals"][-1]["sha256"] == item["sha256"]
    dry = pq.run(bare, item["id"], publishers=pubs)
    assert dry["mode"] == "dry-run" and dry["ok"] and pubs["instagram"].publish_calls == 0
    sent = pq.run(bare, item["id"], send=True, publishers=pubs)
    assert sent["ok"] and pubs["instagram"].publish_calls == 1 and pubs["instagram"].sent[0]["account"] == "ig-main"


def test_a_not_live_account_adapter_blocks_the_send_even_when_approved(bare):
    png = make_png(bare.exports / "card.png")
    item = pq.create(bare, files=[png], media_type="image", platforms=["instagram"], title="Card", alt_texts=["alt"], ai_disclosure=False,
                     publishers={"instagram": RecordingPublisher("instagram")})
    pq.approve(bare, item["id"], "maren", item["sha256"][:12], publishers={"instagram": RecordingPublisher("instagram")})
    off = RecordingPublisher("instagram", live=False)
    rep = pq.run(bare, item["id"], send=True, publishers={"instagram": off})
    assert not rep["ok"] and off.publish_calls == 0 and any("not live" in b for b in rep["blocked"])


def test_adopted_queue_items_are_found_by_reference(tmp_path, bare):
    ep, item = make_episode_with_queue(tmp_path, approve=True)
    v = new_variant(account="ig-main", platform="instagram", post_type="feed")
    v["queue"] = {"root": str(ep.root), "id": item["id"]}
    loaded = state.load_queue_item(bare, v)
    assert loaded["id"] == item["id"] and loaded["status"] == "approved"
    assert state.derive(v, loaded, [], datetime(2026, 10, 19, tzinfo=timezone.utc), bare) == "approved"
    v["queue"]["id"] = "nope"
    assert state.load_queue_item(bare, v) is None


def test_cli_init_and_status(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("AVE_WORKSPACE", raising=False)
    target = tmp_path / "acme"
    assert cli.main(["studio", "init", str(target), "--name", "Acme Bakery", "--tz", "America/Denver"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["created_studio_json"] and out["title"] == "Acme Bakery"
    meta = json.loads((target / "studio.json").read_text())
    assert meta["tz"] == "America/Denver" and meta["kind"] == "studio" and meta["approval"]["mode"] == "pin_after_review"
    from ave.studio import model
    assert model.validate("studio", meta) == []
    assert cli.main(["studio", "init", str(target), "--name", "Other"]) == 0                    # idempotent
    again = json.loads(capsys.readouterr().out)
    assert not again["created_studio_json"] and json.loads((target / "studio.json").read_text())["title"] == "Acme Bakery"
    assert cli.main(["studio", "status", str(target)]) == 0
    st = json.loads(capsys.readouterr().out)
    assert st["ok"] and st["posts"] == 0 and st["studio"] == str(target.resolve()) and st["cursor"] == "ev:0"


def test_cli_status_on_a_non_studio_refuses(tmp_path, capsys):
    assert cli.main(["studio", "status", str(tmp_path / "empty")]) == 2
    cap = capsys.readouterr()
    assert "ave studio init" in cap.err and json.loads(cap.out)["ok"] is False


def test_cli_init_without_dir_or_workspace_exits_2(capsys, monkeypatch, tmp_path):
    monkeypatch.delenv("AVE_WORKSPACE", raising=False)
    monkeypatch.setattr(workspace, "CONFIG", tmp_path / "none.json")
    assert cli.main(["studio", "init"]) == 2
