"""Portable Media programme → fresh registered episode GUI continuation.

Synthetic tones/transcript words and simulated native authority only. Service
setup supplies independent source fixtures; authoring/cleanup/adoption/review and
release actions below use the actual browser and durable workbench workers.
"""
import json,sys
from pathlib import Path
import pytest
from playwright.sync_api import expect
from ave import library,editdoc
from ave.episode import write_json
from ave.occurrences import mapped_words
from ave.publish.safety import sha256_file
from ave.studio import video_workshop as V,episode_registry as R,episode_checkpoints as C
from .test_workbench_browser import rig,browser,wait_for
from .test_video_workshop import mutate
from .test_episode_workshop import recording,FakeAuthority

EVIDENCE=Path('/private/tmp/ave-release-adoption-gui');EVIDENCE.mkdir(exist_ok=True)


@pytest.mark.parametrize('width',[1440,390])
def test_independent_reordered_repeated_programme_adopts_and_packages_in_gui(rig,browser,tmp_path,synthetic_final_asr,width,isolated_secrets,monkeypatch):
    studio,server=rig;p=V.create(studio,{'title':'Independent programme adoption','mode':'audio','request_id':'gui-adopt-fixture'});sources=[];originals={}
    for index,word in enumerate(['First.','Second.']):
        original=recording(tmp_path,f'independent-{index}.wav',duration=4+index);originals[str(original)]=sha256_file(original);rec=library.add_file(studio.sp,original,source={'type':'own'},by='synthetic fixture');library.attest_license(studio.sp,rec['id'],'simulated fixture')
        # Distinct imported files may deduplicate identical tone bytes; use
        # independent sample content for the second source.
        if sources and rec['id']==sources[0]['id']:raise AssertionError('Fixture sources must contain independent bytes')
        p=V.add_source(studio,{'project':p['id'],'source_id':rec['id'],'expected_revision':p['revision'],'request_id':'gui-source-'+str(index)})
        ep,meta=V.project(studio.sp,p['id']);tp=ep.work/f'fixture-words-{index}.json';write_json(tp,{'words':[{'w':word,'start':.2,'end':.5}]});meta['project_sources'][rec['id']]['transcript']={'file':ep.rel(tp),'sha256':sha256_file(tp),'clock':'source'};write_json(ep.meta_path,meta);sources.append(rec)
    for rec in [sources[0],sources[1],sources[0]]:p=mutate(studio,p,{'kind':'insert','source':rec['id'],'in':0,'out':1})
    p=mutate(studio,p,{'kind':'music','asset':sources[0]['id'],'gain_db':-20,'duck_db':-12,'fade_in_s':.2,'fade_out_s':.2})
    # The historical ordinary Media renderer verifies local license ledgers
    # itself. Give ONLY this test-owned ordinary worker its isolated MemKeyring;
    # episode/adoption workers retain the real keyless inherited parent bridge.
    from ave.studio import workbench
    original_argv=workbench.worker_argv;ordinary_worker=tmp_path/'isolated_ordinary_worker.py'
    def worker_argv(root,jid):
        job=json.loads(workbench.job_path(studio.sp,jid).read_text())
        if job['operation']!='video-render':return original_argv(root,jid)
        assert job['args']['project']==p['id'] and not V.exact(studio.sp,p['id'],job['args']['version'])[3].get('kept_clip')
        code=("import sys,json\nfrom pathlib import Path\n"
              "def deny(*a,**k):raise AssertionError('OS Keychain/auth access forbidden in isolated fixture')\n"
              "def audit(event,args):\n if event=='import' and args[0].startswith(('keyring.backends.macOS','LocalAuthentication','Security','ave.desktop_auth','ave.desktop_native')):deny()\n"
              "sys.addaudithook(audit)\nimport keyring\nsys.path.insert(0,"+repr(str(Path(__file__).parents[1]))+")\nfrom publish_helpers import MemKeyring\nmem=MemKeyring()\nmem.d="+repr(isolated_secrets.d)+"\nkeyring.set_keyring(mem)\nassert keyring.get_keyring() is mem\nkeyring.core.init_backend=deny\nkeyring.set_keyring=deny\nkeyring.get_password=mem.get_password\nkeyring.set_password=mem.set_password\nkeyring.delete_password=mem.delete_password\n"
              "from ave.studio import workbench_worker as W\nraise SystemExit(W.main())\n")
        ordinary_worker.write_text(code)
        return [sys.executable,str(ordinary_worker),str(root),jid]
    monkeypatch.setattr(workbench,'worker_argv',worker_argv)
    server.app.native_authority=FakeAuthority();context=browser.new_context(viewport={'width':width,'height':1000 if width==1440 else 844});page=context.new_page();page.set_default_timeout(60000);errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    def confirm(token):
        server.app.episode_checkpoints.prepared[token]['viewed']-=3;return server.app.episode_checkpoints.confirm_native(token,server.app.pin.reveal())
    page.expose_function('fixtureConfirmAdopted',confirm);page.add_init_script('window.aveNative={confirmEpisode:t=>window.fixtureConfirmAdopted(t)}')
    def review(label,ready):
        page.get_by_role('button',name=label,exact=True).click();page.get_by_role('button',name='Confirm in native review',exact=True).click();page.get_by_text(ready,exact=True).wait_for()
    def decoded():
        player=page.locator('.ew-player audio');player.evaluate('(el)=>el.play()');wait_for(lambda:player.evaluate('(el)=>el.readyState>=2&&el.currentTime>.1'),page);player.evaluate('(el)=>el.pause()')
    try:
        page.goto(server.url()+'#/workbench');page.get_by_role('button',name='Edit a video',exact=True).click();page.get_by_label('Saved programme').select_option(p['id']);expect(page.locator('.vw-clip')).to_have_count(3)
        page.locator('.vw-clip').nth(1).focus();page.keyboard.press('Alt+ArrowLeft');expect(page.get_by_label('Programme revision')).to_have_value(str(p['version']+1));page.get_by_role('button',name='Cleanup',exact=True).click();page.get_by_label('Cleanup approach').select_option('gentle');page.get_by_role('button',name='Analyze cleanup',exact=True).click();expect(page.locator('.mw-status')).to_contain_text('Cleanup revision',timeout=60000)
        programme=V.load(studio.sp,p['id']);ep,_=V.project(studio.sp,p['id']);expected=mapped_words(ep,programme['doc']);assert [w['w'] for w in expected]==['Second.','First.','First.'];assert programme['doc']['tracks']['music']
        page.get_by_role('button',name='Render programme',exact=True).click();page.get_by_role('link',name='Download finished audio',exact=True).wait_for();player=page.get_by_label('Rendered programme playback');player.evaluate('(el)=>el.play()');wait_for(lambda:player.evaluate('(el)=>el.readyState>=2&&el.currentTime>.1'),page);player.evaluate('(el)=>el.pause()');page.screenshot(path=str(EVIDENCE/f'programme-{width}.png'))
        page.get_by_role('button',name='Continue as an episode',exact=True).click();page.get_by_role('button',name='Build & render assembly',exact=True).wait_for();eid=next(iter(R.records(studio.sp)));assert not any(e['episode']==eid for e in C.events(studio));page.screenshot(path=str(EVIDENCE/f'fresh-assembly-{width}.png'))
        with R.resolve(studio,eid) as (target,row):assert row['adoption']['source_edit_sha256']==programme['sha256'];assert mapped_words(target,R.json_file(editdoc.path_for(target,'assembly',1,'main')))==expected
        page.get_by_role('button',name='Build & render assembly',exact=True).click();page.get_by_role('button',name='Review this exact assembly',exact=True).wait_for();decoded();review('Review this exact assembly','Assembly review recorded')
        page.get_by_role('button',name='Continue to rough cut',exact=True).click();page.get_by_role('button',name='Analyze and render rough cut',exact=True).click();page.get_by_role('button',name='Review this exact rough cut',exact=True).wait_for();decoded();review('Review this exact rough cut','Rough cut reviewed.')
        page.get_by_role('button',name='Continue to opening & closing',exact=True).click();page.get_by_label('Episode title',exact=True).fill('Independent repeated programme');page.get_by_label('Enable opening').uncheck();page.get_by_label('Enable closing').uncheck();page.get_by_role('button',name='Preview opening & closing',exact=True).click();review('Review this exact plan','Opening and closing reviewed.')
        page.get_by_role('button',name='Continue to final cut',exact=True).click();page.get_by_role('button',name='Render final programme',exact=True).click();page.get_by_role('button',name='Review this exact final cut',exact=True).wait_for();decoded();review('Review this exact final cut','Final cut reviewed.')
        page.get_by_role('button',name='Release',exact=True).click();page.get_by_label('Release description').fill('Synthetic independent programme; all reviews simulated.');page.get_by_role('button',name='Build release package',exact=True).click();page.get_by_text('Ready to download · final v1',exact=True).wait_for();decoded();page.screenshot(path=str(EVIDENCE/f'package-{width}.png'))
        preview=context.new_page();preview.goto(page.evaluate('location.origin')+page.get_by_role('link',name='Play package',exact=True).get_attribute('href'))
        for player in preview.locator('audio').all():player.evaluate('(el)=>el.play()');wait_for(lambda:player.evaluate('(el)=>el.currentTime>.1'),preview);player.evaluate('(el)=>el.pause()')
        preview.screenshot(path=str(EVIDENCE/f'package-playback-{width}.png'));preview.close()
        with R.resolve(studio,eid) as (target,row):
            final=R.json_file(editdoc.path_for(target,'final',1,'main'));assert [w['w'] for w in mapped_words(target,final)]==['Second.','First.','First.'];assert final['tracks']['music']
        assert originals=={p:sha256_file(Path(p)) for p in originals};assert not errors
        (EVIDENCE/f'journey-{width}.json').write_text(json.dumps({'simulated_native':True,'synthetic_source_words':True,'source_hashes':originals,'input_project':programme['id'],'input_sha256':programme['sha256'],'episode':eid,'jobs':server.app.workbench.overview()['jobs']},indent=2))
    except Exception:
        page.screenshot(path=str(EVIDENCE/f'failure-{width}.png'));(EVIDENCE/f'failure-{width}.txt').write_text(page.locator('body').inner_text());raise
    finally:context.close()
