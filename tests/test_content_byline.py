"""Regression: the show name prints ONCE per graphic, and the speaker's name has its title (CO-HOST) beneath it.

Reported on the first drafts: the show name was printed twice and the CO-HOST title was missing beneath the host names. Root cause: the stored ContentDoc carried role == show == "The Long Table", the drafts on disk were
rendered before the blank-the-duplicate fix, and the doc could not be re-rendered at all because the clips had been renumbered
(`Sources changed since the plan was made`). Covered here for every template, with real browser measurements of the DOM text.
"""

import hashlib
import json
from pathlib import Path

import pytest

from ave import motion
from ave.content import manifest as M
from ave.content import people
from ave.content import registry as R
from ave.content import render_canva as C
from ave.content import render_motionkit as RM
from ave.content import settings
from ave.content import sources as S
from ave.episode import Episode
from content_helpers import cep, content_env, make_episode  # noqa: F401

SHOW = "The Long Table"
Q = "So no further amount of counting the trees one by one is going to help us see the whole forest grow back in a healthy way."
SPEAKERS = {"S1": {"name": "Maren", "channel": "left"}, "S2": {"name": "Tomas", "channel": "right"}}


def _set_speakers(ep: Episode, speakers: dict) -> None:
    meta = json.loads(ep.meta_path.read_text())
    meta["speakers"] = speakers
    ep.meta_path.write_text(json.dumps(meta))


@pytest.fixture(scope="module")
def kit(tmp_path_factory):
    ep = make_episode(tmp_path_factory.mktemp("byline") / "ep")
    _set_speakers(ep, SPEAKERS)
    cfg = settings.load()
    reg = R.load_registry(cfg=cfg)
    with RM.Session() as s:
        yield ep, cfg, reg, s


def _shoot(kit, template, style, fmt, slots):
    ep, cfg, reg, s = kit
    item = M.new_item("t", template, style, fmt, slots, reason="t", confidence=0.9)
    tpl = reg.get(template)
    canvas = tpl.size(fmt)
    props = RM._props(tpl, item, ep, canvas, cfg)
    t = RM.CARD_HOLD_S if template == "reel-card" else 0.0
    shot = s.shoot(tpl.still_dir(item["renderer"]), canvas, props, t=t, duration=max(t, 1.0) + 1.0, want_background=False)
    return shot, props


def _count(shot, phrase=SHOW):
    return " ".join(shot["text"].casefold().split()).count(phrase.casefold())


# every duplicate the show name could arrive through, one per template
DUPES = {
    "quote-post": {"quote": Q, "attribution": "Maren", "role": SHOW, "title": SHOW, "show": SHOW},
    "thumbnail": {"title": "Soil is a habit, not a goal", "guest": f"Maren Okafor, {SHOW}", "show": SHOW},
    "carousel": {"show": SHOW, "cover": {"title": "What soil teaches about staying", "subtitle": SHOW},
                 "slides": [{"kind": "quote", "quote": "A goal is a wish and a system is a habit", "attribution": "Maren", "title": SHOW}],
                 "closing": {"line": "Listen to the full conversation", "cta": SHOW}},
    "reel-card": {"kind": "outro", "title": "Soil is a habit, not a goal", "subtitle": f"{SHOW} - episode 00", "show": SHOW, "cta": SHOW},
}


def _shoot_pages(kit, template, style, fmt, slots):
    """One shot per image (a carousel yields one per slide)."""
    ep, cfg, reg, s = kit
    tpl = reg.get(template)
    item = M.new_item("t", template, style, fmt, slots, reason="t", confidence=0.9)
    base = RM._props(tpl, item, ep, tpl.size(fmt), cfg)
    pages = [{**base, "_slide": sl} for sl in RM._slides(base)] if template == "carousel" else [base]
    t = RM.CARD_HOLD_S if template == "reel-card" else 0.0
    return [s.shoot(tpl.still_dir("motion-kit"), tpl.size(fmt), pg, t=t, duration=max(t, 1.0) + 1.0, want_background=False) for pg in pages]


