"""Publishing happens on one computer only (Task 20).

Only the publisher computer has a queue and sends. Which computer that is comes from the pinned owner and the chain of handover
events, never from project.json alone, and it fails closed. Every other computer, and a copy of the folder on a new Mac, refuses
to queue, approve or send with one plain sentence, and shows what the publisher noted, read-only. The queue, this computer's
dispatch record and every credential stay in the local zone and never sync (proved against real Syncthing below). A restored,
duplicated or remounted folder cannot send the same post twice: the dispatch record, keyed on the post (its queue id and its
post_ref) and the platform, is also kept in this computer's own AVE folder under the folder's stable publish_id. Sending a post
there again takes a person (`ave publish resolve ... --send-again`).

No real platform is ever contacted: every adapter here is the publish tests' RecordingPublisher fake.
"""

from __future__ import annotations

import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from publish_helpers import isolated_secrets  # noqa: F401  (autouse: in-memory keyring, no real keychain)
from publish_helpers import RecordingPublisher, approve_item, queue_item

from ave.episode import Episode
from ave.project import zones
from ave.project.log import read_logs
from ave.publish import queue
from ave.publish.adapters.base import Absent
from ave.publish.receipts import result
from ave.studio import dispatch

from . import harness as H

BEN_AND_TOMAS = [{"person": "maren", "devices": ["dev_A"], "media": "full"},
                 {"person": "tomas", "devices": ["dev_B"], "media": "full"}]


def be(monkeypatch, base: Path, device: str) -> Path:
    """Act as the computer `device`: its own HOME, with its own AVE device file (so `Device.load()` is that computer)."""
    home = base / "homes" / device
    f = home / "Library" / "Application Support" / "AVE" / "device.json"
    if not f.exists():
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"device_id": device}) + "\n")
    monkeypatch.setenv("HOME", str(home))
    return home


def _project_json(root: Path, publisher: str | None = "dev_A", members=BEN_AND_TOMAS, owner: str | None = "dev_A") -> None:
    H.write_project_json(root, "prj_PUBGUARD", "The Long Table", "flat", members, publisher)
    meta = json.loads((root / ".ave" / "project.json").read_text())
    if publisher is None:
        meta.pop("publisher_device")
    if owner:
        meta["owner_device"] = owner
    (root / ".ave" / "project.json").write_text(json.dumps(meta, indent=2) + "\n")


def _episode(root: Path, publisher: str | None = "dev_A", members=BEN_AND_TOMAS, owner: str | None = "dev_A") -> Episode:
    _project_json(root, publisher, members, owner)
    (root / "exports").mkdir(parents=True, exist_ok=True)
    (root / "exports" / "final.mp4").write_bytes(b"the reviewed episode\n" * 64)
    return Episode(root)


def _pubs(**kw):
    return {"youtube": RecordingPublisher("youtube", **kw)}


def _published_on_a(ep: Episode) -> tuple[dict, dict]:
    p = _pubs()
    it = queue_item(ep, ep.exports / "final.mp4")
    approve_item(ep, it["id"], p)
    rep = queue.run(ep, it["id"], send=True, publishers=p)
    assert rep["ok"] and p["youtube"].publish_calls == 1
    return queue.load(ep, it["id"]), p


def _sync_by_hand(src: Path, dst: Path, device: str) -> None:
    """What Syncthing would carry from `src` to `dst`: every file the zone rules do not keep local (the real thing is tested below)."""
    rules = zones.project_rules(src, device)
    for f in src.rglob("*"):
        rel = f.relative_to(src).as_posix()
        if f.is_file() and zones.zone_of(rel, rules) != "local":
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst / rel)


@pytest.fixture
def project_a(tmp_path, monkeypatch):
    be(monkeypatch, tmp_path, "dev_A")
    return _episode(tmp_path / "A" / "The Long Table")


@pytest.fixture
def project_b(tmp_path):
    root = tmp_path / "B" / "The Long Table"
    _project_json(root)
    return root


@pytest.fixture
def project(tmp_path, monkeypatch):
    be(monkeypatch, tmp_path, "dev_A")
    return _episode(tmp_path / "Mac A" / "The Long Table")


