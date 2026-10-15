---
name: ave-cleanup
description: Turn the assembly cut into a rough cut by removing dead air, filler words (um/uh), repeated words and false starts, with every splice hidden by a camera switch, cutaway or punch-in. Use after the human has approved the assembly edit and before captions, music or the final cut, or when the user says "clean it up", "tighten", "remove ums", or "cut the dead air".
---

# Cleanup: assembly -> rough cut

Requires `edits/assembly.vN.json`, `work/transcript.json`, and the analysis audio (`work/audio/*.16k.wav`). Never edits raw footage or an
existing edit: each run writes the NEXT `edits/rough.vN.json` from the same assembly, so it is safe to rerun after changing policy.

## 1. Look at the policy, then dry-run

The policy (`config/cleanup.default.yaml`, or `<ep>/policy/cleanup.yaml`, or `--policy`) holds every number: how long a pause may be
(`pause.max_ms`), what it shrinks to (`target_ms`, never under `floor_ms`), which words are fillers, what is protected. Ask the human
about taste ("how tight?") and change the YAML, never the code. A partial file is enough; it merges over the default.

```bash
ave cleanup <episode> --dry-run          # decides everything, saves nothing; stdout is JSON
ave cleanup <episode>                    # writes edits/rough.vN.json + work/cleanup.json
ave cleanup explain <episode>            # every decision in words, items needing a human look first
ave cleanup <episode> --name s1         # multi-session episode: assembly.s1 -> rough.s1 + work/cleanup.s1.json (explain with --name s1 too); only that session's speech is judged
```

## 2. Judge the result (do not just forward it)

Read `ave cleanup explain`. For each item check:

- `totals`: time removed is plausible (typically 3-10% of a conversation). A very large number means the policy is too aggressive.
- Each `needs_review` item: `review_phrase` ("like", "you know" clusters) and `flag` items are NEVER cut automatically; decide with the human.
  `nonlexical` = sound in a gap the transcript calls silent (breath, cough, a missed "um"); listen at that time before cutting.
- Cuts with `mask: none` have only an audio crossfade and a visible jump: tell the human, or fix by policy (allow `punch_in`, add coverage).
- Low `confidence` (<0.6 is auto-flagged) usually means no quiet spot was found near the cut (`snap.valley: false`) or the ASR word was uncertain.
- Pauses kept as `protect` (after laughter or "!", before "turns out"): confirm they really are beats.

To add your own judgment (a false start the heuristics missed, a filler to keep) write `work/cleanup_overrides.json` and rerun:

```json
{"cut":  [{"words": [212, 214], "type": "false_start", "reason": "restarts the sentence", "confidence": 0.8}],
 "keep": [{"id": "c007", "reason": "intentional hesitation"}, {"e0": 431.2, "e1": 433.0, "reason": "keep this pause"}]}
```

`words` are indices into `work/transcript.json` `words`; times snap to whole words, so an override cannot cut inside a word.
Check `warnings` in the output: an override that did not apply is reported there.

## 3. How cuts are made (so you can explain them)

Cuts are only ever between words (or exactly a removed word), then slid to the quietest spot within `snap.window_ms` and to a zero
crossing. Audio crossfade: 5-15 ms inside speech, 20-40 ms across a kept pause. Masking order for each splice: the camera already changes
(speaker change) -> cut away to the other camera (if it has footage and the shot has been up at least `min_hold_s`) -> a wide -> punch-in
(1.10-1.20x, ONE punch-in held, see 3a) -> crossfade only and `needs_review`.

## 3a. Zoom state: one punch-in, a real dwell, then out (never in-out-in)

A punch-in that goes IN, OUT, IN within a few seconds reads as jumpy (creator feedback on the first real final cut: 19 of 42 gaps between zoom changes were under
8 s, 4 in-out-in runs). A slow in/out is natural. So every producer of punch-ins (the masks above, the pacing pass, shorts' jump-cut masks) shares one rule (`src/ave/zoomstate.py`,
keys `masks.zoom_min_dwell_s` and `pacing.zoom_min_dwell_s`, default 8 s, and `max_ins_per_shot: 1`; 0 turns it off):

- a zoom change on a camera must come at least the dwell after the previous zoom change on that camera (a cutaway to the other camera does not reset the clock);
- a shot is never punched in deeper than one level without coming back out;
- a pacing punch-in is a **sustained reframe** held to the end of the hold (the next real camera change), not a 2-4 s blip; in a hold longer than `max_hold_s` it returns at a
  sentence boundary no sooner than the dwell;
- when a mask would break the rule, the cutaway/other camera masks are tried as before; if none fits, the cut gets a plain audio crossfade with `needs_review`, and the decision
  `reason` says `punch-in blocked by the zoom limit: zoom on A2 already changed (in, ...) 0.8s ago`. These are visible jump cuts: look at them (they cluster where cuts are < 1.2 s apart).

`ave cleanup` prints `zoom_check` (and `Zoom:` in `explain`); `ave final` prints `checks.zoom`. Both list every `short_dwell`, `in_out_in` and `multiple_ins` as a warning (a validator never edits).
If the creator wants more punch-ins, lower the dwell by ear (`masks: {zoom_min_dwell_s: 5}`); do not turn the rule off to "fix" the 13 unmasked cuts, add coverage (a wide or the other camera) instead.

