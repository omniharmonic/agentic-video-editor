"""Owned provenance bytes share only one exact episode operation lifetime."""
import contextvars,hashlib,os
from concurrent.futures import ThreadPoolExecutor
import pytest
from ave.publish import safety
from ave.studio import episode_registry as R,episode_workshop as E,episode_checkpoints as C,seal
from .test_workbench import studio
from .test_episode_workshop import managed


def owned(studio,name='one'):
    path=studio.sp.library_dir/'assets'/(name+'.bin');path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'owned synthetic bytes');return path,hashlib.sha256(path.read_bytes()).hexdigest()


def counter(monkeypatch):
    original=safety.sha256_file;calls=[]
    def measured(path):calls.append(str(path));return original(path)
    monkeypatch.setattr(safety,'sha256_file',measured);return calls


def test_same_owned_path_once_plus_fresh_exit_distinct_paths_and_fresh_scopes(studio,tmp_path,monkeypatch):
    eid,_=managed(studio,tmp_path);p,digest=owned(studio);q,_=owned(studio,'two');calls=counter(monkeypatch)
    with R.resolve(studio,eid):
        for _ in range(4):assert R._verified_owned_provenance_sha(studio,p,digest)==digest
        assert R._verified_owned_provenance_sha(studio,q,digest)==digest
        assert calls==[str(p),str(q)]
        with R.resolve(studio,eid,_fresh_sources=True):R._verified_owned_provenance_sha(studio,p,digest)
        assert calls.count(str(p))==3
    assert calls.count(str(p))==4 and calls.count(str(q))==2 and R._VERIFIED_SOURCES.get() is None
    for _ in range(2):R._verified_owned_provenance_sha(studio,p,digest)
    assert calls.count(str(p))==6


@pytest.mark.parametrize('change',['digest','replace','symlink','same-stat-bytes'])
def test_owned_changes_refuse_and_scope_resets(studio,tmp_path,monkeypatch,change):
    eid,_=managed(studio,tmp_path);p,digest=owned(studio)
    with pytest.raises(ValueError,match='changed|symlink'):
        with R.resolve(studio,eid):
            R._verified_owned_provenance_sha(studio,p,digest)
            if change=='digest':R._verified_owned_provenance_sha(studio,p,'0'*64)
            elif change=='replace':
                q,_=owned(studio,'replacement');os.replace(q,p);R._verified_owned_provenance_sha(studio,p,digest)
            elif change=='symlink':
                q,_=owned(studio,'replacement');p.unlink();p.symlink_to(q);R._verified_owned_provenance_sha(studio,p,digest)
            else:
                # Force stat projection unchanged to establish full-byte exit
                # verification, beyond ordinary ctime/mtime detection.
                stat=R._owned_stat(p);monkeypatch.setattr(R,'_owned_stat',lambda path:stat)
                p.write_bytes(b'X'+p.read_bytes()[1:]);assert R._verified_owned_provenance_sha(studio,p,digest)==digest
    assert R._VERIFIED_SOURCES.get() is None


def test_first_read_checks_after_hash_and_preserves_original_failure(studio,tmp_path,monkeypatch):
    eid,_=managed(studio,tmp_path);p,digest=owned(studio);original=safety.sha256_file
    def mutate(path):
        value=original(path);p.write_bytes(b'changed');return value
    monkeypatch.setattr(safety,'sha256_file',mutate)
    with pytest.raises(ValueError,match='changed'):
        with R.resolve(studio,eid):R._verified_owned_provenance_sha(studio,p,digest)
    monkeypatch.setattr(safety,'sha256_file',original);p,digest=owned(studio)
    with pytest.raises(RuntimeError,match='original failure'):
        with R.resolve(studio,eid):
            R._verified_owned_provenance_sha(studio,p,digest);p.unlink();raise RuntimeError('original failure')
    assert R._VERIFIED_SOURCES.get() is None


