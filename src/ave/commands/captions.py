"""`ave captions`: generate, check and burn captions; re-transcribe a render.

One command word, three shapes (kept in one parser so `ave captions <episode>` stays the common case):
  ave captions <episode> [--stage rough|final|select:<name>] [--name SESSION] [--style podcast|short|minimal] [--burn [--video f]]
  ave captions check <episode> <file.srt|file.ass> [--style S]
  ave captions retranscribe <episode> <video> [--srt-out path] [--style S]
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import captions, editdoc, sessions
from ..episode import Episode


def _ep(arg: str) -> Episode:
    ep = Episode(arg)
    ep.require()
    return ep


def _split_stage(stage: str) -> tuple[str, str | None]:
    if ":" in stage:
        st, name = stage.split(":", 1)
        return st, name
    return stage, None


def _stage_and_name(ep: Episode, stage_arg: str, name_arg: str | None) -> tuple[str, str | None]:
    """`--name s2` is `--stage rough:s2`. A cut stage (assembly|rough|final) on a multi-session episode needs one or the other:
    the unnamed edits do not exist there and picking a session silently would caption the wrong cut."""
    stage, name = _split_stage(stage_arg)
    if name_arg:
        if name and name != name_arg:
            raise SystemExit(f"--stage {stage_arg} and --name {name_arg} name different edits; use one of them (--stage {stage}:{name_arg} or --stage {stage_arg}).")
        name = name_arg
    if name is None and stage in ("assembly", "rough", "final") and sessions.is_multi(ep):
        ids = sessions.session_ids(ep)
        raise SystemExit(f"This episode has sessions {', '.join(ids)}: pass --name <session> (same as --stage {stage}:<session>), e.g. "
                         f"`ave captions {ep.root} --stage {stage} --name {ids[0]}`.")
    return stage, name


def _generate(a, ep: Episode) -> int:
    stage, name = _stage_and_name(ep, a.stage, a.name)
    video = None
    if a.burn:
        video = a.video or _default_video(ep, stage, name)
    res = captions.make_captions(ep, stage, a.style, name, a.version, a.styles, video)
    if a.burn and not video:
        res["note"] = "burn flag recorded in the edit; no rendered video was given or found, so nothing was burned yet"
    print(json.dumps(res, indent=2))
    return 0


def _default_video(ep: Episode, stage: str, name: str | None) -> Path | None:
    tag = f"{stage}.{name}" if name else stage
    for cand in (ep.exports / f"{tag}.mp4", ep.exports / f"{stage}.mp4"):
        if cand.exists():
            return cand
    return None


def _check(a, ep: Episode) -> int:
    f = Path(a.file)
    cues = captions.parse_ass(f) if f.suffix.lower() == ".ass" else captions.parse_srt(f)
    style = captions.get_style(a.style, ep=ep, path=a.styles)
    problems = captions.check(cues, style)
    print(json.dumps({"file": str(f), "cues": len(cues), "style": a.style, "problems": problems}, indent=2))
    return 1 if problems else 0


def _retranscribe(a, ep: Episode) -> int:
    res = captions.retranscribe(ep, a.file, asr=a.asr, language=a.language, diarize=a.diarize)
    style = captions.get_style(a.style, ep=ep, path=a.styles)
    cues = captions.cues_from_transcript(res, style["rules"], captions.episode_glossary(ep))
    stem = Path(a.file).stem
    srt = Path(a.srt_out) if a.srt_out else ep.captions / f"{stem}.retranscribed.srt"
    captions.write_srt(cues, srt)
    print(json.dumps({"srt": str(srt), "cues": len(cues), "words": len(res["words"]), "engine": res["engine"],
                      "warnings": res["warnings"], "problems": captions.check(cues, style)}, indent=2))
    return 0


def cmd_captions(a) -> int:
    first = a.args[0]
    if first in ("check", "retranscribe"):
        if len(a.args) != 3:
            raise SystemExit(f"usage: ave captions {first} <episode> <{'file.srt|file.ass' if first == 'check' else 'video'}>")
        a.file = a.args[2]
        return (_check if first == "check" else _retranscribe)(a, _ep(a.args[1]))
    if len(a.args) != 1:
        raise SystemExit("usage: ave captions <episode> [--stage ...]   |   ave captions check|retranscribe <episode> <file>")
    return _generate(a, _ep(first))


def register(sub) -> None:
    s = sub.add_parser("captions", help="SRT/ASS captions for an edit (+ `check`, `retranscribe` sub-actions)")
    s.add_argument("args", nargs="+", metavar="[check|retranscribe] episode [file]")
    s.add_argument("--stage", default="rough", help="rough|final|select:<name>|... (default rough)")
    s.add_argument("--name", metavar="SESSION", help="named edit (a session id): same as --stage <stage>:<name>; required for assembly|rough|final on a multi-session episode")
    s.add_argument("--version", type=int, help="edit version to caption (default latest)")
    s.add_argument("--style", default="podcast", help="podcast|short|minimal (see config/captions.styles.yaml)")
    s.add_argument("--styles", help="alternative styles YAML (overrides episode/policy and ~/.config/ave)")
    s.add_argument("--burn", action="store_true", help="also burn into a rendered video and set captions.burn in the new edit")
    s.add_argument("--video", help="rendered video to burn into (default exports/<stage>.mp4)")
    s.add_argument("--srt-out", help="retranscribe: where to write the SRT")
    s.add_argument("--asr", default="fluidaudio", choices=["fluidaudio", "whisper"], help="retranscribe engine")
    s.add_argument("--language")
    s.add_argument("--diarize", action="store_true", help="retranscribe: also label speakers (FluidAudio)")
    s.set_defaults(fn=cmd_captions)
