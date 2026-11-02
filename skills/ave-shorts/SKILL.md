---
name: ave-shorts
description: Turn approved clips into vertical 1080x1920 shorts (YouTube Shorts, Reels, Facebook Reels, X) with face-aware 9:16 framing, karaoke captions in the safe zone, -14 LUFS audio and publish sidecars. Use after `ave clips decide`/`build --approved-only`, when the human asks for shorts or reels, or to re-frame or re-export one.
---

# Shorts: approved clip to vertical export

Shorts always burn captions, so they need an FFmpeg with libass (macOS: `brew install ffmpeg-full`; `ave doctor` checks).

Needs: `work/clips.json` with approved clips built into `edits/select.<slug>.vN.json` (skill `ave-clip-finder`), `work/transcript.json`.
Nothing here publishes. Raw footage is never touched; outputs are `edits/short.<slug>.vN.json`, `captions/short.<slug>.vN.*`, `exports/shorts/`.

## Hard guarantees (creator feedback 2026-10-14, docs/field-notes/2026-10-14-shorts-feedback.md)

1. **Complete statements only.** A short starts and ends on a complete sentence or phrase. `ave shorts build` checks the clip's `start_complete` / `end_complete` /
   `complete_statement` AND re-judges the edges on the real words of the edit it was picked from (`src/ave/completeness.py`). An incomplete clip is cut from its
   `context_variant` when that is complete (sidecar `completeness.boundary: context_variant`), else REFUSED with every reason. A stale clip, or one that predates
   the completeness logic (no fields), is refused too. Fix it by picking a clip that passes (`ave clips list`), not by overriding. `--allow-incomplete` is the explicit
   override, only when the human accepts a mid-thought edge; it is recorded in the doc, the sidecar (`completeness.override: true`) and PROGRESS.md. The UI can never pass it.
   `ave shorts export` refuses a short with no completeness record or an unrecorded failure. Tune in `config/shorts.yaml` `completeness`.
2. **The cut never slices a word.** The first and last cut are moved to word gaps from `work/transcript.json` (`pad_start_s` 0.10 before the first word, `pad_end_s` 0.18 after
   the last, never past the middle of the gap). The sidecar `edges` says what moved (`was_slicing`); `completeness.cut.interior_slices` and `needs_review` list any cut INSIDE the
   clip (from the rough cut) that still falls inside a word. Look at those.
3. **A short opens with a title, never the quote.** `--opening none|title_card|title_overlay` (default `title_overlay`, `config/shorts.yaml` `opening`):
   - `title_overlay`: a lower-area title panel (show name + topic title) over the first ~2 s of the clip, above the captions. No extra time.
   - `title_card`: a ~1.4 s full-frame title screen, then the clip. Adds that time (silent lead-in; captions start with the clip).
   The title is a DRAFT the human edits, plain topic wording ("Why we are making this show"), written from the clip. Source order: `--title`, `work/shorts/titles.json`
   (`{"<slug or clip id>": "..."}`), then the clip title. It must pass the voice critic (`package.postcopy.critique`: no hype, no emoji, no exclamation, no question) and must not be
   the speaker's own words (a quote on screen before or while it is spoken is what the creator rejected). A title that fails leaves the short without an opening and says why in
   `needs_review`. Write the titles yourself for the shorts you build, then tell the human they are drafts (sidecar `opening.title`, `opening.title_source`, `opening.edit`).
   Brand colours and fonts come from `motion-kit/brand/tokens.json` (template `short-title`).
4. **Quote cards are not openings.** The `quote-card` motion is its own product: `ave motion quote-reel` (looping cards, no speaker audio, docs/motion.md). A short never gets
   one (`opening.refuse_quote_cards`; export refuses it).

## Commands

```bash
ave shorts build <ep> --clip <slug>            # or --all-approved: select -> short EditDoc (new version, idempotent)
                                               # multi-session: clips of every session are found without a flag (slugs and ids are
                                               # prefixed `s1-`); add --name s2 to narrow --all-approved to one session
ave shorts preview <ep> <slug> --t 12          # PNG to LOOK at (see below)
ave shorts export <ep> --clip <slug> [--platforms youtube,reels,facebook,x]   # render + verify + sidecars
ave shorts list <ep>
```

