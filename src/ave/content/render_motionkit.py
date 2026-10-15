"""The in-house renderer: still content (posts, thumbnails, carousel slides, reel cards) from `motion-kit/stills/`.

It reuses the machinery behind `ave motion still` (headless Chromium + the template's `window.__ready`, the brand tokens injected
as globals) but is built for FILES you publish: one browser for a whole batch, an opaque RGB PNG, and byte-for-byte
deterministic output (same slots, same style, same machine, same bytes), so a sha256 identifies what the human reviewed.

Every image also gets a critic result measured from the rendered page, not guessed from the text:
  fit        each text block's tight rect lies inside the format's safe area and no block overflowed its box
  contrast   WCAG ratio of each text colour against the pixels actually behind it (text hidden, background sampled)
  size       the smallest text is large enough to read on a phone
"""

from __future__ import annotations

import io
import json
import re
from contextlib import contextmanager
from pathlib import Path

from .. import motion
from ..episode import Episode
from . import manifest as M
from . import people
from . import registry as R
from . import sources as S

RENDERER = "motion-kit"
RENDERER_VERSION = "motion-kit-stills/1"
CARD_HOLD_S = 1.8            # reel-card: the settled frame is judged at this time of the animation


class LicenseError(RuntimeError):
    """An item uses a media file with no license-log entry. Same rule as `ave final`: no license, no render."""


# ---- colour math (WCAG 2.x) -----------------------------------------------------------------------

def _lin(c: float) -> float:
    c /= 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(rgb) -> float:
    r, g, b = (_lin(float(x)) for x in rgb[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a, b) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def parse_css_color(s: str) -> tuple[int, int, int]:
    m = re.match(r"rgba?\(\s*(\d+)[ ,]+(\d+)[ ,]+(\d+)", s)
    if not m:
        raise ValueError(f"cannot read colour {s!r}")
    return int(m[1]), int(m[2]), int(m[3])


# ---- browser session ------------------------------------------------------------------------------

class Session:
    """One Chromium for many pages. Use as a context manager; `shoot` returns the screenshot and the page's measurements."""

    def __init__(self):
        self._pw = None
        self._browser = None

    def __enter__(self) -> "Session":
        sync_playwright = motion._require_playwright()
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(args=["--font-render-hinting=none", "--disable-lcd-text", "--force-color-profile=srgb",
                                                          "--hide-scrollbars", "--disable-gpu"])
        except Exception as e:
            self._pw.stop()
            raise SystemExit(f"Could not start Chromium ({e}). Run: .venv/bin/python -m playwright install chromium")
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self._browser:
                self._browser.close()
        finally:
            if self._pw:
                self._pw.stop()

    def shoot(self, page_dir: Path, canvas: tuple[int, int], props: dict, t: float = 0.0, duration: float = 1.0, fps: float = 30.0,
              want_background: bool = True) -> dict:
        """Render one page. Returns {png: bytes (RGBA from Chromium), background: bytes|None (text hidden), measure: [...], fit: {...}, text: page text, fonts: brand fonts that loaded}."""
        w, h = canvas
        tokens = motion.load_tokens()
        inject = ("window.__TOKENS__=%s;window.__CANVAS__=%s;window.__PROPS__=%s;window.__DURATION__=%s;"
                  "window.__FRAME__=%s;window.__OUTRO__=false;window.__FONTFACES__=%s;") % (
            json.dumps(tokens), json.dumps({"w": w, "h": h}), json.dumps(props), json.dumps(duration), json.dumps(1.0 / fps),
            json.dumps(motion._font_faces(tokens)))
        ctx = self._browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=1)
        try:
            page = ctx.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.add_init_script(inject)
            page.goto((page_dir / "template.html").as_uri())
            try:
                page.wait_for_function("window.__ready === true", timeout=20000)
            except Exception:
                raise RuntimeError(f"{page_dir.name} did not become ready; page errors: {errors or 'none'}")
            if t:
                page.evaluate("t => window.__render(t)", t)
            page.evaluate("document.fonts.ready")
            png = page.screenshot(type="png")
            measure = page.evaluate("window.__measure()")
            fit = page.evaluate("window.__FIT__ || {}")
            dom_text = page.evaluate("document.body.innerText")           # every word on the page, for the 'show name once' checks
            fonts = page.evaluate("Array.from(document.fonts).filter(f => f.status === 'loaded').map(f => f.family.replace(/\"/g, ''))")
            bg = None
            if want_background:
                page.evaluate("document.querySelectorAll('[data-measure]').forEach(e => { e.style.visibility = 'hidden'; })")
                bg = page.screenshot(type="png")
            if errors:
                raise RuntimeError(f"{page_dir.name} raised: {errors[0]}")
            return {"png": png, "background": bg, "measure": measure, "fit": fit, "text": dom_text, "fonts": sorted(set(fonts))}
        finally:
            ctx.close()


