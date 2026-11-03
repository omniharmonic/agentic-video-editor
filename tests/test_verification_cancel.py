"""Private cancellation never changes hashes, crosses ownership, or leaks scope."""
import contextvars
import hashlib
import os
import threading
import time
from pathlib import Path
import pytest
from ave import _verification_cancel as cancel,editdoc
from ave.assets import licenses
from ave.publish import safety
from ave.episode import Episode


@pytest.mark.parametrize('helper',[safety.sha256_file,licenses.sha256_file])
@pytest.mark.parametrize('at',['before-read','between-blocks','before-return'])
def test_streaming_hash_cancels_without_partial_digest(tmp_path,helper,at):
    path=tmp_path/'bytes.bin';data=b'0123456789abcdef'*(192*1024);path.write_bytes(data);expected=hashlib.sha256(data).hexdigest();checks=0
    # Three1MiB reads, the EOF read, and the final return check.
    threshold={'before-read':2,'between-blocks':4,'before-return':7}[at]
    def cancelled():
        nonlocal checks
        checks+=1;return checks>=threshold
    with pytest.raises(ValueError,match='cancelled'):
        with cancel.scope(cancelled):helper(path)
    assert checks==threshold and cancel._CURRENT.get() is None
    assert helper(path)==expected


def test_cancel_context_preserves_exception_and_cannot_cross_thread_or_pid(tmp_path,monkeypatch):
    path=tmp_path/'bytes';path.write_bytes(b'exact bytes');expected=hashlib.sha256(b'exact bytes').hexdigest();event=threading.Event();results=[]
    with pytest.raises(RuntimeError,match='original failure'):
        with cancel.scope(event.is_set):
            event.set();context=contextvars.copy_context()
            thread=threading.Thread(target=lambda:results.append(context.run(safety.sha256_file,path)));thread.start();thread.join(2)
            assert results==[expected]
            with monkeypatch.context() as m:
                m.setattr(cancel.os,'getpid',lambda:987654321);assert safety.sha256_file(path)==expected
            raise RuntimeError('original failure')
    assert cancel._CURRENT.get() is None and safety.sha256_file(path)==expected


def test_mutation_lock_cancel_is_bounded_and_releases_its_fd(tmp_path,monkeypatch):
    ep=Episode(tmp_path/'episode');ep.create();event=threading.Event();started=threading.Event();errors=[];waiting_fds=[]
    main=threading.get_ident();original_open=os.open
    def opened(path,*args,**kwargs):
        fd=original_open(path,*args,**kwargs)
        if threading.get_ident()!=main and str(path).endswith('.versions.lock'):waiting_fds.append(fd)
        return fd
    monkeypatch.setattr(editdoc.os,'open',opened)
    def waiting():
        started.set()
        try:
            with editdoc.mutation_lock(ep,cancelled=event.is_set):pytest.fail('contended lock entered')
        except ValueError as exc:errors.append(str(exc))
    with editdoc.mutation_lock(ep):
        thread=threading.Thread(target=waiting);thread.start();assert started.wait(1);time.sleep(.1);event.set();thread.join(1)
        assert not thread.is_alive() and errors and 'cancelled' in errors[0]
        assert len(waiting_fds)==1
        with pytest.raises(OSError):os.fstat(waiting_fds[0])
    # Ordinary blocking callers retain the same lock and can enter afterwards.
    with editdoc.mutation_lock(ep):pass
