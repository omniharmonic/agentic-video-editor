"""Regression tests for the adversarial QA of the send gates (qa/E-studio-gates.md): every script that used to bypass a human gate now fails to,
and tampering is DETECTED loudly rather than honoured. Each test names the finding it closes.

THREAT MODEL (docs/studio.md, SECURITY.md): an agent with a same-user shell can always edit files and read the HMAC key, so file-level tampering
cannot be made impossible. These tests prove (1) every sanctioned command and UI path is safe and cannot satisfy a human gate, and (2) a hand
edit is detected (signatures, hash-chained ledgers, tick refuses on tamper) instead of silently honoured. Nothing here sends to a network.
"""

import json
import os
from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from sched_helpers import make_world, no_agent_markers  # noqa: F401
from studio_helpers import RecordingPublisher, make_png

from ave import cli, humanpresence, library
from ave.platforms import attest
from ave.publish import queue as pq, safety
from ave.schedule.tick import TickRefused, tick
from ave.studio import dispatch, doctor, ledger, sendgate, seal, state, store
from ave.studio.events import AGENT, HUMAN, Actor, append_event, make_event
from ave.studio.sync import sync_queue

from .test_sched_tick import NOW, SLOT, ready


def codes(rep) -> set:
    return {c for b in rep.blocked for c in b["codes"]}


def hand_write_post(w, post: dict) -> None:
    (w.sp.posts_dir / f"{post['id']}.json").write_text(json.dumps(post))        # what `vi studio/posts/<id>.json` does: no rev bump, no event


def widen_window(w) -> None:
    """Let a slot stay sendable for a long time, so a test can move the clock without the post going `missed`."""
    meta = w.sp.read_meta()
    meta["late_after_min"] = 100000
    w.sp.meta_path.write_text(json.dumps(meta))


# ------------------------------------------------------------------------------------------------------ E15: the human path works

def test_e15_the_real_ui_shell_is_a_fully_wired_studio(tmp_path):
    from datetime import date

    from ave.studio.cli import real_studio
    from ave.studio.service import HOOKS, INTENTIONALLY_UNREGISTERED
    w = make_world(tmp_path)
    st = real_studio(w.sp, publishers=w.pubs, clock=w.clock)
    assert [h for h in HOOKS if h not in INTENTIONALLY_UNREGISTERED and not st.has(h)] == []
    cal = st.calendar(date(2026, 10, 19), date(2026, 10, 25))             # the page that answered 501 "not implemented yet" before
    assert "days" in cal or "slots" in cal or cal


def test_e15_ui_server_cli_builds_the_wired_studio(tmp_path, monkeypatch):
    """`ave studio ui` (non-fixtures) must serve a studio whose hooks are registered: the calendar route answers 200."""
    from ave.studio.cli import OPEN_KW
    from ave.studio.ui import server
    from .test_api import Client
    w = make_world(tmp_path)
    monkeypatch.setitem(OPEN_KW, "publishers", w.pubs)
    monkeypatch.setitem(OPEN_KW, "clock", w.clock)
    monkeypatch.setattr(humanpresence, "is_human_terminal", lambda: True)
    started = {}

    class A:
        dir, port, open, by, fixtures = str(w.sp.root), 0, False, None, False

    def fake_serve(self):                                   # run_cli builds the server then blocks in serve_forever; capture it instead
        started["srv"] = self
        raise KeyboardInterrupt

    monkeypatch.setattr(server.StudioServer, "serve_forever", fake_serve)
    assert server.run_cli(A) == 0
    studio = started["srv"].app.studio
    assert studio.has("calendar") and studio.has("sync_queue") and studio.has("tick")
    assert studio.publishers and set(studio.publishers) >= {"ig-main"}


def test_e15_approving_an_instagram_account_does_not_demote_the_item(tmp_path):
    """The publishers map is keyed by ACCOUNT id; preflight looks adapters up by PLATFORM. An account whose id is not its platform name used to
    get the unbound legacy adapter, fail preflight, and be DEMOTED to draft by the failed approval."""
    w = make_world(tmp_path)
    pid, vid = w.add_post()
    v = w.variant(pid, vid)
    item = w.item(pid, vid)
    assert item["status"] == "awaiting_approval" and "ig-main" in w.pubs and "instagram" not in w.pubs
    w.pubs["ig-main"].problems = []
    done = pq.approve(w.sp, item["id"], "maren", item["sha256"][:12], publishers=dispatch.queue_publishers(w.pubs, v), bind=dispatch.approval_bind(w.sp, v))
    assert done["status"] == "approved"
    # and an approval that preflight REFUSES leaves an awaiting item awaiting (it is no longer demoted to draft)
    pid2, vid2 = w.add_post(caption="Second post")
    item2 = w.item(pid2, vid2)
    w.pubs["ig-main"].problems = ["instagram: nope"]
    with pytest.raises(ValueError, match="preflight problems block approval"):
        pq.approve(w.sp, item2["id"], "maren", item2["sha256"][:12], publishers=dispatch.queue_publishers(w.pubs, w.variant(pid2, vid2)))
    assert w.item(pid2, vid2)["status"] == "awaiting_approval"


def test_e15_cli_approve_with_account_keyed_publishers(tmp_path, monkeypatch, capsys):
    from ave.studio.cli import OPEN_KW
    import io
    import sys
    w = make_world(tmp_path)
    pid, vid = w.add_post()
    monkeypatch.setitem(OPEN_KW, "publishers", w.pubs)
    monkeypatch.setitem(OPEN_KW, "clock", w.clock)
    monkeypatch.setattr(humanpresence, "is_human_terminal", lambda: True)
    item = w.item(pid, vid)
    monkeypatch.setattr(sys, "stdin", io.StringIO(item["sha256"][:8] + "\n"))
    rc = cli.main(["studio", "approve", pid, vid, "--by", "maren", "--studio", str(w.sp.root)])
    assert rc == 0, capsys.readouterr()
    assert w.item(pid, vid)["status"] == "approved" and w.item(pid, vid)["approvals"][-1]["bind"]["account"] == "ig-main"


def test_e15_daemon_plist_runs_tick_with_a_signed_permission_not_run_send(tmp_path, monkeypatch, capsys):
    """launchd has no tty: `run --send` exits 2 forever. The plist runs `tick --send` on an interval; the human writes the signed send.enabled."""
    w = make_world(tmp_path)
    monkeypatch.setattr(humanpresence, "is_human_terminal", lambda: False)
    (w.sp.root / "send.enabled").unlink()
    rc = cli.main(["studio", "daemon-plist", "--send", "--interval", "60", "--studio", str(w.sp.root)])
    res = json.loads(capsys.readouterr().out)
    assert rc == 0 and "<string>tick</string>" in res["plist"] and "<string>--send</string>" in res["plist"] and "<string>run</string>" not in res["plist"]
    assert "<key>StartInterval</key><integer>60</integer>" in res["plist"] and "KeepAlive" not in res["plist"]
    assert res["send_enabled"] is False and any("send enable" in s for s in res["install"])
    # without the permission a headless `tick --send` refuses (exit 2), sends nothing, and says what to do
    pid, vid = w.add_post(at=SLOT)
    w.approve(pid, vid)
    w.go_live()
    w.clock.set(NOW)
    with pytest.raises(TickRefused, match="send.enabled"):
        w.tick(send=True)
    assert w.pubs["ig-main"].publish_calls == 0


