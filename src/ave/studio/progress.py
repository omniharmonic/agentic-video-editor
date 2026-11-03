"""Studio orientation is an inventory of creative work, never an episode cut plan.

These readers use the same derived post state as the studio and never start a job,
claim feedback, refresh credentials, approve, unpause, or send anything.
"""
from __future__ import annotations

from collections import Counter
import shlex

from ..episode import read_json
from . import dispatch, ledger, store
from .service import Studio


def status(sp) -> dict:
    sp.require()
    studio = Studio(sp)
    posts = studio.posts()
    variants = Counter(v['status'] for p in posts for v in p.get('variants', []))
    stages = Counter(p['stage'] for p in posts)
    writing, warnings = [], []
    index = sp.work / 'content/writing.json'
    if index.is_file():
        try:
            writing = read_json(index).get('files', [])
            if not isinstance(writing, list):
                raise ValueError('writing files must be a list')
        except (OSError, ValueError, TypeError, AttributeError):
            warnings.append('Writing index cannot be read; re-ingest the source text.')
    feedback = ledger.open_feedback(sp, now=studio.now())
    from ..platforms.accounts import check_accounts, list_accounts
    accounts = list_accounts(sp)
    if check_accounts(sp):
        warnings.append('Some account configurations are invalid; run ave studio doctor for details.')
    review = sum(v['status'] == 'in_review' or 'approval_expired' in (v.get('flags') or [])
                 for p in posts for v in p.get('variants', []))
    counts = {'sources': len(writing), 'assets': len(store.read_assets(sp)), 'posts': len(posts),
              'ideas': stages['idea'], 'drafts': stages['draft'],
              'review': review, 'feedback': len(feedback), 'accounts': len(accounts),
              'scheduled': variants['scheduled'], 'approved': variants['approved'], 'published': variants['published'],
              'attention': sum(variants[k] for k in ('failed', 'missed', 'needs_rework', 'publishing'))}
    suffix = ['--studio', str(sp.root)]
    def command(*args):
        return shlex.join(['ave', 'studio', *args, *suffix])
    def row(key, title, done, cmd, skill='ave-studio', human=False):
        return {'stage': key, 'title': title, 'done': done, 'skipped': False, 'optional': False,
                'blocked_by': [], 'command': cmd, 'skill': skill, 'human_gate': human}
    rows = [
        row('source', 'Source material available', bool(writing or counts['assets'] or posts),
            command('ingest', 'idea', '<your idea>'), 'ave-studio-ideas'),
        row('ideas', 'Ideas developed into drafts', bool(posts) and not stages['idea'],
            command('post', 'list', '--stage', 'idea'), 'ave-studio-ideas'),
        row('drafts', 'Drafts prepared for review', bool(stages['active'] or stages['archived']) and not stages['draft'] and not stages['idea'],
            command('post', 'list', '--stage', 'draft'), 'ave-studio-visuals'),
        row('review', 'Creator review completed', bool(counts['approved'] or counts['scheduled'] or counts['published']) and not counts['review'],
            command('ui', '--open'), 'ave-studio', True),
        row('feedback', 'Creator feedback addressed', not feedback,
            command('feedback', 'next'), 'ave-studio-review-loop'),
        row('accounts', 'Publishing accounts configured', bool(accounts), command('account', 'list'), 'ave-studio-calendar'),
        row('scheduled', 'Posts scheduled', bool(counts['scheduled']), command('tick'), 'ave-studio-calendar'),
        row('published', 'Published releases recorded', bool(counts['published']), command('status'), 'ave-studio-calendar'),
    ]
    paused = dispatch.is_paused(sp)
    # This is a next-action suggestion, not permission to cross an editorial or
    # publishing checkpoint. Source text and log notes never choose commands.
    if paused:
        nxt = row('paused', 'Review the paused studio', False, command('status'), human=True)
    elif feedback:
        nxt = rows[4]
    elif counts['attention']:
        nxt = row('attention', 'Inspect posts needing attention', False, command('status'), 'ave-studio-review-loop')
    elif counts['review']:
        nxt = rows[3]
    elif not posts and not writing and not counts['assets']:
        nxt = row('source', 'Add source material or an idea', False, command('ingest', 'idea', '<your idea>'), 'ave-studio-ideas')
    elif stages['idea']:
        nxt = row('ideas', 'Develop the waiting ideas', False, command('post', 'list', '--stage', 'idea'), 'ave-studio-ideas')
    elif stages['draft']:
        nxt = row('drafts', 'Prepare drafts for review', False, command('post', 'list', '--stage', 'draft'), 'ave-studio-visuals')
    elif not posts:
        nxt = row('ideas', 'Choose source passages and plan posts', False, command('recipe', 'list'), 'ave-studio-ideas')
    elif counts['scheduled'] or counts['approved']:
        nxt = row('scheduled', 'Rehearse the publishing queue', False, command('tick'), 'ave-studio-calendar')
    else:
        nxt = row('planning', 'Review the content plan and next material', False, command('status'), 'ave-studio-ideas')
    return {'kind': 'studio', 'studio': sp.root.name, 'title': sp.read_meta().get('title') or sp.root.name,
            'path': str(sp.root), 'stages': rows, 'edits': {}, 'next': nxt, 'counts': counts,
            'variants_by_status': dict(variants), 'paused': paused, 'warnings': warnings}


def checklist_block(sp) -> str:
    from .. import progress
    st = status(sp)
    out = [progress.START, '_Generated by `ave progress` from studio files. These are inventory signals, not editorial approvals._', '']
    for r in st['stages']:
        out.append(f"- [{'x' if r['done'] else ' '}] {r['title']} (`{r['stage']}`)" +
                   (' - human checkpoint; approval must be recorded separately' if r['human_gate'] else ''))
    counts = ', '.join(f'{key}: {value}' for key, value in st['counts'].items())
    out.extend(['', f'**Studio inventory:** {counts}', ''])
    if st['paused']:
        out.extend(['**Paused:** Only the human may unpause the studio.', ''])
    n = st['next']
    out.append(f"**Next:** {n['title']}: `{n['command']}` (skill `{n['skill']}`)." +
               (' STOP for the human.' if n['human_gate'] else ''))
    out.extend(['', 'Publishing, live accounts, licenses and sending retain their separate human gates.', progress.END])
    return '\n'.join(out)


def briefing(sp) -> str:
    from .. import progress
    st = status(sp)
    out = [f"Studio: {st['title']}  ({st['path']})", '',
           'Inventory signals describe the work on disk; they do not approve content or authorize sending.']
    for r in st['stages']:
        out.append(f"[{'x' if r['done'] else ' '}] {r['title']}" + (' [human checkpoint]' if r['human_gate'] else ''))
    out.extend(['', 'Counts: ' + ', '.join(f'{key}={value}' for key, value in st['counts'].items())])
    if st['paused']:
        out.append('PAUSED: sending is blocked; only the human may unpause.')
    for w in st['warnings']:
        out.append('Warning: ' + w)
    nxt = st['next']
    out.extend(['', f"NEXT: {nxt['title']}: {nxt['command']}  (skill: {nxt['skill']})." +
                (' STOP for the human.' if nxt['human_gate'] else ''),
                '`ave resume <studio>` is orientation. `ave studio resume` is the separate human-only unpause action.'])
    out.extend(progress.resume_extras(sp))
    return '\n'.join(out)
