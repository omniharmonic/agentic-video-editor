"""Mutable fictional authoring; no credentials or release authority."""
import pytest
from ave.studio import composer, authoring, store
from ave.studio.events import AGENT
from ave.publish import safety
from tests.studio.test_production_ui import build, browser, rig


def test_profile_and_manual_variant(tmp_path):
    studio=build(tmp_path)
    profile=authoring.profile_save(studio,{'platform':'instagram','profile':{'display_name':'Sofia Notes','handle':'sofia.notes','bio':'Writing slowly','url':'https://example.invalid'}},AGENT)
    post=studio.create_post(copy={'base':'A new passage'})
    post=composer.add_variant(studio,post['id'],{'expected_rev':post['rev'],'account':profile['id'],'post_type':'carousel'},AGENT)
    assert post['variants'][0]['delivery']=={'method':'manual'}
    assets=list(store.read_assets(studio.sp).values());a=next(a for a in assets if a['kind']=='image')
    for _ in range(2):post=composer.media_action(studio,post['id'],{'expected_rev':post['rev'],'variant':post['variants'][0]['id'],'op':'add','asset':a['id']},AGENT)
    v=post['variants'][0];p=composer.projection(post,v,store.read_assets(studio.sp));assert len({m['id'] for m in p['content']})==2
    post=composer.media_action(studio,post['id'],{'expected_rev':post['rev'],'variant':v['id'],'op':'cover','asset':a['id']},AGENT)
    p=composer.projection(post,post['variants'][0],store.read_assets(studio.sp));assert len(p['content'])==2 and p['cover']


def test_bound_options_preserve_legacy_hashes():
    old={'meta_v':2,'caption':'Words'}
    assert safety.meta_hash(old)==safety.meta_hash({**old,'cover':{'sha256':'x'},'delivery':{'method':'manual'}})
    new={**old,'meta_v':3,'delivery':{'method':'manual'}}
    assert safety.meta_hash(new)!=safety.meta_hash({**new,'delivery':{'method':'automatic'}})


def test_composer_first_capture(rig,browser):
    from pathlib import Path
    studio,server=rig;page=browser.new_page(viewport={'width':1440,'height':960});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    out=Path('/private/tmp/ave-composer-evidence');out.mkdir(exist_ok=True)
    try:
        page.goto(server.url()+'#/grid/author');page.locator('.gcell').first.wait_for();page.locator('.g-link').first.click();page.locator('.composer-stage').wait_for()
        page.screenshot(path=str(out/'first-composer-1440.png'),full_page=True)
        assert not errors,errors
    finally:page.close()

@pytest.mark.parametrize('width,height',[(1440,960),(1024,768),(390,844)])
def test_real_carousel_and_video_screen(rig,browser,width,height):
    from pathlib import Path
    studio,server=rig;context=browser.new_context(viewport={'width':width,'height':height},is_mobile=width==390,has_touch=width==390);page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    out=Path('/private/tmp/ave-composer-evidence');out.mkdir(exist_ok=True)
    carousel=next(p for p in store.list_posts(studio.sp) if len(p['media'])==2 and p['media'][0]['role']=='image')
    video=next(p for p in store.list_posts(studio.sp) if any(m['role']=='video' for m in p['media']))
    try:
        page.goto(server.url()+'#/grid/author?surface=all');page.locator(f'.gcell[data-post="{carousel["id"]}"] .g-link').click();page.locator('.composer-stage img').wait_for()
        page.wait_for_function('()=>[...document.querySelectorAll(".composer-stage img, .composer-slide img")].every(i=>i.complete&&i.naturalWidth>0)')
        assert page.get_by_label('Content slides').locator('.composer-slide').count()==2
        assert page.locator('body').evaluate('(el)=>el.scrollWidth<=innerWidth')
        page.screenshot(path=str(out/f'carousel-{width}.png'))
        page.get_by_role('button',name='Close composer',exact=True).click();page.locator(f'.gcell[data-post="{video["id"]}"] .g-link').click();player=page.locator('.composer-stage video');player.wait_for();player.evaluate('(v)=>v.play()');page.wait_for_function('()=>document.querySelector(".composer-stage video").currentTime>0.1');player.evaluate('(v)=>v.pause()')
        assert page.get_by_label('Content slides').locator('.composer-slide').count()==1
        page.screenshot(path=str(out/f'video-{width}.png'))
        assert not errors,errors
    finally:context.close()