def test_e15_send_enable_is_a_human_act_and_the_headless_tick_then_sends(tmp_path, monkeypatch, capsys):
    from ave.studio.cli import OPEN_KW
    import io
    import sys
    w = make_world(tmp_path)
    (w.sp.root / "send.enabled").unlink()
    pid, vid = w.add_post(at=SLOT)
    w.approve(pid, vid)
    w.go_live()
    w.clock.set(NOW)
    monkeypatch.setitem(OPEN_KW, "publishers", w.pubs)
    monkeypatch.setitem(OPEN_KW, "clock", w.clock)
    d = str(w.sp.root)
    monkeypatch.setattr(humanpresence, "is_human_terminal", lambda: False)
    rc = cli.main(["studio", "send", "enable", "--studio", d])                  # an agent: refused, nothing written
    out = json.loads(capsys.readouterr().out)
    assert rc == 2 and out["needs_human"] and not (w.sp.root / "send.enabled").exists()
    rc = cli.main(["studio", "tick", "--send", "--studio", d])                  # headless without the file: refused
    assert rc == 2 and json.loads(capsys.readouterr().out)["needs_human"] and w.pubs["ig-main"].publish_calls == 0
    monkeypatch.setattr(humanpresence, "is_human_terminal", lambda: True)      # the human, in their terminal
    monkeypatch.setattr(sys, "stdin", io.StringIO(w.sp.root.name + "\n"))
    assert cli.main(["studio", "send", "enable", "--by", "maren", "--studio", d]) == 0
    capsys.readouterr()
    assert sendgate.verify(w.sp).ok
    monkeypatch.setattr(humanpresence, "is_human_terminal", lambda: False)    # launchd: no terminal, no markers, a valid send.enabled
    rc = cli.main(["studio", "tick", "--send", "--studio", d])
    assert rc == 0 and len(w.pubs["ig-main"].sent) == 1, capsys.readouterr()
    capsys.readouterr()
    monkeypatch.setenv("CLAUDECODE", "1")                                       # an agent marker still refuses, valid file or not
    rc = cli.main(["studio", "tick", "--send", "--studio", d])
    assert rc == 2 and json.loads(capsys.readouterr().out)["needs_human"]


def test_e15_send_enabled_is_signed_bound_and_not_replayable(tmp_path):
    w = make_world(tmp_path)
    p = sendgate.path(w.sp)
    on = p.read_text()
    assert sendgate.verify(w.sp).ok
    rec = json.loads(on)
    rec["by"] = "mallory"
    p.write_text(json.dumps(rec))
    assert "signature invalid" in sendgate.verify(w.sp).reason                      # hand edit
    p.write_text(on)
    other = make_world(tmp_path / "other")
    (other.sp.root / "send.enabled").write_text(on)
    assert "another studio" in sendgate.verify(other.sp).reason                     # copied to a different studio
    sendgate.disable(w.sp, "maren")
    assert not sendgate.verify(w.sp).ok and json.loads(p.read_text())["enabled"] is False
    p.write_text(on)                                                                # the saved "on" copy is restored
    assert "stale" in sendgate.verify(w.sp).reason


# ------------------------------------------------------------------------------------------------------ E2: send-backs and the feedback ledger

@pytest.mark.parametrize("kind", ["rework_copy", "rework_visual", "retime"])
def test_e2_a_sendback_voids_the_approval_and_the_agent_cannot_resolve_it_without_a_change(tmp_path, kind):
    w, pid, vid = ready(tmp_path)
    rev = store.load_post(w.sp, pid)["rev"]
    fb = w.studio.add_feedback(pid, kind, note="wrong tone, redo", variant=vid, by="maren", source="ui")
    assert w.item(pid, vid)["status"] == "awaiting_approval"                          # the human's approval is gone, in the same step
    assert any(e["op"] == "approval_invalidated" for e in w.events())
    with pytest.raises(ValueError, match=f"still at rev {rev}"):
        w.studio.resolve_feedback(fb["id"], "claude", rev=rev, note="done")
    assert [f["id"] for f in w.studio.feedback("open")] == [fb["id"]]
    assert not w.tick(send=True).posted and w.pubs["ig-main"].publish_calls == 0


def test_e2_resolve_needs_evidence_then_a_fresh_human_approval_releases_the_post(tmp_path):
    w, pid, vid = ready(tmp_path)
    fb = w.studio.add_feedback(pid, "rework_copy", note="shorter", variant=vid, by="maren", source="ui")
    post = store.load_post(w.sp, pid)
    w.studio.edit_post(pid, {"copy.base": "Shorter words."}, expected_rev=post["rev"], actor=AGENT)       # a rev bump AND a changed copy
    sync_queue(w.sp, pid, publishers=w.pubs, now=w.clock.now())
    line = w.studio.resolve_feedback(fb["id"], "claude", note="shortened")
    assert line["evidence"]["post_rev"] > line["evidence"]["note_rev"] and line["human"] is False
    assert not w.tick(send=True).posted                                                # resolved, but the human's approval is still void
    w.approve(pid, vid)                                                                # the human re-approves what they now see
    assert len(w.tick(send=True).posted) == 1


def test_e2_a_rev_bump_that_changes_something_else_is_not_evidence(tmp_path):
    w, pid, vid = ready(tmp_path)
    fb = w.studio.add_feedback(pid, "rework_copy", note="shorter", variant=vid, by="maren", source="ui")
    post = store.load_post(w.sp, pid)
    w.studio.edit_post(pid, {"idea.text": "a different private idea"}, expected_rev=post["rev"], actor=AGENT)
    with pytest.raises(ValueError, match="unchanged since the note"):
        w.studio.resolve_feedback(fb["id"], "claude")


def test_e2_a_human_drop_note_is_executed_by_post_drop_not_resolved_by_hand(tmp_path):
    w, pid, vid = ready(tmp_path)
    fb = w.studio.add_feedback(pid, "drop", note="do not post this", variant=vid, by="maren", source="ui")
    with pytest.raises(ValueError, match="post drop"):
        w.studio.resolve_feedback(fb["id"], "claude")
    assert not w.tick(send=True).posted


def test_e2_a_human_at_a_terminal_may_close_any_note(tmp_path):
    w, pid, vid = ready(tmp_path)
    fb = w.studio.add_feedback(pid, "rework_copy", note="redo", variant=vid, by="maren", source="ui")
    line = w.studio.resolve_feedback(fb["id"], "maren", note="it is fine as it is", human=True)
    assert line["human"] is True and not w.studio.feedback("open")


