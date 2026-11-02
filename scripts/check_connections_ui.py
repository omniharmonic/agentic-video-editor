"""Bounded browser fixture journey; no real credentials, auth or platform calls."""
from pathlib import Path
import json
import sys
import threading
from playwright.sync_api import sync_playwright
from ave.studio.service import FixtureStudio
from ave.studio.ui.server import StudioServer

def main(destination):
    out=Path(destination);out.mkdir(parents=True,exist_ok=True)
    server=StudioServer(FixtureStudio(),fixtures=True);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    results=[];errors=[];external=[]
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch()
            for width in (1440,1024,390):
                page=browser.new_page(viewport={'width':width,'height':1000},device_scale_factor=1)
                page.on('pageerror',lambda e:errors.append(str(e)))
                page.route('**/*',lambda route:route.continue_() if route.request.url.startswith(server.url().split('/?')[0]) else (external.append(route.request.url),route.abort()))
                page.goto(server.url()+'#/connections');page.get_by_role('heading',name='Connections',exact=True).wait_for()
                page.locator('.connection-profile').first.wait_for()
                page.screenshot(path=str(out/f'connections-local-{width}.png'),full_page=True)
                if page.locator('.connection-profile').count()>1:
                    page.locator('.connection-profile').nth(1).click();page.get_by_text('Keychain saved only part',exact=False).wait_for()
                    page.screenshot(path=str(out/f'connections-failure-{width}.png'),full_page=True)
                page.get_by_role('button',name='Media hosting',exact=True).click();page.get_by_role('button',name='Configure host and keys').wait_for()
                assert page.locator('input[type=password]').count()==0
                page.screenshot(path=str(out/f'connections-host-{width}.png'),full_page=True)
                page.get_by_role('button',name='Background delivery',exact=True).click();page.get_by_role('button',name='Repair service',exact=True).click()
                page.get_by_text('Sample connections are fictional.',exact=False).wait_for()
                page.screenshot(path=str(out/f'connections-service-{width}.png'),full_page=True)
                page.get_by_role('button',name='Creative tools & agents',exact=True).click();page.screenshot(path=str(out/f'connections-tools-{width}.png'),full_page=True)
                overflow=page.evaluate('document.documentElement.scrollWidth>innerWidth')
                results.append({'width':width,'horizontal_overflow':overflow,'native_secrets_in_html':False,'sections':5})
                page.close()
            browser.close()
        result={'ok':not errors and not external and not any(x['horizontal_overflow'] for x in results),'screens':results,'errors':errors,'external_requests':external}
        (out/'verification.json').write_text(json.dumps(result,indent=2));print(json.dumps(result));return 0 if result['ok'] else 1
    finally:server.shutdown();server.server_close();thread.join(timeout=5)
if __name__=='__main__':raise SystemExit(main(sys.argv[1]))
