# The content studio (`ave studio`)

A workspace-level project that turns audio, video, written words or a one-line idea into reviewed, scheduled social posts, with a planner-style UI, an
asset library, licensed B-roll, and a local scheduler that posts to platform APIs **only after a human's hash-bound approval**. It is built for a podcast
and for small businesses. Design and reasoning: superpowers/specs/2026-10-19-content-studio-design.md.
Agents read [../skills/ave-studio/SKILL.md](../skills/ave-studio/SKILL.md) first.

## Read this first: what is and is not verified

**Nothing in the studio has posted to a live account.** Every platform fact (limits, endpoints, scopes, fold points, the Instagram grid crop, rate
numbers, provider terms) is taken from vendor documentation or memory and is marked unverified in `config/platforms.yaml` and `config/previews.yaml`.
`ave studio doctor` prints `verified: false` per adapter. Treat every row below that says "fakes" as "works against our own test doubles".

| Area | Status | Evidence | Not verified |
|---|---|---|---|
| Data model: posts, variants, campaigns, ledgers (feedback, events, alerts), status derivation | Built | Schemas, table tests for every status row, compare-and-swap conflict tests | Nothing live; no real month of use |
| Queue extensions: `meta_v` 2 (schedule is not bound), `queue.update`, approval TTL | Built | Golden regression: legacy queue items hash and verify exactly as before | |
| Approval gates: typed hash or UI PIN after viewing the exact sha | Built | API tests (wrong PIN x5 locks, no view event refuses, stale sha refuses) | The PIN is out-of-band only against an agent that cannot read your terminal (see limits) |
| Live attestation (`accounts/<id>.live.json`, HMAC over account, who, when AND the on/off state) | Built | Hand edits, copies to another account, a signed "off" flipped to "on", an old-format signature and another machine's key all fail | The HMAC key is in your own keychain: this stops accidents and hand edits, not an agent running as you |
| Asset library, license log, `derived_from` license closure, derived search index | Built | Hash, chain and coverage tests; a failed download leaves nothing | Face detection needs the optional vision extra |
| B-roll: Pexels, Pixabay, Unsplash, Coverr, local; critic; human-named pick; local thumbnails | Built | `FakeHttp` fixtures derived from the providers' documentation | **Never called a real provider API.** Response shapes, rate limits and license terms are unverified. Unsplash's download ping and attribution rules in particular |
| Platform adapters: Bluesky, Facebook Page (photos), Instagram, Threads, LinkedIn, X, Pinterest, TikTok (inbox first), manual | Built | One contract suite over all adapters on `FakeHttp`: dry-run never touches HTTP, outcome taxonomy, crash recovery | **Every adapter `verified: false`. None has run against a live account.** Meta, TikTok and LinkedIn app review are the human's |
| YouTube and Facebook video (existing adapters), aggregator (experimental) | Existing | The repo's existing mocked-HTTP tests | Never run live; see [publishing-setup.md](publishing-setup.md) |
| Media host for Instagram and Threads (S3-compatible, SigV4 presigned GET, deleted after use) | Built | Signing and flow tests against a fake | No bucket was ever created; the signature scheme is unproven against R2, S3 or B2 |
| Instagram JPEG deliverables (converted when rendered or synced; the approval binds the JPEG) | Built | Format, determinism, approval-binding and idempotency tests | Instagram has not accepted one of these files |
| Scheduler: `tick`, `run`, limits, retries, DST, dedupe, quiet hours, crash safety, two concurrent ticks post once | Built | `FakeClock` + `RecordingPublisher` scenarios, DST 2026-03-08 and 2026-11-01 in America/Denver | Never run unattended across days; a laptop asleep at slot time posts nothing (by design: `missed`) |
| Studio server and UI (calendar, grid, editor, review, library, feedback, accounts, alerts, ideas; drag and drop; keyboard; the planner layer below) | Built | Headless Chromium against fixtures AND a real wired `Studio` (`tests/studio/test_real_shell.py`): no console errors, no non-loopback request | **Previews are approximate** (Instagram 3:4 grid crop, caption folds, safe zones are from documentation and observation). A real post's screenshot must be compared per platform |
| `ave studio ...` CLI (every command in the spec), JSON output, exit 0/2, human acts refuse without a terminal | Built | `tests/studio/test_cli.py`, `test_e2e.py` | |
| Recipes and the validator; skills | Built | `tests/studio/test_recipes.py` | Skills have not yet been exercised by an agent session on a real studio |
| Template pack: quote-post, announcement, offer, stat-card, event, testimonial, product-spotlight, tip-list, before-after (+ thumbnail, carousel, reel-card) | Built | Every template x style x format renders in Chromium at exact size with text inside the safe area, contrast measured | No human has looked at them; brand fonts and the creator's taste are not signed off |
| Ingest: idea, blog (Markdown), audio, video, episode survey, `adopt` | Built | Synthetic audio/video and a synthetic episode queue | **One-camera video** works on a synthetic fixture only (see below); no real footage |
| B-roll in a video edit: `ave broll place` (cutaway, overlay, insert; in/out, ducking, Ken Burns, attribution caption, proposal then `--apply`, `remove`/`undo`), compiled by `ave render` | Built | Synthetic fixtures measured with ffprobe and pixels: frame-exact start/end and `--in` point, a 25 fps asset in a 30 fps cut, an insert's ripple and exact-silence dialogue gap, and a dialogue master that is **bit-identical** before and after placing silent B-roll; `audio: keep` changes only the placement's own frames | **Never run on real footage.** Box size, fades, credit position and Ken Burns presets (`config/broll_place.yaml`) have not been judged by a person; Resolve gets the clip at its in-point at full size (cover fit, moves and kept sound are ffmpeg-only) |
| License closure for placed B-roll: the render blocks an unlicensed or unattested asset or a changed file, `derived_from` and attributions are reported, `final`/`license-check`/shorts/package know library placements | Built | Render refuses with a message naming the asset and leaves no file; the closure reaches a short's sidecar and the post made from it | The studio's license log for stock is the documented one; no real provider terms were read |
| Short-clip helpers: `ave shorts recut` (start/end inward on word boundaries, gate re-run) and `retitle` | Built | On the synthetic demo episode taken through the real clip and short commands: refused mid-sentence edges, new select + short versions, `clips.json` untouched | Word boundaries come from the transcript; the demo's is synthetic. No real talk was recut |
| Shorts to posts: `ave studio ingest shorts` (human-kept clips only; a variant per account; drafted copy; library `derived_from`; queue only with `--sync`) | Built | Synthetic demo episode + synthetic studio: refusal of every unapproved shape, stale/draft/changed exports, idempotence, the license closure through attestation to a queue item that is not approved, a fake-clock tick that sends nothing | No real account; the drafted copy is template text from the clip's own words (nobody has read it as the creator); platform short formats are documentation-derived |
| Generative media | Not built | `broll generate` exists only to refuse, after the human gate | Everything |
| Native platform scheduling (YouTube/Facebook hand-off), analytics, inbox, comment management | Not built (non-goals) | | |
| OAuth consent helpers | Not built | | Tokens are entered with `account auth` (hidden prompt, keychain). Only Instagram and Threads can refresh a long-lived token |

