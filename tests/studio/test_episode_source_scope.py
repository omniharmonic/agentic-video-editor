"""Operation-scoped raw checks carry no reusable editorial authority."""
import contextvars
from concurrent.futures import ThreadPoolExecutor
import pytest
from ave.studio import episode_registry as R
from .test_workbench import studio
from .test_episode_workshop import managed


def instrument(monkeypatch):
    original=R.sha256_file;calls=[]
    def counted(path):calls.append(str(path));return original(path)
    monkeypatch.setattr(R,'sha256_file',counted)
    return calls


def test_nested_sources_reuse_only_inside_exact_operation(studio,tmp_path,monkeypatch):
    eid,_=managed(studio,tmp_path);calls=instrument(monkeypatch)
    with R.resolve(studio,eid) as (ep,row):
        assert len(calls)==1
        with R.resolve(studio,eid):R.assert_sources(ep,row,[s['id'] for s in row['sources']])
        assert len(calls)==1
        with R.resolve(studio,eid,_fresh_sources=True):pass
        assert len(calls)==3
    assert len(calls)==4 and R._VERIFIED_SOURCES.get() is None
    with R.resolve(studio,eid):pass
    assert len(calls)==6


def test_mutation_inventory_grant_and_exception_cleanup(studio,tmp_path):
    eid,_=managed(studio,tmp_path)
    with pytest.raises(ValueError,match='changed'):
        with R.resolve(studio,eid) as (ep,row):
            (ep.root/row['sources'][0]['file']).write_bytes(b'changed synthetic source')
            with R.resolve(studio,eid):pass
    assert R._VERIFIED_SOURCES.get() is None
    eid,_=managed(studio,tmp_path)
    with pytest.raises(ValueError,match='inventory'):
        with R.resolve(studio,eid) as (ep,row):
            (ep.raw/'extra.wav').write_bytes(b'new synthetic source')
            R.assert_sources(ep,row,[row['sources'][0]['id']])
    assert R._VERIFIED_SOURCES.get() is None
    eid,_=managed(studio,tmp_path)
    with pytest.raises(ValueError,match='revoked'):
        with R.resolve(studio,eid):
            R.revoke(studio,eid)
            with R.resolve(studio,eid):pass
    assert R._VERIFIED_SOURCES.get() is None
    with pytest.raises(RuntimeError):
        other,_=managed(studio,tmp_path)
        with R.resolve(studio,other):raise RuntimeError('injected')
    assert R._VERIFIED_SOURCES.get() is None


def test_copied_context_cannot_cross_thread_or_episode(studio,tmp_path,monkeypatch):
    first,_=managed(studio,tmp_path);second,_=managed(studio,tmp_path);calls=instrument(monkeypatch)
    with R.resolve(studio,first):
        context=contextvars.copy_context()
        def check():
            with R.resolve(studio,first):pass
        with ThreadPoolExecutor(max_workers=1) as pool:pool.submit(context.run,check).result()
        assert len(calls)==3
        with R.resolve(studio,second):pass
        assert len(calls)==5
    assert len(calls)==6 and R._VERIFIED_SOURCES.get() is None


def test_assembly_validation_has_one_entry_and_exit_scan(studio,tmp_path,monkeypatch):
    from ave.studio import episode_workshop as E
    from .test_episode_workshop import ready
    eid,_,app=ready(studio,tmp_path)
    try:
        with R.resolve(studio,eid,verify_sources=False) as (ep,row):
            args={'episode':eid,'grant_revision':1,'input_sha256':E.state_hash(ep,row,'episode-assembly','main'),'session':'main','request_id':'scoped-assembly'}
        calls=instrument(monkeypatch);E.validate_operation(studio.sp,'episode-assembly',args)
        assert len(calls)==2 and R._VERIFIED_SOURCES.get() is None
    finally:app.close()


@pytest.mark.parametrize('change',['source','grant'])
def test_assembly_validation_rechecks_after_nested_dependencies(studio,tmp_path,monkeypatch,change):
    from ave.studio import episode_workshop as E
    from .test_episode_workshop import ready
    eid,_,app=ready(studio,tmp_path)
    try:
        with R.resolve(studio,eid,verify_sources=False) as (ep,row):
            args={'episode':eid,'grant_revision':1,'input_sha256':E.state_hash(ep,row,'episode-assembly','main'),'session':'main','request_id':'changed-assembly'}
        original=E.base_inputs
        def changed(studio,ep,row,session):
            value=original(studio,ep,row,session)
            if change=='source':
                path=ep.root/row['sources'][0]['file'];data=path.read_bytes();path.write_bytes(data[:-1]+bytes([data[-1]^1]))
            else:R.revoke(studio,eid)
            return value
        monkeypatch.setattr(E,'base_inputs',changed)
        with pytest.raises(ValueError,match='changed|revoked'):E.validate_operation(studio.sp,'episode-assembly',args)
        assert R._VERIFIED_SOURCES.get() is None
    finally:app.close()
