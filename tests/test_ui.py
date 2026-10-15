"""`ave ui`: token and Host enforcement, path traversal, ranged files, clip decisions into the ledger, publish approval, job allowlist."""

import http.client
import json
import os
import threading
import time

import pytest

from ave import clips
from ave.episode import Episode, read_json
from ave.publish import queue
from ave.ui import jobs as ujobs
from ave.ui import ledger
from ave.ui.server import UiServer

from clips_synth import build_long_episode, pick
from conftest import needs_ffmpeg
from publish_helpers import RecordingPublisher, isolated_secrets, queue_item, video_pool  # noqa: F401 (fixtures)


@pytest.fixture(autouse=True)
def local_ledger(monkeypatch):
    """Always exercise the fallback ledger here: ave.learning (workstream L) has its own tests."""
    monkeypatch.setattr(ledger, "_learning", None)


@pytest.fixture
def ep(tmp_path):
    e = build_long_episode(tmp_path / "ep")
    clips.prepare(e, min_s=10, max_s=25)
    r = clips.propose(e, [pick(1, 5, "Systems beat goals"), pick(9, 13, "The worst failure"), pick(17, 21, "Ten calls")])
    assert len(r["accepted"]) == 3, r
    (e.raw / "camA.mp4").write_bytes(b"raw footage bytes")
    (e.exports / "notes.txt").write_text("0123456789" * 10)
    (e.exports / ".hidden").write_text("secret")
    return e


class Client:
    def __init__(self, srv):
        self.srv = srv
        self.cookie = None

    def req(self, method, path, body=None, headers=None, token="query", host=None):
        h = dict(headers or {})
        h["Host"] = host or f"127.0.0.1:{self.srv.port}"
        if token == "header":
            h["X-AVE-Token"] = self.srv.token
        elif token == "cookie":
            h["Cookie"] = f"ave_ui={self.srv.token}"
        elif token == "query":
            path += ("&" if "?" in path else "?") + "t=" + self.srv.token
        elif token:
            h["X-AVE-Token"] = token
        data = None
        if body is not None:
            data = json.dumps(body)
            h["Content-Type"] = "application/json"
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=10)
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in h.items():
            c.putheader(k, v)
        c.putheader("Content-Length", str(len(data.encode()) if data else 0))
        c.endheaders(data.encode() if data else None)
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, dict(r.getheaders()), raw

    def api(self, method, path, body=None):
        s, _, raw = self.req(method, "/api/" + path, body, token="header")
        return s, json.loads(raw or b"{}")


@pytest.fixture
def srv(ep):
    s = UiServer(ep, port=0, by="tester", publishers={p: RecordingPublisher(p) for p in ("youtube", "instagram")})
    t = threading.Thread(target=s.serve_forever, daemon=True)
    t.start()
    yield s
    s.shutdown()
    s.server_close()


@pytest.fixture
def cl(srv):
    return Client(srv)


# ---- binding, token, Host -----------------------------------------------------------------------------------------------

def test_binds_loopback_only(srv):
    assert srv.server_address[0] == "127.0.0.1"
    assert srv.url().startswith(f"http://127.0.0.1:{srv.port}/?t=")


def test_every_request_needs_the_token(cl):
    for path in ("/", "/static/app.js", "/api/dashboard", "/files/exports/notes.txt"):
        assert cl.req("GET", path, token=None)[0] == 401, path
        assert cl.req("GET", path, token="wrong")[0] == 401, path
    assert cl.req("POST", "/api/trim/clear", {"session": "main"}, token=None)[0] == 401


def test_token_in_url_sets_cookie_and_redirects_clean(cl):
    s, h, _ = cl.req("GET", "/")
    assert s == 303 and h["Location"] == "/"
    assert "ave_ui=" in h["Set-Cookie"] and "HttpOnly" in h["Set-Cookie"] and "SameSite=Strict" in h["Set-Cookie"]
    assert cl.req("GET", "/", token="cookie")[0] == 200
    assert cl.req("GET", "/api/dashboard", token="header")[0] == 200


