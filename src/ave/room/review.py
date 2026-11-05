"""What the Room's Script and Selects views need from disk: the transcript as readable lines, the clips Claude selected with their
state, a still for each, the camera files the viewer plays, and the director's keep / maybe / skip.

Times. Transcript times are episode seconds (the reference recording's clock, as `clips.mapped_words` reads them). A clip's start and
end are output seconds of the edit it was picked from; each select also carries its episode range (`e0`, `e1`) so the Script can
mark it and a flag on it reads in the same clock as the transcript.

People read names: speakers come from episode.json ("Host"), else the transcript, else "Speaker 1". Cameras are "Host camera".
File names, version names and clip ids stay in the data, never in a label.

The originals are never served (the raw footage folders stay off the wire, as everywhere in the app). The viewer plays a review
copy of each recording instead: a small H.264 copy made with ffmpeg into work/room/review/, on its own clock exactly like the
original. Its name carries the original's path, size and time, so a replaced original gets a fresh copy and the old one is
removed. The copy follows proxies.py's rules: read the original only, check free space first, write `.partial`, rename only after
ffmpeg finished, verify the result, and leave no `.partial` behind. One copy is made at a time across the app; a copy that stops
making progress is killed.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from .. import editdoc
from ..episode import Episode, read_json
from . import cut as room_cut

LINE_WORDS = 48                                          # a turn longer than this breaks into lines at sentence ends
HARD_WORDS = 80                                          # ... and at this many words when nobody stops for breath
VERDICTS = ("keep", "maybe", "skip", "pending")
CLIP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
THUMB_DIR = ("room", "thumbs")
REVIEW_DIR = ("room", "review")
REVIEW_HEIGHT = 540
_THUMBS = threading.BoundedSemaphore(2)                   # at most two ffmpeg stills at once, however many cards ask
_QUEUE: list[object] = []                               # review copies waiting their turn, first in line first; one encodes at a time
_QUEUE_CV = threading.Condition()
WAITING_EVERY_S = 10.0                                   # a copy in line says so this often, so nobody takes the wait for a stall
STALL_S = 120.0                                          # a copy whose ffmpeg reports no progress for this long is killed
SPACE_FACTOR = 1.5                                       # as proxies.py: refuse unless free space is this many times the estimate
COPY_MBPS = 2.5                                          # what a 540p review copy takes, video and audio, for the estimate
_TERMINAL = (".", "?", "!", "…")
_RUNNING: set[subprocess.Popen] = set()
_RUN_LOCK = threading.Lock()
_CACHE: dict[tuple, tuple] = {}
_CACHE_LOCK = threading.Lock()


def _stat_key(p: Path) -> tuple:
    try:
        st = p.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return (None, None)


def _cached(name: str, key: tuple, make: Callable[[], object]):
    """`make()` once per `key` (file stats), so a byte-range request does not re-read a 20,000-word transcript."""
    with _CACHE_LOCK:
        hit = _CACHE.get(name)
        if hit and hit[0] == key:
            return hit[1]
    value = make()
    with _CACHE_LOCK:
        _CACHE[name] = (key, value)
        if len(_CACHE) > 256:
            _CACHE.pop(next(iter(_CACHE)))
    return value


def _meta(ep: Episode) -> dict:
    return room_cut._meta(ep)


def transcript_state(ep: Episode) -> str:
    """ok, missing or unreadable: the empty state must say which."""
    p = ep.artifact("transcript.json")
    if not p.is_file():
        return "missing"
    return "ok" if _transcript(ep) is not None else "unreadable"


def _transcript(ep: Episode) -> dict | None:
    p = ep.artifact("transcript.json")
    if not p.is_file():
        return None

    def read():
        try:
            tr = read_json(p)
        except (OSError, ValueError):
            return None
        return tr if isinstance(tr, dict) and isinstance(tr.get("words"), list) else None
    return _cached(f"tr:{p}", _stat_key(p), read)


def files_stamp(ep: Episode) -> dict:
    """What the Script and the Selects read, as stats: when one changes, the view on screen reloads."""
    return {"script": repr((_stat_key(ep.artifact("transcript.json")), _stat_key(ep.meta_path))),
            "selects": repr((_stat_key(ep.artifact("clips.json")), _stat_key(ep.meta_path)))}


def speaker_names(ep: Episode, tr: dict | None) -> dict[str, str]:
    """Speaker id -> the name people read."""
    meta = (_meta(ep).get("speakers") or {})
    ids = sorted({str(w.get("speaker")) for w in (tr or {}).get("words", []) if w.get("speaker")} | set(meta) |
                 set(((tr or {}).get("speakers") or {})))
    out = {}
    for n, sid in enumerate(ids, 1):
        name = (meta.get(sid) or {}).get("name") or (((tr or {}).get("speakers") or {}).get(sid) or {}).get("name")
        out[sid] = str(name) if name else f"Speaker {n}"
    return out


def _latest_doc(ep: Episode) -> dict | None:
    if not ep.edits.is_dir():
        return None
    files = sorted((p for p in ep.edits.glob("*.json") if room_cut.VERSION_RE.fullmatch(p.stem)), key=lambda p: p.stat().st_mtime)
    for p in reversed(files):
        try:
            return editdoc.load(p)                       # refuses a version that has not fully arrived here (readiness)
        except (Exception, SystemExit):                  # a broken or arriving version must not hide the script: the one before
            continue                                     # maps the recordings, and nothing of the arriving one is read
    return None


def _clock(src: dict) -> dict:
    """How episode time maps onto one recording's own clock, for the browser: the offset, the drift factor, and the piecewise sync
    map's knots (file time -> episode time) when `ave sync` measured one. Same rule as editdoc.src_time."""
    out = {"start_offset": float(src.get("start_offset", 0.0) or 0.0), "k": 1.0 + float(editdoc.drift_of(src))}
    kn = editdoc._knots(src)
    if kn:
        out["ft"], out["et"] = [round(x, 4) for x in kn[0]], [round(x, 4) for x in kn[1]]
    return out


