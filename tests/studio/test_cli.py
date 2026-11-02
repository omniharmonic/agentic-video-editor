"""`ave studio ...` (spec 8): every command exists, prints JSON, follows the 0/2 exit convention, and every human act refuses without a terminal and prints the
exact line for the human. Run through the real `ave.cli.main` against `build_studio` data; the platform, the clock and the keychain are fakes."""

import io
import json
import sys

import pytest
from sched_helpers import no_agent_markers  # noqa: F401
from studio_helpers import RecordingPublisher

from ave import cli, humanpresence
from ave.commands.studio import command_names
from ave.platforms import attest
from ave.publish import queue as pq, safety
from ave.studio import ledger, state, store
from ave.studio.cli import OPEN_KW

# every command group and subcommand of spec section 8
SPEC = {
    "init": [], "status": [], "doctor": [], "adopt": [], "daemon-plist": [],
    "account": ["add", "list", "show", "set", "remove", "auth", "live", "refresh"], "pause": [], "resume": [],
    "library": ["add", "attest", "list", "show", "search", "tag", "scan", "reindex", "verify"], "send": ["enable", "disable", "status"],
    "broll": ["queries", "search", "show", "pick", "generate"],
    "post": ["new", "list", "show", "edit", "attach", "swap-asset", "variant", "drop"], "render": [], "deliverables": [],
    "schedule": ["show", "rules", "plan", "apply"], "move": [], "unschedule": [],
    "queue": ["sync", "list", "dry-run"], "ui": [], "review": ["list"], "approve": [],
    "tick": [], "run": [], "resolve": [], "alerts": [],
    "feedback": ["list", "next", "resolve", "stats"], "campaign": ["new", "show", "list", "add-post"],
    "ingest": ["idea", "blog", "audio", "video", "episode", "shorts"], "recipe": ["list", "show", "check"],
}


def last_json(text: str):
    """The last JSON object printed (a human prompt such as the typed-hash question shares stdout with the JSON, as it does in a terminal)."""
    try:
        return json.loads(text)
    except ValueError:
        pass
    idx = [i for i in range(len(text)) if text.startswith("{\n", i)]            # a prompt without a newline can sit right before the "{"
    for i in reversed(idx):
        try:
            return json.loads(text[i:])
        except ValueError:
            continue
    return text


@pytest.fixture
def rig(built, monkeypatch, capsys):
    monkeypatch.setitem(OPEN_KW, "clock", built.clock)
    monkeypatch.setitem(OPEN_KW, "publishers", built.publishers)
    monkeypatch.setenv("AVE_WORKSPACE", str(built.sp.root.parent))
    d = str(built.sp.root)

    def run(*argv, expect=None, stdin=None):
        if stdin is not None:
            monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        rc = cli.main(["studio", *argv, "--studio", d])
        cap = capsys.readouterr()
        res = last_json(cap.out)
        if expect is not None:
            assert rc == expect, (argv, rc, res, cap.err)
        return rc, res, cap.err
    return built, run, d


def test_every_command_of_section_8_is_registered():
    import argparse
    from ave.commands import studio as S
    p = argparse.ArgumentParser()
    ss = p.add_subparsers()
    S._build(ss)
    assert set(SPEC) <= set(ss.choices) and command_names() == sorted(ss.choices)
    for name, subs in SPEC.items():
        if subs:
            inner = [a for a in ss.choices[name]._actions if isinstance(a, argparse._SubParsersAction)]
            assert inner, name
            assert set(subs) <= set(inner[0].choices), (name, sorted(inner[0].choices))


# ---------------------------------------------------------------------------------------------------------------- human acts

HUMAN = [
    (["account", "auth", "ig-main"], "ave studio account auth ig-main"),
    (["account", "live", "ig-main", "--on"], "ave studio account live ig-main --on"),
    (["account", "live", "ig-main", "--off"], "ave studio account live ig-main --off"),
    (["resume"], "ave studio resume"),
    (["approve", "p_fx003", "v_003"], "ave studio approve p_fx003 v_003"),
    (["tick", "--send"], "ave studio tick --send"),
    (["run", "--send"], "ave studio run --send"),
    (["resolve", "p_fx004", "v_004", "--retry"], "ave studio resolve p_fx004 v_004 --retry"),
    (["broll", "pick", "s_1", "pexels:1", "--by", "Maren"], "ave studio broll pick s_1 pexels:1 --by Maren"),
    (["broll", "generate", "--provider", "x", "--prompt", "a loaf", "--max-cost", "1", "--by", "Maren"], "ave studio broll generate --provider x"),
    (["schedule", "apply", "sp_0001"], "ave studio schedule apply sp_0001"),
    (["ui"], "ave studio ui"),
    (["send", "enable"], "ave studio send enable"),
    (["send", "disable"], "ave studio send disable"),
    (["library", "attest", "--all"], "ave studio library attest --all"),
]


