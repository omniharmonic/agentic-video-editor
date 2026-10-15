"""`ave`: one entry point for every deterministic pipeline step.

Skills tell the agent WHEN to call each command and how to judge the result.
Commands only do mechanical work and write JSON artifacts under <episode>/work/.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .episode import Episode


def _episode(args) -> Episode:
    ep = Episode(args.episode)
    ep.require()
    return ep


def cmd_doctor(args) -> int:
    from . import doctor
    return doctor.run(as_json=args.json)


def cmd_init(args) -> int:
    """Idempotent and non-destructive: safe on a drive folder that already holds footage. Only missing pieces are created."""
    from . import episode as epmod
    ep = Episode(args.episode)
    existed = ep.meta_path.exists()
    choice = args.layout if args.layout != "auto" else epmod.detect_layout(ep.root)
    note = None
    if existed:
        note = "episode.json already exists; it and its layout were left untouched"
    ep.create(title=args.title, layout=None if choice == "flat" else choice)
    out = {"ok": True, "episode": str(ep.root), "created_episode_json": not existed,
           "layout": next((n for n, v in epmod.PRESETS.items() if v == ep.layout), "custom"),
           "raw_dirs": [str(d) for d in ep.raw_dirs], "note": note}
    from . import progress
    out["project_files"] = progress.adopt(ep)
    out["next"] = f"Put footage in {', '.join(str(d) for d in ep.raw_dirs)}, then `ave resume {ep.root}`"
    print(json.dumps(out, indent=2))
    return 0


def cmd_ingest(args) -> int:
    from . import media
    result = media.ingest(_episode(args))
    print(json.dumps(result, indent=2))
    return 0


def cmd_audio(args) -> int:
    from . import audio
    outs = audio.extract_all(_episode(args), force=args.force)
    for o in outs:
        print(o)
    return 0


def cmd_transcribe(args) -> int:
    from . import transcribe
    r = transcribe.transcribe(_episode(args), source=args.source, asr=args.asr, language=args.language,
                              num_speakers=args.speakers, diarize=not args.no_diarize,
                              speakers_from=args.speakers_from, check_diarization=args.check_diarization)
    out = {"source": r["source"], "engine": r["engine"], "speakers": r["speakers"],
           "words": len(r["words"]), "utterances": len(r["utterances"])}
    if r.get("diarization_check"):
        out["diarization_check"] = {k: v for k, v in r["diarization_check"].items() if k != "per_speaker"}
    print(json.dumps(out, indent=2))
    return 0


def cmd_sync(args) -> int:
    from . import sync
    print(json.dumps(sync.sync_episode(_episode(args), reference=args.reference, points=args.points), indent=2))
    return 0


def cmd_plan(args) -> int:
    from . import cutplan, sessions
    ep = _episode(args)

    def summary(r: dict) -> dict:
        out = {"fps": r["fps"], "timeline_seconds": r["timeline_seconds"], "stats": r["stats"], "policy_source": r["policy_source"],
               "first_shots": r["shots"][:5]}
        if r.get("session"):
            out.update(session=r["session"], regions=r["regions"], excluded=r["excluded"], cutlist=sessions.cutlist_name(r["session"]))
        return out
    if args.session is None and sessions.is_multi(ep):      # several sessions: plan every one
        print(json.dumps({"sessions": {sid: summary(cutplan.build_cutlist(ep, session=sid, policy_path=args.policy)) for sid in sessions.session_ids(ep)}}, indent=2))
        return 0
    print(json.dumps(summary(cutplan.build_cutlist(ep, session=args.session, policy_path=args.policy)), indent=2))
    return 0


def cmd_frame(args) -> int:
    from . import stills
    print(stills.extract(_episode(args), args.file, args.time))
    return 0


DEPRECATION = ("DEPRECATED: `ave build-timeline` is now an alias of `ave handoff` (the same Resolve builder, read-back checks included) "
               "and will be removed. Use `ave handoff <episode> [--stage assembly|rough|final] [--label NAME]`.")


def cmd_build(args) -> int:
    """Deprecated alias of `ave handoff --stage assembly`: the cut list IS the assembly edit (assemble.build converts it), so the
    timeline is the one `ave handoff` builds; there is no second Resolve code path any more (ROADMAP W9).
    Legacy flags map over: --name is the timeline label, --dry-run is --dry-run."""
    from argparse import Namespace
    from . import assemble, editdoc
    from .commands import handoff as handoff_cmd
    print(DEPRECATION, file=sys.stderr)
    ep = _episode(args)
    stage = args.stage
    if not editdoc.list_versions(ep, stage):
        if stage != "assembly" or not ep.artifact("cutlist.json").exists():
            raise SystemExit(f"No {stage} edit and no cut list. Run: ave plan-cuts {ep.root} && ave assemble {ep.root}")
        if args.dry_run:
            print("note: no assembly edit yet; `ave assemble` is run for you on a real build (a dry run would have to write one).",
                  file=sys.stderr)
            return 0
        path = editdoc.save_new_version(ep, assemble.build(ep, (1920, 1080)))     # what `ave assemble` does by default
        ep.mark_stage("assembly", edit=path.name)
        print(f"note: made {path.name} from work/cutlist.json (the same step as `ave assemble`).", file=sys.stderr)
    return handoff_cmd.cmd(Namespace(episode=str(ep.root), stage=stage, name=None, label=args.name, no_master=False, project=None,
                                     dry_run=args.dry_run, layers=False))


def cmd_mcp_config(args) -> int:
    from . import config
    py, mcp = config.resolve_mcp_python(), config.resolve_mcp_dir()
    if not (py and mcp):
        raise SystemExit("Resolve MCP server not found. Run scripts/setup.sh")
    api = "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
    cfg = {"mcpServers": {"davinci-resolve": {
        "command": str(py), "args": [str(mcp / "src/server.py")],
        "env": {"DAVINCI_RESOLVE_BRIDGE": "1",  # free edition: the in-app bridge is the only transport
                "RESOLVE_SCRIPT_API": api, "PYTHONPATH": f"{api}/Modules"}}}}
    text = json.dumps(cfg, indent=2)
    if args.write:
        from pathlib import Path
        from . import resources
        # In a checkout the agent is started from the repo, so .mcp.json goes there; otherwise into the current folder.
        target = (resources.checkout_root() if resources.is_checkout() else Path.cwd()) / ".mcp.json"
        target.write_text(text + "\n")
        print(f"wrote {target} (machine-specific paths; do not commit)")
    else:
        print(text)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ave", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("doctor", help="verify this machine is ready")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("init-episode", help="create an episode folder")
    s.add_argument("episode")
    s.add_argument("--title")
    s.add_argument("--layout", choices=["auto", "flat", "numbered"], default="auto",
                   help="auto (default): numbered if 02_FOOTAGE already exists in the dir, else flat. Never moves or overwrites files")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("ingest", help="probe raw footage -> work/media.json")
    s.add_argument("episode")
    s.set_defaults(fn=cmd_ingest)

    s = sub.add_parser("audio", help="extract 16 kHz mono analysis audio")
    s.add_argument("episode")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_audio)

    s = sub.add_parser("transcribe", help="ASR + speaker diarization -> work/transcript.json")
    s.add_argument("episode")
    s.add_argument("--source", help="file to transcribe (default: dedicated audio file, else longest video)")
    s.add_argument("--asr", choices=["fluidaudio", "whisper"], default="fluidaudio")
    s.add_argument("--language", help="language hint, e.g. en")
    s.add_argument("--speakers", type=int, help="exact speaker count if known (improves diarization)")
    s.add_argument("--no-diarize", action="store_true")
    s.add_argument("--speakers-from", choices=["auto", "channels", "diarize"], default="auto",
                   help="speaker labels from the louder mic (needs a speaker_per_channel source) or from the diarizer; auto = channels when the layout allows")
    s.add_argument("--check-diarization", action="store_true",
                   help="channels mode: also run the diarizer and report how well it agrees with the channels (diarization_check)")
    s.set_defaults(fn=cmd_transcribe)

    s = sub.add_parser("sync", help="align cameras by audio -> work/sync.json")
    s.add_argument("episode")
    s.add_argument("--reference", help="file to treat as timeline zero (default: a recorder longer than every camera, else the longest file)")
    s.add_argument("--points", type=int, default=5, help="GCC-PHAT measurement points per file, spread across the take (default 5)")
    s.set_defaults(fn=cmd_sync)

    s = sub.add_parser("plan-cuts", help="shot/reverse-shot cut list -> work/cutlist.json (work/cutlist.<sid>.json per session)")
    s.add_argument("episode")
    s.add_argument("--session", help="plan one session (s1, s2...). Default: every session of a multi-session episode, else the whole episode")
    s.add_argument("--policy", help="planner policy YAML (default: <ep>/policy/cutplan.yaml, ~/.config/ave/cutplan.yaml, config/cutplan.default.yaml)")
    s.set_defaults(fn=cmd_plan)

    s = sub.add_parser("frame", help="extract a still at a time (seconds, in that file's own clock) to look at")
    s.add_argument("episode")
    s.add_argument("file")
    s.add_argument("time", type=float)
    s.set_defaults(fn=cmd_frame)

    s = sub.add_parser("build-timeline", help="DEPRECATED alias of `ave handoff --stage assembly` (builds the cut list as a verified Resolve timeline)")
    s.add_argument("episode")
    s.add_argument("--name", help="timeline label (as `ave handoff --label`)")
    s.add_argument("--stage", choices=["assembly", "rough", "final"], default="assembly",
                   help="default assembly: what the cut list always was. Same choices as `ave handoff --stage`")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_build)

    s = sub.add_parser("mcp-config", help="print (or --write) the MCP server config for any MCP-capable agent")
    s.add_argument("--write", action="store_true")
    s.set_defaults(fn=cmd_mcp_config)

    from .commands import discover
    discover(sub)  # plugin modules in ave/commands add their own subcommands
    return p


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:2] == ["learn", "taste"]:        # `ave learn <episode> "insight"` already owns learn's positionals
        from .commands import learn_taste
        return learn_taste.main(argv[2:])
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
