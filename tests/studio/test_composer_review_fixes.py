"""Exact regressions from Task4a independent review; fictional local studios only."""
import copy
import hashlib
import zipfile
from pathlib import Path
import pytest
from ave.studio import composer, authoring, store, feed_plan
from ave.studio.events import AGENT
from tests.studio.test_production_ui import build, browser, rig, wait_for


def carousel(studio):
    return next(p for p in store.list_posts(studio.sp) if len(p['media'])==2 and p['media'][0]['role']=='image')


def projected(studio, post, variant):
    p=composer.projection(post,next((v for v in post['variants'] if v['id']==variant),{}),store.read_assets(studio.sp))
    return p['content']+([p['cover']] if p['cover'] else [])


def action(studio, post, variant, op, **args):
    return composer.media_action(studio,post['id'],{'expected_rev':post['rev'],'variant':variant,'op':op,**args},AGENT)


@pytest.mark.parametrize('inherited',[False,True])
@pytest.mark.parametrize('later_alt',[False,True])
def test_undo_move_preserves_later_metadata_and_inheritance(tmp_path,inherited,later_alt):
    s=build(tmp_path);p=carousel(s);vid=p['variants'][0]['id']
    if not inherited:p=action(s,p,vid,'alt',item='legacy_0',alt='Customized before move')
    before=projected(s,p,vid)
    p=action(s,p,vid,'move',item='legacy_1',before='legacy_0')
    change={'post':p['id'],'variant':vid,'operation':'move','before':before,'after':projected(s,p,vid),'inherited':inherited}
    if later_alt:p=action(s,p,vid,'alt',item='legacy_0',alt='Newer alt survives Undo')
    p=action(s,p,vid,'undo',change=change)
    assert [m['id'] for m in projected(s,p,vid)]==['legacy_0','legacy_1']
    assert projected(s,p,vid)[0]['alt']==('Newer alt survives Undo' if later_alt else before[0]['alt'])
    assert (p['variants'][0]['media'] is None)==(inherited and not later_alt)


def test_undo_scope_and_overlapping_change_refused(tmp_path):
    s=build(tmp_path);p=carousel(s);vid=p['variants'][0]['id'];before=projected(s,p,vid)
    p=action(s,p,vid,'alt',item='legacy_0',alt='First edit')
    change={'post':p['id'],'variant':vid,'operation':'alt','before':before,'after':projected(s,p,vid),'inherited':True}
    p=action(s,p,vid,'alt',item='legacy_0',alt='Later overlapping edit')
    for invalid in ({**change,'variant':'other'}, {**change,'post':'other'}):
        with pytest.raises(ValueError,match='another post or channel'):action(s,p,vid,'undo',change=invalid)
    with pytest.raises(ValueError,match='changed again'):action(s,p,vid,'undo',change=change)
    with pytest.raises(ValueError,match='Unknown media operation'):action(s,p,vid,'restore',items=before)
    assert store.load_post(s.sp,p['id'])['rev']==p['rev']
    assert projected(s,p,vid)[0]['alt']=='Later overlapping edit'


@pytest.mark.parametrize('in_flight',[False,True])
def test_hook_uses_dirty_body_and_pending_provenance(rig,browser,in_flight):
    s,server=rig;p=carousel(s);vid=p['variants'][0]['id']
    for name,text in [('First hook','Hook first.'),('Second hook','Hook second.')]:
        groups=authoring.groups(s);authoring.group_save(s,{'expected_rev':groups['rev'],'item':{'name':name,'kind':'hook','text':text}},AGENT)
    page=browser.new_page();route=f'**/api/studio/posts/{p["id"]}/edit'
    try:
        if in_flight:page.add_init_script('''window.holdSave=true;const realFetch=window.fetch;window.fetch=async(...args)=>{if(window.holdSave&&String(args[0]).endsWith('/edit')){window.holdSave=false;await new Promise(resolve=>window.releaseSave=resolve);}return realFetch(...args);};''')
        page.goto(server.url()+f'#/post/{p["id"]}');caption=page.locator('textarea[data-path$=".copy.caption"]');caption.wait_for()
        if not in_flight:page.route(route,lambda r:r.fulfill(status=503,content_type='application/json',body='{"error":"Save outage"}'))
        caption.fill('UNSAVED BODY MUST SURVIVE');page.get_by_role('button',name='Save draft',exact=True).click()
        if in_flight:page.wait_for_function('()=>typeof window.releaseSave==="function"')
        else:page.get_by_text('Not saved: Save outage',exact=True).wait_for()
        for name in ('First hook','Second hook'):
            page.get_by_role('button',name='Saved writing',exact=True).click();page.get_by_role('button',name=name,exact=True).click();page.get_by_role('button',name='Insert a copy',exact=True).click()
            if in_flight and name=='First hook':
                # Save completion must not clear the newer hook/body/provenance draft.
                page.evaluate('window.releaseSave()')
            page.get_by_role('dialog').wait_for(state='hidden')
        expected='Hook second.\n\nHook first.\n\nUNSAVED BODY MUST SURVIVE'
        assert caption.input_value()==expected
        page.unroute(route);page.get_by_role('button',name='Save draft',exact=True).click()
        wait_for(lambda:store.load_post(s.sp,p['id'])['variants'][0]['copy']['caption']==expected,page)
        saved=store.load_post(s.sp,p['id']);assert len(saved['copy_provenance'])==2
        assert all(x['revision']==1 and x['field']==f'variants.{vid}.copy.caption' for x in saved['copy_provenance'])
    finally:page.close()