def test_manual_originals_cover_binding_and_refusal(tmp_path,monkeypatch):
    import zipfile
    from PIL import Image
    from ave.studio.project import StudioProject
    from ave.studio.service import open_studio
    from ave.studio.sync import sync_queue,queue_diff
    from ave.studio.manual_export import build as pack
    from ave.publish import queue
    from studio_helpers import add_asset
    sp=StudioProject(tmp_path/'manual');sp.create(title='Manual only');studio=open_studio(sp.root)
    profile=authoring.profile_save(studio,{'platform':'instagram','profile':{'display_name':'Sofia'}},AGENT)
    path=tmp_path/'original.png';Image.new('RGB',(2160,2700),'seagreen').save(path)
    asset=add_asset(sp,path,alt='Original painting')
    from ave.library import registry
    from ave.library.ingest import default_own_entry
    registry.license_log(sp).append({**default_own_entry('test','2026-10-01T00:00:00'),'sha256':asset['sha256'],'file':asset['file'],'bytes':asset['bytes'],'approved_by':'test'})
    post=studio.create_post(copy={'base':'Original PNG'},media=[{'slot':0,'asset':asset['id'],'role':'image','alt':'A painting'}])
    post=composer.add_variant(studio,post['id'],{'expected_rev':post['rev'],'account':profile['id'],'post_type':'feed'},AGENT)
    v=post['variants'][0]
    post=composer.media_action(studio,post['id'],{'expected_rev':post['rev'],'variant':v['id'],'op':'cover','asset':asset['id']},AGENT)
    class NoProvider:
        def preflight(self,*a):raise AssertionError('Manual called provider')
        def ready_problems(self):raise AssertionError('Manual checked credentials')
    monkeypatch.setattr(queue,'get_publisher',lambda _:NoProvider())
    result=sync_queue(sp,post['id']);assert not result['problems'],result
    post=store.load_post(sp,post['id']);v=post['variants'][0];item=queue.load(sp,v['queue']['id'])
    assert item['meta_v']==3 and item['cover']['sha256']==asset['sha256']
    assert queue.resolve_files(sp,item)==[sp.root/asset['file']]
    assert queue_diff(sp,post,v,item)==[]
    for send in (False,True):assert 'Manual delivery' in queue.run(sp,item['id'],send=send)['blocked'][0]
    # Connection configuration is independent of per-variant delivery (synthetic fixture only).
    import yaml
    config_path=sp.accounts_dir/(profile['id']+'.yaml');cfg=yaml.safe_load(config_path.read_text());cfg['mode']='live';config_path.write_text(yaml.safe_dump(cfg))
    from ave.studio.dispatch import blockers
    assert [b.code for b in blockers(sp,post,v,now=studio.now(),send=True)]==['manual_delivery']
    assert 'Manual delivery' in queue.run(sp,item['id'],send=True)['blocked'][0]
    out=pack(sp,post['id'],draft=True,expected_rev=post['rev']);assert len(out['manifest']['media'])==1 and out['manifest']['cover']
    with zipfile.ZipFile(out['zip']) as z:
        assert z.read(out['manifest']['media'][0]['filename'])==path.read_bytes()
        assert z.read(out['manifest']['cover']['filename'])==path.read_bytes()
    from pathlib import Path
    evidence=Path('/private/tmp/ave-composer-evidence');evidence.mkdir(exist_ok=True)
    (evidence/'original-png-with-cover.zip').write_bytes(Path(out['zip']).read_bytes())
    assert not list((sp.work/'deliverables').glob('*'))
    baseline=safety.meta_hash(item);post=studio.edit_post(post['id'],{f"variants.{v['id']}.delivery":{'method':'automatic'}},expected_rev=post['rev'])
    sync_queue(sp,post['id']);item=queue.load(sp,v['queue']['id']);assert safety.meta_hash(item)!=baseline
    assert any('cover delivery' in p for p in item['preflight_problems'])


@pytest.mark.parametrize('value',[float('nan'),float('inf'),-1,1.1])
def test_focal_rejects_invalid_values(tmp_path,value):
    studio=build(tmp_path);post=next(p for p in store.list_posts(studio.sp) if p['media'] and p['production']['status']!='ready' and not any(v.get('result') for v in p['variants']));v=post['variants'][0] if post['variants'] else None
    before=store.load_post(studio.sp,post['id'])
    with pytest.raises(ValueError,match='focal'):
        composer.media_action(studio,post['id'],{'expected_rev':post['rev'],'variant':v['id'] if v else None,'op':'crop','item':'legacy_0','crop':{'v':1,'x':value,'y':.5,'aspect':'3:4','purpose':'preview'}},AGENT)
    assert store.load_post(studio.sp,post['id'])==before


def test_actual_frame_extraction_and_captured_revision(tmp_path):
    from ave.studio.workbench import execute_operation
    from ave.studio.events import Actor
    studio=build(tmp_path);post=next(p for p in store.list_posts(studio.sp) if any(m['role']=='video' for m in p['media']));v=post['variants'][0];video=next(m for m in post['media'] if m['role']=='video')
    args={'post':post['id'],'variant':v['id'],'asset':video['asset'],'seconds':.2,'expected_rev':post['rev']}
    job={'id':'job_'+'1'*24,'args':args,'operation':'frame','by':'Test author'}
    result=execute_operation(studio,job)
    assert not result['attachment_conflict'];p=store.load_post(studio.sp,post['id']);cover=composer.projection(p,p['variants'][0],store.read_assets(studio.sp))['cover'];assert cover
    asset=store.read_assets(studio.sp)[cover['asset']];assert asset['derived_from']==[video['asset']]
    from PIL import Image
    with Image.open(studio.sp.root/asset['file']) as im:im.load();assert im.width>0
    from pathlib import Path
    evidence=Path('/private/tmp/ave-composer-evidence');evidence.mkdir(exist_ok=True)
    (evidence/'frame-derived.png').write_bytes((studio.sp.root/asset['file']).read_bytes())
    source=store.read_assets(studio.sp)[video['asset']]
    (evidence/'source-video.mp4').write_bytes((studio.sp.root/source['file']).read_bytes())
    result=execute_operation(studio,{**job,'id':'job_'+'2'*24});assert result['attachment_conflict']
    assert store.load_post(studio.sp,post['id'])==p


