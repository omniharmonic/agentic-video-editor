# Getting started: your first episode

This walks one multicam podcast episode from raw footage to a release package, with the real commands in order. Lines marked
**HUMAN CHECKPOINT** are where you decide before anything moves on. Normally you tell your agent "edit episode X, start with the ave-pipeline
skill" and it runs these for you; this page is for knowing what it is doing, or doing it by hand.

Time to expect: setup 5 to 15 minutes (the FluidAudio build is the long part), the demo 10 seconds, a real two-hour episode a few hours of machine time at 4K
plus your review time.

## 0. Install and prove it works (10 minutes)

```bash
git clone https://github.com/omniharmonic/agentic-video-editor && cd agentic-video-editor
./scripts/setup.sh --minimal        # or ./scripts/setup.sh for everything this machine can do
source .venv/bin/activate           # or prefix commands with .venv/bin/ or `uv run`
ave doctor                          # required / real-footage / optional, with the fix command per gap, and a verdict line
ave demo                            # the whole pipeline on generated footage; no footage or network needed
```

You need FFmpeg and `uv` first; `setup.sh` tells you the exact install command if either is missing. For **real footage** you also need a
transcription engine: on Apple Silicon `./scripts/setup.sh` builds FluidAudio (needs `xcode-select --install`); elsewhere install whisper.cpp
and run `scripts/fetch-whisper-model.sh`. `ave doctor` shows which you have. Resolve is optional; see step 8.

## 1. Create the episode and add footage

```bash
ave workspace ~/Editing                 # once: the folder that holds all episodes (outside this repo)
ave init-episode ep-001                 # creates ~/Editing/ep-001/{raw,work,edits,exports}, episode.json, PROGRESS.md
```

Copy your camera files and the recorder audio into `~/Editing/ep-001/raw/`. Copy, do not move: the originals stay yours, and the tool never
modifies anything in `raw/`. Already have a folder layout? `ave adopt <folder>` or `ave init-episode <folder> --layout numbered` only adds what is missing.

Start your agent in this repo and say: *"Edit episode ep-001. Start with the ave-pipeline skill."* Every session begins with `ave doctor` and
`ave resume ep-001`, which print what is done, what is next, and any open questions.

## 2. Ingest, audio, transcript

```bash
ave ingest ep-001                       # probe every file: duration, fps, channels, sessions -> work/media.json
ave audio ep-001                        # 16 kHz analysis audio (the originals are not touched)
ave transcribe ep-001 --speakers 2      # words with timing + speaker labels -> work/transcript.json
#   no FluidAudio? add:  --asr whisper
```

**HUMAN CHECKPOINT: speaker names.** Speakers come out as S1, S2. The agent reads a few lines of each and proposes names; you confirm. If the
recorder has one mic per channel, speakers come from the channels directly (more reliable than diarization).

## 3. Sync the cameras and map them to speakers

```bash
ave sync ep-001                         # offsets + clock drift per camera -> work/sync.json (warns when a file does not match)
ave frame ep-001 raw/camA.mp4 120       # a still, so the agent (or you) can see who is on which camera
```

**HUMAN CHECKPOINT: camera mapping.** The agent shows a still per camera and proposes "camera A is the host, B the guest"; you confirm, and it
records that in `episode.json`. Check the sync warnings; a weak match means that file shares no audio with the reference.

Optional: `ave trim suggest ep-001` proposes the real start and end (slate talk, welcome, sign-off). It never applies them; `ave trim set ep-001 s1 --in S --out S` does,
and that is your call.

## 4. Assembly

```bash
ave plan-cuts ep-001                    # shot / reverse-shot plan -> work/cutlist.json
ave assemble ep-001                     # edits/assembly.v1.json: every angle, synced, nothing removed
ave render ep-001 --stage assembly --proxy    # exports/assembly.v1.proxy.mp4: a small draft you can watch
```

