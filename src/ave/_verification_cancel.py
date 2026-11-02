"""Private cooperative cancellation for one native confirmation thread.

This carries no authority or verified data. Default hashing is unchanged, and a
copied context in another thread/process cannot cancel unrelated operations.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import os
import threading

_CURRENT = ContextVar('ave_native_verification_cancel', default=None)


def check():
    current = _CURRENT.get()
    if current and current[:2] == (os.getpid(), threading.get_ident()) and current[2]():
        raise ValueError('Native verification cancelled; no new checkpoint was recorded')


@contextmanager
def scope(cancelled):
    if cancelled is None:
        yield
        return
    token = _CURRENT.set((os.getpid(), threading.get_ident(), cancelled))
    try:
        check()
        yield
    finally:
        # Do not check cancellation here: preserve an existing exception and a
        # successfully committed result if cancellation arrived after append.
        _CURRENT.reset(token)
