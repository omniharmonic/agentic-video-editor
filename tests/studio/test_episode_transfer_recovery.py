"""Crash-boundary transfers use parent receipts, never imported authority."""
import copy
import shutil
import uuid
import pytest
from ave import library
from ave.studio import episode_release as L,episode_registry as R,episode_workshop as E,episode_worker_bridge as B,video_workshop as V,workbench as W
from ave.studio.episode_release_bridge import ReleaseParent
from .test_workbench import studio
from .test_episode_workshop import recording
from .test_video_workshop import mutate


def staged_adoption(studio,tmp_path,monkeypatch):
    asset=library.add_file(studio.sp,recording(tmp_path),source={'type':'own'},by='synthetic')
    project=V.create(studio,{'title':'Crash boundary fixture','mode':'audio','request_id':'transfer-project'})
    project=V.add_source(studio,{'project':project['id'],'source_id':asset['id'],'expected_revision':1,'request_id':'transfer-source'})
    project=mutate(studio,project,{'kind':'insert','source':asset['id'],'in':0,'out':3})
    args={'project':project['id'],'version':project['version'],'sha256':project['sha256'],'revision':project['revision'],'title':'Owned adoption','request_id':'owned-adoption'}
    job={'id':'job_'+uuid.uuid4().hex[:24],'operation':'episode-adopt','args':args,'status':'queued','by':'synthetic'}
    job['episode_descriptor']=E.descriptor(studio,job['operation'],args);W.write(studio.sp,W.job_path(studio.sp,job['id']),job)
    parent=ReleaseParent(studio,job)
    class Captured:
        def call(self,action,**data):
            assert action=='episode-adopt-register' and not data
    with monkeypatch.context() as m:
        m.setattr(B,'CLIENT',Captured());L.execute_transfer(studio,job,parent.initial)
    return job,parent


@pytest.mark.parametrize('boundary',['before-rename','after-rename','after-registration'])
def test_transfer_crash_projection_and_explicit_retry(studio,tmp_path,monkeypatch,boundary):
    job,parent=staged_adoption(studio,tmp_path,monkeypatch)
    try:
        publish=W._publish_staged;write=L.write_result
        def interrupted_publish(source,target):
            if boundary=='before-rename':raise RuntimeError('simulated process death before rename')
            publish(source,target)
            if boundary=='after-rename':raise RuntimeError('simulated process death after rename')
        def interrupted_receipt(studio,path,result):
            if path.name=='result.json':raise RuntimeError('simulated process death after registration')
            return write(studio,path,result)
        with monkeypatch.context() as m:
            m.setattr(W,'_publish_staged',interrupted_publish)
            if boundary=='after-registration':m.setattr(L,'write_result',interrupted_receipt)
            with pytest.raises(RuntimeError,match='simulated process death'):
                parent.handle({'job_id':job['id'],'action':'episode-adopt-register','data':{}})
        before=copy.deepcopy(R.records(studio.sp));recovered=L.recover_committed(studio,job)
        assert recovered['recovery']['incomplete'] and recovered['recovery']['committed_earlier']==(boundary!='before-rename')
        assert R.records(studio.sp)==before # read-only recovery never registers
        result=parent.handle({'job_id':job['id'],'action':'episode-adopt-register','data':{}})
        assert len(R.records(studio.sp))==1 and result['episode'] in R.records(studio.sp)
        assert L.recover_committed(studio,job)['episode']==result['episode']
        assert parent.handle({'job_id':job['id'],'action':'episode-adopt-register','data':{}})==result
    finally:parent.close()


def test_foreign_same_byte_transfer_directory_is_not_recoverable(studio,tmp_path,monkeypatch):
    job,parent=staged_adoption(studio,tmp_path,monkeypatch);publish=W._publish_staged
    try:
        def killed(source,target):publish(source,target);raise RuntimeError('simulated process death')
        with monkeypatch.context() as m:
            m.setattr(W,'_publish_staged',killed)
            with pytest.raises(RuntimeError):parent.handle({'job_id':job['id'],'action':'episode-adopt-register','data':{}})
        recovered=L.recover_committed(studio,job);target=studio.sp.root/recovered['target'];saved=target.with_name(target.name+'-owned')
        target.rename(saved);shutil.copytree(saved,target)
        with pytest.raises(ValueError,match='foreign'):L.recover_committed(studio,job)
        assert not R.records(studio.sp)
    finally:parent.close()


def test_incomplete_transfer_retry_requires_current_input_and_keeps_owned_output(studio,tmp_path,monkeypatch):
    job,parent=staged_adoption(studio,tmp_path,monkeypatch);publish=W._publish_staged
    try:
        def killed(source,target):publish(source,target);raise RuntimeError('simulated death after rename')
        with monkeypatch.context() as m:
            m.setattr(W,'_publish_staged',killed)
            with pytest.raises(RuntimeError):parent.handle({'job_id':job['id'],'action':'episode-adopt-register','data':{}})
        projection=L.recover_committed(studio,job);target=studio.sp.root/projection['target'];assert target.exists()
        # A later authored revision invalidates this retry; read-only recovery
        # still identifies the exact incomplete owned result without registering.
        current=V.load(studio.sp,job['args']['project'])
        mutate(studio,current,{'kind':'remove','clip':current['doc']['clips'][0]['id']})
        job.update(status='interrupted',diagnostics='fixture interruption');W.write(studio.sp,W.job_path(studio.sp,job['id']),job)
        wb=W.Workbench(studio)
        try:
            with pytest.raises(ValueError,match='changed'):wb.retry({'job_id':job['id']})
            saved=wb.tail(job['id']);assert saved['result']['recovery']['incomplete'] and target.exists()
            assert not R.records(studio.sp)
        finally:wb.close()
        wrong={**job,'id':'job_'+'e'*24};W.write(studio.sp,W.job_path(studio.sp,wrong['id']),wrong)
        other=L.directory(studio,wrong);other.mkdir();shutil.copyfile(L.directory(studio,job)/'publication-intent.json',other/'publication-intent.json')
        with pytest.raises(ValueError,match='different job'):L.recover_committed(studio,wrong)
    finally:parent.close()
