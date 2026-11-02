"""The FINAL text of a studio post, per platform: caption + link + credits + hashtags, composed ONCE (the fix for "hashtags never reach the platform").

    compose(copy, platform, ...)      -> Composed(text, first_comment, hashtags, problems, counter)   pure; the single source of the final wording
    compose_variant(post, v, assets)  the same for one variant of a post (effective copy + the credit lines its licensed assets require)

`queue sync` writes `Composed.text` into the queue item's `caption` and `description` and `Composed.first_comment` into `first_comment`, so the
text the approval hash binds, the text the reviewer reads and the text the adapter sends are one string. The adapters' `compose_text` then has
nothing left to add (the link is already in the text). Conventions come from `config/platforms.yaml` `<platform>.text` (all unverified):

    hashtags: block         after a blank line, at the end (Instagram, TikTok, Threads, Facebook, LinkedIn, Pinterest, YouTube description)
    hashtags: inline        appended to the caption's last line with a space (X, Bluesky: Bluesky turns each `#tag` into a facet itself)
    hashtags_in_first_comment: true   (Instagram, Threads) the hashtags leave the caption and go below the first comment
    hashtags_max            more tags than the platform honours is a problem, never a silent cut
    unit                    chars | graphemes | x_weighted | bytes: the length is checked on the COMPOSED text with `text_metrics` (2 % margin)

Order: caption, link, credit lines (only licenses that REQUIRE attribution), hashtags. A tag already written in the caption is not repeated.
A text that does not fit is a problem (the sync refuses the variant), never a quiet truncation. Nothing here sends or approves anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from . import model, text_metrics

FIRST_COMMENT_PLATFORMS = ("instagram", "threads")        # the adapters that post `first_comment`
_BLUESKY_TAG = re.compile(r"^[^\s#\d]")                    # the adapter's facet pattern ignores a tag that starts with a digit
_JUNK = re.compile(r"[^\w]", re.UNICODE)


@dataclass
class Composed:
    text: str
    first_comment: str | None
    hashtags: list[str]                                    # normalized, without "#", de-duplicated, in order (the queue item's `tags`)
    problems: list[str] = field(default_factory=list)
    counter: dict = field(default_factory=dict)            # {"unit", "used", "limit"} for `text`


def normalize_tags(tags: Iterable[str]) -> tuple[list[str], list[str]]:
    """(tags, problems): "#Sour Dough" -> "SourDough"; empty results are reported, duplicates (any case) dropped."""
    out: list[str] = []
    problems: list[str] = []
    seen: set[str] = set()
    for raw in tags or []:
        t = _JUNK.sub("", str(raw))
        if not t:
            problems.append(f"hashtag {str(raw)!r} has no usable characters")
        elif t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out, problems


def credit_lines(assets: Mapping[str, Mapping], asset_ids: Iterable[str]) -> list[str]:
    """The exact credit lines the licenses of these assets REQUIRE (a courtesy credit is not added), first-seen order, no repeats."""
    out: list[str] = []
    from ..library.derive import closure
    order, _ = closure(assets, asset_ids)
    for aid in order:
        asset = assets.get(aid) or {}
        att = (asset.get("license") or {}).get("attribution") or {}
        text = (att.get("text") or "").strip()
        if att.get("required") and text and text not in out:
            out.append(text)
    return out


def _limit(platform: str, platforms: Mapping) -> int | None:
    c = platforms.get(platform) or {}
    if platform == "youtube":
        return c.get("description_max_bytes")
    if platform == "facebook" and c.get("description_max_chars"):
        return c["description_max_chars"]
    for block in ("post", "image"):
        v = (c.get(block) or {}).get("caption_max_chars")
        if v:
            return int(v)
    return None


def _already_in(text: str, tag: str) -> bool:
    return re.search(r"(?<![\w#])#" + re.escape(tag) + r"(?!\w)", text, re.IGNORECASE) is not None


def compose(copy: Mapping, platform: str, *, credits: Iterable[str] = (), platforms: Mapping | None = None) -> Composed:
    """`copy` is `model.effective_copy`'s dict ({caption, first_comment, hashtags, link}). `platforms` defaults to config/platforms.yaml."""
    if platforms is None:
        from ..publish.adapters import base
        platforms = base.load_platforms()
    cfg = dict((platforms.get(platform) or {}).get("text") or {})
    problems: list[str] = []
    tags, tag_problems = normalize_tags(copy.get("hashtags") or [])
    problems += tag_problems
    caption = (copy.get("caption") or "").strip()
    first = (copy.get("first_comment") or "").strip()
    link = (copy.get("link") or "").strip()
    mode = cfg.get("hashtags", "block")
    in_first = bool(cfg.get("hashtags_in_first_comment"))
    shown = [t for t in tags if not _already_in(caption, t)]
    if platform == "bluesky":
        problems += [f"hashtag #{t} starts with a digit, which Bluesky does not treat as a hashtag" for t in shown if not _BLUESKY_TAG.match(t)]
    mx = cfg.get("hashtags_max")
    if mx and len(tags) > int(mx):
        problems.append(f"{len(tags)} hashtags, {platform} honours at most {mx}: remove {len(tags) - int(mx)}")
    tagline = " ".join("#" + t for t in shown)
    if in_first and platform not in FIRST_COMMENT_PLATFORMS:
        problems.append(f"{platform}: hashtags_in_first_comment is set but this platform's adapter posts no first comment")
        in_first = False

    head = f"{caption} {tagline}".strip() if (tagline and mode == "inline" and not in_first) else caption
    body = "\n".join(p for p in (head, link if link and link not in caption else "") if p)
    cred = [c for c in credits if c and c not in body]
    if cred:
        body = "\n\n".join(x for x in (body, "\n".join(cred)) if x)
    if tagline and mode == "block" and not in_first:
        body = "\n\n".join(x for x in (body, tagline) if x)
    first_out = "\n\n".join(x for x in (first, tagline) if x) if (tagline and in_first) else first

    unit = cfg.get("unit", "chars")
    limit = _limit(platform, platforms)
    used = text_metrics.counter(body, unit)
    if limit and text_metrics.over_limit(body, unit, int(limit)):
        problems.append(f"{platform}: the final text (caption, link, credits, hashtags) is {used} {unit} (approximate), over the {limit} limit "
                        f"with a 2% margin: shorten the caption or drop hashtags")
    if limit and first_out and text_metrics.over_limit(first_out, unit, int(limit)):
        problems.append(f"{platform}: the first comment is {text_metrics.counter(first_out, unit)} {unit}, over the {limit} limit")
    return Composed(text=body, first_comment=first_out or None, hashtags=tags, problems=problems,
                    counter={"unit": unit, "used": used, "limit": int(limit) if limit else None})


def compose_variant(post: Mapping, variant: Mapping, assets: Mapping[str, Mapping], *, platforms: Mapping | None = None) -> Composed:
    ids = [m["asset"] for m in sorted(model.effective_media(post, variant), key=lambda m: m.get("slot", 0))]
    return compose(model.effective_copy(post, variant), variant["platform"], credits=credit_lines(assets, ids), platforms=platforms)
