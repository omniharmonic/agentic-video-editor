"""The reducer: same events, any order, any duplication, same State. Merge rules from the architecture's "The log"."""

from __future__ import annotations

import copy
import itertools
import json
import random

from ave.project import hlc, read_log, reduce
from ave.project.hlc import iso_to_ms

_seq = itertools.count()
BASE = iso_to_ms("2026-11-02T10:00:00Z")


def ev(device, op, artifact="art", data=None, *, t=None, seen=None, person=None, eid=None, actor="human", needs=()):
    n = next(_seq)
    e = {"id": eid or f"01EV{n:06d}{device}", "hlc": hlc.format_hlc(BASE + (n if t is None else t) * 1000, 0, device),
         "device": device, "person": person or device, "actor": actor, "op": op, "artifact": artifact, "data": data or {},
         "needs": list(needs)}
    if seen is not None:
        e["seen"] = seen
    return e


def signoff(device, to, seen, frm="Draft", t=None):
    return ev(device, "stage.signed_off", data={"from": frm, "to": to}, seen=seen, t=t)


def shuffled(events, seed=7):
    out = list(events)
    random.Random(seed).shuffle(out)
    return out


def events():
    made = ev("a", "artifact.created", data={"kind": "episode", "title": "Ep 1", "stage": "Plan"}, t=1)
    s1 = signoff("a", "Draft", seen={"a": made["hlc"]}, frm="Plan", t=2)
    return [
        made, s1,
        ev("b", "artifact.updated", data={"title": "Episode one"}, t=3),
        ev("a", "artifact.updated", data={"title": "Ep 1 (final)"}, t=4),
        ev("a", "version.added", data={"version": "rough.v1~aaaa", "file": "01_PROJECT/edits/rough.v1~aaaa.json"},
           needs=[{"path": "01_PROJECT/edits/rough.v1~aaaa.json", "sha256": "9f", "bytes": 3}], t=5),
        ev("b", "version.added", data={"version": "rough.v1~bbbb", "file": "01_PROJECT/edits/rough.v1~bbbb.json"}, t=6),
        ev("b", "version.kept", data={"version": "rough.v1~bbbb"}, t=7),
        ev("b", "note.added", data={"text": "tighten the cold open"}, t=8),
        ev("a", "decision.recorded", data={"decision": "dec_music", "question": "Which bed?"}, t=9),
        ev("b", "decision.resolved", data={"decision": "dec_music", "choice": "B"}, t=10),
        ev("a", "task.updated", artifact=None, data={"task": "t1", "status": "doing"}, t=11),
        ev("b", "task.updated", artifact=None, data={"task": "t1", "status": "done"}, t=12),
        ev("a", "artifact.linked", data={"to": "art_short", "rel": "source"}, t=13),
        ev("b", "presence.set", data={"looking_at": "art"}, t=14),
        signoff("a", "Refine", seen={}, t=15),
        signoff("b", "Plan", seen={}, t=16),
    ]


def test_order_of_arrival_does_not_change_state():
    evs = events()
    assert reduce(shuffled(evs)) == reduce(evs)
    assert reduce(list(reversed(evs))) == reduce(evs)


def test_duplicate_delivery_does_not_change_state():
    evs = events()
    assert reduce(evs + copy.deepcopy(evs[3:9])) == reduce(evs)


def test_concurrent_stage_changes_become_a_conflict():
    s = reduce([signoff("a", to="Refine", seen={}), signoff("b", to="Plan", seen={})])
    assert s.artifacts["art"].stage == "Draft" and len(s.conflicts) == 1
    c = s.conflicts[0]
    assert c.artifact == "art" and c.field == "stage" and c.base == "Draft"
    assert sorted(o["to"] for o in c.options) == ["Plan", "Refine"]


def test_stage_change_that_saw_the_other_is_not_a_conflict():
    a = signoff("a", to="Refine", seen={})
    b = signoff("b", to="Plan", seen={"a": a["hlc"]}, frm="Refine")
    s = reduce([b, a])
    assert s.artifacts["art"].stage == "Plan" and s.conflicts == []


def test_a_later_change_that_saw_both_resolves_the_conflict():
    a = signoff("a", to="Refine", seen={})
    b = signoff("b", to="Plan", seen={})
    c = signoff("c", to="Finish", seen={"a": a["hlc"], "b": b["hlc"]}, frm="Refine")
    s = reduce([c, b, a])
    assert s.artifacts["art"].stage == "Finish" and s.conflicts == []


def test_stage_stays_at_the_last_agreed_value():
    made = ev("a", "artifact.created", data={"stage": "Plan"})
    agreed = signoff("a", "Draft", seen={"a": made["hlc"]}, frm="Plan")
    x = signoff("a", "Refine", seen={"a": agreed["hlc"]})
    y = signoff("b", "Finish", seen={"a": agreed["hlc"]})
    s = reduce([made, agreed, x, y])
    assert s.artifacts["art"].stage == "Draft" and len(s.conflicts) == 1


