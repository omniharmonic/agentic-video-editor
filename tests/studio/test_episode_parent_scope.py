"""Every expensive parent capture has its own bounded raw verification scope."""
import copy
import uuid
import pytest
from ave.studio import episode_registry as R,episode_workshop as E,episode_checkpoints as C
from ave.studio.episode_worker_bridge import Parent
from ave.studio.workbench import write,job_path
from .test_workbench import studio
from .test_episode_release import finished
from .test_episode_workshop import confirm


def parent_job(studio,eid,op):
    a=C.latest(studio,eid,'assembly','main')['payload'];r=C.latest(studio,eid,'rough','main')['payload'];q=C.latest(studio,eid,'sequence','main')['payload']
    extra={'episode-assembly':{},'episode-rough':{'assembly':a,'policy':'gentle','restore':[]},
           'episode-sequence':{'rough':r,'show':'Synthetic show','episode_title':'Release','number':'','hosts':[],'cold_open':None,'opening':{'enabled':False,'components':[]},'closing':{'enabled':False,'components':[]}},
           'episode-final':{'rough':r,'sequence':q,'gain_db':0,'caption_style':'podcast','burn':False}}
    session='combined' if op=='episode-combined' else 'main'
    if session=='combined':extra[op]={'constituents':[a,C.latest(studio,eid,'assembly','second')['payload']]}
    with R.resolve(studio,eid,verify_sources=False) as (ep,row):
        args={'episode':eid,'grant_revision':1,'input_sha256':E.state_hash(ep,row,op,session),'session':session,'request_id':str(uuid.uuid4()),**extra[op]}
    job={'id':'job_'+uuid.uuid4().hex[:24],'operation':op,'args':args,'status':'queued','by':'synthetic'}
    job['episode_descriptor']=E.descriptor(studio,op,args);write(studio.sp,job_path(studio.sp,job['id']),job)
    return job


def second_assembly(studio,eid,app):
    confirm(app,eid,'mapping',C.latest(studio,eid,'mapping','main')['payload'],session='second');confirm(app,eid,'bounds',{'in':1,'out':2},session='second')
    with R.resolve(studio,eid,verify_sources=False) as (ep,row):args={'episode':eid,'grant_revision':1,'input_sha256':E.state_hash(ep,row,'episode-assembly','second'),'session':'second','request_id':str(uuid.uuid4())}
    job={'id':'job_'+uuid.uuid4().hex[:24],'operation':'episode-assembly','args':args};job['episode_descriptor']=E.descriptor(studio,job['operation'],args)
    result=E.execute(studio,job);confirm(app,eid,'assembly',{**result['edit'],'artifact_id':result['artifact']['id']},session='second')


@pytest.mark.parametrize('op',['episode-assembly','episode-combined','episode-rough','episode-sequence','episode-final'])
def test_parent_constructor_two_raw_scans_and_no_heartbeat_scan(studio,tmp_path,synthetic_final_asr,monkeypatch,op):
    eid,_,app,_=finished(studio,tmp_path)
    try:
        if op=='episode-combined':second_assembly(studio,eid,app)
        job=parent_job(studio,eid,op);original=R.sha256_file;calls=[]
        def measured(path):calls.append(str(path));return original(path)
        monkeypatch.setattr(R,'sha256_file',measured)
        parent=Parent(studio,job)
        try:
            assert len(calls)==2,(op,calls)
            assert R._VERIFIED_SOURCES.get() is None
            calls.clear();parent.handle({'job_id':job['id'],'action':'snapshot','data':{}})
            assert calls==[]
            with R.resolve(studio,eid,verify_sources=False) as (ep,row):(ep.raw/'added-recording.wav').write_bytes(b'unregistered fixture')
            with pytest.raises(ValueError,match='changed'):parent._current()
            assert calls==[]
        finally:parent.close()
    finally:app.close()


@pytest.mark.parametrize('change',['source','grant','checkpoint'])
def test_parent_capture_rejects_changed_dependency_and_cleans_scope(studio,tmp_path,synthetic_final_asr,monkeypatch,change):
    eid,_,app,_=finished(studio,tmp_path)
    try:
        job=parent_job(studio,eid,'episode-final');original=E.final_inputs
        def changed(studio_,ep,row,args):
            value=original(studio_,ep,row,args)
            if change=='source':
                path=ep.root/row['sources'][0]['file'];data=path.read_bytes();path.write_bytes(data[:-1]+bytes([data[-1]^1]))
            elif change=='grant':R.revoke(studio,eid)
            else:
                event=copy.deepcopy(C.latest(studio,eid,'rough','main'));event.pop('sig',None);event['revoked']=True;event['checkpoint_id']='check_'+uuid.uuid4().hex;R.control(studio.sp).append(event)
            return value
        monkeypatch.setattr(E,'final_inputs',changed)
        with pytest.raises(ValueError,match='changed|revoked'):Parent(studio,job)
        assert R._VERIFIED_SOURCES.get() is None
    finally:app.close()


