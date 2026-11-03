"""Search -> critic -> HUMAN PICK -> download (spec 5.3). A search writes `work/broll/<search_id>.json`; no full-size file is fetched until
`pick` is called with the name of the human who chose it.

The authenticity critic (`assets.critic.critique`, reused unchanged) runs on every stock candidate. Code can only check what text and numbers
show: cliches in the title, tags and query, and a clip too short to use. It cannot see the picture, so every candidate is marked
`unscored` until the AGENT looks at the preview and calls `rescore` with its 0-2 scores; the verdict then follows the same arithmetic and hard
gates as for local footage. A critic verdict never blocks a human's pick (the human has the final say on taste); it is shown next to it.
"""

from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime
from pathlib import Path

from ..assets import critic
from ..episode import read_json, write_json
from ..studio import policy
from ..studio.clock import SystemClock, iso_utc
from . import fetch
from .providers import Candidate, ProviderError, make_providers

UNSCORED = "not visually judged"


class BudgetExceeded(ValueError):
    pass


def search_dir(sp) -> Path:
    return sp.work / "broll"


def search_path(sp, search_id: str) -> Path:
    if not search_id or "/" in search_id or ".." in search_id or search_id.startswith("."):
        raise ValueError(f"bad search id {search_id!r}")
    return search_dir(sp) / f"{search_id}.json"


def get_search(sp, search_id: str) -> dict:
    p = search_path(sp, search_id)
    if not p.is_file():
        raise KeyError(f"no such search: {search_id}")
    return read_json(p)


def list_searches(sp, post: str | None = None) -> list[dict]:
    out = []
    for p in sorted(search_dir(sp).glob("s_*.json")) if search_dir(sp).is_dir() else []:
        try:
            s = read_json(p)
        except (OSError, ValueError):
            continue
        if post is None or s.get("post") == post:
            out.append(s)
    return out


def max_queries(sp) -> int:
    own = (policy.studio_config(sp).get("broll") or {}).get("max_queries")
    if isinstance(own, int) and own > 0:
        return own
    return int(policy.load_policy(sp, "moods").get("max_queries") or 6)


def judge(c: dict, query: str, cfg: dict, scores: dict | None = None, *, release: bool | None = None, looked_at: bool = False) -> dict:
    """The critic verdict for one candidate dict. `verdict`: reject (a hard fail: cliche, too short, or a failed scored check),
    warn (discouraged wording), ok (nothing mechanical objects). `unscored` stays true until the agent supplies scores."""
    probe = {"id": f"{c['provider']}:{c['asset_id']}", "query": query, "title": c.get("title") or "", "tags": list(c.get("tags") or []),
             "duration": c.get("duration"), "looked_at": looked_at}
    if scores:
        probe["scores"] = scores
    if release is not None:
        probe["release"] = release
    res = critic.critique(probe, cfg)
    problems = [h for h in res["hard_fails"] if not h.startswith(UNSCORED) and not h.startswith("missing scores")]
    unscored = not scores or len(scores) < len(cfg["criteria"])
    verdict = "reject" if problems else ("warn" if res["warnings"] else "ok")
    out = {"verdict": verdict, "problems": problems, "warnings": res["warnings"], "unscored": unscored, "confidence": res["confidence"]}
    if scores:
        out.update({"scores": res["scores"], "total": res["total"], "max": res["max"], "pass": res["pass"], "reasons": res["reasons"]})
    return out


def _new_id(sp, query: str, providers: list[str], kind, at: datetime) -> str:
    base = hashlib.sha256(f"{query}|{','.join(providers)}|{kind}|{iso_utc(at)}".encode()).hexdigest()
    for n in range(4, 40):
        sid = f"s_{at.strftime('%Y%m%d')}_{base[:n]}"
        if not search_path(sp, sid).exists():
            return sid
    raise RuntimeError("could not allocate a search id")


