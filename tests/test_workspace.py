import pytest

from ave import workspace
from ave.episode import Episode


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(workspace, "CONFIG", tmp_path / "cfg" / "workspace.json")
    monkeypatch.delenv("AVE_WORKSPACE", raising=False)


def test_bare_name_resolves_inside_the_workspace(tmp_path):
    ws = workspace.set_workspace(tmp_path / "The Long Table")
    ep = Episode("ep-001")
    ep.create()
    assert ep.root == (ws / "ep-001").resolve() and (ws / "ep-001" / "episode.json").exists()
    assert [d.name for d in workspace.episodes()] == ["ep-001"]


def test_explicit_paths_and_existing_dirs_still_win(tmp_path):
    workspace.set_workspace(tmp_path / "ws")
    other = tmp_path / "elsewhere" / "ep"
    assert Episode(other).root == other.resolve()


def test_workspace_cannot_be_inside_the_repo():
    from ave import config
    with pytest.raises(SystemExit, match="outside the repo"):
        workspace.set_workspace(config.REPO_ROOT / "episodes")


def test_env_var_overrides_config(tmp_path, monkeypatch):
    workspace.set_workspace(tmp_path / "a")
    monkeypatch.setenv("AVE_WORKSPACE", str(tmp_path / "b"))
    assert workspace.get() == tmp_path / "b"
