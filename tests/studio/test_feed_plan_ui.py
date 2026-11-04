"""Whole-image mouse/touch gestures against the real disk-backed studio server."""
import threading
from pathlib import Path

import pytest

from ave.studio import feed_plan
from ave.studio.project import StudioProject
from ave.studio.service import open_studio
from ave.studio.ui.server import StudioServer
from studio_helpers import add_asset
from .test_feed_plan import authoritative_bytes


@pytest.fixture(scope='module')
def browser():
    pw = pytest.importorskip('playwright.sync_api')
    with pw.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:
            pytest.skip(f'Chromium unavailable: {e}')
        yield browser
        browser.close()


@pytest.fixture
def planning(tmp_path):
    from PIL import Image, ImageDraw
    sp = StudioProject(tmp_path/'visual-plan'); sp.create(title='Field Notes')
    studio = open_studio(sp.root)
    titles = ['The quiet morning', 'At the studio', 'A slower afternoon', 'Gathering around the table', 'The work in progress', 'Back outside']
    palette = [(203,179,143), (86,117,101), (180,138,115), (214,209,190), (69,91,105), (179,188,160)]
    for i, (title, color) in enumerate(zip(titles,palette)):
        image = Image.new('RGB',(600,800),color)
        draw = ImageDraw.Draw(image)
        draw.rectangle((0,510,600,800), fill=tuple(max(0,x-35) for x in color))
        draw.ellipse((120+i*15,140,440,590),fill=tuple(min(255,x+40) for x in color))
        draw.rectangle((40,60,90,370),fill=(238,231,211))
        path = sp.work/f'art-{i}.jpg';image.save(path)
        asset = add_asset(sp,path,alt=title)
        studio.create_post(idea=title,copy={'base':title+'\nA moment from the everyday.'},media=[{'slot':0,'asset':asset['id'],'role':'image','alt':title}])
    server = StudioServer(studio,pin='424242')
    thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    yield studio, server
    server.shutdown();thread.join(5);server.server_close()


def open_page(browser, server, width=1360, touch=False):
    context = browser.new_context(viewport={'width':width,'height':1000},has_touch=touch)
    page = context.new_page()
    page.goto(server.url()+'#/grid/local')
    page.locator('.planner-tile').first.wait_for()
    return context, page


def tile_keys(page):
    return page.locator('.planner-tile').evaluate_all('(tiles)=>tiles.map(x=>x.dataset.key)')


def center(page,index):
    box=page.locator('.planner-tile .planner-image').nth(index).bounding_box()
    return box['x']+box['width']/2, box['y']+box['height']/2


def mouse_move(page,source,target,escape=False):
    x,y=center(page,source);tx,ty=center(page,target)
    page.mouse.move(x,y);page.mouse.down();page.mouse.move(tx,ty,steps=15)
    page.locator('.dnd-ghost').wait_for()
    if escape: page.keyboard.press('Escape')
    page.mouse.up()


def touch_move(page,source,target):
    x,y=center(page,source);tx,ty=center(page,target)
    cdp=page.context.new_cdp_session(page)
    cdp.send('Input.dispatchTouchEvent',{'type':'touchStart','touchPoints':[{'x':x,'y':y,'id':1}]})
    for i in range(1,13):
        cdp.send('Input.dispatchTouchEvent',{'type':'touchMove','touchPoints':[{'x':x+(tx-x)*i/12,'y':y+(ty-y)*i/12,'id':1}]})
    page.locator('.dnd-ghost').wait_for()
    cdp.send('Input.dispatchTouchEvent',{'type':'touchEnd','touchPoints':[]})
    cdp.detach()


@pytest.mark.legacy_ui
@pytest.mark.parametrize('width,touch',[(1360,False),(390,True)])
def test_whole_image_drag_saves_real_plan_and_phone_uses_same_order(planning,browser,width,touch):
    studio,server=planning
    context,page=open_page(browser,server,width,touch)
    errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    before=authoritative_bytes(studio.sp)
    original=tile_keys(page)
    expected=original[1:3]+original[:1]+original[3:]
    try:
        if touch: page.get_by_role('button',name='Arrange',exact=True).click()
        with page.expect_response(lambda r:'/api/studio/feed-plan' in r.url and r.request.method=='POST') as response:
            (touch_move if touch else mouse_move)(page,0,2)
        assert response.value.status==200
        page.get_by_role('status').filter(has_text='All changes saved').wait_for()
        assert tile_keys(page)==expected
        assert [c['key'] for c in feed_plan.read(studio)['cells']]==expected
        assert page.url.endswith('#/grid/local')  # Dragging did not click through to the editor.
        page.reload();page.locator('.planner-tile').first.wait_for()
        assert tile_keys(page)==expected
        assert page.locator('body').evaluate('(el)=>el.scrollWidth<=innerWidth')
        assert not page.locator('.feed-planner').evaluate('(el)=>[...el.childNodes].some(n=>n.nodeType===3 && /null|undefined/.test(n.textContent))')
        page.screenshot(path=f'/tmp/ave-feed-planner-{width}.png',full_page=True)
        assert page.locator('#main').get_by_role('link',name='Phone preview',exact=True).get_attribute('href')=='/app/#/workspace'   # the phone preview is the Workspace's Previews now
        assert authoritative_bytes(studio.sp)==before
        assert not errors
    finally:
        context.close()


