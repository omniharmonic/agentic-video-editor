"""Bounded local image transforms and display-only renditions (never export identities)."""
from __future__ import annotations
import hashlib
import io
import json
import math
import os
import re
import tempfile
from urllib.parse import quote
from PIL import Image, ImageCms, ImageEnhance, ImageOps
from .. import config, library
from ..publish.safety import sha256_file
from . import production

VERSION = 'pillow-srgb-v1'
MAX_PIXELS = 40_000_000
MAX_EDGE = 8192
TIERS = {'small': 320, 'medium': 768, 'large': 1600}

def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()

def number(value, lo, hi, label, integer=False):
    if type(value) not in ((int,) if integer else (int,float)) or not math.isfinite(value) or not lo <= value <= hi:
        raise ValueError(f'{label} must be {"an integer" if integer else "a finite number"} from {lo} to {hi}')
    return value

def presets():
    import yaml
    return yaml.safe_load((config.REPO_ROOT/'config'/'image-presets.yaml').read_text())

def asset(sp, aid):
    from .workbench import safe
    rec = library.get(sp, aid)
    if rec['kind'] not in ('image','video'): raise ValueError('Choose an image or video asset')
    path = safe(sp, sp.root / rec['file'])
    if sha256_file(path) != rec['sha256']: raise ValueError('Registered original checksum changed; reimport the original')
    return rec, path

def decode(path):
    try:
        with Image.open(path) as im:
            if im.format not in ('JPEG','PNG','WEBP'): raise ValueError('Use JPEG, PNG or WebP')
            if getattr(im,'n_frames',1) != 1: raise ValueError('Animated images are unsupported; choose a still frame')
            if im.width*im.height > MAX_PIXELS: raise ValueError(f'Image exceeds {MAX_PIXELS} pixels')
            if im.mode not in ('RGB','RGBA','L','LA','P','CMYK'): raise ValueError('HDR/high-bit-depth images are unsupported; export an sRGB still')
            im.load()
            normalized = ImageOps.exif_transpose(im)
            alpha = normalized.convert('RGBA').getchannel('A')
            if im.info.get('icc_profile'):
                try:
                    normalized = ImageCms.profileToProfile(normalized.convert('RGB'), ImageCms.ImageCmsProfile(io.BytesIO(im.info['icc_profile'])), ImageCms.createProfile('sRGB'), outputMode='RGB')
                except Exception as exc: raise ValueError('ICC conversion failed; export an sRGB image') from exc
            elif im.mode == 'CMYK': raise ValueError('CMYK image requires a valid ICC profile; export sRGB')
            normalized = normalized.convert('RGBA'); normalized.putalpha(alpha)
            normalized.info.clear()
            return normalized
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError('Image cannot be decoded safely; choose an intact supported still') from exc

def capabilities(sp, aid):
    rec,path=asset(sp,aid)
    if rec['kind']!='image': raise ValueError('The image workshop requires a still image')
    im=decode(path)
    return {'asset':rec['id'],'origin':rec.get('origin',{}),'sha256':rec['sha256'],'w':im.width,'h':im.height,'orientation':'exif-normalized',
            'brand_colors':__import__('ave.motion',fromlist=['load_tokens']).load_tokens()['colors'],'max_pixels':MAX_PIXELS,'max_edge':MAX_EDGE,'presets':presets(),'preview_url':preview_url(rec,'large'),
            'original_url':'/files/'+quote(rec['file'],safe='/'),'formats':['png','jpeg','webp'],'renderer':VERSION}

def validate(sp, raw):
    production.only(raw, {'schema','source_asset','source_sha256','orientation','rotate_quarters','flip_x','flip_y','crop','adjust','preset','output'})
    if raw.get('schema')!='image_transform.v1' or raw.get('orientation')!='exif-normalized': raise ValueError('Use image_transform.v1 with exif-normalized coordinates')
    rec,path=asset(sp,raw.get('source_asset'))
    if rec['kind']!='image' or raw.get('source_sha256')!=rec['sha256']: raise ValueError('Source identity mismatch')
    im=decode(path)
    number(raw.get('rotate_quarters'),0,3,'Quarter turns',True)
    for key in ('flip_x','flip_y'):
        if type(raw.get(key)) is not bool: raise ValueError(key+' must be boolean')
    crop=raw.get('crop');production.only(crop,{'x','y','w','h'})
    for k in ('x','y','w','h'): number(crop.get(k),0 if k in ('x','y') else .000001,1,'Crop '+k)
    if crop['x']+crop['w']>1.000000001 or crop['y']+crop['h']>1.000000001: raise ValueError('Crop must fit the rotated canvas')
    adj=raw.get('adjust');production.only(adj,{'exposure_ev','contrast','saturation','warmth'})
    for k,limits in {'exposure_ev':(-3,3),'contrast':(0,3),'saturation':(0,3),'warmth':(-1,1)}.items(): number(adj.get(k),*limits,k)
    preset=raw.get('preset')
    if preset is not None:
        production.only(preset,{'id','version'})
        cfg=presets()
        if type(preset.get('version')) is not int or preset.get('version')!=cfg['version'] or preset.get('id') not in cfg['presets'] or adj!=cfg['presets'][preset['id']]: raise ValueError('Preset values/version disagree; select Custom for overrides')
    out=raw.get('output');production.only(out,{'w','h','format','color_space','background'})
    for k in ('w','h'): number(out.get(k),1,MAX_EDGE,'Output '+k,True)
    if out['w']*out['h']>MAX_PIXELS: raise ValueError('Output exceeds pixel budget')
    if out.get('format') not in ('png','jpeg','webp') or out.get('color_space')!='srgb': raise ValueError('Output must be PNG, JPEG or WebP in sRGB')
    if 'background' in out and (not isinstance(out['background'],str) or not re.fullmatch(r'#[0-9a-fA-F]{6}',out['background'])): raise ValueError('Background must be a six-digit hex color')
    if out.get('format')=='jpeg' and not re.fullmatch(r'#[0-9a-fA-F]{6}',out.get('background','')): raise ValueError('JPEG needs an explicit flatten background')
    return rec,im

