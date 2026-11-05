"""Adopting existing folders, and device-tagged versions that never collide."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import stat
from pathlib import Path

import pytest

from ave import editdoc
from ave.episode import Episode
from ave.project import Project
from ave.project.adopt import adopt, status

from publish_helpers import isolated_secrets, video_pool  # noqa: F401  (fixtures: a private approval key, test videos)

SRC = {"A": {"file": "02_FOOTAGE/camA.mov", "kind": "video", "start_offset": 0.0}}


def doc(stage: str) -> dict:
    d = editdoc.new_doc(stage, 30, (1920, 1080), copy.deepcopy(SRC), 10.0)
    d["tracks"]["video"] = [{"id": "v1", "src": "A", "o0": 0, "o1": 10, "e0": 0}]
    d["tracks"]["dialogue"] = [{"id": "d1", "src": "A", "o0": 0, "o1": 10, "e0": 0}]
    return d


def tree_hash(root: Path, skip: str | None = None) -> str:
    """Every path, its bytes, mode and mtime: any move, rename, write or touch changes it."""
    h = hashlib.sha256()
    for p in sorted(Path(root).rglob("*")):
        rel = p.relative_to(root)
        if skip and rel.parts[0] == skip:
            continue
        st = p.lstat()
        h.update(f"{rel}|{stat.S_IFMT(st.st_mode)}|{st.st_mode}|{st.st_mtime_ns}|".encode())
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


@pytest.fixture
def numbered_project(tmp_path):
    root = tmp_path / "The Long Table"
    for d in ("02_FOOTAGE", "03_AUDIO", "01_PROJECT/edits", "01_PROJECT/captions", "01_PROJECT/work"):
        (root / d).mkdir(parents=True)
    (root / "episode.json").write_text(json.dumps({"title": "The Long Table", "layout": "numbered"}))
    (root / "02_FOOTAGE" / "camA.mov").write_bytes(b"\0" * 4096)
    (root / "03_AUDIO" / "mic.wav").write_bytes(b"\1" * 2048)
    (root / "01_PROJECT" / "captions" / "rough.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n")
    for name, stage in (("assembly.v1.json", "assembly"), ("rough.v1.json", "rough"), ("rough.v2.json", "rough"),
                        ("rough.s2.v1.json", "rough")):
        d = doc(stage)
        if name == "rough.v2.json":
            d["captions"]["srt"] = "01_PROJECT/captions/rough.srt"
        (root / "01_PROJECT" / "edits" / name).write_text(json.dumps(d))
    (root / "PROGRESS.md").write_text("# progress\n")
    return root


def _readonly(root: Path):
    for d in ("02_FOOTAGE", "03_AUDIO"):
        for p in (root / d).rglob("*"):
            p.chmod(0o444)
        (root / d).chmod(0o555)


def _writable(root: Path):
    for d in ("02_FOOTAGE", "03_AUDIO"):
        (root / d).chmod(0o755)
        for p in (root / d).rglob("*"):
            p.chmod(0o644)


def _use_computer(monkeypatch, tmp_path, name: str):
    home = tmp_path / f"home-{name}"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))


# ---------------------------------------------------------------------------------------------------- adoption
def test_adopting_moves_and_changes_no_existing_file(numbered_project):
    before = tree_hash(numbered_project)
    _readonly(numbered_project)  # original footage is read-only: adoption must not need to write there
    try:
        adopt(numbered_project)
    finally:
        _writable(numbered_project)
    assert tree_hash(numbered_project, skip=".ave") == before
    assert sorted(p.name for p in numbered_project.iterdir() if p.name.startswith(".")) == [".ave"]


def test_adopt_records_existing_edits_as_versions(numbered_project):
    r = adopt(numbered_project)
    assert r.created_project and r.kind == "episode" and r.title == "The Long Table"
    meta = json.loads((numbered_project / ".ave" / "project.json").read_text())
    assert meta["layout"] == "numbered" and meta["title"] == "The Long Table" and meta["project_id"].startswith("prj_")
    assert sorted(r.versions_added) == ["assembly.v1", "rough.s2.v1", "rough.v1", "rough.v2"]
    st = Project.open(numbered_project).state()
    ep = st.artifacts["art_ep"]
    assert ep.kind == "episode" and ep.title == "The Long Table" and ep.stage == "Draft"
    v2 = ep.versions["rough.v2"]
    assert v2["file"] == "01_PROJECT/edits/rough.v2.json"
    edit = numbered_project / "01_PROJECT" / "edits" / "rough.v2.json"
    by_path = {n["path"]: n for n in v2["needs"]}
    assert by_path[v2["file"]] == {"path": v2["file"], "sha256": hashlib.sha256(edit.read_bytes()).hexdigest(),
                                   "bytes": edit.stat().st_size}
    cap = numbered_project / "01_PROJECT" / "captions" / "rough.srt"
    assert by_path["01_PROJECT/captions/rough.srt"]["sha256"] == hashlib.sha256(cap.read_bytes()).hexdigest()
    footage = by_path["02_FOOTAGE/camA.mov"]  # recorded by size and date; never hashed during adoption
    assert footage["hash"] == "pending" and footage["sha256"] is None and footage["bytes"] == 4096 and "mtime" in footage
    assert r.hash_pending == ["02_FOOTAGE/camA.mov"]


def test_adopt_refuses_nothing_and_is_repeatable(numbered_project):
    adopt(numbered_project)
    log = next((numbered_project / ".ave" / "log").glob("*.jsonl"))
    lines = log.read_text().splitlines()
    again = adopt(numbered_project)
    assert not again.created_project and again.artifacts_added == [] and again.versions_added == []
    assert log.read_text().splitlines() == lines
    # a new legacy file that arrived meanwhile is the only thing a third run records
    (numbered_project / "01_PROJECT" / "edits" / "final.v1.json").write_text(json.dumps(doc("final")))
    third = adopt(numbered_project)
    assert third.versions_added == ["final.v1"] and third.artifacts_added == []


def test_episode_stage_is_the_furthest_existing_edit(tmp_path):
    from ave.project.adopt import episode_stage
    root = tmp_path / "ep"
    Episode(root).create("ep")
    assert episode_stage(root) == "Plan"
    (root / "edits" / "assembly.v1.json").write_text("{}")
    assert episode_stage(root) == "Draft"
    (root / "edits" / "final.v1~abcd.json").write_text("{}")
    assert episode_stage(root) == "Finish"
    (root / "work" / "package").mkdir(parents=True)
    (root / "work" / "package" / "status.json").write_text(json.dumps({"built": True}))
    assert episode_stage(root) == "Finish"          # a package is not a publish: Out needs an approved or sent one (final I2)


def test_studio_posts_become_artifacts(tmp_path):
    from ave.studio.project import StudioProject
    sp = StudioProject(tmp_path / "studio")
    sp.create("My Studio")
    posts = {"p_idea": {"stage": "idea", "idea": {"text": "A walk in the hills"}},
             "p_draft": {"stage": "draft", "copy": {"base": "Copy"}},
             "p_active": {"stage": "active", "variants": [{"id": "v1", "dropped": False, "result": None, "schedule": None}]},
             "p_sched": {"stage": "active", "variants": [{"id": "v1", "dropped": False, "result": None,
                                                          "schedule": {"at": "2026-11-03T10:00:00Z"}}]},
             "p_pub": {"stage": "active", "variants": [{"id": "v1", "result": {"state": "published"}},
                                                       {"id": "v2", "result": None}]},
             "p_drop": {"stage": "dropped"}, "p_arch": {"stage": "archived"}}
    for pid, post in posts.items():
        (sp.posts_dir / f"{pid}.json").write_text(json.dumps({"v": 1, "kind": "studio.post", "id": pid, **post}))
    (sp.posts_dir / "broken.json").write_text("{not json")
    before = tree_hash(sp.root)
    r = adopt(sp.root)
    assert tree_hash(sp.root, skip=".ave") == before
    assert r.kind == "studio" and any("broken.json" in w for w in r.warnings)
    meta = json.loads((sp.root / ".ave" / "project.json").read_text())
    assert meta["title"] == "My Studio" and meta["layout"] == {"preset": "flat", "raw_dirs": ["inbox"]}
    st = Project.open(sp.root).state()
    got = {aid[len("art_post_"):]: (a.kind, a.stage, bool(a.fields.get("archived"))) for aid, a in st.artifacts.items()
           if aid.startswith("art_post_")}
    assert got == {"p_idea": ("post", "Plan", False), "p_draft": ("post", "Draft", False), "p_active": ("post", "Refine", False),
                   "p_sched": ("post", "Finish", False), "p_pub": ("post", "Finish", False),     # final I2: only a status
                   "p_drop": ("post", "Plan", True), "p_arch": ("post", "Plan", True)}
    assert st.artifacts["art_post_p_idea"].title == "A walk in the hills"
    flagged = [d for d in st.decisions.values() if d.get("artifact") == "art_post_p_pub"]
    assert len(flagged) == 1 and "not Out" in flagged[0]["data"]["summary"] and "only shown" in flagged[0]["data"]["reason"]
    assert adopt(sp.root).artifacts_added == []


# ---------------------------------------------------------------------------------------------------- device-tagged versions
def test_new_versions_in_an_adopted_project_are_tagged_and_logged(numbered_project):
    adopt(numbered_project)
    ep = Episode(numbered_project)
    me = Project.open(numbered_project).device
    p = editdoc.save_new_version(ep, doc("rough"))
    assert p.name == f"rough.v3~{me.short}.json"
    assert editdoc.latest_path(ep, "rough") == p
    assert editdoc.list_versions(ep, "rough") == [1, 2, 3]
    assert all(type(v) is int for v in editdoc.list_versions(ep, "rough"))  # callers check type(v) is int
    vs = editdoc.version_entries(ep, "rough")
    assert vs == [1, 2, 3] and vs[-1].tag == me.short and vs[-1].id == f"rough.v3~{me.short}"
    assert editdoc.path_for(ep, "rough", 3) == p and editdoc.path_for(ep, "rough", vs[-1]) == p
    ev = [e for e in Project.open(numbered_project).read_events() if e["op"] == "version.added"][-1]
    assert ev["artifact"] == "art_ep" and ev["data"]["file"] == f"01_PROJECT/edits/{p.name}"
    assert ev["needs"][0] == {"path": ev["data"]["file"], "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                              "bytes": p.stat().st_size}
    st = Project.open(numbered_project).state()
    assert st.artifacts["art_ep"].versions[f"rough.v3~{me.short}"]["number"] == 5  # after the four adopted versions
    # the doc's own number is still N: callers that print v{n} keep working
    assert f"{vs[-1]}" == "3" and json.dumps({"v": vs[-1]}) == '{"v": 3}'


def test_two_computers_making_v8_do_not_collide(numbered_project, tmp_path, monkeypatch):
    adopt(numbered_project)
    ep_dir = numbered_project / "01_PROJECT" / "edits"
    for n in range(3, 8):
        (ep_dir / f"rough.v{n}.json").write_text(json.dumps(doc("rough")))
    adopt(numbered_project)
    project_a = numbered_project
    project_b = tmp_path / "copy-on-b"
    shutil.copytree(project_a, project_b)              # the same project, synced to a second computer

    _use_computer(monkeypatch, tmp_path, "a")
    pa = editdoc.save_new_version(Episode(project_a), doc("rough"))
    tag_a = Project.open(project_a).device.short
    _use_computer(monkeypatch, tmp_path, "b")
    pb = editdoc.save_new_version(Episode(project_b), doc("rough"))
    tag_b = Project.open(project_b).device.short
    assert tag_a != tag_b
    assert pa.name == f"rough.v8~{tag_a}.json" and pb.name == f"rough.v8~{tag_b}.json"

    # sync: each computer receives the other's version file and log; nothing is overwritten
    for src, dst in ((project_a, project_b), (project_b, project_a)):
        for f in (src / "01_PROJECT" / "edits").glob("rough.v8~*.json"):
            if not (dst / f.relative_to(src)).exists():
                shutil.copy2(f, dst / f.relative_to(src))
        for f in (src / ".ave" / "log").glob("*.jsonl"):
            target = dst / f.relative_to(src)
            if not target.exists() or target.stat().st_size < f.stat().st_size:
                shutil.copy2(f, target)
    orders = []
    for root in (project_a, project_b):
        vs = editdoc.version_entries(Episode(root), "rough")
        assert editdoc.list_versions(Episode(root), "rough")[-2:] == [8, 8]
        assert sorted(v.id for v in vs if v == 8) == sorted([f"rough.v8~{tag_a}", f"rough.v8~{tag_b}"])
        orders.append([v.id for v in vs])
    assert orders[0] == orders[1]                    # both computers agree on the order (from the log stamps)
    # the next version on either computer is v9
    assert editdoc.save_new_version(Episode(project_b), doc("rough")).name == f"rough.v9~{tag_b}.json"


def test_legacy_names_still_load(tmp_path):
    ep = Episode(tmp_path / "old")
    ep.create("old")
    for name in ("rough.v1.json", "rough.v2.json", "rough.s1.v1.json"):
        (ep.edits / name).write_text(json.dumps(doc("rough")))
    assert editdoc.list_versions(ep, "rough") == [1, 2] and editdoc.list_versions(ep, "rough", "s1") == [1]
    assert editdoc.latest_path(ep, "rough") == ep.edits / "rough.v2.json"
    assert editdoc.load_latest(ep, "rough")["stage"] == "rough"
    p = editdoc.save_new_version(ep, doc("rough"))            # not adopted: legacy name, no .ave, no events
    assert p.name == "rough.v3.json" and not (ep.root / ".ave").exists()
    # a folder holding both forms (a tagged file arrived from a synced computer) reads both
    (ep.edits / "rough.v3~abcd.json").write_text(json.dumps(doc("rough")))
    assert [v.id for v in editdoc.version_entries(ep, "rough")] == ["rough.v1", "rough.v2", "rough.v3", "rough.v3~abcd"]
    assert editdoc.latest_path(ep, "rough") == ep.edits / "rough.v3~abcd.json"
    with pytest.raises(editdoc.AmbiguousVersion):
        editdoc.path_for(ep, "rough", 3)
    assert editdoc.path_for(ep, "rough", 2) == ep.edits / "rough.v2.json"
    assert editdoc.VERSION_RE.match("rough.s2.v8~abcd.json").groupdict() == {"stage": "rough", "name": "s2", "n": "8",
                                                                              "device": "abcd"}


def test_log_order_wins_over_numbers_for_display(numbered_project):
    adopt(numbered_project)
    ep = Episode(numbered_project)
    project = Project.open(numbered_project)
    # a version from another computer whose number is lower, but whose event is newer, is latest here
    other = numbered_project / "01_PROJECT" / "edits" / "rough.v2~zzzz.json"
    other.write_text(json.dumps(doc("rough")))
    assert editdoc.latest_path(ep, "rough") == other        # unlogged yet: (N, tag) places it after rough.v2
    mine = editdoc.save_new_version(ep, doc("rough"))
    assert editdoc.latest_path(ep, "rough") == mine
    from ave.project.adopt import record_version
    record_version(project, other)                           # its event arrives now, stamped after mine
    assert editdoc.latest_path(ep, "rough") == other


# ---------------------------------------------------------------------------------------------------- CLI
def test_cli_adopt_and_status(numbered_project, capsys):
    from ave.cli import main
    assert main(["project", "status", str(numbered_project)]) == 0
    assert "not a project yet" in capsys.readouterr().out
    assert main(["project", "adopt", str(numbered_project)]) == 0
    out = capsys.readouterr().out
    assert "Adopted: The Long Table" in out and "Recorded 4 version(s)" in out and "only .ave/ was added" in out
    assert main(["project", "status", str(numbered_project), "--json"]) == 0
    s = json.loads(capsys.readouterr().out)
    assert s["adopted"] and s["artifacts"][0]["id"] == "art_ep" and s["artifacts"][0]["versions"] == 4
    assert s["hash_pending"] == ["02_FOOTAGE/camA.mov"] and s["unrecorded_edits"] == []
    assert main(["project", "status", str(numbered_project)]) == 0
    out = capsys.readouterr().out
    assert "The Long Table" in out and "Draft" in out and "waiting for a checksum" in out
    assert status(numbered_project)["device"]["tag"] == Project.open(numbered_project).device.short


def test_a_symlinked_ave_is_refused(numbered_project, tmp_path):
    from ave.project import ProjectError
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (numbered_project / ".ave").symlink_to(elsewhere)
    with pytest.raises(ProjectError, match="symlink"):
        adopt(numbered_project)
    with pytest.raises(ValueError, match="symlink"):
        editdoc.save_new_version(Episode(numbered_project), doc("rough"))


def test_duplicate_post_ids_are_recorded_once(tmp_path):
    from ave.studio.project import StudioProject
    sp = StudioProject(tmp_path / "studio")
    sp.create("S")
    for f in ("a.json", "b.json"):
        (sp.posts_dir / f).write_text(json.dumps({"id": "same", "stage": "idea"}))
    r = adopt(sp.root)
    assert r.artifacts_added.count("art_post_same") == 1 and any("also used by" in w for w in r.warnings)


def test_an_unlogged_save_says_so_in_the_result(numbered_project, monkeypatch):
    adopt(numbered_project)
    from ave.project import adopt as A
    record = A.record_version

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(A, "record_version", boom)
    p = editdoc.save_new_version(Episode(numbered_project), doc("rough"))
    assert p.exists() and any("could not record" in w for w in editdoc.saved_warnings(p))
    monkeypatch.setattr(A, "record_version", record)
    assert adopt(numbered_project).versions_added == [p.name[:-len(".json")]]   # re-adopting records it


def test_adoption_puts_an_episode_at_out_only_for_an_approved_or_sent_publish(tmp_path, video_pool, monkeypatch):
    """Final review I2: Out needs the publishing approval. Drafts in the queue (what `ave package queue` makes) or a built package
    adopt at Finish, with a decision that says why; a validly signed approval or a "sent" row in this computer's own dispatch
    record, on the computer that publishes, adopts at Out."""
    from publish_helpers import RecordingPublisher, approve_item, queue_item
    from ave.publish import queue as pq

    def episode(name):
        root = tmp_path / name
        ep = Episode(root)
        ep.create(name)
        video = ep.exports / "final.mp4"
        video.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(video_pool / "wide.mp4", video)
        return ep, video

    ep, video = episode("drafts")
    queue_item(ep, video)                                                    # queued, never approved
    adopt(ep.root)
    st = Project.open(ep.root).state()
    assert st.artifacts["art_ep"].stage == "Finish"
    flagged = [d for d in st.decisions.values() if d.get("artifact") == "art_ep"]
    assert len(flagged) == 1 and "approved or sent" in flagged[0]["data"]["reason"]
    adopt(ep.root)
    assert len([d for d in Project.open(ep.root).state().decisions.values() if d.get("artifact") == "art_ep"]) == 1

    ep, video = episode("forged")                                            # a hand-edited "approved" status
    it = queue_item(ep, video)
    raw = pq.load(ep, it["id"])
    raw.update(status="published", approvals=[{"by": "maren", "at": "2026-11-01T10:00:00+0000", "sha256": raw["sha256"],
                                                "meta_sha256": "x", "sig": "0" * 64}])
    pq.save(ep, raw)
    adopt(ep.root)
    assert Project.open(ep.root).state().artifacts["art_ep"].stage == "Finish"

    ep, video = episode("approved")
    it = queue_item(ep, video)
    approve_item(ep, it["id"], {"youtube": RecordingPublisher("youtube")})
    adopt(ep.root)
    st = Project.open(ep.root).state()
    assert st.artifacts["art_ep"].stage == "Out" and not [d for d in st.decisions.values() if d.get("artifact") == "art_ep"]

    ep, video = episode("sent")
    it = queue_item(ep, video)
    approve_item(ep, it["id"], {"youtube": RecordingPublisher("youtube")})
    assert pq.run(ep, it["id"], send=True, publishers={"youtube": RecordingPublisher("youtube")})["ok"]
    done = pq.load(ep, it["id"])
    done["approvals"] = []                                                   # even with the approval gone, the record says sent
    pq.save(ep, done)
    adopt(ep.root)
    assert Project.open(ep.root).state().artifacts["art_ep"].stage == "Out"


def test_an_approved_studio_post_adopts_at_out_and_an_unapproved_one_at_finish(tmp_path, video_pool):
    """The post-side twin of the episode test: Out needs the publisher's approval of the queue item behind the post."""
    from publish_helpers import RecordingPublisher, approve_item, queue_item
    from ave.studio.project import StudioProject
    sp = StudioProject(tmp_path / "studio")
    sp.create("My Studio")
    video = sp.exports / "final.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(video_pool / "wide.mp4", video)
    ok = queue_item(sp, video)
    approve_item(sp, ok["id"], {"youtube": RecordingPublisher("youtube")})
    no = queue_item(sp, video, title="Other")
    for pid, item in (("p_ok", ok), ("p_no", no)):
        (sp.posts_dir / f"{pid}.json").write_text(json.dumps(
            {"v": 1, "kind": "studio.post", "id": pid, "stage": "active",
             "variants": [{"id": "v1", "result": {"state": "published"}, "queue": {"id": item["id"], "root": "."}}]}))
    adopt(sp.root)
    st = Project.open(sp.root).state()
    assert st.artifacts["art_post_p_ok"].stage == "Out"
    assert st.artifacts["art_post_p_no"].stage == "Finish"
