"""`ave doctor`: verify the machine, grouped by how much each thing matters, with the exact fix command per item.

Groups
  required   everything the CLI, the tests and `ave demo` need (FFmpeg, Python, bundled data). A FAIL here makes the exit code non-zero.
  footage    what you need to turn real recordings into a transcript (an ASR engine), plus Apple-only extras
  optional   DaVinci Resolve review, motion graphics, music analysis, publishing

Optional things that are absent are WARN or SKIP, never FAIL: an agent can gate on the exit code and still be right.
"""

from __future__ import annotations

import importlib.util
import json
import platform
import socket
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from . import config, resources

OK, WARN, FAIL, INFO, SKIP = "OK", "WARN", "FAIL", "INFO", "SKIP"
REQUIRED, FOOTAGE, OPTIONAL = "required", "footage", "optional"
TITLES = {REQUIRED: "Required (CLI, tests, demo)", FOOTAGE: "For real footage (transcription)", OPTIONAL: "Optional (Resolve, motion graphics, music, publishing)"}

IS_MAC = sys.platform == "darwin"
APPLE_SILICON = IS_MAC and platform.machine() == "arm64"


@dataclass
class Check:
    status: str
    name: str
    detail: str
    fix: str = ""
    required: bool = True
    group: str = REQUIRED


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


