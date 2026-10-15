---
name: ave-assembly
description: Create the assembly edit, the first of three cut stages (assembly, rough, final): all cameras synced and switched by who is speaking, with nothing removed. Use after cameras are mapped and a cut list is planned, or when the user asks for the assembly, including per-session assemblies of a multi-session recording.
---

# Assembly edit

The assembly is the whole conversation, in order, with the right camera on the right speaker. Nothing is cut out yet. It
is the safe base the rough cut (cleanup) is made from, and the first thing the human reviews.

```bash
ave plan-cuts <episode>          # shot/reverse-shot plan -> work/cutlist.json   (skill: ave-rough-cut for taste notes)
ave assemble <episode>           # -> edits/assembly.vN.json   (an EditDoc; see docs/superpowers/specs)
ave render <episode> --stage assembly --preset proxy     # quick 540p look, optional
ave handoff <episode> --stage assembly                   # put it in Resolve for the human (skill: ave-resolve-handoff)
```

`--canvas 1920x1080` (default) sets the output size; sources of any size are scaled with letterboxing, never distorted.

## Sessions and excluded regions (several takes on one recorder)

Episode time is the recorder's timeline. When `episode.json` assigns cameras to sessions, each session is planned and assembled on its own:

```bash
ave plan-cuts <episode>                  # plans EVERY session -> work/cutlist.s1.json, cutlist.s2.json   (or --session s1)
ave assemble  <episode>                  # one NAMED edit per session: edits/assembly.s1.vN.json, assembly.s2.vN.json   (--session s1 for one)
ave assemble  <episode> --combined       # sessions joined, uncovered gaps removed -> the unnamed edits/assembly.vN.json
ave cleanup <ep> --name s1   ;   ave final <ep> --name s1   ;   ave render <ep> --name s1   ;   ave handoff <ep> --stage assembly --name s1
```

A session's assembly covers **only the spans where its cameras exist**: the union of camera coverage (drift included) clipped to the audio and transcript. Lead-in,
dead space between sessions and the tail are EXCLUDED, never an error; the cut list and the EditDoc list them (`regions`, `excluded`), and `e0` keeps recorder time so
captions and cleanup still line up. Check `excluded` against what you know ("4.3 s lead-in, 84.2 s dead space, 3.2 s tail"); an excluded region where people are talking
on camera means a sync problem, not dead space. A hole inside a session (one camera stopped before the other started) is skipped the same way.
Single-session episodes are unchanged: no flags, no names, one `assembly.vN.json`. `ave status` / `ave resume` list each session's stages; a stage counts as done when every session has it.

The session that is "experimental" or otherwise different is the human's call: keep it as its own timeline and let them decide whether it joins the combined cut.

## Trim: the real start and end (a checkpoint BEFORE cleanup)

Recordings open with setup talk ("take one... we're recording... can you hear me") and end with talk about stopping; a session's assembly starts at the first camera frame.
Find the real in/out with the human before cleanup:

```bash
ave plan-cuts <ep>                                   # the trim scan uses the session span from the cut list
ave trim suggest <ep> [--session s1]                 # evidence lines with timestamps + a proposed in/out and confidence. NEVER applies anything
ave trim set <ep> s1 --in 188.9 --out 7022.8         # record the human's decision in episode.json `trim` (episode seconds; `main` for an episode with no sessions)
ave assemble <ep> --session s1                       # honours the trim: shorter span, the cut-off parts join `excluded`, still validated
```

`suggest` looks in the first and last `scan_minutes` (6) for phrases in `config/trim.default.yaml` (slate: "take one", "we're recording", "testing", "move your mic"; the welcome; the sign-off; "stop
recording", "we're done") and for show-specific lines in `episode.json` `trim_hints: {"welcome": ["this is the long table"], "signoff": [...]}`. Read the evidence, **listen at the proposed time**, and
stop for the human: a bare "welcome" is often a rehearsal joke (confidence 0.4), the sign-off may be followed by banter worth keeping, and a quiet start may need no trim at all.
`ave resume` lists a suggestion nobody has approved as an open question. Changing the trim means re-running `ave assemble`, then `ave cleanup` (new versions; nothing is overwritten); `ave trim clear <ep> s1` forgets it.

## Check before you hand it over

- `ave status <ep>`: `assembly` done, versions listed. Re-running `ave assemble` writes the NEXT version; it never overwrites.
- Read the EditDoc summary: video segment count, duration equals the transcript length (or, for a session, the sum of its `regions`), both cameras used.
- Fallback reasons in `reason` ("camera not recording") mean a camera started late; tell the human.
- Validate: `python -c "from ave import editdoc; print(editdoc.validate(editdoc.load('edits/assembly.v1.json')))"` must print `[]`.

## Checkpoint

Tell the human what you built (length, shots, which camera covers which speaker) and ask them to review it in Resolve. Say
plainly that nothing has been cleaned up yet. Only after they confirm, move on to `ave-cleanup`.
