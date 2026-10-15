"""`ave clips ...`: prepare evidence, validate the agent's picks, build selects, review pack, record decisions."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .. import clips, review_pack, state
from ..episode import Episode, read_json


def _ep(a) -> Episode:
    ep = Episode(a.episode)
    ep.require()
    return ep


def _out(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def _read(path: str, what: str):
    p = Path(path).expanduser()
    if not p.is_file():
        raise SystemExit(f"{what} not found: {p}")
    try:
        return read_json(p)
    except ValueError as e:
        raise SystemExit(f"{p} is not valid JSON: {e}")


def _flag(a) -> str:
    return f" --name {a.name}" if a.name else ""


def cmd_prepare(a) -> int:
    _out(clips.prepare(_ep(a), a.stage, a.version, a.min_s, a.max_s, a.rubric, a.max_candidates, name=a.name, floor=a.floor))
    return 0


def cmd_propose(a) -> int:
    ep = _ep(a)
    if a.picks_only and not a.picks:
        raise SystemExit("--picks-only needs --picks.")
    r = clips.propose(ep, _read(a.picks, "--picks file") if a.picks else None, a.rubric, name=a.name, recall_fill=not a.picks_only,
                      top_n=a.top, min_score=a.min_score)
    if r["accepted"]:
        ep.mark_stage("selects", clips=len(r["accepted"]))
        state.set_review(ep, f"{len(r['accepted'])} clip proposals ready: run `ave clips review{_flag(a)}` and decide")
    _out(r)
    return 2 if r["rejected"] else 0       # 2 = some picks were rejected (accepted ones ARE saved); read "rejected"


def cmd_build(a) -> int:
    _out(clips.build(_ep(a), a.approved_only, a.gap, a.order, a.force, name=a.name, include_stale=a.include_stale,
                     top_n=a.top, min_score=a.min_score))
    return 0


def cmd_review(a) -> int:
    ep = _ep(a)
    r = review_pack.build_pack(ep, drafts=a.drafts, out_dir=a.out, name=a.name)
    for n in r["notes"]:
        print(f"note: {n}", file=sys.stderr)
    state.set_review(ep, f"review clips at {r['index']}, then `ave clips decide{_flag(a)}`")
    _out(r)
    return 0


def _after_decide(ep: Episode, r: dict) -> int:
    if r["applied"]:
        state.set_review(ep, None if not any(c["status"] == "proposed" and not c.get("maybe") for c in clips.load_clips(ep)["clips"])
                         else "some clip proposals still await a decision")
    _out(r)
    return 2 if r["errors"] else 0


def cmd_decide(a) -> int:
    """Two forms, one code path: `decide EP decisions.json` (a file) or `decide EP CLIP_ID --verdict keep|maybe|skip [--reason R]...`."""
    ep = _ep(a)
    if a.verdict or a.reason or a.note:
        if not a.verdict:
            raise SystemExit("--reason/--note go with --verdict keep|maybe|skip.")
        item = {"id": a.target, "verdict": a.verdict, "reasons": a.reason or [], "note": a.note}
        return _after_decide(ep, clips.decide(ep, [item], name=a.name, by=a.by))
    if a.target is None:
        raise SystemExit("Give a decisions file, or a clip id with --verdict keep|maybe|skip.")
    return _after_decide(ep, clips.decide(ep, _read(a.target, "decisions file"), name=a.name, by=a.by))


def cmd_import(a) -> int:
    """Ingest the decisions JSON the review page exports (keep/maybe/skip + reasons + notes + nudges) into the ledger."""
    ep = _ep(a)
    doc = _read(a.file, "decisions file")
    if not isinstance(doc, dict) or doc.get("kind") != "ave-clip-decisions" or not isinstance(doc.get("decisions"), list):
        raise SystemExit(f"{a.file} is not a review-page export (expected {{\"kind\": \"ave-clip-decisions\", \"decisions\": [...]}}).")
    name = a.name or doc.get("session") or None
    return _after_decide(ep, clips.decide(ep, doc["decisions"], name=name, by=a.by or doc.get("by"), source="cli"))


def cmd_list(a) -> int:
    _out(clips.list_rows(_ep(a), name=a.name, show_all=a.all, top_n=a.top, min_score=a.min_score))
    return 0


def cmd_previews(a) -> int:
    from .. import previews
    ep = _ep(a)
    ids = [x.strip() for x in a.ids.split(",") if x.strip()] if a.ids else None
    r = previews.run(ep, a.name, ids, a.top, a.pad, a.height, a.force, a.dry_run, a.workers,
                     False if a.no_proxies else None, log=lambda m: print(m, file=sys.stderr, flush=True), progress_log=not a.no_progress_log)
    _out(r)
    return 2 if r["failed"] else 0


def register(sub) -> None:
    p = sub.add_parser("clips", help="find, validate and review short-form clips (prepare/propose/build/review/decide)")
    cs = p.add_subparsers(dest="clips_cmd", required=True)

    def cutoff(sp, what: str):
        sp.add_argument("--top", type=int, metavar="N", help=f"{what} the N best clips per session (rank 1 = best); the cut-off is a VIEW, no clip is deleted "
                                                             "(default: config recall.top_n)")
        sp.add_argument("--min-score", type=float, metavar="S", help=f"{what} clips scoring at least S (0-100); with --top both must hold "
                                                                     "(default: config recall.min_score)")

    def name(sp):
        sp.add_argument("--name", metavar="SESSION", help="session whose edit (rough.<name>) to work on; required when the episode has several sessions")

    s = cs.add_parser("prepare", help="sentence table + EVERY clip-worthy section above the floor (scored, tagged, explained) -> work/clips_candidates.json")
    s.add_argument("episode")
    s.add_argument("--stage", choices=["assembly", "rough", "final"], help="source edit (default: rough, else assembly)")
    s.add_argument("--version", type=int, help="edit version (default: latest)")
    s.add_argument("--min-s", type=float, default=20.0)
    s.add_argument("--max-s", type=float, default=60.0)
    s.add_argument("--max-candidates", type=int, default=None, help="sanity cap on surfaced sections (default: config recall.max_per_session, 80; truncation is spread across the whole episode and reported)")
    s.add_argument("--floor", type=float, default=None, help="lowest heuristic score (0-100) a section needs to be surfaced (default: config recall.floor, 30)")
    s.add_argument("--rubric", help="rubric YAML (default: <ep>/policy/clips.rubric.yaml, then config/clips.rubric.yaml)")
    name(s)
    s.set_defaults(fn=cmd_prepare)

    s = cs.add_parser("propose", help="validate the agent's picks (sentence ids + quotes) and ALSO propose every surfaced section above the floor -> work/clips.json")
    s.add_argument("episode")
    s.add_argument("--picks", help="JSON list of {title, hook, sentence_ids, scores, rationale, ...}; omit to propose every surfaced section by heuristic score")
    s.add_argument("--picks-only", action="store_true", help="propose only the picks (no recall fill with the other surfaced sections)")
    s.add_argument("--rubric")
    cutoff(s, "report as above the line")
    name(s)
    s.set_defaults(fn=cmd_propose)

    s = cs.add_parser("build", help="one select.<slug> EditDoc per clip + the combined select.selects timeline")
    s.add_argument("episode")
    s.add_argument("--approved-only", action="store_true")
    s.add_argument("--gap", type=float, help="seconds of room tone between clips in the combined timeline (default: rubric)")
    s.add_argument("--order", choices=["time", "score"], default="time")
    s.add_argument("--force", action="store_true", help="rebuild even if nothing changed (writes new versions)")
    s.add_argument("--include-stale", action="store_true",
                   help="also build stale picks, from the exact older edit they were picked from (default: skip and list them)")
    cutoff(s, "build only")
    name(s)
    s.set_defaults(fn=cmd_build)

    s = cs.add_parser("review", help="static review page with thumbnails -> exports/review/index.html")
    s.add_argument("episode")
    s.add_argument("--drafts", action="store_true", help="also render proxy drafts via ave.compile (if available)")
    s.add_argument("--out", help="output folder (default: <episode>/exports/review, or exports/review/<name>)")
    name(s)
    s.set_defaults(fn=cmd_review)

    s = cs.add_parser("decide", help="record decisions: a decisions.json, or one clip with --verdict keep|maybe|skip [--reason R]... [--note N]")
    s.add_argument("episode")
    s.add_argument("target", nargs="?", help="decisions.json (approve/reject/nudge or keep/maybe/skip entries), or a clip id when --verdict is given")
    s.add_argument("--verdict", choices=["keep", "maybe", "skip"], help="keep = approved, skip = rejected, maybe stays proposed; all go in the learning ledger")
    s.add_argument("--reason", action="append", metavar="R", help="why (repeatable): see config/clip_reasons.yaml, e.g. weak_ending hook_late off_topic")
    s.add_argument("--note", help="free text")
    s.add_argument("--by", help="who decided (default: $AVE_USER or the login name)")
    name(s)
    s.set_defaults(fn=cmd_decide)

    s = cs.add_parser("import-decisions", help="ingest the decisions JSON exported by the review page into the ledger and clips.json")
    s.add_argument("episode")
    s.add_argument("file", help="decisions JSON downloaded from exports/review/index.html")
    s.add_argument("--by", help="who decided (default: the export's `by`, else $AVE_USER / login name)")
    name(s)
    s.set_defaults(fn=cmd_import)

    s = cs.add_parser("list", help="compact table of current clips")
    s.add_argument("episode")
    s.add_argument("--all", action="store_true", help="every session's clips (multi-session episodes)")
    cutoff(s, "show, best first, only")
    name(s)
    s.set_defaults(fn=cmd_list)

    s = cs.add_parser("previews", help="render a small 540p review preview per clip (clip + pad on each side, in context; plus a .tight variant when the clip has tighten ranges) -> work/clips/previews/")
    s.add_argument("episode")
    s.add_argument("--ids", help="comma-separated clip ids (default: every clip that is not rejected)")
    s.add_argument("--top", type=int, help="only the best N (by rank, else score)")
    s.add_argument("--pad", type=float, default=8.0, help="seconds of lead-in and tail around the clip, clamped to the edit (default 8)")
    s.add_argument("--height", type=int, default=540)
    s.add_argument("--force", action="store_true", help="re-render even when the preview is fresh")
    s.add_argument("--dry-run", action="store_true", help="plan and estimate only")
    s.add_argument("--workers", type=int, default=3)
    s.add_argument("--no-proxies", action="store_true", help="decode the real footage even when verified review proxies exist")
    s.add_argument("--no-progress-log", action="store_true", help="do not append to PROGRESS.md")
    name(s)
    s.set_defaults(fn=cmd_previews)
