"""Public connection projections and protected backend operations.

Only desktop_connections calls mutations after contextual native authentication.
No HTTP credential endpoint exists. Ordinary projection never reads Keychain.
"""
from __future__ import annotations
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import yaml
from ..platforms.accounts import Account, account_path, list_accounts, load_account
from ..platforms.connection_verify import PRODUCTS, DESTINATIONS, Reader, VerificationError, endpoint, identifier, verify
from . import production as P

PUBLIC = {'handle','pds','ig_user_id','threads_user_id','page_id','author','board_id','sandbox','user_id','did','channel_id','client_id'}


def digest(value): return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def revision(sp, aid): return hashlib.sha256(P.safe_path(sp,account_path(sp,aid)).read_bytes()).hexdigest()
def directory(sp):
    from .desk import _safe_path
    path=sp.work/'connections';_safe_path(path,sp.root)
    if path.exists() and not path.is_dir():raise ValueError('Connections state must be a directory')
    return path
def record_path(sp,aid): return P.safe_path(sp,directory(sp)/(aid+'.json'))
def read(path):
    try:
        if path.stat().st_size>262144: return {}
        d=json.loads(path.read_text());return d if isinstance(d,dict) else {}
    except (OSError,ValueError): return {}

def atomic(path,data):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    tmp=path.with_name(path.name+'.'+secrets.token_hex(6)+'.tmp')
    try:
        with tmp.open('x') as f: json.dump(data,f,indent=2);f.flush();os.fsync(f.fileno())
        tmp.chmod(0o600);os.replace(tmp,path)
    finally: tmp.unlink(missing_ok=True)


def binding(account):
    return digest({'workspace':str(account.studio_root.resolve()),'id':account.id,'platform':account.platform,'mode':account.mode,'public':account.public,'connection':account.connection})


def fields(account):
    if account.platform=='youtube': return ['client_secret','refresh_token']
    if account.platform=='bluesky': return ['app_password']
    if account.mode=='aggregator': return []
    return ['token'] if account.platform!='manual' else []


class Keychain:
    """OS Keychain only, never the environment/legacy-file fallback."""
    def __init__(self):
        from keyring.backends.macOS import Keyring
        self.backend=Keyring()
    def get(self,name):
        from ..publish.safety import SERVICE, register_secret
        try:v=self.backend.get_password(SERVICE,name)
        except Exception:raise ValueError('macOS Keychain refused access; reopen Connections and try again') from None
        if v: register_secret(v)
        return v
    def set(self,name,value):
        from ..publish.safety import SERVICE, register_secret
        register_secret(value);self.backend.set_password(SERVICE,name,value)
    def remove(self,name):
        from ..publish.safety import SERVICE
        from keyring.errors import PasswordDeleteError
        try:self.backend.delete_password(SERVICE,name)
        except PasswordDeleteError:
            if self.backend.get_password(SERVICE,name) is not None: raise


def overview(studio):
    sp=studio.sp;rows=[]
    for a in list_accounts(sp):
        doc=read(record_path(sp,a.id));bound=doc.get('binding')==binding(a)
        evidence=doc.get('evidence') if bound else None
        trusted=bool(isinstance(evidence,dict) and getattr(studio,'_connection_observations',{}).get(a.id)==observation_identity(sp,a,doc))
        if not trusted:getattr(studio,'_connection_observations',{}).pop(a.id,None)
        if evidence and not trusted:
            evidence={'state':'recorded_unvalidated','source':'unvalidated local record',
                'limitations':['Verify connection in this workspace session to validate identity evidence. Stored evidence has not been authenticated in this session.']}
        rows.append({'id':a.id,'label':a.label or a.id,'platform':a.platform,'mode':a.mode,'public':a.public,
            'revision':revision(sp,a.id),'product':a.connection.get('product'),'token_family':PRODUCTS.get(a.connection.get('product'),('', 'Not selected'))[1],
            'stored':doc.get('stored',[]) if bound else [],'credential_state':doc.get('credential_state','unknown') if bound else 'unknown',
            'evidence':evidence,'evidence_trusted':trusted,'verified':bool(trusted and evidence.get('state')=='identity_verified'),
            'live_enabled':bool(bound and doc.get('live_enabled')),'live_tested':False,
            'paused':a.paused(),'manual_available':True,'secret_fields':fields(a),
            'note':'Live-enabled is the last native decision; the sender rechecks signed authority at every tick. Live-tested requires an independently reviewed real publication.'})
    host=read(P.safe_path(sp,directory(sp)/'host.json'))
    return {'accounts':rows,'products':{k:{'platform':v[0],'family':v[1],'version':v[2]} for k,v in PRODUCTS.items()},
        'host':{'revision':digest(host),'config':host.get('config',{}),'stored':host.get('stored',[]),'state':host.get('state','not_configured')},
        'paused':sp.pause_path.exists(),'sender':read(P.safe_path(sp,directory(sp)/'sender.json')),
        'manual_note':'Local profiles and manual packages work without credentials, a media host or a background sender.'}


