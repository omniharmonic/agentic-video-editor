"""What the Room's Cut view needs from disk: which episode folder an artifact is, a version's segments and alternates, the renders
that can play it, and the fast preview of an offered version.

Until the artifact index arrives (project log, Task 7), an artifact on stage names an episode folder: the project folder itself when
it holds episode.json, otherwise a folder directly inside it. Times are cut (output) seconds throughout.
"""

from __future__ import annotations

import re
from pathlib import Path

from .. import editdoc
from ..episode import Episode, read_json

VERSION_RE = re.compile(r"^(?P<stage>[a-z]+)(?:\.(?P<name>[A-Za-z0-9_-]+))?\.v(?P<n>\d+)(?:~(?P<device>[a-z0-9]{4}))?$")
ARTIFACT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,99}$")
FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.~-]{0,199}\.(mp4|mov|m4v|webm)$")
AREAS = ("exports", "preview")
# The five stages every artifact shares, and where each kind of EditDoc sits among them.
STAGES = ("Plan", "Draft", "Refine", "Finish", "Out")
DOC_STAGE = {"select": "Plan", "assembly": "Draft", "rough": "Draft", "short": "Draft", "final": "Finish"}


class NotFound(LookupError):
    pass


def find_episode(root: Path, artifact: str | None) -> Episode:
    root = Path(root)
    if (root / "episode.json").is_file():
        return Episode(root)
    if isinstance(artifact, str) and ARTIFACT_RE.fullmatch(artifact) and ".." not in artifact:
        d = root / artifact
        if (d / "episode.json").is_file() and d.resolve().parent == root.resolve():
            return Episode(d)
    raise NotFound(f"No episode folder for {artifact!r} in this project")


def version_file(ep: Episode, version: str) -> Path:
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise ValueError(f"{version!r} is not a version name like rough.v3")
    f = ep.edits / f"{version}.json"
    if not f.is_file():
        raise NotFound(f"Version {version} is not in this episode")
    return f


def load_version(ep: Episode, version: str) -> dict:
    return editdoc.load(version_file(ep, version))


def media_url(artifact: str, area: str, name: str) -> str:
    from urllib.parse import quote
    return f"/api/room/media/{quote(artifact, safe='')}/{area}/{quote(name, safe='')}"


def media_path(ep: Episode, area: str, name: str) -> Path:
    if area not in AREAS or not FILE_RE.fullmatch(name or ""):
        raise NotFound("not found")
    base = ep.exports if area == "exports" else ep.work / "preview"
    f = base / name
    if not f.is_file():
        raise NotFound("not found")
    return f


