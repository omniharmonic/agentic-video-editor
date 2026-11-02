"""Real workshop journeys in the actual shell, with decoded outputs and saved state."""
import hashlib
import json
import threading
from pathlib import Path
import pytest
from playwright.sync_api import expect
from PIL import Image
from ave import library
from ave.studio import store,model
from ave.studio.events import Actor
from .test_workbench_browser import browser,rig,wait_for
from .test_media_workshop import spec

EVIDENCE=Path('/private/tmp/ave-media-image-evidence');EVIDENCE.mkdir(exist_ok=True)

@pytest.mark.parametrize('width',[1440,390])
def test_canvas_journey(rig,browser,width,tmp_path):
    studio,server=rig
    im=Image.new('RGB',(1200,900),'#507ca0');p=tmp_path/'photo.png'
    from PIL import ImageDraw
    draw=ImageDraw.Draw(im);draw.rectangle((0,0,600,450),fill='#f1c261');draw.text((80,90),'ORIGINAL / FIELD NOTES',fill='black');im.save(p)
    a=library.add_file(studio.sp,p,source={'type':'own'},by='synthetic')
    post=model.new_post(idea='Field notes',by='synthetic');post['media']=[{'id':'m_photo','slot':0,'asset':a['id'],'role':'image','alt':'Synthetic field notes image'}]
    post=store.save_post(studio.sp,post,expected_rev=None,actor=Actor('human','synthetic'),op='test')
    page=browser.new_page(viewport={'width':width,'height':960 if width==1440 else 844});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        page.goto(server.url()+'#/post/'+post['id']);page.get_by_role('button',name='Edit image / applied crop').wait_for();page.screenshot(path=str(EVIDENCE/f'composer-{width}-toolbar.png'));page.get_by_role('button',name='Edit image / applied crop').click()
        page.get_by_role('button',name='Render image',exact=True).wait_for()
        page.screenshot(path=str(EVIDENCE/f'canvas-{width}-initial.png'))
        box=page.locator('.mw-board canvas').bounding_box();page.mouse.move(box['x']+box['width']*.1,box['y']+box['height']*.1);page.mouse.down();page.mouse.move(box['x']+box['width']*.9,box['y']+box['height']*.9);page.mouse.up()
        page.get_by_text('Crop & size',exact=True).click();page.get_by_label('Crop x',exact=True).fill('0.12');page.get_by_label('Crop x',exact=True).press('ArrowUp')
        page.get_by_role('button',name='Rotate 90°',exact=True).click();page.get_by_label('Exposure (EV)',exact=True).fill('.5')
        page.get_by_label('Contrast',exact=True).press('ArrowUp')
        page.get_by_role('button',name='Render image',exact=True).click();page.get_by_role('button',name='Use in this slide',exact=True).wait_for(timeout=20000)
        assert page.locator('.mw-board>img').evaluate('(el)=>el.complete && el.naturalWidth>0')
        page.screenshot(path=str(EVIDENCE/f'canvas-{width}-render.png'))
        page.get_by_role('button',name='Use in this slide',exact=True).click();page.locator('.media-workshop').wait_for(state='detached')
        saved=store.load_post(studio.sp,post['id']);assert saved['media'][0]['asset']!=a['id'];assert hashlib.sha256(p.read_bytes()).hexdigest()==a['sha256']
        page.get_by_role('button',name='Edit image / applied crop').click();page.get_by_role('button',name='Design',exact=True).click();page.get_by_label('Selected layer').select_option('title')
        page.get_by_label('Layer text').fill('A closer look');page.get_by_label('Interactive draft canvas. Arrow keys move the selected design element.').focus();page.keyboard.press('Shift+ArrowUp')
        canvas=page.locator('.mw-board canvas');box=canvas.bounding_box();page.mouse.move(box['x']+box['width']*.4,box['y']+box['height']*.75);page.mouse.down();page.mouse.move(box['x']+box['width']*.42,box['y']+box['height']*.73);page.mouse.up()
        page.get_by_role('button',name='Layer backward',exact=True).click();page.get_by_role('button',name='Layer forward',exact=True).click()
        page.get_by_text('Position & size',exact=True).click()
        for key,value in {'x':'80','y':'80','w':'600','h':'200'}.items():page.get_by_label('Layer '+key,exact=True).fill(value)
        canvas.scroll_into_view_if_needed();box=canvas.bounding_box();size=canvas.evaluate('(el)=>({w:el.width,h:el.height})')
        px=box['x']+676*box['width']/size['w'];py=box['y']+276*box['height']/size['h']
        page.mouse.move(px,py);page.mouse.down();page.mouse.move(px-20,py-10,steps=3);page.mouse.up()
        page.get_by_text('Position & size',exact=True).click()
        assert float(page.get_by_label('Layer w',exact=True).input_value())<600
        assert float(page.get_by_label('Layer h',exact=True).input_value())<200
        page.get_by_label('Layer w',exact=True).fill('900');page.get_by_label('Layer h',exact=True).fill('300')
        page.screenshot(path=str(EVIDENCE/f'design-{width}-initial.png'))
        page.get_by_role('button',name='Save design revision',exact=True).click();page.get_by_label('Saved revision').wait_for()
        page.get_by_role('button',name='Render saved design',exact=True).click();page.get_by_role('button',name='Use in this slide',exact=True).wait_for(timeout=20000)
        page.screenshot(path=str(EVIDENCE/f'design-{width}-render.png'))
        page.get_by_role('button',name='Continue editing',exact=True).click();page.get_by_label('Layer text').fill('Another view');page.get_by_role('button',name='Save design revision',exact=True).click();expect(page.get_by_label('Saved revision')).to_have_value('2');page.get_by_label('Saved revision').select_option('1')
        expect(page.get_by_label('Layer text')).to_have_value('A closer look')
        assert page.locator('body').evaluate('(el)=>el.scrollWidth<=window.innerWidth');assert not errors
        jobs=server.app.workbench.overview()['jobs'];(EVIDENCE/f'journey-{width}.json').write_text(json.dumps({'source':a,'post':saved,'jobs':jobs},indent=2))
    finally:page.close()

