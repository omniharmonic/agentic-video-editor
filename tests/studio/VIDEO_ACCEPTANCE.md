# Video test inputs

Ordinary collected service/review/browser tests generate small deterministic stereo PCM,
ProRes, lossless time-coded picture and synthetic transcript/kept-authority fixtures in
pytest's temporary directory. `video_fixtures.py` is the generator. They require the
repository's normal FFmpeg/Python/Playwright dependencies, no session directories,
ASR model download, private recording, provider or human approval. Synthetic transcript
and keep rows are test data, never purported ASR/human evidence.

Four explicitly provisioned cases are gated: two actual-ASR GUI widths, one full-hour
GUI and one two-recording actual-speech project GUI. Missing environment configuration
skips only these cases with a reason. Supplying a missing/bad path fails loudly.
The ordinary portable kept-source GUI exercises file-clock word clicks, occurrence
correction, real proxy/audio render and advancing playback at both widths regardless.

To provision fresh slow acceptance, supply two original/licensed speech WAV files of
at least6seconds each. Nothing fetches or records speech:

```sh
.venv/bin/python scripts/prepare_video_acceptance.py --speech-one /path/first.wav --speech-two /path/second.wav --output /tmp/ave-video-acceptance
AVE_VIDEO_SPEECH_FIXTURE=/path/first.wav AVE_VIDEO_LONG_ACCEPTANCE=/tmp/ave-video-acceptance/real-workers.json .venv/bin/python -m pytest -q -rs tests/studio/test_video_workshop_browser.py
```

The provisioner copies each input, creates repeated3600s PCM and video, writes exact
SHA/size input-manifest.json, runs actual app import/local-ASR jobs, and constructs two
managed programmes. The full-hour tests append to persisted programmes and assert
prior duration +8s. Repetition is processing evidence, not natural-conversation accuracy.
No direct engine benchmark or real approval is performed.

Existing accepted2026-10-29 provisioned receipt can be selected explicitly with
`AVE_VIDEO_LONG_ACCEPTANCE=/private/tmp/ave-media-video-evidence/real-workers.json`
and `AVE_VIDEO_SPEECH_FIXTURE=/private/tmp/ave-conversation-fixture-20261029/turn-01.wav`.
Those are optional session inputs, never default test dependencies. Exact original
input SHA/size values are retained in that session's source-identities.json and
real-workers.json. The repair did not rerun full-hour ASR.
