"""`ave studio demo [DIR]`: a fully synthetic, populated studio you can open in the UI with no accounts and no network.

Two fictional brands share one studio: **The Long Table** (a podcast about the people who feed us) and **Loaf & Ladle** (a small bakery).
Nothing here is real: the people, the prices, the pictures (procedural illustrations from `demo_art`), the "stock" provider and its
licenses are all made up, and every URL ends in `.invalid`.

What it builds, through the same code paths the CLI uses (no hand-written files for anything that has a function):
  * 7 accounts (Instagram x2, Facebook Page, Bluesky, LinkedIn, X, TikTok), every one in dry-run: no credentials, no live attestation;
  * schedule rules per account, a library of own photos, licensed "stock" B-roll (stills and short clips) with real license-log entries,
    and cards rendered by the content renderer (quote, announcement, event, stat, offer, testimonial, product, before/after, carousels);
  * about 40 posts over the last two and next two weeks in every status (idea, draft, in review, needs rework, approved, scheduled,
    published, failed, missed, uncertain, dropped), open feedback, alerts, two campaigns and two ghost proposals.

Nothing can post: no account is live, no adapter has credentials, and the sender is never run. The approvals that exist are signed with
YOUR approval key (the one `ave studio approve` uses), recorded `by: "demo"`, so the UI shows them as valid; they bind synthetic files.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from ..episode import read_json, write_json
from . import campaigns, demo_art, ledger, model, store
from .clock import SystemClock, iso_utc
from .events import AGENT, HUMAN, Actor
from .project import StudioProject

TZ = "America/Denver"
BY = "demo"                      # who the synthetic approvals and library additions are recorded as
TITLE = "The Long Table and Loaf & Ladle (demo)"


class DemoHost:
    """A MediaHost stand-in for preflight (Instagram and Threads need a public URL). It records nothing and never touches a network."""

    def put(self, path, *, ttl_s: int = 3600) -> dict:
        raise RuntimeError("the demo has no media host: nothing is ever uploaded")

    def delete(self, key: str) -> None:
        return None


class DemoError(ValueError):
    """The demo cannot be built here (the folder is not empty, a tool is missing)."""


# ------------------------------------------------------------------------------------------------ accounts and rules

ACCOUNTS = [
    {"id": "ig-longtable", "brand": "The Long Table", "platform": "instagram", "mode": "graph", "label": "@thelongtable.fm", "post_types": ["feed", "carousel", "story"],
     "credentials": {"ref": "ig-longtable", "ig_user_id": "17840000000000001"}, "limits": {"max_per_day": 2, "min_gap_min": 180}},
    {"id": "bsky-longtable", "brand": "The Long Table", "platform": "bluesky", "mode": "direct", "label": "thelongtable.bsky.social", "post_types": ["feed", "text"],
     "credentials": {"ref": "bsky-longtable", "handle": "thelongtable.bsky.social"}, "limits": {"max_per_day": 4, "min_gap_min": 60}},
    {"id": "li-longtable", "brand": "The Long Table", "platform": "linkedin", "mode": "graph", "label": "The Long Table (company page)", "post_types": ["feed", "carousel", "text"],
     "credentials": {"ref": "li-longtable"},
     # Fictional preflight data only: no provider verification, token or live attestation.
     "connection": {"product": "Fictional demo organization image grant", "source": "synthetic offline demo; not provider-verified", "scopes": ["w_organization_social"]}, "limits": {"max_per_day": 2, "min_gap_min": 240}},
    {"id": "x-longtable", "brand": "The Long Table", "platform": "x", "mode": "direct", "label": "@longtablefm", "post_types": ["feed", "text"],
     "credentials": {"ref": "x-longtable"}, "limits": {"max_per_day": 3, "min_gap_min": 90}},
    {"id": "tt-longtable", "brand": "The Long Table", "platform": "tiktok", "mode": "inbox", "label": "@thelongtablefm", "post_types": ["carousel", "feed"],
     "credentials": {"ref": "tt-longtable"}, "limits": {"max_per_day": 1, "min_gap_min": 360}},
    {"id": "ig-loaf", "brand": "Loaf & Ladle", "platform": "instagram", "mode": "graph", "label": "@loafandladle", "post_types": ["feed", "carousel", "story"],
     "credentials": {"ref": "ig-loaf", "ig_user_id": "17840000000000002"}, "limits": {"max_per_day": 2, "min_gap_min": 180}},
    {"id": "fb-loaf", "brand": "Loaf & Ladle", "platform": "facebook", "mode": "graph", "label": "Loaf & Ladle (Page)", "post_types": ["feed", "carousel"],
     "credentials": {"ref": "fb-loaf", "page_id": "100000000000001"}, "limits": {"max_per_day": 2, "min_gap_min": 180}},
]

RULES = {
    "ig-longtable": [("weekday-morning", ["mon", "wed", "fri"], "09:00", None), ("sunday-carousel", ["sun"], "17:00", ["carousel"])],
    "bsky-longtable": [("weekday", ["mon", "tue", "wed", "thu", "fri"], "10:30", None)],
    "li-longtable": [("tue-thu", ["tue", "thu"], "08:30", None)],
    "x-longtable": [("mon-wed-fri", ["mon", "wed", "fri"], "11:00", None)],
    "tt-longtable": [("evening", ["wed", "sat"], "18:00", None)],
    "ig-loaf": [("early-bake", ["tue", "thu", "sat"], "07:30", None)],
    "fb-loaf": [("morning", ["tue", "thu", "sat"], "08:00", None), ("friday-pickup", ["fri"], "16:00", None)],
}


def _write_accounts_and_rules(sp: StudioProject) -> None:
    from ..platforms import accounts as pa
    for a in ACCOUNTS:
        pa.save_account(sp, {**a, "tz": TZ, "catch_up": False})
    sp.schedule_dir.mkdir(parents=True, exist_ok=True)
    for acct, rules in RULES.items():
        body: dict[str, Any] = {"tz": TZ, "rules": []}
        for rid, dow, at, types in rules:
            r: dict[str, Any] = {"id": rid, "dow": dow, "time": at}
            if types:
                r["post_types"] = types
            body["rules"].append(r)
        model.require_valid("schedule_rules", body)
        (sp.schedule_dir / f"{acct}.yaml").write_text(yaml.safe_dump(body, sort_keys=False))


# ------------------------------------------------------------------------------------------------ the library

STOCK = [
    ("wheat_dawn", "Wheat field at dawn, low sun over rolling hills", ["b-roll", "field", "wheat", "sunrise"], "ds-1042", None),
    ("steam", "Steam rising in a dark kitchen, warm lights out of focus", ["b-roll", "kitchen", "steam"], "ds-2217", "Footage: Sam Okoye / Demo Stock"),
    ("river_stones", "Smooth river stones under clear moving water", ["b-roll", "water", "stones"], "ds-3310", None),
    ("awning", "A striped market awning above crates of produce", ["b-roll", "market", "produce"], "ds-4408", "Photo: Lin Mercado / Demo Stock"),
    ("rooftops", "City rooftops at dusk, windows lighting up", ["b-roll", "city", "dusk"], "ds-5521", None),
    ("flour_cloud", "A cloud of flour hanging in dark air", ["b-roll", "flour", "baking"], "ds-6633", None),
]
CLIPS = [
    ("clip-embers", "Slow warm light drifting, an abstract kitchen glow", ["b-roll", "abstract", "warm"], "dv-7001", "0x2b1d14", "0xe9a96b", "0x6b3a1f", 3),
    ("clip-dusk", "Dusk colors sliding across the frame", ["b-roll", "abstract", "dusk"], "dv-7002", "0x1d2a4a", "0xe58a6b", "0x3a2147", 11),
    ("clip-field", "Green and gold field colors in slow motion", ["b-roll", "abstract", "field"], "dv-7003", "0x23311d", "0xd8c46a", "0x6b8a3a", 5),
]
LICENSE_URL = "https://demo.invalid/licenses/demo-stock"


def _stock_entry(provider_id: str, kind: str, attribution: str | None) -> dict:
    return {"source": "demo-stock", "url": f"https://demo.invalid/stock/{provider_id}", "license": "Demo Stock License (synthetic)",
            "license_url": LICENSE_URL, "license_text": f"Synthetic {kind} generated by `ave studio demo`. Free to use inside the demo; there is no real "
            "provider, photographer or license behind it.", "attribution": {"required": bool(attribution), "text": attribution}}


def _make_clip(path: Path, c0: str, c1: str, c2: str, seed: int) -> bool:
    ff = shutil.which("ffmpeg")
    if not ff:
        return False
    cmd = [ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
           f"gradients=s=1280x720:d=3:r=24:speed=0.04:c0={c0}:c1={c1}:c2={c2}:nb_colors=3:seed={seed}",
           "-c:v", "libx264", "-preset", "ultrafast", "-crf", "27", "-pix_fmt", "yuv420p", "-threads", "1", "-an", "-map_metadata", "-1",
           "-fflags", "+bitexact", "-flags:v", "+bitexact", str(path)]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=60).returncode == 0 and path.is_file()
    except (OSError, subprocess.SubprocessError):
        return False


OWN_PHOTOS = {
    "loaf": ("A country sourdough boule with a scored ear on a linen runner", ["bread", "sourdough", "product"]),
    "rye": ("A long rye loaf with caraway seeds on dark wood", ["bread", "rye", "product"]),
    "bun": ("Three cardamom morning buns with pearl sugar on a pale board", ["pastry", "bun", "product"]),
    "soup": ("A white bowl of roasted onion soup seen from above", ["soup", "product"]),
    "jar_before": ("A sourdough starter jar on day one, barely risen", ["starter", "process"]),
    "jar_after": ("The same starter jar on day five, doubled and bubbly", ["starter", "process"]),
    "table": ("A long candlelit table set with plates and bread", ["table", "show", "event"]),
    "mic": ("A studio microphone against a dark wall, warm rim light", ["mic", "show", "podcast"]),
}


def _build_library(sp: StudioProject, work: Path, now: datetime) -> dict:
    from .. import library
    out: dict[str, Any] = {"photos": {}, "stock": {}, "clips": {}, "art": None}
    gen = work / "demo" / "gen"
    gen.mkdir(parents=True, exist_ok=True)
    when = now - timedelta(days=40)
    for key, (alt, tags) in OWN_PHOTOS.items():
        p = demo_art.save_jpeg(demo_art.PHOTOS[key](), gen / f"{key}.jpg")
        owner = "Loaf & Ladle" if key in ("loaf", "rye", "bun", "soup", "jar_before", "jar_after") else "The Long Table"
        out["photos"][key] = library.add_file(sp, p, source={"type": "own"}, by=BY, alt=alt, tags=tags + ["demo", owner.lower().replace(" & ", "-").replace(" ", "-")],
                                              now=when)
    for key, alt, tags, pid, attribution in STOCK:
        p = demo_art.save_jpeg(demo_art.STOCK[key](), gen / f"{key}.jpg")
        out["stock"][key] = library.add_file(
            sp, p, source={"type": "stock", "provider": "demo-stock", "asset_id": pid, "url": f"https://demo.invalid/stock/{pid}"},
            license_entry=_stock_entry(pid, "photograph", attribution), by=BY, alt=alt, tags=tags + ["stock", "demo"], now=when + timedelta(days=3))
    for key, alt, tags, pid, c0, c1, c2, seed in CLIPS:
        p = gen / f"{key}.mp4"
        if _make_clip(p, c0, c1, c2, seed):
            out["clips"][key] = library.add_file(
                sp, p, source={"type": "stock", "provider": "demo-stock", "asset_id": pid, "url": f"https://demo.invalid/stock/{pid}"},
                license_entry=_stock_entry(pid, "clip", None), by=BY, alt=alt, tags=tags + ["stock", "video", "demo"], now=when + timedelta(days=5))
    p = demo_art.save_jpeg(demo_art.STOCK["episode_art"](), gen / "episode_art.jpg")
    out["art"] = library.add_file(
        sp, p, source={"type": "generated", "provider": "demo-generator", "url": "https://demo.invalid/generated/1"},
        license_entry={"source": "demo-generator", "url": "https://demo.invalid/generated/1", "license": "Generated for the demo (synthetic)",
                       "license_url": "https://demo.invalid/licenses/generated", "license_text": "Marked AI-generated so the disclosure flag has something to show; "
                       "nothing generative was used.", "attribution": {"required": False, "text": None}},
        by=BY, alt="Abstract warm gradient with a row of thin circles, Season 4 cover art", tags=["art", "season-4", "demo", "ai"], ai_generated=True, now=when + timedelta(days=6))
    return out


# ------------------------------------------------------------------------------------------------ the posts

@dataclass
class V:
    """One variant. `status` is the status the demo drives it to (a derived status: nothing here writes a status)."""
    account: str
    type: str
    status: str                       # published failed missed uncertain scheduled approved in_review rework draft dropped
    caption: str
    first_comment: str | None = None
    hashtags: list[str] | None = None


@dataclass
class P:
    key: str
    brand: str
    card: dict | None              # {"t": template, "s": style, "f": format, "slots": {...}}
    variants: list[V] = field(default_factory=list)
    age: float = 3.0               # days since it was created
    idea: str | None = None
    origin: dict | None = None
    reason: str = ""
    confidence: float | None = None
    base: str = ""
    campaign: str | None = None


LT, LL = "longtable", "loaf"
SHOW, BIZ = "The Long Table", "Loaf & Ladle"
HASH_LT = ["thelongtable", "podcast", "foodpeople"]
HASH_LL = ["loafandladle", "sourdough", "smallbakery"]


def _quote(q: str, who: str, role: str, style: str, fmt: str = "4x5", show: str = SHOW) -> dict:
    return {"t": "quote-post", "s": style, "f": fmt, "slots": {"quote": q, "attribution": who, "title": role, "show": show}}


def _spot(photo: str, title: str, sub: str, detail: str, style: str, fmt: str = "4x5", focus: float = 0.5) -> dict:
    return {"t": "product-spotlight", "s": style, "f": fmt, "photo": {"image": photo}, "slots": {"title": title, "sub": sub, "detail": detail, "show": BIZ,
                                                                                                "image_focus_x": focus}}


def posts_spec() -> list[P]:
    c1, c2 = "c_season-4-launch", "c_thanksgiving-loaf-box"
    return [
        # ---- The Long Table: published in the last two weeks
        P("lt_quote_table", LT, _quote("Nobody remembers the meal. They remember who was at the table.", "Maren Voss", "Host", "paper"), age=13, campaign=c1, variants=[
            V("ig-longtable", "feed", "published", "Nobody remembers the meal. They remember who was at the table.\n\nThat line has been on a sticky note above our mic since episode one.",
              hashtags=HASH_LT),
            V("bsky-longtable", "feed", "published", "\"Nobody remembers the meal. They remember who was at the table.\" Maren, on why we record at one."),
            V("x-longtable", "feed", "published", "\"Nobody remembers the meal. They remember who was at the table.\" Maren Voss, on why we record at one.")]),
        P("lt_ep41_announce", LT, {"t": "announcement", "s": "ink", "f": "4x5", "slots": {"kicker": "New episode", "title": "Flour, in the Dark",
                                                                                      "sub": "Ines Calder on milling at 3 a.m.", "cta": "Listen wherever you get podcasts", "show": SHOW}},
          age=11, campaign=c1, variants=[
            V("ig-longtable", "feed", "published", "Episode 41 is out. Ines Calder mills grain in the middle of the night so the bakery down the road has flour by morning. We talk about the quiet, the dust and why she will not hurry wheat.\n\nLink in bio.",
              first_comment="Transcript and notes are on the episode page.", hashtags=HASH_LT),
            V("li-longtable", "feed", "failed", "New episode: Flour, in the Dark. Ines Calder mills grain overnight so a neighborhood can have bread by morning. We talk about the quiet, the dust and why good flour cannot be hurried."),
            V("x-longtable", "feed", "published", "Episode 41: Flour, in the Dark. Ines Calder on milling at 3 a.m. Listen wherever you get podcasts."),
            V("bsky-longtable", "feed", "published", "Episode 41 is out: Flour, in the Dark. Ines Calder on milling at 3 a.m. and why good flour cannot be hurried.")]),
        P("lt_stat_212", LT, {"t": "stat-card", "s": "paper", "f": "1x1", "slots": {"stat": "212", "title": "episodes recorded around one very long table",
                                                                                   "source": "Our own episode log", "kicker": "Milestone", "show": SHOW}},
          age=9, variants=[
            V("ig-longtable", "feed", "published", "Two hundred and twelve episodes. Thank you for pulling up a chair.", hashtags=HASH_LT),
            V("li-longtable", "feed", "published", "212 episodes in, a small thank-you to everyone who has listened, written in or sent us a recipe. The table is still open.")]),
        P("lt_tiktok_hospitality", LT, _quote("Hospitality is a decision you make before anyone arrives.", "Maren Voss", "Host", "ink"), age=8, variants=[
            V("tt-longtable", "carousel", "missed", "Hospitality is a decision you make before anyone arrives. #foodtok #podcast"),
            V("ig-longtable", "feed", "published", "Hospitality is a decision you make before anyone arrives.", hashtags=HASH_LT)]),
        P("lt_x_transcript", LT, {"t": "announcement", "s": "paper", "f": "1x1", "slots": {"kicker": "Transcripts", "title": "Every word about flour, searchable",
                                                                                         "sub": "Episode 41 transcript is up.", "show": SHOW}},
          age=6, variants=[
            V("x-longtable", "feed", "uncertain", "The transcript for episode 41 is up. Search every word we said about flour."),
            V("bsky-longtable", "feed", "published", "The transcript for episode 41 is up. Search every word we said about flour.")]),
        # ---- scheduled / in review
        P("lt_carousel_miller", LT, {"t": "tip-list", "s": "clay", "f": "4x5", "slots": {
            "show": SHOW, "cover": {"title": "Five things a miller taught us about patience", "subtitle": "From episode 41 with Ines Calder"},
            "slides": [{"kind": "point", "label": "1", "text": "Grain wants rest before it is ground. Skip it and the flour tastes thin."},
                       {"kind": "point", "label": "2", "text": "Slow stones run cool. Heat is the enemy of flavor."},
                       {"kind": "point", "label": "3", "text": "Sift less than you think. The bran is where the story is."},
                       {"kind": "point", "label": "4", "text": "A good loaf starts weeks before the oven."},
                       {"kind": "point", "label": "5", "text": "Do the quiet work at the quiet hour."}],
            "closing": {"line": "Hear the whole conversation", "cta": "Episode 41, link in bio"}}}, age=5, campaign=c1, variants=[
            V("ig-longtable", "carousel", "scheduled", "Five things a miller taught us about patience. Swipe through, then hear the whole thing in episode 41.", hashtags=HASH_LT),
            V("li-longtable", "carousel", "scheduled", "Ines Calder has milled grain at 3 a.m. for twenty years. Five lessons from our conversation, in the order she gave them."),
            V("tt-longtable", "carousel", "in_review", "Five things a miller taught us about patience #bread #flour #podcast")]),
        P("lt_quote_ines", LT, _quote("Good flour is wheat that was left alone long enough.", "Ines Calder", "Miller, Granary Mills", "clay", "1x1"), age=5, campaign=c1, variants=[
            V("ig-longtable", "feed", "scheduled", "Good flour is wheat that was left alone long enough. Ines Calder, episode 41.", hashtags=HASH_LT),
            V("x-longtable", "feed", "scheduled", "\"Good flour is wheat that was left alone long enough.\" Ines Calder, episode 41."),
            V("bsky-longtable", "feed", "scheduled", "\"Good flour is wheat that was left alone long enough.\" Ines Calder, episode 41.")]),
        P("lt_event_live", LT, {"t": "event", "s": "ink", "f": "4x5", "slots": {"kicker": "Supper club live", "title": "A long table, live at The Granary", "when": "Fri Nov 14",
                                                                                "time": "Doors 6, tape at 7", "place": "The Granary, Denver", "cta": "Tickets at the link", "show": SHOW}},
          age=4, campaign=c1, variants=[
            V("ig-longtable", "feed", "approved", "We are taping a live episode at The Granary on Friday, November 14. Come eat with us. Tickets at the link in bio.", hashtags=HASH_LT),
            V("li-longtable", "feed", "in_review", "We are recording a live episode of The Long Table at The Granary in Denver on November 14. Doors at 6, tape at 7. Tickets are limited."),
            V("x-longtable", "feed", "scheduled", "Live at The Granary, Fri Nov 14. Doors 6, tape at 7. Come eat with us. Tickets in the link."),
            V("bsky-longtable", "feed", "in_review", "We are taping live at The Granary in Denver, Fri Nov 14. Doors at 6, tape at 7.")]),
        P("lt_quote_joss", LT, _quote("A dishwasher hears every argument a kitchen has.", "Joss Whitaker", "Chef, former dishwasher", "ink"), age=3, variants=[
            V("ig-longtable", "feed", "rework", "A dishwasher hears every argument a kitchen has. Joss Whitaker, episode 42.", hashtags=HASH_LT),
            V("x-longtable", "feed", "scheduled", "\"A dishwasher hears every argument a kitchen has.\" Joss Whitaker, episode 42.")]),
        P("lt_season_link", LT, {"t": "announcement", "s": "paper", "f": "191x100", "slots": {"title": "Season 4 starts Nov 3", "sub": "New episodes every Monday.", "show": SHOW}},
          age=7, campaign=c1, variants=[
            V("li-longtable", "feed", "scheduled", "Season 4 of The Long Table starts November 3, with new episodes every Monday. First guest: a miller who works at night."),
            V("x-longtable", "feed", "scheduled", "Season 4 starts Nov 3. New episodes every Monday.")]),
        P("lt_story_second", LT, _quote("Say yes to the second helping.", "Teodor Hale", "Co-host", "paper", "9x16"), age=2, base="Story: say yes to the second helping.", variants=[
            V("ig-longtable", "story", "in_review", "")]),
        P("lt_art_s4", LT, {"t": "product-spotlight", "s": "ink", "f": "1x1", "photo": {"image": "art"},
                            "slots": {"title": "Season 4 artwork", "sub": "A year of long tables, in one picture.", "detail": "Cover art, 2026", "show": SHOW}},
          age=2, variants=[V("ig-longtable", "feed", "in_review", "The Season 4 cover. This one was made with an image generator; the disclosure flag on this post says so.", hashtags=HASH_LT)]),
        P("lt_testimonial", LT, {"t": "testimonial", "s": "clay", "f": "4x5", "slots": {"quote": "I listen on my commute and always end up circling the block to hear the end.",
                                                                                         "attribution": "A listener in Boulder", "title": "Review, Apple Podcasts", "show": SHOW}},
          age=2, variants=[
            V("ig-longtable", "feed", "approved", "Our favorite kind of review. Thank you, Boulder.", hashtags=HASH_LT),
            V("li-longtable", "feed", "draft", "A listener review that made our week.")]),
        P("lt_stat_salt", LT, {"t": "stat-card", "s": "ink", "f": "4x5", "slots": {"stat": "41 weeks", "title": "the longest we have ever argued about salt",
                                                                                  "source": "Teodor's calendar", "kicker": "Season 3", "show": SHOW}},
          age=1, variants=[
            V("bsky-longtable", "feed", "scheduled", "41 weeks: the longest we have ever argued about salt."),
            V("x-longtable", "feed", "in_review", "41 weeks. The longest we have ever argued about salt.")]),
        P("lt_dup_quote", LT, _quote("Nobody remembers the meal. They remember who was at the table.", "Maren Voss", "Host", "clay", "1x1"), age=12, variants=[
            V("x-longtable", "feed", "dropped", "Duplicate of the paper card. Dropped."),
            V("li-longtable", "feed", "scheduled", "Nobody remembers the meal. They remember who was at the table. Maren Voss, host of The Long Table.")]),
        P("lt_quote_teodor", LT, _quote("The best seat is the one next to someone who disagrees with you.", "Teodor Hale", "Co-host", "paper", "1x1"), age=14, variants=[
            V("ig-longtable", "feed", "published", "The best seat is the one next to someone who disagrees with you. Teodor, on why we never plan the seating.", hashtags=HASH_LT),
            V("bsky-longtable", "feed", "published", "\"The best seat is the one next to someone who disagrees with you.\" Teodor Hale.")]),
        P("lt_guest_joss", LT, {"t": "announcement", "s": "clay", "f": "1x1", "slots": {"kicker": "Episode 42", "title": "What the dishwasher knows", "sub": "Joss Whitaker, chef and former dishwasher.", "show": SHOW}},
          age=2, variants=[
            V("li-longtable", "feed", "scheduled", "Episode 42 is with Joss Whitaker, who started as a dishwasher and now runs the kitchen. We talk about what you learn from the back of the house."),
            V("bsky-longtable", "feed", "scheduled", "Episode 42: Joss Whitaker on what the dishwasher knows. Out Monday.")]),
        # ---- drafts (media, no variants) and ideas
        P("lt_draft_priya", LT, _quote("Salt remembers the sea even when we forget.", "Priya Raman", "Salt farmer", "paper"), age=1, base="Priya Raman on harvesting salt by hand."),
        P("lt_draft_guest", LT, {"t": "announcement", "s": "ink", "f": "4x5", "slots": {"kicker": "Next week", "title": "A salt farmer comes to the table", "sub": "Priya Raman, episode 43.", "show": SHOW}},
          age=1, base="Next week: Priya Raman, who harvests salt by hand, joins us for episode 43."),
        P("lt_idea_clip", LT, None, age=2, idea="Clip: Ines on why she mills at night (episode 41, 12:40 to 13:25). Works alone as a 45-second vertical.",
          origin={"type": "episode", "ref": "ep41", "clip": "ep41-1240"}, reason="a complete thought, funny, no setup needed", confidence=0.82),
        P("lt_idea_questions", LT, None, age=2, idea="Carousel: five questions to ask at your own long table, taken from the last five guests.",
          origin={"type": "episode", "ref": "season-4"}, reason="listeners ask for conversation starters every month", confidence=0.7),
        P("lt_idea_backstage", LT, None, age=1, idea="Behind the scenes: setting the table for the live taping at The Granary.",
          origin=None, reason="the live event needs warm-up content", confidence=0.55),
        P("lt_idea_poll", LT, None, age=1, idea="Poll: salt before or after the oven?", origin=None, reason="cheap engagement, ties to Priya's episode", confidence=0.4),
        # ---- Loaf & Ladle
        P("ll_sourdough", LL, _spot("loaf", "Country Sourdough", "$9. Fourteen hours, cold-fermented.", "Baked Tuesday to Saturday", "paper"), age=12, variants=[
            V("ig-loaf", "feed", "published", "Country sourdough. Fourteen hours in the cold, one hot oven, a crust that sings when you cut it.", hashtags=HASH_LL),
            V("fb-loaf", "feed", "published", "Country sourdough is back on the shelf: fourteen hours of cold fermentation, a crackling crust and a soft, sour crumb. $9, baked Tuesday to Saturday.")]),
        P("ll_bun", LL, _spot("bun", "Cardamom Morning Bun", "$4.50. Gone by 9 on Saturdays.", "Baked fresh each morning", "clay", "1x1"), age=10, variants=[
            V("ig-loaf", "feed", "published", "Cardamom morning buns, rolled and knotted by hand. Gone by 9 on Saturdays.", hashtags=HASH_LL),
            V("fb-loaf", "feed", "scheduled", "Cardamom morning buns: $4.50, baked fresh each morning. If you want one on Saturday, come before 9.")]),
        P("ll_testimonial", LL, {"t": "testimonial", "s": "paper", "f": "1x1", "slots": {"quote": "The cardamom bun is the reason I leave the house on Saturdays.",
                                                                                          "attribution": "Dana R.", "title": "Regular since 2022", "show": BIZ}},
          age=8, variants=[
            V("ig-loaf", "feed", "published", "Kind words from Dana, shared with permission.", hashtags=HASH_LL),
            V("fb-loaf", "feed", "failed", "\"The cardamom bun is the reason I leave the house on Saturdays.\" Thank you, Dana.")]),
        P("ll_soup", LL, _spot("soup", "Roasted Onion Soup", "$12 a quart, with a heel of rye.", "Thursdays and Saturdays", "ink"), age=4, variants=[
            V("ig-loaf", "feed", "scheduled", "Onion soup season. Slow-roasted, a little sweet, served with a heel of rye. $12 a quart, Thursdays and Saturdays.", hashtags=HASH_LL),
            V("fb-loaf", "feed", "scheduled", "Roasted onion soup is back on Thursdays and Saturdays. $12 a quart, with a heel of rye to go with it.")]),
        P("ll_rye", LL, _spot("rye", "Rye & Caraway", "$10. Dense, tangy, sliceable.", "Fridays only", "paper", "4x5", 0.5), age=3, variants=[
            V("ig-loaf", "feed", "rework", "Rye and caraway: dense, tangy and made for thin slices with butter. Fridays only.", hashtags=HASH_LL),
            V("fb-loaf", "feed", "in_review", "Fridays are rye days. Rye and caraway, $10: dense, tangy, made for thin slices with butter.")]),
        P("ll_thanksgiving", LL, {"t": "offer", "s": "clay", "f": "4x5", "slots": {"kicker": "Pre-order", "title": "The Thanksgiving loaf box", "price": "$38",
                                                                                 "valid": "Order by Nov 15, while the dough lasts", "cta": "Order at the counter", "show": BIZ}},
          age=6, campaign=c2, variants=[
            V("ig-loaf", "feed", "scheduled", "The Thanksgiving loaf box: two loaves, a dozen rolls and a pot of salted butter, $38. Order by Nov 15, while the dough lasts.", hashtags=HASH_LL),
            V("fb-loaf", "feed", "scheduled", "Pre-order the Thanksgiving loaf box: two loaves, a dozen rolls and salted butter for $38. Order at the counter by Nov 15, while the dough lasts."),
            V("ig-loaf", "story", "in_review", "")]),
        P("ll_quote_baker", LL, _quote("Bread is just patience you can eat.", "Rosa Alvarez", "Baker, Loaf & Ladle", "ink", show=BIZ), age=3, variants=[
            V("ig-loaf", "feed", "scheduled", "Bread is just patience you can eat. Rosa, at 4 a.m.", hashtags=HASH_LL),
            V("fb-loaf", "feed", "in_review", "\"Bread is just patience you can eat.\" Rosa Alvarez, baker.")]),
        P("ll_closed", LL, {"t": "announcement", "s": "paper", "f": "1x1", "slots": {"kicker": "Heads up", "title": "Closed Thursday Nov 27", "sub": "Happy Thanksgiving. Back Tuesday at 7.", "show": BIZ}},
          age=13, variants=[
            V("fb-loaf", "feed", "published", "We are closed Thursday, November 27. Happy Thanksgiving. Back Tuesday at 7."),
            V("ig-loaf", "feed", "scheduled", "Closed Thursday, November 27. Happy Thanksgiving. Back Tuesday at 7.", hashtags=HASH_LL)]),
        P("ll_hours", LL, {"t": "announcement", "s": "ink", "f": "1x1", "slots": {"kicker": "New hours", "title": "Winter hours start Nov 3", "sub": "Tuesday to Saturday, 7 to 2.",
                                                                                 "cta": "Closed Sunday and Monday", "show": BIZ}},
          age=3, campaign=c2, variants=[
            V("ig-loaf", "feed", "scheduled", "Winter hours start November 3: Tuesday to Saturday, 7 to 2. Closed Sunday and Monday.", hashtags=HASH_LL),
            V("fb-loaf", "feed", "approved", "Winter hours start November 3. We are open Tuesday to Saturday, 7 to 2, and closed Sunday and Monday.")]),
        P("ll_market", LL, {"t": "event", "s": "paper", "f": "4x5", "slots": {"kicker": "Saturday market", "title": "Warm bread at the Riverside Market", "when": "Sat Nov 8",
                                                                             "time": "8 to noon", "place": "Stall 14, Riverside Market", "cta": "Come early for buns", "show": BIZ}},
          age=5, variants=[
            V("ig-loaf", "feed", "scheduled", "Find us at the Riverside Market on Saturday, November 8, from 8 to noon. Stall 14. Come early for buns.", hashtags=HASH_LL),
            V("fb-loaf", "feed", "in_review", "We will be at the Riverside Market on Saturday, November 8, 8 to noon, Stall 14, with warm bread and buns.")]),
        P("ll_beforeafter", LL, {"t": "before-after", "s": "paper", "f": "4x5", "photos": {"before": "jar_before", "after": "jar_after"},
                                 "slots": {"before_label": "Day 1", "after_label": "Day 5", "title": "Five days of feeding", "show": BIZ}},
          age=4, variants=[
            V("ig-loaf", "feed", "scheduled", "Five days of feeding, flour and water, every morning. This is what a starter looks like when it is ready.", hashtags=HASH_LL),
            V("fb-loaf", "feed", "scheduled", "Day 1 and day 5 of a new starter: flour, water, patience.")]),
        P("ll_tips", LL, {"t": "tip-list", "s": "ink", "f": "4x5", "slots": {
            "show": BIZ, "cover": {"title": "Four ways to revive day-old bread", "subtitle": "From the counter at Loaf & Ladle"},
            "slides": [{"kind": "point", "label": "Tip 1", "text": "Run it under the tap, then into a hot oven for ten minutes."},
                       {"kind": "point", "label": "Tip 2", "text": "Slice thick and toast in butter."},
                       {"kind": "point", "label": "Tip 3", "text": "Tear it up for croutons or a bread soup."},
                       {"kind": "point", "label": "Tip 4", "text": "Freeze what you will not eat today, sliced."}],
            "closing": {"line": "Bread is meant to be used up", "cta": "Ask us at the counter"}}}, age=2, variants=[
            V("ig-loaf", "carousel", "in_review", "Four ways to revive day-old bread. Save this for Monday.", hashtags=HASH_LL),
            V("fb-loaf", "carousel", "draft", "Four ways to revive day-old bread.")]),
        P("ll_stat_14h", LL, {"t": "stat-card", "s": "clay", "f": "1x1", "slots": {"stat": "14 hours", "title": "of cold fermentation in every country loaf",
                                                                                  "source": "Our own bake log", "show": BIZ}},
          age=1, variants=[V("ig-loaf", "feed", "approved", "Fourteen hours is the whole secret. That, and a hot oven.", hashtags=HASH_LL)]),
        P("ll_draft_bun", LL, _spot("bun", "Cardamom Morning Bun", "Back on Saturday.", "While the dough lasts", "paper", "4x5"), age=1, base="Buns are back Saturday."),
        P("ll_draft_soup", LL, _spot("soup", "Roasted Onion Soup", "Back on Thursday.", "By the quart", "clay", "1x1"), age=1, base="Soup returns Thursday."),
        P("ll_idea_swap", LL, None, age=2, idea="Reel idea: the 5 a.m. bake, from first loaf in to last out, with the shop sounds.", origin=None,
          reason="no video of the bake exists yet; needs the baker to shoot it", confidence=0.6),
        P("ll_idea_kids", LL, None, age=1, idea="Saturday kids' bake-along: free, 9:30, one small loaf each.", origin=None, reason="asked about twice this month", confidence=0.35),
    ]


# ------------------------------------------------------------------------------------------------ rendering

def _render_cards(sp: StudioProject, spec: list[P], lib: dict, work: Path) -> dict[str, list[dict]]:
    """One Chromium for every card. Returns key -> [{file, alt, w, h}] (one entry per slide)."""
    from ..content import registry as R, render_motionkit as RM, settings
    cfg = settings.load()
    reg = R.load_registry(cfg=cfg)
    out_dir = work / "demo" / "renders"
    photo_path = {**{k: v["file"] for k, v in lib["photos"].items()}, "art": lib["art"]["file"]}
    out: dict[str, list[dict]] = {}
    with RM.Session() as ses:
        for p in spec:
            if not p.card:
                continue
            c = p.card
            slots = json.loads(json.dumps(c["slots"]))
            for slot, ph in {**(c.get("photo") or {}), **(c.get("photos") or {})}.items():
                slots[slot] = photo_path[ph]
            item = {"id": p.key, "template": c["t"], "style": c["s"], "format": c["f"], "renderer": "motion-kit", "platforms": ["any"], "slots": slots}
            res = RM.render_item(sp, item, ses, out_dir, reg, cfg, want_background=False)
            if not res["critic"]["ok"]:
                raise DemoError(f"the card for {p.key} failed the renderer's own critic: {res['critic']['notes']}")
            seen: dict[str, dict] = {}
            for o in sorted(res["outputs"], key=lambda o: o.get("slide", 0)):
                seen.setdefault(o["file"], o)
            out[p.key] = [{"file": f, "alt": o["alt_text"], "w": o["w"], "h": o["h"]} for f, o in seen.items()]
    return out


def _register_cards(sp: StudioProject, spec: list[P], rendered: dict, lib: dict, now: datetime) -> dict[str, list[dict]]:
    from .. import library
    by_path = {v["file"]: v["id"] for v in [*lib["photos"].values(), lib["art"]]}
    photo_file = {**{k: v["file"] for k, v in lib["photos"].items()}, "art": lib["art"]["file"]}
    media: dict[str, list[dict]] = {}
    for p in spec:
        if p.key not in rendered:
            continue
        names = [*(p.card.get("photo") or {}).values(), *(p.card.get("photos") or {}).values()]
        derived = [by_path[photo_file[n]] for n in names]
        items = []
        for slot, r in enumerate(rendered[p.key]):
            rec = library.add_file(sp, sp.root / r["file"], source={"type": "render"}, by="claude", derived_from=derived, alt=r["alt"],
                                   tags=[p.card["t"], p.card["s"], p.card["f"], "demo", p.brand], origin={"demo": p.key, "file": r["file"]},
                                   now=now - timedelta(days=p.age))
            items.append({"slot": slot, "asset": rec["id"], "role": "image", "alt": r["alt"], "crop": {"grid_focus": [0.5, 0.5]}})
        media[p.key] = items
    return media


# ------------------------------------------------------------------------------------------------ slots

class Slots:
    """Hands out each account's rule slots: past ones (newest first) for finished posts, future ones (soonest first) for the rest."""

    def __init__(self, sp: StudioProject, now: datetime):
        from ..schedule import slots as S
        self.S, self.sp, self.now = S, sp, now
        self.past: dict[str, list[dict]] = {}
        self.future: dict[str, list[dict]] = {}
        for acct in RULES:
            self.past[acct] = list(reversed(list(S.iter_slots(sp, acct, now - timedelta(days=30), now - timedelta(hours=3)))))
            self.future[acct] = list(S.iter_slots(sp, acct, now + timedelta(hours=2), now + timedelta(days=30)))

    def take(self, acct: str, past: bool) -> dict:
        pool = self.past[acct] if past else self.future[acct]
        if not pool:
            raise DemoError(f"no {'past' if past else 'future'} slot left for {acct}")
        return pool.pop(0)

    def schedule(self, slot: dict) -> dict:
        return self.S.make_schedule(slot["at"], slot=slot["slot_id"], set_by="agent")