@contextmanager
def connection_lock(sp):
    from ..schedule.tick import tick_lock
    with tick_lock(sp) as got:
        if not got:raise ValueError('Background delivery is using this workspace. Pause it and retry the connection change.')
        with P.workspace_lock(sp):yield


def evidence_signature(account,evidence):
    from .seal import mac
    return mac('connection-evidence',binding(account),digest(evidence))


def evidence_valid(account,doc):
    from .seal import verify_mac
    e=doc.get('evidence')
    return bool(isinstance(e,dict) and doc.get('binding')==binding(account) and verify_mac(doc.get('evidence_sig'),'connection-evidence',binding(account),digest(e)))


def observation_identity(sp,account,doc):
    # Public comparison only: never obtain the signing key while rendering.
    try:record_sha=hashlib.sha256(record_path(sp,account.id).read_bytes()).hexdigest()
    except OSError:record_sha='missing'
    return digest({'workspace':str(sp.root.resolve()),'account':account.id,'revision':revision(sp,account.id),
        'binding':binding(account),'record':doc,'record_sha':record_sha})


def remember_observation(studio,account,doc):
    # Called only after a protected provider action or signed-evidence validation.
    if not hasattr(studio,'_connection_observations'):studio._connection_observations={}
    studio._connection_observations[account.id]=observation_identity(studio.sp,account,doc)


def check_revision(sp,aid,expected):
    if revision(sp,aid)!=expected: raise ValueError('Connection changed; reopen it before continuing')
    return load_account(sp,aid)


def save_config(studio,aid,expected,values):
    if set(values)!={'product','public'} or not isinstance(values['public'],dict) or set(values['public'])-PUBLIC: raise ValueError('Invalid public connection fields')
    product=values['product'];public={k:v for k,v in values['public'].items() if v not in ('',None)}
    with connection_lock(studio.sp):
        a=check_revision(studio.sp,aid,expected)
        getattr(studio,'_connection_observations',{}).pop(aid,None)
        if product not in PRODUCTS or PRODUCTS[product][0]!=a.platform: raise ValueError('Choose the login product for this platform')
        for k,v in public.items():
            if k=='sandbox':
                if type(v) is not bool: raise ValueError('Sandbox must be a boolean')
            elif k=='pds': public[k]=endpoint(v)
            else: identifier(v)
        if not public.get(DESTINATIONS[a.platform]): raise ValueError('Enter the exact destination identity')
        if a.platform=='youtube' and not public.get('client_id'): raise ValueError('Enter the registered OAuth client ID')
        if a.platform=='bluesky' and not public.get('handle'): raise ValueError('Enter the login handle and durable DID')
        path=account_path(studio.sp,aid);cfg=yaml.safe_load(path.read_text())
        cfg['mode']='inbox' if a.platform=='tiktok' else 'graph'
        cfg['credentials']={'ref':'native_'+digest(str(studio.sp.root.resolve())+'|'+aid)[:28],**public};cfg['connection']={'product':product,'token_family':PRODUCTS[product][1],'root_revision':secrets.token_hex(16)}
        Account.from_config(cfg,studio.sp.accounts_dir)
        # Config binding invalidates earlier live attestations and queue approvals.
        tmp=P.safe_path(studio.sp,path.with_suffix('.yaml.tmp'));tmp.write_text(yaml.safe_dump(cfg,sort_keys=False));tmp.replace(path)
        atomic(record_path(studio.sp,aid),{'binding':binding(load_account(studio.sp,aid)),'credential_state':'unknown','stored':[],'live_enabled':False})
    return {'saved':True,'revision':revision(studio.sp,aid),'message':'Destination saved. Credentials and identity must be checked for this configuration.'}


