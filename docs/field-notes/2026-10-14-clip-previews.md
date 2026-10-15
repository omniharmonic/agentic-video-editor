# Clip previews, 2026-10-14 (workstream G2)

> **Working note from the first real episode.** This is internal history kept as evidence: what we assumed, what a real recording showed, and what changed. It describes one project (a two-host, two-camera podcast), uses its real numbers, and was written while the tools were still changing, so commands and defaults may have moved on. For the current behaviour read the skill or doc a note points to. See [docs/README.md](../README.md).

`ave clips previews <ep> --name s2 --top 3` on the real project (`<episode-root>`, USB ExFAT, 3840x2160 HEVC 30 fps cameras,
rough.s2.v3 = 1122 s, 1080p canvas, no review proxies made yet). Output went to `work/clips/previews/` only; footage was read-only.

## What ran

| clip | preview length | render time (3 in parallel) | size | loudness |
|---|---|---|---|---|
| s2-c03 | 46.2 s | 25.5 s | 1.8 MB | -16.4 LUFS, -2.9 dBTP |
| s2-c02 | 40.0 s | 26.1 s | 1.6 MB | -16.0 LUFS, -3.2 dBTP |
| s2-c01 | 67.9 s | 30.6 s | 3.7 MB | -16.6 LUFS, -2.7 dBTP |

- Wall time for the three: 30.8 s (user CPU 87 s) for 154 s of preview: **about 10 s of wall time per clip, 0.2 s wall per preview second, 0.57 CPU-s per
  preview second.** A 100-clip run of 50 s clips plus 16 s of pad (about 6600 preview seconds) projects to roughly 22 minutes of wall time with 3 workers.
- Output size is about 0.04 MB per preview second (about 0.3 Mbps), well under the 0.11 MB/s the estimate assumes; 100 clips fit in about 300 MB.
- Every file was checked by the compiler (frames, duration within one frame, audio present, true peak under -1 dBTP) and by `previews._verify`
  (duration = pad + clip + pad within one frame at 30 fps). The 540p picture of `s2-c02` at 12 s was extracted (`ave frame`) and shows the right camera, in
  focus, correct colours and aspect.
- Loudness is measured on the encoded file (ebur128): all three within 0.6 LU of the -16 LUFS target.

## Decisions and what real footage showed

- **Decode only the window, VideoToolbox for big pictures.** The compiler seeks on the input (`-ss`) and scales right after decode. For a 40 s window of
  the 4K HEVC file, one ffmpeg took 8.0 s wall but 47 CPU-s in software and 11.7 s wall but 15.6 CPU-s with `-hwaccel videotoolbox`. The preset
  therefore asks for hardware decode (only for HEVC/H.264 of 1440 lines or more; a failure retries in software): it is slower in isolation but leaves the machine
  usable, and three parallel renders do not fight over cores.
- **The machine was not idle.** After the first run, other jobs (a `final` render of this same session, a shorts export, other agents) pushed the load
  average above 300 on 12 cores, so later benchmark runs (software vs hardware decode, 1 vs 3 workers) were useless: the same three clips took 80-190 s.
  The numbers above are from the first run only. Re-measure on a quiet machine before trusting the 22-minute projection.
- **Proxies.** If every picture source of the edit has a verified `ave proxies` file, the picture is decoded from it instead (crops rescaled to the proxy's
  pixels, audio still from the real dialogue sources). None exist for this project, so that path is covered by unit logic only, not by a real run.
- **Rendering reuses `ave.compile.render`** (one code path for pixels and loudness), so the cleanup, crossfades, room tone fills and the limiter behave exactly as
  they do for a select. It was extended with a `preview` preset, optional hardware decode, and dict presets (the height is a parameter).
- **macOS writes `._*` AppleDouble files next to every file on ExFAT.** They appear beside the previews and `index.json`. Readers must ignore them (the index
  is the source of truth, never a directory listing).
- `ave frame` writes its still into the project's `work/stills/`. For this read-mostly project the still was moved out and the folder removed; use
  `ffmpeg -ss T -i preview.mp4 -frames:v 1` into a scratch folder for a check on a project you must not touch.

## Not covered by the real run

- No clip of this project has `tighten` yet (workstream K), so the `.tight.mp4` path ran only on the synthetic episode (duration and joins verified there).
- The estimate shown before a run (`EST_RENDER_PER_MEDIA_S`, `EST_BYTES_PER_S` in `src/ave/previews.py`) is a planning number from this one run.
