"""Focused Task6 review regressions. Synthetic authority, DNS, TLS and jobs only."""
import json
import socket
import ssl
from pathlib import Path
from types import SimpleNamespace
import pytest
from test_connections_service import (studio, isolate_synthetic_redaction_registry,
    account, stored, coordinator, args, service, Vault, Transport)
from ave.platforms.connection_verify import Reader,verify,VerificationError
from ave.studio import connections as C


def dns(address):
    v6=':' in address
    return (socket.AF_INET6 if v6 else socket.AF_INET,socket.SOCK_STREAM,6,'',(address,443,0,0) if v6 else (address,443))

@pytest.mark.parametrize('address',['127.0.0.1','10.0.0.1','169.254.169.254','100.100.100.200','::1','fe80::1','fc00::1','::ffff:127.0.0.1','224.0.0.1'])
def test_public_transport_rejects_entire_mixed_dns_set_before_socket(monkeypatch,address):
    from ave.platforms.public_network import PublicHTTPSConnection
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[dns('8.8.8.8'),dns(address)])
    monkeypatch.setattr(socket,'socket',lambda *a,**k:pytest.fail('socket constructed before validating every address'))
    with pytest.raises(ValueError,match='public DNS'):PublicHTTPSConnection('pds.example.test',443,timeout=1)._new_conn()


def test_checked_socket_no_dns_redial_and_tls_hostname_preserved(monkeypatch):
    from ave.platforms.public_network import PublicHTTPSConnection,session
    import urllib3.connection
    resolutions=[];connections=[];tls=[]
    def resolve(*a,**k):resolutions.append(a);return [dns('8.8.8.8')] if len(resolutions)==1 else [dns('127.0.0.1')]
    class Sock:
        def settimeout(self,*a):pass
        def setsockopt(self,*a):pass
        def connect(self,address):connections.append(address)
        def close(self):pass
    monkeypatch.setattr(socket,'getaddrinfo',resolve);monkeypatch.setattr(socket,'socket',lambda *a,**k:Sock())
    def wrap(**kw):tls.append(kw);return kw['sock'],True
    monkeypatch.setattr(urllib3.connection,'_ssl_wrap_socket_and_match_hostname',wrap)
    c=PublicHTTPSConnection('pds.example.test',443,timeout=1,cert_reqs=ssl.CERT_REQUIRED);c.connect()
    assert len(resolutions)==1 and connections==[('8.8.8.8',443)]
    assert tls[0]['server_hostname']=='pds.example.test' and tls[0]['cert_reqs']==ssl.CERT_REQUIRED and tls[0]['assert_hostname'] is not False
    with pytest.raises(ValueError):PublicHTTPSConnection('pds.example.test',443,timeout=1)._new_conn()
    assert len(connections)==1 and len(resolutions)==2
    transport=session();assert transport.trust_env is False and transport.verify is True
    adapter=transport.get_adapter('https://pds.example.test');assert adapter.max_retries.total==0
    with pytest.raises(ValueError,match='proxies'):adapter.proxy_manager_for('http://proxy.example.test')
    transport.close()


def test_tls_verification_failure_propagates_without_request_body(monkeypatch):
    from ave.platforms.public_network import PublicHTTPSConnection
    import urllib3.connection
    sock=SimpleNamespace(settimeout=lambda *a:None,setsockopt=lambda *a:None,connect=lambda *a:None,close=lambda:None)
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[dns('8.8.8.8')]);monkeypatch.setattr(socket,'socket',lambda *a,**k:sock)
    def refused(**kw):raise ssl.SSLCertVerificationError('synthetic hostname mismatch')
    monkeypatch.setattr(urllib3.connection,'_ssl_wrap_socket_and_match_hostname',refused)
    with pytest.raises(ssl.SSLCertVerificationError):PublicHTTPSConnection('pds.example.test',443,timeout=1).connect()


def meta_pages(*,cursor='second'):
    return {'data':[{'id':'other'}],'paging':{'next':'https://attacker.invalid/token?secret=never-follow','cursors':{'after':cursor}}}


def test_meta_selected_destination_on_later_reconstructed_page(studio):
    a=account(studio,'instagram',{'ig_user_id':'ig1','page_id':'page1'},'instagram_facebook')
    http=Transport({'id':'person1'},{'data':[]},meta_pages(),{'data':[{'id':'page1','tasks':['CREATE_CONTENT'],'instagram_business_account':{'id':'ig1'}}]},{'id':'ig1'})
    out=verify(a,lambda f:'fixture-token-long',reader=Reader(http));assert out['state']=='identity_verified'
    assert http.calls[3][1]=='https://graph.facebook.com/v26.0/me/accounts' and http.calls[3][2]['params']['after']=='second'
    assert all('attacker' not in c[1] for c in http.calls)