def _importable(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except Exception:
        return False


def _install_ffmpeg() -> str:
    if IS_MAC:
        return "brew install ffmpeg"
    if sys.platform.startswith("linux"):
        return "sudo apt-get install ffmpeg   (Debian/Ubuntu; dnf/pacman/apk have an `ffmpeg` package too)"
    return "install FFmpeg from https://ffmpeg.org/download.html and put it on PATH (or use WSL)"


def _checks() -> list[Check]:
    c: list[Check] = []

    def add(status, name, detail, fix="", group=OPTIONAL):
        c.append(Check(status, name, detail, fix, required=(group == REQUIRED), group=group))

    # ---- REQUIRED
    for tool in ("ffmpeg", "ffprobe"):
        p = config.binary(tool)
        add(OK if p else FAIL, tool, str(p) if p else "not found on PATH", _install_ffmpeg(), REQUIRED)
    add(OK if sys.version_info >= (3, 11) else FAIL, "Python", sys.version.split()[0], "uv python install 3.12 && uv sync", REQUIRED)
    try:
        add(OK, "Bundled config, skills, motion-kit", resources.source(), "", REQUIRED)
    except SystemExit as e:
        add(FAIL, "Bundled config, skills, motion-kit", str(e), "set AVE_HOME to a checkout of the repository", REQUIRED)
    for label, mod in (("numpy", "numpy"), ("scipy", "scipy"), ("soundfile", "soundfile"), ("PyYAML", "yaml"), ("Pillow", "PIL")):
        if not _importable(mod):
            add(FAIL, f"Python package {label}", "missing", "uv sync   (or: pip install agentic-video-editor)", REQUIRED)

    # ---- FOOTAGE: transcription
    fa = config.fluidaudio_cli()
    wc, wm = config.whisper_cli(), config.whisper_model()
    if APPLE_SILICON:
        add(OK if fa else WARN, "FluidAudio (fast ASR + speaker diarization)", str(fa) if fa else "not built",
            "scripts/setup.sh   (or just scripts/build-fluidaudio.sh; needs Xcode Command Line Tools: xcode-select --install)", FOOTAGE)
        if fa:
            models = Path.home() / "Library/Application Support/FluidAudio/Models"
            have = sorted(m.name for m in models.iterdir()) if models.is_dir() else []
            add(OK if have else INFO, "FluidAudio models", ", ".join(have) or "none cached yet (downloaded on first `ave transcribe`)", "", FOOTAGE)
    else:
        add(SKIP, "FluidAudio (fast ASR + speaker diarization)",
            "needs macOS on Apple Silicon (CoreML); use whisper.cpp here" if not IS_MAC else "needs Apple Silicon (this Mac is Intel); use whisper.cpp", "", FOOTAGE)
    add(OK if wc else WARN, "whisper-cli (whisper.cpp)", str(wc) if wc else "not found",
        "brew install whisper-cpp" if IS_MAC else "build whisper.cpp (https://github.com/ggml-org/whisper.cpp) and put whisper-cli on PATH", FOOTAGE)
    add(OK if wm else WARN, "whisper.cpp model", str(wm) if wm else f"{config.WHISPER_MODEL_NAME} missing (about 1.6 GB)",
        "scripts/fetch-whisper-model.sh", FOOTAGE)
    if not fa and not (wc and wm):
        add(WARN, "A transcription engine", "none ready: `ave transcribe` cannot run on real footage",
            "install either FluidAudio (Apple Silicon) or whisper.cpp + its model (anywhere). `ave demo` does not need one.", FOOTAGE)
    else:
        add(OK, "A transcription engine", "FluidAudio" if fa else "whisper.cpp", "", FOOTAGE)

    # ---- OPTIONAL: extras (Python packages)
    extras = [("Motion graphics (playwright)", "playwright", "uv sync --extra motion"),
              ("Music analysis (librosa)", "librosa", "uv sync --extra audio"),
              ("B-roll face check (OpenCV)", "cv2", "uv sync --extra vision"),
              ("Publishing (google-api, keyring)", "googleapiclient", "uv sync --extra publish")]
    for label, mod, fix in extras:
        ok = _importable(mod)
        add(OK if ok else WARN, label, "available" if ok else "not installed", fix)
    if IS_MAC:
        ok = _importable("Vision")
        add(OK if ok else WARN, "Apple Vision face detection (shorts reframing)", "available" if ok else "not installed", "uv sync --extra vision")
    else:
        add(SKIP, "Apple Vision face detection (shorts reframing)", "macOS only; shorts fall back to centre crops", "")
    if _importable("playwright"):
        roots = [Path.home() / "Library/Caches/ms-playwright", Path.home() / ".cache/ms-playwright"]
        has_chromium = any(r.is_dir() and any(p.name.startswith("chromium") for p in r.iterdir()) for r in roots)
        add(OK if has_chromium else WARN, "Chromium for motion graphics", "installed" if has_chromium else "missing",
            "python -m playwright install chromium")

    # ---- OPTIONAL: DaVinci Resolve
    if not IS_MAC:
        add(SKIP, "DaVinci Resolve handoff", "set up and tested on macOS only; everything up to the final cut works without it "
            "(you can still open the rendered files in any editor)", "")
    else:
        app = config.resolve_app()
        ver = _run(["defaults", "read", f"{app}/Contents/Info", "CFBundleShortVersionString"]) if app else ""
        if not app:
            add(WARN, "DaVinci Resolve", "not installed (only needed for `ave handoff`)", "Install Resolve 21.0.x (free) from https://www.blackmagicdesign.com")
        else:
            add(OK, "DaVinci Resolve", f"{ver} at {app}")
            if ver.startswith("21.") and not ver.startswith("21.0"):
                add(WARN, "Resolve version", f"{ver}: free 21.1+ no longer lists Python scripts", "Use Resolve 21.0.x free, or Studio")
        usr_local = Path("/usr/local/bin/python3")
        p3h = _run(["launchctl", "getenv", "PYTHON3HOME"])
        if usr_local.exists():
            add(OK, "Resolve Python discovery", f"/usr/local/bin/python3 -> {usr_local.resolve()}")
        elif p3h:
            add(WARN, "Resolve Python discovery", f"PYTHON3HOME={p3h} (lost on reboot)", "scripts/persist-python3home.sh  (one-time, needs sudo)")
        else:
            add(WARN, "Resolve Python discovery", "PYTHON3HOME unset and no /usr/local/bin/python3", "scripts/persist-python3home.sh  (one-time, needs sudo)")
        mcp, py = config.resolve_mcp_dir(), config.resolve_mcp_python()
        add(OK if mcp and py else WARN, "Resolve MCP server", f"{mcp}" if mcp and py else "not installed",
            "scripts/setup.sh   (full mode clones davinci-resolve-mcp into vendor/)")
        port = 49632
        try:
            if config.BRIDGE_CONFIG.exists():
                port = json.loads(config.BRIDGE_CONFIG.read_text()).get("port", port)
        except Exception:
            pass
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                add(OK, "Resolve bridge", f"listening on 127.0.0.1:{port}")
        except OSError:
            add(WARN, "Resolve bridge", f"nothing listening on :{port}", "In Resolve: Workspace > Scripts > resolve_bridge (once per Resolve launch)")
    return c


def verdict(checks: list[Check]) -> tuple[str, int]:
    """(one line, exit code)."""
    req = [c for c in checks if c.group == REQUIRED and c.status == FAIL]
    if req:
        return f"NOT READY: {len(req)} required item(s) missing ({', '.join(c.name for c in req)}). Fix those first.", 1
    foot = [c for c in checks if c.group == FOOTAGE and c.name == "A transcription engine"]
    no_asr = bool(foot) and foot[0].status != OK
    opt = [c for c in checks if c.group == OPTIONAL and c.status == WARN]
    if no_asr:
        return "READY for `ave demo` and tests. For real footage you still need a transcription engine (see above).", 0
    if opt:
        return f"READY for real footage. {len(opt)} optional item(s) not set up (Resolve handoff, motion graphics, etc.); each stage says so if you reach it.", 0
    return "READY: everything is set up.", 0


def run(as_json: bool = False) -> int:
    checks = _checks()
    line, code = verdict(checks)
    if as_json:
        print(json.dumps([asdict(c) for c in checks], indent=2))
        return code
    print(f"ave doctor  (platform: {platform.system()} {platform.machine()}, Python {sys.version.split()[0]})")
    for group in (REQUIRED, FOOTAGE, OPTIONAL):
        rows = [c for c in checks if c.group == group]
        if not rows:
            continue
        print(f"\n{TITLES[group]}")
        for c in rows:
            print(f"  [{c.status:<4}] {c.name}: {c.detail}")
            if c.status in (WARN, FAIL) and c.fix:
                print(f"         fix: {c.fix}")
    print(f"\n{line}")
    return code