# ------------------------------------------------------------------------------------------------ build

PAST = {"published", "failed", "missed", "uncertain"}
FUTURE = {"scheduled", "in_review", "rework"}
NEEDS_QUEUE = {"published", "failed", "missed", "uncertain", "scheduled", "approved", "in_review", "rework"}
NEEDS_APPROVAL = {"published", "failed", "missed", "uncertain", "scheduled", "approved"}
FAIL_ERRORS = {"linkedin": "401 Unauthorized: the access token has expired (demo error)", "facebook": "190: the Page access token was invalidated (demo error)"}


def _vid(post_key: str, i: int) -> str:
    return f"v_{post_key[:18].replace('_', '')}{i}"[:40]


def build_demo(target: str | Path, *, now: datetime | None = None, log=None) -> dict:
    """Build the demo studio in `target` (must not exist or be empty). Returns a summary. `now` pins the clock the data is laid out around
    (default: the real time); the same `now` gives the same studio apart from queue timestamps."""
    from ..platforms import registry as PR
    from ..publish import queue as pq
    from . import sync
    from .service import open_studio
    t0 = time.time()
    say = log or (lambda m: None)
    root = Path(target).expanduser()
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise DemoError(f"{root} is not empty: `ave studio demo` never overwrites anything. Choose a new folder.")
    now = (now or SystemClock().now()).astimezone(ZoneInfo(TZ)).replace(microsecond=0)

    sp = StudioProject(root)
    sp.create(title=TITLE, tz=TZ)
    meta = read_json(sp.meta_path)
    meta.update(show="The Long Table", demo=True, created=iso_utc(now - timedelta(days=45)))
    model.require_valid("studio", meta)
    write_json(sp.meta_path, meta)
    (root / "DEMO.md").write_text(
        "# Demo studio\n\nEverything in this folder is synthetic: the people, shows, prices, pictures, the \"stock\" provider and its licenses.\n"
        "Every account is in dry-run: no credentials, no live attestation, nothing can post.\n\nOpen it: `ave studio ui <this folder> --open` "
        "(in your own terminal; it prints a PIN).\n")
    _write_accounts_and_rules(sp)
    say("accounts and schedule rules")

    lib = _build_library(sp, sp.work, now)
    say("library")
    spec = posts_spec()
    rendered = _render_cards(sp, spec, lib, sp.work)
    media = _register_cards(sp, spec, rendered, lib, now)
    say("cards")

    pubs = {a["id"]: PR.adapter_for(_account(sp, a["id"]), host=DemoHost()) for a in ACCOUNTS}
    slots = Slots(sp, now)
    made: dict[str, dict] = {}                       # post key -> {"id", "variants": {index: variant id}}
    for p in spec:
        made[p.key] = _make_post(sp, p, media.get(p.key, []), slots, pubs, now, pq, sync)
    say("posts")

    _campaigns(sp, spec, made, now)
    ids = {k: v["id"] for k, v in made.items()}
    var = {(p.key, i): made[p.key]["variants"][i] for p in spec for i, _ in enumerate(p.variants) if i in made[p.key]["variants"]}
    _feedback_and_alerts(sp, spec, ids, var, now)
    _proposals(sp, spec, ids, var, slots, now)
    studio = open_studio(root)
    summary = studio.summary()
    return {"studio": str(root), "seconds": round(time.time() - t0, 1), "posts": summary["posts"], "accounts": summary["accounts"],
            "variants_by_status": summary["variants_by_status"], "stages": summary["stages"], "open_feedback": summary["open_feedback"],
            "open_alerts": summary["open_alerts"], "assets": len(store.read_assets(sp)), "now": iso_utc(now)}


