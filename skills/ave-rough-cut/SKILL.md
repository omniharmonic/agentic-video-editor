---
name: ave-rough-cut
description: Plan a shot/reverse-shot rough cut from the transcript and build it as an editable timeline in DaVinci Resolve through the MCP. Use after cameras are synced and mapped, when the user asks for a rough cut, an assembly, or to switch between cameras by who is speaking.
---

# Rough cut: plan, then build

## 1. Plan (no Resolve needed)

```bash
ave plan-cuts <episode>        # -> work/cutlist.json
```

Requires `transcript.json`, `media.json`, `episode.json` (`cameras`, `speakers`), and `sync.json` if more than one file.
Read the summary: shot count, average shot length, `reason` values. Fallback reasons mean a camera was not recording
at that moment. The numbers behind the feel are in **`config/cutplan.default.yaml`** (override per project in `<ep>/policy/cutplan.yaml`, or `ave plan-cuts --policy PATH`;
the resolved numbers and their source are copied into the cut list as `policy` / `policy_source`). Tune them with the human; do not hack the output file.

**Interjections** (something the off-camera person says while the speaker talks): the planner cuts to the listener only when ALL hold: it lasts at least
`interjection.reaction_min_s` (0.8 s), it is clearly on the listener's own mic (`speaker_margin_db` >= `min_margin_db`, 6 dB; transcripts without channel data are accepted unless
`require_margin`), and the current shot has been up at least `min_hold_before_s` (6 s). Backchannel words (`ignore_words`: mm-hm, yeah, right, okay, uh-huh...) stay on the speaker
unless they are laughter-length (`laugh_min_s`, 1.2 s). `legacy_never_cut_to_listener: true` restores "never". Check the `reaction` shots in the cut list (`stats.reactions`): are they
laughs and real reactions, or noise? Raise `reaction_min_s` / `min_hold_before_s` if it ping-pongs; lower them if a laugh was missed.

**A wide camera:** give a third camera `role: wide` in `episode.json` (and map no speaker to it). The planner then opens each region with a short establishing wide (`wide.opening_s`),
shows the wide while two people talk over each other (`wide.crosstalk_min_s`), and uses it as a last resort when no speaker camera is recording. Reaction shots inside long single-speaker
holds are chosen later, by the cleanup pacing pass (`ave-cleanup`), which also prefers the wide.

Each shot already carries Resolve-ready numbers: `file`, `record_frame`, `source_start_frame`, `source_end_frame` (inclusive).

## 2. Build in Resolve (needs the bridge: see ave-setup-check)

Open the target project in Resolve first (a fresh one is best: the frame rate can only be set before the first timeline exists).

```bash
ave handoff <episode> --stage assembly --dry-run     # validates and prints the plan; changes nothing
ave handoff <episode> --stage assembly [--label "Ep 12 rough cut"]
```

`ave build-timeline` is a DEPRECATED alias of exactly this (it prints a notice, makes the assembly edit from `cutlist.json` if there is
none, then runs `ave handoff --stage assembly`; `--name` is the label). There is one Resolve builder: `scripts/resolve_handoff.py`, through
the same MCP tool functions you would call by hand, and it
**reads everything back from Resolve** (count, start, length and camera of every shot, audio length, gaps/overlaps).
Exit code 0 means verified; non-zero prints `problems`. Prefer it over hand-building: it encodes the traps below.

Do **not** use native multicam: Resolve's scripting API cannot switch angles. A plain timeline is fully editable and
is exactly what the cut list describes.

### Traps (all measured on Resolve 21.0.4 free; why the script exists)

- **New projects default to 24 fps.** The script sets `timelineFrameRate` to the cut list's fps *before* creating the
  timeline and refuses to continue if it cannot. A wrong fps silently shifts every cut.
- **`AppendToTimeline` `endFrame` is exclusive** here. Passing the inclusive last frame leaves every clip one frame short
  and a 1-frame gap between all shots. The cut list stores inclusive `source_end_frame`; the script sends `+1`.
- **Timelines start at 01:00:00:00** (frame 108000 at 30 fps), so read-back positions are `108000 + record_frame`.
- **Imports land in the current bin**, and clip ids are UUIDs found with `probe_media_pool`; the script maps by real file path.
- **Batches are all-or-nothing.** A bad id fails the whole call with nothing appended; fix and retry, never append twice.

### By hand (only if the script cannot be used)

```
project_settings(action="set_setting", params={"name": "timelineFrameRate", "value": "30.0"})   # then read it back
media_storage(action="import_to_pool", params={"items": ["<absolute path>", ...]})
media_pool(action="probe_media_pool")                       # clips[].id and clips[].file_path
media_pool(action="create_timeline", params={"name": "<episode> rough cut"})
media_pool(action="append_to_timeline", params={"clip_infos": [
  {"clip_id": "<id>", "start_frame": <source_start_frame>, "end_frame": <source_end_frame + 1>,
   "record_frame": <record_frame>, "track_index": 1, "media_type": 1}, ...]})       # media_type 1 = video only
media_pool(action="append_to_timeline", params={"clip_infos": [
  {"clip_id": "<audio file id>", "start_frame": <max(0, -offset*fps)>, "end_frame": <start + frames>,
   "record_frame": <max(0, offset*fps)>, "track_index": 1, "media_type": 2}]})      # 2 = audio only
```

## 3. Verify (never skip) then report

`ave handoff` already compared the timeline with the cut list and ran `timeline(action="detect_gaps_overlaps")`.
For the human, also spot-check two or three shots *visually*: move the playhead and export a still from Resolve:

```
# timeline.SetCurrentTimecode via the bridge, then:
project_settings(action="export_frame_as_still", params={"path": "<episode>/work/stills/check_01.png"})
```

Look at the image: is it the right camera, and is the person who is speaking on screen? Report: shots, average shot length,
fallbacks, warnings. **Stop for human review.** Do not polish, export, or cut clips until they approve.

## Rough-cut taste notes (editor's defaults)

- Cut to the new speaker slightly *before* they speak (default 0.25 s): it reads as natural; cutting on the first word feels late.
- Never leave a shot sooner than the minimum, even if the other person speaks: delay the cut instead.
- Short interjections ("yeah", "mm-hm") stay on the speaker; a reaction cut earns its place only when the listener's face carries meaning (laughter, surprise): that is what the `interjection:` policy encodes.
- Long single-speaker takes with no change for 14+ seconds get a reaction shot from the cleanup pacing pass (policy `pacing:`); it is logged per insertion, so propose changes to its numbers rather than editing the cut.
