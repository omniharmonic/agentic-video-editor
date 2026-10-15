---
name: ave-intro-outro
description: Design, build and check the opening (optional cold open, title card, host introductions, sting/music) and the closing (thanks and calls to action, credits roll, held end card, tail room tone) of an episode as part of the final cut. Use when the user asks for an intro, outro, opening or closing sequence, credits, a cold open or hook, an end card for YouTube end screens, or before `ave final` when the show has (or should have) a repeatable open and close.
---

# Intro and outro: the show's opening and closing grammar

The sequences are the one place the show says who it is, so they are the creator's call, not yours. You propose, build drafts, look at them,
and the human approves. Everything is data: `config/sequences.default.yaml` (durations, templates, what is on/off, per-show override),
text in `episode.json`, asset credits from the license log. Nothing here publishes anything.

## 0. Rules that never bend

1. **The human decides the grammar** (step 1) before you build anything. A cold open gives away a moment; a CTA asks the audience for something: neither is yours to choose silently.
2. **Plan, look, approve.** `ave sequence plan` writes what will be built and every item that needs approval. **Approval is a human act**: ask them to run `! ave sequence approve <ep>`; you cannot, and `ave final` refuses an unapproved or changed plan (unless `--draft-sequences`, which marks the result a draft).
3. **No unlicensed music or sting.** Every file needs a license-log entry (`ave assets log-license`, skill `ave-broll-music`); the plan and the build refuse otherwise. Credits for what the cut uses are generated from the log, never typed.
4. **No new text that is not sourced.** The plan records where each string came from (`episode.json show.name`, policy, license log) and flags anything derived or assumed. Do not invent a host title, a link, or an editor name.
5. Never publish, post, or call a paid or music service from here.

## 1. Decide the grammar with the human (checkpoint)

Ask, and write the answers down (`ave log`; text goes into `episode.json`, style numbers into `<project>/policy/sequences.yaml`):

