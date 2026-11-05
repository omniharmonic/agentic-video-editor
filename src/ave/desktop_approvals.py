"""The app's approval sheets: publishing approval (one post or several at once), trim approval, and the smaller protected acts
the Workspace and Settings hand to the app (acknowledging an alert, picking B-roll, accepting Claude's suggested moves).

The web view only names what is to be done: the exact `{post, variant, sha}` of each post the approval sheet showed, a
project and one of its sessions, an alert, a B-roll search and candidate, or a proposal. It can never supply a PIN, a name
for an approval or an authority. Everything else happens in AppKit (desktop_native.py): the exact words are read again here,
from the server's own state, and shown in a native sheet; then the existing macOS device-owner check runs
(desktop_auth.NativeAuthority). That check mints one single-use grant bound to this act and exactly these targets, and only
the native code that asked spends it (`authority.use`). For approvals the per-run PIN, shown only in AppKit, is typed back as
before.

The person's own board writes come through here too (`boardWrite`): a stage move, a field, an update, a new artifact, an
answer, a reopen, a keep / maybe / skip or a select nudge. Attribution follows the channel: the same request over HTTP holds only
the room token, which Claude's MCP holds too, so the server records it as Claude's (relayed). Through the app it is the person's,
written with `via="app"`. Confirming what Claude reported keeps its own sheet (`confirmRelayed`), and an answer that changes a
shared settings file (a settings conflict) takes the device-owner check (`answerDecision`), like every other act that changes
authority. Handing over publishing and removing a member (`sharing_grant`) take that check too.

This module is the part around that, kept free of AppKit so it can be tested. An agent marker refuses every step, the grant
must be for this act and these targets, and the work itself is the existing logic, unchanged: `StudioApp.approve` (the same
checks `ave studio approve` and the old Review page passed through, with per-post results) for posts, `trim.confirm` (what
`ave trim set` runs) for a trim, and the studio's own routes for the rest. Nothing here sends anything: an approved post
waits for its slot on the publishing computer. `ave sequence approve` stays a terminal act and has no sheet.

The view the studio requires before approving (2 seconds on the exact item) is recorded from how long the native sheet showed
the exact words and hashes, with `detail.via = "native-sheet"`; the web sheet also holds its button until the files have been
on screen for 2 seconds, but that part is the page's own and is not proof.
"""
from __future__ import annotations

import re

from .humanpresence import agent_markers_set

PIN_ACTIONS = {'approvePublish', 'approveTrim'}
PROTECTED_ACTIONS = {'ackAlert', 'pickBroll', 'acceptProposal', 'confirmRelayed', 'approveContent', 'clearTrim'}
APP_ACTIONS = {'undoRelayed', 'rejectContent', 'rejectEpisodePost', 'boardWrite'}   # recorded as the person only through the app
SHARING_GRANTS = {'handOverPublishing', 'removeMember'}   # they change who publishes or who is on the project: the device-owner check
BOARD_PATH = re.compile(r'/api/workspace/artifact(?:/(?P<key>[A-Za-z0-9_%.:-]{1,240}))?|/api/room/selects/(?P<sel>decide|nudge)')
MAX_BOARD_BODY = 64 * 1024
ACTIONS = PIN_ACTIONS | PROTECTED_ACTIONS | APP_ACTIONS
MAX_ITEMS = 50                                 # StudioApp.approve's own bound
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}')                   # post, variant, alert and proposal ids
_KEY = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}')               # a B-roll search id or candidate key
_SHA = re.compile(r'[0-9a-f]{64}')
_SESSION = re.compile(r'[A-Za-z0-9_-]{1,40}')
PROJECT_ID = re.compile(r'prj_[A-Za-z0-9]{1,64}')
ARTIFACT_KEY = re.compile(r'prj_[A-Za-z0-9]{1,64}:[A-Za-z0-9_-]{1,80}')
EVENT_ID = re.compile(r'[0-9A-Za-z]{10,40}')
NOT_HERE = 'Only the publishing computer approves posts, and this post has nothing to approve on this computer.'


def refuse_agent() -> None:
    """Every step refuses while an agent marker is set, whatever else has happened."""
    found = agent_markers_set()
    if found:
        raise ValueError(f"This is a person's act, and an agent marker is set on this computer ({', '.join(found)}). "
                         f"Nothing was done. Open AVE Studio yourself from Finder.")


