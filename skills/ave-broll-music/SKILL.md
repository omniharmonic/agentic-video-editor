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
4. **Run `ave assets license-check <ep>` before any render or publish.** Exit 1 means stop and fix; report the `missing` list to the human. Put `required_credits` in the description.
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
- Add each insert to the EditDoc as an overlay `kind: "broll"` via `editdoc.save_new_version` (never edit a version in place).

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
