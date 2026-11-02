"""No key/Keychain delegation: exact job scope and recoverable parent loss."""
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
import pytest
from .test_episode_workshop import managed,ready,run
from .test_workbench import studio
from ave.studio import episode_workshop as E,episode_registry as R
from ave.studio.episode_worker_bridge import Parent
from ave.studio.workbench import job_path,write,read


def job_for(studio,eid,operation='episode-intake'):
    with R.resolve(studio,eid) as (ep,row):args={'episode':eid,'grant_revision':1,'input_sha256':E.state_hash(ep,row,operation,'main'),'session':'main','request_id':str(uuid.uuid4())}
    job={'id':'job_'+uuid.uuid4().hex[:24],'operation':operation,'args':args,'status':'queued','by':'synthetic','created':'2026-10-29T00:00:00Z'}
    job['episode_descriptor']=E.descriptor(studio,operation,args);write(studio.sp,job_path(studio.sp,job['id']),job);return job



def assert_heartbeat_does_not_hash_media(bridge,job,monkeypatch):
    original=E.sha256_file
    def counted(path):
        if str(path).lower().endswith(('.wav','.mp4','.m4a','.mov','.mp3')):raise AssertionError('heartbeat rehashed a media blob')
        return original(path)
    with monkeypatch.context() as m:
        m.setattr(E,'sha256_file',counted)
        m.setattr(R,'assert_sources',lambda *a:(_ for _ in ()).throw(AssertionError('heartbeat rehashed registered sources')))
        for _ in range(3):bridge.handle({'job_id':job['id'],'action':'snapshot','data':{}})

def test_bridge_scope_and_revocation(studio,tmp_path):
    eid,source=managed(studio,tmp_path);job=job_for(studio,eid);bridge=Parent(studio,job)
    try:
        result=bridge.handle({'job_id':job['id'],'action':'snapshot','data':{}});assert len(result['rows'])==1
        for message in ({'job_id':'wrong','action':'snapshot','data':{}},{'job_id':job['id'],'action':'confirm','data':{}},{'job_id':job['id'],'action':'snapshot','data':{'path':'/tmp'}}):
            with pytest.raises(ValueError):bridge.handle(message)
        with pytest.raises(ValueError):bridge.handle({'job_id':job['id'],'action':'artifact','data':{'file':'outside','kind':'assembly','source':None,'edit':{},'details':{}}})
        R.revoke(studio,eid)
        with pytest.raises(ValueError,match='grant changed|revoked'):bridge.handle({'job_id':job['id'],'action':'snapshot','data':{}})
    finally:bridge.close()


