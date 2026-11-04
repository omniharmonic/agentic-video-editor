"""Placing library B-roll into an EditDoc: `ave broll place|apply|remove|undo|list|check`.

A studio-library video or image becomes an overlay of `kind: "broll"` in a NEW version of a rough, final, short (or any) edit. The moves:

  cutaway   the whole frame for [at, at+dur): the speaker's picture is covered, the dialogue plays on untouched. (Default.)
  overlay   a picture-in-picture box; the speaker stays visible around it.
  insert    a ripple: the cut gets `dur` longer at `at`, the picture is the B-roll, the dialogue holds still (exact silence) for `dur`.

Why this shape (the rules the rest of the repo relies on):

  * Explicit and reversible. `place` WITHOUT `--apply` writes a proposal (`work/broll/proposals/<id>.json`) and changes no edit. Applying
    writes a new `edits/<stage>.vN.json` (never in place) with a `history` entry that names what was done, by whom, and the inverse. `remove`
    drops one placement; `undo` restores the version the last B-roll operation started from (as a NEW version).
  * Silent unless asked. A placement is silent (`audio: mute`) and the dialogue master is bit-identical with or without it. `audio: keep`
    mixes the B-roll's own sound under the dialogue (ducked), by the same compiler pass that mixes music.
  * License closure. Every placed asset must be covered AND human-attested in the studio library (`ave.library.coverage`) before it can be
    placed, and `ave.compile.render` re-checks at render time (an unlicensed or unattested asset, or a file whose bytes changed, blocks the
    render with a message that names the asset). The render's `derived_from` is the list of placed asset ids; the license's attribution
    text is collected for the description / credits, and, when the license requires it, shown on screen as a `credit` overlay.

Nothing here downloads, spends or posts. Placing is not a human act, but the licence attestation it depends on is.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from pathlib import Path

import yaml

from .. import editdoc, ffutil, resources
from ..episode import Episode, read_json, write_json

CONFIG = resources.config_file("broll_place.yaml")
MODES = ("cutaway", "overlay", "insert")
STAGE_PREFERENCE = ("final", "rough", "assembly")
EPS = editdoc.EPS
PROPOSAL_DIR = ("broll", "proposals")
OPS = ("broll.place", "broll.remove", "broll.undo")


class PlaceError(SystemExit):
    """A refusal with a message the human can act on (exit status 1, like every other `ave` refusal)."""


def _merge(a: dict, b: dict) -> dict:
    out = copy.deepcopy(a)
    for k, v in (b or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def load_config(ep: Episode | None = None) -> dict:
    cfg = yaml.safe_load(CONFIG.read_text())
    if ep is not None:
        p = ep.root / "policy" / "broll_place.yaml"
        if p.is_file():
            cfg = _merge(cfg, yaml.safe_load(p.read_text()) or {})
    return cfg


def _by() -> str:
    from ..learning import _by as who
    return who()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _r(x: float) -> float:
    return round(float(x), 4)


# ---- the studio, the asset, its license -------------------------------------------------------------------------------

def resolve_studio(ep: Episode, studio: str | Path | None = None):
    """The studio whose library holds the asset: `--studio DIR`, else `<workspace>/studio`."""
    from ..studio.project import StudioProject, find_studio
    sp = StudioProject(Path(studio).expanduser()) if studio else find_studio(None)
    sp.require()
    return sp


def studio_ref(ep: Episode, sp) -> str:
    """The studio's path relative to the episode root (artifacts store paths relative to it)."""
    return Path(os.path.relpath(sp.root.resolve(), ep.root.resolve())).as_posix()


def studio_of(ep: Episode, lib: dict):
    from ..studio.project import StudioProject
    return StudioProject((ep.root / lib["studio"]).resolve())


def _license_message(asset: str, missing: list[dict]) -> str:
    why = "; ".join(f"{m.get('asset', asset)}: {m['why']}" for m in missing) or "unknown"
    return (f"cannot use library asset {asset}: its license closure is not covered and attested ({why}). "
            "A human must attest the license claim first: `! ave studio library attest " + asset + "` in their own terminal "
            "(an unlicensed asset has no trail; a stock asset arrives attested through `ave studio broll pick`).")


def library_overlays(doc: dict) -> list[dict]:
    """The B-roll overlays that came from a studio library (they carry `library`). Their license lives in the library, not in the episode's log."""
    return [o for o in doc["tracks"].get("overlays") or [] if o.get("kind") == "broll" and o.get("library")]


def placements(doc: dict) -> list[dict]:
    return [o for o in doc["tracks"].get("overlays") or [] if o.get("kind") == "broll"]


