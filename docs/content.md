# `ave content`: the promotion kit

Source in, a kit out: quote posts, podcast thumbnails, carousels and reel cards, rendered by the in-house renderer, reviewed by the human
on a contact sheet, and queued into the same hash-bound publish approval the videos use. Design and decision record:
[proposals/agentic-content-pipeline.md](proposals/agentic-content-pipeline.md). The in-house renderer needs no Canva and is the default. Canva is an opt-in second renderer (see "Canva renderer" below).

## Turning it on

Opt-in per install. The repo default (`config/content.yaml`) is `enabled: false`; `ave content` then explains how to switch it on and exits.
Turn it on without touching the repo: `enabled: true` in `~/.config/ave/content.yaml` (or `$AVE_CONFIG_DIR/content.yaml`), or `AVE_CONTENT_ENABLED=1`.
The override file is merged over the repo config, so you can also tune policy there (for example `planning.max_items_per_episode`).
Nothing in the video pipeline imports this package. An episode with no `content/` folder behaves exactly as before: `ave status` shows no content stage until `ave content` has started there.

## Flow

```
ave content plan <ep>                    candidates from approved clips (work/clips.json), episode facts, ingested writing
ave content ingest-writing <ep> <dir>    Markdown -> passages p001 and sentences p001.s1
   (agent chooses quotes, writes picks.json)
ave content propose <ep> picks.json      validate (verbatim!) -> content/main.vN.json
ave content render <ep> [--item ID] [--video]     PNGs + critic -> next version
ave content review <ep>                  content/review/index.html (contact sheet; downloads decisions.json)
ave content decide <ep> decisions.json --by NAME  approve | reject | nudge -> next version
ave content queue <ep> [--platforms ..] [--visibility ..] [--dry-run]    APPROVED items -> publish queue (never approves or sends)
ave content status|show|templates <ep>
```
Files live under the layout's work folder: `<work>/content/` holds `main.vN.json` (never overwritten), `candidates.json`, `writing.json`,
`renders/v<N>/` (every render pass writes into the folder of the doc version it creates, so earlier PNGs are never overwritten), `review/`, `decisions.jsonl`. Skills: `ave-content-plan`, `ave-content-render`, `ave-content-review`.

## ContentDoc v1

Versioned and validated like EditDoc (`ave.content.manifest`): every change is a new version. `sources` (`clip_quote`, `markdown`, `episode_meta`) carry the
text quotes are checked against and a hash of it; `items` carry template, style, format, renderer, slots, source, platforms, caption, alt text,
`outputs` per platform (file, size, sha256, alt text; one per slide for a carousel), `review` (status, critic result, who and when),
`provenance` (renderer version, template revision, tokens hash, where each quote was spoken, a reserved `canva` block), `license` (assets and
`ai_generated`), and the agent's `reason` and `confidence`.

**Quotes are verbatim.** A slot marked `x-verbatim` in `template.yaml` must appear word for word in its source (case and punctuation
ignored, the rule `ave clips propose` applies to hook quotes). No ellipses, no `[edits]`. Quotes must start and end on a complete sentence/phrase: `ave content plan` skips (and lists under `skipped_incomplete`) a clip that is not a complete statement and offers its `context_variant` text when that is complete; `ave content propose` rejects a clip source or a quote that starts mid-sentence (`--allow-incomplete` is the explicit, recorded override; the UI can never pass it). The looping quote reel (`ave motion quote-reel`) is the quote cards' own product and is never part of a clip. Only approved clips are quotable (a `keep` verdict in `ave clips decide`/the review page is `approved`; `maybe` and heuristic-surfaced sections that nobody kept are not). `verify_sources` re-reads
the clip or Markdown file before every render and refuses if it changed since the plan. Propose also records the speaker and time of each quote
from the transcript and warns when the attribution names someone else, or when the words read like typos (repeated word, filler).

## Templates, styles, formats

`content/templates/<id>/template.yaml` declares slots (a JSON-Schema subset), formats, styles, renderers. `ave content templates` prints them and the matrix.

| Template | Formats | Notes |
|---|---|---|
| `quote-post` | 1x1 1080x1080, 4x5 1080x1350, 9x16 1080x1920 | one pull quote; show name once at the top; speaker name with the title (CO-HOST) beneath it |
| `thumbnail` | 16x9 1280x720 | short title, guest, optional photo (license-gated) |
| `carousel` | 4x5 | cover, quote/point slides, closing; one PNG per slide with a counter and progress rule |
| `reel-card` | 9x16 | intro or outro; PNG for review, `--video` renders ProRes 4444 through `ave motion` |

Fonts: **Newsreader** (serif) and **Inter** (sans), SIL OFL, are bundled as variable fonts in `motion-kit/brand/fonts/` with their licenses and
source hashes (`SOURCES.md`); the renderer uses them and falls back to Georgia / Helvetica Neue only for a file that is missing. The fit checks run against these metrics.

Styles `ink`, `paper`, `clay` are colour-role maps over `motion-kit/brand/tokens.json` (`motion-kit/brand/styles.json`); a new style is a data change.
The pages live in `motion-kit/stills/<id>/` (not in `motion-kit/templates/`, which holds the video overlays that `ave motion list` enumerates), share
`motion-kit/lib/timeline.js`, `stills.js` and `stills.css`, and follow the kit rules: no hard-coded brand values, no animation library, deterministic.

## Names on a graphic: the show once, the name, the title beneath

A graphic prints the **show name once** (the small mark at the top) and, under the quote, the **speaker's name with their title beneath it**:

