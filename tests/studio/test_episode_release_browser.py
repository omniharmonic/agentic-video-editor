"""Portable GUI release journey; native checkpoints are explicitly simulated."""
from pathlib import Path
import tempfile
import pytest
from playwright.sync_api import expect
from .test_workbench_browser import browser,rig,wait_for
from .test_episode_release import finished
from .test_episode_workshop import FakeAuthority

EVIDENCE=Path('/private/tmp/ave-episode-release-ui');EVIDENCE.mkdir(exist_ok=True)

@pytest.mark.parametrize('width',[1440,390])
def test_audio_select_keep_package_gui(rig,browser,width,tmp_path,synthetic_final_asr):
    studio,server=rig;eid,original,fixture_app,final=finished(studio,tmp_path)
    server.app.native_authority=FakeAuthority()
    context=browser.new_context();page=context.new_page();page.set_viewport_size({'width':width,'height':1000 if width==1440 else 844});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    def confirm(token):
        server.app.episode_checkpoints.prepared[token]['viewed']-=3
        return server.app.episode_checkpoints.confirm_native(token,server.app.pin.reveal())
    page.expose_function('fixtureConfirmEpisode',confirm);page.add_init_script('window.aveNative={confirmEpisode:t=>window.fixtureConfirmEpisode(t)}')
    try:
        page.goto(server.url()+'#/workbench');page.get_by_role('button',name='Episodes',exact=True).click();page.get_by_role('button',name='Synthetic conversation',exact=True).click()
        page.get_by_role('button',name='Clips & shorts',exact=True).click()
        page.get_by_label('Selection title',exact=True).fill('Hello, complete thought')
        page.get_by_label('Selection start seconds',exact=True).fill('0');page.get_by_label('Selection end seconds',exact=True).fill('1.5')
        page.screenshot(path=str(EVIDENCE/f'clips-{width}.png'))
        page.get_by_role('button',name='Render selection for review',exact=True).click()
        page.get_by_text('Hello, complete thought · Ready to review',exact=True).wait_for(timeout=30000)
        page.get_by_label('Complete statement Hello, complete thought').check();page.get_by_label('Context reviewed Hello, complete thought').check()
        page.get_by_role('button',name='Play selection',exact=True).click();player=page.locator('.ew-player audio');player.evaluate('(el)=>el.play()');wait_for(lambda:player.evaluate('(el)=>el.currentTime>.1&&el.readyState>=2'),page)
        page.get_by_role('button',name='Review keep: Hello, complete thought',exact=True).click();page.get_by_role('button',name='Confirm in native review',exact=True).click()
        page.get_by_text('Hello, complete thought · Kept · final v1',exact=True).wait_for();page.screenshot(path=str(EVIDENCE/f'kept-{width}.png'))
        page.get_by_role('button',name='Release',exact=True).click();page.get_by_label('Release description',exact=True).fill('Synthetic audio release.')
        page.get_by_label('Include full-quality original recordings',exact=True).check();page.screenshot(path=str(EVIDENCE/f'package-controls-{width}.png'))
        page.get_by_role('button',name='Build release package',exact=True).click()
        page.get_by_text('Ready to download · final v1',exact=True).wait_for(timeout=45000)
        preview=page.get_by_role('link',name='Play package',exact=True);preview.wait_for();page.screenshot(path=str(EVIDENCE/f'package-ready-{width}.png'))
        response=page.request.get(page.evaluate('location.origin')+preview.get_attribute('href'));assert response.ok and '<audio controls' in response.text()
        package_page=page.context.new_page()
        try:
            package_page.goto(page.evaluate('location.origin')+preview.get_attribute('href'))
            package_audio=package_page.locator('audio').first;package_audio.evaluate('(el)=>el.play()')
            wait_for(lambda:package_audio.evaluate('(el)=>el.readyState>=2&&el.currentTime>.1'),package_page)
            policy=response.headers['content-security-policy']
            assert 'sandbox allow-same-origin' in policy and "media-src 'self'" not in policy and "script-src 'none'" in policy
            assert package_page.locator('a[download]').count()==0
            # A filename alone never receives the package policy, and hostile
            # ordinary HTML cannot execute against the workspace origin.
            ordinary=studio.sp.exports/'index.html';ordinary.parent.mkdir(exist_ok=True)
            ordinary.write_text('<script>window.hostileCaptionExecuted=true</script><p>&lt;script&gt;hostile caption&lt;/script&gt;</p>')
            ordinary_url='/files/'+studio.sp.rel(ordinary)
            ordinary_response=package_page.goto(page.evaluate('location.origin')+ordinary_url)
            assert 'allow-same-origin' not in ordinary_response.headers['content-security-policy']
            assert package_page.evaluate('window.hostileCaptionExecuted===undefined')
        finally:package_page.close()
        assert page.request.get(page.evaluate('location.origin')+page.get_by_role('link',name='Download release ZIP',exact=True).get_attribute('href')).ok
        expect(page.get_by_label('Include full-quality original recordings')).to_be_checked();expect(page.get_by_label('Release description')).to_have_value('Synthetic audio release.')
        expect(page.get_by_text('These choices match the selected package. Changes create a new package.',exact=True)).to_be_visible()
        page.get_by_label('Include full-quality original recordings').uncheck();expect(page.get_by_text('Unbuilt changes. The selected package still contains its saved settings.',exact=True)).to_be_visible()
        page.get_by_role('button',name='Close',exact=True).click();page.get_by_role('button',name='Episodes',exact=True).click();page.get_by_role('button',name='Synthetic conversation',exact=True).click();page.get_by_role('button',name='Release',exact=True).click()
        expect(page.get_by_label('Include full-quality original recordings')).to_be_checked();expect(page.get_by_label('Release description')).to_have_value('Synthetic audio release.')
        assert not errors
        assert page.locator('.episode-workshop').evaluate('(e)=>e.scrollWidth<=e.clientWidth+1')
    finally:context.close();fixture_app.close()


