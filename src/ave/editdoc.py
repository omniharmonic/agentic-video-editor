"""EditDoc v1: the one contract every stage reads and writes. See docs/superpowers/specs/.

All track times are OUTPUT time (seconds from the start of the cut). `e0` is EPISODE time (the reference
timeline; see AGENTS.md). A source file's own clock for episode time e is  (e - start_offset) / (1 + drift_ppm * 1e-6)
(`source_time`: the ONE place this is computed). `drift_ppm` > 0 means the file's clock runs slow against the reference.
A removed section is simply a jump in `e0` between consecutive segments; J/L cuts are video and
dialogue boundaries that differ.
"""

from __future__ import annotations
from .atomic import publish as publish_atomic

import bisect
import copy
import contextlib
import fcntl
import hashlib
import json
import math
import os
import stat
import tempfile
import re
from pathlib import Path
from typing import Iterable

import numpy as np

from .episode import Episode, read_json, write_json

VERSION = 1
STAGES = ("assembly", "rough", "final", "select", "short")
EPS = 1e-3          # 1 ms tolerance for contiguity checks
KINDS = ("video", "audio")


def new_doc(stage: str, fps: float, canvas: tuple[int, int], sources: dict, duration: float = 0.0, *, media_mode: str | None = None) -> dict:
    if stage not in STAGES:
        raise ValueError(f"stage must be one of {STAGES}")
    return {
        "version": VERSION, "stage": stage, "fps": float(fps),
        **({"media_mode": {"version": 1, "kind": media_mode}} if media_mode is not None else {}),
        "canvas": {"w": int(canvas[0]), "h": int(canvas[1])},
        "sources": sources,
        "tracks": {"video": [], "dialogue": [], "room_tone": None, "music": [], "overlays": []},
        "captions": {"srt": None, "ass": None, "burn": False},
        "duration": float(duration), "notes": "",
    }


def effective_mode(doc: dict) -> str:
    mode = doc.get("media_mode", {"version": 1, "kind": "audiovisual"})
    if not isinstance(mode, dict) or set(mode) != {"version", "kind"} or type(mode["version"]) is not int or mode["version"] != 1 or mode["kind"] not in ("audiovisual", "audio", "silent_video"):
        raise ValueError("Unsupported EditDoc media mode or version")
    return mode["kind"]


def require_monotonic(doc: dict, action: str):
    if doc.get("independent_project"):
        raise ValueError(f"{action} is not supported for independent recording projects. Use the Media editor; episode adaptation is pending.")


