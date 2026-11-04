"""Two local creators: essay -> agent draft -> offline edits -> reviewed merge.

No account, network, real approval or original footage is involved.
"""
from ave import collab
from ave.agent import AgentApp
from ave.studio.desk import intake
from ave.studio.events import Actor
from ave.studio.project import StudioProject
from ave.studio.service import open_studio


def test_essay_to_agent_to_offline_conflict_and_reconciliation(tmp_path):
    a = StudioProject(tmp_path / 'maren-studio'); a.create(title='A shared show')
    b = StudioProject(tmp_path / 'imani-studio'); b.create(title='A shared show')
    sa, sb = open_studio(a.root), open_studio(b.root)
    source = intake(sa,kind='writing',text='We make room for one another. Listening takes practice.',
                    title='A shared practice',actor=Actor('human','Maren'))['source']
    aa, ab = AgentApp(sa,by='codex'), AgentApp(sb,by='claude')
    passage = aa.call('read_source', {'name':source['file']})
    assert 'Listening takes practice' in str(passage)
    post = aa.call('create_draft',{'request_id':'from-essay-1','idea':'Make room to listen',
                   'copy':{'base':'Listening takes practice.'}})['post']
    first = tmp_path / 'maren-001.json'
    collab.export_bundle(a,first,peer='imani')
    preview = collab.import_bundle(b,first,peer='maren')
    assert preview['ok']
    assert not sb.posts()
    assert collab.import_bundle(b,first,peer='maren',apply=True)['ok']
    pid=post['id']
    aa.call('edit_draft',{'post_id':pid,'expected_rev':1,'fields':{'base':'Maren leaves room for pauses.'}})
    ab.call('edit_draft',{'post_id':pid,'expected_rev':1,'fields':{'base':'Imani leaves room for questions.'}})
    reply=tmp_path/'imani-001.json'
    collab.export_bundle(b,reply,peer='maren')
    conflict=collab.import_bundle(a,reply,peer='imani',apply=True)
    assert not conflict['ok'] and conflict['conflicts']
    assert aa.call('get_post',{'post_id':pid})['post']['copy']['base']=='Maren leaves room for pauses.'
    record=collab.status(a)['conflicts'][0]
    result=collab.resolve_conflict(a,record['id'],take='incoming',expected_rev=2,apply=True)
    assert result['resolved']
    assert aa.call('get_post',{'post_id':pid})['post']['copy']['base']=='Imani leaves room for questions.'
    assert not collab.status(a)['conflicts']
    assert collab.import_bundle(a,reply,peer='imani',apply=True)['ok']
    assert (a.work/'collaboration/conflicts'/f"{record['id']}.json").is_file()
    for sp in (a,b):
        assert not list((sp.publish/'queue').glob('*.json'))
        assert not list(sp.accounts_dir.glob('*'))
