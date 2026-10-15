# Field notes: clips that start mid-sentence (2026-10-14, workstream K)

> **Working note from the first real episode.** This is internal history kept as evidence: what we assumed, what a real recording showed, and what changed. It describes one project (a two-host, two-camera podcast), uses its real numbers, and was written while the tools were still changing, so commands and defaults may have moved on. For the current behaviour read the skill or doc a note points to. See [docs/README.md](../README.md).

Source: the creator's review of the real 99 session-1 clips (first real episode, `rough.s1.v2`, 103 minutes). Their feedback, paraphrased: some of the clips start
mid-sentence, which does not work, so the agent needs better contextual understanding to pull clips that are complete statements (with suggestions for which portions to cut
out to make a clip tighter); and it should be easy to choose how many candidate clips to extract, or the score threshold at which to cut off.

## N23. The proposer never asked "is this a thought start?"  (status: applied, workstream K)
- Assumed: windows aligned to the sentence table were complete statements. The table splits on speaker change, a long pause or terminal punctuation, and `starts_mid_thought`
  looked only at the first word against a short list, as a 5-point penalty that a good hook easily outweighed.
- Found (read-only copy of `work/clips_candidates.s1.json` + `clips.json` + the rough edit, 1416 sentences):
  - Only **6% of ASR words carry terminal punctuation** (988 of 15,654). The ASR punctuates short chunks, and 463 of 1,416 table sentences start in lowercase: chunk
    remainders after a speaker turn ("...and I | mm-hmm | continued to walk...") or after a forced split of a long unpunctuated run (`_split_long` cut at the largest pause, which
    is often right after "and" or "the").
  - Speakers chain sentences with "And ...", "So ...", "But ..." for a minute at a time, and diarization leaves one-word blips from the other speaker inside a sentence.
  - **Before: 36 of the 99 clips (36%) started mid-sentence** (28% of the top 25 by score), measured with an independent raw-word test (first letter lowercase, first word
    "and/but/so/because/which/then/or/also/plus/cuz/since/like", or the speaker runs on from the previous word with no full stop and under 0.8 s). The creator was right.
    Ends were fine (0 of 99 ended on a dangling word): windows were chosen to end on a terminal-punctuated sentence.
