"""Delivery acceptance: immutable real media, actual queue/scheduler and isolated authority."""
import copy
import hashlib
import json
from datetime import datetime,timedelta,timezone
from pathlib import Path
import pytest
from PIL import Image
from ave.publish import queue as pq,safety,receipts
from ave.studio import manual_export,manual_publication,store,model,dispatch,state
from ave.studio.events import AGENT
from ave.studio.project import StudioProject
from studio_helpers import RecordingPublisher,add_asset,write_account,FakeClock
from .test_adapter_contract import build,CASES,mp4,Http,break_route
from tests.test_manual_export import make_post


@pytest.fixture(autouse=True)
def no_agent_markers(monkeypatch):
    """Synthetic adapters and temp studios only; the same tests run under a coding agent as in a human terminal."""
    for key in ('CLAUDECODE','CLAUDE_CODE_ENTRYPOINT','AVE_AGENT'):monkeypatch.delenv(key,raising=False)



def manual(tmp_path):
    sp=StudioProject(tmp_path/'studio');sp.create(title='Manual delivery sample')
    p=make_post(sp,tmp_path,variant=True,reviewed=True)
    pack=manual_export.build(sp,p['id'],expected_rev=p['rev'])['manifest']
    when=datetime.fromisoformat(pack['created'])+timedelta(seconds=1)
    req=dict(post_id=p['id'],variant_id=p['variants'][0]['id'],package_id=pack['package_id'],reviewed_payload_hash=pack['reviewed_payload_hash'],
        posted_url='https://www.instagram.com/p/synthetic',posted_at=when.isoformat(),expected_rev=p['rev'],request_id='synthetic_request_1234',by='Sofia',changed_in_native='Added licensed music in Instagram',now=when+timedelta(seconds=1))
    return sp,p,pack,req


def test_manual_report_is_exact_idempotent_and_consumes_once(tmp_path):
    sp,p,pack,req=manual(tmp_path)
    out=manual_publication._record(sp,**req)
    assert out['receipt']['source']=='human_report' and out['receipt']['certainty']=='reported'
    assert out['receipt']['reviewed_payload_hash']==pack['reviewed_payload_hash']
    assert out['changed_in_native']==req['changed_in_native']
    rev=store.load_post(sp,p['id'])['rev']
    assert manual_publication._record(sp,**req)==out
    assert store.load_post(sp,p['id'])['rev']==rev
    with pytest.raises(ValueError,match='different report'): manual_publication._record(sp,**{**req,'posted_url':'https://www.instagram.com/p/other'})
    item=pq.load(sp,p['variants'][0]['queue']['id'])
    assert dispatch.consumption(sp,dispatch.approval_sig(item))=='published'
    assert state.derive(store.load_post(sp,p['id'])['variants'][0],item,[],req['now'],sp)=='published'


@pytest.mark.parametrize('boundary',['queue','consumption','post','complete'])
def test_manual_forward_journal_recovers_each_transition(tmp_path,monkeypatch,boundary):
    sp,p,pack,req=manual(tmp_path)
    target={'queue':(pq,'save'),'consumption':(dispatch,'record_consumption'),'post':(store,'save_post')}.get(boundary)
    if target:
        original=getattr(*target);hit=[False]
        def crash(*a,**kw):
            if not hit[0]:hit[0]=True;raise KeyboardInterrupt('injected crash')
            return original(*a,**kw)
        monkeypatch.setattr(*target,crash)
    else:
        original=manual_publication._finish
        def crash(sp,intent):
            out=original(sp,intent);raise KeyboardInterrupt('after all writes')
        monkeypatch.setattr(manual_publication,'_finish',crash)
    with pytest.raises(KeyboardInterrupt):manual_publication._record(sp,**req)
    if target: monkeypatch.setattr(*target,original)
    else: monkeypatch.setattr(manual_publication,'_finish',original)
    assert manual_publication.recover(sp)[0]['package_id']==pack['package_id']
    assert manual_publication._record(sp,**req)['state']=='published'
    assert len(store.load_post(sp,p['id'])['variants'][0]['attempts'])==1


@pytest.mark.parametrize('tamper',['manifest','caption','media','zip'])
def test_package_tampering_refused(tmp_path,tamper):
    sp,p,pack,req=manual(tmp_path);root=sp.work/'manual_exports'/pack['package_id']
    path={'manifest':root/'manifest.json','caption':root/'caption.txt','media':root/pack['media'][0]['filename'],'zip':root.with_suffix('.zip')}[tamper]
    path.write_bytes(b'tampered')
    with pytest.raises((ValueError,OSError)):manual_publication._record(sp,**req)
    assert pq.load(sp,p['variants'][0]['queue']['id'])['status']=='approved'


def test_late_report_keeps_exported_payload_and_checks_cas(tmp_path):
    sp,p,pack,req=manual(tmp_path)
    p['copy']['base']='A later unreviewed caption';p=store.save_post(sp,p,expected_rev=p['rev'],actor=AGENT,op='edit')
    with pytest.raises(model.RevConflict):manual_publication._record(sp,**req)
    req['expected_rev']=p['rev'];req['now']+=timedelta(days=90)
    result=manual_publication._record(sp,**req)
    assert result['exported_rev']==pack['rev']
    assert store.load_post(sp,p['id'])['copy']['base']=='A later unreviewed caption'
    assert pq.load(sp,p['variants'][0]['queue']['id'])['caption']==pack['caption']


def test_expired_at_publication_and_active_sender_refused(tmp_path):
    sp,p,pack,req=manual(tmp_path)
    late=req['now']+timedelta(days=90)
    with pytest.raises(ValueError,match='expired'):manual_publication._record(sp,**{**req,'posted_at':late.isoformat(),'now':late})
    item=pq.load(sp,p['variants'][0]['queue']['id']);item['results']={'instagram':{'state':'sending'}};pq.save(sp,item)
    with pytest.raises(ValueError,match='owns'):manual_publication._record(sp,**req)


def test_no_http_or_agent_manual_record_authority(tmp_path,monkeypatch):
    sp,p,pack,req=manual(tmp_path)
    monkeypatch.setenv('AVE_AGENT','1')
    with pytest.raises(SystemExit):manual_publication.record_manual_publication(sp,**req)


@pytest.mark.parametrize('phase',['processing','scheduled','awaiting_user_publish','sandbox','draft'])
def test_pending_receipt_blocks_recreation_and_refresh_never_creates(tmp_path,phase):
    sp=StudioProject(tmp_path/'studio');sp.create(title='Receipt sample');path=tmp_path/'image.jpg';Image.new('RGB',(1080,1350)).save(path)
    class Adapter(RecordingPublisher):
        def publish(self,item,video,progress):
            self.publish_calls+=1;return receipts.result('instagram','operation',phase=phase,object_kind='upload')
    a=Adapter('instagram');pubs={'instagram':a}
    item=pq.create(sp,files=[path],media_type='image',platforms=['instagram'],title='Caption',caption='Caption',alt_texts=['A sample'],publishers=pubs)
    pq.approve(sp,item['id'],'fixture',item['sha256'][:12],publishers=pubs)
    out=pq.run(sp,item['id'],send=True,publishers=pubs)
    assert out['platforms']['instagram']['outcome']==phase and not out['ok']
    pq.run(sp,item['id'],send=True,publishers=pubs);pq.refresh(sp,item['id'],publishers=pubs)
    assert a.publish_calls==1 and pq.load(sp,item['id'])['status']=='publishing'


