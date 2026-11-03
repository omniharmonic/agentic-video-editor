"""`ave studio ingest shorts <episode>`: human-approved clips and their exported shorts become studio posts (spec 9.2, 9.3).

    episode shorts  --(clips a human approved, built and exported by `ave shorts`)-->  library assets (the render, derived_from its sources
                    and any placed B-roll)  -->  one POST per clip with one VARIANT per chosen account  -->  (only with --sync) queue items

What this does and does not do:

  * Only clips a HUMAN approved: `clips.json` says `approved` AND the learning ledger's latest decision for that clip is `keep`, recorded by a named
    person from a terminal or the UI (`via` tty|ui). A decision an agent typed (`via: notty`) is not enough; the message says how to confirm it. An
    explicit `--clips` with any clip that fails is refused as a whole (nothing is created). Nothing is ever queued from an unapproved clip.
  * Only finished exports: the latest `exports/shorts/<slug>.vN.<platform>.mp4` whose sidecar is not a draft, whose bytes match its recorded sha256,
    whose limits, encode and licenses are ok, whose cut is a complete statement (no `--allow-incomplete` override), and which is the LATEST short
    version (a newer cut with no export yet is refused, never posted stale).
  * One variant per chosen account with the right file and post type for its platform (Instagram/Facebook -> Reels, YouTube -> Short, TikTok/X ->
    video; the TikTok render is the Reels encode). Accounts without a vertical-video format are reported, not guessed.
  * Copy is a DRAFT from the clip's own words: a verbatim quote (the hook when it is said in the clip, else a sentence of the clip), the speaker, the
    title (YouTube: first line), required credits, in the show voice (the package critic, per-studio policy) and within the platform's limit;
    hashtags per `config/package.default.yaml` `copy.max_hashtags`. Our own words are the title and the "Credits" label only.
  * Library: the render is registered with `derived_from` = the episode's source files the short uses (copied by sha as UNATTESTED `own` claims, like
    `adopt`) + every B-roll asset placed from this studio's library + music/other licensed files from the episode's license log. So the license closure
    a variant needs is real, and a human must `ave studio library attest` before a queue item can exist.
  * Never approves, never schedules, never sends. `--sync` runs `queue sync` (preflight and the license closure) and nothing else.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..episode import Episode, read_json
from ..publish import safety
from . import store
from .events import AGENT, Actor

# studio platform -> (the `ave shorts export` platform whose file it posts, the studio post type)
SHORT_FORMAT = {"instagram": ("reels", "reel"), "facebook": ("facebook", "reel"), "youtube": ("youtube", "short"),
                "tiktok": ("reels", "video"), "x": ("x", "video")}
_SIDE = re.compile(r"^(?P<slug>.+)\.v(?P<v>\d+)\.(?P<plat>[a-z0-9]+)\.json$")
APPROVING_VIA = ("tty", "ui")


# ---- who approved the clip -------------------------------------------------------------------------------------------------------

def _decisions(ep: Episode) -> list[dict]:
    from .. import learning
    p = learning.ledger_path(ep)
    rows = []
    for line in p.read_text().splitlines() if p.is_file() else []:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def approval_problems(ep: Episode, clip: dict, rows: list[dict] | None = None) -> list[str]:
    """Why this clip does not count as human-approved (empty = a named human kept it, from a terminal or the UI)."""
    rows = _decisions(ep) if rows is None else rows
    cid = clip["id"]
    out = []
    if clip.get("status") != "approved":
        out.append(f"clip {cid} is {clip.get('status')}, not approved: a human approves a clip with `ave clips decide` or in `ave ui`")
    mine = [r for r in rows if r.get("clip_id") == cid]
    if not mine:
        out.append(f"clip {cid} has no human decision on record (work/learning/clip_decisions.jsonl): a clip that is marked approved without one is not "
                   "trusted; ask the human to decide it in `ave ui` or run `! ave clips decide <episode> " + cid + " --verdict keep` in their own terminal")
        return out
    last = mine[-1]
    if last.get("verdict") != "keep":
        out.append(f"the latest decision on clip {cid} is {last.get('verdict')!r} ({last.get('ts')}), not keep")
    elif not (last.get("by") or "").strip():
        out.append(f"the decision that approved clip {cid} names nobody")
    elif last.get("via") not in APPROVING_VIA:
        out.append(f"the decision that approved clip {cid} was recorded by a command run without a human terminal (via {last.get('via')!r}, by "
                   f"{last.get('by')!r}): an agent can type that, so it does not count. Ask the human to confirm it in `ave ui`, or to run "
                   f"`! ave clips decide <episode> {cid} --verdict keep` in their own terminal")
    return out


# ---- the exported renders -------------------------------------------------------------------------------------------------------------

def exports_for(ep: Episode, slug: str) -> dict[str, dict]:
    """{export platform: {version, mp4, sidecar_path, sidecar}} for the LATEST exported version of each platform (drafts are never looked at)."""
    from .. import shorts
    d = shorts.shorts_dir(ep)
    best: dict[str, dict] = {}
    for f in sorted(d.glob(f"{slug}.v*.json")) if d.is_dir() else []:
        m = _SIDE.match(f.name)
        if not m or m["slug"] != slug:
            continue
        v = int(m["v"])
        if m["plat"] not in best or v > best[m["plat"]]["version"]:
            try:
                side = read_json(f)
            except (OSError, ValueError):
                continue
            best[m["plat"]] = {"version": v, "sidecar_path": f, "sidecar": side, "mp4": ep.root / side["video"] if side.get("video") else f.with_suffix(".mp4")}
    return best


def render_problems(ep: Episode, clip: dict, ex: dict, latest_version: int | None) -> list[str]:
    """Why this exported render may not become a post (empty = fine). Every item is a sentence the agent can act on."""
    side, mp4 = ex["sidecar"], ex["mp4"]
    out = []
    if side.get("draft"):
        out.append("this export is a DRAFT (made from a clip that was not approved): `ave shorts export` it again from the approved clip")
    if not mp4.is_file():
        return out + [f"the file {mp4.name} named by its sidecar is missing"]
    if safety.sha256_file(mp4) != side.get("sha256"):
        out.append(f"{mp4.name} no longer has the sha256 its sidecar recorded (the file changed after the export): export it again")
    if (side.get("clip") or {}).get("id") not in (None, clip["id"]):
        out.append(f"the sidecar belongs to clip {side['clip']['id']}, not {clip['id']}")
    lim = side.get("limits") or {}
    if lim.get("ok") is False:
        out.append("it fails its platform limits: " + "; ".join(lim.get("problems") or ["see the sidecar"]))
    if (side.get("encode") or {}).get("problems"):
        out.append("encode problems: " + "; ".join(side["encode"]["problems"]))
    if (side.get("licenses") or {}).get("ok") is False:
        out.append("its licenses are not ok: " + "; ".join(map(str, (side["licenses"].get("missing") or ["see the sidecar"]))))
    comp = side.get("completeness") or {}
    if not comp:
        out.append("it was exported before the complete-statement gate existed: rebuild with `ave shorts build --force` and export again")
    elif comp.get("override"):
        out.append("it was cut with --allow-incomplete (an override of the complete-statement guarantee): a human accepted a mid-thought edge once; "
                   "post it by hand, or pick a clip that passes")
    elif not comp.get("complete"):
        out.append("its cut is not a complete statement: " + "; ".join(comp.get("problems") or []))
    if latest_version is not None and side.get("version") != latest_version:
        out.append(f"a newer cut (short v{latest_version}) exists and has no export for this platform yet (this one is v{side.get('version')}): "
                   f"`ave shorts export <episode> --clip {clip['slug']}`")
    return out


# ---- copy ----------------------------------------------------------------------------------------------------------------------------

def _tokens(s: str) -> list[str]:
    from .. import completeness
    return completeness.norm_words(s)


def _contains(text: str, quote: str) -> bool:
    t, q = _tokens(text), _tokens(quote)
    return bool(q) and any(t[i:i + len(q)] == q for i in range(len(t) - len(q) + 1))


def sentences(text: str) -> list[str]:
    """The clip's own sentences, verbatim, split on terminal punctuation (the transcript's own, never rewritten)."""
    parts = re.findall(r"[^.!?]+[.!?]+[\"”’)]*|[^.!?]+$", " ".join((text or "").split()))
    return [p.strip() for p in parts if len(_tokens(p)) >= 3]


def quote_candidates(clip: dict) -> list[str]:
    """Verbatim quotes in the order to try them: the hook (when it is actually said in the clip), then the clip's sentences by position."""
    text = clip.get("text") or ""
    out = []
    hook = (clip.get("hook") or "").strip()
    if hook and _contains(text, hook):
        out.append(hook)
    out += [s for s in sentences(text) if s not in out]
    return out


