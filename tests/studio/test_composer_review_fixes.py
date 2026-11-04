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


@pytest.mark.legacy_ui
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


@pytest.mark.legacy_ui
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


@pytest.mark.legacy_ui
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


@pytest.mark.legacy_ui
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


@pytest.mark.legacy_ui
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


@pytest.mark.legacy_ui
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