def test_unrecognized_result_is_not_publication():
    out=receipts.normalize({'id':'upload','url':'https://example.com/operation'},{'sha256':'x'},'unknown')
    assert out['state']=='sending' and out['id'] is None


@pytest.mark.parametrize('case',[c for c in CASES if c.platform not in ('manual','tiktok')],ids=lambda c:c.id)
def test_studio_queue_adapter_scheduler_exact_text(tmp_path,mp4,case,monkeypatch):
    from ave.studio.sync import sync_queue
    from ave.schedule.tick import tick
    from ave.studio import sendgate
    from ave.studio.service import Studio
    from ave.library import registry
    from ave.library.ingest import default_own_entry
    rig=build(tmp_path,case,mp4)
    clock=FakeClock(datetime.now(timezone.utc).isoformat())
    media=[]
    for i,path in enumerate(rig.files):
        rec=add_asset(rig.sp,path,source_type='own',alt=f'Slide {i}')
        registry.license_log(rig.sp).append({**default_own_entry('fixture',clock.now().isoformat()),'sha256':rec['sha256'],'file':rec['file'],'bytes':rec['bytes'],'approved_by':'fixture'})
        media.append({'asset':rec['id'],'role':'image','slot':i,'alt':f'Slide {i}'})
    p=model.new_post(idea='Display title',copy={'base':'Exact reviewed words','link':'https://example.com/post','hashtags':['Craft']},media=media)
    v=model.new_variant(account=rig.acct.id,platform=case.platform,post_type='carousel' if len(media)>1 else 'feed');v.update(payload_version=3,delivery={'method':'automatic'},schedule=__import__('ave.schedule.slots',fromlist=['make_schedule']).make_schedule(clock.now().astimezone(__import__('zoneinfo').ZoneInfo('UTC')),slot=None,set_by='agent'))
    p['variants']=[v];p=store.save_post(rig.sp,p,expected_rev=None,actor=AGENT,op='fixture')
    out=sync_queue(rig.sp,p['id'],publishers=rig.pubs,now=clock.now());assert not out['problems'],out
    p=store.load_post(rig.sp,p['id']);v=p['variants'][0];item=pq.load(rig.sp,v['queue']['id'])
    pq.approve(rig.sp,item['id'],'fixture',item['sha256'][:12],publishers=rig.pubs,bind=dispatch.approval_bind(rig.sp,v))
    monkeypatch.setattr(sendgate,'authorized',lambda sp:type('Auth',(),{'ok':True})())
    report=tick(rig.sp,clock=clock,send=True,publishers=rig.pubs,host=rig.host)
    assert report.posted,report.as_dict()
    def strings(x):
        if isinstance(x,str): return [x]
        if isinstance(x,dict): return [t for v in x.values() for t in strings(v)]
        if isinstance(x,list): return [t for v in x for t in strings(v)]
        return []
    body=next(c.get('json') or c.get('data') for c in rig.http.calls if item['caption'] in strings(c.get('json') or c.get('data')))
    assert strings(body).count(item['caption'])==1
    result=store.load_post(rig.sp,p['id'])['variants'][0]['result']
    assert result['receipt']['reviewed_payload_hash']==receipts.payload_hash(item)
    before=len(rig.http.calls);tick(rig.sp,clock=clock,send=True,publishers=rig.pubs,host=rig.host);assert len(rig.http.calls)==before
    assert all(secret not in json.dumps(pq.load(rig.sp,item['id'])) for secret in rig.secret_values)

@pytest.mark.parametrize('platform',['threads','linkedin','x','bluesky'])
def test_text_only_studio_queue_and_exact_empty_image_caption(tmp_path,mp4,platform):
    from ave.studio.sync import sync_queue
    rig=build(tmp_path,next(c for c in CASES if c.id==platform),mp4)
    p=model.new_post(idea='Internal display title',copy={'base':'Words without media'})
    v=model.new_variant(account=rig.acct.id,platform=platform,post_type='feed');v.update(payload_version=3,delivery={'method':'automatic'});p['variants']=[v]
    p=store.save_post(rig.sp,p,expected_rev=None,actor=AGENT,op='fixture')
    synced=sync_queue(rig.sp,p['id'],publishers=rig.pubs);assert not synced['problems'],synced
    v=store.load_post(rig.sp,p['id'])['variants'][0];q=pq.load(rig.sp,v['queue']['id']);assert q['media_type']=='text' and pq.resolve_files(rig.sp,q)==[]
    pq.approve(rig.sp,q['id'],'fixture',q['sha256'][:12],publishers=rig.pubs,bind=dispatch.approval_bind(rig.sp,v))
    out=pq.run(rig.sp,q['id'],send=True,publishers=rig.pubs);assert out['ok'],out
    from ave.platforms.base import compose_text
    assert compose_text({'meta_v':3,'caption':'','description':'','title':'Internal title'})==''
    assert compose_text({'meta_v':2,'caption':'','description':'','title':'Legacy title'})=='Legacy title'


def test_linkedin_real_pdf_studio_queue_exact_upload_and_manual_export(tmp_path,mp4):
    from ave import library
    from ave.studio.sync import sync_queue
    from ave.library import registry
    from ave.library.ingest import default_own_entry
    rig=build(tmp_path,next(c for c in CASES if c.id=='linkedin'),mp4,routes=[
        {'method':'POST','url':'/rest/documents','response':{'value':{'uploadUrl':'https://www.linkedin.com/dms-uploads/doc','document':'urn:li:document:D1'}}},
        {'method':'PUT','url':'dms-uploads','response':{}},
        {'method':'GET','url':'/rest/documents/','response':{'status':'AVAILABLE'}},
        {'method':'POST','url':'/rest/posts','status':201,'headers':{'x-restli-id':'urn:li:share:1'}}])
    path=tmp_path/'book.pdf';Image.new('RGB',(612,792),'#f2deaa').save(path,'PDF')
    a=library.add_file(rig.sp,path,source={'type':'own'},by='fixture',alt='Book PDF')
    registry.license_log(rig.sp).append({**default_own_entry('fixture','2026-10-29T00:00:00Z'),'sha256':a['sha256'],'file':a['file'],'bytes':a['bytes'],'approved_by':'fixture'})
    p=model.new_post(idea='Document',copy={'base':'Read the document'},media=[{'slot':0,'role':'document','asset':a['id']}])
    v=model.new_variant(account=rig.acct.id,platform='linkedin',post_type='feed');v.update(payload_version=3,delivery={'method':'automatic'});p['variants']=[v]
    p=store.save_post(rig.sp,p,expected_rev=None,actor=AGENT,op='fixture')
    synced=sync_queue(rig.sp,p['id'],publishers=rig.pubs);assert not synced['problems'],synced
    p=store.load_post(rig.sp,p['id']);v=p['variants'][0];q=pq.load(rig.sp,v['queue']['id']);assert q['media_type']=='document'
    pq.approve(rig.sp,q['id'],'fixture',q['sha256'][:12],publishers=rig.pubs,bind=dispatch.approval_bind(rig.sp,v))
    package=manual_export.build(rig.sp,p['id'],v['id'])
    assert (Path(package['directory'])/package['manifest']['media'][0]['filename']).read_bytes()==path.read_bytes()
    out=pq.run(rig.sp,q['id'],send=True,publishers=rig.pubs);assert out['ok'],out
    assert next(c['data'] for c in rig.http.calls if c['method']=='PUT')==path.read_bytes()