def closure(ep: Episode, doc: dict) -> dict:
    """The license closure of every library asset placed in `doc`:

        {"required": bool, "ok": bool, "assets": [ids], "derived_from": [ids], "attributions": [required credit lines],
         "optional_attributions": [courtesy lines], "covered": [...], "missing": [{asset, why}], "problems": [text]}

    `ok` needs: the studio resolves, every placed file exists with the bytes recorded at placement AND the registry's bytes, and
    `ave.library.coverage` finds every asset (and what it was derived from) covered by a human-attested license entry."""
    from ..studio import episode_worker_bridge as bridge
    if bridge.CLIENT is not None and library_overlays(doc):
        # The parent derives the exact candidate and permitted assets from its
        # signed job; the worker cannot choose a scope or inspect the ledger key.
        return bridge.CLIENT.call('final-broll')
    out = {"required": False, "ok": True, "assets": [], "derived_from": [], "attributions": [], "optional_attributions": [],
           "covered": [], "missing": [], "problems": []}
    lib_ovs = library_overlays(doc)
    if not lib_ovs:
        return out
    out["required"] = True
    from .. import library
    from ..assets import licenses
    by_studio: dict[str, list[dict]] = {}
    for o in lib_ovs:
        by_studio.setdefault(o["library"]["studio"], []).append(o)
    for ref, ovs in by_studio.items():
        sp = studio_of(ep, {"studio": ref})
        if not sp.is_studio():
            out["problems"].append(f"the studio {ref!r} that holds the placed B-roll was not found (looked in {sp.root})")
            out["ok"] = False
            continue
        recs = library.read_all(sp)
        ids = list(dict.fromkeys(o["library"]["asset"] for o in ovs))
        for o in ovs:
            a = o["library"]["asset"]
            rec = recs.get(a)
            f = ep.root / o["file"]
            if rec is None:
                out["problems"].append(f"B-roll {o['id']}: asset {a} is not in the library registry of {ref}")
                continue
            if not f.is_file():
                out["problems"].append(f"B-roll {o['id']}: the file {o['file']} is missing")
            elif licenses.sha256_file(f) != rec["sha256"] or o["library"].get("sha256") not in (None, rec["sha256"]):
                out["problems"].append(f"B-roll {o['id']}: {o['file']} no longer has the bytes of library asset {a} (the file was changed after it was licensed)")
        cov = library.coverage(sp, ids)
        out["covered"] += cov["covered"]
        out["missing"] += cov["missing"]
        out["attributions"] += [t for t in cov["attributions"] if t not in out["attributions"]]
        for a in ids:
            att = ((recs.get(a) or {}).get("license") or {}).get("attribution") or {}
            if att.get("text") and not att.get("required") and att["text"] not in out["optional_attributions"]:
                out["optional_attributions"].append(att["text"])
        out["assets"] += [a for a in ids if a not in out["assets"]]
        # a render is derived from the placed assets AND, through them, everything they were derived from (the closure already walks it)
        out["derived_from"] += [a for a in ids if a not in out["derived_from"]]
    out["ok"] = not out["missing"] and not out["problems"]
    return out


def credit_asset(o: dict, used_in: list[str]) -> dict:
    """An asset record in the shape `ave.sequence` uses for its credits roll, taken from a library placement (its license was checked when it was
    placed and is checked again at render time)."""
    att = o.get("attribution") or {}
    return {"file": o["file"], "role": "broll", "used_in": used_in, "status": "licensed", "license": "studio library", "source": "studio library",
            "author": None, "url": None, "attribution_required": bool(att.get("required")), "attribution_text": att.get("text") or "",
            "title": ""}


def blocked_message(c: dict) -> str:
    parts = [f"{m['asset']}: {m['why']}" for m in c["missing"]] + list(c["problems"])
    return ("the render is blocked: the placed B-roll is not licensed and attested in its studio library.\n  - " + "\n  - ".join(parts) +
            "\nA human attests with `! ave studio library attest <asset>` in their own terminal (see skills/ave-studio-broll). "
            "Remove the placement with `ave broll remove` to render without it.")


def require_licensed(ep: Episode, doc: dict) -> dict:
    """Raise `PlaceError` unless every placed library asset is covered and attested. Returns the closure (used for the render report)."""
    c = closure(ep, doc)
    if not c["ok"]:
        raise PlaceError(blocked_message(c))
    return c


# ---- geometry and options -------------------------------------------------------------------------------------------------

def _even(x: float) -> int:
    return max(2, int(round(x / 2.0)) * 2)


