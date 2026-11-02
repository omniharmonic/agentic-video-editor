"""Actual portable episode GUI, generated audio and isolated native confirmation."""
import json
import re
from pathlib import Path
import tempfile
import pytest
from playwright.sync_api import expect
from .test_workbench_browser import browser,rig,wait_for
from .test_episode_workshop import managed,run,seed_transcript,FakeAuthority

EVIDENCE=Path(tempfile.gettempdir())/'ave-episode-intake-evidence';EVIDENCE.mkdir(exist_ok=True)

@pytest.mark.parametrize('width',[1440,390])
def test_audio_episode_gui(rig,browser,width,tmp_path):
    studio,server=rig;eid,source=managed(studio,tmp_path)
    run(studio,eid,'episode-intake');seed_transcript(studio,eid)
    server.app.native_authority=FakeAuthority()
    page=browser.new_page(viewport={'width':width,'height':960 if width==1440 else 844});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    def native_confirm(token):
        item=server.app.episode_checkpoints.prepared[token];item['viewed']-=3
        return server.app.episode_checkpoints.confirm_native(token,server.app.pin.reveal())
    page.expose_function('fixtureConfirmEpisode',native_confirm)
    page.add_init_script("window.aveNative={confirmEpisode:token=>window.fixtureConfirmEpisode(token)}")
    try:
        page.goto(server.url()+'#/workbench');page.get_by_role('button',name='Episodes',exact=True).click();page.get_by_role('button',name='Synthetic conversation',exact=True).click()
        player=page.locator('.ew-player audio');player.evaluate('(el)=>el.play()');wait_for(lambda:player.evaluate('(el)=>el.currentTime>.1&&el.readyState>=2'),page)
        page.get_by_role('button',name='All episodes',exact=True).click();page.get_by_role('button',name='Synthetic conversation',exact=True).click()
        player=page.locator('.ew-player audio');expect(player).to_have_count(1);player.evaluate('(el)=>el.play()');wait_for(lambda:player.evaluate('(el)=>el.currentTime>.1&&el.readyState>=2'),page)
        page.get_by_label('Name S1').fill('Synthetic host');page.get_by_label('Name S2').fill('Synthetic guest');page.screenshot(path=str(EVIDENCE/f'audio-{width}-speakers.png'))
        page.get_by_role('button',name='Review speaker names',exact=True).click();page.get_by_role('button',name='Confirm in native review',exact=True).click()
        page.get_by_role('button',name='Set reference clock',exact=True).click();page.get_by_role('button',name='Review audio session',exact=True).wait_for(timeout=30000)
        page.get_by_role('button',name='Review audio session',exact=True).click();page.get_by_role('button',name='Confirm in native review',exact=True).click()
        page.get_by_label('Session start seconds').fill('.25');page.get_by_label('Session end seconds').fill('3.75');page.get_by_role('button',name='Listen around start',exact=True).click();page.screenshot(path=str(EVIDENCE/f'audio-{width}-bounds.png'))
        page.get_by_role('button',name='Review session bounds',exact=True).click();page.get_by_role('button',name='Confirm in native review',exact=True).click()
        page.get_by_role('button',name='Build & render assembly',exact=True).click();page.get_by_role('button',name='Review this exact assembly',exact=True).wait_for(timeout=30000)
        player=page.locator('.ew-player audio');player.evaluate('(el)=>el.play()');wait_for(lambda:player.evaluate('(el)=>el.currentTime>.1&&el.readyState>=2'),page)
        page.get_by_role('button',name='Review this exact assembly',exact=True).click();page.get_by_role('button',name='Confirm in native review',exact=True).click();expect(page.locator('.mw-status')).to_have_text('Assembly review recorded')
        page.get_by_role('button',name='All episodes',exact=True).click();page.get_by_role('button',name='Synthetic conversation',exact=True).click()
        expect(page.locator('.mw-status')).to_have_text('Assembly review recorded');player=page.locator('.ew-player audio');expect(player).to_have_count(1)
        player.evaluate('(el)=>el.play()');wait_for(lambda:player.evaluate('(el)=>el.currentTime>.1&&el.readyState>=2'),page)
        page.screenshot(path=str(EVIDENCE/f'audio-{width}-assembly.png'));assert not errors
        assert page.locator('.episode-workshop').evaluate('(el)=>el.scrollWidth<=el.clientWidth+1')
        assert page.locator('.episode-workshop .ew-player audio').count()==1
        (EVIDENCE/f'audio-{width}.json').write_text(json.dumps({'simulated_native_boundary':True,'jobs':server.app.workbench.overview()['jobs']},indent=2))
        if width==1440:
            from ave.studio import episode_registry as R,episode_workshop as E,episode_checkpoints as C
            loaded=E.load(studio,eid);artifact=loaded['assemblies'][-1]['render'];origin=page.evaluate('location.origin');url=origin+artifact['url'];events=len(C.events(studio));stored=E.artifact(studio,eid,artifact['id'])
            assert page.request.get(url).status==200
            assert page.request.get(origin+'/files/'+stored['file']).status==403
            with R.resolve(studio,eid) as (ep,row):raw=studio.sp.rel(ep.root/row['sources'][0]['file'])
            assert page.request.get(origin+'/files/'+raw).status==403
            assert page.request.post(origin+'/api/studio/episodes/confirm',data={'episode':eid,'human':True},headers={'Origin':origin}).status==403
            assert len(C.events(studio))==events
            R.revoke(studio,eid);assert page.request.get(url).status>=400

    finally:page.close()

# A caller-provisioned synthetic conversation is optional; no session-specific
# path or unavailable model is a prerequisite of the portable tests above.
ACCEPTANCE=__import__('os').getenv('AVE_EPISODE_ACCEPTANCE')