def _account(sp, account_id: str):
    from ..platforms.accounts import load_account
    return load_account(sp, account_id)


def _make_post(sp, p: P, media: list[dict], slots: Slots, pubs: dict, now: datetime, pq, sync) -> dict:
    created = now - timedelta(days=p.age, hours=2)
    copy = {"base": p.base or next((v.caption for v in p.variants if v.caption), ""), "link": None, "first_comment": None, "hashtags": [], "cta": None}
    post = model.new_post(idea=p.idea, copy=copy, media=media, campaign=p.campaign, by="claude", now=created, post_id=f"p_{p.key}", origin=p.origin,
                          reason=p.reason, confidence=p.confidence)
    post["brand"] = SHOW if p.brand == LT else BIZ           # which business this post is for (accounts carry the same brand)
    queued, later = [], []                            # (index, V) variants that get a queue item now / are attached after
    for i, v in enumerate(p.variants):
        (queued if v.status in NEEDS_QUEUE else later).append((i, v))
    vids: dict[int, str] = {}
    for i, v in queued + later:
        vids[i] = _vid(p.key, i)
    sched: dict[int, dict] = {}
    for i, v in queued:
        if v.status in PAST or v.status in FUTURE or v.status == "scheduled":
            sched[i] = slots.schedule(slots.take(v.account, past=v.status in PAST))

    def variant(i: int, v: V) -> dict:
        platform = next(a["platform"] for a in ACCOUNTS if a["id"] == v.account)
        nv = model.new_variant(account=v.account, platform=platform, post_type="feed" if platform == "tiktok" and len(media) == 1 else v.type, variant_id=vids[i],
                               copy={"caption": v.caption, "first_comment": v.first_comment, "hashtags": v.hashtags})
        # Photo inbox hosting is unverified; Story copy/alt text needs native steps.
        if platform == "tiktok" or (platform == "instagram" and v.type == "story"):
            nv.update(payload_version=3, delivery={"method": "manual"})
        nv["schedule"] = None if v.status in PAST else sched.get(i)      # the queue refuses a past time: it is set below, after approval
        return nv

    post["variants"] = [variant(i, v) for i, v in queued]
    post = store.save_post(sp, post, expected_rev=None, actor=AGENT, op="create", now=created)
    if queued:
        rep = sync.sync_queue(sp, post["id"], publishers=pubs, now=created)
        bad = [x for x in rep["variants"] if x["action"] not in ("created",) or x["queue_status"] != "awaiting_approval"]
        if bad:
            raise DemoError(f"{post['id']}: the queue refused a variant: {bad} {rep['problems']}")
    post = store.load_post(sp, post["id"])
    for i, v in queued:
        qv = model.find_variant(post, vids[i])
        if v.status in NEEDS_APPROVAL:
            item = pq.load(sp, qv["queue"]["id"])
            pq.approve(sp, item["id"], BY, item["sha256"][:12], publishers=PR_for(sp, item), source="demo")
        if v.status in PAST:
            qv["schedule"] = sched[i]
            item = pq.load(sp, qv["queue"]["id"])
            item["schedule"] = sched[i]["utc"]            # meta_v 2: the schedule is not bound, the approval stays valid
            pq.save(sp, item)
        _finish_variant(sp, qv, v, sched.get(i), now, pq)
        if v.status == "scheduled" and (p.key, v.account) == ("lt_quote_ines", "ig-longtable"):
            qv["flags"] = ["moved_after_approval"]        # a human moved this approved post: the approval holds (the time is not signed) and the card says so
            from .events import append_event, make_event
            when = ((sched.get(i) or {}).get("utc") or iso_utc(now))
            append_event(sp, make_event(actor=HUMAN, op="move", post=post["id"], variant=qv["id"], detail={"from": iso_utc(datetime.fromisoformat(when.replace("Z", "+00:00")) - timedelta(days=2)), "to": when, "snap": "slot"},
                                       now=now - timedelta(hours=3)), now=now - timedelta(hours=3))
    for i, v in later:
        nv = variant(i, v)
        nv["dropped"] = v.status == "dropped"
        post["variants"].append(nv)
    post = store.save_post(sp, post, expected_rev=post["rev"], actor=AGENT, op="demo", detail={"variants": len(post["variants"])}, now=now - timedelta(minutes=5))
    return {"id": post["id"], "variants": vids}


