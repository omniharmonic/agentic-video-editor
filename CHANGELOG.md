# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
uses [Semantic Versioning](https://semver.org/) (while the major version is 0, minor releases may change behaviour).

For what is verified on real footage and what is not, read [docs/ROADMAP.md](docs/ROADMAP.md); every "built" below was at least
tested on synthetic fixtures.

## [Unreleased]

## [0.1.0] - first public snapshot (developed 2026-10-13 to 2026-10-14)

First version of the toolkit: an `ave` CLI plus 25 agent skills that edit a podcast with FFmpeg and Python and hand the result to
DaVinci Resolve (free 21.0.x) for human review. About 1,100 automated tests, mostly on synthetic media.

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
