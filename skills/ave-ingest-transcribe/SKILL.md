---
name: ave-ingest-transcribe
description: Probe raw footage and produce a word-timed, speaker-labelled transcript for an episode. Use when new footage lands in raw/ (or 02_FOOTAGE/03_AUDIO), when the user asks for a transcript or who said what, or before any cut planning. Covers ave ingest (sessions, dead channels), ave audio, ave transcribe, speaker naming and quality checks.
---

# Ingest, audio, transcribe

```bash
ave ingest <episode>             # -> work/media.json  (duration, fps, resolution, audio streams, timecode)
ave audio <episode>              # -> work/audio/*.16k.wav  (analysis copies; raw/ untouched)
ave transcribe <episode> --speakers 2     # -> work/transcript.json, transcript.md, speakers.json (speakers.json only when the diarizer ran)
```

## Read the ingest output

- `warnings` in `media.json`: mixed frame rates (conform in the timeline), video with no audio (cannot be synced by audio).
- **`sessions`** (field note N1): cameras are clustered into sessions (takes) by identical `creation_time` (within 120 s) and similar duration (within 2 s): `s1`, `s2`... each `{id, files, creation_time, duration, label}`. A recorder file (audio only) is listed under `recorders`. Give a session a `label` ("experimental") by editing `media.json`; re-ingest keeps it.
- **Sanity-check the durations** before trusting that audio and video belong together: each recorder has `sum_session_durations` and `difference_s`. A recorder longer than any ONE session probably spans several (the warning says so); the difference is lead-in, dead space between sessions and tail on no camera (real episode: sessions 8346.1 s vs audio 8437.6 s = 91.5 s, i.e. 4.3 s lead-in + 84.2 s dead space + 3.2 s tail). A difference of tens of minutes, or sessions longer than the audio, means the files are not one recording: stop and ask.
- **Channel layout** (field note N4, corrected): every audio file gets `channel_layout` plus `channel_evidence` (correlation, left/right dominant share, alternations) in `media.json`. It is classified from the WHOLE file, never from a few sampled minutes (the first guess on the real master, "one live channel per session", came from 3 sample minutes and was wrong).
  - `speaker_per_channel`: two independent mics, one host each (|corr| < 0.3, each channel dominant by >= 6 dB for >= 10% of the loud time, several alternations). `channel_map` is a single `use: "sum"` range and `ingest` prints ONE `info` line instead of per-range warnings. `ave audio` writes L+R (not the average: that loses 6 dB; a soft limit stops clipping), renders play the sum as dual mono, and `ave transcribe` takes speaker labels from the channels. The real master (`THE_LONG_TABLE_00_silly.wav`) is this: corr -0.005, 158 alternations, and the channels agree with the diarizer on 99.7% of segments over 3 s.
  - `dead_channel`: one channel dead (>= 15 dB down) for stretches with no alternating-independent pattern. `channel_map` has `left|right|mix` ranges, the warning names each range, `ave audio` extracts the LIVE channel and renders fold it to dual mono. Verify by ear on a render of each range.
  - `stereo_mix` / `mono`: nothing special.
  - **Verify against an independent signal before trusting a layout**: run `ave transcribe <ep> --check-diarization` and read `diarization_check.agreement` (expect >= 0.98 for speaker_per_channel). If the verdict is wrong, edit `channel_layout`/`channel_map` in `media.json` and re-run `ave audio --force`.
- Several cameras: check each has an audio track. A dedicated recorder file (audio only) is the best transcription source and is chosen automatically; otherwise the longest video. Override with `--source raw/camA.mov`.

## Transcribe options

- `--speakers N`: **always pass it when you know the count** (a two-person podcast is 2). It stops diarization from inventing or merging speakers.
- `--asr whisper`: use only if FluidAudio is unavailable or the language is poorly covered. No diarization difference; diarization is separate.
- `--language en`: hint when the audio is not English.
- `--speakers-from auto|channels|diarize`: `auto` (default) labels words by the louder mic when `channel_layout` is `speaker_per_channel` (`engine.diarization = "channels"`, `speakers.S1.channel = "left"`, `S2` = right, per-utterance `confidence`; words within 6 dB of each other take the diarizer's label if it ran, else the nearest labelled word, at low confidence), otherwise the diarizer. `channels` on any other layout is an error.
- `--check-diarization`: in channels mode also run the diarizer and write `diarization_check {segments, clear, agree, agreement, mapping}`. Do this once per new recording setup.
- `--no-diarize`: for single-speaker audio, or when each speaker has their own microphone file (then attribute by file, not by voice).

Speed: roughly 200x real time on Apple Silicon (22 minutes in about 15 seconds). Rerunning is cheap.

## Quality checks (do these, then report)

1. `speakers` in `transcript.json`: talk-time split should match reality. A 55/45 split for two hosts is plausible; 95/5 means one speaker was lost, so rerun with `--speakers N`.
2. Skim `transcript.md` start, middle, end. Check the first 30 seconds especially (crosstalk, count-ins).
3. **Known weakness: turn boundaries.** The word at a speaker change can land on the wrong side ("Hey. Oh, hello" / "there"). It does not matter for camera cutting (the lead-in absorbs it) but fix it before publishing captions.
4. Sub-second fragments inside a monologue ("um", "like") are real pauses, not errors.
5. ASR text has no reliable punctuation at word level; utterance text is for reading, word times are for cutting. Names and jargon are often misspelled: collect fixes in `episode.json` under `glossary` and apply them when generating captions.

## Name the speakers (checkpoint)

Diarization gives `S1`, `S2` by first appearance (from channels: S1 = left mic, S2 = right mic, names stay `null` until the human confirms). Show the human two or three lines from each, ask who is who,
then record in `episode.json`:

```json
"speakers": { "S1": {"name": "Maren"}, "S2": {"name": "Guest Name"} }
```

In diarizer mode `work/speakers.json` holds a voiceprint (256-dim embedding) per speaker. Planned: a cross-episode voice library
so recurring hosts are recognized automatically; until then, always ask.

## Which file to transcribe

Episode time is the recorder's timeline when there is one. Transcribe the recorder (the default when an audio-only file exists), so transcript time == episode time == `sync.json` reference time. `ave sync` warns if the transcript's source is not the sync reference.

## Outputs other skills rely on

`transcript.json`: `words[] {w,start,end,conf,speaker}` (channels mode adds `speaker_margin_db`, `speaker_conf`), `utterances[] {speaker,start,end,first_word,last_word,text}` (+ `confidence` in channels mode), `speakers`, `source`, `duration`.