def test_accepted_comment_timeout_never_reposts_primary(tmp_path,mp4):
    case=next(c for c in CASES if c.id=='instagram')
    from studio_helpers import DATA
    routes=json.loads((DATA/'instagram'/'ok.json').read_text())+[{'method':'POST','url':'/comments','raise':'timeout'}]
    rig=build(tmp_path,case,mp4,routes=routes,approve=False)
    q=pq.update(rig.sp,rig.item['id'],first_comment='A reviewed first comment',publishers=rig.pubs)
    pq.approve(rig.sp,q['id'],'fixture',q['sha256'][:12],publishers=rig.pubs)
    first=pq.run(rig.sp,q['id'],send=True,publishers=rig.pubs);assert first['ok'],first
    rec=pq.load(rig.sp,q['id'])['results']['instagram'];assert rec['receipt']['adjuncts']['first_comment']['state']=='uncertain'
    before=len(rig.http.calls);pq.run(rig.sp,q['id'],send=True,publishers=rig.pubs);assert len(rig.http.calls)==before
    assert rec['host_leases'] and not rig.host.deleted


def test_linkedin_missing_id_is_uncertain_and_private_refuses_before_upload(tmp_path,mp4):
    case=next(c for c in CASES if c.id=='linkedin');from studio_helpers import DATA
    routes=json.loads((DATA/'linkedin'/'ok.json').read_text())
    routes=break_route(routes,lambda r:r['url'].endswith('/rest/posts'),headers={},response={},status=201)
    rig=build(tmp_path,case,mp4,routes=routes)
    out=pq.run(rig.sp,rig.item['id'],send=True,publishers=rig.pubs);assert out['platforms']['linkedin']['outcome']=='uncertain'
    count=sum(c['url'].endswith('/rest/posts') for c in rig.http.calls)
    pq.run(rig.sp,rig.item['id'],send=True,publishers=rig.pubs)
    assert sum(c['url'].endswith('/rest/posts') for c in rig.http.calls)==count


def test_manual_external_link_requires_explicit_explanation(tmp_path):
    sp,p,pack,req=manual(tmp_path)
    req['posted_url']='https://example.com/custom-publication'
    with pytest.raises(ValueError,match='outside the selected platform'):manual_publication._record(sp,**req)
    out=manual_publication._record(sp,**req,url_mismatch_explanation='Our public short-link redirects to this post')
    assert out['url_mismatch_explanation']


def test_linkedin_write_only_permission_refuses_images_but_keeps_text(tmp_path,mp4):
    from dataclasses import replace
    rig=build(tmp_path,next(c for c in CASES if c.id=='linkedin'),mp4)
    rig.adapter.account=replace(rig.acct,connection={'scopes':['w_member_social']})
    assert any('write-only' in x for x in rig.adapter.preflight(rig.item,rig.files[0]))
    assert not rig.adapter.preflight({**rig.item,'media_type':'text'},None)
    assert not rig.http.calls


def test_linkedin_readiness_failure_never_creates_post(tmp_path,mp4):
    from studio_helpers import DATA
    routes=json.loads((DATA/'linkedin'/'ok.json').read_text())
    routes=break_route(routes,lambda r:r['method']=='GET' and '/rest/images/' in r['url'],response={'status':'PROCESSING_FAILED'})
    rig=build(tmp_path,next(c for c in CASES if c.id=='linkedin'),mp4,routes=routes)
    out=pq.run(rig.sp,rig.item['id'],send=True,publishers=rig.pubs)
    assert not out['ok'] and not any(c['url'].endswith('/posts') for c in rig.http.calls)


def test_manual_and_sender_have_one_operation_winner(tmp_path,monkeypatch):
    import threading
    from ave.schedule.tick import tick_lock
    sp,p,pack,req=manual(tmp_path)
    entered=threading.Event();release=threading.Event()
    def sender():
        with tick_lock(sp) as got:
            assert got;entered.set();release.wait(5)
            item=pq.load(sp,p['variants'][0]['queue']['id']);item['results']={'instagram':{'state':'sending'}};pq.save(sp,item)
    thread=threading.Thread(target=sender);thread.start();assert entered.wait(5)
    try:
        with pytest.raises(ValueError,match='sender is active'):manual_publication._record(sp,**req)
    finally:release.set();thread.join(5)
    with pytest.raises(ValueError,match='owns'):manual_publication._record(sp,**req)
    assert not manual_publication._intents(sp)


@pytest.mark.parametrize('phase',['processing','scheduled','awaiting_user_publish','sandbox','draft'])
def test_real_worker_restart_preserves_accepted_operation(tmp_path,monkeypatch,phase):
    import os,subprocess,sys
    from .test_sched_tick import ready,NOW,ROOT
    monkeypatch.setenv('AVE_APPROVAL_KEY','k'*64)
    w,pid,vid=ready(tmp_path)
    log=tmp_path/'creates.txt'
    code=f'''
import sys
sys.path[:0]=[{str(ROOT/'src')!r},{str(ROOT/'tests')!r}]
from studio_helpers import FakeClock,RecordingPublisher
from ave.studio.project import StudioProject
from ave.schedule.tick import tick
from ave.publish import receipts
from ave.publish import safety;age=safety.approval_age_days;safety.approval_age_days=lambda at,now=None:age(at,FakeClock({NOW!r}).now().timestamp() if now is None else now)
class Accepted(RecordingPublisher):
 def publish(self,item,video,progress):
  with open({str(log)!r},'a') as f:f.write('create\\n')
  return receipts.result('instagram','exact-operation',phase={phase!r},object_kind='upload')
sp=StudioProject({str(w.sp.root)!r})
r=tick(sp,clock=FakeClock({NOW!r}),send=True,publishers={{'ig-main':Accepted('instagram')}})
print(len(r.posted))
'''
    env={k:v for k,v in os.environ.items() if k not in ('CLAUDECODE','CLAUDE_CODE_ENTRYPOINT','AVE_AGENT')}
    env.update(AVE_APPROVAL_KEY='k'*64,HOME=str(tmp_path/'home'))
    for _ in range(2):
        run=subprocess.run([sys.executable,'-c',code],env=env,capture_output=True,text=True,timeout=30)
        assert run.returncode==0,(run.stdout,run.stderr)
        assert run.stdout.strip()=='0'
    assert log.read_text()=='create\n'
    assert w.item(pid,vid)['results']['instagram']['receipt']['phase']==phase
    assert w.sp.schedule_dir.joinpath('heartbeat.json').is_file()


