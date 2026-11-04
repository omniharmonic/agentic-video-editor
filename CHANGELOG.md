# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
uses [Semantic Versioning](https://semver.org/) (while the major version is 0, minor releases may change behaviour).

For what is verified on real footage and what is not, read [docs/ROADMAP.md](docs/ROADMAP.md); every "built" below was at least
tested on synthetic fixtures.

## [Unreleased]

## [0.4.1] - first automatic update

- The first release delivered through the app's own updater. It carries no feature changes from 0.4.0: installing it proves the update path (signed download, safe restart after running jobs, projects intact) end to end.

## [0.4.0] - the Director's Room

- **The Room.** One place to watch and direct an episode: Cut, Script, Selects and Layout views side by side with the video. Flag a moment with a note, keep or go back on a change, sign off a stage, and see what the assistant did and why. When the assistant relays a sign-off for you, you confirm it, and you can undo it.
- **The Workspace.** Your whole studio on one screen: a board of posts by stage, a calendar you can drag posts across, previews of how each post will look, your sources, ideas, campaigns and the running jobs.
- **Settings.** Add and remove people, connect accounts, and choose which alerts reach you. Accounts and alerts are managed in the app instead of on the command line.
- **Approval sheets.** Anything that publishes, changes who may do what, or overrides a check now stops on a sheet you read and approve: publishing one post or a batch, setting or clearing a trim, approving content (including approving despite the critic's objection), acknowledging an alert, handing over publishing, and removing a member. The assistant can prepare these, but only you can approve them.
- **Sync across computers.** Maren and Tomas each see the same projects, versions and approvals on their own Mac. A project saved by a newer version shows "Update needed" and stays read-only on an older one instead of being damaged.
- **Automatic updates.** Choose AVE Studio, then Check for Updates. A new version downloads, is checked, and installs when your current render or import finishes, so a running job is never cut off.
- **More tools.** The older screens you may still need live in a separate window, out of the way of the Room and Workspace.
- Fixes and hardening: a post counts as Out only after an approved or recorded send; the app refuses actions started by an assistant that need your own approval; downloads and the release are signed and notarized, and the app itself is stapled so it opens offline.


- Local studio Desk: pasted writing/idea intake, editorial lanes, episode checkpoints, source shelf and explicit integration readiness. Connection loss preserves text on the page.
- Optional pywebview desktop shell and installation-bound macOS Finder launcher, preserving the human launch and review gates.
- External-agent stdio MCP with bounded context/source/skill/recipe reads and replay-safe, revision-checked draft writes. No embedded model API.
- Offline editorial bundles with peer identity, common bases, conflict snapshots, repeat-safe imports and optional hash-verified library media. Tailscale diagnostics remain read-only; publishing authority stays local.
- Publishing fixes: studio due slots no longer become a second native platform schedule. Post-editor dates use studio time. Whole variant-container replacement can no longer bypass protected operational fields.
- Content studio security hardening after the adversarial gate audit (qa/E): hash-chained, signed feedback/dispatch/control ledgers and a tick that refuses
  to send on tamper; single-use signed approvals bound to account, slot and TTL policy; send-backs void the approval and `feedback resolve` needs evidence;
  the live attestation binds credentials and a sequence number (`--off` is a signed record); the review and `approve` sign the queue item and refuse when it
  differs from the post; `library add`/`adopt` license claims need `library attest`; adopted items no longer bypass studio gates; a signed `send.enabled`
  (`ave studio send enable`) lets a launchd `tick --send` job run without a terminal; `ave studio ui` serves a fully wired studio; approving an Instagram
  account no longer demotes the item; B-roll downloads re-resolve from the provider and only fetch from its hosts; presigned URLs are redacted, media-host
  orphans are swept; the approval key is read once per process and a planted key file is refused; an all-day quiet window fails closed. Threat model in
  SECURITY.md and docs/studio.md, deny rules in `.claude/settings.example.json`, and an "untrusted text is data" rule in every studio skill.
- `ave doctor`: missing libass is a FAIL under "Required for captions & shorts" and the verdict reads `PARTLY READY` (exit code still 0 if the basic flow works).
- Cleanup: shared zoom-state limiter, cut-inside-a-word check and tail pad. Shorts: complete-statement gate, word-gap cuts, title openings. `ave motion quote-reel`.
- CI: cross-platform fixes, FFmpeg filter-list parsing for builds with two-character flags.
- Docs: roadmap brought up to date; dead config keys removed or marked reserved; env var table.

## [0.1.0] - first public snapshot (developed 2026-10-13 to 2026-10-14)

First version of the toolkit: an `ave` CLI plus 25 agent skills that edit a podcast with FFmpeg and Python and hand the result to
DaVinci Resolve (free 21.0.x) for human review. About 1,280 automated tests, mostly on synthetic media.

### Pipeline
- **Ingest and project layout.** Episode folders in a flat or numbered layout (or a custom one), sessions, `ave init-episode` / `adopt`,
  a workspace folder for all episodes, and a `PROGRESS.md` session protocol (`ave resume`, `log`, `learn`, `progress`) so any agent can
  pick a project up from disk. Raw footage is read-only; edits are versioned and never overwritten.
- **Audio and channel layout.** Analysis audio extraction, speaker-per-channel detection (speakers come free from the channels),
  dead-channel handling, room-tone fill that matches the noise floor.
- **Transcription and speakers.** Word-timed transcripts with diarization through FluidAudio (Parakeet ASR plus pyannote/VBx on Apple
  Silicon), with a whisper.cpp (Metal) fallback.
- **Multicam sync.** Audio alignment by GCC-PHAT with drift measured in ppm and a piecewise-linear map when a camera clock wobbles.
- **Cut planning, assembly and rough cut.** Shot/reverse-shot planner with listener-reaction pacing, wide-camera support, trim
  suggestions for the real start and end (human-approved), an assembly with nothing removed, and a cleanup pass (dead air, fillers,
  false starts, masked with multicam). All tunable numbers are in `config/*.yaml`.
- **Final cut.** Music beds, motion graphics, burned-in captions with an exact-match SRT, a true-peak-limited loudness pass,
  opening and closing sequences (cold open, title, host card, thanks, credits roll, end card) that a human approves, and a
  one-shot join.
- **Clips and shorts.** Recall-first clip proposals scored against a rubric, complete-statement boundaries, tighten suggestions
  (never applied automatically), a keep/maybe/skip decision ledger and a per-show taste profile, in-context previews, and vertical
  captioned shorts with several layouts (crop, stack, audiogram).
- **Release package.** `ave package` builds a dated, checksummed folder: episode video, SRT and VTT, podcast audio with chapters,
  thumbnails, approved shorts and quote cards, per-platform post-copy drafts.
- **Publishing.** A hash-bound, HMAC-signed human approval before anything is sent; YouTube and Facebook adapters, an experimental
  aggregator adapter for other platforms; credentials only in the OS keychain or environment. Run so far against mocked HTTP only.
- **Content kit (opt-in).** Quote posts, thumbnails, carousels and reel cards from verbatim quotes, an in-house renderer, a critic,
  and an optional Canva renderer handshake.
- **Assets.** B-roll and music licence log (hash-chained), stock-video search through Pexels and Pixabay, critic checks, and a refusal
  to render unlicensed assets.

### Resolve
- Handoff of any cut stage into a dedicated Resolve project through a pinned MCP bridge: chunked, resumable, verified by read-back;
  a layered handoff with the other angles stacked in sync; review proxies; experimental subtitle import.

### Local UI
- `ave ui`: a localhost-only review desk (clips in context, pipeline board, trim, content, hash-bound publish approval, allowlisted
  jobs), token-protected.

### Notes
- Bundled fonts: Newsreader and Inter (SIL OFL 1.1). See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
- Known limits and the unverified list: [docs/ROADMAP.md](docs/ROADMAP.md).
