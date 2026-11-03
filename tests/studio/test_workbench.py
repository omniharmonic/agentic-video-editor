"""Workbench boundaries, durable recovery and real local renderer integration."""
import os
import hashlib
import json
import threading
import time
from pathlib import Path

import pytest
from ave.studio import model, production, store
from ave.studio.project import StudioProject
from ave.studio.service import open_studio
from ave.studio.ui.server import StudioServer
from ave.studio.workbench import Workbench, Busy, execute_operation, job_path, read, write
from .test_api import Client

from browser_cache import playwright_cache_dir
BROWSER_CACHE = str(playwright_cache_dir())

@pytest.fixture(autouse=True)
def real_browser_cache(monkeypatch):
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH', BROWSER_CACHE)

TEXT = b'# Listening\n\nWe leave room for one another, and we listen before we answer. The work grows from the attention we give it.\n'

@pytest.fixture
def studio(tmp_path):
    sp=StudioProject(tmp_path/'studio');sp.create(title='Workbench synthetic tests',tz='UTC')
    return open_studio(sp.root)

@pytest.fixture
def wb(studio):
    obj=Workbench(studio,by='synthetic-user');yield obj;obj.close()

@pytest.fixture
def http(studio):
    server=StudioServer(studio,pin='424242');thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    yield Client(server),server
    server.shutdown();thread.join(5);server.server_close()


def upload(wb, payload=TEXT, name='essay.md'):
    tmp=wb.sp.work/'uploads'/'test-upload';tmp.parent.mkdir(exist_ok=True);tmp.write_bytes(payload)
    try:return wb.upload(tmp,name,len(payload),hashlib.sha256(payload).hexdigest())
    finally:tmp.unlink()


def render_args(wb):
    src=next(s for s in wb.overview()['sources'] if s['kind']=='writing')
    return {'source_id':src['source_id'],'sentence_id':src['sentences'][0]['id'],'recipe':'quote-post','style':'paper','format':'4x5'}


def finish(wb, job, timeout=60):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        result=wb.tail(job['id'])
        if result['status'] not in ('queued','running'):return result
        time.sleep(.05)
    pytest.fail('Job did not complete')


def test_real_http_writing_render_restart_and_authority(http):
    client,server=http;wb=server.app.workbench
    status,out=client.api('PUT','workbench/upload?name=essay.md',raw=TEXT)
    assert status==201,out
    src=wb.overview()['sources'][0]
    assert src['sentences'][0]['text']=='We leave room for one another, and we listen before we answer.'
    assert out['sha256']==hashlib.sha256(TEXT).hexdigest()
    status,job=client.post('workbench/render',render_args(wb));assert status==202,job
    done=finish(wb,job);assert done['status']=='done',done
    result=done['result'];assert result['attached'] and result['critic']['ok']
    from PIL import Image
    asset=result['assets'][0];path=wb.sp.root/asset['file'];assert Image.open(path).size==(1080,1350)
    assert hashlib.sha256(path.read_bytes()).hexdigest()==asset['sha256']
    post=store.load_post(wb.sp,job['post']);assert post['media'] and post['rev']>1
    assert post['production']['sources'][0]['source_id']==src['source_id']
    assert not post['variants'] and not list((wb.sp.publish/'queue').glob('*.json'))
    doc=json.loads(Path(result['doc']).read_text());assert doc['sources'][doc['items'][0]['source']]['sentences']==[src['sentences'][0]['id']]
    wb.close();reopened=Workbench(server.app.studio)
    assert reopened.tail(job['id'])['result']['assets'][0]['url']==asset['url'];reopened.close()


@pytest.mark.parametrize('name,payload', [('bad.exe',TEXT),('../x.md',TEXT),('a\\b.md',TEXT),('.hidden.md',TEXT),('x.md',b'\xff\xfe'),('empty.md',b'')])
def test_invalid_upload_no_source(wb,name,payload):
    with pytest.raises(ValueError): upload(wb,payload,name)
    assert wb.overview()['sources']==[]