def test_expired_pending_host_lease_stays_locked_on_refresh(tmp_path):
    from .test_sched_tick import ready
    class Pending(RecordingPublisher):
        def publish(self,item,video,progress):
            self.publish_calls+=1
            progress({'host_leases':[{'key':'synthetic/image.jpg','sha256':item['sha256'],'expires':'2026-10-20T00:00:00Z','provider':'instagram'}]})
            return receipts.result('instagram','pull-operation',phase='processing',object_kind='container')
    a=Pending('instagram');w,pid,vid=ready(tmp_path,pub=a)
    w.tick(send=True)
    q=w.item(pid,vid);r=q['results']['instagram']
    assert r['host_leases'][0]['sha256']==q['sha256']
    q=pq.refresh(w.sp,q['id'],publishers={'instagram':a})
    assert q['results']['instagram']['host_lease_diagnostics']['kind']=='lease_expired'
    assert q['status']=='publishing' and a.publish_calls==1
    w.tick(send=True);assert a.publish_calls==1
    v=store.load_post(w.sp,pid)['variants'][0]
    assert 'host_lease_expired' in state.explain(v,q,[],w.clock.now(),w.sp)['flags']


def test_retry_after_survives_scheduler(tmp_path):
    from .test_sched_tick import ready
    from ave.platforms.base import QuotaError
    class Limited(RecordingPublisher):
        def publish(self,item,video,progress):raise QuotaError('Synthetic quota',retry_after=900)
    w,pid,vid=ready(tmp_path,pub=Limited('instagram'))
    w.tick(send=True)
    q=w.item(pid,vid)
    assert q['results']['instagram']['diagnostics']['retry_after']==900
    v=store.load_post(w.sp,pid)['variants'][0]
    assert datetime.fromisoformat(v['attempts'][-1]['retry_at'].replace('Z','+00:00'))>=w.clock.now()+timedelta(seconds=900)


def test_reel_cover_feed_and_exact_empty_caption_studio_queue(tmp_path,mp4,monkeypatch):
    from dataclasses import replace
    from ave.studio.sync import sync_queue
    from ave.schedule.tick import tick
    from ave.studio import sendgate
    from ave.library import registry
    from ave.library.ingest import default_own_entry
    from ave.schedule.slots import make_schedule
    from zoneinfo import ZoneInfo
    rig=build(tmp_path,next(c for c in CASES if c.id=='instagram'),mp4)
    rig.adapter.account=replace(rig.acct,post_types=['reel'])
    clock=FakeClock(datetime.now(timezone.utc).isoformat());cover=tmp_path/'cover.jpg';Image.new('RGB',(1080,1920),'#b49b71').save(cover)
    media=[]
    for i,(path,role) in enumerate(((mp4,'video'),(cover,'cover'))):
        from ave import library
        a=library.add_file(rig.sp,path,source={'type':'own'},by='fixture',alt='')
        # add_asset probes suffix itself; registry is append-only and these are real bytes.
        registry.license_log(rig.sp).append({**default_own_entry('fixture',clock.now().isoformat()),'sha256':a['sha256'],'file':a['file'],'bytes':a['bytes'],'approved_by':'fixture'})
        media.append({'slot':i,'role':role,'asset':a['id'],'alt':''})
    p=model.new_post(idea='Display title must not leak',copy={'base':''},media=media)
    v=model.new_variant(account=rig.acct.id,platform='instagram',post_type='reel');v.update(payload_version=3,delivery={'method':'automatic'},presentation={'feed_presence':False},schedule=make_schedule(clock.now().astimezone(ZoneInfo('UTC')),slot=None,set_by='agent'));p['variants']=[v]
    p=store.save_post(rig.sp,p,expected_rev=None,actor=AGENT,op='fixture')
    out=sync_queue(rig.sp,p['id'],publishers=rig.pubs);assert not out['problems'],out
    v=store.load_post(rig.sp,p['id'])['variants'][0];q=pq.load(rig.sp,v['queue']['id'])
    pq.approve(rig.sp,q['id'],'fixture',q['sha256'][:12],publishers=rig.pubs,bind=dispatch.approval_bind(rig.sp,v))
    monkeypatch.setattr(sendgate,'authorized',lambda sp:type('Auth',(),{'ok':True})())
    report=tick(rig.sp,clock=clock,send=True,publishers=rig.pubs,host=rig.host);assert report.posted,report.as_dict()
    body=next(c['json'] for c in rig.http.calls if c['method']=='POST' and c['url'].endswith('/media'))
    assert body['media_type']=='REELS' and body['share_to_feed'] is False and body['caption']==''
    assert body['cover_url']!=body['video_url'] and len(rig.host.puts)==2
    assert rig.host.puts[1]['sha256']==safety.sha256_file(cover)
    r=pq.load(rig.sp,q['id'])['results']['instagram'];assert len(r['host_leases'])==2
    assert all(x['provider_reference'] for x in r['host_leases'])


def test_portable_manual_package_contains_no_queue_authority(tmp_path):
    import zipfile
    sp,p,pack,req=manual(tmp_path)
    q=pq.load(sp,p['variants'][0]['queue']['id']);approval=q['approvals'][-1]
    protected=manual_export.load_review_snapshot(sp,pack)
    assert protected['approvals']==q['approvals']
    data=json.dumps({'response':pack,'history':manual_export.history(sp,p['id']),'describe':manual_export.describe(sp,p['id'])})
    with zipfile.ZipFile(sp.work/'manual_exports'/(pack['package_id']+'.zip')) as z:
        data+=''.join(z.read(n).decode('utf-8') for n in z.namelist() if n.endswith(('.json','.txt','.html')))
    assert 'reviewed_snapshot' not in data and '"approvals"' not in data
    for key in ('sig','signature'):
        if approval.get(key):assert approval[key] not in data
    assert manual_publication._record(sp,**req)['receipt']['reviewed_payload_hash']==pack['reviewed_payload_hash']


def test_tampered_local_review_attachment_cannot_authorize_report(tmp_path):
    sp,p,pack,req=manual(tmp_path)
    file=sp.ledger_dir/'manual_reviews'/(pack['package_id']+'.json');value=json.loads(file.read_text());value['snapshot']['caption']='Changed';file.write_text(json.dumps(value))
    with pytest.raises(ValueError,match='signature'):manual_publication._record(sp,**req)


@pytest.mark.parametrize('platform',['instagram','threads','x','bluesky','pinterest'])
def test_public_only_adapter_never_silently_promotes_private(tmp_path,mp4,platform):
    rig=build(tmp_path,next(c for c in CASES if c.id==platform),mp4)
    assert any('public creation only' in x for x in rig.adapter.preflight({**rig.item,'visibility':'private'},rig.files[0]))
    from ave.publish.adapters.base import PublishError
    with pytest.raises(PublishError,match='public creation only'):rig.adapter.publish({**rig.item,'visibility':'private'},rig.files[0],lambda _:None)
    assert not rig.http.calls