```
THE LONG TABLE            <- show: once, small label, top
-
"quote ..."
Maren                 <- name (sans)
CO-HOST                  <- title beneath the name (small label)
```
`episode.json` `speakers` drive it: `name` (printed by default), optional `display_name` (printed instead), optional `full_name` (only used to MATCH an
attribution such as "Maren Okafor"; never printed, nothing invents a surname), optional `title` (default **CO-HOST** for a host; a guest has none unless set,
`guest: true` marks a guest). The quote-post slot is `title` (it replaced `role`, which still loads as an alias). Resolution happens in
`registry.effective_slots(schema, slots, speakers)`, used by the renderer, alt text, the Canva job and the queue caption, so every path agrees; `propose` and `render`
also write the corrected slots into the new doc version (`report.normalized`). A slot marked `x-byline` in `template.yaml` (attribution, title, subtitle, guest,
cta) that repeats the show name is emptied or has the name cut out; `propose` rejects such a pick; the critic counts the show name in the rendered DOM and fails a
graphic that prints it twice (`critic.show_name_count`). Never put the show name in a role, subtitle or attribution.

*Why the first drafts showed it twice:* the stored doc had `role` = `show` = "The Long Table", the PNGs on disk were rendered before the blanking fix, and the doc could not be
re-rendered because the clips had been renumbered (`Sources changed since the plan was made`), so the stale images stayed. Now a renumbered clip is followed when exactly one clip
holds byte-identical text (`sources.rebind_clip_sources`, recorded as `rebound_from` in the new version).

## Rendering and the critic

One Chromium per batch; opaque RGB PNG without metadata; same slots, style and machine give identical bytes. Text is fitted by measuring the page (largest size that fits
between a ceiling and a floor), then the critic measures the result from the DOM and pixels: every text rect inside the format's **safe area**
(`config/content.yaml`, 9x16 reserves the bands platform UIs cover), no overflow, **WCAG contrast** against the pixels behind each text block
(4.5:1, 3:1 for display text), and a minimum text size for phones. Failures are listed in `review.critic.notes`; approving a flagged item needs an explicit
override and a note. Unlicensed assets block rendering (`ave assets log-license`), as in `ave final`.

## Canva renderer (opt-in, two-phase handshake)

Default is always the in-house renderer. Canva is off (`renderers.canva.enabled: false`) and is switched on and mapped per install in `~/.config/ave/content.yaml`
(Brand Template ids are the creator's, never committed; `field_map` maps slots to Canva text fields; see `config/content.yaml`). The CLI cannot call Canva (the MCP lives in the agent
harness), so:

```
ave content render <ep> --renderer canva [--item ID] [--export png|jpg|mp4]   writes <work>/content/canva_jobs/<job>.json (fields, steps, target size); calls nothing
! ave content canva-approve <ep> <job>         HUMAN act at a terminal: allows the Canva writes (autofill creates a NEW design; export). Agents cannot run it
   (agent follows skills/ave-canva-render: dataset check, autofill-design, [resize-design], export-design, downloads at once; logs each call with canva-log)
ave content canva-import <ep> <job> <file>     freezes the export: size check, sha256, new ContentDoc version as a DRAFT, provenance.canva
ave content canva-fail <ep> <job> --reason ..  records the failure (provenance.canva.fallback); then `ave content render <ep> --item ID` renders in-house
ave content canva-brief [<ep>] | canva-status   final palette, bundled fonts (files + licenses), names and card layout to enter in Canva's Brand Kit by hand (one way; with <ep> it lists the speakers' names and titles) | local config and job files
```

Imported Canva items enter the normal review, decision and publish approval; the critic is marked `measured: false` (no DOM to measure), so a human or the agent must look at the image.
`provenance.canva` holds design id, Brand Template id, kit id, export format, hash, the call log summary and a license note (Canva Content License, stock elements inside a template UNVERIFIED for commercial social use).
Re-rendering a Canva item in-house needs it named (`--item`); the switch is recorded. Rate limits (export 20/min and 500/day, autofill 60/min, from Canva docs) are in config and enforced by `RateLimiter`.
Supported: text fields on single-image templates. Not yet: carousels, image or chart fields, Brand Kit writes, live verification.

## Publishing images

`queue.create(..., media_type="image"|"carousel", files=[...], alt_texts=[...])`; video items (the default) are untouched, byte for byte. The approval hash is the
file's sha256 for an image and the sha256 of the ordered list of every slide's sha256 for a carousel; alt texts and media type are part of the approved metadata.
Platform limits for images (`image:` blocks in `config/platforms.yaml`: sizes, aspect, bytes, formats, carousel counts, alt text and caption lengths) are checked at queue time and
at approval. The YouTube and Facebook adapters stay video-only and say so; the aggregator adapter uploads every file and exposes `{media_urls}`, `{media_ids}`,
`{alt_texts}` and `{media_type}` to its post template. `ave content queue` skips platforms whose adapter cannot carry images. Approval stays the human's
(`! ave publish approve`), and `ave publish run` is a dry run unless `--yes`.

## Not verified

Live posting of images anywhere; every `image:` limit in `config/platforms.yaml` (all `verified: false`, from documentation); the safe-area fractions against current platform UIs;
ProRes alpha import of reel cards in Resolve;
appearance of other-machine renders. Canva: nothing was run against the live connector (it was not signed in); tool names, field types, limits and fonts are unverified ([field note](field-notes/2026-10-14-canva-discovery.md)).
