# `ave package --dry-run` on the first real episode, 2026-10-14 (workstream P2)

> **Working note from the first real episode.** This is internal history kept as evidence: what we assumed, what a real recording showed, and what changed. It describes one project (a two-host, two-camera podcast), uses its real numbers, and was written while the tools were still changing, so commands and defaults may have moved on. For the current behaviour read the skill or doc a note points to. See [docs/README.md](../README.md).

Command, run read-only against the real project on the drive (nothing was written: no `06_EXPORTS/package/`, no `work/package/`):

```
ave package <episode-root> --dry-run
```

Volume: 1.93 TB free of 2.0 TB, so every file would be copied (the free-space reserve is 512 MB). Planned package size: 0.3 MB, all of it generated text, because
there is no media to package yet. The result is `complete: false`. What it reports, in the checklist's own words:

| Piece | State | What it said |
|---|---|---|
| Final cut (required) | MISSING | "final cut not rendered (final EditDoc exists: s2 v9; render it with `ave final`/`ave render`)". `06_EXPORTS` holds `rough_cut.mp4`, `rough.s2.v2.proxy.mp4`, `test export.mp4`; no `final*.mp4`. A final render of `final.s2.v9` was in progress in `01_PROJECT/work/render/` while this ran (another session's job, not touched). Only session s2 has a final EditDoc at all: there is no combined episode final and no session s1 final. |
| Captions SRT + VTT (required) | MISSING | no SRT for the final; the only SRTs are `captions/rough.s2.srt` (a rough cut). |
| Podcast audio with chapters (required) | MISSING | nothing to take the audio from until a final is rendered. |
| Chapters | not reached | there is a `work/transcript.json` (8437 s) but no `work/chapters.json`; with a final EditDoc the package derives topic breaks and maps them into video time, with machine-draft titles (not exercised on real data yet: it needs the final). |
| Thumbnail 1280x720 under 2 MB (required) | MISSING | no `thumbnail` item in the ContentDoc (`work/content/main.v2.json` has three quote-post items and nothing else). |
| Quote cards and carousels | DRAFT | 3 images exist (`quote-q1` 4x5, `quote-q2` 1x1, `quote-q3` 9x16) and all three are `draft` in review, so they would sit in `drafts/` marked DRAFT, none in the main package. No carousel exists. |
| Shorts | MISSING | "no approved clips (118 proposed)": every clip in `work/clips.json` is `proposed`, none `approved`. There is no `exports/shorts/` folder either (only `06_EXPORTS/shorts_drafts/`, which the package does not read). |
| Post copy drafts | WARN | 10 blocks on 8 platforms were composed (YouTube title/description/tags, episode posts for Instagram, Facebook, X, LinkedIn, TikTok, Threads, Bluesky). 7 carry the problem "no approved quote to use yet", because no quote card or clip is approved, so the posts carry no quote. Placeholders left for the human: `{{EPISODE_URL}}`, `{{LINKS}}`, one `TODO` line (the plain-sentence summary). |

## What this means for "deliver a completed package today"

The blockers are all upstream of packaging, and the checklist names each one:
1. **Render the final** (session s2 final is v9 and rendering; s1 has no final; decide whether the episode ships as one combined final). Captions (exact-match SRT) come with it.
2. **Approve something**: at least one clip in `ave clips review` (and then `ave clips build --approved-only`, `ave shorts export --all-approved`), and the quote cards in `ave content review`.
3. **Make a thumbnail** (`ave content plan`/`propose` with template `thumbnail`), then render and approve it.
4. Optionally write `work/chapters.json` (start seconds in the final video, title) instead of the machine-drafted ones.
Then `ave package <ep>` builds the folder, and `ave package queue <ep>` queues the approved items as drafts for the human's own approval.

## Learnings from building it

## P1. A final EditDoc can exist for a long time before its render does  (status: applied in this workstream)
- Assumed: "no final cut" means "no final stage was run".
- Found: on the real project nine `final.s2.vN` EditDocs exist, yet there is no `final*.mp4` in the exports folder. Saying only "final cut not rendered" hides that the work is one render away.
- Change: the checklist now names the final EditDocs it found.

## P2. Dry runs must be able to touch nothing at all  (status: applied in this workstream)
- Assumed: the checklist file for the UI (`work/package/status.json`) could be written on every run.
- Found: the first real run had to be read-only on the creator's drive while another session was rendering into it.
- Change: `--dry-run` writes nothing; `--dry-run --write-status` writes only `work/package/status.json` (a test asserts that is the only new file).
