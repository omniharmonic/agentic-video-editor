"""Mechanical B-roll query generation (spec 5.2). Counting words and looking up a mood list is code's job; deciding what the post is
ABOUT and which of these queries are worth spending a search on is the agent's. The output is a draft to edit, and says so.

    generate(texts, ...) -> {"queries": [{"q", "why", "kind", "orientation"}], "keywords", "moods", "max_queries", "note"}
    for_post(sp, post_id) -> same, from a post's idea and copy
"""

from __future__ import annotations

import re
from typing import Iterable

from ..studio import policy, store

NOTE = ("mechanical draft: keywords counted from the text and moods matched from config/studio/moods.yaml. Edit it. Search only what "
        "you can justify; the post's query budget is max_queries.")
DEFAULT_MAX = 6


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def moods_config(sp=None) -> dict:
    if sp is not None:
        return policy.load_policy(sp, "moods")
    import yaml
    from .. import resources
    return yaml.safe_load((resources.home() / "config" / "studio" / "moods.yaml").read_text()) or {}


def generate(texts: Iterable[str] | str, *, post_type: str | None = None, kind: str | None = None, orientation: str | None = None,
             max_queries: int | None = None, mood_hints: Iterable[str] = (), cfg: dict | None = None, sp=None) -> dict:
    from ..package import postcopy                      # keyword counter reused as-is (words of 5+ letters, by frequency)
    texts = [texts] if isinstance(texts, str) else [t for t in texts if t]
    cfg = cfg if cfg is not None else moods_config(sp)
    cap = max_queries or int(cfg.get("max_queries") or DEFAULT_MAX)
    kind = kind or (cfg.get("kind_by_post_type") or {}).get(post_type or "", "photo")
    orientation = orientation or (cfg.get("orientation_by_post_type") or {}).get(post_type or "")
    if kind not in ("photo", "video"):
        raise ValueError("kind must be photo or video")

    kws = postcopy.keywords(texts, 8)
    toks = _words(" ".join(texts))
    kwset = set(kws)
    counts: dict[str, int] = {}
    order: dict[str, int] = {}
    for i in range(len(toks) - 1):
        if toks[i] in kwset and toks[i + 1] in kwset and toks[i] != toks[i + 1]:
            p = f"{toks[i]} {toks[i + 1]}"
            counts[p] = counts.get(p, 0) + 1
            order.setdefault(p, i)
    phrases = sorted(counts, key=lambda p: (-counts[p], order[p]))

    wordset = set(toks)
    hinted = {h.lower() for h in mood_hints}
    matched = [name for name, m in (cfg.get("moods") or {}).items()
               if name.lower() in hinted or wordset & {t.lower() for t in (m.get("triggers") or [])}]

    out: list[dict] = []
    seen: set[str] = set()

    def add(q: str, why: str) -> None:
        q = q.strip()
        if q and q not in seen:
            seen.add(q)
            out.append({"q": q, "why": why, "kind": kind, "orientation": orientation})

    for p in phrases:
        add(p, f"adjacent keywords, {counts[p]}x in the text")
    for w in kws:
        add(w, f"keyword, {sum(1 for t in toks if t == w)}x in the text")
    top = (phrases[:1] or kws[:1] or [""])[0]
    for name in matched:
        for mod in ((cfg.get("moods") or {})[name].get("modifiers") or [])[:2]:
            add(f"{top} {mod}" if top else mod, f"mood '{name}' matched in the text")
    return {"queries": out[:cap], "keywords": kws, "moods": matched, "max_queries": cap, "dropped": max(0, len(out) - cap), "note": NOTE}


def for_post(sp, post_id: str, **kw) -> dict:
    """Queries from a post's idea and copy (base copy, CTA, first comment, every variant caption)."""
    p = store.load_post(sp, post_id)
    copy = p.get("copy") or {}
    texts = [(p.get("idea") or {}).get("text"), copy.get("base"), copy.get("cta"), copy.get("first_comment")]
    texts += [(v.get("copy") or {}).get("caption") for v in p.get("variants") or []]
    types = [v.get("post_type") for v in p.get("variants") or [] if v.get("post_type")]
    kw.setdefault("post_type", types[0] if types else None)
    return generate([t for t in texts if t], sp=sp, **kw)