def test_fresh_profile_import_caption_group_and_reload(rig,browser,tmp_path):
    from PIL import Image
    from pathlib import Path
    studio,server=rig;page=browser.new_page(viewport={'width':390,'height':844});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    original=tmp_path/'finished.png';Image.new('RGB',(1200,1500),'#33635c').save(original)
    try:
        page.goto(server.url()+'#/grid/local');page.get_by_role('button',name='New profile',exact=True).click();page.get_by_label('Profile display name',exact=True).fill('Sofia notebook');page.get_by_label('Profile handle',exact=True).fill('sofia.notes');page.get_by_label('Profile bio',exact=True).fill('Notes from the writing room.');page.get_by_role('button',name='Create profile',exact=True).click()
        page.wait_for_url('**/#/grid/local_*');page.get_by_role('button',name='Add draft',exact=True).click();page.locator('.composer-stage').wait_for()
        page.get_by_label('Import finished media',exact=True).set_input_files(str(original));page.locator('.composer-stage img').wait_for()
        caption=page.locator('textarea[data-path$=".copy.caption"]');caption.fill('A quiet place to begin.');page.get_by_role('button',name='Save draft',exact=True).click();page.wait_for_function('()=>document.querySelector(".ed-save").textContent.startsWith("Saved")')
        page.get_by_role('button',name='Saved writing',exact=True).click();page.get_by_role('button',name='New saved writing',exact=True).click();page.get_by_label('Writing name',exact=True).fill('Invitation');page.get_by_label('Saved writing text',exact=True).fill('Come sit with a story.');page.get_by_role('button',name='Save',exact=True).click();page.get_by_role('button',name='Invitation',exact=True).click();assert page.locator('.writing-preview').inner_text()=='Come sit with a story.';page.get_by_role('button',name='Insert a copy',exact=True).click();page.wait_for_function('()=>[...document.querySelectorAll("textarea")].some(t=>t.value==="Come sit with a story.")')
        page.screenshot(path='/private/tmp/ave-composer-evidence/writing-saved-390.png')
        created=next(p for p in store.list_posts(studio.sp) if any((v.get('copy') or {}).get('caption')=='Come sit with a story.' for v in p['variants']));page.get_by_role('button',name='Close composer',exact=True).click();page.reload();page.locator(f'.gcell[data-post="{created["id"]}"] .g-link').click();page.locator('.composer-stage img').wait_for();assert page.locator('textarea[data-path$=".copy.caption"]').input_value()=='Come sit with a story.'
        raw=next(p for p in store.list_posts(studio.sp) if any((v.get('copy') or {}).get('caption')=='Come sit with a story.' for v in p['variants']));assert raw['copy_provenance'][0]['revision']==1
        assert raw['variants'][0]['delivery']['method']=='manual'
        assert (studio.sp.root/store.read_assets(studio.sp)[raw['variants'][0]['media'][0]['asset']]['file']).read_bytes()==original.read_bytes()
        assert not errors,errors
    finally:page.close()


def test_dirty_failure_navigation_and_conflict_preserve_text(rig,browser):
    from tests.studio.test_production_ui import wait_for
    studio,server=rig;page=browser.new_page(viewport={'width':1440,'height':960});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    post=next(p for p in store.list_posts(studio.sp) if p['variants'] and not any(v.get('result') for v in p['variants']));vid=post['variants'][0]['id'];route=f'**/api/studio/posts/{post["id"]}/edit'
    try:
        page.goto(server.url()+f'#/post/{post["id"]}');caption=page.locator('textarea[data-path$=".copy.caption"]');caption.wait_for()
        page.route(route,lambda r:r.fulfill(status=503,content_type='application/json',body='{"error":"Injected save outage"}'))
        caption.fill('My exact unsaved words.');page.get_by_role('button',name='Save draft',exact=True).click();page.get_by_text('Not saved: Injected save outage',exact=True).wait_for()
        page.locator('#nav a[data-p="pipeline"]').click();page.get_by_role('heading',name='Keep your changes?',exact=True).wait_for();page.get_by_role('button',name='Save and continue',exact=True).click();assert caption.input_value()=='My exact unsaved words.'
        page.get_by_role('button',name='Cancel',exact=True).click();assert f'#/post/{post["id"]}' in page.url
        page.unroute(route)
        raw=store.load_post(studio.sp,post['id']);studio.edit_post(post['id'],{f'variants.{vid}.copy.caption':'Concurrent version from another editor.'},expected_rev=raw['rev'])
        page.get_by_role('button',name='Save draft',exact=True).click();page.get_by_role('heading',name='Another editor edited this too',exact=True).wait_for();assert page.get_by_text('My exact unsaved words.',exact=True).count()>0;assert page.get_by_text('Concurrent version from another editor.',exact=True).count()>0
        page.get_by_label('Keep mine',exact=True).check();page.get_by_role('button',name='Save my choice',exact=True).click();wait_for(lambda:store.load_post(studio.sp,post['id'])['variants'][0]['copy']['caption']=='My exact unsaved words.',page)
        page.screenshot(path='/private/tmp/ave-composer-evidence/conflict-resolved-1440.png')
        page.locator('#nav a[data-p="pipeline"]').click();page.locator('.production-table').wait_for();assert not errors,errors
    finally:page.close()