def test_tiktok_accepted_pending_failure_refresh_requires_new_review(tmp_path,mp4,monkeypatch):
    from ave.studio import delivery,sendgate
    from ave.studio.service import Studio
    from ave.studio.sync import sync_queue
    from ave.library import registry
    from ave.library.ingest import default_own_entry
    from ave.schedule.slots import make_schedule
    from ave.schedule.tick import tick
    from zoneinfo import ZoneInfo
    rig=build(tmp_path,next(c for c in CASES if c.id=='tiktok'),mp4)
    status=next(r for r in rig.http.routes if 'status/fetch' in r['url'])
    status['response']['data']={'status':'PROCESSING_UPLOAD'}
    clock=FakeClock(datetime.now(timezone.utc).isoformat())
    from ave import library
    rec=library.add_file(rig.sp,rig.files[0],source={'type':'own'},by='fixture')
    registry.license_log(rig.sp).append({**default_own_entry('fixture',clock.now().isoformat()),'sha256':rec['sha256'],'file':rec['file'],'bytes':rec['bytes'],'approved_by':'fixture'})
    p=model.new_post(idea='TikTok delivery',copy={'base':'Reviewed caption'},media=[{'asset':rec['id'],'role':'video','slot':0,'alt':''}])
    v=model.new_variant(account=rig.acct.id,platform='tiktok',post_type='feed')
    v.update(payload_version=3,delivery={'method':'automatic'},schedule=make_schedule(clock.now().astimezone(ZoneInfo('UTC')),slot=None,set_by='agent'))
    p['variants']=[v];p=store.save_post(rig.sp,p,expected_rev=None,actor=AGENT,op='fixture')
    pubs={rig.acct.id:rig.adapter};studio=Studio(rig.sp,clock=clock,publishers=pubs,host=rig.host)
    assert not sync_queue(rig.sp,p['id'],publishers=pubs)['problems']
    v=store.load_post(rig.sp,p['id'])['variants'][0];qid=v['queue']['id'];q=pq.load(rig.sp,qid)
    pq.approve(rig.sp,qid,'fixture',q['sha256'][:12],publishers=pubs,bind=dispatch.approval_bind(rig.sp,v))
    monkeypatch.setattr(sendgate,'authorized',lambda sp:type('Auth',(),{'ok':True})())
    assert tick(rig.sp,clock=clock,send=True,publishers=pubs).deferred
    q=pq.load(rig.sp,qid);sig=dispatch.approval_sig(q)
    assert q['results']['tiktok']['receipt']['phase']=='processing' and dispatch.consumption(rig.sp,sig)=='open'
    creates=lambda:len([c for c in rig.http.calls if 'inbox/video/init' in c['url']])
    assert creates()==1
    # A failed read is not proof of absence; the same accepted operation remains pending.
    status['raise']='timeout';delivery.refresh(studio,p['id'],v['id'])
    q=pq.load(rig.sp,qid);assert q['results']['tiktok']['state']=='processing'
    assert q['results']['tiktok']['refresh_diagnostics'] and dispatch.consumption(rig.sp,sig)=='open'
    status.pop('raise');status['response']['data']={'status':'FAILED','fail_reason':'video_format_check_failed'}
    delivery.refresh(studio,p['id'],v['id'])
    q=pq.load(rig.sp,qid);nv=store.load_post(rig.sp,p['id'])['variants'][0]
    assert q['status']=='failed' and q['delivery_review_required']
    assert nv['result']['receipt']['phase']=='failed' and nv['result']['receipt']['object']['id']==q['results']['tiktok']['publish_id']
    assert nv['attempts'][-1]['outcome']=='provider_failed' and not nv['attempts'][-1].get('retry_at')
    assert dispatch.consumption(rig.sp,sig)=='review_required' and not dispatch.open_dispatches(rig.sp)
    assert pq.send_blockers(rig.sp,q) and creates()==1
    tick(rig.sp,clock=clock,send=True,publishers=pubs);delivery.refresh(studio,p['id'],v['id']);assert creates()==1
    # Preparing is reversible and grants no authority. A fresh fixture approval is required.
    assert not sync_queue(rig.sp,p['id'],publishers=pubs)['problems']
    q=pq.load(rig.sp,qid);assert q['status']=='awaiting_approval' and pq.send_blockers(rig.sp,q)
    clock.advance(seconds=2)
    pq.approve(rig.sp,qid,'fixture retry review',q['sha256'][:12],publishers=pubs,bind=dispatch.approval_bind(rig.sp,nv))
    status['response']['data']={'status':'SEND_TO_USER_INBOX'}
    assert tick(rig.sp,clock=clock,send=True,publishers=pubs).deferred
    assert creates()==2 and pq.load(rig.sp,qid)['results']['tiktok']['state']=='awaiting_user_publish'


@pytest.mark.parametrize('platform,shape,option', [('instagram','image','first_comment'),('instagram','video','cover'),('instagram','video','presentation'),('x','text',None),('linkedin','document',None)])
def test_selected_aggregator_mode_refuses_unsupported_reviewed_payload(tmp_path,mp4,monkeypatch,platform,shape,option):
    from ave.platforms.registry import adapter_for
    from ave.platforms.accounts import load_account
    from ave.studio.service import Studio
    from ave.studio import delivery
    from ave.studio.sync import sync_queue
    from ave.publish.adapters.base import PublishError
    from ave.library import registry
    from ave.library.ingest import default_own_entry
    sp=StudioProject(tmp_path/'studio');sp.create(title='Selected aggregator')
    write_account(sp,'chosen',platform,mode='aggregator',post_types=['feed','reel','text','document'])
    cfg={'base_url':'https://fixture.invalid','auth':{'header':'X-Key'},'networks':{platform:platform},'upload':{'path':'/upload','id_path':'id'},'post':{'path':'/post','json':{'caption':'{caption}','media':'{media_ids}','alt':'{alt_texts}'}}}
    config=tmp_path/'aggregator.yaml';import yaml;config.write_text(yaml.safe_dump(cfg));monkeypatch.setenv('AVE_AGGREGATOR_CONFIG',str(config))
    http=Http([]);a=adapter_for(load_account(sp,'chosen'),http=http)
    media=[]
    paths=[]
    if shape!='text':
        if shape=='video': paths=[mp4]
        elif shape=='document':
            from pypdf import PdfWriter
            doc=tmp_path/'document.pdf';writer=PdfWriter();writer.add_blank_page(width=100,height=100);writer.write(doc);paths=[doc]
        else:
            jpg=tmp_path/'picture.jpg';Image.new('RGB',(1080,1080),'blue').save(jpg);paths=[jpg]
        if option=='cover':
            cover=tmp_path/'cover.jpg';Image.new('RGB',(1080,1920),'green').save(cover);paths.append(cover)
        for i,path in enumerate(paths):
            from ave import library
            asset=library.add_file(sp,path,source={'type':'own'},by='fixture',alt='Reviewed image')
            registry.license_log(sp).append({**default_own_entry('fixture','2026-10-29T00:00:00Z'),'sha256':asset['sha256'],'file':asset['file'],'bytes':asset['bytes'],'approved_by':'fixture'})
            media.append({'asset':asset['id'],'role':'cover' if i else shape,'slot':i,'alt':'Reviewed image' if shape=='image' else ''})
    p=model.new_post(idea='Selected mode',copy={'base':'Exact caption','first_comment':'Reviewed adjunct' if option=='first_comment' else ''},media=media)
    v=model.new_variant(account='chosen',platform=platform,post_type='reel' if shape=='video' else 'text' if shape=='text' else 'feed');v.update(payload_version=3,delivery={'method':'automatic'})
    if option=='presentation':v['presentation']={'feed_presence':False}
    p['variants']=[v];p=store.save_post(sp,p,expected_rev=None,actor=AGENT,op='fixture')
    # Actual selected account is resolved even without an injected publisher map.
    out=sync_queue(sp,p['id']);assert out['problems'],out
    q=state.load_queue_item(sp,store.load_post(sp,p['id'])['variants'][0])
    assert q and any('aggregator' in x for x in q['preflight_problems']),out
    with pytest.raises(ValueError,match='preflight'):pq.approve(sp,q['id'],'fixture',q['sha256'][:12])
    with pytest.raises(PublishError,match='aggregator'):a.publish(q,paths[0] if paths else None,lambda _:None)
    assert not http.calls
    c=delivery.capabilities(Studio(sp),p['id'],v['id'])
    assert c['selected_adapter']['mode']=='aggregator' and c['selected_adapter']['first_comment'] is False
    assert c['implemented_shapes']==['video','image','carousel']
    assert c['problems']


