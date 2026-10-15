# Field notes: creator feedback on the trial shorts (2026-10-14)

> **Working note from the first real episode.** This is internal history kept as evidence: what we assumed, what a real recording showed, and what changed. It describes one project (a two-host, two-camera podcast), uses its real numbers, and was written while the tools were still changing, so commands and defaults may have moved on. For the current behaviour read the skill or doc a note points to. See [docs/README.md](../README.md).

Source: the creator watched the four trial shorts in `06_EXPORTS/shorts_drafts/` (first real episode). Overall reaction: positive, with two changes asked for:
1. clips must ALWAYS start and end on complete sentences/phrases (the trial shorts were cut from old picks that predate the completeness logic, N23);
2. do not open a short with a motion-graphic QUOTE card and then have the speaker say the same quote; use a title treatment instead. Keep the quote-card motion as its own product.

## N24. Completeness was advice in the clip finder and a gate nowhere downstream  (status: applied)
- Assumed: `ave clips` snapping clips out to thought edges (N23) made shorts safe. Found: `ave shorts build` never read `complete_statement`, so a clip with
  `start_complete: false`, a stale clip, or a clip with no fields at all became a short. The trial shorts used picks from before N23.
- Change: `src/ave/completeness.py` is the gate, used by `ave shorts build` (and therefore export), the content quote picker (`content.sources.clip_source`, `plan`, `propose`) and the quote reel.
  - A clip is judged on its stored fields AND re-judged on the real words at its edges (`segment.Segmenter` over the words of the edit it was picked from), so a stored `true` that the words contradict is caught.
  - Incomplete + a `context_variant` that is complete: the variant is cut (`completeness.boundary: context_variant`, `variant_of` records what was added). Otherwise REFUSED, listing every reason.
    Stale and never-assessed clips are refused. `needs_context` alone only warns by default (`completeness.needs_context: variant` uses the variant instead).
  - `--allow-incomplete` is the one override: explicit, written to PROGRESS.md (`OVERRIDE --allow-incomplete`), the short doc and every sidecar (`completeness.override: true` + the reasons). The UI job runner lists it as forbidden.
  - Export refuses a short that has no completeness record (built before the gate) or an unrecorded failure; sidecars carry `completeness` (flags, audit first/last words, boundary, override) and `edges`.
  - Content: `plan` lists only complete clips (rest under `skipped_incomplete` with reasons) and only quote candidates that begin on a capital/non-opener and do not end dangling; `propose` rejects a quote that starts mid-sentence. A pull-quote may stop at a clause boundary (it need not end on a full stop); a dangling or comma end is refused.
- The cut itself: the first and last cut are snapped to word gaps (`pad_start_s` 0.10 / `pad_end_s` 0.18, never past the middle of the gap, from `work/transcript.json`), all tracks moved together. Interior cuts (from the rough cut) are reported, not moved:
  `completeness.cut.interior_slices` and `needs_review` if any lands inside a word. On the three real shorts below the edges were already in word gaps (the clip finder pads the same way), no interior cut sliced a word, so the guard did not have to move anything; the tests prove it does when it must
  (`tests/test_shorts_feedback.py`: a start 0.1 s inside "are" and an end 0.3 s inside "building" land in gaps with the pad, and a "thanks"/"you" slice is captioned whole).
- Evidence on the real episode: the gate was run on the current top 14 clips of both sessions (non-stale, proposed): all 28 pass; the independent whisper.cpp transcription of the exports begins and ends on whole sentences (below).

## N25. A quote card in front of a speaker who says the quote  (status: applied)
- Change: `ave shorts` opens with a title (`config/shorts.yaml` `opening.mode`: `none | title_card | title_overlay`, default `title_overlay`), template `motion-kit/templates/short-title` (brand tokens and fonts, Newsreader/Inter).
  - `title_overlay`: lower-area panel (show name + topic title), 0.2-2.2 s of the clip, above the captions (bottom edge at 55.5% of the height), no extra time.
  - `title_card`: opaque ~1.4 s title screen, then the clip. The clip's picture is held under the card; dialogue gets a muted lead-in; captions and the first word start when the clip does.
    Only for the crop layout (stack/audiogram fall back to the overlay and say so).
  - The title is a draft text: `--title`, else `work/shorts/titles.json`, else the clip title. It must pass the package voice critic (no hype phrases, emoji, exclamations, questions) and must not be the speaker's own words
    (the longest verbatim run of the title in the clip text is under 4 words or 70% of the title). A title that fails leaves the short without an opening and says why (`needs_review`, sidecar `opening.problems`); it is never silently replaced.
    The sidecar has `opening: {mode, title, show, title_source, draft: true, edit: ...}`.
  - A `quote-card` overlay on a short is refused at build and at export (`opening.refuse_quote_cards`).
- The quote card keeps its own product: `ave motion quote-reel <ep> --quotes quotes.json` (looping cards from verbatim, complete-start quotes; video only, `audio: none`; seamless loop; versions never overwritten). Documented in docs/motion.md, skills/ave-motion, skills/ave-shorts, AGENTS.md.
  Nothing in `ave shorts` ever added the old quote card (the earlier `reel.s1-topic.v1.mp4` was assembled by hand); the new guard makes that structural.