def test_reject_hash_symlink_and_http_cap(wb,http,tmp_path):
    p=wb.sp.work/'uploads'/'bad';p.parent.mkdir(exist_ok=True);p.write_bytes(TEXT)
    with pytest.raises(ValueError,match='checksum'):wb.upload(p,'essay.md',len(TEXT),'f'*64)
    outside=tmp_path/'outside';outside.mkdir();(wb.sp.work/'writing').symlink_to(outside,target_is_directory=True)
    with pytest.raises(ValueError,match='symlink'):upload(wb)
    assert list(outside.iterdir())==[]
    client,server=http;server.app.max_upload=8
    status,out=client.api('PUT','workbench/upload?name=x.md',raw=TEXT);assert status==413
    assert not list((server.app.ep.work/'uploads').glob('.upload-*'))


def test_unknown_and_injection_arguments_do_not_create_posts(wb):
    upload(wb)
    args=render_args(wb)
    for patch in ({'recipe':'shell'},{'style':';touch /tmp/nope'},{'source_id':'../../outside'},{'sentence_id':'missing'},{'env':{}},{'format':'99x99'}):
        with pytest.raises(ValueError):wb.start_render({**args,**patch})
    assert not store.list_posts(wb.sp)


def test_busy_second_instance_and_interrupted_recovery(wb,monkeypatch):
    upload(wb)
    entered=threading.Event();release=threading.Event()
    def waiting(jid,held):
        entered.set();release.wait(5);held.close()
    monkeypatch.setattr(wb,'_launch',waiting)
    job=wb.start_render(render_args(wb));assert entered.wait(2)
    another=Workbench(wb.studio)
    assert another.tail(job['id'])['status']=='queued'
    with pytest.raises(Busy):another.start_render(render_args(wb))
    release.set();wb.close();another.close()
    reopened=Workbench(wb.studio)
    assert reopened.tail(job['id'])['status']=='interrupted'
    retry=reopened.retry({'job_id':job['id']})
    assert retry['id']==job['id'];assert retry['post']==job['post'];assert finish(reopened,retry)['status']=='done'
    assert len(store.list_posts(wb.sp))==1;reopened.close()


def test_genuine_renderer_failure_and_retry_keep_draft(wb,monkeypatch):
    upload(wb)
    from ave.content import settings
    # A real renderer environment with a nonexistent Chromium executable fails;
    # do not replace the renderer with a mock-success implementation.
    original=__import__('ave.studio.workbench',fromlist=['child_env']).child_env
    def broken_env():return {**original(),'PLAYWRIGHT_BROWSERS_PATH':str(wb.sp.work/'missing-browser')}
    monkeypatch.setattr('ave.studio.workbench.child_env',broken_env)
    job=wb.start_render(render_args(wb));failed=finish(wb,job)
    assert failed['status']=='failed' and failed['diagnostics']
    assert not store.load_post(wb.sp,job['post'])['media']
    assert wb.overview()['sources'][0]['sentences']
    monkeypatch.setattr('ave.studio.workbench.child_env',original)
    retry=wb.retry({'job_id':job['id']});assert retry['id']==job['id'];assert retry['post']==job['post']
    assert finish(wb,retry)['status']=='done';assert len(store.list_posts(wb.sp))==1


def test_concurrent_edit_preserved_with_real_artifact(wb,monkeypatch):
    upload(wb);held_jobs=[]
    def defer(jid,held):held_jobs.append(jid);held.close()
    monkeypatch.setattr(wb,'_launch',defer)
    job=wb.start_render(render_args(wb));wb._threads[-1].join()
    post=store.load_post(wb.sp,job['post'])
    wb.studio.edit_post(post['id'],{'copy.base':'A newer caption from the author'},expected_rev=post['rev'])
    result=execute_operation(wb.studio,job)
    assert result['attachment_conflict'] and result['assets'] and not result['attached']
    current=store.load_post(wb.sp,post['id']);assert current['copy']['base']=='A newer caption from the author' and current['media']==[]


def test_empty_recording_preserved_failed_import(wb,tmp_path):
    import wave
    p=tmp_path/'empty.wav'
    with wave.open(str(p),'wb') as f:f.setnchannels(1);f.setsampwidth(2);f.setframerate(16000)
    result=upload(wb,p.read_bytes(),'empty.wav');done=finish(wb,result['job'])
    assert done['status']=='failed' and 'duration' in done['diagnostics']
    src=wb.overview()['sources'][0];assert (wb.sp.root/src['file']).read_bytes()==p.read_bytes()
    assert not src['ready']


