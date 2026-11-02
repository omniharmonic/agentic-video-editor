"""Bounded MCP boundary: tools never expose shell, publishing or human approvals."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from ave.studio.project import StudioProject
from ave.studio.service import open_studio
from ave.studio.model import RevConflict, new_variant
from ave.studio import store
from ave.studio.events import Actor


@pytest.fixture
def app(tmp_path):
    from ave.agent import AgentApp
    sp = StudioProject(tmp_path / 'studio')
    sp.create(title='Grounded conversations', tz='America/Denver')
    return AgentApp(open_studio(sp.root), by='codex')


def call(app, tool_name, **arguments):
    return app.call(tool_name, arguments)


def test_context_reads_disk_and_has_explicit_gates(app):
    root = app.studio.sp.root
    before = {p.relative_to(root):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    ctx = call(app, 'get_context')
    assert ctx['show']['title'] == 'Grounded conversations'
    assert ctx['human_gates'] and ctx['live_verified'] is False
    assert ctx['untrusted_text']
    after = {p.relative_to(root):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    assert before == after


def test_tool_surface_is_explicit(app):
    from ave.agent import TOOLS
    names = {t['name'] for t in TOOLS}
    assert {'get_context','create_draft','edit_draft','list_posts','get_post','read_skill','read_source','list_recipes','recipe_status'} <= names
    assert not names & {'approve','publish','send','run','shell','exec','attest','auth','account_live','sync_queue'}
    assert all(t['inputSchema']['additionalProperties'] is False for t in TOOLS)


def test_create_retry_is_idempotent_and_changed_retry_refused(app):
    args = dict(request_id='idea-001', idea='Enough begins with attention.', copy={'base':'A passage to share.'})
    first = call(app,'create_draft',**args)
    again = call(app,'create_draft',**args)
    assert first['post']['id'] == again['post']['id']
    assert first['post']['rev'] == again['post']['rev'] == 1
    assert len(store.list_posts(app.studio.sp)) == 1
    with pytest.raises(ValueError,match='request_id'):
        call(app,'create_draft',**{**args,'idea':'Different text'})
    assert not first['post']['variants']


def test_revision_conflict_and_projection(app):
    p = call(app,'create_draft',request_id='first',idea='A starting point')['post']
    edited = call(app,'edit_draft',post_id=p['id'],expected_rev=1,fields={'base':'The first draft','hashtags':['listening']})
    assert edited['post']['rev'] == 2 and edited['post']['copy']['base'] == 'The first draft'
    from ave.studio.model import RevConflict
    with pytest.raises(RevConflict):
        call(app,'edit_draft',post_id=p['id'],expected_rev=1,fields={'base':'Overwrites Tomas'})
    assert call(app,'get_post',post_id=p['id'])['post']['copy']['base'] == 'The first draft'


@pytest.mark.parametrize('args', [
    dict(post_id='../../outside',expected_rev=1,fields={'base':'x'}),
    dict(post_id='p_example',expected_rev=True,fields={'base':'x'}),
    dict(post_id='p_example',expected_rev=0,fields={'base':'x'}),
    dict(post_id='p_example',expected_rev=1,fields={'variants':[]}),
    dict(post_id='p_example',expected_rev=1,fields={'copy.base':'x'}),
    dict(post_id='p_example',expected_rev=1,fields={'queue':{'root':'/outside'}}),
    dict(post_id='p_example',expected_rev=1,fields={}),
    dict(post_id='p_example',expected_rev=1,fields={'hashtags':[False]}),
    dict(post_id='p_example',expected_rev=1,fields={'base':'x'},command='rm -rf /'),
])
def test_bad_editorial_arguments_refused(app,args):
    with pytest.raises((ValueError,TypeError)):
        call(app,'edit_draft',**args)


def test_active_posts_cannot_be_edited_through_draft_tool(app):
    p=call(app,'create_draft',request_id='active',idea='Keep review intact')['post']
    raw=store.load_post(app.studio.sp,p['id'])
    raw['variants']=[new_variant(account='main',platform='manual',post_type='text')]
    raw['variants'][0]['queue']={'id':'q_fake','root':'/elsewhere'}
    store.save_post(app.studio.sp,raw,expected_rev=1,actor=Actor('agent','test'),op='fixture')
    with pytest.raises(ValueError,match='draft'):
        call(app,'edit_draft',post_id=p['id'],expected_rev=2,fields={'base':'Do not touch'})


def test_source_lookup_never_reads_selected_filesystem_path(app,tmp_path):
    from ave.studio.desk import intake
    result=intake(app.studio,kind='writing',text='An exact passage with a complete thought.',title='Practice',actor=Actor('human','Maren'))
    src=call(app,'read_source',name=result['source']['file'])
    assert src['title']=='Practice' and 'exact passage' in json.dumps(src)
    outside=tmp_path/'secret.md';outside.write_text('outside-secret')
    with pytest.raises(ValueError): call(app,'read_source',name=str(outside))
    with pytest.raises(ValueError): call(app,'read_source',name='../secret.md')


def test_skills_and_recipes_are_shipped_allowlist(app):
    assert 'pipeline' in call(app,'read_skill',name='ave-pipeline')['text']
    with pytest.raises(ValueError):call(app,'read_skill',name='../../SECURITY.md')
    recipes=call(app,'list_recipes')['recipes']
    assert any(r['name']=='blog-to-posts' for r in recipes)
    status=call(app,'recipe_status',name='idea-to-visuals')
    assert status['next']['kind']=='mechanical' and status['complete'] is False
    with pytest.raises(ValueError):call(app,'recipe_status',name='../../secret')


def test_protocol_initialization_notifications_and_errors(app):
    from ave.agent import Session
    s=Session(app)
    assert s.handle({'jsonrpc':'2.0','id':0,'method':'tools/list'})['error']['code']==-32002
    init=s.handle({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'test','version':'1'}}})
    assert init['result']['protocolVersion']=='2025-06-18'
    assert s.handle({'jsonrpc':'2.0','method':'notifications/initialized'}) is None
    assert s.handle({'jsonrpc':'2.0','id':2,'method':'tools/list'})['result']['tools']
    result=s.handle({'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'publish','arguments':{}}})
    assert result['error']['code']==-32602
    assert s.handle({'jsonrpc':'2.0','method':'tools/call','params':{'name':'create_draft','arguments':{'request_id':'ignored','idea':'Never execute notifications'}}}) is None
    assert not store.list_posts(app.studio.sp)


@pytest.mark.parametrize('raw', [b'not json\n',b'{"x":NaN}\n',b'{"jsonrpc":"2.0","id":1,"id":2,"method":"ping"}\n',b'\xff\n',b'['*30+b']'*30+b'\n'])
def test_malformed_transport_recovers(app,raw):
    from ave.agent import serve
    output=io.BytesIO()
    serve(app,stdin=io.BytesIO(raw+b'{"jsonrpc":"2.0","id":77,"method":"ping"}\n'),stdout=output)
    replies=[json.loads(line) for line in output.getvalue().splitlines()]
    assert 'error' in replies[0] and replies[-1]=={'jsonrpc':'2.0','id':77,'result':{}}


def test_oversized_line_is_drained_and_next_message_works(app):
    from ave.agent import MAX_LINE,serve
    out=io.BytesIO()
    serve(app,stdin=io.BytesIO(b'x'*(MAX_LINE+20)+b'\n'+b'{"jsonrpc":"2.0","id":9,"method":"ping"}\n'),stdout=out)
    replies=[json.loads(line) for line in out.getvalue().splitlines()]
    assert replies[0]['error']['code']==-32700 and replies[-1]['id']==9


def test_real_stdio_subprocess_and_config(tmp_path):
    sp=StudioProject(tmp_path/'stdio studio');sp.create(title='Offline stdio')
    requests=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'test','version':'1'}}},
              {'jsonrpc':'2.0','method':'notifications/initialized'},
              {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'create_draft','arguments':{'request_id':'stdio-replay','idea':'A grounded idea'}}},
              {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'get_context','arguments':{}}}]
    env={**os.environ,'AVE_APPROVAL_KEY':'test-not-real'}
    r=subprocess.run([sys.executable,'-m','ave.cli','agent','serve','--studio',str(sp.root)],input='\n'.join(json.dumps(q) for q in requests)+'\n',text=True,capture_output=True,env=env,timeout=20)
    assert r.returncode==0,r.stderr
    replies=[json.loads(line) for line in r.stdout.splitlines()]
    assert [x['id'] for x in replies]==[1,2,3]
    assert not replies[1]['result'].get('isError')
    assert len(store.list_posts(sp))==1
    cfg=subprocess.run([sys.executable,'-m','ave.cli','agent','mcp-config','--studio',str(sp.root)],capture_output=True,text=True,timeout=10)
    entry=json.loads(cfg.stdout)['mcpServers']['ave-studio']
    assert entry['command']==sys.executable
    assert entry['args'][-1]==str(sp.root)
    assert not any('token' in k.lower() for k in entry)


def test_concurrent_create_request_is_single_write(app):
    from concurrent.futures import ThreadPoolExecutor
    def make():return call(app,'create_draft',request_id='same-request',idea='One thought, two retries')['post']['id']
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids=list(pool.map(lambda _:make(),range(2)))
    assert ids[0]==ids[1]
    assert len(store.list_posts(app.studio.sp))==1
    assert len((app.studio.sp.ledger_dir/'events.jsonl').read_text().splitlines())==1


def test_symlink_post_and_index_are_never_read(app,tmp_path):
    outside=tmp_path/'outside.json';outside.write_text('{"secret":"do not read"}')
    (app.studio.sp.posts_dir/'p_trap.json').symlink_to(outside)
    with pytest.raises(ValueError,match='symlink'):
        call(app,'list_posts')
    index=app.studio.sp.work/'content/writing.json'
    index.parent.mkdir()
    index.symlink_to(outside)
    with pytest.raises(ValueError,match='symlink'):
        call(app,'read_source',name='source.md')


@pytest.mark.parametrize('raw', [b'{"jsonrpc":"2.0","id":true,"method":"ping"}\n',b'{"jsonrpc":"2.0","id":[],"method":"ping"}\n',b'{"jsonrpc":"1.0","id":1,"method":"ping"}\n',b'{"jsonrpc":"2.0","id":1,"method":"ping","params":false}\n'])
def test_invalid_rpc_envelopes_return_errors(app,raw):
    from ave.agent import serve
    out=io.BytesIO();serve(app,stdin=io.BytesIO(raw),stdout=out)
    reply=json.loads(out.getvalue())
    assert reply['error']['code'] in (-32600,-32602)


def test_nan_bool_and_unicode_rejected_before_writing(app):
    for args in ({'request_id':'valid','idea':False},{'request_id':'valid','idea':'\ud800'},{'request_id':'valid','copy':{'base':float('nan')}}):
        with pytest.raises(ValueError):call(app,'create_draft',**args)
    assert store.list_posts(app.studio.sp)==[]


def test_startup_failure_has_no_protocol_stdout(tmp_path):
    r=subprocess.run([sys.executable,'-m','ave.cli','agent','serve','--studio',str(tmp_path/'missing')],capture_output=True,text=True,timeout=10)
    assert r.returncode==2 and r.stdout=='' and 'could not be opened' in r.stderr


def test_future_revision_cannot_erase_a_concurrent_variant(app, monkeypatch):
    p=call(app,'create_draft',request_id='future-race',idea='Keep Tomas’s work')['post']
    original_read=app._post

    def read_then_concurrent_variant(pid):
        stale=original_read(pid)
        concurrent=json.loads(json.dumps(stale))
        concurrent['variants']=[new_variant(account='tomas',platform='manual',post_type='text')]
        store.save_post(app.studio.sp,concurrent,expected_rev=1,actor=Actor('human','Tomas'),op='variant_add')
        return stale

    monkeypatch.setattr(app,'_post',read_then_concurrent_variant)
    with pytest.raises(RevConflict):
        call(app,'edit_draft',post_id=p['id'],expected_rev=2,fields={'base':'A stale snapshot'})
    current=store.load_post(app.studio.sp,p['id'])
    assert current['rev']==2
    assert current['variants'][0]['account']=='tomas'
    assert current['copy']['base']==''


@pytest.mark.parametrize('symlink', [False, True])
def test_context_prevalidates_writing_index_before_shared_view(app,tmp_path,symlink):
    from ave.agent import MAX_FILE
    index=app.studio.sp.work/'content/writing.json'
    index.parent.mkdir()
    if symlink:
        target=tmp_path/'outside-index.json'
        target.write_text('{"files":[]}')
        index.symlink_to(target)
    else:
        index.write_bytes(b' '*(MAX_FILE+1))
    with pytest.raises(ValueError,match='symlink|too large'):
        call(app,'get_context')


def test_context_omits_sibling_episode_scanning(app,monkeypatch):
    from ave.episode import Episode
    from ave.studio import desk
    ep=Episode(app.root.parent/'ep-one');ep.create(title='An independent episode')
    monkeypatch.setattr(desk.state,'status',lambda *a,**kw: (_ for _ in ()).throw(AssertionError('MCP must not scan episode metadata trees')))
    ctx=call(app,'get_context')
    assert ctx['desk']['episodes']==[]
    assert ctx['episode_context']['included'] is False


@pytest.mark.parametrize('operation', ['create', 'edit', 'replay'])
@pytest.mark.parametrize('destination', ['post', 'temporary', 'post_lock', 'events', 'ledger_lock'])
def test_draft_store_sidecar_symlinks_preserve_victim_bytes(app,tmp_path,operation,destination):
    import hashlib
    rid='sidecar-boundary'
    pid='p_mcp_'+hashlib.sha256((app.actor.name+'\0'+rid).encode()).hexdigest()[:40]
    if operation in ('edit','replay'):
        call(app,'create_draft',request_id=rid,idea='A safe draft')
    sp=app.studio.sp
    targets={'post':sp.posts_dir/(pid+'.json'),'temporary':sp.posts_dir/(pid+'.json.tmp'),
             'post_lock':sp.posts_dir/(pid+'.lock'),'events':sp.ledger_dir/'events.jsonl','ledger_lock':sp.ledger_dir/'.lock'}
    victim=tmp_path/'outside-victim.bin'
    original=b'Raw footage bytes must not change.\x00\xff'
    victim.write_bytes(original)
    dest=targets[destination]
    dest.parent.mkdir(parents=True,exist_ok=True)
    dest.unlink(missing_ok=True)
    dest.symlink_to(victim)
    with pytest.raises(ValueError,match='symlink'):
        if operation=='edit':
            call(app,'edit_draft',post_id=pid,expected_rev=1,fields={'base':'Cannot write through links'})
        else:
            call(app,'create_draft',request_id=rid,idea='A safe draft')
    assert victim.read_bytes()==original
    assert dest.is_symlink()


def test_draft_store_ledger_directory_symlink_preserves_outside_data(app,tmp_path):
    import shutil
    sp=app.studio.sp
    outside=tmp_path/'outside-ledger';outside.mkdir()
    victim=outside/'events.jsonl';victim.write_bytes(b'unchanged\n')
    shutil.rmtree(sp.ledger_dir)
    sp.ledger_dir.symlink_to(outside,target_is_directory=True)
    with pytest.raises(ValueError,match='symlink'):
        call(app,'create_draft',request_id='ledger-parent',idea='Preserve outside records')
    assert victim.read_bytes()==b'unchanged\n'
    assert not list(sp.posts_dir.glob('*.json'))


def test_edit_rechecks_sidecars_after_snapshot_read(app,tmp_path,monkeypatch):
    post=call(app,'create_draft',request_id='late-sidecar',idea='Preserve the source')['post']
    victim=tmp_path/'late-victim';victim.write_bytes(b'never truncate')
    original_read=app._post

    def plant_after_read(pid):
        snapshot=original_read(pid)
        lock=app.studio.sp.posts_dir/(pid+'.lock')
        lock.unlink();lock.symlink_to(victim)
        return snapshot

    monkeypatch.setattr(app,'_post',plant_after_read)
    with pytest.raises(ValueError,match='symlink'):
        call(app,'edit_draft',post_id=post['id'],expected_rev=1,fields={'base':'Blocked'})
    assert victim.read_bytes()==b'never truncate'
    assert store.load_post(app.studio.sp,post['id'])['rev']==1


def test_create_conflict_replay_rechecks_store_sidecars(app,tmp_path,monkeypatch):
    original_save=store.save_post
    victim=tmp_path/'replay-victim';victim.write_bytes(b'preserve on retry')

    def concurrent_create_then_link(sp,post,**kwargs):
        current=original_save(sp,post,**kwargs)
        lock=sp.posts_dir/(post['id']+'.lock')
        lock.unlink();lock.symlink_to(victim)
        raise RevConflict(current)

    monkeypatch.setattr(store,'save_post',concurrent_create_then_link)
    with pytest.raises(ValueError,match='symlink'):
        call(app,'create_draft',request_id='late-replay',idea='A concurrent retry')
    assert victim.read_bytes()==b'preserve on retry'


def test_agent_production_tools_keep_release_fields_out(app):
    p=call(app,'create_draft',request_id='production-work',idea='A story')['post']
    result=call(app,'update_production',post_id=p['id'],expected_rev=p['rev'],fields={'title':'The story','status':'writing','due_date':'2026-11-04'})
    assert result['post']['production']['due_date']=='2026-11-04'
    assert not {'queue','approvals','results'} & set(result['post'])
    with pytest.raises(ValueError):call(app,'update_production',post_id=p['id'],expected_rev=2,fields={'approvals':[]})
    reply=call(app,'add_comment',post_id=p['id'],expected_rev=2,text='An editorial observation')
    assert reply['comments'][0]['actor']=={'kind':'agent','name':'codex'}
    assert len(call(app,'get_comments',post_id=p['id'])['comments'])==1
    assert call(app,'production_context')['workflows']['items'][0]['id']=='wf_default'
