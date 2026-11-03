"""Procedural pictures for `ave studio demo`: synthetic stand-ins for photographs and stock footage frames. No network, no model, no
files read: every pixel comes from a seeded numpy generator, so the same call gives the same bytes (Pillow's encoder is deterministic).

They are illustrations, not photographs. They exist so a demo studio can show real library assets, product cards, B-roll thumbnails and
a license trail without anyone's photos or any provider's.
"""

from __future__ import annotations

import io
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

W, H = 1500, 1000


def _rng(seed: int) -> np.random.Generator:
    return np.random.default_rng(seed)


def _noise(w: int, h: int, cells: int, seed: int) -> np.ndarray:
    """Smooth value noise in [0, 1], `cells` lattice cells across the width."""
    r = _rng(seed)
    gh = max(2, round(cells * h / w) + 2)
    grid = r.random((gh, cells + 2)).astype("float32")
    im = Image.fromarray((grid * 255).astype("uint8")).resize((w, h), Image.BICUBIC)
    return np.asarray(im, dtype="float32") / 255.0


def _fbm(w: int, h: int, seed: int, base: int = 3, octaves: int = 4) -> np.ndarray:
    out, amp, tot = np.zeros((h, w), "float32"), 1.0, 0.0
    for o in range(octaves):
        out += amp * _noise(w, h, base * 2 ** o, seed + o * 17)
        tot += amp
        amp *= 0.5
    return out / tot


def _lerp(c0, c1, t):
    c0, c1 = np.array(c0, "float32"), np.array(c1, "float32")
    return c0 + (c1 - c0) * t[..., None]


def _to_img(a: np.ndarray) -> Image.Image:
    return Image.fromarray(np.clip(a, 0, 255).astype("uint8"), "RGB")


def _vignette(a: np.ndarray, strength: float = 0.45) -> np.ndarray:
    h, w = a.shape[:2]
    y, x = np.mgrid[0:h, 0:w].astype("float32")
    d = np.sqrt(((x - w / 2) / (w / 2)) ** 2 + ((y - h / 2) / (h / 2)) ** 2)
    return a * (1 - strength * np.clip(d - 0.35, 0, 1.2)[..., None] ** 1.4)


def _grain(a: np.ndarray, seed: int, amount: float = 6.0) -> np.ndarray:
    return a + _rng(seed).normal(0, amount, a.shape[:2]).astype("float32")[..., None]


def _grade(a: np.ndarray, warm: float = 1.0, lift: float = 0.0) -> np.ndarray:
    a = a.copy()
    a[..., 0] *= 1.0 + 0.05 * warm
    a[..., 2] *= 1.0 - 0.07 * warm
    return a + lift


def _blur(im: Image.Image, r: float) -> Image.Image:
    return im.filter(ImageFilter.GaussianBlur(r))


def _soft_mask(size, draw_fn, blur: float = 3.0) -> Image.Image:
    m = Image.new("L", size, 0)
    draw_fn(ImageDraw.Draw(m))
    return _blur(m, blur)


def _shade(mask: Image.Image, base, light, light_dir=(-0.5, -0.6), seed: int = 1, tex: float = 0.18) -> Image.Image:
    """Colour an object (given by its mask) with a bulged-surface light model and a bit of crust-like texture."""
    w, h = mask.size
    m = np.asarray(mask, "float32") / 255.0
    dist = np.asarray(_blur(mask, 40), "float32") / 255.0            # 0 at the edge, ->1 inside: a cheap height field
    gy, gx = np.gradient(dist)
    lx, ly = light_dir
    lit = np.clip(0.55 + (gx * lx + gy * ly) * 55.0, 0, 1)
    t = np.clip(lit * 0.9 + dist * 0.25, 0, 1)
    t = t * (1 - tex) + _fbm(w, h, seed, 14, 3) * tex
    return Image.fromarray(np.clip(_lerp(base, light, t), 0, 255).astype("uint8"), "RGB"), Image.fromarray((m * 255).astype("uint8"))


