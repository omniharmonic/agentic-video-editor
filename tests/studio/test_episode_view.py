"""Saved projections cannot substitute for protected full-content checks."""
import copy
from pathlib import Path
import pytest
from ave.studio import episode_view as View,episode_registry as R,episode_workshop as E,episode_release as L,episode_checkpoints as C
from .test_workbench import studio
from .test_episode_release import finished,release_job


def test_projection_has_no_raw_hash_and_actions_remain_authoritative(studio,tmp_path,synthetic_final_asr,monkeypatch):
    eid,original,app,final=finished(studio,tmp_path)
    try:
        with R.resolve(studio,eid,verify_sources=False) as (ep,row):
            source=ep.root/row['sources'][0]['file'];baseline=R._source_stats(ep,row)
        calls=[];real=R.sha256_file
        def counted(path):
            if Path(path)==source:calls.append(str(path))
            return real(path)
        monkeypatch.setattr(R,'sha256_file',counted)
        view=View.load(studio,eid);assert not calls
        assert view['view_provenance']['authoritative'] is False and view['checkpoints']['final']['state']=='review recorded'
        assert view['finals'][0]['saved_inputs_match'] and 'inputs_current' not in view['finals'][0]
        proposed=View.candidates(studio,eid,'main');assert proposed['final']==final and proposed['candidates'] and not calls
        View.caption_words(studio,eid,'main',0);View.release(studio,eid,'main');assert not calls
        args=release_job(studio,eid,'episode-select',final=final,start=0,end=1.5,title='Hello')['args'];assert calls
        with pytest.raises(ValueError):L.validate(studio,'episode-select',{**args,'view_provenance':view['view_provenance']})
        with pytest.raises(ValueError):L.validate(studio,'episode-select',{**args,'candidate_id':'forged'})
        with pytest.raises(ValueError):L.validate(studio,'episode-select',{**args,'final':{**final,'sha256':'f'*64}})
        data=source.read_bytes();source.write_bytes(data[:-1]+bytes([data[-1]^1]))
        assert View.load(studio,eid)['checkpoints']['final']['state']=='stale'
        # Simulate a same-stat attacker. The view can remain unknown/recorded;
        # its facts are never installed in the authoritative raw-hash context.
        with monkeypatch.context() as m:
            m.setattr(R,'_source_stats',lambda *a,**k:baseline)
            saved_stat=View._stat
            m.setattr(View,'_stat',lambda p:list(baseline[str(source)]) if Path(p)==source else saved_stat(p))
            assert View.load(studio,eid)['checkpoints']['final']['state']=='review recorded'
            with pytest.raises(ValueError,match='changed'):L.validate(studio,'episode-select',args)
        assert R._VERIFIED_SOURCES.get() is None
        with pytest.raises(ValueError,match='changed'):
            with R.resolve(studio,eid):pass
    finally:app.close()


def test_projection_reflects_small_evidence_review_and_grant_changes(studio,tmp_path,synthetic_final_asr):
    eid,original,app,final=finished(studio,tmp_path)
    try:
        with R.resolve(studio,eid,verify_sources=False) as (ep,row):
            path=ep.artifact('transcript.json');saved=path.read_bytes();path.write_bytes(saved+b' ')
            assert View.load(studio,eid)['checkpoints']['final']['state']=='stale'
            path.write_bytes(saved)
        request=app.episode_checkpoints.prepare({'episode':eid,'grant_revision':1,'kind':'final','session':'main','payload':final,'decision':'revoke'})
        req=request['request'];app.episode_checkpoints.view({'token':request['token'],'payload_sha256':req['payload_sha256'],'preview_sha256':None});app.episode_checkpoints.prepared[request['token']]['viewed']-=3
        app.episode_checkpoints.confirm_native(request['token'],'123456')
        assert View.load(studio,eid)['checkpoints']['final']['state']=='stale'
        with pytest.raises(ValueError,match='revoked'):View.candidates(studio,eid,'main')
        R.revoke(studio,eid)
        with pytest.raises(ValueError,match='revoked'):View.load(studio,eid)
    finally:app.close()


def test_nested_broll_asset_projection_preserves_identity_and_withdrawal_checks(studio,tmp_path,synthetic_final_asr):
    import copy
    from ave import library
    from ave.studio import seal
    eid,_,app,_=finished(studio,tmp_path)
    try:
        with R.resolve(studio,eid,verify_sources=False) as (ep,row):
            reader=View.Reader(studio).bind(ep,row);rec=next(r for r in library.read_all(studio.sp).values() if not r.get('derived_from'))
            value={'broll_proposals':[{'v':1,'kind':'broll.proposal','asset':{'id':rec['id'],'kind':rec['kind']},'placement':{'library':{'asset':rec['id'],'sha256':rec['sha256']}}}]}
            assert reader.dependencies(value)
            changed=copy.deepcopy(value);changed['broll_proposals'][0]['placement']['library']['sha256']='0'*64
            assert not reader.dependencies(changed)
            missing=reader.records.pop(rec['id']);assert not reader.dependencies(value);reader.records[rec['id']]=missing
            seal.log(studio.sp,'control').append({'schema':'license_attestation.v2','op':'license_withdraw','asset':rec['id'],'sha256':rec['sha256'],'entry':rec['license']['entry'],'purpose':'license-use','by':'synthetic fixture'})
            assert not View.Reader(studio).bind(ep,row).dependencies(value)
    finally:app.close()