def speaker_name(ep: Episode, clip: dict) -> str | None:
    """The speaker's name when exactly one person speaks in the clip and episode.json names them."""
    try:
        meta = read_json(ep.meta_path)
    except (OSError, ValueError):
        return None
    ids = [s for s in (clip.get("speakers") or []) if s]
    if len(ids) != 1:
        return None
    sp = (meta.get("speakers") or {}).get(ids[0])
    name = (sp.get("name") if isinstance(sp, dict) else None) or None
    return str(name) if name and name != ids[0] else None


def title_for(ep: Episode, clip: dict, sidecar: dict) -> dict:
    """{text, source}: `work/shorts/titles.json` (slug, then id), then the title the short opened with, then the sidecar/clip title."""
    from .. import opening
    titles = opening._titles(ep)
    for key in (clip.get("slug"), clip.get("id")):
        if key and titles.get(key):
            return {"text": " ".join(str(titles[key]).split()), "source": f"work/shorts/titles.json[{key}]"}
    t = (sidecar.get("opening") or {}).get("title")
    if t:
        return {"text": " ".join(str(t).split()), "source": "the short's opening title"}
    return {"text": " ".join(str(sidecar.get("title") or clip.get("title") or clip.get("slug") or "").split()), "source": "clip title"}


def draft_copy(sp, ep: Episode, clip: dict, sidecar: dict, platform: str, title: dict) -> dict:
    """The caption and hashtags of one variant, from the clip's transcript, through the package voice critic and the platform's limit.
    Returns {caption, hashtags, quote, problems, title}. Nothing is invented: a quote is a sentence the speaker said; the rest is the title,
    the speaker's name, the required credits and the hashtags."""
    from ..package import collect, postcopy
    from ..publish.adapters import base
    policy = collect.load_policy(sp)
    cfg = base.load_platforms()
    lic = sidecar.get("licenses") or {}
    required = list(lic.get("attribution_required") or [])
    courtesy = [c for c in (lic.get("attribution") or []) if c not in required]
    meta = collect.episode_meta(ep)
    show = meta.get("show")
    show = show.get("name") if isinstance(show, dict) else show
    facts = postcopy.make_facts({**meta, "show": show}, [{"text": clip.get("text") or ""}], [{"title": clip.get("title") or ""}], [], required + courtesy)
    tags = [t.lstrip("#") for t in postcopy.tags_for(facts, platform, policy)]
    who = speaker_name(ep, clip)
    limit, unit = postcopy.limit_for(platform, cfg)

    def assemble(quote: str | None, credits: list[str]) -> str:
        parts = []
        if platform == "youtube" and title["text"]:
            parts.append(title["text"])                            # the studio queue takes a YouTube title from the first line
        if quote:
            parts.append(f"“{quote}”" + (f"\n— {who}" if who else ""))
        if credits:
            parts.append("Credits\n" + "\n".join(credits))
        return "\n\n".join(parts).strip()

    def fits(text: str) -> bool:
        return limit is None or postcopy.measure(text, unit, policy) <= limit

    quotes = quote_candidates(clip) or [None]
    chosen: tuple[str, str | None] | None = None
    for credits in (required + courtesy, required):                 # courtesy credits go first when the limit is tight; required ones never do
        for q in quotes:
            text = assemble(q, credits)
            if text and fits(text):
                chosen = (text, q)
                break
        if chosen:
            break
    problems: list[str] = []
    if chosen is None:                                              # nothing fits: the fullest honest version, with the problem said out loud
        q = quotes[0]
        chosen = (assemble(q, required), q)
        problems.append(f"over the {platform} limit ({postcopy.measure(chosen[0], unit, policy)} of {limit} {unit}) even with the shortest quote: edit it")
    caption, quote = chosen
    check = caption + ("\n\n" + " ".join("#" + t for t in tags) if tags else "")
    problems += postcopy.critique(check, platform, policy)
    if title["text"]:
        problems += [f"title: {p}" for p in postcopy.critique(title["text"], platform, policy)]
    if not quote:
        problems.append("no quote from the clip fits: the caption carries no words of the speaker")
    return {"caption": caption, "hashtags": tags, "quote": quote, "problems": problems, "title": title}


