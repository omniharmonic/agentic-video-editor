---
name: ave-content-plan
description: Plan promotion content (quote posts, thumbnails, carousels, reel cards) from an episode's approved clips or the creator's own Markdown writing. Use when the human asks for quote graphics, a thumbnail, a carousel or social visuals, after selects are approved; start with `ave content plan`. Opt-in feature (config/content.yaml).
---

# Plan content: choose the words, say why, let code check them

You choose; the tools verify. A pick is a template, a style, a format, the words, and `reason` + `confidence`. Nothing renders or
posts at this step. If `ave content` says it is switched off, tell the human how to turn it on (their `~/.config/ave/content.yaml`
or `AVE_CONTENT_ENABLED=1`); do not edit the repo default.

## Steps

```bash
ave content templates <ep>                    # registry: slots and limits, the style x format matrix
ave content plan <ep>                         # approved clips' quote candidates (+ speaker, time), episode facts, ingested writing
ave content ingest-writing <ep> <folder>      # the creator's Markdown -> passages p001 and sentences p001.s1
ave content propose <ep> picks.json           # validates every pick, writes content/<id>.vN.json; exit 2 lists every problem
```

A pick (see `ave content templates` for each template's slots):

```json
{"template": "quote-post", "style": "ink", "format": "4x5", "source": {"clip": "c01"},
 "slots": {"quote": "A goal is a wish and a system is a habit", "attribution": "Tomas", "show": "The Long Table"},
 "platforms": ["instagram"], "caption": "optional; else built from the slots",
 "reason": "The line the conversation turns on; one breath long.", "confidence": 0.85}
```
Sources: `{"clip": "c01"}` (APPROVED clips only; on a multi-session episode ids are prefixed by the session, `{"clip": "s2-c01"}`, and the plan lists each clip's `session`), `{"writing": "soil.md", "sentences": ["p003.s2"]}` or `"passages": [...]`, `"episode"` (title/guest facts for thumbnails and cards).

## The one hard rule: quotes are verbatim

A slot the template marks `x-verbatim` must appear word for word in the pick's source (case and punctuation ignored). A paraphrase,
an ellipsis, or `[bracketed]` edits are rejected, and the rejection shows the closest passage. To shorten, choose a shorter
continuous passage; never cut words out of the middle. Point slides in a carousel are your own summary: never set them in quotation marks.
Names: `attribution` is the speaker's name (a host's name and the title CO-HOST fill in from `episode.json` `speakers`; set `title` only for a guest or
an exception). The show name goes in `show` ONLY, once; never as a role, title or subtitle (propose rejects it). The old slot name `role` still loads as `title`.
Check attribution against `speaker_name` in the plan: the transcript, not your memory, says who spoke. Propose warns on a mismatch.

## Taste (the creator's brief: authentic, grounded, never cheesy or corporate)

- Words first. Pick lines that stand alone: concrete, a little surprising, in the speaker's own voice. Avoid slogans, hype, motivational
  poster lines, rhetorical questions as bait, and anything that needs the episode to make sense.
- Prefer candidates with `disfluencies: []`. "is is so much" is verbatim but reads as a typo on a graphic; choose a cleaner line.
- Few and good beats many: the plan limit is `planning.max_items_per_episode` (12). Two or three strong quotes and one thumbnail is a good episode.
- One style per set (ink for most, paper for quieter lines, clay sparingly). Do not vary style to fill space. No emoji, no hashtags in graphics.
- 4x5 for feed quotes and carousels, 1x1 only when a platform needs it, 9x16 for stories. Thumbnail titles: a few words, not a sentence.
- Guest photos need the guest's consent and a license-log entry (`ave assets log-license`); list the file in `assets`. Never use AI-generated
  imagery unless the human said so (then `ai_generated: true`).

## Checkpoint: STOP and show the human the plan

Give them: each item (id, template, style, format, platforms), the exact quote, who said it and when, your `reason` and `confidence`,
any warnings. Ask them to trim. Do not render until they have. Never publish, spend money, or touch Canva (phase 2) without an explicit
approval in this session. Next: `skills/ave-content-render`.

## Failure modes

- "clip c02 is proposed, not approved": ask the human to approve it in `ave clips review`; do not pass `--allow-unapproved` on your own.
- "does not appear verbatim": you paraphrased or merged two places. Re-read the source text the error shows.
- "Sources changed": the clip or Markdown changed after the plan; re-ingest and re-propose.
