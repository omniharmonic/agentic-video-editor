"""`ave project adopt <folder>` and `ave project status <folder>`: join an existing folder to the project model.

`ave start <folder>`: take that folder to a rough cut in one go (`ave.start`), flagging every choice made alone on the board."""

from __future__ import annotations

import json


def _plain_adopt(r: dict) -> str:
    lines = [f"{'Adopted' if r['created_project'] else 'Already a project'}: {r['title']} ({r['kind']}, {r['project_id']})"]
    if not r["artifacts_added"] and not r["versions_added"]:
        lines.append("Nothing new to record.")
    if r["artifacts_added"]:
        lines.append(f"Recorded {len(r['artifacts_added'])} item(s): {', '.join(r['artifacts_added'][:8])}"
                     + (" ..." if len(r["artifacts_added"]) > 8 else ""))
    if r["versions_added"]:
        lines.append(f"Recorded {len(r['versions_added'])} version(s): {', '.join(r['versions_added'][:8])}"
                     + (" ..." if len(r["versions_added"]) > 8 else ""))
    if r["hash_pending"]:
        lines.append(f"{len(r['hash_pending'])} footage file(s) noted by size and date; their checksums come later.")
    lines.append("No existing file was moved or changed; only .ave/ was added.")
    lines += [f"Warning: {w}" for w in r["warnings"]]
    return "\n".join(lines)


def _plain_status(s: dict) -> str:
    if not s["adopted"]:
        return (f"{s['root']} is not a project yet ({s['edit_files']} edit version(s) on disk).\n"
                f"Run: ave project adopt {s['root']}")
    d = s["device"]
    lines = [f"{s['title']} ({s['kind']}, {s['project_id']})",
             f"This computer: {d['tag']} ({d['person']})",
             "People: " + ", ".join(f"{m.get('person')} ({len(m.get('devices') or [])} computer(s))" for m in s["members"])]
    if s["artifacts"]:
        lines.append("Items:")
        for a in s["artifacts"]:
            where = "archived" if a["archived"] else a["stage"]
            vers = f", {a['versions']} version(s), latest {a['latest']}" if a["versions"] else ""
            lines.append(f"  {a['title'] or a['id']} [{a['kind'] or 'item'}]: {where}{vers}")
    if s["unrecorded_edits"]:
        lines.append(f"{len(s['unrecorded_edits'])} edit file(s) not recorded yet; run: ave project adopt {s['root']}")
    if s["hash_pending"]:
        lines.append(f"{len(s['hash_pending'])} footage file(s) waiting for a checksum.")
    if s["conflicts"]:
        lines.append(f"{s['conflicts']} disagreement(s) to settle.")
    lines += [f"Warning: {w}" for w in s["warnings"]]
    return "\n".join(lines)


def cmd_adopt(a) -> int:
    from ..project import ProjectError
    from ..project.adopt import adopt
    try:
        r = adopt(a.folder).to_dict()
    except ProjectError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(r, indent=2, ensure_ascii=False) if a.json else _plain_adopt(r))
    return 0


def cmd_status(a) -> int:
    from ..project import ProjectError
    from ..project.adopt import status
    try:
        s = status(a.folder)
    except ProjectError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(s, indent=2, ensure_ascii=False) if a.json else _plain_status(s))
    return 0


def _speaker_args(pairs: list[str] | None) -> dict:
    out = {}
    for p in pairs or []:
        sid, sep, name = p.partition("=")
        if not sep or not sid.strip() or not name.strip():
            raise SystemExit(f"--speaker needs ID=NAME, e.g. --speaker S1=Maren (got {p!r})")
        out[sid.strip().upper()] = name.strip()
    return out


def cmd_start(a) -> int:
    from .. import start
    try:
        r = start.run(a.folder, through=a.through, clips=a.clips, speakers=_speaker_args(a.speaker),
                      answer=" ".join(f"--speaker {p}" for p in a.speaker) if a.speaker else None)
    except start.NeedsDirector as e:
        print(json.dumps({"ok": False, "needs_director": e.question, "decision": e.decision, "report": e.report.to_dict() if e.report else None}, indent=2,
                         ensure_ascii=False) if a.json else (e.report.text() if e.report else e.question))
        return 2
    except start.StartError as e:
        if a.json:
            print(json.dumps({"ok": False, "failed": e.step, "error": str(e), "report": e.report.to_dict() if e.report else None}, indent=2, ensure_ascii=False))
        else:
            if e.report:
                print(e.report.text())
            print(f"\nStopped at {e.step}: {e}")
        return 1
    print(json.dumps({"ok": True, **r.to_dict()}, indent=2, ensure_ascii=False) if a.json else r.text())
    return 0


def register(sub) -> None:
    from ..start import STEPS
    s = sub.add_parser("start", help="take a folder of footage (new or half-done) to a rough cut in one go; stops only to ask who is speaking, "
                                     "and flags every choice it made alone on the board for review")
    s.add_argument("folder")
    s.add_argument("--through", default="rough", choices=STEPS, help="last stage to run (default rough: a rough cut with a draft render)")
    s.add_argument("--clips", action="store_true", help="also propose clip candidates from the rough cut (choosing them stays the director's)")
    s.add_argument("--speaker", action="append", metavar="ID=NAME", help="the director's answer to who is speaking, e.g. --speaker S1=Maren (repeat)")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(fn=cmd_start)

    p = sub.add_parser("project", help="shared projects: adopt an existing folder, or show what the project log says")
    psub = p.add_subparsers(dest="project_cmd", required=True)
    s = psub.add_parser("adopt", help="add .ave/ to an episode or studio folder and record what is there (moves nothing)")
    s.add_argument("folder")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(fn=cmd_adopt)
    s = psub.add_parser("status", help="the project's items, stages and versions")
    s.add_argument("folder")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(fn=cmd_status)