# ---------------------------------------------------------------------------------------------------------- one queue
def test_only_the_publisher_has_a_queue(project_a, project_b, tmp_path, monkeypatch):
    ep_a = project_a
    assert queue.not_publisher(ep_a) is None
    item, _ = _published_on_a(ep_a)
    view_a = queue.overview(ep_a)
    assert view_a["publisher"] and not view_a["read_only"] and view_a["message"] is None
    assert [i["id"] for i in view_a["items"]] == [item["id"]]

    _sync_by_hand(ep_a.root, project_b, "dev_A")
    be(monkeypatch, tmp_path, "dev_B")
    ep_b = Episode(project_b)
    said = "Publishing happens on Maren's computer. This computer shows what is queued but never sends."
    assert queue.not_publisher(ep_b) == said
    # no queue arrived, and none can be made here
    assert not ep_b.publish.exists() and queue.list_items(ep_b) == []
    (project_b / "exports" / "short.mp4").write_bytes(b"tomas's cut\n")
    with pytest.raises(ValueError) as e:
        queue.create(ep_b, video=project_b / "exports" / "short.mp4", platforms=["youtube"], title="x", publishers=_pubs())
    assert str(e.value) == said
    assert not (ep_b.publish / "queue").exists()
    # what is queued shows here, read-only, from what Maren's computer noted in the log
    view_b = queue.overview(ep_b)
    assert view_b["read_only"] and not view_b["publisher"] and view_b["message"] == said
    assert [(i["id"], i["title"], i["status"], i["noted_by"]) for i in view_b["items"]] == [(item["id"], "Great Episode", "published", "Maren")]
    noted = [e for e in read_logs(project_b / ".ave" / "log") if e["op"] == "publish.noted"]
    assert noted and all(set(e["data"]) == {"item", "post", "title", "platforms", "status", "schedule", "sent", "sending"} for e in noted)
    assert noted[-1]["data"]["sent"] == ["youtube"]

    # Ruling: a status that arrived by sync never triggers a send. Even a queue file carried over by hand, "approved" with a
    # valid-looking approval, does nothing here: run, approve and the studio's gate all stop at the publisher check, first and alone.
    forged = {**item, "status": "approved", "results": {}}
    (ep_b.publish / "queue").mkdir(parents=True)
    (ep_b.publish / "queue" / f"{item['id']}.json").write_text(json.dumps(forged))
    p = _pubs()
    for send in (False, True):
        rep = queue.run(ep_b, item["id"], send=send, publishers=p)
        assert rep["blocked"] == [said] and not rep["ok"] and rep["platforms"] == {}
    assert p["youtube"].publish_calls == 0
    assert queue.send_blockers(ep_b, forged) == [said]
    with pytest.raises(ValueError, match="^Publishing happens on Maren's computer"):
        queue.approve(ep_b, item["id"], "tomas", forged["sha256"][:12], publishers=p)
    variant = {"id": "v1", "account": "acct1", "platform": "youtube", "queue": {"root": ".", "id": item["id"]},
               "result": {"state": "approved"}}
    got = dispatch.blockers(SimpleNamespace(root=project_b), {"id": "p1", "variants": [variant]}, variant,
                            now=datetime.now(timezone.utc), publishers=p, send=True)
    assert [(b.code, b.message) for b in got] == [("not_publisher", said)]
    assert json.loads((ep_b.publish / "queue" / f"{item['id']}.json").read_text()) == forged      # nothing was written

    # ...and never suppresses a send on the publisher: Tomas's computer saying "published" for Maren's next post changes nothing there.
    be(monkeypatch, tmp_path, "dev_A")
    nxt = queue_item(ep_a, ep_a.exports / "final.mp4", title="Next Episode")
    from ave.project.model import Device, Project
    tomas = Project(project_b, json.loads((project_b / ".ave" / "project.json").read_text()), Device("dev_B", "B"))
    tomas.log.append("publish.noted", data={"item": nxt["id"], "title": "Next Episode", "platforms": ["youtube"], "post": None,
                                            "status": "published", "schedule": None, "sent": ["youtube"], "sending": []}, actor="human")
    shutil.copy2(project_b / ".ave" / "log" / "dev_B.jsonl", ep_a.root / ".ave" / "log" / "dev_B.jsonl")
    p2 = _pubs()
    approve_item(ep_a, nxt["id"], p2)
    assert queue.run(ep_a, nxt["id"], send=True, publishers=p2)["ok"] and p2["youtube"].publish_calls == 1


# ---------------------------------------------------------------------------------------------------------- real sync
def test_sync_never_carries_keys_or_dispatch_history(sync_pair, tmp_path, monkeypatch):
    a, b = sync_pair
    be(monkeypatch, tmp_path, "dev_A")
    ra = tmp_path / "A" / "prj_PUBGUARD"
    rb = tmp_path / "B" / "prj_PUBGUARD"
    ep_a = _episode(ra)
    _project_json(rb)
    # Credentials a publisher's folder might hold beside the queue: a platform token, an OAuth client, an approval key, a keyring
    # reference and an .env. Platform tokens normally live in the keychain; these stand in for any that end up as files.
    secrets = ["publish/youtube_token.json", "publish/approval.key", "publish/keyring.json", "token.json",
               "client_secret_123.json", ".env", "exports/youtube_token.json", "edits/credentials.json"]
    H.write_tree(ra, secrets)
    a.manager.share(ra, [b.peer])
    b.manager.share(rb, [a.peer])

    item, _ = _published_on_a(ep_a)
    home_record = queue.record_paths(ep_a)[1]
    assert (ep_a.publish / "dispatch" / "record.jsonl").is_file() and home_record.is_file()
    assert not str(home_record).startswith(str(ra))                     # the home copy is not even in the project folder
    H.write_tree(ra, ["edits/after-publish.v1.json"])                   # something that does sync, written after the publish
    a.manager.rescan(ra)
    H.wait_until(lambda: (rb / "edits" / "after-publish.v1.json").is_file()
                 and any(e["op"] == "publish.noted" and e["data"]["status"] == "published"
                         for e in read_logs(rb / ".ave" / "log")), timeout=120, what="A's edit and log on B")
    a.manager.wait(ra, "up_to_date", 60)
    b.manager.wait(rb, "up_to_date", 60)
    time.sleep(2)                                                       # anything extra would have landed by now

    got = H.files_in(rb)
    everything = {f.relative_to(rb).as_posix() for f in rb.rglob("*") if f.is_file()}
    assert "exports/final.mp4" in got and "edits/after-publish.v1.json" in got and ".ave/log/dev_A.jsonl" in got
    assert (ra / ".ave" / "local" / "publish_noted.json").is_file()
    for rel in sorted(everything):
        assert not rel.startswith("publish/"), rel                      # no queue, no dispatch record, no approval, no lock
        if rel.startswith(".ave/local/"):                               # only what B's own sync manager writes for itself
            assert rel in H.OWN_LOCAL or rel.startswith((".ave/local/locks/", ".ave/local/conflicts/")), rel
        assert "dispatch" not in rel and "record.jsonl" not in rel, rel
        assert not any(rel == s for s in secrets), rel
        assert "token" not in rel.lower() and "secret" not in rel.lower() and "credential" not in rel.lower(), rel
        assert not rel.endswith((".key", ".env")) and Path(rel).name != ".env", rel
    # The log says what is queued and nothing more: no approval, signature, platform id, url, key or history.
    text = (rb / ".ave" / "log" / "dev_A.jsonl").read_text()
    sig = item["approvals"][-1]["sig"]
    assert sig not in text and item["results"]["youtube"]["id"] not in text and "https://x/" not in text
    assert queue.dispatch_key(item, "youtube") not in text and queue.content_hash(item) not in text
    # Tomas's computer shows it read-only and says where publishing happens.
    be(monkeypatch, tmp_path, "dev_B")
    view = queue.overview(Episode(rb))
    assert view["read_only"] and view["message"].startswith("Publishing happens on Maren's computer.")
    assert [(i["id"], i["status"]) for i in view["items"]] == [(item["id"], "published")]


