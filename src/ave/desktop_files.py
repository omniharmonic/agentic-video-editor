"""Native-selected files; these functions are not remotely exposed."""
from __future__ import annotations
from .atomic import publish as publish_atomic
import hashlib
import os
import uuid
from pathlib import Path
import shutil
import stat
import tempfile
import threading
import time
from .desktop_runtime import digest


LIBRARY_LIMIT = 2 * 1024 ** 3      # the library hook reads the whole file into memory


def import_library(studio, selected):
    """Copy chosen files into the Library. The hook takes ownership of what it is given, so it only ever sees a staging copy."""
    from .library import hook_upload
    staging = studio.sp.work / 'native-intake'
    staging.mkdir(parents=True, exist_ok=True)
    added = []
    for path in selected:
        path = Path(path)
        if path.is_symlink() or not path.is_file(): raise ValueError('Select regular files, not links: ' + path.name)
        if path.stat().st_size > LIBRARY_LIMIT: raise ValueError(path.name + ' is too large for the Library. Open its project folder from Sources instead.')
        copy = staging / (uuid.uuid4().hex + path.suffix)
        try:
            shutil.copyfile(path, copy)
            result = hook_upload(studio, path.name, copy)
            added.append({'name': path.name, 'asset': result['asset'], 'duplicate': bool(result.get('duplicate'))})
        finally:
            copy.unlink(missing_ok=True)
    return added


def import_selected(workbench, selected, *, cancelled=lambda: False, progress=lambda n, total: None):
    """Stream selected originals into private staging then existing durable registry.

    A cancelled staging copy is removed. Already registered sources survive a
    later cancellation and duplicate retries retain the same immutable identity.
    """
    from .studio.desk import _safe_path
    directory = workbench.sp.work / 'native-intake'
    _safe_path(directory, workbench.sp.root)
    directory.mkdir(parents=True, exist_ok=True)
    results = []
    for selected_path in selected:
        path = Path(selected_path)
        if path.is_symlink() or not path.is_file(): raise ValueError('Select regular media files, not links')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        staging = None
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= 1024**4: raise ValueError('Choose a nonempty file smaller than 1 TiB')
            # Staging, immutable store, managed episode copy and extraction headroom.
            if shutil.disk_usage(directory).free < before.st_size * 3 + 256 * 1024**2: raise ValueError('Not enough free disk space to import this recording safely')
            handle, name = tempfile.mkstemp(prefix='.import-', dir=directory)
            staging = Path(name); h = hashlib.sha256(); count = 0
            with os.fdopen(handle, 'wb') as out:
                while block := os.read(fd, 1024**2):
                    if cancelled(): raise ValueError('Import cancelled; originals are unchanged. Select the files again to retry.')
                    out.write(block); h.update(block); count += len(block); progress(count, before.st_size)
                out.flush(); os.fsync(out.fileno())
            after = os.fstat(fd)
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino) or count != before.st_size:
                raise ValueError('Original changed during import; select it again after recording has finished')
            if cancelled(): raise ValueError('Import cancelled; originals are unchanged')
            results.append(workbench._register_staged(staging, path.name, count, h.hexdigest()))
            # Workbench owns one process at a time. Preserve that ordering for a
            # native multi-file camera/audio selection.
            for thread in list(workbench._threads): thread.join()
        finally:
            os.close(fd)
            if staging is not None: staging.unlink(missing_ok=True)
    return results


def checked_digest(path, *, cancelled=lambda:False, progress=lambda n,total:None):
    h=hashlib.sha256();n=0;total=Path(path).stat().st_size
    with Path(path).open('rb') as stream:
        while block:=stream.read(1024**2):
            if cancelled(): raise ValueError('File operation cancelled')
            h.update(block);n+=len(block);progress(n,total)
    if cancelled(): raise ValueError('File operation cancelled')
    return h.hexdigest()


