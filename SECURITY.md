# Security policy

## Reporting a vulnerability

Please do not open a public issue for a security problem. Report it privately to **[INSERT SECURITY CONTACT: private
vulnerability reporting on the hosting platform, or an email address]** with:

- what you found and where (file, command, or UI route),
- steps or a small script to reproduce it,
- what an attacker gains (for example: posting without approval, reading files outside an episode folder, leaking a token).

Expect an acknowledgement within a few days. This is a small volunteer-run project; there is no bug bounty. Please give us a
reasonable window to fix the problem before you publish details.

Supported versions: only the latest release (and `main`) receive fixes.

## What is in scope

### Desktop, external agents and collaboration

The optional native desktop shell reuses the loopback server's token, Host/Origin checks, media review and PIN gates. It exposes no JavaScript/Python bridge and starts only after the existing human-terminal check. The Finder launcher opens a real Terminal and retains the PIN there; it never removes agent markers or creates a pseudoterminal. Harness deny rules should cover `ave desktop*` and installation-specific executable/module spellings in addition to the existing UI commands. Fictional `--fixtures` engineering runs cannot approve real posts.

**App approval sheets and protected acts (Director's Room).** The app's web view can only *name* what a person wants to do; the native side does the rest (`ave/desktop_approvals.py`, `ave/desktop_native.py`). The native actions are `approvePublish` (`{items: [{post, variant, sha}]}`, 1 to 50), `approveTrim` (`{project, session}`), `approveContent` (`{project, doc, id, note?}`), `clearTrim` (`{project, session}`), `ackAlert` (`{alert}`), `pickBroll` (`{search, candidate, by}`) and `acceptProposal` (`{proposal}`); any other argument (a PIN, an approver name, a view time) is refused, and an agent marker refuses every step. Each one shows the exact words again in an AppKit sheet, read from the server, then runs the macOS device-owner check (`desktop_auth.NativeAuthority`). That check grants nothing session-wide: it mints one single-use grant bound to that act and exactly those targets, spent once by the native code on its own thread (`authority.use`). No web route approves anything: the old browser `POST /api/studio/approve` (PIN only), `reject`, `resume`, `tick/rehearse`, the view record and `PUT library/upload` were removed with the old studio shell, and a test reads every route string from the server's own dispatchers, calls each one with the room token in a browser and in the app without a grant, and sees no approval move: studio posts, content items, the episode's trim, the episode publish queue, the opening and closing approval and settled sends. Approving a post, recording the view and confirming a trim run in process, inside the grant. The remaining protected studio routes (`broll/pick`, `schedule/proposals/<id>/accept`, `alerts/<id>/ack`) pass only inside that spend, so a caller holding the room token is refused even while the person is reading the PIN sheet, and a grant for one post or act never authorises another. Approvals and trims also take the per-run PIN, shown only in AppKit. The studio API no longer reports whether a check is in progress. `alerts/<id>/ack` records a person only through the app's sheet; without the app it is refused, and the terminal's `ave studio alerts ack` keeps recording who ran it. A view recorded by the approval sheet carries `detail.via: "native-sheet"`, set by the native code, never taken from a request: it is how long the native sheet showed the exact words and hashes, not a measure of watching the media. Every check names its act (`require(action, keys)`); there is no session-wide unlock and no "Unlock Review" menu item. A batch sheet with more than three posts lists one line per post (at most twelve, then a count), because a native alert does not scroll.

**Attribution follows the channel.** Claude's MCP holds the room token, so a request that carries only the token cannot show it is the person. Every board write over HTTP (a field, a stage move, an update, a new artifact, a decision answer or reopen, an undo, a keep / maybe / skip or a select nudge) is recorded as `actor: "claude"`, and a sign-off or an answer carries `relayed_by: "claude"`, so the director confirms or undoes it on the board like anything Claude relays through the MCP tools (a keep over the token is never a human-kept clip for the shorts path). The person's own writes go through the app's native bridge (`aveNative.boardWrite` → `desktop_approvals` → `via="app"`), the only way to `actor: "human"`; confirming what Claude reported (`confirmRelayed`) and an answer to a settings conflict (`answerDecision`) also take the per-act device-owner check, as do handing over publishing and removing a member. The route test above also asserts that no room-token request writes an `actor: "human"` event (only the Room's presence heartbeat is exempt). A plain browser (`ave studio ui`) has no bridge, so its hand edits read as relayed.

The stdio MCP server fixes its studio at process launch. It exposes explicit projected reads and draft-only writes, rejects unknown fields, checks revisions and provides no shell, arbitrary path, publishing, approval or account tool. Project text remains untrusted. The same-user shell limitation below still applies; MCP is a narrow interface, not isolation from an agent that already has full local filesystem access.

