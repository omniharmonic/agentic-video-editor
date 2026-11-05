"""Local authoring facade. Remote transport never imports this module."""
from __future__ import annotations
import copy
import secrets
import time
from pathlib import Path
from .. import collab as C
from .. import collab_v2 as V
from .. import collab_transport as T
from . import model,store
from .events import Actor


class Collaboration:
    def __init__(self,studio,*,identity_factory=None,secrets_factory=None):
        self.studio=studio;self.sp=studio.sp;self.exchange=None;self.views={};self.error=None
        self.identity_factory=identity_factory;self.secrets_factory=secrets_factory
        self.config_path=C._home(self.sp)/'exchange-local.json' if self.sp else None
        self.config=C._read(self.config_path) if self.config_path and self.config_path.exists() else None
    def close(self):
        if self.exchange:self.exchange.stop()
        self.views.clear()
    def enable_native(self,directory=None):
        from ..collab_identity import MacIdentity
        from ..collab_tls import Keychain
        if not self.sp:raise ValueError('Choose a local studio')
        if directory is None:
            if not self.config:raise ValueError('Choose an exchange directory in the native sheet')
            directory=self.config['directory']
        with C._locked(self.sp) as home:
            state=C._state(self.sp,create=True);C._atomic(home/'state.json',state)
        exchange_store=T.ExchangeStore(directory,state['studio'],excluded=[self.sp.root,*self.sp.raw_dirs])
        identity=(self.identity_factory or MacIdentity)();credentials=(self.secrets_factory or Keychain)()
        if self.exchange:self.exchange.stop()
        self.exchange=T.Exchange(exchange_store,identity,credentials)
        self.exchange.on_pair=self.bind_peer
        self.exchange.start()
        self.config={'v':1,'directory':str(C._no_symlink(Path(directory))),'studio':state['studio']};C._atomic(self.config_path,self.config)
        return {'state':'enabled'}
    def bind_peer(self,descriptor):
        with C._locked(self.sp) as home:
            state=C._state(self.sp,create=True);name='peer_'+descriptor['studio']
            existing=state['peers'].get(name)
            if existing and existing['studio']!=descriptor['studio']:raise ValueError('Existing editorial peer binding changed')
            state['peers'].setdefault(name,{'studio':descriptor['studio'],'base':{}})
            C._atomic(home/'state.json',state)
    def require_exchange(self):
        if not self.exchange:raise ValueError('Enable sharing in the native app')
        return self.exchange
    def snapshot(self):
        ex=self.exchange;state=ex.store.state if ex else None
        if state is None and self.config:
            path=C._no_symlink(Path(self.config['directory'])/self.config['studio']/'transport.json')
            if path.exists():state=C._read(path)
        peer=(state or {}).get('peer');inbox=[]
        if self.config:
            directory=C._no_symlink(Path(self.config['directory'])/self.config['studio']/'inbox')
            for path in sorted(directory.glob('*.receipt.json'))[-T.MAX_PENDING:]:
                item=C._read(path);local=C._home(self.sp)/'inbox-reviews'/(item['transport_sha256']+'.json')
                review=C._read(local) if local.exists() else {}
                inbox.append({**item,'review_state':review.get('state','received')})
        pairing=None
        if ex:
            transcript=(ex.pending or {}).get('transcript') or (ex.joining or {}).get('transcript')
            if transcript:
                other=transcript['joiner'] if ex.pending else transcript['inviter']
                pairing={'hash':C._sha(transcript),'code':T.comparison(transcript),'peer':other,'expires':((ex.pending or {}).get('invite') or ex.joining['invite'])['expires']}
        return {'enabled':bool(ex and ex.enabled),'peer':peer['descriptor'] if peer else None,'pairing':pairing,
                'outbox':list((state or {}).get('outbox',{}).values()),'inbox':inbox,'error':self.error or (ex.failure if ex else None),
                'interface':ex.local_descriptor()['endpoint'][0] if ex and ex.enabled else None,
                'scope':'Editorial drafts and selected visual revisions. Footage and other large files reach the people on a project through its sync, not here.',
                'posts':[{'id':pid,'text':content['copy'].get('base','')} for pid,content in C._load_posts(self.sp)] if self.sp else [],
                'conflicts':[{k:v for k,v in r.items() if k!='file'} for r in C.status(self.sp)['conflicts']] if self.sp else []}
    def _path(self,digest):
        T.sha(digest)
        if not self.config:raise ValueError('No exchange folder configured')
        path=C._no_symlink(Path(self.config['directory'])/self.config['studio']/'inbox'/(digest+'.json'))
        receipt=C._read(path.with_suffix('.receipt.json'))
        if receipt['transport_sha256']!=digest or C.sha256_file(path)!=digest:raise ValueError('Received bytes changed')
        return path,receipt
    def _revisions(self,bundle):
        out={}
        for entry in bundle['entries']:
            try:out[entry['id']]=store.load_post(self.sp,entry['id'])['rev']
            except FileNotFoundError:out[entry['id']]=None
        return out
    def preview(self,digest):
        path,receipt=self._path(digest);bundle=C._read(path);peer='peer_'+receipt['source']
        result=C.import_bundle(self.sp,path,peer=peer)
        token=secrets.token_hex(32);self.views[token]={'kind':'incoming','digest':digest,'revisions':self._revisions(bundle),'expires':time.time()+600}
        self._prune()
        comparisons=[]
        for entry in bundle['entries']:
            try:current=store.load_post(self.sp,entry['id'])
            except FileNotFoundError:current=None
            comparisons.append({'post':entry['id'],'base':entry['base']['content'],'local':C.editorial(current) if current else None,'incoming':entry['content'],'revision':current['rev'] if current else None,'protected':bool(current and C._operational(current))})
        return {'token':token,'digest':digest,'result':result,'comparisons':comparisons,'visuals':bundle.get('visuals',[])}
    def _prune(self):
        self.views={k:v for k,v in self.views.items() if v['expires']>time.time()}
        if len(self.views)>64:raise ValueError('Too many open collaboration previews')
    def apply(self,token):
        self._prune();view=self.views.get(token)
        if not view or view['kind']!='incoming':raise ValueError('Preview this exact bundle before applying')
        path,receipt=self._path(view['digest']);bundle=C._read(path)
        if self._revisions(bundle)!=view['revisions']:raise ValueError('Local revisions changed; preview again')
        result=C.import_bundle(self.sp,path,peer='peer_'+receipt['source'],apply=True)
        C._atomic(C._home(self.sp)/'inbox-reviews'/(view['digest']+'.json'),{'state':'conflicts' if result['conflicts'] else 'applied','bundle':bundle['id']})
        self.views.pop(token,None);return result
    def prepare_send(self,body):
        from .production import only
        only(body,{'posts','visuals','media_ids'})
        ex=self.require_exchange();peer=ex.store.state['peer']
        if not peer:raise ValueError('Pair a collaborator first')
        ids=body.get('posts',[])
        if not isinstance(ids,list) or not 1<=len(ids)<=C.MAX_POSTS or len(set(ids))!=len(ids):raise ValueError('Select1–500 unique drafts')
        rows=C._load_posts(self.sp,ids);entries=[{'id':pid,'content':content} for pid,content in rows]
        extension=V.extension(self.sp,entries,ex.store.studio,visuals=body.get('visuals',[]),media_ids=body.get('media_ids',[]))
        token=secrets.token_hex(32);self.views[token]={'kind':'send','selection':copy.deepcopy(body),'contents':rows,'extension':extension,'peer':peer['descriptor'],'expires':time.time()+600};self._prune()
        return {'token':token,'drafts':[{'id':pid,'content':content} for pid,content in rows],'manifests':extension['manifests'],'visuals':extension['visuals'],'media_bytes':sum(len(__import__('base64').b64decode(o['data'])) for o in extension['objects']),'peer':peer['descriptor']}
    def send(self,token):
        self._prune();view=self.views.get(token)
        if not view or view['kind']!='send':raise ValueError('Preview exact outgoing scope first')
        ex=self.require_exchange();peer=ex.store.state['peer']
        if not peer or peer['descriptor']!=view['peer']:raise ValueError('Pairing changed; preview again')
        body=view['selection'];rows=C._load_posts(self.sp,body['posts'])
        if rows!=view['contents'] or V.extension(self.sp,[{'id':p,'content':c} for p,c in rows],ex.store.studio,visuals=body.get('visuals',[]),media_ids=body.get('media_ids',[]))!=view['extension']:raise ValueError('Selected work changed; preview again')
        self.bind_peer(peer['descriptor'])
        path=C._no_symlink(ex.store.root/'partial'/(secrets.token_hex(32)+'.json'))
        try:
            C.export_bundle(self.sp,path,peer='peer_'+peer['descriptor']['studio'],post_ids=body['posts'],version=2,visuals=body.get('visuals',[]),media_ids=body.get('media_ids',[]))
            result=ex.enqueue(path);self.views.pop(token,None);return result
        finally:path.unlink(missing_ok=True)
    def conflict(self,cid):
        model.check_id(cid);obj=C._read(C._inside(self.sp,C._home(self.sp)/'conflicts'/(cid+'.json')))
        current=store.load_post(self.sp,obj['post'])
        return {**obj,'current':C.editorial(current),'current_rev':current['rev'],'protected':C._operational(current)}
    def resolve(self,body):
        from .production import only
        only(body,{'id','take','expected_rev','text'})
        current=self.conflict(body['id'])
        if current['current_rev']!=body['expected_rev']:raise ValueError('Local revision changed; review again')
        if body['take']=='separate':
            pid='p_received_'+secrets.token_hex(10);draft=C._draft(pid,current['incoming'])
            result=store.save_post(self.sp,draft,expected_rev=None,actor=Actor('human','local collaborator'),op='collaboration.separate',detail={'conflict':body['id']})
            C.resolve_conflict(self.sp,body['id'],take='local',expected_rev=body['expected_rev'],apply=True)
            return {'created':result['id']}
        if body['take']=='merge':
            if current['protected']:raise ValueError('Create a separate draft to preserve publishing history')
            if not isinstance(body.get('text'),str) or len(body['text'])>20000:raise ValueError('Bounded merged copy required')
            post=store.load_post(self.sp,current['post']);post['copy']['base']=body['text']
            saved=store.save_post(self.sp,post,expected_rev=body['expected_rev'],actor=Actor('human','local collaborator'),op='collaboration.merge',detail={'conflict':body['id']})
            return C.resolve_conflict(self.sp,body['id'],take='local',expected_rev=saved['rev'],apply=True)
        return C.resolve_conflict(self.sp,body['id'],take=body['take'],expected_rev=body['expected_rev'],apply=True)
    def promote(self,digest,aid):
        from ..library.imports import promote
        path,receipt=self._path(digest);bundle=C._read(path);C._validate_bundle(bundle);V.check_local(self.sp,bundle)
        m=next((m for m in bundle.get('manifests',[]) if m['id']==aid),None)
        if not m:raise ValueError('Received manifest unavailable')
        source=C._inside(self.sp,C._home(self.sp)/'media'/(m['sha256']+m['ext']))
        return promote(self.sp,m,source,receipt=receipt['bundle_id'])