def PR_for(sp, item: dict) -> dict:
    from ..platforms import registry as PR
    return PR.publishers_for_item(sp, item, host=DemoHost())


def _finish_variant(sp, qv: dict, v: V, schedule: dict | None, now: datetime, pq) -> None:
    """Drive an approved queue item to its final demo state by writing what the sender would have written (the sender itself never runs)."""
    if v.status not in ("published", "failed", "uncertain"):
        return
    item = pq.load(sp, qv["queue"]["id"])
    plat = qv["platform"]
    at = (schedule or {}).get("utc") or iso_utc(now)
    if v.status == "published":
        url = f"https://demo.invalid/{plat}/{qv['id']}"
        item["status"] = "published"
        item["results"] = {plat: {"state": "published", "id": f"demo-{qv['id']}", "url": url, "at": at}}
        qv["result"] = {"state": "published", "at": at, "id": f"demo-{qv['id']}", "url": url, "platform": plat, "account": qv["account"]}
        qv["attempts"] = [{"at": at, "outcome": "published", "detail": url}]
    elif v.status == "failed":
        err = FAIL_ERRORS.get(plat, "the platform refused the post (demo error)")
        item["status"] = "failed"
        item["results"] = {plat: {"state": "failed", "error": err, "at": at}}
        qv["result"] = {"state": "failed", "at": at, "error": err, "kind": "auth"}
        qv["attempts"] = [{"at": at, "outcome": "failed", "kind": "auth", "detail": err}]
    else:
        item["results"] = {plat: {"state": "sending", "started": at, "error": "timeout after the final step (demo)"}}
        qv["result"] = {"state": "uncertain", "at": at, "detail": "timeout after the final step"}
        qv["attempts"] = [{"at": at, "outcome": "uncertain", "detail": "timeout after the final step"}]
    pq.save(sp, item)


