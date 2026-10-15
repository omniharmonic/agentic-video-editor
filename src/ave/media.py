"""Probe raw footage with ffprobe and classify each file."""

from __future__ import annotations

import json
import math
import subprocess
from datetime import datetime
from fractions import Fraction
from pathlib import Path

import numpy as np

from . import config
from .episode import Episode, read_json, write_json


def _ffprobe(path: Path) -> dict:
    ffprobe = config.binary("ffprobe")
    if not ffprobe:
        raise SystemExit("ffprobe not found on PATH (install ffmpeg)")
    out = subprocess.run(
        [str(ffprobe), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)


def _fps(stream: dict) -> float | None:
    rate = stream.get("avg_frame_rate") or stream.get("r_frame_rate")
    try:
        f = Fraction(rate)
        return float(f) if f > 0 else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def probe_file(path: Path, root: Path) -> dict:
    info = _ffprobe(path)
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic") != 1), None)
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    fmt = info.get("format", {})
    tags = {k.lower(): v for k, v in fmt.get("tags", {}).items()}
    # Start timecode lives on the format, the video stream, or a data stream depending on the camera.
    timecode = tags.get("timecode") or next(
        (s.get("tags", {}).get("timecode") for s in streams if s.get("tags", {}).get("timecode")), None
    )
    return {
        "file": str(path.relative_to(root)),
        "kind": "video" if video else ("audio" if audio else "other"),
        "duration": float(fmt.get("duration") or 0),
        "size_bytes": int(fmt.get("size") or 0),
        "video": None if not video else {
            "codec": video.get("codec_name"),
            "width": video.get("width"),
            "height": video.get("height"),
            "fps": _fps(video),
            "rotation": next((sd.get("rotation") for sd in video.get("side_data_list", []) if "rotation" in sd), 0),
        },
        "audio": [
            {"index": s.get("index"), "codec": s.get("codec_name"), "channels": s.get("channels"),
             "sample_rate": int(s.get("sample_rate") or 0)}
            for s in audio
        ],
        "start_timecode": timecode,
        "creation_time": tags.get("creation_time"),
    }


# ---- channels (N4): a stereo file can have ONE live channel, and which one can change mid-file ------------------------------

CHANNEL_WINDOW_S = 2.0       # level window. Finer than the ~30 s one might expect so a mid-file switch is located, not smeared
DEAD_DB = 15.0               # a channel this far below the other is dead
QUIET_BELOW_P90_DB = 15.0    # windows quieter than this under the file's speech level say nothing reliable about channels
ENVELOPE_HOP_S = 0.01        # resolution of the per-channel envelopes used to label words by channel
MIN_RUN_S = 10.0             # a run of one verdict shorter than this between different neighbours is a flicker, not a change


def channel_scan(path: Path, stream: int, window_s: float | None = None) -> tuple[np.ndarray, float]:
    """(levels, correlation) for the first two channels of audio stream `stream`.

    levels: per-window RMS in dB (n_windows x 2), decoded at 8 kHz and streamed through a pipe (a 2 h stereo file never
    sits in memory). Levels are what a listener hears, not a mixdown. correlation: Pearson correlation of the two channels
    over the WHOLE file (accumulated sums), the evidence for "two independent mics" vs "one signal on two channels"."""
    win = float(window_s or CHANNEL_WINDOW_S)
    ffmpeg = config.binary("ffmpeg")
    if not ffmpeg:
        raise SystemExit("ffmpeg not found on PATH")
    sr = 8000
    per = int(win * sr)
    proc = subprocess.Popen([str(ffmpeg), "-nostdin", "-v", "error", "-i", str(path), "-vn", "-map", f"0:a:{stream}",
                             "-af", "pan=stereo|c0=c0|c1=c1", "-ar", str(sr), "-f", "f32le", "-"], stdout=subprocess.PIPE)
    out: list[tuple[float, float]] = []
    sxx = syy = sxy = 0.0
    try:
        while True:
            raw = proc.stdout.read(per * 2 * 4)
            if not raw:
                break
            x = np.frombuffer(raw[: len(raw) // 8 * 8], np.float32).reshape(-1, 2).astype(np.float64)
            if len(x) < per // 4 and out:        # a stub at the end: fold it into the last window's verdict
                break
            out.append(tuple(20 * np.log10(np.sqrt((x ** 2).mean(axis=0)) + 1e-9)))
            sxx += float((x[:, 0] ** 2).sum()); syy += float((x[:, 1] ** 2).sum()); sxy += float((x[:, 0] * x[:, 1]).sum())
    finally:
        proc.stdout.close()
        proc.wait()
    corr = sxy / np.sqrt(sxx * syy) if sxx > 0 and syy > 0 else 0.0
    return (np.array(out) if out else np.zeros((0, 2))), float(corr)


def channel_levels(path: Path, stream: int, window_s: float | None = None) -> np.ndarray:
    """Per-window RMS in dB of the first two channels (see `channel_scan`)."""
    return channel_scan(path, stream, window_s)[0]


def channel_envelopes(path: Path, stream: int = 0, hop_s: float | None = None) -> tuple[np.ndarray, np.ndarray]:
    """RMS envelope (linear, one value per `hop_s`, default 10 ms) of the LEFT and RIGHT channel of audio stream `stream`.

    WHY: speaker-per-channel transcripts label each word by which mic is louder over the word's span; this is computed
    once per file (8 kHz decode, streamed) and indexed per word. Memory: 2 floats per 10 ms (about 14 MB for 2 hours)."""
    hop = float(hop_s or ENVELOPE_HOP_S)
    ffmpeg = config.binary("ffmpeg")
    if not ffmpeg:
        raise SystemExit("ffmpeg not found on PATH")
    sr = 8000
    per = max(1, int(round(hop * sr)))
    block = per * 6000
    proc = subprocess.Popen([str(ffmpeg), "-nostdin", "-v", "error", "-i", str(path), "-vn", "-map", f"0:a:{stream}",
                             "-af", "pan=stereo|c0=c0|c1=c1", "-ar", str(sr), "-f", "f32le", "-"], stdout=subprocess.PIPE)
    chunks: list[np.ndarray] = []
    carry = np.zeros((0, 2), np.float32)
    try:
        while True:
            raw = proc.stdout.read(block * 8)
            if not raw:
                break
            x = np.concatenate([carry, np.frombuffer(raw[: len(raw) // 8 * 8], np.float32).reshape(-1, 2)])
            k = len(x) // per
            if k:
                chunks.append(np.sqrt((x[: k * per].astype(np.float64) ** 2).reshape(k, per, 2).mean(axis=1)))
            carry = x[k * per:]
    finally:
        proc.stdout.close()
        proc.wait()
    if len(carry):
        chunks.append(np.sqrt((carry.astype(np.float64) ** 2).mean(axis=0, keepdims=True)))
    env = np.concatenate(chunks) if chunks else np.zeros((0, 2))
    return env[:, 0], env[:, 1]


def derive_channel_map(levels: np.ndarray, win_s: float, duration: float) -> list[dict]:
    """Per-window (left_db, right_db) -> ranges in the FILE's clock: {start, end, use: left|right|mix, left_db, right_db}.

    A channel >= 15 dB below the other is dead, so `use` is the live one; otherwise both are live (`mix`). Windows far
    below the file's speech level (pauses, dead space) cannot tell, so they inherit their neighbour (a switch inside a
    silence lands in it). Runs shorter than MIN_RUN_S wedged between different neighbours are absorbed."""
    n = len(levels)
    if n == 0:
        return [{"start": 0.0, "end": round(duration, 3), "use": "mix", "left_db": None, "right_db": None}]
    mx = levels.max(axis=1)
    loud = mx >= np.percentile(mx, 90) - QUIET_BELOW_P90_DB
    verdict: list[str | None] = []
    for (l, r), ok in zip(levels, loud):
        verdict.append(None if not ok else "right" if l <= r - DEAD_DB else "left" if r <= l - DEAD_DB else "mix")
    known = [i for i, v in enumerate(verdict) if v]
    if not known:
        verdict = ["mix"] * n
    else:
        # run boundaries between two known verdicts that differ go to the middle of the unknown stretch between them
        label = list(verdict)
        for a, b in zip(known, known[1:]):
            gap = range(a + 1, b)
            mid = a + 1 + len(gap) // 2
            for i in gap:
                label[i] = verdict[a] if i < mid else verdict[b]
        for i in range(known[0]):
            label[i] = verdict[known[0]]
        for i in range(known[-1] + 1, n):
            label[i] = verdict[known[-1]]
        verdict = label
    runs: list[list] = []                      # [use, first_window, last_window]
    for i, v in enumerate(verdict):
        if runs and runs[-1][0] == v:
            runs[-1][2] = i
        else:
            runs.append([v, i, i])
    min_w = max(1, int(round(MIN_RUN_S / win_s)))
    changed = True
    while changed and len(runs) > 1:
        changed = False
        for k, (u, a, b) in enumerate(runs):
            if b - a + 1 < min_w:
                nb = [j for j in (k - 1, k + 1) if 0 <= j < len(runs)]
                j = max(nb, key=lambda j: runs[j][2] - runs[j][1])
                runs[j][1], runs[j][2] = min(runs[j][1], a), max(runs[j][2], b)
                del runs[k]
                changed = True
                break
    merged: list[list] = []
    for u, a, b in sorted(runs, key=lambda r: r[1]):
        if merged and merged[-1][0] == u:
            merged[-1][2] = b
        else:
            merged.append([u, a, b])
    out = []
    for u, a, b in merged:
        seg = levels[a:b + 1]
        live = loud[a:b + 1] if loud[a:b + 1].any() else np.ones(len(seg), bool)
        out.append({"start": round(a * win_s, 3), "end": round(min((b + 1) * win_s, duration), 3), "use": u,
                    "left_db": round(float(seg[live, 0].mean()), 1), "right_db": round(float(seg[live, 1].mean()), 1)})
    out[0]["start"], out[-1]["end"] = 0.0, round(duration, 3)
    return out


# layout thresholds (T11). Tunable here, recorded with every verdict in `channel_evidence`
INDEPENDENT_CORR = 0.3       # |corr| below this: two different signals (two mics), not one signal on two channels
DOMINANT_DB = 6.0            # a channel this far above the other "owns" the window (a host talking into their own mic)
MIN_SHARE = 0.10             # each channel must own at least this share of the loud windows ...
MIN_ALTERNATIONS = 4         # ... and ownership must change hands at least this often


def classify_layout(levels: np.ndarray, corr: float, win_s: float, duration: float) -> dict:
    """Classify a stereo file from the WHOLE file (never from spot checks): {channel_layout, channel_map, evidence}.

    speaker_per_channel: independent channels (|corr| < 0.3) AND each channel dominant (>= 6 dB over the other) in >= 10% of
        the loud windows AND ownership alternates >= 4 times: two mics, one host each. One range, `use: "sum"`.
    dead_channel: a channel >= 15 dB down for stretches, without the alternating-independent pattern: the old fold-to-the-
        live-channel map (`derive_channel_map`).
    stereo_mix: neither; both channels live (map is all `mix`)."""
    base = derive_channel_map(levels, win_s, duration)
    n = len(levels)
    if n == 0:
        return {"channel_layout": "stereo_mix", "channel_map": base, "evidence": {"windows": 0}}
    mx = levels.max(axis=1)
    loud = mx >= np.percentile(mx, 90) - QUIET_BELOW_P90_DB
    diff = (levels[:, 0] - levels[:, 1])[loud]
    owner = np.where(diff >= DOMINANT_DB, 1, np.where(diff <= -DOMINANT_DB, -1, 0))
    n_loud = max(1, int(loud.sum()))
    left_share, right_share = float((owner == 1).sum()) / n_loud, float((owner == -1).sum()) / n_loud
    decided = owner[owner != 0]
    alternations = int((np.diff(decided) != 0).sum()) if len(decided) > 1 else 0
    dead_share = float((np.abs(diff) >= DEAD_DB).sum()) / n_loud
    ev = {"correlation": round(float(corr), 3), "left_dominant_share": round(left_share, 3), "right_dominant_share": round(right_share, 3),
          "alternations": alternations, "dead_share": round(dead_share, 3), "windows": n, "window_s": win_s,
          "thresholds": {"independent_corr": INDEPENDENT_CORR, "dominant_db": DOMINANT_DB, "min_share": MIN_SHARE,
                         "min_alternations": MIN_ALTERNATIONS, "dead_db": DEAD_DB}}
    if abs(corr) < INDEPENDENT_CORR and left_share >= MIN_SHARE and right_share >= MIN_SHARE and alternations >= MIN_ALTERNATIONS:
        live = loud if loud.any() else np.ones(n, bool)
        cm = [{"start": 0.0, "end": round(duration, 3), "use": "sum",
               "left_db": round(float(levels[live, 0].mean()), 1), "right_db": round(float(levels[live, 1].mean()), 1)}]
        return {"channel_layout": "speaker_per_channel", "channel_map": cm, "evidence": ev}
    if any(m["use"] in ("left", "right") for m in base):
        return {"channel_layout": "dead_channel", "channel_map": base, "evidence": ev}
    return {"channel_layout": "stereo_mix", "channel_map": base, "evidence": ev}


def analyze_channels(path: Path, item: dict) -> dict | None:
    """Layout verdict for the first audio stream of a probed file (the one extraction and rendering read); None if no audio.

    Mono files return `{"channel_layout": "mono", "channel_map": None}`."""
    a = item["audio"][0] if item["audio"] else None
    if not a:
        return None
    if (a.get("channels") or 0) < 2:
        return {"channel_layout": "mono", "channel_map": None, "evidence": {}}
    levels, corr = channel_scan(path, 0)
    return classify_layout(levels, corr, CHANNEL_WINDOW_S, item["duration"])


def dead_channel_warning(item: dict) -> str | None:
    cm = item.get("channel_map")
    dead = [m for m in cm or [] if m["use"] in ("left", "right")]
    if not dead or item.get("channel_layout") == "speaker_per_channel":
        return None
    parts = [f"{'right' if m['use'] == 'left' else 'left'} channel dead {m['start']:.0f}-{m['end']:.0f}s (live: {m['use']}, "
             f"L {m['left_db']} dB / R {m['right_db']} dB)" for m in dead]
    return (f"{item['file']}: " + "; ".join(parts) + ". Analysis audio uses the live channel and renders fold it to dual mono; "
            "verify by listening to a render of each range.")


SESSION_CREATION_TOL_S = 120.0   # two cameras of one take start within this of each other (the real pair matched to the second)
SESSION_DURATION_TOL_S = 2.0     # ... and stop within this: the same take is the same length on every angle


def _ct(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def cluster_sessions(items: list[dict]) -> list[dict]:
    """Group VIDEO files into sessions (takes): same `creation_time` (within 120 s) and similar duration (within 2 s).

    WHY: one recorder file can span several takes (field note N1), and each take is its own timeline of cameras. A file
    without a creation_time cannot be compared by clock, so it matches on duration alone (the caller warns about that).
    Ids are s1, s2... in creation-time order. `label` is for the human ("experimental"); re-ingest preserves it."""
    vids = [i for i in items if i["kind"] == "video"]
    vids.sort(key=lambda i: (_ct(i.get("creation_time")) if _ct(i.get("creation_time")) is not None else float("inf"), i["file"]))
    groups: list[list[dict]] = []
    for v in vids:
        for g in groups:
            a, b = _ct(g[0].get("creation_time")), _ct(v.get("creation_time"))
            clock_ok = a is None or b is None or abs(a - b) <= SESSION_CREATION_TOL_S
            if clock_ok and abs(g[0]["duration"] - v["duration"]) <= SESSION_DURATION_TOL_S:
                g.append(v)
                break
        else:
            groups.append([v])
    return [{"id": f"s{n}", "files": sorted(i["file"] for i in g), "creation_time": g[0].get("creation_time"),
             "duration": round(max(i["duration"] for i in g), 3), "label": None} for n, g in enumerate(groups, 1)]


def session_report(items: list[dict], sessions: list[dict]) -> dict:
    """Recorder candidates (audio-only files) and the sanity check "do these sessions belong to this audio": the sum of
    session durations against each recorder's duration. A recorder longer than any ONE session may span several."""
    total = sum(s["duration"] for s in sessions)
    longest = max((s["duration"] for s in sessions), default=0.0)
    recs, warnings = [], []
    for i in items:
        if i["kind"] != "audio":
            continue
        spans = bool(sessions) and i["duration"] > longest + SESSION_DURATION_TOL_S
        recs.append({"file": i["file"], "duration": i["duration"], "channels": (i["audio"][0]["channels"] if i["audio"] else None),
                     "spans_sessions": spans, "sum_session_durations": round(total, 3), "difference_s": round(i["duration"] - total, 3)})
        if spans:
            warnings.append(
                f"{i['file']} ({i['duration']:.1f}s) is longer than any one session (longest {longest:.1f}s), so it may span "
                f"sessions: sessions total {total:.1f}s vs audio {i['duration']:.1f}s (difference {i['duration'] - total:+.1f}s = "
                "lead-in, dead space and tail on no camera). A difference far from a few minutes means the audio and video may not belong together.")
    for s in sessions:
        if any(not _ct(next(i for i in items if i["file"] == f).get("creation_time")) for f in s["files"]) and len(s["files"]) > 1:
            warnings.append(f"session {s['id']}: a file has no creation_time, so it was grouped by duration alone. Check it is the right take.")
    return {"recorders": recs, "warnings": warnings}


def ingest(ep: Episode) -> dict:
    files = ep.media_files()
    if not files:
        raise SystemExit(f"No media found in {ep.raw}. Drop footage there first.")
    items = [probe_file(f, ep.root) for f in files]
    chan_warn: list[str] = []
    info: list[str] = []
    for f, it in zip(files, items):
        try:
            res = analyze_channels(f, it)
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            chan_warn.append(f"{it['file']}: could not analyse channels ({e}); assuming both are live")
            res = None
        if res is None:
            continue
        it["channel_layout"] = res["channel_layout"]
        if res["channel_map"] is None:
            continue
        it["channel_map"] = res["channel_map"]
        it["channel_evidence"] = res["evidence"]
        if res["channel_layout"] == "speaker_per_channel":
            ev = res["evidence"]
            info.append(f"{it['file']}: two mics, one host per channel: summing, speaker labels available from channels "
                        f"(corr {ev['correlation']}, left {ev['left_dominant_share']:.0%} / right {ev['right_dominant_share']:.0%} dominant, "
                        f"{ev['alternations']} alternations)")
        else:
            w = dead_channel_warning(it)
            if w:
                chan_warn.append(w)
    sessions = cluster_sessions(items)
    rep = session_report(items, sessions)
    prev = ep.artifact("media.json")
    if prev.exists():   # keep human labels on sessions that still hold the same files
        try:
            old = {tuple(s["files"]): s.get("label") for s in read_json(prev).get("sessions", [])}
            for s in sessions:
                s["label"] = old.get(tuple(s["files"]))
        except (OSError, ValueError, KeyError):
            pass
    result = {"episode": ep.root.name, "files": items, "sessions": sessions, "recorders": rep["recorders"],
              "info": info, "warnings": _warnings(items) + rep["warnings"] + chan_warn}
    write_json(ep.artifact("media.json"), result)
    ep.mark_stage("ingest", files=len(items))
    return result


def _warnings(items: list[dict]) -> list[str]:
    warnings = []
    videos = [i for i in items if i["kind"] == "video"]
    fpss = {round(v["video"]["fps"], 2) for v in videos if v["video"] and v["video"]["fps"]}
    if len(fpss) > 1:
        warnings.append(f"Mixed frame rates across cameras: {sorted(fpss)}. Conform in the Resolve timeline.")
    for i in items:
        if i["kind"] == "video" and not i["audio"]:
            warnings.append(f"{i['file']}: no audio track, so it cannot be synced by audio.")
        if i["kind"] == "other":
            warnings.append(f"{i['file']}: no audio or video streams found.")
    return warnings
