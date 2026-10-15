# Opening and closing sequences

Skill: `skills/ave-intro-outro/SKILL.md` (the agent playbook). This page is the machinery reference.

```
ave sequence plan    <ep> [--name s1] [--policy f.yaml] [--cold-open-clip ID | --cold-open-quote TEXT | --cold-open-range O0 O1]
ave sequence preview <ep> [--name s1] [--only opening|closing]      # 540p draft + a still per item
ave sequence build   <ep> [--name s1] [--preset master] [--burn] [--force]
ave sequence approve <ep>                                            # HUMAN ONLY
ave sequence status  <ep>
ave final <ep> [--opening|--no-opening] [--closing|--no-closing] [--draft-sequences]
```

## What it builds

| Opening | Closing |
|---|---|
| optional **cold open** (10 to 30 s hook from an approved clip, a quoted passage or an output-time range of a cut; captioned; optional ducked music bed) | **thanks** card (links from `episode.json show.links`), optional second **cta** card |
| **title card** (3 to 6 s; animated show mark, episode number and title) | **credits roll** (hosts, guests, editor, music and asset credits from the license log, links; even whole-pixel scroll, edge fade) |
| **host card** (names with titles, default "Co-host") | **end card** held `hold_s` (default 20 s) for YouTube end screens |
| optional **sting** at the title, optional bed | **tail room tone** (default 2 s) |

## How it is rendered

* Each sequence is ONE EditDoc rendered by `compile.render`: a flat brand plate (token color) as the video source, motion-kit cards from `motion-kit/sequences/` as alpha
  overlays (ProRes 4444, the same Chromium path as `ave motion`), the cut's `tracks.room_tone` under the cards (silence when the cut has none), music/sting as license-gated
  music items (ducked by the compiler under speech), and, for a cold open, the hook's own segments in front. So canvas, fps, audio rate and verification are the body's.
* **One gain domain.** The body's loudness pass is a linear gain. Sequences are rendered with that same gain (`compile` preset key `fixed_gain_db`; cached per cut in
  `work/sequence/gain.*.json`) instead of being normalised on their own, so a card-only closing is not boosted to speech level and a cold open speaks at the body's level.
* **Programme join** (`sequence.assemble`): picture by stream copy (track time scales are unified first; the body is never re-encoded), audio in float with an equal-power
  fade-out/fade-in pair of `audio.join_fade_s` (0.2 s) meeting at each join (a fade through the room tone, not an overlapped crossfade: a sequence has no audio beyond its ends),
  one loudness pass to the preset, measured on the encoded file; frame count, fps, canvas and duration verified.
* **Captions.** Cards are uncaptioned; the cold open is captioned (planned SRT, burned with `--burn`). `captions/final.programme.srt` = cold-open cues + the body's cues shifted by
  the opening's exact length (`sequence.srt_shift`). The shipped SRT is still the re-transcription of the joined file.
* **Approval.** `plan` lists every item needing approval (derived text, host titles, links, editor, the cold open) and a content hash; `approve` (human) binds to the hash; `ave final`
  refuses a plan that is unapproved or changed since. `--draft-sequences` overrides and marks `doc["sequences"]["draft"]`.
* **Resolve.** `handoff.plan` calls `sequence.apply_to_handoff_plan` when the final EditDoc carries a `sequences` block: the opening is clip 1 on V1/A1 at frame 0, everything of
  the body shifts by the opening's frames, the closing follows; the SRT is the programme SRT. (Placement is tested against the in-memory fake bridge; not run in live Resolve.)

## Data

| What | Where |
|---|---|
| Style, durations, on/off, templates | `config/sequences.default.yaml` < `~/.config/ave/sequences.yaml` < `<project>/policy/sequences.yaml` < `--policy` |
| Whether `ave final` uses them | `config/final.default.yaml` `sequences: {opening, closing}` (same override chain, `final.yaml`) or the flags |
| Show name, tagline, logo, hosts, links; number; episode title; guests; cold-open pick | `episode.json` (`show`, `number`, `episode_title`, `guests`, `sequences.cold_open`) |
| Editor credit, "edited with ave" | policy `closing.credits.editor`, `edited_with_ave` (off by default) |
| Asset credits | `work/assets/licenses.jsonl` for everything the final EditDoc and the sequences use |
| Plan, build report | `work/sequence.json` (`sequence.<name>.json`), approval in `work/sequence.approval.json` |
| Files | `work/sequence/<tag>/{opening,closing}.<preset>.mp4`, `*.doc.json`, cards under `work/sequence/cards/` |
