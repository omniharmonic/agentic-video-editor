# Contributing

Thanks for looking. This project is a toolkit (the `ave` CLI plus agent skills) that lets an AI agent edit a podcast with FFmpeg and
Python while a human reviews and polishes in DaVinci Resolve. Contributions of code, skills, policy tuning, footage-compatibility
reports and documentation are all welcome.

By taking part you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md). Security problems go through
[SECURITY.md](SECURITY.md), not public issues. The project is MIT licensed ([LICENSE](LICENSE)); by contributing you agree that your
contribution is released under the same license. Bundled and downloaded third-party material is listed in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Read [AGENTS.md](AGENTS.md) before you change behaviour. It states the rules the tool is built around (raw footage is read-only,
disk is the memory, stop at human checkpoints, publishing approval is a human act). A change that weakens one of those rules needs a
discussion first.

## Development setup

Requirements: macOS on Apple Silicon is the supported platform today (Apple Vision, FluidAudio and Metal Whisper are Apple-only);
Python 3.11+, [uv](https://docs.astral.sh/uv/), FFmpeg and ffprobe on `PATH`. Most of the test suite runs without Resolve, a GPU, or
any real footage.

```
uv sync --all-extras                     # creates .venv
.venv/bin/python -m playwright install chromium     # only needed for the motion/content renderers
.venv/bin/ave doctor                     # shows what this machine can and cannot do
```

`scripts/setup.sh` does the whole machine setup (pinned Resolve MCP server, FluidAudio build, Whisper model); you do not need it to
work on most of the code.

## Running the tests

```
PYTHONPATH=src .venv/bin/pytest -q                 # the full suite
PYTHONPATH=src .venv/bin/pytest -q tests/test_clips.py -k completeness
PYTHONPATH=src .venv/bin/pytest -q -m real_motion  # also render with real Chromium (slow)
```

Rules for tests:

- **Real measurements beat mocks.** The engine tests render media with FFmpeg and measure it (ffprobe, pixels, loudness). Prefer an
  end-to-end check over a unit test that passes while the product is wrong.
- **Fixtures are synthetic.** `tests/fixtures.py` and `tests/*_synth.py` build episodes from generated tone, noise and solid-colour
  video. Do not commit real footage, real transcripts, real names, screenshots of real people, or real credentials. Write
  made-up dialogue in the shape you need.
- **Maintainer-only real-footage checks skip cleanly.** A few tests can run against a real episode when you point an environment
  variable at one (`AVE_TEST_REAL_MASTER`, `AVE_TEST_REAL_DIARIZATION`, `AVE_TEST_REAL_ANALYSIS_DIR`, `AVE_TEST_REAL_WORK_DIR`,
  `AVE_TEST_REAL_CAMERA`). Unset, they are skipped. Never hard-code a path to your own machine.
- **Network is mocked.** Publishing and stock-media tests use fake HTTP; nothing in the suite may post, upload, or spend money.
- A bug fix comes with a test that fails without the fix. Policy numbers (thresholds, targets) live in YAML under `config/`, not in
  code, and the test should read them from there.

## How to add a command

Commands are plugins: add one file in `src/ave/commands/` and never edit `src/ave/cli.py`.

```python
# src/ave/commands/hello.py
from __future__ import annotations
import json
from ..episode import Episode

def cmd_hello(a) -> int:
    ep = Episode(a.episode)
    print(json.dumps({"episode": str(ep.root)}, indent=2))   # commands print JSON a human or an agent can read
    return 0

def register(sub) -> None:
    s = sub.add_parser("hello", help="one line that says what it does")
    s.add_argument("episode")
    s.set_defaults(fn=cmd_hello)
```

Conventions: resolve all paths through `Episode` (`ep.work`, `ep.edits`, `ep.exports`, ...) and store paths in artifacts relative to the
episode root; never write into the raw footage folders; write new versions of edits (`edits/<stage>.vN.json`) instead of overwriting;
make re-running a stage safe; anything that publishes or spends money must go through the approval gates described in AGENTS.md.
Add the command to the stage table in `AGENTS.md` and a test in `tests/`.

## How to add or change a skill

A skill is a Markdown file `skills/<name>/SKILL.md` with YAML frontmatter, read by whichever agent harness the user runs:

```
---
name: ave-<name>
description: One or two sentences: what it does and WHEN an agent should reach for it (this is what triggers it).
---
# Title
## What you do       (the steps, with the exact commands)
## How to judge      (the critic checklist: what good looks like, what to look at before acting)
## Stop and ask      (the checkpoints that need the human)
```

`.claude/skills` and `.agents/skills` are symlinks to `skills/`; do not copy skills anywhere else. Skills say *when* and *how to
judge*; deterministic work belongs in the CLI. Keep claims honest: if you did not run it on real footage, say it is unverified.

## Taste and policy contributions

Defaults that decide whether an edit sounds natural (pause lengths, filler thresholds, hold limits, clip scoring, caption styles,
brand voice for post copy) are data, in `config/*.yaml`, with the reason next to each number. If you have tuned them on real
material, a pull request that changes a default should include: what you listened to or measured, on what kind of material
(solo, two-host, panel; speech rate; room), the before/after numbers, and the reason. Defaults are opinionated on purpose; a show that
wants something else overrides them per episode or in `~/.config/ave/`, so prefer adding an option over changing a default when
it is a matter of taste.

## Field notes

When a stage meets real footage and something surprising happens (a wrong assumption, a failure, a number that was not what the
docs said), write it down: [docs/field-notes/README.md](docs/field-notes/README.md) has the loop, the template
(`Assumed / Found / Change`), and the promotion checklist (fix the skill or doc, add a regression test that fails without the fix,
mark the note applied with the commit). Field notes describe a real project, so **anonymise them before you submit**: no private
paths, no names of people who did not agree to appear, no verbatim private speech beyond what the lesson needs.

## Footage-compatibility reports

Different cameras, recorders, codecs and drives break different assumptions. If a stage fails or warns on your footage, open a
"Footage compatibility" issue using the template: it asks for the `ave doctor` output and the `ffprobe` summary of the files, not the
files themselves.

## Pull requests

- Keep a change focused; one concern per pull request.
- Run the full suite before you push, and say in the description what you ran and what you could **not** verify (for example "not
  tried in live Resolve").
- Update the docs and skills a change touches, and add a line to [CHANGELOG.md](CHANGELOG.md) under "Unreleased".
- Do not add a dependency that is not permissively licensed, or a model or asset whose terms restrict redistribution or commercial
  use, without raising it first; record anything bundled or downloaded in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
- Commit messages: a short imperative summary line, then why.
