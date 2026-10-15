"""`ave publish queue --from-clips` reads the sidecars `ave shorts export` writes: latest version per (clip, platform), nothing guessed."""
import json
import shutil

import pytest
from publish_helpers import *  # noqa: F401,F403
from publish_helpers import RecordingPublisher

from ave.cli import main
from ave.publish import queue


def _export(ep, vertical, slug, ver, plat, *, ok=True, title="A title", lic_ok=True):
    d = ep.exports / "shorts"
    d.mkdir(parents=True, exist_ok=True)
    mp4 = d / f"{slug}.v{ver}.{plat}.mp4"
    shutil.copy(vertical, mp4)
    side = {"name": mp4.stem, "platform": plat, "title": title, "description": f"desc v{ver}", "caption": "hook", "tags": ["Shorts"],
            "video": str(mp4.relative_to(ep.root)), "limits": {"ok": ok, "problems": [] if ok else ["too long"]},
            "licenses": {"ok": lic_ok}, "encode": {"problems": []}}
    (d / f"{slug}.v{ver}.{plat}.json").write_text(json.dumps(side))


@pytest.fixture
def fakes(monkeypatch):
    made = {}
    def gp(name, **kw):
        made.setdefault(name, RecordingPublisher(name))
        return made[name]
    monkeypatch.setattr(queue, "get_publisher", gp)
    monkeypatch.setattr("ave.commands.publish.get_publisher", gp)
    return made


def test_queues_latest_version_per_clip_and_platform_with_sidecar_metadata(ep, vertical, fakes, capsys):
    _export(ep, vertical, "hook-one", 1, "youtube", title="Old title")
    _export(ep, vertical, "hook-one", 2, "youtube", title="New title")
    _export(ep, vertical, "hook-one", 2, "facebook", title="New title")
    rc = main(["publish", "queue", str(ep.root), "--from-clips", "--ai-disclosure", "no"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and len(out) == 2                       # v2 youtube + v2 facebook; the old v1 is NOT queued
    items = {i["platforms"][0]: i for i in queue.list_items(ep)}
    assert set(items) == {"youtube", "facebook"}
    assert items["youtube"]["title"] == "New title" and items["youtube"]["description"] == "desc v2"
    assert "v2" in items["youtube"]["video"]


def test_failed_limits_or_licenses_are_skipped_and_reported_never_queued(ep, vertical, fakes, capsys):
    _export(ep, vertical, "too-long", 1, "youtube", ok=False)
    _export(ep, vertical, "unlicensed", 1, "youtube", lic_ok=False)
    _export(ep, vertical, "fine", 1, "youtube")
    main(["publish", "queue", str(ep.root), "--from-clips", "--ai-disclosure", "no"])
    cap = capsys.readouterr()
    assert [i["title"] for i in queue.list_items(ep)] == ["A title"] and len(queue.list_items(ep)) == 1
    skipped = json.loads(cap.err)["skipped"]
    assert {s["clip"] for s in skipped} == {"too-long", "unlicensed"}
    assert any("too long" in r for s in skipped for r in s["reasons"]) and any("license" in r for s in skipped for r in s["reasons"])


def test_no_sidecars_is_a_clear_error(ep, fakes):
    (ep.exports / "shorts").mkdir(parents=True)
    with pytest.raises(SystemExit, match="ave shorts export"):
        main(["publish", "queue", str(ep.root), "--from-clips", "--ai-disclosure", "no"])
