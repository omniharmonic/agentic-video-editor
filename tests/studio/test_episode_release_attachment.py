"""An immutable kept render remains saved when its later attachment is stale."""
import uuid
import signal
from contextlib import contextmanager
import pytest
from ave import library
from ave.assets import licenses
from ave.studio import episode_release as L,episode_registry as R,episode_workshop as E,video_workshop as V,store,model,seal
from ave.studio.events import Actor
from ave.studio.workbench import Workbench
from .test_workbench import studio
from .test_episode_release import finished,wait_job
from .test_episode_workshop import confirm


@contextmanager
def bounded_attachment():
    def expired(*_):raise TimeoutError('Native attachment did not finish within15s; possible lock-order regression')
    previous=signal.signal(signal.SIGALRM,expired);timer=signal.setitimer(signal.ITIMER_REAL,15)
    try:yield
    finally:signal.setitimer(signal.ITIMER_REAL,*timer);signal.signal(signal.SIGALRM,previous)


@pytest.mark.parametrize('change',['unkeep','withdraw'])
def test_attachment_rechecks_during_verification_and_cas(studio,tmp_path,synthetic_final_asr,monkeypatch,change):
    eid,original,app,final=finished(studio,tmp_path);wb=Workbench(studio)
    try:
        with R.resolve(studio,eid,verify_sources=False) as (ep,row):
            args={'episode':eid,'grant_revision':1,'input_sha256':E.state_hash(ep,row,'episode-select','main'),'session':'main','request_id':str(uuid.uuid4()),'final':final,'start':0,'end':1.5,'title':'Kept hello'}
        selected=wait_job(wb,wb.start_media('episode-select',args));keep={**selected['edit'],'artifact_id':selected['artifact']['id'],'complete_statement':True,'context_reviewed':True}
        confirm(app,eid,'keep',keep,session=keep['name'])
        for rec in library.read_all(studio.sp).values():
            if rec['source']['type']=='own':library.attest_license(studio.sp,rec['id'],'simulated fixture')
        desc=next(c for c in V.kept_clips(studio.sp)['clips'] if c['source_id']==eid);desc.pop('title')
        p=wait_job(wb,wb.start_media('video-kept',{**desc,'request_id':'attachment-kept'}))
        job=wb.start_media('video-render',{'project':p['id'],'version':p['version'],'sha256':p['sha256'],'preset':'wav','request_id':'attachment-render'})
        rendered=wait_job(wb,job)
        post=model.new_post(idea='Kept reference',by='synthetic');post=store.save_post(studio.sp,post,expected_rev=None,actor=Actor('human','synthetic'),op='test')
        body={'job_id':job['id'],'asset':rendered['assets'][0]['asset'],'post':post['id'],'expected_rev':post['rev']}
        with bounded_attachment():V.attach_audio(studio,body)
        saved=store.load_post(studio.sp,post['id'])
        assert saved['production']['sources'][0]['source_id']=='output_'+body['asset']
        with pytest.raises(model.RevConflict):V.attach_audio(studio,body)
        before=store.load_post(studio.sp,post['id']);body['expected_rev']=before['rev']
        original_coverage=library.coverage;changed=False
        def after_validation(*a,**k):
            nonlocal changed
            result=original_coverage(*a,**k)
            if not changed and a[1]==[body['asset']]:
                changed=True
                if change=='unkeep':
                    request=app.episode_checkpoints.prepare({'episode':eid,'grant_revision':1,'kind':'keep','session':keep['name'],'payload':keep,'decision':'revoke'});req=request['request']
                    app.episode_checkpoints.view({'token':request['token'],'payload_sha256':req['payload_sha256'],'preview_sha256':None});app.episode_checkpoints.prepared[request['token']]['viewed']-=3;app.episode_checkpoints.confirm_native(request['token'],'123456')
                else:
                    parent=library.get(studio.sp,next(iter(p['doc']['sources'].values()))['parent']);entry=licenses.entry_hash(library.registry.license_log(studio.sp).find(sha256=parent['sha256']))
                    seal.log(studio.sp,'control').append({'schema':'license_attestation.v2','op':'license_withdraw','asset':parent['id'],'sha256':parent['sha256'],'entry':entry,'purpose':'license-use','by':'simulated fixture'})
            return result
        monkeypatch.setattr(library,'coverage',after_validation)
        with pytest.raises(ValueError,match='changed'):V.attach_audio(studio,body)
        assert changed and store.load_post(studio.sp,post['id'])==before
        assert L.recover_committed(studio,wb.tail(job['id']))['assets']==rendered['assets']
    finally:wb.close();app.close()


