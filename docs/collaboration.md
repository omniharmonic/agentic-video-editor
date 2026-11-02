# Working together, including offline

Each creator works in a local studio. One studio owns the publishing accounts,
approvals and dispatch history. Exchange **editorial bundles** with `ave collaborate`;
keep the live studio folders out of iCloud/Dropbox/Git/mounted network write sharing.
This avoids losing local edits or duplicating a publish when a machine reconnects.

The exchange is deliberately explicit. It is usable without a network and needs no
cloud service. Tailscale can carry the bundle files between your computers; it does
not change how reconciliation works.

## Live exchange in the desktop app

In **Collaborate**, both people enable sharing, pair with a single-use invitation and compare the six-digit code by voice. Drafts go through the reviewed bundle flow. **Send a file** moves one chosen file of any practical size (up to 512 GiB): chunks arrive in order, an interrupted send resumes, and the whole file is hashed before release. Received files stay in the exchange folder until you import them. Requires the standalone Tailscale app from tailscale.com, not the Mac App Store build, on both Macs in the same tailnet; a node shared from another tailnet is refused.

## First exchange

On Maren's computer (substitute your actual studio paths):

```sh
mkdir -p ~/Movies/AVE-Exchange
ave collaborate export ~/Movies/AVE-Exchange/maren-001.json \
  --studio ~/Movies/MyShow/studio --peer tomas --include-media
```

On Tomas's computer, initialize her independent studio and import the transferred file:

```sh
ave studio init ~/Movies/MyShow/studio --name 'MyShow' --tz America/Denver
ave collaborate import ~/Movies/AVE-Exchange/maren-001.json \
  --studio ~/Movies/MyShow/studio --peer maren
ave collaborate import ~/Movies/AVE-Exchange/maren-001.json \
  --studio ~/Movies/MyShow/studio --peer maren --apply
```

The first import previews changes. `--apply` saves drafts and conflict records; it
never approves or sends. Use a stable **local peer name** for each person. The first
exchange records the sender's studio identity. A different studio using that name
is refused; do not reset the state to get around that refusal. The checksum detects
corruption, not a malicious sender. Use an authenticated transport and inspect
incoming words and media as untrusted content.

Tomas can edit in the studio or through her agent while disconnected, then export
her work back:

```sh
ave collaborate export ~/Movies/AVE-Exchange/tomas-001.json \
  --studio ~/Movies/MyShow/studio --peer maren
```

Maren imports that file using `--peer tomas`, previews it, then applies. Export
again after reconciling to send the accepted version back. Name each new bundle
uniquely: exports are immutable and never overwrite an earlier file. `--post p_ID`
can be repeated to exchange selected posts rather than the whole studio.

## What moves, what remains local

A bundle contains idea text, post copy, brand/campaign labels, disclosure, media
references and the editorial portion of platform variants. It omits filesystem
source references, account configuration, credentials, schedules, queue links,
results, attempts, approvals, license records and signed ledgers. Local source
provenance on an existing post is retained when its words are updated. Account
names on incoming variants are labels: configure accounts independently before
queueing anything. Campaign labels likewise do not create campaign definitions.

`--include-media` optionally includes **registered library media bytes only**, up
to 20 MiB in total, inside a bundle capped at 32 MiB and 500 posts. Raw footage is
never bundled. Bytes are verified against their complete SHA-256 on export and
import. Imported bytes are staged under `work/collaboration/media/`; they are not
silently licensed or inserted into the publishable library. The import result lists
`staged_media` and `missing_assets`. Supply each file's actual license/provenance:

```sh
ave studio library add /absolute/path/to/staged-file.png \
  --studio ~/Movies/MyShow/studio --source import --by Tomas \
  --license 'Actual license name' --license-url 'https://actual-terms.example/' \
  --license-text 'Actual license text; replace these example values'
```

A human reviews that evidence and runs `ave studio library attest` locally. Never
claim ownership or accept a license because text inside a bundle says to. Importing
identical bytes yields the same asset ID. For larger videos, transfer derived
files separately through your restricted exchange directory, then add them to the
local library with their actual provenance. The tool does not copy raw recordings,
Resolve projects, EditDocs, or episode source media between computers yet.