@pytest.mark.legacy_ui
def test_keyboard_move_and_escape_preserve_state(planning,browser):
    studio,server=planning
    context,page=open_page(browser,server)
    try:
        original=tile_keys(page)
        mouse_move(page,0,2,escape=True)
        assert tile_keys(page)==original and feed_plan.read(studio)['rev']==0
        page.locator('.planner-tile .g-link').first.focus()
        page.keyboard.press('m');page.keyboard.press('ArrowRight');page.keyboard.press('Enter')
        page.get_by_role('status').filter(has_text='All changes saved').wait_for()
        assert tile_keys(page)==original[1:2]+original[:1]+original[2:]
        page.reload();page.locator('.planner-tile').first.wait_for()
        assert feed_plan.read(studio)['rev']==1
        # A visible non-drag alternative has the same saved insertion behavior.
        page.get_by_label('Move selected post to position').select_option('5')
        page.get_by_role('button',name='Move',exact=True).click()
        page.get_by_role('status').filter(has_text='All changes saved').wait_for()
        assert feed_plan.read(studio)['rev']==2
    finally:
        context.close()


@pytest.mark.legacy_ui
def test_conflicting_second_window_reload_is_truthful(planning,browser):
    studio,server=planning
    context,first=open_page(browser,server)
    second=context.new_page();second.goto(server.url()+'#/grid/local');second.locator('.planner-tile').first.wait_for()
    try:
        mouse_move(first,0,2)
        first.get_by_role('status').filter(has_text='All changes saved').wait_for()
        expected=tile_keys(first)
        with second.expect_response(lambda r:'/api/studio/feed-plan' in r.url and r.request.method=='POST') as response:
            mouse_move(second,1,2)
        assert response.value.status==409
        second.get_by_role('alert').filter(has_text='changed in another window').wait_for()
        second.get_by_role('status').filter(has_text='Latest order loaded').wait_for()
        assert tile_keys(second)==expected
        assert feed_plan.read(studio)['rev']==1
    finally:
        context.close()


@pytest.mark.legacy_ui
def test_touch_browsing_scrolls_until_arrange_is_enabled(planning,browser):
    studio,server=planning
    context,page=open_page(browser,server,390,True)
    page.set_viewport_size({'width':390,'height':700})
    try:
        assert page.locator('.planner-tile').first.evaluate('(el)=>getComputedStyle(el).touchAction')=='pan-y'
        page.locator('.planner-tile').first.scroll_into_view_if_needed()
        x,y=center(page,3)
        y=min(y,650)
        start=page.evaluate('scrollY')
        cdp=context.new_cdp_session(page)
        cdp.send('Input.dispatchTouchEvent',{'type':'touchStart','touchPoints':[{'x':x,'y':y,'id':1}]})
        for i in range(1,11):
            cdp.send('Input.dispatchTouchEvent',{'type':'touchMove','touchPoints':[{'x':x,'y':y-20*i,'id':1}]})
        cdp.send('Input.dispatchTouchEvent',{'type':'touchEnd','touchPoints':[]})
        page.wait_for_timeout(200)
        assert page.evaluate('scrollY')>start
        assert feed_plan.read(studio)['rev']==0
        page.get_by_role('button',name='Arrange',exact=True).click()
        assert page.locator('.planner-tile').first.evaluate('(el)=>getComputedStyle(el).touchAction')=='none'
        cdp.detach()
    finally:
        context.close()


@pytest.mark.legacy_ui
def test_seventy_five_drafts_remain_reachable_and_selection_survives_reload(planning,browser):
    from ave.studio import store,model
    from ave.studio.events import AGENT
    studio,server=planning
    for i in range(69):
        store.save_post(studio.sp,model.new_post(idea=f'Additional draft {i}',post_id=f'p_bulk{i:03d}'),expected_rev=None,actor=AGENT,op='create')
    context,page=open_page(browser,server)
    try:
        assert page.locator('.planner-tile').count()==75
        last=page.locator('.planner-tile').last
        last.locator('.g-link').click()
        selected=last.get_attribute('data-key')
        page.get_by_role('button',name='Close composer',exact=True).click()
        page.get_by_label('Move selected post to position').select_option('0')
        page.get_by_role('button',name='Move',exact=True).click()
        page.get_by_role('status').filter(has_text='All changes saved').wait_for()
        assert tile_keys(page)[0]==selected
        page.reload();page.locator('.planner-tile').first.wait_for()
        assert page.locator('.planner-tile').count()==75
        assert page.locator('.planner-tile.is-selected').get_attribute('data-key')==selected
        assert tile_keys(page)[0]==selected
    finally:
        context.close()


