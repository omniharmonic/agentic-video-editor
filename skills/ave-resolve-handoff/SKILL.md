---
name: ave-resolve-handoff
description: Put the latest edit (assembly, rough or final) into DaVinci Resolve as a new timeline so the human can review and tweak it. Use whenever a cut stage is ready for human review, or when the user asks to see or open the latest cut in Resolve.
---

# Hand off to DaVinci Resolve

Resolve is where the human reviews and polishes. The agent edits with FFmpeg; the handoff **compiles the latest EditDoc into a NEW
timeline** and never touches existing ones, so the human's tweaks are never overwritten.

## Preconditions (ask the human if not met)

1. Resolve 21.0.x free is open with a project, and `Workspace > Scripts > resolve_bridge` is running (`ave doctor` shows the bridge listening).
2. The project is **fresh or already at the episode's frame rate**. Resolve only lets the frame rate change before the first timeline exists; the script refuses a mismatch instead of shifting cuts. A project per episode is the clean pattern.

## Run

```bash
ave handoff <episode> --dry-run          # plan only: counts, warnings, nothing changes in Resolve
ave handoff <episode>                    # latest of final > rough > assembly; or --stage rough
ave handoff <episode> --label "ep12 for client"
ave handoff <episode> --stage rough --name s1   # a session's named edit (multi-session episodes have no unnamed edits)
```

