"""Actual browser -> authenticated HTTP -> renderer -> preview -> draft.

All inputs are original synthetic text/tone. Optional spoken-fixture test is an
explicit local acceptance run, never a private production recording.
"""
import hashlib
import json
import os
import re
import shutil
import threading
import time
import wave
from pathlib import Path

import pytest
from ave.studio import store
from ave.studio.project import StudioProject
from ave.studio.service import open_studio
from ave.studio.ui.server import StudioServer
from .test_workbench import TEXT, BROWSER_CACHE

EVIDENCE=Path('/private/tmp/ave-workbench-acceptance-20261029')
SPEECH_FIXTURE=os.getenv('AVE_WORKBENCH_SPEECH_FIXTURE')

@pytest.fixture(scope='module')
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser=pw.chromium.launch();yield browser;browser.close()

@pytest.fixture
def rig(tmp_path,monkeypatch):
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH',BROWSER_CACHE)
    sp=StudioProject(tmp_path/'workbench-sample');sp.create(title='Listening practice · Synthetic sample',tz='UTC')
    studio=open_studio(sp.root);server=StudioServer(studio,pin='424242',by='synthetic-user')
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    EVIDENCE.mkdir(exist_ok=True)
    yield studio,server
    server.shutdown();thread.join(5);server.server_close()


def wait_for(fn,page,seconds=40):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        if fn():return
        page.wait_for_timeout(100)
    assert fn()


def upload(page,name,payload):
    if not page.locator('.wb-upload').is_visible():page.get_by_role('button',name='Add source',exact=True).click()
    page.get_by_label('Upload source file',exact=True).set_input_files({'name':name,'mimeType':'application/octet-stream','buffer':payload})
    page.get_by_role('button',name='Import source',exact=True).click()


@pytest.mark.legacy_ui
@pytest.mark.parametrize('width',[1440,1024,390])
def test_real_quote_journey(rig,browser,width):
    studio,server=rig;page=browser.new_page(viewport={'width':width,'height':960 if width==1440 else 768 if width==1024 else 844});errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        page.goto(server.url()+'#/workbench');page.get_by_role('heading',name='Sources').wait_for()
        upload(page,'listening.md',TEXT)
        page.get_by_label('Select p001.s1',exact=True).wait_for()
        page.reload();page.get_by_label('Select p001.s1',exact=True).check()
        page.get_by_label('Visual style',exact=True).select_option('paper');page.get_by_label('Image format',exact=True).select_option('4x5')
        page.screenshot(path=str(EVIDENCE/f'workbench-{width}-source.png'),full_page=True)
        page.get_by_role('button',name='Render quote image',exact=True).click()
        page.locator('.wb-artifact').wait_for(timeout=45000)
        wait_for(lambda:page.locator('.wb-artifact').evaluate('(el)=>el.complete && el.naturalWidth===1080 && el.naturalHeight===1350'),page)
        assert page.get_by_text('Visual checks passed',exact=True).is_visible()
        assert page.locator('body').evaluate('(el)=>el.scrollWidth<=window.innerWidth')
        page.screenshot(path=str(EVIDENCE/f'workbench-{width}-render.png'),full_page=True)
        job=next(j for j in server.app.workbench.overview()['jobs'] if j['operation']=='render')
        post=store.load_post(studio.sp,job['post']);asset=job['result']['assets'][0]
        image=studio.sp.root/asset['file'];digest=hashlib.sha256(image.read_bytes()).hexdigest()
        assert digest==asset['sha256'];assert not list((studio.sp.publish/'queue').glob('*.json'))
        (EVIDENCE/f'quote-{width}.png').write_bytes(image.read_bytes())
        preserved=EVIDENCE/'studios'/job['id'];shutil.copytree(studio.sp.root,preserved)
        (EVIDENCE/f'quote-{width}.json').write_text(json.dumps({'source_sha256':hashlib.sha256(TEXT).hexdigest(),'post_id':post['id'],'post_rev':post['rev'],'job':job,'studio':str(preserved),'queue_count':0,'approval_count':0},indent=2))
        page.get_by_role('link',name='Open draft',exact=True).click();page.wait_for_selector('#main[data-page=post]')
        page.get_by_text('We leave room for one another, and we listen before we answer.',exact=False).first.wait_for()
        page.screenshot(path=str(EVIDENCE/f'workbench-{width}-draft.png'),full_page=True)
        assert not errors
    finally:page.close()


