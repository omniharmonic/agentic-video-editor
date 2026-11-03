"""Synthetic native action/service/provider contracts; no OS credentials or jobs."""
import json
import plistlib
import time
from pathlib import Path
from types import SimpleNamespace
import pytest
from ave.studio import connections as C
from ave.studio.project import StudioProject
from ave.studio.service import open_studio
from ave.platforms.accounts import save_account,load_account
from ave.platforms.connection_verify import Reader,verify,VerificationError
from ave.desktop_connections import Coordinator
from ave.desktop_service import Service,identity,record,heartbeat

class Vault:
    def __init__(self):self.values={};self.fail=None
    def get(self,k):return self.values.get(k)
    def set(self,k,v):
        if self.fail and self.fail in k:raise RuntimeError('FAKE_SECRET_REFUSED_DO_NOT_EXPOSE')
        self.values[k]=v
    def remove(self,k):self.values.pop(k,None)
class Transport:
    def __init__(self,*results):self.results=list(results);self.calls=[]
    def request(self,method,url,**kw):
        self.calls.append((method,url,kw));out=self.results.pop(0)
        if isinstance(out,Exception):raise out
        status,data=out if isinstance(out,tuple) else (200,out)
        return SimpleNamespace(status_code=status,iter_content=lambda n:[json.dumps(data).encode()],close=lambda:None)
class Authority:
    def __init__(self):self.active=False
    def require(self):
        if not self.active:raise ValueError('synthetic authority refused')
class FakeServiceAdapter:
    def __init__(self,root):self.root=root;self.loaded=False;self.running=False;self.pid=None;self.last_exit=None;self.fail=False;self.calls=[];self.arguments=None
    def path(self,label):return self.root/(label+'.plist')
    def state(self,label):return {'loaded':self.loaded,'running':self.running,'pid':self.pid,'last_exit':self.last_exit,'program':self.arguments[0] if self.arguments else None,'arguments':self.arguments}
    def load(self,label):
        self.calls.append(('load',label))
        if self.fail:raise ValueError('synthetic service failed')
        self.loaded=True;self.arguments=plistlib.loads(self.path(label).read_bytes())['ProgramArguments']
    def unload(self,label,*,expected=None):
        if self.loaded and (not expected or self.arguments!=expected):raise ValueError('Loaded service ownership is unknown or changed')
        self.calls.append(('unload',label));self.loaded=False;self.running=False
@pytest.fixture(autouse=True)
def isolate_synthetic_redaction_registry(monkeypatch):
    from ave.publish import safety
    monkeypatch.setattr(safety,'_KNOWN_SECRETS',set(safety._KNOWN_SECRETS))


@pytest.fixture
def studio(tmp_path,monkeypatch):
    # Isolated signing function; preserve every real agent/environment marker.
    from ave.publish import safety
    monkeypatch.setattr(safety,'approval_key',lambda:b'isolated-connections-test-key')
    sp=StudioProject(tmp_path/'studio');sp.create();s=open_studio(sp.root,publishers={},host=None);s.native_authoring=True
    return s

def account(studio,platform='instagram',public=None,product=None):
    public=public or {'ig_user_id':'ig1'};product=product or 'instagram_login'
    save_account(studio.sp,{'id':'sample','platform':platform,'mode':'graph','credentials':{'ref':'synthetic-only',**public},'connection':{'product':product,'root_revision':'a'*32}})
    return load_account(studio.sp,'sample')

def stored(studio,a,vault):
    values={f:'synthetic-value-'+f for f in C.fields(a)}
    return C.credentials(studio,a.id,C.revision(studio.sp,a.id),values,keychain=vault)

def service(studio,tmp_path):
    exe=tmp_path/'AVE & Studio';exe.write_text('synthetic executable');exe.chmod(0o700)
    return Service(studio.sp,adapter=FakeServiceAdapter(tmp_path/'private-agents'),exe=exe)

def coordinator(studio,tmp_path,**kw):
    authority=Authority();session=SimpleNamespace(fixture=False,closed=False,authority=authority,server=SimpleNamespace(app=SimpleNamespace(studio=studio)))
    return Coordinator(session,service=service(studio,tmp_path),**kw),authority

def args(studio,a):return {'account':a.id,'revision':C.revision(studio.sp,a.id)}