@pytest.mark.legacy_ui
def test_reel_video_preview_and_stories_are_separate_from_posts(planning,browser):
    import shutil,subprocess
    from ave.library import add_file
    from studio_helpers import write_account
    studio,server=planning
    write_account(studio.sp,'ig-visual','instagram')
    video=studio.sp.work/'synthetic-video.mp4'
    subprocess.run([shutil.which('ffmpeg'),'-v','error','-f','lavfi','-i','color=c=0x708a76:s=180x320:d=1','-c:v','libx264','-pix_fmt','yuv420p',str(video)],check=True)
    asset=add_file(studio.sp,video,source={'type':'own'},by='synthetic-test',alt='A green video',attested=False)
    reel=studio.create_post(idea='A real video draft',media=[{'slot':0,'asset':asset['id'],'role':'video','alt':'A green video'}])
    studio.add_variant(reel['id'],account='ig-visual',platform='instagram',post_type='reel',expected_rev=reel['rev'])
    story=studio.create_post(idea='A story draft')
    studio.add_variant(story['id'],account='ig-visual',platform='instagram',post_type='story',expected_rev=story['rev'])
    context,page=open_page(browser,server)
    try:
        assert page.locator('.planner-tile').count()==6
        page.get_by_label('Content surface',exact=True).select_option('reel')
        poster=page.locator('.planner-image img');poster.wait_for()
        assert page.locator('.planner-tile').count()==1
        page.wait_for_function("()=>document.querySelector('.planner-image img')?.naturalWidth>0")
        # Existing image-cover metadata wins over the video frame, without
        # inventing another carousel slide or modifying the media itself.
        from ave.studio import store
        from ave.studio.events import AGENT
        cover = next(a for a in store.read_assets(studio.sp).values() if a['kind'] == 'image')
        updated = store.load_post(studio.sp, reel['id'])
        updated['media'].append({'slot':1,'asset':cover['id'],'role':'cover','alt':'Cover image'})
        store.save_post(studio.sp,updated,expected_rev=updated['rev'],actor=AGENT,op='test-cover')
        page.reload();page.locator('.planner-image img').wait_for()
        assert page.locator('.planner-image video').count()==0
        cell = next(c for c in feed_plan.read(studio)['cells'] if c['post'] == reel['id'])
        assert cell['media_count']==1 and cover['id'] in cell['thumb']
        page.get_by_label('Content surface',exact=True).select_option('story')
        page.get_by_role('heading',name='A story draft',exact=True).wait_for()
        assert page.locator('.planner-tile').count()==1
        page.get_by_label('Content surface',exact=True).select_option('all')
        page.get_by_text('8 of 8 posts',exact=True).wait_for()
        assert page.locator('.planner-tile').count()==8
    finally:
        context.close()


@pytest.mark.legacy_ui
def test_keyboard_navigation_scrolls_focused_tile_into_view_without_refresh_jumps(planning,browser):
    from ave.studio import store, model
    from ave.studio.events import AGENT
    studio,server=planning
    for i in range(24):
        store.save_post(studio.sp, model.new_post(idea=f'Keyboard draft {i}',post_id=f'p_keyboard{i:03d}'), expected_rev=None,actor=AGENT,op='create')
    context,page=open_page(browser,server)
    page.set_viewport_size({'width':1360,'height':800})
    try:
        page.locator('.planner-tile .g-link').first.focus()
        for _ in range(5):
            page.keyboard.press('ArrowDown')
        active=page.locator('.planner-tile:focus-within')
        assert active.get_attribute('data-index')=='15'
        box=active.bounding_box()
        assert box['y'] >= -1 and box['y']+box['height'] <= 801
        assert page.evaluate('scrollY')>0
        position=page.evaluate('scrollY')
        post=store.load_post(studio.sp,active.get_attribute('data-post'))
        post['idea']['text']='Updated while keyboard focus remains in place'
        store.save_post(studio.sp,post,expected_rev=post['rev'],actor=AGENT,op='edit')
        page.locator('.planner-inspector h2').filter(has_text='Updated while keyboard focus remains in place').wait_for()
        assert abs(page.evaluate('scrollY')-position)<=1
        assert page.locator('.planner-tile:focus-within').get_attribute('data-post')==post['id']
    finally:
        context.close()
