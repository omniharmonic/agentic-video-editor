---
name: ave-publish
description: Queue finished videos or shorts for YouTube, Facebook (and Instagram/X/TikTok through an experimental aggregator), get the human's approval, and publish. Use when the human asks to post, upload, schedule or publish an episode or clip, or when `ave resume` shows the publish stage as next. Posts to real public accounts: follow the checkpoints exactly.
---

# Publish (approval-gated)

Nothing leaves the machine until a HUMAN approves the exact file and then a run is started with `--yes`.
You queue and dry-run. You never approve and never pass `--yes` on your own initiative.

## Steps

```bash
ave publish status <ep>                        # which platforms have credentials, any uncertain items
ave publish queue <ep> --video exports/final.mp4 --platforms youtube,facebook \
    --title "..." --description "..." --tags a,b --ai-disclosure no|yes [--schedule 2026-11-01T09:00:00-06:00]
ave publish queue <ep> --from-clips --platforms youtube,facebook --ai-disclosure no   # every exports/shorts/*.mp4
ave publish show <ep> <id>                     # read back the exact metadata and problems
ave publish run <ep> <id>                      # DRY RUN (the default): shows what would be sent, sends nothing
```

Then STOP and tell the human: the item id, title, platforms, visibility, schedule, the sha256, and any preflight
problems. Ask them to watch the exact file. The human approves:

```bash
ave publish approve <ep> <id>                  # prompts; they type the first 8 chars of the sha256 they reviewed
#   You cannot do this step: it refuses without a real terminal and --confirm-sha is disabled. Tell the human to run
#   `! ave publish approve <ep> <id>` (the leading ! runs it in this session). Never set AVE_ALLOW_UNATTENDED_APPROVAL; that is for CI only.
```

Only after the human says to publish in this session: `ave publish run <ep> <id> --yes`.

## Judging the output

- `status: draft` means preflight failed (read `preflight_problems`): wrong aspect for Reels/Shorts, too long, missing
  `--ai-disclosure`, title over 100 chars, schedule in the past. Fix the cause and queue again; do not argue with the limits.
- Defaults are conservative: visibility is `private`. On Facebook a non-public post is uploaded as an unpublished DRAFT.
  Aggregator posts must be `--visibility public` because they cannot be held back. Only set `public` when the human said so.
- Re-encoding or editing the file or any metadata after approval invalidates the approval. Queue/approve again.
- Reading `results`: `published` has a url; `failed` was refused by the platform (safe to re-run after fixing);
  `sending` means UNKNOWN (crash or network error). Never re-queue to "fix" it; that risks a duplicate post.

## Failure modes

- Quota exhausted (YouTube ~1600 of 10,000 daily units per upload): nothing was posted; retry after the reset.
- "UPLOADED BUT FORCED PRIVATE" warning: the Google API project is unaudited so YouTube locks API uploads private.
  Tell the human plainly; they can publish it in YouTube Studio or complete the audit (docs/publishing-setup.md).
- Uncertain item: ask the human to look at the channel/page. If it is there: `ave publish resolve <ep> <id> <platform> --posted URL`.
  If not: `--retry`, then run again with `--yes` (with the human's go-ahead).
- Missing credentials: `ave publish auth youtube --client-secrets FILE` / `auth facebook --page-id ID` /
  `auth aggregator` are interactive and keychain-only. Never ask the human to paste a token into chat, and never write
  tokens to files in the repo.
- Instagram, X and TikTok have no direct adapter. They go through the aggregator (experimental, needs
  `~/.config/ave/aggregator.yaml` built from `config/aggregator.example.yaml`).

Limits in `config/platforms.yaml` are marked `verified: false` until checked against the platform's current docs.
