"""`ave render`: compile an EditDoc to a verified media file."""

from __future__ import annotations

import json
from pathlib import Path

from .. import editdoc, presets
from ..episode import Episode


def _load(ep: Episode, stage: str, version: int | None, name: str | None = None) -> tuple[dict, Path, str, str | None]:
    """Resolve `--stage` (rough|assembly|final|select:<name>), `--name` (a named edit such as a session id) and `--version` to a doc.
    Default stage: latest of final > rough > assembly."""
    if stage == "auto":
        for st in ("final", "rough", "assembly"):
            if editdoc.list_versions(ep, st, name):
                stage = st
                break
        else:
            from .. import sessions
            raise SystemExit(f"No EditDoc{' ' + name if name else ''} yet. Run: ave assemble <episode>" + sessions.hint(ep, name))
    if stage.startswith("select:"):
        stage, name = "select", stage.split(":", 1)[1]
    if stage not in editdoc.STAGES:
        raise SystemExit(f"--stage must be one of {', '.join(editdoc.STAGES)} (or select:<name>), got {stage!r}")
    versions = editdoc.list_versions(ep, stage, name)
    label = stage + (f":{name}" if name else "")
    if not versions:
        raise SystemExit(f"No {label} edit yet for {ep.root.name}.")
    v = version if version is not None else versions[-1]
    if v not in versions:
        raise SystemExit(f"{label} has no version {v}; available: {versions}")
    p = editdoc.path_for(ep, stage, v, name)
    return editdoc.load(p), p, stage, name


def cmd_render(a) -> int:
    from .. import compile as comp
    ep = Episode(a.episode)
    ep.require()
    doc, path, stage, name = _load(ep, a.stage, a.version, a.name)
    preset = "proxy" if a.proxy else a.preset
    pr = presets.get(preset)
    stem = path.name[:-len(".json")]
    out = Path(a.out) if a.out else ep.exports / f"{stem}.{preset}{pr['ext']}"
    try:
        report = comp.render(ep, doc, out, preset=preset, burn_captions=not a.no_captions, overwrite=a.overwrite)
    except (FileExistsError, FileNotFoundError, ValueError) as e:
        raise SystemExit(str(e)) from e
    except (comp.RenderError, comp.ffutil.FFmpegError) as e:
        raise SystemExit(f"render failed: {e}") from e
    report["edit"] = str(path)
    print(json.dumps(report, indent=2))
    return 0


def register(sub) -> None:
    s = sub.add_parser("render", help="compile an EditDoc to a verified video/audio file (loudness-normalized)")
    s.add_argument("episode")
    s.add_argument("--stage", default="auto", help="rough|assembly|final|select:<name> (default: latest of final, rough, assembly)")
    s.add_argument("--version", type=int, help="edit version (default: latest)")
    s.add_argument("--name", help="named edit, e.g. a session id: renders rough.<name>, final.<name>...")
    s.add_argument("--preset", default="master", choices=sorted(presets.PRESETS))
    s.add_argument("--proxy", action="store_true", help="shorthand for --preset proxy (540p draft)")
    s.add_argument("--out", help="output path (default: exports/<edit>.<preset>.<ext>)")
    s.add_argument("--overwrite", action="store_true")
    s.add_argument("--no-captions", action="store_true", help="do not burn captions even if the doc has an ASS file")
    s.set_defaults(fn=cmd_render)