def _paste_shaded(canvas: Image.Image, mask: Image.Image, base, light, seed=1, shadow=(0, 0, 0), shadow_off=(26, 34), shadow_blur=28,
                  shadow_alpha=0.5, light_dir=(-0.5, -0.6), tex: float = 0.18) -> None:
    sh = _blur(mask, shadow_blur).point(lambda v: int(v * shadow_alpha))
    sh = ImageChops.offset(sh, *shadow_off)
    canvas.paste(Image.new("RGB", canvas.size, shadow), (0, 0), sh)
    obj, m = _shade(mask, base, light, light_dir=light_dir, seed=seed, tex=tex)
    canvas.paste(obj, (0, 0), m)


def _wood(w: int, h: int, seed: int, dark=(86, 52, 30), light=(176, 120, 72), planks: int = 5, vertical: bool = False) -> Image.Image:
    g = _fbm(w, h, seed, 2, 3)
    stretch = _noise(w, h, 3, seed + 9)
    # grain: long streaks along the plank
    streak = _rng(seed).random((h, 1 if not vertical else w)).astype("float32")
    if vertical:
        streak = np.repeat(_rng(seed).random((1, w)), h, axis=0)
    else:
        streak = np.repeat(streak, w, axis=1)
    streak = np.asarray(_blur(Image.fromarray((streak * 255).astype("uint8")), 1.2), "float32") / 255.0
    t = np.clip(0.55 * g + 0.25 * streak + 0.2 * stretch, 0, 1)
    a = _lerp(dark, light, t)
    ph = h // planks
    for i in range(1, planks):                                           # plank seams
        y = i * ph
        a[max(0, y - 2):y + 2, :, :] *= 0.45
        a[y + 2:y + 6, :, :] *= 1.06
        a[(i % 2) * 0 + y: y + ph, :, :] *= 0.94 + 0.12 * ((i * 37) % 5) / 5
    return _to_img(a)


def _speckle(canvas: Image.Image, n: int, seed: int, box, color=(250, 246, 236), rmax: float = 2.4, alpha: int = 190) -> None:
    r = _rng(seed)
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    x0, y0, x1, y1 = box
    for _ in range(n):
        x, y, s = r.uniform(x0, x1), r.uniform(y0, y1), r.uniform(0.6, rmax)
        d.ellipse([x - s, y - s, x + s, y + s], fill=(*color, int(r.uniform(alpha * 0.4, alpha))))
    canvas.paste(layer, (0, 0), layer)


def _bokeh(canvas: Image.Image, seed: int, n: int, color, area, rmin=16, rmax=70, alpha=(30, 110)) -> None:
    r = _rng(seed)
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    x0, y0, x1, y1 = area
    for _ in range(n):
        x, y, s = r.uniform(x0, x1), r.uniform(y0, y1), r.uniform(rmin, rmax)
        c = tuple(int(min(255, v * r.uniform(0.8, 1.15))) for v in color)
        d.ellipse([x - s, y - s, x + s, y + s], fill=(*c, int(r.uniform(*alpha))))
    layer = _blur(layer, 2.2)
    canvas.paste(layer, (0, 0), layer)


def _finish(im: Image.Image, seed: int, warm=1.0, vig=0.42, grain=5.0) -> Image.Image:
    a = np.asarray(im, "float32")
    a = _vignette(_grade(a, warm), vig)
    return _to_img(_grain(a, seed, grain))


# ------------------------------------------------------------------------------------------------ bakery photographs