def test_foreign_host_header_is_refused_even_with_the_token(cl):
    for host in ("evil.example.com", f"evil.example.com:{cl.srv.port}", "127.0.0.1", "127.0.0.1:1"):
        assert cl.req("GET", "/api/dashboard", token="header", host=host)[0] == 403, host
    assert cl.req("GET", "/api/dashboard", token="header", host=f"localhost:{cl.srv.port}")[0] == 200


def test_post_with_foreign_origin_is_refused(cl):
    s, _, _ = cl.req("POST", "/api/trim/clear", {"session": "main"}, headers={"Origin": "http://evil.example.com"}, token="header")
    assert s == 403


# ---- files --------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/files/../episode.json", "/files/%2e%2e/episode.json", "/files/exports/../../episode.json",
                                  "/files//etc/passwd", "/files/..%2fepisode.json", "/files/exports/.hidden", "/files/exports/%00",
                                  "/files/", "/files/exports"])
def test_traversal_and_dotfiles_are_refused(cl, path):
    s, _, body = cl.req("GET", path, token="header")
    assert s in (400, 403, 404), (path, s)
    assert b"secret" not in body and b'"title"' not in body


def test_symlink_out_of_episode_is_refused(cl, ep, tmp_path):
    out = tmp_path / "outside.txt"
    out.write_text("outside")
    (ep.exports / "link.txt").symlink_to(out)
    assert cl.req("GET", "/files/exports/link.txt", token="header")[0] == 404


def test_raw_footage_is_not_served(cl):
    assert cl.req("GET", "/files/raw/camA.mp4", token="header")[0] == 403


def test_file_range_requests(cl):
    s, h, body = cl.req("GET", "/files/exports/notes.txt", token="header")
    assert s == 200 and len(body) == 100 and h["Accept-Ranges"] == "bytes"
    s, h, body = cl.req("GET", "/files/exports/notes.txt", headers={"Range": "bytes=10-19"}, token="header")
    assert s == 206 and body == b"0123456789" and h["Content-Range"] == "bytes 10-19/100"
    s, h, body = cl.req("GET", "/files/exports/notes.txt", headers={"Range": "bytes=95-"}, token="header")
    assert s == 206 and len(body) == 5
    s, h, body = cl.req("GET", "/files/exports/notes.txt", headers={"Range": "bytes=-4"}, token="header")
    assert s == 206 and len(body) == 4
    assert cl.req("GET", "/files/exports/notes.txt", headers={"Range": "bytes=500-"}, token="header")[0] == 416
    assert cl.req("GET", "/files/exports/notes.txt", headers={"Range": "bytes=abc"}, token="header")[0] == 416


# ---- pages and API ------------------------------------------------------------------------------------------------------

def test_static_pages_served(cl):
    s, h, body = cl.req("GET", "/", token="cookie")
    assert s == 200 and b"<title>ave ui</title>" in body and "script-src 'self'" in h["Content-Security-Policy"]
    assert cl.req("GET", "/static/app.js", token="cookie")[0] == 200
    assert cl.req("GET", "/static/../../server.py", token="cookie")[0] == 404


def test_dashboard(cl):
    s, d = cl.api("GET", "dashboard")
    assert s == 200 and d["stages"] and any(w["kind"] == "clips" for w in d["waiting"])


def test_unknown_endpoint_and_bad_json(cl):
    assert cl.api("GET", "nope")[0] == 404
    s, _, raw = cl.req("POST", "/api/jobs", None, headers={"Content-Type": "application/json"}, token="header")
    assert s == 400 and b"not an allowed job" in raw


# ---- clips + ledger -----------------------------------------------------------------------------------------------------

def test_clips_sorted_by_score_with_reasons_vocabulary(cl):
    s, d = cl.api("GET", "clips")
    scores = [c["score"] for c in d["clips"]]
    assert s == 200 and len(scores) == 3 and scores == sorted(scores, reverse=True)
    assert "weak_ending" in d["reasons"] and d["ledger"] == "local fallback"


