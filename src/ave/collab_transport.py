"""Dedicated TLS editorial inbox. No authoring, approval, account or file routes.

Serialization: canonical UTF-8 JSON (sorted keys, compact separators, no NaN).
Pair transcript=v1 + invite ID + two fresh nonces + exact inviter/joiner public
app/studio/node/TLS/endpoint descriptors. Its SHA256 binds BOTH local confirms.
A256-bit invite is only sent after TLS chain/SAN/pin and socket WhoIs checks.
Each direction receives a separate256-bit bearer kept in Keychain. Six-digit
comparison text is never a credential. Pair interruption requires fresh pairing.
"""
from __future__ import annotations
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler,HTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
from socketserver import ThreadingMixIn
import ssl
import threading
import time
import uuid
from . import collab as C
from . import collab_tls as TLS
from .collab_identity import IdentityError,endpoint,ip

PORT=37091
MAX_PENDING=32
MAX_STORAGE=128*1024*1024
PAIR_LIMIT=24*1024
# Large files use their own route: sequential chunks, resumable from the receiver's durable size, hashed whole before release.
CHUNK=C.MAX_BUNDLE_BYTES
MAX_FILE=512*1024**3
FILE_FLOOR=2*1024**3
MAX_PARTIAL=4
FILE_RETRIES=12
RESPONSE_WAIT=120
STALE_PARTIAL=24*3600
_FILE_NAME=re.compile(r'[^\W_][\w .()-]{0,199}')


def durable(path,data):
    path=C._no_symlink(Path(path));path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name('.'+uuid.uuid4().hex+'.part')
    try:
        with tmp.open('xb') as f:f.write(data);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path);sync_dir(path.parent)
    finally:tmp.unlink(missing_ok=True)


def sync_dir(path):
    fd=os.open(path,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)


def parse(raw,limit=C.MAX_BUNDLE_BYTES):
    if len(raw)>limit:raise ValueError('request_size')
    try:obj=json.loads(raw,object_pairs_hook=C._pairs);C._bounded(obj)
    except (ValueError,RecursionError,UnicodeError):raise ValueError('invalid_json') from None
    if not isinstance(obj,dict):raise ValueError('object_required')
    return obj


def require_keys(obj,keys):
    if not isinstance(obj,dict) or set(obj)!=set(keys):raise ValueError('unexpected_fields')


def sha(value):
    if not isinstance(value,str) or not C._HEX.fullmatch(value):raise ValueError('invalid_sha256')


def descriptor(obj):
    require_keys(obj,('studio','app','identity','endpoint','tls'))
    if any(not isinstance(obj[k],str) or not C._UUID.fullmatch(obj[k]) for k in ('studio','app')):raise ValueError('invalid_app_identity')
    require_keys(obj['identity'],('node','user'))
    if any(not isinstance(v,str) or not 1<=len(v)<=256 for v in obj['identity'].values()):raise ValueError('invalid_device_identity')
    address,port=endpoint(obj['endpoint'])
    if ip(address).is_unspecified or ip(address).is_multicast:raise ValueError('invalid_endpoint')
    sha(obj['tls'].get('pin'));TLS.client_context(obj['tls'])
    return obj


