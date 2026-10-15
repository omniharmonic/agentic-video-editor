#!/usr/bin/env bash
# Fetch the fallback ASR model for whisper.cpp (whisper-cli, Metal GPU). ~1.6 GB, resumable.
set -euo pipefail
DEST="${AVE_WHISPER_DIR:-$HOME/whisper-models}"
NAME="${1:-ggml-large-v3-turbo.bin}"
command -v curl >/dev/null || { echo "curl not found; install it, or download the model by hand into $DEST" >&2; exit 1; }
mkdir -p "$DEST"
echo "downloading $NAME (about 1.6 GB; re-run if interrupted, it resumes) ..."
if [[ -s "$DEST/$NAME" ]]; then echo "already present: $DEST/$NAME"; exit 0; fi
curl -L --fail -C - -o "$DEST/$NAME.partial" "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$NAME"
mv "$DEST/$NAME.partial" "$DEST/$NAME"
echo "ready: $DEST/$NAME"
