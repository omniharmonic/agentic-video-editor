"""Additive bounded visual/occurrence bundle contract, without imported authority."""
from __future__ import annotations
import base64
import copy
import hashlib
from . import collab as C
from . import collab_origins as O
from .content import visual_snapshot as V
from .library import registry

MAX_ENTRIES=1000


def referenced(entries):
    return {m['asset'] for e in entries for m in e['content']['media']+[m for v in e['content']['variants'] for m in v['media'] or []]}


def manifest(rec, known, namespace):
    out={'id':rec['id'],'sha256':rec['sha256'],'ext':rec['ext'],'bytes':rec['bytes'],'kind':rec['kind'],'namespace':namespace,'parents':[]}
    if rec.get('derivation'):
        parents=[]
        for aid in rec['derived_from']:
            parent=known.get(aid)
            if not parent: raise ValueError('Occurrence parent record is missing: '+aid)
            parents.append({'id':aid,'sha256':parent['sha256']})
            out['parents'].append({'id':aid,'identity_sha256':(parent.get('derivation') or {}).get('sha256',parent['sha256'])})
        out['occurrence']={'schema':'derivative_occurrence.v1','output_sha256':rec['sha256'],'parents':parents,'origin':copy.deepcopy(rec['origin'])}
        out['derivation_sha256']=rec['derivation']['sha256']
    validate_manifest(out)
    return out


def validate_manifest(m):
    base={'id','sha256','ext','bytes','kind','namespace','parents'}
    if not isinstance(m,dict) or set(m) not in (base,base|{'occurrence','derivation_sha256'}): raise ValueError('Invalid asset identity manifest')
    O.asset(m['id']);O.sha(m['sha256'])
    if not isinstance(m['namespace'],str) or not C._UUID.fullmatch(m['namespace']): raise ValueError('Invalid original studio namespace')
    if m['ext'] not in registry.KIND_BY_EXT or registry.KIND_BY_EXT[m['ext']]!=m['kind']: raise ValueError('Invalid media descriptor')
    O.number(m['bytes'],0,2**53,'Byte size',True)
    if not isinstance(m['parents'],list) or len(m['parents'])>500: raise ValueError('Too many parent anchors')
    for p in m['parents']:
        O.exact(p,('id','identity_sha256'));O.asset(p['id']);O.sha(p['identity_sha256'])
        if p['id']!='a_'+p['identity_sha256'][:12]: raise ValueError('Parent full identity mismatch')
    if m['parents']!=sorted(m['parents'],key=lambda x:x['id']) or len({p['id'] for p in m['parents']})!=len(m['parents']): raise ValueError('Parent anchors must be normalized')
    if 'occurrence' not in m:
        if m['id']!='a_'+m['sha256'][:12] or m['parents']: raise ValueError('Ordinary asset identity mismatch')
        return
    o=m['occurrence'];O.exact(o,('schema','output_sha256','parents','origin'))
    if o['schema']!='derivative_occurrence.v1' or o['output_sha256']!=m['sha256'] or len(C._canonical(o))>1_000_000: raise ValueError('Invalid occurrence preimage')
    if not isinstance(o['parents'],list) or len(o['parents'])>500: raise ValueError('Invalid occurrence parents')
    for p in o['parents']: O.exact(p,('id','sha256'));O.asset(p['id']);O.sha(p['sha256'])
    if o['parents']!=sorted(o['parents'],key=lambda x:x['id']) or [p['id'] for p in o['parents']]!=[p['id'] for p in m['parents']]: raise ValueError('Occurrence parent order/anchors mismatch')
    O.validate(o['origin'],o['parents']);O.sha(m['derivation_sha256'])
    if C._sha(o)!=m['derivation_sha256'] or m['id']!='a_'+m['derivation_sha256'][:12]: raise ValueError('Occurrence relationship hash mismatch')


def identity(m): return m.get('derivation_sha256',m['sha256'])