- Change:
  - `src/ave/segment.py`: one boundary judge combining speaker turns, pauses, terminal punctuation, capitalisation (only if the transcript's capitalisation is informative:
    an all-lowercase transcript is not penalised), discourse openers, dangling endings (and/the/but/so, prepositions, auxiliaries, a comma, a cut-off dash, a lowercase run-on),
    and an unresolved-referent check for the first sentence ("that's", "it", "they", "this is", "as I said"). Lists and numbers live in the rubric's `completeness` section.
  - The sentence table no longer splits a hesitation inside a clause ("And And [1.5 s] behind the counter": dangling word, resumed in lowercase, `completeness.merge_gap_s` 2.5 s)
    and chooses forced splits of long runs by pause + comma + capital, never right after a dangling word. (This changes sentence ids: a re-`prepare` marks old picks `stale` by design.)
  - **Snap OUT, never cut in**: a window or pick whose start/end is not a complete thought is expanded to the nearest one (up to 8 sentences back / 4 forward, stepping over the other
    speaker's "mm-hm"), only if the result still fits `--max-s` and does not run into another clip. Agent picks are expanded too and say so (`expanded`). One that cannot be completed is
    kept with `completeness_note` and a penalty (`start_incomplete` 12, `end_incomplete` 8; they replace the old 5-point `starts_mid_thought`). Of several overlapping windows, a complete
    statement within 15 points of the best wins the slot.
  - Clip fields (round-2 contract): `rank`, `total`, `complete_statement`, `start_complete`, `end_complete`, `completeness_note`, `context_before`, `context_after`, `tighten`,
    `tight_duration`; additions: `needs_context` (+ `needs_context_reason`, `context_variant`: the same clip started 1-2 sentences earlier, offered not applied), `expanded`, `tight_text`, and
    `o_start`/`o_end` (clip-timeline seconds) on each suggestion.
- Measured **after** (same session, same `--min-s 20 --max-s 60`, the five real picks mapped onto the new table, then recall fill; 94 clips):

  | | before (stored) | after |
  |---|---|---|
  | clips | 99 | 94 |
  | start mid-sentence (independent test) | **36 (36%)** | **5 (5%)** |
  | ...of the top 25 by score | 7 (28%) | **0** |
  | end mid-thought | 0 | 0 |
  | `complete_statement` | not tracked | 89 of 94; the other 5 are listed with the reason (no complete start fits in 60 s) |
  | opens on an unresolved referent | not tracked | 9 flagged, 5 with an expanded variant |
  | mean length | 38.8 s | 38.3 s |
  | prepare + propose, real session | 7.5 s + 0.5 s | 5.8 s + 0.8 s |

- Ten before/after examples (the transcript text is omitted from this public copy; each line says what kind of change it was; old = the stored clip that began mid-sentence, new = the clip that now covers the same moment):
  1. old `s1-c28` began on a half-word fragment (52 s) -> new clip opens on a full sentence (complete, 32 s)
  2. old `s1-c87` began mid-clause -> new clip opens on a short standalone sentence (complete, flagged `needs_context`)
  3. old `s1-c93` began inside a reported-speech aside -> new clip opens on the sentence that introduces it (complete)
  4. old `s1-c51` entered a sentence halfway through -> it now begins at the start of that sentence (complete)
  5. old `s1-c59` began mid-phrase -> new clip opens on a full sentence (complete)
  6. old `s1-c46` began mid-phrase -> new clip opens on a list-style sentence (complete)
  7. old `s1-c47` began mid-sentence -> the best window of that region now starts at the next whole sentence (complete)
  8. old `s1-c38` began mid-sentence -> new clip opens on a full sentence (complete)
  9. old `s1-c07` (an agent pick) starts on a connective ("So ..."): the thought starts at s234, 66 s before the end, over the 60 s target. Kept, flagged `complete_statement: false`, penalised -12, with the reason. Honest, not fixed.
  10. old `s1-c41` started mid-clause -> now begins at the next clause opener (one speaker chain of "And ..." sentences; flagged incomplete for the same reason as 9).
  A short joke opener (old `s1-c45`, 22 s) is kept but now carries `needs_context` and a variant that adds the preceding one-word sentence (+2 s).
- Tighten suggestions on the real 94 clips: 37 suggestions on 32 clips (repeat 18, aside 11, filler/hedging 7, tangent 1), 25.5 s of 3,596 s (0.7%). Deliberately timid. The first
  version suggested 153 (165 s): it called one-word diarization blips "interjections" (removing them would have broken sentences), called on-topic and joke set-up sentences "tangents"
  because they shared no content word with the rest of the clip, matched "anyway" at the END of a sentence as a tangent marker, and matched "hang on" inside a longer sentence that was not a housekeeping aside. Now: an interjection must be made of backchannel words (yeah, mm-hm, right...), a tangent must announce itself near the start or be housekeeping chatter
  (sorry / hold on / can you see), and word overlap is off by default (`tighten.tangent.use_overlap`).
- Selection: `ave clips propose|list|build --top N --min-score S`, config `recall.top_n` / `recall.min_score`. A VIEW over `rank`/`total`: nothing is deleted. On the real clips,
  `--top 20` shows 20 of 94, `--min-score 55` shows 25, both shows 20; `list` prints the `--- cut line ---` row where the line falls.
- Lesson: a penalty is not a gate. A 5-point penalty on "starts mid-thought" was easily outvoted by a good hook; the fix is to move the boundary to where the thought starts, and to
  penalise only what could not be moved. And measure against the creator's complaint with a definition that does not share code with the fix.

### Known gaps
- 5 of 94 clips are still incomplete: a speaker's chain of "And ..." sentences can exceed the 60 s target before reaching a complete start. No "cut in" fallback (a later start inside the window)
  is offered; `alternates` or a longer `--max-s` are the options.
- The unresolved-referent check is lexical (first words only). A sentence that opens on "It's ..." is flagged although it may be a fine standalone line; the human decides.
- Tighten cannot see meaning: a hedge run is removed only if the join reads, but "reads" is a word-pair check (no stutter, no "the is", no sentence starting on "and"), not a parse.
- Tighten times are episode seconds per the contract; applying them to a select is a later step (the UI/trim) and is not implemented here.
