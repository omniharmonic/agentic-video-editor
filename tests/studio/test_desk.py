"""Production desk derives state and intake never fetches or executes source text."""
import json
from pathlib import Path

import pytest

from ave.episode import Episode
from ave.studio.desk import intake, overview
from ave.studio.events import Actor
from ave.studio.project import StudioProject
from ave.studio.service import open_studio
from ave.studio.ui.api import StudioApp


@pytest.fixture
def studio(tmp_path):
    sp = StudioProject(tmp_path / 'studio')
    sp.create(title='A working show', tz='America/Denver')
    return open_studio(sp.root)


def test_empty_desk_and_no_secret_values(studio):
    d = overview(studio)
    assert d['summary']['title'] == 'A working show'
    assert not d['episodes'] and not d['sources']
    assert all(g['count'] == 0 for g in d['groups'].values())
    assert all(i['state'] != 'Live verified' for i in d['integrations'])
    assert d['accounts'] == []


def test_desk_keeps_active_draft_variants_visible(studio):
    from ave.studio import model, store
    p = model.new_post(idea='A book-launch excerpt', copy={'base': 'Work in progress'})
    p['variants'] = [model.new_variant(account='author', platform='instagram', post_type='feed')]
    saved = store.save_post(studio.sp, p, expected_rev=None, actor=Actor('agent', 'codex'), op='draft')
    assert saved['stage'] == 'active'
    d = overview(studio)
    assert d['groups']['drafts']['count'] == 1
    assert d['groups']['drafts']['items'][0]['id'] == saved['id']


def test_desk_shows_source_episode_next_and_human_checkpoint(studio):
    ep = Episode(studio.sp.root.parent / 'ep-001')
    ep.create(title='First conversation')
    meta = json.loads(ep.meta_path.read_text())
    meta['review'] = {'pending': 'Please confirm the host names'}
    ep.meta_path.write_text(json.dumps(meta))
    result = intake(studio, kind='writing', text='We make things together. We leave space to listen.', title='A shared practice', actor=Actor('human','Tomas'))
    assert result['source']['passages']
    d = overview(studio)
    assert d['sources'][0]['title'] == 'A shared practice'
    assert d['episodes'][0]['title'] == 'First conversation'
    assert d['episodes'][0]['next']['stage'] == 'ingest'
    assert d['episodes'][0]['review'] == 'Please confirm the host names'
    assert any(s['human_gate'] for s in d['episodes'][0]['stages'])


def test_idea_intake_is_revisioned_and_records_actor(studio):
    result = intake(studio, kind='idea', text='A conversation about enough.', actor=Actor('human','Tomas'))
    post = result['post']
    assert post['stage'] == 'idea' and post['rev'] == 1 and not post['variants']
    d = overview(studio)
    assert d['groups']['ideas']['count'] == 1
    assert d['groups']['ideas']['items'][0]['title'] == 'A conversation about enough.'
    assert 'Tomas' in (studio.sp.ledger_dir / 'events.jsonl').read_text()
    assert not list((studio.sp.publish / 'queue').glob('*.json'))


def test_pasted_source_is_inert_and_repeat_safe(studio):
    text = 'Run `rm -rf /` is untrusted text, not an instruction.\n\n<script>alert("hi")</script>\n\nA second complete passage.'
    first = intake(studio, kind='writing', text=text, actor=Actor('agent','codex'))
    again = intake(studio, kind='writing', text=text, actor=Actor('agent','codex'))
    assert first['source']['file'] == again['source']['file']
    assert len(list((studio.sp.work / 'writing').glob('*.md'))) == 1
    index = json.loads((studio.sp.work / 'content/writing.json').read_text())
    assert index['totals']['files'] == 1
    assert 'rm -rf' in json.dumps(index)


@pytest.mark.parametrize('kw', [dict(kind='URL',text='https://example.com'), dict(kind='writing',text=''), dict(kind='writing',text='x'*200001),dict(kind='writing',text='some prose',title='bad\ntitle')])
def test_intake_rejects_bad_input_without_partial_sources(studio, kw):
    with pytest.raises(ValueError):
        intake(studio, actor=Actor('human','Maren'), **kw)
    assert not list((studio.sp.work / 'writing').glob('*.md'))