def validate(doc: dict) -> list[str]:
    """Return a list of problems (empty means valid). Never raises, so callers can show every issue."""
    p: list[str] = []
    try: mode = effective_mode(doc)
    except ValueError as exc: p.append(str(exc)); mode = "audiovisual"
    def finite(value): return type(value) in (int, float) and math.isfinite(value)
    for key in ("duration", "fps"):
        if not finite(doc.get(key)) or doc[key] < 0: p.append(f"{key} must be finite and nonnegative")
    if doc.get("version") != VERSION:
        p.append(f"version must be {VERSION}")
    if doc.get("stage") not in STAGES:
        p.append(f"stage must be one of {STAGES}")
    if not doc.get("fps") or doc["fps"] <= 0:
        p.append("fps must be > 0")
    cv = doc.get("canvas") or {}
    if not (cv.get("w") and cv.get("h")):
        p.append("canvas needs w and h")
    src = doc.get("sources") or {}
    for k, s in src.items():
        if s.get("kind") not in KINDS:
            p.append(f"source {k}: kind must be one of {KINDS}")
        if not s.get("file"):
            p.append(f"source {k}: file is required")
        if not finite(s.get("start_offset")):
            p.append(f"source {k}: start_offset must be finite")
        d = s.get("drift_ppm", 0)
        if not finite(d) or abs(d) > 5000:
            p.append(f"source {k}: drift_ppm must be a number within +-5000 (got {d!r})")
        sm = s.get("sync_map")
        if sm is not None and (not isinstance(sm, list) or any(not isinstance(x, (list, tuple)) or len(x) != 2 for x in sm)):
            p.append(f"source {k}: sync_map must be a list of [file_t, offset] pairs")
        if sm is not None and isinstance(sm,list) and all(isinstance(x,(list,tuple)) and len(x)==2 for x in sm):
            if any(not all(finite(y) for y in x) for x in sm): p.append(f"source {k}: sync_map values must be finite")
            elif any(b[0]<=a[0] or sum(b)<=sum(a) for a,b in zip(sm,sm[1:])): p.append(f"source {k}: sync_map clocks must increase")
        for m in s.get("channel_map") or []:
            if m.get("use") not in ("left", "right", "mix", "sum") or not m["end"] > m["start"] >= 0:
                p.append(f"source {k}: bad channel_map range {m!r} (need start < end and use left|right|mix|sum)")
    tr = doc.get("tracks") or {}
    dur = doc.get("duration", 0.0)
    for name in ("video", "dialogue"):
        segs = tr.get(name) or []
        if name == "video" and mode == "audio":
            if segs: p.append("Audio edit cannot contain picture")
        else:
            p += _check_track(name, segs, src, dur, required_kind="video" if name == "video" else None)
        for segment in segs:
            if any(not finite(segment.get(k)) for k in ("o0", "o1", "e0")): p.append(f"{name}: times must be finite numbers")
            if name == "dialogue" and segment.get("fill") not in (None, "silence", "room_tone", "muted"): p.append("Unknown dialogue fill")
            if name == "dialogue" and mode == "silent_video" and segment.get("fill") != "silence": p.append("Silent video requires explicit silence fills")
    if mode == "silent_video" and (tr.get("music") or any(o.get("audio") == "keep" for o in tr.get("overlays") or [])):
        p.append("Silent video cannot contain audible music or B-roll")
    if mode == "audio" and (tr.get("overlays") or (doc.get("captions") or {}).get("burn")):
        p.append("Audio edit cannot contain visual overlays or burned captions")
    for name in ("music", "overlays"):
        for s in tr.get(name) or []:
            if s["o1"] <= s["o0"]:
                p.append(f"{name} {s.get('id')}: o1 must be > o0")
            if s["o1"] > dur + EPS:
                p.append(f"{name} {s.get('id')}: ends after duration")
    ids = [s.get("id") for n in ("video", "dialogue", "music", "overlays") for s in (tr.get(n) or [])]
    dup = {i for i in ids if i is not None and ids.count(i) > 1}
    if dup:
        p.append(f"duplicate segment ids: {sorted(dup)}")
    return p


def _check_track(name: str, segs: list[dict], src: dict, dur: float, required_kind: str | None) -> list[str]:
    p: list[str] = []
    if not segs:
        return [f"{name}: track is empty"] if dur > 0 else []
    for s in segs:
        sid = s.get("id", "?")
        if s.get("src") not in src:
            p.append(f"{name} {sid}: unknown source {s.get('src')!r}")
        elif required_kind and src[s["src"]].get("kind") != required_kind:
            p.append(f"{name} {sid}: source {s['src']} is not {required_kind}")
        if s["o1"] - s["o0"] <= 0:
            p.append(f"{name} {sid}: empty or negative length")
        if "e0" not in s:
            p.append(f"{name} {sid}: e0 is required")
    ordered = sorted(segs, key=lambda s: s["o0"])
    if abs(ordered[0]["o0"]) > EPS:
        p.append(f"{name}: starts at {ordered[0]['o0']:.3f}s, must start at 0")
    for a, b in zip(ordered, ordered[1:]):
        gap = b["o0"] - a["o1"]
        if gap > EPS:
            p.append(f"{name}: gap of {gap:.3f}s after {a.get('id')}")
        elif gap < -EPS - (b.get("xfade_ms", 0) / 1000.0):
            p.append(f"{name}: overlap of {-gap:.3f}s between {a.get('id')} and {b.get('id')}")
    if abs(ordered[-1]["o1"] - dur) > EPS:
        p.append(f"{name}: ends at {ordered[-1]['o1']:.3f}s but duration is {dur:.3f}s")
    return p