@pytest.mark.parametrize("template", list(DUPES))
def test_every_template_prints_the_show_name_exactly_once_even_when_slots_repeat_it(kit, template):
    reg = kit[2]
    for style in reg.get(template).styles:
        for fmt in reg.get(template).formats:
            for shot in _shoot_pages(kit, template, style, fmt, DUPES[template]):
                assert _count(shot) == 1, (template, style, fmt, shot["text"])
                assert RM.show_name_count(shot["measure"], SHOW) == 1


def test_quote_card_shows_the_show_once_at_the_top_then_the_name_with_co_host_beneath(kit):
    """ink 4:5, paper 1:1, clay 9:16 (the three real drafts): show mark on top, 'Maren'/'Tomas', 'CO-HOST' directly beneath the name."""
    for style, fmt, who in (("ink", "4x5", "Maren"), ("paper", "1x1", "Tomas"), ("clay", "9x16", "Tomas")):
        # stored the way the first drafts had it: full surname attribution and the show repeated as the role
        shot, props = _shoot(kit, "quote-post", style, fmt, {"quote": Q, "attribution": f"{who} Okafor" if who == "Maren" else who,
                                                              "role": SHOW, "show": SHOW})
        assert _count(shot) == 1, (style, fmt, shot["text"])
        el = {e["name"]: e for e in shot["measure"]}
        assert el["show"]["text"] == SHOW and el["attribution"]["text"] == who and el["byline_title"]["text"] == "CO-HOST"
        assert "Okafor" not in shot["text"]
        top, name, title = el["show"]["rect"], el["attribution"]["rect"], el["byline_title"]["rect"]
        assert top["y"] + top["h"] < el["quote"]["rect"]["y"]                       # show mark above the quote
        assert name["y"] > el["quote"]["rect"]["y"] + el["quote"]["rect"]["h"]       # name below it
        assert title["y"] >= name["y"] + name["h"] - 1 and abs(title["x"] - name["x"]) < 1    # CO-HOST directly beneath the name, same left edge
        assert props["title"] == "CO-HOST" and props["role"] == ""


def test_critic_flags_a_double_show_name_it_is_handed(kit):
    """Belt and braces: even if some path let the show through twice, the critic notes it and the item is not ok."""
    ep, cfg, reg, s = kit
    shot, _ = _shoot(kit, "quote-post", "ink", "4x5", {"quote": Q, "attribution": "Maren", "show": SHOW})
    shot["measure"].append({**shot["measure"][0], "name": "byline_title", "text": SHOW})
    crit = RM.critique({**shot, "background": None}, (1080, 1350), cfg["safe_area"]["4x5"], cfg, show=SHOW)
    assert crit["show_name_count"] == 2 and not crit["ok"] and any("printed 2 times" in n for n in crit["notes"])


def test_render_item_records_one_show_name_for_a_clean_item_and_alt_text_names_it_once(kit):
    ep, cfg, reg, s = kit
    it = M.new_item("q", "quote-post", "ink", "4x5", {"quote": Q, "attribution": "Maren Okafor", "role": SHOW, "show": SHOW}, reason="t", confidence=1)
    res = RM.render_item(ep, it, s, M.content_dir(ep) / "renders" / "once", reg, cfg)
    assert res["critic"]["ok"] and res["critic"]["images"][0]["show_name_count"] == 1
    alt = res["outputs"][0]["alt_text"]
    assert alt.count(SHOW) == 1 and "Maren, CO-HOST" in alt and "Okafor" not in alt


# ---- who is named, and how ------------------------------------------------------------------------------

