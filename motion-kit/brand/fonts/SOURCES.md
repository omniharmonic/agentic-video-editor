# Bundled brand fonts: sources and checks

Both families are licensed under the SIL Open Font License 1.1 (full text next to each font; the OFL allows bundling, embedding in
renders, and redistribution with the license text; the fonts may not be sold on their own).

Fetched 2026-10-14 from the official Google Fonts repository, pinned to one commit so the bytes are reproducible:
`https://github.com/google/fonts` commit `67bc647ce1910e92f461bcc82aae30f21d21a9cc` (main at fetch time).

| Local file | Source URL | sha256 | git blob sha (matches the GitHub contents API) |
|---|---|---|---|
| `Newsreader-Variable.ttf` | https://raw.githubusercontent.com/google/fonts/67bc647ce1910e92f461bcc82aae30f21d21a9cc/ofl/newsreader/Newsreader%5Bopsz,wght%5D.ttf | `8a08d13f8a6c0d51be379a60af84f945f65369a67e509ee3c3bdcc421254d7c1` | `ad4a9a8c44ec328c80b61c773f1ba02f6b5ed292` |
| `OFL-Newsreader.txt` | https://raw.githubusercontent.com/google/fonts/67bc647ce1910e92f461bcc82aae30f21d21a9cc/ofl/newsreader/OFL.txt | `fdfad38143ec470553cae82a1e45320bdd1b9ec70415d37bd0171051d8a4ded8` | `d2d4f407109e9c74b8ab502fc79b912408ab8e41` |
| `Inter-Variable.ttf` | https://raw.githubusercontent.com/google/fonts/67bc647ce1910e92f461bcc82aae30f21d21a9cc/ofl/inter/Inter%5Bopsz,wght%5D.ttf | `29160a80ff49ddcab2c97711247e08b1fab27a484a329ce8b813d820dc559031` | `047c92f6e2212473dc436020afed689527076d44` |
| `OFL-Inter.txt` | https://raw.githubusercontent.com/google/fonts/67bc647ce1910e92f461bcc82aae30f21d21a9cc/ofl/inter/OFL.txt | `5b9321a4298cfeb6b34354164a1c3afc3db114569984c502b9b35d988fd58c57` | `21f6aff961064c2e429f570995e446bcdd555422` |

Checks done: both license files say "SIL OPEN FONT LICENSE Version 1.1" (Newsreader: Copyright 2020 The Newsreader Project Authors,
designer Production Type; Inter: Copyright 2020 The Inter Project Authors, designer Rasmus Andersson; the repository's METADATA.pb for
each says `license: "OFL"`). The files were renamed from `Newsreader[opsz,wght].ttf` and `Inter[opsz,wght].ttf` (brackets are awkward in
URLs and shells); the bytes are unchanged. Both are VARIABLE fonts (weight and optical size axes), so one file covers every weight;
`tokens.json` lists them with a weight range. The italic variants are not bundled (nothing uses italics).
To verify: `shasum -a 256 *.ttf *.txt` against the table, or `git hash-object` against the blob sha.
