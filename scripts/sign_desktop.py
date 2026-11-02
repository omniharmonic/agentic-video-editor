#!/usr/bin/env python3
"""Sign a fresh build_desktop.py app with Developer ID, make the DMG, notarize and staple.

Works on a COPY of the built app: nested Mach-O first, each on its own, then the runtime manifests are
regenerated from the signed bytes, and the outer bundle is sealed last. No `codesign --deep` for signing.
JIT is granted only to the three executables that generate code; library validation stays on.
"""
from pathlib import Path
import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).parent))
from build_desktop import manifest, sha

MACHO = {b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca'}
JIT = {'com.apple.security.cs.allow-jit': True}


def is_macho(path):
    with path.open('rb') as f:
        return f.read(4) in MACHO


def nested(bundle):
    """Every Mach-O file and nested bundle inside the app, deepest first, each real path once."""
    seen, rows = set(), []
    for path in bundle.rglob('*'):
        if path.is_symlink() or path == bundle:
            continue
        if (path.is_file() and is_macho(path)) or (path.is_dir() and path.suffix in ('.app', '.framework')):
            real = path.resolve()
            if real not in seen:
                seen.add(real)
                rows.append(path)
    return sorted(rows, key=lambda p: -len(p.parts))


def sign(path, identity, entitlements=None):
    cmd = ['/usr/bin/codesign', '--force', '--sign', identity, '--timestamp', '--options', 'runtime']
    if entitlements:
        cmd += ['--entitlements', str(entitlements)]
    subprocess.run(cmd + [str(path)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--app', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--identity', required=True, help='Developer ID Application certificate fingerprint')
    p.add_argument('--notary-profile', help='saved notarytool profile; omit to stop before submitting')
    p.add_argument('--version', required=True)
    a = p.parse_args()
    out = a.output.absolute()
    out.mkdir(parents=True, exist_ok=False)
    bundle = out / 'AVE Studio.app'
    subprocess.run(['/usr/bin/ditto', str(a.app), str(bundle)], check=True)
    ent = out / 'jit.plist'
    ent.write_bytes(plistlib.dumps(JIT))

    main_exe = bundle / 'Contents/MacOS/AVE Studio'
    data = bundle / 'Contents/Resources/runtime'
    node = bundle / 'Contents/Frameworks/playwright/driver/node'
    jit = {main_exe.resolve(), node.resolve()} | {f.resolve() for f in data.rglob('chrome-headless-shell') if f.is_file()}
    rows = [r for r in nested(bundle) if r.resolve() != main_exe.resolve()]
    for row in rows:
        sign(row, a.identity, ent if row.resolve() in jit else None)
    final = manifest(data)
    (data / 'components.json').write_text(json.dumps(final, indent=2))
    driver = node.parent
    (data / 'driver-files.json').write_text(json.dumps(
        {str((Path(d) / n).relative_to(driver)): sha(Path(d) / n) for d, _, files in os.walk(driver, followlinks=True) for n in files}, indent=2))
    sign(main_exe, a.identity, ent)
    sign(bundle, a.identity, ent)
    subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', '--verbose=1', str(bundle)], check=True)

    dmg = out / f'AVE-Studio-{a.version}-arm64.dmg'
    with tempfile.TemporaryDirectory(dir=out) as stage:
        subprocess.run(['/usr/bin/ditto', str(bundle), str(Path(stage) / 'AVE Studio.app')], check=True)
        os.symlink('/Applications', Path(stage) / 'Applications')
        subprocess.run(['/usr/bin/hdiutil', 'create', '-volname', 'AVE Studio', '-srcfolder', stage, '-ov', '-format', 'UDZO', str(dmg)],
                       check=True, stdout=subprocess.DEVNULL)
    subprocess.run(['/usr/bin/codesign', '--force', '--sign', a.identity, '--timestamp', str(dmg)], check=True)

    receipt = {'identity': a.identity, 'version': a.version, 'nested_signed': len(rows), 'dmg': str(dmg)}
    if a.notary_profile:
        run = subprocess.run(['/usr/bin/xcrun', 'notarytool', 'submit', str(dmg), '--keychain-profile', a.notary_profile,
                              '--wait', '--output-format', 'json'], capture_output=True, text=True)
        result = json.loads(run.stdout or '{}')
        receipt['notary'] = {k: result.get(k) for k in ('id', 'status', 'message')}
        if result.get('id'):
            log = subprocess.run(['/usr/bin/xcrun', 'notarytool', 'log', result['id'], '--keychain-profile', a.notary_profile],
                                 capture_output=True, text=True)
            (out / 'notary-log.json').write_text(log.stdout)
        if result.get('status') != 'Accepted':
            (out / 'release.json').write_text(json.dumps(receipt, indent=2))
            raise SystemExit(f"Notarization was not accepted: {receipt['notary']} {run.stderr[:300]}")
        subprocess.run(['/usr/bin/xcrun', 'stapler', 'staple', str(dmg)], check=True)
        subprocess.run(['/usr/bin/xcrun', 'stapler', 'validate', str(dmg)], check=True)
    receipt['dmg_sha256'] = sha(dmg)
    (out / 'release.json').write_text(json.dumps(receipt, indent=2))
    print(json.dumps(receipt, indent=2))


if __name__ == '__main__':
    main()