def test_context_cannot_reuse_in_copied_thread_pid_or_different_episode(studio,tmp_path,monkeypatch):
    eid,_=managed(studio,tmp_path);other,_=managed(studio,tmp_path);p,digest=owned(studio);calls=counter(monkeypatch)
    with R.resolve(studio,eid):
        R._verified_owned_provenance_sha(studio,p,digest);ctx=contextvars.copy_context()
        with ThreadPoolExecutor(max_workers=1) as pool:pool.submit(ctx.run,R._verified_owned_provenance_sha,studio,p,digest).result()
        assert len(calls)==2
        scope=R._VERIFIED_SOURCES.get();owner=scope['owner'];scope['owner']=(-1,owner[1])
        R._verified_owned_provenance_sha(studio,p,digest);scope['owner']=owner
        assert len(calls)==3
        with R.resolve(studio,other):R._verified_owned_provenance_sha(studio,p,digest)
        assert len(calls)==5
    assert len(calls)==6


def test_owned_exit_cancellation_resets_context(studio,tmp_path):
    from ave._verification_cancel import scope
    eid,_=managed(studio,tmp_path);p,digest=owned(studio);cancel=False
    with pytest.raises(ValueError,match='cancelled'):
        with scope(lambda:cancel):
            with R.resolve(studio,eid):
                R._verified_owned_provenance_sha(studio,p,digest);cancel=True
    assert R._VERIFIED_SOURCES.get() is None
    assert R._verified_owned_provenance_sha(studio,p,digest)==digest


def test_combined_provenance_counts_and_fresh_license_reads(studio,tmp_path,synthetic_final_asr,monkeypatch):
    from .test_episode_release import finished
    from .test_episode_parent_scope import second_assembly
    eid,_,app,_=finished(studio,tmp_path)
    try:
        second_assembly(studio,eid,app);args={'session':'combined','constituents':[C.latest(studio,eid,'assembly',s)['payload'] for s in ('main','second')]};calls=counter(monkeypatch)
        with R.resolve(studio,eid) as (ep,row):
            first=E.combined_inputs(studio,ep,row,args);assert first==E.combined_inputs(studio,ep,row,args)
            assert len(calls)==len(set(calls))
        assert all(calls.count(p)==2 for p in set(calls))
        rec=next(r for r in __import__('ave.library',fromlist=['read_all']).read_all(studio.sp).values() if not r.get('derived_from'))
        from ave import library
        library.attest_license(studio.sp,rec['id'],'simulated fixture')
        selected=[{'id':rec['id'],'sha256':rec['sha256']}]
        with R.resolve(studio,eid):
            E.licensed_asset_closure(studio,selected)
            seal.log(studio.sp,'control').append({'schema':'license_attestation.v2','op':'license_withdraw','asset':rec['id'],'sha256':rec['sha256'],'entry':rec['license']['entry'],'purpose':'license-use','by':'synthetic fixture'})
            with pytest.raises(ValueError,match='attestation'):E.licensed_asset_closure(studio,selected)
    finally:app.close()


@pytest.mark.parametrize('phase',['first-read','exit-read'])
def test_cancellation_during_owned_hash_never_returns_partial_digest(studio,tmp_path,monkeypatch,phase):
    from ave._verification_cancel import scope
    eid,_=managed(studio,tmp_path);p,digest=owned(studio);original=safety.sha256_file;calls=0;cancel=False
    def interrupted(path):
        nonlocal calls,cancel
        if str(path)==str(p):
            calls+=1
            if calls==(1 if phase=='first-read' else 2):cancel=True
        return original(path)
    monkeypatch.setattr(safety,'sha256_file',interrupted)
    with pytest.raises(ValueError,match='cancelled'):
        with scope(lambda:cancel):
            with R.resolve(studio,eid):R._verified_owned_provenance_sha(studio,p,digest)
    assert calls==(1 if phase=='first-read' else 2) and R._VERIFIED_SOURCES.get() is None


def test_changed_expected_digest_cannot_reenroll_and_outside_path_refused(studio,tmp_path):
    eid,_=managed(studio,tmp_path);p,digest=owned(studio)
    with R.resolve(studio,eid):
        R._verified_owned_provenance_sha(studio,p,digest)
        with pytest.raises(ValueError,match='identity changed'):R._verified_owned_provenance_sha(studio,p,'f'*64)
        with pytest.raises(ValueError,match='owned library'):R._verified_owned_provenance_sha(studio,studio.sp.root/'studio.json',digest)
        assert R._verified_owned_provenance_sha(studio,p,digest)==digest
