---
name: ave-ui
description: The local management UI (`ave ui`) for the human: a clip review desk that plays each clip in context (preview, transcript, tighten suggestions, keep/maybe/skip with reasons), a pipeline status board, trim approval, content gallery, hash-bound publish approval, allowlisted jobs. Use when the human wants to review clips or approve things in a browser, or asks what `ave ui` does. Agents do not run or operate it.
---

# ave ui (human surface)

`ave ui <episode> [--port 0] [--open]` starts a localhost-only server and prints a URL with a one-run token. The HUMAN runs it
(`! ave ui <episode> --open` works inside a session) and keeps the URL private.

## What you do
- Tell the human it exists when a checkpoint needs them: clips awaiting a decision, a trim suggestion, content drafts, publish approval.
  Everything also works from the CLI; the UI is optional.
- Make review easy: run `ave clips previews <ep> --name <session> --top 20` so the Clips page can play each clip with lead-in and tail
  (`work/clips/previews/`). Without previews it falls back to `ave render --stage <s> --proxy` or `ave clips review --drafts`, and the
  page itself can queue `ave clips previews` as a job. The human chooses how many to look at (best N, minimum score) in the page; you
  can pre-trim the list with `ave clips propose --top N --min-score S`.
- Fill the review's context fields when you propose clips (`context_before`, `context_after`, `start_complete`, `end_complete`,
  `completeness_note`, `tighten`): the desk shows them as completeness badges and strike-through suggestions. Tighten suggestions are
  proposals only; the UI never applies them.
- After the human decided in the UI, read the results from disk: `work/clips.json` statuses, `work/learning/clip_decisions.jsonl`
  (verdict, reasons, note, `source: ui`), `ave trim show`, `publish/queue/*.json`. Then `ave log` what you learned from the reasons.
  A nudge in the UI moves a clip boundary by whole sentences and returns the clip to `proposed`: its preview needs re-rendering.

## What you never do
- Never start the UI to click through approvals yourself, never read the token to drive its API, never call its endpoints.
  Publishing approval is a human act (AGENTS.md rule 5); the UI's approve needs the file hash prefix typed by the human.
- The UI cannot send or post. Sending is still `ave publish run <ep> <id> --yes`, only with explicit approval in this session.
- Do not add anything to the job allowlist that publishes, approves, spends money, takes a file path, or bypasses a check.

Details: `docs/ui.md`.