def test_keep_writes_clips_json_and_ledger(cl, ep):
    cid = cl.api("GET", "clips")[1]["clips"][0]["id"]
    s, r = cl.api("POST", f"clips/{cid}/decide", {"verdict": "keep", "reasons": ["great_but_long"], "note": "love the opening", "by": "maren"})
    assert s == 200 and r["status"] == "approved"
    assert next(c for c in clips.load_clips(ep)["clips"] if c["id"] == cid)["status"] == "approved"
    lines = [json.loads(x) for x in ledger.ledger_path(ep).read_text().splitlines()]
    last = lines[-1]
    assert (last["v"], last["clip_id"], last["verdict"], last["source"], last["by"]) == (1, cid, "keep", "ui", "maren")
    assert last["reasons"] == ["great_but_long"] and last["note"] == "love the opening"
    assert {"score", "rubric", "duration_s", "tags", "text_sha"} <= set(last["features"])
    row = next(c for c in cl.api("GET", "clips")[1]["clips"] if c["id"] == cid)
    assert row["status"] == "approved" and row["verdict"]["verdict"] == "keep" and row["verdict"]["note"] == "love the opening"


def test_maybe_and_skip_and_latest_wins(cl, ep):
    cid = cl.api("GET", "clips")[1]["clips"][1]["id"]
    assert cl.api("POST", f"clips/{cid}/decide", {"verdict": "maybe"})[0] == 200
    row = next(c for c in cl.api("GET", "clips")[1]["clips"] if c["id"] == cid)
    assert row["status"] == "maybe"
    stored = next(c for c in clips.load_clips(ep)["clips"] if c["id"] == cid)
    assert stored["status"] == "proposed" and stored["maybe"] is True
    assert cl.api("POST", f"clips/{cid}/decide", {"verdict": "skip", "reasons": ["off_topic"]})[0] == 200
    stored = next(c for c in clips.load_clips(ep)["clips"] if c["id"] == cid)
    assert stored["status"] == "rejected" and not stored.get("maybe")
    latest = ledger.latest_decisions(ep)
    assert latest[cid]["verdict"] == "skip" and len(ledger.iter_decisions(ep)) == 2


def test_bad_clip_decisions_are_refused_and_not_logged(cl, ep):
    cid = cl.api("GET", "clips")[1]["clips"][0]["id"]
    assert cl.api("POST", f"clips/{cid}/decide", {"verdict": "love"})[0] == 400
    assert cl.api("POST", f"clips/{cid}/decide", {"verdict": "keep", "reasons": ["not_a_reason"]})[0] == 400
    assert cl.api("POST", "clips/zz99/decide", {"verdict": "keep"})[0] == 404
    assert not ledger.ledger_path(ep).exists()


def test_fixture_ledger_is_read_back(cl, ep):
    """A ledger written by the CLI side (same schema) shows up in the UI; torn lines are skipped."""
    cid = cl.api("GET", "clips")[1]["clips"][0]["id"]
    p = ledger.ledger_path(ep)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"v": 1, "ts": "2026-10-14T18:03:11Z", "episode": "00", "clip_id": cid, "verdict": "skip", "reasons": ["weak_ending"],
                             "note": "ends flat", "by": "maren", "source": "cli", "features": {}}) + "\n{torn line\n")
    row = next(c for c in cl.api("GET", "clips")[1]["clips"] if c["id"] == cid)
    assert row["verdict"]["source"] == "cli" and row["verdict"]["reasons"] == ["weak_ending"]


# ---- trim ---------------------------------------------------------------------------------------------------------------