def test_conflict_discard_switch_saves_latest_and_clears_undo(rig,browser):
    s,server=rig;p=carousel(s);first=p['variants'][0]['id']
    profile=authoring.profile_save(s,{'platform':'instagram','profile':{'display_name':'Second profile'}},AGENT)
    p=composer.add_variant(s,p['id'],{'expected_rev':p['rev'],'account':profile['id'],'post_type':'feed'},AGENT);second=p['variants'][1]['id']
    page=browser.new_page()
    try:
        page.goto(server.url()+f'#/post/{p["id"]}');page.get_by_label('Select slide 1',exact=True).focus();page.keyboard.press('Alt+ArrowRight')
        wait_for(lambda:store.load_post(s.sp,p['id'])['variants'][0]['media'] is not None,page)
        page.locator('.composer-slide-edit > summary').click();page.get_by_role('button',name='Undo media change',exact=True).wait_for()
        raw=store.load_post(s.sp,p['id']);s.edit_post(p['id'],{f'variants.{first}.copy.caption':'Concurrent body'},expected_rev=raw['rev'])
        caption=page.locator('textarea[data-path$=".copy.caption"]');caption.fill('Discard this conflict');page.get_by_role('button',name='Save draft',exact=True).click();page.get_by_role('heading',name='Another editor edited this too',exact=True).wait_for()
        page.locator(f'[role=tab][data-variant="{second}"]').click();page.get_by_role('button',name='Discard changes',exact=True).click()
        page.locator(f'[role=tab][data-variant="{second}"][aria-selected=true]').wait_for()
        assert page.locator('.ed-3way').count()==0 and page.get_by_role('button',name='Undo media change',exact=True).count()==0
        caption.fill('Second channel new text');page.get_by_role('button',name='Save draft',exact=True).click()
        wait_for(lambda:store.load_post(s.sp,p['id'])['variants'][1]['copy']['caption']=='Second channel new text',page)
        page.locator(f'[role=tab][data-variant="{first}"]').click();assert caption.input_value()=='Concurrent body'
        assert page.get_by_role('button',name='Undo media change',exact=True).count()==0
    finally:page.close()


def test_undo_last_slide_accessible_and_gui_later_alt_preserved(rig,browser):
    s,server=rig;p=carousel(s);page=browser.new_page(viewport={'width':390,'height':844})
    try:
        page.goto(server.url()+f'#/post/{p["id"]}');page.get_by_label('Select slide 1',exact=True).focus();page.keyboard.press('Alt+ArrowRight')
        wait_for(lambda:store.load_post(s.sp,p['id'])['variants'][0]['media'] is not None,page)
        page.locator('.composer-slide-edit > summary').click();page.get_by_label('Alt text for slide 1',exact=True).fill('Later alt retained');page.get_by_role('button',name='Save draft',exact=True).click()
        wait_for(lambda:store.load_post(s.sp,p['id'])['variants'][0]['media'][0]['alt']=='Later alt retained',page)
        page.get_by_role('button',name='Undo media change',exact=True).click();wait_for(lambda:store.load_post(s.sp,p['id'])['variants'][0]['media'][1]['alt']=='Later alt retained',page)
        single=next(x for x in store.list_posts(s.sp) if len(x['media'])==1 and not x['variants']);page.goto(server.url()+f'#/post/{single["id"]}')
        page.locator('.composer-slide-edit > summary').click();page.get_by_role('button',name='Remove',exact=True).click();page.get_by_text('Build your next post',exact=True).wait_for()
        page.get_by_role('button',name='Undo media change',exact=True).click();page.locator('.composer-stage img').wait_for();assert store.load_post(s.sp,single['id'])['media'][0]['asset']==single['media'][0]['asset']
    finally:page.close()