# ---- deterministic PNG ----------------------------------------------------------------------------

def to_png_bytes(chromium_png: bytes) -> bytes:
    """Opaque RGB PNG with no metadata. Pillow's encoder is deterministic, so equal pixels give equal bytes."""
    from PIL import Image
    im = Image.open(io.BytesIO(chromium_png)).convert("RGB")
    buf = io.BytesIO()
    im.save(buf, format="PNG", compress_level=6, optimize=False)
    return buf.getvalue()


# ---- the critic -----------------------------------------------------------------------------------

WORDS_ELEMENTS = ("quote", "main", "title")      # the speaker's or the headline's own words: never counted as a repeat of the show name


def show_name_count(measure: list[dict], show: str) -> int:
    """How many measured text blocks print the show name (the speaker's words and headlines excluded). More than one is a double."""
    key = " ".join((show or "").casefold().split())
    if not key:
        return 0
    return sum(1 for e in measure if e["name"] not in WORDS_ELEMENTS and key in " ".join(e["text"].casefold().split()))


def critique(shot: dict, canvas: tuple[int, int], safe: dict, cfg: dict, show: str = "") -> dict:
    """Fit, contrast and size verdict for one rendered image, from the page's own measurements."""
    import numpy as np
    from PIL import Image
    crit = cfg["critic"]
    w, h = canvas
    tol = float(crit.get("tolerance_px", 0.5))
    box = (safe["x"] * w - tol, safe["top"] * h - tol, w - safe["x"] * w + tol, h - safe["bottom"] * h + tol)
    bg = Image.open(io.BytesIO(shot["background"])).convert("RGB") if shot.get("background") else None
    bg_arr = np.asarray(bg, dtype="float64") if bg is not None else None
    notes: list[str] = []
    elements = []
    min_px = float(crit["min_text_px_per_1080"]) * w / 1080.0
    for e in shot["measure"]:
        r = e["rect"]
        inside = r["x"] >= box[0] - float(e.get("hang") or 0) and r["y"] >= box[1] and r["x"] + r["w"] <= box[2] and r["y"] + r["h"] <= box[3]
        ratio = None
        if bg_arr is not None and r["w"] > 0 and r["h"] > 0:
            x0, y0 = max(0, int(r["x"]) - 2), max(0, int(r["y"]) - 2)
            x1, y1 = min(w, int(r["x"] + r["w"]) + 3), min(h, int(r["y"] + r["h"]) + 3)
            patch = bg_arr[y0:y1, x0:x1].reshape(-1, 3)
            fg = parse_css_color(e["color"])
            lin = np.where(patch / 255.0 <= 0.03928, patch / 255.0 / 12.92, ((patch / 255.0 + 0.055) / 1.055) ** 2.4)
            lum = lin @ np.array([0.2126, 0.7152, 0.0722])
            lf = luminance(fg)
            hi, lo = np.maximum(lum, lf), np.minimum(lum, lf)
            ratios = (hi + 0.05) / (lo + 0.05)
            ratio = float(np.percentile(ratios, 1))           # the worst 1% of pixels behind the text, not one stray antialiased dot
        large = e["font_px"] >= 60.0 * w / 1080.0
        need = float(crit["min_contrast_large"] if large else crit["min_contrast"])
        row = {"name": e["name"], "font_px": round(e["font_px"], 1), "inside_safe_area": inside, "overflow": bool(e["overflow"]),
               "contrast": None if ratio is None else round(ratio, 2), "contrast_required": need}
        elements.append(row)
        if not inside:
            notes.append(f"{e['name']}: text reaches outside the safe area ({int(r['x'])},{int(r['y'])} {int(r['w'])}x{int(r['h'])}); shorten it or pick a smaller block")
        if e["overflow"]:
            notes.append(f"{e['name']}: text overflows its box")
        if ratio is not None and ratio < need:
            notes.append(f"{e['name']}: contrast {ratio:.2f}:1 is under the required {need:g}:1; choose another style")
        if e["font_px"] < min_px:
            notes.append(f"{e['name']}: text is {e['font_px']:.0f} px, under the {min_px:.0f} px floor for reading on a phone")
    n_show = show_name_count(shot["measure"], show)
    if n_show > 1:
        notes.append(f"the show name {show!r} is printed {n_show} times on one graphic; it belongs once, at the top")
    for fid, f in (shot.get("fit") or {}).items():
        if not f["ok"]:
            notes.append(f"{fid}: does not fit even at the smallest allowed size ({f['size']:.0f} px); shorten the text")
    contrasts = [x["contrast"] for x in elements if x["contrast"] is not None]
    return {"fits": all(x["inside_safe_area"] and not x["overflow"] for x in elements) and all(f["ok"] for f in (shot.get("fit") or {}).values()),
            "contrast": min(contrasts) if contrasts else None,
            "contrast_ok": all(x["contrast"] is None or x["contrast"] >= x["contrast_required"] for x in elements),
            "min_font_px": min((x["font_px"] for x in elements), default=None),
            "text_size_ok": all(x["font_px"] >= min_px for x in elements),
            "show_name_count": n_show, "ok": not notes, "notes": notes, "elements": elements}


