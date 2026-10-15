# Recall-first clips, taste learning, and the local UI: design

> **Internal design history.** A working plan or spec written for the agents that built this part of the toolkit. It is kept as a record of intent and is not user documentation; the code, `AGENTS.md`, the skills and the reference docs are authoritative. Paths and names in it describe the first project. See [docs/README.md](../../README.md).

Date: 2026-10-14. Status: approved by the author in chat ("approval should be able to happen within terminal and in the UX", "keep building and iterating in parallel").

## Goals
1. `ave clips propose` returns EVERY clip-worthy section above a low floor (recall first), ranked, tagged and explained. The human decides.
2. Every human decision is logged with the clip's features and an optional reason, and fed back so the proposer's judgment improves per show.
3. `ave ui <episode>` serves a localhost-only interface over the same files and CLI (agents keep working without it).
4. Publishing approval works in the terminal AND the UI. The UI path has the same hash-bound checks as `ave publish approve` plus a typed confirmation.

## Shared contracts (do not change without updating both sides)

### Decision ledger: `<episode>/work/learning/clip_decisions.jsonl` (append-only, one JSON object per line)
```json
{"v":1,"ts":"2026-10-14T18:03:11Z","episode":"00","clip_id":"s1-c07","session":"s1","edit":"rough.s1.v2",
 "verdict":"keep|maybe|skip","reasons":["weak_ending","hook_late","off_topic","too_long","too_short","needs_context","great_but_long","other"],
 "note":"free text or null","by":"maren","source":"ui|cli",
 "features":{"score":61.5,"rubric":{"hook":0.7,"...":0.0},"duration_s":59.0,"tags":["story","funny"],"speaker_balance":0.62,
             "hook_pos_s":1.8,"ends_on_sentence":true,"text_sha":"..."}}
```
Latest line per `clip_id` wins; earlier lines are history. Reasons vocabulary lives in `config/clip_reasons.yaml` (extensible).
Python API (module `ave.learning`): `record_decision(ep, clip_id, verdict, reasons=(), note=None, by, source)`, `latest_decisions(ep) -> dict[clip_id, dict]`, `iter_decisions(ep)`.
Clips also keep `status` in `clips.json` (`proposed|approved|rejected`); `keep` => `approved`, `skip` => `rejected`, `maybe` stays `proposed` with `maybe: true`. The existing `ave clips decide` is the single code path that writes both.

### Taste profile: `<show-config>/taste/clips.yaml` (human-readable, versioned in git, per show)
Holds `weights` (rubric weight overrides), `exemplars` (keep/skip quotes with reasons, capped), `notes`. Only a human-approved `ave learn taste apply` rewrites weights. Show config dir = where `config/` project overrides already resolve (follow `ave.config`).

### UI server: `ave ui <episode> [--port 0] [--open]`
127.0.0.1 only, random per-run token required on every request (cookie/header), no CORS, no external network, stdlib or FastAPI-free (prefer `http.server`/small ASGI already in deps; justify any new dep). JSON API under `/api/...`, static pages under `/`. Commands that take time run as jobs (`/api/jobs`, log tail). The UI NEVER shells arbitrary strings: only an allowlist of `ave` subcommands with validated args.

