"""The board tools: artifacts, sign-offs, decisions and tasks that Claude records in the project log."""
import json

import pytest

from ave.agent import AgentApp, BOARD_TOOLS, TOOL_MAP
from ave.project import Project
from ave.project.adopt import adopt
from ave.studio.project import StudioProject
from ave.studio.service import open_studio


@pytest.fixture(autouse=True)
def private_home(monkeypatch, tmp_path):
    (tmp_path / 'home').mkdir()
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))


@pytest.fixture
def mcp(tmp_path):
    sp = StudioProject(tmp_path / 'studio')
    sp.create(title='Grounded conversations', tz='America/Denver')
    adopt(sp.root)
    app = AgentApp(open_studio(sp.root), by='claude-code')
    app.n = 0

    def call(tool, **args):
        if tool in ('artifact_create', 'artifact_update', 'stage_sign_off', 'decision_record', 'decision_resolve', 'task_update'):
            app.n += 1
            args.setdefault('request_id', f'r{app.n}')
        return app.call(tool, args)
    app.call_tool = call
    return type('Mcp', (), {'call': staticmethod(call), 'app': app, 'project': staticmethod(lambda: Project.open(sp.root))})


def _at(mcp, title, stage):
    """An artifact already at a later stage, as the app or an earlier session leaves it (Claude itself starts only at Plan or Draft)."""
    aid = 'art_' + title.replace(' ', '_').lower()
    mcp.project().log.append('artifact.created', artifact=aid, data={'kind': 'episode', 'title': title, 'stage': stage}, actor='human')
    return aid


@pytest.fixture
def art(mcp):
    return _at(mcp, 'Episode 12', 'Refine')


@pytest.fixture
def finish_art(mcp):
    return _at(mcp, 'Episode 13', 'Finish')


@pytest.fixture
def ep_with_short(mcp, art):
    short = mcp.call('artifact_create', kind='short', title='The van story', stage='Draft', parent=art)['artifact']
    return art, short


def test_sign_off_needs_the_directors_words(mcp, art):
    with pytest.raises(ValueError):
        mcp.call('stage_sign_off', artifact=art, stage='Refine')
    with pytest.raises(ValueError):
        mcp.call('stage_sign_off', artifact=art, quote='lock that')            # the stage read is required
    with pytest.raises(ValueError, match='director'):
        mcp.call('stage_sign_off', artifact=art, stage='Refine', quote='   ')
    with pytest.raises(ValueError):
        mcp.call('stage_sign_off', artifact=art, stage='Refine', quote='x' * 501)
    assert mcp.call('artifact_get', artifact=art)['stage'] == 'Refine'


def test_sign_off_cannot_reach_out(mcp, finish_art, art):
    with pytest.raises(ValueError, match='publishing approval'):
        mcp.call('stage_sign_off', artifact=finish_art, stage='Finish', quote='lock it')
    with pytest.raises(ValueError, match='publishing approval'):
        mcp.call('stage_sign_off', artifact=art, quote='lock it', stage='Out')
    assert mcp.call('artifact_get', artifact=finish_art)['stage'] == 'Finish'


def test_sign_off_records_the_quote_and_moves_on_once(mcp, art):
    first = mcp.call('stage_sign_off', artifact=art, quote='lock that', request_id='so-1', stage='Refine')
    assert first['stage'] == 'Finish' and 'lock that' in first['summary']
    assert mcp.call('stage_sign_off', artifact=art, quote='lock that', request_id='so-1', stage='Refine')['replayed'] is True
    with pytest.raises(ValueError, match='request_id'):
        mcp.call('stage_sign_off', artifact=art, quote='different words', request_id='so-1', stage='Refine')
    with pytest.raises(ValueError, match='not Refine'):
        mcp.call('stage_sign_off', artifact=art, quote='again please', stage='Refine')
    got = mcp.call('artifact_get', artifact='episode 12')                    # a plain name finds it too
    assert got['stage'] == 'Finish'
    assert got['sign_offs'] == [{'stage': 'Refine', 'to': 'Finish', 'quote': 'lock that', 'relayed': True,
                                 'reported': 'Claude reports you said: "lock that"', 'person': mcp.project().person,
                                 'actor': 'claude', 'when': got['sign_offs'][0]['when']}]
    assert 'director' not in [k for e in mcp.project().read_events() if e['op'] == 'stage.signed_off' for k in e['data']]


