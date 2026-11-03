"""Feedback / alerts ledgers: shape, claim and resolve, expiry of claims, torn lines, human_edit born resolved."""

import json
from datetime import timedelta

import pytest

from ave.studio import events, ledger, model
from ave.studio.ledger import ClaimConflict


def add(sp, clock, **kw):
    line = {"post": "p_x", "variant": "v_a", "rev": 3, "kind": "rework_copy", "note": "too salesy", "by": "maren", "source": "ui", **kw}
    return ledger.append_feedback(sp, line, now=clock.now())


def test_feedback_line_shape_and_ids(bare, clock):
    a, b = add(bare, clock), add(bare, clock, kind="note")
    assert (a["id"], b["id"]) == ("fb_0001", "fb_0002")
    assert a["op"] == "feedback" and a["v"] == 1 and a["ts"] == "2026-10-19T16:00:00Z"
    assert model.validate("feedback", a) == []
    raw = [json.loads(x) for x in ledger.feedback_path(bare).read_text().splitlines()]
    assert [r["op"] for r in raw] == ["feedback", "feedback"]


def test_open_is_a_feedback_line_without_a_resolve(bare, clock):
    a, b = add(bare, clock), add(bare, clock, post="p_y", variant=None)
    assert [f["id"] for f in ledger.open_feedback(bare, now=clock.now())] == [a["id"], b["id"]]
    ledger.resolve_feedback(bare, a["id"], "claude", rev=4, note="shortened", now=clock.now())
    assert [f["id"] for f in ledger.open_feedback(bare, now=clock.now())] == [b["id"]]
    assert [f["id"] for f in ledger.open_feedback(bare, post="p_y", now=clock.now())] == [b["id"]]
    assert ledger.open_feedback(bare, post="p_x", now=clock.now()) == []
    with pytest.raises(ValueError, match="already resolved"):
        ledger.resolve_feedback(bare, a["id"], "claude")
    with pytest.raises(ValueError):
        ledger.resolve_feedback(bare, "fb_9999", "claude")
    lines = [json.loads(x) for x in ledger.feedback_path(bare).read_text().splitlines()]
    want = {"op": "resolve", "fb": a["id"], "by": "claude", "ts": "2026-10-19T16:00:00Z", "rev": 4, "note": "shortened"}
    assert {k: lines[-1][k] for k in want} == want and lines[-1]["human"] is False          # plus the seal's seq / prev / sig
    assert model.validate("feedback", lines[-1]) == []


def test_variant_filter_includes_post_wide_lines(bare, clock):
    add(bare, clock, variant="v_a")
    add(bare, clock, variant="v_b")
    add(bare, clock, variant=None)
    assert len(ledger.open_feedback(bare, variant="v_a", now=clock.now())) == 2
    assert len(ledger.open_feedback(bare, kind="rework_copy", now=clock.now())) == 3
    assert ledger.open_feedback(bare, kind="drop", now=clock.now()) == []


def test_claim_takes_the_oldest_open_item_and_expires_after_30_minutes(bare, clock):
    a, b = add(bare, clock), add(bare, clock)
    got = ledger.claim_feedback(bare, "claude-7", now=clock.now())
    assert got["id"] == a["id"] and got["claim"]["by"] == "claude-7"
    assert ledger.claim_feedback(bare, "claude-8", now=clock.now())["id"] == b["id"]        # a is claimed, so the next one
    assert ledger.claim_feedback(bare, "claude-9", now=clock.now()) is None
    with pytest.raises(ClaimConflict):
        ledger.claim_feedback(bare, "claude-9", fb=a["id"], now=clock.now())
    assert ledger.claim_feedback(bare, "claude-7", fb=a["id"], now=clock.now())["id"] == a["id"]      # the holder may refresh
    clock.advance(minutes=31)
    assert ledger.claim_feedback(bare, "claude-9", now=clock.now())["id"] == a["id"]         # expired claims are up for grabs
    open_now = {f["id"]: f for f in ledger.open_feedback(bare, now=clock.now())}
    assert open_now[a["id"]]["claim"]["by"] == "claude-9"


def test_resolving_clears_the_claim_path(bare, clock):
    a = add(bare, clock)
    ledger.claim_feedback(bare, "claude-7", now=clock.now())
    ledger.resolve_feedback(bare, a["id"], "claude-7", now=clock.now())
    assert ledger.claim_feedback(bare, "claude-8", now=clock.now()) is None
    with pytest.raises(ValueError):
        ledger.claim_feedback(bare, "claude-8", fb=a["id"], now=clock.now())


def test_human_edit_lines_are_born_resolved(bare, clock):
    h = add(bare, clock, kind="human_edit", structured={"copy": {"field": "copy.base", "before": "a", "after": "b"}})
    assert ledger.open_feedback(bare, now=clock.now()) == []
    folded = ledger.all_feedback(bare, now=clock.now())
    assert folded[0]["id"] == h["id"] and folded[0]["resolved"]["by"] == "system"


def test_a_torn_last_line_does_not_swallow_the_next_record(bare, clock):
    add(bare, clock)
    with open(ledger.feedback_path(bare), "ab") as f:
        f.write(b'{"op":"feedback","id":"fb_00')                  # crash mid-write
    b = add(bare, clock)
    assert b["id"] == "fb_0002"
    ids = [f["id"] for f in ledger.all_feedback(bare, now=clock.now())]
    assert ids == ["fb_0001", "fb_0002"]


def test_reader_never_consumes_a_partial_line(tmp_path):
    p = tmp_path / "l.jsonl"
    p.write_bytes(b'{"a":1}\n{"b":')
    rows, off = ledger.read_jsonl(p)
    assert rows == [{"a": 1}] and off == len(b'{"a":1}\n')
    p.write_bytes(p.read_bytes() + b'2}\n')
    rows, off2 = ledger.read_jsonl(p, off)
    assert rows == [{"b": 2}] and off2 == p.stat().st_size


def test_alerts_append_ack_and_redaction(bare, clock):
    a = ledger.append_alert(bare, severity="warn", kind="account_not_live", message="skipped; token=sk_live_abcdef123456", account="ig-main",
                            now=clock.now())
    assert a["id"] == "al_0001" and a["ack"] is None and "abcdef123456" not in a["message"]
    assert model.validate("alert", a) == []
    assert [x["id"] for x in ledger.list_alerts(bare)] == ["al_0001"]
    ledger.ack_alert(bare, "al_0001", "maren", now=clock.now())
    assert ledger.list_alerts(bare) == [] and ledger.list_alerts(bare, include_acked=True)[0]["ack"]["by"] == "maren"
    with pytest.raises(ValueError):
        ledger.ack_alert(bare, "al_0042", "maren")
    with pytest.raises(ValueError):
        ledger.append_alert(bare, severity="loud", kind="quota", message="x")


def test_events_ledger_survives_concurrent_appends(bare):
    import threading
    ts = [threading.Thread(target=lambda i=i: [events.append_event(bare, events.make_event(actor=events.AGENT, op=f"o{i}")) for _ in range(20)])
          for i in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    rows = events.changes(bare, "ev:0")["events"]
    assert len(rows) == 120 and all(r["op"].startswith("o") for r in rows)