@pytest.mark.parametrize('mode,width',[('audio',390),('audiovisual',1440),('audiovisual',390)])
def test_provisioned_conversation_gui(rig,browser,monkeypatch,mode,width):
    if not ACCEPTANCE:pytest.skip('Opt-in actual ASR/camera acceptance: set AVE_EPISODE_ACCEPTANCE to a fixture manifest')
    from ave.publish.safety import sha256_file
    from ave.studio import episode_registry as R,episode_workshop as E
    fixture=json.loads(Path(ACCEPTANCE).read_text());studio,server=rig;server.app.native_authority=FakeAuthority()
    monkeypatch.setenv('AVE_FLUIDAUDIO',fixture['speech'])
    originals=[Path(fixture['audio']),*([Path(p) for p in fixture['cameras']] if mode=='audiovisual' else [])];hashes={str(p):sha256_file(p) for p in originals}
    page=browser.new_page(viewport={'width':width,'height':960 if width==1440 else 844});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    def native_confirm(token):
        server.app.episode_checkpoints.prepared[token]['viewed']-=3
        return server.app.episode_checkpoints.confirm_native(token,server.app.pin.reveal())
    page.expose_function('fixtureConfirmEpisode',native_confirm);page.add_init_script("window.aveNative={confirmEpisode:token=>window.fixtureConfirmEpisode(token)}")
    def approve(label):
        page.get_by_role('button',name=label,exact=True).click();page.get_by_role('button',name='Confirm in native review',exact=True).click()
    try:
        page.goto(server.url()+'#/workbench');page.get_by_role('button',name='Episodes',exact=True).click();page.get_by_role('button',name='Create from recordings',exact=True).click();page.get_by_label('Episode title',exact=True).fill('Synthetic conversation · Listening practice')
        page.get_by_label('Import episode recordings').set_input_files([str(p) for p in originals]);wait_for(lambda:len(server.app.workbench.overview()['sources'])==len(originals) and all(s.get('ready') for s in server.app.workbench.overview()['sources']),page,seconds=120)
        expect(page.locator('.ew-intake-sources input[type=checkbox]')).to_have_count(len(originals),timeout=30000)
        page.get_by_role('button',name='Create episode',exact=True).click();page.get_by_role('button',name='Prepare recordings',exact=True).wait_for(timeout=30000);page.get_by_role('button',name='Prepare recordings',exact=True).click()
        page.get_by_role('button',name='Transcribe conversation',exact=True).wait_for(timeout=120000);page.get_by_role('button',name='Transcribe conversation',exact=True).click();page.get_by_label('Name S1').wait_for(timeout=120000)
        for sid in ('S1','S2'):page.get_by_label('Name '+sid).fill('Synthetic '+sid)
        player=page.locator('.ew-player audio');player.evaluate('(el)=>el.play()');wait_for(lambda:player.evaluate('(el)=>el.currentTime>.15&&el.readyState>=2'),page)
        page.screenshot(path=str(EVIDENCE/f'conversation-{mode}-{width}-speakers.png'));approve('Review speaker names')
        page.get_by_role('button',name='Measure source alignment' if mode=='audiovisual' else 'Set reference clock',exact=True).click();page.get_by_role('button',name='Review source & camera mapping' if mode=='audiovisual' else 'Review audio session',exact=True).wait_for(timeout=120000)
        if mode=='audiovisual':
            for name,sid in [('camera-a.mp4','S1'),('camera-b.mp4','S2')]:page.get_by_label('Speaker in '+name).select_option(sid)
            page.get_by_role('button',name='camera-b.mp4',exact=True).click();player=page.locator('.ew-player video');player.evaluate('(el)=>{el.muted=true;return el.play()}');wait_for(lambda:player.evaluate('(el)=>el.currentTime>.1&&el.videoWidth>0'),page)
            page.screenshot(path=str(EVIDENCE/f'conversation-{mode}-{width}-mapping.png'))
        approve('Review source & camera mapping' if mode=='audiovisual' else 'Review audio session')
        page.get_by_label('Session start seconds').fill('0');page.get_by_label('Session end seconds').fill('133.3');page.get_by_role('button',name='Listen around end',exact=True).click();page.screenshot(path=str(EVIDENCE/f'conversation-{mode}-{width}-bounds.png'));approve('Review session bounds')
        page.get_by_role('button',name='Build & render assembly',exact=True).click();page.get_by_role('button',name='Review this exact assembly',exact=True).wait_for(timeout=180000)
        player=page.locator('.ew-player audio' if mode=='audio' else '.ew-player video');player.evaluate('(el)=>{el.muted=true;return el.play()}');wait_for(lambda:player.evaluate('(el)=>el.currentTime>.1&&el.readyState>=2'),page);approve('Review this exact assembly');expect(page.locator('.mw-status')).to_have_text('Assembly review recorded')
        page.screenshot(path=str(EVIDENCE/f'conversation-{mode}-{width}-assembly.png'));assert not errors
        eid=next(iter(R.records(studio.sp)));loaded=E.load(studio,eid);assert loaded['transcript']['word_count']>350;assert loaded['checkpoints']['assembly']['state']=='reviewed exact';assert hashes=={str(p):sha256_file(p) for p in originals}
        assert page.locator('.episode-workshop').evaluate('(el)=>el.scrollWidth<=el.clientWidth+1')
        (EVIDENCE/f'conversation-{mode}-{width}.json').write_text(json.dumps({'simulated_native_boundary':True,'source_hashes':hashes,'episode':loaded,'jobs':server.app.workbench.overview()['jobs']},indent=2))
    finally:page.close()


def decoded_frame(player):
    """Wait for an actually presented decoded frame, not only metadata."""
    return player.evaluate('''async el=>{
      el.muted=true;await el.play();
      await new Promise((resolve,reject)=>{
        const timer=setTimeout(()=>reject(Error('No decoded frame presented')),10000);
        el.requestVideoFrameCallback(()=>{clearTimeout(timer);resolve();});
      });
      el.pause();const canvas=document.createElement('canvas');canvas.width=32;canvas.height=18;
      const ctx=canvas.getContext('2d');ctx.drawImage(el,0,0,32,18);const pixels=ctx.getImageData(0,0,32,18).data;
      return {src:el.currentSrc,time:el.currentTime,width:el.videoWidth,height:el.videoHeight,
        meanRGB:[0,1,2].map(c=>Array.from({length:576},(_,i)=>pixels[i*4+c]).reduce((a,b)=>a+b)/576)};
    }''')