def test_aggregator_caption_only_template_cannot_claim_media_delivery(tmp_path):
    from ave.publish.adapters.aggregator import AggregatorPublisher
    a=AggregatorPublisher(config={'post':{'json':{'caption':'{caption}'}}})
    assert a.delivery_contract()['shapes']==[]
    assert a.contract_problems({'media_type':'image','meta_v':3,'caption':'Reviewed caption'})


def test_uncertain_resolution_preserves_human_receipt_in_variant(tmp_path):
    from .test_sched_tick import ready
    from ave.publish.adapters.base import Uncertain
    class Lost(RecordingPublisher):
        def publish(self,*args): raise Uncertain('synthetic lost reply')
    w,pid,vid=ready(tmp_path,pub=Lost('instagram'));w.tick(send=True)
    dispatch.resolve_uncertain(w.sp,pid,vid,posted_url='https://www.instagram.com/p/fixture',by='Fixture person',now=w.clock.now())
    q=w.item(pid,vid);v=w.variant(pid,vid)
    assert v['result']['receipt']==q['results']['instagram']['receipt']
    assert v['result']['receipt']['source']=='human_report' and v['result']['receipt']['certainty']=='reported'
    assert 'reported by a person' in state.explain(v,q,[],w.clock.now(),w.sp)['reason']


def test_wrapped_youtube_refuses_reviewed_adjunct_and_reports_video_contract(tmp_path,mp4):
    from ave.platforms.registry import adapter_for,delivery_contract
    from ave.platforms.accounts import load_account
    from ave.publish.adapters.base import PublishError
    sp=StudioProject(tmp_path/'studio');sp.create(title='Wrapped video adapter')
    write_account(sp,'video-channel','youtube',mode='native',post_types=['video'])
    a=adapter_for(load_account(sp,'video-channel'))
    item={'platforms':['youtube'],'account':'video-channel','post_type':'video','media_type':'video','meta_v':3,'title':'Display title','caption':'Exact caption','first_comment':'Reviewed comment','visibility':'public'}
    assert any('first comment' in p for p in a.preflight(item,mp4))
    with pytest.raises(PublishError,match='first comment'):a.publish(item,mp4,lambda _:None)
    assert delivery_contract(a)['shapes']==['video'] and delivery_contract(a)['first_comment'] is False


@pytest.mark.parametrize('operation',['refresh','run'])
def test_accepted_operation_absence_is_terminal_without_recreation(tmp_path,operation):
    from ave.publish.adapters.base import Absent
    class Accepted(RecordingPublisher):
        def publish(self,item,video,progress):
            self.publish_calls+=1;return receipts.result('instagram','accepted-id',phase='processing',object_kind='container')
        def recover(self,item,record):return Absent('Container EXPIRED; never published')
    from .test_sched_tick import ready
    a=Accepted('instagram');w,pid,vid=ready(tmp_path,pub=a);w.tick(send=True)
    q=w.item(pid,vid)
    if operation=='refresh':pq.refresh(w.sp,q['id'],publishers={'instagram':a})
    else:pq.run(w.sp,q['id'],send=True,publishers={'instagram':a})
    q=w.item(pid,vid)
    assert q['status']=='failed' and q['delivery_review_required'] and a.publish_calls==1
    assert q['results']['instagram']['receipt']['source']=='provider_status'
    assert q['results']['instagram']['receipt']['object']['id']=='accepted-id'
    assert pq.run(w.sp,q['id'],send=True,publishers={'instagram':a})['blocked'] and a.publish_calls==1


def test_aggregator_contract_requires_real_template_values_and_upload_identity():
    from ave.publish.adapters.aggregator import AggregatorPublisher
    cfg={'supports_visibility':True,'upload':{'path':'/upload'},'post':{'json':{'{caption}':'decorated {caption}','media':'{media_ids}'}}}
    a=AggregatorPublisher(config=cfg)
    c=a.delivery_contract();assert c['shapes']==[] and not c['exact_caption']
    assert any('visibility' in x for x in a.contract_problems({'media_type':'image','meta_v':3,'visibility':'private'}))
    cfg['upload']['id_path']='id';cfg['post']['json']={'text':'{caption}','media':'{media_ids}','privacy':'{visibility}'}
    c=a.delivery_contract();assert c['shapes']==['video','image','carousel'] and c['exact_caption'] and c['visibility']


def terminal_world(tmp_path,monkeypatch):
    from .test_sched_tick import ready
    from ave.publish.adapters.base import Absent
    from ave.studio import delivery
    # Known fixture-only key also lets a new process verify the persisted ledger.
    monkeypatch.setenv('AVE_APPROVAL_KEY','r'*64)
    class Accepted(RecordingPublisher):
        recover_calls=0
        def publish(self,item,video,progress):
            self.publish_calls+=1
            return receipts.result('instagram',f'operation-{self.publish_calls}',phase='processing' if self.publish_calls==1 else 'published')
        def recover(self,item,record):
            self.recover_calls+=1
            return Absent('Fixture accepted operation failed permanently')
    a=Accepted('instagram');w,pid,vid=ready(tmp_path,pub=a);w.studio.publishers=w.pubs
    original=copy.deepcopy(w.item(pid,vid));post_bytes=(w.sp.posts_dir/f'{pid}.json').read_bytes()
    assert w.tick(send=True).deferred and a.publish_calls==1
    return w,pid,vid,a,original,post_bytes


