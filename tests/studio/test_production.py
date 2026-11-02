"""Production is persisted editorial work, never release authority."""
import concurrent.futures
import copy
import pytest
from ave.studio import campaigns, model, production as P, store
from ave.studio.events import AGENT
from ave.studio.service import open_studio
from sched_helpers import make_world


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path)


def create(w, title='Draft'):
    return w.studio.create_post(idea=title)


def test_production_preserves_approved_payload_and_schedule(world):
    w = world
    pid, vid = w.add_post('Approved caption', at='2026-10-20T09:00', sync=True)
    w.approve(pid, vid)
    post = store.load_post(w.sp, pid)
    before = copy.deepcopy(post['variants'])
    queue_bytes = {p: p.read_bytes() for p in w.sp.root.rglob('*.json') if '/queue/' in str(p)}
    person = P.save_config(w.studio, 'people', {'name': 'Sofia', 'local_role_label': 'Reviewer'}, expected_rev=0)['items'][0]
    p = P.update(w.studio, pid, {'owner': person['id'], 'reviewer': person['id'], 'due_date': '2026-11-05', 'status': 'design'}, expected_rev=post['rev'])
    p = P.task(w.studio, pid, 'add', {'title': 'Check design'}, expected_rev=p['rev'])
    with pytest.raises(ValueError, match='Complete task'):
        P.update(w.studio, pid, {'status': 'ready'}, expected_rev=p['rev'])
    p = P.task(w.studio, pid, 'edit', {'id': p['production']['checklist'][0]['id'], 'done': True}, expected_rev=p['rev'])
    p = P.update(w.studio, pid, {'status': 'ready'}, expected_rev=p['rev'])
    P.comment(w.studio, pid, expected_rev=p['rev'], text_value='Looks good')
    c=campaigns.new(w.sp,'Approved planning')
    c=campaigns.members(w.sp,c['id'],expected_rev=c['rev'],post_revs={pid:p['rev']},add=[pid],remove=[])
    campaigns.edit(w.sp,c['id'],{'window':{'start':'2026-11-01','end':'2026-11-30'},'milestones':[{'id':'review','title':'Review day','date':'2026-11-05','done':False}]},expected_rev=c['rev'])
    restarted = open_studio(w.sp.root, clock=w.clock).production()['posts'][0]
    assert restarted['production']['owner'] == person['id']
    assert restarted['production']['status'] == 'ready'
    assert store.load_post(w.sp, pid)['variants'] == before
    assert queue_bytes and all(path.read_bytes() == value for path, value in queue_bytes.items())


@pytest.mark.parametrize('fields', [ {'due_date':'2026-02-30'}, {'start_date':'2026-11-05','due_date':'2026-11-01'}, {'queue': {}}, {'status':'published'}, {'status':'blocked'}, {'owner':'unknown'}, {'sources':[{'source_id':'missing'}]}, {'checklist':[{'id':'x','title':'x','done':'yes'}]} ])
def test_invalid_metadata(world, fields):
    p=create(world)
    with pytest.raises(ValueError): P.update(world.studio,p['id'],fields,expected_rev=p['rev'])
    assert store.load_post(world.sp,p['id'])['rev']==p['rev']


def test_cycle_cas_and_concurrent_comments(world):
    a,b=create(world,'A'),create(world,'B')
    def move(post, other):
        try: return P.update(world.studio,post['id'],{'depends_on':[other['id']]},expected_rev=post['rev'])
        except ValueError: return None
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        results=list(pool.map(lambda pair:move(*pair),[(a,b),(b,a)]))
    assert sum(x is not None for x in results)==1
    p=next(x for x in results if x)
    with pytest.raises(model.RevConflict) as e: P.update(world.studio,p['id'],{'priority':'high'},expected_rev=1)
    assert e.value.latest['rev']==p['rev']
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda n:P.comment(world.studio,p['id'],expected_rev=p['rev'],text_value=f'Note {n}'),range(2)))
    rows=P.comments(world.studio,p['id']);assert len(rows)==2
    P.comment(world.studio,p['id'],expected_rev=p['rev'],text_value='Reply',reply_to=rows[0]['id'])
    P.comment(world.studio,p['id'],expected_rev=p['rev'],comment_id=rows[0]['id'],resolved=True)
    assert P.comments(world.studio,p['id'])[0]['resolved']
    assert not world.studio.feedback()