def test_carousel_keyboard_pointer_alt_undo_and_inherit(rig,browser):
    from tests.studio.test_production_ui import wait_for
    studio,server=rig;page=browser.new_page(viewport={'width':1440,'height':960});post=next(p for p in store.list_posts(studio.sp) if len(p['media'])==2 and p['media'][0]['role']=='image');vid=post['variants'][0]['id']
    try:
        page.goto(server.url()+f'#/post/{post["id"]}');page.get_by_label('Content slides').wait_for();page.locator('.composer-slide-edit > summary').click();page.get_by_label('Alt text for slide 1',exact=True).fill('First use of the same image');page.get_by_role('button',name='Save draft',exact=True).click()
        wait_for(lambda:store.load_post(studio.sp,post['id'])['variants'][0]['media'] is not None,page)
        saved=store.load_post(studio.sp,post['id']);assert saved['media'][0]['alt']!='First use of the same image'
        first=page.get_by_label('Select slide 1',exact=True);first.focus();page.keyboard.press('Alt+ArrowRight')
        wait_for(lambda:store.load_post(studio.sp,post['id'])['variants'][0]['media'][1]['alt']=='First use of the same image',page)
        assert page.locator(':focus').evaluate('(e)=>e.closest(".composer-slide")?.dataset.item')=='legacy_0'
        page.locator('.composer-slide-edit > summary').click();page.get_by_role('button',name='Undo media change',exact=True).click();wait_for(lambda:store.load_post(studio.sp,post['id'])['variants'][0]['media'][0]['alt']=='First use of the same image',page)
        a=page.get_by_label('Drag slide 1',exact=True).bounding_box();b=page.get_by_label('Select slide 2',exact=True).bounding_box();page.mouse.move(a['x']+a['width']/2,a['y']+a['height']/2);page.mouse.down();page.mouse.move(b['x']+b['width']/2,b['y']+b['height']/2,steps=12);page.mouse.up()
        # Insertion before the second slide leaves the first in place; reverse drag moves the second before the first.
        a=page.get_by_label('Drag slide 2',exact=True).bounding_box();b=page.get_by_label('Select slide 1',exact=True).bounding_box();page.mouse.move(a['x']+a['width']/2,a['y']+a['height']/2);page.mouse.down();page.mouse.move(b['x']+b['width']/2,b['y']+b['height']/2,steps=12);page.mouse.up()
        wait_for(lambda:store.load_post(studio.sp,post['id'])['variants'][0]['media'][1]['alt']=='First use of the same image',page)
        page.locator('.composer-slide-edit > summary').click();page.get_by_role('button',name='Use shared media',exact=True).click();wait_for(lambda:store.load_post(studio.sp,post['id'])['variants'][0]['media'] is None,page)
        caption=page.locator('textarea[data-path$=".copy.caption"]');caption.fill('');page.get_by_role('button',name='Save draft',exact=True).click();wait_for(lambda:store.load_post(studio.sp,post['id'])['variants'][0]['copy']['caption']=='',page)
        page.get_by_role('button',name='Reset to shared',exact=True).first.click();wait_for(lambda:store.load_post(studio.sp,post['id'])['variants'][0]['copy']['caption'] is None,page)
    finally:page.close()


def test_profiles_groups_context_and_cas(tmp_path):
    import copy
    from ave.studio import model,feed_plan
    studio=build(tmp_path);sp=studio.sp
    a=authoring.profile_save(studio,{'platform':'instagram','profile':{'display_name':'Reading room','bio':'First version'}},AGENT)
    b=authoring.profile_save(studio,{'platform':'instagram','profile':{'display_name':'Other room'}},AGENT)
    before=(sp.accounts_dir/(a['id']+'.yaml')).read_text()
    a2=authoring.profile_save(studio,{'expected_rev':a['revision'],'profile':{**a['profile'],'bio':'Second version'}},AGENT,a['id'])
    with pytest.raises(model.RevConflict):authoring.profile_save(studio,{'expected_rev':a['revision'],'profile':a['profile']},AGENT,a['id'])
    with pytest.raises(ValueError):authoring.profile_save(studio,{'expected_rev':a2['revision'],'profile':a['profile'],'credentials':'secret'},AGENT,a['id'])
    assert 'mode: manual' in (sp.accounts_dir/(a['id']+'.yaml')).read_text()
    post=studio.create_post(copy={'base':'Shared original'})
    for profile in (a,b):post=composer.add_variant(studio,post['id'],{'expected_rev':post['rev'],'account':profile['id'],'post_type':'feed'},AGENT)
    assert all(v['platform']=='instagram' for v in post['variants'])
    with pytest.raises(ValueError,match='content'):authoring.profile_delete(studio,a['id'],{'expected_rev':a2['revision']},AGENT)
    unused=authoring.profile_save(studio,{'platform':'instagram','profile':{'display_name':'Unused'}},AGENT)
    authoring.profile_delete(studio,unused['id'],{'expected_rev':unused['revision']},AGENT)
    g=authoring.group_save(studio,{'expected_rev':0,'item':{'name':'Opening','kind':'hook','text':'An opening line'}},AGENT)
    item=g['items'][0];g2=authoring.group_save(studio,{'expected_rev':g['rev'],'item':{k:v for k,v in {**item,'text':'A different line'}.items() if k in ('id','kind','name','text')}},AGENT)
    assert g2['items'][0]['rev']==2
    with pytest.raises(model.RevConflict):authoring.group_save(studio,{'expected_rev':g['rev'],'remove':item['id']},AGENT)
    assert authoring.group_save(studio,{'expected_rev':g2['rev'],'remove':item['id']},AGENT)['items']==[]
    before_posts=copy.deepcopy(store.list_posts(sp));plan=feed_plan.read(studio,a['id']);key=next(c['key'] for c in plan['cells'] if c['post']==post['id'])
    pinned=feed_plan.decorate(studio,account=a['id'],expected_rev=plan['rev'],snapshot=plan['snapshot'],op='pin',key=key)
    assert pinned['cells'][0]['key']==key and pinned['cells'][0]['pinned']
    image=next(x for x in store.read_assets(sp).values() if x['kind']=='image')
    entry={'title':'Published reading','url':'https://example.invalid/p/one','posted_at':'2026-10-28T10:00:00+00:00','thumbnail':image['id'],'post_type':'feed'}
    context=feed_plan.decorate(studio,account=a['id'],expected_rev=pinned['rev'],snapshot=pinned['snapshot'],op='external',entry=entry)
    assert any(c.get('external') and c['locked'] for c in context['cells'])
    assert not any(c.get('external') or c.get('pinned') for c in feed_plan.read(studio,b['id'])['cells'])
    with pytest.raises(ValueError,match='already'):feed_plan.decorate(studio,account=a['id'],expected_rev=context['rev'],snapshot=context['snapshot'],op='external',entry=entry)
    with pytest.raises(feed_plan.PlanConflict):feed_plan.decorate(studio,account=a['id'],expected_rev=plan['rev'],snapshot=plan['snapshot'],op='pin',key=key)
    assert store.list_posts(sp)==before_posts


