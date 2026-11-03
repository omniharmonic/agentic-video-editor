"""Fast section preview: find what an edit changed, then render only that stretch (plus padding) as a small proxy-preset clip.

The Room shows a new version of the cut seconds after the director asks for a change; rendering the whole cut for that would take
minutes. All times here are OUTPUT (cut) time of the doc passed in, so the Room can seek straight to the change. A preview is a
draft for looking at, not a deliverable: no burned captions, and its level is the full cut's gain (not its own), so A/B previews
of different spans, or of the cut before and after a change, are level-matched.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import logging
import threading
import time
import uuid
from difflib import SequenceMatcher
from pathlib import Path

from . import editdoc, ffutil, presets
from .compile import compile_audio, render
from .episode import Episode

PAD_S = 8.0
PRESET = "proxy"
TRACKS = ("video", "dialogue", "music", "overlays")
MATERIAL_SKIP = ("id", "o0", "o1")
MAX_WINDOW_S = 120.0       # a wider window is not a "fast" preview; the caller must pick a narrower span
WHOLE_CUT_S = 60.0         # a change that can touch any frame is previewed from the start of the cut, this long
KEEP_FILES = 50
KEEP_BYTES = 2 * 1024 ** 3
SHIFT_EPS = 1e-3
MIN_AGE_S = 60.0           # a preview younger than this may just have been handed to the Room: pruning never takes it
STALE_S = 3600.0           # leftover temp/measurement files and lock files of vanished previews go after this long
RETRY_AFTER_S = 600.0      # a failed gain measurement is not retried for this long ...
MAX_FAILURES = 3           # ... and not at all after this many failures
log = logging.getLogger(__name__)


def _key(seg: dict) -> str:
    """What a segment shows or plays, apart from its id and where it sits: two segments with the same key are the same material."""
    body = {k: v for k, v in seg.items() if k not in MATERIAL_SKIP}
    body["len"] = round(seg["o1"] - seg["o0"], 3)
    return json.dumps(body, sort_keys=True)


def _track_spans(old: list[dict], new: list[dict]) -> list[tuple[float, float]]:
    """Changed stretches of one track in `new`'s time: different material, or the same material placed differently.

    A shift that carries on unchanged from the segment before (a ripple after a cut) is not a change in itself; the segment where
    the shift starts (a slid shot, a moved cue, the segment after a lengthened gap) is, and so is the place it used to be."""
    spans: list[tuple[float, float]] = []
    shift: float | None = 0.0
    ops = SequenceMatcher(None, [_key(s) for s in old], [_key(s) for s in new], autojunk=False).get_opcodes()
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            for o, n in zip(old[i1:i2], new[j1:j2]):
                d = n["o0"] - o["o0"]
                if shift is not None and abs(d - shift) > SHIFT_EPS:
                    spans.append((n["o0"], n["o1"]))
                    spans.append((o["o0"], o["o1"]))
                shift = d
            continue
        if j2 > j1:
            spans.append((new[j1]["o0"], new[j2 - 1]["o1"]))
        else:                                       # only removed: the change is the join where they used to be
            at = new[j1]["o0"] if j1 < len(new) else (new[-1]["o1"] if new else 0.0)
            spans.append((at, at))
        shift = None
    return spans


def changed_span(before: dict, after: dict, pad_s: float = PAD_S) -> list[tuple[float, float]]:
    """The stretches of `after`, in output time and in order, that differ from `before`; empty when nothing a viewer would notice changed.

    Tracks are compared as sequences of material, so a moved or trimmed shot is found and a ripple delete reports only the join.
    Placement counts too (a cue moved, a segment slid, a gap lengthened). Stretches closer than 2 * pad_s merge, since their
    previews would overlap; far-apart changes stay separate, so one 1400 s apart from another is not a 1400 s render.
    A change that can touch any frame (media mode, loudness, sources, canvas, fps, room tone) is the first WHOLE_CUT_S of the cut."""
    dur = float(after["duration"])
    b_tr, a_tr = before.get("tracks") or {}, after.get("tracks") or {}
    if (any(before.get(k) != after.get(k) for k in ("media_mode", "loudness", "sources", "canvas", "fps"))
            or b_tr.get("room_tone") != a_tr.get("room_tone")):
        return [(0.0, min(WHOLE_CUT_S, dur))]
    found: list[tuple[float, float]] = []
    for name in TRACKS:
        found += _track_spans(b_tr.get(name) or [], a_tr.get(name) or [])
    merged: list[list[float]] = []
    for lo, hi in sorted((max(0.0, float(a)), min(float(b), dur)) for a, b in found):
        if merged and lo - merged[-1][1] <= 2 * pad_s:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return [(a, b) for a, b in merged]


def _window(doc: dict, span: tuple[float, float], pad_s: float) -> tuple[float, float]:
    """The padded span snapped outward to whole frames and clamped to the cut."""
    fps, dur = float(doc["fps"]), float(doc["duration"])
    lo = math.floor(max(0.0, span[0] - pad_s) * fps + 1e-6) / fps
    hi = dur if span[1] + pad_s >= dur else math.ceil((span[1] + pad_s) * fps - 1e-6) / fps
    return lo, min(hi, dur)


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:20]


def _with_proxies(ep: Episode, doc: dict) -> dict:
    """A copy of `doc` whose picture sources point at their verified proxies; footage without one stays on the original."""
    from . import proxies
    found, _missing = proxies.proxy_map(ep, doc)
    if not found:
        return doc
    out = editdoc.clone(doc)
    for k in {s["src"] for s in doc["tracks"]["video"]}:
        f = Path(doc["sources"][k]["file"])
        original = str(f if f.is_absolute() else (ep.root / f).resolve())
        if original in found:
            out["sources"][k]["file"] = found[original]
    return out


@contextlib.contextmanager
def _locked(path: Path):
    """Exclusive lock on `path` (across threads and processes), so one render of a key runs at a time."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)           # closing releases the lock


