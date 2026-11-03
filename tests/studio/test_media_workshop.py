"""Pixel truth, immutable identities, typed authoring, parser bounds and recovery."""
import copy
import hashlib
import json
from pathlib import Path
import pytest
from PIL import Image
from ave import library
from ave.content import manifest as M
from ave.studio import image_workshop as I, media_workshop as W, manuscripts, model,store
from ave.studio.events import Actor
from .test_workbench import studio,wb,upload,finish

@pytest.fixture
def photo(studio,tmp_path):
    im=Image.new('RGBA',(101,103))
    im.putdata([(x*2,y*2,50,128 if x<50 else 255) for y in range(103) for x in range(101)])
    p=tmp_path/'coordinates.png';im.save(p)
    return library.add_file(studio.sp,p,source={'type':'own'},by='synthetic')

def spec(photo):
    return {'schema':'image_transform.v1','source_asset':photo['id'],'source_sha256':photo['sha256'],'orientation':'exif-normalized','rotate_quarters':0,'flip_x':False,'flip_y':False,'crop':{'x':0,'y':0,'w':1,'h':1},'adjust':{'exposure_ev':0,'contrast':1,'saturation':1,'warmth':0},'preset':{'id':'neutral','version':1},'output':{'w':101,'h':103,'format':'png','color_space':'srgb'}}

def test_exact_pixels_rotation_crop_alpha_and_cache(studio,photo):
    s=spec(photo);s.update(rotate_quarters=1,flip_x=True);s['output'].update(w=103,h=101)
    out=I.transform(studio,s,'synthetic')['assets'][0]
    with Image.open(studio.sp.root/photo['file']) as im,Image.open(studio.sp.root/out['file']) as got:
        expected=im.transpose(Image.Transpose.TRANSPOSE)
        assert got.tobytes()==expected.tobytes()
        assert not got.getexif()
    s['crop']={'x':0,'y':0,'w':.5,'h':1};s['output']['w']=52
    other=I.transform(studio,s,'synthetic')['assets'][0];assert other['sha256']!=out['sha256']
    assert I.transform(studio,s,'synthetic')['assets'][0]['id']==other['id']
    assert I.sha256_file(studio.sp.root/photo['file'])==photo['sha256']

@pytest.mark.parametrize('field,value',[('rotate_quarters',True),('rotate_quarters',4),('rotate_quarters',float('nan')),('flip_x',1),('orientation','raw')])
def test_invalid_transform(studio,photo,field,value):
    s=spec(photo);s[field]=value
    with pytest.raises(ValueError):I.validate(studio.sp,s)

@pytest.mark.parametrize('section,field,value',[('crop','x',float('inf')),('crop','w',False),('crop','x',.4),('output','w',100000),('output','w',True),('adjust','warmth',3),('adjust','exposure_ev',.2)])
def test_invalid_nested_transform(studio,photo,section,field,value):
    s=spec(photo);s[section][field]=value
    with pytest.raises(ValueError):I.validate(studio.sp,s)

def layout(photo):
    return {'schema':'visual_layout.v1','template_kind':'photo','w':1080,'h':1350,'background':'#ffffff','elements':[{'id':'photo','kind':'image','asset':photo['id'],'x':0,'y':0,'w':1080,'h':800},{'id':'title','kind':'text','text':'Attention changes the work','font':'Newsreader','size':48,'color':'#18344b','align':'left','copy_kind':'authored','x':80,'y':900,'w':900,'h':220}]}

def test_design_revisions_pixel_identity_and_overflow(studio,photo):
    doc=W.save_design(studio,{'layout':layout(photo),'expected_version':0});a=W.render_design(studio,{'doc':doc['doc']['id'],'version':1,'sha256':doc['sha256']},'synthetic')
    changed=layout(photo);changed['elements'][1]['y']=1050
    newer=W.save_design(studio,{'id':doc['doc']['id'],'layout':changed,'expected_version':1});b=W.render_design(studio,{'doc':newer['doc']['id'],'version':2,'sha256':newer['sha256']},'synthetic')
    assert a['assets'][0]['sha256']!=b['assets'][0]['sha256'];assert W.load_design(studio.sp,doc['doc']['id'],1)['sha256']==doc['sha256']
    with pytest.raises(ValueError,match='changed'):W.save_design(studio,{'id':doc['doc']['id'],'layout':changed,'expected_version':1})
    changed['elements'][1]['h']=10;third=W.save_design(studio,{'id':doc['doc']['id'],'layout':changed,'expected_version':2})
    with pytest.raises(ValueError,match='overflow'):W.render_design(studio,{'doc':third['doc']['id'],'version':3,'sha256':third['sha256']},'synthetic')

