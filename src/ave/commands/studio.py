"""`ave studio ...`: the content studio CLI (spec section 8). The implementations live in `ave.studio.cli.*`, one module per command group;
this plugin only registers them (cli.py is never edited).

JSON on stdout, human text on stderr, exit 0 ok, 2 blocked/needs-human (the `ave publish` convention). A human act (approve, account live/auth, resume,
`--send`, a named stock pick, resolve, accepting a schedule proposal, the UI) refuses without a human terminal and prints the exact line to run.

    init status doctor adopt daemon-plist ui
    account add|list|show|set|remove|auth|live|refresh   pause   resume
    library add|list|show|search|tag|scan|reindex|verify
    broll queries|search|show|pick|generate
    post new|list|show|edit|attach|swap-asset|variant add|drop|critique   render   deliverables
    schedule show|rules set|rules show|plan|apply   move   unschedule
    queue sync|list|dry-run   review list   approve
    tick   run   resolve   alerts [ack]   send enable|disable|status
    feedback list|next|resolve|stats   campaign new|show|list|add-post
    ingest idea|blog|audio|video|episode|shorts   recipe list|show|check
"""

from __future__ import annotations

import argparse

from ..studio.cli import accounts, broll, campaigns, feedback, ingestcmd, library, posts, projectcmd, queue, run, schedule, send

GROUPS = (projectcmd, accounts, library, broll, posts, schedule, queue, run, send, feedback, campaigns, ingestcmd)


def _build(ss) -> None:
    for g in GROUPS:
        g.add(ss)


def command_names() -> list[str]:
    """Every top-level `ave studio <name>` (used by tests and the docs check)."""
    p = argparse.ArgumentParser()
    ss = p.add_subparsers()
    _build(ss)
    return sorted(ss.choices)


def register(sub) -> None:
    s = sub.add_parser("studio", help="content studio: calendar, library, review and gated posting (see docs/studio.md)")
    ss = s.add_subparsers(dest="studio_cmd", required=True)
    _build(ss)
