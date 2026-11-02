"""Delivery rasterization keeps authored aspect and frame relationships."""
import copy
import io
import json
import subprocess

import numpy as np
import pytest
from PIL import Image

from ave import captions, compile as comp, editdoc, ffutil, presets, sequence
from ave.episode import read_json, write_json
from test_sequence import ep, _policy, NOHOME


def _alpha(path, seconds, size=(360,270)):
    data=subprocess.run(['ffmpeg','-nostdin','-v','error','-ss',str(seconds),'-i',str(path),'-frames:v','1','-vf',f'alphaextract,scale={size[0]}:{size[1]}','-f','image2pipe','-vcodec','png','-'],capture_output=True,check=True).stdout
    return np.asarray(Image.open(io.BytesIO(data)).convert('L'),dtype=float)


def test_credits_target_raster_preserves_authored_frames_and_visible_travel(ep):
    example=read_json(sequence.SEQ_KIT/'credits-roll/example.json')
    item={'template':'credits-roll','props':example['props'],'kit':'synthetic-geometry-v1','px_per_frame':6,'min_s':1,'max_s':3,'lead_in_s':.2,'duration':3}
    source={'w':360,'h':270}; target={'w':1440,'h':1080}
    low,n=sequence.render_credits(ep,item,10,source)
    high,m=sequence.render_credits(ep,item,10,target,timing_canvas=source)
    assert m==n and low!=high
    assert ffutil.count_video_packets(ep.root/high)==n
    header=ffutil.probe(ep.root/high);assert (header['w'],header['h'])==(1440,1080)
    meta=read_json((ep.root/high).with_suffix('.json'))
    assert meta['timing_canvas']==source and meta['canvas']==target
    # Compare early, middle and late *visible* text, independently decoded.
    visible=[]
    for frame in range(n):
        a=_alpha(ep.root/low,frame/10)
        if a.sum()>500:visible.append((frame,a))
    assert len(visible)>=3
    for frame,a in [visible[0],visible[len(visible)//2],visible[-1]]:
        b=_alpha(ep.root/high,frame/10)
        assert b.sum()>100
        # Raster hinting may differ; geometry/content stays in the same region.
        assert np.abs(a-b).mean()<9
    assert _alpha(ep.root/high,0).sum()==0
    assert sequence.render_credits(ep,item,10,source)==(low,n)
    assert sequence.render_credits(ep,item,10,target,timing_canvas=source)==(high,m)


@pytest.mark.parametrize('preset,height',[('episode_draft',540),('episode_hd',1080)])
def test_sequence_build_rasterizes_at_delivery_geometry(ep,monkeypatch,preset,height):
    policy=_policy(ep,{'opening':{'hosts':{'enabled':False},'title_card':{'duration_s':3}},'closing':{'enabled':False}})
    pl=sequence.plan(ep,policy=policy,home=NOHOME)
    bdoc=copy.deepcopy(pl['_bdoc']);bdoc['canvas']={'w':480,'h':360};bdoc['fps']=10;pl['_bdoc']=bdoc
    before=copy.deepcopy(pl['opening'])
    monkeypatch.setattr(sequence,'body_gain_db',lambda *args:0)
    rep=sequence.build(ep,preset=preset,pl=pl,policy=policy,home=NOHOME,which=('opening',),record=False,output_dir=ep.work/preset)
    built=rep['built']['opening'];header=ffutil.probe(ep.root/built['file'])
    assert (header['w'],header['h'])==(round(height*4/3),height)
    assert built['source_canvas']=={'w':480,'h':360}
    doc=read_json(ep.work/preset/'opening.doc.json')
    assert doc['canvas']=={'w':round(height*4/3),'h':height}
    for overlay in doc['tracks']['overlays']:
        raster=ffutil.probe(ep.root/overlay['file'])
        assert (raster['w'],raster['h'])==(doc['canvas']['w'],height)
    assert pl['opening']['hash']==before['hash']
    assert built['frames']==sum(x['frames'] for x in before['items'])
    png=subprocess.run(['ffmpeg','-nostdin','-v','error','-ss','1.5','-i',str(ep.root/built['file']),'-frames:v','1','-f','image2pipe','-vcodec','png','-'],capture_output=True,check=True).stdout
    picture=np.asarray(Image.open(io.BytesIO(png)).convert('RGB'))
    assert picture.std()>5 # actual steady card content, not only file headers


def test_cold_caption_geometry_preserves_source_clocks(ep,monkeypatch):
    meta=read_json(ep.meta_path);meta['sequences']={'cold_open':{'quote':{'text':'welcome to the show today we are talking about building things that last','stage':'assembly'}}};write_json(ep.meta_path,meta)
    policy=_policy(ep,{'closing':{'enabled':False}});pl=sequence.plan(ep,policy=policy,home=NOHOME)
    assert not pl['errors']
    pl['_bdoc']['canvas']={'w':480,'h':360};cold=copy.deepcopy(pl['_cold']['doc'])
    monkeypatch.setattr(sequence,'body_gain_db',lambda *args:0)
    monkeypatch.setattr(sequence,'render_card',lambda *args,**kw:next(iter(cold['sources'].values()))['file'])
    monkeypatch.setattr(sequence,'make_plate',lambda *args,**kw:next(iter(cold['sources'].values()))['file'])
    captured=[]
    def render(_ep,doc,out,**kw):
        captured.append(copy.deepcopy(doc));return {'duration':doc['duration'],'fps':doc['fps'],'lufs':-16,'true_peak_db':-2,'warnings':[]}
    monkeypatch.setattr(comp,'render',render)
    sequence.build(ep,preset='episode_hd',pl=pl,policy=policy,home=NOHOME,which=('opening',),burn=True,record=False,output_dir=ep.work/'caption-geometry')
    doc=captured[0];assert doc['canvas']=={'w':1440,'h':1080}
    ass=(ep.root/doc['captions']['ass']).read_text();assert 'PlayResX: 1440' in ass and 'PlayResY: 1080' in ass
    assert [{k:v for k,v in s.items() if k!='id'} for s in doc['tracks']['video'][:len(cold['tracks']['video'])]]==[{k:v for k,v in s.items() if k!='id'} for s in cold['tracks']['video']]
    assert all(doc['sources'][k]==v for k,v in cold['sources'].items())