def test_live_worker_parent_loss_cancels_without_approval_key(studio,tmp_path):
    eid,source=managed(studio,tmp_path);job=job_for(studio,eid);bridge=Parent(studio,job);bridge.start()
    marker=tmp_path/'worker-started'
    # An actual separate process with no native API, no secrets, and its own
    # process group. Socket closure must end it, rather than leave it running.
    code="""import os,signal,time,sys
from ave.studio import episode_worker_bridge as b
from ave.publish import safety
safety.approval_key=lambda:(_ for _ in ()).throw(AssertionError('worker attempted approval key'))
b.CLIENT=b.Client(int(sys.argv[1]),sys.argv[2])
open(sys.argv[3],'w').write('ready')
try:
 while True:time.sleep(.1)
finally:b.CLIENT.close()
"""
    proc=subprocess.Popen([sys.executable,'-c',code,str(bridge.child.fileno()),job['id'],str(marker)],pass_fds=(bridge.child.fileno(),),start_new_session=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        deadline=time.monotonic()+10
        while not marker.exists() and proc.poll() is None and time.monotonic()<deadline:time.sleep(.02)
        assert marker.exists();bridge.close();proc.wait(timeout=5);assert proc.returncode!=0
        assert not (studio.sp.work/'production'/'episode-artifacts').exists()
    finally:
        bridge.close()
        if proc.poll() is None:proc.kill();proc.wait()


def test_worker_without_live_bridge_refuses_before_registry(studio,tmp_path):
    eid,source=managed(studio,tmp_path);job=job_for(studio,eid)
    # No keychain adapter is imported/invoked: the missing bridge refuses before
    # registry or checkpoint validation in the actual subprocess entrypoint.
    result=subprocess.run([sys.executable,'-m','ave.studio.workbench_worker',str(studio.sp.root),job['id']],capture_output=True,text=True,timeout=10)
    assert result.returncode!=0 and 'live parent verification bridge' in result.stderr
    assert read(studio.sp,job_path(studio.sp,job['id']))['status']=='queued'


@pytest.mark.parametrize('loss',['parent','revoke'])
def test_actual_worker_terminal_state_on_authority_loss(studio,tmp_path,loss):
    eid,source=managed(studio,tmp_path);job=job_for(studio,eid);bridge=Parent(studio,job);bridge.start();marker=tmp_path/'entered-operation'
    # Execute the real worker lifecycle with a bounded blocking operation. The
    # monitor must terminate that operation and persist a recoverable terminal
    # state, including when the original parent is no longer serving requests.
    code="""import sys,time
from ave.studio import workbench_worker as worker
marker=sys.argv.pop(1)
def held_operation(studio,job):
 open(marker,'w').write('running')
 time.sleep(20)
 raise AssertionError('authority-dependent operation continued after loss')
worker.execute_operation=held_operation
raise SystemExit(worker.main())
"""
    proc=subprocess.Popen([sys.executable,'-c',code,str(marker),str(studio.sp.root),job['id'],'--episode-bridge',str(bridge.child.fileno())],pass_fds=(bridge.child.fileno(),),start_new_session=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        deadline=time.monotonic()+10
        while not marker.exists() and proc.poll() is None and time.monotonic()<deadline:time.sleep(.02)
        assert marker.exists();assert read(studio.sp,job_path(studio.sp,job['id']))['status']=='running'
        if loss=='parent':bridge.close()
        else:R.revoke(studio,eid)
        proc.wait(timeout=6);terminal=read(studio.sp,job_path(studio.sp,job['id']))
        assert proc.returncode==2,proc.communicate();assert terminal['status']=='interrupted';assert 'Episode verification lost:' in terminal['diagnostics'];assert 'retry this job' in terminal['diagnostics']
        assert not (studio.sp.work/'production'/'episode-artifacts').exists()
        from ave.studio.workbench import Workbench
        wb=Workbench(studio)
        try:
            if loss=='revoke':
                with pytest.raises(ValueError,match='revoked'):wb.retry({'job_id':job['id']})
            else:
                retried=wb.retry({'job_id':job['id']});assert retried['id']==job['id']
                deadline=time.monotonic()+20
                while time.monotonic()<deadline:
                    current=wb.tail(job['id'])
                    if current['status'] not in ('queued','running'):break
                    time.sleep(.05)
                assert current['status']=='done';assert current['result']['episode']==eid
        finally:wb.close()
    finally:
        bridge.close()
        if proc.poll() is None:proc.kill();proc.wait()


def test_rough_worker_exact_binding_and_review_revocation(studio,tmp_path,monkeypatch):
    from .test_episode_workshop import confirm
    from ave.studio.workbench import Workbench
    eid,original,app=ready(studio,tmp_path)
    assembly=run(studio,eid,'episode-assembly')
    selected={**assembly['edit'],'artifact_id':assembly['artifact']['id']}
    confirm(app,eid,'assembly',selected)
    with R.resolve(studio,eid) as (ep,row):
        args={'episode':eid,'grant_revision':row['grant_revision'],'input_sha256':E.state_hash(ep,row,'episode-rough','main'),
              'session':'main','request_id':str(uuid.uuid4()),'assembly':selected,'policy':'balanced','restore':[]}
    wb=Workbench(studio)
    try:
        job=wb.start_media('episode-rough',args)
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            result=wb.tail(job['id'])
            if result['status'] not in ('queued','running'):break
            time.sleep(.05)
        assert result['status']=='done',result.get('diagnostics')
        assert result['result']['artifact']['kind']=='rough'
        pending={**job,'id':'job_'+uuid.uuid4().hex[:24],'status':'queued'}
        pending['episode_descriptor']=E.descriptor(studio,pending['operation'],pending['args'])
        write(studio.sp,job_path(studio.sp,pending['id']),pending)
        bridge=Parent(studio,pending)
        try:
            assert_heartbeat_does_not_hash_media(bridge,pending,monkeypatch)
            result=bridge.handle({'job_id':pending['id'],'action':'rough-inputs','data':{}})
            assert result['assembly']==selected
            with pytest.raises(ValueError):bridge.handle({'job_id':pending['id'],'action':'rough-inputs','data':{'assembly':selected}})
            confirm(app,eid,'bounds',{'in':.5,'out':3.5})
            with pytest.raises(ValueError,match='assembly'):bridge.handle({'job_id':pending['id'],'action':'snapshot','data':{}})
        finally:bridge.close()
    finally:
        wb.close();app.close()


def test_verified_license_withdrawal_and_exact_reattestation(studio,tmp_path):
    from ave import library
    from ave.assets import licenses
    from ave.studio import seal
    from .test_episode_workshop import recording
    file=recording(tmp_path)
    rec=library.add_file(studio.sp,file,source={'type':'own'},by='synthetic',attested=False)
    library.attest_license(studio.sp,rec['id'],'simulated human')
    assert library.coverage(studio.sp,[rec['id']])['ok']
    entry=licenses.entry_hash(library.registry.license_log(studio.sp).find(sha256=rec['sha256']))
    def event(op,entry_hash=entry):
        seal.log(studio.sp,'control').append({'schema':'license_attestation.v2','op':op,'asset':rec['id'],'sha256':rec['sha256'],'entry':entry_hash,'purpose':'license-use','by':'simulated fixture'})
    event('license_withdraw')
    assert not library.coverage(studio.sp,[rec['id']])['ok']
    event('license_attest','0'*64)
    assert not library.coverage(studio.sp,[rec['id']])['ok']
    event('license_attest')
    assert library.coverage(studio.sp,[rec['id']])['ok']
    selected=[{'id':rec['id'],'sha256':rec['sha256']}]
    closure=E.licensed_asset_closure(studio,selected)
    assert closure['selected']==selected and closure['coverage']['ok']
    old=library.registry.license_log(studio.sp).find(sha256=rec['sha256'])
    library.registry.license_log(studio.sp).append({**old,'license_text':'Changed synthetic license terms'})
    assert not library.coverage(studio.sp,[rec['id']])['ok']
    with pytest.raises(ValueError,match='attestation'):E.licensed_asset_closure(studio,selected)
    current=licenses.entry_hash(library.registry.license_log(studio.sp).find(sha256=rec['sha256']))
    event('license_attest',current)
    assert library.coverage(studio.sp,[rec['id']])['ok']
    event('license_withdraw')
    derivative=library.add_derivative(studio.sp,file,derived_from=[rec['id']],origin={'fixture':'withdrawal'},by='synthetic')
    assert not library.coverage(studio.sp,[derivative['id']])['ok']
    with pytest.raises(ValueError,match='identity'):E.licensed_asset_closure(studio,[{'id':rec['id'],'sha256':'0'*64}])


def test_audio_sequence_worker_exact_reviewed_rough_without_worker_key(studio,tmp_path,monkeypatch):
    from .test_episode_workshop import confirm,recording
    from ave import library
    from ave.assets import licenses
    from ave.studio import seal
    from ave.studio.workbench import Workbench
    eid,_,app=ready(studio,tmp_path)
    assembly=run(studio,eid,'episode-assembly');selected={**assembly['edit'],'artifact_id':assembly['artifact']['id']};confirm(app,eid,'assembly',selected)
    rough=run(studio,eid,'episode-rough',assembly=selected,policy='balanced',restore=[]);selected={**rough['edit'],'artifact_id':rough['artifact']['id']};confirm(app,eid,'rough',selected)
    clip=recording(tmp_path,'licensed-sting.wav',duration=2)
    asset=library.add_file(studio.sp,clip,source={'type':'own'},by='synthetic',attested=False)
    library.attest_license(studio.sp,asset['id'],'simulated fixture')
    with R.resolve(studio,eid) as (ep,row):
        args={'episode':eid,'grant_revision':row['grant_revision'],'input_sha256':E.state_hash(ep,row,'episode-sequence','main'),'session':'main','request_id':str(uuid.uuid4()),
              'rough':selected,'show':'Synthetic show','episode_title':'Synthetic episode','number':'1','hosts':[], 'cold_open':None,
              'opening':{'enabled':True,'components':[{'kind':'excerpt','start':.1,'end':1.1},{'kind':'asset','id':asset['id'],'sha256':asset['sha256'],'start':0,'duration':.5}]},'closing':{'enabled':False,'components':[]}}
    wb=Workbench(studio)
    try:
        job=wb.start_media('episode-sequence',args);deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            result=wb.tail(job['id'])
            if result['status'] not in ('queued','running'):break
            time.sleep(.05)
        assert result['status']=='done',result.get('diagnostics')
        assert result['result']['components']['opening']['samples']==72000
        confirm(app,eid,'sequence',result['result']['review_payload'])
        assert E.load(studio,eid)['checkpoints']['sequence']['state']=='reviewed exact'
        pending={**job,'id':'job_'+uuid.uuid4().hex[:24],'status':'queued'};pending['episode_descriptor']=E.descriptor(studio,pending['operation'],pending['args']);write(studio.sp,job_path(studio.sp,pending['id']),pending)
        bridge=Parent(studio,pending)
        try:
            assert_heartbeat_does_not_hash_media(bridge,pending,monkeypatch)
            assert bridge.handle({'job_id':pending['id'],'action':'sequence-inputs','data':{}})['assets']['coverage']['ok']
            with pytest.raises(ValueError):bridge.handle({'job_id':pending['id'],'action':'sequence-inputs','data':{'asset':asset['id']}})
            entry=licenses.entry_hash(library.registry.license_log(studio.sp).find(sha256=asset['sha256']))
            seal.log(studio.sp,'control').append({'schema':'license_attestation.v2','op':'license_withdraw','asset':asset['id'],'sha256':asset['sha256'],'entry':entry,'purpose':'license-use','by':'simulated fixture'})
            with pytest.raises(ValueError,match='attestation'):bridge.handle({'job_id':pending['id'],'action':'snapshot','data':{}})
            assert E.load(studio,eid)['checkpoints']['sequence']['state']=='stale'
        finally:bridge.close()
    finally:wb.close();app.close()


def test_withdrawal_overrides_legacy_true_and_ledger_tamper_is_refused(studio,tmp_path):
    from ave import library
    from ave.assets import licenses
    from ave.studio import seal
    from .test_episode_workshop import recording
    file=recording(tmp_path,'legacy.wav',duration=2)
    rec=library.add_file(studio.sp,file,source={'type':'own'},by='synthetic',attested=True)
    entry=library.registry.license_log(studio.sp).find(sha256=rec['sha256'])
    entry=library.registry.license_log(studio.sp).append({**entry,'attested':True})
    assert entry['attested'] is True and library.coverage(studio.sp,[rec['id']])['ok']
    seal.log(studio.sp,'control').append({'schema':'license_attestation.v2','op':'license_withdraw','asset':rec['id'],'sha256':rec['sha256'],'entry':licenses.entry_hash(entry),'purpose':'license-use','by':'simulated fixture'})
    assert not library.coverage(studio.sp,[rec['id']])['ok']
    log=seal.log(studio.sp,'control');rows=log.path.read_text().replace('simulated fixture','edited fixture');log.path.write_text(rows)
    with pytest.raises(ValueError,match='verification failed'):library.coverage(studio.sp,[rec['id']])


def test_audio_final_worker_keeps_exact_review_and_caption_sidecar(studio,tmp_path,synthetic_final_asr,monkeypatch):
    from .test_episode_workshop import confirm,recording
    from ave import library
    from ave.studio.workbench import Workbench
    eid,_,app=ready(studio,tmp_path)
    assembly=run(studio,eid,'episode-assembly');a={**assembly['edit'],'artifact_id':assembly['artifact']['id']};confirm(app,eid,'assembly',a)
    rough=run(studio,eid,'episode-rough',assembly=a,policy='balanced',restore=[]);r={**rough['edit'],'artifact_id':rough['artifact']['id']};confirm(app,eid,'rough',r)
    plan=run(studio,eid,'episode-sequence',rough=r,show='Synthetic show',episode_title='Episode',number='1',hosts=[],cold_open=None,
             opening={'enabled':True,'components':[{'kind':'excerpt','start':0,'end':1}]},closing={'enabled':False,'components':[]});confirm(app,eid,'sequence',plan['review_payload'])
    musicfile=recording(tmp_path,'bed.wav',duration=5);music=library.add_file(studio.sp,musicfile,source={'type':'own'},by='synthetic',attested=False)
    library.attest_license(studio.sp,music['id'],'simulated fixture')
    with R.resolve(studio,eid) as (ep,row):
        source_transcript=__import__('ave.publish.safety',fromlist=['sha256_file']).sha256_file(ep.artifact('transcript.json'))
        args={'episode':eid,'grant_revision':row['grant_revision'],'input_sha256':E.state_hash(ep,row,'episode-final','main'),'session':'main','request_id':str(uuid.uuid4()),
              'rough':r,'sequence':plan['review_payload'],'gain_db':0,'caption_style':'podcast','burn':False,
              'music':{'id':music['id'],'sha256':music['sha256'],'start':0,'gain_db':-20,'duck_db':-8,'fade_in_s':.1,'fade_out_s':.1}}
    wb=Workbench(studio)
    try:
        job=wb.start_media('episode-final',args);deadline=time.monotonic()+60
        while time.monotonic()<deadline:
            state=wb.tail(job['id'])
            if state['status'] not in ('queued','running'):break
            time.sleep(.05)
        assert state['status']=='done',state.get('diagnostics')
        result=state['result'];art=E.artifact(studio,eid,result['artifact']['id'])
        assert art['details']['caption']['sha256'] and result['report']['samples']==216000
        assert music['id'] in library.get(studio.sp,art['details']['asset'])['derived_from']
        with R.resolve(studio,eid) as (ep,row):assert __import__('ave.publish.safety',fromlist=['sha256_file']).sha256_file(ep.artifact('transcript.json'))==source_transcript
        confirm(app,eid,'final',{**result['edit'],'artifact_id':art['id']});assert E.load(studio,eid)['checkpoints']['final']['state']=='reviewed exact'
        from ave.studio import seal
        from ave.assets import licenses
        pending={**job,'id':'job_'+uuid.uuid4().hex[:24],'status':'queued'};pending['episode_descriptor']=E.descriptor(studio,pending['operation'],pending['args']);write(studio.sp,job_path(studio.sp,pending['id']),pending)
        model=tmp_path/'runtime'/'asr';model.mkdir(parents=True);model_file=model/'model.bin';model_file.write_bytes(b'model one')
        binary=tmp_path/'runtime'/'speech'/'fluidaudiocli';binary.parent.mkdir();binary.write_bytes(b'fixture binary')
        with monkeypatch.context() as m:
            m.setattr(E,'final_asr_configuration',lambda:{'engine':'fluidaudio-parakeet-v3','model_dir':str(model),'binary_sha256':'a'*64,'model_sha256':'b'*64,'language':None,'diarize':False})
            check=Parent(studio,pending)
            try:
                check.handle({'job_id':pending['id'],'action':'snapshot','data':{}});model_file.write_bytes(b'model two')
                with pytest.raises(ValueError,match='changed'):check.handle({'job_id':pending['id'],'action':'snapshot','data':{}})
            finally:check.close()
        bridge=Parent(studio,pending)
        try:
            assert_heartbeat_does_not_hash_media(bridge,pending,monkeypatch)
            entry=licenses.entry_hash(library.registry.license_log(studio.sp).find(sha256=music['sha256']))
            seal.log(studio.sp,'control').append({'schema':'license_attestation.v2','op':'license_withdraw','asset':music['id'],'sha256':music['sha256'],'entry':entry,'purpose':'license-use','by':'simulated fixture'})
            with pytest.raises(ValueError,match='attestation'):bridge.handle({'job_id':pending['id'],'action':'snapshot','data':{}})
            assert E.load(studio,eid)['checkpoints']['final']['state']=='stale'
        finally:bridge.close()
        cap=studio.sp.root/art['file'];cap=cap.parent/art['details']['caption']['filename'];cap.write_text('tampered')
        with pytest.raises(ValueError,match='caption bytes changed'):E.artifact(studio,eid,art['id'])
    finally:wb.close();app.close()


@pytest.mark.parametrize('interruption',['cancel','revoke'])
def test_episode_registration_rechecks_cancel_after_long_verification(studio,tmp_path,monkeypatch,interruption):
    import threading
    from ave.publish import safety
    from ave.studio.workbench import Workbench
    from .test_episode_workshop import confirm
    eid,source,app=ready(studio,tmp_path);assembly=run(studio,eid,'episode-assembly');selected={**assembly['edit'],'artifact_id':assembly['artifact']['id']};confirm(app,eid,'assembly',selected)
    with R.resolve(studio,eid) as (ep,row):args={'episode':eid,'grant_revision':row['grant_revision'],'input_sha256':E.state_hash(ep,row,'episode-rough','main'),'session':'main','request_id':str(uuid.uuid4()),'assembly':selected,'policy':'balanced','restore':[]}
    wb=Workbench(studio)
    try:
        job=wb.start_media('episode-rough',args);deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            result=wb.tail(job['id'])
            if result['status'] not in ('queued','running'):break
            time.sleep(.05)
        assert result['status']=='done',result.get('diagnostics');record=E.artifact(studio,eid,result['result']['artifact']['id']);wb.close()
        pending={**job,'status':'queued'};write(studio.sp,job_path(studio.sp,job['id']),pending);parent=Parent(studio,pending)
        data={k:record[k] for k in ('file','kind','source','edit','details')};entered=threading.Event();release=threading.Event();real=safety.sha256_file;errors=[];hits=[]
        def held(path):
            val=real(path)
            if str(path)==str(studio.sp.root/record['file']):
                hits.append(path)
                if len(hits)==2:entered.set();assert release.wait(10)
            return val
        monkeypatch.setattr(safety,'sha256_file',held)
        def register():
            try:parent.handle({'job_id':job['id'],'action':'artifact','data':data})
            except Exception as exc:errors.append(str(exc))
        thread=threading.Thread(target=register);thread.start()
        try:
            assert entered.wait(10),errors
            if interruption=='cancel':job_path(studio.sp,job['id']).with_suffix('.cancel').write_text('cancel')
            else:R.revoke(studio,eid)
            release.set();thread.join(10)
            assert errors and ('cancellation' in errors[0] or 'revoked' in errors[0] or 'grant' in errors[0])
        finally:release.set();thread.join(10);parent.close()
    finally:wb.close();app.close()


def test_long_validation_uses_extended_client_deadline():
    import socket,threading
    from ave.studio.episode_worker_bridge import Client,receive,send
    parent,child=socket.socketpair();stream=parent.makefile('rwb')
    def serve():
        try:
            while True:
                message=receive(stream)
                if message['action']=='final-broll':time.sleep(10.5)
                send(stream,{'ok':True,'result':{'action':message['action']}})
        except (ValueError,OSError):pass
    thread=threading.Thread(target=serve);thread.start();client=Client(child.detach(),'job_fixture')
    try:assert client.call('final-broll')['action']=='final-broll'
    finally:client.close();stream.close();parent.close();thread.join(3)


def test_final_registration_rechecks_withdrawal_after_last_hash(studio,tmp_path,synthetic_final_asr,monkeypatch):
    import threading
    from ave import library
    from ave.assets import licenses
    from ave.studio import seal
    from ave.studio.workbench import Workbench
    from .test_episode_workshop import confirm,recording
    eid,source,app=ready(studio,tmp_path);assembly=run(studio,eid,'episode-assembly');a={**assembly['edit'],'artifact_id':assembly['artifact']['id']};confirm(app,eid,'assembly',a)
    rough=run(studio,eid,'episode-rough',assembly=a,policy='gentle',restore=[]);r={**rough['edit'],'artifact_id':rough['artifact']['id']};confirm(app,eid,'rough',r)
    seq=run(studio,eid,'episode-sequence',rough=r,show='Synthetic',episode_title='Race',number='1',hosts=[],cold_open=None,opening={'enabled':False,'components':[]},closing={'enabled':False,'components':[]});confirm(app,eid,'sequence',seq['review_payload'])
    bed=library.add_file(studio.sp,recording(tmp_path,'race-bed.wav',duration=5),source={'type':'own'},by='synthetic',attested=False);library.attest_license(studio.sp,bed['id'],'simulated fixture')
    with R.resolve(studio,eid) as (ep,row):args={'episode':eid,'grant_revision':row['grant_revision'],'input_sha256':E.state_hash(ep,row,'episode-final','main'),'session':'main','request_id':str(uuid.uuid4()),'rough':r,'sequence':seq['review_payload'],'gain_db':0,'caption_style':'podcast','burn':False,'music':{'id':bed['id'],'sha256':bed['sha256'],'start':0,'gain_db':-20,'duck_db':-8,'fade_in_s':.1,'fade_out_s':.1}}
    wb=Workbench(studio)
    try:
        job=wb.start_media('episode-final',args);deadline=time.monotonic()+60
        while time.monotonic()<deadline:
            state=wb.tail(job['id'])
            if state['status'] not in ('queued','running'):break
            time.sleep(.05)
        assert state['status']=='done',state.get('diagnostics');record=E.artifact(studio,eid,state['result']['artifact']['id']);wb.close()
        pending={**job,'status':'queued'};write(studio.sp,job_path(studio.sp,job['id']),pending);parent=Parent(studio,pending)
        entered=threading.Event();release=threading.Event();real=E.sha256_file;errors=[];target=studio.sp.root/record['file']
        def held(path):
            value=real(path)
            if str(path)==str(target):entered.set();assert release.wait(10)
            return value
        monkeypatch.setattr(E,'sha256_file',held)
        def register():
            try:parent.handle({'job_id':job['id'],'action':'artifact','data':{k:record[k] for k in ('file','kind','source','edit','details')}})
            except Exception as exc:errors.append(str(exc))
        thread=threading.Thread(target=register);thread.start()
        try:
            assert entered.wait(15),errors
            entry=licenses.entry_hash(library.registry.license_log(studio.sp).find(sha256=bed['sha256']))
            seal.log(studio.sp,'control').append({'schema':'license_attestation.v2','op':'license_withdraw','asset':bed['id'],'sha256':bed['sha256'],'entry':entry,'purpose':'license-use','by':'simulated fixture'})
            release.set();thread.join(15);assert errors and 'attestation' in errors[0]
        finally:release.set();thread.join(15);parent.close()
    finally:wb.close();app.close()