@pytest.mark.parametrize('width',[1440,390])
def test_real_photo_and_split(rig,browser,width):
    studio,server=rig
    p=Path('/private/tmp/ave-photo-fixtures-20261029/notebook.jpg')
    if not p.exists():pytest.skip('Prepared CC0 photo unavailable')
    a=library.add_file(studio.sp,p,source={'type':'stock','provider':'fixture'},license_entry={'source':'fixture','url':'https://commons.wikimedia.org/wiki/File:A_notebook.jpg','license':'CC0-1.0','license_url':'https://creativecommons.org/publicdomain/zero/1.0/','license_text':'Prepared public CC0 fixture; no live attestation'},by='synthetic')
    page=browser.new_page(viewport={'width':width,'height':960 if width==1440 else 844})
    try:
        page.goto(server.url()+'#/library');page.locator('.lib-open').first.click();page.get_by_role('button',name='Edit image / design').click();page.get_by_role('button',name='Render image',exact=True).wait_for()
        page.get_by_label('Crop aspect',exact=True).select_option('4:5');page.get_by_label('Exposure (EV) slider').focus();page.keyboard.press('ArrowRight');page.get_by_label('Preset',exact=True).select_option('warm')
        page.screenshot(path=str(EVIDENCE/f'photo-{width}-draft.png'))
        page.get_by_role('button',name='Render image',exact=True).click();page.get_by_role('link',name='Open full-resolution output').wait_for(timeout=20000)
        assert page.locator('.mw-board>img').evaluate('(el)=>el.complete&&el.naturalWidth>0');page.screenshot(path=str(EVIDENCE/f'photo-{width}-render.png'))
        page.get_by_role('button',name='Split',exact=True).click();page.get_by_label('Rows',exact=True).fill('2');page.get_by_label('Columns',exact=True).fill('3');page.get_by_label('Destination',exact=True).select_option('drafts');page.get_by_role('button',name='Render ordered tiles').click();page.get_by_role('link',name='Open draft 6',exact=True).wait_for(timeout=25000)
        expect(page.get_by_role('link',name='Open first of 6 drafts',exact=True)).to_be_visible();assert page.locator('.mw-tiles img').evaluate_all('(els)=>els.length===6&&els.every(e=>e.complete&&e.naturalWidth>0)')
        page.screenshot(path=str(EVIDENCE/f'split-{width}-result.png'));page.get_by_role('button',name='View tile order',exact=True).click();page.screenshot(path=str(EVIDENCE/f'split-{width}-arrangement.png'));assert len(store.list_posts(studio.sp))==6
        assert hashlib.sha256(p.read_bytes()).hexdigest()==a['sha256']
    finally:page.close()

