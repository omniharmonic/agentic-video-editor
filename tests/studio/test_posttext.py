"""The final post text (caption + link + credits + hashtags) per platform, and that `queue sync` binds exactly that text (bug: hashtags never posted)."""

import copy

import pytest
from sched_helpers import make_world, no_agent_markers  # noqa: F401

from ave.platforms import bluesky
from ave.publish import queue as pq, safety
from ave.publish.adapters import base
from ave.studio import posttext, store
from ave.studio.events import AGENT
from ave.studio.sync import queue_diff, sync_queue

CAP = "Day 3: the starter smells like pears."


def eff(caption=CAP, tags=("sourdough", "#Baking"), link=None, first=None):
    return {"caption": caption, "hashtags": list(tags), "link": link, "first_comment": first, "cta": None}


def sync(w, pid):
    return sync_queue(w.sp, pid, publishers=w.pubs, now=w.clock.now())


# ---- pure composition, one test per platform convention -------------------------------------------------------------

@pytest.mark.parametrize("platform", ["instagram", "tiktok", "threads", "facebook", "linkedin", "pinterest"])
def test_block_platforms_append_hashtags_after_a_blank_line(platform):
    c = posttext.compose(eff(link="https://acme.example/go"), platform)
    assert c.problems == [] and c.first_comment is None
    assert c.text == f"{CAP}\nhttps://acme.example/go\n\n#sourdough #Baking"
    assert c.hashtags == ["sourdough", "Baking"]


def test_x_appends_hashtags_inline_and_counts_the_link_as_23():
    c = posttext.compose(eff(link="https://acme.example/a/very/long/path/that/would/be/long"), "x")
    assert c.text == f"{CAP} #sourdough #Baking\nhttps://acme.example/a/very/long/path/that/would/be/long"
    assert c.counter["unit"] == "x_weighted" and c.counter["used"] == len(CAP + " #sourdough #Baking\n") + 23 and c.problems == []


def test_bluesky_inline_tags_are_found_by_the_adapters_facet_pattern():
    c = posttext.compose(eff(), "bluesky")
    assert c.text == f"{CAP} #sourdough #Baking" and c.counter["unit"] == "graphemes"
    assert [m.group(1) for m in bluesky._TAG.finditer(c.text)] == ["sourdough", "Baking"]


def test_bluesky_refuses_a_digit_leading_tag():
    c = posttext.compose(eff(tags=["2026"]), "bluesky")
    assert any("starts with a digit" in p for p in c.problems)


def test_youtube_description_gets_the_tag_block_and_the_tag_cap():
    c = posttext.compose(eff(), "youtube")
    assert c.text.endswith("\n\n#sourdough #Baking") and c.counter["unit"] == "bytes" and c.problems == []
    assert any("at most 15" in p for p in posttext.compose(eff(tags=[f"t{i}" for i in range(16)]), "youtube").problems)


def test_instagram_caps_at_30_tags_and_does_not_repeat_a_tag_written_in_the_caption():
    assert any("at most 30" in p for p in posttext.compose(eff(tags=[f"t{i}" for i in range(31)]), "instagram").problems)
    c = posttext.compose(eff(caption="Loving #Sourdough today."), "instagram")
    assert c.text == "Loving #Sourdough today.\n\n#Baking" and c.hashtags == ["sourdough", "Baking"]


def test_tags_are_normalized_and_deduplicated():
    c = posttext.compose(eff(tags=["#Sour Dough", "sourdough", "  ", "bake!"]), "instagram")
    assert c.hashtags == ["SourDough", "bake"] and any("no usable characters" in p for p in c.problems)


def test_required_credits_go_before_the_tags_and_courtesy_credits_do_not():
    assets = {"a": {"license": {"attribution": {"required": True, "text": "Photo by A. Baker on Unsplash"}}},
              "b": {"license": {"attribution": {"required": False, "text": "Image by Z"}}}}
    c = posttext.compose(eff(), "instagram", credits=posttext.credit_lines(assets, ["a", "b", "a"]))
    assert c.text == f"{CAP}\n\nPhoto by A. Baker on Unsplash\n\n#sourdough #Baking"


# ---- first comment mode ----------------------------------------------------------------------------------------------