@pytest.mark.parametrize('width,height',[(390,844),(1024,768)])
def test_cover_focal_touch_preview_export_and_fresh_reel_presence(rig,browser,width,height):
    from ave.studio.manual_export import build as export
    s,server=rig;source=next(p for p in store.list_posts(s.sp) if any(m['role']=='video' for m in p['media']))
    # The shared preview fixture registers its MP4 in work/. This export journey
    # copies those same synthetic bytes into the canonical library location.
    from ave.library import registry
    record=store.read_assets(s.sp)[source['media'][0]['asset']]
    destination=s.sp.root/registry.rel_file(record['sha256'],'.mp4');destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_bytes((s.sp.root/record['file']).read_bytes());registry.append(s.sp,{**record,'file':s.sp.rel(destination)})
    legacy=copy.deepcopy(source['variants'][0])
    profile=authoring.profile_save(s,{'platform':'instagram','profile':{'display_name':'Focal profile'}},AGENT)
    p=s.create_post(copy={'base':'A cover in context'},media=source['media']);p=composer.add_variant(s,p['id'],{'expected_rev':p['rev'],'account':profile['id'],'post_type':'reel'},AGENT)
    assert p['variants'][0]['presentation']=={'feed_presence':True}
    before={m['asset']:(s.sp.root/store.read_assets(s.sp)[m['asset']]['file']).read_bytes() for m in p['media']}
    page=browser.new_page(viewport={'width':width,'height':height},has_touch=True);errors=[];page.on('pageerror',lambda e:errors.append(str(e)));out=Path('/private/tmp/ave-composer-fix-evidence');out.mkdir(exist_ok=True)
    try:
        page.goto(server.url()+f'#/post/{p["id"]}');check=page.get_by_label('Show Reel in the profile feed',exact=True);check.wait_for();assert check.is_checked()
        page.locator('.composer-slide-edit > summary').click();page.locator('.composer-focal > summary').click()
        for label in ('Horizontal focal position','Vertical focal position'):
            control=page.get_by_label(label,exact=True);control.scroll_into_view_if_needed();assert control.bounding_box()['height']>=44
        page.screenshot(path=str(out/f'expanded-slide-focal-{width}.png'))
        page.locator('.composer-slide-edit > summary').click()
        page.locator('.composer-cover > summary').click();page.get_by_label('Cover horizontal focal position',exact=True).fill('0.1')
        wait_for(lambda:projected(s,store.load_post(s.sp,p['id']),p['variants'][0]['id'])[-1].get('crop',{}).get('x')==.1,page)
        page.get_by_label('Cover vertical focal position',exact=True).fill('0.8')
        wait_for(lambda:projected(s,store.load_post(s.sp,p['id']),p['variants'][0]['id'])[-1].get('crop',{}).get('y')==.8,page)
        revision=store.load_post(s.sp,p['id'])['rev'];page.wait_for_function('(rev)=>document.querySelector(".ed-sub")?.textContent===`Draft revision ${rev}` && document.querySelector(".ed-save")?.textContent==="Saved"',arg=revision)
        page.wait_for_function('()=>[...document.querySelectorAll(".composer-cover-comparison img")].every(i=>i.complete&&i.naturalWidth>0)')
        for label in ('Cover frame seconds','Cover horizontal focal position','Cover vertical focal position'):
            node=page.get_by_label(label,exact=True);node.scroll_into_view_if_needed();assert node.bounding_box()['height']>=44
        assert page.locator('body').evaluate('(e)=>e.scrollWidth<=innerWidth')
        assert page.locator('.composer-cover-comparison img').first.evaluate('(e)=>getComputedStyle(e).objectFit')=='contain'
        assert page.locator('.composer-cover-comparison img').last.evaluate('(e)=>getComputedStyle(e).objectPosition')=='10% 80%'
        page.locator('.composer-cover-comparison').scroll_into_view_if_needed();page.screenshot(path=str(out/f'cover-comparison-{width}.png'))
        page.get_by_label('Cover frame seconds',exact=True).scroll_into_view_if_needed();page.screenshot(path=str(out/f'expanded-frame-{width}.png'))
        raw=store.load_post(s.sp,p['id']);proj=projected(s,raw,p['variants'][0]['id']);assert not proj[0].get('crop') and proj[-1]['role']=='cover'
        cell=next(c for c in feed_plan.read(s,profile['id'])['cells'] if c['post']==p['id']);assert cell['crop']['x']==.1 and cell['feed_presence'] is True
        page.goto(server.url()+f'#/grid/{profile["id"]}?surface=feed');tile=page.locator(f'.gcell[data-post="{p["id"]}"]');tile.wait_for();assert tile.locator('img').first.evaluate('(e)=>getComputedStyle(e).objectPosition')=='10% 80%'
        page.goto(server.url()+f'#/phone/{profile["id"]}?surface=feed');image=page.get_by_role('button',name='Preview A cover in context',exact=True).locator('img');image.wait_for();assert image.evaluate('(e)=>getComputedStyle(e).objectPosition')=='10% 80%'
        image.evaluate('(i)=>i.decode()');page.screenshot(path=str(out/f'cover-phone-{width}.png'))
        package=export(s.sp,p['id'],draft=True,expected_rev=raw['rev']);assert package['manifest']['presentation']['feed_presence'] is True
        with zipfile.ZipFile(package['zip']) as z:
            for m in package['manifest']['media']+[package['manifest']['cover']]:assert hashlib.sha256(z.read(m['filename'])).hexdigest()==m['sha256']
            assert b'show in profile feed' in z.read('index.html')
        assert all((s.sp.root/store.read_assets(s.sp)[aid]['file']).read_bytes()==data for aid,data in before.items())
        page.goto(server.url()+f'#/post/{p["id"]}');page.get_by_label('Show Reel in the profile feed',exact=True).uncheck()
        wait_for(lambda:store.load_post(s.sp,p['id'])['variants'][0]['presentation']=={'feed_presence':False},page)
        raw=store.load_post(s.sp,p['id']);package=export(s.sp,p['id'],draft=True,expected_rev=raw['rev'])
        with zipfile.ZipFile(package['zip']) as z:assert b'keep off the profile feed' in z.read('index.html')
        page.goto(server.url()+f'#/grid/{profile["id"]}?surface=feed');page.locator('.planner-feed').wait_for();assert page.locator(f'.gcell[data-post="{p["id"]}"]').count()==0
        page.goto(server.url()+f'#/post/{source["id"]}');old_check=page.get_by_label('Show Reel in the profile feed',exact=True);old_check.wait_for();assert not old_check.is_checked()
        assert 'unspecified' in page.locator('.composer-delivery').inner_text()
        assert store.load_post(s.sp,source['id'])['variants'][0]==legacy and not errors,errors
    finally:page.close()