## Bugs found while doing it (each has a test)
1. **The title card's muted lead-in was captioned.** The first version made the lead-in an ordinary dialogue piece (the master audio before the clip, gain -120 dB); the caption/word mapping treats any dialogue piece as speech, so words spoken BEFORE the clip appeared as captions on the card (seen in the first card export, a few words of the preceding sentence at 1.0 s). Fix: the lead-in is marked `fill: "muted"` (fills are skipped by word mapping, the compiler plays it muted).
   `test_title_card_never_captions_or_plays_the_words_before_the_clip`. The three first card exports are `v2` of each short in `shorts_drafts/`: do NOT watch them (superseded by v3/v5, nothing was deleted).
2. First frame of the card showed only the show name (the title animated in after frame 0; a platform's thumbnail would be empty). The settle now starts before frame 0.
3. Snapping a start later than the first video shot trimmed the shot to zero length (invalid doc). The snap is now a real trim/extend on every track.
4. A transparent test stub that was opaque black (lavfi `color=black@0.0` has no alpha without `format=rgba`): the fixture hid the audiogram waveform. Fixed in `tests/conftest.py`.

## The three re-rendered shorts (trial, `--allow-unapproved`, drafts only, nothing approved or published)
Picked from the CURRENT non-stale proposed clips by rank, one each per slot, all passing the gate (stored flags and edge audit); both sessions:
| short | clip | rank / score | why |
|---|---|---|---|
| `s1-topic-a-c297` | s1-c297, 25.8 s | s1 #2, 69.5 | s1 #1 (c299, 72.5) opens on a pronoun with no antecedent and has no `context_variant`: complete but not a standalone start, skipped. c297 is the line the creator liked best, tight (25.8 s), starts on a sentence, ends on one. |
| `s1-topic-b-c298` | s1-c298, 46.0 s | s1 #3, 68.0 | the clearest statement of what the series is; starts on a "TLDR"-style sentence and lands on a closing sentence. |
| `s2-topic-c-c20` | s2-c20, 51.9 s | s2 #1 (tie with c21), 72.75 | top of session 2; c21 (same score) is the weaker, rambling 24 s. |
Note the ids changed again since the trial (a re-prepare re-numbers): the trial's `s1-c06/c08/c09` are stale; `ave clips list` before naming a clip.

Titles (my drafts, in `01_PROJECT/work/shorts/titles.json`; the human edits them): three short topic phrases, one per short.
Each short has both opening options as new versions, YouTube preset only (the other platform presets are byte-identical copies per `config/shorts.yaml`, run `--platforms` for them when wanted).

| file (in `06_EXPORTS/shorts_drafts/`) | opening | dur | LUFS / true peak | notes |
|---|---|---|---|---|
| `s1-topic-a-c297.v1.youtube.mp4` | title_overlay | 25.77 s | -14.5 / -2.0 | starts and ends on whole sentences (text omitted) |
| `s1-topic-a-c297.v3.youtube.mp4` | title_card (+1.4 s) | 27.17 s | -14.4 / -1.3 | same |
| `s1-topic-b-c298.v4.youtube.mp4` | title_overlay | 45.97 s | -14.1 / -1.4 | starts and ends on whole sentences (text omitted) |
| `s1-topic-b-c298.v5.youtube.mp4` | title_card (+1.4 s) | 47.37 s | -14.1 / -1.2 | same (v1-v3 of this slug carry an earlier title and the caption bug: skip) |
| `s2-topic-c-c20.v1.youtube.mp4` | title_overlay | 51.87 s | -14.4 / -1.4 | starts and ends on whole sentences (text omitted) |
| `s2-topic-c-c20.v3.youtube.mp4` | title_card (+1.4 s) | 53.27 s | -14.4 / -1.2 | same |
| `motion/quote-reel.first-episode.v1.mp4` | the quote reel, 4 cards x 6 s x 3 loops | 72 s | no audio | the quote card as its own product |

Verification (ffprobe, ebur128, frames viewed as images, independent whisper.cpp pass over the exports): all 1080x1920, 30 fps, H.264 High yuv420p, AAC 48 kHz stereo; frame counts equal `round(duration*30)` and match the sidecars; loudness -14.1 to -14.5 LUFS at -1.2 to -2.0 dBTP (inside the 1 LU tolerance; the limiter ceiling is -1);
the title-card audio is silent (max -90 dB) for the first 1.38 s and speech starts at 1.4 s; whisper (large-v3-turbo) hears the same first and last words as the sidecar `audit`. No short has a `needs_review` entry, a cutaway-flicker warning is in the build notes (2-5 listener cutaways each, default `build.cutaways: keep`, as approved before).

## Needs a human look
- The title wording (drafts) and whether the title belongs over the first 2 s (overlay) or before the clip (card). On c298 the face is low in the frame: the overlay panel touches the chin line.
- A card title can end with a one-word second line (a two-line card title whose second line is a single word); the template now balances lines, these files predate that.
- Reaction cutaways (2-5 per short) were left as in the shorts the creator approved (`keep`); use `--cutaways speaker` if they read as flicker.
- Captions run fast on c297 (31 chars/s on one cue): reported by the tool, not retimed.
- `needs_context` is `warn` by default: c299 style clips (opening on "That's ...") pass as complete but open on an unresolved "that"; consider `completeness.needs_context: variant`.
- Not done: interior (rough-cut) seams are reported but not moved if they sit inside a word; a clip that cannot be completed within the target length is refused rather than cut in.