def test_parts_are_flagged_when_a_parent_reopens(mcp, ep_with_short):
    parent, short = ep_with_short
    rows = {r['artifact']: r for r in mcp.call('artifact_list')['artifacts']}
    assert not rows[short]['needs_review'] and rows[parent]['stage'] == 'Refine'
    mcp.project().log.append('stage.reopened', artifact=parent, data={'from': 'Refine', 'to': 'Draft'}, actor='human')
    listed = mcp.call('artifact_list')
    rows = {r['artifact']: r for r in listed['artifacts']}
    assert rows[parent]['stage'] == 'Draft' and rows[short]['stage'] == 'Draft'      # the part itself is not changed
    assert rows[short]['needs_review'] and 'Episode 12' in rows[short]['review_reason'] and not rows[parent]['needs_review']
    assert 'needs review' in listed['summary']
    # dealing with the part clears the flag
    mcp.project().log.append('version.added', artifact=short, data={'version': 'short.v1', 'file': 'edits/short.v1.json'}, actor='claude')
    assert not {r['artifact']: r for r in mcp.call('artifact_list')['artifacts']}[short]['needs_review']


def test_decision_is_listed_at_review(mcp, art):
    rec = mcp.call('decision_record', artifact=art, summary='Which camera on the laugh', options=['wide', 'Tomas close-up'],
                   chosen='wide', reason='her camera was soft', request_id='d-1')
    assert rec['status'] == 'open' and mcp.call('decision_record', artifact=art, summary='Which camera on the laugh',
                                                options=['wide', 'Tomas close-up'], chosen='wide', reason='her camera was soft',
                                                request_id='d-1')['replayed'] is True
    with pytest.raises(ValueError, match='options'):
        mcp.call('decision_record', artifact=art, summary='x', options=['a', 'b'], chosen='c')
    got = mcp.call('artifact_get', artifact=art)
    assert [d['chosen'] for d in got['decisions_open']] == ['wide'] and 'decision' in got['summary']
    assert {r['artifact']: r for r in mcp.call('artifact_list')['artifacts']}[art]['open_decisions'] == 1

    class Room:                                                                # the app, open, with this artifact on stage
        def read(self): return {'room': 'open', 'stage': {'artifact': art, 'view': 'cut'}}
    mcp.app._room_client = Room()
    shown = mcp.call('room_read')['open_decisions']
    assert shown[0]['summary'] == 'Which camera on the laugh' and shown[0]['artifact_name'] == 'Episode 12'

    with pytest.raises(ValueError):
        mcp.call('decision_resolve', decision=rec['decision'], choice='wide', quote='Which camera on the laugh')   # a copy of the summary
    with pytest.raises(ValueError):
        mcp.call('decision_resolve', decision=rec['decision'], choice='wide')            # needs the director's words
    done = mcp.call('decision_resolve', decision=rec['decision'], choice='Tomas close-up', quote='use her close-up')
    assert done['status'] == 'resolved' and done['relayed_by'] == 'claude'
    assert mcp.call('artifact_get', artifact=art)['decisions_resolved'][0]['relayed'] is True
    assert mcp.call('artifact_get', artifact=art)['decisions_open'] == []
    assert mcp.call('room_read')['open_decisions'] == []
    with pytest.raises(ValueError, match='already resolved'):
        mcp.call('decision_resolve', decision=rec['decision'], choice='wide', quote='no wait')


