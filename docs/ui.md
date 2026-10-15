# `ave ui`: the local management interface

```
ave ui <episode> [--port 0] [--open] [--by NAME]
```

A small web UI over the same files and CLI the agents use. It is a **human surface**: the human runs it, the token is printed on
their terminal, and agents never open it. Everything it does is also possible from the CLI.

The look is a paper edit for a podcast: the transcript is marked with a highlighter (the clip) and struck with a red pencil (the
cuts), the scrubber is a real waveform with the clip's in and out ticked on it. Light and dark follow the system (a button in the
bar overrides it and is remembered in the browser). System fonts only, no CDN, no network beyond localhost. Keyboard focus is
always visible, motion is off under `prefers-reduced-motion`, and every text and background pair is at least 4.5:1.

## Safety model

- Binds `127.0.0.1` only. Port `0` picks a free random port.
- A random per-run token (`secrets.token_urlsafe(24)`) is required on **every** request, static pages and files included. It arrives
  as `?t=` (the URL printed on the terminal), then moves into an `HttpOnly; SameSite=Strict` cookie and the URL is redirected clean.
  `X-AVE-Token` also works (curl).
- The `Host` header must be `127.0.0.1:<port>` or `localhost:<port>` (defeats DNS rebinding). A POST with a foreign `Origin` is refused.
- No CORS headers, no CDN, no external network. Stdlib `http.server` and vanilla JS/CSS: no new dependency.
- `/files/<path>` serves only files inside the episode folder: no `..`, no symlinks that leave it, no dot-files, and never the raw
  footage dirs. Ranged requests (`Range: bytes=`) are supported so the video can seek. Previews are served this way too.
- Jobs are an allowlist of `ave` subcommands with typed arguments (`src/ave/ui/jobs.py`). No shell, no free-form strings, no file-path
  arguments, no `--allow-unapproved`/`--allow-unlicensed`, no env overrides, no publish/approve/send. One job at a time.
  `AVE_ALLOW_UNATTENDED_APPROVAL` is stripped from job environments.

## Pages

| Page | Shows | Writes through |
|---|---|---|
| Dashboard | the pipeline as a status board (done, next, waiting on a step, optional, where you sign off), what waits on you, clip tallies per session, one-click safe jobs, last log lines | read only (`ave.state`, `ave.progress`); jobs below |
| Clips | the review desk (below) | `ave.clips.decide` (clips.json status) then the ledger |
| Trim | `trim_suggestions.json`, recorded trims | `ave.trim.set_trim` / `clear_trim` |
| Content | drafts as a wall of images at their real shape, critic results, alt text, approve/reject | `ave.content.review.decide` (nudging stays CLI) |
| Publish | queue; the exact file, its sha256 with the 8 characters you must type highlighted, the metadata bound to the approval | `ave.publish.queue.approve(..., source="ui")` / `reject` |
| Jobs | suggested jobs, recent jobs, a custom runner, a live log (`#/jobs/<id>` follows one) | subprocess `ave ...` |

### Clips: the review desk

One screen to decide a clip **in context**. Left, the queue (score, title, session and time, length, decision mark, a square that is
filled when a preview exists). Middle, the player. Right, the transcript.