# ---------------------------------------------------------------------------------------------------------- restore and copy
def test_a_restored_folder_cannot_publish_twice(project, tmp_path, monkeypatch):
    ep = project
    root = ep.root
    p = _pubs()
    it = queue_item(ep, ep.exports / "final.mp4")
    approve_item(ep, it["id"], p)
    snapshot = tmp_path / "Time Machine" / "The Long Table"
    shutil.copytree(root, snapshot)                                     # last night's backup: approved, not yet sent
    assert queue.run(ep, it["id"], send=True, publishers=p)["ok"] and p["youtube"].publish_calls == 1
    sent = queue.load(ep, it["id"])

    # 1. Time Machine puts the folder back as it was before the send. The queue says "approved", the folder's own record is gone;
    #    this computer's home record still says it was sent, so nothing goes out again and the post is recorded once more.
    shutil.rmtree(root)
    shutil.copytree(snapshot, root)
    assert queue.load(Episode(root), it["id"])["status"] == "approved"
    p1 = _pubs()
    dry = queue.run(Episode(root), it["id"], send=False, publishers=p1)
    assert dry["platforms"]["youtube"]["would"] == "nothing: already sent from this computer"
    rep = queue.run(Episode(root), it["id"], send=True, publishers=p1)
    assert p1["youtube"].publish_calls == 0
    yt = rep["platforms"]["youtube"]
    assert yt["outcome"] == "already_sent" and yt["detail"].startswith("This computer already sent this to youtube on ")
    back = queue.load(Episode(root), it["id"])
    assert back["status"] == "published" and back["results"]["youtube"]["id"] == sent["results"]["youtube"]["id"]
    assert queue.run(Episode(root), it["id"], send=True, publishers=p1)["note"] == "already published; nothing sent"

    # 2. A duplicate of the backup on the same Mac (Finder "Duplicate", a second working copy): the same record stops it.
    dup = tmp_path / "Mac A" / "The Long Table copy"
    shutil.copytree(snapshot, dup)
    p2 = _pubs()
    assert queue.run(Episode(dup), it["id"], send=True, publishers=p2)["platforms"]["youtube"]["outcome"] == "already_sent"
    assert p2["youtube"].publish_calls == 0

    # 3. The published project copied to a new Mac and adopted there. That Mac is not the publisher: it sends nothing, says so
    #    plainly, and leaves the copy exactly as it found it.
    newmac = tmp_path / "Mac C" / "The Long Table"
    shutil.copytree(root, newmac)
    be(monkeypatch, tmp_path, "dev_C")
    from ave.project.adopt import adopt
    adopt(newmac)
    ep_c = Episode(newmac)
    said = queue.not_publisher(ep_c)
    assert said == ("Publishing happens on Maren's computer. This is a copy of the project on a computer that is not part of it, so it "
                    "sends nothing. To publish from here, hand publishing over from Maren's computer and approve again here.")
    before = (ep_c.publish / "queue" / f"{it['id']}.json").read_bytes()
    pc = _pubs()
    for send in (False, True):
        assert queue.run(ep_c, it["id"], send=send, publishers=pc) == {"id": it["id"], "mode": "send" if send else "dry-run",
                                                                         "ok": False, "blocked": [said], "platforms": {}}
    # the restored-from-backup state on the new Mac (approved, not sent) fares no better
    (ep_c.publish / "queue" / f"{it['id']}.json").write_bytes((snapshot / "publish" / "queue" / f"{it['id']}.json").read_bytes())
    assert queue.run(ep_c, it["id"], send=True, publishers=pc)["blocked"] == [said]
    with pytest.raises(ValueError) as e:
        queue.approve(ep_c, it["id"], "cara", it["sha256"][:12], publishers=pc)
    assert str(e.value) == said
    assert pc["youtube"].publish_calls == 0
    for jargon in ("sha256", "signature", "hmac", "approval_problems", "Traceback", "dev_A"):
        assert jargon not in said
    (ep_c.publish / "queue" / f"{it['id']}.json").write_bytes(before)