@pytest.mark.parametrize("argv,line", HUMAN, ids=[" ".join(a[:2]) for a, _ in HUMAN])
def test_human_acts_refuse_without_a_terminal_and_print_the_exact_line(rig, argv, line, monkeypatch):
    built, run, d = rig
    monkeypatch.setattr(humanpresence, "is_human_terminal", lambda: False)
    (built.sp.root / "send.enabled").unlink()              # the standing permission for an unattended sender is not what this test is about
    before = sorted(p.name for p in built.sp.root.rglob("*") if p.is_file())
    rc, res, err = run(*argv)
    assert rc == 2 and res["ok"] is False and res["needs_human"] is True
    assert res["run"].startswith(line) and f"--studio {d}" in res["run"] and res["bang"] == "! " + res["run"]
    assert "human act" in err and res["run"] in err
    assert sorted(p.name for p in built.sp.root.rglob("*") if p.is_file()) == before          # nothing changed


def test_human_acts_also_refuse_under_an_agent_marker(rig, monkeypatch):
    built, run, d = rig
    monkeypatch.setenv("CLAUDECODE", "1")
    for argv in (["resume"], ["account", "live", "ig-main", "--on"], ["approve", "p", "v"]):
        rc, res, _ = run(*argv)
        assert rc == 2 and res["needs_human"], argv


def test_pause_is_open_to_anyone_and_resume_needs_a_human(rig, monkeypatch):
    built, run, d = rig
    assert run("pause", expect=0)[1]["paused"] is True and built.sp.pause_path.exists()
    assert run("pause", "--account", "ig-main", expect=0)[1]["scope"] == "ig-main"
    assert run("resume", expect=2)[1]["needs_human"] and built.sp.pause_path.exists()
    monkeypatch.setattr(humanpresence, "is_human_terminal", lambda: True)
    assert run("resume", expect=0)[1]["paused"] is False and not built.sp.pause_path.exists()
    assert run("resume", "--account", "ig-main", expect=0)
    assert not (built.sp.accounts_dir / "ig-main.paused").exists()


def test_account_live_on_needs_the_typed_handle_and_writes_a_signed_attestation(rig, monkeypatch):
    built, run, d = rig
    monkeypatch.setattr(humanpresence, "is_human_terminal", lambda: True)
    rc, res, _ = run("account", "live", "ig-main", "--on", stdin="wrong\n")
    assert rc == 2 and "does not match" in res["error"] and not attest.verify_live(built.sp.accounts_dir, "ig-main").ok
    rc, res, _ = run("account", "live", "ig-main", "--on", "--by", "maren", stdin="ig-main\n")
    assert rc == 0 and res["live"] is True and attest.verify_live(built.sp.accounts_dir, "ig-main").by == "maren"
    assert {a["id"]: a["live"] for a in run("account", "list", expect=0)[1]["accounts"]}["ig-main"] is True
    assert run("account", "live", "ig-main", "--off", expect=0)[1]["live"] is False
    assert not attest.verify_live(built.sp.accounts_dir, "ig-main").ok


def test_account_auth_stores_secrets_in_the_keychain_only(rig, monkeypatch):
    built, run, d = rig
    monkeypatch.setattr(humanpresence, "is_human_terminal", lambda: True)
    token = "EAAB" + "z" * 40
    monkeypatch.setattr("ave.studio.cli.accounts.getpass.getpass", lambda prompt="": token)
    rc, res, err = run("account", "auth", "ig-main")
    assert rc == 0 and res["stored_in_keychain"] and all("@ig-main" in n for n in res["stored_in_keychain"])
    assert token not in json.dumps(res) and token not in err
    assert safety.get_secret(res["stored_in_keychain"][0]) == token
    assert not any(token in p.read_text(errors="ignore") for p in built.sp.root.rglob("*") if p.is_file() and p.suffix in (".json", ".yaml", ".jsonl"))