def test_media_rejection_and_original_preview_bytes(tmp_path):
    import copy
    from ave.studio import model
    from ave.studio.sync import _spec
    studio=build(tmp_path);post=next(p for p in store.list_posts(studio.sp) if len(p['media'])==2 and p['media'][0]['role']=='image');v=post['variants'][0];assets=store.read_assets(studio.sp)
    before={a['id']:(studio.sp.root/a['file']).read_bytes() for a in assets.values()};args={'expected_rev':post['rev'],'variant':v['id']}
    for body in ({'op':'add','asset':'missing'},{'op':'add','asset':post['media'][0]['asset'],'ignored':True},{'op':'cover','asset':next(a['id'] for a in assets.values() if a['kind']=='video')}):
        with pytest.raises(ValueError):composer.media_action(studio,post['id'],{**args,**body},AGENT)
    p=copy.deepcopy(post);p['media'][1]['slot']=p['media'][0]['slot'];assert composer.projection(p,v,assets)['problems']
    p=copy.deepcopy(post);p['media']=[{**m,'role':'cover'} for m in p['media']];assert 'Choose only one cover' in composer.projection(p,v,assets)['problems']
    changed=composer.media_action(studio,post['id'],{**args,'op':'crop','item':'legacy_0','crop':{'v':1,'x':.1,'y':.9,'aspect':'3:4','purpose':'preview'}},AGENT)
    with pytest.raises(model.RevConflict):composer.media_action(studio,post['id'],{**args,'op':'remove','item':'legacy_0'},AGENT)
    assert before=={a['id']:(studio.sp.root/a['file']).read_bytes() for a in assets.values()}
    altered=copy.deepcopy(assets);altered[post['media'][0]['asset']]['file']='../outside.png'
    assert _spec(studio.sp,post,v,altered)[0] is None
    altered=copy.deepcopy(assets);altered[post['media'][0]['asset']]['sha256']='0'*64
    assert any('changed' in p for p in _spec(studio.sp,post,v,altered)[1])


def test_version_three_approval_bound_options_and_reviewed_zip(tmp_path,monkeypatch):
    from tests.test_manual_export import make_post
    from ave.studio.project import StudioProject
    from ave.studio.service import open_studio
    from ave.studio.sync import sync_queue
    from ave.studio.dispatch import approval_bind,blockers
    from ave.studio.manual_export import build as pack
    from ave.publish import queue
    sp=StudioProject(tmp_path/'bound');sp.create(title='Isolated signature fixture');post=make_post(sp,tmp_path,variant=True);studio=open_studio(sp.root);v=post['variants'][0]
    post=studio.edit_post(post['id'],{f"variants.{v['id']}.delivery":{'method':'manual'}},expected_rev=post['rev'])
    monkeypatch.setattr(queue,'get_publisher',lambda _:(_ for _ in ()).throw(AssertionError('Manual used adapter')))
    for patch in ({'cover':None},{'presentation':{'feed_presence':False}},{'delivery':{'method':'automatic'}}):
        sync_queue(sp,post['id']);post=store.load_post(sp,post['id']);v=post['variants'][0];item=queue.load(sp,v['queue']['id'])
        # Test-only injected approval authority, exactly like manual export contract fixtures.
        approved=queue.approve(sp,item['id'],'Synthetic reviewer',item['sha256'][:12],bind=approval_bind(sp,v))
        assert approved['status']=='approved'
        result=pack(sp,post['id'],expected_rev=post['rev']);assert result['manifest']['reviewed']
        if 'cover' in patch:
            rec=store.read_assets(sp)[post['media'][0]['asset']];patch={'cover':{k:rec[k] for k in ('id','file','sha256')}}
        from studio_helpers import RecordingPublisher
        updated=queue.update(sp,item['id'],publishers={'instagram':RecordingPublisher('instagram')},**patch);assert updated['status']!='approved'
        assert any(e['event']=='approval_invalidated' for e in updated['events'])
        with pytest.raises(ValueError,match='downgraded'):queue.update(sp,item['id'],meta_v=2)
        # Return the queue to the saved manual post for the next independent mutation.
    assert store.load_post(sp,post['id'])['variants'][0]['delivery']['method']=='manual'


