"""EditDoc v1: the one contract every stage reads and writes. See docs/superpowers/specs/.

All track times are OUTPUT time (seconds from the start of the cut). `e0` is EPISODE time (the reference
timeline; see AGENTS.md). A source file's own clock for episode time e is  (e - start_offset) / (1 + drift_ppm * 1e-6)
(`source_time`: the ONE place this is computed). `drift_ppm` > 0 means the file's clock runs slow against the reference.
A removed section is simply a jump in `e0` between consecutive segments; J/L cuts are video and
dialogue boundaries that differ.
"""

from __future__ import annotations

import bisect
import copy
import re
from pathlib import Path
from typing import Iterable

import numpy as np

from .episode import Episode, read_json, write_json

VERSION = 1
STAGES = ("assembly", "rough", "final", "select", "short")
EPS = 1e-3          # 1 ms tolerance for contiguity checks
KINDS = ("video", "audio")


def new_doc(stage: str, fps: float, canvas: tuple[int, int], sources: dict, duration: float = 0.0) -> dict:
    if stage not in STAGES:
        raise ValueError(f"stage must be one of {STAGES}")
    return {
        "version": VERSION, "stage": stage, "fps": float(fps),
        "canvas": {"w": int(canvas[0]), "h": int(canvas[1])},
        "sources": sources,
        "tracks": {"video": [], "dialogue": [], "room_tone": None, "music": [], "overlays": []},
        "captions": {"srt": None, "ass": None, "burn": False},
        "duration": float(duration), "notes": "",
    }


def validate(doc: dict) -> list[str]:
    """Return a list of problems (empty means valid). Never raises, so callers can show every issue."""
    p: list[str] = []
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
        if "start_offset" not in s:
            p.append(f"source {k}: start_offset is required")
        d = s.get("drift_ppm", 0)
        if not isinstance(d, (int, float)) or isinstance(d, bool) or abs(d) > 5000:
            p.append(f"source {k}: drift_ppm must be a number within +-5000 (got {d!r})")
        sm = s.get("sync_map")
        if sm is not None and (not isinstance(sm, list) or any(not isinstance(x, (list, tuple)) or len(x) != 2 for x in sm)):
            p.append(f"source {k}: sync_map must be a list of [file_t, offset] pairs")
        for m in s.get("channel_map") or []:
            if m.get("use") not in ("left", "right", "mix", "sum") or not m["end"] > m["start"] >= 0:
                p.append(f"source {k}: bad channel_map range {m!r} (need start < end and use left|right|mix|sum)")
    tr = doc.get("tracks") or {}
    dur = doc.get("duration", 0.0)
    for name in ("video", "dialogue"):
        segs = tr.get(name) or []
        p += _check_track(name, segs, src, dur, required_kind="video" if name == "video" else None)
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


def clock_rate(src: dict, episode_t: float) -> float:
    """Episode seconds per file second at `episode_t` (1 + drift for a plain source; the local segment slope for a piecewise
    one). Consumers that stretch media to follow the source clock use this, never drift_of directly."""
    kn = _knots(src)
    if kn is None:
        return 1.0 + drift_of(src)
    ft, et = kn
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


# ---- persistence: edits/<stage>.vN.json -------------------------------------------------------

_V = re.compile(r"^(?P<stage>[a-z]+)(?:\.(?P<name>[A-Za-z0-9_-]+))?\.v(?P<n>\d+)\.json$")


def edits_dir(ep: Episode) -> Path:
    return ep.edits


def list_versions(ep: Episode, stage: str, name: str | None = None) -> list[int]:
    out = []
    for f in edits_dir(ep).glob(f"{stage}*.json") if edits_dir(ep).is_dir() else []:
        m = _V.match(f.name)
        if m and m["stage"] == stage and (m["name"] or None) == name:
            out.append(int(m["n"]))
    return sorted(out)


def path_for(ep: Episode, stage: str, version: int, name: str | None = None) -> Path:
    mid = f".{name}" if name else ""
    return edits_dir(ep) / f"{stage}{mid}.v{version}.json"


def latest_path(ep: Episode, stage: str, name: str | None = None) -> Path | None:
    v = list_versions(ep, stage, name)
    return path_for(ep, stage, v[-1], name) if v else None


def save_new_version(ep: Episode, doc: dict, name: str | None = None) -> Path:
    """Write the doc as the next version of its stage. Refuses an invalid doc; never overwrites."""
    problems = validate(doc)
    if problems:
        raise ValueError("invalid EditDoc:\n  - " + "\n  - ".join(problems))
    v = (list_versions(ep, doc["stage"], name) or [0])[-1] + 1
    path = path_for(ep, doc["stage"], v, name)
    write_json(path, doc)
    return path


def load(path: Path | str) -> dict:
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
