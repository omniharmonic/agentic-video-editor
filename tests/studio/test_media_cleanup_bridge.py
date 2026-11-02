"""Bounded Media cleanup authority; all attestations here are synthetic fixtures."""
import hashlib, os, subprocess, sys, time, uuid
import numpy as np
import pytest
import soundfile as sf
from ave import library
from ave.assets import licenses
from ave.episode import write_json
from ave.studio import episode_workshop as E, seal, video_workshop as V
from ave.studio.media_cleanup_bridge import MediaCleanupParent
from ave.studio.workbench import job_path,write,read
from .test_video_workshop import mutate
from .test_workbench import studio


def setup_job(studio,tmp_path):
    source=tmp_path/'voice.wav';sf.write(source,np.zeros((144000,2)),48000)
    rec=library.add_file(studio.sp,source,source={'type':'own'},by='synthetic')
    p=V.create(studio,{'title':'Bridge fixture','mode':'audio','request_id':'bridge-fixture'});p=V.add_source(studio,{'project':p['id'],'source_id':rec['id'],'expected_revision':1,'request_id':'bridge-source'})
    ep,meta=V.project(studio.sp,p['id']);tp=ep.work/'words.json';write_json(tp,{'words':[{'w':'hello','start':.1,'end':.4},{'w':'again','start':2.1,'end':2.4}]});meta['project_sources'][rec['id']]['transcript']={'file':ep.rel(tp),'sha256':hashlib.sha256(tp.read_bytes()).hexdigest(),'clock':'source'};write_json(ep.meta_path,meta)
    p=mutate(studio,p,{'kind':'insert','source':rec['id'],'in':0,'out':3})
    bed=tmp_path/'bed.wav';sf.write(bed,np.full((192000,2),.05),48000);music=library.add_file(studio.sp,bed,source={'type':'own'},by='synthetic');p=mutate(studio,p,{'kind':'music','asset':music['id'],'duck_db':0})
    args={'project':p['id'],'version':p['version'],'sha256':p['sha256'],'policy':'balanced','restore':[],'request_id':'cleanup-bridge'}
    job={'id':'job_'+uuid.uuid4().hex[:24],'operation':'video-cleanup','args':args,'status':'queued','by':'synthetic','created':'2026-10-30T00:00:00Z'};job['episode_descriptor']=E.descriptor(studio,job['operation'],args);write(studio.sp,job_path(studio.sp,job['id']),job)
    return p,music,job


def test_cleanup_bridge_scope_withdrawal_and_revision(studio,tmp_path):
    p,music,job=setup_job(studio,tmp_path);bridge=MediaCleanupParent(studio,job)
    try:
        deps=bridge.handle({'job_id':job['id'],'action':'cleanup-inputs','data':{}});assert music['id'] in deps['parents']
        for request in ({'job_id':'wrong','action':'snapshot','data':{}},{'job_id':job['id'],'action':'cleanup-inputs','data':{'asset':'other'}},{'job_id':job['id'],'action':'cleanup-inputs','data':{'project':'other'}}):
            with pytest.raises(ValueError):bridge.handle(request)
        entry=licenses.entry_hash(library.registry.license_log(studio.sp).find(sha256=music['sha256']))
        seal.log(studio.sp,'control').append({'schema':'license_attestation.v2','op':'license_withdraw','asset':music['id'],'sha256':music['sha256'],'entry':entry,'purpose':'license-use','by':'simulated fixture'})
        with pytest.raises(ValueError,match='license|attestation'):bridge.handle({'job_id':job['id'],'action':'snapshot','data':{}})
    finally:bridge.close()


def test_cleanup_bridge_detects_new_revision(studio,tmp_path):
    p,music,job=setup_job(studio,tmp_path);bridge=MediaCleanupParent(studio,job)
    try:
        mutate(studio,p,{'kind':'sound','clip':p['doc']['clips'][0]['id'],'gain_db':-3})
        with pytest.raises(ValueError,match='revision changed'):bridge.handle({'job_id':job['id'],'action':'snapshot','data':{}})
    finally:bridge.close()


