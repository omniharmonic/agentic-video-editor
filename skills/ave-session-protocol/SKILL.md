---
name: ave-session-protocol
description: Start-of-session ritual, running log, and learning loop for any ave project. Use at the START of every session ("Resume <project path>", or any request to edit/continue an episode), after every finished step, when you need to ask the human something, when a tool failed or an assumption turned out wrong, at the END of a session, and when handing a project to a different agent.
---

# Session protocol: memory that survives a cut-off session

Chat history is gone when a session ends or is cut off. The project folder's `PROGRESS.md` and the files on disk are the memory. Treat them as the source of truth, in this order: files on disk (`ave status`), then `PROGRESS.md`, then what you remember.

## 1. Start of every session (do all three, in order)

```bash
ave doctor                    # fix FAIL lines first (ave-setup-check)
ave resume <project>          # stages done/next, what the human is reviewing, OPEN QUESTIONS, last 5 log lines
```
Then read `<project>/PROGRESS.md` (decisions, field notes, anything the human wrote). `Resume <project path>` from the human means exactly this.
No `PROGRESS.md`? `ave progress <project>` creates it (a new project: `ave init-episode <dir>`, or `ave adopt <dir>` for an existing folder; both leave existing files alone).
If the human answered a question in chat, record it: `ave log <project> "the answer" --resolve N`.

## 2. Log after EVERY step (one line, say why)

```bash
ave log <project> "what was done and the evidence" --kind note --step rough
ave log <project> "use camera A as the wide, B is soft" --kind decision        # decisions need the reason in the sentence
ave log <project> "which of the two intros do you want?" --kind question       # numbered; shown by `ave resume`
ave log <project> "answer text" --resolve 3                                    # closes question 3
ave log <project> "drive unmounted, cannot read footage" --kind blocker
ave log <project> "rough cut approved by the human" --kind done --step rough   # also refreshes the checklist
ave progress <project>                                                         # regenerate the checklist from disk, print a summary
```
Stage commands already write a line themselves (`mark_stage`, review hand-offs), so do not duplicate them; log what the tools cannot know: judgments, human feedback, surprises.
Checkpoints: when a stage needs the human, log a `question` (what to review, what you recommend) and STOP. Do not proceed on a guess.
Humans may edit `PROGRESS.md` anywhere except between the `ave:checklist` markers. Never rewrite the file by hand to "tidy" it; use the commands, or append with an editor and leave their text alone.

## 3. The learning loop

Whenever a tool failed, an assumption was wrong, or you needed a manual step the skills did not mention, record it immediately:

```bash
ave learn <project> "what happened and what is true instead" --skill ave-captions --change "the edit that would have prevented it"
ave learn <project> --list              # proposed notes
```
Notes start as `proposed`. At the END of a session, list the proposed notes and promote the generalizable ones into the repo (not project-specific facts):
1. Edit the affected skill or doc so the next agent does not repeat it. Describe real behavior, with the numbers you saw.
2. If code changed, add a regression test first (see `docs/field-notes/README.md`).
3. Commit in the repo (the human commits, or ask), then `ave learn <project> --apply F3 --commit <sha>`.
Project-specific facts stay in `PROGRESS.md`; durable lessons also go to `docs/field-notes/`.

**Documentation must reflect what actually worked in the real world, not what we planned.** If a skill says one thing and the footage did another, the skill is wrong: fix it.

## 4. End of session

`ave progress <project>`, log where you stopped and the exact next command (`ave log ... "stopped after X; next: Y"`), list `proposed` notes, tell the human what is waiting on them.

## 5. Handing a project to a different agent (or machine)

Nothing is needed beyond the folder: the project-root `AGENTS.md` names the repo and the resume steps, `PROGRESS.md` holds the story, `episode.json` its layout and speakers. The new agent starts a session in the repo and says `Resume <project path>`. If the repo moved, rerun `ave adopt <project>` after deleting only the stale `AGENTS.md`. No symlinks anywhere (the drive may be ExFAT).

## Failure modes
- `ave log` says "no open question N": read the numbers from `ave resume`.
- Checklist looks wrong: it is regenerated from disk; fix the missing/extra artifact, not the Markdown.
- A drive path with `._*` files: ignored by ingest on purpose (AppleDouble). Never delete them; they are not yours.
