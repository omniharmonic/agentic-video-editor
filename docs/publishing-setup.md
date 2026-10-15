# Publishing setup (one-time, manual)

`ave publish` posts to real accounts, so every credential step below is done by the account owner, in a terminal or
browser, once. Credentials live only in the OS keychain (service `ave-publish`) or in `AVE_*` environment variables.
They are never written to the repo, to queue files, or to logs (output is redacted). Everything marked UNVERIFIED was
written from documentation and research without a live account; check it when you do the setup.

## Safety model in one paragraph

`queue` hashes the video (sha256) and runs preflight. `approve` is a human act: it prints the manifest and requires the
first 8+ characters of the file hash, then stores `{by, at, sha256, meta_sha256, sig}`. The signature is an HMAC with a
key in the keychain (or `AVE_APPROVAL_KEY`, or as a last resort `~/.config/ave/approval.key`, mode 0600), so a hand-edited
queue file cannot grant itself approval. `run` is a dry run unless `--yes` is passed, and `--dry-run` always wins. A
send requires status approved, a valid signature, the current file hash equal to the approved hash, and unchanged
metadata. Each platform's state is written as `sending` before the network call, so a crash is never mistaken for "not
sent"; such items are retried only if the platform proves nothing was posted (YouTube uploads-playlist lookup) or the
human settles it with `ave publish resolve`.

Commands: `ave publish queue|list|show|approve|reject|run|resolve|auth|status` (episode path first, then the item id).

## YouTube (direct)

1. Google Cloud console: create a project, enable **YouTube Data API v3**.
2. OAuth consent screen: set user type External, add yourself as a test user, then **publish the app to Production**
   (in Testing mode refresh tokens expire after 7 days).
3. Credentials: create an **OAuth client ID of type Desktop app**; download the JSON.
4. `ave publish auth youtube --client-secrets ~/Downloads/client_secret_....json` opens a browser, you approve the
   `youtube.upload` (and read-only, used only for crash recovery lookups) scopes, and the refresh token goes to the keychain.
   Delete the downloaded JSON afterwards; it is not needed again.
5. **Audit (UNVERIFIED but widely reported):** videos uploaded through an API project that has not passed the YouTube API
   Services audit are locked to private. ave surfaces this as "UPLOADED BUT FORCED PRIVATE". To lift it, submit the
   audit/compliance form for your project. Until then you can flip visibility in YouTube Studio by hand.
6. Quota: default 10,000 units/day, an upload costs about 1,600 (UNVERIFIED; see `config/platforms.yaml`).
7. Per-post fields ave always sets: `selfDeclaredMadeForKids` (default false; `--made-for-kids` to flip) and the AI
   disclosure (`containsSyntheticMedia`, required via `--ai-disclosure yes|no`). Scheduling uses `publishAt` and forces private until then.

## Facebook (direct, Page only)

UNVERIFIED end to end: the flow follows Meta's docs but was not run against a live Page.

1. Meta for Developers: create an app (Business type). Add the Pages API product.
2. Business Verification of the owning Business, then permissions `pages_manage_posts`, `pages_read_engagement`,
   `publish_video` need **App Review** (screencast + justification) before they work for anyone but app admins/testers.
   While the app is in development mode and you are an admin, posting to your own Page works without review.
3. Generate a long-lived **Page access token** for the Page (Graph API Explorer -> user token -> exchange -> `/me/accounts`).
4. `ave publish auth facebook --page-id <PAGE_ID>` then paste the token at the hidden prompt. Or set `AVE_FACEBOOK_PAGE_TOKEN`
   and `AVE_FACEBOOK_PAGE_ID` for a session.
5. Behaviour: Reels use `/{page}/video_reels` start -> upload -> finish; longer videos use chunked `/{page}/videos`.
   A post goes live only for `--visibility public` or when `--schedule` is set (10 min to 75 days ahead, UNVERIFIED);
   otherwise it is uploaded as an unpublished draft.

## Instagram, X, TikTok (not implemented directly)

There is no direct Instagram, X or TikTok adapter, deliberately:

- Instagram publishing needs a public video URL, a Business account linked to a Page, and Meta app review.
- X media/post API is pay-per-use (buy developer credits first).
- TikTok's Content Posting API requires an audit before posts can be public.

Instead, `instagram`, `x` and `tiktok` route through the **experimental aggregator adapter**, a generic HTTP client driven by
`~/.config/ave/aggregator.yaml` (start from `config/aggregator.example.yaml`; override the path with `AVE_AGGREGATOR_CONFIG`).
Candidate hosted services to evaluate (no endpoint is hard-coded; check each vendor's API docs and terms): Ayrshare,
Upload-Post, Late. Store the key with `ave publish auth aggregator`. Aggregator posts cannot be held back, so preflight demands
`--visibility public` unless your service has a draft mode and you set `supports_visibility: true`.

## Limits and preflight

`config/platforms.yaml` holds duration/size/aspect/schedule limits, each block marked `verified: true|false` with a source
URL. All are currently `verified: false`. Preflight blocks obviously invalid posts (16:9 file for Reels/Shorts,
over-length, Reel under 540x960, missing AI disclosure, schedule in the past). Edit the YAML when a platform changes a limit.
