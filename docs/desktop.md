# AVE Studio on your desktop

The desktop window runs the same studio and review gates as the browser. Media and project state stay on your computer. External Claude, Codex or Hermes sessions do the creative work; AVE does not embed a model API.

## Install and launch

In a checkout:

```sh
uv sync --all-extras
.venv/bin/ave doctor
.venv/bin/ave workspace ~/Movies/MyShow
.venv/bin/ave studio init --name 'My Show' --tz America/Denver
.venv/bin/ave desktop --check
.venv/bin/ave desktop
```

An installed wheel supports `pip install 'agentic-video-editor[desktop]'`, then `ave desktop /path/to/studio`. FFmpeg and the editing tools are installed separately; `ave doctor` explains missing pieces. Native windows use pywebview's OS renderer; Linux may need GTK or Qt dependencies (see [pywebview installation](https://pywebview.flowrl.com/guide/installation.html)).

Run the last command yourself in a separate Terminal window. The terminal displays your approval PIN. Agents must not launch or operate a real approval UI. Closing the native window shuts down its loopback server. Review/approval does not send content; the separate signed sender gate still applies.

For development screenshots only, a source checkout supports `ave desktop --fixtures`. Sample data never approves or publishes real posts.

## Standalone app (macOS, Apple Silicon)

`AVE Studio.app` is a signed, notarized app with its own FFmpeg, speech models, browser and Resolve bridge, for macOS 14 or newer. Nothing else needs installing to run the studio. To let an agent work, run this once in Terminal:

```sh
"/Applications/AVE Studio.app/Contents/MacOS/AVE Studio" --install-cli
```

It adds `~/.local/bin/ave`, links the agent skills into `~/.claude/skills` and installs the Resolve bridge, leaving anything it did not create alone. Approvals still need a person in a real terminal or the app window.

Builds go through `scripts/build_desktop.py` then `scripts/sign_desktop.py`. Keep runtime inputs somewhere durable: anything under `/private/tmp` is purged by macOS.

Limits: Apple Silicon only. Music analysis, OpenCV face checks and direct publishing are source-install extras and are not in the app. Parakeet model licensing is unresolved (`packaging/macos/MODEL-TERMS.txt`), so share the app with named testers, not publicly.

## Finder launcher on macOS

```sh
.venv/bin/ave desktop /absolute/path/to/studio --build-launcher '/path/to/AVE Studio.app'
```

Open the generated app in Finder. It opens Terminal to preserve the human launch check and PIN, then opens the native studio window. It does not remove agent environment markers, manufacture a terminal, or install a background sender.

This is an **unsigned launcher tied to this Python installation**, not a standalone installer. Give Tomas the wheel or repository and have her install dependencies and build a launcher on her own machine. Do not share your launcher as a portable application, credentials, approval keys, or account attestations. Use the standalone app above to share with someone else.