THUMB_MAX_PX = 360


def _shrink(data: bytes, ext: str) -> tuple[bytes, str]:
    """A still thumbnail at most THUMB_MAX_PX on its long side (JPEG). Anything Pillow cannot decode is kept as fetched."""
    try:
        import io
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            if getattr(im, "is_animated", False) or max(im.size) <= THUMB_MAX_PX and ext == ".jpg":
                return data, ext
            im = im.convert("RGB")
            im.thumbnail((THUMB_MAX_PX, THUMB_MAX_PX))
            out = io.BytesIO()
            im.save(out, format="JPEG", quality=82)
            return out.getvalue(), ".jpg"
    except Exception:
        return data, ext


def _thumb(sp, provider, c: Candidate, dest_dir: Path) -> str | None:
    """Fetch the small preview once into the search folder so the UI never hotlinks (Pixabay forbids it). Best effort."""
    if provider.name == "local" or not c.preview_url.startswith("http"):
        return None
    try:
        r = provider.http.get(c.preview_url, timeout=30)
        if r.status_code >= 400:
            return None
        data = r.content if hasattr(r, "content") and isinstance(r.content, (bytes, bytearray)) else None
        if not data or fetch.sniff_ext(data[:16]) not in (".jpg", ".png", ".webp", ".gif"):
            return None
        dest_dir.mkdir(parents=True, exist_ok=True)
        ext = fetch.sniff_ext(data[:16])
        data, ext = _shrink(bytes(data), ext)
        p = dest_dir / f"{c.provider}-{re.sub(r'[^A-Za-z0-9._-]', '_', str(c.asset_id))[:80]}{ext}"
        p.write_bytes(data)
        return str(p.relative_to(sp.root))
    except Exception:
        return None


def search(sp, query, *, providers, kind=None, orientation=None, per_page: int = 15, http=None, cache_dir=None, now: datetime | None = None,
           post: str | None = None, page: int = 1, previews: bool = True) -> dict:
    """Search stock providers, run the critic on every candidate, write `work/broll/<id>.json` and return it.

    `providers`: names (built with `http`/`cache_dir`; keys come from env or keychain), provider objects, or a mapping name -> provider.
    `kind`: "photo" | "video" | None (every kind each provider offers). A provider that fails (no key, rate limit) is recorded under
    `errors` and the others still answer; if all fail, ProviderError. With `post`, the post's query budget (`max_queries`) is enforced."""
    query = (query or "").strip()
    if not query:
        raise ValueError("query is empty")
    at = now or SystemClock().now()
    if post:
        used = len(list_searches(sp, post))
        if used >= max_queries(sp):
            raise BudgetExceeded(f"post {post} already used {used} searches (max_queries {max_queries(sp)}); pick from them or raise "
                                 "broll.max_queries in studio.json")
    objs: dict = {}
    clock = (lambda: at.timestamp()) if now else time.time
    names_to_build = []
    for p in (providers.values() if isinstance(providers, dict) else providers):
        if isinstance(p, str):
            names_to_build.append(p)
        else:
            objs[p.name] = p
    if names_to_build:
        objs.update(make_providers(sp, names_to_build, http=http, cache_dir=cache_dir, now=clock))
    if not objs:
        raise ValueError("no providers given")
    cfg = critic.load_config(ep=sp)
    sid = _new_id(sp, query, list(objs), kind, at)
    cands, errors = [], []
    for name, prov in objs.items():
        for k in ([kind] if kind else sorted(prov.kinds)):
            if k not in prov.kinds:
                errors.append({"provider": name, "error": f"{name} does not offer {k}"})
                continue
            try:
                found = prov.search(query, kind=k, orientation=orientation, per_page=per_page, page=page)
            except ProviderError as e:
                errors.append({"provider": name, "error": str(e)})
                continue
            for c in found:
                d = c.as_dict()
                if previews:
                    d["preview_file"] = _thumb(sp, prov, c, search_dir(sp) / sid)
                    d["thumb"] = ("/files/" + d["preview_file"]) if d["preview_file"] else None      # a local path: the UI never loads provider URLs
                d["critic"] = judge(d, query, cfg)
                cands.append(d)
    if not cands and errors and len(errors) >= len(objs):
        raise ProviderError("; ".join(f"{e['provider']}: {e['error']}" for e in errors))
    doc = {"id": sid, "query": query, "kind": kind, "orientation": orientation, "per_page": per_page, "page": page, "providers": list(objs), "post": post,
           "created": iso_utc(at), "errors": errors, "candidates": cands, "picked": []}
    search_dir(sp).mkdir(parents=True, exist_ok=True)
    write_json(search_path(sp, sid), doc)
    return doc