# ---- the library ---------------------------------------------------------------------------------------------------------------------

def _entry_from_episode_log(ep: Episode, f: Path) -> dict | None:
    from ..assets import licenses
    e = licenses.log_for(ep).find(sha256=licenses.sha256_file(f))
    if not e:
        return None
    keep = ("source", "url", "license", "license_url", "license_text", "approved_by", "author", "notes", "attribution", "asset_id")
    return {k: e[k] for k in keep if e.get(k) not in (None, "")}


def register_inputs(sp, ep: Episode, doc: dict, by: str, now=None) -> dict:
    """Copy what the short was made from into the library as claims: the episode's source files it uses, its music/other licensed files and (already
    there) the B-roll placed from this library. Returns {derived_from, sources: [{asset, file}], licensed: [...], placed: [...], problems: []}."""
    from .. import library, shorts
    from ..broll import place
    problems: list[str] = []
    derived: list[str] = []
    sources, licensed = [], []
    used = sorted({s["src"] for n in ("video", "dialogue") for s in doc["tracks"].get(n) or [] if s.get("src") and not s.get("fill")})
    for k in used:
        rel = doc["sources"][k]["file"]
        f = ep.root / rel
        if not f.is_file():
            problems.append(f"source {k} ({rel}) is missing: cannot register what the short was made from")
            continue
        rec = library.add_file(sp, f, source={"type": "own"}, by=by, attested=False, now=now,
                               origin={"episode": ep.root.name, "path": rel, "role": "source", "source_key": k})
        derived.append(rec["id"])
        sources.append({"asset": rec["id"], "file": rel})
    from ..assets import licenses
    for rel in shorts.licensed_files(doc):                           # music and any other licensed file (episode license log); library B-roll is below
        f = ep.root / rel if not Path(rel).is_absolute() else Path(rel)
        if not f.is_file():
            problems.append(f"licensed file {rel} is missing")
            continue
        entry = _entry_from_episode_log(ep, f)
        if entry is None:
            problems.append(f"{rel} has no license-log entry in the episode (`ave assets log-license`): it cannot enter the library")
            continue
        stype = "stock" if str(entry.get("source", "")).lower() in ("pexels", "pixabay", "unsplash", "coverr") else "import"
        rec = library.add_file(sp, f, source={"type": stype, "provider": entry.get("source")}, license_entry=entry, by=by, attested=False, now=now,
                               origin={"episode": ep.root.name, "path": rel, "role": "licensed"})
        derived.append(rec["id"])
        licensed.append({"asset": rec["id"], "file": rel})
    placed = []
    lib = place.closure(ep, doc)
    if lib["required"]:
        mine = sp.root.resolve()
        for o in place.library_overlays(doc):
            if place.studio_of(ep, o["library"]).root.resolve() != mine:
                problems.append(f"B-roll {o['id']} was placed from another studio ({o['library']['studio']}): its asset is not in this library; "
                                "place it from this studio or ingest into that one")
        for a in lib["derived_from"]:
            if a not in derived:
                derived.append(a)
            placed.append(a)
        if not lib["ok"]:
            problems += [f"B-roll {m['asset']}: {m['why']}" for m in lib["missing"]] + list(lib["problems"])
    return {"derived_from": derived, "sources": sources, "licensed": licensed, "placed": placed, "problems": problems}