def full_render(ep: Episode, version: str) -> Path | None:
    """The newest finished render of exactly this version in exports/ (`<version>.<preset>.mp4` or `<version>.mp4`)."""
    if not ep.exports.is_dir():
        return None
    found = [p for p in ep.exports.iterdir() if p.is_file() and not p.name.startswith(".") and p.suffix == ".mp4"
             and (p.stem == version or (p.stem.startswith(version + ".") and "." not in p.stem[len(version) + 1:]))]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def _meta(ep: Episode) -> dict:
    try:
        meta = read_json(ep.meta_path)
    except (OSError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def title_of(ep: Episode, artifact: str) -> str:
    return _meta(ep).get("title") or artifact


def _camera_names(ep: Episode, doc: dict) -> dict[str, str]:
    """Source key -> the person (or role) it films, from episode.json: "Host". A camera nobody is mapped to is "Camera A"."""
    meta = _meta(ep)
    by_file = {}
    speakers = meta.get("speakers") or {}
    for cam, info in (meta.get("cameras") or {}).items():
        who = next((s.get("name") for s in speakers.values() if s.get("camera") == cam and s.get("name")), None)
        by_file[str(info.get("file"))] = who or (str(info.get("role") or "").capitalize() or f"Camera {cam}")
    out = {}
    for i, (k, s) in enumerate((doc.get("sources") or {}).items()):
        f = str(s.get("file", ""))
        out[k] = by_file.get(f) or by_file.get(Path(f).name) or (f"Camera {k}" if len(k) <= 3 else f"Camera {i + 1}")
    return out


def _source_label(name: str, kind: str) -> str:
    """What the strip calls a source: "Host camera", "Camera B", "Audio"."""
    if kind != "video":
        return "Audio" if name.startswith("Camera") else f"{name} audio"
    return name if name.startswith("Camera") else f"{name} camera"


def _output_label(version: str, name: str) -> str:
    """"Draft of version 2" for exports/rough.v2.proxy.mp4, "Master of version 2" for .master, else "Render of version 2"."""
    n = VERSION_RE.fullmatch(version).group("n")
    preset = name[len(version) + 1:].rsplit(".", 1)[0] if name.startswith(version + ".") else ""
    kind = {"proxy": "Draft", "master": "Master"}.get(preset.split(".")[0], "Render")
    return f"{kind} of version {n}"


def _covers(src: dict, e0: float, e1: float) -> bool:
    """Whether a source has footage for the whole episode-time stretch [e0, e1] (from its offset and probed duration)."""
    start = float(src.get("start_offset", 0.0) or 0.0)
    dur = src.get("duration")
    return e0 >= start - 1e-3 and (dur is None or e1 <= start + float(dur) + 1e-3)


def cut_info(ep: Episode, artifact: str, version: str) -> dict:
    """The Cut view's data for one version: segments with the angles that could replace each, the renders, sources and outputs."""
    doc = load_version(ep, version)
    names = _camera_names(ep, doc)
    sources = doc.get("sources") or {}
    video_keys = [k for k, s in sources.items() if s.get("kind", "video") == "video"]
    segments = []
    for seg in (doc.get("tracks") or {}).get("video") or []:
        e0 = float(seg.get("e0", seg["o0"]))
        e1 = e0 + (seg["o1"] - seg["o0"])
        alts = [k for k in video_keys if k != seg.get("src") and _covers(sources[k], e0, e1)]
        segments.append({"id": seg.get("id"), "o0": seg["o0"], "o1": seg["o1"], "src": seg.get("src"),
                         "label": names.get(seg.get("src"), seg.get("src")), "alternates": alts})
    render = full_render(ep, version)
    m = VERSION_RE.fullmatch(version)
    outputs = sorted((p for p in ep.exports.iterdir() if p.is_file() and not p.name.startswith(".") and p.suffix in (".mp4", ".mov", ".wav", ".m4a")
                      and (p.stem == version or p.name.startswith(version + "."))), key=lambda p: p.name) if ep.exports.is_dir() else []
    title = _meta(ep).get("title") or artifact
    return {
        "artifact": artifact, "title": title, "version": version, "duration": float(doc.get("duration", 0.0)),
        "stage": DOC_STAGE.get(m.group("stage"), "Draft"), "stages": list(STAGES),
        "lanes": [{"src": k, "label": _source_label(names.get(k, k), "video")} for k in video_keys],
        "segments": segments,
        "media": {"url": media_url(artifact, "exports", render.name), "start": 0.0, "end": float(doc.get("duration", 0.0)),
                  "kind": "full"} if render else None,
        "sources": [{"name": Path(str(s.get("file", k))).name, "kind": s.get("kind", "video"),
                     "label": _source_label(names.get(k, k), s.get("kind", "video"))} for k, s in sources.items()],
        "outputs": [{"name": p.name, "label": _output_label(version, p.name)} for p in outputs],     # only this version's
        "versions": sorted((p.stem for p in ep.edits.glob("*.json") if VERSION_RE.fullmatch(p.stem)), key=_version_order)
        if ep.edits.is_dir() else [],
        "trim": _trim_of(ep, m.group("name") or "main"),
    }


def _trim_of(ep: Episode, session: str) -> dict | None:
    """The trim recorded for this version's session, for the strip: its ends, whose it is, and the project the app's sheet names."""
    rec = (_meta(ep).get("trim") or {}).get(session)
    if not isinstance(rec, dict) or not (ep.root / ".ave" / "project.json").is_file():
        return None
    from ..project import Project
    try:
        pid = Project.open(ep.root).project_id
    except (OSError, ValueError):
        return None
    return {"session": session, "in": rec.get("in"), "out": rec.get("out"), "project": pid,
            "mine": rec.get("by") == "human" and not rec.get("draft_only")}


def _version_order(v: str):
    m = VERSION_RE.fullmatch(v)
    return (m.group("stage"), m.group("name") or "", int(m.group("n")), m.group("device") or "")


def preview_offer(ep: Episode, artifact: str, version: str, compare_to: str | None, span: list | None) -> dict:
    """Render the changed part of an offered version and say how it went, as the `preview` event's data:
    status ready (url, start, end, seek, span, level_approximate), too_large (span), unchanged, or failed (message)."""
    from .. import preview
    base = {"artifact": artifact, "version": version}
    try:
        after = load_version(ep, version)
        before = load_version(ep, compare_to) if compare_to else None
        if span is None:
            if before is None:
                return {**base, "status": "failed", "message": "Nothing to compare with: offer it with the version it replaces"}
            spans = preview.changed_span(before, after)
            if not spans:
                return {**base, "status": "unchanged"}
            span = list(spans[0])
        try:
            got = preview.render_section(ep, after, (float(span[0]), float(span[1])), parent=before)
        except ValueError as e:
            if "wider than" in str(e):
                return {**base, "status": "too_large", "span": [float(span[0]), float(span[1])]}
            raise
    except NotFound as e:
        return {**base, "status": "failed", "message": str(e)}
    except Exception as e:                              # a broken doc or ffmpeg: say so on the offer, never leave it spinning
        return {**base, "status": "failed", "message": f"The preview could not be rendered: {e}"[:400]}
    path = Path(got["path"])
    return {**base, "status": "ready", "url": media_url(artifact, "preview", path.name), "start": got["start"], "end": got["end"],
            "seek": got["seek"], "span": [float(got["span"][0]), float(got["span"][1])],
            "level_approximate": bool(got["level_approximate"])}


def render_full(ep: Episode, artifact: str, version: str) -> dict:
    """Render the whole version as a proxy draft into exports/ (as `ave render --proxy` would) and say where it plays."""
    from .. import presets
    from ..compile import render
    existing = full_render(ep, version)
    doc = load_version(ep, version)
    if existing is None:
        out = ep.exports / f"{version}.proxy{presets.get('proxy')['ext']}"
        ep.exports.mkdir(parents=True, exist_ok=True)
        render(ep, doc, out, preset="proxy", burn_captions=False)
        existing = out
    return {"artifact": artifact, "version": version, "status": "ready", "url": media_url(artifact, "exports", existing.name),
            "start": 0.0, "end": float(doc.get("duration", 0.0)), "kind": "full"}