def test_trim_set_and_clear_use_the_cli_functions(cl, ep):
    s, r = cl.api("POST", "trim/set", {"session": "main", "in": 2.0, "out": 100.0})
    assert s == 200 and r["trim"]["main"]["in"] == 2.0
    assert read_json(ep.meta_path)["trim"]["main"]["out"] == 100.0
    assert cl.api("POST", "trim/set", {"session": "main", "in": 50, "out": 10})[0] == 400
    assert cl.api("POST", "trim/set", {"session": "main", "in": "x"})[0] == 400
    assert cl.api("GET", "trim")[1]["trim"]["main"]["in"] == 2.0
    assert cl.api("POST", "trim/clear", {"session": "main"})[0] == 200
    assert cl.api("POST", "trim/clear", {"session": "main"})[0] == 400


# ---- content ------------------------------------------------------------------------------------------------------------

def test_content_empty_when_not_started(cl):
    s, d = cl.api("GET", "content")
    assert s == 200 and d["docs"] == []


# ---- publish ------------------------------------------------------------------------------------------------------------

@needs_ffmpeg
def test_publish_approve_requires_the_typed_sha_and_name(cl, ep, vertical_video):
    it = queue_item(ep, vertical_video)
    iid = it["id"]
    s, d = cl.api("GET", f"publish/{iid}")
    assert s == 200 and d["current_sha256"] == it["sha256"] and d["files"][0]["sha256"] == it["sha256"] and d["status"] == "awaiting_approval"
    sha = it["sha256"]
    body = lambda **kw: {"confirm_sha": sha[:12], "by": "Maren", **kw}
    assert cl.api("POST", f"publish/{iid}/approve", body(confirm_sha="deadbeef0000"))[0] == 409     # wrong sha
    assert cl.api("POST", f"publish/{iid}/approve", body(confirm_sha=sha[:7]))[0] == 400            # too short
    assert cl.api("POST", f"publish/{iid}/approve", body(by=""))[0] == 400                           # no name
    assert cl.api("POST", f"publish/{iid}/approve", {"by": "Maren"})[0] == 400                    # no sha at all
    assert queue.load(ep, iid)["status"] == "awaiting_approval" and queue.load(ep, iid)["approvals"] == []
    s, r = cl.api("POST", f"publish/{iid}/approve", body())
    assert s == 200 and r["status"] == "approved" and r["approval"]["source"] == "ui" and r["approval"]["by"] == "Maren"
    saved = queue.load(ep, iid)
    assert saved["status"] == "approved" and saved["events"][-1]["source"] == "ui"
    from ave.publish import safety
    assert safety.approval_problems(saved, queue.current_sha(ep, saved)) == []                      # the signature still verifies
    assert saved.get("results", {}) == {} or not any(v.get("state") == "published" for v in saved["results"].values())   # approving never sends


@needs_ffmpeg
def test_publish_file_changed_after_review_is_refused(cl, ep, vertical_video):
    it = queue_item(ep, vertical_video)
    with open(vertical_video, "ab") as f:
        f.write(b"tamper")
    s, r = cl.api("POST", f"publish/{it['id']}/approve", {"confirm_sha": it["sha256"][:12], "by": "B"})
    assert s == 409


@needs_ffmpeg
def test_publish_reject_and_list(cl, ep, vertical_video):
    it = queue_item(ep, vertical_video)
    assert [i["id"] for i in cl.api("GET", "publish")[1]["items"]] == [it["id"]]
    assert cl.api("POST", f"publish/{it['id']}/reject", {"by": "B", "reason": "wrong cut"})[0] == 200
    assert queue.load(ep, it["id"])["status"] == "rejected"
    assert cl.api("GET", "publish/nope")[0] == 404


def test_publish_has_no_send_endpoint(cl):
    for p in ("publish/x/run", "publish/x/send", "publish/run"):
        assert cl.api("POST", p, {"yes": True, "send": True})[0] == 404


@pytest.fixture
def vertical_video(video_pool, ep):
    import shutil
    dst = ep.exports / "shorts" / "clip1.mp4"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(video_pool / "vertical.mp4", dst)
    return dst


