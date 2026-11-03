"""Workbench adapters for immutable visual ContentDocs, image jobs and split groups."""
from __future__ import annotations
import copy
import re
from .. import library
from ..content import manifest as M, visual
from ..publish.safety import sha256_file
from . import composer, model, production, store
from .events import Actor
from . import image_workshop as images
from .workbench import safe,read,write

OPS=('image-transform','design-render','image-split')

def target(sp,value):
    if value is None: return
    production.only(value,{'post','variant','item','expected_rev'})
    post=store.load_post(sp,model.check_id(value.get('post')))
    images.number(value.get('expected_rev'),1,1e9,'Captured revision',True)
    v=model.find_variant(post,value['variant']) if value.get('variant') else {}
    projection=composer.projection(post,v,store.read_assets(sp))
    if not any(m['id']==value.get('item') for m in projection['content']+([projection['cover']] if projection['cover'] else [])): raise ValueError('Target slide no longer exists')

def validate_operation(sp,op,args):
    production.only(args, {'request_id','spec','target'} if op=='image-transform' else ({'request_id','doc','version','sha256'} if op=='design-render' else {'request_id','asset','rows','columns','tile_aspect','mode'}))
    rid=args.get('request_id')
    if not isinstance(rid,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}',rid): raise ValueError('A stable request ID is required')
    if op=='image-transform':
        rec,_=images.validate(sp,args.get('spec'))
        if args.get('target') is not None:
            production.only(args['target'],{'post','variant','item','expected_rev'})
            model.check_id(args['target'].get('post'));images.number(args['target'].get('expected_rev'),1,1e9,'Captured revision',True)
        return rec,None
    if op=='design-render':
        require_render_identity(args)
        return load_design(sp,args.get('doc'),args['version'],args['sha256']),None
    rec,_,_,_,_=split_master(sp,args)
    return rec,None

def require_render_identity(args):
    images.number(args.get('version'),1,1e9,'Exact design revision',True)
    if not isinstance(args.get('sha256'),str) or not re.fullmatch(r'[0-9a-f]{64}',args['sha256']):
        raise ValueError('Exact design SHA-256 is required')

def visual_identity(sp,layout):
    hashes={}
    for element in layout['elements']:
        if element['kind']=='image':
            rec,_=images.asset(sp,element['asset']);hashes[rec['id']]=rec['sha256']
    return {'visual_snapshot':visual.snapshot(),'asset_hashes':hashes}

def load_design(sp,did,version=None,expected_sha=None):
    model.check_id(did)
    if version is None: path=M.latest_path(sp,did)
    else:
        images.number(version,1,1e9,'Design version',True);path=M.path_for(sp,version,did)
    if path is None: raise ValueError('Design does not exist')
    doc=read(sp,safe(sp,path))
    if not doc['items'] or doc['items'][0].get('template')!='visual-canvas': raise ValueError('Not a visual canvas ContentDoc')
    if expected_sha and sha256_file(path)!=expected_sha: raise ValueError('Saved design identity changed')
    return {'doc':doc,'version':doc['saved']['version'],'sha256':sha256_file(path),'versions':M.list_versions(sp,did)}

def save_design(studio,body):
    production.only(body,{'id','expected_version','layout','source'})
    sp=studio.sp;did=body.get('id');expected=body.get('expected_version',0)
    if did is None:
        import secrets
        did='design_'+secrets.token_hex(10)
        if expected!=0: raise ValueError('New design version must be zero')
    else: model.check_id(did)
    images.number(expected,0,1e9,'Expected design version',True)
    source=None;selection=body.get('source')
    if selection:
        production.only(selection,{'source_id','section_id'})
        from .manuscripts import section
        row=section(sp,selection['source_id'],selection['section_id'])
        if row is None: raise ValueError('Selected manuscript section is missing')
        source={'type':'markdown','file':selection['source_id'],'text':row['text'],'text_sha256':images.digest(row['text'])}
        import hashlib
        source['text_sha256']=hashlib.sha256(row['text'].encode()).hexdigest();source['locator']={k:row[k] for k in ('id','start','end','page')}
    elif expected:
        old=load_design(sp,did)['doc'];source=old['sources'].get('writing')
    layout=copy.deepcopy(body.get('layout'));visual.validate(layout,source)
    identity=visual_identity(sp,layout);assets=list(identity['asset_hashes'])
    doc=M.new_doc(sp,did)
    if source: doc['sources']['writing']=source
    item=M.new_item('canvas','visual-canvas','paper','1x1' if layout['w']==layout['h'] else ('4x5' if layout['w']/layout['h']>.65 else '9x16'),{},source='writing' if source else None,renderer='visual',reason='Creator-authored visual layout; no automated editorial approval',confidence=0,assets=assets)
    item['visual']=layout;item['provenance'].update(identity);doc['items']=[item]
    # All ContentDoc writers share the same lock and allocation protocol.
    safe(sp,M.path_for(sp,expected+1,did))
    M.save_new_version(sp,doc,expected_version=expected)
    return load_design(sp,did)

