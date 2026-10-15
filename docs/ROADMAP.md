# Roadmap and status

Last updated 2026-10-14. This file is the single place that says what is built, what is weak, what is unverified, and what we are working on next.
Detailed evidence lives in [field-notes/](field-notes/) (working notes from the first real episode); designs are in
[superpowers/](superpowers/) and [proposals/](proposals/). Every open item names how we will know it is done. A reading guide to all docs: [README.md](README.md).

## Status at a glance

"Verified" means it ran on a real recording (the first episode: two hosts, two 4K cameras, one two-channel recorder) and the result was checked.
"Built, synthetic only" means it has automated tests on generated media (or against a fake Resolve bridge or mocked HTTP) but has not been exercised end to end
on real material, a live Resolve, or a real account. Treat everything not "verified" as something you will need to check yourself.

| Area | Status | What that means |
|---|---|---|
| Ingest, sessions, project layouts, progress protocol (`PROGRESS.md`) | Verified | Ran on the real episode; flat and numbered layouts |
| Speaker-per-channel detection, transcription with word timing and speakers | Verified | About 340x real time on Apple Silicon; speakers read off the channels |
| Multicam sync with drift measurement and long offsets | Verified | Four cameras, 24 to 80 ppm drift; piecewise map for clock wobble is built but not re-run on the real episode |
| Cut planning, assembly, rough cut (cleanup with masks) | Verified | Cleanup numbers are untuned defaults (W6) |
| Resolve handoff: single timeline, layered handoff, chunked and resumable | Verified in live Resolve 21.0.x | Free Resolve needs the in-app bridge started by hand each launch |
| Final cut (music, motion, captions, loudness, exact-match SRT) | Verified for one session of the real episode | The other session's final is not rendered (hours at 4K) |
| Shorts (reframe, captions, layouts) | Verified on four trial shorts | Creator feedback is folded in; more material needed |
| Clips: recall-first proposals, completeness, review ledger, taste profile, previews, `ave ui` | Verified on the real episode | Awaiting the creator's keep/maybe/skip decisions at scale |
| Opening and closing sequences | Built; smoke build on the real cut | No real music or sting; creator decisions pending |
| Release package (`ave package`) | Built, synthetic only | A dry run on the real episode found nothing to package yet |
| Publishing (YouTube, Facebook, aggregator) and its approval gate | Built, mocked HTTP only | Never posted to a live account; platform limits are from documentation |
| Content kit (quote posts, carousels, thumbnails) | Built; in-house renderer exercised on real quotes | Canva renderer: built, never run live |
| Review proxies, subtitle import, alpha overlays in Resolve | Built, unverified in real Resolve | Each has a short manual check (see W7, W10 and the unverified list) |
| Third camera (wide), room tone, pacing | Built, synthetic or partly real | Not yet heard or seen on a real third camera; the creator has not signed off the feel of pacing |
| Windows or Linux transcription, voice library, bleed-free filler detection, applying tighten cuts automatically, live posting to any platform | Not built | See "Later" and "Not built" below |

**Known limits, short version:** Apple Silicon macOS only today; free Resolve exposes scripting only through its in-app bridge and free 21.1+ cannot run Python
scripts; native multicam angle switching is not scriptable, so timelines are flat and editable; a real recording always finds bugs fixtures do not (four so far
before round 2). The full list is under "Known limits" below.

## Built and verified on real footage (the first episode)
Ingest with **sessions**, **speaker-per-channel** detection (speakers come free from the channels), transcription with word timing (about 340x real time), **drift-corrected multicam
sync** with long offsets (all four cameras, 24 to 80 ppm), per-session **assembly** and **rough cut** (cleanup with masks), the **progress protocol**
(`PROGRESS.md`, `ave log/learn/progress/resume`), your numbered folder layout on the external drive, and a **verified Resolve handoff** into a dedicated project
(chunked, resumable). Built and tested on a smaller real clip plus synthetic fixtures: final cut (music, motion, captions, loudness, exact-match SRT), clips + review page,
vertical shorts, approval-gated publishing (mocked HTTP only), motion kit, B-roll/music assets.

**Opening and closing sequences (workstream I, built; synthetic-fixture tests plus a smoke build on the real first-episode cut, no music).** `ave sequence plan|preview|build|status` (+ human-only `approve`), `config/sequences.default.yaml`
(per-show overridable), `config/final.default.yaml` `sequences: {opening, closing}`, skill `ave-intro-outro`, reference `docs/sequences.md`. Opening: optional cold open (clip, quoted passage or range, captioned, ducked bed), title card, host card with titles, optional
sting/bed. Closing: thanks/CTA cards, credits roll (hosts, guests, editor, license-log asset credits, links), held end card for YouTube end screens, tail room tone. `ave final` joins opening + body + closing
(stream-copy picture, float audio with 0.2 s equal-power fades, one loudness pass, measured), planned SRT shifted by the opening, Resolve handoff places them as clips. **Not yet done:** the creator's decisions (cold open, links, editor credit, brand fonts), real music/sting
(none exists in the license log), a live Resolve placement of the sequence clips, an overlapped (rather than fade-through) audio crossfade at the joins, and a dissolve (not a cut) in the picture at the joins.