def _box(doc: dict, cfg: dict, mode: str, rec: dict, corner: str | None, scale: float | None, box: list[float] | None) -> tuple[int, int, int, int]:
    W, H = doc["canvas"]["w"], doc["canvas"]["h"]
    if mode != "overlay":
        return 0, 0, W, H
    if box:
        x, y, w, h = (int(round(v)) for v in box)
        if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > W or y + h > H:
            raise PlaceError(f"--box {x},{y},{w},{h} is not inside the {W}x{H} canvas")
        return x, y, _even(w), _even(h)
    oc = cfg["overlay"]
    portrait = H > W
    s = float(scale if scale is not None else oc["scale_portrait" if portrait else "scale_landscape"])
    if not 0.05 <= s <= 1.0:
        raise PlaceError("--scale is the box width as a fraction of the canvas width, between 0.05 and 1.0")
    aspect = (rec.get("w") or W) / (rec.get("h") or H) if rec.get("w") and rec.get("h") else W / H
    w = _even(W * s)
    h = _even(w / aspect)
    if h > H:
        h = _even(H)
        w = _even(h * aspect)
    m = int(round(float(oc["margin"]) * W))
    c = corner or oc["corner"]
    if c not in ("tl", "tr", "bl", "br", "center"):
        raise PlaceError("--corner must be tl, tr, bl, br or center")
    x = {"tl": m, "bl": m, "tr": W - w - m, "br": W - w - m, "center": (W - w) // 2}[c]
    y = {"tl": m, "tr": m, "bl": H - h - m, "br": H - h - m, "center": (H - h) // 2}[c]
    return max(0, x), max(0, y), w, h


def _ken_burns(spec, cfg: dict) -> dict | None:
    if not spec:
        return None
    presets = cfg["ken_burns"]
    if spec is True:
        spec = "push_in"
    if isinstance(spec, str):
        s = spec.strip()
        if s.startswith("{"):
            try:
                spec = json.loads(s)
            except ValueError as e:
                raise PlaceError(f"--ken-burns is neither a preset ({', '.join(presets)}) nor JSON: {e}") from e
        else:
            if s not in presets:
                raise PlaceError(f"unknown Ken Burns preset {s!r}; choose {', '.join(presets)} or pass JSON {{zoom_from, zoom_to, from, to}}")
            spec = presets[s]
    kb = {"zoom_from": float(spec.get("zoom_from", 1.0)), "zoom_to": float(spec.get("zoom_to", 1.12)),
          "from": [float(v) for v in spec.get("from", (0.5, 0.5))], "to": [float(v) for v in spec.get("to", (0.5, 0.5))]}
    if min(kb["zoom_from"], kb["zoom_to"]) < 1.0 or max(kb["zoom_from"], kb["zoom_to"]) > 3.0:
        raise PlaceError("Ken Burns zoom must stay between 1.0 and 3.0 (below 1.0 would show past the picture's edge)")
    if any(not 0.0 <= v <= 1.0 for v in kb["from"] + kb["to"]) or len(kb["from"]) != 2 or len(kb["to"]) != 2:
        raise PlaceError("Ken Burns from/to are [x, y] positions between 0 and 1")
    return kb


def _next_id(doc: dict, prefix: str) -> str:
    used = {s.get("id") for n in ("video", "dialogue", "music", "overlays") for s in doc["tracks"].get(n) or []}
    n = 1
    while f"{prefix}{n}" in used:
        n += 1
    return f"{prefix}{n}"


def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---- the on-screen credit ---------------------------------------------------------------------------------------------

def render_credit(ep: Episode, text: str, canvas: tuple[int, int], cfg: dict) -> tuple[str, int, int]:
    """The attribution as a small PNG (dark bar, white text) in work/broll/credits/. Returns (path relative to the episode, w, h)."""
    from PIL import Image, ImageDraw, ImageFont
    cc = cfg["credit"]
    W, H = canvas
    portrait = H > W
    px = int(cc["font_px_portrait" if portrait else "font_px_landscape"])
    fonts = resources.home() / "motion-kit" / "brand" / "fonts"
    font = None
    for f in sorted(fonts.glob("*.[ot]tf")) if fonts.is_dir() else []:
        try:
            font = ImageFont.truetype(str(f), px)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default(px)
    maxw = int(W * float(cc["max_width"]))
    pad = max(8, px // 3)
    probe = ImageDraw.Draw(Image.new("RGB", (4, 4)))
    lines: list[str] = []
    for para in text.splitlines() or [""]:
        cur = ""
        for word in para.split():
            trial = (cur + " " + word).strip()
            if probe.textlength(trial, font=font) + 2 * pad <= maxw or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        lines.append(cur)
    lh = int(px * 1.25)
    tw = int(max(probe.textlength(ln, font=font) for ln in lines)) + 2 * pad
    w, h = _even(min(tw, maxw)), _even(lh * len(lines) + 2 * pad)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=pad, fill=(0, 0, 0, 170))
    for i, ln in enumerate(lines):
        d.text((pad, pad + i * lh), ln, font=font, fill=(255, 255, 255, 255))
    key = hashlib.sha256(json.dumps([text, px, w, h]).encode()).hexdigest()[:12]
    rel = ep.work.relative_to(ep.root) / "broll" / "credits" / f"credit-{key}.png"
    target = ep.root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    img.save(target)
    return rel.as_posix(), w, h


# ---- checks ---------------------------------------------------------------------------------------------------------------

def _pacing(doc: dict, new: dict, extra_dur: float, beats: list[dict] | None) -> dict:
    from ..assets import critic
    ov = [{"id": o["id"], "o0": o["o0"], "o1": o["o1"]} for o in placements(doc)] + [{"id": new["id"], "o0": new["o0"], "o1": new["o1"]}]
    return critic.check_placements(ov, doc["duration"] + extra_dur, beats or [], critic.load_config(), music=doc["tracks"].get("music") or [])


def _word_at(ep: Episode, doc: dict, at: float) -> str | None:
    """The spoken word an output time falls inside (an insert point there cuts a word), or None."""
    try:
        from .. import completeness
        words = completeness.transcript_words(ep)
        seg = next((s for s in doc["tracks"]["dialogue"] if s["o0"] - EPS <= at < s["o1"] and not s.get("fill") and "e0" in s), None)
        if seg is None:
            return None
        w = completeness.slices_word(words, seg["e0"] + (at - seg["o0"]))
        return w["w"] if w else None
    except (OSError, ValueError, KeyError):
        return None


def _probe(path: Path) -> dict:
    try:
        return ffutil.probe(path)
    except Exception as e:      # an unreadable file is reported, never guessed at
        raise PlaceError(f"cannot read {path.name} with ffprobe: {e}") from e


# ---- the proposal -----------------------------------------------------------------------------------------------------

def _stage_for(ep: Episode, stage: str | None, name: str | None) -> str:
    if stage:
        return stage
    for s in STAGE_PREFERENCE:
        if editdoc.list_versions(ep, s, name):
            return s
    raise PlaceError(f"No final, rough or assembly edit{' ' + name if name else ''} yet for {ep.root.name}. Run: ave assemble {ep.root}")


def proposals_dir(ep: Episode) -> Path:
    return ep.work.joinpath(*PROPOSAL_DIR)


def propose(ep: Episode, *, asset: str, at: float, dur: float, mode: str = "cutaway", stage: str | None = None, name: str | None = None,
            studio: str | Path | None = None, src_in: float = 0.0, audio: str = "mute", gain_db: float | None = None,
            duck_db: float | None = None, ken_burns=None, corner: str | None = None, scale: float | None = None,
            box: list[float] | None = None, fit: str | None = None, credit: str | None = None, beats: list[dict] | None = None,
            allow_hold: bool | None = None, strict_pacing: bool = False, reason: str = "", by: str | None = None,
            layer: int | None = None, explicit_base: tuple[Path, dict, int] | None = None, write: bool = True) -> dict:
    """Validate a placement and write it as a PROPOSAL (`work/broll/proposals/<id>.json`). No edit is changed. Raises `PlaceError` with the
    reason when the placement cannot be made (unlicensed asset, wrong kind, outside the cut, too little footage, overlapping cutaway...)."""
    cfg = load_config(ep)
    if mode not in MODES:
        raise PlaceError(f"mode must be one of {', '.join(MODES)}")
    if audio not in ("mute", "keep"):
        raise PlaceError("audio must be mute (default: B-roll is silent) or keep")
    credit = credit or cfg["credit"]["mode"]
    if credit not in ("auto", "always", "never"):
        raise PlaceError("credit must be auto, always or never")
    stage = _stage_for(ep, stage, name)
    if explicit_base is not None:
        base_path,doc,base_version=explicit_base
    else:
        if not editdoc.list_versions(ep, stage, name):
            raise PlaceError(f"No {stage} edit{' ' + name if name else ''} yet for {ep.root.name}.")
        base_path = editdoc.latest_path(ep, stage, name)
        base_version = editdoc.list_versions(ep, stage, name)[-1]
        doc = editdoc.load(base_path)
    fps = ffutil.fps_fraction(doc["fps"])

    sp = resolve_studio(ep, studio)
    from .. import library
    try:
        rec = library.get(sp, asset)
    except KeyError:
        raise PlaceError(f"no library asset {asset!r} in {sp.root}/library (see `ave studio library search`)")
    if rec["kind"] not in ("video", "image"):
        raise PlaceError(f"asset {asset} is {rec['kind']}: B-roll is a video or an image (music goes in with `ave assets`)")
    cov = library.coverage(sp, [asset])
    if not cov["ok"]:
        raise PlaceError(_license_message(asset, cov["missing"]))
    fpath = library.registry.asset_file(sp, rec)
    att = (rec.get("license") or {}).get("attribution") or {}
    if att.get("required") and not (att.get("text") or "").strip():
        raise PlaceError(f"asset {asset}: its license requires attribution but no credit text is on record; "
                         "fix the license entry before placing it (ave studio broll pick records the provider's attribution text)")

    if at < 0 or dur <= 0:
        raise PlaceError("--at must be >= 0 and --dur > 0")
    warnings: list[str] = []
    if mode != "insert" and at + dur > doc["duration"] + EPS:
        raise PlaceError(f"the placement would run {at:g}s to {at + dur:g}s but the {stage} cut is only {doc['duration']:g}s long")
    if mode == "insert" and at > doc["duration"] + EPS:
        raise PlaceError(f"insert point {at:g}s is after the end of the {stage} cut ({doc['duration']:g}s)")
    # frames are the unit the compiler uses: a placement shorter than a frame would vanish
    if ffutil.frame_of(at + dur, fps) - ffutil.frame_of(at, fps) < 1:
        raise PlaceError(f"{dur:g}s is shorter than one frame at {float(fps):.3f} fps")

    is_video = rec["kind"] == "video"
    if not is_video and (src_in or audio == "keep"):
        raise PlaceError("--in and --audio keep only make sense for a video asset")
    if is_video:
        info = _probe(fpath)
        if not info["has_video"]:
            raise PlaceError(f"asset {asset} has no picture")
        if audio == "keep" and not info["has_audio"]:
            raise PlaceError(f"asset {asset} has no sound, so --audio keep has nothing to keep")
        need = float(src_in) + dur
        have = info["duration"] or rec.get("duration") or 0.0
        hold = cfg["placement"]["allow_hold"] if allow_hold is None else allow_hold
        if need > have + 1.0 / float(fps):
            msg = (f"the placement needs {need:.2f}s of video (--in {src_in:g} + {dur:g}s) but asset {asset} is {have:.2f}s long")
            if not hold:
                raise PlaceError(msg + "; shorten --dur, lower --in, or pass --allow-hold to freeze the last frame")
            warnings.append(msg + ": the last frame is held")
    kb = _ken_burns(ken_burns, cfg)
    if kb and is_video:
        raise PlaceError("--ken-burns is for stills; a video already moves")

    x, y, w, h = _box(doc, cfg, mode, rec, corner, scale, box)
    lay = int(layer if layer is not None else cfg[mode]["layer"])
    fitv = fit or cfg[mode]["fit"]
    if fitv not in ("cover", "contain", "stretch"):
        raise PlaceError("--fit must be cover, contain or stretch")
    if mode in ("cutaway", "insert"):
        for o in placements(doc):
            if o.get("mode") in ("cutaway", "insert") and o["o0"] < at + dur - EPS and at < o["o1"] - EPS and mode == "cutaway":
                raise PlaceError(f"the cutaway {at:g}-{at + dur:g}s overlaps B-roll {o['id']} ({o['o0']:g}-{o['o1']:g}s): remove it first or choose another time")
        if mode == "insert":
            for o in placements(doc):
                if o.get("mode") in ("cutaway", "insert") and o["o0"] - EPS < at < o["o1"] - EPS:
                    raise PlaceError(f"the insert point {at:g}s is inside B-roll {o['id']} ({o['o0']:g}-{o['o1']:g}s)")
    if mode == "insert":
        if cfg["placement"]["warn_inside_word"]:
            word = _word_at(ep, doc, at)
            if word:
                warnings.append(f"the insert point {at:g}s falls inside the spoken word {word!r}: the dialogue is cut there; move it to a pause")
        if (doc.get("captions") or {}).get("srt") or (doc.get("captions") or {}).get("ass"):
            warnings.append("an insert moves everything after it: the captions on this edit are dropped and must be regenerated (ave captions)")
        if doc.get("sequences"):
            warnings.append("this edit carries opening/closing sequences whose positions are not shifted by an insert: rebuild them")

    pid = _next_id(doc, "b")
    ac = cfg["audio"]
    placement = {"id": pid, "file": os.path.relpath(fpath.resolve(), ep.root.resolve()).replace(os.sep, "/"), "o0": _r(at), "o1": _r(at + dur),
                 "x": x, "y": y, "w": w, "h": h, "layer": lay, "kind": "broll", "alpha": False, "mode": mode, "fit": fitv,
                 "src_in": _r(src_in) if is_video else 0.0, "audio": audio,
                 "library": {"studio": studio_ref(ep, sp), "asset": asset, "sha256": rec["sha256"], "kind": rec["kind"]},
                 "attribution": {"required": bool(att.get("required")), "text": att.get("text")},
                 "ai_generated": bool(rec.get("ai_generated")), "reason": reason or "", "by": by or _by()}
    if audio == "keep":
        placement.update(gain_db=float(ac["gain_db"] if gain_db is None else gain_db), duck_db=float(ac["duck_db"] if duck_db is None else duck_db),
                         fade_in_s=float(ac["fade_s"]), fade_out_s=float(ac["fade_s"]))
    if kb:
        placement["ken_burns"] = kb

    credit_spec = None
    want_credit = credit == "always" or (credit == "auto" and bool(att.get("required")))
    if want_credit:
        text = (att.get("text") or "").strip()
        if not text:
            raise PlaceError(f"asset {asset} has no attribution text on record, so there is nothing to show as a credit (--credit never to skip)")
        c0 = _r(at)
        c1 = _r(min(at + max(dur, float(cfg["credit"]["min_s"])), doc["duration"] + (dur if mode == "insert" else 0.0)))
        credit_spec = {"text": text, "o0": c0, "o1": c1, "layer": int(cfg["credit"]["layer"])}
    pacing = _pacing(doc, placement, dur if mode == "insert" else 0.0, beats)
    if strict_pacing and not pacing["ok"]:
        raise PlaceError("pacing rules fail (--strict-pacing): " + "; ".join(v["why"] for v in pacing["violations"]))
    if kb is None and not is_video and dur > 6:
        warnings.append(f"a still held for {dur:g}s with no movement reads as a slide: consider --ken-burns")

    body = {"v": 1, "kind": "broll.proposal", "target": {"stage": stage, "name": name, "base_version": base_version,
                                                         "base_sha256": _sha(base_path), "base_duration": doc["duration"]},
            "placement": placement, "credit": credit_spec, "asset": {"id": asset, "kind": rec["kind"], "duration": rec.get("duration"),
                                                                   "w": rec.get("w"), "h": rec.get("h"), "license": (rec.get("license") or {}).get("summary")},
            "checks": {"license": {"ok": True, "covered": [c["asset"] for c in cov["covered"]], "attributions": cov["attributions"]},
                       "pacing": {k: pacing[k] for k in ("ok", "violations", "coverage", "broll_count")}, "warnings": warnings}}
    if not write:return body
    pid_ = "bp_" + hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:10]
    proposal = {**body, "id": pid_, "created": _now(), "by": by or _by(), "reason": reason or "", "studio": str(sp.root), "applied": None,
                "apply": f"ave broll apply {ep.root} {pid_}"}
    d = proposals_dir(ep)
    d.mkdir(parents=True, exist_ok=True)
    write_json(d / f"{pid_}.json", proposal)
    proposal["file"] = str((d / f"{pid_}.json").relative_to(ep.root))
    return proposal


def load_proposal(ep: Episode, proposal_id: str) -> dict:
    p = proposals_dir(ep) / f"{proposal_id}.json"
    if not p.is_file():
        have = sorted(x.stem for x in proposals_dir(ep).glob("bp_*.json")) if proposals_dir(ep).is_dir() else []
        raise PlaceError(f"no proposal {proposal_id!r}" + (f"; have {', '.join(have)}" if have else "; run `ave broll place` first"))
    return read_json(p)


def list_proposals(ep: Episode) -> list[dict]:
    out = []
    for p in sorted(proposals_dir(ep).glob("bp_*.json")) if proposals_dir(ep).is_dir() else []:
        d = read_json(p)
        pl = d["placement"]
        out.append({"id": d["id"], "applied": d.get("applied"), "stage": d["target"]["stage"], "name": d["target"]["name"], "mode": pl["mode"],
                    "asset": pl["library"]["asset"], "o0": pl["o0"], "o1": pl["o1"], "by": d.get("by"), "created": d.get("created")})
    return out


# ---- applying -----------------------------------------------------------------------------------------------------------

def _e_at(segs: list[dict], at: float) -> tuple[dict, float]:
    """The segment covering output time `at` (the last one when `at` is the very end) and the episode time there."""
    ordered = sorted(segs, key=lambda s: s["o0"])
    for s in ordered:
        if s["o0"] - EPS <= at < s["o1"] - EPS:
            return s, s["e0"] + (at - s["o0"])
    s = ordered[-1]
    return s, s["e0"] + (s["o1"] - s["o0"])


def _split_shift(segs: list[dict], at: float, dur: float, id_for) -> list[dict]:
    """Segments with everything at or after `at` pushed `dur` later; the one `at` falls inside is split in two."""
    out: list[dict] = []
    for s in sorted(segs, key=lambda x: x["o0"]):
        if s["o1"] <= at + EPS:
            out.append(s)
        elif s["o0"] >= at - EPS:
            n = dict(s)
            n["o0"], n["o1"] = _r(s["o0"] + dur), _r(s["o1"] + dur)
            out.append(n)
        else:
            left = dict(s)
            left["o1"] = _r(at)
            right = dict(s)
            right["id"] = id_for(s["id"])
            right["o0"], right["o1"] = _r(at + dur), _r(s["o1"] + dur)
            if "e0" in s:
                right["e0"] = _r(s["e0"] + (at - s["o0"]))
            right["xfade_ms"] = 0
            out += [left, right]
    return out


def ripple_insert(doc: dict, at: float, dur: float, tag: str) -> dict:
    """Make the cut `dur` longer at output time `at`. Video: a hold of the covering shot (hidden under the B-roll). Dialogue: a `fill: silence`
    segment (exact zeros). Music and overlays that start after `at` move; ones that span it stretch. Captions are dropped (they must be
    regenerated). Returns what changed, for the history entry."""
    tr = doc["tracks"]
    used = {s.get("id") for n in ("video", "dialogue", "music", "overlays") for s in tr.get(n) or []}

    def fresh(base: str) -> str:
        n = 1
        while f"{base}{n}" in used:
            n += 1
        used.add(f"{base}{n}")
        return f"{base}{n}"

    vseg, v_e = _e_at(tr["video"], at)
    dseg, d_e = _e_at(tr["dialogue"], at)
    video = _split_shift(tr["video"], at, dur, lambda i: fresh(i + "r"))
    dialogue = _split_shift(tr["dialogue"], at, dur, lambda i: fresh(i + "r"))
    gv = {k: v for k, v in vseg.items() if k in ("src", "crop", "zoom")}
    gv.update(id=fresh(f"{tag}v"), o0=_r(at), o1=_r(at + dur), e0=_r(v_e), reason="B-roll insert: this shot is held under the B-roll")
    gd = {"id": fresh(f"{tag}d"), "src": dseg["src"], "o0": _r(at), "o1": _r(at + dur), "e0": _r(d_e), "fill": "silence", "xfade_ms": 0,
          "gain_db": 0.0, "confidence": 1.0, "reason": "B-roll insert: the dialogue holds still (exact silence) while the B-roll plays"}
    tr["video"] = sorted(video + [gv], key=lambda s: s["o0"])
    tr["dialogue"] = sorted(dialogue + [gd], key=lambda s: s["o0"])
    moved = {}
    for name in ("music", "overlays"):
        for s in tr.get(name) or []:
            if s["o0"] >= at - EPS:
                s["o0"], s["o1"] = _r(s["o0"] + dur), _r(s["o1"] + dur)
                moved.setdefault(name, []).append(s["id"])
            elif s["o1"] > at + EPS:
                s["o1"] = _r(s["o1"] + dur)
                moved.setdefault(name + "_stretched", []).append(s["id"])
    doc["duration"] = _r(doc["duration"] + dur)
    tr["video"][-1]["o1"] = doc["duration"]
    tr["dialogue"][-1]["o1"] = doc["duration"]
    dropped = bool((doc.get("captions") or {}).get("srt") or (doc.get("captions") or {}).get("ass"))
    doc["captions"] = {"srt": None, "ass": None, "burn": False}
    return {"at": _r(at), "dur": _r(dur), "video_hold": gv["id"], "dialogue_gap": gd["id"], "moved": moved, "captions_dropped": dropped}


def _history(doc: dict) -> list[dict]:
    return doc.setdefault("history", [])


def append_placement(ep: Episode, doc: dict, pl: dict, credit_spec: dict | None, cfg: dict) -> tuple[dict, dict | None]:
    """Apply a checked placement to an explicit in-memory EditDoc."""
    pl=copy.deepcopy(pl);credit=None
    if credit_spec:
        cs=credit_spec;rel,cw,ch=render_credit(ep,cs['text'],(doc['canvas']['w'],doc['canvas']['h']),cfg)
        portrait=doc['canvas']['h']>doc['canvas']['w'];pos=cfg['credit']['position_portrait' if portrait else 'position_landscape']
        W,H=doc['canvas']['w'],doc['canvas']['h'];cx=max(0,min(int(W*float(pos['x'])),W-cw-int(.02*W)));cy=max(0,min(int(H*float(pos['y'])),H-ch-int(.04*H)))
        credit={'id':pl['id']+'c','file':rel,'o0':cs['o0'],'o1':min(cs['o1'],doc['duration']),'x':cx,'y':cy,'w':cw,'h':ch,
                'layer':cs['layer'],'kind':'credit','alpha':True,'credit_for':pl['id'],'text':cs['text']}
        pl['credit_id']=credit['id']
    doc['tracks'].setdefault('overlays',[]).append(pl)
    if credit:doc['tracks']['overlays'].append(credit)
    return pl,credit


def apply_proposal(ep: Episode, proposal: dict | str, *, by: str | None = None, reason: str | None = None) -> dict:
    """Apply a proposal as a NEW version of its edit. Refuses when the edit moved on since the proposal, or the asset's license no longer holds."""
    if isinstance(proposal, str):
        proposal = load_proposal(ep, proposal)
    if proposal.get("applied"):
        raise PlaceError(f"proposal {proposal['id']} was already applied (version {proposal['applied']['version']}); `ave broll undo` reverses it")
    t = proposal["target"]
    stage, name = t["stage"], t.get("name")
    vers = editdoc.list_versions(ep, stage, name)
    if not vers:
        raise PlaceError(f"the {stage} edit this proposal targets no longer exists")
    cur_path = editdoc.latest_path(ep, stage, name)
    if vers[-1] != t["base_version"] or _sha(cur_path) != t["base_sha256"]:
        raise PlaceError(f"the {stage} edit changed since proposal {proposal['id']} (it was made against v{t['base_version']}, the latest is v{vers[-1]}): "
                         "run `ave broll place` again so the placement is checked against the current cut")
    doc = editdoc.load(cur_path)
    pl = copy.deepcopy(proposal["placement"])
    used = {s.get("id") for n in ("video", "dialogue", "music", "overlays") for s in doc["tracks"].get(n) or []}
    if pl["id"] in used:
        pl["id"] = _next_id(doc, "b")
    c = closure(ep, {"tracks": {"overlays": [pl]}})
    if not c["ok"]:
        raise PlaceError(blocked_message(c))
    cfg = load_config(ep)
    who = by or proposal.get("by") or _by()
    entry = {"op": "broll.place", "at": _now(), "by": who, "reason": reason if reason is not None else proposal.get("reason", ""),
             "ids": [pl["id"]], "asset": pl["library"]["asset"], "mode": pl["mode"], "proposal": proposal["id"],
             "from": {"stage": stage, "name": name, "version": vers[-1]}, "params": {"o0": pl["o0"], "o1": pl["o1"], "audio": pl["audio"]},
             "inverse": f"ave broll undo {ep.root} --stage {stage}" + (f" --name {name}" if name else "")}
    if pl["mode"] == "insert":
        entry["ripple"] = ripple_insert(doc, pl["o0"], pl["o1"] - pl["o0"], "bi")
    pl,credit=append_placement(ep,doc,pl,proposal.get('credit'),cfg)
    if credit:entry['ids'].append(credit['id'])
    entry["result_version"] = vers[-1] + 1
    _history(doc).append(entry)
    path = editdoc.save_new_version(ep, doc, name)
    proposal["applied"] = {"version": vers[-1] + 1, "path": str(path.relative_to(ep.root)), "at": entry["at"], "by": who}
    write_json(proposals_dir(ep) / f"{proposal['id']}.json", proposal)
    return {"saved": str(path), "version": vers[-1] + 1, "stage": stage, "name": name, "placement": pl, "credit": credit, "history": entry,
            "warnings": proposal["checks"]["warnings"], "pacing": proposal["checks"]["pacing"]}


def place(ep: Episode, *, apply: bool = False, **kw) -> dict:
    """`propose`, and with `apply=True` apply it too. Without it, the edit is untouched and the proposal says how to apply it."""
    p = propose(ep, **kw)
    if not apply:
        return {"applied": False, "proposal": p, "note": "suggest-only: nothing in the edit changed. Review the proposal, then "
                f"`ave broll apply {ep.root} {p['id']}` (or run `ave broll place ... --apply`)."}
    res = apply_proposal(ep, p, by=kw.get("by"), reason=kw.get("reason"))
    return {"applied": True, "proposal": p["id"], **res}


# ---- remove / undo / list / check ---------------------------------------------------------------------------------------------

def _latest(ep: Episode, stage: str | None, name: str | None) -> tuple[str, dict, int]:
    stage = _stage_for(ep, stage, name)
    vers = editdoc.list_versions(ep, stage, name)
    if not vers:
        raise PlaceError(f"No {stage} edit{' ' + name if name else ''} yet for {ep.root.name}.")
    return stage, editdoc.load(editdoc.latest_path(ep, stage, name)), vers[-1]


def remove(ep: Episode, placement_id: str, *, stage: str | None = None, name: str | None = None, by: str | None = None, reason: str = "") -> dict:
    """Drop one placement (and its credit) in a new version. An insert changed the timing of everything after it, so only `undo` reverses it."""
    stage, doc, ver = _latest(ep, stage, name)
    ovs = doc["tracks"].get("overlays") or []
    pl = next((o for o in ovs if o.get("id") == placement_id and o.get("kind") == "broll"), None)
    if pl is None:
        have = [o["id"] for o in ovs if o.get("kind") == "broll"]
        raise PlaceError(f"no B-roll placement {placement_id!r} in {stage}.v{ver}" + (f" (have {', '.join(have)})" if have else ""))
    if pl.get("mode") == "insert":
        raise PlaceError(f"{placement_id} is an insert: it moved everything after it, so removing it by hand would leave a gap. "
                         f"If it is the latest B-roll step, `ave broll undo {ep.root} --stage {stage}` restores the cut before it.")
    gone = [pl["id"]] + [o["id"] for o in ovs if o.get("credit_for") == pl["id"]]
    doc["tracks"]["overlays"] = [o for o in ovs if o["id"] not in gone]
    entry = {"op": "broll.remove", "at": _now(), "by": by or _by(), "reason": reason, "ids": gone, "asset": (pl.get("library") or {}).get("asset"),
             "from": {"stage": stage, "name": name, "version": ver}, "result_version": ver + 1,
             "inverse": f"ave broll undo {ep.root} --stage {stage}" + (f" --name {name}" if name else "")}
    _history(doc).append(entry)
    path = editdoc.save_new_version(ep, doc, name)
    return {"saved": str(path), "version": ver + 1, "removed": gone, "history": entry}


def undo(ep: Episode, *, stage: str | None = None, name: str | None = None, by: str | None = None) -> dict:
    """Restore the edit as it was BEFORE the latest B-roll step, as a new version. Refuses when something else was saved since (undoing then would
    throw that work away): the history entry records the version it produced, and the latest must be that one."""
    stage, doc, ver = _latest(ep, stage, name)
    hist = doc.get("history") or []
    last = hist[-1] if hist else None
    if not last or last.get("op") not in OPS[:2]:
        raise PlaceError(f"the latest {stage} edit (v{ver}) does not end in a B-roll step, so there is nothing for `ave broll undo` to reverse")
    if last.get("result_version") != ver:
        raise PlaceError(f"the latest {stage} edit (v{ver}) was saved after the B-roll step (which made v{last.get('result_version')}); "
                         "use `ave broll remove` for one placement instead")
    frm = last["from"]
    prev_path = editdoc.path_for(ep, frm["stage"], frm["version"], frm.get("name"))
    if not prev_path.is_file():
        raise PlaceError(f"the version this step started from ({prev_path.name}) is gone")
    prev = editdoc.load(prev_path)
    entry = {"op": "broll.undo", "at": _now(), "by": by or _by(), "undid": last["op"], "ids": last.get("ids"), "asset": last.get("asset"),
             "from": {"stage": stage, "name": name, "version": ver}, "restores": f"v{frm['version']}", "result_version": ver + 1}
    _history(prev).append(entry)
    path = editdoc.save_new_version(ep, prev, name)
    return {"saved": str(path), "version": ver + 1, "restored_from": f"v{frm['version']}", "undid": last["op"], "ids": last.get("ids"), "history": entry}


def listing(ep: Episode, *, stage: str | None = None, name: str | None = None) -> dict:
    stage, doc, ver = _latest(ep, stage, name)
    ovs = [{k: o.get(k) for k in ("id", "mode", "o0", "o1", "audio", "fit", "src_in", "ken_burns", "credit_id")} | {"asset": (o.get("library") or {}).get("asset"),
                                                                                                                  "file": o["file"]}
           for o in placements(doc)]
    return {"edit": editdoc.latest_path(ep, stage, name).name[:-len(".json")], "placements": ovs, "history": doc.get("history") or [],
            "closure": closure(ep, doc)}


def check(ep: Episode, *, stage: str | None = None, name: str | None = None, beats: list[dict] | None = None) -> dict:
    """License closure and pacing of the B-roll in the latest edit. `ok` is false on an unlicensed asset; pacing violations are advice."""
    from ..assets import critic
    stage, doc, ver = _latest(ep, stage, name)
    c = closure(ep, doc)
    ov = [{"id": o["id"], "o0": o["o0"], "o1": o["o1"]} for o in placements(doc)]
    pacing = critic.check_placements(ov, doc["duration"], beats or [], critic.load_config(), music=doc["tracks"].get("music") or [])
    return {"edit": editdoc.latest_path(ep, stage, name).name[:-len(".json")], "ok": c["ok"], "license": c, "pacing": pacing,
            "credits": c["attributions"], "courtesy_credits": c["optional_attributions"], "derived_from": c["derived_from"],
            "note": None if beats else "pass --beats FILE to check emotional beats; without it that rule is not checked"}
