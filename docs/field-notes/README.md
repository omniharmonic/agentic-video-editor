# Field notes: from a real project back into the tools

Every project's `PROGRESS.md` has a **Field notes** section filled by `ave learn`. This folder is where lessons that outlive one project go, so the
next episode benefits. Rule: **documentation must reflect what actually worked in the real world, not what we planned.**

## Loop
1. During a session, any failure, wrong assumption or manual step: `ave learn <project> "..." --skill NAME --change "proposed edit"` (status `proposed`).
2. End of session: `ave learn <project> --list`. For each note decide: project-specific (leave it in `PROGRESS.md`) or generalizable (promote).
3. Promote: edit the affected skill/doc (real numbers, real commands), add a regression test when code changed (write it failing first), run the suite.
4. Mark it: `ave learn <project> --apply F3 --commit <sha>`. Status becomes `applied`. A note we decide not to act on gets `wontfix` and a reason written in this folder.
5. Write or extend a dated file here (`YYYY-MM-DD-<project>.md`) with the note in the template below, so the history is readable without the project drive.

## Template
```
## N<k>. <short title>  (status: proposed | applied <commit> | wontfix)
- Assumed: what the tool/skill/we believed.
- Found: what was true, with the measurements.
- Change: what changed (or should change) in code, skill, doc, test.
```

## Promotion checklist
- [ ] The skill/doc now says the true thing, including the numbers that surprised us.
- [ ] A test fails without the fix (unit tests that pass while the product is wrong do not count; prefer an end-to-end check).
- [ ] Policy numbers live in YAML, not code.
- [ ] `PROGRESS.md` note marked `applied` with the commit.

## About the notes in this folder
The dated files here are working notes from the first real episode (a two-host, two-camera podcast). They are internal history kept as evidence:
each says what we assumed, what the footage showed, and what changed. They quote real numbers from that project and were written while the tools
were still changing, so a command or default mentioned in a note may have moved on; the skills and reference docs are authoritative. If you add a
note from your own project, anonymise it first (no private paths, no names of people who did not agree to appear, no more verbatim speech than the lesson needs).
Index of all docs: [../README.md](../README.md).