Options (all opt-in; the defaults are unchanged):
- `--opening none|title_card|title_overlay`, `--title "..."`, `--allow-incomplete` (build): see the hard guarantees above.
- `--allow-unapproved` (build/export, with `--clip`): TRIAL short from a proposed clip, only when the human said to. The select is cut on the
  fly (clips.json untouched), files go to `exports/shorts_drafts/` and sidecars say `draft: true`; they are never in the publish folder.
- `--layout stack`: both speakers on screen, one above the other (1080x960 each), captions on the seam. For crosstalk-heavy clips where
  cutting every second would flicker. `--layout audiogram`: waveform card for clips with no usable picture.
- `--cutaways speaker` (or `build.cutaways: speaker`): keep the talker on screen instead of the rough cut's listener reaction shots.
- `ave motion place <ep> --template lower-third --props ... --at S --dur S --stage short --name <slug>`: a graphic on a short (new version;
  re-export). In portrait the lower-third sits above the caption line.

### Polishing a finished short (new versions, never in place)

```bash
ave shorts recut <ep> --clip <slug> --drop-start-words 7 [--drop-end-words 10 | --trim-start S --trim-end S] --dry-run   # first: which words, is each edge a complete statement?
ave shorts recut <ep> --clip <slug> --drop-start-words 7                                                                # then for real (add --by NAME)
ave shorts retitle <ep> --clip <slug> --title "Why finishing is hard"                                                    # swap the opening's title overlay only
ave broll place <ep> --stage short --name <slug> --asset a_... --at 4 --dur 2   # licensed B-roll over the speaker (skill ave-broll-music); proposes first
```

- `recut` moves a start or end INWARD on whole words (seconds are snapped to word edges). It cuts a new `select.<slug>` version from the exact edit the clip was picked from and builds the
  short through `build_short`, so the complete-statement gate, word-gap snapping, captions and the opening all run again: a trim whose new first or last words are not a complete statement
  is REFUSED with both edges quoted (`--allow-incomplete` is the same logged override as for `build`). `--dry-run` shows the new range, first/last words and verdict and writes nothing.
  The approved clip in `work/clips.json` is NOT changed (the human approved that range): the new short is flagged `needs_review` ("recut by ...: the human has not seen this range") in the doc
  and the sidecar until they look at it. To start EARLIER or end LATER, nudge by sentences (`ave clips decide` with a nudge), then `ave shorts build --force`.
