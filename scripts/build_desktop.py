#!/usr/bin/env python3
"""Build a versioned arm64 offline engineering app from a fresh installed wheel.

Inputs are the independently verified runtime preparations, selected explicitly.
The one download is Sparkle 2 (automatic updates), at a pinned release whose SHA-256 is checked before it is unpacked;
a mismatch stops the build. No credential use, signing identity lookup or app launch occurs.

Updates: a `--release` build needs the Sparkle EdDSA PUBLIC key (`--update-public-key FILE` or AVE_UPDATE_PUBLIC_KEY) and
the private feed address (`--update-feed-url-file FILE` or AVE_UPDATE_FEED_URL), and refuses to run without them. They go
into Info.plist (SUPublicEDKey, SUFeedURL). A development build without them builds with updates switched off and says so.
The feed address is never printed, logged or written to the build records.
"""
from pathlib import Path
import argparse
import base64
import hashlib
import importlib.metadata as md
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib

# Sparkle 2.10.0 (MIT), the official release archive: https://github.com/sparkle-project/Sparkle/releases/tag/2.10.0
SPARKLE_VERSION='2.10.0'
SPARKLE_URL=f'https://github.com/sparkle-project/Sparkle/releases/download/{SPARKLE_VERSION}/Sparkle-{SPARKLE_VERSION}.tar.xz'
SPARKLE_SHA256='c2bf58aa8387266ac179357b1415d6f2635f044da8be41042af32425dae6da0c'
UPDATE_DISABLED='Updates are OFF in this build: no update key and feed were given (development build). Pass --release with both to ship one.'

PINS={'media/ffmpeg':'085c6bcc51871b1ee8ff3fc43b84a6fa4d1fde8095f2cd9b46e9e7b8fa8822e4',
      'media/ffprobe':'0a124f9bb08b1b1de290d9bd43a5afcee9877c53b375b18de0a1f7045446f9e7',
      'speech/fluidaudiocli':'44bf1e1abe44c1dc997920fc69fc3f7a69c3b324d021d937563a65430304859a',
      # Syncthing v2.1.7 official macOS arm64 release binary (MPL-2.0), pre-signing
      'sync/syncthing':'38aab77073e47a717ec59eab082670b4fccf28c256fa0f7935aebb3f4665bb01'}

