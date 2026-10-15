---
name: ave-multicam-sync
description: Align multiple cameras and audio recorders on one timeline using their audio, measure each clock's drift in ppm, and apply it. Use when an episode has two or more video or audio files of the same recording (including takes hours into a long recorder file), before planning cuts, or when shots look out of sync in Resolve. Covers ave sync, offset/drift/residual numbers and manual fallbacks.
---

# Multicam sync

```bash
ave sync <episode>                      # reference = a recorder longer than every camera, else the longest file
ave sync <episode> --reference 03_AUDIO/master.wav --points 7     # choose the reference; more measurement points
```

Writes `work/sync.json`. Per file:

```json
{"start_offset": 4.272, "drift_ppm": 51.6, "psr": 92.5, "psr_min": 92.5, "residual_ms": 31.9,
 "points": [{"t": 36.4, "offset": 4.246, "psr": 92.5, "residual_ms": -48.2}, "... 5 points across the take"]}
```

- `start_offset` (a) = where that file's t=0 sits on the reference timeline. **+4.25** means the file started 4.25 s *after* the reference.
- `drift_ppm` (b) = how much slower the file's clock runs than the reference's (positive = slow). **Drift is APPLIED, not just reported:** a file's own time for episode time `e` is `(e - a) / (1 + b*1e-6)` (`editdoc.source_time`). `ave assemble` copies both onto `sources[k]`, and compile, handoff, cleanup, shorts and review packs all derive source positions with that one function. Real cameras were 24-85 ppm slow: 0.33 s (10 frames) off after 2 hours with a single offset.
- `psr` = `psr_min` = the WORST point's peak-to-sidelobe ratio; `points` lists every measurement (`t` = reference time, `offset` = measured, `residual_ms` = distance from the fitted line).
- `residual_ms` = the largest distance of a measured point from the model that will be USED. `model` says which: `linear` (offset + drift) or `piecewise`.
- **Piecewise drift (wobble).** Real clocks wobble 15-50 ms around a line (camera A of session 1: 41 ms). When the straight-line residual exceeds 20 ms (and at least 3 points are good) `sync.json` also stores `sync_map`: `[[file_t, offset], ...]`, the measured points, and `linear_residual_ms` (what the line alone left). `start_offset` / `drift_ppm` stay (the line fit). `ave assemble` copies `sync_map` onto `sources[k]`, and `editdoc.src_time` / `episode_time` interpolate through the points (beyond the first and last point they follow the line); `editdoc.clock_rate` is the local stretch compile uses for picture and audio. **Never write the formula yourself; call those functions.** Files without `sync_map` behave exactly as before. With the map, `residual_ms` is typically under 10 ms at every sample point; more `--points` (7-9) follow a wobble better.
- `ave sync` warns only when the model is still bad: a piecewise segment whose clock rate is more than 1500 ppm off the fitted line (a point probably mis-locked: re-run with `--points 9`), or a straight-line residual above 35 ms that a map could not fix (fewer than 3 good points).

## How it works (so you can judge failures)

1. **Coarse:** the file's decimated (10 ms) log-energy envelope is correlated against the WHOLE reference (hours are fine; offsets of 2 hours work: the real second session sits at 7119.4 s). Long takes also try a 5 minute piece, because 85 ppm smears a 2 hour envelope by 0.6 s.
2. **Refine:** GCC-PHAT at `--points` (default 5) places spread across the take, windows up to 60 s, searching a few seconds around the running prediction.
3. **Fit** offset(t) = a + b*t by least squares over the points that passed; if the line leaves more than 20 ms, keep the points as a piecewise `sync_map` too.

## How to judge it

| Signal | Meaning | Action |
|---|---|---|
| `psr_min` > 100 | Excellent. Real speech between different mics scores 100 to 3000. | Trust it. |
| `psr_min` 25 to 100 | Usable (the real episode had 54 to 150 at its weakest points). | Spot-check below. |
| `psr` < 25 | **Not a match** (unrelated audio scores about 10). `ave sync` warns "no sync found". | Do not use. See fallbacks. |
| warning "weak" | Some points (quiet stretches) were ignored; fewer than 3 usable points means drift is poorly constrained. | Re-run with `--points 9`. |
| warning "residual" | Non-linear drift that the piecewise map could not fix (fewer than 3 good points). | Re-run with `--points 9`; spot-check the end of the take; consider splitting the file. |
| warning "piecewise ... mis-locked" | One map segment runs at an impossible clock rate. | Inspect `points[].map_residual_ms`, re-run with more points. |
| warning "cannot contain" / "outside" | The reference does not cover the file. | Pick the longer file as `--reference`, or the file is a different recording. |
| `abs(drift_ppm)` > 300 | Implausible for a camera clock. | Distrust; re-measure. |

## Spot-check (cheap; do it when psr < 100, the residual warns, or the human is skeptical)

Pick a sharp transient in the transcript (a laugh, a clap, a hard consonant start) NEAR THE END of a take, where drift matters. Extract stills from both
cameras at the *same episode time*, using the file's own clock (`editdoc.src_time`; for a file without `sync_map` that is `(t - start_offset) / (1 + drift_ppm*1e-6)`):

```bash
ave frame <episode> raw/camA.mov 6900.40
ave frame <episode> raw/camB.mov $(python3 -c "print((6900.40 - 4.2499) / (1 + 24e-6))")
```

Look at the two images: the speaker's mouth should be in the same state.

## Channel layout of the reference (read before trusting the sync audio)

The reference recorder is usually a stereo master. Check `channel_layout` in `media.json` (set by `ave ingest` from the WHOLE file, never from spot checks) and, for a new recording setup, verify it against diarization (`ave transcribe --check-diarization`). `speaker_per_channel` (one host per mic) is analysed as the L+R SUM, which keeps interjections and level; a `dead_channel` file is analysed from its live channel. Syncing against an averaged or gated mono derivative of a speaker-per-channel master starves the correlation, so if `psr` is low on a stereo reference, confirm the layout verdict first.

## Sessions

Cameras of different sessions are all synced against the same recorder; each lands at its own place on the timeline (session 2 of the real episode is 7119 s in). A camera that finds no match in the recorder is from another recording.

## Fallbacks

- **Weak or no shared audio:** the cameras may not have heard the same room (one is a screen recording; one is muted). Ask for a clap/slate point, or a visible event, then compute the offset by hand and write it into `sync.json` yourself with `"method": "manual"` (and `"drift_ppm": 0`).
- **Drift you do not trust:** set `drift_ppm` to 0 in `sync.json` by hand and note why; the offset alone is then used.
- **Different frame rates:** sync is in seconds, so unaffected, but conform in Resolve (`media.json` warns).

Never edit the raw folders to fix sync; offsets live in `sync.json` and the EditDoc sources.