class ExchangeStore:
    def __init__(self,root,studio,*,excluded=()):
        if not C._UUID.fullmatch(studio):raise ValueError('invalid_studio')
        root=C._no_symlink(Path(root))
        if any(root==Path(x).resolve() or Path(x).resolve() in root.parents for x in excluded):raise ValueError('Exchange folder must be outside every live studio/raw folder')
        self.root=root/studio;self.studio=studio;self.lock=threading.RLock()
        for name in ('outbox','inbox','partial','files','received'):C._no_symlink(self.root/name).mkdir(parents=True,exist_ok=True)
        os.chmod(self.root,0o700)
        self.state_path=self.root/'transport.json'
        self.state=C._read(self.state_path) if self.state_path.exists() else {'v':1,'app':uuid.uuid4().hex,'peer':None,'outbox':{}}
        self.state.setdefault('files',{});self.state.setdefault('received',{})
        # Connecting/sending is interrupted work, never a delivery receipt.
        for row in self.state['outbox'].values():
            if row['state'] in ('connecting','sending'):row['state']='queued'
        for row in self.state['files'].values():
            if row['state'] in ('sending','verifying'):row.update(state='queued',offset=None)
        for row in self.state['received'].values():
            if row['state']=='verifying':row['state']='partial'     # the hash reruns when the sender finishes again
            row.pop('busy',None)
        self.save()
        for p in (self.root/'partial').iterdir():
            C._no_symlink(p)
            if p.name.endswith('.part') and p.is_file():p.unlink()
    def save(self):
        if len(C._canonical(self.state))>C.MAX_STATE_BYTES:raise ValueError('transport_state_capacity')
        durable(self.state_path,C._canonical(self.state)+b'\n')
    def file(self,area,name):
        if area not in ('inbox','outbox','partial') or not C._HEX.fullmatch(name):raise ValueError('invalid_storage_selector')
        return C._no_symlink(self.root/area/(name+'.json'))
    def capacity(self,size):
        paths=[p for p in self.root.rglob('*') if not {'files','received'}&set(p.relative_to(self.root).parts)];total=0;count=0
        for p in paths:
            C._no_symlink(p)
            if p.is_file():total+=p.stat().st_size;count+=1
        if total+size>MAX_STORAGE or count>MAX_PENDING*6:raise ValueError('exchange_quota')
    def receive(self,path,transport_sha,peer):
        with self.lock:
            bundle=C._read(path);C._validate_bundle(bundle)
            if bundle['source']!=peer['studio'] or bundle['target']!=self.studio:raise ValueError('wrong_bundle_identity')
            if C.sha256_file(path)!=transport_sha:raise ValueError('transport_hash_mismatch')
            dest=self.file('inbox',transport_sha);receipt=dest.with_suffix('.receipt.json');C._no_symlink(receipt)
            record={'state':'received','transport_sha256':transport_sha,'bundle_id':bundle['id'],'bytes':path.stat().st_size,'source':peer['studio'],'target':self.studio}
            if receipt.exists():
                old=C._read(receipt)
                if old!=record or not dest.exists() or C.sha256_file(dest)!=transport_sha:raise ValueError('receipt_identity_mismatch')
                return old
            self.capacity(path.stat().st_size+len(C._canonical(record)))
            if dest.exists() and C.sha256_file(dest)!=transport_sha:raise ValueError('inbox_identity_mismatch')
            if not dest.exists():os.replace(path,dest);sync_dir(dest.parent)
            durable(receipt,C._canonical(record)+b'\n')
            return record
    def receipt(self,digest,peer):
        path=self.file('inbox',digest).with_suffix('.receipt.json');C._no_symlink(path)
        if not path.exists():return {'state':'not_found'}
        obj=C._read(path)
        if obj['source']!=peer['studio'] or obj['target']!=self.studio:raise ValueError('receipt_scope')
        return obj


    def _part(self,digest):
        sha(digest);return C._no_symlink(self.root/'files'/(digest+'.part'))
    def file_status(self,digest,peer):
        with self.lock:
            row=self.state['received'].get(digest)
            if not row:return {'state':'none','received':0}
            if row['source']!=peer['studio']:raise ValueError('receipt_scope')
            part=self._part(digest)
            return {'state':row['state'],'received':part.stat().st_size if part.exists() else row['bytes'] if row['state']=='complete' else 0}
    def file_chunk(self,digest,offset,total,length,reader,peer,recheck):
        """Append one chunk. Only the next byte is accepted; anything that fails rolls the file back to `offset`."""
        with self.lock:
            sha(digest)
            if total<1 or total>MAX_FILE or offset<0 or offset+length>total or not length:raise ValueError('invalid_range')
            row=self.state['received'].get(digest)
            if row and (row['source']!=peer['studio'] or row['bytes']!=total):raise ValueError('receipt_scope')
            if row and (row['state']!='partial' or row.get('busy')):raise ValueError('file_state')
            part=self._part(digest);have=part.stat().st_size if part.exists() else 0
            if have!=offset:raise ValueError('offset_mismatch')
            if shutil.disk_usage(self.root).free-(total-offset)<FILE_FLOOR:raise ValueError('exchange_disk_floor')
            if not row:
                self._expire_partials()
                if sum(r['state']=='partial' for r in self.state['received'].values())>=MAX_PARTIAL:raise ValueError('exchange_quota')
                row={'sha256':digest,'bytes':total,'source':peer['studio'],'state':'partial','name':None,'path':None};self.state['received'][digest]=row
            row['busy']=True;row['touched']=time.time();self.save()
        try:
            with part.open('ab') as out:
                try:
                    remaining=length
                    while remaining:
                        data=reader(min(1024*1024,remaining))
                        if not data:raise ValueError('short_body')
                        out.write(data);remaining-=len(data)
                    recheck();out.flush();os.fsync(out.fileno())
                except BaseException:
                    out.truncate(offset);raise
        finally:
            with self.lock:row.pop('busy',None);self.save()
        return {'state':'partial','received':offset+length}
    def drop_partial(self,digest):
        self.state['received'].pop(digest,None);self._part(digest).unlink(missing_ok=True)
    def _expire_partials(self):
        for digest,row in list(self.state['received'].items()):
            if row['state']=='partial' and not row.get('busy') and time.time()-row.get('touched',0)>STALE_PARTIAL:self.drop_partial(digest)
    def file_finish(self,digest,name,total,peer):
        """Hash the whole file off the request thread; the sender polls. Bytes land in received/, never in a studio."""
        with self.lock:
            row=self.state['received'].get(digest)
            if not row or row['source']!=peer['studio'] or row['bytes']!=total:raise ValueError('receipt_scope')
            if row['state'] in ('verifying','complete'):return {'state':row['state'],'received':total}
            if not isinstance(name,str) or not _FILE_NAME.fullmatch(name) or len(name.encode())>255:raise ValueError('invalid_file_name')
            part=self._part(digest)
            if not part.exists() or part.stat().st_size!=total:raise ValueError('incomplete_file')
            row.update(state='verifying',name=name);self.save()
        threading.Thread(target=self._verify_file,args=(digest,),daemon=True).start()
        return {'state':'verifying','received':total}
    def _verify_file(self,digest):
        part=self._part(digest)
        try:good=C.sha256_file(part)==digest
        except OSError:good=False
        with self.lock:
            row=self.state['received'][digest]
            try:
                if not good:raise ValueError('hash_mismatch')
                folder=C._no_symlink(self.root/'received'/digest);folder.mkdir(exist_ok=True)
                dest=folder/row['name'];os.replace(part,dest);sync_dir(folder);row.update(state='complete',path=str(dest))
            except (OSError,ValueError):
                part.unlink(missing_ok=True);row.update(state='partial')    # the sender's next status sees 0 and starts over
            self.save()


