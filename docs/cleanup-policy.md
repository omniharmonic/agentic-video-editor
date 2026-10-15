# Cleanup policy

`ave cleanup` turns `edits/assembly.vN.json` into `edits/rough.vN.json`. All taste lives in YAML; the defaults are
`config/cleanup.default.yaml` (every key is commented there). They are tunable starting points from editing practice, to be calibrated by
listening, not measured truths.

## Where the policy comes from

First found wins, then it is deep-merged over the default (so a policy file can be two lines):

1. `--policy PATH`
2. `<episode>/policy/cleanup.yaml`
3. `~/.config/ave/cleanup.yaml`
4. `config/cleanup.default.yaml`

The fully resolved policy and its source path are copied into `work/cleanup.json`. Invalid policies fail fast naming the key
(`pause: need 0 < floor_ms <= target_ms <= max_ms`). In YAML quote `"no"` / `"yes"` in word lists (unquoted they become booleans).

## What it decides

| Type | Detected by | Default action |
|---|---|---|
| `dead_air` | gap between two words > `pause.max_ms` (700) | shorten to `target_ms` (350), never below `floor_ms` (200) |
| `filler` | `fillers.cut` words (um, uh, er, ...) | cut |
| `review_phrase` | `fillers.review` ("like", "you know") clustered within `review_window_s` | flag only, never cut |
| `repeat` | same word / n-gram twice in a row (same speaker, gap <= `max_gap_ms`) | cut the earlier copy; `repeats.allow` words are emphasis |
| `false_start` | a cut-off word (`we-`, `th—`) | cut with its clause if the speaker restarts, else flag |
| `nonlexical` | energy burst inside a gap the transcript calls silent | flag (cut only if `nonlexical.auto_cut: true`) |
| `backchannel` | `backchannels.keep` words ("yeah") from the other speaker | kept; recorded so `explain` shows it was considered |

Protected pauses (`protect`): after a laughter/emotion token (`after_regex`), after a word ending in `after_punct` (`!`, `…`, `—`), or
before `before_phrases` ("turns out", "guess what"). They are left alone, except that a pause longer than `cap_ms` is still shortened to the cap.
Anything that would cut another speaker's words (crosstalk) is never cut; it is flagged.

Never inside a word: every splice point has a hard window made from word boundaries (`pause.min_tail_ms` after the previous word,
`min_lead_ms` before the next, and an exact edge against a removed word). Audio snapping (`snap.*`) can only move a point inside that window.

## Snapping and crossfades

Each splice point moves to the quietest frame within `snap.window_ms`, among frames within `valley_tolerance_db` of the quietest, choosing the one nearest the
intended point, then to the nearest zero crossing within `zero_cross_ms`. If the result would leave the kept silence more than `pause.target_tolerance_ms`
from the target, the unsnapped point is used. A splice not in a valley (continuous speech, e.g. an "um" with no pause around it) gets a confidence penalty
and is reported with `snap.valley: false`. Crossfade (`xfade_ms` on the segment after the cut): `xfade.intra_speech_ms` when the kept gap is under
`intra_if_kept_gap_ms`, otherwise `across_gap_ms`. Dialogue segments abut at the cut; the compiler crossfades using source material beyond the cut.

## Masking (priority order, `masks.*`)

1. The picture already changes at the cut (a speaker-change pause that switches camera): nothing extra (`camera_change`).
2. Cut away to the other camera for `cutaway_s`, then return (`cutaway`): needs that camera's footage to cover the moment (from `media.json`), the shot on screen at least `min_hold_s` before, and at least `min_hold_s` left after; the same camera is not reused inside `repeat_window_s`.
3. A wide (a source with `role: wide`) the same way (`wide_cutaway`).
4. Punch-in on the segment after the cut (`punch_in`): `zoom` from `masks.zooms` in turn; the next unmasked cut on the same shot returns to the base framing.
5. Nothing fits: audio crossfade only, `mask.type: none`, `review: true`.

## Pacing (`pacing.*`): breaking long holds

Masks only fire at cut points, so a long turn with no cut stays on one camera for a minute (field note N7: session 1 had 103 holds over 14 s). After masking,
the pacing pass walks the video track and, for every hold (same camera, same framing) longer than `pacing.max_hold_s` (14), inserts a reaction shot. The dialogue is never touched.

- **Where:** between words only (never inside a word of anyone). Preference: the end of a strong sentence (`boundary.sentence_min_words` words, score `prefer.sentence`) over a breath pause (`pause_ms`, `prefer.pause`) over any word boundary, aimed about `target_hold_s` into the hold. At least `reaction.min_gap_after_cut_s` (2.5) after the previous cut and before the next.
- **How long:** `reaction.min_s`-`max_s` (2-4 s); it ends on a word boundary too, closest to `prefer_s`.
- **Which camera** (`camera_order`, first that works): a `role: wide` camera that has footage there; the listener's camera (never while that person speaks, unless `listener_may_speak`); a punch-in on the same camera (`punch_zooms` 1.10 / 1.20 alternating; from an already zoomed shot it returns to 1.0).
- **Busy minutes:** a reaction adds two cuts, so it is skipped when it would push the local cut rate (window `rate_window_s`) over `max_cuts_per_min` (8). That cap yields only to a hold already longer than `max_hold_s + reaction.max_s` (and the decision says `rate_override`); set `hard_rate_cap: true` to make it absolute. Holds left over are listed in `pacing.still_over_limit` with the reason (`within_tolerance` = at most `max_hold_s + reaction.max_s`) and as `long hold` warnings.
- **Each insertion** is a video segment `reason: "pacing: reaction"` with a `confidence` (boundary kind, weaker for a punch-in); `work/cleanup.json` `pacing` holds `{enabled, inserted, by_mask, rate_overrides, still_over_limit, before, after, decisions}`, where `before`/`after` carry `holds_over_limit`, `avg_shot_s`, `cuts_per_min`, `longest_hold_s`.
- Deterministic and idempotent: the same assembly always gives the same rough cut; a track with no over-long hold comes back unchanged. `pacing.enabled: false` or `ave cleanup --no-pacing` skips it.
- **Tuning by ear:** too busy: raise `max_hold_s` (18-20), lower `max_cuts_per_min`, set `hard_rate_cap: true`. Too static: lower `max_hold_s` (10-12) and `target_hold_s`. Reactions feel late or early: `target_hold_s`. The cut rate of a whole cleanup is mostly set by the cutaway masks (`masks.cutaway_s`, `min_hold_s`), not by pacing.

