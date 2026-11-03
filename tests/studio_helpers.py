"""Shared fakes and builders for the studio tests (spec 10.2). No network, no real clock, no real keychain.

    FakeClock(now)            .now() / .sleep(s) / .advance(**timedelta_kw)
    RecordingPublisher(...)   a Publisher stand-in with scripted outcomes per call (ok | refuse | uncertain | crash | network)
    FakeHttp(routes)          scripted HTTP responses; asserts method, URL and body shape (fixtures under tests/data_studio/<platform>/)
    FakeHost()                a MediaHost that records puts/deletes and never touches a network
    build_studio(tmp, ...)    a StudioProject with accounts, library assets, and posts in every lifecycle state
    approve_item(sp, id)      approval for tests: calls ave.publish.queue.approve directly (never an environment switch)
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import yaml
from PIL import Image

from ave.episode import Episode, write_json
from ave.publish import queue as pq, safety
from ave.publish.adapters.base import PublishError, Uncertain
from ave.studio import store
from ave.studio.events import AGENT, Actor
from ave.studio.model import asset_id, new_post, new_variant
from ave.studio.project import StudioProject
from ave.studio.service import Studio

DATA = Path(__file__).parent / "data_studio"
START = datetime(2026, 10, 19, 16, 0, tzinfo=timezone.utc)


class FakeClock:
    """A Clock whose time only moves when told to. `sleep` advances it (nothing ever really waits)."""
    latest = None       # tests/conftest.py ages fixture approvals against this, so the real date cannot expire them

    def __init__(self, now: datetime | str = START):
        if isinstance(now, str):
            now = datetime.fromisoformat(now.replace("Z", "+00:00"))
        assert now.tzinfo is not None, "FakeClock needs an aware datetime"
        self._now = now
        self.slept: list[float] = []
        FakeClock.latest = self

    def now(self) -> datetime:
        return self._now

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self._now += timedelta(seconds=s)

    def advance(self, **kw) -> datetime:
        self._now += timedelta(**kw)
        return self._now

    def set(self, now: datetime | str) -> None:
        self._now = datetime.fromisoformat(now.replace("Z", "+00:00")) if isinstance(now, str) else now


class RecordingPublisher:
    """Publisher stand-in. `behaviors` is consumed one entry per `publish` call (the last repeats):
    "ok" | "refuse" (PublishError) | "uncertain" (Uncertain) | "network" (ConnectionError) | "crash" (KeyboardInterrupt, as a killed process)
    | "crash_after_send" (records the send, THEN dies: the post exists but the queue state stays `sending`).
    `live=False` makes `ready_problems` refuse the way an account without a valid attestation does."""

    carries_images = True
    needs_public_url = False

    def __init__(self, platform: str = "youtube", behaviors=("ok",), *, recover=None, problems=None, live: bool = True,
                 missing_credentials: list[str] | None = None):
        self.name = self.platform = platform
        self.behaviors = list(behaviors) if not isinstance(behaviors, str) else [behaviors]
        self._recover = recover
        self.problems = list(problems or [])
        self.live = live
        self.missing_credentials = list(missing_credentials or [])
        self.publish_calls = 0
        self.sent: list[dict] = []          # what "reached the platform"
        self.http_calls: list[tuple] = []   # stays empty: this fake never touches HTTP

    def preflight(self, item, video):
        return list(self.problems)

    def ready_problems(self):
        out = list(self.missing_credentials)
        if not self.live:
            out.append("account is not live (no valid live attestation)")
        return out

    def publish(self, item, video, progress):
        i = min(self.publish_calls, len(self.behaviors) - 1)
        b = self.behaviors[i]
        self.publish_calls += 1
        progress({"container": f"c{self.publish_calls}"})
        if b == "refuse":
            raise PublishError("quota exceeded")
        if b == "uncertain":
            raise Uncertain("timeout after the final step")
        if b == "network":
            raise ConnectionError("reset by peer")
        if b == "crash":
            raise KeyboardInterrupt()
        self.sent.append({"id": item["id"], "title": item.get("title"), "caption": item.get("caption"), "account": item.get("account"),
                          "files": item.get("_files"), "schedule": item.get("schedule")})
        if b == "crash_after_send":
            raise KeyboardInterrupt()
        from ave.publish.receipts import result
        return result(self.platform,f"{self.platform}-{self.publish_calls}",f"https://example.invalid/{self.platform}/{self.publish_calls}",account=item.get('account'))

    def recover(self, item, record):
        return self._recover

    def status(self):
        return {"platform": self.name, "ready": self.live and not self.missing_credentials}


@dataclass
class FakeResp:
    data: Any = None
    status_code: int = 200
    text: str = ""

    def json(self):
        return self.data if self.data is not None else {}

    @property
    def ok(self):
        return self.status_code < 400


class FakeHttp:
    """Scripted HTTP. Each route: {"method", "url" (substring), "status", "response", "expect": {"json_keys": [...], "headers": [...]}}.
    A request matches the first not-yet-exhausted route with the same method whose `url` is a substring of the request URL; no match is an
    AssertionError (an unexpected call is a test failure, never a silent 200). `from_file` loads a list of routes from JSON under
    tests/data_studio/<platform>/. Every call is recorded in `.calls`."""

    def __init__(self, routes: list[dict]):
        self.routes = [dict(r, _used=0) for r in routes]
        self.calls: list[dict] = []

    @classmethod
    def from_file(cls, platform: str, name: str) -> "FakeHttp":
        return cls(json.loads((DATA / platform / name).read_text()))

    def request(self, method: str, url: str, **kw) -> FakeResp:
        self.calls.append({"method": method.upper(), "url": url, **kw})
        for r in self.routes:
            if r["method"].upper() == method.upper() and r["url"] in url and r["_used"] < r.get("times", 10**9):
                r["_used"] += 1
                exp = r.get("expect") or {}
                body = kw.get("json")
                for k in exp.get("json_keys", []):
                    assert isinstance(body, dict) and k in body, f"{method} {url}: request body lacks {k!r} (has {sorted(body or {})})"
                for h in exp.get("headers", []):
                    assert h in (kw.get("headers") or {}), f"{method} {url}: missing header {h!r}"
                return FakeResp(r.get("response", {}), r.get("status", 200), json.dumps(r.get("response", {})))
        raise AssertionError(f"unexpected HTTP call: {method} {url}")

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    def put(self, url, **kw):
        return self.request("PUT", url, **kw)

    def delete(self, url, **kw):
        return self.request("DELETE", url, **kw)


class FakeHost:
    """MediaHost (spec 4.5) with no network: records what it was asked to host and delete."""

    def __init__(self, base: str = "https://media.example.invalid", *, corrupt: bool = False):
        self.base, self.corrupt = base, corrupt
        self.puts: list[dict] = []
        self.deleted: list[str] = []

    def put(self, path, *, ttl_s: int = 3600) -> dict:
        path = Path(path)
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        key = f"k{len(self.puts)}/{sha}{path.suffix}"
        rec = {"url": f"{self.base}/{key}", "key": key, "sha256": ("0" * 64) if self.corrupt else sha, "expires": ttl_s}
        self.puts.append(rec)
        return rec

    def delete(self, key: str) -> None:
        self.deleted.append(key)


# ---- builders -------------------------------------------------------------------------------------------------

def make_png(path: Path, size=(1080, 1350), color=(27, 24, 20)) -> Path:
    """A solid-colour image. The bytes are JPEG whatever the suffix: Instagram accepts JPEG only (spec E6), and every builder here queues to it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="JPEG")
    return path