def test_artifacts_and_tasks_are_written_to_the_log_and_replay(mcp, art):
    made = mcp.call('artifact_create', kind='post', title='Teaser', request_id='c-1', fields={'owner': 'Tomas'})
    again = mcp.call('artifact_create', kind='post', title='Teaser', request_id='c-1', fields={'owner': 'Tomas'})
    assert again['replayed'] and again['artifact'] == made['artifact']
    with pytest.raises(ValueError, match='request_id'):
        mcp.call('artifact_create', kind='post', title='Other', request_id='c-1', fields={'owner': 'Tomas'})
    with pytest.raises(ValueError, match='stage'):
        mcp.call('artifact_update', artifact=art, fields={'stage': 'Out'})
    mcp.call('artifact_update', artifact=art, fields={'due': '2026-12-01', 'title': 'Episode 12: the van'})
    got = mcp.call('artifact_get', artifact=art)
    assert got['fields']['due'] == '2026-12-01' and got['name'] == 'Episode 12: the van'
    t = mcp.call('task_update', title='Pick the thumbnail', artifact=art, status='todo')
    mcp.call('task_update', task=t['task'], status='done')
    assert mcp.call('artifact_get', artifact=art)['tasks'] == [{'task': t['task'], 'title': 'Pick the thumbnail', 'status': 'done'}]
    with pytest.raises(ValueError, match='title'):
        mcp.call('task_update', status='todo')
    assert all(e['actor'] == 'claude' for e in mcp.project().read_events() if e['op'] in ('task.updated', 'decision.recorded'))


def test_not_adopted_folder_says_how_to_adopt(tmp_path):
    sp = StudioProject(tmp_path / 'plain')
    sp.create(title='Plain', tz='America/Denver')
    app = AgentApp(open_studio(sp.root), by='claude-code')
    assert app.call('artifact_list', {})['adopted'] is False
    with pytest.raises(ValueError, match='ave project adopt'):
        app.call('stage_sign_off', {'request_id': 'a', 'artifact': 'x', 'stage': 'Plan', 'quote': 'lock that'})
    assert not (sp.root / '.ave').exists()


def test_board_tools_speak_plainly_and_cross_no_gate(mcp, ep_with_short):
    assert set(BOARD_TOOLS) <= set(TOOL_MAP)
    assert not set(TOOL_MAP) & {'stage_reopen', 'approve_publish', 'publish'}
    parent, short = ep_with_short
    mcp.call('decision_record', artifact=parent, summary='Where the cut lands', options=['early', 'late'], chosen='late')
    for text in (mcp.call('artifact_list')['summary'], mcp.call('artifact_get', artifact=parent)['summary'],
                 mcp.call('artifact_get', artifact=short)['summary']):
        assert not any(w in text for w in ('art_', 'dec_', 'EditDoc', 'JSON', 'MCP', 'segment')), text
    json.dumps(mcp.call('artifact_list'))


def test_director_skill_names_the_board_tools(mcp):
    text = mcp.call('read_skill', name='ave-director')['text']
    for tool in BOARD_TOOLS:
        assert f'`{tool}' in text, tool


@pytest.mark.parametrize('quote', ['', '   ', 'Episode 12', 'episode 12'])
def test_sign_off_quotes_that_are_empty_or_the_artifact_name_are_refused(mcp, art, quote):
    with pytest.raises(ValueError, match='quote'):
        mcp.call('stage_sign_off', artifact=art, stage='Refine', quote=quote)


@pytest.mark.parametrize('quote', ['ok', 'Take 2', 'go', 'Refine'])
def test_one_word_answers_are_accepted_as_the_directors_words(mcp, art, quote):
    assert mcp.call('stage_sign_off', artifact=art, stage='Refine', quote=quote)['quote'] == quote


def test_decision_can_be_resolved_in_a_word(mcp, art):
    rec = mcp.call('decision_record', artifact=art, summary='Which take', options=['Take 1', 'Take 2'], chosen='Take 1')
    assert mcp.call('decision_resolve', decision=rec['decision'], choice='Take 2', quote='Take 2')['status'] == 'resolved'


def _crash_after_append(monkeypatch, op):
    from ave.project.log import Log
    real = Log.append
    state = {'armed': True}

    def append(self, o, *a, **k):
        out = real(self, o, *a, **k)
        if o == op and state['armed']:
            state['armed'] = False
            raise RuntimeError('crashed after the log write')
        return out
    monkeypatch.setattr(Log, 'append', append)