def test_split_odd_pixels_and_partial_retry(studio,photo,monkeypatch):
    job={'id':'job_'+'e'*24,'by':'synthetic','args':{'asset':photo['id'],'rows':2,'columns':3,'tile_aspect':'original','mode':'drafts'}}
    original=store.save_post;count=0
    def interrupt(*args,**kw):
        nonlocal count
        count+=1
        if count==3:raise OSError('simulated disk full')
        return original(*args,**kw)
    monkeypatch.setattr(store,'save_post',interrupt)
    with pytest.raises(OSError):W.split(studio,job)
    monkeypatch.setattr(store,'save_post',original);result=W.split(studio,job)
    assert len(store.list_posts(studio.sp))==6
    assert W.split(studio,job)['posts']==result['posts']
    restored=Image.new('RGBA',(101,103))
    for tile in result['tiles']:
        asset=library.get(studio.sp,tile['asset']);x,y,x1,y1=tile['pixel_bounds']
        with Image.open(studio.sp.root/asset['file']) as im:restored.paste(im,(x,y))
    with Image.open(studio.sp.root/photo['file']) as im:assert im.tobytes()==restored.tobytes()
    assert M.load_latest(studio.sp,result['group'])['items'][0]['provenance']['split']['source_sha256']==photo['sha256']

def test_stale_attachment_retains_output(wb,studio,photo):
    post=model.new_post(idea='Synthetic image',by='synthetic');post['media']=[{'id':'m_source','asset':photo['id'],'slot':0,'role':'image','alt':'coordinate grid'}]
    post=store.save_post(studio.sp,post,expected_rev=None,actor=Actor('human','synthetic'),op='test')
    target={'post':post['id'],'variant':None,'item':'m_source','expected_rev':post['rev']}
    job=wb.start_media('image-transform',{'request_id':'stale-request','spec':spec(photo),'target':target})
    post['copy']['base']='newer copy';store.save_post(studio.sp,post,expected_rev=post['rev'],actor=Actor('human','synthetic'),op='test')
    done=finish(wb,job);assert done['status']=='done',done
    with pytest.raises(model.RevConflict): W.attach(studio,{'job_id':job['id'],'asset':done['result']['assets'][0]['asset'],'target':target},Actor('human','synthetic'))
    assert store.load_post(studio.sp,post['id'])['copy']['base']=='newer copy'
    assert wb.start_media('image-transform',job['args'])['id']==job['id']

def test_preview_recovery(studio,photo):
    path=I.rendition(studio.sp,photo['id'],'small');good=path.read_bytes();path.write_bytes(b'broken');assert I.rendition(studio.sp,photo['id'],'small').read_bytes()==good
    path.unlink();assert I.rendition(studio.sp,photo['id'],'small').read_bytes()==good
    with pytest.raises(ValueError):I.rendition(studio.sp,photo['id'],'../../raw')

@pytest.mark.parametrize('suffix',['txt','docx','pdf'])
def test_full_manuscript_jobs(wb,studio,suffix):
    p=Path('/private/tmp/ave-manuscript-fixtures-20261029')/('long-manuscript.'+suffix)
    if not p.exists():pytest.skip('Prepared synthetic manuscript unavailable')
    uploaded=upload(wb,p.read_bytes(),p.name);done=finish(wb,uploaded['job']);assert done['status']=='done',done
    src=next(s for s in wb.overview()['sources'] if s['source_id']==uploaded['source_id'])
    text='';last=None
    for n in range(src['pages']):
        page=manuscripts.page(studio.sp,src['source_id'],n);assert len(page['sections'])<=25
        text+=' '.join(r['text'] for r in page['sections']);last=page['sections'][-1]
    assert all(marker in text for marker in ('CH01-P01','CH24-P08','CH48-P16'));assert len(text)>400000
    assert manuscripts.section(studio.sp,src['source_id'],last['id'])==last
    assert hashlib.sha256(p.read_bytes()).hexdigest()==src['sha256']