def loaf(seed: int = 11) -> Image.Image:
    bg = _blur(_wood(W, H, seed), 7)
    cloth = _noise(W, H, 6, seed + 3)
    a = np.asarray(bg, "float32")
    band = np.zeros((H, W), "float32")
    band[:, 140:760] = 1.0                                                  # a linen runner under the loaf
    band = np.asarray(_blur(Image.fromarray((band * 255).astype("uint8")), 3), "float32") / 255.0
    lin = _lerp((214, 204, 184), (238, 230, 214), cloth)
    a = a * (1 - band[..., None] * 0.95) + lin * band[..., None] * 0.95
    canvas = _to_img(a)
    m = _soft_mask((W, H), lambda d: d.ellipse([360, 250, 1100, 800], fill=255), 2.2)
    _paste_shaded(canvas, m, (120, 62, 24), (232, 160, 84), seed=seed, shadow_alpha=0.55)
    # slashed ear: pale scored opening, drawn as soft strokes
    ear = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(ear)
    for k in range(4):
        x = 560 + k * 90
        d.arc([x - 10, 330, x + 190, 660], 215, 320, fill=255, width=16)
    ear = _blur(ear, 4)
    canvas.paste(Image.new("RGB", canvas.size, (244, 214, 160)), (0, 0), ear.point(lambda v: int(v * 0.85)))
    _speckle(canvas, 900, seed + 4, (330, 230, 1130, 820), alpha=120, rmax=2.2)           # flour dust
    _bokeh(canvas, seed + 5, 22, (255, 214, 150), (900, 20, 1500, 500), 14, 50, (14, 48))
    return _finish(canvas, seed, 1.1)


def rye(seed: int = 23) -> Image.Image:
    bg = _blur(_wood(W, H, seed, (60, 40, 28), (132, 94, 62), planks=4, vertical=True), 7)
    m = _soft_mask((W, H), lambda d: d.rounded_rectangle([250, 330, 1250, 700], radius=210, fill=255), 2.4)
    _paste_shaded(bg, m, (74, 42, 22), (178, 118, 66), seed=seed, shadow_alpha=0.6)
    cuts = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(cuts)
    for k in range(5):
        x = 420 + k * 150
        d.line([(x, 390), (x + 90, 640)], fill=255, width=14)
    bg.paste(Image.new("RGB", bg.size, (214, 168, 112)), (0, 0), _blur(cuts, 3).point(lambda v: int(v * 0.7)))
    r = _rng(seed + 1)                                                          # caraway seeds
    layer = ImageDraw.Draw(bg)
    for _ in range(260):
        x, y = r.uniform(300, 1200), r.uniform(360, 680)
        if m.getpixel((int(x), int(y))) > 200:
            layer.ellipse([x - 3, y - 1.4, x + 3, y + 1.4], fill=(238, 212, 150))
    _speckle(bg, 500, seed + 2, (260, 340, 1240, 700), alpha=110)
    return _finish(bg, seed, 1.0, 0.5)


def bun(seed: int = 31) -> Image.Image:
    a = np.asarray(_blur(_wood(W, H, seed, (196, 168, 130), (232, 208, 172), planks=3), 9), "float32")
    canvas = _to_img(a * 0.98)
    for i, (cx, cy, r) in enumerate([(520, 520, 230), (1010, 470, 215), (800, 760, 205)]):
        m = _soft_mask((W, H), lambda d, cx=cx, cy=cy, r=r: d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=255), 2.0)
        _paste_shaded(canvas, m, (150, 84, 36), (240, 176, 104), seed=seed + i, shadow_alpha=0.42, shadow_off=(20, 28), shadow_blur=24)
        sw = Image.new("L", (W, H), 0)
        d = ImageDraw.Draw(sw)
        pts = []
        for k in range(0, 420):                                                  # a spiral of knotted dough
            t = k / 420.0
            ang = t * 5.2 * math.pi + i
            rad = 12 + t * (r - 40)
            pts.append((cx + rad * math.cos(ang), cy + rad * math.sin(ang) * 0.92))
        d.line(pts, fill=255, width=11, joint="curve")
        canvas.paste(Image.new("RGB", canvas.size, (250, 216, 160)), (0, 0), _blur(sw, 3).point(lambda v: int(v * 0.55)))
        _speckle(canvas, 140, seed + 9 + i, (cx - r, cy - r, cx + r, cy + r), color=(255, 252, 244), rmax=3.4, alpha=230)   # pearl sugar
    _bokeh(canvas, seed + 6, 14, (255, 236, 190), (0, 0, 1500, 280), 20, 60, (20, 60))
    return _finish(canvas, seed, 1.05, 0.38)


