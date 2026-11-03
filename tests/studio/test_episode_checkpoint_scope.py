"""Independent exact checkpoint checks share raw scans only within one operation."""
import copy,uuid
import pytest
from ave import library
from ave.studio import episode_registry as R,episode_checkpoints as C,episode_release as L,seal
from ave.studio.episode_release_bridge import ReleaseParent
from .test_workbench import studio
from .test_episode_release import finished,release_job
from .test_episode_workshop import confirm


@pytest.mark.parametrize('checkpoint_kind',C.EXPENSIVE_KINDS)
def test_expensive_checkpoints_two_raw_scans_each_and_no_view_scan(studio,tmp_path,synthetic_final_asr,monkeypatch,checkpoint_kind):
    eid,_,app,final=finished(studio,tmp_path)
    job=release_job(studio,eid,'episode-select',final=final,start=0,end=1.5,title='Complete fixture thought');parent=ReleaseParent(studio,job);folder=L.directory(studio,job);folder.mkdir(parents=True)
    try:
        with R.resolve(studio,eid,verify_sources=False) as (ep,row):L.execute_clip(studio,ep,row,job,parent.initial,folder)
        selected=parent.handle({'job_id':job['id'],'action':'episode-select-register','data':{}})
        keep={**selected['edit'],'artifact_id':selected['artifact']['id'],'complete_statement':True,'context_reviewed':True};confirm(app,eid,'keep',keep,session=keep['name'])
        original=R.sha256_file;calls=[]
        def measured(path):calls.append(str(path));return original(path)
        monkeypatch.setattr(R,'sha256_file',measured)
        for kind in [checkpoint_kind]:
            event=C.latest(studio,eid,kind,keep['name'] if kind=='keep' else 'main');calls.clear()
            prepared=app.episode_checkpoints.prepare({'episode':eid,'grant_revision':1,'kind':kind,'session':event['session'],'payload':event['payload']})
            assert len(calls)==2,(kind,calls)
            request=prepared['request'];calls.clear()
            app.episode_checkpoints.view({'token':prepared['token'],'payload_sha256':request['payload_sha256'],'preview_sha256':request['dependencies'].get('preview_sha256')})
            assert calls==[]
            app.episode_checkpoints.prepared[prepared['token']]['viewed']-=3
            app.episode_checkpoints.confirm_native(prepared['token'],'123456')
            assert len(calls)==2,(kind,calls)
    finally:parent.close();app.close()


@pytest.mark.parametrize('change',['source','inventory','grant','checkpoint','license'])
def test_confirm_rechecks_exit_changes_before_ledger_write(studio,tmp_path,synthetic_final_asr,monkeypatch,change):
    eid,_,app,final=finished(studio,tmp_path)
    try:
        prepared=app.episode_checkpoints.prepare({'episode':eid,'grant_revision':1,'kind':'final','session':'main','payload':final});req=prepared['request']
        app.episode_checkpoints.view({'token':prepared['token'],'payload_sha256':req['payload_sha256'],'preview_sha256':req['dependencies']['preview_sha256']});app.episode_checkpoints.prepared[prepared['token']]['viewed']-=3
        baseline=C.latest(studio,eid,'final','main')['checkpoint_id'];scan=R.scan;calls=0
        with R.resolve(studio,eid,verify_sources=False) as (ep,row):source=ep.root/row['sources'][0]['file']
        def changed(*a,**k):
            nonlocal calls
            calls+=1;result=scan(*a,**k)
            if calls==2:
                if change=='source':
                    data=source.read_bytes();source.write_bytes(data[:-1]+bytes([data[-1]^1]))
                elif change=='inventory':(source.parent/'new-recording.wav').write_bytes(b'unregistered synthetic recording')
                elif change=='grant':R.revoke(studio,eid)
                elif change=='checkpoint':
                    event=copy.deepcopy(C.latest(studio,eid,'rough','main'));event.pop('sig',None);event['checkpoint_id']='check_'+uuid.uuid4().hex;event['revoked']=True;R.control(studio.sp).append(event)
                else:
                    rec=next(r for r in library.read_all(studio.sp).values() if not r.get('derived_from'))
                    seal.log(studio.sp,'control').append({'schema':'license_attestation.v2','op':'license_withdraw','asset':rec['id'],'sha256':rec['sha256'],'entry':rec['license']['entry'],'purpose':'license-use','by':'synthetic fixture'})
            return result
        monkeypatch.setattr(R,'scan',changed)
        with pytest.raises(ValueError,match='changed|revoked'):app.episode_checkpoints.confirm_native(prepared['token'],'123456')
        assert calls==2 and C.latest(studio,eid,'final','main')['checkpoint_id']==baseline
        assert R._VERIFIED_SOURCES.get() is None
    finally:app.close()


