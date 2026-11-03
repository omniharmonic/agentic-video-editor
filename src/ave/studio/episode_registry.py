"""Native-selected episode grants. The verified control ledger is authoritative.

Bookmarks confer scoped filesystem access, never ownership or editorial approval.
HTTP consumers accept opaque IDs only. NativeBooks is replaceable only by Python
fixture injection; there is no environment/protocol switch for simulated grants.
"""
from __future__ import annotations
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
from ..episode import Episode, resolve_layout, MEDIA_EXTS, _ignored
from ..publish.safety import sha256_file
from . import seal, ledger, production
from .clock import iso_utc

ID = re.compile(r'^episode_[a-f0-9]{24}$')
KINDS = ('work', 'edits', 'captions', 'exports', 'gfx', 'music', 'publish')


def policy():
    import yaml
    from ..resources import home
    return yaml.safe_load((home()/'config'/'episode-workshop.yaml').read_text())


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def regular(path, *, directory=False):
    path = Path(os.path.abspath(path))
    for p in (path, *path.parents):
        if p.is_symlink(): raise ValueError('Episode paths cannot contain symlinks: '+str(p))
    if path.exists() and not (path.is_dir() if directory else path.is_file()): raise ValueError('Expected a regular episode '+('folder' if directory else 'file'))
    if not directory and path.exists() and path.stat().st_nlink > 1: raise ValueError('Hard-linked episode files require an independent copy')
    return path


def project_root(chosen, pick):
    """The episode a person meant by the folder they chose: the folder itself, or one of the projects directly inside it.

    Symlinks above the chosen folder are the system's (macOS has several), so the real path is what gets checked and stored."""
    root = Path(os.path.realpath(chosen))
    if (root / 'episode.json').is_file():
        return root
    folders = sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink() and (p / 'episode.json').is_file())
    if not folders:
        raise ValueError('No project found here. Choose a folder containing episode.json, or a folder of projects.')
    return folders[0] if len(folders) == 1 else pick(folders)


def json_file(path):
    path=regular(path)
    if path.stat().st_size > 32*1024*1024: raise ValueError('Episode metadata exceeds 32 MB')
    return json.loads(path.read_text())


class NativeBooks:
    @staticmethod
    def identity(url):
        import Foundation as F
        values,error=url.resourceValuesForKeys_error_([F.NSURLVolumeUUIDStringKey,F.NSURLFileResourceIdentifierKey],None)
        if error or not values: raise ValueError('Cannot identify selected volume and folder')
        ident={'volume':str(values[F.NSURLVolumeUUIDStringKey]),'resource':str(values[F.NSURLFileResourceIdentifierKey])}
        if any(v in ('None','') for v in ident.values()): raise ValueError('Volume does not supply durable folder identity')
        return ident

    def capture(self,path):
        import Foundation as F
        url=F.NSURL.fileURLWithPath_(str(path))
        data,error=url.bookmarkDataWithOptions_includingResourceValuesForKeys_relativeToURL_error_(F.NSURLBookmarkCreationWithSecurityScope,None,None,None)
        if error or data is None: raise ValueError('Cannot save scoped episode bookmark')
        return {'bookmark':base64.b64encode(bytes(data)).decode(),'identity':self.identity(url)}

    @contextmanager
    def access(self,binding):
        import Foundation as F
        data=F.NSData.dataWithBytes_length_(base64.b64decode(binding['bookmark']),len(base64.b64decode(binding['bookmark'])))
        url,stale,error=F.NSURL.URLByResolvingBookmarkData_options_relativeToURL_bookmarkDataIsStale_error_(data,F.NSURLBookmarkResolutionWithSecurityScope|F.NSURLBookmarkResolutionWithoutUI,None,None,None)
        if error or not url or stale: raise ValueError('Episode offline or bookmark stale; reconnect this folder in the native picker')
        if self.identity(url)!=binding['identity']: raise ValueError('Episode root identity changed; reconnect required')
        held=url.startAccessingSecurityScopedResource()
        try:
            path=regular(Path(str(url.path())),directory=True)
            if not path.is_dir(): raise ValueError('Episode is offline; reconnect its original volume')
            yield path
        finally:
            if held:url.stopAccessingSecurityScopedResource()


def control(sp):
    from . import episode_worker_bridge as bridge
    if bridge.CLIENT is not None:return bridge.RemoteControl()
    from .workbench import safe
    for p in (sp.ledger_dir/'control.jsonl',sp.ledger_dir/'.control.head',sp.ledger_dir/'.lock'):
        safe(sp,p)
    problems=seal.verify_all(sp)
    if problems: raise ValueError('Protected ledger verification failed: '+'; '.join(problems))
    return seal.log(sp,'control')


