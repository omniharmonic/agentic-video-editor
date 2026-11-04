from pathlib import Path
import json
import sys
import types
import threading
import pytest
from ave.desktop_auth import NativeAuthority
from ave.desktop_native import navigation, NativeSession, REPORT_FIELDS
from ave.desktop_files import save_verified, import_selected
from ave.desktop_runtime import digest, verify_component, install_component
from ave.studio.project import StudioProject
from ave.studio.service import open_studio
from ave.studio.ui.api import StudioApp, StudioError

@pytest.fixture
def no_markers(monkeypatch):
    from ave.humanpresence import AGENT_MARKERS
    for k in AGENT_MARKERS: monkeypatch.delenv(k,raising=False)

@pytest.fixture
def native_context(monkeypatch,no_markers):
    class Context:
        available=True;callback=None;invalidated=False
        @classmethod
        def alloc(cls):return cls()
        def init(self):return self
        def canEvaluatePolicy_error_(self,*a):return self.available,None
        def evaluatePolicy_localizedReason_reply_(self,policy,reason,callback):
            assert policy==2
            self.callback=callback
        def invalidate(self):self.invalidated=True
    class Queue:
        @classmethod
        def mainQueue(cls):return cls()
        def addOperationWithBlock_(self,fn):fn()
    monkeypatch.setitem(sys.modules,'Foundation',types.SimpleNamespace(NSOperationQueue=Queue))
    monkeypatch.setitem(sys.modules,'LocalAuthentication',types.SimpleNamespace(LAContext=Context,LAPolicyDeviceOwnerAuthentication=2))
    return Context

@pytest.mark.parametrize('outcome',[False,True])
def test_auth_callback_and_shutdown(native_context,outcome):
    authority=NativeAuthority();results=[]
    authority.request('Review this workspace',results.append)
    assert not authority.active
    context=authority._context;context.callback(outcome,None)
    # success is a single-use grant bound to the act that asked, never a session-wide switch
    assert len(results)==1 and bool(results[0])==outcome and not authority.active and context.invalidated
    with pytest.raises(StudioError):authority.require('Review this workspace')
    with pytest.raises(TypeError):authority.require()           # every check names its act (Task 23 minor)
    authority.close()
    with pytest.raises(StudioError):authority.require('Review this workspace')
    context.callback(True,None)
    assert not authority.active and len(results)==1

def test_unavailable_and_late_callback(native_context):
    native_context.available=False
    authority=NativeAuthority();results=[];authority.request('review',results.append)
    assert results==[False] and not authority.active
    native_context.available=True;authority.request('review',results.append)
    c=authority._context;authority.cancel();c.callback(True,None)
    assert results==[False,False] and not authority.active          # the cancelled check answers "no" instead of hanging

def test_timeout_invalidates_then_rejects_late_success(native_context,monkeypatch):
    timers=[]
    class Timer:
        def __init__(self,delay,fn):self.fn=fn;timers.append(self)
        def start(self):pass
        def cancel(self):pass
    monkeypatch.setattr('ave.desktop_auth.threading.Timer',Timer)
    a=NativeAuthority();results=[];a.request('review',results.append);c=a._context
    timers[-1].fn();c.callback(True,None)
    assert results==[False] and not a.active and c.invalidated

def test_agent_never_grants(native_context,monkeypatch):
    monkeypatch.setenv('AVE_AGENT','1')
    a=NativeAuthority();out=[];a.request('review',out.append)
    assert out==[False]
    with pytest.raises(RuntimeError):NativeSession('/unopened')

@pytest.mark.parametrize('url',['https://example.com','file:///tmp/file','http://localhost:5555','http://127.0.0.1:5556','http://user@127.0.0.1:5555','javascript:alert(1)'])
def test_navigation_rejects_outside_origin(url):assert navigation(url,'http://127.0.0.1:5555/?t=secret')=='deny'

def test_navigation_allows_authenticated_local_paths():
    assert navigation('http://127.0.0.1:5555/files/work/manual_exports/p.zip','http://127.0.0.1:5555/?t=secret')=='local'

