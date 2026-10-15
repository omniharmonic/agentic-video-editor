# Canva discovery, 2026-10-14 (T15 phase 2)

> **Working note from the first real episode.** This is internal history kept as evidence: what we assumed, what a real recording showed, and what changed. It describes one project (a two-host, two-camera podcast), uses its real numbers, and was written while the tools were still changing, so commands and defaults may have moved on. For the current behaviour read the skill or doc a note points to. See [docs/README.md](../README.md).

**Result: discovery could not be done. The Canva connector was not authorized in the session that built the renderer, so no Canva call of any kind was made.**
Nothing was created, edited, autofilled, exported or published in the creator's Canva, and no read was made either.

## What was observed

- `ToolSearch "+Canva"` returned exactly two tools: `mcp__claude_ai_Canva__authenticate` and `mcp__claude_ai_Canva__complete_authentication`. Their description says the
  "claude.ai Canva" server (claudeai-proxy at https://mcp.canva.com) "is installed but requires authentication" and that the real tools appear after the OAuth flow.
  So the claude.ai Canva MCP is installed but NOT yet signed in, in this harness. (Same state as the 2026-10-13 proposal's environment check.)
- I did not call `authenticate`: it only starts an OAuth flow that needs the creator's browser, and a build agent has no business starting a sign-in on the creator's account unasked.

## Not discovered (all still open; each is a read-only call for the first authorized session)

| Question | How to answer it (read-only) |
|---|---|
| Which tools exist (names, argument schemas) | `ToolSearch "Canva"` after sign-in; load each schema. The job files carry `tools_verified: false` and the skill tells the agent to compare names and arguments before any call |
| Brand kit contents (palette, fonts) | `list-brand-kits`, then read the kit. Read for the creator's information only; do not copy Brand Kit data into the repo (Canva policy) |
| Which Brand Templates exist, and their autofill fields | `search-brand-templates`, then `get-brand-template-dataset` per template. Record field names and types only |
| Pro-plan capabilities actually available | whether autofill, brand templates and resize are callable (the proposal says Pro; the creator is on Pro per ROADMAP 2026-10-13) |
| Rate and size limits as enforced | proposal figures (export 20/min and 500/day, autofill 60/min, export URLs expire in 24 h) are from Canva docs and UNVERIFIED live; max text length per field is unknown |
| Font behavior | whether Newsreader and Inter (our tokens) are in the Brand Kit or fall back; `Newsreader` and `Inter` are not bundled in this repo either |

## What the creator still needs to set up in Canva

1. Authorize the connector: `/mcp` > claude.ai Canva > authenticate (one-time OAuth in their browser).
2. Brand Kit: enter the palette and fonts. `ave content canva-brief` prints them from `motion-kit/brand/tokens.json` (ink #1b1814, ink_soft #2a2620, paper #f3efe7, paper_dim #d9d3c7, stone #9a9386, clay #b9825a; Newsreader serif, Inter sans). It is a manual, one-way push; the API cannot be assumed to write a Brand Kit.
3. Build 2 to 3 Brand Templates, at the exact target size (quote card 1080x1350 first), with **text fields** named for the slots, for example `QUOTE`, `BY`, `ROLE`, `SHOW` (quote-post). Only text fields are supported for now; no photo or chart fields; no carousel.
4. Give the ids to this install, not to the repo: `~/.config/ave/content.yaml` (see `config/content.yaml`, `renderers.canva`).
5. A first live session: list brand kits/templates/datasets (reads), then run ONE job with the human approving the writes, compare with the in-house render, and record the real limits, font behavior and tool names here.

## What was built meanwhile (all tested with a fake client, no live calls)

`src/ave/content/render_canva.py` (`CanvaClient` protocol, job files, approval gate, rate limiter, import, recorded fallback), `ave content render --renderer canva|canva-brief|canva-status|canva-approve|canva-log|canva-import|canva-fail`, `skills/ave-canva-render`.
The protocol and tool names (`get-brand-template-dataset`, `autofill-design`, `resize-design`, `export-design`) come from the proposal's research, not from the connector, so they are the first thing to verify.