def _campaigns(sp, spec: list[P], made: dict, now: datetime) -> None:
    c1 = campaigns.new(sp, "Season 4 launch", goal="Get listeners to episode 41 and the live taping", start=(now - timedelta(days=14)).date().isoformat(), end=(now + timedelta(days=21)).date().isoformat(), accounts=["ig-longtable", "x-longtable", "li-longtable", "bsky-longtable"],
                       per_week={"ig-longtable": 3, "x-longtable": 3}, themes=["patience", "bread", "the live taping"],
                       brief="Warm, specific, no hype. Quote the guest, name the episode.", now=now - timedelta(days=14))
    c2 = campaigns.new(sp, "Thanksgiving loaf box", goal="Fill 60 pre-orders by Nov 15", start=(now - timedelta(days=7)).date().isoformat(), end=(now + timedelta(days=21)).date().isoformat(), accounts=["ig-loaf", "fb-loaf"], per_week={"ig-loaf": 2, "fb-loaf": 2},
                       themes=["pre-order", "winter hours"], brief="Plain prices and dates. Never invent a deadline.", now=now - timedelta(days=8))
    for p in spec:
        if p.campaign:
            campaigns.add_post(sp, p.campaign, made[p.key]["id"], actor=AGENT, now=now - timedelta(days=p.age - 0.2))
    assert c1 and c2


