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

