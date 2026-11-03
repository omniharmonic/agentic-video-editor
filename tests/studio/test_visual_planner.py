"""Visual planning and manual ZIP download over synthetic studio data."""
import json
import threading
import zipfile
import pytest

from ave.studio.service import open_studio
from ave.studio.ui.server import StudioServer
from studio_helpers import build_studio


@pytest.fixture(scope='module')
def browser():
    pw=pytest.importorskip('playwright.sync_api')
    with pw.sync_playwright() as p:
        try: b=p.chromium.launch()
        except Exception as e: pytest.skip(f'Chromium unavailable: {e}')
        yield b
        b.close()


@pytest.mark.legacy_ui
@pytest.mark.parametrize('width',[1440,390])
def test_phone_preview_and_original_media_download(tmp_path,browser,width):
    built=build_studio(tmp_path)
    studio=open_studio(built.sp.root,clock=built.clock)
    server=StudioServer(studio,by='synthetic-reviewer',pin='424242')
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    page=browser.new_page(viewport={'width':width,'height':980},accept_downloads=True)
    errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        page.goto(server.url()+'#/phone/ig-main')
        page.get_by_label('Planned Instagram profile',exact=True).wait_for()
        assert page.locator('.ig-profile-tile').count()>0
        page.locator('.ig-profile-tile').first.click()
        page.locator('.ig-post-media img').wait_for()
        assert page.locator('body').evaluate('(el)=>el.scrollWidth<=innerWidth')
        page.screenshot(path=f'/tmp/ave-phone-preview-{width}.png',full_page=True)
        page.get_by_role('button',name='Export files and caption',exact=True).click()
        page.get_by_role('button',name='Download unreviewed draft',exact=True).click()
        history=page.get_by_role('region',name='Package history',exact=True)
        link=history.get_by_role('link',name='Download full-resolution package',exact=True)
        link.wait_for()
        assert page.get_by_role('link',name='Download full-resolution package',exact=True).count()==1
        assert history.locator('.manual-package').count()==1
        assert page.locator('.manual-status .manual-package').count()==0
        page.locator('.manual-dialog').screenshot(path=f'/private/tmp/ave-integration-manual-package-{width}.png')
        with page.expect_download() as download:
            link.click()
        path=tmp_path/'originals.zip';download.value.save_as(path)
        with zipfile.ZipFile(path) as z:
            manifest=json.loads(z.read('manifest.json'))
            assert manifest['reviewed'] is False
            assert manifest['media']
            for media in manifest['media']:
                import hashlib
                from ave.studio import store
                source=built.sp.root/store.read_assets(built.sp)[media['asset']]['file']
                assert z.read(media['filename'])==source.read_bytes()
                assert hashlib.sha256(z.read(media['filename'])).hexdigest()==media['sha256']
            assert 'caption.txt' in z.namelist() and 'index.html' in z.namelist()
        # A second package adds history once, preserving the first exact ZIP action.
        original=link.get_attribute('href')
        page.get_by_role('button',name='Download unreviewed draft',exact=True).click()
        from playwright.sync_api import expect
        expect(history.locator('.manual-package')).to_have_count(2)
        links=history.get_by_role('link',name='Download full-resolution package',exact=True).evaluate_all('(nodes)=>nodes.map(n=>n.getAttribute("href"))')
        assert len(set(links))==2 and original in links
        (tmp_path/'hash-verification.json').write_text(json.dumps({'width':width,'manifest':manifest,'original_bytes_matched':True,'unique_history_links':links},indent=2))
        from pathlib import Path
        Path(f'/private/tmp/ave-integration-package-hashes-{width}.json').write_text((tmp_path/'hash-verification.json').read_text())
        assert not errors
    finally:
        page.close();server.shutdown();worker.join(5);server.server_close()


