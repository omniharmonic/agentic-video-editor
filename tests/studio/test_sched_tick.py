"""WS3 scheduler: every invariant of spec 6.3 and every scenario of spec 10.5, with FakeClock and RecordingPublisher. Nothing here touches a
network; nothing is ever sent except to a RecordingPublisher."""

import itertools
import json
import os
import random
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from sched_helpers import make_world, no_agent_markers, utc  # noqa: F401
from studio_helpers import FakeClock, RecordingPublisher

from ave.platforms import attest
from ave.publish import queue as pq, safety
from ave.publish.adapters.base import PublishError, Uncertain
from ave.schedule import limits, retry, slots
from ave.schedule.daemon import check_heartbeat, run_loop
from ave.schedule.tick import TickRefused, tick
from ave.studio import dispatch, ledger, state, store
from ave.studio.events import AGENT, HUMAN

SLOT = "2026-10-21T15:00:00Z"            # Wed 09:00 America/Denver
NOW = "2026-10-21T15:00:30Z"
ROOT = Path(__file__).resolve().parents[2]


class ScriptedPublisher(RecordingPublisher):
    """Each publish call follows `script`: "ok", or ("fail"|"uncertain"|"conn", message). The last step repeats."""

    def __init__(self, script, platform="instagram", **kw):
        super().__init__(platform, **kw)
        self.script = list(script)

    def publish(self, item, video, progress):
        step = self.script[min(self.publish_calls, len(self.script) - 1)]
        if isinstance(step, tuple):
            self.publish_calls += 1
            raise {"fail": PublishError, "uncertain": Uncertain, "conn": ConnectionError}[step[0]](step[1])
        return super().publish(item, video, progress)


def ready(tmp, behaviors=("ok",), *, live=True, slot=SLOT, now=NOW, pub=None, **kw):
    """A world with one approved, scheduled post on a live account, the clock just after its slot."""
    w = make_world(tmp, behaviors=behaviors, **kw)
    if pub is not None:
        w.pubs["ig-main"] = pub
    pid, vid = w.add_post(at=slot)
    w.approve(pid, vid)
    if live:
        w.go_live()
    w.clock.set(now)
    return w, pid, vid


def sent(w):
    return w.pubs["ig-main"].sent


def calls(w):
    return w.pubs["ig-main"].publish_calls


# ------------------------------------------------------------------------------------------------ dry-run is the default

def test_default_tick_is_a_rehearsal_that_posts_nothing(tmp_path):
    w, pid, vid = ready(tmp_path)
    rep = w.tick()                                   # send defaults to False
    assert rep.mode == "dry-run" and calls(w) == 0
    assert rep.would_post == [{"post": pid, "variant": vid, "account": "ig-main", "slot": SLOT, "queue": w.item(pid, vid)["id"]}]
    assert [e["op"] for e in w.events("would_post")] == ["would_post"]
    item = w.item(pid, vid)
    assert item["status"] == "approved" and item["results"] == {} and w.alerts() == []
    assert not (w.sp.schedule_dir / "heartbeat.json").exists()               # a rehearsal does not pretend to be the daemon
    assert w.studio.rehearse()["mode"] == "dry-run" and calls(w) == 0
    assert w.studio.tick(send=False)["would_post"]


def test_rehearsal_lists_why_an_item_is_blocked(tmp_path):
    w, pid, vid = ready(tmp_path, live=False)
    out = w.studio.rehearse()
    ref = json.loads((Path(__file__).parent.parent / "data_studio" / "api" / "POST_tick_rehearse.json").read_text())["response"]
    assert set(out) >= set(ref) and out["would_post"] == []
    assert out["blocked"][0]["post"] == pid and "not live" in out["blocked"][0]["reasons"][0] and out["blocked"][0]["codes"] == ["not_live"]
    assert w.alerts() == []                                               # alerts are the sending scheduler's job


def test_send_posts_exactly_once_across_repeated_ticks(tmp_path):
    w, pid, vid = ready(tmp_path)
    rep = w.tick(send=True)
    assert rep.mode == "send" and [p["post"] for p in rep.posted] == [pid] and calls(w) == 1
    assert sent(w)[0]["account"] == "ig-main" and sent(w)[0]["caption"] == "Day 3: the starter smells like pears."
    item, v = w.item(pid, vid), w.variant(pid, vid)
    assert item["status"] == "published" and item["results"]["instagram"]["state"] == "published"
    assert v["result"]["state"] == "published" and v["result"]["url"].startswith("https://example.invalid/") and v["attempts"][-1]["outcome"] == "published"
    assert store.load_post(w.sp, pid)["stage"] == "archived"
    for _ in range(5):
        w.clock.advance(minutes=10)
        w.tick(send=True)
    w.clock.advance(days=3)
    w.tick(send=True)
    assert calls(w) == 1 and len(sent(w)) == 1
    assert (w.sp.schedule_dir / "heartbeat.json").exists()
    assert dispatch.open_dispatches(w.sp) == {} and [e["op"] for e in w.events() if e["op"].startswith("dispatch_")] == ["dispatch_started", "dispatch_finished"]


def test_the_scheduler_never_posts_before_the_slot_and_posts_at_it(tmp_path):
    w, pid, vid = ready(tmp_path, now="2026-10-21T14:59:59Z")
    assert w.tick(send=True).posted == [] and calls(w) == 0
    w.clock.set(SLOT)
    assert len(w.tick(send=True).posted) == 1 and calls(w) == 1


# ------------------------------------------------------------------------------------------------ the gates (6.3)

def _unapproved(w, pid, vid):
    item = w.item(pid, vid)
    item["status"], item["approvals"] = "awaiting_approval", []
    pq.save(w.sp, item)


def _bytes_changed(w, pid, vid):
    f = w.sp.root / store.read_assets(w.sp)[store.load_post(w.sp, pid)["media"][0]["asset"]]["file"]
    f.write_bytes(f.read_bytes() + b"\0")


def _copy_changed_by_hand(w, pid, vid):
    item = w.item(pid, vid)
    item["caption"] = "A caption nobody approved."
    pq.save(w.sp, item)


def _signature_forged(w, pid, vid):
    item = w.item(pid, vid)
    for a in item["approvals"]:
        a["sig"] = "0" * 64
    pq.save(w.sp, item)


def _approval_added_by_hand(w, pid, vid):
    item = w.item(pid, vid)
    item["status"], item["approvals"] = "approved", [{"by": "claude", "at": "2026-10-21T15:00:00+0000", "sha256": item["sha256"],
                                                      "meta_sha256": safety.meta_hash(item), "sig": "a" * 64}]
    pq.save(w.sp, item)


def _approval_expired(w, pid, vid):
    item = w.item(pid, vid)                                     # an honest, correctly signed approval that is 20 days old
    for a in item["approvals"]:
        a["at"] = "2026-10-01T10:00:00+0000"
        a["sig"] = safety.sign_approval(item["id"], a["sha256"], a["meta_sha256"], a["by"], a["at"])
        if "bind" in a:
            a["bind_sig"] = safety.sign_bind(a["sig"], a["bind"])
    pq.save(w.sp, item)