def _playable_list(ep: Episode) -> list[dict]:
    meta = _meta(ep)
    tr = _transcript(ep) or {}
    doc = _latest_doc(ep) or {}
    by_file = {str(s.get("file")): s for s in (doc.get("sources") or {}).values()}
    try:
        sync = (read_json(ep.artifact("sync.json")).get("files") or {}) if ep.artifact("sync.json").is_file() else {}
    except (OSError, ValueError):
        sync = {}
    names = speaker_names(ep, tr)
    out, seen = [], set()

    def add(rel: str, label: str, kind: str) -> None:
        if not rel or rel in seen or not (ep.root / rel).is_file():
            return
        seen.add(rel)
        src = by_file.get(rel) or (sync.get(rel) if isinstance(sync.get(rel), dict) else None) or {"start_offset": 0.0}
        clock = _clock(src)
        out.append({"key": str(len(out)), "file": rel, "label": label, "kind": kind, "clock": clock,
                    "start_offset": clock["start_offset"], "drift": clock["k"] - 1.0})

    speakers = meta.get("speakers") or {}
    for cam, info in (meta.get("cameras") or {}).items():
        who = next((names.get(sid) for sid, s in speakers.items() if s.get("camera") == cam), None)
        label = f"{who} camera" if who else (f"{str(info.get('role')).capitalize()} camera" if info.get("role") else f"Camera {cam}")
        add(str(info.get("file") or ""), label, "video")
    if tr.get("source"):
        add(str(tr["source"]), "The recording", "audio" if Path(str(tr["source"])).suffix.lower() in (".wav", ".m4a", ".mp3", ".aac", ".flac") else "video")
    return out


