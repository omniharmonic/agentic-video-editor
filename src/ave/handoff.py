"""Hand an EditDoc to DaVinci Resolve for human review and polish.

Resolve is the review surface, not the engine. This module is PURE planning: EditDoc -> a placement plan the
Resolve script (scripts/resolve_handoff.py) executes through the MCP bridge. Every handoff creates a NEW timeline
(named with stage and version); it never touches an existing one, so the human's tweaks are never overwritten.

Resolve facts baked in (measured on 21.0.4 free, see skills/ave-rough-cut):
  - AppendToTimeline endFrame is EXCLUSIVE.
  - Timelines start at 01:00:00:00; record frames are relative to the timeline start.
  - Free cannot fade/transition/retime via API, so audio is handed over as the COMPILED dialogue master (crossfades baked in)
    and video as hard cuts. Subtitles come from our SRT.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping

from . import editdoc
from .episode import Episode, read_json

V_TRACK_MAIN = 1
A_TRACK_DIALOGUE = 1
A_TRACK_MUSIC = 2
A_TRACK_BAKED = 2          # layered handoff only: the consolidated master, muted, next to the editable dialogue clips
V_TRACK_ALT0 = 2           # layered handoff only: first alternate-angle track
LAYERS_NOTE = ("Layers: audio-only appends are frame-granular (cut points on the dialogue clips are snapped to whole timeline frames, "
               "1/fps s) and carry no crossfades; the baked master on A2 keeps the sample-accurate crossfades. Alternate-angle tracks "
               "are DISABLED after placement so playback shows the edit.")


def srt_cue_count(srt: Path) -> int:
    """Number of cues in an SRT (blocks holding a `-->` timing line)."""
    try:
        return len(re.findall(r"^\s*\d{1,2}:\d\d:\d\d[,.]\d{1,3}\s*-->", srt.read_text(encoding="utf-8", errors="replace"), re.M))
    except OSError:
        return 0


def _frames(o0: float, o1: float, fps: float) -> tuple[int, int]:
    """(record_frame, frame_count) using consecutive rounding so adjacent segments abut exactly."""
    a, b = round(o0 * fps), round(o1 * fps)
    return a, max(1, b - a)


def _abs(ep: Episode, f: str | Path) -> str:
    p = Path(f)
    return str(p if p.is_absolute() else (ep.root / p).resolve())


def episode_title(ep: Episode) -> str:
    """The creator's episode title (episode.json `title`), not the folder name: "00" says nothing in a Resolve bin."""
    try:
        return str(read_json(ep.meta_path).get("title") or ep.root.name)
    except Exception:
        return ep.root.name


def timeline_name(ep: Episode, doc: dict, version: int | None, label: str | None = None) -> str:
    v = f" v{version}" if version else ""
    return f"{episode_title(ep)} {label or doc['stage']}{v}"


def sum_wav_path(ep: Episode, doc: dict, src_key: str) -> Path:
    """Where the dual-mono sum of source `src_key` lives (built once by `compile.compile_source_sum`, then reused). Keyed by the source
    key AND its file stem, so a re-mapped source never picks up another file's sum."""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(doc["sources"][src_key]["file"]).stem)
    return ep.work / "render" / f"{src_key}.{stem}.sum.wav"


def _media_durations(ep: Episode) -> dict[str, float]:
    """file (as stored in the doc, relative to the episode root) -> duration in seconds, from media.json when ingest has run."""
    try:
        return {f["file"]: float(f["duration"]) for f in read_json(ep.artifact("media.json")).get("files", []) if f.get("duration")}
    except Exception:
        return {}


def kept_ranges(doc: dict) -> list[dict]:
    """The edit's KEPT RANGES: video segments merged when they are contiguous both in output time and in EPISODE time (within 1 ms), so
    ordinary cuts and camera switches stay inside one range and only removed sections split it. {o0, o1, e0, ids}."""
    out: list[dict] = []
    for s in sorted(doc["tracks"]["video"], key=lambda x: x["o0"]):
        last = out[-1] if out else None
        if last and abs(last["o1"] - s["o0"]) <= editdoc.EPS and abs(last["e0"] + (last["o1"] - last["o0"]) - s["e0"]) <= editdoc.EPS:
            last["o1"] = s["o1"]
            last["ids"].append(s["id"])
        else:
            out.append({"o0": s["o0"], "o1": s["o1"], "e0": s["e0"], "ids": [s["id"]]})
    return out


def _track_gaps(items: list[dict]) -> int:
    ordered = sorted(items, key=lambda i: i["record_frame"])
    return sum(1 for a, b in zip(ordered, ordered[1:]) if b["record_frame"] > a["record_frame"] + a["frames"])