@pytest.mark.parametrize('change',['unkeep','withdraw','wrong-project','wrong-job'])
def test_native_render_registration_rechecks_captured_scope(studio,tmp_path,synthetic_final_asr,monkeypatch,change):
    import copy,time,json
    from ave.studio import episode_release_bridge as B
    from ave.studio.workbench import read,write
    eid,_,app,final=finished(studio,tmp_path);wb=Workbench(studio)
    try:
        with R.resolve(studio,eid,verify_sources=False) as (ep,row):
            args={'episode':eid,'grant_revision':1,'input_sha256':E.state_hash(ep,row,'episode-select','main'),'session':'main','request_id':str(uuid.uuid4()),'final':final,'start':0,'end':1.5,'title':'Registration race fixture'}
        selected=wait_job(wb,wb.start_media('episode-select',args));keep={**selected['edit'],'artifact_id':selected['artifact']['id'],'complete_statement':True,'context_reviewed':True};confirm(app,eid,'keep',keep,session=keep['name'])
        for rec in library.read_all(studio.sp).values():
            if rec['source']['type']=='own':library.attest_license(studio.sp,rec['id'],'simulated fixture')
        descriptor=next(c for c in V.kept_clips(studio.sp)['clips'] if c['source_id']==eid);descriptor.pop('title')
        project=wait_job(wb,wb.start_media('video-kept',{**descriptor,'request_id':'render-race-kept'}))
        original=B.ReleaseParent.handle;injected=[]
        def changed(parent,message):
            if message['action']=='video-render-register' and not injected:
                injected.append(message['job_id'])
                if change=='unkeep':
                    event=copy.deepcopy(E.C.latest(studio,eid,'keep',keep['name']));event.pop('sig',None);event['checkpoint_id']='check_'+uuid.uuid4().hex;event['revoked']=True;R.control(studio.sp).append(event)
                elif change=='withdraw':
                    rec=library.get(studio.sp,next(iter(project['doc']['sources'].values()))['parent'])
                    seal.log(studio.sp,'control').append({'schema':'license_attestation.v2','op':'license_withdraw','asset':rec['id'],'sha256':rec['sha256'],'entry':rec['license']['entry'],'purpose':'license-use','by':'synthetic fixture'})
                elif change=='wrong-project':
                    path=L.directory(studio,parent.job)/'render-candidate.json';candidate=read(studio.sp,path);candidate['identity']['project']='project_'+'0'*24;write(studio.sp,path,candidate)
                else:message={**message,'job_id':'job_'+'0'*24}
            return original(parent,message)
        monkeypatch.setattr(B.ReleaseParent,'handle',changed)
        job=wb.start_media('video-render',{'project':project['id'],'version':project['version'],'sha256':project['sha256'],'preset':'wav','request_id':'native-render-race'})
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            current=wb.tail(job['id'])
            if current['status'] not in ('running','queued'):break
            time.sleep(.05)
        assert injected and current['status']=='failed' and any(word in current['diagnostics'].lower() for word in ('changed','differs','wrong-job'))
        directory=L.directory(studio,job);candidate=read(studio.sp,directory/'render-candidate.json')
        assert (studio.sp.root/candidate['file']).is_file() and not (directory/'result.json').exists()
        assert L.recover_committed(studio,job) is None
    finally:wb.close();app.close()
