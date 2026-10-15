"""Post copy: drafts of the words that go with the episode, written for the human to EDIT.

No model writes prose here. Every line is a template filled with facts that exist on disk (episode title, people, chapters, credits)
and with VERBATIM quotes the human already approved (ContentDoc quote cards, approved clips' hook quotes). A quote is never
shortened: if it does not fit a platform's limit, another quote is tried, then none. What we write ourselves is plain: no hype, no
exclamation marks, no emoji, no rhetorical-question bait; `critique` checks that against `config/package.default.yaml` (the
critic checklist of the content skills) and records what it finds next to the text instead of silently rewriting.

A "block" is one piece of copy: `{id, platform, kind, label, text, chars, limit, unit, problems, ref}`. `copy/copy.json` holds
all blocks (the machine-readable, editable source `ave package queue` reads); the per-platform `.md` files are the readable view.
"""

from __future__ import annotations

import re
from collections import Counter

from . import chapters as C

PLACEHOLDER = re.compile(r"\{\{[A-Z_]+\}\}|TODO:[^\n]*")
_EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF\U0001F900-\U0001F9FF⭐⭕⌚⌛️]")
_QUOTED = re.compile("“[^”]*”")
# which export platform `ave shorts export` names -> the platform whose copy limits apply
EXPORT_TO_COPY = {"youtube": "youtube", "facebook": "facebook", "reels": "instagram", "instagram": "instagram", "x": "x", "tiktok": "tiktok"}

# order of the parts of the episode post per platform, and which are dropped first when the text is over the limit
PLAN = {
    "x":         {"parts": ["quote", "title", "url", "tags"], "drop": ["tags"]},
    "bluesky":   {"parts": ["quote", "title", "url", "tags"], "drop": ["tags", "title"]},
    "threads":   {"parts": ["quote", "title", "url", "tags"], "drop": ["tags", "title"]},
    "instagram": {"parts": ["quote", "title_people", "url", "tags"], "drop": ["tags", "title_people"]},
    "facebook":  {"parts": ["quote", "title_people", "url", "tags"], "drop": ["tags", "title_people"]},
    "linkedin":  {"parts": ["quote", "title_people", "url", "tags"], "drop": ["tags", "title_people"]},
    "tiktok":    {"parts": ["quote", "title", "tags"], "drop": ["tags", "title"]},
}


# ---- facts -------------------------------------------------------------------------------------