def test_concurrent_changes_that_agree_are_not_a_conflict():
    s = reduce([signoff("a", "Refine", seen={}), signoff("b", "Refine", seen={})])
    assert s.artifacts["art"].stage == "Refine" and s.conflicts == []


def test_later_events_on_the_same_computer_saw_the_earlier_ones():
    a1 = signoff("a", "Refine", seen={})
    a2 = signoff("a", "Finish", seen={}, frm="Refine")
    s = reduce([a2, a1])
    assert s.artifacts["art"].stage == "Finish" and not s.conflicts


def test_scalar_fields_last_writer_wins_and_keep_history():
    s = reduce(events())
    a = s.artifacts["art"]
    assert a.title == "Ep 1 (final)" and a.kind == "episode"
    assert [h["value"] for h in a.history["title"]] == ["Ep 1", "Episode one", "Ep 1 (final)"]


def test_versions_notes_and_decisions_union_by_id():
    s = reduce(events())
    a = s.artifacts["art"]
    assert [v["version"] for v in sorted(a.versions.values(), key=lambda v: v["number"])] == ["rough.v1~aaaa", "rough.v1~bbbb"]
    assert a.kept == "rough.v1~bbbb"
    assert a.needs == [{"path": "01_PROJECT/edits/rough.v1~aaaa.json", "sha256": "9f", "bytes": 3}]
    assert len(s.notes) == 1 and a.notes == list(s.notes)
    assert s.decisions["dec_music"]["status"] == "resolved" and s.decisions["dec_music"]["data"]["question"] == "Which bed?"
    assert s.tasks["t1"]["fields"]["status"] == "done"
    assert a.links == [{"to": "art_short", "rel": "source"}]
    assert s.presence["b"]["data"] == {"looking_at": "art"}


def test_resolved_before_recorded_arrives_still_merges():
    rec = ev("a", "decision.recorded", data={"decision": "d1", "question": "Q"}, t=1)
    res = ev("b", "decision.resolved", data={"decision": "d1", "choice": "x"}, t=2)
    assert reduce([res]).decisions["d1"]["status"] == "resolved"
    full = reduce([res, rec]).decisions["d1"]
    assert full["status"] == "resolved" and full["data"]["question"] == "Q"


def test_bad_events_become_warnings_not_errors():
    good = ev("a", "note.added", data={"text": "ok"})
    s = reduce([good, {"id": "x"}, "nonsense", ev("a", "artifact.exploded"), ev("a", "stage.signed_off", data={"to": "Done"}),
                ev("a", "artifact.updated", artifact=None)])
    assert len(s.notes) == 1 and len(s.warnings) == 5


def test_two_different_events_with_one_id_resolve_the_same_way_everywhere():
    x = ev("a", "note.added", data={"text": "one"}, eid="SAME")
    y = dict(x, data={"text": "two"})
    assert reduce([x, y]) == reduce([y, x])
    assert any("share this id" in w for w in reduce([x, y]).warnings)


# ------------------------------------------------------------------------------------------------ property-style

def _simulate(rng):
    """Three computers editing two artifacts, syncing whole log prefixes now and then. Returns every event written."""
    devices = ["aaaa", "bbbb", "cccc"]
    logs = {d: [] for d in devices}
    known = {d: {o: 0 for o in devices} for d in devices}  # how much of each log each computer has received
    clocks = {d: hlc.Clock(d, wall=(lambda d=d: BASE + rng.randint(-5000, 5000))) for d in devices}
    for step in range(rng.randint(20, 60)):
        d = rng.choice(devices)
        if rng.random() < 0.3:
            o = rng.choice([x for x in devices if x != d])
            known[d][o] = rng.randint(known[d][o], len(logs[o]))
            continue
        seen_events = [e for o in devices for e in logs[o][: (len(logs[o]) if o == d else known[d][o])]]
        for e in seen_events:
            clocks[d].observe(e["hlc"])
        seen = {}
        for e in seen_events:
            if e["device"] not in seen or hlc.parse(e["hlc"]) > hlc.parse(seen[e["device"]]):
                seen[e["device"]] = e["hlc"]
        art = rng.choice(["art1", "art2"])
        op = rng.choice(["artifact.updated", "version.added", "version.kept", "stage.signed_off", "stage.reopened",
                         "note.added", "decision.recorded", "decision.resolved", "task.updated", "artifact.linked"])
        data = {
            "artifact.updated": {"title": f"t{rng.randint(0, 5)}", "length": rng.randint(1, 9)},
            "version.added": {"version": f"rough.v{rng.randint(1, 4)}~{d}"},
            "version.kept": {"version": f"rough.v{rng.randint(1, 4)}~{d}"},
            "stage.signed_off": {"to": rng.choice(["Plan", "Draft", "Refine", "Finish", "Out"])},
            "stage.reopened": {"to": rng.choice(["Plan", "Draft"])},
            "note.added": {"text": "n", "note": f"n{rng.randint(0, 6)}"},
            "decision.recorded": {"decision": f"d{rng.randint(0, 3)}", "question": "?"},
            "decision.resolved": {"decision": f"d{rng.randint(0, 3)}", "choice": rng.randint(0, 2)},
            "task.updated": {"task": f"t{rng.randint(0, 3)}", "status": rng.choice(["todo", "doing", "done"])},
            "artifact.linked": {"to": rng.choice(["art1", "art2", "art3"])},
        }[op]
        e = {"id": hlc.ulid(), "hlc": clocks[d].tick(), "device": d, "person": d, "actor": "human", "op": op,
             "artifact": art, "data": data, "needs": [], "seen": seen}
        logs[d].append(e)
    return [e for d in devices for e in logs[d]]


