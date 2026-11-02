"""Reproduce the patched ASR-only helper from an authenticated extracted source.

The source archive and source/build receipts ship with notices/FluidAudio.
A supplied SwiftPM cache may contain the declared but unused NeMo artifact; do
not copy that cache into runtime payloads. Network access is a build decision,
never an inference fallback. Source must match the pinned archive before patch.
"""
from pathlib import Path
import argparse, hashlib, json, shutil, subprocess
p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--cache',type=Path,required=True)
a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
patch=Path(__file__).with_name('ave-local-diarization.patch')
assert hashlib.sha256(patch.read_bytes()).hexdigest()=='0efe1c8c8b57822bf50c9e5d1094c9d984a669779a955af8295d1bea8a9ccf2f'
source=a.output/'source';shutil.copytree(a.source,source,ignore=shutil.ignore_patterns('.build','.swiftpm'))
subprocess.run(['/usr/bin/patch','-p1','-i',str(patch.resolve())],cwd=source,check=True)
argv=['/usr/bin/swift','build','--configuration','release','--product','fluidaudiocli','--disable-default-traits','--jobs','4','--scratch-path',str(a.output/'build'),'--cache-path',str(a.cache)]
with (a.output/'build.log').open('w') as log:subprocess.run(argv,cwd=source,stdout=log,stderr=subprocess.STDOUT,check=True)
(a.output/'reproduction.json').write_text(json.dumps({'base_commit':'20d4f0bd46d11d7f50a6eb4f7835cfdbd2b4ba14','patch':hashlib.sha256(patch.read_bytes()).hexdigest(),'command':argv,'swift':subprocess.check_output(['/usr/bin/swift','--version'],text=True)},indent=2))