def test_touch_reorder_back_forward_and_visible_caption(rig,browser):
    from tests.studio.test_production_ui import wait_for
    studio,server=rig;context=browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True);page=context.new_page();post=next(p for p in store.list_posts(studio.sp) if len(p['media'])==2 and p['media'][0]['role']=='image')
    try:
        page.goto(server.url()+'#/grid/author?surface=all');page.locator(f'.gcell[data-post="{post["id"]}"] .g-link').click();page.locator('.composer-stage').wait_for()
        a=page.get_by_label('Drag slide 2',exact=True).bounding_box();b=page.get_by_label('Select slide 1',exact=True).bounding_box();session=context.new_cdp_session(page)
        x,y=a['x']+a['width']/2,a['y']+a['height']/2;session.send('Input.dispatchTouchEvent',{'type':'touchStart','touchPoints':[{'x':x,'y':y}]})
        for n in range(1,11):session.send('Input.dispatchTouchEvent',{'type':'touchMove','touchPoints':[{'x':x+(b['x']+b['width']/2-x)*n/10,'y':y+(b['y']+b['height']/2-y)*n/10}]})
        session.send('Input.dispatchTouchEvent',{'type':'touchEnd','touchPoints':[]})
        wait_for(lambda:store.load_post(studio.sp,post['id'])['variants'][0]['media'] is not None,page)
        assert store.load_post(studio.sp,post['id'])['variants'][0]['media'][0]['id']=='legacy_1'
        caption=page.locator('textarea[data-path$=".copy.caption"]');caption.fill('Phone draft kept through browser history.');page.get_by_role('button',name='Save draft',exact=True).click();wait_for(lambda:store.load_post(studio.sp,post['id'])['variants'][0]['copy']['caption']=='Phone draft kept through browser history.',page)
        page.go_back();page.wait_for_function('()=>!document.querySelector(".planner-composer")');page.go_forward();page.locator('.composer-stage').wait_for();assert caption.input_value()=='Phone draft kept through browser history.'
        box=caption.bounding_box();footer=page.locator('.composer-savebar').bounding_box();assert footer['y']-box['y']>=45
        page.screenshot(path='/private/tmp/ave-composer-evidence/edited-touch-390.png')
    finally:context.close()


def test_production_source_drafts_and_anchors_survive_navigation(rig,browser):
    from tests.studio.test_production_ui import wait_for
    studio,server=rig;page=browser.new_page(viewport={'width':1440,'height':960});post=next(p for p in store.list_posts(studio.sp) if p['media'] and p['production']['status']!='ready' and not any(v.get('result') for v in p['variants']));pid=post['id']
    try:
        page.goto(server.url()+'#/pipeline');page.locator(f'tr[data-post="{pid}"] .prod-open').click();page.get_by_label('Audience',exact=True).fill('A carefully preserved audience note.')
        page.locator('#nav a[data-p="workbench"]').click();page.get_by_role('heading',name='Sources',exact=True).wait_for();assert store.load_post(studio.sp,pid)['production']['brief']['audience']=='A carefully preserved audience note.'
        page.get_by_role('button',name='Add source',exact=True).click();page.get_by_label('Upload source file',exact=True).set_input_files({'name':'draft-note.md','mimeType':'text/markdown','buffer':b'A complete thought for this source. Another complete thought.'})
        page.locator('#nav a[data-p="pipeline"]').click();page.get_by_role('heading',name='Keep your changes?',exact=True).wait_for();page.get_by_role('button',name='Cancel',exact=True).click();assert page.get_by_label('Upload source file',exact=True).evaluate('(e)=>e.files[0].name')=='draft-note.md'
        page.locator('#nav a[data-p="pipeline"]').click();page.get_by_role('button',name='Save and continue',exact=True).click();page.locator('.production-table').wait_for()
        page.locator(f'tr[data-post="{pid}"] .prod-open').click();page.get_by_role('button',name='Tasks',exact=True).click();page.get_by_label('Task title',exact=True).fill('Keep this unfinished task')
        page.locator('#nav a[data-p="workbench"]').click();page.get_by_role('button',name='Save and continue',exact=True).click();page.wait_for_timeout(300);assert not page.get_by_role('heading',name='Keep your changes?',exact=True).is_visible(),page.locator('body').inner_text();page.get_by_role('heading',name='Sources',exact=True).wait_for();task=next(t for t in store.load_post(studio.sp,pid)['production']['checklist'] if t['title']=='Keep this unfinished task')
        page.locator('#nav a[data-p="pipeline"]').click();page.locator(f'tr[data-post="{pid}"] .prod-open').click();page.get_by_role('button',name='Discussion',exact=True).click();page.get_by_label('Discussion anchor',exact=True).select_option('task:'+task['id']);page.get_by_label('Comment',exact=True).fill('Check this task after the next revision.')
        page.locator('#nav a[data-p="workbench"]').click();page.get_by_role('button',name='Save and continue',exact=True).click();page.get_by_role('heading',name='Sources',exact=True).wait_for()
        page.locator('#nav a[data-p="pipeline"]').click();page.locator(f'tr[data-post="{pid}"] .prod-open').click();page.get_by_role('button',name='Discussion',exact=True).click();page.locator('.prod-comment p').get_by_text('Check this task after the next revision.',exact=True).wait_for();assert 'Task Keep this unfinished task' in page.locator('.prod-comment small').last.inner_text()
    finally:page.close()