- `retitle` re-renders only the title overlay; crops, captions and timing are copied. The title passes the same critic as at build time (no hype, not the speaker's words, one short topic),
  and `work/shorts/titles.json` is updated so `build --force` keeps it. It refuses a short built with `--opening none`.
- Both write a new `short.<slug>.vN` (re-export it: new files, the old exports stay). Neither renders, posts or approves.

### To the studio

Approved shorts go to the studio with `ave studio ingest shorts <episode> [--clips ids] [--accounts ids] [--dry-run] [--sync]` (skill `ave-studio-recipes`, recipe `episode-shorts-to-posts`).
It takes only clips a NAMED human kept from a terminal or `ave ui` (a keep typed through an agent shell, `via: notty`, does not count), only exports that are not drafts, match their recorded
sha256, pass limits/encode/licenses, are complete statements and are the latest cut. Export every platform the studio's accounts post to (`--platforms reels,youtube,facebook,x`).

`build` does: 1080x1920 canvas; per camera, Apple Vision finds the face on sampled frames and one static 9:16 crop is computed
(eyes at the upper third, head-and-shoulders size, medians so it does not jitter); shots under `build.min_hold_s` (2 s) are shown from
the neighbouring camera at the same episode time (still in sync); karaoke captions (`short` style) are attached. Tune everything in
`config/shorts.yaml` (override per episode in `<ep>/policy/shorts.yaml`). Rough-cut punch-ins are kept and any remaining same-camera jump cut gets an alternating `build.jump_cut_zoom` punch-in.

## Judge it (you must LOOK before telling the human it is good)

0. Read `completeness` and `edges` in the build output: `complete: true`, `boundary` (clip or context_variant), no `override`; the first and last words (`completeness.audit.first_words` /
   `last_words`) form a whole sentence; `edges.start/end.was_slicing` shows which edge used to cut a word. Then view the FIRST frame of the export (a title card is the first frame; an overlay
   starts at 0.2 s, so also view 1.0 s) and one frame after the opening ends. Check the title is not the words being said and fits on one or two balanced lines.

1. `ave shorts preview` at the start of each camera's first shot and in the middle of the clip. Left panel: source frame, yellow = detected face,
   green = the crop. Right panel: the vertical result with the caption safe zone (red) and caption line (cyan). Check: whole face inside the crop,
   room above the head (not cut at the hairline), eyes in the upper third, nothing important (mic, hands) sliced awkwardly, the face is not
   under the caption line.
2. Read `needs_review` in the build output. Each entry is a decision the tool could not make with confidence:
   - `no face detected`: centred crop. Look at the frame; if the speaker is off-centre, set a different crop by editing a NEW version of the doc, or
     fix lighting/camera mapping. Do not ship unchecked.
   - `upscaled N.Nx (soft picture)`: that camera is low resolution (a 540p proxy as a camera, or a wide at 1080p). It will look soft. Prefer
     the other camera or tell the human.
   - `face detection unavailable`: Apple Vision (pyobjc-framework-Vision) is missing; every crop is a centred fallback.
3. Read `notes` (shots re-assigned by min hold, `listener cutaway(s)`: reaction shots of the person NOT talking, 2+ in a 30 s short reads as flicker: consider `--cutaways speaker`) and `crop_motion` (per camera: `distinct_crops` 1 and `max_centre_move` 0.0 = no jitter) and `caption_problems` (`reading_speed` on a fast talker is real; report it).
4. Frames from the export too (5 per short: first frame, quarters, last; and run Vision on a 1 fps dump to prove the face is in frame every second): `ffmpeg -ss 8 -i exports/shorts/<name>.mp4 -frames:v 1 f.png`. Captions must sit mid-lower frame, clear of the bottom 25%.

## Export results

`ave shorts export` exits 2 if any platform has `problems`. Read each sidecar `exports/shorts/<slug>.vN.<platform>.json`:
`limits.ok` (duration/size/1080x1920; limits in `config/shorts.yaml` are UNVERIFIED, confirm before relying), `loudness` (target -14 LUFS, true peak under -1 dBTP; a
value within 1 LU is normal for peaky speech), `encode.problems` (H.264 High, yuv420p, BT.709), `licenses` (music/B-roll must be in the license log, else `ok: false`
and the file must not be posted), `needs_review`, `description` (a DRAFT built from the hook: improve it).
Each sidecar also carries `completeness`, `edges` and `opening` (mode, title, title_source, draft: true). `licenses` covers music, other licensed files and B-roll placed from a studio library
(`library_assets`, `derived_from`, `attribution`, `attribution_required`): an unlicensed or unattested placed asset blocks the export with a message naming it. Files are named `<slug>.v<doc version>.<platform>.mp4`; an unchanged doc is not re-rendered, a changed doc makes new files.

## Checkpoint

Show the human the preview PNGs and one burned frame per short. Wait for approval before `ave publish queue` (skill `ave-publish`).
Never post, and never queue from sidecars with `limits.ok: false`, `licenses.ok: false` or unresolved `needs_review`.

## Real-episode lessons (2026-10-14, docs/field-notes/2026-10-14-shorts-real.md)

Clip ids can change after a re-propose: check `ave clips list` before naming a clip. An export that dies in ffmpeg may have been killed by
another process: verify outputs with ffprobe (frame count vs sidecar duration) before trusting or re-running. Drafts are versioned
(`<slug>.v<N>.<platform>.mp4`); old versions stay as evidence, so list `shorts_drafts/` by the highest version.