def test_75_asset_actual_lazy_renditions(rig,browser):
    studio,server=rig;root=Path('/private/tmp/ave-preview-load-fixtures-20261029')
    if not (root/'manifest.json').exists():pytest.skip('Prepared75 inputs unavailable')
    manifest=json.loads((root/'manifest.json').read_text());assets=[]
    for src in manifest['sources']:
        assets.append(library.add_file(studio.sp,root/src['file'],source={'type':'own'},by='synthetic',alt=src['label']))
    page=browser.new_page(viewport={'width':1440,'height':960});responses=[]
    page.on('response',lambda r:responses.append(r) if '/workbench/preview/' in r.url else None)
    try:
        page.goto(server.url()+'#/library');page.locator('.lib-grid img').first.wait_for();page.wait_for_timeout(800)
        initial=len(responses);assert 0<initial<75
        imgs=page.locator('.lib-grid img');assert imgs.count()==75
        for i in range(75):
            im=imgs.nth(i);im.scroll_into_view_if_needed();wait_for(lambda:im.evaluate('(el)=>el.complete&&el.naturalWidth>0'),page)
            assert im.evaluate('(el)=>Math.max(el.naturalWidth,el.naturalHeight)')<=320
        received={r.url:len(r.body()) for r in responses if r.status==200};assert len(received)==75
        assert sum(received.values())<manifest['total_bytes']/3
        from ave.studio.image_workshop import rendition
        cache=rendition(studio.sp,assets[0]['id'],'small');cache.write_bytes(b'interrupted cache')
        response=page.request.get(server.url().split('?')[0]+'api/studio/workbench/preview/'+assets[0]['id']+'/small');assert response.ok;assert response.body().startswith(b'\x89PNG')
        for src in manifest['sources']:assert hashlib.sha256((root/src['file']).read_bytes()).hexdigest()==src['sha256']
        (EVIDENCE/'75-renditions.json').write_text(json.dumps({'original_bytes':manifest['total_bytes'],'initial_requests':initial,'all_requests':len(received),'requested_preview_bytes':sum(received.values()),'max_decoded_edge':320,'sources':manifest['sources']},indent=2))
        page.screenshot(path=str(EVIDENCE/'75-library.png'))
    finally:page.close()

