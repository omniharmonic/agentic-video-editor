"""Core commands: status, resume, assemble."""

from __future__ import annotations

import json

from .. import assemble, editdoc, sessions, state
from ..episode import Episode


def _ep(a) -> Episode:
    ep = Episode(a.episode)
    ep.require()
    return ep


def cmd_status(a) -> int:
    print(json.dumps(state.status(_ep(a)), indent=2))
    return 0


def cmd_resume(a) -> int:
    print(state.briefing(_ep(a)))
    return 0


def _assemble_one(ep: Episode, canvas, session: str | None, combined: bool = False) -> dict:
    doc = assemble.build_combined(ep, canvas) if combined else assemble.build(ep, canvas, session=session)
    path = editdoc.save_new_version(ep, doc, name=session)
    ep.mark_stage("assembly", edit=path.name)
    out = {"saved": str(path), "video_segments": len(doc["tracks"]["video"]), "duration": doc["duration"]}
    if session:
        out.update(session=session, name=session, excluded=doc.get("excluded", []))
    return out


def cmd_assemble(a) -> int:
    ep = _ep(a)
    w, h = (int(x) for x in a.canvas.lower().split("x"))
    if a.combined and not sessions.is_multi(ep):
        raise SystemExit("--combined joins sessions, but this episode has only one. Run `ave assemble` without flags.")
    if a.combined:
        r = _assemble_one(ep, (w, h), None, combined=True)
        print(json.dumps(r, indent=2))
        state.set_review(ep, "combined assembly ready (sessions joined, uncovered gaps removed): review it (ave handoff) and confirm before cleanup")
    elif a.session or sessions.is_multi(ep):
        ids = [a.session] if a.session else sessions.session_ids(ep)
        res = {sid: _assemble_one(ep, (w, h), sid) for sid in ids}
        print(json.dumps(res if len(res) > 1 else next(iter(res.values())), indent=2))
        state.set_review(ep, f"assembly edit(s) ready for session(s) {', '.join(ids)}: review (ave handoff --stage assembly --name <sid>) and confirm before cleanup")
    else:
        print(json.dumps(_assemble_one(ep, (w, h), None), indent=2))
        state.set_review(ep, "assembly edit ready: review it (ave handoff) and confirm before cleanup")
    return 0


def register(sub) -> None:
    s = sub.add_parser("status", help="stage-by-stage state of an episode (JSON)")
    s.add_argument("episode")
    s.set_defaults(fn=cmd_status)
    s = sub.add_parser("resume", help="plain-text briefing for a fresh session: what is done, what is next")
    s.add_argument("episode")
    s.set_defaults(fn=cmd_resume)
    s = sub.add_parser("assemble", help="cut list -> assembly EditDoc (edits/assembly.vN.json)")
    s.add_argument("episode")
    s.add_argument("--canvas", default="1920x1080", help="output canvas WxH (default 1920x1080)")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--session", help="assemble one session as the named edit assembly.<sid> (covers only where its cameras exist)")
    g.add_argument("--all-sessions", action="store_true", help="one assembly.<sid> per session (the default for a multi-session episode)")
    g.add_argument("--combined", action="store_true", help="join all sessions into one assembly with the uncovered gaps removed")
    s.set_defaults(fn=cmd_assemble)