def test_camera_mapping_choices_survive_source_and_stage_navigation(rig,browser,tmp_path):
    import uuid
    from ave import library
    from ave.studio import episode_workshop as E
    from .video_fixtures import picture
    from .test_episode_workshop import confirm
    studio,server=rig;camera=picture(tmp_path/'camera.mov',seconds=4,width=160,height=90)
    assets=[library.add_file(studio.sp,p,source={'type':'own'},by='synthetic',attested=False) for p in (camera.with_suffix('.wav'),camera)]
    eid=E.create(studio,{'title':'Portable camera choices','sources':[a['id'] for a in assets],'request_id':str(uuid.uuid4())})['episode']
    run(studio,eid,'episode-intake');source=seed_transcript(studio,eid);server.app.native_authority=FakeAuthority()
    server.app.pin=__import__('ave.studio.ui.api',fromlist=['PinGate']).PinGate('123456');confirm(server.app,eid,'speakers',{'names':{'S1':'Synthetic one','S2':'Synthetic two'}})
    run(studio,eid,'episode-sync',source_id=source['id'],points=3)
    data=E.load(studio,eid);video=next(s for s in data['sources'] if s['probe']['video']);audio=next(s for s in data['sources'] if not s['probe']['video'])
    page=browser.new_page(viewport={'width':390,'height':844})
    try:
        page.goto(server.url()+'#/workbench');page.get_by_role('button',name='Episodes',exact=True).click();page.get_by_role('button',name='Portable camera choices',exact=True).click()
        page.get_by_label('Speaker in '+video['label']).select_option('S2');page.get_by_role('button',name=video['label'],exact=True).click()
        expect(page.get_by_label('Speaker in '+video['label'])).to_have_value('S2')
        page.get_by_label('Include '+video['label']).uncheck();page.get_by_role('button',name=audio['label'],exact=True).click()
        expect(page.get_by_label('Include '+video['label'])).not_to_be_checked()
        # The choice for an excluded source is irrelevant; reinclude it and make
        # a fresh explicit wide-role choice, then navigate across stages.
        page.get_by_label('Include '+video['label']).check();page.get_by_label('Camera role '+video['label']).select_option('wide')
        page.get_by_role('button',name='Speakers',exact=False).click();page.get_by_label('Name S1').fill('Draft name')
        page.get_by_role('button',name='Sync & cameras',exact=True).click();expect(page.get_by_label('Camera role '+video['label'])).to_have_value('wide')
        page.get_by_role('button',name='Speakers',exact=False).click();expect(page.get_by_label('Name S1')).to_have_value('Draft name')
    finally:page.close()


REPLAY=__import__('os').getenv('AVE_EPISODE_REPLAY')
@pytest.mark.parametrize('width',[1440,390])
def test_retained_conversation_presentation(rig,browser,tmp_path,monkeypatch,width):
    if not REPLAY:pytest.skip('Opt-in exact retained transcript/sync presentation replay')
    import shutil
    from ave.publish.safety import sha256_file
    from ave.studio import episode_registry as R,episode_workshop as E
    from .test_episode_workshop import FakeBooks
    entry=next(r for r in json.loads(Path(REPLAY).read_text()) if r['mode']==f'audiovisual-{width}')
    root=tmp_path/'retained-conversation';shutil.copytree(entry['retained_root'],root)
    studio,server=rig;monkeypatch.setattr(R,'NativeBooks',FakeBooks);server.app.native_authority=FakeAuthority()
    row=R.register_selected(studio,root,derived_consent=True);eid=row['id'];run(studio,eid,'episode-intake')
    assert sha256_file(root/'work/transcript.json')==entry['transcript_sha256'];assert sha256_file(root/'work/sync.json')==entry['sync_sha256']
    hashes={s['file']:s['sha256'] for s in row['sources']};page=browser.new_page(viewport={'width':width,'height':960 if width==1440 else 844});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    def native_confirm(token):
        server.app.episode_checkpoints.prepared[token]['viewed']-=3
        return server.app.episode_checkpoints.confirm_native(token,server.app.pin.reveal())
    page.expose_function('fixtureConfirmEpisode',native_confirm);page.add_init_script("window.aveNative={confirmEpisode:token=>window.fixtureConfirmEpisode(token)}")
    def approve(label):
        page.get_by_role('button',name=label,exact=True).click();page.get_by_role('button',name='Confirm in native review',exact=True).click()
    try:
        page.goto(server.url()+'#/workbench');page.get_by_role('button',name='Episodes',exact=True).click();page.get_by_role('button',name=row['label'],exact=True).click()
        for sid in ('S1','S2'):page.get_by_label('Name '+sid).fill('Synthetic '+sid)
        approve('Review speaker names')
        for name,sid in [('camera-a.mp4','S1'),('camera-b.mp4','S2')]:page.get_by_label('Speaker in '+name).select_option(sid)
        page.get_by_role('button',name='camera-b.mp4',exact=True).click();expect(page.get_by_label('Speaker in camera-a.mp4')).to_have_value('S1');expect(page.get_by_label('Speaker in camera-b.mp4')).to_have_value('S2')
        camera=next(s for s in E.load(studio,eid)['sources'] if s['label']=='camera-b.mp4');projected=E.transcript_page(studio,eid,camera['id'])
        assert projected['utterances'][0]['words'][0]['transcript_start']>=3.2
        frame=decoded_frame(page.locator('.ew-player video'));assert max(frame['meanRGB'])>20
        expect(page.locator('.ew-transcript')).not_to_contain_text('[object Object]')
        page.screenshot(path=str(EVIDENCE/f'replayed-conversation-{width}-mapping.png'));approve('Review source & camera mapping')
        page.get_by_label('Session start seconds').fill('0');page.get_by_label('Session end seconds').fill('133.3');approve('Review session bounds')
        page.get_by_role('button',name='Build & render assembly',exact=True).click();page.get_by_role('button',name='Review this exact assembly',exact=True).wait_for(timeout=180000)
        decoded_frame(page.locator('.ew-player video'));approve('Review this exact assembly');expect(page.locator('.mw-status')).to_have_text('Assembly review recorded')
        after_review=decoded_frame(page.locator('.ew-player video'));assert max(after_review['meanRGB'])>20
        page.get_by_role('button',name='Close',exact=True).click();page.get_by_role('button',name='Episodes',exact=True).click();page.get_by_role('button',name=row['label'],exact=True).click()
        expect(page.locator('.mw-status')).to_have_text('Assembly review recorded');reopened=decoded_frame(page.locator('.ew-player video'));assert max(reopened['meanRGB'])>20
        page.screenshot(path=str(EVIDENCE/f'replayed-conversation-{width}-assembly.png'));assert not errors
        loaded=E.load(studio,eid);assert loaded['checkpoints']['assembly']['state']=='reviewed exact';assert hashes=={s['file']:sha256_file(root/s['file']) for s in row['sources']}
        (EVIDENCE/f'replayed-conversation-{width}.json').write_text(json.dumps({'simulated_native_boundary':True,'retained_input':entry,'transcript_projection':projected,'after_review_frame':after_review,'reopened_frame':reopened,'episode':loaded,'jobs':server.app.workbench.overview()['jobs']},indent=2))
    finally:page.close()