def records(sp):
    rows={}
    for row in control(sp).rows():
        if row.get('schema')=='registered_episode.v1' and row.get('sig') and row.get('workspace')==str(sp.root): rows[row['id']]=row
    return rows


def get(sp,eid,revision=None):
    if not isinstance(eid,str) or not ID.fullmatch(eid): raise ValueError('Choose a registered episode')
    row=records(sp).get(eid)
    if not row: raise ValueError('Episode is not registered in this workspace')
    if row.get('revoked'): raise ValueError('Episode access revoked; reconnect in the native picker')
    if revision is not None and revision!=row['grant_revision']: raise ValueError('Episode grant changed; refresh and retry')
    return row


def layout_paths(root,meta,additional=None):
    spec=resolve_layout(meta.get('layout'));additional=additional or {}
    paths={}
    for key in (*KINDS,'raw_dirs'):
        values=spec[key] if key=='raw_dirs' else [spec[key]]
        if not isinstance(values,list) or not 1<=len(values)<=16: raise ValueError('Invalid episode layout')
        paths[key]=[]
        for value in values:
            if not isinstance(value,str) or not value or len(value)>500 or '\x00' in value: raise ValueError('Invalid '+key+' layout path')
            path=regular(root/value,directory=True)
            if path==root: raise ValueError('Layout roots cannot be the episode root')
            if not path.is_relative_to(root):
                if key=='raw_dirs': raise ValueError('External raw folders require choosing their enclosing episode root')
                if key not in additional or Path(additional[key])!=path: raise ValueError('Additional native folder grant required: '+key+' ('+str(path)+')')
            paths[key].append(path)
    raw=paths['raw_dirs']
    for key in KINDS:
        for path in paths[key]:
            if any(path==r or path in r.parents or r in path.parents for r in raw): raise ValueError('Generated '+key+' overlaps protected raw media')
    # The legacy layout may intentionally nest gfx/music under work. Other
    # generated aliases would let stage sidecars overwrite another stage.
    for a in ('work','edits','captions','exports','publish'):
        for b in ('work','edits','captions','exports','publish'):
            if a<b and (paths[a][0]==paths[b][0] or paths[a][0] in paths[b][0].parents or paths[b][0] in paths[a][0].parents): raise ValueError('Generated layout roots overlap: '+a+' / '+b)
    return spec,paths


def scan(root,paths):
    rows=[]
    for raw in paths['raw_dirs']:
        if not raw.is_dir(): continue
        for base,dirs,files in os.walk(raw,followlinks=False):
            for name in dirs+files:
                p=Path(base)/name
                if _ignored(p.relative_to(raw).parts): continue
                regular(p,directory=name in dirs)
                if name in files and p.suffix.lower() in MEDIA_EXTS:
                    rows.append({'id':'source_'+digest(p.relative_to(root).as_posix())[:24],'file':p.relative_to(root).as_posix(),'sha256':sha256_file(p),'size':p.stat().st_size})
                    if len(rows)>policy()['max_sources']: raise ValueError('Episode intake supports at most 200 sources')
            dirs[:]=[n for n in dirs if not _ignored((n,))]
    if not rows: raise ValueError('Episode contains no supported recordings')
    return sorted(rows,key=lambda r:r['file'])


def register_selected(studio,root,*,derived_consent,books=None,additional=None,reconnect=None):
    """Native caller only: selected path + separately displayed write consent."""
    if derived_consent is not True: raise ValueError('Native derived-write consent is required')
    books=books or NativeBooks();root=regular(root,directory=True)
    regular(root/'episode.json.tmp')
    meta=json_file(root/'episode.json');spec,paths=layout_paths(root,meta,additional)
    sources=scan(root,paths);binding=books.capture(root)
    extra={k:books.capture(p) for k,p in (additional or {}).items()}
    with ledger.ledger_lock(studio.sp):
        prior=records(studio.sp).get(reconnect) if reconnect else None
        if reconnect and prior is None:raise ValueError('Unknown episode reconnect identity')
        if prior and (prior['source_fingerprint']!=digest(sources) or prior['binding']['identity']!=binding['identity']): raise ValueError('Selected folder is not the registered episode; source/root identity differs')
        row={'schema':'registered_episode.v1','id':prior['id'] if prior else 'episode_'+secrets.token_hex(12),'workspace':str(studio.sp.root),
             'ownership':'external_reference','label':str(meta.get('title') or root.name)[:200],'root_hint':str(root),'binding':binding,'additional':extra,
             'layout':spec,'sources':sources,'source_fingerprint':digest(sources),'grant_revision':prior['grant_revision']+1 if prior else 1,
             'derived_consent':True,'at':iso_utc(studio.now()),'prior':prior['sig'] if prior else None}
        return control(studio.sp).append_locked(row)


