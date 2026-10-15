"""`ave cleanup`: assembly -> rough cut, and `ave cleanup explain` for the decisions. Mechanical; judgment is in the policy YAML."""

from __future__ import annotations

import json

from .. import cleanup
from ..episode import Episode, read_json


def _ep(path) -> Episode:
    ep = Episode(path)
    ep.require()
    return ep


def _pacing_summary(p: dict) -> dict:
    """The pacing report without the per-reaction decisions (they are in cleanup.json / `ave cleanup explain`); long lists are cut short."""
    out = {k: v for k, v in p.items() if k not in ("decisions", "still_over_limit")}
    left = p["still_over_limit"]
    out["still_over_limit"] = {"count": len(left), "within_tolerance": sum(1 for h in left if h.get("within_tolerance")), "first": left[:5]}
    return out


def cmd_cleanup(a) -> int:
    words = a.words
    if words[0] == "explain":
        if len(words) != 2:
            raise SystemExit("usage: ave cleanup explain <episode> [--name SESSION]")
        ep = _ep(words[1])
        p = ep.artifact(cleanup.report_name(a.name))
        if not p.exists():
            raise SystemExit(f"No cleanup yet. Run: ave cleanup {ep.root}" + (f" --name {a.name}" if a.name else ""))
        print(cleanup.explain(read_json(p)))
        return 0
    if len(words) != 1:
        raise SystemExit("usage: ave cleanup <episode> [--policy PATH] [--dry-run] [--from-version N] [--name SESSION] [--no-pacing]  |  ave cleanup explain <episode>")
    ep = _ep(words[0])
    try:
        r = cleanup.run(ep, policy_path=a.policy, dry_run=a.dry_run, from_version=a.from_version, name=a.name,
                         pacing=not a.no_pacing)
    except (FileNotFoundError, ValueError) as e:
        raise SystemExit(f"{e}")
    out = {"dry_run": r["dry_run"], "assembly": r["assembly"], "policy_source": r["policy"]["source"], "totals": r["totals"],
           "pacing": _pacing_summary(r["pacing"]),
           "zoom_check": {k: v for k, v in r["zoom"].items() if k != "violations"} | {"violations_first": r["zoom"]["violations"][:5]},
           "word_cut_check": {"inside_word": len(r["cutcheck"]["inside_word"]), "stubs": len(r["cutcheck"]["stubs"]), "hot_edges": len(r["cutcheck"]["hot_edges"]),
                              "audio_checked": r["cutcheck"]["audio_checked"], "first": (r["cutcheck"]["inside_word"] + r["cutcheck"]["hot_edges"])[:5]},
           "needs_review": [{"id": d["id"], "type": d["type"], "at": d["e0"], "reason": d["reason"]} for d in r["decisions"] if d["review"]],
           "warnings": r["warnings"]}
    if not r["dry_run"]:
        out.update(saved=str(ep.root / r["rough"]["file"]), cleanup_json=str(ep.artifact(cleanup.report_name(a.name))),
                   next=f"ave cleanup explain {ep.root}" + (f" --name {a.name}" if a.name else ""))
    print(json.dumps(out, indent=2))
    return 0


def register(sub) -> None:
    s = sub.add_parser("cleanup", help="assembly -> rough cut (dead air, fillers, repeats, false starts, masked cuts); `cleanup explain <ep>` lists decisions")
    s.add_argument("words", nargs="+", metavar="episode | explain episode")
    s.add_argument("--policy", help="policy YAML (default: <ep>/policy/cleanup.yaml, ~/.config/ave/cleanup.yaml, config/cleanup.default.yaml)")
    s.add_argument("--dry-run", action="store_true", help="decide and report; save nothing")
    s.add_argument("--from-version", type=int, help="assembly version to clean (default: latest)")
    s.add_argument("--name", help="named edit to clean, e.g. a session id: assembly.<name> -> rough.<name>")
    s.add_argument("--no-pacing", action="store_true", help="skip the pacing pass (reaction shots that break holds longer than pacing.max_hold_s) for this run")
    s.set_defaults(fn=cmd_cleanup)
