# Sequence templates

Templates for the opening and closing sequences (`ave sequence`, see `docs/sequences.md`). Same contract as `../templates/` (pure
`window.__render(t)`, tokens only, no third-party animation code) but kept in their own folder so the production template list that
`ave motion list` / `ave motion place` offers stays exactly as it was. They are rendered by `ave.sequence` through the same
Chromium + ProRes 4444 alpha path as `ave.motion`, over a solid plate (the brand ink color), so they only draw type.

| id | props | used for |
|---|---|---|
| `episode-title` | `show` (60), `number` (6), `title` (80), `tagline` (80), `logo` (file URI) | the title card: animated show mark + episode number and title |
| `host-card` | `heading` (30), `people` [{name (40), title (50)}] up to 4 | host introductions with titles (for example Co-host) |
| `thanks-card` | `heading` (60), `body` (200), `lines` [{label (24), value (60)}] up to 4 | thanks / calls to action |
| `credits-roll` | `heading` (60), `sections` [{heading (40), lines [string (70)] up to 12}] | the full-height credits column; `ave.sequence` scrolls it |
| `end-card` | `heading` (60), `body` (140), `lines` [{label, value}] up to 3, `layout` end-screen/centered | the held end card (YouTube end-screen placeholder) |
