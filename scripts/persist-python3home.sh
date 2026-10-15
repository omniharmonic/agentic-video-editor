#!/usr/bin/env bash
# DaVinci Resolve on macOS finds Python 3 only via $PYTHON3HOME or /usr/local/bin/python3.
# `launchctl setenv` is lost on reboot. This installs the permanent fix: a symlink at
# /usr/local/bin/python3 to a uv-managed CPython that has BOTH bin/python3 and lib/libpython3.X.dylib.
# Needs sudo once. Idempotent. Verify with: ave doctor
set -euo pipefail
[[ "$(uname -s)" == "Darwin" ]] || { echo "This is only needed on macOS (DaVinci Resolve's Python discovery)." >&2; exit 1; }
ARCH="$(uname -m)"; [[ "$ARCH" == "arm64" ]] || ARCH="x86_64"
UVPY="${UV_PYTHON_INSTALL_DIR:-$HOME/.local/share/uv/python}"
PREFIX="$(ls -d "$UVPY"/cpython-3.12*-macos-"$ARCH"-none 2>/dev/null | sort | tail -1 || true)"
[[ -n "$PREFIX" && -x "$PREFIX/bin/python3" ]] || { echo "No uv-managed CPython 3.12 found in $UVPY. Run: uv python install 3.12" >&2; exit 1; }
if [[ -e /usr/local/bin/python3 && "$(readlink /usr/local/bin/python3 || true)" == "$PREFIX/bin/python3" ]]; then
  echo "already set: /usr/local/bin/python3 -> $PREFIX/bin/python3"; exit 0
fi
if [[ -e /usr/local/bin/python3 ]]; then echo "/usr/local/bin/python3 already exists and points elsewhere; not overwriting." >&2; exit 1; fi
sudo mkdir -p /usr/local/bin
sudo ln -s "$PREFIX/bin/python3" /usr/local/bin/python3
launchctl setenv PYTHON3HOME "$PREFIX"
echo "ok: /usr/local/bin/python3 -> $PREFIX/bin/python3"