def _not_live(w, pid, vid):
    attest.live_path(w.sp.accounts_dir, "ig-main").unlink(missing_ok=True)


def _live_forged(w, pid, vid):
    p = attest.live_path(w.sp.accounts_dir, "ig-main")
    data = json.loads(p.read_text()) if p.exists() else {"account": "ig-main", "live": True, "at": "2026-10-21T00:00:00Z", "sig": "c" * 64}
    data["by"] = "claude"
    p.write_text(json.dumps(data))


def _live_edited_into_existence(w, pid, vid):
    p = attest.live_path(w.sp.accounts_dir, "ig-main")
    p.write_text(json.dumps({"account": "ig-main", "live": True, "by": "claude", "at": "2026-10-21T00:00:00Z", "sig": "b" * 64}))


def _live_switched_off(w, pid, vid):
    p = attest.live_path(w.sp.accounts_dir, "ig-main")
    at = "2026-10-19T09:00:00-0600"
    attest.write_live(w.sp.accounts_dir, "ig-main", "maren", at, live=False)


def _studio_paused(w, pid, vid):
    w.sp.pause_path.write_text("")


def _account_paused(w, pid, vid):
    (w.sp.accounts_dir / "ig-main.paused").write_text("")


def _license_gap(w, pid, vid):
    (w.sp.library_dir / "licenses.jsonl").write_text("")       # the license trail for these bytes is gone: the send must refuse


def _adapter_not_live(w, pid, vid):
    w.pubs["ig-main"].live = False


def _adapter_missing_credentials(w, pid, vid):
    w.pubs["ig-main"].missing_credentials = ["instagram_token@ig-main"]


def _rejected(w, pid, vid):
    pq.reject(w.sp, w.item(pid, vid)["id"], "maren", "no")


def _variant_dropped(w, pid, vid):
    post = store.load_post(w.sp, pid)
    post["variants"][0]["dropped"] = True
    store.save_post(w.sp, post, expected_rev=post["rev"], actor=HUMAN, op="drop")


def _open_feedback(kind):
    def f(w, pid, vid):
        w.studio.add_feedback(pid, kind, note="redo this", variant=vid)
    return f


GATES = {
    "unapproved": _unapproved, "bytes_changed": _bytes_changed, "copy_changed_by_hand": _copy_changed_by_hand,
    "signature_forged": _signature_forged, "approval_added_by_hand": _approval_added_by_hand, "approval_expired": _approval_expired,
    "not_live": _not_live, "live_forged": _live_forged, "live_edited_into_existence": _live_edited_into_existence,
    "live_switched_off": _live_switched_off, "account_paused": _account_paused, "studio_paused": _studio_paused,
    "license_gap": _license_gap, "adapter_not_live": _adapter_not_live, "adapter_missing_credentials": _adapter_missing_credentials,
    "rejected": _rejected, "variant_dropped": _variant_dropped,
    "open_rework_copy": _open_feedback("rework_copy"), "open_rework_visual": _open_feedback("rework_visual"),
    "open_retime": _open_feedback("retime"),
}


def test_the_positive_control_sends(tmp_path):
    w, *_ = ready(tmp_path)
    assert len(w.tick(send=True).posted) == 1


@pytest.mark.parametrize("gate", sorted(GATES))
def test_every_single_gate_blocks_a_send(tmp_path, gate):
    """Invariant 6.3: remove ANY ONE condition and nothing is sent, no matter how the scheduler is called."""
    w, pid, vid = ready(tmp_path)
    GATES[gate](w, pid, vid)
    for _ in range(3):
        w.tick(send=True)
        w.clock.advance(minutes=1)
    assert calls(w) == 0 and sent(w) == []
    assert w.item(pid, vid)["status"] != "published" and w.variant(pid, vid)["result"] is None
    assert w.item(pid, vid)["results"] in ({}, None) or all(r["state"] != "published" for r in w.item(pid, vid)["results"].values())


def test_without_send_true_nothing_is_sent_even_when_every_gate_is_open(tmp_path):
    w, *_ = ready(tmp_path)
    for _ in range(3):
        w.tick()
        w.tick(send=False)
    assert calls(w) == 0


def test_sending_happens_only_if_all_gates_hold_over_random_combinations(tmp_path_factory):
    """Property: for every pair of violated gates (and each alone, and none) the post goes out iff NO gate was violated."""
    names = ["unapproved", "bytes_changed", "signature_forged", "approval_expired", "not_live", "live_forged", "account_paused", "license_gap",
             "adapter_not_live"]
    combos = [()] + [(n,) for n in names] + list(itertools.combinations(names, 2))
    for i, combo in enumerate(combos):
        w, pid, vid = ready(tmp_path_factory.mktemp(f"c{i}"))
        for g in combo:
            GATES[g](w, pid, vid)
        w.tick(send=True)
        assert (calls(w) == 1) == (combo == ()), f"violated={combo} sent={calls(w)}"
        assert calls(w) <= 1


def test_an_unsynced_or_dropped_or_unscheduled_variant_is_never_sent(tmp_path):
    w = make_world(tmp_path)
    w.go_live()
    w.add_post(at=SLOT, sync=False)                      # no queue item
    w.add_post()                                         # unscheduled, in review
    pid, vid = w.add_post()
    w.approve(pid, vid)                                  # approved but in the pool: no slot, no send
    w.clock.set(NOW)
    rep = w.tick(send=True)
    assert calls(w) == 0 and rep.posted == []


def test_the_queue_independently_refuses_when_the_adapter_says_not_live(tmp_path):
    """6.3 second bullet: a bug in tick cannot bypass the account gate, because queue.run asks the adapter itself."""
    w, pid, vid = ready(tmp_path, live=False)
    pub = w.pubs["ig-main"]
    pub.live = False
    out = pq.run(w.sp, w.item(pid, vid)["id"], send=True, publishers={"instagram": pub})
    assert out["ok"] is False and any("not live" in b for b in out["blocked"]) and pub.publish_calls == 0
    assert w.item(pid, vid)["status"] == "approved"


def test_a_changed_account_binding_blocks_the_send(tmp_path):
    w, pid, vid = ready(tmp_path, accounts=(("ig-main", "instagram"), ("ig-alt", "instagram")))
    item = w.item(pid, vid)
    item["account"] = "ig-alt"
    pq.save(w.sp, item)
    w.tick(send=True)
    assert calls(w) == 0


def test_sending_needs_an_account_bound_adapter_never_a_global_one(tmp_path):
    w, pid, vid = ready(tmp_path)
    rep = w.tick(send=True, publishers={})
    assert calls(w) == 0 and rep.blocked[0]["codes"] == ["no_adapter"]
    # Pure rehearsal still checks the selected account mode; a missing injected
    # fixture adapter must not silently fall back to a different platform contract.
    rehearsal=w.tick(send=False, publishers={})
    assert not rehearsal.would_post and rehearsal.blocked[0]["codes"] == ["queue"]