def _layers(ep: Episode, doc: dict, fps: float, total: int, n_video: int, audio_master: Path | None, sum_wavs: Mapping[str, Path] | None,
            warnings: list[str]) -> dict:
    """The extra tracks of the layered handoff (see `plan`). Pure: only reads media.json for camera durations."""
    src = doc["sources"]
    durations = _media_durations(ep)
    cams = sorted(k for k, v in src.items() if v.get("kind") == "video")
    label = {k: (src[k].get("role") or k) for k in cams}
    for k in cams:                                              # two cameras with one role must still get distinct track names
        if sum(1 for j in cams if label[j] == label[k]) > 1:
            label[k] = f"{label[k]} {k}"
    ranges = kept_ranges(doc)

    alt: list[dict] = []
    track_of: dict[str, int] = {}
    for cam in cams:
        file_abs = _abs(ep, src[cam]["file"])
        names = sorted({Path(src[cam]["file"]).name, Path(file_abs).name})
        avail = durations.get(src[cam]["file"])
        avail_f = int(avail * fps + 1e-6) if avail else None
        items: list[dict] = []
        for r_i, r in enumerate(ranges, 1):
            rec, n = _frames(r["o0"], r["o1"], fps)
            start = round(editdoc.source_time(doc, cam, r["e0"]) * fps)
            lead = max(0, -start)                               # the camera started after this range did
            if lead:
                rec, start, n = rec + lead, 0, n - lead
            tail = max(0, start + n - avail_f) if avail_f is not None and n > 0 else 0
            n -= tail
            if n <= 0:
                warnings.append(f"camera {cam} ({label[cam]}) was not recording during kept range {r_i} "
                                f"(episode {r['e0']:.2f}-{r['e0'] + r['o1'] - r['o0']:.2f} s): no {label[cam]} (alt) clip there")
                continue
            if lead or tail:
                warnings.append(f"camera {cam} ({label[cam]}) covers only part of kept range {r_i}: its alternate clip is trimmed by "
                                f"{lead / fps:.2f} s at the start (camera starts into the range) and {tail / fps:.2f} s at the end")
            items.append({"id": f"r{r_i}.{cam}", "camera": cam, "file": file_abs, "names": names, "start_frame": start,
                          "end_frame": start + n, "record_frame": rec, "frames": n, "media_type": 1, "range": r_i,
                          "reason": f"{label[cam]} at the same moment as the edit ({', '.join(r['ids'])})"})
        if items:
            track_of[cam] = V_TRACK_ALT0 + len(track_of)
            for it in items:
                it["track_index"] = track_of[cam]
            alt += items
        else:
            warnings.append(f"camera {cam} ({label[cam]}) covers no kept range: no alternate track for it")

    # Dialogue as editable clips, from the dual-mono sum of each source. Clips butt together on the timeline (exclusive ends).
    wavs = dict(sum_wavs or {})

    def wav(k: str) -> str:
        return str((Path(wavs[k]) if k in wavs else sum_wav_path(ep, doc, k)).resolve())

    clips: list[dict] = []
    room = doc["tracks"].get("room_tone")
    cursor = 0
    segs = sorted(doc["tracks"]["dialogue"], key=lambda x: x["o0"])
    for i, s in enumerate(segs):
        a, b = round(s["o0"] * fps), round(s["o1"] * fps)
        a = max(a, cursor)                                      # an xfade overlap must not make two clips collide on one track
        if i == len(segs) - 1:
            b = max(b, total) if abs(b - total) <= 1 else b     # the last clip ends where the video ends (frame rounding)
        n = b - a
        if n <= 0:
            warnings.append(f"dialogue segment {s['id']} is shorter than one frame at {fps:g} fps: no clip for it")
            continue
        if s.get("fill") == "room_tone":
            if not room:
                raise ValueError(f"dialogue segment {s['id']} uses fill 'room_tone' but the doc has no tracks.room_tone")
            r0 = round(editdoc.source_time(doc, room["src"], room["e0"]) * fps)
            rlen = max(1, round(editdoc.source_time(doc, room["src"], room["e1"]) * fps) - r0)
            left, pos, part = n, a, 1
            while left > 0:
                m = min(left, rlen)
                clips.append({"id": f"{s['id']}.{part}", "file": wav(room["src"]), "start_frame": r0, "end_frame": r0 + m,
                              "record_frame": pos, "frames": m, "track_index": A_TRACK_DIALOGUE, "media_type": 2, "kind": "room_tone"})
                pos, left, part = pos + m, left - m, part + 1
        else:
            start = max(0, round(editdoc.source_time(doc, s["src"], s["e0"]) * fps))
            clips.append({"id": s["id"], "file": wav(s["src"]), "start_frame": start, "end_frame": start + n, "record_frame": a,
                          "frames": n, "track_index": A_TRACK_DIALOGUE, "media_type": 2, "src": s["src"], "reason": s.get("reason")})
        cursor = b
    if clips and (clips[0]["record_frame"] != 0 or cursor != total):
        warnings.append(f"dialogue clips cover frames {clips[0]['record_frame']}-{cursor} of {total}: the video and dialogue tracks differ")

    baked = None
    if audio_master is not None:
        baked = {"id": "dialogue-master", "file": str(Path(audio_master).resolve()), "start_frame": 0, "end_frame": total, "record_frame": 0,
                 "frames": total, "track_index": A_TRACK_BAKED, "media_type": 2,
                 "unit": "master wav at the timeline fps (frames == timeline frames)"}
    else:
        warnings.append("No compiled dialogue master: the layered timeline has no baked master on A2 (render the audio first to add one).")

    video_tracks = {"1": {"name": "Edit", "disabled": False}}
    for cam, t in track_of.items():
        video_tracks[str(t)] = {"name": f"{label[cam]} (alt)", "disabled": True, "camera": cam}
    audio_tracks = {"1": {"name": "Dialogue (editable clips)", "disabled": False}}
    if baked:
        audio_tracks["2"] = {"name": "Baked master (muted)", "disabled": True}
    counts_v = {"1": n_video}
    for it in alt:
        counts_v[str(it["track_index"])] = counts_v.get(str(it["track_index"]), 0) + 1
    counts_a = {"1": len(clips)} | ({"2": 1} if baked else {})
    frames = {str(t): sum(i["frames"] for i in alt if i["track_index"] == t) for t in track_of.values()}
    gaps = {str(t): _track_gaps([i for i in alt if i["track_index"] == t]) for t in track_of.values()}
    return {"alt_video": alt, "audio_clips": clips, "baked_master": baked,
            "tracks": {"video": video_tracks, "audio": audio_tracks},
            "counts": {"video": counts_v, "audio": counts_a, "alt_frames": frames, "alt_gaps": gaps},
            "sum_wavs": sorted({c["file"] for c in clips}), "note": LAYERS_NOTE}