What lands in Resolve: V1 = the video cuts (hard cuts, camera per segment); A1 = the **compiled dialogue master** (crossfades and gain baked in, so
Resolve free's missing fade/transition API does not matter); A2 = music; V2+ = motion/b-roll overlays; the SRT is imported for captions. Free Resolve
may refuse to place the SRT on the timeline; the script says so and leaves it in the Media Pool to drag.

## Layered handoff (`--layers`): the work-in-progress timeline

The consolidated timeline has the chosen angle only and ONE baked audio clip. An editor who needs to grab a clip from the other angle or
adjust a cut needs the layers. `--layers` builds a SECOND timeline, `<title> <stage> <name> vN layers`, next to the consolidated one (it never
replaces it; run both when the editor wants both):

```bash
ave handoff <episode> --stage rough --name s2 --layers --dry-run   # plan only: items per track, warnings; renders and writes nothing
ave handoff <episode> --stage rough --name s2 --layers             # builds the sum WAV(s) + baked master once, then the timeline
```

| Track | What it is | State |
|---|---|---|
| V1 "Edit" | the chosen shots, exactly as in the consolidated handoff | enabled |
| V2, V3 "<role> (alt)" | one track per camera: the SAME source moments, in sync (drift-corrected), as clips following the edit's kept ranges (adjacent shots contiguous in episode time are one clip, so only removed sections split it) | **disabled** |
| A1 "Dialogue (editable clips)" | one clip per dialogue segment, cut from a dual-mono sum WAV of the recorder (`work/render/<key>.<stem>.sum.wav`, 24-bit, L==R, built once and reused) so nothing plays in one ear; every clip has source handles | enabled |
| A2 "Baked master (muted)" | the consolidated dialogue master, for A/B or switching back | **disabled** |

Music and overlays (if any) sit above these tracks. How an editor uses it: playback shows the edit (alternates are disabled); to grab another
angle, enable V2/V3 or copy that clip onto V1; to adjust a cut, trim the A1 clips (handles are there) and the matching V1 clip; to compare with
the polished sound, enable A2 and mute A1.

Limits to state in the report: audio-only appends are **frame-granular** (cut points on A1 are snapped to whole timeline frames, 1/fps s) and
have **no crossfades**, so expect small clicks at joins until the editor adds fades; the baked master on A2 keeps the sample-accurate crossfades.
A camera that was not recording during a kept range gets no clip there (a warning names it); a camera that started or ended inside a range is
trimmed to what it recorded (also a warning). Free Resolve cannot fade or retime via the API, so nothing is faded here.

Same safety as the consolidated handoff: chunked, count-verified, resumable appends. Re-running after a failure verifies V1 and then every
extra track is a prefix of its own plan and resumes after it; anything else is refused. The read-back checks, per track, the item count, every
start/length (and camera on video), total frames, gaps/overlaps (V1 and A1 must have 0 of both), and that V2/V3/A2 are really disabled.
`"ok": true` is the only success; report the per-track counts from `tracks`, the warnings and the frame-snapping note.

Optional live check on a scratch project (never an episode's): `.venv/bin/python scripts/resolve_layers_smoke.py --yes-touch-resolve`.

## Review proxies (`ave proxies` + `--use-proxies`): smooth playback, renders from originals

Playing 4K HEVC from an ExFAT USB drive in Resolve garbled the audio under load (the audio file was clean). Fix playback, not the files:

```bash
ave proxies <episode> --dry-run                  # estimate (GB), free space, what would be made; writes nothing
ave proxies <episode> [--name s1] [--codec prores-proxy|dnxhr-lb] [--height 1080]
ave handoff <episode> --use-proxies              # links the proxies; the timeline still names the ORIGINAL files
```

- Proxies go in a `proxies/` folder NEXT TO the footage (`02_FOOTAGE/proxies/`, `raw/proxies/`), same name, `.mov`. Originals are only read, never changed. Ingest ignores `proxies/`.
- Only the footage the chosen edit uses is proxied. Intraframe (ProRes Proxy or DNxHR LB), never upscaled, VideoToolbox decode where available (`--no-hw` to turn off), progress in the terminal and `PROGRESS.md`.
- It prints a size estimate first and **refuses if free space < 1.5x the estimate**. It is resumable: a proxy that verifies (right codec, duration within one frame) is kept; a bad or cut-off one is rebuilt (`.partial` files are never left looking finished).
- `--use-proxies` calls the MCP `link_proxy_checked` once per imported original (after import, before the timeline is built) and reports `proxies: {requested, linked, readback_verified, failed}`. A failed link is reported, never fatal; those clips just play from the original.
- Resolve must also be switched to proxy playback (Playback > Proxy Mode), and **off again before Deliver**, so renders come from originals.

**UNVERIFIED in real Resolve** (the code paths are tested against an in-memory fake bridge only). Manual check, 2 minutes, on a scratch project: run `ave proxies` on a short clip, `ave handoff --use-proxies`, then in the Media Pool select the clip and confirm Clip Attributes / the Proxy column shows the proxy file; switch Proxy Mode on and play. Report what you saw and update ROADMAP row W10. Do not claim "linked" in your report unless `readback_verified` equals `requested`; say "linked per the bridge, unverified in Resolve" otherwise.

## Subtitles on the timeline (W7, UNVERIFIED)

Routes considered (MCP docs and `api-limitations.md`; there is no dedicated subtitle-import API method):

| Route | Status |
|---|---|
| `ImportMedia(srt)` then `AppendToTimeline(clip_ids)` | Reported working on Studio 21.0.4.5 (MCP issue #169); not measured on free 21.0.4. **This is the `--try-subtitles` route.** |
| UI: File > Import > Subtitle..., or drag the SRT from the Media Pool onto the timeline | The fallback; printed by the handoff as `subtitle_instructions`. |
| `Timeline.ImportIntoTimeline` | AAF/EDL/XML only; not a subtitle route. |
| `CreateSubtitlesFromAudio` | Never: modal on free Resolve, and it ignores our transcript. |
| `.drt` authoring with `spec.subtitlesSrt`, or `project_db write_captions` (Resolve fully quit) | Exist in the MCP; invasive, not implemented. |

`ave handoff` always copies the SRT to `captions/<timeline name>.srt`. Without a flag it does not touch Resolve for subtitles and prints the steps. With `--try-subtitles` it counts the video/audio/subtitle tracks before and after, appends, and reports success ONLY if a subtitle track exists with as many items as the SRT has cues; a video/audio track appearing, no track, or a wrong count all fall back to the steps. A "success" still says UNVERIFIED because timing and text must be eyeballed.

Manual check (2 minutes, scratch project): `ave handoff <ep> --try-subtitles`. Read `subtitles` in the report. Open the timeline: is there a subtitle track, do the first and last cue match the SRT, is the timing right? If it failed, try File > Import > Subtitle... on the copy in `captions/` and note which worked. Edit ROADMAP row W7 with the result. Styled captions stay burned in by FFmpeg either way.

## Verify and report

The script reads the timeline back and exits non-zero on any mismatch (item count, every start/length/camera, audio items, gaps, overlaps). Never say
"it's in Resolve" until it printed `"ok": true`. Report the timeline name, counts, any `warnings`, and the subtitle status. Then **stop and wait for the human's notes**:
the pending review is recorded in `episode.json` so the next session knows what you are waiting on (`ave resume <episode>`).

## Never

- Never call `Timeline.CreateSubtitlesFromAudio` or `MediaPoolItem.TranscribeAudio` on free Resolve: they pop a modal that blocks every later API call until a human clicks it.
- Never delete or modify the human's timelines or project settings. Never run this against a project they did not open for it.
