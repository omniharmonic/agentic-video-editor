"""Actual app worker, pointer/keyboard authoring and decoded rendered playback."""
import hashlib
import json
import tempfile
import os
from pathlib import Path
import pytest
from playwright.sync_api import expect
from .test_workbench_browser import browser,rig,wait_for

EVIDENCE=Path(tempfile.gettempdir())/'ave-media-video-fix1-evidence';EVIDENCE.mkdir(exist_ok=True)

@pytest.mark.parametrize('width',[1440,390])
def test_video_gui(rig,browser,width,tmp_path):
    studio,server=rig
    from .video_fixtures import picture
    source=picture(tmp_path/'source.mov')
    original=hashlib.sha256(source.read_bytes()).hexdigest()
    page=browser.new_page(viewport={'width':width,'height':960 if width==1440 else 844});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        page.goto(server.url()+'#/workbench');page.get_by_label('Upload source file').set_input_files(str(source));page.get_by_role('button',name='Import source',exact=True).click()
        page.get_by_role('button',name='Edit in Media',exact=True).wait_for(timeout=30000)
        wait_for(lambda:server.app.workbench.overview()['sources'][0].get('ready'),page,seconds=30)
        page.get_by_role('button',name='Edit in Media',exact=True).click();page.get_by_label('Programme title').fill('Synthetic camera study');page.get_by_role('button',name='Create programme',exact=True).click()
        page.get_by_label('Source in (seconds)',exact=True).wait_for(timeout=30000)
        player=page.get_by_label('Source playback');player.evaluate('(el)=>{el.muted=true;return el.play();}')
        wait_for(lambda:player.evaluate('(el)=>el.currentTime>.1&&el.videoWidth>0'),page)
        page.screenshot(path=str(EVIDENCE/f'video-{width}-initial.png'))
        wave=page.get_by_label('Source waveform. Drag to select a range; arrow keys adjust in, Shift arrows adjust out.');wave.scroll_into_view_if_needed();box=wave.bounding_box();page.mouse.move(box['x']+box['width']*.1,box['y']+20);page.mouse.down();page.mouse.move(box['x']+box['width']*.5,box['y']+20);page.mouse.up();wave.focus();page.keyboard.press('ArrowRight')
        page.get_by_label('Source in (seconds)',exact=True).fill('1');page.get_by_label('Source out (seconds)',exact=True).fill('4');page.get_by_role('button',name='Add range to programme',exact=True).click()
        expect(page.locator('.vw-clip')).to_have_count(1);page.locator('.vw-clip').click();page.get_by_role('button',name='Sound',exact=True).click();page.get_by_label('Source level (dB)').fill('-6');page.get_by_label('Fade in (seconds)',exact=True).fill('.2');page.get_by_role('button',name='Save clip sound').click()
        page.get_by_role('button',name='Frame',exact=True).click();page.get_by_label('Aspect',exact=True).select_option('9:16');page.get_by_role('button',name='Center portrait crop').click();wait_for(lambda:float(page.get_by_label('Programme revision').input_value())>=5,page)
        frame=page.get_by_label('Reframe source picture. Drag crop to move, drag lower-right corner to resize. Arrow keys move; Shift moves ten pixels.');frame.wait_for();frame.focus();page.keyboard.press('Shift+ArrowLeft');box=frame.bounding_box();page.mouse.move(box['x']+box['width']*.5,box['y']+box['height']*.5);page.mouse.down();page.mouse.move(box['x']+box['width']*.54,box['y']+box['height']*.5);page.mouse.up();prior_crop_version=int(page.get_by_label('Programme revision').input_value());page.get_by_role('button',name='Save reframe',exact=True).click();expect(page.get_by_label('Programme revision')).to_have_value(str(prior_crop_version+1));expect(page.get_by_role('button',name='Save reframe',exact=True)).to_be_enabled();wait_for(lambda:page.get_by_label('Source playback').evaluate('(el)=>el.readyState>=2'),page);page.locator('.video-workshop').evaluate('(el)=>el.scrollTop=0');page.screenshot(path=str(EVIDENCE/f'reframe-{width}-source.png'))
        page.get_by_role('button',name='Render programme',exact=True).click();page.get_by_role('link',name='Download finished video').wait_for(timeout=60000)
        out=page.get_by_label('Rendered programme playback');out.evaluate('(el)=>{el.muted=true;return el.play();}');wait_for(lambda:out.evaluate('(el)=>el.currentTime>.1&&el.videoWidth===360&&el.videoHeight===640'),page)
        page.locator('.video-workshop').evaluate('(el)=>el.scrollTop=0');page.screenshot(path=str(EVIDENCE/f'video-{width}-render.png'))
        assert not errors;assert page.locator('body').evaluate('(el)=>el.scrollWidth<=window.innerWidth');assert hashlib.sha256(source.read_bytes()).hexdigest()==original
        jobs=server.app.workbench.overview()['jobs'];(EVIDENCE/f'video-{width}.json').write_text(json.dumps({'original_sha256':original,'jobs':jobs},indent=2))
    finally:page.close()