def test_retry_after_a_crash_advances_exactly_once(mcp, art, monkeypatch):
    _crash_after_append(monkeypatch, 'stage.signed_off')
    with pytest.raises(RuntimeError):
        mcp.call('stage_sign_off', artifact=art, stage='Refine', quote='lock that', request_id='so-crash')
    again = mcp.call('stage_sign_off', artifact=art, stage='Refine', quote='lock that', request_id='so-crash')
    assert again['replayed'] is True and again['stage'] == 'Finish'
    assert [e['op'] for e in mcp.project().read_events() if e['op'] == 'stage.signed_off'] == ['stage.signed_off']
    assert mcp.call('artifact_get', artifact=art)['stage'] == 'Finish'
    assert not list((mcp.app.root / '.ave' / 'local').glob('mcp-requests*'))


def test_retry_after_a_crash_between_created_and_linked_adds_only_the_link(mcp, art, monkeypatch):
    _crash_after_append(monkeypatch, 'artifact.created')
    args = dict(kind='short', title='The van story', parent=art, request_id='c-crash')
    with pytest.raises(RuntimeError):
        mcp.call('artifact_create', **args)
    made = mcp.call('artifact_create', **args)
    assert made['replayed'] is False
    ops = [e['op'] for e in mcp.project().read_events() if (e.get('data') or {}).get('request_id') == 'c-crash']
    assert ops == ['artifact.created', 'artifact.linked']
    assert mcp.call('artifact_create', **args)['replayed'] is True
    assert mcp.call('artifact_get', artifact=art)['parts'] == [made['artifact']]
    assert 'request_id' not in mcp.call('artifact_get', artifact=made['artifact'])['fields']


def test_claude_starts_artifacts_only_at_plan_or_draft(mcp):
    made = mcp.call('artifact_create', kind='episode', title='Ambitious', stage='Finish')
    assert made['stage'] == 'Plan'
    got = mcp.call('artifact_get', artifact=made['artifact'])
    assert got['stage'] == 'Plan' and got['decisions_open'][0]['options'] == ['Plan', 'Finish']


def test_a_task_or_field_update_does_not_clear_needs_review(mcp, ep_with_short):
    parent, short = ep_with_short
    mcp.project().log.append('stage.reopened', artifact=parent, data={'from': 'Refine', 'to': 'Draft'}, actor='human')
    mcp.call('artifact_update', artifact=short, fields={'note': 'looking at it'})
    mcp.call('task_update', title='Redo the short', artifact=short)
    assert {r['artifact']: r for r in mcp.call('artifact_list')['artifacts']}[short]['needs_review']


def test_a_resolution_undone_in_the_app_reopens_the_decision_for_claude(mcp, art):
    did = mcp.call('decision_record', artifact=art, summary='Opened on the wide shot', options=['Wide', 'Close'], chosen='Wide')['decision']
    mcp.call('decision_resolve', decision=did, choice='Close', quote='go close')
    assert not mcp.call('artifact_get', artifact=art)['decisions_open']
    mcp.project().log.append('decision.resolved', artifact=art, data={'decision': did, 'reopen': True}, actor='human')
    got = mcp.call('artifact_get', artifact=art)
    assert [d['decision'] for d in got['decisions_open']] == [did] and not got['decisions_resolved']
    mcp.call('decision_resolve', decision=did, choice='Wide', quote='actually wide')
    assert not mcp.call('artifact_get', artifact=art)['decisions_open']


def test_a_broken_conflict_record_never_breaks_a_board_read(mcp, art, monkeypatch):
    """Final review M10: like readiness and decisions, the conflict watcher's records are read best effort."""
    from ave.syncthing import conflicts
    def broken(root):
        raise OSError('the conflict folder cannot be read')
    monkeypatch.setattr(conflicts, 'alerts', broken)
    monkeypatch.setattr(conflicts, 'waiting', broken)
    listed = mcp.call('artifact_list')
    assert art in {r['artifact'] for r in listed['artifacts']} and listed['conflict_copies'] == [] and listed['waiting_for_owner'] == []