def test_stored_not_verified_and_partial_update(studio):
    a=account(studio,'youtube',{'channel_id':'channel1','client_id':'client1'},'youtube');vault=Vault();vault.fail='credentials'
    result=stored(studio,a,vault);assert result['state']=='partial_update';row=C.overview(studio)['accounts'][0]
    assert not row['verified'] and not row['live_enabled'];assert 'synthetic-value' not in json.dumps(row)
    vault.fail=None;stored(studio,a,vault);assert C.overview(studio)['accounts'][0]['credential_state']=='stored'
    assert not C.overview(studio)['accounts'][0]['verified']

def test_root_replacement_invalidates_live_and_payload_binding(studio):
    from ave.platforms.attest import write_live,verify_live
    from ave.studio.dispatch import approval_bind
    a=account(studio);vault=Vault();stored(studio,a,vault)
    write_live(studio.sp.accounts_dir,a.id,'synthetic owner');assert verify_live(studio.sp.accounts_dir,a.id).ok
    before=approval_bind(studio.sp,{'account':a.id});stored(studio,a,vault)
    assert not verify_live(studio.sp.accounts_dir,a.id).ok
    assert before['connection']!=approval_bind(studio.sp,{'account':a.id})['connection']

def test_context_auth_cancel_stale_replay(studio,tmp_path):
    a=account(studio);vault=Vault();c,auth=coordinator(studio,tmp_path,keychain=vault)
    request=c.prepare('connectionCredentials',args(studio,a),'1')
    with pytest.raises(ValueError,match='authority'):c.execute(request['token'],{'token':'synthetic'})
    auth.active=True
    with pytest.raises(ValueError,match='already'):c.execute(request['token'],{'token':'synthetic'})
    with pytest.raises(ValueError,match='replayed'):c.prepare('connectionCredentials',args(studio,a),'1')
    request=c.prepare('connectionCredentials',args(studio,a),'2');c.cancel(request['token'])
    with pytest.raises(ValueError):c.execute(request['token'],{'token':'synthetic'})
    request=c.prepare('connectionCredentials',args(studio,a),'3');stored(studio,a,vault)
    with pytest.raises(ValueError,match='changed'):c.execute(request['token'],{'token':'synthetic'})
    assert all(v!='synthetic' for v in vault.values.values())

def test_native_credentials_then_verified_actual_evidence(studio,tmp_path):
    a=account(studio);vault=Vault();http=Transport({'id':'app_scoped','user_id':'ig1','username':'name','access_token':'MUST_NOT_PERSIST'})
    c,auth=coordinator(studio,tmp_path,keychain=vault,reader_factory=lambda:Reader(http));auth.active=True
    p=c.prepare('connectionCredentials',args(studio,a),'1');assert c.execute(p['token'],{'token':'synthetic-super-secret'})['state']=='stored'
    p=c.prepare('connectionVerify',args(studio,a),'2');out=c.execute(p['token']);assert out['actor']['id']=='app_scoped' and out['destination']=='ig1'
    assert out['grants']==[] and out['capabilities']['insights']=='unverified'
    serialized=json.dumps(C.overview(studio));assert 'synthetic-super-secret' not in serialized and 'MUST_NOT_PERSIST' not in serialized
    p=c.prepare('connectionLive',args(studio,a),'3');assert c.execute(p['token'])['live_enabled']
    assert not (studio.sp.root/'send.enabled').exists()

@pytest.mark.parametrize('body,kind',[({'id':'ig1','user_id':'other'},'destination_mismatch'),({'id':'ig1'},'malformed_identity'),({'data':[]},'ambiguous_identity')])
def test_instagram_namespace_fail_closed(studio,body,kind):
    a=account(studio)
    with pytest.raises(VerificationError) as e:verify(a,lambda f:'secret',reader=Reader(Transport(body)))
    assert e.value.detail['state']==kind

@pytest.mark.parametrize('status,body,kind',[(401,{},'authentication_failed'),(400,{'error':{'code':190,'error_subcode':463,'message':'secret https://secret/?token=secret'}},'expired_token'),(403,{},'permission_denied'),(429,{},'rate_limited')])
def test_provider_error_taxonomy_no_secret_or_urls(status,body,kind):
    with pytest.raises(VerificationError) as e:Reader(Transport((status,body))).request('GET','https://example.invalid',token='secret')
    assert e.value.detail['state']==kind and 'secret' not in str(e.value.detail)

