---
name: ave-content-render
description: Render planned content items to PNG with the in-house renderer and look at every image before anyone else does. Use after `ave content propose` and the human's plan trim; also for reel intro/outro cards (ProRes alpha clips). Covers the critic, fixes, and the license gate.
---

# Render and LOOK

```bash
ave content render <ep>                       # draft items -> content/renders/*.png, new doc version with sha256, critic, alt text
ave content render <ep> --item quote-q1-4x5   # only this one (re-rendering an APPROVED item voids its approval)
ave content render <ep> --video               # reel-card items also -> ProRes 4444 .mov (ave motion machinery)
```

Same slots, same style, same machine give identical bytes, so a sha256 names exactly what the human will review. Renders are opaque
RGB PNGs: 1080x1080, 1080x1350, 1080x1920, thumbnails 1280x720.

## LOOK (mandatory, like `ave motion still`)

Read every PNG in the report with the Read tool and critique it before you say it is done:

1. Legible at phone size? Squint: the quote must read at about 390 px wide, a thumbnail at 168 px.
2. Restrained? Flat colour, serif words, thin rule. No glow, gradient, bounce, stock-photo gloss, corporate polish.
3. Composition: does the text block sit well, is there air around it, does the last line orphan one word, does the open quote hang cleanly?
4. Words: is the quote what the plan said, is the attribution right, any stutter or filler showing?
5. Alt text: generated from the slots and accurate; override with `alt_text` in the pick (or a nudge) when you can describe it better.

The critic is measured from the rendered page, not guessed: `fits` (every text block inside the format's safe area, nothing overflowing),
`contrast` (WCAG ratio against the pixels behind the text, needs 4.5:1, 3:1 for large display text), text size (phone floor). It records
`notes` per failure. It is a floor, not taste: a passing critic can still be ugly. A failing item is fixed, not argued with: shorten
the text, pick another style, or reject it.

## Fixing

Change the pick and run `ave content propose` again (undecided drafts are replaced; human decisions survive), or apply a nudge through
`ave content decide`. Fonts: Newsreader and Inter are bundled (`motion-kit/brand/fonts/`, SIL OFL), so renders look the same on any machine; if
`ave motion tokens` lists `missing_brand_fonts`, the fallbacks (Georgia, Helvetica Neue) are in use and the fit checks no longer match the brand type.

## Names on the card

Show name ONCE at the top; the speaker's name with the title CO-HOST beneath it (from `episode.json` `speakers.<id>.title`, default CO-HOST for hosts; a `display_name`
overrides the printed name; "Maren Okafor" resolves to "Maren"). Never set the show name as a role, subtitle or attribution: propose rejects it and the critic
fails a graphic where it prints twice (`critic.show_name_count`). Every render pass writes `renders/v<N>/`, so earlier PNGs are never overwritten; if render stops
with "Sources changed" because clips were renumbered, it follows byte-identical text automatically (report `rebound_sources`) and refuses anything else.

## License gate

An item that uses a media file (a guest photo) needs a license-log entry for exactly those bytes, like `ave final`. The render stops with
the file list; log with `ave assets log-license` (own photo, consent noted). `--allow-unlicensed` only if the human owns the rights and says so.

## Failure modes

- "Could not start Chromium": `.venv/bin/python -m playwright install chromium`.
- "Sources changed since the plan was made": a clip or Markdown file changed; re-propose.
- Reel card video: judged in Resolve like any overlay; alpha import is unverified until the human checks the swatch (see ave-motion).

## Checkpoint

Tell the human which items rendered, which the critic flagged and why, and your own critique of the design. Then `skills/ave-content-review`.