@pytest.mark.legacy_ui
def test_failure_input_retry_and_poll_stop(rig,browser,monkeypatch):
    studio,server=rig;page=browser.new_page(viewport={'width':1440,'height':960});requests=[]
    page.on('request',lambda req:requests.append(req.url))
    try:
        page.goto(server.url()+'#/workbench');upload(page,'listening.md',TEXT)
        page.get_by_label('Select p001.s1',exact=True).check();page.get_by_label('Visual style',exact=True).select_option('clay')
        page.route('**/api/studio/workbench/render',lambda route:route.abort())
        page.get_by_role('button',name='Render quote image',exact=True).click();page.locator('.wb-error:not([hidden])').wait_for()
        assert page.get_by_label('Select p001.s1',exact=True).is_checked();assert page.get_by_label('Visual style',exact=True).input_value()=='clay'
        page.unroute('**/api/studio/workbench/render')
        import ave.studio.workbench as W
        real_env=W.child_env
        monkeypatch.setattr(W,'child_env',lambda:{**real_env(),'PLAYWRIGHT_BROWSERS_PATH':str(studio.sp.work/'missing-browser')})
        page.get_by_role('button',name='Render quote image',exact=True).click()
        page.get_by_role('button',name='Retry job',exact=True).wait_for(timeout=15000)
        assert page.locator('.wb-diagnostic').inner_text()
        page.screenshot(path=str(EVIDENCE/'workbench-failed-render.png'),full_page=True)
        pid=server.app.workbench.overview()['jobs'][0]['post']
        monkeypatch.setattr(W,'child_env',real_env)
        page.get_by_role('button',name='Retry job',exact=True).click();page.locator('.wb-artifact').wait_for(timeout=45000)
        assert len(store.list_posts(studio.sp))==1 and store.list_posts(studio.sp)[0]['id']==pid
        page.get_by_role('link',name='Open production',exact=True).click();page.wait_for_selector('#main[data-page=pipeline]')
        page.wait_for_timeout(300);before=len([r for r in requests if '/api/studio/workbench' in r]);page.wait_for_timeout(2000)
        assert before==len([r for r in requests if '/api/studio/workbench' in r])
    finally:page.close()


@pytest.mark.legacy_ui
def test_recording_import_and_multiple_sources_with_locator(rig,browser,tmp_path):
    import numpy as np
    studio,server=rig;page=browser.new_page(viewport={'width':1440,'height':960})
    wav=tmp_path/'synthetic-tone.wav'
    with wave.open(str(wav),'wb') as out:
        out.setnchannels(1);out.setsampwidth(2);out.setframerate(16000)
        out.writeframes((np.sin(np.arange(32000)*2*np.pi*220/16000)*3000).astype('<i2').tobytes())
    try:
        page.goto(server.url()+'#/workbench');upload(page,'listening.md',TEXT);page.get_by_label('Select p001.s1',exact=True).wait_for()
        upload(page,wav.name,wav.read_bytes());page.get_by_role('button',name='Transcribe locally',exact=True).wait_for()
        wait_for(lambda:page.get_by_role('button',name='Transcribe locally',exact=True).is_enabled(),page)
        player=page.get_by_label('Extracted audio preview',exact=True)
        wait_for(lambda:player.evaluate('(el)=>el.readyState>=1 && el.duration>0'),page)
        player.evaluate('(el)=>el.play()');wait_for(lambda:player.evaluate('(el)=>el.currentTime>0'),page);player.evaluate('(el)=>el.pause()')
        src=next(s for s in server.app.workbench.overview()['sources'] if s['kind']=='audio')
        raw_url='/files/'+src['episode']+'/raw/'+Path(src['file']).name
        assert page.request.get(f'http://127.0.0.1:{server.port}'+raw_url).status==403
        assert (studio.sp.root/src['file']).read_bytes()==wav.read_bytes()
        assert (studio.sp.root/src['episode']/'episode.json').is_file()
        p=studio.create_post(copy={'base':'A shared source brief'})
        page.goto(server.url()+'#/pipeline');page.get_by_role('button',name='Open',exact=True).click()
        page.get_by_label('Add source material',exact=True).select_option(label='Listening');page.get_by_role('button',name='Add source',exact=True).click()
        page.get_by_label('Add source material',exact=True).select_option(label='synthetic-tone.wav');page.get_by_role('button',name='Add source',exact=True).click()
        page.get_by_label('Source 2 start seconds',exact=True).fill('0.2');page.get_by_label('Source 2 end seconds',exact=True).fill('1.5')
        page.get_by_role('button',name='Save brief',exact=True).click()
        wait_for(lambda:len(store.load_post(studio.sp,p['id']).get('production',{}).get('sources',[]))==2,page)
        assert store.load_post(studio.sp,p['id'])['production']['sources'][1]['locator']=={'start':.2,'end':1.5}
        page.reload();page.get_by_role('button',name='Open',exact=True).click()
        assert page.get_by_label('Source 2 end seconds',exact=True).input_value()=='1.5'
        page.screenshot(path=str(EVIDENCE/'production-multiple-sources.png'),full_page=True)
    finally:page.close()


