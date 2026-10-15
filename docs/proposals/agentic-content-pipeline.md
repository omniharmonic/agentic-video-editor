# Proposal: the agentic content pipeline (`ave content`)

> **Proposal and decision record.** Research written before the feature was built; the decision and the current state are in [docs/ROADMAP.md](../ROADMAP.md) and [docs/content.md](../content.md). See [docs/README.md](../README.md).

Date: 2026-10-13. Status: proposal for the creator's decision. Research only; no Canva account was touched.

## 1. Vision

The editor grows from "episode in, finished cut out" into "source in, a promotion kit out". For each episode (and for the creator's writing) the agent produces short-form reels (extended sequences), one-off quote posts, podcast thumbnails and other promo visuals from a handful of reusable templates, each defined by a style and a format. Everything is reviewed by the human and published through the existing hash-bound approval queue. The tone stays authentic, grounded, never cheesy or corporate. Canva is wanted as a design surface; it must not become a hard dependency and must not touch the video pipeline.

## 2. Recommendation: hybrid (c), motion-kit first

Build the **template registry and ContentDoc first, with the in-house renderer as the default**, and add Canva as a second renderer behind the same interface. Reasons, by criterion:

| Criterion | (a) Canva-first | (b) In-house only | (c) Hybrid (recommended) |
|---|---|---|---|
| Taste and brand fidelity | Good tooling, but AI `generate-design` output is generic by default | Exact: tokens, fonts, easing already ours | Ours by default; Canva where its editor beats HTML |
| Determinism, review | Weak: AI layouts vary, export URLs expire in 24 h | Strong: same props, same pixels, hashable | Strong for `motion-kit`; Canva results are frozen as PNGs and hashed |
| Cost / plan | Pro+ needed for autofill, brand templates, resize | Free | Free core; Canva optional |
| Open-source friendliness | Poor: every other creator needs paid Canva | Best | Good: Canva is an opt-in plugin |
| Human in the loop | Natural (edit in Canva) | Needs our review page | Both; one review flow |
| Failure modes | OAuth per user, rate limits, tool bugs (reported open issue, July 2026, on `perform-editing-operations`) | Fonts and layout are our job | Registry can fall back to `motion-kit` when Canva fails |

Canva's own rules push the same way: Brand Kit data "can't be exported for non-Canva workflows" and may only be used to create or change Canva designs, and templates must not be "programmatically analyzed, decomposed, or replicated outside Canva" (https://www.canva.dev/docs/apps/mcp/usage-policy/). So sync is **one way: our tokens push to Canva, never the reverse**, and Canva templates stay opaque slot-fillers.

## 3. Architecture (additive only)

```
sources                 ContentDoc (content/<id>.vN.json)        renderers              review/publish
clips.json quotes  ->   items[]: template + slots + outputs  ->  motion-kit (PNG/MP4)  -> contact sheet -> human
transcript, title       provenance, license hooks, status        canva (opt-in)         -> publish queue (image/carousel/video)
writing/*.md
```

- **New code only:** `src/ave/content/` (`manifest.py`, `registry.py`, `sources.py`, `render_motionkit.py`, `render_canva.py`, `review.py`), `src/ave/commands/content.py` with `register(sub)`, skills `ave-content-plan`, `ave-content-render`, `ave-content-review`. No edit to `cli.py`, `editdoc.py` or any EditDoc schema.
- **Feature flag:** `config/content.yaml` with `enabled: false` by default; `ave content` exits with a clear message when off. Canva sits under `renderers.canva.enabled: false`.
- **Separate artifacts:** `content/` (manifests, `renders/`, `review/`) in the layout's work area; never `edits/` or `exports/`.
- **Reuse:** `motion-kit/lib/base.css`, `brand/tokens.json`, the Playwright renderer behind `ave motion still` (PNG already exists), `ave assets` license log, `publish/queue`.

## 4. ContentDoc sketch (parallels EditDoc: versioned, validated, never overwritten)

```json
{ "version": 1, "kind": "content", "episode": "ep012",
  "brand": {"tokens": "motion-kit/brand/tokens.json", "tokens_sha256": "..."},
  "sources": {
    "q1": {"type": "clip_quote", "clip": "systems-beat-goals", "sentence_ids": ["s005"], "t0": 812.4, "t1": 818.9, "speaker": "S1"},
    "w1": {"type": "markdown", "file": "writing/soil.md", "range": [12, 15]},
    "ep": {"type": "episode_meta", "title": "...", "guest": "..."} },
  "items": [{
    "id": "quote-q1-4x5", "template": "quote-post", "style": "ink", "format": "4x5",
    "renderer": "motion-kit",
    "slots": {"quote": "a goal is a wish and a system is a habit", "attribution": "Ines Calder", "role": "Soil ecologist"},
    "source": "q1",
    "outputs": [{"platform": "instagram", "w": 1080, "h": 1350, "file": "renders/quote-q1-4x5.png", "sha256": "...", "alt_text": "..."}],
    "review": {"status": "draft|approved|rejected", "critic": {"fits": true, "contrast": 7.1, "notes": []}, "by": null, "at": null},
    "provenance": {"renderer_version": "...", "template_rev": "...", "canva": {"design_id": null, "template_id": null}},
    "license": {"assets": [], "ai_generated": false}, "reason": "...", "confidence": 0.8 }],
  "notes": "" }
```

Rules: quote text is checked verbatim against the transcript (as `clips propose` does for `hook_quote`); every item records `reason` and `confidence`; unlicensed assets block rendering as `ave final` does; `ai_generated: true` forces the publish AI-disclosure flag.

## 5. Template registry: style x format

`content/templates/<id>/template.yaml` declares `slots` (JSON Schema, like `motion-kit/templates/*/schema.json`), `formats`, `styles`, and `renderers: {motion-kit: {...}, canva: {brand_template_id, field_map}}`. The default renderer is listed first; a template with only one renderer is fine.

| Template | Formats | Notes |
|---|---|---|
| quote-post | 1:1 (1080x1080), 4:5 (1080x1350), 9:16 story | extends existing `quote-card` |
| thumbnail | 16:9 (1280x720) | guest, title, one image slot |
| carousel | 4:5 slides 1..N | cover, quote or point slides, closing slide with show name |
| reel-intro / reel-outro | 9:16 | animated, rendered by `ave motion` to MP4, spliced into `ave shorts` as overlays |
| waveform-quote (later) | 9:16 | audio snippet from the clip |

Styles are token sets (`ink`, `paper`, `clay`), so a new style is a tokens variant, not a new template. "Extended reels" are the existing `ave shorts` output plus intro/outro cards and optional quote overlays, planned in the ContentDoc, composed through the unchanged EditDoc overlay mechanism.

## 6. Sourcing

- **Episode:** `work/clips.json` (scored quotes, sentence ids, timestamps; only `approved` clips by default), `work/transcript.json`, `episode.json` (title, guest, names). The agent picks quote candidates by sentence id and tells the human why.
- **Writing:** `ave content ingest-writing <dir>`: Markdown files split by heading or paragraph into sentence-addressable passages (same sentence-id idea), front matter for title/date/URL. Quotes are verbatim-checked against the file.
- Both land in `sources`, so templates never know where words came from.

## 7. Where it plugs in

- **State machine:** one new optional stage `content` (`needs: ("selects",)`, `human_gate=True`, `optional=True`) after `selects`. Done when `content/*.json` exists. Because it is optional and not blocking, existing `next` computation is unchanged.
- **Publish queue:** `queue.create` gains `media_type: video|image|carousel` (default `video`, so old items behave identically). Same sha256 binding, approved per file; a carousel hashes an ordered manifest of its images. Image platforms and limits go in `config/platforms.yaml`. Real image posting is gated on the existing adapter work (Instagram and X still route through the aggregator).
- **Brand kit:** `tokens.json` stays the single source of truth. `ave content canva-brief` prints the palette, fonts and type scale for the creator to enter once in Canva's Brand Kit (a push in the Canva UI; no Canva API call needed).
- **Canva renderer, when enabled:** `search-brand-templates` / `get-brand-template-dataset` to read fields, `autofill-design` with the slot map (text, image, chart fields), `export-design` to PNG/MP4, then **download immediately, hash, and treat the file as the artifact**. Never store export URLs. Design ids go in `provenance`.

## 8. Human checkpoints

1. Plan: agent proposes items (which quotes, templates, platforms); human trims. 2. Render preview: contact sheet of all PNGs with critic flags (overflow, contrast, safe zone); human approves/rejects per item. 3. Queue and `ave publish approve` as today (human act, hash-bound). Canva-sourced items additionally show the Canva design link so the human can tweak and re-export, which creates a new hash and requires re-approval.

## 9. Verified Canva facts (as of 2026-10-13)

VERIFIED:
- Official remote MCP at `https://mcp.canva.com/mcp`, OAuth per user, no org-level auth: https://www.canva.dev/docs/mcp/troubleshooting/ , https://www.canva.dev/docs/apps/mcp/access/
- Tools (about 45) include `generate-design`, `create-design`, `autofill-design`, `search-brand-templates`, `list-brand-kits`, `export-design`, `resize-design`, `upload-asset-from-url`, `generate-image`, `remove-background`, and editing transactions (`start/perform/commit/cancel`). Rate limits: `export-design` 20/min, `generate-design` 20/min, `autofill-design` 60/min, `perform-editing-operations` 50/min: https://www.canva.dev/docs/apps/mcp/tools/
- Autofill, brand kits, brand templates, resize need **Pro or above**; export and generation work on all plans (premium elements can fail with `license_required`): same page, plus https://www.canva.dev/docs/apps/mcp/best-practices/
- Connect API autofill: Pro/Teams/Enterprise (not Enterprise-only), text, image (image or video asset), chart and sheet fields; 60 req/min; "Usage limits will be introduced in the future": https://www.canva.dev/docs/connect/autofill-guide/ , https://www.canva.dev/docs/connect/api-reference/autofills/create-design-autofill-job/
- Export: PNG, JPG, PDF, GIF, PPTX, MP4 (480p to 4k, aspect follows the design); transparent PNG and lossy PNG need a premium plan; 20/min per user; 500/day per user; URLs expire in 24 h: https://www.canva.dev/docs/connect/api-reference/exports/create-design-export-job/
- Connect `create-design` takes only a size, a preset type, or an image; it cannot add text: https://www.canva.dev/docs/connect/api-reference/designs/create-design/
- Usage policy: no bulk automation around rate limits, no training external AI on Canva or Brand Kit material, Brand Kits and templates not to be replicated outside Canva: https://www.canva.dev/docs/apps/mcp/usage-policy/
- `generate-design` may need a 60 s timeout.

UNVERIFIED (primary pages returned 403 or are silent): Canva Pro price; Content License Agreement terms for commercial social use (third parties say Free and Pro allow it, Pro-licensed elements are limited to use within a design, and AI-generated content may need disclosure; read https://www.canva.com/policies/content-license-agreement/ yourself); quality of any Canva video/reel generation (docs mention only MP4 export, not video creation); font and text-formatting limits of `perform-editing-operations`; whether custom brand fonts apply in autofill; MCP behaviour with a free account for brand tools; stability (a July 2026 community report of editing-transaction failures: https://claudeissues.com/issue/79884-bug-canva-connector-editing-transactions-fail-with-not-allowed-to-access-editing).

Environment check: ToolSearch "canva" shows only `mcp__claude_ai_Canva__authenticate` and `mcp__claude_ai_Canva__complete_authentication`. The real tools appear only after OAuth; none were called.

## 10. Phased plan

**Step 1: ContentDoc, registry, quote posts (no Canva).** Add manifest validation, registry, `quote-post` at 1:1 and 4:5 and `thumbnail` via the existing Playwright still path, `ave content plan|render|review`. Acceptance (all offline): schema rejects bad docs; a quote not in the transcript is rejected; same input renders byte-identical PNGs twice; PNG dimensions match format; text overflow is detected on a 300-character quote; all existing tests still pass and `ave status` output is unchanged with the flag off.

**Step 2: carousel, writing source, publish media types.** Acceptance: writing ingest yields verbatim passages; carousel manifest hash changes when any slide changes; `publish approve` refuses an image whose bytes changed; a video item queues exactly as before (golden test on a queue file).

**Step 3: reel intro/outro cards and the optional Canva renderer.** Intro/outro animate through `ave motion` and attach to a short as overlays (new short EditDoc version, old untouched). Canva renderer implemented behind an interface with a recorded-fixture fake, so tests need no account. Acceptance with the fake: autofill field mismatches are reported, exports are downloaded and hashed, a failure falls back or stops cleanly. Live verification needs the creator.

Verifiable without Canva: everything in steps 1 and 2, the registry, the fake renderer contract, and the state/queue regression tests.

## 11. Creator to-do and open questions

1. Decide whether Canva is wanted at all in v1 (recommended: after step 2). 2. If yes: confirm plan tier (Pro for autofill and brand templates), run the OAuth sign-in once in a terminal (`! ` prefix; the agent must not do it), and enter the brand palette and fonts in Canva's Brand Kit from `ave content canva-brief`. 3. Build 2 or 3 brand templates in Canva with tagged fields (`QUOTE`, `NAME`, `BACKGROUND`) and publish them. 4. Add Newsreader and Inter files to `motion-kit/brand/fonts/` (currently falls back to Georgia/Helvetica). 5. Read the Canva license and AI terms for your plan. Questions: which platforms first; is AI-generated imagery allowed at all; do thumbnails use guest photos (consent); one visual style or per-season.

## 12. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Canva output feels generic or corporate | High if AI-generated | Autofill only from our own templates; no `generate-design` in the default path; human review |
| Canva API/tool breakage or limits change | Medium | Optional renderer, fallback to motion-kit, fake-based tests, 24 h URL expiry handled by immediate download |
| Licence or policy violation (Brand Kit export, premium elements) | Medium | One-way token push, never read Brand Kit back, `license` log hook, read terms first |
| Paid dependency leaks into open-source core | Low | Flag off, Canva in a separate module, docs say motion-kit is complete on its own |
| Breaking the video pipeline | Low | Additive files, optional stage, `media_type` default `video`, golden regression tests |
| Template sprawl / posting too much | Medium | Registry kept small; plan checkpoint limits count per episode |
| Image posting adapters are unverified | High | Dry-run first, same approval gate, aggregator stays experimental |