def playable(ep: Episode) -> list[dict]:
    """The recordings the viewer can play, in a fixed order: each camera in episode.json, then the transcript's own recording if it
    is not one of them. Each has the clock that maps episode time onto its own time, and whether its review copy is ready. The
    parse is cached on the stats of the files it reads; only `copy` and `ready` are looked up afresh."""
    edits = ep.edits
    key = (_stat_key(ep.meta_path), _stat_key(ep.artifact("sync.json")), _stat_key(ep.artifact("transcript.json")),
           _stat_key(edits) if edits.is_dir() else None)
    base = _cached(f"play:{ep.root}", key, lambda: _playable_list(ep))
    out = []
    for s in base:
        copy = review_copy_path(ep, s["file"])
        out.append({**s, "copy": copy, "ready": copy.is_file()})
    return out


def source(ep: Episode, key: str) -> dict:
    for s in playable(ep):
        if s["key"] == key:
            return s
    raise room_cut.NotFound("That recording is not in this project")


def source_path(ep: Episode, key: str) -> Path:
    """The review copy the viewer plays (never the original)."""
    s = source(ep, key)
    if not s["ready"]:
        raise room_cut.NotFound("not made yet")
    return s["copy"]


def review_copy_path(ep: Episode, rel: str) -> Path:
    """<hash of the path>-<hash of size and mtime>.mp4: one name per original, a new one whenever the original changes."""
    f = ep.root / rel
    try:
        st = f.stat()
        stamp = f"{st.st_size}:{st.st_mtime_ns}"
    except OSError:
        stamp = "missing"
    return ep.work.joinpath(*REVIEW_DIR) / f"{hashlib.sha1(rel.encode()).hexdigest()[:12]}-{hashlib.sha1(stamp.encode()).hexdigest()[:8]}.mp4"


class CopyError(RuntimeError):
    """Why a review copy could not be made, in words the app can show (no paths, no ffmpeg output)."""


def _verify(original: Path, copy: Path, kind: str) -> str | None:
    from .. import ffutil
    try:
        pc, po = ffutil.probe(copy), ffutil.probe(original)
    except Exception:
        return "the copy does not read back"
    if kind == "video" and not pc.get("has_video"):
        return "the copy has no picture"
    if po.get("duration") and abs(float(pc.get("duration") or 0.0) - float(po["duration"])) > 1.0:
        return "the copy is shorter than the original"
    return None


