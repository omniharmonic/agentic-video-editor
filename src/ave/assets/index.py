"""Local footage index (SQLite): shots, thumbnails, quality metrics, agent-written tags, search, contact sheets.

The index never modifies footage. It finds shots (ffmpeg scene detection), keeps one thumbnail per shot, and measures
cheap quality signals (sharpness, exposure, faces) so the agent can filter. What a shot IS (subject, mood, texture) is
written by the agent after it LOOKS at a contact sheet: tags are first-class and survive re-indexing.
Embeddings are optional (open_clip, imported lazily); without them search is tag-based and says so.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path

import numpy as np

from ..episode import MEDIA_EXTS

VIDEO_EXTS = MEDIA_EXTS - {".wav", ".aif", ".aiff", ".mp3", ".m4a", ".flac", ".aac"}
SCENE_THRESHOLD = 0.3
MIN_SHOT_S = 0.5
MAX_SHOT_S = 30.0
THUMB_W = 480
BLUR_REF = 120.0       # Laplacian variance (at THUMB_W) at or above which a frame counts as fully sharp
BLUR_FLAG = 40.0       # below this we flag "blurry"
UNDER, OVER = 0.12, 0.88   # mean luma bounds for exposure flags

SCHEMA = """
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, size INTEGER, mtime REAL, duration REAL, indexed_at TEXT);
CREATE TABLE IF NOT EXISTS shots(
  id INTEGER PRIMARY KEY AUTOINCREMENT, file TEXT NOT NULL, t0 REAL, t1 REAL, duration REAL, w INTEGER, h INTEGER, fps REAL,
  aspect TEXT, thumb TEXT, blur REAL, brightness REAL, contrast REAL, face INTEGER, quality REAL, flags TEXT,
  tags TEXT DEFAULT '[]', embedding BLOB);
CREATE INDEX IF NOT EXISTS shots_file ON shots(file);
"""


class EmbeddingsUnavailable(RuntimeError):
    pass


def default_db() -> Path:
    return Path(os.environ.get("AVE_ASSETS_DB", Path.home() / ".local" / "share" / "ave" / "assets.db"))


def _connect(db_path) -> sqlite3.Connection:
    p = Path(db_path) if db_path else default_db()
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def _row(r: sqlite3.Row) -> dict:
    d = {k: r[k] for k in r.keys() if k != "embedding"}
    d["tags"] = json.loads(d["tags"] or "[]")
    d["flags"] = (d["flags"] or "").split(",") if d["flags"] else []
    d["has_embedding"] = r["embedding"] is not None
    return d


# ---- probing and scene detection -------------------------------------------------------------------------------

def probe(path: Path) -> dict | None:
    """Duration, size (rotation-aware) and fps of the first video stream, or None if there is no picture."""
    p = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
                       capture_output=True, text=True)
    if p.returncode:
        return None
    j = json.loads(p.stdout or "{}")
    v = next((s for s in j.get("streams", []) if s.get("codec_type") == "video" and s.get("avg_frame_rate") != "0/0"), None)
    if not v or not v.get("width"):
        return None
    w, h = v["width"], v["height"]
    rot = int((v.get("tags") or {}).get("rotate", 0) or 0)
    for sd in v.get("side_data_list", []) or []:
        rot = int(sd.get("rotation", rot) or rot)
    if abs(rot) % 180 == 90:
        w, h = h, w
    num, den = (v.get("avg_frame_rate") or "0/1").split("/")
    dur = float(j.get("format", {}).get("duration") or v.get("duration") or 0)
    return {"w": w, "h": h, "fps": float(num) / float(den) if float(den) else 0.0, "duration": dur}


def detect_cuts(path: Path, threshold: float = SCENE_THRESHOLD) -> list[float]:
    """Scene-change times via ffmpeg's `select=gt(scene,X)` on a downscaled copy (fast, resolution independent)."""
    p = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(path), "-an", "-vf",
                        f"scale=320:-2,select='gt(scene,{threshold})',showinfo", "-f", "null", "-"], capture_output=True, text=True)
    return sorted(float(m) for m in re.findall(r"pts_time:([0-9.]+)", p.stderr))


def plan_shots(duration: float, cuts: list[float], min_shot_s: float = MIN_SHOT_S, max_shot_s: float = MAX_SHOT_S) -> list[tuple[float, float]]:
    """Cut times -> shot ranges. Slivers shorter than min_shot_s merge into the previous shot; shots longer than
    max_shot_s are sampled into equal windows so a 20-minute locked-off take is still searchable."""
    edges = [0.0]
    for c in cuts:
        if c - edges[-1] >= min_shot_s and duration - c >= min_shot_s:
            edges.append(c)
    edges.append(duration)
    out = []
    for a, b in zip(edges, edges[1:]):
        n = max(1, int(np.ceil((b - a) / max_shot_s - 1e-9)))
        step = (b - a) / n
        out += [(a + i * step, a + (i + 1) * step) for i in range(n)]
    return out