def test_another_computers_record_never_counts_as_sent_and_never_lets_a_blind_send(tmp_path, monkeypatch):
    """Rows another computer wrote (a copied folder) only make the publisher ask the platform first, with the platform id they name."""
    be(monkeypatch, tmp_path, "dev_C")
    ep = _episode(tmp_path / "Mac C" / "P", publisher="dev_C", owner="dev_C",
                  members=[{"person": "cara", "devices": ["dev_C"], "media": "full"}])
    it = queue_item(ep, ep.exports / "final.mp4")
    approve_item(ep, it["id"], _pubs())
    it = queue.load(ep, it["id"])
    row = {"v": 1, "key": queue.dispatch_key(it, "youtube"), "post": it["id"], "platform": "youtube", "content": queue.content_hash(it),
           "state": "sent", "device": "dev_A", "project": "prj_PUBGUARD", "at": queue.now_iso(), "seq": 1, "platform_id": "yt-123"}
    rec = ep.publish / "dispatch" / "record.jsonl"
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text(json.dumps(row) + "\n")
    assert queue.record_mark(ep, it, "youtube")[0] is None               # not "sent" here: another computer wrote it

    seen = []

    class Asks(RecordingPublisher):
        def recover(self, item, record):
            seen.append((record.get("receipt") or {}).get("object", {}).get("id"))
            return self._recover

    unknown = {"youtube": Asks("youtube", recover=None)}                 # the platform cannot tell: nothing sends, a person decides
    rep = queue.run(ep, it["id"], send=True, publishers=unknown)
    assert rep["platforms"]["youtube"]["outcome"] == "uncertain" and unknown["youtube"].publish_calls == 0 and seen == ["yt-123"]
    queue.resolve(ep, it["id"], "youtube", retry=True, by="cara")       # cara checked: it is not there
    assert queue.record_mark(ep, it, "youtube")[0] == "not_sent"
    ok = {"youtube": Asks("youtube", recover=None)}
    assert queue.run(ep, it["id"], send=True, publishers=ok)["ok"] and ok["youtube"].publish_calls == 1
    assert queue.record_mark(ep, it, "youtube")[0] == "sent"

    # a fresh item whose copied row the platform confirms: recorded as posted, never sent again
    it2 = queue_item(ep, ep.exports / "final.mp4", title="Second")
    approve_item(ep, it2["id"], _pubs())
    it2 = queue.load(ep, it2["id"])
    rec.write_text(rec.read_text() + json.dumps({**row, "key": queue.dispatch_key(it2, "youtube"), "post": it2["id"],
                                                 "content": queue.content_hash(it2), "seq": 2, "platform_id": "yt-456"}) + "\n")
    there = {"youtube": Asks("youtube", recover=result("youtube", "yt-456", "https://x/yt-456"))}
    assert queue.run(ep, it2["id"], send=True, publishers=there)["ok"] and there["youtube"].publish_calls == 0
    # and one the platform proves absent may go out
    it3 = queue_item(ep, ep.exports / "final.mp4", title="Third")
    approve_item(ep, it3["id"], _pubs())
    it3 = queue.load(ep, it3["id"])
    rec.write_text(rec.read_text() + json.dumps({**row, "key": queue.dispatch_key(it3, "youtube"), "post": it3["id"],
                                                 "content": queue.content_hash(it3), "seq": 3}) + "\n")
    gone = {"youtube": Asks("youtube", recover=Absent("no such video"))}
    assert queue.run(ep, it3["id"], send=True, publishers=gone)["ok"] and gone["youtube"].publish_calls == 1


def test_a_restored_folder_never_resends_an_accepted_upload(project, tmp_path):
    """Accepted but not yet a post (still processing): after a restore the platform is asked, and nothing is sent again."""
    ep = project

    class Processing(RecordingPublisher):
        def publish(self, item, video, progress):
            self.publish_calls += 1
            return result("youtube", "yt-789", phase="processing", object_kind="video")

    p = {"youtube": Processing("youtube")}
    it = queue_item(ep, ep.exports / "final.mp4")
    approve_item(ep, it["id"], p)
    snapshot = tmp_path / "backup"
    shutil.copytree(ep.root, snapshot)
    assert queue.run(ep, it["id"], send=True, publishers=p)["platforms"]["youtube"]["outcome"] == "processing"
    assert queue.record_mark(ep, it, "youtube")[1]["phase"] == "processing"
    shutil.rmtree(ep.root)
    shutil.copytree(snapshot, ep.root)
    asked = []

    class Unknown(Processing):
        def recover(self, item, record):
            asked.append(record["receipt"]["object"]["id"])
            return None

    again = {"youtube": Unknown("youtube")}
    rep = queue.run(Episode(ep.root), it["id"], send=True, publishers=again)
    assert rep["platforms"]["youtube"]["outcome"] == "uncertain" and again["youtube"].publish_calls == 0 and asked == ["yt-789"]


