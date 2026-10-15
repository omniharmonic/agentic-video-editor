"""`ave assets ...`: local footage index, stock search/download, license log, music, authenticity critic.

Mechanical only. Every command prints JSON. Judgment (what a shot shows, whether it is authentic, which track fits the
moment) belongs to the agent via skills/ave-broll-music; the numbers it applies live in config/assets.critic.yaml.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from ..episode import Episode, read_json, write_json


def _out(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def _ep(a) -> Episode:
    ep = Episode(a.episode)
    ep.require()
    return ep


def _cfg(a, ep=None):
    from ..assets import critic
    try:
        return critic.load_config(getattr(a, "config", None), ep)
    except FileNotFoundError as e:
        raise SystemExit(str(e))


def _db(a):
    return getattr(a, "db", None)


# ---- local index -----------------------------------------------------------------------------------------------

def cmd_index(a) -> int:
    from ..assets import index
    try:
        _out(index.index_folder(a.folder, db_path=_db(a), threshold=a.threshold, max_shot_s=a.max_shot_s, force=a.force, embed=a.embed))
    except FileNotFoundError as e:
        raise SystemExit(str(e))
    return 0


def cmd_contact_sheet(a) -> int:
    from ..assets import index
    out = Path(a.out) if a.out else Path.cwd() / "contact-sheet.jpg"
    try:
        p = index.contact_sheet(_db(a), [int(i) for i in a.ids], out, cols=a.cols, cell_w=a.cell_width)
    except KeyError as e:
        raise SystemExit(f"{e.args[0]}. Run: ave assets search ... to list valid shot ids")
    _out({"contact_sheet": str(p), "shots": [int(i) for i in a.ids],
          "next": "LOOK at the image, then score each shot against `ave assets checklist` and store what you saw: ave assets tag <id> \"...\""})
    return 0


def cmd_tag(a) -> int:
    from ..assets import index
    try:
        tags = index.tag_shot(_db(a), int(a.id), a.tags, replace=a.replace)
    except KeyError as e:
        raise SystemExit(f"{e.args[0]}")
    _out({"id": int(a.id), "tags": tags})
    return 0


def cmd_search(a) -> int:
    from ..assets import index, critic
    cfg = critic.load_config()
    warn = critic.find_cliches(a.text or "", cfg)
    res = index.search(_db(a), a.text, a.min_duration, a.max_duration, a.aspect, a.min_quality, a.no_faces, a.limit)
    rows = [{k: s[k] for k in ("id", "file", "t0", "t1", "duration", "aspect", "quality", "flags", "face", "tags", "thumb", "matched")} for s in res]
    note = None
    if a.text and index.all_shots(_db(a)) and not any(s["tags"] for s in index.all_shots(_db(a))):
        note = "no shot has tags yet: make a contact sheet, look, and tag them (text search only matches tags)"
    _out({"query": a.text, "count": len(rows), "results": rows, "note": note, "query_cliches": warn["reject"] or None})
    return 0


# ---- stock providers + license log -----------------------------------------------------------------------------

def _provider(name: str):
    from ..assets import providers
    return providers.PROVIDERS[name]()


def _cands_path(ep: Episode) -> Path:
    return ep.work / "assets" / "candidates.json"


def cmd_stock_search(a) -> int:
    from ..assets import critic, providers
    ep = _ep(a)
    cfg = _cfg(a, ep)
    q = critic.find_cliches(a.query, cfg)
    if q["reject"] and not a.allow_cliche:
        raise SystemExit(f"Query '{a.query}' hits the cliche list ({', '.join(q['reject'])}). Search for something specific and observed "
                         "(a particular hand, place, object, weather), or pass --allow-cliche if the human asked for it.")
    names = ["pexels", "pixabay"] if a.provider == "both" else [a.provider]
    results, errors = [], []
    for n in names:
        try:
            results += _provider(n).search(a.query, per_page=a.per_page, orientation=a.orientation)
        except providers.ProviderError as e:
            errors.append(str(e))
    stored = read_json(_cands_path(ep)) if _cands_path(ep).exists() else {}
    for r in results:
        stored[f"{r['provider']}:{r['asset_id']}"] = {**r, "query": a.query}
        text = " ".join(r["tags"] + [r.get("title") or ""])
        hits = critic.find_cliches(text, cfg)
        r["cliche_tags"] = hits["reject"] or None
        r["discouraged"] = hits["discourage"] or None
    write_json(_cands_path(ep), stored)
    if not results and errors:
        raise SystemExit("; ".join(errors))
    _out({"query": a.query, "count": len(results), "candidates": results, "errors": errors,
          "next": "Preview thumb_url, run the authenticity checklist, `ave assets critique`, get human approval, then: ave assets stock-get"})
    return 0


def cmd_stock_get(a) -> int:
    from ..assets import critic, licenses, providers
    ep = _ep(a)
    key = f"{a.provider}:{a.asset_id}"
    stored = read_json(_cands_path(ep)) if _cands_path(ep).exists() else {}
    if key not in stored:
        raise SystemExit(f"{key} was not in a recent stock-search for this episode. Run: ave assets stock-search {ep.root} \"...\" --provider {a.provider}")
    cand = stored[key]
    hits = critic.find_cliches(" ".join(cand["tags"] + [cand.get("title") or "", a.query or cand.get("query", "")]), _cfg(a, ep))
    if hits["reject"] and not a.allow_cliche:
        raise SystemExit(f"{key} hits the cliche list ({', '.join(hits['reject'])}); refusing to download. Pass --allow-cliche only if the human asked for it.")
    try:
        r = providers.download(_provider(a.provider), cand, ep, licenses.log_for(ep), query=a.query or cand.get("query", ""), approved_by=a.approved_by)
    except (ValueError, providers.ProviderError) as e:
        raise SystemExit(str(e))
    _out({**r, "license_log": str(licenses.log_for(ep).path),
          "next": "Add it to the edit as an overlay (kind broll); `ave assets license-check` must pass before render/publish."})
    return 0


def cmd_log_license(a) -> int:
    """Record a license for an asset that did not come from our providers (own footage, a purchased track, a CC track)."""
    from ..assets import licenses
    ep = _ep(a)
    f = Path(a.file)
    p = f if f.is_absolute() else ep.root / f
    if not p.is_file():
        raise SystemExit(f"{p} does not exist. Give --file as a path inside the episode, or an absolute path.")
    try:
        rel = str(p.resolve().relative_to(ep.root))
    except ValueError:
        rel = str(p.resolve())
    entry = {"source": a.source, "asset_id": a.asset_id, "url": a.url, "file": rel, "sha256": licenses.sha256_file(p),
             "license": a.license, "license_url": a.license_url or a.url, "license_text": a.license_text, "query": a.query,
             "approved_by": a.approved_by, "author": a.author, "notes": a.notes,
             "attribution": {"required": a.attribution_required, "text": a.attribution or ""}}
    try:
        e = licenses.log_for(ep).append({k: v for k, v in entry.items() if v is not None})
    except ValueError as err:
        raise SystemExit(str(err))
    _out(e)
    return 0


def _latest_doc(ep: Episode, stage: str | None):
    from .. import editdoc
    for s in ([stage] if stage else ["final", "rough", "assembly"]):
        p = editdoc.latest_path(ep, s)
        if p:
            return editdoc.load(p), p
    raise SystemExit(f"No {stage or 'final/rough/assembly'} edit found for {ep.root.name}. Build one first (see ave status).")


def cmd_license_check(a) -> int:
    from ..assets import licenses
    ep = _ep(a)
    doc, path = _latest_doc(ep, a.stage)
    files = [m["file"] for m in doc["tracks"].get("music", []) if m.get("file")]
    files += [o["file"] for o in doc["tracks"].get("overlays", []) if o.get("kind") == "broll" and o.get("file")]
    # Library tracks and clips that were never logged are exactly the legal gap this check exists to catch.
    cov = licenses.coverage(ep, sorted(set(files)))
    log = licenses.log_for(ep)
    chain = log.verify()
    used = [c["entry"] for c in cov["covered"]]
    cov["covered"] = [{k: v for k, v in c.items() if k != "entry"} for c in cov["covered"]]
    ok = not cov["missing"] and not cov["unreadable"] and not chain
    _out({"ok": ok, "edit": path.name, "assets_in_edit": len(files), **cov, "log_problems": chain,
          "required_credits": licenses.attribution_lines(used),
          "courtesy_credits": licenses.attribution_lines(used, include_optional=True),
          "fix": None if ok else "Each missing file needs a license entry: ave assets stock-get (providers) or ave assets log-license (anything else). Do not render or publish until ok."})
    return 0 if ok else 1


# ---- music -----------------------------------------------------------------------------------------------------

def cmd_music_analyze(a) -> int:
    from ..assets import music
    try:
        r = music.analyze(a.file)
    except Exception as e:
        raise SystemExit(f"cannot analyze {a.file}: {e}")
    if not a.beats:
        r.pop("beat_times", None)
    if a.duration:
        r["suggest"] = music.suggest_points({**music.analyze(a.file)}, a.duration, _cfg(a))
    _out(r)
    return 0


def cmd_music_pick(a) -> int:
    from ..assets import music
    try:
        _out(music.pick(a.dir, a.mood, a.duration, _cfg(a), under_speech=not a.no_speech_fit, limit=a.limit))
    except (FileNotFoundError, ValueError) as e:
        raise SystemExit(str(e))
    return 0


# ---- critic ----------------------------------------------------------------------------------------------------

def cmd_checklist(a) -> int:
    cfg = _cfg(a)
    _out({"config": cfg.get("_path"), "score_each": "0-2, from what you SEE on the contact sheet", "pass_total": cfg["scoring"]["pass_total"],
          "hard_gates": cfg["scoring"]["hard_gate_zero"], "criteria": cfg["criteria"], "pacing": cfg.get("pacing"),
          "candidate_json": {"id": "...", "query": "...", "tags": ["..."], "filename": "...", "duration": 5.2, "has_audio": True,
                             "release": None, "looked_at": "path/to/contact-sheet.jpg",
                             "scores": {k: {"score": 2, "evidence": "what you saw"} for k in cfg["criteria"]}}})
    return 0


def cmd_critique(a) -> int:
    from ..assets import critic
    cfg = _cfg(a, Episode(a.episode) if a.episode else None)
    try:
        data = json.loads(Path(a.candidate).read_text())
    except (OSError, ValueError) as e:
        raise SystemExit(f"cannot read candidate JSON {a.candidate}: {e}")
    items = data if isinstance(data, list) else data.get("candidates", [data])
    try:
        reports = [critic.critique(c, cfg) for c in items]
    except ValueError as e:
        raise SystemExit(str(e))
    _out(reports[0] if not isinstance(data, list) and "candidates" not in data else {"results": reports, "passed": sum(r["pass"] for r in reports)})
    return 0 if all(r["pass"] for r in reports) else 1


def cmd_pacing(a) -> int:
    from ..assets import critic
    ep = _ep(a)
    cfg = _cfg(a, ep)
    doc, path = _latest_doc(ep, a.stage)
    load = lambda p: json.loads(Path(p).read_text()) if p else []  # noqa: E731
    ov = [{"id": o["id"], "o0": o["o0"], "o1": o["o1"]} for o in doc["tracks"].get("overlays", []) if o.get("kind") == "broll"]
    r = critic.check_placements(ov, doc["duration"], load(a.beats), cfg, music=doc["tracks"].get("music", []), punchlines=load(a.punchlines))
    _out({"edit": path.name, **r, "note": "beats/punchlines come from the human or your read of the transcript: pass --beats/--punchlines files; without them those rules are not checked"
          if not (a.beats or a.punchlines) else None})
    return 0 if r["ok"] else 1


# ---- registration ----------------------------------------------------------------------------------------------

def register(sub) -> None:
    s = sub.add_parser("assets", help="B-roll and music: local index, stock search, license log, music, authenticity critic")
    ss = s.add_subparsers(dest="assets_cmd", required=True)

    def add(name, fn, help_):
        p = ss.add_parser(name, help=help_)
        p.set_defaults(fn=fn)
        return p

    p = add("index", cmd_index, "scene-detect + thumbnail + quality metrics for a folder of footage -> SQLite")
    p.add_argument("folder"); p.add_argument("--db"); p.add_argument("--threshold", type=float, default=0.3, help="ffmpeg scene score (default 0.3)")
    p.add_argument("--max-shot-s", type=float, default=30.0, help="split longer shots into windows of at most this length")
    p.add_argument("--force", action="store_true"); p.add_argument("--embed", action="store_true", help="also compute CLIP embeddings if open_clip is installed")

    p = add("contact-sheet", cmd_contact_sheet, "labeled grid image of shots for the agent to LOOK at")
    p.add_argument("ids", nargs="+"); p.add_argument("--db"); p.add_argument("--out"); p.add_argument("--cols", type=int, default=4); p.add_argument("--cell-width", type=int, default=320)

    p = add("tag", cmd_tag, "store the agent's description of a shot")
    p.add_argument("id"); p.add_argument("tags"); p.add_argument("--db"); p.add_argument("--replace", action="store_true")

    p = add("search", cmd_search, "search tags (+embeddings if present) with duration/aspect/quality filters")
    p.add_argument("text", nargs="?"); p.add_argument("--db"); p.add_argument("--min-duration", type=float); p.add_argument("--max-duration", type=float)
    p.add_argument("--aspect", choices=["landscape", "portrait", "square"]); p.add_argument("--min-quality", type=float)
    p.add_argument("--no-faces", action="store_true"); p.add_argument("--limit", type=int, default=20)

    p = add("stock-search", cmd_stock_search, "search Pexels/Pixabay (cached 24 h); cliche queries are refused")
    p.add_argument("episode"); p.add_argument("query"); p.add_argument("--provider", choices=["pexels", "pixabay", "both"], default="both")
    p.add_argument("--orientation", choices=["landscape", "portrait", "square"]); p.add_argument("--per-page", type=int, default=15)
    p.add_argument("--allow-cliche", action="store_true"); p.add_argument("--config")

    p = add("stock-get", cmd_stock_get, "download ONE human-approved candidate and log its license")
    p.add_argument("episode"); p.add_argument("provider", choices=["pexels", "pixabay"]); p.add_argument("asset_id")
    p.add_argument("--approved-by", required=True, help="the human who approved this asset"); p.add_argument("--query"); p.add_argument("--allow-cliche", action="store_true"); p.add_argument("--config")

    p = add("log-license", cmd_log_license, "record a license for own footage, a purchased/CC track, or any non-provider asset")
    p.add_argument("episode"); p.add_argument("--file", required=True); p.add_argument("--source", required=True, help="e.g. own-footage, artlist, epidemic, freemusicarchive")
    p.add_argument("--license", required=True); p.add_argument("--license-text", required=True, help="the terms, quoted or snapshotted"); p.add_argument("--license-url")
    p.add_argument("--url", required=True, help="where the asset/terms live (page URL or 'file://own')"); p.add_argument("--approved-by", required=True)
    p.add_argument("--asset-id"); p.add_argument("--query"); p.add_argument("--author"); p.add_argument("--notes")
    p.add_argument("--attribution", help="exact credit line"); p.add_argument("--attribution-required", action="store_true")

    p = add("license-check", cmd_license_check, "every music/B-roll file in the latest edit has a license entry? exits 1 if not")
    p.add_argument("episode"); p.add_argument("--stage", choices=["assembly", "rough", "final"])

    p = add("music-analyze", cmd_music_analyze, "tempo, key, onset density, RMS, centroid, duration of a track")
    p.add_argument("file"); p.add_argument("--beats", action="store_true"); p.add_argument("--duration", type=float, help="also suggest in/out points for this length"); p.add_argument("--config")

    p = add("music-pick", cmd_music_pick, "rank tracks in a folder for a mood and for sitting under speech")
    p.add_argument("--dir", required=True); p.add_argument("--mood", required=True); p.add_argument("--duration", type=float, required=True)
    p.add_argument("--limit", type=int, default=10); p.add_argument("--no-speech-fit", action="store_true"); p.add_argument("--config")

    p = add("checklist", cmd_checklist, "print the authenticity checklist and candidate JSON shape")
    p.add_argument("--config")

    p = add("critique", cmd_critique, "pass/fail a candidate (or list) against the authenticity checklist; exits 1 on fail")
    p.add_argument("candidate"); p.add_argument("--episode"); p.add_argument("--config")

    p = add("pacing", cmd_pacing, "check B-roll length/coverage/emotional beats and music level in the latest edit; exits 1 on violations")
    p.add_argument("episode"); p.add_argument("--stage", choices=["assembly", "rough", "final"]); p.add_argument("--beats"); p.add_argument("--punchlines"); p.add_argument("--config")