Collaboration bundles carry editorial content and optional SHA-verified media. Checksums detect corruption, not sender identity: exchange them through an authenticated channel with a trusted collaborator. Identity bindings and common bases persist locally; concurrent edits create retained conflicts. Media is staged without license attestations. Credentials, signed ledgers, queue state and approval keys must never be synchronized. Tailscale diagnostics do not establish a restricted OS account, change ACLs or expose the local approval server. See [collaboration.md](docs/collaboration.md).

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
- There is no environment switch or flag that skips the human terminal (`AVE_ALLOW_UNATTENDED_APPROVAL` and `--confirm-sha` were removed).
  Tests call `ave.publish.queue.approve(...)` directly or patch `ave.humanpresence.is_human_terminal`.
- Approvals expire: `approval_problems` refuses an approval older than `publish.approval_ttl_days` (default 14, `config/publish.default.yaml`,
  override in `~/.config/ave/publish.yaml`); approve again after re-reading the item.
- The bytes sent are the bytes hashed: before each platform send the approved file(s) are copied to a private temp directory, hashed
  there, and the platform adapter is handed the verified copy.
- The aggregator API key is sent only to a host the human pinned (`ave publish auth aggregator --host H`, or `AVE_AGGREGATOR_HOSTS`),
  over https (or loopback), never to whatever `base_url` a config file names.

#### Honest limits: what is a convention and what is a control

Human-only commands (`ave publish approve`, `ave sequence approve`, `ave trim set`, `ave trim clear`, `ave learn taste apply`, `ave content canva-approve`, and the content
studio's `ave studio approve|ui|account live|account auth|resume|resolve|broll pick|broll generate|schedule apply|tick --send|run --send|send enable|send disable|library attest`)
call `ave.humanpresence.require_human`: stdin and stdout must be a terminal and none of `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`,
`AVE_AGENT` may be set. **This stops an accident, not an adversary.** An agent that can run shell commands as your user can allocate a
pty (`script`, `expect`, `python -c "import pty"`), unset the markers, and read the HMAC approval key from the keychain or
`~/.config/ave/approval.key`; the signature protects against hand-edited queue files, not against code running as you. The real
control is the agent harness. Add deny rules so the agent cannot start these commands at all (copy
[`.claude/settings.example.json`](.claude/settings.example.json) into `.claude/settings.json` or `~/.claude/settings.json`):

```json
{"permissions": {"deny": ["Bash(ave publish approve*)", "Bash(ave publish run*)", "Bash(ave sequence approve*)",
                          "Bash(ave trim set*)", "Bash(ave trim clear*)", "Bash(ave learn taste apply*)", "Bash(ave studio approve*)", "Bash(ave studio ui*)",
                          "Bash(ave studio account live*)", "Bash(ave studio account auth*)", "Bash(ave studio resume*)", "Bash(ave studio resolve*)",
                          "Bash(ave studio broll pick*)", "Bash(ave studio broll generate*)", "Bash(ave studio schedule apply*)",
                          "Bash(ave studio tick --send*)", "Bash(ave studio run --send*)", "Bash(ave studio send*)", "Bash(ave studio library attest*)",
                          "Bash(ave studio account set*)", "Bash(ave studio library add*)", "Bash(ave studio adopt*)", "Bash(ave studio feedback resolve*)",
                          "Bash(ave publish resolve*)", "Edit(**/studio/ledger/**)", "Write(**/studio/accounts/**)"]}}
```

(abridged: the full list, including `Edit`/`Write`/shell-redirect rules for the gating files, is in `.claude/settings.example.json`.)

Consider also denying `python -c` style escapes and running the agent as a different OS user than the one holding the keychain.
`ave clips decide` and `ave content decide` stay open to the agent but every ledger line records `via: tty|notty`.
- Full description and the per-platform setup: [docs/publishing-setup.md](docs/publishing-setup.md).

### Content studio threat model (read this before trusting any gate)

The studio posts to real public accounts, so its controls are described by what they do and do NOT do.

**The attacker we design for** is a coding agent (or text it was talked into obeying) with a same-user shell, that is, a shell as YOUR OS user. Such an agent can edit any file the
studio keeps, can run any `ave` command that is not behind a human check, and can read the HMAC key (keychain, `AVE_APPROVAL_KEY`, or
`~/.config/ave/approval.key`). **File-level tampering therefore cannot be made impossible**, and nothing below pretends it is. The goals are:

1. **Every sanctioned path is safe.** Each agent-open command and each UI path either cannot satisfy a human gate or is limited to its safe direction:
   `queue sync` never approves; `feedback resolve` closes a send-back only with evidence the post changed (and a send-back already voided the approval);
   `library add` and `adopt` record a license CLAIM that does not count until `library attest`; `account set` of platform, mode, time zone or credentials
   voids the live attestation; `pause` is open but `resume` is not; the UI approves the queue item it shows and refuses when the item differs from the post;
   `ave publish run` refuses studio items.
2. **Tampering is DETECTED loudly, not silently honoured.** The feedback, dispatch and control ledgers are hash-chained and HMAC-signed with a signed head
   record (`src/ave/studio/seal.py`): an edited, removed, reordered or truncated line, a deleted ledger or head, or an unsigned appended line fails
   verification. Approvals carry a signed binding (account, slot, approval-lifetime policy) and are single-use through the sealed dispatch ledger. A live
   attestation binds the account's credentials and carries a sequence number the sealed control ledger also holds (`--off` is a signed record; a saved copy
   of an older "on" file is stale). A pause also lives in the sealed ledger: `rm studio/PAUSE` does not resume. **Every tick verifies all of this first;
   on a failure it raises a `tamper` alert and sends nothing**, and `ave studio doctor` reports it.