def test_second_channel_profile_edit_groups_and_prepare_review(rig,browser):
    from tests.studio.test_production_ui import wait_for
    studio,server=rig;profile=authoring.profile_save(studio,{'platform':'instagram','profile':{'display_name':'Second notebook'}},AGENT);post=next(p for p in store.list_posts(studio.sp) if len(p['media'])==2 and p['media'][0]['role']=='image');page=browser.new_page(viewport={'width':1440,'height':960})
    from ave.library import registry
    from ave.library.ingest import default_own_entry
    for asset in store.read_assets(studio.sp).values():
        registry.license_log(studio.sp).append({**default_own_entry('synthetic-test','2026-10-01T00:00:00'),'sha256':asset['sha256'],'file':asset['file'],'bytes':asset['bytes'],'approved_by':'synthetic-test'})
    try:
        page.goto(server.url()+f'#/post/{post["id"]}');page.get_by_role('button',name='Add channel',exact=True).click();page.get_by_label('Channel profile',exact=True).select_option(profile['id']);page.get_by_label('Post format',exact=True).select_option('carousel');page.get_by_role('dialog').get_by_role('button',name='Add channel',exact=True).click();page.get_by_role('dialog').wait_for(state='hidden')
        page.locator('textarea[data-path$=".copy.caption"]').fill('Copy for the second notebook.');page.get_by_role('button',name='Save draft',exact=True).click();wait_for(lambda:len(store.load_post(studio.sp,post['id'])['variants'])==2 and store.load_post(studio.sp,post['id'])['variants'][1]['copy']['caption']=='Copy for the second notebook.',page)
        raw=store.load_post(studio.sp,post['id']);assert raw['variants'][0]['copy']['caption'] is None
        page.get_by_role('button',name='Prepare review',exact=True).click();wait_for(lambda:store.load_post(studio.sp,post['id'])['variants'][1]['queue'] is not None,page)
        raw=store.load_post(studio.sp,post['id']);assert raw['variants'][0]['queue'] is None
        page.get_by_role('button',name='Edit post title',exact=True).click();page.get_by_label('Post title',exact=True).fill('A passage for two notebooks');page.get_by_role('dialog').get_by_role('button',name='Save',exact=True).click();page.get_by_text('A passage for two notebooks',exact=True).first.wait_for()
        page.get_by_role('button',name='Saved writing',exact=True).click();page.get_by_role('button',name='New saved writing',exact=True).click();page.get_by_label('Writing name',exact=True).fill('Closing');page.get_by_label('Saved writing text',exact=True).fill('Original closing.');page.get_by_role('dialog').last.get_by_role('button',name='Save',exact=True).click();page.get_by_role('button',name='Closing',exact=True).click();page.get_by_role('button',name='Edit saved writing',exact=True).click();page.get_by_label('Saved writing text',exact=True).fill('Revised closing.');page.get_by_role('dialog').last.get_by_role('button',name='Save',exact=True).click();page.get_by_text('Revised closing.',exact=True).wait_for();assert page.locator('.writing-preview').inner_text()=='Revised closing.'
        page.get_by_role('button',name='Insert a copy',exact=True).click();wait_for(lambda:store.load_post(studio.sp,post['id'])['variants'][1]['copy']['caption']=='Revised closing.',page)
        page.get_by_role('button',name='Saved writing',exact=True).click();page.get_by_label('Find saved writing',exact=True).fill('Closing');page.get_by_role('button',name='Closing',exact=True).click();page.get_by_role('button',name='Remove saved writing',exact=True).click();page.get_by_role('button',name='Done',exact=True).click();assert store.load_post(studio.sp,post['id'])['variants'][1]['copy']['caption']=='Revised closing.'
        page.goto(server.url()+f'#/grid/{profile["id"]}');page.get_by_role('button',name='Edit profile',exact=True).click();page.get_by_label('Profile bio',exact=True).fill('A revised local biography.');page.get_by_role('button',name='Save profile',exact=True).click();wait_for(lambda:next(p for p in authoring.profiles(studio) if p['id']==profile['id'])['profile'].get('bio')=='A revised local biography.',page)
    finally:page.close()


def test_gui_frame_cover_keeps_dirty_caption_and_source_bytes(rig,browser):
    from tests.studio.test_production_ui import wait_for
    studio,server=rig;post=next(p for p in store.list_posts(studio.sp) if any(m['role']=='video' for m in p['media']));video=next(m for m in post['media'] if m['role']=='video');record=store.read_assets(studio.sp)[video['asset']];before=(studio.sp.root/record['file']).read_bytes();page=browser.new_page(viewport={'width':1024,'height':768});route=f'**/api/studio/posts/{post["id"]}/edit'
    try:
        page.goto(server.url()+f'#/post/{post["id"]}');page.locator('.composer-cover > summary').click();page.get_by_label('Cover frame seconds',exact=True).fill('0.2');page.get_by_role('button',name='Extract cover frame',exact=True).click();page.get_by_text('Extracting cover frame…',exact=True).wait_for()
        page.route(route,lambda r:r.fulfill(status=503,content_type='application/json',body='{"error":"Injected caption outage"}'))
        caption=page.locator('textarea[data-path$=".copy.caption"]');caption.fill('Still here when the frame job finishes.');page.get_by_role('button',name='Save draft',exact=True).click()
        page.wait_for_function('()=>document.querySelector(".ed-banner").textContent.includes("while you were typing")',timeout=20000)
        assert caption.input_value()=='Still here when the frame job finishes.'
        raw=store.load_post(studio.sp,post['id']);cover=composer.projection(raw,raw['variants'][0],store.read_assets(studio.sp))['cover'];assert cover['asset']!=next(m for m in post['media'] if m['role']=='cover')['asset']
        assert (studio.sp.root/record['file']).read_bytes()==before
        page.unroute(route);page.get_by_role('button',name='Save draft',exact=True).click();wait_for(lambda:store.load_post(studio.sp,post['id'])['variants'][0]['copy']['caption']=='Still here when the frame job finishes.',page)
        from playwright.sync_api import expect
        expect(page.locator('.composer-cover-comparison figure:first-child img')).to_have_attribute('src',f'/api/studio/workbench/preview/{cover["asset"]}/large')
        if not page.locator('.composer-cover').evaluate('(d)=>d.open'):page.locator('.composer-cover > summary').click()
        page.locator('.composer-cover-comparison figure:first-child img').wait_for();page.wait_for_function('()=>document.querySelector(".composer-cover-comparison figure:first-child img").complete&&document.querySelector(".composer-cover-comparison figure:first-child img").naturalWidth>0')
        page.screenshot(path='/private/tmp/ave-composer-evidence/extracted-cover-1024.png')
    finally:page.close()


