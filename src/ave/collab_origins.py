"""Portable occurrence preimages. Validation never rewrites historical values."""
from __future__ import annotations
import re
from .studio.production import only
from .studio.image_workshop import number
from .content import visual

SHA = re.compile(r'^[0-9a-f]{64}$')
ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,100}$')
ASSET = re.compile(r'^a_[0-9a-f]{12}$')


def sha(v):
    if not isinstance(v, str) or not SHA.fullmatch(v): raise ValueError('Expected full SHA-256')


def ident(v):
    if not isinstance(v, str) or not ID.fullmatch(v): raise ValueError('Expected portable identity, never a path')


def asset(v):
    if not isinstance(v, str) or not ASSET.fullmatch(v): raise ValueError('Expected asset identity')


def exact(v, keys):
    only(v, set(keys))
    if set(v) != set(keys): raise ValueError('Missing portable origin fields')


def brand(v):
    exact(v, ('name','colors','alpha','fonts','type','tracking','easing','motion','layout','safe_area'))
    if not isinstance(v['name'],str) or len(v['name'])>200: raise ValueError('Invalid brand name')
    if not isinstance(v['colors'],dict) or not 1<=len(v['colors'])<=30: raise ValueError('Invalid colors')
    for k,c in v['colors'].items(): ident(k);visual.color(c)
    for group,keys,lo,hi in [('alpha',('panel','backdrop_solid','scrim'),0,1),('type',('display','title','quote','name','stat','body','label'),1,1000),('tracking',('label','display'),-1,1),('motion',('in_s','out_s','stagger_s','rise'),0,1000),('layout',('unit_height','margin_x','margin_y','lower_third_bottom'),0,8192),('safe_area',('action','title'),0,1)]:
        exact(v[group],keys)
        for x in v[group].values(): number(x,lo,hi,group)
    exact(v['easing'],('out','in','inout','standard','linear'))
    for curve in v['easing'].values():
        if not isinstance(curve,list) or len(curve)!=4: raise ValueError('Invalid easing')
        for x in curve: number(x,0,1,'Easing')
    exact(v['fonts'],('serif','sans'))
    for role,f in v['fonts'].items():
        exact(f,('family','fallback','files'))
        if f['family'] not in visual.FONTS: raise ValueError('Unsupported brand font')
        if not isinstance(f['fallback'],list) or len(f['fallback'])>8 or any(x not in ('Source Serif 4','Georgia','Times New Roman','serif','Helvetica Neue','Helvetica','Arial','sans-serif') for x in f['fallback']): raise ValueError('Unsupported font fallback')
        if not isinstance(f['files'],list) or len(f['files'])!=1: raise ValueError('Unsupported font files')
        for row in f['files']:
            exact(row,('file','weight'))
            if row['file']!=visual.FONTS[f['family']] or row['weight'] not in ('200 800','100 900'): raise ValueError('Unsupported font resource identity')


def render_snapshot(v):
    exact(v,('renderer_version','template_rev','canva','visual_snapshot','asset_hashes'))
    if v['renderer_version'] is not None or v['template_rev'] is not None: raise ValueError('Unsupported renderer bookkeeping')
    exact(v['canva'],('design_id','template_id'))
    if any(x is not None for x in v['canva'].values()): raise ValueError('Live Canva identifiers are not portable')
    s=v['visual_snapshot'];exact(s,('schema','fonts','template','template_sha256','brand'))
    if s['schema']!='visual-render.v1': raise ValueError('Unsupported visual renderer')
    if not isinstance(s['template'],str) or len(s['template'])>200 or '/' in s['template']: raise ValueError('Invalid template revision')
    exact(s['fonts'],visual.FONTS)
    for x in s['fonts'].values(): sha(x)
    sha(s['template_sha256']);brand(s['brand'])
    if not isinstance(v['asset_hashes'],dict) or len(v['asset_hashes'])>30: raise ValueError('Too many visual assets')
    for k,x in v['asset_hashes'].items(): asset(k);sha(x)


def edit(v):
    exact(v,('stage','name','version','sha256'))
    if v['stage'] not in ('assembly','rough','final','short'): raise ValueError('Unsupported edit stage')
    if v['name'] is not None: ident(v['name'])
    number(v['version'],1,1e9,'Edit version',True);sha(v['sha256'])