def soup(seed: int = 41) -> Image.Image:
    a = np.asarray(_blur(_wood(W, H, seed, (40, 36, 34), (92, 84, 78), planks=4), 8), "float32")
    canvas = _to_img(a)
    cx, cy = 760, 520
    rim = _soft_mask((W, H), lambda d: d.ellipse([cx - 420, cy - 420, cx + 420, cy + 420], fill=255), 1.6)
    _paste_shaded(canvas, rim, (196, 190, 178), (246, 242, 232), seed=seed, shadow_off=(34, 42), shadow_alpha=0.6, shadow_blur=34)
    well = _soft_mask((W, H), lambda d: d.ellipse([cx - 330, cy - 330, cx + 330, cy + 330], fill=255), 2.0)
    broth = np.asarray(canvas, "float32")
    wm = np.asarray(well, "float32")[..., None] / 255.0
    yy, xx = np.mgrid[0:H, 0:W].astype("float32")
    rad = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2) / 330.0
    soupc = _lerp((142, 70, 20), (214, 130, 48), np.clip(1.0 - rad, 0, 1) * 0.9 + _fbm(W, H, seed, 6, 3) * 0.3)
    canvas = _to_img(broth * (1 - wm) + soupc * wm)
    r = _rng(seed + 2)
    d = ImageDraw.Draw(canvas, "RGBA")
    for _ in range(46):                                                         # onion ribbons
        a0 = r.uniform(0, 2 * math.pi)
        dist = r.uniform(0, 250)
        x, y = cx + dist * math.cos(a0), cy + dist * math.sin(a0)
        w_, h_ = r.uniform(40, 110), r.uniform(10, 22)
        ang = r.uniform(0, 180)
        d.arc([x - w_, y - w_, x + w_, y + w_], ang, ang + r.uniform(40, 110), fill=(246, 208, 120, 210), width=int(h_ * 0.5))
    for _ in range(60):                                                         # thyme and pepper
        x, y = cx + r.uniform(-280, 280), cy + r.uniform(-280, 280)
        if (x - cx) ** 2 + (y - cy) ** 2 < 290 ** 2:
            d.ellipse([x - 4, y - 2.5, x + 4, y + 2.5], fill=(70, 100, 48, 230))
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([cx - 200, cy - 250, cx - 40, cy - 110], fill=(255, 238, 200, 70))
    canvas.paste(_blur(glow, 30), (0, 0), _blur(glow, 30))
    return _finish(canvas, seed, 1.0, 0.55)