def credentials(studio,aid,expected,values,*,keychain=None,remove=False):
    keychain=keychain or Keychain();sp=studio.sp
    with connection_lock(sp):
        a=check_revision(sp,aid,expected);names=fields(a)
        getattr(studio,'_connection_observations',{}).pop(aid,None)
        if a.connection.get('product') not in PRODUCTS or not a.connection.get('root_revision'):raise ValueError('Confirm the destination configuration first')
        if not names: raise ValueError('Select a supported connection product first')
        if set(values)!=set(names) and not remove: raise ValueError('All credential fields are required')
        if any(not isinstance(v,str) or not v.strip() or len(v)>32000 for v in values.values()): raise ValueError('Enter a nonempty credential in each secure field')
        # Invalidate BEFORE any Keychain call. A partial operation remains visible
        # and cannot retain old live permission/evidence under changed secrets.
        path=account_path(sp,aid);cfg=yaml.safe_load(path.read_text());cfg.setdefault('connection',{})['root_revision']=secrets.token_hex(16)
        tmp=P.safe_path(sp,path.with_suffix('.yaml.tmp'));tmp.write_text(yaml.safe_dump(cfg,sort_keys=False));tmp.replace(path)
        a=load_account(sp,aid);doc={'binding':binding(a),'credential_state':'updating','stored':[],'live_enabled':False}
        atomic(record_path(sp,aid),doc)
        try:
            # Session credentials are never usable across a root replacement.
            for f in ('access_token','refresh_jwt'): keychain.remove(a.secret_name(f))
            if a.platform=='youtube':
                if remove:keychain.remove(a.secret_name('credentials'))
                else:keychain.set(a.secret_name('credentials'),json.dumps({'client_id':a.public['client_id'],**values}))
                doc['stored']=[] if remove else names
            else:
                for f in names:
                    if remove:keychain.remove(a.secret_name(f))
                    else:keychain.set(a.secret_name(f),values[f]);doc['stored'].append(f)
                    atomic(record_path(sp,aid),doc)
            doc['credential_state']='removed' if remove else 'stored'
        except Exception:
            doc['credential_state']='partial_update';atomic(record_path(sp,aid),doc)
            return {'ok':False,'state':'partial_update','stored':doc['stored'],'message':'Keychain refused part of the update. Sending authority and verification were invalidated. Re-enter all fields or remove the connection credentials.'}
        atomic(record_path(sp,aid),doc)
    return {'ok':True,'state':doc['credential_state'],'stored':doc['stored']}


def verify_connection(studio,aid,expected,*,keychain=None,reader=None):
    keychain=keychain or Keychain();sp=studio.sp
    with connection_lock(sp):
        a=check_revision(sp,aid,expected);doc=read(record_path(sp,aid));bind=binding(a)
        getattr(studio,'_connection_observations',{}).pop(aid,None)
        if doc.get('binding')!=bind or doc.get('credential_state') not in ('stored','session_ready'): raise ValueError('Store the complete credential set before verifying')
        try: evidence=verify(a,lambda f:keychain.get(a.secret_name(f)),reader=reader)
        except VerificationError as exc: evidence={**exc.detail,'checked_at':studio.now().isoformat(),'source':'provider_read','product':a.connection.get('product')}
        except Exception:evidence={'state':'credential_or_response_unavailable','checked_at':studio.now().isoformat(),'source':'native_check','product':a.connection.get('product')}
        if evidence.get('state') in ('authentication_failed','expired_token','revoked_token','permission_denied','destination_mismatch','destination_not_managed','actor_mismatch','inactive_identity','channel_not_found','organization_role_unverified','principal_drift','oauth_client_mismatch'):
            from ..platforms.attest import revoke_live
            revoke_live(sp.accounts_dir,aid,'native verification failed');doc['live_enabled']=False
        doc['evidence']=evidence;doc['evidence_sig']=evidence_signature(a,evidence);atomic(record_path(sp,aid),doc)
        remember_observation(studio,a,doc)
        return evidence


