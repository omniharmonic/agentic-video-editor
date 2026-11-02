"""Exact Resolve planning and explicitly fake external bridge reports."""
import json
import subprocess
from contextlib import contextmanager
import pytest
from ave.studio import episode_release as L,episode_registry as R,episode_workshop as E,episode_worker_bridge as B
from ave.studio.episode_release_bridge import ReleaseParent
from .test_workbench import studio
from .test_episode_release import finished,release_job


@pytest.mark.parametrize('companion_fails',[False,True])
def test_exact_master_and_reference_companion_have_separate_results(studio,tmp_path,synthetic_final_asr,monkeypatch,companion_fails):
    from ave import config,proxies
    eid,original,app,final=finished(studio,tmp_path,video=True)
    if not companion_fails:
        with R.resolve(studio,eid) as (ep,row):
            doc=E.exact(ep,final)[1]
            made=proxies.run(ep,proxies.footage_used(ep,doc),height=180,hw=False,log=lambda _:None)
            assert made['made'] and not made['failed']
    job=release_job(studio,eid,'episode-handoff',final=final,name='Synthetic exact review',sources=True,layers=True,proxies=True,subtitles=True)
    parent=ReleaseParent(studio,job);calls=[]
    with R.resolve(studio,eid,verify_sources=False) as pair:ep,row=pair
    @contextmanager
    def resolved(*args,**kwargs):yield ep,row
    class Captured:
        def call(self,action,**data):
            assert not data
            if action.endswith('-inputs'):return parent.initial
            if action.endswith('-status'):return None
            if action.endswith('-register'):return None
            raise AssertionError(action)
    real=subprocess.run
    def fake_bridge(command,**kwargs):
        if 'resolve_handoff.py' not in str(command):return real(command,**kwargs)
        plan=json.loads(__import__('pathlib').Path(command[2]).read_text());calls.append(plan)
        failed=companion_fails and plan.get('review_mode')=='source-body-reference'
        return subprocess.CompletedProcess(command,2 if failed else 0,json.dumps({'ok':not failed,'timeline':plan['name'],'synthetic_fixture':True,'problems':['simulated companion failure'] if failed else []}), '')
    try:
        with monkeypatch.context() as m:
            m.setattr(B,'CLIENT',Captured());m.setattr(R,'resolve',resolved)
            m.setattr(config,'resolve_mcp_python',lambda:__import__('pathlib').Path('/synthetic/python'))
            m.setattr(config,'resolve_mcp_dir',lambda:tmp_path/'synthetic-bridge')
            m.setattr(subprocess,'run',fake_bridge)
            L.execute(studio,job)
        if not companion_fails:
            mix=L.directory(studio,job)/'source-body-mix.wav';saved=mix.read_bytes();mix.write_bytes(saved[:-1]+bytes([saved[-1]^1]))
            with pytest.raises(ValueError,match='companion audio changed'):parent.handle({'job_id':job['id'],'action':'episode-handoff-register','data':{}})
            mix.write_bytes(saved)
        result=parent.handle({'job_id':job['id'],'action':'episode-handoff-register','data':{}})
        assert result['report']['timeline']=='Synthetic exact review' and result['report']['synthetic_fixture']
        assert len(calls)==2 and calls[0]['review_mode']=='exact-rendered-master'
        assert calls[1]['layers']['baked_master'] and calls[1]['layers']['audio_clips'] and calls[1]['warnings']
        assert result['companion']['status']==('failed; master preserved' if companion_fails else 'created reference timeline')
        assert result['companion']['limitations']
        assert result['companion_files'] and all(x['sample_rate']==48000 and x['channels']==2 for x in result['companion_files'])
        if not companion_fails:assert calls[1]['proxies'] and not parent.initial['handoff']['missing_proxies']
        # Reading a committed report neither invokes Resolve nor needs the raw grant.
        R.revoke(studio,eid);recovered=L.recover_committed(studio,job)
        assert recovered['report']==result['report'] and len(calls)==2
    finally:parent.close();app.close()