# ---- quality ---------------------------------------------------------------------------------------------------

_FACE = "unset"


def face_backend() -> str:
    """Which face detector is usable here. OpenCV 5 dropped the bundled Haar cascades, so without a YuNet ONNX model
    (env AVE_FACE_MODEL) face presence is UNKNOWN (stored as NULL), never silently 'no faces'. The agent's visual
    check of the contact sheet remains the real face/logo gate."""
    global _FACE
    if _FACE == "unset":
        import cv2
        _FACE = None
        if hasattr(cv2, "CascadeClassifier") and hasattr(cv2, "data"):
            xml = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
            if xml.exists():
                _FACE = ("haar", cv2.CascadeClassifier(str(xml)))
        if _FACE is None and os.environ.get("AVE_FACE_MODEL") and Path(os.environ["AVE_FACE_MODEL"]).is_file() and hasattr(cv2, "FaceDetectorYN"):
            _FACE = ("yunet", os.environ["AVE_FACE_MODEL"])
    return _FACE[0] if _FACE else "none"


def _faces(img, gray) -> int | None:
    face_backend()
    if not _FACE:
        return None
    if _FACE[0] == "haar":
        return int(len(_FACE[1].detectMultiScale(gray, 1.1, 5, minSize=(max(24, gray.shape[1] // 14),) * 2)))
    import cv2
    det = cv2.FaceDetectorYN.create(_FACE[1], "", (img.shape[1], img.shape[0]), 0.7)
    _, found = det.detect(img)
    return 0 if found is None else int(len(found))


def measure(img_path: Path) -> dict:
    """Cheap, deterministic quality signals from a thumbnail: sharpness (Laplacian variance), exposure, face count."""
    import cv2
    img = cv2.imread(str(img_path))
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blur = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    bright = float(gray.mean() / 255.0)
    contrast = float(gray.std() / 255.0)
    faces = _faces(img, gray)
    flags = []
    if blur < BLUR_FLAG:
        flags.append("blurry")
    if bright < UNDER:
        flags.append("underexposed")
    if bright > OVER:
        flags.append("overexposed")
    if faces:
        flags.append("face")
    sharp = min(1.0, blur / BLUR_REF)
    exposure = 1.0 - min(1.0, max(0.0, (UNDER - bright) / UNDER, (bright - OVER) / (1 - OVER)))
    return {"blur": blur, "brightness": bright, "contrast": contrast, "face": faces,
            "quality": round(0.6 * sharp + 0.4 * exposure, 4), "flags": flags}


def _thumb(src: Path, t: float, dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(src), "-frames:v", "1",
                        "-vf", f"scale={THUMB_W}:-2", "-q:v", "3", str(dest)], capture_output=True)
    return r.returncode == 0 and dest.exists() and dest.stat().st_size > 0


# ---- indexing --------------------------------------------------------------------------------------------------

def index_folder(folder, db_path=None, threshold: float = SCENE_THRESHOLD, max_shot_s: float = MAX_SHOT_S,
                 min_shot_s: float = MIN_SHOT_S, force: bool = False, embed: bool = False) -> dict:
    """Index every video under `folder` (recursive). Unchanged files (same size+mtime) are skipped so tags are never lost."""
    folder = Path(folder).expanduser().resolve()
    if not folder.is_dir():
        raise FileNotFoundError(f"{folder} is not a folder")
    con = _connect(db_path)
    thumbs = (Path(db_path) if db_path else default_db()).parent / "thumbs"
    done = skipped = failed = 0
    problems = []
    for f in sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTS and not p.name.startswith(".")):
        st = f.stat()
        row = con.execute("SELECT size, mtime FROM files WHERE path=?", (str(f),)).fetchone()
        if row and not force and row["size"] == st.st_size and abs(row["mtime"] - st.st_mtime) < 1e-3:
            skipped += 1
            continue
        info = probe(f)
        if not info or info["duration"] <= 0:
            failed += 1
            problems.append(f"{f.name}: no readable video stream")
            continue
        for old in con.execute("SELECT thumb FROM shots WHERE file=?", (str(f),)).fetchall():
            Path(old["thumb"]).unlink(missing_ok=True)
        con.execute("DELETE FROM shots WHERE file=?", (str(f),))
        key = hashlib.sha1(str(f).encode()).hexdigest()[:12]
        for i, (a, b) in enumerate(plan_shots(info["duration"], detect_cuts(f, threshold), min_shot_s, max_shot_s)):
            tp = thumbs / f"{key}_{f.stem[:30]}_{i:03d}.jpg"
            if not _thumb(f, (a + b) / 2, tp):
                problems.append(f"{f.name}: thumbnail failed at {a:.1f}s")
                continue
            m = measure(tp)
            asp = "landscape" if info["w"] / info["h"] > 1.15 else "portrait" if info["w"] / info["h"] < 0.87 else "square"
            con.execute("INSERT INTO shots(file,t0,t1,duration,w,h,fps,aspect,thumb,blur,brightness,contrast,face,quality,flags) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (str(f), a, b, b - a, info["w"], info["h"], info["fps"], asp, str(tp), m["blur"], m["brightness"],
                         m["contrast"], m["face"], m["quality"], ",".join(m["flags"])))
        con.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?)",
                    (str(f), st.st_size, st.st_mtime, info["duration"], time.strftime("%Y-%m-%dT%H:%M:%S")))
        con.commit()
        done += 1
    # Drop files that vanished from this folder so search never returns shots we cannot open.
    for r in con.execute("SELECT path FROM files").fetchall():
        if str(r["path"]).startswith(str(folder)) and not Path(r["path"]).exists():
            con.execute("DELETE FROM shots WHERE file=?", (r["path"],))
            con.execute("DELETE FROM files WHERE path=?", (r["path"],))
    con.commit()
    out = {"folder": str(folder), "files": done + skipped + failed, "indexed": done, "skipped_unchanged": skipped,
           "failed": failed, "shots": con.execute("SELECT COUNT(*) FROM shots").fetchone()[0], "problems": problems,
           "face_detection": face_backend(), "embeddings": "skipped (not requested)"}
    con.close()
    if embed:
        try:
            ids = [s["id"] for s in all_shots(db_path) if not s["has_embedding"]]
            out["embeddings"] = f"embedded {embed_shots(db_path, ids)} shots"
        except EmbeddingsUnavailable as e:
            out["embeddings"] = str(e)
    return out


