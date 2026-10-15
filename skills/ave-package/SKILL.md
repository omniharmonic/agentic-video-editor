---
name: ave-package
description: Assemble the release package for an episode (final video, SRT + VTT, podcast audio with chapters, thumbnails, approved shorts, quote cards and carousels, per-platform post copy drafts, manifest, index.html) and queue its approved items as drafts. Use when the human says "deliver the package", "get this ready to post", "what is missing before we can publish", or when `ave resume` shows the package stage. Builds files only: it never sends, posts, approves or calls Canva or Resolve.
---

# Release package

```bash
ave package <ep> --dry-run                  # the checklist, sizes and free space; writes NOTHING. Always start here
ave package <ep>                            # build <exports>/package/<date>/   (--date D, --include video,captions,audio,thumbnails,shorts,images,copy,drafts)
ave package queue <ep> --dry-run            # what would enter the publish queue
ave package queue <ep> --ai-disclosure no   # approved items -> publish queue as drafts / awaiting_approval (the human answers the AI question)
```
Optional: `--audio mp3|aac|both`, `--new-version`, `--strict` (exit 2 if a required piece is missing), `--dry-run --write-status` (writes only `work/package/status.json`).

## What you get

`<exports>/package/<date>[-v2]/` with `episode/` (the latest `final*.mp4`), `captions/` (the exact-match SRT from the final + VTT), `audio/` (MP3/M4A at -16 LUFS,
**measured after encoding**, ID3 chapters, plus `chapters.txt` in YouTube's format), `thumbnails/` (1280x720, under 2 MB; an oversize PNG becomes a JPEG),
`shorts/<platform>/` (approved clips, latest export per platform), `images/` (approved quote cards and carousels, alt text in the manifest and `copy/alt-text.md`),
`copy/` (per-platform drafts + `copy.json`), `drafts/`, `manifest.json` (every file: size, sha256, duration, source stage and version, approval, license credits),
`checksums.sha256`, `index.html` (players, thumbnails, copy buttons, sizes), `README.md`. The same checklist is written to `work/package/status.json` for `ave ui`.

## What to tell the human, in this order

1. The checklist from `--dry-run`, plainly: "final cut not rendered", "no approved clips", "no thumbnail". These are the work left, not errors. Do not build around gaps with made-up files.
2. Size and free space (printed before anything is copied). If it does not fit with the 512 MB reserve, big files are **referenced by path** in the manifest with a flag (ExFAT has no
   hardlinks or symlinks, and we use neither); say which.
3. After the build: the package folder, anything under `drafts/`, and the post-copy problems.

## Rules

- **Only approved things are in the package.** Clips still `proposed`, content still in review, a short that missed its platform limits, a thumbnail that is the wrong size: all go to `drafts/`
  with a `DRAFT-` file name and a reason. Approving is the human's (`ave clips review`, `ave content review`); you do not decide for them and you do not flip a status to fill the folder.
- **A package folder is never overwritten.** Re-running with nothing changed says `unchanged`; a change makes `-v2`, `-v3`. Do not delete an old package.
- The final cut's human approval is not machine-recorded (manifest says `unrecorded`); ask the human to confirm they signed it off before they publish.
- Chapters: `work/chapters.json` (`[{"start": seconds in the FINAL video, "title"}]`) wins. Otherwise topic breaks are derived from the transcript and mapped through the final EditDoc; the
  titles are keyword drafts and the checklist says so. YouTube ignores fewer than 3 chapters or a first one not at 0:00.
- Loudness: the audio file is normalised to -16 LUFS and re-measured; a result outside +-1 LU is a `warn` with the numbers. Report it, do not hide it.

## Post copy: draft, in the creator's voice, for them to edit

The copy is built from templates, facts on disk and **verbatim quotes the human already approved** (approved quote cards, approved clips' hook quotes). No invented claims, no summary
you did not read. Never shorten a quote: if it does not fit a platform's limit another approved quote is tried, then none. What we write ourselves must pass the critic in
`config/package.default.yaml` (no hype or corporate phrases, no exclamation marks, no emoji, no capitals for emphasis, no question bait, few hashtags); problems are listed under each block, not silently fixed.
Still look at every block yourself: the creator's brief is authentic and grounded, never cheesy. Limits come from `config/platforms.yaml` (unverified numbers; LinkedIn, Threads and Bluesky
are `via: manual`: nobody publishes there for you). Placeholders `{{EPISODE_URL}}`, `{{LINKS}}` and `TODO:` lines are the human's to fill; the checklist lists them. `copy/copy.json` is the editable
source `queue` reads.

## Queueing, and where you stop

`ave package queue` verifies every packaged file against the manifest, then calls the ordinary publish queue for approved items only: the episode video (YouTube, Facebook), each short on its
platform, quote cards and carousels (platforms whose adapter publishes video only are skipped and reported). Visibility is private. Items carry an origin key, so queueing twice duplicates nothing.
YouTube items stay `draft` until the AI-disclosure answer is given (`--ai-disclosure yes|no` is the human's statement, not yours to guess).

Then STOP. Tell the human the queue ids and ask them to run `! ave publish approve <ep> <id>` for each (they type the first 8 characters of the sha256 of the exact file). You never approve, never
pass the send flag, never post. See `skills/ave-publish`.

## Failure modes

- "no package yet": run `ave package` first. "changed since the package was built": a packaged file was edited; rebuild with `--new-version`.
- Audio encode fails with "no mp3 encoder": use `--audio aac`.
- Copy blocks marked "over the limit": the template could not fit even without optional parts; the human trims it.