Holds still longer than `pacing.max_hold_s` (or `masks.max_hold_s` when pacing is off) are reported as warnings.

## Outputs

- `edits/rough.vN.json`: the new EditDoc (dialogue = kept ranges with `xfade_ms`, `reason`, `confidence`; video = assembly camera choices mapped through `TimeMap`, then masks).
- `work/cleanup.json`: `decisions[]` `{id, type, e0, e1, action, reason, confidence, mask, review, cut}` (ids `c###` word-level, `p###` pauses, `n###` audio bursts, `o###` overrides), `cuts[]` (the actual splices: `e0`, `e1`, `snap`, `xfade_ms`, `context`, `mask`, `confidence`, `review`), `totals`, `warnings`, the resolved `policy`.
- `ave cleanup explain <ep>` renders that as text.

## Overrides: `work/cleanup_overrides.json`

An agent or human can add judgment without touching code. Honored on every run:

```json
{"cut":  [{"words": [212, 214], "type": "false_start", "reason": "...", "confidence": 0.8},
          {"e0": 431.2, "e1": 433.0, "type": "tangent", "reason": "..."}],
 "keep": [{"id": "c007", "reason": "..."}, {"e0": 431.2, "e1": 433.0, "reason": "..."}]}
```

`words` = `[first, last]` indices into `transcript.json` `words`; `e0/e1` select the words whose midpoint falls inside. An optional `text` must match the transcript or the entry is skipped (with a warning).
`keep` vetoes detected decisions by id or by overlapping time range. Detected ids are stable while the transcript and policy are unchanged.

## Limits

Cuts are as accurate as the transcript's word times. Overlapping masks assume the assembly's video and dialogue share the episode clock (music/overlays in the assembly are re-timed through the cuts on that assumption). Masks are not judged for taste beyond the rules above; look at the rough cut.

## Room tone (`room_tone.*`)

Cleanup writes `tracks.room_tone` into every rough cut so selects/clip timelines can have gaps between clips. It picks the quietest steady window of `min_s`..`max_s` seconds (default 2..4) from the dialogue source's analysis audio, near the span the edit uses. A window is rejected when it is digital silence (`min_floor_db`, -80), has a frame more than `steady_db` (4 dB) above or below its mean (a cough, speech with pauses), or sits within `margin_s` of the searched span's edge. `enabled: false` leaves the track empty (selects then butt-join and warn). The compiler level-matches each gap to the noise floor on both sides at render time.

## Zoom state (`masks.zoom_min_dwell_s`, `masks.max_ins_per_shot`, `pacing.zoom_min_dwell_s`)

One rule for every punch-in producer (`ave.zoomstate`): a zoom change on a camera comes at least `zoom_min_dwell_s` (8) after the previous one on that camera, and a shot is never punched deeper than `max_ins_per_shot` (1) levels without coming back out. A mask that would break it is skipped (cutaway/wide are tried first as always); if nothing else fits, the cut is a plain crossfade with `needs_review` and the reason names the limit. A pacing punch-in is a sustained reframe held to the end of the hold (or, in a hold longer than `max_hold_s`, back out at a sentence boundary at least the dwell later); `pacing.zoom_min_dwell_s: 0` restores the old 2-4 s blip, `masks.zoom_min_dwell_s: 0` the old alternate-on-every-cut. The validator (`zoomstate.validate`, run by `ave cleanup` and `ave final`) reports `short_dwell`, `in_out_in`, `multiple_ins`.

## Cuts in words (`pause.word_*_pad_*`, `snap.widen_*`, `snap.pad_decay_tol_db`, `checks.*`)

`cutwords` checks every dialogue splice on the source clock (`editdoc.src_time`) against the transcript: `checks.word_margin_s` (0.04, half an 80 ms ASR frame) is the tolerance for "inside a word", `checks.hot_db_below_speech` (12) with `hot_max_edge_pad_ms` (20) flags a cut at a word edge with speech continuing. To avoid clipping a tail, a cut next to a removed word may slide up to `pause.word_tail_pad_ms` / `word_lead_pad_ms` (80; `*_ghost_ms` 150 when the removed word is a low-confidence `ghost_max_ms` (160) word) into it, only to a real valley reached along a decay (`snap.pad_decay_tol_db`). `snap.widen_if_not_valley` (off) searches the whole allowed range for a quieter point instead.