def test_authoring_server_protected_routes_and_no_secret_reads(tmp_path,monkeypatch):
    sp=StudioProject(tmp_path/'studio');sp.create()
    studio=open_studio(sp.root,publishers={},host=None);studio.native_authoring=True
    from ave.studio import views
    monkeypatch.setattr(views,'_credentials',lambda *a:pytest.fail('credential read'))
    (sp.accounts_dir/'test.yaml').write_text('id: test\nplatform: instagram\nmode: manual\nlabel: Test\n')
    a=NativeAuthority();app=StudioApp(studio,native_authority=a)
    try:
        for route in ('broll/pick','accounts/auth','send'):
            with pytest.raises(StudioError) as err:app.post(route,{'human':True,'authorized':True})
            assert err.value.status==403
        for route in ('approve','resume','reject','tick/rehearse'):            # gone with the old shell: no route approves
            with pytest.raises(StudioError) as err:app.post(route,{'human':True,'authorized':True})
            assert err.value.status==404
        assert app.get('summary',{})[1]['ui']['native']
        rows=app.get('accounts',{})[1]['accounts']
        assert rows[0]['credentials']['status']=='unverified'
        assert app.pin.reveal() not in json.dumps(app.get('summary',{})[1])
        with pytest.raises(StudioError):app.record_view('p_missing',{})
    finally:app.close()

def test_save_exact_bytes_cancel_and_no_overwrite(tmp_path):
    src=tmp_path/'source.zip';src.write_bytes(b'PK exact bytes')
    target=tmp_path/'saved.zip'
    save_verified(src,target,digest(src));assert target.read_bytes()==src.read_bytes()
    with pytest.raises(ValueError):save_verified(src,target,digest(src))
    with pytest.raises(ValueError):save_verified(src,tmp_path/'cancel.zip',digest(src),cancelled=lambda:True)
    with pytest.raises(ValueError):save_verified(src,tmp_path/'wrong.zip','0'*64)
    assert not list(tmp_path.glob('.ave-save-*'))
    assert not (tmp_path/'wrong.zip').exists()

def test_component_inventory_corruption_hidden_marker_and_symlink(tmp_path):
    c=tmp_path/'diarization';c.mkdir();p=c/'.fluidaudio-revision';p.write_text('pinned')
    manifest={'components':{'diarization':{'files':{p.name:digest(p)}}}}
    assert verify_component(tmp_path,manifest,'diarization')==c
    p.unlink()
    with pytest.raises(ValueError):verify_component(tmp_path,manifest,'diarization')
    p.write_text('changed')
    with pytest.raises(ValueError):verify_component(tmp_path,manifest,'diarization')
    p.unlink();p.symlink_to(tmp_path/'elsewhere')
    with pytest.raises(ValueError):verify_component(tmp_path,manifest,'diarization')

def test_component_install_cancel_retry_and_space(tmp_path,monkeypatch):
    source=tmp_path/'source'/'asr';source.mkdir(parents=True);(source/'model').write_bytes(b'pinned model')
    expected={'files':{'model':digest(source/'model')}};target=tmp_path/'destination'/'asr'
    with pytest.raises(ValueError):install_component(source,target,expected,cancelled=lambda:True)
    assert not target.exists() and not list(target.parent.glob('.component-*'))
    assert install_component(source,target,expected)==target
    assert install_component(source,target,expected)==target
    monkeypatch.setattr('ave.desktop_runtime.shutil.disk_usage',lambda p:types.SimpleNamespace(free=0))
    with pytest.raises(ValueError):install_component(source,tmp_path/'no-space'/'asr',expected)

def test_large_native_intake_streams_without_browser_cap(tmp_path):
    # Sparse transport boundary; actual 600 MiB decodable fixture is exercised by
    # the separate installed-app acceptance journey.
    sp=StudioProject(tmp_path/'studio');sp.create()
    source=tmp_path/'camera.mp4'
    with source.open('wb') as out:out.truncate(513*1024**2)
    seen=[]
    def register(path,name,size,sha):
        assert size==source.stat().st_size and sha==digest(source) and digest(path)==sha
        seen.append(size);return {'source_id':'synthetic'}
    wb=types.SimpleNamespace(sp=sp,_threads=[],_register_staged=register)
    assert import_selected(wb,[source])==[{'source_id':'synthetic'}]
    assert seen==[513*1024**2] and source.exists()
    assert import_selected(wb,[source])==[{'source_id':'synthetic'}]
    assert len(seen)==2
    with pytest.raises(ValueError):import_selected(wb,[source],cancelled=lambda:True)
    assert not list((sp.work/'native-intake').iterdir())

