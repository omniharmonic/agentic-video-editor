"""Editorial collaboration plugin. No network mutations, approvals or arbitrary file import."""
from __future__ import annotations

from .. import collab
from ..studio.cli import guarded, out
from ..studio.project import find_studio


@guarded
def command(a):
    if a.action == "tailscale":
        out({"ok": True, **collab.tailscale_status()})
        return 0
    sp = find_studio(a.studio)
    sp.require()
    if a.action == "status":
        result = collab.status(sp)
    elif a.action == "export":
        result = collab.export_bundle(sp, a.output, peer=a.peer, post_ids=a.post, include_media=a.include_media)
    elif a.action == "import":
        result = collab.import_bundle(sp, a.bundle, peer=a.peer, apply=a.apply)
    else:
        result = collab.resolve_conflict(sp, a.conflict, take=a.take, expected_rev=a.expected_rev, apply=a.apply)
    out(result)
    return 0 if result["ok"] else 2


def register(sub):
    root = sub.add_parser("collaborate", help="exchange offline editorial drafts safely; publishing authority stays local")
    ss = root.add_subparsers(dest="action", required=True)
    p = ss.add_parser("status", help="local peer identities and unresolved editorial conflicts (read only)")
    p.add_argument("--studio", metavar="DIR")
    p.set_defaults(fn=command)
    p = ss.add_parser("export", help="create an immutable JSON bundle OUTSIDE the live studio")
    p.add_argument("output", help="new bundle file in an explicitly shared exchange folder")
    p.add_argument("--studio", metavar="DIR")
    p.add_argument("--peer", required=True, help="stable local name for this collaborator, e.g. tomas")
    p.add_argument("--post", action="append", help="select a post (repeatable); default all, up to 500")
    p.add_argument("--include-media", action="store_true", help="include up to 20 MiB of referenced library media; no license/attestation is transferred")
    p.set_defaults(fn=command)
    p = ss.add_parser("import", help="preview reconciliation; --apply saves drafts and preserves conflicts")
    p.add_argument("bundle")
    p.add_argument("--studio", metavar="DIR")
    p.add_argument("--peer", required=True, help="stable local name for the sender; identity changes are refused")
    p.add_argument("--apply", action="store_true")
    p.set_defaults(fn=command)
    p = ss.add_parser("resolve", help="resolve an editorial conflict explicitly; keep both snapshots on disk")
    p.add_argument("conflict", help="id from collaborate status")
    p.add_argument("--studio", metavar="DIR")
    p.add_argument("--take", required=True, choices=["local", "incoming"])
    p.add_argument("--expected-rev", type=int, required=True)
    p.add_argument("--apply", action="store_true")
    p.set_defaults(fn=command)
    p = ss.add_parser("tailscale", help="read-only Tailscale connection diagnostics; never enables SSH or changes ACLs")
    p.set_defaults(fn=command)
