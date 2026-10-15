#!/usr/bin/env bash
# Set up agentic-video-editor on this machine. Safe to re-run: every step checks before it acts.
#
#   ./scripts/setup.sh --minimal     Python env + FFmpeg check only. Enough for `ave demo`, the tests and every FFmpeg-only stage.
#   ./scripts/setup.sh               Everything this machine can do: extras, Chromium, Resolve MCP server, FluidAudio (Apple Silicon),
#                                    a Whisper model when there is no FluidAudio.
#
# Options: --minimal  --skip-playwright  --skip-resolve  --skip-fluidaudio  --with-whisper  --skip-whisper  -h|--help
#
# It never installs system packages and never uses sudo: when something is missing it prints the exact command for you to run.
# It writes only inside this repo (.venv/, vendor/, .mcp.json) plus (optionally) the Whisper model in ~/whisper-models.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

MINIMAL=0; SKIP_PW=0; SKIP_RESOLVE=0; SKIP_FA=0; WITH_WHISPER=0; SKIP_WHISPER=0
for arg in "$@"; do
  case "$arg" in
    --minimal) MINIMAL=1 ;;
    --skip-playwright) SKIP_PW=1 ;;
    --skip-resolve) SKIP_RESOLVE=1 ;;
    --skip-fluidaudio) SKIP_FA=1 ;;
    --with-whisper) WITH_WHISPER=1 ;;
    --skip-whisper) SKIP_WHISPER=1 ;;
    -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done
if [[ $MINIMAL -eq 1 ]]; then SKIP_PW=1; SKIP_RESOLVE=1; SKIP_FA=1; SKIP_WHISPER=1; fi

OS="$(uname -s)"; ARCH="$(uname -m)"
IS_MAC=0; APPLE_SILICON=0
[[ "$OS" == "Darwin" ]] && IS_MAC=1
[[ "$OS" == "Darwin" && "$ARCH" == "arm64" ]] && APPLE_SILICON=1

DONE=(); SKIPPED=(); FAILED=()
say()  { printf '\n== %s\n' "$*"; }
ok()   { DONE+=("$1"); }
skip() { SKIPPED+=("$1"); printf '   skipped: %s\n' "$1"; }
fail() { FAILED+=("$1"); printf '   FAILED: %s\n' "$1" >&2; }

ffmpeg_hint() {
  if [[ $IS_MAC -eq 1 ]]; then echo "brew install ffmpeg-full      (keg-only; has libass for burned-in captions. No Homebrew? https://brew.sh)"
  elif command -v apt-get >/dev/null; then echo "sudo apt-get install ffmpeg"
  elif command -v dnf >/dev/null; then echo "sudo dnf install ffmpeg      (needs RPM Fusion on Fedora)"
  elif command -v pacman >/dev/null; then echo "sudo pacman -S ffmpeg"
  else echo "install FFmpeg from https://ffmpeg.org/download.html and put ffmpeg + ffprobe on PATH"; fi
}

say "Checking prerequisites ($OS $ARCH)"
MISSING=0
if ! command -v uv >/dev/null; then
  echo "  missing: uv (Python package manager)"
  echo "    install: curl -LsSf https://astral.sh/uv/install.sh | sh     or     brew install uv"
  MISSING=1
fi
for tool in ffmpeg ffprobe; do
  if ! command -v "$tool" >/dev/null; then
    echo "  missing: $tool"; echo "    install: $(ffmpeg_hint)"; MISSING=1
  fi
done
if [[ $MINIMAL -eq 0 ]] && ! command -v git >/dev/null; then
  echo "  missing: git (needed to fetch the Resolve MCP server and FluidAudio)"
  if [[ $IS_MAC -eq 1 ]]; then echo "    install: xcode-select --install"; else echo "    install: your package manager's git package"; fi
  MISSING=1
fi
if [[ $MISSING -eq 1 ]]; then echo; echo "Install the missing items above, then re-run: ./scripts/setup.sh $*" >&2; exit 1; fi
echo "  ok: uv, ffmpeg, ffprobe$([[ $MINIMAL -eq 0 ]] && echo ', git')"

say "Python environment (uv)"
if [[ $MINIMAL -eq 1 ]]; then
  uv sync
else
  uv sync --all-extras
fi
ok "python environment (.venv)"

if [[ $SKIP_PW -eq 0 ]]; then
  say "Chromium for motion graphics (Playwright, about 150 MB, skipped if already installed)"
  if .venv/bin/python -m playwright install chromium; then ok "chromium"; else fail "chromium download (motion graphics will not render; retry: .venv/bin/python -m playwright install chromium)"; fi
else
  skip "Chromium / motion graphics"
fi

MCP_REF="$(grep -E '^RESOLVE_MCP_REF' src/ave/config.py | cut -d'"' -f2)"
if [[ $SKIP_RESOLVE -eq 1 ]]; then
  skip "Resolve MCP server"
elif [[ $IS_MAC -eq 0 ]]; then
  skip "Resolve MCP server (the Resolve handoff is set up and tested on macOS only)"