# ---- item -> images -------------------------------------------------------------------------------

def license_check(ep: Episode, item: dict, allow_unlicensed: bool = False) -> list[str]:
    """Every media file an item uses must have a license-log entry for exactly these bytes (the rule `ave final` applies)."""
    files = [a if isinstance(a, str) else a["file"] for a in (item.get("license") or {}).get("assets", [])]
    if not files:
        return []
    from ..assets import licenses
    cov = licenses.coverage(ep, files)
    bad = [m["file"] for m in cov["missing"]] + list(cov["unreadable"])
    if bad and not allow_unlicensed:
        raise LicenseError("These files have no license-log entry, so this item cannot be rendered:\n  - " + "\n  - ".join(bad) +
                           "\nLog each one with `ave assets log-license` (own photo, consented guest photo, purchased image), or pass "
                           "--allow-unlicensed only if you own the rights and accept the risk.")
    return [f"UNLICENSED asset allowed by flag: {f}" for f in bad]


def _slides(slots: dict) -> list[dict]:
    """Carousel slots -> one dict per slide with its position."""
    seq = [{"kind": "cover", **slots["cover"]}]
    for s in slots["slides"]:
        seq.append({k: v for k, v in s.items() if k != "source"})
    seq.append({"kind": "closing", **slots["closing"]})
    total = len(seq)
    return [{**s, "index": i, "total": total} for i, s in enumerate(seq, 1)]


def default_alt_text(item: dict, slots: dict) -> list[str]:
    """Accurate-by-construction alt text (one string per image); the agent can override with item.alt_text."""
    t = item["template"]
    show = slots.get("show") or ""
    if t == "quote-post":
        who = ", ".join(x for x in (slots.get("attribution"), slots.get("title") or slots.get("role")) if x)
        return [f"Quote card{(' from ' + show) if show else ''}: “{slots['quote']}”" + (f" — {who}." if who else "")]
    if t == "thumbnail":
        return [f"Episode thumbnail{(' for ' + show) if show else ''}: “{slots['title']}”" + (f", with {slots['guest']}." if slots.get("guest") else ".")
                + (" A photograph of the guest sits on the right." if slots.get("image") else "")]
    if t == "reel-card":
        return [f"{slots['kind'].capitalize()} card: “{slots['title']}”" + (f", {slots['subtitle']}." if slots.get("subtitle") else ".")]
    if t == "carousel":
        out = []
        for s in _slides(slots):
            n, tot = s["index"], s["total"]
            if s["kind"] == "cover":
                out.append(f"Slide {n} of {tot}, cover: “{s['title']}”" + (f", {s['subtitle']}." if s.get("subtitle") else "."))
            elif s["kind"] == "quote":
                out.append(f"Slide {n} of {tot}, quote: “{s['quote']}”"
                           + (f" — {', '.join(x for x in (s['attribution'], s.get('title')) if x)}." if s.get("attribution") else ""))
            elif s["kind"] == "point":
                out.append(f"Slide {n} of {tot}: {s['text']}")
            else:
                out.append(f"Slide {n} of {tot}, closing: {s['line']}" + (f" {s['cta']}" if s.get("cta") else ""))
        return out
    return [item["template"]]