def _find(doc: dict, key: str) -> dict:
    for c in doc["candidates"]:
        if f"{c['provider']}:{c['asset_id']}" == key:
            return c
    raise KeyError(f"candidate {key} is not in search {doc['id']}")


def rescore(sp, search_id: str, key: str, scores: dict, *, looked_at: bool = True, release: bool | None = None) -> dict:
    """Store the agent's 0-2 scores (from the preview or contact sheet) and the resulting critic verdict on the candidate."""
    doc = get_search(sp, search_id)
    c = _find(doc, key)
    c["critic"] = judge(c, doc["query"], critic.load_config(ep=sp), scores, release=release, looked_at=looked_at)
    write_json(search_path(sp, search_id), doc)
    return c["critic"]


def _reresolve(doc: dict, cand: Candidate, prov) -> Candidate:
    """Ask the PROVIDER again for this candidate (same query and page, so normally a cache hit) and use ITS record: the search file under work/ is
    writable by anyone with a shell, so its download URL, license facts, author and title are never trusted at pick time."""
    kind = cand.kind
    try:
        found = prov.search(doc["query"], kind=kind, orientation=doc.get("orientation"), per_page=int(doc.get("per_page") or 15), page=int(doc.get("page") or 1))
    except ProviderError as e:
        raise ProviderError(f"cannot confirm candidate {cand.key} with {cand.provider} right now ({e}); nothing was downloaded") from None
    fresh = next((f for f in found if f.asset_id == cand.asset_id), None)
    if fresh is None:
        raise ProviderError(f"{cand.provider} no longer returns {cand.key} for {doc['query']!r}; search again (nothing was downloaded)")
    return fresh


def pick(sp, search_id: str, key: str, *, by: str, http=None, providers=None, now: datetime | None = None) -> dict:
    """The human's pick: download candidate `key` ("provider:id") of the search, log its license, register it. `by` is the human's name.
    Returns {"asset", "license_entry", "reused"}; the pick is also recorded in the search file."""
    if not (by or "").strip():
        raise ValueError("by is required: a named human picks every download")
    doc = get_search(sp, search_id)
    c = _find(doc, key)
    cand = Candidate.from_dict(c)
    prov = None
    if isinstance(providers, dict):
        prov = providers.get(cand.provider)
    elif providers:
        prov = next((p for p in providers if getattr(p, "name", None) == cand.provider), None)
    if cand.provider != "local":
        if prov is None:
            prov = make_providers(sp, [cand.provider], http=http)[cand.provider]
        cand = _reresolve(doc, cand, prov)
    res = fetch.download(sp, cand, approved_by=by, http=http, provider=prov, query=doc["query"], now=now, origin={"search": search_id})
    doc = get_search(sp, search_id)               # re-read: another pick may have been recorded meanwhile
    doc.setdefault("picked", []).append({"candidate": key, "by": by.strip(), "asset": res["asset"]["id"], "reused": res["reused"],
                                         "at": iso_utc(now or SystemClock().now()), "critic": c.get("critic")})
    write_json(search_path(sp, search_id), doc)
    return res