def write_account(sp: StudioProject, acct_id: str, platform: str, *, mode: str = "graph", **kw) -> dict:
    cfg = {"id": acct_id, "platform": platform, "mode": mode, "label": f"@{acct_id}", "tz": "America/Denver",
           "post_types": ["feed", "carousel"], "credentials": {"ref": acct_id}, "limits": {"max_per_day": 2, "min_gap_min": 180},
           "catch_up": False, **kw}
    sp.accounts_dir.mkdir(parents=True, exist_ok=True)
    (sp.accounts_dir / f"{acct_id}.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    return cfg


def add_asset(sp: StudioProject, src: Path, *, alt: str = "A test image.", source_type: str = "own", ai: bool = False,
              derived_from=(), added: str = "2026-10-19T09:00:00-0600") -> dict:
    """Copy `src` into the content-addressed library and append its registry line (what WS1's library.add_file does, minimally)."""
    sha = safety.sha256_file(src)
    ext = src.suffix.lower()
    rel = f"library/assets/{sha[:2]}/{sha}{ext}"
    dst = sp.root / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not dst.exists():
        shutil.copy(src, dst)
    with Image.open(dst) as im:
        w, h = im.size
    rec = {"v": 1, "id": asset_id(sha), "sha256": sha, "kind": "image", "ext": ext, "file": rel, "bytes": dst.stat().st_size, "w": w, "h": h,
           "duration": None, "fps": None, "source": {"type": source_type, "provider": None, "asset_id": None, "url": None},
           "license": {"entry": None, "summary": "own work", "attribution": {"required": False, "text": None}},
           "ai_generated": ai, "derived_from": list(derived_from), "tags": ["test"], "alt": alt,
           "people": {"faces": 0, "release": None}, "origin": None, "added": added, "added_by": "maren"}
    with open(sp.library_dir / "assets.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def queue_variant(sp: StudioProject, post: dict, variant: dict, asset: dict, *, publishers: dict | None = None,
                  schedule_utc: str | None = None, caption: str | None = None) -> dict:
    """Create the variant's queue item (meta_v 2, bound to its account) and link it into the variant. Returns the queue item. Never approves."""
    plat = variant["platform"]
    pubs = publishers if publishers is not None else {plat: RecordingPublisher(plat)}
    text = caption if caption is not None else (variant["copy"].get("caption") or post["copy"].get("base") or "post")
    item = pq.create(sp, files=[sp.root / asset["file"]], media_type="image", platforms=[plat], title=text.splitlines()[0][:80] or "post",
                     caption=text, alt_texts=[asset["alt"] or "image"], schedule=schedule_utc, meta_v=2, account=variant["account"],
                     post_type=variant["post_type"], publishers=pubs, ai_disclosure=False)
    variant["queue"] = {"root": ".", "id": item["id"]}
    variant["queue_sha"] = item["sha256"]
    return item


def approve_item(sp, item_id: str, by: str = "maren", publishers: dict | None = None, *, now: datetime | None = None) -> dict:
    """Test-only approval: the human act, performed by calling the queue function directly with the exact sha (>= 8 hex)."""
    item = pq.load(sp, item_id)
    pubs = publishers if publishers is not None else {p: RecordingPublisher(p) for p in item["platforms"]}
    approved = pq.approve(sp, item_id, by, item["sha256"][:12], publishers=pubs)
    if now is not None:
        a = approved["approvals"][-1]
        a["at"] = now.strftime("%Y-%m-%dT%H:%M:%S%z")
        a["sig"] = safety.sign_approval(approved["id"], a["sha256"], a["meta_sha256"], a["by"], a["at"])
        if "bind" in a:
            a["bind_sig"] = safety.sign_bind(a["sig"], a["bind"])
        pq.save(sp, approved)
    return approved


@dataclass
class StudioBuild:
    sp: StudioProject
    clock: FakeClock
    studio: Studio
    publishers: dict
    accounts: list[dict] = field(default_factory=list)
    assets: list[dict] = field(default_factory=list)
    posts: dict[str, str] = field(default_factory=dict)      # lifecycle name -> post id (first of that kind)
    post_ids: list[str] = field(default_factory=list)


ACCOUNTS = (("ig-main", "instagram"), ("fb-page", "facebook"), ("ig-alt", "instagram"))
# post i gets lifecycle LIFECYCLE[i % 6]
LIFECYCLE = ("idea", "draft", "variant", "in_review", "scheduled", "approved")


def build_studio(tmp: Path, *, accounts: int = 3, posts: int = 12, clock: FakeClock | None = None) -> StudioBuild:
    """A studio with `accounts` accounts and `posts` posts spread over every pre-send lifecycle state (idea, draft, variant without
    queue item, in_review, approved+scheduled, approved pool). Images are PIL-generated; queue items are real (meta_v 2); nothing is sent."""
    clock = clock or FakeClock()
    sp = StudioProject(tmp / "studio")
    sp.create(title="Acme Bakery", tz="America/Denver")
    from ave.studio import sendgate
    sendgate.write(sp, "maren")           # the human's signed permission for an unattended sender: library ticks in tests send with no terminal
    accts = []
    for i in range(accounts):
        aid, plat = ACCOUNTS[i] if i < len(ACCOUNTS) else (f"ig-{i}", "instagram")
        accts.append(write_account(sp, aid, plat, mode="aggregator" if aid == "ig-alt" else "graph"))
    pubs = {"instagram": RecordingPublisher("instagram"), "facebook": RecordingPublisher("facebook")}
    b = StudioBuild(sp=sp, clock=clock, studio=Studio(sp, clock=clock, publishers=pubs), publishers=pubs, accounts=accts)
    for i in range(posts):
        kind = LIFECYCLE[i % len(LIFECYCLE)]
        pid = f"p_fx{i:03d}"
        if kind == "idea":
            post = new_post(idea=f"Idea number {i}", post_id=pid, now=clock.now())
        else:
            png = make_png(sp.work / "gen" / f"img{i}.png", color=(20 + i * 7 % 200, 40 + i * 13 % 200, 60 + i * 29 % 200))
            asset = add_asset(sp, png, alt=f"Picture number {i} of a loaf of bread on a table.")
            b.assets.append(asset)
            post = new_post(idea=f"Post {i}", post_id=pid, now=clock.now(), copy={"base": f"Day {i}: the starter smells like pears."},
                            media=[{"slot": 0, "asset": asset["id"], "role": "image", "alt": asset["alt"], "crop": {"grid_focus": [0.5, 0.4]}}])
            if kind != "draft":
                acct = accts[i % len(accts)]
                v = new_variant(account=acct["id"], platform=acct["platform"], post_type="feed", variant_id=f"v_{i:03d}")
                post["variants"].append(v)
                if kind in ("in_review", "scheduled", "approved"):
                    sched = None
                    if kind == "scheduled":
                        at = (clock.now() + timedelta(days=2 + i)).replace(minute=0, second=0, microsecond=0)
                        v["schedule"] = {"local": at.strftime("%Y-%m-%dT%H:%M"), "tz": "UTC", "utc": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                         "slot": None, "locked": False, "set_by": "agent"}
                        sched = at.isoformat()
                    item = queue_variant(sp, post, v, asset, publishers=pubs, schedule_utc=sched)
                    if kind in ("scheduled", "approved"):
                        approve_item(sp, item["id"], publishers=pubs, now=clock.now())
        saved = store.save_post(sp, post, expected_rev=None, actor=AGENT, op="create", now=clock.now())
        b.post_ids.append(saved["id"])
        b.posts.setdefault(kind, saved["id"])
    return b


def make_episode_with_queue(tmp: Path, *, approve: bool = False) -> tuple[Episode, dict]:
    """A plain Episode with one image queue item (for adoption tests: queue.* runs on an Episode exactly as on a StudioProject)."""
    ep = Episode(tmp / "ep-001")
    ep.create("Episode 1")
    png = make_png(ep.exports / "card.png")
    item = pq.create(ep, files=[png], media_type="image", platforms=["instagram"], title="Episode card", caption="New episode.",
                     alt_texts=["A card announcing the new episode."], ai_disclosure=False,
                     publishers={"instagram": RecordingPublisher("instagram")})
    if approve:
        item = approve_item(ep, item["id"])
    return ep, item