def _item(x) -> dict:
    if (not isinstance(x, dict) or set(x) != {'post', 'variant', 'sha'} or not all(isinstance(x[k], str) for k in x)
            or not _ID.fullmatch(x['post']) or not _ID.fullmatch(x['variant']) or not _SHA.fullmatch(x['sha'])):
        raise ValueError('Open the post in the approval sheet first')
    return dict(x)


def approval_args(action: str, args: dict) -> dict:
    """The only arguments a sheet request may carry. Anything else (a PIN, a name for an approval, a view time) is refused."""
    if not isinstance(args, dict):
        raise ValueError('Open it in the approval sheet first')
    if action == 'approvePublish':
        items = args.get('items')
        if set(args) != {'items'} or not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
            raise ValueError(f'Choose 1 to {MAX_ITEMS} posts in the approval sheet first')
        out = [_item(x) for x in items]
        if len({(x['post'], x['variant']) for x in out}) != len(out):
            raise ValueError('A post is listed twice')
        return {'items': out}
    if action == 'approveTrim':
        if (set(args) != {'project', 'session'} or not all(isinstance(args[k], str) for k in args)
                or not PROJECT_ID.fullmatch(args['project']) or not _SESSION.fullmatch(args['session'])):
            raise ValueError('Open the trim in the approval sheet first')
        return dict(args)
    if action == 'ackAlert':
        if set(args) != {'alert'} or not isinstance(args['alert'], str) or not _ID.fullmatch(args['alert']):
            raise ValueError('Choose an alert in Settings first')
        return dict(args)
    if action == 'acceptProposal':
        if set(args) != {'proposal'} or not isinstance(args['proposal'], str) or not _ID.fullmatch(args['proposal']):
            raise ValueError('Choose one of Claude’s suggestions first')
        return dict(args)
    if action in ('confirmRelayed', 'undoRelayed'):
        if (set(args) != {'artifact', 'event'} or not all(isinstance(args[k], str) for k in args)
                or not ARTIFACT_KEY.fullmatch(args['artifact']) or not EVENT_ID.fullmatch(args['event'])):
            raise ValueError('Choose what Claude reported on the board first')
        return dict(args)
    if action == 'approveContent':
        if (set(args) - {'note'} != {'project', 'doc', 'id'} or not all(isinstance(args[k], str) for k in args)
                or not PROJECT_ID.fullmatch(args['project']) or not _ID.fullmatch(args['doc']) or not _ID.fullmatch(args['id'])
                or len(args.get('note') or '') > 2000):
            raise ValueError('Open the content item in the approval sheet first')
        return dict(args)
    if action == 'rejectContent':
        if (set(args) - {'note'} != {'project', 'doc', 'id'} or not all(isinstance(args[k], str) for k in args)
                or not PROJECT_ID.fullmatch(args['project']) or not _ID.fullmatch(args['doc']) or not _ID.fullmatch(args['id'])
                or len(args.get('note') or '') > 2000):
            raise ValueError('Open the content item in the approval sheet first')
        return dict(args)
    if action == 'rejectEpisodePost':
        if (set(args) - {'reason'} != {'project', 'id'} or not all(isinstance(args[k], str) for k in args)
                or not PROJECT_ID.fullmatch(args['project']) or not _KEY.fullmatch(args['id']) or len(args.get('reason') or '') > 500):
            raise ValueError('Open the episode post in the approval sheet first')
        return dict(args)
    if action == 'clearTrim':
        if (set(args) != {'project', 'session'} or not all(isinstance(args[k], str) for k in args)
                or not PROJECT_ID.fullmatch(args['project']) or not _SESSION.fullmatch(args['session'])):
            raise ValueError('Open the trim in the Room first')
        return dict(args)
    if action == 'boardWrite':
        import json
        from urllib.parse import unquote
        m = BOARD_PATH.fullmatch(args.get('path') or '') if isinstance(args.get('path'), str) else None
        if (set(args) != {'method', 'path', 'body'} or m is None or not isinstance(args['body'], dict)
                or len(json.dumps(args['body'])) > MAX_BOARD_BODY):
            raise ValueError('That is not a change the board makes')
        key = unquote(m['key']) if m['key'] else None
        want = 'PATCH' if key else 'POST'
        if args['method'] != want or (key is not None and not ARTIFACT_KEY.fullmatch(key)):
            raise ValueError('That is not a change the board makes')
        return {'method': want, 'path': args['path'], 'body': dict(args['body']), 'key': key, 'selects': m['sel']}
    if action == 'pickBroll':
        if (set(args) != {'search', 'candidate', 'by'} or not all(isinstance(args[k], str) for k in args)
                or not _KEY.fullmatch(args['search']) or not _KEY.fullmatch(args['candidate']) or '..' in args['search'] + args['candidate']
                or not args['by'].strip() or len(args['by']) > 80):
            raise ValueError('Choose a result and add your name first')
        return dict(args)
    raise ValueError('Unknown approval')


