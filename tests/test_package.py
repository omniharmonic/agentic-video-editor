"""`ave package`: the release package. Synthetic episodes only; every branch: complete, missing parts, unapproved excluded, idempotent/v2,
manifest hashes, post-copy limits, free space, queue (which never sends)."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ave import state
from ave.cli import main
from ave.episode import Episode, read_json
from ave.package import audio as A, build as B, chapters as C, collect as K, postcopy as P, queue as PQ
from ave.publish import queue as pubq
from package_helpers import (LONG_QUOTE, QUOTE, add_clips, add_content, add_final, add_short, final_video, full_episode, make_episode,  # noqa: F401
                             noisy_png, png, sha, tree)
from publish_helpers import RecordingPublisher, isolated_secrets, video_pool  # noqa: F401

needs_lame = pytest.mark.skipif(not A.encoder_available("mp3"), reason="ffmpeg has no libmp3lame")


@pytest.fixture
def full(tmp_path, final_video, video_pool):
    return full_episode(tmp_path / "ep", final_video, video_pool)


def build(ep, **kw):
    kw.setdefault("date", "2026-10-14")
    return B.build(ep, **kw)


def manifest(rep) -> dict:
    return read_json(Path(rep["package_dir"]) / "manifest.json")


def paths(m, draft=False):
    return {f["package_path"] for f in m["files"] if f["draft"] == draft}


# ---- a complete episode ------------------------------------------------------------------------

@needs_lame
def test_complete_episode_builds_every_part(full):
    rep = build(full)
    assert rep["status"] == "built" and rep["complete"] is True
    pkg = Path(rep["package_dir"])
    assert pkg == full.exports / "package" / "2026-10-14"
    m = manifest(rep)
    main_files = paths(m)
    assert "episode/final.v1.master.mp4" in main_files
    assert {"captions/final.v1.srt", "captions/final.v1.vtt"} <= main_files
    assert any(p.startswith("audio/") and p.endswith(".mp3") for p in main_files) and "audio/chapters.txt" in main_files
    assert "thumbnails/thumb.png" in main_files
    assert {"shorts/youtube/clip-c01.v1.youtube.mp4", "shorts/reels/clip-c01.v1.reels.mp4"} <= main_files
    assert any(p.startswith("images/quote-q1/") for p in main_files) and "images/carousel-c1/slide-03.png" in main_files
    assert {"copy/youtube.md", "copy/instagram.md", "copy/x.md", "copy/linkedin.md", "copy/tiktok.md", "copy/threads.md", "copy/bluesky.md",
            "copy/facebook.md", "copy/copy.json", "copy/alt-text.md"} <= main_files
    for name in ("manifest.json", "index.html", "README.md", "checksums.sha256"):
        assert (pkg / name).is_file()
    assert [c for c in m["checklist"] if c["state"] == "missing"] == []
    vtt = (pkg / "captions" / "final.v1.vtt").read_text()
    assert vtt.startswith("WEBVTT") and "00:00:01.000 --> 00:00:03.500" in vtt


@needs_lame
def test_podcast_audio_has_id3_chapters_and_is_measured_against_minus_16_lufs(full):
    rep = build(full)
    m = manifest(rep)
    mp3 = Path(rep["package_dir"]) / next(p for p in paths(m) if p.endswith(".mp3"))
    chs = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_chapters", str(mp3)], capture_output=True, text=True).stdout)["chapters"]
    assert [c["tags"]["title"] for c in chs] == ["Opening", "Soil and habit", "What stays"]
    assert [round(float(c["start_time"])) for c in chs] == [0, 15, 28]
    lo = m["audio"]["loudness"]
    assert lo["target_lufs"] == -16.0 and abs(lo["lufs"] + 16.0) <= 1.0 and lo["ok"] is True
    assert next(c for c in m["checklist"] if c["key"] == "podcast_audio")["state"] == "ok"
    assert (Path(rep["package_dir"]) / "audio" / "chapters.txt").read_text() == "0:00 Opening\n0:15 Soil and habit\n0:28 What stays\n"


def test_index_html_is_self_contained_with_players_and_copy_buttons(full):
    rep = build(full, include={"video", "captions", "thumbnails", "shorts", "images", "copy", "drafts"})
    html = (Path(rep["package_dir"]) / "index.html").read_text()
    assert "<video controls" in html and "<img " in html and "copyText(" in html and "Copy</button>" in html
    assert "episode/final.v1.master.mp4" in html and "DRAFT" in html
    assert not re.search(r"""(?:src|href)=['"]https?://""", html) and "<link" not in html          # nothing loads from the network
    assert "MB" in html or "KB" in html                                                          # file sizes shown
    readme = (Path(rep["package_dir"]) / "README.md").read_text()
    assert "Nothing in this package has been sent" in readme and "shasum -a 256 -c checksums.sha256" in readme


# ---- manifest ----------------------------------------------------------------------------------

def test_manifest_lists_every_file_with_matching_size_and_sha256(full):
    rep = build(full, include={"video", "captions", "thumbnails", "shorts", "images", "copy", "drafts"})
    pkg = Path(rep["package_dir"])
    m = manifest(rep)
    on_disk = {str(p.relative_to(pkg)) for p in pkg.rglob("*") if p.is_file()}
    listed = {f["path"] for f in m["files"]}
    assert listed == on_disk - {"manifest.json", "checksums.sha256", "index.html", "README.md"}
    for f in m["files"]:
        assert sha(pkg / f["path"]) == f["sha256"] and (pkg / f["path"]).stat().st_size == f["bytes"]
        assert {"approval", "source", "license", "draft", "copied"} <= set(f)
    v = next(f for f in m["files"] if f["role"] == "episode_video")
    assert v["source"]["stage"] == "final" and v["source"]["version"] == 1 and v["duration_s"] and v["approval"]["status"] == "unrecorded"
    assert next(f for f in m["files"] if f["role"] == "carousel_slide")["alt_text"].startswith("Slide")
    lines = (pkg / "checksums.sha256").read_text().strip().splitlines()
    assert len(lines) == len(m["files"])
    r = subprocess.run(["shasum", "-a", "256", "-c", "checksums.sha256"], cwd=pkg, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_latest_final_render_is_the_one_packaged(tmp_path, final_video):
    ep = make_episode(tmp_path / "ep")
    add_final(ep, final_video, version=1, chapters=False)
    add_final(ep, final_video, version=3, chapters=False)
    (ep.exports / "final.v3.proxy.mp4").write_bytes(b"proxy")
    m = manifest(build(ep, include={"video"}))
    assert [f["package_path"] for f in m["files"] if f["role"] == "episode_video"] == ["episode/final.v3.master.mp4"]
    assert m["files"][0]["source"]["version"] == 3


# ---- missing pieces are a checklist, not a failure ---------------------------------------------

def test_empty_episode_gives_a_clear_checklist_and_still_builds(tmp_path):
    ep = make_episode(tmp_path / "ep")
    rep = build(ep)
    assert rep["status"] == "built" and rep["complete"] is False
    labels = {c["key"]: c for c in rep["checklist"]}
    assert "final cut not rendered" in labels["final_cut"]["detail"] and labels["final_cut"]["state"] == "missing"
    assert "no SRT" in labels["captions"]["detail"] and "no approved clips" in labels["shorts"]["detail"]
    assert labels["thumbnails"]["state"] == "missing" and labels["images"]["state"] == "missing"
    assert set(rep["missing_required"]) >= {"final_cut", "captions", "podcast_audio", "thumbnails"}
    assert (Path(rep["package_dir"]) / "README.md").is_file()


def test_cli_dry_run_reports_the_gaps_and_writes_nothing(tmp_path, capsys):
    ep = make_episode(tmp_path / "ep")
    before = tree(ep.root)
    rc = main(["package", str(ep.root), "--dry-run", "--date", "2026-10-14"])
    cap = capsys.readouterr()
    out = json.loads(cap.out)
    assert rc == 0 and out["status"] == "dry-run" and "package size" in cap.err and "DRY RUN" in cap.err
    assert out["volume"]["free_bytes"] > 0 and any("final cut not rendered" in c["detail"] for c in out["checklist"])
    assert tree(ep.root) == before and not (ep.exports / "package").exists() and not (ep.work / "package").exists()


def test_dry_run_with_write_status_writes_only_the_ui_status_file(tmp_path):
    ep = make_episode(tmp_path / "ep")
    before = tree(ep.root)
    build(ep, dry_run=True, write_status=True)
    after = tree(ep.root)
    new = set(after) - set(before)
    assert new == {"work/package/status.json"}
    st = read_json(ep.work / "package" / "status.json")
    assert st["built"] is False and st["dry_run"] is True and any(c["key"] == "final_cut" for c in st["checklist"])


def test_strict_exit_code(tmp_path):
    ep = make_episode(tmp_path / "ep")
    assert main(["package", str(ep.root), "--dry-run", "--strict"]) == 2
    assert main(["package", str(ep.root), "--dry-run"]) == 0


def test_only_per_session_finals_is_flagged_not_hidden(tmp_path, final_video):
    ep = make_episode(tmp_path / "ep")
    ep.exports.mkdir(parents=True, exist_ok=True)
    shutil.copy(final_video, ep.exports / "final.s1.v1.master.mp4")
    rep = build(ep, include={"video"})
    c = next(c for c in rep["checklist"] if c["key"] == "final_cut")
    assert c["state"] == "warn" and "per-session" in c["detail"]
    assert manifest(rep)["files"][0]["source"]["session"] == "s1"


# ---- unapproved items live only in drafts/ -----------------------------------------------------

def test_unapproved_clips_and_content_are_only_in_drafts_with_a_draft_mark(tmp_path, final_video, video_pool):
    ep = make_episode(tmp_path / "ep")
    add_final(ep, final_video)
    add_clips(ep, {"c01": "proposed", "c02": "rejected"})
    add_short(ep, video_pool / "vertical.mp4", "c01", "youtube")
    add_content(ep, thumb=png(ep.work / "content" / "renders" / "t.png", (1280, 720)), status="draft", cards="draft", carousel="draft")
    rep = build(ep, include={"video", "thumbnails", "shorts", "images", "copy", "drafts"})
    pkg = Path(rep["package_dir"])
    m = manifest(rep)
    main_p, draft_p = paths(m), paths(m, draft=True)
    assert not any(p.startswith(("shorts/", "thumbnails/", "images/")) for p in main_p)
    assert {"drafts/shorts/youtube/DRAFT-clip-c01.v1.youtube.mp4", "drafts/thumbnails/DRAFT-t.png"} <= draft_p
    assert any(p.startswith("drafts/images/carousel-c1/DRAFT-slide-0") for p in draft_p)
    assert (pkg / "drafts" / "DRAFT.txt").read_text().startswith("DRAFT")
    sh = next(f for f in m["files"] if f["role"] == "short")
    assert sh["draft"] and "proposed, not approved" in sh["draft_reasons"][0] and sh["approval"]["status"] == "proposed"
    assert next(c for c in m["checklist"] if c["key"] == "drafts")["state"] == "draft"
    assert "DRAFT" in (pkg / "index.html").read_text()
    # the post copy never mentions an unapproved quote
    assert QUOTE not in (pkg / "copy" / "x.md").read_text()
    c = next(c for c in m["checklist"] if c["key"] == "shorts")
    assert c["state"] == "missing" and "no approved clips" in c["detail"]            # the export exists but nothing is approved


def test_rejected_content_is_not_packaged_at_all(tmp_path):
    ep = make_episode(tmp_path / "ep")
    add_content(ep, thumb=png(ep.work / "content" / "renders" / "t.png", (1280, 720)), status="rejected", cards="rejected", carousel="rejected")
    m = manifest(build(ep, include={"thumbnails", "images", "drafts"}))
    assert not [f for f in m["files"] if f["role"] in ("thumbnail", "quote_card", "carousel_slide")]


def test_failed_short_limits_go_to_drafts_even_when_the_clip_is_approved(tmp_path, video_pool):
    ep = make_episode(tmp_path / "ep")
    add_clips(ep, {"c01": "approved"})
    add_short(ep, video_pool / "vertical.mp4", "c01", "youtube", ok=False)
    m = manifest(build(ep, include={"shorts", "drafts"}))
    f = next(f for f in m["files"] if f["role"] == "short")
    assert f["draft"] and "limits not met" in f["draft_reasons"][0]


def test_include_without_drafts_leaves_them_out_entirely(full):
    m = manifest(build(full, include={"shorts", "thumbnails"}))
    assert not paths(m, draft=True) or paths(m, draft=True) == set()
    assert not any("c02" in p for p in paths(m))


def test_latest_short_version_per_clip_and_platform(tmp_path, video_pool):
    ep = make_episode(tmp_path / "ep")
    add_clips(ep, {"c01": "approved"})
    add_short(ep, video_pool / "vertical.mp4", "c01", "youtube", ver=1)
    add_short(ep, video_pool / "vertical.mp4", "c01", "youtube", ver=2)
    m = manifest(build(ep, include={"shorts"}))
    assert [f["package_path"] for f in m["files"] if f["role"] == "short"] == ["shorts/youtube/clip-c01.v2.youtube.mp4"]


# ---- thumbnails --------------------------------------------------------------------------------

def test_thumbnail_rules_wrong_size_draft_oversize_becomes_a_jpeg(tmp_path):
    ep = make_episode(tmp_path / "ep")
    wrong = png(ep.work / "content" / "renders" / "w.png", (1000, 600))
    add_content(ep, thumb=wrong)
    m = manifest(build(ep, include={"thumbnails", "drafts"}))
    t = next(f for f in m["files"] if f["role"] == "thumbnail")
    assert t["draft"] and "1000x600" in t["draft_reasons"][0]

    ep2 = make_episode(tmp_path / "ep2")
    big = noisy_png(ep2.work / "content" / "renders" / "big.png")
    assert big.stat().st_size > 2 * 1024 * 1024
    add_content(ep2, thumb=big)
    rep = build(ep2, include={"thumbnails"})
    m2 = manifest(rep)
    t2 = next(f for f in m2["files"] if f["role"] == "thumbnail")
    assert not t2["draft"] and t2["package_path"] == "thumbnails/big.jpg" and t2["bytes"] < 2 * 1024 * 1024 and "JPEG" in t2["flag"]


def test_image_that_changed_since_review_is_a_draft(tmp_path):
    ep = make_episode(tmp_path / "ep")
    add_content(ep)
    png(ep.work / "content" / "renders" / "quote-q1-4x5.png", (1080, 1350), (99, 99, 99))      # edited after approval
    m = manifest(build(ep, include={"images", "drafts"}))
    q = next(f for f in m["files"] if f["role"] == "quote_card")
    assert q["draft"] and "changed since it was reviewed" in q["draft_reasons"][0]


# ---- idempotent, versioned, never overwritten --------------------------------------------------

def test_rerun_with_nothing_changed_is_unchanged_and_creates_nothing(full):
    first = build(full, include={"video", "captions", "shorts", "copy"})
    before = tree(full.exports / "package")
    again = build(full, include={"video", "captions", "shorts", "copy"})
    assert again["status"] == "unchanged" and again["package_dir"] == first["package_dir"]
    assert tree(full.exports / "package") == before


def test_a_change_makes_v2_and_leaves_v1_untouched(full):
    inc = {"video", "captions", "shorts", "copy"}
    v1 = build(full, include=inc)
    snap = {p: sha(Path(v1["package_dir"]) / p) for p in tree(Path(v1["package_dir"]))}
    (full.exports / "final.v1.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nChanged.\n", encoding="utf-8")
    v2 = build(full, include=inc)
    assert v2["status"] == "built" and Path(v2["package_dir"]).name == "2026-10-14-v2" and v2["package_version"] == 2
    assert {p: sha(Path(v1["package_dir"]) / p) for p in tree(Path(v1["package_dir"]))} == snap        # v1 byte-for-byte as it was
    assert "Changed." in (Path(v2["package_dir"]) / "captions" / "final.v1.srt").read_text()
    v3 = build(full, include=inc, new_version=True)                    # forced, though nothing changed since v2
    assert Path(v3["package_dir"]).name == "2026-10-14-v3"


def test_a_newly_approved_clip_changes_the_fingerprint(full):
    inc = {"shorts", "copy"}
    build(full, include=inc)
    add_clips(full, {"c01": "approved", "c02": "approved"})
    v2 = build(full, include=inc)
    assert v2["status"] == "built" and any("clip-c02" in f["package_path"] and not f["draft"] for f in manifest(v2)["files"])


def test_a_different_date_is_a_different_folder(full):
    a = build(full, include={"copy"}, date="2026-10-14")
    b = build(full, include={"copy"}, date="2026-10-15")
    assert Path(a["package_dir"]).name == "2026-10-14" and Path(b["package_dir"]).name == "2026-10-15" and b["status"] == "built"


def test_a_crashed_build_leaves_no_package_behind(full, monkeypatch):
    monkeypatch.setattr(B, "_copy_hash", lambda *a, **k: (_ for _ in ()).throw(OSError("disk went away")))
    with pytest.raises(OSError):
        build(full, include={"video"})
    assert list((full.exports / "package").iterdir()) == []


# ---- free space: copy when it fits, reference otherwise ----------------------------------------

def test_not_enough_room_references_the_big_files_with_a_flag_and_copies_the_small(full, monkeypatch):
    real = shutil.disk_usage
    video_size = (full.exports / "final.v1.master.mp4").stat().st_size
    reserve = K.load_policy()["disk"]["reserve_bytes"]
    monkeypatch.setattr(B.shutil, "disk_usage", lambda p: type("U", (), {"free": reserve + B.GENERATED_TEXT_ESTIMATE + video_size // 2, "total": 1, "used": 0})())
    rep = build(full, include={"video", "captions", "copy"})
    m = manifest(rep)
    v = next(f for f in m["files"] if f["role"] == "episode_video")
    assert v["copied"] is False and v["path"] is None and v["reference"].endswith("final.v1.master.mp4") and "NOT COPIED" in v["flag"]
    assert v["sha256"] == sha(full.exports / "final.v1.master.mp4")
    assert next(f for f in m["files"] if f["role"] == "captions_srt")["copied"] is True
    assert not (Path(rep["package_dir"]) / "episode").exists()
    assert (Path(rep["package_dir"]) / "checksums.referenced.sha256").read_text().strip().endswith("final.v1.master.mp4")
    assert next(c for c in m["checklist"] if c["key"] == "volume")["state"] == "warn" and m["volume"]["copied_everything"] is False
    assert "(referenced)" in (Path(rep["package_dir"]) / "index.html").read_text()
    assert real  # the real function is untouched outside the test


def test_nothing_is_linked(full):
    rep = build(full, include={"video", "shorts"})
    assert not [p for p in Path(rep["package_dir"]).rglob("*") if p.is_symlink()]
    assert all(p.stat().st_nlink == 1 for p in Path(rep["package_dir"]).rglob("*") if p.is_file())


# ---- post copy: voice, limits ------------------------------------------------------------------

def facts(quotes, title="The Long Table 00", show="The Long Table", people=("Maren Okafor", "Tomas"), chapters=()):
    meta = {"title": title, "show": show, "speakers": {f"S{i}": {"name": n} for i, n in enumerate(people, 1)}}
    return P.make_facts(meta, [{"text": q, "speaker": "Maren Okafor", "source": "t"} for q in quotes], [], list(chapters), [])


@pytest.fixture
def cfg():
    from ave.publish.adapters.base import load_platforms
    return load_platforms()


@pytest.fixture
def policy():
    return K.load_policy()


@pytest.mark.parametrize("plat,limit", [("x", 280), ("bluesky", 300), ("threads", 500), ("instagram", 2200), ("linkedin", 3000), ("tiktok", 2200), ("facebook", 5000)])
def test_every_platform_post_respects_its_limit_from_platforms_yaml(plat, limit, cfg, policy):
    assert P.limit_for(plat, cfg)[0] == limit
    for qs in ([QUOTE], [LONG_QUOTE, QUOTE], [LONG_QUOTE * 8]):
        b = P.compose_post(plat, facts(qs), cfg, policy)
        assert b["chars"] <= limit or any("over the" in p for p in b["problems"])
        if b["chars"] <= limit:
            assert not any("over the" in p for p in b["problems"])


def test_a_quote_is_never_shortened_a_shorter_one_is_used_or_none(cfg, policy):
    b = P.compose_post("x", facts([LONG_QUOTE * 2, QUOTE]), cfg, policy)
    assert b["quote"] == QUOTE and f"“{QUOTE}”" in b["text"] and b["chars"] <= 280
    b2 = P.compose_post("x", facts([LONG_QUOTE * 3]), cfg, policy)
    assert b2["quote"] is None and "…" not in b2["text"] and "no approved quote fits" in " ".join(b2["problems"])
    assert LONG_QUOTE[:40] not in b2["text"]


def test_no_quote_yet_is_reported_not_invented(cfg, policy):
    b = P.compose_post("instagram", facts([]), cfg, policy)
    assert "“" not in b["text"] and any("no approved quote" in p for p in b["problems"]) and "The Long Table 00" in b["text"]


def test_hashtags_are_few_and_the_url_is_a_placeholder_to_fill(cfg, policy):
    b = P.compose_post("instagram", facts([QUOTE]), cfg, policy)
    tags = re.findall(r"#\w+", b["text"])
    assert tags and tags[0] == "#TheLongTable" and len(tags) <= policy["copy"]["max_hashtags"]["instagram"]
    assert "{{EPISODE_URL}}" in b["text"] and P.placeholders([b]) == ["{{EPISODE_URL}}"]
    assert len(re.findall(r"#\w+", P.compose_post("x", facts([QUOTE]), cfg, policy)["text"])) <= 1


def test_the_critic_flags_cheesy_corporate_copy_but_not_the_speakers_own_words(policy):
    bad = "We're thrilled to announce a game-changer! Unlock your potential 🚀 and TUNE IN. Ready to dive in?"
    probs = " ".join(P.critique(bad, "linkedin", policy))
    for needle in ("thrilled", "game-changer", "unlock", "dive in", "tune in", "exclamation", "emoji", "capitals", "question"):
        assert needle in probs.lower(), needle
    assert P.critique("“What a game-changer this is!” — Tomas\n\nThe Long Table 00. With Maren Okafor and Tomas.", "linkedin", policy) == []
    assert P.critique("#a #b #c #d", "x", policy)                                    # too many hashtags for x


def test_youtube_copy_title_description_tags_chapters_within_limits(cfg, policy):
    chapters = [{"start": 0, "title": "Opening"}, {"start": 15, "title": "Soil and habit"}, {"start": 28, "title": "What stays"}]
    blk, probs = C.youtube_block(chapters, policy)
    blocks = P.compose_youtube(facts([QUOTE], chapters=chapters), cfg, policy, blk, probs)
    t, d, tg = blocks
    assert t["text"] == "The Long Table 00" and t["chars"] <= 100 and any(QUOTE in o for o in t["options"])
    assert "0:00 Opening" in d["text"] and "0:15 Soil and habit" in d["text"] and d["chars"] <= 5000 and d["unit"] == "bytes"
    assert "TODO:" in d["text"] and "{{LINKS}}" in d["text"] and len(tg["text"]) <= 500 and "The Long Table" in tg["text"]
    assert not d["problems"] or all("limit" not in p for p in d["problems"])


def test_youtube_chapters_need_three_and_a_zero_start(policy):
    _, probs = C.youtube_block([{"start": 0, "title": "a"}, {"start": 30, "title": "b"}], policy)
    assert probs and "at least 3" in probs[0]
    norm = C.normalise([{"start": 4, "title": "a"}, {"start": 9, "title": "b"}, {"start": 60, "title": "c"}], 100)
    assert [c["start"] for c in norm] == [0.0, 60] and norm[0]["title"] == "a"


def test_package_copy_files_have_counts_and_the_json_is_the_queue_source(full):
    rep = build(full, include={"video", "shorts", "images", "copy"})
    pkg = Path(rep["package_dir"])
    cj = read_json(pkg / "copy" / "copy.json")
    assert cj["draft"] is True and "{{EPISODE_URL}}" in cj["placeholders"]
    kinds = {b["kind"] for b in cj["blocks"]}
    assert {"youtube_title", "youtube_description", "youtube_tags", "episode_post", "short_post", "image_post"} <= kinds
    assert all(b["id"] and b["platform"] and "problems" in b for b in cj["blocks"])
    x_md = (pkg / "copy" / "x.md").read_text()
    assert "DRAFT" in x_md and "of 280 chars" in x_md
    for b in cj["blocks"]:
        if b.get("limit"):
            assert b["chars"] <= b["limit"], (b["id"], b["problems"])
    assert next(c for c in rep["checklist"] if c["key"] == "post_copy")["state"] == "warn"            # placeholders to fill


# ---- chapters ----------------------------------------------------------------------------------

def topic_transcript(topics, per_topic_s=400, turn_s=20):
    utt, t = [], 0.0
    for words in topics:
        end = t + per_topic_s
        i = 0
        while t < end:
            utt.append({"speaker": "S1", "start": t, "end": t + turn_s, "text": " ".join(words[(i + k) % len(words)] for k in range(14)) + "."})
            t += turn_s
            i += 3
    return {"utterances": utt, "duration": t}


TOPICS = [["soil", "compost", "worms", "nutrients", "fungal", "roots", "garden", "mulch", "harvest", "seeds"],
          ["budget", "treasury", "ledger", "invoices", "payroll", "accountant", "taxes", "revenue", "dollars", "grants"],
          ["river", "watershed", "salmon", "dam", "estuary", "floods", "creek", "wetland", "beaver", "channel"]]


def test_topic_breaks_are_found_near_the_real_topic_changes(policy):
    br = C.derive_breaks(topic_transcript(TOPICS), policy)
    starts = [b["start"] for b in br]
    assert len(br) == 3 and starts[0] == 0 or starts[0] < 60
    assert any(abs(s - 400) <= 60 for s in starts) and any(abs(s - 800) <= 60 for s in starts)
    assert "soil" in br[0]["title"].lower() or "compost" in br[0]["title"].lower() or "worm" in br[0]["title"].lower()
    assert C.derive_breaks({"utterances": topic_transcript(TOPICS)["utterances"][:6]}, policy) == []          # too short to split


def test_derived_chapters_are_mapped_into_final_video_time_and_marked_draft(tmp_path, policy):
    from ave import editdoc
    from ave.episode import write_json
    ep = make_episode(tmp_path / "ep")
    write_json(ep.work / "transcript.json", topic_transcript(TOPICS))
    # the final removed 100 s after the first 300 s: later episode times land 100 s earlier in the video
    doc = editdoc.new_doc("final", 10, (640, 360), {"cam": {"kind": "video", "file": "x", "start_offset": 0.0}}, duration=1100.0)
    doc["tracks"]["dialogue"] = [{"id": "a", "src": "cam", "e0": 0.0, "o0": 0.0, "o1": 300.0}, {"id": "b", "src": "cam", "e0": 400.0, "o0": 300.0, "o1": 1100.0}]
    res = C.build(ep, doc, 1100.0, policy)
    assert res["draft_titles"] is True and res["source"] == "transcript topic breaks" and "machine drafts" in res["note"]
    starts = [c["start"] for c in res["chapters"]]
    assert starts[0] == 0.0 and any(abs(s - 300) <= 60 for s in starts)          # the break at episode 400 is at video 300
    assert C.episode_to_output(doc, 350.0) == 300.0 and C.episode_to_output(doc, 450.0) == 350.0
    write_json(ep.work / "chapters.json", [{"start": 0, "title": "Mine"}, {"start": 500, "title": "Also mine"}])
    given = C.build(ep, doc, 1100.0, policy)
    assert given["source"] == "chapters.json" and given["draft_titles"] is False and given["chapters"][0]["title"] == "Mine"


def test_no_transcript_and_no_chapter_file_is_a_clear_gap(tmp_path, policy):
    ep = make_episode(tmp_path / "ep")
    assert "no chapters" in C.build(ep, None, 100.0, policy)["note"]


def test_ffmetadata_escapes_and_orders_chapters():
    txt = C.ffmetadata([{"start": 0, "title": "A=B; C"}, {"start": 12.5, "title": "Next"}], 30.0, {"title": "T"})
    assert txt.startswith(";FFMETADATA1") and "title=A\\=B\\; C" in txt and "START=12500" in txt and "END=30000" in txt


# ---- status for the UI, and the pipeline stage -------------------------------------------------

def test_status_json_and_pipeline_stage(full):
    st = state.status(full)
    row = next(r for r in st["stages"] if r["stage"] == "package")
    assert row["optional"] and not row["done"] and row["command"].startswith("ave package ")
    build(full, dry_run=True)
    assert not next(r for r in state.status(full)["stages"] if r["stage"] == "package")["done"]          # a dry run is not a build
    build(full, include={"video", "copy"})
    assert next(r for r in state.status(full)["stages"] if r["stage"] == "package")["done"]
    s = read_json(full.work / "package" / "status.json")
    assert s["built"] is True and s["package_dir_rel"] == "exports/package/2026-10-14" and s["checklist"] and "queue_hint" in s
    assert "Release package" in state.briefing(full)


def test_numbered_layout_puts_the_package_in_06_exports(tmp_path, final_video):
    ep = make_episode(tmp_path / "ep", layout="numbered")
    add_final(ep, final_video, chapters=False)
    rep = build(ep, include={"video", "captions"})
    assert Path(rep["package_dir"]) == ep.root / "06_EXPORTS" / "package" / "2026-10-14"
    assert read_json(ep.root / "01_PROJECT" / "work" / "package" / "status.json")["built"] is True


def test_bad_include_is_refused():
    with pytest.raises(SystemExit, match="--include takes"):
        B.parse_include("video,nonsense")


# ---- queue: drafts only, nothing sent ----------------------------------------------------------

def fakes():
    return {p: RecordingPublisher(p) for p in ("youtube", "facebook", "instagram", "x", "tiktok")}


@pytest.fixture
def built(full):
    return build(full, include={"video", "captions", "shorts", "images", "copy", "drafts"})


def test_queue_adds_only_approved_items_as_unapproved_drafts_and_never_sends(full, built):
    pubs = fakes()
    rep = PQ.queue_package(full, ai_disclosure=False, publishers=pubs)
    assert rep["refused"] == [] and rep["queued"]
    items = pubq.list_items(full)
    assert len(items) == len(rep["queued"])
    assert all(i["status"] in ("draft", "awaiting_approval") and i["approvals"] == [] and i["visibility"] == "private" and i["results"] == {} for i in items)
    assert all(p.publish_calls == 0 for p in pubs.values())
    vids = {i["platforms"][0] for i in items if pubq.media_type(i) == "video"}
    assert {"youtube", "facebook", "instagram"} <= vids
    yt = next(i for i in items if i["platforms"] == ["youtube"] and "final.v1" in i["video"])
    assert yt["title"] == "The Long Table 00" and "0:15 Soil and habit" in yt["description"] and yt["ai_disclosure"] is False
    assert not any("c02" in (i.get("video") or "") or "DRAFT" in str(i.get("files") or "") for i in items)           # drafts never queued
    carousel = next(i for i in items if i.get("media_type") == "carousel") if any(i.get("media_type") == "carousel" for i in items) else None
    assert carousel and len(carousel["files"]) == 3 and carousel["alt_texts"] == ["Slide 1", "Slide 2", "Slide 3"]
    assert all(i["origin"]["package"] == "2026-10-14" for i in items)
    assert "Nothing was approved or sent" in rep["next"]


def test_queue_hash_binds_to_the_packaged_bytes_and_approval_stays_human(full, built):
    PQ.queue_package(full, ai_disclosure=False, publishers=fakes())
    yt = next(i for i in pubq.list_items(full) if i["platforms"] == ["youtube"] and "final.v1" in i["video"])
    pkg_file = Path(built["package_dir"]) / "episode" / "final.v1.master.mp4"
    assert yt["sha256"] == sha(pkg_file) == sha(full.exports / "final.v1.master.mp4")
    assert yt["status"] == "awaiting_approval"
    with pytest.raises(ValueError, match="confirmation hash"):
        pubq.approve(full, yt["id"], "ben", "deadbeef", publishers=fakes())      # only the hash prefix of the exact file approves
    run = pubq.run(full, yt["id"], send=False, publishers=fakes())
    assert run["mode"] == "dry-run" and run["ok"] is False and run["blocked"]       # not approved: even the dry run says so


def test_queue_without_an_ai_answer_leaves_youtube_as_a_draft_with_the_problem(full, built):
    PQ.queue_package(full, platforms=["youtube"])                     # the real YouTube preflight (pure: no network)
    yt = next(i for i in pubq.list_items(full) if i["platforms"] == ["youtube"] and "final.v1" in i["video"])
    assert yt["status"] == "draft" and any("ai_disclosure" in p for p in yt["preflight_problems"])


def test_queue_is_idempotent_and_dry_run_writes_nothing(full, built):
    dry = PQ.queue_package(full, ai_disclosure=False, publishers=fakes(), dry_run=True)
    assert dry["queued"] and all(q["dry_run"] for q in dry["queued"]) and pubq.list_items(full) == []
    a = PQ.queue_package(full, ai_disclosure=False, publishers=fakes())
    b = PQ.queue_package(full, ai_disclosure=False, publishers=fakes())
    assert a["queued"] and b["queued"] == [] and all("already in the publish queue" in s["why"] for s in b["skipped"] if "why" in s and "draft" not in s["why"])
    assert len(pubq.list_items(full)) == len(a["queued"])


def test_queue_refuses_when_a_packaged_file_changed(full, built):
    victim = Path(built["package_dir"]) / "captions" / "final.v1.srt"
    victim.write_text("tampered")
    rep = PQ.queue_package(full, ai_disclosure=False, publishers=fakes())
    assert rep["queued"] == [] and any("changed since the package was built" in r for r in rep["refused"]) and pubq.list_items(full) == []


def test_queue_platform_filter_and_video_only_adapters_skip_images(full, built):
    rep = PQ.queue_package(full, platforms=["youtube"], ai_disclosure=False, publishers=fakes())
    assert {p for q in rep["queued"] for p in q["platforms"]} == {"youtube"}
    real = PQ.queue_package(full, platforms=["facebook"], ai_disclosure=False)      # the real facebook adapter publishes video only
    assert real["queued"] and all("->" in q["item"] for q in real["queued"])


def test_queue_needs_a_package_first(tmp_path):
    ep = make_episode(tmp_path / "ep")
    with pytest.raises(SystemExit, match="run `ave package"):
        PQ.queue_package(ep)


def test_package_code_has_no_send_path():
    """The package modules may only create queue items. No run(), no adapter publish, no network, no approve."""
    src = "\n".join(p.read_text() for p in (Path(B.__file__).parent).glob("*.py")) + Path(__file__).parents[1].joinpath("src/ave/commands/package.py").read_text()
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith(("#", '"""')))
    for forbidden in ("pq.run(", "queue.run(", ".publish(", "pubq.approve", "queue.approve", "requests", "urllib.request", "httpx", "smtplib", "send=True"):
        assert forbidden not in code, forbidden


def test_cli_queue_dry_run(full, built, capsys):
    rc = main(["package", "queue", str(full.root), "--dry-run", "--platforms", "youtube", "--ai-disclosure", "no"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["queued"] and pubq.list_items(full) == []
