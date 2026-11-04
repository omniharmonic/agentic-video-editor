"""The allowlist behind the Workspace's Jobs panel (ave/jobs.py, moved from the retired `ave ui`). The gate is unchanged: a fixed
list of `ave` steps with typed arguments, the episode injected, nothing that publishes, approves, sends or takes a path."""

from __future__ import annotations

import pytest

from ave import jobs as ujobs
from ave.episode import Episode


@pytest.fixture
def ep(tmp_path):
    e = Episode(tmp_path / "ep")
    e.create("Jobs")
    return e


@pytest.mark.parametrize("cmd,args", [
    ("publish run", {}), ("publish approve", {}), ("publish", {}), ("rm -rf /", {}), ("status; ls", {}), ("clips decide", {}), ("", {}),
    ("status", {"allow_unapproved": True}), ("content render", {"allow_unlicensed": True}), ("content render", {"allow-unlicensed": True}),
    ("render", {"out": "/tmp/x.mp4"}), ("render", {"env": {"X": "1"}}), ("clips prepare", {"name": "../../raw"}),
    ("clips prepare", {"name": "s1; rm"}), ("clips prepare", {"min_s": "20"}), ("clips prepare", {"min_s": True}), ("clips prepare", {"min_s": -1}),
    ("render", {"stage": "../../etc"}), ("status", {"path": "/Volumes/EXTERNAL"}), ("clips list", {"all": "yes"}),
])
def test_job_allowlist_refuses(ep, cmd, args):
    with pytest.raises(ujobs.JobRefused):
        ujobs.build_argv(ep, cmd, args)


def test_job_argv_is_structured_and_episode_is_injected(ep):
    argv = ujobs.build_argv(ep, "clips prepare", {"min_s": 15, "max_s": 40.5, "name": "s1", "stage": "rough"})
    assert argv[:3] == ["clips", "prepare", str(ep.root)] and ["--min-s", "15"] == argv[argv.index("--min-s"):argv.index("--min-s") + 2]
    assert ujobs.build_argv(ep, "clips review", {"drafts": True}) == ["clips", "review", str(ep.root), "--drafts"]
    assert ujobs.build_argv(ep, "clips review", {"drafts": False}) == ["clips", "review", str(ep.root)]


def test_no_allowlisted_command_publishes_or_approves():
    for cmd, spec in ujobs.ALLOWED.items():
        assert spec["argv"][0] != "publish" or spec["argv"] == ["publish", "list"], cmd
        assert "decide" not in spec["argv"] and "approve" not in spec["argv"]


def test_child_env_marks_agent_and_drops_the_signing_key(monkeypatch):
    monkeypatch.setenv("AVE_APPROVAL_KEY", "k" * 64)
    env = ujobs.child_env()
    assert "AVE_APPROVAL_KEY" not in env and env["AVE_AGENT"] == "1"


def test_ave_ui_is_retired_and_points_at_the_app(capsys, tmp_path):
    from ave import cli
    assert cli.main(["ui", str(tmp_path)]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1 and out[0].startswith("ave ui is retired") and "ave desktop" in out[0]


def test_cancel_stops_the_whole_process_group_and_says_cancelled_only_when_it_is_gone(ep, tmp_path, monkeypatch):
    """A step that starts its own child (as `ave render` starts ffmpeg), both ignoring SIGTERM: Cancel reaches the child too, the row
    reads "stopping" until nothing is left, and no second step starts meanwhile."""
    import os
    import time
    pidfile = tmp_path / "child.pid"
    monkeypatch.setattr(ujobs, "GRACE_S", 1.0)
    monkeypatch.setattr(ujobs, "CHILD_CODE", (
        "import os, signal, subprocess, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "c = subprocess.Popen([sys.executable, '-c', 'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(c.pid))\n"
        "time.sleep(60)\n"))
    r = ujobs.JobRunner(ep)
    job = r.start("status", {})
    end = time.time() + 10
    while not pidfile.exists() and time.time() < end:
        time.sleep(0.05)
    child = int(pidfile.read_text())
    assert r.cancel(job["id"])["status"] == "stopping"
    with pytest.raises(ujobs.JobRefused):
        r.start("status", {})                                                   # the first is still stopping
    end = time.time() + 15
    while r.jobs[job["id"]]["status"] != "cancelled" and time.time() < end:
        assert r.jobs[job["id"]]["status"] == "stopping"
        time.sleep(0.05)
    assert r.jobs[job["id"]]["status"] == "cancelled"
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)                                                       # the grandchild is gone too
    monkeypatch.setattr(ujobs, "CHILD_CODE", "import sys; sys.exit(0)")
    assert r.start("status", {})["status"] == "running"                         # and a new step may start
