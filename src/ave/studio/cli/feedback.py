"""Feedback group: list, next --claim, resolve, stats (spec 2.7, 8). How an agent closes the loop with the human's send-back notes."""

from __future__ import annotations

from collections import Counter

from .. import ledger
from . import Blocked, add_dir, guarded, out, project, studio


@guarded
def cmd_list(a) -> int:
    s = studio(a, adapters=False)
    rows = s.feedback(a.status)
    out({"ok": True, "status": a.status, "count": len(rows), "feedback": rows})
    return 0


@guarded
def cmd_next(a) -> int:
    s = studio(a, adapters=False)
    if a.claim:
        if not a.agent:
            raise ValueError("--claim needs --agent NAME (the claim expires after 30 minutes without a resolve)")
        item = s.claim_next_feedback(a.agent)
    else:
        rows = [f for f in s.feedback("open") if not f.get("claim")]
        item = None
        if rows:
            item = {**rows[0]}
            try:
                item["post_view"] = s.post(item["post"])
            except FileNotFoundError:
                pass
    if item is None:
        out({"ok": True, "feedback": None, "note": "nothing open (or everything open is claimed)"})
        return 0
    out({"ok": True, "claimed": bool(a.claim), "feedback": item,
         "how": ["Read `feedback.note` and `feedback.structured` (the human's before/after is there for human_edit lines).",
                 "Change the post with `ave studio post edit --expected-rev N`, `render`, `post swap-asset` or `move` as the kind asks.",
                 "`ave studio queue sync --post P` so the queue follows; a changed byte or bound copy voids any approval (that is right).",
                 f"`ave studio feedback resolve {item['id']} --rev <the post's new rev> --note \"what you changed\"`. Never approve."]})
    return 0


@guarded
def cmd_resolve(a) -> int:
    from ... import humanpresence
    s = studio(a, adapters=False)
    human = humanpresence.is_human_terminal()          # a person at a terminal may close any note; an agent needs evidence (Studio.resolve_feedback)
    line = s.resolve_feedback(a.id, a.by or ("maren" if human else "claude"), rev=a.rev, note=a.note or "", human=human)
    out({"ok": True, "resolved": line})
    return 0


@guarded
def cmd_stats(a) -> int:
    sp = project(a)
    rows = ledger.all_feedback(sp)
    by_kind = Counter(f["kind"] for f in rows)
    reasons: Counter = Counter()
    for f in rows:
        st = f.get("structured") or {}
        for key in ("visual", "drop"):
            r = (st.get(key) or {}).get("problem") or (st.get(key) or {}).get("reason")
            if r:
                reasons[f"{f['kind']}:{r}"] += 1
    edits = [f for f in rows if f["kind"] == "human_edit"]
    out({"ok": True, "total": len(rows), "open": sum(1 for f in rows if not f["resolved"]), "by_kind": dict(by_kind), "reasons": dict(reasons),
         "human_edits": len(edits), "recent_human_edits": [{"post": f["post"], "field": ((f.get("structured") or {}).get("copy") or {}).get("field"),
                                                           "before": ((f.get("structured") or {}).get("copy") or {}).get("before"),
                                                           "after": ((f.get("structured") or {}).get("copy") or {}).get("after")} for f in edits[-10:]],
         "note": "Human edits are the clearest taste signal: read their before/after pairs before writing the next batch of copy."})
    return 0


def add(ss) -> None:
    fb = ss.add_parser("feedback", help="the human's send-back notes: list, next --claim, resolve, stats")
    sub = fb.add_subparsers(dest="feedback_cmd", required=True)
    p = sub.add_parser("list")
    p.add_argument("--status", default="open", choices=["open", "claimed", "resolved", "all"])
    add_dir(p)
    p.set_defaults(fn=cmd_list)
    p = sub.add_parser("next", help="the oldest open item with its full post context; --claim reserves it for 30 minutes")
    p.add_argument("--claim", action="store_true")
    p.add_argument("--agent")
    add_dir(p)
    p.set_defaults(fn=cmd_next)
    p = sub.add_parser("resolve")
    p.add_argument("id")
    p.add_argument("--rev", type=int, help="the post's rev after your change")
    p.add_argument("--note")
    p.add_argument("--by")
    add_dir(p)
    p.set_defaults(fn=cmd_resolve)
    p = sub.add_parser("stats", help="aggregate by kind and reason, with recent human edits")
    add_dir(p)
    p.set_defaults(fn=cmd_stats)