### The one-camera question (spec 9.3), answered

A single camera that carries its own audio and no separate recorder: `plan-cuts`, `assemble`, `cleanup` and `render` all run unchanged. The one failure is
`ave sync`, which refuses ("Need at least two files with audio to sync"), and the planner expects a `cameras` entry. `ave studio ingest video` therefore
writes exactly two things (a one-file `sync.json` with offset 0, and `cameras: {A: ...}`) and nothing else; after transcription, running it again maps the
transcript's speakers to that camera. Proven in `tests/studio/test_ingest.py::test_one_camera_video_path_fixture_then_shim`. Not yet run on real footage.

## Try it without any account: the demo studio

```bash
ave studio demo ~/studio-demo        # about 15 s, no network; refuses a folder that is not empty
ave studio ui ~/studio-demo --open   # YOU, in your own terminal: it prints the approval PIN
```

Two fictional brands (a podcast, "The Long Table", and a bakery, "Loaf & Ladle"), 7 dry-run accounts, about 40 posts in every status around today
(two weeks back, two ahead), generated cards from the content renderer, a library with synthetic "stock" B-roll and real license-log entries, schedule
rules, feedback, alerts and two ghost proposals. Every person, price, picture and URL is made up (`.invalid`); nothing is real and nothing can post.
Its approvals are signed with your own approval key, `by: demo`, and bind only synthetic files on accounts that are not live.