@pytest.mark.real_motion
@pytest.mark.parametrize('width',[1440,390])
def test_video_kept_portrait_and_package_controls(rig,browser,width,tmp_path,synthetic_final_asr):
    studio,server=rig;eid,original,fixture_app,final=finished(studio,tmp_path,video=True)
    server.app.native_authority=FakeAuthority()
    context=browser.new_context();page=context.new_page();page.set_viewport_size({'width':width,'height':1000 if width==1440 else 844});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    def confirm(token):
        server.app.episode_checkpoints.prepared[token]['viewed']-=3
        return server.app.episode_checkpoints.confirm_native(token,server.app.pin.reveal())
    page.expose_function('fixtureConfirmEpisode',confirm);page.add_init_script('window.aveNative={confirmEpisode:t=>window.fixtureConfirmEpisode(t)}')
    try:
        page.goto(server.url()+'#/workbench');page.get_by_role('button',name='Episodes',exact=True).click()
        page.get_by_role('button',name='Synthetic camera gap',exact=True).click()
        page.get_by_role('button',name='Clips & shorts',exact=True).click()
        page.get_by_label('Selection title',exact=True).fill('Two complete thoughts')
        page.get_by_role('button',name='Render selection for review',exact=True).click()
        page.get_by_text('Two complete thoughts · Ready to review',exact=True).wait_for(timeout=45000)
        page.get_by_label('Complete statement Two complete thoughts').check();page.get_by_label('Context reviewed Two complete thoughts').check()
        page.get_by_role('button',name='Review keep: Two complete thoughts',exact=True).click();page.get_by_role('button',name='Confirm in native review',exact=True).click()
        page.get_by_label('Short title Two complete thoughts').fill('A second perspective')
        kept_player=page.locator('.ew-player video');kept_player.evaluate('(el)=>el.play()');wait_for(lambda:kept_player.evaluate('(el)=>el.readyState>=2&&el.currentTime>.1'),page);kept_player.evaluate('(el)=>el.pause()')
        page.screenshot(path=str(EVIDENCE/f'video-kept-{width}.png'))
        page.get_by_role('button',name='Render short: Two complete thoughts',exact=True).click()
        play=page.get_by_role('button',name='Play short Two complete thoughts',exact=True)
        # Result title remains the original selection; the opening title is
        # captured separately in this rendered short's exact arguments.
        page.get_by_text('Short · Two complete thoughts',exact=True).click(timeout=90000)
        play.click();player=page.locator('.ew-player video');player.evaluate('(el)=>el.play()')
        wait_for(lambda:player.evaluate('(el)=>el.currentTime>.2&&el.readyState>=2&&el.videoWidth===1080&&el.videoHeight===1920'),page)
        player.evaluate('(el)=>{el.pause();el.currentTime=1.2;}');wait_for(lambda:player.evaluate('(el)=>!el.seeking&&el.readyState>=2&&Math.abs(el.currentTime-1.2)<.05'),page)
        page.screenshot(path=str(EVIDENCE/f'portrait-ready-{width}.png'))
        page.get_by_role('button',name='Release',exact=True).click();page.get_by_label('Release description').fill('Synthetic video release.')
        page.get_by_role('button',name='Build release package',exact=True).click()
        page.get_by_text('Ready to download · final v1',exact=True).wait_for(timeout=60000)
        page.screenshot(path=str(EVIDENCE/f'video-package-{width}.png'))
        package_page=page.context.new_page()
        try:
            package_page.goto(page.evaluate('location.origin')+page.get_by_role('link',name='Play package',exact=True).get_attribute('href'))
            package_video=package_page.locator('video').first;package_video.evaluate('(el)=>el.play()')
            wait_for(lambda:package_video.evaluate('(el)=>el.readyState>=2&&el.currentTime>.1&&el.videoWidth>0'),package_page)
        finally:package_page.close()
        assert not errors and page.locator('.episode-workshop').evaluate('(e)=>e.scrollWidth<=e.clientWidth+1')
    finally:context.close();fixture_app.close()