def connect_session(studio,aid,expected,*,keychain=None,reader=None):
    """Explicit credential exchange; routine same-principal renewal preserves root binding.
    A returned actor/client drift invalidates authority and never stores the new session.
    """
    keychain=keychain or Keychain();r=reader or Reader();sp=studio.sp
    with connection_lock(sp):
        a=check_revision(sp,aid,expected);doc=read(record_path(sp,aid))
        getattr(studio,'_connection_observations',{}).pop(aid,None)
        if doc.get('binding')!=binding(a) or doc.get('credential_state') not in ('stored','session_ready'): raise ValueError('Store credentials first')
        try:
            if a.platform=='bluesky':
                result=r.request('POST',endpoint(a.public.get('pds') or 'https://bsky.social')+'/xrpc/com.atproto.server.createSession',json_body={'identifier':a.public['handle'],'password':keychain.get(a.secret_name('app_password'))})
                if result.get('did')!=a.public.get('did'): raise VerificationError('principal_drift')
                token=result.get('accessJwt')
            elif a.platform=='youtube':
                root=json.loads(keychain.get(a.secret_name('credentials')) or '{}')
                if root.get('client_id')!=a.public.get('client_id'): raise VerificationError('oauth_client_mismatch')
                result=r.request('POST','https://oauth2.googleapis.com/token',data={'grant_type':'refresh_token',**root});token=result.get('access_token')
                # Check the new token's client and chosen channel before saving.
                class Candidate:
                    def __call__(self,field): return token if field=='access_token' else None
                evidence=verify(a,Candidate(),reader=r)
                if evidence['state']!='identity_verified': raise VerificationError('principal_drift')
            else: raise ValueError('This product uses direct token entry')
            if not isinstance(token,str) or not token or len(token)>32000: raise VerificationError('missing_session_token')
            keychain.set(a.secret_name('access_token'),token)
            if a.platform=='bluesky' and result.get('refreshJwt'): keychain.set(a.secret_name('refresh_jwt'),result['refreshJwt'])
            doc['credential_state']='session_ready';doc['session_renewed_at']=studio.now().isoformat()
            # New grant/identity evidence is explicit; root identity remains stable.
            if a.platform=='youtube':doc['evidence']=evidence;doc['evidence_sig']=evidence_signature(a,evidence)
            atomic(record_path(sp,aid),doc)
            if a.platform=='youtube':remember_observation(studio,a,doc)
            return {'state':'session_ready','root_binding_preserved':True}
        except VerificationError as exc:
            doc['evidence']=exc.detail;doc['live_enabled']=False
            from ..platforms.attest import revoke_live
            revoke_live(sp.accounts_dir,aid,'native connection failure')
            atomic(record_path(sp,aid),doc);return exc.detail
        except Exception:
            doc['credential_state']='partial_update';doc['live_enabled']=False
            from ..platforms.attest import revoke_live
            revoke_live(sp.accounts_dir,aid,'native session update failed');atomic(record_path(sp,aid),doc)
            return {'state':'session_update_failed','message':'The exchange or Keychain update failed. Reconnect before enabling this account.'}