def drift_of(src: dict) -> float:
    """A source's clock drift as a fraction (80 ppm -> 8e-5). Absent = 0, so every legacy doc keeps working."""
    return float(src.get("drift_ppm") or 0.0) * 1e-6


def _knots(src: dict) -> tuple[list[float], list[float]] | None:
    """(file_t[], episode_t[]) of a source's piecewise sync map, or None for a plain offset + drift source.

    `sync_map` is [[file_t, offset], ...] (offset = where the file's moment file_t sits on the reference, minus file_t), as
    measured by `ave sync` when one straight line leaves more than 20 ms residual. Needs >= 2 points; knots are sorted by
    file time and episode time is forced non-decreasing so the inverse always exists."""
    m = src.get("sync_map")
    if not m or len(m) < 2:
        return None
    pts = sorted((float(t), float(t) + float(o)) for t, o in m)
    ft, et = [pts[0][0]], [pts[0][1]]
    for t, e in pts[1:]:
        if t > ft[-1] + 1e-9:
            ft.append(t)
            et.append(max(e, et[-1]))
    return (ft, et) if len(ft) >= 2 else None


def _interp(x, xs: list[float], ys: list[float], slope_lo: float, slope_hi: float):
    """Piecewise-linear xs -> ys, extrapolating beyond the ends with the given slopes (the line, not a clamp)."""
    xa = np.asarray(x, dtype=float)
    y = np.interp(xa, xs, ys)
    y = np.where(xa < xs[0], ys[0] + (xa - xs[0]) * slope_lo, y)
    y = np.where(xa > xs[-1], ys[-1] + (xa - xs[-1]) * slope_hi, y)
    return float(y) if np.ndim(x) == 0 else y


def src_time(src: dict, episode_t):
    """Episode time -> that source file's own clock:  (e - start_offset) / (1 + drift), or, when the source carries a
    `sync_map` (piecewise-linear drift), interpolation through the measured points. THE one place this is computed.

    The camera's clock runs slow by `drift` against the reference (field note N3: 85 ppm = 0.6 s over a 2 h take), so
    file time advances by 1/(1+drift) per episode second. Takes the source dict so code without a whole doc can use it.
    Accepts a float or a numpy array."""
    kn = _knots(src)
    if kn is None:
        return (episode_t - float(src["start_offset"])) / (1.0 + drift_of(src))
    ft, et = kn
    k = 1.0 + drift_of(src)
    return _interp(episode_t, et, ft, 1.0 / k, 1.0 / k)


def episode_time(src: dict, file_t):
    """Inverse of `src_time`: a source file's own time -> episode time."""
    kn = _knots(src)
    if kn is None:
        return float(src["start_offset"]) + file_t * (1.0 + drift_of(src))
    ft, et = kn
    k = 1.0 + drift_of(src)
    return _interp(file_t, ft, et, k, k)


def clock_boundaries(src: dict, e0: float, e1: float) -> list[float]:
    """Interior episode-clock knots where the local mapping may change slope."""
    knots = _knots(src)
    return [e for e in knots[1] if e0 < e < e1] if knots else []


def clock_rate(src: dict, episode_t: float) -> float:
    """Episode seconds per file second at `episode_t` (1 + drift for a plain source; the local segment slope for a piecewise
    one). Consumers that stretch media to follow the source clock use this, never drift_of directly."""
    kn = _knots(src)
    if kn is None:
        return 1.0 + drift_of(src)
    ft, et = kn
    if episode_t < et[0] or episode_t >= et[-1]:
        return 1.0 + drift_of(src)
    i = int(np.searchsorted(et, episode_t, side="right")) - 1
    i = min(max(i, 0), len(ft) - 2)
    dt = ft[i + 1] - ft[i]
    r = (et[i + 1] - et[i]) / dt if dt > 0 else 1.0 + drift_of(src)
    return r if r > 0 else 1.0 + drift_of(src)


def source_time(doc: dict, src: str, episode_t: float) -> float:
    """Episode time -> that source file's own clock (see `src_time`)."""
    return src_time(doc["sources"][src], episode_t)