def test_delayed_submission_is_single_bound_retry_and_close_safe(rig,browser,tmp_path,synthetic_final_asr):
    studio,server=rig;eid,original,fixture_app,final=finished(studio,tmp_path)
    context=browser.new_context();page=context.new_page();pending=[];bodies=[];polled=[]
    def hold(route):pending.append(route);bodies.append(route.request.post_data_json)
    page.route('**/api/studio/workbench/episode-select',hold)
    page.on('request',lambda request:polled.append(request.url) if '/jobs/job_delayed_fixture' in request.url else None)
    try:
        page.goto(server.url()+'#/workbench');page.get_by_role('button',name='Episodes',exact=True).click();page.get_by_role('button',name='Synthetic conversation',exact=True).click()
        page.get_by_role('button',name='Clips & shorts',exact=True).click();page.get_by_label('Selection title',exact=True).fill('One captured request')
        submit=page.get_by_role('button',name='Render selection for review',exact=True)
        submit.evaluate('(button)=>{button.click();button.click();}')
        wait_for(lambda:len(pending)==1,page)
        expect(page.get_by_text('Checking the selected inputs…',exact=True)).to_be_visible()
        expect(page.get_by_role('button',name='Release',exact=True)).to_be_disabled()
        # Playback remains a native media control while protected inputs verify.
        assert page.locator('.ew-player audio').evaluate('(el)=>el.controls&&!el.hasAttribute("disabled")')
        page.wait_for_timeout(100);assert len(bodies)==1
        pending.pop().fulfill(status=409,content_type='application/json',body='{"error":"Delayed fixture input conflict"}')
        retry=page.get_by_role('button',name='Retry submission',exact=True);retry.wait_for()
        expect(page.get_by_role('button',name='Return to episode',exact=True)).to_be_enabled()
        expect(page.get_by_role('button',name='Release',exact=True)).to_be_enabled()
        retry.click();wait_for(lambda:len(pending)==1,page)
        assert bodies[0]==bodies[1] and bodies[0]['request_id']
        page.get_by_role('button',name='Close',exact=True).click()
        page.get_by_role('button',name='Episodes',exact=True).click();page.get_by_role('button',name='Synthetic conversation',exact=True).click()
        pending.pop().fulfill(status=200,content_type='application/json',body='{"id":"job_delayed_fixture","status":"queued"}')
        page.wait_for_timeout(100)
        assert not polled and page.locator('.episode-workshop').count()==1
        expect(page.get_by_role('button',name='Release',exact=True)).to_be_enabled()
        assert page.get_by_role('button',name='Cancel job',exact=True).count()==0
    finally:context.close();fixture_app.close()