def host_config(values):
    if set(values)!={'endpoint','bucket','region','prefix'}: raise ValueError('Invalid host settings')
    out=dict(values);out['endpoint']=endpoint(out['endpoint'])
    if not re_full(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]',out['bucket']) or '..' in out['bucket']: raise ValueError('Use a valid S3 bucket name')
    if not re_full(r'[A-Za-z0-9-]{1,50}',out['region']): raise ValueError('Use a region name or auto')
    if not re_full(r'[A-Za-z0-9_/-]{0,120}',out['prefix']) or '..' in out['prefix']: raise ValueError('Use a plain object prefix')
    return out


def re_full(pattern,value):
    import re
    return isinstance(value,str) and re.fullmatch(pattern,value)


def save_host(studio,expected,values,secret_values,*,keychain=None):
    cfg=host_config(values);keychain=keychain or Keychain();sp=studio.sp;path=P.safe_path(sp,directory(sp)/'host.json')
    if set(secret_values)!={'access_key','secret_key'} or any(not isinstance(v,str) or not v or len(v)>16000 for v in secret_values.values()): raise ValueError('Enter both host keys')
    with connection_lock(sp):
        if digest(read(path))!=expected: raise ValueError('Host settings changed; reopen Hosting')
        doc={'config':cfg,'stored':[],'state':'updating','ref':'host_'+digest(str(sp.root.resolve()))[:24]}
        atomic(path,doc)
        try:
            for k,v in secret_values.items():keychain.set(doc['ref']+'_'+k,v);doc['stored'].append(k);atomic(path,doc)
            doc['state']='configured_unverified'
        except Exception:doc['state']='partial_update'
        atomic(path,doc)
        return {'state':doc['state'],'message':'Settings stored. No bucket was created and no upload or paid probe was performed.'}


def remove_host(studio,expected,*,keychain=None):
    keychain=keychain or Keychain();sp=studio.sp;path=P.safe_path(sp,directory(sp)/'host.json')
    with connection_lock(sp):
        doc=read(path)
        if digest(doc)!=expected:raise ValueError('Host settings changed; reopen Hosting')
        # Disable first so a partial delete cannot leave the sender using it.
        doc['state']='removing';atomic(path,doc)
        try:
            for k in ('access_key','secret_key'):
                if doc.get('ref'):keychain.remove(doc['ref']+'_'+k)
            doc.update(state='removed',stored=[])
        except Exception:doc['state']='partial_removal'
        atomic(path,doc)
        return {'state':doc['state'],'message':'Host keys removed; public-media cleanup records remain.' if doc['state']=='removed' else 'Keychain refused part of removal. Host use is disabled; retry Remove host keys.'}


def load_workspace_host(sp,*,keychain=None):
    doc=read(P.safe_path(sp,directory(sp)/'host.json'))
    if doc.get('state')!='configured_unverified':return None
    from ..platforms.host import S3Host
    keychain=keychain or Keychain();ak=keychain.get(doc['ref']+'_access_key');sk=keychain.get(doc['ref']+'_secret_key')
    return S3Host(**host_config(doc['config']),access_key=ak,secret_key=sk) if ak and sk else None


def renew_due_sessions(sp,now):
    """Only the already-authorized sender calls this; never an authoring read.
    Refresh the documented session products at most once per 30 minutes, and
    only when this workspace has a due automatic variant for that account.
    """
    from . import store,model
    from .clock import parse_ts
    from .service import open_studio
    accounts=set()
    for p in store.list_posts(sp):
        for v in p.get('variants',[]):
            if v.get('dropped') or (v.get('delivery') or {}).get('method')=='manual':continue
            stamp=(v.get('schedule') or {}).get('utc')
            if stamp and parse_ts(stamp)<=now:accounts.add(v['account'])
    studio=None
    for aid in accounts:
        a=load_account(sp,aid)
        if a.platform not in ('bluesky','youtube') or not a.connection.get('root_revision') or not a.live_ok() or a.paused():continue
        doc=read(record_path(sp,aid));stamp=doc.get('session_renewed_at')
        if stamp and 0<=(now-parse_ts(stamp)).total_seconds()<1800:continue
        if studio is None:studio=open_studio(sp.root,publishers={},host=None)
        connect_session(studio,aid,revision(sp,aid))