# ---- jobs ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("cmd,args", [
    ("publish run", {}), ("publish approve", {}), ("publish", {}), ("rm -rf /", {}), ("status; ls", {}), ("clips decide", {}), ("", {}),
    ("status", {"allow_unapproved": True}), ("content render", {"allow_unlicensed": True}), ("content render", {"allow-unlicensed": True}),
    ("render", {"out": "/tmp/x.mp4"}), ("render", {"env": {"AVE_ALLOW_UNATTENDED_APPROVAL": "1"}}), ("clips prepare", {"name": "../../raw"}),
    ("clips prepare", {"name": "s1; rm"}), ("clips prepare", {"min_s": "20"}), ("clips prepare", {"min_s": True}), ("clips prepare", {"min_s": -1}),
    ("render", {"stage": "../../etc"}), ("status", {"path": "/Volumes/EXTERNAL"}), ("clips list", {"all": "yes"}),
])
def test_job_allowlist_refuses(ep, cmd, args):
    with pytest.raises(ujobs.JobRefused):
        ujobs.build_argv(ep, cmd, args)


def test_job_argv_is_structured_and_episode_is_injected(ep):
    argv = ujobs.build_argv(ep, "clips prepare", {"min_s": 15, "max_s": 40.5, "name": "s1", "stage": "rough"})
    assert argv[:3] == ["clips", "prepare", str(ep.root)] and ["--min-s", "15"] == argv[argv.index("--min-s"):argv.index("--min-s") + 2]
    assert ujobs.build_argv(ep, "clips review", {"drafts": True}) == ["clips", "review", str(ep.root), "--drafts"]
    assert ujobs.build_argv(ep, "clips review", {"drafts": False}) == ["clips", "review", str(ep.root)]


def test_no_allowlisted_command_publishes_or_approves():
    for cmd, spec in ujobs.ALLOWED.items():
        assert spec["argv"][0] != "publish" or spec["argv"] == ["publish", "list"], cmd
        assert "decide" not in spec["argv"] and "approve" not in spec["argv"]


def test_child_env_drops_the_unattended_approval_switch(monkeypatch):
    monkeypatch.setenv("AVE_ALLOW_UNATTENDED_APPROVAL", "1")
    assert "AVE_ALLOW_UNATTENDED_APPROVAL" not in ujobs.child_env()


def test_job_runs_and_streams_log(cl, ep):
    s, r = cl.api("POST", "jobs", {"cmd": "publish run", "args": {}})
    assert s == 400
    s, r = cl.api("POST", "jobs", {"cmd": "status", "args": {"allow_unapproved": True}})
    assert s == 400
    s, job = cl.api("POST", "jobs", {"cmd": "status"})
    assert s == 200 and job["status"] == "running"
    for _ in range(100):
        s, j = cl.api("GET", f"jobs/{job['id']}")
        if j["status"] != "running":
            break
        time.sleep(0.2)
    assert j["status"] == "done", j["tail"]
    assert j["tail"].startswith("$ ave status") and '"stages"' in j["tail"]
    assert cl.api("GET", "jobs/nope")[0] == 404
    assert [x["id"] for x in cl.api("GET", "jobs")[1]["jobs"]] == [job["id"]]


# ---- review desk (round 2): filters, detail, previews, undo, nudge -------------------------------------------------------

def _score_list(cl):
    return [c["score"] for c in cl.api("GET", "clips")[1]["clips"]]


def test_top_n_and_min_score_are_live_views_over_the_same_list(cl):
    scores = _score_list(cl)
    assert len(scores) == 3
    s, d = cl.api("GET", "clips?top=2")
    assert s == 200 and d["shown"] == 2 and d["of"] == 3 and d["selected"] == 2 and [c["score"] for c in d["clips"]] == scores[:2]
    s, d = cl.api("GET", f"clips?min_score={scores[0]}")
    assert d["shown"] == len([x for x in scores if x >= scores[0]]) and d["of"] == 3
    s, d = cl.api("GET", "clips?min_score=100000")
    assert d["shown"] == 0 and d["of"] == 3 and d["clips"] == []
    s, d = cl.api("GET", f"clips?top=3&min_score={min(scores)}")     # both apply: min first, then the best N of what is left
    assert d["shown"] == 3
    assert d["score_range"] == {"min": min(scores), "max": max(scores)}