def _authority(app):
    a = getattr(app, 'native_authority', None)
    if a is None:
        raise ValueError('This needs the AVE Studio app on this computer.')
    return a


def clock(s) -> str:
    """A time in the recording, the way both trim sheets show it: m:ss.ss (or the recording's own end when there is none)."""
    if s is None:
        return "the recording's own"
    m = int(s // 60)
    return f'{m}:{s - m * 60:05.2f}'


def publish_words(view: dict, sha: str) -> str:
    """One post's part of the native sheet: what the approval signs, read from the queue item itself."""
    b = view['bound']
    lines = [f"Account: {b.get('account')}", f"Post type: {b.get('post_type') or 'post'}"]
    for label, key in (('Caption', 'caption'), ('Title', 'title'), ('First comment', 'first_comment'), ('Link', 'link_url')):
        if b.get(key):
            lines.append(f'{label}: {b[key]}')
    if b.get('tags'):
        lines.append('Hashtags: ' + ' '.join('#' + str(t).lstrip('#') for t in b['tags']))
    for i, alt in enumerate(b.get('alt_texts') or [], 1):
        lines.append(f'Alt text {i}: {alt or "(missing)"}')
    for f in b.get('files') or []:
        lines.append(f"File: {f.get('name') or f.get('error')} · {f.get('sha256') or 'cannot be read'}")
    lines.append(f'Approval covers exactly: {sha}')
    return '\n'.join(lines)


FULL_ITEMS = 3                     # a native alert does not scroll: past this, one line per post
SHOWN_ITEMS = 12                   # and past this, a count


def short_words(view: dict, sha: str) -> str:
    """One post in one line, for a sheet with many: account, the caption's start, and the start of what the approval covers."""
    b = view['bound']
    cap = ' '.join(str(b.get('caption') or b.get('title') or '').split())
    cap = cap if len(cap) <= 60 else cap[:59] + '…'
    return f"{b.get('account')} · {cap or 'no caption'} · {len(b.get('files') or [])} file(s) · {sha[:12]}"


def trim_words(title: str, row: dict) -> str:
    lines = [f'Project: {title}', f"Session: {row['session']}",
             f"Claude's trim for the draft: starts at {clock(row['in'])}, ends at {clock(row['out'])}"]
    lines += row['why']
    lines.append('Confirming makes this trim yours. The final cut may then use it, after the assembly and rough cut are made '
                 'again from it.')
    return '\n'.join(lines)


class Approvals:
    """The work behind the sheets. `app` is the studio's StudioApp (its native authority mints the grants); `workspace` lists
    the projects on this computer, for trims."""

    def __init__(self, app, workspace=None, episodes=None, server=None):
        self.app = app
        self.workspace = workspace
        self.episodes = episodes
        self.server = server                   # the StudioServer: the Room's select calls

    # ---------------------------------------------------------------- publishing approval (one post or several)
    def _one(self, post: str, variant: str, sha: str) -> dict:
        from .studio.ui.api import StudioError
        try:
            view = self.app._queue_view(post, variant)
            served = self.app._served_sha(post, variant) if view is not None else None
        except (StudioError, FileNotFoundError, KeyError) as e:
            raise ValueError(NOT_HERE if getattr(e, 'code', '') == 'view.no_queue' else str(e)) from None
        if view is None:
            raise ValueError(NOT_HERE)
        if view.get('mismatch'):
            raise ValueError('The review item differs from the post (' + ', '.join(m['field'] for m in view['mismatch']) +
                             '), so it cannot be approved. Ask Claude to sync the queue, then look again.')
        if served is not None and served != sha:
            raise ValueError('The files changed since the approval sheet showed them. Look at the post again.')
        return view

    def publish_request(self, items: list[dict]) -> dict:
        """What the native sheet shows: each post read again from its queue item. A post that is not what the web sheet
        showed is left out and reported; the rest are approved together after one check."""
        refuse_agent()
        if isinstance(items, dict):                            # one post, as {post, variant, sha}
            items = [items]
        ok, refused, parts, views = [], [], [], []
        for x in items:
            try:
                view = self._one(x['post'], x['variant'], x['sha'])
            except ValueError as e:
                refused.append({'post': x['post'], 'variant': x['variant'], 'reason': str(e)})
                continue
            ok.append(dict(x))
            views.append(view)
            parts.append(publish_words(view, x['sha']))
        if not ok:
            raise ValueError('Nothing to approve: ' + '; '.join(r['reason'] for r in refused))
        n = len(ok)
        head = 'Approve this exact post?' if n == 1 else f'Approve these {n} exact posts?'
        if n <= FULL_ITEMS:
            body = '\n\n'.join(parts if n == 1 else [f'{i}. ' + p for i, p in enumerate(parts, 1)])
        else:                                   # an alert does not scroll: one line per post, at most SHOWN_ITEMS, then a count
            lines = [f"{i}. {short_words(v, x['sha'])}" for i, (v, x) in enumerate(zip(views, ok), 1)]
            body = (f'{n} posts, each exactly as the approval sheet showed it:\n' + '\n'.join(lines[:SHOWN_ITEMS])
                    + (f'\n…and {n - SHOWN_ITEMS} more, each listed with its files in the approval sheet.' if n > SHOWN_ITEMS else ''))
        detail = body + '\n\nApproving sends nothing now. Each post goes out at its time from the publishing computer.'
        if refused:
            detail += '\n\nLeft out: ' + '; '.join(f"{r['post']}: {r['reason']}" for r in refused)
        return {'kind': 'publish', 'items': ok, 'refused': refused, 'title': head, 'detail': detail,
                'typed': self.app._approval_mode() == 'typed', 'action': 'approvePublish',
                'keys': [self.app.approval_key(x['post'], x['variant'], x['sha']) for x in ok]}

    def approve_publish(self, request: dict, grant, pin: str, shown_ms: int, by: str, confirm: dict | None = None) -> dict:
        """Spend the device-owner grant on exactly these posts: record that the person looked at the exact items for
        `shown_ms` (measured by the native sheet), then approve them through the studio's own approval, which checks the PIN,
        the view and the sha again, post by post. One post that fails does not undo the others."""
        refuse_agent()
        authority = _authority(self.app)
        items = []
        for x in request['items']:
            it = dict(x)
            if confirm and confirm.get(f"{x['post']}/{x['variant']}") is not None:
                it['confirm'] = confirm[f"{x['post']}/{x['variant']}"]
            items.append(it)
        with authority.use(grant, 'approvePublish', request['keys']):
            for x in request['items']:
                self.app.record_view(x['post'], {'variant': x['variant'], 'sha': x['sha'], 'displayed_ms': int(shown_ms)}, via='native-sheet')
            refuse_agent()
            res = self.app.approve({'items': items, 'pin': pin, 'by': by})
        refused = list(request.get('refused') or []) + res['refused']
        return {'state': 'approved' if res['approved'] else 'refused', 'approved': res['approved'], 'refused': refused}

    # ---------------------------------------------------------------- trim approval
    def trims(self) -> list[dict]:
        """Every trim waiting on the director, across the episode projects on this computer."""
        from .episode import Episode
        from . import trim
        out = []
        for p in (self.workspace.projects() if self.workspace is not None else []):
            if not (p.root / 'episode.json').is_file():
                continue
            title = p.title or p.root.name
            for row in trim.awaiting(Episode(p.root)):
                out.append({'project': p.project_id, 'title': title, **row})
        return out

    def _trim(self, project_id: str, session: str):
        from .episode import Episode
        from . import trim
        if self.workspace is None:
            raise ValueError('That project is not on this computer.')
        p = next((p for p in self.workspace.projects() if p.project_id == project_id), None)
        if p is None or not (p.root / 'episode.json').is_file():
            raise ValueError('That project is not on this computer.')
        ep = Episode(p.root)
        row = next((r for r in trim.awaiting(ep) if r['session'] == session), None)
        if row is None:
            raise ValueError(f'The trim for {session} is not waiting on you any more. Look at the approval sheet again.')
        return p, ep, row

    def trim_request(self, project_id: str, session: str) -> dict:
        refuse_agent()
        p, _, row = self._trim(project_id, session)
        return {'kind': 'trim', 'project': project_id, 'session': session, 'in': row['in'], 'out': row['out'],
                'title': f"Confirm the trim for {session}?", 'detail': trim_words(p.title or p.root.name, row),
                'action': 'approveTrim', 'keys': [f'{project_id}/{session}']}

    def approve_trim(self, request: dict, grant, pin: str) -> dict:
        """Spend the grant: the PIN, then the director's own `trim set`, for exactly the ends the sheet showed."""
        refuse_agent()
        authority = _authority(self.app)
        with authority.use(grant, 'approveTrim', request['keys']):
            self.app.pin.check(pin)
            _, ep, row = self._trim(request['project'], request['session'])
            if (row['in'], row['out']) != (request['in'], request['out']):
                raise ValueError('Claude changed this trim while the sheet was open. Nothing was confirmed. Look again.')
            from . import trim
            refuse_agent()
            try:
                rec = trim.confirm(ep, request['session'], in_s=row['in'], out_s=row['out'],
                                   quote='Confirmed in the trim approval sheet')
            except SystemExit as e:              # set_trim speaks to a terminal; the sheet shows the same words
                raise ValueError(str(e)) from None
        return {'state': 'confirmed', 'session': request['session'], 'trim': rec[request['session']]}

    # ---------------------------------------------------------------- the smaller protected acts (no PIN, as before)
    def protected_request(self, action: str, args: dict) -> dict:
        refuse_agent()
        if action == 'ackAlert':
            from .studio import ledger
            a = next((x for x in ledger.list_alerts(self.app.studio.sp) if x.get('id') == args['alert']), None)
            if a is None:
                raise ValueError('That alert is not open any more.')
            return {'kind': 'protected', 'action': action, 'keys': [args['alert']], 'title': 'Acknowledge this alert?',
                    'route': f"alerts/{args['alert']}/ack", 'body': {},
                    'detail': f"{str(a.get('kind', '')).replace('_', ' ')}: {a.get('message', '')}\n\nIt leaves the list as read by you. "
                              "Nothing about the post or the account changes."}
        if action == 'acceptProposal':
            return {'kind': 'protected', 'action': action, 'keys': [args['proposal']], 'title': "Accept Claude's suggested moves?",
                    'route': f"schedule/proposals/{args['proposal']}/accept", 'body': {},
                    'detail': 'The posts in this suggestion move to the times Claude proposed. Approvals stay: they cover the words '
                              'and pictures, not the time. Each move is recorded.'}
        if action == 'pickBroll':
            key = f"{args['search']}/{args['candidate']}"
            return {'kind': 'protected', 'action': action, 'keys': [key], 'title': 'Download and add this B-roll?',
                    'route': 'broll/pick', 'body': {'search': args['search'], 'candidate': args['candidate'], 'by': args['by'].strip()},
                    'detail': f"Result {args['candidate']} from this search is downloaded into the studio library with its licence and "
                              f"credit, approved by {args['by'].strip()}. Nothing is placed in an edit or posted."}
        if action == 'confirmRelayed':
            key, eid = args['artifact'], args['event']
            if self.workspace is None:
                raise ValueError('That artifact is not on this computer.')
            row = self.workspace._row_of(*self.workspace._split(key)) or {}
            r = next((x for x in row.get('relayed') or [] if x.get('event') == eid), None)
            if r is None:
                raise ValueError('That is not waiting for your confirmation any more. Look at the board again.')
            return {'kind': 'protected', 'action': action, 'keys': [f'{key}/{eid}'], 'title': 'Confirm what Claude reported?',
                    'workspace': {'key': key, 'body': {'confirm': eid}},
                    'detail': f"{row.get('name')}\n\n{r.get('text')}\n\nConfirming records that you said this. Claude cannot confirm it for you. "
                              "If you did not say it, cancel and use Undo on the board."}
        if action == 'approveContent':
            item = next((x for x in self._episodes().content()
                         if (x['project'], x['doc'], x['id']) == (args['project'], args['doc'], args['id'])), None)
            if item is None:
                raise ValueError('That content item is not waiting for a decision any more. Look at the approval sheet again.')
            files = '\n'.join(f"File: {o['name']} · {o['sha256']}" + ('' if (o.get('alt_text') or '').strip() else ' (no alt text)')
                              for o in item['outputs'])
            critic = item.get('critic') or {}
            flagged = critic.get('ok', True) is False
            note = (args.get('note') or '').strip()
            if flagged and not note:
                raise ValueError('The critic flagged this item. Say why it is fine anyway, then approve it.')
            shas = sorted(o['sha256'] for o in item['outputs'])
            return {'kind': 'protected', 'action': action, 'keys': [f"{args['project']}/{args['doc']}/{args['id']}"],
                    'title': 'Approve this content item despite the critic?' if flagged else 'Approve this content item?',
                    # the old page's "Approve despite the critic": the person's note is the override, and the approval covers
                    # exactly the files this sheet shows (a re-render while it is open is refused)
                    'episodes': ('approve_content', [args['project'], args['doc'], args['id']], {'note': note, 'override': flagged, 'shas': shas}),
                    'detail': f"{item['title']}: {item['template'].replace('_', ' ')}\n{files}\n"
                              + (f"The critic flagged it: {'; '.join(critic.get('notes') or [])}\nYou are overriding the critic: {note}\n" if flagged else '')
                              + '\nApproving records it as yours. Nothing is posted: approved items are queued by `ave content queue`, and '
                                'the episode post they become still needs `ave publish approve`.'}
        if action == 'clearTrim':
            rec = self._episodes().trim_of(args['project'], args['session'])
            if not rec:
                raise ValueError(f"There is no trim for {args['session']} to clear.")
            return {'kind': 'protected', 'action': action, 'keys': [f"{args['project']}/{args['session']}"],
                    'title': f"Clear the trim for {args['session']}?",
                    'episodes': ('clear_trim', [args['project'], args['session']], {}),
                    'detail': f"The trim that starts at {clock(rec.get('in'))} and ends at {clock(rec.get('out'))} is removed "
                              f"({'yours' if rec.get('by') == 'human' else 'Claude' + chr(39) + 's draft'}). The cut then runs from the "
                              "recording's own start to its own end until a trim is set again. Ask Claude to make the assembly again."}
        raise ValueError('Unknown act')

    def _episodes(self):
        if self.episodes is None:
            from .studio.ui.episodes import Episodes
            self.episodes = Episodes(self.workspace, getattr(self.app, 'by', 'you'))
        return self.episodes

    def app_reject(self, action: str, args: dict, by: str | None) -> dict:
        """A reject from the app's sheet: no device-owner check (the old page needed none), but recorded as the person, which
        only the app can do: the same request over the room token is recorded as Claude's."""
        refuse_agent()
        eps = self._episodes()
        if action == 'rejectContent':
            return eps.content_decide(args['project'], args['doc'], args['id'], 'reject', note=args.get('note') or '', by=by)
        return eps.publish_reject(args['project'], args['id'], args.get('reason') or '', by=by)

    def undo_relayed(self, args: dict) -> dict:
        """The app's Undo on something Claude reported: unchecked, and written as the person because it came through the app."""
        refuse_agent()
        if self.workspace is None:
            raise ValueError('That artifact is not on this computer.')
        return self.workspace.patch(args['artifact'], {'undo': args['event']}, via='app')

    # ---------------------------------------------------------------- the person's own board writes, through the app
    def board_request(self, args: dict) -> dict:
        """What one board write needs: `{'kind': 'app', ...}` is done at once as the person; `{'kind': 'protected', ...}` (an answer
        to a settings conflict) first shows its sheet and takes the device-owner check bound to that decision. A confirm of what
        Claude reported is refused here: it has its own sheet."""
        refuse_agent()
        if args.get('selects'):
            return {'kind': 'app', 'selects': f"selects/{args['selects']}", 'body': args['body']}
        if args['key'] is None:
            return {'kind': 'app', 'create': args['body']}
        key, body = args['key'], args['body']
        if 'confirm' in body:
            raise ValueError('Confirm what Claude reported with its own Confirm button: it checks that it is you.')
        if self.workspace is None:
            raise ValueError('That artifact is not on this computer.')
        did = body.get('resolve')
        if isinstance(did, str):
            project, aid = self.workspace._split(key)
            d = project.state().decisions.get(did) or {}
            data = d.get('data') or {}
            if d.get('artifact') == aid and data.get('kind') == 'setting':
                choice = body.get('choice')
                picks = body.get('picks') if isinstance(body.get('picks'), dict) else {}
                lines = [f"{k.get('label') or k.get('key')}: " + (data.get('copy_label') if picks.get(k.get('key')) == 'copy' else data.get('file_label') or 'this file')
                         for k in data.get('keys') or []] if picks else []
                return {'kind': 'protected', 'action': 'answerDecision', 'keys': [f'{key}/{did}'],
                        'title': 'Apply this answer to the settings file?', 'workspace': {'key': key, 'body': body},
                        'detail': f"{data.get('summary')}\n\nYour answer: {choice}" + ('\n' + '\n'.join(lines) if lines else '') +
                                  f"\n\n{data.get('file') or 'The file'} is rewritten with it on the owner's computer, and the other copy is "
                                  "archived there. Undo on the board puts both back."}
        return {'kind': 'app', 'workspace': {'key': key, 'body': body}}

    def board_write(self, request: dict) -> dict:
        """A board write the person made in the app: written as theirs (`via="app"`). It fails the way the same route fails
        over HTTP (a StudioError with its status), so the page says the same words."""
        refuse_agent()
        if request['kind'] != 'app':
            raise ValueError('This change needs its own sheet')
        from .room import cut as room_cut
        from .studio.ui.api import StudioError
        try:
            return self._board_write(request)
        except StudioError:
            raise
        except room_cut.NotFound as e:
            raise StudioError(404, str(e), 'not_found') from None
        except LookupError as e:
            raise StudioError(409, str(e), 'conflict') from None
        except (TypeError, ValueError) as e:
            raise StudioError(400, str(e), 'bad_request') from None

    def _board_write(self, request: dict) -> dict:
        if 'selects' in request:
            server = self.server
            if server is None:
                raise ValueError('The Room is not open on this computer.')
            return server.selects_write(request['selects'], request['body'], via='app')
        if self.workspace is None:
            raise ValueError('That artifact is not on this computer.')
        if 'create' in request:
            return self.workspace.create(request['create'], via='app')
        return self.workspace.patch(request['workspace']['key'], request['workspace']['body'], via='app')

    # ---------------------------------------------------------------- handing over publishing, removing a member
    def sharing_keys(self, action: str, project: str) -> list[str]:
        """What the device-owner check for handing over publishing or removing a member is bound to: this act on this project."""
        refuse_agent()
        if action not in SHARING_GRANTS or not isinstance(project, str) or not PROJECT_ID.fullmatch(project):
            raise ValueError('Unknown act')
        return [project]

    def run_sharing(self, action: str, project: str, grant, work):
        """Spend the grant on exactly this act on this project, on the calling thread, then do `work()` (members.hand_over or
        members.remove). No grant, a grant for anything else, an agent marker, or no app: refused, and nothing is done."""
        refuse_agent()
        keys = self.sharing_keys(action, project)
        authority = _authority(self.app)
        with authority.use(grant, action, keys):
            from .desktop_auth import require_act
            require_act(authority, action, keys)
            refuse_agent()
            return work()

    def run_protected(self, request: dict, grant, by: str | None = None) -> dict:
        """Spend the grant on exactly this act, through the studio's own route."""
        refuse_agent()
        authority = _authority(self.app)
        body = dict(request.get('body') or {})
        if by and request['action'] == 'ackAlert':
            body['by'] = by
        with authority.use(grant, request['action'], request['keys']):
            if 'episodes' in request:
                name, a, kw = request['episodes']
                refuse_agent()
                if name in ('content_decide', 'approve_content'):
                    kw = {**kw, 'by': by or None}
                return getattr(self._episodes(), name)(*a, **kw)
            if 'workspace' in request:
                refuse_agent()
                return self.workspace.patch(request['workspace']['key'], request['workspace']['body'], via='app')
            status, result = self.app.post(request['route'], body)
        return result