class Exchange:
    def __init__(self,store,identity,secrets_store):
        self.store=store;self.identity=identity;self.secrets=secrets_store;self.lock=threading.RLock()
        self.server=None;self.thread=None;self.pending=None;self.joining=None;self.enabled=False
        self.material=TLS.provision(secrets_store,'tls:'+store.state['app'])
        self.tls=TLS.server_context(self.material)
        self.stop_event=threading.Event();self.worker=None
        self.failure=None
        self.on_pair=None
    def local_descriptor(self):
        local=self.identity.local_identity()
        if not self.server:raise ValueError('Sharing is disabled')
        return {'studio':self.store.studio,'app':self.store.state['app'],'identity':{k:local[k] for k in ('node','user')},'endpoint':list(self.server.socket.getsockname()[:2]),'tls':TLS.public(self.material)}
    def start(self,port=PORT):
        with self.lock:
            if self.enabled:return
            local=self.identity.local_identity()
            self.server=Server(self,local,port);self.enabled=True;self.stop_event.clear()
            self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
            self.worker=threading.Thread(target=self._work,daemon=True);self.worker.start()
    def stop(self):
        with self.lock:
            self.enabled=False;self.stop_event.set();self.pending=None;self.joining=None;server=self.server;self.server=None
        if server:server.shutdown();server.server_close()
        if self.worker and self.worker is not threading.current_thread():self.worker.join(timeout=6)
    def invite(self):
        with self.lock:
            if self.store.state['peer']:raise ValueError('Revoke the existing collaborator before pairing another')
            public=self.local_descriptor();secret=secrets.token_hex(32);nonce=secrets.token_hex(32)
            invite={'v':1,'id':uuid.uuid4().hex,'secret':secret,'expires':time.time()+600,'inviter':public}
            self.pending={'invite':invite,'nonce':nonce,'transcript':None,'confirmed':False,'attempts':0}
            return invite
    def cancel_pair(self):
        with self.lock:self.pending=None;self.joining=None
    def _pair(self,obj,observed,address):
        with self.lock:
            p=self.pending
            if not p or time.time()>p['invite']['expires']:self.pending=None;raise ValueError('invitation_unavailable')
            p['attempts']+=1
            if p['attempts']>12:self.pending=None;raise ValueError('invitation_attempt_limit')
            if not isinstance(obj.get('secret'),str) or not hmac.compare_digest(obj['secret'],p['invite']['secret']) or obj.get('id')!=p['invite']['id']:raise ValueError('invitation_refused')
            if obj.get('phase')=='hello':
                require_keys(obj,('phase','id','secret','nonce','joiner'));sha(obj['nonce']);other=descriptor(obj['joiner'])
                if p['transcript'] is not None:raise ValueError('invitation_replay')
                if other['identity']!=observed or ip(other['endpoint'][0])!=ip(address) or other['studio']==self.store.studio:raise ValueError('pair_identity_mismatch')
                t={'v':1,'invite':p['invite']['id'],'inviter':p['invite']['inviter'],'joiner':other,'inviter_nonce':p['nonce'],'joiner_nonce':obj['nonce']}
                p['transcript']=t
                return {'transcript':t,'hash':C._sha(t),'code':comparison(t)}
            require_keys(obj,('phase','id','secret','hash','incoming'))
            if obj['phase']!='confirm' or not p['transcript'] or obj['hash']!=C._sha(p['transcript']):raise ValueError('pair_confirmation_mismatch')
            other=p['transcript']['joiner']
            if observed!=other['identity'] or ip(address)!=ip(other['endpoint'][0]):raise ValueError('pair_identity_changed')
            sha(obj['incoming'])
            if not p['confirmed']:return {'state':'waiting_for_local_confirmation'}
            incoming=secrets.token_hex(32)
            self._activate(other,incoming,obj['incoming'],obj['hash'])
            self.pending=None
            return {'state':'paired','incoming':incoming,'hash':obj['hash']}
    def join(self,invite):
        with self.lock:
            require_keys(invite,('v','id','secret','expires','inviter'))
            if invite['v']!=1 or not isinstance(invite['expires'],(int,float)) or not time.time()<invite['expires']<=time.time()+605:raise ValueError('invitation_expired')
            sha(invite['secret']);descriptor(invite['inviter'])
            if self.store.state['peer']:raise ValueError('Revoke existing collaborator first')
            own=self.local_descriptor();nonce=secrets.token_hex(32)
            result=self.request(invite['inviter'],'POST','/exchange/v1/pair',{'phase':'hello','id':invite['id'],'secret':invite['secret'],'nonce':nonce,'joiner':own})
            expected={'v':1,'invite':invite['id'],'inviter':invite['inviter'],'joiner':own,'inviter_nonce':result['transcript']['inviter_nonce'],'joiner_nonce':nonce}
            sha(expected['inviter_nonce'])
            if result['transcript']!=expected or result['hash']!=C._sha(expected) or result['code']!=comparison(expected):raise ValueError('pair_transcript_mismatch')
            self.joining={'invite':invite,'transcript':expected,'incoming':secrets.token_hex(32),'confirmed':False}
            return {'hash':result['hash'],'code':result['code'],'peer':invite['inviter']}
    def confirm(self,transcript_hash):
        with self.lock:
            if self.pending and self.pending['transcript'] and C._sha(self.pending['transcript'])==transcript_hash:
                self.pending['confirmed']=True;return {'state':'waiting_for_remote_confirmation'}
            j=self.joining
            if not j or C._sha(j['transcript'])!=transcript_hash:raise ValueError('Pairing changed or cancelled; compare a fresh invitation')
            j['confirmed']=True
            return self.finish_pair()
    def finish_pair(self):
        j=self.joining
        if not j or not j['confirmed']:return None
        invite=j['invite']
        if time.time()>invite['expires']:self.joining=None;raise ValueError('invitation_expired')
        digest=C._sha(j['transcript']);result=self.request(invite['inviter'],'POST','/exchange/v1/pair',{'phase':'confirm','id':invite['id'],'secret':invite['secret'],'hash':digest,'incoming':j['incoming']})
        if result['state']=='paired':
            sha(result['incoming'])
            if result['hash']!=digest:raise ValueError('pair_confirmation_mismatch')
            self._activate(invite['inviter'],j['incoming'],result['incoming'],digest);self.joining=None
        return {'state':result['state']}
    def _activate(self,peer,incoming,outgoing,digest):
        descriptor(peer);reference='peer:'+self.store.state['app']+':'+digest
        if self.on_pair:self.on_pair(peer)
        self.secrets.put(reference+':in',incoming);self.secrets.put(reference+':out',outgoing)
        self.store.state['peer']={'descriptor':peer,'reference':reference,'transcript':digest};self.store.save()
    def revoke(self):
        with self.lock:
            p=self.store.state['peer'];self.pending=None;self.joining=None
            self.store.state['peer']=None
            for row in self.store.state['outbox'].values():
                if row['state']!='received':row['state']='blocked';row['error']='peer_revoked'
            for row in self.store.state['files'].values():
                if row['state']!='received':row['state']='blocked';row['error']='peer_revoked'
            for digest,row in list(self.store.state['received'].items()):
                if row['state']!='complete' and not row.get('busy'):self.store.drop_partial(digest)
            self.store.save()
            if p:
                for suffix in (':in',':out'):self.secrets.delete(p['reference']+suffix)
        self.stop()
    def authenticate(self,sock,bearer):
        observed=self.identity.peer_identity(sock);p=self.store.state['peer']
        if not p or observed!=p['descriptor']['identity']:raise IdentityError('peer_identity_changed')
        expected=self.secrets.get(p['reference']+':in')
        if not expected or not isinstance(bearer,str) or not hmac.compare_digest(bearer,'Bearer '+expected):raise ValueError('authentication_refused')
        return p['descriptor']
    def request(self,peer,method,path,payload=None,*,file=None,bearer=None,data=None):
        if not self.enabled:raise ValueError('sharing_disabled')
        descriptor(peer);address,port=endpoint(peer['endpoint']);local=self.identity.local_identity()
        sock=socket.socket(socket.AF_INET6 if ip(address).version==6 else socket.AF_INET,socket.SOCK_STREAM);sock.settimeout(5)
        try:
            self.identity.bind(sock,local)
            sock.connect((address,port))
            if self.identity.peer_identity(sock)!=peer['identity']:raise IdentityError('peer_identity_changed')
            sock=TLS.client_context(peer['tls']).wrap_socket(sock,server_hostname=peer['tls']['name']);TLS.verify_pin(sock,peer['tls'])
            # Same authenticated socket, no proxy, redial or redirect helper.
            self.identity.peer_identity(sock)
            body=data if data is not None else C._canonical(payload) if payload is not None else b'';size=Path(file).stat().st_size if file else len(body)
            headers=[f'{method} {path} HTTP/1.1',f'Host: {peer["tls"]["name"]}','Connection: close',f'Content-Length: {size}','Content-Type: '+('application/octet-stream' if data is not None else 'application/json')]
            if bearer:headers.append('Authorization: Bearer '+bearer)
            sock.sendall(('\r\n'.join(headers)+'\r\n\r\n').encode('ascii'))
            if file:
                with Path(file).open('rb') as source:
                    while data:=source.read(65536):
                        if self.stop_event.is_set():raise OSError('Sharing stopped')
                        sock.sendall(data)
            elif body:
                for start in range(0,len(body),65536):
                    if self.stop_event.is_set():raise OSError('Sharing stopped')
                    sock.sendall(body[start:start+65536])       # the socket timeout is per slice, so slow uplinks keep moving
            if data is not None:sock.settimeout(RESPONSE_WAIT)  # the receiver fsyncs before it answers; exFAT can take a while
            response=http.client.HTTPResponse(sock);response.begin()
            if response.status!=200:raise ValueError('peer_refused_'+str(response.status))
            if response.getheader('Transfer-Encoding') or not response.getheader('Content-Length','').isdigit():raise ValueError('invalid_response_framing')
            length=int(response.getheader('Content-Length'))
            if length>PAIR_LIMIT:raise ValueError('response_size')
            raw=response.read(length+1)
            if len(raw)!=length:raise ValueError('short_response')
            return parse(raw,PAIR_LIMIT)
        finally:sock.close()
    def enqueue(self,path):
        with self.lock,self.store.lock:
            p=self.store.state['peer']
            if not p:raise ValueError('Pair a collaborator first')
            path=C._no_symlink(Path(path));bundle=C._read(path);C._validate_bundle(bundle)
            if bundle['source']!=self.store.studio or bundle['target']!=p['descriptor']['studio']:raise ValueError('bundle_pair_binding')
            digest=C.sha256_file(path)
            if digest in self.store.state['outbox']:return self.store.state['outbox'][digest]
            if len(self.store.state['outbox'])>=MAX_PENDING:raise ValueError('outbox_capacity')
            self.store.capacity(path.stat().st_size);dest=self.store.file('outbox',digest)
            durable(dest,path.read_bytes())
            row={'sha256':digest,'bundle_id':bundle['id'],'peer':p['descriptor']['studio'],'state':'queued','attempts':0,'next':0,'error':None,'receipt':None}
            self.store.state['outbox'][digest]=row;self.store.save();return copy_row(row)
    def cancel(self,digest):
        with self.lock:
            row=self.store.state['outbox'][digest]
            if row['state']!='received':row['state']='cancelled'
            self.store.save()
    def offer_file(self,path):
        """One explicitly chosen file. It is read in place and only ever goes to the paired collaborator."""
        path=C._no_symlink(Path(path).absolute())
        if not path.is_file():raise ValueError('Choose a regular file')
        st=path.stat();name=path.name
        if st.st_size<1 or st.st_size>MAX_FILE:raise ValueError('File size is outside the sharing limit')
        if not _FILE_NAME.fullmatch(name) or len(name.encode())>255:raise ValueError('Rename this file: letters, numbers, spaces, dots, dashes and brackets only')
        with self.lock:
            if not self.store.state['peer']:raise ValueError('Pair a collaborator first')
        digest=C.sha256_file(path)
        with self.lock:
            rows=self.store.state['files']
            if digest in rows and rows[digest]['state'] not in ('cancelled','blocked'):return copy_row(rows[digest])
            if sum(r['state'] not in ('received','cancelled','blocked') for r in rows.values())>=MAX_PARTIAL:raise ValueError('Too many files are already waiting to send')
            rows[digest]={'sha256':digest,'name':name,'path':str(path),'bytes':st.st_size,'mtime_ns':st.st_mtime_ns,'peer':self.store.state['peer']['descriptor']['studio'],
                          'state':'queued','offset':None,'attempts':0,'next':0,'error':None}
            self.store.save();return copy_row(rows[digest])
    def cancel_file(self,digest):
        with self.lock:
            row=self.store.state['files'][digest]
            if row['state']!='received':row['state']='cancelled'
            self.store.save()
    def _tick_file(self):
        with self.lock:
            if not self.enabled or not (p:=self.store.state['peer']):return
            row=next((r for r in self.store.state['files'].values() if r['state'] in ('queued','sending','verifying','retry_wait') and r['next']<=time.time()),None)
            if not row:return
            peer=p['descriptor'];reference=p['reference'];digest=row['sha256']
        route='/exchange/v1/files/'+digest
        try:
            bearer=self.secrets.get(reference+':out')
            if not bearer:raise IdentityError('credential_unavailable')
            source=C._no_symlink(Path(row['path']));st=source.stat()
            if st.st_size!=row['bytes'] or st.st_mtime_ns!=row['mtime_ns']:raise IdentityError('source_changed')
            if row['offset'] is None or row['state']=='verifying':
                status=self.request(peer,'GET',route,bearer=bearer)
                if status['state']=='complete':
                    with self.lock:row.update(state='received',offset=row['bytes'],error=None);self.store.save()
                    return
                if status['state']=='verifying':
                    with self.lock:row.update(state='verifying',next=time.time()+2);self.store.save()
                    return
                with self.lock:
                    moved=status['received']>row.get('confirmed',0)
                    row.update(offset=status['received'],state='sending',confirmed=status['received'],attempts=0 if moved else row['attempts']);self.store.save()
            offset=row['offset']
            if offset<row['bytes']:
                with source.open('rb') as f:
                    f.seek(offset);data=f.read(min(CHUNK,row['bytes']-offset))
                result=self.request(peer,'PUT',f'{route}/{offset}/{row["bytes"]}',data=data,bearer=bearer)
                with self.lock:
                    if row['state']=='sending':row.update(offset=result['received'],attempts=0,error=None);self.store.save()
            else:
                self.request(peer,'POST',route,{'name':row['name'],'bytes':row['bytes']},bearer=bearer)
                with self.lock:row.update(state='verifying',next=time.time()+1);self.store.save()
        except IdentityError as exc:
            with self.lock:row.update(state='blocked',error=str(exc));self.store.save()
        except (OSError,ValueError):
            with self.lock:
                if row['state']=='cancelled':return
                row['attempts']+=1;row['offset']=None
                row.update(state='blocked',error='peer_refused') if row['attempts']>=FILE_RETRIES else row.update(state='retry_wait',error='peer_offline',next=time.time()+min(300,2**min(row['attempts'],8)))
                self.store.save()
    def tick(self):
        self._tick_bundle();self._tick_file()
    def _tick_bundle(self):
        with self.lock:
            if not self.enabled:return
            if self.joining and self.joining['confirmed']:
                try:self.finish_pair()
                except (OSError,ValueError):self.failure='pair_retry_requires_review'
            p=self.store.state['peer']
            if not p:return
            candidates=[r for r in self.store.state['outbox'].values() if r['state'] in ('queued','retry_wait') and r['next']<=time.time()]
            if not candidates:return
            row=candidates[0];row['state']='connecting';row['attempts']+=1;self.store.save();digest=row['sha256'];peer=p['descriptor'];reference=p['reference']
        try:
            bearer=self.secrets.get(reference+':out')
            if not bearer:raise IdentityError('credential_unavailable')
            row['state']='sending';self.store.save()
            receipt=self.request(peer,'PUT','/exchange/v1/bundles/'+digest,file=self.store.file('outbox',digest),bearer=bearer)
            expected={'state':'received','transport_sha256':digest,'bundle_id':row['bundle_id'],'bytes':self.store.file('outbox',digest).stat().st_size,'source':self.store.studio,'target':peer['studio']}
            if receipt!=expected:raise ValueError('receipt_identity_mismatch')
            with self.lock:
                if row['state']!='cancelled':row['state']='received'
                row['receipt']=receipt;row['error']=None;self.store.save()
        except (OSError,ValueError) as exc:
            with self.lock:
                if row['state']=='cancelled':return
                blocked=isinstance(exc,(IdentityError,ssl.SSLError,ValueError))
                row['state']='blocked' if blocked else 'retry_wait';row['error']='identity_or_auth_refused' if blocked else 'peer_offline'
                row['next']=time.time()+min(300,2**min(row['attempts'],8))+secrets.randbelow(1000)/1000;self.store.save()
    def _work(self):
        while not self.stop_event.wait(1):
            try:self.tick()
            except (OSError,ValueError):self.failure='sharing_unavailable'


