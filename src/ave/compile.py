"""EditDoc -> media. The FFmpeg engine.

Timing contract (the reason this module exists):
  * VIDEO is frame-snapped. Every segment boundary is rounded to a frame with ONE rule (`ffutil.frame_of`), and each segment
    is encoded to exactly (f1 - f0) frames, so the picture has exactly round(duration * fps) frames: no drift, no gaps.
  * AUDIO is sample-accurate at 48 kHz. Segment positions are integer sample indices; sources are read by sample range
    (`atrim=start_sample`), never by fuzzy float seeks; the master has exactly round(duration * 48000) samples.
  * Source clock for any episode time e is  (e - start_offset) / (1 + drift_ppm*1e-6)  (see editdoc.source_time): every source
    position below comes from that ONE function, so a drifting camera still lands lips-on-audio at the end of a long take.

Design choices (WHY):
  * Video is built from PER-SEGMENT intermediates concatenated with the concat demuxer (stream copy), not one giant filter
    graph. A 300-segment filter graph has 300 inputs, is slow to build, and one bad segment kills the render; per-segment
    encodes are independent, run in parallel, are cached on disk during the run, and pinpoint a failing segment by id.
    Contiguous pieces of one source are merged first, so a plain assembly is only a handful of encodes.
  * Audio is mixed in numpy, streaming 10 s blocks. That gives exact crossfade shape/position, a loop-free room tone and a
    ducking envelope that lands exactly on `duck_db`, none of which ffmpeg's generic filters guarantee.
  * Loudness is two-pass `loudnorm` (linear mode when possible) and is verified by measuring the FINAL encoded file.
"""

from __future__ import annotations

import hashlib
import math
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from fractions import Fraction
from pathlib import Path

import numpy as np
import soundfile as sf

from . import config, editdoc, ffutil, presets
from .audio import soft_limit
from .episode import Episode

SR = ffutil.SAMPLE_RATE
BLOCK = 10 * SR                  # streaming block, samples
DUCK_THRESHOLD_DB = -45.0        # dialogue short-term level above which the music ducks (room tone sits well below)
DUCK_HOP = 480                   # 10 ms envelope resolution
DUCK_HOLD_HOPS = 30              # bridge pauses shorter than ~300 ms and start ducking ~150 ms before speech
DUCK_ATTACK_S = 0.06
DUCK_RELEASE_S = 0.25
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
LOUDNESS_TOLERANCE_LU = 1.0
TRUE_PEAK_LIMIT_DB = -1.0


SEGMENT_ATTEMPTS = 3     # tries per video segment before the render fails (see compile_video.encode)


class RenderError(RuntimeError):
    """A render finished but the output failed verification, or a stage could not produce valid media."""


# ---- small helpers ------------------------------------------------------------------------------------------------

def _samples(t: float) -> int:
    return int(math.floor(t * SR + 0.5 + 1e-9))


def _db(db: float) -> float:
    return 10.0 ** (db / 20.0)


def _even(x: float) -> int:
    return max(2, int(round(x / 2.0)) * 2)


def _path(ep: Episode, rel: str | Path) -> Path:
    p = Path(rel)
    return p if p.is_absolute() else ep.root / p


def _warn(warnings: list[str] | None, msg: str) -> None:
    if warnings is not None and msg not in warnings:
        warnings.append(msg)


def _preset(p: str | dict | None) -> dict:
    if p is None:
        return presets.get("master")
    return presets.get(p) if isinstance(p, str) else p


def _workdir(ep: Episode, label: str) -> Path:
    stamp = f"{label}-{os.getpid()}-{int(time.time() * 1000) % 10**9}"
    d = ep.work / "render" / stamp
    d.mkdir(parents=True, exist_ok=True)
    return d


def _drop_workdir(d: Path) -> None:
    shutil.rmtree(d, ignore_errors=True)
    try:
        d.parent.rmdir()          # remove work/render if nothing else is rendering
    except OSError:
        pass


def _check_files(ep: Episode, doc: dict, *, video: bool, audio: bool, burn: bool) -> None:
    """Fail up front, naming every missing file, instead of half-rendering."""
    need: dict[Path, str] = {}
    tr = doc["tracks"]
    used = set()
    if video:
        used |= {s["src"] for s in tr["video"]}
    if audio:
        used |= {s["src"] for s in tr["dialogue"] if s.get("src")}
        if tr.get("room_tone"):
            used.add(tr["room_tone"]["src"])
    for k in used:
        need[_path(ep, doc["sources"][k]["file"])] = f"source {k}"
    if audio:
        for m in tr.get("music") or []:
            need[_path(ep, m["file"])] = f"music {m.get('id')}"
    if video:
        for o in tr.get("overlays") or []:
            need[_path(ep, o["file"])] = f"overlay {o.get('id')}"
        if burn and (doc.get("captions") or {}).get("ass"):
            need[_path(ep, doc["captions"]["ass"])] = "captions.ass"
    missing = [f"{p} ({what})" for p, what in need.items() if not p.exists()]
    if missing:
        raise FileNotFoundError("missing media referenced by the EditDoc:\n  - " + "\n  - ".join(missing))


# ======================================================================================================================
# VIDEO
# ======================================================================================================================

def _display_size(info: dict, rotation: int = 0) -> tuple[int, int]:
    w, h = info["w"], info["h"]
    return (h, w) if abs(rotation) % 180 == 90 else (w, h)