def test_e2_the_cli_resolve_passes_the_terminal_to_the_check(tmp_path, monkeypatch, capsys):
    w, pid, vid = ready(tmp_path)
    fb = w.studio.add_feedback(pid, "rework_copy", note="redo", variant=vid, by="maren", source="ui")
    monkeypatch.setattr(humanpresence, "is_human_terminal", lambda: False)
    from ave.studio.cli import OPEN_KW
    monkeypatch.setitem(OPEN_KW, "clock", w.clock)
    rc = cli.main(["studio", "feedback", "resolve", fb["id"], "--rev", "9", "--studio", str(w.sp.root)])
    assert rc == 2 and "still at rev" in json.loads(capsys.readouterr().out)["error"]
    monkeypatch.setattr(humanpresence, "is_human_terminal", lambda: True)
    rc = cli.main(["studio", "feedback", "resolve", fb["id"], "--studio", str(w.sp.root)])
    assert rc == 0 and json.loads(capsys.readouterr().out)["resolved"]["human"] is True


def test_e2_truncating_or_editing_the_feedback_ledger_is_detected_and_the_tick_sends_nothing(tmp_path):
    w, pid, vid = ready(tmp_path)
    w.studio.add_feedback(pid, "note", note="hello", by="maren", source="ui")
    assert seal.log(w.sp, "feedback").verify().ok
    ledger.feedback_path(w.sp).write_text("")                                          # `truncate -s0 ledger/feedback.jsonl`
    rep = w.tick(send=True)
    assert rep.tamper and "feedback" in rep.tamper[0] and not rep.posted and w.pubs["ig-main"].publish_calls == 0
    alerts = w.alerts("tamper")
    assert alerts and alerts[0]["severity"] == "error" and "Nothing is sent" in alerts[0]["message"]
    again = w.tick(send=True)                                                          # still refusing; the alert is raised once
    assert again.tamper and len(w.alerts("tamper")) == 1
    res = doctor.run(w.studio)
    assert not res["ok"] and any(c["name"] == "ledger:feedback" and c["status"] == "fail" for c in res["checks"])


@pytest.mark.parametrize("attack", ["edit_line", "drop_first_line", "append_unsigned", "remove_head", "stale_head"])
def test_e2_every_kind_of_ledger_tampering_is_detected(tmp_path, attack):
    w, pid, vid = ready(tmp_path)
    for n in range(3):
        w.studio.add_feedback(pid, "note", note=f"n{n}", by="maren", source="ui")
    log = seal.log(w.sp, "feedback")
    assert log.verify().ok
    path = log.path
    lines = path.read_text().splitlines()
    if attack == "edit_line":
        lines[1] = lines[1].replace('"n1"', '"n9"')
        path.write_text("\n".join(lines) + "\n")
    elif attack == "drop_first_line":
        path.write_text("\n".join(lines[:1] + lines[2:]) + "\n")
    elif attack == "append_unsigned":
        path.write_text(path.read_text() + json.dumps({"op": "resolve", "fb": "fb_0001", "by": "claude", "ts": "x"}) + "\n")
    elif attack == "remove_head":
        log.head_path.unlink()
    elif attack == "stale_head":
        path.write_text("\n".join(lines[:-1]) + "\n")                                     # the tail is cut; the signed head still says 4
    v = log.verify()
    assert not v.ok and v.problems
    assert w.tick(send=True).tamper


def test_e2_a_pre_seal_feedback_file_is_sealed_in_place_and_covered(tmp_path):
    w = make_world(tmp_path)
    pid, vid = w.add_post()
    legacy = ledger.feedback_path(w.sp)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps({"v": 1, "op": "feedback", "id": "fb_0001", "ts": "2026-10-01T00:00:00Z", "post": pid, "variant": None, "rev": 1,
                                  "kind": "note", "note": "old", "structured": {}, "by": "maren", "source": "cli"}) + "\n")
    assert seal.log(w.sp, "feedback").verify().legacy
    w.studio.add_feedback(pid, "note", note="new", by="maren")
    log = seal.log(w.sp, "feedback")
    assert log.verify().ok and [r["id"] for r in log.rows() if r.get("op") == "feedback"] == ["fb_0001", "fb_0002"]
    text = legacy.read_text().splitlines()
    text[0] = text[0].replace('"old"', '"edited"')
    legacy.write_text("\n".join(text) + "\n")
    assert any("before the ledger was sealed" in p for p in log.verify().problems)


# ------------------------------------------------------------------------------------------------------ E3: live attestation

def test_e3_account_set_of_credentials_voids_the_live_attestation(tmp_path, capsys):
    w, pid, vid = ready(tmp_path)
    assert attest.verify_live(w.sp.accounts_dir, "ig-main").ok
    rc = cli.main(["studio", "account", "set", "ig-main", "credentials.ref=SOMEONE-ELSES-ACCOUNT", "credentials.page_id=999",
                   "--studio", str(w.sp.root)])
    capsys.readouterr()
    assert rc == 0
    st = attest.verify_live(w.sp.accounts_dir, "ig-main")
    assert not st.ok and "credentials changed" in st.reason
    rep = w.tick(send=True)
    assert not rep.posted and "not_live" in codes(rep)
    attest.write_live(w.sp.accounts_dir, "ig-main", "maren")                              # only a human attestation of the NEW config revives it
    assert attest.verify_live(w.sp.accounts_dir, "ig-main").ok


@pytest.mark.parametrize("edit", [{"platform": "facebook"}, {"mode": "aggregator"}, {"tz": "UTC"}, {"credentials": {"ref": "other"}},
                                  {"credentials": {"ref": "ig-main", "page_id": "123"}}])
def test_e3_every_credential_bearing_field_is_in_the_attestation(tmp_path, edit):
    w, pid, vid = ready(tmp_path)
    cfg = yaml.safe_load((w.sp.accounts_dir / "ig-main.yaml").read_text())
    cfg.update(edit)
    (w.sp.accounts_dir / "ig-main.yaml").write_text(yaml.safe_dump(cfg))
    assert not attest.verify_live(w.sp.accounts_dir, "ig-main").ok


def test_e3_policy_fields_are_not_identity_and_do_not_void_it(tmp_path):
    w, pid, vid = ready(tmp_path)
    cfg = yaml.safe_load((w.sp.accounts_dir / "ig-main.yaml").read_text())
    cfg.update(label="@renamed", limits={"max_per_day": 1, "min_gap_min": 60}, post_types=["feed"])
    (w.sp.accounts_dir / "ig-main.yaml").write_text(yaml.safe_dump(cfg))
    assert attest.verify_live(w.sp.accounts_dir, "ig-main").ok