@pytest.mark.parametrize('name,match',[('image-only-no-text.pdf','OCR required')])
def test_scanned_manuscript_job_fails_honestly(wb,name,match):
    p=Path('/private/tmp/ave-manuscript-fixtures-20261029')/name
    if not p.exists():pytest.skip('Prepared synthetic manuscript unavailable')
    up=upload(wb,p.read_bytes(),p.name);done=finish(wb,up['job']);assert done['status']=='failed';assert match in done['diagnostics']

@pytest.mark.parametrize('name',['bounded-high-expansion.docx','encrypted-manuscript.pdf','missing-document.docx','not-really-a-pdf.pdf','traversal-entry.docx','truncated.pdf'])
def test_edge_document_jobs(wb,name):
    root=Path('/private/tmp/ave-document-edge-fixtures-20261029');p=root/name
    if not p.exists():pytest.skip('Specific prepared edge input unavailable')
    up=upload(wb,p.read_bytes(),p.name);done=finish(wb,up['job']);assert done['status']=='failed',done
    assert not next(s for s in wb.overview()['sources'] if s['source_id']==up['source_id'])['ready']

def test_exif_alpha_metadata_and_adjustments(studio,tmp_path):
    p=tmp_path/'oriented.jpg';im=Image.new('RGB',(80,40),(100,100,100));exif=im.getexif();exif[274]=6;exif[270]='Location metadata must not propagate';im.save(p,exif=exif)
    a=library.add_file(studio.sp,p,source={'type':'own'},by='synthetic');assert I.capabilities(studio.sp,a['id'])['w']==40
    s=spec(a);s['output'].update(w=40,h=80);s['preset']=None;s['adjust'].update(exposure_ev=1,warmth=.5)
    out=I.transform(studio,s,'synthetic')['assets'][0]
    with Image.open(studio.sp.root/out['file']) as got:
        assert got.size==(40,80);assert not got.getexif();r,g,b,alpha=got.getpixel((20,40));assert (r,g,b,alpha)==(216,200,184,255)
    assert I.sha256_file(p)==a['sha256']

def test_quote_mutation_refused_and_authored_label_preserved(studio,photo):
    from ave.content.visual import validate
    l=layout(photo);l['elements'][1].update(copy_kind='quote',text='This sentence was altered.')
    with pytest.raises(ValueError,match='verbatim'):validate(l,{'text':'The original sentence is different.'})
    l['elements'][1]['copy_kind']='authored';doc=W.save_design(studio,{'layout':l,'expected_version':0})
    assert doc['doc']['items'][0]['visual']['elements'][1]['text']=='This sentence was altered.'

def test_symlink_and_raw_refusal(studio,photo,tmp_path):
    recpath=studio.sp.root/photo['file'];outside=tmp_path/'outside';outside.write_bytes(recpath.read_bytes());recpath.unlink();recpath.symlink_to(outside)
    with pytest.raises(ValueError,match='symlink'):I.capabilities(studio.sp,photo['id'])

def test_native_document_intake_adapter(wb,studio):
    from ave.desktop_files import import_selected
    p=Path('/private/tmp/ave-manuscript-fixtures-20261029/long-manuscript.pdf')
    if not p.exists():pytest.skip('Prepared PDF unavailable')
    result=import_selected(wb,[p]);src=next(s for s in wb.overview()['sources'] if s['source_id']==result[0]['source_id'])
    assert src['ready'] and src['kind']=='document';assert 'CH48-P16' in ' '.join(x['text'] for x in manuscripts.page(studio.sp,src['source_id'],src['pages']-1)['sections'])

