"""Native window around the existing loopback studio. No JS/Python bridge.

The human terminal remains the approval surface. Closing the last window releases
both the HTTP socket and the temporary fixture studio, even after GUI failures.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import plistlib
import shlex
import sys
import threading

from . import humanpresence


def availability() -> dict:
    return {"native_runtime": importlib.util.find_spec("webview") is not None,
            "install": 'uv sync --extra desktop  (checkout), or pip install "agentic-video-editor[desktop]"',
            "platform": sys.platform, "bind": "127.0.0.1", "sends": False}


def launch(studio_path: str | None = None, *, fixtures: bool = False, by: str | None = None, webview_module=None) -> int:
    if not fixtures:
        line = shlex.join(["ave", "desktop"] + ([studio_path] if studio_path else []))
        humanpresence.require_human("`ave desktop` (it can approve posts)", line)
    if webview_module is None:
        try:
            import webview as webview_module
        except ImportError:
            raise SystemExit("Native window runtime is missing. " + availability()["install"]) from None
    from .studio.ui.server import StudioServer
    from .studio.service import FixtureStudio
    if fixtures:
        studio = FixtureStudio()
    else:
        from .studio.project import find_studio
        from .studio.cli import real_studio
        sp = find_studio(studio_path)
        sp.require()
        studio = real_studio(sp)
    srv = StudioServer(studio, by=by, fixtures=fixtures)
    worker = threading.Thread(target=srv.serve_forever, name="ave-desktop-http", daemon=True)
    worker.start()
    try:
        label = "Sample studio" if fixtures else studio.sp.read_meta().get("title", "Studio")
        print(f"AVE Studio: {label}\nApproval PIN (keep in this terminal): {srv.app.pin.reveal()}\n"
              "Closing the window stops the local server. Nothing is sent by this window.", flush=True)
        if hasattr(webview_module, "settings"):
            webview_module.settings["ALLOW_DOWNLOADS"] = True
            webview_module.settings["ALLOW_FILE_URLS"] = False
        # The OS webview consumes the same authenticated URL as the browser. No
        # js_api object, local-file server, remote debugging or shell capability.
        webview_module.create_window(f"{label} · AVE Studio", srv.app_url(), width=1440, height=960,
                                     min_size=(760, 560))
        webview_module.start(private_mode=True)
    finally:
        srv.shutdown()
        worker.join(timeout=5)
        srv.server_close()
    return 0


def build_launcher(output: str | Path, *, executable: str | Path | None = None, studio_path: str | Path | None = None) -> Path:
    """Create a macOS launcher, not a standalone runtime. Never overwrite a bundle.

    Finder opens Terminal; the PERSON's terminal launches the native window and
    retains the PIN. No pty, marker removal, daemon installation or auto-approval.
    """
    target = Path(output).expanduser().absolute()
    if target.suffix != ".app":
        raise ValueError("the launcher output must end in .app")
    if target.exists():
        raise ValueError(f"refusing to overwrite {target}")
    # Preserve a venv's executable symlink: resolving it selects the base Python
    # and drops the environment where ave and its desktop runtime are installed.
    exe = Path(executable or sys.executable).expanduser().absolute()
    if not exe.is_file():
        raise ValueError(f"Python executable does not exist: {exe}")
    args = [str(exe), "-m", "ave.cli", "desktop"]
    if studio_path is not None:
        args.append(str(Path(studio_path).expanduser().resolve()))
    command = shlex.join(args)
    contents = target / "Contents"
    macos, data = contents / "MacOS", contents / "Resources"
    # mkdir's exclusive creation prevents concurrent builders replacing a bundle.
    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir()
    macos.mkdir(parents=True)
    data.mkdir()
    info = {"CFBundleName": "AVE Studio", "CFBundleDisplayName": "AVE Studio", "CFBundleIdentifier": "local.ave.studio",
            "CFBundleVersion": "1", "CFBundleShortVersionString": "0.1.0", "CFBundlePackageType": "APPL",
            "CFBundleExecutable": "AVE Studio", "LSMinimumSystemVersion": "12.0"}
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))
    runner = data / "Studio.command"
    runner.write_text("#!/bin/sh\n" + command + "\n", encoding="utf-8")
    runner.chmod(0o700)
    launcher = macos / "AVE Studio"
    launcher.write_text('#!/bin/sh\nset -eu\nHERE=$(CDPATH= cd -- "$(dirname -- "$0")/../Resources" && pwd)\n'
                        'exec /usr/bin/open -a Terminal "$HERE/Studio.command"\n', encoding="utf-8")
    launcher.chmod(0o700)
    (data / "README.txt").write_text("This launcher uses the Python installation on this computer:\n" + str(exe) +
        "\nInstall the desktop extra and configure a studio before opening.\n"
        "The terminal retains your private approval PIN. This is unsigned and is not a portable standalone application.\n")
    return target
