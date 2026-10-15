# Field notes: the final cut on the real episode (first real episode, session 2)

> **Working note from the first real episode.** This is internal history kept as evidence: what we assumed, what a real recording showed, and what changed. It describes one project (a two-host, two-camera podcast), uses its real numbers, and was written while the tools were still changing, so commands and defaults may have moved on. For the current behaviour read the skill or doc a note points to. See [docs/README.md](../README.md).

Workstream F1. Input `rough.s2.v3` (1121.95 s, 255 video segments, 124 dialogue segments, 1920x1080 canvas, 4K HEVC sources).
Output (project `06_EXPORTS/`, nothing overwritten, footage untouched): `final.s2.v9.master.mp4` (883 MB), `final.s2.v9.srt` (exact-match, Parakeet),
`final.s2.v9.whisper.srt` (the first, worse retranscription, kept for the record). EditDocs `final.s2.v1..v9` (v1-v5 build-up, v6-v7 from a render that died, v8-v9 the good run).
No music: `05_MUSIC` is empty and there is no license log, so nothing was added (silence under the dialogue is the plan).
Machine load was ~300 (several agents) during the render, so timings below are NOT benchmarks.

## N1. `ave motion place` could not address a session edit  (status: applied)
- Same class as N16 of the first run. `place` loaded and saved only unnamed edits, so on a multi-session episode (`rough.s2`, `final.s2`) there was no way to put a lower third on a session's cut.
- Change: `motion.place(name=)`, `--name` on the CLI, `_pick_stage` honours it. Test `test_place_targets_a_named_session_edit` (fails before).

## N2. `ave final` dropped graphics placed after it  (status: applied)
- The flow in the skill is "place graphics, then final", but `ave motion place --stage final` needs a final to exist, and the next `ave final` re-cloned the rough cut, silently discarding the overlays.
- Change: `ave final --from-final` continues the latest `final.<name>` (overlays kept, captions regenerated, render and retranscription as usual). Isolated to `_base_stage()` plus one `if` in `build_final`, to ease merging with the sequences work.
  Test `test_from_final_keeps_overlays_placed_after_the_first_final`.
- Working order that now works: `ave final --name s2 --no-render` -> `ave motion place --name s2 --stage final ...` (xN) -> `ave final --name s2 --from-final --burn`.

## N3. One transient ffmpeg failure killed a 10-minute render  (status: applied)
- 255 segment encodes run in parallel; one exited 255 with EMPTY stderr and the whole render aborted (RenderError, nothing written). The same command ran fine alone seconds later (load was ~300 and RAM was tight).
- Change: each segment encode is retried (`compile.SEGMENT_ATTEMPTS = 3`, 2 s / 4 s back-off); a persistent failure still fails loudly. Test `test_a_transient_segment_failure_is_retried_not_fatal` (first call fails, then passes; always-failing still raises).
- Also seen: a failed `ave final` leaves its EditDoc versions behind (v6, v7). Harmless (versions never overwrite) but expect gaps in the numbering.

## N4. The whisper fallback produced a wrong "exact-match" SRT, silently  (status: applied: warning; engine choice is a setup issue)
- FluidAudio looked unbuilt because the run came from a git worktree (`vendor/` is git-ignored, so `config.fluidaudio_cli()` found nothing; `AVE_FLUIDAUDIO=<main repo>/vendor/FluidAudio/.build/release/fluidaudiocli` fixes it). `retranscribe` fell back to whisper.cpp and `ave final` never printed the warning (it only lived in the transcript JSON).
- Measured, same render, against the planned captions (transcript words mapped through the edit), 113-209 matched cues:

  | engine | cues | median start vs plan | p10 / p90 | word-sequence similarity | text |
  |---|---|---|---|---|---|
  | whisper.cpp large-v3-turbo (`-ml 1 -sow`) | 262 | -0.55 s | -1.36 / +0.42 s | 0.963 | lowercase, no punctuation, 5 s two-line cues broken mid-phrase |
  | FluidAudio Parakeet | 307 | +0.008 s | -0.11 / +0.13 s | 0.981 | cased and punctuated |

  Whisper word times in the opening were seconds early ("once" at 6.6 s, heard at ~9.5 s), so the sidecar would have been visibly wrong in any player.