def test_visual_snapshot_roundtrip_and_no_authority(studio,photo):
    from ave.content import visual_snapshot as S
    info=W.save_design(studio,{'layout':layout(photo),'expected_version':0});snapshot=S.export(studio.sp,info['doc']['id'],1)
    assert S.validate(json.loads(S.canonical(snapshot)))==snapshot
    assert snapshot['document']['sha256']==info['sha256'] and snapshot['snapshot_sha256']!=info['sha256']
    for mutate in [lambda x:x.update(approved=True),lambda x:x['document'].update(id='../secret'),lambda x:x['layout']['elements'][0].update(x=float('nan')),lambda x:x['identities']['fonts'].update(Inter='/tmp/font.ttf'),lambda x:x['assets'][0].update(file='/tmp/photo'),lambda x:x.update(untrusted_data=False)]:
        invalid=copy.deepcopy(snapshot);mutate(invalid)
        with pytest.raises((ValueError,TypeError)):S.validate(invalid)

def test_contentdoc_concurrent_revision_allocation(studio,photo):
    from concurrent.futures import ThreadPoolExecutor
    info=W.save_design(studio,{'layout':layout(photo),'expected_version':0})
    def save():
        try:return W.save_design(studio,{'id':info['doc']['id'],'layout':layout(photo),'expected_version':1})['version']
        except ValueError:return 'conflict'
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda _:save(),range(2)))
    assert sorted(map(str,results))==['2','conflict'];assert M.list_versions(studio.sp,info['doc']['id'])==[1,2]

@pytest.mark.parametrize('encoding',['utf-8','utf-16','utf-16-le','utf-16-be'])
def test_docx_entity_encoding_refusal(tmp_path,encoding):
    import zipfile
    p=tmp_path/'entities.docx';xml='<?xml version="1.0"?><!DOCTYPE x [<!ENTITY secret "hidden">]><x>&secret;</x>'
    with zipfile.ZipFile(p,'w') as z:z.writestr('word/document.xml',xml.encode(encoding))
    with pytest.raises(ValueError,match='entities|UTF-8'):manuscripts.extract(p)

@pytest.mark.parametrize('field,value',[('version',None),('version',True),('version',0),('sha256',None),('sha256',''),('sha256','0'*63)])
def test_render_job_requires_captured_identity(wb,studio,photo,field,value):
    doc=W.save_design(studio,{'layout':layout(photo),'expected_version':0})
    args={'request_id':'identity-test','doc':doc['doc']['id'],'version':1,'sha256':doc['sha256']};args[field]=value
    before=wb.overview()['jobs']
    with pytest.raises(ValueError):wb.start_media('design-render',args)
    assert wb.overview()['jobs']==before
    args.pop(field)
    with pytest.raises(ValueError):wb.start_media('design-render',args)


def test_captured_design_does_not_follow_latest(studio,photo):
    first=W.save_design(studio,{'layout':layout(photo),'expected_version':0});args={'doc':first['doc']['id'],'version':1,'sha256':first['sha256']}
    changed=layout(photo);changed['elements'][1]['text']='New revision'
    W.save_design(studio,{'id':first['doc']['id'],'layout':changed,'expected_version':1})
    result=W.render_design(studio,args,'synthetic');assert result['version']==1 and result['doc_sha256']==first['sha256']


def test_text_locators_match_original_slices(tmp_path):
    original='   First sentence.\n\nSecond sentence.  \n\n   '+('x '*8100)+'  \n'
    p=tmp_path/'spaces.txt';p.write_text(original)
    rows=manuscripts.extract(p)
    assert rows[0]['start']==3 and rows[0]['text']=='First sentence.'
    assert any(len(r['text'])==8000 for r in rows)
    for row in rows:assert original[row['start']:row['end']]==row['text']


def write_docx(path,body,extra=None):
    import zipfile
    with zipfile.ZipFile(path,'w') as z:
        z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'+body+'</w:body></w:document>')
        if extra:z.writestr(extra,'<text>Ancillary words</text>')


def test_docx_inline_semantics_and_table_order(tmp_path):
    p=tmp_path/'inline.docx';write_docx(p,'<w:p><w:r><w:t>Before</w:t><w:tab/><w:t>after tab</w:t><w:br/><w:t>after break</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Table cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl><w:p><w:r><w:t>After table</w:t></w:r></w:p>')
    extracted=manuscripts.extract(p,with_status=True)
    assert extracted['text']=='Before\tafter tab\nafter break\n\nTable cell\n\nAfter table'
    for r in extracted['sections']:assert extracted['text'][r['start']:r['end']]==r['text']