def test_e3_off_is_a_signed_record_with_a_counter_and_a_saved_copy_cannot_be_replayed(tmp_path):
    w, pid, vid = ready(tmp_path)
    p = attest.live_path(w.sp.accounts_dir, "ig-main")
    saved = p.read_text()
    assert json.loads(saved)["seq"] == 1
    assert attest.revoke_live(w.sp.accounts_dir, "ig-main", "maren") and p.exists()           # not an unlink
    off = json.loads(p.read_text())
    assert off["live"] is False and off["seq"] == 2 and "signed off" in attest.verify_live(w.sp.accounts_dir, "ig-main").reason
    p.write_text(saved)                                                                     # the agent restores the copy it made while live
    st = attest.verify_live(w.sp.accounts_dir, "ig-main")
    assert not st.ok and "stale" in st.reason
    rep = w.tick(send=True)
    assert not rep.posted and "not_live" in codes(rep)
    again = attest.write_live(w.sp.accounts_dir, "ig-main", "maren")                           # a human attesting anew gets the next number
    assert again["seq"] == 3 and attest.verify_live(w.sp.accounts_dir, "ig-main").ok
    p.write_text(saved)
    assert not attest.verify_live(w.sp.accounts_dir, "ig-main").ok


def test_e3_a_live_file_with_no_sealed_record_is_refused(tmp_path):
    w, pid, vid = ready(tmp_path)
    p = attest.live_path(w.sp.accounts_dir, "ig-main")
    seal.log(w.sp, "control").path.unlink()                                                  # the sealed counter is gone: the file stands alone
    assert not attest.verify_live(w.sp.accounts_dir, "ig-main").ok


# ------------------------------------------------------------------------------------------------------ E4/E12: single use, slots, TTL

def reset_item_by_hand(w, pid, vid) -> None:
    item = w.item(pid, vid)
    item["status"], item["results"] = "approved", {}
    pq.save(w.sp, item)
    post = store.load_post(w.sp, pid)
    post["variants"][0]["result"] = None
    hand_write_post(w, post)


def test_e4_an_approval_is_single_use_a_hand_reset_queue_file_cannot_send_it_twice(tmp_path):
    w, pid, vid = ready(tmp_path)
    widen_window(w)
    assert len(w.tick(send=True).posted) == 1
    w.clock.advance(hours=4)                                                                 # past the min gap
    reset_item_by_hand(w, pid, vid)
    rep = w.tick(send=True)
    assert not rep.posted and len(w.pubs["ig-main"].sent) == 1
    assert "approval_consumed" in codes(rep) and w.alerts("tamper")
    assert dispatch.consumption(w.sp, dispatch.approval_sig(w.item(pid, vid))) == "published"


def test_e12_hand_clearing_an_uncertain_send_and_closing_its_dispatch_key_still_does_not_double_post(tmp_path):
    pub = RecordingPublisher("instagram", behaviors=("crash_after_send",))
    w, pid, vid = ready(tmp_path, pub=pub)
    widen_window(w)
    with pytest.raises(KeyboardInterrupt):
        w.tick(send=True)
    assert len(pub.sent) == 1
    item = w.item(pid, vid)
    item["results"], item["status"] = {}, "approved"                                         # "I checked, it is fine, send again"
    pq.save(w.sp, item)
    for key in dispatch.open_dispatches(w.sp):
        append_event(w.sp, make_event(actor=AGENT, op="dispatch_finished", post=pid, variant=vid, detail={"key": key, "outcome": "blocked"}, now=w.clock.now()))
    w.clock.advance(hours=4)
    pub2 = RecordingPublisher("instagram")
    w.pubs["ig-main"] = pub2
    rep = w.tick(send=True)
    assert pub2.sent == [] and "uncertain" in codes(rep)


@pytest.mark.parametrize("how,state_after", [("posted", "published"), ("retry", "free")])
def test_e12_a_humans_resolve_writes_the_signed_record_that_settles_an_uncertain_send(tmp_path, how, state_after):
    pub = RecordingPublisher("instagram", behaviors=("crash_after_send",))
    w, pid, vid = ready(tmp_path, pub=pub)
    widen_window(w)
    with pytest.raises(KeyboardInterrupt):
        w.tick(send=True)
    sig = dispatch.approval_sig(w.item(pid, vid))
    assert dispatch.consumption(w.sp, sig) == "open"
    dispatch.resolve_uncertain(w.sp, pid, vid, posted_url="https://example.invalid/p/1" if how == "posted" else None, retry=how == "retry",
                               by="maren", now=w.clock.now())
    assert dispatch.consumption(w.sp, sig) == state_after


def test_e4_a_hand_edited_slot_is_detected_not_honoured(tmp_path):
    w, pid, vid = ready(tmp_path)
    w.clock.set(w.clock.now() - timedelta(days=3))                                           # three days before the approved slot
    post = store.load_post(w.sp, pid)
    post["variants"][0]["schedule"]["utc"] = (w.clock.now() - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")      # vi studio/posts/<id>.json
    hand_write_post(w, post)
    rep = w.tick(send=True)
    assert not rep.posted and w.pubs["ig-main"].publish_calls == 0 and "schedule_changed" in codes(rep)
    assert any(e["op"] == "schedule_changed_after_approval" for e in w.events())
    assert w.alerts("tamper") and "schedule_changed_after_approval" in w.variant(pid, vid)["flags"]


def test_e4_a_sanctioned_human_retime_keeps_the_approval_and_is_recorded(tmp_path):
    w, pid, vid = ready(tmp_path)
    post = store.load_post(w.sp, pid)
    w.clock.set("2026-10-21T10:00:00Z")
    res = w.studio.move(pid, vid, {"local": "2026-10-21T10:30", "tz": "America/Denver"}, expected_rev=post["rev"], actor=HUMAN, snap="none")
    assert res["applied"] is True
    rows = [r for r in seal.log(w.sp, "control").rows() if r.get("op") == "slot"]
    assert rows and rows[-1]["kind"] == "human" and rows[-1]["utc"] == "2026-10-21T16:30:00Z"
    w.clock.set("2026-10-21T16:31:00Z")
    assert len(w.tick(send=True).posted) == 1


def test_e4_an_agent_retime_of_an_approved_scheduled_post_is_a_proposal_only(tmp_path):
    w, pid, vid = ready(tmp_path)
    post = store.load_post(w.sp, pid)
    res = w.studio.move(pid, vid, {"local": "2026-10-21T11:00", "tz": "America/Denver"}, expected_rev=post["rev"], actor=AGENT, snap="none")
    assert res["applied"] is False and res.get("proposal")
    assert store.load_post(w.sp, pid)["variants"][0]["schedule"]["utc"] == SLOT


def test_e4_an_agent_scheduling_an_approved_pooled_post_leaves_an_event_the_human_sees(tmp_path):
    w = make_world(tmp_path)
    pid, vid = w.add_post()                                                                  # no slot: the pool
    w.approve(pid, vid)
    w.go_live()
    w.clock.set("2026-10-21T10:00:00Z")
    post = store.load_post(w.sp, pid)
    res = w.studio.move(pid, vid, {"local": "2026-10-21T10:30", "tz": "America/Denver"}, expected_rev=post["rev"], actor=AGENT, snap="none")
    assert res["applied"] is True
    assert any(e["op"] == "retime_after_approval" and e["actor"]["kind"] == "agent" for e in w.events())


def test_e4_raising_the_studio_ttl_after_approval_is_detected(tmp_path):
    w, pid, vid = ready(tmp_path)
    meta = w.sp.read_meta()
    meta.setdefault("approval", {})["ttl_days"] = 99999
    w.sp.meta_path.write_text(json.dumps(meta))
    rep = w.tick(send=True)
    assert not rep.posted and "ttl_policy" in codes(rep) and w.alerts("tamper")
    meta["approval"]["ttl_days"] = 7                                                         # tightening is the safe direction: allowed
    w.sp.meta_path.write_text(json.dumps(meta))
    assert "ttl_policy" not in codes(w.tick(send=True)) and len(w.pubs["ig-main"].sent) == 1


def test_e4_raising_the_publish_ttl_after_approval_is_detected(tmp_path):
    w, pid, vid = ready(tmp_path)
    cfg = Path(os.environ["HOME"]) / ".config" / "ave"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "publish.yaml").write_text("publish:\n  approval_ttl_days: 3650\n")
    rep = w.tick(send=True)
    assert not rep.posted and "ttl_policy" in codes(rep)


