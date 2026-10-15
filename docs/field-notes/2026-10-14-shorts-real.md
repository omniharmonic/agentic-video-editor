# Field notes 2026-10-14: shorts on the real 4K episode (workstream F2)

> **Working note from the first real episode.** This is internal history kept as evidence: what we assumed, what a real recording showed, and what changed. It describes one project (a two-host, two-camera podcast), uses its real numbers, and was written while the tools were still changing, so commands and defaults may have moved on. For the current behaviour read the skill or doc a note points to. See [docs/README.md](../README.md).

Episode: the first real episode (`<episode-root>`), two 4K HEVC 30 fps cameras, master WAV. Nothing was approved by the
creator, so every short is a TRIAL (`--allow-unapproved`) written to `06_EXPORTS/shorts_drafts/` (never `exports/shorts/`, which is what
`ave publish queue` reads). Footage untouched; 1.8 TB free. Nothing published, queued, sent to Canva or Resolve.

## What was built

| clip | session | dur | layout | picture | notes |
|---|---|---|---|---|---|
| s1-c06 (topic A) | s1 | 36.7 s | crop | Maren (B1) + 2 listener cutaways to Tomas | v5 = + lower-third; `motion/reel.s1-topic.v1.mp4` = quote card + short |
| s1-c08 (topic B) | s1 | 25.7 s | crop | Tomas (A1) + 1 cutaway | |
| s1-c09 (topic C) | s1 | 23.9 s | crop, `--cutaways speaker` | Maren only, punch-in alternation | v1 (default) had 4 cutaways in 24 s |
| s2-c01 (topic D) | s2 | 51.9 s | crop (v2) and stack (v3) | A2/B2 | v3 = two-up |

Clip ids moved: the original five agent picks (c01..c05 in time order) are now `s1-c06..c10` after the recall-first re-propose;
`c01, c03, c04` of the original five = `s1-c06, s1-c08, s1-c09` (also the three best-scoring agent picks: 62.5, 69.5, 68.0).
`work/clips.json` `sessions.s1.selects.sig` still lists the old ids; the old `select.s1-*` files are untouched.

## Numbers (YouTube preset; reels, facebook, x are byte-identical copies, per `config/shorts.yaml`)

- Spec: 1080x1920, 30 fps CFR, H.264 High yuv420p BT.709, AAC LC 48 kHz stereo, faststart. All frame counts equal `round(duration*30)`.
- Size/bitrate: 31-52 MB, 9.4-11.3 Mbps overall (CRF 18). All limits.ok (duration, size, 1080x1920, fps <= 60, bitrate <= 25 Mbps; limits UNVERIFIED).
- Loudness, measured on the encoded file: before the fix -14.5..-14.9 LUFS at -3.3 dBTP; after `-14.1..-14.5 LUFS, -1.1..-2.3 dBTP` (ceiling -1.0).
  Remaining shortfall of 0.1-0.5 LU is the limiter taming 25 dB-crest speech, inside the 1 LU tolerance.
- Face framing (Apple Vision on every 1 fps frame of the OUTPUT, 4 shorts, 139 frames): face found in 139/139, never touching the frame edge;
  face centre x 0.38-0.62 of width, face top y 0.10-0.27, face height 19-30% of the frame (crop target 25%; the 1.15x punch-ins give 29-30%).
- Reframe stability: one static crop per camera (distinct crops per camera = 1 in every short), max crop-centre move between shots of the
  same camera = 0.0, max size change 0.0. The only motion is the cut between cameras and the intentional punch-in.
- First frame: first caption cue starts at 0.00-0.10 s in all four (clips open on speech); cue starts match word starts within 5 ms (same words).
- Sound/pictures: captions come from the same transcript words mapped through the same doc, so caption-to-audio timing is by construction;
  not independently re-transcribed.

## Bugs found and fixed (each has a regression test in `tests/test_shorts_real.py`)

1. **Shorts could not run on a proposed clip without building selects into clips.json** (and the old `--clip` path silently allowed
   proposed clips). Now `--allow-unapproved` is explicit, cuts a `select.<slug>` on the fly (fingerprint-checked against the pinned source
   edit, reused on repeat) WITHOUT touching clips.json, marks the doc and sidecar `draft: true`, and exports to `shorts_drafts/`.