class TimeMap:
    """Maps between episode time and output time for a list of kept episode ranges.

    keeps: [(e0, e1), ...] in episode time, in order, non-overlapping. Output time is their concatenation.
    """

    def __init__(self, keeps: Iterable[tuple[float, float]]):
        self.keeps = [(float(a), float(b)) for a, b in keeps if b - a > 0]
        self.o_starts: list[float] = []
        acc = 0.0
        for a, b in self.keeps:
            self.o_starts.append(acc)
            acc += b - a
        self.duration = acc
        self._e_starts = [a for a, _ in self.keeps]

    def to_output(self, e: float) -> float | None:
        """Output time for episode time e, or None if e falls in a removed section."""
        i = bisect.bisect_right(self._e_starts, e) - 1
        if i < 0:
            return None
        a, b = self.keeps[i]
        return self.o_starts[i] + (e - a) if e <= b + EPS else None

    def to_output_snap(self, e: float) -> float:
        """Like to_output, but a point inside a removed section snaps to where the cut lands."""
        i = bisect.bisect_right(self._e_starts, e) - 1
        if i < 0:
            return 0.0
        a, b = self.keeps[i]
        return self.o_starts[i] + min(e, b) - a

    def to_episode(self, o: float) -> float:
        i = bisect.bisect_right(self.o_starts, o) - 1
        i = max(i, 0)
        return self.keeps[i][0] + (o - self.o_starts[i])


# ---- persistence: edits/<stage>[.name].vN[~tag].json ------------------------------------------
#
# A version file is `<stage>[.name].v<N>.json` (legacy, and every un-adopted folder) or `<stage>[.name].v<N>~<tag>.json`, where
# tag is the 4-character display tag of the computer that wrote it. Two computers that each make v8 while apart write
# `rough.v8~abcd.json` and `rough.v8~efgh.json`, so nothing collides. N is the next number the writing computer saw.
#
# Order: in an adopted project (`.ave/project.json` exists) a version's place comes from the stamp of its `version.added` log
# event; files the log does not know yet (legacy files, or ones that arrived before their event) fall back to (N, tag).

VERSION_RE = re.compile(r"^(?P<stage>[a-z]+)(?:\.(?P<name>[\w-]+))?\.v(?P<n>\d+)(?:~(?P<device>[a-z0-9]{4}))?\.json$", re.ASCII)
_V = VERSION_RE  # older callers match file names with editdoc._V


class Version(int):
    """One saved edit version. It IS its number N (compares, adds and prints as N, so code written for plain ints keeps
    working); `.tag` is the device tag (None for a legacy name), `.path` the file, `.stamp` the hlc of its log event if any."""

    def __new__(cls, n: int, tag: str | None = None, path: Path | None = None, stamp: str | None = None):
        self = super().__new__(cls, n)
        self.tag, self.path, self.stamp = tag, path, stamp
        return self

    @property
    def id(self) -> str:
        """The version's name: the file name without `.json` (e.g. `rough.s2.v8~abcd`)."""
        return self.path.name[:-len(".json")] if self.path else str(int(self))


def edits_dir(ep: Episode) -> Path:
    return ep.edits


def is_adopted(root: Path) -> bool:
    return (Path(root) / ".ave" / "project.json").is_file()


_LOG_CACHE: dict[str, tuple[tuple, dict]] = {}


def logged_versions(root: Path) -> dict[str, tuple]:
    """{project-relative file: (stamp, event id, hlc text)} of every `version.added` event in an adopted project's logs, the
    earliest event per file. Cached on the log files' size and mtime, so repeated calls cost a few stats."""
    root = Path(root)
    if not is_adopted(root):
        return {}
    from .project import hlc as _hlc
    from .project.log import log_files, read_log
    files = log_files(root / ".ave" / "log")
    key = []
    for p in files:
        try:
            st = p.stat()
        except OSError:
            continue
        key.append((p.name, st.st_ino, st.st_size, st.st_mtime_ns))
    cached = _LOG_CACHE.get(str(root))
    if cached and cached[0] == tuple(key):
        return cached[1]
    out: dict[str, tuple] = {}
    for p in files:
        for e in read_log(p):
            data = e.get("data") or {}
            if e.get("op") != "version.added" or not isinstance(data.get("file"), str):
                continue
            k = (_hlc.parse(e["hlc"]), e["id"], e["hlc"])
            if data["file"] not in out or k[:2] < out[data["file"]][:2]:
                out[data["file"]] = k
    _LOG_CACHE[str(root)] = (tuple(key), out)
    return out