@pytest.mark.parametrize('case',['missing_cursor','loop','budget','complete'])
def test_incomplete_listing_preserves_authority_but_conclusive_absence_revokes(studio,case):
    from ave.platforms.attest import write_live,verify_live
    a=account(studio,'instagram',{'ig_user_id':'ig1','page_id':'page1'},'instagram_facebook');vault=Vault();stored(studio,a,vault)
    write_live(studio.sp.accounts_dir,a.id,'synthetic reviewer')
    pages=([meta_pages(cursor=None)] if case=='missing_cursor' else [meta_pages(),meta_pages()] if case=='loop' else [meta_pages(cursor=str(i)) for i in range(4)] if case=='budget' else [{'data':[]}])
    http=Transport({'id':'person1'},{'data':[]},*pages)
    out=C.verify_connection(studio,a.id,C.revision(studio.sp,a.id),keychain=vault,reader=Reader(http))
    assert out['state']==('destination_not_managed' if case=='complete' else 'destination_listing_incomplete')
    assert verify_live(studio.sp.accounts_dir,a.id).ok is (case!='complete')
    assert len(http.calls)<=6


def test_linkedin_reconstructed_pagination_and_incomplete_budget(studio):
    a=account(studio,'linkedin',{'author':'urn:li:organization:org1'},'linkedin_organization')
    page={'elements':[],'paging':{'start':0,'count':10,'links':[{'rel':'next','href':'https://attacker.invalid'}]}}
    match={'roleAssignee':'urn:li:person:member1','organizationTarget':'urn:li:organization:org1','role':'ADMINISTRATOR','state':'APPROVED'}
    http=Transport({'sub':'member1'},page,{'elements':[match]})
    assert verify(a,lambda f:'fixture-token-long',reader=Reader(http))['roles']==['ADMINISTRATOR']
    assert http.calls[2][1]=='https://api.linkedin.com/rest/organizationAcls' and http.calls[2][2]['params']['start']==10
    http=Transport({'sub':'member1'},*[{'elements':[],'paging':{'start':i*10,'count':10,'links':[{'rel':'next','href':'untrusted'}]}} for i in range(4)])
    with pytest.raises(VerificationError) as e:verify(a,lambda f:'fixture-token-long',reader=Reader(http))
    assert e.value.detail['state']=='role_listing_incomplete' and len(http.calls)==5


def test_public_evidence_trust_requires_exact_session_record_and_no_secret_reads(studio,monkeypatch):
    from ave.studio.service import open_studio
    a=account(studio);vault=Vault();stored(studio,a,vault)
    C.verify_connection(studio,a.id,C.revision(studio.sp,a.id),keychain=vault,reader=Reader(Transport({'id':'app1','user_id':'ig1'})))
    assert C.overview(studio)['accounts'][0]['verified']
    monkeypatch.setattr('ave.publish.safety.approval_key',lambda:pytest.fail('ordinary overview opened signing key'))
    monkeypatch.setattr(C,'Keychain',lambda:pytest.fail('ordinary overview opened Keychain'))
    assert C.overview(studio)['accounts'][0]['verified']
    reopened=open_studio(studio.sp.root,publishers={});row=C.overview(reopened)['accounts'][0]
    assert not row['verified'] and row['evidence']['state']=='recorded_unvalidated'
    path=C.record_path(studio.sp,a.id);path.write_text(path.read_text()+'\n')
    assert not C.overview(studio)['accounts'][0]['verified']
    doc=C.read(path);doc['evidence']={'state':'identity_verified','actor':{'id':'forged'}};C.atomic(path,doc)
    row=C.overview(studio)['accounts'][0];assert not row['verified'] and 'forged' not in json.dumps(row['evidence'])

@pytest.mark.parametrize('operation',['start','stop','remove','repair'])
@pytest.mark.parametrize('saved',['none','foreign','ambiguous'])
def test_loaded_job_missing_plist_requires_exact_ownership(studio,tmp_path,operation,saved):
    svc=service(studio,tmp_path)
    if saved!='none':
        svc.change('start',svc.snapshot()['revision']);svc.adapter.path(svc.snapshot()['label']).unlink();svc.adapter.calls.clear()
    svc.adapter.loaded=True;svc.adapter.arguments=None if saved in ('none','ambiguous') else ['/foreign','--scheduler','--studio','/other','--mode','send']
    before=C.read(__import__('ave.desktop_service',fromlist=['record']).record(studio.sp))
    with pytest.raises(ValueError,match='ownership'):svc.change(operation,svc.snapshot()['revision'])
    assert not svc.adapter.calls and svc.adapter.loaded
    assert C.read(__import__('ave.desktop_service',fromlist=['record']).record(studio.sp))==before

