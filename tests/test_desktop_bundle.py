"""Run against an explicitly selected frozen bundle; ordinary suite tests sources."""
from pathlib import Path
import json
import os
import subprocess
import pytest


def test_packaging_entry_never_analyzes_checkout():
    root=Path(__file__).resolve().parents[1]
    spec=(root/'packaging/macos/ave-studio.spec').read_text()
    assert "pathex=[mcp_root, os.path.join(mcp_root, 'src')]" in spec and "target_arch='arm64'" in spec
    assert "'LocalAuthentication'" in spec
    assert "copy(runtime,data)" in (root/'scripts/build_desktop.py').read_text()
    source=(root/'src/ave/desktop_worker.py').read_text()
    assert "os.environ['AVE_AGENT'] = '1'" in source
    assert '--mcp' in source and '--production-worker' in source


def test_actual_frozen_diagnostics_and_mcp(tmp_path):
    executable=os.environ.get('AVE_TEST_BUNDLE_EXECUTABLE')
    if not executable:pytest.skip('Set AVE_TEST_BUNDLE_EXECUTABLE to the actual standalone binary')
    env=dict(os.environ,PATH='/usr/bin:/bin',XDG_DATA_HOME=str(tmp_path/'data'),XDG_CONFIG_HOME=str(tmp_path/'config'))
    result=subprocess.run([executable,'--diagnostics-json',str(tmp_path/'diagnostics.json')],cwd=tmp_path,env=env,capture_output=True,timeout=120)
    assert result.returncode==0,result.stderr
    data=json.loads((tmp_path/'diagnostics.json').read_text())
    assert data['frozen'] and data['ok'] and data['architecture']=='arm64'
    assert 'AVE Studio.app' in data['resources'] and '/src/' not in data['resources']
    from ave.studio.project import StudioProject
    sp=StudioProject(tmp_path/'studio');sp.create()
    request={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'test','version':'1'}}}
    result=subprocess.run([executable,'--mcp','--studio',str(sp.root)],input=json.dumps(request)+'\n',text=True,capture_output=True,env=env,cwd=tmp_path,timeout=60)
    assert result.returncode==0,result.stderr
    reply=json.loads(result.stdout)
    assert reply['result']['serverInfo']['name']=='ave-studio'


def test_sync_component_is_pinned_required_and_checked(tmp_path, monkeypatch, capsys):
    import sys
    root=Path(__file__).resolve().parents[1]
    build=(root/'scripts/build_desktop.py').read_text()
    assert "'sync/syncthing':'38aab77073e47a717ec59eab082670b4fccf28c256fa0f7935aebb3f4665bb01'" in build
    assert "'--sync-root',type=Path,required=True" in build and "runtime/'sync'" in build
    from ave import desktop_runtime, desktop_worker
    assert 'sync' in desktop_runtime.REQUIRED_COMPONENTS
    stub=tmp_path/'syncthing'
    stub.write_text('#!/bin/sh\necho "syncthing v0.0.0 stub"\n'); stub.chmod(0o755)
    monkeypatch.setenv('AVE_SYNCTHING',str(stub))
    assert desktop_worker.dispatch(['--sync-check'])==0
    assert 'syncthing v0.0.0 stub' in capsys.readouterr().out
    monkeypatch.setenv('AVE_SYNCTHING',str(tmp_path/'absent'))
    assert desktop_worker.dispatch(['--sync-check'])==1


def _scripts():
    import sys
    root=Path(__file__).resolve().parents[1]
    sys.path.insert(0,str(root/'scripts'))
    import build_desktop,sign_desktop
    return build_desktop,sign_desktop


def _macho(path):
    path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'\xcf\xfa\xed\xfe'+b'\0'*60)


def test_sparkle_is_signed_inside_out_before_the_framework_and_the_framework_before_the_app(tmp_path):
    _,sign=_scripts()
    app=tmp_path/'AVE Studio.app';c=app/'Contents'
    _macho(c/'MacOS/AVE Studio');_macho(c/'Frameworks/libpython3.12.dylib')
    fw=c/'Frameworks/Sparkle.framework';b=fw/'Versions/B'
    for rel in ('Sparkle','Autoupdate','Updater.app/Contents/MacOS/Updater','XPCServices/Installer.xpc/Contents/MacOS/Installer',
                'XPCServices/Downloader.xpc/Contents/MacOS/Downloader'):_macho(b/rel)
    (fw/'Versions/Current').symlink_to('B')
    for name in ('Sparkle','Autoupdate','Updater.app','XPCServices'):(fw/name).symlink_to('Versions/Current/'+name)
    order=sign.signing_order(app)
    names=[str(p.relative_to(tmp_path)) for p,_ in order];kinds=dict((str(p.relative_to(tmp_path)),k) for p,k in order)
    at=lambda rel:names.index('AVE Studio.app/Contents/Frameworks/Sparkle.framework'+rel)
    assert [at('/Versions/B/XPCServices/Installer.xpc'),at('/Versions/B/XPCServices/Downloader.xpc'),at('/Versions/B/Autoupdate'),
            at('/Versions/B/Updater.app'),at('')]==sorted([at('/Versions/B/XPCServices/Installer.xpc'),at('/Versions/B/XPCServices/Downloader.xpc'),
            at('/Versions/B/Autoupdate'),at('/Versions/B/Updater.app'),at('')])
    assert at('')<names.index('AVE Studio.app/Contents/MacOS/AVE Studio')<names.index('AVE Studio.app')==len(names)-1
    assert kinds['AVE Studio.app/Contents/Frameworks/Sparkle.framework/Versions/B/XPCServices/Downloader.xpc']=='sparkle-keep-entitlements'
    # Nothing inside Sparkle is signed on its own by the generic pass (that would break its bundles' seals).
    assert not [n for n,k in kinds.items() if k=='nested' and 'Sparkle.framework' in n]
    assert 'AVE Studio.app/Contents/Frameworks/libpython3.12.dylib' in names