# ---------------------------------------------------------------------------------------------------------- fix round 1
def _plain_episode(root: Path) -> Episode:
    """A single-computer episode: episode.json, no project.json."""
    ep = Episode(root)
    ep.create("The Long Table")
    (ep.exports / "final.mp4").write_bytes(b"the reviewed episode\n" * 64)
    return Episode(root)


def test_a_copy_or_a_remount_of_a_presend_backup_never_sends_again(tmp_path, monkeypatch):
    """I-1: the home record is filed under the folder's own publish_id (written into episode.json when the post is queued), not
    under its path, so a Finder duplicate or a drive that remounts under another name still finds it."""
    be(monkeypatch, tmp_path, "dev_A")
    ep = _plain_episode(tmp_path / "Drive" / "Ep")
    it = queue_item(ep, ep.exports / "final.mp4")
    pid = json.loads((ep.root / "episode.json").read_text())["publish_id"]
    assert pid.startswith("pub_") and queue.record_paths(ep)[1].name == f"{pid}.jsonl"
    approve_item(ep, it["id"], _pubs())
    backup = tmp_path / "Time Machine" / "Ep"
    shutil.copytree(ep.root, backup)                                    # approved, not sent
    p = _pubs()
    assert queue.run(ep, it["id"], send=True, publishers=p)["ok"] and p["youtube"].publish_calls == 1
    for where in (tmp_path / "Drive" / "Ep copy", tmp_path / "Volumes" / "Drive 1" / "Ep"):
        shutil.copytree(backup, where)
        again = _pubs()
        rep = queue.run(Episode(where), it["id"], send=True, publishers=again)
        assert rep["platforms"]["youtube"]["outcome"] == "already_sent" and again["youtube"].publish_calls == 0, where
    # a single-computer episode that later becomes a project keeps reading the same record
    from ave.project.model import Project
    Project.create(ep.root, "The Long Table", person="maren")
    shutil.rmtree(ep.publish)
    shutil.copytree(backup / "publish", ep.publish)                    # only publish/ restored from the backup
    later = _pubs()
    assert queue.run(Episode(ep.root), it["id"], send=True, publishers=later)["platforms"]["youtube"]["outcome"] == "already_sent"
    assert later["youtube"].publish_calls == 0


def test_restore_edit_and_reapprove_still_needs_a_person_to_send_again(project, tmp_path, monkeypatch):
    """I-2: the record is keyed on the post and the platform, not the content, so an edited, re-approved restored copy is the same
    post. Only `resolve --send-again` (human-only) lets it go out there again."""
    ep = project
    it = queue_item(ep, ep.exports / "final.mp4")
    approve_item(ep, it["id"], _pubs())
    shutil.copytree(ep.root, tmp_path / "backup")
    assert queue.run(ep, it["id"], send=True, publishers=_pubs())["ok"]
    shutil.rmtree(ep.root)
    shutil.copytree(tmp_path / "backup", ep.root)
    ep = Episode(ep.root)
    queue.update(ep, it["id"], publishers=_pubs(), title="Great Episode (typo fixed)")
    approve_item(ep, it["id"], _pubs())
    p = _pubs()
    rep = queue.run(ep, it["id"], send=True, publishers=p)
    assert rep["platforms"]["youtube"]["outcome"] == "already_sent" and p["youtube"].publish_calls == 0
    with pytest.raises(ValueError, match="exactly one"):
        queue.resolve(ep, it["id"], "youtube", retry=True, send_again=True, by="maren")
    queue.resolve(ep, it["id"], "youtube", send_again=True, by="maren")    # a person checked and wants it out again
    assert queue.record_mark(ep, queue.load(ep, it["id"]), "youtube")[0] == "send_again"
    assert queue.load(ep, it["id"])["status"] == "awaiting_approval"      # sending again needs a fresh approval too
    assert not queue.run(ep, it["id"], send=True, publishers=_pubs())["ok"]
    approve_item(ep, it["id"], _pubs())
    p2 = _pubs()
    assert queue.run(ep, it["id"], send=True, publishers=p2)["ok"] and p2["youtube"].publish_calls == 1
    with pytest.raises(ValueError, match="nothing to send again|already"):
        it2 = queue_item(ep, ep.exports / "final.mp4", title="Never sent")
        queue.resolve(ep, it2["id"], "youtube", send_again=True, by="maren")


def test_send_again_is_a_human_act():
    from ave.cli import main
    with pytest.raises(SystemExit) as e:
        main(["publish", "resolve", "EP", "item1", "youtube", "--send-again"])
    assert "ave publish resolve EP item1 youtube --send-again" in str(e.value) and "human act" in str(e.value)
    agents = (Path(__file__).parents[2] / "AGENTS.md").read_text()
    assert "`ave publish resolve`" in agents and "--send-again" in agents