@pytest.mark.parametrize('width',[1440,390])
def test_transcribe_adopt_caption_and_audio_gui(rig,browser,width):
    if not os.getenv('AVE_VIDEO_SPEECH_FIXTURE'):
        pytest.skip('Provisioned actual-ASR acceptance: set AVE_VIDEO_SPEECH_FIXTURE to a speech WAV (see tests/studio/VIDEO_ACCEPTANCE.md)')
    studio,server=rig
    source=Path(os.environ['AVE_VIDEO_SPEECH_FIXTURE'])
    from ave.studio import store,model
    from ave.studio.events import Actor
    draft=model.new_post(idea='Synthetic production audio draft',by='synthetic');draft=store.save_post(studio.sp,draft,expected_rev=None,actor=Actor('human','synthetic'),op='test')
    page=browser.new_page(viewport={'width':width,'height':960 if width==1440 else 844});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        page.goto(server.url()+'#/workbench');page.get_by_label('Upload source file').set_input_files(str(source));page.get_by_role('button',name='Import source',exact=True).click();page.get_by_role('button',name='Edit in Media').wait_for()
        wait_for(lambda:server.app.workbench.overview()['sources'][0].get('ready'),page)
        page.get_by_role('button',name='Edit in Media').click();page.get_by_label('Programme title').fill('Listening before speaking');page.get_by_role('button',name='Create programme').click();page.get_by_label('Source in (seconds)').wait_for(timeout=20000)
        page.get_by_label('Source out (seconds)').fill('6');page.get_by_role('button',name='Add range to programme',exact=True).click();expect(page.locator('.vw-clip')).to_have_count(1)
        page.get_by_role('button',name='Transcribe this recording').click();expect(page.locator('.mw-status')).to_contain_text('Transcript ready',timeout=120000)
        page.get_by_role('button',name='Adopt latest transcript').click();page.locator('.vw-transcript button').first.wait_for()
        page.get_by_role('button',name='Captions',exact=True).click();page.locator('.vw-transcript button').first.click();page.get_by_label('Corrected caption text').fill('Listening');page.get_by_role('button',name='Save occurrence correction').click();expect(page.locator('.vw-transcript')).to_contain_text('Listening')
        page.locator('.video-workshop').evaluate('(el)=>el.scrollTop=0');page.screenshot(path=str(EVIDENCE/f'caption-{width}-controls.png'))
        page.locator('.vw-clip').click();page.get_by_role('button',name='Sound',exact=True).click();page.get_by_label('Source level (dB)').fill('-3');prior_version=int(page.get_by_label('Programme revision').input_value());page.get_by_role('button',name='Save clip sound').click();expect(page.get_by_label('Programme revision')).to_have_value(str(prior_version+1))
        source_player=page.get_by_label('Source playback');source_player.evaluate('(el)=>el.play()');wait_for(lambda:source_player.evaluate('(el)=>el.readyState>=2&&el.currentTime>.1'),page);page.locator('.video-workshop').evaluate('(el)=>el.scrollTop=0');page.screenshot(path=str(EVIDENCE/f'sound-{width}-controls.png'))
        page.get_by_role('button',name='Render programme',exact=True).click();page.get_by_role('link',name='Download finished audio').wait_for(timeout=30000)
        player=page.get_by_label('Rendered programme playback');player.evaluate('(el)=>el.play()');wait_for(lambda:player.evaluate('(el)=>el.currentTime>.15'),page)
        page.screenshot(path=str(EVIDENCE/f'audio-{width}-render.png'));page.get_by_role('button',name='Use in a draft',exact=True).click();page.get_by_label('Destination draft').select_option(draft['id']);page.get_by_role('button',name='Choose draft',exact=True).click();expect(page.locator('.mw-status')).to_contain_text('Production reference attached');saved=store.load_post(studio.sp,draft['id']);assert saved['media']==draft['media'];assert saved['production']['sources'][0]['source_id'].startswith('output_a_');assert not errors
        (EVIDENCE/f'audio-{width}.json').write_text(json.dumps(server.app.workbench.overview()['jobs'],indent=2))
    finally:page.close()


