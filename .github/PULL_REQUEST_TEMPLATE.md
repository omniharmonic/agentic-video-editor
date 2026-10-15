## What and why

<!-- One concern per pull request. What changes, and what problem does it solve? -->

## How I checked it

- [ ] `PYTHONPATH=src .venv/bin/pytest -q` passes (paste the summary line)
- [ ] I added or updated a test that fails without this change
- [ ] I tried it on real footage / in live Resolve / on real platforms (say which), or I list below what I could **not** verify

**Not verified:**

## Checklist

- [ ] The rules in AGENTS.md still hold (raw footage read-only, versioned edits, human checkpoints, no unapproved publishing or spending)
- [ ] Taste/threshold numbers are in `config/*.yaml`, with the reason, not in code
- [ ] Skills and docs that this touches are updated; `CHANGELOG.md` has a line under "Unreleased"
- [ ] No real footage, transcripts, names, screenshots of people, credentials, or machine-specific paths are included
- [ ] New or changed dependencies, models, fonts or assets are permissively licensed and listed in `THIRD_PARTY_NOTICES.md`
