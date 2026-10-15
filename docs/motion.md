# Motion kit

Brand motion graphics rendered with alpha, deterministic, and license-clean (no GSAP or any third-party animation
library; the only runtime code is `motion-kit/lib/timeline.js`, about 100 lines).

## How it renders

1. A template (`motion-kit/templates/<id>/template.html`) is a plain page that exposes `window.__render(t)`, a pure
   function of time. It never uses CSS transitions, timers or `requestAnimationFrame`.
2. `ave.motion` opens it in headless Chromium (Playwright) at the target canvas, injects the tokens, props, canvas,
   duration and outro flag, waits for fonts, then seeks frame `i` at `t = i / fps` (a virtual clock) and takes a
   transparent-background PNG screenshot.
3. ffmpeg encodes the numbered PNGs to ProRes 4444 with alpha (`prores_ks -profile:v 4444 -pix_fmt yuva444p10le`),
   tagged and converted with BT.709. PNG sequences are available with `--png-sequence`.

Determinism is tested (two renders produce byte-identical PNGs on the same machine). Cross-machine identity needs the
same fonts: bundle brand fonts (see below). The outro finishes exactly on the last frame; pass `--no-outro` / `"outro": false`
to hold the final frame instead.

## Brand tokens

`motion-kit/brand/tokens.json` is the only place for colors, fonts, type scale, easing, motion timing, layout margins and
safe areas. Templates read it through CSS variables (`--c-*`, `--f-*`, `--fs-*`, `--u`). A test fails if a template
hardcodes a hex color or a font family, and another fails if an easing curve could overshoot (no bounce). Sizes scale
with `min(canvas w, h) / 1080`, so 1920x1080, 1080x1920 and 4K all lay out correctly.

Safe areas: action-safe is 3.5% in from each edge, title-safe 5%; layout margins (7.5% / 10%) sit inside title-safe.
`--safe-area` draws pink (action) and blue (title) guides on a still.

Fonts: Newsreader and Inter (SIL OFL, variable) are bundled in `motion-kit/brand/fonts/` with their licenses and the source hashes
(`SOURCES.md`). Templates use them; the token fallback stacks (Georgia, Helvetica Neue) apply only when a file is missing.

## Templates

| id | props (limits enforced) | default duration |
|---|---|---|
| `lower-third` | `name` (40), `title` (60), `side` left/right | 5 s |
| `quote-card` | `quote` (220), `attribution` (60), `role` (60), `backdrop` solid/scrim/none | 7 s |
| `chapter-card` | `title` (70), `number` (4), `label`, `subtitle` (100), `backdrop` | 4 s |
| `stat-callout` | `value` (9), `label` (90), `source` (50), `side` | 5 s |
| `title-card` | `title` (80), `kicker` (40), `subtitle` (80), `backdrop` | 5 s |
| `short-title` | `title` (60), `show` (40), `mode` card/overlay, `bottom` (overlay height) | 2 s |

`short-title` is how a vertical short OPENS (config `opening` in `config/shorts.yaml`, code `src/ave/opening.py`): `overlay` is a lower-area panel over the
first ~2 s of the clip (no extra time), `card` an opaque ~1.4 s title screen before it. It shows a topic title written from the clip, never the quote.

### Quote card vs. title: two different products

The `quote-card` is NOT a short opening. A quote card in front of a clip shows the quote on screen and then has the speaker say it, which the creator found
awkward (2026-10-14). `ave shorts build` refuses a `quote-card` overlay on a short (`opening.refuse_quote_cards`), and nothing prepends one. The quote card keeps
its own product, the **quote reel**: animated quote cards on a long loop, no speaker audio.

    ave motion quote-reel <episode> --quotes quotes.json [--card-s 6] [--loops 3] [--canvas 1080x1920] [--out reel.mp4]

`quotes.json` is `[{"quote", "attribution", "title"}]`; every quote must be verbatim in the transcript and start on a complete sentence or phrase
(`content.sources.quote_completeness`). The cards fade from and to the brand ink plate, so the file loops seamlessly; video only, `<out>.json` says
`audio: none`, `draft: true`. Versions are never overwritten (`quote-reel.v1.mp4`, `.v2`...).

`ave motion list` prints the live schema. To add a template: create `templates/<id>/{template.html,schema.json,example.json}`
(schema is a JSON-schema subset: type, enum, minLength, maxLength, minimum, maximum, required, additionalProperties,
default; plus `x-default-duration`), use only token variables, and call `AVE.ready(function (t) { ... })`.

### Sequence templates (opening and closing)

`motion-kit/sequences/` holds five more templates (`episode-title`, `host-card`, `thanks-card`, `credits-roll`, `end-card`) with the same contract and the
same hygiene tests, but they are not part of `ave motion list` / `place`: `ave sequence` renders them over a brand plate. See `docs/sequences.md`.

## Commands

```
ave motion list
ave motion still --template lower-third --props '{"name":"Ada"}' --t 1.2 [--canvas 1920x1080] [--safe-area] [--preview] [--out f.png]
ave motion render spec.json [--out card.mov | --png-sequence --out dir]
ave motion place <episode> --template lower-third --props '{"name":"Ada"}' --at 12.0 --dur 4 [--stage final] [--layer N]
ave motion swatch [--out motion-swatch.mov] [--canvas 1920x1080]
```

`still --preview` also writes `<out>.preview.png`: the graphic over a stand-in plate, because a transparent PNG is hard
to judge by eye. `place` writes `work/motion/<template>-<hash>.mov` (re-used if the spec is unchanged) and a new edit
version with an overlay `{kind: "motion", alpha: true, x: 0, y: 0, w, h = canvas, layer}`; `file` is relative to the episode root.

## Importing into DaVinci Resolve: UNVERIFIED recipe

Nothing below has been confirmed on the author's Resolve; verify once with the swatch before trusting overlays.

1. `ave motion swatch --out motion-swatch.mov`: a 2 s clip with a 50% red box (left) and a 100% red box (right) on transparency.
2. In Resolve, import it, place it on a track above a mid-grey clip (or any footage).
3. Clip Attributes > Alpha Mode: set to **Straight** (the PNGs are straight alpha; ProRes 4444 stores straight alpha). If the
   edges look dark-fringed, try Premultiplied.
4. Expected: the left box looks like red at half strength over the footage, the right box is solid red, nothing else
   is tinted. Over 50% grey, the left box should read about (191, 64, 64) in a gamma-space blend. If it looks too
   dark or too light, the likely cause is the Color Space Tag/Input Color Space (use Rec.709 Gamma 2.4 or sRGB to match your timeline);
   record the setting that makes it right here.
5. The clip is tagged BT.709 primaries, transfer and matrix.

Until a human confirms steps 3 and 4, treat alpha behaviour in Resolve as unknown. The ffmpeg compiler composites
overlays itself and is independent of this.

## Tests

`tests/test_motion.py` covers: kit hygiene (no hardcoded brand values, no GSAP, no overshoot), schema validation messages,
still alpha and content, safe-area guides, transparent start / visible middle / clean outro, vertical canvas, ProRes 4444
`yuva444p10le` with the right frame count and fps, alpha surviving a decode, PNG-sequence output, byte-identical determinism,
the swatch alpha, `place` producing a valid new doc version, and the CLI.