@pytest.mark.parametrize('rollback',['remove_flag','old_queue','old_queue_and_post'])
def test_terminal_consumption_blocks_queue_and_scheduler_rollback(tmp_path,monkeypatch,rollback):
    from ave.studio import delivery,seal
    w,pid,vid,a,original,post_bytes=terminal_world(tmp_path,monkeypatch)
    sig=dispatch.approval_sig(original);delivery.refresh(w.studio,pid,vid)
    restored=w.item(pid,vid);restored.pop('delivery_review_required')
    if rollback!='remove_flag':restored=copy.deepcopy(original)
    if rollback=='old_queue_and_post':(w.sp.posts_dir/f'{pid}.json').write_bytes(post_bytes)
    pq.save(w.sp,restored)
    assert dispatch.consumption(w.sp,sig)=='review_required'
    assert seal.log(w.sp,'dispatch').verify().ok
    assert pq.send_blockers(w.sp,restored)
    from ave.episode import Episode
    assert pq.send_blockers(Episode(w.sp.root),restored)  # Generic publish callers use the same studio ledger.
    assert pq.run(w.sp,restored['id'],send=True,publishers=w.pubs)['blocked']
    w.tick(send=True);assert a.publish_calls==1
    # Even later ordinary outcome/release records cannot free a terminal identity.
    dispatch.record_consumption(w.sp,'finished',restored,pid,vid,outcome='failed')
    dispatch.record_consumption(w.sp,'released',restored,pid,vid)
    assert dispatch.consumption(w.sp,sig)=='review_required' and pq.send_blockers(w.sp,restored)


def test_terminal_renewal_same_clock_person_payload_keeps_old_approval_consumed(tmp_path,monkeypatch):
    from ave.studio import delivery
    from ave.studio.sync import sync_queue
    w,pid,vid,a,original,post_bytes=terminal_world(tmp_path,monkeypatch)
    old=original['approvals'][-1];sig=old['sig'];delivery.refresh(w.studio,pid,vid)
    # Restore all ordinary state. Prepare still requires a new protected approval.
    pq.save(w.sp,original);(w.sp.posts_dir/f'{pid}.json').write_bytes(post_bytes)
    sync_queue(w.sp,pid,publishers=w.pubs)
    prepared=w.item(pid,vid);assert prepared['status']=='awaiting_approval' and pq.send_blockers(w.sp,prepared)
    monkeypatch.setattr(pq,'now_iso',lambda:old['at'])
    fresh=pq.approve(w.sp,prepared['id'],old['by'],prepared['sha256'][:12],publishers=w.pubs,bind=dispatch.approval_bind(w.sp,w.variant(pid,vid)))
    ap=fresh['approvals'][-1]
    assert all(ap[k]==old[k] for k in ('by','at','sha256','meta_sha256'))
    assert ap['approval_v']==2 and ap['renewed_from']==sig and ap['sig']!=sig
    assert safety.approval_problems(fresh,pq.current_sha(w.sp,fresh),now=w.clock.now().timestamp())==[]
    assert not safety.bind_problem(ap)
    assert dispatch.consumption(w.sp,sig)=='review_required'
    assert w.tick(send=True).posted and a.publish_calls==2
    assert dispatch.consumption(w.sp,ap['sig'])=='published'
    # Successful renewal never releases the old authority, even after complete rollback.
    pq.save(w.sp,original);(w.sp.posts_dir/f'{pid}.json').write_bytes(post_bytes)
    assert pq.run(w.sp,original['id'],send=True,publishers=w.pubs)['blocked']
    w.tick(send=True);assert a.publish_calls==2 and dispatch.consumption(w.sp,sig)=='review_required'


@pytest.mark.parametrize('boundary',['queue_projection','post_projection'])
def test_terminal_sealed_receipt_survives_interruption_and_new_process(tmp_path,monkeypatch,boundary):
    import os,subprocess,sys
    from ave.studio import delivery
    from ave.studio.service import Studio
    w,pid,vid,a,original,post_bytes=terminal_world(tmp_path,monkeypatch)
    sig=dispatch.approval_sig(original)
    target=pq if boundary=='queue_projection' else store
    name='save' if boundary=='queue_projection' else 'save_post';saved=getattr(target,name)
    def interrupted(*args,**kw):raise KeyboardInterrupt('Fixture interruption before mutable projection')
    monkeypatch.setattr(target,name,interrupted)
    with pytest.raises(KeyboardInterrupt):delivery.refresh(w.studio,pid,vid)
    monkeypatch.setattr(target,name,saved)
    assert dispatch.consumption(w.sp,sig)=='review_required'
    # Also restore the entire approved snapshot, not only the interrupted queue file.
    pq.save(w.sp,original);(w.sp.posts_dir/f'{pid}.json').write_bytes(post_bytes)
    code='''import json,sys
from ave.studio.project import StudioProject
from ave.studio import dispatch
from ave.publish import queue as pq
sp=StudioProject(sys.argv[1]);q=pq.load(sp,sys.argv[2])
assert dispatch.consumption(sp,dispatch.approval_sig(q))=='review_required'
assert pq.send_blockers(sp,q)
q=pq.refresh(sp,q['id'],publishers={})
assert q['status']=='failed' and q['delivery_review_required']
assert q['results']['instagram']['receipt']['phase']=='failed'
print('persisted-terminal-block-and-receipt')
'''
    env=dict(os.environ,AVE_APPROVAL_KEY='r'*64,HOME=str(tmp_path/'fixture-home'))
    result=subprocess.run([sys.executable,'-c',code,str(w.sp.root),original['id']],env=env,text=True,capture_output=True,timeout=20)
    assert result.returncode==0,(result.stdout,result.stderr)
    assert result.stdout.strip()=='persisted-terminal-block-and-receipt'
    restarted=Studio(StudioProject(w.sp.root),clock=w.clock,publishers=w.pubs)
    calls=a.recover_calls;delivery.refresh(restarted,pid,vid)
    assert a.recover_calls==calls and a.publish_calls==1
    assert w.variant(pid,vid)['result']['receipt']['phase']=='failed'


def test_terminal_ledger_tamper_blocks_direct_queue_even_when_flag_removed(tmp_path,monkeypatch):
    from ave.studio import delivery,seal
    w,pid,vid,a,original,_=terminal_world(tmp_path,monkeypatch);delivery.refresh(w.studio,pid,vid)
    pq.save(w.sp,original)
    log=seal.log(w.sp,'dispatch');lines=log.path.read_bytes().splitlines(keepends=True);log.path.write_bytes(b''.join(lines[:-1]))
    assert any('sealed dispatch' in p for p in pq.send_blockers(w.sp,original))
    assert pq.run(w.sp,original['id'],send=True,publishers=w.pubs)['blocked'] and a.publish_calls==1