@pytest.mark.parametrize('body,extra',[('<w:p><w:r><w:instrText>hidden field</w:instrText></w:r></w:p>',None),('<w:altChunk/>',None),('<w:p><w:pPr><w:numPr/></w:pPr><w:r><w:t>Numbered item</w:t></w:r></w:p>',None),('<w:p><w:r><w:t>body</w:t></w:r></w:p>','word/comments.xml')])
def test_docx_unsupported_text_refused(tmp_path,body,extra):
    p=tmp_path/'unsupported.docx';write_docx(p,body,extra)
    with pytest.raises(ValueError,match='unsupported|comments'):manuscripts.extract(p)


def mixed_pdf(path):
    """Page 1 has selectable text, page 2 has none, like a part-scanned manuscript."""
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject,DictionaryObject,NameObject
    writer=PdfWriter();page=writer.add_blank_page(612,792);writer.add_blank_page(612,792)
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
    stream=DecodedStreamObject();stream.set_data(b'BT /F1 12 Tf 72 720 Td (CH01-P01 The first paragraph of a synthetic manuscript chapter.) Tj ET')
    page[NameObject('/Contents')]=writer._add_object(stream);writer.write(path)


def test_mixed_pdf_retains_page_status(wb,studio,tmp_path):
    p=tmp_path/'mixed.pdf';mixed_pdf(p)
    result=upload(wb,p.read_bytes(),'mixed.pdf');job=finish(wb,result['job']);assert job['status']=='done'
    assert job['result']['extraction_complete'] is False
    assert job['result']['page_status'][1]['status']=='no_selectable_text'
    data=manuscripts.page(studio.sp,result['source_id']);assert 'PDF page 2' in data['warnings'][0]
    original=(studio.sp.work/'production/manuscripts'/result['source_id']/'extracted.txt').read_text()
    for row in data['sections']:assert original[row['start']:row['end']]==row['text']


def test_split_document_snapshot_roundtrip(studio,photo):
    from ave.content import visual_snapshot as S
    result=W.split(studio,{'id':'job_'+'d'*24,'by':'synthetic','args':{'asset':photo['id'],'rows':2,'columns':3,'tile_aspect':'original','mode':'drafts'}})
    snapshot=S.export(studio.sp,result['group'],1)
    assert S.validate(json.loads(S.canonical(snapshot)))==snapshot
    assert snapshot['assets']==[{'id':photo['id'],'sha256':photo['sha256']}]
    assert 'split' not in snapshot and not any(pid in S.canonical(snapshot).decode() for pid in result['posts'])


def test_identical_pixels_keep_separate_provenance(studio,tmp_path,monkeypatch):
    from ave.library import registry
    originals=[]
    for n,color in enumerate(('red','blue')):
        p=tmp_path/f'parent-{n}.png';im=Image.new('RGB',(100,100),'white');im.paste(color,(50,0,100,100));im.save(p)
        originals.append(library.add_file(studio.sp,p,source={'type':'stock'},license_entry={'source':'synthetic','url':f'https://example.invalid/{n}','license':f'Fixture {n}','license_url':'https://example.invalid/license','license_text':'Synthetic test license','attribution':{'required':True,'text':f'Credit {n}'}},attested=n==0,by='synthetic'))
    outputs=[]
    for original in originals:
        operation=spec(original);operation['crop']['w']=.5;operation['output'].update(w=50,h=100)
        outputs.append(I.transform(studio,operation,'synthetic')['assets'][0])
    first,second=outputs
    assert first['sha256']==second['sha256'] and first['file']==second['file'] and first['id']!=second['id']
    assert first['derived_from']==[originals[0]['id']] and second['derived_from']==[originals[1]['id']]
    assert first['origin']['image_transform']['source_asset']==originals[0]['id']
    assert library.coverage(studio.sp,[first['id']])['attributions']==['Credit 0']
    assert library.coverage(studio.sp,[first['id']])['ok']
    assert not library.coverage(studio.sp,[second['id']])['ok']
    history=registry.assets_path(studio.sp).read_bytes()
    again=I.transform(studio,operation,'synthetic')['assets'][0];assert again['id']==second['id']
    assert registry.assets_path(studio.sp).read_bytes()==history and library.get(studio.sp,first['id'])=={k:v for k,v in first.items() if k not in ('asset','url','preview_url')}
    # A prefix collision with an ordinary imported record must refuse, not reuse it.
    monkeypatch.setattr(model,'asset_id',lambda _:originals[0]['id'])
    with pytest.raises(ValueError,match='collision'):library.add_derivative(studio.sp,studio.sp.root/second['file'],derived_from=[originals[1]['id']],origin={'test':'collision'},by='synthetic')
    assert registry.assets_path(studio.sp).read_bytes()==history