def sha(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for block in iter(lambda:f.read(1024**2),b''):h.update(block)
    return h.hexdigest()

def manifest(root):
    return {'v':1,'architecture':'arm64','tested_macos':platform.mac_ver()[0],'minimum_upstream_helper':'14.0',
            'qualification':'Engineering offline edition only. Parakeet conversion-card license conflict and historical model conversion provenance unresolved; not distribution clearance.',
            'pre_signing_evidence':PINS,
            'components':{p.name:{'files':{str(f.relative_to(p)):sha(f) for f in sorted(p.rglob('*')) if f.is_file()}} for p in sorted(root.iterdir()) if p.is_dir()}}

def fetch_sparkle(cache,opener=None):
    """The pinned Sparkle release, downloaded once into `cache`, checked against SPARKLE_SHA256 and unpacked fresh.
    Returns the unpacked folder (Sparkle.framework, bin/sign_update, LICENSE). Refuses on any hash mismatch."""
    cache=Path(cache);cache.mkdir(parents=True,exist_ok=True)
    archive=cache/f'Sparkle-{SPARKLE_VERSION}.tar.xz'
    if archive.exists() and sha(archive)!=SPARKLE_SHA256:
        archive.unlink()        # a damaged or replaced cache is fetched again, then checked like a new download
    if not archive.exists():
        if opener is None:
            from urllib.request import urlopen as opener
        fd,tmp=tempfile.mkstemp(dir=cache,suffix='.part');tmp=Path(tmp)
        try:
            with os.fdopen(fd,'wb') as out,opener(SPARKLE_URL,timeout=120) as r:
                shutil.copyfileobj(r,out)
            got=sha(tmp)
            if got!=SPARKLE_SHA256:raise ValueError(f'Sparkle {SPARKLE_VERSION} hash mismatch: expected {SPARKLE_SHA256}, got {got}. Refusing to build with it.')
            os.replace(tmp,archive)
        finally:tmp.unlink(missing_ok=True)
    if sha(archive)!=SPARKLE_SHA256:raise ValueError(f'Sparkle {SPARKLE_VERSION} hash mismatch. Refusing to build with it.')
    unpacked=cache/f'Sparkle-{SPARKLE_VERSION}'
    if unpacked.exists():shutil.rmtree(unpacked)
    unpacked.mkdir()
    subprocess.run(['/usr/bin/tar','-xJf',str(archive),'-C',str(unpacked)],check=True)    # keeps the framework's symlinks
    if not (unpacked/'Sparkle.framework/Versions/B/Sparkle').is_file():raise ValueError('The Sparkle archive has no Sparkle.framework')
    return unpacked

def _read_input(path,env):
    if path:return Path(path).read_text().strip()
    return (os.environ.get(env) or '').strip()

def update_plist(public_key,feed_url,release):
    """Info.plist keys for Sparkle. A release needs both inputs; a development build without them gets updates switched
    off (no keys at all, so Sparkle never starts) and the build says so."""
    if not public_key and not feed_url:
        if release:raise SystemExit('A release build needs the update public key (--update-public-key or AVE_UPDATE_PUBLIC_KEY) and the private feed address (--update-feed-url-file or AVE_UPDATE_FEED_URL).')
        return {}
    if not public_key or not feed_url:raise SystemExit('Give both the update public key and the feed address, or neither (a development build with updates off).')
    try:raw=base64.b64decode(public_key,validate=True)
    except ValueError:raw=b''
    if len(raw)!=32:raise SystemExit('The update public key must be the base64 EdDSA public key that Sparkle generate_keys prints (32 bytes).')
    if not feed_url.startswith('https://') or any(c.isspace() for c in feed_url):raise SystemExit('The update feed address must be one https:// address.')
    return {'SUPublicEDKey':public_key,'SUFeedURL':feed_url,'SUEnableAutomaticChecks':True,'SUScheduledCheckInterval':86400,
            'SUAutomaticallyUpdate':False,'SUAllowsAutomaticUpdates':True,'SURequireSignedFeed':True,'SUVerifyUpdateBeforeExtraction':True}

def bundle_versions(repo,build_number,release):
    version=tomllib.loads((repo/'pyproject.toml').read_text())['project']['version']
    if release and not build_number:raise SystemExit('A release build needs --build-number (CFBundleVersion, higher than every build before it).')
    build=build_number or time.strftime('%Y%m%d')
    if not all(p.isdigit() for p in build.split('.')):raise SystemExit('--build-number is digits and dots, for example 20261110 or 2026.11.10.1')
    return version,build

def copy(source,target):
    if source.is_dir():shutil.copytree(source,target)
    else:target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);parser.add_argument('--build-python',type=Path,required=True)
    parser.add_argument('--ffmpeg-root',type=Path,required=True);parser.add_argument('--speech-root',type=Path,required=True)
    parser.add_argument('--speech-base',type=Path,required=True);parser.add_argument('--browser-root',type=Path,required=True);parser.add_argument('--metadata-root',type=Path,required=True);parser.add_argument('--wheel-cache',type=Path,required=True)
    parser.add_argument('--sync-root',type=Path,required=True,help='prepared Syncthing component: syncthing, LICENSE, SOURCE.md')
    parser.add_argument('--resolve-mcp',type=Path,required=True,help='davinci-resolve-mcp checkout')
    parser.add_argument('--release',action='store_true',help='a build to publish: refuses without the update key, feed and build number')
    parser.add_argument('--update-public-key',type=Path,help='file holding the Sparkle EdDSA public key (or AVE_UPDATE_PUBLIC_KEY)')
    parser.add_argument('--update-feed-url-file',type=Path,help='file holding the private appcast address (or AVE_UPDATE_FEED_URL); never printed')
    parser.add_argument('--build-number',help='CFBundleVersion, compared with the appcast sparkle:version')
    parser.add_argument('--sparkle-cache',type=Path,default=Path.home()/'Library/Caches/AVE/sparkle')
    a=parser.parse_args();repo=Path(__file__).resolve().parents[1];out=a.output.absolute()
    plist=update_plist(_read_input(a.update_public_key,'AVE_UPDATE_PUBLIC_KEY'),_read_input(a.update_feed_url_file,'AVE_UPDATE_FEED_URL'),a.release)
    version,build=bundle_versions(repo,a.build_number,a.release)
    print(f'AVE Studio {version} (build {build}): updates '+('ON, feed configured' if plist else 'OFF'),file=sys.stderr)
    if not plist:print(UPDATE_DISABLED,file=sys.stderr)
    sparkle=fetch_sparkle(a.sparkle_cache)
    out.mkdir(parents=True,exist_ok=False);stage=out/'stage';stage.mkdir();runtime=stage/'runtime';runtime.mkdir()
    copy(a.ffmpeg_root/'relocated-zlib/bin',runtime/'media');copy(a.speech_root/'relocated',runtime/'speech')
    copy(a.speech_base/'components/parakeet-v3',runtime/'asr');copy(a.speech_root/'components',runtime/'diarization')
    copy(a.browser_root/'components',runtime/'browser');copy(a.sync_root,runtime/'sync');copy(repo/'motion-kit/brand/fonts',runtime/'fonts')
    for rel,value in PINS.items():
        if sha(runtime/rel)!=value:raise ValueError('Prepared engine hash mismatch: '+rel)
    notes=stage/'notices';notes.mkdir()
    copy(a.metadata_root,notes/'model-metadata');copy(a.speech_root/'ave-local-diarization.patch',notes/'FluidAudio/ave-local-diarization.patch')
    for name in ('LICENSE','ThirdPartyLicenses'):copy(a.speech_root/'source'/name,notes/'FluidAudio'/name)
    for name in ('preparation.json','asr-regression-verification.json','build.log'):copy(a.speech_root/name,notes/'FluidAudio'/name)
    copy(a.speech_base/'source.tar',notes/'FluidAudio/upstream-source.tar')
    copy(a.ffmpeg_root/'ffmpeg-8.0.3-arm64-source-inputs.tar',notes/'FFmpeg/corresponding-source-inputs.tar')
    copy(a.ffmpeg_root/'build-manifest-zlib.json',notes/'FFmpeg/build-manifest.json')
    # Usable notices are extracted alongside preserved complete source archives.
    for base in [a.ffmpeg_root/'sources',a.ffmpeg_root/'x264']:
        for p in base.rglob('*'):
            if p.is_file() and p.name.lower().startswith(('copying','license','notice','ftl.txt')):
                copy(p,notes/'FFmpeg/notices'/base.name/p.relative_to(base))
    copy(repo/'THIRD_PARTY_NOTICES.md',notes/'project-notices.md')
    for name in ('LICENSE','SOURCE.md'):copy(a.sync_root/name,notes/'Syncthing'/name)
    copy(sparkle/'LICENSE',notes/'Sparkle/LICENSE')
    for p in repo.glob('motion-kit/brand/*'):
        if p.is_file() and ('license' in p.name.lower() or 'ofl' in p.name.lower()):copy(p,notes/'fonts'/p.name)
    # Exact model cards/notices are inside the component inventory too.
    for name in ('diarization-LICENSE','diarization-NOTICE.md','diarization-PROVENANCE.md'):
        copy(a.metadata_root/name,runtime/'diarization/notices'/name)
    copy(a.metadata_root/'parakeet-README.md',runtime/'asr/notices/parakeet-README.md')
    copy(repo/'packaging/macos/MODEL-TERMS.txt',runtime/'asr/notices/MODEL-TERMS.txt')
    copy(repo/'packaging/macos/MODEL-TERMS.txt',notes/'MODEL-TERMS.txt')
    (runtime/'components.json').write_text(json.dumps(manifest(runtime),indent=2))
    helpdir=stage/'help';helpdir.mkdir();copy(repo/'packaging/macos/Help.html',helpdir/'index.html')
    copy(repo/'scripts/desktop_entry.py',stage/'desktop_entry.py')
    bridge=stage/'resolve-mcp';copy(repo/'scripts/resolve_handoff.py',bridge/'resolve_handoff.py')
    for name in ('install_resolve_bridge.py','resolve_bridge_launcher.py','resolve_bridge_probe.py','resolve_capability_probe.py'):copy(a.resolve_mcp/'scripts'/name,bridge/'scripts'/name)
    shutil.copytree(a.resolve_mcp/'src',bridge/'src',ignore=shutil.ignore_patterns('__pycache__'))
    copy(a.resolve_mcp/'LICENSE',notes/'davinci-resolve-mcp/LICENSE')
    # Never analyze the editable checkout: install freshly built wheel first.
    cache=out/'cache';wheel=out/'wheel'
    subprocess.run(['uv','--offline','--cache-dir',str(a.wheel_cache),'build','--wheel','--out-dir',str(wheel)],cwd=repo,check=True)
    wheels=list(wheel.glob('*.whl'));assert len(wheels)==1
    subprocess.run(['uv','--cache-dir',str(cache),'pip','install','--python',str(a.build_python),'--no-deps','--reinstall',str(wheels[0])],check=True)
    # Complete installed dependency metadata + licenses, including renderer closure.
    inventory_code="""import importlib.metadata as m,json,pathlib,shutil,sys
out=pathlib.Path(sys.argv[1]);rows=[]
for d in m.distributions():
 n=d.metadata['Name'];v=d.version; row={'name':n,'version':v,'license':d.metadata.get('License'),'license_expression':d.metadata.get('License-Expression'),'files':[]}
 for f in d.files or []:
  if '.dist-info/' in str(f) and any(x in str(f).lower() for x in ['license','notice','copying','metadata']):
   source=pathlib.Path(d.locate_file(f));target=out/n/v/str(f);target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target);row['files'].append(str(target.relative_to(out)))
 rows.append(row)
(out/'python-dependencies.json').write_text(json.dumps(rows,indent=2))
"""
    subprocess.run([str(a.build_python),'-c',inventory_code,str(notes/'python')],cwd=out,check=True)
    python_provenance="""import sys,sysconfig,json,pathlib,shutil
out=pathlib.Path(sys.argv[1]);root=pathlib.Path(sysconfig.get_path('stdlib'))
shutil.copy2(root/'LICENSE.txt',out/'CPython-LICENSE.txt')
(out/'interpreter.json').write_text(json.dumps({'version':sys.version,'implementation':sys.implementation.name,'base_prefix':sys.base_prefix,'build_python':sys.executable},indent=2))
"""
    subprocess.run([str(a.build_python),'-c',python_provenance,str(notes/'python')],cwd=out,check=True)
    env=dict(os.environ,AVE_DESKTOP_STAGE=str(stage),AVE_RESOLVE_MCP_ROOT=str(a.resolve_mcp.resolve()),PYINSTALLER_CONFIG_DIR=str(out/'pyinstaller-cache'),
             AVE_BUNDLE_VERSION=version,AVE_BUNDLE_BUILD=build,AVE_UPDATE_PLIST=json.dumps(plist))
    env.pop('AVE_UPDATE_FEED_URL',None)        # only the plist carries it into the spec
    with (out/'build.log').open('w') as log:
        subprocess.run([str(a.build_python),'-m','PyInstaller','--clean','--noconfirm','--distpath',str(out/'dist'),'--workpath',str(out/'work'),str(repo/'packaging/macos/ave-studio.spec')],cwd=out,env=env,stdout=log,stderr=subprocess.STDOUT,check=True)
    bundle=out/'dist/AVE Studio.app';data=bundle/'Contents/Resources/runtime'
    # Preserve reviewed helper/browser layouts. Chromium's load-command headers
    # cannot be expanded by PyInstaller's dylib relocation; its own relative
    # closure is already relocatable. Stage it unchanged, then sign in place.
    copy(runtime,data)
    # Sparkle as released (its own symlinked layout); sign_desktop.py signs it inside-out for a release.
    frameworks=bundle/'Contents/Frameworks';frameworks.mkdir(parents=True,exist_ok=True)
    subprocess.run(['/usr/bin/ditto',str(sparkle/'Sparkle.framework'),str(frameworks/'Sparkle.framework')],check=True)
    macho={b'\xcf\xfa\xed\xfe',b'\xfe\xed\xfa\xcf',b'\xca\xfe\xba\xbe',b'\xbe\xba\xfe\xca'}
    for file in sorted(data.rglob('*')):
        if file.is_file():
            with file.open('rb') as inp:magic=inp.read(4)
            if magic in macho:subprocess.run(['/usr/bin/codesign','--force','--sign','-',str(file)],check=True)

    # PyInstaller signs Mach-O inputs: record final component identities and then
    # seal the outer bundle. Do not re-sign nested binaries after this inventory.
    node=bundle/'Contents/Frameworks/playwright/driver/node'
    node_before=sha(node)
    subprocess.run(['/usr/bin/codesign','--force','--sign','-','--entitlements',str(repo/'packaging/macos/node-entitlements.plist'),str(node)],check=True)
    (out/'node-signing.json').write_text(json.dumps({'before_final_sign_sha256':node_before,'after_final_sign_sha256':sha(node),'entitlements':{'com.apple.security.cs.allow-jit':True},'scope':'Playwright Node only; nested runtime hashes recorded after signing, outer bundle sealed last'},indent=2))
    final=manifest(data);(data/'components.json').write_text(json.dumps(final,indent=2))
    driver=node.parent
    (data/'driver-files.json').write_text(json.dumps({str((Path(folder)/name).relative_to(driver)):sha(Path(folder)/name) for folder,dirs,files in os.walk(driver,followlinks=True) for name in files},indent=2))
    copy(repo/'packaging/macos/reproduction',bundle/'Contents/Resources/notices/reproduction')
    subprocess.run(['/usr/bin/codesign','--force','--sign','-',str(bundle)],check=True)
    subprocess.run(['/usr/bin/codesign','--verify','--deep','--strict',str(bundle)],check=True)
    inventory={str(p.relative_to(bundle)):sha(p) for p in sorted(bundle.rglob('*')) if p.is_file() and not p.is_symlink()}
    (out/'bundle-files.json').write_text(json.dumps(inventory,indent=2));(out/'build-identity.json').write_text(json.dumps({'wheel_sha256':sha(wheels[0]),'bundle_inventory_sha256':sha(out/'bundle-files.json'),'component_manifest_sha256':sha(data/'components.json'),'app':str(bundle),'python':str(a.build_python),'version':version,'build':build,'updates':'on' if plist else 'off','sparkle':{'version':SPARKLE_VERSION,'sha256':SPARKLE_SHA256},'inputs':{k:str(v) for k,v in vars(a).items()}},indent=2))
    print(bundle)
if __name__=='__main__':main()
