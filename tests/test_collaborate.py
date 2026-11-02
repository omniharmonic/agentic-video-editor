"""Offline editorial reconciliation; no sockets, accounts, approvals or raw footage."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ave import collab
from ave.studio import model, store
from ave.studio.events import Actor
from ave.studio.project import StudioProject

ACTOR = Actor("agent", "test")


@pytest.fixture
def pair(tmp_path):
    a, b = StudioProject(tmp_path / "maren"), StudioProject(tmp_path / "tomas")
    a.create()
    b.create()
    return a, b


def post(sp, pid="p_draft", text="First draft", **extra):
    p = model.new_post(post_id=pid, copy={"base": text})
    p.update(extra)
    return store.save_post(sp, p, expected_rev=None, actor=ACTOR, op="test.create")


def edit(sp, text):
    p = store.load_post(sp, "p_draft")
    p["copy"]["base"] = text
    return store.save_post(sp, p, expected_rev=p["rev"], actor=ACTOR, op="test.edit")


def export(sp, out, peer, **kw):
    return collab.export_bundle(sp, out, peer=peer, **kw)


def seed(pair, tmp_path):
    a, b = pair
    post(a)
    path = tmp_path / "seed.json"
    export(a, path, "tomas")
    assert collab.import_bundle(b, path, peer="maren", apply=True)["created"] == ["p_draft"]
    return a, b


def test_offline_roundtrip_restarts_idempotent_and_one_sided_changes(pair, tmp_path):
    a, b = seed(pair, tmp_path)
    edit(b, "Tomas's edit while offline")
    path = tmp_path / "return.json"
    export(StudioProject(b.root), path, "maren")
    preview = collab.import_bundle(a, path, peer="tomas")
    assert preview["updated"] == ["p_draft"] and not preview["applied"]
    assert store.load_post(a, "p_draft")["copy"]["base"] == "First draft"
    first = collab.import_bundle(a, path, peer="tomas", apply=True)
    assert first["updated"] == ["p_draft"] and first["applied"]
    rev = store.load_post(a, "p_draft")["rev"]
    assert store.load_post(a, "p_draft")["copy"]["base"] == "Tomas's edit while offline"
    again = collab.import_bundle(StudioProject(a.root), path, peer="tomas", apply=True)
    assert again["replayed"] and store.load_post(a, "p_draft")["rev"] == rev
    # The other direction after a successful reply uses a new common base.
    edit(a, "Maren's next draft")
    out = tmp_path / "next.json"
    export(a, out, "tomas")
    assert collab.import_bundle(b, out, peer="maren", apply=True)["updated"] == ["p_draft"]


def test_divergence_keeps_both_and_records_conflict_only_on_apply(pair, tmp_path):
    a, b = seed(pair, tmp_path)
    edit(a, "Maren's edit")
    edit(b, "Tomas's edit")
    path = tmp_path / "divergent.json"
    export(b, path, "maren")
    preview = collab.import_bundle(a, path, peer="tomas")
    assert preview["conflicts"][0]["post"] == "p_draft"
    assert not collab.status(a)["conflicts"]
    result = collab.import_bundle(a, path, peer="tomas", apply=True)
    conflict = json.loads(Path(result["conflicts"][0]["file"]).read_text())
    assert conflict["local"]["copy"]["base"] == "Maren's edit"
    assert conflict["incoming"]["copy"]["base"] == "Tomas's edit"
    assert store.load_post(a, "p_draft")["copy"]["base"] == "Maren's edit"
    assert collab.status(a)["conflicts"]


def test_unchanged_incoming_preserves_local_work(pair, tmp_path):
    a, b = seed(pair, tmp_path)
    edit(a, "Local changed")
    path = tmp_path / "unchanged.json"
    export(b, path, "maren")
    result = collab.import_bundle(a, path, peer="tomas", apply=True)
    assert not result["conflicts"] and result["unchanged"] == ["p_draft"]
    assert store.load_post(a, "p_draft")["copy"]["base"] == "Local changed"


def test_export_projects_only_editorial_fields_and_import_reconstructs_drafts(pair, tmp_path):
    a, b = pair
    v = model.new_variant(account="ig", platform="instagram", post_type="feed", variant_id="v_ig")
    v.update(queue={"root": "/PRIVATE", "id": "q_secret"}, queue_sha="secret", schedule={"anything": "secret"},
             result={"state": "published", "token": "secret"}, attempts=[{"at": "x", "outcome": "published"}],
             credentials="TOPSECRET", unknown={"path": "/PRIVATE"})
    # Operational fields intentionally malformed: export reads only its strict projection.
    p = post(a, variants=[])
    p["variants"] = [v]
    p.update(credentials="TOPSECRET", approval={"signature": "secret"}, idea={"text": "Idea", "origin": {"ref": "/PRIVATE"}})
    (a.posts_dir / "p_draft.json").write_text(json.dumps(p))
    path = tmp_path / "projected.json"
    export(a, path, "tomas")
    text = path.read_text()
    assert all(x not in text for x in ("TOPSECRET", "/PRIVATE", "q_secret", "attempts", "queue_sha", "signature"))
    collab.import_bundle(b, path, peer="maren", apply=True)
    p = store.load_post(b, "p_draft")
    assert p["variants"][0]["queue"] is None and p["variants"][0]["schedule"] is None
    assert p["variants"][0]["result"] is None and p["variants"][0]["attempts"] == []
    assert "needs_review" in p["variants"][0]["flags"]
    assert not list((b.publish / "queue").glob("*.json"))


def test_existing_queue_or_published_post_is_never_overwritten(pair, tmp_path):
    a, b = seed(pair, tmp_path)
    v = model.new_variant(account="ig", platform="instagram", post_type="feed", variant_id="v_ig")
    v["queue"] = {"root": ".", "id": "q_1"}
    p = store.load_post(a, "p_draft")
    p["variants"] = [v]
    store.save_post(a, p, expected_rev=p["rev"], actor=ACTOR, op="test.queue")
    edit(b, "Changed remotely")
    path = tmp_path / "queued.json"
    export(b, path, "maren")
    result = collab.import_bundle(a, path, peer="tomas", apply=True)
    assert result["conflicts"] and not result["updated"]
    assert store.load_post(a, "p_draft")["variants"][0]["queue"] == v["queue"]


def _resign(bundle):
    bundle["id"] = hashlib.sha256(json.dumps({k: v for k, v in bundle.items() if k != "id"}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    return bundle


@pytest.mark.parametrize("change", [
    lambda d: d["entries"][0]["content"].update(approval={"signature": "forged"}),
    lambda d: d["entries"][0].update(id="../../escape"),
    lambda d: d.update(source="not-a-uuid"),
    lambda d: d.update(files=[{"path": "/etc/passwd"}]),
    lambda d: d["entries"].append(d["entries"][0]),
])
def test_malicious_bundle_is_rejected_before_any_write(pair, tmp_path, change):
    a, b = pair
    post(a)
    path = tmp_path / "malicious.json"
    export(a, path, "tomas")
    d = json.loads(path.read_text())
    change(d)
    path.write_text(json.dumps(_resign(d)))
    with pytest.raises(ValueError):
        collab.import_bundle(b, path, peer="maren", apply=True)
    assert not store.list_posts(b) and not collab.status(b)["peers"]


def test_changed_bundle_same_id_rejected(pair, tmp_path):
    a, b = seed(pair, tmp_path)
    path = tmp_path / "seed.json"
    d = json.loads(path.read_text())
    d["entries"][0]["content"]["copy"]["base"] = "tampered"
    path.write_text(json.dumps(d))
    with pytest.raises(ValueError, match="checksum"):
        collab.import_bundle(b, path, peer="maren", apply=True)


def test_different_studio_cannot_reuse_bound_peer(pair, tmp_path):
    a, b = seed(pair, tmp_path)
    third = StudioProject(tmp_path / "stranger")
    third.create()
    post(third, text="Stranger")
    path = tmp_path / "stranger.json"
    export(third, path, "maren")
    with pytest.raises(ValueError, match="identity"):
        collab.import_bundle(b, path, peer="maren", apply=True)
    assert store.load_post(b, "p_draft")["copy"]["base"] == "First draft"


def test_unrelated_existing_post_collision_conflicts(pair, tmp_path):
    a, b = pair
    post(a, text="Incoming")
    post(b, text="Local unrelated")
    path = tmp_path / "collision.json"
    export(a, path, "tomas")
    result = collab.import_bundle(b, path, peer="maren", apply=True)
    assert result["conflicts"] and store.load_post(b, "p_draft")["copy"]["base"] == "Local unrelated"


def test_immutable_exports_and_symlink_input_output_and_posts_refused(pair, tmp_path):
    a, b = pair
    post(a)
    path = tmp_path / "bundle.json"
    export(a, path, "tomas")
    with pytest.raises(FileExistsError):
        export(a, path, "tomas")
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="symlink"):
        collab.import_bundle(b, link, peer="maren", apply=True)
    with pytest.raises(ValueError):
        export(a, a.root / "inbox" / "never.json", "tomas")
    target = tmp_path / "external-post.json"
    target.write_text((a.posts_dir / "p_draft.json").read_text())
    (a.posts_dir / "p_draft.json").unlink()
    (a.posts_dir / "p_draft.json").symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        export(a, tmp_path / "bad.json", "tomas")


def test_oversized_or_nonfinite_or_duplicate_json_keys_refused(pair, tmp_path, monkeypatch):
    a, b = pair
    path = tmp_path / "large.json"
    path.write_text(" " * 100)
    monkeypatch.setattr(collab, "MAX_BUNDLE_BYTES", 50)
    with pytest.raises(ValueError, match="size"):
        collab.import_bundle(b, path, peer="maren")
    path.write_text('{"v":1,"v":2}')
    with pytest.raises(ValueError, match="duplicate"):
        collab.import_bundle(b, path, peer="maren")


def test_import_cas_race_never_overwrites_new_local_revision(pair, tmp_path, monkeypatch):
    a, b = seed(pair, tmp_path)
    edit(b, "Remote")
    path = tmp_path / "race.json"
    export(b, path, "maren")
    real_save = store.save_post
    def race(sp, candidate, **kw):
        monkeypatch.setattr(store, "save_post", real_save)
        edit(a, "Concurrent local")
        return real_save(sp, candidate, **kw)
    monkeypatch.setattr(store, "save_post", race)
    result = collab.import_bundle(a, path, peer="tomas", apply=True)
    assert result["conflicts"] and store.load_post(a, "p_draft")["copy"]["base"] == "Concurrent local"


def test_optional_media_is_hash_checked_and_staged_without_license_claim(pair, tmp_path):
    from PIL import Image
    from ave.library import ingest, registry
    a, b = pair
    image = tmp_path / "picture.png"
    Image.new("RGB", (16, 16), "blue").save(image)
    asset = ingest.add_file(a, image, source={"type": "own"}, by="tester", attested=False)
    post(a, media=[{"slot": 0, "asset": asset["id"], "role": "image", "alt": "Blue square"}])
    path = tmp_path / "media.json"
    export(a, path, "tomas", include_media=True)
    result = collab.import_bundle(b, path, peer="maren", apply=True)
    assert result["missing_assets"] == [asset["id"]]
    assert len(result["staged_media"]) == 1
    staged = Path(result["staged_media"][0]["file"])
    assert staged.read_bytes() == image.read_bytes()
    assert not registry.read_all(b) and not (b.library_dir / "licenses.jsonl").exists()
    d = json.loads(path.read_text())
    d["assets"][0]["data"] = "aW52YWxpZA=="
    path.write_text(json.dumps(_resign(d)))
    with pytest.raises(ValueError, match="hash"):
        collab.import_bundle(b, path, peer="maren", apply=True)


def test_conflict_resolution_requires_current_revision_and_keeps_snapshots(pair, tmp_path):
    a, b = seed(pair, tmp_path)
    edit(a, "Local")
    edit(b, "Incoming")
    path = tmp_path / "conflict.json"
    export(b, path, "maren")
    result = collab.import_bundle(a, path, peer="tomas", apply=True)
    conflict_id = Path(result["conflicts"][0]["file"]).stem
    with pytest.raises(model.RevConflict):
        collab.resolve_conflict(a, conflict_id, take="incoming", expected_rev=1, apply=True)
    rev = store.load_post(a, "p_draft")["rev"]
    assert not collab.resolve_conflict(a, conflict_id, take="incoming", expected_rev=rev)["resolved"]
    assert store.load_post(a, "p_draft")["copy"]["base"] == "Local"
    collab.resolve_conflict(a, conflict_id, take="incoming", expected_rev=rev, apply=True)
    assert store.load_post(a, "p_draft")["copy"]["base"] == "Incoming"
    assert not collab.status(a)["conflicts"]
    saved = json.loads(Path(result["conflicts"][0]["file"]).read_text())
    assert saved["local"]["copy"]["base"] == "Local" and saved["resolution"]["take"] == "incoming"


def test_published_local_post_conflicts_without_erasing_history(pair, tmp_path):
    a, b = seed(pair, tmp_path)
    v = model.new_variant(account="ig", platform="instagram", post_type="feed", variant_id="v_ig")
    v["result"] = {"state": "published", "id": "live-post"}
    p = store.load_post(a, "p_draft")
    p["variants"] = [v]
    store.save_post(a, p, expected_rev=p["rev"], actor=ACTOR, op="test.published")
    edit(b, "Remote copy")
    path = tmp_path / "published.json"
    export(b, path, "maren")
    assert collab.import_bundle(a, path, peer="tomas", apply=True)["conflicts"]
    assert store.load_post(a, "p_draft")["variants"][0]["result"]["id"] == "live-post"


def test_status_read_only_and_tailscale_no_install(monkeypatch, pair):
    a, _ = pair
    assert collab.status(a)["studio"] is None
    assert not (a.work / "collaboration").exists()
    monkeypatch.setattr(collab.shutil, "which", lambda _: None)
    monkeypatch.setattr(collab, "TAILSCALE_APP", Path("/nonexistent/Tailscale"))
    assert collab.tailscale_status()["installed"] is False


def test_tailscale_diagnostics_only_runs_readonly_status(monkeypatch):
    from types import SimpleNamespace
    calls = []
    monkeypatch.setattr(collab.shutil, "which", lambda _: "/usr/local/bin/tailscale")
    def run(args, **kw):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"BackendState": "Running", "Peer": {"secret-id": {"Online": True, "TailscaleIPs": ["100.1.2.3"]}}}))
    monkeypatch.setattr(collab.subprocess, "run", run)
    res = collab.tailscale_status()
    assert calls == [["/usr/local/bin/tailscale", "status", "--json"]]
    assert res["connected"] and res["online_peers"] == 1 and "100.1.2.3" not in json.dumps(res)


def test_cli_preview_export_and_invalid_bundle_are_json(pair, tmp_path, capsys):
    from ave.cli import main
    a, b = pair
    post(a)
    path = tmp_path / "cli.json"
    assert main(["collaborate", "export", str(path), "--studio", str(a.root), "--peer", "tomas"]) == 0
    assert json.loads(capsys.readouterr().out)["posts"] == 1
    assert main(["collaborate", "import", str(path), "--studio", str(b.root), "--peer", "maren"]) == 0
    assert not json.loads(capsys.readouterr().out)["applied"] and not store.list_posts(b)
    path.write_text("{}")
    assert main(["collaborate", "import", str(path), "--studio", str(b.root), "--peer", "maren"]) == 2
    assert not json.loads(capsys.readouterr().out)["ok"]


def test_concurrent_imports_serialize_and_appledouble_is_ignored(pair, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    a, b = pair
    post(a)
    (a.posts_dir / "._p_draft.json").write_bytes(b"AppleDouble metadata, not JSON")
    path = tmp_path / "concurrent.json"
    export(a, path, "tomas")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: collab.import_bundle(b, path, peer="maren", apply=True), range(2)))
    assert sum(bool(r["created"]) for r in results) == 1
    assert sum(r["replayed"] for r in results) == 1
    assert store.load_post(b, "p_draft")["rev"] == 1
    assert (a.posts_dir / "._p_draft.json").exists()


def test_replayed_conflicting_bundle_stays_blocked_until_resolved(pair, tmp_path, capsys):
    a, b = seed(pair, tmp_path)
    edit(a, "Local work")
    edit(b, "Remote work")
    path = tmp_path / "needs-review.json"
    export(b, path, "maren")
    first = collab.import_bundle(a, path, peer="tomas", apply=True)
    replay = collab.import_bundle(a, path, peer="tomas", apply=True)
    assert replay["replayed"] and not replay["ok"]
    assert replay["conflicts"][0]["file"] == first["conflicts"][0]["file"]
    from ave.cli import main
    assert main(["collaborate", "import", str(path), "--studio", str(a.root), "--peer", "tomas", "--apply"]) == 2
    assert json.loads(capsys.readouterr().out)["conflicts"]
    cid = Path(first["conflicts"][0]["file"]).stem
    collab.resolve_conflict(a, cid, take="local", expected_rev=store.load_post(a, "p_draft")["rev"], apply=True)
    resolved = collab.import_bundle(a, path, peer="tomas", apply=True)
    assert resolved["replayed"] and resolved["ok"] and not resolved["conflicts"]


def test_identity_persisted_before_first_export_is_exposed(pair, tmp_path, monkeypatch):
    a, _ = pair
    post(a)
    path = tmp_path / "first-export.json"
    real_open = Path.open
    def fail_output(self, *args, **kwargs):
        if self == path and args and args[0] == "xb":
            raise OSError("simulated full exchange disk")
        return real_open(self, *args, **kwargs)
    monkeypatch.setattr(Path, "open", fail_output)
    with pytest.raises(OSError, match="simulated"):
        export(a, path, "tomas")
    studio_id = collab.status(a)["studio"]
    assert studio_id and not path.exists()
    monkeypatch.setattr(Path, "open", real_open)
    result = export(a, path, "tomas")
    assert result["source"] == studio_id == json.loads(path.read_text())["source"]


def test_failed_state_persistence_never_exposes_first_export(pair, tmp_path, monkeypatch):
    a, _ = pair
    post(a)
    path = tmp_path / "no-identity.json"
    def fail(*args):
        raise OSError("state disk full")
    monkeypatch.setattr(collab, "_atomic", fail)
    with pytest.raises(OSError):
        export(a, path, "tomas")
    assert not path.exists()


def _media_bundle(pair, tmp_path):
    from PIL import Image
    from ave.library import ingest
    a, b = pair
    image = tmp_path / "recovery.png"
    Image.new("RGB", (16, 16), "green").save(image)
    asset = ingest.add_file(a, image, source={"type": "own"}, by="tester", attested=False)
    post(a, media=[{"slot": 0, "asset": asset["id"], "role": "image", "alt": "Green square"}])
    path = tmp_path / "with-media.json"
    export(a, path, "tomas", include_media=True)
    dest = b.work / "collaboration" / "media" / (asset["sha256"] + ".png")
    return a, b, image, path, dest


def test_interrupted_media_stage_cannot_leave_partial_final_name(pair, tmp_path, monkeypatch):
    a, b, image, path, dest = _media_bundle(pair, tmp_path)
    real_replace = collab.os.replace
    def fail_replace(src, target):
        if Path(target) == dest:
            raise OSError("crashed before media rename")
        return real_replace(src, target)
    monkeypatch.setattr(collab.os, "replace", fail_replace)
    with pytest.raises(OSError, match="media rename"):
        collab.import_bundle(b, path, peer="maren", apply=True)
    assert not dest.exists()
    monkeypatch.setattr(collab.os, "replace", real_replace)
    result = collab.import_bundle(b, path, peer="maren", apply=True)
    assert result["ok"] and dest.read_bytes() == image.read_bytes()
    assert store.load_post(b, "p_draft")["rev"] == 1


def test_legacy_partial_staged_media_recovered_without_poisoning_retry(pair, tmp_path):
    a, b, image, path, dest = _media_bundle(pair, tmp_path)
    dest.parent.mkdir(parents=True)
    partial = image.read_bytes()[:12]
    dest.write_bytes(partial)
    result = collab.import_bundle(b, path, peer="maren", apply=True)
    assert result["ok"] and dest.read_bytes() == image.read_bytes()
    assert any(p.read_bytes() == partial for p in (dest.parent / "recovered").iterdir())
    assert not (b.library_dir / "licenses.jsonl").exists()


def test_state_capacity_export_refuses_before_output_or_corrupt_state(pair, tmp_path, monkeypatch):
    a, _ = seed(pair, tmp_path)
    state_path = a.work / "collaboration" / "state.json"
    original = state_path.read_bytes()
    monkeypatch.setattr(collab, "MAX_STATE_BYTES", len(original) + 200)
    path = tmp_path / "oversized-state-export.json"
    post(a, "p_second", text="Extra draft " * 100)
    with pytest.raises(ValueError, match="state capacity"):
        export(a, path, "other-peer")
    assert not path.exists() and state_path.read_bytes() == original
    assert collab.status(a)["studio"]


def test_state_capacity_import_refuses_before_any_post_mutation(pair, tmp_path, monkeypatch):
    a, b = seed(pair, tmp_path)
    post(a, "p_second", text="Extra draft " * 100)
    path = tmp_path / "oversized-state-import.json"
    export(a, path, "tomas")
    state_path = b.work / "collaboration" / "state.json"
    original = state_path.read_bytes()
    monkeypatch.setattr(collab, "MAX_STATE_BYTES", len(original) + 200)
    with pytest.raises(ValueError, match="state capacity"):
        collab.import_bundle(b, path, peer="maren", apply=True)
    assert state_path.read_bytes() == original
    assert [p["id"] for p in store.list_posts(b)] == ["p_draft"]
    assert collab.status(b)["imports"] == 1


def test_receipt_growth_refused_before_editing_a_post(pair, tmp_path, monkeypatch):
    a, b = seed(pair, tmp_path)
    edit(a, "A new draft")
    path = tmp_path / "receipt-limit.json"
    export(a, path, "tomas")
    state_path = b.work / "collaboration" / "state.json"
    original = state_path.read_bytes()
    monkeypatch.setattr(collab, "MAX_STATE_BYTES", len(original) + 10)
    with pytest.raises(ValueError, match="state capacity"):
        collab.import_bundle(b, path, peer="maren", apply=True)
    assert state_path.read_bytes() == original
    assert store.load_post(b, "p_draft")["copy"]["base"] == "First draft"


def test_production_bundle_has_portable_tasks_not_workspace_authority(pair,tmp_path):
    from ave.studio import production as P
    a,b=pair
    p=post(a,production={'title':'Launch note','owner':'person_remote','reviewer':'person_remote','workflow':'wf_remote','status':'writing','checklist':[{'id':'task_words','title':'Draft the words','done':False,'owner':'person_remote','due_date':'2026-11-02'}]})
    out=tmp_path/'production.json';export(a,out,'tomas')
    content=json.loads(out.read_text())['entries'][0]['content']['production']
    assert not {'owner','reviewer','workflow','status','depends_on','sources'} & content.keys()
    assert content['checklist'][0]['owner'] is None
    assert collab.import_bundle(b,out,peer='maren',apply=True)['created']==[p['id']]
    imported=P.defaults(store.load_post(b,p['id']))
    assert imported['owner'] is None and imported['reviewer'] is None
    assert imported['title']=='Launch note' and imported['checklist'][0]['owner'] is None