def test_campaign_recovery_and_config(world,monkeypatch):
    w=world; a,b=create(w,'A'),create(w,'B');c=campaigns.new(w.sp,'Launch')
    original=store.save_post
    def fail(sp,p,**kw):
        if p['id']==b['id']: raise OSError('interrupted')
        return original(sp,p,**kw)
    monkeypatch.setattr(store,'save_post',fail)
    with pytest.raises(OSError): campaigns.members(w.sp,c['id'],expected_rev=c['rev'],post_revs={a['id']:a['rev'],b['id']:b['rev']},add=[a['id'],b['id']],remove=[])
    monkeypatch.setattr(store,'save_post',original)
    recovered=campaigns.load(w.sp,c['id']); assert set(recovered['posts'])=={a['id'],b['id']}
    assert recovered['rev']==2
    c=campaigns.edit(w.sp,c['id'],{'window':{'start':'2026-11-01','end':'2026-11-10'},'milestones':[{'id':'launch','title':'Launch','date':'2026-11-05','done':False}]},expected_rev=2)
    with pytest.raises(model.RevConflict): campaigns.edit(w.sp,c['id'],{'name':'Other'},expected_rev=2)
    wf=copy.deepcopy(P.DEFAULT_WORKFLOW);wf.update(id='wf_book',name='Book launch')
    P.save_config(w.studio,'workflows',wf,expected_rev=0)
    a=store.load_post(w.sp,a['id']);P.update(w.studio,a['id'],{'workflow':'wf_book','status':'writing'},expected_rev=a['rev'])
    wf['stages']=[s for s in wf['stages'] if s['id']!='writing']
    with pytest.raises(ValueError,match='Move existing'):P.save_config(w.studio,'workflows',wf,expected_rev=1)
    view={'name':'My work','filters':{'owner':''},'group':'status','sort':'due_date','mode':'table'}
    P.save_config(w.studio,'production-views',view,expected_rev=0)
    assert P.config(w.sp,'production-views')['items'][0]['name']=='My work'


def test_thumbnail_base_variant_cover_missing_and_text(world,tmp_path):
    from PIL import Image
    from studio_helpers import add_asset
    w=world;assets=[]
    for i,color in enumerate(['red','blue','green']):
        path=tmp_path/f'{i}.jpg';Image.new('RGB',(200+i*10,100),color).save(path);assets.append(add_asset(w.sp,path))
    def media(i,role='image'):return {'slot':0,'asset':assets[i]['id'],'role':role,'alt':'Meaningful preview'}
    p=w.studio.create_post(idea='Image',media=[media(0)])
    assert P.project(w.studio,p)['thumbnail']['w']==200
    p=w.studio.add_variant(p['id'],account='ig-main',platform='instagram',post_type='feed',expected_rev=p['rev'],media=[media(1)])
    assert P.project(w.studio,p)['thumbnail']['w']==210
    raw=store.load_post(w.sp,p['id']);raw['variants'][0]['media'].append({**media(2,'cover'),'slot':1})
    raw=store.save_post(w.sp,raw,expected_rev=raw['rev'],actor=AGENT,op='fixture')
    assert P.project(w.studio,raw)['thumbnail']['w']==220
    (w.sp.root/assets[2]['file']).unlink()
    assert P.project(w.studio,raw)['thumbnail']['kind']=='missing'
    text=create(w,'Text only');assert P.project(w.studio,text)['thumbnail']['kind']=='text'


def test_sidecars_and_generic_patch_cannot_bypass_validation(world,tmp_path):
    w=world;p=create(w)
    with pytest.raises(ValueError,match='typed production'):
        w.studio.edit_post(p['id'],{'production.status':'ready'},expected_rev=p['rev'])
    with pytest.raises(ValueError,match='traverse'):
        P.safe_path(w.sp,w.sp.root/'../escape.json')
    path=w.sp.work/'production';path.mkdir(exist_ok=True)
    outside=tmp_path/'outside.json';outside.write_text('{}');(path/'people.json').symlink_to(outside)
    with pytest.raises(ValueError,match='symlink'):P.config(w.sp,'people')
    assert outside.read_text()=='{}'


def test_links_name_where_the_work_lives():
    from ave.studio import production as P
    P.validate_links([{'tool':'resolve','label':'Episode 12 · rough v3','url':None},{'tool':'canva','label':'Cover','url':'https://www.canva.com/design/abc/edit'}])
    for bad in ([{'tool':'canva','label':'x','url':None}],[{'tool':'canva','label':'x','url':'javascript:alert(1)'}],[{'tool':'canva','label':'x','url':'http://canva.com/a'}],[{'tool':'figma','label':'x','url':'https://x.y'}]):
        with pytest.raises(ValueError):P.validate_links(bad)
