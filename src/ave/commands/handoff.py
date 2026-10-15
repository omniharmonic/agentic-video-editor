"""`ave handoff`: put the latest edit in DaVinci Resolve for human review and polish."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

import re
import shutil

from .. import config, editdoc, handoff, proxies, state
from ..episode import Episode, write_json

ORDER = ("final", "rough", "assembly")


def _pick(ep: Episode, stage: str | None, name: str | None = None) -> tuple[str, int, dict]:
    for st in ([stage] if stage else ORDER):
        v = editdoc.list_versions(ep, st, name)
        if v:
            return st, v[-1], editdoc.load(editdoc.path_for(ep, st, v[-1], name))
    what = f"{stage}{'.' + name if name else ''}"
    raise SystemExit("No edit to hand off. Run `ave assemble` first." if not stage else f"No {what} edit yet (see `ave status`).")


def _master_audio(ep: Episode, doc: dict, stage: str, version: int):
    """Compiled dialogue master (crossfades baked in). Needs the compile module; fall back with a warning if absent."""
    try:
        from .. import compile as comp
    except Exception:
        return None
    out = ep.work / "render" / f"{stage}.v{version}.dialogue.wav"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        comp.compile_audio(ep, doc, out)
    return out


def _sum_stamp(ep: Episode, doc: dict, key: str) -> dict:
    """What a sum WAV was built from: the file's size+mtime and its channel_map. Any change rebuilds it."""
    src = doc["sources"][key]
    f = Path(src["file"]) if Path(src["file"]).is_absolute() else ep.root / src["file"]
    st = f.stat()
    cm = hashlib.sha1(json.dumps(src.get("channel_map") or [], sort_keys=True).encode()).hexdigest()
    return {"file": src["file"], "size": st.st_size, "mtime": int(st.st_mtime), "channel_map": cm}


def _ensure_sum_wavs(ep: Episode, doc: dict, plan: dict) -> list[str]:
    """Build (once, then reuse) the dual-mono sum WAV of every source the layered dialogue clips use. Returns what it built."""
    from .. import compile as comp
    keys = sorted({c["src"] for c in plan["layers"]["audio_clips"] if c.get("src")}
                  | ({doc["tracks"]["room_tone"]["src"]} if doc["tracks"].get("room_tone") else set()))
    built = []
    for k in keys:
        out = handoff.sum_wav_path(ep, doc, k)
        stamp_file = out.with_suffix(".json")
        stamp = _sum_stamp(ep, doc, k)
        try:
            fresh = out.exists() and json.loads(stamp_file.read_text()) == stamp
        except Exception:
            fresh = False
        if not fresh:
            comp.compile_source_sum(ep, doc, k, out)
            write_json(stamp_file, stamp)
            built.append(str(out))
    return built


def cmd(a) -> int:
    ep = Episode(a.episode)
    ep.require()
    stage, version, doc = _pick(ep, a.stage, a.name)
    layers = bool(getattr(a, "layers", False))
    label = a.label or (f"{stage} {a.name}" if a.name else None)
    name = handoff.timeline_name(ep, doc, version, label) + (" layers" if layers else "")
    tag = f"{stage}.{a.name}" if a.name else stage
    if a.no_master:
        master = None
    elif layers and a.dry_run:      # a dry run never renders or writes: plan with the path the baked master will have
        master = ep.work / "render" / f"{tag}.v{version}.dialogue.wav"
    else:
        master = _master_audio(ep, doc, tag, version)
    from ..episode import read_json as _rj
    project = a.project or (_rj(ep.meta_path).get("resolve_project"))
    pmap, pmissing = {}, []
    if getattr(a, "use_proxies", False):
        pmap, pmissing = proxies.proxy_map(ep, doc)
        if not pmap:
            raise SystemExit("--use-proxies: no verified proxies for this edit's footage. Run `ave proxies <episode>` first.")
    srt_copy = ep.captions / (re.sub(r"[\\/:*?\"<>|]+", "-", name) + ".srt")     # the SRT, named after the timeline (W7)
    plan = handoff.plan(ep, doc, name=name, audio_master=master, project=project, layers=layers, proxies=pmap,
                        try_subtitles=bool(getattr(a, "try_subtitles", False)), srt_copy=srt_copy)
    if pmissing:
        plan["warnings"].append(f"{len(pmissing)} footage file(s) have no verified proxy and will play from the original: "
                                + ", ".join(Path(f).name for f in pmissing))
    if plan["subtitles"] and not a.dry_run:
        srt_copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(plan["subtitles"]["srt"], srt_copy)
    if layers and a.dry_run:        # nothing under the episode folder: the plan goes to a temp dir
        plan_path = Path(tempfile.mkdtemp(prefix="ave-handoff-")) / "handoff_layers_plan.json"
    else:
        plan_path = ep.work / ("handoff_layers_plan.json" if layers else "handoff_plan.json")
    if layers and not a.dry_run:
        _ensure_sum_wavs(ep, doc, plan)
    write_json(plan_path, plan)
    py, mcp = config.resolve_mcp_python(), config.resolve_mcp_dir()
    if not (py and mcp):
        raise SystemExit("Resolve MCP server not found. Run scripts/setup.sh (see: ave doctor)")
    cmd_ = [str(py), str(config.REPO_ROOT / "scripts/resolve_handoff.py"), str(plan_path), "--mcp-src", str(mcp / "src")]
    if a.dry_run:
        cmd_.append("--dry-run")
    rc = subprocess.run(cmd_).returncode
    if rc == 0 and not a.dry_run:
        write_json(ep.work / ("handoff_layers.json" if layers else "handoff.json"), {"timeline": name, "stage": stage, "version": version})
        if not layers:
            ep.mark_stage("handoff", timeline=name)
        state.set_review(ep, f"review '{name}' in Resolve and tell the agent what to change")
    return rc


def register(sub) -> None:
    s = sub.add_parser("handoff", help="build the latest edit as a NEW timeline in the open Resolve project (verified)")
    s.add_argument("episode")
    s.add_argument("--stage", choices=list(ORDER) + ["select"], help="default: latest of final, rough, assembly")
    s.add_argument("--name", help="named edit within the stage, e.g. `--stage select --name selects` for the A-clips timeline")
    s.add_argument("--label", help="timeline label (default: the stage)")
    s.add_argument("--no-master", action="store_true", help="skip the compiled dialogue master; place cut source audio")
    s.add_argument("--project", help="Resolve project to open (created if missing); default: episode.json `resolve_project`, else the open project")
    s.add_argument("--layers", action="store_true",
                   help="work-in-progress handoff: a SECOND timeline '<title> <stage> <name> vN layers' with the edit on V1, every camera "
                        "in sync on disabled tracks above it, dialogue as separate editable clips (A1) and the baked master muted (A2)")
    s.add_argument("--use-proxies", action="store_true",
                   help="link the review proxies made by `ave proxies` to the imported clips (playback only; renders still use the originals). "
                        "UNVERIFIED in real Resolve; the report says what the bridge read back")
    s.add_argument("--try-subtitles", action="store_true",
                   help="EXPERIMENTAL (UNVERIFIED): try to put the SRT on the timeline via import + AppendToTimeline; "
                        "on any doubt the report prints exact drag-in steps. The SRT is always copied next to the timeline name in captions/")
    s.add_argument("--dry-run", action="store_true", help="plan only; with --layers nothing is rendered or written under the episode")
    s.set_defaults(fn=cmd)
