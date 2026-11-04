"""Actual manual delivery GUI with synthetic files and injected fixture-only authority."""
from datetime import timedelta
from pathlib import Path
import pytest
from .test_production_ui import browser,rig
from ave.studio import store,manual_export,manual_publication
from ave.studio.events import AGENT
from ave.publish import queue as pq
from ave.studio.dispatch import approval_bind
from ave.studio.sync import sync_queue

def prepare_reviewed(rig, *, method="manual", publishers=None):
    studio,server=rig
    p=next(p for p in store.list_posts(studio.sp) if len(p['media'])==2 and p['media'][0]['role']=='image')
    v=p['variants'][0];v.update(delivery={'method':method},payload_version=3)
    p=store.save_post(studio.sp,p,expected_rev=p['rev'],actor=AGENT,op='fixture')
    from ave.library import registry
    from ave.library.ingest import default_own_entry
    for aid in {m['asset'] for m in p['media']}:
        a=store.read_assets(studio.sp)[aid]
        registry.license_log(studio.sp).append({**default_own_entry('fixture','2026-10-29T00:00:00Z'),'sha256':a['sha256'],'file':a['file'],'bytes':a['bytes'],'approved_by':'fixture'})
    sync_queue(studio.sp,p['id'],publishers=publishers);p=store.load_post(studio.sp,p['id']);v=p['variants'][0]
    item=pq.load(studio.sp,v['queue']['id'])
    item=pq.approve(studio.sp,item['id'],'Fixture reviewer',item['sha256'][:12],bind=approval_bind(studio.sp,v),publishers=publishers)
    # Export now must be within fixture approval time; use actual UTC consistently.
    from datetime import datetime,timezone
    studio.clock.set(datetime.now(timezone.utc).isoformat())
    return p,item


@pytest.mark.legacy_ui
@pytest.mark.parametrize('width,height',[(1440,960),(1024,768),(390,844)])
def test_manual_history_form_and_report(rig,browser,width,height):
    studio,server=rig
    p,item=prepare_reviewed(rig)
    ctx=browser.new_context(viewport={'width':width,'height':height},timezone_id='UTC',permissions=['clipboard-read','clipboard-write'])
    page=ctx.new_page();page.set_default_timeout(30000);errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    out=Path('/private/tmp/ave-delivery-evidence');out.mkdir(exist_ok=True)
    def record(request):
        result=manual_publication._record(studio.sp,**request,now=studio.now()+timedelta(minutes=3))
        return result
    page.expose_function('fixtureRecordPublication',record)
    page.add_init_script('globalThis.aveNative={recordManualPublication:r=>globalThis.fixtureRecordPublication(r)}')
    try:
        page.goto(server.url()+f'#/post/{p["id"]}');page.get_by_role('button',name='Export originals',exact=True).click()
        page.get_by_role('button',name='Export reviewed post',exact=True).click();page.get_by_text('Package prepared.',exact=False).wait_for()
        card=page.get_by_role('region',name='Package history').locator('.manual-package')
        with page.expect_download() as download:card.get_by_role('link',name='Download full-resolution package').click()
        path=download.value.path();assert Path(path).stat().st_size>10000
        assert pq.load(studio.sp,item['id'])['status']=='approved'
        card.get_by_role('button',name='Copy caption',exact=True).click()
        assert page.evaluate('navigator.clipboard.readText()')==item['caption']
        # Copy status must preserve package actions and history.
        history=page.get_by_role('region',name='Package history',exact=True).locator('.manual-package')
        history.get_by_role('button',name='I posted this').click()
        page.get_by_label('Published URL',exact=True).fill('https://www.instagram.com/p/synthetic')
        at=(studio.now()+timedelta(minutes=1)).strftime('%Y-%m-%dT%H:%M')
        page.get_by_label('Publication time',exact=True).fill(at)
        page.get_by_label('Reported by',exact=True).fill('Sofia')
        page.get_by_label('Changes made in the platform app',exact=True).fill('Added native music and a location')
        page.screenshot(path=str(out/f'manual-form-{width}.png'))
        assert page.locator('body').evaluate('(e)=>e.scrollWidth<=innerWidth')
        page.get_by_role('button',name='Record publication',exact=True).click()
        page.locator('.manual-history').get_by_text('Published · human reported',exact=True).wait_for()
        page.screenshot(path=str(out/f'manual-reported-{width}.png'))
        result=store.load_post(studio.sp,p['id'])['variants'][0]['result'];assert result['by']=='Sofia' and result['changed_in_native']
        assert not errors,errors
    finally:ctx.close()