def _file_key(v: Version) -> tuple:
    return (int(v), v.tag or "")


def _ordered(ep: Episode, entries: list[Version]) -> list[Version]:
    """Logged versions in stamp order, merged with unlogged ones by (N, tag)."""
    stamps = logged_versions(ep.root)
    logged, rest = [], []
    for v in entries:
        try:
            rel = v.path.relative_to(ep.root).as_posix()
        except ValueError:
            rel = None
        k = stamps.get(rel) if rel else None
        if k:
            v.stamp = k[2]
            logged.append((k[:2], v))
        else:
            rest.append(v)
    logged_sorted = [v for _, v in sorted(logged, key=lambda kv: kv[0])]
    rest.sort(key=_file_key)
    out, i, j = [], 0, 0
    while i < len(logged_sorted) or j < len(rest):
        if j >= len(rest) or (i < len(logged_sorted) and _file_key(logged_sorted[i]) <= _file_key(rest[j])):
            out.append(logged_sorted[i]); i += 1
        else:
            out.append(rest[j]); j += 1
    return out


def version_entries(ep: Episode, stage: str, name: str | None = None) -> list[Version]:
    """Every saved version of one edit as a `Version` (number, device tag, file, log stamp), oldest first; the last is the one
    this computer considers latest."""
    out = []
    for f in edits_dir(ep).glob(f"{stage}*.json") if edits_dir(ep).is_dir() else []:
        m = VERSION_RE.match(f.name)
        if m and m["stage"] == stage and (m["name"] or None) == name:
            _safe_components(f)
            if not f.is_file(): raise ValueError("Edit versions must be regular files")
            out.append(Version(int(m["n"]), m["device"], f))
    return _ordered(ep, out)


def list_versions(ep: Episode, stage: str, name: str | None = None) -> list[int]:
    """The version numbers in `version_entries` order, as plain ints (callers check `type(v) is int`). Two computers' v8
    appear as two 8s; `path_for(ep, stage, 8)` then names the later one, and `version_entries` tells them apart."""
    return [int(v) for v in version_entries(ep, stage, name)]


def version_name(stage: str, version: int, name: str | None = None, tag: str | None = None) -> str:
    mid = f".{name}" if name else ""
    return f"{stage}{mid}.v{int(version)}{'~' + tag if tag else ''}.json"


class AmbiguousVersion(ValueError):
    """A bare version number matches more than one file (two computers each made that number): name it by its id."""
    code = "edit.version_ambiguous"


_LABEL_RE = re.compile(r"^v(?P<n>\d+)(?:~(?P<device>[a-z0-9]{4}))?$")


def label(version, tag: str | None = None) -> str:
    """`v8~abcd` for a device-tagged version, `v8` for a legacy one. Use it, never f"v{n}", for any name derived from a version
    (exports, captions, cached pictures), so two computers' v8 never share a derived file."""
    if isinstance(version, Version):
        tag = version.tag
    return f"v{int(version)}" + (f"~{tag}" if tag else "")


def label_of(path: Path | str) -> str:
    """The label of a version file (`rough.s2.v8~abcd.json` -> `v8~abcd`)."""
    m = VERSION_RE.match(Path(path).name)
    if not m:
        raise ValueError(f"{Path(path).name} is not an edit version file")
    return label(int(m["n"]), m["device"])


def device_tag(root: Path) -> str | None:
    """This computer's tag when `root` is an adopted project, else None (un-adopted folders keep legacy names)."""
    if not is_adopted(root):
        return None
    from .project.model import Device
    return Device.load().short


def _next_number(versions: list) -> int:
    return (max(int(v) for v in versions) if versions else 0) + 1