def extension(sp, entries, namespace, *, visuals=(), media_ids=()):
    known=registry.read_all(sp);snapshots=[];selected=referenced(entries)
    for d in visuals:
        O.exact(d,('id','version'))
        snapshot=V.export(sp,d['id'],d['version'])
        snapshots.append({'namespace':namespace,'snapshot':snapshot})
        selected.update(a['id'] for a in snapshot['assets'])
    if len(selected)+len(snapshots)>MAX_ENTRIES: raise ValueError('Too many selected identities')
    manifests=[]
    for aid in sorted(selected):
        rec=known.get(aid)
        if rec is None: continue
        # A relayed record keeps its original namespace and exact imported preimage.
        imported=rec.get('untrusted_import')
        manifests.append(copy.deepcopy(imported['manifest']) if imported else manifest(rec,known,namespace))
    requested=set(media_ids)
    if not requested<=selected: raise ValueError('Select byte objects only for explicitly referenced assets')
    objects=[];seen=set();total=0
    for m in manifests:
        if m['id'] not in requested or m['sha256'] in seen: continue
        path=C._inside(sp,registry.asset_file(sp,known[m['id']]))
        if not path.is_relative_to(sp.library_dir/'assets'): raise ValueError('Only registered library byte objects may leave')
        if path.stat().st_size!=m['bytes'] or total+m['bytes']>C.MAX_MEDIA_BYTES: raise ValueError('Selected bytes exceed 20 MiB; large media sharing is not yet available')
        data=path.read_bytes();total+=len(data)
        if hashlib.sha256(data).hexdigest()!=m['sha256']: raise ValueError('Registered bytes changed')
        objects.append({'sha256':m['sha256'],'data':base64.b64encode(data).decode('ascii')});seen.add(m['sha256'])
    return {'manifests':manifests,'objects':objects,'visuals':snapshots}


def validate(bundle):
    """Validate the complete extension before any local/studio mutation."""
    if set(bundle)!={'v','kind','id','source','target','entries','assets','manifests','objects','visuals'} or bundle['assets']: raise ValueError('Invalid v2 envelope')
    projected={k:copy.deepcopy(bundle[k]) for k in ('kind','source','target','entries','assets')};projected['v']=1;projected['id']=C._sha(projected)
    C._validate_bundle(projected)
    if C._sha({k:v for k,v in bundle.items() if k!='id'})!=bundle['id']: raise ValueError('Bundle checksum mismatch')
    for key in ('manifests','objects','visuals'):
        if not isinstance(bundle[key],list): raise ValueError('Invalid extension list')
    if sum(len(bundle[k]) for k in ('entries','manifests','objects','visuals'))>MAX_ENTRIES: raise ValueError('Shared bundle entry budget exceeded')
    manifests={};docs={};refs=referenced(bundle['entries'])
    for wrapper in bundle['visuals']:
        O.exact(wrapper,('namespace','snapshot'))
        if not isinstance(wrapper['namespace'],str) or not C._UUID.fullmatch(wrapper['namespace']): raise ValueError('Invalid visual namespace')
        s=V.validate(wrapper['snapshot']);key=(wrapper['namespace'],s['document']['id'],s['document']['version'])
        if key in docs: raise ValueError('Duplicate visual document revision')
        docs[key]=s;refs.update(a['id'] for a in s['assets'])
    for m in bundle['manifests']:
        validate_manifest(m)
        if m['id'] in manifests: raise ValueError('Duplicate or colliding asset identity')
        if m['id'] not in refs: raise ValueError('Unselected asset manifest')
        manifests[m['id']]=m
    for m in manifests.values():
        for p,anchor in zip(m.get('occurrence',{}).get('parents',[]),m['parents']):
            other=manifests.get(p['id'])
            if other and (other['sha256']!=p['sha256'] or identity(other)!=anchor['identity_sha256']): raise ValueError('Parent full identity mismatch')
        origin=m.get('occurrence',{}).get('origin',{})
        if 'content_doc' in origin:
            s=docs.get((m['namespace'],origin['content_doc'],origin['version']))
            if s:
                rs=origin['render_snapshot'];vs=rs['visual_snapshot']
                if s['document']['sha256']!=origin['doc_sha256'] or s['identities']['fonts']!=vs['fonts'] or s['identities']['template_sha256']!=vs['template_sha256'] or s['identities']['brand_sha256']!=V.hash_of(vs['brand']): raise ValueError('Visual snapshot origin mismatch')
    for s in docs.values():
        for a in s['assets']:
            m=manifests.get(a['id'])
            if m and (m['sha256']!=a['sha256'] or identity(m)!=a.get('derivation_sha256',a['sha256'])): raise ValueError('Visual asset full identity mismatch')
    blobs=[];total=0;seen=set()
    for obj in bundle['objects']:
        O.exact(obj,('sha256','data'));O.sha(obj['sha256'])
        matches=[m for m in manifests.values() if m['sha256']==obj['sha256']]
        if not matches or obj['sha256'] in seen or not isinstance(obj['data'],str): raise ValueError('Unselected or duplicate byte object')
        try:data=base64.b64decode(obj['data'],validate=True)
        except Exception as exc:raise ValueError('Invalid byte encoding') from exc
        total+=len(data)
        if total>C.MAX_MEDIA_BYTES or hashlib.sha256(data).hexdigest()!=obj['sha256'] or any(len(data)!=m['bytes'] for m in matches): raise ValueError('Byte object hash/size mismatch')
        seen.add(obj['sha256']);blobs.append(({'id':matches[0]['id'],'sha256':obj['sha256'],'ext':matches[0]['ext']},data))
    return blobs,refs