@pytest.mark.parametrize('op',['episode-select','episode-package','episode-handoff'])
def test_release_parent_constructor_two_raw_scans(studio,tmp_path,synthetic_final_asr,monkeypatch,op):
    from ave.studio.episode_release_bridge import ReleaseParent
    from .test_episode_release import release_job
    eid,_,app,final=finished(studio,tmp_path)
    try:
        extras={'episode-select':{'start':0,'end':1.5,'title':'Complete thought'},'episode-package':{'chapters':[],'formats':['aac'],'originals':True,'copy':'Synthetic release'},'episode-handoff':{'name':'Synthetic handoff plan only','sources':False,'layers':False,'proxies':False,'subtitles':False}}
        job=release_job(studio,eid,op,final=final,**extras[op]);original=R.sha256_file;calls=[]
        def measured(path):calls.append(str(path));return original(path)
        monkeypatch.setattr(R,'sha256_file',measured);parent=ReleaseParent(studio,job)
        try:
            assert len(calls)==2,(op,calls)
            assert R._VERIFIED_SOURCES.get() is None
            calls.clear();parent._current();assert calls==[]
        finally:parent.close()
    finally:app.close()


def test_short_and_native_kept_capture_independent_bounded_scopes(studio,tmp_path,synthetic_final_asr,monkeypatch):
    from ave.studio import episode_release as L,video_workshop as V
    from ave.studio.episode_release_bridge import ReleaseParent
    from .test_episode_release import release_job
    eid,_,app,final=finished(studio,tmp_path,video=True)
    parent=None
    try:
        job=release_job(studio,eid,'episode-select',final=final,start=0,end=1.5,title='Complete thought');parent=ReleaseParent(studio,job);folder=L.directory(studio,job);folder.mkdir(parents=True)
        with R.resolve(studio,eid,verify_sources=False) as (ep,row):L.execute_clip(studio,ep,row,job,parent.initial,folder)
        result=parent.handle({'job_id':job['id'],'action':'episode-select-register','data':{}});parent.close();parent=None
        keep={**result['edit'],'artifact_id':result['artifact']['id'],'complete_statement':True,'context_reviewed':True};confirm(app,eid,'keep',keep,session=keep['name'])
        short=release_job(studio,eid,'episode-short',keep=keep,start=0,end=1.5,title='Complete thought',layout='crop',captions=True,reframe=None)
        descriptor=next(c for c in V.kept_clips(studio.sp)['clips'] if c['source_id']==eid);descriptor.pop('title');descriptor['request_id']='bounded-kept-capture'
        original=R.sha256_file;calls=[]
        def measured(path):calls.append(str(path));return original(path)
        monkeypatch.setattr(R,'sha256_file',measured)
        parent=ReleaseParent(studio,short);assert len(calls)==6;parent.close();parent=None
        calls.clear();L.native_kept_inputs(studio,descriptor);assert len(calls)==6
        assert R._VERIFIED_SOURCES.get() is None
    finally:
        if parent:parent.close()
        app.close()


def test_worker_reports_validation_before_expensive_checks(studio,tmp_path,synthetic_final_asr,monkeypatch):
    from ave.studio import workbench as W
    eid,_,app,_=finished(studio,tmp_path)
    try:
        job=parent_job(studio,eid,'episode-final')
        def validate(*_):
            assert W.read(studio.sp,job_path(studio.sp,job['id']))['phase']=='Verifying source and review dependencies · cancellation available'
            raise ValueError('Synthetic bounded validation refusal')
        monkeypatch.setattr(W,'validate_operation',validate)
        with pytest.raises(ValueError,match='Synthetic bounded'):W.execute_operation(studio,job)
    finally:app.close()