@pytest.mark.parametrize('loss',['parent','withdrawal'])
def test_cleanup_worker_parent_loss_and_missing_bridge(studio,tmp_path,loss):
    p,music,job=setup_job(studio,tmp_path)
    result=subprocess.run([sys.executable,'-m','ave.studio.workbench_worker',str(studio.sp.root),job['id']],capture_output=True,text=True,timeout=10)
    assert result.returncode and 'live parent verification bridge' in result.stderr
    bridge=MediaCleanupParent(studio,job);bridge.start();marker=tmp_path/'started'
    code="""import sys,time
from ave.studio import workbench_worker as w
from ave.publish import safety
safety.approval_key=lambda:(_ for _ in ()).throw(AssertionError('worker accessed approval key'))
marker=sys.argv.pop(1)
def held(studio,job):
 open(marker,'w').write('running');time.sleep(20)
w.execute_operation=held
raise SystemExit(w.main())
"""
    proc=subprocess.Popen([sys.executable,'-c',code,str(marker),str(studio.sp.root),job['id'],'--episode-bridge',str(bridge.child.fileno())],pass_fds=(bridge.child.fileno(),),start_new_session=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        deadline=time.monotonic()+10
        while not marker.exists() and proc.poll() is None and time.monotonic()<deadline:time.sleep(.02)
        assert marker.exists()
        if loss=='parent':bridge.close()
        else:
            entry=licenses.entry_hash(library.registry.license_log(studio.sp).find(sha256=music['sha256']))
            seal.log(studio.sp,'control').append({'schema':'license_attestation.v2','op':'license_withdraw','asset':music['id'],'sha256':music['sha256'],'entry':entry,'purpose':'license-use','by':'simulated fixture'})
        proc.wait(timeout=6);state=read(studio.sp,job_path(studio.sp,job['id']));assert state['status']=='interrupted';assert proc.returncode==2
        assert V.load(studio.sp,p['id'])['version']==p['version']
    finally:
        bridge.close()
        if proc.poll() is None:proc.kill();proc.wait()


def test_cleanup_heartbeat_does_not_rehash_media(studio,tmp_path,monkeypatch):
    p,music,job=setup_job(studio,tmp_path);bridge=MediaCleanupParent(studio,job)
    original=V.sha256_file;calls=[]
    def counted(path):
        calls.append(str(path))
        if str(path).endswith('.wav'):raise AssertionError('heartbeat rehashed media bytes')
        return original(path)
    monkeypatch.setattr(V,'sha256_file',counted)
    try:
        for _ in range(3):bridge.handle({'job_id':job['id'],'action':'snapshot','data':{}})
        assert calls and all(not p.endswith('.wav') for p in calls)
    finally:bridge.close()


def test_cleanup_recovery_after_publish_before_response(studio,tmp_path):
    p,music,job=setup_job(studio,tmp_path);first=V.cleanup_programme(studio,job)
    bridge=MediaCleanupParent(studio,job)
    try:
        recovered=bridge.handle({'job_id':job['id'],'action':'cleanup-status','data':{}})
        assert recovered==first and V.load(studio.sp,p['id'])['version']==first['version']
    finally:bridge.close()


@pytest.mark.parametrize('interruption',['cancel','withdrawal'])
def test_cleanup_registration_rechecks_after_long_verification(studio,tmp_path,monkeypatch,interruption):
    import threading
    from ave.studio import episode_worker_bridge as transport
    p,music,job=setup_job(studio,tmp_path);parent=MediaCleanupParent(studio,job)
    class CandidateOnly:
        def call(self,action,**kwargs):
            if action=='cleanup-inputs':return parent.initial
            if action=='cleanup-status':return None
            raise RuntimeError('candidate captured')
    transport.CLIENT=CandidateOnly()
    try:
        with pytest.raises(RuntimeError,match='candidate captured'):V.cleanup_programme(studio,job)
    finally:transport.CLIENT=None
    entered=threading.Event();release=threading.Event();real=V.cleanup_dependencies
    def held(*args,**kwargs):
        result=real(*args,**kwargs);entered.set();assert release.wait(10);return result
    monkeypatch.setattr(V,'cleanup_dependencies',held);errors=[]
    def register():
        try:parent.handle({'job_id':job['id'],'action':'cleanup-register','data':{}})
        except Exception as exc:errors.append(str(exc))
    thread=threading.Thread(target=register);thread.start()
    try:
        assert entered.wait(10)
        if interruption=='cancel':job_path(studio.sp,job['id']).with_suffix('.cancel').write_text('cancel')
        else:
            entry=licenses.entry_hash(library.registry.license_log(studio.sp).find(sha256=music['sha256']))
            seal.log(studio.sp,'control').append({'schema':'license_attestation.v2','op':'license_withdraw','asset':music['id'],'sha256':music['sha256'],'entry':entry,'purpose':'license-use','by':'simulated fixture'})
        release.set();thread.join(10);assert errors and V.load(studio.sp,p['id'])['version']==p['version']
    finally:release.set();thread.join(10);parent.close()