def test_transcription_exit_zero_missing_invalid_output(tmp_path,monkeypatch):
    from ave import transcribe
    from types import SimpleNamespace
    monkeypatch.setattr(transcribe.config,'fluidaudio_cli',lambda:Path('/fake/fluidaudio'))
    def fake_run(*a,**kw):
        assert kw['env']['AVE_AGENT']=='1' and 'AVE_APPROVAL_KEY' not in kw['env']
        return SimpleNamespace(returncode=0,stdout='Audio input could not be decoded',stderr='')
    monkeypatch.setattr(transcribe.subprocess,'run',fake_run)
    output=tmp_path/'out.json';output.write_text('{"wordTimings": []}')
    with pytest.raises(SystemExit,match='Audio input could not be decoded'):
        transcribe.run_fluidaudio_asr(tmp_path/'in.wav',output,None)
    assert not output.exists()


def test_recording_sources_and_locator_validation(wb):
    upload(wb);source=wb.overview()['sources'][0]
    post=wb.studio.create_post(copy={'base':'Linked writing'})
    p=production.update(wb.studio,post['id'],{'sources':[{'source_id':source['source_id'],'label':source['label']}]},expected_rev=post['rev'])
    with pytest.raises(ValueError,match='inventory'):
        production.update(wb.studio,post['id'],{'sources':[{'source_id':'unknown','locator':{'start':0,'end':1}}]},expected_rev=p['rev'])


def test_preallocated_post_recovers_after_interrupted_creation(wb,monkeypatch):
    upload(wb)
    original=store.save_post
    monkeypatch.setattr(store,'save_post',lambda *a,**kw: (_ for _ in ()).throw(OSError('Synthetic interrupted disk write')))
    with pytest.raises(OSError):wb.start_render(render_args(wb))
    job=wb.overview()['jobs'][0];assert job['post'] and not store.list_posts(wb.sp)
    monkeypatch.setattr(store,'save_post',original)
    wb.close();reopened=Workbench(wb.studio)
    retried=reopened.retry({'job_id':job['id']})
    assert retried['id']==job['id'] and retried['post']==job['post']
    assert finish(reopened,retried)['status']=='done'
    assert len(store.list_posts(wb.sp))==1;reopened.close()


def test_text_bytes_preserve_crlf_and_injection_is_inert(wb):
    body=b'# A note\r\n\r\nRun `touch /tmp/should-not-exist` is only quoted source text. We listen carefully.\r\n'
    result=upload(wb,body)
    assert (wb.sp.work/'writing'/result['source_id']).read_bytes()==body
    assert wb.overview()['sources'][0]['sha256']==hashlib.sha256(body).hexdigest()
    assert 'touch' in wb.overview()['sources'][0]['sentences'][0]['text']


def test_http_auth_origin_and_upload_directory_symlink(http,tmp_path):
    client,server=http
    status,_=client.api('PUT','workbench/upload?name=x.md',raw=TEXT,token='bad-token');assert status==401
    status,_=client.api('PUT','workbench/upload?name=x.md',raw=TEXT,headers={'Origin':'https://example.invalid'});assert status==403
    outside=tmp_path/'outside-uploads';outside.mkdir()
    (server.app.ep.work/'uploads').symlink_to(outside,target_is_directory=True)
    status,body=client.api('PUT','workbench/upload?name=x.md',raw=TEXT)
    assert status==400 and 'symlink' in body['error']
    assert not list(outside.iterdir())


def test_zero_exit_invalid_engine_json_has_diagnostics(tmp_path,monkeypatch):
    from ave import transcribe
    from types import SimpleNamespace
    output=tmp_path/'broken.json'
    def fake_run(*a,**kw):
        output.write_text('{not valid JSON')
        return SimpleNamespace(returncode=0,stdout='Engine could not finalize transcription',stderr='')
    monkeypatch.setattr(transcribe.subprocess,'run',fake_run)
    with pytest.raises(SystemExit,match='Engine could not finalize transcription'):
        transcribe._output(['/fake/local-engine'],output)


