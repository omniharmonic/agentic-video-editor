"""Motion graphics: brand tokens, HTML templates, and a deterministic alpha renderer.

Why this design: an open-source repo cannot ship GSAP, and a video pipeline cannot depend on wall-clock animation.
Each template is a plain HTML page that exposes `window.__render(t)` (a pure function of time, see
`motion-kit/lib/timeline.js`). We load it once in headless Chromium and seek it frame by frame (a virtual clock),
screenshot with a transparent background, and encode the PNGs to ProRes 4444 with alpha. Same spec in, same bytes out.

Brand values live only in `motion-kit/brand/tokens.json`; templates read them through CSS variables.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import config, editdoc
from .episode import Episode

KIT = Path(os.environ.get("AVE_MOTION_KIT", config.REPO_ROOT / "motion-kit"))
CALIBRATION = "_calibration"
STAGE_PREFERENCE = ("final", "rough", "assembly")   # `place` default: the most finished cut that exists
DEFAULT_FPS = 30.0
DEFAULT_CANVAS = (1920, 1080)
MOTION_LAYER_BASE = 1


class SpecError(ValueError):
    """The spec or its props are invalid. The message says what to change."""


# ---- kit: tokens and templates -----------------------------------------------------------------

def load_tokens() -> dict:
    return json.loads((KIT / "brand" / "tokens.json").read_text())


def _template_dir(template: str) -> Path:
    return KIT / "templates" / template


def list_templates() -> list[dict]:
    """Production templates (folders starting with `_` are internal, e.g. the calibration clip)."""
    out = []
    for d in sorted((KIT / "templates").iterdir()):
        if d.is_dir() and not d.name.startswith("_") and (d / "template.html").exists():
            schema = json.loads((d / "schema.json").read_text())
            out.append({"id": d.name, "title": schema.get("title", d.name),
                        "default_duration": schema.get("x-default-duration", 5.0),
                        "props": schema.get("properties", {}), "required": schema.get("required", [])})
    return out


def template_ids() -> list[str]:
    return [t["id"] for t in list_templates()]


def example(template: str) -> dict:
    return json.loads((_template_dir(template) / "example.json").read_text())


def _schema(template: str) -> dict:
    d = _template_dir(template)
    if not (d / "template.html").exists():
        raise SpecError(f"unknown template {template!r}. Available: {', '.join(template_ids())}")
    return json.loads((d / "schema.json").read_text())


# ---- spec validation (a deliberately tiny JSON-schema subset; no dependency) -------------------

def validate_props(schema: dict, props: Any) -> tuple[list[str], dict]:
    """Return (problems, props_with_defaults). Supports type, enum, minLength, maxLength, minimum, maximum,
    required, additionalProperties and default: enough for template props, with messages an agent can act on."""
    if not isinstance(props, dict):
        return ["props must be an object"], {}
    problems: list[str] = []
    spec = schema.get("properties", {})
    out = dict(props)
    for key in schema.get("required", []):
        if key not in props:
            problems.append(f"props.{key}: required")
    if schema.get("additionalProperties") is False:
        for key in props:
            if key not in spec:
                problems.append(f"props.{key}: not a property of this template (allowed: {', '.join(spec) or 'none'})")
    for key, rule in spec.items():
        if key not in props:
            if "default" in rule:
                out[key] = rule["default"]
            continue
        v, kind = props[key], rule.get("type")
        if kind == "string" and not isinstance(v, str):
            problems.append(f"props.{key}: must be a string")
            continue
        if kind in ("number", "integer") and (isinstance(v, bool) or not isinstance(v, (int, float))):
            problems.append(f"props.{key}: must be a number")
            continue
        if kind == "boolean" and not isinstance(v, bool):
            problems.append(f"props.{key}: must be true or false")
            continue
        if "enum" in rule and v not in rule["enum"]:
            problems.append(f"props.{key}: must be one of {', '.join(map(str, rule['enum']))} (got {v!r})")
        if isinstance(v, str):
            if len(v) < rule.get("minLength", 0):
                problems.append(f"props.{key}: must not be empty" if rule["minLength"] == 1 else f"props.{key}: at least {rule['minLength']} characters")
            if len(v) > rule.get("maxLength", 10 ** 9):
                problems.append(f"props.{key}: {len(v)} characters, the layout holds at most {rule['maxLength']}; shorten it")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            if "minimum" in rule and v < rule["minimum"]:
                problems.append(f"props.{key}: must be >= {rule['minimum']}")
            if "maximum" in rule and v > rule["maximum"]:
                problems.append(f"props.{key}: must be <= {rule['maximum']}")
    return problems, out


def parse_canvas(c: Any) -> tuple[int, int]:
    try:
        if isinstance(c, str):
            w, h = (int(x) for x in c.lower().split("x"))
        elif isinstance(c, dict):
            w, h = int(c["w"]), int(c["h"])
        else:
            w, h = (int(x) for x in c)
    except Exception:
        raise SpecError(f"canvas must look like 1920x1080 (got {c!r})")
    if w < 64 or h < 64 or w > 7680 or h > 7680:
        raise SpecError(f"canvas {w}x{h} is outside 64..7680 px")
    return w, h


def normalize_spec(spec: dict) -> dict:
    """Validate a spec and fill defaults: {template, props, duration, fps, canvas, outro}."""
    if not isinstance(spec, dict) or "template" not in spec:
        raise SpecError("spec needs a 'template' (one of: " + ", ".join(template_ids()) + ")")
    schema = _schema(spec["template"])
    problems, props = validate_props(schema, spec.get("props", {}))
    duration = spec.get("duration", schema.get("x-default-duration", 5.0))
    fps = spec.get("fps", DEFAULT_FPS)
    if not isinstance(duration, (int, float)) or not 0.2 <= duration <= 600:
        problems.append("duration: seconds between 0.2 and 600")
    if not isinstance(fps, (int, float)) or not 1 <= fps <= 120:
        problems.append("fps: between 1 and 120")
    if problems:
        raise SpecError(f"invalid {spec['template']} spec:\n  - " + "\n  - ".join(problems))
    return {"template": spec["template"], "props": props, "duration": float(duration), "fps": float(fps),
            "canvas": parse_canvas(spec.get("canvas", DEFAULT_CANVAS)), "outro": bool(spec.get("outro", True))}


def spec_hash(spec: dict) -> str:
    """Stable identity of what a render shows (includes the tokens and template source, so edits re-render)."""
    d = _template_dir(spec["template"])
    blob = json.dumps({**spec, "canvas": list(spec["canvas"])}, sort_keys=True)
    h = hashlib.sha256(blob.encode())
    for f in (d / "template.html", KIT / "brand" / "tokens.json", KIT / "lib" / "timeline.js", KIT / "lib" / "base.css"):
        h.update(f.read_bytes())
    return h.hexdigest()[:10]


# ---- renderer ----------------------------------------------------------------------------------

def frame_count(spec: dict) -> int:
    return max(1, round(spec["duration"] * spec["fps"]))


def _font_faces(tokens: dict) -> list[dict]:
    """@font-face sources for brand fonts that actually exist on disk (fallback fonts cover the rest)."""
    faces = []
    for role, f in tokens["fonts"].items():
        for entry in f.get("files", []):
            p = KIT / "brand" / "fonts" / entry["file"]
            if p.exists():
                faces.append({"family": f["family"], "url": p.as_uri(), "weight": entry.get("weight", 400)})
    return faces


def missing_brand_fonts(tokens: dict | None = None) -> list[str]:
    tokens = tokens or load_tokens()
    return [e["file"] for f in tokens["fonts"].values() for e in f.get("files", [])
            if not (KIT / "brand" / "fonts" / e["file"]).exists()]


def _require_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit("Playwright is not installed. Run: uv sync --extra motion  (then: .venv/bin/python -m playwright install chromium)")
    return sync_playwright


@contextmanager
def _page(spec: dict, template_dir: Path | None = None):
    """A page with the template loaded, fonts ready, and the spec's globals injected before any script runs."""
    sync_playwright = _require_playwright()
    tokens = load_tokens()
    w, h = spec["canvas"]
    inject = ("window.__TOKENS__=%s;window.__CANVAS__=%s;window.__PROPS__=%s;window.__DURATION__=%s;"
              "window.__FRAME__=%s;window.__OUTRO__=%s;window.__FONTFACES__=%s;") % (
        json.dumps(tokens), json.dumps({"w": w, "h": h}), json.dumps(spec["props"]),
        json.dumps(frame_count(spec) / spec["fps"]), json.dumps(1.0 / spec["fps"]),
        "true" if spec["outro"] else "false", json.dumps(_font_faces(tokens)))
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(args=["--font-render-hinting=none", "--disable-lcd-text", "--force-color-profile=srgb",
                                              "--hide-scrollbars", "--disable-gpu"])
        except Exception as e:  # missing browser is the usual cause
            raise SystemExit(f"Could not start Chromium ({e}). Run: .venv/bin/python -m playwright install chromium")
        try:
            ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=1)
            page = ctx.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.add_init_script(inject)
            page.goto((template_dir or _template_dir(spec["template"])).joinpath("template.html").as_uri())
            try:
                page.wait_for_function("window.__ready === true", timeout=15000)
            except Exception:
                raise RuntimeError(f"template {spec['template']} did not become ready; page errors: {errors or 'none'}")
            if errors:
                raise RuntimeError(f"template {spec['template']} raised: {errors[0]}")
            yield page
            if errors:
                raise RuntimeError(f"template {spec['template']} raised: {errors[0]}")
        finally:
            browser.close()