class NativeFileWork:
    """One owned cancellable file task; UI callbacks always use main dispatch.

    This in-process owner is not a protocol/job store or authority capability.
    Shutdown cancels and joins I/O, and queued callbacks ignore a closed owner.
    """
    def __init__(self, dispatch):
        self.dispatch=dispatch;self.closed=False;self.busy=False
        self.critical=False
        self.cancelled=threading.Event();self.thread=None

    def start(self, operation, complete, progress=lambda n,total:None, *, critical=False, committed_on_return=False):
        # Save returns only after its exclusive destination link commits. Its
        # own pre-commit checks decide cancellation; a later request must not
        # replace the retained file's successful result with a cancellation.
        # Preparation has no commit and remains cancellable after computation.
        if self.closed or self.busy: raise ValueError('A native file operation is already running or closed')
        self.cancelled.clear();self.busy=True;self.critical=critical
        def deliver(fn):
            self.dispatch(lambda:None if self.closed else fn())
        def run():
            last=[0.]
            def report(n,total):
                now=time.monotonic()
                if now-last[0]>=.1 or n==total:
                    last[0]=now;deliver(lambda:progress(n,total))
            try:
                result=operation(self.cancelled.is_set,report)
                if not critical and not committed_on_return and self.cancelled.is_set():raise ValueError('File operation cancelled')
                error=None
            except Exception as exc:result=None;error=str(exc)
            def finished():
                self.busy=False;self.critical=False;complete(result,error)
            deliver(finished)
        self.thread=threading.Thread(target=run,name='AVE native file operation',daemon=True)
        self.thread.start()

    def cancel(self):
        if not self.critical:self.cancelled.set()

    def close(self):
        self.closed=True;self.cancel()
        if self.thread:self.thread.join()
        self.busy=False


def package_file(sp, package_id, filename=None, *, cancelled=lambda:False, progress=lambda n,total:None):
    from .studio.manual_export import load_package, _safe
    manifest = load_package(sp, package_id, cancelled=cancelled, progress=progress)
    folder = _safe(sp.work / 'manual_exports' / package_id, sp.root)
    if filename is None: source = folder.with_suffix('.zip')
    else:
        allowed = {m['filename'] for m in manifest['media']}
        if manifest.get('cover'): allowed.add(manifest['cover']['filename'])
        if filename not in allowed: raise ValueError('Only the exact packaged originals can be shared')
        source = _safe(folder / filename, folder)
    return source, checked_digest(source,cancelled=cancelled,progress=progress), manifest


def save_verified(source, destination, expected_sha, *, cancelled=lambda: False, progress=lambda n,total:None):
    """Save-panel destination only; never accept one from HTTP or JavaScript."""
    source, destination = Path(source), Path(destination)
    if destination.is_symlink(): raise ValueError('Cannot save through a symbolic link')
    if destination.exists(): raise ValueError('Choose a new filename; an existing file will not be replaced')
    if shutil.disk_usage(destination.parent).free < source.stat().st_size + 1024**2: raise ValueError('Not enough free disk space')
    fd, name = tempfile.mkstemp(prefix='.ave-save-', dir=destination.parent)
    temporary = Path(name)
    try:
        size=source.stat().st_size;n=0
        with source.open('rb') as inp, os.fdopen(fd, 'wb') as out:
            while block := inp.read(1024**2):
                if cancelled(): raise ValueError('Save cancelled')
                out.write(block);n+=len(block);progress(n,size*2)
            out.flush(); os.fsync(out.fileno())
        if checked_digest(temporary,cancelled=cancelled,progress=lambda n,total:progress(size+n,size*2)) != expected_sha: raise ValueError('Package changed during save; prepare it again')
        if cancelled(): raise ValueError('Save cancelled')
        # Atomic no-replace publication consumes the owned staging name.
        publish_atomic(temporary, destination)
        return destination
    finally: temporary.unlink(missing_ok=True)