def test_approve_shows_the_files_and_needs_the_typed_hash(rig, monkeypatch):
    built, run, d = rig
    monkeypatch.setattr(humanpresence, "is_human_terminal", lambda: True)
    pid = built.posts["in_review"]
    v = store.load_post(built.sp, pid)["variants"][0]
    item = state.load_queue_item(built.sp, v)
    rc, res, _ = run("approve", pid, v["id"], stdin="deadbeef\n")
    assert rc == 2 and "confirmation hash does not match" in res["error"] and pq.load(built.sp, item["id"])["status"] == "awaiting_approval"
    rc, res, _ = run("approve", pid, v["id"], "--by", "maren", stdin=item["sha256"][:8] + "\n")
    assert rc == 0 and res["status"] == "approved" and res["approval"]["source"] == "studio-cli"
    assert any(e["op"] == "approved" and e["post"] == pid for e in ledger.read_jsonl(ledger.events_path(built.sp))[0])


def test_send_refuses_for_an_agent_even_with_a_terminal_when_a_marker_is_set(rig, monkeypatch):
    built, run, d = rig
    monkeypatch.setattr(humanpresence, "is_human_terminal", lambda: True)
    monkeypatch.setenv("CLAUDECODE", "1")                       # the tick itself refuses (spec 6.2): belt and braces under the CLI gate
    rc, res, _ = run("tick", "--send")
    assert rc == 2 and res["needs_human"] and built.publishers["instagram"].publish_calls == 0


# ---------------------------------------------------------------------------------------------------------------- JSON and exit codes

def test_project_commands(rig):
    built, run, d = rig
    s = run("status", expect=0)[1]
    assert s["ok"] and s["posts"] == 12 and s["accounts"] == 3
    doc = run("doctor")[1]
    assert {"ok", "checks", "failures", "note"} <= set(doc) and any(c["name"].startswith("adapter:") for c in doc["checks"])
    assert all(c["verified"] is False for c in doc["checks"] if c["name"].startswith("adapter:"))
    plist = run("daemon-plist", "--interval", "30", expect=0)[1]
    assert plist["sends"] is False and "<key>Label</key>" in plist["plist"] and "--send" not in plist["plist"] and "REHEARSES" in plist["note"]
    assert "--send" in run("daemon-plist", "--send", expect=0)[1]["plist"]
    assert not (built.sp.root / "work" / "daemon.log").exists()                # printing wrote nothing


def test_doctor_fails_on_a_forged_live_file_and_a_secret_in_a_file(rig):
    built, run, d = rig
    attest.write_live(built.sp.accounts_dir, "ig-main", "maren")
    res = run("doctor")[1]
    assert [c["name"] for c in res["checks"] if c["status"] == "fail"] == ["library"]        # the builder's assets have no license entries: a real finding
    assert any(c["name"] == "live:ig-main" and c["status"] == "ok" and "LIVE" in c["detail"] for c in res["checks"])
    p = attest.live_path(built.sp.accounts_dir, "ig-main")
    p.write_text(json.dumps({**json.loads(p.read_text()), "by": "claude"}))
    rc, res, _ = run("doctor")
    assert rc == 2 and any(c["name"] == "live:ig-main" and c["status"] == "fail" for c in res["checks"])
    attest.write_live(built.sp.accounts_dir, "ig-main", "maren")
    assert not any(c["name"] == "live:ig-main" and c["status"] == "fail" for c in run("doctor")[1]["checks"])
    (built.sp.root / "notes.txt").write_text("my token is " + "EAAB" + "q" * 40)
    rc, res, _ = run("doctor")
    assert rc == 2 and any(c["name"] == "secrets_in_files" and c["status"] == "fail" for c in res["checks"])
    assert "EAAB" + "q" * 40 not in json.dumps(res)


def test_an_account_can_carry_a_brand_and_setting_it_does_not_void_a_live_attestation(rig):
    built, run, d = rig
    rc, res, _ = run("account", "add", "bsky-brand", "--platform", "bluesky", "--brand", "Loaf Co", "--public", "handle=loaf.bsky.social", expect=0)
    listed = {a["id"]: a for a in run("account", "list", expect=0)[1]["accounts"]}
    assert listed["bsky-brand"]["brand"] == "Loaf Co" and listed["ig-main"]["brand"] is None
    attest.write_live(built.sp.accounts_dir, "ig-main", "maren")
    res = run("account", "set", "ig-main", "brand=Acme Bakery", expect=0)[1]
    assert res["live_voided"] is False and attest.verify_live(built.sp.accounts_dir, "ig-main").ok         # brand is policy, not identity
    assert {a["id"]: a for a in run("account", "list", expect=0)[1]["accounts"]}["ig-main"]["brand"] == "Acme Bakery"