def test_renderer_missing_outputs_cannot_clear_draft(wb,monkeypatch):
    from ave.studio.render import RenderError
    upload(wb)
    monkeypatch.setattr(wb,'_launch',lambda jid,held:held.close())
    job=wb.start_render(render_args(wb));wb._threads[-1].join()
    monkeypatch.setattr('ave.studio.render.RM.render_doc',lambda sp,doc,**kw:(doc,{'failed':[]}))
    with pytest.raises(RenderError,match='no output files'):execute_operation(wb.studio,job)
    assert store.load_post(wb.sp,job['post'])['media']==[]


def tone_bytes():
    import io, math, struct, wave
    stream=io.BytesIO()
    with wave.open(stream,'wb') as out:
        out.setnchannels(1);out.setsampwidth(2);out.setframerate(16000)
        out.writeframes(b''.join(struct.pack('<h',int(2000*math.sin(i*2*math.pi*220/16000))) for i in range(16000)))
    return stream.getvalue()


def defer_launch(wb,monkeypatch):
    monkeypatch.setattr(wb,'_launch',lambda jid,held:held.close())


def test_raw_boundary_normalizes_paths_and_internal_aliases(http):
    client,server=http;sp=server.app.ep
    raw=sp.work/'production'/'episodes'/'recording_test'/'raw'/'original.wav'
    raw.parent.mkdir(parents=True);raw.write_bytes(b'private synthetic raw')
    (sp.work/'alias-dir').symlink_to(raw.parent,target_is_directory=True)
    (sp.work/'alias-file.wav').symlink_to(raw)
    paths=['work/production/episodes/recording_test/raw/original.wav',
           'work/./production/episodes/recording_test/raw/original.wav',
           'work/%2e/production/episodes/recording_test/raw/original.wav',
           'work/production/episodes/recording_test//raw/original.wav',
           'work//production//episodes/recording_test/raw/original.wav',
           'work/alias-dir/original.wav','work/alias-file.wav']
    for prefix in ('/files/','/api/studio/files/'):
        for path in paths:
            status,_,body=client.req('GET',prefix+path)
            assert status==403,(prefix,path,status,body)
            assert b'private synthetic raw' not in body
    preview=sp.work/'production'/'sources'/'registered.wav'
    preview.parent.mkdir();preview.write_bytes(b'registered synthetic source')
    status,_,body=client.req('GET','/files/'+sp.rel(preview))
    assert status==200 and body==preview.read_bytes()


@pytest.mark.parametrize('recover_at',['failure','restart','duplicate'])
def test_registered_source_without_job_recovers_once(wb,monkeypatch,recover_at):
    payload=tone_bytes()
    original_start=wb._start;original_recover=wb._recover_imports
    monkeypatch.setattr(wb,'_start',lambda *a,**kw: (_ for _ in ()).throw(OSError('Injected stop after registration, before job')))
    if recover_at!='failure':monkeypatch.setattr(wb,'_recover_imports',lambda:None)  # persisted state at abrupt-death boundary
    with pytest.raises(OSError,match='after registration'):upload(wb,payload,'speech.wav')
    monkeypatch.setattr(wb,'_start',original_start);monkeypatch.setattr(wb,'_recover_imports',original_recover)
    if recover_at=='restart':
        assert not wb._jobs();wb.close();obj=Workbench(wb.studio)
    else:obj=wb
    try:
        if recover_at=='duplicate':assert not obj._jobs()
        duplicate=upload(obj,payload,'speech.wav')
        job=duplicate['job'];assert job['status']=='interrupted'
        assert len(obj._jobs())==1 and len(obj.overview()['sources'])==1
        assert upload(obj,payload,'speech.wav')['job']['id']==job['id']
        retried=obj.retry({'job_id':job['id']});assert retried['id']==job['id']
        assert finish(obj,retried)['status']=='done'
        assert obj.overview()['sources'][0]['ready']
        assert len(obj._jobs())==1
    finally:
        if obj is not wb:obj.close()


