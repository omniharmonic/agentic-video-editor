---
name: ave-captions
description: Generate, check and burn captions (SRT sidecar + styled ASS) for a rough/final/select/short edit, or rebuild exact SRTs by re-transcribing a rendered video. Use after the edit is cut (rough or later) and before the final render, when the user asks for subtitles/captions, karaoke-style short captions, or a caption check.
---

# Captions

Captions are computed from `work/transcript.json` mapped through the edit's dialogue segments (removed words vanish,
room-tone fills are never captioned), segmented by the rules in `config/captions.styles.yaml`, written as plain files and
burned by ffmpeg. Resolve is not involved.

## Commands

```bash
ave captions <ep> --stage rough --style podcast        # -> captions/rough.srt + .ass, NEW edits/rough.vN.json with captions set
ave captions <ep> --stage rough --name s2              # a session's edit (same as --stage rough:s2) -> captions/rough.s2.srt, NEW rough.s2.vN.json
ave captions <ep> --stage select:<name> --style short  # vertical clip: karaoke ASS (\kf per word), block at 65% height
ave captions <ep> --stage final --burn [--video exports/final.mp4]   # also burns into the render -> <video>.captions.mp4
ave captions check <ep> captions/rough.srt             # exit 1 + JSON problems if any rule is broken (SRT or ASS)
ave captions retranscribe <ep> exports/final.mp4 [--srt-out f.srt] [--asr whisper] [--diarize]
```

Styles: `podcast` (bottom centre, white, thin outline), `short` (bold, word highlight, safe zone clear of the bottom 25% and
right 120 px at 1080 wide), `minimal` (small, quiet). Sizes are fractions of the canvas, so the same style is correct at
1920x1080 and 1080x1920. Tune numbers in the YAML, or override per episode in `<ep>/policy/captions.styles.yaml` (deep-merged).

## Judging the output

1. Read `problems` in the JSON. Kinds: `line_too_long`, `too_many_lines`, `orphan_line`, `too_short`, `too_long`,
   `reading_speed`, `overlap`, `empty`. `reading_speed` on a cue boxed in by the next cue is real (fast talker): tell the human, do not hide it.
2. Read the SRT. Check breaks fall at punctuation/clauses, not mid-phrase ("most / of"). If many are bad, adjust `avoid_ending_with`/`clause_starters` in the YAML rather than editing the file by hand.
3. LOOK at a frame with captions burned (`ave frame` on the burned video): text readable on busy footage, inside the safe zone, not covering a lower-third.
4. Proper names: add them to `episode.json` `glossary` and rerun. Either `{"glossary": {"open ai": "OpenAI"}}` (wrong -> right, multi-word ok)
   or a list of correctly capitalised names `["Maren", "Cloudflare"]` (matched case-insensitively). Avoid listing names that are also common words.
5. `drop_fillers` (um, uh...) are heard but not captioned by default; set `drop_fillers: []` for verbatim.

## Final render flow

Captions timing depends on the final cut. After the cut is approved: render, then `ave captions retranscribe` on the render
and use that SRT as the sidecar (exact wording and timing as heard). The ASR engine is FluidAudio Parakeet; if it is not built the
command falls back to whisper.cpp and says so in `warnings`. Review the retranscribed SRT for misheard names before publishing.

## Failure modes

- Retranscribed SRT looks lowercase, unpunctuated, with 5 s cues and times seconds off: that is the whisper.cpp fallback (FluidAudio not found; in a worktree set `AVE_FLUIDAUDIO`). Rerun `ave captions retranscribe ... --asr fluidaudio --srt-out <new file>`. Measured on the real episode: whisper median 0.55 s early (p10 1.4 s), Parakeet within 0.13 s.

- Font warning in the JSON: the style's preferred font is not installed; it falls back to a system font (never fails). Bundle an OFL font in `motion-kit/brand/fonts` to make output identical across machines.
- `No speech falls inside this edit's dialogue`: the edit has only fills or the transcript is missing; run `ave transcribe` first.
- Burned video size differs from the doc canvas (e.g. a proxy): the ASS is regenerated at the video's real size automatically.
- Never overwrite an existing edit: every run creates the next `vN`. Caption files in `captions/` are derived and are replaced.

## Checkpoint

Stop and show the human the style choice and a burned frame before burning a whole final or publishing shorts.
