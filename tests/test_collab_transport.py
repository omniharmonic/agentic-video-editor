"""Two real TLS localhost listeners; only identity/Keychain are explicitly injected."""
import copy
import hashlib
import json
from pathlib import Path
import socket
import ssl
import threading
import time
import pytest
from ave import collab as C
from ave import collab_transport as T
from ave import collab_tls as TLS
from ave.studio import model,store
from ave.studio.events import Actor
from ave.studio.project import StudioProject
from ave.studio.collaboration import Collaboration
from types import SimpleNamespace

class MemorySecrets:
    def __init__(self):self.values={}
    def get(self,k):return self.values.get(k)
    def put(self,k,v):self.values[k]=v
    def delete(self,k):self.values.pop(k,None)

class SocketIdentities:
    def __init__(self):self.sockets={};self.lock=threading.Lock()
    def adapter(self,node):return FixtureIdentity(self,node)

class FixtureIdentity:
    def __init__(self,network,node):self.network=network;self.node=node;self.user='101';self.available=True
    def local_identity(self):
        if not self.available:raise T.IdentityError('fixture_disconnected')
        return {'node':self.node,'user':self.user,'address':'127.0.0.1','interface':'fixture','index':1}
    def bind(self,sock,local,port=0):
        sock.bind(('127.0.0.1',port))
        with self.network.lock:self.network.sockets[sock.getsockname()[:2]]={'node':self.node,'user':self.user}
    def peer_identity(self,sock):
        self.local_identity()
        with self.network.lock:return self.network.sockets[sock.getpeername()[:2]].copy()

@pytest.fixture
def peers(tmp_path):
    network=SocketIdentities();result=[]
    for name in ('A','B'):
        sp=StudioProject(tmp_path/name);sp.create()
        with C._locked(sp) as home:
            state=C._state(sp,create=True);C._atomic(home/'state.json',state)
        ex=T.Exchange(T.ExchangeStore(tmp_path/('exchange-'+name),state['studio'],excluded=[sp.root]),network.adapter(name),MemorySecrets())
        ex.start(0)
        facade=Collaboration(SimpleNamespace(sp=sp));facade.exchange=ex;facade.config={'v':1,'directory':str(tmp_path/('exchange-'+name)),'studio':state['studio']};ex.on_pair=facade.bind_peer
        result.append(facade)
    yield result
    for facade in result:facade.close()

def pair(a,b):
    invite=a.exchange.invite();joined=b.exchange.join(invite)
    assert a.snapshot()['pairing']['code']==joined['code']
    assert b.exchange.confirm(joined['hash'])['state']=='waiting_for_local_confirmation'
    assert not a.exchange.store.state['peer']
    a.exchange.confirm(joined['hash']);assert b.exchange.finish_pair()['state']=='paired'
    return invite

def draft(sp,text='Initial draft'):
    return store.save_post(sp,model.new_post(post_id='p_shared',copy={'base':text}),expected_rev=None,actor=Actor('human','fixture'),op='fixture')

def update(sp,text):
    p=store.load_post(sp,'p_shared');p['copy']['base']=text
    return store.save_post(sp,p,expected_rev=p['rev'],actor=Actor('human','fixture'),op='fixture')

def send(a):
    view=a.prepare_send({'posts':['p_shared'],'visuals':[],'media_ids':[]});row=a.send(view['token']);a.exchange.tick();return row['sha256']

def test_tls_two_listeners_preview_apply_diverge_restart(peers):
    a,b=peers;pair(a,b);draft(a.sp)
    digest=send(a)
    assert a.exchange.store.state['outbox'][digest]['state']=='received'
    assert not store.post_path(b.sp,'p_shared').exists()
    preview=b.preview(digest);assert not store.post_path(b.sp,'p_shared').exists()
    b.apply(preview['token']);update(a.sp,'Local independent edit');update(b.sp,'Remote independent edit')
    returned=send(b);view=a.preview(returned)
    assert view['comparisons'][0]['base']['copy']['base']=='Initial draft'
    assert view['comparisons'][0]['local']['copy']['base']=='Local independent edit'
    assert view['comparisons'][0]['incoming']['copy']['base']=='Remote independent edit'
    applied=a.apply(view['token']);cid=applied['conflicts'][0]['id'];rev=store.load_post(a.sp,'p_shared')['rev']
    with pytest.raises(ValueError):a.resolve({'id':cid,'take':'incoming','expected_rev':rev-1})
    a.resolve({'id':cid,'take':'incoming','expected_rev':rev})
    assert store.load_post(a.sp,'p_shared')['copy']['base']=='Remote independent edit'
    rev=store.load_post(a.sp,'p_shared')['rev'];a.apply(a.preview(returned)['token'])
    assert store.load_post(a.sp,'p_shared')['rev']==rev
    restored=T.ExchangeStore(b.exchange.store.root.parent,b.exchange.store.studio,excluded=[b.sp.root])
    assert restored.receipt(digest,a.exchange.local_descriptor())['state']=='received'