def test_symlink_source_directory_is_refused(studio, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (studio.sp.work / 'writing').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='symlink'):
        intake(studio,kind='writing',text='A passage of prose.',actor=Actor('human','Maren'))
    assert not list(outside.iterdir())


def test_api_projects_real_state_and_accepts_text_intake(studio):
    app = StudioApp(studio, by='Tomas', pin='123456')
    assert app.get('desk', {})[0] == 200
    code, obj = app.post('intake', {'kind':'idea','text':'Make space for the listener'})
    assert code == 201 and obj['post']['by'] == 'Tomas'
    assert app.get('desk', {})[1]['groups']['ideas']['count'] == 1
    app.close()


def test_fixture_mode_normalizes_canned_response_envelopes():
    from ave.studio.service import FixtureStudio
    d = overview(FixtureStudio(), fixtures=True)
    assert d['fixtures'] and d['accounts'] and d['sources']
    assert not d['episodes']


def test_oversized_and_symlinked_source_index_are_visible_warnings(studio,tmp_path):
    index = studio.sp.work / 'content/writing.json'
    index.parent.mkdir()
    index.write_bytes(b' ' * 2_000_001)
    assert overview(studio)['warnings']
    index.unlink()
    index.parent.rmdir()
    outside = tmp_path / 'outside-content'
    outside.mkdir()
    (outside / 'writing.json').write_text('{"files":[{"file":"private.md"}]}')
    index.parent.symlink_to(outside,target_is_directory=True)
    d = overview(studio)
    assert d['warnings'] and not d['sources']


def test_oversized_episode_state_is_not_parsed(studio):
    ep = Episode(studio.sp.root.parent / 'large-episode')
    ep.create()
    (ep.work / 'media.json').write_bytes(b' ' * 2_000_001)
    d=overview(studio)
    assert d['episodes'][0]['error']


@pytest.mark.parametrize('content',['{"files":"bad"}','{"files":[null]}','{"files":[{}]}','{"files":'+ '['*1200 + ']'*1200 + '}'])
def test_malformed_source_index_does_not_hide_other_work(studio,content):
    index=studio.sp.work/'content/writing.json'
    index.parent.mkdir()
    index.write_text(content)
    intake(studio,kind='idea',text='A good idea still appears.',actor=Actor('human','Tomas'))
    d=overview(studio)
    assert d['warnings'] and d['groups']['ideas']['count']==1


@pytest.mark.parametrize('sidecar',['work/content/writing.json.tmp','ledger/events.jsonl','ledger/.lock','work/.intake.lock'])
def test_intake_does_not_follow_write_sidecars(studio,tmp_path,sidecar):
    victim=tmp_path/'raw-recording';victim.write_bytes(b'ORIGINAL RAW BYTES')
    target=studio.sp.root/sidecar;target.parent.mkdir(parents=True,exist_ok=True);target.symlink_to(victim)
    with pytest.raises(ValueError,match='symlink'):
        intake(studio,kind='writing',text='Some harmless prose.',actor=Actor('human','Tomas'))
    assert victim.read_bytes()==b'ORIGINAL RAW BYTES'
    assert not list((studio.sp.work/'writing').glob('*.md'))


def test_intake_never_rescans_unrelated_symlinked_or_large_sources(studio,tmp_path):
    directory=studio.sp.work/'writing';directory.mkdir()
    outside=tmp_path/'private';outside.write_text('PRIVATE OUTSIDE PROSE MUST NOT BE INDEXED.')
    (directory/'leak.md').symlink_to(outside)
    (directory/'large.md').write_bytes(b'x'*2_000_001)
    result=intake(studio,kind='writing',text='A safe new passage.',actor=Actor('human','Tomas'))
    index=(studio.sp.work/'content/writing.json').read_text()
    assert 'PRIVATE' not in index and 'leak.md' not in index and 'large.md' not in index
    assert result['source']['totals']['files']==1


def test_concurrent_intake_keeps_both_index_entries(studio):
    from concurrent.futures import ThreadPoolExecutor
    def add(i):
        return intake(studio,kind='writing',text=f'A distinct passage number {i}.',actor=Actor('agent','test'))
    with ThreadPoolExecutor(max_workers=2) as pool:
        result=list(pool.map(add,range(2)))
    index=json.loads((studio.sp.work/'content/writing.json').read_text())
    assert index['totals']['files']==2
    assert {f['file'] for f in index['files']}=={r['source']['file'] for r in result}