def test_worker_rejects_unknown_fields(tmp_path,monkeypatch):
    from ave.desktop_worker import production
    monkeypatch.setattr('ave.desktop_worker.job_directory',lambda:tmp_path)
    jid='f'*32;(tmp_path/(jid+'.json')).write_text(json.dumps({'episode':'/tmp','cmd':'status','args':{},'python':'evil'}))
    with pytest.raises(ValueError,match='schema'):production(jid)
    with pytest.raises(ValueError,match='identity'):production('../escape')

@pytest.mark.parametrize('start,end,valid', [(-.04,1.0679,True),(0,1.101,False),(0,float('nan'),False),(.8,.7,False)])
def test_native_model_bounds_preserve_raw(tmp_path,start,end,valid):
    import soundfile as sf
    import numpy as np
    from ave.transcribe import _bounded_model_times
    wav=tmp_path/'source.wav';sf.write(wav,np.zeros(16000),16000)
    raw=[{'start':start,'end':end,'speaker':'S1'}]
    if valid:
        assert _bounded_model_times(raw,wav,'start','end')==[{'start':0.,'end':1.,'speaker':'S1'}]
        assert raw[0]['end']==end and raw[0]['start']==start
    else:
        with pytest.raises(SystemExit,match='source bounds'):_bounded_model_times(raw,wav,'start','end')


def test_mocked_native_success_still_needs_pin_exact_view_and_consumes(tmp_path,native_context,monkeypatch):
    # Framework injection exists only in this isolated test. No production flag.
    sp=StudioProject(tmp_path/'studio');sp.create()
    studio=open_studio(sp.root,publishers={},host=None)
    authority=NativeAuthority();app=StudioApp(studio,fixtures=True,native_authority=authority)
    monkeypatch.setattr(app,'_approval_mode',lambda:'view')
    sha='a'*64;item={'post':'synthetic','variant':'synthetic','sha':sha}
    body={'by':'Synthetic reviewer','items':[item],'pin':app.pin.reveal()}
    try:
        grants=[];authority.request('Synthetic review',grants.append,action='approvePublish',keys=[app.approval_key('synthetic','synthetic',sha)])
        authority._context.callback(True,None)
        with pytest.raises(StudioError):app.approve(body)                     # minted, but not spent on this thread
        with authority.use(grants[0],'approvePublish',[app.approval_key('synthetic','synthetic',sha)]):
            with pytest.raises(StudioError):app.approve({**body,'pin':'wrong'})
            assert app.approve(body)['refused'] and not app.views
            app.record_view('synthetic',{'variant':'synthetic','sha':sha,'displayed_ms':10000})
            with pytest.raises(StudioError):app.approve({**body,'items':[{**item,'sha':'b'*64}]})   # the grant names sha a only
            assert app.approve(body)['approved'] and not app.views
            assert app.approve(body)['refused']
        with pytest.raises(StudioError):app.approve(body)
        authority.close()
        with pytest.raises(StudioError):app.approve(body)
    finally:app.close()


