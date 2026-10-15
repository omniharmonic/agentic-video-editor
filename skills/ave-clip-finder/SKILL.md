---
name: ave-clip-finder
description: Find short-form clips (shorts, reels, teasers) in a transcribed episode and assemble them as selects for human review. Use after a rough or final cut exists (or at least an assembly) when the user asks for clips, highlights, shorts candidates, or "the best moments". You judge; the tools validate every sentence id and quote against the transcript.
---

# Clip finder: you judge, the code keeps you honest

You never type a time or invent a word. You refer to sentences by the ids the tool issued (`s000`, `s001`, ...) and
every quote you claim must exist verbatim inside the clip you pick. Anything else is rejected with the reason.

Prerequisites: `work/transcript.json` and an edit in `edits/` (`rough` is best: clips then exclude dead air and fillers).
Run `ave resume <ep>` if unsure. Raw footage is never touched; outputs go to `work/`, `edits/`, `exports/review/`.

## 0. Sessions (multi-session episodes)

If `ave resume` lists sessions (`s1`, `s2`, ...), each session is its own edit (`rough.s1`, `rough.s2`) and **every `ave clips`
command takes `--name <session>`**; without it the command stops and lists the sessions (it never picks one for you). Work one
session at a time: `prepare --name s1`, read `work/clips_transcript.s1.txt`, `propose --name s1`, and so on. Sentence ids
(`s000`...) restart in every session, so a pick is only meaningful for the session it was prepared in. Clips from all sessions
live together in `work/clips.json` with ids prefixed by the session (`s1-c01`, slug `s1-systems-beat-goals`); each records
the exact edit it was picked from (`source`: stage, name, version, fingerprint), and its selects are `select.s1-<slug>` plus
the combined `select.s1-selects`. `shorts` and `content` take clips from any session by id or slug, no flag needed.
Without sessions nothing changes (no `--name`, ids `c01`, `work/clips_candidates.json`).

**Stale picks:** if a session's rough was redone and you run `prepare --name s2` again, picks made from the older edit are kept
but marked `stale` (the output lists `stale_clips`). They cannot be approved or nudged (their sentence ids index the old
transcript table); reject them or re-pick the moment from the new `clips_transcript.s2.txt`. `build` skips stale clips and
lists them in `stale_skipped`; `--include-stale` builds them from the exact old edit. `ave clips list --name s2` shows `newer_edit`
when a later rough exists than the one a clip came from, which is your cue to decide whether to re-prepare.

## 1. Prepare the evidence

```bash
ave clips prepare <ep> [--name s1] [--stage rough|final|assembly] [--min-s 20 --max-s 60]
```

Writes `work/clips_candidates.json` (`clips_candidates.<name>.json` with `--name`; sentences with speaker, output-time start/end, features; the surfaced
sections; the show's exemplars) and `work/clips_transcript.txt`. **Read `clips_transcript.txt`, not the JSON**: first the exemplars (what the human kept and
skipped before, with reasons: learn the taste, do not copy the clips), then one line per sentence,
`s014  02:31.4  S1  text  [? mid ctx ~start ~end]` (`?` question, `mid` starts mid-thought, `ctx` refers to earlier context, **`~start` a clip cannot BEGIN on this
sentence** (lowercase, "and/so/but/because/which/like...", or it runs on from the sentence before), **`~end` a clip cannot END on it** (ends on "and", "the", a comma, a
cut-off, or runs on)), then the surfaced sections (an `INCOMPLETE (...)` suffix means no complete variant fit the target length).

**Recall first.** `prepare` surfaces EVERY section that scores at least the floor (`recall.floor` in `config/clips.rubric.yaml`, 30/100; `--floor N` to
override), spread across the whole edit, with a sanity cap (`recall.max_per_session`, 80; `--max-candidates`). If the cap bites, the output says
`truncated: true` and `windows_available`: tell the human, never present a truncated list as complete. Each section carries `score`, a `breakdown`
(weighted rubric points and penalties), `tags` (hook, story, claim, funny, tangent, quotable), `duration`, `hook_pos_s`, `ends_on_sentence`, a one-line `reason`,
and `alternates` (other in/out choices for the same region; the best of an overlapping cluster is listed, the rest hang under it). These come from cheap,
explainable heuristics; they are an ordering and a safety net, not a verdict. Your judgment is the second opinion, not the gate.