def comparison(transcript):return f'{int(C._sha(transcript)[:12],16)%1000000:06d}'
def copy_row(row):return json.loads(json.dumps(row))


class Server(ThreadingMixIn,HTTPServer):
    daemon_threads=True;allow_reuse_address=True
    def __init__(self,exchange,local,port):
        self.exchange=exchange;self.slots=threading.BoundedSemaphore(4);self.rates={};self.rate_lock=threading.Lock()
        self.address_family=socket.AF_INET6 if ip(local['address']).version==6 else socket.AF_INET
        super().__init__((local['address'],port),Handler,bind_and_activate=False)
        try:
            self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);exchange.identity.bind(self.socket,local,port);self.server_activate()
        except Exception:self.server_close();raise
    def process_request(self,request,address):
        if not self.slots.acquire(blocking=False):request.close();return
        super().process_request(request,address)
    def process_request_thread(self,request,address):
        try:
            request.settimeout(5)
            self.exchange.identity.peer_identity(request)
            wrapped=self.exchange.tls.wrap_socket(request,server_side=True)
            try:super().process_request_thread(wrapped,address)
            finally:wrapped.close()
        except (OSError,ValueError):request.close()
        finally:self.slots.release()
    def rate(self,node):
        with self.rate_lock:
            now=time.monotonic();recent=[v for v in self.rates.get(node,[]) if now-v<60]
            if len(recent)>=60:raise ValueError('request_rate')
            self.rates={k:v for k,v in self.rates.items() if v and now-v[-1]<60}
            if len(self.rates)>=32 and node not in self.rates:raise ValueError('request_rate')
            self.rates[node]=recent+[now]