def apply(im,spec):
    im=im.rotate(-90*spec['rotate_quarters'],expand=True)
    if spec['flip_x']: im=ImageOps.mirror(im)
    if spec['flip_y']: im=ImageOps.flip(im)
    c=spec['crop'];w,h=im.size
    box=(round(c['x']*w),round(c['y']*h),round((c['x']+c['w'])*w),round((c['y']+c['h'])*h))
    if box[2]<=box[0] or box[3]<=box[1]: raise ValueError('Crop contains no source pixels')
    im=im.crop(box);a=im.getchannel('A');rgb=im.convert('RGB');adj=spec['adjust']
    rgb=ImageEnhance.Brightness(rgb).enhance(2**adj['exposure_ev'])
    rgb=ImageEnhance.Contrast(rgb).enhance(adj['contrast']);rgb=ImageEnhance.Color(rgb).enhance(adj['saturation'])
    r,g,b=rgb.split();warm=adj['warmth']*32
    rgb=Image.merge('RGB',(r.point(lambda v: max(0,min(255,round(v+warm)))),g,b.point(lambda v:max(0,min(255,round(v-warm))))))
    rgb.putalpha(a)
    out=spec['output'];result=rgb.resize((out['w'],out['h']),Image.Resampling.LANCZOS)
    return result, {'pixel_bounds':list(box),'upscaled':out['w']>rgb.width or out['h']>rgb.height}

def atomic_image(sp, im, path, fmt='png', background=None):
    from .workbench import safe
    safe(sp,path);path.parent.mkdir(parents=True,exist_ok=True)
    if fmt=='jpeg':
        flat=Image.new('RGB',im.size,background);flat.paste(im,mask=im.getchannel('A'));im=flat
    fd,tmp=tempfile.mkstemp(prefix='.image-',suffix='.part',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f:
            im.save(f,format={'png':'PNG','jpeg':'JPEG','webp':'WEBP'}[fmt],**({'quality':95,'subsampling':0} if fmt=='jpeg' else {}));f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def transform(studio,spec,by):
    from .workbench import safe,read,write
    rec,im=validate(studio.sp,spec);sp=studio.sp
    key=digest({'renderer':VERSION,'spec':spec,'encoder':{'quality':95,'subsampling':0}})
    path=safe(sp,sp.work/'production'/'images'/key/('master.'+spec['output']['format']))
    receipt=path.with_suffix('.json');details=None
    if receipt.exists() and path.exists():
        safe(sp,receipt)
        try:
            meta=read(sp,receipt)
            with Image.open(path) as cached:
                cached.load();valid=cached.size==(spec['output']['w'],spec['output']['h']) and sha256_file(path)==meta['sha256']
            if valid: details=meta['details']
        except (ValueError,OSError,KeyError): pass
    if details is None:
        result,details=apply(im,spec);atomic_image(sp,result,path,spec['output']['format'],spec['output'].get('background'))
        write(sp,receipt,{'sha256':sha256_file(path),'details':details})
    out=library.add_derivative(sp,path,by=by,derived_from=[rec['id']],alt=rec.get('alt') or '',origin={'image_transform':spec,'renderer':VERSION,'cache_key':key},now=studio.now())
    return {'assets':[view(out)],'details':details,'spec':spec}

def view(rec):
    return {**rec,'asset':rec['id'],'url':'/files/'+quote(rec['file'],safe='/'),'preview_url':preview_url(rec,'large')}

def preview_url(rec,tier='small'):
    return '/api/studio/workbench/preview/'+rec['id']+'/'+tier if rec and rec.get('kind') in ('image','video') else None

def rendition(sp,aid,tier):
    """Request-time generation means offscreen lazy images do no decode work."""
    from .workbench import safe,write,read
    if tier not in TIERS: raise ValueError('Unsupported preview size tier')
    rec,path=asset(sp,aid);key=digest({'sha':rec['sha256'],'tier':tier,'renderer':VERSION,'kind':rec['kind']})
    dest=safe(sp,sp.work/'cache'/'previews'/key[:2]/(key+'.png'));meta=dest.with_suffix('.json')
    if dest.exists() and meta.exists():
        try:
            receipt=read(sp,meta)
            with Image.open(dest) as image:
                image.load()
                if max(image.size)<=TIERS[tier] and list(image.size)==receipt['size'] and sha256_file(dest)==receipt['sha256']: return dest
        except (ValueError,OSError,KeyError): pass
    if rec['kind']=='image': im=decode(path)
    else:
        import subprocess
        from ..jobs import child_env
        dest.parent.mkdir(parents=True,exist_ok=True)
        fd,tmp=tempfile.mkstemp(suffix='.png',dir=dest.parent);os.close(fd)
        try:
            command=[config.binary('ffmpeg'),'-nostdin','-v','error','-y','-i',str(path),'-frames:v','1','-vf',f'scale={TIERS[tier]}:{TIERS[tier]}:force_original_aspect_ratio=decrease',tmp]
            subprocess.run(command,env=child_env(),check=True,timeout=45,capture_output=True);im=decode(tmp)
        finally: os.unlink(tmp)
    im.thumbnail((TIERS[tier],TIERS[tier]),Image.Resampling.LANCZOS);atomic_image(sp,im,dest)
    write(sp,meta,{'size':list(im.size),'sha256':sha256_file(dest),'source_sha256':rec['sha256'],'tier':tier})
    return dest