def make_review_copy(ep: Episode, key: str, progress: Callable[[float], None] | None = None,
                     waiting: Callable[[int], None] | None = None) -> Path:
    """Make the review copy of one recording (a no-op when it exists). Waits its turn: one copy at a time."""
    from .. import ffutil, proxies
    s = source(ep, key)
    out = s["copy"]
    if out.is_file():
        return out
    if not shutil.which("ffmpeg"):
        raise CopyError("ffmpeg is not installed on this computer")
    ticket = object()
    with _QUEUE_CV:
        _QUEUE.append(ticket)
    try:                                                 # the wait is inside: whatever happens, `finally` takes the ticket out of line
        with _QUEUE_CV:
            while _QUEUE[0] is not ticket:               # position 1 is the one encoding; waiting() hears its place every 10 s
                if waiting:
                    waiting(_QUEUE.index(ticket))
                _QUEUE_CV.wait(WAITING_EVERY_S)
        if out.is_file():                                # made while this one waited
            return out
        folder = out.parent
        folder.mkdir(parents=True, exist_ok=True)
        stem = out.name.split("-")[0]
        for old in folder.iterdir():                     # an earlier copy of a replaced original, or anything half-made a crash left
            if old != out and (old.name.startswith(f"{stem}-") or ".partial." in old.name):
                old.unlink(missing_ok=True)
        original = ep.root / s["file"]
        try:
            facts = proxies._source_facts(original, REVIEW_HEIGHT)
        except Exception:
            raise CopyError("the recording could not be read") from None
        need = COPY_MBPS * 1e6 / 8 * (facts["duration"] or 0.0)
        if shutil.disk_usage(folder).free < SPACE_FACTOR * need:
            raise CopyError(f"not enough free space for a copy (about {need / 1e9:.1f} GB)")
        partial = out.with_name(out.stem + ".partial.mp4")
        log = out.with_name(out.stem + ".partial.log")
        hw = ["-hwaccel", "videotoolbox"] if s["kind"] == "video" and proxies.hw_decode_available() else []
        video = ["-map", "0:v:0", "-vf", f"scale=-2:{facts['h']}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
                 "-pix_fmt", "yuv420p", "-g", "30"] if s["kind"] == "video" else ["-vn"]
        cmd = [ffutil._bin("ffmpeg"), "-nostdin", "-hide_banner", "-v", "error", "-nostats", "-progress", "pipe:1", "-y", *hw,
               "-i", str(original), *video, "-map", "0:a:0?", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(partial)]
        try:
            with log.open("w") as err:                   # stderr to a file: a chatty ffmpeg can never fill a pipe and hang
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, text=True)
                with _RUN_LOCK:
                    _RUNNING.add(p)
                last = [time.monotonic()]

                def watchdog():
                    while p.poll() is None:
                        if time.monotonic() - last[0] > STALL_S:
                            p.kill()
                            return
                        time.sleep(1.0)
                threading.Thread(target=watchdog, daemon=True, name="review-watchdog").start()
                for line in p.stdout:                    # out_time_us=12345678 about twice a second
                    last[0] = time.monotonic()
                    if progress and facts["duration"] and line.startswith("out_time_us=") and line[12:].strip().isdigit():
                        progress(min(1.0, int(line[12:]) / 1e6 / facts["duration"]))
                code = p.wait()
            if code != 0 or not partial.is_file() or not partial.stat().st_size:
                raise CopyError("the copy stopped making progress" if code in (-9, 137) else "ffmpeg could not make the copy")
            why = _verify(original, partial, s["kind"])
            if why:
                raise CopyError(why)
            os.replace(partial, out)
            return out
        finally:
            with _RUN_LOCK:
                _RUNNING.discard(locals().get("p"))
            partial.unlink(missing_ok=True)
            log.unlink(missing_ok=True) if out.is_file() else None
    finally:
        with _QUEUE_CV:
            _QUEUE.remove(ticket)
            _QUEUE_CV.notify_all()


def stop_copies() -> None:
    """Kill every review copy still being made (the app is closing); their .partial files are removed as they end."""
    with _RUN_LOCK:
        running = list(_RUNNING)
    for p in running:
        try:
            p.kill()
        except OSError:
            pass


def source_url(artifact: str, key: str) -> str:
    from urllib.parse import quote
    return f"/api/room/source/{quote(artifact, safe='')}/{quote(key, safe='')}"


def _lines(tr: dict, names: dict[str, str]) -> list[dict]:
    """Turns as readable lines: one per speaker turn, long turns broken at sentence ends. `w` holds each word's start and end."""
    words = tr["words"]
    utts = tr.get("utterances")
    if not isinstance(utts, list) or not utts:
        from ..transcribe import build_utterances
        utts = build_utterances([dict(w, speaker=w.get("speaker") or "S1") for w in words])
    order = {sid: i for i, sid in enumerate(names)}
    out = []
    for u in utts:
        a, b = int(u.get("first_word", 0)), int(u.get("last_word", -1))
        run = [w for w in words[a:b + 1] if isinstance(w, dict) and w.get("w") is not None]
        sid = str(u.get("speaker") or (run[0].get("speaker") if run else "") or "")
        start = 0
        for k in range(len(run)):
            n = k - start + 1
            end_here = k == len(run) - 1 or (n >= LINE_WORDS and str(run[k]["w"]).endswith(_TERMINAL)) or n >= HARD_WORDS
            if not end_here:
                continue
            part = run[start:k + 1]
            start = k + 1
            ws = []
            for w in part:
                ws += [round(float(w["start"]), 2), round(float(w["end"]), 2)]
            out.append({"i": len(out), "t0": ws[0], "t1": ws[-1], "sp": names.get(sid, "Speaker"), "s": order.get(sid, 0),
                        "text": " ".join(str(w["w"]) for w in part), "w": ws})
    return out


