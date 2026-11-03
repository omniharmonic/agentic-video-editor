"""Same-volume atomic publication that never replaces a destination, on APFS and on exFAT."""
from __future__ import annotations
import ctypes
import errno
import os
import stat
import sys
import time

CLAIM_STALE_S=300


def publish(source, destination):
    """Move owned staging bytes atomically; EEXIST never replaces a destination.

    This is the same no-replace primitive used by workbench directory commits.
    Filesystems without either primitive publish through an exclusive claim. Callers own staging cleanup,
    file fsync and any transaction or cancellation barrier before publication.
    """
    libc=ctypes.CDLL(None,use_errno=True)
    if sys.platform=='darwin':
        fn=libc.renamex_np;fn.argtypes=[ctypes.c_char_p,ctypes.c_char_p,ctypes.c_uint]
        result=fn(os.fsencode(source),os.fsencode(destination),4)  # RENAME_EXCL
    elif sys.platform.startswith('linux') and hasattr(libc,'renameat2'):
        fn=libc.renameat2;fn.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]
        result=fn(-100,os.fsencode(source),-100,os.fsencode(destination),1)  # RENAME_NOREPLACE
    else:
        return _publish_file_link(source,destination)
    if result:
        error=ctypes.get_errno()
        if error in (errno.ENOSYS,errno.ENOTSUP,errno.EINVAL):return _publish_file_link(source,destination)
        raise OSError(error,os.strerror(error),str(destination))


def _publish_file_link(source,destination):
    """Retain the pre-existing file-only platform guarantee when supported."""
    if not stat.S_ISREG(os.stat(source,follow_symlinks=False).st_mode):return _publish_with_claim(source,destination)
    try:os.link(source,destination)  # Atomic EEXIST refusal; no partially visible copy.
    except OSError as exc:
        if exc.errno in (errno.ENOTSUP,errno.ENOSYS,errno.EINVAL,errno.EPERM):return _publish_with_claim(source,destination)
        raise
    os.unlink(source)


def _publish_with_claim(source,destination):
    """exFAT and similar: an O_EXCL sidecar claims the name, then a plain rename publishes it whole.

    Every writer takes the same claim, so a rename can never replace a published file. A claim left by a crash goes stale.
    """
    destination=os.fspath(destination);claim=os.path.join(os.path.dirname(destination),'.'+os.path.basename(destination)+'.claim')
    for _ in range(2):
        try:fd=os.open(claim,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        except FileExistsError:
            if time.time()-os.stat(claim).st_mtime<=CLAIM_STALE_S:raise
            os.unlink(claim);continue
        os.close(fd);break
    else:raise FileExistsError(errno.EEXIST,os.strerror(errno.EEXIST),claim)
    try:
        if os.path.lexists(destination):raise FileExistsError(errno.EEXIST,os.strerror(errno.EEXIST),destination)
        os.rename(source,destination)
    finally:os.unlink(claim)
