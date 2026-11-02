---
name: ave-broll-music
description: Find, judge, license and place B-roll and music for an episode. Use when the user asks for b-roll, cutaways, stock footage, a music bed, to search their own footage library, or before any render or publish that contains music or inserted footage (the license check is mandatory).
---

# B-roll and music: authentic, licensed, restrained

Standard: footage should feel found, not ordered from a catalogue. Voice leads; pictures and music support it.
The checklist is data (`ave assets checklist`, `config/assets.critic.yaml`); the human tunes it, you apply it to **what you actually see**.

## 0. Rules that never bend

1. **No asset without a license-log entry.** `work/assets/licenses.jsonl` is append-only and hash-chained. Provider downloads log themselves; everything else (own footage, a purchased or CC track) needs `ave assets log-license`.
2. **A named human approves every download** (`--approved-by`). Show them the candidate (thumbnail, author, source page, why it fits) and wait. Downloading is a checkpoint.
3. **Never judge from filenames or tags alone.** Look at a contact sheet / thumbnail, then score.
4. **Run `ave assets license-check <ep>` before any render or publish.** Exit 1 means stop and fix; report the `missing` list to the human. Put `required_credits` in the description. (B-roll placed from the studio library with `ave broll place` is checked against that library's attested licenses, not the episode log; `ave broll check` reports it too.)
5. No music downloads from services you cannot verify. Tracks come from a folder the human already has rights to.

## 1. B-roll from the user's own footage

```bash
ave assets index <folder>                        # shots, thumbs, sharpness/exposure; unchanged files are skipped, tags survive
ave assets search "text" [--aspect landscape --min-duration 2 --min-quality 0.5 --no-faces]   # text matches TAGS only
ave assets contact-sheet 12 13 14 15 --out work/assets/sheet.jpg     # then LOOK at the image
ave assets tag 12 "hands, wet clay, window light, handheld"           # tag what you SAW; untagged shots are invisible to text search
```
Untagged library? Tag in batches: make a sheet of ~12 shots, look, tag each one. Face detection may be `none` on this machine (OpenCV 5 ships no
detector): `face: null` means UNKNOWN, not "no faces" -- check faces and logos yourself on the sheet.

## 2. B-roll from Pexels/Pixabay (needs `PEXELS_API_KEY` / `PIXABAY_API_KEY` in the environment)

```bash
ave assets stock-search <ep> "hands shaping wet clay" --provider both     # cached 24 h; cliche queries are refused
ave assets stock-get <ep> pexels 3195394 --approved-by "<name>" --query "hands shaping wet clay"
```
Query like a documentary researcher: a particular hand, object, place, weather, time of day. Not "business", "success", "technology", "teamwork".
Provider tags are scraped from titles and are weak evidence: view `thumb_url`, then critique.

## 3. Judge: the authenticity checklist

For each candidate that survived your eyes, write a candidate JSON (shape in `ave assets checklist`) with six scores 0-2 and a one-line `evidence` each:
texture_light, handheld_observational, specific_over_generic, restraint, ambient_sound, no_logos_faces_unreleased. Then:

```bash
ave assets critique candidates.json          # exit 1 if any fail; reasons and confidence are in the JSON
```
- Auto-reject (no appeal unless the human asks): handshake, laptop typing, drone over a city, neon AI brain, ukulele corporate music, whoosh/riser, light bulb, globe network, etc. The list also checks queries, tags, titles, filenames.
- Hard gate: a recognizable face or readable logo with no release on record fails regardless of total. Stock sites do not guarantee releases; when in doubt, don't.
- More than one zero fails even with a high total. Silent clips cannot claim great ambient sound; plan to lay real room tone.
- Low confidence (< 0.6)? Say so to the human instead of presenting it as a clean pass.

## 4. Place it: pacing rules (checked by `ave assets pacing <ep> [--beats beats.json --punchlines p.json]`)

- B-roll holds 1.5-4 s. Never over an emotional beat (laugh, crack in the voice, a pause that lands): the face carries those. Flag them from the transcript and pass `--beats`.
- At most 40% of the programme; at least 1 s of speaker between inserts.
- Use J/L cuts: let the insert's picture begin before the line it illustrates, or the line finish under it. Do not cut picture and sound together.
- Add real ambience under silent clips. Do not stack effects, speed ramps or "cinematic" treatments.
- Music: bed gain -18 to -24 dB (`gain_db`), ducked further under speech (`duck_db`). Drop the music out for ~0.6 s after a punchline (`--punchlines [{"t": 41.2}]`). Silence is a tool.
- Put each insert into the edit with `ave broll place` (next section), never by editing a version by hand.

### Placing a studio-library asset: `ave broll place`

B-roll that lives in the studio library (a stock pick, a still, your own clip; `ave-studio-broll` finds and licenses it) goes into a rough, final or short edit as an
explicit, reversible step. The asset must be **licensed and human-attested in the library**, or placing refuses and says `ave studio library attest` (a human act).

```bash
# 1. SUGGEST: writes work/broll/proposals/bp_*.json and changes no edit. Read `checks` (license, pacing, warnings) first.
ave broll place <ep> --asset a_9938... --at 41.2 --dur 2.5 [--stage rough|final|short] [--name <slug>] [--mode cutaway|overlay|insert] \
     [--in 0.8] [--audio mute|keep] [--ken-burns push_in|pull_out|pan_left|pan_right|drift] [--credit auto|always|never] [--beats beats.json] \
     [--studio DIR] --reason "hands match the line"
# 2. APPLY the proposal (a NEW edit version with a history entry; refuses if the edit moved on), or pass --apply to step 1
ave broll apply <ep> bp_4f2a...
ave broll list <ep> [--stage S --name N]      # placements, history, license closure
ave broll check <ep>                           # exit 1 = unlicensed/unattested asset; lists required credits; pacing advice
ave broll remove <ep> --id b2                  # drop one placement (a new version)
ave broll undo <ep>                            # restore the edit as it was before the latest B-roll step (a new version; refuses if something was saved since)
```

- **cutaway** (default) covers the whole frame for `[at, at+dur)` while the dialogue plays on, untouched. **overlay** is a picture-in-picture box (`--corner`, `--scale`, `--box`).
  **insert** is a ripple: the cut grows by `dur`, the dialogue holds in exact silence, captions are dropped and must be regenerated (it warns when the point falls inside a word).
  An insert is reversed with `undo`, not `remove`. Prefer cutaways: they never move the dialogue.
- **Silent unless you say `--audio keep`.** Silent B-roll leaves the dialogue master bit-identical. `keep` mixes the asset's own sound under the dialogue and ducks it
  (`config/broll_place.yaml` `audio`); lay real room tone under silent clips as before.
- **Stills** hold still unless you ask for `--ken-burns`; a still on screen for more than a few seconds with no move reads as a slide. Judge the result by looking at a frame.
- **Licenses and credits.** `ave render`, `ave final` and `ave shorts export` re-check the closure and BLOCK with a message naming the asset if it is unlicensed, unattested or its
  file changed. The render report and the short's sidecar carry `derived_from` (every placed asset) and the attribution lines; when the license requires attribution a credit
  caption is drawn on screen (`kind: "credit"` overlay) and the line belongs in the description. `ave assets license-check` accepts library placements.
- Pacing (`ave assets pacing`, `ave broll check --beats`) is advice, not a block, unless you pass `--strict-pacing`. Emotional beats still belong to the face.
- Every number (box size, fades, credit position) is in `config/broll_place.yaml` and has not been judged on real footage: LOOK at a frame (`ave frame`, or render a proxy) before telling the human it is good.

## 5. Music

```bash
ave assets music-analyze track.mp3 [--duration 90]                 # tempo, key, onset density, RMS, centroid, in-point + fades
ave assets music-pick --dir ~/Music/licensed --mood reflective --duration 120
```
Moods: reflective, warm, tense, hopeful, driving (definitions in the YAML). Prefer sparse, low-onset-density, darker tracks under speech; busy percussion and bright
midrange fight consonants. Check `fits` (is the track long enough after the in-point?) and the `reason`. Tempo/key are estimates: confidence is in the output; do not
stake a decision on a 0.2. **Listen to the in-point and fades** (render a proxy) before the human sees it. Then log the license (`log-license`) and add the `music` segment.

## Failure modes

- `PEXELS_API_KEY is not set`: tell the human; do not invent a key. HTTP 429: wait; cached queries still work.
- `license-check` missing entries: the file changed after logging (hash differs) or was never logged. Re-log with the real terms; do not edit the log.
- Everything you find looks stock-y: widen toward the human's own footage, or propose shooting the shot. Say so plainly rather than lowering the bar.