def test_the_adapter_is_found_by_account_id_first_then_platform(tmp_path):
    w, pid, vid = ready(tmp_path, accounts=(("ig-main", "instagram"), ("ig-alt", "instagram")))
    other = RecordingPublisher("instagram")
    w.tick(send=True, publishers={"instagram": other, "ig-main": w.pubs["ig-main"]})
    assert calls(w) == 1 and other.publish_calls == 0
    assert dispatch.adapter_for({"instagram": other}, {"account": "ig-x", "platform": "instagram"}) is other


# ------------------------------------------------------------------------------------------------ pause, kill switches

def test_pause_stops_everything_and_resume_lets_it_go(tmp_path, monkeypatch):
    w, pid, vid = ready(tmp_path)
    w.studio.pause()
    rep = w.tick(send=True)
    assert rep.paused and calls(w) == 0 and rep.posted == [] and rep.blocked == []
    monkeypatch.setattr("ave.humanpresence.is_human_terminal", lambda: True)
    w.studio.resume(by="maren")
    assert len(w.tick(send=True).posted) == 1


def test_a_per_account_pause_only_stops_that_account(tmp_path):
    w = make_world(tmp_path, accounts=(("ig-main", "instagram"), ("fb-page", "facebook")))
    ids = {}
    for a in ("ig-main", "fb-page"):
        ids[a] = w.add_post(at=SLOT, account=a)
        w.approve(*ids[a])
        w.go_live(a)
    w.clock.set(NOW)
    w.studio.pause("fb-page")
    rep = w.tick(send=True)
    assert [p["account"] for p in rep.posted] == ["ig-main"] and w.pubs["fb-page"].publish_calls == 0
    assert rep.blocked[0]["codes"] == ["account_paused"]


def test_sending_is_refused_in_an_agent_environment_but_rehearsal_is_fine(tmp_path, monkeypatch):
    w, *_ = ready(tmp_path)
    monkeypatch.setenv("CLAUDECODE", "1")
    with pytest.raises(TickRefused) as e:
        w.tick(send=True)
    assert "CLAUDECODE" in str(e.value) and calls(w) == 0
    assert w.tick(send=False).would_post
    assert calls(w) == 0
    monkeypatch.setenv("AVE_AGENT", "1")
    with pytest.raises(TickRefused):
        w.studio.tick(send=True)


def test_no_unattended_approval_switch_exists_anywhere_in_the_scheduler():
    root = ROOT / "src" / "ave"
    text = "".join(p.read_text() for d in ("schedule", "studio") for p in (root / d).glob("*.py"))
    assert "AVE_ALLOW_UNATTENDED" not in text


# ------------------------------------------------------------------------------------------------ account gating before the first alert

def test_account_not_live_is_alerted_once_per_slot(tmp_path):
    w, pid, vid = ready(tmp_path, live=False)
    for _ in range(4):
        rep = w.tick(send=True)
        w.clock.advance(minutes=5)
    assert calls(w) == 0 and rep.blocked[0]["codes"] == ["not_live"]
    al = w.alerts("account_not_live")
    assert len(al) == 1 and al[0]["account"] == "ig-main" and al[0]["post"] == pid and SLOT in al[0]["message"]


def test_the_studio_ttl_is_judged_on_the_injected_clock(tmp_path):
    w, pid, vid = ready(tmp_path)                                        # approved at 2026-10-19T16:00Z, slot two days later
    meta = json.loads(w.sp.meta_path.read_text())
    meta["approval"]["ttl_days"] = 1
    w.sp.meta_path.write_text(json.dumps(meta))
    rep = w.tick(send=True)
    assert calls(w) == 0 and rep.blocked[0]["codes"] == ["approval_expired"] and len(w.alerts("approval_expired")) == 1
    meta["approval"]["ttl_days"] = 3
    w.sp.meta_path.write_text(json.dumps(meta))
    assert len(w.tick(send=True).posted) == 1
    w2, p2, v2 = ready(tmp_path / "acct")                                # an account may only TIGHTEN the studio default
    cfg = yaml.safe_load((w2.sp.accounts_dir / "ig-main.yaml").read_text())
    cfg["approval"] = {"ttl_days": 1}
    (w2.sp.accounts_dir / "ig-main.yaml").write_text(yaml.safe_dump(cfg))
    assert w2.tick(send=True).posted == []
    cfg["approval"] = {"ttl_days": 90}
    (w2.sp.accounts_dir / "ig-main.yaml").write_text(yaml.safe_dump(cfg))
    assert len(w2.tick(send=True).posted) == 1


def test_a_missing_credential_is_an_alert_not_a_crash(tmp_path):
    w, pid, vid = ready(tmp_path)
    w.pubs["ig-main"].missing_credentials = ["instagram_token@ig-main"]
    rep = w.tick(send=True)
    assert calls(w) == 0 and rep.blocked[0]["codes"] == ["credentials"] and len(w.alerts("credentials_missing")) == 1


def test_license_gap_and_expired_approval_and_manual_accounts_alert_once(tmp_path):
    w, pid, vid = ready(tmp_path)
    _license_gap(w, pid, vid)
    for _ in range(3):
        w.tick(send=True)
    assert len(w.alerts("license_gap")) == 1 and calls(w) == 0
    w2, p2, v2 = ready(tmp_path / "x")
    _approval_expired(w2, p2, v2)
    for _ in range(3):
        w2.tick(send=True)
    assert len(w2.alerts("approval_expired")) == 1


def test_a_manual_platform_account_gets_a_reminder_not_a_post(tmp_path, monkeypatch):
    monkeypatch.setattr(pq, "ALL_PLATFORMS", (*pq.ALL_PLATFORMS, "manual"))          # WS2 (E3, E6) registers the manual platform for real
    monkeypatch.setattr(pq, "image_problems", lambda *a, **k: [])
    w = make_world(tmp_path, accounts=(("blog", "manual"),), mode="manual")
    pid, vid = w.add_post(at=SLOT, account="blog")
    w.approve(pid, vid)
    w.go_live("blog")
    w.clock.set(NOW)
    rep = w.tick(send=True)
    assert rep.blocked[0]["codes"] == ["manual"] and len(w.alerts("manual_post_due")) == 1 and w.pubs["blog"].publish_calls == 0


# ------------------------------------------------------------------------------------------------ concurrency

class BlockingPublisher(RecordingPublisher):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.entered, self.release = threading.Event(), threading.Event()

    def publish(self, item, video, progress):
        self.entered.set()
        assert self.release.wait(10), "test deadlock"
        return super().publish(item, video, progress)


def test_two_overlapping_ticks_post_once(tmp_path):
    pub = BlockingPublisher("instagram")
    w, pid, vid = ready(tmp_path, pub=pub)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("a", w.tick(send=True)))
    t.start()
    assert pub.entered.wait(10)
    second = w.tick(send=True)                                  # tick A is inside the send
    assert second.locked is True and second.posted == [] and calls(w) == 0
    assert w.tick(send=False).locked is True                    # a rehearsal takes the same lock
    pub.release.set()
    t.join(10)
    assert len(out["a"].posted) == 1 and calls(w) == 1 and len(pub.sent) == 1
    assert w.tick(send=True).posted == [] and calls(w) == 1


