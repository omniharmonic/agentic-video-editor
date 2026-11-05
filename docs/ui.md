# `ave ui` is retired

```
ave ui <episode>
```

prints one line: `ave ui is retired: open AVE Studio (ave desktop, or ave studio ui <studio>) and use the Room and the Workspace.`

Everything the old episode page did now lives in AVE Studio:

| Old `ave ui` page | Where it is now |
|---|---|
| Dashboard | Workspace board and Home |
| Clips (the review desk): keep, maybe, skip, previews, transcript context | Room › Selects |
| Clip nudge (move a start or end by whole sentences) | Room › Selects, the select's detail ("Starts" and "Ends") |
| Review (timestamped notes on an edit) | Room › Cut, Script and Selects: Flag. `ave review next <ep>` reads the Room's open flags first, then `work/review/notes.jsonl` |
| Trim: confirm Claude's draft trim | Workspace › Approvals › Trims to confirm (native sheet, device-owner check and PIN) |
| Trim: set other times | `ave trim set` in the human's own terminal, or ask Claude |
| Trim: clear | Room › Cut, the Trim chip in the strip (native sheet, device-owner check) |
| Content: approve or reject | Workspace › Approvals › Content to review (approve in the native sheet; reject as the person) |
| Publish: approve an episode post | `ave publish approve <folder> <id>` in the human's own terminal (the sheet shows the exact line) |
| Publish: reject | Workspace › Approvals › Episode posts to approve |
| Jobs | Workspace › Jobs: run an allowlisted step, read its log, cancel it |

## What did not change

The loopback server rules moved with it (`src/ave/studio/ui/http.py`): 127.0.0.1 only, a per-run token on every request (`?t=`, then
an `HttpOnly; SameSite=Strict` cookie, or `X-AVE-Token`), Host and Origin checks, ranged file serving that refuses `..`, dot-files,
symlinks out of the project and the raw footage folders (whatever their case, and through symlinks).

Jobs are still an allowlist of `ave` subcommands with typed arguments (`src/ave/jobs.py`): no shell, no free-form strings, no file
paths, no `--allow-unapproved`/`--allow-unlicensed`, no environment overrides, nothing that publishes, approves or sends. A job runs
with `AVE_AGENT=1` (so human-only acts refuse) and never with `AVE_APPROVAL_KEY`. One job at a time per project. Cancel stops the step and every process it started (its process group: SIGTERM, then SIGKILL after 5 s); the row says Stopping until they are all gone, and no other step starts meanwhile.