def test_a_rejected_accepted_upload_needs_a_fresh_review_even_after_a_restore(project, tmp_path):
    """I-3: accepted, then rejected by the platform: the record says so, and only an approval made after that may send."""
    ep = project

    class Processing(RecordingPublisher):
        def publish(self, item, video, progress):
            self.publish_calls += 1
            return result("youtube", "yt-9", phase="processing", object_kind="video")

    it = queue_item(ep, ep.exports / "final.mp4")
    approve_item(ep, it["id"], _pubs())
    shutil.copytree(ep.root, tmp_path / "backup")
    assert queue.run(ep, it["id"], send=True, publishers={"youtube": Processing("youtube")})["platforms"]["youtube"]["outcome"] == "processing"
    queue.refresh(ep, it["id"], publishers=_pubs(recover=Absent("rejected after processing")))
    assert queue.record_mark(ep, queue.load(ep, it["id"]), "youtube")[0] == "rejected_after_accept"
    shutil.rmtree(ep.root)
    shutil.copytree(tmp_path / "backup", ep.root)
    ep = Episode(ep.root)
    p = _pubs()
    rep = queue.run(ep, it["id"], send=True, publishers=p)
    assert p["youtube"].publish_calls == 0 and rep["platforms"]["youtube"]["outcome"] == "blocked"
    assert "accepted this and then rejected it" in rep["blocked"][0] and "approve it again" in rep["blocked"][0]
    assert queue.load(ep, it["id"])["status"] == "awaiting_approval"      # the restored approval no longer counts
    approve_item(ep, it["id"], _pubs())                                   # a person reviewed it again
    p2 = _pubs()
    rep2 = queue.run(ep, it["id"], send=True, publishers=p2)
    assert rep2["ok"] and p2["youtube"].publish_calls == 1, rep2


def test_a_new_publisher_treats_what_the_old_one_sent_as_sent_elsewhere(tmp_path, monkeypatch):
    """I-4, both directions. Queue items are rebuilt under new ids on the new publisher (as the studio does); the post_ref and the
    previous publisher's notes still stop a second post until a person checks and says send again."""
    from ave.syncthing import members as M
    ra, rb = tmp_path / "A" / "P", tmp_path / "B" / "P"
    be(monkeypatch, tmp_path, "dev_A")
    ep_a = _episode(ra)
    _project_json(rb)

    def post(ep, ref, title):
        it = queue.create(ep, video=ep.exports / "final.mp4", platforms=["youtube"], title=title, ai_disclosure=False,
                          publishers=_pubs(), post_ref=ref)
        approve_item(ep, it["id"], _pubs())
        return it

    one = post(ep_a, "studio:p1:v1", "One")
    assert queue.run(ep_a, one["id"], send=True, publishers=_pubs())["ok"]
    assert "Tomas's computer now publishes" in M.hand_over(ra, "tomas", device_id="dev_A")
    assert queue.not_publisher(ep_a).startswith("Publishing happens on Tomas's computer.")
    _sync_by_hand(ra, rb, "dev_A")

    be(monkeypatch, tmp_path, "dev_B")
    ep_b = Episode(rb)
    assert queue.not_publisher(ep_b) is None and M.publisher_chain(rb) == ["dev_A", "dev_B"]
    again = post(ep_b, "studio:p1:v1", "One")                           # rebuilt: a new queue id for the same post
    assert again["id"] != one["id"]
    p = _pubs()
    rep = queue.run(ep_b, again["id"], send=True, publishers=p)
    assert p["youtube"].publish_calls == 0 and rep["platforms"]["youtube"]["outcome"] == "sent_elsewhere"
    assert rep["blocked"][0].startswith("Maren's computer reported this as sent to youtube") and "--send-again" in rep["blocked"][0]
    two = post(ep_b, "studio:p2:v1", "Two")                             # Tomas's own new post goes out
    assert queue.run(ep_b, two["id"], send=True, publishers=_pubs())["ok"]

    assert "Maren's computer now publishes" in M.hand_over(rb, "maren", device_id="dev_B")
    _sync_by_hand(rb, ra, "dev_B")
    be(monkeypatch, tmp_path, "dev_A")
    ep_a = Episode(ra)
    assert queue.not_publisher(ep_a) is None and M.publisher_chain(ra) == ["dev_A", "dev_B", "dev_A"]
    two_again = post(ep_a, "studio:p2:v1", "Two")
    p = _pubs()
    rep = queue.run(ep_a, two_again["id"], send=True, publishers=p)
    assert p["youtube"].publish_calls == 0 and rep["blocked"][0].startswith("Tomas's computer reported this as sent to youtube")
    one_again = post(ep_a, "studio:p1:v1", "One")                       # Maren's own earlier post, rebuilt: his record has it
    p = _pubs()
    assert queue.run(ep_a, one_again["id"], send=True, publishers=p)["platforms"]["youtube"]["outcome"] == "already_sent"
    assert p["youtube"].publish_calls == 0
    queue.resolve(ep_a, two_again["id"], "youtube", send_again=True, by="maren")    # Maren checked: Tomas's post is not there
    approve_item(ep_a, two_again["id"], _pubs())
    p = _pubs()
    assert queue.run(ep_a, two_again["id"], send=True, publishers=p)["ok"] and p["youtube"].publish_calls == 1