@pytest.mark.parametrize('operation',['start','stop','remove','repair'])
def test_loaded_owned_job_missing_plist_remains_repairable(studio,tmp_path,operation):
    svc=service(studio,tmp_path);svc.change('start',svc.snapshot()['revision']);svc.adapter.path(svc.snapshot()['label']).unlink()
    svc.change(operation,svc.snapshot()['revision'])
    assert svc.adapter.loaded is (operation in ('start','repair'))


def test_launchctl_loaded_program_and_argument_parser_and_changed_owner(monkeypatch,tmp_path):
    from ave.desktop_service import Launchd
    manager=Launchd(tmp_path);calls=[]
    output='fixture = {\n\tprogram = /Applications/AVE Studio.app/Contents/MacOS/AVE Studio\n\targuments = {\n\t\t/Applications/AVE Studio.app/Contents/MacOS/AVE Studio\n\t\t--scheduler\n\t}\n\tpid = 42\n}'
    def call(*args):calls.append(args);return SimpleNamespace(returncode=0,stdout=output.encode())
    monkeypatch.setattr(manager,'call',call);state=manager.state('fixture')
    assert state['program']==state['arguments'][0] and state['arguments'][1]=='--scheduler'
    with pytest.raises(ValueError,match='ownership'):manager.unload('fixture',expected=['/different'])
    assert all(c[0]=='print' for c in calls)


def test_queue_only_crash_reopen_native_ui_exact_operation(studio,tmp_path):
    from datetime import datetime,timezone
    import threading
    from sched_helpers import make_world
    from ave.publish import queue as pq
    from ave.studio.service import open_studio
    from ave.studio.ui.server import StudioServer
    from ave.studio import store,dispatch
    from playwright.sync_api import sync_playwright
    w=make_world(tmp_path/'queue-only',now=datetime.now(timezone.utc),behaviors=('crash_after_send',))
    pid,vid=w.add_post(at=w.clock.now());q=w.approve(pid,vid);w.go_live()
    with pytest.raises(KeyboardInterrupt):pq.run(w.sp,q['id'],send=True,publishers=w.pubs)
    assert w.variant(pid,vid).get('result') is None and w.pubs['ig-main'].publish_calls==1
    reopened=open_studio(w.sp.root,publishers={});reopened.native_authoring=True
    stored_before=store.load_post(w.sp,pid)
    projected=reopened.post(pid)['variants'][0]
    assert projected['result'] is None and projected['delivery_operation']['recoverable']
    assert projected['delivery_operation']['digest']==C.digest(pq.load(w.sp,q['id']))
    c,authority=coordinator(reopened,tmp_path);authority.active=True
    seen=[]
    def native(action,arguments):
        assert action=='reviewProviderOutcome'
        assert arguments['operation']==projected['delivery_operation']['digest']
        prepared=c.prepare(action,arguments,str(len(seen)));seen.append({'action':action,'arguments':arguments,'detail':prepared['detail']})
        return c.execute(prepared['token'],{'outcome':'NOT PUBLISHED','posted_url':''})
    server=StudioServer(reopened);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    output=Path('/private/tmp/ave-connections-fix1-queue-only-ui');output.mkdir(exist_ok=True)
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch();page=browser.new_page(viewport={'width':1024,'height':900});page.set_default_timeout(10000)
            errors=[];external=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.route('**/*',lambda route:route.continue_() if route.request.url.startswith(f'http://127.0.0.1:{server.port}/') else (external.append(route.request.url),route.abort()))
            page.expose_function('fixtureNative',native)
            page.add_init_script('window.aveNative={connectionAction:(a,b)=>window.fixtureNative(a,b)}')
            page.goto(server.url()+f'#/post/{pid}')
            page.get_by_role('button',name='Review publication outcome',exact=True).wait_for()
            assert page.get_by_role('button',name='Refresh provider status',exact=True).is_enabled()
            assert page.locator('.ed-form textarea').first.is_disabled()
            caption=page.locator(f'textarea[data-path="variants.{vid}.copy.caption"]')
            before_ui={'caption':caption.input_value(),'shared':page.locator('textarea[data-path="copy.base"]').input_value(),'selected':page.get_by_role('tab',selected=True).get_attribute('data-variant')}
            assert before_ui['caption']==stored_before['copy']['base'] and before_ui['selected']==vid
            assert 'ave studio resolve' not in page.locator('.ed-approval').inner_text()
            page.screenshot(path=str(output/'queue-only-reopened.png'),full_page=True)
            page.get_by_role('button',name='Review publication outcome',exact=True).click()
            page.get_by_text('Fresh human review and approval',exact=False).first.wait_for()
            after_ui={'caption':caption.input_value(),'shared':page.locator('textarea[data-path="copy.base"]').input_value(),'selected':page.get_by_role('tab',selected=True).get_attribute('data-variant')}
            assert after_ui==before_ui
            assert page.locator('.ed-lock-notice').count()==0 and caption.is_enabled()
            assert 'Provider rejected' not in page.locator('.composer-delivery').inner_text()
            assert 'Publication reported by a person.' not in page.locator('.composer-delivery').inner_text()
            assert 'Not published · reported by a person' in page.locator('.ed-approval').inner_text()
            page.screenshot(path=str(output/'queue-only-reported.png'),full_page=True)
            assert len(seen)==1 and not errors and not external
            browser.close()
        item=pq.load(w.sp,q['id']);assert item['results']['instagram']['receipt']['source']=='human_report'
        assert dispatch.consumption(w.sp,dispatch.approval_sig(item))=='review_required'
        assert w.pubs['ig-main'].publish_calls==1 and not w.pubs['ig-main'].http_calls
        stored_after=store.load_post(w.sp,pid)
        assert stored_after['variants'][0]['result']['outcome_review_id']
        assert stored_after['copy']==stored_before['copy'] and stored_after['variants'][0]['copy']==stored_before['variants'][0]['copy']
        (output/'verification.json').write_text(json.dumps({'ok':True,'actual_auth':False,'real_sends':False,'primary_fake_creates':1,'second_primary_creates':0,'queue_only_before':True,'requests':seen,'before_ui':before_ui,'after_ui':after_ui,'stored_copy_unchanged':True,'errors':errors,'external_requests':external},indent=2))
    finally:server.shutdown();server.server_close();thread.join(5)