def test_the_per_item_queue_lock_is_a_second_layer(tmp_path):
    w, pid, vid = ready(tmp_path)
    with pq._lock(w.sp, w.item(pid, vid)["id"]):                # another process is already working on this item
        rep = w.tick(send=True)
    assert calls(w) == 0 and rep.posted == [] and "locked by another process" in rep.deferred[0]["reason"]
    assert dispatch.open_dispatches(w.sp) == {}
    assert len(w.tick(send=True).posted) == 1 and calls(w) == 1


CHILD = r"""
import sys, time
sys.path[:0] = [{src!r}, {tests!r}]
from studio_helpers import FakeClock, RecordingPublisher
from ave.studio.project import StudioProject
from ave.schedule.tick import tick
class Slow(RecordingPublisher):
    def publish(self, item, video, progress):
        time.sleep(1.5)
        with open({log!r}, "a") as f:
            f.write("sent\n")
        return super().publish(item, video, progress)
from ave.publish import safety;age=safety.approval_age_days;safety.approval_age_days=lambda at,now=None:age(at,FakeClock({now!r}).now().timestamp() if now is None else now)
rep = tick(StudioProject({sp!r}), clock=FakeClock({now!r}), send=True, publishers={{"ig-main": Slow("instagram")}})
print("locked" if rep.locked else "ran", len(rep.posted))
"""