def test_speaker_names_and_titles_come_from_episode_json(cep):
    _set_speakers(cep, {"S1": {"name": "Maren", "full_name": "Maren Okafor"}, "S2": {"name": "Tomas", "display_name": "Tomas R.", "title": "PRODUCER"},
                        "S3": {"name": "Ines", "guest": True}})
    spk = people.load_speakers(cep)
    assert people.byline(spk, "Maren Okafor") == ("Maren", "CO-HOST")       # full_name matches; it is never printed
    assert people.byline(spk, "maren") == ("Maren", "CO-HOST")
    assert people.byline(spk, "Tomas") == ("Tomas R.", "PRODUCER")              # display_name prints, title overrides the default
    assert people.byline(spk, "S2") == ("Tomas R.", "PRODUCER")
    assert people.byline(spk, "Ines") == ("Ines", "")                         # a guest has no default title
    assert people.byline(spk, "Ines", "Soil ecologist") == ("Ines", "Soil ecologist")   # an explicit title wins
    assert people.byline(spk, "Someone Else", "") == ("Someone Else", "")       # unknown people are left as written
    assert people.load_speakers(None) == []


def test_two_speakers_sharing_a_first_name_are_never_guessed(cep):
    _set_speakers(cep, {"S1": {"name": "Sam Lee"}, "S2": {"name": "Sam Ortiz"}})
    assert people.find(people.load_speakers(cep), "Sam") is None


def test_effective_slots_strip_the_show_from_bylines_in_nested_slots_but_never_from_quotes():
    tpl = R.load_registry().get("carousel")
    slots = {"show": SHOW, "cover": {"title": "t" * 5, "subtitle": f"Maren, {SHOW}"},
             "slides": [{"kind": "quote", "quote": f"What {SHOW} means to me is simple", "attribution": f"Tomas - {SHOW}", "title": SHOW}],
             "closing": {"line": "Thanks for listening", "cta": SHOW}}
    eff = R.effective_slots(tpl.slots, slots)
    assert eff["cover"]["subtitle"] == "Maren" and eff["slides"][0]["attribution"] == "Tomas" and eff["slides"][0]["title"] == ""
    assert eff["closing"]["cta"] == "" and SHOW in eff["slides"][0]["quote"]
    assert sorted(R.show_duplicates(slots, tpl.slots)) == ["closing.cta", "cover.subtitle", "slides[0].attribution", "slides[0].title"]


def test_legacy_role_is_the_title_and_template_declares_title(cep):
    tpl = R.load_registry().get("quote-post")
    assert "title" in tpl.slots["properties"]
    eff = R.effective_slots(tpl.slots, {"quote": Q, "role": "Soil ecologist"})
    assert eff["title"] == "Soil ecologist" and eff["role"] == ""
    assert R.effective_slots(tpl.slots, {"quote": Q, "role": "Soil ecologist", "title": "CO-HOST"})["title"] == "CO-HOST"
    spk = people.load_speakers(cep)
    norm = R.normalize_slots(tpl.slots, {"quote": Q, "attribution": "Tomas", "role": SHOW, "show": SHOW}, spk)
    assert norm == {"quote": Q, "attribution": "Tomas", "title": "CO-HOST", "show": SHOW}


def test_propose_fills_the_hosts_name_and_title_and_still_rejects_a_repeat(cep):
    from ave.content import propose as P
    from content_helpers import pick_quote
    _set_speakers(cep, SPEAKERS)
    pick = pick_quote()
    pick["slots"].pop("role", None)
    pick["slots"]["attribution"] = "Tomas"
    out = P.propose(cep, [pick], allow_unapproved=True)
    assert not out["rejected"], out
    slots = M.load_latest(cep)["items"][0]["slots"]
    assert slots["attribution"] == "Tomas" and slots["title"] == "CO-HOST"
    bad = pick_quote()
    bad["slots"]["title"] = SHOW.lower()
    assert "repeats the show name" in P.propose(cep, [bad], allow_unapproved=True)["rejected"][-1]["problems"][-1]


# ---- Canva and captions use the same names -----------------------------------------------------------------