def test_reader_no_redirect_and_timeout_budget():
    import requests
    http=Transport(requests.Timeout('secret URL'))
    with pytest.raises(VerificationError,match='network timeout'):Reader(http).request('GET','https://example.invalid',token='secret')
    assert http.calls[0][2]['allow_redirects'] is False and http.calls[0][2]['timeout'][0]==5

def test_youtube_explicit_exchange_and_renewal_binding(studio):
    a=account(studio,'youtube',{'channel_id':'channel1','client_id':'client1'},'youtube');vault=Vault();stored(studio,a,vault)
    rev=C.revision(studio.sp,a.id);binding=C.binding(load_account(studio.sp,a.id))
    http=Transport({'access_token':'short-lived-secret'}, {'items':[{'id':'channel1'}]}, {'aud':'client1','scope':'https://www.googleapis.com/auth/youtube.readonly','exp':'2000000000'})
    out=C.connect_session(studio,a.id,rev,keychain=vault,reader=Reader(http))
    assert out['state']=='session_ready';assert C.revision(studio.sp,a.id)==rev and C.binding(load_account(studio.sp,a.id))==binding
    assert [x[0] for x in http.calls]==['POST','GET','GET'];assert vault.get(a.secret_name('access_token'))=='short-lived-secret'
    assert 'short-lived-secret' not in json.dumps(C.overview(studio))

def test_bluesky_verify_cannot_exchange_and_connect_rejects_did_drift(studio):
    a=account(studio,'bluesky',{'did':'did:plc:one','handle':'one.test','pds':'https://bsky.social'},'bluesky');vault=Vault();stored(studio,a,vault);http=Transport()
    result=C.verify_connection(studio,a.id,C.revision(studio.sp,a.id),keychain=vault,reader=Reader(http));assert result['state']=='connect_session_required' and not http.calls
    result=C.connect_session(studio,a.id,C.revision(studio.sp,a.id),keychain=vault,reader=Reader(Transport({'did':'did:plc:other','accessJwt':'secret'})))
    assert result['state']=='principal_drift' and not vault.get(a.secret_name('access_token'))

def test_pinterest_shared_board_not_owned(studio):
    a=account(studio,'pinterest',{'user_id':'one','board_id':'board1'},'pinterest')
    out=verify(a,lambda f:'secret',reader=Reader(Transport({'id':'one','username':'alice'},{'id':'board1','owner':{'username':'bob'}})))
    assert out['board_owned'] is False and out['capabilities']['publishing']=='unverified'

def test_public_projection_never_constructs_vault_or_adapter(studio,monkeypatch):
    a=account(studio)
    monkeypatch.setattr(C,'Keychain',lambda:pytest.fail('vault was opened'))
    monkeypatch.setattr('ave.publish.safety.get_secret',lambda *a:pytest.fail('secret was read'))
    monkeypatch.setattr('ave.platforms.registry.adapter_for',lambda *a,**k:pytest.fail('publisher was constructed'))
    assert C.overview(studio)['accounts'][0]['verified'] is False


def test_host_settings_and_partial_keys_no_upload(studio):
    vault=Vault();vault.fail='secret_key';cfg={'endpoint':'https://storage.example.com','bucket':'sample-bucket','region':'auto','prefix':'studio/media'}
    out=C.save_host(studio,C.overview(studio)['host']['revision'],cfg,{'access_key':'synthetic-access','secret_key':'synthetic-secret'},keychain=vault)
    assert out['state']=='partial_update';assert C.load_workspace_host(studio.sp,keychain=vault) is None
    assert 'synthetic-secret' not in json.dumps(C.overview(studio))
    for url in ('http://storage.example.com','https://user:pw@storage.example.com','https://storage.example.com/?key=s','https://127.0.0.1'):
        with pytest.raises(ValueError):C.host_config({**cfg,'endpoint':url})

