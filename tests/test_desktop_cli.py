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


# ---- the approval sheets: the web view may only name what to approve; the native side does the rest
ORIGIN = "http://127.0.0.1:4100/"


def _ask(action, args):
    from ave.desktop_native import native_message
    return native_message({"id": "1", "action": action, "args": args}, ORIGIN + "app/", ORIGIN)


def test_the_approval_sheets_are_native_actions_that_take_only_what_to_approve():
    from ave.desktop_approvals import approval_args
    sha = "a" * 64
    one = {"post": "p_1", "variant": "v_ig", "sha": sha}
    assert _ask("approvePublish", {"items": [one]})[1] == "approvePublish"
    assert approval_args("approvePublish", {"items": [one, {**one, "variant": "v_fb"}]})["items"][1]["variant"] == "v_fb"   # several at once
    assert approval_args("approveTrim", {"project": "prj_abc", "session": "main"}) == {"project": "prj_abc", "session": "main"}
    for extra in ({"pin": "424242"}, {"by": "maren"}, {"displayed_ms": 9000}):    # the page can never supply the PIN, a name or a view
        with pytest.raises(ValueError):
            approval_args("approvePublish", {"items": [one], **extra})
        with pytest.raises(ValueError):
            approval_args("approvePublish", {"items": [{**one, **extra}]})
        with pytest.raises(ValueError):
            approval_args("approveTrim", {"project": "prj_abc", "session": "main", **extra})
    for bad in ({"items": []}, {"items": [one] * 51}, {"items": [one, one]}, {"items": [{**one, "sha": "abc"}]},
                {"items": [{**one, "post": ".."}]}, {"items": [{**one, "variant": "."}]}):
        with pytest.raises(ValueError):
            approval_args("approvePublish", bad)
    with pytest.raises(ValueError):
        approval_args("approveTrim", {"project": "../../etc", "session": "main"})
    assert approval_args("ackAlert", {"alert": "al_0001"}) == {"alert": "al_0001"}
    assert approval_args("acceptProposal", {"proposal": "sp_1"}) == {"proposal": "sp_1"}
    assert approval_args("pickBroll", {"search": "bs_1", "candidate": "pexels:123", "by": "Maren"})["candidate"] == "pexels:123"
    relayed = {"artifact": "prj_abc:art_ep", "event": "01JB7Q3W6XN2V4Y8Z0A1B2C3D4"}
    assert approval_args("confirmRelayed", relayed) == relayed and approval_args("undoRelayed", relayed) == relayed
    for action, bad in (("confirmRelayed", {**relayed, "by": "Maren"}), ("confirmRelayed", {**relayed, "artifact": "../x"}),
                        ("undoRelayed", {"artifact": "prj_abc:art_ep"}),
                        ("ackAlert", {"alert": "al_1", "by": "Maren"}), ("pickBroll", {"search": "bs_1", "candidate": "pexels:1"}),
                        ("pickBroll", {"search": "..", "candidate": "x", "by": "Maren"}), ("acceptProposal", {"proposal": "sp 1"})):
        with pytest.raises(ValueError):
            approval_args(action, bad)


def test_the_bridge_offers_both_sheets_and_nothing_for_sequence_approval():
    from ave.desktop_native import ACTIONS, BRIDGE_SCRIPT
    assert "approvePublish:items=>ask('approvePublish',{items})" in BRIDGE_SCRIPT
    for x in ("ackAlert:id=>ask('ackAlert',{alert:id})", "pickBroll:(search,candidate,by)=>ask('pickBroll',{search,candidate,by})",
              "acceptProposal:id=>ask('acceptProposal',{proposal:id})", "confirmRelayed:(key,event)=>ask('confirmRelayed',{artifact:key,event})",
              "undoRelayed:(key,event)=>ask('undoRelayed',{artifact:key,event})"):
        assert x in BRIDGE_SCRIPT
    assert "approveTrim:(project,session)=>ask('approveTrim',{project,session})" in BRIDGE_SCRIPT
    assert not any("sequence" in a.lower() for a in ACTIONS)              # `ave sequence approve` stays a terminal act
    with pytest.raises(ValueError, match="Unknown native action"):
        _ask("approveSequence", {})


def test_an_agent_marker_refuses_the_approval_sheets_before_anything_is_read(monkeypatch):
    from ave.desktop_approvals import Approvals, refuse_agent
    monkeypatch.setenv("CLAUDECODE", "1")
    with pytest.raises(ValueError, match="agent marker"):
        refuse_agent()
    sheets = Approvals(app=None, workspace=None)                        # refuses before touching the studio
    with pytest.raises(ValueError, match="agent marker"):
        sheets.publish_request([{"post": "p_1", "variant": "v_ig", "sha": "a" * 64}])
    with pytest.raises(ValueError, match="agent marker"):
        sheets.trim_request("prj_abc", "main")


def test_sequence_approve_is_unchanged_a_terminal_act(monkeypatch, tmp_path):
    from ave import humanpresence
    from ave.cli import main
    for k in humanpresence.AGENT_MARKERS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CLAUDECODE", "1")
    with pytest.raises(SystemExit) as e:
        main(["sequence", "approve", str(tmp_path)])
    assert "human act" in str(e.value) and f"ave sequence approve {tmp_path}" in str(e.value)