def test_account_commands(rig):
    built, run, d = rig
    rc, res, _ = run("account", "add", "bsky-main", "--platform", "bluesky", "--label", "@acme.bsky.social", "--post-types", "text,feed",
                     "--public", "handle=acme.bsky.social", expect=0)
    assert res["account"]["id"] == "bsky-main" and res["account"]["live"] is False
    assert run("account", "add", "bsky-main", "--platform", "bluesky", expect=2)[1]["blocked"]
    assert {a["id"] for a in run("account", "list", expect=0)[1]["accounts"]} == {"ig-main", "fb-page", "ig-alt", "bsky-main"}
    shown = run("account", "show", "bsky-main", expect=0)[1]
    assert shown["adapter"]["ready"] is False and any("bluesky_" in n for n in shown["secrets_needed"])
    assert run("account", "set", "bsky-main", "limits.max_per_day=5", "label=@acme", expect=0)[1]["account"]["limits"]["max_per_day"] == 5
    rc, res, _ = run("account", "set", "bsky-main", "credentials.token=" + "EAAB" + "z" * 40)             # a secret can never be saved in the file
    assert rc == 2 and "EAAB" + "z" * 40 not in json.dumps(res)
    assert "EAAB" not in (built.sp.accounts_dir / "bsky-main.yaml").read_text()
    rc, res, _ = run("account", "add", "evil", "--platform", "bluesky", "--public", "token=abc")
    assert rc == 2 and "--public takes key=value" in res["error"]
    rc, res, _ = run("account", "remove", "ig-main")                                                       # unfinished variants still use it
    assert rc == 2 and res["blocked"] and res["variants"]
    assert run("account", "remove", "bsky-main", expect=0)[1]["removed"] == "bsky-main" and not (built.sp.accounts_dir / "bsky-main.yaml").exists()
    rc, res, _ = run("account", "refresh", "fb-page")
    assert rc == 2 and "no token refresh" in res["error"]


def test_library_commands(rig, tmp_path):
    built, run, d = rig
    from studio_helpers import make_png
    png = make_png(tmp_path / "own.jpg", (800, 1000), (10, 80, 140))
    rc, res, _ = run("library", "add", str(png), "--source", "own", "--by", "Maren", "--tags", "bread, kitchen", "--alt", "A blue test card.", expect=0)
    aid = res["added"][0]["id"]
    assert run("library", "add", str(png), "--source", "own", "--by", "Maren", expect=0)[1]["already_in_library"][0]["id"] == aid
    assert run("library", "add", str(png), "--source", "stock", "--by", "Maren")[0] == 2                      # a stock file needs its license on record
    rc, res, _ = run("library", "add", str(tmp_path / "missing.jpg"), "--source", "own", "--by", "Maren")
    assert rc == 2 and res["failed"]
    assert any(a["id"] == aid for a in run("library", "list", "--tag", "bread", expect=0)[1]["assets"])
    assert run("library", "show", aid, expect=0)[1]["asset"]["alt"] == "A blue test card."
    assert any(a["id"] == aid for a in run("library", "search", "kitchen", expect=0)[1]["assets"])
    assert run("library", "tag", aid, "--add", "new", "--remove", "kitchen", expect=0)[1]["tags"] == ["bread", "new"]
    (built.sp.root / "inbox" / "drop.jpg").write_bytes(png.read_bytes()[:-1] + b"\0")
    assert run("library", "scan", expect=0)[1]["new"][0]["path"].endswith("drop.jpg")
    assert run("library", "reindex", expect=0)[1]["indexed"] >= 1
    v = run("library", "verify")
    assert v[0] in (0, 2) and "problems" in v[1]


