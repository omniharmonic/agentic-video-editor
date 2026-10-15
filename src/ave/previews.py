"""Clip previews: a small review video per proposed clip, so the human can PLAY IT IN CONTEXT before deciding.

Contract (docs/superpowers/specs/2026-10-14-clip-learning-and-ui-design.md, "Round 2"): `<work>/clips/previews/<clip_id>.mp4`
(540p H.264 + AAC, faststart) and `index.json` = {clip_id: {file, tight_file|null, pad_before_s, pad_after_s, clip_in_s, clip_out_s,
edit, rendered_at, sha}}. `clip_in_s/clip_out_s` are offsets INSIDE the preview where the real clip begins and ends.

How: the clip's range of its own source edit (rough.<name> vN, pinned in the clip's `source`) plus `pad` seconds each side (clamped
to the edit) is cut out of that EditDoc with `editdoc.slice_doc` and rendered by the same compiler as every other render
(`ave.compile.render`, preset `preview`): the edit's real picture, the dialogue master with the cleanup applied, light loudness
normalisation, and the compiler's own verification. Only the windows needed are decoded (input seeking, scale right after
decode, VideoToolbox for big HEVC/H.264). If every picture source of the edit has a verified review proxy (`ave proxies`), the
proxy is decoded instead (crops are rescaled to the proxy's pixels); audio always comes from the real dialogue sources.

If a clip carries `tighten` ranges (episode seconds, suggestions from `ave clips propose`), `<clip_id>.tight.mp4` is ALSO rendered:
only the clip, those ranges removed, a short equal-power audio crossfade at each join, and where the picture would jump on the same
camera at a join, the other camera covers the join for a moment (same instant, so picture stays in step with the audio).

Resumable: `sha` = hash of everything that decides the pixels and samples (edit bytes, range, pad, height, tighten); a clip whose
entry matches and whose files exist is skipped. Nothing is written outside `<work>/clips/previews` (and the compiler's temporary
`<work>/render/` folder, removed after each render); footage is only ever read.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from . import clips as clipmod
from . import compile as comp
from . import editdoc, ffutil, presets
from .episode import Episode, read_json

DEFAULT_PAD_S = 8.0
DEFAULT_HEIGHT = 540
WORKERS = 3
ALGO = 1                              # bump when the rendering recipe changes so old previews are re-rendered
XFADE_MS = 80
COVER_S = 0.6                         # how long the other camera covers a jump cut at a join
MIN_KEEP_S = 0.25                     # a kept piece shorter than this between two removals is dropped
# Planning numbers only (replaced by measurements on real footage; see docs/field-notes/2026-10-14-clip-previews.md)
EST_RENDER_PER_MEDIA_S = 0.6          # wall seconds per preview second for ONE worker (real 4K HEVC run: 0.2 with 3 workers)
EST_BYTES_PER_S = 50_000              # real run: about 40 kB per preview second (CRF 27 at 540p plus AAC 96k)
SPACE_FACTOR = 3.0
SPACE_MIN_BYTES = 300_000_000
_LOCK = threading.Lock()


class PreviewError(RuntimeError):
    pass


def previews_dir(ep: Episode) -> Path:
    return ep.work / "clips" / "previews"


def index_path(ep: Episode) -> Path:
    return previews_dir(ep) / "index.json"


def load_index(ep: Episode) -> dict:
    p = index_path(ep)
    if not p.is_file():
        return {}
    try:
        data = read_json(p)
    except ValueError as e:
        raise SystemExit(f"{p} is not valid JSON ({e}); delete it to re-index (the mp4s stay).")
    return data if isinstance(data, dict) else {}


def _save_index(ep: Episode, idx: dict) -> None:
    p = index_path(ep)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".partial")
    tmp.write_text(json.dumps(idx, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    tmp.replace(p)


# ---- planning ----------------------------------------------------------------------------------------------------

def _edit_ref(src: dict) -> str:
    return f"{src['stage']}{'.' + src['name'] if src.get('name') else ''}.v{src['version']}"


def _tighten_output_ranges(doc: dict, clip: dict, rows: list[dict]) -> list[tuple[float, float]]:
    """The clip's `tighten` ranges (episode seconds) as merged OUTPUT-time ranges of the edit, clamped to the clip."""
    segs = sorted(doc["tracks"]["dialogue"], key=lambda s: s["o0"])

    def to_out(e: float) -> float:
        best, dist = None, None
        for s in segs:
            e1 = s["e0"] + (s["o1"] - s["o0"])
            if s["e0"] - 1e-6 <= e <= e1 + 1e-6:
                return s["o0"] + (e - s["e0"])
            d = min(abs(e - s["e0"]), abs(e - e1))
            if dist is None or d < dist:
                best, dist = (s["o0"] if abs(e - s["e0"]) < abs(e - e1) else s["o1"]), d
        if best is None:
            raise PreviewError(f"{clip['id']}: the edit has no dialogue to place tighten ranges on")
        return best

    out = []
    for r in rows:
        a, b = to_out(float(r["start"])), to_out(float(r["end"]))
        a, b = max(a, clip["start"]), min(b, clip["end"])
        if b - a > 0.02:
            out.append((a, b))
    out.sort()
    merged: list[list[float]] = []
    for a, b in out:
        if merged and a <= merged[-1][1] + 0.02:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def _keeps(start: float, end: float, cuts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    keeps, t = [], start
    for a, b in cuts:
        if a - t >= MIN_KEEP_S:
            keeps.append((t, a))
        t = max(t, b)
    if end - t >= MIN_KEEP_S:
        keeps.append((t, end))
    return keeps


def select_clips(ep: Episode, name: str | None, ids: list[str] | None, top: int | None) -> tuple[dict, list[dict]]:
    name = clipmod.resolve_name(ep, name)
    data = clipmod.load_clips(ep)
    if not data or not data.get("clips"):
        raise SystemExit("No clips yet. Run `ave clips propose` first." )
    pool = clipmod.session_clips(data, name)
    if not pool:
        raise SystemExit(f"No clips for session {name}." if name else "No clips.")
    if ids:
        by_id = {c["id"]: c for c in pool}
        missing = [i for i in ids if i not in by_id]
        if missing:
            raise SystemExit(f"Unknown clip id(s) {', '.join(missing)}. Known: {', '.join(sorted(by_id))}")
        chosen = [by_id[i] for i in ids]
    else:
        chosen = [c for c in pool if c.get("status") != "rejected"]
        chosen.sort(key=lambda c: (c.get("rank") or 10**6, -(c.get("total") or 0)))
        if top:
            chosen = chosen[:top]
    return data, chosen


def _source_of(data: dict, clip: dict) -> dict:
    src = clip.get("source") or data.get("source")
    if not src:
        raise SystemExit(f"{clip['id']} records no source edit; re-run `ave clips propose`.")
    return {"stage": src["stage"], "name": src.get("name"), "version": src["version"]}


def _sha(edit_bytes_sha: str, window: dict, clip: dict, cuts: list, pad: float, height: int) -> str:
    key = {"algo": ALGO, "edit": edit_bytes_sha, "a": round(window["a"], 3), "b": round(window["b"], 3), "start": round(clip["start"], 3),
           "end": round(clip["end"], 3), "pad": pad, "height": height, "cuts": [[round(a, 3), round(b, 3)] for a, b in cuts]}
    return hashlib.sha1(json.dumps(key, sort_keys=True).encode()).hexdigest()[:16]


def plan(ep: Episode, name: str | None = None, ids: list[str] | None = None, top: int | None = None, pad: float = DEFAULT_PAD_S,
         height: int = DEFAULT_HEIGHT, force: bool = False) -> dict:
    """What would be rendered, without rendering: per-clip window, sha, state (fresh/stale/missing), estimates."""
    if pad < 0 or pad > 120:
        raise SystemExit("--pad must be between 0 and 120 seconds.")
    if height < 144 or height > 1080 or height % 2:
        raise SystemExit("--height must be an even number between 144 and 1080.")
    data, chosen = select_clips(ep, name, ids, top)
    index = load_index(ep)
    docs: dict[str, tuple[dict, str]] = {}
    items = []
    for c in chosen:
        src = _source_of(data, c)
        key = _edit_ref(src)
        if key not in docs:
            path = editdoc.path_for(ep, src["stage"], src["version"], src["name"])
            if not path.is_file():
                raise SystemExit(f"{c['id']}: its source edit {key} is missing ({path}); cannot preview it.")
            docs[key] = (editdoc.load(path), hashlib.sha1(path.read_bytes()).hexdigest())
        doc, esha = docs[key]
        if not (0 <= c["start"] < c["end"] <= doc["duration"] + editdoc.EPS):
            raise SystemExit(f"{c['id']}: clip range {c['start']}-{c['end']} lies outside {key} (0-{doc['duration']:.2f}).")
        a, b = max(0.0, c["start"] - pad), min(doc["duration"], c["end"] + pad)
        window = {"a": a, "b": b, "pad_before_s": round(c["start"] - a, 3), "pad_after_s": round(b - c["end"], 3),
                  "clip_in_s": round(c["start"] - a, 3), "clip_out_s": round(c["end"] - a, 3)}
        rows = [r for r in (c.get("tighten") or []) if isinstance(r, dict) and "start" in r and "end" in r]
        cuts = _tighten_output_ranges(doc, c, rows) if rows else []
        keeps = _keeps(c["start"], c["end"], cuts) if cuts else []
        tight = bool(cuts) and bool(keeps) and sum(b2 - a2 for a2, b2 in keeps) < (c["end"] - c["start"]) - 0.05
        sha = _sha(esha, window, c, cuts if tight else [], pad, height)
        old = index.get(c["id"]) or {}
        pdir = previews_dir(ep)
        have = bool(old) and old.get("sha") == sha and (pdir / old.get("file", "")).is_file() and \
            (not tight or (old.get("tight_file") and (pdir / old["tight_file"]).is_file()))
        secs = (b - a) + (sum(b2 - a2 for a2, b2 in keeps) if tight else 0.0)
        items.append({"id": c["id"], "clip": c, "source": src, "edit": key, "window": window, "sha": sha, "cuts": cuts if tight else [],
                      "keeps": keeps if tight else [], "tight": tight, "state": "fresh" if have and not force else
                      ("stale" if old else "missing"), "media_s": secs})
    todo = [i for i in items if i["state"] != "fresh"]
    media = sum(i["media_s"] for i in todo)
    est_bytes = int(media * EST_BYTES_PER_S)
    free = shutil.disk_usage(ep.work if ep.work.exists() else ep.root).free
    return {"items": items, "todo": len(todo), "fresh": len(items) - len(todo), "media_s": round(media, 1), "estimate_bytes": est_bytes,
            "estimate_s": round(media * EST_RENDER_PER_MEDIA_S / WORKERS, 0), "free_bytes": free,
            "space_ok": free >= max(SPACE_FACTOR * est_bytes, SPACE_MIN_BYTES) if todo else True, "docs": docs}


# ---- the tight variant -------------------------------------------------------------------------------------------

def tight_doc(doc: dict, keeps: list[tuple[float, float]]) -> tuple[dict, list[float]]:
    """The clip with everything outside `keeps` (output-time ranges of `doc`) removed. Returns (doc, join times in the new doc)."""
    pieces = []
    for n, (a, b) in enumerate(keeps):
        sl = editdoc.slice_doc(doc, a, b, stage="select")
        for k, s in enumerate(sl["tracks"]["dialogue"]):
            s["xfade_ms"] = XFADE_MS if (k == 0 and n > 0) else (s.get("xfade_ms", 0) if k else 0)
        pieces.append((f"p{n}", sl))
    out = clipmod.concat_docs(pieces, 0.0, "tightened preview")
    joins, t = [], 0.0
    for _, sl in pieces[:-1]:
        t += sl["duration"]
        joins.append(round(t, 6))
    return out, joins


def cover_joins(doc: dict, joins: list[float], cover_s: float = COVER_S) -> int:
    """Where the same camera is on both sides of a join, show another camera for `cover_s` from the join (same instant, so it stays in
    step with the audio). Returns how many joins were covered."""
    covered = 0
    video = doc["tracks"]["video"]
    for tj in joins:
        i = next((k for k, s in enumerate(video) if s["o0"] - 1e-6 <= tj < s["o1"] - 1e-6), None)
        if i is None or i == 0:
            continue
        before, seg = video[i - 1], video[i]
        if abs(seg["o0"] - tj) > 1e-3 or before["src"] != seg["src"] or seg["o1"] - tj < cover_s + 0.3:
            continue
        others = [k for k, s in doc["sources"].items() if k != seg["src"] and s.get("kind", "video") == "video"
                  and editdoc.src_time(s, seg["e0"]) >= 0]
        if not others:
            continue
        cut = round(tj + cover_s, 6)
        cov = dict(seg, id=f"{seg['id']}_cv", src=others[0], o1=cut, crop=None, zoom=1.0, reason="covers a jump cut")
        rest = dict(seg, id=f"{seg['id']}_r", o0=cut, e0=seg["e0"] + cover_s)
        video[i:i + 1] = [cov, rest]
        covered += 1
    return covered


# ---- proxies -----------------------------------------------------------------------------------------------------

def use_proxies(ep: Episode, doc: dict) -> tuple[dict, bool, str]:
    """Point the doc's PICTURE at verified review proxies when every picture source has one. Audio keeps the real files.
    Returns (doc, used, why)."""
    from . import proxies
    vsrc = sorted({s["src"] for s in doc["tracks"]["video"]})
    found, missing = proxies.proxy_map(ep, doc)
    paths = {k: (ep.root / doc["sources"][k]["file"]) if not Path(doc["sources"][k]["file"]).is_absolute() else Path(doc["sources"][k]["file"])
             for k in vsrc}
    miss = [k for k in vsrc if str(paths[k].resolve()) not in {str(Path(p).resolve()) for p in found}]
    if miss:
        return doc, False, f"no verified proxy for {', '.join(miss)}"
    out = editdoc.clone(doc)
    used_by_audio = {s["src"] for s in doc["tracks"]["dialogue"]}
    lookup = {str(Path(o).resolve()): p for o, p in found.items()}
    for k in vsrc:
        px = lookup[str(paths[k].resolve())]
        orig, new = ffutil.probe(paths[k]), ffutil.probe(px)
        sx = (new["w"] / orig["w"]) if orig["w"] else 1.0
        sy = (new["h"] / orig["h"]) if orig["h"] else 1.0
        key = k if k not in used_by_audio else f"{k}~px"
        if key != k:
            out["sources"][key] = dict(out["sources"][k])
        out["sources"][key]["file"] = px
        for s in out["tracks"]["video"]:
            if s["src"] == k:
                s["src"] = key
                if s.get("crop"):
                    x, y, w, h = s["crop"]
                    s["crop"] = [x * sx, y * sy, w * sx, h * sy]
    return out, True, "verified proxies"


# ---- rendering ---------------------------------------------------------------------------------------------------

def _verify(path: Path, want_s: float, label: str) -> dict:
    info = ffutil.probe(path)
    frame = 1.0 / (info["fps"] or 30.0)
    bad = []
    if not info["has_video"]:
        bad.append("no video stream")
    if not info["has_audio"]:
        bad.append("no audio stream")
    if abs(info["duration"] - want_s) > frame + 1e-3:
        bad.append(f"duration {info['duration']:.3f}s, expected {want_s:.3f}s (+/- {frame:.3f}s)")
    if info["has_video"] and info["h"] and info["h"] > 1080:
        bad.append(f"picture is {info['w']}x{info['h']}, not a preview size")
    if bad:
        raise PreviewError(f"{label}: preview failed verification: " + "; ".join(bad))
    return info


def _preset(height: int) -> dict:
    pr = dict(presets.get("preview"))
    pr["height"] = height
    return pr


def render_item(ep: Episode, item: dict, doc: dict, height: int, proxy: bool) -> dict:
    """Render one clip's preview (and tight variant). Verifies with ffprobe; raises PreviewError on any shortfall."""
    t0 = time.monotonic()
    pdir = previews_dir(ep)
    pdir.mkdir(parents=True, exist_ok=True)
    c, w = item["clip"], item["window"]
    pr = _preset(height)
    wdoc = editdoc.slice_doc(doc, w["a"], w["b"], stage="select")
    # a slice starts mid-edit: nothing before its first dialogue segment to crossfade with
    if wdoc["tracks"]["dialogue"]:
        wdoc["tracks"]["dialogue"][0]["xfade_ms"] = 0
    if proxy:
        wdoc, _, _ = use_proxies(ep, wdoc)
    out = pdir / f"{item['id']}.mp4"
    warnings: list[str] = []
    try:
        r = comp.render(ep, wdoc, out, preset=pr, burn_captions=False, overwrite=True)
    except Exception as e:
        raise PreviewError(f"{item['id']}: render failed: {e}") from e
    warnings += r.get("warnings", [])
    _verify(out, w["pad_before_s"] + (c["end"] - c["start"]) + w["pad_after_s"], item["id"])
    res = {"file": out.name, "tight_file": None, "bytes": out.stat().st_size, "lufs": r["lufs"], "warnings": warnings,
           "render_s": 0.0, "tight_s": None}
    if item["tight"]:
        tdoc, joins = tight_doc(doc, item["keeps"])
        covered = cover_joins(tdoc, joins)
        if proxy:
            tdoc, _, _ = use_proxies(ep, tdoc)
        tout = pdir / f"{item['id']}.tight.mp4"
        try:
            tr = comp.render(ep, tdoc, tout, preset=pr, burn_captions=False, overwrite=True)
        except Exception as e:
            raise PreviewError(f"{item['id']}: tight render failed: {e}") from e
        _verify(tout, sum(b - a for a, b in item["keeps"]), item["id"] + " (tight)")
        res.update(tight_file=tout.name, tight_s=round(tdoc["duration"], 3), covered=covered, bytes=res["bytes"] + tout.stat().st_size)
        res["warnings"] += tr.get("warnings", [])
    res["render_s"] = round(time.monotonic() - t0, 1)
    return res


def run(ep: Episode, name: str | None = None, ids: list[str] | None = None, top: int | None = None, pad: float = DEFAULT_PAD_S,
        height: int = DEFAULT_HEIGHT, force: bool = False, dry_run: bool = False, workers: int = WORKERS, proxies: bool | None = None,
        log=None, progress_log: bool = True) -> dict:
    """Render the previews. Refuses (SystemExit) when the disk is too full. Returns {rendered, skipped, failed, ...}."""
    from . import progress
    say = log or (lambda m: print(m, flush=True))
    p = plan(ep, name, ids, top, pad, height, force)
    items, docs = p["items"], p.pop("docs")
    todo = [i for i in items if i["state"] != "fresh"]
    say(f"previews: {len(items)} clip(s), {len(todo)} to render ({p['fresh']} fresh), {p['media_s']:.0f}s of preview, about "
        f"{p['estimate_bytes'] / 1e6:.0f} MB and ~{p['estimate_s'] / 60:.1f} min with {workers} workers; free {p['free_bytes'] / 1e9:.0f} GB")
    report = {"name": name, "pad": pad, "height": height, "planned": len(items), "skipped": [i["id"] for i in items if i["state"] == "fresh"],
              "rendered": [], "failed": [], "dry_run": dry_run, "estimate_bytes": p["estimate_bytes"], "estimate_s": p["estimate_s"],
              "free_bytes": p["free_bytes"], "proxies": None}
    if dry_run or not todo:
        report["would_render"] = [i["id"] for i in todo]
        return report
    if not p["space_ok"]:
        raise SystemExit(f"Refusing: {p['free_bytes'] / 1e9:.1f} GB free is below {SPACE_FACTOR:g}x the {p['estimate_bytes'] / 1e6:.0f} MB estimate "
                         f"(min {SPACE_MIN_BYTES / 1e6:.0f} MB). Free up space or preview fewer clips (--top / --ids).")
    ep.work.mkdir(parents=True, exist_ok=True)
    use_px: dict[str, bool] = {}
    for key, (doc, _) in docs.items():
        if proxies is False:
            use_px[key] = False
            continue
        try:
            _, ok, why = use_proxies(ep, doc)
        except Exception as e:                               # a broken proxy must never block previews
            ok, why = False, f"proxy check failed: {e}"
        use_px[key] = ok
        if ok:
            say(f"{key}: decoding review proxies for the picture")
        elif proxies:
            raise SystemExit(f"--proxies given but {key}: {why}")
    report["proxies"] = use_px
    index = load_index(ep)
    if progress_log:
        progress.auto_log(ep, f"clip previews: rendering {len(todo)} for {name or 'episode'} (pad {pad:g}s, {height}p)", "note", "previews",
                          refresh_checklist=False)
    t0 = time.monotonic()

    def work(it: dict) -> tuple[dict, dict]:
        doc = docs[it["edit"]][0]
        try:
            return it, render_item(ep, it, doc, height, use_px[it["edit"]])
        except PreviewError:
            if not use_px[it["edit"]]:
                raise
            return it, render_item(ep, it, doc, height, False)     # proxy trouble: fall back to the real footage

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = {pool.submit(work, it): it for it in todo}
        done = 0
        for fu in as_completed(futs):
            it = futs[fu]
            done += 1
            try:
                _, res = fu.result()
            except Exception as e:                            # one bad clip must not lose the rest; the report (and exit code) say which
                report["failed"].append({"id": it["id"], "error": str(e)[:1500]})
                say(f"[{done}/{len(todo)}] {it['id']} FAILED: {str(e)[:300]}")
                continue
            w = it["window"]
            with _LOCK:
                index[it["id"]] = {"file": res["file"], "tight_file": res["tight_file"], "pad_before_s": w["pad_before_s"],
                                   "pad_after_s": w["pad_after_s"], "clip_in_s": w["clip_in_s"], "clip_out_s": w["clip_out_s"],
                                   "edit": it["edit"], "rendered_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                                   "sha": it["sha"]}
                _save_index(ep, index)
            report["rendered"].append({"id": it["id"], "preview_s": round(it["media_s"], 1), "render_s": res["render_s"],
                                       "bytes": res["bytes"], "tight_file": res["tight_file"], "lufs": res["lufs"], "warnings": res["warnings"]})
            say(f"[{done}/{len(todo)}] {it['id']}: {it['media_s']:.0f}s in {res['render_s']:.0f}s, {res['bytes'] / 1e6:.1f} MB"
                + (" (+tight)" if res["tight_file"] else ""))
            if progress_log and (done % 10 == 0 or done == len(todo)):
                progress.auto_log(ep, f"clip previews: {done}/{len(todo)} rendered", "note", "previews", refresh_checklist=False)
    report["wall_s"] = round(time.monotonic() - t0, 1)
    report["index"] = str(index_path(ep))
    if progress_log:
        progress.auto_log(ep, f"clip previews: {len(report['rendered'])} rendered, {len(report['failed'])} failed in {report['wall_s']:.0f}s",
                          "done" if not report["failed"] else "blocker", "previews", refresh_checklist=False)
    return report