@pytest.mark.parametrize('later_alt',[False,True])
def test_gapped_legacy_undo_restores_inheritance_and_follows_shared(tmp_path,later_alt):
    s=build(tmp_path);p=carousel(s);vid=p['variants'][0]['id']
    p['media'][0]['slot']=3;p['media'][1]['slot']=0
    p=store.save_post(s.sp,p,expected_rev=p['rev'],actor=AGENT,op='synthetic.legacy-gaps')
    before=projected(s,p,vid)
    p=action(s,p,vid,'move',item='legacy_1',before='legacy_0')
    change={'post':p['id'],'variant':vid,'operation':'move','before':before,'after':projected(s,p,vid),'inherited':True}
    if later_alt:p=action(s,p,vid,'alt',item='legacy_0',alt='Later channel metadata')
    p=action(s,p,vid,'undo',change=change)
    assert (p['variants'][0]['media'] is None)==(not later_alt)
    assert [m['slot'] for m in p['media']]==[3,0]  # Shared legacy representation is never rewritten.
    assert [m['id'] for m in projected(s,p,vid)]==['legacy_0','legacy_1']
    p=s.edit_post(p['id'],{'media.1.alt':'Later shared metadata'},expected_rev=p['rev'])
    saved=store.load_post(s.sp,p['id'])
    assert projected(s,saved,vid)[0]['alt']==('Later channel metadata' if later_alt else 'Later shared metadata')
    assert (saved['variants'][0]['media'] is None)==(not later_alt)