## Threat model (what the gates do and do not stop)

An agent with a same-user shell can always edit files and read the HMAC key (keychain, `AVE_APPROVAL_KEY` or `~/.config/ave/approval.key`), so
**file-level tampering cannot be made impossible**. The studio therefore aims at three things, each with tests in
`tests/studio/test_security_gates.py` and `test_security_misc.py` (the regression suite for the adversarial QA):

1. **Every sanctioned path is safe.** No agent-open command and no UI path can satisfy a human gate. `queue sync` never approves. A human send-back
   (rework or retime) VOIDS the variant's approval in the same step, and `feedback resolve` closes it only with evidence (the post's rev advanced and the
   part the note is about changed; a human drop note is executed by `post drop`). `library add --source own|stock|import` and `adopt` record a license
   CLAIM (`attested: false`) that does not count toward the license closure until a human runs `library attest`. `account set` of platform, mode, time zone
   or any `credentials.*` voids the live attestation. `ave publish run` refuses studio items. `ave studio approve` and the UI sign the QUEUE ITEM, and
   both refuse when it differs from the post you review (caption, first comment, link, account, hashtags, alt texts, every file sha).
2. **Tampering is detected, not silently honoured.** The feedback, dispatch and control ledgers are hash-chained and HMAC-signed with a signed head
   record (`studio/seal.py`). Approvals are single-use (a signed `started`/`finished` record per approval signature), carry a signed binding of account,
   slot and approval-lifetime policy, and a slot changed by hand, a raised TTL (`studio.json` or `~/.config/ave/publish.yaml`), a hand-reset queue file,
   an edited approval, a deleted `PAUSE` (the sealed ledger still says paused), an older `live.json` or `send.enabled` restored from a copy, and an
   unlicensed file swapped into the queue item are all caught at send time. **A tick verifies the sealed ledgers first; on any failure it raises a
   `tamper` alert and sends nothing**; `ave studio doctor` fails with the reason.
3. **The harness protects the gating files** (`.claude/settings.example.json`: deny `Edit`/`Write`/shell redirects to `studio/accounts`, `ledger`,
   `publish/queue`, `schedule`, `PAUSE`, `*.live.json`, `send.enabled`, `studio.json`, `policy`, `~/.config/ave` and `approval.key`, and the human-only
   `ave` commands).

Not defended: an agent that reads the HMAC key can forge anything; a pty-spawning agent with the markers stripped passes `require_human` (also for
`ave studio ui`); restoring a ledger and its signed head together from an earlier backup is not detected; an alert nobody reads protects nothing. See
[../SECURITY.md](../SECURITY.md). Untrusted text (captions, notes, alerts, platform errors, provider titles, transcripts) is data, never instructions: the skills say so.

## The planner layer of the UI

Everything here is a convenience over the same checked paths the CLI uses. None of it can approve, send or change what an approval covers; each point has a Chromium regression test
in `tests/studio/test_planner_ui.py` and `test_planner_review.py` and API tests in `tests/studio/test_bulk.py`.