2. **Jump cuts were exposed.** The rough cut hides removed content with a cutaway or a 1.00-1.18x punch-in, and `ave shorts build` threw the
   punch-in away (`zoom` reset to 1.0) so the same camera appeared twice at identical framing with content missing between: a visible jump
   (s1-c06 had B1, B1, B1). Fix: keep the source zoom; `build.jump_cut_zoom` (1.15) alternates a punch-in on any remaining jump cut.
3. **Loudness landed 0.5-0.9 LU low** (-14.5..-14.9 LUFS, true peak -3.3): the long-form limiter ceiling (tp - 2 dB) was reused. Shorts now
   start at `audio.start_limit_db: 0.0` and the existing measure-the-encoded-file loop tightens it only if the codec overshoots.
4. **Listener cutaways were invisible.** A short reads a reaction shot every ~2 s on a phone as flicker (s1-c09: 4 of 9 shots, s2-c01: 5 of 14).
   They are now reported (`short.cutaways`, a note) and `build.cutaways: speaker` / `--cutaways speaker` keeps the talker on screen
   (default unchanged: `keep`, the creator approved the pacing).
5. **Vertical lower-third sat in the platform UI zone** (82% down a 1080x1920 frame, phone-small) and would have collided with the captions
   at 65%. In portrait it is now 1.35x and its bottom edge sits at 54% of the height (above the captions, clear of the bottom 25%).
6. `ave motion place` could not target a named doc (no `--name`), so it could not touch `final --name s1` or a short. Added `--name` and
   `--stage short`.
7. Bitrate and fps were not checked against platform limits. `check_limits` now reads optional `max_kbps` / `max_fps` (unverified values).

Not reproduced: one export of s2-c01 died with ffmpeg exit 255 in the loudness pass. A second run, same inputs, passed. Another workstream ran
`pkill -f "ffmpeg.*<episode-root>"` during this window, which is the likely cause; every output was re-verified (frame count, duration
against sidecar) afterwards and none is truncated. If it recurs on a quiet machine, treat it as a bug.

## New, opt-in (defaults unchanged)

- `--layout stack`: both speakers at once, 1080x960 each, cameras in speaker-id order (S1 on top), 9:8 face crops
  (`stack.face_frac` 0.42), captions on the seam. Implemented in `src/ave/stack.py` as a second render of each half + vstack into one
  intermediate picture; audio, captions, loudness, verification and sidecars are the normal short code. Run on s2-c01 (v3): both faces in
  frame the whole 52 s. Look at the top edge: the cap is tight to the top in some frames (source headroom).
- `--layout audiogram`: dark card + animated waveform + the same karaoke captions, for clips with no usable picture
  (`src/ave/audiogram.py`). Tested on synthetic fixtures only (the real episode has no audio-only clip).
- Motion pieces for a socials draft reel (clip s1-c06): lower-third (vertical-safe, placed in short v5), an animated quote card as a 6 s
  vertical video from a verbatim quote of the clip, and the karaoke captions (word sweep; highlight is "already spoken").
  `06_EXPORTS/shorts_drafts/motion/{quote-card.s1-topic.v1.mp4, reel.s1-topic.v1.mp4}`.

## Needs a human look

- Cap/headroom: s1-c06 and the c09 punch-ins are tight at the top of the frame (the cap brim is inside but close). Stack top half likewise.
- Whether reaction cutaways stay (keep) or the talker stays on screen (speaker): compare s1-c09 v1 (4 cutaways) with v3 (speaker).
- Caption reading speed: 31 chars/s on one s1-c08 cue, several >23 on s2-c01 (a fast talker); the tool reports, it does not retime.
- Quote card: starts mid-sentence in lower case; attribution type is small on a phone; the brand fonts are
  not installed (system serif/sans used), so look again after adding them.
- Alpha import in Resolve is still unverified (unchanged); these drafts are composited by ffmpeg, not Resolve.
- Captions render in a system fallback font (no brand fonts in `motion-kit/brand/fonts`); the crops are 1.0-1.1x native pixels, so the picture is sharp.