| Question | Where the answer lives |
|---|---|
| Show name, tagline, logo; episode number and **episode title** (the title card's headline) | `episode.json` `show.name`, `show.tagline`, `show.logo`, `number`, `episode_title` |
| Hosts with display names and **titles** (for example Co-host); guests | `show.hosts[]`, `guests[]` |
| Cold open: yes or no? If yes, which moment (10 to 30 s)? | `episode.json` `sequences.cold_open` = `{clip: id}` (an approved clip), or `{quote: {text, stage, name}}`, or `{range: {stage, name, o0, o1}}`; `ave sequence plan --cold-open-clip ID` writes it |
| Music or sting under the open and close? From which licensed file? | policy `opening.music/sting`, `opening.cold_open.music`, `closing.music` |
| Closing calls to action and links (at most 4); one ask per episode is plenty | `show.links[]`, policy `closing.thanks`, `closing.cta` |
| Editor name as it should appear; "Edited with ave" line (off unless they want it) | policy `closing.credits.editor`, `edited_with_ave` |
| End card: how long to hold for YouTube end screens (5 to 20 s) | policy `closing.end_card.hold_s` |
| Turn them on for this show | `config/final.default.yaml` `sequences: {opening: true, closing: true}` in `<project>/policy/final.yaml`, or `ave final --opening --closing` |

If they have no preference: offer title card + host card as the opening (about 8 s, no cold open), thanks + credits + end card as the closing, no music. Silence under cards is a valid choice; the cut's own room tone carries them.

## 2. Plan, then look

```bash
ave sequence plan <ep> [--name s1] [--cold-open-clip ID | --cold-open-quote "exact words" | --cold-open-range O0 O1 [--cold-open-stage rough]]
ave sequence preview <ep> [--name s1]       # 540p draft + a still of every item under work/sequence/<tag>/stills/
```

Read `work/sequence.json`: `errors` must be empty; read every `warnings` line aloud to the human. Then LOOK at each still (Read the PNGs) and apply the critic list.

### Critic list (judgment: the plan warns about some, you check the rest)

- **Opening length**: under about 45 s in total; a title card of 3 to 6 s; the host card 3 to 5 s. Viewers are waiting for the conversation.
- **Cold open**: 10 to 30 s; a complete thought that stands alone (check `source.excerpt`, start and end on sentence boundaries); a tease, not the episode's best line spent twice (do not pick the closing line); it must not need context the title card gives afterwards. Captioned, with a clean tail before the title.
- **Title card**: show mark first, then the episode number and title; the episode title is a name, not a sentence; nothing within the blue title-safe line's margin; legible over its plate.
- **Hosts**: names spelled as the people spell them; titles printed as they want them (Co-host, not "Podcaster"); at most 4 on a card.
- **Credits**: hosts, guests, editor, music and asset credits (from the log: confirm each licensed credit line reads correctly), links. Under about 75 s for the whole closing. Scroll is even and slow enough to read (about 60 px/s at 1080p).
- **End card**: held at least as long as the YouTube end-screen elements the human will add in Studio (5 to 20 s), type in the left third so tiles can sit on the rest, a slow progress line so the hold does not feel frozen.
- **Sound**: sequences are rendered at the BODY's gain (same loudness domain), so a cold open speaks at the body's level and cards sit on room tone at the body's noise floor; no step in level at either join. Fades of 0.2 s meet at each join. The plan warns when the cut has no room tone (cards then sit on digital silence: run `ave cleanup`); `ave final` warns when the body starts or ends with sound (the join fade would soften it: leave room at the edges with `ave trim`).
- **Taste**: restrained motion, no stock-footage intro, no cliche whoosh; if a sting, one short hit. If it feels corporate, take things out.

## 3. Approve (human), build, final

```bash
! ave sequence approve <ep>                        # the human runs this after reading the plan and the stills
ave sequence build <ep> [--name s1] --preset master    # optional: files at work/sequence/<tag>/{opening,closing}.<preset>.mp4
ave final <ep> --opening --closing [--name s1] --music ...   # builds (or reuses) the sequences, joins opening + body + closing
```

What `ave final` does with them: renders the body as usual, then joins opening + body + closing (picture by stream copy, the body is not re-encoded; audio joined in float with equal-power
fades, one loudness pass to the preset, measured on the encoded file), writes the planned SRT of the whole programme (cold-open cues, then the body's shifted by the opening's exact length),
and **re-transcribes the joined file**, so the shipped SRT matches what is heard including the cold open. Check the report's `sequences` block (`joins_s`, `part_lufs`, `planned_srt`) and `render.lufs`
(target -16 master / -14 youtube) and `true_peak_db` (at most -1). If you change anything in the plan after approval, the human must approve again.

`ave handoff <ep>` of that final puts the opening and closing on the Resolve timeline as clips (V1/A1) and shifts the body by the opening's length; the SRT it offers is the programme one.

## 4. Licensing notes (music and stings)

Same rules as `ave-broll-music` and `docs/asset-licensing.md`: log the license (plan, date, track URL) for the sting and for every bed; Content ID can claim even licensed library music, so
keep the invoice; CC BY needs the exact credit line (the credits pull it from the log: `ave assets log-license --attribution "..." --attribution-required`); NC licenses do not fit a monetized show;
ND forbids the fades and trims a sequence applies. Never download from a service to get one: the human supplies the file.

## 5. When something is off

- `no editor name`: ask the human; set `closing.credits.editor`. Do not guess.
- `credits line too long` / `props.x: N characters`: shorten the text; the layout is fixed.
- `cold open is N s, longer than ... max_s`: use `trim: [a, b]` on the pick, or a tighter quote.
- `the plan is not approved`: show the human `work/sequence.json` `approval.required` and the stills, ask them to approve.
- A rebuilt body changes the gain: sequences rebuild themselves (the plan hash and the body gain are checked).