def test_initial_native_confirmation_cancel_revokes_consumer_and_late_callbacks(tmp_path,native_context):
    # Execute the real Controller.unlock method with only its AppKit alert
    # replaced. AST extraction avoids importing/launching any real Cocoa UI.
    import ast
    import ave.desktop_native as native
    tree=ast.parse(Path(native.__file__).read_text())
    main=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='main')
    controller=next(n for n in main.body if isinstance(n,ast.ClassDef) and n.name=='Controller')
    method=next(n for n in controller.body if isinstance(n,ast.FunctionDef) and n.name=='unlock')
    method.decorator_list=[]
    namespace={'fixture':False,'alert':lambda *a:None}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[method],type_ignores=[])),native.__file__,'exec'),namespace)
    sp=StudioProject(tmp_path/'studio');sp.create();studio=open_studio(sp.root,publishers={},host=None)
    authority=NativeAuthority();app=StudioApp(studio,fixtures=True,native_authority=authority)
    fake=types.SimpleNamespace(session=types.SimpleNamespace(authority=authority,server=types.SimpleNamespace(app=app)),reply=lambda *a:None)
    try:
        grants=[];key=app.approval_key('synthetic','one','a'*64)
        authority.request('first session',grants.append,action='approvePublish',keys=[key]);old=authority._context;old.callback(True,None)
        with authority.use(grants[0],'approvePublish',[key]):app.record_view('synthetic',{'variant':'one','sha':'a'*64,'displayed_ms':10000})
        namespace['unlock'](fake,'1',lambda grant:None,None,'approvePublish',[key])
        assert not app.views
        with pytest.raises(StudioError,match='authenticate'):app.record_view('synthetic',{'variant':'one','sha':'a'*64})
        with pytest.raises(StudioError,match='authenticate'):app.approve({'by':'test','pin':app.pin.reveal(),'items':[]})
        old.callback(True,None)
        with pytest.raises(StudioError):app.approve({})
        # A pending OS callback from the old workspace cannot restore authority.
        authority.request('pending request',lambda ok:pytest.fail('stale callback delivered'))
        pending=authority._context;authority.close()
        replacement=NativeAuthority();app.native_authority=replacement
        pending.callback(True,None)
        with pytest.raises(StudioError):app.record_view('synthetic',{})
        assert not replacement.active
    finally:app.close()

@pytest.mark.parametrize('active',[False,True])
def test_native_delivery_details_never_touch_adapter_secrets(tmp_path,monkeypatch,native_context,active):
    from ave.platforms.accounts import Account
    from ave.platforms.base import PlatformAdapter
    from ave.publish import safety
    from ave.studio import delivery
    from datetime import datetime,timezone
    sp=StudioProject(tmp_path/'studio');sp.create()
    studio=open_studio(sp.root,publishers={},host=None);studio.native_authoring=True
    account=Account('synthetic-ln','linkedin',cred_ref='synthetic-ln',accounts_dir=sp.accounts_dir)
    v={'id':'v_test','account':account.id,'platform':'linkedin','delivery':{'method':'automatic'}}
    monkeypatch.setattr('ave.studio.store.load_post',lambda *a:{'variants':[v]})
    monkeypatch.setattr('ave.studio.store.read_assets',lambda *a:{})
    monkeypatch.setattr('ave.platforms.accounts.load_account',lambda *a:account)
    monkeypatch.setattr('ave.studio.sync._spec',lambda *a:({'media_type':'text','visibility':'unlisted','caption':'Synthetic','title':'Synthetic','platforms':['linkedin'],'files':[]},[]))
    secrets=[];calls=[]
    real_secret=Account.secret
    monkeypatch.setattr(Account,'secret',lambda self,k:(calls.append(k),real_secret(self,k))[1])
    monkeypatch.setattr(safety,'get_secret',lambda k:secrets.append(k) or None)
    monkeypatch.setattr(PlatformAdapter,'_send',lambda *a,**kw:pytest.fail('provider request'))
    authority=NativeAuthority()
    if active:
        authority.request('synthetic',lambda ok:None);authority._context.callback(True,None)
    app=StudioApp(studio,native_authority=authority)
    try:
        result=app.get('posts/p_test/delivery-check',{'variant':['v_test']})[1]
        assert not secrets and not calls and result['selected_adapter']['checked'] is False
        assert not result['credential_evidence']['checked'] and 'unverified' in ' '.join(result['problems'])
        # Control: unchanged non-native preflight reaches the actual secret fallback.
        studio.native_authoring=False
        delivery.capabilities(studio,'p_test','v_test')
        assert calls and secrets==['linkedin_author@synthetic-ln']
    finally:app.close()