def test_service_serialized_scope_state_and_repair(studio,tmp_path):
    svc=service(studio,tmp_path);initial=svc.snapshot();svc.change('start',initial['revision'])
    plist=plistlib.loads(svc.adapter.path(identity(studio.sp)).read_bytes());assert plist['ProgramArguments'][0].endswith('AVE & Studio')
    assert plist['ProgramArguments'][-1]=='rehearsal';assert svc.snapshot()['state']=='waiting' and not svc.snapshot()['running']
    doc=C.read(record(studio.sp));C.atomic(heartbeat(studio.sp),{'service':identity(studio.sp),'executable_sha':doc['executable_sha'],'at':time.time(),'pid':222})
    svc.adapter.running=True;svc.adapter.pid=222;assert svc.snapshot()['state']=='running'
    C.atomic(heartbeat(studio.sp),{**C.read(heartbeat(studio.sp)),'at':time.time()-900});assert svc.snapshot()['state']=='stale'
    svc.change('stop',svc.snapshot()['revision']);assert svc.snapshot()['state']=='stopped'
    svc.exe.write_text('new version');assert svc.snapshot()['state']=='update_required'
    svc.change('repair',svc.snapshot()['revision']);assert C.read(record(studio.sp))['mode']=='rehearsal'

def test_service_refuses_foreign_job_and_failure_is_durable(studio,tmp_path):
    svc=service(studio,tmp_path);p=svc.adapter.path(identity(studio.sp));p.parent.mkdir();p.write_bytes(plistlib.dumps({'Label':identity(studio.sp),'ProgramArguments':['foreign']}))
    with pytest.raises(ValueError,match='unrecognized'):svc.change('start',svc.snapshot()['revision'])
    p.unlink();svc.adapter.fail=True
    with pytest.raises(ValueError,match='synthetic'):svc.change('start',svc.snapshot()['revision'])
    assert C.read(record(studio.sp))['state']=='failed'

def test_relocated_workspace_never_changes_old_service(studio,tmp_path):
    svc=service(studio,tmp_path);svc.change('start',svc.snapshot()['revision']);doc=C.read(record(studio.sp));doc['workspace']='/synthetic/old/workspace';C.atomic(record(studio.sp),doc)
    assert svc.snapshot()['state']=='foreign_loaded_job'
    with pytest.raises(ValueError,match='another workspace'):svc.change('repair',svc.snapshot()['revision'])

def test_scheduler_rehearsal_under_agent_and_send_refused(studio,monkeypatch):
    from ave.desktop_service import run_tick
    monkeypatch.setenv('AVE_AGENT','1')
    out=run_tick(studio.sp.root,'rehearsal');assert out['mode']=='dry-run'
    with pytest.raises(ValueError,match='Agent'):run_tick(studio.sp.root,'send')
    assert not (studio.sp.root/'send.enabled').exists()


def test_status_reader_reads_only_existing_container(studio):
    from ave.desktop_status import StatusReader
    a=account(studio);vault=Vault();vault.set(a.secret_name('token'),'secret');http=Transport({'status_code':'PUBLISHED'})
    reader=StatusReader(a,{},reader=Reader(http),keychain=vault)
    result=reader.recover({}, {'container':'container1'})
    assert result['receipt']['phase']=='uncertain';assert http.calls[0][0]=='GET'
    assert not hasattr(reader,'publish')

def test_native_http_refresh_stays_refused_after_auth(studio):
    from ave.studio.ui.api import StudioApp,StudioError
    auth=Authority();auth.active=True;app=StudioApp(studio,native_authority=auth)
    try:
        with pytest.raises(StudioError) as e:app.post('posts/p_sample/delivery-refresh',{'variant':'v1'})
        assert e.value.status==403
    finally:app.close()

@pytest.mark.parametrize('platform,product,public,responses,actor',[
 ('facebook','facebook_page',{'page_id':'page1'},[{'id':'page1'}],'page1'),
 ('threads','threads',{'threads_user_id':'thread1'},[{'id':'thread1'}],'thread1'),
 ('linkedin','linkedin_member',{'author':'urn:li:person:member1'},[{'sub':'member1'}],'urn:li:person:member1'),
 ('x','x_user',{'user_id':'x1'},[{'data':{'id':'x1'}}],'x1'),
 ('tiktok','tiktok_inbox',{'user_id':'tt1'},[{'data':{'user':{'open_id':'tt1'}},'error':{'code':'ok'}}],'tt1'),
 ('instagram','instagram_facebook',{'ig_user_id':'ig1','page_id':'page1'},[{'id':'person1'},{'data':[{'permission':'instagram_basic','status':'granted'},{'permission':'instagram_manage_comments','status':'declined'}]},{'data':[{'id':'page1','tasks':['PROFILE_PLUS_CREATE_CONTENT'],'instagram_business_account':{'id':'ig1'}}]},{'id':'ig1'}],'person1'),
 ('linkedin','linkedin_organization',{'author':'urn:li:organization:org1'},[{'sub':'member1'},{'elements':[{'roleAssignee':'urn:li:person:member1','organizationTarget':'urn:li:organization:org1','role':'ADMINISTRATOR','state':'APPROVED'}]}],'urn:li:person:member1'),
])
def test_fixed_product_reads(studio,platform,product,public,responses,actor):
    a=account(studio,platform,public,product);http=Transport(*responses);out=verify(a,lambda f:'secret',reader=Reader(http))
    assert out['actor']['id']==actor and out['state']=='identity_verified'
    assert all(c[0]=='GET' for c in http.calls)
    assert out['capabilities']=={'publishing':'unverified','insights':'unverified','comments':'unverified'}
    if product=='instagram_facebook':assert out['tasks']==['PROFILE_PLUS_CREATE_CONTENT'] and out['grants']==['instagram_basic']