def _props(tpl: R.Template, item: dict, ep: Episode, canvas: tuple[int, int], cfg: dict) -> dict:
    styles = R.load_styles()
    # the show name prints once (a slot repeating it is blanked); a speaker's attribution gets their display name and title (CO-HOST)
    slots = R.effective_slots(tpl.slots, item["slots"], people.load_speakers(ep))
    props = {**slots, "_style": R.resolve_style(item["style"]), "_type": styles["type"], "_tracking": styles["tracking"],
             "_safe": cfg["safe_area"][item["format"]], "_format": item["format"]}
    if tpl.id == "thumbnail" and slots.get("image"):
        img = (ep.root / slots["image"]) if not Path(slots["image"]).is_absolute() else Path(slots["image"])
        if not img.is_file():
            raise FileNotFoundError(f"thumbnail image not found: {img}")
        props["_image_url"] = img.resolve().as_uri()
    return props


def render_item(ep: Episode, item: dict, session: Session, out_dir: Path, reg: R.Registry, cfg: dict, *,
                allow_unlicensed: bool = False, want_background: bool = True) -> dict:
    """Render one item to PNG file(s). Returns {outputs, critic, provenance, warnings}; the caller stores them in a new doc version.

    `outputs` has one entry per (image, platform). A carousel yields one image per slide. Files are written under `out_dir`
    (named from the item id), hashed, and byte-identical for identical inputs.
    """
    from ..publish import safety
    tpl = reg.get(item["template"])
    warnings = license_check(ep, item, allow_unlicensed)
    canvas = tpl.size(item["format"])
    base = _props(tpl, item, ep, canvas, cfg)
    page_dir = tpl.still_dir(item["renderer"])
    pages = [(None, base)]
    if tpl.id == "carousel":
        pages = [(s["index"], {**base, "_slide": s}) for s in _slides(base)]
    t = CARD_HOLD_S if tpl.id == "reel-card" else 0.0
    out_dir.mkdir(parents=True, exist_ok=True)
    alts = item.get("alt_text")
    alts = [alts] if isinstance(alts, str) and alts else (alts if isinstance(alts, list) and alts else default_alt_text(item, base))
    if len(alts) != len(pages):
        alts = default_alt_text(item, base)
    platforms = item.get("platforms") or tpl.default_platforms or ["any"]
    outputs, per_image, notes = [], [], []
    for k, (idx, props) in enumerate(pages):
        shot = session.shoot(page_dir, canvas, props, t=t, duration=max(t, 1.0) + 1.0, want_background=want_background)
        data = to_png_bytes(shot["png"])
        name = f"{item['id']}.png" if idx is None else f"{item['id']}-s{idx:02d}.png"
        path = out_dir / name
        tmp = path.with_suffix(".png.tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        sha = safety.sha256_file(path)
        crit = critique(shot, canvas, cfg["safe_area"][item["format"]], cfg, show=base.get("show") or "")
        per_image.append({"file": ep.rel(path), **crit})
        notes += [(f"slide {idx}: " if idx else "") + n for n in crit["notes"]]
        for plat in platforms:
            o = {"platform": plat, "w": canvas[0], "h": canvas[1], "file": ep.rel(path), "sha256": sha, "alt_text": alts[k]}
            if idx is not None:
                o["slide"] = idx
            outputs.append(o)
    contrasts = [c["contrast"] for c in per_image if c["contrast"] is not None]
    critic = {"fits": all(c["fits"] for c in per_image), "contrast": min(contrasts) if contrasts else None,
              "contrast_ok": all(c["contrast_ok"] for c in per_image), "text_size_ok": all(c["text_size_ok"] for c in per_image),
              "ok": all(c["ok"] for c in per_image), "notes": notes, "images": per_image}
    prov = {"renderer_version": RENDERER_VERSION, "template_rev": tpl.rev(), "tokens_sha256": M.tokens_sha(),
            }
    return {"outputs": outputs, "critic": critic, "provenance": prov, "warnings": warnings}


def render_doc(ep: Episode, doc: dict, *, item_ids: list[str] | None = None, force: bool = False, allow_unlicensed: bool = False,
               reg: R.Registry | None = None, cfg: dict | None = None) -> tuple[dict, dict]:
    """Render the doc's draft items (or the named ones) into a NEW doc (the caller saves it). Returns (new_doc, report).

    Skips items already rendered with the same template revision unless `force`, and refuses when a source drifted on disk.
    An approved item is never re-rendered silently: a changed render needs a new approval, so it is only redone when named.
    """
    from . import settings
    cfg = cfg or settings.load()
    reg = reg or R.load_registry(cfg=cfg)
    new = M.clone(doc)
    rebound = S.rebind_clip_sources(ep, new)        # clips renumbered since the plan: follow the identical text (recorded in the new version)
    drift = S.verify_sources(ep, new)
    if drift:
        raise SystemExit("Sources changed since the plan was made:\n  - " + "\n  - ".join(drift))
    # Renders are append-only like the doc: every render pass writes into renders/v<next doc version>/, never over an earlier file.
    out_dir = M.content_dir(ep) / "renders" / f"v{(M.list_versions(ep, doc['id']) or [0])[-1] + 1}"
    report = {"rendered": [], "skipped": [], "failed": [], "out_dir": ep.rel(out_dir)}
    if rebound:
        report["rebound_sources"] = rebound
    todo = []
    for it in new["items"]:
        if item_ids is not None and it["id"] not in item_ids:
            continue
        if it["review"]["status"] == "rejected":
            report["skipped"].append({"id": it["id"], "why": "rejected"})
            continue
        if it["review"]["status"] == "approved" and not item_ids:
            report["skipped"].append({"id": it["id"], "why": "approved (name it to re-render; that voids the approval)"})
            continue
        if it["renderer"] == "canva" and not item_ids:
            report["skipped"].append({"id": it["id"], "why": "rendered by Canva (name it to fall back to the in-house renderer)"})
            continue
        if it["outputs"] and not force and not item_ids and it["provenance"].get("template_rev") == reg.get(it["template"]).rev():
            report["skipped"].append({"id": it["id"], "why": "already rendered with the current template"})
            continue
        todo.append(it)
    if not todo:
        return new, report
    speakers = people.load_speakers(ep)
    with Session() as session:
        for it in todo:
            if it["renderer"] == "canva":      # the named fall-back: back to the in-house renderer, and the record says so
                prev = it["provenance"].get("canva") or {}
                it["renderer"] = RENDERER
                it["provenance"]["canva"] = {"design_id": None, "template_id": None, "fallback": {"to": RENDERER, "was": prev.get("design_id"),
                                                                                              **(prev.get("fallback") or {})}}
            fixed = R.normalize_slots(reg.get(it["template"]).slots, it["slots"], speakers)
            if fixed != it["slots"]:
                report.setdefault("normalized", []).append({"id": it["id"], "from": {k: v for k, v in it["slots"].items() if fixed.get(k) != v},
                                                            "to": {k: v for k, v in fixed.items() if it["slots"].get(k) != v}})
                it["slots"] = fixed
            try:
                res = render_item(ep, it, session, out_dir, reg, cfg, allow_unlicensed=allow_unlicensed)
            except LicenseError:
                raise
            except Exception as e:
                report["failed"].append({"id": it["id"], "error": f"{type(e).__name__}: {e}"})
                continue
            it["outputs"], it["provenance"] = res["outputs"], {**it["provenance"], **res["provenance"]}
            it["review"] = {"status": "draft", "critic": res["critic"], "by": None, "at": None, "note": it["review"].get("note", "")}
            report["rendered"].append({"id": it["id"], "images": len({o["file"] for o in res["outputs"]}), "ok": res["critic"]["ok"],
                                       "contrast": res["critic"]["contrast"], "notes": res["critic"]["notes"], "warnings": res["warnings"]})
    return new, report


def render_card_video(ep: Episode, item: dict, out: Path, reg: R.Registry, cfg: dict, duration: float = 3.0, fps: float = 30.0) -> dict:
    """The animated reel card as ProRes 4444 through `ave motion`'s frame renderer and encoder (an overlay-ready .mov)."""
    if item["template"] != "reel-card":
        raise ValueError("only reel-card items render to video")
    tpl = reg.get("reel-card")
    canvas = tpl.size(item["format"])
    props = _props(tpl, item, ep, canvas, cfg)
    spec = {"template": "reel-card", "props": props, "duration": float(duration), "fps": float(fps), "canvas": canvas, "outro": True}
    return motion.render(spec, out, template_dir=tpl.still_dir())