@pytest.mark.parametrize('target',['content','cover'])
def test_existing_focal_keyboard_pointer_undo_keeps_saved_snapshot(rig,browser,target):
    s,server=rig
    p=next(p for p in store.list_posts(s.sp) if any(m['role']=='video' for m in p['media']))
    vid=p['variants'][0]['id'];iid='legacy_1' if target=='cover' else 'legacy_0'
    p=action(s,p,None,'crop',item=iid,crop={'v':1,'x':.1,'y':.6,'aspect':'3:4','purpose':'preview'})
    page=browser.new_page(viewport={'width':1024,'height':768});requests=[]
    page.on('request',lambda r:requests.append(r.post_data_json) if r.url.endswith('/media') else None)
    def saved_item():return next(m for m in projected(s,store.load_post(s.sp,p['id']),vid) if m['id']==iid)
    def settled():
        revision=store.load_post(s.sp,p['id'])['rev']
        page.wait_for_function('(rev)=>document.querySelector(".ed-sub")?.textContent===`Draft revision ${rev}` && document.querySelector(".ed-save")?.textContent==="Saved"',arg=revision)
    def expand(selector):
        if not page.locator(selector).evaluate('(d)=>d.open'):page.locator(selector+' > summary').click()
    try:
        page.goto(server.url()+f'#/post/{p["id"]}');page.locator('.composer-stage video').wait_for()
        for method in ('keyboard','pointer'):
            if target=='cover':expand('.composer-cover')
            else:expand('.composer-slide-edit');expand('.composer-focal')
            slider=page.get_by_label('Cover horizontal focal position' if target=='cover' else 'Horizontal focal position',exact=True)
            assert slider.input_value()=='0.1'
            if method=='keyboard':slider.focus();slider.press('End');expected=1
            else:
                slider.scroll_into_view_if_needed();box=slider.bounding_box();page.mouse.click(box['x']+box['width']*.75,box['y']+box['height']/2)
                expected=float(slider.input_value());assert .6<expected<.9
            wait_for(lambda:saved_item()['crop']['x']==expected,page);settled()
            assert saved_item()['crop']['y']==.6
            expand('.composer-slide-edit');rev=store.load_post(s.sp,p['id'])['rev']
            page.get_by_role('button',name='Undo media change',exact=True).click()
            wait_for(lambda:store.load_post(s.sp,p['id'])['rev']>rev,page);settled()
            undo=next(r for r in reversed(requests) if r['op']=='undo')['change']
            assert next(m for m in undo['before'] if m['id']==iid)['crop']['x']==.1
            assert next(m for m in undo['after'] if m['id']==iid)['crop']['x']==expected
            assert saved_item()['crop']=={'v':1,'x':.1,'y':.6,'aspect':'3:4','purpose':'preview'}
            assert store.load_post(s.sp,p['id'])['variants'][0]['media'] is None
    finally:page.close()


@pytest.mark.parametrize('target',['content','cover'])
def test_rapid_keyboard_focal_axes_wait_for_saved_revision(rig,browser,target):
    s,server=rig;p=next(p for p in store.list_posts(s.sp) if any(m['role']=='video' for m in p['media']))
    vid=p['variants'][0]['id'];iid='legacy_1' if target=='cover' else 'legacy_0'
    p=action(s,p,None,'crop',item=iid,crop={'v':1,'x':.1,'y':.6,'aspect':'3:4','purpose':'preview'})
    page=browser.new_page();requests=[]
    page.add_init_script('''window.holdFocal=true;const realFetch=window.fetch;window.fetch=async(...args)=>{if(window.holdFocal&&String(args[0]).endsWith('/media')){window.holdFocal=false;await new Promise(resolve=>window.releaseFocal=resolve);}return realFetch(...args);};''')
    page.on('request',lambda r:requests.append(r.post_data_json) if r.url.endswith('/media') else None)
    def crop():return next(m['crop'] for m in projected(s,store.load_post(s.sp,p['id']),vid) if m['id']==iid)
    try:
        page.goto(server.url()+f'#/post/{p["id"]}')
        if target=='cover':page.locator('.composer-cover > summary').click()
        else:page.locator('.composer-slide-edit > summary').click();page.locator('.composer-focal > summary').click()
        prefix='Cover ' if target=='cover' else ''
        horizontal=page.get_by_label(prefix+'horizontal focal position' if prefix else 'Horizontal focal position',exact=True)
        vertical=page.get_by_label(prefix+'vertical focal position' if prefix else 'Vertical focal position',exact=True)
        horizontal.focus();horizontal.press('End');page.wait_for_function('()=>typeof window.releaseFocal==="function"')
        vertical.focus();vertical.press('Home')
        assert crop()['x']==.1 and crop()['y']==.6  # Neither input mutated the saved source.
        page.evaluate('window.releaseFocal()')
        wait_for(lambda:crop()['x']==1 and crop()['y']==0,page)
        revision=store.load_post(s.sp,p['id'])['rev'];page.wait_for_function('(rev)=>document.querySelector(".ed-sub")?.textContent===`Draft revision ${rev}` && document.querySelector(".ed-save")?.textContent==="Saved"',arg=revision)
        assert len(requests)==2 and requests[1]['expected_rev']==requests[0]['expected_rev']+1
        if not page.locator('.composer-slide-edit').evaluate('(d)=>d.open'):page.locator('.composer-slide-edit > summary').click()
        page.get_by_role('button',name='Undo media change',exact=True).click();wait_for(lambda:crop()['y']==.6,page)
        assert crop()['x']==1  # Undo only the later vertical operation.
        undo=requests[-1]['change'];assert next(m['crop'] for m in undo['before'] if m['id']==iid)['x']==1
    finally:page.close()