def _shot(page, t: float) -> bytes:
    page.evaluate("t => window.__render(t)", t)
    page.evaluate("document.fonts.ready")  # no-op once loaded; keeps layout settled before capture
    return page.screenshot(type="png", omit_background=True)


_GUIDES = """([w, h, a, t]) => {
  const mk = (m, color, id) => { const d = document.createElement('div'); d.id = id;
    const x = Math.round(w * m), y = Math.round(h * m);
    d.style.cssText = `position:absolute;left:${x}px;top:${y}px;width:${w - 2 * x}px;height:${h - 2 * y}px;` +
      `box-sizing:border-box;border:2px solid ${color};pointer-events:none;`; document.body.appendChild(d); };
  mk(a, '#ff2d95', '__guide_action'); mk(t, '#00c8ff', '__guide_title');
}"""


def render_still(spec: dict, t: float, out: Path | str, safe_area: bool = False) -> Path:
    """One PNG (RGBA, transparent background) of the template at time t. This is what the agent LOOKS at."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tk = load_tokens()
    with _page(spec) as page:
        png = _shot(page, t)
        if safe_area:
            w, h = spec["canvas"]
            page.evaluate(_GUIDES, [w, h, tk["safe_area"]["action"], tk["safe_area"]["title"]])
            png = page.screenshot(type="png", omit_background=True)
    out.write_bytes(png)
    return out


def preview_plate(size: tuple[int, int]):
    """A synthetic, footage-like plate (mid-tone gradient with a bright window) to judge legibility over."""
    import numpy as np
    from PIL import Image
    w, h = size
    yy, xx = np.mgrid[0:h, 0:w].astype("float32")
    base = np.stack([86 - 48 * yy / h, 98 - 56 * yy / h, 92 - 52 * yy / h], axis=-1)
    window = np.exp(-(((xx - 0.78 * w) / (0.22 * w)) ** 2 + ((yy - 0.3 * h) / (0.25 * h)) ** 2))[..., None]
    img = base * (1 - window) + np.array([196, 190, 174], dtype="float32") * window
    return Image.fromarray(np.clip(img, 0, 255).astype("uint8"), "RGB").convert("RGBA")


def composite_preview(png: Path | str, out: Path | str) -> Path:
    """Alpha PNG over a footage-like plate, because a transparent PNG is hard to judge by eye."""
    from PIL import Image
    fg = Image.open(png).convert("RGBA")
    Image.alpha_composite(preview_plate(fg.size), fg).convert("RGB").save(out)
    return Path(out)


def encode_prores(frames_dir: Path, fps: float, out: Path) -> None:
    """PNG sequence -> ProRes 4444 with alpha. BT.709 is tagged and used for the RGB->YUV matrix so colours round-trip."""
    cmd = ["ffmpeg", "-v", "error", "-y", "-framerate", f"{fps:g}", "-start_number", "1", "-i", str(frames_dir / "%05d.png"),
           "-vf", "scale=out_color_matrix=bt709:out_range=tv:flags=accurate_rnd+full_chroma_int,format=yuva444p10le,"
                 "setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709",
           "-c:v", "prores_ks", "-profile:v", "4444", "-vendor", "apl0", "-pix_fmt", "yuva444p10le",
           "-movflags", "+write_colr", "-r", f"{fps:g}", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg ProRes encode failed:\n" + r.stderr[-800:])


def render_frames(spec: dict, frames_dir: Path, template_dir: Path | None = None) -> int:
    """Seek the template frame by frame: frame i shows t = i / fps. Returns the frame count."""
    frames_dir.mkdir(parents=True, exist_ok=True)
    n = frame_count(spec)
    with _page(spec, template_dir) as page:
        for i in range(n):
            (frames_dir / f"{i + 1:05d}.png").write_bytes(_shot(page, i / spec["fps"]))
    return n


def render(spec: dict, out: Path | str, png_sequence: bool = False, template_dir: Path | None = None) -> dict:
    """Render a validated spec to a ProRes 4444 .mov (or, with png_sequence, a folder of numbered PNGs)."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if png_sequence:
        n = render_frames(spec, out, template_dir)
    else:
        tmp = Path(tempfile.mkdtemp(prefix="ave-motion-", dir=out.parent))
        try:
            n = render_frames(spec, tmp, template_dir)
            partial = out.with_name(out.stem + ".partial" + out.suffix)
            encode_prores(tmp, spec["fps"], partial)
            partial.replace(out)     # a failed encode never leaves a half-written .mov at the final path
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            out.with_name(out.stem + ".partial" + out.suffix).unlink(missing_ok=True)
    w, h = spec["canvas"]
    return {"out": str(out), "frames": n, "fps": spec["fps"], "duration": n / spec["fps"], "w": w, "h": h,
            "format": "png-sequence" if png_sequence else "prores_4444_alpha", "template": spec["template"]}


