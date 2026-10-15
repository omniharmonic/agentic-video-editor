---
name: Footage compatibility report
about: A camera, recorder, codec or drive that works, breaks, or needs a workaround
labels: footage-compat
---

Please do not attach footage. We need the shape of it.

**Outcome**

- [ ] Works end to end
- [ ] Works with a workaround (describe below)
- [ ] A stage fails or warns (name the stage and paste the message)

**Stages you ran** (ingest, audio, transcribe, sync, plan-cuts, assemble, cleanup, final, handoff, ...)

**Equipment**

- Cameras (make/model, container, codec, resolution, frame rate, variable or constant frame rate, 8/10-bit, log profile):
- Audio recorder or mics (model, number of channels, one mic per speaker or a mixed track, sample rate, bit depth):
- Number of cameras, and whether clocks drifted (`work/sync.json` `drift_ppm`):
- Drive (internal, USB, exFAT/APFS/HFS+):
- Total length and number of sessions:

**`ffprobe` summary of one file of each kind** (`ffprobe -hide_banner -show_streams -show_format -of json FILE`, trimmed to the
interesting fields; remove file names and metadata tags that identify people or places)

```
```

**`ave doctor` output** (replace your home folder with `~`)

```
```

**What you had to do by hand, and what you would like the tool to do**