def test_clip_filters_sort_and_bad_params(cl):
    rows = cl.api("GET", "clips?sort=time")[1]["clips"]
    starts = [c["start"] for c in rows]
    assert starts == sorted(starts) and len(rows) == 3
    assert cl.api("GET", "clips?sort=nonsense")[0] == 400
    assert cl.api("GET", "clips?top=abc")[0] == 400
    assert cl.api("GET", "clips?min_score=-3")[0] == 400
    assert cl.api("GET", "clips?status=weird")[0] == 400
    d = cl.api("GET", "clips")[1]
    tag = d["tags"][0]["tag"] if d["tags"] else None
    if tag:
        assert all(tag in c["tags"] for c in cl.api("GET", f"clips?tag={tag}")[1]["clips"])
    assert cl.api("GET", "clips?q=zzzzqqqq")[1]["shown"] == 0
    assert cl.api("GET", "clips?session=nope")[1]["shown"] == 0


def test_unreviewed_filter_progress_and_ranks(cl):
    rows = cl.api("GET", "clips")[1]["clips"]
    assert [c["rank"] for c in rows] == [1, 2, 3]                      # computed from the score when clips.json has no rank
    a, b = rows[0]["id"], rows[1]["id"]
    assert cl.api("POST", f"clips/{a}/decide", {"verdict": "keep"})[0] == 200
    assert cl.api("POST", f"clips/{b}/decide", {"verdict": "skip", "reasons": ["off_topic"]})[0] == 200
    d = cl.api("GET", "clips?unreviewed=1")[1]
    assert [c["id"] for c in d["clips"]] == [rows[2]["id"]] and d["of"] == 3 and d["shown"] == 1
    assert d["progress"] == {"done": 2, "total": 3, "keep": 1, "maybe": 0, "skip": 1}      # progress ignores the unreviewed filter
    assert cl.api("GET", "clips?status=rejected")[1]["clips"][0]["id"] == b
    assert d["reason_labels"]["weak_ending"]["label"]


def test_clip_detail_has_transcript_context_completeness_and_no_media_yet(cl):
    cid = cl.api("GET", "clips")[1]["clips"][0]["id"]
    s, d = cl.api("GET", f"clips/{cid}")
    assert s == 200 and d["id"] == cid and d["text"] and d["scores"]
    t = d["transcript"]
    assert t["available"] and t["inside"] and all(w["where"] == "in" for w in t["inside"])
    assert all(w["end"] <= d["start"] + 0.02 for w in t["before"]) and all(w["start"] >= d["end"] - 0.02 for w in t["after"])
    assert d["start_complete"] in (True, False, None) and d["end_complete"] in (True, False, None)
    assert d["media"]["preview"] is None and d["media"]["state"] in ("none", "edit proxy", "edit render", "draft")
    assert d["tighten"] == []
    assert cl.api("GET", "clips/zz99")[0] == 404