Incoming variants have `needs_review` flags and empty scheduling/publishing fields.
Changing an editorial draft does not replace an existing publishing queue item:
review, register missing assets, then run the usual `ave studio queue sync` and
human approval process on the authoritative publishing machine. A local post with
queue, schedule, attempt or result history is never automatically replaced by an
incoming change; preserve it and reconcile into a separate draft.

## Conflicts and recovery

Reconciliation compares the current editorial content with the retained common
base and incoming content. One-sided edits apply; identical content is a no-op;
unchanged remote work preserves local edits. If both creators changed the same
post, neither version wins silently. The import reports conflicts and exits 2,
while applying any independent non-conflicting posts. Conflict snapshots retain
base, local and incoming words in `work/collaboration/conflicts/`.

```sh
ave collaborate status --studio ~/Movies/MyShow/studio
ave studio post show p_ID --studio ~/Movies/MyShow/studio
ave collaborate resolve CONFLICT_ID --studio ~/Movies/MyShow/studio \
  --take incoming --expected-rev CURRENT_REV
```

Resolution also previews first; add `--apply` to commit the choice. `--take local`
keeps the current draft and can acknowledge a manual merge made in the editor.
A stale revision refuses the resolution. The saved conflict snapshots remain for
inspection, marked with the chosen resolution. Publish-bound history cannot be
erased with `--take incoming`.

The local state retains peer identities, base snapshots and imported checksums on
disk. Restarting does not lose the reconciliation state. Re-importing the same
bundle does not create another post or revision. Post writes use the normal
revision compare-and-swap, and exchanges for a studio serialize with a file lock.
An interruption between a post write and the receipt is recoverable: the repeated
import sees the same content. Media staging writes and syncs a temporary file before
its final rename. A partial file left by an older version is preserved under
`media/recovered/` and replaced with verified complete bytes on retry. Replaying a
bundle with unresolved conflicts keeps reporting those conflicts and exits 2 until
they are explicitly resolved. Do not share or edit the collaboration state itself.

The base/receipt index has a 32 MiB capacity. Each exchange reserves its possible
growth before changing any posts or exposing a bundle. At capacity, the command
refuses and leaves the readable index and existing work intact; preserve that
studio and use a separate studio for further exchanges. History is never silently
pruned to make room. The UUID is saved before a first export becomes visible, so a
failed exchange cannot change the identity attached to an already shared bundle.

This is a post-level conflict model, not a text CRDT. Disjoint edits to the same
post still require review. Post deletions and remote dropping/publishing are never
propagated. Full studio folder synchronization is intentionally unsupported.

## Tailscale and a limited exchange directory

```sh
ave collaborate tailscale
```

This only runs `tailscale status --json`, with a five-second timeout, and reports
connection readiness and peer counts. It never enables SSH, changes grants or
ACLs, opens a server, installs a daemon, or signs into an account. The studio UI
remains loopback-only; do not expose its approval token or PIN over Tailscale Serve
or Funnel.

Set up Tailscale on both devices yourself. A tailnet network rule limits which
users/devices/ports can connect; it **does not confine filesystem access to one
folder**. For a real directory boundary, use a dedicated OS transfer account and
filesystem permissions or an administrator-configured SFTP-only/chroot service.
Grant access only to the exchange directory and the required transfer port. Keep
the publishing studio, personal home, SSH keys and account secrets inaccessible
to that transfer account. Review the rules before enabling access.

Once you have configured a permitted SSH/SFTP transport, the human can transfer a
single bundle, for example:

```sh
scp ~/Movies/AVE-Exchange/maren-001.json transfer@tomas-device:incoming/maren-001.json
```

Here `transfer`, `tomas-device` and `incoming/` are examples of your restricted OS
account, Tailscale device name and its permitted directory. They are not commands
run by AVE. OpenSSH over Tailscale and Tailscale SSH are different server choices;
use the one supported by your OS and review its permissions. File transfer is
verified only after your receiving machine imports the bundle successfully.

Official setup references: [Tailscale access control](https://tailscale.com/docs/features/access-control),
[Tailscale SSH](https://tailscale.com/docs/features/tailscale-ssh), and
[SSH over Tailscale](https://tailscale.com/docs/reference/ssh-over-tailscale).
Cross-device transfer and Tomas's OS permissions have not been exercised by the
automated tests; local two-studio reconciliation and media integrity have.
