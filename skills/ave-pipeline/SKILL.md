---
name: ave-pipeline
description: Master workflow for editing a podcast or interview episode end to end with the ave CLI (assembly, rough cut, final cut, clips, shorts, publishing) and DaVinci Resolve for human review. Use when the user says to edit an episode, make a cut, process new footage, or asks what is next on an episode. Start here, then hand off to the stage skills.
---

# Episode editing pipeline

You are the editor; the human is the director. Read `AGENTS.md` once per session. This skill is the map; each stage has its own skill.

## 1. Orient (always, every session)

```bash
ave doctor                  # fix FAIL lines first (ave-setup-check)
ave resume <episode>        # what is done, what is next, what the human is reviewing, open questions, last log lines
```
Then read `<episode>/PROGRESS.md`. Log every step with `ave log`, every surprise with `ave learn` (skill `ave-session-protocol`; it also covers handing a project to another agent).

No `episode.json`? `ave init-episode <dir>` (safe on an existing folder: it only adds what is missing and picks the numbered `02_FOOTAGE`/`03_AUDIO` layout when `02_FOOTAGE` exists), then ask the human to put footage in the raw folder it prints (`raw/` for the flat layout). Paths below (`work/`, `edits/`) are flat-layout names; `episode.json` `"layout"` maps them. Episodes live wherever the human keeps editing folders; ask once, never assume.
`ave status <episode>` gives the same state as JSON. State comes from artifacts on disk, so any agent can resume where another stopped.

## 2. The three cuts, then everything downstream

| # | Stage | You run | Done when | Skill |
|---|---|---|---|---|
| 1 | ingest, audio | `ave ingest`, `ave audio` | `work/media.json`, `work/audio/` | `ave-ingest-transcribe` |
| 2 | transcript + speakers | `ave transcribe --speakers N` | `work/transcript.json` | `ave-ingest-transcribe` |
| 3 | sync cameras | `ave sync` | `work/sync.json` (skip for one camera) | `ave-multicam-sync` |
| 4 | map cameras to speakers | edit `episode.json` | cameras + speakers set | `ave-map-cameras` |
| 5 | plan cuts | `ave plan-cuts` | `work/cutlist.json` | `ave-rough-cut` |
| 5b | real start/end (checkpoint) | `ave trim suggest` then, once the human agrees, `ave trim set` | `episode.json` `trim` | `ave-assembly` |
| 6 | **assembly** | `ave assemble` | `edits/assembly.vN.json` | `ave-assembly` |
| 7 | **rough cut** | `ave cleanup` | `edits/rough.vN.json` | `ave-cleanup` |
| 7b | opening and closing (checkpoint: the human decides the show's grammar, then approves the plan) | `ave sequence plan`, `ave sequence preview`; the human runs `ave sequence approve` | `work/sequence.json` approved | `ave-intro-outro` |
| 8 | **final cut** | `ave final` (add `--opening --closing`, or policy `sequences:`, once 7b is approved) | `edits/final.vN.json`, render, SRT | `ave-final-cut` |
| 9 | selects | `ave clips ...` | `work/clips.json`, `edits/select.*` | `ave-clip-finder` |
| 10 | shorts | `ave shorts ...` | `exports/shorts/*.mp4` | `ave-shorts` |
| 11 | publish | `ave publish ...` | human-approved, sent | `ave-publish` |

At every cut stage: `ave handoff <episode>` puts it in Resolve for the human. Stages 1-8 run entirely in FFmpeg/Python; only the handoff needs Resolve's bridge.

**Several takes on one recorder?** `ave ingest` lists `sessions` (and warns when one audio file spans them); episode time is the recorder's timeline. Put `session` on each camera in `episode.json`, then `ave plan-cuts` and `ave assemble` produce one NAMED edit per session (`assembly.s1`...; `--combined` joins them), and `ave cleanup|final|render|handoff --name s1` carry each session through the later stages. Regions no camera covers are excluded. Sync measures and APPLIES per-camera clock drift. See `ave-ingest-transcribe`, `ave-multicam-sync`, `ave-map-cameras`, `ave-assembly`.

## 3. Checkpoints (stop and ask the human)

After transcript (speaker names), camera mapping (with the stills as evidence), **assembly**, **rough cut** (show `ave cleanup explain`: what was cut, what is flagged), **opening and closing** (before the final: does the show open with a cold open and a title card, which hosts and titles, which links, the editor credit; the human approves `ave sequence plan`), **final cut**,
clip selection (review page), and before anything is queued for publishing. Record what they are reviewing (`ave resume` shows it) and wait.

## 4. Single camera or audio-only

Skip `ave sync`. Still do the mapping step: put the one camera in `cameras` and map every speaker to it, then `ave plan-cuts` (it yields a single shot), `ave assemble`, `ave cleanup`, `ave final` as usual (verified on real footage). With no second angle, cleanup masks every cut with alternating 110-120% punch-ins; if that reads as too much, tune `masks` in the cleanup policy (`ave-cleanup`).

## 5. When something is off

Say what failed, show the command and the last lines of output, propose the fix. Do not paper over low-confidence results. Each stage skill lists its failure modes.
Revisions never overwrite: re-running a stage writes the next version, and the human's Resolve timelines are never touched.