def test_forged_verification_cannot_live_attest(studio,tmp_path):
    a=account(studio);vault=Vault();stored(studio,a,vault);a=load_account(studio.sp,a.id)
    path=C.record_path(studio.sp,a.id);doc=C.read(path);doc['evidence']={'state':'identity_verified'};C.atomic(path,doc)
    c,auth=coordinator(studio,tmp_path,keychain=vault);auth.active=True;p=c.prepare('connectionLive',args(studio,a),'1')
    with pytest.raises(ValueError,match='Verify'):c.execute(p['token'])


def test_connection_change_refuses_while_tick_holds_lock(studio):
    from ave.schedule.tick import tick_lock
    a=account(studio);vault=Vault()
    with tick_lock(studio.sp) as got:
        assert got
        with pytest.raises(ValueError,match='Background delivery'):stored(studio,a,vault)
    assert not vault.values


def test_request_relocation_expiry_and_unknown_arguments(studio,tmp_path):
    a=account(studio);c,auth=coordinator(studio,tmp_path,keychain=Vault());auth.active=True
    with pytest.raises(ValueError,match='fields'):c.prepare('connectionVerify',{**args(studio,a),'authorized':True},'1')
    p=c.prepare('connectionVerify',args(studio,a),'2');c.pending[p['token']]['at']-=181
    with pytest.raises(ValueError,match='expired'):c.execute(p['token'])
    p=c.prepare('connectionVerify',args(studio,a),'3');c.session.closed=True
    with pytest.raises(ValueError,match='Workspace'):c.execute(p['token'])


def test_sender_permission_and_service_are_distinct(studio,tmp_path):
    from ave.studio import sendgate
    c,auth=coordinator(studio,tmp_path,keychain=Vault());auth.active=True
    def run(action,rid):
        p=c.prepare(action,{'revision':c.service.snapshot()['revision']},rid);return c.execute(p['token'])
    run('serviceStart','1');assert not sendgate.path(studio.sp).exists()
    run('senderEnable','2');assert sendgate.verify(studio.sp).ok and C.read(record(studio.sp))['mode']=='send'
    run('serviceStop','3');assert sendgate.verify(studio.sp).ok
    run('senderDisable','4');assert not sendgate.verify(studio.sp).ok
    old=C.read(sendgate.path(studio.sp));assert old['enabled'] is False


def test_sender_enable_failure_compensates_off(studio,tmp_path):
    from ave.studio import sendgate
    c,auth=coordinator(studio,tmp_path);auth.active=True;c.service.adapter.fail=True
    p=c.prepare('senderEnable',{'revision':c.service.snapshot()['revision']},'1')
    with pytest.raises(ValueError,match='synthetic'):c.execute(p['token'])
    assert not sendgate.verify(studio.sp).ok


def test_configuration_save_backup_exact_bytes(tmp_path):
    from ave.desktop_tools import save_agent_config
    p=tmp_path/'host.json';p.write_text('original host settings');config={'mcpServers':{'ave-studio':{'command':'/Applications/AVE Studio.app/Contents/MacOS/AVE Studio','args':['--mcp','--studio','/tmp/synthetic']}}}
    out=save_agent_config(p,config);assert out['backup_created'] and not out['connected']
    assert json.loads(p.read_text())==config;assert next(tmp_path.glob('*.backup-*')).read_text()=='original host settings'