@pytest.mark.parametrize('width',[1440,390])
def test_camera_gap_review_and_same_dialog_return(rig,browser,tmp_path,width):
    from .test_episode_workshop import gap_episode
    from ave.studio import episode_workshop as E
    studio,server=rig;eid,recorder=gap_episode(studio,tmp_path);server.app.native_authority=FakeAuthority()
    page=browser.new_page(viewport={'width':width,'height':960 if width==1440 else 844});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    def native_confirm(token):
        server.app.episode_checkpoints.prepared[token]['viewed']-=3
        return server.app.episode_checkpoints.confirm_native(token,server.app.pin.reveal())
    page.expose_function('fixtureConfirmEpisode',native_confirm);page.add_init_script("window.aveNative={confirmEpisode:token=>window.fixtureConfirmEpisode(token)}")
    try:
        page.goto(server.url()+'#/workbench');page.get_by_role('button',name='Episodes',exact=True).click();page.get_by_role('button',name='Synthetic camera gap',exact=True).click()
        page.get_by_role('button',name='Build & render assembly',exact=True).click();page.get_by_role('button',name='Review this exact assembly',exact=True).wait_for(timeout=30000)
        frame=decoded_frame(page.locator('.ew-player video'));assert max(frame['meanRGB'])>20
        coverage=page.get_by_role('region',name='Assembly coverage');expect(coverage).to_contain_text('00:04.0 output from 00:06.0 confirmed · 00:08.0 reference recording')
        expect(coverage).to_contain_text('00:02.0 inside the confirmed bounds is omitted.')
        expect(coverage).to_contain_text('Omitted episode 00:02.0–00:04.0 · 00:02.0 · No selected camera covers this interval.')
        expect(coverage).to_contain_text('00:04.0–00:06.0 → 00:02.0–00:04.0')
        expect(page.locator('.episode-workshop')).not_to_contain_text('no speech removed')
        expect(page.locator('.ew-next .ew-review-warning')).to_have_text('00:02.0 omitted inside confirmed bounds')
        page.screenshot(path=str(EVIDENCE/f'fix1-gap-{width}-player.png'))
        coverage.get_by_text('Outside confirmed bounds',exact=True).click();coverage.scroll_into_view_if_needed()
        expect(coverage).to_contain_text('00:06.0–00:08.0 · After confirmed end.');page.screenshot(path=str(EVIDENCE/f'fix1-gap-{width}-coverage.png'))
        page.get_by_role('button',name='Review this exact assembly',exact=True).click();page.get_by_role('button',name='Confirm in native review',exact=True).click()
        page.get_by_role('button',name='All episodes',exact=True).click();page.get_by_role('button',name='Synthetic camera gap',exact=True).click()
        expect(page.locator('.mw-status')).to_have_text('Assembly review recorded');returned=decoded_frame(page.locator('.ew-player video'));assert max(returned['meanRGB'])>20
        assert page.locator('.episode-workshop').evaluate('(el)=>el.scrollWidth<=el.clientWidth+1');assert not errors
        (EVIDENCE/f'fix1-gap-{width}.json').write_text(json.dumps({'simulated_native_boundary':True,'generated_clock_fixture':True,'initial_frame':frame,'same_dialog_return_frame':returned,'episode':E.load(studio,eid)},indent=2))
    finally:page.close()


def test_reconnected_episode_prepares_current_grant_preview(rig,browser,tmp_path,monkeypatch):
    from .test_episode_workshop import FakeBooks,recording
    from ave.episode import Episode
    from ave.studio import episode_registry as R,episode_workshop as E
    import shutil
    studio,server=rig;monkeypatch.setattr(R,'NativeBooks',FakeBooks);ep=Episode(tmp_path/'external');ep.create(title='Reconnect this recording');raw=recording(tmp_path);shutil.copyfile(raw,ep.raw/raw.name)
    row=R.register_selected(studio,ep.root,derived_consent=True);eid=row['id'];first=run(studio,eid,'episode-intake')['previews'][0];seed_transcript(studio,eid)
    page=browser.new_page(viewport={'width':390,'height':844})
    try:
        page.goto(server.url()+'#/workbench');page.get_by_role('button',name='Episodes',exact=True).click();page.get_by_role('button',name='Reconnect this recording',exact=True).click();page.get_by_label('Name S1').wait_for()
        page.get_by_role('button',name='All episodes',exact=True).click();R.register_selected(studio,ep.root,derived_consent=True,reconnect=eid)
        page.get_by_role('button',name='Reconnect this recording',exact=True).click();page.get_by_role('button',name='Prepare recordings',exact=True).click();page.get_by_label('Name S1').wait_for(timeout=30000)
        current=E.load(studio,eid)['sources'][0]['preview'];assert current['grant_revision']==2 and current['id']!=first['id']
        player=page.locator('.ew-player audio');player.evaluate('(el)=>el.play()');wait_for(lambda:player.evaluate('(el)=>el.currentTime>.1&&el.readyState>=2'),page)
        assert page.locator('.mw-error:visible').count()==0
    finally:page.close()