def test_sparkle_is_fetched_at_its_pinned_release_and_refused_on_a_hash_mismatch(tmp_path):
    import io,pytest
    build,_=_scripts()
    assert build.SPARKLE_VERSION=='2.10.0' and build.SPARKLE_URL.endswith('/2.10.0/Sparkle-2.10.0.tar.xz')
    assert build.SPARKLE_SHA256=='c2bf58aa8387266ac179357b1415d6f2635f044da8be41042af32425dae6da0c'
    asked=[]
    def opener(url,timeout):asked.append(url);return io.BytesIO(b'a different archive')
    with pytest.raises(ValueError,match='hash mismatch'):build.fetch_sparkle(tmp_path,opener)
    assert asked==[build.SPARKLE_URL] and list(tmp_path.iterdir())==[]           # nothing kept, nothing unpacked
    (tmp_path/'Sparkle-2.10.0.tar.xz').write_bytes(b'tampered cache')
    with pytest.raises(ValueError,match='hash mismatch'):build.fetch_sparkle(tmp_path,opener)
    assert not (tmp_path/'Sparkle-2.10.0.tar.xz').exists()


def test_a_release_build_refuses_without_the_update_key_and_a_dev_build_turns_updates_off():
    import pytest
    build,_=_scripts()
    with pytest.raises(SystemExit,match='release build needs'):build.update_plist('','',True)
    assert build.update_plist('','',False)=={}
    with pytest.raises(SystemExit,match='both'):build.update_plist('A'*43+'=','',False)
    with pytest.raises(SystemExit,match='32 bytes'):build.update_plist('bm90IGEga2V5','https://u.example.test/x/appcast.xml',True)
    with pytest.raises(SystemExit,match='https'):build.update_plist('A'*43+'=','http://u.example.test/appcast.xml',True)
    keys=build.update_plist('A'*43+'=','https://u.example.test/x/appcast.xml',True)
    assert keys['SUPublicEDKey']=='A'*43+'=' and keys['SUFeedURL'].startswith('https://') and keys['SURequireSignedFeed']
    root=Path(__file__).resolve().parents[1]
    with pytest.raises(SystemExit,match='build-number'):build.bundle_versions(root,None,True)
    assert build.bundle_versions(root,'20261110',True)[1]=='20261110'
    spec=(root/'packaging/macos/ave-studio.spec').read_text()
    assert "info_plist.update(json.loads(os.environ.get('AVE_UPDATE_PLIST') or '{}'))" in spec
    source=(root/'scripts/build_desktop.py').read_text()
    assert "fetch_sparkle(a.sparkle_cache)" in source and "'Sparkle.framework'" in source and "env.pop('AVE_UPDATE_FEED_URL',None)" in source


def test_a_build_with_updates_off_is_not_notarized_by_accident(tmp_path):
    import plistlib,pytest
    _,sign=_scripts()
    app=tmp_path/'AVE Studio.app';(app/'Contents').mkdir(parents=True)
    (app/'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleVersion':'1'}))
    with pytest.raises(SystemExit,match='updates off'):sign.updates_state(app,True)
    assert sign.updates_state(app,True,allow_off=True)=='off' and sign.updates_state(app,False)=='off'
    (app/'Contents/Info.plist').write_bytes(plistlib.dumps({'SUPublicEDKey':'k','SUFeedURL':'https://u.example.test/x/appcast.xml'}))
    assert sign.updates_state(app,True)=='on'


def test_both_the_app_and_the_dmg_are_notarized_and_stapled_and_the_app_before_it_is_packed(tmp_path,monkeypatch):
    import json
    _,sign=_scripts()
    app=tmp_path/'AVE Studio.app';app.mkdir();calls=[]
    def fake(cmd,**kw):
        calls.append([str(c) for c in cmd])
        if 'notarytool' in cmd and 'submit' in cmd:
            return type('R',(),{'stdout':json.dumps({'id':'abc','status':'Accepted'}),'stderr':'','returncode':0})()
        if cmd[0].endswith('hdiutil'):Path(cmd[-1]).write_bytes(b'dmg')
        if '-c' in cmd and cmd[0].endswith('ditto'):Path(cmd[-1]).write_bytes(b'zip')
        return type('R',(),{'stdout':'','stderr':'','returncode':0})()
    monkeypatch.setattr(sign.subprocess,'run',fake)
    receipt={}
    dmg=sign.package(app,tmp_path,'0.4.0','ID','prof',receipt)
    staples=[c for c in calls if 'staple' in c]
    assert [Path(c[-1]).name for c in staples]==['AVE Studio.app','AVE-Studio-0.4.0-arm64.dmg']
    packed=next(i for i,c in enumerate(calls) if c[0].endswith('hdiutil'))
    assert calls.index(staples[0])<packed<calls.index(staples[1])
    assert [Path(c[-1]).name for c in calls if 'validate' in c]==['AVE Studio.app','AVE-Studio-0.4.0-arm64.dmg']
    assert receipt['notary_app']['status']=='Accepted' and receipt['notary']['status']=='Accepted' and dmg.name.endswith('.dmg')
    calls.clear()
    sign.package(app,tmp_path,'0.4.0','ID',None,{})                 # no profile: nothing is submitted or stapled
    assert not [c for c in calls if 'notarytool' in c or 'staple' in c]
