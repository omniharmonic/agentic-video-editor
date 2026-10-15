---
name: ave-motion
description: Add brand-consistent motion graphics (lower thirds, quote cards, chapter cards, stat callouts, title cards) to an episode as alpha overlays. Use during polish, after the rough cut is approved, when a speaker needs naming, a number or quote deserves emphasis, or a section needs a break.
---

# Motion graphics: look, critique, then place

Templates live in `motion-kit/templates/`, brand values only in `motion-kit/brand/tokens.json`. Never hardcode a color,
font or easing in a template; change the token. Rendering is deterministic: same spec, same frames.

## 1. Choose sparingly

Editorial restraint is the brief. Name a person once, the first time they speak. One stat or quote card per idea, never
back to back. Chapter cards only at real topic changes. If a graphic does not help the viewer, leave it out.

```bash
ave motion list      # templates, prop limits, and any brand fonts still missing
```

## 2. LOOK before you place (mandatory)

```bash
ave motion still --template lower-third --props '{"name":"Ines Calder","title":"Soil ecologist"}' \
    --t 2.0 --safe-area --preview --out /tmp/lt.png
```

Read the `.preview.png` (graphic over a stand-in plate) and critique it: is the text fully inside the blue title-safe
line, is it legible over both the dark and the bright part of the plate, does the copy fit without crowding, is it
restrained? Also check one early frame (`--t 0.4`) and, for vertical shorts, `--canvas 1080x1920`. A prop longer than
the template allows is rejected with the limit in the message; shorten the copy rather than shrinking type.

## 3. Place it

```bash
ave motion place <episode> --template lower-third --props '{"name":"Ines Calder","title":"Soil ecologist"}' \
    --at 12.0 --dur 5 [--stage final|rough|assembly]
```

`--at` is **output time** in the chosen cut. This renders ProRes 4444 with alpha into `<episode>/work/motion/` and
writes a NEW edit version with an overlay (`kind: motion, alpha: true`, stacked above existing overlays). Older versions
are never touched. Default stage is the latest of final, rough, assembly. Placing needs a valid, current cut: if the
cut changes afterward, the overlay times are stale; re-place against the new version.

`--name <session>` targets a named doc (`final --name s1`); `--stage short --name <slug>` puts the graphic on a vertical short (the lower-third
then sits at 54% height, above the captions and clear of the platform UI). Standalone vertical cards for socials:
`ave motion render spec.json` with `"canvas": "1080x1920"` (flatten onto the solid backdrop with ffmpeg for a plain MP4; see docs/field-notes/2026-10-14-shorts-real.md).

**Quote card is not a clip opening.** Never put a `quote-card` on a short (the speaker then says what the card already showed; `ave shorts` refuses it). A
short opens with a title (`short-title`, `ave shorts build --opening title_overlay|title_card`, skill `ave-shorts`). Quote cards live in the quote reel:
`ave motion quote-reel <ep> --quotes quotes.json` (looping, no speaker audio, verbatim complete-sentence quotes only). See docs/motion.md.

Typical durations: lower third 4-6 s (start about 0.5 s after the person begins speaking), stat 4-6 s, quote 6-8 s,
chapter 3-4 s, title 4-6 s. Do not cover a face with a card: stat and quote cards are for b-roll or wide shots.

## 4. Render standalone / verify the pipeline

```bash
ave motion render spec.json --out card.mov          # or --png-sequence --out frames/
ave motion swatch --out swatch.mov                  # one-time alpha/gamma check, see docs/motion.md
```

Spec: `{"template","props","duration","fps","canvas","outro"}`; unknown template or bad props exit with the reason.

## Failure modes

- Multi-session episode: pass `--name s2` to `ave motion place` (it edits `rough.s2` / `final.s2`); without it only unnamed edits are visible.
- Lower thirds: Maren/Tomas with title `CO-HOST` rendered cleanly bottom-left at 1080p, clear of faces and of the bottom-centre captions; keep the card inside one shot of that person's camera.

- "Could not start Chromium": run `.venv/bin/python -m playwright install chromium`.
- Fonts look generic: if `missing_brand_fonts` is not empty the bundled Newsreader/Inter are absent (see `motion-kit/brand/fonts/README.md`) and renders use fallbacks (rerender everything after adding them).
- Alpha looks wrong in Resolve: ALPHA IMPORT IS UNVERIFIED until the human checks the swatch; ask them to do it and report
  the result before delivering a cut with overlays.

## Checkpoint

After placing graphics, tell the human which overlays were added (id, template, time) and ask them to review in the
render or in Resolve before finalizing. Do not claim the alpha pipeline is verified until they confirm the swatch.
