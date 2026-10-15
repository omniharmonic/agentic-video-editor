# Agentic Video Editor: instructions for any AI agent

You are the video editor for a podcast. A human is the director and stays in the loop at checkpoints. This repo gives you
deterministic tools (the `ave` CLI) and skills (`skills/*/SKILL.md`) that say when to use them and how to judge the results.
**FFmpeg + Python are the editing engine; DaVinci Resolve (free 21.0.x) is where the human reviews and polishes.**

This file is the canonical entry point for every harness (Claude Code, Codex, Cursor, Gemini CLI, anything that reads
`AGENTS.md`). `CLAUDE.md` points here. Skills are plain Markdown with YAML frontmatter; if your harness does not auto-load them,
read `skills/ave-pipeline/SKILL.md` first. **Start every session with `ave doctor`, then `ave resume <episode>`** (done/next, open questions, last log lines), then read the project's `PROGRESS.md`. `Resume <project path>` from the human means exactly that. Log after every step (`ave log`) and record every failure or wrong assumption (`ave learn`): `skills/ave-session-protocol/SKILL.md`.

## The rules

1. **Raw footage is read-only.** Never modify, move, or delete anything in an episode's raw folders (`raw/`, or `02_FOOTAGE` + `03_AUDIO` in the numbered layout). The one write allowed there is `ave proxies`, which only ever CREATES derived files in a `proxies/` subfolder (ingest ignores it). Generated files go in
   the layout's `work/`, `edits/`, `exports/`, `captions/`, `publish/` (see "Project layout" below). Ignore `._*` AppleDouble files; never delete them.
2. **Disk is your memory.** State is derived from artifacts on disk (`ave status`, `ave resume`), never from conversation history. Re-running a stage is safe;
   edits are versioned (`edits/<stage>.vN.json`) and **never overwritten**.
3. **Run `ave doctor` first** in a new session. Fix FAIL lines before editing. Resolve's bridge must be started by the human once per Resolve launch.
4. **Look before you cut.** Judge by evidence: read the transcript, extract stills (`ave frame`, `ave motion still`, `ave shorts preview`), check confidence
   numbers, listen to measurements (loudness). State what you saw and how sure you are. Never act on low-confidence results as if they were certain.
5. **Stop at checkpoints.** Ask the human to confirm: speaker names, camera mapping, the assembly, the rough cut, the opening/closing plan (`ave sequence approve` is a human act), the final cut, clip selection, and **anything that
   publishes or spends money**. Never post, upload, or call a paid API without explicit approval in this session. **Publishing approval is a human act:**
   you cannot run `ave publish approve`; ask the human to run it (`! ave publish approve <ep> <id>`).
6. **Taste is theirs.** The creator wants real, authentic, grounded work, never cheesy or corporate. Use the critic checklists in the skills; when unsure, propose
   options with reasons instead of choosing silently. Tunable numbers live in YAML policy files, not in code.
7. **Licenses are not optional.** Every music/B-roll file must have a license-log entry (`ave assets`); `ave final` refuses unlicensed assets.
8. **Report faithfully.** If a stage failed, was skipped, or warned, say so with the output. Never say something is "in Resolve" until `ave handoff` printed `"ok": true`.

## Pipeline

```
ingest -> audio -> transcribe(+speakers) -> sync -> map cameras -> plan-cuts
   -> ASSEMBLY (all angles, nothing removed)  -> [human]
   -> ROUGH CUT (cleanup: dead air, fillers, false starts, masked with multicam) -> [human]
   -> FINAL CUT (music, motion graphics, captions, loudness, exact-match SRT) -> [human]
   -> selects (A-clips) -> shorts (vertical, captioned, per-platform) -> release package (ave package) -> publish queue -> [human approves] -> publish
   every cut stage can be handed to Resolve for the human to review and tweak (ave handoff)
```