@pytest.mark.parametrize('active',[False,True])
def test_native_refresh_blocks_actual_provider_consumer_even_when_unlocked(tmp_path,monkeypatch,native_context,active):
    from ave.platforms.accounts import Account
    from ave.platforms.instagram import InstagramAdapter
    from ave.publish import safety
    from ave.studio import delivery
    sp=StudioProject(tmp_path/'studio');sp.create();studio=open_studio(sp.root,publishers={},host=None)
    adapter=InstagramAdapter(Account('synthetic-ig','instagram'))
    secrets=[];requests=[]
    monkeypatch.setattr(safety,'get_secret',lambda k:secrets.append(k) or 'synthetic')
    monkeypatch.setattr(adapter,'_send',lambda *a,**kw:requests.append((a,kw)) or types.SimpleNamespace(data={'status_code':'IN_PROGRESS'}))
    monkeypatch.setattr(delivery,'refresh',lambda *a:adapter.recover({}, {'container':'synthetic-pending'}))
    authority=NativeAuthority()
    if active:
        authority.request('synthetic',lambda ok:None);authority._context.callback(True,None)
    app=StudioApp(studio,native_authority=authority)
    try:
        with pytest.raises(StudioError) as error:app.post('posts/p_test/delivery-refresh',{'variant':'v_test'})
        assert error.value.status==403 and not secrets and not requests
        app.native_authority=None
        assert app.post('posts/p_test/delivery-refresh',{'variant':'v_test'})[0]==200
        assert secrets and len(requests)==1 # real adapter recover reached intercepted consumers
    finally:app.close()


def test_native_file_work_cancel_shutdown_and_main_dispatch(tmp_path):
    from ave.desktop_files import NativeFileWork,save_verified
    import queue,time
    ui=queue.Queue();owner=NativeFileWork(ui.put);main=threading.get_ident();threads=[];completed=[]
    source=tmp_path/'large.zip';source.write_bytes(b'a'*(8*1024**2));target=tmp_path/'saved.zip'
    started=threading.Event();release=threading.Event()
    def work(cancel,progress):
        threads.append(threading.get_ident());started.set();release.wait(2)
        return save_verified(source,target,digest(source),cancelled=cancel,progress=progress)
    owner.start(work,lambda result,error:completed.append((threading.get_ident(),error)),committed_on_return=True)
    assert started.wait(1) and threads[0]!=main and owner.busy
    owner.cancel();release.set();owner.thread.join(2)
    while not ui.empty():ui.get()()
    assert completed and completed[0][0]==main and 'cancelled' in completed[0][1]
    assert not target.exists() and not list(tmp_path.glob('.ave-save-*'))
    owner.start(lambda cancel,progress:'prepared',lambda *a:pytest.fail('completion after shutdown'))
    owner.close()
    while not ui.empty():ui.get()()
    assert not owner.thread.is_alive()


def test_package_verification_streams_zip_and_can_cancel(tmp_path,monkeypatch):
    from ave.desktop_fixture import create
    from ave.desktop_files import package_file
    from ave.studio.manual_export import load_package
    import zipfile
    import os
    monkeypatch.setenv('AVE_APPROVAL_KEY',os.environ.get('AVE_APPROVAL_KEY','synthetic-test-key'))
    studio,temp=create()
    try:
        manifest=next((studio.sp.work/'manual_exports').glob('*/manifest.json'))
        pid=json.loads(manifest.read_text())['package_id']
        monkeypatch.setattr(zipfile.ZipFile,'read',lambda *a:pytest.fail('unbounded ZIP read'))
        source,sha,m=package_file(studio.sp,pid)
        assert digest(source)==sha
        seen=[]
        with pytest.raises(ValueError,match='cancelled'):
            load_package(studio.sp,pid,cancelled=lambda:bool(seen),progress=lambda n,total:seen.append(n))
    finally:temp.cleanup()


def test_native_critical_transaction_finishes_once_without_stale_ui():
    from ave.desktop_files import NativeFileWork
    import queue
    ui=queue.Queue();owner=NativeFileWork(ui.put);started=threading.Event();release=threading.Event();calls=[]
    def transaction(cancel,progress):
        calls.append('started');started.set();release.wait(2)
        assert not cancel();calls.append('committed');return 'receipt'
    owner.start(transaction,lambda *a:pytest.fail('late UI result'),critical=True)
    assert started.wait(1)
    owner.cancel();assert not owner.cancelled.is_set()
    with pytest.raises(ValueError):owner.start(transaction,lambda *a:None,critical=True)
    closer=threading.Thread(target=owner.close);closer.start()
    assert closer.is_alive();release.set();closer.join(2)
    while not ui.empty():ui.get()()
    assert calls==['started','committed'] and not closer.is_alive()