def _jar(seed: int, level: float, label: str) -> Image.Image:
    a = np.asarray(_blur(_wood(W, H, seed, (110, 86, 62), (186, 154, 118), planks=4), 10), "float32")
    canvas = _to_img(a)
    x0, x1, y0, y1 = 560, 940, 160, 860
    glass = _soft_mask((W, H), lambda d: d.rounded_rectangle([x0, y0, x1, y1], radius=46, fill=255), 1.6)
    sh = _blur(glass, 30).point(lambda v: int(v * 0.5))
    canvas.paste(Image.new("RGB", canvas.size, (30, 20, 10)), (0, 0), ImageChops.offset(sh, 40, 36))
    top = y1 - int((y1 - y0 - 60) * level)
    dough = _soft_mask((W, H), lambda d: d.rounded_rectangle([x0 + 14, top, x1 - 14, y1 - 12], radius=34, fill=255), 1.4)
    inner, _ = _shade(dough, (222, 204, 170), (250, 240, 214), light_dir=(-0.9, -0.2), seed=seed, tex=0.3)
    arr = np.asarray(canvas, "float32")
    g = np.asarray(glass, "float32")[..., None] / 255.0
    arr = arr * (1 - g * 0.35) + np.array([210, 228, 224], "float32") * g * 0.35                    # the glass body
    canvas = _to_img(arr)
    canvas.paste(inner, (0, 0), dough)
    r = _rng(seed + 3)
    d = ImageDraw.Draw(canvas, "RGBA")
    for _ in range(int(40 + 220 * level)):                                      # bubbles in the dough
        x, y = r.uniform(x0 + 30, x1 - 30), r.uniform(top + 16, y1 - 30)
        s = r.uniform(3, 12)
        d.ellipse([x - s, y - s, x + s, y + s], fill=(255, 250, 232, 150), outline=(190, 170, 130, 120))
    d.rounded_rectangle([x0 + 22, y0 + 20, x0 + 54, y1 - 24], radius=14, fill=(255, 255, 255, 60))   # window reflection
    d.rectangle([x0 - 6, top - 4, x1 + 6, top - 1], fill=(205, 70, 60, 255))                          # the rubber band
    d.rounded_rectangle([x0 + 70, 420, x1 - 70, 540], radius=10, fill=(244, 238, 224, 235))           # label
    d.line([(x0 + 96, 468), (x1 - 96, 468)], fill=(70, 50, 36, 220), width=7)
    d.line([(x0 + 96, 500), (x0 + 96 + (x1 - x0 - 192) // 2, 500)], fill=(70, 50, 36, 160), width=5)
    return _finish(canvas, seed, 1.05, 0.45)


def jar_before(seed: int = 51) -> Image.Image:
    return _jar(seed, 0.34, "day 1")


def jar_after(seed: int = 52) -> Image.Image:
    return _jar(seed, 0.86, "day 5")


# ------------------------------------------------------------------------------------------------ the show

def table(seed: int = 61) -> Image.Image:
    base = np.zeros((H, W, 3), "float32") + np.array([22, 16, 14], "float32")
    canvas = _to_img(base + _fbm(W, H, seed, 3, 3)[..., None] * 18)
    wood = _wood(W, 560, seed, (70, 42, 26), (150, 100, 60), planks=3)
    # a long table receding in perspective: a trapezoid mapped from the plank texture
    top = Image.new("L", (W, H), 0)
    ImageDraw.Draw(top).polygon([(300, 430), (1200, 430), (1500, 1000), (0, 1000)], fill=255)
    tex = Image.new("RGB", (W, H))
    tex.paste(wood.resize((W, 570)), (0, 430))
    canvas.paste(tex, (0, 0), _blur(top, 2))
    _bokeh(canvas, seed + 1, 46, (255, 196, 110), (80, 40, 1420, 430), 18, 72, (26, 100))
    d = ImageDraw.Draw(canvas, "RGBA")
    r = _rng(seed + 2)
    for k, x in enumerate([330, 520, 700, 880, 1060, 1230]):                    # plates down the table
        y = 640 + (k % 2) * 40
        s = 70 + (k % 3) * 14
        d.ellipse([x - s, y - s * 0.34, x + s, y + s * 0.34], fill=(236, 230, 218, 255), outline=(200, 190, 176, 255), width=3)
        d.ellipse([x - s * 0.62, y - s * 0.2, x + s * 0.62, y + s * 0.2], fill=(222, 214, 200, 255))
        if k in (1, 4):
            d.ellipse([x - 22, y - 12, x + 24, y + 10], fill=(176, 110, 52, 255))
    for x, y in [(430, 560), (800, 540), (1150, 565)]:                          # candles with a glow
        glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(glow).ellipse([x - 120, y - 190, x + 120, y + 50], fill=(255, 190, 90, 120))
        canvas.paste(_blur(glow, 46), (0, 0), _blur(glow, 46))
        d.rectangle([x - 9, y - 10, x + 9, y + 70], fill=(240, 228, 200, 255))
        d.ellipse([x - 8, y - 46, x + 8, y - 6], fill=(255, 214, 120, 255))
    return _finish(canvas, seed, 1.15, 0.5, 4)


def mic(seed: int = 71) -> Image.Image:
    base = np.zeros((H, W, 3), "float32") + np.array([30, 28, 32], "float32")
    yy = np.linspace(0, 1, H, dtype="float32")[:, None, None]
    canvas = _to_img(base + 40 * (1 - yy) + _fbm(W, H, seed, 3, 3)[..., None] * 14)
    cx = 750
    body = _soft_mask((W, H), lambda d: d.rounded_rectangle([cx - 150, 180, cx + 150, 620], radius=150, fill=255), 1.6)
    _paste_shaded(canvas, body, (58, 58, 64), (176, 176, 186), light_dir=(-0.8, -0.2), seed=seed, tex=0.08, shadow_alpha=0.6)
    mesh = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(mesh)
    for y in range(210, 600, 14):
        d.line([(cx - 140, y), (cx + 140, y)], fill=90, width=2)
    for x in range(cx - 140, cx + 141, 14):
        d.line([(x, 200), (x, 610)], fill=90, width=2)
    canvas.paste(Image.new("RGB", canvas.size, (20, 20, 24)), (0, 0), ImageChops.multiply(mesh, body))
    d = ImageDraw.Draw(canvas)
    d.arc([cx - 230, 380, cx + 230, 800], 20, 160, fill=(150, 150, 160), width=14)
    d.rectangle([cx - 14, 800, cx + 14, 940], fill=(120, 120, 130))
    d.rounded_rectangle([cx - 150, 920, cx + 150, 960], radius=18, fill=(90, 90, 100))
    rim = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(rim).ellipse([cx + 40, 120, cx + 520, 560], fill=(255, 170, 90, 90))
    canvas.paste(_blur(rim, 60), (0, 0), _blur(rim, 60))
    return _finish(canvas, seed, 1.1, 0.5)


# ------------------------------------------------------------------------------------------------ stock-style stills (B-roll)

def _sky(h: int, top, mid, bottom) -> np.ndarray:
    t = np.linspace(0, 1, h, dtype="float32")
    upper = _lerp(top, mid, np.clip(t / 0.62, 0, 1))
    lower = _lerp(mid, bottom, np.clip((t - 0.62) / 0.38, 0, 1))
    col = np.where((t < 0.62)[:, None], upper, lower)
    return np.repeat(col[:, None, :], W, axis=1)


def wheat_dawn(seed: int = 81) -> Image.Image:
    sky = _sky(H, (92, 120, 170), (246, 170, 120), (255, 214, 150))
    canvas = _to_img(sky)
    sun = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(sun).ellipse([980, 330, 1240, 590], fill=(255, 236, 190, 255))
    canvas.paste(_blur(sun, 70), (0, 0), _blur(sun, 70))
    canvas.paste(_blur(sun, 6), (0, 0), _blur(sun, 6).point(lambda v: int(v * 0.9)))
    a = np.asarray(canvas, "float32")
    for i, (base, amp, col) in enumerate([(560, 70, (118, 92, 98)), (640, 50, (150, 110, 80)), (730, 40, (196, 150, 70))]):
        n = _noise(W, 1, 4 + i, seed + i)[0]
        edge = (base + (n - 0.5) * amp * 2).astype(int)
        for x in range(W):
            a[edge[x]:, x, :] = np.array(col, "float32") * (0.92 + 0.1 * (x / W))
    canvas = _to_img(a)
    d = ImageDraw.Draw(canvas, "RGBA")
    r = _rng(seed + 9)
    for _ in range(620):                                                        # foreground stalks, leaning with the wind
        x = r.uniform(0, W)
        y = r.uniform(780, 1010)
        ln = r.uniform(120, 260)
        lean = 40 + 0.04 * (x - W / 2)
        d.line([(x, y), (x + lean * 0.4, y - ln)], fill=(214, 164, 70, 255), width=3)
        d.ellipse([x + lean * 0.4 - 4, y - ln - 26, x + lean * 0.4 + 5, y - ln + 4], fill=(236, 190, 84, 255))
    return _finish(_blur(canvas, 0.8), seed, 1.0, 0.35, 3)


def steam(seed: int = 82) -> Image.Image:
    a = np.zeros((H, W, 3), "float32") + np.array([26, 22, 22], "float32")
    canvas = _to_img(a + _fbm(W, H, seed, 3, 3)[..., None] * 16)
    _bokeh(canvas, seed + 1, 40, (255, 178, 96), (0, 0, 1500, 700), 22, 90, (22, 90))
    plume = np.zeros((H, W), "float32")
    for k in range(5):
        n = _fbm(W, H, seed + 20 + k, 3, 4)
        cx = 360 + k * 190
        yy, xx = np.mgrid[0:H, 0:W].astype("float32")
        wob = (n - 0.5) * 260
        plume += np.exp(-(((xx - cx - wob) / 120.0) ** 2)) * np.clip((yy - 80) / 820.0, 0, 1) ** 0.6 * np.clip(n * 1.6 - 0.35, 0, 1)
    plume = np.clip(plume, 0, 1)
    arr = np.asarray(canvas, "float32")
    arr = arr + plume[..., None] * np.array([210, 205, 196], "float32") * 0.7
    cut = np.zeros((H, W), "float32")
    cut[800:] = 1
    arr = arr * (1 - cut[..., None] * 0.7)                                   # the pot's dark rim along the bottom
    return _finish(_to_img(arr), seed, 1.1, 0.5)


def river_stones(seed: int = 83) -> Image.Image:
    a = _lerp((52, 86, 100), (150, 190, 196), _fbm(W, H, seed, 4, 4))
    canvas = _to_img(a)
    r = _rng(seed + 1)
    for i in range(12):
        cx, cy = r.uniform(0, W), r.uniform(260, 1000)
        rx, ry = r.uniform(70, 190), r.uniform(46, 110)
        m = _soft_mask((W, H), lambda d, cx=cx, cy=cy, rx=rx, ry=ry: d.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=255), 1.5)
        g = int(r.uniform(90, 160))
        _paste_shaded(canvas, m, (g - 40, g - 30, g - 16), (min(255, g + 70), min(255, g + 66), min(255, g + 54)), seed=seed + i,
                      shadow=(14, 30, 36), shadow_off=(10, 14), shadow_blur=14, shadow_alpha=0.5)
    caustic = np.maximum(_fbm(W, H, seed + 40, 9, 3) - 0.52, 0) * 5
    arr = np.asarray(canvas, "float32") + caustic[..., None] * np.array([90, 110, 100], "float32") * 0.55
    return _finish(_to_img(arr), seed, 0.6, 0.35, 3)


def awning(seed: int = 84) -> Image.Image:
    a = _lerp((250, 226, 188), (206, 168, 124), np.repeat(np.linspace(0, 1, H, dtype="float32")[:, None], W, 1))
    canvas = _to_img(a)
    d = ImageDraw.Draw(canvas)
    sw = 150
    for i in range(-1, W // sw + 2):                                            # a striped market awning in perspective
        x0, x1 = i * sw, (i + 1) * sw
        col = (192, 64, 52) if i % 2 == 0 else (246, 238, 222)
        d.polygon([(x0 * 1.0, 0), (x1 * 1.0, 0), (W / 2 + (x1 - W / 2) * 1.5, 340), (W / 2 + (x0 - W / 2) * 1.5, 340)], fill=col)
    for i in range(-1, W // sw + 2):
        x0 = W / 2 + (i * sw - W / 2) * 1.5
        col = (192, 64, 52) if i % 2 == 0 else (246, 238, 222)
        d.pieslice([x0, 300, x0 + sw * 1.5, 380], 0, 180, fill=col)
    for k, x in enumerate(range(80, 1450, 240)):                                # crates of produce below
        y = 640 + (k % 3) * 40
        d.rectangle([x, y, x + 200, y + 150], fill=(150, 108, 64))
        for j in range(9):
            cx, cy = x + 28 + (j % 5) * 36, y - 4 + (j // 5) * 10
            col = [(206, 70, 40), (230, 170, 50), (120, 150, 60)][(k + j) % 3]
            d.ellipse([cx - 20, cy - 20, cx + 20, cy + 20], fill=col)
    canvas = _blur(canvas, 3.5)
    return _finish(canvas, seed, 1.0, 0.4, 3)


def rooftops(seed: int = 85) -> Image.Image:
    sky = _sky(H, (38, 44, 94), (200, 96, 110), (252, 176, 110))
    canvas = _to_img(sky)
    r = _rng(seed)
    d = ImageDraw.Draw(canvas)
    for layer, (col, base, hmax) in enumerate([((92, 62, 96), 640, 260), ((58, 40, 70), 760, 300), ((26, 20, 36), 880, 330)]):
        x = -40
        while x < W:
            bw = r.integers(70, 190)
            bh = r.integers(60, hmax)
            d.rectangle([x, base - bh, x + bw, H], fill=col)
            if layer == 2:
                for wy in range(base - bh + 24, base - 10, 40):
                    for wx in range(x + 14, x + bw - 14, 30):
                        if r.random() < 0.34:
                            d.rectangle([wx, wy, wx + 12, wy + 18], fill=(255, 210, 120))
            x += bw + int(r.integers(0, 24))
    return _finish(_blur(canvas, 1.0), seed, 1.1, 0.4, 3)


def flour_cloud(seed: int = 86) -> Image.Image:
    a = np.zeros((H, W, 3), "float32") + np.array([32, 28, 26], "float32")
    puff = np.clip((_fbm(W, H, seed, 3, 5) - 0.42) * 2.6, 0, 1) ** 1.2
    yy, xx = np.mgrid[0:H, 0:W].astype("float32")
    puff *= np.exp(-(((xx - 760) / 640.0) ** 2 + ((yy - 520) / 380.0) ** 2))
    a = a + puff[..., None] * np.array([232, 226, 214], "float32")
    canvas = _to_img(a)
    _speckle(canvas, 1400, seed + 2, (200, 120, 1300, 900), alpha=170, rmax=2.0)
    return _finish(canvas, seed, 0.8, 0.5, 4)


def episode_art(seed: int = 87) -> Image.Image:
    """Marked in the library as AI-generated (it is not: it stands in for a generated image so the disclosure flag has something to show)."""
    a = _lerp((34, 30, 72), (238, 144, 96), _fbm(W, H, seed, 2, 4))
    canvas = _to_img(a)
    d = ImageDraw.Draw(canvas, "RGBA")
    r = _rng(seed)
    for i in range(9):
        cx = 180 + i * 150
        s = 60 + (i % 3) * 30
        d.ellipse([cx - s, 500 - s, cx + s, 500 + s], outline=(255, 240, 220, 150), width=4)
    for i in range(36):
        x = r.uniform(0, W)
        d.line([(x, 640), (x + r.uniform(-60, 60), 1000)], fill=(255, 236, 210, 40), width=3)
    return _finish(_blur(canvas, 2), seed, 1.0, 0.45, 4)


PHOTOS = {"loaf": loaf, "rye": rye, "bun": bun, "soup": soup, "jar_before": jar_before, "jar_after": jar_after, "table": table, "mic": mic}
STOCK = {"wheat_dawn": wheat_dawn, "steam": steam, "river_stones": river_stones, "awning": awning, "rooftops": rooftops,
         "flour_cloud": flour_cloud, "episode_art": episode_art}


def save_jpeg(im: Image.Image, path: Path, quality: int = 90) -> Path:
    """Deterministic JPEG bytes (no metadata, fixed settings)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    im.convert("RGB").save(buf, format="JPEG", quality=quality, subsampling=0, optimize=False, progressive=False)
    path.write_bytes(buf.getvalue())
    return path