def _rotation(path: Path) -> int:
    proc = subprocess.run([ffutil._bin("ffprobe"), "-v", "error", "-select_streams", "v:0", "-show_entries",
                           "stream_side_data=rotation:stream_tags=rotate", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    for tok in proc.stdout.replace("\n", ",").split(","):
        try:
            return int(float(tok))
        except ValueError:
            continue
    return 0


def _canvas_for(doc: dict, preset: dict) -> tuple[int, int]:
    """Output picture size: the doc canvas, or (proxy) the same aspect at preset height. Never upscales."""
    W, H = doc["canvas"]["w"], doc["canvas"]["h"]
    h = preset.get("height")
    if h and h < H:
        return _even(W * h / H), _even(h)
    return W, H


def _plan_video(doc: dict, fps: Fraction, warnings: list[str]) -> list[dict]:
    """Doc video segments -> frame-exact pieces [{src,f0,f1,e0,crop,zoom,ids}], merging pieces that are one continuous take."""
    segs = sorted(doc["tracks"]["video"], key=lambda s: s["o0"])
    total = ffutil.frame_of(doc["duration"], fps)
    pieces: list[dict] = []
    for i, s in enumerate(segs):
        f0 = ffutil.frame_of(s["o0"], fps) if i else 0
        f1 = ffutil.frame_of(segs[i + 1]["o0"], fps) if i + 1 < len(segs) else total
        if f1 <= f0:
            _warn(warnings, f"video segment {s['id']} is shorter than one frame at {float(fps):.3f} fps and was dropped")
            continue
        # the frame grid may sit up to half a frame after o0; the source must be sampled at the same instant
        e0 = s["e0"] + (f0 / fps - s["o0"]) if i else s["e0"]
        piece = {"src": s["src"], "f0": f0, "f1": f1, "e0": float(e0), "crop": s.get("crop"),
                 "zoom": float(s.get("zoom") or 1.0), "ids": [s["id"]]}
        prev = pieces[-1] if pieces else None
        if (prev and prev["src"] == piece["src"] and prev["crop"] == piece["crop"] and prev["zoom"] == piece["zoom"]
                and abs(prev["e0"] + (piece["f0"] - prev["f0"]) / fps - piece["e0"]) < 1e-4 and prev["f1"] == piece["f0"]):
            prev["f1"] = piece["f1"]
            prev["ids"].append(s["id"])
        else:
            pieces.append(piece)
    return pieces


def _segment_cmd(piece: dict, doc: dict, fps: Fraction, canvas: tuple[int, int], info: dict, rot: int, ep: Episode,
                 crf: int, x264: str, out: Path, warnings: list[str], hw: bool = False) -> list[str]:
    W, H = canvas
    src = doc["sources"][piece["src"]]
    n = piece["f1"] - piece["f0"]
    src_t = editdoc.src_time(src, piece["e0"])
    k = editdoc.clock_rate(src, piece["e0"])      # the camera clock is slow by k: stretch its picture so long pieces stay locked too
    sw, sh = _display_size(info, rot)
    cx, cy, cw, ch = (piece["crop"] or [0, 0, sw, sh])
    cx, cy = max(0, cx), max(0, cy)
    cw, ch = min(cw, sw - cx), min(ch, sh - cy)
    z = max(1.0, piece["zoom"])
    if z > 1.0:      # centered punch-in applied after the crop
        zw, zh = cw / z, ch / z
        cx, cy, cw, ch = cx + (cw - zw) / 2, cy + (ch - zh) / 2, zw, zh
    cw, ch = max(2, int(cw) // 2 * 2), max(2, int(ch) // 2 * 2)
    cx, cy = int(round(cx)), int(round(cy))
    s = min(W / cw, H / ch)                       # fit inside the canvas, preserving aspect: letterbox, never distort
    tw, th = min(W, _even(cw * s)), min(H, _even(ch * s))
    px, py = (W - tw) // 2, (H - th) // 2

    t_pos = max(src_t, 0.0)
    # A request within half a frame of the start of the file is float dust (a real episode asked for -1e-9 s and got a black first
    # frame), not a camera that is not recording yet: treat it as t=0. Only a real shortfall pads with black.
    half_frame = 0.5 / float(fps)
    if -half_frame < src_t < 0:
        src_t = 0.0
    lead = max(0.0, -src_t)
    coarse = int(math.floor(t_pos))
    t_rel = t_pos - coarse
    # keep every source frame whose instant is within half a SOURCE frame of the start: the fps filter below then maps each
    # to its nearest output frame, i.e. the frame closest to the requested instant is shown (exact when the clocks align)
    eps = 0.5 / float(info.get("fps") or fps)
    if lead > 0:
        _warn(warnings, f"video {piece['ids'][0]}: source {piece['src']} is asked for time before the start of its file ({src_t:.3f} s); black is shown")
    if info.get("duration") and src_t + n / float(fps) > info["duration"] + 1.0 / float(fps):
        _warn(warnings, f"video {piece['ids'][0]}: runs past the end of source {piece['src']}; the last frame is held")

    # max(..., 0): the frame kept from up to half a source frame BEFORE the start would otherwise land at pts -1 when the start sits
    # exactly between two source frames (e.g. 24.25 s at 30 fps); an edit list hides that frame and the picture comes out a frame short
    setpts = f"setpts='max(PTS-{t_rel:.6f}/TB,0)'" if k == 1.0 else f"setpts='max(PTS-{t_rel:.6f}/TB,0)*{k:.9f}'"
    vf = [f"trim=start={max(t_rel - eps, 0.0):.6f}", setpts, f"fps={fps.numerator}/{fps.denominator}"]
    if (cx, cy, cw, ch) != (0, 0, sw, sh):
        vf.append(f"crop={cw}:{ch}:{cx}:{cy}")
    vf.append(f"scale={tw}:{th}:flags=lanczos+accurate_rnd+full_chroma_int:out_color_matrix=bt709:out_range=tv")
    if (tw, th) != (W, H):
        vf.append(f"pad={W}:{H}:{px}:{py}:color=black")
    vf += ["setsar=1", "format=yuv420p"]
    lead_frames = int(round(lead * float(fps)))
    if lead_frames:
        vf.append(f"tpad=start_mode=add:start_duration={lead_frames / float(fps):.6f}:color=black")
    vf.append(f"tpad=stop_mode=clone:stop_duration={n / float(fps) + 1:.3f}")
    ts = fps.numerator * max(1, math.ceil(10000 / fps.numerator))
    return ["-y", *(["-hwaccel", "videotoolbox"] if hw else []), "-ss", str(coarse), "-i", str(_path(ep, src["file"])), "-an", "-sn", "-vf", ",".join(vf),
            "-frames:v", str(n), "-fps_mode", "passthrough", "-c:v", "libx264", "-preset", x264, "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
            "-video_track_timescale", str(ts), str(out)]


_HW: list[bool] = []


def _hw_available() -> bool:
    if not _HW:
        from . import proxies
        _HW.append(proxies.hw_decode_available())
    return _HW[0]


def compile_video(ep: Episode, doc: dict, out_mp4: str | Path, *, preset: str | dict | None = None, burn_captions: bool = False,
                  warnings: list[str] | None = None, workdir: Path | None = None) -> Path:
    """Picture only (no audio): canvas-size H.264 with exactly round(duration*fps) frames, plus the doc's overlays and,
    if `burn_captions`, its ASS captions. `preset` controls quality and (proxy) size; default is master quality."""
    out_mp4 = Path(out_mp4)
    if not doc["tracks"]["video"]:
        raise ValueError("EditDoc has no video segments")
    pr = _preset(preset)
    warnings = warnings if warnings is not None else []
    fps = ffutil.fps_fraction(doc["fps"])
    canvas = _canvas_for(doc, pr)
    own = workdir is None
    wd = workdir or _workdir(ep, "video")
    wd.mkdir(parents=True, exist_ok=True)
    try:
        _check_files(ep, doc, video=True, audio=False, burn=burn_captions)
        total = ffutil.frame_of(doc["duration"], fps)
        pieces = _plan_video(doc, fps, warnings)
        if sum(p["f1"] - p["f0"] for p in pieces) != total:
            raise RenderError("internal: planned frames do not add up to the duration")
        has_post = bool(doc["tracks"].get("overlays")) or (burn_captions and (doc.get("captions") or {}).get("ass"))
        crf = max(10, pr.get("crf", 18) - (4 if has_post else 0))     # a second encode follows: keep the first near-lossless
        x264 = pr.get("x264_preset", "medium") if not has_post else "fast"

        infos: dict[str, tuple[dict, int]] = {}
        for k in {p["src"] for p in pieces}:
            f = _path(ep, doc["sources"][k]["file"])
            info = ffutil.probe(f)
            if not info["has_video"]:
                raise RenderError(f"source {k} ({f.name}) has no video stream but is used by the video track")
            infos[k] = (info, _rotation(f))

        seg_files = [wd / f"seg_{i:05d}.mp4" for i in range(len(pieces))]

        def encode(i: int) -> None:
            p = pieces[i]
            info, rot = infos[p["src"]]
            # preset `hwaccel`: decode big HEVC/H.264 pictures on VideoToolbox (scaling happens right after, in software); a failure retries in software
            hw = bool(pr.get("hwaccel")) and info.get("codec") in ("hevc", "h264") and (info.get("h") or 0) >= 1440 and _hw_available()
            for attempt in range(SEGMENT_ATTEMPTS):
                try:
                    ffutil.run(_segment_cmd(p, doc, fps, canvas, info, rot, ep, crf, x264, seg_files[i], warnings, hw=hw))
                    return
                except ffutil.FFmpegError:
                    # A real 22-minute render died on one of 255 segments with exit 255 and EMPTY stderr (the same command succeeded
                    # alone a minute later: transient decode/IO/memory pressure while other jobs ran). Segments are independent and
                    # idempotent, so retry (in software if hardware decode was in use) before giving up the whole render.
                    seg_files[i].unlink(missing_ok=True)
                    if attempt == SEGMENT_ATTEMPTS - 1:
                        raise
                    hw = False
                    time.sleep(2.0 * (attempt + 1))

        workers = max(1, min(6, (os.cpu_count() or 4) // 2))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(encode, i): i for i in range(len(pieces))}
            try:
                for fu in as_completed(futs):
                    try:
                        fu.result()
                    except ffutil.FFmpegError as e:
                        raise RenderError(f"encoding video segment(s) {pieces[futs[fu]]['ids']} failed:\n{e}") from e
            except BaseException:
                pool.shutdown(wait=True, cancel_futures=True)
                raise

        lst = wd / "concat.txt"
        lst.write_text("".join(f"file '{f.name}'\n" for f in seg_files))
        joined = wd / "joined.mp4" if has_post else out_mp4
        joined.parent.mkdir(parents=True, exist_ok=True)
        ffutil.run(["-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", "-movflags", "+faststart", str(joined)], cwd=wd)
        got = ffutil.count_video_packets(joined)
        if got != total:
            raise RenderError(f"concatenated picture has {got} frames, expected {total}")
        if has_post:
            _post_pass(ep, doc, joined, out_mp4, fps, canvas, pr, burn_captions, wd, warnings)
            got = ffutil.count_video_packets(out_mp4)
            if got != total:
                raise RenderError(f"overlay/caption pass produced {got} frames, expected {total}")
        return out_mp4
    finally:
        if own:
            _drop_workdir(wd)


def _post_pass(ep: Episode, doc: dict, video_in: Path, out: Path, fps: Fraction, canvas: tuple[int, int], pr: dict,
               burn: bool, wd: Path, warnings: list[str]) -> None:
    """Overlays (ascending layer, alpha-aware, visible only between their frame-snapped o0 and o1) and ASS caption burn."""
    W, H = canvas
    sx = W / doc["canvas"]["w"]
    ovs = sorted(enumerate(doc["tracks"].get("overlays") or []), key=lambda iv: (iv[1].get("layer", 0), iv[0]))
    args = ["-y", "-i", str(video_in)]
    chains: list[str] = []
    cur = "0:v"
    f = fps.numerator, fps.denominator
    for k, (_, o) in enumerate(ovs, start=1):
        path = _path(ep, o["file"])
        f0, f1 = ffutil.frame_of(o["o0"], fps), ffutil.frame_of(o["o1"], fps)
        dur = (f1 - f0) / float(fps)
        if f1 <= f0:
            _warn(warnings, f"overlay {o.get('id')} is shorter than one frame and was skipped")
            continue
        if path.suffix.lower() in IMAGE_EXTS:
            args += ["-loop", "1", "-framerate", f"{f[0]}/{f[1]}", "-t", f"{dur + 1:.3f}", "-i", str(path)]
        else:
            args += ["-t", f"{dur:.6f}", "-i", str(path)]
        # The RGB(A)->YUV step is explicit (bt709, limited range, yuva420p = the main picture's format): left to `overlay:format=auto`,
        # older FFmpeg (6.x, Ubuntu 24.04) converts with the BT.601 matrix while the output is tagged BT.709, shifting every colour.
        cs = ":out_color_matrix=bt709:out_range=tv"
        scale = (f"scale={max(1, int(round(o['w'] * sx)))}:{max(1, int(round(o['h'] * sx)))}{cs}" if o.get("w") and o.get("h")
                 else f"scale=trunc(iw*{sx:.6f}/2)*2:trunc(ih*{sx:.6f}/2)*2{cs}")
        shift = f"PTS-STARTPTS+{f0 * f[1]}/{f[0]}/TB"
        chains.append(f"[{k}:v]format=rgba,{scale},format=yuva420p,setpts={shift}[ov{k}]")
        x, y = int(round(o.get("x", 0) * sx)), int(round(o.get("y", 0) * sx))
        chains.append(f"[{cur}][ov{k}]overlay={x}:{y}:eof_action=pass:format=auto:enable='gte(n,{f0})*lt(n,{f1})'[c{k}]")
        cur = f"c{k}"
    ass = (doc.get("captions") or {}).get("ass")
    if burn and ass:
        ffutil.require_libass()
        shutil.copy(_path(ep, ass), wd / "captions.ass")
        fonts = config.REPO_ROOT / "motion-kit" / "brand" / "fonts"
        opt = "ass=captions.ass"
        if fonts.is_dir() and any(fonts.glob("*.[ot]tf")):
            link = wd / "fonts"
            if not link.exists():
                link.symlink_to(fonts)
            opt += ":fontsdir=fonts"
        else:
            _warn(warnings, "no brand fonts in motion-kit/brand/fonts; captions use system fonts")
        chains.append(f"[{cur}]{opt}[cc]")
        cur = "cc"
    chains.append(f"[{cur}]format=yuv420p[vout]")
    args += ["-filter_complex", ";".join(chains), "-map", "[vout]", "-an", "-frames:v", str(ffutil.frame_of(doc["duration"], fps)),
             "-c:v", "libx264", "-preset", pr.get("x264_preset", "medium"), "-crf", str(pr.get("crf", 18)), "-pix_fmt", "yuv420p",
             "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
             "-movflags", "+faststart", str(out)]
    ffutil.run(args, cwd=wd)


# ======================================================================================================================
# AUDIO
# ======================================================================================================================

class _Reader:
    """Sample-range reader for one audio file: float32 stereo at 48 kHz, zero outside the file.

    Positions are integer sample indices on the 48 kHz grid of the file's own clock. Seeking lands on a whole second
    (exact), and the precise start/end are cut by sample count (`atrim`), so there is no rounding of float seeks.
    """

    def __init__(self, path: Path, warnings: list[str] | None, label: str, channel_map: list[dict] | None = None):
        self.path, self.warnings, self.label = path, warnings, label
        # ranges (in this file's clock) where one channel is dead: that range plays the LIVE channel on both ears
        # `sum` ranges (speaker-per-channel masters) play L+R, soft-limited, on both ears: both mics, never gated
        self.fold = [(int(round(m["start"] * SR)), int(round(m["end"] * SR)), {"left": 0, "right": 1, "sum": None}[m["use"]])
                     for m in channel_map or [] if m["use"] in ("left", "right", "sum")]
        info = ffutil.probe(path)
        if not info["has_audio"]:
            raise RenderError(f"{label}: {path.name} has no audio stream")
        self.channels = 1 if info["channels"] == 1 else 2
        self.length = int((info["audio_duration"] or info["duration"] or 0.0) * SR)

    def read(self, start: int, count: int) -> np.ndarray:
        out = np.zeros((count, 2), np.float32)
        a, b = max(start, 0), start + count
        if b <= 0 or count <= 0:
            return out
        if start < 0:
            _warn(self.warnings, f"{self.label}: audio asked for before the start of {self.path.name}; silence is used")
        coarse = a // SR
        proc = ffutil.run(["-ss", str(coarse), "-i", str(self.path), "-vn", "-map", "0:a:0",
                           "-af", f"aresample={SR},atrim=start_sample={a - coarse * SR}:end_sample={b - coarse * SR}",
                           "-ac", str(self.channels), "-f", "f32le", "-"], text=False)
        raw = np.frombuffer(proc.stdout, np.float32)
        if self.channels == 1:
            raw = np.repeat(raw[:, None], 2, axis=1)
        else:
            raw = raw[: len(raw) // 2 * 2].reshape(-1, 2)
        n = min(len(raw), b - a)
        if self.fold and self.channels == 2:
            raw = raw[:n].copy()
            for f0, f1, live in self.fold:
                lo, hi = max(f0, a), min(f1, a + n)
                if hi > lo:
                    if live is None:
                        both = soft_limit(raw[lo - a:hi - a].sum(axis=1))
                        raw[lo - a:hi - a] = both[:, None]
                    else:
                        raw[lo - a:hi - a] = raw[lo - a:hi - a, [live]]   # dual mono from the live channel: never one ear
        out[a - start:a - start + n] = raw[:n]
        if (b - a) - n > 0.04 * SR:   # under ~1 video frame of shortfall is container-rounding dust, not a real overrun
            _warn(self.warnings, f"{self.label}: runs past the end of {self.path.name} by {((b - a) - n) / SR:.2f} s; silence is used for the rest")
        return out


MATCH_DEADBAND_DB = 1.5         # room tone within this of its surroundings is used as measured (the estimate itself has this much noise)
MATCH_MAX_DB = (-8.0, 8.0)      # bounded: a measurement error must not turn a fill into a feature (pause-less neighbours are ignored anyway)


def noise_floor_db(x: np.ndarray, frame_s: float = 0.02, min_range_db: float = 10.0) -> float | None:
    """Noise floor of a block in dBFS: the 10th percentile of its 20 ms frame RMS (speech pauses show through, speech does not).
    None when the block is too short or has no pause (the 90th percentile is under `min_range_db` above the 10th): then the
    "floor" would be speech or a steady tone, and matching to it would raise the fill instead of matching it."""
    m = x.mean(axis=1) if x.ndim > 1 else x
    f = int(frame_s * SR)
    n = len(m) // f
    if n < 5:
        return None
    r = np.sqrt((m[:n * f].astype(np.float64).reshape(n, f) ** 2).mean(axis=1))
    lo, hi = np.percentile(r, [10, 90])
    return float(20 * np.log10(lo + 1e-9)) if 20 * np.log10((hi + 1e-9) / (lo + 1e-9)) >= min_range_db else None


def match_gain_db(room: np.ndarray, around: list[np.ndarray]) -> float:
    """dB to apply to room tone so its floor equals the floor of the audio on either side of the gap (no level jump at the seams).
    0 when the two already agree within MATCH_DEADBAND_DB or when a neighbour is too short to measure."""
    want = [d for d in (noise_floor_db(a) for a in around if a is not None and len(a)) if d is not None]
    have = noise_floor_db(room, min_range_db=0.0)       # the room tone IS floor: no pause needed
    if not want or have is None:
        return 0.0
    diff = float(np.mean(want)) - have
    return 0.0 if abs(diff) <= MATCH_DEADBAND_DB else float(min(max(diff, MATCH_MAX_DB[0]), MATCH_MAX_DB[1]))


def _fill_buffer(room: np.ndarray, n: int, seed: str) -> np.ndarray:
    """Loop-free room tone of n samples: chunks of the room-tone range at seeded random offsets, joined with equal-power
    crossfades. Deterministic per segment id, so re-renders are identical. Reusing a random OFFSET each chunk (instead of
    repeating the same loop) means no periodicity a listener or a correlator can find."""
    R = len(room)
    if R < int(0.2 * SR):
        raise ValueError("room_tone range is shorter than 0.2 s; pick a longer silent stretch")
    rng = np.random.default_rng(int.from_bytes(hashlib.sha1(seed.encode()).digest()[:8], "little"))
    chunk = min(R, int(1.5 * SR))
    xf = max(2, min(int(0.08 * SR), chunk // 3)) // 2 * 2
    out = np.zeros((n + chunk + xf, 2), np.float32)
    pos, last = 0, -1
    th = (np.arange(xf) + 0.5) / xf * (np.pi / 2)
    fin, fout = np.sin(th)[:, None].astype(np.float32), np.cos(th)[:, None].astype(np.float32)
    while pos < n:
        off = int(rng.integers(0, R - chunk + 1)) if R > chunk else 0
        if R > chunk and last >= 0 and abs(off - last) < chunk // 2:      # force a genuinely different excerpt
            off = (off + chunk // 2 + int(rng.integers(0, max(1, R - chunk)))) % (R - chunk + 1)
        last = off
        piece = room[off:off + chunk]
        if pos == 0:
            out[:chunk] = piece
            pos = chunk - xf
        else:
            out[pos:pos + xf] = out[pos:pos + xf] * fout + piece[:xf] * fin
            out[pos + xf:pos + chunk] = piece[xf:]
            pos += chunk - xf
    return out[:n]


class _DialogueSeg:
    def __init__(self, ep: Episode, doc: dict, s: dict, readers: dict, warnings, room: np.ndarray | None):
        self.id = s["id"]
        self.gain = _db(s.get("gain_db", 0.0))
        self.o0 = _samples(s["o0"])
        self.fill = s.get("fill") == "room_tone"
        self.reader: _Reader | None = None
        self.room = room
        self.buf: tuple[int, np.ndarray] | None = None
        if not self.fill:
            src = doc["sources"][s["src"]]
            self.k = editdoc.clock_rate(src, s["e0"])
            self.src0f = editdoc.src_time(src, s["e0"]) * SR       # fractional file sample where this segment starts
            self.src0 = _samples(editdoc.src_time(src, s["e0"]))
            f = _path(ep, src["file"])
            if f not in readers:
                readers[f] = _Reader(f, warnings, f"source {s['src']}", src.get("channel_map"))
            self.reader = readers[f]

    def prepare_fill(self, lo: int, hi: int, around: list[np.ndarray] | None = None) -> None:
        """Build this fill's room tone, level-matched to the audio `around` the gap when it is given."""
        if self.fill:
            if self.room is None:
                raise ValueError(f"dialogue segment {self.id} uses fill 'room_tone' but the doc has no tracks.room_tone")
            buf = _fill_buffer(self.room, hi - lo, self.id)
            g = match_gain_db(self.room, around or [])
            self.buf = (lo, buf * np.float32(_db(g)) if g else buf)
            self.match_db = g

    def read(self, n0: int, n1: int) -> np.ndarray:
        """Output samples [n0, n1) of this segment (it may extend past its own bounds: those are the crossfade handles)."""
        if self.fill:
            lo, buf = self.buf
            seg = np.zeros((n1 - n0, 2), np.float32)
            a, b = max(n0, lo), min(n1, lo + len(buf))
            seg[a - n0:b - n0] = buf[a - lo:b - lo]
        else:
            seg = self._read_source(n0, n1)
        return seg * np.float32(self.gain)

    def _read_source(self, n0: int, n1: int) -> np.ndarray:
        if self.k == 1.0:
            return self.reader.read(self.src0 + (n0 - self.o0), n1 - n0)
        # slow source clock: output sample n is file sample src0 + (n - o0) / k. Read the covering file samples and interpolate
        # at those exact (fractional, absolute) positions, so consecutive blocks join seamlessly.
        pos = self.src0f + (np.arange(n0, n1) - self.o0) / self.k
        a = int(math.floor(pos[0])) - 1
        blk = self.reader.read(a, int(math.ceil(pos[-1])) + 2 - a)
        x = pos - a
        return np.stack([np.interp(x, np.arange(len(blk)), blk[:, c]) for c in range(2)], axis=1).astype(np.float32)


def _room_tone(ep: Episode, doc: dict, warnings) -> np.ndarray | None:
    rt = doc["tracks"].get("room_tone")
    if not rt:
        return None
    src = doc["sources"][rt["src"]]
    r = _Reader(_path(ep, src["file"]), warnings, "room_tone", src.get("channel_map"))
    a = _samples(editdoc.src_time(src, rt["e0"]))
    b = _samples(editdoc.src_time(src, rt["e1"]))
    return r.read(a, b - a)


def _dialogue_master(ep: Episode, doc: dict, out: Path, warnings: list[str]) -> int:
    segs = sorted(doc["tracks"]["dialogue"], key=lambda s: s["o0"])
    if not segs:
        raise ValueError("EditDoc has no dialogue segments")
    N = _samples(doc["duration"])
    S = [0] + [_samples((a["o1"] + b["o0"]) / 2.0) for a, b in zip(segs, segs[1:])] + [N]     # seam positions
    for i in range(1, len(S)):
        S[i] = max(S[i], S[i - 1])
    # crossfade half-widths at interior seams, never wider than half of either neighbour
    H = [0] * len(S)
    for i in range(1, len(S) - 1):
        x = int(round(segs[i].get("xfade_ms", 0) / 1000.0 * SR)) // 2
        H[i] = max(0, min(x, (S[i] - S[i - 1]) // 2, (S[i + 1] - S[i]) // 2))
    room = _room_tone(ep, doc, warnings) if any(s.get("fill") == "room_tone" for s in segs) else None
    readers: dict = {}
    dsegs = [_DialogueSeg(ep, doc, s, readers, warnings, room) for s in segs]
    NEAR = 3 * SR                                   # how much of each neighbour tells the fill what the room sounds like here
    for j, d in enumerate(dsegs):
        around: list[np.ndarray] = []
        if d.fill:
            if j > 0 and not dsegs[j - 1].fill and S[j] - S[j - 1] > 0:
                around.append(dsegs[j - 1].read(max(S[j] - NEAR, S[j - 1]), S[j]))
            if j + 1 < len(dsegs) and not dsegs[j + 1].fill and S[j + 2] - S[j + 1] > 0:
                around.append(dsegs[j + 1].read(S[j + 1], min(S[j + 1] + NEAR, S[j + 2])))
        d.prepare_fill(S[j] - H[j], S[j + 1] + H[j + 1], around)
    out.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with sf.SoundFile(str(out), "w", samplerate=SR, channels=2, subtype="FLOAT", format="WAV") as w:
        for j, d in enumerate(dsegs):
            core0, core1 = S[j] + H[j], S[j + 1] - H[j + 1]
            for a in range(core0, core1, BLOCK):
                blk = d.read(a, min(a + BLOCK, core1))
                w.write(blk)
                written += len(blk)
            if j + 1 < len(dsegs) and H[j + 1] > 0:
                h, c = H[j + 1], S[j + 1]
                th = (np.arange(2 * h) + 0.5) / (2 * h) * (np.pi / 2)         # equal power: cos^2 + sin^2 = 1
                a_part = d.read(c - h, c + h) * np.cos(th)[:, None].astype(np.float32)
                b_part = dsegs[j + 1].read(c - h, c + h) * np.sin(th)[:, None].astype(np.float32)
                w.write(a_part + b_part)
                written += 2 * h
    if written != N:
        raise RenderError(f"internal: dialogue master has {written} samples, expected {N}")
    return N


def _duck_activity(dialogue_wav: Path) -> np.ndarray:
    """0..1 'dialogue is speaking' signal per 10 ms hop, smoothed with attack/release so music ducks and recovers smoothly."""
    from scipy.ndimage import maximum_filter1d
    levels = []
    with sf.SoundFile(str(dialogue_wav)) as f:
        while True:
            blk = f.read(DUCK_HOP * 1000, dtype="float32", always_2d=True)
            if not len(blk):
                break
            m = blk.mean(axis=1)
            n = len(m) // DUCK_HOP
            if len(m) % DUCK_HOP:
                m = np.pad(m, (0, DUCK_HOP - len(m) % DUCK_HOP))
                n += 1
            levels.append(np.sqrt((m.reshape(n, DUCK_HOP) ** 2).mean(axis=1)))
    rms = np.concatenate(levels) if levels else np.zeros(1)
    active = (20 * np.log10(rms + 1e-9) > DUCK_THRESHOLD_DB).astype(np.float64)
    ext = maximum_filter1d(active, size=DUCK_HOLD_HOPS * 2 + 1, mode="constant")
    hop_s = DUCK_HOP / SR
    ka, kr = 1 - math.exp(-hop_s / DUCK_ATTACK_S), 1 - math.exp(-hop_s / DUCK_RELEASE_S)
    env = np.zeros_like(ext)
    g = 0.0
    for i, v in enumerate(ext):
        g += (v - g) * (ka if v > g else kr)
        env[i] = g
    return env


def _mix_music(ep: Episode, doc: dict, dialogue_wav: Path, out: Path, warnings: list[str]) -> None:
    items = doc["tracks"].get("music") or []
    env = _duck_activity(dialogue_wav) if any(m.get("duck_db", 0) for m in items) else None
    hop_t = (np.arange(len(env)) + 0.5) * DUCK_HOP if env is not None else None
    readers = {}
    for m in items:
        f = _path(ep, m["file"])
        readers.setdefault(f, _Reader(f, warnings, f"music {m.get('id')}"))
    with sf.SoundFile(str(dialogue_wav)) as src, sf.SoundFile(str(out), "w", samplerate=SR, channels=2, subtype="FLOAT", format="WAV") as w:
        pos = 0
        while True:
            blk = src.read(BLOCK, dtype="float32", always_2d=True)
            if not len(blk):
                break
            n0, n1 = pos, pos + len(blk)
            for m in items:
                a, b = max(n0, _samples(m["o0"])), min(n1, _samples(m["o1"]))
                if b <= a:
                    continue
                O0, O1 = _samples(m["o0"]), _samples(m["o1"])
                data = readers[_path(ep, m["file"])].read(_samples(m.get("in", 0.0)) + (a - O0), b - a)
                if data.shape[0] and not np.any(data):
                    _warn(warnings, f"music {m.get('id')}: file is shorter than the placement; the rest is silent")
                idx = np.arange(a, b)
                g = np.full(len(idx), _db(m.get("gain_db", -20.0)), np.float64)
                fi, fo = _samples(m.get("fade_in_s", 0.0)), _samples(m.get("fade_out_s", 0.0))
                if fi:
                    g *= np.sin(np.clip((idx - O0) / fi, 0, 1) * (np.pi / 2))
                if fo:
                    g *= np.sin(np.clip((O1 - idx) / fo, 0, 1) * (np.pi / 2))
                if env is not None and m.get("duck_db", 0):
                    g *= 10.0 ** (m["duck_db"] * np.interp(idx, hop_t, env) / 20.0)
                blk[a - n0:b - n0] += data * g[:, None].astype(np.float32)
            w.write(blk)
            pos = n1


def compile_audio(ep: Episode, doc: dict, out_wav: str | Path, *, warnings: list[str] | None = None) -> Path:
    """The dialogue master (+ music, ducked by the dialogue) as a 48 kHz stereo float WAV with exactly
    round(duration * 48000) samples. Dialogue is placed by sample index; contiguous segments with `xfade_ms` get an
    equal-power crossfade centred on the seam (it borrows handle audio from both sides, so duration never changes);
    `fill: room_tone` segments get loop-free room tone."""
    out_wav = Path(out_wav)
    warnings = warnings if warnings is not None else []
    _check_files(ep, doc, video=False, audio=True, burn=False)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_wav.with_name(out_wav.stem + ".partial.wav")
    try:
        if doc["tracks"].get("music"):
            dlg = out_wav.with_name(out_wav.stem + ".dialogue.wav")
            try:
                _dialogue_master(ep, doc, dlg, warnings)
                _mix_music(ep, doc, dlg, tmp, warnings)
            finally:
                dlg.unlink(missing_ok=True)
        else:
            _dialogue_master(ep, doc, tmp, warnings)
        tmp.replace(out_wav)
    finally:
        tmp.unlink(missing_ok=True)
    return out_wav


SUM_BLOCK = 60 * SR              # streaming block for compile_source_sum, samples (fewer ffmpeg spawns on multi-hour recorders)


def compile_source_sum(ep: Episode, doc: dict, src_key: str, out_wav: str | Path, *, warnings: list[str] | None = None) -> Path:
    """One whole source as a DUAL-MONO WAV (48 kHz, 24-bit PCM stereo, L == R) on the source's OWN clock (sample n = file sample n).

    WHY: a speaker-per-channel recorder is hard-panned (host left, guest right), so raw clips cut from it play in one ear and
    miss the other speaker. The layered Resolve handoff cuts dialogue into editable clips; they must come from this sum, with the
    SAME channel_map rules the compiled master uses: `sum` ranges are soft-limited L+R, `left`/`right` ranges play the live channel
    on both ears, `mix` / unmapped stereo is the average of L and R. Handles stay available because the file is one continuous
    source, so an editor can trim any clip out. Written atomically; the caller reuses it per source."""
    out_wav = Path(out_wav)
    warnings = warnings if warnings is not None else []
    src = doc["sources"][src_key]
    f = _path(ep, src["file"])
    if not f.exists():
        raise FileNotFoundError(f"source {src_key}: {f} is missing")
    reader = _Reader(f, warnings, f"source {src_key}", src.get("channel_map"))
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_wav.with_name(out_wav.stem + ".partial.wav")
    try:
        with sf.SoundFile(str(tmp), "w", samplerate=SR, channels=2, subtype="PCM_24", format="WAV") as w:
            for a in range(0, reader.length, SUM_BLOCK):
                blk = reader.read(a, min(SUM_BLOCK, reader.length - a))
                mono = np.clip(blk.mean(axis=1), -1.0, 1.0)         # fold ranges are already L == R, so the mean is exact there
                w.write(np.stack([mono, mono], axis=1))
        tmp.replace(out_wav)
    finally:
        tmp.unlink(missing_ok=True)
    return out_wav


# ======================================================================================================================
# RENDER
# ======================================================================================================================

def _normalize(master: Path, out: Path, pr: dict, n_samples: int, warnings: list[str], limit_db: float = -2.0) -> Path:
    """Two-pass loudnorm to the preset target (linear gain), THEN a lookahead limiter, exact sample count kept.

    WHY the limiter: gain-only (linear) loudnorm reaches the target by raising everything, so material with loud transients
    (claps, laughs, a recording that already clips at full scale: crest factor of 25 dB or more) ends far above full scale and clips
    when decoded. Real episode: a proxy came out at +1.5 dBTP. The limiter holds sample peaks at `tp + limit_db` (default 2 dB under
    the preset ceiling) so the codec's inter-sample overshoot still lands under the ceiling; `render` measures the encoded file and
    lowers the limit and re-encodes if the codec overshot anyway."""
    tgt, tp = pr["lufs"], pr["tp"]
    tail = f"aresample={SR},apad=whole_len={n_samples},atrim=end_sample={n_samples}"
    if pr.get("fixed_gain_db") is not None:     # opening/closing sequences (ave.sequence): the BODY's gain, so cards on room tone are not boosted to the target
        ceiling = min(max(10 ** ((tp + limit_db) / 20), 0.0625), 1.0)
        ffutil.run(["-y", "-i", str(master), "-af", f"volume={float(pr['fixed_gain_db']):.4f}dB,alimiter=limit={ceiling:.4f}:attack=5:release=60:level=disabled,{tail}",
                    "-ar", str(SR), "-c:a", "pcm_f32le", str(out)])
        return out
    m = ffutil.loudnorm_measure(master, tgt, tp)
    if m is None:
        _warn(warnings, "audio is silent: loudness normalization skipped")
        ffutil.run(["-y", "-i", str(master), "-af", tail, "-c:a", "pcm_f32le", str(out)])
        return out
    ceiling = 10 ** ((tp + limit_db) / 20)           # linear sample-peak limit
    ceiling = min(max(ceiling, 0.0625), 1.0)          # alimiter's legal range
    ln = (f"loudnorm=I={tgt}:TP={tp}:LRA=11:measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}"
          f":measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")
    lim = f"alimiter=limit={ceiling:.4f}:attack=5:release=60:level=disabled"
    ffutil.run(["-y", "-i", str(master), "-af", f"{ln},{lim},{tail}", "-ar", str(SR), "-c:a", "pcm_f32le", str(out)])
    return out


def _encode_audio_args(pr: dict) -> list[str]:
    if pr["audio"] == "mp3":
        return ["-c:a", "libmp3lame", "-b:a", f"{pr['audio_kbps']}k"]
    return ["-c:a", "aac", "-b:a", f"{pr['audio_kbps']}k"]


def _verify(out: Path, doc: dict, pr: dict, fps: Fraction, canvas: tuple[int, int]) -> dict:
    """Measure the delivered file. Structural problems raise; loudness shortfalls are reported as warnings by the caller."""
    info = ffutil.probe(out)
    frame = 1.0 / float(fps)
    problems = []
    if not info["has_audio"]:
        problems.append("no audio stream")
    if pr["kind"] == "video":
        if not info["has_video"]:
            problems.append("no video stream")
        else:
            if (info["w"], info["h"]) != tuple(canvas):
                problems.append(f"picture is {info['w']}x{info['h']}, expected {canvas[0]}x{canvas[1]}")
            # The NOMINAL rate (r_frame_rate) is the delivered spec. avg_frame_rate is measured from timestamps and read 29.95-29.97 on
            # short, audio-led preview files that were frame-exact (real preview run, 2026-10-14); the frame-count check below is the strict test.
            got_fps = info.get("r_fps") or info["fps"]
            if abs(got_fps - float(fps)) > 0.01:
                problems.append(f"fps is {got_fps:.4f}, expected {float(fps):.4f}")
            want = ffutil.frame_of(doc["duration"], fps)
            got = ffutil.count_video_packets(out)
            if got != want:
                problems.append(f"{got} frames, expected {want}")
            if info["video_duration"] is not None and abs(info["video_duration"] - doc["duration"]) > frame:
                problems.append(f"video duration {info['video_duration']:.3f}s differs from {doc['duration']:.3f}s by more than one frame")
    elif info["has_video"]:
        problems.append("audio-only preset produced a video stream")
    # Lossy audio-only containers carry encoder delay/padding (LAME: 1105-sample delay + frame padding, up to ~2 MP3 frames = 48 ms at 48 kHz);
    # which part of it a demuxer reports in the duration differs between FFmpeg versions (Ubuntu 24.04's 6.1 reported 38.040 s for 38.000 s).
    pad = 2 * 1152 / SR if (pr["kind"] == "audio" and pr.get("audio") == "mp3") else 0.0
    if abs(info["duration"] - doc["duration"]) > max(frame, 0.001) + pad:
        problems.append(f"duration {info['duration']:.3f}s differs from the doc's {doc['duration']:.3f}s by more than one frame ({frame:.3f}s)")
    if problems:
        raise RenderError("output failed verification:\n  - " + "\n  - ".join(problems))
    return info


def render(ep: Episode, doc: dict, out: str | Path, preset: str = "master", burn_captions: bool = True,
           overwrite: bool = False, keep_intermediates: bool = False, start_limit_db: float = -2.0) -> dict:
    """Compile `doc` to `out` and VERIFY the file. Returns the report; raises (and leaves no partial file) on failure.

    Report: {ok, out, duration, expected_duration, fps, lufs, true_peak_db, lra, preset, segments, warnings[]}.
    """
    out = Path(out)
    pr = _preset(preset)
    problems = editdoc.validate(doc)
    if pr["kind"] == "audio":      # an audio-only render does not need (or look at) the picture track
        problems = [p for p in problems if not p.startswith("video")]
    if problems:
        raise ValueError("invalid EditDoc:\n  - " + "\n  - ".join(problems))
    if out.exists() and not overwrite:
        raise FileExistsError(f"{out} already exists; pass overwrite=True (CLI: --overwrite) or choose another --out")
    warnings: list[str] = []
    fps = ffutil.fps_fraction(doc["fps"])
    video = pr["kind"] == "video"
    ass = (doc.get("captions") or {}).get("ass")
    burn = bool(burn_captions and ass and video)
    if burn_captions and not ass and video and (doc.get("captions") or {}).get("burn"):
        warnings.append("doc.captions.burn is set but no captions.ass is recorded; rendered without captions")
    _check_files(ep, doc, video=video, audio=True, burn=burn)

    out.parent.mkdir(parents=True, exist_ok=True)
    partial = out.with_name(out.stem + ".partial" + out.suffix)
    wd = _workdir(ep, out.stem)
    n_samples = _samples(doc["duration"])
    canvas = _canvas_for(doc, pr) if video else (doc["canvas"]["w"], doc["canvas"]["h"])
    try:
        master = compile_audio(ep, doc, wd / "master.wav", warnings=warnings)
        pic = compile_video(ep, doc, wd / "picture.mp4", preset=pr, burn_captions=burn, warnings=warnings, workdir=wd / "v") if video else None
        limit_db = start_limit_db     # sample-peak limiter ceiling = tp + limit_db; the loop below lowers it if the ENCODED file overshoots
        for attempt in range(3):                      # measure the ENCODED file; if the codec overshot, limit harder and re-encode
            norm = _normalize(master, wd / "norm.wav", pr, n_samples, warnings, limit_db=limit_db)
            if video:
                ffutil.run(["-y", "-i", str(pic), "-i", str(norm), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
                            *_encode_audio_args(pr), "-ar", str(SR), "-movflags", "+faststart", str(partial)])
            else:
                ffutil.run(["-y", "-i", str(norm), *_encode_audio_args(pr), "-ar", str(SR), str(partial)])
            info = _verify(partial, doc, pr, fps, canvas)
            loud = ffutil.measure_loudness(partial)
            if loud["true_peak_db"] <= TRUE_PEAK_LIMIT_DB:
                break
            limit_db -= (loud["true_peak_db"] - TRUE_PEAK_LIMIT_DB) + 0.5
        if loud["lufs"] > -70 and abs(loud["lufs"] - pr["lufs"]) > LOUDNESS_TOLERANCE_LU and pr.get("fixed_gain_db") is None:
            warnings.append(f"loudness {loud['lufs']:.1f} LUFS is outside {pr['lufs']} +/- {LOUDNESS_TOLERANCE_LU} "
                            f"(material too peaky or dynamic to reach the target within the true-peak ceiling)")
        if loud["true_peak_db"] > TRUE_PEAK_LIMIT_DB:
            warnings.append(f"true peak {loud['true_peak_db']:.2f} dBTP exceeds {TRUE_PEAK_LIMIT_DB} dBTP")
        os.replace(partial, out)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    finally:
        if keep_intermediates:
            pass
        else:
            _drop_workdir(wd)
    return {
        "ok": True, "out": str(out), "duration": round(info["duration"], 4), "expected_duration": round(doc["duration"], 4),
        "fps": float(fps) if video else None, "lufs": round(loud["lufs"], 2), "true_peak_db": round(loud["true_peak_db"], 2),
        "lra": round(loud["lra"], 2), "preset": preset if isinstance(preset, str) else "custom",
        "segments": {"video": len(doc["tracks"]["video"]), "dialogue": len(doc["tracks"]["dialogue"]),
                     "music": len(doc["tracks"].get("music") or []), "overlays": len(doc["tracks"].get("overlays") or [])},
        "captions_burned": burn, "warnings": warnings,
    }