def all_shots(db_path=None) -> list[dict]:
    con = _connect(db_path)
    try:
        return [_row(r) for r in con.execute("SELECT * FROM shots ORDER BY file, t0")]
    finally:
        con.close()


def get_shot(db_path, shot_id: int) -> dict:
    con = _connect(db_path)
    try:
        r = con.execute("SELECT * FROM shots WHERE id=?", (int(shot_id),)).fetchone()
        if not r:
            raise KeyError(f"no shot with id {shot_id}")
        return _row(r)
    finally:
        con.close()


# ---- tags and search -------------------------------------------------------------------------------------------

def parse_tags(text: str) -> list[str]:
    seen, out = set(), []
    for t in re.split(r"[,\n;]", text):
        t = re.sub(r"\s+", " ", t.strip().lower())
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def tag_shot(db_path, shot_id: int, tags: str, replace: bool = False) -> list[str]:
    """Store the agent's description of a shot. Appends by default; the agent should tag what it SAW, not what it hoped for."""
    cur = get_shot(db_path, shot_id)
    new = parse_tags(tags) if replace else cur["tags"] + [t for t in parse_tags(tags) if t not in cur["tags"]]
    con = _connect(db_path)
    con.execute("UPDATE shots SET tags=? WHERE id=?", (json.dumps(new, ensure_ascii=False), int(shot_id)))
    con.commit()
    con.close()
    return new


def _stem(w: str) -> str:
    return w[:-1] if len(w) > 3 and w.endswith("s") else w


def _words(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z0-9]+", text.lower())}


def search(db_path=None, text: str | None = None, min_duration: float | None = None, max_duration: float | None = None,
           aspect: str | None = None, min_quality: float | None = None, exclude_faces: bool = False, limit: int = 20,
           use_embeddings: bool = True) -> list[dict]:
    """Tag search (all query words must appear in the shot's tags or file name) plus hard filters, best quality first.

    Untagged shots only match on filters: the agent has not looked at them yet, so text cannot honestly match them.
    """
    q = [w for w in re.findall(r"[a-z0-9]+", (text or "").lower())]
    qs = [_stem(w) for w in q]
    sem = None
    if q and use_embeddings and embeddings_available():
        sem = _semantic_scores(db_path, text)
    out = []
    for s in all_shots(db_path):
        if min_duration is not None and s["duration"] < min_duration:
            continue
        if max_duration is not None and s["duration"] > max_duration:
            continue
        if aspect and s["aspect"] != aspect:
            continue
        if min_quality is not None and s["quality"] < min_quality:
            continue
        if exclude_faces and s["face"]:
            continue
        s["matched"], s["semantic"] = [], None
        if q:
            have = _words(" ".join(s["tags"])) | (_words(Path(s["file"]).stem) if s["tags"] else set())
            if all(w in have for w in qs):
                s["matched"] = q
            elif sem is not None and sem.get(s["id"], 0) >= SEMANTIC_MIN:
                s["semantic"] = sem[s["id"]]
            else:
                continue
        out.append(s)
    out.sort(key=lambda s: (-(1 if s["matched"] else 0), -(s["semantic"] or 0), -s["quality"]))
    return out[:limit]