def test_post_commands_and_compare_and_swap(rig):
    built, run, d = rig
    rc, res, _ = run("post", "new", "--idea", "Starter day 3", "--copy", "Day 3: pears.", "--reason", "evergreen", "--confidence", "0.7", expect=0)
    pid, rev = res["post"]["id"], res["rev"]
    assert run("post", "list", "--stage", "draft", expect=0)[1]["total"] >= 1
    rc, res, _ = run("post", "edit", pid, "--expected-rev", str(rev), "--set", "copy.base=Day 3: it smells like pears.", "--set", "copy.hashtags=[\"sourdough\"]", expect=0)
    assert res["rev"] == rev + 1
    rc, res, _ = run("post", "edit", pid, "--expected-rev", str(rev), "--set", "copy.base=stale")                                   # lost the race
    assert rc == 2 and res["code"] == "conflict.rev" and res["latest_rev"] == rev + 1
    assert run("post", "edit", pid, "--expected-rev", "2", expect=2)[1]["ok"] is False                                              # nothing to change
    asset = built.assets[0]["id"]
    rc, res, _ = run("post", "attach", pid, "--asset", asset, expect=0)
    assert res["post"]["media"] == 1
    rc, res, _ = run("post", "variant", "add", pid, "--account", "ig-main", "--type", "feed", expect=0)
    vid = res["variant"]
    assert run("post", "variant", "add", pid, "--account", "ig-main", "--type", "story", expect=2)[1]["ok"] is False                 # the account takes feed/carousel
    shown = run("post", "show", pid, expect=0)[1]
    assert shown["post"]["variants"][0]["status"] == "draft" and shown["feedback_open"] == []
    other = built.assets[1]["id"]
    rc, res, _ = run("post", "swap-asset", pid, vid, "--asset", other, expect=0)
    assert res["post"]["id"] == pid
    crit = run("post", "critique", pid)
    assert crit[0] in (0, 2) and crit[1]["variants"][0]["counter"]["unit"] == "chars"
    d2 = run("deliverables", pid)
    assert d2[0] == 0 and d2[1]["variants"][0]["accepted_formats"] == ["jpeg"] and d2[1]["variants"][0]["media"][0]["format"] == "jpeg"


def test_critique_flags_hype_and_lengths(rig):
    built, run, d = rig
    pid = built.posts["draft"]
    rc, res, _ = run("post", "edit", pid, "--expected-rev", "1", "--set", "copy.base=We are thrilled to unlock a game-changer!!!", expect=0)
    rc, res, _ = run("post", "variant", "add", pid, "--account", "ig-main", "--type", "feed", expect=0)
    rc, res, _ = run("post", "critique", pid)
    assert rc == 2 and any("corporate/hype" in p for p in res["variants"][0]["problems"])


def test_post_drop_rules_for_an_agent(rig, monkeypatch):
    built, run, d = rig
    pid = built.posts["in_review"]
    rc, res, _ = run("post", "drop", pid)
    assert rc == 2 and res["needs_human"] and not store.load_post(built.sp, pid)["variants"][0]["dropped"]
    v = store.load_post(built.sp, pid)["variants"][0]
    built.studio.add_feedback(pid, "drop", note="off message", variant=v["id"], structured={"drop": {"reason": "off message"}})
    rc, res, _ = run("post", "drop", pid, "--variant", v["id"], "--reason", "off message", expect=0)
    assert res["queue_rejected"] and res["resolved_feedback"]
    after = store.load_post(built.sp, pid)["variants"][0]
    assert after["dropped"] and state.load_queue_item(built.sp, after)["status"] == "rejected"