@pytest.mark.legacy_ui
def test_browser_shows_native_dependency_and_cannot_report(rig,browser):
    studio,server=rig
    p=next(p for p in store.list_posts(studio.sp) if len(p['media'])==2 and p['media'][0]['role']=='image');v=p['variants'][0]
    page=browser.new_page()
    try:
        page.goto(server.url()+f'#/post/{p["id"]}');page.get_by_role('button',name='Export originals',exact=True).click()
        page.get_by_role('button',name='Download unreviewed draft',exact=True).click();page.get_by_text('Package prepared.',exact=False).wait_for()
        assert page.get_by_role('button',name='I posted this',exact=True).count()==0
        response=page.request.post(server.url()+'api/studio/posts/'+p['id']+'/record-manual-publication',data={'human':True,'pin':'424242'})
        assert response.status>=400
    finally:page.close()


@pytest.mark.legacy_ui
def test_reviewed_form_without_native_bridge_is_honest(rig,browser):
    studio,server=rig;p,item=prepare_reviewed(rig)
    manual_export.build(studio.sp,p['id'],p['variants'][0]['id'],expected_rev=p['rev'],now=studio.now())
    page=browser.new_page(viewport={'width':390,'height':844})
    try:
        page.goto(server.url()+f'#/post/{p["id"]}')
        page.get_by_role('button',name='Export originals',exact=True).click()
        page.get_by_role('button',name='I posted this',exact=True).click()
        assert page.get_by_text('Protected recording requires the native desktop connection.',exact=False).is_visible()
        assert page.get_by_role('button',name='Record publication',exact=True).is_disabled()
        page.screenshot(path='/private/tmp/ave-delivery-evidence/native-dependency-390.png')
        assert pq.load(studio.sp,item['id'])['status']=='approved'
        pack=manual_export.history(studio.sp,p['id'])[0]
        hidden=studio.sp.ledger_dir/'manual_reviews'/(pack['package_id']+'.json')
        assert page.request.get(server.url()+'files/'+studio.sp.rel(hidden)).status>=400
        alias=studio.sp.work/'review-alias.json';alias.symlink_to(hidden)
        assert page.request.get(server.url()+'files/'+studio.sp.rel(alias)).status>=400
    finally:page.close()


@pytest.mark.legacy_ui
def test_real_pdf_import_and_delivery_capability_details(rig,browser,tmp_path):
    from PIL import Image
    from ave.studio import model
    studio,server=rig
    p=model.new_post(idea='A PDF handoff',copy={'base':'Read the original document'})
    v=model.new_variant(account='author',platform='instagram',post_type='feed');v.update(payload_version=3,delivery={'method':'manual'});p['variants']=[v]
    p=store.save_post(studio.sp,p,expected_rev=None,actor=AGENT,op='fixture')
    pdf=tmp_path/'original.pdf';Image.new('RGB',(612,792),'#b9c8bc').save(pdf,'PDF')
    page=browser.new_page(viewport={'width':1024,'height':768});native_import(page,studio,pdf)
    try:
        page.goto(server.url()+f'#/post/{p["id"]}')
        page.get_by_role('button',name='Import media',exact=True).click()
        page.locator('.composer-stage').get_by_text('PDF document',exact=True).wait_for()
        assert store.load_post(studio.sp,p['id'])['variants'][0]['media'][0]['role']=='document'
        page.get_by_text('Delivery capabilities and readiness',exact=True).click()
        page.get_by_text('Required access:',exact=False).wait_for()
        assert page.get_by_text('Live verification: not certified.',exact=False).is_visible()
        page.screenshot(path='/private/tmp/ave-delivery-evidence/pdf-capabilities-1024.png')
        page.get_by_role('button',name='Export originals',exact=True).click()
        page.get_by_role('button',name='Download unreviewed draft',exact=True).click()
        page.get_by_text('Package prepared.',exact=False).wait_for()
        packs=manual_export.history(studio.sp,p['id'])
        assert packs and packs[0]['media'][0]['sha256']==__import__('hashlib').sha256(pdf.read_bytes()).hexdigest()
    finally:page.close()