@pytest.mark.parametrize('width',[1440,390])
def test_audio_rough_compare_restore_and_exact_review(rig,browser,width,tmp_path,synthetic_final_asr):
    from .test_episode_workshop import ready,confirm
    from ave.episode import write_json
    from ave.studio import episode_registry as R
    studio,server=rig;eid,original,app=ready(studio,tmp_path)
    from ave import library
    from .test_episode_workshop import recording
    sting=library.add_file(studio.sp,recording(tmp_path,'fixture-sting.wav',duration=4),source={'type':'own'},by='synthetic',attested=False,alt='Synthetic sting');library.attest_license(studio.sp,sting['id'],'simulated fixture')
    with R.resolve(studio,eid) as (ep,row):
        transcript=R.json_file(ep.artifact('transcript.json'))
        transcript['words'].insert(1,{'w':'um','start':.8,'end':.9,'speaker':'S1'})
        write_json(ep.artifact('transcript.json'),transcript);source=row['sources'][0]
    confirm(app,eid,'speakers',{'names':{'S1':'Synthetic host','S2':'Synthetic guest'}})
    confirm(app,eid,'mapping',{'reference':source['id'],'sources':[source['id']],'cameras':{}})
    confirm(app,eid,'bounds',{'in':.25,'out':3.75})
    assembly=run(studio,eid,'episode-assembly');confirm(app,eid,'assembly',{**assembly['edit'],'artifact_id':assembly['artifact']['id']})
    server.app.native_authority=FakeAuthority()
    page=browser.new_page(viewport={'width':width,'height':960 if width==1440 else 844});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    def native_confirm(token):
        item=server.app.episode_checkpoints.prepared[token];item['viewed']-=3
        return server.app.episode_checkpoints.confirm_native(token,server.app.pin.reveal())
    page.expose_function('fixtureConfirmEpisode',native_confirm)
    page.add_init_script("window.aveNative={confirmEpisode:token=>window.fixtureConfirmEpisode(token)}")
    try:
        page.goto(server.url()+'#/workbench');page.get_by_role('button',name='Episodes',exact=True).click();page.get_by_role('button',name='Synthetic conversation',exact=True).click()
        page.get_by_role('button',name='Continue to rough cut').click();page.get_by_role('button',name='Analyze and render rough cut').click()
        page.get_by_role('button',name='Review this exact rough cut').wait_for(timeout=30000)
        player=page.locator('.ew-player audio');player.evaluate('(el)=>el.play()');wait_for(lambda:player.evaluate('(el)=>el.currentTime>.1&&el.readyState>=2'),page)
        page.screenshot(path=str(EVIDENCE/f'finishing-rough-{width}.png'))
        page.get_by_role('button',name='Before · assembly').first.click();expect(page.locator('.ew-player-label')).to_contain_text('Assembly · before cut');page.get_by_role('button',name='After · rough seam').first.click();expect(page.locator('.ew-player-label')).to_contain_text('Rough cut · after cut');page.get_by_role('button',name='After · rough seam').first.scroll_into_view_if_needed();page.screenshot(path=str(EVIDENCE/f'finishing-rough-comparison-{width}.png'))
        page.get_by_role('button',name='Listen in original').first.click();player=page.locator('.ew-player audio');wait_for(lambda:player.evaluate('(el)=>el.currentTime>.1&&el.readyState>=2'),page)
        page.get_by_label('Restore cut k001').check();page.get_by_role('button',name='Render revised cleanup').click()
        page.get_by_text('Rough cut v2',exact=False).first.wait_for(timeout=30000)
        page.get_by_role('button',name='Review this exact rough cut').click();page.get_by_role('button',name='Confirm in native review').click()
        expect(page.get_by_text('Rough cut reviewed.',exact=True)).to_be_visible()
        page.get_by_role('button',name='Continue to opening & closing').click()
        page.get_by_label('Episode title').fill('Synthetic episode')
        page.get_by_label('Enable opening').check();page.get_by_label('opening audio component',exact=True).select_option('excerpt')
        page.get_by_label('opening audio start',exact=True).fill('0');page.get_by_label('opening audio end or duration',exact=True).fill('1.4')
        page.get_by_role('button',name='Correct opening component 1 captions').click();page.get_by_role('button',name=re.compile(r'Hello$')).first.click();page.get_by_label('Correct excerpt word').fill('Fixture correction');page.get_by_role('button',name='Save excerpt correction').click()
        expect(page.get_by_role('button',name='Move opening component 1 earlier')).to_be_disabled();page.get_by_role('button',name='Add opening component').click();page.get_by_label('opening audio component 2').select_option('asset');page.get_by_label('opening audio start 2').fill('0');page.get_by_label('opening audio end or duration 2').fill('.5');page.get_by_label('opening licensed recording 2').select_option(sting['id'])
        page.get_by_label('opening licensed recording 2').scroll_into_view_if_needed();page.screenshot(path=str(EVIDENCE/f'finishing-sequence-edit-{width}.png'))
        page.get_by_role('button',name='Preview opening & closing').click()
        page.get_by_role('button',name='Play opening').wait_for(timeout=30000)
        from ave.studio import episode_workshop as E
        page.get_by_role('button',name='Change plan').click();expect(page.get_by_label('opening audio component 2')).to_have_value('asset')
        page.get_by_label('opening audio component 2').select_option('excerpt');page.get_by_label('opening audio start 2').fill('0');page.get_by_label('opening audio end or duration 2').fill('1.4');page.get_by_role('button',name='Preview opening & closing').click();page.get_by_role('button',name='Play opening').wait_for(timeout=30000)
        components=E.load(studio,eid)['sequences'][-1]['args']['opening']['components'];assert len(components)==2 and set(components[0])=={'kind','start','end','corrections'} and set(components[1])=={'kind','start','end'}
        page.get_by_role('button',name='Change plan').click();page.get_by_label('opening audio component',exact=True).select_option('asset');page.get_by_label('opening audio start',exact=True).fill('0');page.get_by_label('opening audio end or duration',exact=True).fill('.5');page.get_by_label('opening licensed recording',exact=True).select_option(sting['id']);page.get_by_role('button',name='Preview opening & closing').click();page.get_by_role('button',name='Play opening').wait_for(timeout=30000)
        components=E.load(studio,eid)['sequences'][-1]['args']['opening']['components'];assert len(components)==2 and set(components[0])=={'kind','start','duration','id','sha256'} and set(components[1])=={'kind','start','end'}
        page.get_by_role('button',name='Change plan').click();page.get_by_label('opening audio component',exact=True).select_option('excerpt');page.get_by_label('opening audio start',exact=True).fill('0');page.get_by_label('opening audio end or duration',exact=True).fill('1.4');page.get_by_label('opening audio component 2').select_option('asset');page.get_by_label('opening audio start 2').fill('0');page.get_by_label('opening audio end or duration 2').fill('.5');page.get_by_label('opening licensed recording 2').select_option(sting['id']);page.get_by_label('Episode title').fill('Revised synthetic episode');page.get_by_role('button',name='Preview opening & closing').click();page.get_by_role('button',name='Play opening').wait_for(timeout=30000)
        assert len(E.load(studio,eid)['sequences'][-1]['args']['opening']['components'])==2
        assert page.get_by_role('button',name='Review this exact plan').is_disabled()
        page.get_by_role('button',name='Play opening').click()
        player=page.locator('.ew-player audio');wait_for(lambda:player.evaluate('(el)=>el.currentTime>.1&&el.readyState>=2'),page)
        page.screenshot(path=str(EVIDENCE/f'finishing-sequence-{width}.png'))
        page.get_by_role('button',name='Review this exact plan').click();page.get_by_role('button',name='Confirm in native review').click()
        expect(page.get_by_text('Opening and closing reviewed.',exact=True)).to_be_visible()
        page.get_by_role('button',name='Continue to final cut').click();page.get_by_label('Dialogue gain dB').fill('0')
        page.get_by_role('button',name='Render final programme').click();page.get_by_role('button',name='Review this exact final cut').wait_for(timeout=30000)
        player=page.locator('.ew-player audio');player.evaluate('(el)=>el.play()');wait_for(lambda:player.evaluate('(el)=>el.currentTime>.1&&el.readyState>=2'),page)
        expect(page.locator('.ew-inspector')).to_contain_text('LUFS');page.locator('summary').filter(has_text='Selected caption sidecar').click();expect(page.locator('.ew-inspector')).to_contain_text('wording and completeness need review')
        page.screenshot(path=str(EVIDENCE/f'finishing-final-{width}.png'))
        page.get_by_role('button',name='Review this exact final cut').click();page.get_by_role('button',name='Confirm in native review').click();expect(page.get_by_text('Final cut reviewed.',exact=True)).to_be_visible()
        page.locator('.ew-caption-row summary').filter(has_text='Hello').first.click();page.get_by_role('button',name='Edit Hello').first.click();page.get_by_label('Correct component word').fill('Welcome');expect(page.get_by_text('Original: Hello',exact=False)).to_be_visible();page.get_by_role('button',name='Listen to original word in rough cut').click();wait_for(lambda:page.locator('.ew-player audio').evaluate('(el)=>el.readyState>=2'),page);page.get_by_label('Correct component word').scroll_into_view_if_needed();page.screenshot(path=str(EVIDENCE/f'finishing-component-correction-{width}.png'));page.get_by_role('button',name='Apply component correction').click();page.get_by_role('button',name='Preview opening & closing').click();page.get_by_role('button',name='Play opening').wait_for(timeout=30000);page.get_by_role('button',name='Play opening').click();wait_for(lambda:page.locator('.ew-player audio').evaluate('(el)=>el.currentTime>.1'),page);page.get_by_role('button',name='Review this exact plan').click();page.get_by_role('button',name='Confirm in native review').click();page.get_by_role('button',name='Render final programme').click();page.get_by_role('button',name='Review this exact final cut').wait_for(timeout=30000)
        expect(page.locator('.ew-transcript')).to_contain_text('Welcome');page.locator('.ew-caption-row summary').filter(has_text='Hello').last.click();page.get_by_role('button',name='Edit Hello').last.click()
        page.get_by_label('Corrected word').fill('Greetings');page.get_by_label('Word start adjustment seconds').fill('.05');page.get_by_role('button',name='Apply correction').click()
        page.get_by_role('button',name='Render final programme').click();page.get_by_text('Final cut v3',exact=False).wait_for(timeout=30000)
        player=page.locator('.ew-player audio');player.evaluate('(el)=>el.play()');wait_for(lambda:player.evaluate('(el)=>el.currentTime>.1&&el.readyState>=2'),page)
        expect(page.locator('.ew-transcript')).to_contain_text('difference');page.locator('summary').filter(has_text='Selected caption sidecar').click();expect(page.locator('.ew-transcript')).to_contain_text('Greetings')
        page.screenshot(path=str(EVIDENCE/f'finishing-final-revision-{width}.png'))
        if width==390:
            page.locator('.ew-difference').first.scroll_into_view_if_needed();page.screenshot(path=str(EVIDENCE/'finishing-final-revision-390-difference.png'))
        page.get_by_role('button',name='Review this exact final cut').click();page.get_by_role('button',name='Confirm in native review').click();expect(page.get_by_text('Final cut reviewed.',exact=True)).to_be_visible()
        assert not errors
    finally:
        page.close();app.close()