def test_native_render_and_managed_adoption_constructor_scope(studio,tmp_path,synthetic_final_asr,monkeypatch):
    from ave import library
    from ave.studio import video_workshop as V
    from ave.studio.workbench import Workbench
    from ave.studio.episode_release_bridge import ReleaseParent
    from .test_episode_release import release_job,wait_job
    eid,_,app,final=finished(studio,tmp_path);wb=Workbench(studio)
    try:
        job=release_job(studio,eid,'episode-select',final=final,start=0,end=1.5,title='Native input fixture')
        # Use ordinary captured worker jobs for materialization before counting.
        job_path(studio.sp,job['id']).unlink()
        selected=wait_job(wb,wb.start_media(job['operation'],job['args']));keep={**selected['edit'],'artifact_id':selected['artifact']['id'],'complete_statement':True,'context_reviewed':True};confirm(app,eid,'keep',keep,session=keep['name'])
        for rec in library.read_all(studio.sp).values():
            if rec['source']['type']=='own':library.attest_license(studio.sp,rec['id'],'simulated fixture')
        desc=next(c for c in V.kept_clips(studio.sp)['clips'] if c['source_id']==eid);desc.pop('title')
        p=wait_job(wb,wb.start_media('video-kept',{**desc,'request_id':'constructor-kept'}))
        original=R.sha256_file;calls=[]
        def measured(path):calls.append(str(path));return original(path)
        monkeypatch.setattr(R,'sha256_file',measured)
        for op,extra,expected in [('video-kept',{**desc,'request_id':'capture-only-kept'},2),('video-render',{'project':p['id'],'version':p['version'],'sha256':p['sha256'],'preset':'wav','request_id':'capture-only-render'},2),('episode-adopt',{'project':p['id'],'version':p['version'],'sha256':p['sha256'],'revision':p['revision'],'title':'Capture-only adoption','request_id':'capture-only-adoption'},0)]:
            j={'id':'job_'+uuid.uuid4().hex[:24],'operation':op,'args':extra,'status':'queued','by':'synthetic'};j['episode_descriptor']=E.descriptor(studio,op,extra);write(studio.sp,job_path(studio.sp,j['id']),j)
            calls.clear();parent=ReleaseParent(studio,j)
            try:
                assert len(calls)==expected,(op,calls)
                calls.clear();parent._current();assert calls==[]
            finally:parent.close()
        assert R._VERIFIED_SOURCES.get() is None
    finally:wb.close();app.close()


def test_occurrence_cleanup_reports_audio_then_analysis(studio,tmp_path,synthetic_final_asr,monkeypatch):
    from ave import cleanup,compile as compiler
    eid,_,app,_=finished(studio,tmp_path)
    try:
        with R.resolve(studio,eid) as (ep,row):
            _,doc=E.exact(ep,C.latest(studio,eid,'assembly','main')['payload'])
            doc=copy.deepcopy(doc);doc['combined_constituents']=[{'fixture':'exact assembly content'}]
            phases=[];original_compile=compiler.compile_audio;original_envelope=cleanup.Envelope.from_wav
            def compiled(*args,**kwargs):
                assert phases==['Preparing programme audio for cleanup']
                return original_compile(*args,**kwargs)
            def envelope(*args,**kwargs):
                assert phases==['Preparing programme audio for cleanup','Analyzing pauses and edit seams']
                return original_envelope(*args,**kwargs)
            monkeypatch.setattr(compiler,'compile_audio',compiled);monkeypatch.setattr(cleanup.Envelope,'from_wav',envelope)
            result=cleanup.analyze_occurrences(ep,doc,tmp_path/'analysis.wav',cleanup.load_policy(),progress=phases.append)
            assert result['doc']['duration']>0 and phases[-1]=='Analyzing pauses and edit seams'
    finally:app.close()


def test_parent_capture_announces_real_phase_before_expensive_constructor(studio,tmp_path,synthetic_final_asr,monkeypatch):
    from ave.studio import workbench,episode_worker_bridge
    eid,_,app,_=finished(studio,tmp_path)
    try:
        wb=workbench.Workbench(studio);job=parent_job(studio,eid,'episode-final');seen=[]
        def stopped(studio_,captured):
            current=wb.tail(job['id']);seen.append(current['phase']);assert current['status']=='queued';raise ValueError('Injected stop before worker launch')
        monkeypatch.setattr(episode_worker_bridge,'Parent',stopped)
        wb._launch(job['id'],wb._claim())
        assert seen==['Verifying captured media and review dependencies before worker starts']
        assert wb.tail(job['id'])['status']=='failed' and 'Injected stop' in wb.tail(job['id'])['diagnostics']
    finally:app.close()
