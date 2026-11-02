"""Explicitly provisioned full conversation GUI replay, never a suite prerequisite.

Source ASR/sync are hash-pinned retained local-engine evidence. All new renders,
final-media ASR, reviews, selections, shorts and packages run through this GUI.
Native decisions are simulated; no provider or real Resolve action is called.
"""
import json,os,shutil,re,zipfile
from pathlib import Path
import pytest
from .test_workbench_browser import browser,rig,wait_for
from .test_episode_workshop import FakeAuthority,FakeBooks
from ave.studio import episode_registry as R,episode_workshop as E
from ave.publish.safety import sha256_file
REPLAY=os.getenv('AVE_RELEASE_REPLAY')
EVIDENCE_ROOT=os.getenv('AVE_RELEASE_EVIDENCE')
ASR_CONFIGURATION=os.getenv('AVE_RELEASE_ASR_CONFIG')
PACKAGED_WORKER=os.getenv('AVE_RELEASE_PACKAGED_WORKER')


@pytest.mark.real_motion
@pytest.mark.parametrize('mode,width',[('audio',390),('audiovisual',1440)])
def test_provisioned_complete_release_gui(rig,browser,tmp_path,monkeypatch,mode,width):
    manifest=REPLAY
    if not manifest:pytest.skip('Set AVE_RELEASE_REPLAY to an explicitly provisioned retained conversation manifest')
    entry=next(x for x in json.loads(Path(manifest).read_text()) if x['mode']==f'{mode}-{width}')
    root=tmp_path/'conversation';shutil.copytree(entry['retained_root'],root)
    assert sha256_file(root/'work/transcript.json')==entry['transcript_sha256']
    assert sha256_file(root/'work/sync.json')==entry['sync_sha256']
    studio,server=rig;monkeypatch.setattr(R,'NativeBooks',FakeBooks);server.app.native_authority=FakeAuthority()
    if not ASR_CONFIGURATION:pytest.skip('Provision AVE_RELEASE_ASR_CONFIG for actual final-media local ASR')
    import sys
    from ave import config
    from ave.studio import workbench
    pinned=json.loads(Path(ASR_CONFIGURATION).read_text());configuration=pinned['configuration'];cli=Path(pinned['cli'])
    assert sha256_file(cli)==configuration['binary_sha256']
    monkeypatch.setattr(config,'fluidaudio_cli',lambda:cli);monkeypatch.setattr(E,'final_asr_configuration',lambda:configuration)
    worker=tmp_path/'pinned_local_speech_worker.py'
    worker.write_text('from pathlib import Path\nfrom ave import config\nfrom ave.studio import episode_workshop as E,workbench_worker as W\nconfig.fluidaudio_cli=lambda:Path('+repr(str(cli))+')\nE.final_asr_configuration=lambda:'+repr(configuration)+'\nraise SystemExit(W.main())\n')
    if PACKAGED_WORKER:
        executable=Path(PACKAGED_WORKER).resolve(strict=True)
        monkeypatch.setattr(workbench,'worker_argv',lambda root,jid:[str(executable),'--workbench-worker',str(root),jid])
        runtime={'kind':'packaged worker with source-hosted exact parent/UI','executable':str(executable),'executable_sha256':sha256_file(executable)}
    else:
        monkeypatch.setattr(workbench,'worker_argv',lambda root,jid:[sys.executable,str(worker),str(root),jid])
        runtime={'kind':'source-hosted worker with explicitly pinned packaged speech engine','executable':sys.executable,'wrapper_sha256':sha256_file(worker)}
    originals={str(p):sha256_file(p) for p in (root/'raw').rglob('*') if p.is_file()}
    evidence=Path(EVIDENCE_ROOT or str(tmp_path/'evidence'));evidence.mkdir(parents=True,exist_ok=True)
    (evidence/f'complete-{mode}-{width}-runtime.json').write_text(json.dumps({'worker':runtime,'final_asr_configuration':configuration,'simulated_native':True},indent=2))
    context=browser.new_context(viewport={'width':width,'height':1000 if width==1440 else 844});page=context.new_page();page.set_default_timeout(300000);errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    def selected():return R.register_selected(studio,root,derived_consent=True)
    def confirm(token):
        server.app.episode_checkpoints.prepared[token]['viewed']-=3
        return server.app.episode_checkpoints.confirm_native(token,server.app.pin.reveal())
    page.expose_function('fixtureSelected',selected);page.expose_function('fixtureConfirm',confirm)
    page.add_init_script('window.aveNative={selectEpisode:()=>window.fixtureSelected(),confirmEpisode:t=>window.fixtureConfirm(t)}')
    def approve(label):
        page.get_by_role('button',name=label,exact=True).click();page.get_by_role('button',name='Confirm in native review',exact=True).click()
    def decoded():
        player=page.locator('.ew-player audio' if mode=='audio' else '.ew-player video');player.evaluate('(el)=>el.play()')
        wait_for(lambda:player.evaluate('(el)=>el.currentTime>.1&&el.readyState>=2'),page,seconds=30);player.evaluate('(el)=>el.pause()')
    try:
        page.goto(server.url()+'#/workbench');page.get_by_role('button',name='Episodes',exact=True).click();page.get_by_role('button',name='Open episode folder',exact=True).click()
        page.get_by_role('button',name='Recordings',exact=True).click();page.get_by_role('button',name='Prepare recordings',exact=True).click()
        page.get_by_label('Name S1').wait_for()
        for sid in ('S1','S2'):page.get_by_label('Name '+sid).fill('Synthetic '+sid)
        approve('Review speaker names')
        if mode!='audio':
            for name,sid in [('camera-a.mp4','S1'),('camera-b.mp4','S2')]:page.get_by_label('Speaker in '+name).select_option(sid)
        approve('Review source & camera mapping' if mode!='audio' else 'Review audio session')
        page.get_by_label('Session start seconds').fill('0');page.get_by_label('Session end seconds').fill('133.3');approve('Review session bounds')
        page.get_by_role('button',name='Build & render assembly',exact=True).click();page.get_by_role('button',name='Review this exact assembly',exact=True).wait_for();decoded();approve('Review this exact assembly')
        page.get_by_role('button',name='Continue to rough cut',exact=True).click();page.get_by_role('button',name='Analyze and render rough cut',exact=True).click();page.get_by_role('button',name='Review this exact rough cut',exact=True).wait_for();decoded();approve('Review this exact rough cut')
        page.get_by_role('button',name='Continue to opening & closing',exact=True).click();page.get_by_label('Episode title',exact=True).fill('Listening together')
        page.get_by_label('Enable opening').check();page.get_by_label('Enable closing').uncheck()
        if mode=='audio':
            page.get_by_label('opening audio component',exact=True).select_option('excerpt');page.get_by_label('opening audio start',exact=True).fill('0.6');page.get_by_label('opening audio end or duration',exact=True).fill('3.5')
        page.get_by_role('button',name='Preview opening & closing',exact=True).click();page.get_by_role('button',name='Play opening',exact=True).wait_for();page.get_by_role('button',name='Play opening',exact=True).click();decoded();approve('Review this exact plan')
        page.get_by_role('button',name='Continue to final cut',exact=True).click()
        # The exact generated speech script says "returning to. I wrote".
        # Correct the retained ASR's extra token through issued GUI word IDs;
        # the retained transcript and its hash must remain unchanged.
        eid=next(iter(R.records(studio.sp)));word_pages=[];caption_url=page.evaluate('location.origin')+f'/api/studio/episodes/{eid}/view/caption-words?session=main&page='
        first_words=page.request.get(caption_url+'0').json()
        for n in range(first_words['pages']):word_pages.append(first_words if n==0 else page.request.get(caption_url+str(n)).json())
        issued=[(n,i,w) for n,value in enumerate(word_pages) for i,w in enumerate(value['words'])]
        hit=next(i for i,(_,_,w) in enumerate(issued[:-1]) if w['w'].lower()=='to' and issued[i+1][2]['w'].lower()=='the.' and issued[i-1][2]['w'].lower()=='returning')
        n,local,word=issued[hit];following=issued[hit+1][2]
        page.get_by_text('Correct caption wording',exact=True).click()
        for _ in range(n):page.get_by_role('button',name='Next caption words',exact=True).click()
        to_rank=sum(w['w']=='to' for w in word_pages[n]['words'][:local])
        page.locator('.ew-phrase-editor').get_by_role('button',name=re.compile(r' to$')).nth(to_rank).click()
        page.get_by_role('button',name='Include next word',exact=True).click();page.get_by_text('Original: to the.',exact=True).wait_for()
        page.get_by_label('Corrected word',exact=True).fill('to.');page.screenshot(path=str(evidence/f'complete-{mode}-{width}-phrase-correction.png'))
        page.get_by_role('button',name='Apply correction',exact=True).click()
        (evidence/f'complete-{mode}-{width}-phrase-correction.json').write_text(json.dumps({'rough':first_words['rough'],'original_words':[word,following],'replacement':'to.','source_transcript_sha256':entry['transcript_sha256']},indent=2))
        if mode!='audio':page.get_by_label('Final export quality').select_option('episode_hd')
        page.get_by_role('button',name='Render final programme',exact=True).click();page.get_by_role('button',name='Review this exact final cut',exact=True).wait_for();decoded()
        if mode!='audio':
            visible=page.locator('.ew-player video');visible.evaluate('(el)=>{el.pause();el.currentTime=2;}')
            wait_for(lambda:visible.evaluate('(el)=>!el.seeking&&el.readyState>=2&&Math.abs(el.currentTime-2)<.05'),page)
        page.screenshot(path=str(evidence/f'complete-{mode}-{width}-final.png'));approve('Review this exact final cut')
        page.get_by_text('Final cut reviewed.',exact=True).wait_for()
        page.get_by_role('button',name='Clips & shorts',exact=True).click()
        eid=next(iter(R.records(studio.sp)))
        proposal=page.request.get(page.evaluate('location.origin')+'/api/studio/episodes/'+eid+'/candidates?session=main').json()
        first=next(c for c in proposal['candidates'] if c['text'].startswith('I am interested in the moment'))
        last=next(c for c in proposal['candidates'] if c['text'].startswith('How do we make room for other people'))
        selection={'final':proposal['final'],'first_candidate':first,'last_candidate':last,'start':first['start'],'end':last['end'],'native_occurrences':[w for c in proposal['candidates'] if first['start']<=c['start']<=last['start'] for w in c['occurrences']]}
        assert 15<selection['end']-selection['start']<60
        selection_text=' '.join(c['text'] for c in proposal['candidates'] if first['start']<=c['start']<=last['start'])
        assert 'returning to.' in selection_text and 'returning to the.' not in selection_text
        page.locator('.ew-candidate-list button').nth(proposal['candidates'].index(first)).click()
        page.get_by_label('Selection end seconds').fill(str(last['end']));page.get_by_label('Selection title').fill('A complete conversation thought')
        (evidence/f'complete-{mode}-{width}-selection.json').write_text(json.dumps(selection,indent=2))
        page.get_by_role('button',name='Render selection for review',exact=True).click();page.get_by_label('Complete statement A complete conversation thought').check();page.get_by_label('Context reviewed A complete conversation thought').check();approve('Review keep: A complete conversation thought')
        if mode!='audio':
            page.get_by_label('Short show label A complete conversation thought').fill('Listening practice');page.get_by_label('Short title A complete conversation thought').fill('Listening together');page.get_by_role('button',name='Render short: A complete conversation thought',exact=True).click()
            page.get_by_text('Short · A complete conversation thought',exact=True).click();page.get_by_role('button',name='Play short A complete conversation thought',exact=True).click();decoded()
            visible=page.locator('.ew-player video');assert visible.evaluate('(el)=>el.videoWidth===1080&&el.videoHeight===1920')
            for label,at in [('short',1.2),('short-later-caption',16)]:
                visible.evaluate('(el,t)=>{el.pause();el.currentTime=t;}',at)
                wait_for(lambda:visible.evaluate('(el,t)=>!el.seeking&&el.readyState>=2&&Math.abs(el.currentTime-t)<.05',at),page)
                page.screenshot(path=str(evidence/f'complete-{mode}-{width}-{label}.png'))
        page.get_by_role('button',name='Release',exact=True).click();page.get_by_label('Release description').fill('Synthetic conversation release; native reviews simulated.')
        page.get_by_role('button',name='Build release package',exact=True).click();page.get_by_text('Ready to download · final v1',exact=True).wait_for();decoded()
        if mode!='audio':
            visible=page.locator('.ew-player video');visible.evaluate('(el)=>{el.pause();el.currentTime=2;}')
            wait_for(lambda:visible.evaluate('(el)=>!el.seeking&&el.readyState>=2&&Math.abs(el.currentTime-2)<.05'),page)
        page.screenshot(path=str(evidence/f'complete-{mode}-{width}-package.png'))
        package_page=page.context.new_page()
        try:
            package_page.goto(page.evaluate('location.origin')+page.get_by_role('link',name='Play package',exact=True).get_attribute('href'))
            for player in package_page.locator('video,audio').all():
                player.evaluate('(el)=>el.play()');wait_for(lambda:player.evaluate('(el)=>el.readyState>=2&&el.currentTime>.1'),package_page,seconds=30);player.evaluate('(el)=>el.pause()')
                if player.evaluate('(el)=>el.tagName==="VIDEO"'):
                    player.evaluate('(el)=>{el.currentTime=2;}')
                    wait_for(lambda:player.evaluate('(el)=>!el.seeking&&el.readyState>=2&&Math.abs(el.currentTime-2)<.05&&el.videoWidth>0'),package_page)
            package_page.screenshot(path=str(evidence/f'complete-{mode}-{width}-package-playback.png'))
        finally:package_page.close()
        with page.expect_download() as download_info:page.get_by_role('link',name='Download release ZIP',exact=True).click()
        download=download_info.value;downloaded=evidence/f'complete-{mode}-{width}-release.zip';download.save_as(downloaded)
        saved_package=next(j['result'] for j in server.app.workbench.overview()['jobs'] if j['operation']=='episode-package')
        assert sha256_file(downloaded)==saved_package['sha256']
        with zipfile.ZipFile(downloaded) as archive:
            from ave.captions import parse_srt
            caption_path=tmp_path/'downloaded-programme.srt';caption_path.write_bytes(archive.read('captions/programme.srt'))
            package_text=' '.join(c.text for c in parse_srt(caption_path))
            assert 'returning to.' in package_text and 'returning to the.' not in package_text
        assert sha256_file(root/'work/transcript.json')==entry['transcript_sha256']
        assert originals=={p:sha256_file(Path(p)) for p in originals} and not errors
        jobs=server.app.workbench.overview()['jobs'];assert all(j['status']=='done' for j in jobs)
        from ave.captions import parse_srt
        for job in jobs:
            if job['operation'] not in ('episode-select','episode-short'):continue
            artifact=E.artifact(studio,eid,job['result']['artifact']['id']);caption=artifact['details']['caption']
            text=' '.join(c.text for c in parse_srt((studio.sp.root/artifact['file']).parent/caption['filename']))
            assert 'returning to.' in text and 'returning to the.' not in text
        eid=next(iter(R.records(studio.sp)));loaded=E.load(studio,eid)
        (evidence/f'complete-{mode}-{width}.json').write_text(json.dumps({'simulated_native':True,'retained_source_evidence':entry,'source_hashes':originals,'episode':loaded,'jobs':jobs,'studio':str(studio.sp.root)},indent=2))
    except Exception:
        page.screenshot(path=str(evidence/f'complete-{mode}-{width}-failure.png'))
        (evidence/f'complete-{mode}-{width}-failure.txt').write_text(page.locator('body').inner_text())
        raise
    finally:context.close()