3. **The harness protects the gating files.** Copy [`.claude/settings.example.json`](.claude/settings.example.json) into `.claude/settings.json`: it denies
   the agent `Edit`/`Write` and shell redirects to `studio/accounts/**` (account YAML, `*.live.json`, `*.paused`), `studio/ledger/**`,
   `studio/publish/queue/**`, `studio/schedule/**`, `studio/PAUSE`, `studio/send.enabled`, `studio/studio.json` (approval TTL and mode, quiet-hour and
   retime policy), `studio/policy/**`, `~/.config/ave/**` (publish.yaml TTL, host.yaml) and `approval.key`, plus the human-only commands
   (`ave studio approve|ui|resume|resolve|account live|account auth|account set|account add|account remove|library add|library attest|adopt|`
   `feedback resolve|send|broll pick|broll generate|schedule apply|tick --send|run --send`, `ave publish approve|run|resolve`). A shell deny rule is a
   speed bump (`python -c` still writes); running the agent as a DIFFERENT OS user than the one that owns the keychain and the studio is the real wall.

**What is NOT defended (documented limits).** An agent that reads the HMAC key can forge approvals, attestations and ledger lines. A pty-spawning agent
that strips the agent markers passes `require_human`, including for `ave studio ui` (it prints the token and PIN to a terminal the agent owns). Restoring
a ledger together with its signed head from an EARLIER backup is not detected (there is no off-machine anchor). Removing a file and its record together
from a backup is not detected. A tamper alert only helps if a human reads it. The PIN is out-of-band only against an agent that cannot see your terminal.
Hardening that would help and is not built: a per-use OS authentication prompt (Touch ID) on approve and on `account live`, a keychain item the venv
python cannot read, and refusing `ave studio ui` when an agent is in the process chain.

**Unattended sender.** A launchd job has no terminal, so `ave studio tick --send` accepts the human's signed `studio/send.enabled` instead
(`ave studio send enable`, a human act; bound to the studio's path; `send disable` writes a signed off; an agent marker still refuses).
`ave studio daemon-plist --send` prints a job that runs `tick --send` on an interval; installing it is yours.

**Untrusted text.** Captions, feedback notes, alerts, platform error text, B-roll provider titles and tags, transcripts, ingested Markdown and library
metadata are data, never instructions. Every `ave-studio*` skill and `AGENTS.md` say so; the dangerous commands above are human-gated so that an
injected instruction has no lever.

### The app's loopback server (`ave desktop`, `ave studio ui`)

- Binds to `127.0.0.1` only. A random per-run token (`secrets.token_urlsafe(24)`) is required on every request, static files
  included. It arrives once as `?t=` in the URL the app opens (or the terminal prints, for `ave studio ui`), then moves into an
  `HttpOnly; SameSite=Strict` cookie. `X-AVE-Token` also works for curl.
- The `Host` header must be `127.0.0.1:<port>` or `localhost:<port>` (DNS-rebinding defence); a POST, PUT or PATCH with a foreign
  `Origin` is refused; no CORS headers are sent.
- Files are served only from inside the studio or the episode project (no `..`, no symlinks that leave it, no dot-files, never
  the raw footage directories, whatever their case and through symlinks): `src/ave/studio/ui/http.py`.
- The Workspace's Jobs panel runs only an allowlist of `ave` subcommands with typed arguments (`src/ave/jobs.py`), with no shell,
  no free-form paths, and no publish/approve/send; jobs run with `AVE_AGENT=1`.
- In a browser nothing can be approved. `ave ui` is retired.
- Treat the URL like a password for the duration of the run. Do not paste it into chats or issue reports.

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
