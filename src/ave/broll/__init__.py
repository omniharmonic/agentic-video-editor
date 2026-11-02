"""B-roll sourcing for the studio (WS1, spec 5): Pexels, Pixabay, Unsplash, Coverr and the studio's own library.

    search(sp, query, *, providers, kind, orientation, per_page, http, cache_dir, now, post) -> dict   writes work/broll/<id>.json
    download(sp, cand, *, approved_by, http, provider, query) -> {"asset", "license_entry", "reused"}   needs a named human
    pick(sp, search_id, "provider:id", *, by) -> same                                                    the human's choice, then download
    rescore(sp, search_id, key, scores) -> critic verdict                                                the agent's visual scores
    queries.generate(texts, ...) / queries.for_post(sp, post_id) -> {"queries": [...]}                   mechanical draft only
    register(studio)   plug the facade hooks (broll, broll_search, broll_pick) into a `Studio`

All provider facts are unverified (see providers.py); nothing here spends money or posts anything. Generative media (spec 5.5, a human-run
spend) is not part of this workstream.
"""

from __future__ import annotations

from . import queries
from .fetch import download
from .pick import BudgetExceeded, get_search, list_searches, pick, rescore, search
from .providers import ALL_NAMES, Candidate, ProviderError, make_providers

__all__ = ["ALL_NAMES", "BudgetExceeded", "Candidate", "ProviderError", "download", "get_search", "list_searches", "make_providers",
           "pick", "queries", "register", "rescore", "search"]


def _have_key(prov) -> bool:
    from ..publish import safety
    import os
    if getattr(prov, "name", "") == "local":
        return True
    return bool(getattr(prov, "_api_key", None) or os.environ.get(prov.env_key) or safety.get_secret(f"{prov.name}_api_key"))


def _providers_for(studio, names=None) -> dict:
    """Injected providers (Studio(providers=...)) win; otherwise real ones for the named providers, or those with a key plus local."""
    injected = dict(getattr(studio, "providers", None) or {})
    if names:
        missing = [n for n in names if n not in injected]
        return {**{n: injected[n] for n in names if n in injected}, **(make_providers(studio.sp, missing) if missing else {})}
    if injected:
        return injected
    everything = make_providers(studio.sp)
    return {n: p for n, p in everything.items() if _have_key(p)}


def hook_broll(studio, search_id: str) -> dict:
    """GET /api/studio/broll/<id> -> the search document (tests/data_studio/api/GET_broll.json)."""
    return get_search(studio.sp, search_id)


def hook_broll_search(studio, *, query: str, post: str | None = None, providers=None, kind: str | None = None, orientation: str | None = None,
                      per_page: int = 15) -> dict:
    """POST /api/studio/broll/search -> {"search": document}."""
    provs = _providers_for(studio, list(providers) if providers else None)
    return {"search": search(studio.sp, query, providers=provs, kind=kind, orientation=orientation, per_page=per_page, now=studio.now(),
                             post=post)}


def hook_broll_pick(studio, search: str, candidate: str, by: str) -> dict:
    """POST /api/studio/broll/pick -> {"asset", "license_entry"}. `by` is the human's name."""
    res = pick(studio.sp, search, candidate, by=by, providers=_providers_for(studio), now=studio.now())
    return {"asset": res["asset"], "license_entry": res["license_entry"], "reused": res["reused"]}


def register(studio) -> None:
    studio.register("broll", hook_broll)
    studio.register("broll_search", hook_broll_search)
    studio.register("broll_pick", hook_broll_pick)