def render_design(studio,args,by):
    require_render_identity(args)
    info=load_design(studio.sp,args['doc'],args['version'],args['sha256']);doc=info['doc'];item=doc['items'][0]
    for aid,sha in item['provenance']['asset_hashes'].items():
        if images.asset(studio.sp,aid)[0]['sha256']!=sha: raise ValueError('Design asset identity changed')
    im,critic=visual.render(studio.sp,item['visual'],item['provenance']['visual_snapshot'],doc['sources'].get(item.get('source')))
    key=images.digest({'doc':info['sha256'],'renderer':images.VERSION,'visual':'visual-render.v1'})
    path=safe(studio.sp,studio.sp.work/'production'/'images'/key/'design.png');images.atomic_image(studio.sp,im,path)
    rec=library.add_derivative(studio.sp,path,by=by,derived_from=item['license']['assets'],alt='',origin={'content_doc':doc['id'],'version':info['version'],'doc_sha256':info['sha256'],'render_snapshot':item['provenance'],'renderer':'visual-render.v1'},now=studio.now())
    return {'assets':[images.view(rec)],'critic':critic,'doc':doc['id'],'version':info['version'],'doc_sha256':info['sha256']}

def split_master(sp,args):
    rec,path=images.asset(sp,args.get('asset'))
    if rec['kind']!='image': raise ValueError('Split requires an image master')
    for k in ('rows','columns'): images.number(args.get(k),1,6,k,True)
    rows,cols=args['rows'],args['columns']
    if rows*cols>20: raise ValueError('Split supports at most 20 tiles')
    if args.get('mode') not in ('carousel','drafts'): raise ValueError('Choose carousel or separate drafts')
    ratio=args.get('tile_aspect')
    if ratio not in ('original','1:1','4:5','3:4'): raise ValueError('Unsupported tile aspect')
    im=images.decode(path);x=y=0;w,h=im.size
    if ratio!='original':
        a,b=map(int,ratio.split(':'));want=a*cols/(b*rows)
        if w/h>want: nw=int(h*want);x=(w-nw)//2;w=nw
        else: nh=int(w/want);y=(h-nh)//2;h=nh
    if min(w,h)<64: raise ValueError('Split master after aspect crop must be at least 64 pixels in each dimension')
    layout={'schema':'visual_layout.v1','template_kind':'photo','w':w,'h':h,'background':'#ffffff','elements':[{'id':'photo','kind':'image','asset':rec['id'],'x':0,'y':0,'w':w,'h':h}]}
    visual.validate(layout)
    identity=visual_identity(sp,layout)
    return rec,im,(x,y,w,h),layout,identity