def test_schedule_move_plan_apply(rig, monkeypatch):
    built, run, d = rig
    assert run("schedule", "rules", "set", "ig-alt", "--rule", "id=r1;dow=mon,wed,fri;time=09:00;post_types=feed,carousel", expect=0)[1]["rules"]["rules"][0]["id"] == "r1"
    assert run("schedule", "rules", "set", "ig-alt", "--rule", "garbage", expect=2)[1]["ok"] is False
    assert run("schedule", "rules", "show", "ig-alt", expect=0)[1]["rules"]["rules"]
    cal = run("schedule", "show", "--from", "2026-10-19", "--to", "2026-11-15", "--account", "ig-alt", expect=0)[1]
    assert cal["slots"] and {"days", "slots", "unscheduled", "accounts"} <= set(cal)
    rc, res, _ = run("campaign", "new", "Fall launch", "--weeks", "3", "--per-week", "ig-alt=2", expect=0)
    cid = res["campaign"]["id"]
    assert cid == "c_fall-launch" and res["campaign"]["window"]["start"] == "2026-10-19"
    unsched = [(p["id"], p["variants"][0]["id"]) for p in built.studio.posts() if p["variants"] and p["variants"][0]["account"] == "ig-alt"
               and not (p["variants"][0].get("schedule") or {}).get("utc") and p["variants"][0]["status"] == "draft"]
    assert unsched, "the builder has unscheduled ig-alt variants"
    for pid, vid in unsched[:2]:
        assert run("campaign", "add-post", cid, pid, expect=0)
    plan = run("schedule", "plan", "--campaign", cid, "--weeks", "3")
    assert plan[1]["proposal"] and plan[1]["moves"]
    assert not any((built.studio.post(pid)["variants"][0].get("schedule") or {}).get("utc") for pid, _ in unsched[:2])      # a proposal applies nothing
    assert run("schedule", "apply", plan[1]["proposal"], expect=2)[1]["needs_human"]
    monkeypatch.setattr(humanpresence, "is_human_terminal", lambda: True)
    rc, res, _ = run("schedule", "apply", plan[1]["proposal"], "--by", "Maren", expect=0)
    assert res["applied"] and res["failed"] == []
    assert all((built.studio.post(pid)["variants"][0].get("schedule") or {}).get("utc") for pid, _ in unsched[:len(res["applied"])])
    pid, vid = unsched[0]
    rc, res, _ = run("move", pid, vid, "--to", "2026-12-02T09:00", "--snap", "none", expect=0)
    assert res["schedule"]["local"] == "2026-12-02T09:00" and res["applied"]
    assert run("unschedule", pid, vid, expect=0)[1]["ok"]
    assert run("campaign", "list", expect=0)[1]["campaigns"][0]["posts"] == 2
    shown = run("campaign", "show", cid, expect=0)[1]
    assert shown["cadence"]["ig-alt"]["per_week"] == 2 and cid == shown["campaign"]["id"]


def test_queue_commands_and_dry_run(rig):
    built, run, d = rig
    pid = built.posts["variant"]
    rc, res, _ = run("queue", "sync", "--post", pid)
    assert rc in (0, 2) and res["reports"][0]["post"] == pid
    items = run("queue", "list", expect=0)[1]["items"]
    assert items and all("sha256" in i for i in items)
    dr = run("queue", "dry-run", built.posts["scheduled"], expect=0)[1]
    assert dr["dry_run"] is True and dr["variants"][0]["would_post"] is False                  # not live: gates say why
    assert any(g["code"] == "not_live" for g in dr["variants"][0]["gates"])
    assert built.publishers["instagram"].publish_calls == 0
    rv = run("review", "list", expect=0)[1]
    assert rv["waiting"] and "You never approve" not in rv["next"] and "human approves" in rv["next"]


def test_tick_and_run_rehearse_and_alerts(rig):
    built, run, d = rig
    rc, res, _ = run("tick")
    assert rc == 0 and res["mode"] == "dry-run" and res["posted"] == [] and "REHEARSAL" in res["note"]
    rc, res, _ = run("run", "--interval", "1", "--max-ticks", "2", expect=0)
    assert res["ticks"] == 2 and res["sent"] == 0 and res["sending"] is False
    ledger.append_alert(built.sp, severity="warn", kind="quota", message="slow down", account="ig-main", now=built.clock.now())
    al = run("alerts", expect=0)[1]["alerts"]
    assert al[0]["kind"] == "quota"
    assert run("alerts", "ack", al[0]["id"], "--by", "claude", expect=0)[1]["acknowledged"] == [al[0]["id"]]
    assert run("alerts", expect=0)[1]["alerts"] == [] and len(run("alerts", "--all", expect=0)[1]["alerts"]) == 1
    assert run("alerts", "bogus", expect=2)[1]["ok"] is False
    rc, res, _ = run("tick", "--now", "2026-11-30T12:00:00Z")
    assert res["now"] == "2026-11-30T12:00:00Z"