def swatch(out: Path | str, canvas: tuple[int, int] = DEFAULT_CANVAS, fps: float = DEFAULT_FPS) -> dict:
    """Calibration clip: a 50% red box and a 100% red box on transparency. Import it in Resolve over a grey clip and
    compare (see docs/motion.md); it tells you once whether alpha and gamma behave."""
    spec = {"template": CALIBRATION, "props": {}, "duration": 2.0, "fps": float(fps), "canvas": canvas, "outro": False}
    return render(spec, out)


# ---- placing an overlay in an EditDoc ----------------------------------------------------------

def _pick_stage(ep: Episode, stage: str | None, name: str | None = None) -> str:
    if stage:
        return stage
    for s in STAGE_PREFERENCE:
        if editdoc.list_versions(ep, s, name):
            return s
    raise SystemExit(f"No final, rough or assembly edit{' ' + name if name else ''} yet for {ep.root.name}. Run: ave assemble {ep.root}")


def place(ep: Episode, template: str, props: dict, at: float, dur: float, stage: str | None = None,
          fps: float | None = None, layer: int | None = None, name: str | None = None) -> dict:
    """Render a template into <ep>/work/motion/ and add it as an alpha overlay in a NEW version of the doc.

    `at` is OUTPUT time (seconds into the cut). The overlay covers the whole canvas (the clip carries its own
    transparency), so x=y=0 and w,h = canvas. The source doc's version is never modified.
    `name` selects a named doc (a session's `final --name s1`, or a short's slug with stage="short"; its canvas is 1080x1920).
    """
    stage = _pick_stage(ep, stage, name)
    doc = editdoc.load_latest(ep, stage, name)  # SystemExit "No <stage> edit yet" if absent; `name` = a session id (rough.s2)
    if at < 0 or dur <= 0:
        raise SpecError("--at must be >= 0 and --dur > 0")
    if at + dur > doc["duration"] + editdoc.EPS:
        raise SpecError(f"overlay would run {at:g}s to {at + dur:g}s but the {stage} cut is only {doc['duration']:g}s long")
    cv = doc["canvas"]
    spec = normalize_spec({"template": template, "props": props, "duration": dur,
                           "fps": fps or doc["fps"], "canvas": (cv["w"], cv["h"])})
    rel = ep.work.relative_to(ep.root) / "motion" / f"{template}-{spec_hash(spec)}.mov"
    target = ep.root / rel
    if not target.exists():
        render(spec, target)
    overlays = doc["tracks"].setdefault("overlays", [])
    used = {o.get("id") for o in overlays}
    n = 1
    while f"m{n}" in used:
        n += 1
    if layer is None:
        layer = max([o.get("layer", 0) for o in overlays] + [MOTION_LAYER_BASE - 1]) + 1
    o0 = round(at, 3)
    ov = {"id": f"m{n}", "file": str(rel), "o0": o0, "o1": round(o0 + spec["duration"], 3), "x": 0, "y": 0,
          "w": cv["w"], "h": cv["h"], "layer": layer, "kind": "motion", "alpha": True,
          "template": template, "props": spec["props"]}
    overlays.append(ov)
    path = editdoc.save_new_version(ep, doc, name)
    return {"doc": str(path), "overlay": ov, "file": str(target)}