def script_info(ep: Episode, artifact: str) -> dict:
    tr = _transcript(ep)
    names = speaker_names(ep, tr)
    srcs = playable(ep)
    video = [s for s in srcs if s["kind"] == "video"]
    ref = next((s for s in srcs if tr and s["file"] == tr.get("source") and s["kind"] == "video"), video[0] if video else (srcs[0] if srcs else None))
    return {
        "artifact": artifact, "title": _meta(ep).get("title") or artifact,
        "lines": _lines(tr, names) if tr else [], "has_transcript": bool(tr),
        "speakers": [names[k] for k in names], "duration": float((tr or {}).get("duration") or 0.0),
        "sources": [{"key": s["key"], "label": s["label"], "kind": s["kind"], "url": source_url(artifact, s["key"]), "ready": s["ready"],
                     "clock": s["clock"]} for s in srcs],
        "transcript": transcript_state(ep), "stamp": files_stamp(ep)["script"],
        "source": ref["key"] if ref else None,
        "clips": {c["id"]: {"title": c["title"], "e0": c["e0"], "e1": c["e1"]} for c in _clip_rows(ep, artifact, srcs)},
    }


# ---- selects -----------------------------------------------------------------------------------------------------------------

def _load_clips(ep: Episode) -> dict | None:
    from .. import clips
    try:
        return clips.load_clips(ep)
    except (OSError, ValueError):
        return None


def _version_id(src: dict) -> str:
    name = src.get("name")
    return f"{src['stage']}.{name}.v{src['version']}" if name else f"{src['stage']}.v{src['version']}"


def _to_episode(doc: dict, o: float) -> float:
    """Output time of `doc` -> episode time, through the dialogue (else video) segment that plays it."""
    segs = [s for s in (doc.get("tracks") or {}).get("dialogue") or [] if not s.get("fill")] or (doc.get("tracks") or {}).get("video") or []
    if not segs:
        return o
    seg = next((s for s in segs if s["o0"] - 1e-6 <= o <= s["o1"] + 1e-6), min(segs, key=lambda s: min(abs(s["o0"] - o), abs(s["o1"] - o))))
    return float(seg.get("e0", seg["o0"])) + (min(max(o, seg["o0"]), seg["o1"]) - seg["o0"])


def _state(c: dict) -> str:
    if c.get("status") == "approved":
        return "picked"
    if c.get("status") == "rejected":
        return "skipped"
    if c.get("maybe"):
        return "maybe"
    return "new" if c.get("origin") == "agent" else "alternate"          # Claude's own pick, not decided (or taken back)


def _verdict(c: dict) -> str | None:
    if c.get("status") == "approved":
        return "keep"
    if c.get("status") == "rejected":
        return "skip"
    return "maybe" if c.get("maybe") else None


def _found(c: dict) -> str:
    """Why the clip finder surfaced a section nobody picked, in words: its tags ("claim", "quotable"), never its scoring notes."""
    tags = [str(t).replace("_", " ") for t in c.get("tags") or [] if t]
    return f"The clip finder also found this: {', '.join(tags)}." if tags else "The clip finder also found this."


def _speakers_label(ids: list, names: dict[str, str]) -> str:
    who = list(dict.fromkeys(names.get(str(s), "") for s in ids or [] if names.get(str(s))))
    return " and ".join(who) if len(who) <= 2 else ", ".join(who[:-1]) + " and " + who[-1]


