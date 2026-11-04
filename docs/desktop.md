# AVE Studio on your desktop

The desktop window opens AVE Studio: the Room, the Workspace and Settings, with the approval sheets that only the app has (a browser can look but never approve). Media and project state stay on your computer. External Claude, Codex or Hermes sessions do the creative work; AVE does not embed a model API.

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

Run the last command yourself in a separate Terminal window. The approval PIN shows only in the app's own sheets, after the macOS device-owner check. Agents must not launch or operate a real approval UI. The Workspace's **More tools** menu (and View › More Tools…) opens Production, Media tools, the Feed planner and the Post editor in a window of their own. Closing the native window shuts down its loopback server. Review/approval does not send content; the separate signed sender gate still applies.

For development screenshots only, a source checkout supports `ave desktop --fixtures`. Sample data never approves or publishes real posts.

## Standalone app (macOS, Apple Silicon)

`AVE Studio.app` is a signed, notarized app with its own FFmpeg, speech models, browser and Resolve bridge, for macOS 14 or newer. Nothing else needs installing to run the studio. To let an agent work, run this once in Terminal:

```sh
"/Applications/AVE Studio.app/Contents/MacOS/AVE Studio" --install-cli
```

It adds `~/.local/bin/ave`, links the agent skills into `~/.claude/skills` and installs the Resolve bridge, leaving anything it did not create alone. Approvals still need a person in a real terminal or the app window.

Builds go through `scripts/build_desktop.py` then `scripts/sign_desktop.py`. Keep runtime inputs somewhere durable: anything under `/private/tmp` is purged by macOS.

**Updates.** A release build updates itself with Sparkle 2: **AVE Studio › Check for Updates…** (it also checks once a day). Sparkle downloads the new version, checks its EdDSA signature and relaunches; the relaunch waits while a render, an export, a Jobs panel job, a sync transfer or an import is running. On the first launch of a new version the app refreshes the Resolve bridge, the `ave` shim and the skill links it installed before. Every project event records the AVE version that wrote it. When a project holds an event from a newer AVE Studio, it opens read-only and the Workspace shows "Update needed". The app refuses every write to it: log events, edit versions, project.json, episode.json, the publish queue and other synced files. Bump the version in `pyproject.toml` and `src/ave/__init__.py` for every release, because this check compares that version, not the build number. A development build has updates off and the menu item says so. Releases: `build_desktop.py --release --update-public-key FILE --update-feed-url-file FILE --build-number N`, then `sign_desktop.py`, then `scripts/publish_update.py --dmg … --version … --build N` (run `--dry-run` first). The update feed sits in a private bucket at an address nobody can guess. It is kept only in local config (`~/.config/ave/updates.json`) and the release's Info.plist, and is never logged.

Limits: Apple Silicon only. Music analysis, OpenCV face checks and direct publishing are source-install extras and are not in the app. Parakeet model licensing is unresolved (`packaging/macos/MODEL-TERMS.txt`), so share the app with named testers, not publicly.

## Finder launcher on macOS

```sh
.venv/bin/ave desktop /absolute/path/to/studio --build-launcher '/path/to/AVE Studio.app'
```

Open the generated app in Finder. It opens Terminal to preserve the human launch check, then opens the native studio window. It does not remove agent environment markers, manufacture a terminal, or install a background sender.

This is an **unsigned launcher tied to this Python installation**, not a standalone installer. Give Tomas the wheel or repository and have her install dependencies and build a launcher on her own machine. Do not share your launcher as a portable application, credentials, approval keys, or account attestations. Use the standalone app above to share with someone else.