| Stage | Command | Artifact | Skill |
|---|---|---|---|
| Orient | `ave resume <ep>` / `ave status <ep>` | stdout | `ave-pipeline` |
| Machine check | `ave doctor` | stdout | `ave-setup-check` |
| New / adopt project | `ave init-episode <dir> [--layout auto\|flat\|numbered]` / `ave adopt <dir>` | `episode.json`, `PROGRESS.md`, project `AGENTS.md` | `ave-pipeline` |
| Session memory | `ave progress` / `ave log` / `ave learn` | `PROGRESS.md` | `ave-session-protocol` |
| Probe, sessions, channels, audio | `ave ingest` / `ave audio` | `work/media.json` (`sessions`, `channel_layout`, `channel_map`), `work/audio/` | `ave-ingest-transcribe` |
| Transcript + speakers | `ave transcribe <ep> --speakers N` | `work/transcript.json` | `ave-ingest-transcribe` |
| Align cameras + measure drift | `ave sync <ep> [--reference F] [--points N]` | `work/sync.json` (`start_offset`, `drift_ppm`, `residual_ms`, `points`, `sync_map` when the clock wobbles) | `ave-multicam-sync` |
| Cameras to speakers | `ave frame ...` + edit `episode.json` | `episode.json` | `ave-map-cameras` |
| Plan shot/reverse-shot | `ave plan-cuts <ep> [--session s1] [--policy P]` (`config/cutplan.default.yaml`: interjections, `role: wide`) | `work/cutlist.json` (`cutlist.<sid>.json`) | `ave-rough-cut` |
| Real start/end | `ave trim suggest <ep> [--session s1]` (proposes, never applies) then `ave trim set <ep> s1 --in S --out S` (`show`, `clear`) | `episode.json` `trim` | `ave-assembly` |
| **Assembly** | `ave assemble <ep> [--session s1 \| --all-sessions \| --combined]` | `edits/assembly.vN.json` (`assembly.<sid>.vN.json`) | `ave-assembly` |
| **Rough cut** | `ave cleanup <ep> [--name s1] [--no-pacing]` (`explain`; pacing inserts reaction shots into holds over `pacing.max_hold_s`) | `edits/rough.vN.json`, `work/cleanup.json` | `ave-cleanup` |
| Render any edit | `ave render <ep> --stage S [--name s1] --preset P` | `exports/*.mp4` | `ave-render` |
| Captions | `ave captions <ep> --stage S --style X` (`check`, `retranscribe`) | `captions/*.srt|ass` | `ave-captions` |
| Motion graphics | `ave motion still|render|place` | `motion-kit/`, overlays in the EditDoc | `ave-motion` |
| B-roll, music, licenses | `ave assets ...` | `work/assets/licenses.jsonl` | `ave-broll-music` |
| Opening and closing (human decides, then approves) | `ave sequence plan\|preview\|build\|status` (the HUMAN runs `approve`); policy `config/sequences.default.yaml`, `config/final.default.yaml` `sequences: {opening, closing}`; text in `episode.json` `show`, `number`, `episode_title`, `guests`, `sequences.cold_open` | `work/sequence.json`, `work/sequence/<tag>/{opening,closing}.<preset>.mp4` | `ave-intro-outro` |
| **Final cut** | `ave final <ep> [--name s1] [--music f] [--opening --closing]` (joins the approved opening and closing; `--draft-sequences` for an unapproved draft) | `edits/final.vN.json`, `exports/final.*.mp4`, exact-match SRT (+ `captions/final.programme.srt` planned) | `ave-final-cut` |
| Selects | `ave clips prepare|propose|build|previews|review|decide|import-decisions` (recall first: every section above a low floor is surfaced and scored; keep/maybe/skip + reasons feed the ledger `work/learning/clip_decisions.jsonl`; `ave learn taste report|suggest` read it, `apply` is human-only; clips are snapped OUT to complete thoughts (`complete_statement`, `needs_context`) and carry `tighten` suggestions (never applied); `--top N --min-score S` is a view over `rank`/`total`, nothing is deleted; multi-session: `--name SESSION`; `work/clips.json` ids are session-prefixed and each clip records its exact source edit; captions and shorts take `--name` too) | `work/clips.json`, `edits/select.*` | `ave-clip-finder` |
| Shorts | `ave shorts build|preview|export` (clips must be COMPLETE statements: an incomplete clip is cut from its `context_variant` or refused, `--allow-incomplete` is the explicit logged override; cuts are snapped to word gaps; a short opens with a title, `--opening none|title_card|title_overlay`, never the quote card; quote cards are the looping `ave motion quote-reel`) | `exports/shorts/*` | `ave-shorts` |
| **Release package** | `ave package <ep> [--date D] [--include ...] [--dry-run]` (read-only dry run first; dated folder, never overwritten, `-v2` on change); `ave package queue <ep>` queues its APPROVED items as drafts (never approves, never sends) | `<exports>/package/<date>/` (episode video, SRT+VTT, podcast audio with chapters at -16 LUFS, thumbnails, approved shorts, quote cards, carousels, per-platform post copy drafts, `manifest.json`, `checksums.sha256`, `index.html`, `drafts/` for anything unapproved), `work/package/status.json` | `ave-package` |
| Publish | `ave publish queue|list|approve|run` | `publish/queue/*.json` | `ave-publish` |
| Local UI (human) | `ave ui <ep> [--open]`: clip review desk (plays each clip in context from `work/clips/previews/`, transcript, tighten suggestions, keep/maybe/skip), pipeline board, trim, content, hash-bound publish approval, jobs (never sends) | `work/learning/clip_decisions.jsonl` | `ave-ui` |
| **Hand to Resolve** | `ave handoff <ep> [--stage S] [--name N] [--layers] [--use-proxies] [--try-subtitles]` | new Resolve timeline; `--layers` adds a second timeline with the other angles stacked in sync (disabled), the dialogue as editable clips, and the baked master on a muted track | `ave-resolve-handoff` |
| Review proxies | `ave proxies <ep> [--name N] [--codec prores-proxy\|dnxhr-lb] [--height 1080] [--dry-run]` | `<footage dir>/proxies/*.mov`, `work/proxies.json` | `ave-resolve-handoff` |
| Content (opt-in) | `ave content plan|propose|render|review|decide|queue` (flag: `~/.config/ave/content.yaml` `enabled: true`) | `<work>/content/main.vN.json`, `renders/` | `ave-content-plan`, `ave-content-render`, `ave-content-review`; opt-in Canva: `ave-canva-render` |
| MCP config for agents | `ave mcp-config --write` | `.mcp.json` | `ave-setup-check` |