def test_random_shuffles_and_duplicates_reduce_identically():
    rng = random.Random(20261102)
    conflicts_seen = 0
    for _ in range(200):
        evs = _simulate(rng)
        want = reduce(evs)
        conflicts_seen += bool(want.conflicts)
        for _ in range(3):
            dup = evs + [copy.deepcopy(rng.choice(evs)) for _ in range(rng.randint(0, len(evs)))]
            rng.shuffle(dup)
            assert reduce(dup) == want
    assert conflicts_seen > 10  # the generator really does produce concurrent stage changes


# ------------------------------------------------------------------------------------------------ scalar writes and saw (I1)

def _after(prev, device, data, op="artifact.updated", artifact="art"):
    """An event written by `device` right after reading `prev`, as Log.append makes it (R1: its stamp follows prev's)."""
    e = ev(device, op, artifact=artifact, data=data, seen={prev["device"]: prev["hlc"]})
    e["hlc"] = hlc.after(prev["hlc"], device, BASE)
    return e


def test_a_concurrent_far_future_write_wins_by_stamp_but_a_later_write_that_saw_it_wins_over_it():
    future = ev("ffff", "artifact.updated", data={"title": "from the future"}, t=4 * 365 * 86400)
    sane = ev("bbbb", "artifact.updated", data={"title": "sane"}, t=10, seen={})
    assert reduce([sane, future]).artifacts["art"].title == "from the future"
    fixed = _after(future, "cccc", {"title": "fixed after seeing it"})
    s = reduce([fixed, sane, future])
    assert s.artifacts["art"].title == "fixed after seeing it"
    assert [h["value"] for h in s.artifacts["art"].history["title"]] == ["sane", "from the future", "fixed after seeing it"]


def test_a_write_cannot_claim_to_have_seen_a_later_stamp():
    future = ev("ffff", "artifact.updated", data={"title": "from the future"}, t=4 * 365 * 86400)
    forged = ev("cccc", "artifact.updated", data={"title": "lower stamp"}, t=20, seen={"ffff": future["hlc"]})
    s = reduce([forged, future])
    assert s.artifacts["art"].title == "from the future" and any("not before" in w for w in s.warnings)


def test_kept_and_task_fields_follow_saw_too():
    a = ev("ffff", "version.kept", data={"version": "v9"}, t=10_000)
    b = _after(a, "bbbb", {"version": "v2"}, op="version.kept")
    concurrent = ev("cccc", "version.kept", data={"version": "v5"}, t=9_000, seen={})
    ta = ev("ffff", "task.updated", artifact=None, data={"task": "t", "status": "done"}, t=10_000)
    tb = _after(ta, "bbbb", {"task": "t", "status": "todo"}, op="task.updated", artifact=None)
    s = reduce([a, b, concurrent, ta, tb])
    assert s.artifacts["art"].kept == "v2" and s.tasks["t"]["fields"]["status"] == "todo"


# ------------------------------------------------------------------------------------------------ minor rulings

def test_version_numbers_follow_each_version_s_own_stamp():
    v1 = ev("a", "version.added", data={"version": "rough.v1~a"}, t=1)
    v2 = ev("b", "version.added", data={"version": "rough.v1~b"}, t=2)
    v3 = ev("a", "version.added", data={"version": "rough.v2~a"}, t=3)
    before = reduce([v2, v3]).artifacts["art"].versions
    assert {k: v["number"] for k, v in before.items()} == {"rough.v1~b": 1, "rough.v2~a": 2}
    after = reduce([v3, v2, v1]).artifacts["art"].versions
    assert {k: v["number"] for k, v in after.items()} == {"rough.v1~a": 1, "rough.v1~b": 2, "rough.v2~a": 3}