def test_pixel_equal_design_revisions_and_split_occurrences(studio,photo,tmp_path):
    from ave.content import visual_snapshot as S
    first=W.save_design(studio,{'layout':layout(photo),'expected_version':0});second=W.save_design(studio,{'id':first['doc']['id'],'layout':layout(photo),'expected_version':1})
    outputs=[W.render_design(studio,{'doc':d['doc']['id'],'version':d['version'],'sha256':d['sha256']},'synthetic')['assets'][0] for d in (first,second)]
    assert outputs[0]['sha256']==outputs[1]['sha256'] and outputs[0]['id']!=outputs[1]['id']
    assert [a['origin']['version'] for a in outputs]==[1,2]
    nested=layout(outputs[1]);nested['elements'][0]['asset']=outputs[1]['id'];doc=W.save_design(studio,{'layout':nested,'expected_version':0});snapshot=S.export(studio.sp,doc['doc']['id'],1)
    assert snapshot['assets'][0]['derivation_sha256']==outputs[1]['derivation']['sha256'];assert S.validate(snapshot)==snapshot
    p=tmp_path/'uniform.png';Image.new('RGB',(96,96),'white').save(p);a=library.add_file(studio.sp,p,source={'type':'own'},by='synthetic')
    assert library.add_file(studio.sp,p,source={'type':'own'},by='synthetic')['id']==a['id']
    result=W.split(studio,{'id':'job_'+'b'*24,'by':'synthetic','args':{'asset':a['id'],'rows':2,'columns':2,'tile_aspect':'original','mode':'carousel'}})
    assert len({t['asset'] for t in result['tiles']})==4
    assert len({t['sha256'] for t in result['assets']})==1
    assert [t['origin']['index'] for t in result['assets']]==[0,1,2,3]

@pytest.mark.parametrize('size,rows,columns,aspect',[((63,100),1,1,'original'),((128,128),1,6,'1:1')])
def test_split_invalid_effective_master_has_no_side_effects(wb,studio,tmp_path,size,rows,columns,aspect):
    from ave.library import registry
    p=tmp_path/'small.png';Image.new('RGB',size,'white').save(p);a=library.add_file(studio.sp,p,source={'type':'own'},by='synthetic')
    args={'request_id':'too-small-master','asset':a['id'],'rows':rows,'columns':columns,'tile_aspect':aspect,'mode':'drafts'}
    before=registry.assets_path(studio.sp).read_bytes();jobs=wb.overview()['jobs'];posts=store.list_posts(studio.sp)
    with pytest.raises(ValueError,match='at least 64'):wb.start_media('image-split',args)
    with pytest.raises(ValueError,match='at least 64'):W.split(studio,{'id':'job_'+'a'*24,'by':'synthetic','args':args})
    assert registry.assets_path(studio.sp).read_bytes()==before and store.list_posts(studio.sp)==posts and wb.overview()['jobs']==jobs
    assert not (studio.sp.work/'production/splits').exists()
    assert not M.doc_ids(studio.sp)


def test_split_minimum64_master_is_supported(studio,tmp_path):
    p=tmp_path/'minimum.png';Image.new('RGB',(64,64),'white').save(p);a=library.add_file(studio.sp,p,source={'type':'own'},by='synthetic')
    args={'asset':a['id'],'rows':2,'columns':2,'tile_aspect':'original','mode':'carousel'}
    result=W.split(studio,{'id':'job_'+'c'*24,'by':'synthetic','args':args})
    assert len(result['tiles'])==4 and all((a['w'],a['h'])==(32,32) for a in result['assets'])
    doc=M.load_latest(studio.sp,result['group']);assert doc['items'][0]['visual']['w']==64