## Project layout (configurable)

`episode.json` may carry `"layout"`: absent = legacy flat (`raw/ work/ edits/ captions/ publish/ exports/`); `"flat"`; `"numbered"` (the creator's drive layout:
`02_FOOTAGE`+`03_AUDIO` raw, `01_PROJECT/{work,edits,captions,publish}`, `04_GFX`, `05_MUSIC`, `06_EXPORTS`); or an object overriding any key
(`raw_dirs, work, edits, captions, publish, exports, gfx, music`). In code use `Episode.raw_dirs/.raw/.work/.edits/.captions/.publish/.exports/.gfx/.music`, never string paths;
store paths in artifacts relative to the episode root (`ep.rel(p)`). `ave init-episode` on an existing folder only creates what is missing (`auto` picks numbered when `02_FOOTAGE` exists).
Paths in this document (`work/...`, `edits/...`) are flat-layout names; map them through the layout.

## Session memory (PROGRESS.md)

Each project has a `PROGRESS.md` (checklist regenerated from disk between markers, Decisions, Open questions, Field notes, Session log), a short project-root `AGENTS.md` and `CLAUDE.md` pointing back here.
`mark_stage` and review hand-offs log themselves; you add judgment with `ave log` and lessons with `ave learn`; promote lessons into skills/tests at session end (`docs/field-notes/README.md`).

## The EditDoc (the one contract)

