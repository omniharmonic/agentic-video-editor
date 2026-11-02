"""The voice and length critic for the copy of a post (spec 2.2: package/postcopy.py `critique` and `limit_for`, extended with platform metrics 4.4).

`critique_post(sp, post)` -> per variant: {"variant", "account", "platform", "caption", "problems": [...], "counter": {...}}. A problem is a sentence the
agent can act on. Text inside “...” is someone's verbatim quote and is not judged for voice. Studio override: `<studio>/policy/package.yaml`
(deep-merged over config/package.default.yaml, the same file `ave package` uses). Nothing here blocks anything; it advises.
"""

from __future__ import annotations

from ..package import collect, postcopy
from ..publish.adapters import base
from . import model, posttext, text_metrics, views


def _text(post: dict, v: dict) -> str:
    eff = model.effective_copy(post, v)
    tags = " ".join("#" + t.lstrip("#") for t in eff["hashtags"])
    return "\n\n".join(x for x in (eff["caption"] or "", tags, eff["link"] or "") if x).strip()   # for the voice critic (hashtags counted as copy)


def critique_post(sp, post: dict) -> list[dict]:
    from . import store
    policy = collect.load_policy(sp)
    assets = store.read_assets(sp)
    platforms = base.load_platforms()
    pcfg = (views.previews_config().get("platforms") or {})
    out = []
    for v in post.get("variants") or []:
        if v.get("dropped"):
            continue
        plat = v["platform"]
        eff = model.effective_copy(post, v)
        composed = posttext.compose_variant(post, v, assets)      # length is judged on what will actually be posted
        text = composed.text
        problems = list(postcopy.critique(_text(post, v), plat, policy))
        counter = (pcfg.get(plat) or {}).get("counter") or {}
        unit = counter.get("unit", "chars")
        used = text_metrics.counter(text, unit)
        limit, _ = postcopy.limit_for(plat, platforms)
        limit = limit or counter.get("soft_limit")
        if limit and text_metrics.over_limit(text, unit, limit):
            problems.append(f"caption is {used} {unit} (approximate), over the {limit} limit for {plat}")
        if not text.strip():
            problems.append("the caption is empty")
        media = model.effective_media(post, v)
        kinds = {a: r.get("kind") for a, r in assets.items()}
        if any(not (m.get("alt") or "").strip() for m in media if kinds.get(m["asset"], "image") == "image"):      # a video has no alt text to demand
            problems.append("a media slot has no alt text (images need it; the queue refuses without)")
        out.append({"variant": v["id"], "account": v["account"], "platform": plat, "caption": text, "problems": problems,
                    "counter": {"unit": unit, "used": used, "limit": limit}})
    return out