def cached_gain_db(ep: Episode, doc: dict) -> float | None:
    """The full cut's gain if it was already measured for exactly this EditDoc, else None (never measures)."""
    cache = ep.work / "preview" / f"gain-{_sha(doc)}.json"
    try:
        return float(json.loads(cache.read_text())["gain_db"])
    except (OSError, ValueError, KeyError):
        return None


def full_cut_gain_db(ep: Episode, doc: dict) -> float:
    """The gain that takes the FULL cut's programme to the proxy loudness target, measured once per EditDoc and cached in
    work/preview/gain-<hash>.json. A preview uses this fixed gain, so its level is the cut's, not its own."""
    d = ep.work / "preview"
    cache = d / f"gain-{_sha(doc)}.json"
    with _locked(d / f".{cache.name}.lock"):
        if cache.exists():
            return float(json.loads(cache.read_text())["gain_db"])
        pr = presets.get(PRESET)
        wav = d / f".gain-{uuid.uuid4().hex}.wav"
        try:
            compile_audio(ep, doc, wav)
            m = ffutil.loudnorm_measure(wav, pr["lufs"], pr["tp"])
        finally:
            wav.unlink(missing_ok=True)
        gain = 0.0 if m is None else pr["lufs"] - m["input_i"]
        tmp = cache.with_name(f".{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps({"gain_db": gain}))
        os.replace(tmp, cache)
        return gain


_MEASURING: dict[str, threading.Thread] = {}
_MEASURING_LOCK = threading.Lock()


def _failure_path(ep: Episode, doc: dict) -> Path:
    return ep.work / "preview" / f"gainfail-{_sha(doc)}.json"


def _read_failures(ep: Episode, doc: dict) -> dict:
    try:
        rec = json.loads(_failure_path(ep, doc).read_text())
        return {"count": int(rec["count"]), "at": float(rec["at"])}
    except (OSError, ValueError, KeyError, TypeError):
        return {"count": 0, "at": 0.0}


def _measure_in_background(ep: Episode, doc: dict) -> None:
    """Start measuring the full cut's gain on a daemon thread, at most one at a time per EditDoc.

    A failure is remembered per EditDoc (work/preview/gainfail-<hash>.json: count and time) and logged; the measurement is not
    retried for RETRY_AFTER_S and not at all after MAX_FAILURES, so a cut that cannot be measured does not restart a heavy compile
    on every cold preview. Previews of it just stay approximate."""
    key = f"{ep.root}:{_sha(doc)}"
    with _MEASURING_LOCK:
        t = _MEASURING.get(key)
        if t is not None and t.is_alive():
            return
        failed = _read_failures(ep, doc)
        if failed["count"] >= MAX_FAILURES or (failed["count"] and time.time() - failed["at"] < RETRY_AFTER_S):
            return

        def run() -> None:
            try:
                full_cut_gain_db(ep, doc)
                _failure_path(ep, doc).unlink(missing_ok=True)
            except Exception as e:
                count = failed["count"] + 1
                log.warning("preview: measuring the full-cut loudness failed (%d of %d)%s: %s", count, MAX_FAILURES,
                            "; giving up, previews stay approximate" if count >= MAX_FAILURES else "", e)
                try:
                    _failure_path(ep, doc).parent.mkdir(parents=True, exist_ok=True)
                    _failure_path(ep, doc).write_text(json.dumps({"count": count, "at": time.time()}))
                except OSError:
                    pass
            finally:
                with _MEASURING_LOCK:
                    _MEASURING.pop(key, None)

        _MEASURING[key] = t = threading.Thread(target=run, name=f"preview-gain-{key[-8:]}", daemon=True)
        t.start()


def wait_for_measurements(timeout: float | None = None) -> None:
    """Block until background gain measurements started so far have finished (tests, shutdown)."""
    with _MEASURING_LOCK:
        pending = list(_MEASURING.values())
    for t in pending:
        t.join(timeout)


def _prune(d: Path, keep: Path) -> None:
    """Keep the newest KEEP_FILES previews and at most KEEP_BYTES of them; `keep` (just made) and anything younger than MIN_AGE_S
    always stay. Also drops old gain records, lock files of previews that are gone, and stale temp/measurement dot-files."""
    now = time.time()

    def age(p: Path) -> float:
        try:
            return now - p.stat().st_mtime
        except OSError:
            return 0.0

    files = sorted((p for p in d.glob("*.mp4") if not p.name.startswith(".")), key=lambda p: p.stat().st_mtime, reverse=True)
    total = 0
    for i, p in enumerate(files):
        total += p.stat().st_size
        if p != keep and age(p) >= MIN_AGE_S and (i >= KEEP_FILES or total > KEEP_BYTES):
            p.unlink(missing_ok=True)
    for pattern in ("gain-*.json", "gainfail-*.json"):
        for p in sorted(d.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)[KEEP_FILES:]:
            p.unlink(missing_ok=True)
    for p in d.glob(".*"):
        if age(p) < STALE_S:
            continue
        if p.name.endswith(".lock") and p.name.startswith(".gain-"):
            gone = not (d / p.name[1:-len(".lock")]).exists()           # .gain-<hash>.json.lock
        elif p.name.endswith(".lock"):
            gone = not (d / (p.name[1:-len(".lock")] + ".mp4")).exists()
        else:
            gone = p.suffix in (".wav", ".mp4", ".tmp")
        if gone:
            p.unlink(missing_ok=True)


def render_section(ep: Episode, doc: dict, span: tuple[float, float], pad_s: float = PAD_S, gain_db: float | None = None,
                   parent: dict | None = None) -> dict:
    """Render the padded span of `doc` to work/preview/<sha>.mp4 (reused when it already exists) and say where it sits.

    Returns {path, start, end, duration, seek, span, level_approximate}: `start`/`end` are the clip's place in the full cut, `seek`
    is the change's start in the clip's own time, `span` the change in cut time. `gain_db` is the full cut's gain. Omitted, the
    gain cached for this EditDoc is used; with none, the preview does NOT wait for the (tens of seconds) measurement: it renders at
    the nearest known gain (`parent`'s, else unity), says `level_approximate: True`, and measures in the background so the next
    preview is exact. A window wider than MAX_WINDOW_S is refused."""
    problems = editdoc.validate(doc)
    if problems:
        raise ValueError("invalid EditDoc:\n  - " + "\n  - ".join(problems))
    lo, hi = _window(doc, span, pad_s)
    if hi - lo <= 0:
        raise ValueError(f"span {span} is outside the cut (0..{doc['duration']})")
    if hi - lo > MAX_WINDOW_S:
        raise ValueError(f"preview window {lo:.1f}-{hi:.1f} s is {hi - lo:.0f} s, wider than the {MAX_WINDOW_S:.0f} s limit; "
                         f"preview a narrower span")
    used = _with_proxies(ep, doc)
    d = ep.work / "preview"
    approximate = False
    if gain_db is None:
        gain_db = cached_gain_db(ep, doc)
        if gain_db is None:
            approximate = True
            gain_db = cached_gain_db(ep, parent) if parent is not None else None
            gain_db = 0.0 if gain_db is None else gain_db
    gain_db = round(gain_db, 2)
    # the key covers the edit, the span and pad, and which files were read (a proxy renders differently from its original)
    out = d / f"{_sha({'doc': doc, 'span': [round(span[0], 4), round(span[1], 4)], 'pad': pad_s, 'media': {k: s['file'] for k, s in used['sources'].items()}, 'gain': gain_db})}.mp4"
    result = {"path": out, "start": lo, "end": hi, "duration": hi - lo, "seek": max(0.0, span[0] - lo), "span": (span[0], span[1]),
              "level_approximate": approximate}
    if approximate:
        _measure_in_background(ep, doc)
    if out.exists():
        os.utime(out)                         # a hit counts as use: pruning keeps what was just handed out
        return result
    with _locked(d / f".{out.stem}.lock"):
        if out.exists():                      # another render of this key finished while we waited
            os.utime(out)
            return result
        piece = editdoc.slice_doc(used, lo, hi)
        tmp = d / f".{out.stem}.{uuid.uuid4().hex}.mp4"
        try:
            render(ep, piece, tmp, preset={**presets.get(PRESET), "fixed_gain_db": gain_db}, burn_captions=False, overwrite=True)
            os.replace(tmp, out)
        finally:
            tmp.unlink(missing_ok=True)
        _prune(d, out)
    return result


def render_changed(ep: Episode, before: dict, after: dict, pad_s: float = PAD_S) -> dict | None:
    """What the Room calls after an edit: render the first changed stretch of `after`; None when nothing changed.

    Never waits on the full-cut gain measurement: with none cached for `after` it uses `before`'s (an edit moves a cut's loudness by
    a fraction of a dB, and before/after previews then sit at the same level) and flags the result `level_approximate`.

    Raises ValueError when the first changed span's window is wider than MAX_WINDOW_S (120 s), or the doc is invalid; the Room
    should catch it and offer a narrower span instead."""
    spans = changed_span(before, after, pad_s)
    return render_section(ep, after, spans[0], pad_s, parent=before) if spans else None


def section(ep: Episode, doc: dict, span: tuple[float, float], pad_s: float = PAD_S) -> Path:
    """The preview clip for `span` of `doc`, padded by `pad_s` each side (see `render_section` for where it sits in the cut)."""
    return render_section(ep, doc, span, pad_s)["path"]