### Publish approval from the UI
`POST /api/publish/<item>/approve` requires: the current file sha prefix typed by the human (>= 8 chars, same as `--confirm-sha`), approver name from a setting, and records `source: ui` in the approval. It calls the same `ave.publish.queue.approve`. `AVE_ALLOW_UNATTENDED_APPROVAL` stays unsupported: the UI is a human surface, agents must not call it (the token is shown only on the human's terminal and in the browser URL they open).

## Workstreams (parallel; each its own worktree and branch, merged by the orchestrator)
- **L** learning: recall-first proposer, ledger, taste profile, hit-rate report, `ave clips decide` verdict/reason flags, review page emits decisions JSON the CLI can import, `ave learn taste report|apply`.
- **U** ui: `ave ui` server + dashboard + clips page + trim page + content page + publish page + jobs, reading the ledger contract above (coordinate through the contract, not through L's code; use a fixture ledger in tests).
- **C** canva phase 2 (read-only discovery of Canva; NO create/edit/publish on Canva without approval), show-name duplication fix.
- **S** sync/compile roadmap: W3 piecewise-linear drift, W5 room tone in rough, W9 legacy build-timeline alias.
- **P** proxies + subtitles: W10 `ave proxies` and `handoff --use-proxies`, W7 SRT route or documented workaround.
- **D** docs closeout (orchestrator).

## Round 2 contracts (2026-10-14, parallel workstreams G1 gui, G2 previews, K clip quality, T content/brand, F1 final cut, F2 shorts, I sequences, P2 package)

### Clip fields added by K (in `work/clips.json`, per clip; all optional for readers)
- `rank` (1 = best within its session), `total` (score), so "top N" and "min score" are views over the same list.
- `complete_statement` (bool), `start_complete`, `end_complete` (bool: begins at the start of a sentence / thought, ends on a finished one), `completeness_note` (why, if not).
- `context_before` / `context_after`: the ~2 sentences of transcript immediately before/after the clip (text), so a reader knows what they are missing.
- `tighten`: list of `{start,end,kind: filler|tangent|repeat|restart|aside, text, saves_s, reason, confidence}` in EPISODE seconds inside the clip. Suggestions only, never applied silently. `tight_duration` = duration if all applied.
- K implemented (2026-10-14, additive, see `docs/field-notes/2026-10-14-clip-completeness.md`): also on each clip `needs_context` (bool) with `needs_context_reason` and `context_variant` `{sentence_ids,start,end,duration,added_text,note}` (the same clip started 1-2 sentences earlier; offered, never applied), `expanded` (`null`, or `{from,start_sentences,end_sentences,note}` when the agent's/window's ids were snapped OUT to complete thought edges; `sentence_ids` are the expanded ones), `tight_text`, and on each `tighten` entry `o_start`/`o_end` (seconds on the clip's own timeline, whereas `start`/`end` are episode seconds). `rank` is per session, 1 = best, over non-stale clips; `selection` in `clips.json` (top level, or per session under `sessions.<name>`) is informational and only present when a cut-off was asked for. The cut-off is a view: `list --top/--min-score` prints best-first rows with a `{"cut_line": "..."}` marker row and `in_view` per row.
- CLI: `ave clips propose --top N --min-score S` (also `prepare --floor`), config `recall.top_n`/`recall.min_score`; the UI filters by `rank`/`total` live.
### Clip previews (G2 writes, G1 reads): `work/clips/previews/<clip_id>.mp4` (540p h264 + aac, faststart) and `work/clips/previews/index.json`
`{clip_id: {file, tight_file|null, pad_before_s, pad_after_s, clip_in_s, clip_out_s, edit, rendered_at, sha}}`. `clip_in_s/out_s` are offsets INSIDE the preview where the real clip begins/ends, so the player shows lead-in and tail dimmed and can loop just the clip. The UI serves them with Range support. `ave clips previews <ep> --name s1 [--ids ...] [--top N] [--pad 8] [--force]`; resumable, skips fresh ones. Fallback for the UI when missing: ranged playback of the edit proxy.
### Sequences (I): `ave sequence build|preview` + `config/sequences.default.yaml` (opening: cold open, title card, credits roll; closing: credits, thanks, calls to action), referenced by `ave final` via `sequences:` in the final policy; a skill `ave-intro-outro`.
### Package (P2): `ave package <ep>` builds `<project>/06_EXPORTS/package/<date>/` with episode video, SRT/VTT, shorts, thumbnails, quote cards, carousels, per-platform post copy, a manifest and an `index.html`; everything is DRAFT, queued items need human approval.
