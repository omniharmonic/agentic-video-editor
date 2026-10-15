# Documentation index

Who each document is for: **user** (you run `ave` on your own episodes), **contributor** (you change the code, skills or policy),
**history** (internal working notes kept as evidence, not instructions).

Start with the project [README](../README.md). The first-run walkthrough, `docs/getting-started.md`, is maintained alongside it.
For anyone, human or agent, [AGENTS.md](../AGENTS.md) is the canonical statement of the rules and the pipeline, and
[skills/](../skills/) holds the playbooks an agent reads for each stage.

## Reference for users

| Document | What it is for | Audience |
|---|---|---|
| [ROADMAP.md](ROADMAP.md) | What is built, what is verified on real footage, what is not, known limits, and what is next | user, contributor |
| [cleanup-policy.md](cleanup-policy.md) | How the rough-cut cleanup decides (pauses, fillers, masks) and which YAML numbers to tune | user |
| [motion.md](motion.md) | The motion-graphics kit: templates, tokens, brand fonts, alpha renders | user, contributor |
| [sequences.md](sequences.md) | Opening and closing sequences (cold open, title, host card, credits, end card) and their approval | user |
| [content.md](content.md) | The opt-in content kit: quote posts, thumbnails, carousels, reel cards, the Canva renderer | user |
| [ui.md](ui.md) | The local review UI (`ave ui`): pages, shortcuts, and its safety model | user |
| [publishing-setup.md](publishing-setup.md) | One-time credential setup per platform and the publishing safety model | user |
| [asset-licensing.md](asset-licensing.md) | The license log for music and B-roll, provider terms, and the warnings that go with them (mostly unverified) | user |

Screenshots used by these pages live in `images/` (UI) and `sequence-stills/` (title and credit cards). Both are generated from a synthetic demo episode with a fictional show ("The Long Table"); `scripts/shoot_ui_screenshots.py` rebuilds them.

## For contributors

| Document | What it is for |
|---|---|
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | Dev setup, running tests, adding a command or a skill, taste and policy contributions |
| [../AGENTS.md](../AGENTS.md) | The rules and the pipeline table; update it when you add a command |
| [field-notes/README.md](field-notes/README.md) | The loop for turning a surprise on real footage into a fix, and the note template |
| [../SECURITY.md](../SECURITY.md), [../CODE_OF_CONDUCT.md](../CODE_OF_CONDUCT.md), [../THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md), [../CHANGELOG.md](../CHANGELOG.md) | Project policy files |

## Internal history (kept as evidence)

These are working documents from building the first version. They name the first project's real numbers, and some details (commands, defaults)
have since changed. They are accurate about what was found at the time, not about what the tools do today.

| Document | What it is |
|---|---|
| [field-notes/](field-notes/) | Dated notes from running each stage on the first real episode: assumption, finding, change. The evidence behind many rules in AGENTS.md and the skills |
| [superpowers/specs/](superpowers/specs/) | Design documents written before building (pipeline design, recall-first clips and the local UI) |
| [superpowers/plans/](superpowers/plans/) | The implementation plan that the building agents worked from |
| [proposals/agentic-content-pipeline.md](proposals/agentic-content-pipeline.md) | Research and decision record for the content kit and the optional Canva renderer |