## Active: weak points we know about (work these next, in this order)
| # | Weak point | Why it matters | Done when |
|---|---|---|---|
| W1 (built, awaiting ear) | **Pacing.** Long turns leave long holds (103 holds over 14 s in session 1; assembly averages 62 s per shot). Masks only fire at cut points. | A conversation shown from one camera for a minute is dull; the creator wants rhythmic coverage with reaction shots. | A pacing pass inserts listener reaction cutaways (2-4 s) at sentence boundaries when a hold exceeds the policy limit; zero unflagged holds over the limit; the creator signs off the feel on a real session. `should_cut_to_listener` gets real defaults, tuned by the creator. **Status:** built (T12): cleanup `pacing:` pass + `config/cutplan.default.yaml` interjection policy. Real session copies: holds over 14 s 103 -> 55 (S1), 23 -> 10 (S2); none over 18 s except one ASR-bound hold; cuts/min rose 11.8 -> 13.7 because the cutaway masks already cut 10-12 a minute. Open: the creator signs off the feel and tunes `max_hold_s` / `masks.*`. |
| W2 (built) | **Real start/end.** Recordings open with setup chatter ("okay, we're recording... take one"); the assembly starts at t=0 of the first camera frame. | Every episode needs a human-approved in/out. | A `trim` suggestion (scan the first and last minutes of the transcript for slate talk, the welcome, the sign-off) proposes in/out per session; the human approves; assembly honors `episode.json` trim. **Status:** built (T12): `ave trim suggest|set|show|clear`, `config/trim.default.yaml`, assemble honours `episode.json` `trim`, `ave resume` asks. Real session 1: proposes in 188.9 s / out 7022.8 s; needs the creator's approval. |
| W3 (built) | **Sync wobble.** Camera A in session 1 leaves a 41 ms residual against a straight drift line. | About one frame of potential lip-sync error mid-take. | Piecewise-linear offset through the measured points when the residual exceeds 20 ms; residual under 10 ms at every sample point; end-to-end test. **Status:** built (S): when the straight-line residual exceeds 20 ms `sync.json` also stores `sync_map` (`[[file_t, offset], ...]`, the measured points) and `editdoc.src_time` / `episode_time` interpolate through it (extrapolating with the fitted line; `clock_rate` gives the local stretch). `assemble` copies it onto `sources[k]`; compile, cleanup's envelope, cutplan, handoff, shorts and review packs all go through the one function (no duplicated formula left). Files without `sync_map` are unchanged. Evidence: `tests/test_sync.py::test_piecewise_map_follows_a_wobbling_clock_within_10ms_at_every_sample_point` (45 ms wobble, 9 points: under 10 ms from the true clock at every sample point) and the round-trips in `tests/test_editdoc.py`. `ave sync` warns only for a mis-locked-looking segment (>1500 ppm off the line) or a residual the map could not fix. Not yet re-run on the real episode: re-sync camera A of session 1 and compare. |
| W4 (partly done) | **Captions, final cut, clips, shorts, mock publish have not run on the real episode.** | Real footage keeps finding bugs our fixtures could not (4 so far). | Each stage run on the real project with its learnings logged and fixed. |
| W5 (built) | **Room tone in the rough cut** so selects timelines get gaps between clips. | Clips are butt-joined today (the builder warns). | `tracks.room_tone` filled automatically from a quiet stretch; gaps rendered without a change in noise floor. **Status:** built (S): `ave cleanup` fills `tracks.room_tone` (policy `room_tone:`) with the quietest steady 2-4 s stretch of the dialogue source's analysis audio (already the L+R sum for speaker-per-channel masters, the live channel for dead-channel files), inside the take; compile level-matches every `fill: room_tone` gap to the noise floor on either side (`compile.match_gain_db`, ignoring neighbours with no pause); the selects builder warns only when a rough cut has no room tone. Evidence: `tests/test_room_tone.py` (rendered gap RMS within 1.5 dB of the surrounding floor, including a stretch 4-8 dB quieter than the floor; transients, speech and silent pads rejected; the sum decides for speaker-per-channel). Not yet heard on the real episode. |
| W6 | **Cleanup numbers are untuned defaults** (pause targets, filler thresholds, mask choices). | They decide whether the edit sounds natural. | Listening protocol: A/B the creator's ears on 3 sections, adjust `config/cleanup.default.yaml`, record the chosen values and why. |
| W7 | **Subtitles in Resolve.** Free Resolve will not place an SRT on the timeline through its API (not measured on 21.0.4; the MCP notes one report that ImportMedia+AppendToTimeline works on Studio 21.0.4.5). | The creator wants subtitles in the Resolve timeline for tweaking. | **Built, UNVERIFIED in real Resolve.** `ave handoff --try-subtitles` imports the SRT and appends it, and calls it a success only if a subtitle track exists with as many items as the SRT has cues (otherwise it prints exact steps: File > Import > Subtitle...). The SRT is always copied to `captions/<timeline name>.srt`. **Done when** the 2-minute check in `ave-resolve-handoff` passes on free 21.0.4 and this row is edited with the result. Styled captions stay burned in by FFmpeg. |
| W8 (built) | **Third/fourth angles.** Planner assumes one camera per speaker. | The creator plans a third camera (wide). | `role: wide` cameras used for establishing/reset shots by policy; tested with synthetic three-camera fixtures. **Status:** built (T12): the planner opens with an establishing wide and shows it for crosstalk; cleanup masks and pacing prefer it; tested on a 3-camera synthetic episode. Not yet seen on a real third camera. |
| W10 | **Review proxies.** Playing 4K HEVC (two angles, from an ExFAT USB drive) in Resolve garbled the audio under load (the audio file itself was verified clean by A/B). | Smooth review is the point of the handoff. | **Built and tested with real ffmpeg + the fake bridge; linking UNVERIFIED in real Resolve.** `ave proxies` makes ProRes Proxy / DNxHR LB proxies (VideoToolbox decode when available, size estimate and 1.5x free-space refusal, resumable, verified, in `<footage dir>/proxies/`, never touches originals); `ave handoff --use-proxies` links them with MCP `link_proxy_checked` (the timeline and renders still name the originals) and reports what the bridge read back. **Done when** the manual check in `ave-resolve-handoff` passes on the real drive. |
| W11 (verified live) | **Layered (work-in-progress) handoff.** The handoff delivers one consolidated timeline: chosen angle only, one baked audio clip. | Editors need to grab another angle and adjust cuts; the creator asked for exactly this. | `ave handoff --layers` stacks every other angle in sync on disabled tracks under the edit, cuts the dialogue into editable clips from a dual-mono sum of the recorder, and keeps the baked master on a muted track; verified by read-back in Resolve. **Status: built and verified live** (plan, sum-WAV, chunked/resumable executor verified against an in-memory fake bridge, a real `--dry-run`, and live Resolve); **Verified live on the real project (field note N15):** V1 the edit, the other angles as disabled tracks, the dialogue as editable clips, the baked master on a muted track. |
| W12 | **Per-session edits in clips, captions and shorts.** Only cleanup/final/render/handoff understand `--name`. | Selects, shorts and content cannot run on a multi-session episode. | `--name` on every stage; clips record their exact source edit; sessions coexist in `clips.json`; real project runs clips prepare/propose/build for s1 and s2. |
| W9 (built) | **Legacy `ave build-timeline`** still reads only `cutlist.json`. | Two code paths for Resolve. | Removed or made an alias of `ave handoff`. **Status:** built (S): `ave build-timeline` prints a deprecation notice and runs `ave handoff --stage assembly` (making the assembly from `cutlist.json` first if needed; `--name` is the label); `scripts/resolve_build_timeline.py` deleted. Evidence: `tests/test_build_timeline_alias.py` (handoff's plan of the assembly equals the cut list's Resolve numbers shot for shot, audio placement within a frame; alias and handoff write identical plans). |

