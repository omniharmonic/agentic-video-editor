#!/usr/bin/env python3
"""Sign a fresh build_desktop.py app with Developer ID, make the DMG, notarize and staple.

Works on a COPY of the built app: nested Mach-O first, each on its own, then the runtime manifests are
regenerated from the signed bytes, and the outer bundle is sealed last. No `codesign --deep` for signing.
JIT is granted only to the three executables that generate code; library validation stays on.

Sparkle 2 (Contents/Frameworks/Sparkle.framework) is signed the way Sparkle documents for a non-sandboxed app signed outside
Xcode, inside-out: Installer.xpc, Downloader.xpc (keeping its own entitlements), Autoupdate, Updater.app, then the framework,
all with the hardened runtime. `signing_order()` is the whole plan, and the outer app is always last.
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


SPARKLE = 'Contents/Frameworks/Sparkle.framework'


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


def sparkle_plan(bundle):
    """Sparkle's nested items in its documented signing order, then the framework: [(path, preserve_entitlements)]."""
    framework = bundle / SPARKLE
    if not framework.is_dir():
        return []
    b = framework / 'Versions/B'
    rows = [(b / 'XPCServices/Installer.xpc', False), (b / 'XPCServices/Downloader.xpc', True), (b / 'Autoupdate', False),
            (b / 'Updater.app', False)]
    return [(path, keep) for path, keep in rows if path.exists() and not path.is_symlink()] + [(framework, False)]


def signing_order(bundle):
    """Every signature, in order: [(path, kind)], kind one of nested | sparkle | sparkle-keep-entitlements | main | app.
    Ordinary nested code first (deepest first, Sparkle's tree left out), then Sparkle inside-out, the main executable, and
    the outer app last."""
    main_exe = (bundle / 'Contents/MacOS/AVE Studio').resolve()
    framework = (bundle / SPARKLE).resolve()
    def in_sparkle(path):
        real = path.resolve()
        return real == framework or framework in real.parents
    rows = [(r, 'nested') for r in nested(bundle) if r.resolve() != main_exe and not in_sparkle(r)]
    rows += [(path, 'sparkle-keep-entitlements' if keep else 'sparkle') for path, keep in sparkle_plan(bundle)]
    return rows + [(bundle / 'Contents/MacOS/AVE Studio', 'main'), (bundle, 'app')]


def updates_state(app, notarizing, allow_off=False):
    """'on' or 'off' for the receipt. A build headed for notarization (shipping) with updates off is refused unless
    `allow_off`: it would reach people and never update, silently."""
    try:
        info = plistlib.loads((Path(app) / 'Contents/Info.plist').read_bytes())
    except (OSError, ValueError):
        info = {}
    state = 'on' if info.get('SUPublicEDKey') and info.get('SUFeedURL') else 'off'
    if state == 'off' and notarizing and not allow_off:
        raise SystemExit('This app was built without the update key and feed (updates off). Build it with build_desktop.py '
                         '--release, or pass --allow-updates-off to notarize it anyway.')
    return state


