"""Builders for the WS3 scheduler/sync tests (tests/studio/test_sched_*.py). No network, no real clock, no real keychain.

    World             a small studio: accounts, rule files, a FakeClock, one RecordingPublisher per account (keyed by account id)
    world.add_post()  a licensed asset + a post with one variant, optionally scheduled, optionally synced into the queue
    world.approve()   the HUMAN act, performed in tests by calling queue.approve directly, with the approval stamped on the FakeClock
    world.go_live()   writes the HMAC live attestation exactly as spec 2.5 defines it
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from studio_helpers import FakeClock, RecordingPublisher, add_asset, make_png, write_account

from ave.publish import queue as pq, safety
from ave.schedule import register, slots
from ave.studio import dispatch, store
from ave.studio.events import AGENT, HUMAN
from ave.studio.project import StudioProject
from ave.studio.service import Studio
from ave.studio.sync import sync_queue

RULES = {"tz": "America/Denver", "rules": [{"id": "r1", "dow": ["mon", "wed", "fri"], "time": "09:00", "post_types": ["feed", "carousel"]},
                                          {"id": "r2", "dow": ["tue", "thu"], "time": "12:30", "post_types": ["story"]}]}


@pytest.fixture(autouse=True)
def no_agent_markers(monkeypatch):
    """The suite may itself run inside an agent harness; the sending tick refuses when markers are set (tested separately)."""
    for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "AVE_AGENT"):
        monkeypatch.delenv(k, raising=False)


@dataclass
class World:
    sp: StudioProject
    clock: FakeClock
    studio: Studio
    pubs: dict
    n: int = 0
    posts: list = field(default_factory=list)

    # ---- accounts
    def go_live(self, account: str = "ig-main", by: str = "maren", at: str = "2026-10-19T09:00:00-0600") -> Path:
        from ave.platforms import attest
        attest.write_live(self.sp.accounts_dir, account, by, at)
        return attest.live_path(self.sp.accounts_dir, account)

    # ---- posts
    def asset(self, alt: str = "A loaf of bread on a table.", licensed: bool = True) -> dict:
        self.n += 1
        png = make_png(self.sp.work / "gen" / f"img{self.n}.png", color=(10 + self.n * 17 % 200, 30 + self.n * 31 % 200, 50 + self.n * 7 % 200))
        if licensed:
            from ave import library       # a real library add: registry line plus a hash-chained license entry (the send gates check both)
            rec = library.add_file(self.sp, png, source={"type": "own"}, by="test", alt=alt)
        else:
            rec = add_asset(self.sp, png, alt=alt)      # registry line with no license entry: the license gate must refuse it
        return rec

    def license_it(self, rec: dict) -> dict:
        """Give an already-registered asset a real, hash-chained own-work license entry (what the send gates check)."""
        from ave.library import registry
        from ave.library.ingest import default_own_entry
        entry = {**default_own_entry("test", "2026-10-01T00:00:00"), "sha256": rec["sha256"], "file": rec["file"],
                 "bytes": (self.sp.root / rec["file"]).stat().st_size, "approved_by": "test"}
        return registry.license_log(self.sp).append(entry)

    def add_post(self, caption: str = "Day 3: the starter smells like pears.", *, account: str = "ig-main", at: str | datetime | None = None,
                 sync: bool = True, licensed: bool = True, post_type: str = "feed", alt: str = "A loaf of bread on a table.") -> tuple[str, str]:
        """(post_id, variant_id). `at` = an aware datetime or a UTC ISO string; None = unscheduled."""
        asset = self.asset(alt=alt, licensed=licensed)
        post = self.studio.create_post(idea=caption[:30], copy={"base": caption},
                                       media=[{"slot": 0, "asset": asset["id"], "role": "image", "alt": alt, "crop": {}}])
        platform = yaml.safe_load((self.sp.accounts_dir / f"{account}.yaml").read_text())["platform"]
        post = self.studio.add_variant(post["id"], account=account, platform=platform, post_type=post_type, expected_rev=post["rev"])
        vid = post["variants"][-1]["id"]
        if at is not None:
            at = datetime.fromisoformat(at.replace("Z", "+00:00")) if isinstance(at, str) else at
            self.set_schedule(post["id"], vid, at)
        if sync:
            sync_queue(self.sp, post["id"], publishers=self.pubs, now=self.clock.now())
        self.posts.append((post["id"], vid))
        return post["id"], vid

    def set_schedule(self, post_id: str, vid: str, at: datetime) -> None:
        """Write a schedule block directly (bypasses move's checks: some tests need slots in the past)."""
        post = store.load_post(self.sp, post_id)
        z = at.astimezone(__import__("zoneinfo").ZoneInfo("America/Denver"))
        from ave.studio.model import find_variant
        find_variant(post, vid)["schedule"] = slots.make_schedule(z, slot=None, set_by="agent")
        store.save_post(self.sp, post, expected_rev=post["rev"], actor=AGENT, op="test_schedule", now=self.clock.now())

    # ---- views
    def variant(self, post_id: str, vid: str) -> dict:
        from ave.studio.model import find_variant
        return find_variant(store.load_post(self.sp, post_id), vid)

    def item(self, post_id: str, vid: str) -> dict:
        v = self.variant(post_id, vid)
        return pq.load(self.sp, v["queue"]["id"])

    def approve(self, post_id: str, vid: str, by: str = "maren") -> dict:
        """Approve the variant's queue item (the human act) and stamp the approval with the fake clock, re-signed, so TTL tests are exact."""
        item = self.item(post_id, vid)
        by_platform = {p.platform: p for p in self.pubs.values()}          # queue.approve's preflight looks adapters up by platform
        out = pq.approve(self.sp, item["id"], by, item["sha256"][:12], publishers=by_platform,
                         bind=dispatch.approval_bind(self.sp, self.variant(post_id, vid)))          # what the CLI and the UI sign into an approval
        a = out["approvals"][-1]
        a["at"] = self.clock.now().strftime("%Y-%m-%dT%H:%M:%S%z")
        a["sig"] = safety.sign_approval(out["id"], a["sha256"], a["meta_sha256"], a["by"], a["at"])
        if "bind" in a:
            a["bind_sig"] = safety.sign_bind(a["sig"], a["bind"])
        pq.save(self.sp, out)
        return out

    def tick(self, **kw):
        from ave.schedule.tick import tick
        kw.setdefault("publishers", self.pubs)
        return tick(self.sp, clock=self.clock, **kw)

    def alerts(self, kind: str | None = None) -> list[dict]:
        from ave.studio import ledger
        return [a for a in ledger.list_alerts(self.sp, include_acked=True) if kind in (None, a["kind"])]

    def events(self, op: str | None = None) -> list[dict]:
        from ave.studio import ledger
        rows, _ = ledger.read_jsonl(ledger.events_path(self.sp))
        return [r for r in rows if op in (None, r["op"])]


def make_world(tmp: Path, *, accounts=(("ig-main", "instagram"),), behaviors=("ok",), rules: bool = True, now=None, **acct_kw) -> World:
    clock = FakeClock(now) if now else FakeClock()
    sp = StudioProject(tmp / "studio")
    sp.create(title="Acme Bakery", tz="America/Denver")
    from ave.studio import sendgate
    sendgate.write(sp, "maren")           # the human's signed permission for an unattended sender: library ticks in tests send with no terminal
    pubs = {}
    for aid, plat in accounts:
        write_account(sp, aid, plat, **acct_kw)
        pubs[aid] = RecordingPublisher(plat, behaviors)
        if rules:
            (sp.schedule_dir / f"{aid}.yaml").write_text(yaml.safe_dump(RULES, sort_keys=False))
    w = World(sp=sp, clock=clock, studio=Studio(sp, clock=clock, publishers=pubs), pubs=pubs)
    register(w.studio)
    return w


def utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def minutes(n: int) -> timedelta:
    return timedelta(minutes=n)