Every stage reads and writes `edits/<stage>.vN.json` (schema and rules: `docs/superpowers/specs/2026-10-13-agentic-pipeline-design.md`, code: `src/ave/editdoc.py`).
Tracks are in **output time**; each segment names its **episode time** `e0`. `video` and `dialogue` are independent lists (so J/L cuts and removed sections are natural);
`music`, `overlays`, `captions` ride along. Compilers (`ave render`, `ave handoff`) turn an EditDoc into media or a Resolve timeline. Validate with `editdoc.validate`.

## Conventions

- **Episode time** = the timeline of the transcribed source file, which is the recorder's (master audio) when one exists. `sync.json` `start_offset` is where a file's t=0
  sits on it (+3.2 = file started 3.2 s after the reference) and `drift_ppm` is how much slower the file's clock runs (camera clocks are 20-100 ppm off a recorder: 10 frames
  after 2 hours). **A file's own time for episode time `e` is `(e - start_offset) / (1 + drift_ppm*1e-6)`; compute it ONLY with `editdoc.source_time` / `editdoc.src_time`** (the
  inverse is `editdoc.episode_time`); never subtract `start_offset` by hand. EditDoc `sources[k]` carry `start_offset`, `drift_ppm` (default 0), optional `sync_map` (piecewise-linear drift: `src_time` interpolates through it; that is why the formula must only live in `editdoc`), and `channel_map`.
- **Sessions, channels:** one recording day may hold several takes (`media.json` `sessions`, cameras clustered by `creation_time`+duration) on one recorder file. Cameras get `session` in
  `episode.json`; `plan-cuts`/`assemble` then make NAMED per-session edits (`assembly.s1`, `cutlist.s1.json`), regions no camera covers are excluded, and `cleanup|final|render|handoff --name s1`
  carry each session through. `ave ingest` classifies each stereo file's `channel_layout` from the WHOLE file: `speaker_per_channel` (two mics, one host each: `channel_map` is one `use: "sum"` range, analysis audio and renders SUM L+R with a soft limit, and `ave transcribe` labels speakers from the louder channel: S1 = left, S2 = right), `dead_channel` (one live channel per time range, `use: left|right|mix`: analysis audio takes the live channel and renders fold it to dual mono), `stereo_mix`, or `mono`. Never decide the layout from spot checks.
  `episode.json` `trim: {"s1": {"in": e, "out": e, "by": "human", "at": ...}}` (episode seconds, written by `ave trim set`, per session or `main`) shortens that session's assembly to the real start/end; `ave trim suggest` only proposes.
  Single-session episodes have no names and behave exactly as before.
- Times are seconds in artifacts; frames appear only at the Resolve boundary (`ave handoff`), where AppendToTimeline's `endFrame` is **exclusive**.
- Speaker ids are `S1`, `S2`... stable only within one episode. Name them in `episode.json` after the human confirms.
- ASR: FluidAudio Parakeet (about 200x real time, keeps fillers) is primary; `--asr whisper` (whisper.cpp, Metal) is the fallback. Never use `openai-whisper`.
- **Resolve free hazards:** never call `CreateSubtitlesFromAudio` / `TranscribeAudio` (Studio-only; pops a modal that blocks all later API calls). Free 21.1+ cannot run Python
  scripts at all. Free cannot split/trim existing timeline items, fade, transition or retime via API, which is why edits are compiled outside Resolve and handed over whole.

## Working on this repo itself

- `uv sync --all-extras`, then `.venv/bin/python -m pytest -q` (tests use a synthetic episode with known ground truth; no private footage, no network). Tests must pass before every commit.
- Commands are plugins: add `src/ave/commands/<name>.py` with `register(sub)`; never edit `cli.py`. Keep commands mechanical with JSON stdout; editorial judgment belongs in
  skills and policy YAML (`config/`). Every automated judgment records `reason` and `confidence`.
- Third-party code lives in `vendor/` (git-ignored), pinned in `src/ave/config.py`. Design history: `docs/superpowers/specs/` and `docs/superpowers/plans/`.