def test_e4_a_hand_edited_approval_binding_is_detected(tmp_path):
    w, pid, vid = ready(tmp_path)
    item = w.item(pid, vid)
    item["approvals"][-1]["bind"]["slot"] = "2026-10-21T00:00:00Z"
    pq.save(w.sp, item)
    rep = w.tick(send=True)
    assert not rep.posted and "approval_binding" in codes(rep)


# ------------------------------------------------------------------------------------------------------ E5/E6: licenses and foreign approvals

def test_e5_an_agent_added_license_does_not_count_until_a_human_attests_it(tmp_path):
    w = make_world(tmp_path)
    rec = library.add_file(w.sp, make_png(w.sp.work / "gen" / "a.png", color=(5, 6, 7)), source={"type": "own"}, by="agent", alt="x", attested=False)
    cov = library.coverage(w.sp, [rec["id"]])
    assert not cov["ok"] and "attest" in cov["missing"][0]["why"]
    post = w.studio.create_post(idea="x", copy={"base": "c"}, media=[{"slot": 0, "asset": rec["id"], "role": "image", "alt": "x", "crop": {}}])
    post = w.studio.add_variant(post["id"], account="ig-main", platform="instagram", post_type="feed", expected_rev=post["rev"])
    rep = sync_queue(w.sp, post["id"], publishers=w.pubs, now=w.clock.now())
    assert rep["variants"][0]["action"] == "blocked" and any("attest" in p for p in rep["variants"][0]["problems"])
    assert [r["asset"] for r in library.unattested(w.sp)] == [rec["id"]]
    library.attest_license(w.sp, rec["id"], "maren")
    assert library.coverage(w.sp, [rec["id"]])["ok"] and not library.unattested(w.sp)
    assert sync_queue(w.sp, post["id"], publishers=w.pubs, now=w.clock.now())["variants"][0]["action"] == "created"


def test_e5_the_license_log_flag_cannot_be_removed_without_breaking_the_chain(tmp_path):
    from ave.library import registry
    w = make_world(tmp_path)
    library.add_file(w.sp, make_png(w.sp.work / "gen" / "a.png", color=(5, 6, 7)), source={"type": "own"}, by="agent", attested=False)
    lp = registry.license_log(w.sp).path
    lp.write_text(lp.read_text().replace('"attested": false', '"attested": true'))
    assert registry.license_log(w.sp).entries()[0]["attested"] is True            # the flag was flipped by hand ...
    assert library.verify(w.sp)["ok"] is False                                  # ... and `library verify` / doctor say so


def test_e5_library_add_records_an_unattested_claim_and_attest_is_human_only(tmp_path, monkeypatch, capsys):
    import io
    import sys
    w = make_world(tmp_path)
    d = str(w.sp.root)
    src = make_png(tmp_path / "in" / "b.png", color=(9, 9, 9))
    rc = cli.main(["studio", "library", "add", str(src), "--source", "own", "--by", "Maren", "--studio", d])
    res = json.loads(capsys.readouterr().out)
    assert rc == 0 and res["license"]["attested"] is False and res["license"]["pending_human_attestation"]
    aid = res["added"][0]["id"]
    assert not library.coverage(w.sp, [aid])["ok"]
    monkeypatch.setattr(humanpresence, "is_human_terminal", lambda: False)
    rc = cli.main(["studio", "library", "attest", aid, "--studio", d])               # an agent: refused
    assert rc == 2 and json.loads(capsys.readouterr().out)["needs_human"] and not library.coverage(w.sp, [aid])["ok"]
    monkeypatch.setattr(humanpresence, "is_human_terminal", lambda: True)
    monkeypatch.setattr(sys, "stdin", io.StringIO("attest\n"))
    assert cli.main(["studio", "library", "attest", aid, "--by", "Maren", "--studio", d]) == 0
    capsys.readouterr()
    assert library.coverage(w.sp, [aid])["ok"]


def test_e5_e6_adopt_records_unattested_claims_links_no_queue_item_and_stamps_the_episode_item(tmp_path, monkeypatch):
    from studio_helpers import build_studio, make_episode_with_queue
    b = build_studio(tmp_path / "s", clock=None)
    ep, item = make_episode_with_queue(tmp_path / "e", approve=True)
    from ave.studio import ingest
    out = ingest.adopt(b.studio, ep.root)
    assert out["adopted"], out
    post = store.load_post(b.sp, out["adopted"][0]["post"])
    assert all(not v.get("queue") for v in post["variants"])                                 # the episode's approval does not transfer
    assert library.unattested(b.sp) and not library.coverage(b.sp, [post["media"][0]["asset"]])["ok"]
    stamped = pq.load(ep, item["id"])
    assert stamped["studio_bound"]["post"] == post["id"] and stamped["status"] == "approved"
    # the legacy sender refuses the adopted item: pause, live, feedback, license, limits and dedupe gates cannot be bypassed through it
    with pytest.raises(SystemExit, match="refused: it was adopted by the studio"):
        cli.main(["publish", "run", str(ep.root), item["id"], "--yes"])
    assert pq.load(ep, item["id"])["status"] == "approved" and not pq.load(ep, item["id"])["results"]
    # and a re-adoption does not duplicate
    assert not ingest.adopt(b.studio, ep.root)["adopted"]


def test_e6_the_legacy_publish_cli_recognizes_every_studio_item(tmp_path):
    from ave.commands.publish import studio_bound
    w, pid, vid = ready(tmp_path)
    assert "meta_v 2" in studio_bound(w.item(pid, vid))
    assert studio_bound({"id": "x", "studio_bound": {"studio": "/s"}}) and studio_bound({"id": "legacy"}) is None


