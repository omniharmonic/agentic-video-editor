"""Frozen component discovery and integrity: no PATH or developer-cache fallback."""
from __future__ import annotations
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile


REQUIRED_MODULES = ('keyring.backends.macOS', 'requests', 'urllib3', 'cryptography.x509', 'yaml', 'numpy', 'scipy.signal', 'soundfile',
                    'PIL', 'pypdf', 'playwright', 'webview', 'mcp.server.fastmcp', 'Vision', 'LocalAuthentication', 'WebKit')


def frozen(): return bool(getattr(sys, 'frozen', False))

def root():
    if not frozen(): raise RuntimeError('Bundled runtime is available only in the standalone application')
    return Path(sys.executable).parent.parent / 'Resources' / 'runtime'


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''): h.update(block)
    return h.hexdigest()


def verify_component(base, manifest, name):
    """Full inventory including hidden markers; reject links, extras and changed bytes."""
    if name not in manifest['components']: raise ValueError('Unknown component')
    component = manifest['components'][name]
    folder = Path(base) / name
    if folder.is_symlink() or not folder.is_dir(): raise ValueError(f'{name}: component missing; reinstall the verified runtime')
    actual = set()
    for p in folder.rglob('*'):
        if p.is_symlink(): raise ValueError(f'{name}: component symlink refused')
        if p.is_file(): actual.add(p.relative_to(folder).as_posix())
    expected = component['files']
    if actual != set(expected): raise ValueError(f'{name}: incomplete component inventory; reinstall the verified runtime')
    for rel, sha in expected.items():
        if Path(rel).is_absolute() or '..' in Path(rel).parts or digest(folder / rel) != sha:
            raise ValueError(f'{name}: component checksum mismatch; reinstall the verified runtime')
    return folder


def component(name):
    base = root()
    return verify_component(base, json.loads((base / 'components.json').read_text()), name)


def configure():
    """Call before importing application modules in every frozen dispatch."""
    base = root()
    # Override inherited discovery controls; child environments retain AVE_AGENT.
    os.environ['AVE_HOME'] = str(Path(sys._MEIPASS) / 'ave' / '_data')
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(base / 'browser')
    import playwright
    driver = Path(playwright.__file__).parent / 'driver'
    expected = json.loads((base / 'driver-files.json').read_text())
    actual = set()
    boundary = Path(sys.executable).parent.parent.resolve()
    visited = set()
    for folder, dirs, files in os.walk(driver, followlinks=True):
        canonical = Path(folder).resolve()
        if canonical in visited: raise ValueError('Renderer driver contains a directory cycle')
        visited.add(canonical)
        for name in dirs + files:
            candidate = Path(folder) / name
            if not candidate.resolve().is_relative_to(boundary): raise ValueError('Renderer driver escaped the bundle')
        for name in files: actual.add(str((Path(folder) / name).relative_to(driver)))
        if len(actual) > 5000: raise ValueError('Renderer driver inventory is too large')
    if actual != set(expected): raise ValueError('Renderer driver inventory is incomplete')
    for rel, sha in expected.items():
        if digest(driver / rel) != sha: raise ValueError('Renderer driver checksum mismatch')
    os.environ['PLAYWRIGHT_NODEJS_PATH'] = str(driver / 'node')
    os.environ['PATH'] = str(component('media')) + ':/usr/bin:/bin:/usr/sbin:/sbin'


def diagnostics():
    from . import resources
    import platform
    out = {'frozen': frozen(), 'architecture': platform.machine(), 'os': platform.mac_ver()[0],
           'resources': str(resources.home()), 'components': {}, 'platform_credentials_checked': False,
           'distribution': 'Local ad-hoc arm64 engineering build; not notarized. Model terms/provenance remain qualified.'}
    base = root()
    manifest = json.loads((base / 'components.json').read_text())
    for name in manifest['components']:
        try: path = component(name); out['components'][name] = {'ready': True, 'path': str(path)}
        except ValueError as exc: out['components'][name] = {'ready': False, 'problem': str(exc)}
    # Lazy imports are invisible to PyInstaller: a missing one otherwise surfaces only when someone reaches that feature.
    out['modules'] = {}
    for name in REQUIRED_MODULES:
        try: importlib.import_module(name); out['modules'][name] = True
        except Exception as exc: out['modules'][name] = False; out.setdefault('problems', []).append(f'{name}: {exc}')
    out['ok'] = all(c['ready'] for c in out['components'].values()) and all(out['modules'].values())
    return out


def install_component(source, destination, expected, *, cancelled=lambda: False, progress=lambda n, total: None):
    """Offline, explicit native-selected component import; atomic and retryable.

    No URLs, automatic downloads, global caches, overwrite or mutable manifests.
    The expected manifest comes from the signed bundle, never the selected folder.
    """
    source, destination = Path(source), Path(destination)
    name = destination.name
    verify_component(source.parent, {'components': {name: expected}}, name)
    if destination.exists():
        return verify_component(destination.parent, {'components': {name: expected}}, name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    total = sum((source / rel).stat().st_size for rel in expected['files'])
    if shutil.disk_usage(destination.parent).free < total + 64 * 1024**2: raise ValueError('Not enough free disk space for this component')
    stage = Path(tempfile.mkdtemp(prefix='.component-', dir=destination.parent))
    copied = 0
    try:
        target = stage / name
        for rel in expected['files']:
            src, dst = source / rel, target / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            with src.open('rb') as inp, dst.open('xb') as out:
                while block := inp.read(1024**2):
                    if cancelled(): raise ValueError('Component installation cancelled; retry is safe')
                    out.write(block); copied += len(block); progress(copied, total)
                out.flush(); os.fsync(out.fileno())
            dst.chmod(src.stat().st_mode & 0o777)
        verify_component(stage, {'components': {name: expected}}, name)
        if cancelled(): raise ValueError('Component installation cancelled; retry is safe')
        target.rename(destination)
        return destination
    finally: shutil.rmtree(stage)