def test_frame_rejects_nonfinite_out_of_range_and_cancelled_job(tmp_path):
    from ave.studio.workbench import validate_operation,execute_operation,job_path
    studio=build(tmp_path);post=next(p for p in store.list_posts(studio.sp) if any(m['role']=='video' for m in p['media']));args={'post':post['id'],'variant':post['variants'][0]['id'],'asset':post['media'][0]['asset'],'seconds':.2,'expected_rev':post['rev']}
    for sec in (float('nan'),float('inf'),-1,100):
        with pytest.raises(ValueError,match='within'):validate_operation(studio.sp,'frame',{**args,'seconds':sec})
    job={'id':'job_'+'f'*24,'args':args,'operation':'frame','by':'Synthetic test'};marker=job_path(studio.sp,job['id']).with_suffix('.cancel');marker.parent.mkdir(parents=True,exist_ok=True);marker.write_text('cancel')
    with pytest.raises(ValueError,match='cancelled'):execute_operation(studio,job)
    assert store.load_post(studio.sp,post['id'])==post


def test_local_pins_reported_context_avatar_and_profile_delete(rig,browser):
    from tests.studio.test_production_ui import wait_for
    from ave.studio import feed_plan
    studio,server=rig;page=browser.new_page(viewport={'width':1440,'height':960});before=store.list_posts(studio.sp)
    try:
        page.goto(server.url()+'#/grid/author');page.locator('.gcell').first.wait_for();page.get_by_role('button',name='Pin in this preview',exact=True).click();page.get_by_text('Pinned · local',exact=True).wait_for();assert len(feed_plan.read(studio,'author')['pins'])==1
        page.get_by_role('button',name='Add published context',exact=True).click();page.get_by_label('Published post title',exact=True).fill('From last week’s reading');page.get_by_label('Published post URL',exact=True).fill('https://example.invalid/reported/reading');page.get_by_label('Reported publication time',exact=True).fill('2026-10-27T10:00');page.get_by_role('button',name='Add to preview',exact=True).click();page.get_by_role('dialog').wait_for(state='hidden');page.get_by_role('link',name='From last week’s reading.',exact=False).click();page.get_by_text('Published elsewhere · reported by you',exact=True).wait_for();page.get_by_role('button',name='Remove preview context',exact=True).click();wait_for(lambda:not feed_plan.read(studio,'author')['external'],page)
        assert store.list_posts(studio.sp)==before
        page.get_by_role('button',name='Edit profile',exact=True).click();page.get_by_role('button',name='Choose avatar',exact=True).click();page.locator('.lib-grid.pick .lib-open').first.click();page.get_by_role('button',name='Save profile',exact=True).click();page.get_by_role('dialog').wait_for(state='hidden');page.locator('.planner-profile-avatar').wait_for();page.wait_for_function('()=>document.querySelector(".planner-profile-avatar").naturalWidth>0')
        page.goto(server.url()+'#/grid/local');page.get_by_role('button',name='New profile',exact=True).click();page.get_by_label('Profile display name',exact=True).fill('Temporary reading room');page.get_by_role('button',name='Create profile',exact=True).click();page.get_by_role('dialog').wait_for(state='hidden');page.get_by_role('button',name='Edit profile',exact=True).click();page.get_by_role('button',name='Remove unused local profile',exact=True).click();page.wait_for_url('**/#/grid/local');assert not any(p['label']=='Temporary reading room' for p in authoring.profiles(studio))
    finally:page.close()


def test_legacy_slot_order_matches_preview_alt_and_payload(rig,browser):
    from tests.studio.test_production_ui import wait_for
    from ave.studio.sync import _spec
    studio,server=rig;post=next(p for p in store.list_posts(studio.sp) if len(p['media'])==2 and p['media'][0]['role']=='image');v=post['variants'][0];post['variants'][0]['media']=[{**post['media'][0],'slot':3,'alt':'Later slide'},{**post['media'][1],'slot':0,'alt':'Opening slide'}];post=store.save_post(studio.sp,post,expected_rev=post['rev'],actor=AGENT,op='synthetic.unsorted')
    page=browser.new_page(viewport={'width':1440,'height':960})
    try:
        page.goto(server.url()+f'#/post/{post["id"]}');page.locator('.composer-slide-edit > summary').click();assert page.get_by_label('Alt text for slide 1',exact=True).input_value()=='Opening slide';page.get_by_label('Alt text for slide 1',exact=True).fill('Revised opening');page.get_by_role('button',name='Save draft',exact=True).click();wait_for(lambda:store.load_post(studio.sp,post['id'])['variants'][0]['media'][1]['alt']=='Revised opening',page)
        raw=store.load_post(studio.sp,post['id']);fields,problems=_spec(studio.sp,raw,raw['variants'][0],store.read_assets(studio.sp));assert not problems;assert fields['alt_texts']==['Revised opening','Later slide'];assert page.locator('.composer-stage img').get_attribute('alt')=='Revised opening'
    finally:page.close()