def test_clip_detail_reads_k_fields_and_maps_tighten_to_clip_time(cl, ep):
    from ave.episode import write_json
    data = clips.load_clips(ep)
    c = data["clips"][0]
    sents = clips.load_candidates(ep)["sentences"]
    inside = [s for s in sents if s["id"] in c["sentence_ids"]]
    s1 = inside[1]
    c.update(rank=4, start_complete=False, end_complete=True, completeness_note="opens mid-thought", context_before="BEFORE TEXT", context_after="AFTER TEXT",
             tighten=[{"start": s1["e0"], "end": s1["e0"] + (s1["end"] - s1["start"]), "kind": "filler", "text": s1["text"], "saves_s": 1.5, "reason": "x", "confidence": 0.8}],
             tight_duration=c["duration"] - 1.5)
    write_json(ep.artifact(clips.CLIPS), data)
    d = cl.api("GET", f"clips/{c['id']}")[1]
    assert d["rank"] == 4 and d["start_complete"] is False and d["completeness_note"] == "opens mid-thought"
    assert d["context_before"] == "BEFORE TEXT" and d["context_after"] == "AFTER TEXT"
    t = d["tighten"][0]
    assert t["rel"] is not None and abs(t["rel"][0] - (s1["start"] - c["start"])) < 0.01 and s1["id"] in t["sentence_ids"] and t["saves_s"] == 1.5
    row = next(x for x in cl.api("GET", "clips")[1]["clips"] if x["id"] == c["id"])
    assert row["start_complete"] is False and row["tight_saves_s"] == 1.5


def _write_previews(ep, cid, c, tight=True, file="x.mp4", extra=None):
    d = ep.work / "clips" / "previews"
    d.mkdir(parents=True, exist_ok=True)
    (d / "x.mp4").write_bytes(bytes(range(256)) * 8)
    (d / "x.tight.mp4").write_bytes(b"T" * 300)
    entry = {"file": file, "tight_file": "x.tight.mp4" if tight else None, "pad_before_s": 8, "pad_after_s": 8, "clip_in_s": 8.0,
             "clip_out_s": 8.0 + c["duration"], "edit": "rough.v1", "rendered_at": "2026-10-14T10:00:00-0600", "sha": "abc", **(extra or {})}
    (d / "index.json").write_text(json.dumps({cid: entry}))


def test_preview_contract_is_served_with_range_and_dimming_offsets(cl, ep):
    c = clips.load_clips(ep)["clips"][0]
    _write_previews(ep, c["id"], c)
    d = cl.api("GET", f"clips/{c['id']}")[1]
    pv = d["media"]["preview"]
    assert d["media"]["state"] == "preview" and pv["clip_in_s"] == 8.0 and pv["clip_out_s"] == 8.0 + c["duration"] and pv["pad_after_s"] == 8
    assert pv["url"].startswith("/files/") and pv["tight_url"].endswith("x.tight.mp4") and pv["stale"] is False
    row = next(x for x in cl.api("GET", "clips")[1]["clips"] if x["id"] == c["id"])
    assert row["has_preview"] is True and cl.api("GET", "clips")[1]["previews_rendered"] == 1
    s, h, body = cl.req("GET", pv["url"], headers={"Range": "bytes=10-19"}, token="header")
    assert s == 206 and body == bytes(range(256))[10:20] and h["Content-Range"] == "bytes 10-19/2048" and h["Accept-Ranges"] == "bytes"
    s, h, body = cl.req("GET", pv["url"], headers={"Range": "bytes=-5"}, token="header")
    assert s == 206 and len(body) == 5
    assert cl.req("GET", pv["url"], headers={"Range": "bytes=99999-"}, token="header")[0] == 416
    assert cl.req("GET", pv["url"], token=None)[0] == 401                       # the preview needs the token like everything else


def test_preview_without_tight_file_and_stale_range(cl, ep):
    c = clips.load_clips(ep)["clips"][0]
    _write_previews(ep, c["id"], c, tight=False, extra={"clip_out_s": 8.0 + c["duration"] + 5})
    pv = cl.api("GET", f"clips/{c['id']}")[1]["media"]["preview"]
    assert pv["tight_url"] is None and pv["stale"] is True                       # the clip changed length after the preview was rendered


@pytest.mark.parametrize("fname", ["../../episode.json", "missing.mp4", "/etc/passwd", ".hidden.mp4"])
def test_preview_index_cannot_point_outside_the_previews_dir_or_at_missing_files(cl, ep, fname):
    c = clips.load_clips(ep)["clips"][0]
    _write_previews(ep, c["id"], c, tight=False, file=fname)
    d = cl.api("GET", f"clips/{c['id']}")[1]
    pv = d["media"]["preview"]
    assert pv is None or (pv["url"].startswith("/files/") and ".." not in pv["url"] and "episode.json" not in pv["url"])
    if fname in ("missing.mp4", "/etc/passwd", ".hidden.mp4"):
        assert pv is None


