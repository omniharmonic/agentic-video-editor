# Security policy

## Reporting a vulnerability

Please do not open a public issue for a security problem. Report it privately to **GitHub private vulnerability reporting** (the repository's Security tab, then "Report a vulnerability") with:

- what you found and where (file, command, or UI route),
- steps or a small script to reproduce it,
- what an attacker gains (for example: posting without approval, reading files outside an episode folder, leaking a token).

Expect an acknowledgement within a few days. This is a small volunteer-run project; there is no bug bounty. Please give us a
reasonable window to fix the problem before you publish details.

Supported versions: only the latest release (and `main`) receive fixes.

## What is in scope

`ave` runs on your own machine against your own footage and, optionally, your own social accounts. The parts that matter for
security are the ones that touch credentials, the network, or the filesystem outside the repo.

### Publishing credentials

- Platform credentials (YouTube OAuth refresh token, Facebook Page token, an aggregator API key) live **only** in the OS keychain
  (service `ave-publish`) or in `AVE_*` environment variables. They are never written to the repo, to queue files, or to logs;
  output is passed through a redactor (`src/ave/publish/safety.py`).
- Stock-media API keys (`PEXELS_API_KEY`, `PIXABAY_API_KEY`) are read from the environment and redacted from anything stored in the
  license log.
- Approval to publish is a human act. `ave publish approve` requires an interactive terminal and the first 8+ characters of the
  file's sha256. The approval record carries an HMAC signature made with a key from the keychain, `AVE_APPROVAL_KEY`, or as a last
  resort `~/.config/ave/approval.key` (mode 0600), so a hand-edited queue file cannot grant itself approval. A send requires status
  `approved`, a valid signature, the current file hash equal to the approved hash, and unchanged metadata. `ave publish run` is a dry
  run unless `--yes` is passed.
- `AVE_ALLOW_UNATTENDED_APPROVAL` exists for test automation. It must never be set in a real session; the local UI strips it from the
  environment of jobs it starts.
- Full description and the per-platform setup: [docs/publishing-setup.md](docs/publishing-setup.md).

### Local web UI (`ave ui`)

- Binds to `127.0.0.1` only. A random per-run token (`secrets.token_urlsafe(24)`) is required on every request, static files
  included. It arrives once as `?t=` in the URL printed on your terminal, then moves into an `HttpOnly; SameSite=Strict` cookie.
  `X-AVE-Token` also works for curl.
- The `Host` header must be `127.0.0.1:<port>` or `localhost:<port>` (DNS-rebinding defence); a POST with a foreign `Origin` is
  refused; no CORS headers are sent.
- Files are served only from inside the episode folder (no `..`, no symlinks that leave it, no dot-files, never the raw footage
  directories).
- The UI can run only an allowlist of `ave` subcommands with typed arguments, with no shell, no free-form paths, and no
  publish/approve/send. Approving a publish item in the UI records the human's approval of the exact bytes (hash-bound); it never sends.
- Treat the printed URL like a password for the duration of the run. Do not paste it into chats or issue reports.

### Other things worth reporting

- Anything that lets an agent or a web page publish, spend money, or call a paid API without the explicit human approval step.
- Writing to or deleting an episode's raw footage folders (the tool is designed never to).
- Path traversal in any command that takes an episode, edit, or asset name.
- Credentials, tokens, or approval keys showing up in logs, queue files, manifests, or generated packages.

### Out of scope

- Vulnerabilities in third-party software we call (FFmpeg, DaVinci Resolve, Chromium, whisper.cpp, FluidAudio, or the Resolve MCP
  server). Report those upstream.
- The behaviour of a social platform's API, or your account being limited by that platform's own review process.
- Attacks that require an attacker who already has your user account on the machine.

## If you think you leaked a credential

Revoke it at the provider first (Google Cloud console, Meta developer portal, the aggregator's dashboard), then delete the stored
entry (`ave publish auth <platform>` replaces it; the keychain item service is `ave-publish`), and rotate `approval.key` if it may
have been exposed.

## For contributors

Never put real tokens, client-secret JSON files, real footage, or real transcripts in tests, fixtures, docs, or issues. Tests that
need a credential-shaped string must use an obviously fake value and must assert that the redactor removes it.