def _clip_rows(ep: Episode, artifact: str, srcs: list[dict] | None = None) -> list[dict]:
    from .. import clips
    from ..review_pack import frame_source
    data = _load_clips(ep)
    if not data or not isinstance(data.get("clips"), list):
        return []
    srcs = playable(ep) if srcs is None else srcs
    key_of = {s["file"]: s["key"] for s in srcs}
    names = speaker_names(ep, _transcript(ep))
    docs: dict[tuple, dict | None] = {}
    rows = []
    from .. import learning
    try:
        said = learning.latest_decisions(ep)                # the director's last why for each select (reasons and note)
    except (OSError, ValueError):
        said = {}
    for c in data["clips"]:
        if not isinstance(c, dict) or not CLIP_RE.fullmatch(str(c.get("id", ""))):
            continue
        src = c.get("source") or data.get("source") or {}
        k = (src.get("stage"), src.get("name"), src.get("version"))
        if k not in docs:
            try:
                docs[k] = clips.load_stage_doc(ep, src.get("stage"), src.get("version"), src.get("name"))[0]
            except BaseException:                         # SystemExit from a missing edit: the card still shows, without a preview
                docs[k] = None
        doc = docs[k]
        start, end = float(c.get("start", 0.0)), float(c.get("end", 0.0))
        e0, e1 = (_to_episode(doc, start), _to_episode(doc, end)) if doc else (start, end)
        play = None
        version = _version_id(src) if src.get("stage") and src.get("version") is not None else None
        render = room_cut.full_render(ep, version) if version and room_cut.VERSION_RE.fullmatch(version) else None
        if render is not None:
            play = {"url": room_cut.media_url(artifact, "exports", render.name), "start": start, "end": end, "kind": "full"}
        elif doc and (doc.get("tracks") or {}).get("video"):
            f, t = frame_source(doc, start)
            key = key_of.get(str(f))
            if key is not None:
                play = {"url": source_url(artifact, key), "start": t, "end": t + (end - start), "kind": "source", "key": key,
                        "ready": next(s["ready"] for s in srcs if s["key"] == key)}
        rows.append({
            "id": c["id"], "title": c.get("title") or "Untitled select", "duration": float(c.get("duration") or (end - start)),
            "start": start, "end": end, "e0": round(e0, 3), "e1": round(e1, 3),
            "speakers": _speakers_label(c.get("speakers") or [], names),
            # Claude's picks carry its reason; for a section the finder surfaced, `rationale` is a scoring note, so say it in words
            "why": (c.get("rationale") or c.get("hook") or "") if c.get("origin") == "agent" else _found(c),
            "origin": "claude" if c.get("origin") == "agent" else "finder",
            "text": c.get("text") or "", "state": _state(c), "verdict": _verdict(c), "rank": c.get("rank"),
            "stale": bool(c.get("stale")), "play": play,
            "reasons": list((said.get(c["id"]) or {}).get("reasons") or []), "note": (said.get(c["id"]) or {}).get("note") or "",
            "stale_reason": "From an older version of the cut, so it can only be skipped. Ask Claude to pick it again." if c.get("stale") else None,
            "thumb": f"{thumb_url(artifact, c['id'])}?at={int(round((start + end) / 2 * 1000))}" if doc else None,
        })
    rows.sort(key=lambda r: (r["rank"] is None, r["rank"] or 0, r["start"]))
    return rows


def selects_info(ep: Episode, artifact: str) -> dict:
    p = ep.artifact("clips.json")
    data = _load_clips(ep) if p.is_file() else None
    state = "missing" if not p.is_file() else ("unreadable" if not isinstance(data, dict) or not isinstance(data.get("clips"), list)
                                                else "ok" if data["clips"] else "empty")
    return {"artifact": artifact, "title": _meta(ep).get("title") or artifact, "clips_state": state, "has_clips": state == "ok",
            "clips": _clip_rows(ep, artifact) if state == "ok" else [], "stamp": files_stamp(ep)["selects"],
            "reasons": _reasons(ep)}


def _reasons(ep: Episode) -> list[dict]:
    """The why-chips for a keep, maybe or skip: the clip finder's vocabulary (config/clip_reasons.yaml, merged by id)."""
    from .. import learning
    try:
        return [{"id": r["id"], "label": r["label"], "verdicts": r.get("verdicts") or ["keep", "maybe", "skip"]} for r in learning.load_reasons(ep).values()]
    except (OSError, ValueError):
        return []