def test_worker_thread_start_failure_is_retryable(wb,monkeypatch):
    payload=tone_bytes();original=threading.Thread.start
    monkeypatch.setattr(threading.Thread,'start',lambda self: (_ for _ in ()).throw(RuntimeError('Injected thread start failure')))
    with pytest.raises(RuntimeError,match='thread start'):upload(wb,payload,'speech.wav')
    job=wb.overview()['jobs'][0];assert job['status']=='failed' and 'thread start' in job['diagnostics']
    monkeypatch.setattr(threading.Thread,'start',original)
    retried=wb.retry({'job_id':job['id']});assert finish(wb,retried)['status']=='done'
    assert len(wb._jobs())==1


def test_interrupted_source_stage_retries_without_partial_final(wb,monkeypatch):
    import ave.studio.workbench as W
    payload=tone_bytes();original=W.shutil.copyfileobj
    def partial(inp,out,*a,**kw):out.write(inp.read(16));raise OSError('Injected partial source copy')
    monkeypatch.setattr(W.shutil,'copyfileobj',partial)
    with pytest.raises(OSError,match='partial source'):upload(wb,payload,'speech.wav')
    assert not list((wb.sp.work/'production'/'sources').glob('*'))
    assert not wb.overview()['sources']
    parts=list((wb.sp.work/'production'/'staging'/'copies').glob('*.part'));assert len(parts)==1 and parts[0].stat().st_size==16
    monkeypatch.setattr(W.shutil,'copyfileobj',original)
    result=upload(wb,payload,'speech.wav');assert finish(wb,result['job'])['status']=='done'
    src=wb.overview()['sources'][0];assert (wb.sp.root/src['file']).read_bytes()==payload
    # Even a valid source identity does not permit overwriting a modified final.
    (wb.sp.root/src['file']).write_bytes(b'modified final source')
    with pytest.raises(ValueError,match='hash mismatch'):upload(wb,payload,'speech.wav')
    assert (wb.sp.root/src['file']).read_bytes()==b'modified final source'


def test_interrupted_raw_stage_retries_and_modified_raw_is_preserved(wb,monkeypatch):
    import ave.studio.workbench as W
    defer_launch(wb,monkeypatch);payload=tone_bytes();job=upload(wb,payload,'speech.wav')['job'];wb._threads[-1].join()
    original=W.shutil.copyfileobj
    def partial(inp,out,*a,**kw):out.write(inp.read(16));raise OSError('Injected partial raw copy')
    monkeypatch.setattr(W.shutil,'copyfileobj',partial)
    with pytest.raises(OSError,match='partial raw'):execute_operation(wb.studio,job)
    src=W.recording(wb.sp,job['args']['source_id']);ep=W.episode_for(wb.sp,src);target=ep.raw/Path(src['file']).name
    assert not target.exists() and read(wb.sp,ep.meta_path)['workbench_source']==src['source_id']
    monkeypatch.setattr(W.shutil,'copyfileobj',original)
    result=execute_operation(wb.studio,job);assert result['analysis_audio'] and target.read_bytes()==payload
    target.write_bytes(b'changed by another editor')
    with pytest.raises(ValueError,match='refusing replacement'):execute_operation(wb.studio,job)
    assert target.read_bytes()==b'changed by another editor'


def test_episode_metadata_stage_survives_interruption_before_publication(wb,monkeypatch):
    import ave.studio.workbench as W
    defer_launch(wb,monkeypatch);job=upload(wb,tone_bytes(),'speech.wav')['job'];wb._threads[-1].join()
    sid=job['args']['source_id'];original=W._publish_staged
    def interrupt(source,destination):
        if destination.name==sid:raise OSError('Injected stop before episode publication')
        return original(source,destination)
    monkeypatch.setattr(W,'_publish_staged',interrupt)
    with pytest.raises(OSError,match='episode publication'):execute_operation(wb.studio,job)
    ep=W.episode_for(wb.sp,W.recording(wb.sp,sid));assert not ep.root.exists()
    staged=wb.sp.work/'production'/'staging'/'episodes'/sid
    assert read(wb.sp,staged/'episode.json')['workbench_source']==sid
    monkeypatch.setattr(W,'_publish_staged',original)
    result=execute_operation(wb.studio,job)
    assert result['analysis_audio'] and read(wb.sp,ep.meta_path)['workbench_source']==sid
    assert not staged.exists()