class GrantedEpisode(Episode):
    def __init__(self, root):
        super().__init__(root)
        from ..audio import REGISTERED_ANALYSIS_CONTEXT
        self.analysis_context = REGISTERED_ANALYSIS_CONTEXT
    def rel(self,path): return os.path.relpath(path,self.root).replace(os.sep,'/') if Path(path).is_absolute() else str(path)
    def mark_stage(self,stage,**info):
        # Core engines report to derived storage; registering never adopts or
        # overwrites root PROGRESS/AGENTS or editable historical authority.
        from ..episode import write_json
        write_json(self.work/'app-stages'/f'{stage}.json',{'generated':True,**info})


# Raw-byte verification is reusable only while this exact synchronous operation
# owns the scope. It carries no review, license or approval authority.
from contextvars import ContextVar
import threading
_VERIFIED_SOURCES=ContextVar('ave_verified_episode_sources',default=None)


def _scope_key(studio_root,ep,row):
    return (str(studio_root),row['id'],row['grant_revision'],str(ep.root),digest(row['binding']),digest(row['layout']),row['source_fingerprint'],digest(row))


def _source_stats(ep,row):
    result={}
    expected={str(regular(ep.root/s['file'])) for s in row['sources']};found=set()
    for root in ep.raw_dirs:
        for base,dirs,files in os.walk(root,followlinks=False):
            dirs[:]=[n for n in dirs if not _ignored((n,))]
            for name in files:
                p=Path(base)/name
                if not _ignored(p.relative_to(root).parts) and p.suffix.lower() in MEDIA_EXTS:found.add(str(regular(p)))
    if found!=expected:raise ValueError('Registered source inventory changed during operation')
    for source in row['sources']:
        path=regular(ep.root/source['file']);stat=path.stat()
        if stat.st_size!=source['size']:raise ValueError('Registered source size changed: '+source['file'])
        result[str(path)]=(stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)
    return result


def _scope_matches(scope,ep,row):
    return bool(scope and scope['owner']==(os.getpid(),threading.get_ident()) and scope['key']==_scope_key(row['workspace'],ep,row))


def _verified_raw_sha(ep,row,path):
    scope=_VERIFIED_SOURCES.get();path=regular(path)
    if _scope_matches(scope,ep,row) and str(path) in scope['digests']:
        if _source_stats(ep,row)!=scope['stats']:raise ValueError('Registered source changed during verified operation')
        return scope['digests'][str(path)]
    return sha256_file(path)


def _owned_stat(path):
    stat=regular(path).stat()
    return (stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)


def _owned_digest(path,expected,stat):
    from ..publish import safety
    if _owned_stat(path)!=stat:raise ValueError('Owned provenance path changed during verification')
    actual=safety.sha256_file(path)
    if actual!=expected or _owned_stat(path)!=stat:raise ValueError('Owned provenance bytes changed during verification')
    return actual


def _verified_owned_provenance_sha(studio,path,expected):
    """Two episode provenance consumers only; records and rights stay fresh.

    Enroll an exact owned path on its first full read. Successful outer source
    scope exit independently hashes every enrolled path before any publication.
    This is deliberately not a general hashing or library-authority cache.
    """
    from .workbench import safe
    from ..publish import safety
    path=regular(safe(studio.sp,path))
    if not path.is_relative_to(studio.sp.library_dir/'assets'):raise ValueError('Expected an owned library provenance file')
    scope=_VERIFIED_SOURCES.get()
    if not scope or scope['owner']!=(os.getpid(),threading.get_ident()) or scope['key'][0]!=str(studio.sp.root):
        return safety.sha256_file(path)
    key=str(path);stat=_owned_stat(path);prior=scope['owned'].get(key)
    if prior:
        if prior['sha256']!=expected or prior['stat']!=stat:raise ValueError('Owned provenance identity changed during operation')
        return prior['sha256']
    actual=_owned_digest(path,expected,stat)
    scope['owned'][key]={'sha256':expected,'stat':stat}
    return actual


