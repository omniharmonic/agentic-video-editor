"""`ave learn taste report|suggest|apply <episode>`: the show's clip taste, from the decision ledger.

`ave learn <episode> "insight"` (progress.py) records a field note; this is a separate family. `ave.cli.main` routes
`ave learn taste ...` here before argparse sees it, because `learn` already takes an episode as its first positional.
"""

from __future__ import annotations

import argparse
import json

from .. import taste
from ..episode import Episode


def _ep(a) -> Episode:
    ep = Episode(a.episode)
    ep.require()
    return ep


def cmd_report(a) -> int:
    rep = taste.report(_ep(a), a.top, a.also)
    print(json.dumps(rep, indent=2) if a.json else taste.format_report(rep))
    return 0


def cmd_suggest(a) -> int:
    s = taste.suggest(_ep(a), a.also, a.taste)
    print(json.dumps(s, indent=2, ensure_ascii=False))
    return 0


def cmd_apply(a) -> int:
    r = taste.apply_suggestion(_ep(a), a.also, a.taste, dry_run=a.dry_run)
    print(json.dumps(r, indent=2, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ave learn taste", description=__doc__)
    sub = p.add_subparsers(dest="taste_cmd", required=True)

    def common(sp):
        sp.add_argument("episode")
        sp.add_argument("--also", action="append", metavar="EPISODE", help="include another episode's ledger (repeatable); the episode's workspace siblings are included automatically")

    s = sub.add_parser("report", help="hit rate: my top-N by score vs what you kept/skipped, reasons, keep rate per tag")
    common(s)
    s.add_argument("--top", type=int, default=10, help="N for the top-N hit rate (default 10)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("suggest", help="propose rubric weight changes from per-feature keep-rate lift (needs >= 40 decisions); changes nothing")
    common(s)
    s.add_argument("--taste", help="taste profile path (default: AVE_TASTE_DIR, workspace taste/, ~/.config/ave/taste/clips.yaml)")
    s.set_defaults(fn=cmd_suggest)

    s = sub.add_parser("apply", help="HUMAN ONLY: write the suggested weights into the taste profile (agents never run this)")
    common(s)
    s.add_argument("--taste")
    s.add_argument("--dry-run", action="store_true", help="show what would be written")
    s.set_defaults(fn=cmd_apply)
    return p


def main(argv: list[str]) -> int:
    a = build_parser().parse_args(argv)
    return a.fn(a)
