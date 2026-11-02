"""Schedule group: schedule show|rules|plan|apply, move, unschedule.

A proposal (`schedule plan`) carries no authority; ACCEPTING one is a human act (`schedule apply`). An agent's retime of an already-approved,
already-scheduled post is turned into a proposal by the move code itself (policy `approval.agent_retime_approved`).
"""

from __future__ import annotations

import getpass
from datetime import timedelta

import yaml

from ...schedule import proposals, rules as rules_mod, slots
from .. import campaigns, model, policy, store
from ..clock import iso_utc
from ..events import Actor
from . import Blocked, add_dir, actor_for, csv, guarded, human_gate, out, project, studio


def _today(s):
    z = slots.ZoneInfo(s.sp.config().get("tz") or "UTC")
    return s.now().astimezone(z).date()


@guarded
def cmd_show(a) -> int:
    s = studio(a, adapters=False)
    start = slots.date.fromisoformat(a.from_) if a.from_ else _today(s)
    end = slots.date.fromisoformat(a.to) if a.to else start + timedelta(days=13)
    cal = s.calendar(start, end, accounts=csv(a.account) or None)
    out({"ok": True, "from": start.isoformat(), "to": end.isoformat(), **cal})
    return 0


def _parse_rule(spec: str) -> dict:
    """`id=r1;dow=mon,wed,fri;time=09:00;post_types=feed,carousel`"""
    kv = dict(part.split("=", 1) for part in spec.split(";") if "=" in part)
    unknown = set(kv) - {"id", "dow", "time", "post_types"}
    if unknown or not {"id", "dow", "time"} <= set(kv):
        raise ValueError(f"--rule needs id=..;dow=mon,wed;time=HH:MM[;post_types=feed,carousel] (got {spec!r})")
    rule = {"id": kv["id"], "dow": csv(kv["dow"]), "time": kv["time"]}
    if kv.get("post_types"):
        rule["post_types"] = csv(kv["post_types"])
    return rule


@guarded
def cmd_rules_set(a) -> int:
    sp = project(a)
    if not policy.account_config(sp, a.account).get("id"):
        raise KeyError(f"no account {a.account!r}: `ave studio account add`")
    if a.file:
        data = yaml.safe_load(open(a.file).read()) or {}
    else:
        data = {"rules": [_parse_rule(r) for r in a.rule or []], "overrides": []}
    if not data.get("rules") and not a.clear:
        raise ValueError("give at least one --rule (or --file), or --clear to remove all rules")
    for d in a.skip_date or []:
        data.setdefault("overrides", []).append({"date": d, "skip": True})
    data = {"tz": a.tz or data.get("tz") or rules_mod.account_tz(sp, a.account), "rules": data.get("rules") or [], "overrides": data.get("overrides") or []}
    problems = model.validate("schedule_rules", data)
    if problems:
        raise ValueError("; ".join(problems[:5]))
    p = rules_mod.rules_path(sp, a.account)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(data, sort_keys=False))
    out({"ok": True, "account": a.account, "rules": rules_mod.load_rules(sp, a.account)})
    return 0


@guarded
def cmd_rules_show(a) -> int:
    sp = project(a)
    out({"ok": True, "account": a.account, "rules": rules_mod.load_rules(sp, a.account)})
    return 0


@guarded
def cmd_plan(a) -> int:
    s = studio(a, adapters=False)
    res = campaigns.plan(s, a.campaign, weeks=a.weeks, by=a.by or "claude")
    out({"ok": True, **res})
    return 0 if res["proposal"] and not res["unplaced"] else 2


@guarded
def cmd_apply(a) -> int:
    human_gate(a, "accepting a schedule proposal", f"ave studio schedule apply {a.proposal}")
    s = studio(a, adapters=True)
    res = s.accept_proposal(a.proposal, actor=Actor("human", a.by or getpass.getuser()))
    out({"ok": not res["failed"], **res})
    return 0 if not res["failed"] else 2


