"""Vertical reframing: find the face on each camera with Apple Vision and compute a stable 9:16 crop in SOURCE pixels.

WHY these choices:
  * One STATIC crop per camera (a per-segment crop only when the person clearly moved). A human editor frames the shot once and
    cuts between framings; a crop that drifts or re-centres every cut reads as jitter. Detections are medians over several frames.
  * The crop is sized from the face (`face_frac`), so a 4K wide shot becomes a head-and-shoulders portrait, not a sliver of torso.
  * The eye line goes near the upper third, which leaves the lower half free for the karaoke captions (65% height, safe zone).
  * No face found -> centred crop, `needs_review: true`, and the reason says so. Never guess silently.
Apple Vision (pyobjc) is imported lazily; without it every camera falls back and is flagged.
"""

from __future__ import annotations

import statistics
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import editdoc, ffutil

ASPECT = 9 / 16


def even(x: float) -> int:
    return int(x) // 2 * 2


# ---- geometry (pure) ------------------------------------------------------------------------------------------------

def crop_for_face(face: tuple[float, float, float, float] | None, sw: int, sh: int, *, face_frac: float = 0.25,
                  eye_line: float = 0.35, eye_in_box: float = 0.38, min_h: int = 0, aspect: tuple[int, int] = (9, 16)) -> list[int]:
    """9:16 crop [x, y, w, h] in source pixels. face = (x, y, w, h) top-left origin, or None for a centred full-height crop.

    `min_h` stops a small face on a low-res camera from being cropped so tight that it is magnified into mush (the crop widens instead).

    Invariants (tested): inside the frame, ratio exactly 9:16, w and h even (h is a multiple of 32 so w = h*9/16 is an exact even integer).
    """
    aw, ah = aspect                       # (9, 16) portrait; (9, 8) for one half of a two-up stack
    max_h = min(sh, int(sw * ah / aw))
    max_h = max_h // 32 * 32
    if face is None:
        h = max_h
        w = h * aw // ah
        return [even((sw - w) / 2), even((sh - h) / 2), w, h]
    fx, fy, fw, fh = face
    h = int(round(fh / face_frac / 32)) * 32
    h = max(32 * 8, min(max_h, max(h, min_h // 32 * 32 if min_h else 0)))
    w = h * aw // ah
    cx = fx + fw / 2
    eye_y = fy + eye_in_box * fh
    x = even(min(max(cx - w / 2, 0), sw - w))
    y = even(min(max(eye_y - eye_line * h, 0), sh - h))
    return [x, y, w, h]


def preview_zoom_note(crop: list[int], canvas_h: int) -> float:
    """How much the crop is magnified to fill the canvas (1.0 = native pixels; >2 looks soft)."""
    return canvas_h / crop[3]


# ---- Apple Vision ---------------------------------------------------------------------------------------------------

def vision_available() -> bool:
    try:
        import Vision  # noqa: F401
        return True
    except Exception:
        return False


def detect_faces(image: Path | str) -> list[dict]:
    """Faces in an image: [{x, y, w, h, conf}] in PIXELS, top-left origin. Raises RuntimeError if Vision is unavailable."""
    try:
        import Vision
        from Foundation import NSURL
    except Exception as e:  # pragma: no cover - non-macOS
        raise RuntimeError(f"Apple Vision (pyobjc-framework-Vision) is not available: {e}") from e
    from PIL import Image
    with Image.open(image) as im:
        W, H = im.size
    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(NSURL.fileURLWithPath_(str(image)), None)
    req = Vision.VNDetectFaceRectanglesRequest.alloc().init()
    ok, err = handler.performRequests_error_([req], None)
    if not ok:
        raise RuntimeError(f"Vision face detection failed: {err}")
    out = []
    for o in req.results() or []:
        b = o.boundingBox()    # normalized, origin bottom-left
        out.append({"x": b.origin.x * W, "y": (1 - b.origin.y - b.size.height) * H, "w": b.size.width * W,
                    "h": b.size.height * H, "conf": float(o.confidence())})
    return out


def extract_frame(path: Path | str, t: float, out: Path, width: int | None = None) -> Path:
    """One still at file time t (seconds), optionally downscaled to `width`. Orientation metadata is applied by ffmpeg."""
    vf = ["-vf", f"scale={width}:-2"] if width else []
    ffutil.run(["-y", "-ss", f"{max(t, 0):.3f}", "-i", str(path), "-frames:v", "1", *vf, str(out)])
    if not out.exists():
        raise RuntimeError(f"could not extract a frame at {t:.2f}s from {path}")
    return out


def main_face(faces: list[dict], min_conf: float) -> dict | None:
    """The camera's subject = the biggest confident face (a guest in the background is smaller)."""
    ok = [f for f in faces if f["conf"] >= min_conf]
    return max(ok, key=lambda f: f["w"] * f["h"]) if ok else None


def median_face(faces: list[dict]) -> dict | None:
    if not faces:
        return None
    return {k: statistics.median(f[k] for f in faces) for k in ("x", "y", "w", "h", "conf")}


# ---- planning -------------------------------------------------------------------------------------------------------

def _src_time(doc: dict, seg: dict, frac: float) -> float:
    e = seg["e0"] + frac * (seg["o1"] - seg["o0"])
    return editdoc.source_time(doc, seg["src"], e)


def plan_crops(ep, doc: dict, cfg: dict, workdir: Path | None = None, detector=detect_faces,
               aspect: tuple[int, int] = (9, 16)) -> dict[str, dict]:
    """Crop decision for every video segment id: {id: {crop, zoom, reason, confidence, needs_review, face, camera_crop, upscale}}.

    `detector(image) -> faces` is injectable for tests. Frames are extracted at `samples_per_segment` points inside each segment
    (capped per camera), downscaled for speed, detected, mapped back to source pixels, and reduced with medians.
    """
    rc = cfg["reframe"]
    segs = sorted(doc["tracks"]["video"], key=lambda s: s["o0"])
    by_cam: dict[str, list[dict]] = {}
    for s in segs:
        by_cam.setdefault(s["src"], []).append(s)
    own = workdir is None
    wd = Path(tempfile.mkdtemp(prefix="reframe_", dir=str(ep.work) if ep.work.is_dir() else None)) if own else workdir
    wd.mkdir(parents=True, exist_ok=True)
    out: dict[str, dict] = {}
    try:
        infos = {}
        for cam in by_cam:
            f = ep.root / doc["sources"][cam]["file"]
            info = ffutil.probe(f)
            infos[cam] = (f, info["w"], info["h"])
        tasks = []     # (cam, seg_id, src_time)
        for cam, cs in by_cam.items():
            n = rc["samples_per_segment"]
            if n * len(cs) > rc["max_samples_per_camera"]:
                n = max(1, rc["max_samples_per_camera"] // len(cs))
            for s in cs:
                fr = [0.5] if n == 1 else [0.15 + 0.7 * i / (n - 1) for i in range(n)]
                tasks += [(cam, s["id"], _src_time(doc, s, f)) for f in fr]

        def work(i_task):
            i, (cam, sid, t) = i_task
            path, sw, sh = infos[cam]
            img = wd / f"f{i:04d}.png"
            extract_frame(path, t, img, rc["detect_width"])
            from PIL import Image
            with Image.open(img) as im:
                scale = sw / im.size[0]
            try:
                faces = detector(img)
            except RuntimeError as e:      # Vision missing (non-macOS): every camera falls back and is flagged
                unavailable.append(str(e))
                faces = []
            face = main_face(faces, rc["min_confidence"])
            img.unlink(missing_ok=True)
            if face:
                face = {k: v * scale if k != "conf" else v for k, v in face.items()}
            return cam, sid, face

        found: dict[str, list[dict]] = {}
        unavailable: list[str] = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            for cam, sid, face in pool.map(work, list(enumerate(tasks))):
                if face:
                    found.setdefault(sid, []).append(face)
    finally:
        if own:
            import shutil
            shutil.rmtree(wd, ignore_errors=True)

    geo = dict(face_frac=rc["face_frac"], eye_line=rc["eye_line"], eye_in_box=rc["eye_in_box"],
               min_h=int(cfg["canvas"]["h"] / rc["max_upscale"]), aspect=aspect)
    for cam, cs in by_cam.items():
        _, sw, sh = infos[cam]
        pooled = [f for s in cs for f in found.get(s["id"], [])]
        cam_face = median_face(pooled)
        cam_crop = crop_for_face(_box(cam_face), sw, sh, **geo) if cam_face else crop_for_face(None, sw, sh, **geo)
        for s in cs:
            mine = found.get(s["id"], [])
            seg_face = median_face(mine)
            crop, face, reason, conf, review = cam_crop, cam_face, "", 0.0, False
            if not cam_face:
                reason = (f"face detection unavailable ({unavailable[0]}): centred 9:16 crop" if unavailable
                          else f"no face detected on camera {cam}: centred 9:16 crop")
                review = True
            else:
                conf = round(cam_face["conf"], 2)
                reason = f"face-centred 9:16 crop from {len(pooled)} detections on camera {cam}"
                if seg_face and len(mine) >= 2:
                    dx = abs((seg_face["x"] + seg_face["w"] / 2) - (cam_face["x"] + cam_face["w"] / 2))
                    dy = abs((seg_face["y"] + seg_face["h"] / 2) - (cam_face["y"] + cam_face["h"] / 2))
                    if dx > rc["deviate_frac"] * cam_crop[2] or dy > rc["deviate_frac"] * cam_crop[3]:
                        crop, face, conf = crop_for_face(_box(seg_face), sw, sh, **geo), seg_face, round(seg_face["conf"], 2)
                        reason = f"speaker moved: own crop from {len(mine)} detections in this shot"
                elif not mine:
                    reason += " (no detection in this shot: shares the camera crop)"
            up = round(preview_zoom_note(crop, cfg["canvas"]["h"]), 2)
            if up > rc["upscale_warn"]:
                reason += f"; upscaled {up}x (soft picture)"
                review = True
            out[s["id"]] = {"crop": crop, "zoom": 1.0, "reason": reason, "confidence": conf, "needs_review": review,
                            "face": face, "upscale": up, "camera": cam}
    return out


def crop_motion_stats(doc: dict) -> dict:
    """Jitter report for a short's crops: per camera the number of distinct crops and the largest crop-centre move between two
    shots of that SAME camera (in fractions of the crop width; 0 = perfectly static), plus the largest size change. A move
    between different cameras is a cut, not motion, and is not counted."""
    segs = sorted(doc["tracks"]["video"], key=lambda s: s["o0"])
    cams: dict[str, dict] = {}
    last: dict[str, list] = {}
    for s in segs:
        c = s.get("crop")
        if not c:
            continue
        st = cams.setdefault(s["src"], {"crops": set(), "max_centre_move": 0.0, "max_size_change": 0.0, "shots": 0})
        st["crops"].add(tuple(c))
        st["shots"] += 1
        p = last.get(s["src"])
        if p:
            move = max(abs((c[0] + c[2] / 2) - (p[0] + p[2] / 2)), abs((c[1] + c[3] / 2) - (p[1] + p[3] / 2))) / c[2]
            st["max_centre_move"] = max(st["max_centre_move"], move)
            st["max_size_change"] = max(st["max_size_change"], abs(c[3] - p[3]) / p[3])
        last[s["src"]] = c
    return {k: {"distinct_crops": len(v["crops"]), "shots": v["shots"], "max_centre_move": round(v["max_centre_move"], 4),
                "max_size_change": round(v["max_size_change"], 4)} for k, v in cams.items()}


def _box(f: dict | None):
    return (f["x"], f["y"], f["w"], f["h"]) if f else None