- Change: `ave final` now carries `t["warnings"]` into the report, records the `engine`, and adds an explicit "whisper timing is not trustworthy" warning. Test `test_whisper_fallback_retranscription_is_reported_not_swallowed`.
  Shipped SRT = Parakeet (`ave captions retranscribe ... --asr fluidaudio --srt-out exports/final.s2.v9.srt`).
- Lesson: "exact match" is only as good as the engine; verify cue starts against the planned captions (median should be < 0.05 s) before shipping.

## N5. Worktrees need the vendor engines  (status: documented)
- `ave doctor` from a worktree reports FluidAudio as a required failure. Not a bug in the repo; set `AVE_FLUIDAUDIO` (and `AVE_RESOLVE_MCP` if needed) when working from a worktree.

## Quality numbers (final.s2.v9.master.mp4)
- ffprobe: H.264 High 1920x1080, 30 fps, 33659 frames (expected round(1121.9516*30) = 33659), 6.04 Mbps; AAC LC 48 kHz stereo 248 kbps; container 1121.967 s (video) / 1121.951 s (audio).
- Loudness (ebur128 on the encoded file): integrated **-16.6 LUFS** (target -16, tolerance +/-1: pass, 0.6 LU low), LRA 7.0 LU, **true peak -3.3 dBTP** (ceiling -1.0). The speech is peaky, so the limiter holds the average down; this is the repo policy working (N11 of the first run), not a failure. A -16.0 result would need a harder limiter; the human can ask.
- blackdetect (d>=0.1 s): none. silencedetect (-50 dB, >=1.5 s): none.
- 4K source used: every segment encode reads the 3840x2160 HEVC file (~25 Mbps) and downscales with lanczos to the 1080p canvas; stills show detail in the cap lettering and bark at 1080p. The master is 1080p by design (preset `master` = canvas size); a 4K deliverable would need `canvas` 3840x2160 in the EditDoc (not done: 4x the encode and ~3 GB).
- A/V sync at output 30 s / 560 s / 1100 s: audio cross-correlation against the master sum 0.98 (peak), constant lag -5 ms (AAC priming, 1/6 frame); picture matches the source frame at offset 0 / -1 / 0 frames (560 s is a static shot, flat score).
- Captions: planned 338 cues (45 reading_speed problems, all fast speech), burned with the `podcast` style (Helvetica Neue fallback: Inter not installed, warnings said so); shipped Parakeet SRT 307 cues, 37 reading_speed problems, no overlap/length problems.
- Wall clock for the good run (under load ~300): 1923 s including whisper; do not use as a benchmark.

## Editorial choices (all proposals; the human decides)
- Lower thirds, `CO-HOST` title, left side: **Maren** at 1.2-6.2 s (the opening shot is their camera, they speak at 0.7 s) and **Tomas** at 8.6-12.8 s (first shot on their camera, their speech continues from the open). Each sits inside one shot so a name never stays up over the other person.
  Checked on real frames: fully inside title-safe, clear of both faces, and clear of the caption block (captions are bottom-centre, the card is bottom-left, vertically separate).
- One chapter card ("Part two" plus a short topic line, scrim backdrop, 938.6-942.2 s) at the real hinge, just before the hosts announce the change of mood (940 s). It dims the picture for 3.6 s but covers no face. It is the most taste-dependent graphic; drop it by placing the others on a fresh final.
- Not done: stat/quote cards (no moment asked for one), music, B-roll.

## What a human still has to look at and listen to
- Listen through the whole master once: AAC encode, the limiter on loud laughs, and every cut seam (the layer of masks and 15 pacing reactions was approved, but never heard in a final).
- Look at the chapter card choice and the exact words (and Tomas/Maren card timing).
- Read the Parakeet SRT for misheard names (the glossary maps the show name's ASR spelling to its proper form everywhere).
- Alpha overlays are composited by ffmpeg here; their look in Resolve is still the unverified swatch from `docs/motion.md`.
- Session 1 (117 min) was NOT done: with the machine at load ~300 a 4K render of that length would take hours; run `ave final <ep> --name s1 --no-render`, place graphics, then `--from-final --burn` when the machine is quiet.