def thumb_url(artifact: str, clip_id: str) -> str:
    from urllib.parse import quote
    return f"/api/room/thumb/{quote(artifact, safe='')}/{quote(clip_id, safe='')}"


def thumb(ep: Episode, clip_id: str) -> Path:
    """A still from the middle of a select, made once with ffmpeg and kept under work/room/thumbs/ (the name carries the time, so a
    clip moved by a nudge gets a fresh one)."""
    from .. import clips
    from ..review_pack import extract_frame, frame_source
    if not CLIP_RE.fullmatch(clip_id or ""):
        raise room_cut.NotFound("not found")
    data = _load_clips(ep) or {}
    c = next((x for x in data.get("clips") or [] if isinstance(x, dict) and x.get("id") == clip_id), None)
    if c is None:
        raise room_cut.NotFound("not found")
    src = c.get("source") or data.get("source") or {}
    try:
        doc = clips.load_stage_doc(ep, src.get("stage"), src.get("version"), src.get("name"))[0]
    except BaseException:
        raise room_cut.NotFound("not found") from None
    f, t = frame_source(doc, (float(c["start"]) + float(c["end"])) / 2)
    out = ep.work.joinpath(*THUMB_DIR) / f"{clip_id}_{int(round(t * 1000))}.jpg"
    if out.is_file() and out.stat().st_size:
        return out
    with _THUMBS:
        if not (out.is_file() and out.stat().st_size):
            err = extract_frame(ep.root / f, t, out)
            if err:
                raise room_cut.NotFound("no still yet")
            for old in out.parent.glob(f"{clip_id}_*.jpg"):      # a still from before a nudge moved the clip
                if old != out:
                    old.unlink(missing_ok=True)
    return out


def decide(ep: Episode, clip_id: str, verdict: str, person: str | None = None, *, reasons=None, note: str | None = None,
           actor: str = "human") -> dict:
    """The director's keep / maybe / skip (or `pending`, to undo one). The clip finder's own decision path writes clips.json (what
    `ave clips build` reads) and the learning ledger; the project log gets the decision as `actor`'s. `actor="claude"` (a request
    that only held the room token) is recorded by "claude" with source "relayed": never a human UI decision, so the shorts path
    does not take the clip as kept by a person."""
    from .. import clips
    from ..project import Project
    from ..project.adopt import home_artifact
    if verdict not in VERDICTS:
        raise ValueError(f"'verdict' must be one of {', '.join(VERDICTS)}")
    if not CLIP_RE.fullmatch(clip_id or ""):
        raise ValueError("'clip' must name a select")
    data = _load_clips(ep) or {}
    c = next((x for x in data.get("clips") or [] if isinstance(x, dict) and x.get("id") == clip_id), None)
    if c is None:
        raise room_cut.NotFound("That select is no longer in this project")
    from .. import learning
    if c.get("stale") and verdict in ("keep", "maybe"):
        raise StaleSelect("This select came from an older version of the cut, so it can only be skipped. Ask Claude to pick it again.")
    project = None
    if (ep.root / ".ave" / "project.json").is_file():
        project = Project.open(ep.root)
        person = person or project.person
    source = "ui" if actor == "human" else "relayed"
    if actor != "human":
        person = "claude"
    if reasons is not None and (not isinstance(reasons, list) or len(reasons) > 12 or not all(isinstance(r, str) for r in reasons)):
        raise ValueError("'reasons' must be a list of reason ids")
    if note is not None and (not isinstance(note, str) or len(note) > 2000):
        raise ValueError("'note' must be text under 2000 characters")
    item = {"id": clip_id, "decision": "pending"} if verdict == "pending" else {"id": clip_id, "verdict": verdict}
    if verdict != "pending":                            # the why, as the old clip desk wrote it: the taste ledger learns from it
        item.update(reasons=list(reasons or []), note=(note or "").strip() or None)
    with decide_lock(ep):                                # clips.decide reads and rewrites clips.json: one call at a time
        c = next((x for x in (_load_clips(ep) or {}).get("clips") or [] if isinstance(x, dict) and x.get("id") == clip_id), c)
        before = _verdict(c)
        try:
            got = clips.decide(ep, [item], name=c.get("session"), by=person, source=source)
        except SystemExit as e:
            raise ValueError(str(e)) from None
        if got["errors"]:
            raise ValueError(got["errors"][0]["error"])
        if verdict == "pending" and before:              # taking a call back: the learning ledger forgets it too
            learning.withdraw_decision(ep, clip_id, by=person, source=source)
        after = next((x for x in (_load_clips(ep) or {}).get("clips") or [] if isinstance(x, dict) and x.get("id") == clip_id), c)
    if project is not None:
        project.log.append("clip.decided", artifact=home_artifact(ep.root), actor=actor,
                           data={"clip": clip_id, "choice": verdict, "title": c.get("title")})
    return {"clip": clip_id, "verdict": _verdict(after), "state": _state(after), "origin": "claude" if after.get("origin") == "agent" else "finder",
            "title": after.get("title"), "logged": project is not None}