- **Player.** The clip plays inside its preview (see "Previews"): lead-in and tail are dimmed on the video and on the strip, the
  strip draws the audio waveform with the clip's `in` and `out` ticked, and a pencil-hatched band marks every tighten suggestion.
  Controls: play/pause, scrub on the strip, jump -5/+5 s, speed 1x / 1.25x / 1.5x, **loop clip** (wraps at the clip's out), and
  **Original / Tightened** when the preview index has a `tight_file`. Playback starts 3 s before the clip so you hear the lead-in.
  Without a preview it falls back to ranged playback of the per-clip draft or of the edit's proxy render (`exports/<edit>.proxy.mp4`,
  dimmed outside the clip range), and with neither it says "Preview not rendered yet" and offers buttons that queue
  `ave clips previews` as a job (this clip, or the session's best 20) and watch it finish.
- **Transcript.** Sentences from `clips_candidates.<session>.json` (the same sentence table and time base as the clip), with the
  sentences just before and after dimmed, the clip's range highlighted, the line being played emphasised and followed, and a click on any
  line to play from it. If the sentence table is missing it shows `context_before` / the clip text / `context_after` from `clips.json`.
- **Tighten.** Each suggestion (`filler`, `tangent`, `repeat`, `restart`, `aside`) is a chip. Turning one on strikes it from the
  transcript, hatches it on the strip, makes playback skip it, and shows the seconds saved and the new length. Nothing is applied
  to the clip: suggestions only. (`c` toggles all.) Tighten times are episode seconds; the API maps them to clip time through the
  sentence table's `e0`.
- **Completeness badges.** "Starts mid-sentence" / "Ends unfinished" from `start_complete` / `end_complete` (or, when workstream K's
  fields are absent, derived from the sentence features), plus `completeness_note` and the clip's own warnings.
- **Controls for how many to review.** A session switcher, a "Best N" stepper (empty = all), a minimum-score slider, a tag filter, an
  order (score, episode order, length, rank), and "Not decided yet". The count reads live: "Showing 23 of 99". Filters are remembered
  in the browser. A progress meter shows kept / maybe / skipped over the current selection, and Undo steps back through your decisions.
- **Keys.** `k` keep, `m` maybe, `s` skip, `z` undo, up/down previous/next, `space` play/pause, left/right back/forward 5 s, `l` loop,
  `,` `.` slower/faster, `1`-`8` toggle a reason, `[` `]` start earlier / end later by a sentence, `{` `}` start later / end earlier,
  `c` cut all/none, `enter` next. Keep advances at once; maybe and skip stay put so you can add reason chips and a note, then `enter`.
- **Nudge** moves a boundary by whole sentences through `ave.clips.decide` (re-snaps and re-scores). The existing preview then shows
  the old range and says so; re-render the clip to refresh it.

`keep` -> `approved`, `skip` -> `rejected`, `maybe` stays `proposed` with `maybe: true`. Undo of a first decision sets the clip back to
`proposed` (`verdict: "pending"`; no ledger line). Each keep/maybe/skip appends one line to `<work>/learning/clip_decisions.jsonl`
(schema v1 in the design spec) with `source: "ui"`; changing reasons or the note later appends another line (latest wins).
`src/ave/ui/ledger.py` uses `ave.learning` when it exists and otherwise a minimal local writer of the same schema. Reasons and their
labels come from `config/clip_reasons.yaml`.

### Previews (contract with `ave clips previews`)

`work/clips/previews/<clip_id>.mp4` plus `work/clips/previews/index.json`:
`{clip_id: {file, tight_file|null, pad_before_s, pad_after_s, clip_in_s, clip_out_s, edit, rendered_at, sha}}`. `clip_in_s` / `clip_out_s`
are offsets inside the preview where the real clip begins and ends. The UI treats `tight_file` as the tightened clip on its own (no
padding). Files named by the index are served only if they live under the episode folder and pass the same checks as `/files/`; a
broken or hostile index is ignored. A preview whose recorded clip length no longer matches the clip (after a nudge) is flagged stale.

### API (all behind the token)

`GET /api/clips?session=&tag=&q=&top=&min_score=&status=&unreviewed=1&sort=score|time|duration|rank` returns
`{clips, of, shown, selected, progress{done,total,keep,maybe,skip}, score_range, sessions, tags, reasons, reason_labels, previews_rendered}`.
Order of operations: scope (`session`, `tag`, `q`) gives `of`; `min_score`, then the best `top` by score, give the selection;
`status` / `unreviewed` narrow it for display (`shown`). Bad values are 400. `GET /api/clips/<id>` returns the full clip, the transcript
window, tighten ranges in clip time, completeness, and `media: {preview, fallback, state}`.
`POST /api/clips/<id>/decide` takes `verdict` = `keep|maybe|skip|pending|nudge` (`nudge` needs `nudge: {start?, end?}` whole sentences,
-6 to 6), `reasons`, `note`.

### Publish approval

Approve needs the first 8+ characters of the file sha256 (typed by the human, compared with the file on disk now) and an approver name.
It is the same `queue.approve` as the CLI: preflight runs, the signature binds file hash and metadata, and the approval records
`source: "ui"`. **The UI never sends.** There is no send endpoint; sending stays `ave publish run <ep> <id> --yes` in the terminal.

## Files

`src/ave/ui/{server,api,jobs,ledger}.py`, `src/ave/ui/static/{index.html,app.js,style.css}`, `src/ave/commands/ui.py`, tests in
`tests/test_ui.py`. Screenshots, captured from the synthetic `ave demo` episode, are in `docs/images/ui-*.png`.