## Round 2 status (2026-10-14)
- Built and merged (1127 tests): recall-first clips with a decision ledger and per-show taste profile (`ave learn taste`), complete-statement boundaries (`src/ave/segment.py`) and tighten suggestions (`src/ave/tighten.py`), `--top N/--min-score`, in-context clip previews (`ave clips previews`), the redesigned `ave ui`, `ave sequence` (opening/closing/credits), CO-HOST byline + bundled Newsreader/Inter fonts, Canva handshake (unverified live), `ave package`, shorts layouts (stack, audiogram).
- Run on the real episode: final cut s2 (v9: -16.6 LUFS, exact SRT, lower thirds); four trial shorts; session 1 final NOT rendered yet (hours at 4K).
- Needs the creator: listen to the final s2 master; judge the shorts drafts; approve clips (UI); decide opening/closing sequences (`ave sequence approve` is a human act); live Resolve checks for proxies, subtitles and the alpha swatch; Canva sign-in in this session, Brand Kit and Brand Templates.
- Not built: voice library, bleed-free filler detection from isolated mics, portable (non-Apple) transcription, applying tighten cuts to a select automatically, live posting to any platform.

## Release package (P2, built 2026-10-14)
`ave package <ep>` assembles `<exports>/package/<date>/`: the latest final render, SRT + VTT, podcast MP3/M4A with ID3 chapters and a measured -16 LUFS check, approved 1280x720 thumbnails (an oversize PNG
ships as a JPEG), approved shorts per platform, approved quote cards and carousels with alt text, per-platform post-copy drafts (YouTube title/description/tags/chapters; Instagram, Facebook, X, LinkedIn,
TikTok, Threads, Bluesky within the limits in `config/platforms.yaml`, voice-checked by `config/package.default.yaml`), `manifest.json`, `checksums.sha256`, `index.html`, README. Unapproved items go to
`drafts/` only. Dated folder, never overwritten (`-v2` on change, `unchanged` otherwise), copy-or-reference by free space (no links on ExFAT). `ave package queue` adds approved items to the publish queue
as drafts; it has no send path. Stage `package` shows in `ave status/resume/progress`; the checklist is in `work/package/status.json` for the UI. Tests: `tests/test_package.py` (synthetic episodes).
**Not verified on real footage:** the real episode has no final render, no approved clip and no thumbnail yet ([dry-run field note](field-notes/2026-10-14-package-dryrun.md)), so audio/chapters on a 2-hour
file, the drive's copy speed, and the post-copy voice on real quotes are untested. Open: LinkedIn/Threads/Bluesky limits are unverified (`via: manual`); chapters derived from the transcript are keyword drafts.