def test_normal_native_delivery_projection_no_secret_or_provider(studio,monkeypatch):
    from ave.studio import model,store,delivery
    from ave.studio.events import AGENT
    account(studio,'linkedin',{'author':'urn:li:person:one'},'linkedin_member')
    p=model.new_post(idea='Synthetic ordinary authoring',copy={'base':'Text'})
    v=model.new_variant(account='sample',platform='linkedin',post_type='text');v['delivery']={'method':'automatic'};p['variants']=[v]
    p=store.save_post(studio.sp,p,expected_rev=None,actor=AGENT,op='fixture')
    monkeypatch.setattr('ave.publish.safety.get_secret',lambda *a:pytest.fail('secret'))
    monkeypatch.setattr('ave.platforms.accounts.Account.secret',lambda *a:pytest.fail('account secret'))
    monkeypatch.setattr('ave.platforms.registry.adapter_for',lambda *a,**k:pytest.fail('adapter'))
    out=delivery.capabilities(studio,p['id'],v['id']);assert not out['credential_evidence']['checked']


def unresolved(studio,tmp_path):
    """Existing exact approved automatic operation; all authority is synthetic."""
    from sched_helpers import make_world
    from ave.studio import store,model,dispatch
    from ave.publish import queue as pq,receipts
    w=make_world(tmp_path/'outcome-world');pid,vid=w.add_post(at='2026-10-21T15:00:00Z');q=w.approve(pid,vid)
    p=store.load_post(w.sp,pid);v=model.find_variant(p,vid);v['delivery']={'method':'automatic'}
    # Existing helper's queue predates delivery choice: keep this solely an
    # operation fixture; resolution never uses it to publish or approves it.
    rec=receipts.normalize({'receipt':receipts.receipt(provider='instagram',account='ig-main',phase='uncertain',object_id='container1',object_kind='container')},q,'instagram')
    q['results']={'instagram':{**rec,'container':'container1'}};q['status']='uncertain';pq.save(w.sp,q)
    v['result']=q['results']['instagram'];p=store.save_post(w.sp,p,expected_rev=p['rev'],actor=__import__('ave.studio.events',fromlist=['AGENT']).AGENT,op='fixture')
    s=open_studio(w.sp.root,publishers={});a=load_account(w.sp,'ig-main')
    context={'args':{'post':pid,'variant':vid,'revision':p['rev']},'binding':C.binding(a),'operation':C.digest(q)}
    return s,context,q,w

@pytest.mark.parametrize('decision',['PUBLISHED','NOT PUBLISHED'])
def test_outcome_review_consumes_exactly_once_and_never_creates(studio,tmp_path,decision):
    from ave.studio import outcome_review as O,dispatch,seal,store
    from ave.publish import queue as pq
    s,context,q,w=unresolved(studio,tmp_path)
    out=O.record(s,context,{'outcome':decision,'posted_url':'https://www.instagram.com/p/synthetic' if decision=='PUBLISHED' else ''})
    assert out['source']=='human_report';assert w.pubs['ig-main'].publish_calls==0
    item=pq.load(s.sp,q['id']);assert item['results']['instagram']['receipt']['source']=='human_report'
    assert dispatch.consumption(s.sp,dispatch.approval_sig(item))==('published' if decision=='PUBLISHED' else 'review_required')
    intent=next(x for x in seal.log(s.sp,'control').rows() if x.get('op')=='outcome_review_intent')
    count=len(list(seal.log(s.sp,'dispatch').rows()))
    with pq._lock(s.sp,q['id']):O.finish(s.sp,intent)
    assert len(list(seal.log(s.sp,'dispatch').rows()))==count
    with pytest.raises(ValueError):O.record(s,context,{'outcome':decision,'posted_url':''})