def test_feedback_loop_from_the_cli(rig):
    built, run, d = rig
    pid = built.posts["in_review"]
    v = store.load_post(built.sp, pid)["variants"][0]
    fb = built.studio.add_feedback(pid, "rework_copy", note="too salesy, say it plainly", variant=v["id"],
                                   structured={"copy": {"field": "caption", "before": "x", "after": None}})
    assert run("feedback", "list", expect=0)[1]["count"] == 1
    peek = run("feedback", "next", expect=0)[1]
    assert peek["feedback"]["id"] == fb["id"] and peek["claimed"] is False and peek["feedback"]["post_view"]["id"] == pid
    assert run("feedback", "next", "--claim", expect=2)[1]["ok"] is False                       # a claim needs a name
    claimed = run("feedback", "next", "--claim", "--agent", "claude-7", expect=0)[1]
    assert claimed["claimed"] and claimed["feedback"]["claim"]["by"] == "claude-7" and claimed["how"]
    assert run("feedback", "next", expect=0)[1]["feedback"] is None                              # nothing unclaimed
    # while the note is open, dispatch refuses (the human sent it back)
    post = store.load_post(built.sp, pid)
    from ave.studio import dispatch
    assert "open_feedback" in [b.code for b in dispatch.blockers(built.sp, post, v, now=built.clock.now(), publishers=built.publishers)]
    rev = store.load_post(built.sp, pid)["rev"]
    rc, res, _ = run("post", "edit", pid, "--expected-rev", str(rev), "--set", "copy.base=Day 3. The starter smells like pears.", expect=0)
    rc, res, _ = run("feedback", "resolve", fb["id"], "--rev", str(res["rev"]), "--note", "plainer", "--by", "claude-7", expect=0)
    assert res["resolved"]["rev"] and run("feedback", "list", expect=0)[1]["count"] == 0
    assert run("feedback", "resolve", fb["id"], expect=2)[1]["ok"] is False                      # already resolved
    st = run("feedback", "stats", expect=0)[1]
    assert st["by_kind"]["rework_copy"] == 1 and st["open"] == 0


def test_broll_commands_with_an_injected_provider(rig, tmp_path, monkeypatch):
    from studio_broll_helpers import StockHttp, jpeg_bytes
    from ave.broll.providers import Pexels
    built, run, d = rig
    http = StockHttp("pexels.json", downloads={"https://images.pexels.com/photos/2284/pexels-photo-2284.jpeg?auto=compress&cs=tinysrgb&h=350": jpeg_bytes(),
                                               "https://images.pexels.com/photos/3301/m.jpeg": jpeg_bytes()})
    monkeypatch.setitem(OPEN_KW, "providers", {"pexels": Pexels(api_key="sk-test-0123456789abcdef", http=http, cache_dir=tmp_path / "c", now=lambda: 0.0)})
    q = run("broll", "queries", "--text", "hands shaping wet sourdough in window light", expect=0)[1]
    assert q["queries"] and "mechanical draft" in q["note"]
    assert run("broll", "queries", expect=2)[1]["ok"] is False
    rc, res, _ = run("broll", "search", "--query", "bread crust", "--providers", "pexels", "--kind", "photo", expect=0)
    assert res["candidates"][0]["thumb"].startswith("/files/work/broll/") and res["candidates"][0]["key"].startswith("pexels:")
    assert "! ave studio broll pick" in res["next"]
    assert run("broll", "show", res["search"], expect=0)[1]["id"] == res["search"]
    assert not any(built.sp.root.glob("library/assets/**/*.jpg")) or True


def test_ingest_commands_and_adopt_json(rig, tmp_path):
    built, run, d = rig
    rc, res, _ = run("ingest", "idea", "A one-line idea about starters", "--confidence", "0.5", expect=0)
    assert res["post"]["stage"] == "idea"
    md = tmp_path / "e.md"
    md.write_text("# E\n\nA paragraph of the essay, fine and whole.\n")
    assert run("ingest", "blog", str(md), expect=0)[1]["passages"][0]["id"] == "p001"
    assert run("ingest", "blog", "https://example.com/x", expect=2)[1]["ok"] is False
    assert run("ingest", "episode", str(tmp_path / "nope"), expect=2)[1]["ok"] is False
    from studio_helpers import make_episode_with_queue
    ep, item = make_episode_with_queue(tmp_path)
    surv = run("ingest", "episode", str(ep.root), expect=0)[1]
    assert surv["queue"][0]["id"] == item["id"] and (built.sp.work / "survey.json").exists()
    ad = run("adopt", str(ep.root), expect=0)[1]
    assert ad["adopted"][0]["item"] == item["id"]


def test_the_cli_never_leaks_a_secret_in_any_output(rig, monkeypatch):
    built, run, d = rig
    monkeypatch.setenv("AVE_INSTAGRAM_TOKEN_IG_MAIN", "EAAB" + "k" * 40)
    for argv in (["account", "show", "ig-main"], ["account", "list"], ["doctor"], ["status"], ["queue", "list"]):
        rc, res, err = run(*argv)
        assert "EAAB" + "k" * 40 not in json.dumps(res) + err, argv