def sign(path, identity, entitlements=None, preserve_entitlements=False):
    cmd = ['/usr/bin/codesign', '--force', '--sign', identity, '--timestamp', '--options', 'runtime']
    if entitlements:
        cmd += ['--entitlements', str(entitlements)]
    if preserve_entitlements:
        cmd += ['--preserve-metadata=entitlements']
    subprocess.run(cmd + [str(path)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def notarize(path, profile, out, key):
    """Submit `path` to the notary service and wait. Returns the receipt row; raises SystemExit unless Accepted."""
    run = subprocess.run(['/usr/bin/xcrun', 'notarytool', 'submit', str(path), '--keychain-profile', profile,
                          '--wait', '--output-format', 'json'], capture_output=True, text=True)
    result = json.loads(run.stdout or '{}')
    row = {k: result.get(k) for k in ('id', 'status', 'message')}
    if result.get('id'):
        log = subprocess.run(['/usr/bin/xcrun', 'notarytool', 'log', result['id'], '--keychain-profile', profile],
                             capture_output=True, text=True)
        (out / f'notary-log-{key}.json').write_text(log.stdout)
    if result.get('status') != 'Accepted':
        raise NotaryRefused(key, row, run.stderr[:300])
    return row


class NotaryRefused(SystemExit):
    def __init__(self, key, row, stderr):
        super().__init__(f'Notarization of the {key} was not accepted: {row} {stderr}')
        self.key, self.row = key, row


def staple(path):
    subprocess.run(['/usr/bin/xcrun', 'stapler', 'staple', str(path)], check=True)
    subprocess.run(['/usr/bin/xcrun', 'stapler', 'validate', str(path)], check=True)


def package(bundle, out, version, identity, profile, receipt):
    """Notarize and staple the app first (Apple: staple the app, then pack it, so the app opens offline once dragged out of the
    DMG), then make, sign, notarize and staple the DMG. Without a profile, stop after the unsigned-by-Apple DMG."""
    if profile:
        zipped = out / 'AVE Studio.app.zip'
        subprocess.run(['/usr/bin/ditto', '-c', '-k', '--keepParent', str(bundle), str(zipped)], check=True)
        try:
            receipt['notary_app'] = notarize(zipped, profile, out, 'app')
        except NotaryRefused as e:
            receipt['notary_app'] = e.row
            (out / 'release.json').write_text(json.dumps(receipt, indent=2))
            raise
        staple(bundle)
        zipped.unlink()
    dmg = out / f'AVE-Studio-{version}-arm64.dmg'
    with tempfile.TemporaryDirectory(dir=out) as stage:
        subprocess.run(['/usr/bin/ditto', str(bundle), str(Path(stage) / 'AVE Studio.app')], check=True)
        os.symlink('/Applications', Path(stage) / 'Applications')
        subprocess.run(['/usr/bin/hdiutil', 'create', '-volname', 'AVE Studio', '-srcfolder', stage, '-ov', '-format', 'UDZO', str(dmg)],
                       check=True, stdout=subprocess.DEVNULL)
    subprocess.run(['/usr/bin/codesign', '--force', '--sign', identity, '--timestamp', str(dmg)], check=True)
    if profile:
        try:
            receipt['notary'] = notarize(dmg, profile, out, 'dmg')
        except NotaryRefused as e:
            receipt['notary'] = e.row
            receipt['dmg'] = str(dmg)
            (out / 'release.json').write_text(json.dumps(receipt, indent=2))
            raise
        staple(dmg)
    return dmg


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--app', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--identity', required=True, help='Developer ID Application certificate fingerprint')
    p.add_argument('--notary-profile', help='saved notarytool profile; omit to stop before submitting')
    p.add_argument('--version', required=True)
    p.add_argument('--allow-updates-off', action='store_true', help='notarize a build that cannot update itself')
    a = p.parse_args()
    update_state = updates_state(a.app, bool(a.notary_profile), a.allow_updates_off)
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
    order = signing_order(bundle)
    rows = [path for path, kind in order if kind == 'nested']
    for row in rows:
        sign(row, a.identity, ent if row.resolve() in jit else None)
    for path, kind in order:
        if kind.startswith('sparkle'):
            sign(path, a.identity, preserve_entitlements=kind == 'sparkle-keep-entitlements')
    final = manifest(data)
    (data / 'components.json').write_text(json.dumps(final, indent=2))
    driver = node.parent
    (data / 'driver-files.json').write_text(json.dumps(
        {str((Path(d) / n).relative_to(driver)): sha(Path(d) / n) for d, _, files in os.walk(driver, followlinks=True) for n in files}, indent=2))
    sign(main_exe, a.identity, ent)
    sign(bundle, a.identity, ent)
    subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', '--verbose=1', str(bundle)], check=True)

    receipt = {'identity': a.identity, 'version': a.version, 'updates': update_state, 'nested_signed': len(rows),
               'sparkle_signed': [str(p.relative_to(bundle)) for p, k in order if k.startswith('sparkle')]}
    dmg = package(bundle, out, a.version, a.identity, a.notary_profile, receipt)
    receipt['dmg'] = str(dmg)
    receipt['dmg_sha256'] = sha(dmg)
    (out / 'release.json').write_text(json.dumps(receipt, indent=2))
    print(json.dumps(receipt, indent=2))


if __name__ == '__main__':
    main()