@pytest.mark.parametrize('directory',[False,True])
def test_atomic_publication_refuses_existing_destination(tmp_path,directory):
    from ave.studio.workbench import _publish_staged
    source=tmp_path/'staged';dest=tmp_path/'final'
    if directory:
        source.mkdir();dest.mkdir();(source/'owned').write_text('new');(dest/'unrelated').write_text('keep')
    else:source.write_text('new');dest.write_text('keep')
    with pytest.raises(FileExistsError):_publish_staged(source,dest)
    assert source.exists()
    assert (dest/'unrelated').read_text()=='keep' if directory else dest.read_text()=='keep'


def test_unowned_episode_folder_is_never_replaced(wb,monkeypatch):
    import ave.studio.workbench as W
    defer_launch(wb,monkeypatch);job=upload(wb,tone_bytes(),'speech.wav')['job'];wb._threads[-1].join()
    ep=W.episode_for(wb.sp,W.recording(wb.sp,job['args']['source_id']));ep.root.mkdir(parents=True)
    (ep.root/'unrelated.txt').write_text('keep this folder')
    with pytest.raises(ValueError,match='without ownership'):execute_operation(wb.studio,job)
    assert (ep.root/'unrelated.txt').read_text()=='keep this folder'


def test_retry_preserves_original_revision_and_newer_media(wb,monkeypatch):
    upload(wb);defer_launch(wb,monkeypatch);job=wb.start_render(render_args(wb));wb._threads[-1].join()
    job['status']='interrupted';write(wb.sp,job_path(wb.sp,job['id']),job)
    post=store.load_post(wb.sp,job['post']);new_media=[{'asset':'a_0123456789ab','slot':0,'role':'image','alt':'Chosen later'}]
    newer=wb.studio.edit_post(post['id'],{'media':new_media,'copy.base':'Newer caption'},expected_rev=post['rev'])
    retry=wb.retry({'job_id':job['id']});wb._threads[-1].join()
    assert retry['post']==job['post'] and retry['expected_rev']==job['expected_rev']<newer['rev']
    result=execute_operation(wb.studio,retry)
    assert result['attachment_conflict'] and not result['attached'] and result['assets']
    current=store.load_post(wb.sp,post['id']);assert current['media']==new_media and current['copy']['base']=='Newer caption'


