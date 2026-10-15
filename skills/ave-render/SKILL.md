---
name: ave-render
description: Render an EditDoc (assembly, rough, final or a select) to a verified, loudness-normalized video or audio file with FFmpeg. Use when the human wants to watch or hear an edit, when a stage needs a proxy draft for review, before publishing (final master), or to check that an EditDoc actually plays back as intended.
---

# Render: EditDoc to media

`ave render` compiles `edits/<stage>.vN.json` with FFmpeg and then **measures the file it wrote**. It never edits the doc and never
touches `raw/`. Output goes to `exports/` unless `--out` says otherwise.

## Commands

```bash
ave render <ep>                                  # latest of final > rough > assembly, preset master
ave render <ep> --stage rough --proxy            # 540p draft for review (fast)
ave render <ep> --stage select:intro --version 2 # a specific select
ave render <ep> --stage final --preset youtube   # -14 LUFS delivery
ave render <ep> --stage final --preset audio_only --out exports/ep12.m4a
ave render <ep> --name s1 --proxy               # a named edit (a session id): rough.s1 / final.s1 ...
ave render <ep> --overwrite                      # replace an existing output (otherwise it refuses)
ave render <ep> --no-captions                    # skip the ASS burn even if the doc has one
```

Presets: `master` (H.264 CRF 18, AAC 256k, -16 LUFS), `youtube` (-14 LUFS), `proxy` (540p, CRF 28, fast), `audio_only` (.m4a),
`podcast_mp3` (.mp3). All keep true peak at or below -1 dBTP. Tune them in `src/ave/presets.py`.

## Reading the report (stdout JSON)

| Field | Meaning | What to do |
|---|---|---|
| `ok` | The file passed verification (streams present, size, fps, exact frame count, duration within one frame) | If the command exits non-zero, nothing was written; read the error |
| `duration` vs `expected_duration` | Measured vs the doc | Must match to one frame |
| `lufs`, `true_peak_db`, `lra` | Measured on the FINAL encoded file | Target is the preset's LUFS +/- 1; true peak <= -1 |
| `segments` | Counts of video, dialogue, music, overlays rendered | Compare with what you expect from the doc |
| `captions_burned` | Whether the ASS file was burned in | False when the doc has no `captions.ass` or `--no-captions` |
| `warnings[]` | Anything you must tell the human | Never drop these; report them |

Typical warnings and what they mean:
- `loudness ... outside target`: the material cannot reach the target without breaking the -1 dBTP ceiling (very peaky or very dynamic). Report it; do not force it.
- `... before the start of its file` / `runs past the end`: a segment asks for source time that does not exist (black frames or silence were used). That is an EditDoc bug (offsets or `e0`); fix the doc, do not ship.
- `audio is silent`: normalization was skipped; check the dialogue sources.
- `no brand fonts`: captions fell back to system fonts; fine for drafts, flag for finals.

## How to judge a render (do this before telling the human it is good)

1. Look at frames, not just numbers: `ave frame` works on sources; for the render, extract a few stills with
   `ffmpeg -ss <t> -i exports/<file>.mp4 -frames:v 1 /tmp/f.png` at cuts, at zoomed segments, inside overlay windows and while a caption is up.
2. Check the numbers in the report against the checkpoint rule: the rough cut and the final cut each need human approval after a `--proxy` render.
3. If a cut sounds clicky or a pause feels dead, inspect the doc: `xfade_ms` on the dialogue segment (5-15 ms in speech, 20-40 ms across a gap) and `fill: room_tone`.

## What it guarantees (and what it does not)

- Video is frame-snapped: exactly `round(duration * fps)` frames, boundaries rounded with one rule, source clock = `(e0 - start_offset) / (1 + drift_ppm*1e-6)` (`editdoc.source_time`, so a drifting camera still lands on the right frame at the end of a long take). A source with a `channel_map` is folded to dual mono: from its live channel (`left|right`, one dead channel) or as the soft-limited L+R (`sum`, a speaker-per-channel master).
- Audio is sample-accurate at 48 kHz, crossfades are equal power and centred on the seam, music ducks by exactly `duck_db` while dialogue speaks.
- Overlays are visible only between their frame-snapped `o0` and `o1`, ordered by `layer`. Captions burn on top.
- Not guaranteed: that the edit is *good*. Taste is the cleanup policy and the human's call.

## Failure modes

- `encoding video segment(s) [...] failed` with an empty ffmpeg message: transient (machine under heavy load); segments are retried 3 times automatically, so seeing it means all three failed. Check load/RAM and re-run; never `pkill ffmpeg` on a shared machine.

- `FileExistsError`: output exists; use `--overwrite` or `--out`.
- `invalid EditDoc`: the doc fails `editdoc.validate`; the message lists every problem. Fix the producing stage.
- `missing media referenced by the EditDoc`: lists every missing file (sources, music, overlays, ASS). Nothing was rendered.
- `output failed verification`: the rendered file did not match the doc; it is deleted. Re-run; if it repeats, report the message verbatim.
- Intermediates live in `work/render/` and are removed on success and on failure.

## From Python

```python
from ave import compile, editdoc
report = compile.render(ep, editdoc.load_latest(ep, "rough"), ep.exports / "rough.proxy.mp4", preset="proxy")
compile.compile_audio(ep, doc, "work/dialogue.wav")   # dialogue (+ducked music) master, 48 kHz stereo float WAV
compile.compile_video(ep, doc, "work/picture.mp4")    # picture only
```