def register_render(sp, ep: Episode, mp4: Path, sidecar: dict, derived: list[str], by: str, now=None) -> dict:
    """The exported file as a `render` asset derived from `derived` (its license is the license of everything it was made from)."""
    from .. import library
    return library.add_file(sp, mp4, source={"type": "render"}, by=by, derived_from=derived, now=now, tags=["short", sidecar["clip"]["slug"]],
                            origin={"episode": ep.root.name, "short": sidecar["clip"]["slug"], "version": sidecar.get("version"), "clip": sidecar["clip"]["id"],
                                    "platform": sidecar.get("platform"), "sidecar": ep.rel(sidecar["video"]) if sidecar.get("video") else None})


# ---- the command -----------------------------------------------------------------------------------------------------------------------

def _origin_ref(ep: Episode, clip: dict, version: int) -> str:
    return f"{ep.root}::short::{clip['id']}::v{version}"


def _existing(sp, ref: str) -> dict | None:
    for p in store.list_posts(sp):
        if ((p.get("idea") or {}).get("origin") or {}).get("ref") == ref:
            return p
    return None


def _accounts(sp, only: list[str] | None) -> tuple[list[Any], list[dict]]:
    from ..platforms.accounts import list_accounts
    accts = list_accounts(sp)
    skipped = []
    if only:
        have = {a.id for a in accts}
        for want in only:
            if want not in have:
                skipped.append({"account": want, "why": "no such studio account (`ave studio account list`)"})
        accts = [a for a in accts if a.id in only]
    return accts, skipped