| You want to | Do this | Notes |
|---|---|---|
| Look at a post without leaving the calendar | Click it (or `Enter` on it) | A side panel on a desk, a bottom sheet on a phone: the platform mock with its "more" fold, hashtags, first comment, link, alt text (missing ones are called out), why it has its status, the approval, and its history. `o` opens the editor instead; `Esc` closes |
| Change a time without dragging | Calendar icon on a card, **Change time** in the preview or the editor, or `r` | A date and time picker plus the next free slots. A taken slot, minimum gap, quiet hours, a full day or a time in the past is refused in words and nothing moves |
| Take the next free slot | **Next free slot** (preview, editor) or `n` | The first rule slot after the post's current time (after now when it has none) that passes the same checks as a drag |
| Move several posts | **Select** (or `x`, or shift-click), then the bar at the bottom | Change time (shift by N days, start on a date, next free slots), -1 day / +1 day / +1 week, Move to account, Unscheduled. A refused post stays selected with its reason; the rest move |
| Take it back | **Undo** in the toast, the Undo button in the calendar header, or `z` | One level: the last move or bulk change. It restores the old times only if nothing moved them again since |
| Keep two businesses apart | Brand tabs, the campaign menu and the account chips (calendar, grid, review, ideas) | An account's optional `brand` (`ave studio account add --brand "Loaf Co"`, or `account set <id> brand="Loaf Co"`) and a post's optional `brand` define the tabs. The choice is remembered in the browser |
| See ideas | **Ideas** in the navigation | Posts at stage `idea` with Claude's reason and confidence. **Turn into a draft** writes the idea as the first words (the stage follows from that), **Not now** sets it aside; both undo |

What the planner layer does about approvals: **retiming an approved post keeps its approval** (the schedule is not part of what was signed), records the move in the history, and shows
"Moved after approval" on the card with the reason. A post whose approval no longer matches (a file, the caption, the link or the account changed under it) comes back to review as
"Approval stale"; an approval older than the allowed lifetime shows "Approval expired". **Bulk actions never approve, reject or send anything**, and moving a post to another account is
refused when it already has a review item, because an approval is bound to its account.

API: `GET /api/studio/slots?post=&variant=&count=` lists the next free rule slots; `POST /api/studio/bulk {op, items[], days?, account?}` with `op` one of `shift`, `set`, `slots`,
`unschedule`, `account` answers `{done[], refused[], approved: 0}` per post. Both go through the usual token, Host and Origin checks and the `Studio` facade; neither needs, accepts or
records a PIN. `GET /api/studio/posts?thumbs=1` adds a picture to each variant, and a post's detail carries a `url` and `name` for every file in its review item.

Keys (also in the `?` dialog): `Enter` preview, `o` editor, `m` move, `r` change time, `n` next free slot, `u` unschedule, `x` select, `z` undo, `t` today, `j`/`k`/`n` in Review, `s` send back.

## Quick start

```bash
ave workspace ~/Movies/MyShow                              # once; the studio lives beside your episodes
ave studio init --name "Acme Bakery" --tz America/Denver   # creates <workspace>/studio
ave studio account add ig-main --platform instagram --label @acmebakery --post-types feed,carousel --public ig_user_id=1784...
! ave studio account auth ig-main                          # YOU: hidden prompts, keychain only
ave studio ingest blog essay.md                            # words (a URL is never fetched: save the page as Markdown)
ave studio post new --from-blog essay.md --passages p003 --copy "..." --reason "reads well alone" --confidence 0.8
ave studio render --all --style paper --format 4x5         # quote cards -> library -> post media; LOOK at each PNG
ave studio post variant add <post> --account ig-main --type feed
ave studio queue sync                                      # queue items + every preflight; never approves
ave studio schedule rules set ig-main --rule "id=r1;dow=mon,wed,fri;time=09:00;post_types=feed"
ave studio move <post> <variant> --to 2026-11-02T09:00
! ave studio ui                                            # YOU: review each post as it will go out, approve with the PIN
ave studio tick                                            # rehearsal: posts nothing
! ave studio account live ig-main --on                     # YOU: only when you want real posting
! ave studio library attest --all                          # YOU: stand behind the license claims `library add` / `adopt` recorded
! ave studio tick --send                                   # YOU: one sending pass in your terminal ...
! ave studio send enable && ave studio daemon-plist --send # ... or YOU: allow an unattended sender and print its launchd job
```

