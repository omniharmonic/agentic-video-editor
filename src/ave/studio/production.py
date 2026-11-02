"""Editorial production, independent from release approval and delivery.

All graph/config mutations share one workspace flock, then use the post store's
CAS. Comments are append-only events; they never rewrite review feedback.
"""
from __future__ import annotations

import copy
import re
import fcntl
import secrets
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from urllib.parse import quote

from ..episode import read_json, write_json
from . import events, ledger, model, store

STAGES = [('idea', 'Idea'), ('writing', 'Writing'), ('design', 'Design/editing'), ('review', 'Review'), ('ready', 'Ready'), ('blocked', 'Blocked')]
FIELDS = {'title', 'status', 'owner', 'reviewer', 'priority', 'start_date', 'due_date', 'workflow', 'brief', 'sources', 'checklist', 'depends_on', 'estimate_minutes', 'blocked_reason', 'links'}
DEFAULT_WORKFLOW = {'id': 'wf_default', 'name': 'Content production', 'stages': [{'id': k, 'label': v} for k, v in STAGES], 'readiness': {'checklist': True, 'dependencies': True}}


def safe_path(sp, path):
    path = Path(path)
    for candidate in (path, path.with_suffix(path.suffix + '.tmp'), path.with_suffix('.lock')):
        try:
            rel = candidate.relative_to(sp.root)
        except ValueError:
            raise ValueError('production files must stay inside the workspace') from None
        if any(part in (".", "..") for part in rel.parts):
            raise ValueError("production paths must not traverse directories")
        cursor = sp.root
        for part in rel.parts:
            cursor /= part
            if cursor.is_symlink():
                raise ValueError('production files must not be symlinks')
        if candidate.exists() and not candidate.is_file():
            raise ValueError('production sidecars must be regular files')
        if any(candidate == raw or raw in candidate.parents for raw in sp.raw_dirs):
            raise ValueError('production cannot write raw folders')
    return path


