"""Explicitly provisioned512MiB synthetic-byte cancellation/retry integration."""
import os,threading,time,uuid,struct
import pytest
from ave.publish.safety import sha256_file
from ave.studio import episode_registry as R,episode_workshop as E,episode_release as L,episode_release_bridge as B
from ave.studio.workbench import Workbench,Busy
from .test_workbench import studio
from .test_episode_release import finished,wait_job
from . import test_episode_workshop as fixture


@pytest.mark.skipif(os.getenv('AVE_RELEASE_LARGE_TEST')!='1',reason='Set AVE_RELEASE_LARGE_TEST=1 for512MiB self-contained byte-copy/cancel coverage')
def test_large_original_package_cancel_during_hash_and_explicit_retry(studio,tmp_path,synthetic_final_asr,monkeypatch):
    recording=fixture.recording
    def large(*a,**k):
        path=recording(*a,**k)
        # Valid short PCM data followed by inert trailing bytes: actual512MiB
        # copy/hash workload without pretending to be long speech evidence.
        with path.open('r+b') as stream:
            stream.seek(0,2);start=stream.tell();size=512*1024*1024
            stream.write(b'JUNK'+struct.pack('<I',size-start-8));stream.truncate(size)
            stream.seek(4);stream.write(struct.pack('<I',size-8))
        return path
    monkeypatch.setattr(fixture,'recording',large)
    eid,original,app,final=finished(studio,tmp_path);before=sha256_file(original);wb=Workbench(studio)
    entered=threading.Event();release=threading.Event();hashed=[];real=B.sha256_file
    def held(path):
        digest=real(path)
        if path.parent.name=='originals' and path.stat().st_size==512*1024*1024 and not hashed:
            hashed.append(str(path));entered.set();assert release.wait(60)
        return digest
    monkeypatch.setattr(B,'sha256_file',held)
    try:
        with R.resolve(studio,eid,verify_sources=False) as (ep,row):args={'episode':eid,'grant_revision':1,'input_sha256':E.state_hash(ep,row,'episode-package','main'),'session':'main','request_id':str(uuid.uuid4()),'final':final,'chapters':[],'formats':['aac'],'originals':True,'copy':'Synthetic large-byte cancellation fixture'}
        job=wb.start_media('episode-package',args)
        assert entered.wait(60)
        wb.cancel_media({'job_id':job['id']});release.set();deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            current=wb.tail(job['id'])
            if current['status'] not in ('running','queued'):break
            time.sleep(.05)
        assert current['status'] in ('cancelled','interrupted') and not (L.directory(studio,job)/'result.json').exists()
        assert sha256_file(original)==before and L.recover_committed(studio,job) is None
        deadline=time.monotonic()+30
        while True:
            try:retried=wb.retry({'job_id':job['id']});break
            except Busy:
                if time.monotonic()>=deadline:raise
                time.sleep(.1)
        result=wait_job(wb,retried)
        assert result['manifest']['input']['binding']['edit']==final
        assert len(list(L.directory(studio,job).glob('package*')))==2 # one folder and its ZIP, no duplicate recovery encode
        original_entry=next(f for f in result['files'] if f['role']=='original_source');assert original_entry['sha256']==before
    finally:release.set();wb.close();app.close()
