"""`ave studio ingest shorts`: human-approved clips from an episode become studio posts, through the real commands on the synthetic demo episode
(`ave demo` -> clips -> a human keeps one -> shorts build/export) and a synthetic studio. No network (the offline guard), no real keychain."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import shorts_flow_helpers as F
from ave import cli, editdoc, library, shorts
from ave.broll import place
from ave.episode import read_json, write_json
from ave.package import postcopy
from ave.studio import copycheck, ingest_shorts as I, store
from ave.studio.clock import iso_utc
from ave.studio.project import StudioProject
from ave.studio.service import open_studio
from conftest import needs_ffmpeg, needs_libass
from studio_helpers import FakeClock, RecordingPublisher, write_account
import broll_place_helpers as H

pytestmark = needs_ffmpeg
PLATFORMS = "reels,youtube,facebook,x"


@pytest.fixture(scope="module")
def flow_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("ingest-shorts") / "demo"
    F.demo_with_short(root, platforms=PLATFORMS)
    return root


class World:
    pass


@pytest.fixture
def world(flow_root, tmp_path):
    w = World()
    w.tmp = tmp_path
    w.ep = F.copy_episode(flow_root, tmp_path / "demo")
    w.sp = StudioProject(tmp_path / "studio")
    w.sp.create(title="The Long Table", tz="UTC")
    w.accounts = {
        "ig-main": write_account(w.sp, "ig-main", "instagram", post_types=["reel", "feed"]),
        "fb-page": write_account(w.sp, "fb-page", "facebook", post_types=["reel"]),
        "yt-main": write_account(w.sp, "yt-main", "youtube", mode="native", post_types=["short", "video"]),
    }
    w.clock = FakeClock()
    w.pubs = {p: RecordingPublisher(p) for p in ("instagram", "facebook", "youtube", "x", "tiktok")}
    w.studio = open_studio(w.sp.root, clock=w.clock, publishers=w.pubs)
    return w


def ingest(w, **kw):
    return I.shorts_ingest(w.studio, w.ep.root, by=kw.pop("by", "claude"), **kw)


def clip(w):
    return shorts.select_clips(w.ep, "c01")[0]


def posts(w):
    return store.list_posts(w.sp)


def attest_all(w):
    for u in library.unattested(w.sp):
        library.attest_license(w.sp, u["asset"], "Maren")


def edit_sidecars(w, **changes):
    """Rewrite every export sidecar of the clip with `changes` (a dict or a function of the sidecar)."""
    for f in (w.ep.exports / "shorts").glob("*.json"):
        d = read_json(f)
        if callable(changes.get("fn")):
            changes["fn"](d)
        else:
            d.update(changes)
        write_json(f, d)


# ---- the happy path --------------------------------------------------------------------------------------------------------------------

def test_an_approved_clip_becomes_one_post_with_a_variant_per_account(world):
    w = world
    r = ingest(w)
    assert r["refused"] == [] and len(r["posts"]) == 1
    p = r["posts"][0]
    assert p["created"] and p["clip"] == "c01" and p["short_version"] == 1
    post = store.load_post(w.sp, p["post"])
    assert post["stage"] == "active" and post["idea"]["text"] == "Why finishing is hard"       # the title from work/shorts/titles.json
    by_acct = {v["account"]: v for v in post["variants"]}
    assert set(by_acct) == {"ig-main", "fb-page", "yt-main"}
    assert [(by_acct[a]["platform"], by_acct[a]["post_type"]) for a in ("ig-main", "fb-page", "yt-main")] == [("instagram", "reel"), ("facebook", "reel"), ("youtube", "short")]
    # the file each variant posts is the platform's export, registered as a render asset
    assets = store.read_assets(w.sp)
    for v in post["variants"]:
        m = (v["media"] or post["media"])[0]
        rec = assets[m["asset"]]
        assert rec["kind"] == "video" and rec["source"]["type"] == "render" and rec["derived_from"]
        assert (w.sp.root / rec["file"]).stat().st_size > 1000 and m["role"] == "video"
    # nothing was approved, scheduled, queued or sent
    assert all(v["queue"] is None and v["schedule"] is None and not v["attempts"] for v in post["variants"])
    assert not list((w.sp.root / "publish" / "queue").glob("*.json"))
    assert {x["status"] for x in w.studio.post(post["id"])["variants"]} == {"draft"}
    assert post["idea"]["origin"]["type"] == "episode_short" and post["idea"]["origin"]["clip"] == "c01"


def test_the_library_records_where_the_render_came_from_and_a_human_still_has_to_attest(world):
    w = world
    r = ingest(w)
    lib = r["posts"][0]["library"]
    recs = store.read_assets(w.sp)
    render = recs[lib["renders"][0]]
    sources = {recs[s["asset"]]["origin"]["path"] for s in lib["sources"]}
    assert set(render["derived_from"]) == {s["asset"] for s in lib["sources"]} and sources >= {"raw/camA.mp4", "raw/camB.mp4"}
    assert all(recs[s["asset"]]["source"]["type"] == "own" for s in lib["sources"])
    cov = library.coverage(w.sp, [render["id"]])
    assert not cov["ok"] and all("not attested by a human" in m["why"] for m in cov["missing"])      # claims, not facts
    assert {u["asset"] for u in library.unattested(w.sp)} == set(render["derived_from"])
    attest_all(w)
    assert library.coverage(w.sp, [render["id"]])["ok"]
    assert library.verify(w.sp)["ok"]
    # the originals were copied, never moved
    assert (w.ep.root / "raw" / "camA.mp4").is_file()


def test_ingesting_twice_makes_no_second_post_and_adds_only_missing_variants(world):
    w = world
    first = ingest(w, accounts=["ig-main"])
    assert [v["account"] for v in first["posts"][0]["variants"]] == ["ig-main"]
    again = ingest(w)
    assert len(posts(w)) == 1 and again["posts"][0]["created"] is False and again["posts"][0]["post"] == first["posts"][0]["post"]
    status = {v["account"]: v["status"] for v in again["posts"][0]["variants"]}
    assert status == {"ig-main": "already there", "fb-page": "created", "yt-main": "created"}
    third = ingest(w)
    assert {v["status"] for v in third["posts"][0]["variants"]} == {"already there"} and len(posts(w)) == 1
    assert len([a for a in store.read_assets(w.sp).values() if a["source"]["type"] == "render"]) == 1       # identical bytes: one asset


def test_dry_run_checks_and_drafts_but_creates_nothing(world):
    w = world
    r = ingest(w, dry_run=True)
    assert r["dry_run"] and r["posts"][0]["would_create"] and len(r["posts"][0]["variants"]) == 3
    assert r["posts"][0]["variants"][0]["caption"].startswith("“")
    assert posts(w) == [] and store.read_assets(w.sp) == {}


# ---- copy: from the clip's transcript, in the show voice, within limits ----------------------------------------------------------------

def test_captions_quote_the_clip_verbatim_and_pass_the_voice_critic(world):
    w = world
    r = ingest(w)
    c = clip(w)
    post = store.load_post(w.sp, r["posts"][0]["post"])
    by = {v["platform"]: v for v in post["variants"]}
    for v in post["variants"]:
        cap = v["copy"]["caption"]
        quote = cap.split("“", 1)[1].split("”", 1)[0]
        assert I._contains(c["text"], quote), cap                                           # a sentence the speaker said, unchanged
        assert not v["copy"]["caption"].endswith("#")
    # youtube: the title is the first line (the studio queue takes a YouTube title from it); the others carry no title line
    assert by["youtube"]["copy"]["caption"].splitlines()[0] == "Why finishing is hard" and by["instagram"]["copy"]["caption"].startswith("“")
    # hashtags follow the package policy per platform and are stored without '#'
    pol = __import__("ave.package.collect", fromlist=["load_policy"]).load_policy(w.sp)["copy"]["max_hashtags"]
    for v in post["variants"]:
        tags = v["copy"]["hashtags"]
        assert len(tags) <= pol[v["platform"]] and all(not t.startswith("#") and t for t in tags)
    assert any(by["instagram"]["copy"]["hashtags"])
    # the studio's own critic finds nothing to fix
    assert [x for x in copycheck.critique_post(w.sp, post) if x["problems"]] == []


def test_the_hook_is_used_only_when_it_is_really_said_in_the_clip(world):
    w = world
    data = read_json(w.ep.work / "clips.json")
    data["clips"][0]["hook"] = "That is exactly right, and it is why we need systems that carry the work forward"
    write_json(w.ep.work / "clips.json", data)
    r = ingest(w, dry_run=True)
    assert "That is exactly right" in r["posts"][0]["variants"][0]["caption"]
    data["clips"][0]["hook"] = "Opens on a strong claim."
    write_json(w.ep.work / "clips.json", data)
    r = ingest(w, dry_run=True)
    assert "strong claim" not in r["posts"][0]["variants"][0]["caption"] and "Most people never finish" in r["posts"][0]["variants"][0]["caption"]


@pytest.mark.parametrize("limit,fits", [(50, True), (30, False)])
def test_a_quote_that_does_not_fit_the_platform_limit_gives_way_or_says_so(world, monkeypatch, limit, fits):
    from ave.publish.adapters import base
    real = base.load_platforms()
    monkeypatch.setattr(base, "load_platforms", lambda path=None: {**real, "x": {**real["x"], "post": {"caption_max_chars": limit}}})
    write_account(world.sp, "x-main", "x", post_types=["video"])
    r = ingest(world, accounts=["x-main"], dry_run=True)
    v = r["posts"][0]["variants"][0]
    assert v["platform"] == "x"
    if fits:
        assert len(v["caption"]) <= limit and v["caption"].startswith("“Most people never finish what they start.”") and not v["problems"]
    else:
        assert any("over the x limit" in p for p in v["problems"])                                  # said out loud, never silently cut


def test_a_title_the_critic_dislikes_is_reported_not_hidden(world):
    w = world
    (w.ep.work / "shorts" / "titles.json").write_text(json.dumps({"c01": "A game-changer for finishing"}))
    r = ingest(w, accounts=["yt-main"], dry_run=True)
    assert any("hype" in p for p in r["posts"][0]["variants"][0]["problems"])


def test_needs_review_from_the_short_is_carried_into_the_post(world):
    w = world
    r = ingest(w)
    post = store.load_post(w.sp, r["posts"][0]["post"])
    assert "needs_review" in post["idea"]["reason"] and r["warnings"] and "approved by Maren" in post["idea"]["reason"]


# ---- --sync: queue items, never an approval; the license closure gates them -------------------------------------------------------------

def test_sync_is_the_only_way_to_a_queue_item_and_it_waits_for_the_human_attestation(world):
    w = world
    r = ingest(w)
    assert not list((w.sp.root / "publish" / "queue").glob("*.json"))                         # without --sync: nothing is queued
    r2 = ingest(w, sync=True)
    assert r2["sync"]["approved"] is False
    rows = r2["sync"]["posts"][0]["variants"]
    assert {x["action"] for x in rows} == {"blocked"} and all(any("license" in p for p in x["problems"]) for x in rows)
    assert not list((w.sp.root / "publish" / "queue").glob("*.json"))                         # unattested claims: no queue item exists
    attest_all(w)
    r3 = ingest(w, sync=True)
    rows = r3["sync"]["posts"][0]["variants"]
    assert {x["action"] for x in rows} == {"created"} and {x["queue_status"] for x in rows} == {"awaiting_approval"}
    for f in (w.sp.root / "publish" / "queue").glob("*.json"):
        item = json.loads(f.read_text())
        assert item["status"] == "awaiting_approval" and not item.get("approvals") and "media_type" not in item and item["video"]
        assert item["account"] in w.accounts and item["post_type"] in ("reel", "short")
    assert r["posts"][0]["post"] == r3["posts"][0]["post"] and len(posts(w)) == 1


# ---- refusing what a human did not approve -------------------------------------------------------------------------------------------

def _nothing_was_made(w):
    assert posts(w) == [] and store.read_assets(w.sp) == {} and not list((w.sp.root / "publish" / "queue").glob("*.json"))


def test_a_proposed_clip_is_refused_with_a_clear_message_and_nothing_is_made(world):
    w = world
    data = read_json(w.ep.work / "clips.json")
    data["clips"][0]["status"] = "proposed"
    write_json(w.ep.work / "clips.json", data)
    r = ingest(w, clips=["c01"], sync=True)
    assert r["posts"] == [] and r["refused"][0]["clip"] == "c01" and r["refused"][0]["stage"] == "approval"
    assert "is proposed, not approved" in r["refused"][0]["reasons"][0] and "ave clips decide" in r["refused"][0]["reasons"][0]
    assert r["sync"] is None
    _nothing_was_made(w)
    r = ingest(w)                                                    # no --clips: only approved clips are even considered
    assert r["posts"] == [] and "no clip in this episode is approved yet" in r["refused"][0]["reasons"][0]
    _nothing_was_made(w)


def test_a_status_without_a_human_decision_on_record_is_not_trusted(world):
    w = world
    (w.ep.work / "learning" / "clip_decisions.jsonl").unlink()
    r = ingest(w, clips=["c01"])
    assert r["posts"] == [] and "no human decision on record" in r["refused"][0]["reasons"][0]
    _nothing_was_made(w)


def test_a_decision_typed_without_a_human_terminal_does_not_count(tmp_path):
    root = tmp_path / "agent-decided" / "demo"
    ep = F.demo_with_short(root, platforms="reels", tty=False)           # `ave clips decide` run through an agent shell: via notty
    sp = StudioProject(tmp_path / "studio")
    sp.create(title="S", tz="UTC")
    write_account(sp, "ig-main", "instagram", post_types=["reel"])
    studio = open_studio(sp.root, clock=FakeClock())
    r = I.shorts_ingest(studio, ep.root, clips=["c01"], by="claude")
    assert r["posts"] == [] and "without a human terminal" in r["refused"][0]["reasons"][0] and "via 'notty'" in r["refused"][0]["reasons"][0]
    assert "ave ui" in r["refused"][0]["reasons"][0] and store.list_posts(sp) == [] and store.read_assets(sp) == {}


def test_a_later_skip_overrides_an_earlier_keep(world):
    w = world
    with open(w.ep.work / "learning" / "clip_decisions.jsonl", "a") as f:
        f.write(json.dumps({"clip_id": "c01", "verdict": "skip", "by": "Maren", "via": "tty", "ts": "2026-10-20T02:00:00Z"}) + "\n")
    r = ingest(w, clips=["c01"])
    assert "latest decision" in r["refused"][0]["reasons"][0] and r["posts"] == []


def test_one_bad_clip_among_the_named_ones_refuses_the_whole_run(world):
    w = world
    data = read_json(w.ep.work / "clips.json")
    c2 = {**data["clips"][0], "id": "c02", "slug": "second-clip", "status": "proposed"}
    data["clips"].append(c2)
    write_json(w.ep.work / "clips.json", data)
    r = ingest(w, clips=["c01", "second-clip"])
    assert r["posts"] == [] and [x["clip"] for x in r["refused"]] == ["c02"] and "nothing was created" in r["note"]
    _nothing_was_made(w)
    ok = ingest(w)                                                    # without --clips the proposed one is just not considered
    assert len(ok["posts"]) == 1 and ok["posts"][0]["clip"] == "c01" and any(s.get("clip") == "c02" for s in ok["skipped"])


def test_an_unknown_clip_is_an_error(world):
    with pytest.raises(ValueError, match="unknown clip"):
        ingest(world, clips=["c99"])


# ---- refusing renders that are not ready --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("change,msg", [
    ({"draft": True}, "DRAFT"),
    ({"limits": {"ok": False, "problems": ["duration 200s exceeds max 180s"]}}, "platform limits"),
    ({"licenses": {"ok": False, "missing": ["raw/music.mp3"]}}, "licenses are not ok"),
    ({"encode": {"problems": ["pix_fmt yuv444p (want yuv420p)"]}}, "encode problems"),
    ({"completeness": {"complete": False, "override": True, "problems": ["starts mid-sentence"]}}, "--allow-incomplete"),
    ({"completeness": {}}, "complete-statement gate"),
])
def test_a_render_that_is_not_ready_is_refused_with_the_reason(world, change, msg):
    w = world
    edit_sidecars(w, **change)
    r = ingest(w, clips=["c01"])
    assert r["posts"] == [] and any(msg in x for x in r["refused"][0]["reasons"])
    _nothing_was_made(w)


def test_a_file_changed_after_its_export_is_refused(world):
    w = world
    f = w.ep.exports / "shorts" / f"{clip(w)['slug']}.v1.reels.mp4"
    f.write_bytes(f.read_bytes() + b"\0")
    r = ingest(w, clips=["c01"], accounts=["ig-main"])
    assert r["posts"] == [] and "no longer has the sha256" in r["refused"][0]["reasons"][0]


def test_a_newer_cut_without_an_export_is_never_posted_stale(world):
    w = world
    shorts.build_short(w.ep, clip(w), shorts.load_config(w.ep), detector=F.NO_FACE, force=True)
    assert editdoc.list_versions(w.ep, "short", clip(w)["slug"]) == [1, 2]
    r = ingest(w, clips=["c01"])
    assert r["posts"] == [] and any("a newer cut (short v2)" in x and "ave shorts export" in x for x in r["refused"][0]["reasons"])


def test_no_export_at_all_says_which_commands_to_run(world):
    w = world
    shutil.rmtree(w.ep.exports / "shorts")
    r = ingest(w, clips=["c01"])
    assert r["posts"] == [] and "no exported short" in r["refused"][0]["reasons"][0] and "ave shorts export" in r["refused"][0]["reasons"][0]


def test_a_draft_export_folder_is_never_used(world):
    w = world
    shutil.move(str(w.ep.exports / "shorts"), str(w.ep.exports / "shorts_drafts"))
    r = ingest(w, clips=["c01"])
    assert r["posts"] == [] and "no exported short" in r["refused"][0]["reasons"][0]


# ---- accounts and platforms ----------------------------------------------------------------------------------------------------------

def test_accounts_without_a_short_format_or_post_type_are_reported_not_guessed(world):
    w = world
    write_account(w.sp, "li-main", "linkedin", post_types=["text"])
    write_account(w.sp, "ig-feed", "instagram", post_types=["feed", "carousel"])
    write_account(w.sp, "tt-main", "tiktok", mode="inbox", post_types=["video"])
    r = ingest(w, accounts=["ig-main", "li-main", "ig-feed", "tt-main", "nope"])
    why = {(s.get("account")): s["why"] for s in r["skipped"] if s.get("account")}
    assert "no vertical-video short format" in why["li-main"] and "does not take post type 'reel'" in why["ig-feed"] and "no such studio account" in why["nope"]
    post = store.load_post(w.sp, r["posts"][0]["post"])
    assert sorted((v["account"], v["post_type"]) for v in post["variants"]) == [("ig-main", "reel"), ("tt-main", "video")]
    tt = next(v for v in post["variants"] if v["account"] == "tt-main")
    ig = next(v for v in post["variants"] if v["account"] == "ig-main")
    assert (tt["media"] or post["media"]) == (ig["media"] or post["media"])                  # TikTok posts the Reels encode


def test_an_export_that_was_not_made_for_a_platform_says_how_to_make_it(world):
    w = world
    for f in (w.ep.exports / "shorts").glob("*.facebook.*"):
        f.unlink()
    r = ingest(w)
    assert any(s.get("account") == "fb-page" and "ave shorts export" in s["why"] and "--platforms facebook" in s["why"] for s in r["skipped"])
    assert {v["account"] for v in r["posts"][0]["variants"]} == {"ig-main", "yt-main"}


# ---- B-roll placed in the short: the closure reaches the post ---------------------------------------------------------------------------

@needs_libass
def test_placed_broll_is_in_the_renders_derived_from_and_its_credit_is_in_the_caption(world):
    w = world
    c = clip(w)
    video = H.make_broll_video(w.tmp / "src" / "hands.mp4", blocks=8)
    asset = H.add_stock(w.sp, video, H.CREDIT_REQUIRED)
    place.place(w.ep, stage="short", name=c["slug"], asset=asset["id"], studio=w.sp.root, at=3.0, dur=1.5, apply=True, by="Maren")
    shorts.export_short(w.ep, c, ["reels", "youtube", "facebook"], shorts.load_config(w.ep))
    side = read_json(w.ep.exports / "shorts" / f"{c['slug']}.v2.reels.json")
    assert side["licenses"]["ok"] and side["licenses"]["library_assets"] == [asset["id"]] and "Clay hands by A. Maker on Pexels" in side["licenses"]["attribution"]
    r = ingest(w)
    assert r["refused"] == [] and r["posts"][0]["short_version"] == 2
    post = store.load_post(w.sp, r["posts"][0]["post"])
    render = store.read_assets(w.sp)[(post["variants"][0]["media"] or post["media"])[0]["asset"]]
    assert asset["id"] in render["derived_from"] and r["posts"][0]["library"]["placed"] == [asset["id"]]
    for v in post["variants"]:
        assert "Credits\nClay hands by A. Maker on Pexels" in v["copy"]["caption"]
    attest_all(w)
    assert library.coverage(w.sp, [render["id"]])["ok"]                                       # the stock asset was attested at its pick; the sources just now


@needs_libass
def test_broll_placed_from_another_studio_is_refused(world):
    w = world
    c = clip(w)
    other = H.make_studio(w.tmp, "other-studio")
    asset = H.add_stock(other, H.make_broll_video(w.tmp / "src" / "other.mp4"), H.STOCK)
    place.place(w.ep, stage="short", name=c["slug"], asset=asset["id"], studio=other.root, at=3.0, dur=1.5, apply=True, by="Maren")
    shorts.export_short(w.ep, c, ["reels"], shorts.load_config(w.ep))
    r = ingest(w, clips=["c01"], accounts=["ig-main"])
    assert r["posts"] == [] and any("another studio" in x for x in r["refused"][0]["reasons"])


# ---- the command ---------------------------------------------------------------------------------------------------------------------------

def run_cli(capsys, w, *argv):
    rc = cli.main(["studio", "ingest", "shorts", str(w.ep.root), "--studio", str(w.sp.root), *argv])
    cap = capsys.readouterr()
    try:
        res = json.loads(cap.out)
    except ValueError:
        res = None
    return rc, res, cap.err


def test_cli_dry_run_ingest_and_refusal(world, capsys, monkeypatch):
    from ave.studio.cli import OPEN_KW
    monkeypatch.setitem(OPEN_KW, "clock", world.clock)
    monkeypatch.setitem(OPEN_KW, "publishers", world.pubs)
    w = world
    rc, res, _ = run_cli(capsys, w, "--dry-run")
    assert rc == 0 and res["ok"] and res["dry_run"] and posts(w) == []
    rc, res, _ = run_cli(capsys, w, "--clips", "c01", "--accounts", "ig-main,fb-page", "--by", "Maren")
    assert rc == 0 and res["ok"] and len(res["posts"][0]["variants"]) == 2 and any("library attest" in x for x in res["next"])
    assert [p["id"] for p in posts(w)] == [res["posts"][0]["post"]] and posts(w)[0]["by"] == "Maren"
    data = read_json(w.ep.work / "clips.json")
    data["clips"][0]["status"] = "rejected"
    write_json(w.ep.work / "clips.json", data)
    rc, res, err = run_cli(capsys, w, "--clips", "c01")
    assert rc == 2 and res["ok"] is False and res["refused"][0]["reasons"] and "not approved" in err
    rc, res, err = run_cli(capsys, w, "--clips", "nope")
    assert rc == 2 and res["ok"] is False and "unknown clip" in res["error"]


def test_the_command_is_listed_and_never_a_human_act():
    from ave.commands.studio import command_names
    import argparse
    from ave.commands import studio as S
    p = argparse.ArgumentParser()
    ss = p.add_subparsers()
    S._build(ss)
    inner = [a for a in ss.choices["ingest"]._actions if isinstance(a, argparse._SubParsersAction)][0]
    assert "shorts" in inner.choices and "ingest" in command_names()
    src = Path(I.__file__).read_text()
    assert "require_human" not in src and "approve(" not in src.replace("never approves", "")          # it creates drafts; every approval stays the human's


def test_a_post_made_here_is_not_approved_by_anything_in_the_clock(world):
    w = world
    ingest(w)
    attest_all(w)
    ingest(w, sync=True)
    w.clock.advance(days=3)
    tick = w.studio.tick()
    assert tick.get("sent", 0) == 0 and all(not p.sent for p in w.pubs.values())                 # nothing posts: no approval, no live account
    assert iso_utc(w.clock.now())


# ---- the recipe -----------------------------------------------------------------------------------------------------------------------------

def test_the_recipe_walks_the_loop_and_stops_at_every_human_step(world, capsys, monkeypatch):
    from ave.studio import recipes
    from ave.studio.cli import OPEN_KW
    monkeypatch.setitem(OPEN_KW, "clock", world.clock)
    monkeypatch.setitem(OPEN_KW, "publishers", world.pubs)
    w = world
    args = {"episode": str(w.ep.root)}

    def check():
        return recipes.check(w.studio, "episode-shorts-to-posts", args)

    res = check()
    assert res["problems"] == [] and res["next"]["id"] == "survey" and not res["needs_human"]
    assert cli.main(["studio", "ingest", "episode", str(w.ep.root), "--studio", str(w.sp.root)]) == 0
    capsys.readouterr()
    res = check()
    assert [s["state"] for s in res["steps"]][:4] == ["done", "done", "done", "next"]                  # the human's keep and the exports already exist on disk
    assert res["next"]["id"] == "ingest" and res["next"]["skill"] == "ave-studio" and any("--dry-run" in c for c in res["next"]["checklist"])
    ingest(w)
    res = check()
    assert res["next"]["id"] == "attest" and res["needs_human"] is True and res["next"]["run"].startswith("! ave studio library attest --all")
    attest_all(w)
    res = check()
    assert res["next"]["id"] == "sync" and res["next"]["command"].startswith("ave studio queue sync") and not res["needs_human"]
    w.studio.sync_queue(posts(w)[0]["id"])
    res = check()
    assert res["next"]["id"] == "review" and res["needs_human"] is True and "studio ui" in res["next"]["run"] and not res["complete"]      # nothing was approved