@pytest.mark.parametrize('cancel_stage',['confirmation','pin'])
def test_native_report_cancel_before_dispatch_never_starts_transaction(tmp_path,monkeypatch,cancel_stage):
    import ast,queue
    import ave.desktop_native as native
    from ave.desktop_files import NativeFileWork
    tree=ast.parse(Path(native.__file__).read_text());main=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='main')
    cls=next(n for n in main.body if isinstance(n,ast.ClassDef) and n.name=='Controller')
    method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='report');method.decorator_list=[]
    replies=[];ui=queue.Queue();owner=NativeFileWork(ui.put)
    request=dict.fromkeys(native.REPORT_FIELDS,'synthetic');request.update(post_id='p_test',variant_id='v_test',reviewed_payload_hash='a'*64,expected_rev=1)
    m={'reviewed':True,'post':'p_test','variant':'v_test','reviewed_payload_hash':'a'*64,'package_id':'synthetic','account':'synthetic','rev':1}
    monkeypatch.setattr('ave.studio.store.load_post',lambda *a:{'rev':1})
    def alert(title,*args):return True if title.startswith('Record this') and cancel_stage=='pin' else None
    ns={'__name__':'ave.desktop_native','REPORT_FIELDS':native.REPORT_FIELDS,'package_file':lambda *a,**k:(None,None,m),'alert':alert}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[method],type_ignores=[])),native.__file__,'exec'),ns)
    session=types.SimpleNamespace(server=types.SimpleNamespace(app=types.SimpleNamespace(ep=types.SimpleNamespace(root=tmp_path))),record=lambda *a:pytest.fail('cancelled transaction'))
    fake=types.SimpleNamespace(busy=False,session=session,file_work=owner,window=types.SimpleNamespace(setTitle_=lambda *a:None),reply=lambda *a:replies.append(a),unlock=lambda rid,after,purpose=None,**bound:after(object()))
    ns['report'](fake,request,'1');owner.thread.join(2)
    while not ui.empty():ui.get()()
    assert replies[-1][1] is False and 'cancelled' in replies[-1][2]['error'].lower()
    owner.close()


def test_committed_save_reports_success_after_cancel_at_actual_publication(tmp_path,monkeypatch):
    import os,queue
    from ave.desktop_files import NativeFileWork,save_verified
    ui=queue.Queue();owner=NativeFileWork(ui.put);linked=threading.Event();release=threading.Event();completions=[]
    source=tmp_path/'source.zip';source.write_bytes(b'exact committed package'*10000)
    target=tmp_path/'saved.zip';expected=digest(source)
    from ave.atomic import publish as link
    def pause_after_real_link(src,dst):
        link(src,dst);linked.set();assert release.wait(2)
    monkeypatch.setattr('ave.desktop_files.publish_atomic',pause_after_real_link)
    owner.start(lambda cancel,progress:save_verified(source,target,expected,cancelled=cancel,progress=progress),lambda result,error:completions.append((result,error)),committed_on_return=True)
    assert linked.wait(2) and target.is_file()
    owner.cancel();release.set();owner.thread.join(2)
    while not ui.empty():ui.get()()
    assert completions==[(target,None)] and digest(target)==expected
    assert not list(tmp_path.glob('.ave-save-*')) and not owner.busy
    owner.close()


def test_noncommitting_preparation_remains_cancellable_at_return():
    import queue
    from ave.desktop_files import NativeFileWork
    ui=queue.Queue();owner=NativeFileWork(ui.put);ready=threading.Event();release=threading.Event();results=[]
    def prepare(cancel,progress):ready.set();assert release.wait(2);return 'prepared'
    owner.start(prepare,lambda result,error:results.append((result,error)))
    assert ready.wait(1);owner.cancel();release.set();owner.thread.join(2)
    while not ui.empty():ui.get()()
    assert len(results)==1 and results[0][0] is None and 'cancelled' in results[0][1]
    owner.close()