def test_wrong_or_expired_view_cannot_confirm(studio,tmp_path,synthetic_final_asr):
    eid,_,app,final=finished(studio,tmp_path)
    try:
        prepared=app.episode_checkpoints.prepare({'episode':eid,'grant_revision':1,'kind':'final','session':'main','payload':final});req=prepared['request'];before=C.latest(studio,eid,'final','main')['checkpoint_id']
        with pytest.raises(ValueError,match='does not match'):app.episode_checkpoints.view({'token':prepared['token'],'payload_sha256':'0'*64,'preview_sha256':req['dependencies']['preview_sha256']})
        with pytest.raises(ValueError,match='Inspect'):app.episode_checkpoints.confirm_native(prepared['token'],'123456')
        app.episode_checkpoints.view({'token':prepared['token'],'payload_sha256':req['payload_sha256'],'preview_sha256':req['dependencies']['preview_sha256']})
        item=app.episode_checkpoints.prepared[prepared['token']];item['viewed']-=3;item['created']-=901
        with pytest.raises(ValueError,match='expired'):app.episode_checkpoints.confirm_native(prepared['token'],'123456')
        assert C.latest(studio,eid,'final','main')['checkpoint_id']==before
    finally:app.close()


@pytest.mark.parametrize('cancel_at',['after-last-scan','after-commit'])
def test_native_confirmation_cancellation_distinguishes_committed_result(studio,tmp_path,synthetic_final_asr,monkeypatch,cancel_at):
    import threading
    eid,_,app,final=finished(studio,tmp_path);cancel=threading.Event()
    try:
        prepared=app.episode_checkpoints.prepare({'episode':eid,'grant_revision':1,'kind':'final','session':'main','payload':final});request=prepared['request'];token=prepared['token']
        app.episode_checkpoints.view({'token':token,'payload_sha256':request['payload_sha256'],'preview_sha256':request['dependencies']['preview_sha256']});app.episode_checkpoints.prepared[token]['viewed']-=3
        before=C.latest(studio,eid,'final','main')['checkpoint_id']
        scan=R.scan;count=0
        def scanned(*args,**kwargs):
            nonlocal count
            count+=1;result=scan(*args,**kwargs)
            if count==2 and cancel_at=='after-last-scan':cancel.set()
            return result
        monkeypatch.setattr(R,'scan',scanned)
        append=seal.SealedLog.append_locked
        def committed(log,event):
            result=append(log,event)
            if event.get('request_id')==token and cancel_at=='after-commit':cancel.set()
            return result
        monkeypatch.setattr(seal.SealedLog,'append_locked',committed)
        if cancel_at=='after-last-scan':
            with pytest.raises(ValueError,match='cancelled'):app.episode_checkpoints.confirm_native(token,'123456',cancelled=cancel.is_set)
            assert C.latest(studio,eid,'final','main')['checkpoint_id']==before
        else:
            result=app.episode_checkpoints.confirm_native(token,'123456',cancelled=cancel.is_set)
            assert cancel.is_set() and result['checkpoint']==C.latest(studio,eid,'final','main')['checkpoint_id']!=before
            assert token not in app.episode_checkpoints.prepared
        assert R._VERIFIED_SOURCES.get() is None
    finally:app.close()


def test_native_confirm_cancels_in_raw_hash_without_checkpoint_and_can_retry(studio,tmp_path,synthetic_final_asr,monkeypatch):
    import builtins,threading
    from pathlib import Path
    from ave import _verification_cancel as cancellation
    eid,_,app,final=finished(studio,tmp_path);cancel=threading.Event()
    try:
        prepared=app.episode_checkpoints.prepare({'episode':eid,'grant_revision':1,'kind':'final','session':'main','payload':final});request=prepared['request'];token=prepared['token']
        app.episode_checkpoints.view({'token':token,'payload_sha256':request['payload_sha256'],'preview_sha256':request['dependencies']['preview_sha256']});app.episode_checkpoints.prepared[token]['viewed']-=3
        before=C.latest(studio,eid,'final','main')['checkpoint_id']
        with R.resolve(studio,eid,verify_sources=False) as (ep,row):raw=ep.root/row['sources'][0]['file']
        original_open=builtins.open;seen=[]
        class InterruptedRead:
            def __init__(self,stream):self.stream=stream
            def __enter__(self):self.stream.__enter__();return self
            def __exit__(self,*args):return self.stream.__exit__(*args)
            def read(self,*args):
                data=self.stream.read(*args)
                if data:seen.append(len(data));cancel.set()
                return data
        def opened(path,mode='r',*args,**kwargs):
            stream=original_open(path,mode,*args,**kwargs)
            return InterruptedRead(stream) if mode=='rb' and Path(path)==raw else stream
        with monkeypatch.context() as m:
            m.setattr(builtins,'open',opened)
            with pytest.raises(ValueError,match='cancelled'):app.episode_checkpoints.confirm_native(token,'123456',cancelled=cancel.is_set)
        assert seen and C.latest(studio,eid,'final','main')['checkpoint_id']==before
        assert cancellation._CURRENT.get() is None and R._VERIFIED_SOURCES.get() is None
        cancel.clear();result=app.episode_checkpoints.confirm_native(token,'123456',cancelled=cancel.is_set)
        assert result['checkpoint']==C.latest(studio,eid,'final','main')['checkpoint_id']!=before
    finally:app.close()
