"""Desktop uses the existing approval surface and always owns its HTTP lifecycle."""
import json
import plistlib
import shlex
import sys
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from ave import desktop
from ave.cli import main


def test_human_gate_precedes_runtime_and_server(monkeypatch):
    monkeypatch.setattr(desktop.humanpresence, "is_human_terminal", lambda: False)
    with pytest.raises(SystemExit, match="human act"):
        desktop.launch("/missing", webview_module=object())


@pytest.mark.parametrize("failure", [None, "create", "start"])
def test_window_lifecycle_and_cleanup(monkeypatch, failure):
    from ave.studio.ui import server
    from ave.studio import service
    events = []

    class FakeServer:
        def __init__(self, st, **kw):
            assert kw["fixtures"]
            self.app = SimpleNamespace(pin=SimpleNamespace(reveal=lambda: "fixture-pin"))
        def url(self): return "http://127.0.0.1:1234/?t=test"
        def serve_forever(self): events.append("serve")
        def shutdown(self): events.append("shutdown")
        def server_close(self): events.append("close")

    class Native:
        settings = {}
        def create_window(self, title, url, **kw):
            assert url.startswith("http://127.0.0.1:")
            assert "js_api" not in kw
            events.append("create")
            if failure == "create": raise RuntimeError("GUI failed")
        def start(self, **kw):
            assert kw == {"private_mode": True}
            assert self.settings["ALLOW_DOWNLOADS"] is True
            assert self.settings["ALLOW_FILE_URLS"] is False
            events.append("start")
            if failure == "start": raise RuntimeError("GUI failed")

    monkeypatch.setattr(server, "StudioServer", FakeServer)
    monkeypatch.setattr(service, "FixtureStudio", lambda: object())
    if failure:
        with pytest.raises(RuntimeError, match="GUI failed"):
            desktop.launch(fixtures=True, webview_module=Native())
    else:
        assert desktop.launch(fixtures=True, webview_module=Native()) == 0
    assert events[-2:] == ["shutdown", "close"]


def test_launcher_quotes_paths_and_never_overwrites(tmp_path):
    studio = tmp_path / "Tomas's studio $(touch nope)"
    app = desktop.build_launcher(tmp_path / "AVE Studio.app", studio_path=studio)
    plist = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    assert plist["CFBundleExecutable"] == "AVE Studio"
    command = (app / "Contents/Resources/Studio.command").read_text().splitlines()[1]
    assert shlex.split(command) == [str(Path(sys.executable).absolute()), "-m", "ave.cli", "desktop", str(studio.resolve())]
    launcher = (app / "Contents/MacOS/AVE Studio").read_text()
    assert "open -a Terminal" in launcher
    assert "unset" not in launcher and "pty" not in launcher
    with pytest.raises(ValueError, match="overwrite"):
        desktop.build_launcher(app)
    assert not (tmp_path / "nope").exists()


def test_check_is_json_without_native_runtime(capsys):
    assert main(["desktop", "--check"]) == 0
    obj = json.loads(capsys.readouterr().out)
    assert obj["bind"] == "127.0.0.1" and not obj["sends"]


def test_launcher_keeps_virtualenv_interpreter_outside_checkout(tmp_path):
    app = desktop.build_launcher(tmp_path / 'AVE.app')
    command = (app / 'Contents/Resources/Studio.command').read_text().splitlines()[1]
    args = shlex.split(command) + ['--check']
    run = subprocess.run(args, cwd=tmp_path, capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)['bind'] == '127.0.0.1'