def test_two_concurrent_tick_processes_post_once(tmp_path, monkeypatch):
    monkeypatch.setenv("AVE_APPROVAL_KEY", "k" * 64)
    w, pid, vid = ready(tmp_path)
    log = tmp_path / "sent.log"
    code = CHILD.format(src=str(ROOT / "src"), tests=str(ROOT / "tests"), log=str(log), sp=str(w.sp.root), now=NOW)
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "AVE_AGENT")}
    env.update(AVE_APPROVAL_KEY="k" * 64, HOME=str(tmp_path / "home"))
    procs = [subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    outs = [p.communicate(timeout=60) for p in procs]
    assert all(p.returncode == 0 for p in procs), outs
    assert sorted(o[0].strip() for o in outs) == ["locked 0", "ran 1"], outs
    assert log.read_text().count("sent") == 1
    assert w.item(pid, vid)["status"] == "published"


# ------------------------------------------------------------------------------------------------ crash safety

def test_crash_after_sending_is_never_retried(tmp_path):
    w, pid, vid = ready(tmp_path, ("crash_after_send",))
    with pytest.raises(KeyboardInterrupt):
        w.tick(send=True)                                         # the process dies after the post reached the platform
    assert calls(w) == 1 and len(sent(w)) == 1
    item = w.item(pid, vid)
    assert item["status"] == "publishing" and item["results"]["instagram"]["state"] == "sending"
    assert len(dispatch.open_dispatches(w.sp)) == 1
    assert state.derive(w.variant(pid, vid), item, [], w.clock.now(), w.sp) == "publishing"
    for step in (1, 5, 30, 200, 60 * 24 * 3):                       # inside the window, after it, days later
        w.clock.advance(minutes=step)
        rep = w.tick(send=True)
        assert rep.posted == [] and rep.uncertain and rep.locked is False
    assert calls(w) == 1 and len(sent(w)) == 1                    # NEVER auto-retried
    al = w.alerts("uncertain")
    assert len(al) == 1 and al[0]["severity"] == "error" and "resolve" in al[0]["message"]
    assert w.variant(pid, vid)["result"] is None or w.variant(pid, vid)["result"]["state"] == "uncertain"


def test_a_crash_before_the_platform_call_is_also_unknown_not_retried(tmp_path):
    w, pid, vid = ready(tmp_path, ("crash", "ok"))
    with pytest.raises(KeyboardInterrupt):
        w.tick(send=True)
    for _ in range(3):
        w.clock.advance(minutes=1)
        assert w.tick(send=True).uncertain
    assert calls(w) == 1 and sent(w) == []                         # the scheduler cannot tell that nothing went out; only a human can


def test_an_uncertain_adapter_outcome_is_never_retried(tmp_path):
    w, pid, vid = ready(tmp_path, ("uncertain", "ok"))
    rep = w.tick(send=True)
    assert rep.uncertain and calls(w) == 1 and w.variant(pid, vid)["result"]["state"] == "uncertain"
    for _ in range(5):
        w.clock.advance(minutes=3)
        w.tick(send=True)
    assert calls(w) == 1 and sent(w) == [] and len(w.alerts("uncertain")) == 1


def test_a_bare_network_exception_is_uncertain_so_it_is_not_retried(tmp_path):
    w, pid, vid = ready(tmp_path, ("network", "ok"))
    w.tick(send=True)
    w.clock.advance(minutes=10)
    w.tick(send=True)
    assert calls(w) == 1 and w.item(pid, vid)["results"]["instagram"]["state"] == "sending"


def test_resolve_retry_reenables_one_more_attempt(tmp_path):
    w, pid, vid = ready(tmp_path, ("crash", "ok"))
    with pytest.raises(KeyboardInterrupt):
        w.tick(send=True)
    w.clock.advance(minutes=30)
    assert w.tick(send=True).uncertain
    out = dispatch.resolve_uncertain(w.sp, pid, vid, retry=True, by="maren", now=w.clock.now())
    assert out["resolved"] == "retry" and dispatch.open_dispatches(w.sp) == {}
    assert w.item(pid, vid)["results"]["instagram"]["state"] == "failed"
    assert "resolved_retry" in [a["outcome"] for a in w.variant(pid, vid)["attempts"]]
    rep = w.tick(send=True)
    assert len(rep.posted) == 1 and calls(w) == 2 and len(sent(w)) == 1
    w.clock.advance(minutes=5)
    w.tick(send=True)
    assert calls(w) == 2


def test_resolve_posted_records_the_url_and_nothing_is_sent_again(tmp_path):
    w, pid, vid = ready(tmp_path, ("crash_after_send", "ok"))
    with pytest.raises(KeyboardInterrupt):
        w.tick(send=True)
    dispatch.resolve_uncertain(w.sp, pid, vid, posted_url="https://instagram.example/p/abc", by="maren", now=w.clock.now())
    assert w.item(pid, vid)["status"] == "published" and w.variant(pid, vid)["result"]["url"] == "https://instagram.example/p/abc"
    w.clock.advance(minutes=5)
    w.tick(send=True)
    assert calls(w) == 1


def test_resolve_refuses_ambiguous_or_pointless_requests(tmp_path):
    w, pid, vid = ready(tmp_path)
    with pytest.raises(ValueError):
        dispatch.resolve_uncertain(w.sp, pid, vid, by="maren")
    with pytest.raises(ValueError):
        dispatch.resolve_uncertain(w.sp, pid, vid, retry=True, posted_url="https://x", by="maren")
    with pytest.raises(ValueError):
        dispatch.resolve_uncertain(w.sp, pid, vid, retry=True, by="maren")          # nothing uncertain


def test_a_resolved_retry_opens_a_fresh_window_but_never_an_unbounded_one(tmp_path):
    w, pid, vid = ready(tmp_path, ("crash", "ok"))
    with pytest.raises(KeyboardInterrupt):
        w.tick(send=True)
    w.clock.advance(hours=5)
    dispatch.resolve_uncertain(w.sp, pid, vid, retry=True, by="maren", now=w.clock.now())
    w.clock.advance(minutes=121)                                              # the human sat on it past the new window
    rep = w.tick(send=True)
    assert calls(w) == 1 and rep.failed and rep.posted == [] and len(w.alerts("missed")) == 1
    assert w.variant(pid, vid)["result"]["state"] == "failed"


# ------------------------------------------------------------------------------------------------ missed slots

def test_a_stale_slot_is_missed_and_never_posted(tmp_path):
    w, pid, vid = ready(tmp_path, now="2026-10-21T17:01:00Z")                # slot 15:00 + 120 min late window = 17:00
    for _ in range(4):
        rep = w.tick(send=True)
        w.clock.advance(minutes=30)
    assert calls(w) == 0 and rep.missed and rep.posted == []
    al = w.alerts("missed")
    assert len(al) == 1 and "NOT posted" in al[0]["message"]
    assert state.derive(w.variant(pid, vid), w.item(pid, vid), [], w.clock.now(), w.sp) == "missed"


def test_a_missed_slot_is_not_posted_even_if_the_account_allows_catch_up(tmp_path):
    w, pid, vid = ready(tmp_path, now="2026-10-22T09:00:00Z", catch_up=True)
    w.tick(send=True)
    assert calls(w) == 0                                                     # invariant 6.3 beats the catch_up setting


def test_the_late_window_edges(tmp_path):
    w, pid, vid = ready(tmp_path, now="2026-10-21T17:00:00Z")                # exactly late_after_min after the slot: still on time
    assert len(w.tick(send=True).posted) == 1
    w2, p2, v2 = ready(tmp_path / "b", now="2026-10-21T17:00:01Z")
    assert w2.tick(send=True).posted == [] and calls(w2) == 0


def test_a_late_window_follows_the_studio_setting(tmp_path):
    w, pid, vid = ready(tmp_path, now="2026-10-21T15:20:00Z")
    meta = json.loads(w.sp.meta_path.read_text())
    meta["late_after_min"] = 10
    w.sp.meta_path.write_text(json.dumps(meta))
    assert w.tick(send=True).missed and calls(w) == 0


def test_retiming_a_missed_post_lets_it_go_at_the_new_slot_only(tmp_path):
    w, pid, vid = ready(tmp_path, now="2026-10-21T18:00:00Z")
    assert w.tick(send=True).missed
    w.studio.move(pid, vid, {"local": "2026-10-23T09:00", "tz": "America/Denver"}, expected_rev=store.load_post(w.sp, pid)["rev"], actor=HUMAN)
    w.clock.set("2026-10-22T15:00:00Z")
    assert w.tick(send=True).posted == [] and calls(w) == 0
    w.clock.set("2026-10-23T15:00:10Z")
    assert len(w.tick(send=True).posted) == 1 and calls(w) == 1


# ------------------------------------------------------------------------------------------------ DST

def test_fall_back_ambiguous_time_is_sent_once_at_the_first_occurrence(tmp_path):
    w = make_world(tmp_path, now="2026-11-01T06:00:00Z")
    pid, vid = w.add_post(at=slots.resolve_local("2026-11-01T01:30", "America/Denver"))
    w.approve(pid, vid)
    w.go_live()
    assert w.variant(pid, vid)["schedule"]["utc"] == "2026-11-01T07:30:00Z"
    w.clock.set("2026-11-01T07:29:59Z")
    assert w.tick(send=True).posted == [] and calls(w) == 0
    w.clock.set("2026-11-01T07:30:00Z")
    assert len(w.tick(send=True).posted) == 1
    w.clock.set("2026-11-01T08:30:00Z")                                      # the second 01:30 (MST) must NOT post it again
    w.tick(send=True)
    assert calls(w) == 1


def test_spring_forward_nonexistent_time_goes_out_at_the_shifted_minute(tmp_path):
    w = make_world(tmp_path, now="2027-03-13T12:00:00Z")
    at = slots.resolve_local("2027-03-14T02:30", "America/Denver")           # does not exist; shifts to 03:00 MDT = 09:00Z
    pid, vid = w.add_post(at=at)
    w.approve(pid, vid)
    w.go_live()
    assert w.variant(pid, vid)["schedule"]["local"] == "2027-03-14T03:00" and w.variant(pid, vid)["schedule"]["utc"] == "2027-03-14T09:00:00Z"
    w.clock.set("2027-03-14T08:59:59Z")
    assert w.tick(send=True).posted == []
    w.clock.set("2027-03-14T09:00:00Z")
    assert len(w.tick(send=True).posted) == 1 and calls(w) == 1


# ------------------------------------------------------------------------------------------------ limits and deferrals

def _two_posts(tmp, gap, per_day=2, n=2, offsets=(0, 60)):
    w = make_world(tmp, limits={"max_per_day": per_day, "min_gap_min": gap})
    ids = []
    for i in range(n):
        ids.append(w.add_post(f"Post number {i} about bread.", at=utc(SLOT) + timedelta(minutes=offsets[i])))
        w.approve(*ids[-1])
    w.go_live()
    return w, ids


def test_min_gap_defers_the_second_post_and_it_goes_out_when_the_gap_is_over(tmp_path):
    w, ids = _two_posts(tmp_path, gap=100, offsets=(0, 20))
    w.clock.set("2026-10-21T15:30:00Z")
    rep = w.tick(send=True)
    assert len(rep.posted) == 1 and rep.deferred[0]["reason"].startswith("minimum gap of 100 min") and calls(w) == 1
    assert rep.deferred[0]["until"] == "2026-10-21T17:10:00Z"                 # 100 min after the first post at 15:30
    w.clock.set("2026-10-21T16:00:00Z")
    assert w.tick(send=True).posted == [] and calls(w) == 1
    w.clock.set("2026-10-21T17:11:00Z")
    assert len(w.tick(send=True).posted) == 1 and calls(w) == 2


def test_a_deferral_that_outlasts_the_late_window_ends_as_missed(tmp_path):
    w, ids = _two_posts(tmp_path, gap=600, offsets=(0, 5))
    w.clock.set("2026-10-21T15:10:00Z")
    w.tick(send=True)
    w.clock.set("2026-10-21T17:30:00Z")
    rep = w.tick(send=True)
    assert rep.missed and calls(w) == 1                                      # never posted stale just because it was deferred


def test_max_per_day_is_a_rolling_24_hours(tmp_path):
    w = make_world(tmp_path, limits={"max_per_day": 2, "min_gap_min": 0})
    ids = [w.add_post(f"Bread post {i} with words.", at=utc(SLOT) + timedelta(minutes=10 * i)) for i in range(3)]
    for i in ids:
        w.approve(*i)
    w.go_live()
    w.clock.set("2026-10-21T15:35:00Z")
    rep = w.tick(send=True)
    assert len(rep.posted) == 2 and "max_per_day reached (2/2 in 24 h)" in rep.deferred[0]["reason"] and calls(w) == 2
    w.clock.set("2026-10-21T17:00:00Z")
    rep = w.tick(send=True)
    assert rep.posted == [] and rep.deferred and calls(w) == 2
    w.clock.set("2026-10-21T17:30:00Z")
    rep = w.tick(send=True)
    assert rep.posted == [] and rep.missed and calls(w) == 2                 # the third slot's late window closed before the day rolled over
    first_post = limits.load_state(w.sp, "ig-main")["posts"][0]
    assert first_post == "2026-10-21T15:35:00Z"
    assert limits.check(w.sp, "ig-main", "instagram", utc("2026-10-22T15:34:00Z"))
    assert limits.check(w.sp, "ig-main", "instagram", utc("2026-10-22T15:35:01Z")) == []


def test_platform_rate_blocks_use_the_config_and_alert_quota(tmp_path):
    w, ids = _two_posts(tmp_path, gap=0, per_day=10, offsets=(0, 1))
    w.clock.set("2026-10-21T15:10:00Z")
    cfg = {"instagram": {"rate": {"api_posts_per_24h": 1}}}
    rep = w.tick(send=True, platforms_cfg=cfg)
    assert len(rep.posted) == 1 and "instagram api_posts_per_24h reached" in rep.deferred[0]["reason"]
    assert len(w.alerts("quota")) == 1
    w.tick(send=True, platforms_cfg=cfg)
    assert len(w.alerts("quota")) == 1 and calls(w) == 1


def test_quiet_hours_defer_and_then_allow_inside_the_window(tmp_path):
    w, pid, vid = ready(tmp_path, quiet_hours=[{"from": "08:00", "to": "09:30"}])
    rep = w.tick(send=True)                                                  # 09:00:30 local is quiet
    assert calls(w) == 0 and rep.deferred[0]["reason"] == "quiet hours" and rep.deferred[0]["until"] >= "2026-10-21T15:30:00Z"
    w.clock.set("2026-10-21T15:31:00Z")
    assert len(w.tick(send=True).posted) == 1


def test_quiet_hours_that_outlast_the_window_mean_missed(tmp_path):
    w, pid, vid = ready(tmp_path, quiet_hours=[{"from": "08:00", "to": "13:00"}])
    w.tick(send=True)
    w.clock.set("2026-10-21T19:00:30Z")                                      # quiet ends at 13:00 local, the window closed at 11:00 local
    assert w.tick(send=True).missed and calls(w) == 0


# ------------------------------------------------------------------------------------------------ retries and failure classes

LATE_600 = {"late_after_min": 600}


def _studio_setting(w, **kv):
    meta = json.loads(w.sp.meta_path.read_text())
    meta.update(kv)
    w.sp.meta_path.write_text(json.dumps(meta))


def test_network_failures_back_off_5_30_120_minutes_then_stop_after_three_retries(tmp_path):
    pub = ScriptedPublisher([("fail", "Connection reset before any upload")])
    w, pid, vid = ready(tmp_path, pub=pub)
    _studio_setting(w, late_after_min=600)
    t0 = utc(NOW)
    plan = [(0, 1, 5), (5, 2, 30), (35, 3, 120)]                             # (minutes after t0 of the attempt, calls so far, next backoff)
    for at, n, back in plan:
        w.clock.set(t0 + timedelta(minutes=at))
        rep = w.tick(send=True)
        assert calls(w) == n and rep.retry_scheduled[0]["retry_at"] == iso(t0 + timedelta(minutes=at + back))
        w.clock.set(t0 + timedelta(minutes=at + back - 1))
        assert w.tick(send=True).retry_scheduled == [] and calls(w) == n         # not due yet
    w.clock.set(t0 + timedelta(minutes=155))
    rep = w.tick(send=True)
    assert calls(w) == 4 and rep.failed and rep.retry_scheduled == []
    assert w.variant(pid, vid)["result"]["state"] == "failed" and len(w.alerts("rejected")) == 1
    assert [a["outcome"] for a in w.variant(pid, vid)["attempts"]] == ["failed"] * 4
    w.clock.set(t0 + timedelta(days=2))
    w.tick(send=True)
    assert calls(w) == 4 and len(sent(w)) == 0


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_a_network_retry_succeeds_and_posts_exactly_once(tmp_path):
    pub = ScriptedPublisher([("fail", "Temporary DNS failure"), "ok"])
    w, pid, vid = ready(tmp_path, pub=pub)
    assert w.tick(send=True).retry_scheduled and w.variant(pid, vid)["result"] is None
    w.clock.advance(minutes=5)
    assert len(w.tick(send=True).posted) == 1
    w.clock.advance(minutes=60)
    w.tick(send=True)
    assert calls(w) == 2 and len(sent(w)) == 1 and w.item(pid, vid)["status"] == "published"
    assert dispatch.open_dispatches(w.sp) == {}


def test_retries_never_run_past_the_late_window(tmp_path):
    pub = ScriptedPublisher([("fail", "Connection reset before any upload")])
    w, pid, vid = ready(tmp_path, pub=pub)                                   # window = slot + 120 min
    w.tick(send=True)                                                        # +0: retry +5
    w.clock.set("2026-10-21T15:05:30Z")
    w.tick(send=True)                                                        # retry +30 -> 15:35:30, inside the window
    w.clock.set("2026-10-21T15:36:00Z")
    rep = w.tick(send=True)                                                  # next backoff +120 min lands after 17:00: stop
    assert calls(w) == 3 and rep.failed and "late window" in w.alerts("rejected")[0]["message"]
    w.clock.set("2026-10-21T18:00:00Z")
    w.tick(send=True)
    assert calls(w) == 3


def test_auth_errors_are_not_retried_and_alert(tmp_path):
    pub = ScriptedPublisher([("fail", "HTTP 401 Unauthorized: invalid token"), "ok"])
    w, pid, vid = ready(tmp_path, pub=pub)
    rep = w.tick(send=True)
    assert rep.failed[0]["kind"] == "auth" and calls(w) == 1 and w.variant(pid, vid)["result"]["state"] == "failed"
    for _ in range(3):
        w.clock.advance(minutes=10)
        w.tick(send=True)
    assert calls(w) == 1 and len(w.alerts("auth_expired")) == 1 and w.alerts("auth_expired")[0]["severity"] == "error"


def test_validation_errors_are_not_retried_and_the_message_is_redacted(tmp_path):
    secret = "EAAGsupersecrettokenvalue1234567890"
    safety.register_secret(secret)
    pub = ScriptedPublisher([("fail", f"Caption violates platform policy (access_token={secret})"), "ok"])
    w, pid, vid = ready(tmp_path, pub=pub)
    rep = w.tick(send=True)
    assert rep.failed[0]["kind"] == "validation" and calls(w) == 1
    w.clock.advance(minutes=20)
    w.tick(send=True)
    assert calls(w) == 1
    for blob in (json.dumps(w.alerts()), json.dumps(w.variant(pid, vid)), (w.sp.ledger_dir / "events.jsonl").read_text(), json.dumps(w.item(pid, vid))):
        assert secret not in blob
    assert len(w.alerts("rejected")) == 1


def test_quota_answers_wait_for_the_reset_and_defer_the_whole_account(tmp_path):
    pub = ScriptedPublisher([("fail", "429 Too Many Requests; retry after 600"), "ok"])
    w, pid, vid = ready(tmp_path, pub=pub)
    rep = w.tick(send=True)
    assert rep.retry_scheduled[0]["kind"] == "quota" and rep.retry_scheduled[0]["retry_at"] == iso(utc(NOW) + timedelta(seconds=600))
    assert limits.load_state(w.sp, "ig-main")["deferred_until"] == iso(utc(NOW) + timedelta(seconds=600))
    assert len(w.alerts("quota")) == 1
    w.clock.advance(minutes=5)
    w.tick(send=True)
    assert calls(w) == 1
    w.clock.advance(minutes=6)
    assert len(w.tick(send=True).posted) == 1 and calls(w) == 2


def test_a_server_reported_reset_defers_other_variants_of_the_account(tmp_path):
    pub = ScriptedPublisher([("fail", "429 rate limit"), "ok"])
    w = make_world(tmp_path, limits={"max_per_day": 5, "min_gap_min": 0})
    w.pubs["ig-main"] = pub
    ids = [w.add_post(f"Another bread post {i}.", at=utc(SLOT) + timedelta(minutes=i)) for i in range(2)]
    for i in ids:
        w.approve(*i)
    w.go_live()
    w.clock.set("2026-10-21T15:10:00Z")
    rep = w.tick(send=True)
    assert calls(w) == 1 and rep.retry_scheduled and rep.deferred and "deferred after" in rep.deferred[0]["reason"]


def test_retry_classification_and_plan_unit():
    assert retry.classify("HTTP 401 Unauthorized") == "auth" and retry.classify("OAuthException: token expired") == "auth"
    assert retry.classify("429 slow down") == "quota" and retry.classify("daily quota exceeded") == "quota"
    assert retry.classify("connection reset") == "network" and retry.classify("timed out connecting") == "network"
    assert retry.classify("image too small") == "validation"
    now, slot = utc(NOW), utc(SLOT)
    p = retry.plan("connection reset", now=now, slot_utc=slot, late_after_min=120, prior_network_failures=0)
    assert p.retry_at == now + timedelta(minutes=5)
    assert retry.plan("connection reset", now=now, slot_utc=slot, late_after_min=120, prior_network_failures=3).retry_at is None
    assert retry.plan("401 unauthorized", now=now, slot_utc=slot, late_after_min=120, prior_network_failures=0).retry_at is None
    assert retry.plan("429 retry after 90", now=now, slot_utc=slot, late_after_min=120, prior_network_failures=0).retry_at == now + timedelta(seconds=90)


# ------------------------------------------------------------------------------------------------ dedupe window

def _dup_world(tmp, **kw):
    w = make_world(tmp, limits={"max_per_day": 5, "min_gap_min": 0}, **kw)
    first, v1 = w.add_post("Same words about bread.", at=SLOT)
    asset = store.load_post(w.sp, first)["media"][0]["asset"]
    post = w.studio.create_post(copy={"base": "Same  words about\nbread."}, media=[{"slot": 0, "asset": asset, "role": "image", "alt": "A loaf of bread on a table."}])
    post = w.studio.add_variant(post["id"], account="ig-main", platform="instagram", post_type="feed", expected_rev=post["rev"])
    second, v2 = post["id"], post["variants"][0]["id"]
    w.set_schedule(second, v2, utc("2026-10-21T16:00:00Z"))
    from ave.studio.sync import sync_queue
    sync_queue(w.sp, second, publishers=w.pubs, now=w.clock.now())
    w.approve(first, v1)
    w.approve(second, v2)
    w.go_live()
    return w, (first, v1), (second, v2)


def test_the_same_media_and_caption_is_not_posted_twice_within_the_dedupe_window(tmp_path):
    w, a, b = _dup_world(tmp_path)
    w.clock.set("2026-10-21T15:05:00Z")
    assert len(w.tick(send=True).posted) == 1
    w.clock.set("2026-10-21T16:05:00Z")
    rep = w.tick(send=True)
    assert rep.posted == [] and rep.blocked[0]["codes"] == ["duplicate"] and calls(w) == 1
    assert len(w.alerts("duplicate")) == 1 and a[0] in w.alerts("duplicate")[0]["message"]


def test_a_human_override_flag_lets_a_duplicate_through(tmp_path):
    w, a, b = _dup_world(tmp_path)
    w.clock.set("2026-10-21T15:05:00Z")
    w.tick(send=True)
    post = store.load_post(w.sp, b[0])
    post["variants"][0]["flags"] = ["allow_duplicate"]
    store.save_post(w.sp, post, expected_rev=post["rev"], actor=HUMAN, op="allow_duplicate")
    w.clock.set("2026-10-21T16:05:00Z")
    assert len(w.tick(send=True).posted) == 1 and calls(w) == 2


def test_the_dedupe_window_expires(tmp_path):
    w, a, b = _dup_world(tmp_path)
    _studio_setting(w, dedupe_days=1)
    w.clock.set("2026-10-21T15:05:00Z")
    w.tick(send=True)
    w.clock.set("2026-10-21T16:05:00Z")
    assert w.tick(send=True).blocked                                         # one hour later: still a duplicate
    w2, a2, b2 = _dup_world(tmp_path / "w2")
    _studio_setting(w2, dedupe_days=1)
    w2.clock.set("2026-10-21T15:05:00Z")
    w2.tick(send=True)
    assert dispatch.duplicate_of(w2.sp, store.load_post(w2.sp, b2[0]), w2.variant(*b2), w2.item(*b2), utc("2026-10-23T00:00:00Z")) is None


# ------------------------------------------------------------------------------------------------ properties over time

def test_a_single_post_goes_out_at_most_once_under_random_ticking(tmp_path_factory):
    rng = random.Random(11)
    for trial in range(6):
        behaviors = rng.choice([("ok",), ("crash_after_send",), ("uncertain", "ok"), ("refuse", "ok"), ("network", "ok")])
        w, pid, vid = ready(tmp_path_factory.mktemp(f"r{trial}"), behaviors)
        for _ in range(25):
            w.clock.advance(minutes=rng.choice([0, 1, 7, 30, 90, 600]))
            try:
                w.tick(send=rng.random() < 0.9)
            except KeyboardInterrupt:
                pass
        assert len(sent(w)) <= 1, f"{behaviors}: posted {len(sent(w))} times"
        assert calls(w) <= 3


def test_nothing_is_ever_sent_outside_the_slot_window_under_random_ticking(tmp_path_factory):
    rng = random.Random(5)
    for trial in range(4):
        w, pid, vid = ready(tmp_path_factory.mktemp(f"q{trial}"), now="2026-10-20T00:00:00Z")
        slot = utc(SLOT)
        for _ in range(30):
            w.clock.advance(minutes=rng.randint(1, 240))
            before = calls(w)
            w.tick(send=True)
            if calls(w) > before:
                assert slot <= w.clock.now() <= slot + timedelta(minutes=120), f"sent at {w.clock.now()}"


# ------------------------------------------------------------------------------------------------ attestation, heartbeat, daemon, hooks

def test_live_attestation_verification(tmp_path):
    w = make_world(tmp_path)
    assert dispatch.account_live(w.sp, "ig-main") is False and "no live attestation" in dispatch.live_problems(w.sp, "ig-main")[0]
    w.go_live()
    assert dispatch.account_live(w.sp, "ig-main") is True
    path = attest.live_path(w.sp.accounts_dir, "ig-main")
    good = json.loads(path.read_text())
    for patch, why in (({"by": "mallory"}, "signature"), ({"at": "2030-01-01T00:00:00Z"}, "signature"), ({"sig": "z" * 64}, "signature"),
                       ({"account": "other"}, "another account"), ({"live": False}, "signature")):
        path.write_text(json.dumps({**good, **patch}))
        assert dispatch.account_live(w.sp, "ig-main") is False and why in dispatch.live_problems(w.sp, "ig-main")[0]
    path.write_text("not json")
    assert dispatch.account_live(w.sp, "ig-main") is False
    path.write_text(json.dumps(good))
    assert dispatch.account_live(w.sp, "ig-main") is True
    # signatures are bound to the approval key: a different key (e.g. a stolen file on another machine) does not verify
    path.write_text(json.dumps({**good, "sig": attest.sign_live("ig-main", "maren", "2000-01-01")}))
    assert dispatch.account_live(w.sp, "ig-main") is False


def test_the_attestation_of_one_account_does_not_make_another_live(tmp_path):
    w = make_world(tmp_path, accounts=(("ig-main", "instagram"), ("ig-alt", "instagram")))
    w.go_live("ig-main")
    assert dispatch.account_live(w.sp, "ig-alt") is False
    attest.live_path(w.sp.accounts_dir, "ig-alt").write_text(attest.live_path(w.sp.accounts_dir, "ig-main").read_text())
    assert dispatch.account_live(w.sp, "ig-alt") is False


def test_heartbeat_is_written_by_sending_ticks_and_goes_stale(tmp_path):
    w, *_ = ready(tmp_path)
    assert check_heartbeat(w.sp, clock=w.clock, interval=60)["never_ran"] is True
    w.tick(send=True)
    assert check_heartbeat(w.sp, clock=w.clock, interval=60)["alive"] is True
    w.clock.advance(minutes=4)
    hb = check_heartbeat(w.sp, clock=w.clock, interval=60)
    assert hb["alive"] is False and hb["age_s"] == 240
    check_heartbeat(w.sp, clock=w.clock, interval=60)
    assert len(w.alerts("daemon_stale")) == 1


def test_the_daemon_loop_ticks_on_the_fake_clock_and_posts_once(tmp_path):
    w, pid, vid = ready(tmp_path)
    reports = []
    n = run_loop(w.sp, clock=w.clock, send=True, interval=60, publishers=w.pubs, max_ticks=4, on_report=reports.append)
    assert n == 4 and w.clock.slept == [60, 60, 60] and sum(len(r.posted) for r in reports) == 1 and calls(w) == 1
    stop = threading.Event()
    stop.set()
    assert run_loop(w.sp, clock=w.clock, send=True, publishers=w.pubs, stop=stop) == 0


def test_the_daemon_loop_stops_when_sending_is_forbidden(tmp_path, monkeypatch):
    w, *_ = ready(tmp_path)
    monkeypatch.setenv("CLAUDECODE", "1")
    with pytest.raises(TickRefused):
        run_loop(w.sp, clock=w.clock, send=True, publishers=w.pubs, max_ticks=3)


def test_rehearsal_through_the_facade_reports_the_would_post_list(tmp_path):
    w, pid, vid = ready(tmp_path)
    out = w.studio.tick(now="2026-10-21T15:00:30Z")
    assert out["mode"] == "dry-run" and out["now"] == NOW and [x["post"] for x in out["would_post"]] == [pid]
    assert w.studio.tick(now="2026-10-21T14:00:00Z")["would_post"] == []


def test_a_sent_variant_shows_as_published_and_locked_in_the_calendar(tmp_path):
    w, pid, vid = ready(tmp_path)
    w.tick(send=True)
    cal = w.studio.calendar("2026-10-21", "2026-10-21")
    card = cal["days"]["2026-10-21"][0]
    assert card["status"] == "published" and card["locked"] is True and cal["slots"]["2026-10-21"][0]["free"] is False
    with pytest.raises(slots.MoveRefused):
        w.studio.move(pid, vid, {"local": "2026-10-23T09:00", "tz": "America/Denver"}, expected_rev=store.load_post(w.sp, pid)["rev"], actor=HUMAN)


def test_credentials_never_reach_files_or_logs(tmp_path):
    secret = "IGQVJsupersecretlongtokenvalue98765"
    safety.register_secret(secret)
    pub = ScriptedPublisher([("fail", f"Invalid OAuth token {secret}")])
    w, pid, vid = ready(tmp_path, pub=pub)
    w.tick(send=True)
    blob = "".join(p.read_text(errors="ignore") for p in w.sp.root.rglob("*") if p.is_file() and p.suffix in (".json", ".jsonl", ".yaml"))
    assert secret not in blob


def test_resolved_feedback_stops_blocking_and_notes_do_not_block(tmp_path):
    w, pid, vid = ready(tmp_path)
    w.studio.add_feedback(pid, "note", note="nice", variant=vid)
    fb = w.studio.add_feedback(pid, "rework_copy", note="too salesy", variant=vid)
    w.tick(send=True)
    assert calls(w) == 0
    post = store.load_post(w.sp, pid)
    bl = dispatch.blockers(w.sp, post, w.variant(pid, vid), now=w.clock.now(), publishers=w.pubs, send=True)
    assert {b.code for b in bl} == {"approval", "open_feedback"} and "rework_copy" in [b for b in bl if b.code == "open_feedback"][0].message
    w.studio.resolve_feedback(fb["id"], "maren", note="fine as it is", human=True)           # the send-back voided the approval: a person closes the note ...
    assert calls(w) == 0
    w.approve(pid, vid)                                                                     # ... and approves again
    assert len(w.tick(send=True).posted) == 1
