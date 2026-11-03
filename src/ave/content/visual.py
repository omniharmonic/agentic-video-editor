"""Typed ContentDoc visual_layout.v1 validation and local pixel renderer."""
from __future__ import annotations
import re
from PIL import Image, ImageDraw, ImageFont, ImageOps
from .. import motion
from ..publish.safety import sha256_file

FONTS={'Inter':'Inter-Variable.ttf','Newsreader':'Newsreader-Variable.ttf'}

def font_path(name):
    if name not in FONTS: raise ValueError('Choose Inter or Newsreader')
    return motion.KIT/'brand'/'fonts'/FONTS[name]

def snapshot():
    from . import registry
    return {'schema':'visual-render.v1','fonts':{k:sha256_file(font_path(k)) for k in FONTS},
            'template':registry.load_registry().get('visual-canvas').rev(),
            'template_sha256':sha256_file(registry.load_registry().get('visual-canvas').path),
            'brand':motion.load_tokens()}

def validate(layout, source=None):
    from ..studio.production import only
    from ..studio.image_workshop import number
    only(layout,{'schema','w','h','background','elements','template_kind'})
    if layout.get('schema')!='visual_layout.v1': raise ValueError('Use visual_layout.v1')
    for k in ('w','h'): number(layout.get(k),64,8192,k,True)
    if layout['w']*layout['h']>40_000_000: raise ValueError('Canvas exceeds 40 million pixels')
    if layout.get('template_kind') not in ('photo','title','quote'): raise ValueError('Choose photo, title or quote template')
    color(layout.get('background'))
    els=layout.get('elements')
    if not isinstance(els,list) or not 1<=len(els)<=30: raise ValueError('Use 1–30 design elements')
    seen=set()
    for e in els:
        only(e,{'id','kind','x','y','w','h','text','font','size','color','align','asset','copy_kind'})
        eid=e.get('id')
        if not isinstance(eid,str) or not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]{0,39}',eid) or eid in seen: raise ValueError('Elements need unique stable IDs')
        seen.add(eid)
        if e.get('kind') not in ('text','image','shape'): raise ValueError('Unsupported element kind')
        for k in ('x','y','w','h'): number(e.get(k),0 if k in ('x','y') else 1,8192,'Element '+k)
        if e['x']+e['w']>layout['w'] or e['y']+e['h']>layout['h']: raise ValueError('Element must fit the canvas')
        if e['kind']=='text':
            if not isinstance(e.get('text'),str) or len(e['text'])>4000: raise ValueError('Text must be at most 4000 characters; nothing is silently truncated')
            font_path(e.get('font'));number(e.get('size'),8,400,'Font size',True);color(e.get('color'))
            if e.get('align') not in ('left','center','right'): raise ValueError('Unsupported text alignment')
            if e.get('copy_kind') not in ('authored','quote'): raise ValueError('Label text as authored copy or a direct quote')
            if e['copy_kind']=='quote':
                from .sources import quote_problems
                if not source or quote_problems(e['text'],source.get('text','')): raise ValueError('Direct quote must remain verbatim from the selected source')
        elif e['kind']=='shape': color(e.get('color'))
        elif not isinstance(e.get('asset'),str): raise ValueError('Image element needs a registered asset')
    return layout

def color(value):
    if not isinstance(value,str) or not re.fullmatch(r'#[0-9a-fA-F]{6}',value): raise ValueError('Use a six-digit hex color')
    return value

def render(sp,layout,inputs,source=None):
    from ..studio.image_workshop import asset,decode
    validate(layout,source)
    if inputs['fonts']!={k:sha256_file(font_path(k)) for k in FONTS}: raise ValueError('Saved font version is unavailable; save a new revision explicitly')
    canvas=Image.new('RGBA',(layout['w'],layout['h']),layout['background']);draw=ImageDraw.Draw(canvas)
    warnings=[];contrasts=[]
    for e in layout['elements']:
        x,y,w,h=[round(e[k]) for k in ('x','y','w','h')]
        if e['kind']=='image':
            rec,path=asset(sp,e['asset']);im=decode(path);im=ImageOps.fit(im,(w,h),method=Image.Resampling.LANCZOS);canvas.alpha_composite(im,(x,y))
        elif e['kind']=='shape': draw.rectangle((x,y,x+w-1,y+h-1),fill=e['color'])
        else:
            font=ImageFont.truetype(str(font_path(e['font'])),e['size']);lines=[]
            for paragraph in e['text'].split('\n'):
                line=''
                for word in paragraph.split(' '):
                    trial=(line+' '+word).lstrip()
                    if draw.textlength(trial,font=font)>w and line: lines.append(line);line=word
                    else: line=trial
                lines.append(line)
            spacing=round(e['size']*.25);text='\n'.join(lines)
            bbox=draw.multiline_textbbox((0,0),text,font=font,spacing=spacing)
            if bbox[2]>w or bbox[3]>h: raise ValueError(f'Text overflow in {e["id"]}: enlarge its box, reduce size or edit authored copy')
            if min(x,y,layout['w']-x-w,layout['h']-y-h)<min(layout['w'],layout['h'])*.04: warnings.append(e['id']+': text outside 4% safe area')
            # Contrast measured against pixels underneath the text rectangle, not just the background token.
            sample=canvas.crop((x,y,x+w,y+h)).convert('RGB').resize((8,8))
            def luminance(rgb):
                vals=[v/255/12.92 if v/255<=.04045 else ((v/255+.055)/1.055)**2.4 for v in rgb]
                return sum(a*b for a,b in zip(vals,(.2126,.7152,.0722)))
            fg=luminance(tuple(int(e['color'][i:i+2],16) for i in (1,3,5)))
            ratios=[(max(fg,luminance(px))+.05)/(min(fg,luminance(px))+.05) for px in (sample.getpixel((x,y)) for y in range(8) for x in range(8))]
            contrast=round(min(ratios),2);contrasts.append({'element':e['id'],'minimum_sampled':contrast})
            if contrast<4.5: warnings.append(e['id']+': sampled contrast below 4.5:1')
            offset=0 if e['align']=='left' else (w-bbox[2])/(2 if e['align']=='center' else 1)
            draw.multiline_text((x+offset,y),text,font=font,fill=e['color'],spacing=spacing,align=e['align'])
    return canvas,{'overflow':False,'safe_area':.04,'contrast':contrasts,'warnings':warnings,'ok':not warnings}