def _feedback_and_alerts(sp, spec: list[P], ids: dict, var: dict, now: datetime) -> None:
    def vid(key: str, account: str, typ: str | None = None) -> str | None:
        p = next(x for x in spec if x.key == key)
        for i, v in enumerate(p.variants):
            if v.account == account and (typ is None or v.type == typ) and (key, i) in var:
                return var[(key, i)]
        return None

    h = now - timedelta(hours=1)
    ledger.append_feedback(sp, {"post": ids["lt_quote_joss"], "variant": vid("lt_quote_joss", "ig-longtable"), "kind": "rework_copy", "by": "maren", "source": "ui",
                                "note": "Too bleak for a Monday morning slot. Can we pair it with the funnier line from the same episode?",
                                "structured": {"copy": {"field": "variants." + (vid("lt_quote_joss", "ig-longtable") or "") + ".copy.caption"}}}, now=h - timedelta(hours=20))
    ledger.append_feedback(sp, {"post": ids["ll_rye"], "variant": vid("ll_rye", "ig-loaf"), "kind": "rework_visual", "by": "maren", "source": "ui",
                                "note": "The loaf is cut off in the 3:4 grid. Center it or use the square photo.",
                                "structured": {"visual": {"problem": "wrong_crop"}}}, now=h - timedelta(hours=6))
    ledger.append_feedback(sp, {"post": ids["lt_event_live"], "variant": vid("lt_event_live", "x-longtable"), "kind": "retime", "by": "maren", "source": "ui",
                                "note": "Hold until the tickets page is live on Wednesday."}, now=h - timedelta(hours=3))
    ledger.append_feedback(sp, {"post": ids["lt_draft_priya"], "kind": "note", "by": "maren", "source": "cli",
                                "note": "Ask Priya before we put her name on a card."}, now=h - timedelta(hours=30))
    ledger.append_feedback(sp, {"post": ids["ll_hours"], "variant": vid("ll_hours", "fb-loaf"), "kind": "note", "by": "maren", "source": "ui",
                                    "note": "Add the holiday closing days when you have them."}, now=h - timedelta(hours=9))
    done = ledger.append_feedback(sp, {"post": ids["lt_quote_table"], "variant": vid("lt_quote_table", "ig-longtable"), "kind": "rework_copy", "by": "maren", "source": "ui",
                                       "note": "Shorter, please."}, now=now - timedelta(days=13))
    ledger.resolve_feedback(sp, done["id"], "claude", note="Cut the caption to two lines.", now=now - timedelta(days=12, hours=20))
    v = vid("lt_stat_salt", "bsky-longtable")
    ledger.append_feedback(sp, {"post": ids["lt_stat_salt"], "variant": v, "kind": "human_edit", "by": "maren", "source": "ui",
                                "structured": {"copy": {"field": f"variants.{v}.copy.caption", "before": "41 weeks, the longest we argued about salt.",
                                                        "after": "41 weeks: the longest we have ever argued about salt."}}}, now=now - timedelta(hours=26))
    al = ledger.append_alert
    al(sp, severity="error", kind="auth_expired", account="li-longtable", post=ids["lt_ep41_announce"],
       message="LinkedIn rejected the post: the access token has expired. A human re-enters it with `ave studio account auth li-longtable`.", now=now - timedelta(days=10, hours=3))
    al(sp, severity="error", kind="auth_expired", account="fb-loaf", post=ids["ll_testimonial"],
       message="Facebook invalidated the Page token. Re-enter it with `ave studio account auth fb-loaf`.", now=now - timedelta(days=7, hours=5))
    al(sp, severity="warn", kind="missed", account="tt-longtable", post=ids["lt_tiktok_hospitality"],
       message="The TikTok slot passed while the sender was not running. The post was not sent late.", now=now - timedelta(days=7, hours=8))
    al(sp, severity="warn", kind="uncertain", account="x-longtable", post=ids["lt_x_transcript"],
       message="X timed out after the final step. It may or may not have posted: check the account, then run `ave studio resolve`.", now=now - timedelta(days=5, hours=2))
    al(sp, severity="warn", kind="credentials_missing", account="x-longtable", message="No credentials for x-longtable in the keychain (demo account).", now=now - timedelta(days=2))
    al(sp, severity="info", kind="account_not_live", account="ig-longtable", message="ig-longtable is in dry-run. Nothing posts until a human turns it live.", now=now - timedelta(days=1, hours=2))
    al(sp, severity="info", kind="manual_post_due", account="li-longtable", message="A LinkedIn post is due soon. Check the calendar.", now=now - timedelta(hours=4))
    old = al(sp, severity="warn", kind="late", account="ig-loaf", message="The 07:30 Instagram slot was 6 minutes late (the Mac woke from sleep).", now=now - timedelta(days=9))
    ledger.ack_alert(sp, old["id"], "maren", now=now - timedelta(days=9, hours=-1))


def _proposals(sp, spec: list[P], ids: dict, var: dict, slots: Slots, now: datetime) -> None:
    from ..schedule import proposals

    def vid(key: str, account: str, status: str) -> str:
        p = next(x for x in spec if x.key == key)
        return next(var[(key, i)] for i, v in enumerate(p.variants) if v.account == account and v.status == status)

    def move(key: str, account: str, status: str, why: str) -> dict:
        s = slots.take(account, past=False)
        return {"post": ids[key], "variant": vid(key, account, status), "to": {"local": s["local"], "tz": s["tz"]}, "why": why}
    proposals.create(sp, "claude", [
        move("lt_event_live", "ig-longtable", "approved", "approved with no time: next free slot, ahead of the live taping"),
        move("ll_hours", "fb-loaf", "approved", "approved with no time: next free Facebook slot before winter hours start")], now=now - timedelta(hours=5))
    proposals.create(sp, "claude", [
        move("lt_testimonial", "li-longtable", "draft", "campaign plan: next free LinkedIn slot"),
        move("ll_tips", "fb-loaf", "draft", "campaign plan: pair with the Instagram carousel")], now=now - timedelta(hours=2))
