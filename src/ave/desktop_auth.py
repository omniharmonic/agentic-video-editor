"""Contextual macOS device-owner authentication. No HTTP/JS/CLI authority flag.

A successful device-owner check grants nothing session-wide. It mints one single-use grant bound to one action and its
targets (`request(..., action=, keys=)`), handed only to the native code that asked. That code spends it once, on its own
thread, with `use(grant, action, keys)`; inside that block, and only there, `require(action, keys)` passes. A request on any
other thread (an HTTP route called with the room token, for example) holds no grant and is refused, even while the person is
still reading the PIN sheet. A grant for one action never authorises another, and it cannot be used twice.
"""
from __future__ import annotations
import secrets
import threading
import time
from contextlib import contextmanager
from .humanpresence import agent_markers_set

GRANT_S = 180                      # a grant not spent within this long is gone (the PIN sheet stays open at most this long)


class Grant:
    """One device-owner check, for one action on exactly these targets. Truthy, so `complete(ok)` callers keep working."""
    __slots__ = ('nonce', 'action', 'keys', 'expires')

    def __init__(self, action, keys):
        self.nonce = secrets.token_hex(16)
        self.action = action
        self.keys = frozenset(keys)
        self.expires = time.monotonic() + GRANT_S

    def __bool__(self):
        return True

    def __repr__(self):
        return f'Grant({self.action!r}, {len(self.keys)} targets)'


def _refused(message='Use the native Review menu to authenticate for this workspace.'):
    from .studio.ui.api import StudioError
    return StudioError(403, message, 'native.authentication_required')


class NativeAuthority:
    """Per-workspace, process-only capability. A fresh OS callback is its only source of grants."""
    binds = True                       # StudioApp passes the act and its targets to require()

    def __init__(self):
        self.closed = False
        self._generation = 0
        self._context = None
        self._timer = None
        self._pending = None           # the callback of a check still waiting on macOS, told "no" if another check replaces it
        self._grants = {}
        self._lock = threading.Lock()
        self._held = threading.local()

    @property
    def active(self):
        """True only on the thread that is spending a grant right now. There is no session-wide authority."""
        return getattr(self._held, 'grant', None) is not None and not self.closed and not agent_markers_set()

    def request(self, reason, complete, *, action='review', keys=()):
        """Ask macOS for the device owner. `complete(grant)` on success, `complete(False)` otherwise (also when a later
        check replaces this one, so no caller is left waiting)."""
        from Foundation import NSOperationQueue
        from LocalAuthentication import LAContext, LAPolicyDeviceOwnerAuthentication
        if self.closed or agent_markers_set():
            complete(False); return
        self.cancel()
        generation = self._generation
        context = LAContext.alloc().init()
        self._context = context
        ok, error = context.canEvaluatePolicy_error_(LAPolicyDeviceOwnerAuthentication, None)
        if not ok:
            self.cancel(); complete(False); return
        self._pending = complete
        def finish(success):
            def deliver():
                if self.closed or generation != self._generation: return
                if self._timer: self._timer.cancel()
                self._generation += 1
                self._context = None
                self._pending = None
                context.invalidate()
                complete(self._mint(action, keys) if success and not agent_markers_set() else False)
            NSOperationQueue.mainQueue().addOperationWithBlock_(deliver)
        self._timer = threading.Timer(60, lambda: finish(False))
        self._timer.daemon = True
        self._timer.start()
        context.evaluatePolicy_localizedReason_reply_(LAPolicyDeviceOwnerAuthentication, reason, lambda success, error: finish(success))

    def _mint(self, action, keys):
        g = Grant(action, keys)
        with self._lock:
            now = time.monotonic()
            self._grants = {n: x for n, x in self._grants.items() if x.expires > now}
            self._grants[g.nonce] = g
        return g

    @contextmanager
    def use(self, grant, action, keys=()):
        """Spend `grant` once, for `action` on `keys` (each must be one the check was for). Inside the block, on this thread
        only, `require(action, ...)` passes."""
        if self.closed or agent_markers_set() or not isinstance(grant, Grant):
            raise _refused()
        with self._lock:
            live = self._grants.pop(grant.nonce, None)
        if live is not grant or grant.expires < time.monotonic():
            raise _refused('That check was already used or has run out. Nothing was done; start again.')
        if grant.action != action or not set(keys) <= grant.keys:
            raise _refused('That check was for something else. Nothing was done.')
        self._held.grant = grant
        try:
            yield grant
        finally:
            self._held.grant = None

    def cancel(self):
        self._generation += 1
        if self._timer: self._timer.cancel(); self._timer = None
        if self._context: self._context.invalidate(); self._context = None
        pending, self._pending = self._pending, None
        with self._lock:
            self._grants.clear()
        if pending is not None:
            pending(False)             # replaced or cancelled: its caller hears "no" instead of waiting forever

    def close(self):
        self.closed = True
        self._pending = None           # the workspace is going: nobody is left to answer
        self.cancel()

    def require(self, action, keys=()):
        """Passes only on a thread spending a grant (`use`) for exactly `action` covering every key. Every caller names its act:
        a grant for one act never passes for another."""
        if not isinstance(action, str) or not action:
            raise TypeError('require() must name the act it checks')
        g = getattr(self._held, 'grant', None)
        if self.closed or g is None or agent_markers_set():
            raise _refused()
        if g.action != action or not set(keys) <= g.keys:
            raise _refused('That check was for something else. Nothing was done.')


def require_act(authority, action: str, keys=()) -> None:
    """The check for one named act. A real NativeAuthority (`binds`) checks the grant is for exactly this act and these keys; a
    test double without grants keeps its simple `require()`."""
    authority.require(action, tuple(keys)) if getattr(authority, 'binds', False) else authority.require()