@pytest.mark.parametrize('width',[1440,390])
def test_manuscript_gui_first_middle_final(rig,browser,width):
    studio,server=rig;p=Path('/private/tmp/ave-manuscript-fixtures-20261029/long-manuscript.docx')
    design_image=studio.sp.work/'manuscript-design.png';Image.new('RGB',(640,480),'#e9f0f7').save(design_image);library.add_file(studio.sp,design_image,source={'type':'own'},by='synthetic',alt='Manuscript design source')
    if not p.exists():pytest.skip('Prepared manuscript unavailable')
    page=browser.new_page(viewport={'width':width,'height':960 if width==1440 else 844});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        page.goto(server.url()+'#/workbench');page.get_by_label('Upload source file').set_input_files(str(p));page.get_by_role('button',name='Import source',exact=True).click();page.get_by_label('Manuscript section page').wait_for(timeout=20000)
        assert page.locator('.wb-sentences').inner_text().find('CH01-P01')>=0
        src=server.app.workbench.overview()['sources'][0];middle=src['pages']//2
        from ave.content import manifest as M
        from ave.studio.manuscripts import section
        chosen_passages=[]
        def choose_visible(label):
            before=set(M.doc_ids(studio.sp));article=page.locator('.wb-sentence').first
            article.get_by_role('button',name='Design from this section').click();page.locator('.lib-picker .lib-open').first.click();page.get_by_role('button',name='Save design revision',exact=True).click();page.get_by_label('Saved revision').wait_for()
            did=next(iter(set(M.doc_ids(studio.sp))-before));doc=M.load_latest(studio.sp,did);selected=doc['sources']['writing'];row=section(studio.sp,src['source_id'],selected['locator']['id'])
            assert selected['text']==row['text'] and selected['locator']=={k:row[k] for k in ('id','start','end','page')} and selected['file']==src['source_id']
            chosen_passages.append({'position':label,'selection':selected,'document':did});page.get_by_role('button',name='Close editor',exact=True).click()
        choose_visible('first')
        page.get_by_label('Manuscript section page').fill(str(middle));page.get_by_role('button',name='Go to sections',exact=True).click();expect(page.get_by_label('Manuscript section page')).to_have_value(str(middle))
        page.screenshot(path=str(EVIDENCE/f'manuscript-{width}-middle.png'));choose_visible('middle')
        page.get_by_label('Manuscript section page').fill(str(src['pages']));page.get_by_role('button',name='Go to sections',exact=True).click();expect(page.locator('.wb-sentences')).to_contain_text('CH48-P16')
        page.screenshot(path=str(EVIDENCE/f'manuscript-{width}-final.png'));assert not errors
        article=page.locator('.wb-sentence').filter(has_text='CH48-P16');article.get_by_role('button',name='Design from this section').click();page.locator('.lib-picker .lib-open').first.click();page.get_by_role('button',name='Save design revision',exact=True).click();page.get_by_label('Saved revision').wait_for()
        from ave.content import manifest as M
        from ave.studio.manuscripts import section
        did=next(d for d in M.doc_ids(studio.sp) if d.startswith('design_') and 'CH48-P16' in M.load_latest(studio.sp,d)['sources']['writing']['text']);doc=M.load_latest(studio.sp,did);selected=doc['sources']['writing'];row=section(studio.sp,src['source_id'],selected['locator']['id'])
        assert row['text']==selected['text'] and selected['locator']['start']>400000 and 'CH48-P16' in selected['text'];assert selected['file']==src['source_id']
        (EVIDENCE/f'manuscript-{width}-selected.json').write_text(json.dumps({'source_sha256':src['sha256'],'selection':selected,'document':doc['id'],'revision':doc['saved']['version'],'first_and_middle':chosen_passages},indent=2))
        assert hashlib.sha256(p.read_bytes()).hexdigest()==src['sha256']
    finally:page.close()

