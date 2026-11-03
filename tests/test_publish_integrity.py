"""Bytes sent == bytes hashed; approvals expire."""

import shutil
import time

from publish_helpers import *  # noqa: F401,F403
from publish_helpers import RecordingPublisher, approve_item, queue_item

from ave.publish import queue, safety


def pubs(*names):
    return {n: RecordingPublisher(n) for n in names}


class SwappingPublisher(RecordingPublisher):
    """Replaces the ORIGINAL file the moment the upload starts (the TOCTOU window), then reads what it was handed."""

    def __init__(self, name, original, junk=b"swapped-after-approval"):
        super().__init__(name)
        self.original, self.junk, self.seen_sha, self.seen_path = original, junk, None, None

    def publish(self, item, video, progress):
        self.original.write_bytes(self.junk)
        self.seen_path = video
        self.seen_sha = safety.sha256_file(video)
        return super().publish(item, video, progress)


def test_adapter_gets_a_verified_private_copy_not_the_swappable_original(ep, wide):
    it = queue_item(ep, wide)
    approved = safety.sha256_file(wide)
    approve_item(ep, it["id"], pubs("youtube"))
    p = {"youtube": SwappingPublisher("youtube", wide)}
    rep = queue.run(ep, it["id"], send=True, publishers=p)
    assert rep["ok"] and p["youtube"].seen_sha == approved             # what the platform was sent is what the human approved
    assert p["youtube"].seen_path != wide and p["youtube"].seen_path.name == wide.name
    assert not p["youtube"].seen_path.exists()                          # the private copy is removed afterwards
    assert safety.sha256_file(wide) != approved                         # ...even though the original really was swapped


def test_swap_between_platforms_blocks_the_next_platform(ep, wide):
    it = queue_item(ep, wide, platforms=("youtube", "facebook"))
    approve_item(ep, it["id"], pubs("youtube", "facebook"))
    p = {"youtube": SwappingPublisher("youtube", wide), "facebook": RecordingPublisher("facebook")}
    rep = queue.run(ep, it["id"], send=True, publishers=p)
    assert rep["platforms"]["youtube"]["outcome"] == "published"
    assert rep["platforms"]["facebook"]["outcome"] == "blocked" and p["facebook"].publish_calls == 0


def test_verified_copy_keeps_order_and_is_private(tmp_path):
    files = []
    for i in range(2):
        f = tmp_path / f"s{i}.png"
        f.write_bytes(b"png%d" % i)
        files.append(f)
    vdir, copies = queue.verified_copy(files)
    try:
        assert [c.read_bytes() for c in copies] == [b"png0", b"png1"] and all(c.parent.parent == vdir for c in copies)
        assert oct(vdir.stat().st_mode & 0o777) == "0o700" and oct(copies[0].stat().st_mode & 0o777) == "0o400"
    finally:
        shutil.rmtree(vdir)


def _approved_at(ep, item_id, when_epoch):
    """Re-sign the latest approval with a different timestamp (what an old but otherwise valid approval looks like)."""
    d = queue.load(ep, item_id)
    a = d["approvals"][-1]
    a["at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(when_epoch))
    a["sig"] = safety.sign_approval(d["id"], a["sha256"], a["meta_sha256"], a["by"], a["at"])
    queue.save(ep, d)


def test_approval_expires_after_the_ttl(ep, wide):
    it = queue_item(ep, wide)
    approve_item(ep, it["id"], pubs("youtube"))
    assert safety.approval_ttl_days() == 14
    assert safety.approval_problems(queue.load(ep, it["id"]), it["sha256"]) == []
    _approved_at(ep, it["id"], time.time() - 13 * 86400)
    assert queue.run(ep, it["id"], publishers=pubs("youtube"))["ok"]            # 13 days old: still fine
    _approved_at(ep, it["id"], time.time() - 15 * 86400)
    p = pubs("youtube")
    rep = queue.run(ep, it["id"], send=True, publishers=p)
    assert not rep["ok"] and any("expired" in b for b in rep["blocked"]) and p["youtube"].publish_calls == 0
    assert queue.load(ep, it["id"])["status"] == "awaiting_approval"            # approve again, which signs a fresh timestamp
    approve_item(ep, it["id"], pubs("youtube"))
    assert queue.run(ep, it["id"], send=True, publishers=p)["ok"]


def test_ttl_comes_from_the_humans_config(ep, wide, tmp_path, monkeypatch):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "publish.yaml").write_text("publish:\n  approval_ttl_days: 2\n")
    monkeypatch.setenv("AVE_CONFIG_DIR", str(cfg))
    assert safety.approval_ttl_days() == 2
    it = queue_item(ep, wide)
    approve_item(ep, it["id"], pubs("youtube"))
    _approved_at(ep, it["id"], time.time() - 3 * 86400)
    assert any("expired" in x for x in safety.approval_problems(queue.load(ep, it["id"]), it["sha256"]))
    (cfg / "publish.yaml").write_text("publish:\n  approval_ttl_days: -5\n")       # nonsense falls back to the default
    assert safety.approval_ttl_days() == 14


def test_unreadable_approval_timestamp_is_refused(ep, wide):
    it = queue_item(ep, wide)
    approve_item(ep, it["id"], pubs("youtube"))
    d = queue.load(ep, it["id"])
    a = d["approvals"][-1]
    a["at"] = "yesterday"
    a["sig"] = safety.sign_approval(d["id"], a["sha256"], a["meta_sha256"], a["by"], a["at"])
    assert any("timestamp" in x for x in safety.approval_problems(d, it["sha256"]))