def next_version(ep: Episode, stage: str, name: str | None = None) -> Version:
    """The version `save_new_version` would write next on this computer (same number, same tag)."""
    tag = device_tag(ep.root)
    n = _next_number(version_entries(ep, stage, name))
    return Version(n, tag, edits_dir(ep) / version_name(stage, n, name, tag))


def next_label(ep: Episode, stage: str, name: str | None = None) -> str:
    return label(next_version(ep, stage, name))


def latest_version(ep: Episode, stage: str, name: str | None = None) -> Version | None:
    v = version_entries(ep, stage, name)
    return v[-1] if v else None


def path_of_id(ep: Episode, version_id: str) -> Path:
    """The file of a version id (`rough.s2.v8~abcd`); refuses anything that is not a version name."""
    if not isinstance(version_id, str) or not VERSION_RE.match(version_id + ".json"):
        raise ValueError(f"not an edit version id: {version_id!r}")
    return edits_dir(ep) / f"{version_id}.json"


def path_for(ep: Episode, stage: str, version, name: str | None = None) -> Path:
    """The file of one version. `version` may be a `Version` (names its own file), a label (`v8~abcd`), or a bare number. A
    bare number names its only file, or the legacy name when there is none (for writers); when two computers each made that
    number it raises AmbiguousVersion. Use latest_path for "the latest"."""
    if isinstance(version, Version) and version.path is not None:
        return version.path
    if isinstance(version, str):
        m = _LABEL_RE.match(version)
        if not m:
            raise ValueError(f"not a version label: {version!r}")
        if m["device"]:
            return edits_dir(ep) / version_name(stage, int(m["n"]), name, m["device"])
        version = int(m["n"])
    legacy = edits_dir(ep) / version_name(stage, version, name)
    if not edits_dir(ep).is_dir() or not any(edits_dir(ep).glob(legacy.name[:-len(".json")] + "~*.json")):
        return legacy
    same = [v for v in version_entries(ep, stage, name) if int(v) == int(version)]
    if len(same) > 1:
        raise AmbiguousVersion(f"{stage}{'.' + name if name else ''} v{int(version)} was made on more than one computer. "
                               f"Choose one: {', '.join(label(v) for v in same)} (version ids {', '.join(v.id for v in same)})")
    return same[0].path if same else legacy


def resolve(ep: Episode, stage: str, version=None, name: str | None = None, *, id: str | None = None,
            sha256: str | None = None) -> Path:
    """A stored reference to a version: its id when the reference has one (preferred), else the number. A number two
    computers both made is told apart by `sha256` when the reference records one (studio identities do), else it raises
    AmbiguousVersion. No version at all means the latest."""
    if id:
        return path_of_id(ep, id)
    if sha256 and version is not None and not isinstance(version, Version):
        same = [v for v in version_entries(ep, stage, name) if int(v) == int(_LABEL_RE.match(version)["n"] if isinstance(version, str) and _LABEL_RE.match(version) else version)]
        if len(same) > 1:
            hit = [v for v in same if hashlib.sha256(v.path.read_bytes()).hexdigest() == sha256]
            if hit:          # identical bytes on both computers: either file is the same edit
                return hit[0].path
    if version is None:
        p = latest_path(ep, stage, name)
        if p is None:
            raise ValueError(f"no {stage} edit yet")
        return p
    return path_for(ep, stage, version, name)


def has_version(ep: Episode, stage: str, name: str | None = None) -> bool:
    """True when this edit (stage, name) has a version: a file here, or (in an adopted project) one in the log that may still be
    arriving. Commands choose their stage with this, so a later stage that is on its way is never skipped for an earlier one,
    and `latest_path` then says it is arriving instead of "no edit yet"."""
    if version_entries(ep, stage, name):
        return True
    if not is_adopted(ep.root):
        return False
    from .project.readiness import newest_logged
    return newest_logged(ep.root, stage, name) is not None


