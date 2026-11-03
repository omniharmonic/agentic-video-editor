"""The standalone app's agent surface: `--cli`, `--install-cli`, and the human gate staying in place."""
import json
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
    script.write_text(f"from pathlib import Path\nPath({str(tmp_path / I.SCRIPTS / 'resolve_bridge.py')!r}).write_text('')\n")
    assert I.install_bridge(script, tmp_path, tmp_path / "nope")["status"].startswith("skipped")
    assert I.install_bridge(script, tmp_path, tmp_path)["status"] == "installed" and (tmp_path / I.SCRIPTS).is_dir()


def test_a_bridge_install_that_never_wrote_the_menu_entry_is_a_failure(tmp_path):
    script = tmp_path / "i.py"
    script.write_text("")           # the upstream installer exits 0 after skipping a folder
    assert I.install_bridge(script, tmp_path, tmp_path)["status"].startswith("failed")


def test_a_developers_own_skill_link_is_left_alone(tmp_path):
    skills = _skills(tmp_path / "AVE Studio.app")
    links = tmp_path / "home/.claude/skills"
    links.mkdir(parents=True)
    (links / "ave-one").symlink_to(tmp_path / "my-checkout")
    assert I.link_skills(tmp_path / "home", skills)["skipped"] == ["ave-one"]
    assert (links / "ave-one").readlink() == tmp_path / "my-checkout"


def test_a_project_folder_is_found_inside_the_folder_the_person_chose(tmp_path):
    from ave.studio import episode_registry as ER
    for name in ("00", "01"):
        (tmp_path / "episodes" / name).mkdir(parents=True)
        (tmp_path / "episodes" / name / "episode.json").write_text("{}")
    (tmp_path / "episodes" / "notes").mkdir()
    real = (tmp_path / "episodes").resolve()
    assert ER.project_root(real / "00", None) == real / "00"
    assert ER.project_root(real, lambda rows: rows[1]) == real / "01"
    assert ER.project_root(real, lambda rows: None) is None
    (real / "01" / "episode.json").unlink()
    assert ER.project_root(real, None) == real / "00"      # only one project: no question needed
    with pytest.raises(ValueError, match="No project found"):
        ER.project_root(real / "notes", None)


def test_a_symlink_above_the_project_is_resolved_not_refused(tmp_path):
    from ave.studio import episode_registry as ER
    (tmp_path / "drive/show").mkdir(parents=True)
    (tmp_path / "drive/show/episode.json").write_text("{}")
    (tmp_path / "link").symlink_to(tmp_path / "drive")
    root = ER.project_root(tmp_path / "link/show", None)
    assert root == (tmp_path / "drive/show").resolve() and ER.regular(root, directory=True) == root


def test_native_library_import_copies_and_leaves_the_original(tmp_path):
    from PIL import Image
    from ave.desktop_files import import_library
    from studio_helpers import build_studio
    built = build_studio(tmp_path / "s")
    original = tmp_path / "photo.png"
    Image.new("RGB", (20, 20), "red").save(original)
    before = original.read_bytes()
    first = import_library(built.studio, [original])
    assert original.read_bytes() == before and first[0]["name"] == "photo.png" and not first[0]["duplicate"]
    assert json.dumps(first) and import_library(built.studio, [original])[0]["duplicate"]
    assert not list((built.studio.sp.work / "native-intake").iterdir())


def test_native_library_import_refuses_links(tmp_path):
    from ave.desktop_files import import_library
    from studio_helpers import build_studio
    built = build_studio(tmp_path / "s")
    (tmp_path / "real.png").write_bytes(b"x")
    (tmp_path / "link.png").symlink_to(tmp_path / "real.png")
    with pytest.raises(ValueError, match="not links"):
        import_library(built.studio, [tmp_path / "link.png"])
