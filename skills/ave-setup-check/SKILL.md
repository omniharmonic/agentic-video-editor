---
name: ave-setup-check
description: Verify and repair the machine for agentic video editing, including DaVinci Resolve free-edition scripting, the Resolve MCP bridge, FluidAudio and Whisper. Use at the start of a session, when ave doctor reports FAIL or WARN, when Resolve tools say not connected, or after a reboot or Resolve update.
---

# Setup check

```bash
ave doctor          # human-readable; add --json for machine use
```

Exit code is non-zero only for REQUIRED failures. Fix in this order.

## Resolve bridge is not listening (the common one)

DaVinci Resolve **free** blocks external scripting, so the MCP reaches it through a small script that runs
*inside* Resolve. **The human must start it once every time Resolve launches:**

> In Resolve, open any project, then **Workspace > Scripts > resolve_bridge**. It looks like it hangs; that is
> the listener running. Leave Resolve open and do not run it twice.

Then `ave doctor` should show `Resolve bridge: listening on 127.0.0.1:49632`. You cannot click this for them.

## Scripts menu is empty or lacks resolve_bridge

1. Resolve must be **21.0.x free** (or any Studio). Free **21.1 and later removed Python scripting** (repo issue #203); there is no workaround except Studio or staying on 21.0.x. Check: `ave doctor` prints the version.
2. Resolve finds Python only via `PYTHON3HOME` or `/usr/local/bin/python3`. After a reboot the temporary setting is gone. The permanent fix (needs `sudo`, ask the user to run it with `!`):
   `scripts/persist-python3home.sh`
3. Scripts must be in the folder **this** Resolve build reads: `~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/`. The Mac App Store build uses a sandbox container instead; a leftover container from an old install makes the installer pick the wrong place. The repo installer only targets the user folder once `Fusion/Scripts/` exists.
4. Reinstall the bridge: `cd vendor/davinci-resolve-mcp && ./venv/bin/python scripts/install_resolve_bridge.py`, then quit and relaunch Resolve.

## Transcription engines

- FluidAudio missing: `scripts/build-fluidaudio.sh` (about 2 minutes; models download on first use, no token).
- Whisper fallback model missing: `scripts/fetch-whisper-model.sh` (1.6 GB, resumable).

## Never

- Do not reinstall `openai-whisper`; it is deliberately absent (10x slower, CPU only).
- Do not delete Resolve databases, containers or `~/Movies/DaVinci Resolve` without the user confirming a backup. The user's project libraries live there.