def cfg_first_comment(platform="instagram"):
    p = copy.deepcopy(base.load_platforms())
    p[platform]["text"]["hashtags_in_first_comment"] = True
    return p


def test_first_comment_mode_moves_tags_out_of_the_caption_and_below_the_first_comment():
    c = posttext.compose(eff(first="Recipe in bio"), "instagram", platforms=cfg_first_comment())
    assert c.text == CAP and c.first_comment == "Recipe in bio\n\n#sourdough #Baking"
    c = posttext.compose(eff(), "threads", platforms=cfg_first_comment("threads"))
    assert c.text == CAP and c.first_comment == "#sourdough #Baking"


def test_first_comment_mode_is_refused_where_the_adapter_posts_no_first_comment():
    c = posttext.compose(eff(), "tiktok", platforms=cfg_first_comment("tiktok"))
    assert any("posts no first comment" in p for p in c.problems)


# ---- overflow is a refusal, never a quiet cut -----------------------------------------------------------------------

@pytest.mark.parametrize("platform,limit", [("x", 280), ("bluesky", 300), ("threads", 500), ("instagram", 2200), ("linkedin", 3000)])
def test_text_that_only_overflows_with_the_hashtags_is_refused(platform, limit):
    cap = "a" * (int(limit * 0.98) - 6)                 # fits alone with the 2 % margin, not with " #tag1" appended twice
    assert posttext.compose(eff(caption=cap, tags=[]), platform).problems == []
    c = posttext.compose(eff(caption=cap, tags=["tag1", "tag2"]), platform)
    assert any("over the" in p for p in c.problems) and c.counter["limit"] == limit


# ---- queue sync: what is reviewed IS what is posted -----------------------------------------------------------------

def make_copy_post(w, tags, caption=CAP, link=None, platform="instagram", account="ig-main"):
    a = w.asset("A loaf of bread on a table.")
    post = w.studio.create_post(copy={"base": caption, "hashtags": tags, "link": link},
                                media=[{"slot": 0, "asset": a["id"], "role": "image", "alt": "A loaf of bread on a table."}])
    post = w.studio.add_variant(post["id"], account=account, platform=platform, post_type="feed", expected_rev=post["rev"])
    return post["id"], post["variants"][0]["id"]


def test_sync_puts_the_composed_text_in_the_queue_item(tmp_path):
    w = make_world(tmp_path)
    pid, vid = make_copy_post(w, ["sourdough", "baking"], link="https://acme.example/starter")
    assert sync(w, pid)["problems"] == []
    item = w.item(pid, vid)
    want = f"{CAP}\nhttps://acme.example/starter\n\n#sourdough #baking"
    assert item["caption"] == want and item["description"] == want and item["tags"] == ["sourdough", "baking"] and not item.get("first_comment")
    assert item["title"] == CAP                                                        # the title is the caption's first line, not the tag block
    from ave.platforms.base import compose_text
    assert compose_text(item) == want                                                  # the adapter adds nothing: the reviewed text is the posted text
    assert queue_diff(w.sp, store.load_post(w.sp, pid), w.variant(pid, vid), item) == []
    assert sync(w, pid)["variants"][0]["action"] == "unchanged"


def test_sync_in_first_comment_mode(tmp_path, monkeypatch):
    patched = cfg_first_comment()
    monkeypatch.setattr(base, "load_platforms", lambda path=None: patched)
    w = make_world(tmp_path)
    pid, vid = make_copy_post(w, ["sourdough"])
    sync(w, pid)
    item = w.item(pid, vid)
    assert item["caption"] == CAP and item["first_comment"] == "#sourdough"


def test_x_and_bluesky_variants_carry_inline_tags_in_the_item(tmp_path):
    w = make_world(tmp_path, accounts=(("x-main", "x"), ("bsky-main", "bluesky")))
    a = w.asset("A loaf of bread on a table.")
    post = w.studio.create_post(copy={"base": CAP, "hashtags": ["sourdough"]},
                                media=[{"slot": 0, "asset": a["id"], "role": "image", "alt": "A loaf of bread on a table."}])
    post = w.studio.add_variant(post["id"], account="x-main", platform="x", post_type="feed", expected_rev=post["rev"])
    post = w.studio.add_variant(post["id"], account="bsky-main", platform="bluesky", post_type="feed", expected_rev=post["rev"])
    sync(w, post["id"])
    for v in store.load_post(w.sp, post["id"])["variants"]:
        assert pq.load(w.sp, v["queue"]["id"])["caption"] == f"{CAP} #sourdough"