def check_local(sp,bundle):
    """Collision checks include all retained imports, not just current library."""
    known=registry.read_all(sp);home=C._home(sp)/'identities'
    C._inside(sp,home)
    staged={}
    if home.exists():
        paths=list(home.glob('*.json'))
        if len(paths)>MAX_ENTRIES*20: raise ValueError('Staged identity capacity reached')
        for p in paths:
            old=C._read(p);validate_manifest(old)
            if old['id'] in staged and identity(staged[old['id']])!=identity(old): raise ValueError('Existing staged identity collision')
            staged[old['id']]=old
    for m in bundle['manifests']:
        old=known.get(m['id'])
        if old and ((old.get('derivation') or {}).get('sha256',old['sha256'])!=identity(m) or old['sha256']!=m['sha256']): raise ValueError('Local short asset ID collision')
        prior=staged.get(m['id'])
        if prior and (identity(prior)!=identity(m) or prior.get('occurrence')!=m.get('occurrence')): raise ValueError('Staged short asset ID collision')
    # Destination validation and capacity happen before editorial writes.
    paths=[]
    for m in bundle['manifests']: paths.append((home/(identity(m)+'.json'),m))
    for w in bundle['visuals']:
        key=C._sha({'namespace':w['namespace'],'document':w['snapshot']['document'],'snapshot':w['snapshot']['snapshot_sha256']})
        paths.append((C._home(sp)/'visuals'/(key+'.json'),w))
    existing=sum(p.stat().st_size for d in ('identities','visuals') for p in (C._home(sp)/d).glob('*.json'))
    if existing+sum(len(C._canonical(o))+1 for p,o in paths if not p.exists())>C.MAX_STATE_BYTES: raise ValueError('Staged identity capacity reached')
    for p,obj in paths:
        C._inside(sp,p)
        if p.exists() and C._read(p)!=obj: raise ValueError('Historical identity destination differs; preserve both namespaces')
    return paths


def dependencies(sp,bundle):
    known=registry.read_all(sp);supplied={o['sha256'] for o in bundle['objects']};rows=[]
    for m in bundle['manifests']:
        old=known.get(m['id']);missing=[]
        for p,a in zip(m.get('occurrence',{}).get('parents',[]),m['parents']):
            local=known.get(p['id'])
            if not local or local['sha256']!=p['sha256'] or (local.get('derivation') or {}).get('sha256',local['sha256'])!=a['identity_sha256']: missing.append('Exact parent '+p['id'])
        origin=m.get('occurrence',{}).get('origin',{})
        if 'content_doc' in origin and not any(w['namespace']==m['namespace'] and w['snapshot']['document']=={'id':origin['content_doc'],'version':origin['version'],'sha256':origin['doc_sha256']} for w in bundle['visuals']): missing.append('Exact visual document snapshot')
        present=bool(old and registry.asset_file(sp,old).is_file() and C.sha256_file(registry.asset_file(sp,old))==m['sha256'])
        staged=C._home(sp)/'media'/(m['sha256']+m['ext'])
        C._inside(sp,staged)
        present=present or m['sha256'] in supplied or (staged.is_file() and C.sha256_file(staged)==m['sha256'])
        if not present:missing.append('Output bytes')
        rows.append({'asset':m['id'],'identity_valid':True,'bytes_present':present,'missing':missing,'dependency_resolved':not missing,'locally_authorized':False,'renderable':False})
    for w in bundle['visuals']:
        from .content import visual
        current=visual.snapshot();s=w['snapshot'];missing=[]
        if s['identities']['fonts']!=current['fonts']:missing.append('Exact font versions')
        if s['identities']['template_sha256']!=current['template_sha256']:missing.append('Exact template version')
        if s['identities']['brand_sha256']!=V.hash_of(current['brand']):missing.append('Exact brand version')
        for a in s['assets']:
            rec=known.get(a['id'])
            if not rec or rec['sha256']!=a['sha256'] or (rec.get('derivation') or {}).get('sha256',rec['sha256'])!=a.get('derivation_sha256',a['sha256']):missing.append('Exact asset '+a['id'])
        if s['source']:missing.append('Selected source excerpt mapping')
        rows.append({'document':s['document'],'namespace':w['namespace'],'snapshot_sha256':s['snapshot_sha256'],'missing':missing,'renderable':not missing,'locally_authorized':False})
    return rows
