"""The standalone app's agent surface: `--cli`, `--install-cli`, and the human gate staying in place."""
import stat
import sys
from pathlib import Path

import pytest

from ave import desktop_install as I
from ave import desktop_worker as W


def _skills(root):
    (root / "ave-one").mkdir(parents=True)
    (root / "ave-two").mkdir()
    return root


def test_cli_dispatch_reaches_the_ordinary_cli(monkeypatch):
    seen = []
    monkeypatch.setattr("ave.cli.main", lambda argv: seen.append(argv) or 7)
    assert W.dispatch(["--cli", "status", "ep"]) == 7 and seen == [["status", "ep"]]


def test_cli_dispatch_does_not_clear_agent_markers_or_grant_approval(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("AVE_APPROVAL_KEY", "kept")
    monkeypatch.setattr("ave.cli.main", lambda argv: 0)
    W.dispatch(["--cli", "status"])
    from ave import humanpresence
    assert "CLAUDECODE" in humanpresence.agent_markers_set() and not humanpresence.is_human_terminal()


def test_shim_is_executable_quotes_the_app_path_and_passes_arguments(tmp_path):
    exe = Path("/Applications/AVE Studio.app/Contents/MacOS/AVE Studio")
    res = I.install_shim(tmp_path, exe)
    shim = tmp_path / ".local/bin/ave"
    assert res["status"] == "installed" and shim.stat().st_mode & stat.S_IXUSR
    assert "'/Applications/AVE Studio.app/Contents/MacOS/AVE Studio' --cli \"$@\"" in shim.read_text()


def test_shim_never_replaces_an_ave_it_did_not_write_and_refreshes_its_own(tmp_path):
    shim = tmp_path / ".local/bin/ave"
    shim.parent.mkdir(parents=True)
    shim.write_text("#!/bin/sh\necho mine\n")
    assert I.install_shim(tmp_path, Path("/a"))["status"].startswith("skipped") and "mine" in shim.read_text()
    shim.write_text(I.shim_text(Path("/old")))
    I.install_shim(tmp_path, Path("/new"))
    assert "/new" in shim.read_text()


def test_skills_link_without_touching_a_folder_the_person_owns(tmp_path):
    skills = _skills(tmp_path / "bundle")
    own = tmp_path / "home/.claude/skills/ave-one"
    own.mkdir(parents=True)
    res = I.link_skills(tmp_path / "home", skills)
    assert res == {"path": str(own.parent), "linked": 1, "skipped": ["ave-one"]}
    assert (own.parent / "ave-two").resolve() == skills / "ave-two" and not own.is_symlink()


def test_a_moved_app_refreshes_stale_skill_links(tmp_path):
    old, new = _skills(tmp_path / "AVE Studio old"), _skills(tmp_path / "AVE Studio new")
    home = tmp_path / "home"
    I.link_skills(home, old)
    I.link_skills(home, new)
    assert (home / ".claude/skills/ave-one").resolve() == new / "ave-one"


def test_a_failed_bridge_install_is_reported_not_hidden(tmp_path):
    script = tmp_path / "i.py"
    script.write_text("raise SystemExit(3)\n")
    assert I.install_bridge(script, tmp_path, tmp_path)["status"] == "failed: installer exited 3"


def test_bridge_waits_for_resolve_and_makes_the_folder_resolve_has_not_made_yet(tmp_path):
    script = tmp_path / "i.py"
    script.write_text("")
    assert I.install_bridge(script, tmp_path, tmp_path / "nope")["status"].startswith("skipped")
    assert I.install_bridge(script, tmp_path, tmp_path)["status"] == "installed" and (tmp_path / I.SCRIPTS).is_dir()


def test_a_developers_own_skill_link_is_left_alone(tmp_path):
    skills = _skills(tmp_path / "AVE Studio.app")
    links = tmp_path / "home/.claude/skills"
    links.mkdir(parents=True)
    (links / "ave-one").symlink_to(tmp_path / "my-checkout")
    assert I.link_skills(tmp_path / "home", skills)["skipped"] == ["ave-one"]
    assert (links / "ave-one").readlink() == tmp_path / "my-checkout"