@guarded
def cmd_move(a) -> int:
    s = studio(a, adapters=True)
    post = store.load_post(s.sp, a.post)
    model.find_variant(post, a.variant)
    tz = a.tz or rules_mod.account_tz(s.sp, model.find_variant(post, a.variant)["account"])
    to = {"local": a.to, "tz": tz}
    res = s.move(a.post, a.variant, to, expected_rev=a.expected_rev if a.expected_rev is not None else post["rev"], actor=actor_for(False, a.by), snap=a.snap)
    applied = res.get("applied", True)
    out({"ok": True, "applied": applied, "warnings": res.get("warnings", []), "schedule": next(v for v in res["post"]["variants"] if v["id"] == a.variant).get("schedule"),
         **({"proposal": res.get("proposal")} if res.get("proposal") else {}), "rev": res["post"]["rev"]})
    return 0


@guarded
def cmd_unschedule(a) -> int:
    s = studio(a, adapters=True)
    post = store.load_post(s.sp, a.post)
    res = s.unschedule(a.post, a.variant, expected_rev=a.expected_rev if a.expected_rev is not None else post["rev"], actor=actor_for(False, a.by))
    out({"ok": True, "warnings": res.get("warnings", []), "rev": res["post"]["rev"]})
    return 0


def add(ss) -> None:
    sch = ss.add_parser("schedule", help="schedule: show, rules, plan, apply (human)")
    sub = sch.add_subparsers(dest="schedule_cmd", required=True)
    p = sub.add_parser("show", help="the calendar JSON for a date range")
    p.add_argument("--from", dest="from_")
    p.add_argument("--to")
    p.add_argument("--account", help="comma list")
    add_dir(p)
    p.set_defaults(fn=cmd_show)
    r = sub.add_parser("rules", help="recurring slot rules: set, show")
    rs = r.add_subparsers(dest="rules_cmd", required=True)
    p = rs.add_parser("set")
    p.add_argument("account")
    p.add_argument("--rule", action="append", metavar="SPEC", help="id=r1;dow=mon,wed,fri;time=09:00[;post_types=feed,carousel] (repeatable)")
    p.add_argument("--file", help="a rules YAML file instead")
    p.add_argument("--skip-date", action="append", metavar="YYYY-MM-DD")
    p.add_argument("--tz")
    p.add_argument("--clear", action="store_true")
    add_dir(p)
    p.set_defaults(fn=cmd_rules_set)
    p = rs.add_parser("show")
    p.add_argument("account")
    add_dir(p)
    p.set_defaults(fn=cmd_rules_show)
    p = sub.add_parser("plan", help="propose slots for a campaign's unscheduled variants (writes a proposal; applies nothing)")
    p.add_argument("--campaign", required=True)
    p.add_argument("--weeks", type=int, default=2)
    p.add_argument("--by")
    add_dir(p)
    p.set_defaults(fn=cmd_plan)
    p = sub.add_parser("apply", help="HUMAN: accept a proposal (its moves are applied as the human's)")
    p.add_argument("proposal")
    p.add_argument("--by")
    add_dir(p)
    p.set_defaults(fn=cmd_apply)

    p = ss.add_parser("move", help="retime a variant (local time in the account's zone)")
    p.add_argument("post")
    p.add_argument("variant")
    p.add_argument("--to", required=True, help="local time, e.g. 2026-11-03T09:00")
    p.add_argument("--tz", help="IANA zone (default: the account's)")
    p.add_argument("--snap", choices=["slot", "none"], default="slot")
    p.add_argument("--expected-rev", type=int)
    p.add_argument("--by")
    add_dir(p)
    p.set_defaults(fn=cmd_move)
    p = ss.add_parser("unschedule", help="take a variant off the calendar (back to the tray)")
    p.add_argument("post")
    p.add_argument("variant")
    p.add_argument("--expected-rev", type=int)
    p.add_argument("--by")
    add_dir(p)
    p.set_defaults(fn=cmd_unschedule)