def latest_path(ep: Episode, stage: str, name: str | None = None) -> Path | None:
    """The newest version's file. In an adopted project this is also the readiness choke point: when the log's newest version
    of this edit has not fully arrived here (or arrived damaged) it raises readiness.NotReady, naming the version a person can
    ask for on purpose instead, so "the latest" never silently means the one before."""
    v = version_entries(ep, stage, name)
    p = v[-1].path if v else None
    if is_adopted(ep.root):
        from .project.readiness import guard_latest
        guard_latest(ep.root, stage, name, p)
    return p


class EditConflict(ValueError):
    code = "conflict.edit"


def _safe_components(path: Path):
    for component in (path, *path.parents):
        if component.is_symlink(): raise ValueError("Edit paths cannot contain symlinks")


@contextlib.contextmanager
def mutation_lock(ep: Episode, *, cancelled=None):
    """Shared local POSIX lock for every CLI and workbench EditDoc writer."""
    directory = edits_dir(ep)
    _safe_components(directory)
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(directory / ".versions.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode): raise ValueError("Version lock is not a regular file")
        if cancelled is None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            import time
            while True:
                if cancelled():raise ValueError('Edit operation cancelled while waiting for the version lock')
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:time.sleep(.05)
            if cancelled():raise ValueError('Edit operation cancelled before acquiring the version lock')
        yield
    finally: os.close(fd)


class SavedPath(type(Path())):
    """The path `save_new_version` returns: a normal Path, plus `.warnings` a command should put in its result."""

    warnings: tuple | list = ()  # derived paths (.parent, ...) carry none


def _saved(path: Path, warnings: list[str]) -> SavedPath:
    out = SavedPath(path)
    out.warnings = list(warnings)
    return out


def saved_warnings(path) -> list[str]:
    """The warnings of a path returned by save_new_version (empty for any other path)."""
    return list(getattr(path, "warnings", None) or [])


def _record(project, ep: Episode, path: Path, actor: str | None = None) -> list[str]:
    """Log `version.added` for a version file (once per file), as `actor` (default: the log's own, the person). A failure leaves
    the file in place, unrecorded, and says so (returned for the command result, and on stderr): `ave project adopt` records it later."""
    from .project import adopt
    try:
        adopt.record_version(project, path, actor=actor)
        return []
    except Exception as exc:  # noqa: BLE001 - the version is saved; only its log entry is missing
        import sys
        msg = (f"saved {path.name} but could not record it in the project log ({exc}); "
               f"run `ave project adopt {ep.root}` to record it")
        print(f"warning: {msg}", file=sys.stderr)
        return [msg]


def save_new_version(ep: Episode, doc: dict, name: str | None = None, *, expected_version=None,
                     expected_sha256=None, request_id=None, locked=False, actor: str | None = None) -> Path:
    """Atomic no-clobber publication. New callers pin CAS; legacy writers share allocation.

    In an adopted project the file is device-tagged and a `version.added` event (with what it needs) is logged, as `actor`
    (`"claude"` for a version Claude made on its own, e.g. `ave start`; default the person); anywhere
    else the name and behaviour are exactly the legacy ones."""
    if name is not None and (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name)):
        raise ValueError("Invalid edit name")
    problems = validate(doc)
    if problems: raise ValueError("invalid EditDoc:\n  - " + "\n  - ".join(problems))
    project = None
    if (ep.root / ".ave").is_symlink():
        raise ValueError(f"{ep.root / '.ave'} is a symlink; refusing to write a version into a project whose .ave is not a real folder")
    if is_adopted(ep.root):
        from .project import Project
        from .project.model import require_writable
        project = Project.open(ep.root)  # before writing: a broken project refuses here, not after the file exists
        require_writable(ep.root)        # and so does one a newer AVE saved: no version file in an older schema
    with contextlib.nullcontext() if locked else mutation_lock(ep):
        versions = version_entries(ep, doc["stage"], name)
        current = versions[-1] if versions else 0
        if request_id:
            wanted = next((h for h in reversed(doc.get("history", [])) if h.get("request_id") == request_id), None)
            if not wanted: raise ValueError("Request identity requires operation history")
            for v in versions:
                oldpath = path_for(ep, doc["stage"], v, name); _safe_components(oldpath)
                old = load(oldpath)
                found = next((h for h in old.get("history", []) if h.get("request_id") == request_id), None)
                if found:
                    if found != wanted: raise EditConflict("Request ID already belongs to different edit parameters")
                    return _saved(oldpath, _record(project, ep, oldpath, actor) if project is not None else [])
        if expected_version is not None and (not isinstance(expected_version, int) or isinstance(expected_version, bool)
                                             or expected_version != current):
            raise EditConflict(f"Edit changed; current version is {int(current)}")
        if expected_sha256 is not None:
            if not current: raise EditConflict("Edit bytes differ from the captured base")
            latest = path_for(ep, doc["stage"], current, name); _safe_components(latest)
            if hashlib.sha256(latest.read_bytes()).hexdigest() != expected_sha256:
                raise EditConflict("Edit bytes differ from the captured base")
        n = _next_number(versions)
        path = edits_dir(ep) / version_name(doc["stage"], n, name, project.device.short if project else None)
        _safe_components(path)
        data = (json.dumps(doc, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
        fd, temp = tempfile.mkstemp(prefix=".edit-", dir=ep.edits)
        try:
            with os.fdopen(fd, "wb") as out: out.write(data); out.flush(); os.fsync(out.fileno())
            publish_atomic(temp, path)  # EEXIST is a refusal, never a replacement
            dfd = os.open(ep.edits, os.O_RDONLY)
            try: os.fsync(dfd)
            finally: os.close(dfd)
        finally: Path(temp).unlink(missing_ok=True)
        return _saved(path, _record(project, ep, path, actor) if project is not None else [])


def load(path: Path | str) -> dict:
    """Read an edit file. A logged version that has not fully arrived on this computer, or arrived damaged, is refused
    (readiness.NotReady, a SystemExit carrying the plain reason) before a byte of it is parsed."""
    from .project.readiness import guard_load
    guard_load(Path(path))
    return read_json(Path(path))


def load_latest(ep: Episode, stage: str, name: str | None = None) -> dict:
    p = latest_path(ep, stage, name)
    if not p:
        raise SystemExit(f"No {stage} edit yet for {ep.root.name}.")
    return load(p)


def clone(doc: dict) -> dict:
    return copy.deepcopy(doc)


def slice_doc(doc: dict, o0: float, o1: float, stage: str | None = None) -> dict:
    """Cut [o0, o1) out of a doc into a new doc whose output time starts at 0.

    Video/dialogue segments are trimmed (their e0 advances by what was cut from the front); music is trimmed
    with `in` advanced; overlays that survive are shifted. Captions are dropped (they must be regenerated for the
    new timing). Used for selects and shorts.
    """
    if not (0 <= o0 < o1 <= doc["duration"] + EPS):
        raise ValueError(f"slice [{o0}, {o1}) is outside 0..{doc['duration']}")
    out = clone(doc)
    out["stage"] = stage or doc["stage"]
    out["duration"] = o1 - o0
    out["captions"] = {"srt": None, "ass": None, "burn": False}

    def trim(seg: dict, with_e0: bool, with_in: bool) -> dict | None:
        a, b = max(seg["o0"], o0), min(seg["o1"], o1)
        if b - a <= EPS:
            return None
        s = dict(seg)
        if with_e0:
            s["e0"] = seg["e0"] + (a - seg["o0"])
        if with_in:
            s["in"] = seg.get("in", 0.0) + (a - seg["o0"])
        s["o0"], s["o1"] = a - o0, b - o0
        return s

    t = out["tracks"]
    for name in ("video", "dialogue"):
        t[name] = [x for x in (trim(s, True, False) for s in doc["tracks"][name]) if x]
    t["music"] = [x for x in (trim(s, False, True) for s in doc["tracks"].get("music") or []) if x]
    t["overlays"] = [x for x in (trim(s, False, False) for s in doc["tracks"].get("overlays") or []) if x]
    for name in ("video", "dialogue"):  # snap float dust so the slice validates exactly
        if t[name]:
            t[name][0]["o0"] = 0.0
            t[name][-1]["o1"] = out["duration"]
    return out
