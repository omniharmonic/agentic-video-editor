#!/usr/bin/env bash
# Build FluidAudio's CLI (Apple Silicon, CoreML): Parakeet ASR with word timestamps + pyannote/VBx diarization.
# Output: vendor/FluidAudio/.build/release/fluidaudiocli. Models download on first use (no HF token needed).
# Pin FLUIDAUDIO_REF to change versions; default is the commit validated by this project.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/vendor/FluidAudio"
REF="${FLUIDAUDIO_REF:-20d4f0b}"
[[ "$(uname -s)-$(uname -m)" == "Darwin-arm64" ]] || { echo "FluidAudio needs macOS on Apple Silicon (CoreML). Elsewhere use whisper.cpp: scripts/fetch-whisper-model.sh" >&2; exit 1; }
command -v swift >/dev/null || { echo "swift not found. Install the Command Line Tools (xcode-select --install) and re-run." >&2; exit 1; }
if [[ ! -d "$SRC/.git" ]]; then
  git clone -q https://github.com/FluidInference/FluidAudio.git "$SRC"
fi
git -C "$SRC" fetch -q origin "$REF" 2>/dev/null || git -C "$SRC" fetch -q --unshallow origin 2>/dev/null || true
git -C "$SRC" checkout -q "$REF"
(cd "$SRC" && swift build -c release --product fluidaudiocli)
echo "built: $SRC/.build/release/fluidaudiocli"