def test_hour_source_bounded_gui_and_near_end_render(browser):
    if not os.getenv('AVE_VIDEO_LONG_ACCEPTANCE'):
        pytest.skip('Provisioned hour-source acceptance: set AVE_VIDEO_LONG_ACCEPTANCE (see VIDEO_ACCEPTANCE.md)')
    import threading
    from ave.studio.service import open_studio
    from ave.studio.ui.server import StudioServer
    evidence=Path(os.environ['AVE_VIDEO_LONG_ACCEPTANCE'])
    data=json.loads(evidence.read_text());studio=open_studio(data['studio']);server=StudioServer(studio,pin='424242',by='synthetic-user');thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    page=browser.new_page(viewport={'width':1440,'height':960});responses=[]
    page.on('response',lambda r:responses.append(r) if '/words?' in r.url else None)
    try:
        page.goto(server.url()+'#/workbench');page.get_by_role('button',name='Media projects').click();page.get_by_label('Saved programme').select_option(data['projects'][1]);page.get_by_label('Source in (seconds)').wait_for()
        initial_clips=page.locator('.vw-clip').count();initial_duration=float(page.locator('.vw-duration').inner_text().split()[1]);page.get_by_label('Source in (seconds)').fill('3590');page.get_by_label('Source out (seconds)').fill('3598')
        player=page.get_by_label('Source playback');player.evaluate('(el)=>{el.muted=true;el.currentTime=3590;return el.play();}');wait_for(lambda:player.evaluate('(el)=>el.currentTime>3590&&el.videoWidth>0'),page)
        jump=page.get_by_label('Transcript page');pages=int(jump.get_attribute('max'));jump.fill(str(pages));page.get_by_role('button',name='Go to words').click();wait_for(lambda:page.locator('.vw-transcript').inner_text().startswith('35'),page)
        assert page.locator('.vw-transcript button').count()<=100
        page.screenshot(path=str(EVIDENCE/'hour-near-end-source.png'))
        page.get_by_role('button',name='Add range to programme',exact=True).click();expect(page.locator('.vw-clip')).to_have_count(initial_clips+1);expect(page.get_by_label('Transcript page')).to_have_value(str(pages));page.get_by_role('button',name='Render programme',exact=True).click();expect(page.locator('.mw-status')).to_contain_text('Finished programme',timeout=60000)
        out=page.get_by_label('Rendered programme playback');out.evaluate('(el)=>{el.muted=true;return el.play();}');wait_for(lambda:out.evaluate('(el)=>el.currentTime>.1&&el.videoWidth>0'),page)
        expected_duration=initial_duration+8;assert abs(out.evaluate('(el)=>el.duration')-expected_duration)<.05;expect(page.locator('.vw-clock')).to_contain_text(f'/ {expected_duration:.2f} s');page.screenshot(path=str(EVIDENCE/'hour-near-end-output.png'))
        receipts=[{'url':r.url,'bytes':len(r.body())} for r in responses if r.status==200];assert max(r['bytes'] for r in receipts)<150000
        (EVIDENCE/'hour-ui.json').write_text(json.dumps({'transcript_pages':pages,'bounded_responses':receipts,'source_duration':3600,'initial_clips':initial_clips,'added_range_seconds':8,'rendered_programme_seconds':expected_duration,'retained_source_page':pages,'jobs':server.app.workbench.overview()['jobs']},indent=2))
    finally:page.close();server.shutdown();thread.join(5);server.server_close()


