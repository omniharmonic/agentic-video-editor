"""Full source exit verification precedes a short current-state publication guard."""
import copy
import uuid
import pytest
from ave.studio import episode_registry as R,episode_workshop as E,episode_checkpoints as C
from ave.studio.episode_worker_bridge import Parent
from ave.studio.workbench import write,job_path
from .test_workbench import studio
from .test_episode_workshop import ready


@pytest.mark.parametrize('change',['source','checkpoint','closed-parent-retry','inventory'])
def test_assembly_refuses_change_at_final_source_exit_before_artifact(studio,tmp_path,monkeypatch,change):
    eid,source,app=ready(studio,tmp_path)
    with R.resolve(studio,eid,verify_sources=False) as (ep,row):
        args={'episode':eid,'grant_revision':1,'input_sha256':E.state_hash(ep,row,'episode-assembly','main'),'session':'main','request_id':str(uuid.uuid4())}
    job={'id':'job_'+uuid.uuid4().hex[:24],'operation':'episode-assembly','args':args,'status':'queued','by':'synthetic'}
    job['episode_descriptor']=E.descriptor(studio,job['operation'],args);write(studio.sp,job_path(studio.sp,job['id']),job)
    parent=Parent(studio,job)
    try:
        result=E.execute(studio,job);record=E.artifact(studio,eid,result['artifact']['id'])
        artifact_path=studio.sp.work/'production'/'episode-artifacts'/(record['id']+'.json');artifact_path.unlink()
        scan=R.scan;calls=0
        def changed(*a,**k):
            nonlocal calls
            calls+=1
            if calls==2 and change=='source':
                path=ep.root/row['sources'][0]['file'];data=path.read_bytes();path.write_bytes(data[:-1]+bytes([data[-1]^1]))
            value=scan(*a,**k)
            if calls==2 and change=='checkpoint':
                event=copy.deepcopy(C.latest(studio,eid,'mapping','main'));event.pop('sig',None);event['checkpoint_id']='check_'+uuid.uuid4().hex;event['revoked']=True
                R.control(studio.sp).append(event)
            if calls==2 and change=='inventory':(ep.raw/'new-recording.wav').write_bytes(b'unregistered fixture')
            if calls==2 and change=='closed-parent-retry':
                parent.close()
                # Same captured identity can be retried after its old parent
                # closes. It must never revive that old handler's authority.
                write(studio.sp,job_path(studio.sp,job['id']),{**job,'status':'running'})
            return value
        monkeypatch.setattr(R,'scan',changed)
        data={k:record.get(k) for k in ('file','kind','source','edit','details')}
        with pytest.raises(ValueError,match='changed|revoked|parent closed'):
            parent.handle({'job_id':job['id'],'action':'artifact','data':data})
        assert calls==2 and not artifact_path.exists()
    finally:parent.close();app.close()