@pytest.mark.parametrize('boundary',['queue','post','complete'])
def test_outcome_journal_recovers_postcommit_crashes(studio,tmp_path,monkeypatch,boundary):
    from ave.studio import outcome_review as O,seal,store,dispatch
    from ave.publish import queue as pq
    s,context,q,w=unresolved(studio,tmp_path)
    target=(pq,'save') if boundary=='queue' else (store,'save_post') if boundary=='post' else (seal.SealedLog,'append')
    original=getattr(*target);hit=[False]
    def fail(*a,**kw):
        out=original(*a,**kw)
        eligible=boundary!='complete' or isinstance(a[-1],dict) and a[-1].get('op')=='outcome_review_complete'
        if eligible and not hit[0]:hit[0]=True;raise RuntimeError('synthetic postcommit crash')
        return out
    monkeypatch.setattr(*target,fail)
    with pytest.raises(RuntimeError,match='postcommit'):O.record(s,context,{'outcome':'NOT PUBLISHED','posted_url':''})
    monkeypatch.setattr(*target,original)
    intent=next(x for x in seal.log(s.sp,'control').rows() if x.get('op')=='outcome_review_intent')
    with pq._lock(s.sp,q['id']):O.finish(s.sp,intent)
    assert dispatch.consumption(s.sp,dispatch.approval_sig(pq.load(s.sp,q['id'])))=='review_required'
    assert w.pubs['ig-main'].publish_calls==0


def test_native_message_boundary_rejects_remote_subframe_and_flags():
    from ave.desktop_native import native_message
    body={'id':'1','action':'connectionVerify','args':{'account':'sample','revision':'a'}};origin='http://127.0.0.1:1234/'
    assert native_message(body,origin,origin)[1]=='connectionVerify'
    for url,frame in [('https://example.com',True),(origin,False)]:
        with pytest.raises(ValueError):native_message(body,url,origin,frame)
    with pytest.raises(ValueError):native_message({**body,'authenticated':True},origin,origin)


def test_host_removal_native_context_disables_use(studio,tmp_path):
    vault=Vault();cfg={'endpoint':'https://storage.example.com','bucket':'sample-bucket','region':'auto','prefix':''}
    C.save_host(studio,C.overview(studio)['host']['revision'],cfg,{'access_key':'synthetic-ak','secret_key':'synthetic-sk'},keychain=vault)
    c,auth=coordinator(studio,tmp_path,keychain=vault);auth.active=True
    prepared=c.prepare('hostRemove',{'revision':C.overview(studio)['host']['revision']},'1')
    assert c.execute(prepared['token'])['state']=='removed'
    assert not vault.values and C.load_workspace_host(studio.sp,keychain=vault) is None


def test_failed_identity_verification_revokes_live_authority(studio):
    from ave.platforms.attest import write_live,verify_live
    a=account(studio);vault=Vault();stored(studio,a,vault);write_live(studio.sp.accounts_dir,a.id,'synthetic')
    C.verify_connection(studio,a.id,C.revision(studio.sp,a.id),keychain=vault,reader=Reader(Transport((401,{}))))
    assert not verify_live(studio.sp.accounts_dir,a.id).ok


def test_trusted_refresh_existing_operation_no_create(studio,tmp_path):
    from ave.publish import queue as pq
    s,ctx,q,w=unresolved(studio,tmp_path)
    # Bind this synthetic previously-authorized operation to the exact profile.
    q['approvals'][-1]['bind']['connection']=ctx['binding']
    from ave.publish import safety
    q['approvals'][-1]['bind_sig']=safety.sign_bind(q['approvals'][-1]['sig'],q['approvals'][-1]['bind']);pq.save(s.sp,q)
    vault=Vault();a=load_account(s.sp,'ig-main');vault.set(a.secret_name('token'),'synthetic')
    http=Transport({'status_code':'PUBLISHED'})
    c,auth=coordinator(s,tmp_path,keychain=vault,reader_factory=lambda:Reader(http));auth.active=True
    prepared=c.prepare('providerRefresh',ctx['args'],'1');out=c.execute(prepared['token'])
    assert out['state']=='sending' and w.pubs['ig-main'].publish_calls==0
    assert len(http.calls)==1 and http.calls[0][0]=='GET'
    assert pq.load(s.sp,q['id'])['results']['instagram']['receipt']['phase']=='uncertain'