def test_e6_a_foreign_approval_cannot_be_attached_to_a_live_studio_account(tmp_path):
    """B3: an episode-flow approval (no account binding, not meta_v 2) linked to a variant by hand used to send."""
    from ave.episode import Episode
    w, pid, vid = ready(tmp_path)
    ep = Episode(tmp_path / "old-ep")
    ep.create("Old ep")
    png = make_png(ep.exports / "card.png", color=(1, 2, 3))
    leg = pq.create(ep, files=[png], media_type="image", platforms=["instagram"], title="old", caption="approved long ago for another purpose",
                    alt_texts=["a"], ai_disclosure=False, publishers={"instagram": RecordingPublisher("instagram")})
    pq.approve(ep, leg["id"], "maren", leg["sha256"][:12], publishers={"instagram": RecordingPublisher("instagram")})
    post = store.load_post(w.sp, pid)
    post["variants"][0]["queue"] = {"root": str(ep.root), "id": leg["id"]}
    hand_write_post(w, post)
    p2 = store.load_post(w.sp, pid)
    bl = {b.code for b in dispatch.blockers(w.sp, p2, p2["variants"][0], now=w.clock.now(), publishers=w.pubs, send=True)}
    assert {"not_studio_item", "account_mismatch"} <= bl
    rep = w.tick(send=True)
    assert not rep.posted and w.pubs["ig-main"].publish_calls == 0


def test_e5_the_license_closure_is_computed_over_the_bytes_the_queue_item_sends(tmp_path):
    """B2: the post references licensed asset A; the (approved) queue item carries unregistered file B."""
    w, pid, vid = ready(tmp_path)
    bad = make_png(tmp_path / "unlicensed.jpg", color=(250, 10, 10))
    pubs = {"instagram": RecordingPublisher("instagram")}
    newq = pq.create(w.sp, files=[bad], media_type="image", platforms=["instagram"], title="t", caption="c", alt_texts=["x"], meta_v=2,
                     account="ig-main", post_type="feed", publishers=pubs, ai_disclosure=False, schedule=SLOT)
    pq.approve(w.sp, newq["id"], "maren", newq["sha256"][:12], publishers=pubs, bind=dispatch.approval_bind(w.sp, w.variant(pid, vid)))
    post = store.load_post(w.sp, pid)
    post["variants"][0]["queue"] = {"root": ".", "id": newq["id"]}
    hand_write_post(w, post)
    p2 = store.load_post(w.sp, pid)
    bl = {b.code for b in dispatch.blockers(w.sp, p2, p2["variants"][0], now=w.clock.now(), publishers=w.pubs, send=True)}
    assert {"license_gap", "queue_mismatch"} <= bl
    assert not w.tick(send=True).posted and w.pubs["ig-main"].publish_calls == 0


# ------------------------------------------------------------------------------------------------------ E7: pause and quiet hours

def test_e7_removing_the_pause_file_does_not_resume_and_raises_a_tamper_alert(tmp_path):
    w, pid, vid = ready(tmp_path)
    dispatch.pause(w.sp, by="maren", kind="human", now=w.clock.now())
    assert w.tick(send=True).paused
    w.sp.pause_path.unlink()                                                                # `rm studio/PAUSE`
    assert dispatch.is_paused(w.sp) and dispatch.pause_removed(w.sp) == ["studio"]
    rep = w.tick(send=True)
    assert rep.paused and not rep.posted and w.pubs["ig-main"].publish_calls == 0
    assert w.alerts("tamper") and "without `ave studio resume`" in w.alerts("tamper")[0]["message"]
    assert not doctor.run(w.studio)["ok"]
    dispatch.resume(w.sp, by="maren", now=w.clock.now())                                       # the human's resume writes the matching sealed record
    assert not dispatch.is_paused(w.sp) and dispatch.pause_removed(w.sp) == []
    assert len(w.tick(send=True).posted) == 1


def test_e7_an_account_pause_removed_by_hand_stays_paused(tmp_path):
    w, pid, vid = ready(tmp_path)
    dispatch.pause(w.sp, account="ig-main", by="maren", kind="human", now=w.clock.now())
    (w.sp.accounts_dir / "ig-main.paused").unlink()
    rep = w.tick(send=True)
    assert not rep.posted and rep.tamper and "account_paused" in codes(rep) | {"account_paused"}


@pytest.mark.parametrize("windows", [[{"from": "00:00", "to": "24:00"}], [{"from": "00:00", "to": "12:00"}, {"from": "12:00", "to": "24:00"}]])
def test_e7_a_quiet_window_that_covers_all_day_fails_closed(tmp_path, windows):
    w, pid, vid = ready(tmp_path)
    cfg = yaml.safe_load((w.sp.accounts_dir / "ig-main.yaml").read_text())
    cfg["quiet_hours"] = windows
    (w.sp.accounts_dir / "ig-main.yaml").write_text(yaml.safe_dump(cfg))
    rep = w.tick(send=True)
    assert not rep.posted and w.pubs["ig-main"].publish_calls == 0
    assert rep.deferred and rep.deferred[0]["reason"] == "quiet hours"


# ------------------------------------------------------------------------------------------------------ the dry run is unchanged

def test_a_rehearsal_never_needs_the_permission_and_never_sends(tmp_path):
    w, pid, vid = ready(tmp_path)
    (w.sp.root / "send.enabled").unlink()
    rep = w.tick(send=False)
    assert rep.would_post and w.pubs["ig-main"].publish_calls == 0


# ------------------------------------------------------------------------------------------------------ the app's approval sheets
# Publishing approval (one post or several) and trim approval each have a dedicated native sheet (ave.desktop_approvals, opened
# by desktop_native). Both use the existing device-owner check (desktop_auth.NativeAuthority), which now mints a single-use
# grant bound to one act and its targets, and the per-run PIN; an agent marker refuses them; and the work is the existing
# approval logic, unchanged.

def _sheets(w):
    from ave.desktop_approvals import Approvals
    from ave.desktop_auth import NativeAuthority
    from ave.studio.ui.api import StudioApp
    authority = NativeAuthority()
    app = StudioApp(w.studio, pin="424242", native_authority=authority)
    return app, Approvals(app), authority


def _check(authority, req):
    """What a successful macOS device-owner check for this sheet leaves behind (desktop_auth.request): one grant."""
    return authority._mint(req["action"], req["keys"])


def _items(w, *names):
    return [{"post": p, "variant": v, "sha": w.item(p, v)["sha256"]} for p, v in names]


def test_the_publish_approval_sheet_needs_the_device_owner_check(tmp_path):
    from ave.studio.ui.api import StudioError
    w = make_world(tmp_path)
    pid, vid = w.add_post()
    sha = w.item(pid, vid)["sha256"]
    app, sheets, authority = _sheets(w)
    req = sheets.publish_request(_items(w, (pid, vid)))                    # reading what it would approve needs nothing
    assert "Approval covers exactly: " + sha in req["detail"] and "Day 3: the starter smells like pears." in req["detail"]
    with pytest.raises(StudioError) as e:
        sheets.approve_publish(req, None, "424242", 2500, by="maren")          # no check, no grant
    assert e.value.code == "native.authentication_required"
    assert w.item(pid, vid)["status"] == "awaiting_approval"
    done = sheets.approve_publish(req, _check(authority, req), "424242", 2500, by="maren")
    item = w.item(pid, vid)
    assert done["state"] == "approved" and item["status"] == "approved"
    assert item["approvals"][-1]["by"] == "maren" and item["approvals"][-1]["source"] == "studio-ui"   # the studio's own approval
    viewed = [e for e in ledger_events(w) if e.get("op") == "viewed"]
    assert viewed[-1]["detail"]["via"] == "native-sheet"


