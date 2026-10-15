# Asset licensing

The toolkit makes the legal trail automatic; it cannot make a license valid. Anything marked **UNVERIFIED** below is from memory of public
pages and research notes, taken 2026-10-13, and must be re-read at the source before you rely on it. The terms snapshots stored in each license-log
entry (`licenses.PROVIDER_TERMS`) are likewise UNVERIFIED paraphrases.

## What the log records

`<episode>/work/assets/licenses.jsonl`, one JSON object per line, append-only (file lock + fsync), hash-chained (`prev` = SHA-256 of the previous
entry) so edits or mid-file deletions show up in `ave assets license-check` (deleting only the newest line cannot be detected; commit the file).
An entry is refused unless it has: source, url, file, sha256, license, license_url, license_text, approved_by. It also carries time (UTC), query,
author, restrictions and attribution text. API keys are redacted from anything stored. `license-check` matches by file SHA-256, so a swapped or
re-encoded file counts as unlicensed until re-logged.

## Stock video providers (API)

| | Pexels | Pixabay |
|---|---|---|
| License (UNVERIFIED) | Pexels License, commercial use, modification allowed | Pixabay Content License, commercial use, modification allowed |
| Attribution | not required, appreciated; we record a credit line | not required; we record a credit line |
| API rules we follow (UNVERIFIED) | key in `Authorization` header; ~200 req/h; credit Pexels/creator where the API is used | key as query param (never stored); cache results 24 h; download, never hotlink |
| Common restrictions | no selling unaltered copies; no implying endorsement; sensitive depiction of identifiable people; visible trademarks not cleared | same family of limits |
| Releases | **not guaranteed**: recognizable faces/logos are the creator's risk; our critic hard-gates them | same |

Env vars: `PEXELS_API_KEY`, `PIXABAY_API_KEY`. Cache: `~/.cache/ave/assets` (override `AVE_ASSETS_CACHE`).

## Music: sources and warnings

We analyse and rank tracks the human already has rights to; we do **not** download from music services. Record each track with
`ave assets log-license <ep> --file ... --source <service> --license "<plan name>" --license-text "<terms or invoice ref>" --url <track page> --approved-by <you>`.

All of the following are **UNVERIFIED** warnings from research, to confirm before publishing:

- **Content ID / copyright claims.** Even correctly licensed library music can trigger automated claims on YouTube/Facebook, because the same track sits in
  other uploaders' Content ID registrations. Keep the invoice/license page and the track URL; services usually offer a claim-whitelisting channel step.
  Never register your own episode audio with Content ID if it contains third-party music.
- **Epidemic Sound scope.** Plans differ on which channels, platforms and *podcast* uses are covered, and whether a video podcast distributed to
  several platforms needs a commercial tier. Check the current plan terms for your exact distribution (YouTube, Facebook, audio feeds).
- **Subscription lapses.** Some services license only while subscribed or only for channels you registered; log the plan and date.
- **AI-generated music (Suno, Udio, etc.).** Commercial rights usually depend on the paid tier at the time of generation; ownership and copyright
  registrability are legally unsettled; some services restrict Content ID registration or require disclosure. Log the plan, generation date and prompt.
- **Creative Commons tracks.** CC BY needs the exact credit line (we store it); NC licenses are unsuitable for monetized shows; ND forbids edits such as
  fades and trims; share-alike can bind the whole video. Record the license URL.
- **Pixabay/YouTube Audio Library** tracks each carry their own terms (the YouTube library has per-track attribution requirements). Log per track.
- **Live performance or cover versions** need mechanical/sync licenses; the toolkit does not cover them.

## Stings and beds in the opening and closing

`ave sequence plan` treats a sting or bed named in `config/sequences.default.yaml` (or a per-show override) like any other music: the file must have a license-log entry for
exactly its bytes, or the plan errors and `ave sequence build` / `ave final` refuse. The credits roll lists every logged asset the cut and its sequences use (the logged exact
credit line when there is one, else title, author and license), and warns when a license requires attribution but the log has no credit line. Modifying a track (fades, a trim, a duck)
is part of how a sequence uses it: do not use ND-licensed tracks.

## Own footage

Record it too (`--source own-footage --license "Owned" --url file://own --license-text "Shot by <name> on <date>"`) so that `license-check`
passes and releases for any identifiable people or property are noted (`--notes "release on file: ..."`).