@pytest.mark.parametrize('width',[1440,390])
def test_av_sequence_review_shows_real_frames(rig,browser,width,tmp_path,synthetic_final_asr):
    from .test_episode_workshop import gap_episode,confirm
    from ave.studio.ui.api import StudioApp
    from ave import library
    from PIL import Image
    studio,server=rig;eid,_=gap_episode(studio,tmp_path);app=StudioApp(studio,fixtures=False,pin='123456',native_authority=FakeAuthority())
    assembly=run(studio,eid,'episode-assembly');a={**assembly['edit'],'artifact_id':assembly['artifact']['id']};confirm(app,eid,'assembly',a)
    rough=run(studio,eid,'episode-rough',assembly=a,policy='gentle',restore=[]);r={**rough['edit'],'artifact_id':rough['artifact']['id']};confirm(app,eid,'rough',r)
    picture=tmp_path/'red.png';Image.new('RGB',(320,180),(240,20,30)).save(picture)
    license_entry={'source':'synthetic','url':'https://example.org/fixture','license':'CC BY','license_url':'https://example.org/license','license_text':'Synthetic test license','attribution':{'required':True,'text':'Synthetic red image by fixture'}}
    asset=library.add_file(studio.sp,picture,source={'type':'import'},license_entry=license_entry,by='synthetic',attested=False,alt='Synthetic red image');library.attest_license(studio.sp,asset['id'],'simulated fixture')
    server.app.native_authority=FakeAuthority()
    page=browser.new_page(viewport={'width':width,'height':960 if width==1440 else 844});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    def native_confirm(token):
        server.app.episode_checkpoints.prepared[token]['viewed']-=3
        return server.app.episode_checkpoints.confirm_native(token,server.app.pin.reveal())
    page.expose_function('fixtureConfirmEpisode',native_confirm);page.add_init_script("window.aveNative={confirmEpisode:token=>window.fixtureConfirmEpisode(token)}")
    try:
        page.goto(server.url()+'#/workbench');page.get_by_role('button',name='Episodes',exact=True).click();page.get_by_role('button',name='Synthetic camera gap',exact=True).click()
        page.get_by_role('button',name='Continue to opening & closing').click();page.get_by_label('Episode title').fill('Synthetic episode');page.get_by_label('Host names').fill('Synthetic host');page.get_by_label('Enable opening').check()
        page.get_by_role('button',name='Preview opening & closing').click();page.get_by_role('button',name='Play opening').wait_for(timeout=90000)
        player=page.locator('.ew-player video');player.evaluate("el=>new Promise(resolve=>{el.addEventListener('seeked',resolve,{once:true});el.currentTime=1.5})")
        frame=decoded_frame(player);assert max(frame['meanRGB'])>5
        page.get_by_role('button',name='Play opening').click();wait_for(lambda:player.evaluate('(el)=>el.currentTime>.1&&el.readyState>=2'),page)
        expect(page.get_by_text('None',exact=True)).to_be_visible()
        page.locator('.ew-main').evaluate('el=>el.scrollTop=0');page.screenshot(path=str(EVIDENCE/f'finishing-av-sequence-{width}.png'))
        if width==390:
            page.get_by_role('heading',name='Opening',exact=True).scroll_into_view_if_needed();page.screenshot(path=str(EVIDENCE/'finishing-av-sequence-390-plan.png'))
        page.get_by_role('button',name='Review this exact plan').click();page.get_by_role('button',name='Confirm in native review').click();expect(page.get_by_text('Opening and closing reviewed.',exact=True)).to_be_visible()
        page.get_by_role('button',name='Continue to final cut').click();page.get_by_label('Burn captions').check();page.get_by_role('button',name='Add name title',exact=True).click();page.get_by_role('button',name='Add B-roll',exact=True).click()
        page.get_by_label('Name title text').fill('Synthetic host');page.get_by_label('Name title role').fill('Host');page.get_by_label('Name title start').fill('.25');page.get_by_label('Name title duration').fill('2')
        page.get_by_label('B-roll',exact=True).locator(f'option[value="{asset["id"]}"]').wait_for(state='attached');page.get_by_label('B-roll',exact=True).select_option(asset['id']);page.get_by_label('B-roll start',exact=True).fill('.5');page.get_by_label('B-roll duration',exact=True).fill('1')
        page.get_by_role('button',name='Add B-roll',exact=True).click();page.get_by_label('B-roll 2',exact=True).select_option(asset['id']);page.get_by_label('B-roll start 2').fill('2');page.get_by_label('B-roll duration 2').fill('1')
        page.get_by_role('button',name='Add name title',exact=True).click();page.get_by_label('Name title text 2').fill('Synthetic guest');page.get_by_label('Name title role 2').fill('Guest');page.get_by_label('Name title side 2').select_option('right');page.get_by_label('Name title start 2').fill('1.5');page.get_by_label('Name title duration 2').fill('2')
        page.get_by_label('B-roll duration',exact=True).scroll_into_view_if_needed();page.get_by_role('button',name='Listen at placement').first.click();wait_for(lambda:page.locator('.ew-player video').evaluate('(el)=>el.readyState>=2&&el.currentTime>=.5'),page);page.screenshot(path=str(EVIDENCE/f'finishing-final-controls-{width}.png'))
        page.get_by_role('button',name='Render final programme').click();page.get_by_role('button',name='Review this exact final cut').wait_for(timeout=120000)
        from ave.studio import episode_workshop as E
        final=E.load(studio,eid)['finals'][-1];at=final['render']['details']['report']['joins_s'][0]+1.25
        player=page.locator('.ew-player video');player.evaluate('(el,t)=>new Promise(resolve=>{el.addEventListener("seeked",resolve,{once:true});el.currentTime=t})',at)
        frame=decoded_frame(player);assert frame['meanRGB'][0]>frame['meanRGB'][1]*2
        import base64
        png=player.evaluate('el=>{const c=document.createElement(\"canvas\");c.width=el.videoWidth;c.height=el.videoHeight;c.getContext(\"2d\").drawImage(el,0,0);return c.toDataURL(\"image/png\").split(\",\")[1]}')
        (EVIDENCE/f'finishing-av-frame-{width}.png').write_bytes(base64.b64decode(png))
        player.evaluate('(el)=>el.play()');wait_for(lambda:player.evaluate('(el)=>el.currentTime>0&&el.readyState>=2'),page)
        expect(page.locator('.ew-inspector')).to_contain_text('LUFS');expect(page.locator('.ew-transcript')).to_contain_text('comparison')
        page.locator('.ew-main').evaluate('el=>el.scrollTop=0');page.screenshot(path=str(EVIDENCE/f'finishing-av-final-{width}.png'))
        page.get_by_role('button',name='Revise sound & captions').click();expect(page.get_by_label('Name title text 2')).to_have_value('Synthetic guest');expect(page.get_by_label('B-roll start 2')).to_have_value('2');page.get_by_label('Dialogue gain dB').fill('-1');page.get_by_role('button',name='Render final programme').click();page.get_by_role('button',name='Review this exact final cut').wait_for(timeout=120000)
        latest=E.load(studio,eid)['finals'][-1];assert len(latest['spec']['motion'])==2 and len(latest['spec']['broll'])==2
        player=page.locator('.ew-player video');at=latest['render']['details']['report']['joins_s'][0]+2.3;player.evaluate('(el,t)=>new Promise(resolve=>{el.addEventListener("seeked",resolve,{once:true});el.currentTime=t})',at);frame=decoded_frame(player);assert frame['meanRGB'][0]>frame['meanRGB'][1]*2;png=player.evaluate('el=>{const c=document.createElement("canvas");c.width=el.videoWidth;c.height=el.videoHeight;c.getContext("2d").drawImage(el,0,0);return c.toDataURL("image/png").split(",")[1]}');(EVIDENCE/f'finishing-av-second-placement-{width}.png').write_bytes(base64.b64decode(png));player.evaluate('(el)=>el.play()');wait_for(lambda:player.evaluate('(el)=>el.currentTime>0&&el.readyState>=2'),page)
        page.get_by_role('button',name='Review this exact final cut').click();page.get_by_role('button',name='Confirm in native review').click();expect(page.get_by_text('Final cut reviewed.',exact=True)).to_be_visible()
        assert not errors
    except Exception:
        print('AV final GUI failure:',json.dumps({'page_errors':errors,'body':page.locator('body').inner_text()}))
        raise
    finally:page.close();app.close()