def split(studio,job):
    sp=studio.sp;args=job['args']
    # Validate the actual source, effective crop and all ContentDoc identities
    # before receipts, derivatives, registry entries or posts can be created.
    rec,im,(x,y,w,h),master_layout,master_identity=split_master(sp,args)
    rows,cols=args['rows'],args['columns']
    group='split_'+job['id'][4:];did=group;receipt=safe(sp,sp.work/'production'/'splits'/(group+'.json'))
    if receipt.exists(): state=read(sp,receipt)
    else:
        state={'schema':'image_split.v1','group':group,'source_asset':rec['id'],'source_sha256':rec['sha256'],'source_doc':(rec.get('origin') or {}).get('content_doc'),'source_version':(rec.get('origin') or {}).get('version'),'rows':rows,'columns':cols,'source_rectangle':[x,y,x+w,y+h],'tiles':[],'posts':[],'local_feed_order':list(range(rows*cols)),'suggested_manual_posting_order':list(reversed(range(rows*cols))),'note':'Local profile arrangement is a planning preview; platform placement is not guaranteed.'}
        write(sp,receipt,state)
    for previous in state['tiles']:
        cached,_=images.asset(sp,previous['asset'])
        if (cached['w'],cached['h'])!=(previous['w'],previous['h']): raise ValueError('Completed split tile dimensions changed; preserved for inspection')
    for i in range(rows*cols):
        if any(t['index']==i for t in state['tiles']): continue
        r,c=divmod(i,cols);box=[x+c*w//cols,y+r*h//rows,x+(c+1)*w//cols,y+(r+1)*h//rows]
        tile=im.crop(box);dest=safe(sp,sp.work/'production'/'splits'/group/f'tile-{i}.png');images.atomic_image(sp,tile,dest)
        asset=library.add_derivative(sp,dest,by=job['by'],derived_from=[rec['id']],alt=f'Tile {i+1} of {rows*cols}',origin={'split_group':group,'index':i,'pixel_bounds':box,'source_sha256':rec['sha256']},now=studio.now())
        state['tiles'].append({'index':i,'row':r,'column':c,'pixel_bounds':box,'asset':asset['id'],'w':tile.width,'h':tile.height,'slot_id':f'm_tile_{i}'});write(sp,receipt,state)
    for n,tiles in enumerate([state['tiles']] if args['mode']=='carousel' else [[t] for t in state['tiles']]):
        pid=f'p_{group}_{n}'
        if safe(sp,store.post_path(sp,pid)).exists():
            existing=store.load_post(sp,pid)
            if ((existing.get('idea') or {}).get('origin') or {}).get('split_group')!=group: raise ValueError('Split draft identity belongs to another draft; existing draft preserved')
        else:
            post=model.new_post(idea='Split composition',by=job['by'],post_id=pid,reason='Creator-requested split composition; unreviewed draft',confidence=0,now=studio.now())
            post['media']=[{'id':t['slot_id'],'slot':i,'asset':t['asset'],'role':'image','alt':f'Tile {t["index"]+1}'} for i,t in enumerate(tiles)]
            post['idea']['origin']={'type':'idea','ref':group,'split_group':group}
            store.save_post(sp,post,expected_rev=None,actor=Actor('human',job['by']),op='workshop.split',now=studio.now())
        if pid not in state['posts']: state['posts'].append(pid);write(sp,receipt,state)
    # Split operation is part of a revisioned ContentDoc, not only a loose UI group.
    if not M.latest_path(sp,did):
        doc=M.new_doc(sp,did);item=M.new_item('master','visual-canvas','paper','1x1',{},renderer='visual',reason='Creator split operation',confidence=0,assets=[rec['id']]);item['visual']=master_layout;item['provenance'].update(master_identity);item['provenance']['split']=state;doc['items']=[item];M.save_new_version(sp,doc,expected_version=0)
    return {**state,'assets':[images.view(library.get(sp,t['asset'])) for t in state['tiles']]}

def execute(studio,job):
    if job['operation']=='image-transform': return images.transform(studio,job['args']['spec'],job['by'])
    if job['operation']=='design-render': return render_design(studio,job['args'],job['by'])
    return split(studio,job)

def attach(studio,body,actor):
    production.only(body,{'job_id','asset','target'})
    from .workbench import job_path
    job=read(studio.sp,job_path(studio.sp,body.get('job_id')))
    if job['status']!='done' or job['operation'] not in (*OPS,'video-render'): raise ValueError('Choose a completed workshop result')
    ids=[a['asset'] for a in job['result']['assets']]
    if body.get('asset') not in ids: raise ValueError('Asset is not an output of this job')
    chosen=body.get('target') or job['args'].get('target')
    if not chosen: raise ValueError('Choose a target slide explicitly')
    target(studio.sp,chosen)
    from .episode_release import native_attachment
    with native_attachment(studio,job) as guard:
        return composer.media_action(studio,chosen['post'],{'expected_rev':chosen['expected_rev'],'variant':chosen.get('variant'),'item':chosen['item'],'asset':body['asset'],'op':'replace'},actor,_commit_context=guard)