def test_default_reader_blocks_private_dns_before_sending_credentials(monkeypatch):
    try: import requests  # noqa: F401  urllib3 probes IPv6 with a socket on first import; import before blocking sockets
    except ImportError: pass
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[dns('169.254.169.254')])
    monkeypatch.setattr(socket,'socket',lambda *a,**k:pytest.fail('private socket dial'))
    with pytest.raises(VerificationError) as e:
        Reader().request('POST','https://pds.example.test/xrpc/com.atproto.server.createSession',json_body={'identifier':'person.test','password':'fixture-password-never-sent'})
    assert e.value.detail['state']=='network_or_response_failure' and 'password' not in str(e.value.detail)


def test_replaced_credentials_invalidate_cached_evidence_even_if_files_rolled_back(studio):
    a=account(studio);vault=Vault();stored(studio,a,vault)
    C.verify_connection(studio,a.id,C.revision(studio.sp,a.id),keychain=vault,reader=Reader(Transport({'id':'app1','user_id':'ig1'})))
    path=C.record_path(studio.sp,a.id);cfg=studio.sp.accounts_dir/'sample.yaml';old_record=path.read_bytes();old_config=cfg.read_bytes()
    assert C.overview(studio)['accounts'][0]['verified']
    stored(studio,a,vault);path.write_bytes(old_record);cfg.write_bytes(old_config)
    assert not C.overview(studio)['accounts'][0]['verified']


def test_loaded_ownership_is_rechecked_at_unload_before_bootout(studio,tmp_path,monkeypatch):
    from ave.desktop_service import Launchd,record
    svc=service(studio,tmp_path);svc.change('start',svc.snapshot()['revision'])
    doc=C.read(record(studio.sp));adapter=Launchd(svc.adapter.root);svc.adapter=adapter;calls=[]
    def call(*args):
        calls.append(args);assert args[0]=='print','foreign job must not reach bootout'
        argv=doc['arguments'] if len(calls)==1 else ['/foreign','--scheduler']
        output='fixture = {\n\tprogram = '+argv[0]+'\n\targuments = {\n'+''.join('\t\t'+x+'\n' for x in argv)+'\t}\n}'
        return SimpleNamespace(returncode=0,stdout=output.encode())
    monkeypatch.setattr(adapter,'call',call)
    with pytest.raises(ValueError,match='ownership'):svc.change('stop',C.digest(doc))
    assert len(calls)==2 and C.read(record(studio.sp))==doc


@pytest.mark.parametrize('operation',['start','stop','remove','repair'])
def test_foreign_program_override_in_existing_plist_refused(studio,tmp_path,operation):
    import plistlib
    svc=service(studio,tmp_path);svc.change('start',svc.snapshot()['revision']);path=svc.adapter.path(svc.snapshot()['label'])
    doc=plistlib.loads(path.read_bytes());doc['Program']='/foreign';path.write_bytes(plistlib.dumps(doc));svc.adapter.calls.clear()
    with pytest.raises(ValueError,match='unrecognized'):svc.change(operation,svc.snapshot()['revision'])
    assert not svc.adapter.calls and svc.adapter.loaded