def shorts_ingest(studio, path: str | Path, *, clips: list[str] | None = None, accounts: list[str] | None = None, by: str | None = None,
                  sync: bool = False, dry_run: bool = False, actor: Actor = AGENT) -> dict:
    """See the module docstring. Returns a report; `refused` lists every clip that may not become a post and why (an explicit `--clips` with any
    refusal creates nothing), `skipped` lists accounts/clips that were left out and why."""
    from .. import editdoc, learning, shorts
    sp = studio.sp
    ep = Episode(path)
    ep.require()
    by = by or learning._by()
    cpath = ep.work / "clips.json"
    if not cpath.is_file():
        raise ValueError(f"{ep.root.name} has no clips yet (work/clips.json): the shorts path is `ave clips propose` -> a human decides -> `ave shorts build|export`")
    allclips = read_json(cpath)["clips"]
    rows = _decisions(ep)
    explicit = bool(clips)
    if explicit:
        by_key = {}
        for c in allclips:
            by_key[c["id"]] = c
            by_key[c["slug"]] = c
        unknown = [x for x in clips if x not in by_key]
        if unknown:
            raise ValueError(f"unknown clip(s) {', '.join(unknown)}; this episode has {', '.join(c['id'] for c in allclips)}")
        chosen = []
        for x in clips:
            if by_key[x] not in chosen:
                chosen.append(by_key[x])
    else:
        chosen = [c for c in allclips if c.get("status") == "approved"]
    report: dict[str, Any] = {"episode": str(ep.root), "dry_run": dry_run, "posts": [], "refused": [], "skipped": [], "sync": None, "warnings": []}
    candidates = []
    for c in chosen:
        probs = approval_problems(ep, c, rows)
        if probs:
            report["refused"].append({"clip": c["id"], "slug": c["slug"], "stage": "approval", "reasons": probs})
            continue
        candidates.append(c)
    if not explicit:
        report["skipped"] += [{"clip": c["id"], "why": f"{c.get('status')}: not approved by a human"} for c in allclips
                              if c.get("status") != "approved" and c["id"] not in {x["id"] for x in chosen}]
    if not chosen:
        report["refused"].append({"clip": None, "stage": "approval", "reasons": ["no clip in this episode is approved yet: a human decides clips "
                                  "(`ave ui`, or `! ave clips decide <episode> <id> --verdict keep`), then `ave clips build --approved-only`, `ave shorts build`, `ave shorts export`"]})
    accts, skipped_accts = _accounts(sp, accounts)
    report["skipped"] += skipped_accts
    plans = []
    for c in candidates:
        slug = c["slug"]
        exps = exports_for(ep, slug)
        if not exps:
            report["refused"].append({"clip": c["id"], "slug": slug, "stage": "render", "reasons": [
                f"no exported short for {slug} in exports/shorts/ (drafts are never used): `ave shorts build {ep.root} --clip {slug}` then `ave shorts export {ep.root} --clip {slug}`"]})
            continue
        vs = editdoc.list_versions(ep, "short", slug)
        latest = vs[-1] if vs else None
        variants, bad = [], []
        for a in accts:
            fmt = SHORT_FORMAT.get(a.platform)
            if fmt is None:
                report["skipped"].append({"clip": c["id"], "account": a.id, "why": f"{a.platform} has no vertical-video short format in this build"})
                continue
            exp_plat, ptype = fmt
            ex = exps.get(exp_plat)
            if ex is None:
                report["skipped"].append({"clip": c["id"], "account": a.id, "why": f"no {exp_plat} export of {slug}: "
                                          f"`ave shorts export {ep.root} --clip {slug} --platforms {exp_plat}`"})
                continue
            if a.post_types and ptype not in a.post_types:
                report["skipped"].append({"clip": c["id"], "account": a.id, "why": f"the account does not take post type {ptype!r} (accepts {', '.join(a.post_types)})"})
                continue
            probs = render_problems(ep, c, ex, latest)
            if probs:
                bad.append({"account": a.id, "export": exp_plat, "reasons": probs})
                continue
            variants.append((a, exp_plat, ptype, ex))
        if bad:
            report["refused"].append({"clip": c["id"], "slug": slug, "stage": "render", "reasons": [f"{b['export']} export: {r}" for b in bad for r in b["reasons"]]})
            if explicit:
                continue
        if variants:
            plans.append((c, variants, latest))
        elif not bad:
            report["skipped"].append({"clip": c["id"], "why": "no chosen account takes a short from this clip"})
    if explicit and report["refused"]:
        report["posts"] = []
        report["note"] = "--clips names clips that may not be posted: nothing was created (fix or drop them and run again)"
        return report
    for c, variants, latest in plans:
        slug = c["slug"]
        doc = editdoc.load(editdoc.path_for(ep, "short", latest, slug))
        main_side = variants[0][3]["sidecar"]
        title = title_for(ep, c, main_side)
        ref = _origin_ref(ep, c, latest)
        existing = _existing(sp, ref)
        entry: dict[str, Any] = {"clip": c["id"], "slug": slug, "short_version": latest, "title": title, "variants": [], "post": existing["id"] if existing else None,
                                 "created": False}
        drafts = {}
        for a, exp_plat, ptype, ex in variants:
            dc = draft_copy(sp, ep, c, ex["sidecar"], a.platform, title)
            drafts[a.id] = dc
        reg = None
        if not dry_run:
            reg = register_inputs(sp, ep, doc, by, now=studio.now())
            if reg["problems"]:
                report["refused"].append({"clip": c["id"], "slug": slug, "stage": "library", "reasons": reg["problems"]})
                continue
        review = (doc.get("short") or {}).get("needs_review") or []
        reason = (f"short {slug} v{latest} of {ep.root.name}, clip {c['id']} (approved by {_approver(rows, c)}); drafted from the clip's transcript"
                  + (f"; needs_review: {'; '.join(review)}" if review else ""))
        if dry_run:
            entry["variants"] = [{"account": a.id, "platform": a.platform, "post_type": ptype, "export": exp_plat, "file": ep.rel(ex["mp4"]),
                                  "caption": drafts[a.id]["caption"], "hashtags": drafts[a.id]["hashtags"], "problems": drafts[a.id]["problems"]}
                                 for a, exp_plat, ptype, ex in variants]
            entry["would_create"] = existing is None
            report["posts"].append(entry)
            continue
        made = _make_post(studio, sp, ep, c, variants, drafts, title, ref, reg, existing, reason, review, actor, by)
        entry.update(made)
        report["posts"].append(entry)
        for v in review:
            w = f"{slug}: needs_review: {v}"
            if w not in report["warnings"]:
                report["warnings"].append(w)
    if sync and not dry_run:
        report["sync"] = _sync(studio, report["posts"], actor)
    report["next"] = _next(report, sync)
    return report