@contextmanager
def _source_verification(studio,ep,row,paths,*,fresh=False):
    current=_VERIFIED_SOURCES.get()
    if not fresh and _scope_matches(current,ep,row):
        if _source_stats(ep,row)!=current['stats']:raise ValueError('Registered source changed during nested operation')
        yield
        get(studio.sp,row['id'],row['grant_revision'])
        if _source_stats(ep,row)!=current['stats']:raise ValueError('Registered source changed during nested operation')
        return
    before=_source_stats(ep,row);actual=scan(ep.root,paths)
    if digest(actual)!=row['source_fingerprint'] or before!=_source_stats(ep,row):raise ValueError('Registered source inventory changed; reconnect and inspect the originals')
    scope={'owner':(os.getpid(),threading.get_ident()),'key':_scope_key(studio.sp.root,ep,row),'stats':before,'owned':{},
           'digests':{str(regular(ep.root/s['file'])):s['sha256'] for s in actual}}
    token=_VERIFIED_SOURCES.set(scope)
    try:
        yield
        get(studio.sp,row['id'],row['grant_revision'])
        # Never trust a stat-only check at the outer operation boundary.
        if digest(scan(ep.root,paths))!=row['source_fingerprint']:raise ValueError('Registered originals changed during the operation; completed output is unreviewed')
        for path,item in scope['owned'].items():_owned_digest(Path(path),item['sha256'],item['stat'])
    finally:_VERIFIED_SOURCES.reset(token)


def assert_sources(ep,row,ids):
    wanted=set(ids);known={s['id']:s for s in row['sources']}
    if wanted-set(known):raise ValueError('Unknown registered source identity')
    for sid in wanted:
        source=known[sid];path=regular(ep.root/source['file'])
        if not path.is_file() or path.stat().st_size!=source['size'] or _verified_raw_sha(ep,row,path)!=source['sha256']:
            raise ValueError('Registered source changed or offline: '+source['file'])


@contextmanager
def resolve(studio,eid,revision=None,*,books=None,verify_sources=True,_fresh_sources=False):
    from contextlib import ExitStack
    row=get(studio.sp,eid,revision);books=books or NativeBooks()
    from . import episode_worker_bridge as bridge
    remote=bridge.CLIENT.call('snapshot') if bridge.CLIENT is not None else None
    with ExitStack() as stack:
        if remote is not None:
            if not remote.get('root'):raise ValueError('This worker has no episode path grant')
            root=regular(Path(remote['root']),directory=True)
        elif row['ownership']=='managed_copy':
            from .video_workshop import safe_dir
            root=regular(safe_dir(studio.sp,studio.sp.root/row['root_hint']),directory=True)
            if not root.is_dir() or [root.stat().st_dev,root.stat().st_ino]!=row['binding']['identity']: raise ValueError('Managed episode root was replaced or removed')
        else: root=stack.enter_context(books.access(row['binding']))
        additional={k:Path(v) for k,v in remote['additional'].items()} if remote is not None else {k:stack.enter_context(books.access(v)) for k,v in row.get('additional',{}).items()}
        regular(root/'episode.json.tmp')
        meta=json_file(root/'episode.json');spec,paths=layout_paths(root,meta,additional)
        if spec!=row['layout']: raise ValueError('Episode layout changed; reconnect to grant its new destinations')
        for key in KINDS:
            for p in paths[key]:
                for base,dirs,files in os.walk(p,followlinks=False):
                    for name in dirs+files: regular(Path(base)/name,directory=name in dirs)
        ep=GrantedEpisode(root)
        if verify_sources:stack.enter_context(_source_verification(studio,ep,row,paths,fresh=_fresh_sources))
        yield ep,row
        get(studio.sp,eid,row['grant_revision'])



def catalog(studio,*,books=None):
    result=[]
    for eid,row in records(studio.sp).items():
        item={k:row[k] for k in ('id','label','ownership','grant_revision')};item['status']='available'
        try:
            with resolve(studio,eid,books=books,verify_sources=False): pass
        except (ValueError,OSError) as exc:item.update(status='blocked',reason=str(exc))
        result.append(item)
    return {'episodes':result}


def revoke(studio,eid):
    with ledger.ledger_lock(studio.sp):
        row=get(studio.sp,eid)
        return control(studio.sp).append_locked({**{k:v for k,v in row.items() if k not in ('sig','seq','prev')},'revoked':True,'grant_revision':row['grant_revision']+1,'prior':row['sig'],'at':iso_utc(studio.now())})