@contextmanager
def workspace_lock(sp):
    path = safe_path(sp, sp.work / 'production' / 'workspace.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def config(sp, kind):
    if kind not in ('people', 'workflows', 'production-views'):
        raise ValueError('unknown production policy')
    if sp is None:
        return {'v': 1, 'rev': 0, 'items': [copy.deepcopy(DEFAULT_WORKFLOW)] if kind == 'workflows' else []}
    path = safe_path(sp, sp.work / 'production' / (kind + '.json'))
    return read_json(path) if path.exists() else {'v': 1, 'rev': 0, 'items': [copy.deepcopy(DEFAULT_WORKFLOW)] if kind == 'workflows' else []}


def only(obj, keys):
    if not isinstance(obj, dict) or set(obj) - set(keys):
        raise ValueError('unknown or invalid fields: ' + ', '.join(sorted(set(obj) - set(keys))) if isinstance(obj, dict) else 'fields must be an object')


def text(value, label, limit=2000, required=False):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError(f'{label} must be {"nonempty " if required else ""}text of at most {limit} characters')


def iso_date(value, label):
    if value is None:
        return
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError(f'{label} must be YYYY-MM-DD or null')
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError()
    except ValueError:
        raise ValueError(f'{label} must be a real ISO date') from None


def defaults(post):
    return {'title': '', 'status': 'idea' if post.get('stage') == 'idea' else 'writing', 'owner': None, 'reviewer': None,
            'priority': 'normal', 'start_date': None, 'due_date': None, 'workflow': 'wf_default', 'brief': {}, 'sources': [],
            'checklist': [], 'depends_on': [], 'estimate_minutes': None, 'blocked_reason': '', 'links': [], **copy.deepcopy(post.get('production') or {})}


LINK_TOOLS = ('resolve', 'canva', 'other')


def validate_links(links):
    """Where the work lives: a Resolve timeline by name, a Canva design or any other page by URL."""
    if not isinstance(links, list) or len(links) > 12: raise ValueError('links must be a list of at most 12')
    for link in links:
        if not isinstance(link, dict): raise ValueError('A link must be an object')
        only(link, {'tool', 'label', 'url'})
        if link.get('tool') not in LINK_TOOLS: raise ValueError('link tool must be resolve, canva or other')
        text(link.get('label'), 'link label', 200)
        url = link.get('url')
        if url is not None and not re.fullmatch(r'https://[^\s]{1,2000}', str(url)): raise ValueError('A link URL must be https')
        if link['tool'] != 'resolve' and not url: raise ValueError('This link needs a URL')


def validate_shape(p):
    only(p, FIELDS)
    if 'links' in p: validate_links(p['links'])
    for k in ('start_date', 'due_date'):
        iso_date(p.get(k), k)
    if p.get('start_date') and p.get('due_date') and p['start_date'] > p['due_date']:
        raise ValueError('start_date must be on or before due_date')
    for k, limit in [('title', 300), ('blocked_reason', 2000)]:
        if k in p: text(p[k], k, limit)
    if p.get('priority', 'normal') not in ('low', 'normal', 'high', 'urgent'):
        raise ValueError('priority must be low, normal, high or urgent')
    for k in ('status', 'workflow', 'owner', 'reviewer'):
        if p.get(k) is not None: model.check_id(p[k], k)
    if p.get('status') == 'published': raise ValueError('Published is delivery, not a production stage')
    if p.get('status') == 'blocked' and not p.get('blocked_reason', '').strip():
        raise ValueError('Blocked needs a reason')
    n = p.get('estimate_minutes')
    if n is not None and (type(n) is not int or not 0 <= n <= 100000):
        raise ValueError('estimate_minutes must be an integer from 0 to 100000 or null')
    brief = p.get('brief', {})
    only(brief, {'audience', 'objective', 'message', 'cta'})
    for k, v in brief.items(): text(v, k, 10000)
    for key, limit in [('checklist', 100), ('sources', 50), ('depends_on', 100)]:
        if not isinstance(p.get(key, []), list) or len(p.get(key, [])) > limit:
            raise ValueError(f'{key} must contain at most {limit} items')
    ids = []
    for t in p.get('checklist', []):
        only(t, {'id', 'title', 'owner', 'done', 'due_date'})
        model.check_id(t.get('id'), 'task id'); ids.append(t['id'])
        text(t.get('title'), 'task title', 300, True)
        if type(t.get('done')) is not bool: raise ValueError('task done must be boolean')
        if t.get('owner') is not None: model.check_id(t['owner'], 'task owner')
        iso_date(t.get('due_date'), 'task due_date')
    if len(ids) != len(set(ids)): raise ValueError('duplicate task ids')
    deps = p.get('depends_on', [])
    for d in deps: model.check_id(d, 'dependency')
    if len(deps) != len(set(deps)): raise ValueError('duplicate dependencies')
    for s in p.get('sources', []):
        only(s, {'source_id', 'label', 'locator'})
        text(s.get('source_id'), 'source id', 1000, True)
        text(s.get('label', ''), 'source label', 300)
        loc = s.get('locator', {})
        only(loc, {'start', 'end'})
        if loc and (set(loc) != {'start', 'end'} or any(type(v) not in (int, float) or not 0 <= v <= 1e9 for v in loc.values()) or loc['end'] < loc['start']):
            raise ValueError('source locator requires ordered nonnegative start/end')


def source_rows(sp):
    from .workbench import inventory
    return [{k: v for k, v in s.items() if k in ('source_id', 'label', 'kind', 'duration','production_reference','previews','asset','sha256')} for s in inventory(sp)]


def validate(sp, post, p):
    validate_shape(p)
    workflow = next((w for w in config(sp, 'workflows')['items'] if w['id'] == p['workflow']), None)
    if workflow is None: raise ValueError('Choose an existing workflow')
    if p['status'] not in [s['id'] for s in workflow['stages']]: raise ValueError('Choose a stage in this workflow')
    people = {x['id'] for x in config(sp, 'people')['items']}
    for person in [p['owner'], p['reviewer']] + [t.get('owner') for t in p['checklist']]:
        if person is not None and person not in people: raise ValueError('Choose a person in this workspace')
    sources = {s['source_id']: s for s in source_rows(sp)}
    for ref in p['sources']:
        if ref['source_id'] not in sources: raise ValueError('Source must exist in the workspace source inventory')
        src = sources[ref['source_id']]
        if ref.get('locator'):
            if src.get('kind') not in ('writing','document') and (not src.get('duration') or ref['locator']['end'] > src['duration']):
                raise ValueError('Time locator exceeds the imported recording duration')
    graph = {x['id']: defaults(x) for x in store.list_posts(sp)}
    graph[post['id']] = p
    if post['id'] in p['depends_on']: raise ValueError('Work cannot depend on itself')
    if any(d not in graph for d in p['depends_on']): raise ValueError('Dependency does not exist')
    visiting, done = set(), set()
    def visit(pid):
        if pid in visiting: raise ValueError('Dependency would create a cycle')
        if pid in done: return
        visiting.add(pid)
        for dep in graph.get(pid, {}).get('depends_on', []): visit(dep)
        visiting.remove(pid); done.add(pid)
    for pid in graph: visit(pid)
    if p['status'] == 'ready':
        blockers = []
        if workflow['readiness']['checklist']:
            blockers += ['Complete task: ' + t['title'] for t in p['checklist'] if not t['done']]
        if workflow['readiness']['dependencies']:
            blockers += ['Finish dependency: ' + (graph[d].get('title') or d) for d in p['depends_on'] if graph[d]['status'] != 'ready']
        if blockers: raise ValueError('Ready is blocked. ' + '; '.join(blockers))


def update(studio, pid, fields, *, expected_rev, actor=events.AGENT, _commit_context=None):
    only(fields, FIELDS)
    if not fields: raise ValueError('Provide at least one production field')
    with workspace_lock(studio.sp):
        p = store.load_post(studio.sp, pid)
        if type(expected_rev) is not int or p['rev'] != expected_rev: raise model.RevConflict(studio.view(p))
        data = defaults(p); data.update(copy.deepcopy(fields)); validate(studio.sp, p, data)
        p['production'] = data
        saved = store.save_post(studio.sp, p, expected_rev=expected_rev, actor=actor, op='production.edit', detail={'fields': sorted(fields)}, now=studio.now(),_commit_context=_commit_context)
    return project(studio, saved)


def task(studio, pid, op, task, *, expected_rev, actor=events.AGENT):
    # CAS keeps the read outside the graph lock safe: update rejects any intervening edit.
    p = store.load_post(studio.sp, pid); tasks = defaults(p)['checklist']
    only(task, {'id', 'title', 'owner', 'done', 'due_date'})
    if op == 'add':
        tasks.append({'id': 'task_' + secrets.token_hex(6), 'title': '', 'owner': None, 'due_date': None, 'done': False, **task})
    elif op in ('edit', 'remove'):
        hit = next((t for t in tasks if t['id'] == task.get('id')), None)
        if hit is None: raise ValueError('Task no longer exists')
        if op == 'remove': tasks.remove(hit)
        else: hit.update(task)
    else: raise ValueError('task op must be add, edit or remove')
    return update(studio, pid, {'checklist': tasks}, expected_rev=expected_rev, actor=actor)


def save_config(studio, kind, item, *, expected_rev, actor=events.AGENT):
    with workspace_lock(studio.sp):
        safe_path(studio.sp, ledger.events_path(studio.sp))
        safe_path(studio.sp, studio.sp.ledger_dir / '.lock')
        doc = config(studio.sp, kind)
        if type(expected_rev) is not int or doc['rev'] != expected_rev: raise model.RevConflict(doc)
        item = copy.deepcopy(item)
        if kind == 'people':
            only(item, {'id', 'name', 'local_role_label'})
            text(item.get('name'), 'name', 100, True); text(item.get('local_role_label', ''), 'role label', 100)
        elif kind == 'workflows':
            only(item, {'id', 'name', 'stages', 'readiness'})
            text(item.get('name'), 'workflow name', 100, True)
            stages = item.get('stages')
            if not isinstance(stages, list) or not 2 <= len(stages) <= 20: raise ValueError('Workflow needs 2–20 stages')
            for st in stages:
                only(st, {'id', 'label'}); model.check_id(st.get('id'), 'stage id'); text(st.get('label'), 'stage label', 60, True)
            ids = [s['id'] for s in stages]
            if len(set(ids)) != len(ids) or 'ready' not in ids or 'blocked' not in ids or 'published' in ids: raise ValueError('Unique stages must include ready and blocked; Published is delivery')
            only(item.get('readiness'), {'checklist', 'dependencies'})
            if set(item['readiness']) != {'checklist', 'dependencies'} or any(type(v) is not bool for v in item['readiness'].values()): raise ValueError('Readiness criteria must be booleans')
            for p in store.list_posts(studio.sp):
                prod = defaults(p)
                if prod['workflow'] == item.get('id') and prod['status'] not in ids:
                    raise ValueError('Move existing work to a retained stage before retiring this stage')
        else:
            only(item, {'id', 'name', 'filters', 'group', 'sort', 'mode'})
            text(item.get('name'), 'view name', 100, True)
            only(item.get('filters', {}), {'q', 'owner', 'reviewer', 'status', 'priority', 'campaign', 'content_type', 'overdue', 'from', 'to'})
            for k, v in item.get('filters', {}).items(): text(v, k, 300)
            if item.get('group') not in ('status', 'campaign', 'owner'): raise ValueError('Invalid grouping')
            if item.get('sort') not in ('title', 'due_date', 'priority'): raise ValueError('Invalid sort')
            if item.get('mode') not in ('table', 'board', 'timeline'): raise ValueError('Invalid view mode')
        item.setdefault('id', ('person_' if kind == 'people' else 'wf_' if kind == 'workflows' else 'view_') + secrets.token_hex(6))
        model.check_id(item['id'])
        if any(x['name'].casefold() == item['name'].strip().casefold() and x['id'] != item['id'] for x in doc['items']): raise ValueError('That name is already in use')
        if len(doc['items']) >= 200 and not any(x['id'] == item['id'] for x in doc['items']): raise ValueError('Workspace limit is 200 definitions')
        doc['items'] = [x for x in doc['items'] if x['id'] != item['id']] + [item]; doc['rev'] += 1
        write_json(safe_path(studio.sp, studio.sp.work / 'production' / (kind + '.json')), doc)
        events.append_event(studio.sp, events.make_event(actor=actor, op=kind + '.edit', detail={'id': item['id'], 'rev': doc['rev']}, now=studio.now()))
        return doc


def comments(studio, pid):
    store.load_post(studio.sp, pid)
    rows, _ = ledger.read_jsonl(safe_path(studio.sp, ledger.events_path(studio.sp)))
    out = {}
    for e in rows:
        if e.get('post') != pid: continue
        d = e.get('detail', {})
        if e['op'] in ('comment.add', 'comment.reply'):
            out[d['id']] = {**d, 'post': pid, 'actor': e['actor'], 'at': e['ts'], 'resolved': False}
        elif e['op'] == 'comment.resolve' and d.get('id') in out:
            out[d['id']]['resolved'] = d['resolved']
    return list(out.values())


def comment(studio, pid, *, expected_rev, text_value=None, reply_to=None, anchor=None, comment_id=None, resolved=None, actor=events.AGENT):
    with workspace_lock(studio.sp):
        p = store.load_post(studio.sp, pid)
        if type(expected_rev) is not int or p['rev'] != expected_rev: raise model.RevConflict(studio.view(p))
        store.check_write_paths(studio.sp, pid)
        existing = {c['id']: c for c in comments(studio, pid)}
        if comment_id:
            if comment_id not in existing or type(resolved) is not bool: raise ValueError('Choose an existing comment and boolean resolved')
            detail, op = {'id': comment_id, 'resolved': resolved}, 'comment.resolve'
        else:
            text(text_value, 'comment', 5000, True)
            if reply_to and reply_to not in existing: raise ValueError('Reply target does not exist')
            if anchor is not None:
                only(anchor, {'task', 'media', 'revision'})
                if anchor.get('task') and anchor['task'] not in {t['id'] for t in defaults(p)['checklist']}: raise ValueError('Task anchor does not exist')
                if anchor.get('media') and anchor['media'] not in model.media_asset_ids(p): raise ValueError('Media anchor does not exist')
                if 'revision' in anchor and (type(anchor['revision']) is not int or not 1 <= anchor['revision'] <= p['rev']): raise ValueError('Invalid revision anchor')
            if len(existing) >= 1000: raise ValueError('Discussion limit reached')
            detail, op = {'id': 'comment_' + secrets.token_hex(8), 'text': text_value, 'reply_to': reply_to, 'anchor': anchor}, 'comment.reply' if reply_to else 'comment.add'
        events.append_event(studio.sp, events.make_event(actor=actor, op=op, post=pid, rev=p['rev'], detail=detail, now=studio.now()))
        return {'comments': comments(studio, pid), 'rev': p['rev']}


def project(studio, post, *, assets=None):
    from . import campaigns
    p = studio.view(post)
    p['production'] = defaults(post)
    p['title'] = p['production']['title'] or (p.get('idea') or {}).get('text') or p['copy'].get('base') or 'Untitled item'
    assets = assets if assets is not None else store.read_assets(studio.sp)
    variant = next((v for v in p['variants'] if not v.get('dropped')), {'media': None})
    media = sorted(model.effective_media(p, variant), key=lambda m: m.get('slot', 0))
    cover = next((m for m in media if m.get('role') == 'cover'), None)
    chosen = cover or next(iter(media), {})
    asset = assets.get(chosen.get('asset'), {})
    url = None
    if asset.get('file'):
        path = studio.sp.root / asset['file']
        try:
            safe_path(studio.sp, path)
            if path.is_file(): url = '/files/' + quote(asset['file'], safe='/')
        except ValueError: pass
    p['thumbnail'] = {'url': url, 'kind': asset.get('kind') if url else 'missing' if media else 'text', 'w': asset.get('w'), 'h': asset.get('h'), 'alt': chosen.get('alt') or 'Content preview'}
    p['campaign_label'] = next((c['name'] for c in campaigns.list_all(studio.sp) if c['id'] == p.get('campaign')), p.get('campaign'))
    p['review_feedback'] = ledger.open_feedback(studio.sp, post=p['id'], now=studio.now())
    return p


def inventory(studio, **filters):
    if studio.sp is None:
        rows = []
        # FixtureStudio serves the HTTP envelope; live Studio.posts returns rows.
        fixture_posts = studio.posts()
        for raw in fixture_posts['posts']:
            p = copy.deepcopy(raw)
            p.update(production=defaults(raw), title=(raw.get('idea') or {}).get('text') or raw.get('copy', {}).get('base') or 'Untitled item',
                     thumbnail={'kind': 'text', 'url': None}, review_feedback=[], campaign_label=raw.get('campaign'))
            rows.append(p)
        return {'posts': rows, 'total': len(rows), 'filtered': len(rows), 'sources': [], 'read_only': True}
    assets = store.read_assets(studio.sp)
    rows = [project(studio, p, assets=assets) for p in store.list_posts(studio.sp) if p['stage'] != 'dropped']
    total = len(rows)
    for key, value in filters.items():
        if not value: continue
        if key in ('owner', 'reviewer', 'status', 'priority'): rows = [p for p in rows if p['production'][key] == value]
        elif key == 'campaign': rows = [p for p in rows if p.get(key) == value]
        elif key == 'q': rows = [p for p in rows if value.lower() in (p['title'] + ' ' + p['copy'].get('base', '')).lower()]
    return {'posts': rows, 'total': total, 'filtered': len(rows), 'sources': source_rows(studio.sp)}


PORTABLE_FIELDS = {'title', 'priority', 'start_date', 'due_date', 'brief', 'checklist', 'estimate_minutes'}


def portable(data):
    """Peer-safe editorial projection. Local person, workflow, source and graph IDs
    are intentionally not portable; an import never guesses an identity mapping.
    Discussion actor/event IDs remain in the originating workspace's event log.
    """
    result = {k: copy.deepcopy(v) for k, v in data.items() if k in PORTABLE_FIELDS}
    for task in result.get('checklist', []):
        task['owner'] = None
    validate_shape(result)
    return result