def test_independent_sources_trim_split_join_reorder_gui(browser):
    if not os.getenv('AVE_VIDEO_LONG_ACCEPTANCE'):
        pytest.skip('Provisioned independent speech acceptance: set AVE_VIDEO_LONG_ACCEPTANCE (see VIDEO_ACCEPTANCE.md)')
    import threading
    from ave.studio.service import open_studio
    from ave.studio.ui.server import StudioServer
    evidence=Path(os.environ['AVE_VIDEO_LONG_ACCEPTANCE'])
    data=json.loads(evidence.read_text());studio=open_studio(data['studio']);server=StudioServer(studio,pin='424242',by='synthetic-user');thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();page=browser.new_page(viewport={'width':1440,'height':960})
    try:
        page.goto(server.url()+'#/workbench');page.get_by_role('button',name='Media projects').click();page.get_by_label('Saved programme').select_option(data['projects'][0]);page.locator('.vw-clip').first.wait_for()
        initial=page.locator('.vw-clip').count();page.locator('.vw-clip').last.click();page.get_by_role('button',name='Remove range',exact=True).click();expect(page.locator('.vw-clip')).to_have_count(initial-1)
        page.locator('.vw-clip').first.click();page.get_by_label('Source in (seconds)').fill('1.5');page.get_by_label('Source out (seconds)').fill('4.5');page.get_by_role('button',name='Save range trim').click()
        player=page.get_by_label('Source playback');player.evaluate('(el)=>el.currentTime=3');page.get_by_role('button',name='Split at playhead').click();expect(page.locator('.vw-clip')).to_have_count(initial)
        page.get_by_role('button',name='Join next range').click();expect(page.locator('.vw-clip')).to_have_count(initial-1)
        page.locator('.vw-clip').last.click();page.locator('.vw-clip').last.focus();page.keyboard.press('Alt+ArrowLeft');wait_for(lambda:'turn-01' in page.locator('.vw-clip').first.inner_text(),page)
        page.get_by_role('button',name='Captions',exact=True).click();page.locator('.vw-transcript button').first.wait_for();page.get_by_role('button',name='Render programme',exact=True).click();page.get_by_role('link',name='Download finished audio').wait_for(timeout=30000)
        out=page.get_by_label('Rendered programme playback');out.evaluate('(el)=>el.play()');wait_for(lambda:out.evaluate('(el)=>el.currentTime>.1'),page)
        page.screenshot(path=str(EVIDENCE/'independent-reordered-playback.png'))
        newest=page.get_by_label('Programme revision').input_value();page.get_by_label('Programme revision').select_option('1');page.get_by_role('button',name='Restore revision').wait_for();page.get_by_label('Programme revision').select_option(newest)
        page.get_by_role('button',name='Close editor',exact=True).click();page.get_by_role('button',name='Media projects').click();page.get_by_label('Saved programme').select_option(data['projects'][0]);page.get_by_role('link',name='Download finished audio').wait_for()
    finally:page.close();server.shutdown();thread.join(5);server.server_close()


@pytest.mark.parametrize('width',[1440,390])
def test_portable_kept_source_click_captions_and_playback(rig,browser,width,tmp_path):
    from .video_fixtures import tone,registered_kept
    from .test_video_workshop_review import original_doc
    from ave.studio import video_workshop as V
    from ave import editdoc
    studio,server=rig
    source=tone(tmp_path/'offset.wav',3)
    doc=original_doc('audio',duration=1.5,offset=2)
    ep,original,args=registered_kept(studio,source,doc,[{'w':'local','start':2.6,'end':2.8},{'w':'outside','start':7,'end':7.2}])
    programme=V.adapt_kept(studio,args)
    page=browser.new_page(viewport={'width':width,'height':960 if width==1440 else 844})
    page.on('pageerror',lambda e:print('portable GUI page error:',e))
    try:
        page.goto(server.url()+'#/workbench');page.get_by_role('button',name='Media projects').click()
        page.get_by_label('Saved programme').select_option(programme['id'])
        word=page.locator('.vw-transcript button').filter(has_text='local');word.wait_for();word.click()
        expected=editdoc.src_time(doc['sources']['mic'],2.6)
        assert abs(float(page.get_by_label('Source in (seconds)',exact=True).input_value())-expected)<1e-8
        player=page.get_by_label('Source playback');wait_for(lambda:abs(player.evaluate('(el)=>el.currentTime')-expected)<.02,page)
        assert 'outside' not in page.locator('.vw-transcript').inner_text()
        player.evaluate('(el)=>el.play()');wait_for(lambda:player.evaluate('(el)=>el.currentTime')>expected+.1,page)
        page.screenshot(path=str(EVIDENCE/f'kept-source-clock-{width}.png'))
        page.get_by_role('button',name='Add range to programme',exact=True).click();expect(page.locator('.vw-clip')).to_have_count(2)
        page.get_by_role('button',name='Captions',exact=True).click();page.locator('.vw-transcript button').first.click()
        page.get_by_label('Corrected caption text').fill('Corrected local');page.get_by_role('button',name='Save occurrence correction').click()
        expect(page.locator('.vw-transcript')).to_contain_text('Corrected local')
        assert page.locator('.vw-transcript button').count()==2
        page.locator('.video-workshop').evaluate('(el)=>el.scrollTop=0');page.screenshot(path=str(EVIDENCE/f'kept-captions-{width}.png'))
        page.get_by_role('button',name='Render programme',exact=True).click();expect(page.locator('.mw-status')).to_contain_text('Finished programme',timeout=30000)
        out=page.get_by_label('Rendered programme playback');out.evaluate('(el)=>el.play()');wait_for(lambda:out.evaluate('(el)=>el.currentTime>.1'),page)
        page.screenshot(path=str(EVIDENCE/f'kept-playback-{width}.png'))
    finally:page.close()