@pytest.mark.legacy_ui
@pytest.mark.skipif(not SPEECH_FIXTURE,reason='Explicit original synthetic speech fixture required for local ASR acceptance')
def test_real_spoken_fixture_gui(rig,browser):
    studio,server=rig;source=Path(SPEECH_FIXTURE);payload=source.read_bytes()
    expected='This is a short studio test. A good conversation starts with careful listening. We can take one idea from an essay and turn it into a thoughtful post. The original recording should remain unchanged.'
    page=browser.new_page(viewport={'width':1440,'height':960})
    try:
        page.goto(server.url()+'#/workbench');upload(page,source.name,payload)
        page.get_by_role('button',name='Transcribe locally',exact=True).wait_for()
        wait_for(lambda:page.get_by_role('button',name='Transcribe locally',exact=True).is_enabled(),page)
        player=page.get_by_label('Extracted audio preview',exact=True)
        wait_for(lambda:player.evaluate('(el)=>el.readyState>=1 && el.duration>0'),page)
        player.evaluate('(el)=>el.play()');wait_for(lambda:player.evaluate('(el)=>el.currentTime>0'),page);player.evaluate('(el)=>el.pause()')
        page.get_by_role('button',name='Transcribe locally',exact=True).click()
        page.locator('.wb-transcript').wait_for(timeout=120000)
        jobs=server.app.workbench.overview()['jobs'];job=next(j for j in jobs if j['operation']=='transcribe')
        assert job['status']=='done',job
        actual=' '.join(u['text'] for u in job['result']['utterances'])
        normalize=lambda t:re.findall(r'[a-z]+',t.lower())
        assert normalize(actual)==normalize(expected),(actual,expected)
        src=next(s for s in server.app.workbench.overview()['sources'] if s['kind']=='audio')
        digest=hashlib.sha256(payload).hexdigest();assert hashlib.sha256((studio.sp.root/src['file']).read_bytes()).hexdigest()==digest
        raw=list((studio.sp.root/src['episode']/'raw').iterdir())[0];assert hashlib.sha256(raw.read_bytes()).hexdigest()==digest
        page.screenshot(path=str(EVIDENCE/'workbench-real-synthetic-transcript.png'),full_page=True)
        preserved=EVIDENCE/'studios'/job['id'];shutil.copytree(studio.sp.root,preserved)
        (EVIDENCE/'speech.json').write_text(json.dumps({'scope':'Original synthetic narration, not production-footage validation','input':str(source),'source_sha256':digest,'expected':expected,'actual':actual,'normalized_match':True,'job':job,'source':src,'studio':str(preserved),'queue_count':len(list((studio.sp.publish/'queue').glob('*.json')))},indent=2))
    finally:page.close()


@pytest.mark.legacy_ui
def test_video_upload_has_playable_registered_preview(rig,browser,tmp_path):
    import subprocess
    from ave import config
    studio,server=rig;file=tmp_path/'synthetic-scene.mp4'
    subprocess.run([str(config.binary('ffmpeg')),'-nostdin','-v','error','-f','lavfi','-i','color=c=0x647d75:s=160x90:r=25:d=2',
                    '-f','lavfi','-i','sine=frequency=330:duration=2','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-shortest',str(file)],check=True)
    page=browser.new_page(viewport={'width':1440,'height':960})
    try:
        page.goto(server.url()+'#/workbench');upload(page,file.name,file.read_bytes())
        player=page.get_by_label('Browser preview (original preserved)',exact=True);player.wait_for()
        wait_for(lambda:player.evaluate('(el)=>el.readyState>=2 && el.videoWidth>0'),page)
        player.evaluate('(el)=>el.play()');wait_for(lambda:player.evaluate('(el)=>el.currentTime>0'),page);player.evaluate('(el)=>el.pause()')
        src=next(s for s in server.app.workbench.overview()['sources'] if s['kind']=='video')
        assert (studio.sp.root/src['file']).read_bytes()==file.read_bytes()
        assert page.request.get(f'http://127.0.0.1:{server.port}/files/'+src['episode']+'/raw/'+Path(src['file']).name).status==403
        page.screenshot(path=str(EVIDENCE/'workbench-video-source.png'),full_page=True)
    finally:page.close()