## Unverified: do not assume these work
- Live posting to any platform (everything was run against mocks; platform limits in `config/platforms.yaml` and `config/shorts.yaml` come from documentation).
- Resolve alpha import of ProRes 4444 overlays (`ave motion swatch`, one check in Resolve).
- Brand fonts are bundled (variable Newsreader and Inter, SIL OFL, `motion-kit/brand/fonts/SOURCES.md`); caption burn-in (libass) with variable fonts and renders on another machine are not verified.
- `handoff --no-master` for stereo/speaker-per-channel sources (warns; not exercised).
- Dead-channel handling on a real file (the real master turned out to be speaker-per-channel instead).
- Provider terms, Content ID and AI-music licensing notes in `docs/asset-licensing.md`.
- Resolve behavior on very large appends: handled by chunking and resume after a real `stale_handle`; the null-item-id failure was transient and not root-caused.

## Proposals awaiting a decision
- **Agentic content pipeline (quote posts, thumbnails, carousels, reel cards; Canva as an optional renderer).** **Phase 1 BUILT (T14): in-house renderer, ContentDoc, review, image/carousel publish queue; phase 2 Canva renderer BUILT as an opt-in handshake (T15: `render_canva.py`, job files, approval gate, import, recorded fallback, `skills/ave-canva-render`; fake-client tests only) but NOT verified live: the Canva connector was not signed in, so discovery is open ([field note](field-notes/2026-10-14-canva-discovery.md)).** Researched: [docs/proposals/agentic-content-pipeline.md](proposals/agentic-content-pipeline.md).
  Recommendation: hybrid with the in-house renderer first (free, deterministic, open-source friendly), Canva as an opt-in second renderer, additive code under `src/ave/content/`, a `ContentDoc` parallel to `EditDoc`,
  publish queue gains image/carousel support with the same hash-bound human approval. **DECIDED 2026-10-13: Canva is in v1; the creator is on Canva Pro** (autofill, brand templates and resize are available). Still needed from the creator (list in the field note): the one-time OAuth sign-in (`/mcp` > claude.ai Canva), Brand Kit (palette, fonts) in Canva, and 2-3 tagged Brand Templates.

## Later
1. Voice library: persist speaker embeddings across episodes (per-channel files may not need it; keep for single-mic shows).
2. Two-up (stack) vertical layout; gaze-aware crops.
3. Direct Instagram/X adapters if app review is worth it; TikTok.
4. Generative B-roll/motion through user-approved APIs; licensed music sourcing.
5. Windows/Linux transcription (FluidAudio is Apple-only; sherpa-onnx + pyannote ONNX is the portable path).
6. Cleanup using each host's isolated mic channel (bleed-free filler detection and cut snapping).
7. A combined episode timeline (sessions joined) once the creator decides which sessions are in.

## Known limits
Free Resolve exposes scripting only through the in-app bridge (started by hand each launch); free 21.1+ cannot run Python scripts; native multicam angle switching is not scriptable, so we build flat editable timelines;
Resolve gates timeline deletion behind a human confirmation, so failed handoffs keep a resumable partial timeline.

## How we work this list
`ave learn` records a finding in the project's `PROGRESS.md`; when it generalizes, it is promoted here and into the affected skill, with a regression test, and the note is marked `applied` with the commit.
Real footage always beats fixtures: when a stage runs on the real episode, its findings go into `docs/field-notes/` the same day.