@pytest.mark.parametrize('target',['content','cover'])
def test_focal_third_keyboard_edit_after_redraw_preserves_pending_axis(rig,browser,target):
    s,server=rig;p=next(p for p in store.list_posts(s.sp) if any(m['role']=='video' for m in p['media']))
    vid=p['variants'][0]['id'];iid='legacy_1' if target=='cover' else 'legacy_0'
    p=action(s,p,None,'crop',item=iid,crop={'v':1,'x':.1,'y':.6,'aspect':'3:4','purpose':'preview'})
    page=browser.new_page(viewport={'width':1024,'height':768});requests=[]
    page.add_init_script('''window.focalCount=0;const realFetch=window.fetch;window.fetch=async(...args)=>{if(String(args[0]).endsWith('/media')){const n=++window.focalCount;if(n<=2)await new Promise(resolve=>window['releaseFocal'+n]=resolve);}return realFetch(...args);};''')
    page.on('request',lambda r:requests.append(r.post_data_json) if r.url.endswith('/media') else None)
    def crop():return next(m['crop'] for m in projected(s,store.load_post(s.sp,p['id']),vid) if m['id']==iid)
    def settled():
        revision=store.load_post(s.sp,p['id'])['rev']
        page.wait_for_function('(rev)=>document.querySelector(".ed-sub")?.textContent===`Draft revision ${rev}` && document.querySelector(".ed-save")?.textContent==="Saved"',arg=revision)
    try:
        page.goto(server.url()+f'#/post/{p["id"]}')
        if target=='cover':page.locator('.composer-cover > summary').click()
        else:page.locator('.composer-slide-edit > summary').click();page.locator('.composer-focal > summary').click()
        horizontal=page.get_by_label('Cover horizontal focal position' if target=='cover' else 'Horizontal focal position',exact=True)
        vertical=page.get_by_label('Cover vertical focal position' if target=='cover' else 'Vertical focal position',exact=True)
        horizontal.focus();horizontal.press('End');page.wait_for_function('()=>typeof window.releaseFocal1==="function"')
        vertical.focus();vertical.press('Home');assert crop()['x']==.1 and crop()['y']==.6
        page.evaluate('window.releaseFocal1()');page.wait_for_function('()=>typeof window.releaseFocal2==="function"')
        # This is the intermediate redraw, not the original local slider object.
        redrawn=(horizontal.input_value(),vertical.input_value())
        horizontal.focus();horizontal.press('Home');page.evaluate('window.releaseFocal2()')
        wait_for(lambda:len(requests)==3 and crop()['x']==0,page);settled()
        assert crop()['y']==0 and redrawn==('1','0')
        assert [(r['crop']['x'],r['crop']['y']) for r in requests]==[(1,.6),(1,0),(0,0)]
        assert [r['expected_rev'] for r in requests]==list(range(p['rev'],p['rev']+3))
        assert all(r['item']==iid and r['variant']==vid for r in requests)
        if not page.locator('.composer-slide-edit').evaluate('(d)=>d.open'):page.locator('.composer-slide-edit > summary').click()
        page.get_by_role('button',name='Undo media change',exact=True).click();wait_for(lambda:crop()['x']==1,page);settled()
        assert crop()['y']==0  # Undo only the third operation, never the pending vertical intent.
        change=requests[-1]['change']
        assert next(m['crop'] for m in change['before'] if m['id']==iid)=={'v':1,'x':1,'y':0,'aspect':'3:4','purpose':'preview'}
        assert next(m['crop'] for m in change['after'] if m['id']==iid)=={'v':1,'x':0,'y':0,'aspect':'3:4','purpose':'preview'}
    finally:page.close()