Or follow a recipe: `ave studio recipe check blog-to-posts --args file=essay.md` names the next step at every point and stops at each human step.

## The gates

A variant is sent only if ALL of these hold at the moment of sending (each has a test; the first failure blocks it):

1. a human started the sender with `--send` in their own terminal (`tick --send`, `run --send`) OR wrote the signed `studio/send.enabled` (`send enable`) that
   lets a launchd job without a terminal do so; the tick itself also refuses where an agent marker is set, and refuses everything while a sealed ledger fails
   verification (tamper alert);
2. the queue item is a studio item (`meta_v` 2) bound to THIS account, `approved` with a valid HMAC signature for the **current file bytes and the current
   bound metadata** (caption, alt text, account, link, first comment, AI disclosure, post type; the schedule is deliberately not bound, so a sanctioned
   retime does not void an approval), still equal to the post, and the approval is **single-use** (not already consumed in the sealed dispatch ledger);
3. the approval is younger than its TTL (default 14 days, judged on the injected clock), and neither TTL setting was raised since it was given; the slot is
   the one the approval covers (or one a recorded, sanctioned retime set);
4. the account holds a valid, human-attested live file bound to its current platform, mode, time zone and credentials, and its adapter independently agrees;
5. no studio `PAUSE` and no per-account pause (a pause is also in the sealed ledger: deleting the file does not resume);
6. the license closure of every FILE THE QUEUE ITEM SENDS (including what it was derived from) is covered and human-attested;
7. no open rework, retime or drop note on the variant (a human's send-back also voided the approval, so a fresh human approval is needed anyway);
8. the slot has arrived and is not older than `late_after_min` (a stale slot is `missed`, never posted late);
9. limits and quiet hours allow it, it is not a duplicate of a recent post, and no earlier send of it is unresolved.

A crash after the `sending` state is written leaves the variant UNKNOWN: it is never retried; a human checks the platform and runs `ave studio resolve`.

## What only a human can do

Each exits 2 with `needs_human: true` and prints the exact line to run **in your own terminal window** (not through the agent). `ave.humanpresence`
requires stdin and stdout to be a terminal and no agent marker in the environment. That is a speed bump, not a security boundary: a process running as you
can spawn a pty and read the keychain. The real control is the harness deny rules in [../SECURITY.md](../SECURITY.md) and
`.claude/settings.example.json` (they now include every command below).

| Command | Why it is yours |
|---|---|
| `ave studio approve <post> <variant>` / the UI's Review page | Approval of any post; typed hash or PIN after viewing the exact bytes. No auto-approve class exists |
| `ave studio account live <id> --on\|--off` | Enabling real posting is an attested human act (you type the account handle) |
| `ave studio account auth <id>` | Credentials go from a hidden prompt to the keychain, never argv or a file |
| `ave studio resume` | Clearing a pause (any actor may pause) |
| `ave studio tick --send`, `ave studio run --send` | Choosing to let it post; `daemon-plist --send` prints a launchd job that runs `tick --send` |
| `ave studio send enable\|disable` | The standing permission (signed `studio/send.enabled`) that lets an unattended job send; `disable` writes a signed off |
| `ave studio library attest [ASSET ...\|--all]` | Standing behind a license claim that `library add` / `adopt` only recorded; unattested claims do not count toward a post's license closure |
| `ave studio resolve <post> <variant> --posted URL\|--retry` | Settling an uncertain publish after checking the platform |
| `ave studio broll pick <search> <cand> --by NAME` | A named human approves every stock download; it writes the license trail |
| `ave studio broll generate ...` | A spend; not enabled in this build |
| `ave studio schedule apply <proposal>` | Accepting an agent's proposed times |
| `ave studio ui` | The server that can approve (prints a per-run PIN to your terminal only) |

Also yours, with no command: creating developer apps, buckets and keys, OAuth consent, platform review submissions, reading each provider's and platform's
terms before relying on `verified: true`, deciding the AI-disclosure policy, and the final say on taste.

## Commands

JSON on stdout, human text on stderr, exit 0 done, 2 blocked or needs a human. `--studio DIR` on any command (default `<workspace>/studio`).

| Group | Commands |
|---|---|
| Project | `init`, `demo [DIR]`, `status`, `doctor`, `adopt <episode>`, `daemon-plist [--send]`, `ui` |
| Accounts | `account add\|list\|show\|set\|remove\|auth\|live\|refresh`, `pause [--account]`, `resume` |
| Library | `library add\|attest\|list\|show\|search\|tag\|scan\|reindex\|verify` |
| Sender permission | `send enable\|disable\|status` |
| B-roll | `broll queries\|search\|show\|pick\|generate` |
| Posts | `post new\|list\|show\|edit\|attach\|swap-asset\|variant add\|drop\|critique` (`critique` is an addition: the voice and length critic) |
| Render | `render <post> [--template --style --format --slots --source]`, `render --all`, `deliverables <post> [--apply]` |
| Schedule | `schedule show\|rules set\|rules show\|plan\|apply`, `move`, `unschedule` |
| Queue and review | `queue sync\|list\|dry-run`, `review list`, `approve` |
| Run | `tick [--send] [--now]`, `run [--send] [--interval]`, `resolve`, `alerts [ack ID]` |
| Feedback | `feedback list\|next [--claim --agent]\|resolve\|stats` |
| Campaigns | `campaign new\|show\|list\|add-post` |
| Ingest and recipes | `ingest idea\|blog\|audio\|video\|episode\|shorts`, `recipe list\|show\|check` |

Recipes (config/studio/recipes/, validated by `ave studio recipe show`): `blog-to-posts`, `audio-to-clips`, `video-to-shorts`, `episode-shorts-to-posts`,
`episode-to-campaign`, `idea-to-visuals`, `quote-drop`, `product-spotlight`. A studio may override or add its own in `<studio>/recipes/`. The validator keeps every human
act (approve, `account live`, `library attest`, `send enable`, `ui`, ...) out of a mechanical step.

### Short clips to posts (`ave studio ingest shorts`)

The creator's loop for short video: source licensed B-roll (`broll search` / human `broll pick`), edit short clips, move them through review into the posting queue.

```bash
ave broll place <episode> --stage short --name <slug> --asset a_... --at 4 --dur 2 [--apply]      # B-roll over a short: proposal first, then a new edit version
ave shorts recut <episode> --clip <slug> --drop-start-words 7 --dry-run                          # tighten on word boundaries (the complete-statement gate runs)
ave shorts retitle <episode> --clip <slug> --title "Why finishing is hard"
ave shorts export <episode> --clip <slug> --platforms reels,youtube,facebook,x
ave studio ingest shorts <episode> --dry-run                                                      # then without --dry-run; --clips ids, --accounts ids, --sync
! ave studio library attest --all                                                                 # YOU: stand behind the footage's license claims
ave studio queue sync                                                                             # (or --sync above) queue items; never approves
! ave studio ui                                                                                   # YOU: watch the exact file, approve
```

- **Human-kept clips only.** A clip counts when `clips.json` says `approved` AND the learning ledger's latest decision for it is `keep`, recorded by a named person from a terminal or the UI
  (`via: tty|ui`). A keep typed through an agent shell (`via: notty`), a later skip, a proposed clip and a status with no decision on record are refused, with the line for the human. Naming such
  a clip in `--clips` refuses the whole run and creates nothing. (A human's browser decisions that an agent imported with `ave clips import-decisions` are `notty` too: confirm them in `ave ui`.)
- **Only finished renders.** Latest cut only (a newer cut with no export is refused), not a draft, bytes equal to the sidecar's sha256, limits, encode and licenses ok, a complete statement without
  the `--allow-incomplete` override. `needs_review` is advisory (as in `ave publish queue --from-clips`): it is carried into the post's reason and the result's `warnings`.
- **A post per clip, a variant per account** with that platform's export and post type (Instagram/Facebook `reel`, YouTube `short`, TikTok/X `video`; TikTok posts the Reels encode); accounts with no
  vertical-video format or post type are listed under `skipped`. Idea = the title (`work/shorts/titles.json`, then the opening's title, then the clip's). Caption = a verbatim quote from the clip (the
  hook when it is said, else a sentence), the speaker when one person speaks, the title as YouTube's first line, required credits, within the platform limit and through the package voice critic;
  hashtags per `max_hashtags` (stored without `#`, in the variant's `hashtags`). Idempotent per clip and short version.
- **Library and licenses.** The render is registered `derived_from` the episode's source files the short uses (copied by sha as UNATTESTED `own` claims, like `adopt`: a multi-hour recorder is a real copy),
  music/other files from the episode's license log, and every placed library B-roll. Until a human runs `library attest`, the closure is not covered and `queue sync` creates no queue item.
- Never approves, schedules or sends. `--sync` only runs `queue sync` (queue items in `awaiting_approval`).

## The final post text (caption, link, credits, hashtags)

`queue sync` composes ONE final text per variant with `src/ave/studio/posttext.py` and writes it into the queue item's `caption` and `description`
(and `first_comment`), so the text the approval hash binds, the text the reviewer reads (Review screen, `queue list`, the preview) and the text the
adapter sends are the same string. The adapters' own `compose_text` then adds nothing (the link is already in). Order: caption, link, the credit
lines of licenses that *require* attribution, hashtags. Conventions live in `config/platforms.yaml` `<platform>.text` (all unverified):

| Platform | Hashtags | Length unit (checked on the composed text, 2 % margin) |
|---|---|---|
| Instagram, TikTok, Facebook, LinkedIn, Pinterest, Manual | block after a blank line (Instagram max 30) | chars |
| Threads | block, or first comment | chars |
| X | inline, after the caption | X weighted (URL 23, emoji and CJK 2) |
| Bluesky | inline (the adapter turns each `#tag` into a facet; a tag starting with a digit is refused) | graphemes |
| YouTube | block at the end of the description (max 15) | description bytes |

`hashtags_in_first_comment: true` (Instagram, Threads only) moves the tags out of the caption to the end of the first comment. A tag already
written in the caption is not repeated; tags are stored without `#`, spaces and punctuation removed. Text that does not fit (too long, too many tags,
an unusable tag) is **refused, never cut**: the variant is `blocked` with the reason and keeps whatever item it had. Changing hashtags changes the
bound caption, so an approved item is demoted to `awaiting_approval` like any copy edit. The editor's live character counter in the browser still
counts the caption alone; the Review preview and the server preflight count the composed text. Not verified: every convention above against a live post.

## Where things live

```
<workspace>/studio/
  studio.json  accounts/<id>.yaml (+ .live.json, .paused)  posts/<id>.json  campaigns/<id>.json
  library/assets.jsonl, licenses.jsonl, assets/<sha[:2]>/<sha>.<ext>, index.sqlite (derived)
  schedule/<account>.yaml, proposals/, state/, heartbeat.json
  ledger/events.jsonl, feedback.jsonl, alerts.jsonl      publish/queue/<id>.json (the existing queue format)
  work/ (renders, writing, broll searches)  exports/  inbox/ (drop files here; read-only)  policy/  recipes/  PAUSE
```

Secrets are never in these files: the keychain holds them (`ave-publish` service, names like `instagram_token@ig-main`), and `ave studio doctor` fails if a
token-shaped value appears in the studio or `~/.config/ave`.

## Known limits (found while integrating; honest list)

- **Edits and the queue.** A copy or media edit made in the UI re-syncs the queue item immediately and drops an approval whose bytes or bound copy changed
  (the editor says so). An edit made with `ave studio post edit` reaches the queue at the next `ave studio queue sync`; until then the approved item still
  holds the old copy, and the old copy is what would post (the approved bytes are the posted bytes).
- **Text cut in the Instagram grid** is not computed on the server (`text_cut` is always false): it needs the renderer's measured rectangles, which are not stored
  per asset. The preview still draws the crop and the percentage of the image lost.
- **The accounts page reads the keychain** to say which credentials are present (names only, never values). On macOS the first read can prompt.
- **Token expiry** is not recorded anywhere (`token_expires` is null); the `auth_expiring` alert exists but nothing raises it yet.
- **`approve` in the CLI** shows the exact files and hash and takes the typed prefix; it does not check that you opened them. The UI path requires a recorded view of the exact sha.
- **Adopted episode items** are registered in the library as UNATTESTED `own` claims and become posts WITHOUT a queue item: the episode's approval does not
  transfer (no account binding). `library attest`, `queue sync`, then a fresh approval. The episode's queue item is stamped so `ave publish run` refuses it. Their
  inputs (music, B-roll) keep their license trail in the episode's own license log, which `ave final` checks.
- **The Review screen is drawn from the queue item.** What an approval signs is the queue item, so the sheet shows the item's own caption, first comment, link, hashtags, alt texts and the sha256 of every file, serves the item's own picture, and builds the platform mock from the item's words. When the item differs from the post a red banner lists each field (item against post) and the post cannot be ticked; the server refuses it too. Fixing it is `ave studio queue sync` (an agent may run it; it never approves), then you look again.
  list with `view` and refuses to approve unless the item equals the post, so what you reviewed is what is signed. Showing the bound fields in the screen is UI work.
- **Media-host objects** are deleted in `finally` (retried once); a killed process leaves an object that the next sending tick's sweep (at most every 15 minutes)
  removes; presigned URLs live 15 minutes and are masked everywhere they could be persisted.
- **A machine that is off or asleep at the slot** posts nothing; the variant becomes `missed` and is never posted late. Run the sender on an always-on Mac (`ave studio daemon-plist`).

- **Approvals and live files made by an earlier build** lack the signed binding / sequence number and the account-config hash: the tick refuses them
  ("approve again", "attest again"). Re-approve and re-attest once.
- **Still plain data (documented, not closed by the sealed ledgers):** `schedule/state/<account>.json` (the per-day count and minimum gap: deleting it resets the
  limits), a variant's `allow_duplicate` flag, and an account YAML's `quiet_hours` and `limits` (policy, not identity: they are not in the live attestation). An agent
  can loosen these by hand; the harness deny rules cover the files. `ave studio ui --open` puts the token URL in a browser child's argv for a moment; `/files/` checks
  the served prefix on the lexical path; the approver name `by` in the UI is free text; uploads are not bounded in number. None of these lets a post go out without an approval.
- **Shorts bridge.** Hashtags now reach the post (see "The final post text"). Source footage and music are copied into
  the library to make the license closure real: for a long recording that is a full copy (`library/assets/`), and the `own` claims must be attested by the human. A hand-made `clips.json` (no ledger) is refused on purpose.
- **B-roll placement** is not yet reflected in Resolve beyond the clip at its in-point (see the table), the on-screen credit is a PNG overlay rendered with Pillow (the brand font if present), and an insert drops the
  edit's captions (regenerate them). Opening/closing sequences are not shifted by an insert.
- **Pause is detectable, not preventable.** Deleting `studio/PAUSE` does not resume (the sealed ledger still says paused, the tick alerts and stays paused), but a
  process that can write the ledger with the key can resume; only the human's `resume` is the sanctioned path.

## Burn-in checklist (a human, one platform at a time: Bluesky, Facebook Page, Threads, Instagram)

1. Create a throwaway account and the developer app or app password; keep review submissions (Meta, TikTok, LinkedIn) for later.
2. `ave studio account add ...`, then `! ave studio account auth <id>` (and, for Instagram or Threads, configure the media host: `~/.config/ave/host.yaml` plus keychain keys).
3. Make one text+image post, `queue sync`, approve it, `! ave studio account live <id> --on`, `! ave studio tick --send` at its slot.
4. If it posted, compare a screenshot of the real post to the studio's preview and file corrections in `config/previews.yaml`; then flip that adapter's `verified: true`
   in `config/platforms.yaml` with the date and the source. If it failed or came back uncertain, `ave studio resolve` and record what you saw in `docs/field-notes/`.
