"""Portable, untrusted visual snapshot contract for the later collaboration adapter.

No paths, outputs, licenses or approval records cross this boundary. Validation
never installs fonts/assets, allocates a local revision, or authorizes review.
"""
from __future__ import annotations
import copy
import hashlib
import re
from . import visual

SCHEMA='visual_document_snapshot.v1'
SHA=re.compile(r'^[0-9a-f]{64}$')
ID=re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,80}$')

def canonical(value):
    import json
    raw=json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
    if len(raw)>100_000: raise ValueError('Visual snapshot exceeds 100000 bytes')
    return raw

def hash_of(value): return hashlib.sha256(canonical(value)).hexdigest()

def validate(raw):
    from ..studio.production import only
    from ..studio.image_workshop import number
    only(raw,{'schema','document','layout','source','assets','identities','snapshot_sha256','untrusted_data'})
    if raw.get('schema')!=SCHEMA or raw.get('untrusted_data') is not True: raise ValueError('Snapshot is untrusted visual_document_snapshot.v1 data')
    canonical(raw)
    doc=raw.get('document');only(doc,{'id','version','sha256'})
    if not isinstance(doc.get('id'),str) or not ID.fullmatch(doc['id']): raise ValueError('Invalid portable document ID')
    number(doc.get('version'),1,1e9,'Revision',True)
    if not isinstance(doc.get('sha256'),str) or not SHA.fullmatch(doc['sha256']): raise ValueError('Invalid exact document SHA')
    source=raw.get('source')
    if source is not None:
        only(source,{'source_id','source_sha256','text','text_sha256','locator'})
        if not isinstance(source.get('source_id'),str) or not ID.fullmatch(source['source_id']): raise ValueError('Source must be an ID, never a file path')
        if not isinstance(source.get('text'),str) or len(source['text'])>8000: raise ValueError('Bounded selected source text required')
        if not SHA.fullmatch(source.get('source_sha256','')) or hashlib.sha256(source['text'].encode()).hexdigest()!=source.get('text_sha256'): raise ValueError('Source identity mismatch')
        loc=source.get('locator');only(loc,{'id','start','end','page'})
        if not re.fullmatch(r'section_[0-9]{5}',loc.get('id','')): raise ValueError('Invalid section ID')
        for k in ('start','end'): number(loc.get(k),0,2_100_000,'Source '+k,True)
        if loc['end']<loc['start']: raise ValueError('Reversed source range')
        if loc.get('page') is not None: number(loc['page'],1,500,'PDF page',True)
    visual.validate(raw.get('layout'),source)
    assets=raw.get('assets')
    if not isinstance(assets,list) or len(assets)>30: raise ValueError('At most 30 asset references')
    ids=set()
    for a in assets:
        only(a,{'id','sha256','derivation_sha256'})
        identity_sha=a.get('derivation_sha256',a.get('sha256'))
        if not isinstance(identity_sha,str) or not SHA.fullmatch(identity_sha): raise ValueError('Invalid derivative occurrence identity')
        if not isinstance(a.get('sha256'),str) or not SHA.fullmatch(a['sha256']) or a.get('id')!='a_'+identity_sha[:12] or a['id'] in ids: raise ValueError('Invalid immutable asset reference')
        ids.add(a['id'])
    if ids!={e['asset'] for e in raw['layout']['elements'] if e['kind']=='image'}: raise ValueError('Asset references must exactly match image layers')
    identity=raw.get('identities');only(identity,{'renderer','template_id','template_sha256','brand_sha256','fonts','palette'})
    if identity.get('renderer')!='visual-render.v1' or identity.get('template_id')!='visual-canvas': raise ValueError('Unsupported visual renderer/template identity')
    for k in ('template_sha256','brand_sha256'):
        if not isinstance(identity.get(k),str) or not SHA.fullmatch(identity[k]): raise ValueError('Invalid '+k)
    only(identity.get('fonts'),set(visual.FONTS))
    if set(identity['fonts'])!=set(visual.FONTS) or any(not isinstance(v,str) or not SHA.fullmatch(v) for v in identity['fonts'].values()): raise ValueError('Exact allowlisted font hashes required')
    palette=identity.get('palette')
    if not isinstance(palette,dict) or not 1<=len(palette)<=30: raise ValueError('Bounded palette required')
    for k,v in palette.items():
        if not ID.fullmatch(k): raise ValueError('Invalid color token')
        visual.color(v)
    payload={k:v for k,v in raw.items() if k!='snapshot_sha256'}
    if hash_of(payload)!=raw.get('snapshot_sha256'): raise ValueError('Snapshot hash mismatch')
    return copy.deepcopy(raw)

def export(sp,did,version):
    from ..studio.media_workshop import load_design
    info=load_design(sp,did,version);doc=info['doc'];item=doc['items'][0];provenance=item['provenance'];inputs=provenance['visual_snapshot'];source=doc['sources'].get(item.get('source'))
    if source:
        source={'source_id':source['file'],'source_sha256':source['file'].removeprefix('recording_'),'text':source['text'],'text_sha256':source['text_sha256'],'locator':source['locator']}
    from ..studio.image_workshop import asset
    references=[]
    for aid,sha in sorted(provenance['asset_hashes'].items()):
        rec,_=asset(sp,aid)
        if rec['sha256']!=sha: raise ValueError('Saved asset reference changed')
        reference={'id':aid,'sha256':sha}
        if rec.get('derivation'): reference['derivation_sha256']=rec['derivation']['sha256']
        references.append(reference)
    out={'schema':SCHEMA,'untrusted_data':True,'document':{'id':did,'version':version,'sha256':info['sha256']},'layout':copy.deepcopy(item['visual']),'source':source,'assets':references,
         'identities':{'renderer':'visual-render.v1','template_id':'visual-canvas','template_sha256':inputs['template_sha256'],'brand_sha256':hash_of(inputs['brand']),'fonts':inputs['fonts'],'palette':inputs['brand']['colors']}}
    out['snapshot_sha256']=hash_of(out)
    return validate(out)