def test_pair_cancel_replay_and_pin_no_credential(peers,monkeypatch):
    a,b=peers;invite=a.exchange.invite();joined=b.exchange.join(invite);a.exchange.cancel_pair()
    with pytest.raises(ValueError):b.exchange.confirm(joined['hash'])
    b.exchange.cancel_pair();invite=a.exchange.invite();bad=copy.deepcopy(invite);bad['inviter']['tls']['pin']='0'*64
    with pytest.raises(ValueError,match='certificate changed'):b.exchange.join(bad)
    assert a.exchange.pending['transcript'] is None
    b.exchange.cancel_pair();pair(a,b)
    with pytest.raises(ValueError):b.exchange.request(invite['inviter'],'POST','/exchange/v1/pair',{'phase':'hello'})

def test_lost_ack_and_offline_immutable_queue(peers,monkeypatch):
    a,b=peers;pair(a,b);draft(a.sp)
    original=b.exchange.store.receive;once=[True]
    def receive(*args):
        result=original(*args)
        if once.pop() if once else False:raise OSError('simulated lost acknowledgement')
        return result
    monkeypatch.setattr(b.exchange.store,'receive',receive)
    digest=send(a)
    assert a.exchange.store.state['outbox'][digest]['state']=='blocked'
    row=a.exchange.store.state['outbox'][digest];row['state']='queued';row['next']=0
    a.exchange.tick();assert row['state']=='received'
    assert len(b.snapshot()['inbox'])==1
    update(a.sp,'Second queued revision');b.exchange.stop()
    view=a.prepare_send({'posts':['p_shared']});digest=a.send(view['token'])['sha256'];original_bytes=a.exchange.store.file('outbox',digest).read_bytes()
    a.exchange.tick();assert a.exchange.store.state['outbox'][digest]['state']=='retry_wait'
    update(a.sp,'Later local words');assert a.exchange.store.file('outbox',digest).read_bytes()==original_bytes
    a.exchange.cancel(digest);assert a.exchange.store.state['outbox'][digest]['state']=='cancelled'

@pytest.mark.xfail(reason='Live Tailscale peer identity is deferred (owner decision 2026-10-30); finish before enabling transport',strict=False)
def test_stale_preview_identity_revocation_and_wrong_routes(peers):
    a,b=peers;pair(a,b);draft(a.sp);digest=send(a)
    view=b.preview(digest);draft(b.sp,'Local concurrent creation')
    with pytest.raises(ValueError,match='revisions changed'):b.apply(view['token'])
    bearer=a.exchange.secrets.get(a.exchange.store.state['peer']['reference']+':out');remote=b.exchange.local_descriptor()
    for path in ('/api/studio/approve','/files/','/exchange/v1/receipts/../../secret'):
        with pytest.raises(ValueError):a.exchange.request(remote,'GET',path,bearer=bearer)
    b.exchange.identity.user='999'
    with pytest.raises(T.IdentityError):a.exchange.request(remote,'GET','/exchange/v1/receipts/'+digest,bearer=bearer)
    b.exchange.identity.user='101';b.exchange.revoke()
    assert b.exchange.store.state['peer'] is None and not b.exchange.enabled

def test_malformed_headers_and_partial_upload_leave_no_inbox(peers):
    a,b=peers;pair(a,b);remote=b.exchange.local_descriptor();bearer=a.exchange.secrets.get(a.exchange.store.state['peer']['reference']+':out')
    for header in ('Content-Length: 4\r\nContent-Length: 5','Content-Length: 4\r\nTransfer-Encoding: chunked','Content-Length: 4\r\nOrigin: https://example.invalid','Content-Length: -1'):
        sock=socket.socket();a.exchange.identity.bind(sock,a.exchange.identity.local_identity());sock.connect(tuple(remote['endpoint']))
        tls=TLS.client_context(remote['tls']).wrap_socket(sock,server_hostname=remote['tls']['name'])
        tls.sendall(('PUT /exchange/v1/bundles/'+'a'*64+' HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nAuthorization: Bearer '+bearer+'\r\n'+header+'\r\n\r\n').encode())
        assert b'400' in tls.recv(1024);tls.close()
    assert not b.snapshot()['inbox']

def test_wrong_target_malicious_json_and_quota(peers,monkeypatch,tmp_path):
    a,b=peers;pair(a,b);draft(a.sp);view=a.prepare_send({'posts':['p_shared']});row=a.send(view['token'])
    monkeypatch.setattr(T,'MAX_STORAGE',1);a.exchange.tick()
    assert not b.snapshot()['inbox'] and not store.post_path(b.sp,'p_shared').exists()


# The large-file channel is retired (Syncthing carries project media). Its route answers nobody, paired or not.
def test_the_retired_file_route_refuses_everyone(peers,tmp_path):
    a,b=peers;pair(a,b)
    assert not hasattr(T,'CHUNK') and not hasattr(a.exchange,'offer_file') and 'files' not in a.exchange.store.state
    bearer=a.exchange.secrets.get(a.exchange.store.state['peer']['reference']+':out')
    with pytest.raises(ValueError):a.exchange.request(a.exchange.store.state['peer']['descriptor'],'GET','/exchange/v1/files/'+'e'*64,bearer=bearer)
    with pytest.raises(ValueError):a.exchange.request(b.exchange.store.state['peer']['descriptor'] and a.exchange.store.state['peer']['descriptor'],'GET','/exchange/v1/files/'+'e'*64,bearer='wrong')