@pytest.mark.legacy_ui
def test_pipeline_board_and_table_share_real_post_inventory(tmp_path,browser):
    built=build_studio(tmp_path)
    from ave.studio import store
    from ave.studio.events import AGENT
    post = store.load_post(built.sp, next(built.sp.posts_dir.glob('*.json')).stem)
    post['campaign'] = 'c_launch'
    store.save_post(built.sp, post, expected_rev=post['rev'], actor=AGENT, op='campaign-fixture')
    server=StudioServer(open_studio(built.sp.root,clock=built.clock),pin='424242')
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    page=browser.new_page(viewport={'width':1440,'height':980})
    try:
        page.goto(server.url()+'#/pipeline')
        page.locator('.production-table tr[data-post]').first.wait_for()
        page.get_by_role('button',name='Board',exact=True).click()
        page.get_by_label('Content production board',exact=True).wait_for()
        assert page.locator('.production-card').count()>0
        campaign = page.get_by_role('combobox', name='Campaign filter', exact=True)
        assert campaign.locator('option').all_text_contents() == ['All campaigns', 'c_launch']
        campaign.select_option('c_launch')
        assert page.locator('.production-card').count() == 1
        page.get_by_role('button',name='Table',exact=True).click()
        page.locator('.production-table tbody tr[data-post]').first.wait_for()
        assert page.locator('.production-table tbody tr[data-post]').count()>0
        page.get_by_role('searchbox',name='Search pipeline',exact=True).fill('nothing-matches-this')
        page.get_by_text('No content matches these filters.',exact=True).wait_for()
    finally:
        page.close();server.shutdown();worker.join(5);server.server_close()


def test_incomplete_manual_media_is_not_served(tmp_path):
    from studio.test_api import Client
    sp=build_studio(tmp_path).sp
    folder=sp.work/'manual_exports/manual-0123456789abcdef0123456789abcdef'
    folder.mkdir(parents=True);(folder/'.incomplete').write_text('building')
    (folder/'caption.txt').write_text('Not ready yet')
    server=StudioServer(open_studio(sp.root),pin='424242')
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    try:
        c=Client(server)
        path='/files/'+sp.rel(folder/'caption.txt')
        assert c.req('GET',path)[0]==404
    finally:
        server.shutdown();worker.join(5);server.server_close()


def test_numbered_layout_manual_download_uses_its_actual_work_path(tmp_path):
    from studio.test_api import Client
    from ave.studio.project import StudioProject
    sp=StudioProject(tmp_path/'numbered');sp.create(layout='numbered')
    folder=sp.work/'manual_exports/manual-0123456789abcdef0123456789abcdef'
    folder.mkdir(parents=True);(folder/'caption.txt').write_text('A completed handoff')
    server=StudioServer(open_studio(sp.root),pin='424242')
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    try:
        c=Client(server)
        status,headers,body=c.req('GET','/files/'+sp.rel(folder/'caption.txt'))
        assert status==200 and body==b'A completed handoff'
    finally:
        server.shutdown();worker.join(5);server.server_close()


@pytest.mark.legacy_ui
def test_phone_carousel_matches_export_order_and_composed_caption(tmp_path,browser):
    from test_manual_export import make_post
    from ave.studio.project import StudioProject
    from ave.studio import manual_export
    sp=StudioProject(tmp_path/'book-studio');sp.create(title='Book launch studio')
    p=make_post(sp,tmp_path,variant=True)
    v=p['variants'][0]
    manifest=manual_export.describe(sp,p['id'],v['id'],draft=True)['manifest']
    studio=open_studio(sp.root)
    server=StudioServer(studio,pin='424242')
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    page=browser.new_page()
    try:
        page.goto(server.url()+'#/phone/sofia-ig')
        page.locator('.ig-profile-tile').first.click()
        first=page.locator('.ig-post-media img');first.wait_for()
        asset=manifest['media'][0]['sha256']
        assert asset in first.get_attribute('src')
        assert manifest['caption'] in page.locator('.ig-post-caption').inner_text()
        page.get_by_role('button',name='Next slide',exact=True).click()
        assert manifest['media'][1]['sha256'] in page.locator('.ig-post-media img').get_attribute('src')
    finally:
        page.close();server.shutdown();worker.join(5);server.server_close()
