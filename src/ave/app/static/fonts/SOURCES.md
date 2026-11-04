# Interface fonts: sources and checks

Figtree (body) and Poppins (titles) for the Director's Room app, both under the SIL Open Font License 1.1 (full text next to the
fonts). The OFL allows bundling and redistribution with the license text; the fonts may not be sold on their own.

Fetched 2026-11-02 from Google Fonts. The WOFF2 files are the ones Google Fonts serves for the CSS API request
`https://fonts.googleapis.com/css2?family=Figtree:wght@300..900&family=Poppins:wght@500;600` (latin and latin-ext subsets only),
renamed, bytes unchanged. The license texts come from the official Google Fonts repository, pinned:
`https://github.com/google/fonts` commit `2fe1e8c6b137fb13041a7cfca6fdcd8a910ea4dc`.

| Local file | Source URL | sha256 |
|---|---|---|
| `figtree-latin.woff2` (variable, weight 300 to 900) | https://fonts.gstatic.com/s/figtree/v9/_Xms-HUzqDCFdgfMm4S9DaRvzig.woff2 | `8330490a01c60c196eae00b823de8102275aaa5862e7b76a7af21b8745338928` |
| `figtree-latin-ext.woff2` (variable, weight 300 to 900) | https://fonts.gstatic.com/s/figtree/v9/_Xms-HUzqDCFdgfMm4q9DaRvziissg.woff2 | `f153aa07c1b16fbb12391c2512860c97819a0a9fd014f338b2b3f12496479d13` |
| `poppins-500-latin.woff2` | https://fonts.gstatic.com/s/poppins/v24/pxiByp8kv8JHgFVrLGT9Z1xlFd2JQEk.woff2 | `289e0afc8be731a86822349e54557296f145926496bd2138db1bac0db77f77a2` |
| `poppins-500-latin-ext.woff2` | https://fonts.gstatic.com/s/poppins/v24/pxiByp8kv8JHgFVrLGT9Z1JlFd2JQEl8qw.woff2 | `f8f0c28661c88edbcee022a4a80657619e8931af8c2a47c28e230307e2af30e5` |
| `poppins-600-latin.woff2` | https://fonts.gstatic.com/s/poppins/v24/pxiByp8kv8JHgFVrLEj6Z1xlFd2JQEk.woff2 | `872e862918591a9e824dc03ed92f05729435ffbb8ebbb10eff7eda26592b1798` |
| `poppins-600-latin-ext.woff2` | https://fonts.gstatic.com/s/poppins/v24/pxiByp8kv8JHgFVrLEj6Z1JlFd2JQEl8qw.woff2 | `72efc6a32cfd37c557108628cffa01153c480c3d4cde556744406e50b9d8e9f7` |
| `OFL-Figtree.txt` | https://raw.githubusercontent.com/google/fonts/2fe1e8c6b137fb13041a7cfca6fdcd8a910ea4dc/ofl/figtree/OFL.txt | `140d37233e7f3ce7313798befa9600893bcceaf41a55fa0fa5ad52f7f657a268` |
| `OFL-Poppins.txt` | https://raw.githubusercontent.com/google/fonts/2fe1e8c6b137fb13041a7cfca6fdcd8a910ea4dc/ofl/poppins/OFL.txt | `6be04893d770899a015649c7aa3b582f871b272f8747a92b78b17c3e5c8b2573` |

Checks done: both license files say "SIL OPEN FONT LICENSE Version 1.1" (Figtree: Copyright 2022 The Figtree Project Authors;
Poppins: Copyright 2020 The Poppins Project Authors); the repository's METADATA.pb for Poppins says `license: "OFL"`. No Reserved Font
Name is declared. Italics and other weights are not bundled (nothing uses them).
To verify: `shasum -a 256 *.woff2 *.txt` against the table.