# ---- contact sheet ---------------------------------------------------------------------------------------------

def contact_sheet(db_path, shot_ids: list[int], out: Path | str, cols: int = 4, cell_w: int = 320) -> Path:
    """A labeled grid the agent LOOKS at to judge texture, light and authenticity. Label: id, duration, quality, tags, file."""
    from PIL import Image, ImageDraw, ImageFont
    shots = [get_shot(db_path, i) for i in shot_ids]  # raises KeyError naming the missing id
    if not shots:
        raise ValueError("no shot ids given")
    try:
        font = ImageFont.load_default(size=13)
    except TypeError:  # older Pillow: fixed-size default font
        font = ImageFont.load_default()
    label_h, th = 36, int(cell_w * 9 / 16)
    cols = min(cols, len(shots))
    rows = -(-len(shots) // cols)
    sheet = Image.new("RGB", (cols * cell_w, rows * (th + label_h)), (24, 24, 24))
    d = ImageDraw.Draw(sheet)
    for i, s in enumerate(shots):
        x, y = (i % cols) * cell_w, (i // cols) * (th + label_h)
        im = Image.open(s["thumb"]).convert("RGB")
        im.thumbnail((cell_w, th))
        sheet.paste(im, (x + (cell_w - im.width) // 2, y + (th - im.height) // 2))
        d.text((x + 4, y + th + 2), f"#{s['id']}  {s['duration']:.1f}s  q{s['quality']:.2f}" + ("  FACE" if s["face"] else ""), fill=(255, 255, 255), font=font)
        d.text((x + 4, y + th + 18), (", ".join(s["tags"]) or Path(s["file"]).name)[: cell_w // 7], fill=(160, 200, 255), font=font)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, quality=88)
    return out


# ---- optional embeddings ---------------------------------------------------------------------------------------

SEMANTIC_MIN = 0.24
_CLIP = None


def embeddings_available() -> bool:
    try:
        import open_clip  # noqa: F401
        import torch  # noqa: F401
        return True
    except ImportError:
        return False


def _clip():
    global _CLIP
    if not embeddings_available():
        raise EmbeddingsUnavailable("embeddings need open_clip and torch (not installed). Search stays tag-based; "
                                    "install the 'embeddings' extra to add semantic search. Nothing else is affected.")
    if _CLIP is None:
        import open_clip
        model, _, pre = open_clip.create_model_and_transforms("ViT-B-32", pretrained="laion2b_s34b_b79k")
        model.eval()
        _CLIP = (model, pre, open_clip.get_tokenizer("ViT-B-32"))
    return _CLIP


def embed_shots(db_path, shot_ids: list[int]) -> int:
    """Store a CLIP embedding per shot thumbnail. UNVERIFIED on this machine (open_clip not installed here)."""
    model, pre, _ = _clip()
    import torch
    from PIL import Image
    con = _connect(db_path)
    n = 0
    for sid in shot_ids:
        s = get_shot(db_path, sid)
        with torch.no_grad():
            v = model.encode_image(pre(Image.open(s["thumb"]).convert("RGB")).unsqueeze(0))
            v = (v / v.norm(dim=-1, keepdim=True)).squeeze(0).numpy().astype("float32")
        con.execute("UPDATE shots SET embedding=? WHERE id=?", (v.tobytes(), sid))
        n += 1
    con.commit()
    con.close()
    return n


def _semantic_scores(db_path, text) -> dict[int, float]:
    model, _, tok = _clip()
    import torch
    with torch.no_grad():
        t = model.encode_text(tok([text]))
        t = (t / t.norm(dim=-1, keepdim=True)).squeeze(0).numpy().astype("float32")
    con = _connect(db_path)
    out = {r["id"]: float(np.frombuffer(r["embedding"], dtype="float32") @ t)
           for r in con.execute("SELECT id, embedding FROM shots WHERE embedding IS NOT NULL")}
    con.close()
    return out