@pytest.mark.parametrize('width',[1440,390])
def test_reopened_design_layer_sources_and_fit(rig,browser,width,tmp_path):
    from ave.studio import media_workshop as W
    studio,server=rig;assets=[]
    for n,colors in enumerate((('red','blue','green'),('yellow','purple','cyan'))):
        im=Image.new('RGB',(300,100));im.paste(colors[0],(0,0,100,100));im.paste(colors[1],(100,0,200,100));im.paste(colors[2],(200,0,300,100));p=tmp_path/f'fit-{n}.png';im.save(p);assets.append(library.add_file(studio.sp,p,source={'type':'own'},by='synthetic'))
    layout={'schema':'visual_layout.v1','template_kind':'photo','w':400,'h':200,'background':'#ffffff','elements':[{'id':'left','kind':'image','asset':assets[0]['id'],'x':0,'y':0,'w':150,'h':150},{'id':'right','kind':'image','asset':assets[1]['id'],'x':200,'y':0,'w':150,'h':150}]}
    first=W.save_design(studio,{'layout':layout,'expected_version':0});finished=W.render_design(studio,{'doc':first['doc']['id'],'version':1,'sha256':first['sha256']},'synthetic')['assets'][0]
    changed=json.loads(json.dumps(layout));changed['elements'][0]['asset']=assets[1]['id'];changed['elements'][1]['asset']=assets[0]['id']
    W.save_design(studio,{'id':first['doc']['id'],'layout':changed,'expected_version':1})
    other=W.save_design(studio,{'layout':changed,'expected_version':0})
    post=model.new_post(idea='Reopen exact finished design',by='synthetic');post['media']=[{'id':'m_design','asset':finished['id'],'role':'image','slot':0,'alt':'Two distinct sources'}];post=store.save_post(studio.sp,post,expected_rev=None,actor=Actor('human','synthetic'),op='test')
    page=browser.new_page(viewport={'width':width,'height':960 if width==1440 else 844})
    try:
        page.goto(server.url()+'#/post/'+post['id']);page.get_by_role('button',name='Edit image / applied crop').click()
        expect(page.get_by_label('Saved revision')).to_have_value('1');expect(page.get_by_label('Canvas',exact=True)).to_have_value('Custom 400 × 200')
        canvas=page.locator('.mw-board canvas');expect(canvas).to_have_attribute('aria-busy','false')
        pixel=lambda x:canvas.evaluate('(el,x)=>Array.from(el.getContext("2d").getImageData(x,75,1,1).data)',x)
        assert pixel(25)==[0,0,255,255] and pixel(225)==[128,0,128,255]
        page.screenshot(path=str(EVIDENCE/f'fix1-reopened-{width}.png'))
        page.get_by_label('Saved design',exact=True).select_option(other['doc']['id']);expect(canvas).to_have_attribute('aria-busy','false')
        wait_for(lambda:pixel(25)==[128,0,128,255],page);assert pixel(225)==[0,0,255,255]
        page.screenshot(path=str(EVIDENCE/f'fix1-other-design-{width}.png'))
        page.get_by_label('Canvas',exact=True).select_option('4:5');expect(page.get_by_label('Canvas',exact=True)).to_have_value('4:5')
        assert canvas.evaluate('(el)=>[el.width,el.height]')==[1080,1350]
    finally:page.close()

@pytest.mark.parametrize('width',[1440,390])
def test_mixed_document_gui_discloses_missing_text(rig,browser,width,tmp_path):
    from .test_media_workshop import mixed_pdf,write_docx
    studio,server=rig;p=tmp_path/'mixed-text-scanned.pdf';mixed_pdf(p)
    page=browser.new_page(viewport={'width':width,'height':960 if width==1440 else 844})
    try:
        page.goto(server.url()+'#/workbench');page.get_by_label('Upload source file').set_input_files(str(p));page.get_by_role('button',name='Import source',exact=True).click()
        expect(page.locator('.wb-source-warnings')).to_contain_text('PDF page 2: no selectable text',timeout=20000)
        expect(page.locator('.wb-sentences')).to_contain_text('CH01-P01')
        page.screenshot(path=str(EVIDENCE/f'fix1-mixed-pdf-{width}.png'))
        doc=tmp_path/'inline.docx';write_docx(doc,'<w:p><w:r><w:t>  Before</w:t><w:tab/><w:t>after tab</w:t><w:br/><w:t>after break  </w:t></w:r></w:p>')
        page.get_by_role('button',name='Add source',exact=True).click();page.get_by_label('Upload source file').set_input_files(str(doc));page.get_by_role('button',name='Import source',exact=True).click()
        expect(page.locator('.wb-sentence p')).to_have_text('Before\tafter tab\nafter break',timeout=20000)
        assert page.locator('.wb-sentence p').evaluate('(el)=>getComputedStyle(el).whiteSpace')=='pre-wrap'
        page.screenshot(path=str(EVIDENCE/f'fix1-docx-text-{width}.png'))
        source=next(s for s in server.app.workbench.overview()['sources'] if s['label']=='inline.docx')
        from ave.studio.manuscripts import page as source_page
        row=source_page(studio.sp,source['source_id'])['sections'][0]
        canonical=(studio.sp.work/'production/manuscripts'/source['source_id']/'extracted.txt').read_text()
        assert canonical[row['start']:row['end']]==row['text'] and row['start']==2
    finally:page.close()
