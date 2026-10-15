# Field notes: punch-in dwell and a clipped word (first real episode, session 2)

> **Working note from the first real episode.** This is internal history kept as evidence: what we assumed, what a real recording showed, and what changed. It describes one project (a two-host, two-camera podcast), uses its real numbers, and was written while the tools were still changing, so commands and defaults may have moved on. For the current behaviour read the skill or doc a note points to. See [docs/README.md](../README.md).

Creator feedback on the real final cut (`06_EXPORTS/final.s2.v9.master.mp4`, built from `rough.s2.v3`): (1) punch-ins go IN, OUT, IN within seconds and feel jumpy;
a slow in/out is natural, in-out-in is not, there must be a hard limit; (2) in one place a cut sliced off a word the speaker was saying.
Everything below was measured on the real EditDocs and transcript, read-only. The "after" is `ave cleanup` run on a SCRATCH copy of the episode (symlinked footage and work files,
copied edits), never on the episode: the episode's own edits are untouched.

## N1. Zoom changes: measured before (rough.s2.v3 = final.s2.v9, same video track)  (status: applied)
- 255 video segments, 43 zoom changes on the same camera (28 in, 15 out). Zoom values 1.0 / 1.10 / 1.12 / 1.18 / 1.20.
  Producers: 47 segments punched by masking (`_Masker` punch_in, 39 of 123 cuts) and 2 by the pacing pass (15 reactions: 13 listener, 2 punch-in; the pacing blips are 2-4 s).
- Intervals between consecutive zoom changes: 42 gaps, median 7.4 s (13.2 s when only same-camera chains are counted), min 0.58 s. 16 gaps < 4 s, 21 < 8 s, 23 < 12 s.
- **In-out-in inside 8 s: 4** (73.8 / 74.6 / 75.9 s, 75.9 / 76.6 / 77.4 s, 197.3 / 197.9 / 198.6 s, 1006.3 / 1007.1 / 1013.7 s; 8 inside 12 s). Of the "in" punch-ins, 12 of 15 were held for under 8 s (median 1.1 s) before the next cut flipped them back out.
- Cause, in `cleanup._Masker._mask_cut`: a cut inside a hold that is already punched in goes to `z = base` (an "out"), the next one in again, so a cluster of cuts (fillers 0.6-1.3 s apart, e.g. 73-78 s) flips the framing at every cut. The pacing punch-in was a 2-4 s blip. `shorts.mask_jump_cuts` alternated on every jump cut the same way.
- The limiter's own count with the default dwell (8 s): 19 `short_dwell` (12 in->out, 7 out->in), 4 `in_out_in`, 4 `multiple_ins`.

## N2. Zoom-state limiter shared by all producers  (status: applied)
- New `src/ave/zoomstate.py`: `allows()` (ask before reframing), `upcoming_change_ok()`, `validate()` (post-pass audit). Rule: a zoom change on a camera comes >= `zoom_min_dwell_s` after the previous one on that camera
  (a cutaway to the other camera does not reset the clock; a camera change is not a zoom change), and a shot is never punched in deeper than `max_ins_per_shot` without coming back out.
- Policy (config/cleanup.default.yaml): `masks.zoom_min_dwell_s: 8`, `masks.max_ins_per_shot: 1`, `pacing.zoom_min_dwell_s: 8`, `pacing.max_ins_per_shot: 1`. `0` restores the legacy behaviour exactly
  (verified: with every new key off, `analyze` on the real assembly reproduces `rough.s2.v3` video and dialogue tracks byte for byte).
- Masks: cutaway / wide / camera-change masks are unchanged. A punch-in that would break the rule is skipped; with no other mask the cut is a crossfade with `needs_review` and the reason says
  `punch-in blocked by the zoom limit: zoom on A2 already changed (in, 1.00x->1.18x) 0.8s ago (zoom_min_dwell_s 8)`.
- Pacing: a punch-in reaction is a sustained reframe held to the end of the hold (the next real camera change); in a hold longer than `max_hold_s` it returns at a sentence boundary no sooner than the dwell
  (real example in the scratch cut: B2 at 1022.1 s, 1.10x held 13.5 s to the end of the hold, instead of a 2-4 s blip). Decision records `sustained: true`.
- Shorts: `mask_jump_cuts` takes the same dwell (`build.zoom_min_dwell_s`, default 8); a quick default zoom-out after a masked jump cut is held at the punched framing (the cut stays a plain jump cut, noted).
- Validator in `ave cleanup` (`zoom_check` in the JSON, `Zoom:` line and warnings in `explain`, `zoom` in work/cleanup.json) and in `ave final` (`checks.zoom` + warnings). It warns, never edits.