@pytest.mark.parametrize('width',[1440,390])
def test_combined_session_order_and_render_gui(rig,browser,tmp_path,width):
    import uuid
    from .test_episode_workshop import ready,run,confirm
    from ave.studio import episode_registry as R,episode_checkpoints as C,episode_workshop as E
    studio,server=rig;eid,source,app=ready(studio,tmp_path)
    with R.resolve(studio,eid) as (ep,row):mapping=C.latest(studio,eid,'mapping','main')['payload']
    confirm(app,eid,'mapping',mapping,session='second');confirm(app,eid,'bounds',{'in':1,'out':2},session='second')
    first=run(studio,eid,'episode-assembly');confirm(app,eid,'assembly',{**first['edit'],'artifact_id':first['artifact']['id']})
    with R.resolve(studio,eid) as (ep,row):args={'episode':eid,'grant_revision':row['grant_revision'],'input_sha256':E.state_hash(ep,row,'episode-assembly','second'),'request_id':str(uuid.uuid4()),'session':'second'}
    j={'id':'job_'+uuid.uuid4().hex[:24],'operation':'episode-assembly','args':args};j['episode_descriptor']=E.descriptor(studio,j['operation'],args);second=E.execute(studio,j);confirm(app,eid,'assembly',{**second['edit'],'artifact_id':second['artifact']['id']},session='second')
    page=browser.new_page(viewport={'width':width,'height':960 if width==1440 else 844});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        page.goto(server.url()+'#/workbench');page.get_by_role('button',name='Episodes',exact=True).click();page.get_by_role('button',name=E.load(studio,eid)['label'],exact=True).click()
        pending=[];pattern='**/api/studio/episodes/'+eid+'/view?*'
        page.route(pattern,lambda route:pending.append(route))
        select=page.get_by_label('Session',exact=True);select.select_option('combined')
        expect(select).to_be_disabled();expect(page.get_by_role('button',name='Rough cut',exact=True)).to_be_disabled()
        # Rapid synthetic events cannot submit another context or cancel this
        # matching read. Native disabled controls also prevent ordinary clicks.
        select.evaluate("el=>{el.value='main';el.dispatchEvent(new Event('change'));}")
        expect(select).to_have_value('combined');assert len(pending)==1
        player=page.locator('.ew-player audio');player.evaluate('(el)=>el.play()');wait_for(lambda:player.evaluate('(el)=>el.currentTime>.1&&el.readyState>=2'),page)
        pending.pop().fulfill(status=503,json={'error':'Synthetic delayed session read failure'})
        expect(select).to_have_value('main');expect(select).to_be_enabled()
        select.select_option('combined');expect(page.get_by_role('button',name='Rough cut',exact=True)).to_be_disabled()
        wait_for(lambda:len(pending)==1,page);pending.pop().continue_();page.unroute(pattern)
        expect(page.get_by_text('Included sessions in order')).to_be_visible();page.get_by_role('button',name='Move second earlier').click();page.get_by_role('button',name='Build & render assembly').click()
        expect(page.get_by_role('button',name='Review this exact assembly')).to_be_visible(timeout=60000);expect(page.get_by_text('Rendered order and coverage')).to_be_visible();assert '1. second' in page.locator('.ew-inspector').inner_text()
        assert page.get_by_role('button',name='Move second earlier').is_disabled();page.get_by_role('button',name='Move main earlier').click();expect(page.get_by_text('Session order has unapplied changes.',exact=False)).to_be_visible();assert page.get_by_role('button',name='Review this exact assembly').count()==0
        page.get_by_role('button',name='Discard order changes').click();expect(page.get_by_role('button',name='Review this exact assembly')).to_be_visible();assert page.get_by_role('button',name='Move second earlier').is_disabled()
        player=page.locator('.ew-player audio');player.evaluate('(el)=>el.play()');wait_for(lambda:player.evaluate('(el)=>el.currentTime>.1&&el.readyState>=2'),page)
        page.screenshot(path=str(EVIDENCE/f'finishing-combined-{width}.png'));assert not errors
        page.route(pattern,lambda route:pending.append(route));page.get_by_label('Session',exact=True).select_option('main');wait_for(lambda:len(pending)==1,page)
        page.get_by_role('button',name='Close',exact=True).click();pending.pop().continue_();expect(page.get_by_role('dialog',name='Media · Episode')).to_have_count(0)
    finally:page.close();app.close()