@pytest.mark.parametrize('width',[1440,390])
def test_occurrence_cleanup_worker_and_restore_gui(rig,browser,width,tmp_path):
    import numpy as np
    import soundfile as sf
    from ave import library
    from ave.episode import write_json
    from ave.studio import video_workshop as V
    from .test_video_workshop import mutate
    studio,server=rig;rate=48000;source=tmp_path/'speech.wav';samples=np.zeros((rate*3,2));samples[:rate//2]=.1;samples[rate*2:rate*5//2]=.1;sf.write(source,samples,rate)
    rec=library.add_file(studio.sp,source,source={'type':'own'},by='synthetic fixture');p=V.create(studio,{'title':'Cleanup fixture','mode':'audio','request_id':'browser-cleanup'});p=V.add_source(studio,{'project':p['id'],'source_id':rec['id'],'expected_revision':1,'request_id':'browser-source'})
    ep,meta=V.project(studio.sp,p['id']);tp=ep.work/'words.json';write_json(tp,{'words':[{'w':'hello','start':.1,'end':.4,'speaker':'S1'},{'w':'again','start':2.1,'end':2.4,'speaker':'S1'}]});meta['project_sources'][rec['id']]['transcript']={'file':ep.rel(tp),'sha256':hashlib.sha256(tp.read_bytes()).hexdigest(),'clock':'source'};write_json(ep.meta_path,meta)
    p=mutate(studio,p,{'kind':'insert','source':rec['id'],'in':0,'out':3});page=browser.new_page(viewport={'width':width,'height':960 if width==1440 else 844});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        page.goto(server.url()+'#/workbench');page.get_by_role('button',name='Media projects').click();page.get_by_label('Saved programme').select_option(p['id']);page.get_by_role('button',name='Cleanup',exact=True).click();page.get_by_role('button',name='Analyze cleanup').click()
        expect(page.locator('.mw-status')).to_contain_text('Cleanup revision',timeout=60000);expect(page.get_by_text('Compared with',exact=False)).to_be_visible();page.get_by_role('button',name='Render programme').click();expect(page.locator('.mw-status')).to_contain_text('Finished programme',timeout=60000)
        player=page.get_by_label('Rendered programme playback');player.evaluate('(el)=>el.play()');wait_for(lambda:player.evaluate('(el)=>el.currentTime>.1&&el.readyState>=2'),page)
        page.get_by_role('button',name='Cleanup',exact=True).click();checks=page.locator('input[aria-label^="Restore cut "]');assert checks.count()>0
        for i in range(checks.count()):checks.nth(i).check()
        version=int(page.get_by_label('Programme revision').input_value());page.get_by_role('button',name='Apply cleanup choices').click();expect(page.get_by_label('Programme revision')).to_have_value(str(version+1),timeout=60000);expect(page.locator('.mw-status')).to_contain_text('Cleanup revision',timeout=60000)
        assert V.load(studio.sp,p['id'])['doc']['duration']==pytest.approx(3)
        assert page.get_by_label('Rendered programme playback').count()==0
        assert page.locator('.vw-results').get_by_role('link',name='Download finished audio').count()==0
        page.get_by_role('button',name='Play earlier export').click();old=page.get_by_label('Rendered programme playback');old.evaluate('(el)=>el.play()');wait_for(lambda:old.evaluate('(el)=>el.currentTime>.1'),page)
        expect(page.locator('.vw-clock')).to_contain_text('Rendered v');expect(page.locator('.vw-clock')).to_contain_text('editing v');expect(page.locator('.vw-results').get_by_role('link',name=f'Download finished audio v{version}')).to_be_visible();expect(page.get_by_role('button',name=f'View finished programme v{version}')).to_be_visible();expect(page.get_by_role('button',name=f'Use in a draft v{version}')).to_be_visible()
        page.screenshot(path=str(EVIDENCE/f'occurrence-cleanup-{width}.png'));assert not errors
    finally:page.close()