def test_real_worker_retains_lock_after_parent_process_death(wb,tmp_path):
    """Real _launch/Popen/worker.main; only an operation-boundary SIGSTOP gate.

    Killing the parent leaves the stopped worker and its inherited descriptor
    alive. Killing that worker permits recovery and a real retry/render.
    """
    import signal, subprocess, sys
    from ave.ui.jobs import child_env
    upload(wb)
    control=tmp_path/'process-control';control.mkdir()
    worker_script=control/'worker_fixture.py';parent_script=control/'parent_fixture.py'
    worker_script.write_text('''import os, signal, sys
from pathlib import Path
import ave.studio.workbench_worker as worker
operation = worker.execute_operation
def stopped(studio, job):
    Path(sys.argv[3]).write_text(str(os.getpid()))
    os.kill(os.getpid(), signal.SIGSTOP)
    return operation(studio, job)
worker.execute_operation = stopped
raise SystemExit(worker.main(sys.argv[1:3]))
''')
    parent_script.write_text('''import json, signal, sys
from pathlib import Path
import ave.studio.workbench as workbench
from ave.studio.service import open_studio
root, wrapper, pid_file, job_file = sys.argv[1:]
workbench.worker_argv = lambda workspace, jid: [sys.executable, wrapper, str(workspace), jid, pid_file]
obj = workbench.Workbench(open_studio(root))
src = obj.overview()['sources'][0]
job = obj.start_render({'source_id': src['source_id'], 'sentence_id': src['sentences'][0]['id'], 'recipe': 'quote-post', 'style': 'paper', 'format': '4x5'})
Path(job_file).write_text(json.dumps(job))
while True: signal.pause()
''')
    pid_file=control/'worker.pid';job_file=control/'job.json'
    parent=subprocess.Popen([sys.executable,str(parent_script),str(wb.sp.root),str(worker_script),str(pid_file),str(job_file)],
                            env=child_env(),stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    child_pid=None;second=None;recovered=None
    def until(fn,seconds=10):
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            if fn():return
            time.sleep(.05)
        assert fn()
    try:
        until(lambda:pid_file.exists() and pid_file.stat().st_size>0 and job_file.exists())
        child_pid=int(pid_file.read_text());job=json.loads(job_file.read_text())
        assert wb.tail(job['id'])['status']=='running'
        parent.kill();assert parent.wait(timeout=5)==-signal.SIGKILL
        os.kill(child_pid,0)  # OS confirms the worker still exists after parent death.
        second=Workbench(wb.studio)
        assert second.tail(job['id'])['status']=='running'
        with pytest.raises(Busy):second.start_render(render_args(second))
        assert len(second._jobs())==1
        os.kill(child_pid,signal.SIGKILL)
        def lock_released():
            try:held=second._claim()
            except Busy:return False
            held.close();return True
        until(lock_released)
        recovered=Workbench(wb.studio)
        assert recovered.tail(job['id'])['status']=='interrupted'
        retry=recovered.retry({'job_id':job['id']});done=finish(recovered,retry)
        assert done['status']=='done' and done['result']['attached']
        assert len(recovered._jobs())==1 and len(store.list_posts(wb.sp))==1
        evidence=Path('/private/tmp/ave-workbench-acceptance-20261029/fix-round1-subprocess.json')
        evidence.parent.mkdir(exist_ok=True)
        evidence.write_text(json.dumps({'scope':'Real parent death and surviving worker; test-only SIGSTOP before operation execution',
            'parent_pid':parent.pid,'worker_pid':child_pid,'parent_exit':parent.returncode,'job_id':job['id'],
            'while_worker_alive':'running; second start refused; one job',
            'after_worker_kill':'interrupted; retry preserved job/post; actual renderer completed',
            'result':done['result'],'job_count':1,'post_count':1},indent=2)+'\n')
    finally:
        if parent.poll() is None:parent.kill();parent.wait(timeout=5)
        if child_pid:
            try:os.kill(child_pid,signal.SIGKILL)
            except ProcessLookupError:pass
        if second:second.close()
        if recovered:recovered.close()
        if parent.stderr:parent.stderr.close()


def test_old_thread_leaves_a_retry_alone_even_past_twenty_attempts(wb,monkeypatch):
    import sys
    upload(wb)
    argv=lambda code:(lambda root,jid:[sys.executable,'-c',code])
    monkeypatch.setattr('ave.studio.workbench.worker_argv',argv('pass'))              # exits without settling: the job ends failed
    job=wb.start_render(render_args(wb));assert finish(wb,job)['status']=='failed'
    seeded=wb.tail(job['id']);seeded['attempts']=[{'status':'failed','diagnostics':'','at':'t'}]*20      # attempts[] is at its cap of 20
    write(wb.sp,job_path(wb.sp,job['id']),seeded)
    monkeypatch.setattr('ave.studio.workbench.worker_argv',argv('import time;time.sleep(1.5)'))
    before=wb._job_processes[job['id']]
    retry=wb.retry({'job_id':job['id']});first=retry['attempt_id']                   # a len-based guard would record 20 here
    end=time.monotonic()+10
    while wb._job_processes[job['id']] is before and time.monotonic()<end:time.sleep(.01)   # the retry's worker is alive
    assert wb._job_processes[job['id']] is not before
    taken=wb.tail(job['id']);taken.update(status='running',attempt_id='newer-retry',attempts=[{'status':'failed','diagnostics':'','at':'t'}]*20)
    write(wb.sp,job_path(wb.sp,job['id']),taken)                                     # still 20 attempts, but a different attempt
    wb.close()                                                                       # joins the retry's thread as its worker exits
    after=wb.tail(job['id'])
    assert first!='newer-retry' and after['attempt_id']=='newer-retry'
    assert after['status']=='running' and not after['diagnostics']