def test_canva_job_and_queue_caption_use_the_display_name_and_title(cep):
    from ave.content import queueing as Q_
    _set_speakers(cep, SPEAKERS)
    cfg = json.loads(json.dumps(settings.load()))
    cfg["renderers"]["canva"].update(enabled=True, templates={"quote-post": {"brand_template_id": "B1", "native_format": "4x5",
                                                              "field_map": {"quote": "QUOTE", "attribution": "BY", "role": "ROLE", "show": "SHOW"}}})
    it = M.new_item("q", "quote-post", "ink", "4x5", {"quote": Q, "attribution": "Maren Okafor", "role": SHOW, "show": SHOW}, reason="t", confidence=1)
    job = C.build_job(cep, M.new_doc(cep), it, R.load_registry(), cfg)
    assert job["fields"]["BY"]["text"] == "Maren" and job["fields"]["ROLE"]["text"] == "CO-HOST" and job["fields"]["SHOW"]["text"] == SHOW
    assert job["warnings"] == []                                                # the old `role` mapping still carries the title
    cap = Q_.derive_caption(it, people.load_speakers(cep))
    assert cap.endswith("Maren, CO-HOST") and SHOW not in cap
    assert Q_.derive_title(it, people.load_speakers(cep)).endswith("(Maren)")


def test_canva_brief_prints_the_final_palette_fonts_and_names(cep, capsys):
    from ave.commands import content as cmd
    _set_speakers(cep, SPEAKERS)
    brief = C.brand_brief(cep)
    assert brief["palette"]["clay"] == "#b9825a" and brief["styles"]["ink"]["bg"].startswith("#1b1814")
    assert brief["fonts"]["serif"]["family"] == "Newsreader" and brief["fonts"]["serif"]["files"][0]["bundled"]
    assert brief["fonts"]["sans"]["license_file"] == "OFL-Inter.txt" and brief["fonts"]["serif"]["license"].startswith("SIL Open Font")
    assert [(s["prints_as"], s["title"]) for s in brief["names"]["speakers"]] == [("Maren", "CO-HOST"), ("Tomas", "CO-HOST")]
    assert any("ONCE" in x for x in brief["card_layout"]["quote_post"])
    assert C.brand_brief()["names"]["speakers"] == []                           # no project given: no names, no crash


# ---- bundled brand fonts -----------------------------------------------------------------------------------

FONT_DIR = motion.KIT / "brand" / "fonts"


def test_bundled_fonts_match_the_recorded_hashes_and_ship_their_licenses():
    sources = (FONT_DIR / "SOURCES.md").read_text()
    for name in ("Newsreader-Variable.ttf", "Inter-Variable.ttf", "OFL-Newsreader.txt", "OFL-Inter.txt"):
        assert hashlib.sha256((FONT_DIR / name).read_bytes()).hexdigest() in sources, name
    for lic in ("OFL-Newsreader.txt", "OFL-Inter.txt"):
        assert "SIL OPEN FONT LICENSE Version 1.1" in (FONT_DIR / lic).read_text()
    assert "raw.githubusercontent.com/google/fonts/" in sources
    assert motion.missing_brand_fonts() == []


def test_fonts_are_used_when_present_and_fall_back_only_when_missing(kit, monkeypatch):
    shot, _ = _shoot(kit, "quote-post", "ink", "4x5", {"quote": Q, "attribution": "Maren", "show": SHOW})
    assert shot["fonts"] == ["Inter", "Newsreader"]
    tokens = motion.load_tokens()
    assert {f["family"] for f in motion._font_faces(tokens)} == {"Newsreader", "Inter"}
    gone = json.loads(json.dumps(tokens))
    gone["fonts"]["serif"]["files"] = [{"file": "NoSuchFont.ttf", "weight": 400}]
    assert {f["family"] for f in motion._font_faces(gone)} == {"Inter"}         # missing file: skipped, the fallback stack covers it
    monkeypatch.setattr(motion, "load_tokens", lambda: gone)
    ep, cfg, reg, s = kit
    shot2, _ = _shoot(kit, "quote-post", "ink", "4x5", {"quote": Q, "attribution": "Maren", "show": SHOW})
    assert shot2["fonts"] == ["Inter"]
