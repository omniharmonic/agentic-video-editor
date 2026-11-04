# Run only with the wheel installed in an isolated build environment.
import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata
stage = Path(os.environ['AVE_DESKTOP_STAGE'])
entry = str(stage / 'desktop_entry.py')
datas = [(str(stage / 'help'), 'help'), (str(stage / 'notices'), 'notices'), (str(stage / 'resolve-mcp'), 'resolve-mcp')]
binaries = []
hiddenimports = ['AppKit', 'Foundation', 'WebKit', 'LocalAuthentication', 'Vision', 'Quartz', 'CoreML', 'objc'] + collect_submodules('ave.commands') + collect_submodules('ave.studio.cli')
mcp_root = os.environ['AVE_RESOLVE_MCP_ROOT']       # the MCP server runs in this interpreter: analyze it so its imports ship
hiddenimports += ['server'] + collect_submodules('src', filter=lambda n: 'dashboard' not in n and 'control_panel' not in n)
for package in ('keyring', 'jaraco', 'requests', 'urllib3', 'certifi', 'mcp', 'pydantic', 'pydantic_core', 'ave', 'playwright', 'greenlet', 'pyee', 'yaml', 'numpy', 'scipy', 'soundfile', 'PIL', 'webview', 'pypdf'):
    d,b,h = collect_all(package)
    datas += d; binaries += b; hiddenimports += h
for package in ('keyring','requests','agentic-video-editor','playwright','greenlet','pyee','pywebview','pyobjc-core','pyobjc-framework-LocalAuthentication'):
    datas += copy_metadata(package, recursive=True)
a = Analysis([entry], pathex=[mcp_root, os.path.join(mcp_root, 'src')], binaries=binaries, datas=datas, hiddenimports=hiddenimports,
             excludes=['pytest','tkinter','PyQt5','PySide6','matplotlib','IPython','notebook'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='AVE Studio', debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False, target_arch='arm64', codesign_identity='-')
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='AVE Studio')
# Versions and the Sparkle keys come from build_desktop.py. AVE_UPDATE_PLIST is empty for a development build (updates off);
# a release carries SUPublicEDKey and the private SUFeedURL. Never print it.
import json
info_plist = {'CFBundleDisplayName':'AVE Studio','CFBundleShortVersionString':os.environ.get('AVE_BUNDLE_VERSION','0.1.0'),
              'CFBundleVersion':os.environ.get('AVE_BUNDLE_BUILD','20261103'),'LSMinimumSystemVersion':'14.0',
              'NSHighResolutionCapable':True,'NSAppTransportSecurity':{'NSAllowsLocalNetworking':True}}
info_plist.update(json.loads(os.environ.get('AVE_UPDATE_PLIST') or '{}'))
app = BUNDLE(coll, name='AVE Studio.app', bundle_identifier='local.ave.studio', info_plist=info_plist)