def test_review_preflight_and_confirmation_bind_one_context(rig,browser,tmp_path):
    import json
    from .test_episode_workshop import ready,run
    studio,server=rig;eid,_,fixture_app=ready(studio,tmp_path);run(studio,eid,'episode-assembly')
    context=browser.new_context();page=context.new_page();pending=[];views=[]
    page.route('**/api/studio/episodes/prepare',lambda route:pending.append(route))
    def view(route):views.append(route.request.post_data_json);route.continue_()
    page.route('**/api/studio/episodes/view',view)
    page.add_init_script('window.fixtureTokens=[];window.aveNative={confirmEpisode:token=>{window.fixtureTokens.push(token);return new Promise((resolve,reject)=>{window.finishFixtureConfirm=resolve;window.rejectFixtureConfirm=reject;});}}')
    def open_episode():
        page.get_by_role('button',name='Episodes',exact=True).click();page.get_by_role('button',name='Synthetic conversation',exact=True).click()
    try:
        page.goto(server.url()+'#/workbench');open_episode()
        button=page.get_by_role('button',name='Review this exact assembly',exact=True);button.evaluate('(el)=>{el.click();el.click()}')
        wait_for(lambda:len(pending)==1,page);expect(page.get_by_role('button',name='Rough cut',exact=True)).to_be_disabled()
        assert page.locator('.ew-player audio').evaluate('(el)=>el.controls&&!el.hasAttribute("disabled")')
        pending.pop().fulfill(status=409,content_type='application/json',body=json.dumps({'error':'Synthetic preparation conflict'}))
        page.get_by_role('button',name='Retry preparing review',exact=True).click();wait_for(lambda:len(pending)==1,page)
        route=pending.pop();response=route.fetch();prepared=response.json();route.fulfill(response=response)
        confirm=page.get_by_role('button',name='Confirm in native review',exact=True);confirm.wait_for()
        assert views[-1]['token']==prepared['token']
        confirm.evaluate('(el)=>{el.click();el.click()}');wait_for(lambda:page.evaluate('window.fixtureTokens.length')==1,page)
        assert page.evaluate('window.fixtureTokens')==[prepared['token']]
        expect(page.get_by_role('button',name='Change proposal',exact=True)).to_be_disabled()
        page.evaluate('window.rejectFixtureConfirm(new Error("Synthetic native cancellation"))')
        expect(confirm).to_be_enabled();confirm.click();wait_for(lambda:page.evaluate('window.fixtureTokens.length')==2,page)
        assert page.evaluate('window.fixtureTokens')==[prepared['token'],prepared['token']]
        page.get_by_role('button',name='Close',exact=True).click();open_episode()
        page.evaluate('window.finishFixtureConfirm({checkpoint:"synthetic"})');page.wait_for_timeout(100)
        expect(page.get_by_role('button',name='Review this exact assembly',exact=True)).to_be_enabled()
        # A response to a closed prepare must not even mark the old token viewed.
        page.get_by_role('button',name='Review this exact assembly',exact=True).click();wait_for(lambda:len(pending)==1,page)
        before=len(views);page.get_by_role('button',name='Close',exact=True).click();open_episode()
        pending.pop().fulfill(status=200,content_type='application/json',body=json.dumps(prepared));page.wait_for_timeout(100)
        assert len(views)==before and page.locator('.episode-workshop').count()==1
        expect(page.get_by_role('button',name='Review this exact assembly',exact=True)).to_be_enabled()
    finally:context.close();fixture_app.close()