else
  say "Resolve MCP server (pinned $MCP_REF)"
  mkdir -p vendor
  if [[ -n "${AVE_RESOLVE_MCP:-}" && -d "${AVE_RESOLVE_MCP}" ]]; then
    echo "  using AVE_RESOLVE_MCP=$AVE_RESOLVE_MCP (no clone)"; ok "Resolve MCP server (AVE_RESOLVE_MCP)"
  else
    (
      set -e
      [[ -d vendor/davinci-resolve-mcp/.git ]] || git clone -q https://github.com/samuelgursky/davinci-resolve-mcp.git vendor/davinci-resolve-mcp
      git -C vendor/davinci-resolve-mcp checkout -q "$MCP_REF"
      if [[ ! -x vendor/davinci-resolve-mcp/venv/bin/python ]]; then
        (cd vendor/davinci-resolve-mcp && uv venv -q venv --python 3.12 && uv pip install -q --python venv/bin/python -r requirements.txt)
      fi
    ) && ok "Resolve MCP server" || fail "Resolve MCP server (needs network + git; retry: ./scripts/setup.sh)"
  fi
fi

FLUID_READY=0
if [[ -x vendor/FluidAudio/.build/release/fluidaudiocli ]]; then FLUID_READY=1; fi
if [[ $SKIP_FA -eq 1 ]]; then
  skip "FluidAudio (fast transcription + speaker labels)"
elif [[ $APPLE_SILICON -eq 0 ]]; then
  skip "FluidAudio (needs macOS on Apple Silicon; whisper.cpp is the transcription engine here)"
elif [[ $FLUID_READY -eq 1 ]]; then
  say "FluidAudio"; echo "  already built"; ok "FluidAudio (already built)"
elif ! command -v swift >/dev/null; then
  skip "FluidAudio: Swift not found. Run: xcode-select --install   then re-run ./scripts/setup.sh  (or use Whisper below)"
else
  say "FluidAudio (Swift build, a few minutes the first time)"
  if ./scripts/build-fluidaudio.sh; then FLUID_READY=1; ok "FluidAudio"; else fail "FluidAudio build (see output above; Whisper still works: ./scripts/fetch-whisper-model.sh)"; fi
fi

if [[ $SKIP_WHISPER -eq 1 ]]; then
  [[ $MINIMAL -eq 1 ]] || skip "Whisper model"
elif [[ $FLUID_READY -eq 1 && $WITH_WHISPER -eq 0 ]]; then
  skip "Whisper model (FluidAudio is the default engine; add --with-whisper for the fallback too)"
else
  say "Whisper fallback model (about 1.6 GB, resumable)"
  if ! command -v whisper-cli >/dev/null; then
    if [[ $IS_MAC -eq 1 ]]; then echo "  note: whisper-cli is not installed. Install it with: brew install whisper-cpp"
    else echo "  note: whisper-cli is not installed. Build whisper.cpp: https://github.com/ggml-org/whisper.cpp (put whisper-cli on PATH)"; fi
  fi
  if ./scripts/fetch-whisper-model.sh; then ok "Whisper model"; else fail "Whisper model download (retry: ./scripts/fetch-whisper-model.sh)"; fi
fi

if [[ $SKIP_RESOLVE -eq 0 && $IS_MAC -eq 1 ]] && [[ -d vendor/davinci-resolve-mcp || -n "${AVE_RESOLVE_MCP:-}" ]]; then
  say "MCP config for agents (.mcp.json)"
  if .venv/bin/ave mcp-config --write; then ok ".mcp.json"; else fail ".mcp.json"; fi
fi

say "Checking the install"
.venv/bin/ave doctor || true

say "Summary"
for x in ${DONE[@]+"${DONE[@]}"}; do echo "  done:    $x"; done
for x in ${SKIPPED[@]+"${SKIPPED[@]}"}; do echo "  skipped: $x"; done
for x in ${FAILED[@]+"${FAILED[@]}"}; do echo "  FAILED:  $x"; done

cat <<MSG

Try it now (no footage, about a minute; needs only FFmpeg):
  .venv/bin/ave demo

Then your own episode:
  .venv/bin/ave init-episode ~/Editing/ep-001      # drop footage into its raw/ folder, then follow docs/getting-started.md
MSG

if [[ $MINIMAL -eq 0 && $IS_MAC -eq 1 ]]; then
cat <<'MSG'

Human-only steps for the DaVinci Resolve handoff (skip them if you do not use Resolve; everything up to the final cut works without it):
  1. Install DaVinci Resolve 21.0.x FREE from https://www.blackmagicdesign.com
     (free 21.1+ removed Python scripting; Studio of any version works)
  2. One time, needs your password:    scripts/persist-python3home.sh
  3. Install the bridge:               vendor/davinci-resolve-mcp/venv/bin/python vendor/davinci-resolve-mcp/scripts/install_resolve_bridge.py
     then relaunch Resolve.
  4. EVERY Resolve launch: open a project, then Workspace > Scripts > resolve_bridge, and leave it running.
  Check with:                          .venv/bin/ave doctor
MSG
fi
if [[ $MINIMAL -eq 1 ]]; then
  printf '\nMinimal install: transcription, Resolve handoff, motion graphics, music analysis and publishing were not set up. Re-run ./scripts/setup.sh (no flag) to add them.\n'
fi
[[ ${#FAILED[@]} -eq 0 ]] || exit 1