def validate(origin, parents):
    """Accept only known typed producer shapes; unknown fields fail closed."""
    if not isinstance(origin,dict): raise ValueError('Occurrence origin must be an object')
    if 'image_transform' in origin:
        exact(origin,('image_transform','renderer','cache_key'))
        if origin['renderer']!='pillow-srgb-v1': raise ValueError('Unsupported image renderer')
        sha(origin['cache_key']);s=origin['image_transform']
        exact(s,('schema','source_asset','source_sha256','orientation','rotate_quarters','flip_x','flip_y','crop','adjust','preset','output'))
        if s['schema']!='image_transform.v1' or s['orientation']!='exif-normalized': raise ValueError('Unsupported image transform')
        asset(s['source_asset']);sha(s['source_sha256'])
        if parents!=[{'id':s['source_asset'],'sha256':s['source_sha256']}]: raise ValueError('Transform parent mismatch')
        number(s['rotate_quarters'],0,3,'Rotation',True)
        if any(type(s[k]) is not bool for k in ('flip_x','flip_y')): raise ValueError('Invalid flip')
        exact(s['crop'],('x','y','w','h'))
        for k,x in s['crop'].items(): number(x,0 if k in ('x','y') else .000001,1,'Crop')
        if s['crop']['x']+s['crop']['w']>1.000000001 or s['crop']['y']+s['crop']['h']>1.000000001: raise ValueError('Crop outside canvas')
        exact(s['adjust'],('exposure_ev','contrast','saturation','warmth'))
        for k,bounds in {'exposure_ev':(-3,3),'contrast':(0,3),'saturation':(0,3),'warmth':(-1,1)}.items(): number(s['adjust'][k],*bounds,k)
        if s['preset'] is not None:
            exact(s['preset'],('id','version'));ident(s['preset']['id']);number(s['preset']['version'],1,1e9,'Preset version',True)
        o=s['output'];only(o,{'w','h','format','color_space','background'})
        for k in ('w','h'): number(o.get(k),1,8192,k,True)
        if o['w']*o['h']>40_000_000 or o.get('format') not in ('png','jpeg','webp') or o.get('color_space')!='srgb': raise ValueError('Unsupported image output')
        if 'background' in o: visual.color(o['background'])
        if o['format']=='jpeg': visual.color(o.get('background'))
    elif 'split_group' in origin:
        exact(origin,('split_group','index','pixel_bounds','source_sha256'));ident(origin['split_group']);sha(origin['source_sha256']);number(origin['index'],0,35,'Tile',True)
        b=origin['pixel_bounds']
        if not isinstance(b,list) or len(b)!=4: raise ValueError('Invalid tile bounds')
        for n in b: number(n,0,8192,'Tile bound',True)
        if b[2]<=b[0] or b[3]<=b[1] or len(parents)!=1 or parents[0]['sha256']!=origin['source_sha256']: raise ValueError('Split parent or bounds mismatch')
    elif 'content_doc' in origin:
        exact(origin,('content_doc','version','doc_sha256','render_snapshot','renderer'));ident(origin['content_doc']);number(origin['version'],1,1e9,'Document version',True);sha(origin['doc_sha256'])
        if origin['renderer']!='visual-render.v1': raise ValueError('Unsupported design renderer')
        render_snapshot(origin['render_snapshot'])
        if {p['id']:p['sha256'] for p in parents}!=origin['render_snapshot']['asset_hashes']: raise ValueError('Design parents mismatch')
    elif origin.get('schema')=='video_render.v1':
        exact(origin,('schema','project','stage','version','edit_sha256','caption_edit_sha256','preset','mode','request_id','renderer','caption_snapshot_sha256','font','srt_sha256','ass_sha256'))
        for k in ('project','request_id'): ident(origin[k])
        number(origin['version'],1,1e9,'Version',True)
        for k in ('edit_sha256','caption_edit_sha256','caption_snapshot_sha256','srt_sha256','ass_sha256'): sha(origin[k])
        if origin['stage']!='short' or origin['mode'] not in ('audio','audiovisual','silent_video') or origin['preset'] not in ('master','audio_only','podcast_mp3','wav') or origin['renderer'] not in ('ave.compile.v1','ave.compile.v2-clock-occurrences'): raise ValueError('Unsupported video render identity')
        font=origin['font'];exact(font,('family','file_name','sha256'))
        if font['family'] not in visual.FONTS or font['file_name'] != visual.FONTS[font['family']]: raise ValueError('Unsupported caption font resource')
        sha(font['sha256'])
    elif origin.get('schema')=='episode_sequence_render.v1':
        exact(origin,('schema','episode','plan_sha256','component','rough'));ident(origin['episode']);sha(origin['plan_sha256']);edit(origin['rough'])
        if origin['component'] not in ('opening','closing'): raise ValueError('Invalid sequence component')
    else:
        raise ValueError('Unsupported portable occurrence origin: '+str(origin.get('schema','unknown'))+'; local paths/authority must not be sanitized into a different historical identity')