@pytest.mark.legacy_ui
@pytest.mark.parametrize('phase,source,adjunct,width',[
    ('published','provider_response','uncertain',1440),('published','provider_response','uncertain',390),
    ('published','provider_response','failed',1024),('published','provider_response','published',390),
    ('processing','provider_response',None,1024),('awaiting_user_publish','provider_response',None,390),
    ('sandbox','provider_response',None,1024),('draft','provider_response',None,390),
    ('published','human_report',None,1024),('failed','provider_status',None,390)])
def test_visible_receipt_outcomes_editor_details_and_history(rig,browser,phase,source,adjunct,width):
    from ave.publish import receipts
    studio,server=rig;p,item=prepare_reviewed(rig);v=p['variants'][0]
    v['delivery']={'method':'automatic'};item['delivery']={'method':'automatic'}
    r=receipts.receipt(provider='instagram',account=v['account'],phase=phase,object_id='fixture-operation',
        publication_id='fixture-post' if phase=='published' and source!='human_report' else None,
        url='https://www.instagram.com/p/fixture' if phase=='published' else None,
        source=source,certainty='reported' if source=='human_report' else 'confirmed',
        adjuncts={'first_comment':{'state':adjunct,'key':'fixture:first_comment'}} if adjunct else {})
    result=receipts.normalize({'receipt':r},item,'instagram');item['results']={'instagram':result};item['status']=receipts.aggregate(item)
    if phase=='failed':item['delivery_review_required']=True
    pq.save(studio.sp,item);v['result']=result
    p=store.save_post(studio.sp,p,expected_rev=p['rev'],actor=AGENT,op='fixture.delivery',detail={'receipt':r})
    ctx=browser.new_context(viewport={'width':width,'height':960 if width>1000 else 844})
    page=ctx.new_page();page.set_default_timeout(30000);errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    evidence=Path('/private/tmp/ave-delivery-evidence/round1');evidence.mkdir(exist_ok=True)
    try:
        page.goto(server.url()+f'#/post/{p["id"]}')
        card=page.locator('.composer-delivery .delivery-receipt');card.wait_for()
        expected={'published':'reported by a person' if source=='human_report' else 'Primary post published','processing':'Processing at provider','awaiting_user_publish':'Awaiting your publication','sandbox':'Sandbox only','draft':'Provider draft','failed':'Fresh human review and approval'}[phase]
        assert expected in card.inner_text()
        assert page.get_by_role('button',name='Refresh provider status').is_enabled()
        if adjunct:
            assert f'First comment: {adjunct}.' in card.inner_text()
            assert card.get_by_role('link',name='Inspect on platform').get_attribute('href')=='https://www.instagram.com/p/fixture'
            if adjunct in ('failed','uncertain'):assert 'primary post will not be sent again' in card.inner_text()
        page.locator('.ed-history summary').click();assert expected in page.locator('.ed-history').inner_text()
        card.scroll_into_view_if_needed();page.screenshot(path=str(evidence/f'editor-{phase}-{adjunct or source}-{width}.png'))
        assert page.locator('body').evaluate('(e)=>e.scrollWidth<=innerWidth')
        # (the old calendar peek is retired; the Workspace post sheet shows the same receipt: test_workspace_browser)
        assert pq.load(studio.sp,item['id'])['results']['instagram']==result
        assert not errors
    finally:ctx.close()