class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1';server_version='AVEExchange';sys_version=''
    def log_message(self,*args):pass
    def handle_expect_100(self):self.send_error(417);return False
    def do_POST(self):self.dispatch()
    def do_PUT(self):self.dispatch()
    def do_GET(self):self.dispatch()
    def dispatch(self):
        self.close_connection=True;temp=None
        try:
            if not self.server.exchange.enabled:raise ValueError('sharing_disabled')
            for name in ('Content-Length','Content-Type','Authorization','Host','Connection'):
                if len(self.headers.get_all(name,[]))>1:raise ValueError('ambiguous_headers')
            if self.headers.get('Origin') is not None or self.headers.get('Transfer-Encoding') is not None or self.headers.get('Expect') is not None:raise ValueError('unsupported_framing')
            length=self.headers.get('Content-Length','')
            if not length.isascii() or not length.isdigit() or len(length)>10:raise ValueError('length_required')
            length=int(length)
            files=self.path.startswith('/exchange/v1/files/')
            if length>C.MAX_BUNDLE_BYTES or self.headers.get('Content-Type') not in (('application/json','application/octet-stream') if files else ('application/json',)):raise ValueError('unsupported_body')
            ex=self.server.exchange;observed=ex.identity.peer_identity(self.connection);self.server.rate(observed['node'])
            if self.command=='POST' and self.path=='/exchange/v1/pair':
                if length>PAIR_LIMIT:raise ValueError('pair_body_limit')
                raw=self.rfile.read(length)
                if len(raw)!=length:raise ValueError('short_body')
                result=ex._pair(parse(raw,PAIR_LIMIT),observed,self.connection.getpeername()[0])
            elif files:
                bearer=self.headers.get('Authorization');peer=ex.authenticate(self.connection,bearer)
                parts=self.path[len('/exchange/v1/files/'):].split('/');digest=parts[0];sha(digest)
                if self.command=='GET' and len(parts)==1 and not length:result=ex.store.file_status(digest,peer)
                elif self.command=='POST' and len(parts)==1:
                    raw=self.rfile.read(length)
                    if len(raw)!=length or length>PAIR_LIMIT:raise ValueError('short_body')
                    obj=parse(raw,PAIR_LIMIT);require_keys(obj,('name','bytes'));result=ex.store.file_finish(digest,obj['name'],obj['bytes'],peer)
                elif self.command=='PUT' and len(parts)==3 and all(x.isascii() and x.isdigit() and len(x)<16 for x in parts[1:]):
                    result=ex.store.file_chunk(digest,int(parts[1]),int(parts[2]),length,self.rfile.read,peer,lambda:ex.authenticate(self.connection,bearer))
                else:raise ValueError('unknown_route')
            else:
                peer=ex.authenticate(self.connection,self.headers.get('Authorization'))
                prefix='/exchange/v1/bundles/' if self.command=='PUT' else '/exchange/v1/receipts/'
                if self.command not in ('PUT','GET') or not self.path.startswith(prefix):raise ValueError('unknown_route')
                digest=self.path[len(prefix):];sha(digest)
                if self.command=='GET':
                    if length:raise ValueError('unexpected_body')
                    result=ex.store.receipt(digest,peer)
                else:
                    ex.store.capacity(length);temp=C._no_symlink(ex.store.root/'partial'/(uuid.uuid4().hex+'.part'))
                    with temp.open('xb') as out:
                        remaining=length
                        while remaining:
                            data=self.rfile.read(min(65536,remaining))
                            if not data:raise ValueError('short_body')
                            out.write(data);remaining-=len(data)
                        out.flush();os.fsync(out.fileno())
                    # Local/remote identity and immediate revocation rechecked
                    # after a bounded upload, before publishing a receipt.
                    ex.authenticate(self.connection,self.headers.get('Authorization'))
                    result=ex.store.receive(temp,digest,peer)
            raw=C._canonical(result);self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.send_header('Connection','close');self.end_headers();self.wfile.write(raw)
        except (OSError,ValueError,KeyError,TypeError):
            try:self.send_error(400,'Exchange request refused')
            except OSError:pass
        finally:
            if temp:temp.unlink(missing_ok=True)