def test_who_publishes_fails_closed(tmp_path, monkeypatch):
    """I-5: the publisher comes from the pinned owner and the handover chain. No publisher on a shared project: nobody sends. A
    solo project uses its owner. A hand-edited project.json naming another computer stops everyone until it is put right."""
    from ave.syncthing import members as M
    be(monkeypatch, tmp_path, "dev_A")
    shared = _episode(tmp_path / "shared", publisher=None)
    for dev in ("dev_A", "dev_B"):
        be(monkeypatch, tmp_path, dev)
        why = queue.not_publisher(shared)
        assert why.startswith("Publishing is on hold: this project's settings do not say which computer publishes."), dev
        assert queue.run(shared, "x", send=True, publishers=_pubs())["blocked"] == [why]
        with pytest.raises(ValueError, match="^Publishing is on hold"):
            queue_item(shared, shared.exports / "final.mp4")

    be(monkeypatch, tmp_path, "dev_A")
    solo = _episode(tmp_path / "solo", publisher=None, members=[{"person": "maren", "devices": ["dev_A"], "media": "full"}])
    assert queue.not_publisher(solo) is None
    it = queue_item(solo, solo.exports / "final.mp4")
    approve_item(solo, it["id"], _pubs())
    rep = queue.run(solo, it["id"], send=True, publishers=_pubs())
    assert rep["ok"], rep
    bare = _episode(tmp_path / "bare", publisher=None, owner=None, members=[{"person": "maren", "devices": ["dev_A"], "media": "full"}])
    assert queue.not_publisher(bare) is None                           # solo, no owner recorded: its only computer

    edited = _episode(tmp_path / "edited")
    assert queue.not_publisher(edited) is None                          # Maren's computer pins its owner on first look
    meta = json.loads((edited.root / ".ave" / "project.json").read_text())
    (edited.root / ".ave" / "project.json").write_text(json.dumps({**meta, "publisher_device": "dev_B"}))   # Tomas's hand edit
    for dev in ("dev_A", "dev_B"):
        be(monkeypatch, tmp_path, dev)
        why = queue.not_publisher(edited)
        assert why == ("Publishing is on hold: this project's settings say Tomas's computer publishes, but no handover from Maren's "
                       "computer has reached this computer. If it does not arrive once sync is up to date, hand publishing over "
                       "again from Maren's computer."), dev
    with pytest.raises(M.MemberError, match="Only the computer that publishes"):
        M.hand_over(edited.root, "maren", device_id="dev_B")
    be(monkeypatch, tmp_path, "dev_A")
    assert "again" in M.hand_over(edited.root, "maren", device_id="dev_A")    # Maren puts it right; nothing was handed over
    assert queue.not_publisher(edited) is None
    stranger = _episode(tmp_path / "stranger")
    queue.not_publisher(stranger)
    meta = json.loads((stranger.root / ".ave" / "project.json").read_text())
    (stranger.root / ".ave" / "project.json").write_text(json.dumps({**meta, "publisher_device": "dev_X"}))
    assert queue.not_publisher(stranger).startswith("Publishing is on hold: this project's settings say another computer publishes")


def test_a_computer_that_does_not_publish_offers_no_episode_post_to_approve_or_reject(project_a, project_b, tmp_path, monkeypatch):
    """I-6, now in the Workspace's approval sheet (the retired `ave ui` publish page had it): on a computer that does not publish, the
    queue reads as noted, read-only, with the sentence saying where publishing happens; the sheet lists nothing to approve, and a
    reject is refused."""
    from types import SimpleNamespace
    from ave.project import Project
    from ave.studio.ui.episodes import EpisodeError, Episodes
    item, _ = _published_on_a(project_a)
    _sync_by_hand(project_a.root, project_b, "dev_A")
    be(monkeypatch, tmp_path, "dev_B")
    Episode(project_b).create("The Long Table")                          # the episode settings arrive with sync in real use
    view = queue.overview(Episode(project_b))
    assert view["read_only"] and view["message"].startswith("Publishing happens on Maren's computer.")
    assert [i["id"] for i in view["items"]] == [item["id"]]
    proj = Project.open(project_b)
    eps = Episodes(SimpleNamespace(projects=lambda: [proj]), "tomas")
    assert eps.publish() == []
    with pytest.raises(EpisodeError):
        eps.publish_reject(proj.project_id, item["id"], "no")


def test_the_acceptance_fake_sends_once_and_never_touches_the_network(tmp_path, monkeypatch):
    """I-7: AVE_PUBLISH_FAKE swaps every platform for a local file, so the checklist can send for real and see "already sent"."""
    from ave.publish.adapters import get_publisher
    be(monkeypatch, tmp_path, "dev_A")
    posts = tmp_path / "fake-posts.jsonl"
    monkeypatch.setenv("AVE_PUBLISH_FAKE", str(posts))
    assert type(get_publisher("youtube")).__name__ == "FakePublisher"
    ep = _plain_episode(tmp_path / "Ep")
    it = queue.create(ep, video=ep.exports / "final.mp4", platforms=["youtube"], title="Fake", ai_disclosure=False)
    queue.approve(ep, it["id"], "maren", it["sha256"][:12])
    shutil.copytree(ep.root, tmp_path / "dup")
    assert queue.run(ep, it["id"], send=True)["ok"]
    assert len(posts.read_text().splitlines()) == 1
    rep = queue.run(Episode(tmp_path / "dup"), it["id"], send=True)
    assert rep["platforms"]["youtube"]["outcome"] == "already_sent" and len(posts.read_text().splitlines()) == 1
    assert queue.load(ep, it["id"])["results"]["youtube"]["url"].startswith("file://")


