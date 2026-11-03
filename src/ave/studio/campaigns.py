"""Campaigns (spec 2.3): `campaigns/<id>.json`, a planning envelope only. It carries no approval or send authority.

    new(sp, name, ...)       create (id = c_<slug of name>)
    load / list_all
    add_post(sp, id, post)   record membership on BOTH sides (campaign.posts and post.campaign), under the post's compare-and-swap
    progress(sp, id)         cadence per week vs. posts actually scheduled in the window
    plan(studio, id, weeks)  a schedule proposal that places the campaign's unscheduled variants into the account's free slots (never applies it)
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path

from ..episode import read_json, write_json
from . import model, store
from .clock import iso_utc, parse_ts
from .events import AGENT, Actor


def path_for(sp, campaign_id: str) -> Path:
    return sp.campaigns_dir / f"{model.check_id(campaign_id, 'campaign id')}.json"


def new_id(name: str) -> str:
    return "c_" + (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:50] or "campaign")


def _read(sp, campaign_id):
    from .production import safe_path
    p = safe_path(sp, path_for(sp, campaign_id))
    if not p.is_file():
        raise FileNotFoundError(f"no campaign {campaign_id!r}")
    c = read_json(p)
    c.setdefault("rev", 1)
    c.setdefault("milestones", [])
    # Membership has one authoritative domain, including legacy cached records.
    c["posts"] = [p["id"] for p in store.list_posts(sp, campaign=campaign_id)]
    return c


def load(sp, campaign_id):
    from .production import workspace_lock
    with workspace_lock(sp):
        _recover(sp)
        return _read(sp, campaign_id)


def list_all(sp):
    from .production import workspace_lock
    with workspace_lock(sp):
        _recover(sp)
        return [_read(sp, p.stem) for p in sorted(sp.campaigns_dir.glob("c_*.json"))] if sp.campaigns_dir.is_dir() else []


def _validate(sp, c):
    from .production import only, text, iso_date
    for k, limit in (("name", 200), ("goal", 2000), ("brief", 10000)):
        text(c.get(k, ""), k, limit, k == "name")
    window = c.get("window")
    if window is not None:
        only(window, {"start", "end"})
        if not window.get("start") or not window.get("end"): raise ValueError("Set both campaign dates")
        iso_date(window["start"], "start"); iso_date(window["end"], "end")
        if window["start"] > window["end"]: raise ValueError("Campaign start must be before end")
    for key in ("accounts", "themes", "milestones"):
        if not isinstance(c.get(key, []), list) or len(c.get(key, [])) > 100: raise ValueError(key + " allows at most 100 entries")
    for a in c.get("accounts", []):
        model.check_id(a, "account")
        if not (sp.accounts_dir / (a + '.yaml')).is_file(): raise ValueError("Choose an existing account")
    for t in c.get("themes", []): text(t, 'theme', 100, True)
    ids = []
    for m in c.get("milestones", []):
        only(m, {"id", "title", "date", "done"})
        model.check_id(m.get("id"), 'milestone id'); ids.append(m['id'])
        text(m.get('title'), 'milestone title', 300, True); iso_date(m.get('date'), 'milestone date')
        if type(m.get('done')) is not bool: raise ValueError('milestone done must be boolean')
    if len(ids) != len(set(ids)): raise ValueError('duplicate milestone ids')
    if any(k not in c.get('accounts', []) for k in c.get('cadence', {})): raise ValueError('Cadence must reference campaign accounts')
    for old in [_read(sp, p.stem) for p in sp.campaigns_dir.glob('c_*.json')]:
        if old['id'] != c['id'] and old['name'].casefold() == c['name'].strip().casefold(): raise ValueError('Campaign name already exists')
    model.require_valid('campaign', c)


def _write(sp, c):
    from .production import safe_path
    safe_path(sp, sp.ledger_dir / 'events.jsonl')
    safe_path(sp, sp.ledger_dir / '.lock')
    sp.campaigns_dir.mkdir(parents=True, exist_ok=True)
    write_json(safe_path(sp, path_for(sp, c['id'])), c)


def new(sp, name, *, goal='', start=None, end=None, accounts=(), per_week=None, themes=(), brief='', now=None):
    from .production import workspace_lock
    with workspace_lock(sp):
        _recover(sp)
        cid = new_id(name)
        if path_for(sp, cid).exists(): raise ValueError('Campaign name already exists')
        c = {'v': 1, 'id': cid, 'rev': 1, 'name': name.strip(), 'goal': goal, 'status': 'planned',
             'accounts': list(dict.fromkeys(list(accounts) + list((per_week or {}).keys()))), 'cadence': {a: {'per_week': n} for a, n in (per_week or {}).items()},
             'themes': list(themes), 'brief': brief, 'posts': [], 'milestones': [],
             'created': iso_utc(now or datetime.now().astimezone())}
        if start or end: c['window'] = {'start': start, 'end': end}
        _validate(sp, c); _write(sp, c)
        return c


def edit(sp, cid, fields, *, expected_rev, actor=AGENT, now=None):
    from .production import only, workspace_lock
    from .events import append_event, make_event
    only(fields, {'name', 'goal', 'brief', 'window', 'accounts', 'themes', 'cadence', 'status', 'milestones'})
    with workspace_lock(sp):
        _recover(sp); c = _read(sp, cid)
        if type(expected_rev) is not int or c['rev'] != expected_rev: raise model.RevConflict(c)
        c.update(fields)
        if c.get('window') is None: c.pop('window', None)
        c['rev'] += 1
        _validate(sp, c); _write(sp, c)
        append_event(sp, make_event(actor=actor, op='campaign.edit', detail={'campaign': cid, 'rev': c['rev']}, now=now))
        return c


def _journal(sp):
    from .production import safe_path
    return safe_path(sp, sp.work / 'production' / 'campaign-members.json')


def _recover(sp):
    """Finish a bounded membership intent after interruption. Only campaign fields
    are replayed onto current post revisions; unrelated edits are preserved."""
    path = _journal(sp)
    if not path.exists(): return
    intent = read_json(path)
    for pid, cid in intent['members'].items():
        post = store.load_post(sp, pid)
        if post.get('campaign') != cid:
            post['campaign'] = cid
            store.save_post(sp, post, expected_rev=post['rev'], actor=Actor(**intent['actor']), op='campaign.members', detail={'campaign': cid})
    c = _read(sp, intent['campaign'])
    c['rev'] = max(c['rev'], intent['rev'])
    _write(sp, c)
    path.unlink()


def members(sp, cid, *, expected_rev, post_revs, add=(), remove=(), actor=AGENT):
    from .production import workspace_lock
    if not isinstance(add, list) or not isinstance(remove, list) or len(add) + len(remove) > 200 or set(add) & set(remove):
        raise ValueError('Membership needs distinct add/remove lists, at most 200 posts')
    if not isinstance(post_revs, dict) or set(post_revs) != set(add + remove): raise ValueError('Supply revisions for every selected post')
    with workspace_lock(sp):
        _recover(sp); c = _read(sp, cid)
        if type(expected_rev) is not int or c['rev'] != expected_rev: raise model.RevConflict(c)
        changes = {}
        for pid in add + remove:
            p = store.load_post(sp, pid)
            if type(post_revs[pid]) is not int or p['rev'] != post_revs[pid]: raise model.RevConflict(p)
            if pid in add: changes[pid] = cid
            elif p.get('campaign') == cid: changes[pid] = None
        write_json(_journal(sp), {'campaign': cid, 'rev': c['rev'] + 1, 'members': changes, 'actor': actor.as_dict()})
        _recover(sp)
        return _read(sp, cid)


def add_post(sp, campaign_id, post_id, *, actor=AGENT, now=None):
    c = load(sp, campaign_id); p = store.load_post(sp, post_id)
    return members(sp, campaign_id, expected_rev=c['rev'], post_revs={post_id: p['rev']}, add=[post_id], remove=[], actor=actor)


def progress(sp, campaign_id: str) -> dict:
    c = load(sp, campaign_id)
    posts = [p for p in store.list_posts(sp, campaign=campaign_id)]
    by_account: dict[str, dict] = {}
    for p in posts:
        for v in p.get("variants") or []:
            if v.get("dropped"):
                continue
            row = by_account.setdefault(v["account"], {"variants": 0, "scheduled": 0})
            row["variants"] += 1
            if (v.get("schedule") or {}).get("utc"):
                row["scheduled"] += 1
    weeks = None
    if c.get("window"):
        d0, d1 = date.fromisoformat(c["window"]["start"]), date.fromisoformat(c["window"]["end"])
        weeks = max(1, round(((d1 - d0).days + 1) / 7))
    cadence = {a: {"per_week": v.get("per_week"), "planned_total": (v.get("per_week") or 0) * weeks if weeks else None,
                   **by_account.get(a, {"variants": 0, "scheduled": 0})} for a, v in (c.get("cadence") or {}).items()}
    return {"campaign": c, "posts": [p["id"] for p in posts], "cadence": cadence, "other_accounts": {a: r for a, r in by_account.items() if a not in cadence}}


def plan(studio, campaign_id: str, *, weeks: int = 2, by: str = "claude") -> dict:
    """Place the campaign's variants that have no time yet into each account's next free rule slots (capped by cadence.per_week), as ONE
    proposal file. A proposal changes nothing until a human accepts it (`ave studio schedule apply`)."""
    from ..schedule import proposals, slots
    sp, now = studio.sp, studio.now()
    c = load(sp, campaign_id)
    end = now + timedelta(weeks=weeks)
    moves, unplaced = [], []
    per_acct_week: dict[tuple[str, str], int] = {}
    pending = [(p, v) for p in store.list_posts(sp, campaign=campaign_id) for v in p.get("variants") or []
               if not v.get("dropped") and not (v.get("schedule") or {}).get("utc")]
    cursor: dict[str, datetime] = {}
    for p, v in sorted(pending, key=lambda t: (t[0]["id"], t[1]["id"])):
        acct = v["account"]
        cap = ((c.get("cadence") or {}).get(acct) or {}).get("per_week")
        after = cursor.get(acct, now)
        placed = None
        for _ in range(200):
            s = slots.next_slot(sp, acct, after=after, post_type=v["post_type"], now=now)
            if s is None:
                break
            utc = parse_ts(s["utc"])
            if utc > end:
                break
            wk = (acct, utc.strftime("%G-W%V"))
            if cap is not None and per_acct_week.get(wk, 0) >= cap:
                after = utc
                continue
            placed = (s, utc, wk)
            break
        if placed is None:
            unplaced.append({"post": p["id"], "variant": v["id"], "why": "no free slot in the window (add schedule rules: `ave studio schedule rules set`)"})
            continue
        s, utc, wk = placed
        per_acct_week[wk] = per_acct_week.get(wk, 0) + 1
        cursor[acct] = utc
        moves.append({"post": p["id"], "variant": v["id"], "to": {"local": s["local"], "tz": s["tz"]},
                      "why": f"next free slot for {acct} (rule {s['rule']}), campaign {campaign_id}"})
    pid = proposals.create(sp, by, moves, now=now) if moves else None
    return {"proposal": pid, "moves": moves, "unplaced": unplaced,
            "next": f"A human reviews the ghost cards in `ave studio ui`, or runs `ave studio schedule apply {pid}`." if pid else "nothing to place"}