class StaleSelect(ValueError):
    pass


def nudge(ep: Episode, clip_id: str, moves: dict, person: str | None = None, *, actor: str = "human") -> dict:
    """The director moves a select's start or end by whole sentences (the old clip desk's nudge): the clip finder's own `decide`
    path, so the range is re-checked (inside the transcript, a complete length, no overlap) and the select goes back to undecided,
    to be kept again on its new range."""
    from .. import clips
    from ..project import Project
    from ..project.adopt import home_artifact
    if not CLIP_RE.fullmatch(clip_id or ""):
        raise ValueError("'clip' must name a select")
    if (not isinstance(moves, dict) or not moves or not set(moves) <= {"start", "end"}
            or not all(isinstance(v, int) and not isinstance(v, bool) and -6 <= v <= 6 and v for v in moves.values())):
        raise ValueError("Move the start or the end by 1 to 6 sentences")
    data = _load_clips(ep) or {}
    c = next((x for x in data.get("clips") or [] if isinstance(x, dict) and x.get("id") == clip_id), None)
    if c is None:
        raise room_cut.NotFound("That select is no longer in this project")
    if c.get("stale"):
        raise StaleSelect("This select came from an older version of the cut, so it cannot be moved. Ask Claude to pick it again.")
    project = Project.open(ep.root) if (ep.root / ".ave" / "project.json").is_file() else None
    person = person or (project.person if project else None)
    if actor != "human":                                 # a room-token request: Claude's (see decide)
        person = "claude"
    with decide_lock(ep):
        try:
            got = clips.decide(ep, [{"id": clip_id, "decision": "nudge", "nudge": moves}], name=c.get("session"), by=person,
                               source="ui" if actor == "human" else "relayed")
        except SystemExit as e:
            raise ValueError(str(e)) from None
        if got["errors"]:
            raise ValueError(got["errors"][0]["error"])
        after = next(x for x in (_load_clips(ep) or {}).get("clips") or [] if isinstance(x, dict) and x.get("id") == clip_id)
    if project is not None:
        project.log.append("clip.decided", artifact=home_artifact(ep.root), actor=actor,
                           data={"clip": clip_id, "choice": "nudge", "moves": moves, "title": c.get("title")})
    return {"clip": clip_id, "start": after["start"], "end": after["end"], "duration": after.get("duration"),
            "verdict": _verdict(after), "state": _state(after), "title": after.get("title")}


def decide_lock(ep: Episode):
    """The clip finder's own lock (clips.decide_lock): one decision at a time per episode across every process; reentrant."""
    from .. import clips
    return clips.decide_lock(ep)