**Complete statements, not fragments.** The ASR's punctuation is unreliable (6% of words carry a full stop on the real session; chunks start in lowercase
mid-sentence), so `src/ave/segment.py` combines speaker turns, pauses, punctuation, capitalisation, discourse openers and dangling endings to decide where a
thought starts and ends. A window or pick that begins or ends inside a thought is **snapped OUT** to the nearest complete thought start/end (a little earlier or later,
stepping over a listener's "mm-hm"), as long as the result fits `--max-s`; it never cuts in. Each clip then says what it is:
`start_complete`, `end_complete`, `complete_statement` (both), `completeness_note` (why not), `expanded` (what moved, if your ids were snapped out: the ids in
`sentence_ids` are the expanded ones), and an incomplete clip is penalised (`start_incomplete` 12, `end_incomplete` 8). A clip whose first sentence opens on
"that's / it / they / this is..." with nothing in the clip to refer to gets `needs_context: true` and a `context_variant` (the same clip started one or two sentences
earlier); the variant is offered, never applied.

Defaults: 20-60 s clips (a hard limit of 12-90 s lives in `config/clips.rubric.yaml`). Ask the human the target length and
platform if they have not said ("shorts" = 30-60 s, teasers = 15-30 s).

## 2. Judge (this is your job)

Read the whole transcript once, then judge the surfaced sections and anything the heuristics missed. There is no "pick 5": pick every moment you can
vouch for (typically 8-15 on a 100-minute session, more if the material is rich). The tool will show the human the other surfaced sections too, so do not
pad your picks to be safe; your picks are the ones you stake a score on. For each, score 0-10 on every criterion in
`config/clips.rubric.yaml` (weights in parentheses): **hook** (25: the first 3 seconds make a stranger stay; a claim,
question, or tension, never "so, um, today"), **arc** (20: setup, turn, payoff inside the clip), **insight** (15: a specific
idea, number or story), **emotion** (15: laughter, conviction, vulnerability), **shareability** (10: would someone send it
standalone), **ending** (10: lands on a finished thought or punchline), **speech_quality** (5: few stumbles).

Rules of judgment:
- Be a hard grader. Most moments are 4-6. An 8+ hook is rare; use the full range or the ranking is meaningless.
- The clip must make sense to someone who has not heard the rest. If it opens with "that", "it", "as I said", set
  `"needs_context": true` (a penalty) or choose a start sentence that stands alone. Set `false` only if you checked it does.
- Start on the strongest sentence; skip warm-up. End on the sentence that completes the thought, not mid-argument.
- **Judge completeness yourself, too.** A clip must be a whole statement a stranger can follow. Do not pick a range that starts on a `~start` sentence or ends on
  a `~end` one: begin at the sentence where the thought starts (the tool will snap you out if you do not, but it can only go as far as `--max-s`). Read the first
  sentence alone: if it leans on "that/it/they/as I said", start earlier or set `needs_context: true`. After `propose`, check `complete_statement` and
  `completeness_note` on every clip and tell the human about the ones that are still incomplete.
- Flag `"sensitive": true` for health, legal, finances, private third parties, anything the host might regret.
- Keep one idea per clip. Do not stitch non-adjacent sentences; a clip is one contiguous run (list every id first to last).
- Prefer variety: different topics and speakers across the set. Clips may not overlap.
- `hook_quote` (the opening line you are betting on) and `end_quote` are optional but valuable: they are checked verbatim
  (case and punctuation ignored) and fail the pick if wrong. Copy them from the transcript text, never from memory.

Write `picks.json`:

```json
[{"title": "Systems beat goals", "hook": "Claims goals are wishes and systems are habits",
  "hook_quote": "a goal is a wish and a system is a habit",
  "sentence_ids": ["s005", "s006", "s007", "s008"],
  "scores": {"hook": 8, "arc": 6, "insight": 8, "emotion": 5, "shareability": 8, "ending": 7, "speech_quality": 7},
  "rationale": "Counterintuitive claim, then a concrete example, ends on the takeaway.",
  "needs_context": false, "sensitive": false}]
```

## 3. Validate

```bash
ave clips propose <ep> --picks picks.json [--name s1]   # your picks, validated, PLUS every other surfaced section not overlapping them
ave clips propose <ep> [--name s1]                      # no picks: every surfaced section, scored by the heuristics (origin: heuristic)
ave clips propose <ep> --picks picks.json --picks-only  # only your picks (the old behaviour; use when the human asked for a short list)
```

Clips you did not pick are `origin: heuristic` (their `inputs.scores` are the heuristic 0-10 values; say so when you report). A section the human already
skipped is not proposed again by the automatic fill. Human-decided clips (kept, skipped, or maybe) survive a re-run.

### Tighten suggestions (read them, report them, never apply them)

Every clip carries `tighten`: a list of conservative suggestions for making it shorter without hurting it, each
`{start, end, kind, text, saves_s, reason, confidence}` (`start`/`end` in EPISODE seconds, plus `o_start`/`o_end` in the clip timeline's own seconds), and `tight_duration`
(its length if all were applied) with `tight_text` (what it would read like). Kinds: `filler` (fillers and runs of hedging: "you know, I mean, kind of"), `repeat` ("to to"),
`restart` (an abandoned clause), `aside` (a listener's "mm-hm" in the middle of the speaker's thought, or a comma-bounded "which ..." clause), `tangent` (a sentence that
announces itself as an aside, or housekeeping chatter such as "sorry, hold on"). Safety rules the code enforces: whole words only; never inside the hook or the payoff
(no sentence- or clause-level cut in the opening, hook, last or end-quote sentence; no cut at all inside `hook_quote`/`end_quote`); every seam must still read (no stutter,
no "the is", no sentence that now starts on "and"); all together at most 35% of the clip and never under the hard minimum. **They are suggestions for the human's trim
decision; nothing is applied to the clip, the select or the timeline.** When you report a clip, mention its biggest suggestions ("tightens 41s to 36s: drops a
'sorry, my dog' aside and a hedging run"). `context_before` / `context_after` hold the two sentences either side so the human can see what is missing.

Exit 0 = all accepted; exit 2 = some rejected (the accepted ones ARE saved). Read `rejected[*].problems`, fix those picks, and
run again (undecided proposals are replaced; human decisions are kept and block overlaps). Common rejections: unknown or
non-contiguous ids, a quote that is not in the clip, length outside the limits after snapping, overlap, score below the
minimum (the floor, 30), missing or unknown criterion. Boundaries are snapped for you: the start to the first spoken word (leading
fillers trimmed, 0.1 s pre-roll), the end to the last word plus 0.3 s, never into a neighbouring word. Check `warnings` and
`penalties` in the output; report them honestly rather than arguing the score up.

If you see "stale": the transcript or edit changed. Re-run `prepare`, then re-pick.

### How many clips: top N and the cut-off score (a view, nothing is deleted)

Every clip has a `rank` (1 = best within its session) and a `total` (the score). The human chooses how many to look at, or where to stop:

```bash
ave clips propose <ep> [--picks picks.json] --top 20 --min-score 55   # reports how many sit above the line; ALL clips are still written to clips.json
ave clips list <ep> --top 20 --min-score 55                            # best first, with a "--- cut line ---" row where the line falls
ave clips build <ep> --top 10                                          # build only what is above the line
```

`--top N` keeps the N best per session, `--min-score S` those scoring at least S; with both, a clip must satisfy both. `recall.top_n` / `recall.min_score` in
`config/clips.rubric.yaml` (or the episode's `policy/clips.rubric.yaml`) set the default; the flags override it. The cut-off is only a **view**: nothing is deleted or
rewritten, a clip below the line can still be reviewed, approved or built, and lowering the line later brings it back. It is separate from `recall.floor` (`prepare --floor`),
which decides what is surfaced at all. When the human says "give me the top 15" or "nothing under 60", run it and read them the cut line.

## 4. Build and review (CHECKPOINT: stop for the human)

```bash
ave clips build <ep> [--name s1]              # select.<slug>.vN EditDocs + select.selects.vN (all clips with a gap between)
ave clips previews <ep> --name s1 [--top 20] [--ids a,b] [--pad 8] [--force]   # play-in-context previews (below); do this BEFORE the human reviews
ave clips review <ep> [--name s1] [--drafts]  # exports/review/index.html (open it; exports/review/s1/index.html per session); --drafts renders proxy videos if ave.compile exists
```

**Previews first.** The human should hear a clip with its lead-in and tail, not read a transcript. `ave clips previews` renders, for every clip that is not
rejected (or `--top N` / `--ids`), a 540p H.264+AAC file of the clip plus `--pad` seconds (default 8) before and after, clamped to the edit, cut from the clip's
own source edit (`rough.<name> vN`, with the cleanup applied, exactly as the clip would be) and loudness-normalised, into `work/clips/previews/<id>.mp4`;
`work/clips/previews/index.json` says where the real clip starts and ends inside each (`clip_in_s`/`clip_out_s`) so the UI dims the lead-in and tail. When a
clip has `tighten` ranges it also renders `<id>.tight.mp4` (those ranges removed, crossfaded; another camera covers a jump cut). It prints a size/time estimate
first, refuses a too-full disk, runs 3 renders at once, skips fresh previews (`--force` redoes them), verifies every file with ffprobe and exits 2 listing any
clip that failed. Run it with `--top` first on a long episode (about 0.2 s of wall time per preview second on a 4K HEVC edit; `ave proxies` makes it faster).
Say how many rendered, how many failed and why. Footage is only read.

Tell the human to open `exports/review/index.html` (works from file://, no network). It lists ALL surfaced sections, sortable (score, time, length) and
groupable (tag, score band, decision), each with three frames, tags, hook position, score bars and penalties, the one-line reason, the alternates, and the
transcript with in/out handles. They press **Keep / Maybe / Skip** (keys `y`/`m`/`s`), tick reason chips (weak ending, hook late, off topic, too long, ...),
optionally move a boundary (click a sentence's `in`/`out`, or "use this in/out" on an alternate), then **Download decisions**. Do not decide anything yourself
and do not publish or post. Then ingest what they downloaded:

```bash
ave clips import-decisions <ep> ~/Downloads/clip-decisions.json [--name s1]   # page export -> clips.json status + the learning ledger
ave clips decide <ep> <clip-id> --verdict keep|maybe|skip [--reason weak_ending --reason hook_late] [--note "..."] [--name s1]   # terminal, one clip
ave clips decide <ep> decisions.json [--name s1]                              # older approve/reject/nudge files still work
ave clips build <ep> --approved-only [--name s1]    # rebuild selects for approved clips only (new versions, nothing overwritten)
```

`keep` = `approved`, `skip` = `rejected`, `maybe` stays `proposed` with `maybe: true` (it is not built with `--approved-only`). Every verdict is
appended to `work/learning/clip_decisions.jsonl` (the ledger: latest line per clip wins, history stays) together with the clip's features. Reasons come from
`config/clip_reasons.yaml` (extend it per episode in `policy/clip_reasons.yaml`). Approved clips (`work/clips.json`, `status: approved`) are the input to shorts.

## 5. Learn the show's taste (human-owned)

```bash
ave learn taste report <ep>     # my top-N by score vs what you kept/skipped, reasons, keep rate per tag
ave learn taste suggest <ep>    # explainable weight changes from per-feature keep-rate lift; needs >= 40 decisions, else says so; changes nothing
ave learn taste apply <ep>      # HUMAN ONLY: writes the weights to taste/clips.yaml (--dry-run to preview)
```

Run `report` at the end of a clip session and tell the human where your ranking missed (`buried_keeps`, `high_scored_skips`). You may run `report` and
`suggest`; **never run `apply`** (and never edit `taste/clips.yaml` weights): changing the rubric is the human's call. The profile is per show
(`$AVE_TASTE_DIR`, else `<workspace>/taste/clips.yaml`, else `~/.config/ave/taste/clips.yaml`); its `weights` override `config/clips.rubric.yaml`, and
its `exemplars` (plus the recent ledger decisions, capped) appear at the top of the next `clips_transcript.txt`.

## Failure modes
- "This episode has sessions s1, s2: pass --name": add `--name <session>` (see section 0).
- `No transcript yet`: run `ave transcribe`. `No rough edit`: use `--stage assembly` (clips will contain the dead air) or run cleanup first.
- Few surfaced sections: relax `--min-s/--max-s` or lower `--floor`; short or plain episodes legitimately have few clips.
- Build warning "no tracks.room_tone": the gaps between selects are skipped (clips butt-join) because gaps are filled from room tone. `ave cleanup` now fills `tracks.room_tone` automatically; re-run it (or add a range by hand) and rebuild with `--force`. Gaps are level-matched to the noise floor around them when rendered.
- A clip is `complete_statement: false`: no complete start/end fit within `--max-s` (a speaker can chain "And ... And ..." for a minute). Offer the human a longer `--max-s`, a nudge, or the `alternates`; do not argue the score up.
- Drafts skipped: `ave.compile` is not available yet; the review page works without video.