def test_broken_preview_index_is_ignored(cl, ep):
    p = ep.work / "clips" / "previews"
    p.mkdir(parents=True)
    (p / "index.json").write_text("{not json")
    assert cl.api("GET", "clips")[0] == 200
    assert cl.api("GET", "clips")[1]["previews_rendered"] == 0


def test_undo_a_first_decision_and_nudge_through_the_api(cl, ep):
    row = cl.api("GET", "clips")[1]["clips"][0]
    cid = row["id"]
    assert cl.api("POST", f"clips/{cid}/decide", {"verdict": "skip"})[0] == 200
    assert next(c for c in clips.load_clips(ep)["clips"] if c["id"] == cid)["status"] == "rejected"
    s, r = cl.api("POST", f"clips/{cid}/decide", {"verdict": "pending"})
    assert s == 200 and r["status"] == "proposed"
    assert next(c for c in cl.api("GET", "clips")[1]["clips"] if c["id"] == cid)["status"] == "proposed"
    s, r = cl.api("POST", f"clips/{cid}/decide", {"verdict": "nudge", "nudge": {"end": 1}})
    assert s == 200 and r["end"] != row["end"] and r["status"] == "proposed"
    for bad in ({"verdict": "nudge"}, {"verdict": "nudge", "nudge": {"end": 99}}, {"verdict": "nudge", "nudge": {"bogus": 1}},
                {"verdict": "nudge", "nudge": {"end": "1"}}, {"verdict": "nudge", "nudge": {"end": True}}):
        assert cl.api("POST", f"clips/{cid}/decide", bad)[0] == 400, bad


def test_dashboard_has_clip_counts_and_only_allowlisted_safe_actions(cl, ep):
    d = cl.api("GET", "dashboard")[1]
    assert d["clip_counts"]["main"]["total"] == 3 and d["clip_counts"]["main"]["proposed"] == 3
    assert d["actions"] and all(a["cmd"] in ujobs.ALLOWED for a in d["actions"])
    for a in d["actions"]:
        ujobs.build_argv(ep, a["cmd"], a["args"])                             # every suggested one-click job passes the allowlist


def test_previews_job_is_allowlisted_with_typed_args(ep):
    argv = ujobs.build_argv(ep, "clips previews", {"name": "s1", "ids": "s1-c07", "top": 20, "pad": 8.5, "force": True})
    assert argv[:3] == ["clips", "previews", str(ep.root)] and "--force" in argv and argv[argv.index("--top") + 1] == "20"
    for bad in ({"ids": "s1 c07"}, {"top": "20"}, {"out": "/tmp/x"}, {"name": "../x"}, {"top": -1}):
        with pytest.raises(ujobs.JobRefused):
            ujobs.build_argv(ep, "clips previews", bad)


def test_static_assets_are_self_contained(cl):
    for path in ("/", "/static/app.js", "/static/style.css"):
        s, h, body = cl.req("GET", path, token="cookie")
        assert s == 200
        text = body.decode()
        assert "https://" not in text and "@import" not in text and "http://" not in text.replace("http://www.w3.org", "")


def test_stale_proposed_clips_hidden_by_default(cl, ep):
    data = clips.load_clips(ep)
    victim = data["clips"][0]
    victim["stale"] = True
    from ave.episode import write_json
    write_json(ep.artifact(clips.CLIPS), data)
    ids = [c["id"] for c in cl.api("GET", "clips")[1]["clips"]]
    assert victim["id"] not in ids
    ids_all = [c["id"] for c in cl.api("GET", "clips?stale=1")[1]["clips"]]
    assert victim["id"] in ids_all
