"""First-run polish found by the fresh-install QA: nothing lands in a git checkout, bad ids are one-liners, the demo says what is next."""

import pytest

from ave import cli, workspace
from ave.commands import demo


@pytest.fixture
def repo_cwd(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AVE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("AVE_WORKSPACE", raising=False)
    monkeypatch.setattr(workspace, "CONFIG", tmp_path / "cfg" / "workspace.json")
    return repo


def test_relative_init_episode_inside_a_git_checkout_is_refused(repo_cwd):
    with pytest.raises(SystemExit) as e:
        cli.main(["init-episode", "ep-001"])
    assert "git checkout" in str(e.value) and "ave workspace" in str(e.value)
    assert not (repo_cwd / "ep-001").exists()


def test_init_episode_absolute_path_or_workspace_is_fine(repo_cwd, tmp_path, capsys):
    assert cli.main(["init-episode", str(tmp_path / "elsewhere" / "ep-001")]) == 0
    ws = tmp_path / "ws"
    workspace.set_workspace(ws)
    assert cli.main(["init-episode", "ep-002"]) == 0
    assert (ws / "ep-002" / "episode.json").exists() and not (repo_cwd / "ep-002").exists()


def test_relative_init_episode_outside_a_repo_still_works(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AVE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("AVE_WORKSPACE", raising=False)
    monkeypatch.setattr(workspace, "CONFIG", tmp_path / "cfg" / "workspace.json")
    if workspace.in_git_checkout(tmp_path):
        pytest.skip("tmp dir is itself inside a git checkout")
    assert cli.main(["init-episode", "ep-001"]) == 0


def test_demo_default_dir_avoids_git_checkouts(repo_cwd, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert demo.default_dir(repo_cwd) == (tmp_path / "home" / "ave-demo")
    plain = tmp_path / "plain"
    plain.mkdir()
    if not workspace.in_git_checkout(plain):
        assert demo.default_dir(plain) == plain.resolve() / "ave-demo"


def test_demo_continue_notes_name_whisper_model_and_playwright(monkeypatch):
    from ave import config
    monkeypatch.setattr(config, "fluidaudio_cli", lambda: None)
    monkeypatch.setattr(config, "whisper_cli", lambda: None)
    monkeypatch.setitem(__import__("sys").modules, "playwright", None)   # makes `import playwright` raise ImportError
    text = " ".join(demo._continue_notes())
    assert "1.6 GB" in text and "Playwright" in text and "playwright install chromium" in text


def test_missing_queue_item_is_a_one_line_error_not_a_traceback(tmp_path, capsys, human):
    ep = tmp_path / "e"
    assert cli.main(["init-episode", str(ep)]) == 0
    capsys.readouterr()
    assert cli.main(["publish", "approve", str(ep), "nosuch"]) == 1
    assert "no queue item" in capsys.readouterr().err
    assert cli.main(["publish", "show", str(ep), "nosuch"]) == 1
    err = capsys.readouterr().err
    assert "no queue item 'nosuch'" in err and "Traceback" not in err
