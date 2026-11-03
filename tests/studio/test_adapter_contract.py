"""The adapter contract suite (spec 10.3, WS2): every studio adapter, parametrized, on scripted HTTP built from DOCUMENTED request/response shapes
(tests/data_studio/<platform>/*.json, each route labelled documentation-derived, UNVERIFIED live).

What it pins for every adapter: dry-run never touches HTTP or the media host; `ready_problems` lists missing credentials AND "not live";
`preflight` is pure; the outcome taxonomy (a refusal before the final step is `PublishError` -> `failed`; a 5xx or a transport error on the
final step is `Uncertain` -> the queue state stays `sending`; 401 is an `AuthError`); an unattested or hand-edited live file blocks a send; a
send through `queue.run` on a studio queue item works end to end; secrets never reach the queue file, a report, a status or a log.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from studio_helpers import DATA, FakeClock, FakeHost, FakeHttp, make_png, write_account

from ave.platforms import attest, registry
from ave.platforms.accounts import load_account
from ave.platforms.base import AuthError, CredStatus, PlatformAdapter
from ave.publish import queue as pq, safety
from ave.publish.adapters.base import Absent, PublishError, Uncertain
from ave.studio.project import StudioProject

TOKEN = "EAAB" + "x1y2z3w4v5u6t7s8r9q0" * 2          # Meta-shaped, so the redaction patterns see it too


class Resp:
    def __init__(self, data, status=200, headers=None):
        self.data, self.status_code, self.headers = data, status, headers or {}
        self.text = json.dumps(data)

    def json(self):
        return self.data


class Http(FakeHttp):
    """FakeHttp plus response headers and scripted transport failures (`"raise": "timeout"`)."""

    def request(self, method, url, **kw):
        self.calls.append({"method": method.upper(), "url": url, **kw})
        for r in self.routes:
            if r["method"].upper() == method.upper() and r["url"] in url and r["_used"] < r.get("times", 10**9):
                r["_used"] += 1
                if r.get("raise"):
                    raise TimeoutError("scripted transport failure")
                exp = r.get("expect") or {}
                body = kw.get("json")
                for k in exp.get("json_keys", []):
                    assert isinstance(body, dict) and k in body, f"{method} {url}: request body lacks {k!r} (has {sorted(body or {})})"
                for h in exp.get("headers", []):
                    assert h in (kw.get("headers") or {}), f"{method} {url}: missing header {h!r}"
                return Resp(r.get("response", {}), r.get("status", 200), r.get("headers"))
        raise AssertionError(f"unexpected HTTP call: {method} {url}")

    @classmethod
    def load(cls, platform: str, name: str) -> "Http":
        return cls(json.loads((DATA / platform / name).read_text()))


@dataclass
class Case:
    id: str
    platform: str
    fixture: str = "ok.json"
    media: str = "image"                    # image | carousel | video
    secrets: dict = field(default_factory=dict)
    public: dict = field(default_factory=dict)
    mode: str = "graph"
    sends: str = "published"                # published | sending (manual: waits for the human)
    needs_host: bool = False
    verify: bool = True


S = {"token": TOKEN}
CASES = [
    Case("bluesky", "bluesky", secrets={"app_password": "abcd-efgh-ijkl-mnop"}, public={"handle": "acme.bsky.social"}),
    Case("bluesky-carousel", "bluesky", media="carousel", secrets={"app_password": "abcd-efgh-ijkl-mnop"}, public={"handle": "acme.bsky.social"}),
    Case("facebook", "facebook", "photo_ok.json", secrets={"page_token": TOKEN}, public={"page_id": "100000000000001"}),
    Case("facebook-carousel", "facebook", "carousel_ok.json", media="carousel", secrets={"page_token": TOKEN}, public={"page_id": "100000000000001"}),
    Case("instagram", "instagram", secrets=S, public={"ig_user_id": "17841400000000000"}, needs_host=True),
    Case("instagram-carousel", "instagram", media="carousel", secrets=S, public={"ig_user_id": "17841400000000000"}, needs_host=True),
    Case("threads", "threads", secrets=S, public={"threads_user_id": "17841400000000001"}, needs_host=True),
    Case("threads-carousel", "threads", media="carousel", secrets=S, public={"threads_user_id": "17841400000000001"}, needs_host=True),
    Case("linkedin", "linkedin", secrets=S, public={"author": "urn:li:person:abc123"}),
    Case("linkedin-carousel", "linkedin", media="carousel", secrets=S, public={"author": "urn:li:person:abc123"}),
    Case("x", "x", secrets=S),
    Case("x-carousel", "x", media="carousel", secrets=S),
    Case("pinterest", "pinterest", secrets=S, public={"board_id": "549755885175"}),
    Case("pinterest-carousel", "pinterest", media="carousel", secrets=S, public={"board_id": "549755885175"}),
    Case("tiktok", "tiktok", media="video", mode="inbox", secrets=S, sends="awaiting_user_publish", verify=False),
    Case("manual", "manual", media="image", mode="manual", sends="sending", verify=False),
]
IDS = [c.id for c in CASES]


@pytest.fixture(scope="session")
def mp4(tmp_path_factory):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    p = tmp_path_factory.mktemp("tt") / "clip.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=360x640:r=10:d=4", "-f", "lavfi", "-i", "sine=f=300:d=4",
                    "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(p)], check=True)
    return p


@dataclass
class Rig:
    case: Case
    sp: StudioProject
    acct: object
    adapter: PlatformAdapter
    http: Http
    host: FakeHost
    item: dict
    files: list
    pubs: dict
    secret_values: list


def build(tmp_path, case: Case, mp4, *, secrets=True, live=True, routes=None, approve=True, host=True) -> Rig:
    sp = StudioProject(tmp_path / "studio")
    sp.create(title="Acme Bakery", tz="America/Denver")
    aid = f"{case.platform}-main"
    write_account(sp, aid, case.platform, mode=case.mode, credentials={"ref": aid, **case.public}, post_types=["feed", "carousel", "story"], connection={"scopes":["w_organization_social"],"source":"synthetic fixture"} if case.platform=="linkedin" else {"verified_domains":["media.example.invalid"],"source":"synthetic fixture"} if case.platform=="tiktok" else {})
    acct = load_account(sp, aid)
    values = []
    if secrets:
        for f, v in case.secrets.items():
            safety.set_secret(acct.secret_name(f), v)
            values.append(v)
    if live:
        attest.write_live(sp.accounts_dir, aid, "maren")
    http = Http(routes) if routes is not None else Http.load(case.platform, case.fixture) if (DATA / case.platform / case.fixture).exists() else Http([])
    fh = FakeHost()
    clock = FakeClock()
    adapter = registry.adapter_for(acct, http=http, host=fh if host else None, sleep=lambda s: None, now=clock.now)
    pubs = {case.platform: adapter}
    kw = dict(platforms=[case.platform], title="Day 3 of the starter", caption="Day 3: the starter smells like pears. https://acme.example/starter",
              visibility="public", ai_disclosure=False, meta_v=2, account=aid, post_type="feed", publishers=pubs)
    if case.media == "video":
        dst = sp.work / "gen" / "clip.mp4"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(mp4, dst)
        item = pq.create(sp, video=dst, media_type="video", **kw)
    else:
        n = 2 if case.media == "carousel" else 1
        imgs = [make_png(sp.work / "gen" / f"s{i}.jpg", color=(30 + i * 40, 50, 70)) for i in range(n)]
        item = pq.create(sp, files=imgs, media_type=case.media, alt_texts=[f"Slide {i + 1}: a jar of sourdough starter." for i in range(n)], **kw)
    assert item["status"] == "awaiting_approval", item["preflight_problems"]
    if approve:
        pq.approve(sp, item["id"], "maren", item["sha256"][:12], publishers=pubs)
        item = pq.load(sp, item["id"])
    return Rig(case, sp, acct, adapter, http, fh, item, pq.resolve_files(sp, item), pubs, values)


def send(rig: Rig) -> dict:
    return pq.run(rig.sp, rig.item["id"], send=True, publishers=rig.pubs)


def direct_publish(rig: Rig):
    """Call the adapter exactly the way queue.run does: with the verified copies as `_files`."""
    item = {**rig.item, "_files": [str(f) for f in rig.files]}
    return rig.adapter.publish(item, rig.files[0], lambda partial: None)


def break_route(routes: list[dict], pick, **change) -> list[dict]:
    routes = copy.deepcopy(routes)
    hit = [r for r in routes if pick(r)]
    assert hit, "fixture has no such route"
    for r in hit:
        r.update(change)
    return routes


REFUSE = {"status": 400, "response": {"error": {"code": 100, "message": "scripted refusal", "type": "invalid"}, "message": "scripted refusal"}}


def fixture_routes(case):
    p = DATA / case.platform / case.fixture
    return json.loads(p.read_text()) if p.exists() else []


def final_of(routes):
    return [r for r in routes if r.get("final")]


# ------------------------------------------------------------------------------------------------ identity

@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_adapter_identity_and_honesty(tmp_path, mp4, case):
    rig = build(tmp_path, case, mp4)
    a = rig.adapter
    assert isinstance(a, PlatformAdapter) or hasattr(a, "ready_problems")
    assert a.account.id == rig.acct.id and a.name == case.platform
    st = a.status()
    assert st["verified"] is False and st["platform"] == case.platform and st["account"] == rig.acct.id
    assert isinstance(a.capabilities, frozenset) and a.capabilities
    assert a.carries_images is True
    assert (case.platform in ("instagram", "threads")) == bool(a.needs_public_url) or case.platform == "tiktok"


# ------------------------------------------------------------------------------------------------ dry run

@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_dry_run_never_touches_http_or_the_host(tmp_path, mp4, case):
    rig = build(tmp_path, case, mp4, secrets=False, live=False)      # not even credentials or a live switch
    rep = pq.run(rig.sp, rig.item["id"], send=False, publishers=rig.pubs)
    assert rep["ok"] is True and rep["mode"] == "dry-run", rep
    assert rig.http.calls == [] and rig.host.puts == []
    assert pq.load(rig.sp, rig.item["id"])["results"] == {}


# ------------------------------------------------------------------------------------------------ gates

@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_ready_problems_lists_missing_credentials_and_not_live(tmp_path, mp4, case):
    rig = build(tmp_path, case, mp4, secrets=False, live=False)
    text = " | ".join(rig.adapter.ready_problems())
    assert "not live" in text
    for f in case.secrets:
        assert rig.acct.secret_name(f) in text, f"{f} not named in: {text}"
    assert rig.http.calls == []
    rig2 = build(tmp_path / "b", case, mp4, secrets=True, live=False)
    assert all("credential" not in p for p in rig2.adapter.ready_problems()) and any("not live" in p for p in rig2.adapter.ready_problems())
    rig3 = build(tmp_path / "c", case, mp4, secrets=True, live=True)
    assert rig3.adapter.ready_problems() == []
    if case.platform == "instagram":          # no host configured: images cannot be fetched by Meta
        assert any("needs_public_url" in p for p in registry.adapter_for(rig3.acct, http=rig3.http).ready_problems())


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_a_not_live_account_cannot_be_sent_through_queue_run(tmp_path, mp4, case):
    rig = build(tmp_path, case, mp4, live=False)
    rep = send(rig)
    assert rep["ok"] is False and any("not live" in b for b in rep["blocked"]), rep
    assert rig.http.calls == [] and rig.host.puts == []
    assert pq.load(rig.sp, rig.item["id"])["results"] == {}


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_a_hand_edited_live_file_is_rejected(tmp_path, mp4, case):
    rig = build(tmp_path, case, mp4, live=False)
    forged = {"account": rig.acct.id, "live": True, "by": "agent", "at": "2026-10-19T09:00:00-0600", "sig": "0" * 64}
    attest.live_path(rig.sp.accounts_dir, rig.acct.id).write_text(json.dumps(forged))
    assert not rig.acct.live_ok() and "signature invalid" in rig.acct.live_status().reason
    rep = send(rig)
    assert rep["ok"] is False and rig.http.calls == []
    # a real one for another account does not transfer either
    attest.write_live(rig.sp.accounts_dir, "someone-else", "maren")
    shutil.copy(attest.live_path(rig.sp.accounts_dir, "someone-else"), attest.live_path(rig.sp.accounts_dir, rig.acct.id))
    assert not rig.acct.live_ok()


# ------------------------------------------------------------------------------------------------ preflight is pure

@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_preflight_is_pure_and_catches_the_shared_problems(tmp_path, mp4, case):
    rig = build(tmp_path, case, mp4, secrets=False, live=False)
    item, f = rig.item, rig.files[0]
    first = rig.adapter.preflight(item, f)
    assert first == rig.adapter.preflight(item, f) == []
    assert rig.http.calls == [] and rig.host.puts == []
    wrong = rig.adapter.preflight({**item, "account": "another-account"}, f)
    assert any("bound to" in p for p in wrong)
    if case.media != "video":
        assert any("alt" in p.lower() for p in rig.adapter.preflight({**item, "alt_texts": [""] * len(item["alt_texts"])}, f))
    assert any("does not post gif" in p for p in rig.adapter.preflight({**item, "media_type": "gif"}, f))


# ------------------------------------------------------------------------------------------------ a send, end to end

@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_fake_send_end_to_end_through_queue_run(tmp_path, mp4, case, caplog):
    caplog.set_level("DEBUG")
    rig = build(tmp_path, case, mp4)
    rep = send(rig)
    out = rep["platforms"][case.platform]
    saved = pq.load(rig.sp, rig.item["id"])
    if case.sends == "published":
        assert rep["ok"] is True and out["outcome"] == "published", rep
        assert out["id"] and out["platform"] == case.platform and out["account"] == rig.acct.id
        assert saved["status"] == "published" and saved["results"][case.platform]["state"] == "published"
        for r in final_of(rig.http.routes):
            assert r["_used"] == 1, "the final step runs exactly once"
        assert rig.http.calls, "a real send makes calls"
        if case.needs_host:
            assert rig.host.puts and not rig.host.deleted and saved["results"][case.platform]["host_leases"], "leases are durably retained for bounded expiry"
        # a second run never resends
        n = len(rig.http.calls)
        again = pq.run(rig.sp, rig.item["id"], send=True, publishers=rig.pubs)
        assert again["ok"] and len(rig.http.calls) == n, "a published item is never resent"
    elif case.sends=='awaiting_user_publish':
        assert out['outcome']=='awaiting_user_publish' and saved['status']=='publishing'
        assert out['id'] is None and out['receipt']['object']['id']
        before=len([c for c in rig.http.calls if c['url'].endswith('/inbox/video/init/')])
        send(rig)
        assert len([c for c in rig.http.calls if c['url'].endswith('/inbox/video/init/')])==before
    else:
        assert out["outcome"] == "uncertain" and saved["results"][case.platform]["state"] == "sending"
        folder = rig.sp.root / "exports" / "manual" / rig.acct.id / rig.item["id"]
        assert (folder / "caption.txt").is_file() and (folder / "manifest.json").is_file()
        settled = pq.resolve(rig.sp, rig.item["id"], "manual", posted_url="https://example.invalid/post/1", by="maren")
        assert settled["status"] == "published"
    # secrets never reach the queue file, the report, the status or a log
    blob = pq._path(rig.sp, rig.item["id"]).read_text() + json.dumps(rep, default=str) + json.dumps(rig.adapter.status()) + caplog.text
    for v in rig.secret_values:
        assert v not in blob
    assert TOKEN not in blob


# ------------------------------------------------------------------------------------------------ the outcome taxonomy

def _failure_cases():
    return [c for c in CASES if c.sends == "published"]


@pytest.mark.parametrize("case", _failure_cases(), ids=[c.id for c in _failure_cases()])
def test_a_refusal_before_the_final_step_is_a_definite_failure(tmp_path, mp4, case):
    routes = fixture_routes(case)
    early = [r for r in routes if r.get("early") and not r.get("final")]
    if not early:
        pytest.skip("single-step platform: the only step is the final one")
    rig = build(tmp_path, case, mp4, routes=break_route(routes, lambda r: r.get("early") and not r.get("final"), **REFUSE))
    rep = send(rig)
    assert rep["platforms"][case.platform]["outcome"] == "failed", rep
    assert pq.load(rig.sp, rig.item["id"])["results"][case.platform]["state"] == "failed"
    assert all(r["_used"] == 0 for r in final_of(rig.http.routes)), "nothing may reach the final step"
    if case.needs_host:
        assert not rig.host.deleted, "provider pull leases are retained until bounded expiry"
    with pytest.raises(PublishError):
        direct_publish(build(tmp_path / "again", case, mp4, routes=break_route(routes, lambda r: r.get("early") and not r.get("final"), **REFUSE)))


@pytest.mark.parametrize("case", _failure_cases(), ids=[c.id for c in _failure_cases()])
def test_an_expired_token_is_an_auth_error_and_never_retried_blindly(tmp_path, mp4, case):
    routes = fixture_routes(case)
    first = [r for r in routes if r.get("early")] or final_of(routes)
    rig = build(tmp_path, case, mp4, routes=break_route(routes, lambda r: r["url"] == first[0]["url"], status=401,
                                                        response={"error": {"code": 190, "message": "Invalid OAuth access token"}, "message": "expired"}))
    with pytest.raises(AuthError):
        direct_publish(rig)


@pytest.mark.parametrize("case", _failure_cases(), ids=[c.id for c in _failure_cases()])
def test_a_5xx_or_a_timeout_on_the_final_step_is_uncertain_and_the_queue_keeps_sending(tmp_path, mp4, case):
    routes = fixture_routes(case)
    for change in ({"status": 503, "response": {"error": {"message": "unavailable", "code": 2}, "message": "unavailable"}}, {"raise": "timeout"}):
        rig = build(tmp_path / ("a" if "status" in change else "b"), case, mp4, routes=break_route(routes, lambda r: r.get("final"), **change))
        rep = send(rig)
        assert rep["platforms"][case.platform]["outcome"] == "uncertain", rep
        saved = pq.load(rig.sp, rig.item["id"])
        assert saved["results"][case.platform]["state"] == "sending" and saved["status"] != "published"
        # recover never raises and never claims a post it cannot prove; it must give a verdict the queue understands
        verdict = rig.adapter.recover(saved, saved["results"][case.platform])
        assert verdict is None or isinstance(verdict, (dict, Absent))
        # a re-run asks recover() first and never blindly resends: no final-step call happens twice
        pq.run(rig.sp, rig.item["id"], send=True, publishers=rig.pubs)
        assert all(r["_used"] <= 1 for r in final_of(rig.http.routes)) or isinstance(verdict, Absent)


@pytest.mark.parametrize("case", _failure_cases(), ids=[c.id for c in _failure_cases()])
def test_a_4xx_on_the_final_step_is_a_definite_refusal(tmp_path, mp4, case):
    routes = fixture_routes(case)
    rig = build(tmp_path, case, mp4, routes=break_route(routes, lambda r: r.get("final"), **REFUSE))
    rep = send(rig)
    assert rep["platforms"][case.platform]["outcome"] == "failed", rep
    assert pq.load(rig.sp, rig.item["id"])["results"][case.platform]["state"] == "failed"


# ------------------------------------------------------------------------------------------------ credentials

@pytest.mark.parametrize("case", [c for c in CASES if c.verify], ids=[c.id for c in CASES if c.verify])
def test_verify_credentials_is_read_only_and_returns_no_secret(tmp_path, mp4, case):
    rig = build(tmp_path, case, mp4, routes=json.loads((DATA / case.platform / "verify.json").read_text()), secrets=False)
    st = rig.adapter.verify_credentials()
    assert isinstance(st, CredStatus) and st.ready is False and st.checked is False and st.missing
    assert rig.http.calls == []
    rig = build(tmp_path / "ok", case, mp4, routes=json.loads((DATA / case.platform / "verify.json").read_text()))
    st = rig.adapter.verify_credentials()
    assert st.ready is True and st.checked is (case.platform != "linkedin")
    if case.platform == "linkedin": assert "does not establish" in st.detail["note"]
    assert [c["method"] for c in rig.http.calls] and all(c["method"] in ("GET", "POST") for c in rig.http.calls)
    blob = json.dumps(st.to_dict())
    for v in rig.secret_values:
        assert v not in blob


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_an_unbound_adapter_is_dry_run_only(case):
    a = registry.get_publisher(case.platform) if case.platform in registry.ADAPTERS else None
    assert a is not None
    assert not a.account.live_ok() and any("not live" in p for p in a.ready_problems())