def test_selected_assembly_warnings_are_visible_and_replaced(rig,browser,tmp_path):
    import copy,json
    from .test_episode_workshop import ready,run
    studio,server=rig;eid,_,fixture_app=ready(studio,tmp_path);run(studio,eid,'episode-assembly')
    context=browser.new_context();page=context.new_page()
    def projection(route):
        response=route.fetch();value=response.json();first=value['assemblies'][0]
        first['render']['details']['report']['warnings']=['Synthetic v1 loudness warning']
        second=copy.deepcopy(first);second['version']=2;second['render']['details']['report']['warnings']=['Synthetic v2 peak warning'];value['assemblies'].append(second)
        route.fulfill(response=response,body=json.dumps(value))
    page.route('**/api/studio/episodes/'+eid+'/view?*',projection)
    try:
        page.goto(server.url()+'#/workbench');page.get_by_role('button',name='Episodes',exact=True).click();page.get_by_role('button',name='Synthetic conversation',exact=True).click()
        selector=page.get_by_label('Assembly version',exact=True);selector.select_option('1')
        warnings=page.locator('.ew-next .ew-review-warning[data-render-version]')
        expect(warnings).to_have_count(1);expect(warnings).to_have_text('Synthetic v1 loudness warning');expect(warnings).to_be_visible()
        selector.select_option('2');expect(warnings).to_have_count(1);expect(warnings).to_have_text('Synthetic v2 peak warning')
        selector.select_option('1');expect(warnings).to_have_count(1);expect(warnings).to_have_text('Synthetic v1 loudness warning')
    finally:context.close();fixture_app.close()


def test_coverage_rounding_and_short_omission_presentation(rig,browser,tmp_path):
    import copy,json
    from .test_episode_workshop import ready,run
    from .test_episode_coverage_precision import coverage
    studio,server=rig;eid,_,fixture_app=ready(studio,tmp_path);run(studio,eid,'episode-assembly')
    context=browser.new_context();page=context.new_page()
    def projection(route):
        response=route.fetch();value=response.json();first=value['assemblies'][0];first['coverage']=coverage();first['render']['details']['report']['warnings']=[]
        second=copy.deepcopy(first);second['version']=2;second['coverage']=coverage(True);value['assemblies'].append(second)
        route.fulfill(response=response,body=json.dumps(value))
    page.route('**/api/studio/episodes/'+eid+'/view?*',projection)
    try:
        page.goto(server.url()+'#/workbench');page.get_by_role('button',name='Episodes',exact=True).click();page.get_by_role('button',name='Synthetic conversation',exact=True).click()
        selector=page.get_by_label('Assembly version',exact=True);selector.select_option('1')
        expect(page.get_by_text('Retained within 1 ms boundary precision.',exact=True)).to_be_visible()
        assert page.locator('.ew-next .ew-review-warning').count()==0
        page.get_by_text('Boundary rounding details',exact=True).click();expect(page.locator('.ew-boundary-rounding pre')).to_contain_text('0.0004')
        selector.select_option('2');expect(page.locator('.ew-next .ew-review-warning')).to_have_text('20 ms omitted inside confirmed bounds')
        expect(page.get_by_text('20 ms inside the confirmed bounds is omitted.',exact=True)).to_be_visible()
        expect(page.get_by_text('Omitted episode 0.020000–0.040000 s · 20 ms · Not retained by the usable camera/source coverage plan.',exact=True)).to_be_visible()
    finally:context.close();fixture_app.close()


