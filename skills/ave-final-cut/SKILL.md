---
name: ave-final-cut
description: Produce the final cut, the third stage after assembly and rough cut: add music, motion graphics and B-roll with taste and licenses, attach styled captions, render with measured loudness, and regenerate exact-match subtitles from the rendered file. Use when the rough cut is approved and the user asks for the final, the finished episode, or "polish".
---

# Final cut

Input: the approved **rough** cut (`edits/rough.vN.json`). Output: `edits/final.vN.json`, a verified master render, and an SRT that matches the rendered audio exactly.

## 1. Decide the creative layer (propose, then confirm with the human)

Read `work/transcript.json` / `ave cleanup explain` and propose a short plan: where a lower third belongs (first time each speaker talks), chapter cards if the episode has clear
sections, any pull-quote card, and whether music is wanted at all (silence is a valid choice; many conversational episodes want only a quiet bed under the intro/outro).

- **Motion graphics** (`ave-motion`): `ave motion still ...` and LOOK at it before placing; `ave motion place <ep> --template lower-third --props '{...}' --at S --dur 4 --stage rough`.
- **B-roll and music** (`ave-broll-music`): only with the license log. Apply the critic checklist visually (contact sheets); B-roll holds 1.5-4 s, never over the emotional beat, at most 40% coverage.
  Pick music with `ave assets music-pick`; it sits -18 to -24 dB under dialogue and ducks.
- Everything you add is recorded in the EditDoc with `reason`; nothing is baked into the footage.

### Opening and closing (decide with the human, skill `ave-intro-outro`)

The final also carries the show's opening (optional cold open, title card, host introductions) and closing (thanks, credits roll, held end card). Ask the human about them at this step:
do they want them, which hook, which links, which editor credit. Then `ave sequence plan <ep> [--name s1]`, look at `ave sequence preview`, and ask the human to run
`! ave sequence approve <ep>`. Turn them on with policy `sequences: {opening: true, closing: true}` (`<project>/policy/final.yaml`) or `ave final --opening --closing`.
`ave final` refuses an unapproved or since-changed plan (`--draft-sequences` builds a marked draft). Music or stings used there are license-gated like any other.

## 2. Build the final

**Order when you add graphics (multi-session or not):** `ave final <ep> --name s2 --no-render` (makes `final.s2.vN` with captions), then `ave motion place <ep> --name s2 --stage final ...` for each graphic, then
`ave final <ep> --name s2 --from-final --burn --preset master`. Without `--from-final`, `ave final` re-clones the rough cut and silently drops the overlays. Place a lower third inside ONE shot of that person's camera (check `video[].src`), not across a cut.
Working from a worktree? `vendor/` is untracked there: `export AVE_FLUIDAUDIO=<main repo>/vendor/FluidAudio/.build/release/fluidaudiocli`, otherwise the retranscription falls back to whisper (see below).

```bash
ave final <episode> --music <file> --music-in <seconds> --music-gain -20 --duck -8 [--style podcast] [--burn] [--preset master|youtube]
ave final <episode> --name s1            # multi-session episode: rough.s1 -> final.s1, exports/final.s1.vN.*, one session at a time
ave final <episode> --no-render            # EditDoc + captions only, for a quick check
```

It (1) clones the rough cut into `final`, (2) refuses music/B-roll without a license-log entry (use `ave assets log-license`, or `--allow-unlicensed` only if the human owns the rights),
(3) attaches captions (SRT + styled ASS), (4) renders with `ave render` (frame-exact video, sample-accurate audio, two-pass loudness) and verifies the file,
(5) **re-transcribes the rendered file** and writes `exports/final.vN.srt`: ship THIS SRT, not the planned one, so captions match what is heard after every edit.

Without a rough cut it builds from the assembly and warns; say so to the human.

With sequences on, the body is rendered aside and joined to the opening and closing (picture by stream copy, audio in float with 0.2 s equal-power fades at the joins, one loudness pass).
The report gains a `sequences` block (`opening_offset_s`, `joins_s`, `part_lufs`, `planned_srt`); the shipped SRT is the re-transcription of the JOINED file, so it already includes the cold open and the opening's offset.
`captions/final.programme.srt` is the planned SRT of the whole programme (cold-open cues, then the body's shifted by the opening's exact length); `ave handoff` offers that one.

## 3. Check before you present

Real numbers from the first final (22 min, 4K -> 1080p master): 883 MB, -16.6 LUFS integrated, -3.3 dBTP (peaky speech: the limiter keeps the average ~0.5 LU under target; inside the +/-1 tolerance), no black frames or silences over 1.5 s, audio 5 ms (AAC priming) and picture 0 frames off the sources.
Measure it yourself: `ffmpeg -i f.mp4 -af ebur128=peak=true -vn -f null -`, `blackdetect`, `silencedetect`. The retranscribed SRT must match the planned one: median cue-start difference < 0.05 s (Parakeet: 0.008 s; **whisper.cpp: 0.55 s, p10 1.4 s early, no case or punctuation, so never ship it**: the report carries a warning and `retranscribed_srt.engine`).
A transient ffmpeg segment failure is retried 3 times; if a render still fails, the message names the segment id (`v170`), re-run it.

- Report: duration, `lufs` (target -16 master / -14 youtube) and `true_peak_db` (<= -1 dBTP), caption `problems` (reading speed over 21 cps is common in fast speech; only worry about overlap, line length, orphan words).
- Spot-check a few frames and a few seams: `ave frame` on the render at cuts, a caption frame (`ave render --preset proxy` then look), a motion overlay mid-animation.
- Reading `warnings` matters: a missing font falls back (install Inter/Newsreader under `motion-kit/brand/fonts` for the intended look), a source running past its end, a true peak above target.

## 4. Hand over

`ave handoff <episode>` puts the final in Resolve as a NEW timeline (video cuts, the compiled dialogue master, music on A2, overlays on V2+). **Stop and wait for the human's notes.**
Their tweaks in Resolve are theirs; if they want regeneration instead, take notes and make the next `final.vN`. Re-run `ave final` for each revision; versions never overwrite.

## Never

Never publish, upload, or spend on generative APIs from here. Never describe the final as approved until the human says so. After approval: `ave-clip-finder`, then `ave-shorts`, then `ave-publish`.