def test_overflow_blocks_the_variant_and_creates_no_item(tmp_path):
    w = make_world(tmp_path, accounts=(("x-main", "x"),))
    pid, vid = make_copy_post(w, ["sourdough", "baking"], caption="a" * 270, platform="x", account="x-main")
    rep = sync(w, pid)["variants"][0]
    assert rep["action"] == "blocked" and any("over the 280 limit" in p for p in rep["problems"]) and rep["queue"] is None
    assert pq.list_items(w.sp) == []


def test_overflow_after_approval_leaves_the_approved_text_untouched_and_reports(tmp_path):
    w = make_world(tmp_path, accounts=(("x-main", "x"),))
    pid, vid = make_copy_post(w, [], caption="a" * 270, platform="x", account="x-main")
    sync(w, pid)
    w.approve(pid, vid)
    w.studio.edit_post(pid, {"copy.hashtags": ["sourdough", "baking"]}, expected_rev=store.load_post(w.sp, pid)["rev"], actor=AGENT)
    rep = sync(w, pid)["variants"][0]
    assert rep["action"] == "blocked"
    assert w.item(pid, vid)["caption"] == "a" * 270       # nothing unreviewed replaced the approved wording


def test_the_approval_hash_changes_when_the_hashtags_change(tmp_path):
    w = make_world(tmp_path)
    pid, vid = make_copy_post(w, ["sourdough"])
    sync(w, pid)
    approved = w.approve(pid, vid)
    meta_before = safety.meta_hash(approved)
    w.studio.edit_post(pid, {"copy.hashtags": ["sourdough", "levain"]}, expected_rev=store.load_post(w.sp, pid)["rev"], actor=AGENT)
    rep = sync(w, pid)["variants"][0]
    item = w.item(pid, vid)
    assert rep["invalidated"] and item["status"] == "awaiting_approval"
    assert item["caption"].endswith("#sourdough #levain") and safety.meta_hash(item) != meta_before
    assert pq.send_blockers(w.sp, item)


def test_a_required_credit_reaches_the_item_text(tmp_path):
    w = make_world(tmp_path)
    a = w.asset("A loaf of bread on a table.")
    reg = w.sp.library_dir / "assets.jsonl"
    import json
    rec = {**a, "license": {"attribution": {"required": True, "text": "Photo by A. Baker on Unsplash"}}}
    lines = [json.loads(x) for x in reg.read_text().splitlines() if x.strip()]
    reg.write_text("".join(json.dumps(rec if r.get("id") == a["id"] else r) + "\n" for r in lines))
    post = w.studio.create_post(copy={"base": CAP, "hashtags": ["sourdough"]},
                                media=[{"slot": 0, "asset": a["id"], "role": "image", "alt": "A loaf of bread on a table."}])
    post = w.studio.add_variant(post["id"], account="ig-main", platform="instagram", post_type="feed", expected_rev=post["rev"])
    sync(w, post["id"])
    assert w.item(post["id"], post["variants"][0]["id"])["caption"] == f"{CAP}\n\nPhoto by A. Baker on Unsplash\n\n#sourdough"


def test_required_credits_follow_derived_media_and_ignore_cycles():
    from ave.studio.posttext import credit_lines
    assets={'a_root':{'derived_from':['a_source']},'a_source':{'derived_from':['a_root'],'license':{'attribution':{'required':True,'text':'Photo by Author'}}}}
    assert credit_lines(assets,['a_root','a_source'])==['Photo by Author']


def test_credit_order_is_slot_order_then_depth_first_provenance():
    from ave.studio.posttext import compose_variant
    def credit(text): return {'attribution':{'required':True,'text':text}}
    assets={'render':{'derived_from':['photo_a']},'photo_a':{'license':credit('Credit A')},'photo_b':{'license':credit('Credit B')}}
    post={'copy':{'base':'Caption'},'media':[{'slot':2,'asset':'photo_b'},{'slot':1,'asset':'render'}]}
    variant={'platform':'instagram','copy':{},'media':None}
    text=compose_variant(post,variant,assets).text
    assert text.index('Credit A')<text.index('Credit B')