def test_a_link_without_a_target_does_not_create_the_artifact():
    s = reduce([ev("a", "artifact.linked", artifact="ghost", data={})])
    assert "ghost" not in s.artifacts and s.warnings


def test_an_event_whose_stamp_names_another_device_is_dropped():
    e = ev("a", "note.added", data={"text": "x"})
    e["device"] = "b"
    s = reduce([e])
    assert not s.notes and any("not the event's device" in w for w in s.warnings)


def test_stamps_beyond_year_9000_are_rejected():
    e = ev("a", "note.added")
    e["hlc"] = "9001-01-01T00:00:00.000Z/0000/a"
    assert not reduce([e]).notes


def test_id_like_fields_must_be_strings():
    bad = [ev("a", "version.added", data={"version": ["x"]}), ev("a", "note.added", data={"note": {"a": 1}}),
           ev("a", "decision.resolved", data={"decision": 3}), ev("a", "task.updated", artifact=None, data={"task": []}),
           ev("a", "stage.signed_off", data={"to": ["Draft"]}, seen={}), ev("a", "artifact.linked", data={"to": "x", "rel": 1}),
           dict(ev("a", "stage.signed_off", data={"to": "Draft"}), seen=["nope"])]
    s = reduce(bad)
    assert not s.artifacts and not s.notes and not s.decisions and not s.tasks and len(s.warnings) == len(bad)


# ------------------------------------------------------------------------------------------------ fuzz (C1)

def _random_json(rng, depth=0):
    kind = rng.randrange(9 if depth < 6 else 5)
    if kind == 0:
        return None
    if kind == 1:
        return rng.choice([True, False])
    if kind == 2:
        return rng.randint(-10**20, 10**20)
    if kind == 3:
        return rng.random() * 1e300
    if kind == 4:
        return rng.choice(["", "x" * rng.randint(0, 200_000), "Plan", "Draft", "2026-11-02T10:00:00.000Z/0000/a", "\u0000"])
    if kind in (5, 6):
        return [_random_json(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    return {rng.choice(["id", "hlc", "op", "data", "to", "version", "seen", "a", "needs"]): _random_json(rng, depth + 1)
            for _ in range(rng.randint(0, 5))}


def _mutate(rng, e):
    e = copy.deepcopy(e)
    for _ in range(rng.randint(1, 3)):
        target = e if rng.random() < 0.5 or not isinstance(e.get("data"), dict) else e["data"]
        key = rng.choice(list(target) + ["version", "note", "decision", "task", "to", "from", "rel", "seen", "needs"])
        target[key] = _random_json(rng)
    return e


def test_fuzzed_logs_never_raise(tmp_path):
    rng = random.Random(1102)
    base = events()
    for i in range(150):
        lines = []
        pool = []
        for _ in range(rng.randint(1, 12)):
            r = rng.random()
            if r < 0.4:
                v = _mutate(rng, rng.choice(base))
            elif r < 0.6:
                v = _random_json(rng)
            else:
                v = copy.deepcopy(rng.choice(base))
            pool.append(v)
            lines.append(json.dumps(v))
        if rng.random() < 0.3:
            lines.append("[" * 100_000 + "]" * 100_000)
        if rng.random() < 0.3:
            lines.append('{"data":' * 5000 + "1" + "}" * 5000)
        if rng.random() < 0.3:
            lines.append(json.dumps({"id": "deep", "hlc": "2026-11-02T10:00:00.000Z/0000/a", "device": "a", "op": "note.added",
                                     "data": {"x": json.loads("[" * 500 + "]" * 500)}}))
        p = tmp_path / f"a{i}.jsonl"
        p.write_text("\n".join(lines) + ("\n" if rng.random() < 0.5 else ""))
        read = read_log(p)
        reduce(read)
        reduce(pool)
        reduce(pool + list(read) + [rng.choice(pool)])


def test_a_hand_edited_seen_cycle_is_rejected_and_the_field_survives():
    made = ev("A", "artifact.created", data={"title": "t0"}, t=1, seen={})
    x = ev("A", "artifact.updated", data={"title": "X-forged"}, t=5, seen={"B": hlc.format_hlc(BASE + 8_000_000_000, 0, "B")})
    y = ev("B", "artifact.updated", data={"title": "Y"}, t=6, seen={"A": x["hlc"]})
    s = reduce([made, x, y])
    assert s.artifacts["art"].title == "Y"
    assert any("not before the event's own hlc" in w for w in s.warnings)


def test_seen_entries_must_be_stamps_before_the_event():
    e = ev("a", "note.added", seen={"b": "not a stamp"})
    own = ev("a", "note.added")
    own["seen"] = {"a": own["hlc"]}
    assert not reduce([e]).notes and not reduce([own]).notes