@pytest.mark.parametrize('change',['manual','published','newer','destination'])
def test_native_outcome_rejects_changed_or_ineligible_operation(studio,tmp_path,change):
    from ave.studio import store,model
    from ave.publish import queue as pq
    s,ctx,q,w=unresolved(studio,tmp_path)
    c,auth=coordinator(s,tmp_path);auth.active=True
    if change=='manual':
        p=store.load_post(s.sp,ctx['args']['post']);model.find_variant(p,ctx['args']['variant'])['delivery']={'method':'manual'}
        p=store.save_post(s.sp,p,expected_rev=p['rev'],actor=__import__('ave.studio.events',fromlist=['AGENT']).AGENT,op='fixture');ctx['args']['revision']=p['rev']
    if change=='published':q['results']['instagram']['state']='published';pq.save(s.sp,q)
    prepared=c.prepare('reviewProviderOutcome',ctx['args'],'1')
    if change=='newer':q['caption']='newer operation';pq.save(s.sp,q)
    if change=='destination':
        a=load_account(s.sp,'ig-main');path=s.sp.accounts_dir/'ig-main.yaml';path.write_text(path.read_text().replace('ig-main','changed',1))
    with pytest.raises(ValueError):c.execute(prepared['token'],{'outcome':'NOT PUBLISHED','posted_url':''})
    assert w.pubs['ig-main'].publish_calls==0


def test_outcome_restart_recovers_saved_decision_through_native_action(studio,tmp_path,monkeypatch):
    from ave.studio import store,seal,dispatch
    from ave.publish import queue as pq
    s,context,q,w=unresolved(studio,tmp_path);original=store.save_post
    def crash(*a,**kw):
        original(*a,**kw);raise RuntimeError('synthetic process exit after post commit')
    c,auth=coordinator(s,tmp_path);auth.active=True;prepared=c.prepare('reviewProviderOutcome',context['args'],'1')
    monkeypatch.setattr(store,'save_post',crash)
    with pytest.raises(RuntimeError):c.execute(prepared['token'],{'outcome':'NOT PUBLISHED','posted_url':''})
    monkeypatch.setattr(store,'save_post',original)
    reopened=open_studio(s.sp.root,publishers={});c2,auth2=coordinator(reopened,tmp_path);auth2.active=True
    p=store.load_post(s.sp,context['args']['post']);request={**context['args'],'revision':p['rev']}
    prepared=c2.prepare('reviewProviderOutcome',request,'1');assert c2.execute(prepared['token'],{'outcome':'NOT PUBLISHED','posted_url':''})['source']=='human_report'
    assert dispatch.consumption(s.sp,dispatch.approval_sig(pq.load(s.sp,q['id'])))=='review_required'
    assert len([r for r in seal.log(s.sp,'control').rows() if r.get('op')=='outcome_review_intent'])==1
    assert w.pubs['ig-main'].publish_calls==0


def test_synthetic_send_crash_restart_never_second_primary(studio,tmp_path):
    from datetime import datetime,timezone
    from sched_helpers import make_world
    from studio_helpers import RecordingPublisher
    from ave.publish import queue as pq
    w=make_world(tmp_path/'restart-world',now=datetime.now(timezone.utc),behaviors=('crash_after_send',))
    pid,vid=w.add_post(at=w.clock.now());q=w.approve(pid,vid);w.go_live()
    with pytest.raises(KeyboardInterrupt):pq.run(w.sp,q['id'],send=True,publishers=w.pubs)
    assert w.pubs['ig-main'].publish_calls==1 and len(w.pubs['ig-main'].sent)==1
    restarted=RecordingPublisher('instagram',recover=None)
    for _ in range(2):
        out=pq.run(w.sp,q['id'],send=True,publishers={'ig-main':restarted})
        assert out['platforms']['instagram']['outcome']=='uncertain'
    assert restarted.publish_calls==0 and not restarted.http_calls


def test_journal_recovery_refuses_newer_payload_after_queue_commit(studio,tmp_path,monkeypatch):
    from ave.studio import outcome_review as O,seal
    from ave.publish import queue as pq
    s,context,q,w=unresolved(studio,tmp_path);original=pq.save
    def crash(*a,**kw):original(*a,**kw);raise RuntimeError('synthetic queue commit interruption')
    monkeypatch.setattr(pq,'save',crash)
    with pytest.raises(RuntimeError):O.record(s,context,{'outcome':'NOT PUBLISHED','posted_url':''})
    monkeypatch.setattr(pq,'save',original)
    item=pq.load(s.sp,q['id']);item['caption']='newer payload';pq.save(s.sp,item)
    intent=next(r for r in seal.log(s.sp,'control').rows() if r.get('op')=='outcome_review_intent')
    with pq._lock(s.sp,q['id']),pytest.raises(ValueError,match='newer operation'):O.finish(s.sp,intent)
    assert w.pubs['ig-main'].publish_calls==0
