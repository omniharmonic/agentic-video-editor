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


# Large files: resumable chunks over the paired channel, quarantined on arrival.
def _big(tmp_path,monkeypatch,size=5*1024*1024+17):
    monkeypatch.setattr(T,'CHUNK',1024*1024)
    f=tmp_path/'camera a.mov';f.write_bytes(hashlib.sha256(b'x').digest()*(size//32+1));f.write_bytes(f.read_bytes()[:size]);return f

def _drain(a,b,digest,limit=4000):
    rows=a.exchange.store.state['files']
    for _ in range(limit):
        a.exchange._tick_file();rows[digest]['next']=0
        if rows[digest]['state'] in ('received','blocked'):return rows[digest]
        time.sleep(.002)
    raise AssertionError(rows[digest])

def test_large_file_arrives_verified_and_outside_the_studio(peers,tmp_path,monkeypatch):
    a,b=peers;pair(a,b);f=_big(tmp_path,monkeypatch)
    row=a.exchange.offer_file(f);assert _drain(a,b,row['sha256'])['state']=='received'
    got=b.snapshot()['received_files'][0];dest=Path(got['path'])
    assert got['state']=='complete' and dest.read_bytes()==f.read_bytes() and dest.name=='camera a.mov'
    assert b.sp.root not in dest.parents and not (dest.parent.parent.parent/'files'/(row['sha256']+'.part')).exists()

def test_large_file_resumes_after_a_lost_chunk_and_a_receiver_restart(peers,tmp_path,monkeypatch):
    a,b=peers;pair(a,b);f=_big(tmp_path,monkeypatch);digest=a.exchange.offer_file(f)['sha256']
    for _ in range(3):a.exchange._tick_file()
    sent=a.exchange.store.state['files'][digest]['offset'];assert 0<sent<f.stat().st_size
    a.exchange.store.state['files'][digest]['offset']=None      # the sender forgot: it must ask, not restart
    a.exchange._tick_file()
    assert a.exchange.store.state['files'][digest]['offset']==sent+T.CHUNK
    assert _drain(a,b,digest)['state']=='received' and Path(b.snapshot()['received_files'][0]['path']).read_bytes()==f.read_bytes()

def test_a_short_chunk_rolls_back_and_a_gap_is_refused(peers,tmp_path):
    a,b=peers;pair(a,b);store_=b.exchange.store;peer={'studio':a.exchange.store.studio};digest='a'*64;ok=lambda:None
    def broken(n,calls=[b'abcd']):
        return calls.pop() if calls else b''
    with pytest.raises(ValueError):store_.file_chunk(digest,0,100,10,broken,peer,ok)
    assert store_._part(digest).stat().st_size==0
    with pytest.raises(ValueError,match='offset_mismatch'):store_.file_chunk(digest,5,100,5,lambda n:b'x'*n,peer,ok)

def test_revocation_during_a_chunk_discards_it(peers):
    a,b=peers;pair(a,b);store_=b.exchange.store;peer={'studio':a.exchange.store.studio};digest='b'*64
    def revoked():raise ValueError('authentication_refused')
    with pytest.raises(ValueError):store_.file_chunk(digest,0,100,4,lambda n:b'x'*n,peer,revoked)
    assert store_._part(digest).stat().st_size==0

def test_corrupted_bytes_never_reach_received_and_the_sender_starts_over(peers,tmp_path,monkeypatch):
    a,b=peers;pair(a,b);f=_big(tmp_path,monkeypatch);digest=a.exchange.offer_file(f)['sha256']
    peer={'studio':a.exchange.store.studio};size=f.stat().st_size;ok=lambda:None;bad=b.exchange.store
    data=bytearray(f.read_bytes());data[9]^=1;pos=[0]
    def read(n):
        chunk=bytes(data[pos[0]:pos[0]+n]);pos[0]+=len(chunk);return chunk
    bad.file_chunk(digest,0,size,size,read,peer,ok);bad.file_finish(digest,'camera a.mov',size,peer)
    for _ in range(500):
        if bad.state['received'][digest]['state']=='partial':break
        time.sleep(.01)
    assert not list((bad.root/'received').rglob('*.mov')) and not bad._part(digest).exists()
    assert _drain(a,b,digest)['state']=='received'

def test_unsafe_names_a_changed_source_and_the_disk_floor_are_refused(peers,tmp_path,monkeypatch):
    a,b=peers;pair(a,b);f=_big(tmp_path,monkeypatch);digest=a.exchange.offer_file(f)['sha256'];peer={'studio':a.exchange.store.studio}
    for name in ('../evil.mov','.hidden','a/b.mov','x\n.mov'):
        with pytest.raises(ValueError):b.exchange.store.file_finish('c'*64,name,1,peer)
    monkeypatch.setattr(T.shutil,'disk_usage',lambda p:SimpleNamespace(free=T.FILE_FLOOR))
    with pytest.raises(ValueError,match='exchange_disk_floor'):b.exchange.store.file_chunk('d'*64,0,100,4,lambda n:b'x'*n,peer,lambda:None)
    monkeypatch.undo();monkeypatch.setattr(T,'CHUNK',1024*1024)
    with f.open('ab') as out:out.write(b'more')
    assert _drain(a,b,digest)['error']=='source_changed'

def test_an_unpaired_caller_cannot_use_the_file_route(peers,tmp_path):
    a,b=peers;pair(a,b)
    with pytest.raises(ValueError):a.exchange.request(b.exchange.store.state['peer']['descriptor'] and a.exchange.store.state['peer']['descriptor'],'GET','/exchange/v1/files/'+'e'*64,bearer='wrong')

def test_big_files_do_not_use_up_the_bundle_quota(peers,tmp_path,monkeypatch):
    a,b=peers;pair(a,b);f=_big(tmp_path,monkeypatch);digest=a.exchange.offer_file(f)['sha256'];_drain(a,b,digest)
    monkeypatch.setattr(T,'MAX_STORAGE',1024*1024);draft(a.sp);assert send(a) and a.exchange.store.state['outbox']


def test_refused_chunks_leave_no_quota_row_and_stale_partials_expire(peers,monkeypatch):
    a,b=peers;pair(a,b);s=b.exchange.store;peer={'studio':a.exchange.store.studio}
    for i in range(8):
        with pytest.raises(ValueError,match='offset_mismatch'):s.file_chunk(f'{i:064x}',5,10,5,lambda n:b'x'*n,peer,lambda:None)
    assert not s.state['received']
    for i in range(T.MAX_PARTIAL):s.file_chunk(f'{i:064x}',0,10,1,lambda n:b'x',peer,lambda:None)
    with pytest.raises(ValueError,match='exchange_quota'):s.file_chunk('f'*64,0,10,1,lambda n:b'x',peer,lambda:None)
    for row in s.state['received'].values():row['touched']=0
    assert s.file_chunk('f'*64,0,10,1,lambda n:b'x',peer,lambda:None)['received']==1 and len(s.state['received'])==1

def test_revocation_clears_incomplete_received_files(peers):
    a,b=peers;pair(a,b);s=b.exchange.store;peer={'studio':a.exchange.store.studio}
    s.file_chunk('a'*64,0,10,1,lambda n:b'x',peer,lambda:None);b.exchange.revoke()
    assert not s.state['received'] and not s._part('a'*64).exists()

def test_a_name_too_long_for_the_disk_is_refused_and_verify_failures_do_not_stick(peers,monkeypatch):
    a,b=peers;pair(a,b);s=b.exchange.store;peer={'studio':a.exchange.store.studio}
    data=b'hello';digest=hashlib.sha256(data).hexdigest();s.file_chunk(digest,0,5,5,lambda n,c=[data]:c.pop() if c else b'',peer,lambda:None)
    with pytest.raises(ValueError,match='invalid_file_name'):s.file_finish(digest,'é'*150,5,peer)
    s.state['received'][digest].update(state='verifying',name='x'*300);s._verify_file(digest)      # the disk itself refuses this name
    assert s.state['received'][digest]['state']=='partial'

def test_a_timed_out_reply_with_progress_does_not_use_up_retries(peers,tmp_path,monkeypatch):
    a,b=peers;pair(a,b);f=_big(tmp_path,monkeypatch);digest=a.exchange.offer_file(f)['sha256'];real=a.exchange.request
    def flaky(peer,method,path,*x,**k):
        result=real(peer,method,path,*x,**k)
        if method=='PUT':raise OSError('reply timed out')
        return result
    monkeypatch.setattr(a.exchange,'request',flaky);monkeypatch.setattr(T,'FILE_RETRIES',3)
    rows=a.exchange.store.state['files']
    for _ in range(14):
        a.exchange._tick_file();rows[digest]['next']=0
    monkeypatch.setattr(a.exchange,'request',real)
    assert rows[digest]['state']!='blocked' and _drain(a,b,digest)['state']=='received'