@pytest.mark.parametrize('representation',['archived','retained','retained_without_hint','retained_wrong_hint'])
def test_renewed_approval_survives_repeated_historical_refresh(tmp_path,monkeypatch,representation):
    from ave.studio import delivery,seal
    from ave.studio.sync import sync_queue
    w,pid,vid,a,original,post_bytes=terminal_world(tmp_path,monkeypatch)
    delivery.refresh(w.studio,pid,vid)
    failed=copy.deepcopy(w.item(pid,vid)['results']);old=original['approvals'][-1]
    sync_queue(w.sp,pid,publishers=w.pubs);prepared=w.item(pid,vid)
    monkeypatch.setattr(pq,'now_iso',lambda:old['at'])
    fresh=pq.approve(w.sp,prepared['id'],old['by'],prepared['sha256'][:12],publishers=w.pubs,bind=dispatch.approval_bind(w.sp,w.variant(pid,vid)))
    new_sig=dispatch.approval_sig(fresh)
    history=[e for e in fresh['events'] if e['event']=='delivery_history'][-1]
    assert history['results']['instagram']['receipt']['phase']=='failed'
    assert history['approval_identity']==dispatch.approval_identity(old['sig'])
    assert old['sig'] not in json.dumps(history)  # Correlation is not approval authority.
    assert fresh['results']=={}
    if representation!='archived':
        fresh['results']=failed
        if representation=='retained_without_hint':fresh['results']['instagram'].pop('approval_identity')
        if representation=='retained_wrong_hint':fresh['results']['instagram']['approval_identity']=dispatch.approval_identity(new_sig)
        pq.save(w.sp,fresh)
    requests=a.recover_calls
    for _ in range(3):
        delivery.refresh(w.studio,pid,vid)
        q=w.item(pid,vid)
        assert q['status']=='approved' and not pq.send_blockers(w.sp,q)
        assert dispatch.consumption(w.sp,new_sig)=='free'
        assert dispatch.consumption(w.sp,old['sig'])=='review_required'
        assert a.recover_calls==requests and a.publish_calls==1
    assert not [r for r in seal.log(w.sp,'dispatch').rows() if r.get('op')=='review_required' and r.get('approval')==new_sig]
    assert w.tick(send=True).posted and a.publish_calls==2
    assert w.item(pid,vid)['results']['instagram']['approval_identity']==dispatch.approval_identity(new_sig)
    pq.save(w.sp,original);(w.sp.posts_dir/f'{pid}.json').write_bytes(post_bytes)
    assert pq.run(w.sp,original['id'],send=True,publishers=w.pubs)['blocked']
    w.tick(send=True);assert a.publish_calls==2


@pytest.mark.parametrize('interruption',['none','queue_projection','studio_projection'])
def test_adopted_terminal_association_survives_renewal_and_refresh(tmp_path,monkeypatch,interruption):
    import shutil
    from ave.episode import Episode
    from ave.studio import delivery,seal
    w,pid,vid,a,original,_=terminal_world(tmp_path,monkeypatch)
    adopted=Episode(tmp_path/'adopted');adopted.root.mkdir()
    pending=w.item(pid,vid)
    for path in pq.resolve_files(w.sp,pending):
        dest=adopted.root/path.relative_to(w.sp.root);dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,dest)
    pq.save(adopted,pending)
    p=store.load_post(w.sp,pid);p['variants'][0]['queue']['root']=str(adopted.root)
    store.save_post(w.sp,p,expected_rev=p['rev'],actor=AGENT,op='fixture.adopted')
    if interruption!='none':
        target=pq if interruption=='queue_projection' else store;name='save' if interruption=='queue_projection' else 'save_post';real=getattr(target,name)
        def crash(*args,**kw):raise KeyboardInterrupt('Fixture interrupted projection')
        monkeypatch.setattr(target,name,crash)
        with pytest.raises(KeyboardInterrupt):delivery.refresh(w.studio,pid,vid)
        monkeypatch.setattr(target,name,real)
    else:delivery.refresh(w.studio,pid,vid)
    # Recover the source queue only, then renew BEFORE studio recovery projects it.
    failed=pq.refresh(adopted,pending['id'],publishers={});old=original['approvals'][-1]
    assert dispatch.terminal_review(adopted,failed)['approval']==old['sig']
    prepared=pq.update(adopted,pending['id'],publishers=w.pubs)
    monkeypatch.setattr(pq,'now_iso',lambda:old['at'])
    fresh=pq.approve(adopted,prepared['id'],old['by'],prepared['sha256'][:12],publishers=w.pubs,bind=dispatch.approval_bind(w.sp,w.variant(pid,vid)))
    new_sig=dispatch.approval_sig(fresh);calls=a.recover_calls
    # This has no active result; historical authority must come from the verified source ledger.
    assert fresh['results']=={}
    for _ in range(2):delivery.refresh(w.studio,pid,vid)
    assert dispatch.consumption(w.sp,old['sig'])=='review_required'
    assert dispatch.consumption(w.sp,new_sig)=='free'
    assert a.recover_calls==calls and a.publish_calls==1
    q=pq.load(adopted,pending['id']);assert q['status']=='approved' and not pq.send_blockers(adopted,q)
    mirrored=[r for r in seal.log(w.sp,'dispatch').rows() if r.get('op')=='review_required']
    assert len(mirrored)==1 and mirrored[0]['approval']==old['sig']
    assert mirrored[0]['result']['receipt']['object']['id']=='operation-1'
    sent=pq.run(adopted,q['id'],send=True,publishers=w.pubs)
    assert sent['ok'] and a.publish_calls==2
    pq.save(adopted,original)
    assert pq.run(adopted,q['id'],send=True,publishers=w.pubs)['blocked'] and a.publish_calls==2


def test_historical_refresh_does_not_close_new_interrupted_dispatch(tmp_path,monkeypatch):
    from ave.studio import delivery
    from ave.studio.sync import sync_queue
    w,pid,vid,a,original,_=terminal_world(tmp_path,monkeypatch)
    delivery.refresh(w.studio,pid,vid);failed=copy.deepcopy(w.item(pid,vid)['results'])
    sync_queue(w.sp,pid,publishers=w.pubs);q=w.item(pid,vid);old=original['approvals'][-1]
    monkeypatch.setattr(pq,'now_iso',lambda:old['at'])
    fresh=pq.approve(w.sp,q['id'],old['by'],q['sha256'][:12],publishers=w.pubs,bind=dispatch.approval_bind(w.sp,w.variant(pid,vid)))
    fresh['results']=failed;pq.save(w.sp,fresh);sig=dispatch.approval_sig(fresh)
    key=dispatch.key_for(w.variant(pid,vid),fresh,w.variant(pid,vid)['schedule']['utc'])
    dispatch.log_dispatch(w.sp,'dispatch_started',pid,vid,key,w.clock.now(),AGENT)
    dispatch.record_consumption(w.sp,'started',fresh,pid,vid,key=key,now=w.clock.now())
    # Crash before the new queue operation starts; history must not settle this dispatch.
    delivery.refresh(w.studio,pid,vid)
    assert key in dispatch.open_dispatches(w.sp) and dispatch.consumption(w.sp,sig)=='open'
    assert a.publish_calls==1 and w.item(pid,vid)['status']=='approved'
