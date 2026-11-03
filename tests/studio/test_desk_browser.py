"""Browser-to-API smoke test over synthetic sources; never operates approval UI."""
import threading
import pytest

from ave.studio.project import StudioProject
from ave.studio.service import open_studio
from ave.studio.ui.server import StudioServer


@pytest.fixture(scope='module')
def browser():
    pw = pytest.importorskip('playwright.sync_api')
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:
            pytest.skip(f'Chromium unavailable: {e}')
        yield b
        b.close()


@pytest.mark.legacy_ui
@pytest.mark.parametrize('width', [1440, 390])
def test_desk_intake_and_navigation(tmp_path, width, browser):
    sp = StudioProject(tmp_path / 'studio')
    sp.create(title='The shared table', tz='America/Denver')
    server = StudioServer(open_studio(sp.root), by='synthetic-user', pin='424242')
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        page = browser.new_page(viewport={'width':width,'height':960})
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto(server.url() + '#/desk')
        page.get_by_role('heading', name='Studio desk', exact=True).wait_for()
        page.get_by_role('textbox', name='Source text', exact=True).fill('Where does enough begin?')
        page.get_by_role('button', name='Add to ideas', exact=True).click()
        page.get_by_role('link', name='Open idea', exact=True).wait_for()
        assert page.get_by_role('link', name='Where does enough begin?', exact=True).count() == 1
        page.get_by_role('combobox', name='Source type', exact=True).select_option('writing')
        page.get_by_role('textbox', name='Source title', exact=True).fill('A grounded practice')
        page.get_by_role('textbox', name='Source text', exact=True).fill('We leave room for one another. There is more than one way to listen.')
        page.get_by_role('button', name='Save source text', exact=True).click()
        page.get_by_text('A grounded practice', exact=True).wait_for()
        assert page.locator('body').evaluate('(el) => el.scrollWidth <= window.innerWidth')
        page.screenshot(path=f'/tmp/ave-studio-desk-{width}.png', full_page=True)
        page.get_by_role('link', name='Open calendar', exact=True).click()
        page.wait_for_selector('#main[data-page=calendar]')
        assert not errors
        page.close()
    finally:
        server.shutdown()
        worker.join(5)
        server.server_close()


@pytest.mark.legacy_ui
def test_disconnected_desk_preserves_unsaved_text(tmp_path, browser):
    sp = StudioProject(tmp_path / 'studio')
    sp.create(title='Offline work')
    server = StudioServer(open_studio(sp.root), pin='424242')
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    page = browser.new_page()
    try:
        page.goto(server.url() + '&poll=200#/desk')
        field = page.get_by_role('textbox', name='Source text', exact=True)
        field.fill('Keep this thought while the server is away.')
        page.route('**/api/studio/changes?*', lambda r: r.abort())
        page.route('**/api/studio/intake', lambda r: r.abort())
        page.locator('#connection').get_by_text('Connection lost.', exact=False).wait_for()
        page.get_by_role('button', name='Add to ideas', exact=True).click()
        page.get_by_text('Could not save.', exact=False).wait_for()
        assert field.input_value() == 'Keep this thought while the server is away.'
        assert page.get_by_role('button', name='Add to ideas', exact=True).is_enabled()
    finally:
        page.close()
        server.shutdown()
        worker.join(5)
        server.server_close()
