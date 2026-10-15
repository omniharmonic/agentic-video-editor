"""Locate every external tool in one place.

Each tool has an env-var override, then a repo-local default, then a PATH or
well-known fallback. Skills and scripts never hard-code paths; they call these.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import resources

# REPO_ROOT is the historical name for "where config/, skills/, motion-kit/ live": a checkout, $AVE_HOME, or packaged data.
REPO_ROOT = resources.home()
# Third-party tools (FluidAudio, the Resolve MCP server) are cloned here: <checkout>/vendor, or ~/.local/share/ave/vendor when installed.
VENDOR = Path(os.environ["AVE_VENDOR"]).expanduser() if os.environ.get("AVE_VENDOR") else (
    (REPO_ROOT / "vendor") if resources.is_checkout() else resources.user_data_dir() / "vendor")

FLUIDAUDIO_REF = "20d4f0b"
RESOLVE_MCP_REF = "v4.10.6"

BRIDGE_CONFIG = Path(
    os.environ.get("DAVINCI_RESOLVE_BRIDGE_CONFIG", Path.home() / ".config/davinci-resolve-mcp/bridge.json")
)
WHISPER_MODEL_NAME = "ggml-large-v3-turbo.bin"


def _first_existing(*candidates: str | Path | None) -> Path | None:
    for c in candidates:
        if c and Path(c).expanduser().exists():
            return Path(c).expanduser()
    return None


def fluidaudio_cli() -> Path | None:
    return _first_existing(
        os.environ.get("AVE_FLUIDAUDIO"),
        VENDOR / "FluidAudio/.build/release/fluidaudiocli",
    )


def resolve_mcp_dir() -> Path | None:
    return _first_existing(
        os.environ.get("AVE_RESOLVE_MCP"),
        VENDOR / "davinci-resolve-mcp",
    )


def resolve_mcp_python() -> Path | None:
    d = resolve_mcp_dir()
    return _first_existing(d / "venv/bin/python") if d else None


def whisper_cli() -> Path | None:
    found = shutil.which("whisper-cli")
    return Path(found) if found else None


def whisper_model() -> Path | None:
    return _first_existing(
        os.environ.get("AVE_WHISPER_MODEL"),
        Path.home() / "whisper-models" / WHISPER_MODEL_NAME,
        Path.home() / ".cache/whisper-cpp-models" / WHISPER_MODEL_NAME,
    )


def binary(name: str) -> Path | None:
    found = shutil.which(name)
    return Path(found) if found else None


def resolve_app() -> Path | None:
    return _first_existing(
        os.environ.get("RESOLVE_APP"),
        "/Applications/DaVinci Resolve/DaVinci Resolve.app",
        "/Applications/DaVinci Resolve.app",
    )
