"""`ave motion`: brand-consistent motion graphics rendered with alpha."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from .. import motion
from ..episode import Episode


def _spec_from_args(a) -> dict:
    """A spec file, or --template/--props; flags override the file."""
    spec: dict = {}
    if getattr(a, "spec", None):
        try:
            spec = json.loads(Path(a.spec).read_text())
        except (OSError, ValueError) as e:
            raise SystemExit(f"Cannot read spec {a.spec}: {e}")
    if a.template:
        spec["template"] = a.template
    if a.props:
        try:
            spec["props"] = json.loads(a.props)
        except ValueError as e:
            raise SystemExit(f"--props must be JSON, e.g. '{{\"name\": \"Ada\"}}' ({e})")
    for key in ("duration", "fps", "canvas"):
        v = getattr(a, key, None)
        if v is not None:
            spec[key] = v
    if getattr(a, "no_outro", False):
        spec["outro"] = False
    try:
        return motion.normalize_spec(spec)
    except motion.SpecError as e:
        raise SystemExit(str(e))


def cmd_list(a) -> int:
    tk = motion.load_tokens()
    print(json.dumps({"templates": motion.list_templates(), "tokens": str(motion.KIT / "brand" / "tokens.json"),
                      "missing_brand_fonts": motion.missing_brand_fonts(tk)}, indent=2))
    return 0


def cmd_still(a) -> int:
    spec = _spec_from_args(a)
    out = Path(a.out) if a.out else Path(tempfile.gettempdir()) / "ave-motion" / f"{spec['template']}-t{a.t:g}.png"
    png = motion.render_still(spec, a.t, out, safe_area=a.safe_area)
    res = {"png": str(png), "t": a.t, "canvas": list(spec["canvas"]), "template": spec["template"], "safe_area": a.safe_area}
    if a.preview:
        res["preview"] = str(motion.composite_preview(png, png.with_name(png.stem + ".preview.png")))
        res["note"] = "preview is the graphic over a stand-in plate: Read it to judge legibility; the .png keeps the real alpha"
    print(json.dumps(res, indent=2))
    return 0


def cmd_render(a) -> int:
    spec = _spec_from_args(a)
    default = Path(tempfile.gettempdir()) / "ave-motion" / (f"{spec['template']}" + ("" if a.png_sequence else ".mov"))
    out = Path(a.out) if a.out else default
    print(json.dumps(motion.render(spec, out, png_sequence=a.png_sequence), indent=2))
    return 0


def cmd_place(a) -> int:
    ep = Episode(a.episode)
    ep.require()
    try:
        props = json.loads(a.props or "{}")
    except ValueError as e:
        raise SystemExit(f"--props must be JSON ({e})")
    try:
        res = motion.place(ep, a.template, props, at=a.at, dur=a.dur, stage=a.stage, layer=a.layer, name=a.name)
    except motion.SpecError as e:
        raise SystemExit(str(e))
    print(json.dumps(res, indent=2))
    return 0


def cmd_quote_reel(a) -> int:
    from .. import quote_reel
    ep = Episode(a.episode)
    ep.require()
    try:
        quotes = json.loads(Path(a.quotes).read_text())
    except (OSError, ValueError) as e:
        raise SystemExit(f"Cannot read --quotes {a.quotes}: {e}")
    if isinstance(quotes, dict):
        quotes = quotes.get("quotes")
    w, h = motion.parse_canvas(a.canvas)
    print(json.dumps(quote_reel.build(ep, quotes, Path(a.out) if a.out else None, card_s=a.card_s, canvas=(w, h), loops=a.loops), indent=2,
                     ensure_ascii=False))
    return 0


def cmd_swatch(a) -> int:
    w, h = motion.parse_canvas(a.canvas)
    print(json.dumps(motion.swatch(Path(a.out), (w, h)), indent=2))
    return 0


def _add_spec_args(s) -> None:
    s.add_argument("spec", nargs="?", help="spec JSON file: {template, props, duration, fps, canvas, outro}")
    s.add_argument("--template", help="template id (see: ave motion list)")
    s.add_argument("--props", help="props as a JSON object")
    s.add_argument("--duration", type=float)
    s.add_argument("--fps", type=float)
    s.add_argument("--canvas", help="WxH, default 1920x1080")
    s.add_argument("--no-outro", action="store_true", help="hold the final frame instead of animating off")


def register(sub) -> None:
    p = sub.add_parser("motion", help="brand motion graphics: list, still, render, place, quote-reel, swatch")
    m = p.add_subparsers(dest="motion_cmd", required=True)

    s = m.add_parser("list", help="templates, their props, and the brand tokens in use")
    s.set_defaults(fn=cmd_list)

    s = m.add_parser("still", help="render one frame to a PNG (with alpha) to LOOK at")
    _add_spec_args(s)
    s.add_argument("--t", type=float, default=1.2, help="seconds into the animation")
    s.add_argument("--safe-area", action="store_true", help="draw action-safe (pink) and title-safe (blue) guides")
    s.add_argument("--preview", action="store_true", help="also write <out>.preview.png over a stand-in plate for judging legibility")
    s.add_argument("--out", help="PNG path (default: a temp folder)")
    s.set_defaults(fn=cmd_still)

    s = m.add_parser("render", help="render ProRes 4444 with alpha (or --png-sequence)")
    _add_spec_args(s)
    s.add_argument("--out", help=".mov path, or a folder with --png-sequence")
    s.add_argument("--png-sequence", action="store_true")
    s.set_defaults(fn=cmd_render)

    s = m.add_parser("place", help="render a template and add it as an overlay in a NEW edit version")
    s.add_argument("episode")
    s.add_argument("--template", required=True)
    s.add_argument("--props", help="props as a JSON object")
    s.add_argument("--at", type=float, required=True, help="output-time seconds where it starts")
    s.add_argument("--dur", type=float, required=True, help="seconds on screen")
    s.add_argument("--stage", choices=["assembly", "rough", "final", "short"], help="default: the latest of final, rough, assembly")
    s.add_argument("--name", help="named doc: a session (final --name s1) or a short's slug with --stage short")
    s.add_argument("--layer", type=int, help="overlay layer (default: above existing overlays)")
    s.set_defaults(fn=cmd_place)

    s = m.add_parser("quote-reel", help="animated quote cards on a long loop, no speaker audio (its own product; never prepended to a clip)")
    s.add_argument("episode")
    s.add_argument("--quotes", required=True, help='JSON list: [{"quote": "...", "attribution": "Maren", "title": "CO-HOST"}]; each is checked verbatim against the transcript and must start on a complete sentence')
    s.add_argument("--card-s", type=float, default=6.0, help="seconds per card")
    s.add_argument("--loops", type=int, default=1, help="repeat the sequence this many times inside the file (the file loops seamlessly either way)")
    s.add_argument("--canvas", default="1080x1920")
    s.add_argument("--out", help="mp4 path (default: <exports>/quote_reels/quote-reel.vN.mp4; never overwritten)")
    s.set_defaults(fn=cmd_quote_reel)

    s = m.add_parser("swatch", help="alpha/gamma calibration clip to check once in Resolve")
    s.add_argument("--out", default="motion-swatch.mov")
    s.add_argument("--canvas", default="1920x1080")
    s.set_defaults(fn=cmd_swatch)