# ---------------------------------------------------------------------------------------------------------- fix round 2
def test_a_later_publishers_send_holds_the_post_over_this_computers_older_row(tmp_path, monkeypatch):
    """N-1 (the re-review's probe): A's own `not_sent` row is older than B's send, so after the handback A holds the post."""
    from ave.syncthing import members as M
    ra, rb = tmp_path / "A" / "P", tmp_path / "B" / "P"
    be(monkeypatch, tmp_path, "dev_A")
    ep_a = _episode(ra)
    _project_json(rb)

    def post(ep, ref, title):
        it = queue.create(ep, video=ep.exports / "final.mp4", platforms=["youtube"], title=title, ai_disclosure=False,
                          publishers=_pubs(), post_ref=ref)
        approve_item(ep, it["id"], _pubs())
        return it

    one = post(ep_a, "studio:p1:v1", "One")
    assert queue.run(ep_a, one["id"], send=True, publishers=_pubs(behavior="refuse"))["platforms"]["youtube"]["outcome"] == "failed"
    assert queue.record_mark(ep_a, queue.load(ep_a, one["id"]), "youtube")[0] == "not_sent"
    M.hand_over(ra, "tomas", device_id="dev_A")
    _sync_by_hand(ra, rb, "dev_A")
    be(monkeypatch, tmp_path, "dev_B")
    ep_b = Episode(rb)
    pb = _pubs()
    assert queue.run(ep_b, post(ep_b, "studio:p1:v1", "One")["id"], send=True, publishers=pb)["ok"] and pb["youtube"].publish_calls == 1
    M.hand_over(rb, "maren", device_id="dev_B")
    _sync_by_hand(rb, ra, "dev_B")
    be(monkeypatch, tmp_path, "dev_A")
    ep_a = Episode(ra)
    back = post(ep_a, "studio:p1:v1", "One")
    pa = _pubs()
    rep = queue.run(ep_a, back["id"], send=True, publishers=pa)
    assert pa["youtube"].publish_calls == 0 and rep["platforms"]["youtube"]["outcome"] == "sent_elsewhere"
    assert "Tomas's computer reported this as sent to youtube" in rep["blocked"][0]
    assert "ave studio resolve p1 v1 --send-again --studio " in rep["blocked"][0]       # the command that works for a studio post
    plain = queue.create(ep_a, video=ep_a.exports / "final.mp4", platforms=["youtube"], title="Plain", ai_disclosure=False,
                         publishers=_pubs())
    import shlex
    assert queue.send_again_command(ep_a, plain, "youtube") == f"ave publish resolve {shlex.quote(str(ra))} {plain['id']} youtube --send-again"
    spaced = Episode(tmp_path / "My Drive" / "Ep 1")
    assert "'" + str(spaced.root) + "'" in queue.send_again_command(spaced, plain, "youtube")
    # once a person sends it again here, the row is newer than Tomas's note and the hold clears
    queue.resolve(ep_a, back["id"], "youtube", send_again=True, by="maren")
    approve_item(ep_a, back["id"], _pubs())
    pa = _pubs()
    assert queue.run(ep_a, back["id"], send=True, publishers=pa)["ok"] and pa["youtube"].publish_calls == 1


def test_a_stale_episode_json_rewrite_keeps_the_publish_id(tmp_path, monkeypatch):
    """M-A: a writer that saves episode.json from a copy loaded before the id existed does not make the record forget the post."""
    be(monkeypatch, tmp_path, "dev_A")
    ep = _plain_episode(tmp_path / "Ep")
    stale = json.loads((ep.root / "episode.json").read_text())
    it = queue_item(ep, ep.exports / "final.mp4")
    approve_item(ep, it["id"], _pubs())
    pid = queue.record_scope(ep)
    shutil.copytree(ep.root, tmp_path / "backup")
    (ep.root / "episode.json").write_text(json.dumps({**stale, "title": "Renamed"}))     # the stale writer saves
    assert queue.run(ep, it["id"], send=True, publishers=_pubs())["ok"]
    assert queue.record_scope(ep) == pid and json.loads((ep.root / "episode.json").read_text())["publish_id"] == pid
    shutil.rmtree(ep.root)
    shutil.copytree(tmp_path / "backup", ep.root)
    p = _pubs()
    assert queue.run(Episode(ep.root), it["id"], send=True, publishers=p)["platforms"]["youtube"]["outcome"] == "already_sent"
    assert p["youtube"].publish_calls == 0


def test_the_fake_says_so_in_every_send_and_in_doctor(tmp_path, monkeypatch, capsys):
    from ave import doctor
    from ave.cli import main
    be(monkeypatch, tmp_path, "dev_A")
    assert doctor.publish_fake_checks() == []
    monkeypatch.setenv("AVE_PUBLISH_FAKE", str(tmp_path / "fake.jsonl"))
    warn = doctor.publish_fake_checks()
    assert len(warn) == 1 and warn[0].status == doctor.WARN and "AVE_PUBLISH_FAKE" in warn[0].detail
    ep = _plain_episode(tmp_path / "Ep")
    it = queue.create(ep, video=ep.exports / "final.mp4", platforms=["youtube"], title="Fake", ai_disclosure=False)
    queue.approve(ep, it["id"], "maren", it["sha256"][:12])
    assert queue.run(ep, it["id"])["fake"].startswith("FAKE (no platform contacted)")
    main(["publish", "run", str(ep.root), it["id"], "--yes"])
    cap = capsys.readouterr()
    assert "FAKE (no platform contacted)" in cap.err and '"fake": "FAKE (no platform contacted)' in cap.out
    assert queue.run(ep, it["id"], send=True, publishers=_pubs()).get("fake") is None    # a real adapter in use: no such claim
