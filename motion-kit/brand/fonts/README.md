# Brand fonts

Bundled: **Newsreader** (serif) and **Inter** (sans), both SIL OFL 1.1, as variable fonts. Licenses are next to the files
(`OFL-Newsreader.txt`, `OFL-Inter.txt`); where they came from and their hashes are in [SOURCES.md](SOURCES.md).

The renderer injects an `@font-face` for every file listed in `../tokens.json` (`fonts.<role>.files[]`) that exists in this folder; the
`fallback` stacks (Georgia, Helvetica Neue ...) are used only when a file is missing. The captions renderer prefers this folder too.
Renders therefore look the same on any machine. `ave motion tokens` lists `missing_brand_fonts`; it should be empty.

To change a font: use another SIL OFL family, copy the font file and its license text here, update `fonts.<role>` in `../tokens.json`
(`weight` may be a range such as "200 800" for a variable font), record the source in SOURCES.md, and re-render everything
(`ave content render --force`), because every metric shifts.