def hashtag(s: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", s)
    return "#" + "".join(w[:1].upper() + w[1:] for w in words) if words else ""


def keywords(texts: list[str], n: int = 6) -> list[str]:
    bag = Counter(w for t in texts for w in C.words_of(t) if len(w) >= 5)
    return [w for w, _ in sorted(bag.items(), key=lambda x: (-x[1], x[0]))[:n]]


def join_names(names: list[str]) -> str:
    return names[0] if len(names) == 1 else (", ".join(names[:-1]) + " and " + names[-1] if names else "")


def make_facts(meta: dict, quotes: list[dict], clips: list[dict], chapters: list[dict], credits: list[str]) -> dict:
    people = [s["name"] for s in (meta.get("speakers") or {}).values() if isinstance(s, dict) and s.get("name")]
    title = meta.get("title") or ""
    show = meta.get("show") or None
    texts = [q["text"] for q in quotes] + [c.get("title", "") for c in clips]       # what was actually said, not our own chapter labels
    return {"show": show, "title": title, "people": people, "quotes": quotes, "chapters": chapters, "credits": credits,
            "keywords": keywords(texts), "show_tag": hashtag(show) if show else ""}


def tags_for(facts: dict, platform: str, policy: dict) -> list[str]:
    cap = policy["copy"]["max_hashtags"].get(platform, 1)
    out = [facts["show_tag"]] if facts.get("show_tag") else []
    out += [hashtag(k) for k in facts["keywords"]]
    return [t for t in dict.fromkeys(out) if t][:cap]


# ---- limits ------------------------------------------------------------------------------------

def limit_for(platform: str, cfg: dict, kind: str = "post") -> tuple[int | None, str]:
    """(limit, unit) for a kind of copy on a platform, from config/platforms.yaml. None = no configured limit."""
    c = cfg.get(platform) or {}
    if platform == "youtube":
        return {"title": (c.get("title_max_chars"), "chars"), "description": (c.get("description_max_bytes"), "bytes"),
                "tags": (c.get("tags_max_chars"), "chars"), "post": (c.get("description_max_bytes"), "bytes")}[kind]
    if platform == "facebook":
        return c.get("description_max_chars"), "chars"
    for block in ("post", "image"):
        v = (c.get(block) or {}).get("caption_max_chars")
        if v:
            return v, "chars"
    return None, "chars"


def measure(text: str, unit: str, policy: dict) -> int:
    t = text.replace(policy["copy"]["url_placeholder"], "x" * policy["copy"]["url_chars"])
    return len(t.encode("utf-8")) if unit == "bytes" else len(t)


# ---- the critic --------------------------------------------------------------------------------

def critique(text: str, platform: str, policy: dict) -> list[str]:
    """Voice and form problems in the words WE wrote (text inside “...” is someone's verbatim quote and is not judged)."""
    cp = policy["copy"]
    mine = _QUOTED.sub(" ", text)
    low = mine.lower()
    out = []
    for ph in cp["banned_phrases"]:
        if re.search(r"(?<![a-z])" + re.escape(ph.lower()) + r"(?![a-z])", low):
            out.append(f"corporate/hype phrase: {ph!r}")
    if mine.count("!") > cp["max_exclamations"]:
        out.append("exclamation marks")
    if not cp["allow_emoji"] and _EMOJI.search(mine):
        out.append("emoji")
    caps = [w for w in re.findall(r"\b[A-Z]{4,}\b", PLACEHOLDER.sub("", mine))]
    if len(caps) > cp["max_allcaps_words"]:
        out.append(f"words in capitals: {', '.join(caps[:3])}")
    n_tags = len(re.findall(r"(?<!\w)#\w+", text))
    if n_tags > cp["max_hashtags"].get(platform, 3):
        out.append(f"{n_tags} hashtags (max {cp['max_hashtags'].get(platform, 3)} on {platform})")
    if "?" in re.sub(r"https?://\S+", "", mine):
        out.append("a question in our own words (reads as bait; state it plainly)")
    return out


# ---- composing ---------------------------------------------------------------------------------

def _part(name: str, facts: dict, quote: dict | None, tags: list[str], policy: dict) -> str:
    url = policy["copy"]["url_placeholder"]
    if name == "quote":
        if not quote:
            return ""
        return f"“{quote['text']}”" + (f"\n— {quote['speaker']}" if quote.get("speaker") else "")
    if name == "title":
        return facts["title"]
    if name == "title_people":
        who = join_names(facts["people"])
        return facts["title"] + (f"\nWith {who}." if who else "")
    if name == "url":
        return f"Full episode: {url}"
    if name == "tags":
        return " ".join(tags)
    return ""


def compose_post(platform: str, facts: dict, cfg: dict, policy: dict, quotes: list[dict] | None = None) -> dict:
    """The episode post for one platform. Tries each quote in order with progressively fewer optional parts; the first that fits
    the limit wins. Returns a block (without id/ref)."""
    plan = PLAN[platform]
    limit, unit = limit_for(platform, cfg)
    tags = tags_for(facts, platform, policy)
    pool = list(facts["quotes"] if quotes is None else quotes)
    attempts = [(q, plan["drop"][:k]) for q in pool for k in range(len(plan["drop"]) + 1)] + \
               [(None, plan["drop"][:k]) for k in range(len(plan["drop"]) + 1)]
    best = None
    for q, dropped in attempts:
        parts = [p for p in plan["parts"] if p not in dropped]
        segs = [_part(p, facts, q, tags, policy) for p in parts]
        text = "\n\n".join(s for s in segs if s).strip()
        best = best or (text, q, dropped)
        if text and (limit is None or measure(text, unit, policy) <= limit):
            best = (text, q, dropped)
            break
    # (when nothing fits, `best` is still the fullest version; the over-limit problem is recorded below)
    text, q, dropped = best
    chars = measure(text, unit, policy)
    problems = critique(text, platform, policy)
    if limit is not None and chars > limit:
        problems.append(f"over the {platform} limit: {chars} of {limit} {unit}")
    if pool and q is None:
        problems.append("no approved quote fits this platform's limit, so the post carries none")
    if not pool:
        problems.append("no approved quote to use yet (approve a quote card or a clip with a hook quote)")
    return {"platform": platform, "kind": "episode_post", "label": f"{platform}: episode post", "text": text, "chars": chars, "limit": limit,
            "unit": unit, "dropped": dropped, "problems": problems, "quote": q["text"] if q else None}


def compose_youtube(facts: dict, cfg: dict, policy: dict, chapter_block: str, chapter_problems: list[str]) -> list[dict]:
    """YouTube: title (+ options), description with chapters and credits, tags."""
    title_lim, _ = limit_for("youtube", cfg, "title")
    desc_lim, _ = limit_for("youtube", cfg, "description")
    tags_lim, _ = limit_for("youtube", cfg, "tags")
    q = facts["quotes"][0] if facts["quotes"] else None
    options = [facts["title"]]
    if q:
        alt = f"“{q['text']}” | {facts['title']}"
        if title_lim is None or len(alt) <= title_lim:
            options.append(alt)
    blocks = [{"platform": "youtube", "kind": "youtube_title", "label": "YouTube: title", "text": options[0], "limit": title_lim, "unit": "chars",
               "options": options}]
    who = join_names(facts["people"])
    parts = []
    if q:
        parts.append(f"“{q['text']}”" + (f"\n— {q['speaker']}" if q.get("speaker") else ""))
    parts.append(facts["title"] + (f"\nWith {who}." if who else ""))
    parts.append("TODO: one or two plain sentences, in your own words, about what this conversation is.")
    if chapter_block:
        parts.append("Chapters\n" + chapter_block)
    if facts["credits"]:
        parts.append("Credits\n" + "\n".join(facts["credits"]))
    parts.append("Links: " + "{{LINKS}}")
    blocks.append({"platform": "youtube", "kind": "youtube_description", "label": "YouTube: description", "text": "\n\n".join(parts),
                   "limit": desc_lim, "unit": "bytes", "extra_problems": list(chapter_problems)})
    tag_words = list(dict.fromkeys([t for t in [facts["show"]] + facts["people"] + facts["keywords"] if t]))
    kept, total = [], 0
    for t in tag_words:
        add = len(t) + (1 if kept else 0)
        if tags_lim is None or total + add <= tags_lim:
            kept.append(t)
            total += add
    blocks.append({"platform": "youtube", "kind": "youtube_tags", "label": "YouTube: tags", "text": ", ".join(kept), "limit": tags_lim, "unit": "chars",
                   "tags": kept})
    for b in blocks:
        b["chars"] = measure(b["text"], b["unit"], policy)
        pr = list(b.pop("extra_problems", []))
        pr += critique(b["text"], "youtube", policy) if b["kind"] != "youtube_tags" else []
        if b["limit"] is not None and b["chars"] > b["limit"]:
            pr.append(f"over the youtube limit: {b['chars']} of {b['limit']} {b['unit']}")
        b["problems"] = pr
    return blocks


def compose_short(platform_export: str, clip: dict, sidecar: dict, facts: dict, cfg: dict, policy: dict) -> dict:
    """Copy for one short on one export platform: title and a caption that is the clip's own hook, in quotes."""
    cp = EXPORT_TO_COPY.get(platform_export, platform_export)
    limit, unit = limit_for(cp, cfg)
    title = (sidecar.get("title") or clip.get("title") or clip.get("slug") or "").strip()
    hook = (sidecar.get("hook") or clip.get("hook") or "").strip()
    tags = tags_for(facts, cp, policy)
    if cp == "youtube":
        tags = tags[:max(policy["copy"]["max_hashtags"]["youtube"] - 1, 0)]       # #Shorts takes one of the slots
    caption = "\n\n".join(p for p in [f"“{hook}”" if hook else "", " ".join(tags)] if p)
    if cp == "youtube" and "#Shorts" not in caption:
        caption = (caption + "\n\n#Shorts").strip()
    chars = measure(caption, unit, policy)
    problems = critique(caption, cp, policy) + critique(title, cp, policy)
    if limit is not None and chars > limit:
        problems.append(f"over the {cp} limit: {chars} of {limit} {unit}")
    t_lim, _ = limit_for("youtube", cfg, "title")
    if cp == "youtube" and t_lim and len(title) > t_lim:
        problems.append(f"title is {len(title)} characters, youtube max {t_lim}")
    return {"platform": cp, "kind": "short_post", "label": f"{cp}: short “{title}”", "title": title, "text": caption, "chars": chars, "limit": limit,
            "unit": unit, "problems": problems, "export_platform": platform_export}


def compose_image(platform: str, item_caption: str, quote: dict | None, facts: dict, cfg: dict, policy: dict, label: str) -> dict:
    """Copy for a quote card / carousel: the approved caption (the words, who said them), then the link, then at most a tag or two."""
    limit, unit = limit_for(platform, cfg)
    tags = tags_for(facts, platform, policy)
    base = item_caption.strip()
    full = "\n\n".join(p for p in [base, f"Full episode: {policy['copy']['url_placeholder']}", " ".join(tags)] if p)
    short = "\n\n".join(p for p in [base, f"Full episode: {policy['copy']['url_placeholder']}"] if p)
    text = full
    dropped = []
    if limit is not None and measure(text, unit, policy) > limit:
        text, dropped = short, ["tags"]
    if limit is not None and measure(text, unit, policy) > limit:
        text, dropped = base, ["tags", "url"]
    chars = measure(text, unit, policy)
    problems = critique(text, platform, policy)
    if limit is not None and chars > limit:
        problems.append(f"over the {platform} limit: {chars} of {limit} {unit}")
    return {"platform": platform, "kind": "image_post", "label": label, "text": text, "chars": chars, "limit": limit, "unit": unit,
            "dropped": dropped, "problems": problems}


def placeholders(blocks: list[dict]) -> list[str]:
    seen = []
    for b in blocks:
        for m in PLACEHOLDER.findall(b["text"]):
            m = "TODO" if m.startswith("TODO") else m
            if m not in seen:
                seen.append(m)
    return seen


def markdown(platform: str, blocks: list[dict], header: str) -> str:
    out = [f"# {platform}: post copy (DRAFT)", "", header, ""]
    for b in blocks:
        lim = f"{b['chars']} of {b['limit']} {b['unit']}" if b.get("limit") else f"{b['chars']} {b['unit']} (no limit configured)"
        out += [f"## {b['label']}", "", f"_{lim}_", ""]
        if b.get("options") and len(b["options"]) > 1:
            out += ["Other title options:", ""] + [f"- {o}" for o in b["options"][1:]] + [""]
        out += ["```text", b["text"], "```", ""]
        if b.get("problems"):
            out += ["Check before posting:", ""] + [f"- {p}" for p in b["problems"]] + [""]
    return "\n".join(out)
