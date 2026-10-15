---
name: ave-map-cameras
description: Decide which camera shows which speaker and record it in episode.json so cuts can follow the conversation. Use after transcription and sync, before ave plan-cuts, or when a cut shows the wrong person. Uses stills extracted at moments when each speaker is talking as visual evidence.
---

# Map cameras to speakers

Shot/reverse-shot needs one fact per speaker: **which camera frames them.** Never guess it; look.

## 1. Collect evidence

For each speaker, pick 2 or 3 moments where *only they* speak for several seconds (long utterances in
`work/transcript.json`, away from the start). For every camera, extract a still at that episode time
(file time = `(t - start_offset) / (1 + drift_ppm*1e-6)` from `sync.json`; the drift term only matters late in a long take):

```bash
ave frame <episode> raw/camA.mov 312.4
ave frame <episode> raw/camB.mov $(python3 -c "print((312.4 - 4.2499) / (1 + 24e-6))")
```

Open the JPEGs under `work/stills/` and look. You are answering: whose face/body is the subject of this frame,
and is their mouth moving while the audio says they are talking?

A camera can also be a **wide** (both people). Record it as role `wide`; the planner can use it as a fallback.

## 2. Write it down

Edit `<episode>/episode.json` (keep existing keys):

```json
{
  "cameras": {
    "A": {"file": "raw/camA.mov", "role": "host"},
    "B": {"file": "raw/camB.mov", "role": "guest"},
    "W": {"file": "raw/wide.mov", "role": "wide"}
  },
  "speakers": {
    "S1": {"name": "Maren", "camera": "A"},
    "S2": {"name": "Guest",    "camera": "B"}
  },
  "audio_source": "raw/recorder.wav"
}
```

### Several sessions (field note N1)

`ave ingest` printed `sessions` in `media.json` (cameras that started together and run the same length). Give every camera a `session`, and tell the
planner which camera frames each speaker in each session; `camera` stays as the default:

```json
"cameras": {
  "A1": {"file": "02_FOOTAGE/IMG_0002.mov",   "role": "host",  "session": "s1"},
  "B1": {"file": "02_FOOTAGE/IMG_0002 2.mov", "role": "guest", "session": "s1"},
  "A2": {"file": "02_FOOTAGE/IMG_0003.mov",   "role": "host",  "session": "s2"},
  "B2": {"file": "02_FOOTAGE/IMG_0003 2.mov", "role": "guest", "session": "s2"}
},
"speakers": {"S1": {"name": "Maren", "camera": "A1", "cameras": {"s1": "A1", "s2": "A2"}},
             "S2": {"name": "Guest",    "camera": "B1", "cameras": {"s1": "B1", "s2": "B2"}}}
```

A camera's angle can differ between sessions (people swap seats): look at stills from EACH session, never assume session 2 matches session 1. `session` may be omitted
when `media.json` already groups the file (it is the fallback).

`camera` values must be keys of `cameras`. `audio_source` is the one continuous audio track under the whole cut
(best microphone; defaults to the transcribed file). Camera audio is *not* cut with the picture.

## 3. Checkpoint

Show the human the mapping and the stills you based it on, e.g. "S1 = Maren on camera A (stills at 05:12 and
11:40, mouth matches speech)". Wait for a yes before `ave plan-cuts`.

## Failure modes

- **Diarization swapped two speakers mid-episode** (they sound alike): a still at a late timestamp will contradict the early evidence. Check one early and one late moment per speaker.
- **Two speakers on one camera, one on another:** map each speaker to the camera that frames them best; the other person shares it.
- **Mic-per-person recording:** speaker identity is the file, not the voice. Transcribe each mic separately with `--no-diarize` and treat each file as that speaker.
- **A speaker with no camera in a session** (`speakers.<id>.cameras` has no entry for it and `camera` belongs to another session): their lines do not trigger cuts in that session; `ave plan-cuts --session` errors only if NO speaker has a camera there.
- **A speaker not on any camera** (off-screen host): leave `camera` unset; their lines will not trigger cuts.