## N3. Zoom: measured after (scratch run, `ave cleanup` default policy on assembly.s2.v2)  (status: measured)
| | before (rough.s2.v3) | after (scratch rough.s2.v4) |
|---|---|---|
| zoom changes (in / out) | 43 (28 / 15) | 25 (21 / 4) |
| shortest gap between zoom changes | 0.58 s | 8.5 s |
| median gap (same-camera chains) | 13.2 s | 56.6 s |
| `short_dwell` / `in_out_in` / `multiple_ins` | 19 / 4 / 4 | 0 / 0 / 0 |
| masks | cutaway 72, punch_in 39, camera_change 12 | cutaway 73, punch_in 24, camera_change 13, **none 13** |
| cuts needing review | 0 | 13 |
| pacing reactions | 15 (13 listener, 2 punch-in blips of 2-4 s) | 16 (15 listener, 1 sustained punch-in) |
| output duration | 1121.95 s | 1122.43 s (+0.48 s from the tail pad, see N5) |
- The price is **13 cuts that are now a plain audio crossfade over a visible jump cut** (output 74.6, 75.9, 76.6, 198.1, 370.0, 395.0, 682.1, 715.4, 909.2, 951.1, 1002.1, 1007.6, 1014.2 s in the new cut). They are the cuts that used to be masked by a quick punch-in/out flip
  in clusters of fillers < 1.2 s apart, where no cutaway fits (`min_hold_s`) either. Each one carries the reason. This is a taste call for the human: look at them; the levers are `masks.zoom_min_dwell_s` (lower = quicker zooms), more coverage (a wide camera), or `min_hold_s`.

## N4. Cut inside a word: the span test finds nothing, the audio finds the clipped tails  (status: measured; fix applied; the human must listen)
- Check (new `src/ave/cutwords.py`): every dialogue splice of the compiled EditDoc in SOURCE clock (`editdoc.src_time`, drift and sync maps honoured) against `work/transcript.json` word spans.
  On `rough.s2.v3` and `final.s2.v9`: 123 splices, 246 cut points, **0 inside a word** at the 40 ms margin (2 touch a word edge exactly at margin 0). So the slice the creator heard is NOT a cut inside an ASR word span.
- Why: **every word time in the transcript is on an 80 ms grid** (Parakeet frames: 18932 of 18932 edges are multiples of 0.08 s) and ASR word ends run early. Measured on 236 gaps >= 250 ms: energy above the quiet limit (speech p90 - 20 dB) ran >= 40 ms past the ASR word end
  for 36% of words, >= 80 ms for 31% (p75 105 ms); and >= 80 ms before the ASR start for 14% of words. A cut AT the ASR end (22 of 123 cut-outs had < 20 ms pad, because a removed word starts right after the kept one) clips the tail.
- Second check (`hot_edges`): a cut at a word edge, no pad, with audio within 12 dB of speech level. rough.s2.v3 has **18**; the two worst: **"right." at output 894.13 s (episode 8129.68, -16.5 dB at the cut, tail decays over 145 ms, ASR then hears a 0.08 s ghost "Oh" at conf 0.77)**
  and **"okay." at output 1006.28 s (episode 8301.12, -14.7 dB, a removed "Um" follows immediately)** (output times in rough.s2.v3; the new cut shifts them by about +0.5 s). These are the prime suspects for the creator's slice; I could not hear them, the human must.
- Fix (policy `pause.word_tail_pad_ms` / `word_lead_pad_ms`: 80 ms = one ASR frame; `*_ghost_ms`: 150 ms): where the default cut point is not in a quiet valley, the cut-out (cut-in) may slide into the removed word to a REAL valley (<= speech - 20 dB)
  reached along a smooth decay (`snap.pad_decay_tol_db` 8), at most 40% of the removed run, 150 ms only when the first removed word is a low-confidence word of <= 160 ms (a "ghost": the kept word's own tail), 80 ms otherwise, never through a confident real word.
  Tried and rejected: sliding without the ghost rule ate 119 ms of a confident short word ("the") mid-phrase (stutter); `snap.widen_if_not_valley` (search the whole allowed range) kept 4.2 s more silence over 123 cuts and did not reduce the hot count, so it ships off.
- After (scratch rough.s2.v4): inside a word 0, `hot_edges` 14 (was 18). **"right." is fixed** (the cut moved past the decay to a real valley; it no longer appears in `hot_edges`). **"okay." is not**: no real valley within 80 ms, the removed "Um" starts at the same level, so the cut stays and is warned.
  The remaining 14 are cuts between words with zero gap (repeats like "because because", "we're doing") where the ASR gives no gap to cut in.
- `ave cleanup` and `ave final` run both checks and warn (`cut inside a word: ...`, `cut at a word edge with speech continuing: ... listen, the word may be clipped`); they never edit.

## N5. Operational notes
- Worktrees have no `.venv` and `pip -e` points to the main checkout: run tests with `PYTHONPATH=src .venv/bin/pytest -q` (a symlinked .venv works) or the suite silently tests main's code.
- The scratch episode used for the "after": `$SCRATCH/ep00` (episode.json + edits copied, work files and footage symlinked); nothing was written to `<external-drive>`.
- Not done / for the human: listen to "right." (894 s) and "okay." (1006 s) in the new cut, look at the 13 unmasked cuts, decide on `zoom_min_dwell_s` (8 default; a lower value gives masks back at the price of quicker zooms; not measured at other values).