def plan(ep: Episode, doc: dict, *, name: str, audio_master: Path | None = None, srt: Path | None = None,
         project: str | None = None, layers: bool = False, sum_wavs: Mapping[str, Path] | None = None,
         proxies: Mapping[str, str] | None = None, try_subtitles: bool = False, srt_copy: Path | None = None) -> dict:
    """EditDoc -> placement plan. `layers=True` (the work-in-progress handoff) adds a `layers` block and moves the audio:
      V1 the edit; V2.. one DISABLED alternate-angle track per camera following the edit's kept ranges (in sync, drift-corrected);
      A1 dialogue as separate clips from the dual-mono sum WAV of each source (`sum_wavs`, default `sum_wav_path`); A2 the baked
      master, muted. Music and overlays move above those tracks. Without it the plan is exactly the consolidated one.
    `proxies` ({original abs path: proxy abs path}, from `proxies.proxy_map`) asks the script to LINK those proxies to the imported
    originals; every item in the plan still names the ORIGINAL file, so renders come from originals. `try_subtitles` (W7, UNVERIFIED in
    real Resolve) asks the script to attempt SRT-on-timeline; `srt_copy` is where `ave handoff` put a copy named after the timeline."""
    problems = editdoc.validate(doc)
    if problems:
        raise ValueError("cannot hand off an invalid EditDoc:\n  - " + "\n  - ".join(problems))
    fps = doc["fps"]
    src = doc["sources"]
    warnings: list[str] = []

    video = []
    for s in doc["tracks"]["video"]:
        rec, n = _frames(s["o0"], s["o1"], fps)
        start = round(editdoc.source_time(doc, s["src"], s["e0"]) * fps)
        if start < 0:
            raise ValueError(f"video {s['id']}: source frame {start} is before the start of {src[s['src']]['file']} "
                             f"(camera not recording at episode time {s['e0']:.2f}s)")
        # Resolve names a clip after the path it was imported under, which may be a symlink: accept either basename.
        names = sorted({Path(src[s["src"]]["file"]).name, Path(_abs(ep, src[s["src"]]["file"])).name})
        video.append({"id": s["id"], "file": _abs(ep, src[s["src"]]["file"]), "names": names, "start_frame": start,
                      "end_frame": start + n,                    # EXCLUSIVE, as Resolve's AppendToTimeline expects
                      "record_frame": rec, "frames": n, "track_index": V_TRACK_MAIN, "media_type": 1,
                      "zoom": s.get("zoom", 1.0), "crop": s.get("crop"), "reason": s.get("reason")})
    total = video[-1]["record_frame"] + video[-1]["frames"]

    audio = []
    if layers:
        pass                                                    # A1/A2 are built below from `layers`
    elif audio_master is not None:
        audio.append({"id": "dialogue-master", "file": str(Path(audio_master).resolve()), "start_frame": 0, "end_frame": total,
                      "record_frame": 0, "frames": total, "track_index": A_TRACK_DIALOGUE, "media_type": 2,
                      "unit": "master wav at the timeline fps (frames == timeline frames)"})
    else:
        warnings.append("No compiled dialogue master: audio is placed as hard-cut source segments with no crossfades. "
                        "Render the audio first (ave render ... --preset audio_only) for a clean handoff.")
        mapped = sorted({k for k in {d["src"] for d in doc["tracks"]["dialogue"] if d.get("src")}
                         if any(m["use"] != "mix" for m in src[k].get("channel_map") or [])})
        if mapped:     # Resolve would play the raw stereo file: one-eared for left/right ranges, a single mic for a sum range
            warnings.append(f"source(s) {', '.join(mapped)} have a channel_map (dead channel, or `sum` for a speaker-per-channel master): "
                            "the raw files play as recorded in Resolve. Hand over the compiled dialogue master (dual mono) instead.")
        for s in doc["tracks"]["dialogue"]:
            if s.get("fill") == "room_tone":
                continue
            rec, n = _frames(s["o0"], s["o1"], fps)
            start = max(0, round(editdoc.source_time(doc, s["src"], s["e0"]) * fps))
            audio.append({"id": s["id"], "file": _abs(ep, src[s["src"]]["file"]), "start_frame": start, "end_frame": start + n,
                          "record_frame": rec, "frames": n, "track_index": A_TRACK_DIALOGUE, "media_type": 2})

    layer_block = _layers(ep, doc, fps, total, len(video), audio_master, sum_wavs, warnings) if layers else None
    music_track = A_TRACK_BAKED + 1 if layers else A_TRACK_MUSIC
    overlay_shift = len(layer_block["tracks"]["video"]) - 1 if layers else 0

    music = []
    for m in doc["tracks"].get("music") or []:
        rec, n = _frames(m["o0"], m["o1"], fps)
        start = round(m.get("in", 0.0) * fps)
        music.append({"id": m["id"], "file": _abs(ep, m["file"]), "start_frame": start, "end_frame": start + n,
                      "record_frame": rec, "frames": n, "track_index": music_track, "media_type": 2, "gain_db": m.get("gain_db")})

    overlays = []
    for o in doc["tracks"].get("overlays") or []:
        rec, n = _frames(o["o0"], o["o1"], fps)
        overlays.append({"id": o["id"], "file": _abs(ep, o["file"]), "start_frame": 0, "end_frame": n, "record_frame": rec,
                         "frames": n, "track_index": max(2, int(o.get("layer", 1)) + 1) + overlay_shift, "media_type": 1, "kind": o.get("kind")})

    captions = doc.get("captions") or {}
    srt_path = srt or (ep.root / captions["srt"] if captions.get("srt") else None)
    if captions.get("burn"):
        warnings.append("EditDoc has burn=true captions; the Resolve timeline gets the SRT track instead of burned-in text.")

    extra = (layer_block["alt_video"] + layer_block["audio_clips"] + [layer_block["baked_master"]] * bool(layer_block["baked_master"])
             ) if layer_block else []
    imports = sorted({x["file"] for x in video + audio + music + overlays + extra})
    out = {"name": name, "project": project, "stage": doc["stage"], "fps": fps, "canvas": doc["canvas"], "timeline_frames": total,
            "import": imports, "video": video, "audio": audio, "music": music, "overlays": overlays,
            "subtitles": ({"srt": str(Path(srt_path).resolve()), "try": bool(try_subtitles), "cues": srt_cue_count(Path(srt_path)),
                           "copy": str(srt_copy) if srt_copy else None}
                          if srt_path and Path(srt_path).exists() else None),
            "warnings": warnings}
    if proxies:
        out["proxies"] = {f: p_ for f, p_ in proxies.items() if f in imports}
    if layer_block:
        out["layers"] = layer_block
    if doc.get("sequences"):       # opening/closing sequences of a final cut: clips on the timeline (isolated in ave.sequence)
        from . import sequence
        sequence.apply_to_handoff_plan(ep, out, doc["sequences"])
    return out