**HUMAN CHECKPOINT: the assembly.** Watch the draft (or open it in Resolve, step 8). Is the right person on screen when they speak? Are
the cameras in sync? Tell the agent what is wrong; it re-plans and writes a new version. Nothing is overwritten.

## 5. Rough cut

```bash
ave cleanup ep-001                      # edits/rough.v1.json: dead air shortened, fillers and false starts removed, every cut masked
ave cleanup explain ep-001              # every decision, with the reason and a confidence
ave render ep-001 --stage rough --proxy
```

**HUMAN CHECKPOINT: the rough cut.** Listen for cuts that feel wrong. The numbers (pause lengths, filler lists) are in
`config/cleanup.default.yaml`; override per show in `~/.config/ave/cleanup.yaml` or `<episode>/policy/cleanup.yaml` rather than editing code.
See [cleanup-policy.md](cleanup-policy.md).

## 6. Opening and closing (optional)

```bash
ave sequence plan ep-001                # what the opening/closing would contain, and what is missing (links, editor credit)
ave sequence preview ep-001
ave sequence build ep-001
```

**HUMAN CHECKPOINT: you run `ave sequence approve ep-001`.** The agent cannot. See [sequences.md](sequences.md).

## 7. Final cut

```bash
ave captions ep-001 --stage rough --style podcast     # SRT/ASS; `ave captions check` verifies timing
ave assets ...                                         # only if you use music or B-roll: every file needs a license-log entry
ave final ep-001 --opening --closing                   # music (ducked), motion graphics, captions, two-pass loudness, exact-match SRT
```

`ave final` refuses unlicensed assets. It re-transcribes its own render so the SRT matches the audio exactly.

**HUMAN CHECKPOINT: the final cut.** Watch the whole thing with sound on.

## 8. Review and tweak in DaVinci Resolve (optional, macOS)

Do the human-only setup that `setup.sh` printed (Resolve 21.0.x free or Studio, `scripts/persist-python3home.sh`, install the bridge), then in
Resolve open a project and run **Workspace > Scripts > resolve_bridge**.

```bash
ave doctor                              # Resolve bridge should say "listening"
ave proxies ep-001 --dry-run            # optional: lighter review proxies for 4K footage; `ave handoff --use-proxies` links them
ave handoff ep-001 --stage rough        # creates a NEW timeline, read back and verified; add --layers to keep the other angles
```

The agent may say "it is in Resolve" only after `ave handoff` prints `"ok": true`. Timelines are only created, never modified.

## 9. Clips, shorts and the release package

```bash
ave clips prepare ep-001 && ave clips propose ep-001    # candidates scored against config/clips.rubric.yaml
ave ui ep-001 --open                    # the review desk: play each clip in context, mark keep / maybe / skip
ave shorts build ep-001 --all-approved  # vertical 9:16, captioned, from the clips you approved (look first with `ave shorts preview`; then `ave shorts export`)
ave package ep-001 --dry-run            # what would ship; then run it without --dry-run
```

**HUMAN CHECKPOINT: clip selection.** Your keep/maybe/skip decisions are recorded and tune later suggestions (`ave learn taste report`).

## 10. Publishing (always yours)

```bash
ave package queue ep-001                # queues approved items as DRAFTS; nothing is sent
ave publish list ep-001
ave publish approve ep-001 <id>         # YOU run this; approval is bound to the exact file bytes
ave publish run ep-001 <id>             # a dry run; add --yes to really send
```

**HUMAN CHECKPOINT: every publish.** The agent cannot approve; it will ask you to run the approve command. Platform credentials are in
[publishing-setup.md](publishing-setup.md). Live posting has only been tested against mocks, so try a private or unlisted post first.

## If something goes wrong

- `ave resume ep-001` and `PROGRESS.md` say what finished and what is blocked. Re-running any stage is safe.
- `ave doctor` after any environment change.
- Agents record failures with `ave learn`; real-footage findings are in [field-notes](field-notes/README.md).
- [README troubleshooting](../README.md#faq-and-troubleshooting) covers the common installation problems.