def test_cancel_projects_stopping_until_terminal_job_and_bound_retry(rig,browser,tmp_path,synthetic_final_asr):
    studio,server=rig;eid,_,fixture_app,final=finished(studio,tmp_path)
    page=browser.new_page();cancels=[];terminal=False;retried=[]
    page.route('**/api/studio/workbench/episode-select',lambda route:route.fulfill(json={'id':'job_stopping_fixture','status':'queued'}))
    page.route('**/api/studio/workbench/jobs/job_stopping_fixture',lambda route:route.fulfill(json={'id':'job_stopping_fixture','status':'cancelled' if terminal else 'running','phase':'Verifying source and review dependencies','diagnostics':'Cancelled; completed output preserved'}))
    page.route('**/api/studio/workbench/cancel-media',lambda route:(cancels.append(route.request.post_data_json),route.fulfill(json={'cancel_requested':True})))
    page.route('**/api/studio/workbench/retry',lambda route:(retried.append(route.request.post_data_json),route.fulfill(status=409,json={'error':'Busy; the previous job is stopping. Retry shortly.'})))
    try:
        page.goto(server.url()+'#/workbench');page.get_by_role('button',name='Episodes',exact=True).click();page.get_by_role('button',name='Synthetic conversation',exact=True).click();page.get_by_role('button',name='Clips & shorts',exact=True).click()
        page.get_by_role('button',name='Render selection for review',exact=True).click();cancel=page.get_by_role('button',name='Cancel job',exact=True);cancel.click()
        expect(cancel).to_be_disabled();expect(page.get_by_text('Stopping the job. Completed outputs are preserved; retry becomes available after it stops.',exact=True)).to_be_visible()
        assert cancels==[{'job_id':'job_stopping_fixture'}]
        assert page.locator('.ew-player audio').evaluate('(el)=>el.controls')
        terminal=True;retry=page.get_by_role('button',name='Retry this job',exact=True);retry.wait_for();retry.click()
        wait_for(lambda:bool(retried),page);assert retried==[{'job_id':'job_stopping_fixture'}]
        expect(page.get_by_text('Busy; the previous job is stopping. Retry shortly.',exact=True)).to_be_visible();expect(retry).to_be_enabled()
    finally:page.close();fixture_app.close()


@pytest.mark.parametrize('after_reload',['stay','catalog','close'])
def test_committed_render_projection_failure_recovers_without_replaying_job(rig,browser,tmp_path,synthetic_final_asr,after_reload):
    studio,server=rig;eid,_,app,_=finished(studio,tmp_path);context=browser.new_context();page=context.new_page()
    try:
        page.goto(server.url()+'#/workbench');page.get_by_role('button',name='Episodes',exact=True).click();page.get_by_role('button',name='Synthetic conversation',exact=True).click();page.get_by_role('button',name='Revise sound & captions',exact=True).click();page.get_by_label('Dialogue gain dB').fill('-1')
        pending=[];deferred=[]
        def fail_once(route):
            if not pending:
                pending.append(route.fetch().json());route.fulfill(status=500,content_type='application/json',body='{"error":"Injected saved-view failure"}')
            elif after_reload!='stay':deferred.append(route)
            else:route.continue_()
        page.route('**/view?session=main',fail_once)
        page.get_by_role('button',name='Render final programme',exact=True).click();expect(page.locator('.mw-status')).to_have_text('Completed output saved · view could not refresh',timeout=30000)
        jobs=server.app.workbench.overview()['jobs'];completed=next(j for j in jobs if j['operation']=='episode-final');assert completed['status']=='done'
        expect(page.get_by_role('button',name='Cancel job',exact=True)).to_have_count(0);expect(page.get_by_label('Dialogue gain dB')).to_have_value('-1');page.get_by_role('button',name='Reload completed result',exact=True).click()
        if after_reload=='stay':expect(page.get_by_role('button',name='Review this exact final cut',exact=True)).to_be_visible()
        else:
            import json
            wait_for(lambda:bool(deferred),page);page.get_by_role('button',name='Close' if after_reload=='close' else 'Return to episodes',exact=True).click();deferred.pop().fulfill(status=200,content_type='application/json',body=json.dumps(pending[0]));page.wait_for_timeout(100)
            if after_reload=='close':expect(page.locator('.episode-workshop')).to_have_count(0)
            else:expect(page.get_by_role('button',name='Synthetic conversation',exact=True)).to_be_visible();expect(page.get_by_role('button',name='Review this exact final cut',exact=True)).to_have_count(0)
        assert {j['id'] for j in server.app.workbench.overview()['jobs']}=={j['id'] for j in jobs}
        assert server.app.workbench.tail(completed['id'])['result']==completed['result']
    finally:context.close();app.close()