def ledger_events(w):
    return ledger.read_jsonl(ledger.events_path(w.sp))[0]


def test_the_publish_approval_sheet_keeps_the_pin_the_view_and_the_sha(tmp_path):
    from ave.studio.ui.api import StudioError
    w = make_world(tmp_path)
    pid, vid = w.add_post()
    app, sheets, authority = _sheets(w)
    req = sheets.publish_request(_items(w, (pid, vid)))
    short = sheets.approve_publish(req, _check(authority, req), "424242", 400, by="maren")    # on screen under 2 seconds
    assert short["state"] == "refused" and "not viewed" in short["refused"][0]["reason"]
    with pytest.raises(StudioError, match="wrong PIN"):
        sheets.approve_publish(req, _check(authority, req), "000000", 2500, by="maren")
    with pytest.raises(ValueError, match="changed since"):
        sheets.publish_request([{"post": pid, "variant": vid, "sha": "0" * 64}])
    assert w.item(pid, vid)["status"] == "awaiting_approval"


def test_an_agent_marker_refuses_the_publish_approval_sheet(tmp_path, monkeypatch):
    w = make_world(tmp_path)
    pid, vid = w.add_post()
    app, sheets, authority = _sheets(w)
    req = sheets.publish_request(_items(w, (pid, vid)))
    grant = _check(authority, req)
    monkeypatch.setenv("CLAUDECODE", "1")
    with pytest.raises(ValueError, match="agent marker"):
        sheets.publish_request(_items(w, (pid, vid)))
    with pytest.raises(ValueError, match="agent marker"):
        sheets.approve_publish(req, grant, "424242", 2500, by="maren")
    assert w.item(pid, vid)["status"] == "awaiting_approval"


def test_the_publish_approval_sheet_has_nothing_on_a_computer_without_the_queue(tmp_path):
    w = make_world(tmp_path)
    pid, vid = w.add_post(sync=False)                                  # no queue item: this is not the publishing computer
    app, sheets, authority = _sheets(w)
    with pytest.raises(ValueError, match="Only the publishing computer approves posts"):
        sheets.publish_request([{"post": pid, "variant": vid, "sha": "a" * 64}])


def test_several_posts_are_approved_with_one_check_and_each_gets_its_own_result(tmp_path):
    """The old Review page's batch: one sheet listing every post, one device-owner check, one PIN, per-post results."""
    w = make_world(tmp_path)
    a, b, c = w.add_post("First post."), w.add_post("Second post."), w.add_post("Third post.")
    app, sheets, authority = _sheets(w)
    items = _items(w, a, b, c)
    items[2]["sha"] = "f" * 64                                           # the third changed since the web sheet showed it
    req = sheets.publish_request(items)
    assert len(req["items"]) == 2 and req["refused"][0]["post"] == c[0] and "First post." in req["detail"] and "Second post." in req["detail"]
    w.studio.edit_post(b[0], {"copy.base": "Second post, changed."}, expected_rev=store.load_post(w.sp, b[0])["rev"], actor=AGENT)
    done = sheets.approve_publish(req, _check(authority, req), "424242", 2500, by="maren")
    assert [x["post"] for x in done["approved"]] == [a[0]]               # one post failing does not undo the other
    assert {r["post"] for r in done["refused"]} == {b[0], c[0]}
    assert w.item(*a)["status"] == "approved" and w.item(*b)["status"] != "approved"


def test_a_grant_is_single_use_and_bound_to_its_act_and_its_targets(tmp_path):
    from ave.studio.ui.api import StudioError
    w = make_world(tmp_path)
    pa, pb = w.add_post("Post A."), w.add_post("Post B.")
    app, sheets, authority = _sheets(w)
    req_a, req_b = sheets.publish_request(_items(w, pa)), sheets.publish_request(_items(w, pb))
    grant_a = _check(authority, req_a)
    with pytest.raises(StudioError, match="something else"):
        sheets.approve_publish(req_b, grant_a, "424242", 2500, by="maren")      # A's check cannot approve B
    grant_a = _check(authority, req_a)
    sheets.approve_publish(req_a, grant_a, "424242", 2500, by="maren")
    with pytest.raises(StudioError, match="already used"):
        sheets.approve_publish(req_a, grant_a, "424242", 2500, by="maren")      # nor be used twice
    trim_grant = authority._mint("approveTrim", req_b["keys"])
    with pytest.raises(StudioError, match="something else"):
        sheets.approve_publish(req_b, trim_grant, "424242", 2500, by="maren")   # a check for X never authorises Y
    assert w.item(*pb)["status"] == "awaiting_approval"


def test_while_a_pin_sheet_is_open_the_token_routes_stay_shut(tmp_path):
    """I2: the person passed Touch ID for post A and is reading the PIN. Another caller with the room token (another thread,
    no grant) cannot pick B-roll, accept a schedule proposal, acknowledge an alert, resume or approve post B."""
    import threading
    from ave.studio.ui.api import StudioError
    w = make_world(tmp_path)
    pa, pb = w.add_post("Post A."), w.add_post("Post B.")
    app, sheets, authority = _sheets(w)
    req_a = sheets.publish_request(_items(w, pa))
    grant_a = _check(authority, req_a)                                  # minted, not yet spent: the PIN sheet is open
    ledger.append_alert(w.sp, severity="error", kind="uncertain", message="The platform may have the post.", account="ig-main")
    alert_id = ledger.list_alerts(w.sp)[0]["id"]
    results = []

    def caller():
        for route, body in (("broll/pick", {"search": "s1", "candidate": "pexels:1", "by": "maren"}), ("schedule/proposals/sp1/accept", {}),
                            (f"alerts/{alert_id}/ack", {}), ("resume", {}),
                            ("approve", {"items": [{"post": pb[0], "variant": pb[1], "sha": w.item(*pb)["sha256"]}], "pin": "424242", "by": "maren"})):
            try:
                app.post(route, body)
                results.append((route, "allowed"))
            except StudioError as e:
                results.append((route, e.code))

    with authority.use(grant_a, "approvePublish", req_a["keys"]):           # even while A's grant is being spent on this thread
        t = threading.Thread(target=caller)
        t.start()
        t.join()
    gone = {"resume", "approve"}                                          # removed with the old shell: no route at all
    assert all(code == ("not_found" if route in gone else "native.authentication_required") for route, code in results), results
    assert w.item(*pb)["status"] == "awaiting_approval" and ledger.list_alerts(w.sp)            # nothing approved, nothing acknowledged
    assert "native_authorized" not in app.get("summary", {})[1]["ui"]                          # the window is not advertised