## 3a2. Cut inside a word (post-pass check, and the tail pad)

`src/ave/cutwords.py` converts every dialogue splice to the SOURCE clock with `editdoc.src_time` and reports (1) cuts that land inside a transcript word (`start + word_margin_s < t < end - margin`;
`sliced` through a kept word, `stub` = a short leftover of a removed word) and (2) `hot_edges`: a cut at an ASR word edge, with no pad, where the audio is within `hot_db_below_speech` of speech level, i.e.
the word is probably clipped although the span test passes. Parakeet word times sit on an 80 ms grid (every word of the real episode), so an edge is only good to +-40 ms and ASR word ends run early:
energy ran >= 80 ms past the ASR end for 31% of words. The cut therefore may slide up to `pause.word_tail_pad_ms` / `word_lead_pad_ms` (80 ms; 150 ms through a low-confidence "ghost" word) INTO the removed word, but only
to a real valley reached along a smooth decay, never through a confident real word. Warnings read `cut inside a word: ...` / `cut at a word edge with speech continuing: ... listen, the word may be clipped`:
listen to each at its output time before approving. `ave final` runs the same check on the compiled edit (`checks.word_cuts`).

## 3b. Pacing: breaking long holds (after the masks)

Masks only fire at cut points, so a long turn would stay on one camera for a minute. After masking, the pacing pass inserts a 2-4 s **reaction shot** into every hold
longer than `pacing.max_hold_s` (14): a `role: wide` camera first, else the listener's camera (never while they speak), else a punch-in (1.10/1.20x alternating). It cuts
only between words, preferably at the end of a strong sentence, at least 2.5 s from the neighbouring cuts, and never touches the dialogue. Each one is a video segment
`reason: "pacing: reaction"` with a `confidence`.

```bash
ave cleanup <episode> --no-pacing        # same rough cut without reaction shots, to compare
ave cleanup explain <episode>            # "Pacing:" line: reactions by camera kind, holds over the limit before -> after, average shot, cuts per minute
```

Judge it from the `Pacing:` line and `pacing` in `work/cleanup.json` (do not just forward it):

- `holds_over_limit` should fall toward 0. `STILL OVER THE LIMIT` rows say why: the cut-rate cap (`max_cuts_per_min`, default 8) in a minute that already has many masks, no word boundary (one very long ASR word), or no footage on the listener camera.
- `cuts_per_min` can go UP: the cutaway masks already make 10-12 a minute on real conversations, and a hold past `max_hold_s + reaction.max_s` is fixed anyway (`rate_override: true`). If the edit feels busy, the lever is `masks.*` (cutaway length, `min_hold_s`) as much as `pacing.*`.
- Look at three reactions in `ave render` or Resolve: is the listener actually reacting (not mid-sentence, not talking)? Punch-ins are the weak fallback; a third (wide) camera makes pacing much better.
- Tune with the creator by ear, in `<episode>/policy/cleanup.yaml` (two lines are enough): `pacing: {max_hold_s: 18}` calmer, `{max_hold_s: 10}` busier, `hard_rate_cap: true` never above `max_cuts_per_min`, `camera_order: [listener, punch_in]` to skip the wide, `enabled: false` off.
  Full key list: `docs/cleanup-policy.md` ("Pacing").

## 3c. Room tone (so selects can have gaps)

Every rough cut gets `tracks.room_tone` automatically: the quietest steady 2-4 s stretch of the dialogue source's analysis audio (the L+R sum for a speaker-per-channel master, the live channel for a dead-channel file), searched near the span the edit uses; speech, transients and silent recorder pads are rejected. It is written as `{"src","e0","e1","rms_db","auto":true}` in episode time. Policy keys: `room_tone:` in `config/cleanup.default.yaml` (`enabled`, `min_s`, `max_s`, `steady_db`, `min_floor_db`, `margin_s`). A cleanup warning "no room tone" means no suitable stretch exists; set `tracks.room_tone` by hand and `ave clips build --force` will then give gaps. When the compiler renders a `fill: room_tone` gap it matches the fill to the noise floor measured on both sides, so there is no level jump. Check one gap by ear.

## 4. Checkpoint

Stop here. Summarize for the human: duration before/after, counts (fillers, repeats, false starts, pauses), the masks used, the pacing numbers (reactions inserted, holds before -> after, cuts per minute), every
`needs_review` item with its timestamp, and any `warnings` (including `long hold` and any `STILL OVER THE LIMIT` pacing rows). Do not start captions, music or the final cut until
they approve the rough cut (`ave status` shows it as waiting). Rendering a preview needs `ave render` (a separate stage).

## Failure modes

- `Missing analysis audio`: run `ave audio <episode>`. `No assembly edit`: run `ave assemble <episode>`.
- `policy pause: need 0 < floor_ms <= target_ms <= max_ms`: fix the YAML; the message names the key.
- Everything flagged / nothing cut: the transcript may lack fillers (some ASR engines drop them). Check `work/transcript.json` for "um"; if absent,
  cleanup cannot see them. Try `--asr whisper` re-transcription, or rely on `nonlexical` detection.
- Timestamps drift in long files (cuts land late): re-run `ave sync`; word times are only as good as the transcript.
