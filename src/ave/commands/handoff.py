"""`ave handoff`: put the latest edit in DaVinci Resolve for human review and polish."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import re
import shutil

from .. import config, editdoc, handoff, proxies, state
from ..desktop_runtime import frozen
from ..episode import Episode, write_json

ORDER = ("final", "rough", "assembly")


def _pick(ep: Episode, stage: str | None, name: str | None = None, version: str | None = None) -> tuple[str, int, dict, Path]:
    """(stage, number, doc, file) of the edit to hand off: the latest, or `version` (a number or label such as 8 or v8~abcd)
    named on purpose. Both go through editdoc, which refuses a version that has not fully arrived here."""
    for st in ([stage] if stage else ORDER):
        v = editdoc.list_versions(ep, st, name)
        if not v and version is None and editdoc.has_version(ep, st, name):
            editdoc.latest_path(ep, st, name)          # logged, not here yet: refused as arriving, never skipped for the stage below
        if v:
            if version is None:
                p = editdoc.latest_path(ep, st, name)
                return st, v[-1], editdoc.load(p), p
            try:
                p = editdoc.path_for(ep, st, version if not str(version).isdigit() else int(version), name)
            except editdoc.AmbiguousVersion as exc:
                raise SystemExit(str(exc)) from exc
            except ValueError as exc:
                raise SystemExit(f"--version: {exc}") from exc
            if not p.is_file():
                raise SystemExit(f"{st}{'.' + name if name else ''} has no version {version}.")
            return st, int(editdoc.VERSION_RE.match(p.name)["n"]), editdoc.load(p), p
    what = f"{stage}{'.' + name if name else ''}"
    raise SystemExit("No edit to hand off. Run `ave assemble` first." if not stage else f"No {what} edit yet (see `ave status`).")


def _master_path(ep: Episode, stage: str, version) -> Path:
    """work/render/<stage>.<label>.dialogue.wav: `version` is the edit's label (v8, or v8~abcd in an adopted project)."""
    lab = version if isinstance(version, str) else f"v{int(version)}"
    return ep.work / "render" / f"{stage}.{lab}.dialogue.wav"


def _master_audio(ep: Episode, doc: dict, stage: str, version):
    """Compiled dialogue master (crossfades baked in). Needs the compile module; fall back with a warning if absent.

    Reused only while its `.src.sha256` matches this EditDoc, so another computer's v8 never reuses this computer's audio."""
    try:
        from .. import compile as comp
    except Exception:
        return None
    out = _master_path(ep, stage, version)
    key = out.with_suffix(".src.sha256")
    digest = hashlib.sha256(json.dumps(doc, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    try:
        fresh = out.exists() and key.read_text().strip() == digest
    except OSError:
        fresh = False
    if not fresh:
        out.parent.mkdir(parents=True, exist_ok=True)
        comp.compile_audio(ep, doc, out)
        key.write_text(digest + "\n")
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
    from ..project.readiness import choosing
    with choosing("--version", explicit=bool(a.stage or getattr(a, "version", None))):
        stage, version, doc, path = _pick(ep, a.stage, a.name, getattr(a, "version", None))
    lab = editdoc.label_of(path)    # v8, or v8~abcd: names derived from this edit
    layers = bool(getattr(a, "layers", False))
    label = a.label or (f"{stage} {a.name}" if a.name else None)
    name = handoff.timeline_name(ep, doc, lab, label) + (" layers" if layers else "")
    tag = f"{stage}.{a.name}" if a.name else stage
    if a.no_master:
        master = None
    elif layers and a.dry_run:      # a dry run never renders or writes: plan with the path the baked master will have
        master = _master_path(ep, tag, lab)
    else:
        master = _master_audio(ep, doc, tag, lab)
    from ..episode import read_json as _rj
    project = a.project or (_rj(ep.meta_path).get("resolve_project"))
    pmap, pmissing = {}, []
    if getattr(a, "use_proxies", False):
        pmap, pmissing = proxies.proxy_map(ep, doc)
        if not pmap:
            raise SystemExit("--use-proxies: no verified proxies for this edit's footage. Run `ave proxies <episode>` first.")
    srt_copy = ep.captions / (re.sub(r"[\\/:*?\"<>|]+", "-", name) + ".srt")     # the SRT, named after the timeline (W7)
    shipped = ep.exports / f"{tag}.{lab}.srt"      # `ave final` retranscribes the render: what ships is what Resolve should get
    plan = handoff.plan(ep, doc, name=name, audio_master=master, project=project, layers=layers, proxies=pmap, srt=shipped if shipped.exists() else None,
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
    if frozen():    # the standalone app carries the MCP server inside its own interpreter
        cmd_ = [sys.executable, "--resolve-handoff", str(plan_path), "--mcp-src", str(Path(sys._MEIPASS) / "resolve-mcp/src")]
    else:
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
    s.add_argument("--version", help="hand off this version on purpose (a number or label, e.g. 8 or v8~abcd); default: the latest")
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