def pending_operation(rig):
    from ave.publish import receipts
    from ave.studio import dispatch
    from studio_helpers import RecordingPublisher
    studio,server=rig
    class StatusOnly(RecordingPublisher):
        def recover(self,item,record):return receipts.result('instagram','accepted-fixture',phase='failed',error='Provider rejected operation')
        def publish(self,*args):raise AssertionError('Status refresh must never create')
    a=StatusOnly('instagram')
    p,item=prepare_reviewed(rig,method='automatic',publishers={'instagram':a});v=p['variants'][0]
    studio.publishers[v['account']]=a
    assert item['approvals'] and item['delivery']['method']=='automatic'
    from ave.publish import safety
    assert not safety.approval_problems(item,item['sha256'])
    assert item['approvals'][-1]['bind']['account']==v['account']
    assert item['approvals'][-1]['bind']['slot']==approval_bind(studio.sp,v)['slot']
    result=receipts.normalize(receipts.result('instagram','accepted-fixture',phase='processing'),item,'instagram')
    result['approval_identity']=dispatch.approval_identity(dispatch.approval_sig(item))
    item['results']={'instagram':result};item['status']='publishing';pq.save(studio.sp,item);v['result']=result
    p=store.save_post(studio.sp,p,expected_rev=p['rev'],actor=AGENT,op='fixture.pending')
    return p,item,a


@pytest.mark.legacy_ui
def test_pending_status_refresh_button_settles_failure_without_send(rig,browser):
    from ave.studio import dispatch,seal
    studio,server=rig;p,item,a=pending_operation(rig);v=p['variants'][0]
    ctx=browser.new_context(viewport={'width':390,'height':844});page=ctx.new_page();page.set_default_timeout(30000)
    errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        page.goto(server.url()+f'#/post/{p["id"]}')
        with page.expect_response(lambda r:r.url.endswith('/delivery-refresh')) as response:
            page.get_by_role('button',name='Refresh provider status',exact=True).click()
        assert response.value.status==200, response.value.text()
        page.locator('.composer-delivery').get_by_text('Fresh human review and approval',exact=False).wait_for()
        q=pq.load(studio.sp,item['id']);assert q['status']=='failed' and q['delivery_review_required']
        assert store.load_post(studio.sp,p['id'])['variants'][0]['result']['receipt']['phase']=='failed'
        assert a.publish_calls==0
        pq.refresh(studio.sp,item['id'],publishers={'instagram':a})
        rows=[r for r in seal.log(studio.sp,'dispatch').rows() if r.get('op')=='review_required' and r.get('approval')==dispatch.approval_sig(item)]
        assert len(rows)==1
        assert dispatch.terminal_review(studio.sp,q)
        assert page.locator('.ed textarea').first.is_enabled()
        page.get_by_role('button',name='Prepare review',exact=True).click()
        page.get_by_text('Prepared for human review',exact=True).wait_for()
        assert pq.load(studio.sp,item['id'])['status']=='awaiting_approval' and a.publish_calls==0
        assert not errors, errors
    finally:ctx.close()


@pytest.mark.parametrize('invalid',['missing','mismatched'])
def test_pending_status_refresh_refuses_missing_or_mismatched_producing_approval(rig,invalid):
    studio,server=rig;p,item,a=pending_operation(rig)
    if invalid=='missing':item['approvals']=[]
    else:item['results']['instagram']['approval_identity']='0'*64
    pq.save(studio.sp,item)
    before=pq.load(studio.sp,item['id'])
    with pytest.raises(ValueError,match='no approval identity|another approval'):
        pq.refresh(studio.sp,item['id'],publishers={'instagram':a})
    assert pq.load(studio.sp,item['id'])==before and a.publish_calls==0


def native_import(page, studio, path):
    """The app's native import (aveNative.importLibrary), stood in for by the studio's own upload: a web file input no longer exists."""
    def imp():
        res = studio.upload(Path(path).name, Path(path), by="maren")
        res = res if isinstance(res, dict) and "asset" in res else {"asset": res, "duplicate": False}
        return {"added": [{**res, "name": Path(path).name}]}
    page.expose_function("__aveImport", imp)
    page.add_init_script("window.aveNative = Object.assign(window.aveNative || {}, { importLibrary: () => window.__aveImport() });")
