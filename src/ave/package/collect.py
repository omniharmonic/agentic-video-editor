"""Find what an episode already has, for the release package. Read-only: nothing here writes, renders or hashes a large file.

Every candidate is an *entry*: `{id, role, src, dest, draft, draft_reasons, approval, source, ...}`. An entry is a DRAFT when the
human has not approved it (a clip still `proposed`, a content item in review) or when it fails a delivery rule (thumbnail not
1280x720 under 2 MB, a short that missed its platform's limits). Drafts go to `drafts/`, never to the main package.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from .. import editdoc, resources
from ..episode import Episode, read_json

DEFAULT_POLICY = resources.config_file("package.default.yaml")
_FINAL = re.compile(r"^final(?:\.(?P<name>[A-Za-z0-9_-]+))?\.v(?P<n>\d+)\.(?P<preset>[A-Za-z0-9_-]+)\.mp4$")
_SHORT = re.compile(r"^(?P<slug>.+)\.v(?P<v>\d+)\.(?P<plat>[a-z]+)$")
IMAGE_EXTS = {".png", ".jpg", ".jpeg"}


def _merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in (b or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_policy(ep: Episode | None = None) -> dict:
    pol = yaml.safe_load(DEFAULT_POLICY.read_text())
    if ep is not None:
        p = ep.root / "policy" / "package.yaml"
        if p.is_file():
            pol = _merge(pol, yaml.safe_load(p.read_text()) or {})
    return pol


def episode_meta(ep: Episode) -> dict:
    try:
        return read_json(ep.meta_path)
    except (OSError, ValueError):
        return {}


def entry(**kw) -> dict:
    e = {"id": None, "role": None, "src": None, "dest": None, "draft": False, "draft_reasons": [], "approval": {"status": "n/a"},
         "source": {}, "license": {}, "alt_text": None, "platforms": [], "problems": [], "extra": {}}
    e.update(kw)
    return e


def _approval(status: str, by=None, at=None) -> dict:
    return {"status": status, "by": by, "at": at}


# ---- the final cut -----------------------------------------------------------------------------

def find_final(ep: Episode) -> dict | None:
    """The latest final render: highest version of the unnamed final (a multi-session episode's combined one), else the
    latest of each named session's final (all returned, the first is `primary`)."""
    if not ep.exports.is_dir():
        return None
    found: dict[str | None, list[tuple[int, int, float, Path]]] = {}
    for f in ep.exports.iterdir():
        m = _FINAL.match(f.name)
        if f.name.startswith(".") or not m or not f.is_file():
            continue
        rank = 0 if m["preset"] == "master" else (2 if "proxy" in m["preset"] else 1)
        found.setdefault(m["name"], []).append((int(m["n"]), -rank, f.stat().st_size, f))
    if not found:
        return None
    best = {}
    for name, cands in found.items():
        n, _, _, f = max(cands)
        best[name] = {"name": name, "version": n, "path": f, "preset": _FINAL.match(f.name)["preset"]}
    primary = best.get(None) or best[sorted(k for k in best if k)[0]]
    return {"primary": primary, "all": best}


def final_edits(ep: Episode) -> dict[str | None, int]:
    """Latest final EditDoc version per session name (None = unnamed): an edit can exist long before its render does."""
    out: dict[str | None, int] = {}
    if not ep.edits.is_dir():
        return out
    for f in ep.edits.iterdir():
        m = editdoc._V.match(f.name)
        if m and m["stage"] == "final" and not f.name.startswith("."):
            out[m["name"]] = max(out.get(m["name"], 0), int(m["n"]))
    return out


def final_doc(ep: Episode, fin: dict) -> dict | None:
    try:
        p = editdoc.path_for(ep, "final", fin["version"], fin["name"])
        if p.is_file():
            return editdoc.load(p)
        return editdoc.load_latest(ep, "final", fin["name"])
    except (OSError, ValueError, SystemExit):
        return None


def find_srt(ep: Episode, fin: dict, doc: dict | None) -> Path | None:
    tag = f"final.{fin['name']}" if fin["name"] else "final"
    exact = ep.exports / f"{tag}.v{fin['version']}.srt"
    if exact.is_file():
        return exact
    if doc and (doc.get("captions") or {}).get("srt"):
        p = ep.root / doc["captions"]["srt"]
        if p.is_file():
            return p
    p = ep.captions / f"{tag}.srt"
    return p if p.is_file() else None


def credits_for(ep: Episode, doc: dict | None) -> dict:
    """Music/B-roll credit lines for the final, and any asset without a license-log entry."""
    if not doc:
        return {"lines": [], "missing": [], "assets": 0}
    try:
        from .. import shorts
        from ..assets import licenses
        files = shorts.licensed_files(doc)
        if not files:
            return {"lines": [], "missing": [], "assets": 0}
        cov = licenses.coverage(ep, files)
        entries = [c["entry"] for c in cov["covered"] if c.get("entry")]
        return {"lines": licenses.attribution_lines(entries, include_optional=True),
                "missing": [m["file"] for m in cov["missing"]] + list(cov["unreadable"]), "assets": len(files)}
    except Exception as e:      # a damaged license log must not stop the package; it is reported
        return {"lines": [], "missing": [], "assets": 0, "error": f"{type(e).__name__}: {e}"}


# ---- clips and shorts --------------------------------------------------------------------------

def load_clips(ep: Episode) -> dict[str, dict]:
    p = ep.work / "clips.json"
    if not p.is_file():
        return {}
    try:
        return {c["id"]: c for c in read_json(p).get("clips", [])}
    except (OSError, ValueError, KeyError):
        return {}


def _decisions(ep: Episode) -> dict:
    try:
        from .. import learning
        return dict(learning.latest_decisions(ep))
    except Exception:
        return {}


def find_shorts(ep: Episode) -> list[dict]:
    """The LATEST export per (clip, platform), from the sidecars `ave shorts export` wrote. Approved clips whose export passed its
    platform limits, encode and license checks and whose bytes still match the sidecar go in the package; the rest are drafts."""
    d = ep.exports / "shorts"
    if not d.is_dir():
        return []
    clips = load_clips(ep)
    by_slug = {c.get("slug"): c for c in clips.values()}
    decisions = _decisions(ep)
    best: dict[tuple[str, str], tuple[int, Path, dict]] = {}
    for side in sorted(d.glob("*.json")):
        m = _SHORT.match(side.stem)
        if side.name.startswith(".") or not m:
            continue
        try:
            sc = read_json(side)
        except (OSError, ValueError):
            continue
        key = (m["slug"], m["plat"])
        if key not in best or int(m["v"]) > best[key][0]:
            best[key] = (int(m["v"]), side, sc)
    out = []
    for (slug, plat), (ver, side, sc) in sorted(best.items()):
        mp4 = ep.root / sc.get("video", "")
        if not mp4.is_file():
            continue
        clip = clips.get((sc.get("clip") or {}).get("id")) or by_slug.get(slug)
        reasons = []
        if clip is None:
            reasons.append("no clip in work/clips.json matches this export")
            status = "unknown"
        else:
            status = clip.get("status", "proposed")
            if status != "approved":
                reasons.append(f"clip is {status}, not approved (decide it in `ave clips review`)")
        lim = sc.get("limits") or {}
        if not lim.get("ok", False):
            reasons.append("platform limits not met: " + "; ".join(lim.get("problems", [])) if lim.get("problems") else "platform limits not checked")
        if (sc.get("encode") or {}).get("problems"):
            reasons.append("encode problems: " + "; ".join(sc["encode"]["problems"]))
        if (sc.get("licenses") or {}).get("ok") is False:
            reasons.append("license coverage incomplete")
        dec = decisions.get((clip or {}).get("id") or "", {})
        out.append(entry(id=f"{slug}.{plat}", role="short", src=mp4, dest=f"shorts/{plat}/{mp4.name}", draft=bool(reasons), draft_reasons=reasons,
                         approval=_approval(status, dec.get("by"), dec.get("at")),
                         source={"stage": "shorts", "version": ver, "file": ep.rel(mp4), "sidecar": ep.rel(side), "sha256": sc.get("sha256")},
                         license={"credits": (sc.get("licenses") or {}).get("attribution", [])}, platforms=[plat],
                         extra={"clip": clip, "sidecar": sc, "slug": slug, "export_platform": plat,
                                "duration": sc.get("duration"), "width": sc.get("width"), "height": sc.get("height")}))
    return out


# ---- content: thumbnails, quote cards, carousels ----------------------------------------------

def _image_size(p: Path) -> tuple[int, int] | None:
    try:
        from PIL import Image
        with Image.open(p) as im:
            return im.size
    except Exception:
        return None


def thumbnail_problems(p: Path, policy: dict) -> list[str]:
    t = policy["thumbnails"]
    sz = _image_size(p)
    out = []
    if sz is None:
        return [f"{p.name} cannot be read as an image"]
    if sz != (t["width"], t["height"]):
        out.append(f"{p.name} is {sz[0]}x{sz[1]}, a YouTube thumbnail is {t['width']}x{t['height']}")
    if p.suffix.lower() not in IMAGE_EXTS:
        out.append(f"{p.name} is not a png or jpeg")
    if p.stat().st_size > t["max_bytes"]:
        out.append(f"{p.name} is {p.stat().st_size} bytes, YouTube takes under {t['max_bytes']}")
    return out


def find_content(ep: Episode, policy: dict) -> dict:
    """{thumbnails: [entry], images: [entry], quotes: [{text, speaker, source}], captions: {item_id: caption}, version}"""
    from ..content import manifest as M
    out = {"thumbnails": [], "images": [], "quotes": [], "items": {}, "version": None}
    if not M.list_versions(ep):
        return out
    from ..publish import safety
    doc = M.load_latest(ep)
    out["version"] = M.list_versions(ep)[-1]
    meta_names = {k: (v.get("name") if isinstance(v, dict) else None) for k, v in (episode_meta(ep).get("speakers") or {}).items()}
    for item in doc.get("items", []):
        rv = item.get("review") or {}
        status = rv.get("status", "draft")
        if status == "rejected":
            continue
        reasons = [] if status == "approved" else [f"content item is {status}, not approved (decide it in `ave content review`)"]
        outs = [o for o in item.get("outputs", []) if Path(o.get("file", "")).suffix.lower() in IMAGE_EXTS]
        if not outs:
            continue
        probs = []
        for o in outs:
            f = ep.root / o["file"]
            if not f.is_file():
                probs.append(f"{o['file']} is missing")
            elif safety.sha256_file(f) != o.get("sha256"):
                probs.append(f"{o['file']} changed since it was reviewed; re-render and approve again")
            if not str(o.get("alt_text") or "").strip():
                probs.append(f"{o['file']} has no alt text")
        reasons += probs
        base = dict(approval=_approval(status, rv.get("by"), rv.get("at")), license=item.get("license") or {},
                    source={"stage": "content", "version": out["version"], "item": item["id"], "template": item["template"]})
        if item["template"] == "thumbnail":
            for o in outs:
                f = ep.root / o["file"]
                tp = thumbnail_problems(f, policy) if f.is_file() else []
                convert = bool(tp) and all("YouTube takes under" in m for m in tp)      # right size, too heavy: ship a JPEG instead
                if convert:
                    tp = []
                out["thumbnails"].append(entry(id=f"{item['id']}", role="thumbnail", src=f, dest=f"thumbnails/{f.stem}.jpg" if convert else f"thumbnails/{f.name}", draft=bool(reasons or tp),
                                               draft_reasons=reasons + tp, alt_text=o.get("alt_text"), platforms=["youtube"],
                                               extra={"item": item, "width": (_image_size(f) or (None, None))[0], "height": (_image_size(f) or (None, None))[1],
                                                      "convert_jpeg": convert},
                                               **{**base, "source": {**base["source"], "file": o["file"]}}))
            continue
        quote = None
        slots = item.get("slots") or {}
        if item["template"] == "quote-post" and slots.get("quote") and status == "approved" and not probs:
            quote = {"text": slots["quote"], "speaker": slots.get("attribution"), "source": f"content:{item['id']}"}
            out["quotes"].append(quote)
        folder = item["id"]
        if item["template"] == "carousel":
            outs = sorted(outs, key=lambda o: o.get("slide", 0))
        for n, o in enumerate(outs, 1):
            f = ep.root / o["file"]
            dest = f"images/{folder}/{'slide-%02d' % n if item['template'] == 'carousel' else f.stem}{f.suffix}"
            out["images"].append(entry(id=f"{item['id']}" + (f"#{n}" if item["template"] == "carousel" else f"@{o.get('platform', '')}"),
                                       role="carousel_slide" if item["template"] == "carousel" else "quote_card", src=f, dest=dest,
                                       draft=bool(reasons), draft_reasons=reasons, alt_text=o.get("alt_text"),
                                       platforms=[o["platform"]] if o.get("platform") else list(item.get("platforms") or []),
                                       extra={"item": item, "slide": n if item["template"] == "carousel" else None,
                                              "width": o.get("w"), "height": o.get("h"), "quote": quote},
                                       **{**base, "source": {**base["source"], "file": o["file"]}}))
        out["items"][item["id"]] = item
    return out


def clip_quotes(clips: dict[str, dict], meta: dict) -> list[dict]:
    """Verbatim hook quotes of APPROVED clips (the transcript says who spoke)."""
    names = {k: (v.get("name") if isinstance(v, dict) else None) for k, v in (meta.get("speakers") or {}).items()}
    out = []
    for c in clips.values():
        if c.get("status") == "approved" and c.get("hook_quote"):
            sp = [names.get(s) for s in c.get("speakers", []) if names.get(s)]
            out.append({"text": c["hook_quote"], "speaker": sp[0] if len(sp) == 1 else None, "source": f"clip:{c['id']}"})
    return out
