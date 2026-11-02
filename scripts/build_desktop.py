#!/usr/bin/env python3
"""Build a versioned arm64 offline engineering app from a fresh installed wheel.

Inputs are the independently verified runtime preparations, selected explicitly.
No downloading, credential use, signing identity lookup or app launch occurs.
"""
from pathlib import Path
import argparse
import hashlib
import importlib.metadata as md
import json
import os
import platform
import shutil
import subprocess
import sys

PINS={'media/ffmpeg':'085c6bcc51871b1ee8ff3fc43b84a6fa4d1fde8095f2cd9b46e9e7b8fa8822e4',
      'media/ffprobe':'0a124f9bb08b1b1de290d9bd43a5afcee9877c53b375b18de0a1f7045446f9e7',
      'speech/fluidaudiocli':'44bf1e1abe44c1dc997920fc69fc3f7a69c3b324d021d937563a65430304859a'}

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

def copy(source,target):
    if source.is_dir():shutil.copytree(source,target)
    else:target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);parser.add_argument('--build-python',type=Path,required=True)
    parser.add_argument('--ffmpeg-root',type=Path,required=True);parser.add_argument('--speech-root',type=Path,required=True)
    parser.add_argument('--speech-base',type=Path,required=True);parser.add_argument('--browser-root',type=Path,required=True);parser.add_argument('--metadata-root',type=Path,required=True);parser.add_argument('--wheel-cache',type=Path,required=True)
    parser.add_argument('--resolve-mcp',type=Path,required=True,help='davinci-resolve-mcp checkout')
    a=parser.parse_args();repo=Path(__file__).resolve().parents[1];out=a.output.absolute()
    out.mkdir(parents=True,exist_ok=False);stage=out/'stage';stage.mkdir();runtime=stage/'runtime';runtime.mkdir()
    copy(a.ffmpeg_root/'relocated-zlib/bin',runtime/'media');copy(a.speech_root/'relocated',runtime/'speech')
    copy(a.speech_base/'components/parakeet-v3',runtime/'asr');copy(a.speech_root/'components',runtime/'diarization')
    copy(a.browser_root/'components',runtime/'browser');copy(repo/'motion-kit/brand/fonts',runtime/'fonts')
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
    for name in ('install_resolve_bridge.py','resolve_bridge_probe.py','resolve_capability_probe.py'):copy(a.resolve_mcp/'scripts'/name,bridge/'scripts'/name)
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
    env=dict(os.environ,AVE_DESKTOP_STAGE=str(stage),AVE_RESOLVE_MCP_ROOT=str(a.resolve_mcp.resolve()),PYINSTALLER_CONFIG_DIR=str(out/'pyinstaller-cache'))
    with (out/'build.log').open('w') as log:
        subprocess.run([str(a.build_python),'-m','PyInstaller','--clean','--noconfirm','--distpath',str(out/'dist'),'--workpath',str(out/'work'),str(repo/'packaging/macos/ave-studio.spec')],cwd=out,env=env,stdout=log,stderr=subprocess.STDOUT,check=True)
    bundle=out/'dist/AVE Studio.app';data=bundle/'Contents/Resources/runtime'
    # Preserve reviewed helper/browser layouts. Chromium's load-command headers
    # cannot be expanded by PyInstaller's dylib relocation; its own relative
    # closure is already relocatable. Stage it unchanged, then sign in place.
    copy(runtime,data)
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
    (out/'bundle-files.json').write_text(json.dumps(inventory,indent=2));(out/'build-identity.json').write_text(json.dumps({'wheel_sha256':sha(wheels[0]),'bundle_inventory_sha256':sha(out/'bundle-files.json'),'component_manifest_sha256':sha(data/'components.json'),'app':str(bundle),'python':str(a.build_python),'inputs':{k:str(v) for k,v in vars(a).items()}},indent=2))
    print(bundle)
if __name__=='__main__':main()