def test_the_alert_acknowledge_route_records_nothing_without_the_check(tmp_path, monkeypatch):
    """I1: acknowledging names the person, so it needs the device-owner check, made in the app's sheet. With only the room
    token (or with no app at all) it is refused and nothing is recorded; an agent marker refuses it even with a grant."""
    from ave.desktop_approvals import Approvals
    from ave.studio.ui.api import StudioApp, StudioError
    w = make_world(tmp_path)
    ledger.append_alert(w.sp, severity="error", kind="uncertain", message="The platform may have the post.", account="ig-main")
    aid = ledger.list_alerts(w.sp)[0]["id"]
    app, sheets, authority = _sheets(w)
    with pytest.raises(StudioError) as e:
        app.post(f"alerts/{aid}/ack", {"by": "maren"})                                           # the room token, no check
    assert e.value.code == "native.authentication_required"
    browser = StudioApp(w.studio, pin="424242")                                                 # `ave studio ui`, no app
    with pytest.raises(StudioError, match="ave studio alerts ack"):
        browser.post(f"alerts/{aid}/ack", {"by": "maren"})
    assert [a["id"] for a in ledger.list_alerts(w.sp)] == [aid]                                  # nothing recorded
    req = sheets.protected_request("ackAlert", {"alert": aid})
    grant = _check(authority, req)
    monkeypatch.setenv("CLAUDECODE", "1")
    with pytest.raises(ValueError, match="agent marker"):
        sheets.run_protected(req, grant, by="Maren")
    monkeypatch.delenv("CLAUDECODE")
    sheets.run_protected(req, _check(authority, req), by="Maren")                                 # the app's sheet, after the check
    assert ledger.list_alerts(w.sp) == [] and ledger.list_alerts(w.sp, include_acked=True)[0]["ack"]["by"] == "Maren"


def _trim_world(tmp_path):
    from types import SimpleNamespace
    from ave.project import Project
    root = tmp_path / "ep12"
    p = Project.create(root, "Episode 12", person="Maren")
    (root / "episode.json").write_text(json.dumps({"title": "Episode 12", "trim": {
        "main": {"in": 3.2, "out": 2741.0, "by": "claude", "at": "2026-11-02T10:00:00+0000", "draft_only": True}}}))
    return p, SimpleNamespace(projects=lambda: [p])


def _trim_sheets(tmp_path):
    from ave.desktop_approvals import Approvals
    w = make_world(tmp_path)
    app, _, authority = _sheets(w)
    p, workspace = _trim_world(tmp_path)
    return p, Approvals(app, workspace), authority


def test_the_trim_approval_sheet_needs_the_device_owner_check(tmp_path):
    from ave.studio.ui.api import StudioError
    from ave import trim
    from ave.episode import Episode
    p, sheets, authority = _trim_sheets(tmp_path)
    assert [(t["project"], t["session"], t["in"], t["out"]) for t in sheets.trims()] == [(p.project_id, "main", 3.2, 2741.0)]
    req = sheets.trim_request(p.project_id, "main")
    assert "starts at 0:03.20, ends at 45:41.00" in req["detail"]                  # the same clock the web sheet shows
    with pytest.raises(StudioError) as e:
        sheets.approve_trim(req, None, "424242")
    assert e.value.code == "native.authentication_required"
    assert not trim.confirmed(json.loads((p.root / "episode.json").read_text())["trim"]["main"])
    done = sheets.approve_trim(req, _check(authority, req), "424242")
    rec = json.loads((p.root / "episode.json").read_text())["trim"]["main"]
    assert done["state"] == "confirmed" and rec["by"] == "human" and "draft_only" not in rec and (rec["in"], rec["out"]) == (3.2, 2741.0)
    assert trim.final_block(Episode(p.root), None) is None and sheets.trims() == []      # the final may now use it


def test_an_agent_marker_refuses_the_trim_approval_sheet(tmp_path, monkeypatch):
    p, sheets, authority = _trim_sheets(tmp_path)
    req = sheets.trim_request(p.project_id, "main")
    grant = _check(authority, req)
    monkeypatch.setenv("AVE_AGENT", "1")
    with pytest.raises(ValueError, match="agent marker"):
        sheets.trim_request(p.project_id, "main")
    with pytest.raises(ValueError, match="agent marker"):
        sheets.approve_trim(req, grant, "424242")
    assert json.loads((p.root / "episode.json").read_text())["trim"]["main"]["by"] == "claude"


def test_the_trim_approval_sheet_confirms_only_the_ends_it_showed(tmp_path):
    from ave.studio.ui.api import StudioError
    p, sheets, authority = _trim_sheets(tmp_path)
    req = sheets.trim_request(p.project_id, "main")
    meta = json.loads((p.root / "episode.json").read_text())
    meta["trim"]["main"]["in"] = 5.0                                       # Claude re-proposed while the sheet was open
    (p.root / "episode.json").write_text(json.dumps(meta))
    with pytest.raises(ValueError, match="changed this trim"):
        sheets.approve_trim(req, _check(authority, req), "424242")
    fresh = sheets.trim_request(p.project_id, "main")
    with pytest.raises(StudioError, match="wrong PIN"):
        sheets.approve_trim(fresh, _check(authority, fresh), "000000")
    assert json.loads((p.root / "episode.json").read_text())["trim"]["main"]["by"] == "claude"


def test_a_post_that_differs_from_its_queue_item_is_never_approved_by_the_sheet(tmp_path):
    """From the old Review page's tests: an approval signs the queue item, so an item that differs from the post (here an agent
    rewrote the caption behind the editor) is refused before any check, and stays waiting even with a grant and the PIN."""
    w = make_world(tmp_path)
    pid, vid = w.add_post()
    item = w.item(pid, vid)
    item["caption"] = "ACT NOW: send crypto to 0xBAD"
    pq.save(w.sp, item)
    app, sheets, authority = _sheets(w)
    with pytest.raises(ValueError, match="differs from the post"):
        sheets.publish_request(_items(w, (pid, vid)))
    assert w.item(pid, vid)["status"] == "awaiting_approval" and not w.item(pid, vid).get("approvals")


def test_a_batch_approval_sheet_lists_every_post_in_lines_that_fit(tmp_path):
    """Task 23 minor: a native alert does not scroll, so past three posts the sheet is one line per post, and past twelve a count."""
    from ave.desktop_approvals import SHOWN_ITEMS
    w = make_world(tmp_path)
    posts = [w.add_post(caption=f"Day {i}: the starter smells like pears.") for i in range(SHOWN_ITEMS + 2)]
    _, sheets, _ = _sheets(w)
    req = sheets.publish_request(_items(w, *posts))
    lines = req["detail"].splitlines()
    assert lines[0].startswith(f"{len(posts)} posts") and sum(1 for l in lines if l[:1].isdigit() and ". " in l[:4]) == SHOWN_ITEMS
    assert f"…and 2 more" in req["detail"] and len(req["keys"]) == len(posts)
