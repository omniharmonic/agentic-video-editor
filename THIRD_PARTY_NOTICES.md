# Third-party notices

The project's own code is MIT licensed (see [LICENSE](LICENSE)). It bundles two fonts, and it can fetch, build, or call several
other tools and models on your machine. This file says what each one is, where it comes from, and under what terms. Nothing under
"Fetched, not bundled" is part of this repository or of anything you redistribute from it; you obtain it from its own source under its
own license.

How to read the **Status** column:

- **Verified** means the license text was read in a file on disk (in this repository, in the pinned checkout under `vendor/`, or in an
  installed package's metadata) on 2026-10-14.
- **Unverified** means the statement comes from memory of the upstream project's public pages and must be re-read at the source
  before you rely on it, especially before redistributing a model or a build.

This file is a good-faith inventory, not legal advice.

## Bundled in this repository

| Item | Where | License | Status |
|---|---|---|---|
| Newsreader (variable font, `Newsreader-Variable.ttf`), Copyright 2020 The Newsreader Project Authors, designer Production Type | `motion-kit/brand/fonts/` | SIL Open Font License 1.1, text in `OFL-Newsreader.txt` | Verified (license file read; sha256 of the font and license match `SOURCES.md`; no Reserved Font Name is declared) |
| Inter (variable font, `Inter-Variable.ttf`), Copyright 2020 The Inter Project Authors, designer Rasmus Andersson | `motion-kit/brand/fonts/` | SIL Open Font License 1.1, text in `OFL-Inter.txt` | Verified (same checks) |

The fonts were fetched from the Google Fonts repository at a pinned commit and only renamed (bytes unchanged); provenance and hashes are
in `motion-kit/brand/fonts/SOURCES.md`. The OFL allows bundling, embedding in rendered video and images, and redistribution with
the license text, but the fonts may not be sold on their own. If you change a font, keep its license file next to it.

No other third-party code, images, audio, or video is committed. Test media is generated at test time. The two small JSON files in
`tests/data_assets/` named `pexels_videos_search.json` and `pixabay_videos_search.json` are hand-written samples shaped like API responses (invented
author names, placeholder ids) used to test the response parsers; they contain no downloadable media.

## Fetched or built by the setup scripts, not bundled (`vendor/` is git-ignored)

| Item | How it is obtained | License | Status |
|---|---|---|---|
| **davinci-resolve-mcp** (Resolve MCP server, pinned to tag `v4.10.6` in `src/ave/config.py`) by DaVinci Resolve MCP Contributors and Bradford Operations LLC | `git clone` of `github.com/samuelgursky/davinci-resolve-mcp` by `scripts/setup.sh`, plus its own `requirements.txt` (`mcp[cli]<2`, `pyaaf2`) | MIT (its `LICENSE` read in the checkout; `pyaaf2` is described as MIT in that file's comments) | Verified for the server; the `mcp` SDK license Unverified here |
| **FluidAudio** (Swift SDK and `fluidaudiocli`; pinned to commit `20d4f0b` in `scripts/build-fluidaudio.sh`) | `git clone` of `github.com/FluidInference/FluidAudio`, built with `swift build` | Apache License 2.0 (its `LICENSE` read). Its `ThirdPartyLicenses/` folder carries `vbx-LICENSE.md` (Apache-2.0, Copyright 2021-2024 BUT Speech@FIT, original VBx project), `fastcluster-LICENSE.md` (BSD-style, Copyright Daniel Muellner / Google), plus NeMo text-processing and G2P notices for components this project does not use | Verified |
| **whisper.cpp** (`whisper-cli`, Metal build, fallback ASR) | `brew install whisper-cpp`; not built or vendored by this repo | MIT, Copyright 2023-2024 The ggml authors (read in the Homebrew-installed copy, version 1.8.3) | Verified |
| **Whisper model weights** (`ggml-large-v3-turbo.bin`, about 1.6 GB) | `scripts/fetch-whisper-model.sh` downloads it from the `ggerganov/whisper.cpp` repository on Hugging Face into `~/whisper-models` | OpenAI Whisper weights are published under the MIT license | Unverified (the model card was not read) |
| **NVIDIA Parakeet TDT v3** (Core ML conversion used by FluidAudio, `FluidInference/parakeet-tdt-0.6b-v3-coreml`) | Downloaded by FluidAudio on first use into `~/Library/Application Support/FluidAudio/Models` | The upstream NVIDIA Parakeet TDT 0.6B v3 model is published under CC-BY-4.0 (attribution required: credit NVIDIA and link the license). FluidAudio's docs call its model set "permissive (MIT/Apache 2.0)" and do not restate the per-model terms | Unverified. Read the model card of the exact Hugging Face repo before redistributing the model or a product built around it |
| **pyannote diarization pipeline, WeSpeaker embeddings, VBx clustering, Silero VAD** (Core ML conversions used by FluidAudio, `FluidInference/speaker-diarization-coreml` and `silero-vad-coreml`) | Downloaded by FluidAudio on first use | pyannote segmentation models are published under MIT or CC-BY-4.0 depending on the release; WeSpeaker is Apache-2.0 (code) with separately licensed pretrained weights; VBx is Apache-2.0 (verified above); Silero VAD is MIT. FluidAudio pins the download to an immutable revision and keeps a PROVENANCE.md per model repo | Unverified. The terms of the pretrained weights are the part to read at the source |
| **Chromium** (headless, for the motion and content renderers) | `python -m playwright install chromium` | Chromium and its components carry a mix of BSD-style and other open-source licenses; Playwright downloads a build with its own license bundle | Unverified |
| **DaVinci Resolve** (the human's review and polishing tool) | Installed by the user from Blackmagic Design | Proprietary. The free edition's scripting limits are described in `docs/ROADMAP.md`. Nothing from Resolve is bundled or redistributed | Not applicable |
| **FFmpeg / ffprobe** | Installed by the user (`brew install ffmpeg-full`, `apt-get install ffmpeg`, or any build) | Called as an external program; the license depends on the build. A build that includes `libx264`, `libx265` or other GPL components is GPL; the encoders the project asks for are `libx264`, `libmp3lame`, `aac`, and `prores_ks`. Output files are not themselves encumbered by FFmpeg's license, but MP3, H.264 and H.265 encoders have their own patent-licensing considerations for commercial distribution | Unverified for your build. Run `ffmpeg -version` and `ffmpeg -buildconf` and check |

## Python dependencies (declared in `pyproject.toml`, locked in `uv.lock`)

Installed into your own virtual environment, never redistributed by this repository. Licenses below were read from the installed
package metadata on 2026-10-14 (versions as locked then):

| Package | Extra | License |
|---|---|---|
| numpy 2.x | core | BSD-3-Clause (the wheel bundles components under 0BSD, MIT, Zlib, CC0) |
| scipy 1.x | core | BSD-3-Clause |
| soundfile | core | BSD-3-Clause (the wheel bundles libsndfile, LGPL-2.1; dynamic linking, no changes) |
| pyyaml | core | MIT |
| pillow | core | MIT-CMU (HPND-style) |
| opencv-python-headless | `vision` | Apache-2.0 (the wheel bundles FFmpeg libraries under LGPL-2.1; see OpenCV's own notices) |
| pyobjc-framework-Vision | `vision`, macOS only | MIT |
| librosa | `audio` | ISC (depends on numba BSD-2/3 and llvmlite BSD-2 plus Apache-2.0 with LLVM exception, and others) |
| playwright (Python package) | `motion` | Apache-2.0 |
| google-api-python-client, google-auth-oauthlib | `publish` | Apache-2.0 |
| requests | `publish`, `assets` | Apache-2.0 |
| keyring | `publish` | MIT |
| pytest | dev | MIT |

Nothing in the dependency set is copyleft in a way that reaches this project's own code (the LGPL components above are separate
shared libraries inside third-party wheels). Transitive dependencies of the optional extras were not individually audited; run a
license scanner on `uv.lock` (for example `pip-licenses`) if you need a full list.

## Services and content you bring or fetch at run time

These are not software we ship; they carry terms you accept when you use them. `docs/asset-licensing.md` has the toolkit's
working notes, all of which are marked unverified there.

- **Pexels API and Pixabay API** (stock video search, optional): you need your own API key (`PEXELS_API_KEY`, `PIXABAY_API_KEY`) and
  you are bound by each service's API terms and content license. As recorded in `docs/asset-licensing.md` (UNVERIFIED, re-read at
  the source): Pexels asks for attribution where the API is used and caps request rates; Pixabay asks that results be cached and files
  downloaded rather than hotlinked; neither guarantees model or property releases for recognizable people or logos. The license log
  records the license and credit line of every downloaded file, and `ave final` refuses unlicensed assets.
- **Music** (Epidemic Sound, Artlist, Creative Commons tracks, AI-generated music, and so on): the toolkit does not download music. You
  record each track's license in the log. Plans differ on which platforms and on podcast use, Content ID claims can still occur, and
  AI-generated music has unsettled ownership. See `docs/asset-licensing.md`.
- **Platform APIs** (YouTube Data API, Meta Graph API, an aggregator service you choose): you create your own developer
  project and credentials and are bound by the platform's developer terms. Uploads from an unaudited YouTube API project can be
  forced private (see `docs/publishing-setup.md`).
- **Canva** (optional renderer handshake): requires your own Canva account and its connector; subject to Canva's terms. Unverified live.

## Models are not covered by this project's MIT license

The MIT license on this repository covers this project's code, skills, policies and docs only. Model weights that tools download on
your machine (ASR, diarization, VAD) are separate works under their own terms, as listed above.
