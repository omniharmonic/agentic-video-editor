---
name: ave-content-review
description: Build the contact-sheet review page for rendered content, apply the human's approve/reject/nudge decisions, and queue approved images and carousels into the approval-gated publish queue. Use after `ave content render`. The human decides; you never approve or publish.
---

# Review, decide, queue

```bash
ave content review <ep>                  # content/review/index.html: every item, the critic's numbers, source with the quote marked
# the human opens it, decides, downloads decisions.json
ave content decide <ep> ~/Downloads/decisions.json --by <name>     # new doc version; refuses a changed file or an unflagged-critic bypass
ave content queue <ep> --dry-run         # what would be queued
ave content queue <ep>                   # APPROVED items -> publish queue as image/carousel items (never approves, never sends)
```

## What the human sees and does

Each card: the images (phone-size toggle), the source passage with the quote highlighted, who said it and when, critic chips (fit,
contrast, text size) and notes, your reason and confidence, alt text, caption, platforms. Buttons: approve, reject, nudge (another
style or format). Keys: j/k, a, r, u, d. Open the page for them (`open <index>`) and tell them what to look for; do not decide for them.

## Rules for you

- `decide` approves only: a rendered item whose files still match their recorded hashes, alt text on every image, a passing critic
  (override needs `"override": true` plus a note the HUMAN gave). Do not write an override yourself.
- A nudge clears the render. Run `ave content render` again, then review again. An alt-text-only nudge needs no re-render.
- Rejected items stay rejected; propose a new item rather than editing one.
- `queue` skips platforms whose adapter publishes video only (YouTube, Facebook today) and says so. Visibility defaults to private;
  the aggregator (Instagram, X, TikTok) cannot hold a post back and will leave the item as a draft with that problem until the human
  passes `--visibility public` deliberately.

## The publish gate is still the human's

Queued image items show their files and alt texts at approval. STOP and tell the human the queue ids and ask them to run:

```bash
! ave publish approve <ep> <id>          # they type the first 8 chars of the sha256 they reviewed; covers EVERY file and the alt text
```
A carousel's hash covers every slide in order: changing, adding, removing or reordering a slide voids the approval. Then
`ave publish run <ep> <id>` is a dry run; only with the human's word, `--yes`. Never set `AVE_ALLOW_UNATTENDED_APPROVAL`. Never post,
spend money, or call Canva without explicit approval in this session. See `skills/ave-publish`.

## Failure modes

- "changed on disk since it was rendered": someone edited a PNG. Re-render; the old approval is void.
- Queue item stuck in `draft`: read `preflight_problems` (alt text missing, aspect or size outside `config/platforms.yaml` `image:` limits,
  carousel too long for the platform). Limits are marked `verified: false` until checked against the platform's current docs.