def _approver(rows: list[dict], clip: dict) -> str:
    mine = [r for r in rows if r.get("clip_id") == clip["id"]]
    return (mine[-1].get("by") or "a human") if mine else "a human"


def _make_post(studio, sp, ep: Episode, clip: dict, variants, drafts: dict, title: dict, ref: str, reg: dict, existing: dict | None, reason: str,
               review: list[str], actor: Actor, by: str) -> dict:
    first = variants[0]
    base_caption = drafts[first[0].id]["caption"]
    media_of, renders = {}, []
    for a, exp_plat, ptype, ex in variants:
        rec = register_render(sp, ep, ex["mp4"], ex["sidecar"], reg["derived_from"], by, now=studio.now())
        media_of[a.id] = [{"slot": 0, "asset": rec["id"], "role": "video", "alt": "", "crop": None}]
        if rec["id"] not in renders:
            renders.append(rec["id"])
    created = existing is None
    if created:
        post = studio.create_post(idea=title["text"] or clip["slug"], copy={"base": base_caption, "hashtags": drafts[first[0].id]["hashtags"]},
                                  media=media_of[first[0].id], actor=actor, reason=reason, confidence=None,
                                  origin={"type": "episode_short", "ref": ref, "episode": str(ep.root), "clip": clip["id"], "short": clip["slug"],
                                          "title_source": title["source"], "passages": []})
    else:
        post = existing
    rev = post["rev"]
    have = {(v["account"], v["post_type"]) for v in post.get("variants") or [] if not v.get("dropped")}
    rows = []
    for a, exp_plat, ptype, ex in variants:
        if (a.id, ptype) in have:
            rows.append({"account": a.id, "variant": next(v["id"] for v in post["variants"] if v["account"] == a.id and v["post_type"] == ptype),
                         "status": "already there"})
            continue
        dc = drafts[a.id]
        same_media = media_of[a.id] == (post.get("media") or [])
        post = studio.add_variant(post["id"], account=a.id, platform=a.platform, post_type=ptype, expected_rev=rev, actor=actor,
                                  copy={"caption": dc["caption"], "hashtags": dc["hashtags"]}, media=None if same_media else media_of[a.id])
        rev = post["rev"]
        v = post["variants"][-1]
        rows.append({"account": a.id, "platform": a.platform, "variant": v["id"], "post_type": ptype, "export": exp_plat, "asset": media_of[a.id][0]["asset"],
                     "file": ep.rel(ex["mp4"]), "caption": dc["caption"], "hashtags": dc["hashtags"], "quote": dc["quote"], "problems": dc["problems"],
                     "status": "created"})
    return {"post": post["id"], "created": created, "variants": rows,
            "library": {"renders": renders, **{k: reg[k] for k in ("derived_from", "sources", "licensed", "placed")}}, "needs_review": review}


def _sync(studio, posts: list[dict], actor: Actor) -> dict:
    """`queue sync` for each post this run touched: queue items and every preflight, NEVER an approval. A variant with an unattested source or a
    preflight problem is `blocked` and says why."""
    out = {"approved": False, "posts": []}
    for p in posts:
        if not p.get("post"):
            continue
        rep = studio.sync_queue(p["post"], actor=actor)
        out["posts"].append({"post": p["post"], "variants": [{k: v.get(k) for k in ("variant", "action", "queue", "queue_status", "problems")} for v in rep["variants"]]})
    return out


def _next(report: dict, sync: bool) -> list[str]:
    nxt = []
    if report["posts"] and not report.get("dry_run"):
        nxt.append("Review each draft in the studio (`ave studio post show <post>`, `ave studio post critique <post>`); edit the copy, then:")
        nxt.append("! ave studio library attest --all     (a HUMAN stands behind the license claims for the episode's own footage and music; unattested claims "
                   "block the queue)")
        if not sync:
            nxt.append("ave studio queue sync     (or re-run with --sync): creates the queue items and runs every preflight; never approves")
        nxt.append("! ave studio approve <post> <variant>     (a HUMAN, after viewing the exact file)")
    return nxt
